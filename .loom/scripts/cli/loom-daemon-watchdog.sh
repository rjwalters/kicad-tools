#!/usr/bin/env bash
# loom-daemon-watchdog.sh - Host-side autonomy-loss detector for the RAW
# loom-daemon process (Issue #4011).
#
# THE PROBLEM IT SOLVES
#   On 2026-07-26 the loom-daemon launchd job took a SIGTERM two seconds after
#   starting and was left `bootout`-ed (unloaded) from launchd. Autonomous
#   dispatch (work finder, role runner) silently stopped. NOTHING surfaced it —
#   no log line, no forge signal, no notification. It was discovered hours later
#   only because someone happened to run `loom-daemon status` by hand. A pull
#   nobody performed for hours is not a detector.
#
# WHAT THIS IS
#   The payload of a SECOND launchd job (`<daemon-label>-watchdog`) that runs on
#   a `StartInterval` cadence, SEPARATE from the daemon job. It compares two
#   things:
#     (1) operator INTENT — the durable `autonomy-desired` marker that
#         loom-daemon-start.sh writes on a successful start and only an
#         operator-initiated loom-daemon-stop.sh removes; and
#     (2) REALITY — whether a daemon for the expected launchd label is actually
#         loaded and alive, and whether its declared-cadence heartbeat file
#         (written by the daemon, #4011) is fresh.
#   When intent says "a daemon should be running" but reality disagrees, it
#   REPORTS loudly (a timestamped line to the watchdog log + stderr, which
#   launchd captures) instead of staying silent.
#
# WHY A SECOND LAUNCHD JOB, NOT A RESIDENT PROCESS
#   The reporter must live OUTSIDE the daemon process: a dead daemon cannot
#   report its own death (which is why #3971's in-daemon watch loop is not
#   reusable here). And it must itself be supervised — but a long-lived resident
#   watchdog just moves the "who watches the watchdog" problem up one level (it
#   too can crash and stay dead). A `StartInterval` job owns NO long-lived
#   process: launchd re-runs it every interval regardless of how the last run
#   exited, so it structurally cannot crash-and-stay-dead. That is what resolves
#   the recursion.
#
# WHY THE MARKER, NOT "is the pid file / launchd job present"
#   loom-daemon-stop.sh boots out the job AND deletes the pid file, so after ANY
#   stop those would be gone — making a deliberately-stopped daemon and a
#   silently-dead one byte-identical. A detector built on them would page on
#   every intentional stop or never page at all. The marker's lifetime is
#   OPERATOR INTENT: present ⇒ a daemon is expected; absent ⇒ it was
#   deliberately stopped (or never started) ⇒ stay silent.
#
# HANG-AWARE IPC LIVENESS PROBE (#4398)
#   Process-exists + heartbeat-fresh are BOTH out-of-band signals: neither ever
#   talks to the daemon over the socket it actually serves work on. Two observed
#   incidents show why that is not enough:
#     - #4381: the installed loom-daemon binary was replaced by a stub that
#       answered `--version` and then hung forever. A pid-alive check passes
#       against that indefinitely.
#     - 2026-07-29: the production daemon (pid 1484) was alive AND writing a
#       FRESH heartbeat while every `loom-daemon status` round-trip hung. The
#       heartbeat writer (`daemon_heartbeat::spawn_heartbeat_task`) and the IPC
#       accept loop are independent `tokio::spawn`ed tasks on a multi-threaded
#       runtime, so one can keep ticking while the other is wedged.
#   So after liveness is confirmed, this job also runs a BOUNDED in-band IPC
#   round-trip through the installed loom-daemon CLI. Design points:
#     * BOUNDED TWICE. The CLI bounds its own connect + round-trip (5s each,
#       `query_daemon_bounded`); this job additionally wraps the invocation in a
#       hard external timeout, so even a CLI that never returns at all (the
#       #4381 stub) cannot hang the watchdog tick.
#     * LIGHTWEIGHT SUBCOMMAND, NOT `status`. The probe defaults to
#       `quarantine list` — a pure IPC round-trip with no post-reply work.
#       `loom-daemon status --json` looks like the obvious probe but, AFTER a
#       successful round-trip, it also runs a per-account token-pool network
#       check plus a self-update check; measured at 15.3s against a HEALTHY
#       daemon, which would make the probe timeout itself the false-positive
#       generator. Override with LOOM_WATCHDOG_IPC_PROBE_ARGS if desired.
#     * DEBOUNCED. A single failed round-trip can be transient contention (a
#       per-connection task dropping a `status` under concurrent-sweep load —
#       #4279). One failure is REPORTED (loud, logged) but only N consecutive
#       failures, tracked in <loom_dir>/.watchdog-probe-fail-count and keyed to
#       the live pid, are called a CONFIRMED hang.
#     * STARTUP-GRACE AWARE. For up to ~90s after a supervised relaunch the
#       socket may not be bound yet even though launchd reports a live pid
#       (#4213). A probe against a process younger than the grace window is
#       skipped entirely and never counts toward the confirmed-hang tally —
#       the same window `daemon_install_state::DEFAULT_STARTUP_GRACE_SECS`
#       uses, so `status` and this watchdog cannot disagree.
#     * GRACEFULLY DEGRADING. No resolvable loom-daemon binary, a build whose
#       CLI does not know the probe subcommand, or any daemon-side application
#       error (the IPC round-trip demonstrably WORKED) skips the probe rather
#       than inventing a divergence. The probe must never become a new hard
#       dependency that pages on its own absence.
#     * REPORT-ONLY, DELIBERATELY. Unlike #4232's narrow auto-`kickstart`, there
#       is NO provably-safe unattended remediation for a wedged-but-alive
#       process: the only real fix is killing it, which would also kill a daemon
#       that is merely under heavy legitimate load. A confirmed hang therefore
#       escalates to a maximally actionable DIVERGENCE report (exit 1) with the
#       explicit recovery commands — never an automatic kill/restart.
#
# SOCKET-FIRST LIVENESS, PID FILE DEMOTED TO A HINT (#5118)
#   Until #5118 the OUT-OF-BAND probe above was the ONLY thing that could
#   establish liveness, and on a host with neither a launchd job nor a
#   systemd-managed unit to ask, that reduced to a single artifact: the pid
#   file. Three stacked defects made that unusable fleet-wide:
#     1. PATH DISAGREEMENT. This script derived the pid file from the SOCKET's
#        directory (`<dirname $LOOM_SOCKET_PATH>/.daemon.pid`) in the
#        marker-absent path, while the daemon derives it from the START
#        SCRIPT's `LOOM_PID_FILE` / the workspace (`daemon_pidfile.rs`). On a
#        workspace-rooted install those are never the same directory.
#     2. `LOOM_PID_FILE` WAS IGNORED HERE. It is exported by
#        loom-daemon-start.sh and baked into every plist/unit it renders — and
#        honored by the daemon as precedence tier 1 — but this script had no
#        support for it at all, so the one variable that is supposed to
#        single-source the path could not align the two ends.
#     3. A STALE pid file reads as a CONFIRMED death. A pid file naming a
#        long-dead pid (#4774, before the daemon self-wrote it at bind) is
#        indistinguishable here from "no daemon".
#   Observed 2026-08-03: BOTH fleet hosts ran healthy daemons while this
#   watchdog reported `[DIVERGENCE] ... no live pid file at ~/.loom/.daemon.pid`
#   every five minutes since 2026-08-01. An alarm that is always on carries no
#   information — and two genuine outages that same session were
#   indistinguishable from it.
#
#   The fix inverts the precedence to match what `loom-daemon health` already
#   does: the IPC ROUND-TRIP IS AUTHORITATIVE, the pid file is a corroborating
#   hint. Concretely, when the out-of-band probe does NOT find a live daemon
#   this script now ASKS THE SOCKET before reporting anything:
#     * socket ANSWERS + no supervisor was consulted (pid-file path) ⇒ HEALTHY.
#       The pid file's absence/staleness is a note, never a page.
#     * socket ANSWERS + the supervisor (launchd/systemd) says its job is down
#       ⇒ a WARN state mismatch: a daemon is serving work but nothing is
#       supervising it. Auto-remediation is SUPPRESSED — kickstarting a second
#       daemon at a socket a live one already owns only produces a refusal.
#     * socket is UNREACHABLE ⇒ the daemon really is down: the existing
#       DIVERGENCE + bounded auto-remediation path, unchanged.
#     * liveness CANNOT BE DETERMINED (no probe available — no resolvable
#       binary, probe disabled, CLI wedged — AND no usable pid file) ⇒ a
#       DISTINCT `UNKNOWN` report and exit 3. Defaulting to "the daemon is
#       gone" is what manufactured the permanent false positive; an alerting
#       component must say "I cannot tell" in its own words.
#   The pid file's PATH is also now derived by the same precedence the daemon
#   uses (`LOOM_PID_FILE` > the marker's `pid_file=` > machine/workspace >
#   `<loom dir>`), so the two ends can no longer disagree about which file
#   they mean.
#
# BOUNDED AUTO-REMEDIATION (#4232)
#   The watchdog was deliberately report-only until #4232: on 2026-07-28 a
#   `loom-daemon restart` was ack'd (the running daemon exited 0, honoring its
#   #4054/#4077 restart contract) but launchd never relaunched it — the
#   watchdog could describe that outage but not fix it, which matters once
#   #4055's unattended self-update path can hit the same race with no operator
#   watching. This job now auto-runs `launchctl kickstart <label>` (PLAIN,
#   NEVER `-k`) for EXACTLY ONE divergence signature: the launchd job is
#   LOADED (launchctl still knows about it) + NOT running + its last exit
#   status was 0. That signature can ONLY arise from a restart-primitive exit
#   that launchd failed to honor — an operator SIGTERM stop exits 143/130
#   (loom-daemon-stop.sh), a crash exits non-zero, and a booted-out/never-
#   loaded job fails `launchctl print` outright. Every other divergence stays
#   report-only, exactly as before: no crash-loop revival, no reviving a
#   deliberate stop.
#
#   #4862 adds the systemd --user mirror of this gate: a clean main-process
#   exit (ExecMainCode=exited, ExecMainStatus=0) with the unit LOADED but not
#   running auto-runs 'systemctl --user reset-failed <unit> && systemctl
#   --user start <unit>' — the systemd analog of 'launchctl kickstart', for
#   the exact "Restart=on-success didn't fire" incident #4862 reported (and
#   the render_systemd_unit KillMode=mixed fix in loom-daemon-start.sh
#   addresses at the source). Same narrow construction: an operator stop, a
#   genuine crash, or a never-installed unit cannot produce this signature.
#
# EXIT CODES (a StartInterval/OnUnitActiveSec job's exit code does not affect
# relaunch — these exist for testability and for a human running it by hand):
#   0  no divergence — daemon healthy, OR no daemon expected AND none running
#      (marker absent + nothing alive), OR the #4232/#4862 bounded
#      auto-remediation (see above) successfully relaunched it via
#      'launchctl kickstart' / 'systemctl --user start'
#   1  DIVERGENCE / state mismatch reported — a daemon is expected but is not
#      running (and either the #4232 remediation gate did not apply, or it fired
#      but the daemon is still not confirmed running), or is running but its
#      heartbeat is stale (possibly wedged), OR (#4398) it is running with a
#      fresh heartbeat but its bounded IPC round-trip failed, OR (#4331) a daemon
#      IS running while the marker is ABSENT (crash protection disarmed — a WARN
#      state mismatch), OR (#5118) the socket answers while the supervisor says
#      its job is down (an UNSUPERVISED daemon — serving work, but not crash
#      protected)
#   2  usage error
#   3  (#5118) LIVENESS UNDETERMINED — deliberately NOT exit 1: no out-of-band
#      signal found a live daemon AND the in-band socket probe could not run (no
#      resolvable loom-daemon binary, the probe is disabled, or the CLI itself
#      never returned), so this tick has NO EVIDENCE either way. Reported as
#      UNKNOWN, never as "the daemon is down".
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh            Check once, report on divergence
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh --verbose  Also log the healthy/idle no-op cases
#   ./.loom/scripts/cli/loom-daemon-watchdog.sh --help
#
# Environment:
#   LOOM_AUTONOMY_MARKER           Path to the intent marker (default: derived
#                                  from LOOM_SOCKET_PATH's dir, else ~/.loom/autonomy-desired)
#   LOOM_WATCHDOG_LOG              Report log path (default: <loom dir>/logs/daemon-watchdog.log)
#   LOOM_DAEMON_HEARTBEAT_STALE_SECS  Staleness threshold in seconds (default:
#                                  max(5 × heartbeat cadence, 300))
#   LOOM_SOCKET_PATH              Override the daemon socket (its dir is the loom dir)
#   LOOM_PID_FILE                 #5118: the pid file path, honored END-TO-END —
#                                 the same precedence tier 1 the daemon itself
#                                 uses (daemon_pidfile.rs) and the value
#                                 loom-daemon-start.sh exports into every plist
#                                 / systemd unit it renders. Wins over the
#                                 marker's `pid_file=` field. The pid file is
#                                 only ever a CORROBORATING hint here: the
#                                 socket round-trip is authoritative.
#   LOOM_LAUNCHD_LABEL            macOS: the DAEMON label to probe (default com.rjwalters.loom-daemon)
#   LOOM_LAUNCHD_DOMAIN          macOS: pin the launchd domain (gui/<uid> or user/<uid>);
#                                else auto-resolved gui→user (#4130), matching the start
#   LOOM_DAEMON_LAUNCHD          0/false/no: treat as a non-launchd (nohup) daemon; check the pid file only
#   LOOM_SYSTEMD_UNIT             #4862: the systemd --user unit to probe (default
#                                loom-daemon.service, else the marker's systemd_unit)
#   LOOM_WATCHDOG_SYSTEMD_PROBE   #4862: 1/true/yes: opt in to the systemd --user
#                                probe (default: per the marker's use_systemd
#                                field, else OFF -- `systemctl` merely being on
#                                PATH is not proof of a systemd-managed daemon).
#                                0/false/no forces it off regardless of the marker.
#   LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS  #4232/#4862: how many times to re-check
#                                for a live pid after the auto-kickstart / systemctl-start fallback
#                                (default 3).
#   LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL  #4232: seconds between re-checks
#                                (default 1; may be fractional).
#   LOOM_WATCHDOG_IPC_PROBE       #4398: 0/false/no disables the bounded in-band
#                                IPC probe entirely (pid + heartbeat checks only).
#   LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS  #4398: hard external budget for the
#                                probe (default 15). Must stay comfortably above
#                                the CLI's own 5s connect + 5s round-trip bound.
#   LOOM_WATCHDOG_IPC_PROBE_ARGS  #4398: the loom-daemon argv used as the probe
#                                (default 'quarantine list' — a pure IPC
#                                round-trip with no post-reply work).
#   LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD  #4398: consecutive failed probes
#                                before a hang is called CONFIRMED (default 3).
#   LOOM_WATCHDOG_IPC_PROBE_GRACE_SECS  #4398: post-relaunch socket-bind grace
#                                window; a younger process is not probed
#                                (default: LOOM_DAEMON_STARTUP_GRACE_SECS, else 90).
#   LOOM_WATCHDOG_IPC_PROBE_STATE  #4398: path to the consecutive-failure counter
#                                (default <loom dir>/.watchdog-probe-fail-count).
#   LOOM_FORCE_PORTABLE_TIMEOUT    #4398, renamed from the watchdog-local
#                                LOOM_WATCHDOG_FORCE_PORTABLE_TIMEOUT by #4832
#                                when bounded_run() moved to the shared
#                                lib/bounded-run.sh: 1 forces the built-in
#                                bounded runner instead of `timeout(1)` (test
#                                seam for the default macOS no-`timeout` shape).
#   LOOM_DAEMON_BIN               Explicit loom-daemon binary for the IPC probe
#                                (same resolution order as loom-daemon-start.sh).

