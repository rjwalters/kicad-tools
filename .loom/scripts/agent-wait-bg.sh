#!/bin/bash
# agent-wait-bg.sh - Wait for a tmux Claude agent with shutdown signal checking
#
# Wraps agent-wait.sh to run in the background while polling for shutdown signals.
# This allows shepherds to detect shutdown/abort requests during long waits
# instead of blocking until the phase completes.
#
# Also includes stuck detection with configurable thresholds to identify agents
# that appear unresponsive or making no progress.
#
# Exit codes:
#   0 - Agent completed (same as agent-wait.sh)
#   1 - Timeout reached (same as agent-wait.sh)
#   2 - Session not found (same as agent-wait.sh)
#   3 - Shutdown signal detected during wait
#   4 - Agent stuck and intervention triggered (pause/restart)
#
# Stuck Detection Environment Variables:
#   LOOM_STUCK_WARNING   - Seconds without progress before warning (default: 300)
#   LOOM_STUCK_CRITICAL  - Seconds without progress before critical (default: 600)
#   LOOM_STUCK_ACTION    - Action on stuck: warn, pause, restart, retry (default: warn)
#   LOOM_PROMPT_STUCK_THRESHOLD - Seconds before checking for 'stuck at prompt' (default: 30)
#
# Usage:
#   agent-wait-bg.sh <name> [--timeout <s>] [--poll-interval <s>] [--issue <N>] [--task-id <id>] [--json]
#
# Examples:
#   agent-wait-bg.sh builder-issue-42 --timeout 1800 --issue 42 --task-id abc123
#   agent-wait-bg.sh shepherd-1 --poll-interval 10 --json
#   LOOM_STUCK_WARNING=180 LOOM_STUCK_ACTION=pause agent-wait-bg.sh builder-1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Use gh-cached for read-only queries to reduce API calls (see issue #1609)
GH_CACHED="$REPO_ROOT/.loom/scripts/gh-cached"
if [[ -x "$GH_CACHED" ]]; then
    GH="$GH_CACHED"
else
    GH="gh"
fi

# tmux configuration (must match agent-spawn.sh)
TMUX_SOCKET="loom"
SESSION_PREFIX="loom-"

# Colors (RED unused but kept for consistency with other scripts and future error logging)
# shellcheck disable=SC2034
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*" >&2; }
log_success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $*" >&2; }

# Default poll interval for signal checking
DEFAULT_SIGNAL_POLL=5

# Default interval (seconds) for emitting shepherd heartbeats during long waits.
# Keeps progress files fresh so daemon-snapshot.sh and stuck-detection.sh don't
# falsely flag actively building shepherds as stale (see issue #1586).
DEFAULT_HEARTBEAT_INTERVAL=60

# Default idle timeout (seconds) before checking phase contract via GitHub state
DEFAULT_IDLE_TIMEOUT=60

# Default interval (seconds) for proactive phase contract checking
# Checks the phase contract periodically during the poll loop rather than
# waiting for idle timeout. This detects completion much faster for phases
# like builder where the signal is on GitHub (PR exists) rather than in logs.
# Set to 0 to disable proactive checking (fall back to idle timeout only).
# Note: The idle timeout (DEFAULT_IDLE_TIMEOUT=60) still provides faster
# detection when the agent is actually idle, so this interval can be longer
# without significantly impacting completion detection latency.
DEFAULT_CONTRACT_INTERVAL=90

# Stuck detection thresholds (configurable via environment variables)
STUCK_WARNING_THRESHOLD=${LOOM_STUCK_WARNING:-300}   # 5 min default
STUCK_CRITICAL_THRESHOLD=${LOOM_STUCK_CRITICAL:-600} # 10 min default
STUCK_ACTION=${LOOM_STUCK_ACTION:-warn}              # warn, pause, restart, retry

# "Stuck at prompt" detection - command visible but not processing
# This is a distinct, faster-detectable failure mode from general stuck detection
PROMPT_STUCK_THRESHOLD=${LOOM_PROMPT_STUCK_THRESHOLD:-30}  # 30 seconds default

# Pattern for detecting Claude is processing a command (shared with agent-spawn.sh)
PROCESSING_INDICATORS='⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏|Beaming|Loading|● |✓ |◐|◓|◑|◒|thinking|streaming|Wandering'

# Progress tracking file prefix
PROGRESS_DIR="/tmp/loom-agent-progress"

