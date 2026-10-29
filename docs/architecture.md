# Architecture

Blackbox is a small recorder with three layers: **collectors** that observe, a **recorder** that commits what they saw, and a **store** that keeps an append-only, hash-chained history. Reports and the CLI only read from the store.

```
 host                                          blackbox process
 ────                                          ────────────────
 tracked config files ──┐
 services (systemd /     ├─► Collectors ─► Observation ─► Recorder ─► Store (SQLite, WAL)
   Windows / TCP / cmd) ─┤   (read-only)   drafts, state,   one atomic        events (append-only,
 CPU / memory / disk ───┘                  file versions    commit per tick     hash-chained)
                                                                                file_versions, state
                                                                                      │
                                              blackbox timeline / search / diff       │
                                              blackbox incident ──► HTML / MD / JSON ◄┘
                                              blackbox verify   ──► recompute chain
```

## Modules

| Module | Responsibility |
|---|---|
| `collectors/config_files.py` | Hash, redact and diff tracked files. Detects creation, change, deletion, unreadable paths. |
| `collectors/services.py` | Probe services (`systemd`, `windows`, `tcp`, `command`); emit up/down/unknown transitions. |
| `collectors/resources.py` | Sample CPU/memory/disk; flag spikes against a rolling baseline. |
| `recorder.py` | Run collectors each tick, isolate their failures, commit atomically, mark start/stop/gaps. |
| `store.py` | SQLite schema, hash chain, append-only triggers, keyed digests, queries, `verify_chain`. |
| `redact.py` | Remove likely secrets before content is stored or shown. |
| `report.py` | Build incidents (failures and what preceded them) and render HTML, Markdown, JSON. |
| `config.py` | Strict `blackbox.toml` loading and collector construction. |
| `cli.py` | Thin command-line layer over the above. |

The core library has no third-party dependencies. `psutil` is an optional extra for CPU/memory sampling; without it Linux falls back to `/proc`.

## One tick

1. The recorder asks each collector what it sees now. A collector compares that with **persisted state** (last digest, last service status) and returns an `Observation`: event drafts, state updates, new file versions.
2. A collector that raises does not stop the others. The failure is recorded once as a `recorder.note` and cleared when the collector recovers.
3. The recorder commits all drafts, state and file versions in **one transaction**, so a crash can never leave state ahead of events.
4. Because state is persisted, a change made while Blackbox was not running is found on the next start. Its event is dated by detection time, and for files also carries the file's modification time.

## Data model

`events` (append-only; UPDATE and DELETE are rejected by triggers)

| Column | Meaning |
|---|---|
| `id` | Insertion order, contiguous from 1 (gaps are detected). |
| `ts_us` | Detection time, microseconds since the Unix epoch, UTC. |
| `machine_id`, `kind`, `subject`, `severity` | What happened, to what, how serious (`info`, `warning`, `error`, `critical`). |
| `before_digest`, `after_digest` | Keyed digests (HMAC-SHA-256) of file content before and after. |
| `payload` | Canonical JSON with the details (diff, status, spike values). |
| `prev_hash`, `hash` | `hash = SHA-256(prev_hash ‖ all fields above)`. The chain. |

`file_versions` holds the redacted text of each tracked file version (used to compute the next diff). `state` holds collector memory. `meta` holds the schema version, a random per-database HMAC key and the last pass time. Schema version is tracked with `PRAGMA user_version`; a database newer than the program is refused.

## Event kinds

| Kind | Severity | When |
|---|---|---|
| `recorder.started` / `recorder.stopped` | info | A run begins / ends. |
| `recorder.gap` | warning | A run started and the previous one never stopped cleanly. |
| `recorder.note` | warning | A collector failed. |
| `config.baseline` | info | First sighting of a tracked file (or its absence). |
| `config.changed` | warning | Content changed; payload carries a redacted unified diff. |
| `config.created` / `config.deleted` | warning | A tracked path appeared / disappeared. |
| `config.unreadable` | warning | Not a regular file, or permission denied. |
| `service.baseline` | info | First observation of a service. |
| `service.failed` | error | Status became `down`. |
| `service.recovered` | info | Status became `up` again. |
| `service.unknown` | warning | The probe cannot tell (tool missing, host unresolvable, unit not found). |
| `resource.spike` / `resource.recovered` | warning / info | Metric above its floor **and** above its baseline (disk: floor only) / back below. |

## The incident correlation rule

For each failure (`service.failed`, `resource.spike`) in the window, the report lists every earlier configuration change, earlier failure and resource spike inside the look-back window, **nearest first**. Config events are ordered by the file's modification time when known, because it is more precise than the poll that noticed it. A future-dated modification time is ignored.

"Preceded by" is deliberately not "caused by". The report states what happened and how long before, and leaves the conclusion to the reader. See [ADR 0004](adr/0004-precedence-not-causation.md).

## Writing a collector

A collector is any object with a `name` and a `collect(store, now_us)` method returning an `Observation`. It reads state through `store.state_get`, never writes to the database itself, and must treat everything it reads as hostile input.

```python
from blackbox.collectors.base import Observation
from blackbox.model import EventDraft


class UptimeCollector:
    name = "uptime"

    def collect(self, store, now_us):
        booted = read_boot_time()  # your platform call
        previous = store.state_get("uptime:boot")
        obs = Observation(state={"uptime:boot": str(booted)})
        if previous is not None and previous != str(booted):
            obs.drafts.append(
                EventDraft("system.rebooted", "host", "warning", payload={"boot_time": booted})
            )
        return obs
```

Add it to the `collectors` list passed to `Recorder`. Add tests alongside: a first-sighting baseline, a transition, silence when nothing changed, and a hostile-input case.
