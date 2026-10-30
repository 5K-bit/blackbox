"""Watch service health and record state transitions.

Four probe kinds, all behind one small interface so platform differences stay
out of the rest of the code:

* ``systemd``  - ``systemctl show <unit>`` (Linux)
* ``windows``  - ``sc query <unit>`` (Windows)
* ``tcp``      - can a TCP connection be opened to host:port (any platform)
* ``command``  - run an argument list; exit code 0 means up (any platform)

Commands come from the operator's own ``blackbox.toml`` and are executed
without a shell.
"""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..model import (
    KIND_SERVICE_BASELINE,
    KIND_SERVICE_FAILED,
    KIND_SERVICE_RECOVERED,
    KIND_SERVICE_UNKNOWN,
    EventDraft,
)
from ..store import Store
from .base import Observation

UP = "up"
DOWN = "down"
UNKNOWN = "unknown"

Runner = Callable[[Sequence[str], float], tuple[int, str]]


def default_runner(command: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run ``command`` without a shell. Returns (returncode, stdout).

    127 means the executable was not found; 124 means the command timed out.
    """
    try:
        done = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,  # a probe must never wait on, or read from, the terminal
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""
    return done.returncode, done.stdout


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    kind: str
    unit: str | None = None
    host: str = "127.0.0.1"
    port: int | None = None
    command: tuple[str, ...] = ()
    timeout: float = 3.0


@dataclass(frozen=True)
class ProbeResult:
    status: str
    detail: str


def probe(spec: ServiceSpec, runner: Runner = default_runner) -> ProbeResult:
    if spec.kind == "systemd":
        return _probe_systemd(spec, runner)
    if spec.kind == "windows":
        return _probe_windows(spec, runner)
    if spec.kind == "tcp":
        return _probe_tcp(spec)
    if spec.kind == "command":
        return _probe_command(spec, runner)
    return ProbeResult(UNKNOWN, f"unsupported probe kind {spec.kind!r}")


def _probe_systemd(spec: ServiceSpec, runner: Runner) -> ProbeResult:
    code, out = runner(
        [
            "systemctl",
            "show",
            spec.unit or spec.name,
            "--property=LoadState,ActiveState,SubState,Result",
            "--no-pager",
        ],
        spec.timeout,
    )
    if code == 127:
        return ProbeResult(UNKNOWN, "systemctl not available")
    if code == 124:
        return ProbeResult(UNKNOWN, "systemctl timed out")
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if props.get("LoadState") == "not-found":
        return ProbeResult(UNKNOWN, "unit not found")
    state = props.get("ActiveState", "")
    detail = f"{state}/{props.get('SubState', '?')} result={props.get('Result', '?')}"
    if state in ("active", "reloading"):
        return ProbeResult(UP, detail)
    if state in ("inactive", "failed", "deactivating"):
        return ProbeResult(DOWN, detail)
    return ProbeResult(UNKNOWN, detail or f"systemctl exit {code}")


def _probe_windows(spec: ServiceSpec, runner: Runner) -> ProbeResult:
    code, out = runner(["sc", "query", spec.unit or spec.name], spec.timeout)
    if code == 127:
        return ProbeResult(UNKNOWN, "sc not available")
    if code == 124:
        return ProbeResult(UNKNOWN, "sc timed out")
    state_line = next((ln.strip() for ln in out.splitlines() if "STATE" in ln), "")
    if "RUNNING" in state_line:
        return ProbeResult(UP, state_line)
    if "STOPPED" in state_line or "STOP_PENDING" in state_line:
        return ProbeResult(DOWN, state_line)
    if code == 1060:
        return ProbeResult(UNKNOWN, "service does not exist")
    return ProbeResult(UNKNOWN, state_line or f"sc exit {code}")


def _probe_tcp(spec: ServiceSpec) -> ProbeResult:
    if spec.port is None:
        return ProbeResult(UNKNOWN, "no port configured")
    try:
        with socket.create_connection((spec.host, spec.port), timeout=spec.timeout):
            return ProbeResult(UP, f"connected to {spec.host}:{spec.port}")
    except socket.gaierror as exc:
        return ProbeResult(UNKNOWN, f"cannot resolve {spec.host}: {exc.strerror or exc}")
    except TimeoutError:
        return ProbeResult(DOWN, f"timed out connecting to {spec.host}:{spec.port}")
    except OSError as exc:
        return ProbeResult(DOWN, f"{spec.host}:{spec.port}: {exc.strerror or type(exc).__name__}")


def _probe_command(spec: ServiceSpec, runner: Runner) -> ProbeResult:
    if not spec.command:
        return ProbeResult(UNKNOWN, "no command configured")
    code, _ = runner(spec.command, spec.timeout)
    if code == 0:
        return ProbeResult(UP, "exit 0")
    if code == 127:
        return ProbeResult(UNKNOWN, "command not found")
    if code == 124:
        return ProbeResult(DOWN, "timed out")
    return ProbeResult(DOWN, f"exit {code}")


class ServicesCollector:
    name = "services"

    def __init__(self, specs: Sequence[ServiceSpec], runner: Runner = default_runner) -> None:
        self.specs = list(specs)
        self.runner = runner

    def collect(self, store: Store, now_us: int) -> Observation:
        obs = Observation()
        for spec in self.specs:
            result = probe(spec, self.runner)
            key = f"service:{spec.name}"
            previous = store.state_get(key)
            payload = {"kind": spec.kind, "status": result.status, "detail": result.detail}
            if previous is None:
                obs.drafts.append(
                    EventDraft(KIND_SERVICE_BASELINE, spec.name, "info", payload=payload)
                )
            elif previous != result.status:
                payload["from"] = previous
                if result.status == DOWN:
                    kind, severity = KIND_SERVICE_FAILED, "error"
                elif result.status == UP:
                    kind, severity = KIND_SERVICE_RECOVERED, "info"
                else:
                    kind, severity = KIND_SERVICE_UNKNOWN, "warning"
                obs.drafts.append(EventDraft(kind, spec.name, severity, payload=payload))
            obs.state[key] = result.status
        return obs
