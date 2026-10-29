# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Release workflow now re-runs the full CI matrix on the tagged commit and publishes only if it passes.
- Release workflow builds the wheel, installs it in a clean environment, smoke-tests the CLI and runs the test suite against it before publishing.
- Release notes come from `docs/releases/<tag>.md` (capabilities and known limitations); `0.x` and suffixed tags are published as prereleases.

## [0.1.0] - 2026-10-29

First working version.

### Added

- Append-only, hash-chained SQLite event store with `blackbox verify` and external anchoring.
- Collectors: tracked configuration files (redacted unified diffs, creation, deletion),
  services (`systemd`, `windows`, `tcp`, `command` probes) and resources (CPU, memory,
  disk spikes against a rolling baseline).
- Recorder with clean start/stop markers, gap detection after unclean shutdown, and
  isolation of failing collectors.
- Incident reports in HTML, Markdown and JSON: failures, what preceded them, diffs attached.
- CLI: `init`, `run`, `status`, `timeline`, `search`, `diff`, `incident`, `verify`.
- `blackbox.toml` configuration with strict validation.
- Secret redaction for stored file content and reports; owner-only database permissions on POSIX.
