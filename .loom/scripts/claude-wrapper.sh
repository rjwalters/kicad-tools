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
#   LOOM_AUTH_CACHE_TTL    - Auth cache TTL in seconds (default: 120)
#   LOOM_AUTH_CACHE_STALE_LOCK_THRESHOLD - Stale lock cleanup threshold in seconds (default: 90)
#   LOOM_AUTH_CACHE_LOCK_WAIT - Max time to wait for lock holder in seconds (default: 60)

set -euo pipefail

# Configuration with environment variable overrides
MAX_RETRIES="${LOOM_MAX_RETRIES:-5}"
INITIAL_WAIT="${LOOM_INITIAL_WAIT:-60}"
MAX_WAIT="${LOOM_MAX_WAIT:-1800}"  # 30 minutes
MULTIPLIER="${LOOM_BACKOFF_MULTIPLIER:-2}"

# Output monitor configuration
# How long to wait after detecting an API error pattern before killing claude
API_ERROR_IDLE_TIMEOUT="${LOOM_API_ERROR_IDLE_TIMEOUT:-60}"

# Auth cache configuration
# Short-TTL file cache to prevent concurrent `claude auth status` calls
# from overwhelming the auth endpoint when multiple agents start simultaneously
AUTH_CACHE_TTL="${LOOM_AUTH_CACHE_TTL:-120}"  # seconds
# Max time a single auth check cycle can take: 15s timeout × 3 retries + backoff (2+5+10) ≈ 62s
AUTH_CACHE_STALE_LOCK_THRESHOLD="${LOOM_AUTH_CACHE_STALE_LOCK_THRESHOLD:-90}"  # seconds
AUTH_CACHE_LOCK_WAIT="${LOOM_AUTH_CACHE_LOCK_WAIT:-60}"  # seconds

# Startup health monitor configuration
# How long (seconds) to watch early output for MCP/plugin failures
STARTUP_MONITOR_WINDOW="${LOOM_STARTUP_MONITOR_WINDOW:-90}"
# Grace period (seconds) after detecting startup failure before killing.
# The monitor polls every 2s within this window for loom MCP connection.
STARTUP_GRACE_PERIOD="${LOOM_STARTUP_GRACE_PERIOD:-20}"

# Terminal identification for stop signals
TERMINAL_ID="${LOOM_TERMINAL_ID:-}"
# Note: WORKSPACE may fail if CWD is invalid at startup - recover_cwd handles this
WORKSPACE="${LOOM_WORKSPACE:-$(pwd 2>/dev/null || echo "$HOME")}"

# Whether --dangerously-skip-permissions was passed (detected in main())
SKIP_PERMISSIONS_MODE=false

# Global state for signal-handler log flush (issue #2586).
# When tmux kill-session sends SIGHUP, the wrapper needs to append any
# tee-captured output to the log file before dying.  These globals are set
# in run_with_retry() and read by the _flush_output_on_signal() handler.
_FLUSH_TEMP_OUTPUT=""       # Path to tee-captured temp output file
_FLUSH_LOG_FILE=""          # Path to the agent log file
_FLUSH_PRE_LOG_LINES=0     # Log file line count before CLI started

# Retry state file for external observability (see issue #2296).
# When TERMINAL_ID is set, the wrapper writes its retry/backoff state to this
# file so agent-wait-bg.sh and the shepherd can distinguish "wrapper retrying"
# from "claude actively working".
RETRY_STATE_DIR="${WORKSPACE}/.loom/retry-state"
RETRY_STATE_FILE=""
if [[ -n "${TERMINAL_ID}" ]]; then
    RETRY_STATE_FILE="${RETRY_STATE_DIR}/${TERMINAL_ID}.json"
fi

# Sidecar exit code file for shepherd visibility (issue #2737).
# When agent-wait returns 0 (shell idle, no claude process), the wrapper's
# actual exit code is lost.  This file preserves it so run_worker_phase()
# can detect failures that agent-wait missed.
_EXIT_CODE_SIDECAR=""
if [[ -n "${TERMINAL_ID}" ]]; then
    _EXIT_CODE_SIDECAR="${WORKSPACE}/.loom/exit-codes/${TERMINAL_ID}.exit"
fi

# Write the wrapper's exit code to the sidecar file.  Called on every exit
# path (normal, pre-flight failure, signal kill) so the shepherd always has
# the real exit code regardless of how the tmux session ended.
_write_exit_sidecar() {
    local code="${1:-1}"
    if [[ -n "${_EXIT_CODE_SIDECAR}" ]]; then
        mkdir -p "$(dirname "${_EXIT_CODE_SIDECAR}")" 2>/dev/null || true
        echo "${code}" > "${_EXIT_CODE_SIDECAR}" 2>/dev/null || true
    fi
}

# Logging helpers
log_info() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [INFO] $*" >&2
}

log_warn() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [WARN] $*" >&2
}

log_error() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [ERROR] $*" >&2
}

