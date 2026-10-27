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


async def test_on_handler_called_for_every_matching_event(connect):
    client, sock = await connect()
    seen = []
    client.on("browsingContext.load", seen.append)
    sock.push({"type": "event", "method": "browsingContext.load", "params": {"context": "A"}})
    sock.push({"type": "event", "method": "browsingContext.load", "params": {"context": "B"}})
    await asyncio.sleep(0.01)
    assert seen == [{"context": "A"}, {"context": "B"}]


async def test_on_handler_ignores_other_methods(connect):
    client, sock = await connect()
    seen = []
    client.on("browsingContext.load", seen.append)
    sock.push({"type": "event", "method": "browsingContext.domContentLoaded", "params": {}})
    await asyncio.sleep(0.01)
    assert seen == []


async def test_unsubscribe_stops_handler(connect):
    client, sock = await connect()
    seen = []
    unsubscribe = client.on("browsingContext.load", seen.append)
    sock.push({"type": "event", "method": "browsingContext.load", "params": {"n": 1}})
    await asyncio.sleep(0.01)
    unsubscribe()
    sock.push({"type": "event", "method": "browsingContext.load", "params": {"n": 2}})
    await asyncio.sleep(0.01)
    assert seen == [{"n": 1}]


async def test_raising_handler_does_not_block_expect_or_other_handlers(connect):
    client, sock = await connect()
    seen = []

    def boom(params):
        raise RuntimeError("handler bug")

    client.on("browsingContext.load", boom)
    client.on("browsingContext.load", seen.append)
    fut = client.expect("browsingContext.load")
    sock.push({"type": "event", "method": "browsingContext.load", "params": {"context": "A"}})
    assert (await asyncio.wait_for(fut, 1)) == {"context": "A"}
    assert seen == [{"context": "A"}]


async def test_handlers_cleared_when_connection_ends(connect):
    client, sock = await connect()
    seen = []
    client.on("browsingContext.load", seen.append)
    sock.drop()
    await asyncio.sleep(0.01)
    assert client._handlers == {}
