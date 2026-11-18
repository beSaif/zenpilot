"""Tab ids and refs across reconnects, with getTree and the page walker faked."""

import asyncio

import pytest

from zenpilot.bidi import BiDiError
from zenpilot.browser import DIALOG_CHARS, NETWORK_LIMIT, Browser, BrowserError
from zenpilot.tree import Item, Ref


def ctx(context, url):
    return {"context": context, "url": url, "clientWindow": "w1"}


class FakeBrowser(Browser):
    """Answers getTree from self.contexts and walks pages from self.pages (context -> items)."""

    def __init__(self, contexts):
        super().__init__()
        self.contexts = contexts
        self.pages: dict[str, list[Item]] = {}
        self._new_session(9222)

    def reconnect(self, contexts):
        self._new_session(9222)  # what connect() does before the new session starts
        self.contexts = contexts

    async def call(self, method, params=None, timeout=30):
        assert method == "browsingContext.getTree"
        contexts = [c for c in self.contexts if c["context"] == params.get("root", c["context"])]
        return {"contexts": contexts}

    async def _walk(self, context):
        return [(it, None) for it in self.pages.get(context, [])], False


async def ids(b):
    return {t["context"]: t["id"] for t in await b.tabs(probe=False)}


async def test_tab_ids_survive_a_reconnect():
    b = FakeBrowser([ctx("a", "https://mail.example.com/inbox"), ctx("b", "https://jobs.example.org/apply")])
    before = await ids(b)
    assert before == {"a": "t1", "b": "t2"}
    # Zen gives every tab a new context id in a new session; the mail tab moved on meanwhile.
    b.reconnect(
        [
            ctx("a2", "https://mail.example.com/thread/9"),
            ctx("b2", "https://jobs.example.org/apply"),
            ctx("c", "about:blank"),
        ]
    )
    assert await ids(b) == {"a2": "t1", "b2": "t2", "c": "t3"}
    assert (await b.tab("t2"))["context"] == "b2"


async def test_same_url_tabs_keep_their_order():
    b = FakeBrowser([ctx("a", "https://x.com/"), ctx("b", "https://x.com/")])
    await ids(b)
    b.reconnect([ctx("a2", "https://x.com/"), ctx("b2", "https://x.com/")])
    assert await ids(b) == {"a2": "t1", "b2": "t2"}


async def test_closed_tabs_ids_are_not_reused():
    b = FakeBrowser([ctx("a", "https://x.com/"), ctx("b", "https://y.com/")])
    await ids(b)
    b.reconnect([ctx("b2", "https://y.com/"), ctx("n", "https://z.com/")])
    assert await ids(b) == {"b2": "t2", "n": "t3"}
    with pytest.raises(BrowserError, match="Unknown tab id 't1'"):
        await b.tab("t1")


async def test_another_zenpilot_process_sees_the_same_ids():
    first = FakeBrowser([ctx("a", "https://x.com/"), ctx("b", "https://y.com/")])
    await ids(first)
    # A resumed Claude Code session is a new process, with a new BiDi session.
    second = FakeBrowser([ctx("a2", "https://x.com/"), ctx("b2", "https://y.com/"), ctx("c", "https://z.com/")])
    assert await ids(second) == {"a2": "t1", "b2": "t2", "c": "t3"}
    first.reconnect(second.contexts)
    assert await ids(first) == {"a2": "t1", "b2": "t2", "c": "t3"}  # and the first sees the second's new tab


def page(context, *keys):
    return [Item(0, k, context, f"node-{context}-{i}", k) for i, k in enumerate(keys)]


async def test_refs_are_found_again_after_a_reconnect():
    url = "https://jobs.example.org/apply"
    b = FakeBrowser([ctx("a", url)])
    await ids(b)
    apply = b.refs.add(Ref("t1", "a", "old-node", 'button "Apply"', url, b._session))
    b.reconnect([ctx("a2", url + "#form")])
    b.pages["a2"] = page("a2", 'textbox "Name"', 'button "Apply"')

    async def check(key, ref):
        return ref.shared_id

    ref, node, note = await b._target(apply, check)
    assert node == "node-a2-1" and ref.context == "a2"
    assert note == f' ({apply} was found again as button "Apply")'
    assert b.refs.get(apply) == ref  # and it stays fixed


