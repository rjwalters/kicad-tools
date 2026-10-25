#!/usr/bin/env bash
# loom-daemon-stop.sh - Clean shutdown for the RAW loom-daemon process
# (autonomous work-finder + main-health-gate host — epic #3809, Phase D #3813).
#
# This is NOT the tmux agent pool. `.loom/bin/loom stop` (loom-stop.sh) tears
# down the Manual-Orchestration-Mode tmux pool; THIS script stops the
# `loom-daemon` binary started by loom-daemon-start.sh.
#
# Shutdown model (drain vs. survive):
#   The daemon handles BOTH SIGINT (Ctrl-C) and SIGTERM (`kill <pid>`) by
#   removing its Unix socket and exiting cleanly (#3813). This script sends
#   SIGTERM, waits a grace window, then escalates to SIGKILL if needed.
#
#   In-flight `/loom:sweep` children are NOT cancelled. They are independent
#   detached processes that survive a daemon restart BY DESIGN — stopping the
#   dispatcher must not kill dispatched work. This "survive, don't drain"
#   decision means you can stop+start the daemon without losing running builds;
#   the reaper reconciles their state on the next start. To actively cancel a
#   sweep, use `mcp__loom__cancel_sweep` against a running daemon before stopping.
#
#   Scheduled ROLE agents (Champion/Curator/Judge/Doctor/Guide/…) survive this
#   stop for the same reason, and on a Linux `systemd --user` host they can
#   ALSO be architecturally detached from this daemon's own process tree — a
#   transient `systemd-run --user --scope` (issue #5111's CPU-quota mechanism)
#   parents them to the user manager, not to `loom-daemon`, so they keep
#   running and drawing on the token pool even after this script reports
#   success (issue #6129). This script's stop is intentionally NOT a fleet
#   quiesce. An operator who actually wants to stop dispatch AND every
#   in-flight role/sweep child — draining a host for maintenance or an
#   exhausted token pool — should run `loom-daemon-quiesce.sh` instead, which
#   does both, the same way on launchd and systemd.
#
# Linux systemd --user counterpart (#4268): when loom-daemon-start.sh installed
# the daemon as a `systemd --user` service, this script detects that ownership
# (`systemctl --user is-active`/`is-enabled <unit>`) and stops + DISABLES the unit
# (`systemctl --user disable --now <unit>`) instead of a raw pid kill — the systemd
# analog of the launchd bootout below. Disabling is what keeps a subsequent reboot
# from resurrecting the daemon (the unit sets [Install] WantedBy=default.target).
# The escape hatch LOOM_DAEMON_SYSTEMD=0 disables ALL systemd interaction
# symmetrically with loom-daemon-start.sh --no-systemd (#4078 analog), falling back
# to the pid-file/nohup tier.
#
# macOS launchd counterpart (#3972): when loom-daemon-start.sh loaded the
# daemon as a launchd LaunchAgent (in the resolve_launchd_domain() domain —
# gui/<uid> or user/<uid>, #4130), this script ALSO unloads the launchd
# job definition (`launchctl bootout`) after the process is confirmed dead —
# not just kills the pid. This matters because the generated plist sets
# RunAtLoad=true (mirroring the validated incident-fix plist), so leaving the
# definition loaded would silently relaunch the daemon at the next login/boot
# even though the operator explicitly stopped it. The pid-kill sequence itself
# is unchanged (SIGTERM -> grace -> SIGKILL, sent directly to the resolved pid).
#
# Interaction with the supervised restart primitive (#4054): the plist now uses
# KeepAlive:{SuccessfulExit:true}, so launchd relaunches the daemon on a clean
# exit 0. But the daemon exits NON-ZERO on SIGTERM (143) and SIGINT (130), so an
# operator stop is NOT a "successful" exit and launchd does not relaunch it -- the
# "operator stop stays stopped" guarantee holds WITHOUT depending on bootout
# timing (Curator Finding 1). SIGKILL (--force) likewise terminates the job by
# signal, never a clean exit, so it is not respawned either. The bootout below is
# therefore belt-and-braces (it still unloads the definition so it does not come
# back at the next login). After the stop this script re-verifies that no daemon
# for THIS launchd label is still alive and exits non-zero if one is -- closing
# the inverted-#4011 silent-success hole where a failed bootout could leave a
# relaunched daemon dispatching while the script reported success.
#
# Autonomy-desired marker (#4011, #9957): a normal operator stop is INTENT to
# stop, but the marker is the operator's standing wish, not stop state -- so the
# stop RECORDS itself instead of deleting it. `loom-daemon operator-stop record`
# moves the `autonomy-desired` marker loom-daemon-start.sh wrote aside, verbatim,
# into `<marker>.stopped` (the #9588 operator-stop record) with
# `operator_stop_reason=loom-daemon-stop.sh caller=<a<b<c>`: the caller's process
# chain as `comm` names only (never arguments). The watchdog is left armed; it
# honours the record (no revival, no page), so a stop it did not issue can no
# longer take the detector down with it. `loom-daemon restart --abort-drain`
# restores the marker from the record; an explicit loom-daemon-start.sh clears
# it. A binary older than #9957 lacks the verb: the stop then falls back to
# removing the marker, with a warning, and still succeeds. But the internal stop
# that loom-daemon-update.sh performs is NOT operator intent to stop; it is a
# restart, so update.sh passes --restarting (or LOOM_DAEMON_STOP_KEEP_INTENT=1)
# and this script leaves the marker untouched and writes no record. Inferring
# restart-vs-stop would be wrong (every self-update would silently disarm the
# detector -- the exact bug class #4011 fixes), so it must be an explicit signal.
#
# Test-only dry-run seam (#5501): a test proving "default label = the
# operator's real stop, behaviour unchanged" needs LOOM_LAUNCHD_LABEL /
# LOOM_WATCHDOG_LABEL to resolve to the REAL production values — but doing
# that against a live host is exactly the incident this seam exists to close
# (verifying #5131 booted the operator's real daemon, see #5501). With
# LOOM_DAEMON_STOP_DRYRUN=1, this script still RESOLVES the pid / launchd job /
# systemd unit it would act on (so the "what would this target" logic is fully
# exercised), but every MUTATING action against it — kill -TERM/-KILL, launchctl
# bootout, systemctl --user disable --now — becomes a logged "DRY-RUN: would ..." line instead of a real
# syscall. Pair with LOOM_DAEMON_STOP_DRYRUN_LOG=<path> to capture those lines
# for a test to grep instead of parsing captured stdout.
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-stop.sh              Graceful stop (SIGTERM -> SIGKILL); records the stop (autonomy-desired -> autonomy-desired.stopped)
#   ./.loom/scripts/cli/loom-daemon-stop.sh --force      Skip the grace window (SIGKILL)
#   ./.loom/scripts/cli/loom-daemon-stop.sh --restarting Restart (update.sh): leave the marker in place, record nothing
#   ./.loom/scripts/cli/loom-daemon-stop.sh --help
#
# Environment:
#   LOOM_PID_FILE                 #6386: the pid file to read, TIER 1 -- ahead of the
#                                 $PWD/machine-derived "<state home>/.daemon.pid".
#                                 Same precedence the daemon (daemon_pidfile.rs) and
#                                 loom-daemon-watchdog.sh use, and the value
#                                 loom-daemon-start.sh exports, so all four ends
#                                 always mean the SAME file. Before #6386 this script
#                                 ignored it and killed whatever $PWD's repo resolved
#                                 to -- which SIGTERM'd a live fleet dispatcher during
#                                 a test run that had explicitly pointed it elsewhere.
#   LOOM_DAEMON_STOP_GRACE_SECS   Grace window before SIGKILL (default 10)
#   LOOM_DAEMON_STOP_KEEP_INTENT  1/true/yes: preserve the autonomy-desired marker, record nothing (same as --restarting)
#   LOOM_DAEMON_SELF_BIN          The loom-daemon that implements `operator-stop record`;
#                                 else the usual lib/locate-daemon-bin.sh resolution.
#   LOOM_DAEMON_STOP_DRYRUN       1/true/yes: TEST-ONLY. Resolve the target pid/launchd
#                                 job/systemd unit but never actually kill/bootout/disable
#                                 it — logs "DRY-RUN: would ..." instead (#5501). The
#                                 supported way to exercise default-label semantics
#                                 without touching a real supervised job.
#   LOOM_DAEMON_STOP_DRYRUN_LOG   Path to also append each dry-run action line to
#                                 (in addition to stdout), for a test to grep.
#   LOOM_LAUNCHD_LABEL            macOS only: the LaunchAgent label to bootout (default com.rjwalters.loom-daemon)
#   LOOM_LAUNCHD_DOMAIN           macOS only: pin the launchd domain (gui/<uid> or
#                                 user/<uid>); else auto-resolved gui→user (#4130).
#                                 Must match the domain the start used so the
#                                 bootout targets the right service.
#   LOOM_DAEMON_LAUNCHD           macOS only: 0/false/no disables ALL launchd interaction
#                                 (lookup + bootout), symmetric with loom-daemon-start.sh.
#                                 A start done with --no-launchd / LOOM_DAEMON_LAUNCHD=0
#                                 must get a stop that never reads or mutates the
#                                 machine-global launchd domain (issue #4078).
#   LOOM_DAEMON_SYSTEMD           Linux only: 0/false/no disables ALL systemd interaction
#                                 (is-active/is-enabled lookup + disable --now),
#                                 symmetric with loom-daemon-start.sh --no-systemd (#4268).
#   LOOM_SYSTEMD_UNIT             Linux only: the systemd --user unit to stop + disable
#                                 (default loom-daemon.service); must match the start's.
#   LOOM_MACHINE_CHECKOUT         Machine mode (Epic #3835 Phase 3b, #4229): set
#                                 by the `scripts/loom` dispatcher before it execs
#                                 this script. When set, the pid file is read from
#                                 the machine-level state home (~/.loom) instead of
#                                 $PWD's repo -- matching loom-daemon-start.sh, so
#                                 `loom stop` always targets the same machine-wide
#                                 daemon regardless of the invoking directory.
#                                 Direct invocation (no dispatcher) never sets it.
#
# Exit codes:
#   0  daemon stopped (or was not running)
#   1  usage error / failed to stop

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
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi
err()  { echo -e "${RED}$*${NC}" >&2; }
warn() { echo -e "${YELLOW}$*${NC}" >&2; }
ok()   { echo -e "${GREEN}$*${NC}"; }

