#!/usr/bin/env bash
# check-ci-status.sh - Check GitHub Actions CI status for the latest main branch commit
#
# Usage:
#   ./check-ci-status.sh [--commit SHA]
#
# Options:
#   --commit SHA  Check specific commit (default: HEAD)
#   --json        Output results as JSON
#   --quiet       Only output status: success, failure, pending, or unknown
#   --help        Show this help message
#
# Exit Codes:
#   0 - CI passed (all checks successful)
#   1 - CI failed (at least one check failed)
#   2 - CI pending (checks still running)
#   3 - Unknown state or error
#
# Environment Variables:
#   LOOM_CI_TIMEOUT - Timeout for API calls in seconds (default: 10)
#
# Examples:
#   ./check-ci-status.sh                    # Check HEAD status
#   ./check-ci-status.sh --commit abc123    # Check specific commit
#   ./check-ci-status.sh --json             # JSON output for scripting
#   ./check-ci-status.sh --quiet && echo "CI passed!"
#
# The Auditor uses this script to determine whether to skip redundant build/test
# when CI has already validated the main branch.

set -euo pipefail

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

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
if ! command -v gh &>/dev/null; then
    echo "Error: GitHub CLI (gh) is required but not installed" >&2
    exit 3
fi

if ! command -v jq &>/dev/null; then
    echo "Error: jq is required but not installed" >&2
    exit 3
fi

cd "$WORKSPACE_ROOT"

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
# Use GitHub CLI to get combined check status
# This combines both status API and check runs API

get_ci_status() {
    local commit="$1"

    # Get check runs (GitHub Actions workflow runs)
    local check_runs
    check_runs=$(gh api "repos/{owner}/{repo}/commits/$commit/check-runs" \
        --header "Accept: application/vnd.github+json" \
        --jq '{
            total_count: .total_count,
            check_runs: [.check_runs[] | {
                name: .name,
                status: .status,
                conclusion: .conclusion,
                html_url: .html_url
            }]
        }' 2>/dev/null) || {
        echo "Error: Failed to fetch check runs from GitHub API" >&2
        return 1
    }

    # Get combined status (commit status API - older checks)
    local combined_status
    combined_status=$(gh api "repos/{owner}/{repo}/commits/$commit/status" \
        --header "Accept: application/vnd.github+json" \
        --jq '{
            state: .state,
            statuses: [.statuses[] | {
                context: .context,
                state: .state,
                target_url: .target_url
            }]
        }' 2>/dev/null) || {
        # Not critical if this fails - check runs are more important
        combined_status='{"state": "unknown", "statuses": []}'
    }

    # Merge results
    echo "$check_runs" | jq --argjson status "$combined_status" '. + {combined_status: $status}'
}

analyze_status() {
    local data="$1"

    local total_count
    total_count=$(echo "$data" | jq -r '.total_count // 0')

    local check_runs
    check_runs=$(echo "$data" | jq -c '.check_runs // []')

    local combined_state
    combined_state=$(echo "$data" | jq -r '.combined_status.state // "unknown"')

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

    # Output JSON results
    jq -n \
        --arg commit "$COMMIT" \
        --arg short_sha "$SHORT_SHA" \
        --arg status "$overall_status" \
        --arg combined_state "$combined_state" \
        --argjson total "$total_count" \
        --argjson completed "$completed" \
        --argjson success "$success" \
        --argjson failure "$failure" \
        --argjson pending "$pending" \
        --argjson skipped "$skipped" \
        --argjson check_runs "$check_runs" \
        '{
            commit: $commit,
            short_sha: $short_sha,
            status: $status,
            combined_state: $combined_state,
            counts: {
                total: $total,
                completed: $completed,
                success: $success,
                failure: $failure,
                pending: $pending,
                skipped: $skipped
            },
            check_runs: $check_runs
        }'
}

# --- Main ---
CI_DATA=$(get_ci_status "$COMMIT")
if [[ $? -ne 0 ]]; then
    exit 3
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

    # Show pending checks
    if [[ "$PENDING_COUNT" -gt 0 ]]; then
        echo ""
        echo "Pending checks:"
        echo "$RESULT" | jq -r '.check_runs[] | select(.status != "completed") | "  - \(.name): \(.status)"'
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
