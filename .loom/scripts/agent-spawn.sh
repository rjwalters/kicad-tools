#!/bin/bash
# agent-spawn.sh - Spawn Claude Code CLI agents in tmux sessions
#
# This script provides the atomic building block for tmux-based agent management,
# enabling persistent, inspectable, and interactive Claude Code agents that can
# be spawned programmatically from the CLI.
#
# Features:
# - Creates tmux sessions with predictable names (loom-<name>)
# - Uses shared tmux socket (-L loom) for unified session visibility
# - Captures all output to .loom/logs/<session-name>.log
# - Integrates with signal.sh for graceful shutdown
# - Wraps Claude CLI with claude-wrapper.sh for resilience
# - Supports git worktrees for isolated development
#
# Usage:
#   agent-spawn.sh --role <role> --name <name> [--args "<args>"] [--worktree <path>]
#   agent-spawn.sh --role <role> --name <name> --on-demand [--wait [--timeout <s>]]
#   agent-spawn.sh --check <name>
#   agent-spawn.sh --help
#
# Examples:
#   # Spawn a shepherd agent for issue 42
#   agent-spawn.sh --role shepherd --args "42 --force" --name shepherd-1
#
#   # Spawn a builder agent in a worktree
#   agent-spawn.sh --role builder --args "42" --name builder-1 --worktree .loom/worktrees/issue-42
#
#   # Spawn an ephemeral on-demand worker and wait for completion
#   agent-spawn.sh --role builder --name builder-issue-42 --args "42" --on-demand --wait --timeout 1800
#
#   # Check if a session exists
#   agent-spawn.sh --check shepherd-1
#
#   # Attach to a running session
#   tmux -L loom attach -t loom-shepherd-1

set -euo pipefail

# Configuration
TMUX_SOCKET="loom"
SESSION_PREFIX="loom-"
STUCK_SESSION_THRESHOLD_SECONDS=${LOOM_STUCK_SESSION_THRESHOLD:-300}  # 5 minutes

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging helpers
log_info() {
    echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*" >&2
}

log_success() {
    echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*" >&2
}

log_warn() {
    echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $*" >&2
}

log_error() {
    echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $*" >&2
}

# Find the repository root (works from any subdirectory)
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

# Show help
show_help() {
    cat <<EOF
${BLUE}agent-spawn.sh - Spawn Claude Code CLI agents in tmux sessions${NC}

${YELLOW}USAGE:${NC}
    agent-spawn.sh --role <role> --name <name> [OPTIONS]
    agent-spawn.sh --check <name>
    agent-spawn.sh --list
    agent-spawn.sh --help

${YELLOW}OPTIONS:${NC}
    --role <role>       Role name (shepherd, builder, judge, etc.)
                        Maps to .loom/roles/<role>.md or .claude/commands/<role>.md
    --name <name>       Session identifier (used in tmux session name: loom-<name>)
    --args "<args>"     Arguments to pass to the role slash command
    --worktree <path>   Path to git worktree (agent runs in isolated worktree)
    --on-demand         Mark session as ephemeral (for agent-destroy.sh cleanup)
    --fresh             Force new session even if one already exists (kills stuck sessions)
    --wait              Block until agent completes (requires agent-wait.sh)
    --timeout <seconds> Timeout for --wait (default: 3600)
    --json              Output spawn result as JSON
    --check <name>      Check if session exists (exit 0 if yes, 1 if no)
    --list              List all active loom-agent sessions
    --help              Show this help message

${YELLOW}EXAMPLES:${NC}
    # Spawn a shepherd agent for issue 42
    agent-spawn.sh --role shepherd --args "42 --force" --name shepherd-1

    # Spawn a builder agent in a worktree
    agent-spawn.sh --role builder --args "42" --name builder-1 \\
        --worktree .loom/worktrees/issue-42

    # Spawn a support role (judge, champion, etc.) from main repo
    agent-spawn.sh --role judge --name judge-1

    # Spawn ephemeral worker, wait for completion, get JSON result
    agent-spawn.sh --role builder --name builder-issue-42 --args "42" \\
        --worktree .loom/worktrees/issue-42 --on-demand --wait --timeout 1800 --json

    # Check if a session exists
    agent-spawn.sh --check shepherd-1

    # List all sessions
    agent-spawn.sh --list

    # Attach to a running session
    tmux -L loom attach -t loom-shepherd-1

    # Stop a session gracefully
    ./.loom/scripts/signal.sh stop shepherd-1

${YELLOW}ENVIRONMENT:${NC}
    LOOM_SPAWN_VERIFY_TIMEOUT - Timeout for process existence check in seconds (default: 10)
    LOOM_STUCK_SESSION_THRESHOLD - Seconds before idle session is considered stuck (default: 300)

${YELLOW}TMUX ARCHITECTURE:${NC}
    Socket: -L loom (shared with CLI tools for unified visibility)
    Session naming: loom-<name> where <name> is the --name parameter
    Output capture: .loom/logs/<session-name>.log via pipe-pane

${YELLOW}SIGNAL FILES:${NC}
    .loom/stop-daemon           - Global stop (all agents)
    .loom/stop-shepherds        - Stop all shepherd agents
    .loom/signals/stop-<name>   - Stop specific agent by name

EOF
}

