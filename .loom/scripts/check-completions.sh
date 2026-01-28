#!/usr/bin/env bash
# check-completions.sh - Check task completions and detect silent failures
#
# This script polls all active task IDs from daemon-state.json and checks if
# their output files indicate completion. It detects silent failures where
# tasks have exited but issues are still in loom:building state.
#
# Usage:
#   ./check-completions.sh                # Check all active tasks
#   ./check-completions.sh --json         # Output JSON for programmatic use
#   ./check-completions.sh --recover      # Auto-recover silently failed tasks
#   ./check-completions.sh --verbose      # Show detailed progress
#
# Exit codes:
#   0 - All tasks healthy (or recovered with --recover)
#   1 - Silent failures detected
#   2 - State file not found
#
# Checks:
#   - Shepherd task output files for completion/error markers
#   - Support role task output files
#   - Progress files for heartbeat staleness
#   - GitHub label state vs daemon state consistency
#
# Detects:
#   - completed: Task completed successfully
#   - errored: Task exited with error
#   - stale: No heartbeat for extended period
#   - orphaned: Issue in loom:building but no active task
#   - missing_output: Output file doesn't exist

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Staleness thresholds (in seconds)
HEARTBEAT_STALE_THRESHOLD="${LOOM_HEARTBEAT_STALE_THRESHOLD:-300}"  # 5 minutes
OUTPUT_STALE_THRESHOLD="${LOOM_OUTPUT_STALE_THRESHOLD:-600}"        # 10 minutes

# Find the repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
STATE_FILE="$REPO_ROOT/.loom/daemon-state.json"
PROGRESS_DIR="$REPO_ROOT/.loom/progress"

# Parse arguments
JSON_OUTPUT=false
RECOVER=false
VERBOSE=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --recover)
            RECOVER=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            cat <<EOF
Usage: $0 [OPTIONS]

Check task completions and detect silent failures.

Options:
  --json        Output JSON for programmatic use
  --recover     Auto-recover silently failed tasks (revert labels)
  --verbose     Show detailed progress
  --dry-run     Show what would be recovered without making changes
  --help        Show this help message

Exit codes:
  0 - All tasks healthy (or recovered with --recover)
  1 - Silent failures detected
  2 - State file not found

Environment variables:
  LOOM_HEARTBEAT_STALE_THRESHOLD   Seconds before heartbeat is stale (default: 300)
  LOOM_OUTPUT_STALE_THRESHOLD      Seconds before output is stale (default: 600)

Task states detected:
  completed        Task completed successfully
  errored          Task exited with error
  stale            No heartbeat for extended period
  orphaned         Issue in loom:building but no active task
  missing_output   Output file doesn't exist
  running          Task is still active

Examples:
  $0                     # Check all tasks
  $0 --json              # Machine-readable output
  $0 --recover --verbose # Recover and show details
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Logging helpers
log_info() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${BLUE}[INFO]${NC} $*" >&2
    fi
}

log_verbose() {
    if [[ "$VERBOSE" == "true" && "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${BLUE}[DEBUG]${NC} $*" >&2
    fi
}

log_success() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${GREEN}[OK]${NC} $*" >&2
    fi
}

log_warn() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${YELLOW}[WARN]${NC} $*" >&2
    fi
}

log_error() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo -e "${RED}[ERROR]${NC} $*" >&2
    fi
}

