"""Claude Desktop runs this with `uv run`. It first updates zenpilot from PyPI, so the extension
stays current without reinstalling it; offline or slow, the installed version starts."""

import importlib
import os
import shutil
import subprocess
from pathlib import Path

uv = os.environ.get("UV") or shutil.which("uv")  # uv run sets UV to itself
if uv:
    try:
        subprocess.run(
            [uv, "sync", "--directory", str(Path(__file__).parent), "--upgrade-package", "zenpilot", "--quiet"],
            capture_output=True,  # stdout is the MCP channel
            timeout=20,
            env={**os.environ, "UV_HTTP_TIMEOUT": "8", "UV_HTTP_RETRIES": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    importlib.invalidate_caches()

from zenpilot import main  # noqa: E402

main()
