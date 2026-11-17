#!/bin/bash
# clean.sh - Backwards-compatible wrapper for loom-clean
#
# Delegates to the native `loom-daemon clean` subcommand (issue #4272, epic
# #4081 Phase 3 family 2 — a byte-compatible Rust port of the historical
# Python `loom-clean` CLI). A capability probe still detects a daemon binary
# that predates the `clean` subcommand (a host mid-roll), but there is NO
# fallback to fall back to: PR #4301 (commit dba33666) deleted
# `loom_tools/clean.py` and its `loom-clean` console-script entry in the very
# same commit that added the probe, so the old `run_loom_tool` branch could
# only ever produce `No module named loom_tools.clean`. A stale binary now
# fails loudly with a rebuild remedy instead (#4384).
#
# Usage:
#   clean.sh             # Interactive cleanup
#   clean.sh --force     # Non-interactive cleanup
#   clean.sh --dry-run   # Preview what would be cleaned
#   clean.sh --deep      # Also remove build artifacts
#   clean.sh --aggressive --dry-run   # Preview vestigial-worktree cleanup
#   clean.sh --help      # Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DAEMON_BIN="$(loom_locate_daemon_bin "$REPO_ROOT")"

# Capability-probe before committing to the native path: a host mid-roll can
# have a `loom-daemon` binary predating the `clean` subcommand, which would
# fail with "unrecognized subcommand `clean`".
if [[ -n "$DAEMON_BIN" ]] && "$DAEMON_BIN" clean --help >/dev/null 2>&1; then
    exec "$DAEMON_BIN" clean "$@"
fi

# No usable path remains. Fail loudly and actionably rather than degrading
# into a `No module named loom_tools.clean` traceback (#4384).
if [[ -n "$DAEMON_BIN" ]]; then
    DAEMON_VERSION="$("$DAEMON_BIN" --version 2>/dev/null || true)"
    echo "ERROR clean.sh: $DAEMON_BIN does not support the 'clean' subcommand (stale build)." >&2
    echo "  Reported version: ${DAEMON_VERSION:-unknown}" >&2
else
    echo "ERROR clean.sh: no loom-daemon binary could be resolved." >&2
    echo "  Searched: \$LOOM_DAEMON_BIN, PATH, then $REPO_ROOT/{loom-daemon/,}target/{release,debug}/loom-daemon" >&2
fi
echo "  'loom-daemon clean' requires a binary built at or after commit dba33666 (PR #4301)." >&2
echo "  There is no Python fallback — loom_tools/clean.py was deleted in that same commit." >&2
echo "  Remedy: rebuild or update loom-daemon, then retry:" >&2
echo "    cargo build --release -p loom-daemon        # source checkout" >&2
echo "    ./.loom/scripts/cli/loom-daemon-update.sh   # installed host (self-update)" >&2
exit 1
