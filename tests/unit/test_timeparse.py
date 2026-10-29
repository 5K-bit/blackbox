from __future__ import annotations

import pytest

from blackbox.timeparse import TimeParseError, iso, parse_duration_us, parse_time_us

NOW = 1_800_000_000_000_000


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("30m", 1800), ("2h", 7200), ("1d", 86400), ("1w", 604800), ("1.5h", 5400)],
)
def test_durations(text: str, seconds: int) -> None:
    assert parse_duration_us(text) == seconds * 1_000_000


def test_times() -> None:
    assert parse_time_us("now", now_us=NOW) == NOW
    assert parse_time_us("2h", now_us=NOW) == NOW - 7200 * 1_000_000
    assert parse_time_us("1800000000", now_us=NOW) == NOW
    assert parse_time_us("2026-10-29T14:00:00Z", now_us=NOW) == parse_time_us(
        "2026-10-29T14:00:00+00:00", now_us=NOW
    )
    assert parse_time_us("2026-10-29T14:00:00", now_us=NOW) == parse_time_us(
        "2026-10-29T14:00:00Z", now_us=NOW
    )
    assert parse_time_us("2026-10-29T10:00:00-04:00", now_us=NOW) == parse_time_us(
        "2026-10-29T14:00:00Z", now_us=NOW
    )


@pytest.mark.parametrize("bad", ["yesterday", "2x", "", "12:00"])
def test_bad_input_is_rejected(bad: str) -> None:
    with pytest.raises(TimeParseError):
        parse_time_us(bad, now_us=NOW)
    with pytest.raises(TimeParseError):
        parse_duration_us(bad)


def test_iso_formatting() -> None:
    assert iso(1_800_000_000_000_000) == "2027-01-15T08:00:00Z"
    assert iso(1_800_000_000_123_456, precision="milliseconds") == "2027-01-15T08:00:00.123Z"
