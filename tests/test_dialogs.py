"""The Linux dialog backends (zenity and kdialog), with the commands faked."""

import subprocess

import pytest

from zenpilot import dialogs


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(dialogs, "MAC", False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    seen = {}

    def fake(tool, code=0, out=""):
        monkeypatch.setattr(dialogs.shutil, "which", lambda name: f"/bin/{name}" if name == tool else None)

        def run(cmd, timeout=None):
            seen["cmd"] = cmd
            return None if code is None else subprocess.CompletedProcess(cmd, code, out, "")

        monkeypatch.setattr(dialogs, "_run", run)
        return seen

    return fake


BUTTONS = ["Don't Allow", "Allow for 8 Hours", "Always Allow"]


@pytest.mark.parametrize(
    ("code", "out", "answer"),
    [(0, "", "Don't Allow"), (1, "Always Allow\n", "Always Allow"), (1, "", "Don't Allow"), (None, "", None)],
)
def test_zenity_enter_and_escape_pick_the_default(linux, code, out, answer):
    seen = linux("zenity", code, out)
    assert dialogs.ask("Allow?", BUTTONS, "Don't Allow") == answer
    assert "--ok-label=Don't Allow" in seen["cmd"] and "--extra-button=Allow for 8 Hours" in seen["cmd"]
    assert not any(a.startswith("--timeout") for a in seen["cmd"])


@pytest.mark.parametrize(("code", "out", "answer"), [(0, "Always Allow\n", "Always Allow"), (1, "", "Don't Allow")])
def test_kdialog_lists_the_default_first(linux, code, out, answer):
    seen = linux("kdialog", code, out)
    assert dialogs.ask("Allow?", BUTTONS, "Don't Allow") == answer
    assert seen["cmd"][-9:-6] == ["Don't Allow", "Don't Allow", "on"]


def test_no_tool_or_screen_means_nobody_answered(linux, monkeypatch):
    linux("none")
    assert dialogs.ask("Allow?", BUTTONS, "Don't Allow") is None
    assert dialogs.choose("Which?", ["a", "b"]) is None
    linux("zenity")
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert dialogs.ask("Allow?", BUTTONS, "Don't Allow") is None


def test_a_timed_out_dialog_is_none(linux):
    linux("kdialog", None)
    assert dialogs.ask("Allow?", BUTTONS, "Don't Allow") is None


def test_choose(linux):
    seen = linux("zenity", 0, "b\n")
    assert dialogs.choose("Which?", ["a", "b"]) == "b"
    assert seen["cmd"][-4:] == ["TRUE", "a", "FALSE", "b"]
    linux("zenity", 1, "")
    assert dialogs.choose("Which?", ["a", "b"]) is None
