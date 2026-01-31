#!/bin/bash
# retry-blocked-issues.sh - Retry blocked issues with exponential backoff
#
# Usage:
#   retry-blocked-issues.sh                  # Retry eligible blocked issues (dry run)
#   retry-blocked-issues.sh --execute        # Actually perform retries
#   retry-blocked-issues.sh --json           # Output as JSON
#   retry-blocked-issues.sh --help           # Show help
#
# This script handles automatic retry of blocked issues with exponential backoff.
# It reads retry metadata from daemon-state.json and determines which blocked issues
# are eligible for retry based on cooldown timing.
#
# Retry behavior:
#   - Initial cooldown: 30 minutes (configurable via LOOM_RETRY_COOLDOWN)
#   - Backoff multiplier: 2x (configurable via LOOM_RETRY_BACKOFF_MULTIPLIER)
#   - Max cooldown: 4 hours (configurable via LOOM_RETRY_MAX_COOLDOWN)
#   - Max retries: 3 (configurable via LOOM_MAX_RETRY_COUNT)
#   - After max retries, issue stays loom:blocked permanently
#
# When retrying an issue:
#   1. Remove loom:blocked label
#   2. Add loom:issue label
#   3. Increment retry_count in daemon-state.json
#   4. Add issue comment with retry information

set -euo pipefail

# Configuration
MAX_RETRY_COUNT="${LOOM_MAX_RETRY_COUNT:-3}"
RETRY_COOLDOWN="${LOOM_RETRY_COOLDOWN:-1800}"            # 30 minutes
RETRY_BACKOFF_MULTIPLIER="${LOOM_RETRY_BACKOFF_MULTIPLIER:-2}"
RETRY_MAX_COOLDOWN="${LOOM_RETRY_MAX_COOLDOWN:-14400}"   # 4 hours

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
EXECUTE=false
JSON_OUTPUT=false

show_help() {
    cat <<EOF
retry-blocked-issues.sh - Retry blocked issues with exponential backoff

USAGE:
    retry-blocked-issues.sh                  Dry run - show eligible issues
    retry-blocked-issues.sh --execute        Retry eligible issues
    retry-blocked-issues.sh --json           JSON output
    retry-blocked-issues.sh --help           Show help

ENVIRONMENT:
    LOOM_MAX_RETRY_COUNT              Max retries (default: 3)
    LOOM_RETRY_COOLDOWN               Initial cooldown seconds (default: 1800)
    LOOM_RETRY_BACKOFF_MULTIPLIER     Backoff multiplier (default: 2)
    LOOM_RETRY_MAX_COOLDOWN           Max cooldown seconds (default: 14400)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute)
            EXECUTE=true
            shift
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Get blocked issues
BLOCKED_ISSUES=$(gh issue list --label "loom:blocked" --state open --json number,title,createdAt 2>/dev/null || echo "[]")
BLOCKED_COUNT=$(echo "$BLOCKED_ISSUES" | jq 'length')

