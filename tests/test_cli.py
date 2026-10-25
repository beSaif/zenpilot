"""Profile handling, config editing and client registration, without touching the real Zen or Claude."""

import json
import sys

import pytest

from zenpilot import cli, zen
from zenpilot.safety import load_config, set_config_value


@pytest.fixture
def zen_dir(tmp_path, monkeypatch):
    (tmp_path / "Profiles/abc.zenpilot").mkdir(parents=True)
    (tmp_path / "profiles.ini").write_text(
        "[General]\nVersion=2\n\n"
        "[Profile0]\nName=Default (release)\nIsRelative=1\nPath=Profiles/x.Default (release)\n\n"
        "[Profile1]\nName=zenpilot\nIsRelative=1\nPath=Profiles/abc.zenpilot\n\n"
        "[Install123]\nDefault=Profiles/abc.zenpilot\n"
    )
    monkeypatch.setattr(zen, "ZEN_DIR", tmp_path)
    return tmp_path


def test_profile_path(zen_dir):
    assert zen.profile_path("zenpilot") == zen_dir / "Profiles/abc.zenpilot"
    assert zen.profile_path("Default (release)") == zen_dir / "Profiles/x.Default (release)"
    assert zen.profile_path("nope") is None


def test_describe(zen_dir, monkeypatch):
    monkeypatch.setattr(zen, "holder", lambda path: None)
    assert zen.describe("Default (release)") == "never used"
    profile = zen_dir / "Profiles/abc.zenpilot"
    (profile / "prefs.js").write_text('user_pref("services.sync.username", "me@example.com");\n')
    assert zen.describe("zenpilot").startswith("last used ")
    monkeypatch.setattr(zen, "holder", lambda path: 123)
    assert zen.describe("zenpilot") == "open now, signed in as me@example.com"


def test_ensure_prefs_is_idempotent(zen_dir):
    profile = zen_dir / "Profiles/abc.zenpilot"
    (profile / "user.js").write_text('user_pref("zen.welcome-screen.seen", true);\n')
    assert not zen.is_safe(profile)
    assert zen.ensure_prefs(profile, [zen.SAFE_PREF, zen.WELCOME_PREF]) == ["remote.prefs.recommended"]
    assert zen.ensure_prefs(profile, [zen.SAFE_PREF, zen.WELCOME_PREF]) == []
    assert zen.is_safe(profile)
    assert zen.prefs(profile) == {"zen.welcome-screen.seen": "true", "remote.prefs.recommended": "false"}


def test_resolve_never_creates_a_mistyped_profile(zen_dir, monkeypatch):
    monkeypatch.setattr(zen, "create_profile", lambda name: pytest.fail("created a profile"))
    with pytest.raises(zen.ZenError) as e:
        zen.resolve("Default")
    assert "no Zen profile named 'Default' (profiles: 'Default (release)', 'zenpilot')" in str(e.value)


def test_resolve_creates_the_dedicated_profile(zen_dir, monkeypatch):
    (zen_dir / "profiles.ini").write_text("[General]\nVersion=2\n")
    created = zen_dir / "Profiles/new.zenpilot"
    monkeypatch.setattr(zen, "create_profile", lambda name: created.mkdir() or created)
    assert zen.resolve("zenpilot") == created
    assert zen.is_safe(created) and zen.prefs(created)["zen.welcome-screen.seen"] == "true"


@pytest.fixture
def fake_launch(zen_dir, monkeypatch):
    """launch() with the port, the profile lock, quitting and starting Zen faked."""
    calls = []
    (zen_dir / "zen").touch()
    monkeypatch.setattr(zen, "ZEN_BIN", zen_dir / "zen")  # exists, like an installed Zen
    monkeypatch.setattr(zen, "port_open", lambda port: bool(calls and calls[-1][0] == "open"))
    monkeypatch.setattr(zen, "holder", lambda path: 123)
    monkeypatch.setattr(zen, "quit_zen", lambda pid, path: calls.append(("quit", pid)))
    monkeypatch.setattr(zen, "_spawn", lambda name, port: calls.append(("open", name)))
    return calls


