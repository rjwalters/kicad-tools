#!/bin/bash

# test-plan-metrics.sh - Test plan execution metrics from Judge PR reviews
#
# Extracts and aggregates test plan execution data from Judge review comments
# on merged PRs. The Judge documents test execution using a structured format
# with emoji markers (✅ executed, ⚠️ observation-only, ⏭️ skipped).
#
# Metric Definitions:
#   prs_analyzed:         Total PRs examined in the period
#   prs_with_test_plan:   PRs where the Judge comment contains "## Test Execution"
#   prs_without_test_plan: PRs with no Judge test execution section
#   prs_not_yet_reviewed: PRs with test plan in description but no Judge review
#   total_steps:          Total test plan steps across all PRs
#   steps_executed:       Steps marked ✅ (run and result recorded)
#   steps_skipped:        Steps marked ⚠️ or ⏭️ (not executed)
#   skip_observation:     Steps marked ⚠️ (requires manual/visual verification)
#   skip_long_running:    Steps marked ⏭️ with "long-running" in reason
#   skip_external:        Steps marked ⏭️ with "external" or "service" in reason
#   skip_other:           Steps marked ⏭️ without specific category match
#   steps_unknown:        Steps with no recognized execution marker
#   execution_rate:       Percentage of steps that were executed (steps_executed / total_steps)
#
# Usage:
#   test-plan-metrics.sh [OPTIONS]
#   test-plan-metrics.sh --help
#
# Options:
#   --period PERIOD   Time period: today, week, month, all (default: week)
#   --format FORMAT   Output format: text, json (default: text)
#   --limit N         Max PRs to analyze (default: 50)
#   --help            Show this help message
#
# Examples:
#   # Summary for the past week
#   ./.loom/scripts/test-plan-metrics.sh
#
#   # JSON output for the past month
#   ./.loom/scripts/test-plan-metrics.sh --period month --format json
#
#   # All-time metrics, text format
#   ./.loom/scripts/test-plan-metrics.sh --period all

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
NC='\033[0m' # No Color

# Default options
PERIOD="week"
FORMAT="text"
LIMIT=50
REPO_SLUG=""

# Cache repo slug to avoid repeated API calls
get_repo_slug() {
    if [[ -z "$REPO_SLUG" ]]; then
        REPO_SLUG=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null || echo "")
    fi
    echo "$REPO_SLUG"
}

show_help() {
    cat <<EOF
${BLUE}test-plan-metrics.sh - Test plan execution metrics from Judge PR reviews${NC}

${YELLOW}SYNOPSIS${NC}
    test-plan-metrics.sh [OPTIONS]

${YELLOW}OPTIONS${NC}
    --period PERIOD   Time period: today, week, month, all (default: week)
    --format FORMAT   Output format: text, json (default: text)
    --limit N         Max PRs to analyze (default: 50)
    --help            Show this help message

${YELLOW}EXAMPLES${NC}
    # Summary for the past week
    ./.loom/scripts/test-plan-metrics.sh

    # JSON output for the past month
    ./.loom/scripts/test-plan-metrics.sh --period month --format json

    # All-time metrics
    ./.loom/scripts/test-plan-metrics.sh --period all

${YELLOW}METRICS${NC}
    ${GREEN}Coverage:${NC}
    - PRs with/without test plan execution sections
    - PRs not yet reviewed by Judge

    ${GREEN}Execution:${NC}
    - Total steps across all test plans
    - Steps executed (✅) vs skipped (⚠️/⏭️)
    - Execution rate (executed / total)

    ${GREEN}Skip Reasons:${NC}
    - observation-only (⚠️) - requires manual verification
    - long-running (⏭️) - process >2 minutes
    - external (⏭️) - requires external service
    - other (⏭️) - other skip reasons

${YELLOW}DATA SOURCE${NC}
    Parses Judge review comments on merged PRs via GitHub API.
    The Judge documents test execution using structured emoji markers:
      ✅ Executed    ⚠️ Observation-only    ⏭️ Skipped (long-running/external)
EOF
}

# Get date filter for gh CLI based on period
get_date_filter() {
    local period="$1"
    case "$period" in
        today)
            date -u -v-1d '+%Y-%m-%dT00:00:00Z' 2>/dev/null || date -u -d '1 day ago' '+%Y-%m-%dT00:00:00Z' 2>/dev/null || echo ""
            ;;
        week)
            date -u -v-7d '+%Y-%m-%dT00:00:00Z' 2>/dev/null || date -u -d '7 days ago' '+%Y-%m-%dT00:00:00Z' 2>/dev/null || echo ""
            ;;
        month)
            date -u -v-30d '+%Y-%m-%dT00:00:00Z' 2>/dev/null || date -u -d '30 days ago' '+%Y-%m-%dT00:00:00Z' 2>/dev/null || echo ""
            ;;
        all)
            echo ""
            ;;
        *)
            echo "Error: Invalid period '$period'. Use: today, week, month, all" >&2
            exit 1
            ;;
    esac
}

