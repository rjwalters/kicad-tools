#!/bin/bash

# stuck-detection.sh - Detect stuck or struggling agents and signal for intervention
#
# Usage:
#   stuck-detection.sh check [options]           - Check all agents for stuck indicators
#   stuck-detection.sh check-agent <agent-id>    - Check specific agent
#   stuck-detection.sh status                    - Show stuck detection status summary
#   stuck-detection.sh configure [options]       - Configure thresholds
#   stuck-detection.sh history [agent-id]        - Show intervention history
#   stuck-detection.sh --help                    - Show help
#
# Part of the Loom orchestration system for autonomous agent management.
# Detects stuck agents and triggers appropriate interventions.

set -euo pipefail

# Colors for output (disabled if stdout is not a terminal)
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
            # Check if this is a worktree (has .git file, not directory)
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
DAEMON_STATE="$LOOM_DIR/daemon-state.json"
STUCK_CONFIG="$LOOM_DIR/stuck-config.json"
STUCK_HISTORY="$LOOM_DIR/stuck-history.json"
INTERVENTIONS_DIR="$LOOM_DIR/interventions"
PROGRESS_DIR="$LOOM_DIR/progress"

# Default thresholds (in seconds)
DEFAULT_IDLE_THRESHOLD=600        # 10 minutes without output
DEFAULT_WORKING_THRESHOLD=1800    # 30 minutes on same issue with no progress
DEFAULT_LOOP_THRESHOLD=3          # 3 similar error patterns = looping
DEFAULT_ERROR_SPIKE_THRESHOLD=5   # 5 errors in 5 minutes = error spike
DEFAULT_COST_THRESHOLD=1000000    # Cost tokens without meaningful output
DEFAULT_HEARTBEAT_STALE=120       # 2 minutes without heartbeat = stale
DEFAULT_NO_WORKTREE_THRESHOLD=300 # 5 minutes without worktree creation = warning

# Ensure directories exist
ensure_dirs() {
    mkdir -p "$INTERVENTIONS_DIR"
}

# Show help
show_help() {
    cat <<EOF
${BOLD}stuck-detection.sh - Stuck Agent Detection for Loom${NC}

${YELLOW}USAGE:${NC}
    stuck-detection.sh check [options]           Check all agents for stuck indicators
    stuck-detection.sh check-agent <agent-id>    Check specific agent
    stuck-detection.sh status                    Show stuck detection status summary
    stuck-detection.sh configure [options]       Configure thresholds
    stuck-detection.sh history [agent-id]        Show intervention history
    stuck-detection.sh intervene <agent-id> <type> [message]  Manually trigger intervention
    stuck-detection.sh clear <agent-id|all>      Clear stuck state/interventions
    stuck-detection.sh --help                    Show this help

${YELLOW}STUCK INDICATORS:${NC}
    1. ${BOLD}No Progress${NC} - No output for extended time (default: 10 min)
    2. ${BOLD}Extended Work${NC} - Same issue for too long without PR (default: 30 min)
    3. ${BOLD}Looping${NC} - Repeated similar prompts or errors
    4. ${BOLD}Error Spike${NC} - Multiple errors in short period
    5. ${BOLD}High Cost${NC} - Token usage without meaningful output

${YELLOW}INTERVENTION TYPES:${NC}
    ${CYAN}alert${NC}        - Notify human observer (write to interventions/)
    ${CYAN}suggest${NC}      - Suggest role switch (e.g., Builder -> Doctor)
    ${CYAN}pause${NC}        - Auto-pause agent with summary
    ${CYAN}clarify${NC}      - Request clarification from issue author
    ${CYAN}escalate${NC}     - Full escalation chain: warn -> pause -> alert

${YELLOW}EXAMPLES:${NC}
    # Check all shepherds for stuck indicators
    stuck-detection.sh check

    # Check specific agent with verbose output
    stuck-detection.sh check-agent shepherd-1 --verbose

    # Configure thresholds
    stuck-detection.sh configure --idle-threshold 900 --working-threshold 2400

    # Manually trigger intervention
    stuck-detection.sh intervene shepherd-1 pause "Agent appears to be looping"

    # View intervention history
    stuck-detection.sh history shepherd-1

    # Clear stuck state for agent
    stuck-detection.sh clear shepherd-1

${YELLOW}CONFIGURATION:${NC}
    Thresholds are stored in .loom/stuck-config.json:

    {
      "idle_threshold": 600,
      "working_threshold": 1800,
      "loop_threshold": 3,
      "error_spike_threshold": 5,
      "intervention_mode": "escalate"
    }

${YELLOW}INTEGRATION:${NC}
    The Loom daemon calls this script periodically:

    # In daemon loop (every iteration)
    stuck_result=\$(./scripts/stuck-detection.sh check --json)

    # Process any stuck agents
    for agent in \$(echo "\$stuck_result" | jq -r '.stuck_agents[]'); do
        # Handle intervention based on configuration
    done

${YELLOW}OUTPUT FILES:${NC}
    .loom/stuck-config.json      Configuration thresholds
    .loom/stuck-history.json     History of stuck detections
    .loom/interventions/         Active intervention signals

${YELLOW}EXIT CODES:${NC}
    0 - No stuck agents detected
    1 - Error occurred
    2 - Stuck agents detected (check output for details)
EOF
}

