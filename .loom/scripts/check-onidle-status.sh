#!/usr/bin/env bash
# check-onidle-status.sh - Verify autonomous.roleRunner.onIdle is actually
# firing for a registered workspace, not just configured.
#
# `autonomous.roleRunner.onIdle` is a distinct opt-in from the interval
# `roles` list (see loom-daemon/src/role_runner.rs -> resolve_on_idle_roles):
# a repo can have onIdle roles *configured* in .loom/config.json while the
# daemon never actually fires them (work finder disabled, per-root
# roleRunner.enabled false, or simply never idle). This script closes that
# configured-vs-dormant gap by cross-checking the config against the daemon's
# own log for the "idle edge ... firing idle-triggered <role> run" line
# (#4364) that only appears when a real tick fired.
#
# Usage:
#   ./check-onidle-status.sh [--root PATH] [--daemon-log PATH] [--json]
#
# Options:
#   --root PATH        Workspace root to check (default: repo root containing
#                       this script, resolved the same way check-ci-status.sh
#                       does).
#   --daemon-log PATH  Daemon log file to scan (default: $LOOM_DAEMON_LOG, or
#                       $HOME/.loom/daemon.log — the same precedence
#                       loom-daemon's own resolve_log_path() uses).
#   --json              Output machine-readable JSON instead of prose.
#   --help              Show this help message.
#
# Exit codes:
#   0 - no onIdle roles configured (nothing to verify), or every configured
#       role has at least one confirmed fire in the log
#   1 - bad usage / missing dependency (jq)
#   2 - onIdle roles are configured but the daemon log is missing/unreadable
#       (cannot verify either way)
#   3 - onIdle roles are configured but at least one has never fired
#       according to the log — configured-but-dormant, the exact failure
#       mode this script exists to catch
#
# This is a read-only diagnostic — it never mutates config or logs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'
[[ ! -t 1 ]] && RED="" && GREEN="" && YELLOW="" && BLUE="" && NC=""

ROOT=""
DAEMON_LOG="${LOOM_DAEMON_LOG:-$HOME/.loom/daemon.log}"
JSON_OUTPUT=false

show_help() {
    sed -n '2,/^$/p' "$0" | sed 's/^# //' | sed 's/^#//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)
            ROOT="$2"
            shift 2
            ;;
        --daemon-log)
            DAEMON_LOG="$2"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --help|-h)
            show_help
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Run with --help for usage" >&2
            exit 1
            ;;
    esac
done

if ! command -v jq &>/dev/null; then
    echo "Error: jq is required but not installed" >&2
    exit 1
fi

if [[ -z "$ROOT" ]]; then
    ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi
ROOT="$(cd "$ROOT" && pwd)"

CONFIG_FILE="$ROOT/.loom/config.json"

# --- Resolve configured onIdle roles ---
ON_IDLE_ROLES=()
if [[ -f "$CONFIG_FILE" ]]; then
    while IFS= read -r role; do
        [[ -n "$role" ]] && ON_IDLE_ROLES+=("$role")
    done < <(jq -r '.autonomous.roleRunner.onIdle // [] | .[]' "$CONFIG_FILE" 2>/dev/null || true)
fi

if [[ ${#ON_IDLE_ROLES[@]} -eq 0 ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        jq -n --arg root "$ROOT" '{root: $root, configured: [], verified: [], dormant: [], status: "not_configured"}'
    else
        echo -e "${YELLOW}No autonomous.roleRunner.onIdle roles configured for $ROOT — nothing to verify.${NC}"
    fi
    exit 0
fi

if [[ ! -r "$DAEMON_LOG" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        jq -n --arg root "$ROOT" --argjson configured "$(printf '%s\n' "${ON_IDLE_ROLES[@]}" | jq -R . | jq -s .)" \
            '{root: $root, configured: $configured, verified: [], dormant: [], status: "log_unavailable"}'
    else
        echo -e "${RED}Cannot verify — daemon log not readable: $DAEMON_LOG${NC}" >&2
    fi
    exit 2
fi

# --- Cross-check each configured role against the daemon log ---
# Matches loom-daemon/src/role_runner.rs's plan_idle_runs log line:
#   "role_runner: idle edge for <root> — firing idle-triggered <role> run (#4364)"
VERIFIED=()
DORMANT=()
declare -A LAST_FIRED
declare -A FIRE_COUNT

for role in "${ON_IDLE_ROLES[@]}"; do
    pattern="idle edge for ${ROOT} — firing idle-triggered ${role} run"
    matches="$(grep -F "$pattern" "$DAEMON_LOG" 2>/dev/null || true)"
    count="$(printf '%s\n' "$matches" | grep -cF "$pattern" 2>/dev/null || echo 0)"
    if [[ -n "$matches" && "$count" -gt 0 ]]; then
        VERIFIED+=("$role")
        FIRE_COUNT["$role"]="$count"
        LAST_FIRED["$role"]="$(printf '%s\n' "$matches" | tail -1 | grep -oE '^\[[0-9T:.+-]+\]' | tr -d '[]')"
    else
        DORMANT+=("$role")
        FIRE_COUNT["$role"]=0
    fi
done

STATUS="verified"
[[ ${#DORMANT[@]} -gt 0 ]] && STATUS="dormant"

if [[ "$JSON_OUTPUT" == "true" ]]; then
    jq -n \
        --arg root "$ROOT" \
        --arg status "$STATUS" \
        --argjson configured "$(printf '%s\n' "${ON_IDLE_ROLES[@]}" | jq -R . | jq -s .)" \
        --argjson verified "$(printf '%s\n' "${VERIFIED[@]:-}" | jq -R 'select(length > 0)' | jq -s .)" \
        --argjson dormant "$(printf '%s\n' "${DORMANT[@]:-}" | jq -R 'select(length > 0)' | jq -s .)" \
        '{root: $root, configured: $configured, verified: $verified, dormant: $dormant, status: $status}'
else
    echo -e "${BLUE}onIdle status for $ROOT${NC}"
    echo "─────────────────────────────────"
    for role in "${ON_IDLE_ROLES[@]}"; do
        if [[ "${FIRE_COUNT[$role]}" -gt 0 ]]; then
            echo -e "  ${GREEN}$role${NC}: fired ${FIRE_COUNT[$role]}x (last: ${LAST_FIRED[$role]:-unknown})"
        else
            echo -e "  ${RED}$role${NC}: configured but never observed firing in $DAEMON_LOG"
        fi
    done
    echo ""
fi

[[ ${#DORMANT[@]} -gt 0 ]] && exit 3
exit 0
