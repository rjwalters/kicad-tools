#!/usr/bin/env bash
# loom scale - Dynamic agent scaling (LEGACY: tmux Manual-Orchestration-Mode)
#
# DEPRECATED (#3811): this script scales tmux-session-based agents for the
# hands-on Manual Orchestration Mode. It is NOT the autonomous daemon's scaling
# mechanism. The Rust `loom-daemon` work finder now scales concurrent autonomous
# sweeps automatically, work-driven: min(token-pool size, disk headroom,
# LOOM_WORK_FINDER_MAX_CONCURRENT), recomputed every tick. To tune autonomous
# concurrency, set those env vars instead of using this script:
#   LOOM_WORK_FINDER=1                     Enable the daemon work-finder loop.
#   LOOM_WORK_FINDER_MAX_CONCURRENT=<N>    Operator ceiling on concurrent sweeps.
# See .loom/docs/daemon-reference.md -> "Autonomous work finder".
#
# The `shepherd` role no longer exists (removed in v0.10.0); use `builder` or
# another live role name if you drive tmux agents manually.
#
# Usage:
#   loom scale <role> <count>     Scale role to target count
#   loom scale --status           Show current scale
#   loom scale --help             Show help
#
# Examples:
#   loom scale builder 2          Scale builder agents to 2
#   loom scale builder 0          Stop all builder agents
#   loom scale judge 1            Scale judge agents to 1

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

# Source shared pipe-pane helper
source "$REPO_ROOT/.loom/scripts/lib/pipe-pane-cmd.sh"

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
${BOLD}loom scale - Dynamic agent scaling (LEGACY: tmux Manual Orchestration Mode)${NC}

${YELLOW}DEPRECATED (#3811):${NC}
    This scales tmux-session agents for hands-on Manual Orchestration Mode. It is
    NOT the autonomous daemon's scaling mechanism. The Rust loom-daemon work
    finder scales concurrent autonomous sweeps automatically, work-driven:
    min(token-pool size, disk headroom, LOOM_WORK_FINDER_MAX_CONCURRENT),
    recomputed every tick. To tune AUTONOMOUS concurrency, set:
        LOOM_WORK_FINDER=1                    Enable the daemon work-finder loop.
        LOOM_WORK_FINDER_MAX_CONCURRENT=<N>   Operator ceiling on concurrent sweeps.
    See .loom/docs/daemon-reference.md -> "Autonomous work finder".

${YELLOW}USAGE:${NC}
    loom scale <role> <count>     Scale role to target count
    loom scale --status           Show current scaling status
    loom scale --dry-run          Preview scaling changes
    loom scale --help             Show this help

${YELLOW}OPTIONS:${NC}
    --dry-run         Preview what would change without doing it
    --force           Force scale-down even if agents are working
    --status          Show current scale for each role

${YELLOW}EXAMPLES:${NC}
    loom scale builder 2          Scale builder agents to 2
    loom scale builder 0          Stop all builder agents
    loom scale judge 1            Scale judge agents to 1
    loom scale --status           Show current agent counts

${YELLOW}ROLES:${NC}
    Common roles: builder, judge, curator, champion, architect
    (the 'shepherd' role was removed in v0.10.0)

${YELLOW}HOW IT WORKS:${NC}
    Scale up:
      - Creates new tmux sessions for additional agents
      - Uses role template from existing config or defaults

    Scale down:
      - Gracefully stops excess agents (oldest first)
      - Warns if agents are actively working
      - Use --force to skip warnings
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

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "${RED}Error: Missing required dependencies: ${missing[*]}${NC}" >&2
        exit 1
    fi
}

# Get running sessions for a role
get_role_sessions() {
    local role="$1"
    tmux -L "$TMUX_SOCKET" list-sessions -F "#{session_name}" 2>/dev/null | grep "^loom-.*$role" || true
}

# Count running sessions for a role
count_role_sessions() {
    local role="$1"
    local count
    count=$(get_role_sessions "$role" | wc -l | tr -d ' ')
    echo "$count"
}

