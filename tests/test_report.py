"""Session reports: what gets recorded, what the file says and the GitHub issue link."""

from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

from zenpilot import report, server
from zenpilot.browser import BrowserError
from zenpilot.report import SessionReport, issue_url, scrub
from zenpilot.safety import load_config


def test_only_failed_or_slow_calls_are_findings(tmp_path):
    r = SessionReport()
    assert r.record_call("read_page", "example.com", 300, None) is False
    assert r.save(tmp_path) is None  # nothing to report, no file
    assert r.record_call("click", "example.com", 200, "Ref r3 is stale") is True
    assert r.record_call("navigate", "example.com", 12_000, None) is True
    assert r.problems == 2
    path = r.save(tmp_path)
    assert path.parent == tmp_path and path.suffix == ".md"
    text = path.read_text()
    assert "| click | example.com | 1 | Ref r3 is stale |" in text
    assert "| navigate | example.com | 12.0s |" in text
    assert "| read_page | 1 | 0 | 0.3s | 0.3s |" in text
    assert r.save(tmp_path) == path  # one file per session, rewritten


def test_limitations_name_the_last_site(tmp_path):
    r = SessionReport()
    r.record_call("navigate", "jobs.example.com", 900, None)
    r.record_call("screenshot", None, 100, None)
    r.add_limitation("upload a CV", "type on the file input did nothing", "asked the user to attach it")
    text = r.save(tmp_path).read_text()
    assert "### 1. upload a CV (jobs.example.com)" in text
    assert "type on the file input did nothing\n\nWorkaround: asked the user to attach it" in text


def test_scrub_drops_queries_and_fragments():
    assert (
        scrub("Now at https://a.com/p?token=x#y, then http://b.com/q")
        == "Now at https://a.com/p?…, then http://b.com/q"
    )


def test_issue_url():
    r = SessionReport()
    r.add_limitation("upload a CV", "nothing happened")
    r.add_limitation("drag a slider", "no drag tool")
    url, fits = issue_url(r.render())
    query = parse_qs(urlsplit(url).query)
    assert fits and url.startswith(report.ISSUES_URL)
    assert query["title"] == ["Couldn't: upload a CV (+1 more)"]
    assert "## What Claude couldn't do" in query["body"][0]

    r.add_limitation("x", "y" * 10_000)
    url, fits = issue_url(r.render())
    assert not fits and len(url) < report.MAX_URL and "clipboard" in url


class FakeBrowser:
    connected = True

    def __init__(self, tab=None, error=None):
        self._tab, self._error = tab, error

    @asynccontextmanager
    async def session(self, port):
        yield

    async def tab(self, tab_id):
        if self._error:
            raise self._error
        return self._tab


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(f'prompts = false\naudit_log = "{tmp_path / "audit.jsonl"}"\nreport_dir = "{tmp_path / "r"}"\n')
    monkeypatch.setattr(server, "load_config", lambda: load_config(path))
    monkeypatch.setattr(server, "session_report", SessionReport())
    return tmp_path / "r"


async def test_server_records_browser_failures_but_not_the_allowlist(env, monkeypatch):
    monkeypatch.setattr(server, "browser", FakeBrowser(tab={"id": "t1", "url": "https://evil.com/", "context": "c"}))
    with pytest.raises(Exception, match="not on the zenpilot allowlist"):
        await server.read_page()
    assert not server.session_report.calls and not env.exists()

    monkeypatch.setattr(server, "browser", FakeBrowser(error=BrowserError("Zen did not answer in 30s.")))
    with pytest.raises(Exception, match="did not answer"):
        await server.read_page()
    assert "| read_page |  | 1 | Zen did not answer in 30s. |" in report.reports(env)[0].read_text()


async def test_report_limitation_tool(env):
    out = await server.report_limitation(goal="upload a file", problem="no tool for <input type=file>")
    assert "Noted in the session report" in out
    assert "### 1. upload a file" in report.reports(env)[0].read_text()
