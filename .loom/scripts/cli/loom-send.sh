#!/usr/bin/env bash
# loom send - Send command to a tmux agent session
#
# Usage:
#   loom send <agent-name> "<command>"   Send command to agent
#   loom send --list                     List available sessions
#   loom send --help                     Show help
#
# Examples:
#   loom send shepherd-1 "/shepherd 123 --force-pr"
#   loom send terminal-2 "/builder 456"
#   loom send champion "/champion"
#
# Notes:
#   - Commands are sent with a trailing newline (Enter key)
#   - Returns immediately (non-blocking)
#   - Agent must be running in tmux pool

set -euo pipefail

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

# shellcheck disable=SC2034  # Used for validation side-effect of find_repo_root
REPO_ROOT=$(find_repo_root)
TMUX_SOCKET="loom"

# ANSI colors
# shellcheck disable=SC2034  # Color palette - not all colors used in every script
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
${BOLD}loom send - Send command to a tmux agent session${NC}

${YELLOW}USAGE:${NC}
    loom send <agent-name> "<command>"   Send command to agent
    loom send --list                     List available sessions
    loom send --help                     Show this help

${YELLOW}OPTIONS:${NC}
    --list, -l        List available sessions to send to
    --no-newline      Don't append newline (Enter key)
    --wait <ms>       Wait for specified milliseconds after sending
    --json            Output result as JSON

${YELLOW}EXAMPLES:${NC}
    loom send shepherd-1 "/shepherd 123 --force-pr"
    loom send terminal-2 "/builder 456"
    loom send champion "/champion"

${YELLOW}SESSION NAMING:${NC}
    Loom uses tmux sessions with the naming pattern:
      loom-<agent-name>           e.g., loom-shepherd-1, loom-terminal-2

    You can specify either the full session name or just the agent name:
      loom send loom-shepherd-1 "..."   (full name)
      loom send shepherd-1 "..."        (short name - loom- prefix added)

${YELLOW}NOTES:${NC}
    - Commands are sent with a trailing newline (Enter key) by default
    - Returns immediately after sending (non-blocking)
    - Use with daemon iteration to dispatch work to tmux agents
    - Exit code 0 = success, 1 = error, 2 = session not found
EOF
}

# List available sessions
list_sessions() {
    local json_output="${1:-false}"

    if [[ "$json_output" == "true" ]]; then
        # JSON output
        local sessions
        sessions=$(tmux -L "$TMUX_SOCKET" list-sessions -F "#{session_name}" 2>/dev/null || true)

        if [[ -z "$sessions" ]]; then
            echo '{"sessions":[],"count":0}'
            return 0
        fi

        local json_array="["
        local first=true
        while IFS= read -r session; do
            if [[ -n "$session" ]]; then
                if [[ "$first" == "true" ]]; then
                    first=false
                else
                    json_array+=","
                fi
                json_array+="\"$session\""
            fi
        done <<< "$sessions"
        json_array+="]"

        local count
        count=$(echo "$sessions" | grep -c . || echo "0")
        echo "{\"sessions\":$json_array,\"count\":$count}"
        return 0
    fi

    echo -e "${BOLD}Available Loom sessions:${NC}"
    echo ""

    # Check if tmux is running with loom socket
    if ! tmux -L "$TMUX_SOCKET" list-sessions 2>/dev/null; then
        echo -e "${YELLOW}No Loom sessions found${NC}"
        echo ""
        echo "Start agents with: loom start"
        return 0
    fi

    echo ""
    echo -e "${CYAN}To send command:${NC} loom send <name> \"<command>\""
}

# Send command to session
send_to_session() {
    local session_name="$1"
    local command="$2"
    local append_newline="${3:-true}"
    local wait_ms="${4:-0}"
    local json_output="${5:-false}"

    # Normalize session name (add loom- prefix if not present)
    if [[ ! "$session_name" =~ ^loom- ]]; then
        session_name="loom-$session_name"
    fi

    # Check if session exists
    if ! tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
        if [[ "$json_output" == "true" ]]; then
            echo "{\"success\":false,\"error\":\"session_not_found\",\"session\":\"$session_name\"}"
        else
            echo -e "${RED}Error: Session '$session_name' is not running${NC}" >&2
            echo "" >&2
            echo "Available sessions:" >&2
            tmux -L "$TMUX_SOCKET" list-sessions 2>/dev/null || echo "  (none running)" >&2
        fi
        return 2
    fi

    # Send the command
    if [[ "$append_newline" == "true" ]]; then
        # Send with Enter key
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "$command" C-m
    else
        # Send without Enter key
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "$command"
    fi

    # Wait if specified
    if [[ "$wait_ms" -gt 0 ]]; then
        # Convert ms to seconds for sleep (bash doesn't support ms natively)
        local wait_sec
        wait_sec=$(echo "scale=3; $wait_ms / 1000" | bc)
        sleep "$wait_sec"
    fi

    if [[ "$json_output" == "true" ]]; then
        echo "{\"success\":true,\"session\":\"$session_name\",\"command\":$(printf '%s' "$command" | jq -Rs .)}"
    else
        echo -e "${GREEN}Sent to $session_name${NC}"
    fi

    return 0
}

# Main logic
main() {
    local agent_name=""
    local command=""
    local append_newline=true
    local wait_ms=0
    local json_output=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                exit 0
                ;;
            --list|-l)
                list_sessions "$json_output"
                exit 0
                ;;
            --no-newline)
                append_newline=false
                shift
                ;;
            --wait)
                if [[ -z "${2:-}" ]]; then
                    echo -e "${RED}Error: --wait requires a value in milliseconds${NC}" >&2
                    exit 1
                fi
                wait_ms="$2"
                shift 2
                ;;
            --json)
                json_output=true
                shift
                ;;
            -*)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Use 'loom send --help' for usage" >&2
                exit 1
                ;;
            *)
                if [[ -z "$agent_name" ]]; then
                    agent_name="$1"
                elif [[ -z "$command" ]]; then
                    command="$1"
                else
                    echo -e "${RED}Error: Too many arguments${NC}" >&2
                    echo "Usage: loom send <agent-name> \"<command>\"" >&2
                    exit 1
                fi
                shift
                ;;
        esac
    done

    # Check if tmux is installed
    if ! command -v tmux &> /dev/null; then
        if [[ "$json_output" == "true" ]]; then
            echo '{"success":false,"error":"tmux_not_installed"}'
        else
            echo -e "${RED}Error: tmux is not installed${NC}" >&2
            echo "Install with: brew install tmux (macOS) or apt-get install tmux (Linux)" >&2
        fi
        exit 1
    fi

    # Validate arguments
    if [[ -z "$agent_name" ]]; then
        if [[ "$json_output" == "true" ]]; then
            echo '{"success":false,"error":"missing_agent_name"}'
        else
            echo -e "${RED}Error: Agent name required${NC}" >&2
            echo "Usage: loom send <agent-name> \"<command>\"" >&2
        fi
        exit 1
    fi

    if [[ -z "$command" ]]; then
        if [[ "$json_output" == "true" ]]; then
            echo '{"success":false,"error":"missing_command"}'
        else
            echo -e "${RED}Error: Command required${NC}" >&2
            echo "Usage: loom send <agent-name> \"<command>\"" >&2
        fi
        exit 1
    fi

    send_to_session "$agent_name" "$command" "$append_newline" "$wait_ms" "$json_output"
}

main "$@"
