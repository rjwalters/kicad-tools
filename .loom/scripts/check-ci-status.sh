#!/usr/bin/env bash
# check-ci-status.sh - Check CI status for the latest main branch commit
#
# Supports both GitHub and Gitea forges. Forge detection is automatic.
# - GitHub: uses Check Runs API + commit status API + Actions workflow-run API
# - Gitea: uses commit status API (mapped to check-run shape) + Actions
#   task-list API for workflow-run state
#
# A queued/in_progress workflow run with zero dispatched jobs has zero
# check-runs, so the workflow-run API call above is folded into the pending
# count independently of check-run counts -- otherwise other, unrelated,
# already-completed checks for the same commit could make this script report
# overall "success" before the primary CI workflow has run a single job
# (#5495).
#
# Usage:
#   ./check-ci-status.sh [--commit SHA] [--job "<name>"]
#
# Options:
#   --commit SHA  Check specific commit (default: HEAD)
#   --job NAME    Report only the named check-run's own status/conclusion
#                 (exact match against the check-run "name" field, e.g. the
#                 GitHub Actions job's `name:` -- "loom-worker Image Smoke
#                 Test" for the worker-image-smoke job) instead of the
#                 aggregate CI status across every check. Useful when a host
#                 cannot run a docker-dependent CI leg itself (#5748) and
#                 needs that specific job's real forge-reported outcome.
#   --json        Output results as JSON
#   --quiet       Only output status (see below)
#   --help        Show this help message
#
# Exit Codes (aggregate mode, default):
#   0 - CI passed (all checks successful)
#   1 - CI failed (at least one check failed)
#   2 - CI pending (checks still running)
#   3 - Unknown state or error
#
# Exit Codes (--job mode):
#   0 - the named job completed with a success conclusion
#   1 - the named job completed with a failure conclusion
#   2 - the named job is queued/in_progress (not yet completed)
#   3 - the named job did not run for this commit at all -- either no
#       check-run with that name exists (e.g. a Gitea repo, which has no
#       GitHub Actions job of that name -- or a workflow run that has not
#       dispatched that job yet), or it exists with conclusion "skipped"
#       (the job's own `if:` condition evaluated false for this commit,
#       e.g. `worker-image-smoke`'s docker-changed-files gate). --quiet
#       prints "not_found" for the former and "skipped" for the latter so
#       callers can tell the two apart; both are exit 3 (not a false
#       pass/fail) per the aggregate mode's existing "3 = unknown" bucket.
#
# Environment Variables:
#   LOOM_CI_TIMEOUT - Timeout for API calls in seconds (default: 10)
#   LOOM_FORGE_TYPE - Override forge detection ("github" or "gitea")
#
# Examples:
#   ./check-ci-status.sh                    # Check HEAD status
#   ./check-ci-status.sh --commit abc123    # Check specific commit
#   ./check-ci-status.sh --json             # JSON output for scripting
#   ./check-ci-status.sh --quiet && echo "CI passed!"
#   ./check-ci-status.sh --job "loom-worker Image Smoke Test" --quiet
#
# The Auditor uses this script to determine whether to skip redundant build/test
# when CI has already validated the main branch, and (--job) to look up a
# single docker-dependent job's real conclusion when it cannot run that leg
# itself locally (no docker on the Auditor host, #5748).

set -euo pipefail

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Source forge helpers for multi-forge support
source "$SCRIPT_DIR/lib/forge-helpers.sh"

# --- Color Output ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Disable colors if not a terminal
[[ ! -t 1 ]] && RED="" && GREEN="" && YELLOW="" && BLUE="" && NC=""

# --- Arguments ---
COMMIT=""
JSON_OUTPUT=false
QUIET=false
JOB_NAME=""

show_help() {
    sed -n '2,/^$/p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --commit)
            COMMIT="$2"
            shift 2
            ;;
        --job)
            JOB_NAME="$2"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --quiet)
            QUIET=true
            shift
            ;;
        --help|-h)
            show_help
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Run with --help for usage" >&2
            exit 3
            ;;
    esac
done

# --- Validation ---
if ! command -v jq &>/dev/null; then
    echo "Error: jq is required but not installed" >&2
    exit 3
fi

cd "$WORKSPACE_ROOT"

# Detect forge type
forge_detect

# For GitHub, we need the gh CLI
if [[ "$FORGE_TYPE" == "github" ]] && ! command -v gh &>/dev/null; then
    echo "Error: GitHub CLI (gh) is required but not installed" >&2
    exit 3
