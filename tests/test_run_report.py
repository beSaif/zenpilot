"""Run report: HTML and JSON written from history, devtools buffers and summary, with escaping and no external refs."""

import json
import re

from zenpilot.history import Record
from zenpilot.run_report import write_report

RUN_ID = "20261027T120000Z-ab12"
SUMMARY = {
    "run_id": RUN_ID,
    "started": "2026-10-27T12:00:00.000+00:00",
    "ended": "2026-10-27T12:00:05.000+00:00",
    "total": 2,
    "ok": 1,
    "errors": 1,
    "by_tool": {"navigate": 1, "click": 1},
}
XSS = "<script>alert(1)</script>"


def rec(seq, ok=True, args=None, url=None, screenshot=None, tool="navigate", message="done"):
    return Record(
        seq=seq,
        ts=f"2026-10-27T12:00:0{seq}.000+00:00",
        tool=tool,
        args=args if args is not None else {"url": "https://example.test/"},
        ok=ok,
        message=message,
        duration_ms=100 * seq,
        url=url,
        screenshot=screenshot,
    )


def console(level, text="msg", url=None, line=None):
    return {
        "context": "top",
        "level": level,
        "type": "log",
        "text": text,
        "url": url,
        "line": line,
        "timestamp": 1,
    }


def net(rid, status=None, error=None, duration=None, url="https://example.test/api", method="GET", size=None):
    return {
        "id": rid,
        "context": "top",
        "method": method,
        "url": url,
        "resource_type": "fetch",
        "status": status,
        "status_text": "OK" if status else None,
        "mime_type": "application/json",
        "started": 0,
        "ended": duration,
        "duration_ms": duration,
        "size": size,
        "error": error,
        "from_cache": False,
        "request_headers": [],
        "response_headers": [],
    }


def write(tmp_path, **kw):
    args = {
        "run_dir": tmp_path / "runs" / RUN_ID,
        "summary": SUMMARY,
        "records": [rec(1), rec(2, ok=False, message="boom")],
        "console": [],
        "network": [],
        "har_path": None,
        "title": "Smoke run",
        "zen_version": "1.2.3",
    }
    args.update(kw)
    return write_report(**args), args["run_dir"]


def read_json(json_path):
    return json.loads(json_path.read_text(encoding="utf-8"))


def test_writes_both_files_and_returns_paths(tmp_path):
    (html_path, json_path), run_dir = write(tmp_path)
    assert html_path == run_dir / "report.html"
    assert json_path == run_dir / "report.json"
    assert html_path.is_file()
    assert json_path.is_file()


def test_overwrites_its_own_files(tmp_path):
    write(tmp_path, title="first")
    (html_path, json_path), _ = write(tmp_path, title="second")
    assert read_json(json_path)["title"] == "second"
    assert "second" in html_path.read_text(encoding="utf-8")
    assert "first" not in html_path.read_text(encoding="utf-8")


def test_json_structure_and_actions(tmp_path):
    (_, json_path), _ = write(tmp_path)
    data = read_json(json_path)
    assert data["title"] == "Smoke run"
    assert data["zen_version"] == "1.2.3"
    assert data["summary"] == SUMMARY
    assert data["har"] is None
    assert [a["seq"] for a in data["actions"]] == [1, 2]
    assert data["actions"][1]["ok"] is False
    assert data["actions"][1]["message"] == "boom"
    assert set(data["actions"][0]) >= {"seq", "ts", "tool", "args", "ok", "message", "duration_ms", "url", "screenshot"}


def test_default_title_uses_run_id(tmp_path):
    (html_path, json_path), _ = write(tmp_path, title=None)
    assert read_json(json_path)["title"] == f"zenpilot run {RUN_ID}"
    assert f"zenpilot run {RUN_ID}" in html_path.read_text(encoding="utf-8")


def test_console_counts_and_last_20_errors(tmp_path):
    entries = [console("log", "info")] + [console("error", f"e{i}") for i in range(25)] + [console("warn", "w")]
    (_, json_path), _ = write(tmp_path, console=entries)
    data = read_json(json_path)["console"]
    assert data["counts"] == {"log": 1, "error": 25, "warn": 1}
    assert len(data["errors"]) == 20
    assert data["errors"][0]["text"] == "e5"
    assert data["errors"][-1]["text"] == "e24"


def test_network_counts_failed_and_slowest(tmp_path):
    entries = [
        net("a", status=200, duration=10),
        net("b", status=301, duration=20),
        net("c", status=404, duration=30),
        net("d", status=503, duration=40),
        net("e", error="net::ERR_FAILED", duration=50),
        net("f"),
    ]
    entries += [net(f"s{i}", status=200, duration=100 + i) for i in range(12)]
    (_, json_path), _ = write(tmp_path, network=entries)
    data = read_json(json_path)["network"]
    assert data["counts"]["2xx"] == 13
    assert data["counts"]["3xx"] == 1
    assert data["counts"]["4xx"] == 1
    assert data["counts"]["5xx"] == 1
    assert data["counts"]["failed"] == 3
    assert data["counts"]["pending"] == 1
    failed_ids = {e["id"] for e in data["failed"]}
    assert failed_ids == {"c", "d", "e"}
    slow = [e["duration_ms"] for e in data["slowest"]]
    assert len(slow) == 10
    assert slow == sorted(slow, reverse=True)
    assert slow[0] == 111