# Prints the banner captured at startup above -- no filesystem access (#7794).
show_help() { printf '%s\n' "$_LOOM_HELP_BANNER"; }

find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then echo "$dir"; return 0; fi
        if [[ -f "$dir/.git" ]]; then
            local gitdir main_repo
            gitdir=$(sed 's/^gitdir: //' "$dir/.git")
            main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
            if [[ -d "$main_repo/.loom" ]]; then echo "$main_repo"; return 0; fi
        fi
        dir="$(dirname "$dir")"
    done
    echo ""
}

FORCE=false
# Leave the autonomy-desired marker in place and record nothing? True for a
# restart (update.sh), false for an operator stop. Env or --restarting.
KEEP_INTENT=false
if [[ "${LOOM_DAEMON_STOP_KEEP_INTENT:-}" =~ ^(1|true|yes|on)$ ]]; then
    KEEP_INTENT=true
fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) show_help; exit 0 ;;
        --force|-f) FORCE=true; shift ;;
        --restarting) KEEP_INTENT=true; shift ;;
        *) err "Unknown option '$1'"; echo "Use --help for usage" >&2; exit 1 ;;
    esac
done

REPO_ROOT=$(find_repo_root)

# ---------- machine-mode resolution (Epic #3835 Phase 3b, #4229) ----------
# Mirrors loom-daemon-start.sh: LOOM_MACHINE_CHECKOUT (set by the dispatcher)
# is authoritative regardless of $PWD, so `loom stop` always resolves the SAME
# pid file loom-daemon-start.sh wrote for the machine-wide singleton daemon --
# not a different $PWD-derived one. Direct invocation is unaffected.
MACHINE_CHECKOUT="${LOOM_MACHINE_CHECKOUT:-}"
if [[ -n "$MACHINE_CHECKOUT" ]]; then
    if [[ ! -d "$MACHINE_CHECKOUT" ]]; then
        err "LOOM_MACHINE_CHECKOUT does not exist: $MACHINE_CHECKOUT"
        exit 1
    fi
    DAEMON_STATE_HOME="$HOME/.loom"
