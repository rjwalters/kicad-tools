#!/usr/bin/env bash
# Shepherd Orchestration Loop - Shell script-based deterministic orchestration
#
# This script implements the shepherd orchestration in bash, replacing the
# LLM-interpreted /shepherd slash command with deterministic shell logic.
#
# Benefits over LLM-based shepherd:
#   - No token accumulation (fresh context per phase)
#   - Deterministic behavior (shell conditionals vs LLM reasoning)
#   - Configurable polling intervals (shell sleep vs LLM polling)
#   - No context bloat (each phase is isolated)
#   - Debuggable (read shell script vs conversation history)
#
# Usage:
#   ./.loom/scripts/shepherd-loop.sh <issue-number> [options]
#
# Options:
#   --force, -f     Auto-approve, resolve conflicts, auto-merge after approval
#   --wait          Wait for human approval at each gate (explicit non-default)
#   --to <phase>    Stop after specified phase (curated, pr, approved)
#   --task-id <id>  Use specific task ID (generated if not provided)
#
# Deprecated:
#   --force-pr      (deprecated) Now the default behavior
#   --force-merge   (deprecated) Use --force or -f instead
#
# Environment Variables:
#   LOOM_CURATOR_TIMEOUT     Seconds for curator phase (default: 300)
#   LOOM_BUILDER_TIMEOUT     Seconds for builder phase (default: 1800)
#   LOOM_JUDGE_TIMEOUT       Seconds for judge phase (default: 600)
#   LOOM_DOCTOR_TIMEOUT      Seconds for doctor phase (default: 900)
#   LOOM_DOCTOR_MAX_RETRIES  Maximum doctor retry attempts (default: 3)
#   LOOM_POLL_INTERVAL       Seconds between completion checks (default: 5)
#
# Example:
#   # Shepherd issue 42 (creates PR without waiting, default)
#   ./.loom/scripts/shepherd-loop.sh 42
#
#   # Shepherd issue 42 with full automation (auto-merge)
#   ./.loom/scripts/shepherd-loop.sh 42 --force
#
#   # Shepherd with custom timeout and wait for human approval
#   LOOM_BUILDER_TIMEOUT=3600 ./.loom/scripts/shepherd-loop.sh 42 --wait

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────

CURATOR_TIMEOUT="${LOOM_CURATOR_TIMEOUT:-300}"
BUILDER_TIMEOUT="${LOOM_BUILDER_TIMEOUT:-1800}"
JUDGE_TIMEOUT="${LOOM_JUDGE_TIMEOUT:-600}"
DOCTOR_TIMEOUT="${LOOM_DOCTOR_TIMEOUT:-900}"
DOCTOR_MAX_RETRIES="${LOOM_DOCTOR_MAX_RETRIES:-3}"
STUCK_MAX_RETRIES="${LOOM_STUCK_MAX_RETRIES:-1}"
POLL_INTERVAL="${LOOM_POLL_INTERVAL:-5}"

# Marker file to prevent premature worktree cleanup during orchestration
# See issue #1485: cleanup scripts will skip worktrees with this marker
WORKTREE_MARKER_FILE=".loom-in-use"

# ─── Colors ───────────────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    CYAN=''
    NC=''
fi

# ─── Repository root detection ────────────────────────────────────────────────

# Find the repository root (works from any subdirectory including worktrees)
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            # Check if this is a worktree (has .git file, not directory)
            if [[ -f "$dir/.git" ]]; then
                local gitdir
                gitdir=$(cat "$dir/.git" | sed 's/^gitdir: //')
                # gitdir is like /path/to/repo/.git/worktrees/issue-123
                # main repo is 3 levels up from there
                local main_repo
                main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
                if [[ -d "$main_repo/.loom" ]]; then
                    echo "$main_repo"
                    return 0
                fi
            fi
            # Not a worktree, check if .loom exists here
            if [[ -d "$dir/.loom" ]]; then
                echo "$dir"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository with .loom directory" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
cd "$REPO_ROOT"

# ─── Logging ──────────────────────────────────────────────────────────────────

log() {
    echo -e "[$(date '+%H:%M:%S')] $*"
}

log_phase() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  $*${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
}

log_info() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $*"; }
log_success() { echo -e "${GREEN}[$(date '+%H:%M:%S')] ✓${NC} $*"; }
log_warn() { echo -e "${YELLOW}[$(date '+%H:%M:%S')] ⚠${NC} $*"; }
log_error() { echo -e "${RED}[$(date '+%H:%M:%S')] ✗${NC} $*"; }

# ─── Worktree marker functions ────────────────────────────────────────────────
# Marker file prevents cleanup scripts from removing worktrees during orchestration
# See issue #1485: worktree was being deleted while judge phase still needed it

