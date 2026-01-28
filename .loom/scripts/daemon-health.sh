#!/usr/bin/env bash
# daemon-health.sh - Diagnostic health check for Loom daemon
#
# Usage:
#   daemon-health.sh              Display health report
#   daemon-health.sh --json       Output health report as JSON
#   daemon-health.sh --help       Show help
#
# This script consolidates daemon diagnostics into a single report:
# - Validates daemon state file structure and integrity
# - Checks shepherd task ID format (7-char hex)
# - Queries GitHub for pipeline state (label counts)
# - Detects orphaned loom:building issues (no shepherd entry)
# - Detects stale loom:building issues (no PR after threshold)
# - Reports support role spawn times vs expected intervals
# - Works when daemon is NOT running (reads last state snapshot)
#
# Exit codes:
#   0 - Healthy (no warnings or critical issues)
#   1 - Warnings detected (degraded but functional)
#   2 - Critical issues (state corruption, orphaned work)

set -euo pipefail

# Colors for output (disabled if stdout is not a terminal)
# shellcheck disable=SC2034  # Color palette - not all colors used in every script
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    GRAY='\033[0;90m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    CYAN=''
    GRAY=''
    BOLD=''
    NC=''
fi

# Find the repository root (works from any subdirectory)
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
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
DAEMON_STATE_FILE="$REPO_ROOT/.loom/daemon-state.json"

# Stale threshold in minutes for loom:building issues without a PR
STALE_BUILDING_MINUTES="${LOOM_STALE_BUILDING_MINUTES:-15}"

# Support role expected intervals (seconds)
GUIDE_INTERVAL="${LOOM_GUIDE_INTERVAL:-900}"        # 15 minutes
CHAMPION_INTERVAL="${LOOM_CHAMPION_INTERVAL:-600}"  # 10 minutes
DOCTOR_INTERVAL="${LOOM_DOCTOR_INTERVAL:-300}"      # 5 minutes
AUDITOR_INTERVAL="${LOOM_AUDITOR_INTERVAL:-600}"    # 10 minutes
JUDGE_INTERVAL="${LOOM_JUDGE_INTERVAL:-300}"        # 5 minutes

show_help() {
    cat <<EOF
${BOLD}daemon-health.sh - Diagnostic Health Check for Loom Daemon${NC}

${YELLOW}USAGE:${NC}
    daemon-health.sh              Display health report
    daemon-health.sh --json       Output health report as JSON
    daemon-health.sh --help       Show this help message

${YELLOW}DESCRIPTION:${NC}
    Provides a consolidated diagnostic view of daemon health by:

    - Validating state file JSON structure and required fields
    - Checking shepherd task ID format (7-char hex)
    - Querying GitHub for pipeline state (label counts)
    - Detecting orphaned loom:building issues (no shepherd entry)
    - Detecting stale loom:building issues (no PR after threshold)
    - Reporting support role spawn times vs expected intervals
    - Working when daemon is NOT running (reads last state snapshot)

${YELLOW}EXIT CODES:${NC}
    0   Healthy - no warnings or critical issues
    1   Warnings detected - degraded but functional
    2   Critical issues - state corruption, orphaned work

${YELLOW}ENVIRONMENT VARIABLES:${NC}
    LOOM_STALE_BUILDING_MINUTES    Minutes before flagging stale building (default: 15)
    LOOM_GUIDE_INTERVAL            Guide expected interval in seconds (default: 900)
    LOOM_CHAMPION_INTERVAL         Champion expected interval in seconds (default: 600)
    LOOM_DOCTOR_INTERVAL           Doctor expected interval in seconds (default: 300)
    LOOM_AUDITOR_INTERVAL          Auditor expected interval in seconds (default: 600)
    LOOM_JUDGE_INTERVAL            Judge expected interval in seconds (default: 300)

${YELLOW}EXAMPLES:${NC}
    # Quick diagnostic check
    daemon-health.sh

    # Machine-readable output
    daemon-health.sh --json

    # Use in scripts
    if ! daemon-health.sh >/dev/null 2>&1; then
        echo "Daemon health check failed"
    fi

${YELLOW}RELATED SCRIPTS:${NC}
    daemon-snapshot.sh             Real-time state snapshot (JSON)
    stale-building-check.sh        Detect stuck issues
    recover-orphaned-shepherds.sh  Clean up after crashes
    health-check.sh                Proactive health monitoring and alerting
EOF
}

# Parse arguments
JSON_OUTPUT=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
            echo "Run 'daemon-health.sh --help' for usage" >&2
            exit 1
            ;;
    esac
