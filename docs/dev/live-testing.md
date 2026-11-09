# Live testing

Run the integration tests with:

```sh
scripts/live-test.sh [checkout] [pytest args]   # e.g. scripts/live-test.sh ../pr20 -k read_console
```

It starts the `zenpilot` profile on port 9333, runs the tests from `checkout`, and closes that Zen again. Pass a worktree as `checkout` to test a PR's code.

Port 9222 is the default for zenpilot and for the tests, so a bare `uv run pytest` drives whichever Zen holds it, possibly the one you browse with.

## Ad-hoc scripts

Point a script at its own config and state, so it can't touch your allowlist, audit log or tab ids:

```sh
export ZENPILOT_CONFIG=$(mktemp -d)/config.toml ZENPILOT_STATE=$(mktemp -d)
printf 'port = 9333\nprofile = "zenpilot"\nprompts = false\n' > "$ZENPILOT_CONFIG"
uv run zenpilot launch
```

- Wrap the work in `try`/`finally: await b.close()`. A BiDi session left open, or a process killed with SIGKILL, makes Zen refuse new sessions until it restarts.
- Close one Zen with `zen.quit_zen(pid, profile)`, taking the pid from `zen.holder(profile)`. Other Zen instances keep running.

## Zen behaviour worth knowing

- Every BiDi session gets new context ids and sharedIds, so tabs are matched back by URL and refs by role and name.
- Tabs restore lazily: BiDi only sees a restored tab once it has been opened.
- Zen keeps a window when its last tab closes, so the "getTabBrowser(...) is null" no-window error can't be reproduced over BiDi.
- `Browser.screenshot` hides the in-page badge. Call `browsingContext.captureScreenshot` directly to see it.
- BiDi screenshots don't show Zen's rounded corners.

## Unit tests

Fake `zen.subprocess.run`, `port_open` and `holder`, or `launch()` runs the real Zen.
