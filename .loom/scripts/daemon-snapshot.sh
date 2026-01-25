#!/bin/bash
# daemon-snapshot.sh - Consolidated daemon state snapshot
#
# Usage:
#   daemon-snapshot.sh                # Output JSON snapshot
#   daemon-snapshot.sh --pretty       # Pretty-printed JSON
#   daemon-snapshot.sh --help         # Show help
#
# This script consolidates all daemon state queries into a single JSON output,
# running gh queries in parallel for efficiency. It replaces 10+ individual
# tool calls with a single deterministic script.
#
# Output structure:
# {
#   "timestamp": "...",
#   "pipeline": { ready_issues, building_issues, ... },
#   "proposals": { architect, hermit, curated },
#   "prs": { review_requested, changes_requested, ready_to_merge },
#   "usage": { session_percent, ... },
#   "computed": { total_ready, needs_work_generation, ... },
#   "config": { issue_threshold, max_shepherds, ... }
# }

set -euo pipefail

# Colors for output (only used with --pretty)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default configuration thresholds
ISSUE_THRESHOLD="${LOOM_ISSUE_THRESHOLD:-3}"
MAX_SHEPHERDS="${LOOM_MAX_SHEPHERDS:-3}"
MAX_PROPOSALS="${LOOM_MAX_PROPOSALS:-5}"
ARCHITECT_COOLDOWN="${LOOM_ARCHITECT_COOLDOWN:-1800}"
HERMIT_COOLDOWN="${LOOM_HERMIT_COOLDOWN:-1800}"

# Issue selection strategy: fifo (default), lifo, priority
# - fifo: Oldest issues first (FIFO - prevents starvation)
# - lifo: Newest issues first (LIFO - current GitHub API default)
# - priority: Sort by loom:urgent first, then by age (oldest first)
# Note: loom:urgent always takes precedence regardless of strategy
ISSUE_STRATEGY="${LOOM_ISSUE_STRATEGY:-fifo}"

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
DAEMON_STATE_FILE="$REPO_ROOT/.loom/daemon-state.json"
PROGRESS_DIR="$REPO_ROOT/.loom/progress"

# Heartbeat staleness threshold in seconds (default: 2 minutes)
HEARTBEAT_STALE_THRESHOLD="${LOOM_HEARTBEAT_STALE_THRESHOLD:-120}"

show_help() {
    cat <<EOF
daemon-snapshot.sh - Consolidated daemon state snapshot

USAGE:
    daemon-snapshot.sh              Output JSON snapshot (compact)
    daemon-snapshot.sh --pretty     Output pretty-printed JSON
    daemon-snapshot.sh --help       Show this help

DESCRIPTION:
    Consolidates all daemon state queries into a single JSON output.
    Runs GitHub API queries in parallel for efficiency.

    Replaces 10+ individual tool calls:
    - gh issue list --label "loom:issue"
    - gh issue list --label "loom:building"
    - gh issue list --label "loom:architect"
    - gh issue list --label "loom:hermit"
    - gh issue list --label "loom:curated"
    - gh pr list --label "loom:review-requested"
    - gh pr list --label "loom:changes-requested"
    - gh pr list --label "loom:pr"
    - check-usage.sh

ENVIRONMENT VARIABLES:
    LOOM_ISSUE_THRESHOLD     Threshold for work generation (default: 3)
    LOOM_MAX_SHEPHERDS       Maximum concurrent shepherds (default: 3)
    LOOM_MAX_PROPOSALS       Maximum pending proposals (default: 5)
    LOOM_ARCHITECT_COOLDOWN  Architect trigger cooldown in seconds (default: 1800)
    LOOM_HERMIT_COOLDOWN     Hermit trigger cooldown in seconds (default: 1800)
    LOOM_ISSUE_STRATEGY      Issue selection strategy (default: fifo)
                             - fifo: Oldest issues first (prevents starvation)
                             - lifo: Newest issues first
                             - priority: loom:urgent first, then oldest
                             Note: loom:urgent always takes precedence

OUTPUT:
    JSON object with fields:
    - timestamp: ISO 8601 timestamp
    - pipeline: Issue state counts
    - proposals: Proposal issue lists
    - prs: PR state lists
    - usage: Session usage from claude-monitor (if available)
    - computed: Pre-computed decision values
    - config: Current threshold configuration

EXAMPLE OUTPUT:
    {
      "timestamp": "2026-01-25T08:00:00Z",
      "pipeline": {
        "ready_issues": [{"number": 46, "title": "..."}],
        "building_issues": []
      },
      "proposals": {
        "architect": [{"number": 47, "title": "..."}],
        "hermit": [],
        "curated": []
      },
      "computed": {
        "total_ready": 1,
        "needs_work_generation": false,
        "recommended_actions": ["spawn_shepherds"]
      }
    }
EOF
}

