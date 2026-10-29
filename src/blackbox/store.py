"""SQLite-backed, append-only, hash-chained event store.

Design notes (see docs/adr/0001-sqlite-hash-chain-store.md):

* Events are append-only. Triggers reject UPDATE and DELETE on the ``events``
  table.
* Every event carries ``hash = SHA-256(prev_hash || canonical fields)``. Editing
  or removing a row in the middle of the history breaks the chain and is found
  by :meth:`Store.verify_chain`. This is *tamper evidence*, not tamper proofing:
  someone with write access to the file can rebuild the chain. Record the head
  hash somewhere else (``blackbox status`` prints it) to detect that too.
* File digests are HMAC-SHA-256 with a per-database random key, so a stored
  digest does not let anyone test guesses against a low-entropy secret.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from .model import SEVERITY_RANK, Event, EventDraft

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_us         INTEGER NOT NULL,
    machine_id    TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    subject       TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    before_digest TEXT,
    after_digest  TEXT,
    payload       TEXT    NOT NULL,
    prev_hash     TEXT    NOT NULL,
    hash          TEXT    NOT NULL
);
CREATE INDEX idx_events_ts ON events (ts_us, id);
CREATE INDEX idx_events_kind ON events (kind);
CREATE INDEX idx_events_subject ON events (subject);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'blackbox events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'blackbox events are append-only'); END;
CREATE TABLE file_versions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    path    TEXT    NOT NULL,
    digest  TEXT    NOT NULL,
    size    INTEGER NOT NULL,
    ts_us   INTEGER NOT NULL,
    content TEXT
);
CREATE INDEX idx_file_versions_path ON file_versions (path, id);
CREATE TABLE state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StoreError(RuntimeError):
    """Raised for unusable or incompatible databases."""


@dataclass(frozen=True)
class FileVersion:
    path: str
    digest: str
    size: int
    ts_us: int
    content: str | None


@dataclass(frozen=True)
class ChainResult:
    ok: bool
    checked: int
    head: str
    message: str


def now_us() -> int:
    return time.time_ns() // 1_000


def compute_hash(
    prev_hash: str,
    ts_us: int,
    machine_id: str,
    kind: str,
    subject: str,
    severity: str,
    before_digest: str | None,
    after_digest: str | None,
    payload_json: str,
) -> str:
    material = json.dumps(
        [ts_us, machine_id, kind, subject, severity, before_digest, after_digest, payload_json],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(f"{prev_hash}\n{material}".encode("ascii")).hexdigest()


class Store:
    """Append-only event store plus the small amount of state collectors need."""

    def __init__(
        self,
        path: str | Path,
        machine_id: str,
        clock: Callable[[], int] = now_us,
    ) -> None:
        self.path = str(path)
        self.machine_id = machine_id
        self._clock = clock
        _create_owner_only(self.path)
        try:
            self._conn = sqlite3.connect(self.path, isolation_level=None)
        except sqlite3.Error as exc:
            raise StoreError(f"cannot open database {self.path}: {exc}") from exc
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            self._migrate()
            self._key = bytes.fromhex(self._meta_get("hash_key") or "")
        except (sqlite3.Error, StoreError) as exc:
            self._conn.close()  # do not leak the handle when the database is unusable
            if isinstance(exc, StoreError):
                raise
            raise StoreError(f"cannot use database {self.path}: {exc}") from exc

    # -- lifecycle -------------------------------------------------------------

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"database schema v{version} is newer than this blackbox (v{SCHEMA_VERSION}); "
                "upgrade blackbox"
            )
        if version == 0:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split_statements(_SCHEMA):
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('hash_key', ?)",
                    (secrets.token_hex(32),),
                )
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('created_us', ?)", (str(self._clock()),)
                )
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self._conn.execute("ROLLBACK")
                raise StoreError(f"cannot initialise database: {exc}") from exc

    # -- meta / state ----------------------------------------------------------

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def meta_get(self, key: str) -> str | None:
        if key == "hash_key":
            raise StoreError("hash_key is private")
        return self._meta_get(key)

    def meta_set(self, key: str, value: str) -> None:
        if key == "hash_key":
            raise StoreError("hash_key is private")
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def state_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def state_items(self, prefix: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM state WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (_like_escape(prefix) + "%",),
        ).fetchall()
        return {str(k): str(v) for k, v in rows}

    def digest(self, data: bytes) -> str:
        """Keyed digest of raw content (see module docstring)."""
        return hmac.new(self._key, data, hashlib.sha256).hexdigest()

    def hasher(self) -> hmac.HMAC:
        """Incremental keyed hasher, for content too large to hold in memory."""
        return hmac.new(self._key, digestmod=hashlib.sha256)

    # -- writing ---------------------------------------------------------------

    def commit(
        self,
        drafts: Sequence[EventDraft] = (),
        *,
        state: Mapping[str, str | None] | None = None,
        versions: Iterable[FileVersion] = (),
        ts_us: int | None = None,
    ) -> list[Event]:
        """Atomically append events, update collector state and store file versions."""
        stamp = self._clock() if ts_us is None else ts_us
        stored: list[Event] = []
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            prev_hash = self._head_hash()
            for draft in drafts:
                payload_json = json.dumps(draft.payload, sort_keys=True, separators=(",", ":"))
                event_hash = compute_hash(
                    prev_hash,
                    stamp,
                    self.machine_id,
                    draft.kind,
                    draft.subject,
                    draft.severity,
                    draft.before_digest,
                    draft.after_digest,
                    payload_json,
                )
                cursor = self._conn.execute(
                    "INSERT INTO events (ts_us, machine_id, kind, subject, severity, "
                    "before_digest, after_digest, payload, prev_hash, hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stamp,
                        self.machine_id,
                        draft.kind,
                        draft.subject,
                        draft.severity,
                        draft.before_digest,
                        draft.after_digest,
                        payload_json,
                        prev_hash,
                        event_hash,
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is None:  # pragma: no cover - sqlite always sets it on INSERT
                    raise StoreError("insert returned no row id")
                stored.append(
                    Event(
                        id=row_id,
                        ts_us=stamp,
                        machine_id=self.machine_id,
                        kind=draft.kind,
                        subject=draft.subject,
                        severity=draft.severity,
                        before_digest=draft.before_digest,
                        after_digest=draft.after_digest,
                        payload=json.loads(payload_json),
                        prev_hash=prev_hash,
                        hash=event_hash,
                    )
                )
                prev_hash = event_hash
            for version in versions:
                self._conn.execute(
                    "INSERT INTO file_versions (path, digest, size, ts_us, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (version.path, version.digest, version.size, version.ts_us, version.content),
                )
            for key, value in (state or {}).items():
                if value is None:
                    self._conn.execute("DELETE FROM state WHERE key = ?", (key,))
                else:
                    self._conn.execute(
                        "INSERT INTO state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value),
                    )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return stored

    def _head_hash(self) -> str:
        row = self._conn.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        return GENESIS_HASH if row is None else str(row[0])

    def head_hash(self) -> str:
        return self._head_hash()

    # -- reading ---------------------------------------------------------------

    def latest_version(self, path: str) -> FileVersion | None:
        row = self._conn.execute(
            "SELECT path, digest, size, ts_us, content FROM file_versions "
            "WHERE path = ? ORDER BY id DESC LIMIT 1",
            (path,),
        ).fetchone()
        return None if row is None else FileVersion(*row)

    def count_events(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def events(
        self,
        *,
        since_us: int | None = None,
        until_us: int | None = None,
        kinds: Sequence[str] | None = None,
        kind_prefix: str | None = None,
        subject: str | None = None,
        min_severity: str | None = None,
        limit: int | None = None,
        newest_first: bool = False,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list[Any] = []
        if since_us is not None:
            clauses.append("ts_us >= ?")
            params.append(since_us)
        if until_us is not None:
            clauses.append("ts_us <= ?")
            params.append(until_us)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if kind_prefix:
            clauses.append("kind LIKE ? ESCAPE '\\'")
            params.append(_like_escape(kind_prefix) + "%")
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        if min_severity is not None:
            wanted = [n for n, r in SEVERITY_RANK.items() if r >= SEVERITY_RANK[min_severity]]
            clauses.append(f"severity IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM events {where} ORDER BY ts_us {direction}, id {direction}"  # noqa: S608
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_event(row) for row in self._conn.execute(sql, params).fetchall()]

    def search(self, term: str, *, limit: int = 200) -> list[Event]:
        """Case-insensitive substring search over kind, subject and payload."""
        pattern = f"%{_like_escape(term)}%"
        rows = self._conn.execute(
            "SELECT * FROM events WHERE kind LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' "
            "OR payload LIKE ? ESCAPE '\\' ORDER BY ts_us ASC, id ASC LIMIT ?",
            (pattern, pattern, pattern, limit),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    # -- integrity -------------------------------------------------------------

    def verify_chain(self, expected_head: str | None = None) -> ChainResult:
        """Recompute the hash chain. ``expected_head`` is an externally recorded anchor."""
        prev = GENESIS_HASH
        expected_id = 1
        checked = 0
        cursor = self._conn.execute("SELECT * FROM events ORDER BY id ASC")
        for row in cursor:
            event = _row_to_event(row)
            if event.id != expected_id:
                return ChainResult(
                    False, checked, prev, f"event id gap: expected {expected_id}, found {event.id}"
                )
            if event.prev_hash != prev:
                return ChainResult(
                    False, checked, prev, f"event {event.id}: prev_hash does not match chain"
                )
            payload_json = str(row[8])
            recomputed = compute_hash(
                prev,
                event.ts_us,
                event.machine_id,
                event.kind,
                event.subject,
                event.severity,
                event.before_digest,
                event.after_digest,
                payload_json,
            )
            if recomputed != event.hash:
                return ChainResult(False, checked, prev, f"event {event.id}: hash mismatch")
            prev = event.hash
            expected_id += 1
            checked += 1
        if expected_head is not None and expected_head != prev:
            return ChainResult(
                False,
                checked,
                prev,
                "chain is internally consistent but its head does not match the recorded anchor "
                "(events may have been removed from the end)",
            )
        return ChainResult(True, checked, prev, "chain intact")


def _row_to_event(row: Sequence[Any]) -> Event:
    return Event(
        id=int(row[0]),
        ts_us=int(row[1]),
        machine_id=str(row[2]),
        kind=str(row[3]),
        subject=str(row[4]),
        severity=str(row[5]),
        before_digest=None if row[6] is None else str(row[6]),
        after_digest=None if row[7] is None else str(row[7]),
        payload=json.loads(str(row[8])),
        prev_hash=str(row[9]),
        hash=str(row[10]),
    )


def _create_owner_only(path: str) -> None:
    """Create the database file with mode 0600 *before* SQLite first opens it.

    The database and its -wal/-shm sidecars hold redacted configuration content.
    SQLite creates the sidecars with the main file's permissions, so the main file
    must already be restricted; changing the mode afterwards would leave sidecars
    that were created earlier world-readable. On Windows, access follows the
    directory ACL instead.
    """
    if path == ":memory:" or os.name != "posix" or os.path.exists(path):
        return
    # On failure, sqlite3.connect() below reports the real problem with a clear error.
    with contextlib.suppress(OSError):
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _split_statements(script: str) -> list[str]:
    """Split the schema script, keeping CREATE TRIGGER ... END; bodies intact."""
    statements: list[str] = []
    buffer: list[str] = []
    in_trigger = False
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buffer.append(line)
        if stripped.upper().startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if stripped.upper().endswith("END;"):
                statements.append("\n".join(buffer))
                buffer, in_trigger = [], False
        elif stripped.endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    return statements
