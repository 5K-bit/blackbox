from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from blackbox.store import Store


def at(text: str) -> int:
    """ISO-8601 UTC string -> microseconds since epoch."""
    return int(
        datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp() * 1e6
    )


class FakeClock:
    def __init__(self, start_us: int = 1_700_000_000_000_000) -> None:
        self.now = start_us

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: float) -> int:
        self.now += int(seconds * 1_000_000)
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Iterator[Store]:
    with Store(tmp_path / "bb.db", "test-host", clock=clock) as s:
        yield s
