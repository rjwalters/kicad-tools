#!/usr/bin/env bash
# Loom Daemon Loop - Shell script wrapper for robust continuous operation
#
# This script implements the "thin parent loop" from loom.md in bash,
# delegating iteration work to Claude via the /loom iterate command.
#
# Usage:
#   ./.loom/scripts/daemon-loop.sh [--force] [--debug] [--status] [--health]
#
# Options:
#   --force    Enable force mode for aggressive autonomous development
#   --debug    Enable debug mode for verbose subagent troubleshooting
#   --status   Check if daemon loop is running
#   --health   Show daemon health status and exit
#
# Environment Variables:
#   LOOM_POLL_INTERVAL - Seconds between iterations (default: 120)
#   LOOM_ITERATION_TIMEOUT - Max seconds per iteration (default: 300)
#   LOOM_MAX_BACKOFF - Maximum backoff interval in seconds (default: 1800)
#   LOOM_BACKOFF_MULTIPLIER - Backoff multiplier on failure (default: 2)
#   LOOM_BACKOFF_THRESHOLD - Failures before backoff kicks in (default: 3)
#   LOOM_SLOW_ITERATION_THRESHOLD_MULTIPLIER - Multiplier of rolling average to trigger slow warning (default: 2)
#
# Features:
#   - Deterministic loop behavior (no LLM interpretation variability)
#   - Configurable poll interval via environment variable
#   - Timeout protection prevents hung iterations
#   - Exponential backoff on repeated failures (configurable)
#   - All output logged to .loom/daemon.log
#   - Graceful shutdown via .loom/stop-daemon signal file
#   - Session state rotation on startup
#   - Force mode support passed to iterations
#   - PID file prevents multiple instances (.loom/daemon-loop.pid)
#   - Iteration metrics and health reporting (.loom/daemon-metrics.json)
#
# Example:
#   # Start daemon with default settings
#   ./.loom/scripts/daemon-loop.sh
#
#   # Start in force mode with custom interval
#   LOOM_POLL_INTERVAL=60 ./.loom/scripts/daemon-loop.sh --force
#
#   # Run in background
#   nohup ./.loom/scripts/daemon-loop.sh --force > /dev/null 2>&1 &
#
#   # Check if daemon is running
#   ./.loom/scripts/daemon-loop.sh --status
#
#   # Check daemon health
#   ./.loom/scripts/daemon-loop.sh --health
#
#   # Stop daemon gracefully
#   touch .loom/stop-daemon

set -euo pipefail

# Configuration
POLL_INTERVAL="${LOOM_POLL_INTERVAL:-120}"
ITERATION_TIMEOUT="${LOOM_ITERATION_TIMEOUT:-300}"
MAX_BACKOFF="${LOOM_MAX_BACKOFF:-1800}"
BACKOFF_MULTIPLIER="${LOOM_BACKOFF_MULTIPLIER:-2}"
BACKOFF_THRESHOLD="${LOOM_BACKOFF_THRESHOLD:-3}"
SLOW_ITERATION_THRESHOLD_MULTIPLIER="${LOOM_SLOW_ITERATION_THRESHOLD_MULTIPLIER:-2}"
LOG_FILE=".loom/daemon.log"
STATE_FILE=".loom/daemon-state.json"
METRICS_FILE=".loom/daemon-metrics.json"
STOP_SIGNAL=".loom/stop-daemon"
PID_FILE=".loom/daemon-loop.pid"
SESSION_ID="$(date +%s)-$$"  # Unique session ID: timestamp-PID

# ANSI colors (disabled if not a terminal)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    BLUE='\033[0;34m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    BLUE=''
    YELLOW=''
    CYAN=''
    NC=''
fi

