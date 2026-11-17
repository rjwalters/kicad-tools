#!/usr/bin/env bash
# fleet-check.sh — read/check ONE safehouse mailbox over the AF_UNIX socket,
# for lifecycle role subagents (Builder / Judge / Doctor) whose Claude-Code
# tool allowlists exclude MCP tools and therefore can't reach the
# session-injected `safehouse_read` MCP tool. Read/check counterpart to
# `fleet-send.sh` (issue #4248, follow-on from #4199 / phase 2 of #4196 /
# #3997).
#
# It is a thin JSON-lines client speaking the same wire protocol as
# `fleet-send.sh`: connect, send the mandatory `hello` handshake, then one
# `check` request. The `check` contract is confirmed against
# `rjwalters/safehouse` (commit 803f35b, `safehoused/src/rpc.rs` "check"
# branch + `safehoused/src/mailbox.rs::check`):
#
#   {"id": 1, "op": "check"}                 # default: advances the read cursor
#   {"id": 1, "op": "check", "peek": true}   # peek: does NOT advance the cursor
#   {"id": 1, "op": "check", "limit": 2}     # cap returned count (server clamps)
#
#   {"ok": true, "advanced": true, "messages": [ {...envelope...}, ... ]}
#
# `check` returns unread envelopes for the authenticated persona, oldest
# first. Default (advance=true) moves the persona's read cursor past
# everything returned — an immediate second `check` returns `[]`. `peek: true`
# leaves the cursor untouched. With `limit`, the cursor only advances past
# what was actually returned.
#
# CAVEAT — shared cursor: spawned lifecycle roles share the persona's mailbox
# cursor with any other process using the same persona. An advancing `check`
# from one consumer consumes mail another consumer expected to see. Use
# `--peek` when you don't want to consume mail meant for someone else sharing
# your persona.
#
# HARD degradation contract — this helper MUST NEVER block, stall, or fail a
# role. Absent env vars, an unresolvable socket, or any connect/hello/check
# failure ⇒ **exit 0 SILENTLY** (no stdout, no stderr). An empty mailbox is
# ALSO silent (zero output, exit 0) — a caller can treat empty output as "no
# mail / no room" uniformly.
#
# UNLIKE fleet-send.sh (which never prints to stdout on any path): on
# success, this helper prints each message in the `messages` array as one
# JSON object per line to stdout.
#
# Usage:
#   fleet-check.sh [--peek] [--limit <n>]
#
# Resolution:
#   socket  = $SAFEHOUSED_SOCKET  (fallback $LOOM_SAFEHOUSE_SOCKET)
#   persona = $SAFEHOUSE_PERSONA
#
# Both are exported into the worker session by spawn-claude.sh alongside the
# safehouse MCP injection; a plain shell without them is the common case and
# exits 0.

# Deliberately NOT `set -e` — every failure path must fall through to exit 0.
set -u

# Any unexpected error, signal, or EXIT ⇒ silent success. Belt-and-suspenders
# on top of the explicit `exit 0`s below.
trap 'exit 0' ERR
trap 'exit 0' EXIT

# --- Parse args -------------------------------------------------------------
_peek="false"
_limit=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --peek) _peek="true"; shift ;;
        --limit) _limit="${2:-}"; shift 2 || exit 0 ;;
        --limit=*) _limit="${1#*=}"; shift ;;
        *) shift ;; # ignore unknown args — never fail a role over a typo
    esac
done

# --- Resolve socket + persona (silent no-op when absent) --------------------
_socket="${SAFEHOUSED_SOCKET:-${LOOM_SAFEHOUSE_SOCKET:-}}"
_persona="${SAFEHOUSE_PERSONA:-}"

[[ -z "$_socket" || -z "$_persona" ]] && exit 0

# `limit`, when present, must be a non-negative integer (else ignore it —
# never fail a role over a typo).
if [[ -n "$_limit" && ! "$_limit" =~ ^[0-9]+$ ]]; then
    _limit=""
fi

# --- Check over the socket (best-effort; python does the AF_UNIX I/O) -------
SAFEHOUSE_FC_SOCKET="$_socket" \
    SAFEHOUSE_FC_PERSONA="$_persona" \
    SAFEHOUSE_FC_PEEK="$_peek" \
    SAFEHOUSE_FC_LIMIT="$_limit" \
    "${LOOM_PYTHON:-python3}" - <<'PY' 2>/dev/null || exit 0
import json, os, socket, sys

sock_path = os.environ.get("SAFEHOUSE_FC_SOCKET", "")
persona = os.environ.get("SAFEHOUSE_FC_PERSONA", "")
peek = os.environ.get("SAFEHOUSE_FC_PEEK", "false") == "true"
limit_raw = os.environ.get("SAFEHOUSE_FC_LIMIT", "")


def read_reply(reader):
    # Skip interleaved async push lines (they carry an `event` key and no `id`)
    # and return the first genuine reply object, or None on EOF/timeout.
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and "event" in msg and "id" not in msg:
            continue
        return msg
    return None


try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5.0)
    s.connect(sock_path)
except Exception:
    sys.exit(0)

try:
    reader = s.makefile("r", encoding="utf-8")

    def write_line(obj):
        s.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    # Mandatory hello handshake.
    write_line({"id": 0, "op": "hello", "persona": persona})
    reply = read_reply(reader)
    if not (isinstance(reply, dict) and reply.get("ok") is True):
        sys.exit(0)

    # check request (id=1).
    req = {"id": 1, "op": "check"}
    if peek:
        req["peek"] = True
    if limit_raw:
        req["limit"] = int(limit_raw)
    write_line(req)

    reply = read_reply(reader)
    if not (isinstance(reply, dict) and reply.get("ok") is True):
        sys.exit(0)

    messages = reply.get("messages")
    if not isinstance(messages, list) or not messages:
        sys.exit(0)

    for msg in messages:
        sys.stdout.write(json.dumps(msg) + "\n")
except Exception:
    sys.exit(0)
finally:
    try:
        s.close()
    except Exception:
        pass

sys.exit(0)
PY

exit 0
