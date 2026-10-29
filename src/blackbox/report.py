"""Incident reports: what changed before this broke?

The correlation rule is deliberately simple and deterministic. For each failure
(a service going down, a resource spike) the report lists every configuration
change, earlier failure and resource spike that *preceded* it inside the
look-back window, nearest first. "Preceded" is not "caused": the report says
what happened before, and how long before, and leaves the conclusion to you.

Timing precision is bounded by the polling interval. Configuration events are
ordered by the file's modification time when it is known (it is more precise
than the poll that noticed it); failures can only be dated by the poll that saw
them.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from . import __version__
from .model import (
    KIND_CONFIG_CHANGED,
    KIND_CONFIG_CREATED,
    KIND_CONFIG_DELETED,
    KIND_CONFIG_UNREADABLE,
    KIND_RESOURCE_SPIKE,
    KIND_SERVICE_FAILED,
    Event,
)
from .store import ChainResult, Store
from .timeparse import iso

FAILURE_KINDS = (KIND_SERVICE_FAILED, KIND_RESOURCE_SPIKE)
CHANGE_KINDS = (
    KIND_CONFIG_CHANGED,
    KIND_CONFIG_CREATED,
    KIND_CONFIG_DELETED,
    KIND_CONFIG_UNREADABLE,
)
PRECURSOR_KINDS = (*CHANGE_KINDS, KIND_RESOURCE_SPIKE, KIND_SERVICE_FAILED)


class NoIncidentError(LookupError):
    """There is no failure to build a report around."""


def effective_ts_us(event: Event) -> int:
    """Best estimate of when the thing happened, as opposed to when it was noticed."""
    if event.kind in (KIND_CONFIG_CHANGED, KIND_CONFIG_CREATED):
        mtime = event.payload.get("mtime_us")
        if isinstance(mtime, int) and mtime <= event.ts_us:
            return mtime
    return event.ts_us


@dataclass(frozen=True)
class Precursor:
    event: Event
    effective_us: int
    lead_us: int  # how long before the failure


@dataclass(frozen=True)
class Finding:
    failure: Event
    precursors: tuple[Precursor, ...]


@dataclass(frozen=True)
class Incident:
    machine_id: str
    around_us: int
    window_start_us: int
    window_end_us: int
    events: tuple[Event, ...]
    findings: tuple[Finding, ...]
    chain: ChainResult
    generated_us: int
    version: str = __version__


def build_incident(
    store: Store,
    *,
    around_us: int | None,
    window_us: int,
    after_us: int,
    generated_us: int,
) -> Incident:
    if around_us is None:
        failures = store.events(kinds=FAILURE_KINDS, limit=1, newest_first=True)
        if not failures:
            raise NoIncidentError("no failures have been recorded yet")
        around_us = failures[0].ts_us
    start, end = around_us - window_us, around_us + after_us
    events = store.events(since_us=start, until_us=end)
    ordered = tuple(sorted(events, key=lambda e: (effective_ts_us(e), e.id)))
    findings = []
    for failure in (e for e in ordered if e.kind in FAILURE_KINDS):
        precursors = [
            Precursor(e, effective_ts_us(e), failure.ts_us - effective_ts_us(e))
            for e in ordered
            if e.kind in PRECURSOR_KINDS
            and e.id != failure.id
            and effective_ts_us(e) <= failure.ts_us
            and (e.id < failure.id or effective_ts_us(e) < failure.ts_us)
        ]
        findings.append(Finding(failure, tuple(sorted(precursors, key=lambda p: p.lead_us))))
    return Incident(
        machine_id=store.machine_id,
        around_us=around_us,
        window_start_us=start,
        window_end_us=end,
        events=ordered,
        findings=tuple(findings),
        chain=store.verify_chain(),
        generated_us=generated_us,
    )


# -- describing events -------------------------------------------------------------


def describe(event: Event) -> str:
    p = event.payload
    kind = event.kind
    if kind == KIND_CONFIG_CHANGED:
        if p.get("diff") is None:
            return "content changed (no stored content to diff)"
        if p.get("redacted_only"):
            return "only redacted values changed"
        return f"+{p.get('lines_added', 0)} -{p.get('lines_removed', 0)} lines"
    if kind == KIND_CONFIG_CREATED:
        return f"file created ({p.get('size', '?')} bytes)"
    if kind == KIND_CONFIG_DELETED:
        return "file deleted"
    if kind == KIND_CONFIG_UNREADABLE:
        return f"file unreadable: {p.get('reason', 'unknown')}"
    if kind.startswith("service."):
        before = f"{p['from']} -> " if "from" in p else ""
        return f"{before}{p.get('status', '?')} ({p.get('detail', '')})"
    if kind == KIND_RESOURCE_SPIKE:
        base = p.get("baseline_mean")
        extra = "" if base is None else f", baseline {base}%"
        return f"{p.get('value')}% (floor {p.get('floor')}%{extra})"
    if kind == "resource.recovered":
        return f"back to {p.get('value')}%"
    if kind == "config.baseline":
        return "baseline recorded" if p.get("present", True) else "baseline: file absent"
    if kind.startswith("recorder."):
        return str(p.get("note") or p.get("error") or p.get("reason") or p.get("version") or "")
    return ""


def human_delta(us: int) -> str:
    seconds = max(0, us) / 1_000_000
    if seconds < 1:
        return "<1s"
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


# -- renderers ---------------------------------------------------------------------


def render_markdown(inc: Incident) -> str:
    out: list[str] = []
    out.append(f"# Incident report - {inc.machine_id}")
    out.append("")
    out.append(
        f"Window: {iso(inc.window_start_us)} to {iso(inc.window_end_us)} (UTC), "
        f"around {iso(inc.around_us)}"
    )
    out.append(
        f"Generated by Blackbox {inc.version} at {iso(inc.generated_us)} | "
        f"hash chain: {'intact' if inc.chain.ok else 'BROKEN - ' + inc.chain.message} "
        f"({inc.chain.checked} events, head {inc.chain.head[:12]})"
    )
    out.append("")
    out.append("## Summary")
    out.append("")
    if not inc.findings:
        out.append("No failures were recorded in this window.")
    for number, finding in enumerate(inc.findings, 1):
        f = finding.failure
        line = f"{number}. {f.kind} on {f.subject} at {iso(f.ts_us)}"
        if finding.precursors:
            nearest = finding.precursors[0]
            line += (
                f" - nearest preceding event: {nearest.event.kind} on {nearest.event.subject} "
                f"{human_delta(nearest.lead_us)} earlier"
            )
        else:
            line += " - nothing recorded before it in this window"
        out.append(line)
    out.append("")
    out.append("## Findings")
    for number, finding in enumerate(inc.findings, 1):
        f = finding.failure
        out.append("")
        out.append(f"### {number}. {f.kind} - {f.subject}")
        out.append("")
        out.append(f"- Detected: {iso(f.ts_us)}")
        out.append(f"- Detail: {describe(f)}")
        out.append("- Preceded by (nearest first):")
        if not finding.precursors:
            out.append("  - nothing recorded in the look-back window")
        for pre in finding.precursors:
            e = pre.event
            when = f"{human_delta(pre.lead_us)} before"
            stamp = iso(pre.effective_us)
            out.append(f"  - {when}: {e.kind} - {e.subject} ({describe(e)}), at {stamp}")
        for pre in finding.precursors:
            diff = pre.event.payload.get("diff")
            if pre.event.kind == KIND_CONFIG_CHANGED and diff:
                out.append("")
                out.append(f"Diff for {pre.event.subject}:")
                out.append("")
                out.append("```diff")
                out.append(str(diff))
                out.append("```")
    out.append("")
    out.append("## Timeline")
    out.append("")
    out.append("| Time (UTC) | Kind | Subject | Severity | Detail |")
    out.append("|---|---|---|---|---|")
    for e in inc.events:
        out.append(
            f"| {iso(effective_ts_us(e))} | {e.kind} | {_md(e.subject)} | {e.severity} | "
            f"{_md(describe(e))} |"
        )
    out.append("")
    out.append("## How to read this")
    out.append("")
    out.append(
        "Events are ordered by when they happened where Blackbox can tell (file modification "
        "time) and otherwise by when a poll noticed them, so timing is accurate to the polling "
        "interval. 'Preceded by' means earlier, not 'caused by'."
    )
    out.append("")
    return "\n".join(out)


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_dict(inc: Incident) -> dict[str, Any]:
    def ev(e: Event) -> dict[str, Any]:
        return {
            "id": e.id,
            "ts": iso(e.ts_us, precision="milliseconds"),
            "effective_ts": iso(effective_ts_us(e), precision="milliseconds"),
            "kind": e.kind,
            "subject": e.subject,
            "severity": e.severity,
            "detail": describe(e),
            "payload": e.payload,
            "hash": e.hash,
        }

    return {
        "blackbox_version": inc.version,
        "machine_id": inc.machine_id,
        "generated": iso(inc.generated_us),
        "window": {
            "start": iso(inc.window_start_us),
            "end": iso(inc.window_end_us),
            "around": iso(inc.around_us),
        },
        "chain": {
            "ok": inc.chain.ok,
            "checked": inc.chain.checked,
            "head": inc.chain.head,
            "message": inc.chain.message,
        },
        "findings": [
            {
                "failure": ev(f.failure),
                "preceded_by": [
                    {"lead_seconds": round(p.lead_us / 1_000_000, 3), "event": ev(p.event)}
                    for p in f.precursors
                ],
            }
            for f in inc.findings
        ],
        "timeline": [ev(e) for e in inc.events],
    }


_CSS = """
:root{--bg:#0c1220;--panel:#121a2c;--line:#283345;--text:#f3f5fa;--muted:#99a7bc;--accent:#ff9f1c;
--add:#5fd38d;--del:#ff6b6b;}
@media (prefers-color-scheme: light){:root{--bg:#f6f7fa;--panel:#fff;--line:#d5dae3;--text:#161c2b;
--muted:#566074;--accent:#b86800;--add:#17803d;--del:#c62828;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.55 -apple-system,Segoe UI,Roboto,Arial,sans-serif;padding:32px 16px}
main{max-width:980px;margin:0 auto}h1{font-size:26px;margin:0 0 4px}
h2{font-size:18px;margin:32px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{font-size:16px;margin:0 0 8px}.eyebrow{color:var(--accent);letter-spacing:.14em;font-size:12px;
font-weight:700;text-transform:uppercase}.meta{color:var(--muted);font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;
margin:12px 0}.ok{color:var(--add)}.bad{color:var(--del);font-weight:700}
code,pre{font:13px ui-monospace,Menlo,Consolas,monospace}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
.add{color:var(--add)}.del{color:var(--del)}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.sev-error,.sev-critical{color:var(--del)}
.sev-warning{color:var(--accent)}ul{padding-left:20px}footer{margin-top:32px}
"""


def render_html(inc: Incident) -> str:
    e = html.escape
    parts: list[str] = []
    parts.append('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append(f"<title>Blackbox incident report - {e(inc.machine_id)}</title>")
    parts.append(f"<style>{_CSS}</style></head><body><main>")
    parts.append('<div class="eyebrow">Blackbox by OBEOS</div>')
    parts.append(f"<h1>Incident report - {e(inc.machine_id)}</h1>")
    chain = (
        '<span class="ok">intact</span>'
        if inc.chain.ok
        else f'<span class="bad">BROKEN - {e(inc.chain.message)}</span>'
    )
    parts.append(
        f'<p class="meta">Window {e(iso(inc.window_start_us))} to {e(iso(inc.window_end_us))} UTC, '
        f"around {e(iso(inc.around_us))}.<br>Generated by Blackbox {e(inc.version)} at "
        f"{e(iso(inc.generated_us))}. Hash chain: {chain} ({inc.chain.checked} events, head "
        f"<code>{e(inc.chain.head[:12])}</code>).</p>"
    )
    parts.append("<h2>Summary</h2>")
    if not inc.findings:
        parts.append("<p>No failures were recorded in this window.</p>")
    else:
        parts.append("<ol>")
        for finding in inc.findings:
            f = finding.failure
            text = f"{f.kind} on {f.subject} at {iso(f.ts_us)}"
            if finding.precursors:
                n = finding.precursors[0]
                text += (
                    f" - nearest preceding event: {n.event.kind} on {n.event.subject} "
                    f"{human_delta(n.lead_us)} earlier"
                )
            else:
                text += " - nothing recorded before it in this window"
            parts.append(f"<li>{e(text)}</li>")
        parts.append("</ol>")
    parts.append("<h2>Findings</h2>")
    for number, finding in enumerate(inc.findings, 1):
        f = finding.failure
        parts.append(f'<div class="card"><h3>{number}. {e(f.kind)} - {e(f.subject)}</h3>')
        parts.append(
            f'<p class="meta">Detected {e(iso(f.ts_us))} - {e(describe(f))}</p>'
            "<p><strong>Preceded by</strong> (nearest first)</p><ul>"
        )
        if not finding.precursors:
            parts.append("<li>nothing recorded in the look-back window</li>")
        for pre in finding.precursors:
            ev = pre.event
            parts.append(
                f"<li>{e(human_delta(pre.lead_us))} before: <code>{e(ev.kind)}</code> "
                f"{e(ev.subject)} ({e(describe(ev))}) at {e(iso(pre.effective_us))}</li>"
            )
        parts.append("</ul>")
        for pre in finding.precursors:
            diff = pre.event.payload.get("diff")
            if pre.event.kind == KIND_CONFIG_CHANGED and diff:
                parts.append(f'<p class="meta">Diff for {e(pre.event.subject)}</p><pre>')
                for line in str(diff).splitlines():
                    cls = (
                        "add"
                        if line.startswith("+") and not line.startswith("+++")
                        else "del"
                        if line.startswith("-") and not line.startswith("---")
                        else ""
                    )
                    parts.append(
                        f'<span class="{cls}">{e(line)}</span>\n' if cls else f"{e(line)}\n"
                    )
                parts.append("</pre>")
        parts.append("</div>")
    parts.append(
        "<h2>Timeline</h2><table><thead><tr><th>Time (UTC)</th><th>Kind</th>"
        "<th>Subject</th><th>Severity</th><th>Detail</th></tr></thead><tbody>"
    )
    for ev in inc.events:
        parts.append(
            f"<tr><td>{e(iso(effective_ts_us(ev)))}</td><td><code>{e(ev.kind)}</code></td>"
            f'<td>{e(ev.subject)}</td><td class="sev-{e(ev.severity)}">{e(ev.severity)}</td>'
            f"<td>{e(describe(ev))}</td></tr>"
        )
    parts.append("</tbody></table>")
    parts.append(
        '<footer class="meta"><h2>How to read this</h2><p>Events are ordered by when they '
        "happened where Blackbox can tell (file modification time) and otherwise by when a poll "
        "noticed them, so timing is accurate to the polling interval. 'Preceded by' means "
        "earlier, not 'caused by'.</p></footer></main></body></html>"
    )
    return "".join(parts)
