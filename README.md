<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/beSaif/zenpilot/main/docs/logo-wordmark-dark.svg">
    <img alt="zenpilot" src="https://raw.githubusercontent.com/beSaif/zenpilot/main/docs/logo-wordmark.svg" height="56">
  </picture>
</h1>

<!-- mcp-name: io.github.beSaif/zenpilot -->

[![CI](https://github.com/beSaif/zenpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/beSaif/zenpilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Let Claude use your [Zen browser](https://zen-browser.app). Works with Claude Desktop and
Claude Code, kind of like Claude in Chrome but for Zen.

![zenpilot demo: install, set up, then Claude fills in and submits a form in Zen](https://raw.githubusercontent.com/beSaif/zenpilot/main/docs/demo.gif)

There's no browser extension. zenpilot opens Zen with its remote control turned on and
talks to it directly.

> **Alpha.** Only tested on macOS with Zen 1.23.

## Install

You need a Mac with Zen in `/Applications`.

**Claude Desktop:** download `zenpilot-<version>.mcpb` from the
[latest release](https://github.com/beSaif/zenpilot/releases/latest) and open it. That's it.

**Claude Code (or both):** with [uv](https://docs.astral.sh/uv/) installed, run:

```sh
uv tool install zenpilot
zenpilot setup
```

Setup asks which Zen profile to use and which sites Claude may visit. Then it connects
zenpilot to Claude Code (and Claude Desktop, if you have it) and opens Zen. Restart Claude
Desktop afterwards.

Either way, zenpilot updates itself to the newest release each time Claude starts it. To stay
on one version with uv, install it pinned: `uv tool install zenpilot==0.5.0`.

## Use it

Just ask Claude: *"order a large pizza with bacon on httpbin.org/forms/post for Sam, then submit it"*.

The first time, a few popups may show up. Only you see them, not Claude:

- **Which profile?** `zenpilot` is a fresh profile that isn't logged in to anything. Pick
  your own profile if you want Claude to use sites you're logged into.
- **Reopen Zen?** If Zen is already open without remote control, zenpilot offers to restart
  it. Your tabs come back.
- **Allow this site?** Claude only works on sites you allow. Choose *Don't Allow*, *Allow
  for 12 Hours* or *Always Allow*. If you miss it, it asks again next time. After *Don't
  Allow* it waits 10 minutes before asking again, or run `zenpilot allow <site>`.

Pressing Enter always picks the safe answer.

While Claude works in a tab, an orange glow goes around the page and a zenpilot badge in the
bottom-left corner says so. Press **Stop** on it and Claude stops and asks you what to do. If
it tries again anyway, a popup asks whether to let it continue.

Claude can open, switch and close tabs, go to a URL, read the page, click, type, pick from
dropdowns, scroll and take screenshots. It can attach files to upload, but only from folders
you list in `upload_dirs`. It can't touch cookies, passwords or downloads, and it can't run its
own JavaScript.

## When it gets stuck

zenpilot is far from perfect, and every site is different. When Claude can't do something
(say, drag and drop), it notes what it tried. zenpilot also notes tool calls that failed or
took over 5 seconds. If anything went wrong, a notification at the end of the session points
you to a report in `~/.local/state/zenpilot/reports/`.

```sh
zenpilot report           # show the latest report
zenpilot report --issue   # open a GitHub issue with it filled in
```

What you typed and the pages' text stay out of it, Claude is asked to leave out personal data,
and URLs lose their `?query` part. Still, read it before you submit.

## Staying safe

- **Allowlist.** Claude only works on sites you've allowed. Add one with
  `zenpilot allow github.com`.
- **Stick to the `zenpilot` profile if you can.** It's logged out of everything. On your main
  profile, a shady page could try to trick Claude into doing stuff in your accounts, so only
  allow sites you trust.
- **Your default profile is opt-in.** Pick Zen's default profile (`profile = "default"`, or
  the setup choice) to let Claude use your logins. zenpilot then attaches to the Zen you
  already have open, starts it with `open -a` (no second instance), and never writes to that
  profile's `user.js`. Uploads don't work on it, so keep a dedicated profile for those.
- **Read-only mode.** Set `read_only = true` and Claude can look but not click or type.
- **Log.** Everything Claude does is written to `~/.local/state/zenpilot/audit.jsonl`, with
  what it clicked. Typed text isn't logged, only its length. `zenpilot log` shows it as a timeline.
- **Remote control is open while Zen runs this way.** Other apps on your Mac could connect
  to it too, and some sites (banks, Cloudflare checks) may think you're a bot. When you're
  done, quit Zen and open it normally.

zenpilot also stops Zen from switching off its own protections (Safe Browsing, the popup
blocker and so on), which would otherwise happen when remote control is on.

## Commands

| Command | What it does |
|---|---|
| `zenpilot setup` | Set everything up (see above) |
| `zenpilot allow [site...]` | Allow sites. `--remove` takes them off (12-hour ones too). No sites lists them. |
| `zenpilot launch` | Open Zen with remote control on |
| `zenpilot doctor` | Check your setup and say how to fix anything that's broken |
| `zenpilot report` | Show the latest session report. `--issue` files it on GitHub, `--list` lists them all. |
| `zenpilot log` | Show what Claude did lately. `-n 200` for more, `--errors` for failures only |

## Settings

They live in `~/.config/zenpilot/config.toml`, and changes apply right away.

```toml
allowlist = ["localhost", "127.0.0.1"]   # "example.com" covers its subdomains too
read_only = false                        # true: Claude can look but not click or type
profile = "zenpilot"                     # which Zen profile Claude uses
auto_launch = true                       # open Zen when Claude needs it
prompts = true                           # show the popups above
port = 9222
audit_log = "~/.local/state/zenpilot/audit.jsonl"
report_dir = "~/.local/state/zenpilot/reports"
audit_redact_urls = false                # true: leave ?query and #fragment out of the log
upload_dirs = []                         # folders Claude may upload files from, e.g. ["~/Documents/CV"]
dangerously_allow_all_sites = false      # skips the allowlist. Please don't.
```

Uploads need one Firefox setting (`dom.file.createInChild`) in the profile. zenpilot adds it
once `upload_dirs` isn't empty, and it takes effect the next time Zen starts.

## Good to know

- Only one Claude can drive Zen at a time. zenpilot lets go after 2 minutes of doing
  nothing, so Claude Desktop and Claude Code can take turns.
- Zen can be hidden or behind other windows. It still works.
- Zen loads restored tabs lazily, so Claude only sees an old tab once you've opened it.
- Stuck? Run `zenpilot doctor`. If Zen says it already has a session after a crash, restart Zen.

## Hacking on it

```sh
git clone https://github.com/beSaif/zenpilot.git && cd zenpilot
uv sync && uv run zenpilot setup     # points Claude at your clone
uv run pytest -m "not integration"   # unit tests
uv run pytest                        # all tests, needs Zen running (zenpilot launch)
ZENPILOT_TEST_PORT=9333 uv run pytest   # against a second Zen on a test profile
```

## License

[MIT](LICENSE)