set -uo pipefail

# ---------- output helpers ----------
if [[ -t 2 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; NC=''
fi

show_help() {
    awk 'NR>=2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

# Shared domain resolver (#4130): gui/<uid> ↦ user/<uid>, sourced verbatim so the
# watchdog probes the daemon in the same domain the start put it in.
_LOOM_LAUNCHD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh" ]]; then
    # shellcheck source=../lib/launchd-domain.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh"
fi
# bounded_run() (#4398, shared with loom-daemon-start.sh's print_calibrate_hint,
# de-duplicated from this script's own former inline copy by #4832) — the IPC
# probe's hard wall-clock budget. Unlike the start script's advisory hint, the
# probe below is NOT optional, so run_ipc_probe() checks explicitly for a
# missing `bounded_run` rather than letting an undefined-function call degrade
# silently.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/bounded-run.sh" ]]; then
    # shellcheck source=../lib/bounded-run.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/bounded-run.sh"
fi
# loom_locate_daemon_bin() (#4875, shared with loom-daemon-start.sh /
# loom-daemon-update.sh / loom-status.sh / `.loom/bin/loom health`) — includes
# the machine-level ~/.local/bin fallback so this watchdog (which runs from a
# systemd/launchd timer with a minimal, non-login environment) still finds a
# machine-level install even though $PATH never carries ~/.local/bin here.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/locate-daemon-bin.sh" ]]; then
    # shellcheck source=../lib/locate-daemon-bin.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/locate-daemon-bin.sh"
fi

VERBOSE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) show_help; exit 0 ;;
        --verbose|-v) VERBOSE=true; shift ;;
        *) echo "Unknown option '$1' (use --help)" >&2; exit 2 ;;
    esac
