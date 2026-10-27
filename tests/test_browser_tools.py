"""find, form_input, hover, wait_for, network routing and browser_batch, against fakes."""

import asyncio
import contextlib
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zenpilot import browser as browser_mod
from zenpilot import server
from zenpilot.bidi import BiDiError
from zenpilot.browser import ENTER, Browser, BrowserError
from zenpilot.devtools import Devtools
from zenpilot.safety import Config
from zenpilot.tree import Ref

OUTLINE = "\n".join(
    [
        'r1 textbox "Name"',
        'r2 button "Save"',
        '  text "Saved items"',
        'r3 checkbox "Terms" [unchecked]',
    ]
)


# -- server level -----------------------------------------------------------


class FakeBrowser:
    connected = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.devtools = Devtools()
        self.devtools_error = None
        self.calls: list = []
        self.outline = OUTLINE
        self.wait_result = True

    @contextlib.asynccontextmanager
    async def session(self, port):
        yield

    async def tab(self, tab_id):
        return {"id": "t1", "context": "c1", "url": "http://localhost/", "active": True, "title": ""}

    async def ref_url(self, key):
        return "http://localhost/"

    def ref(self, key):
        return key

    def top_context(self, ref):
        return "c1"

    async def read_page(self, tab, reason):
        return self.outline

    def dialog_note(self, context):
        return None

    async def press(self, tab, keys):
        self.calls.append(("press", keys))

    async def screenshot(self, tab, full_page=False):
        return b"img"

    async def form_input(self, key, value):
        self.calls.append(("form_input", key, value))
        return f"Set {key}."

    async def hover(self, key):
        self.calls.append(("hover", key))
        return f"Hovered {key}."

    async def wait_for(self, tab, *, text, key, url_contains, timeout_ms):
        self.calls.append(("wait_for", text, key, url_contains, timeout_ms))
        return self.wait_result

    async def network_route(self, url, action, status, body, content_type):
        self.calls.append(("route", url, action, status, body, content_type))
        return "Route route1 set."

    async def network_unroute(self, route_id):
        self.calls.append(("unroute", route_id))
        return "Removed 1 route(s)."

    async def url_of(self, context):
        return "http://localhost/page"

    async def type(self, key, text, submit):
        self.calls.append(("type", key, text, submit))
        return f"Typed into {key}."

    async def evaluate(self, tab, code):
        self.calls.append(("evaluate", code))
        return "1"

    async def handle_dialog(self, tab, accept, text):
        self.calls.append(("dialog", text))
        return ("alert", "ok")


@pytest.fixture
def fake(monkeypatch, tmp_path):
    cfg = Config(
        allowlist=["localhost"],
        read_only=False,
        allow_all_sites=False,
        port=9222,
        audit_log=tmp_path / "audit.jsonl",
        auto_launch=False,
    )
    monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)
    fb = FakeBrowser(cfg)
    monkeypatch.setattr(server, "browser", fb)
    return fb


def last_audit(tmp_path):
    return json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])


async def test_find_keeps_matching_ref_lines(fake):
    out = await server.find(query="SAVE")
    assert 'r2 button "Save"' in out
    assert "Name" not in out
    assert "Saved items" not in out  # text lines have no ref
    assert "untrusted_web_content" in out


async def test_find_reports_no_match(fake):
    assert "No element matches" in await server.find(query="nothing here")


async def test_find_caps_results_at_50(fake):
    fake.outline = "\n".join(f'r{i} button "Item {i}"' for i in range(1, 80))
    out = await server.find(query="item")
    assert out.count('button "Item') == 50
    assert "29 more not shown" in out


async def test_find_rejects_empty_query(fake):
    with pytest.raises(ToolError, match="Give a query"):
        await server.find(query="  ")


async def test_find_works_in_read_only_mode(fake):
    fake.cfg.read_only = True
    assert 'r2 button "Save"' in await server.find(query="save")


async def test_form_input_passes_value_and_redacts_audit(fake, tmp_path):
    assert await server.form_input(ref="r1", value="s3cret") == "Set r1."
    assert fake.calls == [("form_input", "r1", "s3cret")]
    assert last_audit(tmp_path)["params"]["value"] == {"chars": 6}


async def test_form_input_is_blocked_in_read_only_mode(fake):
    fake.cfg.read_only = True
    with pytest.raises(ToolError, match="read-only"):
        await server.form_input(ref="r1", value="x")
    assert fake.calls == []