elif [[ -n "$REPO_ROOT" ]]; then
    DAEMON_STATE_HOME="$REPO_ROOT/.loom"
else
    err "Not in a Loom workspace (.loom directory not found)"
    exit 1
fi

# ---------- pid-file resolution (#6386) ----------
# LOOM_PID_FILE is TIER 1, ahead of the $PWD/machine-derived state home --
# exactly the precedence the daemon's own `daemon_pidfile::resolve_pid_file_path_from`
# and loom-daemon-watchdog.sh's resolve_pid_file() already use, and the value
# loom-daemon-start.sh exports (and bakes into the rendered plist / systemd
# unit) for the pid file the daemon actually claims.
#
# Before #6386 this script IGNORED LOOM_PID_FILE entirely and always read
# "$DAEMON_STATE_HOME/.daemon.pid" -- while honoring LOOM_SOCKET_PATH for the
# marker/loom-dir. That split resolution is what killed the fleet's live
# dispatcher for 11h: an Auditor run of the shell test suites from the LIVE
# checkout invoked this script with LOOM_PID_FILE pointed at a scratch file
# (and no `cd` into a fixture), so `find_repo_root` walked up from $PWD onto
# the real checkout, and the script SIGTERM'd + `rm -f`'d the REAL daemon's
# pid file that the caller had explicitly told it not to touch.
#
# On a real host this is a no-op: whatever LOOM_PID_FILE is present there was
# exported by loom-daemon-start.sh and already names the same file the tier
# below derives. It differs only when a caller DELIBERATELY names another file
# -- which is precisely the request that must be honored, not overruled by cwd.
#
# Deliberately NOT widened: the "Not in a Loom workspace" refusal above still
# runs first, so LOOM_PID_FILE narrows which pid file a stop targets but never
# grants a stop from a directory that previously refused outright. Letting the
# env var bypass that gate would ENLARGE the blast radius (an agent session
# exports LOOM_PID_FILE=<the real .daemon.pid> into every child it spawns, so a
# stray `loom-daemon-stop.sh` from /tmp would newly reach the live daemon) --
# the opposite of this fix's purpose. An empty value is skipped exactly like an
# unset one, which is how the suites pin a case to the derived tier on purpose.
if [[ -n "${LOOM_PID_FILE:-}" ]]; then
    PID_FILE="$LOOM_PID_FILE"
