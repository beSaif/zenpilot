"""Action history: ring buffer, run summary, formatting, and recording through the tool decorator."""

import contextlib
import json
import re
from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zenpilot import server
from zenpilot.history import History, format_history
from zenpilot.safety import Config

T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)


def clock(*moments):
    """Clock returning the given datetimes in order, then the last one forever."""
    seq = list(moments) or [T0]

    def now():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return now


# -- History ------------------------------------------------------------------


def test_seq_starts_at_one_and_increments():
    h = History(now=clock(T0))
    a = h.add("find", {"query": "x"}, True, "ok", 5)
    b = h.add("click", {"ref": "r1"}, False, "boom", 7)
    assert (a.seq, b.seq) == (1, 2)
    assert h.last() is b
    assert [r.tool for r in h.records()] == ["find", "click"]


def test_ring_drops_oldest_but_seq_keeps_counting():
    h = History(max_records=3, now=clock(T0))
    for i in range(5):
        h.add("find", {"i": i}, True, str(i), 1)
    assert [r.seq for r in h.records()] == [3, 4, 5]
    assert h.add("find", {}, True, "", 1).seq == 6


def test_run_id_format_uses_utc_start_and_four_hex():
    h = History(now=clock(T0))
    assert re.fullmatch(r"20261027T120000Z-[0-9a-f]{4}", h.run_id)
    assert h.summary()["started"] == "2026-10-27T12:00:00.000+00:00"


def test_summary_counts_ok_errors_and_tools():
    h = History(now=clock(T0, T0))
    empty = h.summary()
    assert (empty["total"], empty["ok"], empty["errors"], empty["by_tool"], empty["ended"]) == (0, 0, 0, {}, None)
    h.add("find", {}, True, "", 1)
    h.add("find", {}, False, "err", 1)
    h.add("click", {}, True, "", 1)
    s = h.summary()
    assert (s["total"], s["ok"], s["errors"]) == (3, 2, 1)
    assert s["by_tool"] == {"find": 2, "click": 1}
    assert s["ended"] == h.last().ts
    assert s["run_id"] == h.run_id


def test_run_dir_is_created_under_base(tmp_path):
    h = History(now=clock(T0))
    path = h.run_dir(tmp_path / "runs")
    assert path == tmp_path / "runs" / h.run_id
    assert path.is_dir()


# -- format_history -----------------------------------------------------------


def test_format_empty_says_nothing_recorded():
    assert format_history([]) == "No actions recorded."


def test_format_line_has_seq_time_tool_status_duration_args_and_message():
    h = History(now=clock(datetime(2026, 10, 27, 9, 5, 7, tzinfo=UTC)))
    h.add("find", {"query": "save", "tab_id": "t1"}, True, "Found 2", 12)
    h.add("click", {"ref": "r3"}, False, "No such ref", 3)
    ok_line, err_line = format_history(h.records()).splitlines()
    assert ok_line == "#1 09:05:07 find ok 12ms query=save tab_id=t1 — Found 2"
    assert err_line == "#2 09:05:07 click ERR 3ms ref=r3 — No such ref"


def test_format_line_shows_url_when_recorded():
    h = History(now=clock(datetime(2026, 10, 27, 9, 5, 7, tzinfo=UTC)))
    h.add("click", {"ref": "r3"}, True, "Clicked r3.", 4, url="http://localhost/p")
    assert format_history(h.records()) == "#1 09:05:07 click ok 4ms @http://localhost/p ref=r3 — Clicked r3."


def test_format_truncates_long_arg_values_to_40_chars():
    h = History(now=clock(T0))
    h.add("find", {"query": "a" * 60}, True, "", 1)
    line = format_history(h.records())
    assert "query=" + "a" * 40 + "..." in line
    assert "a" * 41 not in line


# -- decorator recording ------------------------------------------------------


class FakeBrowser:
    connected = True

    def __init__(self):
        self.calls = []

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
        return 'r1 textbox "Name"\nr2 button "Save"'

    async def type(self, key, text, submit):
        self.calls.append(("type", key, text, submit))
        return f"Typed into {key}."

    async def screenshot(self, tab, full_page=False):
        return b"img"


