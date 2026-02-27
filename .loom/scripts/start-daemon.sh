#!/usr/bin/env bash
# Thin wrapper — delegates to daemon.sh start.
# Kept for backwards compatibility.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Map legacy flags to daemon.sh subcommands
for arg in "$@"; do
    case "$arg" in
        --status) exec "$SCRIPT_DIR/daemon.sh" status;;
        --stop)   exec "$SCRIPT_DIR/daemon.sh" stop;;
    esac
done

exec "$SCRIPT_DIR/daemon.sh" start "$@"
