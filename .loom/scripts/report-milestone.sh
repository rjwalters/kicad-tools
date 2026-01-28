#!/bin/bash

# report-milestone.sh - Report shepherd progress milestones for daemon visibility
#
# Usage:
#   report-milestone.sh <event> [options]
#
# Events:
#   started             --task-id ID --issue NUM [--mode MODE]
#   phase_entered       --task-id ID --phase PHASE
#   worktree_created    --task-id ID --path PATH
#   first_commit        --task-id ID --sha SHA
#   pr_created          --task-id ID --pr-number NUM
#   heartbeat           --task-id ID --action "description"
#   completed           --task-id ID [--pr-merged]
#   blocked             --task-id ID --reason "reason" [--details "details"]
#   error               --task-id ID --error "message" [--will-retry]
#
# Options:
#   --task-id ID        Required: Shepherd task ID (e.g., a7dc1e0)
#   --quiet             Suppress output on success
#
# Part of the Loom orchestration system for daemon visibility into shepherd progress.

set -euo pipefail

# Colors for output (disabled if stdout is not a terminal)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
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
PROGRESS_DIR="$REPO_ROOT/.loom/progress"

# Ensure progress directory exists
ensure_progress_dir() {
    if [[ ! -d "$PROGRESS_DIR" ]]; then
        mkdir -p "$PROGRESS_DIR"
    fi
}

# Show help
show_help() {
    cat <<EOF
${BLUE}report-milestone.sh - Report shepherd progress milestones${NC}

${YELLOW}USAGE:${NC}
    report-milestone.sh <event> [options]

${YELLOW}EVENTS:${NC}
    started             Shepherd started working on issue
    phase_entered       Entered a new orchestration phase
    worktree_created    Created worktree for issue
    first_commit        Made first commit in worktree
    pr_created          Created pull request
    heartbeat           Periodic heartbeat during work
    completed           Successfully completed orchestration
    blocked             Work is blocked
    error               Encountered an error

${YELLOW}OPTIONS:${NC}
    --task-id ID        Required: Shepherd task ID (e.g., a7dc1e0)
    --issue NUM         Issue number (for 'started' event)
    --mode MODE         Orchestration mode (e.g., force-pr, force-merge)
    --phase PHASE       Phase name (for 'phase_entered')
    --path PATH         Worktree path (for 'worktree_created')
    --sha SHA           Commit SHA (for 'first_commit')
    --pr-number NUM     PR number (for 'pr_created')
    --action DESC       Action description (for 'heartbeat')
    --pr-merged         Flag indicating PR was merged (for 'completed')
    --reason REASON     Reason for block (for 'blocked')
    --details DETAILS   Additional details (for 'blocked')
    --error MSG         Error message (for 'error')
    --will-retry        Flag indicating error is recoverable (for 'error')
    --quiet             Suppress output on success
    --help              Show this help

${YELLOW}EXAMPLES:${NC}
    # Report shepherd started
    report-milestone.sh started --task-id abc123 --issue 42 --mode force-pr

    # Report phase transition
    report-milestone.sh phase_entered --task-id abc123 --phase builder

    # Report heartbeat during long operation
    report-milestone.sh heartbeat --task-id abc123 --action "running tests"

    # Report completion
    report-milestone.sh completed --task-id abc123 --pr-merged

    # Report error
    report-milestone.sh error --task-id abc123 --error "build failed" --will-retry

${YELLOW}OUTPUT FILES:${NC}
    Progress files are stored in .loom/progress/:
    - shepherd-{task_id}.json: Active shepherd progress

${YELLOW}JSON STRUCTURE:${NC}
    {
      "task_id": "abc123",
      "issue": 42,
      "mode": "force-pr",
      "started_at": "2026-01-25T10:00:00Z",
      "current_phase": "builder",
      "last_heartbeat": "2026-01-25T10:15:00Z",
      "status": "working",
      "milestones": [
        {"event": "started", "timestamp": "...", "data": {...}},
        {"event": "phase_entered", "timestamp": "...", "data": {"phase": "builder"}}
      ]
    }
EOF
}

# Get current timestamp
get_timestamp() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# Atomic write to file (write to temp, then move)
atomic_write() {
    local file="$1"
    local content="$2"
    local temp_file="${file}.tmp.$$"

    echo "$content" > "$temp_file"
    mv -f "$temp_file" "$file"
}

