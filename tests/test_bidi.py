"""BiDi client against a fake WebSocket."""

import asyncio
import json

import pytest

from zenpilot import bidi as bidi_mod
from zenpilot.bidi import BiDi, BiDiError


class FakeSocket:
    """Records sent commands; replies come from `respond` or are pushed by the test."""

    def __init__(self, respond=None):
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.respond = respond or (lambda cmd: {"type": "success", "id": cmd["id"], "result": {}})
        self.closed = False

    async def send(self, raw):
        cmd = json.loads(raw)
        self.sent.append(cmd)
        reply = self.respond(cmd)
        if reply is not None:
            self.push(reply)

    def push(self, msg):
        self.incoming.put_nowait(json.dumps(msg))

    def drop(self):
        self.incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self.incoming.get()
        if raw is None:
            raise StopAsyncIteration
        return raw

    async def close(self):
        self.closed = True
        self.drop()


@pytest.fixture
def connect(monkeypatch):
    async def _connect(respond=None):
        sock = FakeSocket(respond)

        async def fake_connect(url, **kwargs):
            assert "origin" not in kwargs  # the remote agent rejects Origin headers
            return sock

        monkeypatch.setattr(bidi_mod.websockets, "connect", fake_connect)
        client = BiDi("ws://127.0.0.1:9222/session")
        await client.connect()
        return client, sock

    return _connect


async def test_connect_creates_session(connect):
    client, sock = await connect()
    assert sock.sent[0]["method"] == "session.new"
    assert client.connected


async def test_responses_matched_by_id_out_of_order(connect):
    client, sock = await connect()
    sock.respond = lambda cmd: None  # reply manually from here on
    a = asyncio.create_task(client.send("a.one"))
    b = asyncio.create_task(client.send("b.two"))
    await asyncio.sleep(0.01)
    ids = {c["method"]: c["id"] for c in sock.sent}
    sock.push({"type": "success", "id": ids["b.two"], "result": {"v": "b"}})
    sock.push({"type": "success", "id": ids["a.one"], "result": {"v": "a"}})
    assert await a == {"v": "a"}
    assert await b == {"v": "b"}


async def test_error_response_raises(connect):
    def respond(cmd):
        if cmd["method"] == "script.callFunction":
            return {"type": "error", "id": cmd["id"], "error": "no such node", "message": "gone"}
        return {"type": "success", "id": cmd["id"], "result": {}}

    client, _ = await connect(respond)
    with pytest.raises(BiDiError) as exc:
        await client.send("script.callFunction", {})
    assert exc.value.error == "no such node"


async def test_timeout(connect):
    client, sock = await connect()
    sock.respond = lambda cmd: None
    with pytest.raises(TimeoutError):
        await client.send("slow.method", timeout=0.05)


async def test_expect_matches_event_with_predicate(connect):
    client, sock = await connect()
    fut = client.expect("browsingContext.load", lambda p: p["context"] == "B")
    sock.push({"type": "event", "method": "browsingContext.load", "params": {"context": "A"}})
    sock.push({"type": "event", "method": "browsingContext.load", "params": {"context": "B"}})
    assert (await asyncio.wait_for(fut, 1))["context"] == "B"


async def test_cancelled_expectations_are_dropped(connect):
    client, sock = await connect()
    fut = client.expect("browsingContext.load")
    fut.cancel()
    sock.push({"type": "event", "method": "browsingContext.load", "params": {}})
    await asyncio.sleep(0.01)
    assert client._listeners == []


async def test_connection_loss_fails_pending_and_listeners(connect):
    client, sock = await connect()
    sock.respond = lambda cmd: None
    pending = asyncio.create_task(client.send("never.answered"))
    event = client.expect("browsingContext.load")
    await asyncio.sleep(0.01)
    sock.drop()
    with pytest.raises(ConnectionError):
        await pending
    with pytest.raises(ConnectionError):
        await event
    assert not client.connected
    with pytest.raises(ConnectionError):
        await client.send("after.close")


async def test_close_ends_session(connect):
    client, sock = await connect()
    await client.close()
    assert sock.sent[-1]["method"] == "session.end"
    assert sock.closed


async def test_browser_scripts_run_sandboxed_and_skip_console_events(connect, monkeypatch):
    """Page scripts must not be able to see zenpilot's code, or be poked by console capture.

    Subscribing to log.* makes Firefox serialize console arguments, which runs page getters:
    a known way pages detect automation. Every script runs in zenpilot's sandbox.
    """
    from zenpilot import browser as browser_mod

    def respond(cmd):
        result = {"type": "success", "result": {"type": "string", "value": "ok"}} if "script" in cmd["method"] else {}
        return {"type": "success", "id": cmd["id"], "result": result}

    sockets = []

    async def fake_connect(url, **kwargs):
        sockets.append(FakeSocket(respond))
        return sockets[-1]

    monkeypatch.setattr(bidi_mod.websockets, "connect", fake_connect)
    b = browser_mod.Browser()
    await b.connect(9222)
    await b.run("ctx", "() => 1")
    sent = sockets[0].sent
    events = [e for c in sent if c["method"] == "session.subscribe" for e in c["params"]["events"]]
    assert events and not any(e.startswith("log.") for e in events)
    calls = [c for c in sent if c["method"] == "script.callFunction"]
    assert calls and all(c["params"]["target"]["sandbox"] == browser_mod.SANDBOX for c in calls)
    await b.close()
