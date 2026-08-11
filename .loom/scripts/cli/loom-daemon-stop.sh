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
# Autonomy-desired marker (#4011): a normal operator stop is INTENT to stop, so
# it removes the durable `autonomy-desired` marker loom-daemon-start.sh wrote and
# tears down the watchdog scheduler — the launchd LaunchAgent on Darwin
# (`launchctl bootout`), or the `<unit>-watchdog.timer` + `.service` pair on a
# systemd Linux host (`systemctl --user disable --now`, #4260 sub-issue D) —
# after that the watchdog correctly stays silent (no daemon is expected). But
# the internal stop that
# loom-daemon-update.sh performs is NOT operator intent to stop; it is a restart,
# so update.sh passes --restarting (or LOOM_DAEMON_STOP_KEEP_INTENT=1) and this
# script PRESERVES the marker + watchdog. Inferring restart-vs-stop would be
# wrong (every self-update would silently disarm the detector — the exact bug
# class #4011 fixes), so it must be an explicit signal.
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-stop.sh              Graceful stop (SIGTERM -> SIGKILL); clears the autonomy-desired marker
#   ./.loom/scripts/cli/loom-daemon-stop.sh --force      Skip the grace window (SIGKILL)
#   ./.loom/scripts/cli/loom-daemon-stop.sh --restarting Restart (update.sh): PRESERVE the marker + watchdog
#   ./.loom/scripts/cli/loom-daemon-stop.sh --help
#
# Environment:
#   LOOM_DAEMON_STOP_GRACE_SECS   Grace window before SIGKILL (default 10)
#   LOOM_DAEMON_STOP_KEEP_INTENT  1/true/yes: preserve the autonomy-desired marker + watchdog (same as --restarting)
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

if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi
err()  { echo -e "${RED}$*${NC}" >&2; }
warn() { echo -e "${YELLOW}$*${NC}" >&2; }
ok()   { echo -e "${GREEN}$*${NC}"; }

show_help() {
    # Print the leading comment banner (line 2 through the last comment line
    # before `set -uo pipefail`), stripping the leading "# ". Same pattern as
    # loom-daemon-start.sh's show_help -- robust to the banner growing.
    awk 'NR>=2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

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
# Preserve the autonomy-desired marker + watchdog across this stop? True for a
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

PID_FILE="$DAEMON_STATE_HOME/.daemon.pid"
GRACE_SECS="${LOOM_DAEMON_STOP_GRACE_SECS:-10}"

# ---------- autonomy-desired marker + watchdog (#4011) ----------
SOCKET_PATH="${LOOM_SOCKET_PATH:-$HOME/.loom/loom-daemon.sock}"
LOOM_DIR="$(dirname "$SOCKET_PATH")"
INTENT_MARKER="${LOOM_AUTONOMY_MARKER:-$LOOM_DIR/autonomy-desired}"

# Remove the operator-intent marker and tear down the watchdog LaunchAgent /
# systemd timer+service. Only called on an operator-initiated stop (NOT a
# --restarting update.sh stop). After this the watchdog sees no marker and
# correctly stays silent — no false page on a deliberate stop. Best-effort;
# failures never change the stop's exit status.
teardown_autonomy_intent() {
    rm -f "$INTENT_MARKER" 2>/dev/null || true
    local wd_label wd_service
    wd_label="${LOOM_WATCHDOG_LABEL:-${LAUNCHD_LABEL}-watchdog}"
    # $LAUNCHD_DOMAIN is resolved below (gui/<uid> ↦ user/<uid>, #4130) before any
    # call to this function; the launchctl calls here are gated on USE_LAUNCHD.
    wd_service="${LAUNCHD_DOMAIN}/${wd_label}"
    if [[ "$USE_LAUNCHD" == "true" ]] && command -v launchctl >/dev/null 2>&1; then
        if launchctl print "$wd_service" >/dev/null 2>&1; then
            launchctl bootout "$wd_service" >/dev/null 2>&1 || true
        fi
    fi
    # systemd --user watchdog timer+service teardown (#4260 sub-issue D),
    # symmetric with the launchd bootout above. $IS_LINUX_SYSTEMD is resolved
    # below (before any call to this function); mirrors loom-daemon-start.sh's
    # resolve_systemd_watchdog_unit() naming (<daemon unit>-watchdog, same
    # LOOM_WATCHDOG_LABEL override) so stop always targets the SAME unit pair
    # start provisioned.
    if [[ "$IS_LINUX_SYSTEMD" == "true" ]] && command -v systemctl >/dev/null 2>&1; then
        local sd_daemon_unit sd_wd_unit
        sd_daemon_unit="$(resolve_systemd_unit)"
        sd_wd_unit="${LOOM_WATCHDOG_LABEL:-${sd_daemon_unit%.service}-watchdog}"
        systemctl --user disable --now "${sd_wd_unit}.timer" >/dev/null 2>&1 || true
        systemctl --user disable --now "${sd_wd_unit}.service" >/dev/null 2>&1 || true
    fi
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
            teardown_autonomy_intent
            ok "loom-daemon stopped + disabled (systemd unit $SYSTEMD_UNIT). Autonomy-desired marker cleared."
        else
            ok "loom-daemon stopped + disabled (systemd unit $SYSTEMD_UNIT). Autonomy-desired marker preserved (restart in progress)."
        fi
        echo "In-flight sweeps (if any) were left running by design; the next start reconciles them."
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
    if [[ "$LAUNCHD_LABEL" == "$DEFAULT_LAUNCHD_LABEL" ]] && command -v pgrep >/dev/null 2>&1; then
        pid=$(pgrep -f '(^|/)loom-daemon$' 2>/dev/null | head -n1 || true)
    fi
fi

if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    warn "No running loom-daemon found (nothing to stop)."
    rm -f "$PID_FILE"
    launchd_bootout_if_loaded
    if [[ "$KEEP_INTENT" != "true" ]]; then
        teardown_autonomy_intent
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
# Operator stop ⇒ clear the autonomy-desired marker + watchdog so the detector
# stays silent (no daemon is expected). A --restarting stop (update.sh) preserves
# both so a self-update never silently disarms the detector (#4011).
if [[ "$KEEP_INTENT" != "true" ]]; then
    teardown_autonomy_intent
    ok "loom-daemon stopped (pid $pid). Autonomy-desired marker cleared."
else
    ok "loom-daemon stopped (pid $pid). Autonomy-desired marker preserved (restart in progress)."
fi
echo "In-flight sweeps (if any) were left running by design; the next start reconciles them."
exit 0
