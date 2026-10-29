from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from blackbox.model import EventDraft
from blackbox.report import (
    NoIncidentError,
    build_incident,
    effective_ts_us,
    human_delta,
    render_html,
    render_markdown,
    to_dict,
)
from blackbox.store import Store
from conftest import at

FIXTURES = Path(__file__).parent.parent / "fixtures"
CONF = "/etc/app/app.conf"
DIFF = "\n".join(
    [
        f"--- {CONF} (before)",
        f"+++ {CONF} (after)",
        "@@ -1,3 +1,3 @@",
        " listen = 8080",
        "-workers = 4",
        "+workers = 400",
        " log_level = info",
    ]
)


def scenario(store: Store) -> None:
    def put(when: str, ev_kind: str, subject: str, severity: str, /, **payload: object) -> None:
        store.commit([EventDraft(ev_kind, subject, severity, payload=payload)], ts_us=at(when))

    put("2026-10-29T14:00:00Z", "recorder.started", "recorder", "info", version="0.1.0")
    put("2026-10-29T14:00:10Z", "config.baseline", CONF, "info", present=True)
    put(
        "2026-10-29T14:00:10Z",
        "service.baseline",
        "api",
        "info",
        kind="tcp",
        status="up",
        detail="ok",
    )
    put(
        "2026-10-29T14:12:03Z",
        "config.changed",
        CONF,
        "warning",
        mtime_us=at("2026-10-29T14:12:01Z"),
        diff=DIFF,
        lines_added=1,
        lines_removed=1,
        redacted_only=False,
        size=60,
    )
    put(
        "2026-10-29T14:12:13Z",
        "service.failed",
        "api",
        "error",
        kind="tcp",
        status="down",
        detail="127.0.0.1:8080: Connection refused",
        **{"from": "up"},
    )
    put(
        "2026-10-29T14:12:23Z",
        "resource.spike",
        "cpu",
        "warning",
        value=97.0,
        floor=85.0,
        baseline_mean=20.0,
        baseline_std=1.5,
        baseline_samples=30,
    )
    put(
        "2026-10-29T14:14:03Z",
        "service.recovered",
        "api",
        "info",
        kind="tcp",
        status="up",
        detail="ok",
        **{"from": "down"},
    )


@pytest.fixture
def populated(tmp_path: Path) -> Store:
    with Store(tmp_path / "g.db", "web-01") as s:
        scenario(s)
        yield s


def incident(store: Store, around: str | None = "2026-10-29T14:12:13Z"):  # type: ignore[no-untyped-def]
    return build_incident(
        store,
        around_us=None if around is None else at(around),
        window_us=30 * 60 * 1_000_000,
        after_us=5 * 60 * 1_000_000,
        generated_us=at("2026-10-29T14:20:00Z"),
    )


def test_config_change_precedes_the_failure_with_its_diff(populated: Store) -> None:
    inc = incident(populated)
    first = inc.findings[0]
    assert first.failure.kind == "service.failed"
    nearest = first.precursors[0]
    assert nearest.event.kind == "config.changed"
    assert nearest.lead_us == 12 * 1_000_000  # 14:12:01 mtime -> 14:12:13 detection
    assert "-workers = 4" in nearest.event.payload["diff"]


def test_precursors_are_nearest_first_and_include_earlier_failures(populated: Store) -> None:
    inc = incident(populated)
    spike = next(f for f in inc.findings if f.failure.kind == "resource.spike")
    assert [p.event.kind for p in spike.precursors] == ["service.failed", "config.changed"]
    assert [p.lead_us for p in spike.precursors] == sorted(p.lead_us for p in spike.precursors)


def test_later_events_are_never_precursors(populated: Store) -> None:
    inc = incident(populated)
    failure = inc.findings[0]
    assert all(p.event.kind != "service.recovered" for p in failure.precursors)
    assert all(p.event.id < failure.failure.id or p.lead_us > 0 for p in failure.precursors)


def test_mtime_orders_a_change_noticed_in_the_same_poll(tmp_path: Path) -> None:
    """Both found in one poll (same timestamp); mtime shows the config came first."""
    with Store(tmp_path / "m.db", "h") as store:
        poll = at("2026-10-29T09:00:00Z")
        store.commit(
            [
                EventDraft("service.failed", "api", "error", payload={"status": "down"}),
                EventDraft(
                    "config.changed",
                    CONF,
                    "warning",
                    payload={"mtime_us": poll - 5_000_000, "diff": "d"},
                ),
            ],
            ts_us=poll,
        )
        inc = build_incident(
            store, around_us=None, window_us=600_000_000, after_us=0, generated_us=poll
        )
        (finding,) = inc.findings
        assert [p.event.kind for p in finding.precursors] == ["config.changed"]
        assert [e.kind for e in inc.events] == ["config.changed", "service.failed"]