# Fetch merged PRs for the period
fetch_merged_prs() {
    local date_filter="$1"
    if [[ -n "$date_filter" ]]; then
        gh pr list --state merged --limit "$LIMIT" --json number,mergedAt \
            --jq '[.[] | select(.mergedAt >= "'"$date_filter"'")] | .[].number' 2>/dev/null || echo ""
    else
        gh pr list --state merged --limit "$LIMIT" --json number --jq '.[].number' 2>/dev/null || echo ""
    fi
}

# Extract Judge review comment with Test Execution section from a PR
# Returns the most recent comment containing a Test Execution section.
# Handles both "## Test Execution" (heading) and "**Test Execution:**" (bold) formats.
get_judge_test_execution() {
    local pr_number="$1"
    # Get all comments (issue comments + review comments) and find the most recent
    # one with a Test Execution section
    local comments
    comments=$(gh pr view "$pr_number" --comments --json comments --jq '.comments[].body' 2>/dev/null || echo "")

    # Also check review bodies
    local reviews
    reviews=$(gh api "repos/$(get_repo_slug)/pulls/${pr_number}/reviews" --jq '.[].body // empty' 2>/dev/null || echo "")

    # Combine and find the last one with Test Execution
    local all_bodies
    all_bodies=$(printf '%s\n%s' "$comments" "$reviews")

    # Extract the last Test Execution section using a bash loop
    # (macOS nawk doesn't support IGNORECASE or chained regex conditions)
    local in_section=0
    local section=""
    while IFS= read -r line; do
        if echo "$line" | grep -qiE '^(\*\*|##).*test execution'; then
            in_section=1
            section="$line"
            continue
        fi
        if [[ $in_section -eq 1 ]]; then
            # End section at new ## heading
            if echo "$line" | grep -qE '^## '; then
                break
            fi
            section="${section}
${line}"
        fi
    done <<< "$all_bodies"

    echo "$section"
}

# Check if PR description has a test plan section
# Accepts pre-fetched body as $2 to avoid redundant API calls
pr_has_test_plan() {
    local pr_number="$1"
    local body="${2:-}"
    if [[ -z "$body" ]]; then
        body=$(gh pr view "$pr_number" --json body --jq '.body // ""' 2>/dev/null || echo "")
    fi
    echo "$body" | grep -qi '## test plan' && return 0
    return 1
}

# Parse a Test Execution section into step classifications
# Output: one line per step with format "STATUS\tREASON"
# STATUS: executed, skipped, unknown
# REASON: observation, long_running, external, other, none
parse_test_execution() {
    local section="$1"

    echo "$section" | while IFS= read -r line; do
        # Match numbered list items with execution markers
        # Format: N. [step] — [emoji] [Status]: [details]
        # Also handle "N. [step] — [emoji] [Status text]" without colon
        if echo "$line" | grep -qE '^\s*[0-9]+\.'; then
            if echo "$line" | grep -q '✅'; then
                echo "executed	none"
            elif echo "$line" | grep -q '⚠️'; then
                echo "skipped	observation"
            elif echo "$line" | grep -q '⏭️'; then
                # Disambiguate skip reason from the text after the emoji
                local lower_line
                lower_line=$(echo "$line" | tr '[:upper:]' '[:lower:]')
                if echo "$lower_line" | grep -qE 'long.?running|>.*min|minutes'; then
                    echo "skipped	long_running"
                elif echo "$lower_line" | grep -qE 'external|service|staging|api|email'; then
                    echo "skipped	external"
                else
                    echo "skipped	other"
                fi
            elif echo "$line" | grep -q '❓'; then
                echo "unknown	none"
            else
                # Numbered line but no recognized marker
                # Check if it looks like a test step (has a dash or descriptive text)
                # vs a header line like "**Test plan from PR description:**"
                if echo "$line" | grep -qE '^\s*[0-9]+\.\s+\S'; then
                    echo "unknown	none"
                fi
            fi
        fi
    done
}

