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

_LOOM_START_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/config-resolver.sh
source "$_LOOM_START_SCRIPT_DIR/../lib/config-resolver.sh"

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
    - a resolved config with a non-empty terminals array must exist (.loom/config.json,
      .loom-project/project.json, or .loom-local/local.json)
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

# List of config tier paths (lowest to highest precedence), for diagnostics.
_loom_start_config_tiers() {
    local repo_root="$1"
    local defaults_path
    defaults_path="$(_loom_config_private_defaults_path)"
    [[ -n "$defaults_path" ]] && echo "$defaults_path"
    echo "$repo_root/$LOOM_CONFIG_LEGACY_REL"
    echo "$repo_root/$LOOM_CONFIG_PROJECT_REL"
    echo "$repo_root/$LOOM_CONFIG_LOCAL_REL"
}

# Check that a valid, non-empty effective config resolves for this workspace.
#
# Migrated onto the config-resolver tier chain (#4062). Two distinct failure
# modes are preserved, matching loom-daemon's handle_validate_command /
# role_validation.rs `has_terminals` precondition (#4059) so the Bash and Rust
# entry points agree:
#
#   1. MALFORMED TIER (regression guard): loom_resolve_config soft-fails a
#      malformed tier file to `{}` silently -- appropriate for the resolver's
#      general contract, but NOT here. A typo'd config must still fail loudly
#      (non-zero, explicit message) rather than silently degrading to "no
#      terminals configured" -- so every EXISTING tier file is validated as
#      parseable JSON before the merge is trusted.
#   2. NO TERMINALS ANYWHERE: retargeted from "the legacy .loom/config.json
#      file exists" to "the resolved config has a non-empty `terminals`
#      array" -- absence of the legacy file no longer implies absence of
#      config under tiering (terminals may come from
#      .loom-project/project.json alone). The exit behavior (non-zero) is
#      preserved; only the condition is retargeted.
#
# Sets EFFECTIVE_CONFIG (compact JSON) as a side effect for reuse by callers
# (Trap 2 -- resolve once, don't re-merge per key).
check_config() {
    local tier_path
    while IFS= read -r tier_path; do
        if [[ -f "$tier_path" ]] && ! jq empty "$tier_path" 2>/dev/null; then
            echo -e "${RED}Error: Invalid JSON in $tier_path${NC}" >&2
            exit 1
        fi
    done < <(_loom_start_config_tiers "$REPO_ROOT")

    EFFECTIVE_CONFIG="$(loom_resolve_config "$REPO_ROOT")"

    local has_terminals
    has_terminals=$(echo "$EFFECTIVE_CONFIG" | jq '(.terminals // []) | length > 0' 2>/dev/null || echo false)
    if [[ "$has_terminals" != "true" ]]; then
        echo -e "${RED}Error: No Loom config with a non-empty \`terminals\` array found in any tier.${NC}" >&2
        echo "" >&2
        echo "Searched (lowest to highest precedence):" >&2
        while IFS= read -r tier_path; do
            echo "  - $tier_path" >&2
        done < <(_loom_start_config_tiers "$REPO_ROOT")
        echo "" >&2
        # A SESSION MODE install (#8884) reaches this refusal BY DESIGN -- it
        # wrote `terminals: []` on purpose -- so the hint names both causes
        # rather than sending such an operator into a needless reinstall. It is
        # stated unconditionally, not behind a `.mode == "session"` test: this
        # file is `contract` shell in epic #7810's portable pool, which may not
        # grow, and a static line costs nothing while a branch costs 7 lines.
        # The refusal itself is unchanged -- #8884 deliberately added no second
        # guard, because this function already is the guard.
        echo "Have you initialized Loom here (./scripts/install-loom.sh)? A session-mode" >&2
        echo "install (\`\"mode\": \"session\"\`) lands here by design -- see .loom/docs/session-mode.md." >&2
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

    # Parse terminals from the effective config (EFFECTIVE_CONFIG was resolved
    # once by check_config() above via loom_resolve_config -- Trap 1/2: array
    # sites resolve once and reuse the merged JSON, never per-field
    # loom_config_get). check_config() already guarantees a non-empty
    # terminals array here (the hard-fail precondition it enforces), so no
    # redundant zero-count check is needed at this point.
    local terminals
    terminals=$(echo "$EFFECTIVE_CONFIG" | jq -c '.terminals // []')

    local terminal_count
    terminal_count=$(echo "$terminals" | jq 'length')

    # Filter by role if specified
    if [[ -n "$only_role" ]]; then
        terminals=$(echo "$terminals" | jq -c "[.[] | select(.roleConfig.roleFile | test(\"$only_role\"; \"i\"))]")
        terminal_count=$(echo "$terminals" | jq 'length')

        if [[ "$terminal_count" -eq 0 ]]; then
            echo -e "${YELLOW}No terminals found matching role '$only_role'${NC}"
            exit 0
        fi
    fi

    # Display summary. Under tiering, "Config:" names every tier that
    # actually exists on disk rather than a single legacy path -- naming only
    # .loom/config.json would be misleading (or falsely read as "none") once
    # a higher tier supplies the effective config (#4062).
    local config_tiers_present
    # `|| true`: under `set -o pipefail`, a while-loop's exit status is that
    # of the last command run in its final iteration -- if the LAST tier path
    # tested happens to not exist (the common case for .loom-local/local.json
    # and the private-defaults tier), `[[ -f "$t" ]]` is the last thing that
    # ran and its nonzero status would otherwise propagate through the
    # pipeline and trip `set -e`, aborting the script (#4062 regression).
    config_tiers_present=$(_loom_start_config_tiers "$REPO_ROOT" | while IFS= read -r t; do [[ -f "$t" ]] && echo "$t"; done | paste -sd ', ' - || true)
    echo -e "${BOLD}Loom Agent Pool${NC}"
    echo ""
    echo -e "  Workspace: ${CYAN}$REPO_ROOT${NC}"
    echo -e "  Config: ${CYAN}${config_tiers_present:-none found}${NC}"
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