done

# ---- Tracking ----
WARNINGS=0
CRITICALS=0
WARNING_MESSAGES=()
CRITICAL_MESSAGES=()
RECOMMENDATION_MESSAGES=()

add_warning() {
    WARNINGS=$((WARNINGS + 1))
    WARNING_MESSAGES+=("$1")
}

add_critical() {
    CRITICALS=$((CRITICALS + 1))
    CRITICAL_MESSAGES+=("$1")
}

add_recommendation() {
    RECOMMENDATION_MESSAGES+=("$1")
}

# ---- Timestamp helpers ----

now_epoch() {
    date +%s
}

# Convert ISO timestamp to epoch seconds (macOS/Linux compatible)
timestamp_to_epoch() {
    local ts="$1"
    if [[ -z "$ts" ]] || [[ "$ts" == "null" ]]; then
        echo "0"
        return
    fi
    if [[ "$(uname)" == "Darwin" ]]; then
        TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" "+%s" 2>/dev/null || echo "0"
    else
        date -d "$ts" "+%s" 2>/dev/null || echo "0"
    fi
}

# Format seconds into human-readable duration
format_duration() {
    local seconds="$1"
    if [[ "$seconds" -lt 0 ]]; then
        echo "unknown"
        return
    fi
    if [[ $seconds -lt 60 ]]; then
        echo "${seconds} sec"
    elif [[ $seconds -lt 3600 ]]; then
        echo "$((seconds / 60)) min"
    elif [[ $seconds -lt 86400 ]]; then
        local hours=$((seconds / 3600))
        local mins=$(((seconds % 3600) / 60))
        echo "${hours}h ${mins}m"
    else
        local days=$((seconds / 86400))
        local hours=$(((seconds % 86400) / 3600))
        echo "${days}d ${hours}h"
    fi
}

# Format epoch diff as "N min ago" style string
time_ago() {
    local ts="$1"
    local epoch
    epoch=$(timestamp_to_epoch "$ts")
    if [[ "$epoch" == "0" ]]; then
        echo "never"
        return
    fi
    local diff=$(( $(now_epoch) - epoch ))
    echo "$(format_duration "$diff") ago"
}

# ---- Helper: read JSON file with safe fallback ----
read_json_safe() {
    local f="$1"
    if [[ -f "$f" ]] && [[ -s "$f" ]]; then
        cat "$f"
    else
        echo "[]"
    fi
}

# ---- State file validation ----
# Returns exit code:
#   0 = valid
#   1 = file missing
#   2 = invalid JSON
#   3 = missing fields (writes field names to VALIDATE_MISSING_FIELDS)
VALIDATE_MISSING_FIELDS=""

validate_state_file() {
    VALIDATE_MISSING_FIELDS=""

    if [[ ! -f "$DAEMON_STATE_FILE" ]]; then
        return 1
    fi

    # Check if valid JSON
    if ! jq -e . "$DAEMON_STATE_FILE" >/dev/null 2>&1; then
        return 2
    fi

    # Check required top-level fields
    # Use 'has()' instead of '-e' because '-e' treats false/null as failure
    local missing=()
    for field in started_at running iteration shepherds; do
        if ! jq -e "has(\"$field\")" "$DAEMON_STATE_FILE" >/dev/null 2>&1; then
            missing+=("$field")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        VALIDATE_MISSING_FIELDS="${missing[*]}"
        return 3
    fi

    return 0
}

# ---- Task ID validation ----
# Valid task IDs are 7-character hex strings (e.g., "a1b2c3d")
validate_task_id() {
    local task_id="$1"
    if [[ -z "$task_id" ]] || [[ "$task_id" == "null" ]]; then
        # null task_id is fine for idle shepherds
        return 0
    fi
    if [[ "$task_id" =~ ^[0-9a-f]{7}$ ]]; then
        return 0
    fi
    return 1
}

# ---- Pipeline state from GitHub ----

# Global pipeline variables set by get_pipeline_state
PIPELINE_READY="[]"
PIPELINE_BUILDING="[]"
PIPELINE_REVIEW="[]"
PIPELINE_MERGE="[]"
PIPELINE_BLOCKED="[]"
PIPELINE_READY_COUNT=0
PIPELINE_BUILDING_COUNT=0
PIPELINE_REVIEW_COUNT=0
PIPELINE_MERGE_COUNT=0
PIPELINE_BLOCKED_COUNT=0

