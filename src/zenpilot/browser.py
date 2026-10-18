"""Browser operations over WebDriver BiDi: tabs, page reading and trusted input."""

import asyncio
import base64
import contextlib
import json
import mimetypes
import random
import string
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import websockets

from . import challenges, human
from .bidi import BiDi, BiDiError
from .tree import MAX_ITEMS, WALKER_JS, Item, Ref, Refs, render, unwrap

# BiDi errors that mean "this node or frame no longer exists".
STALE_ERRORS = {"no such node", "no such element", "stale element reference", "detached shadow root", "no such frame"}
ENTER, BACKSPACE = "\ue007", "\ue003"  # WebDriver key codes
SPECIAL_KEYS = {"\n": ENTER, "\t": "\ue004"}
NAMED_KEYS = {
    "enter": ENTER, "return": ENTER, "tab": "\ue004", "escape": "\ue00c", "esc": "\ue00c",
    "backspace": BACKSPACE, "delete": "\ue017", "space": " ", "home": "\ue011", "end": "\ue010",
    "pageup": "\ue00e", "pagedown": "\ue00f", "arrowup": "\ue013", "arrowdown": "\ue015",
    "arrowleft": "\ue012", "arrowright": "\ue014", "up": "\ue013", "down": "\ue015", "left": "\ue012",
    "right": "\ue014", **{f"f{i}": chr(0xE031 + i - 1) for i in range(1, 13)},
}  # fmt: skip
MODIFIERS = {"shift": "\ue008", "control": "\ue009", "ctrl": "\ue009", "alt": "\ue00a", "option": "\ue00a",
             "meta": "\ue03d", "cmd": "\ue03d", "command": "\ue03d"}  # fmt: skip
SANDBOX = "zenpilot"  # page scripts can't see or tamper with code run here
OVERLAY_JS = (files("zenpilot") / "overlay.js").read_text()

# Runs in Zen's browser window (chrome scope), only with tab_indicator: outlines the tab Claude
# is acting in, in the tab strip, for a few seconds after each action. Pages can't see this.
TAB_MARK_JS = """(contextId) => {
  const { NavigableManager } = ChromeUtils.importESModule("chrome://remote/content/shared/NavigableManager.sys.mjs");
  const win = window;
  const tab = win.gBrowser?.tabs.find((t) => NavigableManager.getIdForBrowser(t.linkedBrowser) === contextId);
  if (!tab) return false;
  if (!win.zenpilotTabSheet) {
    const css = `
      .tabbrowser-tab[zenpilot-active] .tab-background {
        outline: 2px solid #d97757 !important; outline-offset: -2px;
        box-shadow: 0 0 10px 2px rgba(217, 119, 87, .55) !important;
      }
      .tabbrowser-tab[zenpilot-active] .tab-label { color: #d97757 !important; }`;
    win.windowUtils.loadSheetUsingURIString("data:text/css," + encodeURIComponent(css), win.windowUtils.AUTHOR_SHEET);
    win.zenpilotTabSheet = true;
  }
  tab.setAttribute("zenpilot-active", "true");
  win.clearTimeout(tab.zenpilotTimer);
  tab.zenpilotTimer = win.setTimeout(() => tab.removeAttribute("zenpilot-active"), 4000);
  return true;
}"""
# Firefox allows one BiDi session. Releasing it when idle lets Claude Desktop and
# several Claude Code sessions take turns; refs and tab ids expire with it.
IDLE_RELEASE_SECONDS = 120

# Finds where the element is on screen, scrolling it into view if needed ("instant"), or
# reporting how far the page must scroll ("wheel"). Also reports anything covering it.
PREPARE_JS = """(el, scroll) => {
  if (!el.isConnected) return ["stale"];
  const vw = document.documentElement.clientWidth || innerWidth;
  const vh = document.documentElement.clientHeight || innerHeight;
  const box = () => {
    // A link that wraps has one box per line; use the biggest, not the union.
    const rs = [...el.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
    return rs.length ? rs.reduce((a, b) => (a.width * a.height >= b.width * b.height ? a : b)) : null;
  };
  const shown = (r) => {
    const w = Math.min(r.right, vw) - Math.max(r.left, 0), h = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    return w >= Math.min(r.width, vw / 2) - 1 && h >= Math.min(r.height, vh / 2) - 1;
  };
  const coverAt = (r) => {
    const x = Math.max(0, Math.min(vw - 1, r.left + r.width / 2));
    const y = Math.max(0, Math.min(vh - 1, r.top + r.height / 2));
    const top = el.getRootNode().elementFromPoint?.(x, y);
    if (!top || el === top || el.contains(top) || top.contains(el) || top.control === el) return "";
    const classes = typeof top.className === "string" ? top.className.trim().split(/\\s+/).filter(Boolean) : [];
    return top.tagName.toLowerCase() + (top.id ? "#" + top.id : "") +
      (classes.length ? "." + classes.slice(0, 2).join(".") : "");
  };
  let r = box();
  if (!r) {
    el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
    r = box();
    if (!r) return ["hidden", el.tagName];
  }
  if (!shown(r) && scroll === "wheel")
    return ["offscreen", el.tagName,
            Math.round(r.left + r.width / 2 - vw / 2), Math.round(r.top + r.height / 2 - vh / 2)];
  // Off screen, or covered (maybe clipped by a scrolling container, or under a sticky header).
  if (!shown(r) || coverAt(r)) {
    el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
    r = box() || r;
  }
  return ["ok", el.tagName, coverAt(r), el.tagName === "INPUT" ? el.type : "",
          [r.left, r.top, r.width, r.height], [vw, vh], document.visibilityState === "visible" && document.hasFocus()];
}"""

