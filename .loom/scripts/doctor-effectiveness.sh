#!/bin/bash

# doctor-effectiveness.sh - Track Doctor success/skip rates
#
# Analyzes .loom/progress/shepherd-*.json files to understand
# when Doctor succeeds vs skips as "unrelated".
#
# Usage:
#   doctor-effectiveness.sh                     # Doctor analysis
#   doctor-effectiveness.sh --format json       # JSON output

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run doctor subcommand of test-failure-analysis
run_loom_tool "test-failure-analysis" "test_failure_analysis" doctor "$@"
