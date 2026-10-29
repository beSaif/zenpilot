"""Command line: with no command, the MCP server; otherwise setup, launch, doctor, allow and log."""

import argparse
import asyncio
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import report as reports
from . import zen
from .safety import CONFIG_PATH, grants, load_config, normalize_domain, revoke, set_config_value

NAME = "zenpilot"
DESKTOP_CONFIG = Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()
CLAUDE_CODE_CONFIG = Path("~/.claude.json").expanduser()


def server_command() -> list[str]:
    """The command MCP clients should run to start this zenpilot."""
    cache = Path(os.environ.get("UV_CACHE_DIR", "~/.cache/uv")).expanduser().resolve()
    if Path(sys.prefix).resolve().is_relative_to(cache):  # `uvx zenpilot`: this environment is temporary
        uvx = shutil.which("uvx")
        if not uvx:
            raise zen.ZenError("can't find uvx on PATH")
        return [uvx, NAME]
    return [os.path.abspath(sys.argv[0])]  # not resolved: uv tool's ~/.local/bin symlink survives upgrades


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
    data = json.loads(path.read_text()) if path.exists() else {}
    entry = {"command": cmd[0], "args": cmd[1:]}
    servers = data.setdefault("mcpServers", {})
    if servers.get(NAME) == entry:
        return "✓ Claude Desktop: already registered"
    servers[NAME] = entry
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
    return "✓ Claude Desktop: registered; restart Claude Desktop to load it"


def _registered(path: Path) -> str | None:
    try:
        entry = json.loads(path.read_text()).get("mcpServers", {}).get(NAME)
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
    names = [
        zen.DEFAULT_PROFILE,
        zen.ZEN_DEFAULT,
        *(n for n in zen.profile_names() if n not in (zen.DEFAULT_PROFILE, zen.ZEN_DEFAULT)),
    ]
    print("Which Zen profile should Claude use?")
    for i, n in enumerate(names, 1):
        if n == zen.ZEN_DEFAULT:
            note = "Zen's default profile, with your logins; zenpilot never writes to it"
        else:
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
    if not zen.ZEN_BIN.exists():
        raise zen.ZenError(f"Zen isn't installed at {zen.ZEN_APP}. Get it from https://zen-browser.app")
    interactive = _interactive() and not args.profile
    name = args.profile or (_pick_profile(cfg.profile) if interactive else cfg.profile or zen.DEFAULT_PROFILE)
    existed = zen.profile_path(name) is not None
    path = zen.resolve(name)  # creates only the dedicated profile, so a typo can't make a new one
    if name == zen.ZEN_DEFAULT:
        print(f"✓ Zen's default profile ({path.name}); zenpilot leaves its user.js as it is")
    else:
        print(f"✓ Zen profile '{name}'" + ("" if existed else " created"))
        added = zen.ensure_prefs(path, [zen.SAFE_PREF, zen.WELCOME_PREF, *_prefs(cfg)])
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
    if zen.launch(name, cfg.port, _yes if interactive else None, prefs=_prefs(cfg)):
        print(f"✓ Started Zen on profile '{name}'")
    return 0 if asyncio.run(_print_tabs(cfg.port)) else 1


def _prefs(cfg) -> list[tuple[str, str]]:
    return [zen.UPLOAD_PREF] if cfg.upload_dirs else []


def launch(args) -> int:
    cfg = load_config()
    name = args.profile or cfg.profile or zen.DEFAULT_PROFILE
    if zen.launch(name, cfg.port, _yes if _interactive() else None, prefs=_prefs(cfg)):
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
    if granted := _grants():
        print(f"  for now: {granted}")
    report(zen.ZEN_BIN.exists(), f"Zen installed at {zen.ZEN_APP}", f"Zen isn't installed at {zen.ZEN_APP}")
    path = None
    if cfg.profile == zen.ZEN_DEFAULT:
        path = zen.install_default_path()
        report(
            bool(path),
            f"Zen's default profile ({path.name if path else ''})",
            "Zen has no default profile in profiles.ini. Run `zenpilot setup`.",
        )
        if cfg.upload_dirs:
            print(
                f"· Uploads need {zen.UPLOAD_PREF[0]} in its user.js, which zenpilot won't write to the default profile"
            )
    elif cfg.profile is None:
        print("· No Zen profile chosen yet; you'll be asked on first use, or run `zenpilot setup`")
    elif report(
        bool(path := zen.profile_path(cfg.profile)),
        f"Zen profile '{cfg.profile}'",
        f"No Zen profile '{cfg.profile}'. Run `zenpilot setup`.",
    ):
        report(
            zen.is_safe(path),
            f"{zen.SAFE_PREF[0]} is off in its user.js",
            f"{zen.SAFE_PREF[0]} isn't set to false in {path / 'user.js'}. Run `zenpilot setup`.",
        )
        seen = {**zen.prefs(path, "prefs.js"), **zen.prefs(path)}.get(zen.WELCOME_PREF[0]) == "true"
        report(seen, "Welcome screen dismissed", "Zen's welcome screen isn't dismissed yet. Run `zenpilot setup`.")
        if cfg.upload_dirs:
            report(
                zen.prefs(path).get(zen.UPLOAD_PREF[0]) == "true",
                f"{zen.UPLOAD_PREF[0]} is on, so uploads work",
                f"Uploads need {zen.UPLOAD_PREF[0]} in its user.js. Run `zenpilot setup`, then restart Zen.",
            )
    if zen.port_open(cfg.port):
        problems += not asyncio.run(_print_tabs(cfg.port))
    elif path and zen.holder(path):
        report(False, "", f"Zen is open on '{cfg.profile}' without remote debugging. `zenpilot launch` reopens it.")
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
        if set(domains) & set(grants()):
            revoke(domains)
    else:
        new = list(dict.fromkeys(cfg.allowlist + domains))
    if new != cfg.allowlist:
        set_config_value("allowlist", new)
    print(f"Allowlist: {', '.join(new) or '(empty)'}")
    if granted := _grants():
        print(f"For now: {granted}")
    return 0


