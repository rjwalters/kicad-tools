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

# Support role re-trigger intervals (in seconds)
GUIDE_INTERVAL="${LOOM_GUIDE_INTERVAL:-900}"        # 15 minutes default
CHAMPION_INTERVAL="${LOOM_CHAMPION_INTERVAL:-600}"  # 10 minutes default
DOCTOR_INTERVAL="${LOOM_DOCTOR_INTERVAL:-300}"      # 5 minutes default
AUDITOR_INTERVAL="${LOOM_AUDITOR_INTERVAL:-600}"    # 10 minutes default
JUDGE_INTERVAL="${LOOM_JUDGE_INTERVAL:-300}"        # 5 minutes default

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

# tmux socket name for agent pool
TMUX_SOCKET="${LOOM_TMUX_SOCKET:-loom}"

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
    LOOM_GUIDE_INTERVAL      Guide re-trigger interval in seconds (default: 900)
    LOOM_CHAMPION_INTERVAL   Champion re-trigger interval in seconds (default: 600)
    LOOM_DOCTOR_INTERVAL     Doctor re-trigger interval in seconds (default: 300)
    LOOM_AUDITOR_INTERVAL    Auditor re-trigger interval in seconds (default: 600)
    LOOM_JUDGE_INTERVAL      Judge re-trigger interval in seconds (default: 300)
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
    - tmux_pool: tmux agent pool status (if available)
    - computed: Pre-computed decision values (includes execution_mode)
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
      "tmux_pool": {
        "available": true,
        "sessions": ["loom-shepherd-1", "loom-shepherd-2"],
        "shepherd_count": 2,
        "execution_mode": "tmux"
      },
      "computed": {
        "total_ready": 1,
        "needs_work_generation": false,
        "execution_mode": "tmux",
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

# Support role state defaults
GUIDE_LAST_COMPLETED=""
GUIDE_STATUS="idle"
CHAMPION_LAST_COMPLETED=""
CHAMPION_STATUS="idle"
DOCTOR_LAST_COMPLETED=""
DOCTOR_STATUS="idle"
AUDITOR_LAST_COMPLETED=""
AUDITOR_STATUS="idle"
JUDGE_LAST_COMPLETED=""
JUDGE_STATUS="idle"

if [[ -f "$DAEMON_STATE_FILE" ]]; then
    # Count active shepherds (those with status="working")
    ACTIVE_SHEPHERDS=$(jq -r '[.shepherds // {} | to_entries[] | select(.value.status == "working")] | length' "$DAEMON_STATE_FILE" 2>/dev/null || echo "0")
    LAST_ARCHITECT_TRIGGER=$(jq -r '.last_architect_trigger // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
    LAST_HERMIT_TRIGGER=$(jq -r '.last_hermit_trigger // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")

    # Read support role last_completed timestamps and statuses
    GUIDE_LAST_COMPLETED=$(jq -r '.support_roles.guide.last_completed // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
    GUIDE_STATUS=$(jq -r '.support_roles.guide.status // "idle"' "$DAEMON_STATE_FILE" 2>/dev/null || echo "idle")
    CHAMPION_LAST_COMPLETED=$(jq -r '.support_roles.champion.last_completed // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
    CHAMPION_STATUS=$(jq -r '.support_roles.champion.status // "idle"' "$DAEMON_STATE_FILE" 2>/dev/null || echo "idle")
    DOCTOR_LAST_COMPLETED=$(jq -r '.support_roles.doctor.last_completed // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
    DOCTOR_STATUS=$(jq -r '.support_roles.doctor.status // "idle"' "$DAEMON_STATE_FILE" 2>/dev/null || echo "idle")
    AUDITOR_LAST_COMPLETED=$(jq -r '.support_roles.auditor.last_completed // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
    AUDITOR_STATUS=$(jq -r '.support_roles.auditor.status // "idle"' "$DAEMON_STATE_FILE" 2>/dev/null || echo "idle")
    JUDGE_LAST_COMPLETED=$(jq -r '.support_roles.judge.last_completed // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
    JUDGE_STATUS=$(jq -r '.support_roles.judge.status // "idle"' "$DAEMON_STATE_FILE" 2>/dev/null || echo "idle")
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

# Calculate support role idle times
GUIDE_IDLE_SECONDS=0
GUIDE_NEEDS_TRIGGER="false"
if [[ -n "$GUIDE_LAST_COMPLETED" && "$GUIDE_LAST_COMPLETED" != "null" ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        GUIDE_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$GUIDE_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    else
        GUIDE_EPOCH=$(date -d "$GUIDE_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    fi
    if [[ "$GUIDE_EPOCH" != "0" ]]; then
        GUIDE_IDLE_SECONDS=$((NOW_EPOCH - GUIDE_EPOCH))
        # Only trigger if not currently running and idle > interval
        if [[ "$GUIDE_STATUS" != "running" ]] && [[ $GUIDE_IDLE_SECONDS -gt $GUIDE_INTERVAL ]]; then
            GUIDE_NEEDS_TRIGGER="true"
        fi
    fi
elif [[ "$GUIDE_STATUS" != "running" ]]; then
    # No last_completed means never run - needs trigger
    GUIDE_NEEDS_TRIGGER="true"
fi

CHAMPION_IDLE_SECONDS=0
CHAMPION_NEEDS_TRIGGER="false"
if [[ -n "$CHAMPION_LAST_COMPLETED" && "$CHAMPION_LAST_COMPLETED" != "null" ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        CHAMPION_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$CHAMPION_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    else
        CHAMPION_EPOCH=$(date -d "$CHAMPION_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    fi
    if [[ "$CHAMPION_EPOCH" != "0" ]]; then
        CHAMPION_IDLE_SECONDS=$((NOW_EPOCH - CHAMPION_EPOCH))
        if [[ "$CHAMPION_STATUS" != "running" ]] && [[ $CHAMPION_IDLE_SECONDS -gt $CHAMPION_INTERVAL ]]; then
            CHAMPION_NEEDS_TRIGGER="true"
        fi
    fi
elif [[ "$CHAMPION_STATUS" != "running" ]]; then
    CHAMPION_NEEDS_TRIGGER="true"
fi

DOCTOR_IDLE_SECONDS=0
DOCTOR_NEEDS_TRIGGER="false"
if [[ -n "$DOCTOR_LAST_COMPLETED" && "$DOCTOR_LAST_COMPLETED" != "null" ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        DOCTOR_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$DOCTOR_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    else
        DOCTOR_EPOCH=$(date -d "$DOCTOR_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    fi
    if [[ "$DOCTOR_EPOCH" != "0" ]]; then
        DOCTOR_IDLE_SECONDS=$((NOW_EPOCH - DOCTOR_EPOCH))
        if [[ "$DOCTOR_STATUS" != "running" ]] && [[ $DOCTOR_IDLE_SECONDS -gt $DOCTOR_INTERVAL ]]; then
            DOCTOR_NEEDS_TRIGGER="true"
        fi
    fi
elif [[ "$DOCTOR_STATUS" != "running" ]]; then
    DOCTOR_NEEDS_TRIGGER="true"
fi

AUDITOR_IDLE_SECONDS=0
AUDITOR_NEEDS_TRIGGER="false"
if [[ -n "$AUDITOR_LAST_COMPLETED" && "$AUDITOR_LAST_COMPLETED" != "null" ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        AUDITOR_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$AUDITOR_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    else
        AUDITOR_EPOCH=$(date -d "$AUDITOR_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    fi
    if [[ "$AUDITOR_EPOCH" != "0" ]]; then
        AUDITOR_IDLE_SECONDS=$((NOW_EPOCH - AUDITOR_EPOCH))
        if [[ "$AUDITOR_STATUS" != "running" ]] && [[ $AUDITOR_IDLE_SECONDS -gt $AUDITOR_INTERVAL ]]; then
            AUDITOR_NEEDS_TRIGGER="true"
        fi
    fi
elif [[ "$AUDITOR_STATUS" != "running" ]]; then
    AUDITOR_NEEDS_TRIGGER="true"
fi

JUDGE_IDLE_SECONDS=0
JUDGE_NEEDS_TRIGGER="false"
if [[ -n "$JUDGE_LAST_COMPLETED" && "$JUDGE_LAST_COMPLETED" != "null" ]]; then
    if [[ "$(uname)" == "Darwin" ]]; then
        JUDGE_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$JUDGE_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    else
        JUDGE_EPOCH=$(date -d "$JUDGE_LAST_COMPLETED" "+%s" 2>/dev/null || echo "0")
    fi
    if [[ "$JUDGE_EPOCH" != "0" ]]; then
        JUDGE_IDLE_SECONDS=$((NOW_EPOCH - JUDGE_EPOCH))
        if [[ "$JUDGE_STATUS" != "running" ]] && [[ $JUDGE_IDLE_SECONDS -gt $JUDGE_INTERVAL ]]; then
            JUDGE_NEEDS_TRIGGER="true"
        fi
    fi
elif [[ "$JUDGE_STATUS" != "running" ]]; then
    JUDGE_NEEDS_TRIGGER="true"
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

# Action: demand-based spawning (spawn roles immediately when work exists)
# These take priority over interval-based triggers for faster response
CHAMPION_DEMAND="false"
DOCTOR_DEMAND="false"
JUDGE_DEMAND="false"

# Spawn Champion on-demand if PRs are ready to merge and Champion not running
if [[ "$MERGE_COUNT" -gt 0 ]] && [[ "$CHAMPION_STATUS" != "running" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["spawn_champion_demand"]')
    CHAMPION_DEMAND="true"
fi

# Spawn Doctor on-demand if PRs need fixes and Doctor not running
if [[ "$CHANGES_COUNT" -gt 0 ]] && [[ "$DOCTOR_STATUS" != "running" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["spawn_doctor_demand"]')
    DOCTOR_DEMAND="true"
fi

# Spawn Judge on-demand if PRs need review and Judge not running
if [[ "$REVIEW_COUNT" -gt 0 ]] && [[ "$JUDGE_STATUS" != "running" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["spawn_judge_demand"]')
    JUDGE_DEMAND="true"
fi

# Action: trigger support roles when idle > interval (interval-based fallback)
# Skip interval-based trigger if demand-based trigger will handle it
if [[ "$GUIDE_NEEDS_TRIGGER" == "true" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["trigger_guide"]')
fi
if [[ "$CHAMPION_NEEDS_TRIGGER" == "true" ]] && [[ "$CHAMPION_DEMAND" == "false" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["trigger_champion"]')
fi
if [[ "$DOCTOR_NEEDS_TRIGGER" == "true" ]] && [[ "$DOCTOR_DEMAND" == "false" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["trigger_doctor"]')
fi
if [[ "$AUDITOR_NEEDS_TRIGGER" == "true" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["trigger_auditor"]')
fi
if [[ "$JUDGE_NEEDS_TRIGGER" == "true" ]] && [[ "$JUDGE_DEMAND" == "false" ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["trigger_judge"]')
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

# Detect tmux agent pool status
detect_tmux_pool() {
    local pool_json='{"available": false, "sessions": [], "shepherd_count": 0, "total_count": 0, "execution_mode": "direct"}'

    # Check if tmux server is running with loom socket
    if tmux -L "$TMUX_SOCKET" has-session 2>/dev/null; then
        local sessions
        sessions=$(tmux -L "$TMUX_SOCKET" list-sessions -F '#{session_name}' 2>/dev/null || true)

        if [[ -n "$sessions" ]]; then
            local session_array="[]"
            local shepherd_count=0
            local total_count=0

            while IFS= read -r session; do
                if [[ -n "$session" ]]; then
                    session_array=$(echo "$session_array" | jq --arg s "$session" '. + [$s]')
                    ((total_count++))
                    if [[ "$session" == *"shepherd"* ]]; then
                        ((shepherd_count++))
                    fi
                fi
            done <<< "$sessions"

            # Determine execution mode
            local exec_mode="direct"
            if [[ $shepherd_count -gt 0 ]]; then
                exec_mode="tmux"
            fi

            pool_json=$(jq -n \
                --argjson available "true" \
                --argjson sessions "$session_array" \
                --argjson shepherd_count "$shepherd_count" \
                --argjson total_count "$total_count" \
                --arg execution_mode "$exec_mode" \
                '{
                    available: $available,
                    sessions: $sessions,
                    shepherd_count: $shepherd_count,
                    total_count: $total_count,
                    execution_mode: $execution_mode
                }')
        fi
    fi

    echo "$pool_json"
}

TMUX_POOL=$(detect_tmux_pool)
TMUX_AVAILABLE=$(echo "$TMUX_POOL" | jq -r '.available')
TMUX_SHEPHERD_COUNT=$(echo "$TMUX_POOL" | jq -r '.shepherd_count')
TMUX_EXECUTION_MODE=$(echo "$TMUX_POOL" | jq -r '.execution_mode')

# Count stale heartbeats for warnings
STALE_HEARTBEAT_COUNT=$(echo "$SHEPHERD_PROGRESS" | jq '[.[] | select(.heartbeat_stale == true and .status == "working")] | length')

# Detect orphaned shepherds
# An orphaned shepherd is:
# 1. A shepherd in daemon-state with status="working" but the issue has loom:building removed
# 2. An issue with loom:building that's not tracked in any shepherd's assignment
# 3. A progress file with stale heartbeat and no corresponding daemon-state entry
detect_orphaned_shepherds() {
    local orphaned_json="[]"

    # Get tracked issues from daemon-state
    local daemon_tracked_issues="[]"
    local daemon_shepherd_task_ids="[]"
    if [[ -f "$DAEMON_STATE_FILE" ]]; then
        daemon_tracked_issues=$(jq '[.shepherds // {} | to_entries[] | select(.value.status == "working") | .value.issue] | map(select(. != null))' "$DAEMON_STATE_FILE" 2>/dev/null || echo "[]")
        daemon_shepherd_task_ids=$(jq '[.shepherds // {} | to_entries[] | select(.value.status == "working") | .value.task_id] | map(select(. != null))' "$DAEMON_STATE_FILE" 2>/dev/null || echo "[]")
    fi

    # Check 1: loom:building issues not tracked in daemon-state
    local building_numbers
    building_numbers=$(echo "$BUILDING_ISSUES" | jq -r '.[].number')
    for issue_num in $building_numbers; do
        [[ -z "$issue_num" ]] && continue

        local is_tracked
        is_tracked=$(echo "$daemon_tracked_issues" | jq --argjson num "$issue_num" 'any(. == $num)')

        if [[ "$is_tracked" == "false" ]]; then
            # Check if there's an active progress file for this issue
            local has_active_progress=false
            for progress in $(echo "$SHEPHERD_PROGRESS" | jq -c '.[]'); do
                local p_issue
                p_issue=$(echo "$progress" | jq -r '.issue')
                local p_status
                p_status=$(echo "$progress" | jq -r '.status')
                local p_stale
                p_stale=$(echo "$progress" | jq -r '.heartbeat_stale')

                if [[ "$p_issue" == "$issue_num" && "$p_status" == "working" && "$p_stale" == "false" ]]; then
                    has_active_progress=true
                    break
                fi
            done

            if [[ "$has_active_progress" == "false" ]]; then
                orphaned_json=$(echo "$orphaned_json" | jq --argjson issue "$issue_num" \
                    '. + [{type: "untracked_building", issue: $issue, reason: "no_daemon_entry"}]')
            fi
        fi
    done

    # Check 2: Progress files with stale heartbeats
    for progress in $(echo "$SHEPHERD_PROGRESS" | jq -c '.[] | select(.status == "working" and .heartbeat_stale == true)'); do
        local task_id
        task_id=$(echo "$progress" | jq -r '.task_id')
        local issue
        issue=$(echo "$progress" | jq -r '.issue')
        local age
        age=$(echo "$progress" | jq -r '.heartbeat_age_seconds')

        orphaned_json=$(echo "$orphaned_json" | jq \
            --arg task_id "$task_id" \
            --argjson issue "$issue" \
            --argjson age "$age" \
            '. + [{type: "stale_heartbeat", task_id: $task_id, issue: $issue, age_seconds: $age, reason: "heartbeat_stale"}]')
    done

    echo "$orphaned_json"
}

ORPHANED_SHEPHERDS=$(detect_orphaned_shepherds)
ORPHANED_COUNT=$(echo "$ORPHANED_SHEPHERDS" | jq 'length')

# Add recover_orphans action if any orphans detected
if [[ "$ORPHANED_COUNT" -gt 0 ]]; then
    ACTIONS=$(echo "$ACTIONS" | jq '. + ["recover_orphans"]')
fi

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
    --argjson guide_idle_seconds "$GUIDE_IDLE_SECONDS" \
    --argjson guide_interval "$GUIDE_INTERVAL" \
    --argjson guide_needs_trigger "$GUIDE_NEEDS_TRIGGER" \
    --arg guide_status "$GUIDE_STATUS" \
    --argjson champion_idle_seconds "$CHAMPION_IDLE_SECONDS" \
    --argjson champion_interval "$CHAMPION_INTERVAL" \
    --argjson champion_needs_trigger "$CHAMPION_NEEDS_TRIGGER" \
    --arg champion_status "$CHAMPION_STATUS" \
    --argjson doctor_idle_seconds "$DOCTOR_IDLE_SECONDS" \
    --argjson doctor_interval "$DOCTOR_INTERVAL" \
    --argjson doctor_needs_trigger "$DOCTOR_NEEDS_TRIGGER" \
    --arg doctor_status "$DOCTOR_STATUS" \
    --argjson auditor_idle_seconds "$AUDITOR_IDLE_SECONDS" \
    --argjson auditor_interval "$AUDITOR_INTERVAL" \
    --argjson auditor_needs_trigger "$AUDITOR_NEEDS_TRIGGER" \
    --arg auditor_status "$AUDITOR_STATUS" \
    --argjson orphaned_shepherds "$ORPHANED_SHEPHERDS" \
    --argjson orphaned_count "$ORPHANED_COUNT" \
    --argjson champion_demand "$CHAMPION_DEMAND" \
    --argjson doctor_demand "$DOCTOR_DEMAND" \
    --argjson judge_idle_seconds "$JUDGE_IDLE_SECONDS" \
    --argjson judge_interval "$JUDGE_INTERVAL" \
    --argjson judge_needs_trigger "$JUDGE_NEEDS_TRIGGER" \
    --arg judge_status "$JUDGE_STATUS" \
    --argjson judge_demand "$JUDGE_DEMAND" \
    --argjson review_requested_count "$REVIEW_COUNT" \
    --argjson changes_requested_count "$CHANGES_COUNT" \
    --argjson ready_to_merge_count "$MERGE_COUNT" \
    --argjson tmux_pool "$TMUX_POOL" \
    --argjson tmux_available "$TMUX_AVAILABLE" \
    --argjson tmux_shepherd_count "$TMUX_SHEPHERD_COUNT" \
    --arg tmux_execution_mode "$TMUX_EXECUTION_MODE" \
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
            stale_heartbeat_count: $stale_heartbeat_count,
            orphaned: $orphaned_shepherds,
            orphaned_count: $orphaned_count
        },
        support_roles: {
            guide: {
                status: $guide_status,
                idle_seconds: $guide_idle_seconds,
                interval: $guide_interval,
                needs_trigger: $guide_needs_trigger
            },
            champion: {
                status: $champion_status,
                idle_seconds: $champion_idle_seconds,
                interval: $champion_interval,
                needs_trigger: $champion_needs_trigger,
                demand_trigger: $champion_demand
            },
            doctor: {
                status: $doctor_status,
                idle_seconds: $doctor_idle_seconds,
                interval: $doctor_interval,
                needs_trigger: $doctor_needs_trigger,
                demand_trigger: $doctor_demand
            },
            auditor: {
                status: $auditor_status,
                idle_seconds: $auditor_idle_seconds,
                interval: $auditor_interval,
                needs_trigger: $auditor_needs_trigger
            },
            judge: {
                status: $judge_status,
                idle_seconds: $judge_idle_seconds,
                interval: $judge_interval,
                needs_trigger: $judge_needs_trigger,
                demand_trigger: $judge_demand
            }
        },
        usage: ($usage + {healthy: $usage_healthy}),
        tmux_pool: $tmux_pool,
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
            stale_heartbeat_count: $stale_heartbeat_count,
            orphaned_count: $orphaned_count,
            prs_awaiting_review: $review_requested_count,
            prs_needing_fixes: $changes_requested_count,
            prs_ready_to_merge: $ready_to_merge_count,
            champion_demand: $champion_demand,
            doctor_demand: $doctor_demand,
            judge_demand: $judge_demand,
            execution_mode: $tmux_execution_mode,
            tmux_available: $tmux_available,
            tmux_shepherd_count: $tmux_shepherd_count
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
