"""HAR 1.2 files of the requests read_network_requests lists."""

import base64
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from . import safety
from .report import _version
from .safety import Blocked

BODY_BYTES = 100_000  # of each response body, when bodies are included


def target(path: str | None, name: str) -> Path:
    """Where to save: the har folder in zenpilot's state folder, or path if it's a .har file there or in temp."""
    if path is None:
        folder = safety.STATE_DIR / "har"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.har"
    p = Path(path).expanduser()
    real = p.resolve()  # follows symlinks, so a link can't point outside the folders
    folders = [safety.STATE_DIR.resolve(), Path(tempfile.gettempdir()).resolve()]
    if not p.is_absolute() or real.suffix != ".har":
        raise Blocked(f"'{path}' isn't an absolute path to a .har file.")
    if not any(real.is_relative_to(f) for f in folders):
        raise Blocked(f"HAR files can only be saved in {folders[0]} or {folders[1]}. Leave out path for the first.")
    return real


def write(path: Path, entries: list[dict], bodies: dict[str, bytes]) -> None:
    log = {"version": "1.2", "creator": {"name": "zenpilot", "version": _version()}}
    log["entries"] = [_entry(e, bodies.get(e["id"])) for e in entries]
    with path.open("x") as f:  # never overwrites, nor writes through a link put there
        json.dump({"log": log}, f, indent=1)


def _pairs(pairs) -> list[dict]:
    return [{"name": name, "value": value} for name, value in pairs]


def _entry(e: dict, body: bytes | None) -> dict:
    ms = e["ms"] or 0
    http = (e["protocol"] or "http/1.1").upper()
    content = {"size": e["size"] or 0, "mimeType": e["mime"]}
    if body is not None:
        cut = body[:BODY_BYTES]
        try:
            content["text"] = cut.decode()
        except UnicodeDecodeError:
            content |= {"text": base64.b64encode(cut).decode(), "encoding": "base64"}
        if len(body) > BODY_BYTES:
            content["comment"] = f"Cut to the first {BODY_BYTES} of {len(body)} bytes."
    entry = {
        "startedDateTime": datetime.fromtimestamp(e["time"] / 1000, UTC).isoformat(timespec="milliseconds"),
        "time": ms,
        "request": {
            "method": e["method"],
            "url": e["url"],
            "httpVersion": http,
            "cookies": [],
            "headers": _pairs(e["headers"]),
            "queryString": _pairs(parse_qsl(urlsplit(e["url"]).query)),
            "headersSize": -1,
            "bodySize": -1,
        },
        "response": {
            "status": e["status"] or 0,
            "statusText": e["status_text"],
            "httpVersion": http,
            "cookies": [],
            "headers": _pairs(e["response_headers"]),
            "content": content,
            "redirectURL": next((v for n, v in e["response_headers"] if n.lower() == "location"), ""),
            "headersSize": -1,
            "bodySize": -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": ms, "receive": 0},
    }
    if e["error"]:
        entry["comment"] = f"Failed: {e['error']}"
    return entry