show_help() {
    cat <<EOF
${BLUE}agent-wait-bg.sh - Wait for agent with shutdown signal checking${NC}

${YELLOW}USAGE:${NC}
    agent-wait-bg.sh <name> [OPTIONS]

${YELLOW}OPTIONS:${NC}
    --timeout <seconds>        Maximum time to wait (default: 3600)
    --poll-interval <seconds>  Time between signal checks (default: $DEFAULT_SIGNAL_POLL)
    --issue <N>                Issue number for per-issue abort checking
    --task-id <id>             Shepherd task ID for heartbeat emission during long waits
    --phase <phase>            Phase name (curator, builder, judge, doctor) for contract checking
    --worktree <path>          Worktree path for builder phase recovery
    --pr <N>                   PR number for judge/doctor phase validation
    --grace-period <seconds>   Deprecated (no-op). Agent is terminated immediately on completion detection.
    --idle-timeout <seconds>   Time without output before checking phase contract (default: $DEFAULT_IDLE_TIMEOUT)
    --contract-interval <s>    Seconds between proactive phase contract checks (default: $DEFAULT_CONTRACT_INTERVAL, 0=disable)
    --json                     Output result as JSON
    --help                     Show this help message

${YELLOW}EXIT CODES:${NC}
    0  Agent completed
    1  Timeout reached
    2  Session not found
    3  Shutdown signal detected
    4  Agent stuck and intervention triggered

${YELLOW}SIGNALS CHECKED:${NC}
    - .loom/stop-shepherds file (global shepherd shutdown)
    - loom:abort label on issue (per-issue abort, requires --issue)

${YELLOW}COMPLETION DETECTION:${NC}
    Primary: Proactive phase contract checking (when --phase provided)
    - Checks actual GitHub labels/PRs rather than parsing log output
    - Proactively checked every --contract-interval seconds (default: ${DEFAULT_CONTRACT_INTERVAL}s)
    - Uses validate-phase.sh --check-only for safe, side-effect-free verification
    - Detects completion within one interval of actual work finishing

    Secondary: Idle-triggered phase contract check (when --phase provided)
    - Triggers when agent is idle (no output for --idle-timeout seconds)
    - Acts as fallback if proactive checks are disabled (--contract-interval 0)

    Fallback: Log pattern matching (when --phase not provided)
    - Builder: PR created with loom:review-requested
    - Judge: PR labeled with loom:pr or loom:changes-requested
    - Doctor: PR fixed and labeled with loom:review-requested
    - Curator: Issue labeled with loom:curated

${YELLOW}STUCK DETECTION:${NC}
    Monitors agent progress by tracking tmux pane content changes.
    Configure via environment variables:

    LOOM_STUCK_WARNING   Seconds without progress before warning (default: 300)
    LOOM_STUCK_CRITICAL  Seconds without progress before critical (default: 600)
    LOOM_STUCK_ACTION    Action on stuck: warn, pause, restart (default: warn)

${YELLOW}PROMPT STUCK DETECTION:${NC}
    Fast detection of 'stuck at prompt' state - command visible but not processing.
    This distinct failure mode is detected much faster than general stuck detection.
    Configure via environment variables:

    LOOM_PROMPT_STUCK_THRESHOLD  Seconds before checking for stuck-at-prompt (default: 30)

    Recovery is attempted automatically:
    1. Enter key nudge (command may just need Enter to submit)
    2. Full command retry (if recoverable from session name)

${YELLOW}EXAMPLES:${NC}
    # Phase-aware completion detection with heartbeat (recommended)
    agent-wait-bg.sh builder-issue-42 --timeout 1800 --issue 42 --task-id abc123 --phase builder --worktree .loom/worktrees/issue-42

    # Legacy log-based detection
    agent-wait-bg.sh curator-issue-10 --poll-interval 10 --json

    # With custom stuck thresholds
    LOOM_STUCK_WARNING=180 LOOM_STUCK_ACTION=pause agent-wait-bg.sh builder-1

EOF
}

# Check for shutdown signals
check_signals() {
    local issue="$1"

    # Check global shutdown signal
    if [ -f "${REPO_ROOT}/.loom/stop-shepherds" ]; then
        log_warn "Shutdown signal detected (stop-shepherds)"
        return 0
    fi

    # Check per-issue abort label
    if [ -n "$issue" ]; then
        local labels
        labels=$($GH issue view "$issue" --repo "$($GH repo view --json nameWithOwner --jq '.nameWithOwner')" --json labels --jq '.labels[].name' 2>/dev/null || true)
        if echo "$labels" | grep -q "loom:abort"; then
            log_warn "Abort signal detected for issue #${issue}"
            return 0
        fi
    fi

    return 1
}

# Initialize progress tracking for an agent
init_progress_tracking() {
    local name="$1"

    mkdir -p "$PROGRESS_DIR"

    local progress_file="$PROGRESS_DIR/${name}"
    local hash_file="${progress_file}.hash"
    local time_file="${progress_file}.time"

    # Initialize with current time as last progress
    date +%s > "$time_file"
    # Clear any existing hash
    rm -f "$hash_file"
}

# Check for progress by comparing tmux pane content hash
# Returns 0 if progress detected, 1 if no change
check_progress() {
    local name="$1"
    local session_name="$2"

    local progress_file="$PROGRESS_DIR/${name}"
    local hash_file="${progress_file}.hash"
    local time_file="${progress_file}.time"

    # Capture current pane content and hash it
    local current_content
    current_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || echo "")

    if [[ -z "$current_content" ]]; then
        # Can't capture pane - session may be gone
        return 1
    fi

    local current_hash
    current_hash=$(echo "$current_content" | md5 -q 2>/dev/null || echo "$current_content" | md5sum 2>/dev/null | cut -d' ' -f1)

    local last_hash=""
    if [[ -f "$hash_file" ]]; then
        last_hash=$(cat "$hash_file" 2>/dev/null || echo "")
    fi

    if [[ "$current_hash" != "$last_hash" ]]; then
        # Content changed - progress detected
        echo "$current_hash" > "$hash_file"
        date +%s > "$time_file"
        return 0  # Progress detected
    fi

    return 1  # No progress
}

