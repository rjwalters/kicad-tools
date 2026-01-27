#!/bin/bash

# status.sh - Agent status reporting for Loom
#
# Usage:
#   status.sh report <agent-id> <state> [issue-number] [details]  - Report agent status
#   status.sh get <agent-id>                                       - Get agent's current status
#   status.sh list                                                 - List all agent statuses
#   status.sh clear <agent-id|all>                                 - Clear status file(s)
#   status.sh --help                                               - Show help
#
# States: idle, working, blocked, stopping
#
# Agents should call 'status.sh report' when their state changes.
# Status files auto-expire (marked stale) after 5 minutes of no updates.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
GRAY='\033[0;90m'
NC='\033[0m' # No Color

# Stale timeout in seconds (5 minutes)
STALE_TIMEOUT=300

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
STATUS_DIR="$REPO_ROOT/.loom/status"

# Ensure status directory exists
ensure_status_dir() {
    if [[ ! -d "$STATUS_DIR" ]]; then
        mkdir -p "$STATUS_DIR"
    fi
}

# Show help
show_help() {
    cat <<EOF
${BLUE}status.sh - Agent status reporting for Loom${NC}

${YELLOW}USAGE:${NC}
    status.sh report <agent-id> <state> [issue-number] [details]
    status.sh get <agent-id>
    status.sh list
    status.sh clear <agent-id|all>
    status.sh --help

${YELLOW}STATES:${NC}
    idle      - Agent is waiting for work
    working   - Agent is actively working on an issue
    blocked   - Agent is blocked and needs help
    stopping  - Agent is completing current work before shutdown

${YELLOW}EXAMPLES:${NC}
    # Report that builder-1 is working on issue #123
    status.sh report builder-1 working 123 "Implementing feature"

    # Report that builder-1 is idle
    status.sh report builder-1 idle

    # Report that builder-1 is blocked
    status.sh report builder-1 blocked 123 "Waiting for dependency #456"

    # Get status of a specific agent
    status.sh get builder-1

    # List all agent statuses
    status.sh list

    # Clear a specific agent's status
    status.sh clear builder-1

    # Clear all statuses
    status.sh clear all

${YELLOW}STATUS FILE FORMAT:${NC}
    Status files are stored in .loom/status/<agent-id>.json:

    {
      "agent_id": "builder-1",
      "role": "builder",
      "state": "working",
      "issue": 123,
      "details": "Implementing feature",
      "updated_at": "2025-01-23T10:00:00Z"
    }

${YELLOW}STALE DETECTION:${NC}
    Statuses not updated within ${STALE_TIMEOUT} seconds are marked as stale.
    This helps detect agents that have crashed or become unresponsive.

${YELLOW}INTEGRATION:${NC}
    Agents should update their status at key points:

    # When starting work on an issue
    ./.loom/scripts/status.sh report "\$AGENT_ID" working "\$ISSUE_NUMBER" "Starting implementation"

    # When completing work
    ./.loom/scripts/status.sh report "\$AGENT_ID" idle

    # When blocked
    ./.loom/scripts/status.sh report "\$AGENT_ID" blocked "\$ISSUE_NUMBER" "Reason for block"

    # When shutting down gracefully
    ./.loom/scripts/status.sh report "\$AGENT_ID" stopping
EOF
}

# Extract role from agent_id (e.g., "builder-1" -> "builder")
extract_role() {
    local agent_id="$1"
    echo "$agent_id" | sed 's/-[0-9]*$//'
}

