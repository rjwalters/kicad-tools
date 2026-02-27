#!/bin/bash
# run-tests.sh - Run Python tests with correct PYTHONPATH for worktree context
#
# In Loom, loom-tools is installed as an editable package from the main repo root.
# When running tests inside an issue worktree (.loom/worktrees/issue-N), the
# editable install resolves to the main branch's source, not the worktree's code.
# This causes false test failures when PRs modify loom-tools.
#
# This script detects the worktree context and sets PYTHONPATH to the worktree's
# loom-tools/src so that imports resolve to the worktree's version.
#
# Usage:
#   ./.loom/scripts/run-tests.sh [pytest args...]
#   ./.loom/scripts/run-tests.sh --worktree-path /path/to/worktree [pytest args...]
#
# Examples:
#   ./.loom/scripts/run-tests.sh -x -q
#   ./.loom/scripts/run-tests.sh --testmon -x -q
#   ./.loom/scripts/run-tests.sh loom-tools/tests/

set -euo pipefail

# Parse args — extract --worktree-path if provided, pass rest to pytest
WORKTREE_PATH=""
PYTEST_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --worktree-path)
            WORKTREE_PATH="$2"
            shift 2
            ;;
        *)
            PYTEST_ARGS+=("$1")
            shift
            ;;
    esac
done

# Auto-detect worktree path from current directory if not provided
if [[ -z "$WORKTREE_PATH" ]]; then
    CWD="$(pwd)"
    # Check if current directory is inside a .loom/worktrees/issue-N path
    if [[ "$CWD" =~ ^(.*/.loom/worktrees/issue-[0-9]+)(/.*)?$ ]]; then
        WORKTREE_PATH="${BASH_REMATCH[1]}"
    fi
fi

# Set PYTHONPATH if the worktree has a local loom-tools/src
if [[ -n "$WORKTREE_PATH" ]] && [[ -d "$WORKTREE_PATH/loom-tools/src" ]]; then
    LOOM_TOOLS_SRC="$WORKTREE_PATH/loom-tools/src"
    echo "[run-tests] Setting PYTHONPATH=$LOOM_TOOLS_SRC (worktree override for loom-tools)" >&2
    export PYTHONPATH="${LOOM_TOOLS_SRC}:${PYTHONPATH:-}"
fi

exec python3 -m pytest "${PYTEST_ARGS[@]}"
