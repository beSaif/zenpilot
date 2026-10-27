"""Session reports: what zenpilot couldn't do in one run of the server, ready to file as a GitHub issue.

Two sources feed a report. The server records every tool call that failed for a reason other
than the user's settings, and every slow one. Claude adds what it couldn't do with the tools at
all (an upload, a widget that ignores clicks) through report_limitation, since only it knows
what it was trying to do. The file is rewritten on each new finding, so it survives a crash.
"""

import os
import platform
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote, urlencode

ISSUES_URL = "https://github.com/beSaif/zenpilot/issues/new"
SLOW_MS = 5000  # a tool call this slow is worth a look even when it worked
MAX_URL = 8000  # GitHub rejects longer new-issue links

# Query strings and fragments can hold tokens or personal data; paths are kept.
_QUERY_RE = re.compile(r"(https?://[^\s?#'\"<>]+)[?#](?:[^\s'\"<>]*[^\s'\"<>.,;:)\]])?")


def scrub(text: str) -> str:
    return _QUERY_RE.sub(r"\1?…", text)


def _version() -> str:
    try:
        return version("zenpilot")
    except PackageNotFoundError:
        return "dev"


@dataclass
class Call:
    tool: str
    domain: str | None
    ms: float
    error: str | None  # None: it worked


@dataclass
class Limitation:
    goal: str
    problem: str
    workaround: str
    domain: str | None


@dataclass
class SessionReport:
    started: datetime = field(default_factory=datetime.now)
    calls: list[Call] = field(default_factory=list)
    limitations: list[Limitation] = field(default_factory=list)
    zen_version: str | None = None
    path: Path | None = None  # set on the first save

    def record_call(self, tool: str, domain: str | None, ms: float, error: str | None) -> bool:
        """Note a tool call. Returns whether it's a finding, i.e. the report should be saved."""
        self.calls.append(Call(tool, domain, ms, error and scrub(error)))
        return error is not None or ms >= SLOW_MS

    def add_limitation(self, goal: str, problem: str, workaround: str = "") -> None:
        domain = next((c.domain for c in reversed(self.calls) if c.domain), None)
        self.limitations.append(Limitation(scrub(goal), scrub(problem), scrub(workaround), domain))

    @property
    def failed(self) -> list[Call]:
        return [c for c in self.calls if c.error]

    @property
    def slow(self) -> list[Call]:
        return [c for c in self.calls if c.ms >= SLOW_MS]

    @property
    def problems(self) -> int:
        return len(self.limitations) + sum(c.error is not None or c.ms >= SLOW_MS for c in self.calls)

    def has_findings(self) -> bool:
        return self.problems > 0

    def render(self) -> str:
        mac = platform.mac_ver()[0]
        env = [f"zenpilot {_version()}", f"macOS {mac}" if mac else platform.platform(terse=True)]
        if self.zen_version:
            env.append(f"Zen {self.zen_version}")
        env += [
            f"{self.started:%Y-%m-%d %H:%M}–{datetime.now():%H:%M}",
            f"tool calls: {len(self.calls)}, failed: {len(self.failed)}, slow: {len(self.slow)}",
        ]
        lines = ["# zenpilot session report", "", " · ".join(env)]
        if self.limitations:
            lines += ["", "## What Claude couldn't do"]
            for i, lim in enumerate(self.limitations, 1):
                lines += ["", f"### {i}. {lim.goal}" + (f" ({lim.domain})" if lim.domain else ""), "", lim.problem]
                if lim.workaround:
                    lines += ["", f"Workaround: {lim.workaround}"]
        if self.failed:
            lines += ["", "## Failed tool calls", "", "| Tool | Site | Times | Error |", "|---|---|---|---|"]
            groups: dict[tuple, int] = {}
            for c in self.failed:
                groups[(c.tool, c.domain, c.error)] = groups.get((c.tool, c.domain, c.error), 0) + 1
            for (tool, domain, error), n in groups.items():
                lines.append(f"| {tool} | {domain or ''} | {n} | {_cell(error)} |")
        if self.slow:
            lines += [
                "",
                f"## Slow tool calls (over {SLOW_MS / 1000:g}s)",
                "",
                "| Tool | Site | Time |",
                "|---|---|---|",
            ]
            for c in sorted(self.slow, key=lambda c: -c.ms)[:15]:
                lines.append(f"| {c.tool} | {c.domain or ''} | {c.ms / 1000:.1f}s |")
        if self.calls:
            lines += [
                "",
                "## All tool calls",
                "",
                "| Tool | Calls | Failed | Median | Slowest |",
                "|---|---|---|---|---|",
            ]
            by_tool: dict[str, list[Call]] = {}
            for c in self.calls:
                by_tool.setdefault(c.tool, []).append(c)
            for tool, calls in by_tool.items():
                median, slowest = statistics.median(c.ms for c in calls) / 1000, max(c.ms for c in calls) / 1000
                failed = sum(c.error is not None for c in calls)
                lines.append(f"| {tool} | {len(calls)} | {failed} | {median:.1f}s | {slowest:.1f}s |")
        return "\n".join(lines) + "\n"

    def save(self, directory: Path) -> Path | None:
        """Write the report if there's anything in it. Returns its path."""
        if not self.has_findings():
            return None
        if self.path is None:
            self.path = directory / f"{self.started:%Y-%m-%d_%H%M%S}_{os.getpid()}.md"
        directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(self.render())
        tmp.replace(self.path)
        return self.path


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def reports(directory: Path) -> list[Path]:
    """Saved reports, newest first."""
    return sorted(directory.glob("*.md"), reverse=True) if directory.exists() else []


def issue_url(text: str) -> tuple[str, bool]:
    """A link to a prefilled new-issue page. Returns (url, whether the whole report fit in it)."""
    found = [line.split(". ", 1)[1] for line in text.splitlines() if line.startswith("### ") and ". " in line]
    title = "Session report: failed or slow tool calls"
    if found:
        title = f"Couldn't: {found[0]}" + (f" (+{len(found) - 1} more)" if len(found) > 1 else "")
    url = f"{ISSUES_URL}?{urlencode({'title': title, 'body': text}, quote_via=quote)}"
    if len(url) <= MAX_URL:
        return url, True
    body = "The report was too long for the link and is on your clipboard: paste it here.\n"
    return f"{ISSUES_URL}?{urlencode({'title': title, 'body': body}, quote_via=quote)}", False
