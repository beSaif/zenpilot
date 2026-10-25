"""Native dialogs: questions only the person at the computer can answer, whatever the MCP client.

MCP elicitation would be the protocol-native way, but Claude Desktop doesn't support it.
macOS uses osascript; Windows uses a WinForms window shown by PowerShell. Either way the
text is passed as data (osascript arguments, an environment variable), never spliced into the script.
"""

import base64
import json
import os
import subprocess
import sys

# Claude's MCP clients time out a tool call after 60s by default; answer or give up before.
TIMEOUT = 45


def ask(message: str, buttons: list[str], default: str) -> str | None:
    """The button clicked, or None if the dialog timed out or couldn't be shown."""
    if sys.platform == "win32":
        picked = _windows({"kind": "ask", "message": message, "items": buttons, "default": buttons.index(default)})
        return None if picked is None else buttons[picked]
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
    if sys.platform == "win32":
        picked = _windows({"kind": "choose", "message": message, "items": items, "default": 0})
        return None if picked is None else items[picked]
    out = _osascript(
        'return choose from list (items 2 thru -1 of argv) with title "zenpilot" with prompt (item 1 of argv) '
        "default items {item 2 of argv}",
        [message, *items],
    )
    return None if out in (None, "false") else out


def _osascript(line: str, args: list[str]) -> str | None:
    cmd = ["osascript", "-e", "on run argv", "-e", line, "-e", "end run", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT + 5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# Reads {kind, message, items, default, timeout} from ZENPILOT_DIALOG and prints the index
# of the button or item picked, or nothing. Enter picks the default, Esc and closing cancel.
_WINFORMS = r"""
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$d = $env:ZENPILOT_DIALOG | ConvertFrom-Json
$global:picked = ''

$form = New-Object System.Windows.Forms.Form
$form.Text = 'zenpilot'
$form.TopMost = $true
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.AutoSize = $true
$form.AutoSizeMode = 'GrowAndShrink'
$form.Padding = New-Object System.Windows.Forms.Padding(12)
$form.Font = [System.Drawing.SystemFonts]::MessageBoxFont
$form.Icon = [System.Drawing.SystemIcons]::Warning

$page = New-Object System.Windows.Forms.FlowLayoutPanel
$page.FlowDirection = 'TopDown'
$page.AutoSize = $true
$page.WrapContents = $false
$form.Controls.Add($page)

$label = New-Object System.Windows.Forms.Label
$label.Text = $d.message
$label.AutoSize = $true
$label.MaximumSize = New-Object System.Drawing.Size(460, 0)
$label.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 12)
$page.Controls.Add($label)

$row = New-Object System.Windows.Forms.FlowLayoutPanel
$row.AutoSize = $true
$row.WrapContents = $false
$row.Anchor = 'Right'

function Add-Button($text, $onClick) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.AutoSize = $true
    $b.MinimumSize = New-Object System.Drawing.Size(88, 0)
    $b.Add_Click($onClick)
    $row.Controls.Add($b)
    return $b
}

if ($d.kind -eq 'choose') {
    $list = New-Object System.Windows.Forms.ListBox
    $list.Width = 460
    $list.IntegralHeight = $true
    $list.Height = [Math]::Min(12, [Math]::Max(4, $d.items.Count)) * $list.ItemHeight + 4
    foreach ($item in $d.items) { [void]$list.Items.Add([string]$item) }
    $list.SelectedIndex = $d.default
    $pick = { if ($list.SelectedIndex -ge 0) { $global:picked = [string]$list.SelectedIndex; $form.Close() } }
    $list.Add_DoubleClick($pick)
    $page.Controls.Add($list)
    $ok = Add-Button 'OK' $pick
    $cancel = Add-Button 'Cancel' { $form.Close() }
    $form.AcceptButton = $ok
    $form.CancelButton = $cancel
    $global:focus = $list
} else {
    for ($i = 0; $i -lt $d.items.Count; $i++) {
        $b = Add-Button ([string]$d.items[$i]) { $global:picked = [string]$this.Tag; $form.Close() }
        $b.Tag = $i
        if ($i -eq $d.default) { $form.AcceptButton = $b; $form.CancelButton = $b; $global:focus = $b }
    }
}
$page.Controls.Add($row)

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [int]$d.timeout * 1000
$timer.Add_Tick({ $timer.Stop(); $global:picked = ''; $form.Close() })
$form.Add_Shown({ $form.Activate(); [void]$global:focus.Focus(); $timer.Start() })
[void]$form.ShowDialog()
[Console]::Out.Write($global:picked)
"""


def _windows(spec: dict) -> int | None:
    """Show the WinForms dialog; the index picked, or None."""
    env = {**os.environ, "ZENPILOT_DIALOG": json.dumps({**spec, "timeout": TIMEOUT})}
    script = base64.b64encode(_WINFORMS.encode("utf-16-le")).decode()
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", script]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=TIMEOUT + 15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return int(out) if r.returncode == 0 and out.isdigit() and int(out) < len(spec["items"]) else None