# Get highest numbered session for a role
get_max_role_number() {
    local role="$1"
    local max=0

    while read -r session; do
        if [[ -n "$session" ]]; then
            # Extract number from session name (e.g., loom-builder-3 -> 3)
            local num
            num=$(echo "$session" | grep -oE '[0-9]+$' || echo "0")
            if [[ "$num" -gt "$max" ]]; then
                max="$num"
            fi
        fi
    done < <(get_role_sessions "$role")

    echo "$max"
}

# Show current scaling status
show_status() {
    echo -e "${BOLD}Current Agent Scale${NC}"
    echo ""

    # Get all running loom sessions
    local sessions
    sessions=$(tmux -L "$TMUX_SOCKET" list-sessions -F "#{session_name}" 2>/dev/null | grep "^loom-" || true)

    if [[ -z "$sessions" ]]; then
        echo -e "${GRAY}No agents running${NC}"
        return 0
    fi

    # Count by role pattern (bash 3.2-compatible: no associative arrays)
    # Extract role names, sort and count with uniq -c, then display
    local role_summary
    role_summary=$(echo "$sessions" \
        | sed 's/^loom-//' \
        | sed 's/-[0-9]*$//' \
        | sort \
        | uniq -c)

    while read -r count role; do
        if [[ -n "$role" ]]; then
            echo -e "  ${CYAN}$role:${NC} $count"
        fi
    done <<< "$role_summary"

    echo ""
    echo -e "${GRAY}Total: $(echo "$sessions" | wc -l | tr -d ' ') session(s)${NC}"
}

# Spawn a new agent for a role
spawn_role_agent() {
    local role="$1"
    local number="$2"
    local dry_run="${3:-false}"

    local agent_id="$role-$number"
    local session_name="loom-$agent_id"
    local log_file="$LOG_DIR/loom-$agent_id.out"

    # Check if already running
    if tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
        echo -e "  ${GRAY}$agent_id:${NC} Already running, skipping"
        return 0
    fi

    if [[ "$dry_run" == "true" ]]; then
        echo -e "  ${CYAN}$agent_id:${NC} Would create"
        return 0
    fi

    echo -e "  ${GREEN}$agent_id:${NC} Creating..."

    # Create tmux session
    tmux -L "$TMUX_SOCKET" new-session -d -s "$session_name" -n "$role"

    # Set up output capture with ANSI stripping
    tmux -L "$TMUX_SOCKET" pipe-pane -t "$session_name" -o "$(pipe_pane_cmd "$log_file")"

    # Change to workspace
    tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "cd '$REPO_ROOT'" C-m

    # Start claude with role.  Prefer the resilient wrapper (claude-wrapper.sh)
    # so OAuth-token rotation, retry, and auth pre-flight all run consistently
    # with the Python spawn path in agent_spawn.py.  The wrapper exec's the
    # real `claude` binary; CLAUDE_CODE_OAUTH_TOKEN inherited from the parent
    # shell flows through naturally.  See issue #3236.
    local role_file="${role}.md"
    local role_path="$REPO_ROOT/.loom/roles/$role_file"
    local wrapper_path="$REPO_ROOT/.loom/scripts/claude-wrapper.sh"
    local claude_invocation
    if [[ -x "$wrapper_path" ]]; then
        claude_invocation="'$wrapper_path'"
    else
        claude_invocation="claude"
    fi

    if [[ -f "$role_path" ]]; then
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "$claude_invocation -p '/$role' --dangerously-skip-permissions" C-m
    else
        echo -e "    ${YELLOW}Warning: Role file not found: $role_file${NC}"
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "$claude_invocation --dangerously-skip-permissions" C-m
    fi

    return 0
}