# Load configuration or use defaults
load_config() {
    if [[ -f "$STUCK_CONFIG" ]]; then
        IDLE_THRESHOLD=$(jq -r '.idle_threshold // 600' "$STUCK_CONFIG")
        WORKING_THRESHOLD=$(jq -r '.working_threshold // 1800' "$STUCK_CONFIG")
        LOOP_THRESHOLD=$(jq -r '.loop_threshold // 3' "$STUCK_CONFIG")
        ERROR_SPIKE_THRESHOLD=$(jq -r '.error_spike_threshold // 5' "$STUCK_CONFIG")
        INTERVENTION_MODE=$(jq -r '.intervention_mode // "escalate"' "$STUCK_CONFIG")
        HEARTBEAT_STALE=$(jq -r '.heartbeat_stale // 120' "$STUCK_CONFIG")
        NO_WORKTREE_THRESHOLD=$(jq -r '.no_worktree_threshold // 300' "$STUCK_CONFIG")
    else
        IDLE_THRESHOLD=$DEFAULT_IDLE_THRESHOLD
        WORKING_THRESHOLD=$DEFAULT_WORKING_THRESHOLD
        LOOP_THRESHOLD=$DEFAULT_LOOP_THRESHOLD
        ERROR_SPIKE_THRESHOLD=$DEFAULT_ERROR_SPIKE_THRESHOLD
        INTERVENTION_MODE="escalate"
        HEARTBEAT_STALE=$DEFAULT_HEARTBEAT_STALE
        NO_WORKTREE_THRESHOLD=$DEFAULT_NO_WORKTREE_THRESHOLD
    fi
}

# Save configuration
save_config() {
    cat > "$STUCK_CONFIG" <<EOF
{
  "idle_threshold": $IDLE_THRESHOLD,
  "working_threshold": $WORKING_THRESHOLD,
  "loop_threshold": $LOOP_THRESHOLD,
  "error_spike_threshold": $ERROR_SPIKE_THRESHOLD,
  "intervention_mode": "$INTERVENTION_MODE",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
}

# Get file modification time as epoch seconds
get_file_mtime() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        echo "0"
        return
    fi

    if [[ "$(uname)" == "Darwin" ]]; then
        stat -f %m "$file" 2>/dev/null || echo "0"
    else
        stat -c %Y "$file" 2>/dev/null || echo "0"
    fi
}

# Get idle time in seconds from output file
get_idle_seconds() {
    local output_file="$1"

    if [[ -z "$output_file" ]] || [[ "$output_file" == "null" ]] || [[ ! -f "$output_file" ]]; then
        echo "-1"
        return
    fi

    local now_epoch
    local file_mtime

    now_epoch=$(date +%s)
    file_mtime=$(get_file_mtime "$output_file")

    if [[ "$file_mtime" == "0" ]]; then
        echo "-1"
        return
    fi

    echo $((now_epoch - file_mtime))
}

# Get working duration in seconds
get_working_seconds() {
    local started="$1"

    if [[ -z "$started" ]] || [[ "$started" == "null" ]]; then
        echo "0"
        return
    fi

    local now_epoch
    local started_epoch

    now_epoch=$(date +%s)

    if [[ "$(uname)" == "Darwin" ]]; then
        started_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$started" "+%s" 2>/dev/null || echo "0")
    else
        started_epoch=$(date -d "$started" "+%s" 2>/dev/null || echo "0")
    fi

    if [[ "$started_epoch" == "0" ]]; then
        echo "0"
        return
    fi

    echo $((now_epoch - started_epoch))
}