# Get idle time (seconds since last progress)
get_idle_time() {
    local name="$1"

    local time_file="$PROGRESS_DIR/${name}.time"

    if [[ ! -f "$time_file" ]]; then
        echo "0"
        return
    fi

    local last_progress
    last_progress=$(cat "$time_file" 2>/dev/null || echo "0")
    local now
    now=$(date +%s)

    echo $((now - last_progress))
}

# Check if agent is stuck and return status
# Returns: OK, WARNING, or CRITICAL
check_stuck_status() {
    local name="$1"

    local idle_time
    idle_time=$(get_idle_time "$name")

    if [[ "$idle_time" -gt "$STUCK_CRITICAL_THRESHOLD" ]]; then
        echo "CRITICAL"
    elif [[ "$idle_time" -gt "$STUCK_WARNING_THRESHOLD" ]]; then
        echo "WARNING"
    else
        echo "OK"
    fi
}

# Check if agent is stuck at prompt - command visible but not processing.
# This is a distinct failure mode from general stuck detection and can be
# identified much faster (30s vs 5min).
#
# Returns 0 if stuck at prompt, 1 if processing normally or cannot determine.
# This function checks for role slash commands visible at the prompt without
# any processing indicators, suggesting the command was not dispatched.
check_stuck_at_prompt() {
    local session_name="$1"

    # Capture current pane content
    local pane_content
    pane_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || true)

    if [[ -z "$pane_content" ]]; then
        return 1  # Can't determine, session may be gone
    fi

    # Check for role slash command visible at the prompt line
    # Pattern: ❯ followed by a role command like /builder, /judge, /curator, /doctor, /shepherd
    local command_at_prompt=false
    if echo "$pane_content" | grep -qE '❯[[:space:]]*/?(builder|judge|curator|doctor|shepherd)'; then
        command_at_prompt=true
    fi

    # Check for processing indicators that show Claude is working
    local processing=false
    if echo "$pane_content" | grep -qE "$PROCESSING_INDICATORS"; then
        processing=true
    fi

    # Stuck at prompt = command visible but not processing
    if [[ "$command_at_prompt" == "true" ]] && [[ "$processing" == "false" ]]; then
        return 0  # Stuck at prompt
    fi

    return 1  # Not stuck at prompt (either processing or no command visible)
}

# Attempt to recover an agent stuck at the prompt.
# Tries Enter key nudge first, then full command retry if that fails.
# Returns 0 if recovered, 1 if recovery failed.
attempt_prompt_stuck_recovery() {
    local session_name="$1"
    local role_cmd="$2"

    # Strategy 1: Try an Enter key nudge first
    # The command is typically already visible at the prompt and just needs Enter to trigger processing
    log_info "Trying Enter key nudge to recover stuck prompt..."
    tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" C-m 2>/dev/null || return 1
    sleep 3

    # Check if now processing
    local pane_content
    pane_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || true)
    if echo "$pane_content" | grep -qE "$PROCESSING_INDICATORS"; then
        log_success "Agent recovered with Enter key nudge"
        return 0
    fi

    # Strategy 2: If nudge failed and we have the role command, re-send it
    if [[ -n "$role_cmd" ]]; then
        log_info "Enter nudge failed, re-sending role command: $role_cmd"
        sleep 2  # Additional wait for TUI
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "$role_cmd" C-m 2>/dev/null || return 1
        sleep 3

        pane_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || true)
        if echo "$pane_content" | grep -qE "$PROCESSING_INDICATORS"; then
            log_success "Agent recovered with full command retry"
            return 0
        fi
    fi

    log_warn "Prompt stuck recovery failed - intervention may be needed"
    return 1
}

# Capture diagnostic information from a stuck agent before killing it
# Saves tmux pane content and log tail to a diagnostics file
capture_stuck_diagnostics() {
    local name="$1"
    local session_name="$2"
    local idle_time="$3"

    local diag_dir="${REPO_ROOT}/.loom/diagnostics"
    mkdir -p "$diag_dir"

    local timestamp
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local diag_file="${diag_dir}/stuck-${name}-$(date +%s).txt"

    {
        echo "=== Stuck Agent Diagnostics ==="
        echo "Agent: $name"
        echo "Session: $session_name"
        echo "Timestamp: $timestamp"
        echo "Idle time: ${idle_time}s"
        echo ""

        echo "=== Tmux Pane Content (last visible) ==="
        tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || echo "(session not available)"
        echo ""

        echo "=== Log File Tail ==="
        local log_file="${REPO_ROOT}/.loom/logs/${session_name}.log"
        if [[ -f "$log_file" ]]; then
            tail -50 "$log_file" 2>/dev/null || echo "(could not read log)"
        else
            echo "(no log file found at $log_file)"
        fi
    } > "$diag_file" 2>&1

    log_info "Diagnostics captured to $diag_file"
    echo "$diag_file"
}

