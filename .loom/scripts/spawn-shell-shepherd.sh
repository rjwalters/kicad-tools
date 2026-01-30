#!/usr/bin/env bash
# spawn-shell-shepherd.sh - Spawn a shell-based shepherd in a tmux session
#
# This script spawns the shell-based shepherd-loop.sh in a tmux session,
# providing the same interface as agent-spawn.sh but using the deterministic
# shell script instead of launching Claude Code with the /shepherd role.
#
# Usage:
#   spawn-shell-shepherd.sh <issue-number> [options]
#
# Options:
#   --force, -f     Auto-approve, resolve conflicts, auto-merge after approval
#   --name <name>   Session name (default: shepherd-issue-<N>)
#   --json          Output spawn result as JSON
#   --help          Show this help message
#
# Deprecated:
#   --force-pr      (deprecated) Now the default behavior
#   --force-merge   (deprecated) Use --force or -f instead
#   --wait          (deprecated) No longer blocks; shepherd always exits after PR approval
#
# The daemon can configure LOOM_SHELL_SHEPHERDS=true to use this script
# instead of agent-spawn.sh for shepherd spawning.
#
# Example:
#   spawn-shell-shepherd.sh 42 --json            # Default: exit after PR approval
#   spawn-shell-shepherd.sh 42 --force --json    # Full automation with auto-merge

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────

TMUX_SOCKET="loom"
SESSION_PREFIX="loom-"

# ─── Colors ───────────────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

# ─── Helpers ──────────────────────────────────────────────────────────────────

log_info() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*" >&2; }
log_success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*" >&2; }
# shellcheck disable=SC2329  # Standard logging helper, may be used in future
log_warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $*" >&2; }
log_error() { echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $*" >&2; }

# Find the repository root (works from any subdirectory including worktrees)
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            if [[ -f "$dir/.git" ]]; then
                local gitdir
                gitdir=$(cat "$dir/.git" | sed 's/^gitdir: //')
                local main_repo
                main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
                if [[ -d "$main_repo/.loom" ]]; then
                    echo "$main_repo"
                    return 0
                fi
            fi
            if [[ -d "$dir/.loom" ]]; then
                echo "$dir"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

show_help() {
    cat <<EOF
${BLUE}spawn-shell-shepherd.sh - Spawn shell-based shepherd in tmux${NC}

${YELLOW}USAGE:${NC}
    spawn-shell-shepherd.sh <issue-number> [OPTIONS]

${YELLOW}OPTIONS:${NC}
    --force, -f     Auto-approve, resolve conflicts, auto-merge after approval
    --name <name>   Session name (default: shepherd-issue-<N>)
    --json          Output spawn result as JSON
    --help          Show this help message

${YELLOW}DEPRECATED:${NC}
    --force-pr      (deprecated) Now the default behavior
    --force-merge   (deprecated) Use --force or -f instead
    --wait          (deprecated) No longer blocks; shepherd always exits after PR approval

${YELLOW}EXAMPLES:${NC}
    # Spawn with default behavior (exit after PR approval)
    spawn-shell-shepherd.sh 42

    # Spawn with full automation (auto-merge)
    spawn-shell-shepherd.sh 42 --force

    # Spawn with custom name and JSON output
    spawn-shell-shepherd.sh 42 --name shepherd-1 --json

${YELLOW}TMUX SESSION:${NC}
    Session: ${SESSION_PREFIX}<name>
    Attach: tmux -L $TMUX_SOCKET attach -t ${SESSION_PREFIX}<name>
    Logs: .loom/logs/${SESSION_PREFIX}<name>.log

EOF
}

# Check if session exists
session_exists() {
    local session_name="$1"
    tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null
}

# ─── Parse arguments ──────────────────────────────────────────────────────────

ISSUE=""
MODE=""
SESSION_NAME=""
JSON_OUTPUT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f)
            MODE="--force"
            shift
            ;;
        --wait)
            # Deprecated: --wait used to block indefinitely at the merge gate.
            log_warn "Flag --wait is deprecated (shepherd always exits after PR approval)"
            MODE="--wait"
            shift
            ;;
        --force-pr)
            # Deprecated: now the default behavior
            log_warn "Flag --force-pr is deprecated (now default behavior)"
            MODE=""  # Default, no flag needed
            shift
            ;;
        --force-merge)
            # Deprecated: use --force or -f instead
            log_warn "Flag --force-merge is deprecated (use --force or -f instead)"
            MODE="--force"
            shift
            ;;
        --name)
            SESSION_NAME="$2"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        -*)
            log_error "Unknown option: $1"
            exit 1
            ;;
        *)
            if [[ -z "$ISSUE" ]]; then
                ISSUE="$1"
            else
                log_error "Unexpected argument: $1"
                exit 1
            fi
            shift
            ;;
    esac
