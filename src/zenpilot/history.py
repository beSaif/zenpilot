"""In-memory action history of one zenpilot run: what ran, how it ended, and a readable listing."""

import secrets
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ARG_MAX = 40  # characters kept per argument value in format_history
RUNS_DIR = Path("~/.local/state/zenpilot/runs").expanduser()
MESSAGE_MAX = 200


@dataclass
class Record:
    seq: int
    ts: str  # ISO-8601 UTC
    tool: str
    args: dict  # already redacted
    ok: bool
    message: str  # first line of the result or error
    duration_ms: int
    url: str | None = None
    screenshot: str | None = None
    batch: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class History:
    """Bounded list of Records for one run; the oldest are dropped first."""

    def __init__(self, max_records: int = 1000, now=_utc_now):
        self._now = now
        started = now()
        self.run_id = f"{started:%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
        self.started = started.isoformat(timespec="milliseconds")
        self._records: deque[Record] = deque(maxlen=max_records)
        self._seq = 0

    def add(
        self,
        tool: str,
        args: dict,
        ok: bool,
        message: str,
        duration_ms: int,
        url: str | None = None,
        screenshot: str | None = None,
        batch: bool = False,
    ) -> Record:
        self._seq += 1
        rec = Record(
            seq=self._seq,
            ts=self._now().isoformat(timespec="milliseconds"),
            tool=tool,
            args=args,
            ok=ok,
            message=message,
            duration_ms=duration_ms,
            url=url,
            screenshot=screenshot,
            batch=batch,
        )
        self._records.append(rec)
        return rec

    def records(self) -> list[Record]:
        return list(self._records)

    def last(self) -> Record | None:
        return self._records[-1] if self._records else None

    def summary(self) -> dict:
        recs = self.records()
        by_tool: dict[str, int] = {}
        for r in recs:
            by_tool[r.tool] = by_tool.get(r.tool, 0) + 1
        errors = sum(1 for r in recs if not r.ok)
        return {
            "run_id": self.run_id,
            "started": self.started,
            "ended": recs[-1].ts if recs else None,
            "total": len(recs),
            "ok": len(recs) - errors,
            "errors": errors,
            "by_tool": by_tool,
        }

    def run_dir(self, base: Path = RUNS_DIR) -> Path:
        path = base / self.run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def _arg_text(value) -> str:
    text = str(value)
    return text if len(text) <= ARG_MAX else text[:ARG_MAX] + "..."


def format_history(records: list[Record]) -> str:
    """One line per record: #seq time tool ok|ERR duration args — message."""
    if not records:
        return "No actions recorded."
    lines = []
    for r in records:
        parts = [f"#{r.seq}", datetime.fromisoformat(r.ts).strftime("%H:%M:%S"), r.tool]
        parts += ["ok" if r.ok else "ERR", f"{r.duration_ms}ms"]
        if r.url:
            parts.append(f"@{r.url}")
        parts += [f"{k}={_arg_text(v)}" for k, v in r.args.items()]
        lines.append(" ".join(parts) + f" — {r.message}")
    return "\n".join(lines)