@pytest.mark.parametrize(
    "keys, url, ref_url",
    [
        (['button "Delete"', 'button "Delete"'], "https://x.com/", "https://x.com/"),  # which one?
        (['button "Apply"'], "https://x.com/next-step", "https://x.com/"),  # another page
        (['button "Apply"'], "https://x.com/", ""),  # was in a frame, or not unique when read
    ],
)
async def test_refs_are_not_guessed(keys, url, ref_url):
    b = FakeBrowser([ctx("a", url)])
    await ids(b)
    key = b.refs.add(Ref("t1", "a", "old-node", keys[0], ref_url, b._session - 1))
    b.pages["a"] = page("a", *keys)
    with pytest.raises(BrowserError, match="can't be found again"):
        await b._target(key, lambda k, r: None)


async def test_no_window_is_explained():
    b = FakeBrowser([])  # Zen still runs, but its last window was closed
    with pytest.raises(BrowserError, match="no open window"):
        await b.tab(None)

    async def create_fails(method, params=None, timeout=30):
        message = """TypeError: can't access property "selectedTab", lazy.TabManager.getTabBrowser(...) is null"""
        raise BiDiError("unknown error", message)

    b.call = create_fails
    with pytest.raises(BrowserError, match="Ask the user to open a Zen window"):
        await b.new_tab()


async def test_actions_report_what_changed_on_the_page():
    b = FakeBrowser([ctx("a", "https://shop.example.com/cart")])
    await ids(b)
    b.pages["a"] = [Item(0, 'button "Pay"', "a", "n1", 'button "Pay"'), Item(0, '"Loading…"', "a")]
    await b.read_page(await b.tab("t1"), lambda url: None)
    ref = b.refs.get("r1")
    b.pages["a"] = [
        Item(0, 'button "Pay"', "a", "n1", 'button "Pay"'),
        Item(0, "dialog", "a"),
        Item(1, 'button "Confirm"', "a", "n2", 'button "Confirm"'),
    ]
    out = await b._changes(ref)
    assert "+ dialog" in out and '+ r2 button "Confirm"' in out and '- "Loading…"' in out
    assert 'button "Pay"' not in out.split("Page changes:")[1]  # unchanged lines are left out
    assert b.refs.get("r2").url == "https://shop.example.com/cart"  # unique, so it can be found again
    assert "didn't change" in await b._changes(ref)  # compared with the latest walk, not read_page's


async def test_find_lists_only_matching_controls_with_usable_refs():
    b = FakeBrowser([ctx("a", "https://shop.example.com/cart")])
    await ids(b)
    b.pages["a"] = [
        Item(0, "form", "a"),
        Item(1, '"Save money with a plan"', "a"),
        Item(1, 'button "Save draft"', "a", "n1", 'button "Save draft"'),
        Item(1, 'textbox "Email"', "a", "n2", 'textbox "Email"'),
    ]
    out = await b.read_page(await b.tab("t1"), lambda url: None, "SAVE")
    assert out == 'r1 button "Save draft"'  # no plain text, no indent
    assert b.refs.get("r1").shared_id == "n1" and b.refs.get("r1").url  # unique on the page, so found again
    assert await b.read_page(await b.tab("t1"), lambda url: None, "checkout") == ""


async def test_stop_on_the_badge_is_the_users_not_zenpilots_click():
    b = FakeBrowser([])
    stop = {"channel": "zenpilot", "data": {"type": "string", "value": "stop"}}
    b._busy_input = True  # zenpilot's own click landed on the badge
    b._on_message(stop)
    assert not b.stopped
    b._busy_input = False
    b._on_message({"channel": "other", "data": {"type": "string", "value": "stop"}})
    assert not b.stopped
    b._on_message(stop)
    assert b.stopped


