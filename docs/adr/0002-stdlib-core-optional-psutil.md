# ADR 0002: Standard-library core, `psutil` optional

Status: accepted

## Context

A recorder is most useful on the machine that is already in trouble, where installing dependencies may be slow, impossible or undesirable. Sentinel's CLI makes the same trade.

## Decision

The core (store, collectors, recorder, reports, CLI) depends only on the Python standard library. CPU and memory sampling use `psutil` when it is installed and otherwise read `/proc` on Linux. Disk usage uses `shutil.disk_usage`. Service probes shell out to `systemctl` or `sc` without a shell. Reports are built with string templates, not a template engine.

## Consequences

- `pip install` pulls in nothing else; CI runs the core tests with and without `psutil`.
- On platforms with neither `psutil` nor `/proc` (for example Windows without `psutil`), the resource collector reports itself unavailable once as a `recorder.note`; the other collectors keep working.
- Adding features that genuinely need a dependency means making it an optional extra with a fallback, or revisiting this decision explicitly.
