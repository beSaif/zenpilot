#!/bin/sh
# Compact views of pull requests, and a checkout that doesn't run the PR's code.
# Usage: pr.sh triage | show N | checkout N DIR
set -eu

case "${1:-}" in
triage)
    git fetch -q origin main
    gh pr list --state open --limit 100 \
        --json number,title,author,isDraft,mergeStateStatus,additions,deletions,changedFiles,updatedAt \
        --jq '.[] | "\(.number)\t#\(.number) \(.author.login) +\(.additions)/-\(.deletions) in \(.changedFiles) files, \(.mergeStateStatus)\(if .isDraft then ", draft" else "" end), updated \(.updatedAt[:10]): \(.title)"' |
        while IFS="$(printf '\t')" read -r n line; do
            echo "$line"
            git fetch -q origin "pull/$n/head"
            out=$(git merge-tree --write-tree --name-only origin/main FETCH_HEAD) && continue
            echo "$out" | sed -n '2,/^$/p' | sed '/^$/d; s/^/    conflict: /'
        done
    ;;
show)
    gh pr view "$2" --json number,title,author,body,headRefOid,mergeStateStatus,maintainerCanModify,statusCheckRollup,commits,files,comments,reviews --jq '
        def cut($n): if length > $n then .[:$n] + "… (cut, see gh pr view)" else . end;
        "#\(.number) \(.title)\nby \(.author.login), head \(.headRefOid[:7]), \(.mergeStateStatus), maintainer can push: \(.maintainerCanModify)",
        "checks: \([.statusCheckRollup[] | "\(.name)=\(.conclusion // .status)"] | join(", "))",
        "\n\(.body | cut(1500))\n",
        (.files[] | "file \(.path) +\(.additions) -\(.deletions)"),
        (.commits[] | "commit \(.oid[:7]) \(.committedDate[:10]) \(.messageHeadline)"),
        (.comments[] | "comment \(.author.login) \(.createdAt[:10]): \(.body | cut(400))"),
        (.reviews[] | select(.body != "") | "review \(.author.login) \(.state): \(.body | cut(400))")'
    gh api "repos/{owner}/{repo}/pulls/$2/comments" \
        --jq '.[] | "inline \(.user.login) \(.path):\(.line): \(.body[:300])"'
    ;;
checkout)
    git fetch -q origin main
    git fetch -q origin "pull/$2/head"
    head=$(git rev-parse FETCH_HEAD)
    git worktree add -q --detach "$3" "$head"
    echo "worktree: $3"
    echo "head: $head"
    echo "behind main: $(git rev-list --count "$head..origin/main"), ahead: $(git rev-list --count "origin/main..$head")"
    gh api "repos/{owner}/{repo}/pulls/$2" \
        --jq '"push fixes: git push \(.head.repo.clone_url) HEAD:\(.head.ref) (maintainer can push: \(.maintainer_can_modify))"'
    ;;
*)
    echo "Usage: pr.sh triage | show N | checkout N DIR" >&2
    exit 2
    ;;
esac