# Track the worktree path for cleanup in exit handler
ACTIVE_WORKTREE_PATH=""

create_worktree_marker() {
    local worktree_path="$1"
    if [[ -d "$worktree_path" ]]; then
        local marker_path="${worktree_path}/${WORKTREE_MARKER_FILE}"
        local timestamp
        timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        cat > "$marker_path" <<EOF
{
  "shepherd_task_id": "$TASK_ID",
  "issue": $ISSUE,
  "created_at": "$timestamp",
  "pid": $$
}
EOF
        ACTIVE_WORKTREE_PATH="$worktree_path"
        log_info "Created worktree marker: $marker_path"
    fi
}

# shellcheck disable=SC2120  # Function designed with optional argument, called without args when using ACTIVE_WORKTREE_PATH
remove_worktree_marker() {
    local worktree_path="${1:-$ACTIVE_WORKTREE_PATH}"
    if [[ -n "$worktree_path" && -f "${worktree_path}/${WORKTREE_MARKER_FILE}" ]]; then
        rm -f "${worktree_path}/${WORKTREE_MARKER_FILE}"
        log_info "Removed worktree marker"
    fi
    ACTIVE_WORKTREE_PATH=""
}

# ─── Parse arguments ──────────────────────────────────────────────────────────

ISSUE=""
MODE="force-pr"
STOP_AFTER=""
TASK_ID=""

show_help() {
    cat <<EOF
${BLUE}shepherd-loop.sh - Shell-based shepherd orchestration${NC}

${YELLOW}USAGE:${NC}
    shepherd-loop.sh <issue-number> [OPTIONS]

${YELLOW}OPTIONS:${NC}
    --force, -f     Auto-approve, resolve conflicts, auto-merge after approval
    --wait          Wait for human approval at each gate (explicit non-default)
    --to <phase>    Stop after specified phase (curated, pr, approved)
    --task-id <id>  Use specific task ID (generated if not provided)
    --help          Show this help message

${YELLOW}DEPRECATED:${NC}
    --force-pr      (deprecated) Now the default behavior
    --force-merge   (deprecated) Use --force or -f instead

${YELLOW}PHASES:${NC}
    1. Curator    - Enhance issue with implementation guidance
    2. Approval   - Wait for loom:issue label (or auto-approve in force mode)
    3. Builder    - Create worktree, implement, create PR
    4. Judge      - Review PR, approve or request changes
    5. Doctor     - Address requested changes (if any)
    6. Merge      - Auto-merge (--force) or wait for human

${YELLOW}ENVIRONMENT:${NC}
    LOOM_CURATOR_TIMEOUT     Seconds for curator phase (default: 300)
    LOOM_BUILDER_TIMEOUT     Seconds for builder phase (default: 1800)
    LOOM_JUDGE_TIMEOUT       Seconds for judge phase (default: 600)
    LOOM_DOCTOR_TIMEOUT      Seconds for doctor phase (default: 900)
    LOOM_DOCTOR_MAX_RETRIES  Maximum doctor retry attempts (default: 3)
    LOOM_POLL_INTERVAL       Seconds between completion checks (default: 5)

${YELLOW}EXAMPLES:${NC}
    # Create PR without waiting (default behavior)
    shepherd-loop.sh 42

    # Full automation with auto-merge
    shepherd-loop.sh 42 --force
    shepherd-loop.sh 42 -f

    # Wait for human approval at each gate
    shepherd-loop.sh 42 --wait

    # Stop after curation (for review before building)
    shepherd-loop.sh 42 --to curated

EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f)
            MODE="force-merge"
            shift
            ;;
        --wait)
            MODE="normal"
            shift
            ;;
        --force-pr)
            # Deprecated: now the default behavior
            log_warn "Flag --force-pr is deprecated (now default behavior)"
            MODE="force-pr"
            shift
            ;;
        --force-merge)
            # Deprecated: use --force or -f instead
            log_warn "Flag --force-merge is deprecated (use --force or -f instead)"
            MODE="force-merge"
            shift
            ;;
        --to)
            STOP_AFTER="$2"
            shift 2
            ;;
        --task-id)
            TASK_ID="$2"
            shift 2
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        -*)
            log_error "Unknown option: $1"
            echo "Use --help for usage information" >&2
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

# Generate task ID if not provided (7 lowercase hex chars)
if [[ -z "$TASK_ID" ]]; then
    TASK_ID=$(head -c 4 /dev/urandom | xxd -p | cut -c1-7)
fi

# ─── Label helpers ────────────────────────────────────────────────────────────

