"""Browser operations over WebDriver BiDi: tabs, page reading and trusted input."""

import asyncio
import base64
from collections.abc import Callable
from contextlib import asynccontextmanager

import websockets

from .bidi import BiDi, BiDiError
from .tree import MAX_ITEMS, WALKER_JS, Item, Ref, Refs, render, unwrap

# BiDi errors that mean "this node or frame no longer exists".
STALE_ERRORS = {"no such node", "no such element", "stale element reference", "detached shadow root", "no such frame"}
ENTER, BACKSPACE = "\ue007", "\ue003"  # WebDriver key codes
SPECIAL_KEYS = {"\n": ENTER, "\t": "\ue004"}
# Firefox allows one BiDi session. Releasing it when idle lets Claude Desktop and
# several Claude Code sessions take turns; refs and tab ids expire with it.
IDLE_RELEASE_SECONDS = 120

# Scrolls the element into view and reports anything covering its center.
PREPARE_JS = """(el) => {
  if (!el.isConnected) return ["stale", "", ""];
  el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return ["hidden", el.tagName, ""];
  const top = el.getRootNode().elementFromPoint?.(r.left + r.width / 2, r.top + r.height / 2);
  const ok = !top || el === top || el.contains(top) || top.contains(el) || top.control === el;
  const cover = ok ? "" : top.tagName.toLowerCase() + (top.id ? "#" + top.id : "") +
    (typeof top.className === "string" && top.className.trim() ? "." + top.className.trim().split(/\\s+/).slice(0, 2).join(".") : "");
  return ["ok", el.tagName, cover];
}"""

SELECT_CONTENT_JS = """(el) => {
  if (typeof el.select === "function" && el.type !== "file") el.select();
  else if (el.isContentEditable) getSelection().selectAllChildren(el);
}"""

GET_TEXT_JS = """(max) => {
  const articles = document.querySelectorAll("article");
  const root = document.querySelector("main, [role=main]") || (articles.length === 1 ? articles[0] : document.body);
  const text = (root?.innerText || "").replace(/[ \\t]+\\n/g, "\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
  return [document.title, text.length, text.slice(0, max)];
}"""

SCROLL_INFO_JS = """() => [innerWidth, innerHeight, scrollX, scrollY,
  document.documentElement.scrollWidth, document.documentElement.scrollHeight]"""

# Scroll offsets of the window and everything under the viewport's center.
SCROLL_STATE_JS = """() => {
  const out = [scrollX, scrollY];
  for (let el = document.elementFromPoint(innerWidth / 2, innerHeight / 2); el; el = el.parentElement)
    out.push(el.scrollLeft, el.scrollTop);
  return out.join(",");
}"""

# Fallback for when wheel events don't scroll (Zen's window is hidden): scroll the
# nearest scrollable ancestor of the viewport's center, like the wheel would.
SCROLL_JS_FALLBACK = """(dx, dy) => {
  let el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  for (; el && el !== document.documentElement; el = el.parentElement) {
    const s = getComputedStyle(el);
    const canY = dy && /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight;
    const canX = dx && /(auto|scroll)/.test(s.overflowX) && el.scrollWidth > el.clientWidth;
    if (canY || canX) break;
  }
  (el && el !== document.documentElement ? el : document.scrollingElement).scrollBy({left: dx, top: dy, behavior: "instant"});
}"""


class BrowserError(Exception):
    """A failure with a message written for the model."""


class BrowserTimeout(BrowserError):
    pass


def _arg(v) -> dict:
    if isinstance(v, dict):  # {"sharedId": ...}
        return v
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "boolean", "value": v}
    if isinstance(v, (int, float)):
        return {"type": "number", "value": v}
    return {"type": "string", "value": str(v)}


def _stale(key: str) -> BrowserError:
    return BrowserError(f"Ref {key} is stale: the page changed since read_page. Call read_page again for fresh refs.")