# Analyze output file for loop patterns
detect_loop_pattern() {
    local output_file="$1"

    if [[ ! -f "$output_file" ]]; then
        echo "0"
        return
    fi

    # Look for repeated error patterns in last 100 lines
    local repeated_errors
    repeated_errors=$(tail -100 "$output_file" 2>/dev/null | \
        grep -i "error\|failed\|exception\|cannot\|unable" | \
        sort | uniq -c | sort -rn | head -1 | awk '{print $1}')

    echo "${repeated_errors:-0}"
}

# Count recent errors (within last 5 minutes)
count_recent_errors() {
    local output_file="$1"

    if [[ ! -f "$output_file" ]]; then
        echo "0"
        return
    fi

    # Get last 5 minutes of output (approximately last 500 lines)
    # and count error-like patterns
    local error_count
    error_count=$(tail -500 "$output_file" 2>/dev/null | \
        grep -ci "error\|failed\|exception\|panic\|fatal" 2>/dev/null || echo "0")

    echo "$error_count"
}

# Read progress file for a shepherd by task_id
read_progress_by_task() {
    local task_id="$1"
    local progress_file="$PROGRESS_DIR/shepherd-${task_id}.json"

    if [[ -f "$progress_file" ]]; then
        cat "$progress_file" 2>/dev/null
    else
        echo "{}"
    fi
}

# Find progress file for an agent by matching issue number
find_progress_for_agent() {
    local agent_id="$1"
    local issue="$2"

    if [[ ! -d "$PROGRESS_DIR" ]]; then
        echo "{}"
        return
    fi

    # Look for progress file matching this issue
    for progress_file in "$PROGRESS_DIR"/shepherd-*.json; do
        if [[ -f "$progress_file" ]]; then
            local file_issue
            file_issue=$(jq -r '.issue // 0' "$progress_file" 2>/dev/null || echo "0")
            if [[ "$file_issue" == "$issue" ]]; then
                cat "$progress_file"
                return
            fi
        fi
    done

    echo "{}"
}

# Get heartbeat age from progress file
get_heartbeat_age() {
    local progress="$1"

    if [[ -z "$progress" ]] || [[ "$progress" == "{}" ]]; then
        echo "-1"
        return
    fi

    local last_heartbeat
    last_heartbeat=$(echo "$progress" | jq -r '.last_heartbeat // ""')

    if [[ -z "$last_heartbeat" ]] || [[ "$last_heartbeat" == "null" ]]; then
        echo "-1"
        return
    fi

    local now_epoch
    local hb_epoch

    now_epoch=$(date +%s)

    if [[ "$(uname)" == "Darwin" ]]; then
        hb_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_heartbeat" "+%s" 2>/dev/null || echo "0")
    else
        hb_epoch=$(date -d "$last_heartbeat" "+%s" 2>/dev/null || echo "0")
    fi

    if [[ "$hb_epoch" == "0" ]]; then
        echo "-1"
        return
    fi

    echo $((now_epoch - hb_epoch))
}

