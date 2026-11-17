#!/bin/bash
# cleanup.sh - Log archival for Loom
#
# Delegates to the native `loom-daemon cleanup logs` subcommand (issue
# #4272, epic #4081 Phase 3 family 2 — a byte-compatible Rust port of the
# historical Python `loom-cleanup` CLI). A capability probe still detects a
# daemon binary that predates the `cleanup` subcommand (a host mid-roll), but
# there is NO fallback to fall back to: PR #4301 (commit dba33666) deleted
# `loom_tools/cleanup.py` and its `loom-cleanup` console-script entry in the
# very same commit that added the probe, so the old `run_loom_tool` branch
# could only ever produce `No module named loom_tools.cleanup`. A stale binary
# now fails loudly with a rebuild remedy instead (#4384).
#
# History: this script was previously named daemon-cleanup.sh and dispatched
# event-driven cleanup for the Loom daemon (shepherd-complete, daemon-startup,
# daemon-shutdown, periodic, prune-sessions).  Those events were removed in
# #3396 (Phase 3.1.7 of #3372) -- session rotation goes away with the daemon
# brain in Phase 3.2.  Only log archival survives.
#
# Usage:
#   cleanup.sh logs                          # archive task outputs + prune
#   cleanup.sh logs --dry-run                # preview
#   cleanup.sh logs --prune-only             # skip archival, only prune
#   cleanup.sh logs --retention-days N       # override retention window
#   cleanup.sh --help                        # show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DAEMON_BIN="$(loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -n "$DAEMON_BIN" ]] && "$DAEMON_BIN" cleanup logs --help >/dev/null 2>&1; then
    exec "$DAEMON_BIN" cleanup "$@"
fi

# No usable path remains. Fail loudly and actionably rather than degrading
# into a `No module named loom_tools.cleanup` traceback (#4384).
if [[ -n "$DAEMON_BIN" ]]; then
    DAEMON_VERSION="$("$DAEMON_BIN" --version 2>/dev/null || true)"
    echo "ERROR cleanup.sh: $DAEMON_BIN does not support the 'cleanup logs' subcommand (stale build)." >&2
    echo "  Reported version: ${DAEMON_VERSION:-unknown}" >&2
else
    echo "ERROR cleanup.sh: no loom-daemon binary could be resolved." >&2
    echo "  Searched: \$LOOM_DAEMON_BIN, PATH, then $REPO_ROOT/{loom-daemon/,}target/{release,debug}/loom-daemon" >&2
fi
echo "  'loom-daemon cleanup' requires a binary built at or after commit dba33666 (PR #4301)." >&2
echo "  There is no Python fallback — loom_tools/cleanup.py was deleted in that same commit." >&2
echo "  Remedy: rebuild or update loom-daemon, then retry:" >&2
echo "    cargo build --release -p loom-daemon        # source checkout" >&2
echo "    ./.loom/scripts/cli/loom-daemon-update.sh   # installed host (self-update)" >&2
exit 1
