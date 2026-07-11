#!/usr/bin/env bash
# check-main-clean.sh - Backstop guard: fail if the MAIN worktree is dirty.
#
# Detects the #2802 / #3513 failure mode where a Builder agent's cwd resets
# between tool calls and a repo-relative Write/Edit/Bash file operation lands
# in the MAIN repository working tree instead of the issue worktree under
# .loom/worktrees/issue-N. The builder guidance (builder.md / builder-worktree.md)
# is the primary defense (capture the absolute worktree path once, use absolute
# paths everywhere). This script is the BACKSTOP: run it after the builder phase
# and before opening a PR. If the main worktree has uncommitted changes, the
# builder has contaminated main and the sweep should abort.
#
# It resolves the MAIN worktree from anywhere — including from inside an issue
# worktree — via `git rev-parse --git-common-dir`, so it works whether invoked
# from the repo root or from a worktree.
#
# Usage:
#   ./.loom/scripts/check-main-clean.sh            # check main worktree, exit 3 if dirty
#   ./.loom/scripts/check-main-clean.sh --help     # show usage
#
# Exit codes:
#   0 - Main worktree is clean (no uncommitted changes).
#   2 - Usage error or could not resolve the main worktree (not a git repo).
#   3 - Main worktree is DIRTY: uncommitted changes detected (the contamination
#       this guard exists to catch).
#
# Notes:
#   - "Dirty" means `git status --porcelain` on the MAIN worktree returns any
#     output: staged, unstaged, or untracked files all count. A builder working
#     correctly in its own worktree leaves the main worktree pristine.
#   - Issue worktrees live under <main>/.loom/worktrees/ which is gitignored, so
#     their existence does NOT make the main worktree dirty.

set -euo pipefail

EXIT_OK=0
EXIT_USAGE=2
EXIT_MAIN_DIRTY=3

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
    -h|--help)
        usage
        exit "$EXIT_OK"
        ;;
    "")
        ;;
    *)
        echo "check-main-clean.sh: unknown argument: $1" >&2
        echo "Run with --help for usage." >&2
        exit "$EXIT_USAGE"
        ;;
esac

# Resolve the canonical git common dir, then the main worktree root (its parent).
# This works from the repo root AND from inside any worktree.
common_dir=$(git rev-parse --git-common-dir 2>/dev/null || true)
if [[ -z "$common_dir" ]]; then
    echo "check-main-clean.sh: not inside a git repository" >&2
    exit "$EXIT_USAGE"
fi

# git-common-dir may be relative; resolve to an absolute path.
abs_common=$(cd "$common_dir" 2>/dev/null && pwd) || abs_common="$common_dir"
main_root=$(dirname "$abs_common")

if [[ ! -d "$main_root" ]]; then
    echo "check-main-clean.sh: could not resolve main worktree root from '$common_dir'" >&2
    exit "$EXIT_USAGE"
fi

status=$(git -C "$main_root" status --porcelain 2>/dev/null || true)

if [[ -n "$status" ]]; then
    echo "ERROR: MAIN worktree is dirty (uncommitted changes detected)." >&2
    echo "       Main worktree: $main_root" >&2
    echo "" >&2
    echo "       This usually means a builder wrote to the main repo instead of" >&2
    echo "       its issue worktree (cwd reset between tool calls — see #3513/#2802)." >&2
    echo "       Builders MUST capture the absolute worktree path once and use" >&2
    echo "       absolute paths for every Write/Edit/Bash file operation." >&2
    echo "" >&2
    echo "       Offending changes:" >&2
    while IFS= read -r line; do
        echo "         $line" >&2
    done <<< "$status"
    exit "$EXIT_MAIN_DIRTY"
fi

echo "check-main-clean.sh: main worktree is clean ($main_root)"
exit "$EXIT_OK"
