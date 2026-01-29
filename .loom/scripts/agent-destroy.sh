#!/bin/bash
# agent-destroy.sh - Clean up a tmux agent session and its resources
#
# Destroys a tmux session and optionally cleans up its worktree.
# Designed for ephemeral on-demand workers spawned by agent-spawn.sh.
#
# Usage:
#   agent-destroy.sh <name> [--clean-worktree] [--force] [--json]
#
# Examples:
#   agent-destroy.sh builder-issue-42
#   agent-destroy.sh builder-issue-42 --clean-worktree
#   agent-destroy.sh builder-issue-42 --force --json

set -euo pipefail

TMUX_SOCKET="loom"
SESSION_PREFIX="loom-"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*" >&2; }
log_success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*" >&2; }
# shellcheck disable=SC2329  # log_warn kept for API consistency with other scripts
log_warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $*" >&2; }
log_error() { echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $*" >&2; }

# shellcheck disable=SC2120
find_repo_root() {
    local dir="${1:-$PWD}"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

show_help() {
    cat <<EOF
${BLUE}agent-destroy.sh - Clean up a tmux agent session${NC}

${YELLOW}USAGE:${NC}
    agent-destroy.sh <name> [OPTIONS]

${YELLOW}OPTIONS:${NC}
    --clean-worktree    Also remove the git worktree (if session has LOOM_WORKSPACE set)
    --force             Kill session immediately (no graceful shutdown attempt)
    --json              Output result as JSON
    --help              Show this help message

${YELLOW}EXAMPLES:${NC}
    agent-destroy.sh builder-issue-42
    agent-destroy.sh builder-issue-42 --clean-worktree --json

EOF
}

main() {
    if [[ $# -lt 1 ]]; then
        show_help
        exit 1
    fi

    local name="$1"
    shift

    local clean_worktree=false
    local force=false
    local json_output=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --clean-worktree) clean_worktree=true; shift ;;
            --force) force=true; shift ;;
            --json) json_output=true; shift ;;
            --help|-h) show_help; exit 0 ;;
            *) log_error "Unknown argument: $1"; exit 1 ;;
        esac
    done

    local session_name="${SESSION_PREFIX}${name}"
    local worktree_path=""
    local session_existed=false

    # Get worktree path before destroying session
    if tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
        session_existed=true
        worktree_path=$(tmux -L "$TMUX_SOCKET" show-environment -t "$session_name" LOOM_WORKSPACE 2>/dev/null | sed 's/^LOOM_WORKSPACE=//' || true)

        if [[ "$force" == "true" ]]; then
            tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true
        else
            # Graceful: send Ctrl-C then exit
            tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" C-c 2>/dev/null || true
            sleep 1
            tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "exit" C-m 2>/dev/null || true
            sleep 2
            # Force kill if still alive
            if tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
                tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true
            fi
        fi
        log_success "Destroyed session: $session_name"
    else
        log_info "Session not found: $session_name (already destroyed)"
    fi

    # Clean worktree if requested
    local worktree_cleaned=false
    if [[ "$clean_worktree" == "true" ]] && [[ -n "$worktree_path" ]] && [[ -d "$worktree_path" ]]; then
        local repo_root
        if repo_root=$(find_repo_root); then
            # Only clean if it's actually a worktree (not the main repo)
            if [[ "$worktree_path" != "$repo_root" ]] && [[ "$worktree_path" == *".loom/worktrees/"* ]]; then
                log_info "Removing worktree: $worktree_path"
                git -C "$repo_root" worktree remove "$worktree_path" --force 2>/dev/null || true
                worktree_cleaned=true
                log_success "Removed worktree: $worktree_path"
            fi
        fi
    fi

    if [[ "$json_output" == "true" ]]; then
        echo "{\"status\":\"destroyed\",\"name\":\"$name\",\"session\":\"$session_name\",\"session_existed\":$session_existed,\"worktree_cleaned\":$worktree_cleaned}"
    fi

    exit 0
}

main "$@"
