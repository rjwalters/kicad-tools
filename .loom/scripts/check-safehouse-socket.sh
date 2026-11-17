#!/usr/bin/env bash
# check-safehouse-socket.sh — one-shot, per-host safehouse-socket resolution
# report (issue #5523).
#
# Why: #5457 removed the only hardcoded `safehouse.socket` default — a path
# that had been committed to the SHARED `.loom/config.json` and stranded
# loom-worker-2 75 commits behind `git pull` (a correct fix). But nothing took
# its place as a *check*: `loom_mcp_safehouse_socket()`
# (defaults/scripts/lib/mcp-config.sh) has always silently returned an empty
# string when nothing resolves, and every caller's only signal was a
# `log_warn` buried inside that particular sweep's own log file. From the
# moment #5457 merged, every sweep on an affected host silently stopped
# narrating to safehouse — and the failure ran for 11 hours before a human
# noticed the public fleet pulse had gone stale, because nothing
# outside a sweep's own log was watching for it.
#
# This script is that "something watching": a standalone, on-demand check
# that reports — per managed repo, without reading a single sweep log —
# whether `safehouse.enabled` is set and, if so, whether a socket resolves
# AND is actually present on disk. It reuses the exact resolvers
# spawn-claude.sh / loom-daemon-start.sh already call
# (`defaults/scripts/lib/mcp-config.sh`), so "drift" here means exactly what
# it means to a spawning worker — no separate definition to keep in sync.
#
# Deliberately does NOT add a code-level conventional-path default to the
# resolver (see mcp-config.sh's `loom_mcp_safehouse_socket` header and
# defaults/docs/safehouse.md "Socket resolution" for the full writeup on
# why): the fix for "silence went unnoticed for 11 hours" is making the
# silence loud and cheap to check, independent of the daemon's own
# start/restart lifecycle — not guessing a path. The pre-existing static
# check in `loom-daemon-start.sh` (`print_safehouse_status`) only re-runs
# when the daemon itself (re)starts, which is exactly the case that let this
# incident run unnoticed on an already-running daemon; this script has no
# such dependency.
#
# Usage:
#   check-safehouse-socket.sh [--json] [REPO_ROOT ...]
#
#   With no REPO_ROOT arguments, checks every workspace registered in this
#   host's machine-level daemon registry (~/.loom/workspaces.json, or
#   $LOOM_WORKSPACES_PATH — see loom-daemon/src/workspace_registry.rs),
#   falling back to the enclosing repo of the current directory when the
#   registry is empty/absent/unreadable, so a lone interactive host still
#   gets a useful answer.
#
# Exit codes:
#   0 — every checked repo is either "not configured" (safehouse.enabled is
#       false/absent — a byte-for-byte no-op, see the degradation contract in
#       defaults/docs/safehouse.md) or "resolved" (a socket path resolved AND
#       is present on disk).
#   1 — at least one repo is "configured, enabled, unreachable": either
#       safehouse.enabled is true with no socket resolving at all (the exact
#       #5523 failure mode), or a socket path resolved but nothing exists
#       there yet (safehoused not running / not yet started).
#   2 — usage error (e.g. the sibling lib/mcp-config.sh is missing, a
#       stale/partial install).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SCRIPT_DIR/lib/mcp-config.sh"

if [[ ! -r "$LIB" ]]; then
    echo "check-safehouse-socket: missing $LIB (stale/partial install) — cannot check." >&2
    exit 2
fi
# shellcheck source=./lib/mcp-config.sh
source "$LIB"

JSON=false
declare -a ROOTS=()
for _arg in "$@"; do
    case "$_arg" in
        --json) JSON=true ;;
        -h | --help)
            sed -n '2,45p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *) ROOTS+=("$_arg") ;;
    esac
done

