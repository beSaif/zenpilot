"""The Claude Desktop bundle in mcpb/ and the registry's server.json must match the package."""

import json
import tomllib
from pathlib import Path

from zenpilot.server import mcp

ROOT = Path(__file__).parent.parent


async def test_bundle_matches_package():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((ROOT / "mcpb/manifest.json").read_text())
    bundle = tomllib.loads((ROOT / "mcpb/pyproject.toml").read_text())["project"]
    assert manifest["version"] == bundle["version"] == version
    # A floor, not a pin: the bundle's server.py updates zenpilot on each start.
    assert bundle["dependencies"] == [f"zenpilot>={version}"]
    server = json.loads((ROOT / "server.json").read_text())
    assert server["version"] == server["packages"][0]["version"] == version
    assert [t["name"] for t in manifest["tools"]] == [t.name for t in await mcp.list_tools()]
