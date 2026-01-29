#!/usr/bin/env bash
# loom start - Spawn agent pool from config
#
# Usage:
#   loom start                    Start all configured agents
#   loom start --only <role>      Start only agents with matching role
#   loom start --dry-run          Show what would be started
#   loom start --help             Show help
#
# Examples:
#   loom start                    Start all agents from config
#   loom start --only shepherd    Start only shepherd agents
#   loom start --only builder     Start only builder agents

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
if [[ -z "$REPO_ROOT" ]]; then
    echo "Error: Not in a Loom workspace (.loom directory not found)" >&2
    exit 1
fi

CONFIG_FILE="$REPO_ROOT/.loom/config.json"
# shellcheck disable=SC2034  # LOG_DIR reserved for future logging enhancements
LOG_DIR="/tmp"
TMUX_SOCKET="loom"

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
${BOLD}loom start - Spawn agent pool from config${NC}

${YELLOW}USAGE:${NC}
    loom start                    Start all configured agents
    loom start --only <role>      Start only agents with matching role
    loom start --dry-run          Show what would be started
    loom start --yes              Skip confirmation prompts
    loom start --help             Show this help

${YELLOW}OPTIONS:${NC}
    --only <role>     Filter agents by role file (e.g., shepherd, builder, judge)
    --dry-run         Preview what would be started without doing it
    --yes, -y         Non-interactive mode, skip prompts
    --force           Force restart of already-running agents

${YELLOW}EXAMPLES:${NC}
    loom start                    Start all agents from config
    loom start --only shepherd    Start only shepherd agents
    loom start --only builder     Start only builder agents
    loom start --dry-run          Preview what would start

${YELLOW}CONFIGURATION:${NC}
    Agents are configured in .loom/config.json with this structure:

    {
      "terminals": [
        {
          "id": "terminal-1",
          "name": "Builder",
          "role": "claude-code-worker",
          "roleConfig": {
            "roleFile": "builder.md",
            "targetInterval": 0,
            "intervalPrompt": ""
          }
        }
      ]
    }

${YELLOW}REQUIREMENTS:${NC}
    - tmux must be installed
    - claude CLI must be in PATH
    - .loom/config.json must exist
EOF
}

# Check dependencies
check_dependencies() {
    local missing=()

    if ! command -v tmux &> /dev/null; then
        missing+=("tmux")
    fi

    if ! command -v jq &> /dev/null; then
        missing+=("jq")
    fi

    if ! command -v claude &> /dev/null; then
        missing+=("claude")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "${RED}Error: Missing required dependencies: ${missing[*]}${NC}" >&2
        echo "" >&2
        echo "Install with:" >&2
        for dep in "${missing[@]}"; do
            case $dep in
                tmux)
                    echo "  brew install tmux (macOS) or apt-get install tmux (Linux)" >&2
                    ;;
                jq)
                    echo "  brew install jq (macOS) or apt-get install jq (Linux)" >&2
                    ;;
                claude)
                    echo "  npm install -g @anthropic-ai/claude-code" >&2
                    ;;
            esac
        done
        exit 1
    fi
}

# Check if config file exists
check_config() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo -e "${RED}Error: Configuration file not found: $CONFIG_FILE${NC}" >&2
        echo "" >&2
        echo "Have you initialized Loom in this repository?" >&2
        echo "Run: ./scripts/install-loom.sh" >&2
        exit 1
    fi

    # Validate JSON
    if ! jq empty "$CONFIG_FILE" 2>/dev/null; then
        echo -e "${RED}Error: Invalid JSON in $CONFIG_FILE${NC}" >&2
        exit 1
    fi
}

# Get session name for a terminal
get_session_name() {
    local terminal_id="$1"
    echo "loom-$terminal_id"
}

# Check if session is already running
is_session_running() {
    local session_name="$1"
    tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null
}

# Spawn a single agent using agent-spawn.sh
spawn_agent() {
    local terminal_id="$1"
    local terminal_name="$2"
    local role_file="$3"
    local interval="${4:-0}"
    local interval_prompt="${5:-}"
    local dry_run="${6:-false}"
    local force="${7:-false}"

    local session_name
    session_name=$(get_session_name "$terminal_id")

    # Check if already running
    if is_session_running "$session_name"; then
        if [[ "$force" == "true" ]]; then
            echo -e "  ${YELLOW}$terminal_name ($terminal_id):${NC} Stopping existing session..."
            if [[ "$dry_run" != "true" ]]; then
                "$REPO_ROOT/.loom/scripts/agent-destroy.sh" "$terminal_id" --force 2>/dev/null || true
                sleep 1
            fi
        else
            echo -e "  ${GRAY}$terminal_name ($terminal_id):${NC} Already running, skipping"
            return 0
        fi
    fi

    if [[ "$dry_run" == "true" ]]; then
        echo -e "  ${CYAN}$terminal_name ($terminal_id):${NC} Would start (role: $role_file)"
        return 0
    fi

    echo -e "  ${GREEN}$terminal_name ($terminal_id):${NC} Starting..."

    # Derive role name from role file (e.g., "builder.md" -> "builder")
    local role_name=""
    if [[ -n "$role_file" && "$role_file" != "null" ]]; then
        role_name="${role_file%.md}"
    fi

    # Use agent-spawn.sh for consistent session creation
    local spawn_script="$REPO_ROOT/.loom/scripts/agent-spawn.sh"
    if [[ -x "$spawn_script" ]]; then
        "$spawn_script" --role "$role_name" --name "$terminal_id"
    else
        echo -e "    ${RED}Error: agent-spawn.sh not found${NC}"
        return 1
    fi

    return 0
}