async def _new_tab(self):
    return "t9"


async def _navigate(self, tab, url):
    return url, False


async def _url_of(self, context):
    return "http://localhost/landed" if context == "c1" else "?"


@pytest.fixture
def hist(monkeypatch, tmp_path):
    cfg = Config(
        allowlist=["localhost"],
        read_only=False,
        allow_all_sites=False,
        port=9222,
        audit_log=tmp_path / "audit.jsonl",
        auto_launch=False,
    )
    monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(server, "browser", FakeBrowser())
    h = History(now=clock(T0))
    monkeypatch.setattr(server, "HISTORY", h)
    return h


async def test_ok_call_is_recorded(hist):
    out = await server.find(query="save")
    assert 'r2 button "Save"' in out
    (rec,) = hist.records()
    assert (rec.tool, rec.ok, rec.batch) == ("find", True, False)
    assert rec.args == {"query": "save"}
    assert rec.message == out.splitlines()[1]
    assert rec.duration_ms >= 0


async def test_error_call_is_recorded_with_error_message(hist):
    with pytest.raises(ToolError, match="Give a query"):
        await server.find(query="  ")
    (rec,) = hist.records()
    assert (rec.tool, rec.ok) == ("find", False)
    assert rec.message.startswith("Give a query")


async def test_type_text_is_redacted_in_record(hist):
    await server.type(ref="r1", text="s3cret-pass", submit=False)
    (rec,) = hist.records()
    assert rec.args["text"] == {"chars": 11}
    assert "s3cret-pass" not in json.dumps(rec.args)


async def test_screenshot_message_is_image_marker(hist):
    await server.screenshot()
    (rec,) = hist.records()
    assert rec.message == "[image]"


async def test_record_screenshot_sets_last_record_path(hist):
    await server.find(query="save")
    server.record_screenshot("/tmp/shot.jpg")
    assert hist.last().screenshot == "/tmp/shot.jpg"


async def test_action_history_is_not_recorded(hist):
    await server.find(query="save")
    out = await server.action_history()
    assert "find ok" in out
    assert len(hist.records()) == 1


async def test_action_history_limit_returns_last_n(hist):
    for _ in range(4):
        await server.find(query="save")
    out = await server.action_history(limit=2)
    assert out.count("find ok") == 2
    assert out.splitlines()[0].startswith("#3 ")


async def test_action_history_clamps_limit(hist):
    await server.find(query="save")
    assert "#1 " in await server.action_history(limit=0)
    assert await server.action_history(limit=10_000) == format_history(hist.records())


async def test_browser_batch_records_steps_then_batch(hist):
    out = await server.browser_batch(
        steps=[{"tool": "find", "args": {"query": "save"}}, {"tool": "find", "args": {"query": "name"}}]
    )
    assert out.count(": OK") == 2
    recs = hist.records()
    assert [(r.tool, r.batch) for r in recs] == [("find", False), ("find", False), ("browser_batch", True)]
    assert [r.seq for r in recs] == [1, 2, 3]


async def test_browser_batch_step_type_text_redacted_in_step_record(hist):
    await server.browser_batch(steps=[{"tool": "type", "args": {"ref": "r1", "text": "hunter2"}}])
    step = hist.records()[0]
    assert step.args["text"] == {"chars": 7}


async def test_new_tab_with_url_records_the_new_tabs_url(hist, monkeypatch):
    fb = server.browser
    monkeypatch.setattr(type(fb), "new_tab", _new_tab, raising=False)
    monkeypatch.setattr(type(fb), "navigate", _navigate, raising=False)
    monkeypatch.setattr(type(fb), "url_of", _url_of, raising=False)
    await server.new_tab(url="http://localhost/landed")
    assert hist.last().url == "http://localhost/landed"


def test_message_skips_the_untrusted_fence_line():
    wrapped = server.untrusted("Tab t1\nr1 button", "http://localhost/")
    assert server._message(wrapped, None) == "Tab t1"
