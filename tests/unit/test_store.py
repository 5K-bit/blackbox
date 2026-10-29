from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from blackbox.model import EventDraft
from blackbox.store import GENESIS_HASH, FileVersion, Store, StoreError


def drafts(n: int) -> list[EventDraft]:
    return [EventDraft("recorder.note", f"s{i}", "info", payload={"i": i}) for i in range(n)]


def test_commit_chains_events_from_genesis(store: Store) -> None:
    events = store.commit(drafts(3))
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == events[0].hash
    assert events[2].prev_hash == events[1].hash
    assert store.head_hash() == events[2].hash
    result = store.verify_chain()
    assert result.ok and result.checked == 3


def test_empty_chain_is_valid(store: Store) -> None:
    result = store.verify_chain()
    assert result.ok and result.checked == 0 and result.head == GENESIS_HASH


def test_events_are_append_only(store: Store) -> None:
    store.commit(drafts(1))
    conn = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE events SET subject = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM events")
    conn.close()


def _bypass(path: str, *statements: str) -> None:
    """Simulate an attacker with file access who drops the triggers first."""
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("DROP TRIGGER events_no_delete")
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def test_edit_in_the_middle_is_detected(store: Store) -> None:
    store.commit(drafts(4))
    _bypass(store.path, "UPDATE events SET subject = 'forged' WHERE id = 2")
    result = store.verify_chain()
    assert not result.ok
    assert "event 2" in result.message


def test_deleting_a_middle_event_is_detected(store: Store) -> None:
    store.commit(drafts(4))
    _bypass(store.path, "DELETE FROM events WHERE id = 2")
    result = store.verify_chain()
    assert not result.ok
    assert "gap" in result.message


def test_trailing_deletion_needs_an_external_anchor(store: Store) -> None:
    events = store.commit(drafts(3))
    anchor = events[2].hash
    _bypass(store.path, "DELETE FROM events WHERE id = 3")
    assert store.verify_chain().ok  # internally consistent
    result = store.verify_chain(expected_head=anchor)
    assert not result.ok
    assert "anchor" in result.message


def test_anchor_matches_when_untouched(store: Store) -> None:
    events = store.commit(drafts(2))
    assert store.verify_chain(expected_head=events[1].hash).ok


def test_commit_is_atomic(store: Store) -> None:
    store.commit(drafts(1))
    bad = [EventDraft("a.b", "ok"), EventDraft("a.b", "bad", payload={"x": object()})]
    with pytest.raises(TypeError):
        store.commit(bad)
    assert store.count_events() == 1
    assert store.verify_chain().ok


def test_state_and_versions_commit_with_events(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    version = FileVersion("/etc/a", "d1", 3, clock(), "abc")
    store.commit(drafts(1), state={"file:/etc/a": "d1", "x": "1"}, versions=[version])
    assert store.state_get("file:/etc/a") == "d1"
    assert store.latest_version("/etc/a") == version
    store.commit(state={"x": None})
    assert store.state_get("x") is None
    assert store.state_items("file:") == {"file:/etc/a": "d1"}


def test_state_prefix_is_not_a_wildcard(store: Store) -> None:
    store.commit(state={"a_b": "1", "axb": "2"})
    assert store.state_items("a_") == {"a_b": "1"}


def test_digest_is_keyed_per_database(tmp_path: Path) -> None:
    with Store(tmp_path / "one.db", "h") as one, Store(tmp_path / "two.db", "h") as two:
        assert one.digest(b"secret") == one.digest(b"secret")
        assert one.digest(b"secret") != two.digest(b"secret")
        hasher = one.hasher()
        hasher.update(b"sec")
        hasher.update(b"ret")
        assert hasher.hexdigest() == one.digest(b"secret")


def test_digest_key_is_not_exposed(store: Store) -> None:
    with pytest.raises(StoreError):
        store.meta_get("hash_key")
    with pytest.raises(StoreError):
        store.meta_set("hash_key", "x")


def test_filters_and_ordering(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    store.commit([EventDraft("config.changed", "/a", "warning")], ts_us=clock.advance(1))
    store.commit([EventDraft("service.failed", "api", "error")], ts_us=clock.advance(1))
    store.commit([EventDraft("service.recovered", "api", "info")], ts_us=clock.advance(1))
    assert [e.kind for e in store.events()] == [
        "config.changed",
        "service.failed",
        "service.recovered",
    ]
    assert [e.kind for e in store.events(newest_first=True, limit=1)] == ["service.recovered"]
    assert [e.kind for e in store.events(kind_prefix="service.")] == [
        "service.failed",
        "service.recovered",
    ]
    assert [e.kind for e in store.events(min_severity="error")] == ["service.failed"]
    assert [e.kind for e in store.events(subject="api", kinds=["service.failed"])] == [
        "service.failed"
    ]


def test_search_is_literal_not_a_pattern(store: Store) -> None:
    store.commit(
        [
            EventDraft("a.b", "100%_done", "info", payload={"note": "x"}),
            EventDraft("a.b", "other", "info", payload={"note": "needle here"}),
        ]
    )
    assert [e.subject for e in store.search("100%_")] == ["100%_done"]
    assert [e.subject for e in store.search("NEEDLE")] == ["other"]
    assert store.search("%") == [store.events()[0]]


def test_reopening_preserves_chain_and_key(tmp_path: Path) -> None:
    path = tmp_path / "bb.db"
    with Store(path, "h") as first:
        first.commit(drafts(2))
        digest = first.digest(b"x")
        head = first.head_hash()
    with Store(path, "h") as second:
        assert second.head_hash() == head
        assert second.digest(b"x") == digest
        second.commit(drafts(1))
        assert second.verify_chain().checked == 3


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bb.db"
    Store(path, "h").close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(StoreError, match="newer"):
        Store(path, "h")
    path.unlink()  # fails on Windows if the refused database handle was leaked


def test_unopenable_path_raises_store_error(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        Store(tmp_path / "missing-dir" / "bb.db", "h")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_new_database_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "bb.db"
    with Store(path, "h") as store:
        store.commit(drafts(1))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        for sidecar in (tmp_path / "bb.db-wal", tmp_path / "bb.db-shm"):
            if sidecar.exists():
                assert stat.S_IMODE(os.stat(sidecar).st_mode) & 0o077 == 0
