#!/usr/bin/env bash
# loom stop - Graceful shutdown of agent pool
#
# Usage:
#   loom stop                     Graceful shutdown (waits for phase boundaries)
#   loom stop --force             Force kill immediately
#   loom stop <agent-name>        Stop single agent
#   loom stop --help              Show help
#
# Examples:
#   loom stop                     Graceful shutdown of all agents
#   loom stop --force             Force kill all sessions
#   loom stop shepherd-1          Stop only shepherd-1

set -euo pipefail

# Source the process tree kill helper
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../kill-session-tree.sh
source "$SCRIPT_DIR/../kill-session-tree.sh"

# Find repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        if [[ -f "$dir/.git" ]]; then
            local gitdir
            gitdir=$(sed 's/^gitdir: //' "$dir/.git")
            local main_repo
            main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
            if [[ -d "$main_repo/.loom" ]]; then
                echo "$main_repo"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    echo ""
}

REPO_ROOT=$(find_repo_root)
if [[ -z "$REPO_ROOT" ]]; then
    echo "Error: Not in a Loom workspace (.loom directory not found)" >&2
    exit 1
fi

STOP_SIGNAL="$REPO_ROOT/.loom/stop-daemon"
LOG_DIR="/tmp"
TMUX_SOCKET="loom"
GRACEFUL_TIMEOUT="${LOOM_STOP_TIMEOUT:-300}"  # 5 minutes default

# ANSI colors
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    GRAY='\033[0;90m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    GRAY=''
    BOLD=''
    NC=''
fi

# Show help
show_help() {
    cat <<EOF
${BOLD}loom stop - Graceful shutdown of agent pool${NC}

${YELLOW}USAGE:${NC}
    loom stop                     Graceful shutdown (waits for phase boundaries)
    loom stop --force             Force kill immediately
    loom stop <agent-name>        Stop single agent
    loom stop --timeout <secs>    Custom timeout for graceful shutdown
    loom stop --help              Show this help

${YELLOW}OPTIONS:${NC}
    --force, -f           Force kill sessions immediately (no graceful wait)
    --timeout <seconds>   Maximum time to wait for graceful shutdown (default: 300)
    --yes, -y             Non-interactive mode, skip confirmation
    --clean               Also clean up log files after stop

${YELLOW}EXAMPLES:${NC}
    loom stop                     Graceful shutdown all agents
    loom stop --force             Force kill all sessions
    loom stop shepherd-1          Stop only shepherd-1
    loom stop --timeout 60        Wait max 60 seconds for graceful shutdown

${YELLOW}GRACEFUL SHUTDOWN:${NC}
    By default, stop signals agents to complete their current phase:

    1. Creates .loom/stop-daemon signal file
    2. Agents check this file and stop at phase boundaries
    3. Waits up to timeout for agents to complete
    4. After timeout, kills remaining sessions
    5. Cleans up signal files

${YELLOW}ENVIRONMENT:${NC}
    LOOM_STOP_TIMEOUT     Override default timeout (seconds)
EOF
}

# Get list of running loom sessions
get_running_sessions() {
    tmux -L "$TMUX_SOCKET" list-sessions -F "#{session_name}" 2>/dev/null | grep "^loom-" || true
}

# Count running sessions
count_sessions() {
    local count
    count=$(get_running_sessions | wc -l | tr -d ' ')
    echo "$count"
}

# Stop a single session
stop_session() {
    local session_name="$1"
    local force="${2:-false}"

    if [[ "$force" == "true" ]]; then
        # Force kill - kill process tree then destroy session
        kill_session_tree "$session_name" "--force" "$TMUX_SOCKET"
    else
        # Send Ctrl+C to interrupt current operation
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" C-c 2>/dev/null || true
        sleep 1
        # Then exit the shell
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "exit" C-m 2>/dev/null || true
    fi
}

# Wait for sessions to stop
wait_for_sessions() {
    local timeout="$1"
    local start_time
    start_time=$(date +%s)

    echo -e "${CYAN}Waiting for agents to complete (max ${timeout}s)...${NC}"

    while true; do
        local current_count
        current_count=$(count_sessions)

        if [[ "$current_count" -eq 0 ]]; then
            echo -e "${GREEN}All agents stopped${NC}"
            return 0
        fi

        local elapsed
        elapsed=$(($(date +%s) - start_time))

        if [[ $elapsed -ge $timeout ]]; then
            echo -e "${YELLOW}Timeout reached with $current_count session(s) still running${NC}"
            return 1
        fi

        echo -e "  ${GRAY}$current_count session(s) still running (${elapsed}s elapsed)${NC}"
        sleep 5
    done
}

