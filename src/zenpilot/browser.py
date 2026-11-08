"""Browser operations over WebDriver BiDi: tabs, page reading and trusted input."""

import asyncio
import base64
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib.resources import files
from urllib.parse import urlsplit

import websockets

from .bidi import BiDi, BiDiError
from .safety import load_state, save_state, untrusted
from .tree import MAX_ITEMS, WALKER_JS, Item, Ref, Refs, mark_unique, render, unwrap

# BiDi errors that mean "this node or frame no longer exists".
STALE_ERRORS = {"no such node", "no such element", "stale element reference", "detached shadow root", "no such frame"}
ENTER, BACKSPACE, SHIFT = "\ue007", "\ue003", "\ue008"  # WebDriver key codes
# Shift+Enter makes a line break in chat composers too, where plain Enter would send.
SPECIAL_KEYS = {"\n": (SHIFT, ENTER), "\t": "\ue004"}
# Firefox allows one BiDi session. Releasing it when idle lets Claude Desktop and
# several Claude Code sessions take turns. Zen renames every tab and node in a new
# session, so tabs get their old ids back by URL, and refs are found again by role and name.
IDLE_RELEASE_SECONDS = 120
WAITING_SECONDS = 8  # with no tool call for this long, the badge says Claude may be waiting for the user
READ_RETRIES = 3  # extra read_page attempts, 0.5s apart, while the page is near-empty
CONSOLE_LIMIT = 200  # console entries kept per tab
MAX_ADDED, MAX_REMOVED = 15, 8  # lines of page changes shown after an action

# The badge (zenpilot's logo and name, so it isn't taken for Claude's own browser extension,
# and a Stop button) and a glow around the page in the logo's orange.
# It runs in a sandbox, so the page's scripts can't reach it or fake its button, and sits in
# a closed shadow root outside <body>, so read_page and get_text don't see it.
# The host gets the top z-index itself: as a fixed element it's a stacking context, so the
# z-indexes inside only order its own children, and a page's sticky header would cover the glow.
# state: "on", "waiting" (no tool call for a while), "stopped", "hidden" (for screenshots) or "off".
BADGE_JS = """(state, send, action) => {
  let b = globalThis.zenpilotBadge;
  if (state === "off") { b?.host.remove(); globalThis.zenpilotBadge = null; return; }
  if (!b) {
    const host = document.createElement("zenpilot-badge");
    const root = host.attachShadow({mode: "closed"});
    root.innerHTML = `<style>
      :host { all: initial !important; display: block !important; position: fixed !important;
        z-index: 2147483647 !important; width: 0; height: 0; }
      :host([hidden]) { display: none !important; }
      .edge { position: fixed; inset: 0; pointer-events: none; z-index: 2147483646;
        box-shadow: inset 0 0 4px 1px rgba(232, 96, 60, .9), inset 0 0 16px 3px rgba(232, 96, 60, .5),
          inset 0 0 44px 8px rgba(232, 96, 60, .22);
        animation: breathe 3s ease-in-out infinite; }
      .pill { position: fixed; left: 12px; bottom: 12px; z-index: 2147483647; display: flex;
        align-items: center; gap: 8px; padding: 5px 5px 5px 11px; border-radius: 999px;
        background: rgba(30, 30, 32, .94); color: #fff; box-shadow: 0 2px 12px rgba(0, 0, 0, .3);
        font: 500 12px/1.2 -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
      svg { width: 16px; height: 16px; flex: none; margin-left: -3px; }
      b { font-weight: 700; }
      .text { opacity: .8; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .text:empty { display: none; }
      .stopped { padding-right: 12px; }
      .stopped svg { filter: grayscale(1); opacity: .7; }
      .stopped ~ .edge, .stopped button { display: none; }
      .waiting ~ .edge { animation: none; opacity: .45; }
      button { all: unset; cursor: pointer; padding: 4px 10px; border-radius: 999px;
        background: rgba(255, 255, 255, .14); font-weight: 600; }
      button:hover { background: rgba(255, 255, 255, .24); }
      button:focus-visible { outline: 2px solid #e8603c; }
      @keyframes breathe { 50% { opacity: .8; } }
      @media (prefers-reduced-motion: reduce) { .edge { animation: none; } }
    </style><div class="pill" role="status">LOGO<b>zenpilot</b><span class="text" aria-live="polite"></span>
      <button type="button">Stop</button></div><div class="edge"></div>`;
    b = globalThis.zenpilotBadge = {host, pill: root.querySelector(".pill"), text: root.querySelector(".text")};
    root.querySelector("button").addEventListener("click", (e) => {
      if (e.isTrusted) b.send("stop");
    });
  }
  b.send = send;
  if (!b.host.isConnected) document.documentElement.append(b.host);
  b.host.hidden = state === "hidden";
  if (state === "hidden") return;
  b.pill.classList.toggle("stopped", state === "stopped");
  b.pill.classList.toggle("waiting", state === "waiting");
  if (state === "stopped") b.text.textContent = "Stopped";
  else if (state === "waiting") b.text.textContent = "Claude may be waiting for you";
  else if (action != null) b.text.textContent = action;
}""".replace("LOGO", (files("zenpilot") / "logo.svg").read_text().strip())

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

