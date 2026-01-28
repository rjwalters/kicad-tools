#!/bin/bash
# health-check.sh - Proactive health monitoring and alerting for Loom daemon
#
# Usage:
#   health-check.sh                    # Display health summary
#   health-check.sh --json             # Output health status as JSON
#   health-check.sh --collect          # Collect and store health metrics
#   health-check.sh --alerts           # Show current alerts
#   health-check.sh --acknowledge <id> # Acknowledge an alert
#   health-check.sh --help             # Show help
#
# This script provides proactive health monitoring for the Loom daemon by:
# - Tracking throughput, latency, and error metrics over time
# - Computing a composite health score (0-100)
# - Generating alerts when metrics cross thresholds
# - Maintaining historical data for trend analysis (24-hour retention)
#
# The health system is designed to:
# - Detect degradation patterns before they become critical
# - Enable extended unattended autonomous operation
# - Integrate with existing daemon-state.json and daemon-snapshot.sh
#
# Health metrics are stored in .loom/health-metrics.json
# Alerts are stored in .loom/alerts.json

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
LOOM_DIR="$REPO_ROOT/.loom"
HEALTH_METRICS_FILE="$LOOM_DIR/health-metrics.json"
ALERTS_FILE="$LOOM_DIR/alerts.json"
DAEMON_STATE_FILE="$LOOM_DIR/daemon-state.json"
DAEMON_METRICS_FILE="$LOOM_DIR/daemon-metrics.json"

# Configuration defaults (can be overridden via environment)
RETENTION_HOURS="${LOOM_HEALTH_RETENTION_HOURS:-24}"
THROUGHPUT_DECLINE_THRESHOLD="${LOOM_THROUGHPUT_DECLINE_THRESHOLD:-50}"  # % decline
QUEUE_GROWTH_THRESHOLD="${LOOM_QUEUE_GROWTH_THRESHOLD:-5}"              # absolute growth
# shellcheck disable=SC2034  # Config variables - available for future use
STUCK_AGENT_THRESHOLD="${LOOM_STUCK_AGENT_THRESHOLD:-10}"               # minutes without heartbeat
# shellcheck disable=SC2034  # Config variable - available for future use
ERROR_RATE_THRESHOLD="${LOOM_ERROR_RATE_THRESHOLD:-20}"                 # % error rate

show_help() {
    cat <<EOF
${BOLD}health-check.sh - Proactive Health Monitoring for Loom${NC}

${YELLOW}USAGE:${NC}
    health-check.sh                    Display health summary
    health-check.sh --json             Output health status as JSON
    health-check.sh --collect          Collect and store health metrics
    health-check.sh --history [hours]  Show metric history (default: 1 hour)
    health-check.sh --alerts           Show current alerts
    health-check.sh --acknowledge <id> Acknowledge an alert
    health-check.sh --clear-alerts     Clear all alerts
    health-check.sh --help             Show this help

${YELLOW}HEALTH SCORE:${NC}
    The health score (0-100) is computed from:
    - Throughput trend (declining = lower score)
    - Queue depth trend (growing = lower score)
    - Error rate (increasing = lower score)
    - Resource availability (near limits = lower score)

    Score ranges:
      90-100: ${GREEN}Excellent${NC} - System operating optimally
      70-89:  ${GREEN}Good${NC} - Normal operation, minor issues
      50-69:  ${YELLOW}Fair${NC} - Some degradation detected
      30-49:  ${YELLOW}Warning${NC} - Significant issues, attention needed
      0-29:   ${RED}Critical${NC} - Immediate intervention required

${YELLOW}ALERT TYPES:${NC}
    ${CYAN}throughput_decline${NC}    Throughput dropped significantly
    ${CYAN}stuck_agents${NC}          Agents without recent heartbeats
    ${CYAN}queue_growth${NC}          Ready queue growing without progress
    ${CYAN}high_error_rate${NC}       Error rate exceeds threshold
    ${CYAN}resource_exhaustion${NC}   Session budget or capacity limits

${YELLOW}ALERT SEVERITY:${NC}
    ${GRAY}info${NC}       Metric changed significantly (informational)
    ${YELLOW}warning${NC}    Metric approaching threshold
    ${RED}critical${NC}   Metric exceeded threshold, intervention needed

${YELLOW}ENVIRONMENT VARIABLES:${NC}
    LOOM_HEALTH_RETENTION_HOURS      Metric retention (default: 24)
    LOOM_THROUGHPUT_DECLINE_THRESHOLD  Throughput decline % (default: 50)
    LOOM_QUEUE_GROWTH_THRESHOLD      Queue growth count (default: 5)
    LOOM_STUCK_AGENT_THRESHOLD       Stuck minutes (default: 10)
    LOOM_ERROR_RATE_THRESHOLD        Error rate % (default: 20)

${YELLOW}FILES:${NC}
    .loom/health-metrics.json   Historical health metrics
    .loom/alerts.json           Active and acknowledged alerts
    .loom/daemon-state.json     Current daemon state
    .loom/daemon-metrics.json   Daemon iteration metrics

${YELLOW}EXAMPLES:${NC}
    # Check current health status
    health-check.sh

    # Collect metrics (called by daemon iteration)
    health-check.sh --collect

    # View alerts in JSON format
    health-check.sh --alerts --json

    # Acknowledge a specific alert
    health-check.sh --acknowledge alert-12345

    # View 4-hour metric history
    health-check.sh --history 4
EOF
}

