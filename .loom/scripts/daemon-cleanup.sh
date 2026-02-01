#!/bin/bash
# daemon-cleanup.sh - Event-driven cleanup for the Loom daemon
#
# This is a thin stub that delegates to the Python implementation.
# See loom-tools/src/loom_tools/daemon_cleanup.py for the full implementation.
#
# Usage:
#   daemon-cleanup.sh shepherd-complete <issue>  # Cleanup after shepherd finishes
#   daemon-cleanup.sh daemon-startup             # Cleanup stale artifacts
#   daemon-cleanup.sh daemon-shutdown            # Archive logs and cleanup
#   daemon-cleanup.sh periodic                   # Conservative periodic cleanup
#   daemon-cleanup.sh prune-sessions             # Prune old session archives
#   daemon-cleanup.sh <event> --dry-run          # Preview cleanup
#   daemon-cleanup.sh --help                     # Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source shared loom-tools helper
source "$SCRIPT_DIR/lib/loom-tools.sh"

# Run the command with proper fallback chain
run_loom_tool "daemon-cleanup" "daemon_cleanup" "$@"
