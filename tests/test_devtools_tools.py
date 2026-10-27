"""Devtools wiring in Browser and the console/network/HAR tools, against fakes."""

import base64
import contextlib
import json
from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zenpilot import browser as browser_mod
from zenpilot import server
from zenpilot.bidi import BiDiError
from zenpilot.browser import Browser, BrowserError
from zenpilot.devtools import Devtools
from zenpilot.safety import Config

EVENTS = [
    "log.entryAdded",
    "network.beforeRequestSent",
    "network.responseStarted",
    "network.responseCompleted",
    "network.fetchError",
]


class FakeBiDi:
    """Records commands; `fail(method, params)` may return an exception to raise; `replies` maps method to result."""

    instances: list = []
    fail = None
    replies: dict = {}

    def __init__(self, url):
        self.url = url
        self.connected = False
        self.sent: list[tuple[str, dict]] = []
        self.handlers: dict[str, list] = {}
        FakeBiDi.instances.append(self)

    async def connect(self):
        self.connected = True
        return {}

    async def send(self, method, params=None, timeout=30):
        params = params or {}
        self.sent.append((method, params))
        if FakeBiDi.fail and (exc := FakeBiDi.fail(method, params)):
            raise exc
        return FakeBiDi.replies.get(method, {})

    def on(self, method, callback):
        self.handlers.setdefault(method, []).append(callback)
        return lambda: self.handlers[method].remove(callback)

    def emit(self, method, params):
        for callback in list(self.handlers.get(method, [])):
            callback(params)

    async def close(self):
        self.connected = False


@pytest.fixture
def fake_bidi(monkeypatch):
    FakeBiDi.instances = []
    FakeBiDi.fail = None
    FakeBiDi.replies = {}
    monkeypatch.setattr(browser_mod, "BiDi", FakeBiDi)
    return FakeBiDi


def last_params(bidi, method):
    return [p for m, p in bidi.sent if m == method][-1]


async def test_connect_subscribes_devtools_events_and_data_collector(fake_bidi):
    b = Browser()
    await b.connect(9222)
    sent = fake_bidi.instances[0].sent
    assert [m for m, _ in sent] == ["session.subscribe", "session.subscribe", "network.addDataCollector"]
    assert sent[1][1] == {"events": EVENTS}
    assert sent[2][1] == {"dataTypes": ["response"], "maxEncodedDataSize": 2_000_000}
    assert b.devtools_error is None


async def test_devtools_events_reach_buffers(fake_bidi):
    b = Browser()
    await b.connect(9222)
    bidi = fake_bidi.instances[0]
    bidi.emit(
        "log.entryAdded",
        {"type": "console", "level": "error", "text": "boom", "timestamp": 1, "source": {"context": "c1"}},
    )
    assert [e["text"] for e in b.devtools.console("c1")] == ["boom"]


async def test_collector_failure_keeps_browser_usable(fake_bidi):
    fake_bidi.fail = lambda m, p: BiDiError("unsupported operation", "no") if m == "network.addDataCollector" else None
    b = Browser()
    await b.connect(9222)
    assert b.connected
    assert "unsupported operation" in b.devtools_error


async def test_subscribe_failure_keeps_browser_usable(fake_bidi):
    fake_bidi.fail = lambda m, p: (
        BiDiError("invalid argument", "no") if "log.entryAdded" in p.get("events", []) else None
    )
    b = Browser()
    await b.connect(9222)
    assert b.connected
    assert "invalid argument" in b.devtools_error


async def test_response_body_text_and_base64(fake_bidi):
    b = Browser()
    await b.connect(9222)
    bidi = fake_bidi.instances[0]
    fake_bidi.replies["network.getData"] = {"bytes": {"type": "string", "value": "hi"}}
    assert await b.response_body("r1") == ("hi", False)
    assert last_params(bidi, "network.getData") == {"dataType": "response", "request": "r1"}
    fake_bidi.replies["network.getData"] = {"bytes": {"type": "base64", "value": "aGk="}}
    assert await b.response_body("r1") == ("aGk=", True)


async def test_screenshot_full_page_uses_document_origin(fake_bidi):
    b = Browser()
    await b.connect(9222)
    bidi = fake_bidi.instances[0]
    fake_bidi.replies["browsingContext.captureScreenshot"] = {"data": base64.b64encode(b"img").decode()}
    tab = {"context": "c1"}
    assert await b.screenshot(tab) == b"img"
    assert "origin" not in last_params(bidi, "browsingContext.captureScreenshot")
    assert await b.screenshot(tab, full_page=True) == b"img"
    assert last_params(bidi, "browsingContext.captureScreenshot")["origin"] == "document"