# Parse arguments
FORCE_FLAG=""
DEBUG_FLAG=""
SHOW_HEALTH=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --force|-f)
            FORCE_FLAG="--force"
            shift
            ;;
        --debug|-d)
            DEBUG_FLAG="--debug"
            shift
            ;;
        --status)
            if [[ -f "$PID_FILE" ]]; then
                pid=$(cat "$PID_FILE")
                if kill -0 "$pid" 2>/dev/null; then
                    echo -e "${GREEN}Daemon loop running (PID: $pid)${NC}"
                    # Show session ID from state file if available
                    if [[ -f "$STATE_FILE" ]]; then
                        session_id=$(jq -r '.daemon_session_id // "unknown"' "$STATE_FILE" 2>/dev/null)
                        echo -e "  Session ID: $session_id"
                    fi
                    exit 0
                else
                    echo -e "${YELLOW}Daemon loop not running (stale PID file)${NC}"
                    rm -f "$PID_FILE"
                    exit 1
                fi
            else
                echo "Daemon loop not running"
                exit 1
            fi
            ;;
        --health)
            SHOW_HEALTH=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--force] [--debug] [--status] [--health]"
            echo ""
            echo "Options:"
            echo "  --force, -f    Enable force mode for aggressive autonomous development"
            echo "  --debug, -d    Enable debug mode for verbose subagent troubleshooting"
            echo "  --status       Check if daemon loop is running"
            echo "  --health       Show daemon health status and exit"
            echo "  --help, -h     Show this help message"
            echo ""
            echo "Environment Variables:"
            echo "  LOOM_POLL_INTERVAL      Seconds between iterations (default: 120)"
            echo "  LOOM_ITERATION_TIMEOUT  Max seconds per iteration (default: 300)"
            echo "  LOOM_MAX_BACKOFF        Maximum backoff interval in seconds (default: 1800)"
            echo "  LOOM_BACKOFF_MULTIPLIER Backoff multiplier on failure (default: 2)"
            echo "  LOOM_BACKOFF_THRESHOLD  Failures before backoff kicks in (default: 3)"
            echo ""
            echo "To stop the daemon gracefully:"
            echo "  touch .loom/stop-daemon"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

# Ensure we're in a git repository with .loom directory
if [[ ! -d ".loom" ]]; then
    echo -e "${RED}Error: .loom directory not found${NC}" >&2
    echo "Run this script from a Loom-enabled repository root" >&2
    exit 1
fi

