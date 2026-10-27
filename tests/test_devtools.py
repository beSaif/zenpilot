"""Console/network buffers, filters, formatting and HAR export."""

import pytest

from zenpilot.devtools import Devtools, format_console, format_requests, har, redact_headers


def log(text, level="info", context="c1", url=None, line=None):
    params = {"type": "console", "level": level, "text": text, "timestamp": 1000, "source": {"context": context}}
    if url:
        params["stackTrace"] = {"callFrames": [{"url": url, "lineNumber": line, "columnNumber": 0}]}
    return params


def before(rid, url, context="c1", method="GET", redirect=0, dest="script", ts=1000, headers=None):
    return {
        "context": context,
        "navigation": None,
        "redirectCount": redirect,
        "timestamp": ts,
        "request": {
            "request": rid,
            "url": url,
            "method": method,
            "headers": headers or [],
            "destination": dest,
            "timings": {},
        },
    }


def resp(rid, status, event_ts=None, context="c1", headers=None, size=None):
    params = {
        "context": context,
        "timestamp": event_ts or 1100,
        "request": {"request": rid},
        "response": {
            "url": "https://x/",
            "status": status,
            "statusText": "OK" if status == 200 else "X",
            "fromCache": False,
            "mimeType": "text/html",
            "headers": headers or [],
        },
    }
    if size is not None:
        params["response"]["bytesReceived"] = size
    return params


def completed(rid, status, ts=1300, context="c1", size=512):
    p = resp(rid, status, event_ts=ts, context=context, size=size)
    p["response"]["content"] = {"size": 999}
    return p


def hdr(name, value):
    return {"name": name, "value": {"type": "string", "value": value}}


def test_console_buffer_filters_and_cap():
    dt = Devtools(max_console=2)
    dt.on_log(log("a", "info", "c1"))
    dt.on_log(log("b", "error", "c2"))
    dt.on_log(log("c", "warn", "c1"))
    assert [e["text"] for e in dt.console()] == ["b", "c"]
    assert [e["text"] for e in dt.console(level="error")] == ["b"]
    assert [e["text"] for e in dt.console(context="c1")] == ["c"]


def test_console_entry_fields_from_first_frame():
    dt = Devtools()
    dt.on_log(log("boom", "error", url="https://x/app.js", line=12))
    entry = dt.console()[0]
    assert entry == {
        "context": "c1",
        "level": "error",
        "type": "console",
        "text": "boom",
        "url": "https://x/app.js",
        "line": 12,
        "timestamp": 1000,
    }


def test_request_lifecycle_before_started_completed():
    dt = Devtools()
    dt.on_before_request(before("r1", "https://x/a?q=1", ts=1000, headers=[hdr("Accept", "*/*")]))
    pending = dt.request("r1")
    assert pending["status"] is None and pending["ended"] is None
    assert pending["request_headers"] == [{"name": "Accept", "value": "*/*"}]
    assert pending["resource_type"] == "script"

    dt.on_response_started(resp("r1", 200, event_ts=1100, headers=[hdr("Content-Type", "text/html")]))
    assert dt.request("r1")["status"] == 200
    assert dt.request("r1")["mime_type"] == "text/html"

    dt.on_response_completed(completed("r1", 200, ts=1300, size=512))
    done = dt.request("r1")
    assert done["ended"] == 1300
    assert done["duration_ms"] == 300
    assert done["size"] == 512
    assert done["status_text"] == "OK"
    assert done["from_cache"] is False


def test_resource_type_falls_back_to_initiator_type():
    dt = Devtools()
    p = before("r1", "https://x/")
    p["request"].pop("destination")
    p["request"]["initiatorType"] = "fetch"
    dt.on_before_request(p)
    assert dt.request("r1")["resource_type"] == "fetch"


def test_fetch_error_closes_entry():
    dt = Devtools()
    dt.on_before_request(before("r9", "https://x/down"))
    dt.on_fetch_error(
        {"context": "c1", "timestamp": 1500, "request": {"request": "r9"}, "errorText": "net::ERR_FAILED"}
    )
    entry = dt.request("r9")
    assert entry["error"] == "net::ERR_FAILED"
    assert entry["ended"] == 1500
    assert entry["status"] is None