# Report agent status
report_status() {
    local agent_id="$1"
    local state="$2"
    local issue="${3:-}"
    local details="${4:-}"

    # Validate state
    case "$state" in
        idle|working|blocked|stopping)
            ;;
        *)
            echo -e "${RED}Error: Invalid state '$state'. Must be: idle, working, blocked, or stopping${NC}" >&2
            exit 1
            ;;
    esac

    ensure_status_dir

    local role=$(extract_role "$agent_id")
    local timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local status_file="$STATUS_DIR/$agent_id.json"

    # Build JSON (using printf to avoid jq dependency)
    local json="{"
    json+="\"agent_id\":\"$agent_id\","
    json+="\"role\":\"$role\","
    json+="\"state\":\"$state\","

    if [[ -n "$issue" ]]; then
        json+="\"issue\":$issue,"
    else
        json+="\"issue\":null,"
    fi

    if [[ -n "$details" ]]; then
        # Escape quotes in details
        local escaped_details="${details//\"/\\\"}"
        json+="\"details\":\"$escaped_details\","
    else
        json+="\"details\":null,"
    fi

    json+="\"updated_at\":\"$timestamp\""
    json+="}"

    echo "$json" > "$status_file"

    echo -e "${GREEN}✓ Status updated: $agent_id${NC}"
    echo -e "  State: ${YELLOW}$state${NC}"
    if [[ -n "$issue" ]]; then
        echo -e "  Issue: #$issue"
    fi
    if [[ -n "$details" ]]; then
        echo -e "  Details: $details"
    fi
}

# Get agent status
get_status() {
    local agent_id="$1"
    local status_file="$STATUS_DIR/$agent_id.json"

    if [[ ! -f "$status_file" ]]; then
        echo -e "${YELLOW}⚠ No status found for: $agent_id${NC}"
        exit 1
    fi

    # Read and display status
    local content=$(cat "$status_file")

    # Check if stale
    local updated_at=$(echo "$content" | grep -o '"updated_at":"[^"]*"' | cut -d'"' -f4)
    local is_stale=$(check_stale "$updated_at")

    echo -e "${BLUE}Status for $agent_id:${NC}"

    if [[ "$is_stale" == "true" ]]; then
        echo -e "  ${RED}⚠ STALE (not updated in >$((STALE_TIMEOUT/60)) minutes)${NC}"
    fi

    # Parse and display fields
    local state=$(echo "$content" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
    local role=$(echo "$content" | grep -o '"role":"[^"]*"' | cut -d'"' -f4)
    local issue=$(echo "$content" | grep -o '"issue":[0-9]*' | cut -d':' -f2)
    local details=$(echo "$content" | grep -o '"details":"[^"]*"' | cut -d'"' -f4)

    echo -e "  Role: $role"

    case "$state" in
        idle)
            echo -e "  State: ${GREEN}$state${NC}"
            ;;
        working)
            echo -e "  State: ${BLUE}$state${NC}"
            ;;
        blocked)
            echo -e "  State: ${RED}$state${NC}"
            ;;
        stopping)
            echo -e "  State: ${YELLOW}$state${NC}"
            ;;
        *)
            echo -e "  State: $state"
            ;;
    esac

    if [[ -n "$issue" && "$issue" != "null" ]]; then
        echo -e "  Issue: #$issue"
    fi

    if [[ -n "$details" && "$details" != "null" ]]; then
        echo -e "  Details: $details"
    fi

    echo -e "  Updated: $updated_at"
}

# Check if a timestamp is stale
check_stale() {
    local updated_at="$1"

    # Convert ISO timestamp to epoch
    local updated_epoch
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS
        updated_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$updated_at" "+%s" 2>/dev/null || echo "0")
    else
        # Linux
        updated_epoch=$(date -d "$updated_at" "+%s" 2>/dev/null || echo "0")
    fi

    local now_epoch=$(date +%s)
    local age=$((now_epoch - updated_epoch))

    if [[ $age -gt $STALE_TIMEOUT ]]; then
        echo "true"
    else
        echo "false"
    fi
}