# Handle --health flag early (before other checks that might modify state)
if [[ "$SHOW_HEALTH" == true ]]; then
    if [[ ! -f "$METRICS_FILE" ]]; then
        echo "Daemon: not running (no metrics file)"
        exit 1
    fi
    # Check for running daemon via PID file (more reliable than state file)
    running_status="stopped"
    if [[ -f "$PID_FILE" ]]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            running_status="running (PID: $pid)"
        fi
    fi
    # Extract and display metrics
    health_status=$(jq -r '.health.status // "unknown"' "$METRICS_FILE" 2>/dev/null || echo "unknown")
    total_iterations=$(jq -r '.total_iterations // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    consecutive_failures=$(jq -r '.health.consecutive_failures // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    avg_duration=$(jq -r '.average_iteration_seconds // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    last_status=$(jq -r '.last_iteration.status // "none"' "$METRICS_FILE" 2>/dev/null || echo "none")
    last_duration=$(jq -r '.last_iteration.duration_seconds // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    # Calculate success rate
    if [[ "$total_iterations" -gt 0 ]]; then
        successful=$(jq -r '.successful_iterations // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
        success_rate=$(( (successful * 100) / total_iterations ))
    else
        success_rate="n/a"
    fi
    # Format health status with failure count if unhealthy
    health_display="$health_status"
    if [[ "$health_status" == "unhealthy" ]]; then
        health_display="$health_status ($consecutive_failures consecutive failures)"
    fi
    echo "Daemon: $running_status"
    echo "Health: $health_display"
    echo "Iterations: $total_iterations (${success_rate}% success)"
    echo "Avg duration: ${avg_duration}s"
    echo "Last iteration: $last_status (${last_duration}s)"

    # Show health monitoring metrics if available
    if [[ -f ".loom/health-metrics.json" ]]; then
        health_score=$(jq -r '.health_score // "?"' .loom/health-metrics.json 2>/dev/null || echo "?")
        health_monitor_status=$(jq -r '.health_status // "?"' .loom/health-metrics.json 2>/dev/null || echo "?")
        echo "Health score: ${health_score}/100 (${health_monitor_status})"
    fi

    # Show unacknowledged alerts if any
    if [[ -f ".loom/alerts.json" ]]; then
        unack_count=$(jq -r '[.alerts[] | select(.acknowledged == false)] | length' .loom/alerts.json 2>/dev/null || echo "0")
        if [[ "$unack_count" -gt 0 ]]; then
            echo "Alerts: $unack_count unacknowledged"
        fi
    fi

    # Exit with appropriate code
    if [[ "$health_status" == "unhealthy" ]]; then
        exit 2
    fi
    exit 0
fi

# Check for existing daemon instance
if [[ -f "$PID_FILE" ]]; then
    existing_pid=$(cat "$PID_FILE")
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo -e "${RED}Error: Daemon loop already running (PID: $existing_pid)${NC}" >&2
        echo "Use --status to check status or stop the existing daemon first" >&2
        exit 1
    else
        echo -e "${YELLOW}Removing stale PID file${NC}"
        rm -f "$PID_FILE"
    fi
fi

# Write PID file
echo $$ > "$PID_FILE"

# Check for claude CLI (only needed when actually running the daemon)
if ! command -v claude &> /dev/null; then
    echo -e "${RED}Error: 'claude' CLI not found in PATH${NC}" >&2
    echo "Install Claude Code CLI: https://claude.ai/code" >&2
    exit 1
fi

# Rotate existing state file if present
if [[ -f "./.loom/scripts/rotate-daemon-state.sh" ]] && [[ -f "$STATE_FILE" ]]; then
    echo -e "${BLUE}Rotating previous daemon state...${NC}"
    ./.loom/scripts/rotate-daemon-state.sh 2>/dev/null || true
fi

# Rotate existing metrics file if present (alongside daemon state rotation)
# This ensures metrics history is preserved when daemon restarts
if [[ -f "$METRICS_FILE" ]]; then
    # Archive metrics with timestamp if there's meaningful data
    metrics_iterations=$(jq -r '.total_iterations // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    if [[ "$metrics_iterations" -gt 0 ]]; then
        archive_timestamp=$(date +%Y%m%d-%H%M%S)
        archive_name=".loom/daemon-metrics-${archive_timestamp}.json"
        cp "$METRICS_FILE" "$archive_name" 2>/dev/null || true
        echo -e "${BLUE}Archived previous metrics to: $archive_name${NC}"

        # Prune old metrics archives (keep last 10)
        metrics_archives=$(find .loom -maxdepth 1 -name 'daemon-metrics-*.json' 2>/dev/null | sort -r)
        archive_count=$(echo "$metrics_archives" | grep -c . || echo "0")
        if [[ "$archive_count" -gt 10 ]]; then
            echo "$metrics_archives" | tail -n +11 | xargs rm -f 2>/dev/null || true
        fi
    fi
fi

# Create log directory if needed
mkdir -p "$(dirname "$LOG_FILE")"

# Log function that writes to both console and file
log() {
    local timestamp
    timestamp=$(date -Iseconds)
    echo -e "$timestamp $*" | tee -a "$LOG_FILE"
}

log_header() {
    local mode_display="Normal"
    if [[ -n "$FORCE_FLAG" ]] && [[ -n "$DEBUG_FLAG" ]]; then
        mode_display="Force + Debug"
    elif [[ -n "$FORCE_FLAG" ]]; then
        mode_display="Force"
    elif [[ -n "$DEBUG_FLAG" ]]; then
        mode_display="Debug"
    fi
    echo "" | tee -a "$LOG_FILE"
    echo "═══════════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"
    echo -e "${CYAN}  LOOM DAEMON - SHELL SCRIPT WRAPPER MODE${NC}" | tee -a "$LOG_FILE"
    echo "═══════════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"
    echo "  Started: $(date -Iseconds)" | tee -a "$LOG_FILE"
    echo "  PID: $$" | tee -a "$LOG_FILE"
    echo "  Session ID: $SESSION_ID" | tee -a "$LOG_FILE"
    echo "  Mode: $mode_display" | tee -a "$LOG_FILE"
    echo "  Poll interval: ${POLL_INTERVAL}s" | tee -a "$LOG_FILE"
    echo "  Iteration timeout: ${ITERATION_TIMEOUT}s" | tee -a "$LOG_FILE"
    echo "  Max backoff: ${MAX_BACKOFF}s (after ${BACKOFF_THRESHOLD} failures, ${BACKOFF_MULTIPLIER}x multiplier)" | tee -a "$LOG_FILE"
    echo "  PID file: $PID_FILE" | tee -a "$LOG_FILE"
    echo "  Metrics file: $METRICS_FILE" | tee -a "$LOG_FILE"
    echo "  Stop signal: $STOP_SIGNAL" | tee -a "$LOG_FILE"
    echo "═══════════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"
    echo "" | tee -a "$LOG_FILE"
}

# Initialize metrics file for a new daemon session
init_metrics() {
    local timestamp
    timestamp=$(date -Iseconds)
    cat > "$METRICS_FILE" <<EOF
{
  "session_start": "$timestamp",
  "total_iterations": 0,
  "successful_iterations": 0,
  "failed_iterations": 0,
  "timeout_iterations": 0,
  "iteration_durations": [],
  "average_iteration_seconds": 0,
  "last_iteration": null,
  "health": {
    "status": "healthy",
    "consecutive_failures": 0,
    "last_success": null
  }
}
EOF
}

# Update metrics after each iteration
# Usage: update_metrics <status> <duration> <summary>
# status: "success", "failure", or "timeout"
update_metrics() {
    local status="$1"
    local duration="$2"
    local summary="$3"
    local timestamp
    timestamp=$(date -Iseconds)

    # Initialize metrics file if it doesn't exist
    if [[ ! -f "$METRICS_FILE" ]]; then
        init_metrics
    fi

    # Use jq to update metrics atomically
    local temp_file
    temp_file=$(mktemp)

    if jq --arg status "$status" \
       --arg duration "$duration" \
       --arg summary "$summary" \
       --arg timestamp "$timestamp" '
       .total_iterations += 1 |
       .last_iteration = {
           timestamp: $timestamp,
           duration_seconds: ($duration | tonumber),
           status: $status,
           summary: $summary
       } |
       if $status == "success" then
           .successful_iterations += 1 |
           .health.consecutive_failures = 0 |
           .health.last_success = $timestamp |
           .health.status = "healthy"
       elif $status == "timeout" then
           .timeout_iterations += 1 |
           .health.consecutive_failures += 1
       else
           .failed_iterations += 1 |
           .health.consecutive_failures += 1
       end |
       .iteration_durations = (.iteration_durations + [($duration | tonumber)])[-100:] |
       .average_iteration_seconds = (if (.iteration_durations | length) > 0 then ((.iteration_durations | add) / (.iteration_durations | length) | floor) else 0 end) |
       if .health.consecutive_failures >= 3 then .health.status = "unhealthy" else . end
    ' "$METRICS_FILE" > "$temp_file" 2>/dev/null; then
        mv "$temp_file" "$METRICS_FILE"
    else
        # jq failed, log warning but don't crash daemon
        rm -f "$temp_file"
        log "${YELLOW}Warning: Failed to update metrics file${NC}"
    fi
}

# Update iteration timing summary in daemon-state.json
# Reads rolling data from daemon-metrics.json (source of truth) and writes
# a summary view to daemon-state.json for observability/debugging.
# Usage: update_state_timing
update_state_timing() {
    if [[ ! -f "$STATE_FILE" ]] || [[ ! -f "$METRICS_FILE" ]]; then
        return
    fi

    local temp_file
    temp_file=$(mktemp)

    # Read timing data from metrics file and write summary to state file
    local last_duration avg_duration max_duration
    last_duration=$(jq -r '.last_iteration.duration_seconds // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    avg_duration=$(jq -r '.average_iteration_seconds // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    max_duration=$(jq -r '[.iteration_durations[] // 0] | max // 0' "$METRICS_FILE" 2>/dev/null || echo "0")

    if jq --argjson last "$last_duration" \
       --argjson avg "$avg_duration" \
       --argjson max "$max_duration" '
       .iteration_timing = {
           last_duration_seconds: $last,
           avg_duration_seconds: $avg,
           max_duration_seconds: $max
       }
    ' "$STATE_FILE" > "$temp_file" 2>/dev/null; then
        mv "$temp_file" "$STATE_FILE"
    else
        rm -f "$temp_file"
    fi
}

# Check for slow iteration and log warning if duration exceeds threshold
# Usage: check_slow_iteration <duration>
check_slow_iteration() {
    local duration="$1"

    if [[ ! -f "$METRICS_FILE" ]]; then
        return
    fi

    local avg_duration total_iterations
    avg_duration=$(jq -r '.average_iteration_seconds // 0' "$METRICS_FILE" 2>/dev/null || echo "0")
    total_iterations=$(jq -r '.total_iterations // 0' "$METRICS_FILE" 2>/dev/null || echo "0")

    # Need at least 3 iterations for a meaningful average
    if [[ "$total_iterations" -lt 3 ]]; then
        return
    fi

    # Skip if average is zero (avoid division issues)
    if [[ "$avg_duration" -eq 0 ]]; then
        return
    fi

    local threshold=$((avg_duration * SLOW_ITERATION_THRESHOLD_MULTIPLIER))
    if [[ "$duration" -gt "$threshold" ]]; then
        log "${YELLOW}WARNING: Slow iteration detected - ${duration}s exceeds ${SLOW_ITERATION_THRESHOLD_MULTIPLIER}x average (${avg_duration}s, threshold: ${threshold}s)${NC}"
    fi
}

# Cleanup function called on exit
cleanup() {
    local exit_code=$?
    echo "" | tee -a "$LOG_FILE"
    log "${YELLOW}Daemon loop terminated (exit code: $exit_code)${NC}"
    rm -f "$STOP_SIGNAL"
    rm -f "$PID_FILE"

    # Update state file to mark as not running
    if [[ -f "$STATE_FILE" ]]; then
        # Use a temp file to avoid corrupting state on write failure
        local temp_file
        temp_file=$(mktemp)
        if jq '.running = false | .stopped_at = "'"$(date -Iseconds)"'"' "$STATE_FILE" > "$temp_file" 2>/dev/null; then
            mv "$temp_file" "$STATE_FILE"
        else
            rm -f "$temp_file"
        fi
    fi
}

trap cleanup EXIT SIGINT SIGTERM

# Clear any existing stop signal
rm -f "$STOP_SIGNAL"

# Print startup header
log_header

# Initialize metrics for new session
init_metrics
log "Metrics file initialized: $METRICS_FILE"

# Initialize daemon state file with force_mode flag
# This allows other roles (especially Champion) to detect force mode
init_daemon_state() {
    local timestamp
    timestamp=$(date -Iseconds)
    local force_mode_value="false"
    if [[ -n "$FORCE_FLAG" ]]; then
        force_mode_value="true"
    fi

    # If state file exists, update force_mode; otherwise create new state
    if [[ -f "$STATE_FILE" ]]; then
        local temp_file
        temp_file=$(mktemp)
        if jq --arg force_mode "$force_mode_value" \
              --arg started_at "$timestamp" \
              --arg session_id "$SESSION_ID" \
              '.force_mode = ($force_mode == "true") | .started_at = $started_at | .running = true | .iteration = 0 | .daemon_session_id = $session_id' \
              "$STATE_FILE" > "$temp_file" 2>/dev/null; then
            mv "$temp_file" "$STATE_FILE"
        else
            rm -f "$temp_file"
            # Fall back to creating new state file
            create_fresh_state "$force_mode_value" "$timestamp"
        fi
    else
        create_fresh_state "$force_mode_value" "$timestamp"
    fi
}

# Create a fresh daemon state file
create_fresh_state() {
    local force_mode_value="$1"
    local timestamp="$2"
    cat > "$STATE_FILE" <<EOF
{
  "started_at": "$timestamp",
  "last_poll": null,
  "running": true,
  "iteration": 0,
  "force_mode": $force_mode_value,
  "daemon_session_id": "$SESSION_ID",
  "shepherds": {},
  "completed_issues": [],
  "total_prs_merged": 0
}
EOF
}

init_daemon_state
if [[ -n "$FORCE_FLAG" ]]; then
    log "${YELLOW}Force mode enabled - stored in daemon-state.json${NC}"
fi
log "Session ID: $SESSION_ID"

# Validate session ownership of state file
# Returns 0 if we own the session, 1 if another daemon took over
validate_session_ownership() {
    if [[ ! -f "$STATE_FILE" ]]; then
        return 0  # No state file = we're the only daemon
    fi

    local file_session_id
    file_session_id=$(jq -r '.daemon_session_id // empty' "$STATE_FILE" 2>/dev/null)

    if [[ -n "$file_session_id" ]] && [[ "$file_session_id" != "$SESSION_ID" ]]; then
        return 1  # Another daemon has taken over
    fi

    return 0
}

iteration=0
consecutive_failures=0
current_backoff=$POLL_INTERVAL

# Main loop
while true; do
    iteration=$((iteration + 1))

    # Check for stop signal
    if [[ -f "$STOP_SIGNAL" ]]; then
        log "${YELLOW}Iteration $iteration: SHUTDOWN_SIGNAL detected${NC}"
        break
    fi

    # Validate session ownership before each iteration
    # Detects if another daemon has taken over the state file
    if ! validate_session_ownership; then
        file_session_id=$(jq -r '.daemon_session_id // "unknown"' "$STATE_FILE" 2>/dev/null)
        log "${RED}SESSION CONFLICT: Another daemon has taken over the state file${NC}"
        log "${RED}  Our session:    $SESSION_ID${NC}"
        log "${RED}  File session:   $file_session_id${NC}"
        log "${RED}  Yielding to the other daemon instance. Exiting.${NC}"
        break
    fi

    # Run iteration via Claude
    timestamp=$(date -Iseconds)
    iteration_start=$(date +%s)
    log "${BLUE}Iteration $iteration: Starting...${NC}"

    # Build the command
    ITERATE_CMD="/loom iterate"
    if [[ -n "$FORCE_FLAG" ]]; then
        ITERATE_CMD="$ITERATE_CMD $FORCE_FLAG"
    fi
    if [[ -n "$DEBUG_FLAG" ]]; then
        ITERATE_CMD="$ITERATE_CMD $DEBUG_FLAG"
    fi

    # Track iteration status for metrics
    iteration_status="success"

    # Capture iteration output with timeout
    # Using timeout command to prevent hung iterations
    # --print flag outputs the response without interactive UI
    if output=$(timeout "$ITERATION_TIMEOUT" claude --print "$ITERATE_CMD" 2>&1); then
        # Extract the summary line (looks for ready=X building=Y pattern)
        summary=$(echo "$output" | grep -E '^ready=' | tail -1 || echo "")

        # If no summary pattern found, look for other indicators
        if [[ -z "$summary" ]]; then
            if echo "$output" | grep -qi "shutdown"; then
                summary="SHUTDOWN_SIGNAL"
            elif echo "$output" | grep -qi "error"; then
                # Extract first error line
                summary="ERROR: $(echo "$output" | grep -i "error" | head -1 | cut -c1-80)"
                iteration_status="failure"
            elif echo "$output" | grep -qi "complete\|success\|done"; then
                summary="completed"
            else
                # Take last non-empty line as summary
                summary=$(echo "$output" | grep -v '^$' | tail -1 | cut -c1-80 || echo "no output")
            fi
        fi
    else
        exit_code=$?
        if [[ $exit_code -eq 124 ]]; then
            summary="TIMEOUT (iteration exceeded ${ITERATION_TIMEOUT}s)"
            iteration_status="timeout"
            log "${RED}Iteration $iteration: $summary${NC}"
        else
            summary="ERROR (exit code: $exit_code)"
            iteration_status="failure"
            log "${RED}Iteration $iteration: $summary${NC}"
        fi
    fi

    # Calculate iteration duration
    iteration_end=$(date +%s)
    iteration_duration=$((iteration_end - iteration_start))

    # Update metrics with iteration results
    update_metrics "$iteration_status" "$iteration_duration" "$summary"
    update_state_timing
    check_slow_iteration "$iteration_duration"

    # Collect health metrics (proactive monitoring)
    if [[ -x "./.loom/scripts/health-check.sh" ]]; then
        ./.loom/scripts/health-check.sh --collect >> "$LOG_FILE" 2>&1 || true
    fi

    # Log the summary and track success/failure for backoff
    if [[ "$summary" == *"SHUTDOWN"* ]]; then
        log "${YELLOW}Iteration $iteration: $summary${NC}"
        break
    elif [[ "$summary" == *"ERROR"* ]] || [[ "$summary" == *"TIMEOUT"* ]]; then
        log "${RED}Iteration $iteration: $summary (${iteration_duration}s)${NC}"
        # Track failure and potentially increase backoff
        consecutive_failures=$((consecutive_failures + 1))
        if [[ $consecutive_failures -ge $BACKOFF_THRESHOLD ]]; then
            # Calculate new backoff (with cap)
            new_backoff=$((current_backoff * BACKOFF_MULTIPLIER))
            if [[ $new_backoff -gt $MAX_BACKOFF ]]; then
                new_backoff=$MAX_BACKOFF
            fi
            if [[ $new_backoff -ne $current_backoff ]]; then
                current_backoff=$new_backoff
                log "${YELLOW}Backing off to ${current_backoff}s (failure ${consecutive_failures})${NC}"
            fi
        fi
    else
        log "${GREEN}Iteration $iteration: $summary (${iteration_duration}s)${NC}"
        # Reset backoff on success
        if [[ $consecutive_failures -gt 0 ]] || [[ $current_backoff -ne $POLL_INTERVAL ]]; then
            consecutive_failures=0
            current_backoff=$POLL_INTERVAL
            log "${GREEN}Backoff reset to ${POLL_INTERVAL}s${NC}"
        fi
    fi

    # Check for stop signal again before sleeping
    if [[ -f "$STOP_SIGNAL" ]]; then
        log "${YELLOW}SHUTDOWN_SIGNAL detected after iteration${NC}"
        break
    fi

    # Sleep before next iteration (using current_backoff which may be elevated)
    log "Sleeping ${current_backoff}s until next iteration..."
    sleep "$current_backoff"
done

log "${GREEN}Daemon loop completed gracefully${NC}"
