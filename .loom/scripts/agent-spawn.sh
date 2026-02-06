#!/bin/bash
# agent-spawn.sh - Thin stub that delegates to the Python loom-agent-spawn CLI.
#
# This script preserves backward compatibility for callers that invoke
# agent-spawn.sh directly.  All logic now lives in:
#   loom-tools/src/loom_tools/agent_spawn.py
#
# Usage is unchanged — all flags are forwarded as-is:
#   agent-spawn.sh --role <role> --name <name> [--args "<args>"] [--worktree <path>]
#   agent-spawn.sh --check <name>
#   agent-spawn.sh --list
#   agent-spawn.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "agent-spawn" "agent_spawn" "$@"
