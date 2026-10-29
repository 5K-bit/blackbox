from __future__ import annotations

import socket
from collections.abc import Sequence

from blackbox.collectors.services import (
    DOWN,
    UNKNOWN,
    UP,
    ServicesCollector,
    ServiceSpec,
    default_runner,
    probe,
)
from blackbox.store import Store


def runner_returning(code: int, out: str = ""):  # type: ignore[no-untyped-def]
    def run(command: Sequence[str], timeout: float) -> tuple[int, str]:
        return code, out

    return run


SYSTEMD_ACTIVE = "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n"
SYSTEMD_FAILED = "LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\n"
SYSTEMD_MISSING = "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n"


def test_systemd_states() -> None:
    spec = ServiceSpec("nginx", "systemd", unit="nginx.service")
    assert probe(spec, runner_returning(0, SYSTEMD_ACTIVE)).status == UP
    failed = probe(spec, runner_returning(0, SYSTEMD_FAILED))
    assert failed.status == DOWN and "exit-code" in failed.detail
    assert probe(spec, runner_returning(0, SYSTEMD_MISSING)).status == UNKNOWN
    assert probe(spec, runner_returning(127)).status == UNKNOWN
    assert probe(spec, runner_returning(124)).status == UNKNOWN
    activating = "LoadState=loaded\nActiveState=activating\nSubState=start\nResult=success\n"
    assert probe(spec, runner_returning(0, activating)).status == UNKNOWN


def test_windows_states() -> None:
    spec = ServiceSpec("spooler", "windows", unit="Spooler")
    running = "SERVICE_NAME: Spooler\n        STATE              : 4  RUNNING\n"
    stopped = "SERVICE_NAME: Spooler\n        STATE              : 1  STOPPED\n"
    assert probe(spec, runner_returning(0, running)).status == UP
    assert probe(spec, runner_returning(0, stopped)).status == DOWN
    assert probe(spec, runner_returning(1060)).status == UNKNOWN
    assert probe(spec, runner_returning(127)).status == UNKNOWN


def test_command_probe() -> None:
    spec = ServiceSpec("w", "command", command=("true",))
    assert probe(spec, runner_returning(0)).status == UP
    assert probe(spec, runner_returning(1)).status == DOWN
    assert probe(spec, runner_returning(124)).status == DOWN
    assert probe(spec, runner_returning(127)).status == UNKNOWN
    assert probe(ServiceSpec("w", "command"), runner_returning(0)).status == UNKNOWN


def test_default_runner_runs_without_a_shell() -> None:
    import sys

    code, out = default_runner([sys.executable, "-c", "print('hi')"], 10)
    assert code == 0 and out.strip() == "hi"
    assert default_runner([sys.executable, "-c", "raise SystemExit(3)"], 10)[0] == 3
    assert default_runner(["definitely-not-a-real-binary-xyz"], 5)[0] == 127
    assert default_runner([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)[0] == 124
    # shell metacharacters are inert arguments, not syntax
    code, out = default_runner([sys.executable, "-c", "import sys;print(sys.argv[1])", "a; b"], 10)
    assert out.strip() == "a; b"


def test_tcp_probe_against_a_real_socket() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    spec = ServiceSpec("api", "tcp", host="127.0.0.1", port=port, timeout=1)
    assert probe(spec).status == UP
    server.close()
    result = probe(spec)
    assert result.status == DOWN
    assert probe(ServiceSpec("api", "tcp")).status == UNKNOWN
    assert probe(ServiceSpec("x", "bogus")).status == UNKNOWN


def test_tcp_unresolvable_host_is_unknown_not_down() -> None:
    spec = ServiceSpec("api", "tcp", host="no-such-host.invalid", port=80, timeout=1)
    assert probe(spec).status == UNKNOWN


class Sequenced:
    """Runner that returns scripted exit codes, one per call."""

    def __init__(self, codes: list[int]) -> None:
        self.codes = codes

    def __call__(self, command: Sequence[str], timeout: float) -> tuple[int, str]:
        return self.codes.pop(0), ""


def test_transitions_become_events(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    spec = ServiceSpec("worker", "command", command=("x",))
    collector = ServicesCollector([spec], Sequenced([0, 0, 1, 1, 0, 127, 0]))
    kinds = []
    for _ in range(7):
        clock.advance(10)
        obs = collector.collect(store, clock())
        events = store.commit(obs.drafts, state=obs.state, ts_us=clock())
        kinds.append([(e.kind, e.severity) for e in events])
    assert kinds == [
        [("service.baseline", "info")],
        [],
        [("service.failed", "error")],
        [],  # still down: no repeat alarm
        [("service.recovered", "info")],
        [("service.unknown", "warning")],
        [("service.recovered", "info")],
    ]


def test_failure_event_carries_previous_state_and_detail(store: Store, clock) -> None:  # type: ignore[no-untyped-def]
    collector = ServicesCollector(
        [ServiceSpec("worker", "command", command=("x",))], Sequenced([0, 1])
    )
    for _ in range(2):
        obs = collector.collect(store, clock())
        events = store.commit(obs.drafts, state=obs.state, ts_us=clock.advance(10))
    assert events[0].payload == {
        "kind": "command",
        "status": "down",
        "detail": "exit 1",
        "from": "up",
    }
