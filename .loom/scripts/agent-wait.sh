#!/bin/bash
# agent-wait.sh - Wait for a tmux Claude agent to finish its task
#
# Thin stub that delegates to the Python loom-agent-wait CLI.
# The Python implementation provides identical behavior with better
# error handling, testability, and structured output.
#
# Exit codes:
#   0 - Agent completed (shell is idle, no claude process)
#   1 - Timeout reached
#   2 - Session not found
#
# Usage:
#   agent-wait.sh <name> [--timeout <seconds>] [--poll-interval <seconds>] [--json]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "agent-wait" "agent_wait" "$@"