# List all agent statuses
list_statuses() {
    ensure_status_dir

    local found=0
    echo -e "${BLUE}Agent Statuses:${NC}"
    echo ""

    # JSON output mode check
    local json_output=false
    if [[ "${1:-}" == "--json" ]]; then
        json_output=true
        echo "["
        local first=true
    fi

    for status_file in "$STATUS_DIR"/*.json; do
        if [[ -f "$status_file" ]]; then
            found=1
            local content=$(cat "$status_file")
            local agent_id=$(echo "$content" | grep -o '"agent_id":"[^"]*"' | cut -d'"' -f4)
            local state=$(echo "$content" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
            local role=$(echo "$content" | grep -o '"role":"[^"]*"' | cut -d'"' -f4)
            local issue=$(echo "$content" | grep -o '"issue":[0-9]*' | cut -d':' -f2)
            local updated_at=$(echo "$content" | grep -o '"updated_at":"[^"]*"' | cut -d'"' -f4)
            local is_stale=$(check_stale "$updated_at")

            if [[ "$json_output" == "true" ]]; then
                if [[ "$first" == "true" ]]; then
                    first=false
                else
                    echo ","
                fi
                # Add stale field to JSON
                local stale_json="false"
                if [[ "$is_stale" == "true" ]]; then
                    stale_json="true"
                fi
                echo "$content" | sed 's/}$/,"stale":'$stale_json'}/'
            else
                # Pretty print
                local state_color
                case "$state" in
                    idle) state_color="${GREEN}" ;;
                    working) state_color="${BLUE}" ;;
                    blocked) state_color="${RED}" ;;
                    stopping) state_color="${YELLOW}" ;;
                    *) state_color="${NC}" ;;
                esac

                if [[ "$is_stale" == "true" ]]; then
                    echo -e "  ${GRAY}● $agent_id${NC} ${RED}[STALE]${NC}"
                else
                    echo -e "  ${state_color}● $agent_id${NC}"
                fi
                echo -e "    Role: $role | State: ${state_color}$state${NC}"
                if [[ -n "$issue" && "$issue" != "null" ]]; then
                    echo -e "    Issue: #$issue"
                fi
                echo -e "    ${GRAY}Updated: $updated_at${NC}"
                echo ""
            fi
        fi
    done

    if [[ "$json_output" == "true" ]]; then
        echo "]"
    elif [[ $found -eq 0 ]]; then
        echo -e "  ${GRAY}No agents have reported status${NC}"
    fi
}

# Clear status file(s)
clear_status() {
    local target="$1"

    if [[ "$target" == "all" ]]; then
        # Clear all status files
        if [[ -d "$STATUS_DIR" ]]; then
            local count=0
            for status_file in "$STATUS_DIR"/*.json; do
                if [[ -f "$status_file" ]]; then
                    rm -f "$status_file"
                    ((count++)) || true
                fi
            done
            if [[ $count -gt 0 ]]; then
                echo -e "${GREEN}✓ Cleared $count status file(s)${NC}"
            else
                echo -e "${BLUE}ℹ No status files to clear${NC}"
            fi
        else
            echo -e "${BLUE}ℹ No status directory exists${NC}"
        fi
    else
        # Clear specific agent status
        local status_file="$STATUS_DIR/$target.json"
        if [[ -f "$status_file" ]]; then
            rm -f "$status_file"
            echo -e "${GREEN}✓ Cleared status for: $target${NC}"
        else
            echo -e "${BLUE}ℹ No status found for: $target${NC}"
        fi
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
        report)
            if [[ $# -lt 2 ]]; then
                echo -e "${RED}Error: 'report' requires agent-id and state${NC}" >&2
                echo "Usage: status.sh report <agent-id> <state> [issue-number] [details]" >&2
                exit 1
            fi
            report_status "$1" "$2" "${3:-}" "${4:-}"
            ;;
        get)
            if [[ $# -lt 1 ]]; then
                echo -e "${RED}Error: 'get' requires an agent-id${NC}" >&2
                echo "Usage: status.sh get <agent-id>" >&2
                exit 1
            fi
            get_status "$1"
            ;;
        list)
            list_statuses "${1:-}"
            ;;
        clear)
            if [[ $# -lt 1 ]]; then
                echo -e "${RED}Error: 'clear' requires a target (agent-id or 'all')${NC}" >&2
                echo "Usage: status.sh clear <agent-id|all>" >&2
                exit 1
            fi
            clear_status "$1"
            ;;
        --help|-h|help)
            show_help
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}" >&2
            echo "Run 'status.sh --help' for usage" >&2
            exit 1
            ;;
    esac
}

main "$@"
