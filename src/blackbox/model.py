"""Core data types shared by collectors, the store and the report builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITIES = ("info", "warning", "error", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

# Event kinds. Kept as plain strings so the database stays readable with any
# SQLite tool.
KIND_RECORDER_STARTED = "recorder.started"
KIND_RECORDER_STOPPED = "recorder.stopped"
KIND_RECORDER_GAP = "recorder.gap"
KIND_RECORDER_NOTE = "recorder.note"
KIND_CONFIG_BASELINE = "config.baseline"
KIND_CONFIG_CHANGED = "config.changed"
KIND_CONFIG_DELETED = "config.deleted"
KIND_CONFIG_CREATED = "config.created"
KIND_CONFIG_UNREADABLE = "config.unreadable"
KIND_SERVICE_BASELINE = "service.baseline"
KIND_SERVICE_FAILED = "service.failed"
KIND_SERVICE_RECOVERED = "service.recovered"
KIND_SERVICE_UNKNOWN = "service.unknown"
KIND_RESOURCE_SPIKE = "resource.spike"
KIND_RESOURCE_RECOVERED = "resource.recovered"


@dataclass(frozen=True)
class EventDraft:
    """An event produced by a collector, before it is timestamped and chained."""

    kind: str
    subject: str
    severity: str = "info"
    before_digest: str | None = None
    after_digest: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity: {self.severity!r}")


@dataclass(frozen=True)
class Event:
    """A stored event. ``ts_us`` is microseconds since the Unix epoch (UTC)."""

    id: int
    ts_us: int
    machine_id: str
    kind: str
    subject: str
    severity: str
    before_digest: str | None
    after_digest: str | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    @property
    def ts(self) -> float:
        return self.ts_us / 1_000_000

    @property
    def rank(self) -> int:
        return SEVERITY_RANK[self.severity]
