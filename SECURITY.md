# Security Policy

## Intended use

Blackbox records changes on a computer **you own or administer**. It reads the configuration files you list, probes the services you list, and stores the result in a local SQLite file. It makes no network connections except the TCP probes you configure, and sends nothing anywhere.

## What Blackbox protects, and what it does not

- Tracked file content is passed through secret redaction before it is stored or shown. Redaction is pattern-based and best-effort. Do not track files whose secrets you cannot recognise by pattern; use `[redaction] extra_patterns` for your own formats.
- The history is hash-chained, so edits and deletions in the middle are detected by `blackbox verify`. This is **tamper evidence, not tamper proofing**: someone with write access to the database file can rebuild the chain. Record the chain head (`blackbox status`) somewhere else and pass it to `blackbox verify --anchor` to also detect removed trailing events.
- `blackbox.toml` is trusted configuration. `kind = "command"` probes run the argument list you give, without a shell, as the user running Blackbox. Protect that file like any other file that can run code.
- On POSIX systems the database is created readable by its owner only. On Windows access follows the folder's ACL.

The full analysis is in [docs/threat-model.md](docs/threat-model.md).

## Reporting a vulnerability

If you find a way to read secrets that redaction should have removed, make `verify` pass on altered history, make a hostile file or probe target stall or crash the recorder, or inject script into a generated report, please report it privately through GitHub's **Security → Report a vulnerability** on this repository rather than a public issue.

Include the Blackbox version (`blackbox --version`), your OS and Python version, and the smallest configuration and file content that reproduces it. Expect an initial response within a few days.