# The option to pick in a <select>, an ARIA combobox's popup or a listbox: an exact match,
# else the only one starting with, else containing, the wanted text.
FIND_OPTION_JS = """(el, want) => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const label = (o) => norm(o.getAttribute("aria-label") || o.label || o.innerText || o.textContent);
  let opts;
  if (el.tagName === "SELECT") opts = [...el.options];
  else {
    const ids = [el, ...el.querySelectorAll("[role=combobox]")].flatMap((e) =>
      `${e.getAttribute("aria-controls") || ""} ${e.getAttribute("aria-owns") || ""}`.split(/\\s+/));
    const lists = [el, ...ids.filter(Boolean).map((id) => el.getRootNode().getElementById?.(id))].filter(Boolean);
    const shown = (o) => o.checkVisibility({ checkVisibilityCSS: true }) && o.getBoundingClientRect().height > 0;
    opts = lists.flatMap((l) => [...l.querySelectorAll("[role=option]")]).filter(shown);
    // A popup without aria-controls: the options on screen now are the open one's.
    if (!opts.length) opts = [...document.querySelectorAll("[role=option]")].filter(shown);
  }
  const w = norm(want).toLowerCase();
  const matches = [(l) => l === w, (l) => l.startsWith(w), (l) => l.includes(w)]
    .map((f) => opts.filter((o) => f(label(o).toLowerCase())))
    .find((m) => m.length);
  const names = (matches || opts).slice(0, 15).map(label);
  const status = !matches ? "none" : matches.length === 1 || label(matches[0]).toLowerCase() === w ? "ok" : "ambiguous";
  return [status, status === "ok" ? matches[0] : null, names, opts.length];
}"""

PICK_NATIVE_JS = """(el, opt) => {
  if (el.disabled) return false;
  el.focus();
  opt.selected = true;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return true;
}"""

# What a select, combobox or listbox shows as chosen, and whether its list is still open.
CHOSEN_JS = """(el) => {
  if (el.tagName === "SELECT") return [el.selectedOptions[0]?.text.trim() || "", false];
  const box = el.matches("input, textarea") ? el : el.querySelector("input, textarea");
  const chosen = box ? box.value
    : el.getAttribute("role") === "listbox" ? el.querySelector("[aria-selected=true]")?.innerText : el.innerText;
  const open = [el, box].some((e) => e?.getAttribute("aria-expanded") === "true");
  return [(chosen || "").replace(/\\s+/g, " ").trim(), open];
}"""

# The file input a ref stands for: itself, one inside it, or its label's.
FILE_INPUT_JS = """(el) => {
  if (!el.isConnected) return ["stale"];
  const f = el.matches("input[type=file]") ? el
    : el.querySelector("input[type=file]") || el.control || el.closest("label")?.control;
  if (!f || f.type !== "file") return ["none"];
  return [f.disabled ? "disabled" : "ok", f, f.multiple];
}"""

