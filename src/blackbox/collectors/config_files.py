"""Track selected configuration files: content changes, creation and deletion."""

from __future__ import annotations

import difflib
import os
import stat
from collections.abc import Sequence

from ..model import (
    KIND_CONFIG_BASELINE,
    KIND_CONFIG_CHANGED,
    KIND_CONFIG_CREATED,
    KIND_CONFIG_DELETED,
    KIND_CONFIG_UNREADABLE,
    EventDraft,
)
from ..redact import Redactor
from ..store import FileVersion, Store
from .base import Observation

ABSENT = "absent"
UNREADABLE = "unreadable"
_CHUNK = 64 * 1024


class ConfigFilesCollector:
    name = "config_files"

    def __init__(
        self,
        paths: Sequence[str],
        redactor: Redactor | None = None,
        *,
        max_bytes: int = 1_048_576,
        diff_line_limit: int = 200,
    ) -> None:
        self.paths = [os.path.abspath(os.path.expanduser(p)) for p in paths]
        self.redactor = redactor or Redactor()
        self.max_bytes = max_bytes
        self.diff_line_limit = diff_line_limit

    def collect(self, store: Store, now_us: int) -> Observation:
        obs = Observation()
        for path in sorted(self.paths):
            self._inspect(store, path, now_us, obs)
        return obs

    # -- internals -------------------------------------------------------------

    def _inspect(self, store: Store, path: str, now_us: int, obs: Observation) -> None:
        key = f"file:{path}"
        previous = store.state_get(key)
        snapshot = self._read(store, path)

        if snapshot == ABSENT:
            if previous is None:
                obs.drafts.append(
                    EventDraft(KIND_CONFIG_BASELINE, path, "info", payload={"present": False})
                )
            elif previous != ABSENT:
                obs.drafts.append(
                    EventDraft(
                        KIND_CONFIG_DELETED,
                        path,
                        "warning",
                        before_digest=None if previous == UNREADABLE else previous,
                        payload={"present": False},
                    )
                )
            obs.state[key] = ABSENT
            return

        if isinstance(snapshot, str):  # UNREADABLE with a reason after a colon
            if previous != UNREADABLE:
                obs.drafts.append(
                    EventDraft(
                        KIND_CONFIG_UNREADABLE,
                        path,
                        "warning",
                        payload={"reason": snapshot.partition(":")[2]},
                    )
                )
            obs.state[key] = UNREADABLE
            return

        digest, size, text, mtime_us = (
            snapshot.digest,
            snapshot.size,
            snapshot.text,
            snapshot.mtime_us,
        )
        if previous == digest:
            return

        stored_text = None if text is None else self.redactor.redact(text)
        obs.versions.append(FileVersion(path, digest, size, now_us, stored_text))
        obs.state[key] = digest
        payload: dict[str, object] = {
            "present": True,
            "size": size,
            "content_stored": stored_text is not None,
            "mtime_us": mtime_us,
        }

        if previous is None:
            obs.drafts.append(
                EventDraft(KIND_CONFIG_BASELINE, path, "info", after_digest=digest, payload=payload)
            )
            return

        if previous in (ABSENT, UNREADABLE):
            obs.drafts.append(
                EventDraft(
                    KIND_CONFIG_CREATED, path, "warning", after_digest=digest, payload=payload
                )
            )
            return

        old = store.latest_version(path)
        old_text = None if old is None else old.content
        payload["size_before"] = None if old is None else old.size
        if stored_text is not None and old_text is not None:
            diff, truncated = self._diff(path, old_text, stored_text)
            payload["diff"] = diff
            payload["diff_truncated"] = truncated
            payload["lines_added"] = sum(
                1
                for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            payload["lines_removed"] = sum(
                1
                for line in diff.splitlines()
                if line.startswith("-") and not line.startswith("---")
            )
            payload["redacted_only"] = old_text == stored_text
        else:
            payload["diff"] = None
            payload["note"] = (
                "no stored content for before/after (binary, oversize or first sighting)"
            )
        obs.drafts.append(
            EventDraft(
                KIND_CONFIG_CHANGED,
                path,
                "warning",
                before_digest=previous,
                after_digest=digest,
                payload=payload,
            )
        )

    def _diff(self, path: str, before: str, after: str) -> tuple[str, bool]:
        lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"{path} (before)",
                tofile=f"{path} (after)",
                lineterm="",
                n=3,
            )
        )
        truncated = len(lines) > self.diff_line_limit
        if truncated:
            lines = [*lines[: self.diff_line_limit], "... diff truncated ..."]
        return "\n".join(lines), truncated

    def _read(self, store: Store, path: str) -> _Snapshot | str:
        """Return a snapshot, ``ABSENT``, or ``"unreadable:<reason>"``."""
        try:
            # Refuse non-regular files before opening: opening a FIFO would block forever.
            if not stat.S_ISREG(os.stat(path).st_mode):
                return f"{UNREADABLE}:not a regular file"
            with open(path, "rb") as handle:
                file_stat = os.fstat(handle.fileno())
                hasher = store.hasher()
                head = handle.read(self.max_bytes + 1)
                hasher.update(head)
                size = len(head)
                oversize = size > self.max_bytes
                if oversize:
                    while chunk := handle.read(_CHUNK):
                        hasher.update(chunk)
                        size += len(chunk)
        except FileNotFoundError:
            return ABSENT
        except OSError as exc:
            return f"{UNREADABLE}:{exc.strerror or type(exc).__name__}"
        text: str | None = None
        if not oversize and b"\x00" not in head:
            text = head.decode("utf-8", errors="replace")
        return _Snapshot(
            digest=hasher.hexdigest(),
            size=size,
            text=text,
            mtime_us=int(file_stat.st_mtime_ns // 1_000),
        )


class _Snapshot:
    __slots__ = ("digest", "mtime_us", "size", "text")

    def __init__(self, digest: str, size: int, text: str | None, mtime_us: int) -> None:
        self.digest = digest
        self.size = size
        self.text = text
        self.mtime_us = mtime_us