else
    PID_FILE="$DAEMON_STATE_HOME/.daemon.pid"
fi
GRACE_SECS="${LOOM_DAEMON_STOP_GRACE_SECS:-10}"

# ---------- dry-run seam (#5501) ----------
# See the header comment. Resolution stays real; every mutating action against
# the resolved pid/job/unit is replaced by a logged line instead.
DRYRUN=false
if [[ "${LOOM_DAEMON_STOP_DRYRUN:-}" =~ ^(1|true|yes|on)$ ]]; then
    DRYRUN=true
fi
dryrun_log() {
    echo "DRY-RUN: $*"
    if [[ -n "${LOOM_DAEMON_STOP_DRYRUN_LOG:-}" ]]; then
        printf '%s\n' "$*" >> "$LOOM_DAEMON_STOP_DRYRUN_LOG"
    fi
}

# ---------- autonomy-desired marker -> operator-stop record (#4011, #9957) ----------
SOCKET_PATH="${LOOM_SOCKET_PATH:-$HOME/.loom/loom-daemon.sock}"
LOOM_DIR="$(dirname "$SOCKET_PATH")"
INTENT_MARKER="${LOOM_AUTONOMY_MARKER:-$LOOM_DIR/autonomy-desired}"

# The caller's process chain, `comm` names only -- NEVER arguments, which can
# carry secrets (CLAUDE.md rule 12). Up to three ancestors from $PPID, nearest
# first, joined by `<` (e.g. `bash<claude<launchd`). basename because macOS
# `ps -o comm=` prints the executable's full path. `unknown` when ps is absent.
stop_caller_chain() {
    local pid="$PPID" chain="" comm
    for _ in 1 2 3; do
        [[ "$pid" =~ ^[0-9]+$ ]] && (( pid > 0 )) || break
        comm="$(ps -o comm= -p "$pid" 2>/dev/null)" || break
        comm="${comm##*/}"
        comm="${comm//[[:space:]]/}"
        [[ -n "$comm" ]] || break
        chain="${chain:+$chain<}$comm"
        (( pid == 1 )) && break
        pid="$(ps -o ppid= -p "$pid" 2>/dev/null)" || break
        pid="${pid//[[:space:]]/}"
    done
    printf '%s\n' "${chain:-unknown}"
}

