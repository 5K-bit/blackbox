"""Acceptance scenario for Blackbox v0.1: "what changed before this broke?"

Runs a real service (``demo_service.py``), records it with the real collectors,
makes one harmless config edit and one fatal one, lets the service crash, and
builds the incident report. Nothing is mocked: the failure is a real process
exit seen through a real TCP probe.

    python examples/acceptance.py --out docs/evidence

The same function is exercised by tests/integration/test_acceptance.py.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from blackbox.collectors import ConfigFilesCollector, ServicesCollector, ServiceSpec
from blackbox.recorder import Recorder
from blackbox.redact import Redactor
from blackbox.report import (
    Incident,
    build_incident,
    describe,
    effective_ts_us,
    render_html,
    render_markdown,
    to_dict,
)
from blackbox.store import Store, now_us
from blackbox.timeparse import iso

HERE = Path(__file__).resolve().parent
SECRET_BEFORE = "s3cr3t-before-9f2a"  # noqa: S105 - deliberately fake, proves redaction
SECRET_AFTER = "s3cr3t-after-77be"  # noqa: S105 - deliberately fake, proves redaction


@dataclass
class Result:
    workdir: Path
    db_path: Path
    incident: Incident
    transcript: list[str]
    markdown: str
    html: str
    json_text: str
    # Raw bytes of the database and its -wal/-shm files captured while the recorder was still
    # running. The WAL disappears on a clean close, so this is the only way to inspect it.
    live_db_bytes: list[tuple[str, bytes]]


def run_scenario(workdir: Path, interval: float = 0.25, timeout: float = 15.0) -> Result:
    import json

    workdir.mkdir(parents=True, exist_ok=True)
    app, db, limits = workdir / "app.conf", workdir / "db.conf", workdir / "limits.conf"
    app.write_text(f"listen = auto\nworkers = 4\nmode = ok\napi_token = {SECRET_BEFORE}\n")
    db.write_text("pool_size = 10\ntimeout = 30\n")
    limits.write_text("max_open_files = 1024\n")
    port_file = workdir / "port"

    service = subprocess.Popen(
        [
            sys.executable,
            str(HERE / "demo_service.py"),
            "--config",
            str(app),
            "--port-file",
            str(port_file),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not port_file.exists():
            if time.monotonic() > deadline or service.poll() is not None:
                raise RuntimeError("demo service did not start")
            time.sleep(0.05)
        port = int(port_file.read_text())

        store = Store(workdir / "blackbox.db", machine_id="acceptance-host")
        collectors = [
            ConfigFilesCollector([str(app), str(db), str(limits)], Redactor()),
            ServicesCollector([ServiceSpec("demo-api", "tcp", port=port, timeout=1.0)]),
        ]
        transcript: list[str] = []

        def echo(events):  # type: ignore[no-untyped-def]
            for e in events:
                transcript.append(
                    f"{iso(effective_ts_us(e), precision='seconds')}  {e.severity:<8}  "
                    f"{e.kind:<18}  {e.subject}  {describe(e)}".rstrip()
                )

        recorder = Recorder(store, collectors, interval=interval, on_events=echo)
        recorder.start()
        for _ in range(3):  # baselines, then a quiet stretch
            recorder.tick()
            time.sleep(interval)

        # 1. a harmless edit to an unrelated file
        db.write_text("pool_size = 20\ntimeout = 30\n")
        recorder.tick()
        time.sleep(interval)
        # 2. the fatal edit: bad mode, plus a rotated secret and a scaled-up worker count
        app.write_text(f"listen = auto\nworkers = 400\nmode = broken\napi_token = {SECRET_AFTER}\n")

        # keep recording until the failure is observed
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            recorder.tick()
            if store.events(kinds=["service.failed"]):
                break
            time.sleep(interval)
        recorder.tick()
        recorder.stop()
        live_db_bytes = [(p.name, p.read_bytes()) for p in sorted(workdir.glob("blackbox.db*"))]

        incident = build_incident(
            store,
            around_us=None,
            window_us=30 * 60 * 1_000_000,
            after_us=5 * 60 * 1_000_000,
            generated_us=now_us(),
        )
        store.close()
    finally:
        if service.poll() is None:
            service.terminate()
        service.wait(timeout=10)
        if service.stderr:
            service.stderr.close()

    return Result(
        workdir=workdir,
        db_path=workdir / "blackbox.db",
        incident=incident,
        transcript=transcript,
        markdown=render_markdown(incident),
        html=render_html(incident),
        json_text=json.dumps(to_dict(incident), indent=2, sort_keys=True),
        live_db_bytes=live_db_bytes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("docs/evidence"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blackbox-acceptance-") as tmp:
        result = run_scenario(Path(tmp))
        (args.out / "acceptance-transcript.txt").write_text(
            "\n".join(result.transcript) + "\n", encoding="utf-8"
        )
        (args.out / "acceptance-incident.md").write_text(result.markdown, encoding="utf-8")
        (args.out / "acceptance-incident.html").write_text(result.html, encoding="utf-8")
        shutil.copyfile(result.db_path, args.out / "acceptance.db")
    print("\n".join(result.transcript))
    print(f"\nevidence written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