def test_status_spec():
    assert server._status_spec(None) is None
    assert server._status_spec("200") == 200
    assert server._status_spec("2xx") == "2xx"
    assert server._status_spec("400-499") == "400-499"
    with pytest.raises(BrowserError):
        server._status_spec("bogus")


def test_pattern_rejects_invalid_regex():
    assert server._pattern(None) is None
    assert server._pattern("boo.").search("boom")
    with pytest.raises(BrowserError, match="regex"):
        server._pattern("(")


def test_har_default_path_under_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = server._har_path(datetime(2026, 10, 27, 12, 0, 0, 5000, tzinfo=UTC))
    assert path == tmp_path / ".local/state/zenpilot/har/20261027T120000005Z.har"


class FakeTabBrowser:
    """Stands in for Browser inside the server tools: one tab on context c1."""

    connected = True

    def __init__(self, devtools):
        self.devtools = devtools
        self.devtools_error = None
        self.bodies = {}

    @contextlib.asynccontextmanager
    async def session(self, port):
        yield

    async def tab(self, tab_id):
        return {"id": "t1", "context": "c1", "url": "http://localhost:8000/", "active": True, "title": ""}

    async def response_body(self, request_id):
        if request_id not in self.bodies:
            raise BrowserError("no body")
        return self.bodies[request_id]


@pytest.fixture
def zen(monkeypatch, tmp_path):
    cfg = Config(
        allowlist=["localhost"],
        read_only=False,
        allow_all_sites=False,
        port=9222,
        audit_log=tmp_path / "audit.jsonl",
        auto_launch=False,
    )
    monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)
    fake = FakeTabBrowser(Devtools())
    monkeypatch.setattr(server, "browser", fake)
    return fake


def console_event(text, level="info"):
    return {"type": "console", "level": level, "text": text, "timestamp": 1, "source": {"context": "c1"}}


def seed_request(devtools, rid, url, status, headers=()):
    devtools.on_before_request(
        {
            "context": "c1",
            "redirectCount": 0,
            "timestamp": 1000,
            "request": {
                "request": rid,
                "url": url,
                "method": "GET",
                "headers": list(headers),
                "destination": "document",
            },
        }
    )
    devtools.on_response_completed(
        {
            "context": "c1",
            "timestamp": 1100,
            "request": {"request": rid},
            "response": {
                "status": status,
                "statusText": "OK",
                "mimeType": "text/html",
                "headers": [],
                "bytesReceived": 5,
            },
        }
    )


async def test_read_console_filters_and_clears(zen):
    zen.devtools.on_log(console_event("boom happened", "error"))
    zen.devtools.on_log(console_event("hello"))
    out = await server.read_console_messages(pattern="boo", clear=True)
    assert "boom happened" in out
    assert "hello" not in out
    assert zen.devtools.console("c1") == []


async def test_read_console_reports_degraded_devtools(zen):
    zen.devtools_error = "unsupported operation: no"
    out = await server.read_console_messages()
    assert "unsupported operation" in out


async def test_read_console_bad_pattern_is_tool_error(zen):
    with pytest.raises(ToolError, match="regex"):
        await server.read_console_messages(pattern="(")


async def test_read_network_status_range(zen):
    seed_request(zen.devtools, "r1", "http://localhost/ok", 200)
    seed_request(zen.devtools, "r2", "http://localhost/missing", 404)
    out = await server.read_network_requests(status="4xx")
    assert "/missing" in out
    assert "/ok" not in out


async def test_network_request_redacts_and_shows_body(zen):
    seed_request(
        zen.devtools,
        "r1",
        "http://localhost/api",
        200,
        headers=[{"name": "Authorization", "value": {"type": "string", "value": "Bearer secret"}}],
    )
    zen.bodies["r1"] = ("<html>page</html>", False)
    out = await server.network_request(request_id="r1")
    assert "[redacted]" in out
    assert "secret" not in out
    assert "<html>page</html>" in out


async def test_network_request_binary_body_not_shown(zen):
    seed_request(zen.devtools, "r1", "http://localhost/img", 200)
    zen.bodies["r1"] = ("aGk=", True)
    out = await server.network_request(request_id="r1")
    assert "aGk=" not in out
    assert "binary" in out


async def test_har_export_writes_file(zen, tmp_path):
    seed_request(zen.devtools, "r1", "http://localhost/api", 200)
    zen.bodies["r1"] = ("{}", False)
    target = tmp_path / "out.har"
    out = await server.har_export(path=str(target))
    assert str(target) in out
    log = json.loads(target.read_text())["log"]
    assert len(log["entries"]) == 1
    assert log["entries"][0]["response"]["content"]["text"] == "{}"
