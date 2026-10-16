# zenpilot

[![CI](https://github.com/beSaif/zenpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/beSaif/zenpilot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An MCP server that lets Claude Desktop and Claude Code drive a running
[Zen Browser](https://zen-browser.app) the way Claude in Chrome drives Chrome.

It is one Python process speaking MCP over stdio. It talks to Zen over
[WebDriver BiDi](https://w3c.github.io/webdriver-bidi/), which Firefox (and so Zen)
exposes when started with `--remote-debugging-port`. There is no browser extension,
HTTP server or native messaging host.

> **Status: alpha.** Tested on macOS with Zen 1.23.2b (Firefox 157). The Python code is
> platform-neutral, but the setup below is macOS-specific. Plain Firefox should work the
> same way, but it isn't tested yet.

## Setup

Requires macOS and Zen in `/Applications`.

**Claude Desktop:** download `zenpilot-<version>.mcpb` from the
[latest release](https://github.com/beSaif/zenpilot/releases/latest) and open it.
Claude Desktop installs it as an extension and provides Python itself. That's all; the
rest happens in dialogs the first time Claude uses it (below).

**Claude Code, or both:** install with [uv](https://docs.astral.sh/uv/) and run the setup
wizard, which asks which profile to use and which sites to allow:

```sh
uv tool install zenpilot
zenpilot setup
```

It writes zenpilot's prefs to the profile's `user.js`, registers zenpilot with Claude
Code (user scope) and Claude Desktop if they're installed (restart Claude Desktop
afterwards), then starts Zen and prints its tabs.

### First use

Nothing needs starting by hand. When Claude calls a tool:

- **No profile chosen yet** (the Desktop extension): a dialog asks which Zen profile
  Claude should use. `zenpilot` is a separate profile, logged out of everything, that
  zenpilot creates; your own profile lets Claude use sites you're logged into.
- **Zen isn't running:** zenpilot starts it on that profile with remote debugging. The
  `zenpilot` profile runs as a second instance alongside your normal Zen.
- **The profile is already open without remote debugging** (the usual case for your own
  profile): a dialog asks to quit and reopen Zen. Zen restores your tabs, but lazily,
  so Claude only sees a restored tab once it has been opened.
- **A site isn't allowlisted:** a dialog asks *Don't Allow*, *Allow This Session* or
  *Always Allow*. *Always* saves it to the allowlist. A declined site isn't asked about
  again until the server restarts.

The dialogs are native macOS dialogs, so they work in every client and only the person
at the Mac can answer them; the model never sees them. They default to the safe answer,
so a stray Enter declines. Set `prompts = false` in the config to turn them all off.

Before Zen starts with remote debugging, zenpilot always sets
`remote.prefs.recommended = false` in the profile's `user.js`. Without it, Firefox's
remote agent rewrites about 40 prefs for test automation and Zen writes them into the
profile: Safe Browsing (phishing and malware), password saving and autofill, the popup
blocker, tracking protection, and crash session restore. If Zen doesn't quit cleanly,
they stay that way. zenpilot also only ever creates the `zenpilot` profile, so a
mistyped profile name fails with the list of real ones instead of making a new profile.

### Commands

| Command | What it does |
|---|---|
| `zenpilot` | The MCP server. Claude runs this; you don't. |
| `zenpilot setup [--profile NAME]` | The wizard above; `--profile` skips the questions. `--no-register` and `--no-launch` skip registering and starting Zen. |
| `zenpilot allow [DOMAIN...]` | Add sites to the allowlist; `--remove` takes them off; no arguments prints it |
| `zenpilot launch [PROFILE]` | Start Zen with remote debugging, offering to reopen it if it's open without |
| `zenpilot doctor` | Check each part of the setup and print a fix for anything wrong |

Without installing, `uvx zenpilot setup` works too and registers `uvx zenpilot` as the
server command; then run the other commands as `uvx zenpilot ...`.

To hack on it: `git clone https://github.com/beSaif/zenpilot.git && cd zenpilot && uv sync
&& uv run zenpilot setup`. That registers the clone's `.venv/bin/zenpilot`, so edits take
effect the next time Claude starts the server.

## Tools

| Tool | What it does |
|---|---|
| `list_tabs` | Tabs with short ids (`t3`); `*` marks the active one |
| `new_tab(url?)` | Open a tab, make it active, optionally load `url` |
| `switch_tab(tab_id)` | Activate a tab |
| `close_tab(tab_id)` | Close a tab |
| `navigate(url, tab_id?)` | Load a URL and wait for the load event |
| `read_page(tab_id?)` | Indented outline of visible interactive elements and text, with refs like `r12` |
| `click(ref)` | Real mouse click on a ref |
| `type(ref, text, submit=false)` | Replace a field's content with real key events; on a `<select>` it picks the option starting with `text` |
| `scroll(direction, amount=600, tab_id?)` | Mouse-wheel scroll |
| `screenshot(tab_id?)` | JPEG of the viewport |
| `get_text(tab_id?, max_chars=8000)` | Readable text of `<main>`, a single `<article>`, or `<body>` |

`tab_id` defaults to the active tab. Input goes through `input.performActions`, so pages
see trusted events (`event.isTrusted === true`).

Example `read_page` output:

```
form
  r3 textbox "Name" value="old value"
  r5 textbox "Password" type=password value="•••"
  r6 select "Color" value="Red"
  r7 checkbox "I agree"
  r9 button "Sign up"
frame "Same-origin frame"
  r14 button "Frame button"
frame "Cross-origin frame" [skipped: 127.0.0.1 not allowlisted]
```

Refs point to BiDi `sharedId` node references, not CSS selectors. They reach into open
shadow roots and into iframes, which are read through their own browsing context.
Calling `read_page` again, or navigating the page, invalidates a tab's refs. A stale or
unknown ref returns an error that tells the model to call `read_page` again. Output is
capped at about 12k characters; when a page is longer, plain text is dropped before
interactive elements.

## Safety

Config lives in `~/.config/zenpilot/config.toml` and is created on first run. Changes
apply on the next tool call. `zenpilot allow github.com` edits the allowlist for you.

```toml
allowlist = ["localhost", "127.0.0.1"]   # "example.com" also covers its subdomains
read_only = false                        # true disables click, type, navigate, new_tab, close_tab
dangerously_allow_all_sites = false      # true ignores the allowlist (see below)
profile = "zenpilot"                     # Zen profile to drive; asked on first use if unset
port = 9222
auto_launch = true                       # start Zen on that profile when a tool needs it
prompts = true                           # ask in macOS dialogs (see First use)
audit_log = "~/.local/state/zenpilot/audit.jsonl"
```

- **Allowlist.** Every tool that touches a page checks the tab's current domain. That
  covers reading, screenshots, clicking and typing. `navigate` also checks both the
  current domain and the target. Blocked calls return an error naming the domain and
  the `zenpilot allow` command, so Claude can ask you to run it.
  `list_tabs` hides the titles and URLs of other sites. A click can still take a tab to
  another domain, but every call after that is blocked. Only `http(s)` and `about:blank`
  are allowed; `file:`, `data:` and `about:config` are not.
- **Dangerous mode.** `dangerously_allow_all_sites = true` skips the allowlist for every
  http(s) site. Non-web schemes stay blocked, and fencing, read-only mode and the audit
  log still apply. Only the literal `true` turns it on. With a logged-in profile, this
  means a prompt injection on any page you visit can steer Claude inside your accounts.
  Prefer adding sites to the allowlist, or use it together with `read_only = true`.
- **Untrusted content.** Text from pages (`read_page`, `get_text`, tab titles) is wrapped
  in `<untrusted_web_content>`, followed by a note that it is data, not instructions.
  Fake closing tags inside the content are neutralized. Screenshots carry the same note.
- **Audit log.** Each call appends `{ts, tool, params, domain, ok, ms, error?}` as one
  JSON line. Typed text is recorded only as a character count.
- **Not included, on purpose.** There are no tools for cookies, storage, clipboard,
  downloads, passwords, clearing data, or running arbitrary JavaScript.
- **The debugging port.** Zen's remote agent listens only on 127.0.0.1. It rejects any
  connection that sends an `Origin` header, so web pages can't connect to it. Don't pass
  `--remote-allow-origins`. Any local process running as you can still connect when no
  session is open, which is one more reason to use a dedicated profile.

## How it behaves

- **One session at a time.** Firefox allows one BiDi session. zenpilot opens it on the
  first tool call and releases it after 2 minutes idle, so Claude Desktop and several
  Claude Code sessions can take turns. Refs and tab ids expire when the session is
  released. If another zenpilot holds the session, you get an "already has an active
  automation session" error.
- **Stuck sessions.** Firefox keeps a session alive after its socket closes. zenpilot
  ends the session on exit (stdin close, SIGTERM, SIGINT, SIGHUP). A `kill -9` can't be
  handled, though, and after one Zen refuses new sessions until you restart it.
- **Covered window.** zenpilot works while Zen's window is hidden or covered. Firefox
  doesn't answer `activate` in that state, or when Zen isn't the frontmost app, so
  zenpilot gives up waiting after 1s and `new_tab` can take about a second. If wheel
  scrolling doesn't move the page, it falls back to `scrollBy`.
- **Which tab is active.** BiDi has no "selected tab" API. zenpilot ranks tabs by
  `document.hasFocus()` and visibility, and uses the tab it last opened, switched to or
  navigated to as a tie-breaker.
- **No new windows.** Zen doesn't support creating windows over BiDi, so new tabs open
  in the current window.

## Tests

```sh
uv run pytest -m "not integration"   # unit tests: BiDi client (fake socket), ref tree, safety, CLI, prompts
uv run pytest                        # with Zen running (zenpilot launch): adds integration tests
uv run ruff check src tests && uv run ruff format src tests
```

CI runs the lint checks and unit tests on Python 3.12 and 3.13. Pushing a `v*` tag that
matches `pyproject.toml` runs `release.yml`: it publishes to PyPI (trusted publishing,
environment `pypi`) and creates a GitHub release with the wheel, sdist and `.mcpb`. The
extension's files are in `mcpb/`; a unit test keeps their version and tool list in sync.

The integration tests start the real `zenpilot` over MCP stdio and serve
`tests/fixture/` on localhost. The fixture has a form, buttons, a shadow-DOM widget,
same-origin and cross-origin iframes, and a link that opens a new tab. The tests
use a temporary config, so your allowlist and audit log aren't touched.

## Using your main profile

zenpilot works with any profile; the profile is only chosen when Zen starts. Pick yours
in the first-use dialog or in `zenpilot setup` (or set `profile` in the config, using
the name from `about:profiles`). Then, whenever Zen is open normally, Claude's next tool
call offers to reopen it with remote debugging.

Consider `auto_launch = false`, so that Claude can't start or reopen your logged-in
browser with the debugging port open. Start it with `zenpilot launch` when you want it.

While Zen runs this way:

- **Port access.** Any local process running as you can open a BiDi session on port
  9222 whenever zenpilot isn't holding it. BiDi can read cookies and run script in your
  logged-in sites, even though zenpilot exposes no such tools. Only run it like this
  while you need it, and quit and relaunch Zen normally afterwards.
- **Automation visible to sites.** `navigator.webdriver` is `true` and Zen shows its
  remote-control indicator. Some sites (banks, Cloudflare challenges) treat that as a
  bot.
- **One debug instance.** Only one Zen instance can have remote debugging on port 9222
  at a time.

## License

[MIT](LICENSE)
