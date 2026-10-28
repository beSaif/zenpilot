"""Tab ids and refs across reconnects, with getTree and the page walker faked."""

import pytest

from zenpilot.bidi import BiDiError
from zenpilot.browser import Browser, BrowserError
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