if [[ "$BLOCKED_COUNT" -eq 0 ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"blocked_count": 0, "retryable": [], "exhausted": [], "retried": []}'
    else
        echo "No blocked issues found"
    fi
    exit 0
fi

# Read existing retry metadata from daemon-state.json
RETRY_DATA="{}"
if [[ -f "$DAEMON_STATE_FILE" ]]; then
    RETRY_DATA=$(jq -r '.blocked_issue_retries // {}' "$DAEMON_STATE_FILE" 2>/dev/null || echo "{}")
fi

NOW_EPOCH=$(date +%s)
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

RETRYABLE="[]"
EXHAUSTED="[]"
COOLING_DOWN="[]"
RETRIED="[]"

for blocked_num in $(echo "$BLOCKED_ISSUES" | jq -r '.[].number'); do
    [[ -z "$blocked_num" ]] && continue

    issue_key="$blocked_num"
    retry_info=$(echo "$RETRY_DATA" | jq --arg key "$issue_key" '.[$key] // {"retry_count": 0, "last_retry_at": null, "error_class": null, "retry_exhausted": false}')
    retry_count=$(echo "$retry_info" | jq -r '.retry_count // 0')
    retry_exhausted=$(echo "$retry_info" | jq -r '.retry_exhausted // false')
    error_class=$(echo "$retry_info" | jq -r '.error_class // "unknown"')

    # Check if retries are exhausted
    if [[ "$retry_exhausted" == "true" ]] || [[ "$retry_count" -ge "$MAX_RETRY_COUNT" ]]; then
        EXHAUSTED=$(echo "$EXHAUSTED" | jq --argjson num "$blocked_num" --argjson rc "$retry_count" --arg ec "$error_class" \
            '. + [{"number": $num, "retry_count": $rc, "error_class": $ec, "reason": "exhausted"}]')
        continue
    fi

    # Check cooldown
    last_retry=$(echo "$retry_info" | jq -r '.last_retry_at // ""')
    cooldown_elapsed="true"

    if [[ -n "$last_retry" && "$last_retry" != "null" ]]; then
        if [[ "$(uname)" == "Darwin" ]]; then
            last_retry_epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_retry" "+%s" 2>/dev/null || echo "0")
        else
            last_retry_epoch=$(date -d "$last_retry" "+%s" 2>/dev/null || echo "0")
        fi

        if [[ "$last_retry_epoch" != "0" ]]; then
            # Calculate effective cooldown with exponential backoff
            effective_cooldown=$RETRY_COOLDOWN
            for ((i=0; i<retry_count; i++)); do
                effective_cooldown=$((effective_cooldown * RETRY_BACKOFF_MULTIPLIER))
            done
            if [[ $effective_cooldown -gt $RETRY_MAX_COOLDOWN ]]; then
                effective_cooldown=$RETRY_MAX_COOLDOWN
            fi

            elapsed=$((NOW_EPOCH - last_retry_epoch))
            if [[ $elapsed -lt $effective_cooldown ]]; then
                cooldown_elapsed="false"
                remaining=$((effective_cooldown - elapsed))
                COOLING_DOWN=$(echo "$COOLING_DOWN" | jq --argjson num "$blocked_num" --argjson rc "$retry_count" \
                    --argjson remaining "$remaining" --argjson cooldown "$effective_cooldown" \
                    '. + [{"number": $num, "retry_count": $rc, "cooldown_remaining_seconds": $remaining, "total_cooldown_seconds": $cooldown}]')
            fi
        fi
    fi

    if [[ "$cooldown_elapsed" == "true" ]]; then
        RETRYABLE=$(echo "$RETRYABLE" | jq --argjson num "$blocked_num" --argjson rc "$retry_count" --arg ec "$error_class" \
            '. + [{"number": $num, "retry_count": $rc, "error_class": $ec}]')
    fi
done

# Execute retries if requested
if [[ "$EXECUTE" == "true" ]]; then
    for entry in $(echo "$RETRYABLE" | jq -c '.[]'); do
        issue_num=$(echo "$entry" | jq -r '.number')
        retry_count=$(echo "$entry" | jq -r '.retry_count')
        error_class=$(echo "$entry" | jq -r '.error_class')
        new_retry_count=$((retry_count + 1))

        # Calculate next cooldown for the comment
        next_cooldown=$RETRY_COOLDOWN
        for ((i=0; i<new_retry_count; i++)); do
            next_cooldown=$((next_cooldown * RETRY_BACKOFF_MULTIPLIER))
        done
        if [[ $next_cooldown -gt $RETRY_MAX_COOLDOWN ]]; then
            next_cooldown=$RETRY_MAX_COOLDOWN
        fi
        next_cooldown_min=$((next_cooldown / 60))

        # Transition labels: loom:blocked -> loom:issue
        gh issue edit "$issue_num" --remove-label "loom:blocked" --add-label "loom:issue" >/dev/null 2>&1

        # Add comment
        gh issue comment "$issue_num" --body "**[daemon] Retry attempt $new_retry_count/$MAX_RETRY_COUNT**

Previous failure: \`$error_class\`

This issue has been automatically unblocked for retry. If it fails again, the next retry will be in ${next_cooldown_min} minutes.

$(if [[ $new_retry_count -ge $MAX_RETRY_COUNT ]]; then echo "**Warning**: This is the final retry attempt. If this fails, the issue will remain blocked permanently."; fi)

---
*Automated by Loom daemon retry system*" >/dev/null 2>&1

        # Update retry metadata in daemon-state.json
        if [[ -f "$DAEMON_STATE_FILE" ]]; then
            local_retry_exhausted="false"
            if [[ $new_retry_count -ge $MAX_RETRY_COUNT ]]; then
                local_retry_exhausted="true"
            fi

            temp_file=$(mktemp)
            if jq --arg key "$issue_num" \
                   --argjson count "$new_retry_count" \
                   --arg ts "$NOW_ISO" \
                   --arg ec "$error_class" \
                   --argjson exhausted "$local_retry_exhausted" '
                .blocked_issue_retries[$key] = {
                    retry_count: $count,
                    last_retry_at: $ts,
                    error_class: $ec,
                    retry_exhausted: $exhausted
                }
            ' "$DAEMON_STATE_FILE" > "$temp_file" 2>/dev/null; then
                mv "$temp_file" "$DAEMON_STATE_FILE"
            else
                rm -f "$temp_file"
            fi
        fi

        RETRIED=$(echo "$RETRIED" | jq --argjson num "$issue_num" --argjson rc "$new_retry_count" \
            '. + [{"number": $num, "retry_count": $rc}]')
    done
fi

# Output results
RETRYABLE_COUNT=$(echo "$RETRYABLE" | jq 'length')
EXHAUSTED_COUNT=$(echo "$EXHAUSTED" | jq 'length')
RETRIED_COUNT=$(echo "$RETRIED" | jq 'length')

if [[ "$JSON_OUTPUT" == "true" ]]; then
    jq -n \
        --argjson blocked_count "$BLOCKED_COUNT" \
        --argjson retryable "$RETRYABLE" \
        --argjson retryable_count "$RETRYABLE_COUNT" \
        --argjson exhausted "$EXHAUSTED" \
        --argjson exhausted_count "$EXHAUSTED_COUNT" \
        --argjson cooling_down "$COOLING_DOWN" \
        --argjson retried "$RETRIED" \
        --argjson retried_count "$RETRIED_COUNT" \
        '{
            blocked_count: $blocked_count,
            retryable: $retryable,
            retryable_count: $retryable_count,
            exhausted: $exhausted,
            exhausted_count: $exhausted_count,
            cooling_down: $cooling_down,
            retried: $retried,
            retried_count: $retried_count
        }'
else
    echo "Blocked issues: $BLOCKED_COUNT"
    echo "  Retryable: $RETRYABLE_COUNT"
    echo "  Exhausted: $EXHAUSTED_COUNT"
    if [[ "$EXECUTE" == "true" ]]; then
        echo "  Retried: $RETRIED_COUNT"
    fi
fi