# Whether a point lands on the element (after the pointer got there, a hover menu may cover it).
HITS_JS = """(el, x, y) => {
  const top = el.getRootNode().elementFromPoint?.(x, y);
  return !top || el === top || el.contains(top) || top.contains(el) || top.control === el;
}"""

# Where an iframe's content starts inside its parent's viewport.
FRAME_BOX_JS = """(frame) => {
  const r = frame.getBoundingClientRect(), s = getComputedStyle(frame);
  return [r.left + frame.clientLeft + parseFloat(s.paddingLeft), r.top + frame.clientTop + parseFloat(s.paddingTop)];
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

VALUE_JS = """(el) => el.type === "password" ? null : el.isContentEditable ? el.innerText : el.value"""

# What kind of form field an element is, for fill_form and select_option.
FIELD_JS = """(el) => {
  const role = el.getAttribute("role") || "";
  const t = el.tagName;
  if (el.disabled || el.getAttribute("aria-disabled") === "true") return ["disabled"];
  if (t === "SELECT") return [el.multiple ? "select-multiple" : "select"];
  if (t === "INPUT" && ["checkbox", "radio"].includes(el.type)) return [el.type, el.checked];
  if (["checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"].includes(role))
    return [role === "radio" || role === "menuitemradio" ? "radio" : "checkbox",
            el.getAttribute("aria-checked") === "true"];
  if (t === "INPUT" && el.type === "file") return ["file"];
  if (t === "INPUT" && ["button", "submit", "reset", "image"].includes(el.type)) return ["button"];
  if (role === "slider" && t !== "INPUT")
    return ["slider", +el.getAttribute("aria-valuenow"), +el.getAttribute("aria-valuemin"),
            +el.getAttribute("aria-valuemax")];
  if (["combobox", "listbox"].includes(role) || el.getAttribute("aria-haspopup") === "listbox")
    return ["combobox", t === "INPUT" || t === "TEXTAREA" || el.isContentEditable];
  if ((t === "INPUT" || t === "TEXTAREA") && el.readOnly) return ["readonly"];
  if (t === "INPUT" || t === "TEXTAREA" || el.isContentEditable) return ["text"];
  return ["other"];
}"""

# Picks <select> options by visible text or value: exact, then prefix, then substring.
SELECT_JS = """(el, wanted) => {
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim().toLowerCase();
  const opts = [...el.options].filter((o) => !o.disabled);
  const find = (w) => {
    w = norm(w);
    for (const test of [(o) => norm(o.text) === w || norm(o.value) === w, (o) => norm(o.text).startsWith(w),
                        (o) => norm(o.text).includes(w)]) {
      const hits = opts.filter(test);
      if (hits.length) return hits;
    }
    return [];
  };
  const picks = wanted.map(find);
  const missing = wanted.filter((w, i) => !picks[i].length);
  if (missing.length) return ["missing", missing, opts.slice(0, 30).map((o) => o.text.trim())];
  const vague = wanted.filter((w, i) => picks[i].length > 1 && norm(picks[i][0].text) !== norm(w));
  if (vague.length) return ["ambiguous", vague, picks[wanted.indexOf(vague[0])].slice(0, 10).map((o) => o.text.trim())];
  return ["ok", picks.map((p) => p[0].index), picks.map((p) => p[0].text.trim())];
}"""

# Sets <select> options directly (for <select multiple>, or when typing can't reach an option).
SET_OPTIONS_JS = """(el, indexes) => {
  const want = new Set(JSON.parse(indexes));
  el.focus();
  for (const o of el.options) o.selected = want.has(o.index);
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return [...el.selectedOptions].map((o) => o.text.trim());
}"""

SELECTED_JS = """(el) => [...el.selectedOptions].map((o) => o.index)"""

# The options a custom dropdown (ARIA combobox/listbox) shows once opened: [node, text] pairs.
LISTBOX_OPTIONS_JS = """(el) => {
  const root = el.getRootNode();
  const ids = [el.getAttribute("aria-controls"), el.getAttribute("aria-owns")].join(" ").split(/\\s+/).filter(Boolean);
  const lists = ids.map((id) => root.getElementById?.(id) || document.getElementById(id)).filter(Boolean);
  if (el.getAttribute("role") === "listbox") lists.push(el);
  const scopes = lists.length ? lists : [document];
  const out = [];
  for (const scope of scopes)
    for (const o of scope.querySelectorAll("[role=option], [role=menuitem], [role=menuitemradio], li[data-value]")) {
      const r = o.getBoundingClientRect();
      const usable = o.checkVisibility({checkVisibilityCSS: true}) && o.getAttribute("aria-disabled") !== "true";
      if (r.width > 0 && r.height > 0 && usable)
        out.push([o, (o.innerText || o.textContent || "").replace(/\\s+/g, " ").trim()]);
    }
  return out.slice(0, 200);
}"""

# The file input a ref stands for: itself, its label's control, or one inside or near it
# (sites often hide the real input behind a styled button).
FILE_INPUT_JS = """(el) => {
  const isFile = (n) => n && n.tagName === "INPUT" && n.type === "file";
  if (isFile(el)) return [el, el.multiple, el.accept];
  const label = el.closest?.("label");
  if (isFile(el.control)) return [el.control, el.control.multiple, el.control.accept];
  if (isFile(label?.control)) return [label.control, label.control.multiple, label.control.accept];
  for (let n = el, i = 0; n && i < 4; n = n.parentElement || n.getRootNode().host, i++) {
    const found = n.querySelectorAll?.("input[type=file]");
    if (found?.length === 1) return [found[0], found[0].multiple, found[0].accept];
  }
  return null;
}"""

# Attaches files built from their bytes. Used when input.setFiles isn't available: it needs
# dom.file.createInChild, one of the automation prefs zenpilot keeps off.
ATTACH_FILES_JS = """(input, files) => {
  const dt = new DataTransfer();
  for (const f of JSON.parse(files)) {
    const bin = Uint8Array.from(atob(f.data), (c) => c.charCodeAt(0));
    dt.items.add(new File([bin], f.name, {type: f.type, lastModified: f.modified}));
  }
  input.files = dt.files;
  input.dispatchEvent(new Event("input", {bubbles: true}));
  input.dispatchEvent(new Event("change", {bubbles: true}));
  return input.files.length;
}"""
MAX_UPLOAD_BYTES = 25_000_000

GET_TEXT_JS = """(max) => {
  const articles = document.querySelectorAll("article");
  const root = document.querySelector("main, [role=main]") || (articles.length === 1 ? articles[0] : document.body);
  const text = (root?.innerText || "").replace(/[ \\t]+\\n/g, "\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
  return [document.title, text.length, text.slice(0, max)];
}"""

TAB_STATE_JS = "() => [document.title, document.visibilityState, document.hasFocus()]"
FOCUS_STATE_JS = "() => document.visibilityState === 'visible' && document.hasFocus()"

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


def _arg(v) -> dict:
    if isinstance(v, dict):  # {"sharedId": ...}
        return v
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "boolean", "value": v}
    if isinstance(v, (int, float)):
        return {"type": "number", "value": v}
    if isinstance(v, (list, tuple)):
        return {"type": "array", "value": [_arg(x) for x in v]}
    return {"type": "string", "value": str(v)}


def _stale(key: str) -> BrowserError:
    return BrowserError(f"Ref {key} is stale: the page changed since read_page. Call read_page again for fresh refs.")


class Browser:
    def __init__(self):
        self._bidi: BiDi | None = None
        self.port: int | None = None
        self._lock = asyncio.Lock()
        self._next_tab = 1  # never reset, so old tab ids can't alias new tabs
        self._tab_ids: dict[str, str] = {}  # context -> "t1"
        self._contexts: dict[str, str] = {}  # "t1" -> context
        self._last_used: str | None = None  # context zenpilot last opened, switched to or navigated
        self._busy = 0
        self._idle_timer: asyncio.TimerHandle | None = None
        self.refs = Refs()
        self.style = human.STYLES["natural"]  # set from the config on every tool call
        self.show_cursor = True
        self.tab_indicator = False
        self._rng = random.Random()
        self._mouse_at: dict[str, tuple[float, float]] = {}  # top context -> last pointer position
        self._careful: set[str] = set()  # top contexts where a bot check showed up
        self._told_unfocused: set[str] = set()
        # A fresh, meaningless element name for the overlay, so pages can't target it.
        self._overlay_tag = (
            "".join(self._rng.choices(string.ascii_lowercase, k=7))
            + "-"
            + "".join(self._rng.choices(string.ascii_lowercase, k=5))
        )

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
            self._mouse_at.clear()  # BiDi input state starts over with each session
            self._careful.clear()
            self._told_unfocused.clear()
            self.port = port
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
                        ]
                    },
                )
            except BaseException:
                await bidi.close()  # don't leave an orphaned session behind
                raise
            self._bidi = bidi

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
        """Call a JS function in a browsing context and return its unwrapped result.

        It runs in zenpilot's sandbox: it sees the page's DOM through clean built-ins, while
        the page can't see its globals, hook the functions it calls or find it in stack traces.
        """
        r = await self.call(
            "script.callFunction",
            {
                "functionDeclaration": fn,
                "target": {"context": context, "sandbox": SANDBOX},
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

    async def read_page(self, tab: dict, frame_allowed: Callable[[str], str | None]) -> tuple[str, list[str]]:
        """Outline of the page, and the CAPTCHA vendors on it.

        frame_allowed(url) returns a reason string to skip a frame.
        """
        self.refs.drop_tab(tab["id"])
        items: list[Item] = []
        truncated = False
        vendors: list[str] = []

        async def collect(context: str, base: int):
            nonlocal truncated
            out, trunc = await self.run(context, WALKER_JS, MAX_ITEMS, timeout=20)
            truncated |= trunc
            vendors.extend(await self.run(context, challenges.DETECT_JS))
            for depth, line, node, frame in out:
                item = Item(base + depth, line, context, node and node["sharedId"])
                items.append(item)
                if not frame:
                    continue
                try:
                    url = await self.url_of(frame["context"])
                    if vendor := challenges.frame_vendor(url):
                        vendors.append(vendor)
                        item.line += f" [{vendor} CAPTCHA: not for Claude; use handoff_to_user]"
                    elif reason := frame_allowed(url):
                        item.line += f" [skipped: {reason}]"
                    else:
                        await collect(frame["context"], base + depth + 1)
                except (BrowserError, BiDiError):
                    item.line += " [not readable]"

        await collect(tab["context"], 0)
        if vendors:
            self._careful.add(tab["context"])
        return render(items, self.refs, tab["id"], truncated), sorted(set(vendors))

    async def challenge(self, tab: dict) -> list[str]:
        """Vendors of CAPTCHAs or bot checks showing in a tab (in the page or its frames)."""
        vendors = []
        try:
            vendors += await self.run(tab["context"], challenges.DETECT_JS, timeout=5)
            tree = (await self.call("browsingContext.getTree", {"root": tab["context"]}))["contexts"]
        except (BrowserError, BiDiError):
            return vendors
        stack = [c for t in tree for c in t.get("children") or []]
        while stack:
            c = stack.pop()
            if vendor := challenges.frame_vendor(c["url"]):
                if await self._frame_shown(c["context"]):
                    vendors.append(vendor)
            stack += c.get("children") or []
        if vendors:
            self._careful.add(tab["context"])
        return sorted(set(vendors))

    async def _frame_shown(self, context: str) -> bool:
        """Whether a frame's element in its parent is on screen (hidden challenge frames don't count)."""
        try:
            parent = (await self.call("browsingContext.getTree", {"root": context, "maxDepth": 0}))["contexts"][0]
            nodes = (
                await self.call(
                    "browsingContext.locateNodes",
                    {"context": parent["parent"], "locator": {"type": "context", "value": {"context": context}}},
                )
            )["nodes"]
            return bool(nodes) and await self.run(
                parent["parent"],
                "(f) => { const r = f.getBoundingClientRect(); return r.width > 1 && r.height > 1 && "
                "f.checkVisibility({checkVisibilityCSS: true}); }",
                {"sharedId": nodes[0]["sharedId"]},
            )
        except (BrowserError, BiDiError, KeyError, IndexError):
            return True

    async def get_text(self, tab: dict, max_chars: int) -> tuple[str, str]:
        title, length, text = await self.run(tab["context"], GET_TEXT_JS, max_chars)
        if length > max_chars:
            text += f"\n[truncated: showing {max_chars} of {length} characters]"
        return title, text or "[no text]"

    async def screenshot(self, tab: dict) -> bytes:
        # The overlay is part of the page, so it would show up; take it out of the picture.
        await self._overlay(tab["context"], "hide")
        try:
            r = await self.call(
                "browsingContext.captureScreenshot",
                {
                    "context": tab["context"],
                    "format": {"type": "image/jpeg", "quality": 0.8},
                },
            )
        finally:
            await self._overlay(tab["context"], "show")
        return base64.b64decode(r["data"])

    # -- the "Claude is using this tab" overlay ------------------------------

    async def _overlay(self, context: str, cmd: str, **arg) -> None:
        """Update the overlay in a top-level context. It's cosmetic: failures are ignored."""
        if not self.show_cursor and cmd not in ("hide", "remove"):
            return
        try:
            await self.run(context, OVERLAY_JS, self._overlay_tag, cmd, json.dumps(arg) if arg else None, timeout=3)
        except (BrowserError, BiDiError):
            pass
        if self.tab_indicator and cmd not in ("hide", "show", "remove"):
            await self._mark_tab(context)

    async def _mark_tab(self, context: str) -> None:
        """Outline the tab itself in Zen's tab strip (needs tab_indicator: privileged access)."""
        try:
            # One chrome context per browser window; the tab is in one of them.
            windows = (await self.call("browsingContext.getTree", {"maxDepth": 0, "moz:scope": "chrome"}))["contexts"]
            for w in windows:
                r = await self.call(
                    "script.callFunction",
                    {
                        "functionDeclaration": TAB_MARK_JS,
                        "target": {"context": w["context"]},
                        "arguments": [_arg(context)],
                        "awaitPromise": False,
                        "resultOwnership": "none",
                    },
                    timeout=3,
                )
                if r.get("type") == "success" and unwrap(r["result"]) is True:
                    return
        except (BrowserError, BiDiError, KeyError):
            self.tab_indicator = False  # Zen wasn't started with system access; don't retry every action

    async def _frame_offset(self, context: str, top: str) -> tuple[float, float] | None:
        """Where a frame's viewport starts in the tab's viewport; None if it can't be worked out."""
        x = y = 0.0
        try:
            while context != top:
                parent = (await self.call("browsingContext.getTree", {"root": context, "maxDepth": 0}))["contexts"][0][
                    "parent"
                ]
                nodes = (
                    await self.call(
                        "browsingContext.locateNodes",
                        {"context": parent, "locator": {"type": "context", "value": {"context": context}}},
                    )
                )["nodes"]
                dx, dy = await self.run(parent, FRAME_BOX_JS, {"sharedId": nodes[0]["sharedId"]})
                x, y, context = x + dx, y + dy, parent
        except (BrowserError, BiDiError, KeyError, IndexError, TypeError):
            return None
        return x, y

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

    def _style(self, top: str) -> human.Style:
        # Where a site has shown a bot check, slow down further (unless the user chose "fast").
        return human.STYLES["careful"] if top in self._careful and self.style.paths else self.style

    async def _node(self, key: str, ref: Ref, fn: str, *args):
        """Run fn(element, *args) on a ref's node, mapping a vanished node to a stale-ref error."""
        try:
            return await self.run(ref.context, fn, {"sharedId": ref.shared_id}, *args)
        except BiDiError as e:
            if e.error in STALE_ERRORS:
                raise _stale(key) from None
            raise

    async def _prepare(self, key: str, ref: Ref, style: human.Style) -> "Target":
        """Bring the node on screen and measure it. Scrolls with the wheel, as a person would."""
        top = self.top_context(ref)
        res = await self._node(key, ref, PREPARE_JS, "wheel" if style.paths and ref.context == top else "instant")
        if res[0] == "offscreen":
            await self._wheel(ref.context, res[2], res[3], style)
            res = await self._node(key, ref, PREPARE_JS, "instant")  # a nested scroller the wheel missed
        if res[0] == "stale":
            raise _stale(key)
        if res[0] == "hidden":
            raise BrowserError(f"Ref {key} is no longer visible. Call read_page again.")
        _, tag, cover, kind, rect, viewport, focused = res
        return Target(tag, kind, cover, rect, (viewport[0], viewport[1]), focused)

    async def _perform(self, context: str, source: dict) -> None:
        """Run one input source's actions; the reply comes when they're done."""
        timeout = human.duration_s(source["actions"]) + 15
        try:
            await self.call("input.performActions", {"context": context, "actions": [source]}, timeout)
        except BaseException:
            # Don't leave a key or button held down if this went wrong halfway.
            with contextlib.suppress(BrowserError, BiDiError):
                await self.call("input.releaseActions", {"context": context}, 5)
            raise

    async def _pointer(self, context: str, actions: list[dict]) -> None:
        if actions:
            await self._perform(
                context, {"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": actions}
            )

    def _start(self, top: str, offset: tuple[float, float] | None, viewport: tuple[float, float]):
        """Where the pointer is now, in the target frame's coordinates (a guess on first use)."""
        last = self._mouse_at.get(top)
        if last is None or offset is None:
            return self._rng.uniform(0.25, 0.75) * viewport[0], self._rng.uniform(0.3, 0.8) * viewport[1]
        return last[0] - offset[0], last[1] - offset[1]

    async def _glide(self, top: str, offset, points: list[tuple[float, float, float]]) -> None:
        """Animate the overlay cursor along points (frame coordinates) while the real pointer moves."""
        if offset is not None and points:
            await self._overlay(top, "move", points=[[x + offset[0], y + offset[1], ms] for x, y, ms in points])

    async def _click(self, key: str, ref: Ref, t: "Target", style: human.Style) -> str:
        """Move to the element and click it. Returns a note if something got in the way."""
        top = self.top_context(ref)
        offset = (0.0, 0.0) if ref.context == top else await self._frame_offset(ref.context, top)
        note = ""
        if not style.paths:
            end = (t.rect[0] + t.rect[2] / 2, t.rect[1] + t.rect[3] / 2)
            await self._glide(top, offset, [(*end, 180)])
            origin = {"type": "element", "element": {"sharedId": ref.shared_id}}
            await self._pointer(ref.context, [{"type": "pointerMove", "x": 0, "y": 0, "origin": origin}])
            await self._pointer(ref.context, human.click_actions([], style, self._rng))
        else:
            end = human.click_point(t.rect, t.viewport, self._rng)
            start = self._start(top, offset, t.viewport)
            points = human.path(start, end, min(t.rect[2], t.rect[3]), t.viewport, style, self._rng)
            await self._glide(top, offset, points)
            await self._pointer(ref.context, human.pointer_moves(points))
            end = points[-1][:2] if points else end
            if not await self._node(key, ref, HITS_JS, *end):
                await asyncio.sleep(0.3)  # a menu opened by passing over something may close again
                if not await self._node(key, ref, HITS_JS, *end):
                    note = "something covered it when the pointer arrived (a hover menu or popup?), and got the click"
            await self._pointer(ref.context, human.click_actions([], style, self._rng))
        if offset is not None:
            self._mouse_at[top] = (end[0] + offset[0], end[1] + offset[1])
            await self._overlay(top, "click", x=end[0] + offset[0], y=end[1] + offset[1])
        return note

    async def _click_ref(self, key: str, ref: Ref, style: human.Style) -> str:
        return await self._click(key, ref, await self._prepare(key, ref, style), style)

    async def _keys(self, context: str, keys: list[str], style: human.Style) -> None:
        actions = human.key_actions(keys, style, self._rng)
        for i in range(0, len(actions), 300):
            await self._perform(context, {"type": "key", "id": "keyboard", "actions": actions[i : i + 300]})

    async def _wheel(self, context: str, dx: int, dy: int, style: human.Style) -> bool:
        """Scroll with the wheel near the middle of the viewport; True if anything moved."""
        w, h, *_ = await self.run(context, SCROLL_INFO_JS)
        x = int(min(max(w / 2 + self._rng.gauss(0, w * 0.05), 1), w - 2))
        y = int(min(max(h / 2 + self._rng.gauss(0, h * 0.05), 1), h - 2))
        if style.paths:  # the pointer has to be over what it scrolls
            points = human.path(self._start(context, (0.0, 0.0), (w, h)), (x, y), 200, (w, h), style, self._rng)
            await self._glide(context, (0.0, 0.0), points)
            await self._pointer(context, human.pointer_moves(points))
            self._mouse_at[context] = (x, y)
        await self._overlay(context, "badge", rect=[x - 30, y - 12, 60, 24], text="Scrolling", ms=1200)
        before = await self.run(context, SCROLL_STATE_JS)
        await self._perform(
            context, {"type": "wheel", "id": "wheel", "actions": human.wheel_actions(x, y, dx, dy, style, self._rng)}
        )
        # Smooth scrolling takes a while: wait until the position settles (up to 2s),
        # or give up after 0.4s if nothing moved at all.
        state = before
        for i in range(20):
            await asyncio.sleep(0.1)
            now = await self.run(context, SCROLL_STATE_JS)
            if now == state and (now != before or i >= 3):
                break
            state = now
        return state != before

    async def _settle(self, top: str, action) -> str:
        """Run an input action; if it starts a navigation or opens a tab, wait and report it."""
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

    def _unfocused_note(self, top: str, t: "Target") -> str:
        if t.focused or top in self._told_unfocused:
            return ""
        self._told_unfocused.add(top)
        return (
            " Note: Zen's window isn't in front. Input still works, but some sites treat input to a background "
            "window as automated; if one blocks you, ask the user to bring Zen to the front."
        )

    async def click(self, key: str) -> str:
        ref = self.ref(key)
        top = self.top_context(ref)
        style = self._style(top)
        await self._overlay(top, "active")
        t = await self._prepare(key, ref, style)
        result = f"Clicked {key}."
        if t.cover:
            result += f" Note: its center was covered by <{t.cover}>, which received the click."
        note = ""

        async def act():
            nonlocal note
            note = await self._click(key, ref, t, style)

        nav = await self._settle(top, act)
        if note and not t.cover:
            result += f" Note: {note}."
        if nav:
            result += f" {nav}."
        return result + self._unfocused_note(top, t)

    async def type(self, key: str, text: str, submit: bool) -> str:
        ref = self.ref(key)
        top = self.top_context(ref)
        style = self._style(top)
        await self._overlay(top, "active")
        t = await self._prepare(key, ref, style)
        if t.kind in VALUE_INPUTS:
            value = await self._node(key, ref, SET_VALUE_JS, text)
            if value is None:
                raise BrowserError(f"Ref {key} is disabled or read-only.")
            if text and not value:
                raise BrowserError(f"{text!r} isn't a valid {t.kind} value. Use the format {VALUE_INPUTS[t.kind]}.")
            result = f"Set {key} to {value!r}."
        else:
            kind = (await self._node(key, ref, FIELD_JS))[0]
            if kind == "readonly":
                raise BrowserError(
                    f"Ref {key} is read-only, so it can't be typed into. It's probably a custom date picker or "
                    "dropdown: click it, then read_page and pick from what opens (or use select_option)."
                )
            if t.tag == "SELECT":
                # Clicking would open the native popup; focus and type to pick an option instead.
                await self._node(key, ref, "(el) => el.focus()")
            else:
                await self._click(key, ref, t, style)
                await self._node(key, ref, SELECT_CONTENT_JS)
            keys = [SPECIAL_KEYS.get(ch, ch) for ch in text] or [BACKSPACE]
            if self.show_cursor and (offset := await self._frame_offset(ref.context, top)) is not None:
                x, y, w, h = t.rect
                ms = int(len(keys) * style.key_ms) + 800
                await self._overlay(top, "badge", rect=[x + offset[0], y + offset[1], w, h], text="Typing…", ms=ms)
            await self._keys(ref.context, keys, style)
            result = f"Typed {len(text)} characters into {key}."
            if t.tag != "SELECT" and (shown := await self._node(key, ref, VALUE_JS)) is not None:
                if " ".join(shown.split()) != " ".join(text.split()):
                    result += (
                        " The field's content differs from what was typed (the page may have formatted, limited "
                        "or autocompleted it); read_page shows it."
                    )
        if submit and (nav := await self._settle(top, lambda: self._keys(ref.context, [ENTER], style))):
            result += f" Pressed Enter. {nav}."
        elif submit:
            result += " Pressed Enter."
        return result + self._unfocused_note(top, t)

    async def scroll(self, tab: dict, direction: str, amount: int) -> str:
        ctx = tab["context"]
        dx, dy = {"down": (0, amount), "up": (0, -amount), "right": (amount, 0), "left": (-amount, 0)}[direction]
        await self._overlay(ctx, "active")
        if not await self._wheel(ctx, dx, dy, self._style(ctx)):
            # Wheel events don't scroll while Zen's window is hidden: scroll what the wheel would have.
            await self.run(ctx, SCROLL_JS_FALLBACK, dx, dy)
        _, h, x, y, sw, sh = await self.run(ctx, SCROLL_INFO_JS)
        return f"Scrolled {direction}. Viewport now at y={y:.0f} of {sh:.0f} (height {h:.0f}), x={x:.0f} of {sw:.0f}."

    async def press(self, combo: str, tab: dict | None = None, key: str | None = None) -> str:
        """Press a key or shortcut like "Escape" or "Shift+Tab", on a ref or the focused element."""
        parts = [p for p in combo.replace("++", "+plus").split("+") if p]
        if not parts:
            raise BrowserError(
                "No key given. Use a name like Enter, Escape, Tab, ArrowDown, or a combo like Shift+Tab."
            )
        *mods, last = parts
        try:
            held = [MODIFIERS[m.strip().lower()] for m in mods]
        except KeyError as e:
            raise BrowserError(f"Unknown modifier {e.args[0]!r}. Use Shift, Control, Alt or Meta.") from None
        last = "+" if last == "plus" else last.strip() or " "
        k = NAMED_KEYS.get(last.lower(), last if len(last) == 1 else None)
        if k is None:
            raise BrowserError(f"Unknown key {last!r}. Use a single character or one of: {', '.join(NAMED_KEYS)}.")
        if key is not None:
            ref = self.ref(key)
            context, top = ref.context, self.top_context(ref)
            await self._node(key, ref, "(el) => el.focus()")
        else:
            context = top = tab["context"]
        style = self._style(top)
        await self._overlay(top, "active")
        hold = [human.pause(self._rng.uniform(60, 120) * style.speed)] if style.paths else []
        actions = (
            [{"type": "keyDown", "value": m} for m in held]
            + [{"type": "keyDown", "value": k}, *hold, {"type": "keyUp", "value": k}]
            + [{"type": "keyUp", "value": m} for m in reversed(held)]
        )
        nav = await self._settle(
            top, lambda: self._perform(context, {"type": "key", "id": "keyboard", "actions": actions})
        )
        return f"Pressed {combo}" + (f" on {key}" if key else "") + "." + (f" {nav}." if nav else "")

    # -- form fields ------------------------------------------------------

    async def field_kind(self, key: str) -> list:
        return await self._node(key, self.ref(key), FIELD_JS)

    async def select_option(self, key: str, options: list[str]) -> str:
        """Pick options in a <select>, or in a custom dropdown (ARIA combobox, listbox or menu button)."""
        ref = self.ref(key)
        top = self.top_context(ref)
        style = self._style(top)
        options = [o for o in options if o.strip()]
        if not options:
            raise BrowserError("Say which option to pick.")
        kind, *info = await self._node(key, ref, FIELD_JS)
        typed = kind == "text" or (kind == "combobox" and info[0])  # autocomplete: typing filters the list
        if kind == "disabled":
            raise BrowserError(f"Ref {key} is disabled.")
        if kind in ("checkbox", "radio", "file", "readonly"):
            raise BrowserError(f"Ref {key} is a {kind}, not a dropdown. Use click, type or fill_form instead.")
        await self._overlay(top, "active")
        t = await self._prepare(key, ref, style)
        if kind in ("select", "select-multiple"):
            status, a, b = await self._node(key, ref, SELECT_JS, options)
            if status == "missing":
                raise BrowserError(f"No option matching {a[0]!r} in {key}. Options include: {', '.join(map(repr, b))}.")
            if status == "ambiguous":
                raise BrowserError(f"{a[0]!r} matches several options in {key}: {', '.join(map(repr, b))}. Be exact.")
            if kind == "select" and len(a) > 1:
                raise BrowserError(f"Ref {key} takes one option; got {len(a)}.")
            if kind == "select":
                # Type the option's name, which a focused <select> jumps to, with trusted key events.
                await self._node(key, ref, "(el) => el.focus()")
                await self._keys(ref.context, list(b[0][:12]), style)
                if await self._node(key, ref, SELECTED_JS) == a:
                    return f"Selected {b[0]!r} in {key}."
            picked = await self._node(key, ref, SET_OPTIONS_JS, json.dumps(a))
            return f"Selected {', '.join(map(repr, picked))} in {key}."
        # A custom dropdown: open it, then click the matching option.
        picked = []
        for wanted in options:
            if typed:
                await self.type(key, wanted, False)
            else:
                expanded = await self._node(key, ref, "(el) => el.getAttribute('aria-expanded') === 'true'")
                if not expanded:
                    await self._click_ref(key, ref, style)
            found = []
            for _ in range(10):  # options often render a moment after opening
                await asyncio.sleep(0.15)
                found = await self._node(key, ref, LISTBOX_OPTIONS_JS)
                if found:
                    break
            if not found:
                raise BrowserError(
                    f"Opened {key} but found no options in it. "
                    "Call read_page to see what opened, then click the option."
                )
            node, text = _match_option(wanted, found)
            if node is None:
                names = ", ".join(repr(t) for _, t in found[:25])
                raise BrowserError(f"{text} for {wanted!r} in {key}. It shows: {names}.")
            opt = Ref(ref.tab, ref.context, node["sharedId"])
            await self._settle(top, lambda opt=opt: self._click_ref(key, opt, style))
            picked.append(text)
        return f"Selected {', '.join(map(repr, picked))} in {key}." + self._unfocused_note(top, t)

    async def set_checked(self, key: str, want: bool) -> str:
        ref = self.ref(key)
        kind, checked = (await self._node(key, ref, FIELD_JS))[:2]
        if kind == "radio" and not want:
            raise BrowserError(f"Ref {key} is a radio button: it can't be unchecked; pick another option instead.")
        if checked == want:
            return f"{key} was already {'checked' if want else 'unchecked'}."
        await self.click(key)
        now = (await self._node(key, ref, FIELD_JS))[1]
        if now != want:
            raise BrowserError(f"Clicked {key}, but it's still {'checked' if now else 'unchecked'}.")
        return f"{'Checked' if want else 'Unchecked'} {key}."

    async def set_slider(self, key: str, target: float) -> str:
        """Move a custom (ARIA) slider with the arrow keys until it reaches target."""
        ref = self.ref(key)
        top = self.top_context(ref)
        style = self._style(top)
        now = (await self._node(key, ref, FIELD_JS))[1]
        await self._click_ref(key, ref, style)
        for _ in range(200):
            if now == target:
                break
            await self._keys(ref.context, ["\ue014" if now < target else "\ue012"], style)  # arrow right/left
            new = (await self._node(key, ref, FIELD_JS))[1]
            stepped_past = now < target < new or new < target < now
            if new == now or stepped_past:  # stuck at an end, or between steps: stop at the nearest step
                now = new
                break
            now = new
        return f"Set {key} to {now:g}."

    async def upload(self, key: str, paths: list[Path]) -> str:
        ref = self.ref(key)
        found = await self._node(key, ref, FILE_INPUT_JS)
        if not found:
            raise BrowserError(
                f"Ref {key} isn't a file upload and has no file input near it. Use the ref read_page shows as "
                "'file' (it may be hidden behind a button). Drag-and-drop only areas can't be used."
            )
        node, multiple, accept = found
        if len(paths) > 1 and not multiple:
            raise BrowserError(f"This upload takes one file; got {len(paths)}.")
        top = self.top_context(ref)
        await self._overlay(top, "active")
        try:
            await self.call(
                "input.setFiles", {"context": ref.context, "element": node, "files": [str(p) for p in paths]}
            )
        except BiDiError as e:
            if e.error in STALE_ERRORS:
                raise _stale(key) from None
            if e.error != "unsupported operation":
                raise BrowserError(f"Couldn't attach the files: {e.message or e.error}") from None
            if sum(p.stat().st_size for p in paths) > MAX_UPLOAD_BYTES:
                raise BrowserError(
                    "These files are too big for zenpilot to attach (25 MB in all); ask the user to."
                ) from None
            data = [
                {
                    "name": p.name,
                    "type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                    "modified": int(p.stat().st_mtime * 1000),
                    "data": base64.b64encode(p.read_bytes()).decode(),
                }
                for p in paths
            ]
            await self.run(ref.context, ATTACH_FILES_JS, node, json.dumps(data), timeout=60)
        names = ", ".join(p.name for p in paths)
        hint = f" (it accepts {accept})" if accept else ""
        return f"Attached {names} to {key}{hint}."


@dataclass
class Target:
    """Where an element is on screen, from PREPARE_JS."""

    tag: str
    kind: str  # input type, or ""
    cover: str  # what covers its center, or ""
    rect: list[float]  # x, y, width, height in its frame's viewport
    viewport: tuple[float, float]
    focused: bool  # the page is visible and Zen's window has focus


def _match_option(wanted: str, found: list) -> tuple[dict | None, str]:
    """The option whose text matches wanted (exact, then prefix, then substring), else (None, why)."""

    def norm(s):
        return " ".join(s.split()).casefold()

    w = norm(wanted)
    for test in (lambda t: t == w, lambda t: t.startswith(w), lambda t: w in t):
        hits = [(n, t) for n, t in found if test(norm(t))]
        if len(hits) == 1 or (hits and len({norm(t) for _, t in hits}) == 1):
            return hits[0]
        if len(hits) > 1:
            return None, f"Several options match ({', '.join(repr(t) for _, t in hits[:8])}); be exact"
    return None, "No option matches"
