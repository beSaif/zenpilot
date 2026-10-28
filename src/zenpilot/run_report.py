"""Run report: self-contained HTML and JSON written per run from its history, console, network and HAR."""

import html
import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from zenpilot.history import Record

ARG_MAX = 60  # characters kept per argument value in the HTML timeline
ERRORS_MAX = 20
SLOWEST_MAX = 10
THUMB_WIDTH = 160
THUMB_NAME = re.compile(r"step-\d{4}\.jpg")
NET_CLASSES = ("2xx", "3xx", "4xx", "5xx", "failed", "pending")

CSS = """
:root{--bg:#f7f7f8;--fg:#1d1f23;--muted:#6b7280;--card:#fff;--line:#e5e7eb;--chip:#eef2ff;--link:#1d4ed8;
--ok:#15803d;--ok-bg:#dcfce7;--err:#b91c1c;--err-bg:#fee2e2}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--fg:#e5e7eb;--muted:#9ca3af;--card:#171a21;--line:#2a2f3a;
--chip:#1e2433;--link:#93c5fd;--ok:#4ade80;--ok-bg:#14532d;--err:#f87171;--err-bg:#7f1d1d}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,sans-serif}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:24px 0 8px}
.meta{color:var(--muted);margin:0 0 12px;word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--chip);border-radius:999px;padding:4px 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:12px}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:480px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}
.args,.url{word-break:break-all;max-width:320px}
.badge{border-radius:4px;padding:1px 6px;font-size:12px;font-weight:600}
.badge.ok{color:var(--ok);background:var(--ok-bg)}
.badge.err{color:var(--err);background:var(--err-bg)}
.muted{color:var(--muted)}
img{display:block;max-width:160px;height:auto;border:1px solid var(--line);border-radius:4px}
a{color:var(--link)}
"""


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _td(value, cls: str = "", raw: str | None = None) -> str:
    attr = f' class="{cls}"' if cls else ""
    return f"<td{attr}>{raw if raw is not None else _e(value)}</td>"


def _none(text: str) -> str:
    return f'<p class="muted">{_e(text)}</p>'


def _chips(pairs) -> str:
    items = "".join(f'<span class="chip">{_e(k)} <b>{_e(v)}</b></span>' for k, v in pairs)
    return f'<div class="chips">{items}</div>'


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _thumb(run_dir: Path, shot: str | None) -> str:
    """Relative thumbnail link for a screenshot directly inside run_dir with a valid step name; else empty."""
    if not shot:
        return ""
    path = Path(shot)
    if not path.is_absolute():
        path = run_dir / path
    path = path.resolve()
    if path.parent != run_dir.resolve() or not THUMB_NAME.fullmatch(path.name):
        return ""
    name = path.name
    return f'<a href="{name}"><img src="{name}" loading="lazy" width="{THUMB_WIDTH}"></a>'


def _arg(value) -> str:
    text = str(value)
    return text if len(text) <= ARG_MAX else text[:ARG_MAX] + "..."