# Initialize progress file for new shepherd
init_progress_file() {
    local task_id="$1"
    local issue="$2"
    local mode="${3:-}"
    local timestamp
    timestamp=$(get_timestamp)

    local progress_file="$PROGRESS_DIR/shepherd-${task_id}.json"

    local json
    json=$(jq -n \
        --arg task_id "$task_id" \
        --argjson issue "$issue" \
        --arg mode "$mode" \
        --arg started_at "$timestamp" \
        '{
            task_id: $task_id,
            issue: $issue,
            mode: $mode,
            started_at: $started_at,
            current_phase: "started",
            last_heartbeat: $started_at,
            status: "working",
            milestones: [{
                event: "started",
                timestamp: $started_at,
                data: {
                    issue: $issue,
                    mode: $mode
                }
            }]
        }')

    atomic_write "$progress_file" "$json"
    echo "$progress_file"
}

# Update progress file with new milestone
add_milestone() {
    local task_id="$1"
    local event="$2"
    local data="$3"
    local timestamp
    timestamp=$(get_timestamp)

    local progress_file="$PROGRESS_DIR/shepherd-${task_id}.json"

    if [[ ! -f "$progress_file" ]]; then
        echo -e "${RED}Error: No progress file found for task $task_id${NC}" >&2
        echo "Run 'report-milestone.sh started --task-id $task_id --issue N' first" >&2
        return 1
    fi

    # Read current file
    local current
    if ! current=$(cat "$progress_file" 2>/dev/null); then
        echo -e "${RED}Error: Cannot read progress file${NC}" >&2
        return 1
    fi

    # Validate it's valid JSON
    if ! echo "$current" | jq -e . >/dev/null 2>&1; then
        echo -e "${RED}Error: Progress file is corrupted${NC}" >&2
        return 1
    fi

    # Create milestone entry
    local milestone
    milestone=$(jq -n \
        --arg event "$event" \
        --arg timestamp "$timestamp" \
        --argjson data "$data" \
        '{
            event: $event,
            timestamp: $timestamp,
            data: $data
        }')

    # Update the progress file
    local updated
    updated=$(echo "$current" | jq \
        --argjson milestone "$milestone" \
        --arg timestamp "$timestamp" \
        --arg event "$event" \
        '.milestones += [$milestone] | .last_heartbeat = $timestamp |
         if $event == "phase_entered" then .current_phase = $milestone.data.phase
         elif $event == "completed" then .status = "completed"
         elif $event == "blocked" then .status = "blocked"
         elif $event == "error" then .status = (if $milestone.data.will_retry then "retrying" else "errored" end)
         else . end')

    atomic_write "$progress_file" "$updated"
}

# Parse arguments
parse_args() {
    EVENT=""
    TASK_ID=""
    ISSUE=""
    MODE=""
    PHASE=""
    PATH_ARG=""
    SHA=""
    PR_NUMBER=""
    ACTION=""
    PR_MERGED=false
    REASON=""
    DETAILS=""
    ERROR_MSG=""
    WILL_RETRY=false
    QUIET=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            started|phase_entered|worktree_created|first_commit|pr_created|heartbeat|completed|blocked|error)
                EVENT="$1"
                shift
                ;;
            --task-id)
                TASK_ID="$2"
                shift 2
                ;;
            --issue)
                ISSUE="$2"
                shift 2
                ;;
            --mode)
                MODE="$2"
                shift 2
                ;;
            --phase)
                PHASE="$2"
                shift 2
                ;;
            --path)
                PATH_ARG="$2"
                shift 2
                ;;
            --sha)
                SHA="$2"
                shift 2
                ;;
            --pr-number)
                PR_NUMBER="$2"
                shift 2
                ;;
            --action)
                ACTION="$2"
                shift 2
                ;;
            --pr-merged)
                PR_MERGED=true
                shift
                ;;
            --reason)
                REASON="$2"
                shift 2
                ;;
            --details)
                DETAILS="$2"
                shift 2
                ;;
            --error)
                ERROR_MSG="$2"
                shift 2
                ;;
            --will-retry)
                WILL_RETRY=true
                shift
                ;;
            --quiet|-q)
                QUIET=true
                shift
                ;;
            --help|-h|help)
                show_help
                exit 0
                ;;
            *)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Run 'report-milestone.sh --help' for usage" >&2
                exit 1
                ;;
        esac
    done
}

