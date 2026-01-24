#!/bin/bash

# loom-status.sh - Read-only system status for Layer 3 observation
#
# Usage:
#   loom-status.sh              - Display full system status
#   loom-status.sh --json       - Output status as JSON
#   loom-status.sh --help       - Show help
#
# This script provides a read-only view of the Loom daemon state without
# taking any action. It's designed for Layer 3 (human observer) to monitor
# the system state.

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
    NC='\033[0m' # No Color
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
                # Read the gitdir path from .git file
                local gitdir
                gitdir=$(sed 's/^gitdir: //' "$dir/.git")
                # Navigate up from .git/worktrees/<name> to find main repo
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
DAEMON_STATE="$REPO_ROOT/.loom/daemon-state.json"
STOP_FILE="$REPO_ROOT/.loom/stop-daemon"

# Show help
show_help() {
    cat <<EOF
${BOLD}loom-status.sh - Loom System Status (Read-Only)${NC}

${YELLOW}USAGE:${NC}
    loom-status.sh              Display full system status
    loom-status.sh --json       Output status as JSON
    loom-status.sh --help       Show this help message

${YELLOW}DESCRIPTION:${NC}
    This script provides a read-only observation interface for the Loom
    orchestration system. It displays:

    - Daemon status (running/stopped, uptime)
    - System state (issue counts by label)
    - Shepherd pool status (active/idle, assigned issues, idle time)
    - Support role status (Architect, Hermit, Guide, Champion)
    - Session statistics (completed issues, PRs merged)
    - Available Layer 3 interventions

    For active shepherds, idle time is computed from the output file
    modification time (time since last output was written).

${YELLOW}LAYER 3 ROLE:${NC}
    The human observer (Layer 3) uses this command to:

    - Monitor autonomous development progress
    - Identify issues needing human intervention
    - Approve pending proposals
    - Initiate graceful shutdown when needed

${YELLOW}EXAMPLES:${NC}
    # View current system status
    ./loom-status.sh

    # Get status as JSON for scripting
    ./loom-status.sh --json | jq '.shepherds'

${YELLOW}FILES:${NC}
    .loom/daemon-state.json     Daemon state file
    .loom/stop-daemon           Shutdown signal file

${YELLOW}RELATED COMMANDS:${NC}
    /loom                       Run the daemon (Layer 2)
    /loom status                Equivalent to this script
    touch .loom/stop-daemon     Signal graceful shutdown
EOF
}

# Calculate time difference in human-readable format
time_ago() {
    local timestamp="$1"

    if [[ -z "$timestamp" ]] || [[ "$timestamp" == "null" ]]; then
        echo "never"
        return
    fi

    local now_epoch
    local then_epoch

    now_epoch=$(date +%s)

    # Parse ISO timestamp
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS
        then_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$timestamp" "+%s" 2>/dev/null || echo "0")
    else
        # Linux
        then_epoch=$(date -d "$timestamp" "+%s" 2>/dev/null || echo "0")
    fi

    if [[ "$then_epoch" == "0" ]]; then
        echo "unknown"
        return
    fi

    local diff=$((now_epoch - then_epoch))

    if [[ $diff -lt 60 ]]; then
        echo "${diff}s ago"
    elif [[ $diff -lt 3600 ]]; then
        echo "$((diff / 60))m ago"
    elif [[ $diff -lt 86400 ]]; then
        local hours=$((diff / 3600))
        local mins=$(((diff % 3600) / 60))
        echo "${hours}h ${mins}m ago"
    else
        local days=$((diff / 86400))
        local hours=$(((diff % 86400) / 3600))
        echo "${days}d ${hours}h ago"
    fi
}

# Format duration from timestamp to now
format_uptime() {
    local timestamp="$1"

    if [[ -z "$timestamp" ]] || [[ "$timestamp" == "null" ]]; then
        echo "unknown"
        return
    fi

    local now_epoch
    local then_epoch

    now_epoch=$(date +%s)

    if [[ "$(uname)" == "Darwin" ]]; then
        then_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$timestamp" "+%s" 2>/dev/null || echo "0")
    else
        then_epoch=$(date -d "$timestamp" "+%s" 2>/dev/null || echo "0")
    fi

    if [[ "$then_epoch" == "0" ]]; then
        echo "unknown"
        return
    fi

    local diff=$((now_epoch - then_epoch))

    if [[ $diff -lt 60 ]]; then
        echo "${diff}s"
    elif [[ $diff -lt 3600 ]]; then
        echo "$((diff / 60))m"
    elif [[ $diff -lt 86400 ]]; then
        local hours=$((diff / 3600))
        local mins=$(((diff % 3600) / 60))
        echo "${hours}h ${mins}m"
    else
        local days=$((diff / 86400))
        local hours=$(((diff % 86400) / 3600))
        echo "${days}d ${hours}h"
    fi
}