done

# ---------- path resolution (mirrors loom-daemon-start.sh / resolve_loom_dir) ----------
SOCKET_PATH="${LOOM_SOCKET_PATH:-$HOME/.loom/loom-daemon.sock}"
LOOM_DIR="$(dirname "$SOCKET_PATH")"
MARKER="${LOOM_AUTONOMY_MARKER:-$LOOM_DIR/autonomy-desired}"
WATCHDOG_LOG="${LOOM_WATCHDOG_LOG:-$LOOM_DIR/logs/daemon-watchdog.log}"

# ---------- IPC probe knobs (#4398) ----------
# The external budget must stay comfortably ABOVE the CLI's own worst case (5s
# connect + 5s round-trip, doubled by the single #4279 reconnect retry) so on a
# merely-slow-but-healthy daemon the CLI's own bound is always what fires first
# and this timeout is never the thing that manufactures a "hang".
PROBE_TIMEOUT_SECS="${LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS:-15}"
[[ "$PROBE_TIMEOUT_SECS" =~ ^[0-9]+$ ]] || PROBE_TIMEOUT_SECS=15
PROBE_ARGS="${LOOM_WATCHDOG_IPC_PROBE_ARGS:-quarantine list}"
PROBE_FAIL_THRESHOLD="${LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD:-3}"
[[ "$PROBE_FAIL_THRESHOLD" =~ ^[1-9][0-9]*$ ]] || PROBE_FAIL_THRESHOLD=3
# Mirrors daemon_install_state::DEFAULT_STARTUP_GRACE_SECS (90) and honors the
# same LOOM_DAEMON_STARTUP_GRACE_SECS override, so `loom-daemon status` and this
# watchdog can never disagree about when a young daemon is merely *starting*.
PROBE_GRACE_SECS="${LOOM_WATCHDOG_IPC_PROBE_GRACE_SECS:-${LOOM_DAEMON_STARTUP_GRACE_SECS:-90}}"
[[ "$PROBE_GRACE_SECS" =~ ^[0-9]+$ ]] || PROBE_GRACE_SECS=90
PROBE_STATE_FILE="${LOOM_WATCHDOG_IPC_PROBE_STATE:-$LOOM_DIR/.watchdog-probe-fail-count}"
# Set true once a probe divergence has been REPORTED on this tick, so the
# heartbeat section's otherwise-healthy exits still surface a non-zero code.
PROBE_DIVERGED=false

# Append a timestamped line to the watchdog log (best-effort) and echo to
# stderr, which launchd captures to the job's StandardErrorPath. This IS the
# report — the durable, operator-visible signal that a pull never surfaced.
report() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    mkdir -p "$(dirname "$WATCHDOG_LOG")" 2>/dev/null || true
    echo "$ts [$level] $msg" >> "$WATCHDOG_LOG" 2>/dev/null || true
    case "$level" in
        DIVERGENCE) echo -e "${RED}$ts [$level] $msg${NC}" >&2 ;;
        OK)         [[ "$VERBOSE" == "true" ]] && echo -e "${GREEN}$ts [$level] $msg${NC}" >&2 ;;
        *)          echo -e "${YELLOW}$ts [$level] $msg${NC}" >&2 ;;
    esac
}

# ---------- marker reader ----------
# Defined UP HERE (moved by #5118) rather than beside the marker-present path
# below: locate_daemon_bin() consults `marker_get repo_root`, and #5118's
# socket probe now runs on the marker-ABSENT path too, which executes before
# that path's own code. A function must exist at CALL time, so this has to be
# defined before section 1 runs.
marker_get() {
    local key="$1"
    # First matching `key=value` line; strip the key= prefix. Comments start '#'.
    [[ -f "$MARKER" ]] || return 0
    grep -E "^${key}=" "$MARKER" 2>/dev/null | head -n1 | cut -d= -f2-
}

# ---------- pid-file path resolution (#5118) ----------
# Mirrors the daemon's own `daemon_pidfile::resolve_pid_file_path_from`
# precedence EXACTLY so the two ends can never mean different files — the #5118
# path disagreement (this script read `<socket dir>/.daemon.pid`; the daemon
# wrote `<workspace>/.loom/.daemon.pid`) was possible only because each side
# derived its own path:
#   1. LOOM_PID_FILE            — explicit, exported by loom-daemon-start.sh and
#                                 baked into the rendered plist / systemd unit,
#                                 so every supervisor relaunch resolves it too.
#   2. the marker's `pid_file=` — the path the start script chose on THIS host
#                                 (i.e. what it exported as LOOM_PID_FILE).
#   3. LOOM_MACHINE_CHECKOUT    — machine mode keeps runtime state in the
#                                 machine-level loom dir.
#   4. LOOM_WORKSPACE / the marker's repo_root — repo mode: `<repo>/.loom`.
#   5. `<loom dir>/.daemon.pid` — the daemon's own final fallback.
# Whatever this returns is only ever a CORROBORATING hint; an absent or stale
# file must not by itself produce a divergence (that is the whole bug).
resolve_pid_file() { # [marker pid_file value]
    local from_marker="${1:-}"
    if [[ -n "${LOOM_PID_FILE:-}" ]]; then
        echo "$LOOM_PID_FILE"; return 0
    fi
    if [[ -n "$from_marker" ]]; then
        echo "$from_marker"; return 0
    fi
    if [[ -n "${LOOM_MACHINE_CHECKOUT:-}" ]]; then
        echo "$LOOM_DIR/.daemon.pid"; return 0
    fi
    local workspace="${LOOM_WORKSPACE:-$(marker_get repo_root)}"
    if [[ -n "$workspace" ]]; then
        echo "$workspace/.loom/.daemon.pid"; return 0
    fi
    echo "$LOOM_DIR/.daemon.pid"
}

# ---------- reality probe (shared) ----------
# Determine whether the expected daemon is actually alive. Reads the resolved
# USE_LAUNCHD / USE_SYSTEMD / LABEL / SYSTEMD_UNIT / PID_FILE and sets globals
# the callers branch on:
#   daemon_alive     true|false
#   liveness_detail  human-readable string (mirrored into status/log messages)
#   job_loaded       true|false — launchd job / systemd unit known but with no
#                    live pid (feeds the #4232 / #4862 bounded auto-remediation
#                    gates)
#   launchd_service  <domain>/<label> for the launchd path (else empty)
#   systemd_service  <unit> for the systemd path (else empty)
#   liveness_source  launchd|systemd|pidfile — WHICH out-of-band signal was
#                    consulted (#5118: a supervisor's "job is down" is real
#                    evidence; a missing pid file is not, so the two must be
#                    told apart by the callers)
#   pidfile_evidence alive|dead|absent — only meaningful on the pidfile path
#                    (#5118: `dead` — a file naming a non-live pid — is positive
#                    evidence; `absent` is no evidence at all)
# Factored out (#4331) so the no-marker state-mismatch check below and the
# marker-present path below run the IDENTICAL liveness logic — they can never
# diverge on what "alive" means.
detect_daemon_liveness() {
    daemon_alive=false
    liveness_detail=""
    job_loaded=false
    launchd_service=""
    systemd_service=""
    live_pid=""
    liveness_source=pidfile
    pidfile_evidence=absent
    if [[ "$USE_LAUNCHD" == "true" ]] && command -v launchctl >/dev/null 2>&1; then
        # Resolve the domain (gui/<uid> ↦ user/<uid>, #4130) the same way the
        # start did, so a headless daemon in user/<uid> is probed correctly.
        liveness_source=launchd
        launchd_service="$(resolve_launchd_domain)/${LABEL}"
        launchd_print_output="$(launchctl print "$launchd_service" 2>/dev/null)"
        launchd_print_rc=$?
        launchd_pid="$(printf '%s\n' "$launchd_print_output" | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}')"
        if [[ -n "$launchd_pid" ]] && kill -0 "$launchd_pid" 2>/dev/null; then
            daemon_alive=true
            liveness_detail="launchd job $launchd_service alive (pid $launchd_pid)"
            live_pid="$launchd_pid"
        elif [[ "$launchd_print_rc" -eq 0 ]]; then
            job_loaded=true
            liveness_detail="launchd job $launchd_service is LOADED but NOT running (no live pid)"
        else
            liveness_detail="launchd job $launchd_service is not loaded/alive"
        fi
    elif [[ "$USE_SYSTEMD" == "true" ]] && command -v systemctl >/dev/null 2>&1; then
        # systemd --user path (#4862): mirrors the launchd branch above so the
        # #4232-style auto-remediation gate below has an equivalent signal on
        # Linux. `show -p X --value` against an unknown unit answers cleanly
        # (LoadState=not-found) rather than erroring, so no separate rc check
        # is needed the way launchd's print exit code is used above.
        liveness_source=systemd
        systemd_service="$SYSTEMD_UNIT"
        systemd_main_pid="$(systemctl --user show -p MainPID --value "$systemd_service" 2>/dev/null)"
        if [[ -n "$systemd_main_pid" && "$systemd_main_pid" != "0" ]] && kill -0 "$systemd_main_pid" 2>/dev/null; then
            daemon_alive=true
            liveness_detail="systemd unit $systemd_service alive (pid $systemd_main_pid)"
            live_pid="$systemd_main_pid"
        else
            systemd_load_state="$(systemctl --user show -p LoadState --value "$systemd_service" 2>/dev/null)"
            if [[ "$systemd_load_state" == "loaded" ]]; then
                job_loaded=true
                liveness_detail="systemd unit $systemd_service is LOADED but NOT running (no live MainPID)"
            else
                liveness_detail="systemd unit $systemd_service is not loaded/alive"
            fi
        fi
    else
        # Non-launchd, non-systemd (nohup) path: the pid file is the only
        # OUT-OF-BAND signal — and, since #5118, explicitly the WEAKEST one.
        # A live pid here is still accepted as liveness (cheap, and correct
        # whenever the file is fresh), but neither of the negative outcomes
        # below may end the check: the caller asks the socket before reporting.
        liveness_source=pidfile
        if [[ -n "$PID_FILE" && -f "$PID_FILE" ]]; then
            pid="$(cat "$PID_FILE" 2>/dev/null || true)"
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                daemon_alive=true
                pidfile_evidence=alive
                liveness_detail="pid $pid (from $PID_FILE) alive"
                live_pid="$pid"
            else
                pidfile_evidence=dead
                liveness_detail="pid file $PID_FILE present but pid not alive"
            fi
        else
            pidfile_evidence=absent
            liveness_detail="no live pid file at ${PID_FILE:-<none>}"
        fi
    fi
}

