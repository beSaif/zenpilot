#!/bin/sh
# Integration tests on a throwaway Zen: the `zenpilot` profile on port 9333, never the browser you use.
# Usage: scripts/live-test.sh [checkout] [pytest args]
set -eu

checkout=$(dirname "$0")/..
if [ $# -gt 0 ] && [ -f "$1/pyproject.toml" ]; then
    checkout=$1
    shift
fi
cd "$checkout"

exec uv run --locked python - "$@" <<'EOF'
import os
import subprocess
import sys

from zenpilot import zen

PORT = 9333
profile = zen.resolve(zen.DEFAULT_PROFILE)
if zen.port_open(PORT) and not zen.holder(profile):
    sys.exit(f"Something other than the {zen.DEFAULT_PROFILE} profile is listening on port {PORT}. Stop it and retry.")

# A Zen that was already up stays up; only the one started here is closed.
started = zen.launch(zen.DEFAULT_PROFILE, PORT, prefs=[zen.UPLOAD_PREF])
try:
    env = {**os.environ, "ZENPILOT_TEST_PORT": str(PORT)}
    code = subprocess.call([sys.executable, "-m", "pytest", "-m", "integration", *sys.argv[1:]], env=env)
finally:
    if started and (pid := zen.holder(profile)):
        zen.quit_zen(pid, profile)
sys.exit(code)
EOF
