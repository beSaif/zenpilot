"""Command line: with no command, the MCP server; otherwise setup, launch, doctor and allow."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import zen
from .safety import CONFIG_PATH, load_config, normalize_domain, set_config_value

NAME = "zenpilot"
CLAUDE_CODE_CONFIG = Path("~/.claude.json").expanduser()


def _desktop_config() -> Path:
    if sys.platform != "win32":
        return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
    # The Microsoft Store build may keep its config in its package folder instead.
    roaming = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser() / "Claude"
    packages = Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local")).expanduser() / "Packages"
    folders = [roaming, *packages.glob("Claude_*/LocalCache/Roaming/Claude")]
    folder = next((f for f in folders if (f / "claude_desktop_config.json").exists()), roaming)
    return folder / "claude_desktop_config.json"


DESKTOP_CONFIG = _desktop_config()


def _uv_cache() -> Path:
    if env := os.environ.get("UV_CACHE_DIR"):
        return Path(env).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local"), "uv", "cache").expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME", "~/.cache"), "uv").expanduser()


def server_command() -> list[str]:
    """The command MCP clients should run to start this zenpilot."""
    cache = _uv_cache().resolve()
    if Path(sys.prefix).resolve().is_relative_to(cache):  # `uvx zenpilot`: this environment is temporary
        uvx = shutil.which("uvx")
        if not uvx:
            raise zen.ZenError("can't find uvx on PATH")
        return [uvx, NAME]
    exe = os.path.abspath(sys.argv[0])  # not resolved: uv tool's ~/.local/bin symlink survives upgrades
    if sys.platform == "win32" and not os.path.splitext(exe)[1] and os.path.isfile(exe + ".exe"):
        exe += ".exe"  # started as `zenpilot`; clients spawn the launcher by its full name
    return [exe]


def register_claude_code(cmd: list[str]) -> str:
    claude = shutil.which("claude")
    if not claude:
        return "· Claude Code: `claude` isn't on PATH, skipped"
    subprocess.run([claude, "mcp", "remove", "--scope", "user", NAME], capture_output=True)
    r = subprocess.run([claude, "mcp", "add", "--scope", "user", NAME, "--", *cmd], capture_output=True, text=True)
    if r.returncode:
        return f"✗ Claude Code: `claude mcp add` failed: {(r.stderr or r.stdout).strip()}"
    return "✓ Claude Code: registered (user scope)"


def register_desktop(cmd: list[str], path: Path = DESKTOP_CONFIG) -> str:
    if not path.parent.exists():
        return "· Claude Desktop: not installed, skipped"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    entry = {"command": cmd[0], "args": cmd[1:]}
    servers = data.setdefault("mcpServers", {})
    if servers.get(NAME) == entry:
        return "✓ Claude Desktop: already registered"
    servers[NAME] = entry
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return "✓ Claude Desktop: registered; restart Claude Desktop to load it"


def _registered(path: Path) -> str | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}).get(NAME)
    except (OSError, ValueError):
        return None
    return " ".join([entry["command"], *entry.get("args", [])]) if entry else None


async def _print_tabs(port: int) -> bool:
    from .browser import Browser, BrowserError

    browser = Browser()
    try:
        await browser.connect(port)
        tabs = await browser.tabs()
        print(f"✓ Connected to Zen on port {port}. Tabs (* = active):")
        for t in tabs:
            print(("   *" if t["active"] else "    ") + f"{t['id']}  {t['url']}  {t['title']}")
        return True
    except BrowserError as e:
        print(f"✗ {e}")
        return False
    finally:
        await browser.close()


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _yes(question: str) -> bool:
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def _pick_profile(current: str | None) -> str:
    names = [zen.DEFAULT_PROFILE, *(n for n in zen.profile_names() if n != zen.DEFAULT_PROFILE)]
    print("Which Zen profile should Claude use?")
    for i, n in enumerate(names, 1):
        note = "separate and logged out of everything; recommended" if n == zen.DEFAULT_PROFILE else zen.describe(n)
        print(f"  {i}) {n} ({note})")
    print("Your everyday profile is usually the one open now. With it, Claude can use sites")
    print("you're logged into, after you allow each one.")
    default = names.index(current) + 1 if current in names else 1
    while True:
        answer = input(f"Choose [{default}]: ").strip() or str(default)
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]


def setup(args) -> int:
    cfg = load_config()
    if not zen.installed():
        raise zen.not_installed()
    interactive = _interactive() and not args.profile
    name = args.profile or (_pick_profile(cfg.profile) if interactive else cfg.profile or zen.DEFAULT_PROFILE)
    existed = zen.profile_path(name) is not None
    path = zen.resolve(name)  # creates only the dedicated profile, so a typo can't make a new one
    print(f"✓ Zen profile '{name}'" + ("" if existed else " created"))
    added = zen.ensure_prefs(path, [zen.SAFE_PREF, zen.WELCOME_PREF])
    print(f"✓ Set {', '.join(added)} in its user.js" if added else "✓ Its user.js already has zenpilot's prefs")
    if name != cfg.profile:
        set_config_value("profile", name)
        print(f'✓ Set profile = "{name}" in {CONFIG_PATH}')
    if interactive:
        print(f"\nClaude may use: {', '.join(cfg.allowlist) or '(no sites)'}.")
        print("When it needs another site, a dialog asks you.")
        if sites := input("Sites to allow now, separated by spaces (Enter to skip): ").split():
            allow(argparse.Namespace(domains=sites, remove=False))
        print()
    if not args.no_register:
        cmd = server_command()
        print(register_claude_code(cmd))
        print(register_desktop(cmd))
    if args.no_launch:
        return 0
    if zen.launch(name, cfg.port, _yes if interactive else None):
        print(f"✓ Started Zen on profile '{name}'")
    return 0 if asyncio.run(_print_tabs(cfg.port)) else 1


def launch(args) -> int:
    cfg = load_config()
    name = args.profile or cfg.profile or zen.DEFAULT_PROFILE
    if zen.launch(name, cfg.port, _yes if _interactive() else None):
        print(f"Started Zen on profile '{name}' with remote debugging on port {cfg.port}.")
    else:
        print(f"Something is already listening on port {cfg.port}, probably Zen with remote debugging.")
    return 0


def doctor(args) -> int:
    problems = 0

    def report(ok: bool, good: str, bad: str) -> bool:
        nonlocal problems
        problems += not ok
        print(f"✓ {good}" if ok else f"✗ {bad}")
        return ok

    try:
        cfg = load_config()
    except (OSError, ValueError) as e:
        print(f"✗ Config error in {CONFIG_PATH}: {e}")
        return 1
    modes = [m for m, on in [("read-only", cfg.read_only), ("ALL SITES ALLOWED", cfg.allow_all_sites)] if on]
    print(f"✓ Config {CONFIG_PATH}" + (f" ({', '.join(modes)})" if modes else ""))
    print(f"  allowlist: {', '.join(cfg.allowlist) or '(empty)'}")
    report(zen.installed(), f"Zen installed at {zen.ZEN_APP}", str(zen.not_installed()))
    path = zen.profile_path(cfg.profile) if cfg.profile else None
    if cfg.profile is None:
        print("· No Zen profile chosen yet; you'll be asked on first use, or run `zenpilot setup`")
    elif report(bool(path), f"Zen profile '{cfg.profile}'", f"No Zen profile '{cfg.profile}'. Run `zenpilot setup`."):
        report(
            zen.is_safe(path),
            f"{zen.SAFE_PREF[0]} is off in its user.js",
            f"{zen.SAFE_PREF[0]} isn't set to false in {path / 'user.js'}. Run `zenpilot setup`.",
        )
        seen = {**zen.prefs(path, "prefs.js"), **zen.prefs(path)}.get(zen.WELCOME_PREF[0]) == "true"
        report(seen, "Welcome screen dismissed", "Zen's welcome screen isn't dismissed yet. Run `zenpilot setup`.")
    if zen.port_open(cfg.port):
        problems += not asyncio.run(_print_tabs(cfg.port))
    elif cfg.auto_launch:
        print(f"· Zen isn't listening on port {cfg.port}; zenpilot will start it on the first tool call")
    else:
        report(False, "", f"Zen isn't listening on port {cfg.port}. Run `zenpilot launch`.")
    for client, file in [("Claude Code", CLAUDE_CODE_CONFIG), ("Claude Desktop", DESKTOP_CONFIG)]:
        cmd = _registered(file)
        print(f"✓ {client}: {cmd}" if cmd else f"· {client}: not registered (`zenpilot setup` does it)")
    return 1 if problems else 0


def allow(args) -> int:
    cfg = load_config()
    domains = [normalize_domain(d) for d in args.domains]
    if "" in domains:
        raise ValueError("not a domain: " + args.domains[domains.index("")])
    if args.remove:
        new = [d for d in cfg.allowlist if d not in domains]
    else:
        new = list(dict.fromkeys(cfg.allowlist + domains))
    if new != cfg.allowlist:
        set_config_value("allowlist", new)
    print(f"Allowlist: {', '.join(new) or '(empty)'}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog=NAME, description="With no command, runs the MCP server that Claude starts.")
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)  # old name for doctor
    sub = parser.add_subparsers(dest="command", metavar="command")
    p = sub.add_parser("setup", help="create the Zen profile, register with Claude, start Zen")
    p.add_argument("--profile", help="Zen profile to set up (default: the config's, 'zenpilot')")
    p.add_argument("--no-register", action="store_true", help="don't register with Claude Code or Claude Desktop")
    p.add_argument("--no-launch", action="store_true", help="don't start Zen at the end")
    p = sub.add_parser("launch", help="start Zen with remote debugging")
    p.add_argument("profile", nargs="?", help="Zen profile (default: the config's)")
    sub.add_parser("doctor", help="check each part of the setup")
    p = sub.add_parser("allow", help="add sites to the allowlist; with no sites, show it")
    p.add_argument("domains", nargs="*", metavar="domain")
    p.add_argument("--remove", action="store_true", help="remove the sites instead")
    args = parser.parse_args()

    command = "doctor" if args.check else args.command
    if command is None:
        import anyio

        from .server import serve

        anyio.run(serve)
        return
    for stream in (sys.stdout, sys.stderr):
        # Redirected on Windows, output uses the ANSI code page, which can't encode ✓ and ✗.
        if stream and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(errors="replace")
    try:
        sys.exit({"setup": setup, "launch": launch, "doctor": doctor, "allow": allow}[command](args))
    except (zen.ZenError, ValueError, OSError, subprocess.SubprocessError) as e:
        sys.exit(f"zenpilot: {e}")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nzenpilot: cancelled")
