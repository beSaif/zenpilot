"""Console and network buffers for tabs on the allowlist. Kept in memory; nothing is written to disk."""

import re
from collections import OrderedDict, deque
from collections.abc import Callable

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


def _status_matches(status: int | None, spec: int | str | None) -> bool:
    if spec is None:
        return True
    if status is None:
        return False
    if isinstance(spec, int):
        return status == spec
    if re.fullmatch(r"[1-5]xx", spec):
        return status // 100 == int(spec[0])
    low, high = (int(x) for x in spec.split("-"))
    return low <= status <= high


class Devtools:
    """Events from tabs whose current URL passes `allowed`. Oldest entries are dropped first.

    A tab's URL comes from navigations, so a tab that moves to a blocked site stops recording
    and loses what it recorded. Events from tabs with no known URL are dropped.
    """

    def __init__(self, allowed: Callable[[str], bool], max_console: int = 500, max_network: int = 500):
        self._allowed = allowed
        self._urls: dict[str, str] = {}  # context -> its current URL
        self._console: deque[dict] = deque(maxlen=max_console)
        self._network: OrderedDict[str, dict] = OrderedDict()
        self._hops: dict[str, int] = {}
        self._max_network = max_network

    # Tab URLs

    def set_url(self, context: str, url: str) -> None:
        """Record a tab's current URL. Leaving the allowlist drops what the tab recorded."""
        self._urls[context] = url
        if not self._recording(context):
            self.clear_console(context)
            self.clear_network(context)

    def _recording(self, context: str | None) -> bool:
        url = self._urls.get(context)
        return url is not None and self._allowed(url)

    # Event handlers

    def on_log(self, params: dict) -> None:
        context = (params.get("source") or {}).get("context")
        if not self._recording(context):
            return
        frames = (params.get("stackTrace") or {}).get("callFrames") or []
        frame = frames[0] if frames else {}
        self._console.append(
            {
                "context": context,
                "level": params.get("level"),
                "type": params.get("type"),
                "text": params.get("text"),
                "url": frame.get("url"),
                "line": frame.get("lineNumber"),
                "timestamp": params.get("timestamp"),
            }
        )

    def on_before_request(self, params: dict) -> None:
        context = params.get("context")
        req = params["request"]
        if params.get("navigation"):
            # Each redirect hop of a navigation is a request, so this follows the tab to its final URL.
            self.set_url(context, req.get("url") or "")
        if not self._recording(context):
            return
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
            "context": context,
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
        }
        self._trim()

    def on_response_started(self, params: dict) -> None:
        if (entry := self._entry(params)) is not None:
            self._fill_response(entry, params["response"])

    def on_response_completed(self, params: dict) -> None:
        if (entry := self._entry(params)) is None:
            return
        res = params["response"]
        self._fill_response(entry, res)
        entry["size"] = res.get("bytesReceived")
        self._close(entry, params.get("timestamp"))

    def on_fetch_error(self, params: dict) -> None:
        if (entry := self._entry(params)) is None:
            return
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
        return [
            e
            for e in self._network.values()
            if (context is None or e["context"] == context)
            and (url_contains is None or url_contains in (e["url"] or ""))
            and (method is None or (e["method"] or "").upper() == method.upper())
            and _status_matches(e["status"], status)
            and (resource_type is None or e["resource_type"] == resource_type)
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

    def _entry(self, params: dict) -> dict | None:
        """The request's entry, or None when its tab isn't recorded. A response with no entry gets one."""
        context = params.get("context")
        if not self._recording(context):
            return None
        rid = params["request"]["request"]
        if rid not in self._network:
            self.on_before_request(
                {
                    "context": context,
                    "timestamp": params.get("timestamp"),
                    "redirectCount": 0,
                    "request": {"request": rid, "url": None},
                }
            )
        return self._network.get(rid)

    @staticmethod
    def _fill_response(entry: dict, res: dict) -> None:
        entry["status"] = res.get("status")
        entry["status_text"] = res.get("statusText")
        entry["mime_type"] = res.get("mimeType")
        entry["response_headers"] = redact_headers(res.get("headers", []))

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
