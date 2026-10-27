"""Which Zen port the integration tests use: env, then the user's config, then 9222."""

from zen_port import zen_port


def write_config(path, text):
    path.write_text(text)
    return path


def test_env_wins_over_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ZENPILOT_TEST_PORT", "9300")
    assert zen_port(write_config(tmp_path / "c.toml", "port = 9223\n")) == 9300


def test_config_port_when_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("ZENPILOT_TEST_PORT", raising=False)
    assert zen_port(write_config(tmp_path / "c.toml", "port = 9223\n")) == 9223


def test_falls_back_to_9222_when_config_unreadable(tmp_path, monkeypatch):
    monkeypatch.delenv("ZENPILOT_TEST_PORT", raising=False)
    assert zen_port(tmp_path) == 9222  # a directory: reading it raises OSError


def test_falls_back_to_9222_when_config_is_invalid(tmp_path, monkeypatch):
    monkeypatch.delenv("ZENPILOT_TEST_PORT", raising=False)
    assert zen_port(write_config(tmp_path / "c.toml", "port = [\n")) == 9222
