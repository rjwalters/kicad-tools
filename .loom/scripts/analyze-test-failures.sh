#!/bin/bash

# analyze-test-failures.sh - Analyze shepherd test failure patterns
#
# Parses .loom/progress/shepherd-*.json files to categorize blocked runs
# and track Doctor effectiveness.
#
# Usage:
#   analyze-test-failures.sh                    # Summary (default)
#   analyze-test-failures.sh summary            # Overall summary
#   analyze-test-failures.sh categorize         # Categorize each failure
#   analyze-test-failures.sh doctor             # Doctor effectiveness
#   analyze-test-failures.sh --format json      # JSON output

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "test-failure-analysis" "test_failure_analysis" "$@"