# Get current timestamp in ISO format
get_timestamp() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# Convert ISO timestamp to epoch seconds
timestamp_to_epoch() {
    local timestamp="$1"
    if [[ -z "$timestamp" ]] || [[ "$timestamp" == "null" ]]; then
        echo "0"
        return
    fi
    if [[ "$(uname)" == "Darwin" ]]; then
        date -j -f "%Y-%m-%dT%H:%M:%SZ" "$timestamp" "+%s" 2>/dev/null || echo "0"
    else
        date -d "$timestamp" "+%s" 2>/dev/null || echo "0"
    fi
}

# Initialize health metrics file if it doesn't exist
init_health_metrics() {
    if [[ ! -f "$HEALTH_METRICS_FILE" ]]; then
        local timestamp
        timestamp=$(get_timestamp)
        cat > "$HEALTH_METRICS_FILE" <<EOF
{
  "initialized_at": "$timestamp",
  "retention_hours": $RETENTION_HOURS,
  "metrics": [],
  "health_score": 100,
  "health_status": "excellent",
  "last_updated": "$timestamp"
}
EOF
    fi
}

# Initialize alerts file if it doesn't exist
init_alerts() {
    if [[ ! -f "$ALERTS_FILE" ]]; then
        local timestamp
        timestamp=$(get_timestamp)
        cat > "$ALERTS_FILE" <<EOF
{
  "initialized_at": "$timestamp",
  "alerts": [],
  "acknowledged": []
}
EOF
    fi
}

