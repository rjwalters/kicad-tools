#!/usr/bin/env bash
# Convenience wrapper to start the Loom daemon from your repository root.
#
# Usage:
#   ./loom.sh               # Start in normal mode
#   ./loom.sh --merge       # Force/merge mode (auto-promote + auto-merge)
#   ./loom.sh --status      # Check if daemon is running
#   ./loom.sh --stop        # Send graceful shutdown signal
#   ./loom.sh --help        # Show all options
#
# This script is a thin wrapper around .loom/scripts/start-daemon.sh.
# Run it from a terminal OUTSIDE Claude Code so that worker sessions
# (builder, judge, etc.) are not spawned as Claude Code descendants.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_DAEMON="$SCRIPT_DIR/.loom/scripts/start-daemon.sh"

if [[ ! -x "$START_DAEMON" ]]; then
    echo "Error: Loom daemon script not found at $START_DAEMON" >&2
    echo "Is Loom installed in this repository?" >&2
    exit 1
fi

exec "$START_DAEMON" "$@"
