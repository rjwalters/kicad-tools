#!/usr/bin/env bash
# is-force-mode.sh - Check if the Loom daemon is running in force mode
#
# This script allows other roles (especially Champion) to detect when the daemon
# is running with --force flag, enabling aggressive autonomous development.
#
# Usage:
#   ./.loom/scripts/is-force-mode.sh          # Exit 0 if force mode, 1 otherwise
#   ./.loom/scripts/is-force-mode.sh --json   # Output JSON result
#   ./.loom/scripts/is-force-mode.sh --quiet  # No output, just exit code
#
# Exit codes:
#   0 - Force mode is enabled
#   1 - Force mode is not enabled (or daemon state not found)
#
# Example usage in Champion role:
#   if ./.loom/scripts/is-force-mode.sh; then
#       echo "Auto-promoting proposals in force mode"
#   fi
#
# Example usage for conditional logic:
#   FORCE_MODE=$(./.loom/scripts/is-force-mode.sh --json | jq -r '.force_mode')

set -euo pipefail

STATE_FILE=".loom/daemon-state.json"

# Parse arguments
JSON_OUTPUT=false
QUIET=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --quiet|-q)
            QUIET=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--json] [--quiet]"
            echo ""
            echo "Check if the Loom daemon is running in force mode."
            echo ""
            echo "Options:"
            echo "  --json     Output JSON result"
            echo "  --quiet    No output, just exit code"
            echo "  --help     Show this help message"
            echo ""
            echo "Exit codes:"
            echo "  0 - Force mode is enabled"
            echo "  1 - Force mode is not enabled (or daemon state not found)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

# Check if daemon state file exists
if [[ ! -f "$STATE_FILE" ]]; then
    if [[ "$JSON_OUTPUT" == true ]]; then
        echo '{"force_mode": false, "reason": "daemon_state_not_found"}'
    elif [[ "$QUIET" != true ]]; then
        echo "Daemon state file not found: $STATE_FILE"
    fi
    exit 1
fi

# Read force_mode from daemon state
FORCE_MODE=$(jq -r '.force_mode // false' "$STATE_FILE" 2>/dev/null || echo "false")
DAEMON_RUNNING=$(jq -r '.running // false' "$STATE_FILE" 2>/dev/null || echo "false")

# Output result based on flags
if [[ "$JSON_OUTPUT" == true ]]; then
    echo "{\"force_mode\": $FORCE_MODE, \"daemon_running\": $DAEMON_RUNNING}"
elif [[ "$QUIET" != true ]]; then
    if [[ "$FORCE_MODE" == "true" ]]; then
        echo "Force mode: enabled"
    else
        echo "Force mode: disabled"
    fi
fi

# Exit with appropriate code
if [[ "$FORCE_MODE" == "true" ]]; then
    exit 0
else
    exit 1
fi
