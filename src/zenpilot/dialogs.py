"""Native macOS dialogs: questions only the person at the Mac can answer, whatever the MCP client.

MCP elicitation would be the protocol-native way, but Claude Desktop doesn't support it.
Text is passed as osascript arguments, never spliced into the script.
"""

import subprocess

# Claude's MCP clients time out a tool call after 60s by default; answer or give up before.
TIMEOUT = 45


def _osascript(line: str, args: list[str]) -> str | None:
    cmd = ["osascript", "-e", "on run argv", "-e", line, "-e", "end run", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def ask(message: str, buttons: list[str], default: str) -> str | None:
    """The button clicked, or None if the dialog timed out or couldn't be shown."""
    out = _osascript(
        "display dialog (item 1 of argv) buttons (items 3 thru -1 of argv) default button (item 2 of argv) "
        f'with title "zenpilot" with icon caution giving up after {TIMEOUT}',
        [message, default, *buttons],
    )
    if not out or out.endswith("gave up:true"):
        return None
    return out.removeprefix("button returned:").rsplit(", gave up:", 1)[0]


def choose(message: str, items: list[str]) -> str | None:
    """The item picked (the first is preselected), or None if cancelled or unavailable."""
    out = _osascript(
        'return choose from list (items 2 thru -1 of argv) with title "zenpilot" with prompt (item 1 of argv) '
        "default items {item 2 of argv}",
        [message, *items],
    )
    return None if out in (None, "false") else out


def notify(message: str, subtitle: str = "") -> None:
    """A notification in the corner of the screen; nothing to answer."""
    _osascript(
        'display notification (item 1 of argv) with title "zenpilot" subtitle (item 2 of argv)', [message, subtitle]
    )
