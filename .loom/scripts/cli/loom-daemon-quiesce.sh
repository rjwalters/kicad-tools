#!/usr/bin/env bash
# loom-daemon-quiesce.sh - Explicit, single-command FLEET quiesce (issue #6129).
#
# `loom-daemon-stop.sh` stops the DISPATCHER only, by design: in-flight
# `/loom:sweep` children and scheduled role-agent ticks (Champion, Curator,
# Judge, Doctor, Guide, …) are independent detached processes that survive an
# ordinary stop/restart on purpose (see loom-daemon-stop.sh's header and
# daemon_service.rs's "survive, don't drain" policy comment) -- killing the
# dispatcher must not silently kill dispatched work.
#
# THIS script is the deliberate opposite action: an operator explicitly
# choosing to drain a host -- for maintenance, for cost, or (the incident this
# closes, 2026-08-13 on loom-worker-2) to stop it drawing on an exhausted
# token pool -- needs ONE command that (1) stops dispatch AND (2) enumerates
# and stops every in-flight role/sweep child, the SAME WAY on launchd and
# systemd. It never runs automatically; nothing else in this repo invokes it.
#
# What it does, in order:
#   1. Stops (and, on systemd, disables) the daemon itself via
#      loom-daemon-stop.sh, so nothing NEW gets dispatched while step 2 runs.
#   2. Enumerates surviving agent processes and stops them:
#        - Linux systemd --user: every active `loom-agent-*.scope` unit
#          (the predictable per-spawn naming spawn-claude.sh assigns under
#          `loom-agents.slice` as of #6129) via `systemctl --user stop`.
#        - EVERY platform (belt-and-braces, and the ONLY mechanism on
#          launchd, where there is no scope/unit construct): any process
#          whose command line names both a `claude`/`claude-wrapper.sh`
#          binary AND a `-p /loom:` prompt flag -- the same shape the
#          incident's manual `pgrep -af "claude-wrapper.sh -p /loom:"`
#          workaround matched, generalized to also catch a bare `claude`
#          invocation (LOOM_USE_WRAPPER=0) and any systemd-run-wrapped scope
#          this host's `systemd-run` version renamed on its own. SIGTERM
#          first, escalate surviving pids to SIGKILL after a grace window.
#          This step's own pid and its ancestry are always excluded.
#
# This never touches anything OTHER than a bare `systemctl --user stop
# loom-daemon` would leave running -- it does not reach into a worktree, a
# git branch, or a forge label. Cancelling a specific sweep's forge-visible
# claim is still `mcp__loom__cancel_sweep` (against a RUNNING daemon, before
# quiescing) or the label-recovery playbook in troubleshooting.md; this
# script's job ends at "no more Loom-spawned processes are running / drawing
# on the token pool".
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-quiesce.sh              Full quiesce (SIGTERM -> SIGKILL, grace window)
#   ./.loom/scripts/cli/loom-daemon-quiesce.sh --force       Skip the grace window (SIGKILL immediately)
#   ./.loom/scripts/cli/loom-daemon-quiesce.sh --dry-run      Resolve + print every target, mutate nothing
#   ./.loom/scripts/cli/loom-daemon-quiesce.sh --help
#
# Environment:
#   LOOM_DAEMON_QUIESCE_GRACE_SECS  Grace window before SIGKILL for surviving
#                                   agent processes (default 10, same default
#                                   as loom-daemon-stop.sh's own grace window).
#   LOOM_DAEMON_QUIESCE_DRYRUN      1/true/yes: same effect as --dry-run (also
#                                   forwarded to loom-daemon-stop.sh as
#                                   LOOM_DAEMON_STOP_DRYRUN, so the daemon
#                                   stop step is simulated too).
#   Every loom-daemon-stop.sh environment variable (LOOM_DAEMON_STOP_GRACE_SECS,
#   LOOM_LAUNCHD_LABEL, LOOM_SYSTEMD_UNIT, LOOM_MACHINE_CHECKOUT, ...) is
#   inherited verbatim by the daemon-stop step this script shells out to.
#
# Exit codes:
#   0  quiesce completed (daemon stopped or already down; zero or more agent
#      processes/scopes were stopped)
#   1  the daemon-stop step failed, or an agent process survived SIGKILL

set -uo pipefail

