#!/bin/bash
# recover-orphaned-shepherds.sh - Detect and recover orphaned shepherd state
#
# Orphaned shepherds occur when:
# - Daemon crashes mid-session leaving task_ids that no longer exist
# - Issues have loom:building label but no active shepherd
# - Progress files exist but the shepherd task is not running
#
# Usage:
#   recover-orphaned-shepherds.sh              # Dry-run: show what would be recovered
#   recover-orphaned-shepherds.sh --recover    # Actually recover orphaned state
#   recover-orphaned-shepherds.sh --json       # Output JSON for programmatic use
#   recover-orphaned-shepherds.sh --help       # Show help
#
# Recovery actions:
#   1. Validate task_ids in daemon-state.json (mark invalid as orphaned)
#   2. Cross-reference loom:building issues with daemon-state shepherds
#   3. Check progress files for heartbeat staleness
#   4. Reset orphaned issues to loom:issue for re-pickup
#   5. Clean up stale progress files
#
# Part of Loom's orphaned shepherd detection and recovery system.

set -euo pipefail

# Colors for output (disabled if stdout is not a terminal)
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
    echo "Error: Not in a git repository" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
DAEMON_STATE_FILE="$REPO_ROOT/.loom/daemon-state.json"
PROGRESS_DIR="$REPO_ROOT/.loom/progress"

# Configuration
HEARTBEAT_STALE_THRESHOLD="${LOOM_HEARTBEAT_STALE_THRESHOLD:-300}"  # 5 minutes default

# Show help
show_help() {
    cat <<EOF
${BLUE}recover-orphaned-shepherds.sh - Detect and recover orphaned shepherd state${NC}

${YELLOW}USAGE:${NC}
    recover-orphaned-shepherds.sh              Dry-run: show what would be recovered
    recover-orphaned-shepherds.sh --recover    Actually recover orphaned state
    recover-orphaned-shepherds.sh --json       Output JSON for programmatic use
    recover-orphaned-shepherds.sh --help       Show this help

${YELLOW}DESCRIPTION:${NC}
    Detects orphaned shepherds from crashed or terminated daemon sessions.

    Orphaned shepherds occur when:
    - Daemon crashes mid-session leaving task_ids that no longer exist
    - Issues have loom:building label but no active shepherd
    - Progress files exist but the shepherd task is not running
    - Heartbeats in progress files are stale

${YELLOW}RECOVERY ACTIONS:${NC}
    1. Validate task_ids in daemon-state.json
    2. Cross-reference loom:building issues with daemon-state
    3. Check progress files for heartbeat staleness
    4. Reset orphaned issues to loom:issue
    5. Clean up stale progress files and daemon-state

${YELLOW}OPTIONS:${NC}
    --recover       Actually perform recovery (default is dry-run)
    --json          Output JSON for programmatic use
    --verbose       Show detailed progress
    --help          Show this help

${YELLOW}ENVIRONMENT VARIABLES:${NC}
    LOOM_HEARTBEAT_STALE_THRESHOLD    Seconds before heartbeat is considered stale (default: 300)
    LOOM_TASK_VERIFY_TIMEOUT          Timeout for task verification in ms (default: 5000)

${YELLOW}EXAMPLES:${NC}
    # Check for orphaned shepherds (dry run)
    recover-orphaned-shepherds.sh

    # Actually recover orphaned state
    recover-orphaned-shepherds.sh --recover

    # Get JSON output for automation
    recover-orphaned-shepherds.sh --json

${YELLOW}OUTPUT:${NC}
    Returns a summary of orphaned shepherds found and recovery actions taken.

${YELLOW}JSON OUTPUT STRUCTURE:${NC}
    {
      "orphaned": [
        {
          "type": "stale_task_id",
          "shepherd_id": "shepherd-1",
          "issue": 123,
          "task_id": "abc123",
          "reason": "task_not_found"
        },
        {
          "type": "untracked_building",
          "issue": 456,
          "reason": "no_daemon_entry"
        }
      ],
      "recovered": [...],
      "total_orphaned": 2,
      "total_recovered": 0
    }
EOF
}