def test_html_has_sections_and_header(tmp_path):
    (html_path, _), _ = write(tmp_path, console=[console("error", "x")], network=[net("a", status=200, duration=5)])
    html = html_path.read_text(encoding="utf-8")
    assert '<meta charset="utf-8">' in html
    assert 'name="viewport"' in html
    assert RUN_ID in html
    assert "1.2.3" in html
    assert "Timeline" in html
    assert "Console" in html
    assert "Network" in html
    assert "<style>" in html
    assert "prefers-color-scheme: dark" in html
    assert ":root" in html


def test_html_timeline_row_content(tmp_path):
    (html_path, _), _ = write(tmp_path)
    html = html_path.read_text(encoding="utf-8")
    assert "12:00:01" in html
    assert "navigate" in html
    assert "boom" in html
    assert "https://example.test/" in html
    assert '<a href="https://example.test/' not in html


def test_args_truncated_to_60_chars(tmp_path):
    long = "x" * 100
    (html_path, _), _ = write(tmp_path, records=[rec(1, args={"text": long})])
    html = html_path.read_text(encoding="utf-8")
    assert "x" * 60 + "..." in html
    assert "x" * 61 not in html


def test_thumbnail_for_valid_name_inside_run_dir(tmp_path):
    (html_path, _), _ = write(tmp_path, records=[rec(3, screenshot="step-0003.jpg")])
    html = html_path.read_text(encoding="utf-8")
    assert '<a href="step-0003.jpg"><img src="step-0003.jpg" loading="lazy" width="160"></a>' in html


def test_thumbnail_from_absolute_path_inside_run_dir(tmp_path):
    run_dir = tmp_path / "runs" / RUN_ID
    shot = str(run_dir / "step-0004.jpg")
    (html_path, _), _ = write(tmp_path, records=[rec(4, screenshot=shot)])
    html = html_path.read_text(encoding="utf-8")
    assert 'src="step-0004.jpg"' in html
    assert str(tmp_path) not in html


def test_no_thumbnail_for_invalid_or_outside_names(tmp_path):
    outside = tmp_path / "elsewhere" / "step-0005.jpg"
    records = [
        rec(1, screenshot="shot.png"),
        rec(2, screenshot="step-1.jpg"),
        rec(3, screenshot="../step-0006.jpg"),
        rec(4, screenshot=str(outside)),
        rec(5, screenshot=None),
    ]
    (html_path, _), _ = write(tmp_path, records=records)
    html = html_path.read_text(encoding="utf-8")
    assert "<img" not in html
    assert str(tmp_path) not in html


def test_escapes_untrusted_strings(tmp_path):
    records = [rec(1, args={"q": XSS}, url=XSS, message=XSS, tool="navigate")]
    entries = [console("error", XSS, url=XSS, line=3)]
    network = [net("a", status=500, duration=5, url=f"https://x.test/{XSS}")]
    (html_path, json_path), _ = write(tmp_path, records=records, console=entries, network=network, title=XSS)
    html = html_path.read_text(encoding="utf-8")
    assert "<script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.count("&lt;script&gt;") >= 5
    assert read_json(json_path)["title"] == XSS


def test_empty_inputs_render_placeholders(tmp_path):
    (html_path, json_path), _ = write(tmp_path, records=[], console=[], network=[], summary={**SUMMARY, "total": 0})
    html = html_path.read_text(encoding="utf-8")
    assert "No actions recorded." in html
    assert "No console messages." in html
    assert "No failed requests." in html
    assert "No network requests." in html
    data = read_json(json_path)
    assert data["actions"] == []
    assert data["console"]["errors"] == []
    assert data["network"]["slowest"] == []


def test_har_link_present_when_given(tmp_path):
    (html_path, json_path), run_dir = write(tmp_path, har_path=run_dir_har(tmp_path))
    html = html_path.read_text(encoding="utf-8")
    assert '<a href="run.har">run.har</a>' in html
    assert read_json(json_path)["har"] == "run.har"


def test_har_link_absent_when_none(tmp_path):
    (html_path, _), _ = write(tmp_path, har_path=None)
    assert 'href="run.har"' not in html_path.read_text(encoding="utf-8")


def run_dir_har(tmp_path):
    return tmp_path / "runs" / RUN_ID / "run.har"


def test_no_external_resource_refs(tmp_path):
    (html_path, _), _ = write(
        tmp_path,
        records=[rec(1, url="https://example.test/page")],
        network=[net("a", status=200, duration=5)],
        har_path=run_dir_har(tmp_path),
    )
    html = html_path.read_text(encoding="utf-8")
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "<script" not in html
    assert "<link" not in html
    assert not re.search(r"@import|url\(\s*['\"]?https?:", html)


def test_thumbnail_regex_contract(tmp_path):
    (html_path, _), _ = write(tmp_path, records=[rec(1, screenshot="step-0001.jpg")])
    srcs = re.findall(r'<img src="([^"]+)"', html_path.read_text(encoding="utf-8"))
    assert srcs == ["step-0001.jpg"]
    assert all(re.fullmatch(r"step-\d{4}\.jpg", s) for s in srcs)
