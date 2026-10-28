"""Action capture: capture_actions config, step screenshots, screenshot saving, run_report and the HAR it writes."""

import contextlib
import json
import sys
import types
from pathlib import Path

import pytest

from zenpilot import browser as browser_mod
from zenpilot import server
from zenpilot.browser import Browser, BrowserError
from zenpilot.devtools import Devtools
from zenpilot.history import History
from zenpilot.safety import DEFAULT_CONFIG, Config, load_config

JPEG = b"jpeg-bytes"


class FakeCaptureBrowser:
    connected = True

    def __init__(self):
        self.devtools = Devtools()
        self.devtools_error = None
        self.zen_version = "150.0"
        self.fail_shot = None

    @contextlib.asynccontextmanager
    async def session(self, port):
        yield

    async def tab(self, tab_id):
        return {"id": "t1", "context": "c1", "url": "http://localhost/", "active": True, "title": ""}

    async def press(self, tab, keys):
        return None

    async def get_text(self, tab, max_chars):
        return "T", "body"

    async def screenshot(self, tab, full_page=False):
        if self.fail_shot is not None:
            raise self.fail_shot
        return JPEG

    async def url_of(self, context):
        return "http://localhost/page"

    async def response_body(self, request_id):
        raise BrowserError("Zen did not keep it")