async def test_hover_is_blocked_in_read_only_mode(fake):
    fake.cfg.read_only = True
    with pytest.raises(ToolError, match="read-only"):
        await server.hover(ref="r2")
    assert fake.calls == []


async def test_hover_moves_to_ref(fake):
    assert await server.hover(ref="r2") == "Hovered r2."
    assert fake.calls == [("hover", "r2")]


async def test_wait_for_needs_a_condition(fake):
    with pytest.raises(ToolError, match="Give text"):
        await server.wait_for()
    assert fake.calls == []


async def test_wait_for_clamps_timeout(fake):
    await server.wait_for(text="ok", timeout_ms=10)
    await server.wait_for(text="ok", timeout_ms=99999)
    assert [c[-1] for c in fake.calls] == [100, 30_000]


async def test_wait_for_timeout_is_tool_error(fake):
    fake.wait_result = False
    with pytest.raises(ToolError, match='Timed out after 5000 ms waiting for text "done"'):
        await server.wait_for(text="done")


async def test_wait_for_works_in_read_only_mode(fake):
    fake.cfg.read_only = True
    assert "Condition met" in await server.wait_for(url_contains="localhost")


async def test_network_route_rejects_bad_arguments(fake):
    with pytest.raises(ToolError, match="exact URL"):
        await server.network_route(url_pattern="http://localhost/api/*")
    with pytest.raises(ToolError, match="http"):
        await server.network_route(url_pattern="ftp://localhost/x")
    with pytest.raises(ToolError, match="abort or respond"):
        await server.network_route(url_pattern="http://localhost/x", action="block")
    assert fake.calls == []


async def test_network_route_is_blocked_in_read_only_mode(fake):
    fake.cfg.read_only = True
    with pytest.raises(ToolError, match="read-only"):
        await server.network_route(url_pattern="http://localhost/x")


async def test_network_route_redacts_body_in_audit(fake, tmp_path):
    await server.network_route(url_pattern="http://localhost/x", action="respond", body="secret-body")
    assert fake.calls[-1] == ("route", "http://localhost/x", "respond", 200, "secret-body", "application/json")
    assert last_audit(tmp_path)["params"]["body"] == {"chars": 11}


async def test_network_unroute_passes_id(fake):
    await server.network_unroute(route_id="route1")
    await server.network_unroute()
    assert fake.calls == [("unroute", "route1"), ("unroute", None)]


async def test_browser_batch_runs_steps_in_order(fake):
    out = await server.browser_batch(
        steps=[{"tool": "read_page"}, {"tool": "press_key", "args": {"keys": "Enter"}}, {"tool": "screenshot"}]
    )
    assert out.index("1. read_page: OK") < out.index("2. press_key: OK") < out.index("3. screenshot: OK")
    assert "[image]" in out
    assert fake.calls == [("press", [ENTER])]


async def test_browser_batch_rejects_nested_and_unknown_before_running(fake):
    with pytest.raises(ToolError, match="can't run inside"):
        await server.browser_batch(
            steps=[{"tool": "press_key", "args": {"keys": "Enter"}}, {"tool": "browser_batch", "args": {"steps": []}}]
        )
    with pytest.raises(ToolError, match="unknown tool 'nope'"):
        await server.browser_batch(steps=[{"tool": "nope"}])
    assert fake.calls == []


async def test_browser_batch_limits_steps(fake):
    with pytest.raises(ToolError, match="1 to 30"):
        await server.browser_batch(steps=[])
    with pytest.raises(ToolError, match="1 to 30"):
        await server.browser_batch(steps=[{"tool": "read_page"}] * 31)


async def test_browser_batch_stops_on_first_error(fake):
    bad = {"tool": "press_key", "args": {"keys": "Hyper"}}
    good = {"tool": "press_key", "args": {"keys": "Enter"}}
    out = await server.browser_batch(steps=[bad, good])
    assert "1. press_key: ERROR" in out
    assert "2. press_key: skipped" in out
    assert fake.calls == []
    out = await server.browser_batch(steps=[bad, good], stop_on_error=False)
    assert "2. press_key: OK" in out
    assert fake.calls == [("press", [ENTER])]


