"""A tiny but real service for the Blackbox acceptance scenario.

It listens on a TCP port and re-reads its config file every 100 ms, the way many
daemons handle a reload. If the config says ``mode = broken`` it logs the problem
and exits with status 2, so the failure is a real process exit that Blackbox
observes through a real TCP probe, not a simulated event.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path


def config_is_valid(path: Path) -> bool:
    try:
        return "mode = broken" not in path.read_text(encoding="utf-8")
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port-file", required=True, type=Path, help="where to publish the port")
    args = parser.parse_args()

    if not config_is_valid(args.config):
        print("demo-api: invalid configuration at startup", file=sys.stderr)
        return 2
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.1)
    tmp = args.port_file.with_suffix(".tmp")
    tmp.write_text(str(server.getsockname()[1]), encoding="ascii")
    os.replace(tmp, args.port_file)

    while True:
        try:
            conn, _ = server.accept()
            conn.close()
        except TimeoutError:
            pass
        if not config_is_valid(args.config):
            print("demo-api: configuration reload failed: mode = broken", file=sys.stderr)
            server.close()
            return 2
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
