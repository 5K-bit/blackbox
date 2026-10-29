from __future__ import annotations

import pytest

from blackbox.model import SEVERITIES, Event, EventDraft


def test_unknown_severity_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown severity"):
        EventDraft("a.b", "x", "catastrophic")


def test_severities_are_ordered_and_event_helpers_work() -> None:
    assert SEVERITIES == ("info", "warning", "error", "critical")
    event = Event(1, 2_500_000, "h", "a.b", "x", "error", None, None, {}, "p", "h")
    assert event.ts == 2.5 and event.rank == 2