get_pipeline_state() {
    local tmpdir
    tmpdir=$(mktemp -d)
    # Clean up temp dir on function exit
    trap 'rm -rf "$tmpdir"' RETURN

    # Run all queries in parallel
    gh issue list --label "loom:issue" --state open --json number,title \
        > "$tmpdir/ready" 2>/dev/null &
    local pid_ready=$!

    gh issue list --label "loom:building" --state open --json number,title,createdAt,updatedAt \
        > "$tmpdir/building" 2>/dev/null &
    local pid_building=$!

    gh pr list --label "loom:review-requested" --state open --json number,title \
        > "$tmpdir/review" 2>/dev/null &
    local pid_review=$!

    gh pr list --label "loom:pr" --state open --json number,title \
        > "$tmpdir/merge" 2>/dev/null &
    local pid_merge=$!

    gh issue list --label "loom:blocked" --state open --json number,title \
        > "$tmpdir/blocked" 2>/dev/null &
    local pid_blocked=$!

    # Wait for all queries
    wait $pid_ready $pid_building $pid_review $pid_merge $pid_blocked 2>/dev/null || true

    PIPELINE_READY=$(read_json_safe "$tmpdir/ready")
    PIPELINE_BUILDING=$(read_json_safe "$tmpdir/building")
    PIPELINE_REVIEW=$(read_json_safe "$tmpdir/review")
    PIPELINE_MERGE=$(read_json_safe "$tmpdir/merge")
    PIPELINE_BLOCKED=$(read_json_safe "$tmpdir/blocked")

    PIPELINE_READY_COUNT=$(echo "$PIPELINE_READY" | jq 'length')
    PIPELINE_BUILDING_COUNT=$(echo "$PIPELINE_BUILDING" | jq 'length')
    PIPELINE_REVIEW_COUNT=$(echo "$PIPELINE_REVIEW" | jq 'length')
    PIPELINE_MERGE_COUNT=$(echo "$PIPELINE_MERGE" | jq 'length')
    PIPELINE_BLOCKED_COUNT=$(echo "$PIPELINE_BLOCKED" | jq 'length')
}

# ---- Check orphaned loom:building (labeled but no shepherd entry) ----

ORPHANED_BUILDING=()

check_orphaned_building() {
    local building_numbers
    building_numbers=$(echo "$PIPELINE_BUILDING" | jq -r '.[].number' 2>/dev/null)

    ORPHANED_BUILDING=()

    if [[ -z "$building_numbers" ]]; then
        return
    fi

    # Get tracked issues from daemon-state shepherds
    local tracked_issues="[]"
    if [[ -f "$DAEMON_STATE_FILE" ]]; then
        tracked_issues=$(jq '[.shepherds // {} | to_entries[] | select(.value.status == "working") | .value.issue] | map(select(. != null))' "$DAEMON_STATE_FILE" 2>/dev/null || echo "[]")
    fi

    for num in $building_numbers; do
        [[ -z "$num" ]] && continue
        local is_tracked
        is_tracked=$(echo "$tracked_issues" | jq --argjson n "$num" 'any(. == $n)')
        if [[ "$is_tracked" == "false" ]]; then
            ORPHANED_BUILDING+=("$num")
        fi
    done
}

# ---- Check stale loom:building (no PR after threshold) ----

STALE_BUILDING=()

check_stale_building() {
    STALE_BUILDING=()
    local threshold_secs=$((STALE_BUILDING_MINUTES * 60))
    local now
    now=$(now_epoch)

    # Get all open PRs once for matching
    local open_prs
    open_prs=$(gh pr list --state open --json number,headRefName,body 2>/dev/null || echo "[]")

    local count
    count=$(echo "$PIPELINE_BUILDING" | jq 'length')

    local i
    for i in $(seq 0 $((count - 1))); do
        local issue
        issue=$(echo "$PIPELINE_BUILDING" | jq -c ".[$i]")
        local num
        num=$(echo "$issue" | jq -r '.number')
        local updated_at
        updated_at=$(echo "$issue" | jq -r '.updatedAt // .createdAt')

        local updated_epoch
        updated_epoch=$(timestamp_to_epoch "$updated_at")
        if [[ "$updated_epoch" == "0" ]]; then
            continue
        fi

        local age_secs=$((now - updated_epoch))
        if [[ $age_secs -lt $threshold_secs ]]; then
            continue
        fi

        # Check if a PR exists for this issue
        local has_pr
        has_pr=$(echo "$open_prs" | jq --arg n "$num" \
            '[.[] | select(
                (.body // "" | test("(Closes|Fixes|Resolves) #" + $n + "\\b"; "i")) or
                (.headRefName | test("issue-" + $n + "\\b"))
            )] | length > 0')

        if [[ "$has_pr" == "false" ]]; then
            local age_min=$((age_secs / 60))
            STALE_BUILDING+=("${num}:${age_min}")
        fi
    done
}

