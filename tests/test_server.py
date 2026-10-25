"""The MCP server process itself: shutting down on a signal on every platform."""

import asyncio
import signal

from zenpilot import server

STOP = [signal.SIGINT, signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [signal.SIGHUP])


async def test_next_signal_returns_on_each_stop_signal():
    saved = {sig: signal.getsignal(sig) for sig in STOP}
    try:
        for sig in STOP:
            waiting = asyncio.create_task(server._next_signal())
            await asyncio.sleep(0.05)  # let it install its handlers
            signal.raise_signal(sig)
            await asyncio.wait_for(waiting, 2)
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
