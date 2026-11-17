#!/usr/bin/env bash
# roll-pause.sh — safe-point pause hook for a daemon roll (issue #10830; design
# docs/design/daemon-roll-pause-resume.md §2).
#
# Wired as a match-all PreToolUse hook plus PostToolUse and PostToolUseFailure
# hooks for Claude Code (through hook-wiring.sh), and run first by
# guard-codex-bridge.sh for Codex. A thin entry point: the park / ledger /
# safe-point logic is `loom-daemon roll-pause hook` (loom-daemon/src/roll_pause).
#
# Inert unless LOOM_DAEMON_ITEM_ID is set, which only the daemon's dispatch
# does: an in-session or attended agent returns here at once, without starting
# the daemon binary. With it set, the binary records the in-flight ledger, and
# once the daemon has requested a pause for this item it parks every new tool
# call (the call does not run), writes the safe-point record, and denies the
# call with "paused for a daemon roll" if its park window runs out.
#
# `hook` (the default) always exits 0: a binary that is missing, or too old to
# know the subcommand, prints nothing and the call is allowed. `active` (the
# Stop guard's question) passes the binary's exit status through: 0 = a pause
# request is active.
#
# LOOM_ROLL_PAUSE_BIN overrides the binary (a session container whose image
# daemon predates the subcommand, or a test).
#
# requires-daemon: roll-pause optional   #10830 — an older daemon exits 2 on the unknown subcommand: `hook` then prints nothing and exits 0 (allow, the agent is simply never parked) and `active` reads as not active.

if [[ -z "${LOOM_DAEMON_ITEM_ID:-}" ]]; then
    [[ "${1:-hook}" == "hook" ]] && exit 0
    exit 1
fi
_bin="${LOOM_ROLL_PAUSE_BIN:-}"
_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts/lib" 2>/dev/null && pwd)/locate-daemon-bin.sh"
if [[ -z "$_bin" && -r "$_lib" ]] && source "$_lib" 2>/dev/null; then
    _bin="$(loom_resolve_self_daemon_bin 2>/dev/null)" || _bin=""
fi
_bin="${_bin:-loom-daemon}"
if [[ "${1:-hook}" == "hook" ]]; then
    "$_bin" roll-pause hook --harness-pid "$PPID" 2>/dev/null
    exit 0
fi
"$_bin" roll-pause "$@" 2>/dev/null
