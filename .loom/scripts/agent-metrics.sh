#!/bin/bash

# agent-metrics.sh - Thin stub delegating to loom-agent-metrics (Python)
#
# This script preserves the CLI interface for backwards compatibility.
# The MCP tool (mcp-loom) shells out to this script, so the interface
# must remain stable.
#
# Usage:
#   agent-metrics.sh [--role ROLE] [--period PERIOD] [--format FORMAT]
#   agent-metrics.sh summary
#   agent-metrics.sh effectiveness [--role ROLE] [--by-model]
#   agent-metrics.sh costs [--issue NUMBER] [--by-model]
#   agent-metrics.sh velocity
#   agent-metrics.sh --help
#
# --by-model (#3482, Phase 3a) adds a per-model dimension to the
# effectiveness/costs output; NULL/absent model values render as 'default'.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "agent-metrics" "agent_metrics" "$@"