# Parse arguments
PRETTY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pretty)
            PRETTY=true
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Run 'daemon-snapshot.sh --help' for usage" >&2
            exit 1
            ;;
    esac
done

# Create temp directory for parallel query outputs
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Run all gh queries in parallel
# Each query writes to a temp file

# Issues
gh issue list --label "loom:issue" --state open --json number,title,labels,createdAt \
    > "$TMPDIR/ready_issues" 2>/dev/null &
PID_READY=$!

gh issue list --label "loom:building" --state open --json number,title,labels \
    > "$TMPDIR/building_issues" 2>/dev/null &
PID_BUILDING=$!

gh issue list --label "loom:architect" --state open --json number,title,labels \
    > "$TMPDIR/architect_proposals" 2>/dev/null &
PID_ARCHITECT=$!

gh issue list --label "loom:hermit" --state open --json number,title,labels \
    > "$TMPDIR/hermit_proposals" 2>/dev/null &
PID_HERMIT=$!

gh issue list --label "loom:curated" --state open --json number,title,labels \
    > "$TMPDIR/curated_issues" 2>/dev/null &
PID_CURATED=$!

gh issue list --label "loom:blocked" --state open --json number,title,labels \
    > "$TMPDIR/blocked_issues" 2>/dev/null &
PID_BLOCKED=$!

# PRs
gh pr list --label "loom:review-requested" --state open --json number,title,labels,headRefName \
    > "$TMPDIR/review_requested_prs" 2>/dev/null &
PID_REVIEW=$!

gh pr list --label "loom:changes-requested" --state open --json number,title,labels,headRefName \
    > "$TMPDIR/changes_requested_prs" 2>/dev/null &
PID_CHANGES=$!

gh pr list --label "loom:pr" --state open --json number,title,labels,headRefName \
    > "$TMPDIR/ready_to_merge_prs" 2>/dev/null &
PID_MERGE=$!

# Usage stats (if check-usage.sh exists)
if [[ -x "$REPO_ROOT/.loom/scripts/check-usage.sh" ]]; then
    "$REPO_ROOT/.loom/scripts/check-usage.sh" > "$TMPDIR/usage" 2>/dev/null &
    PID_USAGE=$!
else
    echo '{"error": "check-usage.sh not found"}' > "$TMPDIR/usage" &
    PID_USAGE=$!
fi

# Wait for all queries to complete
wait $PID_READY $PID_BUILDING $PID_ARCHITECT $PID_HERMIT $PID_CURATED $PID_BLOCKED \
     $PID_REVIEW $PID_CHANGES $PID_MERGE $PID_USAGE 2>/dev/null || true

# Read results (with fallbacks for empty/failed queries)
read_json_file() {
    local file="$1"
    if [[ -f "$file" ]] && [[ -s "$file" ]]; then
        cat "$file"
    else
        echo "[]"
    fi
}

READY_ISSUES_RAW=$(read_json_file "$TMPDIR/ready_issues")