# Check whether pipe-pane already captured the CLI output by comparing
# content fingerprints.  Normalizes both the new log portion and probe
# lines from captured output by stripping non-alphanumeric chars, then
# checks for substring match.  Handles garbled pipe-pane output where
# terminal control characters are interleaved with actual text.  #2590.
_pipe_pane_captured_content() {
    local _pp_temp="$1" _pp_log="$2" _pp_growth="$3"
    [[ $_pp_growth -le 0 ]] && return 1
    local _pp_log_norm
    _pp_log_norm=$(tail -n "$_pp_growth" "$_pp_log" 2>/dev/null | LC_ALL=C tr -cd '[:alnum:]' | head -c 5000)
    [[ ${#_pp_log_norm} -lt 10 ]] && return 1
    local _pp_probe
    while IFS= read -r _pp_probe; do
        _pp_probe=$(LC_ALL=C tr -cd '[:alnum:]' <<< "$_pp_probe")
        [[ ${#_pp_probe} -lt 12 ]] && continue
        [[ "$_pp_log_norm" == *"$_pp_probe"* ]] && return 0
    done < <(grep -v '^[[:space:]]*$' "$_pp_temp" 2>/dev/null | grep -v '^#' | tail -20 | head -5)
    return 1
}

# Flush tee-captured output to the log file on signal (issue #2586).
# Called via trap when tmux kill-session sends SIGHUP/SIGTERM.  Without
# this, the fallback append logic (lines after the CLI invocation) never
# executes because the process is killed before reaching it.
#
# Uses sentinel detection, content fingerprinting, and a conservative
# line-count ratio to decide whether pipe-pane already captured output.
# See #2582, #2590.
_flush_output_on_signal() {
    # Only flush if we have the required state
    if [[ -z "$_FLUSH_TEMP_OUTPUT" ]] || [[ ! -s "$_FLUSH_TEMP_OUTPUT" ]]; then
        return
    fi
    if [[ -z "$_FLUSH_LOG_FILE" ]]; then
        return
    fi

    if [[ -f "$_FLUSH_LOG_FILE" ]]; then
        local _post_lines
        _post_lines=$(wc -l < "$_FLUSH_LOG_FILE" 2>/dev/null || echo "0")
        local _growth=$(( _post_lines - _FLUSH_PRE_LOG_LINES ))
        local _temp_lines
        _temp_lines=$(wc -l < "$_FLUSH_TEMP_OUTPUT" 2>/dev/null || echo "0")
        local _needs_append=true
        # Tier 1: If log contains the CLI start sentinel with content after
        # it, pipe-pane was working — no fallback append needed.
        # Unanchored match handles garbled pipe-pane output.  #2582, #2590.
        if grep -q "CLAUDE_CLI_START" "$_FLUSH_LOG_FILE" 2>/dev/null; then
            local _lines_after_sentinel
            _lines_after_sentinel=$(sed -n '/CLAUDE_CLI_START/,$p' "$_FLUSH_LOG_FILE" 2>/dev/null | wc -l)
            if [[ $_lines_after_sentinel -gt 1 ]]; then
                _needs_append=false
            fi
        fi
        # Tier 2: Content fingerprint — check if distinctive text from
        # captured output already appears in the new log portion.  #2590.
        if [[ "$_needs_append" == "true" ]] && \
           _pipe_pane_captured_content "$_FLUSH_TEMP_OUTPUT" "$_FLUSH_LOG_FILE" "$_growth"; then
            _needs_append=false
        fi
        # Tier 3: Conservative line-count ratio as safety net.  #2582.
        if [[ "$_needs_append" == "true" ]] && \
           [[ $_growth -lt $(( _temp_lines / 4 )) ]]; then
            cat "$_FLUSH_TEMP_OUTPUT" >> "$_FLUSH_LOG_FILE" 2>/dev/null || true
        fi
    else
        # Log file doesn't exist yet - create it with the captured output
        cat "$_FLUSH_TEMP_OUTPUT" > "$_FLUSH_LOG_FILE" 2>/dev/null || true
    fi

    # Clean up temp file
    rm -f "$_FLUSH_TEMP_OUTPUT" 2>/dev/null || true
    _FLUSH_TEMP_OUTPUT=""

    # Write sidecar exit code (1 = killed by signal) so the shepherd
    # detects this wasn't a clean exit.  See issue #2737.
    _write_exit_sidecar 1
}

# Write retry state to a JSON file for external observability (issue #2296).
# Called when entering backoff or starting a new attempt so the shepherd
# and agent-wait-bg.sh can see what the wrapper is doing.
write_retry_state() {
    if [[ -z "${RETRY_STATE_FILE}" ]]; then
        return
    fi
    local status="$1"
    local attempt="$2"
    local last_error="${3:-}"
    local next_retry_at="${4:-}"

    mkdir -p "${RETRY_STATE_DIR}"
    cat > "${RETRY_STATE_FILE}" <<EOJSON
{
  "status": "${status}",
  "attempt": ${attempt},
  "max_retries": ${MAX_RETRIES},
  "last_error": $(printf '%s' "${last_error}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""'),
  "next_retry_at": "${next_retry_at}",
  "terminal_id": "${TERMINAL_ID}",
  "updated_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}
EOJSON
}

# Remove the retry state file on exit (success or permanent failure).
clear_retry_state() {
    if [[ -n "${RETRY_STATE_FILE}" ]] && [[ -f "${RETRY_STATE_FILE}" ]]; then
        rm -f "${RETRY_STATE_FILE}"
    fi
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

# Resolve workspace root for MCP config lookup.
# In worktrees, WORKSPACE may point to the worktree itself; the MCP config
# (.mcp.json) lives in the git common directory (the main checkout).
resolve_mcp_workspace() {
    # If .mcp.json exists in WORKSPACE, use it directly
    if [[ -f "${WORKSPACE}/.mcp.json" ]]; then
        echo "${WORKSPACE}"
        return
    fi

    # In a worktree, try the git common directory (main checkout)
    local common_dir
    if common_dir=$(git -C "${WORKSPACE}" rev-parse --git-common-dir 2>/dev/null); then
        # common_dir is the .git dir; parent is the repo root
        local repo_root
        repo_root=$(cd "${common_dir}/.." 2>/dev/null && pwd)
        if [[ -f "${repo_root}/.mcp.json" ]]; then
            echo "${repo_root}"
            return
        fi
    fi

    # Fallback to WORKSPACE
    echo "${WORKSPACE}"
}

# Pre-flight check: verify MCP server can start
# Attempts to launch the mcp-loom Node.js server and checks for the startup
# message on stderr. If the dist/ directory is missing or stale, attempts
# a rebuild before retrying.
check_mcp_server() {
    local mcp_workspace
    mcp_workspace=$(resolve_mcp_workspace)

    local mcp_config="${mcp_workspace}/.mcp.json"
    if [[ ! -f "${mcp_config}" ]]; then
        log_warn "MCP config not found at ${mcp_config} - skipping MCP pre-flight"
        return 0  # Non-fatal: MCP may not be configured
    fi

    # Extract the MCP server entry point from .mcp.json
    # Use timeout to prevent hanging on resource-contended systems (see issue #2472).
    local mcp_entry
    mcp_entry=$(timeout 10 python3 -c "
import json, sys
with open('${mcp_config}') as f:
    cfg = json.load(f)
servers = cfg.get('mcpServers', {})
for name, srv in servers.items():
    args = srv.get('args', [])
    if args:
        print(args[-1])
        sys.exit(0)
" 2>/dev/null || echo "")

    if [[ -z "${mcp_entry}" ]]; then
        log_warn "Could not extract MCP entry point from ${mcp_config} - skipping MCP pre-flight"
        return 0
    fi

    # Check if the entry point file exists
    if [[ ! -f "${mcp_entry}" ]]; then
        log_warn "MCP entry point missing: ${mcp_entry}"
        _try_mcp_rebuild "${mcp_entry}"
        return $?
    fi

    # Smoke test: start MCP server and verify it emits the startup message
    # The MCP server writes "Loom MCP server running on stdio" to stderr on success.
    # Use a short timeout - we just need to see the startup message.
    local mcp_stderr
    mcp_stderr=$(timeout 5 node "${mcp_entry}" </dev/null 2>&1 || true)

    if echo "${mcp_stderr}" | grep -qi "running on stdio"; then
        log_info "MCP server health check passed"
        return 0
    fi

    # MCP server failed to start - log the error
    log_warn "MCP server health check failed"
    if [[ -n "${mcp_stderr}" ]]; then
        log_warn "MCP stderr: ${mcp_stderr}"
    fi

    # Attempt rebuild and retry
    _try_mcp_rebuild "${mcp_entry}"
    return $?
}

# Check global MCP configurations from ~/.claude.json for missing binaries.
# This is a warning-only pre-flight check — missing global MCPs cause agent
# sessions to fail with "1 MCP server failed" before any useful work is done.
# We log clear warnings but never abort: global MCPs are outside Loom's
# control and the binary may simply need to be rebuilt.
check_global_mcp_configs() {
    local global_config="${HOME}/.claude.json"
    if [[ ! -f "${global_config}" ]]; then
        return 0  # No global config — nothing to validate
    fi

    # Parse mcpServers from ~/.claude.json.
    # Output one line per server: "name|command|args0"
    # Falls through silently on malformed JSON or missing python3.
    local server_info
    server_info=$(python3 - "${global_config}" 2>/dev/null <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        cfg = json.load(f)
    for name, srv in cfg.get('mcpServers', {}).items():
        command = srv.get('command', '')
        args = srv.get('args', [])
        args0 = args[0] if args else ''
        print(f"{name}|{command}|{args0}")
except Exception:
    pass
PYEOF
)

    if [[ -z "${server_info}" ]]; then
        return 0
    fi

    local warned=false
    while IFS='|' read -r server_name cmd args0; do
        [[ -z "${server_name}" ]] && continue

        # Check command: absolute path must exist on disk; otherwise must be in PATH.
        if [[ -n "${cmd}" ]]; then
            if [[ "${cmd}" == /* ]]; then
                if [[ ! -f "${cmd}" ]]; then
                    log_warn "⚠ Global MCP '${server_name}': command not found at ${cmd} — agent sessions will fail"
                    warned=true
                fi
            elif ! command -v "${cmd}" &>/dev/null; then
                log_warn "⚠ Global MCP '${server_name}': command '${cmd}' not found in PATH — agent sessions will fail"
                warned=true
            fi
        fi

        # Check args[0] if it looks like an absolute file path.
        if [[ -n "${args0}" && "${args0}" == /* ]]; then
            if [[ ! -f "${args0}" ]]; then
                log_warn "⚠ Global MCP '${server_name}': binary not found at ${args0} — agent sessions will fail"
                warned=true
            fi
        fi
    done <<< "${server_info}"

    if [[ "${warned}" == "true" ]]; then
        log_warn "Fix missing MCP binaries or remove entries from ~/.claude.json to prevent agent session failures"
    fi

    return 0  # Always succeed — this is a warning-only check
}

# Attempt to rebuild the MCP server and re-verify
_try_mcp_rebuild() {
    local mcp_entry="$1"

    # Derive the package directory from the entry point
    # e.g., /path/to/mcp-loom/dist/index.js -> /path/to/mcp-loom
    local mcp_dir
    mcp_dir=$(dirname "$(dirname "${mcp_entry}")")

    if [[ ! -f "${mcp_dir}/package.json" ]]; then
        log_error "MCP package directory not found at ${mcp_dir} - cannot rebuild"
        return 1
    fi

    log_info "Attempting MCP server rebuild in ${mcp_dir}..."

    # Run npm build (suppressing verbose output)
    if (cd "${mcp_dir}" && npm run build 2>&1 | tail -5) >&2; then
        log_info "MCP rebuild completed"
    else
        log_error "MCP rebuild failed"
        return 1
    fi

    # Re-check after rebuild
    if [[ ! -f "${mcp_entry}" ]]; then
        log_error "MCP entry point still missing after rebuild: ${mcp_entry}"
        return 1
    fi

    local mcp_stderr
    mcp_stderr=$(timeout 5 node "${mcp_entry}" </dev/null 2>&1 || true)

    if echo "${mcp_stderr}" | grep -qi "running on stdio"; then
        log_info "MCP server health check passed after rebuild"
        return 0
    fi

    log_error "MCP server still fails after rebuild"
    if [[ -n "${mcp_stderr}" ]]; then
        log_error "MCP stderr after rebuild: ${mcp_stderr}"
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

# --- Auth cache helpers ---
# Prevent concurrent `claude auth status` calls from overwhelming the auth
# endpoint when multiple agents start simultaneously (thundering-herd protection).
# Cache is user-scoped and short-lived; any failure falls through to a direct call.

# Return a random integer in [1, max] using $RANDOM (portable bash).
# Used to add jitter and desynchronize concurrent agents.
_auth_jitter() {
    local max="${1:-5}"
    echo $(( (RANDOM % max) + 1 ))
}

_auth_cache_file() {
    echo "/tmp/claude-auth-cache-$(id -u).json"
}

_auth_lock_dir() {
    echo "/tmp/claude-auth-cache-$(id -u).lock"
}

# Acquire the cache lock (non-blocking).
# Returns 0 if acquired, 1 if another process holds it.
# Cleans up stale locks older than AUTH_CACHE_STALE_LOCK_THRESHOLD first.
_auth_cache_lock() {
    local lock_dir
    lock_dir=$(_auth_lock_dir)

    # Clean up stale locks (process that created it likely died)
    if [[ -d "${lock_dir}" ]]; then
        local lock_age=0
        if [[ "$(uname)" == "Darwin" ]]; then
            lock_age=$(( $(date +%s) - $(stat -f '%m' "${lock_dir}" 2>/dev/null || echo "0") ))
        else
            lock_age=$(( $(date +%s) - $(stat -c '%Y' "${lock_dir}" 2>/dev/null || echo "0") ))
        fi
        if [[ "${lock_age}" -gt "${AUTH_CACHE_STALE_LOCK_THRESHOLD}" ]]; then
            log_info "Removing stale auth cache lock (age: ${lock_age}s, threshold: ${AUTH_CACHE_STALE_LOCK_THRESHOLD}s)"
            rmdir "${lock_dir}" 2>/dev/null || true
        fi
    fi

    # Atomic lock acquisition via mkdir
    mkdir "${lock_dir}" 2>/dev/null
}

# Release the cache lock.
_auth_cache_unlock() {
    local lock_dir
    lock_dir=$(_auth_lock_dir)
    rmdir "${lock_dir}" 2>/dev/null || true
}

# Read cached auth output if it exists and is within TTL.
# On success, echoes the cached auth JSON output and returns 0.
# Returns 1 if cache is missing, corrupt, or expired.
_auth_cache_read() {
    local cache_file
    cache_file=$(_auth_cache_file)

    if [[ ! -f "${cache_file}" ]]; then
        return 1
    fi

    # Parse cache: extract time, exit_code, and output
    local cache_time cache_exit cached_output
    cache_time=$(python3 -c "import json,sys; d=json.load(open('${cache_file}')); print(d['time'])" 2>/dev/null) || return 1
    cache_exit=$(python3 -c "import json,sys; d=json.load(open('${cache_file}')); print(d['exit_code'])" 2>/dev/null) || return 1

    # Check TTL
    local now
    now=$(date +%s)
    local age=$(( now - cache_time ))
    if [[ "${age}" -gt "${AUTH_CACHE_TTL}" ]]; then
        return 1
    fi

    # Only use cache if the original call succeeded
    if [[ "${cache_exit}" -ne 0 ]]; then
        return 1
    fi

    # Extract the output field
    cached_output=$(python3 -c "import json,sys; print(json.load(open('${cache_file}'))['output'])" 2>/dev/null) || return 1
    echo "${cached_output}"
    return 0
}

# Write auth output to the cache file.
# Arguments: $1 = auth JSON output, $2 = exit code
_auth_cache_write() {
    local output="$1"
    local exit_code="$2"
    local cache_file
    cache_file=$(_auth_cache_file)
    local now
    now=$(date +%s)

    printf '%s' "${output}" | python3 -c "
import json, sys
data = {
    'time': ${now},
    'exit_code': ${exit_code},
    'output': sys.stdin.read()
}
with open('${cache_file}', 'w') as f:
    json.dump(data, f)
" 2>/dev/null || true
}

# Pre-flight check: verify authentication status
# Uses `claude auth status --json` to confirm the CLI is logged in.
# When CLAUDE_CONFIG_DIR is set, passes it through so the check uses
# the same config the session will use.
check_auth_status() {
    local auth_output
    local auth_exit_code

    # --- Step 1: Try cache first ---
    local cached_output
    if cached_output=$(_auth_cache_read); then
        local logged_in
        logged_in=$(echo "${cached_output}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('loggedIn', False))" 2>/dev/null || echo "")
        if [[ "${logged_in}" == "True" ]]; then
            log_info "Authentication check passed (cached)"
            return 0
        fi
        # Cache says not logged in — fall through to fresh check
    fi

    # --- Step 2: Try to acquire lock for fresh check ---
    local lock_acquired=false
    if _auth_cache_lock; then
        lock_acquired=true
    else
        # Another process is refreshing — wait for it to finish, polling cache
        log_info "Auth cache lock held by another process, waiting up to ${AUTH_CACHE_LOCK_WAIT}s..."
        local wait_elapsed=0
        while [[ "${wait_elapsed}" -lt "${AUTH_CACHE_LOCK_WAIT}" ]]; do
            sleep 2
            wait_elapsed=$((wait_elapsed + 2))
            if cached_output=$(_auth_cache_read); then
                local logged_in
                logged_in=$(echo "${cached_output}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('loggedIn', False))" 2>/dev/null || echo "")
                if [[ "${logged_in}" == "True" ]]; then
                    log_info "Authentication check passed (cached, after ${wait_elapsed}s wait)"
                    return 0
                fi
            fi
        done
        # Still stale — fall through to direct check with jitter to desynchronize
        local jitter
        jitter=$(_auth_jitter 5)
        log_info "Auth cache still stale after ${AUTH_CACHE_LOCK_WAIT}s wait, proceeding with direct check (jitter: ${jitter}s)"
        sleep "${jitter}"
    fi

    # --- Step 3: Existing retry logic (with cache write on success) ---
    local max_retries=3
    local -a backoff_seconds=(2 5 10)

    for (( attempt=1; attempt<=max_retries; attempt++ )); do
        auth_exit_code=0

        # Refresh lock mtime so other processes don't consider it stale
        # while we're still actively retrying
        if [[ "${lock_acquired}" == "true" ]]; then
            touch "$(_auth_lock_dir)" 2>/dev/null || true
        fi

        # Unset CLAUDECODE to avoid nested-session guard when running inside
        # a Claude Code session (e.g., during testing or shepherd-spawned builds).
        # Use timeout to prevent hanging after a long first attempt leaves
        # auth in a bad state (see issue #2472).
        auth_output=$(timeout 15 bash -c 'CLAUDECODE="" claude auth status --json 2>&1') || auth_exit_code=$?

        # timeout exits with 124 when the command times out
        if [[ "${auth_exit_code}" -eq 124 ]]; then
            if (( attempt < max_retries )); then
                local backoff=${backoff_seconds[$((attempt - 1))]}
                local jitter
                jitter=$(_auth_jitter 3)
                backoff=$((backoff + jitter))
                log_info "Authentication check timed out (attempt ${attempt}/${max_retries}), retrying in ${backoff}s (includes ${jitter}s jitter)..."
                sleep "${backoff}"
                continue
            fi
            log_warn "Authentication check timed out after ${max_retries} attempts"
            [[ "${lock_acquired}" == "true" ]] && _auth_cache_unlock
            return 1
        fi

        if [[ "${auth_exit_code}" -ne 0 ]]; then
            log_warn "Authentication check command failed (exit ${auth_exit_code})"
            log_warn "Output: ${auth_output}"
            if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
                log_warn "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}"
                log_warn "Run: CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR} claude auth login"
            else
                log_warn "Run: claude auth login"
            fi
            [[ "${lock_acquired}" == "true" ]] && _auth_cache_unlock
            return 1
        fi

        # Write successful result to cache (best-effort)
        _auth_cache_write "${auth_output}" "${auth_exit_code}"

        # Parse the loggedIn field from JSON output
        local logged_in
        logged_in=$(echo "${auth_output}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('loggedIn', False))" 2>/dev/null || echo "")

        if [[ "${logged_in}" != "True" ]]; then
            log_warn "Authentication check failed: not logged in"
            if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
                log_warn "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR}"
                log_warn "Run: CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR} claude auth login"
            else
                log_warn "Run: claude auth login"
            fi
            [[ "${lock_acquired}" == "true" ]] && _auth_cache_unlock
            return 1
        fi

        log_info "Authentication check passed (logged in)"
        [[ "${lock_acquired}" == "true" ]] && _auth_cache_unlock
        return 0
    done

    # Should not reach here, but guard against it
    log_warn "Authentication check failed after ${max_retries} attempts"
    [[ "${lock_acquired}" == "true" ]] && _auth_cache_unlock
    return 1
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

    # Rate limit abort is NOT transient — the CLI hit a usage/plan limit
    # and showed an interactive prompt.  Retrying will hit the same limit.
    if echo "${output}" | grep -q "RATE_LIMIT_ABORT"; then
        return 1
    fi

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
        "MCP server failed"
        "MCP.*failed"
        "plugins failed"
        "plugin.*failed to install"
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

# Check if error output specifically indicates an MCP/plugin failure.
# Used to map exhausted-retry exits to exit code 7 so the Python retry
# layer can recognize MCP failures even when the wrapper's own retries
# are exhausted.  See issue #2746.
is_mcp_error() {
    local output="$1"
    local mcp_patterns=(
        "MCP server failed"
        "MCP.*failed"
        "plugins failed"
        "plugin.*failed to install"
    )
    for pattern in "${mcp_patterns[@]}"; do
        if echo "${output}" | grep -qi "${pattern}"; then
            return 0
        fi
    done
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
        trap 'exit 0' TERM INT
        local last_size=0
        local error_detected_at=0

        while true; do
            sleep 5 &
            wait $! || exit 0

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

                # Check for CLI usage/plan limit prompt (interactive prompt
                # that blocks headless sessions).  Normalize text by stripping
                # non-alphanumeric chars to handle TUI garbling, then match
                # the distinctive prompt text.
                local normalized
                normalized=$(echo "${tail_content}" | LC_ALL=C tr -cd '[:alnum:]')
                if echo "${normalized}" | grep -qi "Stopandwaitforlimittoreset" 2>/dev/null; then
                    log_warn "Output monitor: CLI usage/plan limit prompt detected — killing claude"
                    echo "# RATE_LIMIT_ABORT" >&2
                    pkill -INT -P $$ -f "claude" 2>/dev/null || true
                    break
                fi

                # Check for 100% weekly usage limit banner (see issue #2859).
                # "You've used 100% of your weekly limit · resets ..." normalizes
                # to "Youveused100ofyourweeklylimit...".  Unlike the plan-limit
                # prompt, this does NOT show an interactive modal — the CLI just
                # keeps spinning indefinitely.  Kill immediately and write the
                # RATE_LIMIT_ABORT sentinel so the shepherd classifies this as
                # non-retryable (same as the plan-limit case).
                if echo "${normalized}" | grep -qi "Youveused100ofyourweeklylimit" 2>/dev/null; then
                    log_warn "Output monitor: Claude weekly usage limit exhausted (100%) — killing claude"
                    echo "# RATE_LIMIT_ABORT" >&2
                    pkill -INT -P $$ -f "claude" 2>/dev/null || true
                    break
                fi

                local found_error=false
                for pattern in "500 Internal Server Error" "Rate limit exceeded" \
                    "overloaded" "temporarily unavailable" "503 Service" \
                    "502 Bad Gateway" "No messages returned" \
                    "PreToolUse.*hook error"; do
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

# Monitor early CLI output for MCP/plugin startup failures.
# If the CLI starts with failed MCP servers or plugins, it often runs in a
# degraded state (stuck in thinking loops with no meaningful tool calls) rather
# than crashing.  This monitor watches the first STARTUP_MONITOR_WINDOW seconds
# of output for failure indicators and kills the CLI session so the retry loop
# can restart it cleanly.
#
# When all project MCP servers (from .mcp.json) connect successfully, only
# global plugin/MCP failures remain.  Since these won't self-resolve on
# restart (e.g. rust-analyzer-lsp in worktrees), the session is allowed to
# continue rather than wasting retries.  See issue #2721.
#
# Arguments: $1 = output file path, $2 = PID file path to write monitor PID
start_startup_monitor() {
    local output_file="$1"
    local monitor_pid_file="$2"

    (
        trap 'exit 0' TERM INT
        local check_interval=2
        local elapsed=0

        while [[ "${elapsed}" -lt "${STARTUP_MONITOR_WINDOW}" ]]; do
            sleep "${check_interval}" &
            wait $! || exit 0
            elapsed=$((elapsed + check_interval))

            # Exit if output file is gone (session ended)
            if [[ ! -f "${output_file}" ]]; then
                break
            fi

            # Check first 50 lines for startup failure patterns
            local head_content
            head_content=$(head -50 "${output_file}" 2>/dev/null || echo "")

            if [[ -z "${head_content}" ]]; then
                continue
            fi

            # Check for CLI usage/plan limit prompt in early output.
            # The limit prompt often appears within seconds of startup.
            local head_normalized
            head_normalized=$(echo "${head_content}" | LC_ALL=C tr -cd '[:alnum:]')
            if echo "${head_normalized}" | grep -qi "Stopandwaitforlimittoreset" 2>/dev/null; then
                log_warn "Startup monitor: CLI usage/plan limit prompt detected — killing claude"
                echo "# RATE_LIMIT_ABORT" >&2
                pkill -INT -P $$ -f "claude" 2>/dev/null || true
                break
            fi
            # Check for 100% weekly usage limit in early output (see issue #2859).
            if echo "${head_normalized}" | grep -qi "Youveused100ofyourweeklylimit" 2>/dev/null; then
                log_warn "Startup monitor: Claude weekly usage limit exhausted (100%) — killing claude"
                echo "# RATE_LIMIT_ABORT" >&2
                pkill -INT -P $$ -f "claude" 2>/dev/null || true
                break
            fi

            local found_failure=false
            local matched_pattern=""
            for pattern in \
                "MCP server failed" \
                "MCP servers failed" \
                "plugins failed"; do
                if echo "${head_content}" | grep -qi "${pattern}" 2>/dev/null; then
                    found_failure=true
                    matched_pattern="${pattern}"
                    break
                fi
            done

            if [[ "${found_failure}" == "true" ]]; then
                log_info "Startup monitor: detected '${matched_pattern}' in early output"

                # Poll for loom MCP connection within the grace period.
                # Check BEFORE sleeping so that if the MCP connected before
                # the failure was noticed (common case — MCP init ~1-3s,
                # first output check at ~2s) we skip the delay entirely.
                # See issue #2660 for why single-shot was insufficient.
                # See issue #2763 for check_interval reduction (5s → 2s).
                # If all project MCPs connected, the session is allowed to
                # continue (global plugin failures won't self-resolve).
                # Otherwise, we kill the session to avoid a degraded state
                # where injected commands are not processed (see #2652).
                local poll_interval=2
                local grace_elapsed=0
                local loom_connected=false
                # Claude Code writes debug logs to ~/.claude/debug/latest regardless
                # of CLAUDE_CONFIG_DIR.  The per-agent config dir never has a debug/
                # subdirectory populated by Claude Code itself.  Check the global
                # debug log as the authoritative source.  See issue #2835.
                local global_debug_log="${HOME}/.claude/debug/latest"
                local agent_debug_log="${CLAUDE_CONFIG_DIR:-}/debug/latest"
                local debug_log="${global_debug_log}"
                # Prefer per-agent dir if it has a populated debug log (future-proof).
                if [[ -L "${agent_debug_log}" || -f "${agent_debug_log}" ]]; then
                    debug_log="${agent_debug_log}"
                fi
                # Record when we started monitoring (epoch seconds).  Used below to
                # reject debug log hits from previous sessions: ~/.claude/debug/latest
                # is a symlink that may still point to the prior session's log while
                # the new (failing) session hasn't written its own log yet.  Accepting
                # a stale "Successfully connected" line causes the monitor to
                # incorrectly conclude "global-plugin-only failure, proceed" when
                # loom actually failed at runtime.  See issue #2911.
                local monitor_start_time
                monitor_start_time=$(date +%s)
                # Track whether the debug log was ever fresh (vs always stale/
                # concurrent).  Used below to distinguish "loom confirmed absent"
                # from "loom status unknown due to stale log".  See issue #3031.
                local debug_log_ever_fresh=false

                while [[ "${grace_elapsed}" -lt "${STARTUP_GRACE_PERIOD}" ]]; do
                    # Session ended on its own — nothing to kill
                    if [[ ! -f "${output_file}" ]]; then
                        loom_connected=true  # not a failure, just exited
                        break
                    fi

                    # Check if the critical "loom" MCP server connected.
                    # Re-evaluate debug_log each iteration: the symlink may update
                    # as new debug sessions start.
                    if [[ -L "${global_debug_log}" || -f "${global_debug_log}" ]]; then
                        debug_log="${global_debug_log}"
                    fi
                    if [[ -L "${debug_log}" || -f "${debug_log}" ]]; then
                        # Resolve the symlink to the actual file so we can check its mtime.
                        local resolved_debug_log
                        resolved_debug_log=$(readlink -f "${debug_log}" 2>/dev/null || echo "${debug_log}")

                        # Guard against stale logs: only trust a debug log that was
                        # created/modified at or after this monitoring session started.
                        # This prevents misclassifying "global-plugin-only" when the
                        # symlink still points to the previous session's successful log.
                        # See issue #2911.
                        local debug_log_mtime=0
                        if [[ -f "${resolved_debug_log}" ]]; then
                            if [[ "$(uname)" == "Darwin" ]]; then
                                debug_log_mtime=$(stat -f '%m' "${resolved_debug_log}" 2>/dev/null || echo "0")
                            else
                                debug_log_mtime=$(stat -c '%Y' "${resolved_debug_log}" 2>/dev/null || echo "0")
                            fi
                        fi
                        local debug_log_is_fresh=false
                        if [[ "${debug_log_mtime}" -ge "${monitor_start_time}" ]]; then
                            debug_log_is_fresh=true
                        fi

                        if [[ "${debug_log_is_fresh}" == "true" ]]; then
                            debug_log_ever_fresh=true
                            # Fast-path: if loom is explicitly listed as failed in the
                            # current session's debug log, kill immediately without waiting
                            # for the full grace period.  See issue #2911.
                            if grep -q 'MCP server "loom".*[Cc]onnection failed\|MCP server "loom".*[Ff]ailed to connect' \
                               "${resolved_debug_log}" 2>/dev/null; then
                                log_warn "Startup monitor: debug log confirms loom MCP failed to connect — killing session immediately"
                                pkill -INT -P $$ -f "claude" 2>/dev/null || true
                                break 2  # Break out of both while loops
                            fi

                            if grep -q 'MCP server "loom": Successfully connected' \
                               "${resolved_debug_log}" 2>/dev/null; then
                                loom_connected=true
                                break
                            fi
                        fi
                        # Debug log is stale (from a previous session) — do not trust
                        # "Successfully connected" hits; keep polling until the new
                        # session writes its own log or the grace period expires.
                    fi

                    # Sleep AFTER checking so the first iteration is instant
                    sleep "${poll_interval}" &
                    wait $! || exit 0
                    grace_elapsed=$((grace_elapsed + poll_interval))
                done

                if [[ "${loom_connected}" == "true" ]]; then
                    # Check if ALL project MCP servers (from .mcp.json)
                    # connected.  If they did, the only failures are from
                    # global MCP servers or plugins (e.g. rust-analyzer-lsp,
                    # swift-lsp) which won't self-resolve on restart.
                    # Killing the session would just waste retries.
                    # See issue #2721.
                    local all_project_ok=true
                    local mcp_json="${WORKSPACE}/.mcp.json"
                    # Resolve the debug log path once more for the project-MCP check.
                    local resolved_debug_log_final
                    resolved_debug_log_final=$(readlink -f "${debug_log}" 2>/dev/null || echo "${debug_log}")
                    if [[ -f "${mcp_json}" ]] && command -v jq &>/dev/null; then
                        local server_names
                        server_names=$(jq -r '.mcpServers // {} | keys[]' "${mcp_json}" 2>/dev/null)
                        for srv in ${server_names}; do
                            if ! grep -q "MCP server \"${srv}\": Successfully connected" "${resolved_debug_log_final}" 2>/dev/null; then
                                all_project_ok=false
                                log_warn "Startup monitor: project MCP server '${srv}' did not connect"
                                break
                            fi
                        done
                    else
                        # Can't determine project MCPs — fall back to
                        # conservative kill behavior (see issue #2652).
                        all_project_ok=false
                    fi

                    if [[ "${all_project_ok}" == "true" ]]; then
                        log_info "Startup monitor: only global plugin/MCP failures detected, project MCPs OK — continuing"
                        break
                    else
                        log_warn "Startup monitor: project MCP failure detected — killing session for clean restart (see issue #2652)"
                    fi
                else
                    # Build informative kill message: identify which MCP(s) actually
                    # failed rather than always blaming loom.  See issue #3031.
                    local _mcp_fail_lines="" _failed_mcps="" _fail_detail=""
                    if [[ -n "${resolved_debug_log:-}" ]] && [[ -f "${resolved_debug_log}" ]]; then
                        _mcp_fail_lines=$(grep -E 'MCP server "[^"]+".*([Cc]onnection [Ff]ailed|[Ff]ailed to connect)' \
                            "${resolved_debug_log}" 2>/dev/null || true)
                        if [[ -n "${_mcp_fail_lines}" ]]; then
                            _failed_mcps=$(printf '%s\n' "${_mcp_fail_lines}" | \
                                grep -oE 'MCP server "[^"]+"' | grep -oE '"[^"]+"' | \
                                tr -d '"' | grep -v '^loom$' | sort -u | head -3 | \
                                tr '\n' ',' | sed 's/,$//')
                            _fail_detail=$(printf '%s\n' "${_mcp_fail_lines}" | head -1 | \
                                grep -oE 'Cannot find module[^;|]*|ENOENT[^;|]*|spawn ENOENT[^;|]*' | \
                                head -1 | sed 's/[[:space:]]*$//' | cut -c1-80 || true)
                        fi
                    fi
                    if [[ "${debug_log_ever_fresh}" == "false" ]]; then
                        if [[ -n "${_failed_mcps}" ]]; then
                            log_warn "Startup monitor: loom MCP not confirmed (debug log stale/concurrent); '${_failed_mcps}' also failed — killing degraded session"
                        else
                            log_warn "Startup monitor: loom MCP not confirmed after ${STARTUP_GRACE_PERIOD}s (debug log stale/concurrent) — killing degraded session"
                        fi
                    elif [[ -n "${_failed_mcps}" ]]; then
                        local _detail_suffix=""
                        [[ -n "${_fail_detail}" ]] && _detail_suffix=" — ${_fail_detail}"
                        log_warn "Startup monitor: '${_failed_mcps}' MCP failed${_detail_suffix} — loom not confirmed — killing degraded session"
                    else
                        log_warn "Startup monitor: loom MCP not connected after ${STARTUP_GRACE_PERIOD}s — killing degraded session"
                    fi
                fi
                pkill -INT -P $$ -f "claude" 2>/dev/null || true
                break
            fi
        done
    ) &
    echo $! > "${monitor_pid_file}"
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
        write_retry_state "running" "${attempt}"

        # Run Claude CLI, capturing both stdout and stderr
        # We need to capture output while also displaying it in real-time
        # Use a temp file to capture output for error detection
        local temp_output
        temp_output=$(mktemp)

        # Start background output monitor to detect API errors during execution
        local monitor_pid_file
        monitor_pid_file=$(mktemp)

        # Start startup health monitor to detect MCP/plugin failures in early output
        local startup_monitor_pid_file
        startup_monitor_pid_file=$(mktemp)

        # Run claude with all arguments passed to wrapper
        # Three execution modes for Claude CLI:
        #
        # 1. Slash command prompt detected (e.g., "/judge 2434"):
        #    Use --print mode for reliable one-shot execution.  Interactive mode
        #    (script -q) can be blocked by onboarding/promotional dialogs that
        #    require user interaction before the prompt is processed. (Issue #2438)
        #
        # 2. No prompt, TTY available (autonomous agents):
        #    Use macOS `script` to preserve TTY so Claude CLI sees isatty(stdout) = true.
        #    A plain pipe (`| tee`) would replace stdout with a pipe fd, causing Claude
        #    to switch to non-interactive --print mode.
        #
        # 3. No prompt, no TTY (spawned from Claude Code's Bash tool):
        #    Run claude directly with tee for error detection.
        start_output_monitor "${temp_output}" "${monitor_pid_file}"
        start_startup_monitor "${temp_output}" "${startup_monitor_pid_file}"
        # Write sentinel marker so _is_low_output_session() in the shepherd can
        # distinguish wrapper pre-flight output from actual Claude CLI output.
        # The "# " prefix means it is also filtered as a header line.
        echo "# CLAUDE_CLI_START" >&2
        set +e  # Temporarily disable errexit to capture exit code
        unset CLAUDECODE  # Prevent nested session guard from blocking subprocess
        # Export per-agent config dir if set (for session isolation)
        if [[ -n "${CLAUDE_CONFIG_DIR:-}" ]]; then
            export CLAUDE_CONFIG_DIR
        fi
        if [[ -n "${TMPDIR:-}" ]]; then
            export TMPDIR
        fi
        # Detect slash command prompt in arguments (e.g., "/judge 2434").
        # On-demand workers spawned by the shepherd receive a slash command
        # as a positional prompt argument.  This flag is used for log-capture
        # heuristics (fallback append when pipe-pane misses output).
        # Note: we do NOT switch to --print mode for slash commands because
        # --print treats "/role" as a skill-definition print (exits with 0s
        # duration, no actual work).  Interactive mode (script -q) is safe
        # because CLAUDE_CONFIG_DIR isolation ensures onboarding is complete.
        # Regression from #2537, fixed in #2608.
        _has_slash_cmd=false
        for _arg in "$@"; do
            case "$_arg" in
                --*|-*) ;;  # Skip flags
                /*) _has_slash_cmd=true; break ;;
            esac
        done

        # Record log file line count before CLI runs so we can detect whether
        # pipe-pane captured the output (avoids duplicate appends).  Issue #2569.
        local _pre_log_lines=0
        local _log_file=""
        if [[ "$_has_slash_cmd" == "true" && -n "${TERMINAL_ID:-}" ]]; then
            _log_file="${WORKSPACE}/.loom/logs/loom-${TERMINAL_ID}.log"
            if [[ -f "$_log_file" ]]; then
                _pre_log_lines=$(wc -l < "$_log_file")
            fi
        fi

        # Wire up the signal handler for log flush on kill (issue #2586).
        # When tmux kill-session sends SIGHUP/SIGTERM, the normal fallback
        # append code (after the CLI invocation) never executes.  The signal
        # handler ensures captured output is flushed to the log file.
        _FLUSH_TEMP_OUTPUT="${temp_output}"
        _FLUSH_LOG_FILE="${_log_file}"
        _FLUSH_PRE_LOG_LINES="${_pre_log_lines}"
        trap _flush_output_on_signal SIGHUP SIGTERM

        if [ -t 0 ]; then
            # No prompt, TTY available - use script to preserve interactive mode
            script -q "${temp_output}" claude "$@"
            exit_code=$?
        else
            # No TTY (socket/pipe) - run claude directly, tee output for error detection
            log_info "No TTY available, running claude directly (non-interactive mode)"
            claude "$@" 2>&1 | tee "${temp_output}"
            exit_code=${PIPESTATUS[0]}
        fi
        set -e
        stop_output_monitor "${monitor_pid_file}"
        stop_output_monitor "${startup_monitor_pid_file}"

        # CLI exited normally — clear signal handler globals and restore
        # the default trap so the fallback append below handles flushing
        # instead of the signal handler (avoids double-flushing).
        _FLUSH_TEMP_OUTPUT=""
        _FLUSH_LOG_FILE=""
        _FLUSH_PRE_LOG_LINES=0
        trap clear_retry_state EXIT

        output=$(cat "${temp_output}")

        # In --print mode, pipe-pane may not flush before session exit.
        # Append captured output to the log file so log-based heuristics
        # (_is_low_output_session, _is_mcp_failure) have content to analyze
        # and post-mortem debugging is possible.  See issue #2550.
        # Only append if pipe-pane did NOT already capture sufficient output.
        # Three-tier detection: sentinel, content fingerprint, line-count.
        # See #2569, #2582, #2590.
        if [[ "$_has_slash_cmd" == "true" && -n "${TERMINAL_ID:-}" ]]; then
            if [[ -n "$_log_file" && -f "$_log_file" && -s "${temp_output}" ]]; then
                local _post_log_lines
                _post_log_lines=$(wc -l < "$_log_file")
                local _log_growth=$(( _post_log_lines - _pre_log_lines ))
                local _temp_lines
                _temp_lines=$(wc -l < "${temp_output}")
                local _needs_append=true
                # Tier 1: If the log contains the CLI start sentinel with
                # content after it, pipe-pane captured the output.
                # Unanchored match handles garbled pipe-pane.  #2582, #2590.
                if grep -q "CLAUDE_CLI_START" "$_log_file" 2>/dev/null; then
                    local _lines_after_sentinel
                    _lines_after_sentinel=$(sed -n '/CLAUDE_CLI_START/,$p' "$_log_file" | wc -l)
                    if [[ $_lines_after_sentinel -gt 1 ]]; then
                        _needs_append=false
                    fi
                fi
                # Tier 2: Content fingerprint — normalize text and check if
                # distinctive output already appears in the log.  #2590.
                if [[ "$_needs_append" == "true" ]] && \
                   _pipe_pane_captured_content "${temp_output}" "$_log_file" "$_log_growth"; then
                    _needs_append=false
                fi
                # Tier 3: Conservative line-count ratio as safety net.  #2582.
                if [[ "$_needs_append" == "true" ]] && \
                   [[ $_log_growth -lt $(( _temp_lines / 4 )) ]]; then
                    cat "${temp_output}" >> "$_log_file"
                fi
            fi
        fi

        rm -f "${temp_output}"

        # Check exit code
        if [[ "${exit_code}" -eq 0 ]]; then
            log_info "Claude CLI completed successfully"
            clear_retry_state
            return 0
        fi

        log_warn "Claude CLI exited with code ${exit_code}"

        # Check if this is a transient error worth retrying
        if ! is_transient_error "${output}" "${exit_code}"; then
            log_error "Non-transient error detected - not retrying"
            log_error "Output: ${output}"
            clear_retry_state
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

            # Truncate error output for the retry state file (first 200 chars)
            local error_snippet="${output:0:200}"
            local next_retry_ts
            next_retry_ts=$(date -u -v+"${wait_time}"S '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
                || date -u -d "+${wait_time} seconds" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
                || echo "")
            write_retry_state "backoff" "${attempt}" "${error_snippet}" "${next_retry_ts}"

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
    clear_retry_state

    # When the last failure was MCP-related, exit with code 7 so the
    # Python retry layer (run_phase_with_retry) can recognise and retry
    # the MCP failure instead of treating it as a generic error.
    # See issue #2746.
    if is_mcp_error "${output}"; then
        log_info "Last failure was MCP-related — exiting with code 7"
        return 7
    fi
    return 1
}

# Run pre-flight checks
run_preflight_checks() {
    log_info "Running pre-flight checks..."

    if ! check_cli_available; then
        return 1
    fi

    check_api_reachable  # Non-fatal, just logs

    if [[ -n "${LOOM_SHEPHERD_TASK_ID:-}" ]]; then
        log_info "Skipping auth pre-flight (shepherd subprocess, task=${LOOM_SHEPHERD_TASK_ID})"
    elif ! check_auth_status; then
        if [[ "${SKIP_PERMISSIONS_MODE}" == "true" ]]; then
            log_warn "Authentication pre-flight check failed (non-fatal in --dangerously-skip-permissions mode)"
        else
            log_error "Authentication pre-flight check failed"
            # Write sentinel so the shepherd can distinguish auth failures from
            # generic low-output sessions and avoid futile retries.  See issue #2508.
            echo "# AUTH_PREFLIGHT_FAILED" >&2
            return 1
        fi
    fi

    log_info "Running MCP server pre-flight check..."
    if ! check_mcp_server; then
        log_error "MCP server pre-flight check failed"
        # Write sentinel so the shepherd can distinguish MCP pre-flight failures
        # from generic low-output sessions.  Mirrors AUTH_PREFLIGHT_FAILED.
        # See issue #2706.
        echo "# MCP_PREFLIGHT_FAILED" >&2
        return 1
    fi
    log_info "MCP server pre-flight check passed"

    # Warn about missing global MCP binaries from ~/.claude.json (issue #3033).
    # This is non-fatal: warnings are logged but pre-flight always succeeds.
    check_global_mcp_configs

    log_info "All pre-flight checks passed"
    return 0
}

# Main entry point
main() {
    # Ensure retry state file is cleaned up on exit (normal or abnormal)
    trap clear_retry_state EXIT

    log_info "Claude wrapper starting"
    log_info "Arguments: $*"
    log_info "Workspace: ${WORKSPACE}"
    [[ -n "${TERMINAL_ID}" ]] && log_info "Terminal ID: ${TERMINAL_ID}"

    # Detect --dangerously-skip-permissions flag (automated agent mode)
    for arg in "$@"; do
        if [[ "$arg" == "--dangerously-skip-permissions" ]]; then
            SKIP_PERMISSIONS_MODE=true
            break
        fi
    done

    # Run pre-flight checks
    if ! run_preflight_checks; then
        _write_exit_sidecar 1
        exit 1
    fi

    # Check for stop signal before starting
    if check_stop_signal; then
        log_info "Stop signal already present - exiting without starting"
        _write_exit_sidecar 0
        exit 0
    fi

    # Run Claude with retry logic
    log_info "Pre-flight complete, launching Claude CLI..."
    run_with_retry "$@"
    exit_code=$?

    _write_exit_sidecar "${exit_code}"
    log_info "Claude wrapper exiting with code ${exit_code}"
    exit "${exit_code}"
}

# Run main with all script arguments
main "$@"
