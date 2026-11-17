#!/bin/bash
# agent-wait.sh - Wait for a tmux Claude agent to finish its task
#
# Thin stub delegating to `loom-daemon agent-wait`.
#
# The Python `loom_tools.agent_wait` implementation was ported to native
# `loom-daemon agent-wait` in epic #4081 Phase 3 family 4 (issue #4415); this
# stub now execs the daemon binary instead of the Python CLI. Flags, the
# `--json` payload shape, and the exit-code contract that loom-start.sh /
# agent-destroy.sh depend on are unchanged:
#
# Exit codes:
#   0 - Agent completed (shell is idle, no claude process)
#   1 - Timeout reached
#   2 - Session not found (or error)
#
# Usage:
#   agent-wait.sh <name> [--timeout <seconds>] [--poll-interval <seconds>]
#                        [--min-session-age <seconds>] [--json]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
# shellcheck source=lib/repo-root.sh
source "$SCRIPT_DIR/lib/repo-root.sh"

REPO_ROOT="$(_find_repo_root "$SCRIPT_DIR" 2>/dev/null || echo "$PWD")"
DAEMON_BIN="$(loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -z "$DAEMON_BIN" ]]; then
    echo "[ERROR] loom-daemon binary not found." >&2
    echo "" >&2
    echo "Build it with: cd loom-daemon && cargo build --release" >&2
    echo "Or set LOOM_DAEMON_BIN to an existing binary." >&2
    exit 2
fi

exec "$DAEMON_BIN" agent-wait "$@"