# Record an operator-initiated stop (NOT a --restarting update.sh stop) via the
# #9588 operator-stop record, written by the daemon itself so the format has one
# implementation: the marker moves aside, verbatim, into "$INTENT_MARKER.stopped".
# The watchdog is deliberately left armed -- it reads the record and stays
# silent, and a stop it did not issue can no longer disarm it (#9957).
#
# --if-marker: record only when a marker exists (the "nothing to stop" path,
# where there is no daemon to attribute a stop to and no intent to move aside).
#
# A binary that predates the verb (or none at all) falls back to the pre-#9957
# `rm -f` with a warning. Best-effort; never changes the stop's exit status.
# Sets STOP_INTENT_NOTE for the final summary line.
# requires-daemon: operator-stop optional   Without the `record` verb (absent binary, or one predating #9957) the stop removes the autonomy-desired marker as before, with a warning; the stop itself never fails on it.
STOP_INTENT_NOTE=""
record_operator_stop() {
    [[ "${1:-}" == "--if-marker" && ! -f "$INTENT_MARKER" ]] && return 0
    local reason bin
    reason="loom-daemon-stop.sh caller=$(stop_caller_chain)"
    bin="$(declare -f loom_locate_daemon_bin >/dev/null 2>&1 \
        && { loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT"; })"
    if [[ -n "$bin" ]] && "$bin" operator-stop record --marker "$INTENT_MARKER" \
        --reason "$reason" >/dev/null 2>&1; then
        STOP_INTENT_NOTE="Operator stop recorded in $INTENT_MARKER.stopped ($reason)."
        return 0
    fi
    warn "Could not record the operator stop (${bin:-no loom-daemon found}; a binary older than #9957 lacks \`operator-stop record\`)."
    warn "  Removing the autonomy-desired marker instead (pre-#9957 behaviour)."
    rm -f "$INTENT_MARKER" 2>/dev/null || true
    STOP_INTENT_NOTE="Autonomy-desired marker cleared (operator-stop record unavailable)."
}

# ---------- launchd plumbing (macOS, #3972) ----------
# Shared domain resolver (#4130): gui/<uid> ↦ user/<uid>, sourced verbatim so
# stop agrees with the domain the start put the job in.
_LOOM_LAUNCHD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh" ]]; then
    # shellcheck source=../lib/launchd-domain.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh"
fi
# systemd --user resolver (#4268) — is_linux_systemd / resolve_systemd_unit,
# shared with loom-daemon-start.sh so stop agrees on the unit the start installed.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/systemd-user.sh" ]]; then
    # shellcheck source=../lib/systemd-user.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/systemd-user.sh"
fi
# loom-daemon resolver (#8134) for `operator-stop record` (#9957). Optional:
# without it the stop falls back to removing the marker.
# shellcheck source=../lib/locate-daemon-bin.sh
[[ -r "$_LOOM_LAUNCHD_LIB_DIR/locate-daemon-bin.sh" ]] && source "$_LOOM_LAUNCHD_LIB_DIR/locate-daemon-bin.sh"

IS_DARWIN=false
[[ "$(uname -s)" == "Darwin" ]] && IS_DARWIN=true

# Honor LOOM_DAEMON_LAUNCHD symmetrically with loom-daemon-start.sh (#4078): a
# daemon started with --no-launchd / LOOM_DAEMON_LAUNCHD=0 was never a launchd
# job, so the stop side must NOT reach into the machine-global launchd domain to
# look it up (which would resolve against — and then SIGTERM — the operator's
# real production LaunchAgent under the same default label). Before this, the
# start script gated its whole launchd path on this var but the stop script did
# not, leaving the guard inert on the stop side.
USE_LAUNCHD="$IS_DARWIN"
if [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]]; then
    USE_LAUNCHD=false
fi

DEFAULT_LAUNCHD_LABEL="com.rjwalters.loom-daemon"
LAUNCHD_LABEL="${LOOM_LAUNCHD_LABEL:-$DEFAULT_LAUNCHD_LABEL}"
# Resolve the domain ONLY when launchd interaction is on (#4130): probing
# `launchctl print gui/<uid>` when LOOM_DAEMON_LAUNCHD=0 would reach the
# machine-global launchd domain the disabled path must never touch (#4078). The
# placeholder below is inert — every launchd function short-circuits on
# USE_LAUNCHD, so it is never consumed when launchd is off.
if [[ "$USE_LAUNCHD" == "true" ]]; then
    LAUNCHD_DOMAIN="$(resolve_launchd_domain)"
else
    LAUNCHD_DOMAIN=""
