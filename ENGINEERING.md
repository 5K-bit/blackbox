# BES-1 pilot: Blackbox

**Status:** Pilot proposal; does not assert production readiness.  
**Canonical standard:** [BES-1 on OBEOS](https://github.com/5K-bit/O.B.E.O.S/blob/main/docs/standards/BES-1.md) (proposed in [OBEOS PR #16](https://github.com/5K-bit/O.B.E.O.S/pull/16); merge that PR before adopting this pilot).

## Task interface

`just` is optional. It provides consistent aliases without replacing the existing CI workflow. Requires Python 3.11+; `just setup` installs the project's dev extras and Python `build` into the selected environment. Use a virtual environment. On Windows, `just` uses PowerShell.

| Command | Existing checks or behavior |
| --- | --- |
| `just setup` | Install editable package with `[dev]` and `build`. |
| `just check` | Ruff, formatting, strict mypy and pytest with >=90% coverage. |
| `just integration` | Existing `tests/integration` suite. |
| `just smoke` | Safe `blackbox --help` invocation. |
| `just build` | Build local Python artifacts. |
| `just release-check` | All above preflight checks; **never publishes**. |

Without `just`, use the commands inside `justfile` directly. No runtime file or GitHub release workflow is changed by this pilot.

## Evidence boundaries

- **Built:** commit/PR scoped change and affected files are identified.
- **Verified:** CI run and checks on the exact commit are recorded; report omitted tests.
- **Released:** tagged GitHub release has passing reusable CI, tag/version validation, release notes, tested wheel, and checksums. The existing `.github/workflows/release.yml` remains authoritative.
- **Operational:** actual Windows service or Linux systemd run is recorded with OS/version, uptime period, observed logs, restart behavior, state persistence, secret redaction, failure injection, recovery and uninstall. Do not infer this from a passing wheel smoke test.

## Required review notes

State the failure mode and recovery path. Record test commands and links; do not include secret-bearing sample data. Security-sensitive changes must demonstrate that sensitive values cannot enter Blackbox storage or reports. Clearly distinguish event ordering evidence from proof of causation.

**AI assistance:** describe implementation/review use in the PR.  
**Human verification:** record only tests and acceptance personally observed; `not run` is acceptable.