fi

# Get repo NWO for forge API calls
REPO_NWO="$(forge_get_repo_nwo)" || {
    echo "Error: Could not determine repository" >&2
    exit 3
}

# --- Get Commit SHA ---
if [[ -z "$COMMIT" ]]; then
    COMMIT=$(git rev-parse HEAD 2>/dev/null)
    if [[ -z "$COMMIT" ]]; then
        echo "Error: Could not determine current commit" >&2
        exit 3
    fi
fi

# Get short SHA for display
SHORT_SHA="${COMMIT:0:7}"

# --- Fetch CI Status ---
# Uses forge helpers to get combined check status from either GitHub or Gitea

get_ci_status() {
    local commit="$1"

    # Get check runs (GitHub Actions / Gitea CI statuses mapped to check-run shape)
    local check_runs
    check_runs=$(forge_get_check_runs "$REPO_NWO" "$commit") || {
        echo "Error: Failed to fetch check runs from forge API" >&2
        return 1
    }

    # Get combined status (commit status API)
    local combined_status
    combined_status=$(forge_get_commit_status "$REPO_NWO" "$commit") || {
        # Not critical if this fails - check runs are more important
        combined_status='{"state": "unknown", "statuses": []}'
    }

    # Get workflow-run state directly (independent of the Checks API). A
    # queued/in_progress workflow run with zero dispatched jobs has zero
    # check-runs and is otherwise invisible to analyze_status() -- see
    # forge_get_workflow_runs's doc comment and issue #5495.
    local workflow_runs
    workflow_runs=$(forge_get_workflow_runs "$REPO_NWO" "$commit") || {
        # Fail closed (#9879): an unreadable CI feed is PENDING, never "no
        # runs" — empty previously read as a false green through
        # analyze_status. The state marker forces the verdict to pending
        # unless a definitive failure exists.
        workflow_runs='{"workflow_runs": [], "workflow_runs_state": "unknown"}'
    }

    # Merge results
    echo "$check_runs" | jq \
        --argjson status "$combined_status" \
        --argjson wf "$workflow_runs" \
        '. + {combined_status: $status, workflow_runs: ($wf.workflow_runs // []), workflow_runs_state: ($wf.workflow_runs_state // "ok")}'
}

analyze_status() {
    local data="$1"

    local total_count
    total_count=$(echo "$data" | jq -r '.total_count // 0')

    local check_runs
    check_runs=$(echo "$data" | jq -c '.check_runs // []')

    local workflow_runs
    workflow_runs=$(echo "$data" | jq -c '.workflow_runs // []')

    local combined_state
    combined_state=$(echo "$data" | jq -r '.combined_status.state // "unknown"')

    local workflow_runs_state; workflow_runs_state=$(echo "$data" | jq -r '.workflow_runs_state // "ok"')

    # Count by status
    local completed=0
    local success=0
    local failure=0
    local pending=0
    local skipped=0

    # Analyze check runs
    while IFS= read -r run; do
        local status conclusion
        status=$(echo "$run" | jq -r '.status')
        conclusion=$(echo "$run" | jq -r '.conclusion // "null"')

        case "$status" in
            completed)
                ((completed++))
                case "$conclusion" in
                    success|neutral)
                        ((success++))
                        ;;
                    failure|timed_out|cancelled|action_required)
                        ((failure++))
                        ;;
                    skipped)
                        ((skipped++))
                        ;;
                esac
                ;;
            queued|in_progress|waiting)
                ((pending++))
                ;;
        esac
    done < <(echo "$check_runs" | jq -c '.[]')

    # Analyze workflow runs (#5495): a workflow run that is `queued` or
    # `in_progress` counts as pending even when it has dispatched zero jobs
    # yet -- and therefore has no corresponding entries at all in $check_runs
    # above. This is deliberately additive-only: a `completed` workflow run
    # contributes nothing here because its job-level detail already arrived
    # via $check_runs (double-counting it would risk over-counting success/
    # failure, not just pending).
    while IFS= read -r run; do
        local wf_status
        wf_status=$(echo "$run" | jq -r '.status')
        if [[ "$wf_status" != "completed" ]]; then
            ((pending++))
        fi
    done < <(echo "$workflow_runs" | jq -c '.[]')

    # Determine overall status
    local overall_status
    if [[ $failure -gt 0 ]]; then
        overall_status="failure"
    elif [[ $pending -gt 0 ]]; then
        overall_status="pending"
    elif [[ $success -gt 0 || $completed -gt 0 ]]; then
        overall_status="success"
    elif [[ "$combined_state" == "success" ]]; then
        overall_status="success"
    elif [[ "$combined_state" == "failure" ]]; then
        overall_status="failure"
    elif [[ "$combined_state" == "pending" ]]; then
        overall_status="pending"
    else
        overall_status="unknown"
    fi

    # Fail closed (#9879): an unreadable workflow-runs feed is PENDING, never
    # resolved. A definitive failure still dominates — "some CI failed" plus
    # "the rest is unreadable" must not soften to pending. (Statement-level
    # `&&` for the shell-budget ratchet: safe mid-function — a failing LEFT
    # side of `&&` is exempt from `set -e` (line 68), and the jq -n that
    # follows consumes the status anyway.)
    [[ "$workflow_runs_state" == "unknown" && "$overall_status" != "failure" ]] && overall_status="pending"

    # Output JSON results
    jq -n \
        --arg commit "$COMMIT" \
        --arg short_sha "$SHORT_SHA" \
        --arg status "$overall_status" \
        --arg combined_state "$combined_state" --arg wf_state "$workflow_runs_state" \
        --argjson total "$total_count" \
        --argjson completed "$completed" \
        --argjson success "$success" \
        --argjson failure "$failure" \
        --argjson pending "$pending" \
        --argjson skipped "$skipped" \
        --argjson check_runs "$check_runs" \
        --argjson workflow_runs "$workflow_runs" \
        '{
            commit: $commit,
            short_sha: $short_sha,
            status: $status,
            combined_state: $combined_state, workflow_runs_state: $wf_state,
            counts: {
                total: $total,
                completed: $completed,
                success: $success,
                failure: $failure,
                pending: $pending,
                skipped: $skipped
            },
            check_runs: $check_runs,
            workflow_runs: $workflow_runs
        }'
}

