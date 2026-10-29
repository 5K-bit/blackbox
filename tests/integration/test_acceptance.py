"""v0.1 acceptance: a real service crashes after a config edit; the report shows why."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ACCEPTANCE = Path(__file__).resolve().parents[2] / "examples" / "acceptance.py"


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("acceptance", ACCEPTANCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["acceptance"] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module, module.run_scenario(tmp_path_factory.mktemp("acceptance"))


def test_the_real_failure_was_observed(result) -> None:  # type: ignore[no-untyped-def]
    _, r = result
    failures = [f for f in r.incident.findings if f.failure.kind == "service.failed"]
    assert len(failures) == 1
    assert failures[0].failure.subject == "demo-api"
    assert failures[0].failure.payload["from"] == "up"


def test_report_orders_config_change_before_service_failure(result) -> None:  # type: ignore[no-untyped-def]
    _, r = result
    finding = next(f for f in r.incident.findings if f.failure.kind == "service.failed")
    nearest = finding.precursors[0]
    assert nearest.event.kind == "config.changed"
    assert nearest.event.subject.endswith("app.conf")
    assert nearest.lead_us > 0


def test_the_unrelated_edit_is_listed_but_ranked_further_away(result) -> None:  # type: ignore[no-untyped-def]
    _, r = result
    finding = next(f for f in r.incident.findings if f.failure.kind == "service.failed")
    changed = [p for p in finding.precursors if p.event.kind == "config.changed"]
    assert [Path(p.event.subject).name for p in changed] == ["app.conf", "db.conf"]
    assert changed[0].lead_us < changed[1].lead_us


def test_diff_is_attached_and_secrets_are_redacted(result) -> None:  # type: ignore[no-untyped-def]
    mod, r = result
    diff = next(
        p.event.payload["diff"]
        for f in r.incident.findings
        for p in f.precursors
        if p.event.subject.endswith("app.conf") and p.event.kind == "config.changed"
    )
    assert "-mode = ok" in diff and "+mode = broken" in diff
    assert "-workers = 4" in diff and "+workers = 400" in diff
    for text in (r.markdown, r.html, r.json_text, diff):
        assert mod.SECRET_BEFORE not in text and mod.SECRET_AFTER not in text


def test_secrets_never_reach_the_database_or_its_wal(result) -> None:  # type: ignore[no-untyped-def]
    mod, r = result
    secrets = (mod.SECRET_BEFORE.encode(), mod.SECRET_AFTER.encode())
    names = [name for name, _ in r.live_db_bytes]
    assert "blackbox.db-wal" in names, (
        "the WAL must have been captured, or this test proves nothing"
    )
    for name, raw in r.live_db_bytes:  # captured while the recorder was still running
        assert not any(secret in raw for secret in secrets), name
    for path in r.workdir.glob("blackbox.db*"):  # and again after the clean close
        assert not any(secret in path.read_bytes() for secret in secrets), path.name


def test_three_files_tracked_and_chain_intact(result) -> None:  # type: ignore[no-untyped-def]
    _, r = result
    assert r.incident.chain.ok
    conn = sqlite3.connect(r.db_path)
    tracked = conn.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'file:%'").fetchone()[0]
    conn.close()
    assert tracked == 3
