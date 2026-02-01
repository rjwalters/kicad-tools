#!/bin/bash

# loom-status.sh - Read-only system status for Layer 3 observation
#
# This is a thin stub that delegates to the Python CLI (loom-status).
# The full implementation was ported from bash to Python in loom-tools.
#
# Usage:
#   loom-status.sh              - Display full system status
#   loom-status.sh --json       - Output status as JSON
#   loom-status.sh --help       - Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "status" "status" "$@"
