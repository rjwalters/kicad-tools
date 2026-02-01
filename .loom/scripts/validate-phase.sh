#!/bin/bash

# validate-phase.sh - Thin stub that delegates to the Python implementation.
#
# Usage:
#   validate-phase.sh <phase> <issue-number> [options]
#
# This script is a compatibility shim.  The real implementation lives in
# loom-tools/src/loom_tools/validate_phase.py and is available as the
# ``loom-validate-phase`` CLI entry point.
#
# Exit codes:
#   0 - Contract satisfied (initially or after recovery)
#   1 - Contract failed, recovery failed or not possible
#   2 - Invalid arguments

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "validate-phase" "validate_phase" "$@"