def test_mtime_in_the_future_is_ignored(populated: Store) -> None:
    event = populated.events(kinds=["config.changed"])[0]
    forged = type(event)(**{**event.__dict__, "payload": {"mtime_us": event.ts_us + 10**9}})
    assert effective_ts_us(forged) == event.ts_us


def test_default_centre_is_the_latest_failure(populated: Store) -> None:
    inc = incident(populated, around=None)
    assert inc.around_us == at("2026-10-29T14:12:23Z")  # the cpu spike is the latest failure


def test_no_failures_is_an_explicit_error(tmp_path: Path) -> None:
    with Store(tmp_path / "e.db", "h") as store:
        store.commit([EventDraft("recorder.started", "recorder", "info")])
        with pytest.raises(NoIncidentError):
            incident(store, around=None)


def test_quiet_window_reports_no_failures(populated: Store) -> None:
    inc = incident(populated, around="2026-10-29T13:00:00Z")
    assert inc.findings == ()
    assert "No failures were recorded" in render_markdown(inc)


def test_failure_with_nothing_before_it(tmp_path: Path) -> None:
    with Store(tmp_path / "n.db", "h") as store:
        store.commit(
            [EventDraft("service.failed", "api", "error", payload={"status": "down"})],
            ts_us=at("2026-10-29T10:00:00Z"),
        )
        inc = build_incident(
            store,
            around_us=at("2026-10-29T10:00:00Z"),
            window_us=600_000_000,
            after_us=0,
            generated_us=at("2026-10-29T10:05:00Z"),
        )
        assert "nothing recorded before it" in render_markdown(inc)


def test_broken_chain_is_called_out_in_every_format(populated: Store) -> None:
    conn = sqlite3.connect(populated.path)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET subject = 'forged' WHERE id = 3")
    conn.commit()
    conn.close()
    inc = incident(populated)
    assert not inc.chain.ok
    assert "BROKEN" in render_markdown(inc)
    assert "BROKEN" in render_html(inc)
    assert to_dict(inc)["chain"]["ok"] is False


def test_html_escapes_untrusted_content(tmp_path: Path) -> None:
    evil = "<script>alert(1)</script>"
    with Store(tmp_path / "x.db", "h") as store:
        store.commit(
            [
                EventDraft(
                    "config.changed",
                    evil,
                    "warning",
                    payload={"diff": f"+{evil}", "lines_added": 1, "lines_removed": 0},
                ),
                EventDraft(
                    "service.failed",
                    evil,
                    "error",
                    payload={"status": "down", "detail": evil, "from": "up"},
                ),
            ],
            ts_us=at("2026-10-29T10:00:00Z"),
        )
        inc = build_incident(
            store,
            around_us=None,
            window_us=600_000_000,
            after_us=0,
            generated_us=at("2026-10-29T10:01:00Z"),
        )
    page = render_html(inc)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "<script" not in render_html(inc).replace("&lt;script", "")


def test_html_is_self_contained(populated: Store) -> None:
    page = render_html(incident(populated))
    assert page.startswith("<!doctype html>")
    for external in ("http://", "https://", "<link", "<script", "src="):
        assert external not in page
    assert "web-01" in page and "workers = 400" in page and "Blackbox by OBEOS" in page


def test_human_delta() -> None:
    assert [human_delta(us) for us in (0, 400_000, 12_000_000, 120_000_000, 7_200_000_000)] == [
        "<1s",
        "<1s",
        "12s",
        "2m",
        "2.0h",
    ]


def check_golden(name: str, actual: str) -> None:
    path = FIXTURES / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(actual, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == actual, (
        f"{name} differs from the committed golden file; "
        "if the change is intended, regenerate with UPDATE_GOLDEN=1 and review the diff"
    )


def test_golden_markdown(populated: Store) -> None:
    check_golden("incident_report.golden.md", render_markdown(incident(populated)))


def test_golden_json(populated: Store) -> None:
    check_golden(
        "incident_report.golden.json",
        json.dumps(to_dict(incident(populated)), indent=2, sort_keys=True) + "\n",
    )
