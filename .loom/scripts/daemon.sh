#!/usr/bin/env bash
# Unified Loom daemon management script.
#
# The daemon starts independently of any Claude Code session.  When invoked
# from the shell (not from within Claude Code), its process tree is:
#
#   init/launchd -> loom-daemon -> loom-shepherd.sh -> claude /builder
#
# This means worker claude sessions are NOT descendants of any Claude Code
# session, which avoids the nested-Claude-Code spawning restrictions that
# cause shepherd spawning failures when /loom runs the daemon directly.
#
# Usage:
#   ./.loom/scripts/daemon.sh <command> [OPTIONS]
#
# Commands:
#   start, up         Start the daemon (default if no command given)
#   stop, down         Stop the daemon gracefully
#   restart            Stop then start the daemon
#   status             Print daemon status and exit
#
# Start options:
#   --auto-build, -a   Enable automatic shepherd spawning from loom:issue queue
#   --timeout-min N    Stop daemon after N minutes (0 = no timeout)
#   --debug, -d        Enable debug logging
#
# Stop options:
#   --wait [N]         Wait up to N seconds for daemon to exit (default: 30)
#   --force            Send SIGTERM immediately instead of graceful stop
#
# General options:
#   --help, -h         Show this help
#
# Modes:
#   (no flags)         Support-only: judge, champion, doctor, auditor, curator,
#                      guide run autonomously. Shepherds NOT auto-spawned.
#                      Use /shepherd <N> to shepherd specific issues manually.
#   --auto-build       Also auto-spawn shepherds from loom:issue queue.
#                      Use /loom --merge in Claude Code for force mode on top.
#
# After starting, the /loom Claude Code skill detects the running daemon
# via .loom/daemon-loop.pid and operates as a signal-writer + observer.
# Write spawn_shepherd / stop / etc. signals to .loom/signals/ to control
# the daemon from within Claude Code without spawning subprocesses.
#
# Examples:
#   ./.loom/scripts/daemon.sh start                # Start in support-only mode
#   ./.loom/scripts/daemon.sh up --auto-build      # Start with auto-build
#   ./.loom/scripts/daemon.sh stop                 # Graceful shutdown
#   ./.loom/scripts/daemon.sh down --force         # Immediate SIGTERM
#   ./.loom/scripts/daemon.sh restart              # Stop + start
#   ./.loom/scripts/daemon.sh restart --auto-build # Stop + start with auto-build
#   ./.loom/scripts/daemon.sh status               # Check if running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PIDFILE="$REPO_ROOT/.loom/daemon-loop.pid"
LOGFILE="$REPO_ROOT/.loom/daemon.log"
STOP_SIGNAL="$REPO_ROOT/.loom/stop-daemon"

# ── Parse command ────────────────────────────────────────────────────────────
COMMAND=""
ARGS=()
WAIT=false
WAIT_SECS=30
FORCE_STOP=false

# Extract the first non-flag argument as the command
if [[ $# -gt 0 ]]; then
    case "$1" in
        start|up)     COMMAND="start"; shift;;
        stop|down)    COMMAND="stop"; shift;;
        restart)      COMMAND="restart"; shift;;
        status)       COMMAND="status"; shift;;
        --help|-h)
            sed -n '/^# Usage:/,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \?//'
            exit 0
            ;;
        -*)           COMMAND="start";;  # Flag without command → default to start
        *)
            echo "Unknown command: $1" >&2
            echo "Usage: daemon.sh {start|stop|restart|status} [OPTIONS]" >&2
            echo "Aliases: up=start, down=stop" >&2
            exit 1
            ;;
    esac
else
    COMMAND="start"  # No args → default to start
fi

# ── Parse remaining options ──────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f|--merge|-m)
            if [[ "$COMMAND" == "stop" ]]; then
                FORCE_STOP=true
            else
                echo "ERROR: --merge/--force is not a daemon startup flag." >&2
                echo "Start the daemon normally, then use /loom --merge in Claude Code." >&2
                exit 1
            fi
            ;;
        --wait)
            WAIT=true
            if [[ "${2:-}" =~ ^[0-9]+$ ]]; then
                WAIT_SECS="$2"
                shift
            fi
            ;;
        --help|-h)
            sed -n '/^# Usage:/,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            ARGS+=("$1")
            ;;
    esac
    shift
done

# ── Helper: check if daemon is running ───────────────────────────────────────
daemon_pid() {
    if [[ -f "$PIDFILE" ]]; then
        local pid
        pid="$(cat "$PIDFILE")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        else
            rm -f "$PIDFILE"
            return 1
        fi
    fi
    return 1
}

# ── Command: status ──────────────────────────────────────────────────────────
cmd_status() {
    if command -v loom-status >/dev/null 2>&1; then
        loom-status
        exit 0
    fi
    local pid
    if pid=$(daemon_pid); then
        echo "Daemon running (PID $pid)"
        exit 0
    else
        echo "Daemon not running"
        exit 1
    fi
}

