from __future__ import annotations

import threading

from blackbox.collectors.base import Observation
from blackbox.model import EventDraft
from blackbox.recorder import Recorder
from blackbox.store import Store


class Boom:
    name = "boom"

    def __init__(self) -> None:
        self.calls = 0

    def collect(self, store: Store, now_us: int) -> Observation:
        self.calls += 1
        if self.calls <= 3:
            raise RuntimeError("probe exploded")
        return Observation(drafts=[EventDraft("recorder.note", "boom", "info")])


class Fixed:
    name = "fixed"

    def collect(self, store: Store, now_us: int) -> Observation:
        return Observation(
            drafts=[EventDraft("service.baseline", "x", "info")], state={"service:x": "up"}
        )


def test_run_brackets_ticks_with_start_and_stop(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder(store, [Fixed()], interval=0, clock=clock)
    assert rec.run(max_ticks=2) == 2
    kinds = [e.kind for e in store.events()]
    assert kinds == [
        "recorder.started",
        "service.baseline",  # Fixed emits one event per tick; two ticks ran
        "service.baseline",
        "recorder.stopped",
    ]
    assert store.events(kinds=["recorder.stopped"])[0].payload["reason"] == "completed"


def test_unclean_shutdown_is_flagged_as_a_gap(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    rec = Recorder(store, [Fixed()], interval=0, clock=clock)
    rec.start()  # ... and the machine loses power: no stop
    clock.advance(600)
    rec2 = Recorder(store, [Fixed()], interval=0, clock=clock)
    rec2.start()
    kinds = [e.kind for e in store.events()]
    assert kinds == ["recorder.started", "recorder.gap", "recorder.started"]
    gap = store.events(kinds=["recorder.gap"])[0]
    assert gap.severity == "warning" and "previous_start_us" in gap.payload


def test_clean_restart_has_no_gap(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    Recorder(store, [Fixed()], interval=0, clock=clock).run(max_ticks=1)
    Recorder(store, [Fixed()], interval=0, clock=clock).run(max_ticks=1)
    assert store.events(kinds=["recorder.gap"]) == []


def test_a_failing_collector_is_reported_once_and_does_not_stop_others(  # type: ignore[no-untyped-def]
    store: Store, clock
) -> None:
    boom = Boom()
    rec = Recorder(store, [boom, Fixed()], interval=0, clock=clock)
    for _ in range(5):
        rec.tick()
    notes = [e for e in store.events(kinds=["recorder.note"]) if "error" in e.payload]
    assert len(notes) == 1  # three failures, one note
    assert "RuntimeError: probe exploded" in notes[0].payload["error"]
    assert boom.calls == 5
    assert store.state_get("service:x") == "up"  # the healthy collector kept working


def test_stop_event_ends_the_loop_with_signal_reason(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    stop = threading.Event()
    stop.set()
    rec = Recorder(store, [Fixed()], interval=0, clock=clock)
    assert rec.run(stop) == 0
    assert store.events(kinds=["recorder.stopped"])[0].payload["reason"] == "signal"


def test_tick_updates_last_tick_marker(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    Recorder(store, [Fixed()], interval=0, clock=clock).tick()
    assert store.meta_get("last_tick_us") == str(clock())
