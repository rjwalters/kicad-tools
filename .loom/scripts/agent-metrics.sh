#!/bin/bash

# agent-metrics.sh - Agent performance metrics for self-aware agents
#
# Enables agents to query their own performance metrics for informed decisions.
# Part of Phase 5 (Autonomous Learning) - Issue #1073.
#
# Usage:
#   agent-metrics.sh [--role ROLE] [--period PERIOD] [--format FORMAT]
#   agent-metrics.sh summary
#   agent-metrics.sh effectiveness [--role ROLE]
#   agent-metrics.sh costs [--issue NUMBER]
#   agent-metrics.sh velocity
#   agent-metrics.sh --help
#
# Options:
#   --role ROLE       Filter by agent role (builder, judge, curator, etc.)
#   --period PERIOD   Time period: today, week, month, all (default: week)
#   --format FORMAT   Output format: text, json (default: text)
#
# Examples:
#   # Get my metrics as a builder
#   ./.loom/scripts/agent-metrics.sh --role builder
#
#   # Check cost for a specific issue
#   ./.loom/scripts/agent-metrics.sh costs --issue 123
#
#   # Get JSON output for programmatic use
#   ./.loom/scripts/agent-metrics.sh summary --format json

set -euo pipefail

# Colors for output (not all colors used in every script)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
# shellcheck disable=SC2034
CYAN='\033[0;36m'
GRAY='\033[0;90m'
NC='\033[0m' # No Color

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
ACTIVITY_DB="${LOOM_ACTIVITY_DB:-$HOME/.loom/activity.db}"
DAEMON_STATE="$REPO_ROOT/.loom/daemon-state.json"

# Default options
ROLE=""
PERIOD="week"
FORMAT="text"
ISSUE_NUMBER=""

# Show help
show_help() {
    cat <<EOF
${BLUE}agent-metrics.sh - Agent performance metrics for self-aware agents${NC}

${YELLOW}SYNOPSIS${NC}
    agent-metrics.sh [COMMAND] [OPTIONS]

${YELLOW}COMMANDS${NC}
    summary             Show overall metrics summary (default)
    effectiveness       Show agent effectiveness by role
    costs               Show cost breakdown by issue or role
    velocity            Show development velocity trends

${YELLOW}OPTIONS${NC}
    --role ROLE         Filter by agent role (builder, judge, curator, architect,
                        hermit, doctor, guide, champion, shepherd)
    --period PERIOD     Time period: today, week, month, all (default: week)
    --format FORMAT     Output format: text, json (default: text)
    --issue NUMBER      Filter by issue number (for costs command)
    --help              Show this help message

${YELLOW}EXAMPLES${NC}
    # Get my metrics as a builder
    ./.loom/scripts/agent-metrics.sh --role builder

    # Get effectiveness metrics for all roles
    ./.loom/scripts/agent-metrics.sh effectiveness

    # Check cost for issue #123
    ./.loom/scripts/agent-metrics.sh costs --issue 123

    # Get velocity trends
    ./.loom/scripts/agent-metrics.sh velocity

    # JSON output for programmatic use
    ./.loom/scripts/agent-metrics.sh summary --format json

${YELLOW}METRICS AVAILABLE${NC}
    ${GREEN}Summary:${NC}
    - Total prompts sent
    - Total tokens used
    - Estimated cost (USD)
    - Issues worked on
    - PRs created/merged
    - Average success rate

    ${GREEN}Effectiveness (per role):${NC}
    - Total prompts
    - Successful prompts
    - Success rate (%)
    - Average cost per prompt
    - Average duration

    ${GREEN}Costs:${NC}
    - Cost per issue
    - Tokens per issue
    - Time spent per issue

    ${GREEN}Velocity:${NC}
    - Issues closed per day/week
    - PRs merged per day/week
    - Average cycle time

${YELLOW}USE CASES FOR AGENTS${NC}
    1. Check if struggling with a task type:
       agent-metrics.sh effectiveness --role builder

    2. Select approach based on historical success:
       agent-metrics.sh --role builder --format json

    3. Decide to escalate if below threshold:
       success_rate=\$(agent-metrics.sh --role builder --format json | jq '.success_rate')
       if (( \$(echo "\$success_rate < 70" | bc -l) )); then
           echo "Consider escalating - success rate below threshold"
       fi

${YELLOW}DATA SOURCE${NC}
    Metrics are read from:
    - Activity database: ~/.loom/activity.db (if available)
    - Daemon state: .loom/daemon-state.json (for completed counts)
    - GitHub API: Issue/PR counts via gh CLI

${YELLOW}NOTES${NC}
    - Metrics are read-only (agents cannot modify their own metrics)
    - Historical data requires activity tracking to be enabled
    - Success rate is based on test outcomes and PR approvals
EOF
}

