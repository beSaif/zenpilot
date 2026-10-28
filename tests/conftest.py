import pytest

from zenpilot import safety


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Tab ids and site grants go to a temp folder, never the user's."""
    monkeypatch.setattr(safety, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"