# Parse a `ps -o etime=` duration ([[dd-]hh:]mm:ss) into whole seconds —
# mirrors the Rust probe's `parse_etime` exactly (`daemon_install_state.rs`,
# #4368) so the two never disagree on a process's age. Any unexpected shape
# or non-numeric field fails (exit 1, nothing echoed) — the caller treats an
# unparseable age as *unknown* and makes no prior-boot claim, never a false
# one either way.
parse_etime_secs() {
    local raw days rest hours minutes seconds parts
    raw="$(printf '%s' "${1:-}" | tr -d '[:space:]')"
    [[ -z "$raw" ]] && return 1
    days=0
    rest="$raw"
    if [[ "$raw" == *-* ]]; then
        days="${raw%%-*}"
        rest="${raw#*-}"
        [[ "$days" =~ ^[0-9]+$ ]] || return 1
    fi
    IFS=':' read -r -a parts <<< "$rest"
    case "${#parts[@]}" in
        1) hours=0; minutes=0; seconds="${parts[0]}" ;;
        2) hours=0; minutes="${parts[0]}"; seconds="${parts[1]}" ;;
        3) hours="${parts[0]}"; minutes="${parts[1]}"; seconds="${parts[2]}" ;;
        *) return 1 ;;
    esac
    [[ "$hours" =~ ^[0-9]+$ && "$minutes" =~ ^[0-9]+$ && "$seconds" =~ ^[0-9]+$ ]] || return 1
    echo $(( days * 86400 + hours * 3600 + minutes * 60 + seconds ))
}

# Live process age in seconds via `ps -o etime= -p <pid>`, degrading to
# nothing (no output, non-zero return) on any failure — the caller must never
# turn an unknown age into a false prior-boot claim (#4368).
process_age_secs() {
    local pid="$1" etime
    [[ -n "$pid" ]] || return 1
    etime="$(ps -o etime= -p "$pid" 2>/dev/null)" || return 1
    parse_etime_secs "$etime"
}

# ---------- bounded in-band IPC probe (#4398) ----------

# Resolve the loom-daemon binary the probe should invoke, via the shared
# loom_locate_daemon_bin() (lib/locate-daemon-bin.sh, sourced above) so this
# watchdog, loom-daemon-start.sh, loom-daemon-update.sh, loom-status.sh and
# `.loom/bin/loom health` can never disagree about which binary is "the"
# daemon CLI. Preserves this function's original contract (thin wrapper):
# echoes nothing and returns 1 when nothing is resolvable — the caller must
# then SKIP the probe, never report a divergence: a watchdog that pages
# because its own optional helper is missing is worse than one that quietly
# keeps doing the other two checks.
locate_daemon_bin() {
    local root bin
    root="$(marker_get repo_root 2>/dev/null)"
    bin="$(loom_locate_daemon_bin "$root")"
    [[ -n "$bin" ]] || return 1
    echo "$bin"
}

# bounded_run() — a HARD wall-clock budget around a command, returning 124 on
# timeout exactly like GNU `timeout` does. Sourced from the shared
# lib/bounded-run.sh (#4807 extracted this watchdog's own inline copy so
# loom-daemon-start.sh's print_calibrate_hint() could reuse it, #4799); see
# that file for the full implementation notes (the `-k 2` KILL escalation,
# the portable no-`timeout(1)` fallback for macOS, the 143/137 -> 124
# normalization). The lib is sourced just below (near launchd-domain.sh); if
# sourcing failed for any reason, `bounded_run` is simply undefined and
# run_ipc_probe() below detects that explicitly and skips the probe with a
# clear diagnostic — never a raw `command not found` on a scheduled tick
# (#4832).

# Read the consecutive-failure tally for <pid> from the state file. The tally is
# KEYED TO THE PID: a relaunched daemon is a different process and must start
# its own streak, never inherit the wedged predecessor's (which would escalate a
# healthy fresh daemon on its first failed tick).
probe_fail_count_for_pid() { # <pid>
    local pid="$1" saved_pid saved_count
    [[ -r "$PROBE_STATE_FILE" ]] || { echo 0; return 0; }
    read -r saved_pid saved_count < "$PROBE_STATE_FILE" 2>/dev/null || { echo 0; return 0; }
    [[ "$saved_pid" == "$pid" && "$saved_count" =~ ^[0-9]+$ ]] || { echo 0; return 0; }
    echo "$saved_count"
}

probe_fail_count_write() { # <pid> <count>
    mkdir -p "$(dirname "$PROBE_STATE_FILE")" 2>/dev/null
    printf '%s %s\n' "$1" "$2" > "$PROBE_STATE_FILE" 2>/dev/null || true
}

probe_fail_count_clear() {
    rm -f "$PROBE_STATE_FILE" 2>/dev/null || true
}

