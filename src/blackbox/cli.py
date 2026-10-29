"""Command-line interface: a thin layer over the library."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from . import __version__
from .config import DEFAULT_FILENAME, TEMPLATE, Config, ConfigError, build_collectors, load_config
from .model import SEVERITIES, Event
from .recorder import Recorder
from .report import (
    NoIncidentError,
    build_incident,
    describe,
    effective_ts_us,
    render_html,
    render_markdown,
    to_dict,
)
from .store import Store, StoreError, now_us
from .timeparse import TimeParseError, iso, parse_duration_us, parse_time_us

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTEGRITY = 2
EXIT_NO_INCIDENT = 3


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_ERROR
    try:
        return int(args.handler(args))
    except (ConfigError, StoreError, TimeParseError) as exc:
        print(f"blackbox: error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blackbox",
        description="Flight recorder for your computers: what changed before this broke?",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_FILENAME, help="path to blackbox.toml")
    parser.add_argument("--version", action="version", version=f"blackbox {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("init", help="write a starter blackbox.toml and create the database")
    p.add_argument("--force", action="store_true", help="overwrite an existing config file")
    p.set_defaults(handler=cmd_init)

    p = sub.add_parser("run", help="record until interrupted (Ctrl+C or SIGTERM stops cleanly)")
    p.add_argument("--once", action="store_true", help="run a single collection pass and exit")
    p.add_argument("--duration", help="stop after this long, e.g. 30s, 10m")
    p.add_argument("--quiet", action="store_true", help="do not print events as they are recorded")
    p.set_defaults(handler=cmd_run)

    p = sub.add_parser("status", help="show database, chain and watch status")
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser("timeline", help="list recorded events")
    p.add_argument("--since", default="2h", help="start: 2h, ISO-8601, epoch seconds (default 2h)")
    p.add_argument("--until", default="now", help="end (default now)")
    p.add_argument("--kind", help="only kinds starting with this prefix, e.g. config or service")
    p.add_argument("--min-severity", choices=SEVERITIES)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--json", action="store_true", help="emit JSON lines")
    p.set_defaults(handler=cmd_timeline)

    p = sub.add_parser("search", help="search events by kind, subject or payload text")
    p.add_argument("term")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(handler=cmd_search)

    p = sub.add_parser("diff", help="show what changed in a tracked file")
    p.add_argument("file")
    p.add_argument("--at", default="now", help="latest change at or before this time")
    p.set_defaults(handler=cmd_diff)

    p = sub.add_parser("incident", help="build an incident report")
    p.add_argument(
        "--around",
        default="last-failure",
        help="time to centre on (ISO-8601/epoch/2h) or 'last-failure' (default)",
    )
    p.add_argument("--window", default="30m", help="look-back before the time (default 30m)")
    p.add_argument("--after", default="5m", help="how far past the time to include (default 5m)")
    p.add_argument("--out", help="write to this file; .html, .md or .json picks the format")
    p.add_argument("--format", choices=("html", "md", "json"), help="override the format")
    p.set_defaults(handler=cmd_incident)

    p = sub.add_parser("verify", help="check the hash chain for tampering")
    p.add_argument("--anchor", help="head hash recorded earlier; detects removed trailing events")
    p.set_defaults(handler=cmd_verify)
    return parser


# -- helpers ------------------------------------------------------------------------


def _open(args: argparse.Namespace) -> tuple[Config, Store]:
    config = load_config(args.config)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    return config, Store(config.db_path, config.machine_id)


def _line(event: Event) -> str:
    detail = describe(event)
    return (
        f"{iso(effective_ts_us(event))}  {event.severity:<8}  {event.kind:<20}  "
        f"{event.subject}" + (f"  {detail}" if detail else "")
    )


# -- commands -----------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_ERROR
    path.write_text(TEMPLATE, encoding="utf-8")
    config, store = _open(args)
    store.close()
    print(f"wrote {path}")
    print(f"created {config.db_path}")
    print("edit the config, then run: blackbox run")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    config, store = _open(args)
    collectors = build_collectors(config)
    if not collectors:
        print(
            "nothing to record: enable resources or add files/services to the config",
            file=sys.stderr,
        )
        store.close()
        return EXIT_ERROR
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        stop.set()

    def echo(events: list[Event]) -> None:
        for event in events:
            if event.severity != "info" or event.kind.startswith("recorder."):
                print(_line(event), flush=True)

    previous_handlers = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            previous_handlers[number] = signal.signal(number, request_stop)
    timer = None
    if args.duration:
        timer = threading.Timer(parse_duration_us(args.duration) / 1_000_000, stop.set)
        timer.daemon = True
        timer.start()

    recorder = Recorder(
        store, collectors, interval=config.interval_seconds, on_events=None if args.quiet else echo
    )
    if not args.quiet:
        names = ", ".join(c.name for c in collectors)
        print(f"recording to {config.db_path} every {config.interval_seconds:g}s ({names})")
    try:
        ticks = recorder.run(stop, max_ticks=1 if args.once else None)
    finally:
        if timer is not None:
            timer.cancel()
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
        store.close()
    if not args.quiet:
        print(f"stopped after {ticks} collection pass(es)")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config, store = _open(args)
    try:
        chain = store.verify_chain()
        last_tick = store.meta_get("last_tick_us")
        print(f"machine        {config.machine_id}")
        print(f"database       {config.db_path}")
        print(f"events         {store.count_events()}")
        if last_tick:
            age = (now_us() - int(last_tick)) / 1_000_000
            print(f"last pass      {iso(int(last_tick))} ({age:.0f}s ago)")
        else:
            print("last pass      never")
        print(f"hash chain     {'intact' if chain.ok else 'BROKEN: ' + chain.message}")
        print(f"chain head     {chain.head}")
        files = store.state_items("file:")
        services = store.state_items("service:")
        print(f"tracked files  {len(files)}")
        for key, value in files.items():
            print(f"  {key[5:]}  [{value if value in ('absent', 'unreadable') else 'present'}]")
        print(f"services       {len(services)}")
        for key, value in services.items():
            print(f"  {key[8:]}  [{value}]")
        return EXIT_OK if chain.ok else EXIT_INTEGRITY
    finally:
        store.close()


def cmd_timeline(args: argparse.Namespace) -> int:
    _, store = _open(args)
    try:
        now = now_us()
        events = store.events(
            since_us=parse_time_us(args.since, now_us=now),
            until_us=parse_time_us(args.until, now_us=now),
            kind_prefix=args.kind,
            min_severity=args.min_severity,
            limit=args.limit,
        )
        for event in events:
            if args.json:
                print(
                    json.dumps(
                        {
                            "id": event.id,
                            "ts": iso(event.ts_us),
                            "kind": event.kind,
                            "subject": event.subject,
                            "severity": event.severity,
                            "payload": event.payload,
                        }
                    )
                )
            else:
                print(_line(event))
        if not events and not args.json:
            print("no events in this range")
        return EXIT_OK
    finally:
        store.close()


def cmd_search(args: argparse.Namespace) -> int:
    _, store = _open(args)
    try:
        events = store.search(args.term, limit=args.limit)
        for event in events:
            print(_line(event))
        if not events:
            print(f"no events match {args.term!r}")
        return EXIT_OK
    finally:
        store.close()


def cmd_diff(args: argparse.Namespace) -> int:
    _, store = _open(args)
    try:
        at = parse_time_us(args.at, now_us=now_us())
        path = os.path.abspath(os.path.expanduser(args.file))
        changes = store.events(
            kinds=["config.changed"], subject=path, until_us=at, limit=1, newest_first=True
        )
        if not changes:
            print(f"no recorded change to {path} at or before {iso(at)}", file=sys.stderr)
            return EXIT_ERROR
        event = changes[0]
        print(
            f"# change detected {iso(event.ts_us)}, file modified "
            f"{iso(effective_ts_us(event))}: {describe(event)}"
        )
        diff = event.payload.get("diff")
        if diff:
            print(diff)
        else:
            print("(no stored content to diff)")
        return EXIT_OK
    finally:
        store.close()


def cmd_incident(args: argparse.Namespace) -> int:
    _, store = _open(args)
    try:
        now = now_us()
        around = None if args.around == "last-failure" else parse_time_us(args.around, now_us=now)
        try:
            incident = build_incident(
                store,
                around_us=around,
                window_us=parse_duration_us(args.window),
                after_us=parse_duration_us(args.after),
                generated_us=now,
            )
        except NoIncidentError as exc:
            print(f"blackbox: {exc}", file=sys.stderr)
            return EXIT_NO_INCIDENT
        fmt = args.format
        if fmt is None:
            suffix = Path(args.out).suffix.lower() if args.out else ".md"
            fmt = {".html": "html", ".htm": "html", ".json": "json"}.get(suffix, "md")
        text = (
            render_html(incident)
            if fmt == "html"
            else json.dumps(to_dict(incident), indent=2, sort_keys=True)
            if fmt == "json"
            else render_markdown(incident)
        )
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(
                f"wrote {args.out} ({len(incident.findings)} failure(s), "
                f"{len(incident.events)} event(s) in window)"
            )
        else:
            print(text)
        return EXIT_OK if incident.chain.ok else EXIT_INTEGRITY
    finally:
        store.close()


def cmd_verify(args: argparse.Namespace) -> int:
    _, store = _open(args)
    try:
        result = store.verify_chain(args.anchor)
        print(f"{'OK' if result.ok else 'FAILED'}: {result.message}")
        print(f"events checked {result.checked}")
        print(f"chain head     {result.head}")
        return EXIT_OK if result.ok else EXIT_INTEGRITY
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
