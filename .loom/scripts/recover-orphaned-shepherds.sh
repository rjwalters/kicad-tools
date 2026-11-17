#!/bin/bash
# recover-orphaned-shepherds.sh - Detect and recover orphaned task state.
#
# Delegates to the native `loom-daemon recover-orphans` subcommand (issue
# #4272, epic #4081 Phase 3 family 2 — a byte-compatible Rust port of the
# historical Python `loom-recover-orphans` CLI). A capability probe still
# detects a daemon binary that predates the `recover-orphans` subcommand (a
# host mid-roll), but there is NO fallback to fall back to: PR #4301 (commit
# dba33666) deleted `loom_tools/orphan_recovery.py` and its
# `loom-recover-orphans` console-script entry in the very same commit that
# added the probe, so the old `run_loom_tool` branch could only ever produce
# `No module named loom_tools.orphan_recovery`. A stale binary now fails
# loudly with a rebuild remedy instead (#4384).
#
# The script name is preserved for back-compat with operator muscle memory
# and any CLAUDE.md / docs that link to it.
#
# Usage:
#   recover-orphaned-shepherds.sh              # Dry-run: show what would be recovered
#   recover-orphaned-shepherds.sh --recover    # Actually recover orphaned state
#   recover-orphaned-shepherds.sh --json       # Output JSON for programmatic use
#   recover-orphaned-shepherds.sh --verbose    # Show detailed progress
#   recover-orphaned-shepherds.sh --help       # Show help
#
# Exit codes (#6392 — every non-zero exit below is paired with a
# human-readable one-line reason on stderr; see each site for the exact
# text):
#   0 — assessed cleanly; either no orphans were found, or --recover ran.
#   1 — this WRAPPER could not even reach `loom-daemon recover-orphans`: no
#       binary resolved, or the resolved binary predates the subcommand
#       (stale build). Not emitted by the Rust subcommand itself.
#   2 — dry-run only (no --recover): the assessment succeeded AND found
#       orphaned claims. This is a REPORT, not a failure — rerun with
#       --recover to reclaim them. Emitted by `loom-daemon recover-orphans`.
#   3 — the assessment itself failed (e.g. a `gh issue list` dependency
#       call errored), so orphan state could not be determined at all.
#       Emitted by `loom-daemon recover-orphans`.
#
# Sourcing lib/locate-daemon-bin.sh's binary-resolution trace: this script
# execs straight into the resolved binary (see below), so its stderr is
# whatever that binary prints. A bare `loom_locate_daemon_bin: resolved ...`
# success trace, if it ended up the ONLY line on a failure path, previously
# read like the failure reason itself to a caller quoting "the first stderr
# line" (#6392) even though it always reports a successful resolution — so
# it is suppressed here unless --verbose/-v was requested (the exec'd
# subcommand's own dry-run/error output is the diagnostic line in every
# other case; see the exit-code table above).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Quiet the resolution-success trace unless the caller asked for verbose
# output -- see the header comment above (#6392).
LOOM_LOCATE_DAEMON_BIN_QUIET=1
for _arg in "$@"; do
    if [[ "$_arg" == "--verbose" || "$_arg" == "-v" ]]; then
        LOOM_LOCATE_DAEMON_BIN_QUIET=0
        break
    fi
done
export LOOM_LOCATE_DAEMON_BIN_QUIET
DAEMON_BIN="$(loom_locate_daemon_bin "$REPO_ROOT")"

if [[ -n "$DAEMON_BIN" ]] && "$DAEMON_BIN" recover-orphans --help >/dev/null 2>&1; then
    exec "$DAEMON_BIN" recover-orphans "$@"
fi

# No usable path remains. Fail loudly and actionably rather than degrading
# into a `No module named loom_tools.orphan_recovery` traceback (#4384).
if [[ -n "$DAEMON_BIN" ]]; then
    DAEMON_VERSION="$("$DAEMON_BIN" --version 2>/dev/null || true)"
    echo "ERROR recover-orphaned-shepherds.sh: $DAEMON_BIN does not support the 'recover-orphans' subcommand (stale build)." >&2
    echo "  Reported version: ${DAEMON_VERSION:-unknown}" >&2
else
    echo "ERROR recover-orphaned-shepherds.sh: no loom-daemon binary could be resolved." >&2
    echo "  Searched: \$LOOM_DAEMON_BIN, PATH, then $REPO_ROOT/{loom-daemon/,}target/{release,debug}/loom-daemon" >&2
fi
echo "  'loom-daemon recover-orphans' requires a binary built at or after commit dba33666 (PR #4301)." >&2
echo "  There is no Python fallback — loom_tools/orphan_recovery.py was deleted in that same commit." >&2
echo "  Remedy: rebuild or update loom-daemon, then retry:" >&2
echo "    cargo build --release -p loom-daemon        # source checkout" >&2
echo "    ./.loom/scripts/cli/loom-daemon-update.sh   # installed host (self-update)" >&2
exit 1
