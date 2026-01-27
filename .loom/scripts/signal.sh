#!/bin/bash

# signal.sh - Graceful shutdown signaling for Loom agents
#
# Usage:
#   signal.sh stop <agent-id|all>   - Send stop signal to agent or all agents
#   signal.sh check <agent-id>      - Check if stop signal exists (exit 0 if yes, 1 if no)
#   signal.sh clear <agent-id|all>  - Clear stop signal for agent or all agents
#   signal.sh list                  - List all active stop signals
#   signal.sh --help                - Show help
#
# Agents should call 'signal.sh check <agent-id>' before claiming new work.
# If exit code is 0, the agent should complete current work and exit gracefully.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Find the repository root (works from any subdirectory)
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
SIGNALS_DIR="$REPO_ROOT/.loom/signals"

# Ensure signals directory exists
ensure_signals_dir() {
    if [[ ! -d "$SIGNALS_DIR" ]]; then
        mkdir -p "$SIGNALS_DIR"
    fi
}

# Show help
show_help() {
    cat <<EOF
${BLUE}signal.sh - Graceful shutdown signaling for Loom agents${NC}

${YELLOW}USAGE:${NC}
    signal.sh stop <agent-id|all>   - Send stop signal to agent or all agents
    signal.sh check <agent-id>      - Check if stop signal exists (exit 0=yes, 1=no)
    signal.sh clear <agent-id|all>  - Clear stop signal for agent or all agents
    signal.sh list                  - List all active stop signals
    signal.sh --help                - Show this help

${YELLOW}EXAMPLES:${NC}
    # Stop a specific agent
    signal.sh stop terminal-1

    # Stop all agents
    signal.sh stop all

    # Check if agent should stop (use in agent work loop)
    if signal.sh check terminal-1; then
        echo "Stop signal received, exiting gracefully"
        exit 0
    fi

    # Clear stop signal after agent has stopped
    signal.sh clear terminal-1

    # Clear all stop signals
    signal.sh clear all

    # List active signals
    signal.sh list

${YELLOW}INTEGRATION:${NC}
    Agents should check for stop signals before claiming new work:

    # In agent work loop
    if ./.loom/scripts/signal.sh check "\$AGENT_ID"; then
        echo "Stop signal received, completing current work and exiting"
        # ... complete current work ...
        exit 0
    fi

${YELLOW}SIGNAL FILES:${NC}
    Signals are stored as files in .loom/signals/:
    - stop-all           : Stops all agents
    - stop-<agent-id>    : Stops specific agent

    Files contain timestamp and optional message.
EOF
}

# Send stop signal to an agent
send_stop_signal() {
    local target="$1"
    local message="${2:-Manual stop requested}"

    ensure_signals_dir

    local signal_file
    if [[ "$target" == "all" ]]; then
        signal_file="$SIGNALS_DIR/stop-all"
    else
        signal_file="$SIGNALS_DIR/stop-$target"
    fi

    # Write signal file with timestamp and message
    cat > "$signal_file" <<EOF
timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
message=$message
sender=${USER:-unknown}
EOF

    if [[ "$target" == "all" ]]; then
        echo -e "${YELLOW}⚠ Stop signal sent to ALL agents${NC}"
    else
        echo -e "${YELLOW}⚠ Stop signal sent to: $target${NC}"
    fi
    echo -e "${BLUE}ℹ Signal file: $signal_file${NC}"
}

# Check if stop signal exists for an agent
# Returns: 0 if signal exists (agent should stop), 1 if no signal (agent can continue)
check_stop_signal() {
    local agent_id="$1"

    # Check for stop-all signal
    if [[ -f "$SIGNALS_DIR/stop-all" ]]; then
        return 0
    fi

    # Check for agent-specific signal
    if [[ -f "$SIGNALS_DIR/stop-$agent_id" ]]; then
        return 0
    fi

    # No stop signal found
    return 1
}

# Clear stop signal for an agent
clear_stop_signal() {
    local target="$1"

    if [[ "$target" == "all" ]]; then
        # Clear all stop signals
        if [[ -d "$SIGNALS_DIR" ]]; then
            local count=0
            for signal_file in "$SIGNALS_DIR"/stop-*; do
                if [[ -f "$signal_file" ]]; then
                    rm -f "$signal_file"
                    ((count++)) || true
                fi
            done
            if [[ $count -gt 0 ]]; then
                echo -e "${GREEN}✓ Cleared $count stop signal(s)${NC}"
            else
                echo -e "${BLUE}ℹ No stop signals to clear${NC}"
            fi
        else
            echo -e "${BLUE}ℹ No signals directory exists${NC}"
        fi
    else
        # Clear specific agent signal
        local signal_file="$SIGNALS_DIR/stop-$target"
        if [[ -f "$signal_file" ]]; then
            rm -f "$signal_file"
            echo -e "${GREEN}✓ Cleared stop signal for: $target${NC}"
        else
            echo -e "${BLUE}ℹ No stop signal found for: $target${NC}"
        fi

        # Also check if we need to remove stop-all
        # (Don't auto-remove stop-all when clearing individual - that's intentional)
    fi
}

# List all active stop signals
list_signals() {
    ensure_signals_dir

    local found=0
    echo -e "${BLUE}Active stop signals:${NC}"
    echo ""

    for signal_file in "$SIGNALS_DIR"/stop-*; do
        if [[ -f "$signal_file" ]]; then
            found=1
            local filename=$(basename "$signal_file")
            local target="${filename#stop-}"

            # Read signal details
            local timestamp=$(grep "^timestamp=" "$signal_file" 2>/dev/null | cut -d= -f2-)
            local message=$(grep "^message=" "$signal_file" 2>/dev/null | cut -d= -f2-)
            local sender=$(grep "^sender=" "$signal_file" 2>/dev/null | cut -d= -f2-)

            if [[ "$target" == "all" ]]; then
                echo -e "  ${RED}● stop-all${NC} (affects all agents)"
            else
                echo -e "  ${YELLOW}● $target${NC}"
            fi
            echo -e "    Created: ${timestamp:-unknown}"
            echo -e "    Message: ${message:-none}"
            echo -e "    By: ${sender:-unknown}"
            echo ""
        fi
    done

    if [[ $found -eq 0 ]]; then
        echo -e "  ${GREEN}No active stop signals${NC}"
    fi
}

# Main command handling
main() {
    if [[ $# -eq 0 ]]; then
        show_help
        exit 0
    fi

    local command="$1"
    shift

    case "$command" in
        stop)
            if [[ $# -lt 1 ]]; then
                echo -e "${RED}Error: 'stop' requires a target (agent-id or 'all')${NC}" >&2
                echo "Usage: signal.sh stop <agent-id|all>" >&2
                exit 1
            fi
            send_stop_signal "$1" "${2:-}"
            ;;
        check)
            if [[ $# -lt 1 ]]; then
                echo -e "${RED}Error: 'check' requires an agent-id${NC}" >&2
                echo "Usage: signal.sh check <agent-id>" >&2
                exit 1
            fi
            # Silent check - just exit code
            check_stop_signal "$1"
            ;;
        clear)
            if [[ $# -lt 1 ]]; then
                echo -e "${RED}Error: 'clear' requires a target (agent-id or 'all')${NC}" >&2
                echo "Usage: signal.sh clear <agent-id|all>" >&2
                exit 1
            fi
            clear_stop_signal "$1"
            ;;
        list)
            list_signals
            ;;
        --help|-h|help)
            show_help
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}" >&2
            echo "Run 'signal.sh --help' for usage" >&2
            exit 1
            ;;
    esac
}

main "$@"
