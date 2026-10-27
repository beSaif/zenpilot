"""Console and network buffers for BiDi events, with filters, text formatting and HAR export."""

import re
from collections import OrderedDict, deque
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

REDACTED = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
REDACTED_HINTS = ("token", "secret", "api-key", "apikey", "password", "credential")
URL_MAX = 120


def redact_headers(headers: list[dict]) -> list[dict]:
    """Flatten BiDi headers to name/value strings, masking sensitive ones."""
    out = []
    for h in headers:
        value = h["value"]
        value = value.get("value", "") if isinstance(value, dict) else value
        name = h["name"].lower()
        if name in REDACTED or any(hint in name for hint in REDACTED_HINTS):
            value = "[redacted]"
        out.append({"name": h["name"], "value": str(value)})
    return out


# Names users pass for a request type that Firefox reports under another name.
_RESOURCE_ALIASES = {"stylesheet": "style"}


def _status_matches(status: int | None, spec: int | str | None) -> bool:
    if spec is None:
        return True
    if status is None:
        return False
    if isinstance(spec, int):
        return status == spec
    text = spec.strip().lower()
    if re.fullmatch(r"[1-5]xx", text):
        return status // 100 == int(text[0])
    m = re.fullmatch(r"(\d{3})-(\d{3})", text)
    if m:
        return int(m.group(1)) <= status <= int(m.group(2))
    if text.isdigit():
        return status == int(text)
    raise ValueError(f"invalid status filter: {spec!r}")


class Devtools:
    """Bounded buffers fed by BiDi event params; oldest entries are dropped first."""

    def __init__(self, max_console: int = 500, max_network: int = 500):
        self._console: deque[dict] = deque(maxlen=max_console)
        self._network: OrderedDict[str, dict] = OrderedDict()
        self._hops: dict[str, int] = {}
        self._max_network = max_network

    # Event handlers

    def on_log(self, params: dict) -> None:
        frames = (params.get("stackTrace") or {}).get("callFrames") or []
        frame = frames[0] if frames else {}
        self._console.append(
            {
                "context": (params.get("source") or {}).get("context"),
                "level": params.get("level"),
                "type": params.get("type"),
                "text": params.get("text"),
                "url": frame.get("url"),
                "line": frame.get("lineNumber"),
                "timestamp": params.get("timestamp"),
            }
        )

    def on_before_request(self, params: dict) -> None:
        req = params["request"]
        rid = req["request"]
        hops = params.get("redirectCount", 0)
        old = self._network.get(rid)
        if old is not None:
            self._network.pop(rid)
            if hops > self._hops.get(rid, 0):
                prev = f"{rid}.{self._hops[rid]}"
                old["id"] = prev
                self._network[prev] = old
        self._hops[rid] = hops
        self._network[rid] = {
            "id": rid,
            "context": params.get("context"),
            "method": req.get("method"),
            "url": req.get("url"),
            "resource_type": req.get("destination") or req.get("initiatorType") or "",
            "status": None,
            "status_text": None,
            "mime_type": None,
            "request_headers": redact_headers(req.get("headers", [])),
            "response_headers": [],
            "started": params.get("timestamp"),
            "ended": None,
            "duration_ms": None,
            "size": None,
            "error": None,
            "from_cache": None,
        }
        self._trim()

    def on_response_started(self, params: dict) -> None:
        entry = self._entry(params)
        self._fill_response(entry, params["response"])

    def on_response_completed(self, params: dict) -> None:
        entry = self._entry(params)
        res = params["response"]
        self._fill_response(entry, res)
        entry["size"] = (
            res.get("bytesReceived") if res.get("bytesReceived") is not None else (res.get("content") or {}).get("size")
        )
        self._close(entry, params.get("timestamp"))

    def on_fetch_error(self, params: dict) -> None:
        entry = self._entry(params)
        entry["error"] = params.get("errorText")
        self._close(entry, params.get("timestamp"))

    # Queries

    def console(self, context: str | None = None, level: str | None = None) -> list[dict]:
        return [
            e
            for e in self._console
            if (context is None or e["context"] == context) and (level is None or e["level"] == level)
        ]

    def requests(
        self,
        context: str | None = None,
        url_contains: str | None = None,
        method: str | None = None,
        status: int | str | None = None,
        resource_type: str | None = None,
    ) -> list[dict]:
        wanted = _RESOURCE_ALIASES.get(resource_type, resource_type) if resource_type else None
        return [
            e
            for e in self._network.values()
            if (context is None or e["context"] == context)
            and (url_contains is None or url_contains in (e["url"] or ""))
            and (method is None or (e["method"] or "").upper() == method.upper())
            and _status_matches(e["status"], status)
            and (wanted is None or e["resource_type"] == wanted)
        ]

    def request(self, request_id: str) -> dict | None:
        return self._network.get(request_id)

    def clear_console(self, context: str | None = None) -> None:
        if context is None:
            self._console.clear()
        else:
            self._console = deque((e for e in self._console if e["context"] != context), maxlen=self._console.maxlen)

    def clear_network(self, context: str | None = None) -> None:
        for key in [k for k, e in self._network.items() if context is None or e["context"] == context]:
            del self._network[key]
            self._hops.pop(key, None)

    # Internals

    def _entry(self, params: dict) -> dict:
        rid = params["request"]["request"]
        if rid not in self._network:
            self.on_before_request(
                {
                    "context": params.get("context"),
                    "timestamp": params.get("timestamp"),
                    "redirectCount": 0,
                    "request": {"request": rid, "url": None},
                }
            )
        return self._network[rid]

    @staticmethod
    def _fill_response(entry: dict, res: dict) -> None:
        entry["status"] = res.get("status")
        entry["status_text"] = res.get("statusText")
        entry["mime_type"] = res.get("mimeType")
        entry["response_headers"] = redact_headers(res.get("headers", []))
        entry["from_cache"] = res.get("fromCache")

    @staticmethod
    def _close(entry: dict, ended: int | None) -> None:
        entry["ended"] = ended
        if entry["started"] is not None and ended is not None:
            entry["duration_ms"] = ended - entry["started"]

    def _trim(self) -> None:
        while len(self._network) > self._max_network:
            key, _ = self._network.popitem(last=False)
            self._hops.pop(key, None)