async def test_browser_batch_applies_read_only_per_step(fake):
    fake.cfg.read_only = True
    out = await server.browser_batch(steps=[{"tool": "read_page"}, {"tool": "press_key", "args": {"keys": "Enter"}}])
    assert "1. read_page: OK" in out
    assert "2. press_key: ERROR" in out
    assert "read-only" in out
    assert fake.calls == []


# -- browser level ----------------------------------------------------------


class FakeBiDi:
    instances: list = []
    replies: dict = {}  # a list reply is consumed one item per call
    fail = None

    def __init__(self, url):
        self.url = url
        self.connected = False
        self.sent: list[tuple[str, dict]] = []
        self.handlers: dict[str, list] = {}
        FakeBiDi.instances.append(self)

    async def connect(self):
        self.connected = True
        return {}

    async def send(self, method, params=None, timeout=30):
        params = params or {}
        self.sent.append((method, params))
        if FakeBiDi.fail and (exc := FakeBiDi.fail(method, params)):
            raise exc
        reply = FakeBiDi.replies.get(method, {})
        if isinstance(reply, list):
            reply = reply.pop(0)
        return reply

    def on(self, method, callback):
        self.handlers.setdefault(method, []).append(callback)
        return lambda: None

    def expect(self, method, predicate=lambda p: True):
        return asyncio.get_running_loop().create_future()

    def emit(self, method, params):
        for callback in list(self.handlers.get(method, [])):
            callback(params)

    async def close(self):
        self.connected = False


@pytest.fixture
def fake_bidi(monkeypatch):
    FakeBiDi.instances = []
    FakeBiDi.replies = {}
    FakeBiDi.fail = None
    monkeypatch.setattr(browser_mod, "BiDi", FakeBiDi)
    return FakeBiDi


def sent(bidi, method):
    return [p for m, p in bidi.sent if m == method]


def array(*items):
    return {"type": "success", "result": {"type": "array", "value": [{"type": "string", "value": v} for v in items]}}


async def connected(fake_bidi):
    b = Browser()
    await b.connect(9222)
    return b, fake_bidi.instances[0]


async def test_form_input_runs_setter_script(fake_bidi):
    b, bidi = await connected(fake_bidi)
    b.refs.add(Ref("t1", "c1", "s1"))
    FakeBiDi.replies["script.callFunction"] = [array("ok", "BUTTON", "", ""), array("ok", "Brazil")]
    assert await b.form_input("r1", "br") == "Set r1 to 'Brazil'."
    params = sent(bidi, "script.callFunction")[-1]
    assert params["functionDeclaration"] == browser_mod.FORM_INPUT_JS
    assert params["target"] == {"context": "c1"}
    assert params["arguments"] == [{"sharedId": "s1"}, {"type": "string", "value": "br"}]


@pytest.mark.parametrize(
    ("status", "detail", "value", "message"),
    [
        ("readonly", "", "x", "disabled or read-only"),
        ("nooption", "Brazil, Chile", "Peru", "No option of r1 matches 'Peru'. Options: Brazil, Chile"),
        ("badbool", "", "yes", "Use true or false"),
        ("unsupported", "input type=file", "x", "can't set"),
    ],
)
async def test_form_input_explains_refusals(fake_bidi, status, detail, value, message):
    b, _ = await connected(fake_bidi)
    b.refs.add(Ref("t1", "c1", "s1"))
    FakeBiDi.replies["script.callFunction"] = [array("ok", "INPUT", "", ""), array(status, detail)]
    with pytest.raises(BrowserError, match=message.replace("(", r"\(").replace("?", r"\?")):
        await b.form_input("r1", value)


async def test_hover_moves_pointer_to_element_center(fake_bidi):
    b, bidi = await connected(fake_bidi)
    b.refs.add(Ref("t1", "c1", "s1"))
    FakeBiDi.replies["script.callFunction"] = array("ok", "BUTTON", "", "")
    assert await b.hover("r1") == "Hovered r1."
    actions = sent(bidi, "input.performActions")[-1]
    assert actions["context"] == "c1"
    assert actions["actions"][0]["actions"] == [
        {"type": "pointerMove", "x": 0, "y": 0, "origin": {"type": "element", "element": {"sharedId": "s1"}}}
    ]


async def test_wait_for_text_polls_until_shown():
    b = Browser()
    answers = [False, True]

    async def run(ctx, fn, *args, timeout=15):
        return answers.pop(0)

    b.run = run
    assert await b.wait_for({"context": "c1"}, text="Saved", key=None, url_contains=None, timeout_ms=3000)
    assert answers == []