FILE_NAMES_JS = "(f) => [...f.files].map((x) => x.name)"

RECT_JS = "(el) => JSON.stringify(el.getBoundingClientRect())"

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


class BrowserError(Exception):
    """A failure with a message written for the model."""


class BrowserTimeout(BrowserError):
    pass


class StaleRef(BrowserError):
    """The ref's element is gone or hidden; it may be found again by role and name."""


class FilesRefused(BrowserError):
    """Zen can't create File objects for input.setFiles (zen.UPLOAD_PREF is off)."""


NO_WINDOW = (
    "Zen is running but has no open window, so there's no tab to use or open. Ask the user to open "
    "a Zen window (click Zen in the Dock), then retry."
)


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
    return StaleRef(f"Ref {key} is stale: the page changed since read_page. Call read_page again for fresh refs.")


def _site(url: str) -> str:
    return urlsplit(url).hostname or ""


class Browser:
    def __init__(self):
        self._bidi: BiDi | None = None
        self._lock = asyncio.Lock()
        self._next_tab = 1  # never reset (and shared through the state file), so old tab ids can't alias new tabs
        self._tab_ids: dict[str, str] = {}  # context -> "t1"
        self._contexts: dict[str, str] = {}  # "t1" -> context
        self._orphans: list[list[str]] = []  # [tab id, url] from the last session, matched on the next getTree
        self._state = ""  # state file with the tab ids, per port
        self._saved: dict = {}  # {"next": 5, "tabs": [[tab id, url], ...]} as last seen
        self._session = 0
        self._last_used: str | None = None  # tab zenpilot last opened, switched to or navigated
        self._busy = 0
        self._idle_timer: asyncio.TimerHandle | None = None
        self._waiting_timer: asyncio.TimerHandle | None = None
        self.refs = Refs()
        self._outlines: dict[str, list[str]] = {}  # context -> its lines at the latest read, to report changes
        self._badges: set[str] = set()  # contexts showing the badge
        self._acted = 0.0  # when the last input action ended
        self._busy_input = False  # an input action is running
        self.stopped = False  # the user pressed Stop on the badge
        self._console: dict[str, deque] = {}  # context -> its latest console messages and errors

    # -- connection -------------------------------------------------------

    async def connect(self, port: int) -> None:
        async with self._lock:
            if self._bidi and self._bidi.connected:
                return
            self._new_session(port)
            bidi = BiDi(f"ws://127.0.0.1:{port}/session")
            try:
                await bidi.connect()
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
            try:
                await bidi.send(
                    "session.subscribe",
                    {
                        "events": [
                            "browsingContext.navigationStarted",
                            "browsingContext.load",
                            "browsingContext.contextCreated",
                            "script.message",
                            "log.entryAdded",
                        ]
                    },
                )
            except BaseException:
                await bidi.close()  # don't leave an orphaned session behind
                raise
            bidi.on("script.message", self._on_message)
            bidi.on("log.entryAdded", self._on_log)
            self._bidi = bidi

    def _new_session(self, port: int) -> None:
        """Forget this session's context ids; tabs are matched to their ids on the next getTree."""
        # Another zenpilot may have used Zen since; its view of the tabs is the latest.
        self._state = f"tabs-{port}"
        self._saved = load_state(self._state) or self._saved
        self._orphans = list(self._saved.get("tabs", []))
        self._next_tab = max(self._next_tab, self._saved.get("next", 1))
        self._tab_ids.clear()
        self._contexts.clear()
        self._outlines.clear()
        self._badges.clear()
        self._session += 1

    @property
    def connected(self) -> bool:
        return bool(self._bidi and self._bidi.connected)

    async def close(self) -> None:
        if self._bidi:
            if self._bidi.connected:
                for context in list(self._badges):
                    await self._badge(context, "off")
                try:
                    await self.tabs(probe=False)  # save the tabs' latest URLs, to match them by next time
                except (BrowserError, BiDiError):
                    pass
            await self._bidi.close()
        self._console.clear()  # the next session only sees messages logged from now on

    @asynccontextmanager
    async def session(self, port: int):
        """Hold the BiDi session for one tool call; release it after a quiet period."""
        for timer in (self._idle_timer, self._waiting_timer):
            if timer:
                timer.cancel()
        self._busy += 1
        try:
            await self.connect(port)
            yield
        finally:
            self._busy -= 1
            loop = asyncio.get_running_loop()
            self._idle_timer = loop.call_later(IDLE_RELEASE_SECONDS, lambda: loop.create_task(self._release_if_idle()))
            self._waiting_timer = loop.call_later(WAITING_SECONDS, lambda: loop.create_task(self._show_waiting()))

    async def _show_waiting(self) -> None:
        """Between calls, Claude may be asking the user something in its own app; say so on the badge."""
        if self._busy == 0 and not self.stopped and self.connected:
            for context in list(self._badges):
                await self._badge(context, "waiting")

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

    # -- badge --------------------------------------------------------------

    async def _badge(self, context: str, state: str, action: str | None = None) -> None:
        """Show, change or remove the badge. It's only a sign for the user, so failures are ignored."""
        try:
            await self.call(
                "script.callFunction",
                {
                    "functionDeclaration": BADGE_JS,
                    "target": {"context": context, "sandbox": "zenpilot"},
                    "arguments": [_arg(state), {"type": "channel", "value": {"channel": "zenpilot"}}, _arg(action)],
                    "awaitPromise": False,
                    "resultOwnership": "none",
                },
                timeout=3,
            )
        except (BrowserError, BiDiError):
            pass
        if state == "off":
            self._badges.discard(context)
        else:
            self._badges.add(context)

    async def mark(self, context: str, action: str | None = None) -> None:
        """Show the badge on a tab zenpilot uses, saying what it's doing (None keeps the last words)."""
        await self._badge(context, "stopped" if self.stopped else "on", action)

    def _on_message(self, params: dict) -> None:
        if params.get("channel") != "zenpilot" or unwrap(params["data"]) != "stop":
            return
        # zenpilot's own click can land on the badge when it covers the target; that's not the user.
        if self._busy_input or time.monotonic() - self._acted < 1:
            return
        self.stopped = True
        for context in list(self._badges):
            asyncio.get_running_loop().create_task(self._badge(context, "stopped"))

    def _on_log(self, params: dict) -> None:
        context = (params.get("source") or {}).get("context")
        if not context:
            return
        frames = (params.get("stackTrace") or {}).get("callFrames") or [{}]
        entry = {
            "level": params.get("level", ""),
            "type": params.get("type", ""),
            "text": str(params.get("text", "")),
            "url": frames[0].get("url", ""),
            "line": frames[0].get("lineNumber"),
            "time": params.get("timestamp"),
        }
        self._console.setdefault(context, deque(maxlen=CONSOLE_LIMIT)).append(entry)

    def console(self, tab: dict, level: str | None = None, limit: int = 50, clear: bool = False) -> list[dict]:
        """The tab's latest entries, oldest first, optionally one level only. clear empties its buffer."""
        entries = self._console.get(tab["context"], deque())
        picked = [e for e in entries if level is None or e["level"] == level][-limit:]
        if clear:
            self._console.pop(tab["context"], None)
        return picked

    # -- tabs ---------------------------------------------------------------

    def _tab_id(self, context: str, tab: str | None = None) -> str:
        if context not in self._tab_ids:
            if tab is None:
                tab = f"t{self._next_tab}"
                self._next_tab += 1
            self._tab_ids[context] = tab
            self._contexts[tab] = context
        return self._tab_ids[context]

    def _adopt(self, contexts: list[dict]) -> None:
        """Give tabs from before a reconnect their old ids: same URL first, then same site, in tab order."""
        orphans, self._orphans = self._orphans, []
        for same in (lambda a, b: a == b, lambda a, b: _site(a) != "" and _site(a) == _site(b)):
            for c in contexts:
                match = c["context"] not in self._tab_ids and next((o for o in orphans if same(o[1], c["url"])), None)
                if match:
                    orphans.remove(match)
                    self._tab_id(c["context"], match[0])

    async def tabs(self, probe: bool = True) -> list[dict]:
        """Top-level tabs. With probe, also fetch titles and work out the active tab."""
        contexts = (await self.call("browsingContext.getTree", {"maxDepth": 0}))["contexts"]
        if self._orphans:
            self._adopt(contexts)
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
        state = {"next": self._next_tab, "tabs": [[t["id"], t["url"]] for t in tabs]}
        if state != self._saved:
            self._saved = state
            try:
                save_state(self._state, state)
            except OSError:
                pass  # ids still survive reconnects, just not restarts
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
            return 4 * focus + 2 * visible + in_focused_window + 2.5 * (t["id"] == self._last_used)

        for t, state in zip(tabs, states, strict=True):
            t["title"] = state[0] or ""
        max(zip(tabs, states, strict=True), key=lambda ts: rank(*ts))[0]["active"] = True
        return tabs

    async def tab(self, tab_id: str | None) -> dict:
        tabs = await self.tabs(probe=tab_id is None)
        if tab_id is None:
            if not tabs:
                raise BrowserError(NO_WINDOW)
            return next(t for t in tabs if t["active"])
        tab = next((t for t in tabs if t["id"] == tab_id.strip().lower()), None)
        if tab is None:
            raise BrowserError(f"Unknown tab id '{tab_id}': that tab was closed. Call list_tabs for current ids.")
        return tab

    async def url_of(self, context: str) -> str:
        return (await self.call("browsingContext.getTree", {"root": context, "maxDepth": 0}))["contexts"][0]["url"]

    async def new_tab(self) -> str:
        # A foreground create waits for the tab to become visible, which never happens
        # while Zen's window is covered. Create in the background, then activate.
        try:
            context = (await self.call("browsingContext.create", {"type": "tab", "background": True}))["context"]
        except BiDiError as e:
            if "getTabBrowser" in e.message:  # Firefox's remote agent fails like this with no browser window
                raise BrowserError(NO_WINDOW) from None
            raise
        tab = self._tab_id(context)
        await self.activate({"id": tab, "context": context})
        return tab

    async def activate(self, tab: dict) -> None:
        self._last_used = tab["id"]
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
        self._last_used = tab["id"]
        try:
            await self.call("browsingContext.navigate", {"context": tab["context"], "url": url, "wait": "complete"})
            loading = False
        except BrowserTimeout:
            loading = True
        except BiDiError as e:
            raise BrowserError(f"Navigation to {url} failed: {e.message or e.error}") from None
        return await self.url_of(tab["context"]), loading

    # -- page content -----------------------------------------------------

    async def _walk(self, context: str) -> tuple[list[tuple[Item, dict | None]], bool]:
        """One frame's outline: (item, child frame or None) pairs, and whether it was cut short."""
        out, truncated = await self.run(context, WALKER_JS, MAX_ITEMS, timeout=20)
        items = [(Item(d, line, context, node and node["sharedId"], key), frame) for d, line, node, frame, key in out]
        return items, truncated

    async def read_page(self, tab: dict, frame_allowed: Callable[[str], str | None]) -> str:
        """Outline of the page; frame_allowed(url) returns a reason string to skip a frame."""
        self.refs.drop_tab(tab["id"])
        items: list[Item] = []
        truncated = False

        async def collect(context: str, base: int):
            nonlocal truncated
            out, trunc = await self._walk(context)
            self._outlines[context] = [item.line for item, _ in out]
            truncated |= trunc
            for item, frame in out:
                item.depth += base
                items.append(item)
                if not frame:
                    continue
                try:
                    url = await self.url_of(frame["context"])
                    if reason := frame_allowed(url):
                        item.line += f" [skipped: {reason}]"
                    else:
                        await collect(frame["context"], item.depth + 1)
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
        mark_unique(items, tab["context"])
        url = await self.url_of(tab["context"])  # where the refs were read, to find them again there only
        return render(items, self.refs, tab["id"], truncated, url, self._session)

    async def get_text(self, tab: dict, max_chars: int) -> tuple[str, str]:
        title, length, text = await self.run(tab["context"], GET_TEXT_JS, max_chars)
        if length > max_chars:
            text += f"\n[truncated: showing {max_chars} of {length} characters]"
        return title, text or "[no text]"

    async def screenshot(self, tab: dict) -> bytes:
        await self._badge(tab["context"], "hidden")
        try:
            r = await self.call(
                "browsingContext.captureScreenshot",
                {
                    "context": tab["context"],
                    "format": {"type": "image/jpeg", "quality": 0.8},
                },
            )
        finally:
            await self.mark(tab["context"])
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

    def ref_key(self, key: str) -> str | None:
        """The ref's role and name, for the audit log."""
        ref = self.refs.get(key)
        return ref.key if ref else None

    def top_context(self, ref: Ref) -> str:
        return self._contexts.get(ref.tab, ref.context)

    async def ref_url(self, key: str) -> str:
        """URL of the tab a ref belongs to."""
        ref = self.ref(key)
        if ref.tab not in self._contexts:
            await self.tabs(probe=False)  # first call after a reconnect: match tabs to their ids
        try:
            return await self.url_of(self._contexts[ref.tab])
        except (KeyError, BiDiError):
            raise BrowserError(f"The tab ref {key} belonged to is gone. Call list_tabs, then read_page.") from None

    async def _found_again(self, key: str, ref: Ref) -> Ref | None:
        """Find a ref's element again by its role and name, if it's the only one on the same page."""
        if ref.tab not in self._contexts:
            await self.tabs(probe=False)
        context = self._contexts.get(ref.tab)
        if not ref.url or not context or (await self.url_of(context)).split("#")[0] != ref.url.split("#")[0]:
            return None
        same = [it for it, _ in (await self._walk(context))[0] if it.key == ref.key]
        if len(same) != 1:
            return None
        ref = replace(ref, context=context, shared_id=same[0].shared_id, session=self._session)
        self.refs.replace(key, ref)
        return ref

    async def _target[T](self, key: str, check: Callable[[str, Ref], Awaitable[T]]) -> tuple[Ref, T, str]:
        """The ref's element, found again by role and name if needed. Returns (ref, check's result, note)."""
        ref = self.ref(key)
        if ref.session == self._session:
            try:
                return ref, await check(key, ref), ""
            except StaleRef:
                if not (ref := await self._found_again(key, ref)):
                    raise
        elif not (ref := await self._found_again(key, ref)):
            raise BrowserError(
                f"Ref {key} is from before zenpilot reconnected to Zen and can't be found again "
                "by role and name. Call read_page again."
            )
        return ref, await check(key, ref), f" ({key} was found again as {ref.key})"

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
            raise StaleRef(f"Ref {key} is no longer visible. Call read_page again.")
        return tag, kind, cover

    async def _actions(self, ref: Ref, actions: list[dict]) -> None:
        self._busy_input = True
        try:
            await self.call("input.performActions", {"context": ref.context, "actions": actions})
        finally:
            self._busy_input, self._acted = False, time.monotonic()

    async def _click(self, ref: Ref) -> None:
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
        )

    async def _keys(self, ref: Ref, keys: list[str | tuple[str, ...]]) -> None:
        """Press keys in order; a tuple is a chord, e.g. (SHIFT, ENTER)."""
        for i in range(0, len(keys), 100):
            chunk = []
            for k in keys[i : i + 100]:
                k = k if isinstance(k, tuple) else (k,)
                chunk += [{"type": "keyDown", "value": c} for c in k]
                chunk += [{"type": "keyUp", "value": c} for c in reversed(k)]
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

    async def _changes(self, ref: Ref) -> str:
        """What changed in the ref's frame since it was last read, with refs for new controls."""
        old = self._outlines.get(ref.context)
        if old is None:
            return ""
        try:
            items = [item for item, _ in (await self._walk(ref.context))[0]]
            url = await self.url_of(ref.context)
        except (BrowserError, BiDiError):
            return ""
        self._outlines[ref.context] = [it.line for it in items]
        mark_unique(items, self.top_context(ref))
        left, now = Counter(old), Counter(it.line for it in items)
        added = []
        for it in items:
            if left[it.line]:
                left[it.line] -= 1
            else:
                added.append(it)
        removed = []
        for line in old:
            if now[line]:
                now[line] -= 1
            else:
                removed.append(line)
        if not added and not removed:
            return "\nThe page outline didn't change (yet)."
        lines = []
        for it in added[:MAX_ADDED]:
            if it.shared_id:
                new = Ref(ref.tab, it.context, it.shared_id, it.key or "", url if it.unique else "", self._session)
                lines.append(f"+ {self.refs.add(new)} {it.line}")
            else:
                lines.append(f"+ {it.line}")
        lines += [f"- {line}" for line in removed[:MAX_REMOVED]]
        if more := max(0, len(added) - MAX_ADDED) + max(0, len(removed) - MAX_REMOVED):
            lines.append(f"[{more} more changes; read_page shows the whole page]")
        return "\nPage changes:\n" + untrusted("\n".join(lines), url)

    async def click(self, key: str) -> str:
        ref, (_, _, cover), note = await self._target(key, self._prepare)
        result = f"Clicked {key}{note}."
        if cover:
            result += f" Note: its center was covered by <{cover}>, which received the click."
        if nav := await self._settle(ref, lambda: self._click(ref)):
            return result + f" {nav}."
        return result + await self._changes(ref)

    async def type(self, key: str, text: str, submit: bool) -> str:
        ref, (tag, kind, _), note = await self._target(key, self._prepare)
        if kind in VALUE_INPUTS:
            value = await self.run(ref.context, SET_VALUE_JS, {"sharedId": ref.shared_id}, text)
            if value is None:
                raise BrowserError(f"Ref {key} is disabled or read-only.")
            if text and not value:
                raise BrowserError(f"{text!r} isn't a valid {kind} value. Use the format {VALUE_INPUTS[kind]}.")
            result = f"Set {key}{note} to {value!r}."
        else:
            if tag == "SELECT":
                # Clicking would open the native popup; focus and type to pick an option instead.
                await self.run(ref.context, "(el) => el.focus()", {"sharedId": ref.shared_id})
            else:
                await self._click(ref)
                await self.run(ref.context, SELECT_CONTENT_JS, {"sharedId": ref.shared_id})
            await self._keys(ref, [SPECIAL_KEYS.get(ch, ch) for ch in text] or [BACKSPACE])
            result = f"Typed {len(text)} characters into {key}{note}."
            if ref.key.startswith("combobox"):
                result += " If it offers a list, use select to pick an option; typing alone may not choose it."
        if submit and (nav := await self._settle(ref, lambda: self._keys(ref, [ENTER]))):
            return result + f" Pressed Enter. {nav}."
        if submit:
            result += " Pressed Enter."
        await asyncio.sleep(0.3)  # suggestion lists and validation messages show up as you type
        return result + await self._changes(ref)

    async def _find_option(self, ref: Ref, option: str, wait: float) -> tuple[str, dict | None, list[str], int]:
        """Look for the option, for up to wait seconds while the list opens or filters."""
        for i in range(int(wait / 0.25) + 1):
            if i:
                await asyncio.sleep(0.25)
            found = await self.run(ref.context, FIND_OPTION_JS, {"sharedId": ref.shared_id}, option)
            if found[0] != "none":
                break
        return found

    async def select(self, key: str, option: str) -> str:
        ref, (tag, _, _), note = await self._target(key, self._prepare)
        el = {"sharedId": ref.shared_id}
        found = await self._find_option(ref, option, 0)
        if tag != "SELECT" and found[0] == "none":
            await self._click(ref)  # opens it
            found = await self._find_option(ref, option, 1)
            if found[0] == "none" and tag in ("INPUT", "TEXTAREA"):
                await self.run(ref.context, SELECT_CONTENT_JS, el)  # type to filter, as a person would
                await self._keys(ref, list(option))
                found = await self._find_option(ref, option, 3)
        status, opt, names, total = found
        if status == "none":
            listed = f" Its options include: {', '.join(map(repr, names))}." if names else " No options showed up."
            raise BrowserError(f"No option matching {option!r} in {key}.{listed}")
        if status == "ambiguous":
            raise BrowserError(f"{option!r} matches several options in {key}: {', '.join(map(repr, names))}. Be exact.")
        nav = ""
        if tag == "SELECT":
            if not await self.run(ref.context, PICK_NATIVE_JS, el, opt):
                raise BrowserError(f"Ref {key} is disabled.")
        else:
            opt_ref = replace(ref, shared_id=opt["sharedId"])
            await self._wait_still(key, opt_ref)  # a click on a popup that's still animating open can miss
            nav = await self._settle(ref, lambda: self._click(opt_ref))
        chosen, open_ = await self._chosen(ref)
        # Typeahead fills a combobox's text before anything is chosen, so it only counts once the list closes.
        if not open_ and (names[0].lower() in chosen.lower() or (chosen and chosen.lower() in names[0].lower())):
            result = f"Selected {names[0]!r} in {key}{note}."
        else:
            state = "its list is still open" if open_ else f"it shows {chosen!r}"
            result = (
                f"Clicked the option {names[0]!r} in {key}{note}, but {state}, so the choice may not have been "
                "committed. Check with read_page or screenshot."
            )
        return result + (f" {nav}." if nav else await self._changes(ref))

    async def _wait_still(self, key: str, ref: Ref) -> None:
        """Scroll an option into view once, then wait (up to 2s) until it stops moving."""
        await self._prepare(key, ref)
        last = None
        for _ in range(10):
            rect = await self.run(ref.context, RECT_JS, {"sharedId": ref.shared_id})
            if rect == last:
                return
            last = rect
            await asyncio.sleep(0.2)

    async def _chosen(self, ref: Ref) -> tuple[str, bool]:
        """What the dropdown shows as chosen, once its list closes (waiting up to 2s), and whether it's open."""
        for _ in range(8):
            await asyncio.sleep(0.25)
            chosen, open_ = await self.run(ref.context, CHOSEN_JS, {"sharedId": ref.shared_id})
            if not open_:
                break
        return chosen, open_

    async def _file_input(self, key: str, ref: Ref) -> dict:
        status, *rest = await self.run(ref.context, FILE_INPUT_JS, {"sharedId": ref.shared_id})
        if status == "stale":
            raise _stale(key)
        if status == "none":
            raise BrowserError(f"Ref {key} isn't a file input, and has none inside it or in its label.")
        if status == "disabled":
            raise BrowserError(f"The file input {key} is disabled.")
        return {"input": rest[0], "multiple": rest[1]}

    async def upload(self, key: str, files: list[str]) -> str:
        ref, f, note = await self._target(key, self._file_input)
        if len(files) > 1 and not f["multiple"]:
            raise BrowserError(f"The file input {key} takes one file.")
        try:
            await self.call("input.setFiles", {"context": ref.context, "element": f["input"], "files": files})
        except BiDiError as e:
            if "InvalidStateError" in e.message:
                raise FilesRefused(e.message) from None
            raise
        names = await self.run(ref.context, FILE_NAMES_JS, f["input"])
        if len(names) != len(files):
            raise BrowserError(f"Zen didn't take the files: {key} now holds {names or 'none'}.")
        return f"Attached {', '.join(names)} to {key}{note}."

    async def scroll(self, tab: dict, direction: str, amount: int) -> str:
        ctx = tab["context"]
        w, h, *_ = await self.run(ctx, SCROLL_INFO_JS)
        dx, dy = {"down": (0, amount), "up": (0, -amount), "right": (amount, 0), "left": (-amount, 0)}[direction]
        before = await self.run(ctx, SCROLL_STATE_JS)
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
        if state == before:
            await self.run(ctx, SCROLL_JS_FALLBACK, dx, dy)
        _, h, x, y, sw, sh = await self.run(ctx, SCROLL_INFO_JS)
        return f"Scrolled {direction}. Viewport now at y={y:.0f} of {sh:.0f} (height {h:.0f}), x={x:.0f} of {sw:.0f}."
