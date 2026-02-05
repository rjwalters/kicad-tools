#!/bin/bash
# claude-wrapper.sh - Resilient Claude CLI wrapper with retry logic
#
# This wrapper script handles transient API failures from the Claude CLI
# by implementing exponential backoff retry logic. It's designed for use
# with autonomous agents in Loom terminals.
#
# Features:
# - Pre-flight checks (CLI availability, API reachability)
# - Error pattern detection for known transient failures
# - Exponential backoff with configurable parameters
# - Graceful shutdown via stop signal file
# - Working directory recovery (handles deleted worktrees)
# - Detailed logging for debugging
#
# Usage:
#   ./claude-wrapper.sh [claude arguments]
#   ./claude-wrapper.sh --dangerously-skip-permissions
#
# Environment Variables:
#   LOOM_MAX_RETRIES       - Maximum retry attempts (default: 5)
#   LOOM_INITIAL_WAIT      - Initial wait time in seconds (default: 60)
#   LOOM_MAX_WAIT          - Maximum wait time in seconds (default: 1800 = 30min)
#   LOOM_BACKOFF_MULTIPLIER - Backoff multiplier (default: 2)
#   LOOM_TERMINAL_ID       - Terminal ID for stop signal (optional)
#   LOOM_WORKSPACE         - Workspace path for stop signal (optional)

set -euo pipefail

# Configuration with environment variable overrides
MAX_RETRIES="${LOOM_MAX_RETRIES:-5}"
INITIAL_WAIT="${LOOM_INITIAL_WAIT:-60}"
MAX_WAIT="${LOOM_MAX_WAIT:-1800}"  # 30 minutes
MULTIPLIER="${LOOM_BACKOFF_MULTIPLIER:-2}"

# Output monitor configuration
# How long to wait after detecting an API error pattern before killing claude
API_ERROR_IDLE_TIMEOUT="${LOOM_API_ERROR_IDLE_TIMEOUT:-60}"

# Terminal identification for stop signals
TERMINAL_ID="${LOOM_TERMINAL_ID:-}"
# Note: WORKSPACE may fail if CWD is invalid at startup - recover_cwd handles this
WORKSPACE="${LOOM_WORKSPACE:-$(pwd 2>/dev/null || echo "$HOME")}"

# Logging helpers
log_info() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $*" >&2
}

log_warn() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $*" >&2
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*" >&2
}

# Recover from deleted working directory
# This handles the case where the agent's worktree is deleted while it's running
# (e.g., by loom-clean, merge-pr.sh, or agent-destroy.sh)
recover_cwd() {
    # Check if current directory is still valid
    if pwd &>/dev/null 2>&1; then
        return 0  # CWD is fine, nothing to do
    fi

    log_warn "Working directory deleted, attempting recovery..."

    # Try WORKSPACE first (set by agent-spawn.sh, may point to repo root)
    if [[ -n "${WORKSPACE:-}" ]] && [[ -d "$WORKSPACE" ]]; then
        if cd "$WORKSPACE" 2>/dev/null; then
            log_info "Recovered to workspace: $WORKSPACE"
            return 0
        fi
    fi

    # Try to find git root (may fail if CWD context is completely gone)
    local git_root
    if git_root=$(git rev-parse --show-toplevel 2>/dev/null) && [[ -d "$git_root" ]]; then
        if cd "$git_root" 2>/dev/null; then
            log_info "Recovered to git root: $git_root"
            return 0
        fi
    fi

    # Last resort: home directory
    if cd "$HOME" 2>/dev/null; then
        log_warn "Recovered to HOME (worktree likely removed): $HOME"
        return 0
    fi

    # Absolute last resort: /tmp
    if cd /tmp 2>/dev/null; then
        log_warn "Recovered to /tmp (all other recovery paths failed)"
        return 0
    fi

    log_error "Failed to recover working directory - all recovery paths failed"
    return 1
}

# Check if stop signal exists (graceful shutdown support)
check_stop_signal() {
    # Global stop signal for all agents
    if [[ -f "${WORKSPACE}/.loom/stop-daemon" ]]; then
        log_info "Global stop signal detected (.loom/stop-daemon)"
        return 0
    fi

    # Per-terminal stop signal
    if [[ -n "${TERMINAL_ID}" && -f "${WORKSPACE}/.loom/stop-agent-${TERMINAL_ID}" ]]; then
        log_info "Agent stop signal detected (.loom/stop-agent-${TERMINAL_ID})"
        return 0
    fi

    return 1
}

# Pre-flight check: verify Claude CLI is available
check_cli_available() {
    if ! command -v claude &>/dev/null; then
        log_error "Claude CLI not found in PATH"
        log_error "Install with: npm install -g @anthropic-ai/claude-code"
        return 1
    fi
    log_info "Claude CLI found: $(command -v claude)"
    return 0
}