has_label() {
    local issue="$1"
    local label="$2"
    local labels
    labels=$(gh issue view "$issue" --json labels --jq '.labels[].name' 2>/dev/null) || return 1
    echo "$labels" | grep -q "^${label}$"
}

has_label_pr() {
    local pr="$1"
    local label="$2"
    local labels
    labels=$(gh pr view "$pr" --json labels --jq '.labels[].name' 2>/dev/null) || return 1
    echo "$labels" | grep -q "^${label}$"
}

add_label() {
    local issue="$1"
    local label="$2"
    gh issue edit "$issue" --add-label "$label" >/dev/null 2>&1
}

remove_label() {
    local issue="$1"
    local label="$2"
    gh issue edit "$issue" --remove-label "$label" >/dev/null 2>&1 || true
}

add_label_pr() {
    local pr="$1"
    local label="$2"
    gh pr edit "$pr" --add-label "$label" >/dev/null 2>&1
}

remove_label_pr() {
    local pr="$1"
    local label="$2"
    gh pr edit "$pr" --remove-label "$label" >/dev/null 2>&1 || true
}

# ─── Rate limit checking ─────────────────────────────────────────────────────

# Check if Claude API usage is too high to spawn agents.
# Uses check-usage.sh (requires claude-monitor) to detect approaching limits.
# Returns 0 if rate-limited (should NOT spawn), 1 if OK to proceed.
# Silently returns 1 (OK) if check-usage.sh is unavailable.
RATE_LIMIT_THRESHOLD="${LOOM_RATE_LIMIT_THRESHOLD:-90}"

is_rate_limited() {
    local usage_script="$REPO_ROOT/.loom/scripts/check-usage.sh"
    if [[ ! -x "$usage_script" ]]; then
        return 1  # No usage script, assume OK
    fi

    local usage_json
    usage_json=$("$usage_script" 2>/dev/null) || return 1

    # Check session percent
    local session_pct
    session_pct=$(echo "$usage_json" | jq -r '.session_percent // 0' 2>/dev/null) || return 1
    if [[ -n "$session_pct" && "$session_pct" != "null" ]]; then
        # Remove decimal part for integer comparison
        local session_int="${session_pct%.*}"
        if [[ "$session_int" -ge "$RATE_LIMIT_THRESHOLD" ]]; then
            log_warn "Rate limited: session usage at ${session_pct}% (threshold: ${RATE_LIMIT_THRESHOLD}%)"
            return 0
        fi
    fi

    return 1  # Not rate-limited
}

# ─── Shutdown signal handling ─────────────────────────────────────────────────

check_shutdown() {
    # Check for global shutdown signal
    if [[ -f "$REPO_ROOT/.loom/stop-shepherds" ]]; then
        return 0
    fi
    # Check for issue-specific abort
    if has_label "$ISSUE" "loom:abort"; then
        return 0
    fi
    return 1
}

handle_shutdown() {
    local phase="${1:-unknown}"
    log_warn "Shutdown signal detected during $phase phase"

    # Report blocked milestone
    if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
        "$REPO_ROOT/.loom/scripts/report-milestone.sh" blocked \
            --task-id "$TASK_ID" \
            --reason "shutdown_signal" \
            --details "Graceful shutdown during $phase phase" \
            --quiet || true
    fi

    log_info "Cleaning up and exiting gracefully..."
    exit 0
}

# ─── Exit handling for error status reporting ─────────────────────────────────

# Track last failure for exit handler
LAST_FAILURE_REASON=""
LAST_FAILURE_PHASE=""

# Report error status on non-zero exit
report_final_status() {
    local exit_code=$?

    # Always remove worktree marker on exit (prevents orphaned markers)
    # The marker prevents cleanup scripts from removing the worktree during orchestration
    remove_worktree_marker "$ACTIVE_WORKTREE_PATH"

    # Only report errors for non-zero exits (excluding shutdown which is 0)
    # Also requires TASK_ID to be set and progress file to exist (created after 'started' milestone)
    if [[ $exit_code -ne 0 ]] && [[ -n "$TASK_ID" ]]; then
        local progress_file="$REPO_ROOT/.loom/progress/shepherd-${TASK_ID}.json"
        if [[ -f "$progress_file" ]]; then
            local error_msg="${LAST_FAILURE_REASON:-Orchestration failed with exit code $exit_code}"
            if [[ -n "$LAST_FAILURE_PHASE" ]]; then
                error_msg="$LAST_FAILURE_PHASE phase: $error_msg"
            fi

            if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
                "$REPO_ROOT/.loom/scripts/report-milestone.sh" error \
                    --task-id "$TASK_ID" \
                    --error "$error_msg" \
                    --quiet || true
            fi
        fi
    fi
}