# ---------- help banner: read "$0" exactly ONCE, before anything else (#7794) ----------
# `--help` prints this file's leading comment block. Recovering that text
# lazily -- an awk pass over "$0" from inside show_help(), after sourcing and
# argument parsing have run -- races a same-path truncate+rewrite of this very
# file (a resync step, a shared self-hosted-runner checkout, or an operator's
# `git merge --ff-only` all write in place: open+truncate+write, not an atomic
# rename-into-place), handing back a torn, incomplete banner with no I/O error
# to catch. That has failed CI twice on unrelated PRs (#7201, PR #7768).
# Capturing it here -- the first statement executed, before any sourcing,
# argument parsing or subprocess -- narrows the window to this script's own
# startup instant and lets show_help() print from memory. It narrows the
# window, it does not close it: the permanent fix is #7810 Phase 6, where this
# wrapper becomes an `exec` stub and clap owns `--help`. Deliberately interim,
# and deliberately byte-identical -- awk stops at the first non-comment line
# (the blank line above `set -uo pipefail`), so the captured text never ends in
# a blank line and the single trailing newline `$( )` strips is exactly the one
# show_help()'s `printf '%s\n'` puts back.
_LOOM_HELP_BANNER="$(awk 'NR>=2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0")"

if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi
err()  { echo -e "${RED}$*${NC}" >&2; }
warn() { echo -e "${YELLOW}$*${NC}" >&2; }
ok()   { echo -e "${GREEN}$*${NC}"; }
info() { echo -e "${BLUE}$*${NC}"; }

# Prints the banner captured at startup above -- no filesystem access (#7794).
show_help() { printf '%s\n' "$_LOOM_HELP_BANNER"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOP_SCRIPT="$SCRIPT_DIR/loom-daemon-stop.sh"

FORCE=false
DRYRUN=false
if [[ "${LOOM_DAEMON_QUIESCE_DRYRUN:-}" =~ ^(1|true|yes|on)$ ]]; then
    DRYRUN=true
fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) show_help; exit 0 ;;
        --force|-f) FORCE=true; shift ;;
        --dry-run) DRYRUN=true; shift ;;
        *) err "Unknown option '$1'"; echo "Use --help for usage" >&2; exit 1 ;;
    esac
done

GRACE_SECS="${LOOM_DAEMON_QUIESCE_GRACE_SECS:-10}"

# ---------- step 1: stop dispatch ----------
info "== loom-daemon-quiesce: step 1/2 -- stopping the daemon (no new dispatch) =="
if [[ ! -x "$STOP_SCRIPT" ]]; then
    err "Cannot find loom-daemon-stop.sh at $STOP_SCRIPT"
    exit 1
fi
# Every array read in this script uses the `${arr[@]+"${arr[@]}"}` idiom (and
# counts are tracked in a scalar rather than derived from `${#arr[@]}`).
# Under `set -u`, bash < 4.4 -- notably the bash 3.2 stock macOS still ships,
# and macOS/launchd is a first-class platform for THIS script by construction
# (its whole point is "the same command on launchd and systemd") -- treats an
# empty array as unset, so a naive `"${STOP_ARGS[@]}"` / `${#_matches[@]}`
# would abort the quiesce with "unbound variable" on exactly the most common
# case: no --force and nothing left running. Same rationale as
# loom-daemon-update.sh's identical guard.
STOP_ARGS=()
[[ "$FORCE" == "true" ]] && STOP_ARGS+=(--force)
if [[ "$DRYRUN" == "true" ]]; then
    LOOM_DAEMON_STOP_DRYRUN=1 "$STOP_SCRIPT" ${STOP_ARGS[@]+"${STOP_ARGS[@]}"}
else
    "$STOP_SCRIPT" ${STOP_ARGS[@]+"${STOP_ARGS[@]}"}
fi
stop_rc=$?
if [[ "$stop_rc" -ne 0 ]]; then
    err "loom-daemon-stop.sh exited $stop_rc -- aborting quiesce before touching any agent process."
    err "Resolve the daemon-stop failure first (see the output above), then re-run this script."
    exit 1
fi

# ---------- step 2a: systemd --user scopes (Linux, #6129 naming) ----------
_LOOM_LIB_DIR="$SCRIPT_DIR/../lib"
if [[ -r "$_LOOM_LIB_DIR/systemd-user.sh" ]]; then
    # shellcheck source=../lib/systemd-user.sh
    source "$_LOOM_LIB_DIR/systemd-user.sh"
fi

SCOPES_STOPPED=0
if declare -f is_linux_systemd >/dev/null 2>&1 && is_linux_systemd; then
    info "== loom-daemon-quiesce: step 2/2 -- enumerating loom-agent-*.scope units =="
    while IFS= read -r _unit; do
        [[ -z "$_unit" ]] && continue
        if [[ "$DRYRUN" == "true" ]]; then
            echo "DRY-RUN: would systemctl --user stop $_unit"
        else
            echo "Stopping scope: $_unit"
            systemctl --user stop "$_unit" >/dev/null 2>&1 || true
        fi
        SCOPES_STOPPED=$((SCOPES_STOPPED + 1))
    done < <(systemctl --user list-units --state=active --no-legend --plain 'loom-agent-*.scope' 2>/dev/null | awk '{print $1}')
    if [[ "$SCOPES_STOPPED" -eq 0 ]]; then
        echo "No active loom-agent-*.scope units found."
    fi
