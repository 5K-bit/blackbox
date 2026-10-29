from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from blackbox.cli import EXIT_ERROR, EXIT_INTEGRITY, EXIT_NO_INCIDENT, EXIT_OK, main


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with a config tracking one file and one command-probed service."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.conf").write_text("mode = ok\npassword = hunter2\n")
    flag = tmp_path / "healthy"
    flag.write_text("")
    probe = f"import os,sys; sys.exit(0 if os.path.exists(r'{flag}') else 1)"
    (tmp_path / "blackbox.toml").write_text(
        f"""
machine_id = "cli-host"
interval_seconds = 1
[files]
paths = ["app.conf"]
[[services]]
name = "worker"
kind = "command"
command = [{json.dumps(sys.executable)}, "-c", {json.dumps(probe)}]
[resources]
enabled = false
"""
    )
    return tmp_path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def break_things(lab: Path) -> None:
    (lab / "app.conf").write_text("mode = broken\npassword = hunter3\n")
    (lab / "healthy").unlink()


def test_init_creates_config_and_database_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "init")
    assert code == EXIT_OK and (tmp_path / "blackbox.toml").exists()
    assert (tmp_path / "blackbox.db").exists() and "wrote" in out
    code, _, err = run(capsys, "init")
    assert code == EXIT_ERROR and "already exists" in err
    assert run(capsys, "init", "--force")[0] == EXIT_OK


def test_full_flow_from_baseline_to_incident_report(
    lab: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, "run", "--once")[0] == EXIT_OK  # baseline
    break_things(lab)
    code, out, _ = run(capsys, "run", "--once")
    assert code == EXIT_OK
    assert "config.changed" in out and "service.failed" in out  # echoed live

    code, out, _ = run(capsys, "timeline", "--since", "1h", "--min-severity", "warning")
    assert code == EXIT_OK
    assert "config.changed" in out and "service.failed" in out and "config.baseline" not in out

    code, out, _ = run(capsys, "timeline", "--kind", "service", "--json")
    rows = [json.loads(line) for line in out.splitlines()]
    assert {r["kind"] for r in rows} == {"service.baseline", "service.failed"}

    code, out, _ = run(capsys, "search", "worker")
    assert code == EXIT_OK and "service.failed" in out
    assert "no events match" in run(capsys, "search", "zzz-nothing")[1]

    code, out, _ = run(capsys, "diff", "app.conf")
    assert code == EXIT_OK and "-mode = ok" in out and "+mode = broken" in out
    assert "hunter" not in out

    code, out, _ = run(capsys, "incident")
    assert code == EXIT_OK
    assert "service.failed on worker" in out and "config.changed on" in out
    assert "hunter" not in out

    code, out, _ = run(capsys, "incident", "--out", "report.html")
    assert code == EXIT_OK and (lab / "report.html").read_text().startswith("<!doctype html>")
    code, _, _ = run(capsys, "incident", "--out", "report.json")
    data = json.loads((lab / "report.json").read_text())
    assert data["findings"][0]["failure"]["kind"] == "service.failed"
    assert run(capsys, "incident", "--format", "json")[1].lstrip().startswith("{")

    code, out, _ = run(capsys, "status")
    assert code == EXIT_OK and "hash chain     intact" in out and "worker  [down]" in out

    code, out, _ = run(capsys, "verify")
    assert code == EXIT_OK and out.startswith("OK")
    head = next(line.split()[-1] for line in out.splitlines() if line.startswith("chain head"))
    assert run(capsys, "verify", "--anchor", head)[0] == EXIT_OK
    assert run(capsys, "verify", "--anchor", "0" * 64)[0] == EXIT_INTEGRITY


def test_tampering_is_reported_with_exit_code_2(
    lab: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "run", "--once")
    conn = sqlite3.connect(lab / "blackbox.db")
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET subject = 'forged' WHERE id = 2")
    conn.commit()
    conn.close()
    code, out, _ = run(capsys, "verify")
    assert code == EXIT_INTEGRITY and out.startswith("FAILED")
    assert run(capsys, "status")[0] == EXIT_INTEGRITY


def test_incident_with_no_failures_exits_3(lab: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys, "run", "--once")
    code, _, err = run(capsys, "incident")
    assert code == EXIT_NO_INCIDENT and "no failures" in err


def test_diff_for_untracked_or_unchanged_file_is_an_error(
    lab: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "run", "--once")
    code, _, err = run(capsys, "diff", "app.conf")
    assert code == EXIT_ERROR and "no recorded change" in err


@pytest.mark.parametrize(
    "argv",
    [
        ("timeline", "--since", "yesterday-ish"),
        ("incident", "--window", "soon"),
        ("diff", "x", "--at", "never"),
    ],
)
def test_bad_arguments_exit_1_with_a_message(
    lab: Path, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    code, _, err = run(capsys, *argv)
    assert code == EXIT_ERROR and err.startswith("blackbox: error:")


def test_missing_config_is_a_helpful_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = run(capsys, "status")
    assert code == EXIT_ERROR and "blackbox init" in err


def test_run_with_nothing_to_watch_explains_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blackbox.toml").write_text("[resources]\nenabled = false\n")
    code, _, err = run(capsys, "run", "--once")
    assert code == EXIT_ERROR and "nothing to record" in err


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys)
    assert code == EXIT_ERROR and "usage:" in out


def test_duration_stops_a_continuous_run_cleanly(
    lab: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "run", "--duration", "1s", "--quiet")
    assert code == EXIT_OK and out == ""
    _, timeline, _ = run(capsys, "timeline", "--kind", "recorder")
    assert "recorder.started" in timeline and "recorder.stopped" in timeline