# Handle stuck agent intervention
# Returns 0 if should continue waiting, 1 if should exit
handle_stuck() {
    local name="$1"
    local session_name="$2"
    local status="$3"
    local issue="$4"
    local json_output="$5"
    local elapsed="$6"

    local idle_time
    idle_time=$(get_idle_time "$name")

    case "$STUCK_ACTION" in
        warn)
            if [[ "$status" == "CRITICAL" ]]; then
                log_warn "CRITICAL: Agent '$name' appears stuck (no progress for ${idle_time}s)"
            else
                log_warn "WARNING: Agent '$name' may be stuck (no progress for ${idle_time}s)"
            fi
            return 0  # Continue waiting
            ;;
        pause)
            log_warn "PAUSE: Pausing stuck agent '$name' (no progress for ${idle_time}s)"

            # Signal the agent to pause via .loom/signals
            local signal_file="${REPO_ROOT}/.loom/signals/pause-${name}"
            mkdir -p "${REPO_ROOT}/.loom/signals"
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) - Auto-paused: stuck detection (idle ${idle_time}s)" > "$signal_file"

            if [[ "$json_output" == "true" ]]; then
                echo "{\"status\":\"stuck\",\"name\":\"$name\",\"action\":\"paused\",\"idle_time\":$idle_time,\"stuck_status\":\"$status\",\"elapsed\":$elapsed}"
            fi
            return 1  # Exit with stuck status
            ;;
        restart)
            log_warn "RESTART: Restarting stuck agent '$name' (no progress for ${idle_time}s)"

            # Capture diagnostics before killing
            capture_stuck_diagnostics "$name" "$session_name" "$idle_time" || true

            # Destroy the tmux session
            tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true

            # Clean up progress files
            cleanup_progress_files "$name"

            if [[ "$json_output" == "true" ]]; then
                echo "{\"status\":\"stuck\",\"name\":\"$name\",\"action\":\"restarted\",\"idle_time\":$idle_time,\"stuck_status\":\"$status\",\"elapsed\":$elapsed}"
            fi
            return 1  # Exit with stuck status (shepherd will respawn)
            ;;
        retry)
            log_warn "RETRY: Killing stuck agent '$name' for retry (no progress for ${idle_time}s)"

            # Capture diagnostics before killing
            capture_stuck_diagnostics "$name" "$session_name" "$idle_time" || true

            # Destroy the tmux session
            tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true

            # Clean up progress files
            cleanup_progress_files "$name"

            if [[ "$json_output" == "true" ]]; then
                echo "{\"status\":\"stuck\",\"name\":\"$name\",\"action\":\"retry\",\"idle_time\":$idle_time,\"stuck_status\":\"$status\",\"elapsed\":$elapsed}"
            fi
            return 1  # Exit with stuck status (shepherd will retry phase)
            ;;
        *)
            # Unknown action, default to warn
            log_warn "Agent '$name' stuck status: $status (idle ${idle_time}s)"
            return 0
            ;;
    esac
}

# Clean up progress tracking files for an agent
cleanup_progress_files() {
    local name="$1"

    rm -f "$PROGRESS_DIR/${name}.hash"
    rm -f "$PROGRESS_DIR/${name}.time"
    rm -f "$PROGRESS_DIR/${name}"
}

# Extract phase from session name (e.g., "builder-issue-123" -> "builder")
# Returns empty string if no recognized phase found
extract_phase_from_session() {
    local session_name="$1"

    # Remove the "loom-" prefix if present (session_name may be full tmux session name)
    local base_name="${session_name#loom-}"

    # Extract the first component before "-issue-" or "-"
    local phase
    phase=$(echo "$base_name" | sed -E 's/^(builder|judge|curator|doctor|shepherd)-.*$/\1/')

    # Verify it's a recognized phase
    case "$phase" in
        builder|judge|curator|doctor|shepherd)
            echo "$phase"
            ;;
        *)
            echo ""
            ;;
    esac
}