# Run the bounded probe command ONCE. Extracted from run_ipc_probe() by #5118 so
# the post-liveness hang probe and the new pre-report socket-liveness probe
# invoke the daemon CLI through the IDENTICAL code path (same binary
# resolution, same argv, same hard budget) and can never disagree about what
# "the daemon answered" means.
#
# Sets PROBE_RUN_RC / PROBE_RUN_OUTPUT / PROBE_RUN_BIN and returns 0 when the
# command actually ran; returns 1 with PROBE_RUN_SKIP_REASON set when it could
# NOT be run at all (disabled, no bounded_run, no binary, empty argv). The
# distinction matters: "ran and failed" is evidence, "could not run" is not.
invoke_probe_command() {
    PROBE_RUN_RC=0
    PROBE_RUN_OUTPUT=""
    PROBE_RUN_BIN=""
    PROBE_RUN_SKIP_REASON=""

    if [[ "${LOOM_WATCHDOG_IPC_PROBE:-}" =~ ^(0|false|no)$ ]]; then
        PROBE_RUN_SKIP_REASON="IPC probe disabled via LOOM_WATCHDOG_IPC_PROBE"
        return 1
    fi

    # #4832: bounded_run is defined by sourcing lib/bounded-run.sh above. A
    # missing/unreadable lib file leaves it undefined -- without this explicit
    # check, the invocation below would fail as a raw shell "command not
    # found" (rc 127) on every scheduled tick instead of a diagnosed skip.
    if ! command -v bounded_run >/dev/null 2>&1; then
        PROBE_RUN_SKIP_REASON="bounded_run is undefined -- lib/bounded-run.sh failed to source (missing or unreadable)"
        return 1
    fi

    local bin
    bin="$(locate_daemon_bin)" || bin=""
    if [[ -z "$bin" ]]; then
        PROBE_RUN_SKIP_REASON="no loom-daemon binary resolvable (LOOM_DAEMON_BIN unset, not on PATH, no in-repo build)"
        return 1
    fi
    PROBE_RUN_BIN="$bin"

    local -a probe_argv
    # shellcheck disable=SC2206  # deliberate word-splitting of the argv override
    read -r -a probe_argv <<< "$PROBE_ARGS"
    if (( ${#probe_argv[@]} == 0 )); then
        PROBE_RUN_SKIP_REASON="LOOM_WATCHDOG_IPC_PROBE_ARGS is empty — nothing to probe with"
        return 1
    fi

    local out rc=0
    out="$(mktemp "${TMPDIR:-/tmp}/loom-watchdog-probe.XXXXXX" 2>/dev/null)" || out=""
    if [[ -n "$out" ]]; then
        LOOM_SOCKET_PATH="$SOCKET_PATH" bounded_run "$PROBE_TIMEOUT_SECS" \
            "$bin" "${probe_argv[@]}" > "$out" 2>&1
        rc=$?
        PROBE_RUN_OUTPUT="$(cat "$out" 2>/dev/null)"
        rm -f "$out" 2>/dev/null
    else
        LOOM_SOCKET_PATH="$SOCKET_PATH" bounded_run "$PROBE_TIMEOUT_SECS" \
            "$bin" "${probe_argv[@]}" >/dev/null 2>&1
        rc=$?
    fi
    PROBE_RUN_RC=$rc
    return 0
}

# ---------- authoritative socket-liveness probe (#5118) ----------
# Ask the daemon's own socket whether ANYTHING is serving there, and classify
# the answer for the liveness decision (NOT the hang decision — that is
# run_ipc_probe's job, and it only runs once liveness is established).
#
# Sets:
#   socket_verdict  answered | unreachable | indeterminate
#   socket_detail   human-readable reason, mirrored into every report
#
# The three verdicts are deliberately asymmetric about what counts as evidence:
#   answered      a round-trip completed, OR the daemon replied with an
#                 application-level error (which PROVES the socket is served),
#                 OR the CLI itself says the daemon is alive-but-starting.
#   unreachable   positive evidence of absence: the CLI could not reach the
#                 socket (no listener / connect refused / connect timed out).
#   indeterminate everything else — the probe could not be run, the CLI itself
#                 never returned, or the daemon looks alive-but-wedged. The
#                 caller must report UNKNOWN, never "the daemon is down".
probe_socket_liveness() {
    socket_verdict=indeterminate
    socket_detail=""

    if ! invoke_probe_command; then
        socket_detail="could not ask the socket: ${PROBE_RUN_SKIP_REASON}"
        return 0
    fi

    local label
    label="$(basename "$PROBE_RUN_BIN") ${PROBE_ARGS}"
    case "$PROBE_RUN_RC" in
        0)
            socket_verdict=answered
            socket_detail="'${label}' round-tripped over ${SOCKET_PATH} within ${PROBE_TIMEOUT_SECS}s"
            return 0
            ;;
        124)
            socket_detail="'${label}' did NOT return within the ${PROBE_TIMEOUT_SECS}s probe budget — the CLI itself is wedged, so this tick proves nothing about the daemon either way"
            return 0
            ;;
        2|126|127)
            socket_detail="probe command '${label}' is unsupported by this binary (exit ${PROBE_RUN_RC}) — no in-band liveness signal available"
            return 0
            ;;
    esac

    if printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'alive-starting|socket has not bound'; then
        socket_verdict=answered
        socket_detail="the daemon is alive and STARTING (its socket is not bound yet) — alive, not gone"
    elif printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'alive-but-unresponsive'; then
        socket_detail="the CLI reports the daemon process as alive-but-unresponsive — alive-vs-gone is UNDETERMINED from this tick"
    elif printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'could not reach loom-daemon|connect timed out|connect failed|connection refused|no such file'; then
        socket_verdict=unreachable
        socket_detail="'${label}' could not reach anything at ${SOCKET_PATH} (exit ${PROBE_RUN_RC}): $(printf '%s' "$PROBE_RUN_OUTPUT" | head -n1)"
    elif printf '%s' "$PROBE_RUN_OUTPUT" | grep -qiE 'round-trip timed out|closed the connection without responding'; then
        socket_detail="'${label}' connected but the round-trip did not complete (exit ${PROBE_RUN_RC}) — a wedge, not a proven absence: $(printf '%s' "$PROBE_RUN_OUTPUT" | head -n1)"
    else
        socket_verdict=answered
        socket_detail="'${label}' exited ${PROBE_RUN_RC} with an application-level error (the daemon ANSWERED, so the socket is served): $(printf '%s' "$PROBE_RUN_OUTPUT" | head -n1)"
    fi
}

# Perform the bounded IPC round-trip and classify the outcome. Sets:
#   probe_verdict  healthy | unresponsive | skipped
#   probe_detail   human-readable reason (mirrored into the report)
# NEVER exits, never blocks longer than PROBE_TIMEOUT_SECS (+ the 2s KILL grace
# of the portable fallback).
run_ipc_probe() { # <live_pid> <proc_age_or_empty>
    probe_verdict=skipped
    probe_detail=""
    local pid="$1" age="$2"

    # Post-relaunch socket-bind window (#4213/#4331): a live pid whose socket is
    # not bound yet is STARTING, not wedged. Skip outright so it cannot count
    # toward a confirmed hang. An UNPARSEABLE age does not skip — `ps` failing
    # for a live pid is not a real deployment state, and silently disabling the
    # probe on it would reintroduce the blind spot; the N-consecutive debounce
    # still spans far more than any bind window. Checked BEFORE the invocation
    # so a young daemon is never even asked.
    if [[ "$age" =~ ^[0-9]+$ ]] && (( age < PROBE_GRACE_SECS )); then
        probe_detail="process is only ${age}s old (< ${PROBE_GRACE_SECS}s startup grace) — socket may not be bound yet"
        return 0
    fi

    # #5118: the invocation itself (probe-disabled / missing bounded_run /
    # unresolvable binary / empty argv checks included) now lives in the shared
    # invoke_probe_command(). Its skip reasons are the SAME "could not run at
    # all" set this function has always degraded on, so they still map to
    # `skipped`, never to a divergence.
    if ! invoke_probe_command; then
        probe_detail="$PROBE_RUN_SKIP_REASON"
        return 0
    fi

    local bin="$PROBE_RUN_BIN" rc="$PROBE_RUN_RC" probe_output="$PROBE_RUN_OUTPUT"

    case "$rc" in
        0)
            probe_verdict=healthy
            probe_detail="'$(basename "$bin") ${PROBE_ARGS}' round-tripped over ${SOCKET_PATH} within ${PROBE_TIMEOUT_SECS}s"
            return 0
            ;;
        124)
            # The CLI never returned inside our hard budget even though it bounds
            # its own connect/round-trip at 5s each — i.e. the binary itself is
            # not answering (the #4381 hung-stub shape).
            probe_verdict=unresponsive
            probe_detail="'$(basename "$bin") ${PROBE_ARGS}' did NOT return within the ${PROBE_TIMEOUT_SECS}s probe budget (the CLI's own 5s connect + 5s round-trip bounds did not even fire)"
            return 0
            ;;
        2|126|127)
            # clap usage error / not executable: this build's CLI does not
            # understand the probe, or the binary cannot be run at all. Skip.
            probe_verdict=skipped
            probe_detail="probe command '$(basename "$bin") ${PROBE_ARGS}' is unsupported by this binary (exit ${rc}) — skipping the IPC probe"
            return 0
            ;;
    esac

    # Any other non-zero exit: only call it UNRESPONSIVE on positive evidence
    # that the round-trip itself failed. A daemon that ANSWERED with an
    # application-level error proves IPC works, so it must degrade to `skipped`.
    if printf '%s' "$probe_output" | grep -qiE 'alive-starting|socket has not bound'; then
        probe_verdict=skipped
        probe_detail="probe reports the daemon is still STARTING (socket not bound yet) — not counted as a hang"
    elif printf '%s' "$probe_output" | grep -qiE 'could not reach loom-daemon|round-trip timed out|connect timed out|connect failed|closed the connection without responding|alive-but-unresponsive'; then
        probe_verdict=unresponsive
        probe_detail="'$(basename "$bin") ${PROBE_ARGS}' failed the IPC round-trip (exit ${rc}): $(printf '%s' "$probe_output" | head -n1)"
    else
        probe_verdict=skipped
        probe_detail="probe exited ${rc} without an IPC-failure signature (the daemon answered) — not counted as a hang: $(printf '%s' "$probe_output" | head -n1)"
    fi
}