@pytest.fixture
def run(monkeypatch, tmp_path):
    cfg = Config(
        allowlist=["localhost"],
        read_only=False,
        allow_all_sites=False,
        port=9222,
        audit_log=tmp_path / "audit.jsonl",
        auto_launch=False,
        capture_actions=True,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    hist = History()
    hist.run_dir = lambda base=None: run_dir
    fb = FakeCaptureBrowser()
    monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(server, "browser", fb)
    monkeypatch.setattr(server, "HISTORY", hist)
    monkeypatch.setattr(server, "_step_shots", 0)
    return types.SimpleNamespace(cfg=cfg, browser=fb, run_dir=run_dir)


@pytest.fixture
def report_module(monkeypatch):
    """Stand-in for zenpilot.run_report, so these tests don't depend on that module's code."""
    calls = []

    def write_report(run_dir, summary, records, console, network, har_path, title, zen_version):
        calls.append(
            {
                "run_dir": run_dir,
                "summary": summary,
                "records": records,
                "console": console,
                "network": network,
                "har_path": har_path,
                "title": title,
                "zen_version": zen_version,
            }
        )
        return run_dir / "report.html", run_dir / "report.json"

    mod = types.ModuleType("zenpilot.run_report")
    mod.write_report = write_report
    monkeypatch.setitem(sys.modules, "zenpilot.run_report", mod)
    return calls


# -- config -------------------------------------------------------------------


def test_capture_actions_defaults_off(tmp_path):
    assert load_config(tmp_path / "config.toml").capture_actions is False


def test_capture_actions_true_turns_it_on(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("capture_actions = true\n")
    assert load_config(path).capture_actions is True


def test_capture_actions_needs_literal_true(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('capture_actions = "yes"\n')
    assert load_config(path).capture_actions is False


def test_default_config_documents_capture_actions():
    assert (
        "# true saves a screenshot after every action Claude takes, for run_report.\ncapture_actions = false\n"
    ) in DEFAULT_CONFIG


# -- step screenshots ---------------------------------------------------------


async def test_mutating_tool_saves_step_screenshot_on_its_record(run):
    await server.press_key(keys="Enter")
    path = run.run_dir / "step-0001.jpg"
    assert server.HISTORY.last().screenshot == str(path)
    assert path.read_bytes() == JPEG


async def test_each_step_gets_its_own_file(run):
    for _ in range(3):
        await server.press_key(keys="Enter")
    assert sorted(p.name for p in run.run_dir.glob("step-*.jpg")) == [
        "step-0001.jpg",
        "step-0002.jpg",
        "step-0003.jpg",
    ]
    assert [Path(r.screenshot).name for r in server.HISTORY.records()] == [
        "step-0001.jpg",
        "step-0002.jpg",
        "step-0003.jpg",
    ]


async def test_batch_steps_are_captured_but_the_batch_is_not(run):
    await server.browser_batch(steps=[{"tool": "press_key", "args": {"keys": "Enter"}}] * 2)
    assert sorted(p.name for p in run.run_dir.glob("*.jpg")) == ["step-0001.jpg", "step-0002.jpg"]
    assert server.HISTORY.last().screenshot is None


async def test_read_only_tool_saves_no_step_screenshot(run):
    await server.get_text()
    assert list(run.run_dir.glob("*.jpg")) == []
    assert server.HISTORY.last().screenshot is None


async def test_capture_off_saves_nothing(run):
    run.cfg.capture_actions = False
    await server.press_key(keys="Enter")
    assert list(run.run_dir.glob("*.jpg")) == []
    assert server.HISTORY.last().screenshot is None


async def test_failed_step_screenshot_never_fails_the_action(run):
    run.browser.fail_shot = BrowserError("no shot")
    assert "Pressed" in await server.press_key(keys="Enter")
    assert server.HISTORY.last().ok is True
    assert server.HISTORY.last().screenshot is None


async def test_step_screenshots_stop_at_200_per_run(run, monkeypatch):
    monkeypatch.setattr(server, "_step_shots", server.STEP_SHOTS_MAX)
    await server.press_key(keys="Enter")
    assert list(run.run_dir.glob("*.jpg")) == []
    assert server.HISTORY.last().screenshot is None


async def test_screenshot_tool_saves_its_image_when_capturing(run):
    out = await server.screenshot()
    assert len(out) == 2  # still returns the image to the model
    assert server.HISTORY.last().screenshot == str(run.run_dir / "step-0001.jpg")
    assert (run.run_dir / "step-0001.jpg").read_bytes() == JPEG


async def test_screenshot_tool_saves_nothing_when_capture_off(run):
    run.cfg.capture_actions = False
    await server.screenshot()
    assert list(run.run_dir.glob("*.jpg")) == []


# -- browser keeps the Zen version ---------------------------------------------


class CapsBiDi:
    connected = True

    def __init__(self, url):
        self.url = url

    async def connect(self):
        return {"sessionId": "s", "capabilities": {"browserVersion": "150.0.1"}}

    async def send(self, method, params=None, timeout=30):
        return {}

    def on(self, event, handler):
        pass

    async def close(self):
        pass


async def test_browser_keeps_zen_version_from_session(monkeypatch):
    monkeypatch.setattr(browser_mod, "BiDi", CapsBiDi)
    b = Browser()
    await b.connect(9222)
    assert b.zen_version == "150.0.1"


# -- run_report -----------------------------------------------------------------


async def test_run_report_passes_run_data_and_writes_har(run, report_module):
    run.browser.devtools.on_log(
        {"level": "error", "type": "console", "text": "boom", "source": {"context": "c1"}, "timestamp": 1}
    )
    run.browser.devtools.on_before_request(
        {
            "context": "c1",
            "redirectCount": 0,
            "timestamp": 1,
            "request": {"request": "n1", "url": "http://localhost/api", "method": "GET", "headers": []},
        }
    )
    await server.press_key(keys="Enter")
    text = await server.run_report(title="Smoke")
    (call,) = report_module
    assert call["run_dir"] == run.run_dir
    assert call["title"] == "Smoke"
    assert call["summary"]["total"] == 1
    assert [r.tool for r in call["records"]] == ["press_key"]
    assert call["console"][0]["text"] == "boom"
    assert call["network"][0]["id"] == "n1"
    assert call["zen_version"] == "150.0"
    assert call["har_path"] == run.run_dir / "run.har"
    assert json.loads((run.run_dir / "run.har").read_text())["log"]["version"] == "1.2"
    assert str(run.run_dir / "report.html") in text
    assert str(run.run_dir / "report.json") in text


async def test_run_report_writes_no_har_without_network(run, report_module):
    await server.run_report()
    (call,) = report_module
    assert call["har_path"] is None
    assert call["title"] is None
    assert not (run.run_dir / "run.har").exists()


async def test_run_report_works_in_read_only_mode(run, report_module):
    run.cfg.read_only = True
    await server.run_report()
    assert len(report_module) == 1


async def test_run_report_zen_version_none_when_browser_has_none(run, report_module):
    run.browser.zen_version = None
    await server.run_report()
    assert report_module[0]["zen_version"] is None


# -- ref-based tools: url and screenshot follow the ref's tab -----------------


async def test_ref_tool_records_url_and_shoots_the_refs_tab(run):
    shot_contexts, url_contexts = [], []

    async def ref_url(key):
        return "http://localhost/"

    async def click(ref):
        return "Clicked."

    async def screenshot(tab, full_page=False):
        shot_contexts.append(tab["context"])
        return JPEG

    async def url_of(context):
        url_contexts.append(context)
        return "http://localhost/ref-page"

    run.browser.ref_url = ref_url
    run.browser.ref = lambda key: key
    run.browser.top_context = lambda ref: "c-other"
    run.browser.click = click
    run.browser.screenshot = screenshot
    run.browser.url_of = url_of
    await server.click(ref="r1")
    assert server.HISTORY.last().url == "http://localhost/ref-page"
    assert url_contexts == ["c-other"]
    assert shot_contexts == ["c-other"]
