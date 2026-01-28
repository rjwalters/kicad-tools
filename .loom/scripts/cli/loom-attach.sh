#!/usr/bin/env bash
# loom attach - Open live tmux session for an agent
#
# Usage:
#   loom attach <agent-name>      Attach to agent terminal
#   loom attach --list            List available sessions
#   loom attach --help            Show help
#
# Examples:
#   loom attach shepherd-1        Attach to shepherd-1 terminal
#   loom attach terminal-2        Attach to terminal-2
#
# Keyboard shortcuts when attached:
#   Ctrl+B then d                 Detach (leave agent running)
#   Ctrl+B then [                 Scroll mode (q to exit)
#   Ctrl+B then ?                 Show all shortcuts

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

# ANSI colors
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    BOLD=''
    NC=''
fi

# Show help
show_help() {
    cat <<EOF
${BOLD}loom attach - Open live tmux session for an agent${NC}

${YELLOW}USAGE:${NC}
    loom attach <agent-name>      Attach to agent terminal
    loom attach --list            List available sessions
    loom attach --help            Show this help

${YELLOW}EXAMPLES:${NC}
    loom attach shepherd-1        Attach to shepherd-1 terminal
    loom attach terminal-2        Attach to terminal-2

${YELLOW}KEYBOARD SHORTCUTS (when attached):${NC}
    Ctrl+B then d                 Detach (leave agent running)
    Ctrl+B then [                 Scroll mode (q to exit)
    Ctrl+B then ?                 Show all shortcuts

${YELLOW}SESSION NAMING:${NC}
    Loom uses tmux sessions with the naming pattern:
      loom-<agent-name>           e.g., loom-shepherd-1, loom-terminal-2

    You can specify either the full session name or just the agent name:
      loom attach loom-shepherd-1  (full name)
      loom attach shepherd-1       (short name - loom- prefix added)
EOF
}

# List available sessions
list_sessions() {
    echo -e "${BOLD}Available Loom sessions:${NC}"
    echo ""

    # Check if tmux is running with loom socket
    if ! tmux -L loom list-sessions 2>/dev/null; then
        echo -e "${YELLOW}No Loom sessions found${NC}"
        echo ""
        echo "Start agents with: ./loom start"
        return 0
    fi

    echo ""
    echo -e "${CYAN}To attach:${NC} loom attach <name>"
}

# Main logic
main() {
    local agent_name=""

    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                exit 0
                ;;
            --list|-l)
                list_sessions
                exit 0
                ;;
            -*)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Use 'loom attach --help' for usage" >&2
                exit 1
                ;;
            *)
                agent_name="$1"
                shift
                ;;
        esac
    done

    if [[ -z "$agent_name" ]]; then
        echo -e "${RED}Error: Agent name required${NC}" >&2
        echo "Usage: loom attach <agent-name>" >&2
        echo ""
        echo "Available sessions:"
        tmux -L loom list-sessions 2>/dev/null || echo "  (none running)"
        exit 1
    fi

    # Check if tmux is installed
    if ! command -v tmux &> /dev/null; then
        echo -e "${RED}Error: tmux is not installed${NC}" >&2
        echo "Install with: brew install tmux (macOS) or apt-get install tmux (Linux)" >&2
        exit 1
    fi

    # Normalize session name (add loom- prefix if not present)
    local session_name="$agent_name"
    if [[ ! "$session_name" =~ ^loom- ]]; then
        session_name="loom-$agent_name"
    fi

    # Check if session exists
    if ! tmux -L loom has-session -t "$session_name" 2>/dev/null; then
        echo -e "${RED}Error: Session '$session_name' is not running${NC}" >&2
        echo ""
        echo "Available sessions:"
        tmux -L loom list-sessions 2>/dev/null || echo "  (none running)"
        echo ""
        echo "Use './loom status' to see all agents" >&2
        exit 1
    fi

    echo -e "${GREEN}Attaching to $session_name...${NC}"
    echo -e "${CYAN}Tip: Press Ctrl+B then d to detach${NC}"
    echo ""

    # Attach to the session
    exec tmux -L loom attach -t "$session_name"
}

main "$@"
