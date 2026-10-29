# Evidence

Everything in this folder was produced by running the code in this repository. Nothing is hand-edited.

| File | What it shows | How to reproduce |
|---|---|---|
| `acceptance-transcript.txt` | Events recorded during the acceptance scenario, as printed live | `python examples/acceptance.py --out docs/evidence` |
| `acceptance-incident.md`, `.html` | The incident report for that run: a real process crashed after a config edit | same command |
| `acceptance.db` | The database from that run. Open it with any SQLite tool, or point a config's `db_path` at it and run `blackbox verify` | same command |
| `test-run.txt` | Full verbose test run with the coverage table | `python -m pytest -o addopts="" -v --cov --cov-report=term-missing` |
| `overhead.txt` | Recorder CPU, memory and database growth over a 30-second run at 1 s polling | `python examples/overhead.py --seconds 30 --interval 1` |

## How to read the acceptance run

`examples/demo_service.py` is a real process that listens on a TCP port and exits when its config says `mode = broken`. The scenario tracks three config files, makes a harmless edit to one and the fatal edit to another, and records until the service dies. The crash is a real process exit seen through a real TCP probe. The report must order the config change before the failure, attach the diff, rank the fatal edit ahead of the harmless one, and show no secret (the test inspects the database file and its write-ahead log, captured while the recorder was still running, as well as every report).

The temporary directory names in the transcript differ on every run, and the two config edits and the crash are often noticed in the same one-second poll. In that case the report orders them by the files' modification times, which is why it can still say the config change came first.

## What this evidence does not show

- Behaviour on Windows, or against a real `systemd` or Windows service. Those probes are unit-tested against sample command output only.
- Long-running behaviour. The overhead figure is a 30-second smoke measurement, not a 24-hour soak test.
- Anything about CI. The workflows in `.github/` have not yet run on GitHub.