def _status_text(e: dict) -> str:
    if e["error"] is not None:
        return f"ERR {e['error']}"
    return "…" if e["status"] is None else str(e["status"])


def _short(url: str | None) -> str:
    url = url or ""
    return url if len(url) <= URL_MAX else url[: URL_MAX - 1] + "…"


def format_console(entries: list[dict]) -> str:
    if not entries:
        return "No console messages."
    lines = []
    for e in entries:
        loc = ""
        if e["url"]:
            loc = f" ({e['url']}:{e['line']})" if e["line"] is not None else f" ({e['url']})"
        lines.append(f"[{e['level']}] {e['text']}{loc}")
    return "\n".join(lines)


def format_requests(entries: list[dict]) -> str:
    if not entries:
        return "No requests recorded."
    lines = []
    for e in entries:
        dur = "…" if e["duration_ms"] is None else f"{e['duration_ms']} ms"
        size = "-" if e["size"] is None else f"{e['size']} B"
        lines.append(
            f"{e['id']}  {(e['method'] or '').upper()}  {_status_text(e)}  {e['resource_type'] or '-'}  "
            f"{dur}  {size}  {_short(e['url'])}"
        )
    return "\n".join(lines)


def _iso(ms: int | None) -> str:
    if ms is None:
        return ""
    stamp = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _har_entry(e: dict, body: str | None) -> dict:
    url = e["url"] or ""
    content = {"size": e["size"] or 0, "mimeType": e["mime_type"] or ""}
    if body is not None:
        content["text"] = body
    duration = e["duration_ms"] or 0
    return {
        "startedDateTime": _iso(e["started"]),
        "time": duration,
        "request": {
            "method": e["method"] or "",
            "url": url,
            "httpVersion": "",
            "headers": [{"name": h["name"], "value": h["value"]} for h in e["request_headers"]],
            "queryString": [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True)],
            "cookies": [],
            "headersSize": -1,
            "bodySize": -1,
        },
        "response": {
            "status": e["status"] or 0,
            "statusText": e["status_text"] or "",
            "httpVersion": "",
            "headers": [{"name": h["name"], "value": h["value"]} for h in e["response_headers"]],
            "cookies": [],
            "content": content,
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": duration, "receive": 0},
    }


def har(entries: list[dict], bodies: dict[str, str] | None = None, creator_version: str = "") -> dict:
    bodies = bodies or {}
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "zenpilot", "version": creator_version},
            "entries": [_har_entry(e, bodies.get(e["id"])) for e in entries],
        }
    }
