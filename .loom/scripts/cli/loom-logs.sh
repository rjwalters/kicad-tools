#!/usr/bin/env bash
# loom logs - Tail agent output
#
# Usage:
#   loom logs <agent-name>        Tail specific agent's output
#   loom logs --all               Tail all agent logs (interleaved)
#   loom logs --list              List available log files
#   loom logs -n <lines>          Show last N lines (default: 50)
#   loom logs --help              Show help
#
# Examples:
#   loom logs shepherd-1          Tail shepherd-1 output
#   loom logs terminal-1 -n 100   Show last 100 lines
#   loom logs --all               Follow all agent logs

set -euo pipefail

# Find repository root
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
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
        dir="$(dirname "$dir")"
    done
    echo ""
}

REPO_ROOT=$(find_repo_root)

# Default log directory
LOG_DIR="/tmp"

# ANSI colors
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    GRAY='\033[0;90m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    GRAY=''
    BOLD=''
    NC=''
fi

# Show help
show_help() {
    cat <<EOF
${BOLD}loom logs - Tail agent output${NC}

${YELLOW}USAGE:${NC}
    loom logs <agent-name>        Tail specific agent's output
    loom logs --all               Tail all agent logs (interleaved)
    loom logs --list              List available log files
    loom logs -n <lines>          Show last N lines (default: 50)
    loom logs --follow, -f        Follow log output (default for single agent)
    loom logs --no-follow         Show lines but don't follow
    loom logs --help              Show this help

${YELLOW}EXAMPLES:${NC}
    loom logs shepherd-1          Tail shepherd-1 output (follows)
    loom logs terminal-1 -n 100   Show last 100 lines then follow
    loom logs --all               Follow all agent logs interleaved
    loom logs shepherd-1 --no-follow  Just show last lines, don't follow

${YELLOW}LOG LOCATIONS:${NC}
    Agent output is captured to:
      /tmp/loom-<agent-name>.out  e.g., /tmp/loom-shepherd-1.out

    Daemon log:
      .loom/daemon.log

${YELLOW}TIPS:${NC}
    Press Ctrl+C to stop following logs
    Use --list to see all available log files
EOF
}

# List available log files
list_logs() {
    echo -e "${BOLD}Available Loom log files:${NC}"
    echo ""

    local found=false

    # Check for agent output files
    for logfile in "$LOG_DIR"/loom-*.out; do
        if [[ -f "$logfile" ]]; then
            found=true
            local name
            name=$(basename "$logfile" .out | sed 's/^loom-//')
            local size
            size=$(du -h "$logfile" | cut -f1)
            local modified
            modified=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$logfile" 2>/dev/null || stat -c "%y" "$logfile" 2>/dev/null | cut -d. -f1)
            echo -e "  ${GREEN}$name${NC}  ${GRAY}($size, modified $modified)${NC}"
        fi
    done

    # Check for daemon log
    if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/daemon.log" ]]; then
        found=true
        local size
        size=$(du -h "$REPO_ROOT/.loom/daemon.log" | cut -f1)
        echo -e "  ${CYAN}daemon${NC}  ${GRAY}($size) - .loom/daemon.log${NC}"
    fi

    if [[ "$found" == "false" ]]; then
        echo -e "${YELLOW}No log files found${NC}"
        echo ""
        echo "Start agents with: ./loom start"
    else
        echo ""
        echo -e "${CYAN}To view:${NC} loom logs <name>"
    fi
}

# Get log file path for an agent
get_log_path() {
    local agent_name="$1"

    # Special case: daemon log
    if [[ "$agent_name" == "daemon" ]]; then
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/daemon.log" ]]; then
            echo "$REPO_ROOT/.loom/daemon.log"
            return 0
        fi
        return 1
    fi

    # Try with loom- prefix first
    local logfile="$LOG_DIR/loom-$agent_name.out"
    if [[ -f "$logfile" ]]; then
        echo "$logfile"
        return 0
    fi

    # Try without loom- prefix (in case full name was given)
    if [[ "$agent_name" =~ ^loom- ]]; then
        local short_name="${agent_name#loom-}"
        logfile="$LOG_DIR/loom-$short_name.out"
        if [[ -f "$logfile" ]]; then
            echo "$logfile"
            return 0
        fi
    fi

    # Also try the raw name
    logfile="$LOG_DIR/$agent_name.out"
    if [[ -f "$logfile" ]]; then
        echo "$logfile"
        return 0
    fi

    return 1
}