# ---- Check support role spawn times ----

SUPPORT_ROLE_STATUS=()

check_support_roles() {
    SUPPORT_ROLE_STATUS=()
    local now
    now=$(now_epoch)

    local roles=("guide" "judge" "champion" "doctor" "auditor")
    local intervals=("$GUIDE_INTERVAL" "$JUDGE_INTERVAL" "$CHAMPION_INTERVAL" "$DOCTOR_INTERVAL" "$AUDITOR_INTERVAL")
    local interval_display=("15 min" "5 min" "10 min" "5 min" "10 min")

    local idx
    for idx in "${!roles[@]}"; do
        local role="${roles[$idx]}"
        local expected_interval="${intervals[$idx]}"
        local interval_str="${interval_display[$idx]}"

        local last_completed=""
        local status="unknown"

        if [[ -f "$DAEMON_STATE_FILE" ]]; then
            last_completed=$(jq -r ".support_roles.${role}.last_completed // \"\"" "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
            status=$(jq -r ".support_roles.${role}.status // \"idle\"" "$DAEMON_STATE_FILE" 2>/dev/null || echo "idle")
        fi

        local display_name
        display_name="$(echo "${role:0:1}" | tr '[:lower:]' '[:upper:]')${role:1}"

        if [[ -z "$last_completed" ]] || [[ "$last_completed" == "null" ]] || [[ "$last_completed" == "" ]]; then
            SUPPORT_ROLE_STATUS+=("${display_name}:NEVER_SPAWNED:${interval_str}:${status}")
            if [[ "$status" != "running" ]]; then
                add_warning "$display_name has NEVER SPAWNED (should spawn every $interval_str)"
            fi
        else
            local lc_epoch
            lc_epoch=$(timestamp_to_epoch "$last_completed")
            if [[ "$lc_epoch" == "0" ]]; then
                SUPPORT_ROLE_STATUS+=("${display_name}:UNKNOWN:${interval_str}:${status}")
                continue
            fi
            local elapsed=$((now - lc_epoch))
            local elapsed_str
            elapsed_str=$(format_duration "$elapsed")
            SUPPORT_ROLE_STATUS+=("${display_name}:${elapsed_str}:${interval_str}:${status}")

            # Check if overdue (only warn if not currently running)
            if [[ "$status" != "running" ]] && [[ $elapsed -gt $((expected_interval * 2)) ]]; then
                add_warning "$display_name last completed $elapsed_str ago (expected every $interval_str)"
            fi
        fi
    done
}

# Format issue/PR number lists from JSON array
format_numbers() {
    local json_array="$1"
    local count
    count=$(echo "$json_array" | jq 'length')
    if [[ "$count" -eq 0 ]]; then
        echo ""
        return
    fi
    echo "$json_array" | jq -r '[.[].number] | map("#" + tostring) | join(", ")' 2>/dev/null
}

# ============================
# MAIN EXECUTION
# ============================

# ---- 1. Validate state file ----
STATE_FILE_STATUS="ok"
STATE_FILE_DETAILS=""
DAEMON_RUNNING="false"
DAEMON_ITERATION=0
DAEMON_STARTED_AT=""
DAEMON_FORCE_MODE="false"

if validate_state_file; then
    STATE_FILE_STATUS="ok"
    DAEMON_RUNNING=$(jq -r '.running // false' "$DAEMON_STATE_FILE")
    DAEMON_ITERATION=$(jq -r '.iteration // 0' "$DAEMON_STATE_FILE")
    DAEMON_STARTED_AT=$(jq -r '.started_at // ""' "$DAEMON_STATE_FILE")
    DAEMON_FORCE_MODE=$(jq -r '.force_mode // false' "$DAEMON_STATE_FILE")
else
    validate_exit=$?
    case $validate_exit in
        1)
            STATE_FILE_STATUS="missing"
            STATE_FILE_DETAILS="No daemon state file found at $DAEMON_STATE_FILE"
            add_critical "Daemon state file not found"
            add_recommendation "Start the daemon with /loom or /loom --force"
            ;;
        2)
            STATE_FILE_STATUS="corrupt"
            STATE_FILE_DETAILS="State file contains invalid JSON"
            add_critical "Daemon state file is corrupt (invalid JSON)"
            add_recommendation "Fix state corruption: delete .loom/daemon-state.json and restart daemon"
            ;;
        3)
            STATE_FILE_STATUS="incomplete"
            STATE_FILE_DETAILS="Missing required fields: $VALIDATE_MISSING_FIELDS"
            add_critical "Daemon state file missing required fields: $VALIDATE_MISSING_FIELDS"
            add_recommendation "State file may be partially written; restart daemon to regenerate"
            ;;
    esac
