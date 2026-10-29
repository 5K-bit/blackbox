"""Loading and validating ``blackbox.toml`` (config as code)."""

from __future__ import annotations

import platform
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .collectors import (
    Collector,
    ConfigFilesCollector,
    ResourcesCollector,
    ServicesCollector,
    ServiceSpec,
)
from .redact import Redactor

DEFAULT_FILENAME = "blackbox.toml"
SERVICE_KINDS = ("systemd", "windows", "tcp", "command")

TEMPLATE = """\
# Blackbox configuration. Relative paths are relative to this file.

# "auto" uses this computer's hostname.
machine_id = "auto"
db_path = "blackbox.db"
interval_seconds = 10

[files]
# Configuration files to track. Content is redacted for likely secrets before storage.
paths = [
  # "/etc/nginx/nginx.conf",
  # "C:/ProgramData/MyApp/config.ini",
]
max_bytes = 1048576        # larger files are tracked by digest only

# One [[services]] table per service to watch. kind = systemd | windows | tcp | command
# [[services]]
# name = "nginx"
# kind = "systemd"
# unit = "nginx.service"
#
# [[services]]
# name = "api"
# kind = "tcp"
# host = "127.0.0.1"
# port = 8080
#
# [[services]]
# name = "worker"
# kind = "command"          # exit code 0 means healthy; run without a shell
# command = ["pgrep", "-f", "my-worker"]

[resources]
enabled = true
cpu_floor = 85             # percent; a spike must also exceed the recent baseline
mem_floor = 90
disk_floor = 90
disk_paths = ["/"]
window = 30                # baseline samples

[redaction]
extra_patterns = []        # extra regular expressions to redact
"""


class ConfigError(ValueError):
    """Raised for invalid configuration, with a message that names the problem."""


@dataclass(frozen=True)
class ResourceConfig:
    enabled: bool = True
    cpu_floor: float = 85.0
    mem_floor: float = 90.0
    disk_floor: float = 90.0
    disk_paths: tuple[str, ...] = ("/",)
    window: int = 30


@dataclass(frozen=True)
class Config:
    machine_id: str
    db_path: Path
    interval_seconds: float
    file_paths: tuple[str, ...] = ()
    max_file_bytes: int = 1_048_576
    services: tuple[ServiceSpec, ...] = ()
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    redaction_patterns: tuple[str, ...] = ()
    source: Path | None = None


def load_config(path: str | Path) -> Config:
    source = Path(path)
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {source} (run 'blackbox init')") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from exc
    return parse_config(raw, base_dir=source.resolve().parent, source=source)


