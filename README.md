# zenpilot

An MCP server that lets Claude Desktop and Claude Code drive a running
[Zen Browser](https://zen-browser.app) the way Claude in Chrome drives Chrome.

It is one Python process speaking MCP over stdio. It talks to Zen over
[WebDriver BiDi](https://w3c.github.io/webdriver-bidi/), which Firefox (and so Zen)
exposes when started with `--remote-debugging-port`. There is no browser extension,
HTTP server or native messaging host.

## Setup

Requires macOS, Zen in `/Applications`, and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> ~/Work/zenpilot && cd ~/Work/zenpilot
uv sync

# A dedicated, logged-out profile for zenpilot (one time)
/Applications/Zen.app/Contents/MacOS/zen -CreateProfile zenpilot
```

Add the launch alias to `~/.zshrc`:

```sh
alias zenr='pgrep -qf "zen -P zenpilot" && echo "Zen (zenpilot profile) is already running" || open -na /Applications/Zen.app --args -P zenpilot --remote-debugging-port 9222'
```

`zenr` starts a second Zen instance on the `zenpilot` profile alongside your normal one.
The first time, dismiss Zen's welcome screen. Until you do, pages have a 0×0 viewport
and clicks and screenshots fail.

Check the connection:

```sh
uv run zenpilot --check   # prints the tab list, * marks the active tab
```

### Claude Desktop

In `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "zenpilot": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--directory", "/Users/you/Work/zenpilot", "zenpilot"]
    }
  }
}
```

Use absolute paths, because Claude Desktop doesn't run with your shell's `PATH`. Restart
Claude Desktop afterwards.

### Claude Code

```sh
claude mcp add --scope user zenpilot -- /opt/homebrew/bin/uv run --directory ~/Work/zenpilot zenpilot
```

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
apply on the next tool call.

```toml
allowlist = ["localhost", "127.0.0.1"]   # "example.com" also covers its subdomains
read_only = false                        # true disables click, type, navigate, new_tab, close_tab
port = 9222
audit_log = "~/.local/state/zenpilot/audit.jsonl"
```

- **Allowlist.** Every tool that touches a page checks the tab's current domain. That
  covers reading, screenshots, clicking and typing. `navigate` also checks both the
  current domain and the target. Blocked calls return an error naming the domain.
  `list_tabs` hides the titles and URLs of other sites. A click can still take a tab to
  another domain, but every call after that is blocked. Only `http(s)` and `about:blank`
  are allowed; `file:`, `data:` and `about:config` are not.
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
  doesn't answer `activate` in that state, so zenpilot uses a short timeout. If wheel
  scrolling doesn't move the page, it falls back to `scrollBy`.
- **Which tab is active.** BiDi has no "selected tab" API. zenpilot ranks tabs by
  `document.hasFocus()` and visibility, and uses the tab it last opened, switched to or
  navigated to as a tie-breaker.
- **No new windows.** Zen doesn't support creating windows over BiDi, so new tabs open
  in the current window.

## Tests

```sh
uv run pytest -m "not integration"   # unit tests: BiDi client (fake socket), ref tree, safety
uv run pytest                        # with Zen started via zenr: adds integration tests
```

The integration tests start the real `zenpilot` over MCP stdio and serve
`tests/fixture/` on localhost. The fixture has a form, buttons, a shadow-DOM widget,
same-origin and cross-origin iframes, and a link that opens a new tab. The tests
use a temporary config, so your allowlist and audit log aren't touched.

## Using your main profile

Use the dedicated profile until you trust the setup. To switch later, change `-P
zenpilot` in the alias to your profile's name (see `about:profiles`). Firefox only
enables the remote agent when Zen starts with the flag, so you'll need to quit and
relaunch Zen with `zenr`.
