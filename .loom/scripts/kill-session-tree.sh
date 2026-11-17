#!/bin/bash
# kill-session-tree.sh - Kill process tree before destroying a tmux session
#
# When tmux kill-session sends SIGHUP, it doesn't propagate across process group
# boundaries. The claude CLI is typically behind a wrapper/timeout chain that
# creates separate process groups, so it survives session destruction as an orphan.
#
# This script kills the entire process tree first, then destroys the session.
#
# Usage:
#   kill-session-tree.sh <session-name> [--force] [--socket <name>]
#
# Options:
#   --force     Use SIGKILL instead of SIGTERM (for force-stop scenarios)
#   --socket    tmux socket name (default: loom)
#
# Can also be sourced for the kill_session_tree() function:
#   source kill-session-tree.sh
#   kill_session_tree "loom-session-name"
#   kill_session_tree "loom-session-name" "--force"

set -euo pipefail

TMUX_SOCKET="${LOOM_TMUX_SOCKET:-loom}"

# Recursively collect all descendant PIDs of a given PID (depth-first)
_collect_descendants() {
    local parent_pid="$1"
    local children
    children=$(pgrep -P "$parent_pid" 2>/dev/null || true)

    for child in $children; do
        # Recurse into grandchildren first (depth-first for bottom-up kill)
        _collect_descendants "$child"
        echo "$child"
    done
}

# Kill the process tree rooted at a tmux session's pane, then destroy the session
#
# Arguments:
#   $1 - tmux session name
#   $2 - optional: "--force" to use SIGKILL instead of SIGTERM
#   $3 - optional: tmux socket name (default: $TMUX_SOCKET)
kill_session_tree() {
    local session_name="$1"
    local force="${2:-}"
    local socket="${3:-$TMUX_SOCKET}"

    # Get the pane PID(s) for this session
    local pane_pids
    pane_pids=$(tmux -L "$socket" list-panes -t "$session_name" -F '#{pane_pid}' 2>/dev/null || true)

    if [[ -n "$pane_pids" ]]; then
        local all_pids=""

        for pane_pid in $pane_pids; do
            # Collect all descendants (depth-first order for bottom-up kill)
            local descendants
            descendants=$(_collect_descendants "$pane_pid")

            if [[ -n "$descendants" ]]; then
                all_pids="${all_pids}${all_pids:+ }${descendants}"
            fi
            # Include the pane shell itself
            all_pids="${all_pids}${all_pids:+ }${pane_pid}"
        done

        if [[ -n "$all_pids" ]]; then
            if [[ "$force" == "--force" ]]; then
                # Force mode: SIGKILL immediately
                # shellcheck disable=SC2086
                kill -9 $all_pids 2>/dev/null || true
            else
                # Graceful mode: SIGTERM first
                # shellcheck disable=SC2086
                kill -15 $all_pids 2>/dev/null || true

                # Brief wait for processes to terminate
                sleep 1

                # Escalate to SIGKILL for any survivors
                for pid in $all_pids; do
                    if kill -0 "$pid" 2>/dev/null; then
                        kill -9 "$pid" 2>/dev/null || true
                    fi
                done
            fi
        fi
    fi

    # Now destroy the tmux session
    tmux -L "$socket" kill-session -t "$session_name" 2>/dev/null || true
}

# Sweep for orphaned claude processes with no controlling terminal (TTY ??)
# These are processes that survived session destruction somehow.
#
# Arguments:
#   $1 - optional: "--force" to use SIGKILL instead of SIGTERM
sweep_orphaned_claude_processes() {
    local force="${1:-}"

    # Find claude processes with no controlling terminal
    # The [c] trick in grep prevents matching the grep process itself
    local orphan_pids
    orphan_pids=$(ps aux | grep '[c]laude' | awk '$7 == "??" {print $2}' || true)

    if [[ -z "$orphan_pids" ]]; then
        return 0
    fi

    local count
    count=$(echo "$orphan_pids" | wc -l | tr -d ' ')

    if [[ "$force" == "--force" ]]; then
        echo "  Killing $count orphaned claude process(es) (SIGKILL)..." >&2
        echo "$orphan_pids" | xargs kill -9 2>/dev/null || true
    else
        echo "  Terminating $count orphaned claude process(es) (SIGTERM)..." >&2
        echo "$orphan_pids" | xargs kill -15 2>/dev/null || true

        # Wait for graceful termination
        sleep 2

        # Check for survivors and escalate
        local survivors
        survivors=$(ps aux | grep '[c]laude' | awk '$7 == "??" {print $2}' || true)
        if [[ -n "$survivors" ]]; then
            local surv_count
            surv_count=$(echo "$survivors" | wc -l | tr -d ' ')
            echo "  Escalating to SIGKILL for $surv_count stubborn process(es)..." >&2
            echo "$survivors" | xargs kill -9 2>/dev/null || true
        fi
    fi
}

# If run directly (not sourced), execute kill_session_tree with arguments
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    session_name=""
    force=""
    socket="$TMUX_SOCKET"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force|-f) force="--force"; shift ;;
            --socket|-s) socket="$2"; shift 2 ;;
            --help|-h)
                echo "Usage: kill-session-tree.sh <session-name> [--force] [--socket <name>]"
                echo ""
                echo "Kill process tree before destroying a tmux session."
                echo ""
                echo "Options:"
                echo "  --force     Use SIGKILL instead of SIGTERM"
                echo "  --socket    tmux socket name (default: loom)"
                exit 0
                ;;
            -*) echo "Unknown option: $1" >&2; exit 1 ;;
            *) session_name="$1"; shift ;;
        esac
    done

    if [[ -z "$session_name" ]]; then
        echo "Error: session name required" >&2
        echo "Usage: kill-session-tree.sh <session-name> [--force]" >&2
        exit 1
    fi

    kill_session_tree "$session_name" "$force" "$socket"
fi
