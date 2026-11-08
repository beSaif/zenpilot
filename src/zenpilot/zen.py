"""Finding, preparing and launching Zen profiles (macOS and Linux)."""

import configparser
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

MAC = sys.platform == "darwin"
ZEN_APP = Path("/Applications/Zen.app")
ZEN_BIN = ZEN_APP / "Contents/MacOS/zen"
FLATPAK_ID = "app.zen_browser.zen"
# Where Linux installs that aren't on PATH put the binary (tarball, AUR, distro packages).
LINUX_BINS = [
    Path("/opt/zen/zen"),
    Path("/opt/zen-browser-bin/zen-bin"),
    Path("/usr/lib/zen/zen"),
    Path("~/.local/share/zen/zen").expanduser(),
    Path("~/.local/zen/zen").expanduser(),
]


def _data_dir() -> Path:
    """Where Zen keeps profiles.ini. Linux has moved between ~/.zen and ~/.config/zen, so look."""
    home = Path.home()
    if MAC:
        return home / "Library/Application Support/zen"
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    flatpak = home / ".var/app" / FLATPAK_ID
    candidates = [home / ".zen", config / "zen", flatpak / ".zen", flatpak / "config/zen"]
    return next((d for d in candidates if (d / "profiles.ini").exists()), candidates[0])


ZEN_DIR = _data_dir()
DEFAULT_PROFILE = "zenpilot"  # the dedicated, logged-out profile zenpilot creates

# With remote debugging, Firefox's remote agent rewrites ~40 prefs for test automation
# (Safe Browsing, password saving, popup blocker, ...) and Zen persists them. This stops it.
SAFE_PREF = ("remote.prefs.recommended", "false")
# Until Zen's welcome screen is dismissed, pages have a 0x0 viewport.
WELCOME_PREF = ("zen.welcome-screen.seen", "true")
# The one recommended automation pref uploads need: input.setFiles builds File objects in the
# page's process. Only written once the user lists upload_dirs.
UPLOAD_PREF = ("dom.file.createInChild", "true")

_PREF_RE = re.compile(r'^\s*user_pref\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;', re.M)


class ZenError(Exception):
    """A setup or launch failure with a message for the user (or the model)."""


def command() -> list[str] | None:
    """The command that runs Zen, or None if it isn't installed.

    On Linux, $ZENPILOT_ZEN overrides the search (it may hold a command with arguments).
    """
    if MAC:
        return [str(ZEN_BIN)] if ZEN_BIN.exists() else None
    if override := os.environ.get("ZENPILOT_ZEN"):
        return shlex.split(override)
    for name in ("zen", "zen-browser", "zen-bin"):
        if found := shutil.which(name):
            return [found]
    for path in LINUX_BINS:
        if path.exists():
            return [str(path)]
    if shutil.which("flatpak") and (Path.home() / ".var/app" / FLATPAK_ID).exists():
        return ["flatpak", "run", FLATPAK_ID]
    return None


def not_installed() -> str:
    if MAC:
        return f"Zen isn't installed at {ZEN_APP}. Get it from https://zen-browser.app"
    return "Can't find Zen. Install it from https://zen-browser.app, or set ZENPILOT_ZEN to the command that starts it."


def installed_at() -> str:
    cmd = command()
    return str(ZEN_APP if MAC else shlex.join(cmd)) if cmd else ""


def quit_keys() -> str:
    return "Cmd+Q" if MAC else "Ctrl+Q"


def _profiles() -> dict[str, Path]:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(ZEN_DIR / "profiles.ini")
    found = {}
    for section in ini.sections():
        s = ini[section]
        if section.startswith("Profile") and "Name" in s and "Path" in s:
            found[s["Name"]] = ZEN_DIR / s["Path"] if s.get("IsRelative", "1") == "1" else Path(s["Path"])
    return found


def profile_names() -> list[str]:
    return list(_profiles())


def profile_path(name: str) -> Path | None:
    return _profiles().get(name)


def describe(name: str) -> str:
    """A hint for telling profiles apart, since most are named like 'Default (release)'."""
    path = profile_path(name)
    if not path or not (path / "prefs.js").exists():
        return "never used"
    used = (path / "prefs.js").stat().st_mtime
    notes = ["open now" if holder(path) else time.strftime("last used %b %-d, %Y", time.localtime(used))]
    if account := prefs(path, "prefs.js").get("services.sync.username"):
        notes.append("signed in as " + account.strip('"'))
    return ", ".join(notes)


def resolve(name: str) -> Path:
    """The profile's folder. Only the dedicated profile is created when missing, so a typo can't make one."""
    if path := profile_path(name):
        return path
    if name != DEFAULT_PROFILE:
        names = ", ".join(f"'{n}'" for n in profile_names()) or "none"
        raise ZenError(f"There is no Zen profile named '{name}' (profiles: {names}). Run `zenpilot setup` to pick one.")
    path = create_profile(name)
    ensure_prefs(path, [SAFE_PREF, WELCOME_PREF])
    return path