def test_redirect_chain_keeps_previous_hop():
    dt = Devtools()
    dt.on_before_request(before("r1", "https://x/old", redirect=0))
    dt.on_before_request(before("r1", "https://x/new", redirect=1))
    assert dt.request("r1.0")["url"] == "https://x/old"
    assert dt.request("r1")["url"] == "https://x/new"
    assert dt.request("r1")["status"] is None


def test_request_cap_drops_oldest():
    dt = Devtools(max_network=2)
    for rid in ("a", "b", "c"):
        dt.on_before_request(before(rid, f"https://x/{rid}"))
    assert dt.request("a") is None
    assert [e["id"] for e in dt.requests()] == ["b", "c"]


def test_status_filters():
    dt = Devtools()
    for rid, status in (("a", 200), ("b", 404), ("c", 500), ("d", 499)):
        dt.on_before_request(before(rid, f"https://x/{rid}"))
        dt.on_response_completed(completed(rid, status))
    assert [e["id"] for e in dt.requests(status=200)] == ["a"]
    assert [e["id"] for e in dt.requests(status="2xx")] == ["a"]
    assert [e["id"] for e in dt.requests(status="4xx")] == ["b", "d"]
    assert [e["id"] for e in dt.requests(status="400-499")] == ["b", "d"]
    with pytest.raises(ValueError):
        dt.requests(status="bogus")


def test_url_method_type_and_context_filters():
    dt = Devtools()
    dt.on_before_request(before("a", "https://x/api/users", method="GET", dest="fetch"))
    dt.on_before_request(before("b", "https://x/api/users", method="POST", dest="fetch", context="c2"))
    dt.on_before_request(before("c", "https://x/logo.png", method="GET", dest="image"))
    assert [e["id"] for e in dt.requests(url_contains="/api/")] == ["a", "b"]
    assert [e["id"] for e in dt.requests(method="post")] == ["b"]
    assert [e["id"] for e in dt.requests(resource_type="image")] == ["c"]
    assert [e["id"] for e in dt.requests(context="c2")] == ["b"]


def test_resource_type_stylesheet_matches_firefox_style_destination():
    # Firefox reports CSS as destination "style"; users ask for "stylesheet" (what the spec calls it).
    dt = Devtools()
    dt.on_before_request(before("a", "https://x/site.css", method="GET", dest="style"))
    dt.on_before_request(before("b", "https://x/app.js", method="GET", dest="script"))
    assert [e["id"] for e in dt.requests(resource_type="stylesheet")] == ["a"]
    assert [e["id"] for e in dt.requests(resource_type="style")] == ["a"]


def test_redact_headers_case_insensitive_and_flattened():
    out = redact_headers(
        [
            hdr("Authorization", "Bearer t"),
            hdr("COOKIE", "sid=1"),
            hdr("Set-Cookie", "sid=2"),
            hdr("Proxy-Authorization", "p"),
            hdr("X-Ok", "fine"),
        ]
    )
    assert out == [
        {"name": "Authorization", "value": "[redacted]"},
        {"name": "COOKIE", "value": "[redacted]"},
        {"name": "Set-Cookie", "value": "[redacted]"},
        {"name": "Proxy-Authorization", "value": "[redacted]"},
        {"name": "X-Ok", "value": "fine"},
    ]


def test_redaction_applies_to_request_and_response_stores():
    dt = Devtools()
    dt.on_before_request(before("a", "https://x/", headers=[hdr("cookie", "sid=1")]))
    dt.on_response_completed(
        completed("a", 200) | {"response": {**completed("a", 200)["response"], "headers": [hdr("Set-Cookie", "s=1")]}}
    )
    entry = dt.request("a")
    assert entry["request_headers"][0]["value"] == "[redacted]"
    assert entry["response_headers"][0] == {"name": "Set-Cookie", "value": "[redacted]"}