# Stop an agent
stop_role_agent() {
    local session_name="$1"
    local force="${2:-false}"
    local dry_run="${3:-false}"

    if [[ "$dry_run" == "true" ]]; then
        echo -e "  ${YELLOW}$session_name:${NC} Would stop"
        return 0
    fi

    echo -e "  ${YELLOW}$session_name:${NC} Stopping..."

    if [[ "$force" == "true" ]]; then
        tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true
    else
        # Graceful stop
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" C-c 2>/dev/null || true
        sleep 1
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "exit" C-m 2>/dev/null || true
        sleep 2

        # Force kill if still running
        if tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
            tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true
        fi
    fi

    return 0
}

# Main logic
main() {
    local role=""
    local target=""
    local dry_run=false
    local force=false
    local show_status_flag=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                exit 0
                ;;
            --status)
                show_status_flag=true
                shift
                ;;
            --dry-run)
                dry_run=true
                shift
                ;;
            --force|-f)
                force=true
                shift
                ;;
            -*)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Use 'loom scale --help' for usage" >&2
                exit 1
                ;;
            *)
                if [[ -z "$role" ]]; then
                    role="$1"
                elif [[ -z "$target" ]]; then
                    target="$1"
                else
                    echo -e "${RED}Error: Too many arguments${NC}" >&2
                    exit 1
                fi
                shift
                ;;
        esac
    done

    # Check dependencies
    check_dependencies

    # Show status
    if [[ "$show_status_flag" == "true" ]]; then
        show_status
        exit 0
    fi

    # Validate arguments
    if [[ -z "$role" ]]; then
        echo -e "${RED}Error: Role name required${NC}" >&2
        echo "Usage: loom scale <role> <count>" >&2
        exit 1
    fi

    if [[ -z "$target" ]]; then
        echo -e "${RED}Error: Target count required${NC}" >&2
        echo "Usage: loom scale <role> <count>" >&2
        exit 1
    fi

    if ! [[ "$target" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}Error: Target must be a number${NC}" >&2
        exit 1
    fi

    # Get current count
    local current
    current=$(count_role_sessions "$role")

    echo -e "${BOLD}Scaling $role: $current -> $target${NC}"
    echo ""

    if [[ "$current" -eq "$target" ]]; then
        echo -e "${GRAY}Already at target scale${NC}"
        exit 0
    fi

    if [[ "$dry_run" == "true" ]]; then
        echo -e "${YELLOW}Dry run - showing what would change:${NC}"
        echo ""
    fi

    if [[ "$target" -gt "$current" ]]; then
        # Scale up
        local to_add=$((target - current))
        echo -e "${GREEN}Scaling up: +$to_add agent(s)${NC}"
        echo ""

        local max_num
        max_num=$(get_max_role_number "$role")

        for ((i = 1; i <= to_add; i++)); do
            local new_num=$((max_num + i))
            spawn_role_agent "$role" "$new_num" "$dry_run"
        done
    else
        # Scale down
        local to_remove=$((current - target))
        echo -e "${YELLOW}Scaling down: -$to_remove agent(s)${NC}"
        echo ""

        if [[ "$force" != "true" && "$dry_run" != "true" ]]; then
            echo -e "${YELLOW}Warning: This will stop $to_remove running agent(s)${NC}"
            echo -e "${GRAY}Use --force to skip this warning${NC}"
            echo ""
            read -r -p "Continue? [y/N] " -n 1 confirm
            echo ""
            if [[ ! $confirm =~ ^[Yy]$ ]]; then
                echo "Cancelled"
                exit 0
            fi
            echo ""
        fi

        # Get sessions to stop (oldest first, which typically have lowest numbers)
        local sessions_to_stop
        sessions_to_stop=$(get_role_sessions "$role" | sort | tail -n "$to_remove")

        while read -r session; do
            if [[ -n "$session" ]]; then
                stop_role_agent "$session" "$force" "$dry_run"
            fi
        done <<< "$sessions_to_stop"
    fi

    echo ""

    if [[ "$dry_run" == "true" ]]; then
        echo -e "${CYAN}Dry run complete. Remove --dry-run to apply changes.${NC}"
    else
        local new_count
        new_count=$(count_role_sessions "$role")
        echo -e "${GREEN}Scale complete: $role now at $new_count${NC}"
    fi
}

main "$@"
