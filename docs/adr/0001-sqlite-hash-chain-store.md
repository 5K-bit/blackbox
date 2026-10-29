# ADR 0001: SQLite with an append-only hash chain

Status: accepted

## Context

The history must be queryable (timeline, search, "around this time"), survive crashes, run with no server, and be trustworthy after an incident. Plain log files are hard to query and easy to edit silently.

## Decision

Store events in one SQLite database (WAL mode). Reject UPDATE and DELETE with triggers. Chain each event to the previous with `hash = SHA-256(prev_hash ‖ fields)`, and expose `verify_chain` with an optional external anchor. Commit all of a tick's events, state and file versions in a single transaction.

## Consequences

- Queries are plain SQL and the file opens in any SQLite tool.
- Crash safety comes from SQLite transactions; state can never get ahead of events.
- Tampering in the middle of history is detected. Removing trailing events is detected only with an anchor recorded elsewhere. Someone with write access can rebuild the chain: this is tamper evidence, not tamper proofing, and every document says so.
- There is no retention or compaction yet; deleting old events means starting a new database or a future, explicit archive operation.