# Sort ready issues based on ISSUE_STRATEGY
# loom:urgent always takes precedence (sorted first)
# Then apply the configured strategy to the remaining issues
sort_issues() {
    local issues="$1"
    local strategy="$2"

    # Partition into urgent and non-urgent
    # Urgent issues are always first, sorted by createdAt (oldest first within urgent)
    # Non-urgent issues are sorted according to strategy

    case "$strategy" in
        fifo)
            # FIFO: Oldest first (ascending by createdAt)
            # Urgent first (oldest urgent), then non-urgent (oldest first)
            echo "$issues" | jq '
                (map(select([.labels[].name] | contains(["loom:urgent"]))) | sort_by(.createdAt)) +
                (map(select([.labels[].name] | contains(["loom:urgent"]) | not)) | sort_by(.createdAt))
            '
            ;;
        lifo)
            # LIFO: Newest first (descending by createdAt)
            # Urgent first (newest urgent), then non-urgent (newest first)
            echo "$issues" | jq '
                (map(select([.labels[].name] | contains(["loom:urgent"]))) | sort_by(.createdAt) | reverse) +
                (map(select([.labels[].name] | contains(["loom:urgent"]) | not)) | sort_by(.createdAt) | reverse)
            '
            ;;
        priority)
            # Priority: loom:urgent first (oldest), then by age (oldest first)
            # Same as fifo but explicitly named for clarity
            echo "$issues" | jq '
                (map(select([.labels[].name] | contains(["loom:urgent"]))) | sort_by(.createdAt)) +
                (map(select([.labels[].name] | contains(["loom:urgent"]) | not)) | sort_by(.createdAt))
            '
            ;;
        *)
            # Unknown strategy, warn and fall back to fifo
            echo "Warning: Unknown issue strategy '$strategy', falling back to fifo" >&2
            echo "$issues" | jq '
                (map(select([.labels[].name] | contains(["loom:urgent"]))) | sort_by(.createdAt)) +
                (map(select([.labels[].name] | contains(["loom:urgent"]) | not)) | sort_by(.createdAt))
            '
            ;;
    esac
}

READY_ISSUES=$(sort_issues "$READY_ISSUES_RAW" "$ISSUE_STRATEGY")
BUILDING_ISSUES=$(read_json_file "$TMPDIR/building_issues")
ARCHITECT_PROPOSALS=$(read_json_file "$TMPDIR/architect_proposals")
HERMIT_PROPOSALS=$(read_json_file "$TMPDIR/hermit_proposals")
CURATED_ISSUES=$(read_json_file "$TMPDIR/curated_issues")
BLOCKED_ISSUES=$(read_json_file "$TMPDIR/blocked_issues")
REVIEW_REQUESTED=$(read_json_file "$TMPDIR/review_requested_prs")
CHANGES_REQUESTED=$(read_json_file "$TMPDIR/changes_requested_prs")
READY_TO_MERGE=$(read_json_file "$TMPDIR/ready_to_merge_prs")

# Usage may be an object or error
if [[ -f "$TMPDIR/usage" ]] && [[ -s "$TMPDIR/usage" ]]; then
    USAGE=$(cat "$TMPDIR/usage")
    # Check if it's valid JSON
    if ! echo "$USAGE" | jq -e . >/dev/null 2>&1; then
        USAGE='{"error": "invalid response"}'
    fi
else
    USAGE='{"error": "no data"}'
fi

# Read daemon state for active shepherd count and cooldown timestamps
ACTIVE_SHEPHERDS=0
LAST_ARCHITECT_TRIGGER=""
LAST_HERMIT_TRIGGER=""

