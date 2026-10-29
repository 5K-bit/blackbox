"""Measure Blackbox's own cost while it records a quiet machine.

    python examples/overhead.py --seconds 30 --interval 1

This is a short smoke measurement, not a 24-hour soak test: it reports what
the recorder itself consumed (CPU time of this process, resident memory, database
growth) for the duration it ran, with three tracked files, one TCP probe and the
resource sampler. Polling at 1 s is ten times more frequent than the default.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import platform
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from blackbox.collectors import (
    ConfigFilesCollector,
    ResourcesCollector,
    ServicesCollector,
    ServiceSpec,
)
from blackbox.recorder import Recorder
from blackbox.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    port = server.getsockname()[1]
    stop_server = threading.Event()

    def accept_loop() -> None:
        server.settimeout(0.2)
        while not stop_server.is_set():
            with contextlib.suppress(OSError):
                server.accept()[0].close()

    threading.Thread(target=accept_loop, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="blackbox-overhead-") as tmp:
        root = Path(tmp)
        files = []
        for name in ("a.conf", "b.conf", "c.conf"):
            (root / name).write_text("setting = 1\n" * 50)
            files.append(str(root / name))
        store = Store(root / "bb.db", "overhead-host")
        recorder = Recorder(
            store,
            [
                ConfigFilesCollector(files),
                ServicesCollector([ServiceSpec("probe", "tcp", port=port, timeout=1.0)]),
                ResourcesCollector(disk_paths=(str(root),)),
            ],
            interval=args.interval,
        )
        stop_flag = threading.Event()
        stopper = threading.Timer(args.seconds, stop_flag.set)
        cpu0, wall0 = time.process_time(), time.perf_counter()
        stopper.start()
        ticks = recorder.run(stop_flag)
        cpu, wall = time.process_time() - cpu0, time.perf_counter() - wall0
        events = store.count_events()
        db_bytes = sum(p.stat().st_size for p in root.glob("bb.db*"))
        store.close()
    stop_server.set()

    rss = ""
    try:
        psutil = importlib.import_module("psutil")
        rss = f"{psutil.Process(os.getpid()).memory_info().rss / 1_048_576:.1f} MiB"
    except ImportError:
        rss = "n/a (psutil not installed)"

    lines = [
        f"platform         {platform.platform()} / Python {platform.python_version()}",
        f"duration         {wall:.1f} s at {args.interval:g} s polling ({ticks} collection passes)",
        "workload         3 tracked files, 1 TCP probe, CPU/memory/disk sampling",
        f"recorder CPU     {cpu:.3f} s of CPU time = {100 * cpu / wall:.2f}% of one core",
        f"resident memory  {rss} (whole process, includes the interpreter)",
        f"events written   {events}",
        f"database size    {db_bytes / 1024:.0f} KiB including WAL",
        "note             short smoke measurement, not a 24-hour soak test",
        "note             CPU time includes the benchmark's own listener thread, so it",
        "                 slightly overstates the recorder",
    ]
    print("\n".join(lines))
    if args.out:
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