else
    info "== loom-daemon-quiesce: step 2/2 -- no reachable systemd --user manager; using the cross-platform process scan =="
fi

# ---------- step 2b: cross-platform process-pattern fallback ----------
# The ONLY mechanism on launchd (no scope/unit construct exists there), and a
# belt-and-braces catch on systemd for anything the scope enumeration above
# missed (LOOM_SWEEP_CPU_QUOTA=0 hosts never wrap in a scope at all; an older
# `run-r<hex>.scope` predating #6129's naming; a host where the systemd-run
# probe failed and the spawn fell through unwrapped). Matches a command line
# that names a claude binary AND a `-p /loom:` prompt flag -- the same shape
# the incident's manual `pgrep -af "claude-wrapper.sh -p /loom:"` workaround
# used, generalized to also catch a bare (non-wrapper) `claude` invocation.
# `ps -eo pid,ppid,args` is supported by both GNU/Linux ps and BSD/macOS ps.
_self_pid=$$
_matches=()
_match_count=0
while IFS= read -r _line; do
    [[ -z "$_line" ]] && continue
    _pid="${_line%% *}"
    _rest="${_line#* }"
    _ppid="${_rest%% *}"
    _cmd="${_rest#* }"
    [[ "$_pid" =~ ^[0-9]+$ ]] || continue
    # Never a candidate: ourselves, our own parent (the shell/agent that
    # launched this script), or any pid that is not a plausible agent.
    [[ "$_pid" == "$_self_pid" ]] && continue
    [[ "$_ppid" == "$_self_pid" ]] && continue
    case "$_cmd" in
        *claude*"-p /loom:"*|*claude*'-p "/loom:'*)
            _matches+=("$_pid|$_cmd")
            _match_count=$((_match_count + 1))
            ;;
    esac
done < <(ps -eo pid=,ppid=,args= 2>/dev/null)

if [[ "$_match_count" -eq 0 ]]; then
    echo "No matching claude/claude-wrapper.sh -p /loom:* processes found."
else
    echo "Found ${_match_count} agent process(es) matching 'claude* -p /loom:*':"
    for _m in "${_matches[@]}"; do
        _pid="${_m%%|*}"
        _cmd="${_m#*|}"
        echo "  pid=$_pid  $_cmd"
    done
    if [[ "$DRYRUN" == "true" ]]; then
        for _m in "${_matches[@]}"; do
            _pid="${_m%%|*}"
            echo "DRY-RUN: would SIGTERM pid $_pid (grace ${GRACE_SECS}s), escalating to SIGKILL on timeout"
        done
    else
        for _m in "${_matches[@]}"; do
            _pid="${_m%%|*}"
            if [[ "$FORCE" == "true" ]]; then
                kill -KILL "$_pid" 2>/dev/null || true
            else
                kill -TERM "$_pid" 2>/dev/null || true
            fi
        done
        if [[ "$FORCE" != "true" ]]; then
            waited=0
            _survivors=("${_matches[@]}")
            _survivor_count="$_match_count"
            while [[ "$waited" -lt "$GRACE_SECS" ]]; do
                _still=()
                _still_count=0
                for _m in ${_survivors[@]+"${_survivors[@]}"}; do
                    _pid="${_m%%|*}"
                    if kill -0 "$_pid" 2>/dev/null; then
                        _still+=("$_m")
                        _still_count=$((_still_count + 1))
                    fi
                done
                _survivors=(${_still[@]+"${_still[@]}"})
                _survivor_count="$_still_count"
                [[ "$_survivor_count" -eq 0 ]] && break
                sleep 1
                waited=$((waited + 1))
            done
            for _m in ${_survivors[@]+"${_survivors[@]}"}; do
                _pid="${_m%%|*}"
                warn "pid $_pid did not exit within ${GRACE_SECS}s -- escalating to SIGKILL."
                kill -KILL "$_pid" 2>/dev/null || true
            done
        fi
        sleep 1
        _still_alive=0
        for _m in "${_matches[@]}"; do
            _pid="${_m%%|*}"
            kill -0 "$_pid" 2>/dev/null && _still_alive=$((_still_alive + 1))
        done
        if [[ "$_still_alive" -gt 0 ]]; then
            err "$_still_alive agent process(es) survived SIGKILL. Investigate manually (ps -p <pid>)."
            exit 1
        fi
    fi
fi

if [[ "$DRYRUN" == "true" ]]; then
    ok "DRY-RUN: loom-daemon-quiesce simulated. No real signal or systemctl action was taken."
else
    ok "loom-daemon-quiesce complete: dispatch stopped, $SCOPES_STOPPED scope(s) and ${_match_count} process(es) targeted."
fi
exit 0
