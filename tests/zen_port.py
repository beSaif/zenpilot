"""Port of the Zen remote agent the integration tests drive: ZENPILOT_TEST_PORT, then the user's config, then 9222."""

import os

from zenpilot.safety import CONFIG_PATH, load_config

DEFAULT_PORT = 9222


def zen_port(path=CONFIG_PATH) -> int:
    if env := os.environ.get("ZENPILOT_TEST_PORT"):
        return int(env)
    if not path.exists():  # load_config would create the user's config file
        return DEFAULT_PORT
    try:
        return load_config(path).port
    except (OSError, ValueError):
        return DEFAULT_PORT