def _console_counts(entries: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in entries:
        level = str(e.get("level"))
        counts[level] = counts.get(level, 0) + 1
    return counts


def _network_counts(entries: list[dict]) -> dict[str, int]:
    """Status class per request; failed = network error or status >= 400; pending = no status and no error."""
    counts = dict.fromkeys(NET_CLASSES, 0)
    for e in entries:
        status, error = e.get("status"), e.get("error")
        if status is None and error is None:
            counts["pending"] += 1
        if status is not None and 200 <= status < 600:
            counts[f"{status // 100}xx"] += 1
        if error is not None or (status or 0) >= 400:
            counts["failed"] += 1
    return counts


def _failed(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("error") is not None or (e.get("status") or 0) >= 400]


def _slowest(entries: list[dict]) -> list[dict]:
    timed = [e for e in entries if e.get("duration_ms") is not None]
    return sorted(timed, key=lambda e: e["duration_ms"], reverse=True)[:SLOWEST_MAX]


def _timeline(run_dir: Path, records: list[Record]) -> str:
    if not records:
        return _none("No actions recorded.")
    rows = []
    for r in records:
        badge = '<span class="badge ok">ok</span>' if r.ok else '<span class="badge err">error</span>'
        outcome = f'{badge}<div class="muted">{_e(r.message)}</div>'
        args = " ".join(f"{k}={_arg(v)}" for k, v in r.args.items())
        time = datetime.fromisoformat(r.ts).strftime("%H:%M:%S")
        rows.append(
            "<tr>"
            + _td(r.seq)
            + _td(time)
            + _td(r.tool)
            + _td(args, cls="args")
            + _td("", raw=outcome)
            + _td(r.duration_ms)
            + _td(r.url or "-", cls="url")
            + _td("", raw=_thumb(run_dir, r.screenshot))
            + "</tr>"
        )
    return _table(["#", "time", "tool", "args", "outcome", "ms", "URL", "screenshot"], rows)


def _console_section(console: list[dict]) -> str:
    if not console:
        return _none("No console messages.")
    counts = _console_counts(console)
    errors = [e for e in console if e.get("level") == "error"][-ERRORS_MAX:]
    rows = []
    for e in errors:
        loc = e.get("url") or "-"
        if e.get("url") and e.get("line") is not None:
            loc = f"{e['url']}:{e['line']}"
        rows.append("<tr>" + _td(e.get("text")) + _td(loc, cls="url") + "</tr>")
    table = _table(["error", "location"], rows) if rows else _none("No console errors.")
    return _chips(sorted(counts.items())) + table


def _network_section(network: list[dict]) -> str:
    counts = _network_counts(network)
    failed_rows = [
        "<tr>"
        + _td(e.get("method"))
        + _td(e.get("status") if e.get("status") is not None else f"ERR {e.get('error') or 'pending'}")
        + _td(e.get("resource_type") or "-")
        + _td(e.get("url") or "-", cls="url")
        + "</tr>"
        for e in _failed(network)
    ]
    failed = _table(["method", "status", "type", "URL"], failed_rows) if failed_rows else _none("No failed requests.")
    slow_rows = [
        "<tr>"
        + _td(e.get("method"))
        + _td(e.get("status") if e.get("status") is not None else "-")
        + _td(e.get("resource_type") or "-")
        + _td(e["duration_ms"])
        + _td("-" if e.get("size") is None else f"{e['size']} B")
        + _td(e.get("url") or "-", cls="url")
        + "</tr>"
        for e in _slowest(network)
    ]
    empty = "No timed requests." if network else "No network requests."
    slow = _table(["method", "status", "type", "ms", "size", "URL"], slow_rows) if slow_rows else _none(empty)
    return _chips([(k, counts[k]) for k in NET_CLASSES]) + "<h2>Failed</h2>" + failed + "<h2>Slowest</h2>" + slow


def _page(title: str, summary: dict, zen_version: str | None, body: dict, har_name: str | None) -> str:
    stats = _chips(
        [("actions", summary.get("total", 0)), ("ok", summary.get("ok", 0)), ("errors", summary.get("errors", 0))]
    )
    har = f'<p class="meta">HAR: <a href="{_e(har_name)}">{_e(har_name)}</a></p>' if har_name else ""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        f"<h1>{_e(title)}</h1>\n"
        f'<p class="meta">run {_e(summary.get("run_id"))} · zen {_e(zen_version or "unknown")} · '
        f"{_e(summary.get('started'))} → {_e(summary.get('ended') or '-')}</p>\n"
        f"{stats}\n{har}\n"
        f'<section class="card"><h2>Timeline</h2>{body["timeline"]}</section>\n'
        f'<section class="card"><h2>Console</h2>{body["console"]}</section>\n'
        f'<section class="card"><h2>Network</h2>{body["network"]}</section>\n'
        "</body>\n</html>\n"
    )


def write_report(
    run_dir: Path,
    summary: dict,
    records: list[Record],
    console: list[dict],
    network: list[dict],
    har_path: Path | None,
    title: str | None,
    zen_version: str | None,
) -> tuple[Path, Path]:
    """Write run_dir/report.html and run_dir/report.json for one run; overwrites previous report files."""
    run_dir.mkdir(parents=True, exist_ok=True)
    title = title or f"zenpilot run {summary.get('run_id')}"
    har_name = har_path.name if har_path else None
    data = {
        "title": title,
        "zen_version": zen_version,
        "summary": summary,
        "actions": [asdict(r) for r in records],
        "console": {
            "counts": _console_counts(console),
            "errors": [e for e in console if e.get("level") == "error"][-ERRORS_MAX:],
        },
        "network": {
            "counts": _network_counts(network),
            "failed": _failed(network),
            "slowest": _slowest(network),
        },
        "har": har_name,
    }
    body = {
        "timeline": _timeline(run_dir, records),
        "console": _console_section(console),
        "network": _network_section(network),
    }
    html_path = run_dir / "report.html"
    json_path = run_dir / "report.json"
    html_path.write_text(_page(title, summary, zen_version, body, har_name), encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return html_path, json_path
