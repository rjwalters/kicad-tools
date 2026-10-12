#!/usr/bin/env bash
# presence-beat.sh — operator presence heartbeat hook (issue #11563).
#
# Wired (opt-in, LOOM_OPERATOR_PRESENCE=1 at provision time) for
# UserPromptSubmit, PostToolUse and Stop through the user-scope hook wrapper.
# A thin entry point: the throttle, state and journal logic is
# `loom-daemon presence beat`, which reads the hook JSON on stdin for
# `session_id` and `hook_event_name` only and never stores anything else.
#
# Always exits 0 and prints nothing: a missing or older binary must never
# block a prompt. Inert for a daemon-dispatched agent (LOOM_DAEMON_ITEM_ID).
#
# requires-daemon: presence optional   #11563 — an older daemon exits 2 on the unknown subcommand; the hook swallows it and no fact is recorded.
[[ -n "${LOOM_DAEMON_ITEM_ID:-}" ]] && exit 0
_bin="${LOOM_PRESENCE_BIN:-}"
_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/../scripts/lib" 2>/dev/null && pwd)/locate-daemon-bin.sh"
if [[ -z "$_bin" && -r "$_lib" ]] && source "$_lib" 2>/dev/null; then
    _bin="$(loom_resolve_self_daemon_bin 2>/dev/null)" || _bin=""
fi
_bin="${_bin:-loom-daemon}"
# Output is discarded so the hook never prints into the session. Failures are
# not lost: the binary appends each one to `.loom/logs/operator-presence.errors`
# and `loom-daemon presence status` reports the count and the last one.
"$_bin" presence beat >/dev/null 2>&1
exit 0
