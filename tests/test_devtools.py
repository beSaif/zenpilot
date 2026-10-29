"""Console and network buffers: allowlist scoping, bounds, filters and formatting."""

from zenpilot.devtools import Devtools, format_console, format_requests, redact_headers

OK = "https://ok.com/"
MAIL = "https://mail.example.com/"


def allowed(url):
    return url.startswith("https://ok.com")


def devtools(**kwargs):
    dt = Devtools(allowed, **kwargs)
    dt.set_url("c1", OK)
    dt.set_url("c2", MAIL)
    return dt


def log(context, text, level="info"):
    return {"type": "console", "level": level, "text": text, "timestamp": 1000, "source": {"context": context}}


def before(context, rid, url, navigation=None, redirect=0, headers=None):
    return {
        "context": context,
        "navigation": navigation,
        "redirectCount": redirect,
        "timestamp": 1000,
        "request": {"request": rid, "url": url, "method": "GET", "headers": headers or [], "destination": "script"},
    }


def completed(context, rid, status=200, ts=1300):
    return {
        "context": context,
        "timestamp": ts,
        "request": {"request": rid},
        "response": {"status": status, "statusText": "OK", "mimeType": "text/html", "headers": [], "bytesReceived": 42},
    }


def test_console_buffer_keeps_the_newest_messages():
    dt = devtools(max_console=2)
    for text in ("a", "b", "c"):
        dt.on_log(log("c1", text))
    assert [e["text"] for e in dt.console()] == ["b", "c"]


def test_console_filters_by_level_and_context():
    dt = devtools()
    dt.on_log(log("c1", "a", "info"))
    dt.on_log(log("c1", "b", "error"))
    assert [e["text"] for e in dt.console("c1", "error")] == ["b"]


def test_console_from_a_blocked_tab_is_never_stored():
    dt = devtools()
    dt.on_log(log("c2", "secret"))
    assert dt.console() == []


def test_network_from_a_blocked_tab_is_never_stored():
    dt = devtools()
    dt.on_before_request(before("c2", "r1", MAIL + "inbox"))
    dt.on_response_started(completed("c2", "r1"))
    dt.on_response_completed(completed("c2", "r1"))
    dt.on_fetch_error({"context": "c2", "request": {"request": "r2"}, "errorText": "x", "timestamp": 1})
    assert dt.requests() == []
    assert dt.request("r1") is None


def test_unknown_tab_is_not_recorded():
    dt = Devtools(allowed)
    dt.on_log(log("c9", "early"))
    dt.on_before_request(before("c9", "r1", OK))
    assert dt.console() == []
    assert dt.requests() == []


def test_navigating_to_a_blocked_site_drops_the_tabs_earlier_entries():
    dt = devtools()
    dt.on_log(log("c1", "from ok"))
    dt.on_before_request(before("c1", "r1", OK + "page"))
    dt.set_url("c1", MAIL)
    assert dt.console() == []
    assert dt.requests() == []
    dt.on_log(log("c1", "from mail"))
    dt.on_before_request(before("c1", "r2", MAIL + "inbox"))
    assert dt.console() == []
    assert dt.requests() == []


def test_navigating_back_to_an_allowed_site_records_again():
    dt = devtools()
    dt.set_url("c1", MAIL)
    dt.set_url("c1", OK)
    dt.on_log(log("c1", "back"))
    assert [e["text"] for e in dt.console()] == ["back"]


def test_redirect_into_a_blocked_site_stops_recording_that_tab():
    dt = devtools()
    dt.on_before_request(before("c1", "r1", OK + "login", navigation="n1"))
    dt.on_before_request(before("c1", "r1", MAIL + "idp", navigation="n1", redirect=1))
    dt.on_log(log("c1", "after redirect"))
    assert dt.console() == []
    assert dt.requests() == []


def test_other_tabs_keep_recording_when_one_is_blocked():
    dt = devtools()
    dt.on_log(log("c1", "ok tab"))
    dt.on_log(log("c2", "mail tab"))
    assert [e["text"] for e in dt.console()] == ["ok tab"]


def test_requests_keep_their_timing_and_status():
    dt = devtools()
    dt.on_before_request(before("c1", "r1", OK + "api"))
    dt.on_response_completed(completed("c1", "r1", status=201, ts=1300))
    entry = dt.request("r1")
    assert (entry["status"], entry["size"], entry["duration_ms"]) == (201, 42, 300)


def test_redirect_hop_keeps_its_own_entry():
    dt = devtools()
    dt.on_before_request(before("c1", "r1", OK + "a", navigation="n1"))
    dt.on_before_request(before("c1", "r1", OK + "b", navigation="n1", redirect=1))
    assert dt.request("r1")["url"] == OK + "b"
    assert dt.request("r1.0")["url"] == OK + "a"


def test_request_buffer_drops_the_oldest_entries():
    dt = devtools(max_network=2)
    for rid in ("r1", "r2", "r3"):
        dt.on_before_request(before("c1", rid, OK + rid))
    assert [e["id"] for e in dt.requests()] == ["r2", "r3"]


def test_request_filters():
    dt = devtools()
    dt.on_before_request(before("c1", "r1", OK + "api/users"))
    dt.on_before_request(before("c1", "r2", OK + "logo.png"))
    dt.on_response_completed(completed("c1", "r1", status=404))
    assert [e["id"] for e in dt.requests(url_contains="api")] == ["r1"]
    assert [e["id"] for e in dt.requests(status="4xx")] == ["r1"]
    assert [e["id"] for e in dt.requests(method="post")] == []


def test_clear_only_the_given_tab():
    dt = devtools()
    dt.on_log(log("c1", "a"))
    dt.on_log(log("c1", "b"))
    dt.clear_console("c1")
    assert dt.console() == []
    dt.on_before_request(before("c1", "r1", OK))
    dt.clear_network("c1")
    assert dt.requests() == []


def test_sensitive_headers_are_redacted():
    headers = [
        {"name": "Authorization", "value": {"type": "string", "value": "Bearer x"}},
        {"name": "X-Api-Key", "value": {"type": "string", "value": "k"}},
        {"name": "Accept", "value": {"type": "string", "value": "text/html"}},
    ]
    assert redact_headers(headers) == [
        {"name": "Authorization", "value": "[redacted]"},
        {"name": "X-Api-Key", "value": "[redacted]"},
        {"name": "Accept", "value": "text/html"},
    ]


def test_formatting():
    dt = devtools()
    dt.on_log(log("c1", "boom", "error"))
    dt.on_before_request(before("c1", "r1", OK + "api"))
    dt.on_response_completed(completed("c1", "r1", status=500))
    assert format_console(dt.console()) == "[error] boom"
    assert format_requests(dt.requests()) == f"r1  GET  500  script  300 ms  42 B  {OK}api"
    assert format_console([]) == "No console messages."
    assert format_requests([]) == "No requests recorded."