# Report the status of a single named check-run (--job), for callers that
# need one specific docker-dependent CI leg's real conclusion instead of the
# aggregate status across every check (#5748 -- the Auditor can't build/run
# the loom-worker image itself on a docker-less host, so it looks up
# worker-image-smoke's own forge-reported outcome here instead).
analyze_job_status() {
    local data="$1"
    local job_name="$2"

    # Exact match on the check-run "name" field. If a name legitimately has
    # more than one entry (e.g. a re-run left a stale completed entry
    # alongside a fresh queued one), prefer the last one -- forge Checks
    # APIs list check-runs newest-first, so the *last* array element is the
    # oldest; picking it back to front would report stale state. In
    # practice this is rare enough that either choice is defensible; "last"
    # keeps the implementation simple and matches jq's natural iteration
    # order without an explicit timestamp sort.
    local matches
    matches=$(echo "$data" | jq -c --arg name "$job_name" '[(.check_runs // [])[] | select(.name == $name)]')

    local match_count
    match_count=$(echo "$matches" | jq 'length')

    local found status conclusion html_url
    if [[ "$match_count" -eq 0 ]]; then
        found=false
        status="not_found"
        conclusion="null"
        html_url="null"
    else
        found=true
        local run
        run=$(echo "$matches" | jq -c '.[-1]')
        local run_status run_conclusion
        run_status=$(echo "$run" | jq -r '.status')
        run_conclusion=$(echo "$run" | jq -r '.conclusion // "null"')
        html_url=$(echo "$run" | jq -r '.html_url // "null"')

        case "$run_status" in
            completed)
                case "$run_conclusion" in
                    success|neutral)
                        status="success"
                        ;;
                    skipped)
                        # The job's own `if:` condition evaluated false for
                        # this commit (e.g. worker-image-smoke's
                        # docker-changed-files gate) -- it did not run, this
                        # is not a pass or a fail.
                        status="skipped"
                        ;;
                    failure|timed_out|cancelled|action_required)
                        status="failure"
                        ;;
                    *)
                        status="not_found"
                        ;;
                esac
                ;;
            queued|in_progress|waiting)
                status="pending"
                ;;
            *)
                status="not_found"
                ;;
        esac
        conclusion="$run_conclusion"
    fi

    jq -n \
        --arg job "$job_name" \
        --argjson found "$found" \
        --arg status "$status" \
        --arg conclusion "$conclusion" \
        --arg html_url "$html_url" \
        '{
            job: $job,
            found: $found,
            status: $status,
            conclusion: (if $conclusion == "null" then null else $conclusion end),
            html_url: (if $html_url == "null" then null else $html_url end)
        }'
}

