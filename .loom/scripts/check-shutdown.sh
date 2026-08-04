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
# OPERATOR-MANUAL TOOL (not invoked by any current in-repo caller, #5277).
# This script originally backed the Bash "shepherd" orchestration loop, which
# was removed in v0.10.0 (docs/migration/v0.10.0-shepherd-deprecation.md) --
# the Rust `loom-daemon` checks `.loom/stop-shepherds` directly
# (`agent_session::spawn`) instead of shelling out here. It is kept, not
# deleted, because CHANGELOG.md's v0.10.0 daemon-port entry explicitly
# preserves its "name, flags, and graceful `gh`-fallback contract" as a
# supported operator surface: run it by hand, or from a custom orchestration
# script, to check the two live shutdown signals it still reads correctly --
# the global `.loom/stop-shepherds` file and the per-issue `loom:abort`
# label -- both of which remain part of the current label/signal taxonomy.

set -e

# Forge-agnostic issue/PR operations via the native `loom-daemon forge`
# subcommand (port of the retired `loom-forge`). GitHub: passthrough to `gh`.
# Gitea: declines (exit 3), degrading to the `gh` fallback. Fall back to `gh`
# when loom-daemon is absent so a bare workspace still works.
if command -v loom-daemon &>/dev/null; then
    FORGE="loom-daemon forge"
else
    FORGE="gh"
fi

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
    LABELS=$($FORGE issue view "$ISSUE_NUMBER" --json labels --jq '.labels[].name' 2>/dev/null || echo "")
    if echo "$LABELS" | grep -q "loom:abort"; then
        echo "SHUTDOWN:abort:$ISSUE_NUMBER"
        exit 0
    fi
fi

# No shutdown signal
exit 1