# Check if activity database exists
check_activity_db() {
    if [[ ! -f "$ACTIVITY_DB" ]]; then
        return 1
    fi
    return 0
}

# Query activity database
query_db() {
    local sql="$1"
    if ! check_activity_db; then
        echo "[]"
        return 0
    fi
    sqlite3 -json "$ACTIVITY_DB" "$sql" 2>/dev/null || echo "[]"
}

# Get period filter for SQL
get_period_filter() {
    local period="$1"
    case "$period" in
        today)
            echo "AND timestamp >= datetime('now', 'start of day')"
            ;;
        week)
            echo "AND timestamp >= datetime('now', '-7 days')"
            ;;
        month)
            echo "AND timestamp >= datetime('now', '-30 days')"
            ;;
        all|*)
            echo ""
            ;;
    esac
}

# Get summary metrics
get_summary() {
    local role_filter=""
    local period_filter=$(get_period_filter "$PERIOD")

    if [[ -n "$ROLE" ]]; then
        role_filter="AND agent_role = '$ROLE'"
    fi

    if check_activity_db; then
        # Query from activity database
        local result=$(sqlite3 -json "$ACTIVITY_DB" "
            SELECT
                COUNT(*) as total_prompts,
                COALESCE(SUM(r.tokens_input + r.tokens_output), 0) as total_tokens,
                ROUND(COALESCE(SUM(r.cost_usd), 0), 4) as total_cost,
                COUNT(DISTINCT pg.issue_number) as issues_count,
                COUNT(DISTINCT pg.pr_number) as prs_count
            FROM agent_inputs i
            LEFT JOIN resource_usage r ON i.id = r.input_id
            LEFT JOIN prompt_github pg ON i.id = pg.input_id
            WHERE 1=1 $role_filter $period_filter
        " 2>/dev/null || echo '[{"total_prompts":0,"total_tokens":0,"total_cost":0,"issues_count":0,"prs_count":0}]')

        # Get success rate
        local success_rate=$(sqlite3 "$ACTIVITY_DB" "
            SELECT ROUND(100.0 * SUM(CASE WHEN q.tests_passed > 0 AND (q.tests_failed IS NULL OR q.tests_failed = 0) THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1)
            FROM agent_inputs i
            LEFT JOIN quality_metrics q ON i.id = q.input_id
            WHERE 1=1 $role_filter $period_filter
        " 2>/dev/null || echo "0")

        if [[ "$FORMAT" == "json" ]]; then
            echo "$result" | jq --argjson sr "${success_rate:-0}" '.[0] + {success_rate: $sr}'
        else
            echo "$result" | jq -r '.[0] | "
'"${BLUE}"'Agent Performance Summary'"${NC}"' ('"$PERIOD"')
'"${GRAY}"'────────────────────────────────────────'"${NC}"'
  Total Prompts:   \(.total_prompts)
  Total Tokens:    \(.total_tokens | . / 1000 | floor)K
  Total Cost:      $\(.total_cost)
  Issues Worked:   \(.issues_count)
  PRs Created:     \(.prs_count)
  Success Rate:    '"${success_rate:-0}"'%
"'
        fi
    else
        # Fallback: Use daemon state and GitHub
        local completed_count=0
        local total_prs=0

        if [[ -f "$DAEMON_STATE" ]]; then
            completed_count=$(jq '.completed_issues | length' "$DAEMON_STATE" 2>/dev/null || echo 0)
            total_prs=$(jq '.total_prs_merged // 0' "$DAEMON_STATE" 2>/dev/null || echo 0)
        fi

        local open_issues=$(gh issue list --state open --json number --jq 'length' 2>/dev/null || echo 0)
        local open_prs=$(gh pr list --state open --json number --jq 'length' 2>/dev/null || echo 0)

        if [[ "$FORMAT" == "json" ]]; then
            jq -n \
                --argjson completed "$completed_count" \
                --argjson prs "$total_prs" \
                --argjson open_issues "$open_issues" \
                --argjson open_prs "$open_prs" \
                '{
                    completed_issues: $completed,
                    total_prs_merged: $prs,
                    open_issues: $open_issues,
                    open_prs: $open_prs,
                    note: "Limited data - activity database not available"
                }'
        else
            echo -e "${BLUE}Agent Performance Summary${NC} (daemon state)"
            echo -e "${GRAY}────────────────────────────────────────${NC}"
            echo "  Completed Issues: $completed_count"
            echo "  PRs Merged:       $total_prs"
            echo "  Open Issues:      $open_issues"
            echo "  Open PRs:         $open_prs"
            echo ""
            echo -e "${YELLOW}Note: Activity database not available. Enable tracking for detailed metrics.${NC}"
        fi
    fi
}

# Get effectiveness metrics by role
get_effectiveness() {
    local role_filter=""
    local period_filter=$(get_period_filter "$PERIOD")

    if [[ -n "$ROLE" ]]; then
        role_filter="AND agent_role = '$ROLE'"
    fi

    if ! check_activity_db; then
        if [[ "$FORMAT" == "json" ]]; then
            echo '{"error": "Activity database not available", "roles": []}'
        else
            echo -e "${YELLOW}Activity database not available. Enable tracking for effectiveness metrics.${NC}"
        fi
        return 0
    fi

    local result=$(sqlite3 -json "$ACTIVITY_DB" "
        SELECT
            COALESCE(i.agent_role, 'unknown') as role,
            COUNT(*) as total_prompts,
            SUM(CASE WHEN q.tests_passed > 0 AND (q.tests_failed IS NULL OR q.tests_failed = 0) THEN 1 ELSE 0 END) as successful_prompts,
            ROUND(100.0 * SUM(CASE WHEN q.tests_passed > 0 AND (q.tests_failed IS NULL OR q.tests_failed = 0) THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 1) as success_rate,
            ROUND(COALESCE(AVG(r.cost_usd), 0), 4) as avg_cost,
            ROUND(COALESCE(AVG(r.duration_ms / 1000.0), 0), 1) as avg_duration_sec
        FROM agent_inputs i
        LEFT JOIN quality_metrics q ON i.id = q.input_id
        LEFT JOIN resource_usage r ON i.id = r.input_id
        WHERE 1=1 $role_filter $period_filter
        GROUP BY COALESCE(i.agent_role, 'unknown')
        ORDER BY success_rate DESC
    " 2>/dev/null || echo "[]")

    if [[ "$FORMAT" == "json" ]]; then
        echo "$result"
    else
        echo -e "${BLUE}Agent Effectiveness by Role${NC} ($PERIOD)"
        echo -e "${GRAY}────────────────────────────────────────────────────────────────────${NC}"
        printf "%-12s %10s %10s %10s %10s %10s\n" "Role" "Prompts" "Success" "Rate" "Avg Cost" "Avg Time"
        echo -e "${GRAY}────────────────────────────────────────────────────────────────────${NC}"

        echo "$result" | jq -r '.[] | [.role, .total_prompts, .successful_prompts, (.success_rate // 0), .avg_cost, .avg_duration_sec] | @tsv' | while IFS=$'\t' read -r role prompts success rate cost duration; do
            # Color code success rate
            local rate_color="${RED}"
            if (( $(echo "$rate >= 90" | bc -l 2>/dev/null || echo 0) )); then
                rate_color="${GREEN}"
            elif (( $(echo "$rate >= 70" | bc -l 2>/dev/null || echo 0) )); then
                rate_color="${YELLOW}"
            fi
            printf "%-12s %10s %10s ${rate_color}%9s%%${NC} %10s %9ss\n" \
                "$role" "$prompts" "$success" "$rate" "\$$cost" "$duration"
        done
    fi
}

# Get cost breakdown
get_costs() {
    local issue_filter=""

    if [[ -n "$ISSUE_NUMBER" ]]; then
        issue_filter="WHERE pg.issue_number = $ISSUE_NUMBER"
    fi

    if ! check_activity_db; then
        if [[ "$FORMAT" == "json" ]]; then
            echo '{"error": "Activity database not available", "costs": []}'
        else
            echo -e "${YELLOW}Activity database not available. Enable tracking for cost metrics.${NC}"
        fi
        return 0
    fi

    local result=$(sqlite3 -json "$ACTIVITY_DB" "
        SELECT
            pg.issue_number,
            COUNT(DISTINCT i.id) as prompt_count,
            ROUND(COALESCE(SUM(r.cost_usd), 0), 4) as total_cost,
            COALESCE(SUM(r.tokens_input + r.tokens_output), 0) as total_tokens,
            MIN(i.timestamp) as started,
            MAX(i.timestamp) as completed
        FROM prompt_github pg
        JOIN agent_inputs i ON pg.input_id = i.id
        LEFT JOIN resource_usage r ON i.id = r.input_id
        $issue_filter
        GROUP BY pg.issue_number
        ORDER BY total_cost DESC
        LIMIT 20
    " 2>/dev/null || echo "[]")

    if [[ "$FORMAT" == "json" ]]; then
        echo "$result"
    else
        echo -e "${BLUE}Cost Breakdown by Issue${NC}"
        echo -e "${GRAY}────────────────────────────────────────────────────────────────────${NC}"
        printf "%-8s %10s %12s %12s\n" "Issue" "Prompts" "Cost" "Tokens"
        echo -e "${GRAY}────────────────────────────────────────────────────────────────────${NC}"

        echo "$result" | jq -r '.[] | select(.issue_number != null) | [.issue_number, .prompt_count, .total_cost, .total_tokens] | @tsv' | while IFS=$'\t' read -r issue prompts cost tokens; do
            printf "#%-7s %10s %11s %12s\n" "$issue" "$prompts" "\$$cost" "$tokens"
        done
    fi
}

# Get velocity metrics
get_velocity() {
    if ! check_activity_db; then
        # Fallback: Use daemon state
        if [[ -f "$DAEMON_STATE" ]]; then
            local completed=$(jq '.completed_issues | length' "$DAEMON_STATE" 2>/dev/null || echo 0)
            local prs=$(jq '.total_prs_merged // 0' "$DAEMON_STATE" 2>/dev/null || echo 0)
            local started=$(jq -r '.started_at // empty' "$DAEMON_STATE" 2>/dev/null || echo "")

            if [[ "$FORMAT" == "json" ]]; then
                jq -n \
                    --argjson completed "$completed" \
                    --argjson prs "$prs" \
                    --arg started "$started" \
                    '{
                        completed_issues: $completed,
                        prs_merged: $prs,
                        session_started: $started,
                        note: "Velocity from daemon state (limited data)"
                    }'
            else
                echo -e "${BLUE}Development Velocity${NC} (daemon state)"
                echo -e "${GRAY}────────────────────────────────────────${NC}"
                echo "  Issues Completed: $completed"
                echo "  PRs Merged:       $prs"
                if [[ -n "$started" ]]; then
                    echo "  Session Started:  $started"
                fi
            fi
        else
            if [[ "$FORMAT" == "json" ]]; then
                echo '{"error": "No velocity data available"}'
            else
                echo -e "${YELLOW}No velocity data available.${NC}"
            fi
        fi
        return 0
    fi

    local result=$(sqlite3 -json "$ACTIVITY_DB" "
        SELECT
            strftime('%Y-W%W', timestamp) as week,
            COUNT(*) as prompts,
            COUNT(DISTINCT pg.issue_number) as issues,
            COUNT(DISTINCT CASE WHEN pg.event_type = 'pr_merged' THEN pg.pr_number END) as prs_merged,
            ROUND(SUM(r.cost_usd), 2) as cost
        FROM agent_inputs i
        LEFT JOIN prompt_github pg ON i.id = pg.input_id
        LEFT JOIN resource_usage r ON i.id = r.input_id
        WHERE timestamp >= datetime('now', '-8 weeks')
        GROUP BY week
        ORDER BY week DESC
        LIMIT 8
    " 2>/dev/null || echo "[]")

    if [[ "$FORMAT" == "json" ]]; then
        echo "$result"
    else
        echo -e "${BLUE}Development Velocity (Last 8 Weeks)${NC}"
        echo -e "${GRAY}────────────────────────────────────────────────────────────────────${NC}"
        printf "%-10s %10s %10s %10s %10s\n" "Week" "Prompts" "Issues" "PRs" "Cost"
        echo -e "${GRAY}────────────────────────────────────────────────────────────────────${NC}"

        echo "$result" | jq -r '.[] | [.week, .prompts, (.issues // 0), (.prs_merged // 0), (.cost // 0)] | @tsv' | while IFS=$'\t' read -r week prompts issues prs cost; do
            printf "%-10s %10s %10s %10s %9s\n" "$week" "$prompts" "$issues" "$prs" "\$$cost"
        done
    fi
}

# Parse command line arguments
COMMAND="summary"
while [[ $# -gt 0 ]]; do
    case $1 in
        summary|effectiveness|costs|velocity)
            COMMAND="$1"
            shift
            ;;
        --role)
            ROLE="$2"
            shift 2
            ;;
        --period)
            PERIOD="$2"
            shift 2
            ;;
        --format)
            FORMAT="$2"
            shift 2
            ;;
        --issue)
            ISSUE_NUMBER="$2"
            shift 2
            ;;
        --help|-h|help)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
            echo "Run 'agent-metrics.sh --help' for usage" >&2
            exit 1
            ;;
    esac
done

# Execute command
case "$COMMAND" in
    summary)
        get_summary
        ;;
    effectiveness)
        get_effectiveness
        ;;
    costs)
        get_costs
        ;;
    velocity)
        get_velocity
        ;;
    *)
        echo -e "${RED}Error: Unknown command '$COMMAND'${NC}" >&2
        exit 1
        ;;
esac