async def test_wait_for_times_out():
    b = Browser()

    async def run(ctx, fn, *args, timeout=15):
        return False

    b.run = run
    assert not await b.wait_for({"context": "c1"}, text="x", key=None, url_contains=None, timeout_ms=300)


async def test_wait_for_url_condition():
    b = Browser()

    async def url_of(context):
        return "http://localhost/done"

    b.url_of = url_of
    assert await b.wait_for({"context": "c1"}, text=None, key=None, url_contains="/done", timeout_ms=300)


async def test_wait_for_ref_needs_visible_element():
    b = Browser()

    async def run(ctx, fn, *args, timeout=15):
        return True

    b.run = run
    b.refs.add(Ref("t1", "c1", "s1"))
    assert await b.wait_for({"context": "c1"}, text=None, key="r1", url_contains=None, timeout_ms=300)
    with pytest.raises(BrowserError, match="Unknown ref"):
        await b.wait_for({"context": "c1"}, text=None, key="r9", url_contains=None, timeout_ms=300)


async def test_network_route_registers_exact_url_intercept(fake_bidi):
    b, bidi = await connected(fake_bidi)
    FakeBiDi.replies["network.addIntercept"] = {"intercept": "i1"}
    out = await b.network_route("http://localhost/data.json", "abort", 200, None, "application/json")
    assert sent(bidi, "network.addIntercept") == [
        {"phases": ["beforeRequestSent"], "urlPatterns": [{"type": "string", "pattern": "http://localhost/data.json"}]}
    ]
    assert "route1" in out


async def test_matching_blocked_request_is_aborted(fake_bidi):
    b, bidi = await connected(fake_bidi)
    FakeBiDi.replies["network.addIntercept"] = {"intercept": "i1"}
    await b.network_route("http://localhost/data.json", "abort", 200, None, "application/json")
    bidi.emit("network.beforeRequestSent", {"request": {"request": "req1"}, "isBlocked": True, "intercepts": ["i1"]})
    await asyncio.sleep(0.01)
    assert sent(bidi, "network.failRequest") == [{"request": "req1"}]


async def test_respond_route_sends_mock(fake_bidi):
    b, bidi = await connected(fake_bidi)
    FakeBiDi.replies["network.addIntercept"] = {"intercept": "i1"}
    await b.network_route("http://localhost/data.json", "respond", 201, '{"ok": 1}', "application/json")
    bidi.emit("network.beforeRequestSent", {"request": {"request": "req1"}, "isBlocked": True, "intercepts": ["i1"]})
    await asyncio.sleep(0.01)
    assert sent(bidi, "network.provideResponse") == [
        {
            "request": "req1",
            "statusCode": 201,
            "headers": [{"name": "content-type", "value": {"type": "string", "value": "application/json"}}],
            "body": {"type": "string", "value": '{"ok": 1}'},
        }
    ]


async def test_unmatched_and_unblocked_requests_are_left_alone(fake_bidi):
    b, bidi = await connected(fake_bidi)
    FakeBiDi.replies["network.addIntercept"] = {"intercept": "i1"}
    await b.network_route("http://localhost/data.json", "abort", 200, None, "application/json")
    bidi.emit("network.beforeRequestSent", {"request": {"request": "req2"}, "isBlocked": True, "intercepts": ["other"]})
    bidi.emit("network.beforeRequestSent", {"request": {"request": "req3"}, "isBlocked": False, "intercepts": ["i1"]})
    await asyncio.sleep(0.01)
    assert sent(bidi, "network.continueRequest") == [{"request": "req2"}]
    assert sent(bidi, "network.failRequest") == []


async def test_unroute_removes_intercept_and_stops_answering(fake_bidi):
    b, bidi = await connected(fake_bidi)
    FakeBiDi.replies["network.addIntercept"] = {"intercept": "i1"}
    await b.network_route("http://localhost/data.json", "abort", 200, None, "application/json")
    await b.network_unroute("route1")
    assert sent(bidi, "network.removeIntercept") == [{"intercept": "i1"}]
    bidi.emit("network.beforeRequestSent", {"request": {"request": "req1"}, "isBlocked": True, "intercepts": ["i1"]})
    await asyncio.sleep(0.01)
    assert sent(bidi, "network.continueRequest") == [{"request": "req1"}]


