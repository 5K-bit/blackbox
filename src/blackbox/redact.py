"""Secret redaction applied before configuration content is stored or reported.

Blackbox keeps copies of tracked configuration files so it can show diffs. Those
files often contain credentials, so every line passes through :class:`Redactor`
first. Redaction is best-effort pattern matching: it reduces the chance of
storing secrets, it does not guarantee it. Track files accordingly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = (
    r"pass(?:word|wd|phrase)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|auth(?:orization)?|client[_-]?secret|connection[_-]?string"
)

# key = value / key: value / "key": "value" / KEY=value
_ASSIGNMENT = re.compile(
    rf"""(?P<prefix>["']?[\w.\-]*(?:{_SENSITIVE_KEY})[\w.\-]*["']?\s*[:=]\s*)
         (?P<value>"[^"\n]*"|'[^'\n]*'|[^\s#;,]+)""",
    re.IGNORECASE | re.VERBOSE,
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


class Redactor:
    """Replace likely secrets in text with a fixed marker."""

    def __init__(self, extra_patterns: Iterable[str] = ()) -> None:
        self._extra = [re.compile(p) for p in extra_patterns]

    def redact(self, text: str) -> str:
        text = _PEM_BLOCK.sub(
            f"-----BEGIN PRIVATE KEY-----\n{REDACTED}\n-----END PRIVATE KEY-----", text
        )
        text = _URL_CREDENTIALS.sub(rf"\g<scheme>{REDACTED}@", text)
        text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
        text = _AWS_KEY.sub(REDACTED, text)
        text = _ASSIGNMENT.sub(self._assignment, text)
        for pattern in self._extra:
            text = pattern.sub(REDACTED, text)
        return text

    @staticmethod
    def _assignment(match: re.Match[str]) -> str:
        value = match.group("value")
        if value == REDACTED:
            return match.group(0)
        if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) >= 2:
            return f"{match.group('prefix')}{value[0]}{REDACTED}{value[0]}"
        return f"{match.group('prefix')}{REDACTED}"