def test_format_console():
    dt = Devtools()
    dt.on_log(log("hi", "info", url="https://x/a.js", line=3))
    dt.on_log(log("plain", "warn"))
    assert format_console(dt.console()) == "[info] hi (https://x/a.js:3)\n[warn] plain"
    assert format_console([]) == "No console messages."


def test_format_requests_pending_error_and_truncation():
    dt = Devtools()
    dt.on_before_request(before("a", "https://x/" + "p" * 200, method="GET", dest="fetch"))
    dt.on_before_request(before("b", "https://x/bad", method="POST", dest="fetch"))
    dt.on_fetch_error({"context": "c1", "timestamp": 1200, "request": {"request": "b"}, "errorText": "boom"})
    dt.on_before_request(before("c", "https://x/ok", method="GET", dest="script"))
    dt.on_response_completed(completed("c", 200, ts=1400, size=2048))
    lines = format_requests(dt.requests()).splitlines()
    assert lines[0].startswith("a  GET  …  fetch")
    assert len(lines[0].split("  ")[-1]) <= 120
    assert "ERR boom" in lines[1]
    assert lines[2] == "c  GET  200  script  400 ms  2048 B  https://x/ok"
    assert format_requests([]) == "No requests recorded."


def test_har_structure_bodies_and_cookies_empty():
    dt = Devtools()
    dt.on_before_request(
        before(
            "a",
            "https://x/p?x=1&y=2",
            method="POST",
            headers=[hdr("Cookie", "sid=1"), hdr("Accept", "*/*")],
            ts=1_700_000_000_000,
        )
    )
    dt.on_response_completed(completed("a", 200, ts=1_700_000_000_250, size=40) | {})
    out = har(dt.requests(), bodies={"a": "<html/>"}, creator_version="9.9")
    log_ = out["log"]
    assert log_["version"] == "1.2"
    assert log_["creator"] == {"name": "zenpilot", "version": "9.9"}
    entry = log_["entries"][0]
    assert entry["startedDateTime"] == "2023-11-14T22:13:20.000Z"
    assert entry["time"] == 250
    assert entry["timings"] == {"send": 0, "wait": 250, "receive": 0}
    assert entry["request"]["cookies"] == []
    assert entry["request"]["queryString"] == [{"name": "x", "value": "1"}, {"name": "y", "value": "2"}]
    assert entry["request"]["headers"][0] == {"name": "Cookie", "value": "[redacted]"}
    assert entry["response"]["cookies"] == []
    assert entry["response"]["status"] == 200
    assert entry["response"]["content"]["text"] == "<html/>"
    assert entry["cache"] == {}


def test_har_without_body_omits_text():
    dt = Devtools()
    dt.on_before_request(before("a", "https://x/"))
    dt.on_response_completed(completed("a", 204))
    content = har(dt.requests())["log"]["entries"][0]["response"]["content"]
    assert "text" not in content


def test_clear_console_and_network_by_context():
    dt = Devtools()
    dt.on_log(log("a", context="c1"))
    dt.on_log(log("b", context="c2"))
    dt.on_before_request(before("a", "https://x/", context="c1"))
    dt.on_before_request(before("b", "https://x/", context="c2"))
    dt.clear_console(context="c1")
    dt.clear_network(context="c1")
    assert [e["text"] for e in dt.console()] == ["b"]
    assert [e["id"] for e in dt.requests()] == ["b"]
    dt.clear_console()
    dt.clear_network()
    assert dt.console() == [] and dt.requests() == []


def test_redact_headers_masks_credential_looking_names():
    out = redact_headers(
        [
            hdr("X-API-Key", "k"),
            hdr("X-Auth-Token", "t"),
            hdr("X-CSRF-Token", "c"),
            hdr("Client-Secret", "s"),
            hdr("X-Password", "p"),
            hdr("Content-Type", "text/html"),
        ]
    )
    assert [h["value"] for h in out] == ["[redacted]"] * 5 + ["text/html"]
