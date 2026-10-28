"""The dialogs zenpilot shows: site access and the first-run profile choice (dialogs faked)."""

import pytest

from zenpilot import server
from zenpilot.safety import Config, load_config, set_config_value


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(server, "load_config", lambda: load_config(path))
    monkeypatch.setattr(server, "set_config_value", lambda k, v: set_config_value(k, v, path))
    monkeypatch.setattr(server, "_declined", set())
    answers, asked = [], []

    def ask(message, buttons, default):
        asked.append((message, buttons, default))
        return answers.pop(0)

    monkeypatch.setattr(server.dialogs, "ask", ask)
    return path, answers, asked


def cfg():
    return Config(["localhost"], False, False, 9222, None, profile="Default (release)")


def test_always_allow_saves_to_the_allowlist(env):
    path, answers, asked = env
    answers.append("Always Allow")
    c = cfg()
    assert server._grant("github.com", c) is True
    assert "github.com" in c.allowlist and "github.com" in load_config(path).allowlist
    message, buttons, default = asked[0]
    assert "github.com" in message and "Default (release)" in message
    assert default == "Don't Allow"  # a stray Enter must not grant access


def test_allow_for_hours_outlives_the_server_but_not_the_hours(env, monkeypatch):
    path, answers, asked = env
    answers.append("Allow for 12 Hours")
    assert server._grant("github.com", cfg()) is True
    assert "Allow for 12 Hours" in asked[0][1]
    assert "github.com" not in load_config(path).allowlist
    # Kept in the state file, so a restarted server (a resumed Claude session) still has it.
    assert server._granted() == ["github.com"]
    later = server.time.time() + 13 * 3600
    monkeypatch.setattr(server.time, "time", lambda: later)
    assert server._granted() == []


@pytest.mark.parametrize("answer", ["Don't Allow", None])  # None: timed out
def test_declined_sites_are_not_asked_again(env, answer):
    _, answers, asked = env
    answers.append(answer)
    assert server._grant("evil.com", cfg()) is False
    assert server._grant("evil.com", cfg()) is False
    assert len(asked) == 1


def test_choose_profile(monkeypatch):
    monkeypatch.setattr(server, "profile_names", lambda: ["Default (release)", "zenpilot"])
    monkeypatch.setattr(server, "describe", lambda name: "open now")
    shown = []
    monkeypatch.setattr(server.dialogs, "choose", lambda msg, items: shown.extend(items) or items[0])
    assert server._choose_profile() == "zenpilot"
    assert shown[1:] == ["Default (release) (open now)"]
    monkeypatch.setattr(server.dialogs, "choose", lambda msg, items: "Default (release) (open now)")
    assert server._choose_profile() == "Default (release)"
    monkeypatch.setattr(server.dialogs, "choose", lambda msg, items: None)
    with pytest.raises(server.ZenError, match="No Zen profile was chosen"):
        server._choose_profile()