def test_launch_reopens_an_open_profile_only_when_confirmed(zen_dir, fake_launch):
    profile = zen_dir / "Profiles/abc.zenpilot"
    with pytest.raises(zen.ZenError, match="open without remote debugging"):
        zen.launch("zenpilot", 9222, lambda question: False)
    assert fake_launch == [] and zen.is_safe(profile)  # the safety pref is written before anything else
    questions = []
    assert zen.launch("zenpilot", 9222, lambda q: questions.append(q) or True) is True
    assert fake_launch == [("quit", 123), ("open", "zenpilot")]
    assert "Your tabs will be restored" in questions[0]


def test_launch_skips_when_port_is_taken(monkeypatch):
    monkeypatch.setattr(zen, "port_open", lambda port: True)
    assert zen.launch("anything", 9222) is False


def test_set_config_value_keeps_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# mine\nallowlist = [\n  "localhost",  # dev\n]\nread_only = false\n')
    set_config_value("allowlist", ["localhost", "github.com"], path)
    set_config_value("profile", "Default (release)", path)
    text = path.read_text()
    assert text.startswith("# mine\n") and "read_only = false" in text
    cfg = load_config(path)
    assert cfg.allowlist == ["localhost", "github.com"] and cfg.profile == "Default (release)"


def test_config_is_utf8_with_or_without_a_bom(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes('﻿profile = "Perfil padrão"\n'.encode())  # as Notepad may save it
    assert load_config(path).profile == "Perfil padrão"
    set_config_value("profile", "Café ☕", path)
    assert load_config(path).profile == "Café ☕"


def test_allow(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "load_config", lambda: load_config(path))
    monkeypatch.setattr(cli, "set_config_value", lambda k, v: set_config_value(k, v, path))
    args = type("Args", (), {"domains": ["https://GitHub.com/beSaif", "*.example.org", "localhost"], "remove": False})
    cli.allow(args)
    assert load_config(path).allowlist == ["localhost", "127.0.0.1", "github.com", "example.org"]
    args.domains, args.remove = ["127.0.0.1"], True
    cli.allow(args)
    assert load_config(path).allowlist == ["localhost", "github.com", "example.org"]
    assert capsys.readouterr().out.endswith("Allowlist: localhost, github.com, example.org\n")


def test_server_command(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    launcher = str(tmp_path / "bin" / "zenpilot")
    monkeypatch.setattr(sys, "argv", [launcher, "setup"])
    assert cli.server_command() == [launcher]
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "cache/archive-v0/abc"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/opt/bin/{name}")
    assert cli.server_command() == ["/opt/bin/uvx", "zenpilot"]


def test_server_command_names_the_windows_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "zenpilot.exe").touch()
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "zenpilot"), "setup"])
    assert cli.server_command() == [str(tmp_path / "zenpilot.exe")]


def test_windows_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    assert cli._uv_cache() == tmp_path / "Local/uv/cache"
    assert cli._desktop_config() == tmp_path / "Roaming/Claude/claude_desktop_config.json"
    store = tmp_path / "Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"
    store.mkdir(parents=True)
    (store / "claude_desktop_config.json").write_text("{}")
    assert cli._desktop_config() == store / "claude_desktop_config.json"  # the Store build's own copy
    (tmp_path / "Roaming/Claude").mkdir(parents=True)
    (tmp_path / "Roaming/Claude/claude_desktop_config.json").write_text("{}")
    # A stale %APPDATA% copy loses to the folder the app really runs from (it has "Local State").
    data = tmp_path / "Local/Claude-Data/claude_pzs8sxrjxfjjc"
    data.mkdir(parents=True)
    (data / "Local State").write_text("{}")
    assert cli._desktop_config() == data / "claude_desktop_config.json"


def test_register_desktop_keeps_other_settings(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "preferences": {"a": 1}}))
    assert "registered;" in cli.register_desktop(["/opt/bin/uvx", "zenpilot"], path)
    assert "already" in cli.register_desktop(["/opt/bin/uvx", "zenpilot"], path)
    data = json.loads(path.read_text())
    assert data["preferences"] == {"a": 1} and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["zenpilot"] == {"command": "/opt/bin/uvx", "args": ["zenpilot"]}
    assert "not installed" in cli.register_desktop(["x"], tmp_path / "missing/config.json")
