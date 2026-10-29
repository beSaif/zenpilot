"""Tool wrappers: audit records, allowlist hints and redirects, with the browser faked."""

import json
from contextlib import asynccontextmanager

import pytest

from zenpilot import server
from zenpilot.safety import Config


class FakeBrowser:
    connected = True
    stopped = False

    def __init__(self):
        self.tabs_list = [
            {"id": "t1", "context": "c1", "url": "https://ok.com/form", "title": "Form", "active": True},
            {"id": "t2", "context": "c2", "url": "https://mail.example.com/", "title": "Mail", "active": False},
        ]
        self.landing = "https://ok.com/done"
        self.wait_result = True

    @asynccontextmanager
    async def session(self, port):
        yield

    async def tabs(self, probe=True):
        return self.tabs_list

    async def tab(self, tab_id):
        return next(t for t in self.tabs_list if t["id"] == (tab_id or "t1"))

    def ref_key(self, key):
        return 'button "Apply"' if key == "r1" else None

    def ref(self, key):
        return key

    def top_context(self, ref):
        return "c1"

    async def mark(self, context, action=None):
        self.marked = context
        self.actions = [*getattr(self, "actions", []), action]

    async def ref_url(self, key):
        return "https://ok.com/form"

    async def click(self, key):
        return f"Clicked {key}."

    async def hover(self, key):
        return f"Hovered {key}."

    async def press(self, tab, spec):
        return f"Pressed {spec}."

    async def wait_for(self, tab, *, text, key, url_contains, timeout_ms, allowed):
        self.waited = {"text": text, "url_contains": url_contains, "timeout_ms": timeout_ms}
        allowed(self.landing)  # a redirect during the wait is checked like any navigation
        return self.wait_result

    async def navigate(self, tab, url):
        return self.landing, False


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(["ok.com"], False, False, 9222, tmp_path / "audit.jsonl", auto_launch=False)
    config.report_dir = tmp_path / "reports"
    monkeypatch.setattr(server, "load_config", lambda: Config(**vars(config) | {"allowlist": list(config.allowlist)}))
    monkeypatch.setattr(server, "browser", FakeBrowser())
    monkeypatch.setattr(server, "_declined", {})
    monkeypatch.setattr(server, "_stop_told", False)
    answers = []
    monkeypatch.setattr(server.dialogs, "ask", lambda message, buttons, default: answers.pop(0))
    return config, answers


def records(config):
    return [json.loads(line) for line in config.audit_log.read_text().splitlines()]


async def test_audit_names_the_element_a_ref_was(env):
    config, _ = env
    await server.click(ref="r1")
    assert records(config)[-1]["params"] == {"ref": "r1", "element": 'button "Apply"'}


async def test_list_tabs_says_a_blocked_tab_can_be_asked_for(env):
    config, _ = env
    out = await server.list_tabs()
    assert "t2 [not allowlisted: mail.example.com; using it asks the user to allow it]" in out
    config.prompts = False
    assert "t2 [not allowlisted: mail.example.com; ask the user]" in await server.list_tabs()


async def test_redirect_to_a_blocked_site_asks_the_user(env):
    config, answers = env
    server.browser.landing = "https://wayf.switch.ch/login"
    answers.append("Allow for 12 Hours")
    assert await server.navigate(url="https://ok.com/login") == "Now at https://wayf.switch.ch/login."
    assert records(config)[-1]["ok"] is True

    server.browser.landing = "https://idp.evil.com/"
    answers.append("Don't Allow")
    with pytest.raises(server.ToolError) as e:
        await server.navigate(url="https://ok.com/login")
    assert "The page loaded but ended up on a site that isn't allowed" in str(e.value)
    assert "didn't allow zenpilot to use 'idp.evil.com'" in str(e.value)
    assert records(config)[-1]["domain"] == "idp.evil.com"


async def test_dialog_timeout_says_nobody_answered(env):
    config, answers = env
    server.browser.landing = "https://idp.example.net/"
    answers.append(None)
    with pytest.raises(server.ToolError, match="Nobody answered the dialog asking to allow 'idp.example.net'"):
        await server.navigate(url="https://ok.com/login")


async def test_stop_refuses_once_then_asks_to_continue(env):
    config, answers = env
    server.browser.stopped = True
    with pytest.raises(server.ToolError, match="pressed Stop"):
        await server.click(ref="r1")
    assert answers == []  # the first call after Stop is refused without asking
    answers.append("Keep Stopped")
    with pytest.raises(server.ToolError, match="pressed Stop"):
        await server.click(ref="r1")
    answers.append("Let It Continue")
    assert await server.click(ref="r1") == "Clicked r1."
    assert server.browser.stopped is False and server.browser.marked == "c1"


async def test_the_badge_says_what_zenpilot_is_doing_but_not_what_it_types(env):
    await server.click(ref="r1")
    assert server.browser.actions[-2:] == ["Clicking \u201cApply\u201d", "Clicked \u201cApply\u201d"]
    await server.navigate(url="https://ok.com/done")
    assert server.browser.actions[-1] == "Opened ok.com"
    server._call.set(("type", {"ref": "r1", "text": "my secret"}))
    assert "secret" not in server._action()


async def test_press_key_asks_before_acting_on_a_blocked_tab(env):
    config, answers = env
    answers.append("Don't Allow")
    with pytest.raises(server.ToolError, match="didn't allow zenpilot to use 'mail.example.com'"):
        await server.press_key(keys="Enter", tab_id="t2")
    assert records(config)[-1]["domain"] == "mail.example.com"


async def test_press_key_is_audited_with_its_keys_redacted(env):
    config, _ = env
    assert await server.press_key(keys="ctrl+a") == "Pressed ctrl+a."
    assert records(config)[-1]["params"] == {"keys": {"chars": 6}}


async def test_hover_and_press_key_are_blocked_in_read_only_mode(env, monkeypatch):
    config, _ = env
    read_only = Config(["ok.com"], True, False, 9222, config.audit_log, auto_launch=False)
    monkeypatch.setattr(server, "load_config", lambda: read_only)
    with pytest.raises(server.ToolError, match="read-only"):
        await server.press_key(keys="Enter")
    with pytest.raises(server.ToolError, match="read-only"):
        await server.hover(ref="r1")
    assert await server.wait_for(text="Saved") == "Condition met in t1."


async def test_hover_and_press_key_say_what_they_do_on_the_badge(env):
    await server.hover(ref="r1")
    assert server.browser.actions[-2:] == ["Hovering “Apply”", "Hovered “Apply”"]
    await server.press_key(keys="h i")
    assert server.browser.actions[-1] == "Pressed keys"


async def test_wait_for_checks_the_site_on_every_poll(env):
    config, answers = env
    server.browser.landing = "https://mail.example.com/"
    answers.append("Don't Allow")
    with pytest.raises(server.ToolError, match="didn't allow zenpilot to use 'mail.example.com'"):
        await server.wait_for(url_contains="done")


async def test_wait_for_says_when_it_times_out(env):
    server.browser.wait_result = False
    with pytest.raises(server.ToolError, match='Timed out after 100 ms waiting for text "Saved"'):
        await server.wait_for(text="Saved", timeout_ms=1)
    with pytest.raises(server.ToolError, match="Give text, ref or url_contains to wait for"):
        await server.wait_for()
