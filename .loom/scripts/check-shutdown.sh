#!/bin/bash
# check-shutdown.sh - Check for shepherd shutdown signals
#
# Usage:
#   ./.loom/scripts/check-shutdown.sh           # Check global shutdown signal
#   ./.loom/scripts/check-shutdown.sh <issue>   # Also check issue-specific abort
#
# Exit codes:
#   0 - Shutdown signal detected (should exit gracefully)
#   1 - No shutdown signal (continue normally)
#
# This script is used by shepherds to check for graceful shutdown signals
# at phase boundaries during orchestration.

set -e

# Navigate to repository root (handle being called from worktree)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$SCRIPT_DIR/../..")"

# Handle worktrees - find the main workspace
if [ -f "$REPO_ROOT/.git" ]; then
    # We're in a worktree, find main workspace
    MAIN_WORKSPACE="$(git -C "$REPO_ROOT" worktree list | head -1 | awk '{print $1}')"
else
    MAIN_WORKSPACE="$REPO_ROOT"
fi

LOOM_DIR="$MAIN_WORKSPACE/.loom"
ISSUE_NUMBER="$1"

# Check for global shutdown signal
if [ -f "$LOOM_DIR/stop-shepherds" ]; then
    echo "SHUTDOWN:global"
    exit 0
fi

# Check for issue-specific abort (if issue number provided)
if [ -n "$ISSUE_NUMBER" ]; then
    LABELS=$(gh issue view "$ISSUE_NUMBER" --json labels --jq '.labels[].name' 2>/dev/null || echo "")
    if echo "$LABELS" | grep -q "loom:abort"; then
        echo "SHUTDOWN:abort:$ISSUE_NUMBER"
        exit 0
    fi
fi

# No shutdown signal
exit 1