fi
LAUNCHD_SERVICE="${LAUNCHD_DOMAIN}/${LAUNCHD_LABEL}"

launchd_job_loaded() {
    [[ "$USE_LAUNCHD" == "true" ]] || return 1
    command -v launchctl >/dev/null 2>&1 || return 1
    launchctl print "$LAUNCHD_SERVICE" >/dev/null 2>&1
}

launchd_job_pid() {
    launchctl print "$LAUNCHD_SERVICE" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}'
}

# Unload the launchd job definition so it does NOT silently relaunch at the
# next login/boot (the generated plist sets RunAtLoad=true). Idempotent and
# best-effort -- a job that was never loaded (or already unloaded) is a no-op.
launchd_bootout_if_loaded() {
    if launchd_job_loaded; then
        if [[ "$DRYRUN" == "true" ]]; then
            dryrun_log "would launchctl bootout $LAUNCHD_SERVICE"
            return 0
        fi
        launchctl bootout "$LAUNCHD_SERVICE" >/dev/null 2>&1 || true
    fi
}

# ---------- systemd --user ownership tier (Linux, #4268) ----------
# When the daemon was started as a `systemd --user` service, stop + DISABLE the
# unit rather than raw-killing the pid: disabling is what keeps a reboot from
# resurrecting it (the unit sets WantedBy=default.target). This is the systemd
# analog of the launchd bootout tier. Honors LOOM_DAEMON_SYSTEMD=0 symmetrically
# with loom-daemon-start.sh --no-systemd (#4078 analog): a start done with
# --no-systemd was a plain nohup job, so the stop must never reach into the
# systemd user manager to look it up. When systemd is off, absent, or the unit
# is not this daemon's, fall through to the pid-file/nohup tier below.
IS_LINUX_SYSTEMD=false
if ! [[ "${LOOM_DAEMON_SYSTEMD:-}" =~ ^(0|false|no)$ ]] \
    && declare -f is_linux_systemd >/dev/null 2>&1 && is_linux_systemd; then
    IS_LINUX_SYSTEMD=true
fi
if [[ "$IS_LINUX_SYSTEMD" == "true" ]]; then
    SYSTEMD_UNIT="$(resolve_systemd_unit)"
    # Only adopt the systemd tier when a unit under THIS name is actually loaded
    # (active or enabled) — otherwise a --no-systemd / nohup start (or a stale
    # scratch unit) should route to the pid-file tier, not a no-op disable.
    if systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null \
        || systemctl --user is-enabled --quiet "$SYSTEMD_UNIT" 2>/dev/null; then
        if [[ "$DRYRUN" == "true" ]]; then
            # #5501: never actually stop/disable the resolved unit -- log the
            # intended action and skip the mutating call + its post-disable
            # verification (which assumes the disable really happened).
            dryrun_log "would systemctl --user disable --now $SYSTEMD_UNIT"
            if [[ "$KEEP_INTENT" != "true" ]]; then
                record_operator_stop
                ok "DRY-RUN: loom-daemon stop simulated (systemd unit $SYSTEMD_UNIT). $STOP_INTENT_NOTE No real action taken."
            else
                ok "DRY-RUN: loom-daemon stop simulated (systemd unit $SYSTEMD_UNIT). No real action taken."
            fi
            exit 0
        fi
        echo "Stopping loom-daemon via systemd (systemctl --user disable --now $SYSTEMD_UNIT)..."
        systemctl --user disable --now "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
        # Verify the unit is actually down — a disable --now that did not stop the
        # service is the systemd analog of a bootout that did not stick (the
        # inverted-#4011 silent-success hole): fail loudly instead of reporting success.
        if systemctl --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null; then
            err "loom-daemon is still active under systemd ($SYSTEMD_UNIT) after disable --now."
            err "Retry the stop, or disable manually: systemctl --user disable --now $SYSTEMD_UNIT"
            exit 1
        fi
        rm -f "$PID_FILE"
        if [[ "$KEEP_INTENT" != "true" ]]; then
            record_operator_stop
            ok "loom-daemon stopped + disabled (systemd unit $SYSTEMD_UNIT). $STOP_INTENT_NOTE"
        else
            ok "loom-daemon stopped + disabled (systemd unit $SYSTEMD_UNIT). Autonomy-desired marker preserved (restart in progress)."
        fi
        echo "In-flight sweeps and role agents (if any) were left running by design; the next start reconciles sweeps. To also stop them, run: .loom/scripts/cli/loom-daemon-quiesce.sh (issue #6129)."
        exit 0
    fi
