#!/bin/bash
# clean.sh - Backwards-compatible wrapper for loom-clean
#
# This is a thin stub that delegates to the Python implementation.
# See loom-tools/src/loom_tools/clean.py for the full implementation.
#
# Usage:
#   clean.sh             # Interactive cleanup
#   clean.sh --force     # Non-interactive cleanup
#   clean.sh --dry-run   # Preview what would be cleaned
#   clean.sh --deep      # Also remove build artifacts
#   clean.sh --help      # Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "clean" "clean" "$@"
