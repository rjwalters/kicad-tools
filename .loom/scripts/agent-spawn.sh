#!/bin/bash
# agent-spawn.sh - Thin stub delegating to `loom-daemon agent-spawn`.
#
# This script preserves backward compatibility for callers that invoke
# agent-spawn.sh directly (loom-start.sh, loom-scale.sh, operators).
#
# The Python `loom_tools.agent_spawn` implementation was ported to native
# `loom-daemon agent-spawn` in epic #4081 Phase 3 family 4 (issue #4415); this
# stub now execs the daemon binary instead of the Python CLI. All flags, exit
# codes, and the `--json` payload shape forward unchanged:
#
#   0 - Success
#   1 - Error (or `--check <name>` found no session)
#
# Usage is unchanged — all flags are forwarded as-is:
#   agent-spawn.sh --role <role> --name <name> [--args "<args>"] [--worktree <path>]
#                  [--on-demand] [--fresh] [--wait [--timeout <s>]] [--json]
#   agent-spawn.sh --check <name>
#   agent-spawn.sh --list
#   agent-spawn.sh --help

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
    exit 1
fi

exec "$DAEMON_BIN" agent-spawn "$@"
