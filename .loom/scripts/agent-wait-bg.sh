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
#   LOOM_STUCK_CRITICAL  - Seconds without progress before kill+retry (default: 600)
#   LOOM_STUCK_ACTION    - Action on stuck: warn, pause, restart, retry (default: retry)
#   LOOM_PROMPT_STUCK_CHECK_INTERVAL - Check interval for 'stuck at prompt' detection (default: 10)
#   LOOM_PROMPT_STUCK_AGE_THRESHOLD - How long stuck before triggering detection (default: 30)
#   LOOM_PROMPT_STUCK_RECOVERY_COOLDOWN - Seconds before re-attempting recovery (default: 60)
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

log_info() { echo -e "${BLUE}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]${NC} $*" >&2; }
log_success() { echo -e "${GREEN}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ✓${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ⚠${NC} $*" >&2; }

# Default poll interval for signal checking
DEFAULT_SIGNAL_POLL=5

# Default interval (seconds) for emitting shepherd heartbeats during long waits.
# Keeps progress files fresh so the snapshot and stuck-detection systems don't
# falsely flag actively building shepherds as stale (see issue #1586).
DEFAULT_HEARTBEAT_INTERVAL=60

# Default idle timeout (seconds) before checking phase contract via GitHub state
DEFAULT_IDLE_TIMEOUT=60

# Adaptive contract checking intervals (see issue #1678)
# Contract checks are expensive (GitHub API calls), so we start with longer
# intervals and decrease them over time as completion becomes more likely.
#
# Interval schedule based on elapsed time:
#   0-180s:   No contract checks (wait for initial processing)
#   180-270s: 90s interval (agent still early in work)
#   270-330s: 60s interval (agent progressing)
#   330-360s: 30s interval (likely nearing completion)
#   360s+:    10s interval (final detection mode)
#
# Set CONTRACT_INTERVAL_OVERRIDE > 0 to use a fixed interval instead of adaptive.
# Set to 0 to disable proactive checking entirely (fall back to idle timeout only).
CONTRACT_INTERVAL_OVERRIDE=${LOOM_CONTRACT_INTERVAL_OVERRIDE:-0}

# Initial delay before first contract check (seconds)
CONTRACT_INITIAL_DELAY=180

# Stuck detection thresholds (configurable via environment variables)
#
# Two independent stuck-detection subsystems exist:
#
# 1. **General idle detection** (this section): Monitors tmux pane content
#    changes and worktree file modifications.  If neither changes for
#    STUCK_WARNING_THRESHOLD seconds, a warning fires.  At STUCK_CRITICAL_THRESHOLD,
#    the STUCK_ACTION is taken (default: retry = kill and retry the phase).
#    Use --max-idle to set the critical threshold and force action=retry.
#
# 2. **Prompt-stuck detection** (next section): Detects the specific failure
#    mode where a command is visible at the prompt but not processing.  This
#    fires much faster (~30s) via PROMPT_STUCK_AGE_THRESHOLD.
#
# These subsystems are complementary: prompt-stuck catches "command not
# dispatched" failures quickly, while general idle catches "agent running
# but producing nothing" failures over minutes.
#
# Lowered from 3600/7200 (issue #2001) to 300/600 (issue #2406) after a
# stuck builder ran undetected for 150 minutes.
STUCK_WARNING_THRESHOLD=${LOOM_STUCK_WARNING:-300}    # 5 min default
STUCK_CRITICAL_THRESHOLD=${LOOM_STUCK_CRITICAL:-600}  # 10 min default
STUCK_ACTION=${LOOM_STUCK_ACTION:-retry}              # warn, pause, restart, retry

# "Stuck at prompt" detection - command visible but not processing
# This is a distinct, faster-detectable failure mode from general stuck detection.
# Detection fires when agent has been stuck for >= AGE_THRESHOLD seconds.
# We check at CHECK_INTERVAL frequency for responsiveness.
#
# LOOM_PROMPT_STUCK_CHECK_INTERVAL: How often to poll for stuck state (default: 10s)
# LOOM_PROMPT_STUCK_AGE_THRESHOLD: How long stuck before detection fires (default: 30s)
# LOOM_PROMPT_STUCK_RECOVERY_COOLDOWN: Seconds before re-attempting recovery (default: 60s)
#
# With defaults (check=10s, age=30s, poll=5s), detection fires within ~35-40s of becoming stuck.
PROMPT_STUCK_CHECK_INTERVAL=${LOOM_PROMPT_STUCK_CHECK_INTERVAL:-10}  # check every 10 seconds
PROMPT_STUCK_AGE_THRESHOLD=${LOOM_PROMPT_STUCK_AGE_THRESHOLD:-30}    # stuck for 30s before detection
PROMPT_STUCK_RECOVERY_COOLDOWN=${LOOM_PROMPT_STUCK_RECOVERY_COOLDOWN:-60}  # 60s before re-trying recovery