done

# Validate issue number
if [[ -z "$ISSUE" ]]; then
    log_error "Issue number required"
    echo ""
    show_help
    exit 1
fi

if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
    log_error "Issue number must be numeric, got '$ISSUE'"
    exit 1
fi

# Default session name
if [[ -z "$SESSION_NAME" ]]; then
    SESSION_NAME="shepherd-issue-${ISSUE}"
fi

# Find repository root
REPO_ROOT=$(find_repo_root) || {
    log_error "Not in a Loom-enabled repository"
    exit 1
}

# ─── Main ─────────────────────────────────────────────────────────────────────

FULL_SESSION_NAME="${SESSION_PREFIX}${SESSION_NAME}"
LOG_FILE="$REPO_ROOT/.loom/logs/${FULL_SESSION_NAME}.log"
SHEPHERD_SCRIPT="$REPO_ROOT/.loom/scripts/shepherd-loop.sh"

# Check if shepherd script exists
if [[ ! -x "$SHEPHERD_SCRIPT" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"status":"error","error":"shepherd-loop.sh not found or not executable"}'
    else
        log_error "shepherd-loop.sh not found at $SHEPHERD_SCRIPT"
    fi
    exit 1
fi

# Check if session already exists
if session_exists "$FULL_SESSION_NAME"; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo "{\"status\":\"exists\",\"name\":\"$SESSION_NAME\",\"session\":\"$FULL_SESSION_NAME\"}"
    else
        log_success "Session already exists: $FULL_SESSION_NAME"
        log_info "Attach: tmux -L $TMUX_SOCKET attach -t $FULL_SESSION_NAME"
    fi
    exit 0
fi

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

# Initialize log file
cat > "$LOG_FILE" <<EOF
# Shell Shepherd Log
# Session: $FULL_SESSION_NAME
# Issue: $ISSUE
# Mode: ${MODE:-default}
# Started: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
# ---
EOF

log_info "Creating tmux session: $FULL_SESSION_NAME"
log_info "Log file: $LOG_FILE"

# Build the shepherd command
SHEPHERD_CMD="$SHEPHERD_SCRIPT $ISSUE"
if [[ -n "$MODE" ]]; then
    SHEPHERD_CMD="$SHEPHERD_CMD $MODE"
fi

# Redirect stderr to stdout (logging handled by pipe-pane below)
SHEPHERD_CMD="$SHEPHERD_CMD 2>&1"

# Create new detached session with working directory
if ! tmux -L "$TMUX_SOCKET" new-session -d -s "$FULL_SESSION_NAME" -c "$REPO_ROOT"; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"status":"error","error":"failed to create tmux session"}'
    else
        log_error "Failed to create tmux session: $FULL_SESSION_NAME"
    fi
    exit 1
fi

# Set up output capture via pipe-pane
tmux -L "$TMUX_SOCKET" pipe-pane -t "$FULL_SESSION_NAME" "cat >> '$LOG_FILE'" 2>/dev/null || true

# Set environment variables for the session
tmux -L "$TMUX_SOCKET" set-environment -t "$FULL_SESSION_NAME" LOOM_TERMINAL_ID "$SESSION_NAME"
tmux -L "$TMUX_SOCKET" set-environment -t "$FULL_SESSION_NAME" LOOM_WORKSPACE "$REPO_ROOT"
tmux -L "$TMUX_SOCKET" set-environment -t "$FULL_SESSION_NAME" LOOM_ROLE "shepherd-sh"
tmux -L "$TMUX_SOCKET" set-environment -t "$FULL_SESSION_NAME" LOOM_ISSUE "$ISSUE"
tmux -L "$TMUX_SOCKET" set-environment -t "$FULL_SESSION_NAME" LOOM_ON_DEMAND "true"

# Send the shepherd command to the session
tmux -L "$TMUX_SOCKET" send-keys -t "$FULL_SESSION_NAME" "$SHEPHERD_CMD" C-m

if [[ "$JSON_OUTPUT" == "true" ]]; then
    echo "{\"status\":\"started\",\"name\":\"$SESSION_NAME\",\"session\":\"$FULL_SESSION_NAME\",\"log\":\"$LOG_FILE\"}"
else
    log_success "Shell shepherd spawned successfully"
    log_info ""
    log_info "Session: $FULL_SESSION_NAME"
    log_info "Attach:  tmux -L $TMUX_SOCKET attach -t $FULL_SESSION_NAME"
    log_info "Logs:    tail -f $LOG_FILE"
fi

exit 0
