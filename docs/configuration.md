# Settings

They live in `~/.config/zenpilot/config.toml`, and changes apply right away.

```toml
allowlist = ["localhost", "127.0.0.1"]   # "example.com" covers its subdomains too
read_only = false                        # true: Claude can look but not click or type
profile = "zenpilot"                     # which Zen profile Claude uses
auto_launch = true                       # open Zen when Claude needs it
prompts = true                           # show the popups described in the README
port = 9222
audit_log = "~/.local/state/zenpilot/audit.jsonl"
report_dir = "~/.local/state/zenpilot/reports"
audit_redact_urls = false                # true: leave ?query and #fragment out of the log
upload_dirs = []                         # folders Claude may upload files from, e.g. ["~/Documents/CV"]
dangerously_allow_all_sites = false      # skips the allowlist. Please don't.
```

Uploads need one Firefox setting (`dom.file.createInChild`) in the profile. zenpilot adds it
once `upload_dirs` isn't empty, and it takes effect the next time Zen starts.
