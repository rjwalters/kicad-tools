#!/usr/bin/env bash
# validate-daemon-state.sh - Thin stub delegating to Python implementation
#
# See loom-tools/src/loom_tools/validate_state.py for the full implementation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "validate-state" "validate_state" "$@"