# Collect current metrics from daemon state and snapshot
collect_current_metrics() {
    local timestamp
    timestamp=$(get_timestamp)

    # Get snapshot data (if daemon-snapshot.sh exists)
    local snapshot="{}"
    if [[ -x "$REPO_ROOT/.loom/scripts/daemon-snapshot.sh" ]]; then
        snapshot=$("$REPO_ROOT/.loom/scripts/daemon-snapshot.sh" 2>/dev/null || echo "{}")
    fi

    # Extract metrics from snapshot
    local ready_count building_count review_count changes_count merge_count
    ready_count=$(echo "$snapshot" | jq -r '.computed.total_ready // 0')
    building_count=$(echo "$snapshot" | jq -r '.computed.total_building // 0')
    review_count=$(echo "$snapshot" | jq -r '.computed.prs_awaiting_review // 0')
    changes_count=$(echo "$snapshot" | jq -r '.computed.prs_needing_fixes // 0')
    merge_count=$(echo "$snapshot" | jq -r '.computed.prs_ready_to_merge // 0')

    # Get shepherd status
    local active_shepherds stale_heartbeats
    active_shepherds=$(echo "$snapshot" | jq -r '.computed.active_shepherds // 0')
    stale_heartbeats=$(echo "$snapshot" | jq -r '.computed.stale_heartbeat_count // 0')

    # Get daemon metrics if available
    local session_percent iteration_count avg_duration success_rate consecutive_failures
    session_percent=0
    iteration_count=0
    avg_duration=0
    success_rate=100
    consecutive_failures=0

    if [[ -f "$DAEMON_METRICS_FILE" ]]; then
        session_percent=$(jq -r '.session_percent // 0' "$DAEMON_METRICS_FILE" 2>/dev/null || echo "0")
        iteration_count=$(jq -r '.total_iterations // 0' "$DAEMON_METRICS_FILE" 2>/dev/null || echo "0")
        avg_duration=$(jq -r '.average_iteration_seconds // 0' "$DAEMON_METRICS_FILE" 2>/dev/null || echo "0")
        consecutive_failures=$(jq -r '.health.consecutive_failures // 0' "$DAEMON_METRICS_FILE" 2>/dev/null || echo "0")

        # Calculate success rate
        local successful
        successful=$(jq -r '.successful_iterations // 0' "$DAEMON_METRICS_FILE" 2>/dev/null || echo "0")
        if [[ $iteration_count -gt 0 ]]; then
            success_rate=$(( (successful * 100) / iteration_count ))
        fi
    fi

    # Get usage data from snapshot
    if echo "$snapshot" | jq -e '.usage.session_percent' >/dev/null 2>&1; then
        session_percent=$(echo "$snapshot" | jq -r '.usage.session_percent // 0')
    fi

    # Calculate throughput from daemon state (completed issues in last hour)
    local issues_per_hour prs_per_hour
    issues_per_hour=0
    prs_per_hour=0

    if [[ -f "$DAEMON_STATE_FILE" ]]; then
        local started_at
        started_at=$(jq -r '.started_at // ""' "$DAEMON_STATE_FILE" 2>/dev/null || echo "")
        local completed_count
        completed_count=$(jq -r '.completed_issues | length // 0' "$DAEMON_STATE_FILE" 2>/dev/null || echo "0")
        local prs_merged
        prs_merged=$(jq -r '.total_prs_merged // 0' "$DAEMON_STATE_FILE" 2>/dev/null || echo "0")

        if [[ -n "$started_at" ]]; then
            local started_epoch now_epoch hours_running
            started_epoch=$(timestamp_to_epoch "$started_at")
            now_epoch=$(date +%s)
            hours_running=$(( (now_epoch - started_epoch) / 3600 ))

            if [[ $hours_running -gt 0 ]]; then
                issues_per_hour=$(( completed_count / hours_running ))
                prs_per_hour=$(( prs_merged / hours_running ))
            elif [[ $hours_running -eq 0 ]]; then
                # Less than an hour, use actual counts
                issues_per_hour=$completed_count
                prs_per_hour=$prs_merged
            fi
        fi
    fi

    # Build the metric entry
    cat <<EOF
{
  "timestamp": "$timestamp",
  "throughput": {
    "issues_per_hour": $issues_per_hour,
    "prs_per_hour": $prs_per_hour
  },
  "latency": {
    "avg_iteration_seconds": $avg_duration
  },
  "queue_depths": {
    "ready": $ready_count,
    "building": $building_count,
    "review_requested": $review_count,
    "changes_requested": $changes_count,
    "ready_to_merge": $merge_count
  },
  "error_rates": {
    "consecutive_failures": $consecutive_failures,
    "success_rate": $success_rate,
    "stuck_agents": $stale_heartbeats
  },
  "resource_usage": {
    "active_shepherds": $active_shepherds,
    "session_percent": $session_percent
  }
}
EOF
}

