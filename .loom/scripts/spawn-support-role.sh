#!/bin/bash
# spawn-support-role.sh - Deterministic support role spawn decision logic
#
# This script encapsulates the decision logic for spawning support roles
# (Guide, Champion, Doctor, Auditor, Judge, Architect, Hermit) in the daemon.
# It replaces LLM-interpreted pseudocode with deterministic bash logic.
#
# This script is a PURE DECISION FUNCTION - it is read-only and does NOT
# modify daemon-state.json. All state management (marking roles as running
# or completed) is handled by the iteration subagent, which is the sole
# writer of daemon-state.json. This eliminates race conditions between
# concurrent writers.
#
# Usage:
#   spawn-support-role.sh <role> [--demand] [--check-only] [--json]
#   spawn-support-role.sh --check-all [--json]
#   spawn-support-role.sh --help
#
# Options:
#   <role>            Role name: guide, champion, doctor, auditor, judge, architect, hermit
#   --demand          Demand-based spawn (skip interval check)
#   --check-only      Only check if spawn is needed, don't modify state
#   --check-all       Check all support roles and output combined result
#   --json            Output JSON instead of human-readable text
#   --help            Show this help message
#
# Exit Codes:
#   0 - Role should be spawned
#   1 - Role should NOT be spawned (already running, interval not elapsed)
#   2 - Error (invalid role, missing state file, etc.)
#
# Environment Variables:
#   LOOM_GUIDE_INTERVAL      Guide re-trigger interval in seconds (default: 900)
#   LOOM_CHAMPION_INTERVAL   Champion re-trigger interval in seconds (default: 600)
#   LOOM_DOCTOR_INTERVAL     Doctor re-trigger interval in seconds (default: 300)
#   LOOM_AUDITOR_INTERVAL    Auditor re-trigger interval in seconds (default: 600)
#   LOOM_JUDGE_INTERVAL      Judge re-trigger interval in seconds (default: 300)
#   LOOM_ARCHITECT_COOLDOWN  Architect re-trigger interval in seconds (default: 1800)
#   LOOM_HERMIT_COOLDOWN     Hermit re-trigger interval in seconds (default: 1800)
#
# Examples:
#   # Check if Guide should be spawned (interval-based)
#   spawn-support-role.sh guide
#
#   # Check if Champion should be spawned on-demand
#   spawn-support-role.sh champion --demand
#
#   # Check all roles and get JSON output
#   spawn-support-role.sh --check-all --json

set -euo pipefail

# Find the repository root (works from any subdirectory)
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

# Default intervals (in seconds)
GUIDE_INTERVAL="${LOOM_GUIDE_INTERVAL:-900}"        # 15 minutes
CHAMPION_INTERVAL="${LOOM_CHAMPION_INTERVAL:-600}"  # 10 minutes
DOCTOR_INTERVAL="${LOOM_DOCTOR_INTERVAL:-300}"      # 5 minutes
AUDITOR_INTERVAL="${LOOM_AUDITOR_INTERVAL:-600}"    # 10 minutes
JUDGE_INTERVAL="${LOOM_JUDGE_INTERVAL:-300}"        # 5 minutes
ARCHITECT_INTERVAL="${LOOM_ARCHITECT_COOLDOWN:-1800}"  # 30 minutes
HERMIT_INTERVAL="${LOOM_HERMIT_COOLDOWN:-1800}"        # 30 minutes

# Valid roles
VALID_ROLES=("guide" "champion" "doctor" "auditor" "judge" "architect" "hermit")

show_help() {
    cat <<'EOF'
spawn-support-role.sh - Deterministic support role spawn decision logic

This script is a PURE DECISION FUNCTION - it is read-only and does NOT
modify daemon-state.json. State management is handled by the iteration
subagent (the sole writer of daemon-state.json).

USAGE:
    spawn-support-role.sh <role> [--demand] [--check-only] [--json]
    spawn-support-role.sh --check-all [--json]
    spawn-support-role.sh --help

ROLES:
    guide       Backlog triage (interval: 15 min)
    champion    PR merging and proposal evaluation (interval: 10 min)
    doctor      PR conflict resolution (interval: 5 min)
    auditor     Main branch validation (interval: 10 min)
    judge       PR review (interval: 5 min)
    architect   Work generation / architectural proposals (cooldown: 30 min)
    hermit      Simplification proposals (cooldown: 30 min)

OPTIONS:
    --demand        Demand-based spawn (skip interval check)
    --check-only    Only check if spawn is needed, don't modify state
    --check-all     Check all roles and output combined result
    --json          Output JSON format
    --help          Show this help message

EXIT CODES:
    0 - Role should be spawned
    1 - Role should NOT be spawned
    2 - Error

ENVIRONMENT VARIABLES:
    LOOM_GUIDE_INTERVAL      (default: 900)
    LOOM_CHAMPION_INTERVAL   (default: 600)
    LOOM_DOCTOR_INTERVAL     (default: 300)
    LOOM_AUDITOR_INTERVAL    (default: 600)
    LOOM_JUDGE_INTERVAL      (default: 300)
    LOOM_ARCHITECT_COOLDOWN  (default: 1800)
    LOOM_HERMIT_COOLDOWN     (default: 1800)
EOF
}