# Install exit trap
trap 'report_final_status' EXIT

# Helper to record failure reason before exiting
fail_with_reason() {
    LAST_FAILURE_PHASE="$1"
    LAST_FAILURE_REASON="$2"
    exit 1
}

# ─── Phase execution ──────────────────────────────────────────────────────────

# Run a phase worker and wait for completion
# Usage: run_phase <role> <name> <timeout> [--worktree <path>] [--args <args>] [--phase <phase>] [--pr <N>]
run_phase() {
    local role="$1"
    local name="$2"
    local timeout="$3"
    shift 3

    local worktree=""
    local args=""
    local phase=""
    local pr_number=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --worktree)
                worktree="$2"
                shift 2
                ;;
            --args)
                args="$2"
                shift 2
                ;;
            --phase)
                phase="$2"
                shift 2
                ;;
            --pr)
                pr_number="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    log_info "Starting $role worker: $name"

    # Build spawn command
    local spawn_cmd=(
        "$REPO_ROOT/.loom/scripts/agent-spawn.sh"
        --role "$role"
        --name "$name"
        --on-demand
    )

    if [[ -n "$args" ]]; then
        spawn_cmd+=(--args "$args")
    fi

    if [[ -n "$worktree" ]]; then
        spawn_cmd+=(--worktree "$worktree")
    fi

    # Spawn the worker
    if ! "${spawn_cmd[@]}"; then
        log_error "Failed to spawn $role worker"
        return 1
    fi

    # Wait for completion with signal checking
    # Pass phase info for activity-based completion detection (see issue #1461)
    local wait_exit=0
    local wait_args=(
        "$name"
        --timeout "$timeout"
        --poll-interval "$POLL_INTERVAL"
        --issue "$ISSUE"
    )

    if [[ -n "$phase" ]]; then
        wait_args+=(--phase "$phase")
    fi
    if [[ -n "$worktree" ]]; then
        wait_args+=(--worktree "$worktree")
    fi
    if [[ -n "$pr_number" ]]; then
        wait_args+=(--pr "$pr_number")
    fi

    "$REPO_ROOT/.loom/scripts/agent-wait-bg.sh" "${wait_args[@]}" || wait_exit=$?

    # Clean up the worker session
    "$REPO_ROOT/.loom/scripts/agent-destroy.sh" "$name" --force >/dev/null 2>&1 || true

    # Check exit code
    if [[ $wait_exit -eq 3 ]]; then
        # Shutdown signal detected during wait
        return 3
    elif [[ $wait_exit -ne 0 ]]; then
        log_warn "$role worker completed with exit code $wait_exit"
    fi

    return $wait_exit
}

# Run a phase with automatic retry on stuck detection (exit code 4)
# Uses LOOM_STUCK_ACTION=retry to enable kill-and-retry behavior.
# On exit code 4, retries the phase up to STUCK_MAX_RETRIES times.
# All arguments are passed through to run_phase.
run_phase_with_retry() {
    local role="$1"
    local stuck_retries=0
    local phase_exit=0

    while true; do
        phase_exit=0
        LOOM_STUCK_ACTION=retry run_phase "$@" || phase_exit=$?

        if [[ $phase_exit -ne 4 ]]; then
            # Not a stuck exit - return as-is (0=ok, 3=shutdown, other=error)
            return $phase_exit
        fi

        stuck_retries=$((stuck_retries + 1))
        if [[ $stuck_retries -gt $STUCK_MAX_RETRIES ]]; then
            log_error "$role worker stuck after $STUCK_MAX_RETRIES retry attempt(s) - giving up"
            return 4
        fi

        log_warn "$role worker was stuck - retrying (attempt $stuck_retries of $STUCK_MAX_RETRIES)"

        # Report retry milestone
        if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
            "$REPO_ROOT/.loom/scripts/report-milestone.sh" heartbeat \
                --task-id "$TASK_ID" \
                --action "retrying stuck $role (attempt $stuck_retries)" \
                --quiet || true
        fi
    done
}

# ─── Get PR for issue ─────────────────────────────────────────────────────────

# Find a PR for an issue, trying multiple linking patterns
# Usage: get_pr_for_issue <issue> [state]
# state: open (default), closed, or all
get_pr_for_issue() {
    local issue="$1"
    local state="${2:-open}"
    local pr

    # Try multiple linking patterns (search body for these phrases)
    for pattern in "Closes #${issue}" "Fixes #${issue}" "Resolves #${issue}"; do
        pr=$(gh pr list --search "$pattern" --state "$state" --json number --jq '.[0].number' 2>/dev/null) || true
        if [[ -n "$pr" && "$pr" != "null" ]]; then
            echo "$pr"
            return 0
        fi
    done

    # Fallback: search by branch name
    pr=$(gh pr list --head "feature/issue-${issue}" --state "$state" --json number --jq '.[0].number' 2>/dev/null) || true
    echo "$pr"
}