# Calculate health score from recent metrics
calculate_health_score() {
    local metrics_json="$1"

    # Start with perfect score
    local score=100

    # Get the most recent metric
    local latest
    latest=$(echo "$metrics_json" | jq -r '.metrics | last // {}')

    if [[ "$latest" == "{}" ]] || [[ -z "$latest" ]]; then
        echo "100"
        return
    fi

    # Factor 1: Error rate (0-25 points deduction)
    local success_rate consecutive_failures
    success_rate=$(echo "$latest" | jq -r '.error_rates.success_rate // 100')
    consecutive_failures=$(echo "$latest" | jq -r '.error_rates.consecutive_failures // 0')

    if [[ $success_rate -lt 50 ]]; then
        score=$((score - 25))
    elif [[ $success_rate -lt 70 ]]; then
        score=$((score - 15))
    elif [[ $success_rate -lt 90 ]]; then
        score=$((score - 5))
    fi

    if [[ $consecutive_failures -ge 5 ]]; then
        score=$((score - 15))
    elif [[ $consecutive_failures -ge 3 ]]; then
        score=$((score - 10))
    elif [[ $consecutive_failures -ge 1 ]]; then
        score=$((score - 5))
    fi

    # Factor 2: Stuck agents (0-20 points deduction)
    local stuck_agents
    stuck_agents=$(echo "$latest" | jq -r '.error_rates.stuck_agents // 0')
    if [[ $stuck_agents -ge 3 ]]; then
        score=$((score - 20))
    elif [[ $stuck_agents -ge 2 ]]; then
        score=$((score - 15))
    elif [[ $stuck_agents -ge 1 ]]; then
        score=$((score - 10))
    fi

    # Factor 3: Queue growth (0-15 points deduction)
    # Compare with previous metrics if available
    local prev_ready current_ready
    current_ready=$(echo "$latest" | jq -r '.queue_depths.ready // 0')
    prev_ready=$(echo "$metrics_json" | jq -r '.metrics[-2].queue_depths.ready // 0' 2>/dev/null || echo "0")

    local queue_growth=$((current_ready - prev_ready))
    if [[ $queue_growth -ge $QUEUE_GROWTH_THRESHOLD ]]; then
        score=$((score - 15))
    elif [[ $queue_growth -ge 3 ]]; then
        score=$((score - 10))
    elif [[ $queue_growth -ge 1 ]]; then
        score=$((score - 5))
    fi

    # Factor 4: Resource usage (0-15 points deduction)
    local session_percent
    session_percent=$(echo "$latest" | jq -r '.resource_usage.session_percent // 0')
    if [[ $session_percent -ge 95 ]]; then
        score=$((score - 15))
    elif [[ $session_percent -ge 90 ]]; then
        score=$((score - 10))
    elif [[ $session_percent -ge 80 ]]; then
        score=$((score - 5))
    fi

    # Factor 5: Throughput decline (0-15 points deduction)
    local current_throughput prev_throughput
    current_throughput=$(echo "$latest" | jq -r '.throughput.issues_per_hour // 0')
    prev_throughput=$(echo "$metrics_json" | jq -r '.metrics[-2].throughput.issues_per_hour // 0' 2>/dev/null || echo "0")

    if [[ $prev_throughput -gt 0 ]] && [[ $current_throughput -lt $prev_throughput ]]; then
        local decline_percent=$(( ((prev_throughput - current_throughput) * 100) / prev_throughput ))
        if [[ $decline_percent -ge $THROUGHPUT_DECLINE_THRESHOLD ]]; then
            score=$((score - 15))
        elif [[ $decline_percent -ge 30 ]]; then
            score=$((score - 10))
        elif [[ $decline_percent -ge 10 ]]; then
            score=$((score - 5))
        fi
    fi

    # Ensure score is within bounds
    if [[ $score -lt 0 ]]; then
        score=0
    elif [[ $score -gt 100 ]]; then
        score=100
    fi

    echo "$score"
}