async def test_the_badge_says_claude_may_be_waiting_between_calls(monkeypatch):
    b = FakeBrowser([])
    shown = []

    async def badge(context, state, action=None):
        shown.append(state)

    monkeypatch.setattr(b, "_badge", badge)
    monkeypatch.setattr(Browser, "connected", True)
    b._badges.add("c1")
    b._busy = 1  # a tool call is running
    await b._show_waiting()
    assert shown == []
    b._busy = 0
    await b._show_waiting()
    assert shown == ["waiting"]
    b.stopped = True  # Stopped stays on the badge
    await b._show_waiting()
    assert shown == ["waiting"]


def test_parse_keys_names_chords_and_plus():
    from zenpilot import browser as b

    assert b.parse_keys("Enter Tab") == [b.ENTER, ""]
    assert b.parse_keys("ctrl+a") == [("", "a")]
    assert b.parse_keys("shift+Tab Escape") == [("", ""), ""]
    assert b.parse_keys("+ a") == ["+", "a"]
    with pytest.raises(BrowserError, match=r"Unknown key \(5 chars\)"):  # not the key: it may be typed text
        b.parse_keys("Bogus")
    with pytest.raises(BrowserError, match="No keys given"):
        b.parse_keys("  ")


@pytest.mark.parametrize(
    "chord",
    ["cmd+v", "ctrl+v", "CTRL+V", "meta+V", "ctrl+shift+v", "cmd+option+shift+v", "shift+insert", "Insert+Shift"],
)
def test_parse_keys_refuses_paste_chords(chord):
    from zenpilot import browser as b

    with pytest.raises(BrowserError, match="Paste shortcuts"):
        b.parse_keys(f"Enter {chord}")


def test_parse_keys_refuses_raw_webdriver_codes_and_keeps_near_misses():
    from zenpilot import browser as b

    with pytest.raises(BrowserError, match="Unknown key"):
        b.parse_keys("+v")  # Meta as a raw code, which would slip past the paste check
    assert b.parse_keys("v shift+v Insert ctrl+a") == [
        "v",
        (b.SHIFT, "v"),
        b.NAMED_KEYS["insert"],
        (b.MODIFIERS["ctrl"], "a"),
    ]


async def test_wait_for_polls_until_the_text_shows(monkeypatch):
    from zenpilot import browser as b

    monkeypatch.setattr(b, "WAIT_POLL_SECONDS", 0.01)
    fb = FakeBrowser([ctx("a", "https://ok.com/")])
    answers = iter([False, False, True])

    async def run(context, fn, *args, timeout=15):
        return next(answers)

    async def url_of(context):
        return "https://ok.com/"

    fb.run, fb.url_of = run, url_of
    seen = []
    tab = {"id": "t1", "context": "a"}
    assert await fb.wait_for(tab, text="Saved", key=None, url_contains=None, timeout_ms=1000, allowed=seen.append)
    assert seen == ["https://ok.com/"] * 3  # the site is checked on every poll


async def test_wait_for_url_and_timeout(monkeypatch):
    from zenpilot import browser as b

    monkeypatch.setattr(b, "WAIT_POLL_SECONDS", 0.01)
    fb = FakeBrowser([ctx("a", "https://ok.com/")])

    async def run(context, fn, *args, timeout=15):
        return False

    async def url_of(context):
        return "https://ok.com/login"

    fb.run, fb.url_of = run, url_of
    tab = {"id": "t1", "context": "a"}
    assert (
        await fb.wait_for(tab, text=None, key=None, url_contains="done", timeout_ms=30, allowed=lambda u: None) is False
    )
    assert await fb.wait_for(tab, text=None, key=None, url_contains="login", timeout_ms=30, allowed=lambda u: None)


