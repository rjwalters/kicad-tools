#!/bin/bash

# checkpoint.sh - Manage builder checkpoints for progress tracking
#
# This script allows builders to write checkpoints as they progress through
# stages of work. The shepherd uses these checkpoints to make smarter recovery
# decisions when builders fail.
#
# Usage:
#   checkpoint.sh write --stage <stage> [options]
#   checkpoint.sh read [--json]
#   checkpoint.sh clear
#   checkpoint.sh stages
#
# Stages (in order of progression):
#   planning      - Reading issue, planning approach
#   implementing  - Writing code, making changes
#   tested        - Tests ran (pass or fail)
#   committed     - Changes committed locally
#   pushed        - Branch pushed to remote
#   pr_created    - PR exists with proper labels
#
# Examples:
#   # Write checkpoint when starting implementation
#   checkpoint.sh write --stage implementing --issue 42
#
#   # Write checkpoint after tests pass
#   checkpoint.sh write --stage tested --test-result pass --test-command "pnpm check:ci"
#
#   # Write checkpoint after commit
#   checkpoint.sh write --stage committed --commit-sha abc123
#
#   # Read current checkpoint
#   checkpoint.sh read
#
#   # Read checkpoint as JSON
#   checkpoint.sh read --json
#
# See `loom-checkpoint --help` for full usage.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "checkpoint" "checkpoints" "$@"
