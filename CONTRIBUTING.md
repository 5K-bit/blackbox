# Contributing

Thanks for your interest in Blackbox.

## Ways to help

- Report bugs, especially platform differences (Windows services, systemd edge cases).
- Add or improve a collector (see [docs/architecture.md](docs/architecture.md#writing-a-collector)).
- Improve redaction patterns, report output or docs.

## Development

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
python -m pytest --cov                            # must pass before you open a PR
python examples/acceptance.py --out /tmp/evidence # the real-process acceptance scenario
```

## Guidelines

- The core uses only the Python standard library. `psutil` is an optional extra and the code must keep working without it.
- No network calls by default. Anything that touches the network is opt-in, documented, and covered in the threat model.
- Parsers and collectors treat all input as hostile: size limits, no `eval`, no shell, never block the recorder.
- Secrets must not reach the database or any report. Add a test that proves it for new data paths.
- Changing a report format? Regenerate the golden files with `UPDATE_GOLDEN=1 python -m pytest tests/unit/test_report.py` and review the diff.
- Do not add claims (performance, platform support, security) that a test or recorded evidence does not back up.
- Use clear commit messages and keep pull requests focused.
