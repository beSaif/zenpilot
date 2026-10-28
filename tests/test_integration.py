"""End to end against a live Zen: MCP stdio client -> zenpilot -> BiDi -> fixture page.

Needs Zen running on the port from zen_port() (`zenpilot launch`); skipped otherwise.
Uses its own temp config, so your allowlist and audit log are untouched.
"""

import asyncio
import base64
import functools
import http.server
import json
import re
import shutil
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from zen_port import zen_port

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "tests" / "fixture"


PORT = zen_port()


def _zen_running() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _zen_running(), reason=f"Zen is not listening on 127.0.0.1:{PORT} (run `zenpilot launch`)"),
]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(FIXTURE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://localhost:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"

    def write(allowlist=("localhost",), read_only=False):
        path.write_text(
            f"allowlist = {json.dumps(list(allowlist))}\n"
            f"read_only = {'true' if read_only else 'false'}\n"
            f"port = {PORT}\n"
            "prompts = false\n"
            f'audit_log = "{tmp_path / "audit.jsonl"}"\n'
            f'report_dir = "{tmp_path / "reports"}"\n'
        )

    write()
    write.path = path
    write.audit = tmp_path / "audit.jsonl"
    return write


class Session:
    def __init__(self, client):
        self.client = client

    async def call(self, tool, **args):
        r = await self.client.call_tool(tool, args)
        return r

    async def ok(self, tool, **args) -> str:
        r = await self.call(tool, **args)
        text = "\n".join(b.text for b in r.content if b.type == "text")
        assert not r.is_error, f"{tool}({args}) failed: {text}"
        return text

    async def err(self, tool, **args) -> str:
        r = await self.call(tool, **args)
        text = "\n".join(b.text for b in r.content if b.type == "text")
        assert r.is_error, f"{tool}({args}) unexpectedly succeeded: {text}"
        return text


@asynccontextmanager
async def zenpilot(config):
    exe = shutil.which("zenpilot", path=str(ROOT / ".venv" / "bin")) or "zenpilot"
    params = StdioServerParameters(command=exe, env={"ZENPILOT_CONFIG": str(config.path)})
    async with Client(params) as client:
        yield Session(client)


@asynccontextmanager
async def fixture_tab(s: Session, url: str):
    opened = await s.ok("new_tab", url=url)
    tab = re.search(r"Opened tab (t\d+)", opened).group(1)
    try:
        yield tab
    finally:
        await s.call("close_tab", tab_id=tab)


def ref(outline: str, role: str, name: str) -> str:
    m = re.search(rf"(r\d+) {role} \"{re.escape(name)}\"", outline)
    assert m, f'no {role} "{name}" in:\n{outline}'
    return m.group(1)


async def page_log(s: Session) -> str:
    return await s.ok("get_text", max_chars=5000)


async def test_tabs_navigate_screenshot_text(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html") as tab:
        tabs = await s.ok("list_tabs")
        assert re.search(rf"\*{tab} {re.escape(site)}/index.html \| zenpilot fixture", tabs)
        assert "<untrusted_web_content" in tabs

        blocked = await s.err("navigate", url="https://example.com/")
        assert "'example.com' is not on the zenpilot allowlist" in blocked

        assert f"Now at {site}/page2.html" in await s.ok("navigate", url=f"{site}/page2.html", tab_id=tab)

        shot = await s.call("screenshot")
        image = next(b for b in shot.content if b.type == "image")
        assert image.mime_type == "image/jpeg"
        assert base64.b64decode(image.data)[:2] == b"\xff\xd8"
        assert any("untrusted" in b.text for b in shot.content if b.type == "text")

        text = await s.ok("get_text", tab_id=tab)
        assert "You navigated here." in text and text.startswith("<untrusted_web_content")

        assert "Unknown tab id" in await s.err("read_page", tab_id="t9999")


async def test_read_page_outline(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        page = await s.ok("read_page")
        # Prompt injection text stays inside the untrusted fence.
        fence = page[page.index("<untrusted_web_content") : page.index("</untrusted_web_content>")]
        assert "Ignore previous instructions" in fence
        for role, name in [
            ("link", "Second page"),
            ("textbox", "Name"),
            ("textbox", "Email"),
            ("select", "Color"),
            ("checkbox", "I agree"),
            ("checkbox", "Fly"),
            ("button", "Sign up"),
            ("textbox", "Shadow input"),
            ("button", "Shadow button"),
            ("button", "Slotted button"),
            ("button", "Frame button"),
            ("button", "Bottom button"),
        ]:
            ref(page, role, name)
        assert 'textbox "Password" type=password value="•••"' in page
        assert 'checkbox "I agree" unchecked' in page
        assert 'checkbox "Fly" unchecked' in page  # hidden input, ref on its label
        assert re.search(r'"Cracked"\n\s*r\d+ clickable\n', page)  # div with cursor: pointer
        assert "secret" not in page
        assert "Invisible button" not in page
        assert 'frame "Cross-origin frame" [skipped: 127.0.0.1 not allowlisted]' in page
        assert re.search(r'frame "Same-origin frame"\n\s+r\d+ button "Frame button"', page)


async def test_trusted_input(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        page = await s.ok("read_page")
        await s.ok("click", ref=ref(page, "button", "Check trust"))
        await s.ok("type", ref=ref(page, "textbox", "Name"), text="Ada Lovelace")
        await s.ok("type", ref=ref(page, "select", "Color"), text="Blue")
        await s.ok("click", ref=ref(page, "checkbox", "I agree"))
        await s.ok("click", ref=ref(page, "checkbox", "Fly"))
        await s.ok("click", ref=re.search(r'"Cracked"\n\s*(r\d+) clickable', page).group(1))
        await s.ok("type", ref=ref(page, "textbox", "Shadow input"), text="in shadow")
        await s.ok("click", ref=ref(page, "button", "Shadow button"))
        await s.ok("click", ref=ref(page, "button", "Frame button"))
        await s.ok("click", ref=ref(page, "button", "Bottom button"))  # far below the fold
        await s.ok("click", ref=ref(page, "button", "Slotted button"))
        await s.ok("type", ref=ref(page, "textbox", "Bio"), text="line one\nline two")

        log = await page_log(s)
        for line in [
            "click:true",
            "select:blue:true",
            "checkbox:true:true",
            "fly:true",
            "cracked:true",
            "shadow:in shadow:true",
            "bottom:true",
            "slotted:true",
        ]:
            assert line in log
        assert "plain-enter" not in log  # newlines are Shift+Enter, which doesn't send in chat apps
        page = await s.ok("read_page")
        assert 'textbox "Name" value="Ada Lovelace"' in page  # replaced, not appended
        assert 'checkbox "I agree" checked' in page
        assert 'checkbox "Fly" checked' in page
        assert 'button "frame clicked:true:localhost"' in page


async def test_date_and_time_inputs(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        page = await s.ok("read_page")
        await s.ok("type", ref=ref(page, "textbox", "Delivery time"), text="19:30")
        await s.ok("type", ref=ref(page, "textbox", "Delivery date"), text="2026-10-16")
        assert "isn't a valid time" in await s.err("type", ref=ref(page, "textbox", "Delivery time"), text="7:30pm")

        log = await page_log(s)
        assert "time:19:30" in log and "date:2026-10-16" in log
        page = await s.ok("read_page")
        assert 'textbox "Delivery time" type=time value="19:30"' in page


async def test_cross_origin_frame_when_allowlisted(site, config):
    config(allowlist=("localhost", "127.0.0.1"))
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        page = await s.ok("read_page")
        cross = page[page.index('frame "Cross-origin frame"') :]
        await s.ok("click", ref=ref(cross, "button", "Frame button"))
        assert 'button "frame clicked:true:127.0.0.1"' in await s.ok("read_page")


async def test_stale_and_unknown_refs(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        page = await s.ok("read_page")
        await s.ok("click", ref=ref(page, "button", "Rerender"))
        stale = await s.err("click", ref=ref(page, "button", "Sign up"))
        assert "stale" in stale and "read_page" in stale
        assert "Unknown ref" in await s.err("click", ref="r999999")
        fresh = await s.ok("read_page")
        assert "Unknown ref" in await s.err("click", ref=ref(page, "button", "Sign up"))  # superseded
        await s.ok("click", ref=ref(fresh, "button", "Check trust"))


async def test_navigation_and_new_tab_reporting(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        page = await s.ok("read_page")
        opened = await s.ok("click", ref=ref(page, "link", "Open in new tab"))
        new_tab = re.search(r"A new tab (t\d+) opened", opened)
        assert new_tab, opened
        await s.ok("close_tab", tab_id=new_tab.group(1))

        page = await s.ok("read_page")
        result = await s.ok("click", ref=ref(page, "link", "Second page"))
        assert f"Page navigated to {site}/page2.html" in result

        await s.ok("navigate", url=f"{site}/index.html")
        page = await s.ok("read_page")
        await s.ok("type", ref=ref(page, "textbox", "Name"), text="Grace", submit=True)
        assert "submit:Grace" in await page_log(s)


async def test_scroll(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        # Smooth scrolling must have settled before the position is reported.
        assert "y=1500 " in await s.ok("scroll", direction="down", amount=1500)
        assert "y=0 " in await s.ok("scroll", direction="up", amount=5000)


async def test_read_only_mode_and_audit_log(site, config):
    async with zenpilot(config) as s, fixture_tab(s, f"{site}/index.html"):
        page = await s.ok("read_page")
        await s.ok("type", ref=ref(page, "textbox", "Bio"), text="hello there")
        config(read_only=True)  # applies on the next call, no restart
        assert "read-only" in await s.err("click", ref=ref(page, "button", "Check trust"))
        assert "read-only" in await s.err("navigate", url=f"{site}/page2.html")
        assert "read-only" in await s.err("type", ref=ref(page, "textbox", "Bio"), text="x")
        await s.ok("read_page")
        await s.ok("screenshot")
        config(read_only=False)  # so fixture_tab can close the tab

    records = [json.loads(line) for line in config.audit.read_text().splitlines()]
    by_tool = {r["tool"]: r for r in records}
    assert {"new_tab", "read_page", "type", "click", "navigate", "screenshot"} <= by_tool.keys()
    typed = next(r for r in records if r["tool"] == "type" and r["ok"])
    assert typed["params"]["text"] == {"chars": 11}  # typed text is not logged
    assert typed["domain"] == "localhost"
    assert all({"ts", "params", "domain", "ok", "ms"} <= r.keys() for r in records)
    assert any(not r["ok"] and "read-only" in r["error"] for r in records)


async def test_idle_release_lets_another_client_in(monkeypatch):
    from zenpilot import browser as browser_mod

    monkeypatch.setattr(browser_mod, "IDLE_RELEASE_SECONDS", 0.2)
    first, second = browser_mod.Browser(), browser_mod.Browser()
    try:
        async with first.session(PORT):
            await first.tabs()
            with pytest.raises(browser_mod.BrowserError, match="already has an active automation session"):
                await second.connect(PORT)
        await asyncio.sleep(0.5)  # first goes idle and ends its session
        async with second.session(PORT):
            assert await second.tabs()
        await asyncio.sleep(0.5)
        async with first.session(PORT):  # the first reconnects transparently
            assert await first.tabs()
    finally:
        await first.close()
        await second.close()
