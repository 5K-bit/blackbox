"""Collector interface.

A collector inspects one aspect of the machine and returns what it observed.
Collectors never write to the database themselves; the recorder commits all
observations from one tick atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..model import EventDraft
from ..store import FileVersion, Store


@dataclass
class Observation:
    drafts: list[EventDraft] = field(default_factory=list)
    state: dict[str, str | None] = field(default_factory=dict)
    versions: list[FileVersion] = field(default_factory=list)

    def extend(self, other: Observation) -> None:
        self.drafts.extend(other.drafts)
        self.state.update(other.state)
        self.versions.extend(other.versions)


class Collector(Protocol):
    name: str

    def collect(self, store: Store, now_us: int) -> Observation:
        """Inspect the machine and describe what changed since the last tick."""
        ...
