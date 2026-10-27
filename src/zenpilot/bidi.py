"""Minimal WebDriver BiDi client: command/response matching and event listeners."""

import asyncio
import itertools
import json
import logging
from collections.abc import Callable

import websockets

log = logging.getLogger(__name__)


class BiDiError(Exception):
    """An error response from the browser, e.g. error="no such node"."""

    def __init__(self, error: str, message: str):
        super().__init__(f"{error}: {message}")
        self.error = error
        self.message = message


class BiDi:
    def __init__(self, url: str):
        self.url = url
        self._ws = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[tuple[str, Callable[[dict], bool], asyncio.Future]] = []
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._reader: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._reader is not None and not self._reader.done()

    async def connect(self) -> dict:
        """Open the socket and create a session. Returns session.new's result."""
        # websockets sends no Origin header, which the remote agent requires.
        self._ws = await websockets.connect(self.url, max_size=None, ping_interval=None)
        self._reader = asyncio.create_task(self._read_loop())
        try:
            return await self.send("session.new", {"capabilities": {}})
        except BaseException:
            await self._ws.close()
            raise

    async def close(self) -> None:
        """End the session (Firefox keeps orphaned sessions alive) and close."""
        if not self.connected:
            return
        try:
            await self.send("session.end", timeout=2)
        except (BiDiError, ConnectionError, TimeoutError):
            pass
        await self._ws.close()

    async def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        if not self.connected:
            raise ConnectionError("not connected to the browser")
        id_ = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[id_] = fut
        try:
            await self._ws.send(json.dumps({"id": id_, "method": method, "params": params or {}}))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(id_, None)

    def expect(self, method: str, predicate: Callable[[dict], bool] = lambda p: True) -> asyncio.Future:
        """Future resolving with the params of the next matching event.

        Register it *before* triggering the action that emits the event. Cancel it
        if you stop waiting; cancelled futures are dropped on the next event.
        """
        fut = asyncio.get_running_loop().create_future()
        self._listeners.append((method, predicate, fut))
        return fut

    def on(self, method: str, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Call `callback(params)` for every event with this method. Returns an unsubscribe function."""
        self._handlers.setdefault(method, []).append(callback)

        def unsubscribe() -> None:
            handlers = self._handlers.get(method, [])
            if callback in handlers:
                handlers.remove(callback)

        return unsubscribe

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                self._dispatch(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            err = ConnectionError("connection to the browser closed")
            for fut in [*self._pending.values(), *(f for _, _, f in self._listeners)]:
                if not fut.done():
                    fut.set_exception(err)
            self._listeners.clear()
            self._handlers.clear()

    def _dispatch(self, msg: dict) -> None:
        if msg.get("type") == "event":
            method, params = msg["method"], msg["params"]
            for callback in list(self._handlers.get(method, [])):
                try:
                    callback(params)
                except Exception:
                    log.exception("BiDi handler for %s failed", method)
            keep = []
            for listener in self._listeners:
                m, pred, fut = listener
                if fut.done():
                    continue
                if m == method and pred(params):
                    fut.set_result(params)
                else:
                    keep.append(listener)
            self._listeners = keep
            return
        fut = self._pending.get(msg.get("id"))
        if fut is None or fut.done():
            return
        if msg.get("type") == "error":
            fut.set_exception(BiDiError(msg.get("error", "unknown error"), msg.get("message", "")))
        else:
            fut.set_result(msg.get("result", {}))
