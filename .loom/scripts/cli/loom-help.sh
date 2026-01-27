#!/usr/bin/env bash
# loom help - Show help for Loom CLI
#
# Usage:
#   loom help              Show main help
#   loom help <command>    Show help for specific command
#   loom --help            Same as 'loom help'

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

REPO_ROOT=$(find_repo_root)
CLI_DIR=""
if [[ -n "$REPO_ROOT" ]]; then
    CLI_DIR="$REPO_ROOT/.loom/scripts/cli"
fi

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

# Show main help
show_main_help() {
    cat <<EOF
${BOLD}Loom CLI - tmux agent pool management${NC}

Loom is a multi-terminal orchestration system for AI-powered development.
This CLI provides management of tmux-backed agent pools.

${YELLOW}USAGE:${NC}
    ./loom <command> [options]

${YELLOW}COMMANDS:${NC}
    ${GREEN}start${NC}     Spawn agent pool from .loom/config.json
    ${GREEN}status${NC}    Display agent pool state and work queues
    ${GREEN}stop${NC}      Graceful shutdown (or --force to kill immediately)
    ${GREEN}attach${NC}    Open live tmux session for an agent
    ${GREEN}send${NC}      Send command to agent session
    ${GREEN}scale${NC}     Dynamic agent scaling
    ${GREEN}logs${NC}      Tail agent output
    ${GREEN}help${NC}      Show this help message

${YELLOW}QUICK START:${NC}
    ${GRAY}# Start all configured agents${NC}
    ./loom start

    ${GRAY}# Check what's running${NC}
    ./loom status

    ${GRAY}# View an agent's terminal${NC}
    ./loom attach shepherd-1

    ${GRAY}# Send a command to an agent${NC}
    ./loom send shepherd-1 "/shepherd 123 --force-pr"

    ${GRAY}# View logs${NC}
    ./loom logs shepherd-1

    ${GRAY}# Stop everything${NC}
    ./loom stop

${YELLOW}EXAMPLES:${NC}
    ./loom start                  Start all configured agents
    ./loom start --only shepherd  Start only shepherd agents
    ./loom status                 Show current state
    ./loom status --json          Machine-readable status
    ./loom attach shepherd-1      Connect to agent terminal
    ./loom send shepherd-1 "/shepherd 123"  Send command to agent
    ./loom stop                   Graceful shutdown
    ./loom stop --force           Force kill all sessions
    ./loom stop shepherd-1        Stop single agent
    ./loom scale shepherd 3       Scale shepherd pool to 3
    ./loom logs terminal-1        Tail agent output
    ./loom logs --all             Tail all agent logs

${YELLOW}SSH AND REMOTE:${NC}
    All commands work over SSH without \$DISPLAY.
    For non-interactive scripts, use --yes flag to skip prompts.

${YELLOW}CONFIGURATION:${NC}
    Agents are configured in .loom/config.json:

    {
      "terminals": [
        {
          "id": "terminal-1",
          "name": "Builder",
          "roleConfig": {
            "roleFile": "builder.md"
          }
        }
      ]
    }

${YELLOW}TMUX SOCKET:${NC}
    Loom uses a dedicated tmux socket named "loom" for isolation.
    Sessions are named "loom-<agent-id>" (e.g., loom-shepherd-1).

    To manually interact with Loom tmux sessions:
    ${GRAY}tmux -L loom list-sessions${NC}
    ${GRAY}tmux -L loom attach -t loom-shepherd-1${NC}

${YELLOW}FOR MORE HELP:${NC}
    ./loom <command> --help    Command-specific help
    ./loom help <command>      Same as above

${YELLOW}RELATED COMMANDS:${NC}
    /loom                     Run the daemon (Layer 2 orchestration)
    /shepherd <issue>         Orchestrate a single issue lifecycle
    /builder, /judge, etc.    Assume specialized agent roles

${GRAY}Loom CLI v0.1.0${NC}
EOF
}

# Show command-specific help
show_command_help() {
    local command="$1"

    case "$command" in
        start)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-start.sh" ]]; then
                exec "$CLI_DIR/loom-start.sh" --help
            else
                echo -e "${RED}Error: start command not installed${NC}"
                exit 1
            fi
            ;;
        status)
            if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/scripts/loom-status.sh" ]]; then
                exec "$REPO_ROOT/.loom/scripts/loom-status.sh" --help
            else
                echo -e "${RED}Error: status command not installed${NC}"
                exit 1
            fi
            ;;
        stop)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-stop.sh" ]]; then
                exec "$CLI_DIR/loom-stop.sh" --help
            else
                echo -e "${RED}Error: stop command not installed${NC}"
                exit 1
            fi
            ;;
        attach)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-attach.sh" ]]; then
                exec "$CLI_DIR/loom-attach.sh" --help
            else
                echo -e "${RED}Error: attach command not installed${NC}"
                exit 1
            fi
            ;;
        scale)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-scale.sh" ]]; then
                exec "$CLI_DIR/loom-scale.sh" --help
            else
                echo -e "${RED}Error: scale command not installed${NC}"
                exit 1
            fi
            ;;
        logs)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-logs.sh" ]]; then
                exec "$CLI_DIR/loom-logs.sh" --help
            else
                echo -e "${RED}Error: logs command not installed${NC}"
                exit 1
            fi
            ;;
        send)
            if [[ -n "$CLI_DIR" && -f "$CLI_DIR/loom-send.sh" ]]; then
                exec "$CLI_DIR/loom-send.sh" --help
            else
                echo -e "${RED}Error: send command not installed${NC}"
                exit 1
            fi
            ;;
        help)
            show_main_help
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}"
            echo ""
            echo "Available commands: start, status, stop, attach, send, scale, logs, help"
            exit 1
            ;;
    esac
}

# Main
main() {
    case "${1:-}" in
        ""|--help|-h)
            show_main_help
            ;;
        *)
            show_command_help "$1"
            ;;
    esac
}

main "$@"