# Get health status from score
get_health_status() {
    local score="$1"
    if [[ $score -ge 90 ]]; then
        echo "excellent"
    elif [[ $score -ge 70 ]]; then
        echo "good"
    elif [[ $score -ge 50 ]]; then
        echo "fair"
    elif [[ $score -ge 30 ]]; then
        echo "warning"
    else
        echo "critical"
    fi
}

# Generate alerts based on current metrics
generate_alerts() {
    local metrics_json="$1"
    local timestamp
    timestamp=$(get_timestamp)

    local alerts="[]"
    local latest
    latest=$(echo "$metrics_json" | jq -r '.metrics | last // {}')

    if [[ "$latest" == "{}" ]] || [[ -z "$latest" ]]; then
        echo "[]"
        return
    fi

    # Check for stuck agents
    local stuck_agents
    stuck_agents=$(echo "$latest" | jq -r '.error_rates.stuck_agents // 0')
    if [[ $stuck_agents -ge 1 ]]; then
        local severity="warning"
        if [[ $stuck_agents -ge 3 ]]; then
            severity="critical"
        fi
        local alert_id="alert-stuck-$(date +%s)"
        alerts=$(echo "$alerts" | jq --arg id "$alert_id" \
            --arg severity "$severity" \
            --arg timestamp "$timestamp" \
            --argjson count "$stuck_agents" \
            '. + [{
                "id": $id,
                "type": "stuck_agents",
                "severity": $severity,
                "message": "\($count) agent(s) with stale heartbeats",
                "timestamp": $timestamp,
                "acknowledged": false,
                "context": {"stuck_count": $count}
            }]')
    fi

    # Check for consecutive failures
    local consecutive_failures
    consecutive_failures=$(echo "$latest" | jq -r '.error_rates.consecutive_failures // 0')
    if [[ $consecutive_failures -ge 3 ]]; then
        local severity="warning"
        if [[ $consecutive_failures -ge 5 ]]; then
            severity="critical"
        fi
        local alert_id="alert-failures-$(date +%s)"
        alerts=$(echo "$alerts" | jq --arg id "$alert_id" \
            --arg severity "$severity" \
            --arg timestamp "$timestamp" \
            --argjson count "$consecutive_failures" \
            '. + [{
                "id": $id,
                "type": "high_error_rate",
                "severity": $severity,
                "message": "\($count) consecutive iteration failures",
                "timestamp": $timestamp,
                "acknowledged": false,
                "context": {"consecutive_failures": $count}
            }]')
    fi

    # Check for resource exhaustion
    local session_percent
    session_percent=$(echo "$latest" | jq -r '.resource_usage.session_percent // 0')
    if [[ $session_percent -ge 90 ]]; then
        local severity="warning"
        if [[ $session_percent -ge 97 ]]; then
            severity="critical"
        fi
        local alert_id="alert-resource-$(date +%s)"
        alerts=$(echo "$alerts" | jq --arg id "$alert_id" \
            --arg severity "$severity" \
            --arg timestamp "$timestamp" \
            --argjson percent "$session_percent" \
            '. + [{
                "id": $id,
                "type": "resource_exhaustion",
                "severity": $severity,
                "message": "Session budget at \($percent)%",
                "timestamp": $timestamp,
                "acknowledged": false,
                "context": {"session_percent": $percent}
            }]')
    fi

    # Check for queue growth
    local current_ready prev_ready
    current_ready=$(echo "$latest" | jq -r '.queue_depths.ready // 0')
    prev_ready=$(echo "$metrics_json" | jq -r '.metrics[-2].queue_depths.ready // 0' 2>/dev/null || echo "0")
    local queue_growth=$((current_ready - prev_ready))

    if [[ $queue_growth -ge $QUEUE_GROWTH_THRESHOLD ]]; then
        local alert_id="alert-queue-$(date +%s)"
        alerts=$(echo "$alerts" | jq --arg id "$alert_id" \
            --arg timestamp "$timestamp" \
            --argjson growth "$queue_growth" \
            --argjson current "$current_ready" \
            '. + [{
                "id": $id,
                "type": "queue_growth",
                "severity": "warning",
                "message": "Ready queue grew by \($growth) (now \($current))",
                "timestamp": $timestamp,
                "acknowledged": false,
                "context": {"growth": $growth, "current": $current}
            }]')
    fi

    echo "$alerts"
}