# ---------- 1. intent: is a daemon expected at all? ----------
if [[ ! -f "$MARKER" ]]; then
    # A missing marker is SUPPOSED to mean "deliberately stopped (or never
    # started) — nothing to check". But the marker can go absent while a
    # supervised daemon is very much alive: an out-of-band delete, a failed
    # marker write, or a daemon rolled ONLY via `loom-daemon restart` / the
    # self-update loop (neither re-writes the marker — #4331). In that state the
    # daemon runs with crash protection DISARMED, and a bare `[OK] nothing to
    # check` hides exactly the gap the watchdog exists to surface. So before
    # staying quiet, cheaply probe reality with env-derived defaults.
    USE_LAUNCHD=true
    if [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]]; then
        USE_LAUNCHD=false
    fi
    [[ "$(uname -s)" == "Darwin" ]] || USE_LAUNCHD=false
    LABEL="${LOOM_LAUNCHD_LABEL:-com.rjwalters.loom-daemon}"
    # #4862: with no marker to read use_systemd/systemd_unit from, there is no
    # reliable signal that THIS host's daemon is systemd-managed rather than a
    # bystander user session with `systemctl` merely on PATH (every dev/test
    # host in this suite has that) -- so, unlike USE_LAUNCHD above (which is
    # genuinely platform-derived), the systemd probe stays OFF here unless
    # explicitly requested via LOOM_WATCHDOG_SYSTEMD_PROBE=1. The marker-present path
    # below (the common case — a daemon just started) gets it from the
    # use_systemd field loom-daemon-start.sh's systemd branch now writes.
    USE_SYSTEMD=false
    if [[ "$USE_LAUNCHD" != "true" ]] && [[ "$(uname -s)" != "Darwin" ]] \
        && [[ "${LOOM_WATCHDOG_SYSTEMD_PROBE:-}" =~ ^(1|true|yes)$ ]] && command -v systemctl >/dev/null 2>&1; then
        USE_SYSTEMD=true
    fi
    SYSTEMD_UNIT="${LOOM_SYSTEMD_UNIT:-loom-daemon.service}"
    # #5118: derived by the SAME precedence the daemon uses, not blindly from
    # the socket's directory (which is what made this file unfindable on a
    # workspace-rooted install).
    PID_FILE="$(resolve_pid_file "")"
    detect_daemon_liveness
    if [[ "$daemon_alive" != "true" ]]; then
        # #5118: the out-of-band signals found nothing — which, before this fix,
        # was accepted as "nothing is running" on the strength of a pid file
        # alone. Ask the socket before concluding: an unmarked-but-LIVE daemon
        # is exactly the #4331 state this section exists to surface, and it must
        # not be missed just because its pid file is absent or stale.
        probe_socket_liveness
        if [[ "$socket_verdict" == "answered" ]]; then
            daemon_alive=true
            liveness_detail="a daemon ANSWERS on ${SOCKET_PATH} (${socket_detail}); the out-of-band signal disagreed: ${liveness_detail}"
        fi
    fi
    if [[ "$daemon_alive" == "true" ]]; then
        report WARN \
            "STATE MISMATCH: no autonomy-desired marker at $MARKER, but a daemon IS running (${liveness_detail}). Crash protection is DISARMED — if this daemon dies the watchdog will NOT revive it. Heal it by restarting the daemon (it self-heals the marker at startup, #4331) or re-running ./.loom/scripts/cli/loom-daemon-start.sh; if the daemon should NOT be running, stop it with ./.loom/scripts/cli/loom-daemon-stop.sh."
        exit 1
    fi
    # Nothing alive ⇒ the load-bearing quiet case: a deliberate stop (which also
    # boots out the daemon job, so nothing is found here) must never page.
    # Preserve the silent OK exactly as before.
    report OK "no autonomy-desired marker at $MARKER — no daemon expected; nothing to check."
    exit 0
fi

# ---------- parse the marker (key=value; ignore comments/blanks) ----------
# marker_get() itself is defined near the top (#5118) so the marker-ABSENT path
# above can use the same reader.
HEARTBEAT_FILE="$(marker_get heartbeat_file)"
HEARTBEAT_INTERVAL_SECS="$(marker_get heartbeat_interval_secs)"
MARKER_USE_LAUNCHD="$(marker_get use_launchd)"
MARKER_LABEL="$(marker_get launchd_label)"
MARKER_USE_SYSTEMD="$(marker_get use_systemd)"
MARKER_SYSTEMD_UNIT="$(marker_get systemd_unit)"
# #5118: LOOM_PID_FILE (the value loom-daemon-start.sh exports and the daemon
# honors as its own tier 1) now wins over the marker's recorded path, and an
# absent field falls through the daemon's remaining tiers instead of resolving
# to the empty string. Before this the two ends could — and on both fleet hosts
# did — mean different files.
PID_FILE="$(resolve_pid_file "$(marker_get pid_file)")"

# Fallbacks when the marker predates a field or the value is empty.
[[ -z "$HEARTBEAT_FILE" ]] && HEARTBEAT_FILE="$LOOM_DIR/daemon.heartbeat"
[[ "$HEARTBEAT_INTERVAL_SECS" =~ ^[0-9]+$ ]] || HEARTBEAT_INTERVAL_SECS=60

# Env overrides win over the marker (a stop/start under a different label should
# be probed with the current env, not a stale marker value).
USE_LAUNCHD="${MARKER_USE_LAUNCHD:-true}"
if [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]]; then
    USE_LAUNCHD=false
fi
[[ "$(uname -s)" == "Darwin" ]] || USE_LAUNCHD=false
LABEL="${LOOM_LAUNCHD_LABEL:-${MARKER_LABEL:-com.rjwalters.loom-daemon}}"

# #4862: use_systemd/systemd_unit are new marker fields, written by
# loom-daemon-start.sh's systemd branch (a marker from before this fix, or
# from the launchd/nohup branches, has no use_systemd=true line). A blank
# MARKER_USE_SYSTEMD stays OFF by default here -- same rationale as section 1
# above: `systemctl` merely being on PATH is not proof THIS daemon is
# systemd-managed, so no platform auto-detect. LOOM_WATCHDOG_SYSTEMD_PROBE=1 opts in
# explicitly for a pre-#4862 marker that has not been rewritten yet.
#
# Deliberately NO platform clobber here. An earlier revision ended this block
# with `[[ "$(uname -s)" == "Darwin" ]] && USE_SYSTEMD=false`, which ran after
# the opt-in and silently overrode both the marker and the documented
# LOOM_WATCHDOG_SYSTEMD_PROBE=1 escape hatch -- contradicting the comment above
# and making the whole #4862 systemd remediation path unreachable (and
# untestable) on a Darwin host.
#
# It was also redundant for the case it appeared to protect: the marker is the
# authority, and only loom-daemon-start.sh's systemd branch writes
# `use_systemd=true`. The launchd branch calls `write_intent_marker "true"
# "$LAUNCHD_LABEL"` with no third argument, so a real Darwin host's marker
# carries `use_systemd=false` and this resolves to false on its own.
USE_SYSTEMD="${MARKER_USE_SYSTEMD:-false}"
if [[ "${LOOM_WATCHDOG_SYSTEMD_PROBE:-}" =~ ^(1|true|yes)$ ]]; then
    USE_SYSTEMD=true
elif [[ "${LOOM_WATCHDOG_SYSTEMD_PROBE:-}" =~ ^(0|false|no)$ ]]; then
    USE_SYSTEMD=false
fi
SYSTEMD_UNIT="${LOOM_SYSTEMD_UNIT:-${MARKER_SYSTEMD_UNIT:-loom-daemon.service}}"

# ---------- 2. reality: is the expected daemon actually alive? ----------
# Shared probe (#4331): sets daemon_alive / liveness_detail / job_loaded /
# launchd_service from the resolved USE_LAUNCHD / LABEL / PID_FILE. job_loaded /
# launchd_service feed the #4232 bounded auto-remediation gate below:
# job_loaded=true means `launchctl print` succeeded (the job IS in launchd's
# table) even though no live pid was found — distinct from "not loaded at all"
# (a booted-out job, or a non-launchd host), which stays report-only no matter
# what.
detect_daemon_liveness

