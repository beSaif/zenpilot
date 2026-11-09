---
name: pr-review
description: Review zenpilot pull requests. Use when triaging open PRs, deciding whether to merge one, pushing fixes to a contributor's branch, or merging.
---

Run from the repo root. `pr.sh` is `.claude/skills/pr-review/pr.sh`.

## Triage all open PRs

1. Run `pr.sh triage`.
2. Run `pr.sh show N` for each PR, and read code only where the summary leaves the call unclear.

Done when every open PR has a recommendation: merge, fix it ourselves, ask the contributor, or close.

## Review one PR

1. Run `pr.sh show N`, then `pr.sh checkout N <scratchpad>/prN`. Keep the head sha and push command it prints.
2. Read `git -C <worktree> diff origin/main...HEAD -- src tests`, then check it against the rules below.
3. Report a verdict that says exactly what ran: CI, unit tests, live Zen, or nothing.

Done when every changed file in `src/` has been read and every rule applied.

### Rules

- **Declined sites stay declined**: content from a site the user declined can't surface later, including after the tab navigates to an allowed one.
- **Typed text stays private**: tools taking text or keys list them in `@tool(redact=...)`, and errors leave the text out, because errors reach the audit log and session report.
- **The clipboard stays out of the page**: input tools refuse paste shortcuts.
- **Page-controlled output is capped**: per entry and per tool result.
- **A failed dialog is no answer**: a dialog that crashes or can't start returns `None`, and only a real click counts as "Don't Allow".
- **Omitted arguments are `None`** by the time they reach the `tool` wrapper in `server.py`, including its `finally` block.

## Running the PR's code

Installing or testing a fork executes the contributor's code on this machine, so get the user's go-ahead first, or hand them the commands. Reading the worktree needs no approval.

Changes to launch, dialog or BiDi code need `scripts/live-test.sh <worktree>` before you recommend merging, because unit tests fake those paths.

## Push fixes, when the user asks

1. Confirm `gh pr view N --json state,headRefOid` is still open and still at the sha you checked out. If it has merged, branch from `origin/main` and open a new PR.
2. Commit in the worktree and run the push command `checkout` printed. Bring in `origin/main` first if the PR is behind or conflicting.
3. Comment on the PR with what changed and what was tested.

Fork CI can sit at `action_required` after a push, which shows as no checks and `UNSTABLE`. Confirm with `gh run list --commit <sha>`, then ask the user to approve the run on GitHub.

## Merge, when the user asks

Use `gh pr merge N --merge --match-head-commit <full sha>`, or `--squash` if the user asks for it. A squash body credits the contributor with a `Co-authored-by:` line, using the name and email from `gh api repos/{owner}/{repo}/pulls/N/commits`.

Then run `git worktree remove <worktree>`.
