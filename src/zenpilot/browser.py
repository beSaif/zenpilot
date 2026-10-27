"""Browser operations over WebDriver BiDi: tabs, page reading and trusted input."""

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib.resources import files

import websockets

from .bidi import BiDi, BiDiError
from .devtools import Devtools
from .tree import MAX_ITEMS, WALKER_JS, Item, Ref, Refs, render, unwrap

# BiDi errors that mean "this node or frame no longer exists".
STALE_ERRORS = {"no such node", "no such element", "stale element reference", "detached shadow root", "no such frame"}
ENTER, BACKSPACE, SHIFT = "\ue007", "\ue003", "\ue008"  # WebDriver key codes
# Shift+Enter makes a line break in chat composers too, where plain Enter would send.
SPECIAL_KEYS = {"\n": (SHIFT, ENTER), "\t": "\ue004"}
# Firefox allows one BiDi session. Releasing it when idle lets Claude Desktop and
# several Claude Code sessions take turns; refs and tab ids expire with it.
IDLE_RELEASE_SECONDS = 120
READ_RETRIES = 3  # extra read_page attempts, 0.5s apart, while the page is near-empty
# Console and network events buffered by Devtools, mapped to the Devtools handler for each.
DEVTOOLS_EVENTS = {
    "log.entryAdded": "on_log",
    "network.beforeRequestSent": "on_before_request",
    "network.responseStarted": "on_response_started",
    "network.responseCompleted": "on_response_completed",
    "network.fetchError": "on_fetch_error",
}
BODY_MAX_BYTES = 2_000_000  # response bodies above this are not kept by Zen
# A click that opens a JavaScript dialog blocks Zen's input reply; give up on it sooner than the default.
CLICK_TIMEOUT = 5
SCROLL_ROUNDS = 8  # wheel actions a scroll may take to reach its target
# Action overlay (config show_actions): drawn before click, type and hover. Cosmetic, so short and best effort.
OVERLAY_JS = (files("zenpilot") / "overlay.js").read_text()
OVERLAY_TIMEOUT = 2
SHOW_PAUSE = 0.25  # seconds the page gets to show the cursor before the action
DIALOG_HINT = "Action sent; page may be showing a dialog — use handle_dialog"
log = logging.getLogger(__name__)

# Center and size of an element in viewport pixels: [x, y, width, height].
RECT_JS = "(el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }"

# Scrolls the element into view and reports anything covering its center.
PREPARE_JS = """(el) => {
  if (!el.isConnected) return ["stale", "", "", ""];
  el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return ["hidden", el.tagName, "", ""];
  const top = el.getRootNode().elementFromPoint?.(r.left + r.width / 2, r.top + r.height / 2);
  const ok = !top || el === top || el.contains(top) || top.contains(el) || top.control === el;
  const classes = typeof top?.className === "string" ? top.className.trim().split(/\\s+/).filter(Boolean) : [];
  const cover = ok ? "" : top.tagName.toLowerCase() + (top.id ? "#" + top.id : "") +
    (classes.length ? "." + classes.slice(0, 2).join(".") : "");
  return ["ok", el.tagName, cover, el.tagName === "INPUT" ? el.type : ""];
}"""

SELECT_CONTENT_JS = """(el) => {
  if (typeof el.select === "function" && el.type !== "file") el.select();
  else if (el.isContentEditable) getSelection().selectAllChildren(el);
}"""

# Inputs whose native UI can't be typed into like a text field: Firefox splits dates and
# times into segments, and color opens a dialog. These get their value set directly.
VALUE_INPUTS = {
    "date": "YYYY-MM-DD",
    "time": "HH:MM (24-hour)",
    "datetime-local": "YYYY-MM-DDTHH:MM",
    "month": "YYYY-MM",
    "week": "YYYY-Www",
    "color": "#rrggbb",
    "range": "a number",
}

# Uses the prototype's setter so frameworks like React notice the change.
SET_VALUE_JS = """(el, v) => {
  if (el.disabled || el.readOnly) return null;
  el.focus();
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const old = el.value;
  set.call(el, v);
  if (v && !el.value) {  // invalid for this type: the browser cleared it, so put the old value back
    set.call(el, old);
    return "";
  }
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return el.value;
}"""

