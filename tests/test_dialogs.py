"""The Windows dialog plumbing, with PowerShell faked so no window opens."""

import json
import os
import subprocess
import sys

import pytest

from zenpilot import dialogs


@pytest.fixture
def powershell(monkeypatch):
    """Fake subprocess.run; set .stdout to what the dialog prints. Records each spec it was given."""
    calls = []

    class Fake:
        stdout = ""
        returncode = 0

    def run(cmd, **kwargs):
        calls.append(json.loads(kwargs["env"]["ZENPILOT_DIALOG"]))
        assert cmd[0] == "powershell.exe" and "-EncodedCommand" in cmd
        return subprocess.CompletedProcess(cmd, Fake.returncode, Fake.stdout, "")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dialogs.subprocess, "run", run)
    Fake.calls = calls
    return Fake


def test_ask_maps_the_index_back_to_the_button(powershell):
    powershell.stdout = "1"
    assert dialogs.ask("Allow “github.com”?", ["Don't Allow", "Allow This Session"], "Don't Allow") == (
        "Allow This Session"
    )
    spec = powershell.calls[0]
    assert spec["kind"] == "ask" and spec["default"] == 0 and spec["timeout"] == dialogs.TIMEOUT
    assert spec["message"] == "Allow “github.com”?"  # passed as data, not spliced into the script


def test_choose_maps_the_index_back_to_the_item(powershell):
    powershell.stdout = "2\r\n"
    assert dialogs.choose("Which profile?", ["a", "b", "c"]) == "c"
    assert powershell.calls[0]["kind"] == "choose"


@pytest.mark.parametrize("stdout, returncode", [("", 0), ("7", 0), ("-1", 0), ("junk", 0), ("0", 1)])
def test_no_answer_or_garbage_is_none(powershell, stdout, returncode):
    powershell.stdout, powershell.returncode = stdout, returncode
    assert dialogs.ask("?", ["No", "Yes"], "No") is None
    assert dialogs.choose("?", ["a", "b"]) is None


def test_powershell_missing_is_none(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def run(*args, **kwargs):
        raise FileNotFoundError("powershell.exe")

    monkeypatch.setattr(dialogs.subprocess, "run", run)
    assert dialogs.ask("?", ["No", "Yes"], "No") is None


@pytest.mark.skipif(sys.platform != "win32", reason="needs PowerShell")
def test_winforms_script_parses():
    """Parse (not run) the dialog script, so a syntax error fails here instead of silently denying."""
    check = (
        "$e = $null; $parser = [System.Management.Automation.Language.Parser];"
        " [void]$parser::ParseInput($env:SCRIPT, [ref]$null, [ref]$e); $e | ForEach-Object { $_.Message }"
    )
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True,
        text=True,
        env={**os.environ, "SCRIPT": dialogs._WINFORMS},
        timeout=60,
    )
    assert r.returncode == 0 and r.stdout.strip() == "", r.stdout + r.stderr