# find_repo_root <start_dir> — walk up looking for a `.loom` directory
# (mirrors loom-daemon-start.sh's find_repo_root, minimal subset: no worktree
# .git-file resolution, since this is only used as a last-resort fallback).
find_repo_root() {
    local dir="$1"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

if [[ ${#ROOTS[@]} -eq 0 ]]; then
    _registry="${LOOM_WORKSPACES_PATH:-$HOME/.loom/workspaces.json}"
    if [[ -f "$_registry" ]] && command -v jq >/dev/null 2>&1; then
        while IFS= read -r _root; do
            [[ -n "$_root" ]] && ROOTS+=("$_root")
        done < <(jq -r '.workspaces[]?.root // empty' "$_registry" 2>/dev/null || true)
    fi
    if [[ ${#ROOTS[@]} -eq 0 ]]; then
        # No registry (or unreadable/empty) — fall back to the repo enclosing
        # the current directory, so a lone interactive host still gets an
        # answer instead of silently checking nothing.
        _fallback_root="$(find_repo_root "$PWD" || true)"
        if [[ -n "$_fallback_root" ]]; then
            ROOTS+=("$_fallback_root")
        fi
    fi
fi

if [[ ${#ROOTS[@]} -eq 0 ]]; then
    echo "check-safehouse-socket: no managed workspaces found (no $HOME/.loom/workspaces.json entries, and no enclosing .loom/ from \$PWD) — nothing to check." >&2
    exit 0
fi

_exit=0
_tsv_file=""
if $JSON; then
    _tsv_file="$(mktemp)"
    trap 'rm -f "$_tsv_file"' EXIT
fi

for _root in "${ROOTS[@]}"; do
    _enabled="$(loom_mcp_safehouse_enabled "$_root" 2>/dev/null || echo false)"
    _socket=""
    _present="false"
    if [[ "$_enabled" != "true" ]]; then
        _status="not_configured"
    else
        _socket="$(loom_mcp_safehouse_socket "$_root" 2>/dev/null || true)"
        if [[ -z "$_socket" ]]; then
            _status="unreachable_no_socket"
            _exit=1
        elif [[ -S "$_socket" || -e "$_socket" ]]; then
            _status="resolved"
            _present="true"
        else
            _status="unreachable_missing_file"
            _exit=1
        fi
    fi

    if $JSON; then
        printf '%s\t%s\t%s\t%s\t%s\n' "$_root" "$_enabled" "$_socket" "$_present" "$_status" >>"$_tsv_file"
    else
        case "$_status" in
            not_configured)
                echo "check-safehouse-socket: $_root -- not configured (safehouse.enabled is false/absent)"
                ;;
            resolved)
                echo "check-safehouse-socket: $_root -- OK (socket resolved: $_socket)"
                ;;
            unreachable_no_socket)
                echo "check-safehouse-socket: $_root -- DRIFT: safehouse.enabled is true but no socket resolves (safehouse.socket / \$LOOM_SAFEHOUSE_SOCKET / \$SAFEHOUSED_SOCKET) -- no safehouse narration will be recorded from this repo; the public fleet pulse is fed exclusively from safehouse narration (issue #5523)." >&2
                ;;
            unreachable_missing_file)
                echo "check-safehouse-socket: $_root -- DRIFT: safehouse.enabled is true, socket resolved to '$_socket', but nothing exists there yet -- is safehoused running? (issue #5523)" >&2
                ;;
        esac
    fi
done

if $JSON; then
    if command -v python3 >/dev/null 2>&1; then
        python3 -c '
import json, sys
rows = []
for line in open(sys.argv[1], encoding="utf-8"):
    repo, enabled, socket, present, status = line.rstrip("\n").split("\t")
    rows.append({
        "repo": repo,
        "enabled": enabled == "true",
        "socket": socket,
        "present": present == "true",
        "status": status,
    })
json.dump(rows, sys.stdout)
print()
' "$_tsv_file"
    else
        echo "check-safehouse-socket: --json requires python3 (not found) -- falling back to TSV" >&2
        cat "$_tsv_file"
    fi
fi

exit "$_exit"
