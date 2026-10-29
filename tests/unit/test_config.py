from __future__ import annotations

from pathlib import Path

import pytest

from blackbox.config import TEMPLATE, ConfigError, build_collectors, load_config, parse_config


def parse(raw: dict, tmp_path: Path):  # type: ignore[no-untyped-def,type-arg]
    return parse_config(raw, base_dir=tmp_path)


def test_template_is_valid_and_builds_resource_collector_only(tmp_path: Path) -> None:
    path = tmp_path / "blackbox.toml"
    path.write_text(TEMPLATE)
    config = load_config(path)
    assert config.db_path == tmp_path / "blackbox.db"
    assert [c.name for c in build_collectors(config)] == ["resources"]


def test_relative_paths_resolve_against_the_config_file(tmp_path: Path) -> None:
    config = parse({"db_path": "data/x.db", "files": {"paths": ["app.conf"]}}, tmp_path)
    assert config.db_path == tmp_path / "data" / "x.db"
    assert config.file_paths == (str(tmp_path / "app.conf"),)


def test_full_config(tmp_path: Path) -> None:
    config = parse(
        {
            "machine_id": "web-01",
            "interval_seconds": 5,
            "files": {"paths": ["/etc/a"], "max_bytes": 10},
            "services": [
                {"name": "n", "kind": "systemd", "unit": "n.service"},
                {"name": "t", "kind": "tcp", "port": 80},
                {"name": "c", "kind": "command", "command": ["true"]},
            ],
            "resources": {"cpu_floor": 70, "window": 10, "disk_paths": ["/", "/data"]},
            "redaction": {"extra_patterns": ["x+"]},
        },
        tmp_path,
    )
    assert config.machine_id == "web-01" and config.interval_seconds == 5
    assert [s.kind for s in config.services] == ["systemd", "tcp", "command"]
    assert config.resources.disk_paths == ("/", "/data")
    assert [c.name for c in build_collectors(config)] == ["config_files", "services", "resources"]


def test_auto_machine_id_uses_hostname(tmp_path: Path) -> None:
    import platform

    assert parse({}, tmp_path).machine_id == (platform.node() or "unknown-host")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"typo_key": 1}, "unknown key"),
        ({"files": {"pathz": []}}, "unknown key"),
        ({"interval_seconds": 0.1}, "at least 0.5"),
        ({"interval_seconds": "fast"}, "wrong type"),
        ({"interval_seconds": True}, "wrong type"),
        ({"files": {"paths": [1]}}, "non-empty strings"),
        ({"files": {"max_bytes": 0}}, "positive"),
        ({"services": [{"kind": "tcp", "port": 1}]}, "name is required"),
        ({"services": [{"name": "a", "kind": "ftp"}]}, "kind must be one of"),
        ({"services": [{"name": "a", "kind": "tcp"}]}, "port is required"),
        ({"services": [{"name": "a", "kind": "command"}]}, "non-empty list"),
        (
            {
                "services": [
                    {"name": "a", "kind": "tcp", "port": 1},
                    {"name": "a", "kind": "tcp", "port": 2},
                ]
            },
            "unique",
        ),
        ({"resources": {"window": 2}}, "at least 5"),
    ],
)
def test_invalid_configs_are_rejected_with_a_clear_message(
    raw: dict,
    message: str,
    tmp_path: Path,  # type: ignore[type-arg]
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse(raw, tmp_path)


def test_bad_regex_fails_at_load_time(tmp_path: Path) -> None:
    import re

    with pytest.raises(re.error):
        parse({"redaction": {"extra_patterns": ["("]}}, tmp_path)


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(bad)


def test_shipped_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[2] / "examples" / "blackbox.example.toml"
    config = load_config(example)
    assert [s.name for s in config.services] == ["nginx", "myapp-api", "backup-job"]
    assert [c.name for c in build_collectors(config)] == ["config_files", "services", "resources"]