# Clean up log files
cleanup_logs() {
    echo -e "${CYAN}Cleaning up log files...${NC}"

    local count=0
    for logfile in "$LOG_DIR"/loom-*.out; do
        if [[ -f "$logfile" ]]; then
            rm -f "$logfile"
            ((count++)) || true
        fi
    done

    if [[ $count -gt 0 ]]; then
        echo -e "  ${GREEN}Removed $count log file(s)${NC}"
    else
        echo -e "  ${GRAY}No log files to clean${NC}"
    fi
}

# Main logic
main() {
    local force=false
    local timeout="$GRACEFUL_TIMEOUT"
    local clean_logs=false
    local target_agent=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                exit 0
                ;;
            --force|-f)
                force=true
                shift
                ;;
            --timeout)
                if [[ -z "${2:-}" ]]; then
                    echo -e "${RED}Error: --timeout requires a number${NC}" >&2
                    exit 1
                fi
                timeout="$2"
                shift 2
                ;;
            --yes|-y)
                # Accepted for compatibility but not currently used
                shift
                ;;
            --clean)
                clean_logs=true
                shift
                ;;
            -*)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Use 'loom stop --help' for usage" >&2
                exit 1
                ;;
            *)
                target_agent="$1"
                shift
                ;;
        esac
    done

    # Check if tmux is available
    if ! command -v tmux &> /dev/null; then
        echo -e "${RED}Error: tmux is not installed${NC}" >&2
        exit 1
    fi

    # Single agent stop
    if [[ -n "$target_agent" ]]; then
        local session_name="$target_agent"
        if [[ ! "$session_name" =~ ^loom- ]]; then
            session_name="loom-$target_agent"
        fi

        if ! tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
            echo -e "${YELLOW}Session '$session_name' is not running${NC}"
            exit 0
        fi

        echo -e "${BOLD}Stopping $session_name...${NC}"

        if [[ "$force" == "true" ]]; then
            kill_session_tree "$session_name" "--force" "$TMUX_SOCKET"
            echo -e "${GREEN}Session killed${NC}"
        else
            stop_session "$session_name" false
            sleep 2

            if tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
                echo -e "${YELLOW}Session still running, force killing...${NC}"
                kill_session_tree "$session_name" "" "$TMUX_SOCKET"
            fi
            echo -e "${GREEN}Session stopped${NC}"
        fi

        exit 0
    fi

    # All agents stop
    local current_count
    current_count=$(count_sessions)

    if [[ "$current_count" -eq 0 ]]; then
        echo -e "${GRAY}No Loom sessions running${NC}"

        # Clean up stale signal file if exists
        if [[ -f "$STOP_SIGNAL" ]]; then
            rm -f "$STOP_SIGNAL"
            echo -e "${GRAY}Removed stale stop signal${NC}"
        fi

        if [[ "$clean_logs" == "true" ]]; then
            cleanup_logs
        fi

        exit 0
    fi

    echo -e "${BOLD}Loom Stop${NC}"
    echo ""
    echo -e "  Running sessions: ${CYAN}$current_count${NC}"
    echo ""

    if [[ "$force" == "true" ]]; then
        echo -e "${YELLOW}Force stopping all sessions...${NC}"
        echo ""

        get_running_sessions | while read -r session; do
            echo -e "  ${RED}Killing:${NC} $session"
            kill_session_tree "$session" "--force" "$TMUX_SOCKET"
        done

        echo ""
        echo -e "${GREEN}All sessions killed${NC}"

        # Sweep for any orphaned claude processes that escaped
        sweep_orphaned_claude_processes "--force"
    else
        echo -e "${CYAN}Starting graceful shutdown...${NC}"
        echo ""

        # Create stop signal file
        touch "$STOP_SIGNAL"
        echo -e "  ${GREEN}Created stop signal: $STOP_SIGNAL${NC}"

        # Send Ctrl+C to all sessions to interrupt current operations
        get_running_sessions | while read -r session; do
            echo -e "  ${CYAN}Signaling:${NC} $session"
            tmux -L "$TMUX_SOCKET" send-keys -t "$session" C-c 2>/dev/null || true
        done

        echo ""

        # Wait for graceful shutdown
        if wait_for_sessions "$timeout"; then
            echo ""
        else
            echo ""
            echo -e "${YELLOW}Force killing remaining sessions...${NC}"

            get_running_sessions | while read -r session; do
                echo -e "  ${RED}Killing:${NC} $session"
                kill_session_tree "$session" "--force" "$TMUX_SOCKET"
            done
        fi

        # Sweep for any orphaned claude processes that escaped
        sweep_orphaned_claude_processes

        # Remove stop signal
        rm -f "$STOP_SIGNAL"
        echo -e "${GRAY}Removed stop signal${NC}"
    fi

    # Clean up logs if requested
    if [[ "$clean_logs" == "true" ]]; then
        echo ""
        cleanup_logs
    fi

    echo ""
    echo -e "${GREEN}Shutdown complete${NC}"
}

main "$@"