GET_TEXT_JS = """(max) => {
  const articles = document.querySelectorAll("article");
  const root = document.querySelector("main, [role=main]") || (articles.length === 1 ? articles[0] : document.body);
  const text = (root?.innerText || "").replace(/[ \\t]+\\n/g, "\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
  return [document.title, text.length, text.slice(0, max)];
}"""

IS_FILE_INPUT_JS = "(el) => el.isConnected && el.tagName === 'INPUT' && el.type === 'file'"

TAB_STATE_JS = "() => [document.title, document.visibilityState, document.hasFocus()]"

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
  const target = el && el !== document.documentElement ? el : document.scrollingElement;
  target.scrollBy({left: dx, top: dy, behavior: "instant"});
}"""


# Sets a control's value the way a user would, then fires input and change so page scripts see it.
FORM_INPUT_JS = """(el, v) => {
  if (!el.isConnected) return ["stale", ""];
  if (el.disabled || el.readOnly) return ["readonly", ""];
  const fire = () => {
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  };
  if (el.tagName === "SELECT") {
    const opts = [...el.options];
    const opt = opts.find(o => o.value === v) || opts.find(o => o.text.trim() === v);
    if (!opt) return ["nooption", opts.map(o => o.text.trim() || o.value).join(", ")];
    opt.selected = true;
    fire();
    return ["ok", opt.text.trim() || opt.value];
  }
  if (el.tagName === "INPUT" && (el.type === "checkbox" || el.type === "radio")) {
    if (v !== "true" && v !== "false") return ["badbool", ""];
    el.checked = v === "true";
    fire();
    return ["ok", String(el.checked)];
  }
  if (el.tagName === "INPUT" && el.type === "file") return ["unsupported", "input type=file"];
  if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
    const proto = el.tagName === "INPUT" ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
    const set = Object.getOwnPropertyDescriptor(proto, "value").set;
    const old = el.value;
    set.call(el, v);
    if (v && !el.value) {  // invalid for this type (e.g. a date): the browser cleared it, so put the old value back
      set.call(el, old);
      return ["invalid", el.type];
    }
    fire();
    return ["ok", el.value];
  }
  return ["unsupported", el.tagName.toLowerCase()];
}"""

WAIT_TEXT_JS = '(t) => (document.body?.innerText || "").includes(t)'
WAIT_VISIBLE_JS = "(el) => el.isConnected && el.getClientRects().length > 0"
WAIT_POLL_SECONDS = 0.25


MODIFIERS = {
    "ctrl": "\ue009",
    "control": "\ue009",
    "shift": SHIFT,
    "alt": "\ue00a",
    "option": "\ue00a",
    "cmd": "\ue03d",
    "command": "\ue03d",
    "meta": "\ue03d",
}
NAMED_KEYS = {
    "enter": ENTER,
    "return": ENTER,
    "tab": "\ue004",
    "escape": "\ue00c",
    "esc": "\ue00c",
    "backspace": BACKSPACE,
    "delete": "\ue017",
    "insert": "\ue016",
    "space": " ",
    "arrowup": "\ue013",
    "arrowdown": "\ue015",
    "arrowleft": "\ue012",
    "arrowright": "\ue014",
    "home": "\ue011",
    "end": "\ue010",
    "pageup": "\ue00e",
    "pagedown": "\ue00f",
    **{f"f{n}": chr(0xE031 + n - 1) for n in range(1, 13)},
}


def _key_code(name: str) -> str:
    if name == "+":
        return "+"
    lower = name.lower()
    if lower in MODIFIERS:
        return MODIFIERS[lower]
    if lower in NAMED_KEYS:
        return NAMED_KEYS[lower]
    if len(name) == 1:
        return name
    raise BrowserError(f"Unknown key '{name}'. Use names such as Enter, Tab, Escape, ArrowDown or ctrl+a.")


def parse_keys(spec: str) -> list[str | tuple[str, ...]]:
    """Space-separated keys; "ctrl+a" is a chord, returned as a tuple of key codes."""
    keys: list[str | tuple[str, ...]] = []
    for token in spec.split():
        codes = tuple(_key_code(part) for part in (["+"] if token == "+" else token.split("+")))
        keys.append(codes[0] if len(codes) == 1 else codes)
    if not keys:
        raise BrowserError("No keys given. Use names such as Enter, Tab or ctrl+a.")
    return keys


def _describe(v: dict, top: bool = True) -> str:
    """Readable text for a BiDi RemoteValue; strings are bare at top level and quoted inside."""
    t, value = v.get("type"), v.get("value")
    if t == "string":
        return value if top else json.dumps(value)
    if t == "boolean":
        return "true" if value else "false"
    if t in ("number", "bigint"):
        return f"{value}n" if t == "bigint" else str(value)
    if t == "array":
        return "[...]" if value is None else "[" + ", ".join(_describe(x, False) for x in value) + "]"
    if t == "object":
        if value is None:
            return "{...}"
        keys = (json.dumps(k) if isinstance(k, str) else _describe(k, False) for k, _ in value)
        return "{" + ", ".join(f"{key}: {_describe(x, False)}" for key, (_, x) in zip(keys, value, strict=True)) + "}"
    if t == "node":
        return f"<{(value or {}).get('localName', 'node')}>"
    if t == "regexp":
        return f"/{value.get('pattern', '')}/{value.get('flags', '')}"
    if t == "date":
        return str(value)
    if t in ("null", "undefined"):
        return t
    return f"[{t}]"


class BrowserError(Exception):
    """A failure with a message written for the model."""


class BrowserTimeout(BrowserError):
    pass


def _arg(v) -> dict:
    if isinstance(v, dict):
        if "sharedId" in v:  # already a remote reference
            return v
        # plain dict -> BiDi local value of type object (keys must be strings)
        return {"type": "object", "value": [[str(k), _arg(x)] for k, x in v.items()]}
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
    """One BiDi session at a time. Devtools buffers survive reconnects; events sent while disconnected are missed."""

    def __init__(self):
        self._bidi: BiDi | None = None
        self.devtools = Devtools()
        self.devtools_error: str | None = None  # why console/network capture is off, if it is
        self._lock = asyncio.Lock()
        self._next_tab = 1  # never reset, so old tab ids can't alias new tabs
        self._tab_ids: dict[str, str] = {}  # context -> "t1"
        self._contexts: dict[str, str] = {}  # "t1" -> context
        self._last_used: str | None = None  # context zenpilot last opened, switched to or navigated
        self._prompts: dict[str, tuple[str, str]] = {}  # context -> (type, message) of an open dialog
        self._busy = 0
        self._idle_timer: asyncio.TimerHandle | None = None
        self.refs = Refs()
        self._routes: dict[str, dict] = {}  # route id -> url, action, reply, and its intercept id on this session
        self._route_seq = 0
        self._answers: set[asyncio.Task] = set()
        # Mirrors config show_actions; server.py sets it before each tool call. Off until then.
        self.show_actions = False
        self.zen_version: str | None = None  # browserVersion from the BiDi session, once connected

    # -- connection -------------------------------------------------------

    async def connect(self, port: int) -> None:
        async with self._lock:
            if self._bidi and self._bidi.connected:
                return
            # Firefox issues new context ids per session, so old ids and refs are void.
            self._tab_ids.clear()
            self._contexts.clear()
            self._last_used = None
            self._prompts.clear()
            self.refs.clear()
            bidi = BiDi(f"ws://127.0.0.1:{port}/session")
            try:
                session = await bidi.connect()
            except (OSError, websockets.InvalidHandshake) as e:
                raise BrowserError(
                    f"Can't reach Zen's remote agent on 127.0.0.1:{port} ({e}). "
                    "Ask the user to run `zenpilot launch`, or `zenpilot doctor` to see what's wrong."
                ) from None
            except BiDiError as e:
                if e.error == "session not created":
                    raise BrowserError(
                        "Zen already has an active automation session. Either another zenpilot "
                        "(Claude Desktop or Claude Code) is using it, or a previous one was killed "
                        "without ending its session; in that case Zen must be restarted."
                    ) from None
                raise
            bidi.on("browsingContext.userPromptOpened", self._on_prompt)
            bidi.on("browsingContext.userPromptClosed", self._on_prompt_closed)
            try:
                await bidi.send(
                    "session.subscribe",
                    {
                        "events": [
                            "browsingContext.navigationStarted",
                            "browsingContext.load",
                            "browsingContext.contextCreated",
                            "browsingContext.userPromptOpened",
                            "browsingContext.userPromptClosed",
                        ]
                    },
                )
            except BaseException:
                await bidi.close()  # don't leave an orphaned session behind
                raise
            try:
                await self._enable_devtools(bidi)
                bidi.on("network.beforeRequestSent", lambda p, b=bidi: self._answer_route(b, p))
                for rid, route in list(self._routes.items()):  # intercepts die with the session
                    try:
                        route["intercept"] = await self._add_intercept(bidi, route["url"])
                    except BrowserError as e:
                        del self._routes[rid]
                        log.warning("route %s dropped on reconnect: %s", rid, e)
            except BaseException:
                await bidi.close()  # don't leave an orphaned session behind
                raise
            self.zen_version = ((session or {}).get("capabilities") or {}).get("browserVersion")
            self._bidi = bidi

    def _on_prompt(self, params: dict) -> None:
        self._prompts[params["context"]] = (params.get("type", ""), params.get("message", ""))

    def _on_prompt_closed(self, params: dict) -> None:
        self._prompts.pop(params.get("context"), None)

    async def _enable_devtools(self, bidi: BiDi) -> None:
        """Buffer console and network events. Best effort: on failure the browser stays usable without them."""
        self.devtools_error = None
        for event, handler in DEVTOOLS_EVENTS.items():
            bidi.on(event, getattr(self.devtools, handler))
        try:
            await bidi.send("session.subscribe", {"events": list(DEVTOOLS_EVENTS)})
            await bidi.send(
                "network.addDataCollector", {"dataTypes": ["response"], "maxEncodedDataSize": BODY_MAX_BYTES}
            )
        except (BiDiError, ConnectionError, TimeoutError) as e:
            self.devtools_error = str(e) or type(e).__name__
            log.warning("devtools capture unavailable: %s", self.devtools_error)

    @property
    def connected(self) -> bool:
        return bool(self._bidi and self._bidi.connected)

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
            raise BrowserError(
                "Lost the connection to Zen. Retry; if it persists, check that Zen is running."
            ) from None
        except TimeoutError:
            raise BrowserTimeout(f"Zen did not answer {method} within {timeout:g}s.") from None

    async def run(self, context: str, fn: str, *args, timeout: float = 15):
        """Call a JS function in a browsing context and return its unwrapped result."""
        r = await self.call(
            "script.callFunction",
            {
                "functionDeclaration": fn,
                "target": {"context": context},
                "arguments": [_arg(a) for a in args],
                "awaitPromise": True,
                "resultOwnership": "none",
                "serializationOptions": {"maxDomDepth": 0},
            },
            timeout,
        )
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
        tabs = [
            {
                "id": self._tab_id(c["context"]),
                "context": c["context"],
                "url": c["url"],
                "window": c.get("clientWindow"),
                "title": "",
                "active": False,
            }
            for c in contexts
        ]
        live = {t["context"] for t in tabs}
        for ctx in [c for c in self._tab_ids if c not in live]:
            self.refs.drop_tab(self._tab_ids[ctx])
            del self._contexts[self._tab_ids.pop(ctx)]
        if not probe or not tabs:
            return tabs

        async def state(tab):
            try:
                return await self.run(tab["context"], TAB_STATE_JS, timeout=3)
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
            in_focused_window = visible and t["window"] == focused_window
            return 4 * focus + 2 * visible + in_focused_window + 2.5 * (t["context"] == self._last_used)

        for t, state in zip(tabs, states, strict=True):
            t["title"] = state[0] or ""
        max(zip(tabs, states, strict=True), key=lambda ts: rank(*ts))[0]["active"] = True
        return tabs

    async def tab(self, tab_id: str | None) -> dict:
        tabs = await self.tabs(probe=tab_id is None)
        if tab_id is None:
            if not tabs:
                raise BrowserError("Zen has no open tabs. Use new_tab.")
            return next(t for t in tabs if t["active"])
        tab = next((t for t in tabs if t["id"] == tab_id.strip().lower()), None)
        if tab is None:
            raise BrowserError(
                f"Unknown tab id '{tab_id}'. Tab ids change when a tab closes or zenpilot reconnects "
                "after being idle. Call list_tabs for current ids."
            )
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
            await self.call("browsingContext.activate", {"context": tab["context"]}, timeout=1)
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
        # Single-page apps often render after the load event: give a near-empty page a moment.
        for _ in range(READ_RETRIES):
            if len(items) > 2:
                break
            await asyncio.sleep(0.5)
            items.clear()
            truncated = False
            await collect(tab["context"], 0)
        return render(items, self.refs, tab["id"], truncated)

    async def get_text(self, tab: dict, max_chars: int) -> tuple[str, str]:
        title, length, text = await self.run(tab["context"], GET_TEXT_JS, max_chars)
        if length > max_chars:
            text += f"\n[truncated: showing {max_chars} of {length} characters]"
        return title, text or "[no text]"

    async def screenshot(self, tab: dict, full_page: bool = False) -> bytes:
        params = {"context": tab["context"], "format": {"type": "image/jpeg", "quality": 0.8}}
        if full_page:  # "document" captures the whole page, not just the viewport
            params["origin"] = "document"
        r = await self.call("browsingContext.captureScreenshot", params)
        return base64.b64decode(r["data"])

    async def response_body(self, request_id: str) -> tuple[str, bool]:
        """Response body of a recorded request as (text, is_base64). Raises BiDiError if Zen didn't keep it."""
        r = await self.call("network.getData", {"dataType": "response", "request": request_id})
        return r["bytes"]["value"], r["bytes"]["type"] == "base64"

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

    async def _prepare(self, key: str, ref: Ref) -> tuple[str, str, str]:
        """Scroll the node into view. Returns (tagName, input type or "", covering element or "")."""
        try:
            status, tag, cover, kind = await self.run(ref.context, PREPARE_JS, {"sharedId": ref.shared_id})
        except BiDiError as e:
            if e.error in STALE_ERRORS:
                raise _stale(key) from None
            raise
        if status == "stale":
            raise _stale(key)
        if status == "hidden":
            raise BrowserError(f"Ref {key} is no longer visible. Call read_page again.")
        return tag, kind, cover

    async def _actions(self, ref: Ref, actions: list[dict], timeout: float = 30) -> None:
        await self.call("input.performActions", {"context": ref.context, "actions": actions}, timeout)

    async def _click(self, ref: Ref, timeout: float = 30) -> None:
        origin = {"type": "element", "element": {"sharedId": ref.shared_id}}
        await self._actions(
            ref,
            [
                {
                    "type": "pointer",
                    "id": "mouse",
                    "parameters": {"pointerType": "mouse"},
                    "actions": [
                        {"type": "pointerMove", "x": 0, "y": 0, "origin": origin},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerUp", "button": 0},
                    ],
                }
            ],
            timeout,
        )

    async def _show(self, ref: Ref, kind: str, label: str) -> None:
        """Draw the action on the page first, if show_actions is on. Best effort: never fails the action."""
        if not self.show_actions:
            return
        try:
            x, y, w, h = await self.run(ref.context, RECT_JS, {"sharedId": ref.shared_id}, timeout=OVERLAY_TIMEOUT)
            rect = {"x": x, "y": y, "w": w, "h": h}
            await self.run(ref.context, OVERLAY_JS, x + w / 2, y + h / 2, kind, label, rect, timeout=OVERLAY_TIMEOUT)
            await asyncio.sleep(SHOW_PAUSE)
        except Exception:  # cosmetic: the action goes ahead either way
            log.debug("action overlay failed", exc_info=True)

    async def _keys(self, ref: Ref, keys: list[str | tuple[str, ...]]) -> None:
        await self.press({"context": ref.context}, keys)

    async def press(self, tab: dict, keys: list[str | tuple[str, ...]]) -> None:
        """Press keys in order in a tab; a tuple is a chord, e.g. (SHIFT, ENTER)."""
        for i in range(0, len(keys), 100):
            chunk = []
            for k in keys[i : i + 100]:
                k = k if isinstance(k, tuple) else (k,)
                chunk += [{"type": "keyDown", "value": c} for c in k]
                chunk += [{"type": "keyUp", "value": c} for c in reversed(k)]
            await self.call(
                "input.performActions",
                {"context": tab["context"], "actions": [{"type": "key", "id": "keyboard", "actions": chunk}]},
            )

    async def evaluate(self, tab: dict, code: str) -> str:
        """Run a script in the tab's page and describe its result."""
        r = await self.call(
            "script.evaluate",
            {
                "expression": code,
                "target": {"context": tab["context"]},
                "awaitPromise": True,
                "resultOwnership": "none",
                "serializationOptions": {"maxObjectDepth": 5, "maxDomDepth": 0},
            },
            timeout=30,
        )
        if r["type"] == "exception":
            raise BrowserError(f"JavaScript error: {r['exceptionDetails'].get('text', 'unknown error')}")
        return _describe(r["result"])

    async def resize(self, tab: dict, width: int, height: int) -> None:
        await self.call(
            "browsingContext.setViewport",
            {"context": tab["context"], "viewport": {"width": width, "height": height}},
        )

    def dialog_note(self, context: str) -> str | None:
        """'dialog open: <type> <message>' while a JavaScript dialog waits in this context."""
        if context not in self._prompts:
            return None
        kind, message = self._prompts[context]
        return f"dialog open: {kind} {message}"

    async def handle_dialog(self, tab: dict, accept: bool, text: str | None) -> tuple[str, str]:
        """Accept or dismiss the open dialog. Returns the dialog's (type, message)."""
        params: dict = {"context": tab["context"], "accept": accept}
        if text is not None:
            params["userText"] = text
        try:
            await self.call("browsingContext.handleUserPrompt", params)
        except BiDiError as e:
            if e.error == "no such alert":
                raise BrowserError("No dialog is open in this tab.") from None
            raise
        return self._prompts.pop(tab["context"], ("", ""))

    async def file_upload(self, key: str, paths: list[str]) -> str:
        """Set the files of an <input type=file> by ref. Paths must already be checked."""
        ref = self.ref(key)
        try:
            is_file = await self.run(ref.context, IS_FILE_INPUT_JS, {"sharedId": ref.shared_id})
            if not is_file:
                raise BrowserError(f"Ref {key} is not a file input. Use a ref for <input type=file>.")
            await self._show(ref, "click", f"upload {len(paths)} file(s)")
            await self.call(
                "input.setFiles",
                {"context": ref.context, "element": {"sharedId": ref.shared_id}, "files": paths},
            )
        except BiDiError as e:
            if e.error in STALE_ERRORS:
                raise _stale(key) from None
            raise
        return f"Uploaded {len(paths)} file(s) to {key}."

    async def form_input(self, key: str, value: str) -> str:
        ref = self.ref(key)
        await self._prepare(key, ref)
        await self._show(ref, "type", "set value")
        status, detail = await self.run(ref.context, FORM_INPUT_JS, {"sharedId": ref.shared_id}, value)
        if status == "stale":
            raise _stale(key)
        if status == "readonly":
            raise BrowserError(f"Ref {key} is disabled or read-only.")
        if status == "nooption":
            raise BrowserError(f"No option of {key} matches {value!r}. Options: {detail}.")
        if status == "invalid":
            raise BrowserError(f"{value!r} is not a valid value for a {detail} input ({key}).")
        if status == "badbool":
            raise BrowserError(f"Use true or false for a checkbox, not {value!r}.")
        if status == "unsupported":
            raise BrowserError(f"Ref {key} is a {detail}, which form_input can't set. Use type, click or file_upload.")
        return f"Set {key} to {detail!r}."

    async def hover(self, key: str) -> str:
        ref = self.ref(key)
        await self._prepare(key, ref)
        await self._show(ref, "hover", "hover")
        origin = {"type": "element", "element": {"sharedId": ref.shared_id}}
        await self._actions(
            ref,
            [
                {
                    "type": "pointer",
                    "id": "mouse",
                    "parameters": {"pointerType": "mouse"},
                    "actions": [{"type": "pointerMove", "x": 0, "y": 0, "origin": origin}],
                }
            ],
        )
        return f"Hovered {key}."

    async def wait_for(
        self, tab: dict, *, text: str | None, key: str | None, url_contains: str | None, timeout_ms: int
    ) -> bool:
        """Poll until every given condition holds. Returns False on timeout."""
        ref = self.ref(key) if key is not None else None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            if await self._conditions_hold(tab, text, ref, url_contains):
                return True
            left = deadline - loop.time()
            if left <= 0:
                return False
            await asyncio.sleep(min(WAIT_POLL_SECONDS, left))

    async def _conditions_hold(self, tab: dict, text: str | None, ref: Ref | None, url_contains: str | None) -> bool:
        if text is not None and not await self.run(tab["context"], WAIT_TEXT_JS, text):
            return False
        if ref is not None:
            try:
                if not await self.run(ref.context, WAIT_VISIBLE_JS, {"sharedId": ref.shared_id}):
                    return False
            except BiDiError:  # the node is gone
                return False
        if url_contains is not None and url_contains not in await self.url_of(tab["context"]):
            return False
        return True

    # -- network routes ---------------------------------------------------

    async def _add_intercept(self, bidi: BiDi, url: str) -> str:
        try:
            r = await bidi.send(
                "network.addIntercept",
                {"phases": ["beforeRequestSent"], "urlPatterns": [{"type": "string", "pattern": url}]},
            )
        except BiDiError as e:
            raise BrowserError(f"Zen rejected the route for {url}: {e.message or e.error}") from None
        return r["intercept"]

    async def network_route(self, url: str, action: str, status: int, body: str, content_type: str) -> str:
        """Fail or answer every request to url, from now until network_unroute. Zen matches the URL exactly."""
        self._route_seq += 1
        rid = f"route{self._route_seq}"
        route = {"url": url, "action": action, "status": status, "body": body, "content_type": content_type}
        self._routes[rid] = {**route, "intercept": None}
        if self.connected:
            try:
                self._routes[rid]["intercept"] = await self._add_intercept(self._bidi, url)
            except BrowserError:
                del self._routes[rid]
                raise
        effect = "fails with a network error" if action == "abort" else f"answers {status}"
        return f"Route {rid} set: requests to {url} {effect}."

    async def network_unroute(self, route_id: str | None = None) -> str:
        if route_id is not None and route_id not in self._routes:
            raise BrowserError(f"Unknown route '{route_id}'. Routes are the ids network_route returned.")
        ids = [route_id] if route_id is not None else list(self._routes)
        if not ids:
            return "No routes to remove."
        for rid in ids:
            route = self._routes.pop(rid)
            if route["intercept"] and self.connected:
                await self.call("network.removeIntercept", {"intercept": route["intercept"]})
        return f"Removed {len(ids)} route(s)."

    def _answer_route(self, bidi: BiDi, params: dict) -> None:
        if not params.get("isBlocked"):
            return
        task = asyncio.get_running_loop().create_task(self._send_route_answer(bidi, params))
        self._answers.add(task)
        task.add_done_callback(self._answers.discard)

    async def _send_route_answer(self, bidi: BiDi, params: dict) -> None:
        request = params["request"]["request"]
        ids = set(params.get("intercepts") or [])
        route = next((r for r in self._routes.values() if r["intercept"] in ids), None)
        try:
            if route is None:
                await bidi.send("network.continueRequest", {"request": request})
            elif route["action"] == "abort":
                await bidi.send("network.failRequest", {"request": request})
            else:
                await bidi.send(
                    "network.provideResponse",
                    {
                        "request": request,
                        "statusCode": route["status"],
                        "headers": [
                            {"name": "content-type", "value": {"type": "string", "value": route["content_type"]}}
                        ],
                        "body": {"type": "string", "value": route["body"] or ""},
                    },
                )
        except (BiDiError, ConnectionError, TimeoutError) as e:
            log.warning("could not answer intercepted request %s: %s", request, e)

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
        _, _, cover = await self._prepare(key, ref)
        await self._show(ref, "click", "click")
        result = f"Clicked {key}."
        if cover:
            result += f" Note: its center was covered by <{cover}>, which received the click."
        try:
            nav = await self._settle(ref, lambda: self._click(ref, CLICK_TIMEOUT))
        except BrowserTimeout:
            # Zen doesn't answer input while a dialog is open; the click itself went through.
            hint = DIALOG_HINT
            if note := self.dialog_note(self.top_context(ref)):
                hint = f"{note}. {hint}"
            return hint
        if nav:
            result += f" {nav}."
        return result

    async def type(self, key: str, text: str, submit: bool) -> str:
        ref = self.ref(key)
        tag, kind, _ = await self._prepare(key, ref)
        await self._show(ref, "type", f"type {len(text)} chars")
        if kind in VALUE_INPUTS:
            value = await self.run(ref.context, SET_VALUE_JS, {"sharedId": ref.shared_id}, text)
            if value is None:
                raise BrowserError(f"Ref {key} is disabled or read-only.")
            if text and not value:
                raise BrowserError(f"{text!r} isn't a valid {kind} value. Use the format {VALUE_INPUTS[kind]}.")
            result = f"Set {key} to {value!r}."
        else:
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

    async def _wheel(self, ctx: str, w: float, h: float, dx: int, dy: int, before: str) -> str:
        """Send one wheel action and wait for the scroll position to settle. Returns the settled state."""
        await self.call(
            "input.performActions",
            {
                "context": ctx,
                "actions": [
                    {
                        "type": "wheel",
                        "id": "wheel",
                        "actions": [{"type": "scroll", "x": int(w // 2), "y": int(h // 2), "deltaX": dx, "deltaY": dy}],
                    }
                ],
            },
        )
        # Smooth scrolling takes a while: wait until the position settles (up to 2s),
        # or give up after 0.4s if nothing moved at all.
        state = before
        for i in range(20):
            await asyncio.sleep(0.1)
            now = await self.run(ctx, SCROLL_STATE_JS)
            if now == state and (now != before or i >= 3):
                break
            state = now
        return state

    async def scroll(self, tab: dict, direction: str, amount: int) -> str:
        ctx = tab["context"]
        w, h, *_ = await self.run(ctx, SCROLL_INFO_JS)
        sx, sy = {"down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0)}[direction]
        state = await self.run(ctx, SCROLL_STATE_JS)
        start = [float(v) for v in state.split(",")]
        axis = 0 if sx else 1  # even slots of the state are x offsets, odd slots y
        sign = sx or sy

        def moved(now: str) -> float:
            # Window and documentElement repeat the same offset, so take the largest move, not the sum.
            cur = [float(v) for v in now.split(",")]
            return max((sign * (c - s0) for c, s0 in list(zip(cur, start, strict=False))[axis::2]), default=0.0)

        # Zen applies only part of one wheel action, so repeat until the amount is covered,
        # the page stops moving, or the round cap is hit. Progress counts the inner containers too.
        for round_ in range(SCROLL_ROUNDS):
            remaining = round(amount - moved(state))
            if remaining <= 0:
                break
            dx, dy = sx * remaining, sy * remaining
            new = await self._wheel(ctx, w, h, dx, dy, state)
            if new == state:
                if round_ == 0:  # the wheel did nothing: scroll the nearest scrollable ancestor instead
                    await self.run(ctx, SCROLL_JS_FALLBACK, dx, dy)
                break
            state = new
        _, h, x, y, sw, sh = await self.run(ctx, SCROLL_INFO_JS)
        return f"Scrolled {direction}. Viewport now at y={y:.0f} of {sh:.0f} (height {h:.0f}), x={x:.0f} of {sw:.0f}."