# ── Command: stop ────────────────────────────────────────────────────────────
cmd_stop() {
    local pid
    if ! pid=$(daemon_pid); then
        echo "Daemon not running"
        return 0
    fi

    if "$FORCE_STOP"; then
        echo "Sending SIGTERM to daemon (PID $pid)..."
        kill -TERM "$pid" 2>/dev/null || true
    else
        echo "Stopping daemon (PID $pid)..."
        touch "$STOP_SIGNAL"
    fi

    if "$WAIT" || "$FORCE_STOP"; then
        echo "Waiting up to ${WAIT_SECS}s for daemon to exit..."
        local elapsed=0
        while kill -0 "$pid" 2>/dev/null; do
            sleep 1
            elapsed=$((elapsed + 1))
            if [[ $elapsed -ge $WAIT_SECS ]]; then
                echo "Timed out waiting for daemon (PID $pid) to exit"
                echo "Use 'daemon.sh stop --force' to send SIGTERM immediately"
                return 1
            fi
        done
        echo "Daemon exited after ${elapsed}s"
        rm -f "$PIDFILE"
    else
        # Default: wait briefly for the daemon to pick up the signal
        echo "Waiting for daemon to exit..."
        local elapsed=0
        while kill -0 "$pid" 2>/dev/null; do
            sleep 1
            elapsed=$((elapsed + 1))
            if [[ $elapsed -ge $WAIT_SECS ]]; then
                echo "Daemon still running after ${elapsed}s (PID $pid)"
                echo "Use 'daemon.sh stop --force' to send SIGTERM immediately"
                return 1
            fi
        done
        echo "Daemon exited after ${elapsed}s"
        rm -f "$PIDFILE"
    fi
}

# ── Command: start ───────────────────────────────────────────────────────────
cmd_start() {
    local pid
    if pid=$(daemon_pid); then
        echo "Daemon already running (PID $pid)"
        echo "Use 'daemon.sh status' to check, 'daemon.sh stop' to shut down."
        return 0
    fi

    # Ensure required directories exist
    mkdir -p "$REPO_ROOT/.loom/logs" \
             "$REPO_ROOT/.loom/signals" \
             "$REPO_ROOT/.loom/progress"

    # Remove any leftover stop signal from a previous session
    rm -f "$STOP_SIGNAL"

    # Locate loom-daemon executable
    local LOOM_DAEMON_CMD=""

    if command -v loom-daemon &>/dev/null; then
        LOOM_DAEMON_CMD="loom-daemon"
    else
        local DAEMON_SH="$SCRIPT_DIR/loom-daemon.sh"
        if [[ -x "$DAEMON_SH" ]]; then
            LOOM_DAEMON_CMD="$DAEMON_SH"
        else
            local LOOM_TOOLS_SRC="$REPO_ROOT/loom-tools/src"
            if [[ -d "$LOOM_TOOLS_SRC" ]]; then
                LOOM_DAEMON_CMD="env PYTHONPATH=$LOOM_TOOLS_SRC:${PYTHONPATH:-} python3 -m loom_tools.daemon_v2.cli"
            else
                echo "ERROR: Cannot locate loom-daemon executable." >&2
                echo "Run: pip install -e $REPO_ROOT/loom-tools" >&2
                return 1
            fi
        fi
    fi

    echo "Starting Loom daemon..."
    echo "  Log:  $LOGFILE"
    echo "  PID:  $PIDFILE"
    echo "  Mode: ${ARGS[*]:-normal}"

    # Append a startup marker to the log
    {
        echo ""
        echo "========================================"
        echo " daemon.sh start: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo " Args: ${ARGS[*]:-<none>}"
        echo "========================================"
    } >> "$LOGFILE" 2>/dev/null || true

    # shellcheck disable=SC2086
    nohup $LOOM_DAEMON_CMD "${ARGS[@]+"${ARGS[@]}"}" \
        >> "$LOGFILE" 2>&1 &
    local DAEMON_PID=$!

    echo "Daemon started (PID $DAEMON_PID)"
    echo ""
    echo "Monitor:  tail -f $LOGFILE"
    echo "Status:   $0 status"
    echo "Stop:     $0 stop"
    echo ""
    echo "Start /loom in Claude Code to begin orchestration:"
    echo "  /loom"
    echo "  /loom --merge   # force mode: auto-promote + auto-merge"
    echo ""
    echo "To enable automatic shepherd spawning, restart with --auto-build:"
    echo "  $0 restart --auto-build"
}

# ── Command: restart ─────────────────────────────────────────────────────────
cmd_restart() {
    local was_running=false
    if daemon_pid >/dev/null 2>&1; then
        was_running=true
        cmd_stop
    fi

    if "$was_running"; then
        echo ""
    fi

    cmd_start
}

# ── Dispatch ─────────────────────────────────────────────────────────────────
case "$COMMAND" in
    status)   cmd_status;;
    stop)     cmd_stop;;
    start)    cmd_start;;
    restart)  cmd_restart;;
esac