# --- Main ---
CI_DATA=$(get_ci_status "$COMMIT")
if [[ $? -ne 0 ]]; then
    exit 3
fi

if [[ -n "$JOB_NAME" ]]; then
    JOB_RESULT=$(analyze_job_status "$CI_DATA" "$JOB_NAME")
    JOB_STATUS=$(echo "$JOB_RESULT" | jq -r '.status')

    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo "$JOB_RESULT"
    elif [[ "$QUIET" == "true" ]]; then
        echo "$JOB_STATUS"
    else
        echo ""
        echo -e "${BLUE}CI job '${JOB_NAME}' for commit ${SHORT_SHA}${NC}"
        echo "─────────────────────────────────"
        case "$JOB_STATUS" in
            success)
                echo -e "Status: ${GREEN}SUCCESS${NC}"
                ;;
            failure)
                echo -e "Status: ${RED}FAILURE${NC}"
                ;;
            pending)
                echo -e "Status: ${YELLOW}PENDING${NC}"
                ;;
            skipped)
                echo -e "Status: ${YELLOW}SKIPPED (not applicable for this commit)${NC}"
                ;;
            *)
                echo -e "Status: ${YELLOW}NOT FOUND${NC} (no check-run named '${JOB_NAME}' for this commit)"
                ;;
        esac
        echo ""
    fi

    case "$JOB_STATUS" in
        success) exit 0 ;;
        failure) exit 1 ;;
        pending) exit 2 ;;
        *) exit 3 ;;
    esac
fi

RESULT=$(analyze_status "$CI_DATA")
STATUS=$(echo "$RESULT" | jq -r '.status')

# --- Output ---
if [[ "$JSON_OUTPUT" == "true" ]]; then
    echo "$RESULT"
elif [[ "$QUIET" == "true" ]]; then
    echo "$STATUS"
else
    # Human-readable output
    echo ""
    echo -e "${BLUE}CI Status for commit ${SHORT_SHA}${NC}"
    echo "─────────────────────────────────"

    case "$STATUS" in
        success)
            echo -e "Overall: ${GREEN}SUCCESS${NC}"
            ;;
        failure)
            echo -e "Overall: ${RED}FAILURE${NC}"
            ;;
        pending)
            echo -e "Overall: ${YELLOW}PENDING${NC}"
            ;;
        *)
            echo -e "Overall: ${YELLOW}UNKNOWN${NC}"
            ;;
    esac

    # Show counts
    TOTAL=$(echo "$RESULT" | jq -r '.counts.total')
    SUCCESS_COUNT=$(echo "$RESULT" | jq -r '.counts.success')
    FAILURE_COUNT=$(echo "$RESULT" | jq -r '.counts.failure')
    PENDING_COUNT=$(echo "$RESULT" | jq -r '.counts.pending')

    echo ""
    echo "Checks: $TOTAL total"
    [[ "$SUCCESS_COUNT" -gt 0 ]] && echo -e "  ${GREEN}$SUCCESS_COUNT passed${NC}"
    [[ "$FAILURE_COUNT" -gt 0 ]] && echo -e "  ${RED}$FAILURE_COUNT failed${NC}"
    [[ "$PENDING_COUNT" -gt 0 ]] && echo -e "  ${YELLOW}$PENDING_COUNT pending${NC}"

    # Show failed checks
    if [[ "$FAILURE_COUNT" -gt 0 ]]; then
        echo ""
        echo "Failed checks:"
        echo "$RESULT" | jq -r '.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "cancelled") | "  - \(.name): \(.conclusion)"'
    fi

    # Show pending checks (check-runs with dispatched jobs, plus workflow
    # runs that are queued/in_progress with no dispatched jobs yet — #5495)
    if [[ "$PENDING_COUNT" -gt 0 ]]; then
        echo ""
        echo "Pending checks:"
        echo "$RESULT" | jq -r '.check_runs[] | select(.status != "completed") | "  - \(.name): \(.status)"'
        echo "$RESULT" | jq -r '.workflow_runs[] | select(.status != "completed") | "  - \(.name): \(.status) (workflow run)"'
    fi

    echo ""
fi

# --- Exit Code ---
case "$STATUS" in
    success) exit 0 ;;
    failure) exit 1 ;;
    pending) exit 2 ;;
    *) exit 3 ;;
esac