async def test_wait_for_ends_early_when_the_user_presses_stop(monkeypatch):
    from zenpilot import browser as b

    monkeypatch.setattr(b, "WAIT_POLL_SECONDS", 0.01)
    fb = FakeBrowser([ctx("a", "https://ok.com/")])
    fb.stopped = True
    with pytest.raises(BrowserError, match="pressed Stop"):
        await fb.wait_for(
            {"id": "t1", "context": "a"}, text="x", key=None, url_contains=None, timeout_ms=1000, allowed=lambda u: None
        )


def log_entry(context, level, text, *, type="console", url="https://x.com/app.js", line=12):
    """A BiDi log.entryAdded event, as Zen sends it for console calls and uncaught errors."""
    return {
        "type": type,
        "level": level,
        "text": text,
        "timestamp": 1700000000000,
        "source": {"realm": "r1", "context": context},
        "stackTrace": {"callFrames": [{"url": url, "lineNumber": line, "columnNumber": 3, "functionName": ""}]},
    }


def test_console_keeps_each_tabs_entries_apart_and_bounded():
    b = FakeBrowser([])
    for i in range(250):
        b._on_log(log_entry("a", "info", f"line {i}"))
    b._on_log(log_entry("b", "error", "other tab", type="javascript"))
    tab_a = {"context": "a"}
    entries = b.console(tab_a, limit=500)
    assert len(entries) == 200
    assert entries[0]["text"] == "line 50" and entries[-1]["text"] == "line 249"
    assert entries[-1] == {
        "level": "info",
        "type": "console",
        "text": "line 249",
        "url": "https://x.com/app.js",
        "line": 12,
        "time": 1700000000000,
    }
    assert [e["text"] for e in b.console({"context": "b"})] == ["other tab"]


def test_console_filters_by_level_and_clears_after_reading():
    b = FakeBrowser([])
    b._on_log(log_entry("a", "info", "hello"))
    b._on_log(log_entry("a", "error", "boom", type="javascript"))
    b._on_log(log_entry("a", "error", "again"))
    tab = {"context": "a"}
    assert [e["text"] for e in b.console(tab, level="error")] == ["boom", "again"]
    assert [e["text"] for e in b.console(tab, limit=1)] == ["again"]
    assert [e["text"] for e in b.console(tab, clear=True)] == ["hello", "boom", "again"]
    assert b.console(tab) == []


def test_console_ignores_events_without_a_context():
    b = FakeBrowser([])
    b._on_log({"type": "console", "level": "info", "text": "orphan", "source": {"realm": "r1"}})
    assert b._console == {}


def test_console_forgets_the_old_page_when_the_tab_navigates():
    b = FakeBrowser([])
    b._on_log(log_entry("a", "error", "from the old site"))
    b._on_log(log_entry("b", "error", "other tab"))
    b._on_navigation({"context": "a", "url": "https://next.com/"})
    assert b.console({"context": "a"}) == []
    assert [e["text"] for e in b.console({"context": "b"})] == ["other tab"]


def test_console_cuts_long_entries_and_copes_with_null_text():
    b = FakeBrowser([])
    b._on_log(log_entry("a", "info", "x" * 100_000, url="data:" + "y" * 100_000))
    b._on_log(log_entry("a", "info", None))
    long, empty = b.console({"context": "a"})
    assert len(long["text"]) == len(long["url"]) == 2000
    assert empty["text"] == ""


class DialogBrowser(FakeBrowser):
    """Also answers handleUserPrompt, or fails it with error, and lets _settle wait for events that never come."""

    def __init__(self, contexts, error=None):
        super().__init__(contexts)
        self.error, self.answered = error, []
        self._bidi = self

    def expect(self, method, predicate=None):
        return asyncio.get_running_loop().create_future()

    async def call(self, method, params=None, timeout=30):
        if method != "browsingContext.handleUserPrompt":
            return await super().call(method, params, timeout)
        if self.error:
            raise BiDiError(*self.error)
        self.answered.append(params)
        return {}


