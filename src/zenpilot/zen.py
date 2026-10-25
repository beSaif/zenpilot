"""Finding, preparing and launching Zen profiles (macOS and Windows)."""

import configparser
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

WINDOWS = sys.platform == "win32"


def _windows_zen() -> Path:
    """zen.exe from the registry (where the installer records it), else the usual install folders."""
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\zen.exe") as key:
                exe = Path(winreg.QueryValue(key, None))
        except OSError:
            continue
        if exe.is_file():
            return exe
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "Zen Browser", "zen.exe"),
        Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local"), "Programs", "Zen Browser", "zen.exe").expanduser(),
        Path("~/scoop/apps/zen-browser/current/zen.exe").expanduser(),
    ]
    return next((c for c in candidates if c.is_file()), candidates[0])


if WINDOWS:
    # ZENPILOT_ZEN points at zen.exe for portable or otherwise unusual installs.
    ZEN_BIN = Path(os.environ["ZENPILOT_ZEN"]).expanduser() if os.environ.get("ZENPILOT_ZEN") else _windows_zen()
    ZEN_APP = ZEN_BIN
    ZEN_DIR = Path(os.environ.get("APPDATA", "~/AppData/Roaming"), "zen").expanduser()
    LOCK_FILE = "parent.lock"  # stays on disk after Zen quits; only an open handle means it's in use
    QUIT_KEYS = "Ctrl+Shift+Q"
else:
    ZEN_APP = Path("/Applications/Zen.app")
    ZEN_BIN = ZEN_APP / "Contents/MacOS/zen"
    ZEN_DIR = Path("~/Library/Application Support/zen").expanduser()
    LOCK_FILE = ".parentlock"
    QUIT_KEYS = "Cmd+Q"
DEFAULT_PROFILE = "zenpilot"  # the dedicated, logged-out profile zenpilot creates

# With remote debugging, Firefox's remote agent rewrites ~40 prefs for test automation
# (Safe Browsing, password saving, popup blocker, ...) and Zen persists them. This stops it.
SAFE_PREF = ("remote.prefs.recommended", "false")
# Until Zen's welcome screen is dismissed, pages have a 0x0 viewport.
WELCOME_PREF = ("zen.welcome-screen.seen", "true")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flashing up on Windows
_PREF_RE = re.compile(r'^\s*user_pref\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;', re.M)


class ZenError(Exception):
    """A setup or launch failure with a message for the user (or the model)."""


def installed() -> bool:
    return ZEN_BIN.is_file()


def not_installed() -> ZenError:
    return ZenError(f"Zen isn't installed at {ZEN_APP}. Get it from https://zen-browser.app")


def _profiles() -> dict[str, Path]:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(ZEN_DIR / "profiles.ini", encoding="utf-8")
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
    used = time.localtime((path / "prefs.js").stat().st_mtime)
    notes = ["open now" if holder(path) else f"last used {time.strftime('%b', used)} {used.tm_mday}, {used.tm_year}"]
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
    if not installed():
        raise not_installed()
    subprocess.run([str(ZEN_BIN), "-CreateProfile", name], check=True, capture_output=True, timeout=60)
    path = profile_path(name)
    if not path:
        raise ZenError(f"`zen -CreateProfile {name}` didn't register a profile in {ZEN_DIR / 'profiles.ini'}.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def prefs(profile: Path, file: str = "user.js") -> dict[str, str]:
    try:
        return dict(_PREF_RE.findall((profile / file).read_text(encoding="utf-8", errors="replace")))
    except FileNotFoundError:
        return {}


def ensure_prefs(profile: Path, wanted: list[tuple[str, str]]) -> list[str]:
    """Append the user.js prefs that are missing or different. Returns the names written."""
    have = prefs(profile)
    missing = [(k, v) for k, v in wanted if have.get(k) != v]
    if missing:
        with (profile / "user.js").open("a", encoding="utf-8") as f:
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
    lock = profile / LOCK_FILE
    if not lock.exists():
        return None
    if WINDOWS:
        from .winproc import file_holders

        pids = file_holders(lock)
    else:
        pids = subprocess.run(["lsof", "-t", str(lock)], capture_output=True, text=True).stdout.split()
    return int(pids[0]) if pids else None


def quit_zen(pid: int, profile: Path, timeout: float = 30) -> None:
    """Quit one Zen instance gracefully (like Cmd+Q, or closing its windows on Windows), so it saves its session."""
    if WINDOWS:
        # Without /F, taskkill asks the windows to close, as clicking their X would.
        r = subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, creationflags=_NO_WINDOW)
        if r.returncode:
            raise ZenError(f"Couldn't ask Zen to quit. Quit it ({QUIT_KEYS}) and retry.")
    else:
        js = f"ObjC.import('AppKit'); $.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}).terminate"
        subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, check=True)
    deadline = time.monotonic() + timeout
    while holder(profile):
        if time.monotonic() > deadline:
            raise ZenError("Zen didn't quit. It may be asking you to confirm closing its windows.")
        time.sleep(0.25)


def _spawn(name: str, port: int) -> None:
    """Start a new Zen process on the profile, detached from zenpilot."""
    args = ["-P", name, "--remote-debugging-port", str(port)]
    if not WINDOWS:
        subprocess.run(["open", "-na", str(ZEN_APP), "--args", *args], check=True)
        return
    # Detached, without zenpilot's stdio (the MCP pipe), and outside the MCP client's job
    # if allowed, so Zen keeps running when Claude exits.
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            subprocess.Popen(
                [str(ZEN_BIN), "--new-instance", *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags | extra,
                close_fds=True,
            )
            return
        except PermissionError:  # the job doesn't allow breaking away
            continue
    raise ZenError(f"Couldn't start {ZEN_BIN}.")


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
                f"Zen profile '{name}' is open without remote debugging. Quit it ({QUIT_KEYS}) and retry, "
                "or run `zenpilot launch` to reopen it."
            )
        quit_zen(pid, path)
    if not installed():
        raise not_installed()
    _spawn(name, port)
    deadline = time.monotonic() + timeout
    while not port_open(port):
        if time.monotonic() > deadline:
            raise ZenError(
                f"Started Zen on profile '{name}', but nothing is listening on port {port} after {timeout:g}s. "
                "Check that Zen started, or run `zenpilot doctor`."
            )
        time.sleep(0.25)
    return True