def parse_config(raw: dict[str, Any], *, base_dir: Path, source: Path | None = None) -> Config:
    _only(
        raw,
        {
            "machine_id",
            "db_path",
            "interval_seconds",
            "files",
            "services",
            "resources",
            "redaction",
        },
        "top level",
    )
    machine = _get(raw, "machine_id", str, "auto", "machine_id")
    machine_id = (platform.node() or "unknown-host") if machine == "auto" else machine
    db_path = Path(_get(raw, "db_path", str, "blackbox.db", "db_path"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path
    interval = float(_get(raw, "interval_seconds", (int, float), 10, "interval_seconds"))
    if interval < 0.5:
        raise ConfigError("interval_seconds must be at least 0.5")

    files = raw.get("files", {})
    _only(files, {"paths", "max_bytes"}, "[files]")
    paths = _get(files, "paths", list, [], "files.paths")
    if not all(isinstance(p, str) and p for p in paths):
        raise ConfigError("files.paths must be a list of non-empty strings")
    resolved = tuple(str(p if Path(p).is_absolute() else base_dir / p) for p in paths)
    max_bytes = int(_get(files, "max_bytes", int, 1_048_576, "files.max_bytes"))
    if max_bytes < 1:
        raise ConfigError("files.max_bytes must be positive")

    services = tuple(_parse_service(i, entry) for i, entry in enumerate(raw.get("services", [])))
    names = [s.name for s in services]
    if len(set(names)) != len(names):
        raise ConfigError("service names must be unique")

    res = raw.get("resources", {})
    _only(
        res,
        {"enabled", "cpu_floor", "mem_floor", "disk_floor", "disk_paths", "window"},
        "[resources]",
    )
    resources = ResourceConfig(
        enabled=bool(_get(res, "enabled", bool, True, "resources.enabled")),
        cpu_floor=float(_get(res, "cpu_floor", (int, float), 85, "resources.cpu_floor")),
        mem_floor=float(_get(res, "mem_floor", (int, float), 90, "resources.mem_floor")),
        disk_floor=float(_get(res, "disk_floor", (int, float), 90, "resources.disk_floor")),
        disk_paths=tuple(_get(res, "disk_paths", list, ["/"], "resources.disk_paths")),
        window=int(_get(res, "window", int, 30, "resources.window")),
    )
    if resources.window < 5:
        raise ConfigError("resources.window must be at least 5")

    red = raw.get("redaction", {})
    _only(red, {"extra_patterns"}, "[redaction]")
    patterns = tuple(_get(red, "extra_patterns", list, [], "redaction.extra_patterns"))
    Redactor(patterns)  # compile now so a bad pattern fails at load time, not mid-recording

    return Config(
        machine_id=machine_id,
        db_path=db_path,
        interval_seconds=interval,
        file_paths=resolved,
        max_file_bytes=max_bytes,
        services=services,
        resources=resources,
        redaction_patterns=patterns,
        source=source,
    )


def build_collectors(config: Config) -> list[Collector]:
    collectors: list[Collector] = []
    if config.file_paths:
        collectors.append(
            ConfigFilesCollector(
                config.file_paths,
                Redactor(config.redaction_patterns),
                max_bytes=config.max_file_bytes,
            )
        )
    if config.services:
        collectors.append(ServicesCollector(config.services))
    if config.resources.enabled:
        r = config.resources
        collectors.append(
            ResourcesCollector(
                cpu_floor=r.cpu_floor,
                mem_floor=r.mem_floor,
                disk_floor=r.disk_floor,
                disk_paths=r.disk_paths,
                window=r.window,
            )
        )
    return collectors


# -- helpers --------------------------------------------------------------------


def _only(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _get(
    table: dict[str, Any], key: str, kind: type | tuple[type, ...], default: Any, label: str
) -> Any:
    if key not in table:
        return default
    value = table[key]
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(value, bool) and bool not in kinds:  # bool is an int subclass; reject it
        raise ConfigError(f"{label} has the wrong type (got bool)")
    if not isinstance(value, kinds):
        raise ConfigError(f"{label} has the wrong type (got {type(value).__name__})")
    return value


def _parse_service(index: int, entry: dict[str, Any]) -> ServiceSpec:
    where = f"services[{index}]"
    _only(entry, {"name", "kind", "unit", "host", "port", "command", "timeout"}, where)
    name = _get(entry, "name", str, "", f"{where}.name")
    kind = _get(entry, "kind", str, "", f"{where}.kind")
    if not name:
        raise ConfigError(f"{where}.name is required")
    if kind not in SERVICE_KINDS:
        raise ConfigError(f"{where}.kind must be one of: {', '.join(SERVICE_KINDS)}")
    command = tuple(_get(entry, "command", list, [], f"{where}.command"))
    port = entry.get("port")
    if kind == "tcp" and not isinstance(port, int):
        raise ConfigError(f"{where}.port is required (integer) for kind = 'tcp'")
    if kind == "command" and (not command or not all(isinstance(c, str) for c in command)):
        raise ConfigError(f"{where}.command must be a non-empty list of strings")
    return ServiceSpec(
        name=name,
        kind=kind,
        unit=_get(entry, "unit", str, None, f"{where}.unit"),
        host=_get(entry, "host", str, "127.0.0.1", f"{where}.host"),
        port=port if isinstance(port, int) else None,
        command=command,
        timeout=float(_get(entry, "timeout", (int, float), 3, f"{where}.timeout")),
    )
