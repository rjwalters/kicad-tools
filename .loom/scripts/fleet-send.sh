#!/usr/bin/env bash
# fleet-send.sh — post ONE safehouse envelope over the AF_UNIX socket, for
# lifecycle role subagents (Builder / Judge / Doctor) whose Claude-Code tool
# allowlists exclude MCP tools and therefore can't reach the session-injected
# `safehouse_send` MCP tool (issue #4199, phase 2 of #4196 / #3997).
#
# THIN STUB (issue #9517, epic #7810). The implementation is
# `loom-daemon fleet-send` (Rust, `loom-daemon/src/cli/fleet_send.rs`), which
# reuses `safehouse.rs`'s own `build_send_request` — the file this script's
# bash body used to hand-copy. Flags and the wire behavior are unchanged;
# `defaults/scripts/tests/test-fleet-send.sh` is the equivalence evidence.
#
# HARD degradation contract — this helper MUST NEVER block, stall, or fail a
# role. Absent env vars, a missing loom-daemon, an unresolvable socket, an
# invalid argument, or any connect/hello/send failure ⇒ **exit 0 SILENTLY**
# (no stdout, no stderr). Treat "posted" as best-effort; the room is optional,
# the role's work is not.
#
# The contract is also why this stub does NOT delegate to
# lib/script-helper.sh like the other stubs: that resolver's missing-daemon
# path is an actionable loud error (exit 1) — right for every stub whose
# caller reads its output, precisely wrong for the one entry point whose
# entire interface is silence. Here a missing binary is just the room being
# unavailable, the same degradation class as a missing socket, so the
# resolution goes through lib/locate-daemon-bin.sh's implementation resolver
# directly and a miss is a silent no-op. Declared accordingly:
#
# requires-daemon: fleet-send optional   a daemon without the subcommand (or none at all) degrades to this script's hard silent-exit-0 contract
#
# Usage:
#   fleet-send.sh --task-id <id> --type chat|task|handoff|ack \
#                 --body <text> [--to <persona|*>] [--room <name>]
#
# Resolution:
#   socket  = $SAFEHOUSED_SOCKET  (fallback $LOOM_SAFEHOUSE_SOCKET)
#   persona = $SAFEHOUSE_PERSONA
#
# Both are exported into the worker session by spawn-claude.sh alongside the
# safehouse MCP injection; a plain shell without them is the common case and
# exits 0. `task_id` should be the repo-qualified form the daemon narrates on
# (`<repo>_<issue>`, e.g. `loom_4199`, post-#4224) so role posts thread with
# the daemon's dispatch narration; the subcommand enforces the same
# `[A-Za-z0-9_]` charset the script always did.

# Deliberately NOT `set -e` — every failure path must fall through to exit 0.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"

# The IMPLEMENTATION resolver, not loom_locate_daemon_bin: $LOOM_DAEMON_BIN
# means "the daemon this caller manages" and can be a deliberately-fake mock
# in a suite (#8134/#7977); a stub must exec the binary that implements it —
# this checkout's build in the source repo, the installed daemon elsewhere.
# The whole block is stderr-silenced (which also covers the resolver's #4997
# trace and the child's own output) because the contract allows no output on
# ANY path, resolution included.
{
    bin="$(loom_resolve_self_daemon_bin)"
    if [[ -n "$bin" ]]; then
        # No `exec`: the child's exit code is data-free here — an old daemon
        # answers an unknown subcommand with clap's exit 2, and even that must
        # surface as a silent success. Output is discarded for the same
        # reason; the subcommand itself prints nothing by contract.
        "$bin" fleet-send "$@" >/dev/null 2>&1 || true
    fi
} 2>/dev/null

exit 0
