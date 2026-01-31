#!/bin/bash
# record-blocked-reason.sh - Record error class when an issue is blocked
#
# Usage:
#   record-blocked-reason.sh <issue-number> --error-class <class> [--phase <phase>] [--details <msg>]
#
# Records structured failure metadata in daemon-state.json for retry classification.
# Called by loom-shepherd when transitioning issues to loom:blocked.
#
# Error classes:
#   builder_validation    - Builder phase validation failed
#   builder_stuck         - Builder agent stuck and didn't recover
#   judge_validation      - Judge phase validation failed
#   judge_stuck           - Judge agent stuck and didn't recover
#   doctor_exhausted      - Doctor max retries exceeded
#   doctor_stuck          - Doctor agent stuck and didn't recover
#   merge_failed          - PR merge failed (conflicts, checks)
#   rate_limited          - API rate limit exceeded
#   worktree_failed       - Failed to create worktree
#   skip_precondition     - --from flag precondition failed (e.g., no PR exists, PR not approved)
#   unknown               - Unclassified error

set -euo pipefail

# Find the repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            if [[ -f "$dir/.git" ]]; then
                local gitdir
                gitdir=$(sed 's/^gitdir: //' "$dir/.git")
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
    echo "Error: Not in a git repository with .loom directory" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
DAEMON_STATE_FILE="$REPO_ROOT/.loom/daemon-state.json"

# Parse arguments
ISSUE=""
ERROR_CLASS="unknown"
PHASE=""
DETAILS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --error-class)
            ERROR_CLASS="$2"
            shift 2
            ;;
        --phase)
            PHASE="$2"
            shift 2
            ;;
        --details)
            DETAILS="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: record-blocked-reason.sh <issue> --error-class <class> [--phase <phase>] [--details <msg>]"
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            if [[ -z "$ISSUE" ]]; then
                ISSUE="$1"
            fi
            shift
            ;;
    esac
done

if [[ -z "$ISSUE" ]]; then
    echo "Error: Issue number required" >&2
    exit 1
fi

if [[ ! -f "$DAEMON_STATE_FILE" ]]; then
    # No daemon state file - nothing to record to
    exit 0
fi

NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Update daemon-state.json with blocked reason metadata
# Initialize blocked_issue_retries if not present, and set or update the entry for this issue
temp_file=$(mktemp)
if jq --arg key "$ISSUE" \
       --arg ec "$ERROR_CLASS" \
       --arg phase "$PHASE" \
       --arg details "$DETAILS" \
       --arg ts "$NOW_ISO" '
    # Ensure blocked_issue_retries exists
    .blocked_issue_retries //= {} |
    # Update or create entry for this issue
    .blocked_issue_retries[$key] = (
        (.blocked_issue_retries[$key] // {"retry_count": 0, "last_retry_at": null, "retry_exhausted": false}) +
        {
            "error_class": $ec,
            "last_blocked_at": $ts,
            "last_blocked_phase": $phase,
            "last_blocked_details": $details
        }
    )
' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
    mv "$temp_file" "$DAEMON_STATE_FILE"
else
    rm -f "$temp_file"
    exit 1
fi

# Also record in the recent_failures array for systematic failure detection
temp_file=$(mktemp)
if jq --arg ec "$ERROR_CLASS" \
       --arg ts "$NOW_ISO" \
       --arg phase "$PHASE" \
       --argjson issue "$ISSUE" '
    # Ensure recent_failures array exists
    .recent_failures //= [] |
    # Add this failure
    .recent_failures = (.recent_failures + [{
        "issue": $issue,
        "error_class": $ec,
        "phase": $phase,
        "timestamp": $ts
    }]) |
    # Keep only last 20 failures
    .recent_failures = .recent_failures[-20:]
' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
    mv "$temp_file" "$DAEMON_STATE_FILE"
else
    rm -f "$temp_file"
fi