# Main metrics collection
collect_metrics() {
    local date_filter
    date_filter=$(get_date_filter "$PERIOD")

    local pr_numbers
    pr_numbers=$(fetch_merged_prs "$date_filter")

    if [[ -z "$pr_numbers" ]]; then
        if [[ "$FORMAT" == "json" ]]; then
            jq -n '{
                period: "'"$PERIOD"'",
                prs_analyzed: 0,
                prs_with_test_plan: 0,
                prs_without_test_plan: 0,
                prs_not_yet_reviewed: 0,
                total_steps: 0,
                steps_executed: 0,
                steps_skipped: 0,
                skip_observation: 0,
                skip_long_running: 0,
                skip_external: 0,
                skip_other: 0,
                steps_unknown: 0,
                execution_rate: 0,
                pr_details: []
            }'
        else
            echo -e "${YELLOW}No merged PRs found for period: $PERIOD${NC}"
        fi
        return 0
    fi

    local prs_analyzed=0
    local prs_with_test_plan=0
    local prs_without_test_plan=0
    local prs_not_yet_reviewed=0
    local total_steps=0
    local steps_executed=0
    local steps_skipped=0
    local skip_observation=0
    local skip_long_running=0
    local skip_external=0
    local skip_other=0
    local steps_unknown=0
    local pr_details_json="[]"

    while IFS= read -r pr_number; do
        [[ -z "$pr_number" ]] && continue
        prs_analyzed=$((prs_analyzed + 1))

        # Prefetch PR body to avoid redundant API calls
        local pr_body
        pr_body=$(gh pr view "$pr_number" --json body --jq '.body // ""' 2>/dev/null || echo "")

        # Get test execution section from Judge comments
        local test_section
        test_section=$(get_judge_test_execution "$pr_number")

        local pr_executed=0
        local pr_skipped=0
        local pr_unknown=0
        local pr_obs=0
        local pr_long=0
        local pr_ext=0
        local pr_oth=0
        local pr_status="reviewed"

        if [[ -z "$test_section" ]] || ! echo "$test_section" | grep -qi "test execution"; then
            # No test execution section found
            if pr_has_test_plan "$pr_number" "$pr_body"; then
                prs_not_yet_reviewed=$((prs_not_yet_reviewed + 1))
                pr_status="not_reviewed"
            else
                prs_without_test_plan=$((prs_without_test_plan + 1))
                pr_status="no_test_plan"
            fi
        else
            prs_with_test_plan=$((prs_with_test_plan + 1))

            # Parse steps
            local parsed
            parsed=$(parse_test_execution "$test_section")

            while IFS=$'\t' read -r status reason; do
                [[ -z "$status" ]] && continue
                total_steps=$((total_steps + 1))
                case "$status" in
                    executed)
                        steps_executed=$((steps_executed + 1))
                        pr_executed=$((pr_executed + 1))
                        ;;
                    skipped)
                        steps_skipped=$((steps_skipped + 1))
                        pr_skipped=$((pr_skipped + 1))
                        case "$reason" in
                            observation)
                                skip_observation=$((skip_observation + 1))
                                pr_obs=$((pr_obs + 1))
                                ;;
                            long_running)
                                skip_long_running=$((skip_long_running + 1))
                                pr_long=$((pr_long + 1))
                                ;;
                            external)
                                skip_external=$((skip_external + 1))
                                pr_ext=$((pr_ext + 1))
                                ;;
                            *)
                                skip_other=$((skip_other + 1))
                                pr_oth=$((pr_oth + 1))
                                ;;
                        esac
                        ;;
                    unknown)
                        steps_unknown=$((steps_unknown + 1))
                        pr_unknown=$((pr_unknown + 1))
                        ;;
                esac
            done <<< "$parsed"
        fi

        local pr_total=$((pr_executed + pr_skipped + pr_unknown))
        local pr_rate=0
        if [[ $pr_total -gt 0 ]]; then
            pr_rate=$(echo "scale=1; $pr_executed * 100 / $pr_total" | bc)
        fi

        pr_details_json=$(echo "$pr_details_json" | jq \
            --argjson num "$pr_number" \
            --arg status "$pr_status" \
            --argjson total "$pr_total" \
            --argjson exec "$pr_executed" \
            --argjson skip "$pr_skipped" \
            --argjson unk "$pr_unknown" \
            --argjson rate "$pr_rate" \
            '. + [{
                pr_number: $num,
                status: $status,
                total_steps: $total,
                executed: $exec,
                skipped: $skip,
                unknown: $unk,
                execution_rate: $rate
            }]')

    done <<< "$pr_numbers"

    # Calculate overall execution rate
    local execution_rate=0
    if [[ $total_steps -gt 0 ]]; then
        execution_rate=$(echo "scale=1; $steps_executed * 100 / $total_steps" | bc)
    fi

    if [[ "$FORMAT" == "json" ]]; then
        jq -n \
            --arg period "$PERIOD" \
            --argjson prs_analyzed "$prs_analyzed" \
            --argjson prs_with_test_plan "$prs_with_test_plan" \
            --argjson prs_without_test_plan "$prs_without_test_plan" \
            --argjson prs_not_yet_reviewed "$prs_not_yet_reviewed" \
            --argjson total_steps "$total_steps" \
            --argjson steps_executed "$steps_executed" \
            --argjson steps_skipped "$steps_skipped" \
            --argjson skip_observation "$skip_observation" \
            --argjson skip_long_running "$skip_long_running" \
            --argjson skip_external "$skip_external" \
            --argjson skip_other "$skip_other" \
            --argjson steps_unknown "$steps_unknown" \
            --argjson execution_rate "$execution_rate" \
            --argjson pr_details "$pr_details_json" \
            '{
                period: $period,
                prs_analyzed: $prs_analyzed,
                prs_with_test_plan: $prs_with_test_plan,
                prs_without_test_plan: $prs_without_test_plan,
                prs_not_yet_reviewed: $prs_not_yet_reviewed,
                total_steps: $total_steps,
                steps_executed: $steps_executed,
                steps_skipped: $steps_skipped,
                skip_breakdown: {
                    observation: $skip_observation,
                    long_running: $skip_long_running,
                    external: $skip_external,
                    other: $skip_other
                },
                steps_unknown: $steps_unknown,
                execution_rate: $execution_rate,
                pr_details: $pr_details
            }'
    else
        echo -e "${BLUE}Test Plan Execution Metrics${NC} ($PERIOD)"
        echo -e "${GRAY}────────────────────────────────────────────────────${NC}"
        echo ""
        echo -e "${GREEN}PR Coverage:${NC}"
        printf "  PRs analyzed:         %d\n" "$prs_analyzed"
        printf "  With test execution:  %d\n" "$prs_with_test_plan"
        printf "  Without test plan:    %d\n" "$prs_without_test_plan"
        printf "  Not yet reviewed:     %d\n" "$prs_not_yet_reviewed"
        echo ""
        echo -e "${GREEN}Step Execution:${NC}"
        printf "  Total steps:          %d\n" "$total_steps"

        # Color code execution rate
        local rate_color="${RED}"
        if (( $(echo "$execution_rate >= 90" | bc -l 2>/dev/null || echo 0) )); then
            rate_color="${GREEN}"
        elif (( $(echo "$execution_rate >= 70" | bc -l 2>/dev/null || echo 0) )); then
            rate_color="${YELLOW}"
        fi
        printf "  Executed (✅):        %d\n" "$steps_executed"
        printf "  Skipped:              %d\n" "$steps_skipped"
        printf "  Unknown:              %d\n" "$steps_unknown"
        printf "  Execution rate:       ${rate_color}%s%%${NC}\n" "$execution_rate"
        echo ""
        echo -e "${GREEN}Skip Reasons:${NC}"
        printf "  Observation-only (⚠️):  %d\n" "$skip_observation"
        printf "  Long-running (⏭️):      %d\n" "$skip_long_running"
        printf "  External service (⏭️):  %d\n" "$skip_external"
        printf "  Other (⏭️):             %d\n" "$skip_other"

        # Show per-PR details for reviewed PRs
        local reviewed_count
        reviewed_count=$(echo "$pr_details_json" | jq '[.[] | select(.status == "reviewed")] | length')
        if [[ "$reviewed_count" -gt 0 ]]; then
            echo ""
            echo -e "${GREEN}Per-PR Details:${NC}"
            echo -e "${GRAY}────────────────────────────────────────────────────${NC}"
            printf "  %-8s %6s %8s %7s %7s %6s\n" "PR" "Steps" "Executed" "Skipped" "Unknown" "Rate"
            echo -e "${GRAY}  ────── ────── ──────── ─────── ─────── ──────${NC}"
            echo "$pr_details_json" | jq -r '.[] | select(.status == "reviewed") | [.pr_number, .total_steps, .executed, .skipped, .unknown, .execution_rate] | @tsv' | while IFS=$'\t' read -r num tot exec skip unk rate; do
                printf "  #%-7s %5d %8d %7d %7d %5s%%\n" "$num" "$tot" "$exec" "$skip" "$unk" "$rate"
            done
        fi
    fi
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --period)
            PERIOD="$2"
            shift 2
            ;;
        --format)
            FORMAT="$2"
            shift 2
            ;;
        --limit)
            LIMIT="$2"
            shift 2
            ;;
        --help|-h|help)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
            echo "Run 'test-plan-metrics.sh --help' for usage" >&2
            exit 1
            ;;
    esac
done

# Validate options
case "$PERIOD" in
    today|week|month|all) ;;
    *) echo -e "${RED}Error: Invalid period '$PERIOD'. Use: today, week, month, all${NC}" >&2; exit 1 ;;
esac

case "$FORMAT" in
    text|json) ;;
    *) echo -e "${RED}Error: Invalid format '$FORMAT'. Use: text, json${NC}" >&2; exit 1 ;;
esac

# Run
collect_metrics