# Get the interval for a role
get_interval() {
    local role="$1"
    case "$role" in
        guide)     echo "$GUIDE_INTERVAL" ;;
        champion)  echo "$CHAMPION_INTERVAL" ;;
        doctor)    echo "$DOCTOR_INTERVAL" ;;
        auditor)   echo "$AUDITOR_INTERVAL" ;;
        judge)     echo "$JUDGE_INTERVAL" ;;
        architect) echo "$ARCHITECT_INTERVAL" ;;
        hermit)    echo "$HERMIT_INTERVAL" ;;
        *)         echo "0" ;;
    esac
}

# Validate role name
validate_role() {
    local role="$1"
    for valid in "${VALID_ROLES[@]}"; do
        if [[ "$role" == "$valid" ]]; then
            return 0
        fi
    done
    return 1
}

# Validate task_id format (7-char hex)
validate_task_id() {
    local task_id="$1"
    if [[ -z "$task_id" ]]; then
        return 1
    fi
    # Real Task tool IDs are 7-char lowercase hex strings
    if [[ "$task_id" =~ ^[a-f0-9]{7}$ ]]; then
        return 0
    fi
    return 1
}

# Check if a tmux session exists and is running
# Returns 0 if session exists, 1 otherwise
check_tmux_session_exists() {
    local session_name="$1"
    if [[ -z "$session_name" ]]; then
        return 1
    fi
    # Check if tmux session exists using loom socket
    tmux -L loom has-session -t "$session_name" 2>/dev/null
}

# Convert ISO timestamp (UTC) to epoch seconds (cross-platform)
iso_to_epoch() {
    local timestamp="$1"
    if [[ -z "$timestamp" || "$timestamp" == "null" ]]; then
        echo "0"
        return
    fi
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS: date -j -f interprets as local time, so we must set TZ=UTC
        TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%SZ" "$timestamp" "+%s" 2>/dev/null || echo "0"
    else
        # Linux: date -d handles ISO 8601 with timezone suffix natively
        date -d "$timestamp" "+%s" 2>/dev/null || echo "0"
    fi
}

# Check if a role should be spawned
# Returns: JSON object with decision and reason
check_role() {
    local role="$1"
    local demand_mode="${2:-false}"
    local now_epoch
    now_epoch=$(date +%s)

    # Ensure state file exists
    if [[ ! -f "$STATE_FILE" ]]; then
        echo '{"should_spawn":true,"reason":"no_state_file","role":"'"$role"'"}'
        return 0
    fi

    # Read role status from state
    local status
    status=$(jq -r ".support_roles.${role}.status // \"idle\"" "$STATE_FILE" 2>/dev/null || echo "idle")

    # Check if already running - never spawn duplicates
    if [[ "$status" == "running" ]]; then
        local task_id tmux_session
        task_id=$(jq -r ".support_roles.${role}.task_id // \"\"" "$STATE_FILE" 2>/dev/null || echo "")
        tmux_session=$(jq -r ".support_roles.${role}.tmux_session // \"\"" "$STATE_FILE" 2>/dev/null || echo "")

        # First check tmux_session (preferred for agent-spawn.sh execution model)
        if [[ -n "$tmux_session" && "$tmux_session" != "null" ]]; then
            if check_tmux_session_exists "$tmux_session"; then
                echo '{"should_spawn":false,"reason":"already_running","role":"'"$role"'","tmux_session":"'"$tmux_session"'"}'
                return 1
            else
                # tmux_session recorded but session doesn't exist - stale state
                echo '{"should_spawn":true,"reason":"stale_tmux_session","role":"'"$role"'","stale_session":"'"$tmux_session"'"}'
                return 0
            fi
        fi

        # Fall back to task_id check (for backward compatibility with Task() execution)
        if [[ -n "$task_id" && "$task_id" != "null" ]]; then
            if validate_task_id "$task_id"; then
                echo '{"should_spawn":false,"reason":"already_running","role":"'"$role"'","task_id":"'"$task_id"'"}'
                return 1
            else
                # Fabricated task_id - treat as idle
                echo '{"should_spawn":true,"reason":"fabricated_task_id","role":"'"$role"'","stale_task_id":"'"$task_id"'"}'
                return 0
            fi
        fi

        # Running but no task_id or tmux_session - something is wrong, allow spawn
        echo '{"should_spawn":true,"reason":"running_no_identifier","role":"'"$role"'"}'
        return 0
    fi

    # In demand mode, skip interval check
    if [[ "$demand_mode" == "true" ]]; then
        echo '{"should_spawn":true,"reason":"demand","role":"'"$role"'"}'
        return 0
    fi

    # Check interval
    local interval
    interval=$(get_interval "$role")

    local last_completed
    last_completed=$(jq -r ".support_roles.${role}.last_completed // \"\"" "$STATE_FILE" 2>/dev/null || echo "")

    if [[ -z "$last_completed" || "$last_completed" == "null" ]]; then
        # Never completed - needs trigger
        echo '{"should_spawn":true,"reason":"never_run","role":"'"$role"'"}'
        return 0
    fi

    local last_epoch
    last_epoch=$(iso_to_epoch "$last_completed")

    if [[ "$last_epoch" == "0" ]]; then
        # Invalid timestamp - needs trigger
        echo '{"should_spawn":true,"reason":"invalid_timestamp","role":"'"$role"'"}'
        return 0
    fi

    local elapsed=$((now_epoch - last_epoch))

    if [[ $elapsed -gt $interval ]]; then
        echo '{"should_spawn":true,"reason":"interval_elapsed","role":"'"$role"'","elapsed_seconds":'"$elapsed"',"interval_seconds":'"$interval"'}'
        return 0
    fi

    local remaining=$((interval - elapsed))
    echo '{"should_spawn":false,"reason":"interval_not_elapsed","role":"'"$role"'","elapsed_seconds":'"$elapsed"',"interval_seconds":'"$interval"',"remaining_seconds":'"$remaining"'}'
    return 1
}

