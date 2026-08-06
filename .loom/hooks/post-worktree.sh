#!/bin/bash
# post-worktree.sh -- REPO-OWNED kicad-tools project hook (issue #4558).
#
# NOTE for /repo tooling and auditors: unlike the guard-*.sh scripts next to
# it, this file is NOT vendored/installer-managed. It uses the documented
# Loom extension point (worktree.sh runs `.loom/hooks/post-worktree.sh` after
# creating a worktree, and Loom upgrades never overwrite this path). Do not
# flag or delete it as unmanaged Loom content.
#
# Why: worktree creation performs zero Python env setup, so a fresh
# worktree's .venv can drift from uv.lock (observed: mypy 1.20.2 vs the
# locked 1.19.1), producing spurious mypy-baseline noise that looks like a
# real regression. Syncing the lock-pinned dev environment here makes every
# local tool run match CI's `uv sync --frozen --extra dev` environment.
#
# Args (passed by worktree.sh):
#   $1 - absolute path to the new worktree
#   $2 - branch name (e.g. feature/issue-42)   [unused]
#   $3 - issue number                          [unused]
#
# This hook must never fail worktree creation: it warns and exits 0 on any
# problem (missing uv, failed sync).

set -u

WORKTREE_PATH="${1:-$PWD}"

if ! command -v uv >/dev/null 2>&1; then
    echo "post-worktree hook: 'uv' not found on PATH -- skipping env sync." >&2
    echo "post-worktree hook: run 'uv sync --frozen --extra dev' manually in $WORKTREE_PATH." >&2
    exit 0
fi

echo "post-worktree hook: syncing lock-pinned dev env (uv sync --frozen --extra dev)..."
if (cd "$WORKTREE_PATH" && uv sync --frozen --extra dev); then
    echo "post-worktree hook: worktree env matches uv.lock."
else
    echo "post-worktree hook: WARNING: 'uv sync --frozen --extra dev' failed." >&2
    echo "post-worktree hook: run it manually in $WORKTREE_PATH before trusting mypy/pytest results." >&2
fi

# Standing reminder (CLAUDE.md): uv sync does NOT build the native extension.
echo "post-worktree hook: reminder -- 'uv run kct build-native' is still a separate step" \
    "(the C++ router extension is not built by uv sync)."

exit 0
