# Hacking on zenpilot

```sh
git clone https://github.com/beSaif/zenpilot.git && cd zenpilot
uv sync && uv run zenpilot setup     # points Claude at your clone
uv run pytest -m "not integration"   # unit tests
uv run pytest                        # all tests, needs Zen running (zenpilot launch)
ZENPILOT_TEST_PORT=9333 uv run pytest   # against a second Zen on a test profile
scripts/live-test.sh                 # integration tests on a throwaway Zen it starts and closes
```