fi

# Resolve the target pid: prefer the PID file, else a launchd lookup (Darwin),
# else best-effort pgrep.
pid=""
if [[ -f "$PID_FILE" ]]; then
    pid=$(cat "$PID_FILE" 2>/dev/null || true)
fi
if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    # PID file missing/stale — try the launchd job (macOS) before falling
    # back to a process-name match.
    if launchd_job_loaded; then
        launchd_pid=$(launchd_job_pid)
        [[ -n "$launchd_pid" ]] && pid="$launchd_pid"
    fi
fi
if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    # Last-resort process-name match. This tier is LABEL-BLIND: `pgrep -f`
    # matches ANY loom-daemon on the machine by binary name — including the
    # operator's production daemon or another repo's daemon — so it can kill a
    # daemon this invocation was never meant to touch (issue #4078, curator
    # Correction 3; the incident's actual over-broad kill). Only fall through to
    # it when the caller did NOT explicitly scope this stop to a specific
    # launchd label: a non-default LOOM_LAUNCHD_LABEL (a test's scratch label,
    # or an operator managing a specifically-labeled daemon) means "stop THAT
    # daemon, not whatever else happens to be named loom-daemon", so a
    # label-blind kill would violate that scoping and contradict the #4054
    # label-scoped-stop discipline. With the default label we keep the fallback
    # as a genuine lost-PID-file recovery path.
    # #5548 cross-reference: this tier's `pgrep -f` is still forgeable by a
    # leaked test fixture whose path literally ends in `/loom-daemon` (the
    # exact shape #5548 hit) -- the `(^|/)...$` anchor narrows it versus a
    # bare substring match, but does not close that gap. No code change here:
    # this is the intentionally-last-resort tier, already gated behind the
    # default-label check above and already covered by a decoy regression
    # test (test-loom-daemon-update.sh's suite-level decoy, #4078). #5548's
    # actual fixes are in scripts/stop-daemon.sh, scripts/start-daemon.sh, and
    # the renamed `loom-daemon-mock` test fixtures elsewhere in this repo.
    if [[ "$LAUNCHD_LABEL" == "$DEFAULT_LAUNCHD_LABEL" ]] && command -v pgrep >/dev/null 2>&1; then
        pid=$(pgrep -f '(^|/)loom-daemon$' 2>/dev/null | head -n1 || true)
    fi
fi

if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    warn "No running loom-daemon found (nothing to stop)."
    rm -f "$PID_FILE"
    launchd_bootout_if_loaded
    # #5131: apply the SAME scoping to the teardown that already protects the
    # kill. The pid resolver's last-resort  tier is deliberately skipped
    # for a non-default LOOM_LAUNCHD_LABEL (#4078) — "stop THAT daemon, not
    # whatever else is named loom-daemon". But the marker is
    # HOST-GLOBAL ($LOOM_DIR/autonomy-desired), while PID_FILE is per-workspace,
    # so a label-scoped stop that legitimately finds nothing would still disarm
    # the whole host and exit 0 — silently, since "nothing to stop" reads like a
    # correct no-op.
    #
    # An invocation that scoped its label but NOT its marker is asking to stop
    # one specific daemon; it is not asking to revoke host-wide autonomy. Only
    # tear down when this stop owns that state: the default label (the
    # operator's real daemon), or an explicit LOOM_AUTONOMY_MARKER (the caller
    # scoped the marker too — what the test suites do via
    # live_state_sandbox_init).
    _teardown_is_in_scope=true
    if [[ "$LAUNCHD_LABEL" != "$DEFAULT_LAUNCHD_LABEL" && -z "${LOOM_AUTONOMY_MARKER:-}" ]]; then
        _teardown_is_in_scope=false
    fi
    if [[ "$KEEP_INTENT" != "true" ]]; then
        if [[ "$_teardown_is_in_scope" == "true" ]]; then
            # Nothing was stopped, so record only when there is intent to move
            # aside -- a repeated stop on a quiet host invents no record.
            record_operator_stop --if-marker
        else
            warn "Label-scoped stop (LOOM_LAUNCHD_LABEL=$LAUNCHD_LABEL) found no daemon;"
            warn "  leaving the host-global autonomy marker untouched (#5131)."
            warn "  Set LOOM_AUTONOMY_MARKER to scope the marker too, or use the default"
            warn "  label, if this stop is meant to disarm the host."
        fi
    fi
    exit 0