if [[ -f "$DAEMON_STATE_FILE" ]]; then
    # Count active shepherds (those with status="working")
    ACTIVE_SHEPHERDS=$(jq -r '[.shepherds // {} | to_entries[] | select(.value.status == "working")] | length' "$DAEMON_STATE_FILE" 2>/dev/null || echo "0")
    LAST_ARCHITECT_TRIGGER=$(jq -r '.last_architect_trigger // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
    LAST_HERMIT_TRIGGER=$(jq -r '.last_hermit_trigger // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
fi

# Calculate counts
READY_COUNT=$(echo "$READY_ISSUES" | jq 'length')
BUILDING_COUNT=$(echo "$BUILDING_ISSUES" | jq 'length')
ARCHITECT_COUNT=$(echo "$ARCHITECT_PROPOSALS" | jq 'length')
HERMIT_COUNT=$(echo "$HERMIT_PROPOSALS" | jq 'length')
CURATED_COUNT=$(echo "$CURATED_ISSUES" | jq 'length')
BLOCKED_COUNT=$(echo "$BLOCKED_ISSUES" | jq 'length')
REVIEW_COUNT=$(echo "$REVIEW_REQUESTED" | jq 'length')
CHANGES_COUNT=$(echo "$CHANGES_REQUESTED" | jq 'length')
MERGE_COUNT=$(echo "$READY_TO_MERGE" | jq 'length')

TOTAL_PROPOSALS=$((ARCHITECT_COUNT + HERMIT_COUNT + CURATED_COUNT))
TOTAL_IN_FLIGHT=$((BUILDING_COUNT + REVIEW_COUNT + CHANGES_COUNT + MERGE_COUNT))
AVAILABLE_SHEPHERD_SLOTS=$((MAX_SHEPHERDS - ACTIVE_SHEPHERDS))

# Compute needs_work_generation
NEEDS_WORK_GEN="false"
if [[ $READY_COUNT -lt $ISSUE_THRESHOLD ]] && [[ $TOTAL_PROPOSALS -lt $MAX_PROPOSALS ]]; then
    NEEDS_WORK_GEN="true"
fi

# Calculate cooldown status
NOW_EPOCH=$(date +%s)
ARCHITECT_COOLDOWN_OK="false"
HERMIT_COOLDOWN_OK="false"

if [[ -n "$LAST_ARCHITECT_TRIGGER" ]]; then
    # Convert ISO timestamp to epoch
    if [[ "$(uname)" == "Darwin" ]]; then
        ARCH_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$LAST_ARCHITECT_TRIGGER" "+%s" 2>/dev/null || echo "0")
    else
        ARCH_EPOCH=$(date -d "$LAST_ARCHITECT_TRIGGER" "+%s" 2>/dev/null || echo "0")
    fi
    ARCH_ELAPSED=$((NOW_EPOCH - ARCH_EPOCH))
    if [[ $ARCH_ELAPSED -gt $ARCHITECT_COOLDOWN ]]; then
        ARCHITECT_COOLDOWN_OK="true"
    fi
else
    ARCHITECT_COOLDOWN_OK="true"
fi

if [[ -n "$LAST_HERMIT_TRIGGER" ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        HERMIT_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$LAST_HERMIT_TRIGGER" "+%s" 2>/dev/null || echo "0")
    else
        HERMIT_EPOCH=$(date -d "$LAST_HERMIT_TRIGGER" "+%s" 2>/dev/null || echo "0")
    fi
    HERMIT_ELAPSED=$((NOW_EPOCH - HERMIT_EPOCH))
    if [[ $HERMIT_ELAPSED -gt $HERMIT_COOLDOWN ]]; then
        HERMIT_COOLDOWN_OK="true"
    fi
else
    HERMIT_COOLDOWN_OK="true"
fi

# Build recommended actions array
ACTIONS="[]"

# Action: promote proposals (for force mode)
if [[ $TOTAL_PROPOSALS -gt 0 ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["promote_proposals"]')
fi

# Action: spawn shepherds
if [[ $READY_COUNT -gt 0 ]] && [[ $AVAILABLE_SHEPHERD_SLOTS -gt 0 ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["spawn_shepherds"]')
fi

# Action: trigger architect
if [[ "$NEEDS_WORK_GEN" == "true" ]] && [[ "$ARCHITECT_COOLDOWN_OK" == "true" ]] && [[ $ARCHITECT_COUNT -lt 2 ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["trigger_architect"]')
fi

# Action: trigger hermit
if [[ "$NEEDS_WORK_GEN" == "true" ]] && [[ "$HERMIT_COOLDOWN_OK" == "true" ]] && [[ $HERMIT_COUNT -lt 2 ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["trigger_hermit"]')
fi

# Action: check stuck (if building issues exist for extended time)
# This is a simple heuristic - could be enhanced with timestamp checks
if [[ $BUILDING_COUNT -gt 0 ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["check_stuck"]')
fi

# Action: wait (if nothing else to do)
if [[ $(echo "$ACTIONS" | jq 'length') -eq 0 ]] || [[ $(echo "$ACTIONS" | jq 'length') -eq 1 && $(echo "$ACTIONS" | jq -r '.[0]') == "check_stuck" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["wait"]')
fi

# Build promotable proposals list (issue numbers)
PROMOTABLE_PROPOSALS=$(jq -n \
    --argjson arch "$ARCHITECT_PROPOSALS" \
    --argjson herm "$HERMIT_PROPOSALS" \
    --argjson cur "$CURATED_ISSUES" \
    '[$arch[].number, $herm[].number, $cur[].number]')

# Check usage health
USAGE_HEALTHY="true"
SESSION_PERCENT=$(echo "$USAGE" | jq -r '.session_percent // 0')
if [[ -n "$SESSION_PERCENT" ]] && [[ "$SESSION_PERCENT" != "null" ]]; then
    # Compare as integers (handle decimals)
    SESSION_INT=${SESSION_PERCENT%.*}
    if [[ $SESSION_INT -ge 97 ]]; then
        USAGE_HEALTHY="false"
    fi
fi

# Read shepherd progress files
read_shepherd_progress() {
    local progress_json="[]"

    if [[ -d "$PROGRESS_DIR" ]]; then
        for progress_file in "$PROGRESS_DIR"/shepherd-*.json; do
            if [[ -f "$progress_file" ]]; then
                # Read and validate JSON
                local content
                if content=$(cat "$progress_file" 2>/dev/null) && echo "$content" | jq -e . >/dev/null 2>&1; then
                    # Calculate time since last heartbeat
                    local last_heartbeat
                    last_heartbeat=$(echo "$content" | jq -r '.last_heartbeat // ""')

                    local heartbeat_age=-1
                    local heartbeat_stale=false

                    if [[ -n "$last_heartbeat" && "$last_heartbeat" != "null" ]]; then
                        local hb_epoch
                        if [[ "$(uname)" == "Darwin" ]]; then
                            hb_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_heartbeat" "+%s" 2>/dev/null || echo "0")
                        else
                            hb_epoch=$(date -d "$last_heartbeat" "+%s" 2>/dev/null || echo "0")
                        fi

                        if [[ "$hb_epoch" != "0" ]]; then
                            heartbeat_age=$((NOW_EPOCH - hb_epoch))
                            if [[ $heartbeat_age -gt $HEARTBEAT_STALE_THRESHOLD ]]; then
                                heartbeat_stale=true
                            fi
                        fi
                    fi

                    # Add computed fields to progress entry
                    local enhanced_content
                    enhanced_content=$(echo "$content" | jq \
                        --argjson heartbeat_age "$heartbeat_age" \
                        --argjson heartbeat_stale "$heartbeat_stale" \
                        '. + {heartbeat_age_seconds: $heartbeat_age, heartbeat_stale: $heartbeat_stale}')

                    progress_json=$(echo "$progress_json" | jq --argjson entry "$enhanced_content" '. + [$entry]')
                fi
            fi
        done
    fi

    echo "$progress_json"
}

SHEPHERD_PROGRESS=$(read_shepherd_progress)

# Count stale heartbeats for warnings
STALE_HEARTBEAT_COUNT=$(echo "$SHEPHERD_PROGRESS" | jq '[.[] | select(.heartbeat_stale == true and .status == "working")] | length')

# Build the final JSON output
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

OUTPUT=$(jq -n \
    --arg timestamp "$TIMESTAMP" \
    --argjson ready_issues "$READY_ISSUES" \
    --argjson building_issues "$BUILDING_ISSUES" \
    --argjson blocked_issues "$BLOCKED_ISSUES" \
    --argjson architect "$ARCHITECT_PROPOSALS" \
    --argjson hermit "$HERMIT_PROPOSALS" \
    --argjson curated "$CURATED_ISSUES" \
    --argjson review_requested "$REVIEW_REQUESTED" \
    --argjson changes_requested "$CHANGES_REQUESTED" \
    --argjson ready_to_merge "$READY_TO_MERGE" \
    --argjson usage "$USAGE" \
    --argjson usage_healthy "$USAGE_HEALTHY" \
    --argjson total_ready "$READY_COUNT" \
    --argjson total_building "$BUILDING_COUNT" \
    --argjson total_blocked "$BLOCKED_COUNT" \
    --argjson total_proposals "$TOTAL_PROPOSALS" \
    --argjson total_in_flight "$TOTAL_IN_FLIGHT" \
    --argjson active_shepherds "$ACTIVE_SHEPHERDS" \
    --argjson available_shepherd_slots "$AVAILABLE_SHEPHERD_SLOTS" \
    --argjson needs_work_generation "$NEEDS_WORK_GEN" \
    --argjson architect_cooldown_ok "$ARCHITECT_COOLDOWN_OK" \
    --argjson hermit_cooldown_ok "$HERMIT_COOLDOWN_OK" \
    --argjson promotable_proposals "$PROMOTABLE_PROPOSALS" \
    --argjson recommended_actions "$ACTIONS" \
    --argjson issue_threshold "$ISSUE_THRESHOLD" \
    --argjson max_shepherds "$MAX_SHEPHERDS" \
    --argjson max_proposals "$MAX_PROPOSALS" \
    --arg issue_strategy "$ISSUE_STRATEGY" \
    --argjson shepherd_progress "$SHEPHERD_PROGRESS" \
    --argjson stale_heartbeat_count "$STALE_HEARTBEAT_COUNT" \
    '{
        timestamp: $timestamp,
        pipeline: {
            ready_issues: $ready_issues,
            building_issues: $building_issues,
            blocked_issues: $blocked_issues
        },
        proposals: {
            architect: $architect,
            hermit: $hermit,
            curated: $curated
        },
        prs: {
            review_requested: $review_requested,
            changes_requested: $changes_requested,
            ready_to_merge: $ready_to_merge
        },
        shepherds: {
            progress: $shepherd_progress,
            stale_heartbeat_count: $stale_heartbeat_count
        },
        usage: ($usage + {healthy: $usage_healthy}),
        computed: {
            total_ready: $total_ready,
            total_building: $total_building,
            total_blocked: $total_blocked,
            total_proposals: $total_proposals,
            total_in_flight: $total_in_flight,
            active_shepherds: $active_shepherds,
            available_shepherd_slots: $available_shepherd_slots,
            needs_work_generation: $needs_work_generation,
            architect_cooldown_ok: $architect_cooldown_ok,
            hermit_cooldown_ok: $hermit_cooldown_ok,
            promotable_proposals: $promotable_proposals,
            recommended_actions: $recommended_actions,
            stale_heartbeat_count: $stale_heartbeat_count
        },
        config: {
            issue_threshold: $issue_threshold,
            max_shepherds: $max_shepherds,
            max_proposals: $max_proposals,
            issue_strategy: $issue_strategy
        }
    }')

# Output
if [[ "$PRETTY" == "true" ]]; then
    echo "$OUTPUT" | jq .
else
    echo "$OUTPUT" | jq -c .
fi