# Pre-flight check: verify API is reachable
# Uses a lightweight HEAD request to api.anthropic.com
check_api_reachable() {
    local timeout=10

    # Try curl first (most common)
    if command -v curl &>/dev/null; then
        if curl --silent --head --max-time "${timeout}" https://api.anthropic.com/ &>/dev/null; then
            log_info "API endpoint reachable (curl)"
            return 0
        fi
    fi

    # Fallback to nc (netcat)
    if command -v nc &>/dev/null; then
        if nc -z -w "${timeout}" api.anthropic.com 443 2>/dev/null; then
            log_info "API endpoint reachable (nc)"
            return 0
        fi
    fi

    log_warn "Could not verify API reachability (continuing anyway)"
    return 0  # Don't fail on network check - let Claude CLI handle it
}

# Detect if error output indicates a transient/retryable error
is_transient_error() {
    local output="$1"
    local exit_code="${2:-1}"

    # Known transient error patterns
    local patterns=(
        "No messages returned"
        "Rate limit exceeded"
        "rate_limit"
        "Connection refused"
        "ECONNREFUSED"
        "network error"
        "NetworkError"
        "ETIMEDOUT"
        "ECONNRESET"
        "ENETUNREACH"
        "socket hang up"
        "503 Service"
        "502 Bad Gateway"
        "500 Internal Server Error"
        "overloaded"
        "temporarily unavailable"
    )

    for pattern in "${patterns[@]}"; do
        if echo "${output}" | grep -qi "${pattern}"; then
            log_info "Detected transient error pattern: ${pattern}"
            return 0
        fi
    done

    # Exit code 1 with no output often indicates API issues
    if [[ "${exit_code}" -eq 1 && -z "${output}" ]]; then
        log_info "Empty output with exit code 1 - treating as transient"
        return 0
    fi

    return 1
}

# Monitor output file for API errors during execution.
# If an API error pattern is detected and no new output arrives within
# API_ERROR_IDLE_TIMEOUT seconds, sends SIGINT to the claude process.
# This handles the "agent waits for 'try again' input" scenario.
#
# Arguments: $1 = output file path, $2 = PID file path to write monitor PID
start_output_monitor() {
    local output_file="$1"
    local monitor_pid_file="$2"

    (
        local last_size=0
        local error_detected_at=0

        while true; do
            sleep 5

            # Exit if output file is gone (session ended)
            if [[ ! -f "${output_file}" ]]; then
                break
            fi

            local current_size
            current_size=$(wc -c < "${output_file}" 2>/dev/null || echo "0")

            if [[ "${current_size}" -ne "${last_size}" ]]; then
                # New output arrived - check for API error patterns
                local tail_content
                tail_content=$(tail -c 2000 "${output_file}" 2>/dev/null || echo "")

                local found_error=false
                for pattern in "500 Internal Server Error" "Rate limit exceeded" \
                    "overloaded" "temporarily unavailable" "503 Service" \
                    "502 Bad Gateway" "No messages returned"; do
                    if echo "${tail_content}" | grep -qi "${pattern}" 2>/dev/null; then
                        found_error=true
                        break
                    fi
                done

                if [[ "${found_error}" == "true" ]]; then
                    if [[ "${error_detected_at}" -eq 0 ]]; then
                        error_detected_at=$(date +%s)
                        log_warn "Output monitor: API error pattern detected, watching for idle..."
                    fi
                else
                    # New non-error output - reset detection
                    error_detected_at=0
                fi
                last_size="${current_size}"
            elif [[ "${error_detected_at}" -gt 0 ]]; then
                # No new output since error was detected
                local now
                now=$(date +%s)
                local idle_time=$((now - error_detected_at))
                if [[ "${idle_time}" -ge "${API_ERROR_IDLE_TIMEOUT}" ]]; then
                    log_warn "Output monitor: No new output for ${idle_time}s after API error - sending SIGINT to claude"
                    # Find and signal the claude process (child of this wrapper's shell)
                    pkill -INT -P $$ -f "claude" 2>/dev/null || true
                    break
                fi
            fi
        done
    ) &
    echo $! > "${monitor_pid_file}"
}

# Stop the background output monitor
stop_output_monitor() {
    local monitor_pid_file="$1"
    if [[ -f "${monitor_pid_file}" ]]; then
        local pid
        pid=$(cat "${monitor_pid_file}" 2>/dev/null || echo "")
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
            wait "${pid}" 2>/dev/null || true
        fi
        rm -f "${monitor_pid_file}"
    fi
}

# Calculate wait time with exponential backoff
calculate_wait_time() {
    local attempt="$1"
    local wait_time=$((INITIAL_WAIT * (MULTIPLIER ** (attempt - 1))))

    # Cap at maximum wait time
    if [[ "${wait_time}" -gt "${MAX_WAIT}" ]]; then
        wait_time="${MAX_WAIT}"
    fi

    echo "${wait_time}"
}

