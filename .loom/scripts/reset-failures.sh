#!/bin/bash
# reset-failures.sh - Reset failure counters for issues
#
# Clears failure state from ALL three tracking locations:
#   1. .loom/issue-failures.json  (persistent cross-session failure log)
#   2. daemon-state.json blocked_issue_retries  (per-issue retry metadata)
#   3. daemon-state.json recent_failures  (sliding window for systematic failure detection)
#
# Usage:
#   reset-failures.sh <issue-number>     # Reset failures for a specific issue
#   reset-failures.sh --all              # Reset all failure counters
#   reset-failures.sh --signal <issue>   # Send reset signal to running daemon
#   reset-failures.sh --signal --all     # Send reset-all signal to running daemon
#   reset-failures.sh --list             # List current failure entries
#   reset-failures.sh --help             # Show help
#
# When the daemon is running, use --signal to send an IPC signal so the
# daemon's in-memory state is also updated. Without --signal, only the
# on-disk files are modified (the daemon will pick up changes on next restart).

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Find the repository root
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
            if [[ -d "$dir/.loom" ]]; then
                echo "$dir"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository with .loom directory" >&2
    return 1
}

REPO_ROOT=$(find_repo_root)
LOOM_DIR="$REPO_ROOT/.loom"
DAEMON_STATE="$LOOM_DIR/daemon-state.json"
ISSUE_FAILURES="$LOOM_DIR/issue-failures.json"
SIGNALS_DIR="$LOOM_DIR/signals"

show_help() {
    cat <<EOF
${BLUE}reset-failures.sh - Reset failure counters for issues${NC}

${YELLOW}USAGE:${NC}
    reset-failures.sh <issue-number>       Reset failures for one issue
    reset-failures.sh --all                Reset all failure counters
    reset-failures.sh --signal <issue>     Send reset signal to running daemon
    reset-failures.sh --signal --all       Send reset-all signal to running daemon
    reset-failures.sh --list               List current failure entries
    reset-failures.sh --help               Show this help

${YELLOW}DESCRIPTION:${NC}
    Failure state is tracked in three locations:
      1. .loom/issue-failures.json        Persistent cross-session failure log
      2. daemon-state.json                blocked_issue_retries (retry metadata)
      3. daemon-state.json                recent_failures (systematic failure window)

    This script clears ALL three locations atomically.

${YELLOW}EXAMPLES:${NC}
    # Reset failures for issue #42
    reset-failures.sh 42

    # Reset all failures
    reset-failures.sh --all

    # Reset via daemon signal (updates in-memory state too)
    reset-failures.sh --signal 42

    # List current failures
    reset-failures.sh --list

${YELLOW}NOTES:${NC}
    Without --signal, only on-disk files are modified. If the daemon is
    running, its in-memory state will be stale until the next restart.
    Use --signal when the daemon is running to update both disk and memory.
EOF
}

# List current failure entries
list_failures() {
    local has_data=false

    echo -e "${BLUE}Failure tracking state:${NC}"
    echo ""

    # 1. Persistent failure log
    if [[ -f "$ISSUE_FAILURES" ]]; then
        local count
        count=$(jq '.entries | length' "$ISSUE_FAILURES" 2>/dev/null || echo "0")
        if [[ "$count" -gt 0 ]]; then
            has_data=true
            echo -e "  ${YELLOW}issue-failures.json${NC} ($count entries):"
            jq -r '.entries | to_entries[] | "    #\(.key): \(.value.total_failures) failures (\(.value.error_class), phase=\(.value.phase))"' \
                "$ISSUE_FAILURES" 2>/dev/null || true
            echo ""
        fi
    fi

    # 2. Daemon state blocked_issue_retries
    if [[ -f "$DAEMON_STATE" ]]; then
        local retry_count
        retry_count=$(jq '.blocked_issue_retries | length' "$DAEMON_STATE" 2>/dev/null || echo "0")
        if [[ "$retry_count" -gt 0 ]]; then
            has_data=true
            echo -e "  ${YELLOW}daemon-state.json blocked_issue_retries${NC} ($retry_count entries):"
            jq -r '.blocked_issue_retries | to_entries[] | "    #\(.key): retries=\(.value.retry_count // 0), exhausted=\(.value.retry_exhausted // false), class=\(.value.error_class // "unknown")"' \
                "$DAEMON_STATE" 2>/dev/null || true
            echo ""
        fi

        # 3. Recent failures
        local recent_count
        recent_count=$(jq '.recent_failures | length' "$DAEMON_STATE" 2>/dev/null || echo "0")
        if [[ "$recent_count" -gt 0 ]]; then
            has_data=true
            echo -e "  ${YELLOW}daemon-state.json recent_failures${NC} ($recent_count entries):"
            jq -r '.recent_failures[-5:][] | "    #\(.issue): \(.error_class) (phase=\(.phase), at=\(.timestamp))"' \
                "$DAEMON_STATE" 2>/dev/null || true
            if [[ "$recent_count" -gt 5 ]]; then
                echo "    ... and $((recent_count - 5)) more"
            fi
            echo ""
        fi

        # 4. Systematic failure
        local sf_active
        sf_active=$(jq -r '.systematic_failure.active // false' "$DAEMON_STATE" 2>/dev/null || echo "false")
        if [[ "$sf_active" == "true" ]]; then
            has_data=true
            local pattern
            pattern=$(jq -r '.systematic_failure.pattern // "unknown"' "$DAEMON_STATE" 2>/dev/null)
            echo -e "  ${RED}Systematic failure ACTIVE${NC}: pattern=$pattern"
            echo ""
        fi
    fi

    if [[ "$has_data" == "false" ]]; then
        echo -e "  ${GREEN}No failure entries found${NC}"
    fi
}