# Collect metrics and update health status
collect_metrics() {
    init_health_metrics
    init_alerts

    local timestamp
    timestamp=$(get_timestamp)

    # Collect current metrics
    local current_metric
    current_metric=$(collect_current_metrics)

    # Read existing metrics
    local metrics_json
    metrics_json=$(cat "$HEALTH_METRICS_FILE")

    # Add new metric
    metrics_json=$(echo "$metrics_json" | jq --argjson metric "$current_metric" \
        '.metrics = (.metrics + [$metric])')

    # Prune old metrics (keep only last RETENTION_HOURS worth)
    local cutoff_epoch
    cutoff_epoch=$(($(date +%s) - (RETENTION_HOURS * 3600)))

    metrics_json=$(echo "$metrics_json" | jq --argjson cutoff "$cutoff_epoch" '
        .metrics = [.metrics[] | select(
            (.timestamp | fromdateiso8601) > $cutoff
        )]
    ')

    # Calculate health score
    local health_score
    health_score=$(calculate_health_score "$metrics_json")
    local health_status
    health_status=$(get_health_status "$health_score")

    # Update metrics file
    metrics_json=$(echo "$metrics_json" | jq \
        --argjson score "$health_score" \
        --arg status "$health_status" \
        --arg updated "$timestamp" \
        '.health_score = $score | .health_status = $status | .last_updated = $updated')

    echo "$metrics_json" > "$HEALTH_METRICS_FILE"

    # Generate and store alerts
    local new_alerts
    new_alerts=$(generate_alerts "$metrics_json")

    if [[ "$new_alerts" != "[]" ]]; then
        local alerts_json
        alerts_json=$(cat "$ALERTS_FILE")
        alerts_json=$(echo "$alerts_json" | jq --argjson new "$new_alerts" \
            '.alerts = (.alerts + $new)')

        # Keep only last 100 alerts
        alerts_json=$(echo "$alerts_json" | jq '.alerts = .alerts[-100:]')

        echo "$alerts_json" > "$ALERTS_FILE"
    fi

    echo "Metrics collected. Health score: $health_score ($health_status)"
}

# Show health status
show_health_status() {
    local json_output="${1:-false}"

    init_health_metrics
    init_alerts

    local metrics_json
    metrics_json=$(cat "$HEALTH_METRICS_FILE")

    local health_score health_status last_updated metric_count
    health_score=$(echo "$metrics_json" | jq -r '.health_score // 100')
    health_status=$(echo "$metrics_json" | jq -r '.health_status // "unknown"')
    last_updated=$(echo "$metrics_json" | jq -r '.last_updated // "never"')
    metric_count=$(echo "$metrics_json" | jq -r '.metrics | length')

    # Get latest metrics for display
    local latest
    latest=$(echo "$metrics_json" | jq -r '.metrics | last // {}')

    # Get alert counts
    local alerts_json
    alerts_json=$(cat "$ALERTS_FILE")
    local unack_count total_alerts
    unack_count=$(echo "$alerts_json" | jq -r '[.alerts[] | select(.acknowledged == false)] | length')
    total_alerts=$(echo "$alerts_json" | jq -r '.alerts | length')

    if [[ "$json_output" == "true" ]]; then
        jq -n \
            --argjson score "$health_score" \
            --arg status "$health_status" \
            --arg updated "$last_updated" \
            --argjson metric_count "$metric_count" \
            --argjson unack_alerts "$unack_count" \
            --argjson total_alerts "$total_alerts" \
            --argjson latest "$latest" \
            --argjson full_metrics "$metrics_json" \
            '{
                health_score: $score,
                health_status: $status,
                last_updated: $updated,
                metric_count: $metric_count,
                unacknowledged_alerts: $unack_alerts,
                total_alerts: $total_alerts,
                latest_metrics: $latest,
                metrics_history: $full_metrics.metrics
            }'
        return
    fi

    echo ""
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo -e "${BOLD}${CYAN}  LOOM HEALTH STATUS${NC}"
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""

    # Health score with color
    local score_color="$GREEN"
    if [[ $health_score -lt 30 ]]; then
        score_color="$RED"
    elif [[ $health_score -lt 50 ]]; then
        score_color="$YELLOW"
    elif [[ $health_score -lt 70 ]]; then
        score_color="$YELLOW"
    fi

    echo -e "  ${BOLD}Health Score:${NC} ${score_color}${health_score}/100${NC} (${health_status})"
    echo -e "  ${BOLD}Last Updated:${NC} $last_updated"
    echo -e "  ${BOLD}Metrics Stored:${NC} $metric_count samples"
    echo ""

    # Alert summary
    if [[ $unack_count -gt 0 ]]; then
        echo -e "  ${BOLD}Alerts:${NC} ${RED}$unack_count unacknowledged${NC} ($total_alerts total)"
    else
        echo -e "  ${BOLD}Alerts:${NC} ${GREEN}No unacknowledged alerts${NC} ($total_alerts total)"
    fi
    echo ""

    # Latest metrics
    if [[ "$latest" != "{}" ]]; then
        echo -e "  ${BOLD}Current Metrics:${NC}"

        local issues_per_hour prs_per_hour
        issues_per_hour=$(echo "$latest" | jq -r '.throughput.issues_per_hour // 0')
        prs_per_hour=$(echo "$latest" | jq -r '.throughput.prs_per_hour // 0')
        echo -e "    Throughput: ${issues_per_hour} issues/hr, ${prs_per_hour} PRs/hr"

        local ready building review
        ready=$(echo "$latest" | jq -r '.queue_depths.ready // 0')
        building=$(echo "$latest" | jq -r '.queue_depths.building // 0')
        review=$(echo "$latest" | jq -r '.queue_depths.review_requested // 0')
        echo -e "    Queue Depths: ready=$ready, building=$building, review=$review"

        local success_rate consecutive_failures stuck
        success_rate=$(echo "$latest" | jq -r '.error_rates.success_rate // 100')
        consecutive_failures=$(echo "$latest" | jq -r '.error_rates.consecutive_failures // 0')
        stuck=$(echo "$latest" | jq -r '.error_rates.stuck_agents // 0')
        echo -e "    Error Rates: ${success_rate}% success, ${consecutive_failures} failures, ${stuck} stuck"

        local active_shepherds session_percent
        active_shepherds=$(echo "$latest" | jq -r '.resource_usage.active_shepherds // 0')
        session_percent=$(echo "$latest" | jq -r '.resource_usage.session_percent // 0')
        echo -e "    Resources: ${active_shepherds} shepherds, ${session_percent}% session"
    else
        echo -e "  ${GRAY}No metrics collected yet. Run: health-check.sh --collect${NC}"
    fi
    echo ""

    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""
}

