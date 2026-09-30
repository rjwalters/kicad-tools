#!/usr/bin/env bash
# Compatibility entry point; runtime selection and launch behavior live in Rust.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
# shellcheck source=lib/loom-tools.sh
source "$SCRIPT_DIR/lib/loom-tools.sh"
REPO_ROOT="$(_find_repo_root "$SCRIPT_DIR" 2>/dev/null || echo "$PWD")"
DAEMON_BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT")"
if [[ -z "$DAEMON_BIN" ]]; then
    echo "[ERROR] loom-daemon binary not found. Build it or set LOOM_DAEMON_BIN." >&2
    exit 1
fi
exec "$DAEMON_BIN" spawn-worker --scripts-dir "$SCRIPT_DIR" -- "$@"
