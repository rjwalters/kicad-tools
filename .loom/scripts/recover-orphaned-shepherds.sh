#!/bin/bash
# recover-orphaned-shepherds.sh - Detect and recover orphaned task state.
#
# Thin stub that delegates to the Python implementation. The script
# name is preserved for back-compat with operator muscle memory and any
# CLAUDE.md / docs that link to it; the underlying Python module was
# ported in Phase 3.1.6 (#3395, epic #3372) to read
# `.loom/spawn-loop-state.json` (#3374) + `gh issue list --label loom:building`
# instead of `.loom/daemon-state.json` + `.loom/progress/`.
#
# See `loom-tools/src/loom_tools/orphan_recovery.py` for the full
# implementation and the per-orphan recovery semantics.
#
# Usage:
#   recover-orphaned-shepherds.sh              # Dry-run: show what would be recovered
#   recover-orphaned-shepherds.sh --recover    # Actually recover orphaned state
#   recover-orphaned-shepherds.sh --json       # Output JSON for programmatic use
#   recover-orphaned-shepherds.sh --verbose    # Show detailed progress
#   recover-orphaned-shepherds.sh --help       # Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "recover-orphans" "orphan_recovery" "$@"