def confirm(context, message):
    return {"context": context, "type": "confirm", "message": message, "handler": "ignore"}


async def test_an_open_dialog_is_reported_fenced_and_blocks_input():
    b = FakeBrowser([ctx("a", "https://shop.example.com/cart")])
    await ids(b)
    b.pages["a"] = [Item(0, 'button "Delete"', "a", "n1", 'button "Delete"')]
    await b.read_page(await b.tab("t1"), lambda url: None)
    ref = b.refs.get("r1")
    b._dialogs["a"] = confirm("a", "x" * 5000)
    out = await b._changes(ref)
    assert "opened a dialog (confirm)" in out and "handle_dialog (accept or dismiss)" in out
    assert '<untrusted_web_content source="https://shop.example.com/cart">' in out  # the page wrote it
    assert "x" * DIALOG_CHARS in out and "x" * (DIALOG_CHARS + 1) not in out
    # The page is blocked, so input would only hang until the dialog is answered.
    with pytest.raises(BrowserError, match="Answer it with handle_dialog first"):
        await b._actions(ref, [])


async def test_answering_a_prompt_types_its_text_and_forgets_the_dialog():
    b = DialogBrowser([ctx("a", "https://shop.example.com/cart")])
    tab = (await b.tabs(probe=False))[0]
    b._dialogs["a"] = {**confirm("a", "Your name?"), "type": "prompt"}
    assert await b.answer_dialog(tab, True, "Ada") == "Accepted the prompt dialog in t1."
    assert b.answered == [{"context": "a", "accept": True, "userText": "Ada"}]
    assert not b._dialogs
    assert await b.dialog_note("a") == ""


@pytest.mark.parametrize(
    ("error", "message"),
    [(("no such alert", ""), "No dialog is open in t1"), (("unsupported operation", "alertCheck"), "can't answer")],
)
async def test_dialogs_that_cant_be_answered_are_explained(error, message):
    b = DialogBrowser([ctx("a", "https://shop.example.com/cart")], error)
    tab = (await b.tabs(probe=False))[0]
    b._dialogs["a"] = confirm("a", "Delete this?")
    with pytest.raises(BrowserError, match=message):
        await b.answer_dialog(tab, False, None)


async def test_a_frames_dialog_counts_for_its_tab_until_the_frame_navigates():
    b = FakeBrowser([ctx("a", "https://shop.example.com/cart")])
    await ids(b)
    b._frame_tops["f"] = "a"  # read_page went into frame f
    b._dialogs["f"] = confirm("f", "From the frame?")
    assert "From the frame?" in await b.dialog_note("a")
    b._on_navigation({"context": "f"})  # its new site hasn't been checked
    assert await b.dialog_note("a") == ""


def sent(context, rid, url, *, navigation=None, hops=0, headers=(), t=1000):
    """A BiDi network.beforeRequestSent event, as Zen sends it."""
    hs = [{"name": n, "value": {"type": "string", "value": v}} for n, v in headers]
    req = {"request": rid, "url": url, "method": "GET", "headers": hs, "destination": "", "initiatorType": "fetch"}
    return {"context": context, "navigation": navigation, "redirectCount": hops, "request": req, "timestamp": t}


def done(context, rid, status=200, *, hops=0, t=1040):
    """A BiDi network.responseCompleted event."""
    res = {"status": status, "statusText": "OK", "protocol": "http/1.1", "mimeType": "application/json"}
    res["headers"] = [{"name": "Set-Cookie", "value": {"type": "string", "value": "sid=1"}}]
    return {"context": context, "redirectCount": hops, "request": {"request": rid}, "timestamp": t, "response": res}


def network_browser():
    """Tab a on an allowlisted site, tab b on one that isn't."""
    b = FakeBrowser([])
    b.allowed = lambda url: url.startswith("https://ok.com/")
    b._pages = {"a": "https://ok.com/app", "b": "https://bank.example/"}
    return b


