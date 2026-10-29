from __future__ import annotations

import pytest

from blackbox.redact import REDACTED, Redactor


@pytest.mark.parametrize(
    "line",
    [
        "password = hunter2",
        "DB_PASSWORD=hunter2",
        'api_key: "hunter2"',
        "api-key = 'hunter2'",
        '"client_secret": "hunter2",',
        "token=hunter2",
        "aws_access_key_id = hunter2",
        "private_key: hunter2",
        "connection_string = Server=db;Password=hunter2",
    ],
)
def test_sensitive_assignments_are_redacted(line: str) -> None:
    out = Redactor().redact(line)
    assert "hunter2" not in out
    assert REDACTED in out


def test_quotes_and_keys_are_preserved() -> None:
    assert Redactor().redact('password = "abc"') == f'password = "{REDACTED}"'
    assert Redactor().redact("token: abc") == f"token: {REDACTED}"


def test_ordinary_settings_are_untouched() -> None:
    text = "listen = 8080\nworkers = 4\nlog_level = info\nserver_name = example.com"
    assert Redactor().redact(text) == text


def test_url_credentials_bearer_and_aws_ids() -> None:
    text = (
        "url = postgres://admin:s3cr3t@db.local/app\n"
        "header = Bearer abcdefghijklmnop12345\n"
        "id = AKIAABCDEFGHIJKLMNOP"
    )
    out = Redactor().redact(text)
    assert "s3cr3t" not in out and "abcdefghijklmnop12345" not in out
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "postgres://" in out and "db.local" in out


def test_pem_private_key_block_is_removed() -> None:
    text = (
        "key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----\nafter"
    )
    out = Redactor().redact(text)
    assert "MIIEow" not in out
    assert out.endswith("after")


def test_extra_patterns_and_idempotence() -> None:
    redactor = Redactor([r"internal-\d+"])
    once = redactor.redact("host = internal-42 password = x")
    assert "internal-42" not in once
    assert redactor.redact(once) == once
