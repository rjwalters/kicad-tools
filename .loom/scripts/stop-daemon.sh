#!/usr/bin/env bash
# Thin wrapper — delegates to daemon.sh stop.
# Kept for backwards compatibility.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/daemon.sh" stop "$@"
