"""Console and network tools against a real Devtools buffer and a faked browser."""

import json
from contextlib import asynccontextmanager

import pytest

from zenpilot import server
from zenpilot.devtools import Devtools
from zenpilot.safety import Config

OK = "https://ok.com/"
MAIL = "https://mail.example.com/"


class FakeBrowser:
    connected = True
    stopped = False

    def __init__(self):
        self.urls = {"t1": OK + "form", "t2": MAIL + "inbox"}
        self.devtools = Devtools(lambda url: url.startswith("https://ok.com"))
        self.fetched: list[str] = []
        self.devtools.set_url("c1", self.urls["t1"])
        self.devtools.set_url("c2", self.urls["t2"])

    @asynccontextmanager
    async def session(self, port):
        yield

    async def tab(self, tab_id):
        tab_id = tab_id or "t1"
        context = {"t1": "c1", "t2": "c2"}[tab_id]
        return {"id": tab_id, "context": context, "url": self.urls[tab_id], "title": "", "active": tab_id == "t1"}

    async def mark(self, context, action=None):
        pass

    def ref_key(self, key):
        return None

    async def response_body(self, request_id):
        self.fetched.append(request_id)
        return "x" * 500, False


def before(context, rid, url, headers=None):
    return {
        "context": context,
        "navigation": None,
        "redirectCount": 0,
        "timestamp": 1000,
        "request": {"request": rid, "url": url, "method": "GET", "headers": headers or [], "destination": "fetch"},
    }


def completed(context, rid):
    return {
        "context": context,
        "timestamp": 1100,
        "request": {"request": rid},
        "response": {"status": 200, "statusText": "OK", "mimeType": "text/plain", "headers": [], "bytesReceived": 500},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(["ok.com"], False, False, 9222, tmp_path / "audit.jsonl", auto_launch=False)
    config.prompts = False
    config.report_dir = tmp_path / "reports"
    monkeypatch.setattr(server, "load_config", lambda: Config(**vars(config) | {"allowlist": list(config.allowlist)}))
    monkeypatch.setattr(server, "browser", FakeBrowser())
    monkeypatch.setattr(server, "_declined", {})
    monkeypatch.setattr(server, "_stop_told", False)
    return server.browser


async def test_console_of_a_blocked_tab_is_never_stored_or_read(env):
    env.devtools.on_log({"type": "console", "level": "info", "text": "bank pin 1234", "source": {"context": "c2"}})
    assert env.devtools.console() == []
    with pytest.raises(server.ToolError):
        await server.read_console_messages(tab_id="t2")


async def test_read_console_only_returns_the_allowed_tab(env):
    env.devtools.on_log({"type": "console", "level": "error", "text": "ok boom", "source": {"context": "c1"}})
    env.devtools.on_log({"type": "console", "level": "error", "text": "mail boom", "source": {"context": "c2"}})
    out = await server.read_console_messages()
    assert "ok boom" in out
    assert "mail boom" not in out


async def test_read_console_with_pattern_and_only_errors(env):
    env.devtools.on_log({"type": "console", "level": "info", "text": "hello", "source": {"context": "c1"}})
    env.devtools.on_log({"type": "console", "level": "error", "text": "hello failed", "source": {"context": "c1"}})
    out = await server.read_console_messages(pattern="fail", only_errors=True)
    assert "hello failed" in out
    assert "[info]" not in out


async def test_read_network_lists_requests_of_the_tab(env):
    env.devtools.on_before_request(before("c1", "r1", OK + "api/users"))
    env.devtools.on_response_completed(completed("c1", "r1"))
    out = await server.read_network_requests(url_pattern="api")
    assert "r1" in out
    assert "https://ok.com/api/users" in out


async def test_network_request_shows_redacted_headers(env):
    headers = [{"name": "Cookie", "value": {"type": "string", "value": "sid=1"}}]
    env.devtools.on_before_request(before("c1", "r1", OK + "api", headers=headers))
    env.devtools.on_response_completed(completed("c1", "r1"))
    out = await server.network_request(request_id="r1")
    assert "Cookie: [redacted]" in out
    assert "sid=1" not in out


async def test_network_request_fetches_body_only_for_allowlisted_entries(env):
    env.devtools.on_before_request(before("c1", "r1", OK + "api"))
    env.devtools.on_response_completed(completed("c1", "r1"))
    env.devtools.on_before_request(before("c1", "r2", "https://cdn.other.net/lib.js"))
    env.devtools.on_response_completed(completed("c1", "r2"))
    out = await server.network_request(request_id="r2")
    assert env.fetched == []
    assert "not on the allowlist" in out
    await server.network_request(request_id="r1")
    assert env.fetched == ["r1"]


async def test_network_request_truncates_the_body(env):
    env.devtools.on_before_request(before("c1", "r1", OK + "api"))
    env.devtools.on_response_completed(completed("c1", "r1"))
    out = await server.network_request(request_id="r1", max_body_chars=100)
    assert "[truncated: showing 100 of 500 characters]" in out
    assert "x" * 101 not in out


async def test_network_request_of_a_blocked_tab_is_unknown(env):
    env.devtools.on_before_request(before("c2", "r9", MAIL + "inbox"))
    with pytest.raises(server.ToolError):
        await server.network_request(request_id="r9")
    assert env.fetched == []


async def test_audit_does_not_record_bodies(env, tmp_path):
    env.devtools.on_before_request(before("c1", "r1", OK + "api"))
    env.devtools.on_response_completed(completed("c1", "r1"))
    await server.network_request(request_id="r1")
    record = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert record["tool"] == "network_request"
    assert "x" * 100 not in json.dumps(record)