# Format seconds as human-readable duration
format_duration() {
    local seconds="$1"
    local minutes=$((seconds / 60))
    local remaining=$((seconds % 60))

    if [[ "${minutes}" -gt 0 ]]; then
        echo "${minutes}m ${remaining}s"
    else
        echo "${seconds}s"
    fi
}

# Main retry loop with exponential backoff
run_with_retry() {
    local attempt=1
    local exit_code=0
    local output=""

    # Recover CWD if it was deleted before we started
    if ! recover_cwd; then
        log_error "Cannot proceed - working directory recovery failed"
        return 1
    fi

    log_info "Starting Claude CLI with resilient wrapper"
    log_info "Configuration: max_retries=${MAX_RETRIES}, initial_wait=${INITIAL_WAIT}s, max_wait=${MAX_WAIT}s, multiplier=${MULTIPLIER}x"

    while [[ "${attempt}" -le "${MAX_RETRIES}" ]]; do
        # Recover CWD if it was deleted during previous attempt or backoff
        if ! recover_cwd; then
            log_error "Cannot proceed - working directory recovery failed"
            return 1
        fi

        # Check for stop signal before each attempt
        if check_stop_signal; then
            log_info "Stop signal detected - exiting gracefully"
            return 0
        fi

        log_info "Attempt ${attempt}/${MAX_RETRIES}: Starting Claude CLI"

        # Run Claude CLI, capturing both stdout and stderr
        # We need to capture output while also displaying it in real-time
        # Use a temp file to capture output for error detection
        local temp_output
        temp_output=$(mktemp)

        # Start background output monitor to detect API errors during execution
        local monitor_pid_file
        monitor_pid_file=$(mktemp)

        # Run claude with all arguments passed to wrapper
        # Use macOS `script` to preserve TTY (so Claude CLI sees isatty(stdout) = true)
        # while still capturing output to a file. A plain pipe (`| tee`) would replace
        # stdout with a pipe fd, causing Claude to switch to non-interactive --print mode.
        start_output_monitor "${temp_output}" "${monitor_pid_file}"
        set +e  # Temporarily disable errexit to capture exit code
        script -q "${temp_output}" claude "$@"
        exit_code=$?
        set -e
        stop_output_monitor "${monitor_pid_file}"

        output=$(cat "${temp_output}")
        rm -f "${temp_output}"

        # Check exit code
        if [[ "${exit_code}" -eq 0 ]]; then
            log_info "Claude CLI completed successfully"
            return 0
        fi

        log_warn "Claude CLI exited with code ${exit_code}"

        # Check if this is a transient error worth retrying
        if ! is_transient_error "${output}" "${exit_code}"; then
            log_error "Non-transient error detected - not retrying"
            log_error "Output: ${output}"
            return "${exit_code}"
        fi

        # Check for stop signal before waiting
        if check_stop_signal; then
            log_info "Stop signal detected - exiting gracefully"
            return 0
        fi

        # Calculate backoff wait time
        local wait_time
        wait_time=$(calculate_wait_time "${attempt}")

        if [[ "${attempt}" -lt "${MAX_RETRIES}" ]]; then
            log_warn "Transient error detected. Waiting $(format_duration "${wait_time}") before retry..."

            # Sleep with periodic stop signal checks
            local elapsed=0
            while [[ "${elapsed}" -lt "${wait_time}" ]]; do
                if check_stop_signal; then
                    log_info "Stop signal detected during backoff - exiting gracefully"
                    return 0
                fi
                sleep 5
                elapsed=$((elapsed + 5))
            done

            log_info "Backoff complete, retrying..."
        fi

        attempt=$((attempt + 1))
    done

    log_error "Max retries (${MAX_RETRIES}) exceeded"
    log_error "Last error: ${output}"
    return 1
}

# Run pre-flight checks
run_preflight_checks() {
    log_info "Running pre-flight checks..."

    if ! check_cli_available; then
        return 1
    fi

    check_api_reachable  # Non-fatal, just logs

    log_info "Pre-flight checks passed"
    return 0
}

# Main entry point
main() {
    log_info "Claude wrapper starting"
    log_info "Arguments: $*"
    log_info "Workspace: ${WORKSPACE}"
    [[ -n "${TERMINAL_ID}" ]] && log_info "Terminal ID: ${TERMINAL_ID}"

    # Run pre-flight checks
    if ! run_preflight_checks; then
        exit 1
    fi

    # Check for stop signal before starting
    if check_stop_signal; then
        log_info "Stop signal already present - exiting without starting"
        exit 0
    fi

    # Run Claude with retry logic
    run_with_retry "$@"
    exit_code=$?

    log_info "Claude wrapper exiting with code ${exit_code}"
    exit "${exit_code}"
}

# Run main with all script arguments
main "$@"