# Strip ANSI escape codes (optional, for cleaner output)
strip_ansi() {
    sed 's/\x1b\[[0-9;]*m//g'
}

# Main logic
main() {
    local agent_name=""
    local follow=true
    local lines=50
    local show_all=false
    local strip_colors=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                exit 0
                ;;
            --list|-l)
                list_logs
                exit 0
                ;;
            --all|-a)
                show_all=true
                shift
                ;;
            --follow|-f)
                follow=true
                shift
                ;;
            --no-follow)
                follow=false
                shift
                ;;
            -n)
                if [[ -z "${2:-}" ]]; then
                    echo -e "${RED}Error: -n requires a number${NC}" >&2
                    exit 1
                fi
                lines="$2"
                shift 2
                ;;
            --strip-colors)
                strip_colors=true
                shift
                ;;
            -*)
                echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
                echo "Use 'loom logs --help' for usage" >&2
                exit 1
                ;;
            *)
                agent_name="$1"
                shift
                ;;
        esac
    done

    # Show all logs interleaved
    if [[ "$show_all" == "true" ]]; then
        local log_files=()

        # Collect all loom log files
        for logfile in "$LOG_DIR"/loom-*.out; do
            if [[ -f "$logfile" ]]; then
                log_files+=("$logfile")
            fi
        done

        # Add daemon log if exists
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/daemon.log" ]]; then
            log_files+=("$REPO_ROOT/.loom/daemon.log")
        fi

        if [[ ${#log_files[@]} -eq 0 ]]; then
            echo -e "${YELLOW}No log files found${NC}" >&2
            echo "Start agents with: ./loom start" >&2
            exit 1
        fi

        echo -e "${CYAN}Following ${#log_files[@]} log file(s)...${NC}"
        echo -e "${GRAY}Press Ctrl+C to stop${NC}"
        echo ""

        if [[ "$follow" == "true" ]]; then
            # Use tail -f with file headers
            exec tail -f "${log_files[@]}"
        else
            # Show last lines from each
            for logfile in "${log_files[@]}"; do
                local name
                name=$(basename "$logfile")
                echo -e "${BOLD}==> $name <==${NC}"
                tail -n "$lines" "$logfile"
                echo ""
            done
        fi
        exit 0
    fi

    # Single agent log
    if [[ -z "$agent_name" ]]; then
        echo -e "${RED}Error: Agent name required${NC}" >&2
        echo "Usage: loom logs <agent-name>" >&2
        echo ""
        echo "Use 'loom logs --list' to see available log files" >&2
        exit 1
    fi

    # Get log file path
    local logfile
    if ! logfile=$(get_log_path "$agent_name"); then
        echo -e "${RED}Error: Log file not found for '$agent_name'${NC}" >&2
        echo ""
        echo "Available log files:"
        for lf in "$LOG_DIR"/loom-*.out; do
            if [[ -f "$lf" ]]; then
                local name
                name=$(basename "$lf" .out | sed 's/^loom-//')
                echo "  $name"
            fi
        done
        if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.loom/daemon.log" ]]; then
            echo "  daemon"
        fi
        exit 1
    fi

    echo -e "${CYAN}Showing logs for: $agent_name${NC}"
    echo -e "${GRAY}File: $logfile${NC}"
    if [[ "$follow" == "true" ]]; then
        echo -e "${GRAY}Press Ctrl+C to stop${NC}"
    fi
    echo ""

    if [[ "$follow" == "true" ]]; then
        if [[ "$strip_colors" == "true" ]]; then
            tail -n "$lines" -f "$logfile" | strip_ansi
        else
            exec tail -n "$lines" -f "$logfile"
        fi
    else
        if [[ "$strip_colors" == "true" ]]; then
            tail -n "$lines" "$logfile" | strip_ansi
        else
            tail -n "$lines" "$logfile"
        fi
    fi
}

main "$@"
