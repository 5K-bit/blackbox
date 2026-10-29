"""The recorder: runs collectors on a schedule and commits what they see."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

from . import __version__
from .collectors.base import Collector, Observation
from .model import (
    KIND_RECORDER_GAP,
    KIND_RECORDER_NOTE,
    KIND_RECORDER_STARTED,
    KIND_RECORDER_STOPPED,
    Event,
    EventDraft,
)
from .store import Store, now_us


class Recorder:
    def __init__(
        self,
        store: Store,
        collectors: Sequence[Collector],
        *,
        interval: float = 10.0,
        clock: Callable[[], int] = now_us,
        on_events: Callable[[list[Event]], None] | None = None,
    ) -> None:
        self.store = store
        self._on_events = on_events
        self.collectors = list(collectors)
        self.interval = interval
        self._clock = clock
        self._failing: set[str] = set()

    def start(self) -> list[Event]:
        """Record a start marker, noting a gap if the last run never stopped cleanly."""
        drafts: list[EventDraft] = []
        latest = self.store.events(kind_prefix="recorder.", limit=1, newest_first=True)
        if latest and latest[0].kind == KIND_RECORDER_STARTED:
            drafts.append(
                EventDraft(
                    KIND_RECORDER_GAP,
                    "recorder",
                    "warning",
                    payload={
                        "previous_start_us": latest[0].ts_us,
                        "note": "previous run ended without a clean stop; changes during the gap "
                        "are detected on the first tick and dated by detection time and file mtime",
                    },
                )
            )
        drafts.append(
            EventDraft(
                KIND_RECORDER_STARTED,
                "recorder",
                "info",
                payload={
                    "version": __version__,
                    "collectors": [c.name for c in self.collectors],
                    "interval_seconds": self.interval,
                },
            )
        )
        return self._publish(self.store.commit(drafts, ts_us=self._clock()))

    def _publish(self, events: list[Event]) -> list[Event]:
        if self._on_events is not None and events:
            self._on_events(events)
        return events

    def stop(self, reason: str = "requested") -> list[Event]:
        return self._publish(
            self.store.commit(
                [EventDraft(KIND_RECORDER_STOPPED, "recorder", "info", payload={"reason": reason})],
                ts_us=self._clock(),
            )
        )

    def tick(self) -> list[Event]:
        """Run every collector once and commit the combined result atomically."""
        stamp = self._clock()
        combined = Observation()
        for collector in self.collectors:
            try:
                combined.extend(collector.collect(self.store, stamp))
            except Exception as exc:  # collectors must never take the recorder down
                if collector.name not in self._failing:
                    self._failing.add(collector.name)
                    combined.drafts.append(
                        EventDraft(
                            KIND_RECORDER_NOTE,
                            collector.name,
                            "warning",
                            payload={"error": f"{type(exc).__name__}: {exc}"},
                        )
                    )
            else:
                self._failing.discard(collector.name)
        self.store.meta_set("last_tick_us", str(stamp))
        return self._publish(
            self.store.commit(
                combined.drafts, state=combined.state, versions=combined.versions, ts_us=stamp
            )
        )

    def run(self, stop: threading.Event | None = None, max_ticks: int | None = None) -> int:
        """Tick until ``stop`` is set (or ``max_ticks`` is reached). Returns ticks run."""
        stop = stop or threading.Event()
        ticks = 0
        self.start()
        try:
            while not stop.is_set():
                self.tick()
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                stop.wait(self.interval)
        finally:
            self.stop("signal" if stop.is_set() else "completed")
        return ticks
