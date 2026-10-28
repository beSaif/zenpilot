"""Tool wrappers: audit records, allowlist hints and redirects, with the browser faked."""

import json
from contextlib import asynccontextmanager

import pytest

from zenpilot import server
from zenpilot.safety import Config


class FakeBrowser:
    connected = True

    def __init__(self):
        self.tabs_list = [
            {"id": "t1", "context": "c1", "url": "https://ok.com/form", "title": "Form", "active": True},
            {"id": "t2", "context": "c2", "url": "https://mail.example.com/", "title": "Mail", "active": False},
        ]
        self.landing = "https://ok.com/done"

    @asynccontextmanager
    async def session(self, port):
        yield

    async def tabs(self, probe=True):
        return self.tabs_list

    async def tab(self, tab_id):
        return next(t for t in self.tabs_list if t["id"] == (tab_id or "t1"))

    def ref_key(self, key):
        return 'button "Apply"' if key == "r1" else None

    async def ref_url(self, key):
        return "https://ok.com/form"

    async def click(self, key):
        return f"Clicked {key}."

    async def navigate(self, tab, url):
        return self.landing, False


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(["ok.com"], False, False, 9222, tmp_path / "audit.jsonl", auto_launch=False)
    config.report_dir = tmp_path / "reports"
    monkeypatch.setattr(server, "load_config", lambda: Config(**vars(config) | {"allowlist": list(config.allowlist)}))
    monkeypatch.setattr(server, "browser", FakeBrowser())
    monkeypatch.setattr(server, "_declined", set())
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
