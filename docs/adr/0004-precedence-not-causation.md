# ADR 0004: Reports say "preceded by", not "caused by"

Status: accepted

## Context

An incident report that names a root cause will sometimes be wrong, and a wrong confident answer is worse than none during an outage. Blackbox sees changes and failures; it does not understand why one led to the other.

## Decision

For each failure, list every configuration change, earlier failure and resource spike that happened before it within the look-back window, nearest first, with how long before, and attach the diffs. State in the report that this is ordering, not causation. The rule is deterministic and small enough to read in one sitting (`report.build_incident`).

## Consequences

- Reports are reproducible: same database and parameters, same output (golden-file tested).
- Unrelated changes appear in the list too, ranked by proximity. In the acceptance scenario a harmless edit to a second file is listed behind the fatal one.
- There is no scoring or machine-learned ranking. If one is added later it must be opt-in, must keep the deterministic list, and must label itself as a hint.