def create_profile(name: str) -> Path:
    global ZEN_DIR
    cmd = command()
    if not cmd:
        raise ZenError(not_installed())
    subprocess.run([*cmd, "-CreateProfile", name], check=True, capture_output=True, timeout=60)
    path = profile_path(name)
    if not path and not MAC:  # Zen's first run may have picked another data folder than the one guessed
        ZEN_DIR = _data_dir()
        path = profile_path(name)
    if not path:
        raise ZenError(f"`zen -CreateProfile {name}` didn't register a profile in {ZEN_DIR / 'profiles.ini'}.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def prefs(profile: Path, file: str = "user.js") -> dict[str, str]:
    try:
        return dict(_PREF_RE.findall((profile / file).read_text()))
    except FileNotFoundError:
        return {}


def ensure_prefs(profile: Path, wanted: list[tuple[str, str]]) -> list[str]:
    """Append the user.js prefs that are missing or different. Returns the names written."""
    have = prefs(profile)
    missing = [(k, v) for k, v in wanted if have.get(k) != v]
    if missing:
        with (profile / "user.js").open("a") as f:
            f.write("\n// zenpilot\n" + "".join(f'user_pref("{k}", {v});\n' for k, v in missing))
    return [k for k, _ in missing]


def is_safe(profile: Path) -> bool:
    return prefs(profile).get(SAFE_PREF[0]) == SAFE_PREF[1]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def holder(profile: Path) -> int | None:
    """Pid of the Zen that has the profile open, if any."""
    if not MAC:
        # Firefox leaves a symlink "lock" -> "127.0.0.1:+<pid>" in the profile while it runs.
        try:
            target = os.readlink(profile / "lock")
        except OSError:
            return None
        found = re.search(r":\+(\d+)$", target)
        return int(found[1]) if found and Path(f"/proc/{found[1]}").exists() else None
    lock = profile / ".parentlock"
    if not lock.exists():
        return None
    out = subprocess.run(["lsof", "-t", str(lock)], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def quit_zen(pid: int, profile: Path, timeout: float = 30) -> None:
    """Quit one Zen instance the way Cmd+Q does, so it saves its session."""
    if MAC:
        js = f"ObjC.import('AppKit'); $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}).terminate"
        subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, check=True)
    else:
        try:  # Firefox shuts down cleanly on SIGTERM. Check it's Zen first: a pid in a Flatpak sandbox isn't ours.
            argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
            if not re.search(r"(zen|firefox)(?![a-z])", os.path.basename(argv0).lower()):
                raise ZenError(f"Zen is open on this profile, but zenpilot can't stop it. Quit it ({quit_keys()}).")
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # already gone
    deadline = time.monotonic() + timeout
    while holder(profile):
        if time.monotonic() > deadline:
            raise ZenError("Zen didn't quit. It may be asking you to confirm closing its windows.")
        time.sleep(0.25)


def _start(cmd: list[str], name: str, port: int) -> None:
    args = ["-P", name, "--remote-debugging-port", str(port)]
    if MAC:
        subprocess.run(["open", "-na", str(ZEN_APP), "--args", *args], check=True)
    else:  # detached, so Zen outlives the MCP server
        subprocess.Popen(
            [*cmd, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def launch(
    name: str,
    port: int,
    confirm_relaunch: Callable[[str], bool] | None = None,
    timeout: float = 20,
    prefs: list[tuple[str, str]] | None = None,
) -> bool:
    """Start Zen on profile `name` with remote debugging.

    If the profile is already open without it, Zen is quit and reopened when
    confirm_relaunch(question) agrees. Returns False if something already listens on the port.
    """
    if port_open(port):
        return False
    path = resolve(name)
    ensure_prefs(path, [SAFE_PREF, *(prefs or [])])  # before Zen reads user.js, or remote debugging weakens it
    if pid := holder(path):
        question = (
            f"Zen is open on profile \u201c{name}\u201d without remote debugging, so Claude can't use it. "
            "Quit and reopen Zen with remote debugging? "
            + ("Your tabs will be restored." if MAC else "Tabs you opened in the last few seconds may not come back.")
        )
        if not (confirm_relaunch and confirm_relaunch(question)):
            raise ZenError(
                f"Zen profile '{name}' is open without remote debugging, probably because Zen was restarted "
                "outside zenpilot (after an update, or from the Dock), and the user didn't agree to reopen it. "
                f"Ask them to quit Zen ({quit_keys()}) and retry, or to run `zenpilot launch`."
            )
        quit_zen(pid, path)
    cmd = command()
    if not cmd:
        raise ZenError(not_installed())
    _start(cmd, name, port)
    deadline = time.monotonic() + timeout
    while not port_open(port):
        if time.monotonic() > deadline:
            raise ZenError(
                f"Started Zen on profile '{name}', but nothing is listening on port {port} after {timeout:g}s. "
                "Check that Zen started, or run `zenpilot doctor`."
            )
        time.sleep(0.25)
    return True


def version() -> str | None:
    if not MAC:
        cmd = command()
        try:
            out = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=10).stdout if cmd else ""
        except (OSError, subprocess.SubprocessError):
            return None
        return out.split()[-1] if out.split() else None  # "Mozilla Zen 1.23.4b"
    try:
        with (ZEN_APP / "Contents/Info.plist").open("rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None
