"""Parsing and formatting of times for the command line and reports (UTC)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class TimeParseError(ValueError):
    pass


def parse_duration_us(text: str) -> int:
    match = _DURATION.match(text.strip().lower())
    if not match:
        raise TimeParseError(f"invalid duration {text!r}; use forms like 90s, 30m, 2h, 7d")
    return int(float(match.group(1)) * _UNITS[match.group(2)] * 1_000_000)


def parse_time_us(text: str, *, now_us: int) -> int:
    """Accept ``now``, a duration ago (``2h``), an epoch number, or ISO-8601 (UTC if naive)."""
    value = text.strip()
    if value.lower() == "now":
        return now_us
    if _DURATION.match(value.lower()):
        return now_us - parse_duration_us(value)
    try:
        number = float(value)
    except ValueError:
        pass
    else:
        return int(number * 1_000_000)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeParseError(
            f"invalid time {text!r}; use ISO-8601 (2026-10-29T14:05:00Z), epoch seconds, "
            "a duration ago (2h), or 'now'"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000)


def iso(ts_us: int, *, precision: str = "seconds") -> str:
    moment = datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC)
    if precision == "milliseconds":
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