async def test_unroute_unknown_id_and_unroute_all(fake_bidi):
    b, bidi = await connected(fake_bidi)
    FakeBiDi.replies["network.addIntercept"] = [{"intercept": "i1"}, {"intercept": "i2"}]
    await b.network_route("http://localhost/a", "abort", 200, None, "application/json")
    await b.network_route("http://localhost/b", "abort", 200, None, "application/json")
    with pytest.raises(BrowserError, match="Unknown route"):
        await b.network_unroute("route9")
    await b.network_unroute()
    assert sent(bidi, "network.removeIntercept") == [{"intercept": "i1"}, {"intercept": "i2"}]
    assert await b.network_unroute() == "No routes to remove."


async def test_routes_are_restored_on_reconnect(fake_bidi):
    b, bidi = await connected(fake_bidi)
    FakeBiDi.replies["network.addIntercept"] = {"intercept": "i1"}
    await b.network_route("http://localhost/data.json", "abort", 200, None, "application/json")
    await b.close()
    await b.connect(9222)
    second = fake_bidi.instances[1]
    assert len(sent(second, "network.addIntercept")) == 1
    second.emit("network.beforeRequestSent", {"request": {"request": "req1"}, "isBlocked": True, "intercepts": ["i1"]})
    await asyncio.sleep(0.01)
    assert sent(second, "network.failRequest") == [{"request": "req1"}]


async def test_route_rejected_by_zen_is_not_kept(fake_bidi):
    b, _ = await connected(fake_bidi)
    fake_bidi.fail = lambda m, p: BiDiError("invalid argument", "bad") if m == "network.addIntercept" else None
    with pytest.raises(BrowserError, match="Zen rejected"):
        await b.network_route("http://localhost/data.json", "abort", 200, None, "application/json")
    assert await b.network_unroute() == "No routes to remove."


# -- privacy, history url, allowlist for network_route ----------------------


async def test_browser_batch_audit_redacts_each_step(fake, tmp_path):
    fake.cfg.allow_scripts = True
    await server.browser_batch(
        steps=[
            {"tool": "type", "args": {"ref": "r1", "text": "hunter2"}},
            {"tool": "javascript_tool", "args": {"code": "secretCode()"}},
            {"tool": "handle_dialog", "args": {"text": "pw-answer"}},
        ]
    )
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "hunter2" not in raw
    assert "secretCode" not in raw
    assert "pw-answer" not in raw
    batch = json.loads(raw.splitlines()[-1])
    assert batch["tool"] == "browser_batch"
    assert batch["params"]["steps"][0]["args"] == {"ref": "r1", "text": {"chars": 7}}
    assert batch["params"]["steps"][1]["args"] == {"code": {"chars": len("secretCode()")}}
    assert server.HISTORY.last().args["steps"][2]["args"] == {"text": {"chars": 9}}


async def test_javascript_tool_audit_redacts_code(fake, tmp_path):
    fake.cfg.allow_scripts = True
    await server.javascript_tool(code="secretCode()")
    assert "secretCode" not in (tmp_path / "audit.jsonl").read_text()
    assert last_audit(tmp_path)["params"]["code"] == {"chars": len("secretCode()")}


async def test_history_records_tab_url_after_action(fake):
    await server.press_key(keys="Enter")
    assert server.HISTORY.last().url == "http://localhost/page"


async def test_history_url_is_none_when_lookup_fails(fake):
    async def boom(context):
        raise BrowserError("tab gone")

    fake.url_of = boom
    await server.press_key(keys="Enter")
    assert server.HISTORY.last().url is None
    assert server.HISTORY.last().ok is True


async def test_network_route_rejects_host_not_on_allowlist(fake):
    fake.cfg.prompts = False
    with pytest.raises(ToolError, match="not on the zenpilot allowlist"):
        await server.network_route(url_pattern="http://evil.example/api?x=1")
    assert fake.calls == []


async def test_network_route_body_json_string_through_mcp_call(fake):
    await server.mcp.call_tool(
        "network_route",
        {"url_pattern": "http://localhost/x", "action": "respond", "body": '{"ok": true}'},
    )
    assert fake.calls[-1] == ("route", "http://localhost/x", "respond", 200, '{"ok": true}', "application/json")