# Check for missing expected milestones
check_missing_milestones() {
    local progress="$1"
    local working_seconds="$2"
    local missing=()

    if [[ -z "$progress" ]] || [[ "$progress" == "{}" ]]; then
        # No progress file - can't check milestones
        echo "[]"
        return
    fi

    local milestones
    milestones=$(echo "$progress" | jq -r '.milestones // []')

    # Check if worktree_created is expected but missing
    if [[ $working_seconds -gt $NO_WORKTREE_THRESHOLD ]]; then
        local has_worktree
        has_worktree=$(echo "$milestones" | jq '[.[] | select(.event == "worktree_created")] | length')
        if [[ "$has_worktree" == "0" ]]; then
            missing+=("worktree_created")
        fi
    fi

    # Output as JSON array
    if [[ ${#missing[@]} -eq 0 ]]; then
        echo "[]"
    else
        printf '%s\n' "${missing[@]}" | jq -R . | jq -s .
    fi
}

# Check a single agent for stuck indicators
check_agent() {
    local agent_id="$1"
    local verbose="${2:-false}"

    # Get agent info from daemon state
    if [[ ! -f "$DAEMON_STATE" ]]; then
        echo "{\"agent_id\":\"$agent_id\",\"status\":\"unknown\",\"reason\":\"no daemon state\"}"
        return
    fi

    local issue
    local output_file
    local started
    local task_id

    issue=$(jq -r ".shepherds[\"$agent_id\"].issue // null" "$DAEMON_STATE" 2>/dev/null)
    output_file=$(jq -r ".shepherds[\"$agent_id\"].output_file // null" "$DAEMON_STATE" 2>/dev/null)
    started=$(jq -r ".shepherds[\"$agent_id\"].started // null" "$DAEMON_STATE" 2>/dev/null)
    task_id=$(jq -r ".shepherds[\"$agent_id\"].task_id // null" "$DAEMON_STATE" 2>/dev/null)

    # If no issue assigned, agent is idle - not stuck
    if [[ "$issue" == "null" ]] || [[ -z "$issue" ]]; then
        echo "{\"agent_id\":\"$agent_id\",\"status\":\"idle\",\"stuck\":false}"
        return
    fi

    # Try to read progress file (prefer task_id, fall back to issue match)
    local progress="{}"
    if [[ -n "$task_id" ]] && [[ "$task_id" != "null" ]]; then
        progress=$(read_progress_by_task "$task_id")
    fi
    if [[ "$progress" == "{}" ]]; then
        progress=$(find_progress_for_agent "$agent_id" "$issue")
    fi

    # Calculate metrics
    local idle_seconds
    local working_seconds
    local loop_count
    local error_count
    local heartbeat_age
    local missing_milestones

    working_seconds=$(get_working_seconds "$started")

    # Prefer heartbeat-based idle detection if progress file exists
    heartbeat_age=$(get_heartbeat_age "$progress")
    if [[ "$heartbeat_age" -ge 0 ]]; then
        # Use heartbeat freshness as primary idle indicator
        idle_seconds=$heartbeat_age
    else
        # Fall back to output file timestamp
        idle_seconds=$(get_idle_seconds "$output_file")
    fi

    loop_count=$(detect_loop_pattern "$output_file")
    error_count=$(count_recent_errors "$output_file")
    missing_milestones=$(check_missing_milestones "$progress" "$working_seconds")

    # Check each stuck indicator
    local stuck=false
    local indicators=()
    local severity="none"
    local suggested_intervention="none"

    # 1. No progress indicator (prefer heartbeat if available)
    if [[ "$idle_seconds" -ge "$IDLE_THRESHOLD" ]]; then
        stuck=true
        if [[ "$heartbeat_age" -ge 0 ]]; then
            indicators+=("stale_heartbeat:${idle_seconds}s")
        else
            indicators+=("no_progress:${idle_seconds}s")
        fi
        severity="warning"
        suggested_intervention="alert"
    fi

    # 2. Extended working time without PR
    if [[ "$working_seconds" -ge "$WORKING_THRESHOLD" ]]; then
        # Check if PR exists for this issue
        local pr_exists
        pr_exists=$(gh pr list --search "Closes #$issue" --state open --json number --jq 'length' 2>/dev/null || echo "0")

        if [[ "$pr_exists" == "0" ]]; then
            stuck=true
            indicators+=("extended_work:${working_seconds}s")
            if [[ "$severity" == "none" ]] || [[ "$severity" == "warning" ]]; then
                severity="elevated"
            fi
            suggested_intervention="suggest"
        fi
    fi

    # 3. Looping indicator
    if [[ "$loop_count" -ge "$LOOP_THRESHOLD" ]]; then
        stuck=true
        indicators+=("looping:${loop_count}x")
        severity="critical"
        suggested_intervention="pause"
    fi

    # 4. Error spike indicator
    if [[ "$error_count" -ge "$ERROR_SPIKE_THRESHOLD" ]]; then
        stuck=true
        indicators+=("error_spike:${error_count}")
        if [[ "$severity" != "critical" ]]; then
            severity="elevated"
        fi
        if [[ "$suggested_intervention" == "none" ]] || [[ "$suggested_intervention" == "alert" ]]; then
            suggested_intervention="clarify"
        fi
    fi

    # 5. Missing expected milestones
    local missing_count
    missing_count=$(echo "$missing_milestones" | jq 'length')
    if [[ "$missing_count" -gt 0 ]]; then
        stuck=true
        local missing_list
        missing_list=$(echo "$missing_milestones" | jq -r 'join(",")')
        indicators+=("missing_milestone:$missing_list")
        if [[ "$severity" == "none" ]]; then
            severity="warning"
        fi
        if [[ "$suggested_intervention" == "none" ]]; then
            suggested_intervention="alert"
        fi
    fi

    # Build JSON output
    local indicators_json
    if [[ ${#indicators[@]} -eq 0 ]]; then
        indicators_json="[]"
    else
        indicators_json=$(printf '%s\n' "${indicators[@]}" | jq -R . | jq -s .)
    fi

    # Get current phase from progress if available
    local current_phase="unknown"
    if [[ "$progress" != "{}" ]]; then
        current_phase=$(echo "$progress" | jq -r '.current_phase // "unknown"')
    fi

    cat <<EOF
{
  "agent_id": "$agent_id",
  "issue": $issue,
  "status": "working",
  "stuck": $stuck,
  "severity": "$severity",
  "suggested_intervention": "$suggested_intervention",
  "indicators": $indicators_json,
  "metrics": {
    "idle_seconds": $idle_seconds,
    "heartbeat_age": $heartbeat_age,
    "working_seconds": $working_seconds,
    "loop_count": $loop_count,
    "error_count": $error_count,
    "current_phase": "$current_phase"
  },
  "thresholds": {
    "idle": $IDLE_THRESHOLD,
    "working": $WORKING_THRESHOLD,
    "loop": $LOOP_THRESHOLD,
    "error_spike": $ERROR_SPIKE_THRESHOLD,
    "heartbeat_stale": $HEARTBEAT_STALE
  },
  "missing_milestones": $missing_milestones,
  "checked_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
}

# Check all agents
check_all() {
    local json_output="${1:-false}"
    local verbose="${2:-false}"

    ensure_dirs
    load_config

    if [[ ! -f "$DAEMON_STATE" ]]; then
        if [[ "$json_output" == "true" ]]; then
            echo '{"error":"no daemon state","stuck_agents":[],"total_checked":0}'
        else
            echo -e "${YELLOW}No daemon state found - daemon may not be running${NC}"
        fi
        return 0
    fi

    local stuck_agents=()
    local results=()
    local total_checked=0

    # Check each shepherd
    for i in 1 2 3; do
        local agent_id="shepherd-$i"
        local result
        result=$(check_agent "$agent_id" "$verbose")
        results+=("$result")
        ((total_checked++)) || true

        local is_stuck
        is_stuck=$(echo "$result" | jq -r '.stuck')

        if [[ "$is_stuck" == "true" ]]; then
            stuck_agents+=("$agent_id")

            # Record in history
            record_stuck_detection "$result"

            # Trigger intervention if configured
            if [[ "$INTERVENTION_MODE" != "none" ]]; then
                local severity
                local intervention
                severity=$(echo "$result" | jq -r '.severity')
                intervention=$(echo "$result" | jq -r '.suggested_intervention')

                trigger_intervention "$agent_id" "$intervention" "$result"
            fi
        fi
    done

    # Output results
    if [[ "$json_output" == "true" ]]; then
        local results_json
        results_json=$(printf '%s\n' "${results[@]}" | jq -s .)
        local stuck_json
        if [[ ${#stuck_agents[@]} -eq 0 ]]; then
            stuck_json="[]"
        else
            stuck_json=$(printf '%s\n' "${stuck_agents[@]}" | jq -R . | jq -s .)
        fi

        cat <<EOF
{
  "checked_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "total_checked": $total_checked,
  "stuck_count": ${#stuck_agents[@]},
  "stuck_agents": $stuck_json,
  "results": $results_json,
  "config": {
    "idle_threshold": $IDLE_THRESHOLD,
    "working_threshold": $WORKING_THRESHOLD,
    "intervention_mode": "$INTERVENTION_MODE"
  }
}
EOF
        # Return exit code 2 if stuck agents found
        if [[ ${#stuck_agents[@]} -gt 0 ]]; then
            return 2
        fi
    else
        echo ""
        echo -e "${BOLD}${CYAN}=======================================================================${NC}"
        echo -e "${BOLD}${CYAN}  STUCK AGENT DETECTION${NC}"
        echo -e "${BOLD}${CYAN}=======================================================================${NC}"
        echo ""

        echo -e "  ${BOLD}Configuration:${NC}"
        echo -e "    Idle threshold: ${IDLE_THRESHOLD}s"
        echo -e "    Working threshold: ${WORKING_THRESHOLD}s"
        echo -e "    Intervention mode: $INTERVENTION_MODE"
        echo ""

        echo -e "  ${BOLD}Results:${NC}"
        echo -e "    Total checked: $total_checked"
        echo -e "    Stuck agents: ${#stuck_agents[@]}"
        echo ""

        for result in "${results[@]}"; do
            local agent_id
            local is_stuck
            local severity
            local indicators
            local issue

            agent_id=$(echo "$result" | jq -r '.agent_id')
            is_stuck=$(echo "$result" | jq -r '.stuck')
            severity=$(echo "$result" | jq -r '.severity')
            indicators=$(echo "$result" | jq -r 'if .indicators then (.indicators | join(", ")) else "none" end')
            issue=$(echo "$result" | jq -r '.issue')

            if [[ "$is_stuck" == "true" ]]; then
                local severity_color
                case "$severity" in
                    warning) severity_color="${YELLOW}" ;;
                    elevated) severity_color="${RED}" ;;
                    critical) severity_color="${RED}${BOLD}" ;;
                    *) severity_color="${NC}" ;;
                esac

                echo -e "    ${severity_color}STUCK${NC} $agent_id (issue #$issue)"
                echo -e "      Severity: ${severity_color}$severity${NC}"
                echo -e "      Indicators: $indicators"
                echo ""
            else
                local status
                status=$(echo "$result" | jq -r '.status')
                if [[ "$status" == "idle" ]]; then
                    echo -e "    ${GRAY}$agent_id: idle${NC}"
                else
                    echo -e "    ${GREEN}$agent_id: working normally (issue #$issue)${NC}"
                fi
            fi
        done

        echo -e "${BOLD}${CYAN}=======================================================================${NC}"
        echo ""

        if [[ ${#stuck_agents[@]} -gt 0 ]]; then
            return 2
        fi
    fi

    return 0
}

# Record stuck detection in history
record_stuck_detection() {
    local result="$1"

    local history_entry
    history_entry=$(cat <<EOF
{
  "detected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "detection": $result
}
EOF
)

    # Append to history file
    if [[ -f "$STUCK_HISTORY" ]]; then
        # Read existing, append new entry, keep last 100 entries
        local updated
        updated=$(jq --argjson new "$history_entry" '.entries = (.entries + [$new])[-100:]' "$STUCK_HISTORY")
        echo "$updated" > "$STUCK_HISTORY"
    else
        cat > "$STUCK_HISTORY" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "entries": [$history_entry]
}
EOF
    fi
}

# Trigger intervention for stuck agent
trigger_intervention() {
    local agent_id="$1"
    local intervention_type="$2"
    local detection_result="$3"

    local timestamp
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    local issue
    local severity
    local indicators

    issue=$(echo "$detection_result" | jq -r '.issue')
    severity=$(echo "$detection_result" | jq -r '.severity')
    indicators=$(echo "$detection_result" | jq -r '.indicators | join(", ")')

    # Create intervention signal file
    local intervention_file="$INTERVENTIONS_DIR/${agent_id}-$(date +%Y%m%d%H%M%S).json"

    cat > "$intervention_file" <<EOF
{
  "agent_id": "$agent_id",
  "issue": $issue,
  "intervention_type": "$intervention_type",
  "severity": "$severity",
  "indicators": "$indicators",
  "triggered_at": "$timestamp",
  "status": "pending",
  "detection": $detection_result
}
EOF

    # Also add a human-readable summary
    local summary_file="$INTERVENTIONS_DIR/${agent_id}-latest.txt"
    cat > "$summary_file" <<EOF
STUCK AGENT INTERVENTION
========================

Agent:       $agent_id
Issue:       #$issue
Severity:    $severity
Type:        $intervention_type
Detected:    $timestamp

Indicators:  $indicators

Suggested Actions:
EOF

    case "$intervention_type" in
        alert)
            echo "  - Review agent output: cat \$(jq -r '.shepherds[\"$agent_id\"].output_file' .loom/daemon-state.json)" >> "$summary_file"
            echo "  - Check issue status: gh issue view $issue" >> "$summary_file"
            ;;
        suggest)
            echo "  - Consider switching roles (Builder -> Doctor)" >> "$summary_file"
            echo "  - Check if issue dependencies are blocking" >> "$summary_file"
            echo "  - Review issue for missing requirements" >> "$summary_file"
            ;;
        pause)
            echo "  - Agent has been paused automatically" >> "$summary_file"
            echo "  - Review loop patterns in output" >> "$summary_file"
            echo "  - Restart with: signal.sh clear $agent_id" >> "$summary_file"
            # Actually pause the agent via signal
            "$REPO_ROOT/.loom/scripts/signal.sh" stop "$agent_id" "Auto-paused: stuck detection ($indicators)"
            ;;
        clarify)
            echo "  - Request clarification from issue author" >> "$summary_file"
            echo "  - Add loom:blocked label with reason" >> "$summary_file"
            echo "  - Command: gh issue edit $issue --add-label loom:blocked" >> "$summary_file"
            ;;
        escalate)
            echo "  - ESCALATION: All interventions triggered" >> "$summary_file"
            echo "  - Human attention required immediately" >> "$summary_file"
            # Trigger pause as part of escalation
            "$REPO_ROOT/.loom/scripts/signal.sh" stop "$agent_id" "ESCALATION: stuck detection ($indicators)"
            ;;
    esac

    echo "" >> "$summary_file"
    echo "Intervention file: $intervention_file" >> "$summary_file"
}

# Manually trigger intervention
manual_intervene() {
    local agent_id="$1"
    local intervention_type="$2"
    local message="${3:-Manual intervention triggered}"

    ensure_dirs
    load_config

    # Get current agent state
    local result
    result=$(check_agent "$agent_id" "false")

    # Override suggested intervention
    result=$(echo "$result" | jq --arg type "$intervention_type" '.suggested_intervention = $type')
    result=$(echo "$result" | jq --arg msg "$message" '.manual_message = $msg')

    trigger_intervention "$agent_id" "$intervention_type" "$result"

    echo -e "${GREEN}Intervention triggered for $agent_id${NC}"
    echo -e "  Type: $intervention_type"
    echo -e "  Message: $message"
    echo -e "  Details: $INTERVENTIONS_DIR/${agent_id}-latest.txt"
}

# Show status summary
show_status() {
    ensure_dirs
    load_config

    echo ""
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo -e "${BOLD}${CYAN}  STUCK DETECTION STATUS${NC}"
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""

    # Show configuration
    echo -e "  ${BOLD}Configuration:${NC}"
    if [[ -f "$STUCK_CONFIG" ]]; then
        echo -e "    Idle threshold: ${IDLE_THRESHOLD}s ($(( IDLE_THRESHOLD / 60 ))m)"
        echo -e "    Working threshold: ${WORKING_THRESHOLD}s ($(( WORKING_THRESHOLD / 60 ))m)"
        echo -e "    Loop threshold: ${LOOP_THRESHOLD}x"
        echo -e "    Error spike threshold: ${ERROR_SPIKE_THRESHOLD}"
        echo -e "    Intervention mode: $INTERVENTION_MODE"
    else
        echo -e "    ${GRAY}Using defaults (no config file)${NC}"
    fi
    echo ""

    # Show active interventions
    echo -e "  ${BOLD}Active Interventions:${NC}"
    local intervention_count=0
    for intervention_file in "$INTERVENTIONS_DIR"/*.json; do
        if [[ -f "$intervention_file" ]]; then
            local agent_id
            local severity
            local intervention_type
            local triggered_at

            agent_id=$(jq -r '.agent_id' "$intervention_file")
            severity=$(jq -r '.severity' "$intervention_file")
            intervention_type=$(jq -r '.intervention_type' "$intervention_file")
            triggered_at=$(jq -r '.triggered_at' "$intervention_file")

            ((intervention_count++)) || true
            echo -e "    ${YELLOW}$agent_id${NC}: $intervention_type ($severity) - $triggered_at"
        fi
    done

    if [[ $intervention_count -eq 0 ]]; then
        echo -e "    ${GREEN}No active interventions${NC}"
    fi
    echo ""

    # Show recent history
    echo -e "  ${BOLD}Recent Detections:${NC}"
    if [[ -f "$STUCK_HISTORY" ]]; then
        local recent
        recent=$(jq -r '.entries[-5:][] | "    \(.detected_at): \(.detection.agent_id) - \(.detection.severity)"' "$STUCK_HISTORY" 2>/dev/null)
        if [[ -n "$recent" ]]; then
            echo "$recent"
        else
            echo -e "    ${GRAY}No recent detections${NC}"
        fi
    else
        echo -e "    ${GRAY}No history available${NC}"
    fi
    echo ""

    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""
}

# Show history for an agent
show_history() {
    local agent_id="${1:-}"

    if [[ ! -f "$STUCK_HISTORY" ]]; then
        echo -e "${GRAY}No stuck detection history available${NC}"
        return
    fi

    echo ""
    echo -e "${BOLD}Stuck Detection History${NC}"
    echo ""

    if [[ -n "$agent_id" ]]; then
        echo -e "Agent: $agent_id"
        echo ""
        jq -r ".entries[] | select(.detection.agent_id == \"$agent_id\") | \"  \(.detected_at): \(.detection.severity) - \(.detection.indicators | join(\", \"))\"" "$STUCK_HISTORY"
    else
        jq -r '.entries[-20:][] | "  \(.detected_at): \(.detection.agent_id) - \(.detection.severity) - \(.detection.indicators | join(", "))"' "$STUCK_HISTORY"
    fi
}

# Configure thresholds
configure() {
    load_config

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --idle-threshold)
                IDLE_THRESHOLD="$2"
                shift 2
                ;;
            --working-threshold)
                WORKING_THRESHOLD="$2"
                shift 2
                ;;
            --loop-threshold)
                LOOP_THRESHOLD="$2"
                shift 2
                ;;
            --error-threshold)
                ERROR_SPIKE_THRESHOLD="$2"
                shift 2
                ;;
            --intervention-mode)
                INTERVENTION_MODE="$2"
                shift 2
                ;;
            *)
                echo -e "${RED}Unknown option: $1${NC}" >&2
                exit 1
                ;;
        esac
    done

    save_config
    echo -e "${GREEN}Configuration saved to $STUCK_CONFIG${NC}"
    echo ""
    echo -e "  Idle threshold: ${IDLE_THRESHOLD}s"
    echo -e "  Working threshold: ${WORKING_THRESHOLD}s"
    echo -e "  Loop threshold: ${LOOP_THRESHOLD}x"
    echo -e "  Error spike threshold: ${ERROR_SPIKE_THRESHOLD}"
    echo -e "  Intervention mode: $INTERVENTION_MODE"
}

# Clear stuck state
clear_stuck() {
    local target="$1"

    if [[ "$target" == "all" ]]; then
        rm -f "$INTERVENTIONS_DIR"/*.json
        rm -f "$INTERVENTIONS_DIR"/*.txt
        echo -e "${GREEN}Cleared all intervention files${NC}"
    else
        rm -f "$INTERVENTIONS_DIR/${target}-"*.json
        rm -f "$INTERVENTIONS_DIR/${target}-"*.txt
        # Also clear any stop signal
        "$REPO_ROOT/.loom/scripts/signal.sh" clear "$target" 2>/dev/null || true
        echo -e "${GREEN}Cleared interventions for $target${NC}"
    fi
}

# Main command handling
main() {
    if [[ $# -eq 0 ]]; then
        show_help
        exit 0
    fi

    local command="$1"
    shift

    case "$command" in
        check)
            local json_output=false
            local verbose=false

            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --json) json_output=true; shift ;;
                    --verbose|-v) verbose=true; shift ;;
                    *) echo -e "${RED}Unknown option: $1${NC}" >&2; exit 1 ;;
                esac
            done

            check_all "$json_output" "$verbose"
            ;;
        check-agent)
            if [[ $# -lt 1 ]]; then
                echo -e "${RED}Error: 'check-agent' requires an agent-id${NC}" >&2
                exit 1
            fi
            load_config
            check_agent "$1" "${2:-false}"
            ;;
        status)
            show_status
            ;;
        configure)
            configure "$@"
            ;;
        history)
            show_history "${1:-}"
            ;;
        intervene)
            if [[ $# -lt 2 ]]; then
                echo -e "${RED}Error: 'intervene' requires agent-id and type${NC}" >&2
                echo "Usage: stuck-detection.sh intervene <agent-id> <type> [message]" >&2
                exit 1
            fi
            manual_intervene "$1" "$2" "${3:-}"
            ;;
        clear)
            if [[ $# -lt 1 ]]; then
                echo -e "${RED}Error: 'clear' requires a target (agent-id or 'all')${NC}" >&2
                exit 1
            fi
            clear_stuck "$1"
            ;;
        --help|-h|help)
            show_help
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}" >&2
            echo "Run 'stuck-detection.sh --help' for usage" >&2
            exit 1
            ;;
    esac
}

main "$@"
