"""The Claude Desktop bundle in mcpb/ must match the package it installs."""

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
    assert bundle["dependencies"] == [f"zenpilot=={version}"]
    assert [t["name"] for t in manifest["tools"]] == [t.name for t in await mcp.list_tools()]