# Main logic
main() {
    local only_role=""
    local dry_run=false
    local force=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                exit 0
                ;;
            --only)
                if [[ -z "${2:-}" ]]; then
                    echo -e "${RED}Error: --only requires a role name${NC}" >&2
                    exit 1
                fi
                only_role="$2"
                shift 2
                ;;
            --dry-run)
                dry_run=true
                shift
                ;;
            --yes|-y)
                # Accepted for compatibility but not currently used
                shift
                ;;
            --force)
                force=true
                shift
                ;;
            -*)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Use 'loom start --help' for usage" >&2
                exit 1
                ;;
            *)
                echo -e "${RED}Error: Unexpected argument '$1'${NC}" >&2
                exit 1
                ;;
        esac
    done

    # Check dependencies
    check_dependencies

    # Check config
    check_config

    # Parse terminals from config
    local terminals
    terminals=$(jq -c '.terminals // []' "$CONFIG_FILE")

    local terminal_count
    terminal_count=$(echo "$terminals" | jq 'length')

    if [[ "$terminal_count" -eq 0 ]]; then
        echo -e "${YELLOW}No terminals configured in $CONFIG_FILE${NC}"
        exit 0
    fi

    # Filter by role if specified
    if [[ -n "$only_role" ]]; then
        terminals=$(echo "$terminals" | jq -c "[.[] | select(.roleConfig.roleFile | test(\"$only_role\"; \"i\"))]")
        terminal_count=$(echo "$terminals" | jq 'length')

        if [[ "$terminal_count" -eq 0 ]]; then
            echo -e "${YELLOW}No terminals found matching role '$only_role'${NC}"
            exit 0
        fi
    fi

    # Display summary
    echo -e "${BOLD}Loom Agent Pool${NC}"
    echo ""
    echo -e "  Workspace: ${CYAN}$REPO_ROOT${NC}"
    echo -e "  Config: ${CYAN}$CONFIG_FILE${NC}"
    echo -e "  Agents: ${CYAN}$terminal_count${NC}"
    if [[ -n "$only_role" ]]; then
        echo -e "  Filter: ${CYAN}$only_role${NC}"
    fi
    echo ""

    # Show what will be started
    if [[ "$dry_run" == "true" ]]; then
        echo -e "${YELLOW}Dry run - showing what would be started:${NC}"
        echo ""
    else
        echo -e "${GREEN}Starting agents:${NC}"
        echo ""
    fi

    # Start each terminal
    local started=0
    local failed=0

    echo "$terminals" | jq -c '.[]' | while read -r terminal; do
        local id name role_file interval interval_prompt

        id=$(echo "$terminal" | jq -r '.id // ""')
        name=$(echo "$terminal" | jq -r '.name // .id')
        role_file=$(echo "$terminal" | jq -r '.roleConfig.roleFile // ""')
        interval=$(echo "$terminal" | jq -r '.roleConfig.targetInterval // 0')
        interval_prompt=$(echo "$terminal" | jq -r '.roleConfig.intervalPrompt // ""')

        if [[ -z "$id" ]]; then
            echo -e "  ${RED}Error: Terminal missing id field${NC}"
            continue
        fi

        if spawn_agent "$id" "$name" "$role_file" "$interval" "$interval_prompt" "$dry_run" "$force"; then
            ((started++)) || true
        else
            ((failed++)) || true
        fi
    done

    echo ""

    if [[ "$dry_run" == "true" ]]; then
        echo -e "${CYAN}Dry run complete. Use 'loom start' to actually start agents.${NC}"
    else
        echo -e "${GREEN}Agent pool started.${NC}"
        echo ""
        echo "Commands:"
        echo "  loom status      Show agent status"
        echo "  loom attach <id> Attach to agent terminal"
        echo "  loom logs <id>   View agent output"
        echo "  loom stop        Stop all agents"
    fi
}

main "$@"
