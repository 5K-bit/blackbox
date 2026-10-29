from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from blackbox.collectors.config_files import ConfigFilesCollector
from blackbox.store import Store


def tick(store: Store, collector: ConfigFilesCollector, clock) -> list:  # type: ignore[no-untyped-def]
    clock.advance(10)
    obs = collector.collect(store, clock())
    return store.commit(obs.drafts, state=obs.state, versions=obs.versions, ts_us=clock())


def test_baseline_then_no_noise(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "app.conf"
    f.write_text("a = 1\n")
    col = ConfigFilesCollector([str(f)])
    first = tick(store, col, clock)
    assert [e.kind for e in first] == ["config.baseline"]
    assert tick(store, col, clock) == []


def test_change_records_redacted_unified_diff(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "app.conf"
    f.write_text("port = 8080\npassword = old-secret\n")
    col = ConfigFilesCollector([str(f)])
    tick(store, col, clock)
    f.write_text("port = 9090\npassword = new-secret\n")
    (event,) = tick(store, col, clock)
    assert event.kind == "config.changed" and event.severity == "warning"
    diff = event.payload["diff"]
    assert "-port = 8080" in diff and "+port = 9090" in diff
    assert "old-secret" not in diff and "new-secret" not in diff
    assert event.payload["lines_added"] == 1 and event.payload["lines_removed"] == 1
    assert event.before_digest != event.after_digest
    # the stored content never contains the secret either
    version = store.latest_version(str(f))
    assert version is not None and version.content is not None
    assert "new-secret" not in version.content


def test_secret_rotation_is_a_change_with_no_visible_diff(
    store: Store, tmp_path: Path, clock
) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "app.conf"
    f.write_text("password = one\n")
    col = ConfigFilesCollector([str(f)])
    tick(store, col, clock)
    f.write_text("password = two\n")
    (event,) = tick(store, col, clock)
    assert event.kind == "config.changed"
    assert event.payload["redacted_only"] is True
    assert event.payload["diff"] == ""


def test_delete_then_recreate(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "app.conf"
    f.write_text("a = 1\n")
    col = ConfigFilesCollector([str(f)])
    tick(store, col, clock)
    f.unlink()
    assert [e.kind for e in tick(store, col, clock)] == ["config.deleted"]
    assert tick(store, col, clock) == []
    f.write_text("a = 2\n")
    assert [e.kind for e in tick(store, col, clock)] == ["config.created"]


def test_file_missing_at_start_is_a_baseline_not_an_alarm(
    store: Store, tmp_path: Path, clock
) -> None:  # type: ignore[no-untyped-def]
    col = ConfigFilesCollector([str(tmp_path / "later.conf")])
    (event,) = tick(store, col, clock)
    assert event.kind == "config.baseline" and event.payload == {"present": False}
    (tmp_path / "later.conf").write_text("x")
    assert [e.kind for e in tick(store, col, clock)] == ["config.created"]


def test_change_made_while_stopped_is_found_on_next_run(
    store: Store, tmp_path: Path, clock
) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "app.conf"
    f.write_text("a = 1\n")
    tick(store, ConfigFilesCollector([str(f)]), clock)
    f.write_text("a = 2\n")  # nobody is watching
    (event,) = tick(store, ConfigFilesCollector([str(f)]), clock)  # a brand new collector
    assert event.kind == "config.changed"
    assert "+a = 2" in event.payload["diff"]


def test_mtime_is_recorded(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "app.conf"
    f.write_text("a = 1\n")
    os.utime(f, (1_700_000_000, 1_700_000_000))
    (event,) = tick(store, ConfigFilesCollector([str(f)]), clock)
    assert event.payload["mtime_us"] == 1_700_000_000_000_000


def test_oversize_files_are_hash_only(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "big.conf"
    f.write_text("x" * 200)
    col = ConfigFilesCollector([str(f)], max_bytes=100)
    (baseline,) = tick(store, col, clock)
    assert baseline.payload["content_stored"] is False and baseline.payload["size"] == 200
    f.write_text("y" * 250)
    (event,) = tick(store, col, clock)
    assert event.kind == "config.changed" and event.payload["diff"] is None
    assert event.payload["size"] == 250


def test_binary_files_are_hash_only(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02")
    col = ConfigFilesCollector([str(f)])
    (baseline,) = tick(store, col, clock)
    assert baseline.payload["content_stored"] is False
    f.write_bytes(b"\x00\x01\x03")
    (event,) = tick(store, col, clock)
    assert event.kind == "config.changed" and event.payload["diff"] is None


def test_unreadable_path_is_reported_once(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    d = tmp_path / "actually-a-dir"
    d.mkdir()
    col = ConfigFilesCollector([str(d)])
    (event,) = tick(store, col, clock)
    assert event.kind == "config.unreadable"
    assert tick(store, col, clock) == []


def test_long_diffs_are_truncated(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "long.conf"
    f.write_text("\n".join(f"a{i} = 1" for i in range(300)))
    col = ConfigFilesCollector([str(f)], diff_line_limit=20)
    tick(store, col, clock)
    f.write_text("\n".join(f"a{i} = 2" for i in range(300)))
    (event,) = tick(store, col, clock)
    assert event.payload["diff_truncated"] is True
    assert event.payload["diff"].splitlines()[-1] == "... diff truncated ..."


def test_directory_is_reported_as_not_a_regular_file(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    (event,) = tick(store, ConfigFilesCollector([str(tmp_path)]), clock)
    assert event.kind == "config.unreadable"
    assert event.payload["reason"] == "not a regular file"


@pytest.mark.skipif(sys.platform == "win32", reason="FIFOs are POSIX-only")
def test_fifo_does_not_block_the_recorder(store: Store, tmp_path: Path, clock) -> None:  # type: ignore[no-untyped-def]
    fifo = tmp_path / "pipe.conf"
    os.mkfifo(fifo)  # open() on this with no writer would block forever
    (event,) = tick(store, ConfigFilesCollector([str(fifo)]), clock)
    assert event.kind == "config.unreadable"
    assert event.payload["reason"] == "not a regular file"