# Pattern for detecting Claude is processing a command (shared with agent-spawn.sh)
# Claude Code shows "esc to interrupt" in the status bar whenever it is working
# (thinking, running tools, streaming). This text is absent when idle at prompt.
PROCESSING_INDICATORS='esc to interrupt'

# Progress tracking file prefix
PROGRESS_DIR="/tmp/loom-agent-progress"

# Get adaptive contract check interval based on elapsed time since agent started.
# Returns the appropriate interval in seconds, or 0 if we should skip this check.
#
# The schedule balances detection latency against API cost:
#   0-180s:   Skip checks (return 0) - agent still processing initial work
#   180-270s: 90s interval - early work phase
#   270-330s: 60s interval - mid work phase
#   330-360s: 30s interval - likely nearing completion
#   360s+:    10s interval - final rapid detection mode
#
# If CONTRACT_INTERVAL_OVERRIDE is set > 0, returns that fixed value instead.
# Returns 0 to signal "skip this check" (used during initial delay period).
get_adaptive_contract_interval() {
    local elapsed=$1

    # Allow override for testing or specific use cases
    if [[ "${CONTRACT_INTERVAL_OVERRIDE:-0}" -gt 0 ]]; then
        echo "$CONTRACT_INTERVAL_OVERRIDE"
        return
    fi

    # Adaptive schedule based on elapsed time
    if [[ "$elapsed" -lt 180 ]]; then
        echo "0"  # No check yet - wait for initial delay
    elif [[ "$elapsed" -lt 270 ]]; then
        echo "90"
    elif [[ "$elapsed" -lt 330 ]]; then
        echo "60"
    elif [[ "$elapsed" -lt 360 ]]; then
        echo "30"
    else
        echo "10"
    fi
}

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
    --max-idle <seconds>       Max seconds without progress before kill+retry (sets critical threshold and action=retry)
    --min-session-age <secs>   Minimum session age before idle prompt detection activates (default: 10)
    --min-idle-elapsed <secs>  Deprecated alias for --min-session-age
    --worktree <path>          Worktree path for builder phase recovery and file-change detection
    --pr <N>                   PR number for judge/doctor phase validation
    --grace-period <seconds>   Deprecated (no-op). Agent is terminated immediately on completion detection.
    --idle-timeout <seconds>   Time without output before checking phase contract (default: $DEFAULT_IDLE_TIMEOUT)
    --contract-interval <s>    Override adaptive interval with fixed value (default: adaptive, 0=disable)
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
    - Uses adaptive intervals that decrease as agent runs longer (issue #1678):
        0-180s:   No checks (initial processing delay)
        180-270s: 90s interval
        270-330s: 60s interval
        330-360s: 30s interval
        360s+:    10s interval (final rapid detection)
    - Uses validate-phase.sh --check-only for safe, side-effect-free verification
    - Override with --contract-interval <seconds> or LOOM_CONTRACT_INTERVAL_OVERRIDE env var

    Secondary: Idle-triggered phase contract check (when --phase provided)
    - Triggers when agent is idle (no output for --idle-timeout seconds)
    - Acts as fallback if proactive checks are disabled (--contract-interval 0)

    Fallback: Log pattern matching (when --phase not provided)
    - Builder: PR created with loom:review-requested
    - Judge: PR labeled with loom:pr or loom:changes-requested
    - Doctor: PR fixed and labeled with loom:review-requested
    - Curator: Issue labeled with loom:curated

${YELLOW}STUCK DETECTION:${NC}
    Monitors agent progress by tracking tmux pane content changes and
    worktree file modifications (when --worktree is provided).
    Configure via environment variables or --max-idle flag:

    LOOM_STUCK_WARNING   Seconds without progress before warning (default: 300)
    LOOM_STUCK_CRITICAL  Seconds without progress before kill+retry (default: 600)
    LOOM_STUCK_ACTION    Action on stuck: warn, pause, restart, retry (default: retry)

    --max-idle <seconds> sets STUCK_CRITICAL and forces action=retry

${YELLOW}PROMPT STUCK DETECTION:${NC}
    Fast detection of 'stuck at prompt' state - command visible but not processing.
    This distinct failure mode is detected much faster than general stuck detection.
    Configure via environment variables:

    LOOM_PROMPT_STUCK_CHECK_INTERVAL   How often to check for stuck state (default: 10)
    LOOM_PROMPT_STUCK_AGE_THRESHOLD    How long stuck before detection fires (default: 30)
    LOOM_PROMPT_STUCK_RECOVERY_COOLDOWN  Seconds before re-attempting recovery (default: 60)

    With defaults (check=10s, age=30s, poll=5s), detection fires within ~35-40s.
    Recovery is attempted automatically after age threshold is reached:
    1. Enter key nudge (command may just need Enter to submit)
    2. Full command retry (if recoverable from session name)
    Recovery can be re-attempted after RECOVERY_COOLDOWN if still stuck.

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

# Check for progress by comparing tmux pane content hash and worktree file changes.
# Returns 0 if progress detected, 1 if no change.
#
# Progress signals (any one resets the idle timer):
#   1. Tmux pane content changed (agent produced visible output)
#   2. Worktree has modified/new files (agent is writing code even if pane is static)
check_progress() {
    local name="$1"
    local session_name="$2"

    local progress_file="$PROGRESS_DIR/${name}"
    local hash_file="${progress_file}.hash"
    local time_file="${progress_file}.time"
    local worktree_hash_file="${progress_file}.worktree_hash"

    local progress_detected=false

    # Signal 1: Tmux pane content changed
    local current_content
    current_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || echo "")

    if [[ -n "$current_content" ]]; then
        local current_hash
        current_hash=$(echo "$current_content" | md5 -q 2>/dev/null || echo "$current_content" | md5sum 2>/dev/null | cut -d' ' -f1)

        local last_hash=""
        if [[ -f "$hash_file" ]]; then
            last_hash=$(cat "$hash_file" 2>/dev/null || echo "")
        fi

        if [[ "$current_hash" != "$last_hash" ]]; then
            echo "$current_hash" > "$hash_file"
            progress_detected=true
        fi
    fi

    # Signal 2: Worktree file changes (if --worktree was provided)
    # Uses git status hash to detect new/modified files without expensive I/O.
    if [[ -n "${worktree:-}" ]] && [[ -d "${worktree}" ]]; then
        local wt_status
        wt_status=$(git -C "$worktree" status --porcelain 2>/dev/null || echo "")
        local wt_hash
        wt_hash=$(echo "$wt_status" | md5 -q 2>/dev/null || echo "$wt_status" | md5sum 2>/dev/null | cut -d' ' -f1)

        local last_wt_hash=""
        if [[ -f "$worktree_hash_file" ]]; then
            last_wt_hash=$(cat "$worktree_hash_file" 2>/dev/null || echo "")
        fi

        if [[ "$wt_hash" != "$last_wt_hash" ]]; then
            echo "$wt_hash" > "$worktree_hash_file"
            progress_detected=true
        fi
    fi

    if [[ "$progress_detected" == "true" ]]; then
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

    # Check for interactive theme/style picker prompts from Claude Code onboarding.
    # These appear when CLAUDE_CONFIG_DIR is isolated without .claude.json.
    # Patterns: "Choose the text style", "Choose a theme", numbered option lines like "❯ 1. Dark mode"
    STUCK_AT_THEME_PICKER=false
    if echo "$pane_content" | grep -qE '(Choose the text style|Choose a theme|❯[[:space:]]*[0-9]+\.)'; then
        command_at_prompt=true
        STUCK_AT_THEME_PICKER=true
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

    # Theme picker stuck: Enter won't help, kill session so it can be respawned
    if [[ "${STUCK_AT_THEME_PICKER:-false}" == "true" ]]; then
        log_warn "Theme picker detected in $session_name - killing session for respawn"
        tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true
        return 1  # Signal failure so caller knows session was killed
    fi

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
    rm -f "$PROGRESS_DIR/${name}.worktree_hash"
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
    local contract_interval_override=""  # Empty = use adaptive, 0 = disable, >0 = fixed
    local phase=""
    local worktree=""
    local pr_number=""
    local min_session_age=""
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
                contract_interval_override="$2"
                shift 2
                ;;
            --phase)
                phase="$2"
                shift 2
                ;;
            --max-idle)
                STUCK_CRITICAL_THRESHOLD="$2"
                STUCK_ACTION="retry"
                shift 2
                ;;
            --min-session-age)
                min_session_age="$2"
                shift 2
                ;;
            --min-idle-elapsed)
                # Deprecated alias for --min-session-age
                min_session_age="$2"
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

    # Apply contract interval override from CLI arg or environment variable
    if [[ -n "$contract_interval_override" ]]; then
        CONTRACT_INTERVAL_OVERRIDE="$contract_interval_override"
    fi

    log_info "Waiting for agent '$name' with signal checking (poll: ${poll_interval}s, timeout: ${timeout}s)"
    if [[ -n "$task_id" ]]; then
        log_info "Heartbeat emission: every ${DEFAULT_HEARTBEAT_INTERVAL}s (task-id: $task_id)"
    fi
    if [[ -n "$phase" ]]; then
        if [[ "${CONTRACT_INTERVAL_OVERRIDE:-0}" -eq 0 ]]; then
            log_info "Proactive contract checking: adaptive intervals for phase '$phase' (initial delay: ${CONTRACT_INITIAL_DELAY}s)"
        elif [[ "${CONTRACT_INTERVAL_OVERRIDE:-0}" -gt 0 ]]; then
            log_info "Proactive contract checking: fixed ${CONTRACT_INTERVAL_OVERRIDE}s interval for phase '$phase'"
        else
            log_info "Proactive contract checking: disabled for phase '$phase'"
        fi
    fi
    log_info "Stuck detection: warning=${STUCK_WARNING_THRESHOLD}s, critical=${STUCK_CRITICAL_THRESHOLD}s, action=${STUCK_ACTION}"
    log_info "Prompt stuck detection: check_interval=${PROMPT_STUCK_CHECK_INTERVAL}s, age_threshold=${PROMPT_STUCK_AGE_THRESHOLD}s, recovery_cooldown=${PROMPT_STUCK_RECOVERY_COOLDOWN}s"

    # Launch agent-wait.sh in the background with stdout redirected to a temp file.
    # This prevents the child from writing JSON to the parent's stdout, which would
    # produce two JSON objects when both scripts detect completion (issue #1792).
    local wait_output
    wait_output=$(mktemp "${TMPDIR:-/tmp}/agent-wait-output.XXXXXX")
    local wait_cmd=("${SCRIPT_DIR}/agent-wait.sh" "$name" --timeout "$timeout" --poll-interval "$poll_interval" --json)
    if [[ -n "$min_session_age" ]]; then
        wait_cmd+=(--min-session-age "$min_session_age")
    fi
    "${wait_cmd[@]}" > "$wait_output" &
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
    local last_prompt_stuck_check=$start_time
    local prompt_stuck_since=0                  # When stuck state was first detected (0 = not stuck)
    local prompt_stuck_recovery_attempted=false
    local prompt_stuck_recovery_time=0          # When last recovery was attempted
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
                # Pass through agent-wait.sh's JSON from the temp file (not shared stdout)
                cat "$wait_output"
            fi
            rm -f "$wait_output"
            exit "$exit_code"
        fi

        # Check for shutdown signals
        if check_signals "$issue"; then
            local elapsed=$(( $(date +%s) - start_time ))

            # Kill the background wait process
            kill "$wait_pid" 2>/dev/null || true
            wait "$wait_pid" 2>/dev/null || true

            # Clean up progress files and temp output
            cleanup_progress_files "$name"
            rm -f "$wait_output"

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

        # Check shepherd progress file for errored status (fast error detection)
        # When loom-shepherd reports an error milestone, the progress file status
        # is set to "errored". Detecting this here terminates the session within one
        # poll cycle (~5s) rather than waiting for idle heuristics (issue #1619).
        if [[ -n "$task_id" ]]; then
            local progress_file="$REPO_ROOT/.loom/progress/shepherd-${task_id}.json"
            if [[ -f "$progress_file" ]]; then
                local progress_status
                progress_status=$(jq -r '.status // "working"' "$progress_file" 2>/dev/null || echo "working")
                if [[ "$progress_status" == "errored" ]]; then
                    local elapsed=$(( $(date +%s) - start_time ))
                    log_warn "Shepherd errored (progress file status), terminating session '$session_name'"

                    # Kill the background wait process
                    kill "$wait_pid" 2>/dev/null || true
                    wait "$wait_pid" 2>/dev/null || true

                    # Clean up progress files and temp output
                    cleanup_progress_files "$name"
                    rm -f "$wait_output"

                    # Destroy the tmux session
                    tmux -L "$TMUX_SOCKET" kill-session -t "$session_name" 2>/dev/null || true

                    if [[ "$json_output" == "true" ]]; then
                        echo "{\"status\":\"errored\",\"name\":\"$name\",\"reason\":\"progress_file_errored\",\"elapsed\":$elapsed}"
                    fi
                    exit 4
                fi
            fi
        fi

        # Fast "stuck at prompt" detection - command visible but not processing
        # This failure mode can be detected faster than the 5min general stuck threshold.
        #
        # Key variables:
        #   prompt_stuck_since: timestamp when stuck state was first detected (0 = not stuck)
        #   prompt_stuck_recovery_attempted: whether recovery was tried this stuck episode
        #   prompt_stuck_recovery_time: timestamp of last recovery attempt
        #
        # Detection fires when stuck for >= PROMPT_STUCK_AGE_THRESHOLD seconds.
        # We check at PROMPT_STUCK_CHECK_INTERVAL frequency for responsiveness.
        local now
        now=$(date +%s)
        local since_last_prompt_check=$((now - last_prompt_stuck_check))

        if [[ "$since_last_prompt_check" -ge "$PROMPT_STUCK_CHECK_INTERVAL" ]]; then
            last_prompt_stuck_check=$now

            if check_stuck_at_prompt "$session_name"; then
                # Agent appears stuck at prompt
                if [[ "$prompt_stuck_since" -eq 0 ]]; then
                    # First detection of stuck state
                    prompt_stuck_since=$now
                    log_info "Checking for stuck-at-prompt (first detection, waiting for age threshold)"
                fi

                local stuck_duration=$((now - prompt_stuck_since))

                # Check if recovery cooldown has elapsed (allow re-attempt)
                local since_recovery=0
                if [[ "$prompt_stuck_recovery_time" -gt 0 ]]; then
                    since_recovery=$((now - prompt_stuck_recovery_time))
                fi
                local recovery_allowed=true
                if [[ "$prompt_stuck_recovery_attempted" == "true" ]] && [[ "$since_recovery" -lt "$PROMPT_STUCK_RECOVERY_COOLDOWN" ]]; then
                    recovery_allowed=false
                fi

                # Only take action if stuck for >= threshold AND recovery is allowed
                if [[ "$stuck_duration" -ge "$PROMPT_STUCK_AGE_THRESHOLD" ]] && [[ "$recovery_allowed" == "true" ]]; then
                    local elapsed=$(( now - start_time ))
                    log_warn "Agent stuck at prompt for ${stuck_duration}s (total elapsed: ${elapsed}s) - attempting recovery"

                    # Attempt recovery
                    prompt_stuck_recovery_attempted=true
                    prompt_stuck_recovery_time=$now

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
                        # Reset stuck tracking on successful recovery
                        prompt_stuck_since=0
                        prompt_stuck_recovery_attempted=false
                    else
                        # Recovery failed - log with timing info for debugging
                        local remaining_cooldown=$((PROMPT_STUCK_RECOVERY_COOLDOWN - since_recovery))
                        if [[ "$remaining_cooldown" -lt 0 ]]; then
                            remaining_cooldown=0
                        fi
                        log_warn "Stuck-at-prompt recovery failed - will retry after ${PROMPT_STUCK_RECOVERY_COOLDOWN}s cooldown"
                    fi
                elif [[ "$stuck_duration" -lt "$PROMPT_STUCK_AGE_THRESHOLD" ]]; then
                    # Still within initial detection period
                    local remaining=$((PROMPT_STUCK_AGE_THRESHOLD - stuck_duration))
                    log_info "Agent may be stuck at prompt (${stuck_duration}s/${PROMPT_STUCK_AGE_THRESHOLD}s threshold, ${remaining}s until detection)"
                fi
            else
                # Agent is not stuck at prompt - reset tracking
                if [[ "$prompt_stuck_since" -gt 0 ]]; then
                    log_info "Agent no longer stuck at prompt - resetting tracking"
                fi
                prompt_stuck_since=0
                prompt_stuck_recovery_attempted=false
            fi
        fi

        # Also check for processing indicators to reset stuck tracking (even between checks)
        # This provides faster recovery detection if agent starts processing
        if [[ "$prompt_stuck_since" -gt 0 ]]; then
            local pane_content
            pane_content=$(tmux -L "$TMUX_SOCKET" capture-pane -t "$session_name" -p 2>/dev/null || true)
            if echo "$pane_content" | grep -qE "$PROCESSING_INDICATORS"; then
                # Agent is now processing - reset all stuck tracking
                log_info "Agent now processing - resetting stuck-at-prompt tracking"
                prompt_stuck_since=0
                prompt_stuck_recovery_attempted=false
            fi
        fi

        # Proactive phase contract checking with adaptive intervals (issue #1678)
        # This detects completion within one interval of actual work finishing,
        # rather than waiting for the idle timeout to trigger (see issue #1581).
        # Intervals start long (90s) and decrease over time (down to 10s) to balance
        # detection latency against GitHub API cost.
        if [[ "$completion_detected" != "true" ]] && [[ -n "$phase" ]]; then
            local now
            now=$(date +%s)
            local elapsed=$((now - start_time))
            local adaptive_interval
            adaptive_interval=$(get_adaptive_contract_interval "$elapsed")

            # adaptive_interval of 0 means "skip this check" (during initial delay)
            if [[ "$adaptive_interval" -gt 0 ]]; then
                local since_last_check=$((now - last_contract_check))

                if [[ "$since_last_check" -ge "$adaptive_interval" ]]; then
                    last_contract_check=$now

                    if check_phase_contract "$phase" "$issue" "$worktree" "$pr_number" "check_only"; then
                        completion_detected=true
                        COMPLETION_REASON="phase_contract_satisfied"

                        log_info "Phase contract satisfied ($CONTRACT_STATUS) via proactive check (interval: ${adaptive_interval}s)"
                        log_info "Agent completed work but didn't exit - terminating session"
                    fi
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

            # Clean up progress files and discard child's partial output
            cleanup_progress_files "$name"
            rm -f "$wait_output"

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

                    # Clean up progress files and temp output
                    cleanup_progress_files "$name"
                    rm -f "$wait_output"

                    exit 4
                fi
            fi
        fi

        # Emit periodic heartbeat to keep shepherd progress file fresh (issue #1586).
        # Without this, long-running phases (builder, doctor) cause the progress file's
        # last_heartbeat to go stale, triggering false positives in the snapshot
        # and stuck-detection systems which use a 120s stale threshold.
        if [[ -n "$task_id" ]] && [[ -x "$SCRIPT_DIR/report-milestone.sh" ]]; then
            local now
            now=$(date +%s)
            local since_last_heartbeat=$((now - last_heartbeat_time))

            if [[ "$since_last_heartbeat" -ge "$DEFAULT_HEARTBEAT_INTERVAL" ]]; then
                last_heartbeat_time=$now
                local elapsed=$((now - start_time))
                local elapsed_min=$((elapsed / 60))
                local phase_desc="${phase:-agent}"

                # Check for wrapper retry state (issue #2296).
                # When claude-wrapper.sh is in exponential backoff, report that
                # instead of the generic "running" heartbeat so operators can
                # distinguish "wrapper retrying" from "claude actively working".
                local heartbeat_action="${phase_desc} running (${elapsed_min}m elapsed)"
                local retry_state_file="${REPO_ROOT}/.loom/retry-state/${name}.json"
                if [[ -f "$retry_state_file" ]]; then
                    local retry_status retry_attempt retry_max
                    retry_status=$(jq -r '.status // ""' "$retry_state_file" 2>/dev/null || echo "")
                    retry_attempt=$(jq -r '.attempt // 0' "$retry_state_file" 2>/dev/null || echo "0")
                    retry_max=$(jq -r '.max_retries // 0' "$retry_state_file" 2>/dev/null || echo "0")
                    if [[ "$retry_status" == "backoff" ]]; then
                        heartbeat_action="${phase_desc} wrapper retrying (attempt ${retry_attempt}/${retry_max}, ${elapsed_min}m elapsed)"
                        log_warn "Wrapper in backoff: attempt ${retry_attempt}/${retry_max} for '${name}'"
                    fi
                fi

                "$SCRIPT_DIR/report-milestone.sh" heartbeat \
                    --task-id "$task_id" \
                    --action "$heartbeat_action" \
                    --quiet 2>/dev/null || true
            fi
        fi

        sleep "$poll_interval"
    done
}

main "$@"