# Format seconds into human-readable duration (e.g., "2m 30s")
format_seconds() {
    local seconds="$1"

    if [[ -z "$seconds" ]] || [[ "$seconds" -lt 0 ]]; then
        echo "unknown"
        return
    fi

    if [[ $seconds -lt 60 ]]; then
        echo "${seconds}s"
    elif [[ $seconds -lt 3600 ]]; then
        local mins=$((seconds / 60))
        local secs=$((seconds % 60))
        if [[ $secs -gt 0 ]]; then
            echo "${mins}m ${secs}s"
        else
            echo "${mins}m"
        fi
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

# Get idle time in seconds from output file modification time
# Returns idle seconds, or -1 if file doesn't exist or can't be read
get_file_idle_seconds() {
    local output_file="$1"

    if [[ -z "$output_file" ]] || [[ "$output_file" == "null" ]]; then
        echo "-1"
        return
    fi

    if [[ ! -f "$output_file" ]]; then
        echo "-1"
        return
    fi

    local now_epoch
    local file_mtime

    now_epoch=$(date +%s)

    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS: stat -f %m returns modification time as epoch seconds
        file_mtime=$(stat -f %m "$output_file" 2>/dev/null || echo "0")
    else
        # Linux: stat -c %Y returns modification time as epoch seconds
        file_mtime=$(stat -c %Y "$output_file" 2>/dev/null || echo "0")
    fi

    if [[ "$file_mtime" == "0" ]]; then
        echo "-1"
        return
    fi

    local idle_seconds=$((now_epoch - file_mtime))
    echo "$idle_seconds"
}

# Get GitHub issue/PR counts
get_github_counts() {
    local label="$1"
    local type="${2:-issue}"

    if [[ "$type" == "pr" ]]; then
        gh pr list --label "$label" --state open --json number --jq 'length' 2>/dev/null || echo "?"
    else
        gh issue list --label "$label" --state open --json number --jq 'length' 2>/dev/null || echo "?"
    fi
}

# Output JSON status
output_json() {
    local daemon_running="false"
    local daemon_state="{}"

    if [[ -f "$DAEMON_STATE" ]]; then
        daemon_state=$(cat "$DAEMON_STATE")
        daemon_running=$(echo "$daemon_state" | jq -r '.running // false')
    fi

    local shutdown_pending="false"
    if [[ -f "$STOP_FILE" ]]; then
        shutdown_pending="true"
    fi

    # Get GitHub counts
    local ready_issues=$(get_github_counts "loom:issue")
    local building_issues=$(get_github_counts "loom:building")
    local curated_issues=$(get_github_counts "loom:curated")
    local architect_proposals=$(get_github_counts "loom:architect")
    local hermit_proposals=$(get_github_counts "loom:hermit")
    local pending_reviews=$(get_github_counts "loom:review-requested" "pr")
    local ready_to_merge=$(get_github_counts "loom:pr" "pr")

    # Build shepherd status with computed idle times
    local shepherds_json="{"
    local first_shepherd=true
    if [[ -f "$DAEMON_STATE" ]]; then
        for i in 1 2 3; do
            local shepherd_id="shepherd-$i"
            local issue
            local output_file
            issue=$(jq -r ".shepherds[\"$shepherd_id\"].issue // null" "$DAEMON_STATE" 2>/dev/null)
            output_file=$(jq -r ".shepherds[\"$shepherd_id\"].output_file // null" "$DAEMON_STATE" 2>/dev/null)

            local idle_seconds=-1
            local idle_display="null"

            if [[ "$issue" != "null" ]] && [[ -n "$issue" ]]; then
                idle_seconds=$(get_file_idle_seconds "$output_file")
                if [[ "$idle_seconds" -ge 0 ]]; then
                    idle_display="\"$(format_seconds "$idle_seconds")\""
                fi
            fi

            if [[ "$first_shepherd" == "true" ]]; then
                first_shepherd=false
            else
                shepherds_json+=","
            fi
            shepherds_json+="\"$shepherd_id\":{\"issue\":$issue,\"idle_seconds\":$idle_seconds,\"idle_display\":$idle_display}"
        done
    fi
    shepherds_json+="}"

    # Build JSON output
    cat <<EOF
{
  "daemon": {
    "running": $daemon_running,
    "shutdown_pending": $shutdown_pending,
    "state_file": "$DAEMON_STATE"
  },
  "github": {
    "ready_issues": $ready_issues,
    "building_issues": $building_issues,
    "curated_issues": $curated_issues,
    "architect_proposals": $architect_proposals,
    "hermit_proposals": $hermit_proposals,
    "pending_reviews": $pending_reviews,
    "ready_to_merge": $ready_to_merge
  },
  "shepherds": $shepherds_json,
  "daemon_state": $daemon_state
}
EOF
}

# Output formatted status
output_formatted() {
    echo ""
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo -e "${BOLD}${CYAN}  LOOM SYSTEM STATUS (read-only)${NC}"
    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""

    # Daemon status
    local daemon_status="${RED}Stopped${NC}"
    local uptime="n/a"
    local last_poll="n/a"

    if [[ -f "$DAEMON_STATE" ]]; then
        local running
        running=$(jq -r '.running // false' "$DAEMON_STATE")

        if [[ "$running" == "true" ]]; then
            daemon_status="${GREEN}Running${NC}"

            local started_at
            started_at=$(jq -r '.started_at // ""' "$DAEMON_STATE")
            uptime=$(format_uptime "$started_at")

            local last_poll_ts
            last_poll_ts=$(jq -r '.last_poll // ""' "$DAEMON_STATE")
            last_poll=$(time_ago "$last_poll_ts")
        fi
    fi

    # Check for shutdown signal
    if [[ -f "$STOP_FILE" ]]; then
        daemon_status="${YELLOW}Stopping${NC}"
    fi

    echo -e "  ${BOLD}Daemon:${NC} $daemon_status"
    echo -e "  ${BOLD}Uptime:${NC} $uptime"
    echo -e "  ${BOLD}Last Poll:${NC} $last_poll"
    echo ""

    # System State
    echo -e "  ${BOLD}System State:${NC}"
    local ready_issues=$(get_github_counts "loom:issue")
    local building_issues=$(get_github_counts "loom:building")
    local curated_issues=$(get_github_counts "loom:curated")
    local architect_proposals=$(get_github_counts "loom:architect")
    local hermit_proposals=$(get_github_counts "loom:hermit")
    local pending_reviews=$(get_github_counts "loom:review-requested" "pr")
    local ready_to_merge=$(get_github_counts "loom:pr" "pr")

    echo -e "    Ready issues (loom:issue): ${BOLD}$ready_issues${NC}"
    echo -e "    Building (loom:building): ${BOLD}$building_issues${NC}"
    echo -e "    Curated (awaiting approval): ${BOLD}$curated_issues${NC}"
    echo -e "    Proposals pending: ${BOLD}$((architect_proposals + hermit_proposals))${NC} (arch: $architect_proposals, hermit: $hermit_proposals)"
    echo -e "    PRs pending review: ${BOLD}$pending_reviews${NC}"
    echo -e "    PRs ready to merge: ${BOLD}$ready_to_merge${NC}"
    echo ""

    # Shepherds
    echo -e "  ${BOLD}Shepherds:${NC}"
    if [[ -f "$DAEMON_STATE" ]]; then
        local active_count=0
        local total_count=0

        # Count shepherds
        for i in 1 2 3; do
            local shepherd_id="shepherd-$i"
            local issue
            issue=$(jq -r ".shepherds[\"$shepherd_id\"].issue // null" "$DAEMON_STATE" 2>/dev/null)

            ((total_count++)) || true

            if [[ "$issue" != "null" ]] && [[ -n "$issue" ]]; then
                ((active_count++)) || true
            fi
        done

        echo -e "    ${CYAN}$active_count/$total_count active${NC}"
        echo ""

        # List each shepherd
        for i in 1 2 3; do
            local shepherd_id="shepherd-$i"
            local issue
            local started
            local output_file
            local idle_since
            issue=$(jq -r ".shepherds[\"$shepherd_id\"].issue // null" "$DAEMON_STATE" 2>/dev/null)
            started=$(jq -r ".shepherds[\"$shepherd_id\"].started // null" "$DAEMON_STATE" 2>/dev/null)
            output_file=$(jq -r ".shepherds[\"$shepherd_id\"].output_file // null" "$DAEMON_STATE" 2>/dev/null)
            idle_since=$(jq -r ".shepherds[\"$shepherd_id\"].idle_since // null" "$DAEMON_STATE" 2>/dev/null)

            if [[ "$issue" != "null" ]] && [[ -n "$issue" ]]; then
                local duration
                duration=$(format_uptime "$started")

                # Check output file for idle time
                local idle_seconds
                idle_seconds=$(get_file_idle_seconds "$output_file")

                if [[ "$idle_seconds" -ge 0 ]]; then
                    local idle_display
                    idle_display=$(format_seconds "$idle_seconds")
                    echo -e "    ${GREEN}$shepherd_id:${NC} Issue #$issue (${duration}, idle ${idle_display})"
                else
                    echo -e "    ${GREEN}$shepherd_id:${NC} Issue #$issue (${duration})"
                fi
            else
                # Show idle duration for idle shepherds
                if [[ "$idle_since" != "null" ]] && [[ -n "$idle_since" ]]; then
                    local idle_duration
                    idle_duration=$(format_uptime "$idle_since")
                    echo -e "    ${GRAY}$shepherd_id:${NC} idle (${idle_duration})"
                else
                    echo -e "    ${GRAY}$shepherd_id:${NC} idle"
                fi
            fi
        done
    else
        echo -e "    ${GRAY}No daemon state available${NC}"
    fi
    echo ""

    # Support Roles
    echo -e "  ${BOLD}Support Roles:${NC}"
    if [[ -f "$DAEMON_STATE" ]]; then
        for role in architect hermit guide champion; do
            local task_id
            local last_completed
            task_id=$(jq -r ".support_roles[\"$role\"].task_id // null" "$DAEMON_STATE" 2>/dev/null)
            last_completed=$(jq -r ".support_roles[\"$role\"].last_completed // null" "$DAEMON_STATE" 2>/dev/null)

            local role_display
            # Capitalize first letter (works on both macOS and Linux)
            role_display="$(echo "${role:0:1}" | tr '[:lower:]' '[:upper:]')${role:1}"

            if [[ "$task_id" != "null" ]] && [[ -n "$task_id" ]]; then
                echo -e "    ${GREEN}$role_display:${NC} running"
            else
                local last_ago
                last_ago=$(time_ago "$last_completed")
                echo -e "    ${GRAY}$role_display:${NC} idle (last: $last_ago)"
            fi
        done
    else
        echo -e "    ${GRAY}No daemon state available${NC}"
    fi
    echo ""

    # Session Stats
    echo -e "  ${BOLD}Session Statistics:${NC}"
    if [[ -f "$DAEMON_STATE" ]]; then
        local completed_count
        local prs_merged
        completed_count=$(jq -r '.completed_issues | length // 0' "$DAEMON_STATE" 2>/dev/null || echo "0")
        prs_merged=$(jq -r '.total_prs_merged // 0' "$DAEMON_STATE" 2>/dev/null || echo "0")

        echo -e "    Issues completed: ${BOLD}$completed_count${NC}"
        echo -e "    PRs merged: ${BOLD}$prs_merged${NC}"
    else
        echo -e "    ${GRAY}No session data available${NC}"
    fi
    echo ""

    # Layer 3 Actions
    echo -e "  ${BOLD}Layer 3 Actions Available:${NC}"
    echo ""

    # Show pending approvals if any
    if [[ "$architect_proposals" -gt 0 ]] || [[ "$hermit_proposals" -gt 0 ]]; then
        echo -e "    ${YELLOW}Pending Approvals:${NC}"
        if [[ "$architect_proposals" -gt 0 ]]; then
            echo -e "      - View architect proposals: ${CYAN}gh issue list --label loom:architect${NC}"
            echo -e "      - Approve proposal: ${CYAN}gh issue edit <N> --remove-label loom:architect --add-label loom:issue${NC}"
        fi
        if [[ "$hermit_proposals" -gt 0 ]]; then
            echo -e "      - View hermit proposals: ${CYAN}gh issue list --label loom:hermit${NC}"
            echo -e "      - Approve proposal: ${CYAN}gh issue edit <N> --remove-label loom:hermit --add-label loom:issue${NC}"
        fi
        echo ""
    fi

    if [[ "$curated_issues" -gt 0 ]]; then
        echo -e "    ${YELLOW}Curated Issues Awaiting Approval:${NC}"
        echo -e "      - View curated: ${CYAN}gh issue list --label loom:curated${NC}"
        echo -e "      - Approve: ${CYAN}gh issue edit <N> --remove-label loom:curated --add-label loom:issue${NC}"
        echo ""
    fi

    echo -e "    ${YELLOW}Daemon Control:${NC}"
    if [[ -f "$STOP_FILE" ]]; then
        echo -e "      - Cancel shutdown: ${CYAN}rm .loom/stop-daemon${NC}"
    else
        echo -e "      - Stop daemon: ${CYAN}touch .loom/stop-daemon${NC}"
    fi
    echo -e "      - View daemon state: ${CYAN}cat .loom/daemon-state.json | jq${NC}"
    echo ""

    echo -e "${BOLD}${CYAN}=======================================================================${NC}"
    echo ""
}

# Main
main() {
    case "${1:-}" in
        --json)
            output_json
            ;;
        --help|-h)
            show_help
            ;;
        "")
            output_formatted
            ;;
        *)
            echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
            echo "Run 'loom-status.sh --help' for usage" >&2
            exit 1
            ;;
    esac
}

main "$@"