# Check for role-specific completion patterns in log file
# Returns 0 if completion detected, 1 otherwise
# Sets COMPLETION_REASON global variable with the detected pattern
#
# The function is phase-aware: it only checks patterns relevant to the
# current phase (extracted from session name) to avoid false matches.
# For example, a judge reviewing a PR with loom:review-requested won't
# incorrectly match the builder_pr_created pattern.
check_completion_patterns() {
    local session_name="$1"
    local log_file="${REPO_ROOT}/.loom/logs/${session_name}.log"

    if [[ ! -f "$log_file" ]]; then
        return 1
    fi

    # Get recent log content (last 100 lines to check for completion)
    local recent_log
    recent_log=$(tail -100 "$log_file" 2>/dev/null || true)

    if [[ -z "$recent_log" ]]; then
        return 1
    fi

    # Extract phase from session name for phase-aware pattern matching
    local phase
    phase=$(extract_phase_from_session "$session_name")

    # Generic completion: /exit command detected (always checked regardless of phase)
    # More robust pattern to catch various prompt styles and formatting
    # Including indented /exit from LLM text output (e.g., "  /exit")
    if echo "$recent_log" | grep -qE '(^|\s+|❯\s*|>\s*)/exit\s*$'; then
        COMPLETION_REASON="explicit_exit"
        return 0
    fi

    # Phase-specific completion patterns
    # Only check the pattern relevant to the current phase to avoid false matches
    case "$phase" in
        builder)
            # Builder completion: PR created successfully
            # Match the actual gh pr create OUTPUT (the PR URL), not the command text.
            # The command text (including "loom:review-requested") appears in Claude Code's
            # UI rendering while the command is still running, causing false positives.
            # gh pr create prints the PR URL on success: https://github.com/.../pull/NNN
            if echo "$recent_log" | grep -qE 'https://github\.com/.*/pull/[0-9]+'; then
                COMPLETION_REASON="builder_pr_created"
                return 0
            fi
            ;;
        judge)
            # Judge completion: PR labeled with loom:pr or loom:changes-requested
            if echo "$recent_log" | grep -qE 'add-label.*loom:pr|add-label.*loom:changes-requested|--add-label "loom:pr"|--add-label "loom:changes-requested"'; then
                COMPLETION_REASON="judge_review_complete"
                return 0
            fi
            ;;
        doctor)
            # Doctor completion: PR labeled with loom:review-requested after fixes
            # Similar to builder but in context of fixing (look for treating label removal)
            if echo "$recent_log" | grep -qE 'remove-label.*loom:treating.*add-label.*loom:review-requested|remove-label.*loom:changes-requested.*add-label.*loom:review-requested'; then
                COMPLETION_REASON="doctor_fixes_complete"
                return 0
            fi
            ;;
        curator)
            # Curator completion: Issue labeled with loom:curated
            if echo "$recent_log" | grep -qE 'add-label.*loom:curated|--add-label "loom:curated"'; then
                COMPLETION_REASON="curator_curation_complete"
                return 0
            fi
            ;;
        *)
            # Unknown phase or shepherd - check all patterns as fallback
            # This handles generic or shepherd sessions that may spawn worker roles
            if echo "$recent_log" | grep -qE 'https://github\.com/.*/pull/[0-9]+'; then
                COMPLETION_REASON="builder_pr_created"
                return 0
            fi
            if echo "$recent_log" | grep -qE 'add-label.*loom:pr|add-label.*loom:changes-requested|--add-label "loom:pr"|--add-label "loom:changes-requested"'; then
                COMPLETION_REASON="judge_review_complete"
                return 0
            fi
            if echo "$recent_log" | grep -qE 'remove-label.*loom:treating.*add-label.*loom:review-requested|remove-label.*loom:changes-requested.*add-label.*loom:review-requested'; then
                COMPLETION_REASON="doctor_fixes_complete"
                return 0
            fi
            if echo "$recent_log" | grep -qE 'add-label.*loom:curated|--add-label "loom:curated"'; then
                COMPLETION_REASON="curator_curation_complete"
                return 0
            fi
            ;;
    esac

    return 1
}

# Check phase contract satisfaction via validate-phase.sh
# Returns 0 if contract is satisfied (work complete), 1 otherwise
# Sets CONTRACT_STATUS global variable with the validation result
# Optional 5th parameter: "check_only" to skip side effects (for idle timeout checks)
check_phase_contract() {
    local phase="$1"
    local issue="$2"
    local worktree="$3"
    local pr_number="$4"
    local check_only="${5:-}"

    if [[ -z "$phase" ]] || [[ -z "$issue" ]]; then
        return 1
    fi

    local validate_args=("$phase" "$issue")
    if [[ -n "$worktree" ]]; then
        validate_args+=("--worktree" "$worktree")
    fi
    if [[ -n "$pr_number" ]]; then
        validate_args+=("--pr" "$pr_number")
    fi
    # Use --check-only to avoid side effects (worktree removal, label changes)
    # when checking during idle timeout (see issue #1536)
    if [[ "$check_only" == "check_only" ]]; then
        validate_args+=("--check-only")
    fi
    validate_args+=("--json")

    local result
    if result=$("${SCRIPT_DIR}/validate-phase.sh" "${validate_args[@]}" 2>/dev/null); then
        CONTRACT_STATUS=$(echo "$result" | jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")
        if [[ "$CONTRACT_STATUS" == "satisfied" ]] || [[ "$CONTRACT_STATUS" == "recovered" ]]; then
            return 0
        fi
    fi

    CONTRACT_STATUS="not_satisfied"
    return 1
}

# Get the time since log file was last modified (in seconds)
# Returns the number of seconds, or -1 if log file doesn't exist
get_log_idle_time() {
    local log_file="$1"

    if [[ ! -f "$log_file" ]]; then
        echo "-1"
        return
    fi

    local now
    local mtime
    now=$(date +%s)

    # macOS uses -f %m for modification time in seconds since epoch
    if [[ "$(uname)" == "Darwin" ]]; then
        mtime=$(stat -f %m "$log_file" 2>/dev/null || echo "$now")
    else
        # Linux uses -c %Y
        mtime=$(stat -c %Y "$log_file" 2>/dev/null || echo "$now")
    fi

    echo $((now - mtime))
}

# Check for interactive prompts in the agent's tmux pane and auto-resolve them.
# Claude Code's plan mode presents an approval prompt that blocks execution when
# no human is present. This function detects the prompt and sends the approval
# keystroke so autonomous agents can proceed.
check_and_resolve_prompts() {
    local session_name="$1"

    # Capture current pane content (silently fail if session gone)
    local pane_content
    pane_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || true)

    if [[ -z "$pane_content" ]]; then
        return 1
    fi

    # Detect Claude Code plan mode approval prompt.
    # The prompt shows numbered options like:
    #   "Would you like to proceed?"
    #   1. Yes, clear context and bypass permissions
    #   2. Yes, and bypass permissions
    # We look for the distinctive "Would you like to proceed" text.
    if echo "$pane_content" | grep -q "Would you like to proceed"; then
        log_info "Plan mode approval prompt detected in $session_name - auto-approving"
        # Send "1" to select "Yes, clear context and bypass permissions"
        tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "1" C-m
        return 0
    fi

    return 1
}

