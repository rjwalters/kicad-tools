#!/bin/bash
# run-tests.sh - Run a Python test suite with a worktree-correct PYTHONPATH.
#
# THE PROBLEM this solves (generic to any Python repo Loom is installed into):
# when a package is installed in editable mode (`pip install -e .`) from the main
# checkout, the resulting `.pth` entry points at the MAIN checkout's source tree.
# Run `python3 -m pytest` from inside an issue worktree
# (`.loom/worktrees/issue-N`) and Python then imports the *main branch's* code,
# not the code the PR under review actually changed — producing test results that
# describe the wrong tree (first observed in PR #2818's review).
#
# This wrapper detects the worktree it is running inside and PREPENDS that
# worktree's source root(s) to PYTHONPATH so imports resolve to the worktree's
# version, then execs pytest.
#
# NOTE for the Loom repo itself: Loom ships no Python package at all — epic
# #4081 Phase 4 (#4557) retired `loom-tools`'s core, and #4970 finished the
# job by retiring its last residue, the opt-in `loom-search` carve-out (per
# the operator's RETIRE decision on #4608). The orchestration layer is the
# native `loom-daemon` binary plus bash scripts (see
# docs/adr/0013-loom-tools-python-retirement.md). This script now exists
# purely for OTHER repos under Loom orchestration that are genuinely
# Python-first — nothing in the Loom repo itself uses it.
#
# Usage:
#   ./.loom/scripts/run-tests.sh [pytest args...]
#   ./.loom/scripts/run-tests.sh --worktree-path /path/to/worktree [pytest args...]
#
# Examples:
#   ./.loom/scripts/run-tests.sh -x -q
#   ./.loom/scripts/run-tests.sh --testmon -x -q

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

# Prepend the worktree's source root to PYTHONPATH, if present.
#
# Probed layout: <worktree>/src — the conventional src-layout package root.
if [[ -n "$WORKTREE_PATH" ]]; then
    candidate="$WORKTREE_PATH/src"
    if [[ -d "$candidate" ]]; then
        echo "[run-tests] Prepending PYTHONPATH=$candidate (worktree source override)" >&2
        export PYTHONPATH="${candidate}${PYTHONPATH:+:${PYTHONPATH}}"
    fi
fi

exec python3 -m pytest "${PYTEST_ARGS[@]}"
