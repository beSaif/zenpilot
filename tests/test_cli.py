"""Profile handling, config editing and client registration, without touching the real Zen or Claude."""

import json
import os
import subprocess
import sys

import pytest

from zenpilot import cli, safety, zen
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
    """launch() with the port, the profile lock, quitting and `open` faked."""
    calls = []
    monkeypatch.setattr(zen, "command", lambda: ["zen"])  # like an installed Zen
    monkeypatch.setattr(zen, "port_open", lambda port: bool(calls and calls[-1][0] == "open"))
    monkeypatch.setattr(zen, "holder", lambda path: 123)
    monkeypatch.setattr(zen, "quit_zen", lambda pid, path: calls.append(("quit", pid)))
    monkeypatch.setattr(zen, "_start", lambda cmd, name, port: calls.append(("open", name)))
    return calls


def test_launch_reopens_an_open_profile_only_when_confirmed(zen_dir, fake_launch):
    profile = zen_dir / "Profiles/abc.zenpilot"
    with pytest.raises(zen.ZenError, match="open without remote debugging"):
        zen.launch("zenpilot", 9222, lambda question: False)
    assert fake_launch == [] and zen.is_safe(profile)  # the safety pref is written before anything else
    questions = []
    assert zen.launch("zenpilot", 9222, lambda q: questions.append(q) or True) is True
    assert fake_launch == [("quit", 123), ("open", "zenpilot")]
    assert "Quit and reopen Zen" in questions[0]


needs_proc = pytest.mark.skipif(sys.platform != "linux", reason="needs /proc")


@needs_proc
def test_holder_reads_the_linux_lock_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(zen, "MAC", False)
    assert zen.holder(tmp_path) is None
    (tmp_path / "lock").symlink_to(f"127.0.0.1:+{os.getpid()}")
    assert zen.holder(tmp_path) == os.getpid()
    (tmp_path / "lock").unlink()
    (tmp_path / "lock").symlink_to("127.0.0.1:+4194303")  # a stale lock left by a crash
    assert zen.holder(tmp_path) is None