fi

# ---- 2. Validate shepherd task IDs ----
SHEPHERD_DETAILS=()
INVALID_TASK_IDS=0
TOTAL_SHEPHERDS=0

if [[ "$STATE_FILE_STATUS" == "ok" ]]; then
    shepherd_keys=$(jq -r '.shepherds // {} | keys[]' "$DAEMON_STATE_FILE" 2>/dev/null || true)

    for shepherd_key in $shepherd_keys; do
        TOTAL_SHEPHERDS=$((TOTAL_SHEPHERDS + 1))
        task_id=$(jq -r ".shepherds[\"$shepherd_key\"].task_id // null" "$DAEMON_STATE_FILE")
        status=$(jq -r ".shepherds[\"$shepherd_key\"].status // \"unknown\"" "$DAEMON_STATE_FILE")
        issue=$(jq -r ".shepherds[\"$shepherd_key\"].issue // null" "$DAEMON_STATE_FILE")

        task_id_valid="true"
        if [[ "$task_id" != "null" ]] && [[ -n "$task_id" ]]; then
            if ! validate_task_id "$task_id"; then
                task_id_valid="false"
                INVALID_TASK_IDS=$((INVALID_TASK_IDS + 1))
            fi
        fi

        SHEPHERD_DETAILS+=("${shepherd_key}:${task_id}:${status}:${issue}:${task_id_valid}")
    done

    if [[ $INVALID_TASK_IDS -gt 0 ]]; then
        add_critical "$INVALID_TASK_IDS/$TOTAL_SHEPHERDS shepherds have invalid task IDs -- completion tracking broken"
        add_recommendation "Fix shepherd task IDs (state corruption)"
    fi
fi

# ---- 3. Get pipeline state from GitHub ----
get_pipeline_state

# ---- 4. Check orphaned building issues ----
check_orphaned_building