# Parse arguments
RECOVER=false
JSON_OUTPUT=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --recover)
            RECOVER=true
            shift
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
            echo "Run 'recover-orphaned-shepherds.sh --help' for usage" >&2
            exit 1
            ;;
    esac
done

# Logging helpers
log_info() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${BLUE}$*${NC}"
    fi
}

log_warn() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${YELLOW}$*${NC}"
    fi
}

log_error() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${RED}$*${NC}"
    fi
}

log_success() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${GREEN}$*${NC}"
    fi
}

log_verbose() {
    if [[ "$VERBOSE" == "true" && "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${CYAN}  $*${NC}"
    fi
}

# Initialize results
ORPHANED_JSON="[]"
RECOVERED_JSON="[]"

# Add orphaned entry to results
add_orphaned() {
    local type="$1"
    local json_data="$2"

    ORPHANED_JSON=$(echo "$ORPHANED_JSON" | jq --arg type "$type" --argjson data "$json_data" \
        '. + [$data + {type: $type}]')
}

# Add recovered entry to results
add_recovered() {
    local json_data="$1"

    RECOVERED_JSON=$(echo "$RECOVERED_JSON" | jq --argjson data "$json_data" \
        '. + [$data]')
}

# Check if a task ID exists by attempting to read its output
# This is a heuristic - we check if the task output file exists
check_task_exists() {
    local task_id="$1"
    local output_file="$2"

    # If we have an output file path, check if it exists
    if [[ -n "$output_file" && -f "$output_file" ]]; then
        return 0  # Task likely exists
    fi

    # Check common task output locations
    local claude_task_dir="/tmp/claude"
    if [[ -d "$claude_task_dir" ]]; then
        # Look for task output file
        if find "$claude_task_dir" -name "*.output" -path "*$task_id*" -print -quit 2>/dev/null | grep -q .; then
            return 0
        fi
    fi

    # No evidence the task exists
    return 1
}

# Check for stale daemon-state task IDs
check_daemon_state_tasks() {
    log_info "Checking daemon-state.json for stale task IDs..."

    if [[ ! -f "$DAEMON_STATE_FILE" ]]; then
        log_verbose "No daemon-state.json found"
        return
    fi

    # Get all shepherds with task_ids
    local shepherds
    shepherds=$(jq -r '.shepherds // {} | to_entries[] | select(.value.task_id != null) | @json' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")

    if [[ -z "$shepherds" ]]; then
        log_verbose "No active shepherds with task IDs found in daemon-state"
        return
    fi

    while IFS= read -r shepherd_json; do
        [[ -z "$shepherd_json" ]] && continue

        local shepherd_id
        shepherd_id=$(echo "$shepherd_json" | jq -r '.key')
        local shepherd_data
        shepherd_data=$(echo "$shepherd_json" | jq '.value')
        local task_id
        task_id=$(echo "$shepherd_data" | jq -r '.task_id')
        local output_file
        output_file=$(echo "$shepherd_data" | jq -r '.output_file // ""')
        local issue
        issue=$(echo "$shepherd_data" | jq -r '.issue // "null"')
        local status
        status=$(echo "$shepherd_data" | jq -r '.status // "unknown"')

        log_verbose "Checking $shepherd_id: task_id=$task_id, issue=#$issue, status=$status"

        # Only check working shepherds
        if [[ "$status" != "working" ]]; then
            log_verbose "  Skipping (not working status)"
            continue
        fi

        # Check task ID format: must be exactly 7 lowercase hex characters
        if [[ ! "$task_id" =~ ^[a-f0-9]{7}$ ]]; then
            log_warn "  ORPHANED: $shepherd_id has invalid task_id format '$task_id' (expected 7 hex chars)"

            add_orphaned "invalid_task_id" "$(jq -n \
                --arg shepherd_id "$shepherd_id" \
                --arg task_id "$task_id" \
                --argjson issue "$issue" \
                --arg reason "invalid_task_id_format" \
                '{shepherd_id: $shepherd_id, task_id: $task_id, issue: $issue, reason: $reason}')"

            # Recover if requested
            if [[ "$RECOVER" == "true" ]]; then
                recover_shepherd "$shepherd_id" "$issue" "$task_id" "invalid_task_id_format"
            fi
            continue
        fi

        # Check if task exists
        if ! check_task_exists "$task_id" "$output_file"; then
            log_warn "  ORPHANED: $shepherd_id has stale task_id $task_id"

            add_orphaned "stale_task_id" "$(jq -n \
                --arg shepherd_id "$shepherd_id" \
                --arg task_id "$task_id" \
                --argjson issue "$issue" \
                --arg reason "task_not_found" \
                '{shepherd_id: $shepherd_id, task_id: $task_id, issue: $issue, reason: $reason}')"

            # Recover if requested
            if [[ "$RECOVER" == "true" ]]; then
                recover_shepherd "$shepherd_id" "$issue" "$task_id" "stale_task_id"
            fi
        else
            log_verbose "  OK: task exists"
        fi
    done <<< "$shepherds"
}

# Check for loom:building issues without daemon-state entries
check_untracked_building() {
    log_info "Checking for loom:building issues without active shepherds..."

    # Get all loom:building issues
    local building_issues
    building_issues=$(gh issue list --label "loom:building" --state open --json number,title 2>/dev/null || echo "[]")

    if [[ "$building_issues" == "[]" ]]; then
        log_verbose "No loom:building issues found"
        return
    fi

    local building_count
    building_count=$(echo "$building_issues" | jq 'length')
    log_verbose "Found $building_count issues with loom:building label"

    # Get tracked issues from daemon-state
    local tracked_issues="[]"
    if [[ -f "$DAEMON_STATE_FILE" ]]; then
        tracked_issues=$(jq '[.shepherds // {} | to_entries[] | select(.value.status == "working") | .value.issue] | map(select(. != null))' "$DAEMON_STATE_FILE" 2>/dev/null || echo "[]")
    fi

    # Find untracked building issues
    # Note: Using here-string pattern to avoid subshell (pipe creates subshell that loses variable updates)
    while IFS= read -r issue_json; do
        [[ -z "$issue_json" ]] && continue

        local issue_num
        issue_num=$(echo "$issue_json" | jq -r '.number')
        local issue_title
        issue_title=$(echo "$issue_json" | jq -r '.title')

        log_verbose "Checking issue #$issue_num"

        # Check if this issue is tracked in daemon-state
        local is_tracked
        is_tracked=$(echo "$tracked_issues" | jq --argjson num "$issue_num" 'any(. == $num)')

        if [[ "$is_tracked" == "false" ]]; then
            # Also check progress files
            local has_progress=false
            if [[ -d "$PROGRESS_DIR" ]]; then
                for progress_file in "$PROGRESS_DIR"/shepherd-*.json; do
                    if [[ -f "$progress_file" ]]; then
                        local progress_issue
                        progress_issue=$(jq -r '.issue // 0' "$progress_file" 2>/dev/null || echo "0")
                        local progress_status
                        progress_status=$(jq -r '.status // "unknown"' "$progress_file" 2>/dev/null || echo "unknown")

                        if [[ "$progress_issue" == "$issue_num" && "$progress_status" == "working" ]]; then
                            has_progress=true
                            log_verbose "  Found active progress file for issue #$issue_num"
                            break
                        fi
                    fi
                done
            fi

            if [[ "$has_progress" == "false" ]]; then
                log_warn "  ORPHANED: #$issue_num has loom:building but no active shepherd"

                add_orphaned "untracked_building" "$(jq -n \
                    --argjson issue "$issue_num" \
                    --arg title "$issue_title" \
                    --arg reason "no_daemon_entry" \
                    '{issue: $issue, title: $title, reason: $reason}')"

                # Recover if requested
                if [[ "$RECOVER" == "true" ]]; then
                    recover_issue "$issue_num" "untracked_building"
                fi
            fi
        else
            log_verbose "  OK: tracked in daemon-state"
        fi
    done <<< "$(echo "$building_issues" | jq -c '.[]')"
}

# Check progress files for stale heartbeats
check_stale_progress() {
    log_info "Checking progress files for stale heartbeats..."

    if [[ ! -d "$PROGRESS_DIR" ]]; then
        log_verbose "No progress directory found"
        return
    fi

    local now_epoch
    now_epoch=$(date +%s)

    for progress_file in "$PROGRESS_DIR"/shepherd-*.json; do
        [[ -f "$progress_file" ]] || continue

        local task_id
        task_id=$(jq -r '.task_id // "unknown"' "$progress_file" 2>/dev/null || echo "unknown")
        local issue
        issue=$(jq -r '.issue // 0' "$progress_file" 2>/dev/null || echo "0")
        local status
        status=$(jq -r '.status // "unknown"' "$progress_file" 2>/dev/null || echo "unknown")
        local last_heartbeat
        last_heartbeat=$(jq -r '.last_heartbeat // ""' "$progress_file" 2>/dev/null || echo "")

        log_verbose "Checking progress: task=$task_id, issue=#$issue, status=$status"

        # Skip non-working progress files
        if [[ "$status" != "working" ]]; then
            log_verbose "  Skipping (status: $status)"
            continue
        fi

        # Check heartbeat staleness
        if [[ -n "$last_heartbeat" && "$last_heartbeat" != "null" ]]; then
            local hb_epoch
            if [[ "$(uname)" == "Darwin" ]]; then
                hb_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_heartbeat" "+%s" 2>/dev/null || echo "0")
            else
                hb_epoch=$(date -d "$last_heartbeat" "+%s" 2>/dev/null || echo "0")
            fi

            if [[ "$hb_epoch" != "0" ]]; then
                local age_secs=$((now_epoch - hb_epoch))
                local age_mins=$((age_secs / 60))

                log_verbose "  Heartbeat age: ${age_mins}m (threshold: $((HEARTBEAT_STALE_THRESHOLD / 60))m)"

                if [[ $age_secs -gt $HEARTBEAT_STALE_THRESHOLD ]]; then
                    log_warn "  ORPHANED: task $task_id has stale heartbeat (${age_mins}m old)"

                    add_orphaned "stale_heartbeat" "$(jq -n \
                        --arg task_id "$task_id" \
                        --argjson issue "$issue" \
                        --argjson age_secs "$age_secs" \
                        --arg reason "heartbeat_stale" \
                        '{task_id: $task_id, issue: $issue, age_seconds: $age_secs, reason: $reason}')"

                    # Recover if requested
                    if [[ "$RECOVER" == "true" ]]; then
                        recover_progress_file "$progress_file" "$task_id" "$issue"
                    fi
                fi
            fi
        fi
    done
}

# Recovery functions
recover_shepherd() {
    local shepherd_id="$1"
    local issue="$2"
    local task_id="$3"
    local reason="$4"

    log_info "Recovering shepherd $shepherd_id (issue #$issue)..."

    # Mark shepherd as idle in daemon-state
    if [[ -f "$DAEMON_STATE_FILE" ]]; then
        local timestamp
        timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

        jq --arg id "$shepherd_id" --arg ts "$timestamp" --arg reason "orphan_recovery" \
            '.shepherds[$id] = {
                status: "idle",
                issue: null,
                task_id: null,
                output_file: null,
                idle_since: $ts,
                idle_reason: $reason,
                last_issue: .shepherds[$id].issue,
                last_completed: $ts
            }' "$DAEMON_STATE_FILE" > "${DAEMON_STATE_FILE}.tmp" && \
            mv "${DAEMON_STATE_FILE}.tmp" "$DAEMON_STATE_FILE"

        log_verbose "  Updated daemon-state for $shepherd_id"
    fi

    # Reset issue label if we have one
    if [[ "$issue" != "null" && "$issue" != "0" ]]; then
        recover_issue "$issue" "$reason"
    fi

    add_recovered "$(jq -n \
        --arg shepherd_id "$shepherd_id" \
        --argjson issue "$issue" \
        --arg task_id "$task_id" \
        --arg action "reset_shepherd" \
        '{shepherd_id: $shepherd_id, issue: $issue, task_id: $task_id, action: $action}')"

    log_success "  Recovered shepherd $shepherd_id"
}

recover_issue() {
    local issue="$1"
    local reason="$2"

    log_info "Recovering issue #$issue..."

    # Reset issue from loom:building to loom:issue
    gh issue edit "$issue" --remove-label "loom:building" --add-label "loom:issue" 2>/dev/null || {
        log_warn "  Failed to update labels for issue #$issue"
        return 1
    }

    # Add recovery comment
    local comment_body="## Orphan Recovery

This issue was automatically recovered from an orphaned state.

**Reason**: $reason
**What happened**:
- The daemon or shepherd that was working on this issue crashed or was terminated
- The issue was left in \`loom:building\` state with no active worker

**Action taken**:
- Removed \`loom:building\` label
- Added \`loom:issue\` label to return to ready queue

This issue is now available for a new shepherd to pick up.

---
*Recovered by recover-orphaned-shepherds.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ)*"

    gh issue comment "$issue" --body "$comment_body" 2>/dev/null || {
        log_warn "  Failed to add comment to issue #$issue"
    }

    add_recovered "$(jq -n \
        --argjson issue "$issue" \
        --arg reason "$reason" \
        --arg action "reset_issue_label" \
        '{issue: $issue, reason: $reason, action: $action}')"

    log_success "  Recovered issue #$issue"
}

recover_progress_file() {
    local progress_file="$1"
    local task_id="$2"
    local issue="$3"

    log_info "Recovering progress file for task $task_id..."

    # Mark progress file as errored due to orphan recovery
    local timestamp
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    local updated
    updated=$(jq --arg ts "$timestamp" \
        '.status = "errored" | .last_heartbeat = $ts | .milestones += [{
            event: "error",
            timestamp: $ts,
            data: {error: "orphan_recovery", will_retry: false}
        }]' "$progress_file")

    echo "$updated" > "$progress_file"

    log_verbose "  Updated progress file status to errored"

    # Also recover the issue if applicable
    if [[ "$issue" != "0" && "$issue" != "null" ]]; then
        recover_issue "$issue" "stale_heartbeat"
    fi

    add_recovered "$(jq -n \
        --arg task_id "$task_id" \
        --argjson issue "$issue" \
        --arg action "mark_progress_errored" \
        '{task_id: $task_id, issue: $issue, action: $action}')"

    log_success "  Recovered progress file for task $task_id"
}

# Main execution
main() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo ""
        echo -e "${CYAN}========================================${NC}"
        echo -e "${CYAN}  Orphaned Shepherd Detection & Recovery${NC}"
        echo -e "${CYAN}========================================${NC}"
        echo ""

        if [[ "$RECOVER" != "true" ]]; then
            log_info "DRY RUN - No changes will be made"
            log_info "Use --recover to actually perform recovery"
            echo ""
        fi
    fi

    # Run all checks
    check_daemon_state_tasks
    check_untracked_building
    check_stale_progress

    # Output results
    local total_orphaned
    total_orphaned=$(echo "$ORPHANED_JSON" | jq 'length')
    local total_recovered
    total_recovered=$(echo "$RECOVERED_JSON" | jq 'length')

    if [[ "$JSON_OUTPUT" == "true" ]]; then
        jq -n \
            --argjson orphaned "$ORPHANED_JSON" \
            --argjson recovered "$RECOVERED_JSON" \
            --argjson total_orphaned "$total_orphaned" \
            --argjson total_recovered "$total_recovered" \
            --argjson recover_mode "$RECOVER" \
            '{
                orphaned: $orphaned,
                recovered: $recovered,
                total_orphaned: $total_orphaned,
                total_recovered: $total_recovered,
                recover_mode: $recover_mode
            }'
    else
        echo ""
        echo -e "${CYAN}========================================${NC}"
        echo -e "${CYAN}  Summary${NC}"
        echo -e "${CYAN}========================================${NC}"
        echo ""

        if [[ $total_orphaned -eq 0 ]]; then
            log_success "No orphaned shepherds found"
        else
            log_warn "Found $total_orphaned orphaned shepherd(s)"

            if [[ "$RECOVER" == "true" ]]; then
                log_success "Recovered $total_recovered item(s)"
            else
                echo ""
                log_info "Run with --recover to fix these issues"
            fi
        fi
    fi

    # Return non-zero if orphaned and not recovered
    if [[ $total_orphaned -gt 0 && "$RECOVER" != "true" ]]; then
        exit 1
    fi
}

main
