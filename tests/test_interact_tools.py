"""Scripts, keys, viewport, dialogs, file upload and HAR path rules, against fakes."""

import asyncio
import contextlib
import json
import tempfile

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zenpilot import browser as browser_mod
from zenpilot import server
from zenpilot.browser import ENTER, Browser, BrowserError, parse_keys
from zenpilot.devtools import Devtools
from zenpilot.safety import Config, load_config
from zenpilot.tree import Ref

CTRL, SHIFT, META = "", "", ""


class FakeBiDi:
    instances: list = []
    fail = None
    replies: dict = {}

    def __init__(self, url):
        self.url = url
        self.connected = False
        self.sent: list[tuple[str, dict]] = []
        self.timeouts: list[tuple[str, float]] = []
        self.handlers: dict[str, list] = {}
        FakeBiDi.instances.append(self)

    async def connect(self):
        self.connected = True
        return {}

    async def send(self, method, params=None, timeout=30):
        params = params or {}
        self.sent.append((method, params))
        self.timeouts.append((method, timeout))
        if FakeBiDi.fail and (exc := FakeBiDi.fail(method, params)):
            raise exc
        return FakeBiDi.replies.get(method, {})

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
    FakeBiDi.fail = None
    FakeBiDi.replies = {}
    monkeypatch.setattr(browser_mod, "BiDi", FakeBiDi)
    return FakeBiDi


def sent(bidi, method):
    return [p for m, p in bidi.sent if m == method]


async def connected_browser(fake_bidi):
    b = Browser()
    await b.connect(9222)
    return b, fake_bidi.instances[0]


# -- browser level ----------------------------------------------------------


async def test_connect_subscribes_to_user_prompts(fake_bidi):
    await connected_browser(fake_bidi)
    first = fake_bidi.instances[0].sent[0]
    assert first[0] == "session.subscribe"
    assert "browsingContext.userPromptOpened" in first[1]["events"]


def test_parse_keys_names_and_chords():
    assert parse_keys("Enter") == [ENTER]
    assert parse_keys("ctrl+a") == [(CTRL, "a")]
    assert parse_keys("cmd+shift+k") == [(META, SHIFT, "k")]
    assert parse_keys("ArrowDown Tab") == ["", ""]
    assert parse_keys("a") == ["a"]


@pytest.mark.parametrize("spec", ["", "   ", "Foo", "ctrl+Foo", "ctrl+"])
def test_parse_keys_rejects_bad_input(spec):
    with pytest.raises(BrowserError):
        parse_keys(spec)