if [[ ${#ORPHANED_BUILDING[@]} -gt 0 ]]; then
    orphan_list=$(printf "#%s, " "${ORPHANED_BUILDING[@]}")
    orphan_list="${orphan_list%, }"
    add_warning "Orphaned loom:building issues (labeled but no shepherd): $orphan_list"
    add_recommendation "Check orphaned issues with: ./.loom/scripts/stale-building-check.sh --recover"
fi

# ---- 5. Check stale building issues ----
check_stale_building

if [[ ${#STALE_BUILDING[@]} -gt 0 ]]; then
    for entry in "${STALE_BUILDING[@]}"; do
        num="${entry%%:*}"
        age="${entry##*:}"
        add_warning "#$num in loom:building for ${age} min with no PR"
    done
    add_recommendation "Check stale loom:building issues (>${STALE_BUILDING_MINUTES} min without PR)"
fi

# ---- 6. Check support roles ----
check_support_roles

# Count how many support roles have never spawned
NEVER_SPAWNED_COUNT=0
for entry in "${SUPPORT_ROLE_STATUS[@]}"; do
    spawned_status="${entry#*:}"
    spawned_status="${spawned_status%%:*}"
    if [[ "$spawned_status" == "NEVER_SPAWNED" ]]; then
        NEVER_SPAWNED_COUNT=$((NEVER_SPAWNED_COUNT + 1))
    fi
done

if [[ $NEVER_SPAWNED_COUNT -eq ${#SUPPORT_ROLE_STATUS[@]} ]] && [[ $NEVER_SPAWNED_COUNT -gt 0 ]]; then
    add_warning "No support roles have run this session"
    add_recommendation "Spawn Guide for triage"
fi

# ---- Determine final exit code ----
EXIT_CODE=0
if [[ $CRITICALS -gt 0 ]]; then
    EXIT_CODE=2
elif [[ $WARNINGS -gt 0 ]]; then
    EXIT_CODE=1
fi

# ============================
# OUTPUT
# ============================

if [[ "$JSON_OUTPUT" == "true" ]]; then
    # Build JSON arrays for warnings and criticals
    warnings_json="[]"
    for msg in "${WARNING_MESSAGES[@]+"${WARNING_MESSAGES[@]}"}"; do
        warnings_json=$(echo "$warnings_json" | jq --arg m "$msg" '. + [$m]')
    done

    criticals_json="[]"
    for msg in "${CRITICAL_MESSAGES[@]+"${CRITICAL_MESSAGES[@]}"}"; do
        criticals_json=$(echo "$criticals_json" | jq --arg m "$msg" '. + [$m]')
    done

    recommendations_json="[]"
    for msg in "${RECOMMENDATION_MESSAGES[@]+"${RECOMMENDATION_MESSAGES[@]}"}"; do
        recommendations_json=$(echo "$recommendations_json" | jq --arg m "$msg" '. + [$m]')
    done

    # Build shepherd details JSON
    shepherds_json="[]"
    for entry in "${SHEPHERD_DETAILS[@]+"${SHEPHERD_DETAILS[@]}"}"; do
        IFS=':' read -r s_key s_task_id s_status s_issue s_valid <<< "$entry"
        shepherds_json=$(echo "$shepherds_json" | jq \
            --arg key "$s_key" \
            --arg task_id "$s_task_id" \
            --arg status "$s_status" \
            --arg issue "$s_issue" \
            --arg valid "$s_valid" \
            '. + [{key: $key, task_id: $task_id, status: $status, issue: (if $issue == "null" then null else ($issue | tonumber? // $issue) end), task_id_valid: ($valid == "true")}]')
    done

    # Build support roles JSON
    support_json="[]"
    for entry in "${SUPPORT_ROLE_STATUS[@]+"${SUPPORT_ROLE_STATUS[@]}"}"; do
        IFS=':' read -r sr_name sr_elapsed sr_interval sr_status <<< "$entry"
        support_json=$(echo "$support_json" | jq \
            --arg name "$sr_name" \
            --arg elapsed "$sr_elapsed" \
            --arg interval "$sr_interval" \
            --arg status "$sr_status" \
            '. + [{name: $name, last_completed_ago: $elapsed, expected_interval: $interval, current_status: $status}]')
    done

    # Build orphaned and stale arrays
    orphaned_json="[]"
    for num in "${ORPHANED_BUILDING[@]+"${ORPHANED_BUILDING[@]}"}"; do
        orphaned_json=$(echo "$orphaned_json" | jq --argjson n "$num" '. + [$n]')
    done

    stale_json="[]"
    for entry in "${STALE_BUILDING[@]+"${STALE_BUILDING[@]}"}"; do
        s_num="${entry%%:*}"
        s_age="${entry##*:}"
        stale_json=$(echo "$stale_json" | jq --argjson n "$s_num" --argjson a "$s_age" '. + [{issue: $n, age_minutes: $a}]')
    done

    # Build the full JSON output
    jq -n \
        --arg state_file "$DAEMON_STATE_FILE" \
        --arg state_status "$STATE_FILE_STATUS" \
        --argjson running "$DAEMON_RUNNING" \
        --argjson iteration "$DAEMON_ITERATION" \
        --arg started_at "$DAEMON_STARTED_AT" \
        --argjson force_mode "$DAEMON_FORCE_MODE" \
        --argjson shepherds "$shepherds_json" \
        --argjson invalid_task_ids "$INVALID_TASK_IDS" \
        --argjson total_shepherds "$TOTAL_SHEPHERDS" \
        --argjson pipeline_ready "$PIPELINE_READY_COUNT" \
        --argjson pipeline_building "$PIPELINE_BUILDING_COUNT" \
        --argjson pipeline_review "$PIPELINE_REVIEW_COUNT" \
        --argjson pipeline_merge "$PIPELINE_MERGE_COUNT" \
        --argjson pipeline_blocked "$PIPELINE_BLOCKED_COUNT" \
        --argjson ready_issues "$PIPELINE_READY" \
        --argjson building_issues "$PIPELINE_BUILDING" \
        --argjson review_prs "$PIPELINE_REVIEW" \
        --argjson merge_prs "$PIPELINE_MERGE" \
        --argjson blocked_issues "$PIPELINE_BLOCKED" \
        --argjson orphaned_building "$orphaned_json" \
        --argjson stale_building "$stale_json" \
        --argjson support_roles "$support_json" \
        --argjson warnings_list "$warnings_json" \
        --argjson criticals_list "$criticals_json" \
        --argjson recommendations "$recommendations_json" \
        --argjson warning_count "$WARNINGS" \
        --argjson critical_count "$CRITICALS" \
        --argjson exit_code "$EXIT_CODE" \
        '{
            state_file: {
                path: $state_file,
                status: $state_status
            },
            daemon: {
                running: $running,
                iteration: $iteration,
                started_at: $started_at,
                force_mode: $force_mode
            },
            shepherds: {
                entries: $shepherds,
                invalid_task_ids: $invalid_task_ids,
                total: $total_shepherds
            },
            pipeline: {
                ready: { count: $pipeline_ready, issues: $ready_issues },
                building: { count: $pipeline_building, issues: $building_issues },
                review_requested: { count: $pipeline_review, prs: $review_prs },
                ready_to_merge: { count: $pipeline_merge, prs: $merge_prs },
                blocked: { count: $pipeline_blocked, issues: $blocked_issues }
            },
            consistency: {
                orphaned_building: $orphaned_building,
                stale_building: $stale_building
            },
            support_roles: $support_roles,
            diagnostics: {
                warnings: $warnings_list,
                criticals: $criticals_list,
                recommendations: $recommendations,
                warning_count: $warning_count,
                critical_count: $critical_count,
                exit_code: $exit_code
            }
        }'

    exit "$EXIT_CODE"
fi

# ---- Human-readable output ----

echo ""
echo -e "${BOLD}LOOM DAEMON HEALTH CHECK${NC}"
echo "========================"
echo ""

# State File section
echo -e "${BOLD}State File:${NC} $DAEMON_STATE_FILE"

if [[ "$STATE_FILE_STATUS" == "ok" ]]; then
    status_label="running"
    if [[ "$DAEMON_RUNNING" == "false" ]]; then
        status_label="stopped"
    fi
    echo -e "  Status: ${BOLD}$status_label${NC} (iteration $DAEMON_ITERATION)"

    if [[ -n "$DAEMON_STARTED_AT" ]] && [[ "$DAEMON_STARTED_AT" != "null" ]]; then
        echo -e "  Started: $DAEMON_STARTED_AT ($(time_ago "$DAEMON_STARTED_AT"))"
    fi

    if [[ "$DAEMON_FORCE_MODE" == "true" ]]; then
        echo -e "  Force mode: ${YELLOW}enabled${NC}"
    else
        echo -e "  Force mode: disabled"
    fi

    if [[ "$DAEMON_RUNNING" == "false" ]]; then
        echo -e "  ${GRAY}(showing last known state)${NC}"
    fi
elif [[ "$STATE_FILE_STATUS" == "missing" ]]; then
    echo -e "  ${RED}CRITICAL: State file not found${NC}"
    echo -e "  ${GRAY}Daemon may have never started. Run /loom or /loom --force${NC}"
elif [[ "$STATE_FILE_STATUS" == "corrupt" ]]; then
    echo -e "  ${RED}CRITICAL: State file contains invalid JSON${NC}"
    echo -e "  ${GRAY}Delete .loom/daemon-state.json and restart daemon${NC}"
elif [[ "$STATE_FILE_STATUS" == "incomplete" ]]; then
    echo -e "  ${RED}CRITICAL: State file missing required fields${NC}"
    echo -e "  ${GRAY}$STATE_FILE_DETAILS${NC}"
fi
echo ""

# Shepherd State Integrity
echo -e "${BOLD}Shepherd State Integrity:${NC}"
if [[ ${#SHEPHERD_DETAILS[@]} -eq 0 ]]; then
    echo -e "  ${GRAY}No shepherd data available${NC}"
else
    for entry in "${SHEPHERD_DETAILS[@]}"; do
        IFS=':' read -r s_key s_task_id s_status s_issue s_valid <<< "$entry"

        if [[ "$s_task_id" == "null" ]] || [[ -z "$s_task_id" ]]; then
            echo -e "  ${GRAY}$s_key: idle (no task)${NC}"
        elif [[ "$s_valid" == "true" ]]; then
            issue_display=""
            if [[ "$s_issue" != "null" ]] && [[ -n "$s_issue" ]]; then
                issue_display=" issue=#$s_issue"
            fi
            echo -e "  ${GREEN}$s_key: task_id=\"$s_task_id\" $s_status$issue_display${NC}"
        else
            echo -e "  ${RED}$s_key: task_id=\"$s_task_id\" <- INVALID (not 7-char hex)${NC}"
        fi
    done

    if [[ $INVALID_TASK_IDS -gt 0 ]]; then
        echo -e "  ${RED}WARNING: $INVALID_TASK_IDS/$TOTAL_SHEPHERDS shepherds have invalid task IDs -- completion tracking broken${NC}"
    fi
fi
echo ""

# Pipeline Consistency
echo -e "${BOLD}Pipeline Consistency:${NC}"

ready_nums=$(format_numbers "$PIPELINE_READY")
building_nums=$(format_numbers "$PIPELINE_BUILDING")
review_nums=$(format_numbers "$PIPELINE_REVIEW")
merge_nums=$(format_numbers "$PIPELINE_MERGE")
blocked_nums=$(format_numbers "$PIPELINE_BLOCKED")

printf "  %-27s %d issues" "loom:issue (ready):" "$PIPELINE_READY_COUNT"
[[ -n "$ready_nums" ]] && printf " (%s)" "$ready_nums"
echo ""

printf "  %-27s %d issues" "loom:building:" "$PIPELINE_BUILDING_COUNT"
[[ -n "$building_nums" ]] && printf " (%s)" "$building_nums"
echo ""

printf "  %-27s %d PRs" "loom:review-requested:" "$PIPELINE_REVIEW_COUNT"
[[ -n "$review_nums" ]] && printf " (%s)" "$review_nums"
echo ""

printf "  %-27s %d PRs" "loom:pr (ready merge):" "$PIPELINE_MERGE_COUNT"
[[ -n "$merge_nums" ]] && printf " (%s)" "$merge_nums"
echo ""

printf "  %-27s %d issues" "loom:blocked:" "$PIPELINE_BLOCKED_COUNT"
[[ -n "$blocked_nums" ]] && printf " (%s)" "$blocked_nums"
echo ""
echo ""

# Orphaned/stale building
if [[ ${#ORPHANED_BUILDING[@]} -eq 0 ]]; then
    echo -e "  Orphaned loom:building: ${GREEN}NONE${NC} (all have shepherd entries)"
else
    echo -e "  Orphaned loom:building: ${RED}${#ORPHANED_BUILDING[@]} issue(s)${NC}"
    for num in "${ORPHANED_BUILDING[@]}"; do
        echo -e "    ${YELLOW}#$num (labeled but no active shepherd)${NC}"
    done
fi

if [[ ${#STALE_BUILDING[@]} -eq 0 ]]; then
    echo -e "  Stale loom:building:    ${GREEN}NONE${NC}"
else
    echo -e "  Stale loom:building:    ${YELLOW}${#STALE_BUILDING[@]} issue(s)${NC}"
    for entry in "${STALE_BUILDING[@]}"; do
        s_num="${entry%%:*}"
        s_age="${entry##*:}"
        echo -e "    ${YELLOW}#$s_num (${s_age} min, no PR yet)${NC}"
    done
fi
echo ""

# Support Roles
echo -e "${BOLD}Support Roles:${NC}"
for entry in "${SUPPORT_ROLE_STATUS[@]+"${SUPPORT_ROLE_STATUS[@]}"}"; do
    IFS=':' read -r sr_name sr_elapsed sr_interval sr_status <<< "$entry"

    if [[ "$sr_status" == "running" ]]; then
        printf "  %-12s ${GREEN}RUNNING${NC} (interval: every %s)\n" "$sr_name:" "$sr_interval"
    elif [[ "$sr_elapsed" == "NEVER_SPAWNED" ]]; then
        printf "  %-12s ${RED}NEVER SPAWNED${NC} (should spawn every %s)\n" "$sr_name:" "$sr_interval"
    elif [[ "$sr_elapsed" == "UNKNOWN" ]]; then
        printf "  %-12s ${GRAY}unknown${NC} (interval: every %s)\n" "$sr_name:" "$sr_interval"
    else
        printf "  %-12s %s ago (interval: every %s)\n" "$sr_name:" "$sr_elapsed" "$sr_interval"
    fi
done

# Check if any never spawned
if [[ $NEVER_SPAWNED_COUNT -eq ${#SUPPORT_ROLE_STATUS[@]} ]] && [[ $NEVER_SPAWNED_COUNT -gt 0 ]]; then
    echo -e "  ${YELLOW}WARNING: No support roles have run this session${NC}"
fi
echo ""

# Recommendations
if [[ ${#RECOMMENDATION_MESSAGES[@]} -gt 0 ]]; then
    echo -e "${BOLD}Recommendations:${NC}"
    rec_num=1
    for msg in "${RECOMMENDATION_MESSAGES[@]}"; do
        echo -e "  $rec_num. $msg"
        rec_num=$((rec_num + 1))
    done
    echo ""
fi

# Summary
if [[ $EXIT_CODE -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}Health: OK${NC} - No issues detected"
elif [[ $EXIT_CODE -eq 1 ]]; then
    echo -e "${YELLOW}${BOLD}Health: WARNINGS${NC} - $WARNINGS warning(s) detected"
else
    echo -e "${RED}${BOLD}Health: CRITICAL${NC} - $CRITICALS critical issue(s), $WARNINGS warning(s)"
fi
echo ""

exit "$EXIT_CODE"
