"""Finding, preparing and launching Zen profiles (macOS)."""

import configparser
import re
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

ZEN_APP = Path("/Applications/Zen.app")
ZEN_BIN = ZEN_APP / "Contents/MacOS/zen"
ZEN_DIR = Path("~/Library/Application Support/zen").expanduser()
DEFAULT_PROFILE = "zenpilot"  # the dedicated, logged-out profile zenpilot creates

# With remote debugging, Firefox's remote agent rewrites ~40 prefs for test automation
# (Safe Browsing, password saving, popup blocker, ...) and Zen persists them. This stops it.
SAFE_PREF = ("remote.prefs.recommended", "false")
# Until Zen's welcome screen is dismissed, pages have a 0x0 viewport.
WELCOME_PREF = ("zen.welcome-screen.seen", "true")

_PREF_RE = re.compile(r'^\s*user_pref\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;', re.M)


class ZenError(Exception):
    """A setup or launch failure with a message for the user (or the model)."""


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
    if not ZEN_BIN.exists():
        raise ZenError(f"Zen isn't installed at {ZEN_APP}. Get it from https://zen-browser.app")
    subprocess.run([str(ZEN_BIN), "-CreateProfile", name], check=True, capture_output=True, timeout=60)
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
    lock = profile / ".parentlock"
    if not lock.exists():
        return None
    out = subprocess.run(["lsof", "-t", str(lock)], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def quit_zen(pid: int, profile: Path, timeout: float = 30) -> None:
    """Quit one Zen instance the way Cmd+Q does, so it saves its session."""
    js = f"ObjC.import('AppKit'); $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}).terminate"
    subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, check=True)
    deadline = time.monotonic() + timeout
    while holder(profile):
        if time.monotonic() > deadline:
            raise ZenError("Zen didn't quit. It may be asking you to confirm closing its windows.")
        time.sleep(0.25)


def launch(name: str, port: int, confirm_relaunch: Callable[[str], bool] | None = None, timeout: float = 20) -> bool:
    """Start Zen on profile `name` with remote debugging.

    If the profile is already open without it, Zen is quit and reopened when
    confirm_relaunch(question) agrees. Returns False if something already listens on the port.
    """
    if port_open(port):
        return False
    path = resolve(name)
    ensure_prefs(path, [SAFE_PREF])  # before Zen reads user.js, or remote debugging weakens the profile
    if pid := holder(path):
        question = (
            f"Zen is open on profile \u201c{name}\u201d without remote debugging, so Claude can't use it. "
            "Quit and reopen Zen with remote debugging? Your tabs will be restored."
        )
        if not (confirm_relaunch and confirm_relaunch(question)):
            raise ZenError(
                f"Zen profile '{name}' is open without remote debugging. Quit it (Cmd+Q) and retry, "
                "or run `zenpilot launch` to reopen it."
            )
        quit_zen(pid, path)
    if not ZEN_APP.exists():
        raise ZenError(f"Zen isn't installed at {ZEN_APP}.")
    subprocess.run(
        ["open", "-na", str(ZEN_APP), "--args", "-P", name, "--remote-debugging-port", str(port)], check=True
    )
    deadline = time.monotonic() + timeout
    while not port_open(port):
        if time.monotonic() > deadline:
            raise ZenError(
                f"Started Zen on profile '{name}', but nothing is listening on port {port} after {timeout:g}s. "
                "Check that Zen started, or run `zenpilot doctor`."
            )
        time.sleep(0.25)
    return True
