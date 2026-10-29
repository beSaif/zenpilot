"""Response data collectors: one per allowlisted tab, removed when the tab leaves the allowlist or is destroyed."""

import asyncio

import pytest

from zenpilot.browser import Browser
from zenpilot.safety import Config

OK = "https://ok.com/"
MAIL = "https://mail.example.com/"


class FakeBiDi:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method, params=None, timeout=30):
        self.sent.append((method, params or {}))
        if method == "network.addDataCollector":
            return {"collector": f"d{sum(m == method for m, _ in self.sent)}"}
        return {}


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def browser(tmp_path):
    b = Browser()
    b._bidi = FakeBiDi()
    b.allowlist = Config(["ok.com"], False, False, 9222, tmp_path / "audit.jsonl", auto_launch=False)
    return b


async def test_a_non_allowlisted_tab_never_gets_a_collector(browser):
    browser.devtools.set_url("c2", MAIL)
    await settle()
    assert browser._bidi.sent == []


async def test_an_allowlisted_tab_gets_a_collector_for_itself(browser):
    browser.devtools.set_url("c1", OK)
    await settle()
    assert browser._bidi.sent == [
        (
            "network.addDataCollector",
            {"dataTypes": ["response"], "maxEncodedDataSize": 2_000_000, "contexts": ["c1"]},
        )
    ]
    assert browser._collectors == {"c1": "d1"}


async def test_leaving_the_allowlist_removes_the_collector(browser):
    browser.devtools.set_url("c1", OK)
    await settle()
    browser.devtools.set_url("c1", MAIL)
    await settle()
    assert browser._bidi.sent[-1] == ("network.removeDataCollector", {"collector": "d1"})
    assert browser._collectors == {}


async def test_destroying_a_tab_removes_its_collector_and_url(browser):
    browser.devtools.set_url("c1", OK)
    await settle()
    browser._on_destroyed({"context": "c1"})
    await settle()
    assert browser._bidi.sent[-1] == ("network.removeDataCollector", {"collector": "d1"})
    assert not browser.devtools.recording("c1")
    assert browser._collectors == {}