# ---------- 2b. authoritative in-band cross-check (#5118) ----------
# The probe above is entirely OUT-OF-BAND. When it does not find a live daemon,
# that is NOT yet a finding: on both fleet hosts the pid-file branch reported
# "no live pid file" every five minutes for two days while a healthy daemon was
# serving work on the socket the whole time. So before ANY report, ask the
# socket — the same signal `loom-daemon health` already treats as authoritative,
# with the pid file demoted to a corroborating hint.
#
# SOCKET_ONLY_LIVENESS records that liveness came from the socket alone (no live
# pid to age-check or key a hang streak to), which the sections below honor.
SOCKET_ONLY_LIVENESS=false
socket_verdict=""
socket_detail=""
if [[ "$daemon_alive" != "true" ]]; then
    probe_socket_liveness
    case "$socket_verdict" in
        answered)
            if [[ "$liveness_source" == "pidfile" ]]; then
                # No supervisor was consulted, so nothing contradicts the
                # socket: the daemon is HEALTHY and the pid file was simply not
                # a usable signal. This is the false-alarm case #5118 fixes —
                # it must be an OK, never a page.
                daemon_alive=true
                SOCKET_ONLY_LIVENESS=true
                liveness_detail="daemon ANSWERS on ${SOCKET_PATH} (${socket_detail}) — authoritative; the pid-file hint was unusable (${liveness_detail}), which is NOT evidence of an outage (#5118)"
                report OK "daemon healthy via the in-band socket round-trip: ${liveness_detail}."
            else
                # A supervisor DOES claim its job is down while something is
                # serving the socket. That is a real anomaly (an unsupervised
                # daemon: still dispatching, but with no crash protection) — but
                # it is NOT "autonomous dispatch has stopped", and auto-
                # remediation must NOT fire: kickstarting a second daemon at a
                # socket a live one already owns can only produce a refusal.
                report WARN \
                    "STATE MISMATCH: ${liveness_detail}, yet a daemon ANSWERS on ${SOCKET_PATH} (${socket_detail}). Dispatch is still running, so this is NOT an autonomy outage — but the daemon is UNSUPERVISED: nothing will relaunch it if it dies, and no auto-remediation is attempted here (relaunching into a served socket would only be refused by the singleton guard). Heal it by rolling the daemon through ./.loom/scripts/cli/loom-daemon-stop.sh && ./.loom/scripts/cli/loom-daemon-start.sh [flags] so the supervisor owns it again."
                exit 1
            fi
            ;;
        unreachable)
            # Positive evidence of absence — fall through to the existing
            # divergence + bounded auto-remediation path, now carrying BOTH
            # signals in its message.
            liveness_detail="${liveness_detail}; and the in-band probe confirms it: ${socket_detail}"
            ;;
        *)
            # Indeterminate. Report "the daemon is down" ONLY when some signal
            # actually says so: a supervisor that reports its job down, or a pid
            # file naming a pid that is not alive. With neither — the exact
            # shape that produced the permanent false positive — say UNKNOWN in
            # its own words and exit 3, distinct from a real outage (#5118).
            if [[ "$liveness_source" == "pidfile" && "$pidfile_evidence" == "absent" ]]; then
                report UNKNOWN \
                    "LIVENESS UNDETERMINED: a daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but this tick found NO evidence either way — ${liveness_detail}, and the in-band socket probe could not answer: ${socket_detail}. This is deliberately NOT reported as an outage (#5118): the pid file alone is too weak a signal to declare one. Restore the in-band probe (make a loom-daemon binary resolvable — LOOM_DAEMON_BIN / PATH / ~/.local/bin — and leave LOOM_WATCHDOG_IPC_PROBE enabled), then re-check with 'loom-daemon health'."
                exit 3
            fi
            liveness_detail="${liveness_detail}; the in-band probe could not corroborate: ${socket_detail}"
            ;;
    esac
fi

if [[ "$daemon_alive" != "true" ]]; then
    # ---------- bounded auto-remediation (#4232) ----------
    # THE PROBLEM: the restart primitive's contract (#4054/#4077) is "the
    # supervised daemon exits 0 -> KeepAlive:SuccessfulExit relaunches it". On
    # 2026-07-28 that contract's exit-0 half held but launchd's relaunch half
    # silently didn't, and this watchdog (a report-only detector) could only
    # describe the outage, not fix it — exactly the unattended-#4055-rollout
    # risk this narrow gate closes.
    #
    # THE GATE IS NARROW BY CONSTRUCTION: auto-`kickstart` fires ONLY for the
    # exact signature "job LOADED (launchctl still knows about it) + NOT
    # running + last exit status 0". An operator-initiated SIGTERM stop exits
    # 143/130 (loom-daemon-stop.sh); a genuine crash exits non-zero; a booted-
    # out/never-loaded job fails `launchctl print` outright (job_loaded=false).
    # NONE of those can produce "loaded, down, exit 0" — only a restart-
    # primitive exit that launchd failed to honor can. So every OTHER
    # divergence (stop, crash, bootout) falls through to the report-only path
    # below unchanged: no crash-loop revival, no reviving a deliberate stop.
    if [[ "$job_loaded" == "true" && -n "$launchd_service" ]]; then
        last_exit_status="$(launchctl print "$launchd_service" 2>/dev/null \
            | grep -oE 'last exit (code|status)[[:space:]]*=[[:space:]]*[-0-9]+' \
            | head -n1 | grep -oE '[-0-9]+$')"
        if [[ "$last_exit_status" == "0" ]]; then
            report DIVERGENCE \
                "A daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but is NOT running: ${liveness_detail}. Last exit status was 0 — the restart-primitive's own exit-0 contract (#4054/#4077) — which launchd failed to honor. Auto-remediating with 'launchctl kickstart ${launchd_service}' (PLAIN, never -k, so a daemon that is mid-relaunch is never killed) (#4232)."
            launchctl kickstart "$launchd_service" >/dev/null 2>&1
            # Brief, bounded re-check — this is a StartInterval job (re-run
            # every cadence regardless), so a failure here is NOT the last
            # chance; it just means this pass still reports divergence and the
            # next pass tries again.
            RECHECK_ATTEMPTS="${LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS:-3}"
            RECHECK_INTERVAL="${LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL:-1}"
            recheck_pid=""
            for _ in $(seq 1 "$RECHECK_ATTEMPTS"); do
                recheck_pid="$(launchctl print "$launchd_service" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}')"
                if [[ -n "$recheck_pid" ]] && kill -0 "$recheck_pid" 2>/dev/null; then
                    break
                fi
                recheck_pid=""
                sleep "$RECHECK_INTERVAL"
            done
            if [[ -n "$recheck_pid" ]]; then
                report OK "auto-remediation succeeded: 'launchctl kickstart' relaunched ${launchd_service} (new pid ${recheck_pid})."
                exit 0
            fi
            report DIVERGENCE \
                "Auto-remediation attempted ('launchctl kickstart ${launchd_service}') but the daemon is STILL not confirmed running. Escalate manually: launchctl print ${launchd_service}  (or ./.loom/scripts/cli/loom-daemon-start.sh [flags])."
            exit 1
        fi
    fi

    # ---------- bounded auto-remediation, systemd (#4862) ----------
    # The Linux mirror of the #4232 launchd gate above, closing the exact gap
    # #4862 reported: on a systemd host the watchdog previously only LOGGED a
    # clean-exit-turned-timeout divergence, never acted on it. Narrow by the
    # SAME construction as the launchd gate: fires ONLY for "unit LOADED
    # (systemd still knows about it) + NOT running + main process's own last
    # exit was a clean status 0" (ExecMainCode=exited, ExecMainStatus=0). An
    # operator stop (loom-daemon-stop.sh) disables the unit outright; a genuine
    # crash leaves a non-zero/signal ExecMainStatus; a never-installed unit
    # fails LoadState=loaded (job_loaded=false). None of those can produce
    # "loaded, down, ExecMainStatus=0" — only a restart-primitive exit that
    # Restart=on-success failed to honor can (e.g. a pre-#4862 unit still
    # missing KillMode=mixed). Every other divergence falls through unchanged.
    if [[ "$job_loaded" == "true" && -n "$systemd_service" ]] && command -v systemctl >/dev/null 2>&1; then
        exec_main_code="$(systemctl --user show -p ExecMainCode --value "$systemd_service" 2>/dev/null)"
        exec_main_status="$(systemctl --user show -p ExecMainStatus --value "$systemd_service" 2>/dev/null)"
        if [[ "$exec_main_code" == "exited" && "$exec_main_status" == "0" ]]; then
            report DIVERGENCE \
                "A daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but is NOT running: ${liveness_detail}. Main process's last exit was clean (status 0) — the restart-primitive's own exit-0 contract (#4054/#4077) — which Restart=on-success failed to honor (likely a unit-result reclassification, #4862). Auto-remediating with 'systemctl --user reset-failed ${systemd_service} && systemctl --user start ${systemd_service}'."
            systemctl --user reset-failed "$systemd_service" >/dev/null 2>&1 || true
            systemctl --user start "$systemd_service" >/dev/null 2>&1
            RECHECK_ATTEMPTS="${LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS:-3}"
            RECHECK_INTERVAL="${LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL:-1}"
            recheck_pid=""
            for _ in $(seq 1 "$RECHECK_ATTEMPTS"); do
                recheck_pid="$(systemctl --user show -p MainPID --value "$systemd_service" 2>/dev/null)"
                if [[ -n "$recheck_pid" && "$recheck_pid" != "0" ]] && kill -0 "$recheck_pid" 2>/dev/null; then
                    break
                fi
                recheck_pid=""
                sleep "$RECHECK_INTERVAL"
            done
            if [[ -n "$recheck_pid" ]]; then
                report OK "auto-remediation succeeded: 'systemctl --user start' relaunched ${systemd_service} (new pid ${recheck_pid})."
                exit 0
            fi
            report DIVERGENCE \
                "Auto-remediation attempted ('systemctl --user start ${systemd_service}') but the daemon is STILL not confirmed running. Escalate manually: systemctl --user status ${systemd_service}  (or ./.loom/scripts/cli/loom-daemon-start.sh [flags])."
            exit 1
        fi
    fi

    report DIVERGENCE \
        "A daemon is EXPECTED (autonomy-desired marker present, started $(marker_get started_at)) but is NOT running: ${liveness_detail}. Autonomous dispatch has stopped. Recover with: ./.loom/scripts/cli/loom-daemon-start.sh [flags]  (or 'loom-daemon status' to inspect)."
    exit 1