# Show alerts
show_alerts() {
    local json_output="${1:-false}"

    init_alerts

    local alerts_json
    alerts_json=$(cat "$ALERTS_FILE")

    if [[ "$json_output" == "true" ]]; then
        echo "$alerts_json"
        return
    fi

    local unack_alerts
    unack_alerts=$(echo "$alerts_json" | jq -r '[.alerts[] | select(.acknowledged == false)]')
    local unack_count
    unack_count=$(echo "$unack_alerts" | jq -r 'length')

    echo ""
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo -e "${BOLD}${CYAN}  LOOM ALERTS${NC}"
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""

    if [[ $unack_count -eq 0 ]]; then
        echo -e "  ${GREEN}No unacknowledged alerts${NC}"
    else
        echo -e "  ${YELLOW}$unack_count unacknowledged alert(s):${NC}"
        echo ""

        echo "$unack_alerts" | jq -r '.[] | "    [\(.severity)] \(.type): \(.message)\n      ID: \(.id)\n      Time: \(.timestamp)\n"'
    fi

    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""
}

# Acknowledge an alert
acknowledge_alert() {
    local alert_id="$1"

    init_alerts

    local alerts_json
    alerts_json=$(cat "$ALERTS_FILE")

    # Check if alert exists
    local exists
    exists=$(echo "$alerts_json" | jq --arg id "$alert_id" '[.alerts[] | select(.id == $id)] | length')

    if [[ "$exists" -eq 0 ]]; then
        echo -e "${RED}Alert not found: $alert_id${NC}"
        return 1
    fi

    # Mark as acknowledged
    local timestamp
    timestamp=$(get_timestamp)
    alerts_json=$(echo "$alerts_json" | jq --arg id "$alert_id" --arg ts "$timestamp" '
        .alerts = [.alerts[] | if .id == $id then .acknowledged = true | .acknowledged_at = $ts else . end]
    ')

    echo "$alerts_json" > "$ALERTS_FILE"
    echo -e "${GREEN}Alert acknowledged: $alert_id${NC}"
}

# Clear all alerts
clear_alerts() {
    init_alerts

    local timestamp
    timestamp=$(get_timestamp)

    cat > "$ALERTS_FILE" <<EOF
{
  "initialized_at": "$timestamp",
  "alerts": [],
  "acknowledged": []
}
EOF

    echo -e "${GREEN}All alerts cleared${NC}"
}

# Show metric history
show_history() {
    local hours="${1:-1}"
    local json_output="${2:-false}"

    init_health_metrics

    local metrics_json
    metrics_json=$(cat "$HEALTH_METRICS_FILE")

    local cutoff_epoch
    cutoff_epoch=$(($(date +%s) - (hours * 3600)))

    local filtered
    filtered=$(echo "$metrics_json" | jq --argjson cutoff "$cutoff_epoch" '
        .metrics = [.metrics[] | select(
            (.timestamp | fromdateiso8601) > $cutoff
        )]
    ')

    if [[ "$json_output" == "true" ]]; then
        echo "$filtered"
        return
    fi

    local count
    count=$(echo "$filtered" | jq -r '.metrics | length')

    echo ""
    echo -e "${BOLD}Metric History (last ${hours} hour(s), $count samples):${NC}"
    echo ""

    echo "$filtered" | jq -r '.metrics[] | "\(.timestamp): score=\(.health_score // "?"), ready=\(.queue_depths.ready // 0), building=\(.queue_depths.building // 0), stuck=\(.error_rates.stuck_agents // 0)"'
    echo ""
}

# Main command handling
main() {
    local json_output=false

    # Parse global flags
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)
                json_output=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            --collect)
                collect_metrics
                exit 0
                ;;
            --alerts)
                shift
                # Check if --json follows
                if [[ "${1:-}" == "--json" ]]; then
                    json_output=true
                    shift
                fi
                show_alerts "$json_output"
                exit 0
                ;;
            --acknowledge)
                if [[ -z "${2:-}" ]]; then
                    echo -e "${RED}Error: --acknowledge requires an alert ID${NC}" >&2
                    exit 1
                fi
                acknowledge_alert "$2"
                exit 0
                ;;
            --clear-alerts)
                clear_alerts
                exit 0
                ;;
            --history)
                local hours="${2:-1}"
                shift
                if [[ -n "${1:-}" ]] && [[ "$1" =~ ^[0-9]+$ ]]; then
                    hours="$1"
                    shift
                fi
                # Check if --json follows
                if [[ "${1:-}" == "--json" ]]; then
                    json_output=true
                    shift
                fi
                show_history "$hours" "$json_output"
                exit 0
                ;;
            *)
                # Default: show health status
                show_health_status "$json_output"
                exit 0
                ;;
        esac
    done

    # Default: show health status
    show_health_status "$json_output"
}

main "$@"
