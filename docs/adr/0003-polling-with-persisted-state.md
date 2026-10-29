# ADR 0003: Poll and diff against persisted state

Status: accepted

## Context

File-watch and service-event APIs differ per platform and miss anything that happened while the recorder was not running. The question we want to answer, "what changed before this broke?", often involves a recorder that was restarted or a machine that rebooted.

## Decision

Collectors poll on an interval and compare the current observation with **state persisted in the database** (last digest, last service status). They do not rely on live event hooks. File events also record the file's modification time.

## Consequences

- One implementation works on every platform; collectors are easy to test with a fake clock and temp files.
- Changes made while Blackbox was stopped are detected on the next run, and a `recorder.gap` event marks an unclean shutdown.
- Precision is bounded by the polling interval. A file modified and reverted between two polls is invisible; a service that fails and recovers between polls is invisible. File ordering is refined by modification time; failures can only be dated by the poll that saw them.
- Event-driven collectors (inotify, Windows Event Log) can be added later behind the same `Collector` interface.