# Validate tmux is installed
check_tmux() {
    if ! command -v tmux &>/dev/null; then
        log_error "tmux is not installed"
        log_info "Install with: brew install tmux (macOS) or apt-get install tmux (Linux)"
        return 1
    fi

    # Check tmux version for pipe-pane support (requires >= 1.8)
    local version
    version=$(tmux -V 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
    if [[ -n "$version" ]]; then
        local major minor
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [[ "$major" -lt 1 ]] || { [[ "$major" -eq 1 ]] && [[ "$minor" -lt 8 ]]; }; then
            log_warn "tmux version $version may not support all features (recommend >= 1.8)"
        fi
    fi

    return 0
}

# Validate Claude CLI is available
check_claude_cli() {
    if ! command -v claude &>/dev/null; then
        log_error "Claude CLI not found in PATH"
        log_info "Install with: npm install -g @anthropic-ai/claude-code"
        return 1
    fi
    return 0
}

# Validate role exists
validate_role() {
    local role="$1"
    local repo_root="$2"

    # Check .loom/roles/<role>.md first (may be symlink)
    local role_file="${repo_root}/.loom/roles/${role}.md"
    if [[ -f "$role_file" ]] || [[ -L "$role_file" ]]; then
        return 0
    fi

    # Check .claude/commands/<role>.md as fallback
    role_file="${repo_root}/.claude/commands/${role}.md"
    if [[ -f "$role_file" ]]; then
        return 0
    fi

    log_error "Role not found: $role"
    log_info "Expected at: ${repo_root}/.loom/roles/${role}.md"
    log_info "         or: ${repo_root}/.claude/commands/${role}.md"
    log_info ""
    log_info "Available roles:"
    # List available roles
    if [[ -d "${repo_root}/.loom/roles" ]]; then
        for f in "${repo_root}/.loom/roles/"*.md; do
            if [[ -f "$f" ]] || [[ -L "$f" ]]; then
                local name
                name=$(basename "$f" .md)
                if [[ "$name" != "README" ]]; then
                    log_info "  - $name"
                fi
            fi
        done
    fi
    return 1
}

# Validate worktree path
validate_worktree() {
    local worktree_path="$1"

    if [[ ! -d "$worktree_path" ]]; then
        log_error "Worktree path does not exist: $worktree_path"
        return 1
    fi

    # Check if it's a valid git repository (main or worktree)
    if ! git -C "$worktree_path" rev-parse --git-dir &>/dev/null; then
        log_error "Not a valid git repository: $worktree_path"
        return 1
    fi

    return 0
}

# Check if session exists
session_exists() {
    local name="$1"
    local session_name="${SESSION_PREFIX}${name}"

    tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null
}

# Check if session is alive (has windows and panes)
session_is_alive() {
    local name="$1"
    local session_name="${SESSION_PREFIX}${name}"

    # Check if session exists and has at least one window
    local window_count
    window_count=$(tmux -L "$TMUX_SOCKET" list-windows -t "$session_name" 2>/dev/null | wc -l | tr -d ' ')

    [[ "$window_count" -gt 0 ]]
}

# Clean up dead session
cleanup_dead_session() {
    local name="$1"
    local session_name="${SESSION_PREFIX}${name}"

    log_info "Cleaning up dead session: $session_name"
    tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true
}

# Check if an existing session is stuck (idle with no claude activity)
# Returns 0 if stuck, 1 if healthy
session_is_stuck() {
    local name="$1"
    local repo_root="$2"
    local session_name="${SESSION_PREFIX}${name}"
    local log_file="${repo_root}/.loom/logs/${session_name}.log"

    # Check 1: Is claude actually running in this session?
    local shell_pid
    shell_pid=$(tmux -L "$TMUX_SOCKET" list-panes -t "$session_name" -F '#{pane_pid}' 2>/dev/null | head -1)
    if [[ -z "$shell_pid" ]]; then
        log_warn "Session has no shell PID - considered stuck"
        return 0
    fi

    local claude_running=false
    if pgrep -P "$shell_pid" -f "claude" >/dev/null 2>&1; then
        claude_running=true
    else
        # Check grandchildren (claude-wrapper.sh -> claude)
        local children
        children=$(pgrep -P "$shell_pid" 2>/dev/null || true)
        for child in $children; do
            if pgrep -P "$child" -f "claude" >/dev/null 2>&1; then
                claude_running=true
                break
            fi
        done
    fi

    if [[ "$claude_running" != "true" ]]; then
        log_warn "No claude process found in session - considered stuck"
        return 0
    fi

    # Check 2: Has the log file been written to recently?
    if [[ -f "$log_file" ]]; then
        local now
        now=$(date +%s)
        local log_mtime
        # macOS stat syntax
        if stat -f '%m' "$log_file" >/dev/null 2>&1; then
            log_mtime=$(stat -f '%m' "$log_file")
        else
            # Linux stat syntax
            log_mtime=$(stat -c '%Y' "$log_file")
        fi
        local idle_seconds=$((now - log_mtime))

        if [[ "$idle_seconds" -ge "$STUCK_SESSION_THRESHOLD_SECONDS" ]]; then
            log_warn "Session log idle for ${idle_seconds}s (threshold: ${STUCK_SESSION_THRESHOLD_SECONDS}s)"

            # Check 3: Look for progress milestones as a secondary signal
            local progress_dir="${repo_root}/.loom/progress"
            local has_recent_milestone=false
            if [[ -d "$progress_dir" ]]; then
                for pfile in "$progress_dir"/shepherd-*.json; do
                    [[ -f "$pfile" ]] || continue
                    local pfile_mtime
                    if stat -f '%m' "$pfile" >/dev/null 2>&1; then
                        pfile_mtime=$(stat -f '%m' "$pfile")
                    else
                        pfile_mtime=$(stat -c '%Y' "$pfile")
                    fi
                    local pfile_age=$((now - pfile_mtime))
                    if [[ "$pfile_age" -lt "$STUCK_SESSION_THRESHOLD_SECONDS" ]]; then
                        has_recent_milestone=true
                        break
                    fi
                done
            fi

            if [[ "$has_recent_milestone" == "true" ]]; then
                log_info "Recent progress milestone found - session may still be active"
                return 1  # Not stuck
            fi

            return 0  # Stuck
        fi
    fi

    # Session appears healthy
    return 1
}

# Kill a stuck session and clean up
kill_stuck_session() {
    local name="$1"
    local session_name="${SESSION_PREFIX}${name}"

    log_warn "Killing stuck session: $session_name"

    # Attempt graceful shutdown first
    tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" C-c 2>/dev/null || true
    sleep 1

    # Force kill
    tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true

    log_success "Stuck session killed: $session_name"
}

# List all loom-agent sessions
list_sessions() {
    if ! tmux -L "$TMUX_SOCKET" list-sessions 2>/dev/null; then
        log_info "No active loom-agent sessions"
        return 0
    fi
}

# Check for stop signals before spawning
check_stop_signals() {
    local name="$1"
    local repo_root="$2"

    # Global stop signal
    if [[ -f "${repo_root}/.loom/stop-daemon" ]]; then
        log_warn "Global stop signal exists (.loom/stop-daemon) - not spawning"
        return 1
    fi

    # Check shepherd-specific stop signal for shepherd roles
    if [[ "$name" == shepherd-* ]] && [[ -f "${repo_root}/.loom/stop-shepherds" ]]; then
        log_warn "Shepherd stop signal exists (.loom/stop-shepherds) - not spawning"
        return 1
    fi

    # Per-agent stop signal
    if [[ -f "${repo_root}/.loom/signals/stop-${name}" ]]; then
        log_warn "Agent stop signal exists (.loom/signals/stop-${name}) - not spawning"
        return 1
    fi

    return 0
}

# Ensure log directory exists
ensure_log_directory() {
    local repo_root="$1"
    local log_dir="${repo_root}/.loom/logs"

    if [[ ! -d "$log_dir" ]]; then
        mkdir -p "$log_dir"
        log_info "Created log directory: $log_dir"
    fi
}

# Spawn the agent
spawn_agent() {
    local role="$1"
    local name="$2"
    local args="${3:-}"
    local worktree="${4:-}"
    local repo_root="$5"

    local session_name="${SESSION_PREFIX}${name}"
    local log_file="${repo_root}/.loom/logs/${session_name}.log"
    local working_dir="$repo_root"

    # Use worktree as working directory if specified
    if [[ -n "$worktree" ]]; then
        # Convert to absolute path if relative
        if [[ "$worktree" != /* ]]; then
            worktree="${repo_root}/${worktree}"
        fi
        working_dir="$worktree"
    fi

    # Ensure log directory exists
    ensure_log_directory "$repo_root"

    # Clear previous log file if it exists (new session = new log)
    if [[ -f "$log_file" ]]; then
        # Rotate old log
        local timestamp
        timestamp=$(date '+%Y%m%d-%H%M%S')
        mv "$log_file" "${log_file%.log}.${timestamp}.log" 2>/dev/null || true
        log_info "Rotated previous log file"
    fi

    # Initialize new log file with header
    cat > "$log_file" <<EOF
# Loom Agent Log
# Session: $session_name
# Role: $role
# Args: $args
# Working Directory: $working_dir
# Started: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
# ---
EOF

    log_info "Creating tmux session: $session_name"
    log_info "Working directory: $working_dir"
    log_info "Log file: $log_file"

    # Start tmux server if not running (use new-session with detach)
    # The server starts automatically on first command, but we ensure it here

    # Create new detached session with working directory
    if ! tmux -L "$TMUX_SOCKET" new-session -d -s "$session_name" -c "$working_dir"; then
        log_error "Failed to create tmux session: $session_name"
        return 1
    fi

    # Set up output capture via pipe-pane
    # This captures all terminal output to the log file
    if ! tmux -L "$TMUX_SOCKET" pipe-pane -t "$session_name" "cat >> '$log_file'"; then
        log_warn "Failed to set up output capture (continuing anyway)"
    fi

    # Set environment variables for the session
    tmux -L "$TMUX_SOCKET" set-environment -t "$session_name" LOOM_TERMINAL_ID "$name"
    tmux -L "$TMUX_SOCKET" set-environment -t "$session_name" LOOM_WORKSPACE "$working_dir"
    tmux -L "$TMUX_SOCKET" set-environment -t "$session_name" LOOM_ROLE "$role"

    # Build the role slash command to pass as initial prompt
    local role_cmd="/${role}"
    if [[ -n "$args" ]]; then
        role_cmd="${role_cmd} ${args}"
    fi

    # Build the Claude CLI command with the role command as initial prompt
    # This eliminates TUI timing issues by passing the command at launch
    # instead of sending it via tmux after the TUI is ready.
    # See: https://github.com/rjwalters/loom/issues/1559
    local claude_cmd
    local wrapper_script="${repo_root}/.loom/scripts/claude-wrapper.sh"

    if [[ -x "$wrapper_script" ]]; then
        # Export environment variables for the wrapper
        # Quote the role_cmd to preserve spaces in args
        claude_cmd="LOOM_TERMINAL_ID='$name' LOOM_WORKSPACE='$working_dir' '$wrapper_script' --dangerously-skip-permissions \"$role_cmd\""
    else
        claude_cmd="claude --dangerously-skip-permissions \"$role_cmd\""
        log_warn "claude-wrapper.sh not found, using claude directly (no retry logic)"
    fi

    # Send the Claude CLI command with initial prompt to the session
    log_info "Starting Claude CLI with command: $role_cmd"
    tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "$claude_cmd" C-m

    # Verify spawn succeeded by checking process existence.
    # We do NOT poll for visible output (spinners, etc.) because Claude CLI
    # startup time is highly variable (network, model load, context size).
    # The agent-wait script handles all timing concerns including slow starts.
    #
    # Spawn verification checks:
    # 1. tmux session still exists
    # 2. Shell has started a child process (claude-wrapper or claude)
    #
    # This separates concerns:
    # - spawn = session created, process started (fast, reliable)
    # - wait = handle startup, processing, completion (agent-wait-bg.sh)

    local verify_timeout="${LOOM_SPAWN_VERIFY_TIMEOUT:-10}"  # Time to wait for process to start
    local elapsed=0

    log_info "Verifying Claude process started (up to ${verify_timeout}s)..."

    while [[ $elapsed -lt $verify_timeout ]]; do
        # Check session still exists
        if ! tmux -L "$TMUX_SOCKET" has-session -t "$session_name" 2>/dev/null; then
            log_error "tmux session disappeared: $session_name"
            return 1
        fi

        # Get the shell PID from the tmux pane
        local shell_pid
        shell_pid=$(tmux -L "$TMUX_SOCKET" list-panes -t "$session_name" -F '#{pane_pid}' 2>/dev/null | head -1)

        if [[ -n "$shell_pid" ]]; then
            # Check if claude process is running (child or grandchild of shell)
            local claude_running=false

            # Direct child: shell -> claude or shell -> claude-wrapper
            if pgrep -P "$shell_pid" -f "claude" >/dev/null 2>&1; then
                claude_running=true
            else
                # Grandchild: shell -> claude-wrapper -> claude
                local children
                children=$(pgrep -P "$shell_pid" 2>/dev/null || true)
                for child in $children; do
                    if pgrep -P "$child" -f "claude" >/dev/null 2>&1; then
                        claude_running=true
                        break
                    fi
                done
            fi

            if [[ "$claude_running" == "true" ]]; then
                log_info "Claude process detected after ${elapsed}s"
                break
            fi
        fi

        sleep 1
        elapsed=$((elapsed + 1))
    done

    # Verify we found the claude process
    if [[ $elapsed -ge $verify_timeout ]]; then
        log_error "Claude process not detected within ${verify_timeout}s"
        log_error "Session: $session_name"
        log_error "The tmux session exists but no claude process is running."
        log_error "Check: tmux -L $TMUX_SOCKET attach -t $session_name"
        # Don't kill the session - leave it for debugging
        return 1
    fi

    log_success "Agent spawned successfully"
    log_info ""
    log_info "Session: $session_name"
    log_info "Attach:  tmux -L $TMUX_SOCKET attach -t $session_name"
    log_info "Logs:    tail -f $log_file"
    log_info "Stop:    ./.loom/scripts/signal.sh stop $name"

    return 0
}

# Main entry point
main() {
    local role=""
    local name=""
    local args=""
    local worktree=""
    local check_name=""
    local do_list=false
    local on_demand=false
    local fresh=false
    local do_wait=false
    local wait_timeout=3600
    local json_output=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --role)
                role="$2"
                shift 2
                ;;
            --name)
                name="$2"
                shift 2
                ;;
            --args)
                args="$2"
                shift 2
                ;;
            --worktree)
                worktree="$2"
                shift 2
                ;;
            --on-demand)
                on_demand=true
                shift
                ;;
            --fresh)
                fresh=true
                shift
                ;;
            --wait)
                do_wait=true
                shift
                ;;
            --timeout)
                wait_timeout="$2"
                shift 2
                ;;
            --json)
                json_output=true
                shift
                ;;
            --check)
                check_name="$2"
                shift 2
                ;;
            --list)
                do_list=true
                shift
                ;;
            --help|-h|help)
                show_help
                exit 0
                ;;
            *)
                log_error "Unknown argument: $1"
                log_info "Run 'agent-spawn.sh --help' for usage"
                exit 1
                ;;
        esac
    done

    # Handle --list
    if [[ "$do_list" == "true" ]]; then
        list_sessions
        exit 0
    fi

    # Handle --check
    if [[ -n "$check_name" ]]; then
        if session_exists "$check_name"; then
            log_success "Session exists: ${SESSION_PREFIX}${check_name}"
            exit 0
        else
            log_info "Session does not exist: ${SESSION_PREFIX}${check_name}"
            exit 1
        fi
    fi

    # Validate required parameters for spawn
    if [[ -z "$role" ]]; then
        log_error "Missing required parameter: --role"
        log_info "Run 'agent-spawn.sh --help' for usage"
        exit 1
    fi

    if [[ -z "$name" ]]; then
        log_error "Missing required parameter: --name"
        log_info "Run 'agent-spawn.sh --help' for usage"
        exit 1
    fi

    # Find repository root
    local repo_root
    if ! repo_root=$(find_repo_root); then
        log_error "Not in a git repository"
        exit 1
    fi

    # Run validations
    if ! check_tmux; then
        exit 1
    fi

    if ! check_claude_cli; then
        exit 1
    fi

    if ! validate_role "$role" "$repo_root"; then
        exit 1
    fi

    if [[ -n "$worktree" ]]; then
        # Convert to absolute path for validation
        local abs_worktree
        if [[ "$worktree" != /* ]]; then
            abs_worktree="${repo_root}/${worktree}"
        else
            abs_worktree="$worktree"
        fi

        if ! validate_worktree "$abs_worktree"; then
            exit 1
        fi
    fi

    # Check for stop signals before spawning
    if ! check_stop_signals "$name" "$repo_root"; then
        exit 1
    fi

    # Handle idempotency - check if session already exists
    if session_exists "$name"; then
        if [[ "$fresh" == "true" ]]; then
            log_info "Fresh session requested - killing existing session: ${SESSION_PREFIX}${name}"
            kill_stuck_session "$name"
        elif session_is_alive "$name"; then
            # Session exists and has windows - check if it's actually making progress
            log_info "Checking health of existing session: ${SESSION_PREFIX}${name}"
            if session_is_stuck "$name" "$repo_root"; then
                log_warn "Session is stuck (idle > ${STUCK_SESSION_THRESHOLD_SECONDS}s with no progress)"
                log_info "Recovering: killing stuck session and restarting fresh"
                kill_stuck_session "$name"
            else
                log_success "Session already exists and is healthy: ${SESSION_PREFIX}${name}"
                log_info "Attach:  tmux -L $TMUX_SOCKET attach -t ${SESSION_PREFIX}${name}"
                exit 0
            fi
        else
            # Session exists but is dead - clean it up
            cleanup_dead_session "$name"
        fi
    fi

    # Spawn the agent
    if ! spawn_agent "$role" "$name" "$args" "$worktree" "$repo_root"; then
        if [[ "$json_output" == "true" ]]; then
            echo "{\"status\":\"error\",\"name\":\"$name\",\"error\":\"spawn_failed\"}"
        fi
        exit 1
    fi

    # Mark as on-demand (ephemeral) for agent-destroy.sh
    if [[ "$on_demand" == "true" ]]; then
        tmux -L "$TMUX_SOCKET" set-environment -t "${SESSION_PREFIX}${name}" LOOM_ON_DEMAND "true"
    fi

    local session_name="${SESSION_PREFIX}${name}"
    local log_file="${repo_root}/.loom/logs/${session_name}.log"

    if [[ "$json_output" == "true" ]] && [[ "$do_wait" != "true" ]]; then
        echo "{\"status\":\"spawned\",\"name\":\"$name\",\"session\":\"$session_name\",\"on_demand\":$on_demand,\"log\":\"$log_file\"}"
    fi

    # Wait for completion if requested
    if [[ "$do_wait" == "true" ]]; then
        local wait_script="${repo_root}/.loom/scripts/agent-wait.sh"
        if [[ ! -x "$wait_script" ]]; then
            log_error "agent-wait.sh not found at $wait_script"
            exit 1
        fi

        local wait_args=("$name" "--timeout" "$wait_timeout")
        if [[ "$json_output" == "true" ]]; then
            wait_args+=("--json")
        fi

        # agent-wait.sh exits 0=completed, 1=timeout, 2=not found
        "$wait_script" "${wait_args[@]}"
        exit $?
    fi

    exit 0
}

# Run main with all script arguments
main "$@"