# Validate required arguments for event
validate_args() {
    if [[ -z "$EVENT" ]]; then
        echo -e "${RED}Error: Event type required${NC}" >&2
        echo "Run 'report-milestone.sh --help' for usage" >&2
        exit 1
    fi

    if [[ -z "$TASK_ID" ]]; then
        echo -e "${RED}Error: --task-id is required${NC}" >&2
        exit 1
    fi

    # Validate task ID format: must be exactly 7 lowercase hex characters
    if [[ ! "$TASK_ID" =~ ^[a-f0-9]{7}$ ]]; then
        echo -e "${RED}Error: Invalid task_id '$TASK_ID' - must be exactly 7 lowercase hex characters (e.g., a7dc1e0)${NC}" >&2
        exit 1
    fi

    case "$EVENT" in
        started)
            if [[ -z "$ISSUE" ]]; then
                echo -e "${RED}Error: --issue is required for 'started' event${NC}" >&2
                exit 1
            fi
            ;;
        phase_entered)
            if [[ -z "$PHASE" ]]; then
                echo -e "${RED}Error: --phase is required for 'phase_entered' event${NC}" >&2
                exit 1
            fi
            ;;
        worktree_created)
            if [[ -z "$PATH_ARG" ]]; then
                echo -e "${RED}Error: --path is required for 'worktree_created' event${NC}" >&2
                exit 1
            fi
            ;;
        first_commit)
            if [[ -z "$SHA" ]]; then
                echo -e "${RED}Error: --sha is required for 'first_commit' event${NC}" >&2
                exit 1
            fi
            ;;
        pr_created)
            if [[ -z "$PR_NUMBER" ]]; then
                echo -e "${RED}Error: --pr-number is required for 'pr_created' event${NC}" >&2
                exit 1
            fi
            ;;
        heartbeat)
            if [[ -z "$ACTION" ]]; then
                echo -e "${RED}Error: --action is required for 'heartbeat' event${NC}" >&2
                exit 1
            fi
            ;;
        blocked)
            if [[ -z "$REASON" ]]; then
                echo -e "${RED}Error: --reason is required for 'blocked' event${NC}" >&2
                exit 1
            fi
            ;;
        error)
            if [[ -z "$ERROR_MSG" ]]; then
                echo -e "${RED}Error: --error is required for 'error' event${NC}" >&2
                exit 1
            fi
            ;;
    esac
}

# Handle event
handle_event() {
    ensure_progress_dir

    local data="{}"

    case "$EVENT" in
        started)
            init_progress_file "$TASK_ID" "$ISSUE" "$MODE"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${GREEN}Started tracking shepherd $TASK_ID for issue #$ISSUE${NC}"
            fi
            ;;
        phase_entered)
            data=$(jq -n --arg phase "$PHASE" '{phase: $phase}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${BLUE}Phase: $PHASE${NC}"
            fi
            ;;
        worktree_created)
            data=$(jq -n --arg path "$PATH_ARG" '{path: $path}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${BLUE}Worktree created: $PATH_ARG${NC}"
            fi
            ;;
        first_commit)
            data=$(jq -n --arg sha "$SHA" '{sha: $sha}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${BLUE}First commit: $SHA${NC}"
            fi
            ;;
        pr_created)
            data=$(jq -n --argjson pr_number "$PR_NUMBER" '{pr_number: $pr_number}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${BLUE}PR created: #$PR_NUMBER${NC}"
            fi
            ;;
        heartbeat)
            data=$(jq -n --arg action "$ACTION" '{action: $action}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${BLUE}Heartbeat: $ACTION${NC}"
            fi
            ;;
        completed)
            data=$(jq -n --argjson pr_merged "$PR_MERGED" '{pr_merged: $pr_merged}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${GREEN}Completed${NC}"
            fi
            ;;
        blocked)
            data=$(jq -n --arg reason "$REASON" --arg details "$DETAILS" '{reason: $reason, details: $details}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${YELLOW}Blocked: $REASON${NC}"
            fi
            ;;
        error)
            data=$(jq -n --arg error "$ERROR_MSG" --argjson will_retry "$WILL_RETRY" '{error: $error, will_retry: $will_retry}')
            add_milestone "$TASK_ID" "$EVENT" "$data"
            if [[ "$QUIET" != "true" ]]; then
                echo -e "${RED}Error: $ERROR_MSG${NC}"
            fi
            ;;
    esac
}

# Main
main() {
    if [[ $# -eq 0 ]]; then
        show_help
        exit 0
    fi

    parse_args "$@"
    validate_args
    handle_event
}

main "$@"