@needs_proc
def test_quit_zen_leaves_other_processes_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(zen, "MAC", False)
    killed = []
    monkeypatch.setattr(zen.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(zen, "holder", lambda profile: None)
    with pytest.raises(zen.ZenError, match="can't stop it"):
        zen.quit_zen(os.getpid(), tmp_path)  # this is pytest, not Zen
    assert killed == []


def test_command_finds_zen_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(zen, "MAC", False)
    monkeypatch.setattr(zen, "LINUX_BINS", [tmp_path / "zen"])
    monkeypatch.setattr(zen.shutil, "which", lambda name: None)
    monkeypatch.delenv("ZENPILOT_ZEN", raising=False)
    monkeypatch.setattr(zen.Path, "home", lambda: tmp_path)
    assert zen.command() is None
    (tmp_path / "zen").touch()
    assert zen.command() == [str(tmp_path / "zen")]
    monkeypatch.setenv("ZENPILOT_ZEN", "flatpak run app.zen_browser.zen")
    assert zen.command() == ["flatpak", "run", "app.zen_browser.zen"]


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


def test_allow_lists_and_removes_sites_allowed_for_a_while(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "load_config", lambda: load_config(path))
    monkeypatch.setattr(cli, "set_config_value", lambda k, v: set_config_value(k, v, path))
    safety.grant("jobs.example.org")
    args = type("Args", (), {"domains": [], "remove": False})
    cli.allow(args)
    assert "For now: jobs.example.org (until " in capsys.readouterr().out
    args.domains, args.remove = ["jobs.example.org"], True
    cli.allow(args)
    assert safety.grants() == {} and "For now" not in capsys.readouterr().out


def test_zenpilot_in_a_terminal_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["zenpilot"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_update", lambda: pytest.fail("would start the server"))
    cli.main()
    assert "zenpilot setup" in capsys.readouterr().out


def test_server_command(tmp_path, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(sys, "argv", ["/home/me/.local/bin/zenpilot", "setup"])
    assert cli.server_command() == ["/home/me/.local/bin/zenpilot"]
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "cache/archive-v0/abc"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/opt/bin/{name}")
    assert cli.server_command() == ["/opt/bin/uvx", "zenpilot"]


def test_register_desktop_keeps_other_settings(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "preferences": {"a": 1}}))
    assert "registered;" in cli.register_desktop(["/opt/bin/uvx", "zenpilot"], path)
    assert "already" in cli.register_desktop(["/opt/bin/uvx", "zenpilot"], path)
    data = json.loads(path.read_text())
    assert data["preferences"] == {"a": 1} and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["zenpilot"] == {"command": "/opt/bin/uvx", "args": ["zenpilot"]}
    assert "not installed" in cli.register_desktop(["x"], tmp_path / "missing/config.json")


def test_log_prints_a_timeline(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    log = tmp_path / "audit.jsonl"
    path.write_text(f'audit_log = "{log}"\n')
    monkeypatch.setattr(cli, "load_config", lambda: load_config(path))
    site = "career55.sapsf.eu"
    calls = [
        {"tool": "click", "params": {"ref": "r55", "element": 'button "Next"'}, "domain": site, "ok": True},
        {"tool": "upload", "params": {"ref": "r9", "paths": ["/d/cv.pdf"], "element": 'file "CV"'}, "domain": site},
        {"tool": "read_page", "params": {"tab_id": "t5"}, "ok": False, "error": "Unknown tab id 't5'. Call list_tabs."},
    ]
    lines = [json.dumps({"ts": f"2026-10-28T06:4{i}:00+00:00", "ms": 1, "ok": True} | c) for i, c in enumerate(calls)]
    log.write_text("\n".join([lines[0], "not json", *lines[1:]]) + "\n")
    cli.log(type("Args", (), {"n": 50, "errors": False}))
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3
    assert 'click      r55 button "Next"' in out[0] and out[0].rstrip().endswith(f"{site:<24} ok")
    assert 'upload     r9 file "CV": cv.pdf' in out[1]
    assert out[2].endswith("✗ Unknown tab id 't5'")
    cli.log(type("Args", (), {"n": 1, "errors": True}))
    assert capsys.readouterr().out.count("\n") == 1


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    """zenpilot running from a `uv tool install` of 0.4.1, with uv, the upgrade and the restart faked."""
    tools = tmp_path / "tools"
    monkeypatch.setenv("UV_TOOL_DIR", str(tools))
    monkeypatch.delenv("ZENPILOT_UPDATED", raising=False)
    monkeypatch.setattr(sys, "prefix", str(tools / "zenpilot"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/uv")
    env = {"installed": "0.4.1", "on_pypi": "0.4.1", "calls": []}
    monkeypatch.setattr(cli.importlib.metadata, "version", lambda name: env["installed"])
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: env["calls"].append(("restart", path)))

    def run(cmd, **kw):
        env["calls"].append(("run", cmd[1:]))
        env["installed"] = env["on_pypi"]
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    return env


def test_update_restarts_into_a_newer_version(tool_env):
    tool_env["on_pypi"] = "0.4.3"
    cli._update()
    assert tool_env["calls"] == [("run", ["tool", "upgrade", "zenpilot"]), ("restart", sys.argv[0])]
    assert cli.os.environ["ZENPILOT_UPDATED"] == "1"  # the restarted one doesn't check again


def test_update_runs_the_installed_version_when_nothing_is_newer(tool_env):
    cli._update()
    assert tool_env["calls"] == [("run", ["tool", "upgrade", "zenpilot"])]


def test_update_leaves_a_clone_or_the_desktop_extension_alone(tool_env, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "zenpilot-clone/.venv"))
    cli._update()
    assert tool_env["calls"] == []


def test_update_offline_or_slow_runs_the_installed_version(tool_env, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)

    monkeypatch.setattr(cli.subprocess, "run", slow)
    cli._update()
    assert tool_env["calls"] == [] and tool_env["installed"] == "0.4.1"


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """A Claude Desktop folder holding the extension: manifest 0.5.1, updated itself to 0.6.0."""
    base = tmp_path / "Claude"
    ext = base / "Claude Extensions/local.mcpb.besaif.zenpilot"
    (ext / ".venv/lib/python3.14/site-packages/zenpilot-0.6.0.dist-info").mkdir(parents=True)
    (ext / "manifest.json").write_text(json.dumps({"name": "zenpilot", "version": "0.5.1"}))
    (base / "Claude Extensions Settings").mkdir()
    monkeypatch.setattr(cli, "DESKTOP_CONFIG", base / "claude_desktop_config.json")
    monkeypatch.setattr(cli, "CLAUDE_CODE_CONFIG", tmp_path / "claude.json")
    monkeypatch.setattr(cli, "load_config", lambda: load_config(tmp_path / "config.toml"))
    monkeypatch.setattr(zen, "command", lambda: ["zen"])
    monkeypatch.setattr(zen, "installed_at", lambda: "/zen")
    monkeypatch.setattr(zen, "port_open", lambda port: False)
    return base


def set_extension_enabled(base, enabled):
    (base / "Claude Extensions Settings/local.mcpb.besaif.zenpilot.json").write_text(json.dumps({"isEnabled": enabled}))


def register_on_desktop(base):
    (base / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"zenpilot": {"command": "zp"}}}))


def test_doctor_shows_the_version_the_extension_really_runs(desktop, capsys):
    set_extension_enabled(desktop, True)
    assert cli.doctor(None) == 0
    out = capsys.readouterr().out
    assert "extension local.mcpb.besaif.zenpilot: zenpilot 0.6.0" in out
    assert f"zenpilot {cli.importlib.metadata.version('zenpilot')} in {sys.prefix}" in out


def test_doctor_flags_two_zenpilots_in_claude_desktop(desktop, capsys):
    set_extension_enabled(desktop, True)
    register_on_desktop(desktop)
    assert cli.doctor(None) == 1
    assert "more than one zenpilot" in capsys.readouterr().out


def test_doctor_ignores_a_disabled_extension(desktop, capsys):
    set_extension_enabled(desktop, False)
    register_on_desktop(desktop)
    assert cli.doctor(None) == 0
    out = capsys.readouterr().out
    assert "disabled" in out and "more than one" not in out