# ─── Main orchestration ───────────────────────────────────────────────────────

main() {
    local start_time
    start_time=$(date +%s)

    # Announce orchestration
    log_phase "SHEPHERD ORCHESTRATION STARTED"
    echo ""
    log_info "Issue: #$ISSUE"
    log_info "Mode: $MODE"
    log_info "Task ID: $TASK_ID"
    log_info "Repository: $REPO_ROOT"
    echo ""

    # Verify issue exists
    if ! gh issue view "$ISSUE" --json number >/dev/null 2>&1; then
        log_error "Issue #$ISSUE does not exist"
        exit 1
    fi

    # Check if issue is already closed
    local issue_state
    issue_state=$(gh issue view "$ISSUE" --json state --jq '.state' 2>/dev/null)
    if [[ "$issue_state" == "CLOSED" ]]; then
        log_info "Issue #$ISSUE is already closed - no orchestration needed"
        exit 0
    fi

    # Check if issue is blocked
    if has_label "$ISSUE" "loom:blocked"; then
        if [[ "$MODE" == "force-merge" ]]; then
            log_warn "Issue #$ISSUE has loom:blocked label - proceeding anyway (--force mode)"
        else
            log_error "Issue #$ISSUE has loom:blocked label - refusing to process"
            log_info "Use --force to override blocked status"
            exit 1
        fi
    fi

    local issue_title
    issue_title=$(gh issue view "$ISSUE" --json title --jq '.title' 2>/dev/null)
    log_info "Title: $issue_title"
    echo ""

    # Report started milestone
    if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
        "$REPO_ROOT/.loom/scripts/report-milestone.sh" started \
            --task-id "$TASK_ID" \
            --issue "$ISSUE" \
            --mode "$MODE" \
            --quiet || true
    fi

    # Track completed phases for final report
    local completed_phases=()
    local pr_number=""

    # ─── PHASE 1: Curator ─────────────────────────────────────────────────────

    if ! has_label "$ISSUE" "loom:curated"; then
        log_phase "PHASE 1: CURATOR"

        if check_shutdown; then
            handle_shutdown "curator"
        fi

        # Report phase
        if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
            "$REPO_ROOT/.loom/scripts/report-milestone.sh" phase_entered \
                --task-id "$TASK_ID" \
                --phase "curator" \
                --quiet || true
        fi

        local curator_exit=0
        run_phase_with_retry "curator" "curator-issue-${ISSUE}" "$CURATOR_TIMEOUT" \
            --phase "curator" \
            --args "$ISSUE" || curator_exit=$?

        if [[ $curator_exit -eq 3 ]]; then
            handle_shutdown "curator"
        elif [[ $curator_exit -eq 4 ]]; then
            log_warn "Curator stuck after retry - skipping curation, proceeding to builder"
            completed_phases+=("Curator (stuck - skipped)")
            # Don't block issue for curator failure - it's not critical
        else
            # Validate curator phase
            if ! "$REPO_ROOT/.loom/scripts/validate-phase.sh" curator "$ISSUE" --task-id "$TASK_ID"; then
                log_error "Curator phase validation failed"
                fail_with_reason "curator" "validation failed"
            fi

            completed_phases+=("Curator")
            log_success "Curator phase complete"
        fi
    else
        log_info "Issue already curated, skipping curator phase"
        completed_phases+=("Curator (skipped)")
    fi

    if [[ "$STOP_AFTER" == "curated" ]]; then
        log_phase "STOPPING: Reached --to curated"
        exit 0
    fi

    # ─── PHASE 2: Approval Gate ───────────────────────────────────────────────

    log_phase "PHASE 2: APPROVAL GATE"

    if check_shutdown; then
        handle_shutdown "approval"
    fi

    if has_label "$ISSUE" "loom:issue"; then
        log_info "Issue already approved (has loom:issue label)"
        completed_phases+=("Approval (already approved)")
    elif [[ "$MODE" == "force-pr" || "$MODE" == "force-merge" ]]; then
        log_info "Auto-approving issue (force mode)"
        add_label "$ISSUE" "loom:issue"
        completed_phases+=("Approval (auto-approved)")
        log_success "Issue approved"
    else
        log_info "Waiting for human approval (loom:issue label)..."
        log_info "To approve: gh issue edit $ISSUE --add-label loom:issue"

        # Poll until approved or shutdown
        while ! has_label "$ISSUE" "loom:issue"; do
            if check_shutdown; then
                handle_shutdown "approval"
            fi
            sleep "$POLL_INTERVAL"
        done

        completed_phases+=("Approval (human approved)")
        log_success "Issue approved by human"
    fi

    if [[ "$STOP_AFTER" == "approved" ]]; then
        log_phase "STOPPING: Reached --to approved"
        exit 0
    fi

    # ─── Stage Detection: Check for existing PRs ─────────────────────────────

    # Check if a PR already exists for this issue before starting the builder phase.
    # This handles cases where:
    #   - Shepherd was interrupted and restarted after builder completed
    #   - PR was created manually
    #   - Previous shepherd run failed after PR creation
    local existing_pr=""
    local worktree_path="$REPO_ROOT/.loom/worktrees/issue-${ISSUE}"

    existing_pr=$(get_pr_for_issue "$ISSUE")
    if [[ -n "$existing_pr" && "$existing_pr" != "null" ]]; then
        log_info "Existing PR #$existing_pr found for issue #$ISSUE"
        log_info "Skipping builder phase, proceeding to judge"

        # Report milestone for existing PR
        if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
            "$REPO_ROOT/.loom/scripts/report-milestone.sh" pr_created \
                --task-id "$TASK_ID" \
                --pr-number "$existing_pr" \
                --quiet || true
        fi

        # Ensure issue has loom:building label (may not have it if PR was created manually)
        if ! has_label "$ISSUE" "loom:building"; then
            remove_label "$ISSUE" "loom:issue"
            add_label "$ISSUE" "loom:building"
        fi

        # Create marker if worktree exists (doctor phase may need it)
        if [[ -d "$worktree_path" ]]; then
            create_worktree_marker "$worktree_path"
        fi

        completed_phases+=("Builder (skipped - PR #$existing_pr exists)")
        pr_number="$existing_pr"
    else
        # ─── PHASE 3: Builder ─────────────────────────────────────────────────────

        log_phase "PHASE 3: BUILDER"

        if check_shutdown; then
            handle_shutdown "builder"
        fi

        # Check rate limits before spawning the builder (most expensive phase)
        if is_rate_limited; then
            log_error "Cannot spawn builder: API rate limit exceeded"
            log_info "Issue #$ISSUE will remain available for retry when limits reset"
            # Don't claim the issue - leave it for retry
            fail_with_reason "builder" "API rate limit exceeded (session usage >= ${RATE_LIMIT_THRESHOLD}%)"
        fi

        # Report phase
        if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
            "$REPO_ROOT/.loom/scripts/report-milestone.sh" phase_entered \
                --task-id "$TASK_ID" \
                --phase "builder" \
                --quiet || true
        fi

        # Claim the issue
        remove_label "$ISSUE" "loom:issue"
        add_label "$ISSUE" "loom:building"

        # Create worktree
        if [[ ! -d "$worktree_path" ]]; then
            log_info "Creating worktree..."
            "$REPO_ROOT/.loom/scripts/worktree.sh" "$ISSUE" >/dev/null 2>&1 || {
                log_error "Failed to create worktree"
                fail_with_reason "builder" "failed to create worktree"
            }

            # Report worktree created
            if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
                "$REPO_ROOT/.loom/scripts/report-milestone.sh" worktree_created \
                    --task-id "$TASK_ID" \
                    --path "$worktree_path" \
                    --quiet || true
            fi
        fi

        # Create marker to prevent premature cleanup during orchestration (issue #1485)
        # This marker is checked by clean.sh and will be removed on orchestration completion
        create_worktree_marker "$worktree_path"

        local builder_exit=0
        run_phase_with_retry "builder" "builder-issue-${ISSUE}" "$BUILDER_TIMEOUT" \
            --phase "builder" \
            --worktree "$worktree_path" \
            --args "$ISSUE" || builder_exit=$?

        if [[ $builder_exit -eq 3 ]]; then
            # Revert claim on shutdown
            remove_label "$ISSUE" "loom:building"
            add_label "$ISSUE" "loom:issue"
            handle_shutdown "builder"
        elif [[ $builder_exit -eq 4 ]]; then
            log_error "Builder stuck after retry - marking issue as blocked"
            gh issue edit "$ISSUE" --remove-label "loom:building" --add-label "loom:blocked" >/dev/null 2>&1 || true
            gh issue comment "$ISSUE" --body "**Shepherd blocked**: Builder agent was stuck and did not recover after retry. Diagnostics saved to \`.loom/diagnostics/\`." >/dev/null 2>&1 || true
            fail_with_reason "builder" "agent stuck after retry"
        fi

        # Validate builder phase
        if ! "$REPO_ROOT/.loom/scripts/validate-phase.sh" builder "$ISSUE" \
            --worktree "$worktree_path" \
            --task-id "$TASK_ID"; then
            log_error "Builder phase validation failed"
            fail_with_reason "builder" "validation failed"
        fi

        # Get PR number
        pr_number=$(get_pr_for_issue "$ISSUE")
        if [[ -z "$pr_number" || "$pr_number" == "null" ]]; then
            log_error "Could not find PR for issue #$ISSUE"
            fail_with_reason "builder" "could not find PR for issue"
        fi

        # Report PR created
        if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
            "$REPO_ROOT/.loom/scripts/report-milestone.sh" pr_created \
                --task-id "$TASK_ID" \
                --pr-number "$pr_number" \
                --quiet || true
        fi

        completed_phases+=("Builder (PR #$pr_number)")
        log_success "Builder phase complete - PR #$pr_number created"
    fi

    # ─── PHASE 4/5: Judge/Doctor Loop ─────────────────────────────────────────

    local doctor_attempts=0
    local pr_approved=false

    while [[ "$pr_approved" != "true" ]] && [[ $doctor_attempts -lt $DOCTOR_MAX_RETRIES ]]; do
        log_phase "PHASE 4: JUDGE (attempt $((doctor_attempts + 1)))"

        if check_shutdown; then
            handle_shutdown "judge"
        fi

        # Report phase
        if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
            "$REPO_ROOT/.loom/scripts/report-milestone.sh" phase_entered \
                --task-id "$TASK_ID" \
                --phase "judge" \
                --quiet || true
        fi

        local judge_exit=0
        run_phase_with_retry "judge" "judge-issue-${ISSUE}" "$JUDGE_TIMEOUT" \
            --phase "judge" \
            --pr "$pr_number" \
            --args "$pr_number" || judge_exit=$?

        if [[ $judge_exit -eq 3 ]]; then
            handle_shutdown "judge"
        elif [[ $judge_exit -eq 4 ]]; then
            log_error "Judge stuck after retry - marking issue as blocked"
            gh issue edit "$ISSUE" --remove-label "loom:building" --add-label "loom:blocked" >/dev/null 2>&1 || true
            gh issue comment "$ISSUE" --body "**Shepherd blocked**: Judge agent was stuck and did not recover after retry. Diagnostics saved to \`.loom/diagnostics/\`." >/dev/null 2>&1 || true
            fail_with_reason "judge" "agent stuck after retry"
        fi

        # Validate judge phase
        if ! "$REPO_ROOT/.loom/scripts/validate-phase.sh" judge "$ISSUE" \
            --pr "$pr_number" \
            --task-id "$TASK_ID"; then
            log_error "Judge phase validation failed"
            fail_with_reason "judge" "validation failed"
        fi

        # Check result
        if has_label_pr "$pr_number" "loom:pr"; then
            pr_approved=true
            completed_phases+=("Judge (approved)")
            log_success "PR #$pr_number approved by Judge"
        elif has_label_pr "$pr_number" "loom:changes-requested"; then
            log_warn "Judge requested changes on PR #$pr_number"
            completed_phases+=("Judge (changes requested)")

            doctor_attempts=$((doctor_attempts + 1))

            if [[ $doctor_attempts -ge $DOCTOR_MAX_RETRIES ]]; then
                log_error "Doctor max retries ($DOCTOR_MAX_RETRIES) exceeded"
                # Atomic transition: loom:building -> loom:blocked (mutually exclusive states)
                gh issue edit "$ISSUE" --remove-label "loom:building" --add-label "loom:blocked" >/dev/null 2>&1 || true
                gh issue comment "$ISSUE" --body "**Shepherd blocked**: Doctor could not resolve Judge feedback after $DOCTOR_MAX_RETRIES attempts." >/dev/null 2>&1 || true
                fail_with_reason "doctor" "max retries ($DOCTOR_MAX_RETRIES) exceeded"
            fi

            # ─── Doctor Phase ─────────────────────────────────────────────

            log_phase "PHASE 5: DOCTOR (attempt $doctor_attempts)"

            if check_shutdown; then
                handle_shutdown "doctor"
            fi

            # Report phase
            if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
                "$REPO_ROOT/.loom/scripts/report-milestone.sh" phase_entered \
                    --task-id "$TASK_ID" \
                    --phase "doctor" \
                    --quiet || true
            fi

            local doctor_exit=0
            run_phase_with_retry "doctor" "doctor-issue-${ISSUE}" "$DOCTOR_TIMEOUT" \
                --phase "doctor" \
                --pr "$pr_number" \
                --worktree "$worktree_path" \
                --args "$pr_number" || doctor_exit=$?

            if [[ $doctor_exit -eq 3 ]]; then
                handle_shutdown "doctor"
            elif [[ $doctor_exit -eq 4 ]]; then
                log_error "Doctor stuck after retry - marking issue as blocked"
                gh issue edit "$ISSUE" --remove-label "loom:building" --add-label "loom:blocked" >/dev/null 2>&1 || true
                gh issue comment "$ISSUE" --body "**Shepherd blocked**: Doctor agent was stuck and did not recover after retry. Diagnostics saved to \`.loom/diagnostics/\`." >/dev/null 2>&1 || true
                fail_with_reason "doctor" "agent stuck after retry"
            fi

            # Validate doctor phase
            if ! "$REPO_ROOT/.loom/scripts/validate-phase.sh" doctor "$ISSUE" \
                --pr "$pr_number" \
                --task-id "$TASK_ID"; then
                log_error "Doctor phase validation failed"
                fail_with_reason "doctor" "validation failed"
            fi

            completed_phases+=("Doctor (fixes applied)")
            log_success "Doctor applied fixes"
        else
            log_error "Unexpected state: PR has neither loom:pr nor loom:changes-requested"
            fail_with_reason "judge" "PR has neither loom:pr nor loom:changes-requested"
        fi
    done

    if [[ "$STOP_AFTER" == "pr" ]]; then
        log_phase "STOPPING: Reached --to pr"
        exit 0
    fi

    # ─── PHASE 6: Merge Gate ──────────────────────────────────────────────────

    log_phase "PHASE 6: MERGE GATE"

    if check_shutdown; then
        handle_shutdown "merge"
    fi

    if [[ "$MODE" == "force-merge" ]]; then
        log_info "Auto-merging PR #$pr_number (force-merge mode)"

        # Merge via merge-pr.sh
        if "$REPO_ROOT/.loom/scripts/merge-pr.sh" "$pr_number" --cleanup-worktree; then
            completed_phases+=("Merge (auto-merged)")
            log_success "PR #$pr_number merged successfully"
        else
            log_error "Failed to merge PR #$pr_number"
            # Atomic transition: loom:building -> loom:blocked (mutually exclusive states)
            gh issue edit "$ISSUE" --remove-label "loom:building" --add-label "loom:blocked" >/dev/null 2>&1 || true
            gh issue comment "$ISSUE" --body "**Shepherd blocked**: Failed to merge PR #$pr_number. Branch may be out of date or have merge conflicts." >/dev/null 2>&1 || true
            fail_with_reason "merge" "failed to merge PR #$pr_number"
        fi
    elif [[ "$MODE" == "force-pr" ]]; then
        log_info "Stopping at loom:pr state (force-pr mode)"
        log_info "PR #$pr_number is approved and ready for human merge"
        completed_phases+=("Merge (awaiting human)")
    else
        log_info "Waiting for human to merge PR #$pr_number..."
        log_info "To merge: gh pr merge $pr_number --squash --delete-branch"

        # Poll until merged or shutdown
        while true; do
            if check_shutdown; then
                handle_shutdown "merge"
            fi

            local pr_state
            pr_state=$(gh pr view "$pr_number" --json state --jq '.state' 2>/dev/null) || true

            if [[ "$pr_state" == "MERGED" ]]; then
                completed_phases+=("Merge (human merged)")
                log_success "PR #$pr_number merged by human"
                break
            elif [[ "$pr_state" == "CLOSED" ]]; then
                log_warn "PR #$pr_number was closed without merging"
                completed_phases+=("Merge (closed)")
                break
            fi

            sleep "$POLL_INTERVAL"
        done
    fi

    # ─── Complete ─────────────────────────────────────────────────────────────

    local end_time
    end_time=$(date +%s)
    local duration=$((end_time - start_time))

    # Report completion
    if [[ -x "$REPO_ROOT/.loom/scripts/report-milestone.sh" ]]; then
        "$REPO_ROOT/.loom/scripts/report-milestone.sh" completed \
            --task-id "$TASK_ID" \
            --pr-merged \
            --quiet || true
    fi

    log_phase "SHEPHERD ORCHESTRATION COMPLETE"
    echo ""
    log_info "Issue: #$ISSUE - $issue_title"
    log_info "Mode: $MODE"
    log_info "Duration: ${duration}s"
    echo ""
    log_info "Phases completed:"
    for phase in "${completed_phases[@]}"; do
        echo "  - $phase"
    done
    echo ""
    log_success "Orchestration complete!"
}

# ─── Run ──────────────────────────────────────────────────────────────────────

main
