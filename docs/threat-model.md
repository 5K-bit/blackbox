# Threat model

Blackbox is a local, single-machine recorder. This document lists what it protects, who it assumes is hostile, and what remains your responsibility. Each mitigation points at the test that exercises it.

## Assets

1. **Integrity of the history.** The point of a flight recorder is that you can trust what it says happened.
2. **Confidentiality of tracked content.** Config files often contain credentials, and Blackbox stores copies.
3. **Availability of the host.** A monitor must not take down the machine it watches.
4. **The configuration file.** `blackbox.toml` can cause commands to run.

## Trust boundaries

| Boundary | Trusted | Untrusted |
|---|---|---|
| `blackbox.toml` | The operator who writes it | Anyone who can modify it (treat as code execution) |
| Tracked files and service output | Nothing they contain | Their size, encoding, type, content, timing |
| The database file | The Blackbox process | Other local users; anyone with file access (tamper evidence only) |
| Generated reports | Blackbox's escaping | Every string that came from a file, service or hostname |

## Threats and mitigations

| # | Threat | Mitigation | Evidence | Residual risk |
|---|---|---|---|---|
| 1 | A secret in a tracked file is copied into the database or a report | Pattern redaction before storage, diff and report; digests are keyed HMACs, so a stored digest cannot be used to test guesses; oversize and binary files are hash-only | `tests/unit/test_redact.py`, `test_config_files.py`, `tests/integration/test_acceptance.py` (secret bytes absent from the database file and its write-ahead log, captured live, and from every report) | Redaction is pattern-based. A secret in an unrecognised format will be stored. Use `extra_patterns`, and do not track files you cannot redact. |
| 2 | Someone edits or deletes history to hide a change | Hash chain over every field; append-only triggers; contiguous ids; `verify` recomputes everything; external `--anchor` detects removed trailing events | `tests/unit/test_store.py` (edit, middle delete, trailing delete with and without anchor) | Tamper **evidence**, not proofing. An attacker with write access can drop the triggers and rebuild the chain; the anchor must be stored elsewhere. |
| 3 | A hostile or odd file stalls or crashes the recorder | `max_bytes` cap with streaming hash; only regular files are opened (a FIFO would block forever); binary content never decoded; diff line cap; collector exceptions isolated and reported once | `test_config_files.py` (oversize, binary, FIFO, directory, long diff), `test_recorder.py` (failing collector) | A file on a hung network mount can still block a read. Track local paths. |
| 4 | Script injection through a generated report | All dynamic text HTML-escaped; reports are self-contained (no scripts, links or external resources) | `test_report.py::test_html_escapes_untrusted_content`, `test_html_is_self_contained` | None known. Markdown output is plain text and is not sanitised for renderers that execute embedded HTML. |
| 5 | Another local user reads the database | Created owner-only (mode 0600) before SQLite first opens it, so the -wal/-shm files inherit it | `test_store.py::test_new_database_is_owner_only` | POSIX only. On Windows, access follows the folder ACL; put the database in a protected directory. Existing files keep their current mode. |
| 6 | A malicious `blackbox.toml` runs arbitrary commands | `command` probes run an argument list with `shell=False`; unknown keys and wrong types are rejected at load | `test_services.py::test_default_runner_runs_without_a_shell`, `test_config.py` | The file is trusted code. Protect it with the same permissions as a script that runs as this user. |
| 7 | The recorder consumes too much CPU, memory or disk | Polling only; bounded file reads; bounded diffs; WAL mode | `docs/evidence/overhead.txt` (short smoke measurement) | **No retention yet**: the event table grows without limit. There is no 24-hour soak result. |
| 8 | The system clock moves | Ordering uses insertion id as a tiebreaker; `verify` does not depend on time | `test_store.py::test_filters_and_ordering` (time, then id) | Timestamps are not authenticated. A clock step makes the timeline non-monotonic in time (never in id). |
| 9 | Probes become a scanning tool | Probes only touch hosts/ports the operator lists; default host is loopback | `test_config.py` | The operator can point a TCP probe anywhere. It opens a connection and closes it; it sends no data. |
| 10 | Accidental network egress | The code makes no network calls other than the configured TCP probes; there is no telemetry | Code review of `src/` (no HTTP client imported); stdlib-only core | Not enforced by a test. |

## Out of scope for v0.1

- A hostile local administrator or kernel. Anyone with root can alter anything Blackbox can see.
- Protecting the host from malware; Blackbox records, it does not prevent.
- Multi-machine collection, remote shipping, or access control between users. Each machine has its own database.
