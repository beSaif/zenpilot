# Releasing

1. Set the new version in `pyproject.toml`, `mcpb/pyproject.toml`, `mcpb/manifest.json` and both fields in `server.json`, then run `uv lock`.
2. Leave the `zenpilot>=0.4.1` floor in `mcpb/pyproject.toml` alone. An exact pin breaks installs while uv's 10-minute cached index lags a release.
3. Run `uv run pytest -m "not integration"`. `tests/test_mcpb.py` fails if the versions disagree.
4. Commit, then `git push origin main` and confirm it landed.
5. Push the tag on its own: `git push origin vX.Y.Z`.

Keep steps 4 and 5 separate. A combined `git push origin main vX.Y.Z` can reject `main` and still push the tag, and the tag alone triggers the release.

`release.yml` checks the tag matches the version, then publishes to PyPI, the GitHub release (with the `.mcpb`) and the MCP Registry over OIDC. If the registry step fails, run `mcp-publisher login github` and then `mcp-publisher publish` straight away: that login expires within minutes.

Installs update themselves when Claude starts them, and uv's cache can hold back a new version for a few minutes after a release.