fi

# ---------- 3. reality: does the daemon still ANSWER over its socket? ----------
# The two checks above are both out-of-band; this is the only in-band one (#4398).
# See "HANG-AWARE IPC LIVENESS PROBE" in the header for the full rationale — in
# short: the heartbeat writer and the IPC accept loop are independent tokio
# tasks, so "pid alive + heartbeat fresh" can hold while every socket round-trip
# hangs and dispatch is effectively dead.
#
# The process age is computed ONCE here and reused by the #4368 prior-boot
# heartbeat check below, so both consumers see the same number.
proc_age="$(process_age_secs "$live_pid" 2>/dev/null)" || proc_age=""

if [[ "$SOCKET_ONLY_LIVENESS" == "true" ]]; then
    # #5118: liveness was ESTABLISHED by a socket round-trip a moment ago, so
    # re-running the same probe would only spend a second budget to learn the
    # same thing. Adopt that result directly (and clear any stale streak: a
    # successful round-trip is exactly what ends one).
    probe_verdict=healthy
    probe_detail="$socket_detail"
else
    run_ipc_probe "$live_pid" "$proc_age"
fi

case "$probe_verdict" in
    healthy)
        # A successful round-trip ends any prior failure streak outright: the
        # confirmed-hang tally must only ever count CONSECUTIVE failures.
        probe_fail_count_clear
        [[ "$VERBOSE" == "true" ]] && report OK "IPC probe OK: ${probe_detail}."
        ;;
    unresponsive)
        probe_fail_streak=$(( $(probe_fail_count_for_pid "$live_pid") + 1 ))
        probe_fail_count_write "$live_pid" "$probe_fail_streak"
        if (( probe_fail_streak >= PROBE_FAIL_THRESHOLD )); then
            # CONFIRMED, SUSTAINED hang. Deliberately report-only: there is no
            # provably-safe unattended remediation for a wedged-but-alive
            # process (the only real fix is killing it, which would equally kill
            # a daemon merely under heavy legitimate load), so unlike #4232's
            # narrow auto-kickstart gate this escalates to a maximally
            # actionable report and stops there.
            report DIVERGENCE \
                "daemon IPC UNRESPONSIVE (CONFIRMED): the process is alive (${liveness_detail}) — and its heartbeat may well look FRESH — but the bounded socket round-trip has now failed on ${probe_fail_streak} CONSECUTIVE watchdog ticks (threshold ${PROBE_FAIL_THRESHOLD}). ${probe_detail}. The heartbeat writer and the IPC accept loop are independent tokio tasks, so a fresh heartbeat does NOT prove the daemon can still serve work: autonomous dispatch is effectively DEAD while this holds. No automatic kill/restart is attempted (#4398 — there is no provably-safe unattended remediation for a wedged-but-alive process). RECOVER: 'loom-daemon restart' (note: the restart primitive travels over this same wedged socket and may itself hang), else ./.loom/scripts/cli/loom-daemon-stop.sh && ./.loom/scripts/cli/loom-daemon-start.sh [flags]. Diagnose with: LOOM_SOCKET_PATH=${SOCKET_PATH} ${PROBE_TIMEOUT_SECS}s-bounded 'loom-daemon status'; sample the process with 'sample ${live_pid}' (macOS) or 'gdb -p ${live_pid}' to capture the wedge before killing it."
            exit 1
        fi
        # Below threshold: loud + logged, but explicitly NOT a confirmed hang —
        # one failed round-trip can be transient contention (#4279: a
        # per-connection task dropping a request under concurrent-sweep load
        # that the very next one answers).
        report DIVERGENCE \
            "daemon IPC probe FAILED while the process is alive (${liveness_detail}): ${probe_detail}. This is consecutive failure ${probe_fail_streak} of ${PROBE_FAIL_THRESHOLD} — NOT yet a confirmed hang (a single failure can be transient contention under concurrent-sweep load, #4279) and no remediation is attempted. If the next watchdog tick round-trips cleanly the streak resets."
        PROBE_DIVERGED=true
        ;;
    *)
        # skipped: startup grace, no resolvable binary, unsupported subcommand,
        # probe disabled, or a daemon-side application error (which PROVES IPC
        # works). None of these may increment the tally or invent a divergence —
        # and none may clear an existing streak either.
        [[ "$VERBOSE" == "true" ]] && report OK "IPC probe skipped: ${probe_detail}."
        ;;
esac

# Final exit for the paths below that find nothing wrong themselves. A probe
# divergence already REPORTED above still owns the exit code — otherwise a
# wedged-but-heartbeating daemon would exit 0 and the report would be the only
# trace, which is exactly the signal-vs-exit-code split #4398 closes.
exit_ok() {
    [[ "$PROBE_DIVERGED" == "true" ]] && exit 1
    exit 0
}

# ---------- 4. reality: is the heartbeat fresh? ----------
# The daemon writes HEARTBEAT_FILE on a declared cadence (#4011). A live daemon
# whose heartbeat has gone stale is likely wedged — still a process, but not
# doing its periodic work. The threshold is a comfortable multiple of the
# cadence so a single missed write never false-positives.
STALE_SECS="${LOOM_DAEMON_HEARTBEAT_STALE_SECS:-}"
if [[ ! "$STALE_SECS" =~ ^[0-9]+$ ]]; then
    STALE_SECS=$(( HEARTBEAT_INTERVAL_SECS * 5 ))
    (( STALE_SECS < 300 )) && STALE_SECS=300
fi

file_mtime() {
    # Portable mtime (epoch secs): GNU `stat -c` vs BSD/macOS `stat -f`.
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

if [[ -f "$HEARTBEAT_FILE" ]]; then
    mtime="$(file_mtime "$HEARTBEAT_FILE")"
    if [[ "$mtime" =~ ^[0-9]+$ ]]; then
        now="$(date -u +%s)"
        age=$(( now - mtime ))
        # Prior-boot detection (#4368): if this heartbeat file predates the
        # live process's own start time, it is not evidence about the current
        # process at all — necessarily left over from a previous boot (or a
        # previous enablement of the opt-in heartbeat loop). Checked BEFORE
        # the staleness threshold below, mirroring the Rust probe's
        # `check_heartbeat` exactly (`daemon_install_state.rs`) so `status`
        # and this watchdog can never contradict each other. Only claim this
        # when the process age is actually known; an unparseable `ps` age
        # degrades to the ordinary Stale/Fresh checks below rather than a
        # false claim either way. `proc_age` is computed once in section 3.
        if [[ -n "$proc_age" ]] && (( age > proc_age )); then
            report OK "daemon alive (${liveness_detail}); heartbeat ${HEARTBEAT_FILE} is from a PREVIOUS boot (${age}s old; this process is only ${proc_age}s old) — not evidence about the current process. Liveness-only OK; re-check after the process is well past startup if you still suspect a wedge."
            exit_ok
        fi
        if (( age > STALE_SECS )); then
            report DIVERGENCE \
                "Daemon process is alive (${liveness_detail}) but its heartbeat ${HEARTBEAT_FILE} is STALE (${age}s old > ${STALE_SECS}s threshold) — the daemon may be wedged. Inspect with 'loom-daemon status'; consider ./.loom/scripts/cli/loom-daemon-stop.sh && ...start.sh."
            exit 1
        fi
        report OK "daemon healthy (${liveness_detail}); heartbeat fresh (${age}s ≤ ${STALE_SECS}s)."
        exit_ok
    fi
    # Unreadable mtime — degrade to liveness-only rather than false-report.
    report OK "daemon alive (${liveness_detail}); heartbeat mtime unreadable — liveness-only OK."
    exit_ok
fi

# No heartbeat file but the daemon is alive: either the heartbeat loop is
# disabled (LOOM_DAEMON_HEARTBEAT=0) or the daemon just started and has not
# written yet. Degrade to liveness-only — do NOT false-report, since the daemon
# clearly IS running.
report OK "daemon alive (${liveness_detail}); no heartbeat file at ${HEARTBEAT_FILE} (heartbeat disabled or not yet written) — liveness-only OK."
exit_ok