# Check all roles and output combined result
check_all_roles() {
    local json_output="${1:-false}"
    local results="[]"
    local any_should_spawn=false

    for role in "${VALID_ROLES[@]}"; do
        local result
        result=$(check_role "$role" "false") && true || true

        local should_spawn
        should_spawn=$(echo "$result" | jq -r '.should_spawn')

        if [[ "$should_spawn" == "true" ]]; then
            any_should_spawn=true
        fi

        results=$(echo "$results" | jq --argjson entry "$result" '. + [$entry]')
    done

    if [[ "$json_output" == "true" ]]; then
        jq -n \
            --argjson results "$results" \
            --argjson any_should_spawn "$any_should_spawn" \
            '{roles: $results, any_should_spawn: $any_should_spawn}'
    else
        for role in "${VALID_ROLES[@]}"; do
            local entry
            entry=$(echo "$results" | jq -r ".[] | select(.role == \"$role\")")
            local should_spawn reason
            should_spawn=$(echo "$entry" | jq -r '.should_spawn')
            reason=$(echo "$entry" | jq -r '.reason')

            if [[ "$should_spawn" == "true" ]]; then
                echo "  $role: SPAWN ($reason)"
            else
                local remaining
                remaining=$(echo "$entry" | jq -r '.remaining_seconds // "n/a"')
                echo "  $role: SKIP ($reason, ${remaining}s remaining)"
            fi
        done
    fi

    if [[ "$any_should_spawn" == "true" ]]; then
        return 0
    fi
    return 1
}

# Main entry point
main() {
    local role=""
    local demand_mode=false
    # shellcheck disable=SC2034  # Parsed from --check-only flag, reserved for future use
    local check_only=false
    local check_all=false
    local json_output=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --demand)
                demand_mode=true
                shift
                ;;
            --check-only)
                # shellcheck disable=SC2034  # Reserved for future use
                check_only=true
                shift
                ;;
            --check-all)
                check_all=true
                shift
                ;;
            --json)
                json_output=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            -*)
                echo "Unknown option: $1" >&2
                exit 2
                ;;
            *)
                if [[ -z "$role" ]]; then
                    role="$1"
                else
                    echo "Unexpected argument: $1" >&2
                    exit 2
                fi
                shift
                ;;
        esac
    done

    # Handle --check-all
    if [[ "$check_all" == "true" ]]; then
        check_all_roles "$json_output" && exit 0 || exit $?
    fi

    # Handle role check (default mode)
    if [[ -z "$role" ]]; then
        echo "Error: No role specified" >&2
        echo "Usage: spawn-support-role.sh <role> [--demand] [--check-only] [--json]" >&2
        exit 2
    fi

    if ! validate_role "$role"; then
        echo "Error: Invalid role: $role (valid: ${VALID_ROLES[*]})" >&2
        exit 2
    fi

    local result exit_code
    result=$(check_role "$role" "$demand_mode") && exit_code=0 || exit_code=$?

    if [[ "$json_output" == "true" ]]; then
        echo "$result"
    else
        local should_spawn reason
        should_spawn=$(echo "$result" | jq -r '.should_spawn')
        reason=$(echo "$result" | jq -r '.reason')

        if [[ "$should_spawn" == "true" ]]; then
            echo "SPAWN $role ($reason)"
        else
            echo "SKIP $role ($reason)"
        fi
    fi

    exit $exit_code
}

main "$@"