async def test_press_sends_key_actions_in_order(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    await b.press({"context": "c1"}, parse_keys("ctrl+a"))
    action = sent(bidi, "input.performActions")[-1]
    assert action["context"] == "c1"
    chunk = action["actions"][0]["actions"]
    assert chunk == [
        {"type": "keyDown", "value": CTRL},
        {"type": "keyDown", "value": "a"},
        {"type": "keyUp", "value": "a"},
        {"type": "keyUp", "value": CTRL},
    ]


async def test_evaluate_describes_result(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    fake_bidi.replies["script.evaluate"] = {"type": "success", "result": {"type": "number", "value": 2}}
    assert await b.evaluate({"context": "c1"}, "1 + 1") == "2"
    params = sent(bidi, "script.evaluate")[-1]
    assert params["expression"] == "1 + 1"
    assert params["target"] == {"context": "c1"}
    assert params["awaitPromise"] is True
    assert params["resultOwnership"] == "none"


@pytest.mark.parametrize(
    ("remote", "text"),
    [
        ({"type": "string", "value": "hi"}, "hi"),
        ({"type": "null"}, "null"),
        ({"type": "undefined"}, "undefined"),
        ({"type": "boolean", "value": True}, "true"),
        (
            {"type": "array", "value": [{"type": "number", "value": 1}, {"type": "string", "value": "a"}]},
            '[1, "a"]',
        ),
        ({"type": "object", "value": [["k", {"type": "boolean", "value": True}]]}, '{"k": true}'),
        ({"type": "node", "sharedId": "s1", "value": {"localName": "button"}}, "<button>"),
        ({"type": "function"}, "[function]"),
    ],
)
async def test_evaluate_serializes_remote_values(fake_bidi, remote, text):
    b, _ = await connected_browser(fake_bidi)
    fake_bidi.replies["script.evaluate"] = {"type": "success", "result": remote}
    assert await b.evaluate({"context": "c1"}, "x") == text


async def test_evaluate_exception_is_browser_error(fake_bidi):
    b, _ = await connected_browser(fake_bidi)
    fake_bidi.replies["script.evaluate"] = {
        "type": "exception",
        "exceptionDetails": {"text": "ReferenceError: x is not defined"},
    }
    with pytest.raises(BrowserError, match="x is not defined"):
        await b.evaluate({"context": "c1"}, "x")


async def test_resize_sends_viewport(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    await b.resize({"context": "c1"}, 800, 600)
    assert sent(bidi, "browsingContext.setViewport")[-1] == {
        "context": "c1",
        "viewport": {"width": 800, "height": 600},
    }


async def test_prompt_event_is_tracked_and_cleared_on_handle(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    bidi.emit("browsingContext.userPromptOpened", {"context": "c1", "type": "alert", "message": "hi"})
    assert b.dialog_note("c1") == "dialog open: alert hi"
    assert b.dialog_note("c2") is None
    assert await b.handle_dialog({"context": "c1"}, False, None) == ("alert", "hi")
    params = sent(bidi, "browsingContext.handleUserPrompt")[-1]
    assert params == {"context": "c1", "accept": False}
    assert b.dialog_note("c1") is None


async def test_prompt_closed_event_clears_the_dialog(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    assert "browsingContext.userPromptClosed" in bidi.sent[0][1]["events"]
    bidi.emit("browsingContext.userPromptOpened", {"context": "c1", "type": "alert", "message": "hi"})
    bidi.emit("browsingContext.userPromptClosed", {"context": "c1", "accepted": True})
    assert b.dialog_note("c1") is None


async def test_handle_dialog_sends_prompt_text(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    await b.handle_dialog({"context": "c1"}, True, "answer")
    assert sent(bidi, "browsingContext.handleUserPrompt")[-1] == {
        "context": "c1",
        "accept": True,
        "userText": "answer",
    }


async def test_handle_dialog_without_dialog_is_browser_error(fake_bidi):
    from zenpilot.bidi import BiDiError

    b, _ = await connected_browser(fake_bidi)

    def fail(method, params):
        return BiDiError("no such alert", "none") if method == "browsingContext.handleUserPrompt" else None

    fake_bidi.fail = fail
    with pytest.raises(BrowserError, match="No dialog"):
        await b.handle_dialog({"context": "c1"}, True, None)


async def test_file_upload_sets_files_on_file_input(fake_bidi, tmp_path):
    b, bidi = await connected_browser(fake_bidi)
    b.refs.add(Ref("t1", "c1", "s1"))
    fake_bidi.replies["script.callFunction"] = {"type": "success", "result": {"type": "boolean", "value": True}}
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert await b.file_upload("r1", [str(f)]) == "Uploaded 1 file(s) to r1."
    assert sent(bidi, "input.setFiles")[-1] == {
        "context": "c1",
        "element": {"sharedId": "s1"},
        "files": [str(f)],
    }


async def test_file_upload_rejects_non_file_input(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    b.refs.add(Ref("t1", "c1", "s1"))
    fake_bidi.replies["script.callFunction"] = {"type": "success", "result": {"type": "boolean", "value": False}}
    with pytest.raises(BrowserError, match="not a file input"):
        await b.file_upload("r1", ["/tmp/x"])
    assert not sent(bidi, "input.setFiles")


async def test_click_on_a_dialog_opener_returns_a_hint_instead_of_hanging(fake_bidi):
    b, bidi = await connected_browser(fake_bidi)
    b.refs.add(Ref("t1", "c1", "s1"))
    fake_bidi.replies["script.callFunction"] = {
        "type": "success",
        "result": {
            "type": "array",
            "value": [{"type": "string", "value": v} for v in ("ok", "BUTTON", "", "")],
        },
    }
    fake_bidi.fail = lambda method, params: TimeoutError() if method == "input.performActions" else None
    assert await b.click("r1") == "Action sent; page may be showing a dialog — use handle_dialog"
    assert ("input.performActions", 5) in bidi.timeouts


# -- server level -----------------------------------------------------------


class FakeInteract:
    connected = True

    def __init__(self):
        self.devtools = Devtools()
        self.devtools_error = None
        self.calls: list = []
        self.dialog = False

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

    async def evaluate(self, tab, code):
        self.calls.append(("evaluate", code))
        return "2"

    async def press(self, tab, keys):
        self.calls.append(("press", keys))

    async def resize(self, tab, width, height):
        self.calls.append(("resize", width, height))

    async def handle_dialog(self, tab, accept, text):
        self.calls.append(("dialog", accept, text))
        return ("alert", "hi")

    async def file_upload(self, key, paths):
        self.calls.append(("upload", key, paths))
        return "ok"

    def dialog_note(self, context):
        return "dialog open: alert hi" if self.dialog else None

    async def read_page(self, tab, reason):
        return "r1 button Go"


@pytest.fixture
def zen(monkeypatch, tmp_path):
    cfg = Config(
        allowlist=["localhost"],
        read_only=False,
        allow_all_sites=False,
        port=9222,
        audit_log=tmp_path / "audit.jsonl",
        auto_launch=False,
    )
    monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)
    fake = FakeInteract()
    monkeypatch.setattr(server, "browser", fake)
    return fake


def audit_records(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]


async def test_javascript_tool_is_off_by_default(zen):
    with pytest.raises(ToolError, match="allow_scripts = true"):
        await server.javascript_tool(code="1 + 1")
    assert zen.calls == []


async def test_javascript_tool_runs_when_allowed(zen, monkeypatch, tmp_path):
    cfg = load_config(tmp_path / "c.toml")
    cfg.allow_scripts = True
    monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)
    out = await server.javascript_tool(code="1 + 1")
    assert "2" in out
    assert "untrusted_web_content" in out
    assert zen.calls == [("evaluate", "1 + 1")]


async def test_press_key_rejects_unknown_key(zen):
    with pytest.raises(ToolError, match="Unknown key"):
        await server.press_key(keys="Hyper")


async def test_press_key_passes_parsed_keys(zen):
    await server.press_key(keys="Enter ctrl+a")
    assert zen.calls == [("press", [ENTER, (CTRL, "a")])]


async def test_resize_window_checks_bounds(zen):
    with pytest.raises(ToolError, match="between 200 and 4000"):
        await server.resize_window(width=100, height=600)
    with pytest.raises(ToolError, match="between 200 and 4000"):
        await server.resize_window(width=800, height=4001)
    assert zen.calls == []


async def test_resize_window_sets_size(zen):
    out = await server.resize_window(width=800, height=600)
    assert "800x600" in out
    assert zen.calls == [("resize", 800, 600)]


async def test_handle_dialog_redacts_text_in_audit(zen, tmp_path):
    out = await server.handle_dialog(accept=True, text="s3cret")
    assert "Accepted alert: hi" in out
    assert zen.calls == [("dialog", True, "s3cret")]
    record = audit_records(tmp_path)[-1]
    assert record["params"]["text"] == {"chars": 6}
    assert "s3cret" not in json.dumps(record)


async def test_handle_dialog_without_text_audits_cleanly(zen, tmp_path):
    await server.handle_dialog(accept=False, text=None)
    record = audit_records(tmp_path)[-1]
    assert record["ok"] is True
    assert record["params"]["text"] is None


async def test_read_page_reports_open_dialog(zen):
    zen.dialog = True
    out = await server.read_page()
    assert "dialog open: alert hi" in out


async def test_file_upload_rejects_directory(zen, tmp_path):
    with pytest.raises(ToolError, match="regular file"):
        await server.file_upload(ref="r1", paths=[str(tmp_path)])
    assert zen.calls == []


async def test_file_upload_passes_resolved_paths(zen, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    await server.file_upload(ref="r1", paths=[str(f)])
    assert zen.calls == [("upload", "r1", [str(f.resolve())])]


def har_setup(monkeypatch, tmp_path, zen):
    home = tmp_path / "home"
    temp = tmp_path / "tmp"
    home.mkdir()
    temp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    zen.devtools.on_before_request(
        {
            "context": "c1",
            "redirectCount": 0,
            "timestamp": 1000,
            "request": {"request": "r1", "url": "http://localhost/a", "method": "GET", "headers": []},
        }
    )
    return home, temp


async def test_har_export_rejects_path_outside_allowed_dirs(zen, monkeypatch, tmp_path):
    home, _ = har_setup(monkeypatch, tmp_path, zen)
    with pytest.raises(ToolError, match="can only be saved under"):
        await server.har_export(path=str(home / "x.har"), include_bodies=False)


async def test_har_export_rejects_non_har_suffix(zen, monkeypatch, tmp_path):
    _, temp = har_setup(monkeypatch, tmp_path, zen)
    with pytest.raises(ToolError, match=r"\.har"):
        await server.har_export(path=str(temp / "x.json"), include_bodies=False)


async def test_har_export_never_overwrites(zen, monkeypatch, tmp_path):
    _, temp = har_setup(monkeypatch, tmp_path, zen)
    target = temp / "x.har"
    target.write_text("keep me")
    with pytest.raises(ToolError, match="already exists"):
        await server.har_export(path=str(target), include_bodies=False)
    assert target.read_text() == "keep me"


async def test_har_export_allows_temp_dir_and_state_dir(zen, monkeypatch, tmp_path):
    home, temp = har_setup(monkeypatch, tmp_path, zen)
    out = await server.har_export(path=str(temp / "ok.har"), include_bodies=False)
    assert str((temp / "ok.har").resolve()) in out
    state = home / ".local/state/zenpilot/har"
    out = await server.har_export(path=str(state / "b.har"), include_bodies=False)
    assert (state / "b.har").exists()
    assert "Wrote 1 entries" in out


async def test_har_default_path_still_under_state_dir(zen, monkeypatch, tmp_path):
    home, _ = har_setup(monkeypatch, tmp_path, zen)
    out = await server.har_export(include_bodies=False)
    written = out.split(" to ", 1)[1].rstrip(".")
    assert written.startswith(str(home / ".local/state/zenpilot/har"))


# -- config -------------------------------------------------------------------


def test_allow_scripts_defaults_off_and_parses(tmp_path):
    path = tmp_path / "config.toml"
    assert load_config(path).allow_scripts is False
    assert "allow_scripts = false" in path.read_text()
    path.write_text('allow_scripts = true\nallowlist = ["localhost"]\n')
    assert load_config(path).allow_scripts is True
    path.write_text('allow_scripts = "yes"\n')
    assert load_config(path).allow_scripts is False


async def test_handle_dialog_returns_metadata_when_closed_event_arrives_first(fake_bidi):
    # The userPromptClosed event can reach us before the handleUserPrompt response; the metadata must survive it.
    b, bidi = await connected_browser(fake_bidi)
    bidi.emit("browsingContext.userPromptOpened", {"context": "c1", "type": "prompt", "message": "name?"})
    real_send = bidi.send

    async def send_with_closed_first(method, params=None, timeout=30):
        if method == "browsingContext.handleUserPrompt":
            bidi.emit("browsingContext.userPromptClosed", {"context": "c1", "accepted": True})
        return await real_send(method, params, timeout)

    bidi.send = send_with_closed_first
    assert await b.handle_dialog({"context": "c1"}, True, "ana") == ("prompt", "name?")
    assert b.dialog_note("c1") is None