fi

if [[ "$DRYRUN" == "true" ]]; then
    # #5501: a pid WAS resolved (possibly via the launchd-label fallback above,
    # which is exactly how a #5501-shaped default-label test would reach the
    # operator's real daemon) -- log what would happen and stop here, never
    # sending a real signal, never calling launchctl bootout for real, and
    # never running the "still alive" verification below (it assumes a real
    # stop was attempted).
    if [[ "$FORCE" == "true" ]]; then
        dryrun_log "would SIGKILL pid $pid"
    else
        dryrun_log "would SIGTERM pid $pid (grace ${GRACE_SECS}s), escalating to SIGKILL on timeout"
    fi
    launchd_bootout_if_loaded
    rm -f "$PID_FILE"
    if [[ "$KEEP_INTENT" != "true" ]]; then
        record_operator_stop
        ok "DRY-RUN: loom-daemon stop simulated (pid $pid, target $LAUNCHD_SERVICE). $STOP_INTENT_NOTE No real signal or launchd/systemd action was taken."
    else
        ok "DRY-RUN: loom-daemon stop simulated (pid $pid, target $LAUNCHD_SERVICE). No real signal or launchd/systemd action was taken."
    fi
    exit 0
fi

if [[ "$FORCE" == "true" ]]; then
    warn "Force-killing loom-daemon (pid $pid) with SIGKILL..."
    kill -KILL "$pid" 2>/dev/null || true
else
    echo "Stopping loom-daemon (pid $pid) with SIGTERM (grace ${GRACE_SECS}s)..."
    kill -TERM "$pid" 2>/dev/null || true
    # Wait up to the grace window for a clean exit.
    waited=0
    while kill -0 "$pid" 2>/dev/null && (( waited < GRACE_SECS )); do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        warn "Daemon did not exit within ${GRACE_SECS}s — escalating to SIGKILL."
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
fi

if kill -0 "$pid" 2>/dev/null; then
    err "Failed to stop loom-daemon (pid $pid)."
    exit 1
fi

# Unload the launchd job definition (macOS, #3972) now that the process is
# confirmed dead -- RunAtLoad=true on the generated plist means leaving it
# loaded would silently relaunch the daemon at the next login/boot.
launchd_bootout_if_loaded

# Post-stop verification (#4054): assert no daemon for THIS launchd label is
# still alive, rather than trusting that killing the original pid was enough.
# Under KeepAlive:SuccessfulExit a relaunched daemon carries a DIFFERENT pid than
# the one we killed, so re-testing the original pid alone would miss it. A
# still-loaded job with a live pid means the bootout did not stick (the
# inverted-#4011 silent-success hole) -- fail loudly instead of reporting
# success. Scoped to this label (not a global `pgrep loom-daemon`) so a test
# daemon under a non-default LOOM_LAUNCHD_LABEL never false-positives against a
# separate production daemon, and vice versa.
if launchd_job_loaded; then
    relaunched_pid=$(launchd_job_pid)
    if [[ -n "$relaunched_pid" ]] && kill -0 "$relaunched_pid" 2>/dev/null; then
        err "loom-daemon is still alive (pid $relaunched_pid) under $LAUNCHD_SERVICE after stop."
        err "The launchd bootout did not stick — the daemon may still be dispatching."
        err "Retry the stop, or bootout manually: launchctl bootout $LAUNCHD_SERVICE"
        exit 1
    fi
fi

rm -f "$PID_FILE"
# Operator stop ⇒ record it (#9957): the marker moves aside into the record, so
# the still-armed watchdog stays silent (no daemon is expected). A --restarting
# stop (update.sh) leaves the marker alone so a self-update never silently
# disarms the detector (#4011).
if [[ "$KEEP_INTENT" != "true" ]]; then
    record_operator_stop
    ok "loom-daemon stopped (pid $pid). $STOP_INTENT_NOTE"
else
    ok "loom-daemon stopped (pid $pid). Autonomy-desired marker preserved (restart in progress)."
fi
echo "In-flight sweeps and role agents (if any) were left running by design; the next start reconciles sweeps. To also stop them, run: .loom/scripts/cli/loom-daemon-quiesce.sh (issue #6129)."
exit 0