class Browser:
    def __init__(self):
        self._bidi: BiDi | None = None
        self._lock = asyncio.Lock()
        self._next_tab = 1  # never reset, so old tab ids can't alias new tabs
        self._tab_ids: dict[str, str] = {}  # context -> "t1"
        self._contexts: dict[str, str] = {}  # "t1" -> context
        self._last_used: str | None = None  # context zenpilot last opened, switched to or navigated
        self._busy = 0
        self._idle_timer: asyncio.TimerHandle | None = None
        self.refs = Refs()

    # -- connection -------------------------------------------------------

    async def connect(self, port: int) -> None:
        async with self._lock:
            if self._bidi and self._bidi.connected:
                return
            # Firefox issues new context ids per session, so old ids and refs are void.
            self._tab_ids.clear()
            self._contexts.clear()
            self._last_used = None
            self.refs.clear()
            bidi = BiDi(f"ws://127.0.0.1:{port}/session")
            try:
                await bidi.connect()
            except (OSError, websockets.InvalidHandshake) as e:
                raise BrowserError(
                    f"Can't reach Zen's remote agent on 127.0.0.1:{port} ({e}). "
                    "Ask the user to start Zen with remote debugging (the `zenr` alias)."
                ) from None
            except BiDiError as e:
                if e.error == "session not created":
                    raise BrowserError(
                        "Zen already has an active automation session. Either another zenpilot "
                        "(Claude Desktop or Claude Code) is using it, or a previous one was killed "
                        "without ending its session; in that case Zen must be restarted."
                    ) from None
                raise
            try:
                await bidi.send("session.subscribe", {"events": [
                    "browsingContext.navigationStarted", "browsingContext.load", "browsingContext.contextCreated",
                ]})
            except BaseException:
                await bidi.close()  # don't leave an orphaned session behind
                raise
            self._bidi = bidi

    async def close(self) -> None:
        if self._bidi:
            await self._bidi.close()

    @asynccontextmanager
    async def session(self, port: int):
        """Hold the BiDi session for one tool call; release it after a quiet period."""
        if self._idle_timer:
            self._idle_timer.cancel()
        self._busy += 1
        try:
            await self.connect(port)
            yield
        finally:
            self._busy -= 1
            loop = asyncio.get_running_loop()
            self._idle_timer = loop.call_later(IDLE_RELEASE_SECONDS, lambda: loop.create_task(self._release_if_idle()))

    async def _release_if_idle(self) -> None:
        async with self._lock:
            if self._busy == 0:
                await self.close()

    async def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        try:
            return await self._bidi.send(method, params, timeout)
        except ConnectionError:
            raise BrowserError("Lost the connection to Zen. Retry; if it persists, check that Zen is running.") from None
        except TimeoutError:
            raise BrowserTimeout(f"Zen did not answer {method} within {timeout:g}s.") from None

    async def run(self, context: str, fn: str, *args, timeout: float = 15):
        """Call a JS function in a browsing context and return its unwrapped result."""
        r = await self.call("script.callFunction", {
            "functionDeclaration": fn,
            "target": {"context": context},
            "arguments": [_arg(a) for a in args],
            "awaitPromise": True,
            "resultOwnership": "none",
            "serializationOptions": {"maxDomDepth": 0},
        }, timeout)
        if r["type"] == "exception":
            raise BrowserError(f"Script error in page: {r['exceptionDetails'].get('text', '')}")
        return unwrap(r["result"])

    # -- tabs ---------------------------------------------------------------

    def _tab_id(self, context: str) -> str:
        if context not in self._tab_ids:
            tab = f"t{self._next_tab}"
            self._next_tab += 1
            self._tab_ids[context] = tab
            self._contexts[tab] = context
        return self._tab_ids[context]

    async def tabs(self, probe: bool = True) -> list[dict]:
        """Top-level tabs. With probe, also fetch titles and work out the active tab."""
        contexts = (await self.call("browsingContext.getTree", {"maxDepth": 0}))["contexts"]
        tabs = [{"id": self._tab_id(c["context"]), "context": c["context"], "url": c["url"],
                 "window": c.get("clientWindow"), "title": "", "active": False} for c in contexts]
        live = {t["context"] for t in tabs}
        for ctx in [c for c in self._tab_ids if c not in live]:
            self.refs.drop_tab(self._tab_ids[ctx])
            del self._contexts[self._tab_ids.pop(ctx)]
        if not probe or not tabs:
            return tabs

        async def state(tab):
            try:
                return await self.run(tab["context"], "() => [document.title, document.visibilityState, document.hasFocus()]", timeout=3)
            except (BrowserError, BiDiError):
                return ["", "hidden", False]

        states = await asyncio.gather(*(state(t) for t in tabs))
        windows = (await self.call("browser.getClientWindows"))["clientWindows"]
        focused_window = next((w["clientWindow"] for w in windows if w.get("active")), None)
        # Zen has no "selected tab" API. Focus and visibility identify it while the window
        # is on screen; when it's covered they go stale, so the tab zenpilot last used breaks ties.
        def rank(t, state):
            _, visibility, focus = state
            visible = visibility == "visible"
            return 4 * focus + 2 * visible + (visible and t["window"] == focused_window) + 2.5 * (t["context"] == self._last_used)

        for t, state in zip(tabs, states):
            t["title"] = state[0] or ""
        max(zip(tabs, states), key=lambda ts: rank(*ts))[0]["active"] = True
        return tabs

    async def tab(self, tab_id: str | None) -> dict:
        tabs = await self.tabs(probe=tab_id is None)
        if tab_id is None:
            if not tabs:
                raise BrowserError("Zen has no open tabs. Use new_tab.")
            return next(t for t in tabs if t["active"])
        tab = next((t for t in tabs if t["id"] == tab_id.strip().lower()), None)
        if tab is None:
            raise BrowserError(f"Unknown tab id '{tab_id}'. Call list_tabs for current ids.")
        return tab

    async def url_of(self, context: str) -> str:
        return (await self.call("browsingContext.getTree", {"root": context, "maxDepth": 0}))["contexts"][0]["url"]

    async def new_tab(self) -> str:
        # A foreground create waits for the tab to become visible, which never happens
        # while Zen's window is covered. Create in the background, then activate.
        context = (await self.call("browsingContext.create", {"type": "tab", "background": True}))["context"]
        await self.activate({"context": context})
        return self._tab_id(context)

    async def activate(self, tab: dict) -> None:
        self._last_used = tab["context"]
        try:
            # Selects the tab at once, but the reply waits on window focus and
            # visibility changes that a covered window never delivers.
            await self.call("browsingContext.activate", {"context": tab["context"]}, timeout=2)
        except BrowserTimeout:
            pass

    async def close_tab(self, tab: dict) -> None:
        await self.call("browsingContext.close", {"context": tab["context"]})
        self.refs.drop_tab(tab["id"])

    async def navigate(self, tab: dict, url: str) -> tuple[str, bool]:
        """Navigate and wait for load. Returns (final url, still_loading)."""
        self.refs.drop_tab(tab["id"])
        self._last_used = tab["context"]
        try:
            await self.call("browsingContext.navigate", {"context": tab["context"], "url": url, "wait": "complete"})
            loading = False
        except BrowserTimeout:
            loading = True
        except BiDiError as e:
            raise BrowserError(f"Navigation to {url} failed: {e.message or e.error}") from None
        return await self.url_of(tab["context"]), loading

    # -- page content -----------------------------------------------------

    async def read_page(self, tab: dict, frame_allowed: Callable[[str], str | None]) -> str:
        """Outline of the page; frame_allowed(url) returns a reason string to skip a frame."""
        self.refs.drop_tab(tab["id"])
        items: list[Item] = []
        truncated = False

        async def collect(context: str, base: int):
            nonlocal truncated
            out, trunc = await self.run(context, WALKER_JS, MAX_ITEMS, timeout=20)
            truncated |= trunc
            for depth, line, node, frame in out:
                item = Item(base + depth, line, context, node and node["sharedId"])
                items.append(item)
                if not frame:
                    continue
                try:
                    url = await self.url_of(frame["context"])
                    if reason := frame_allowed(url):
                        item.line += f" [skipped: {reason}]"
                    else:
                        await collect(frame["context"], base + depth + 1)
                except (BrowserError, BiDiError):
                    item.line += " [not readable]"

        await collect(tab["context"], 0)
        return render(items, self.refs, tab["id"], truncated)

    async def get_text(self, tab: dict, max_chars: int) -> tuple[str, str]:
        title, length, text = await self.run(tab["context"], GET_TEXT_JS, max_chars)
        if length > max_chars:
            text += f"\n[truncated: showing {max_chars} of {length} characters]"
        return title, text or "[no text]"

    async def screenshot(self, tab: dict) -> bytes:
        r = await self.call("browsingContext.captureScreenshot", {
            "context": tab["context"], "format": {"type": "image/jpeg", "quality": 0.8},
        })
        return base64.b64decode(r["data"])

    # -- input ----------------------------------------------------------------

    def ref(self, key: str) -> Ref:
        ref = self.refs.get(key)
        if ref is None:
            raise BrowserError(
                f"Unknown ref '{key}'. Refs come from the latest read_page of a tab and expire "
                "when read_page runs again or the page navigates. Call read_page again."
            )
        return ref

    def top_context(self, ref: Ref) -> str:
        return self._contexts.get(ref.tab, ref.context)

    async def ref_url(self, key: str) -> str:
        """URL of the tab a ref belongs to."""
        try:
            return await self.url_of(self.top_context(self.ref(key)))
        except BiDiError:
            raise BrowserError(f"The tab ref {key} belonged to is gone. Call list_tabs, then read_page.") from None

    async def _prepare(self, key: str, ref: Ref) -> tuple[str, str]:
        """Scroll the node into view. Returns (tagName, covering element or "")."""
        try:
            status, tag, cover = await self.run(ref.context, PREPARE_JS, {"sharedId": ref.shared_id})
        except BiDiError as e:
            if e.error in STALE_ERRORS:
                raise _stale(key) from None
            raise
        if status == "stale":
            raise _stale(key)
        if status == "hidden":
            raise BrowserError(f"Ref {key} is no longer visible. Call read_page again.")
        return tag, cover

    async def _actions(self, ref: Ref, actions: list[dict]) -> None:
        await self.call("input.performActions", {"context": ref.context, "actions": actions})

    async def _click(self, ref: Ref) -> None:
        await self._actions(ref, [{"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": [
            {"type": "pointerMove", "x": 0, "y": 0, "origin": {"type": "element", "element": {"sharedId": ref.shared_id}}},
            {"type": "pointerDown", "button": 0},
            {"type": "pointerUp", "button": 0},
        ]}])

    async def _keys(self, ref: Ref, keys: list[str]) -> None:
        for i in range(0, len(keys), 100):
            chunk = [a for k in keys[i:i + 100] for a in ({"type": "keyDown", "value": k}, {"type": "keyUp", "value": k})]
            await self._actions(ref, [{"type": "key", "id": "keyboard", "actions": chunk}])

    async def _settle(self, ref: Ref, action) -> str:
        """Run an input action; if it starts a navigation or opens a tab, wait and report it."""
        top = self.top_context(ref)
        started = self._bidi.expect("browsingContext.navigationStarted", lambda p: p["context"] == top)
        loaded = self._bidi.expect("browsingContext.load", lambda p: p["context"] == top)
        created = self._bidi.expect("browsingContext.contextCreated", lambda p: p.get("parent") is None)
        try:
            await action()
            await asyncio.wait({started, created}, timeout=0.4)
            notes = []
            if started.done() and not started.exception():
                try:
                    await asyncio.wait_for(loaded, 15)
                    notes.append(f"Page navigated to {await self.url_of(top)}")
                except TimeoutError:
                    notes.append("A navigation started and is still loading")
            if created.done() and not created.exception():
                tab = self._tab_id(created.result()["context"])
                notes.append(f"A new tab {tab} opened")
            return ". ".join(notes)
        finally:
            for fut in (started, loaded, created):
                fut.cancel()

    async def click(self, key: str) -> str:
        ref = self.ref(key)
        _, cover = await self._prepare(key, ref)
        result = f"Clicked {key}."
        if cover:
            result += f" Note: its center was covered by <{cover}>, which received the click."
        if nav := await self._settle(ref, lambda: self._click(ref)):
            result += f" {nav}."
        return result

    async def type(self, key: str, text: str, submit: bool) -> str:
        ref = self.ref(key)
        tag, _ = await self._prepare(key, ref)
        if tag == "SELECT":
            # Clicking would open the native popup; focus and type to pick an option instead.
            await self.run(ref.context, "(el) => el.focus()", {"sharedId": ref.shared_id})
        else:
            await self._click(ref)
            await self.run(ref.context, SELECT_CONTENT_JS, {"sharedId": ref.shared_id})
        await self._keys(ref, [SPECIAL_KEYS.get(ch, ch) for ch in text] or [BACKSPACE])
        result = f"Typed {len(text)} characters into {key}."
        if submit and (nav := await self._settle(ref, lambda: self._keys(ref, [ENTER]))):
            result += f" Pressed Enter. {nav}."
        elif submit:
            result += " Pressed Enter."
        return result

    async def scroll(self, tab: dict, direction: str, amount: int) -> str:
        ctx = tab["context"]
        w, h, *_ = await self.run(ctx, SCROLL_INFO_JS)
        dx, dy = {"down": (0, amount), "up": (0, -amount), "right": (amount, 0), "left": (-amount, 0)}[direction]
        before = await self.run(ctx, SCROLL_STATE_JS)
        await self.call("input.performActions", {"context": ctx, "actions": [{
            "type": "wheel", "id": "wheel",
            "actions": [{"type": "scroll", "x": int(w // 2), "y": int(h // 2), "deltaX": dx, "deltaY": dy}],
        }]})
        await asyncio.sleep(0.4)  # let smooth scrolling finish
        if await self.run(ctx, SCROLL_STATE_JS) == before:
            await self.run(ctx, SCROLL_JS_FALLBACK, dx, dy)
        _, h, x, y, sw, sh = await self.run(ctx, SCROLL_INFO_JS)
        return f"Scrolled {direction}. Viewport now at y={y:.0f} of {sh:.0f} (height {h:.0f}), x={x:.0f} of {sw:.0f}."