def _grants() -> str:
    """Sites allowed for a while from the dialog, with when that ends."""
    today = datetime.now().date()

    def until(ts: float) -> str:
        end = datetime.fromtimestamp(ts)
        return end.strftime("%H:%M" if end.date() == today else "%a %H:%M")

    return ", ".join(f"{d} (until {until(ts)})" for d, ts in sorted(grants().items()))


def report(args) -> int:
    saved = reports.reports(load_config().report_dir)
    if args.list:
        for path in saved:
            print(path)
        return 0
    path = Path(args.file).expanduser() if args.file else next(iter(saved), None)
    if path is None:
        print("No session reports yet. zenpilot writes one when Claude runs into trouble.")
        return 0
    text = path.read_text()
    if not args.issue:
        print(f"{path}\n\n{text}", end="")
        return 0
    url, fits = reports.issue_url(text)
    if not fits:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        print("The report is too long for a link, so it's on your clipboard: paste it into the issue.")
    print(f"Opening a new GitHub issue for {path}. Check it for anything private before you submit.")
    subprocess.run(["open", url], check=True)
    return 0


def _target(r: dict) -> str:
    """What a logged call acted on, in a few words."""
    p = r.get("params", {})
    if "ref" not in p:
        return p.get("url") or p.get("tab_id") or ""
    target = f"{p['ref']} {p.get('element', '')}".strip()
    return target + (": " + ", ".join(Path(f).name for f in p["paths"]) if p.get("paths") else "")


def log(args) -> int:
    path = load_config().audit_log
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        print(f"No audit log at {path} yet.")
        return 0
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    if args.errors:
        records = [r for r in records if not r.get("ok")]
    for r in records[-args.n :] if args.n else records:
        when = datetime.fromisoformat(r["ts"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        target = _target(r)
        target = target if len(target) <= 60 else target[:59] + "…"
        status = "ok" if r.get("ok") else "✗ " + r.get("error", "").split(". ")[0]
        print(f"{when}  {r['tool']:<10} {target:<60}  {r.get('domain') or '':<24} {status}")
    return 0


def _update() -> None:
    """Upgrade a `uv tool install` of zenpilot before serving, and restart into the new version.

    Offline or slow, the installed version runs. A clone, the Desktop extension (which updates
    itself) or a version pinned at install time is left alone.
    """
    data = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
    tools = Path(os.environ.get("UV_TOOL_DIR") or data / "uv/tools").expanduser().resolve()
    uv = shutil.which("uv")
    if os.environ.get("ZENPILOT_UPDATED") or not uv or Path(sys.prefix).resolve() != tools / NAME:
        return
    before = importlib.metadata.version(NAME)
    env = {**os.environ, "UV_HTTP_TIMEOUT": "8", "UV_HTTP_RETRIES": "0"}
    try:  # its output would corrupt the MCP channel on stdout
        subprocess.run([uv, "tool", "upgrade", NAME], capture_output=True, timeout=20, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return
    if importlib.metadata.version(NAME) != before:
        os.environ["ZENPILOT_UPDATED"] = "1"  # once per start
        os.execv(sys.argv[0], sys.argv)


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
    p.add_argument("--remove", action="store_true", help="take the sites off, including ones allowed for a while")
    p = sub.add_parser("report", help="show the latest session report of what Claude couldn't do")
    p.add_argument("file", nargs="?", help="a report to use instead of the latest")
    p.add_argument("--issue", action="store_true", help="open a GitHub issue prefilled with it")
    p.add_argument("--list", action="store_true", help="list all reports, newest first")
    p = sub.add_parser("log", help="print the audit log as a timeline")
    p.add_argument("-n", type=int, default=50, help="show the last N calls (default 50, 0 for all)")
    p.add_argument("--errors", action="store_true", help="only failed calls")
    args = parser.parse_args()

    command = "doctor" if args.check else args.command
    if command is None and sys.stdin.isatty():  # a person typed `zenpilot`; the server would just wait
        parser.print_help()
        print("\nClaude starts zenpilot itself. To set it up, run `zenpilot setup`.")
        return
    if command is None:
        _update()
        import anyio

        from .server import serve

        anyio.run(serve)
        return
    try:
        commands = {"setup": setup, "launch": launch, "doctor": doctor, "allow": allow, "report": report, "log": log}
        sys.exit(commands[command](args))
    except (zen.ZenError, ValueError, OSError, subprocess.SubprocessError) as e:
        sys.exit(f"zenpilot: {e}")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nzenpilot: cancelled")
