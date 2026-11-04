"""Native dialogs: questions only the person at the computer can answer, whatever the MCP client.

MCP elicitation would be the protocol-native way, but Claude Desktop doesn't support it.
On macOS text is passed as osascript arguments, never spliced into the script. On Linux it uses
zenity or kdialog, and notify-send for notifications. With none of them (or no display) every
question is unanswered, which callers treat as "nobody was there".
"""

import os
import shutil
import subprocess
import sys

# Claude's MCP clients time out a tool call after 60s by default; answer or give up before.
TIMEOUT = 45

MAC = sys.platform == "darwin"


def _run(cmd: list[str], timeout: float = TIMEOUT + 5) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _osascript(line: str, args: list[str]) -> str | None:
    r = _run(["osascript", "-e", "on run argv", "-e", line, "-e", "end run", *args])
    return r.stdout.strip() if r and r.returncode == 0 else None


def _linux_tool() -> str | None:
    """zenity or kdialog, when there's a screen to show it on."""
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return None
    # Prefer the toolkit of the desktop the person is on.
    order = ("kdialog", "zenity") if "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "") else ("zenity", "kdialog")
    return next((t for t in order if shutil.which(t)), None)


def ask(message: str, buttons: list[str], default: str) -> str | None:
    """The button clicked, or None if the dialog timed out or couldn't be shown."""
    if MAC:
        out = _osascript(
            "display dialog (item 1 of argv) buttons (items 3 thru -1 of argv) default button (item 2 of argv) "
            f'with title "zenpilot" with icon caution giving up after {TIMEOUT}',
            [message, default, *buttons],
        )
        if not out or out.endswith("gave up:true"):
            return None
        return out.removeprefix("button returned:").rsplit(", gave up:", 1)[0]
    tool = _linux_tool()
    others = [b for b in buttons if b != default]
    if tool == "zenity":
        # Enter is OK and Esc is Cancel, and both must give the safe answer: OK is the default
        # button, the rest are extra buttons that print their label, and Cancel means the default.
        cmd = ["zenity", "--question", "--title=zenpilot", f"--text={message}", "--no-markup"]
        cmd += [f"--ok-label={default}", "--cancel-label=Cancel", *(f"--extra-button={b}" for b in others)]
        # Not zenity's own --timeout: on a timeout it has printed the last extra button, as if it was clicked.
        r = _run(cmd, TIMEOUT)
        return None if r is None else r.stdout.strip() or default
    if tool == "kdialog":
        # kdialog has too few buttons: use a list with the default preselected.
        r = _pick("kdialog", message, [default, *others], TIMEOUT)
        return None if r is None else r.stdout.strip() or default  # cancelled or closed: the safe answer
    return None


def choose(message: str, items: list[str]) -> str | None:
    """The item picked (the first is preselected), or None if cancelled or unavailable."""
    if MAC:
        out = _osascript(
            'return choose from list (items 2 thru -1 of argv) with title "zenpilot" with prompt (item 1 of argv) '
            "default items {item 2 of argv}",
            [message, *items],
        )
        return None if out in (None, "false") else out
    tool = _linux_tool()
    r = _pick(tool, message, items, TIMEOUT) if tool else None
    out = r.stdout.strip() if r and r.returncode == 0 else ""
    return out or None


def _pick(tool: str, message: str, items: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """A radio list with the first item preselected, printing the one chosen."""
    if tool == "zenity":
        rows = [x for i, item in enumerate(items) for x in ("TRUE" if i == 0 else "FALSE", item)]
        cmd = ["zenity", "--list", "--radiolist", "--title=zenpilot", f"--text={message}", "--no-markup"]
        cmd += ["--column=", "--column=Choice", "--print-column=2", "--width=560", "--height=360", *rows]
    else:
        rows = [x for i, item in enumerate(items) for x in (item, item, "on" if i == 0 else "off")]
        cmd = ["kdialog", "--title", "zenpilot", "--radiolist", message, *rows]
    return _run(cmd, timeout)


def notify(message: str, subtitle: str = "") -> None:
    """A notification in the corner of the screen; nothing to answer."""
    if MAC:
        _osascript(
            'display notification (item 1 of argv) with title "zenpilot" subtitle (item 2 of argv)',
            [message, subtitle],
        )
    elif shutil.which("notify-send"):
        _run(["notify-send", "--app-name=zenpilot", "--", f"zenpilot: {subtitle}" if subtitle else "zenpilot", message])