main() {
    local name=""
    local timeout="3600"
    local poll_interval="$DEFAULT_SIGNAL_POLL"
    local issue=""
    local task_id=""
    local idle_timeout="$DEFAULT_IDLE_TIMEOUT"
    local contract_interval="$DEFAULT_CONTRACT_INTERVAL"
    local phase=""
    local worktree=""
    local pr_number=""
    local json_output=false

    if [[ $# -lt 1 ]]; then
        show_help
        exit 2
    fi

    name="$1"
    shift

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --timeout)
                timeout="$2"
                shift 2
                ;;
            --poll-interval)
                poll_interval="$2"
                shift 2
                ;;
            --issue)
                issue="$2"
                shift 2
                ;;
            --task-id)
                task_id="$2"
                shift 2
                ;;
            --grace-period)
                # Deprecated: grace period is no longer used (agents are terminated immediately)
                shift 2
                ;;
            --idle-timeout)
                idle_timeout="$2"
                shift 2
                ;;
            --contract-interval)
                contract_interval="$2"
                shift 2
                ;;
            --phase)
                phase="$2"
                shift 2
                ;;
            --worktree)
                worktree="$2"
                shift 2
                ;;
            --pr)
                pr_number="$2"
                shift 2
                ;;
            --json)
                json_output=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                echo "Unknown argument: $1" >&2
                exit 2
                ;;
        esac
    done

    log_info "Waiting for agent '$name' with signal checking (poll: ${poll_interval}s, timeout: ${timeout}s)"
    if [[ -n "$task_id" ]]; then
        log_info "Heartbeat emission: every ${DEFAULT_HEARTBEAT_INTERVAL}s (task-id: $task_id)"
    fi
    if [[ -n "$phase" ]] && [[ "$contract_interval" -gt 0 ]]; then
        log_info "Proactive contract checking: every ${contract_interval}s for phase '$phase'"
    fi
    log_info "Stuck detection: warning=${STUCK_WARNING_THRESHOLD}s, critical=${STUCK_CRITICAL_THRESHOLD}s, action=${STUCK_ACTION}"
    log_info "Prompt stuck detection: threshold=${PROMPT_STUCK_THRESHOLD}s"

    # Launch agent-wait.sh in the background
    "${SCRIPT_DIR}/agent-wait.sh" "$name" --timeout "$timeout" --poll-interval "$poll_interval" --json &
    local wait_pid=$!

    local start_time
    start_time=$(date +%s)

    local session_name="${SESSION_PREFIX}${name}"
    local log_file="${REPO_ROOT}/.loom/logs/${session_name}.log"
    local prompt_resolved=false
    local completion_detected=false
    local idle_contract_checked=false
    local last_contract_check=0
    local stuck_warned=false
    local stuck_critical_reported=false
    local prompt_stuck_checked=false
    local prompt_stuck_recovery_attempted=false
    local last_heartbeat_time=$start_time
    COMPLETION_REASON=""
    CONTRACT_STATUS=""

    # Initialize progress tracking
    init_progress_tracking "$name"

    # Poll for signals, prompts, completion patterns, and stuck detection while background process runs
    while true; do
        # Check for interactive prompts that need auto-approval (e.g., plan mode).
        # Only attempt once to avoid sending stray keystrokes after the prompt clears.
        if [[ "$prompt_resolved" != "true" ]]; then
            if check_and_resolve_prompts "$session_name"; then
                prompt_resolved=true
            fi
        fi

        # Check if agent-wait.sh has finished
        if ! kill -0 "$wait_pid" 2>/dev/null; then
            # Process exited, get its exit code
            wait "$wait_pid"
            local exit_code=$?

            # Clean up progress files on completion
            cleanup_progress_files "$name"

            if [[ "$json_output" == "true" ]]; then
                # agent-wait.sh already output JSON, just pass through exit code
                :
            fi
            exit "$exit_code"
        fi

        # Check for shutdown signals
        if check_signals "$issue"; then
            local elapsed=$(( $(date +%s) - start_time ))

            # Kill the background wait process
            kill "$wait_pid" 2>/dev/null || true
            wait "$wait_pid" 2>/dev/null || true

            # Clean up progress files
            cleanup_progress_files "$name"

            if [[ "$json_output" == "true" ]]; then
                local signal_type="shutdown"
                if [ -n "$issue" ]; then
                    local labels
                    labels=$($GH issue view "$issue" --json labels --jq '.labels[].name' 2>/dev/null || true)
                    if echo "$labels" | grep -q "loom:abort"; then
                        signal_type="abort"
                    fi
                fi
                echo "{\"status\":\"signal\",\"name\":\"$name\",\"signal_type\":\"$signal_type\",\"elapsed\":$elapsed}"
            else
                log_warn "Shutdown signal detected after ${elapsed}s - aborting wait for '$name'"
            fi
            exit 3
        fi

        # Fast "stuck at prompt" detection - command visible but not processing
        # This failure mode can be detected in ~30s vs the 5min general stuck threshold.
        # Only check once after the threshold and only if not already processing.
        local elapsed=$(( $(date +%s) - start_time ))
        if [[ "$prompt_stuck_checked" != "true" ]] && [[ "$prompt_stuck_recovery_attempted" != "true" ]] && [[ "$elapsed" -ge "$PROMPT_STUCK_THRESHOLD" ]]; then
            if check_stuck_at_prompt "$session_name"; then
                prompt_stuck_checked=true
                log_warn "Agent appears stuck at prompt (command visible but not processing after ${elapsed}s)"

                # Attempt recovery
                prompt_stuck_recovery_attempted=true
                # Extract the likely role command from the session name for retry
                local role_cmd=""
                if [[ "$name" == builder-issue-* ]]; then
                    local issue_num="${name#builder-issue-}"
                    role_cmd="/builder ${issue_num}"
                elif [[ "$name" == judge-* ]] || [[ "$name" == curator-* ]] || [[ "$name" == doctor-* ]]; then
                    # For other roles, we can't easily reconstruct the command
                    # Enter nudge is still attempted
                    role_cmd=""
                fi

                if attempt_prompt_stuck_recovery "$session_name" "$role_cmd"; then
                    log_success "Agent recovered from stuck-at-prompt state"
                    # Reset tracking so we continue normal monitoring
                    prompt_stuck_checked=false
                else
                    # Recovery failed - let the general stuck detection handle escalation
                    log_warn "Stuck-at-prompt recovery failed - waiting for general stuck detection"
                fi
            else
                # Not stuck at prompt - mark as checked so we don't keep rechecking
                prompt_stuck_checked=true
            fi
        fi

        # Reset prompt stuck tracking if we see progress (pane content changed)
        if [[ "$prompt_stuck_checked" == "true" ]] || [[ "$prompt_stuck_recovery_attempted" == "true" ]]; then
            local pane_content
            pane_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || true)
            if echo "$pane_content" | grep -qE "$PROCESSING_INDICATORS"; then
                # Agent is now processing - reset tracking
                prompt_stuck_checked=false
                prompt_stuck_recovery_attempted=false
            fi
        fi

        # Proactive phase contract checking: check periodically regardless of idle state
        # This detects completion within one contract_interval of actual work finishing,
        # rather than waiting for the idle timeout to trigger (see issue #1581)
        if [[ "$completion_detected" != "true" ]] && [[ -n "$phase" ]] && [[ "$contract_interval" -gt 0 ]]; then
            local now
            now=$(date +%s)
            local since_last_check=$((now - last_contract_check))

            if [[ "$since_last_check" -ge "$contract_interval" ]]; then
                last_contract_check=$now

                if check_phase_contract "$phase" "$issue" "$worktree" "$pr_number" "check_only"; then
                    completion_detected=true
                    COMPLETION_REASON="phase_contract_satisfied"

                    log_info "Phase contract satisfied ($CONTRACT_STATUS) via proactive check"
                    log_info "Agent completed work but didn't exit - terminating session"
                fi
            fi
        fi

        # Activity-based completion detection: check phase contract when agent is idle
        # This is a backup mechanism when /exit doesn't work (see issue #1461)
        # Skipped if proactive checking already detected completion above
        if [[ "$completion_detected" != "true" ]] && [[ -n "$phase" ]] && [[ "$idle_contract_checked" != "true" ]]; then
            local idle_time
            idle_time=$(get_log_idle_time "$log_file")

            if [[ "$idle_time" -ge "$idle_timeout" ]]; then
                log_info "Agent idle for ${idle_time}s (threshold: ${idle_timeout}s) - checking phase contract"

                # Use check_only mode to avoid side effects during idle check
                # This prevents premature worktree removal that breaks retry (issue #1536)
                if check_phase_contract "$phase" "$issue" "$worktree" "$pr_number" "check_only"; then
                    completion_detected=true
                    COMPLETION_REASON="phase_contract_satisfied"

                    log_info "Phase contract satisfied ($CONTRACT_STATUS) - terminating session"
                else
                    # Contract not satisfied, don't check again until next idle timeout
                    idle_contract_checked=true
                    log_info "Phase contract not satisfied - continuing to wait"
                fi
            fi
        fi

        # Reset idle check flag if there's been new activity
        if [[ "$idle_contract_checked" == "true" ]]; then
            local idle_time
            idle_time=$(get_log_idle_time "$log_file")
            if [[ "$idle_time" -lt "$idle_timeout" ]]; then
                idle_contract_checked=false
            fi
        fi

        # Check for completion patterns in log (backup detection)
        if [[ "$completion_detected" != "true" ]]; then
            if check_completion_patterns "$session_name"; then
                if [[ "$COMPLETION_REASON" == "explicit_exit" ]]; then
                    completion_detected=true
                elif [[ -n "$phase" ]] && [[ -n "$issue" ]]; then
                    # Non-exit completion pattern detected (e.g., label command in log).
                    # The pattern matches the *intent* to run a gh command, not its
                    # confirmed execution. Sleep briefly to let the gh command finish,
                    # then verify the phase contract is actually satisfied before
                    # terminating. This prevents killing the session while gh is still
                    # executing (see issue #1596).
                    log_info "Completion pattern detected ($COMPLETION_REASON) - verifying phase contract"
                    sleep 3
                    if check_phase_contract "$phase" "$issue" "$worktree" "$pr_number" "check_only"; then
                        completion_detected=true
                        log_info "Phase contract verified ($CONTRACT_STATUS) - terminating session"
                    else
                        log_warn "Completion pattern detected but phase contract not yet satisfied - continuing to wait"
                        COMPLETION_REASON=""
                    fi
                else
                    # No phase info available, trust the pattern
                    completion_detected=true
                    log_info "Completion pattern detected ($COMPLETION_REASON) - terminating session"
                fi
            fi
        fi

        # If completion was detected, terminate immediately
        if [[ "$completion_detected" == "true" ]]; then
            local elapsed=$(( $(date +%s) - start_time ))

            if [[ "$COMPLETION_REASON" == "explicit_exit" ]]; then
                log_info "/exit detected in output - sending /exit to prompt and terminating '$session_name'"

                # Send /exit to the actual tmux prompt as backup
                # This ensures the CLI receives /exit even if the LLM just output it as text
                tmux -L "$TMUX_SOCKET" send-keys -t "$session_name" "/exit" C-m 2>/dev/null || true

                # Brief pause to let /exit process
                sleep 1
            fi

            # Kill the background wait process
            kill "$wait_pid" 2>/dev/null || true
            wait "$wait_pid" 2>/dev/null || true

            # Clean up progress files
            cleanup_progress_files "$name"

            # Destroy the tmux session to clean up
            tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true

            if [[ "$json_output" == "true" ]]; then
                echo "{\"status\":\"completed\",\"name\":\"$name\",\"reason\":\"$COMPLETION_REASON\",\"elapsed\":$elapsed}"
            else
                log_success "Agent '$name' completed ($COMPLETION_REASON after ${elapsed}s)"
            fi
            exit 0
        fi

        # Check for progress and update stuck tracking
        check_progress "$name" "$session_name" || true

        # Check stuck status (only if not already completing)
        if [[ "$completion_detected" != "true" ]]; then
            local stuck_status
            stuck_status=$(check_stuck_status "$name")

            if [[ "$stuck_status" == "WARNING" ]] && [[ "$stuck_warned" != "true" ]]; then
                stuck_warned=true
                local elapsed=$(( $(date +%s) - start_time ))
                if [[ "$STUCK_ACTION" != "warn" ]]; then
                    # For pause/restart actions, only trigger on CRITICAL
                    log_warn "Agent '$name' showing signs of being stuck (no progress for $(get_idle_time "$name")s)"
                else
                    handle_stuck "$name" "$session_name" "$stuck_status" "$issue" "$json_output" "$elapsed"
                fi
            elif [[ "$stuck_status" == "CRITICAL" ]] && [[ "$stuck_critical_reported" != "true" ]]; then
                stuck_critical_reported=true
                local elapsed=$(( $(date +%s) - start_time ))

                # For pause/restart, trigger intervention at CRITICAL level
                if ! handle_stuck "$name" "$session_name" "$stuck_status" "$issue" "$json_output" "$elapsed"; then
                    # Intervention triggered that requires exit

                    # Kill the background wait process
                    kill "$wait_pid" 2>/dev/null || true
                    wait "$wait_pid" 2>/dev/null || true

                    # Clean up progress files
                    cleanup_progress_files "$name"

                    exit 4
                fi
            fi
        fi

        # Emit periodic heartbeat to keep shepherd progress file fresh (issue #1586).
        # Without this, long-running phases (builder, doctor) cause the progress file's
        # last_heartbeat to go stale, triggering false positives in daemon-snapshot.sh
        # and stuck-detection.sh which use a 120s stale threshold.
        if [[ -n "$task_id" ]] && [[ -x "$SCRIPT_DIR/report-milestone.sh" ]]; then
            local now
            now=$(date +%s)
            local since_last_heartbeat=$((now - last_heartbeat_time))

            if [[ "$since_last_heartbeat" -ge "$DEFAULT_HEARTBEAT_INTERVAL" ]]; then
                last_heartbeat_time=$now
                local elapsed=$((now - start_time))
                local elapsed_min=$((elapsed / 60))
                local phase_desc="${phase:-agent}"
                "$SCRIPT_DIR/report-milestone.sh" heartbeat \
                    --task-id "$task_id" \
                    --action "${phase_desc} running (${elapsed_min}m elapsed)" \
                    --quiet 2>/dev/null || true
            fi
        fi

        sleep "$poll_interval"
    done
}

main "$@"