def test_only_allowlisted_tabs_requests_are_recorded():
    b = network_browser()
    b._on_request(sent("a", "1", "https://ok.com/api", headers=[("Authorization", "Bearer x"), ("Accept", "*/*")]))
    b._on_response(done("a", "1"))
    b._on_request(sent("b", "2", "https://bank.example/balance"))
    b._on_response(done("b", "2"))
    b._on_request(sent("frame", "3", "https://ok.com/frame-api"))  # a frame, not a tab
    assert set(b._network) == {"a"}
    [e] = b.requests({"context": "a"})
    assert (e["id"], e["status"], e["ms"], e["type"]) == ("1", 200, 40, "fetch")
    assert e["headers"] == [("Authorization", "[redacted]"), ("Accept", "*/*")]
    assert e["response_headers"] == [("Set-Cookie", "[redacted]")]


def test_navigation_starts_a_tabs_list_over_keeping_the_page_load():
    b = network_browser()
    b._on_request(sent("a", "1", "https://ok.com/api"))
    b._on_request(sent("a", "2", "https://ok.com/next", navigation="n1"))
    b._on_navigation({"context": "a", "navigation": "n1", "url": "https://ok.com/next"})
    assert [e["id"] for e in b.requests({"context": "a"})] == ["2"]
    # A page load to a blocked site isn't recorded, and the tab's list is dropped when it gets there.
    b._on_request(sent("a", "3", "https://bank.example/", navigation="n2"))
    b._on_navigation({"context": "a", "navigation": "n2", "url": "https://bank.example/"})
    b._on_request(sent("a", "4", "https://ok.com/beacon"))
    assert b.requests({"context": "a"}) == []
    # Coming back from a blocked site: the page load counts for the page it loads.
    b._on_request(sent("a", "5", "https://ok.com/", navigation="n3"))
    b._on_navigation({"context": "a", "navigation": "n3", "url": "https://ok.com/"})
    assert [e["id"] for e in b.requests({"context": "a"})] == ["5"]


def test_requests_are_capped_per_tab_filtered_and_cleared():
    b = network_browser()
    for i in range(NETWORK_LIMIT + 5):
        b._on_request(sent("a", str(i), f"https://ok.com/{i}.js"))
    entries = b.requests({"context": "a"}, limit=NETWORK_LIMIT + 10)
    assert len(entries) == NETWORK_LIMIT and entries[0]["id"] == "5"
    assert [e["id"] for e in b.requests({"context": "a"}, url_pattern="/42.js")] == ["42"]
    assert len(b.requests({"context": "a"}, limit=3, clear=True)) == 3
    assert b.requests({"context": "a"}) == []


def test_each_redirect_hop_is_its_own_request():
    b = network_browser()
    b._on_request(sent("a", "1", "https://ok.com/old"))
    b._on_response(done("a", "1", 301))
    b._on_request(sent("a", "1", "https://ok.com/new", hops=1))
    b._on_response(done("a", "1", 200, hops=1))
    assert [(e["id"], e["status"]) for e in b.requests({"context": "a"})] == [("1", 301), ("1.1", 200)]


async def test_response_bodies_come_from_zens_data_collector():
    b = network_browser()
    b._on_request(sent("a", "1", "https://ok.com/api"))
    entry = b.request({"context": "a"}, "1")
    assert await b.response_body(entry) == (
        None,
        "this Zen can't keep response bodies (it lacks network.addDataCollector)",
    )

    calls = []

    async def call(method, params=None, timeout=30):
        calls.append((method, params))
        if params["request"] == "gone":
            raise BiDiError("no such network data", "not found")
        return {"bytes": {"type": "base64", "value": "aGk="}}

    b._collector, b.call = "col1", call
    assert await b.response_body(entry) == (b"hi", "")
    assert calls == [("network.getData", {"dataType": "response", "collector": "col1", "request": "1"})]
    body, why = await b.response_body({**entry, "request": "gone"})
    assert body is None and "no such network data" in why