# Check if state file exists
if [[ ! -f "$STATE_FILE" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "state_file_not_found", "file": "'"$STATE_FILE"'"}'
    else
        log_error "State file not found: $STATE_FILE"
    fi
    exit 2
fi

# Load state
STATE=$(cat "$STATE_FILE")

# Results tracking
declare -a COMPLETED=()
declare -a ERRORED=()
declare -a STALE=()
declare -a ORPHANED=()
declare -a RUNNING=()
declare -a RECOVERIES=()

# Get current timestamp in seconds since epoch
get_epoch() {
    if [[ "$(uname)" == "Darwin" ]]; then
        date +%s
    else
        date +%s
    fi
}

# Convert ISO timestamp to epoch
iso_to_epoch() {
    local iso="$1"
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS date
        date -j -f "%Y-%m-%dT%H:%M:%SZ" "$iso" "+%s" 2>/dev/null || echo "0"
    else
        # GNU date
        date -d "$iso" "+%s" 2>/dev/null || echo "0"
    fi
}

NOW_EPOCH=$(get_epoch)

# Check shepherd tasks
log_info "Checking shepherd tasks..."

SHEPHERDS=$(echo "$STATE" | jq -c '.shepherds // {} | to_entries[]' 2>/dev/null || true)

while IFS= read -r shepherd_entry; do
    [[ -z "$shepherd_entry" ]] && continue

    shepherd_id=$(echo "$shepherd_entry" | jq -r '.key')
    shepherd_data=$(echo "$shepherd_entry" | jq -r '.value')
    status=$(echo "$shepherd_data" | jq -r '.status // "unknown"')
    issue=$(echo "$shepherd_data" | jq -r '.issue // empty')
    task_id=$(echo "$shepherd_data" | jq -r '.task_id // empty')
    output_file=$(echo "$shepherd_data" | jq -r '.output_file // empty')
    execution_mode=$(echo "$shepherd_data" | jq -r '.execution_mode // "direct"')

    log_verbose "Checking shepherd $shepherd_id: status=$status, issue=$issue, task_id=$task_id"

    # Skip idle shepherds
    if [[ "$status" == "idle" ]]; then
        log_verbose "  Skipping (idle)"
        continue
    fi

    # For working shepherds, check task status
    if [[ "$status" == "working" ]]; then
        # Check progress file for heartbeat
        if [[ -n "$task_id" && -d "$PROGRESS_DIR" ]]; then
            progress_file="$PROGRESS_DIR/shepherd-${task_id}.json"
            if [[ -f "$progress_file" ]]; then
                last_heartbeat=$(jq -r '.last_heartbeat // empty' "$progress_file" 2>/dev/null || true)
                if [[ -n "$last_heartbeat" ]]; then
                    heartbeat_epoch=$(iso_to_epoch "$last_heartbeat")
                    heartbeat_age=$((NOW_EPOCH - heartbeat_epoch))
                    log_verbose "  Heartbeat age: ${heartbeat_age}s"

                    if [[ $heartbeat_age -gt $HEARTBEAT_STALE_THRESHOLD ]]; then
                        STALE+=("$shepherd_id:$issue:$task_id:heartbeat_stale:${heartbeat_age}s")
                        log_warn "Shepherd $shepherd_id has stale heartbeat (${heartbeat_age}s)"
                        continue
                    fi
                fi

                # Check progress file status
                progress_status=$(jq -r '.status // "working"' "$progress_file" 2>/dev/null || echo "working")
                if [[ "$progress_status" == "completed" ]]; then
                    COMPLETED+=("$shepherd_id:$issue:$task_id")
                    log_success "Shepherd $shepherd_id completed (issue #$issue)"
                    continue
                elif [[ "$progress_status" == "error" ]]; then
                    ERRORED+=("$shepherd_id:$issue:$task_id:progress_error")
                    log_error "Shepherd $shepherd_id errored (issue #$issue)"
                    continue
                fi
            fi
        fi

        # Check output file for direct mode tasks
        if [[ "$execution_mode" == "direct" && -n "$output_file" ]]; then
            if [[ ! -f "$output_file" ]]; then
                # Output file missing - task may have never started or crashed
                ERRORED+=("$shepherd_id:$issue:$task_id:missing_output")
                log_warn "Shepherd $shepherd_id output file missing: $output_file"
                continue
            fi

            # Check output file modification time
            if [[ "$(uname)" == "Darwin" ]]; then
                output_mtime=$(stat -f %m "$output_file" 2>/dev/null || echo "0")
            else
                output_mtime=$(stat -c %Y "$output_file" 2>/dev/null || echo "0")
            fi
            output_age=$((NOW_EPOCH - output_mtime))

            if [[ $output_age -gt $OUTPUT_STALE_THRESHOLD ]]; then
                STALE+=("$shepherd_id:$issue:$task_id:output_stale:${output_age}s")
                log_warn "Shepherd $shepherd_id has stale output (${output_age}s)"
                continue
            fi

            # Check output for completion/error markers
            if grep -q "AGENT_EXIT_CODE=0" "$output_file" 2>/dev/null; then
                COMPLETED+=("$shepherd_id:$issue:$task_id")
                log_success "Shepherd $shepherd_id completed (issue #$issue)"
                continue
            elif grep -q "AGENT_EXIT_CODE=" "$output_file" 2>/dev/null; then
                ERRORED+=("$shepherd_id:$issue:$task_id:exit_error")
                log_error "Shepherd $shepherd_id exited with error (issue #$issue)"
                continue
            fi
        fi

        # Still running
        RUNNING+=("$shepherd_id:$issue:$task_id")
        log_verbose "  Still running"
    fi
done < <(echo "$SHEPHERDS")

# Check support role tasks
log_info "Checking support role tasks..."

SUPPORT_ROLES=$(echo "$STATE" | jq -c '.support_roles // {} | to_entries[]' 2>/dev/null || true)

while IFS= read -r role_entry; do
    [[ -z "$role_entry" ]] && continue

    role_name=$(echo "$role_entry" | jq -r '.key')
    role_data=$(echo "$role_entry" | jq -r '.value')
    status=$(echo "$role_data" | jq -r '.status // "unknown"')
    task_id=$(echo "$role_data" | jq -r '.task_id // empty')
    output_file=$(echo "$role_data" | jq -r '.output_file // empty')

    log_verbose "Checking support role $role_name: status=$status, task_id=$task_id"

    # Skip idle roles
    if [[ "$status" == "idle" ]]; then
        log_verbose "  Skipping (idle)"
        continue
    fi

    # For running roles, check task status
    if [[ "$status" == "running" && -n "$output_file" ]]; then
        if [[ ! -f "$output_file" ]]; then
            ERRORED+=("support:$role_name:$task_id:missing_output")
            log_warn "Support role $role_name output file missing"
            continue
        fi

        # Check output file modification time
        if [[ "$(uname)" == "Darwin" ]]; then
            output_mtime=$(stat -f %m "$output_file" 2>/dev/null || echo "0")
        else
            output_mtime=$(stat -c %Y "$output_file" 2>/dev/null || echo "0")
        fi
        output_age=$((NOW_EPOCH - output_mtime))

        if [[ $output_age -gt $OUTPUT_STALE_THRESHOLD ]]; then
            STALE+=("support:$role_name:$task_id:output_stale:${output_age}s")
            log_warn "Support role $role_name has stale output (${output_age}s)"
            continue
        fi

        # Check output for completion/error markers
        if grep -q "AGENT_EXIT_CODE=0" "$output_file" 2>/dev/null; then
            COMPLETED+=("support:$role_name:$task_id")
            log_success "Support role $role_name completed"
        elif grep -q "AGENT_EXIT_CODE=" "$output_file" 2>/dev/null; then
            ERRORED+=("support:$role_name:$task_id:exit_error")
            log_error "Support role $role_name exited with error"
        else
            RUNNING+=("support:$role_name:$task_id")
            log_verbose "  Still running"
        fi
    fi
done < <(echo "$SUPPORT_ROLES")

# Check for orphaned issues (loom:building but no active shepherd)
log_info "Checking for orphaned issues..."

BUILDING_ISSUES=$(gh issue list --label "loom:building" --state open --json number --jq '.[].number' 2>/dev/null || true)

for issue_num in $BUILDING_ISSUES; do
    # Check if this issue is tracked by any shepherd
    tracked=false
    while IFS= read -r shepherd_entry; do
        [[ -z "$shepherd_entry" ]] && continue
        shepherd_issue=$(echo "$shepherd_entry" | jq -r '.value.issue // empty')
        if [[ "$shepherd_issue" == "$issue_num" ]]; then
            tracked=true
            break
        fi
    done < <(echo "$STATE" | jq -c '.shepherds // {} | to_entries[]' 2>/dev/null || true)

    if [[ "$tracked" == "false" ]]; then
        ORPHANED+=("issue:$issue_num")
        log_warn "Issue #$issue_num is in loom:building but not tracked by any shepherd"
    fi
done

# Recovery actions
if [[ "$RECOVER" == "true" ]]; then
    log_info "Performing recovery actions..."

    # Recover errored shepherds
    for error_entry in "${ERRORED[@]}"; do
        shepherd_part=$(echo "$error_entry" | cut -d: -f1)
        issue=$(echo "$error_entry" | cut -d: -f2)

        # Only recover shepherd issues (not support roles)
        if [[ "$shepherd_part" != "support" && -n "$issue" ]]; then
            if [[ "$DRY_RUN" == "true" ]]; then
                log_info "  Would revert issue #$issue from loom:building to loom:issue"
            else
                if gh issue edit "$issue" --remove-label "loom:building" --add-label "loom:issue" 2>/dev/null; then
                    log_success "  Reverted issue #$issue to loom:issue"
                    RECOVERIES+=("revert:$issue")

                    # Add recovery comment
                    gh issue comment "$issue" --body "**Silent Failure Recovery**

This issue was automatically recovered after a silent task failure.

**What happened**:
- The shepherd task exited without completing
- The issue was left in \`loom:building\` state

**Action taken**:
- Returned to \`loom:issue\` state for re-processing

---
*Recovered by check-completions.sh at $(date -u +%Y-%m-%dT%H:%M:%SZ)*" 2>/dev/null || true
                else
                    log_error "  Failed to revert issue #$issue"
                fi
            fi
        fi
    done

    # Recover orphaned issues
    for orphan_entry in "${ORPHANED[@]}"; do
        issue=$(echo "$orphan_entry" | cut -d: -f2)
        if [[ "$DRY_RUN" == "true" ]]; then
            log_info "  Would revert orphaned issue #$issue from loom:building to loom:issue"
        else
            if gh issue edit "$issue" --remove-label "loom:building" --add-label "loom:issue" 2>/dev/null; then
                log_success "  Reverted orphaned issue #$issue to loom:issue"
                RECOVERIES+=("revert_orphan:$issue")
            else
                log_error "  Failed to revert orphaned issue #$issue"
            fi
        fi
    done
fi

# Generate output
if [[ "$JSON_OUTPUT" == "true" ]]; then
    # Build arrays
    completed_json=$(printf '%s\n' "${COMPLETED[@]}" 2>/dev/null | jq -R . | jq -s . || echo "[]")
    errored_json=$(printf '%s\n' "${ERRORED[@]}" 2>/dev/null | jq -R . | jq -s . || echo "[]")
    stale_json=$(printf '%s\n' "${STALE[@]}" 2>/dev/null | jq -R . | jq -s . || echo "[]")
    orphaned_json=$(printf '%s\n' "${ORPHANED[@]}" 2>/dev/null | jq -R . | jq -s . || echo "[]")
    running_json=$(printf '%s\n' "${RUNNING[@]}" 2>/dev/null | jq -R . | jq -s . || echo "[]")
    recoveries_json=$(printf '%s\n' "${RECOVERIES[@]}" 2>/dev/null | jq -R . | jq -s . || echo "[]")

    has_failures="false"
    if [[ ${#ERRORED[@]} -gt 0 || ${#ORPHANED[@]} -gt 0 ]]; then
        has_failures="true"
    fi

    jq -n \
        --argjson completed "$completed_json" \
        --argjson errored "$errored_json" \
        --argjson stale "$stale_json" \
        --argjson orphaned "$orphaned_json" \
        --argjson running "$running_json" \
        --argjson recoveries "$recoveries_json" \
        --argjson has_failures "$has_failures" \
        '{
            completed: $completed,
            errored: $errored,
            stale: $stale,
            orphaned: $orphaned,
            running: $running,
            recoveries: $recoveries,
            summary: {
                completed_count: ($completed | length),
                errored_count: ($errored | length),
                stale_count: ($stale | length),
                orphaned_count: ($orphaned | length),
                running_count: ($running | length),
                recovery_count: ($recoveries | length),
                has_failures: $has_failures
            }
        }'
else
    # Human-readable summary
    echo ""
    log_info "=== Task Completion Summary ==="
    echo "  Running:   ${#RUNNING[@]}"
    echo "  Completed: ${#COMPLETED[@]}"
    echo "  Errored:   ${#ERRORED[@]}"
    echo "  Stale:     ${#STALE[@]}"
    echo "  Orphaned:  ${#ORPHANED[@]}"
    if [[ ${#RECOVERIES[@]} -gt 0 ]]; then
        echo "  Recovered: ${#RECOVERIES[@]}"
    fi
fi

# Exit code
if [[ ${#ERRORED[@]} -gt 0 || ${#ORPHANED[@]} -gt 0 ]]; then
    if [[ "$RECOVER" == "true" && ${#RECOVERIES[@]} -gt 0 ]]; then
        exit 0  # Issues detected but recovered
    fi
    exit 1  # Silent failures detected
fi

exit 0