# Reset failures for a specific issue (on-disk only)
reset_issue() {
    local issue_num="$1"
    local cleared=0

    # 1. Clear from issue-failures.json
    if [[ -f "$ISSUE_FAILURES" ]]; then
        local before
        before=$(jq '.entries | length' "$ISSUE_FAILURES" 2>/dev/null || echo "0")
        temp_file=$(mktemp)
        if jq --arg key "$issue_num" 'del(.entries[$key])' "$ISSUE_FAILURES" > "$temp_file" 2>/dev/null; then
            mv "$temp_file" "$ISSUE_FAILURES"
            local after
            after=$(jq '.entries | length' "$ISSUE_FAILURES" 2>/dev/null || echo "0")
            cleared=$((before - after))
        else
            rm -f "$temp_file"
        fi
    fi

    # 2. Clear from daemon-state.json
    if [[ -f "$DAEMON_STATE" ]]; then
        temp_file=$(mktemp)
        if jq --arg key "$issue_num" --argjson inum "$issue_num" '
            del(.blocked_issue_retries[$key]) |
            .recent_failures = [.recent_failures[] | select(.issue != $inum)] |
            .needs_human_input = [(.needs_human_input // [])[] | select(.type != "exhausted_retry" or .issue != $inum)]
        ' "$DAEMON_STATE" > "$temp_file" 2>/dev/null; then
            mv "$temp_file" "$DAEMON_STATE"
        else
            rm -f "$temp_file"
        fi
    fi

    echo -e "${GREEN}✓ Reset failure tracking for issue #${issue_num}${NC}"
    echo -e "  Cleared ${cleared} entry(ies) from issue-failures.json"
    echo -e "  Cleared blocked_issue_retries and recent_failures entries from daemon-state.json"
}

# Reset all failures (on-disk only)
reset_all() {
    local cleared=0

    # 1. Clear issue-failures.json
    if [[ -f "$ISSUE_FAILURES" ]]; then
        cleared=$(jq '.entries | length' "$ISSUE_FAILURES" 2>/dev/null || echo "0")
        echo '{"entries": {}, "updated_at": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}' > "$ISSUE_FAILURES"
    fi

    # 2. Clear daemon-state.json failure fields
    if [[ -f "$DAEMON_STATE" ]]; then
        temp_file=$(mktemp)
        if jq '
            .blocked_issue_retries = {} |
            .recent_failures = [] |
            .systematic_failure = {} |
            .needs_human_input = [(.needs_human_input // [])[] | select(.type != "exhausted_retry")]
        ' "$DAEMON_STATE" > "$temp_file" 2>/dev/null; then
            mv "$temp_file" "$DAEMON_STATE"
        else
            rm -f "$temp_file"
        fi
    fi

    echo -e "${GREEN}✓ Reset ALL failure tracking${NC}"
    echo -e "  Cleared ${cleared} entry(ies) from issue-failures.json"
    echo -e "  Cleared blocked_issue_retries, recent_failures, and systematic_failure from daemon-state.json"
}

# Send reset signal to running daemon
send_signal() {
    local payload="$1"

    mkdir -p "$SIGNALS_DIR"
    local ts
    ts=$(date +%s%N | cut -c1-13)
    local rand
    rand=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')
    local signal_file="$SIGNALS_DIR/cmd-${ts}-${rand}.json"

    echo "$payload" > "$signal_file"
    echo -e "${GREEN}✓ Reset signal sent to daemon${NC}"
    echo -e "  Signal file: $signal_file"
    echo -e "  The daemon will process this on its next poll cycle"
}

# Main
SIGNAL_MODE=false
ALL_MODE=false
LIST_MODE=false
ISSUE_NUM=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            show_help
            exit 0
            ;;
        --list|-l)
            LIST_MODE=true
            shift
            ;;
        --signal|-s)
            SIGNAL_MODE=true
            shift
            ;;
        --all|-a)
            ALL_MODE=true
            shift
            ;;
        [0-9]*)
            ISSUE_NUM="$1"
            shift
            ;;
        *)
            echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
            echo "Run 'reset-failures.sh --help' for usage" >&2
            exit 1
            ;;
    esac
done

if [[ "$LIST_MODE" == "true" ]]; then
    list_failures
    exit 0
fi

if [[ "$ALL_MODE" == "true" ]]; then
    if [[ "$SIGNAL_MODE" == "true" ]]; then
        send_signal '{"action": "reset_failures", "all": true, "created_at": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}'
    else
        reset_all
    fi
    exit 0
fi

if [[ -n "$ISSUE_NUM" ]]; then
    if [[ "$SIGNAL_MODE" == "true" ]]; then
        send_signal '{"action": "reset_failures", "issue": '"$ISSUE_NUM"', "created_at": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}'
    else
        reset_issue "$ISSUE_NUM"
    fi
    exit 0
fi

echo -e "${RED}Error: Specify an issue number or --all${NC}" >&2
echo "Run 'reset-failures.sh --help' for usage" >&2
exit 1
