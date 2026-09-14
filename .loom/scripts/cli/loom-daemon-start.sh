#!/usr/bin/env bash
# loom-daemon-start.sh - Safe start wrapper for the RAW loom-daemon process
# (the autonomous work-finder + main-health-gate host — epic #3809, Phase D
# #3813).
#
# This is NOT the tmux agent pool. `.loom/bin/loom start` (loom-start.sh)
# manages the Manual-Orchestration-Mode tmux pool; THIS script backgrounds the
# `loom-daemon` binary itself, which hosts the autonomous forge-polling work
# finder (#3810) and the reactive main-health gate (#3812). The two process
# models are independent and can coexist.
#
# It:
#   - locates the loom-daemon binary,
#   - runs the (advisory, never-blocking) host-sleep check (#3350),
#   - starts a plain reliability daemon with BOTH autonomous loops OFF by
#     default (matching the ecosystem-wide opt-in / default-off contract:
#     LOOM_WORK_FINDER unset => off, LOOM_MAIN_HEALTH_GATE unset => off). Opt in
#     explicitly with --work-finder / --health-gate, or hand control to
#     .loom/config.json -> autonomous with --from-config (#3911),
#   - on macOS, backgrounds the daemon as a launchd LaunchAgent (#3972) in the
#     resolved per-user domain (`gui/<uid>` when a GUI login is active, else
#     `user/<uid>` — #4130, so it can also be (re)started headlessly over SSH)
#     so it survives the launching session's death instead of a plain `nohup ...
#     &`; on a systemd Linux host, installs + enables a `systemd --user` service
#     (#4268) that mirrors the launchd contract (Restart=on-success,
#     disable-on-stop, LOOM_DAEMON_SUPERVISOR=systemd) — see --no-systemd for the
#     escape hatch; on a non-systemd Linux host (or with --no-systemd) it stays a
#     plain nohup background job,
#   - arms the autonomy-loss watchdog (#4011): on Darwin a SECOND launchd
#     StartInterval job, on a systemd Linux host a `<unit>-watchdog.timer` +
#     `.service` pair (#4260 sub-issue D) — both drive the SAME
#     loom-daemon-watchdog.sh payload on a recurring interval, independent of
#     the daemon job/unit, so a wedged or dead daemon still gets checked,
#   - self-heals a watchdog-provisioning GAP (#5343): if the daemon was armed
#     by a path other than a fresh start here (e.g. `fleet add-worker`'s
#     hand-rolled systemd unit install, or the daemon's own startup marker
#     healing, #4331) the autonomy-desired marker can end up present with NO
#     watchdog ever provisioned. Re-running this script against an
#     ALREADY-RUNNING daemon now provisions the missing watchdog before
#     exiting (rather than a bare "already running" no-op), or — on a
#     platform tier with no scheduled-job mechanism at all — files ONE
#     tracking issue instead of leaving the gap as a status line nobody reads,
#   - backgrounds the daemon and writes a PID file (.loom/.daemon.pid),
#   - persists the resolved invocation flags to .loom/.daemon.flags so
#     `loom-daemon-update.sh` (#3968) can restart with EXACTLY the same
#     autonomy flags after a rebuild — never wider,
#   - surfaces the singleton-guard refusal (#3806) legibly instead of leaving a
#     silently-exited background process.
#
# Default is FLAGS-OFF: a bare `loom-daemon-start.sh` does NOT auto-dispatch
# sweeps. This is a deliberate safe default — enable autonomy explicitly.
#
# macOS session-bootstrap hazard (#3972): a plain `nohup "$DAEMON_BIN" &`
# leaves the process wired into the LAUNCHING SESSION's Mach bootstrap
# namespace. When that session dies (a Claude Code session crash, a closed
# terminal, a dropped SSH connection) the daemon and every child it spawns
# start failing XPC lookups to trustd (cert verification -- `gh` TLS errors)
# and opendirectoryd (`getpwuid` -- "No user exists for uid N" from `git`),
# with NO crash and no obvious log signal beyond those downstream errors. This
# is why "start it from a terminal that might die" is unsafe on macOS. This
# script defaults to loading the daemon as a `launchd` LaunchAgent on Darwin
# specifically to avoid that failure mode; see --no-launchd below for the
# escape hatch and daemon-reference.md Operability for the incident writeup.
#
# launchd domain (#4130): the LaunchAgent is loaded into the domain
# resolve_launchd_domain() (lib/launchd-domain.sh) picks — `gui/<uid>` when a
# GUI (Aqua) login session is active (byte-for-byte the pre-#4130 behavior),
# else the background per-user `user/<uid>` domain that sshd instantiates, so a
# headless / SSH-only start no longer fails with `error 125: Domain does not
# support specified action`. Override with LOOM_LAUNCHD_DOMAIN.
#
# Usage:
#   ./.loom/scripts/cli/loom-daemon-start.sh                 Reliability daemon (both loops OFF)
#   ./.loom/scripts/cli/loom-daemon-start.sh --work-finder   Enable the autonomous work finder
#   ./.loom/scripts/cli/loom-daemon-start.sh --health-gate   Enable the main-health gate
#   ./.loom/scripts/cli/loom-daemon-start.sh --work-finder --health-gate   Both loops ON
#   ./.loom/scripts/cli/loom-daemon-start.sh --from-config   Enable per .loom/config.json only
#   ./.loom/scripts/cli/loom-daemon-start.sh --no-work-finder    Force work finder OFF (explicit)
#   ./.loom/scripts/cli/loom-daemon-start.sh --no-health-gate    Force health gate OFF (explicit)
#   ./.loom/scripts/cli/loom-daemon-start.sh --from-config --work-finder   Config-driven, but FORCE the work finder on (#4353)
#   ./.loom/scripts/cli/loom-daemon-start.sh --from-config --no-health-gate   Config-driven, but FORCE the health gate off (#4353)
#   ./.loom/scripts/cli/loom-daemon-start.sh --foreground    Run in the foreground (no PID file)
#   ./.loom/scripts/cli/loom-daemon-start.sh --no-launchd    macOS only: use legacy nohup instead of a LaunchAgent
#   ./.loom/scripts/cli/loom-daemon-start.sh --no-systemd    Linux only: use legacy nohup instead of a systemd --user service
#   ./.loom/scripts/cli/loom-daemon-start.sh --print-plist   Print the LaunchAgent plist that WOULD be installed and exit (no side effects)
#   ./.loom/scripts/cli/loom-daemon-start.sh --print-unit    Print the systemd --user unit that WOULD be installed and exit (no side effects)
#   ./.loom/scripts/cli/loom-daemon-start.sh --force-env     Acknowledge an intentional narrower re-render (#4522) -- actually DROPS env keys missing from this invocation's env; without it, dropped keys are carried forward from the installed unit/plist by default (#5344)
#   ./.loom/scripts/cli/loom-daemon-start.sh --heal-watchdog-only   Re-provision a missing watchdog job/timer (#5343's heal_watchdog_provisioning_gap) and exit -- never touches the PID file or attempts to start/stop a daemon (#5405)
#   ./.loom/scripts/cli/loom-daemon-start.sh --help
#
# Environment:
#   LOOM_DAEMON_BIN     Path to the loom-daemon binary (else auto-detected)
#   LOOM_SOCKET_PATH    Override the daemon socket (default ~/.loom/loom-daemon.sock)
#   LOOM_PID_FILE       OUTPUT, not input (#6420). This script is the EXPORTER of
#                        the pid-file path -- it derives "<state home>/.daemon.pid"
#                        and exports/bakes it into the plist/unit for the daemon
#                        and every reader (loom-daemon-stop.sh, -update.sh,
#                        -watchdog.sh, daemon_pidfile.rs), all of which DO honor an
#                        inbound value as tier 1 (#6386/#5118). An inbound value is
#                        deliberately ignored HERE; to place the pid file elsewhere,
#                        move the state home (LOOM_MACHINE_CHECKOUT / the repo root
#                        $PWD resolves to). See the rationale at the export site.
#   LOOM_WORK_FINDER / LOOM_MAIN_HEALTH_GATE  Respected when already exported
#                        (always wins, even under --from-config -- #4353)
#   LOOM_DAEMON_LAUNCHD  macOS only: 0/false/no forces the legacy nohup path (same as --no-launchd)
#   LOOM_DAEMON_SYSTEMD  Linux only: 0/false/no forces the legacy nohup path (same as --no-systemd)
#   LOOM_SYSTEMD_UNIT    Linux only: override the systemd --user unit name (default loom-daemon.service)
#   LOOM_WATCHDOG_LABEL  Override the watchdog job identifier (macOS: LaunchAgent
#                        label, default <daemon label>-watchdog; systemd Linux:
#                        service/timer unit basename, default <daemon unit>-watchdog)
#   LOOM_WATCHDOG_INTERVAL_SECS  Watchdog check cadence in seconds (default 300) —
#                        macOS StartInterval / systemd OnUnitActiveSec+OnBootSec
#   LOOM_LAUNCHD_LABEL   macOS only: override the LaunchAgent label (default com.rjwalters.loom-daemon)
#   LOOM_ALLOW_SESSION_DAEMON_START  Acknowledge a deliberate PRODUCTION start
#                        from inside a Loom agent session (#6568). By default,
#                        a shell exporting agent-session vars
#                        (LOOM_SWEEP_*/LOOM_TERMINAL_ID/LOOM_ROLE) is REFUSED a
#                        real start that would write the DEFAULT supervisor
#                        identity (com.rjwalters.loom-daemon /
#                        loom-daemon.service) -- the 2026-08-17 incident, where
#                        a sweep's start-path test overwrote the production
#                        LaunchAgent on both operator Macs with its own
#                        session env. Scope the identity instead
#                        (LOOM_LAUNCHD_LABEL / LOOM_SYSTEMD_UNIT) for a test;
#                        set this to 1 only for a genuine operator start.
#                        --print-plist/--print-unit are never refused (warn
#                        only). Independently of this refusal, the session-
#                        scoped keys themselves (LOOM_SWEEP_*,
#                        LOOM_TERMINAL_ID, LOOM_ROLE, LOOM_RUNTIME) are ALWAYS
#                        stripped from the rendered plist/unit and are never
#                        carried forward from an installed one.
#   LOOM_LAUNCHD_DOMAIN  macOS only: pin the launchd domain (e.g. gui/$(id -u) or
#                        user/$(id -u)); honored verbatim, else auto-resolved
#                        gui→user (#4130). A pinned domain that does not resolve
#                        fails loudly at bootstrap rather than falling back.
#   LOOM_DAEMON_PATH        Full override for the rendered plist's PATH (#4172).
#                        Used verbatim -- no canonical fallback is appended. For
#                        a host that needs a wholly custom PATH.
#   LOOM_DAEMON_PATH_EXTRA  Extra dir(s) to prepend onto the canonical minimal
#                        PATH (#4172) instead of overriding it entirely -- for
#                        a host that needs one or two additional dirs (e.g. a
#                        project-local toolchain) without inheriting the WHOLE
#                        invoking shell's interactive PATH.
#   LOOM_DAEMON_BOOTOUT_SETTLE_SECS  macOS/launchd only (#5081): max seconds to
#                        poll `launchctl print` after a `bootout`, waiting for
#                        the old job to actually leave the bootstrap namespace,
#                        before attempting `bootstrap` (default 5). `bootout`
#                        is asynchronous; an immediate `bootstrap` can race it
#                        and fail with "Bootstrap failed: 5: Input/output
#                        error" even against a valid plist.
#   LOOM_DAEMON_BOOTSTRAP_RETRY_ATTEMPTS  macOS/launchd only (#5081): max
#                        `launchctl bootstrap` attempts when it keeps failing
#                        with that same async-race I/O error (default 4).
#                        Never retries on any OTHER bootstrap failure (a
#                        genuinely bad plist/permission problem a retry cannot
#                        fix).
#   LOOM_DAEMON_BOOTSTRAP_RETRY_SECS  macOS/launchd only (#5081): seconds to
#                        sleep between bootstrap retries (default 2).
#   LOOM_MACHINE_CHECKOUT  Machine mode (Epic #3835 Phase 3b, #4229): set by
#                        the `scripts/loom` dispatcher to the resolved
#                        ~/.local/share/loom checkout before it execs this
#                        script. When set, the plist's WorkingDirectory and the
#                        pid/flags home resolve from THIS path -- not from
#                        $PWD -- so `loom start` manages the SAME machine-wide
#                        singleton daemon no matter which repo it is run from.
#                        Direct invocation of this script (the existing dev
#                        workflow) never sets it and is unaffected: $PWD-based
#                        find_repo_root() stays the fallback. See
#                        defaults/docs/machine-dispatcher.md.
#
# Exit codes:
#   0  daemon started (or already running)
#   1  usage error / binary not found / daemon failed to start / (#5409) a
#      DETECTED autonomy downgrade on a real start, refused pending an
#      explicit --work-finder / --no-work-finder / --health-gate /
#      --no-health-gate / --from-config / (#6568) a real start from a shell
#      carrying agent-session context that would write the DEFAULT supervisor
#      identity, refused pending an explicit LOOM_LAUNCHD_LABEL /
#      LOOM_SYSTEMD_UNIT or LOOM_ALLOW_SESSION_DAEMON_START=1

set -uo pipefail

# ---------- output helpers ----------
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BOLD=''; NC=''
fi
err()  { echo -e "${RED}$*${NC}" >&2; }
warn() { echo -e "${YELLOW}$*${NC}" >&2; }
ok()   { echo -e "${GREEN}$*${NC}"; }

show_help() {
    # Print the leading comment banner (line 2 through the last comment line
    # before `set -uo pipefail`), stripping the leading "# ".
    awk 'NR>=2 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

# ---------- repo root ----------
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

# ---------- locate the daemon binary ----------
# Shared with loom-daemon-watchdog.sh / loom-daemon-update.sh / loom-status.sh
# / `.loom/bin/loom health` via lib/locate-daemon-bin.sh (#4875) so all five
# never disagree about which binary is "the" daemon CLI, and a new candidate
# path only needs to be added in that one file. Includes the machine-level
# ~/.local/bin fallback so a non-interactive `ssh host 'cmd'` (which never
# sources the login profile, so ~/.local/bin is not on PATH) still finds the
# epic #3835 Phase 3a machine-level install.
_LOOM_LOCATE_BIN_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_LOCATE_BIN_LIB_DIR/locate-daemon-bin.sh" ]]; then
    # shellcheck source=../lib/locate-daemon-bin.sh
    source "$_LOOM_LOCATE_BIN_LIB_DIR/locate-daemon-bin.sh"
else
    err "locate-daemon-bin.sh not found at $_LOOM_LOCATE_BIN_LIB_DIR — this checkout is missing an expected lib file."
    exit 1
fi

# ---------- launchd plist rendering (#3972) ----------
# Pure string rendering -- safe to call on ANY platform (used by
# --print-plist for inspection/testing). The actual `launchctl` invocation
# that consumes this plist is gated to Darwin separately, below.
xml_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    printf '%s' "$s"
}

resolve_launchd_label() {
    echo "${LOOM_LAUNCHD_LABEL:-com.rjwalters.loom-daemon}"
}

# ---------- agent-session isolation (#6568) ----------
# Incident 2026-08-17: a daemon-dispatched sweep (working issue #6388 out of a
# /tmp checkout) invoked this script to exercise the start path. Two properties
# combined to overwrite the REAL production LaunchAgent on BOTH operator Macs:
#
#   1. resolve_launchd_label() above returns the fixed production label
#      whenever LOOM_LAUNCHD_LABEL is unset -- so a test/session invocation
#      that forgets the override targets `com.rjwalters.loom-daemon` in the
#      real gui/<uid> domain, i.e. it REPLACES the production job.
#   2. render_launchd_plist / render_systemd_unit harvest EVERY exported LOOM_*
#      var into the durable EnvironmentVariables dict / Environment= lines --
#      so the sweep's own per-invocation session env (LOOM_SWEEP_CLAIM_OWNED,
#      LOOM_TERMINAL_ID, LOOM_ROLE, LOOM_RUNTIME) plus its /tmp-rooted
#      LOOM_WORKSPACE and a stray LOOM_ROLE_RUNNER_INTERVAL_SECS became the
#      production daemon's durable config. Undetected for two days.
#
# Two independent defenses, because either alone still reproduces part of it:
#
#   * is_session_scoped_env_key() -- a HARD FILTER applied at every render (and
#     at the #5344 carry-forward merge, so an ALREADY-poisoned installed
#     plist/unit cannot re-inject them either). These keys describe ONE agent
#     invocation; they are never correct as durable daemon config, no matter
#     who runs the script.
#   * guard_session_context_start() -- REFUSES a real start that would write
#     the DEFAULT production identity from a shell carrying agent-session
#     context, without requiring the caller to remember LOOM_LAUNCHD_LABEL.
#
# LOOM_RUNTIME is stripped but is deliberately NOT a detection signal: it is a
# plausible thing for an operator to export as a personal default, and a
# refusal keyed on it would block legitimate `loom start` runs. The three
# detection keys below are set only by Loom's own agent-session spawn path.
LOOM_SESSION_CONTEXT_KEY_RE='^(LOOM_SWEEP_[A-Za-z0-9_]*|LOOM_TERMINAL_ID|LOOM_ROLE)='

# is_session_scoped_env_key <KEY> — true when KEY names a per-invocation AGENT
# SESSION variable that must never be baked into durable daemon config.
is_session_scoped_env_key() {
    case "$1" in
        LOOM_SWEEP_*|LOOM_TERMINAL_ID|LOOM_ROLE|LOOM_RUNTIME) return 0 ;;
        *) return 1 ;;
    esac
}

# session_context_keys — space-separated list of the agent-session keys THIS
# shell exports (empty when the invoking shell is a plain operator shell).
session_context_keys() {
    env | grep -E "$LOOM_SESSION_CONTEXT_KEY_RE" 2>/dev/null | cut -d= -f1 | sort -u | tr '\n' ' ' | sed 's/ *$//'
}

# in_session_context — true when this shell looks like a Loom agent session.
in_session_context() {
    [[ -n "$(session_context_keys)" ]]
}

# resolve_launchd_domain() — the launchd domain (gui/<uid> ↦ user/<uid>) the
# LaunchAgent is loaded/inspected/booted-out under (#4130). Shared verbatim with
# loom-daemon-stop.sh / -update.sh / -watchdog.sh via lib/launchd-domain.sh so
# all four lifecycle scripts always agree on the domain. Sourced here (all four
# scripts source the same one definition).
_LOOM_LAUNCHD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" 2>/dev/null && pwd)"
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh" ]]; then
    # shellcheck source=../lib/launchd-domain.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/launchd-domain.sh"
fi
# systemd --user resolver (#4268) — the Linux counterpart to launchd-domain.sh
# (is_linux_systemd / resolve_systemd_unit* / systemd_user_manager_reachable),
# sourced by start/stop so both agree on unit name + path + detection.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/systemd-user.sh" ]]; then
    # shellcheck source=../lib/systemd-user.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/systemd-user.sh"
fi
# bounded_run() (#4398, shared with loom-daemon-watchdog.sh's IPC probe) —
# print_calibrate_hint() below needs it to bound its own blocking `$(...)`
# call (#4799).
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/bounded-run.sh" ]]; then
    # shellcheck source=../lib/bounded-run.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/bounded-run.sh"
fi
# canonical_daemon_path() (#4831) — the single shared canonical PATH superset
# (~/.local/bin, ~/.cargo/bin, Homebrew, standard system dirs) resolve_plist_path()
# below renders into every plist/unit. Extracted out of this script so the
# fleet provisioning path (loom-daemon/src/fleet/add_worker.rs) and the
# self-update cargo fallback (loom-daemon-update.sh, #4695) can agree with it
# instead of maintaining their own disagreeing partial copies.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/canonical-daemon-path.sh" ]]; then
    # shellcheck source=../lib/canonical-daemon-path.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/canonical-daemon-path.sh"
fi
# verify_launchd_env_applied() (#5081) — post-bootstrap check that the
# launchd job actually reports the freshly-rendered plist's
# EnvironmentVariables, used by the launchd start path below to catch a
# "bootstrap succeeded, pid is alive, but the env is somehow still stale"
# outcome rather than silently reporting success. Shared with
# loom-daemon-update.sh via lib/daemon-env-harvest.sh (#4581) so both call
# sites agree on how a plist's env is read back.
if [[ -r "$_LOOM_LAUNCHD_LIB_DIR/daemon-env-harvest.sh" ]]; then
    # shellcheck source=../lib/daemon-env-harvest.sh
    source "$_LOOM_LAUNCHD_LIB_DIR/daemon-env-harvest.sh"
fi

# resolve_plist_path() — the deterministic PATH baked into every rendered
# plist (daemon + watchdog), issue #4172. Previously the rendered PATH was
# "$PATH:<canonical fallback>" -- the INVOKING SHELL's entire interactive
# PATH prefixed onto the fallback set -- which made a re-render non-hermetic:
# whoever's shell happened to run `loom-daemon-start.sh` (or
# `loom-daemon-update.sh --relaunch`) determined the daemon's tool
# resolution, and an unrelated project-specific toolchain earlier in that
# PATH could shadow the binaries the daemon and its sweep children expect.
# Resolution order (highest precedence first), always logged to STDERR (never
# stdout, so `--print-plist`'s XML output stays pipeable/diffable):
#   1. LOOM_DAEMON_PATH      -- full override, used verbatim (no fallback
#                               appended). For a host that needs a wholly
#                               custom PATH.
#   2. LOOM_DAEMON_PATH_EXTRA -- prepended onto the canonical minimal PATH,
#                               for a host that needs one or two additional
#                               dirs without inheriting the whole invoking
#                               shell's interactive PATH.
#   3. Default: the canonical minimal PATH -- exactly the pre-#4172 fallback
#      set (~/.local/bin, ~/.cargo/bin, Homebrew, standard bin dirs, already
#      sufficient for gh/git/cargo/python3), sourced from
#      lib/canonical-daemon-path.sh (#4831) so this is no longer the only
#      place that set is spelled out, with NO shell-PATH prefix. This makes a
#      bare re-render byte-for-byte reproducible across hosts/sessions.
resolve_plist_path() {
    local canonical
    if declare -F canonical_daemon_path >/dev/null 2>&1; then
        canonical="$(canonical_daemon_path)"
    else
        # Degraded fallback if lib/canonical-daemon-path.sh could not be
        # sourced (e.g. a partial/corrupted install) -- keep byte-for-byte
        # identical to the lib's definition.
        canonical="${HOME}/.local/bin:${HOME}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    fi
    if [[ -n "${LOOM_DAEMON_PATH:-}" ]]; then
        echo "Rendered plist PATH: full override via LOOM_DAEMON_PATH -> ${LOOM_DAEMON_PATH}" >&2
        printf '%s' "${LOOM_DAEMON_PATH}"
        return 0
    fi
    if [[ -n "${LOOM_DAEMON_PATH_EXTRA:-}" ]]; then
        echo "Rendered plist PATH: canonical minimal PATH + LOOM_DAEMON_PATH_EXTRA -> ${LOOM_DAEMON_PATH_EXTRA}:${canonical}" >&2
        printf '%s' "${LOOM_DAEMON_PATH_EXTRA}:${canonical}"
        return 0
    fi
    echo "Rendered plist PATH: canonical minimal PATH (deterministic default) -> ${canonical}" >&2
    printf '%s' "$canonical"
}

# extract_plist_path_value <plist_file> — best-effort textual extraction of
# the <key>PATH</key>\n<string>VALUE</string> pair from a rendered launchd
# plist. Deliberately NOT a general plist parser (no plutil/jq dependency) --
# every plist this script renders follows that exact two-line shape, so a
# simple awk match is sufficient. Used only by the --print-plist drift check
# below; returns empty (and exit 1) when the key is absent or the file is
# missing.
extract_plist_path_value() {
    local plist_file="$1"
    [[ -f "$plist_file" ]] || return 1
    awk '
        /<key>PATH<\/key>/ { want=1; next }
        want { sub(/^[ \t]*<string>/, ""); sub(/<\/string>[ \t]*$/, ""); print; exit }
    ' "$plist_file"
}

# ---------- dropped-env-key detection (#4522) ----------
# Root cause under test: render_launchd_plist / render_systemd_unit render the
# EnvironmentVariables dict / Environment= lines strictly from whatever THIS
# invocation has exported ("Respected when already exported" note above). Any
# invocation missing the operator's exports (a watchdog, a bare re-run, another
# tool shelling out to this script) silently replaces a richer installed
# plist/unit with a narrower one -- e.g. every LOOM_SAFEHOUSE_* key and
# LOOM_WORK_FINDER=1 quietly gone. The functions below detect that KEY REMOVAL
# (not a value change -- see the PATH-specific drift check above for that) so
# it is surfaced instead of silently applied.

# extract_plist_env_keys <plist_file> — list of every <key>...</key> entry
# inside the EnvironmentVariables dict, one per line. Extends
# extract_plist_path_value's textual-awk approach (no plutil/XML-parser
# dependency) from a single key to the whole dict.
extract_plist_env_keys() {
    local plist_file="$1"
    [[ -f "$plist_file" ]] || return 1
    awk '
        /<key>EnvironmentVariables<\/key>/ { in_env=1; next }
        in_env && /<\/dict>/ { exit }
        in_env && /<key>/ {
            line=$0
            sub(/^[ \t]*<key>/, "", line)
            sub(/<\/key>.*$/, "", line)
            print line
        }
    ' "$plist_file"
}

# extract_systemd_env_keys <unit_file> — list of every `Environment=KEY=...`
# key in a rendered systemd unit, one per line. The systemd analog of
# extract_plist_env_keys above.
extract_systemd_env_keys() {
    local unit_file="$1"
    [[ -f "$unit_file" ]] || return 1
    sed -n 's/^Environment=\([^=]*\)=.*/\1/p' "$unit_file"
}

# ---------- installed-plist/unit VALUE extraction (#4693) ----------
# Single-key siblings of extract_plist_env_keys / extract_systemd_env_keys
# above (which list every key present -- these read the VALUE of one named
# key). Used by the silent-autonomy-downgrade check below to read what
# LOOM_WORK_FINDER / LOOM_MAIN_HEALTH_GATE the PRIOR installed plist/unit
# actually carried, before it gets overwritten by this invocation's render.

# extract_plist_env_value <plist_file> <key> — the <string> value paired with
# <key>KEY</key> inside the EnvironmentVariables dict, or empty when the file
# or the key is absent.
extract_plist_env_value() {
    local plist_file="$1" want_key="$2"
    [[ -f "$plist_file" ]] || return 1
    awk -v want="$want_key" '
        /<key>EnvironmentVariables<\/key>/ { in_env=1; next }
        in_env && /<\/dict>/ { exit }
        in_env && found && /<string>/ {
            line=$0
            sub(/^[ \t]*<string>/, "", line); sub(/<\/string>[ \t]*$/, "", line)
            print line
            exit
        }
        in_env && /<key>/ {
            line=$0
            sub(/^[ \t]*<key>/, "", line); sub(/<\/key>.*$/, "", line)
            found = (line == want) ? 1 : 0
        }
    ' "$plist_file"
}

# extract_systemd_env_value <unit_file> <key> — the value of a
# `Environment=KEY=...` line for a specific key, or empty when the file or the
# key is absent.
extract_systemd_env_value() {
    local unit_file="$1" want_key="$2"
    [[ -f "$unit_file" ]] || return 1
    sed -n "s/^Environment=${want_key}=\\(.*\\)\$/\\1/p" "$unit_file" | head -n1
}

# ---------- carry-forward injection (#5344) ----------
# Single-key siblings of the VALUE extractors above -- these WRITE a key/value
# pair into an already-rendered plist/unit file, in place. Used by
# warn_dropped_env_keys below to carry a dropped key's INSTALLED value forward
# into a freshly-rendered file so an unattended re-render (watchdog /
# automated / a bare re-run from a different shell) never silently narrows
# the running job's environment.

# inject_one_plist_env_entry <file> <key> <value> — insert a
# <key>KEY</key><string>VALUE</string> pair into the EnvironmentVariables
# dict of <file>, immediately before the </dict> that closes it.
inject_one_plist_env_entry() {
    local file="$1" key="$2" value="$3"
    local esc_key esc_value tmp
    esc_key="$(xml_escape "$key")"
    esc_value="$(xml_escape "$value")"
    tmp="$(mktemp "${TMPDIR:-/tmp}/loom-plist-inject.XXXXXX")"
    awk -v k="$esc_key" -v v="$esc_value" '
        BEGIN { in_env = 0; injected = 0 }
        /<key>EnvironmentVariables<\/key>/ { in_env = 1; print; next }
        in_env && !injected && /<\/dict>/ {
            printf "        <key>%s</key>\n        <string>%s</string>\n", k, v
            injected = 1
        }
        { print }
    ' "$file" > "$tmp" && mv "$tmp" "$file"
}

# inject_one_systemd_env_entry <file> <key> <value> — append an
# `Environment=KEY=VALUE` line to <file>, immediately after the last existing
# `Environment=` line (falling back to right after `[Service]` if somehow
# none exist). The systemd analog of inject_one_plist_env_entry above.
inject_one_systemd_env_entry() {
    local file="$1" key="$2" value="$3"
    local last_line tmp
    last_line="$(grep -n '^Environment=' "$file" | tail -n1 | cut -d: -f1)"
    [[ -z "$last_line" ]] && last_line="$(grep -n '^\[Service\]' "$file" | head -n1 | cut -d: -f1)"
    tmp="$(mktemp "${TMPDIR:-/tmp}/loom-unit-inject.XXXXXX")"
    awk -v ln="${last_line:-0}" -v ins="Environment=${key}=${value}" '
        { print }
        NR == ln { print ins }
    ' "$file" > "$tmp" && mv "$tmp" "$file"
}

# warn_dropped_env_keys <old_file> <new_file> <keys_extractor_fn> <value_extractor_fn> <injector_fn> — compare
# the env-var KEY sets (not values) between an already-installed plist/unit and
# a freshly-rendered replacement; when the replacement would DROP a key the
# installed file carried, warn (listing the keys) AND -- by default -- carry
# the installed VALUE forward into <new_file> in place so the drop never
# actually happens (#5344). <keys_extractor_fn> is extract_plist_env_keys or
# extract_systemd_env_keys; <value_extractor_fn> is its single-key VALUE
# sibling (extract_plist_env_value / extract_systemd_env_value);
# <injector_fn> is the matching writer (inject_one_plist_env_entry /
# inject_one_systemd_env_entry).
#
#   - A missing old_file (first-ever install -- nothing installed yet) is not a
#     drop: returns silently, no warning, no merge.
#   - --force-env (FORCE_ENV=true) acknowledges an INTENTIONAL narrowing (e.g.
#     an explicit minimal re-render): the merge is skipped entirely and the
#     dropped key(s) are actually absent from <new_file>, with no warning.
#     This is the ONLY way to shrink the installed env now -- the default
#     path can only ever widen or match it.
#   - A dropped LOOM_SAFEHOUSE_* key gets a specific migration hint (the
#     "safehouse" block in .loom/config.json + --from-config, #4353) alongside
#     the generic warning.
warn_dropped_env_keys() {
    local old_file="$1" new_file="$2" keys_extractor="$3" value_extractor="$4" injector="$5"
    [[ -f "$old_file" ]] || return 0

    local old_keys new_keys
    old_keys="$("$keys_extractor" "$old_file" 2>/dev/null || true)"
    [[ -z "$old_keys" ]] && return 0
    new_keys="$("$keys_extractor" "$new_file" 2>/dev/null || true)"

    local dropped=() purged=() k nk hit
    while IFS= read -r k; do
        [[ -z "$k" ]] && continue
        hit=false
        if [[ -n "$new_keys" ]]; then
            while IFS= read -r nk; do
                if [[ "$nk" == "$k" ]]; then hit=true; break; fi
            done <<< "$new_keys"
        fi
        [[ "$hit" == "true" ]] && continue
        # #6568: an agent-session key the INSTALLED file carries is exactly the
        # corruption this merge must NOT preserve. Without this branch the
        # carry-forward would faithfully re-inject LOOM_SWEEP_CLAIM_OWNED /
        # LOOM_TERMINAL_ID / LOOM_ROLE / LOOM_RUNTIME on every subsequent
        # re-render, making the 2026-08-17 poisoning self-healing-proof: the
        # renderers' strip (above) would drop them and this merge would put
        # them straight back. Report the purge instead of carrying it forward.
        if is_session_scoped_env_key "$k"; then
            purged+=("$k")
            continue
        fi
        dropped+=("$k")
    done <<< "$old_keys"

    if [[ "${#purged[@]}" -gt 0 ]]; then
        warn ""
        warn "NOTICE: purging ${#purged[@]} AGENT-SESSION env key(s) carried by the installed $old_file (#6568):"
        for k in "${purged[@]}"; do
            warn "  - $k (per-invocation session state, never durable daemon config)"
        done
        warn "These are deliberately NOT carried forward into $new_file. Their presence means a daemon"
        warn "config was once written from an agent session's environment -- see .loom/docs/daemon-reference.md."
    fi

    [[ "${#dropped[@]}" -eq 0 ]] && return 0

    # --force-env: acknowledge the intentional narrowing and let it stand --
    # no merge, no warning. Checked here (after computing $dropped) rather
    # than as an early return so both the merge and the warning share exactly
    # the same "what would be dropped" computation above.
    [[ "${FORCE_ENV:-false}" == "true" ]] && return 0

    warn ""
    warn "WARNING: re-rendering $new_file drops ${#dropped[@]} env key(s) present in the installed $old_file:"
    for k in "${dropped[@]}"; do
        if [[ "$k" == LOOM_SAFEHOUSE_* ]]; then
            warn "  - $k (config-tier equivalent: the \"safehouse\" block in .loom/config.json + --from-config, #4353)"
        else
            warn "  - $k"
        fi
    done
    warn "This usually means this invocation ran without the operator's exported env (a watchdog / automated re-render / a bare re-run from a different shell)."
    warn "Carrying the installed value(s) of the key(s) above forward into $new_file so this invocation does not silently narrow it. Pass --force-env to acknowledge an intentional narrowing and actually drop them instead."

    # Merge (#5344): carry each dropped key's INSTALLED value forward into
    # $new_file so the file on disk after this call is never narrower than
    # $old_file, matching the warning above.
    local v
    for k in "${dropped[@]}"; do
        v="$("$value_extractor" "$old_file" "$k" 2>/dev/null || true)"
        [[ -z "$v" ]] && continue
        "$injector" "$new_file" "$k" "$v"
    done
}

# ---------- silent autonomy-downgrade detection (#4693, hardened #5409) ----------
# Incident 2026-07-30: a routine loom-daemon-start.sh run (no flags) silently
# re-rendered the plist with LOOM_WORK_FINDER=0 -- downgrading a previously
# autonomous daemon to FLAGS-OFF with NO warning. ~3h of dispatch outage (23
# ready issues sat queued, "work availability is the limiter") before the
# missing "work_finder: starting" log line was traced back to the plist env.
#
# Incident 2026-08-05 (#5409): the #4693 mitigation below (a WARNING, never
# blocking) was NOT enough -- it recurred, on the RECOVERY path specifically:
# an operator ran the exact command `loom-daemon status` itself recommends
# ("Recover with: ./.loom/scripts/cli/loom-daemon-start.sh"), the WARNING
# scrolled past in the recovery output, and the fleet host lost ~1h of
# dispatch with a daemon reporting perfectly healthy the whole time. #5409
# resolved the issue's own "asymmetry worth weighing" (a wrongly-preserved-on
# daemon is trivially visible and reversible; a wrongly-silenced-off daemon
# looks like a healthy, quiet fleet) in favor of erring toward NOT silently
# downgrading: a DETECTED downgrade on a REAL start now REFUSES to proceed
# (exit 1) rather than warn-and-continue, until the operator states the
# desired value for THIS invocation explicitly. --print-plist / --print-unit
# stay warn-only (see the $PRINT_PLIST/$PRINT_UNIT guard in
# warn_autonomy_downgrade below) -- they are read-only preview modes with no
# side effect to block, and refusing them would make it IMPOSSIBLE to inspect
# what a real start would render.
#
# The FLAGS-OFF default for a PLAIN, GENUINELY FRESH start (#3911 -- no prior
# plist/unit, no marker) is correct and stays completely unchanged -- this
# only closes the SILENT part of a transition FROM autonomous TO FLAGS-OFF.
#
# Signals consulted (either alone is sufficient to flag a downgrade):
#   1. the PRIOR installed plist/unit had the key ON (=1) -- direct evidence
#      this daemon was running autonomously a moment ago.
#   1b. (#5437) when no plist/unit exists to consult (always true on the nohup
#      fallback tier -- no plist/unit is EVER rendered there), fall back to the
#      work_finder=/health_gate= fields write_intent_marker() persists into the
#      marker itself on every successful start -- the actual prior flag value,
#      not just "a daemon started here at some point". This is what lets a
#      bare restart following a PRIOR bare start on that tier stay silent
#      (old value was already "0" -- no transition) while a bare restart
#      following a PRIOR autonomous start still correctly falls through to #2.
#   2. the autonomy-desired marker (#4011) is present but NEITHER the prior
#      plist/unit NOR the marker's own persisted fields yielded a value (e.g.
#      the first Darwin start after a nohup-only history whose marker predates
#      #5437) -- marker presence alone is recorded operator intent, and the
#      issue explicitly calls this combination out.
# When the prior value was already "0" (no transition) this stays silent --
# a standing marker-vs-FLAGS-OFF mismatch with no fresh transition is
# `loom-daemon status`'s job to flag (a non-OK/exit-code signal as of #5409),
# not this one-shot start-time check.
#
# Deliberately NOT triggered by:
#   - --from-config (control is explicitly handed to .loom/config.json --
#     not a silent default; see the FROM_CONFIG guard in the caller),
#   - an explicit --no-work-finder / --no-health-gate THIS invocation (an
#     explicit ask is not silent -- this is precisely the "state it
#     explicitly" escape hatch #5409 asks for),
#   - an operator-exported LOOM_WORK_FINDER=0 / LOOM_MAIN_HEALTH_GATE=0 in the
#     calling shell (also an explicit, non-default signal -- "Respected when
#     already exported", see the Environment section in the help banner).
AUTONOMY_DOWNGRADE_DETECTED=false

check_autonomy_downgrade_key() {
    local key="$1" new_val="$2" want_flag="$3" pre_exported="$4"
    [[ "$new_val" == "0" ]] || return 0
    [[ "$want_flag" == "off" ]] && return 0
    [[ -n "$pre_exported" ]] && return 0

    local old_val=""
    if [[ -n "${PRIOR_AUTONOMY_FILE:-}" && -f "$PRIOR_AUTONOMY_FILE" ]]; then
        old_val="$("$PRIOR_AUTONOMY_EXTRACTOR" "$PRIOR_AUTONOMY_FILE" "$key" 2>/dev/null || true)"
    fi

    local marker_present=false
    [[ -f "$INTENT_MARKER" ]] && marker_present=true

    # #5437: fall back to the actual prior value THIS SAME MARKER recorded on
    # the last successful start (write_intent_marker's work_finder=/
    # health_gate= fields) when the mechanism-specific file above yielded
    # nothing. This is the ONLY signal available on the nohup fallback tier
    # (PRIOR_AUTONOMY_FILE is always empty there -- no plist/unit is ever
    # rendered) and is strictly more accurate than the marker-presence-only
    # inference below: it distinguishes a PRIOR bare (FLAGS-OFF) start from a
    # PRIOR autonomous one, instead of treating both identically. A marker
    # written before this field existed (old format) still falls through to
    # the presence-only check, preserving the original conservative refusal.
    if [[ -z "$old_val" && "$marker_present" == "true" ]]; then
        local marker_field=""
        case "$key" in
            LOOM_WORK_FINDER) marker_field="work_finder" ;;
            LOOM_MAIN_HEALTH_GATE) marker_field="health_gate" ;;
        esac
        if [[ -n "$marker_field" ]]; then
            old_val="$(grep -E "^${marker_field}=" "$INTENT_MARKER" 2>/dev/null | head -n1 | cut -d= -f2-)"
        fi
    fi

    if [[ "$old_val" == "1" ]]; then
        AUTONOMY_DOWNGRADE_DETECTED=true
        warn ""
        warn "WARNING: autonomy downgrade -- $key: 1 -> 0"
        warn "  The previously installed daemon had $key=1 (autonomous); this plain start"
        warn "  would render it OFF -- matching the FLAGS-OFF-by-default contract for a start"
        warn "  with no explicit flags (#3911), but SILENTLY from an operator's point of view."
        warn "  Remediation: pass --from-config (drive from .loom/config.json -> autonomous),"
        warn "  --work-finder / --health-gate to keep autonomy on, or --no-work-finder /"
        warn "  --no-health-gate to confirm you want it off."
        return 0
    fi

    if [[ -z "$old_val" && "$marker_present" == "true" ]]; then
        AUTONOMY_DOWNGRADE_DETECTED=true
        warn ""
        warn "WARNING: autonomy downgrade -- $key renders 0 this start, and no prior plist/unit"
        warn "  value could be read -- but the autonomy-desired marker ($INTENT_MARKER) is"
        warn "  present, meaning this host previously ran loom-daemon autonomously."
        warn "  Remediation: pass --from-config (drive from .loom/config.json -> autonomous),"
        warn "  --work-finder / --health-gate to keep autonomy on, or --no-work-finder /"
        warn "  --no-health-gate to confirm you want it off."
        return 0
    fi
}

# warn_autonomy_downgrade — evaluate both autonomy loops, then (#5409) REFUSE
# a real start outright if either flagged a downgrade. Called once
# PRIOR_AUTONOMY_FILE/PRIOR_AUTONOMY_EXTRACTOR and INTENT_MARKER are resolved
# (after platform detection, before the plist/unit gets overwritten -- and
# also from the read-only --print-plist/--print-unit inspection paths, so an
# operator sees the warning before committing to a real start too).
warn_autonomy_downgrade() {
    # --from-config hands control to .loom/config.json deliberately -- not a
    # silent default -- so it is exempt from this check entirely.
    [[ "$FROM_CONFIG" == "true" ]] && return 0
    check_autonomy_downgrade_key "LOOM_WORK_FINDER" "$LOOM_WORK_FINDER" "$WANT_WORK_FINDER" "$PRE_EXPORTED_WORK_FINDER"
    check_autonomy_downgrade_key "LOOM_MAIN_HEALTH_GATE" "$LOOM_MAIN_HEALTH_GATE" "$WANT_HEALTH_GATE" "$PRE_EXPORTED_MAIN_HEALTH_GATE"

    # #5409 AC1: refuse a REAL start (never a pure inspection) rather than
    # warn-and-continue. The two --print-plist/--print-unit inspection modes
    # stay warn-only -- they render a preview with no side effect, and
    # refusing them would make it impossible to see what a real start would
    # do before committing to it.
    if [[ "$AUTONOMY_DOWNGRADE_DETECTED" == "true" && "$PRINT_PLIST" != "true" && "$PRINT_UNIT" != "true" ]]; then
        err ""
        err "ERROR: refusing to start -- this would silently downgrade autonomy (see the"
        err "WARNING(s) above). Pass an explicit --work-finder / --no-work-finder (and/or"
        err "--health-gate / --no-health-gate) to state the desired value for THIS"
        err "invocation, or --from-config to drive from .loom/config.json -> autonomous."
        err "(This refusal fires only on a DETECTED downgrade -- prior plist/unit had the"
        err "loop on, or the autonomy-desired marker is present. A genuinely fresh start"
        err "with no prior signal, #3911, is unaffected and still defaults FLAGS-OFF.)"
        exit 1
    fi
}

# ---------- agent-session isolation guard (#6568) ----------
# The refusal half of the #6568 defenses (the strip half lives in
# is_session_scoped_env_key, used by both renderers and by the carry-forward
# merge). Modeled EXACTLY on warn_autonomy_downgrade above, deliberately:
#
#   * REAL START -> refuse (exit 1). Writing the default production identity
#     from a shell that carries agent-session context is the 2026-08-17
#     incident shape, and there is no way for the script to tell a deliberate
#     "exercise the start path" from a genuine operator start after the fact.
#   * --print-plist / --print-unit -> WARN only, exit 0, plist/unit still on
#     stdout. These are read-only previews with no side effect to block, and
#     refusing them would make it impossible to see what a real start would
#     render -- the same reasoning (and the same $PRINT_PLIST/$PRINT_UNIT
#     guard) warn_autonomy_downgrade uses.
#
# Scope: only the two tiers that write a durable supervisor definition under a
# well-known identity (launchd label, systemd --user unit). The nohup fallback
# tier renders NO plist/unit at all, so it has no durable config to poison and
# is left byte-for-byte unchanged.
#
# Exemptions (both explicit acts, never a default):
#   * An explicit LOOM_LAUNCHD_LABEL (launchd) / LOOM_SYSTEMD_UNIT (systemd)
#     -- the caller has already scoped the start to a non-production identity,
#     which is what every test in this repo does. This keeps the existing
#     test-authoring pattern working unchanged from a session context.
#   * LOOM_ALLOW_SESSION_DAEMON_START=1 -- an operator acknowledging that they
#     really are starting the production daemon from inside an agent session
#     (e.g. recovering a fleet host from a Claude Code terminal). It is loud,
#     not silent: the warning still prints.
guard_session_context_start() {
    local session_keys mech identity_hint override_hint
    session_keys="$(session_context_keys)"
    [[ -z "$session_keys" ]] && return 0

    # Which mechanism would this invocation write? The inspection modes decide
    # it from argv alone (--print-plist => launchd, --print-unit => systemd),
    # exactly like run_inspection_mode_and_exit's PRIOR_AUTONOMY_* resolution;
    # a real start uses whatever platform detection picked.
    if [[ "$PRINT_PLIST" == "true" ]]; then
        mech="launchd"
    elif [[ "$PRINT_UNIT" == "true" ]]; then
        mech="systemd"
    elif [[ "${USE_LAUNCHD:-false}" == "true" ]]; then
        mech="launchd"
    elif [[ "${IS_LINUX_SYSTEMD:-false}" == "true" ]]; then
        mech="systemd"
    else
        return 0
    fi

    if [[ "$mech" == "launchd" ]]; then
        [[ -n "${LOOM_LAUNCHD_LABEL:-}" ]] && return 0
        identity_hint="the production LaunchAgent label $(resolve_launchd_label) in the real launchd domain"
        override_hint="LOOM_LAUNCHD_LABEL=com.example.loom-daemon-test"
    else
        [[ -n "${LOOM_SYSTEMD_UNIT:-}" ]] && return 0
        identity_hint="the production systemd --user unit ${LOOM_SYSTEMD_UNIT:-loom-daemon.service}"
        override_hint="LOOM_SYSTEMD_UNIT=loom-daemon-test.service"
    fi

    warn ""
    warn "WARNING: agent-session context detected -- this shell exports: $session_keys"
    warn "  A start from here would write $identity_hint"
    warn "  from a per-invocation agent environment (workspace, log paths and autonomy"
    warn "  knobs scoped to ONE sweep). That is incident 2026-08-17: both operator Macs'"
    warn "  production daemons ran for two days under a sweep's test configuration."

    if [[ "${LOOM_ALLOW_SESSION_DAEMON_START:-}" =~ ^(1|true|yes)$ ]]; then
        warn "  Proceeding anyway: LOOM_ALLOW_SESSION_DAEMON_START is set (explicit operator acknowledgement)."
        return 0
    fi

    # Read-only previews are never refused (see the rationale above).
    if [[ "$PRINT_PLIST" == "true" || "$PRINT_UNIT" == "true" ]]; then
        warn "  This is a read-only preview, so it is NOT refused -- but a REAL start with this"
        warn "  environment would be. See the remediation below."
        warn "  Remediation: scope the identity ($override_hint),"
        warn "  drop the session vars (env -u LOOM_ROLE -u LOOM_TERMINAL_ID -u LOOM_SWEEP_CLAIM_OWNED ...),"
        warn "  or set LOOM_ALLOW_SESSION_DAEMON_START=1 to acknowledge a deliberate production start."
        return 0
    fi

    err ""
    err "ERROR: refusing to start -- this would overwrite the REAL daemon configuration with"
    err "an agent session's environment (see the WARNING above). Choose one:"
    err "  * Exercising/testing the start path? Scope the supervisor identity:"
    err "      $override_hint"
    err "  * Genuinely starting the production daemon from inside an agent session?"
    err "      LOOM_ALLOW_SESSION_DAEMON_START=1 $0 ..."
    err "  * Or run it from a clean shell:"
    err "      env -u LOOM_ROLE -u LOOM_TERMINAL_ID -u LOOM_SWEEP_CLAIM_OWNED $0 ..."
    err "(#6568 -- the session-scoped keys themselves are stripped from every rendered"
    err "plist/unit regardless; this refusal additionally protects the production identity.)"
    exit 1
}

# ---------- scratch-workdir drift warning (#6568 ask 3) ----------
# The other half of the 2026-08-17 blast radius: nothing on either Mac
# complained that the production daemon's WorkingDirectory / LOOM_WORKSPACE
# had become /tmp/pr6416-checkout, so it booted silently under it for two
# days (and its watchdog/socket/pid paths, under a mktemp dir, evaporated at
# the next reboot). Surface it loudly at start/render time instead.
#
# Advisory only -- it never blocks a start. A scratch-rooted daemon is exactly
# what this repo's own hermetic test suites deliberately run, so a refusal
# here would be wrong; the point is that an OPERATOR reading start output sees
# it immediately rather than two days later.
is_scratch_style_path() {
    local p="$1"
    [[ -z "$p" ]] && return 1
    local tmpdir="${TMPDIR:-}"
    tmpdir="${tmpdir%/}"
    [[ -n "$tmpdir" && "$p" == "$tmpdir"/* ]] && return 0
    case "$p" in
        /tmp/*|/private/tmp/*|/var/tmp/*|/var/folders/*|/private/var/folders/*) return 0 ;;
        *-checkout|*-checkout/*) return 0 ;;
        */.loom/worktrees/*) return 0 ;;
        *) return 1 ;;
    esac
}

warn_scratch_workdir_drift() {
    local offenders=() workspace="${LOOM_WORKSPACE:-}"
    is_scratch_style_path "$REPO_ROOT" && offenders+=("WorkingDirectory=$REPO_ROOT")
    if [[ -n "$workspace" && "$workspace" != "$REPO_ROOT" ]] && is_scratch_style_path "$workspace"; then
        offenders+=("LOOM_WORKSPACE=$workspace")
    fi
    [[ "${#offenders[@]}" -eq 0 ]] && return 0

    local o
    warn ""
    warn "WARNING: this daemon would run out of a SCRATCH / temporary directory (#6568):"
    for o in "${offenders[@]}"; do
        warn "  - $o"
    done
    warn "  A \$TMPDIR / *-checkout / worktree root is not durable daemon config: its contents"
    warn "  (and any watchdog, socket or pid path under it) vanish on reboot or cleanup, and"
    warn "  the daemon keeps reporting healthy the whole time. If this is a test fixture this"
    warn "  is expected; if this is a real host, the daemon is misconfigured -- restart it from"
    warn "  the machine checkout (see .loom/docs/troubleshooting.md)."
}

# ---------- autonomy env resolution (shared: inspection + real start, #6387) ----------
# Lifted VERBATIM out of the former inline block that sat BETWEEN the
# already-running guard and the plist/unit render, so both callers resolve
# byte-identical env:
#   * the pure-inspection short-circuit (--print-plist / --print-unit), which
#     #6387 moved ABOVE the already-running guard. It still needs every LOOM_*
#     var exported here, because render_launchd_plist / render_systemd_unit
#     harvest the PROCESS ENV to build EnvironmentVariables / Environment=.
#   * the real start path, at the exact position the block always occupied.
# Exactly one of the two runs per invocation (the inspection path exits).
#
# One deliberate difference between the two: under --print-plist/--print-unit
# the informational autonomy line goes to STDERR (see _autonomy_echo), so an
# inspection mode emits the plist/unit on stdout and NOTHING else -- which is
# what --help promises ("Print the LaunchAgent plist that WOULD be installed")
# and what every other advisory on that path (PATH drift, "Rendered plist
# PATH: ...", the autonomy-downgrade warning) already does. A real start is
# unchanged: the line still goes to stdout.
_autonomy_echo() {
    if [[ "$PRINT_PLIST" == "true" || "$PRINT_UNIT" == "true" ]]; then
        echo -e "$1" >&2
    else
        echo -e "$1"
    fi
}

resolve_autonomy_env() {
    # ---------- autonomous-mode env ----------
    # Precedence: an already-exported env var is always respected. Otherwise the
    # default is FLAGS-OFF (#3911) — a plain start is a reliability daemon with both
    # autonomous loops OFF, matching the ecosystem-wide opt-in / default-off contract
    # (LOOM_WORK_FINDER unset => off, LOOM_MAIN_HEALTH_GATE unset => off). Opt in with
    # --work-finder / --health-gate (force the var to 1), or pass --from-config to
    # leave both unset so .loom/config.json -> autonomous drives.
    #
    # --from-config COMPOSES with --work-finder/--health-gate/--no-work-finder/
    # --no-health-gate rather than ignoring them (#4353): --from-config alone still
    # leaves both vars unset for config to drive (byte-for-byte the pre-#4353
    # behavior — test case 6 asserts this stays green); pairing it with an
    # explicit --work-finder / --no-work-finder additionally FORCES that one var
    # (same env-var-wins-if-already-exported rule), while the loop with no
    # explicit flag is still left to config. So `--from-config --work-finder`
    # forces LOOM_WORK_FINDER=1 and leaves LOOM_MAIN_HEALTH_GATE unset.
    export LOOM_WORKSPACE="${LOOM_WORKSPACE:-$REPO_ROOT}"

    # ---------- guard-hook autonomy defaults (#3898) ----------
    # The daemon dispatches headless /loom:sweep children under
    # --dangerously-skip-permissions, where a guard ASK has no human to answer it
    # and therefore BLOCKS — a silent stall. So autonomous runs get two guard
    # defaults, both env-overridable (an already-exported value always wins):
    #   * LOOM_GUARD_DECISION_LOG=1 — capture every guard DENY/ASK to
    #     .loom/logs/guard-decisions.log so the standing per-trigger review policy
    #     (see CLAUDE.md → "Autonomous guard defaults") can dedup by pattern and
    #     file one issue per distinct trigger. Off by default outside autonomous
    #     mode; here we opt it on so the feedback loop actually has data.
    #   * LOOM_FORCE_SCOPE=protected — allow an agent to force-push / hard-reset its
    #     OWN working branch without a stall, while force-push to a protected branch
    #     (main/master/default) stays a hard DENY via ALWAYS_BLOCK_PATTERNS. This is
    #     the Loom-recommended force-scope for autonomous repos.
    # Children inherit these through the daemon's process environment. This is a
    # DELIBERATE, agent-wide (not per-invocation) export: there is no mechanism to
    # scope an env var to only the guard hook's own PreToolUse invocations without
    # also handing it to every OTHER subprocess the dispatched agent spawns —
    # `export`/`Command::env` inheritance is transitive to the whole child tree.
    #
    # KNOWN CONSEQUENCE (#5388): a dispatched agent that runs a *managed repo's
    # own* guard-hook test suite (one that asserts the guard's FACTORY-DEFAULT
    # force-push/reset-hard `ask` tier or decision-log-off behavior, e.g.
    # `hooks/repo/tests/test-guard-destructive.sh`) will see these two ambient
    # values override exactly the defaults under test — a clean shell run and a
    # dispatched-agent run of the identical suite, on the identical commit, can
    # disagree by dozens of failures. An agent that does not know its own
    # environment is non-default has no way to distinguish "main is broken" from
    # "my environment is lying to me" — this caused a Builder to close a valid
    # issue as a false "already resolved" duplicate. The Builder role brief
    # (defaults/roles/builder.md → "Build Verification") tells dispatched agents
    # these two vars may be set and gives the remedy:
    #   env -u LOOM_FORCE_SCOPE -u LOOM_GUARD_DECISION_LOG <test-suite-command>
    export LOOM_GUARD_DECISION_LOG="${LOOM_GUARD_DECISION_LOG:-1}"
    export LOOM_FORCE_SCOPE="${LOOM_FORCE_SCOPE:-protected}"

    local FORCED_DESC=() FORCED_JOINED=""
    if [[ "$FROM_CONFIG" == "true" ]]; then
        # Compose (#4353): --from-config alone leaves BOTH vars unset for config to
        # drive. An explicit --work-finder/--no-work-finder (or the health-gate
        # equivalent) additionally FORCES that one var -- using the
        # ${VAR:-default} form so an already-exported env var still wins over the
        # CLI flag, exactly like the non-config branch below. The loop with no
        # explicit flag is left untouched (stays unset, config drives it).
        if [[ "$WANT_WORK_FINDER" == "on" ]]; then
            export LOOM_WORK_FINDER="${LOOM_WORK_FINDER:-1}"
            FORCED_DESC+=("work_finder=${LOOM_WORK_FINDER}")
        elif [[ "$WANT_WORK_FINDER" == "off" ]]; then
            export LOOM_WORK_FINDER="${LOOM_WORK_FINDER:-0}"
            FORCED_DESC+=("work_finder=${LOOM_WORK_FINDER}")
        fi
        if [[ "$WANT_HEALTH_GATE" == "on" ]]; then
            export LOOM_MAIN_HEALTH_GATE="${LOOM_MAIN_HEALTH_GATE:-1}"
            FORCED_DESC+=("main_health_gate=${LOOM_MAIN_HEALTH_GATE}")
        elif [[ "$WANT_HEALTH_GATE" == "off" ]]; then
            export LOOM_MAIN_HEALTH_GATE="${LOOM_MAIN_HEALTH_GATE:-0}"
            FORCED_DESC+=("main_health_gate=${LOOM_MAIN_HEALTH_GATE}")
        fi
        if [[ "${#FORCED_DESC[@]}" -eq 0 ]]; then
            _autonomy_echo "${BOLD}Autonomous mode: driven by .loom/config.json -> autonomous (env not forced)${NC}"
        else
            FORCED_JOINED="$(IFS=', '; echo "${FORCED_DESC[*]}")"
            _autonomy_echo "${BOLD}Autonomous mode: config-driven; forced: ${FORCED_JOINED}${NC}"
        fi
    else
        # An already-exported env var always wins. Otherwise --work-finder /
        # --health-gate force the loop ON (=1); the default (flags off) forces it
        # OFF (=0), so a plain start is a reliability daemon that never auto-dispatches.
        if [[ "$WANT_WORK_FINDER" == "on" ]]; then
            export LOOM_WORK_FINDER="${LOOM_WORK_FINDER:-1}"
        else
            export LOOM_WORK_FINDER="${LOOM_WORK_FINDER:-0}"
        fi
        if [[ "$WANT_HEALTH_GATE" == "on" ]]; then
            export LOOM_MAIN_HEALTH_GATE="${LOOM_MAIN_HEALTH_GATE:-1}"
        else
            export LOOM_MAIN_HEALTH_GATE="${LOOM_MAIN_HEALTH_GATE:-0}"
        fi
        if [[ "$LOOM_WORK_FINDER" == "0" && "$LOOM_MAIN_HEALTH_GATE" == "0" ]]; then
            _autonomy_echo "${BOLD}Reliability daemon:${NC} work_finder=off main_health_gate=off (both loops OFF; opt in with --work-finder / --health-gate / --from-config)"
        else
            _autonomy_echo "${BOLD}Autonomous mode:${NC} work_finder=${LOOM_WORK_FINDER} main_health_gate=${LOOM_MAIN_HEALTH_GATE}"
        fi
    fi
}

# ---------- inspection-mode short-circuit body (--print-plist/--print-unit, #6387) ----------
# The rendering half of the two pure-inspection modes, factored into a function
# purely so the CALL SITE can sit as early as possible in the linear flow (see
# the call, immediately after --heal-watchdog-only and BEFORE the
# already-running guard). Everything it touches is read-only: it renders to a
# scratch tempfile it deletes, and only ever READS an installed plist/unit.
run_inspection_mode_and_exit() {
    # ---------- prior installed plist/unit (autonomy-downgrade check, #4693) ----------
    # The mechanism is decided by the INVOCATION, never by the host OS: these are
    # pure inspection modes that render (and inspect) their mechanism's file
    # regardless of the platform running them, exactly like the --print-plist
    # PATH-drift (#4172) and dropped-env-key (#4522) checks below, which read
    # $HOME/Library/LaunchAgents/<label>.plist unconditionally. That argv-only
    # decision is also what lets this whole block run before platform detection
    # -- and therefore before the already-running guard (#6387).
    if [[ "$PRINT_PLIST" == "true" ]]; then
        PRIOR_AUTONOMY_MECH="launchd"
        PRIOR_AUTONOMY_FILE="$HOME/Library/LaunchAgents/$(resolve_launchd_label).plist"
        PRIOR_AUTONOMY_EXTRACTOR="extract_plist_env_value"
    else
        PRIOR_AUTONOMY_MECH="systemd"
        PRIOR_AUTONOMY_FILE=""
        PRIOR_AUTONOMY_EXTRACTOR="extract_systemd_env_value"
        if declare -f resolve_systemd_unit_path >/dev/null 2>&1; then
            PRIOR_AUTONOMY_FILE="$(resolve_systemd_unit_path 2>/dev/null || true)"
        fi
    fi

    # Run BEFORE the render below, so an operator sees the warning whether they
    # are just inspecting or actually starting. Warn-only here by construction
    # (see the $PRINT_PLIST/$PRINT_UNIT guard inside warn_autonomy_downgrade).
    warn_autonomy_downgrade
    # #6568: same treatment, same reason -- the session-isolation guard and the
    # scratch-workdir drift warning both degrade to stderr advisories here, so
    # --print-plist/--print-unit keep their "stdout is the plist/unit, exit 0,
    # no side effects" contract exactly as before. Drift warning FIRST so it is
    # still emitted on the real-start path, where the guard exits.
    warn_scratch_workdir_drift
    guard_session_context_start

    # ---------- --print-plist: pure inspection, no side effects ----------
    if [[ "$PRINT_PLIST" == "true" ]]; then
        local _plist_rendered _plist_print_tmp _live_plist _live_path
        _plist_rendered="$(render_launchd_plist "$(resolve_launchd_label)" "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG")"
        # Render to a scratch file (never printed directly) so the dropped-env-key
        # merge (#5344) below can carry forward any installed-but-missing key
        # BEFORE printing -- the preview must match what a real install would
        # actually write, not the pre-merge render.
        _plist_print_tmp="$(mktemp "${TMPDIR:-/tmp}/loom-print-plist.XXXXXX")"
        printf '%s\n' "$_plist_rendered" > "$_plist_print_tmp"
        # PATH-drift check (#4172): if a live plist is already installed for this
        # label, compare its PATH against the one just rendered and warn (stderr
        # only -- READ-ONLY, no side effect) when they differ. This is what makes
        # a PATH change from the live plist visible at inspection/roll time
        # instead of silently swapping it out on the next real start/relaunch.
        _live_plist="$HOME/Library/LaunchAgents/$(resolve_launchd_label).plist"
        if [[ -f "$_live_plist" ]]; then
            _live_path="$(extract_plist_path_value "$_live_plist" 2>/dev/null || true)"
            if [[ -n "$_live_path" && "$_live_path" != "$PLIST_PATH_VALUE" ]]; then
                {
                    echo ""
                    echo "PATH DRIFT DETECTED vs the installed plist ($_live_plist):"
                    echo "- live: $_live_path"
                    echo "+ new:  $PLIST_PATH_VALUE"
                } >&2
            fi
            # Dropped-env-key check (#4522, merge #5344): read-only inspection
            # counterpart of the same check the real install path below runs
            # before overwriting -- carries dropped keys forward into
            # $_plist_print_tmp in place (unless --force-env).
            warn_dropped_env_keys "$_live_plist" "$_plist_print_tmp" extract_plist_env_keys extract_plist_env_value inject_one_plist_env_entry
        fi
        cat "$_plist_print_tmp"
        rm -f "$_plist_print_tmp"
        exit 0
    fi

    # ---------- --print-unit: pure inspection, no side effects (#4268) ----------
    local _unit_rendered _unit_print_tmp _live_unit
    _unit_rendered="$(render_systemd_unit "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG")"
    # Render to a scratch file (never printed directly) so the dropped-env-key
    # merge (#5344) below can carry forward any installed-but-missing key
    # BEFORE printing -- see the --print-plist rationale above.
    _unit_print_tmp="$(mktemp "${TMPDIR:-/tmp}/loom-print-unit.XXXXXX")"
    printf '%s\n' "$_unit_rendered" > "$_unit_print_tmp"
    # Dropped-env-key check (#4522, merge #5344): read-only inspection
    # counterpart of the same check the real install path below runs before
    # overwriting -- carries dropped keys forward into $_unit_print_tmp in
    # place (unless --force-env).
    _live_unit="$PRIOR_AUTONOMY_FILE"
    if [[ -n "$_live_unit" && -f "$_live_unit" ]]; then
        warn_dropped_env_keys "$_live_unit" "$_unit_print_tmp" extract_systemd_env_keys extract_systemd_env_value inject_one_systemd_env_entry
    fi
    cat "$_unit_print_tmp"
    rm -f "$_unit_print_tmp"
    exit 0
}

# render_launchd_plist <label> <daemon_bin> <workdir> <log_path>
# Prints the LaunchAgent plist XML to stdout. Mirrors the hand-written plist
# that validated the #3972 fix during the incident
# (~/Library/LaunchAgents/com.rjwalters.loom-daemon.plist): RunAtLoad=true
# (the daemon also comes back after a reboot/re-login, not just a session
# death -- strictly more durable than the pre-#3972 nohup contract, which
# didn't survive a reboot either).
#
# KeepAlive is `{ SuccessfulExit: true }` as of the supervised restart primitive
# (#4054, Phase 2 of #4017): launchd relaunches the job ONLY when it exits with
# status 0, and leaves it down on any non-zero exit. This is what lets the
# daemon END and reliably COME BACK on demand -- the `RestartDaemon` IPC request
# (loom-daemon restart) is the ONLY path that exits 0, so it is the only thing
# that trips a relaunch. Crucially this PRESERVES the old no-crash-loop semantics
# of KeepAlive=false: a crashed/panicked daemon, a SIGTERM'd operator stop (exit
# 143), and a SIGINT/Ctrl-C (exit 130) all exit NON-ZERO, so launchd does NOT
# respawn them. Making the exit code carry intent (daemon side, #4054) is also
# what closes the SuccessfulExit/bootout race (Curator Finding 1): an operator
# stop exits non-zero, so launchd never relaunches it during the stop window --
# "an operator stop stays stopped" no longer depends on bootout timing. The
# bootout in loom-daemon-stop.sh is demoted to belt-and-braces (it still unloads
# the definition so it does not come back at the next login).
#
# LOOM_DAEMON_SUPERVISOR=launchd is baked into the plist env so the daemon can
# PROVE it is supervised before it will exit for a restart. It is hardcoded here
# (not harvested from the caller's env) so it lands in EVERY rendered plist --
# and, conversely, is ABSENT from the nohup path (which never renders a plist),
# so an unsupervised daemon correctly refuses to exit on a restart request
# (nothing would bring it back). Because it survives in the plist, the relaunched
# daemon still sees it.
#
# The PATH baked into the plist is DETERMINISTIC (#4172), not derived from the
# invoking shell's PATH. It used to be "$PATH:<fallback>" -- the invoking
# shell/session's ENTIRE interactive PATH prefixed onto a fallback set -- so a
# re-render (e.g. a `loom-daemon-update.sh --relaunch` run from an interactive
# terminal with a large project-specific PATH) silently replaced whatever PATH
# the live plist carried with whoever's shell happened to run the roll:
# non-hermetic, non-reproducible across hosts/sessions, and able to let an
# unrelated toolchain earlier in that PATH shadow the binaries the daemon and
# its sweep children expect (gh/git/cargo/python3). resolve_plist_path() (see
# above) instead resolves, in order: an explicit LOOM_DAEMON_PATH override
# (verbatim), LOOM_DAEMON_PATH_EXTRA prepended onto the canonical minimal PATH,
# or the canonical minimal PATH alone (~/.local/bin, ~/.cargo/bin, Homebrew,
# standard bin dirs -- the same fallback set this always carried, just no
# longer prefixed with the invoking shell's PATH). It is computed exactly once
# per script invocation into $PLIST_PATH_VALUE and logs its choice to stderr.
# Every already-exported LOOM_* / GH_TOKEN / GITEA_TOKEN / FORGE_TOKEN var is
# still forwarded verbatim so the launchd job sees EXACTLY the autonomy flags
# and auth this invocation resolved -- never wider, never narrower (#3972 AC:
# "preserves the current flag semantics") -- with ONE carve-out (#6568): the
# per-invocation AGENT SESSION keys (LOOM_SWEEP_*, LOOM_TERMINAL_ID, LOOM_ROLE,
# LOOM_RUNTIME, see is_session_scoped_env_key) are ALWAYS dropped. They describe
# one sweep/role invocation, never durable daemon config; forwarding them is
# what put LOOM_SWEEP_CLAIM_OWNED=6388 / LOOM_ROLE=sweep-lifecycle into the
# production plist on both operator Macs for two days.
#
# Reconciling this STATIC forwarding with the #4430 MINTED GitHub App token
# path (deliberate, not an oversight): `LOOM_GITHUB_APP_ID` /
# `LOOM_GITHUB_APP_KEY_PATH` already match the `LOOM_[A-Za-z0-9_]*` pattern
# above, so they ride along into the plist exactly like any other LOOM_* flag
# -- but note that's a non-secret app id and a *path* to the private key, never
# the key material itself (which stays on disk wherever the operator put it,
# read only by openssl at mint time). Any GH_TOKEN forwarded here is this
# invocation's snapshot at RENDER time; the daemon's own #4430 preflight/
# refresh loop calls `std::env::set_var("GH_TOKEN", …)` on its OWN process
# environment once a fresh installation token is minted, which the plist's
# static value cannot see or fight (it only seeds the daemon's env at
# process start, same as it always did) -- every `gh`/`git` child spawned
# AFTER that point inherits the live, minted value, not the stale plist one.
# If minting ever fails (revoked/unreadable key, network hiccup), the daemon
# falls back to whatever GH_TOKEN this static forwarding already provided --
# so leaving GH_TOKEN forwarding in place is exactly the right fallback
# layer, not a footgun to remove.
render_launchd_plist() {
    local label="$1" bin="$2" workdir="$3" log_path="$4"
    local plist_path_value="$PLIST_PATH_VALUE"

    local env_entries=""
    env_entries+="        <key>PATH</key>\n        <string>$(xml_escape "$plist_path_value")</string>\n"
    env_entries+="        <key>HOME</key>\n        <string>$(xml_escape "$HOME")</string>\n"
    # Mark the daemon as launchd-supervised so its RestartDaemon handler (#4054)
    # will exit 0 for a supervised relaunch. Hardcoded (not env-harvested) so it
    # is present in every rendered plist and its relaunch, and never leaks to the
    # unsupervised nohup path.
    env_entries+="        <key>LOOM_DAEMON_SUPERVISOR</key>\n        <string>launchd</string>\n"

    local line key value
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        key="${line%%=*}"
        value="${line#*=}"
        # Never duplicate the supervisor key hardcoded above (a caller that
        # exported LOOM_DAEMON_SUPERVISOR must not produce two plist entries).
        [[ "$key" == "LOOM_DAEMON_SUPERVISOR" ]] && continue
        # #6568: agent-session keys are per-invocation, never durable daemon
        # config. Dropped unconditionally -- including for an operator
        # invocation that merely happens to have them exported (a `loom start`
        # typed inside a Claude Code session inherits all of them).
        is_session_scoped_env_key "$key" && continue
        env_entries+="        <key>$(xml_escape "$key")</key>\n        <string>$(xml_escape "$value")</string>\n"
    done < <(env | grep -E '^(LOOM_[A-Za-z0-9_]*|GH_TOKEN|GITEA_TOKEN|FORGE_TOKEN)=' || true)

    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0">\n<dict>\n'
    printf '    <key>Label</key>\n    <string>%s</string>\n' "$(xml_escape "$label")"
    printf '    <key>ProgramArguments</key>\n    <array>\n        <string>%s</string>\n    </array>\n' "$(xml_escape "$bin")"
    printf '    <key>WorkingDirectory</key>\n    <string>%s</string>\n' "$(xml_escape "$workdir")"
    printf '    <key>EnvironmentVariables</key>\n    <dict>\n'
    printf '%b' "$env_entries"
    printf '    </dict>\n'
    printf '    <key>RunAtLoad</key>\n    <true/>\n'
    # KeepAlive:SuccessfulExit=true (#4054): relaunch ONLY on a clean exit 0 (the
    # RestartDaemon primitive). A crash/SIGTERM/SIGINT exits non-zero and is NOT
    # respawned -- preserving the pre-#4054 no-crash-loop semantics of KeepAlive=false.
    # #4862 NOTE: launchd's KeepAlive:{SuccessfulExit:true} has the SAME "was the
    # exit clean" dependency as systemd's Restart=on-success (see
    # render_systemd_unit's KillMode=mixed fix above), but launchd has no
    # documented cgroup-timeout reclassification of a clean exit into a
    # failure -- there is no launchd analog of systemd's kill(5) Result=timeout
    # escalation. Not reproduced/fixed here (#4862 scoped its systemd-only
    # incident); if a launchd analog ever surfaces, audit whether lingering
    # `claude`/`tee`/`sleep` children under this job's ProcessType=Background
    # can flip SuccessfulExit's observed exit status before filing a follow-up.
    printf '    <key>KeepAlive</key>\n    <dict>\n        <key>SuccessfulExit</key>\n        <true/>\n    </dict>\n'
    printf '    <key>ProcessType</key>\n    <string>Background</string>\n'
    printf '    <key>StandardOutPath</key>\n    <string>%s</string>\n' "$(xml_escape "$log_path")"
    printf '    <key>StandardErrorPath</key>\n    <string>%s</string>\n' "$(xml_escape "$log_path")"
    printf '</dict>\n</plist>\n'
}

# ---------- systemd --user unit rendering (#4268) ----------
# render_systemd_unit <daemon_bin> <workdir> <log_path>
# Prints the `systemd --user` service unit to stdout. Pure string rendering --
# safe to call on ANY platform (used by --print-unit for inspection/testing); the
# `systemctl --user` invocation that consumes it is gated to a systemd Linux host
# separately, below. This is the Linux mirror of render_launchd_plist:
#
#   * Restart=on-success is the exact analog of the launchd
#     KeepAlive:{SuccessfulExit:true} contract (#4054): systemd relaunches the
#     service ONLY when it exits with status 0 (the RestartDaemon primitive), and
#     leaves it down on any non-zero exit -- a crash/panic, a SIGTERM operator
#     stop (143), a SIGINT/Ctrl-C (130) -- so it preserves the no-crash-loop
#     semantics while making the one deliberate clean exit the only relaunch
#     trigger. Crash relaunch (Restart=always/on-failure) is deliberately NOT set
#     here -- that is watchdog territory (sub-issue D of #4260).
#   * KillMode=mixed (#4862): a self-update relaunch calls exit(0) while the
#     daemon's own `claude`/`tee`/`sleep` worker children (spawned sweeps, in
#     the SAME cgroup) may still be running. Under the default KillMode=
#     control-group, systemd's kill(5) escalates to SIGKILLing those leftover
#     processes only after the FULL TimeoutStopSec deadline elapses -- and a
#     forced-timeout SIGKILL sets the UNIT's Result to 'timeout', which
#     Restart=on-success does NOT match (only 'success' does -- see the
#     Restart= table in systemd.service(5)), so the relaunch never fires and
#     the daemon sits dead. Empirically verified (see #4862): a clean exit(0)
#     with lingering cgroup children reproduces Result=timeout under
#     control-group and Result=success (Restart=on-success DOES fire) under
#     mixed. Per kill(5): "If set to mixed, the SIGTERM signal is sent to the
#     main process while the subsequent SIGKILL signal is sent to all
#     remaining processes... after: the main process of a unit has exited
#     (applies to KillMode=: mixed)" -- i.e. mixed escalates to SIGKILL
#     IMMEDIATELY on the main process's own exit, never waiting out
#     TimeoutStopSec, so the unit's Result tracks the main process's own exit
#     status. This does not change genuine-crash semantics (still Result=
#     exit-code / signal, still refused by on-success) -- verified with both
#     shapes in test-loom-daemon-start.sh.
#   * TimeoutStopSec=20 (#4950): a fast-failure backstop well below systemd's
#     90s default -- see the printf site below for the full sizing rationale
#     (both the RestartDaemon primitive and the operator-stop SIGTERM handler
#     exit near-instantly, so a healthy daemon never approaches 20s). Without
#     this, a stop-transition that DOES stall (e.g. a stale unit predating
#     KillMode=mixed above, still lingering on an already-provisioned host)
#     drags out the default 90s before landing the unit in `failed (Result:
#     timeout)` -- the exact 2026-08-02 incident `loom-daemon-update.sh`'s
#     #4950 restart-verification poll now detects and self-heals.
#   * SuccessExitStatus=143 130 + RestartPreventExitStatus=143 130 (#6129): a
#     clean operator stop (SIGTERM->143, SIGINT->130, see ipc.rs's
#     EXIT_SIGTERM/EXIT_SIGINT) was landing the unit in `failed`, not
#     `inactive` -- see the printf site below for the full classification +
#     safety rationale.
#   * [Install] WantedBy=default.target + `systemctl --user enable` is the
#     RunAtLoad=true analog: the service comes up on login (and, with
#     `loginctl enable-linger`, after a reboot).
#   * LOOM_DAEMON_SUPERVISOR=systemd is baked in (hardcoded, not env-harvested) so
#     the daemon can PROVE it is supervised before it exits for a restart (#4054,
#     recognized daemon-side by detect_supervisor() since PR #4298 / #4267) -- and,
#     conversely, is ABSENT from the nohup path, so an unsupervised daemon
#     correctly refuses to exit on a restart request.
#   * The PATH baked in is the SAME deterministic value as the launchd plist
#     (#4172, $PLIST_PATH_VALUE), not the invoking shell's PATH; every already-
#     exported LOOM_* / GH_TOKEN / GITEA_TOKEN / FORGE_TOKEN var is forwarded
#     verbatim so the service sees EXACTLY the autonomy flags + auth this
#     invocation resolved -- never wider, never narrower, minus the #6568
#     agent-session strip the launchd renderer applies too. See
#     render_launchd_plist's #4430 reconciliation note above -- this static
#     forwarding and the daemon's own minted-GitHub-App-token refresh loop
#     are complementary (static = render-time seed/fallback, minted = live
#     process-env override), never in conflict.
render_systemd_unit() {
    local bin="$1" workdir="$2" log_path="$3"
    local unit_path_value="$PLIST_PATH_VALUE"

    local env_lines=""
    env_lines+="Environment=PATH=${unit_path_value}\n"
    env_lines+="Environment=HOME=${HOME}\n"
    # Mark the daemon as systemd-supervised so its RestartDaemon handler (#4054)
    # will exit 0 for a supervised relaunch. Hardcoded (not env-harvested) so it
    # is present in every rendered unit and never leaks to the nohup path.
    env_lines+="Environment=LOOM_DAEMON_SUPERVISOR=systemd\n"

    local line key
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        key="${line%%=*}"
        # Never duplicate the supervisor key hardcoded above.
        [[ "$key" == "LOOM_DAEMON_SUPERVISOR" ]] && continue
        # #6568: same agent-session strip the launchd renderer applies -- the
        # systemd tier laundered the identical keys into Environment= lines.
        is_session_scoped_env_key "$key" && continue
        env_lines+="Environment=${line}\n"
    done < <(env | grep -E '^(LOOM_[A-Za-z0-9_]*|GH_TOKEN|GITEA_TOKEN|FORGE_TOKEN)=' || true)

    printf '[Unit]\n'
    printf 'Description=Loom autonomous daemon (loom-daemon)\n'
    printf 'After=network-online.target\n'
    printf 'Wants=network-online.target\n'
    printf '\n'
    printf '[Service]\n'
    printf 'Type=simple\n'
    printf 'WorkingDirectory=%s\n' "$workdir"
    printf 'ExecStart=%s\n' "$bin"
    # Restart=on-success == launchd KeepAlive:{SuccessfulExit:true} (#4054): only a
    # clean exit 0 (the RestartDaemon primitive) trips a relaunch; a crash / an
    # operator SIGTERM/SIGINT exits non-zero and stays down.
    printf 'Restart=on-success\n'
    # KillMode=mixed (#4862): see the render_systemd_unit doc comment above for
    # the full kill(5)-sourced rationale -- without this, a clean exit(0) with
    # lingering `claude`/`tee`/`sleep` worker children in the cgroup gets
    # reclassified as Result=timeout (control-group's default forced-SIGKILL-
    # after-TimeoutStopSec path) and Restart=on-success never fires.
    printf 'KillMode=mixed\n'
    # TimeoutStopSec=20 (#4950): bounds the unit's own stop-transition wait
    # well below systemd's 90s default. Both the RestartDaemon primitive
    # (#4054, exit(0) synchronously after the IPC ack) and the operator-stop
    # SIGTERM handler (#3813, exit(143) right after removing the socket) exit
    # near-instantly with no blocking drain -- 20s is a generous multiple of
    # that worst case, not a tight fit -- so a HEALTHY daemon never brushes
    # this ceiling. It exists purely as a fast-failure backstop: if a future
    # regression reintroduces a slow/blocking shutdown path (or a stale,
    # not-yet-re-rendered unit predating KillMode=mixed above leaves lingering
    # cgroup children), the unit fails fast at 20s instead of dragging out the
    # full 90s default before `loom-daemon-update.sh`'s #4950 verification
    # poll (LOOM_DAEMON_RESTART_POLL_SECS, default 30s) even has a chance to
    # observe the failure and self-heal via `systemctl --user reset-failed &&
    # start`.
    printf 'TimeoutStopSec=20\n'
    # SuccessExitStatus=143 130 + RestartPreventExitStatus=143 130 (issue
    # #6129): a clean operator `systemctl --user stop loom-daemon` was landing
    # the unit in `failed` (not `inactive`), because `EXIT_SIGTERM`/
    # `EXIT_SHUTDOWN` (143) and `EXIT_SIGINT` (130, see ipc.rs) are non-zero
    # and Type=simple's default "clean exit" criterion is exit(0) or one of
    # SIGHUP/SIGINT/SIGTERM/SIGPIPE *terminating the process by signal* --
    # neither matches a `std::process::exit(143)` *exit code* (as opposed to
    # dying BY the signal), so systemd classified it Result=exit-code and
    # ActiveState=failed even though nothing crashed. Listing 143/130 in
    # SuccessExitStatus= fixes the classification (Result=success,
    # ActiveState=inactive) for both codes.
    #
    # This is deliberately paired with RestartPreventExitStatus=143 130,
    # NOT left to rely on SuccessExitStatus= alone, even though
    # systemd.service(5)'s own Restart= semantics already say "the death of
    # the process is a result of systemd operation (e.g. service stop or
    # restart)" is never restarted, regardless of exit status -- which is
    # true for the supported path (this repo's own loom-daemon-stop.sh always
    # goes through `systemctl --user disable --now`, a systemd-tracked stop).
    # But SuccessExitStatus= widens what counts as "a clean exit" for
    # Restart=on-success too: without the belt-and-braces
    # RestartPreventExitStatus= entry, an operator who bypasses the script
    # and sends a raw `kill -TERM <pid>` directly (not through systemctl) --
    # untracked by systemd as an intentional stop -- would newly get an
    # AUTOMATIC RELAUNCH after that raw kill, the opposite of "operator stop
    # stays stopped" (#4054 Curator Finding 1). RestartPreventExitStatus=
    # vetoes a restart on these codes unconditionally, independent of how
    # the process died, closing that gap regardless of which door the
    # operator used.
    printf 'SuccessExitStatus=143 130\n'
    printf 'RestartPreventExitStatus=143 130\n'
    printf '%b' "$env_lines"
    printf 'StandardOutput=append:%s\n' "$log_path"
    printf 'StandardError=append:%s\n' "$log_path"
    printf '\n'
    printf '[Install]\n'
    printf 'WantedBy=default.target\n'
}

# ---------- autonomy-desired intent marker (#4011) ----------
# Write the durable "a daemon is EXPECTED to be running on this host" marker on a
# successful start. Its LIFETIME is operator intent, NOT process liveness: only
# an operator-initiated loom-daemon-stop.sh removes it, and it is deliberately
# PRESERVED across the internal stop loom-daemon-update.sh performs (via
# LOOM_DAEMON_STOP_KEEP_INTENT). The host-side watchdog (loom-daemon-watchdog.sh)
# reads it to decide whether a missing daemon is a silent failure (marker present
# ⇒ report) or a deliberate stop (marker absent ⇒ stay silent). Records the paths
# and label the watchdog needs so it can probe reality without re-deriving them.
# Args: <use_launchd true|false> <launchd_label> [use_systemd true|false] [systemd_unit]
# #4862: use_systemd/systemd_unit are new, OPTIONAL trailing fields (default
# false/"") so the watchdog can tell a systemd-supervised daemon apart from the
# plain-nohup fallback -- both previously wrote identical `use_launchd=false`
# markers, leaving the watchdog with no way to probe `systemctl --user` for the
# #4232-style bounded auto-remediation gate (see loom-daemon-watchdog.sh).
#
# work_finder/health_gate (#5437): persist THIS invocation's actual resolved
# LOOM_WORK_FINDER / LOOM_MAIN_HEALTH_GATE values (both are exported, one way
# or another, by every code path above this function -- see the autonomy-flag
# resolution block preceding "persist invocation flags"). This is the only
# durable record of "was the daemon most recently started autonomously?" on
# the nohup fallback tier, which never renders a plist/unit for
# check_autonomy_downgrade_key() to read back (see PRIOR_AUTONOMY_FILE below,
# always empty on that tier). Without it, that check had no way to tell a
# PRIOR bare (FLAGS-OFF) start apart from a PRIOR autonomous one -- both left
# an identical "marker present, no readable prior value" signal -- so EVERY
# bare restart following ANY prior start looked like a downgrade.
write_intent_marker() {
    local use_launchd="$1" label="$2" use_systemd="${3:-false}" systemd_unit="${4:-}"
    mkdir -p "$LOOM_DIR" 2>/dev/null || true
    (
        umask 077
        cat > "$INTENT_MARKER" <<EOF
# loom autonomy-desired marker (issue #4011)
# Presence ⇒ a loom-daemon is EXPECTED to be running on this host. Written by
# loom-daemon-start.sh on a successful start; removed ONLY by an
# operator-initiated loom-daemon-stop.sh (preserved across update.sh restarts).
# Do not hand-edit — delete via loom-daemon-stop.sh so the watchdog stays quiet.
started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
repo_root=$REPO_ROOT
pid_file=$PID_FILE
heartbeat_file=$HEARTBEAT_FILE
heartbeat_interval_secs=$HEARTBEAT_INTERVAL_SECS
use_launchd=$use_launchd
launchd_label=$label
use_systemd=$use_systemd
systemd_unit=$systemd_unit
socket_path=$SOCKET_PATH
work_finder=${LOOM_WORK_FINDER:-}
health_gate=${LOOM_MAIN_HEALTH_GATE:-}
EOF
    )
}

# ---------- safehouse fleet-comms status (#4345) ----------
# Reuses the same env>config>default resolvers `mcp-config.sh` already defines
# for the safehouse-mcp worker injection (phase 2, #3999) — this is a purely
# static, PRE-CONNECT check: "would the daemon even try?" It can only report
# "not configured" vs "configured", never "connected" (proving a live
# connection needs the daemon's own socket, surfaced instead by
# `loom-daemon status` --- see .loom/docs/safehouse.md "New-host onboarding").
_LOOM_MCP_CONFIG_LIB="$_LOOM_LAUNCHD_LIB_DIR/mcp-config.sh"
if [[ -r "$_LOOM_MCP_CONFIG_LIB" ]]; then
    # shellcheck source=../lib/mcp-config.sh
    source "$_LOOM_MCP_CONFIG_LIB"
fi
print_safehouse_status() {
    if ! command -v loom_mcp_safehouse_enabled >/dev/null 2>&1; then
        return 0 # mcp-config.sh missing (stale/partial install) — skip silently
    fi
    local enabled socket
    enabled=$(loom_mcp_safehouse_enabled "$REPO_ROOT")
    if [[ "$enabled" != "true" ]]; then
        echo "Safehouse:     not configured (safehouse.enabled is false/absent)"
        return 0
    fi
    socket=$(loom_mcp_safehouse_socket "$REPO_ROOT")
    if [[ -z "$socket" ]]; then
        warn "Safehouse:     configured, unreachable (enabled but no socket path resolved -- set" \
             "safehouse.socket, \$LOOM_SAFEHOUSE_SOCKET, or \$SAFEHOUSED_SOCKET)"
        return 0
    fi
    if [[ -S "$socket" ]]; then
        ok "Safehouse:     configured (socket present at $socket) -- see 'loom-daemon status' for live connection state"
        # #4464: omitting safehouse.room is only valid when safehoused joined
        # exactly ONE room; on a multi-room host it makes safehoused reject
        # every send ('room' required) -- which silently kills narration and
        # peer-claim dedup, and 'loom-daemon status' will show
        # "connected, sends rejected: ...". Surface the caveat statically here.
        # #4225: attention-class routing can supply the room instead, via
        # safehouse.rooms.signal -- a host that set it needs no scalar
        # safehouse.room (the resolver falls back from one to the other), so the
        # caveat must not fire for it.
        local room signal
        room=${LOOM_SAFEHOUSE_ROOM:-$(loom_config_get "$REPO_ROOT" "safehouse.room" "" 2>/dev/null || true)}
        signal=${LOOM_SAFEHOUSE_ROOM_SIGNAL:-$(loom_config_get "$REPO_ROOT" "safehouse.rooms.signal" "" 2>/dev/null || true)}
        if [[ -z "$room" && -z "$signal" ]]; then
            echo "               note: safehouse.room is unset (and so is safehouse.rooms.signal) -- valid only if" \
                 "safehoused joined exactly one room; a multi-room host needs an explicit room id or every send is rejected"
        fi
    else
        warn "Safehouse:     configured, unreachable (socket $socket does not exist -- is safehoused running?)"
    fi
}

# ---------- calibrate binding-ceiling hint (#4390, re-based on #4512) ----------
# `loom-daemon calibrate` is purely file/host-based (no running daemon
# required, unlike `status`), so it is safe to run right here at start time.
# One advisory line, printed only when `autonomous.workFinder.maxConcurrent` is
# CURRENTLY the binding term AND the host is measurably idle -- i.e. this machine
# is under-subscribed at its current knob.
#
# #4512 changed the basis: calibrate no longer computes a *recommended* value
# (the CPU-headroom term it derived one from is gone; maxConcurrent is now a
# per-machine knob tuned empirically), so the hint reports the observed idle
# fraction instead of a number to copy.
# Never fatal: a missing jq, a calibrate error, or an unparseable payload all
# fall through silently -- this is advisory-only, exactly like
# print_safehouse_status above.
#
# BOUNDED (#4799): `calibrate` is normally file/host-based and fast (see
# above), but a `$DAEMON_BIN` with no `calibrate` handler at all -- a test
# fixture stub, or a future breaking CLI change -- makes the `$(...)` below
# block forever, exactly like the #4773 leak incident this call reproduced
# verbatim under the #4790 judge's hard-kill repro. Worse, a signal arriving
# while THIS script is blocked inside that command substitution is deferred
# until the substitution returns -- which for a truly-wedged child never
# happens -- so even loom-daemon-start.sh's own EXIT/INT/TERM traps cannot
# fire in that state. bounded_run() (lib/bounded-run.sh, shared with
# loom-daemon-watchdog.sh's IPC probe, #4398) guarantees the substitution
# always returns, closing that gap. If the lib failed to source for any
# reason, `bounded_run` is simply undefined and the `|| return 0` below
# degrades this hint to a silent no-op -- never a hang.
CALIBRATE_HINT_TIMEOUT_SECS="${LOOM_CALIBRATE_HINT_TIMEOUT_SECS:-5}"
[[ "$CALIBRATE_HINT_TIMEOUT_SECS" =~ ^[0-9]+$ ]] || CALIBRATE_HINT_TIMEOUT_SECS=5
print_calibrate_hint() {
    if ! command -v jq >/dev/null 2>&1; then
        return 0
    fi
    local calib_json
    calib_json="$(bounded_run "$CALIBRATE_HINT_TIMEOUT_SECS" "$DAEMON_BIN" calibrate --workspace "$REPO_ROOT" --json 2>/dev/null)" || return 0
    [[ -n "$calib_json" ]] || return 0

    local binding ceiling idle idle_pct
    binding=$(jq -r '.binding_term // empty' <<<"$calib_json" 2>/dev/null)
    [[ "$binding" == "ceiling" ]] || return 0

    ceiling=$(jq -r '.measurements.configured_max_concurrent // empty' <<<"$calib_json" 2>/dev/null)
    # Integer percent so the comparison below is plain shell arithmetic; `null`
    # (no idle sample on this host yet) yields an empty string and we bail.
    idle=$(jq -r '.measurements.cpu_idle_fraction // empty' <<<"$calib_json" 2>/dev/null)
    [[ -n "$idle" ]] || return 0
    idle_pct=$(jq -rn --argjson f "$idle" '($f * 100) | floor' 2>/dev/null) || return 0

    # Defensively require plain non-negative integers before shell arithmetic.
    [[ "$ceiling" =~ ^[0-9]+$ && "$idle_pct" =~ ^[0-9]+$ ]] || return 0
    (( ceiling > 0 )) || return 0

    # 50% idle mirrors calibrate::IDLE_HEADROOM_FRACTION -- the "grossly
    # under-subscribed" bar (#4512's motivating host measured 95% idle at cap 2).
    if (( idle_pct >= 50 )); then
        warn "maxConcurrent ${ceiling} binds while the host is ${idle_pct}% idle -- consider raising autonomous.workFinder.maxConcurrent ('loom-daemon calibrate' for the full reading)"
    fi
}

# ---------- watchdog LaunchAgent / systemd timer (#4011, #4260 sub-issue D) ----------
# The watchdog is the payload of a SECOND, SEPARATE scheduled job from the
# daemon job/unit itself, and reports when intent (the marker above) diverges
# from reality (daemon not loaded/alive, or heartbeat stale):
#   - Darwin: a launchd job on a StartInterval cadence. StartInterval, NOT
#     KeepAlive: a KeepAlive'd short-lived job would busy-loop, whereas
#     StartInterval already re-runs it every interval regardless of how the
#     last run exited.
#   - systemd Linux: a `Type=oneshot` service driven by a `.timer` unit
#     (`OnUnitActiveSec`). The systemd equivalent of StartInterval — a timer
#     re-fires the oneshot service every interval independent of the last run's
#     exit status.
# Both mechanisms share the same property: the watchdog job owns NO long-lived
# process, so it structurally cannot crash-and-stay-dead (the
# who-watches-the-watchdog resolution).
resolve_watchdog_label() {
    echo "${LOOM_WATCHDOG_LABEL:-$(resolve_launchd_label)-watchdog}"
}

# Locate the installed watchdog script (installed copy first, then the defaults/
# copy for a Loom source checkout that has not yet synced), mirroring the daemon
# binary/script resolution elsewhere.
locate_watchdog_script() {
    local candidate
    for candidate in \
        "$REPO_ROOT/.loom/scripts/cli/loom-daemon-watchdog.sh" \
        "$REPO_ROOT/defaults/scripts/cli/loom-daemon-watchdog.sh"; do
        if [[ -f "$candidate" ]]; then echo "$candidate"; return 0; fi
    done
    echo ""
}

# render_watchdog_plist <label> <watchdog_script> <workdir> <log_path> <interval_secs>
# Uses the SAME deterministic PATH as render_launchd_plist (#4172) -- see the
# resolve_plist_path() comment above render_launchd_plist for the rationale.
render_watchdog_plist() {
    local label="$1" script="$2" workdir="$3" log_path="$4" interval="$5"
    local plist_path_value="$PLIST_PATH_VALUE"
    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0">\n<dict>\n'
    printf '    <key>Label</key>\n    <string>%s</string>\n' "$(xml_escape "$label")"
    printf '    <key>ProgramArguments</key>\n    <array>\n        <string>/bin/bash</string>\n        <string>%s</string>\n    </array>\n' "$(xml_escape "$script")"
    printf '    <key>WorkingDirectory</key>\n    <string>%s</string>\n' "$(xml_escape "$workdir")"
    printf '    <key>EnvironmentVariables</key>\n    <dict>\n'
    printf '        <key>PATH</key>\n        <string>%s</string>\n' "$(xml_escape "$plist_path_value")"
    printf '        <key>HOME</key>\n        <string>%s</string>\n' "$(xml_escape "$HOME")"
    printf '        <key>LOOM_AUTONOMY_MARKER</key>\n        <string>%s</string>\n' "$(xml_escape "$INTENT_MARKER")"
    printf '        <key>LOOM_SOCKET_PATH</key>\n        <string>%s</string>\n' "$(xml_escape "$SOCKET_PATH")"
    # #5118: the watchdog honors LOOM_PID_FILE with the SAME precedence the
    # daemon does (daemon_pidfile.rs tier 1), so passing the path this script
    # chose makes the two ends single-source it. Before this the watchdog
    # derived its own path from the socket's directory and, on a
    # workspace-rooted install, looked at a file nothing ever writes -- a
    # permanent false "[DIVERGENCE] no live pid file" on every fleet host.
    printf '        <key>LOOM_PID_FILE</key>\n        <string>%s</string>\n' "$(xml_escape "$PID_FILE")"
    printf '        <key>LOOM_LAUNCHD_LABEL</key>\n        <string>%s</string>\n' "$(xml_escape "$(resolve_launchd_label)")"
    printf '    </dict>\n'
    printf '    <key>RunAtLoad</key>\n    <true/>\n'
    printf '    <key>StartInterval</key>\n    <integer>%s</integer>\n' "$interval"
    printf '    <key>ProcessType</key>\n    <string>Background</string>\n'
    printf '    <key>StandardOutPath</key>\n    <string>%s</string>\n' "$(xml_escape "$log_path")"
    printf '    <key>StandardErrorPath</key>\n    <string>%s</string>\n' "$(xml_escape "$log_path")"
    printf '</dict>\n</plist>\n'
}

# Provision + (re)load the watchdog LaunchAgent. Best-effort and NON-FATAL: a
# watchdog that fails to install must never fail the daemon start (the daemon
# running without a watchdog is strictly better than no daemon at all).
provision_watchdog_job_launchd() {
    command -v launchctl >/dev/null 2>&1 || { warn "watchdog: launchctl not found — skipping."; return 0; }
    local script; script="$(locate_watchdog_script)"
    if [[ -z "$script" ]]; then
        warn "watchdog: loom-daemon-watchdog.sh not found — skipping (autonomy-loss detection disabled)."
        return 0
    fi
    local wd_label wd_domain wd_service wd_plist wd_interval wd_log
    wd_label="$(resolve_watchdog_label)"
    # Same resolved domain the daemon job uses (#4130) so the watchdog is
    # bootstrapped where stop.sh will later look for it — gui/<uid> with a GUI
    # login, else the SSH-reachable user/<uid> domain.
    wd_domain="$(resolve_launchd_domain)"
    wd_service="${wd_domain}/${wd_label}"
    wd_plist="$HOME/Library/LaunchAgents/${wd_label}.plist"
    wd_interval="${LOOM_WATCHDOG_INTERVAL_SECS:-300}"
    wd_log="$LOOM_DIR/logs/daemon-watchdog.log"
    mkdir -p "$HOME/Library/LaunchAgents" "$LOOM_DIR/logs" 2>/dev/null || true
    local wd_plist_new; wd_plist_new="$(mktemp "${TMPDIR:-/tmp}/loom-watchdog-plist.XXXXXX" 2>/dev/null)" || wd_plist_new=""
    if [[ -z "$wd_plist_new" ]] || ! render_watchdog_plist "$wd_label" "$script" "$REPO_ROOT" "$wd_log" "$wd_interval" > "$wd_plist_new" 2>/dev/null; then
        warn "watchdog: could not write $wd_plist — skipping."
        rm -f "$wd_plist_new" 2>/dev/null || true
        return 0
    fi
    local wd_job_loaded=false
    launchctl print "$wd_service" >/dev/null 2>&1 && wd_job_loaded=true
    # #4862 double-fire fix: RunAtLoad=true means EVERY bootout+bootstrap cycle
    # fires an extra immediate run, on top of the regular StartInterval cadence.
    # provision_watchdog_job_launchd runs on EVERY loom-daemon-start.sh
    # invocation (every daemon start, restart, AND self-update relaunch) -- so
    # unconditionally re-bootstrapping here duplicated a run each time,
    # independent of the watchdog's own schedule. Skip the reload cycle
    # entirely when the job is already loaded and the rendered plist is
    # byte-identical to what's installed -- nothing to apply, so no reason to
    # trigger RunAtLoad again.
    if [[ "$wd_job_loaded" == "true" ]] && cmp -s "$wd_plist_new" "$wd_plist" 2>/dev/null; then
        rm -f "$wd_plist_new" 2>/dev/null || true
        echo "Watchdog:       $wd_label (StartInterval ${wd_interval}s) → $wd_log (unchanged, already loaded — skipped reload)"
        return 0
    fi
    mv -f "$wd_plist_new" "$wd_plist" 2>/dev/null || { warn "watchdog: could not install $wd_plist — skipping."; rm -f "$wd_plist_new" 2>/dev/null || true; return 0; }
    if [[ "$wd_job_loaded" == "true" ]]; then
        launchctl bootout "$wd_service" >/dev/null 2>&1 || true
    fi
    if launchctl bootstrap "$wd_domain" "$wd_plist" >/dev/null 2>&1; then
        echo "Watchdog:       $wd_label (StartInterval ${wd_interval}s) → $wd_log"
    else
        warn "watchdog: launchctl bootstrap failed for $wd_service — autonomy-loss detection not active (non-fatal)."
    fi
}

# ---------- watchdog systemd --user timer (#4260 sub-issue D) ----------
# resolve_systemd_watchdog_unit — the watchdog service/timer basename (no
# `.service`/`.timer` suffix), mirroring resolve_watchdog_label's Darwin
# `<daemon label>-watchdog` pattern: `<daemon unit>-watchdog`, with the same
# LOOM_WATCHDOG_LABEL override.
resolve_systemd_watchdog_unit() {
    local daemon_unit; daemon_unit="$(resolve_systemd_unit)"
    echo "${LOOM_WATCHDOG_LABEL:-${daemon_unit%.service}-watchdog}"
}

# render_systemd_watchdog_service <watchdog_script> <workdir> <log_path>
# Type=oneshot: the unit owns no long-lived process (the ExecStart runs the
# watchdog's single check-and-exit pass) -- the timer unit below re-fires it,
# not a Restart= directive.
render_systemd_watchdog_service() {
    local script="$1" workdir="$2" log_path="$3"
    printf '[Unit]\n'
    printf 'Description=Loom daemon autonomy-loss watchdog (loom-daemon-watchdog)\n'
    printf '\n'
    printf '[Service]\n'
    printf 'Type=oneshot\n'
    printf 'WorkingDirectory=%s\n' "$workdir"
    printf 'ExecStart=/bin/bash %s\n' "$script"
    printf 'Environment=PATH=%s\n' "$PLIST_PATH_VALUE"
    printf 'Environment=HOME=%s\n' "$HOME"
    printf 'Environment=LOOM_AUTONOMY_MARKER=%s\n' "$INTENT_MARKER"
    printf 'Environment=LOOM_SOCKET_PATH=%s\n' "$SOCKET_PATH"
    # #5118: same single-sourcing as the launchd watchdog plist above -- the
    # watchdog resolves the pid file exactly as the daemon does, and this is
    # the tier-1 value. (Observed on loom-worker-1: the watchdog read
    # ~/.loom/.daemon.pid while the daemon wrote <workspace>/.loom/.daemon.pid.)
    printf 'Environment=LOOM_PID_FILE=%s\n' "$PID_FILE"
    printf 'Environment=LOOM_DAEMON_LAUNCHD=0\n'
    printf 'StandardOutput=append:%s\n' "$log_path"
    printf 'StandardError=append:%s\n' "$log_path"
}

# render_systemd_watchdog_timer <service_unit_name> <interval_secs>
# OnUnitActiveSec is the systemd analog of launchd's StartInterval (re-fires
# every <interval>s regardless of the last run's exit status). OnBootSec gives
# the RunAtLoad-equivalent "run shortly after the user session starts" —
# though `enable --now` on a timer already triggers an immediate first run, so
# this only matters across reboots. Persistent=false: a watchdog tick missed
# while the session was down should NOT fire a catch-up run the moment the
# session resumes -- the next regular tick is soon enough.
render_systemd_watchdog_timer() {
    local service_unit="$1" interval="$2"
    printf '[Unit]\n'
    printf 'Description=Loom daemon autonomy-loss watchdog timer (loom-daemon-watchdog)\n'
    printf '\n'
    printf '[Timer]\n'
    printf 'OnBootSec=%s\n' "$interval"
    printf 'OnUnitActiveSec=%s\n' "$interval"
    printf 'Unit=%s\n' "$service_unit"
    printf 'Persistent=false\n'
    printf '\n'
    printf '[Install]\n'
    printf 'WantedBy=timers.target\n'
}

# Provision + enable the watchdog service+timer pair under `systemd --user`.
# Best-effort and NON-FATAL, same contract as the launchd path.
provision_watchdog_job_systemd() {
    command -v systemctl >/dev/null 2>&1 || { warn "watchdog: systemctl not found — skipping."; return 0; }
    local script; script="$(locate_watchdog_script)"
    if [[ -z "$script" ]]; then
        warn "watchdog: loom-daemon-watchdog.sh not found — skipping (autonomy-loss detection disabled)."
        return 0
    fi
    local wd_unit svc_unit timer_unit unit_dir svc_path timer_path wd_interval wd_log
    wd_unit="$(resolve_systemd_watchdog_unit)"
    svc_unit="${wd_unit}.service"
    timer_unit="${wd_unit}.timer"
    unit_dir="$(resolve_systemd_unit_dir)"
    svc_path="${unit_dir}/${svc_unit}"
    timer_path="${unit_dir}/${timer_unit}"
    wd_interval="${LOOM_WATCHDOG_INTERVAL_SECS:-300}"
    wd_log="$LOOM_DIR/logs/daemon-watchdog.log"
    mkdir -p "$unit_dir" "$LOOM_DIR/logs" 2>/dev/null || true
    if ! render_systemd_watchdog_service "$script" "$REPO_ROOT" "$wd_log" > "$svc_path" 2>/dev/null; then
        warn "watchdog: could not write $svc_path — skipping."
        return 0
    fi
    if ! render_systemd_watchdog_timer "$svc_unit" "$wd_interval" > "$timer_path" 2>/dev/null; then
        warn "watchdog: could not write $timer_path — skipping."
        return 0
    fi
    # #4862: unlike the launchd branch above (which must guard against
    # re-provisioning triggering an extra RunAtLoad run), `systemctl --user
    # enable --now` on an ALREADY ACTIVE timer is a no-op job that does NOT
    # re-trigger OnBootSec/re-run the service -- empirically verified (#4862):
    # two consecutive `enable --now` calls against the same active timer with
    # an already-elapsed OnBootSec produced exactly one execution, not two. So
    # re-running this on every daemon start/relaunch is safe as-is; no
    # unchanged-content guard needed here.
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    if systemctl --user enable --now "$timer_unit" >/dev/null 2>&1; then
        echo "Watchdog:       $timer_unit (OnUnitActiveSec ${wd_interval}s) → $wd_log"
    else
        warn "watchdog: systemctl --user enable --now failed for $timer_unit — autonomy-loss detection not active (non-fatal)."
    fi
}

# Called from the nohup fallback tier (non-systemd Linux host, or
# --no-launchd/--no-systemd): no scheduled watchdog job/timer to provision.
# Deliberately NOT re-derived from $IS_DARWIN/$IS_LINUX_SYSTEMD -- each of the
# three call sites below already knows definitively which supervisor tier it
# is in (that is what selected this code path), so it calls the matching
# provision_watchdog_job_{launchd,systemd} directly instead of re-detecting.
# Re-detecting here would be redundant AND actively wrong under the
# LOOM_SYSTEMD_FORCE=1 test seam, where a Darwin test runner can have both
# $IS_DARWIN and $IS_LINUX_SYSTEMD true simultaneously.
provision_watchdog_job_none() {
    warn "watchdog: no scheduled checker on this platform (nohup-fallback Linux / non-systemd host) — skipping (marker+heartbeat still active). Run loom-daemon-watchdog.sh by hand or wire it to cron."
    escalate_watchdog_unprovisionable
    return 0
}

# ---------- watchdog escalation when NO scheduled mechanism exists (#5343 AC4) ----------
# Called only from the nohup-fallback tier (provision_watchdog_job_none /
# heal_watchdog_provisioning_gap's own "no mechanism" branch below):
# non-systemd Linux, or an operator's explicit --no-launchd/--no-systemd escape
# hatch. On that tier this tooling structurally cannot provision a scheduled
# watchdog job at all (no StartInterval/OnUnitActiveSec-equivalent mechanism),
# so "auto-provisioning is out of scope" (the issue's AC4 condition) applies
# unconditionally here. Leaving that as a one-line stderr warning is exactly
# the failure mode #5343 exists to close -- a host can run for months with the
# gap and nothing surfaces it beyond a log line an operator has to go looking
# for. File ONE tracking issue via ./.loom/scripts/create-issue.sh (never a
# bare `gh issue create` — see this repo's own CLAUDE.md), deduped by a
# persistent sentinel file so this never re-files on every subsequent
# start/heal pass. Best-effort and NON-FATAL: any failure (no create-issue.sh
# on this host, no forge auth, offline) is warned and swallowed — a daemon
# (even an unprotected one) is strictly better than no daemon.
escalate_watchdog_unprovisionable() {
    [[ -f "$INTENT_MARKER" ]] || return 0

    local sentinel="$LOOM_DIR/.watchdog-unprovisionable-escalated"
    [[ -f "$sentinel" ]] && return 0

    local issue_script="$REPO_ROOT/.loom/scripts/create-issue.sh"
    [[ -f "$issue_script" ]] || issue_script="$REPO_ROOT/defaults/scripts/create-issue.sh"
    if [[ ! -f "$issue_script" ]]; then
        warn "watchdog: no scheduled checker on this platform, and create-issue.sh not found — cannot escalate (#5343)."
        return 0
    fi

    local hostname_str; hostname_str="$(hostname 2>/dev/null || echo unknown-host)"
    local body
    body="$(cat <<EOF
The autonomy-desired marker at \`$INTENT_MARKER\` is present on host \`$hostname_str\`,
meaning a loom-daemon is EXPECTED to be running here — but this platform tier (no
\`systemd --user\`, no launchd: a plain nohup-backgrounded daemon, or an explicit
--no-launchd/--no-systemd start) has no OS-level scheduled-job mechanism this tooling
can provision a watchdog timer onto.

Nothing is scheduled to detect a future daemon death on this host. Auto-provisioning is
out of scope here (issue #5343 AC4) — mitigate manually: run
\`loom-daemon-watchdog.sh\` by hand, wire it to cron, or move this host onto a
systemd/launchd-managed start.

Filed automatically by the loom-daemon-start.sh watchdog escalation (#5343). Deduped by a
sentinel file at \`$sentinel\` — delete it to allow re-filing after a genuine
reconfiguration.
EOF
)"
    if "$issue_script" \
        --title "loom-daemon-watchdog cannot be scheduled on $hostname_str (no systemd/launchd) — crash protection absent" \
        --body "$body" \
        --label "loom:triage" >/dev/null 2>"$LOOM_DIR/logs/.watchdog-escalation-err"; then
        mkdir -p "$LOOM_DIR" 2>/dev/null || true
        date -u '+%Y-%m-%dT%H:%M:%SZ' > "$sentinel" 2>/dev/null || true
        warn "watchdog: filed a tracking issue for the unprovisionable watchdog gap on this host (#5343 AC4)."
    else
        warn "watchdog: could not file a tracking issue for the unprovisionable watchdog gap (create-issue.sh failed — see $LOOM_DIR/logs/.watchdog-escalation-err)."
    fi
}

# ---------- watchdog self-heal for an ALREADY-RUNNING daemon (#5343) ----------
# Root cause (#5343): the watchdog job/timer was previously provisioned ONLY as
# a side effect of a FRESH loom-daemon-start.sh run reaching the
# launchd/systemd install branch (the unconditional provision_watchdog_job_*
# calls further down this file). Two paths leave the autonomy-desired marker
# present with NO watchdog ever provisioned:
#   1. `loom-daemon fleet add-worker` (loom-daemon/src/fleet/add_worker.rs)
#      hand-installs the `loom-daemon.service` systemd --user unit directly
#      (its own render_daemon_unit()) and never calls this script at all, so
#      its watchdog-provisioning branch never runs.
#   2. The daemon's own startup marker-healing (autonomy_marker.rs, #4331):
#      whenever a supervised daemon starts (LOOM_DAEMON_SUPERVISOR set — which
#      fleet add-worker's hand-rolled unit DOES set) with the marker absent, the
#      DAEMON PROCESS ITSELF re-writes the marker — independent of, and with no
#      knowledge of, the watchdog job.
# Once armed that way, the "already-running guard" just above used to `exit 0`
# immediately on ANY subsequent loom-daemon-start.sh invocation — including one
# an operator runs BY HAND specifically to check/repair the install — without
# ever reaching the watchdog-provisioning code below (which is unconditionally
# skipped whenever the running-daemon guard fires first). So even a deliberate
# re-run could not close the gap. This call makes that guard corrective instead
# of a silent no-op: marker present + watchdog job missing -> provisions it now,
# without touching the already-running daemon process at all (mirrors the
# daemon's own startup marker-healing pattern, #4331, applied to the watchdog
# side of the same "expected protection" contract).
#
# Non-fatal, and safe to call unconditionally:
#   - marker absent -> no provisioning attempt (nothing was ever "desired").
#   - marker present + job already present -> provision_watchdog_job_launchd /
#     _systemd are already idempotent (the launchd branch skips the reload
#     when already loaded and byte-identical; a bare `enable --now` on an
#     already-active systemd timer is a verified no-op, #4862) — calling them
#     again here never double-fires anything.
#   - marker present + no scheduled-job mechanism on this platform tier (the
#     nohup-fallback tier, or an explicit --no-launchd/--no-systemd escape
#     hatch) -> escalates instead of silently reporting (AC4), same as the
#     fresh-start nohup tier above.
# Platform detection is duplicated (deliberately, not shared with the real
# detection block below) because it must run BEFORE the already-running guard,
# ahead of where the real platform-detection block executes today — keeping it
# local avoids reordering the rest of this carefully-sequenced script.
heal_watchdog_provisioning_gap() {
    [[ -f "$INTENT_MARKER" ]] || return 0

    local heal_is_darwin=false heal_use_launchd=false heal_is_systemd=false
    [[ "$(uname -s)" == "Darwin" ]] && heal_is_darwin=true

    if [[ "$heal_is_darwin" == "true" ]]; then
        heal_use_launchd=true
        [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]] && heal_use_launchd=false
    fi
    [[ "$NO_LAUNCHD" == "true" ]] && heal_use_launchd=false

    if [[ "$heal_use_launchd" != "true" ]] \
        && ! [[ "${LOOM_DAEMON_SYSTEMD:-}" =~ ^(0|false|no)$ ]] \
        && [[ "$NO_SYSTEMD" != "true" ]] \
        && declare -f is_linux_systemd >/dev/null 2>&1 && is_linux_systemd; then
        heal_is_systemd=true
    fi

    # Deliberately no separate "is it already provisioned?" pre-check here —
    # provision_watchdog_job_launchd/_systemd already probe that internally
    # and are already idempotent (see their own doc comments), so a bare call
    # is both simpler and cannot double-fire the job.
    if [[ "$heal_use_launchd" == "true" ]]; then
        provision_watchdog_job_launchd
    elif [[ "$heal_is_systemd" == "true" ]]; then
        provision_watchdog_job_systemd
    else
        escalate_watchdog_unprovisionable
    fi
}

# ---------- args ----------
# Capture the raw invocation args before the parsing loop consumes "$@" — used
# below to persist exactly what was passed (Issue #3968: `loom-daemon-update.sh`
# replays these flags verbatim on restart, so a rebuild+restart never widens the
# FLAGS-OFF/opt-in contract).
ORIGINAL_ARGS=("$@")

# Default is FLAGS-OFF (#3911): both autonomous loops default OFF, matching the
# ecosystem-wide opt-in / default-off contract. Opt in with --work-finder /
# --health-gate, or hand control to config with --from-config.
FROM_CONFIG=false
FOREGROUND=false
# Tri-state (#4353): "" = not passed on the CLI (unset), "on" = an explicit
# --work-finder/--health-gate, "off" = an explicit --no-work-finder/
# --no-health-gate. This lets --from-config tell "the operator asked to force
# this loop" apart from "the operator said nothing, config drives it" --
# a plain boolean collapsed both to the same false and silently dropped the
# force.
WANT_WORK_FINDER=""
WANT_HEALTH_GATE=""
NO_LAUNCHD=false
NO_SYSTEMD=false
PRINT_PLIST=false
PRINT_UNIT=false
# --force-env (#4522, inverted #5344): acknowledges an intentional narrower
# re-render. By DEFAULT (this flag unset), warn_dropped_env_keys carries any
# env key present in the installed unit/plist forward into the re-render, even
# when this invocation's own env no longer has it -- a re-render can never
# silently narrow. --force-env is the only way to actually drop a missing key.
# Script-only (like --print-plist), not a daemon autonomy flag -- excluded
# from the persisted .daemon.flags file below.
FORCE_ENV=false
# --heal-watchdog-only (#5405): a narrow, side-effect-scoped entry point that
# performs ONLY the watchdog provisioning-gap heal (heal_watchdog_provisioning_gap,
# #5343) and exits -- it never reaches the "already-running guard" (below) or
# the daemon-start path at all, so it is safe for a host-resident periodic
# caller (the daemon's own watchdog_provisioning_guard loop) to invoke
# repeatedly without any risk of accidentally starting a second daemon if a
# PID-file read were ever wrong. Script-only, not a daemon autonomy flag --
# excluded from the persisted .daemon.flags file below.
HEAL_WATCHDOG_ONLY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) show_help; exit 0 ;;
        --from-config) FROM_CONFIG=true; shift ;;
        --foreground|--fg) FOREGROUND=true; shift ;;
        --work-finder) WANT_WORK_FINDER="on"; shift ;;
        --health-gate) WANT_HEALTH_GATE="on"; shift ;;
        --no-work-finder) WANT_WORK_FINDER="off"; shift ;;
        --no-health-gate) WANT_HEALTH_GATE="off"; shift ;;
        --no-launchd) NO_LAUNCHD=true; shift ;;
        --no-systemd) NO_SYSTEMD=true; shift ;;
        --print-plist) PRINT_PLIST=true; shift ;;
        --print-unit) PRINT_UNIT=true; shift ;;
        --force-env) FORCE_ENV=true; shift ;;
        --heal-watchdog-only) HEAL_WATCHDOG_ONLY=true; shift ;;
        *) err "Unknown option '$1'"; echo "Use --help for usage" >&2; exit 1 ;;
    esac
done

# Snapshot whatever the CALLING SHELL already exported, BEFORE the
# autonomous-mode env block below applies the FLAGS-OFF default (#3911). The
# silent-autonomy-downgrade check (#4693) needs to tell "this invocation's
# own default logic produced 0" (worth a warning if it downgrades a
# previously-autonomous host) apart from "the operator explicitly exported 0
# themselves" (an explicit, non-default signal -- never silent).
PRE_EXPORTED_WORK_FINDER="${LOOM_WORK_FINDER:-}"
PRE_EXPORTED_MAIN_HEALTH_GATE="${LOOM_MAIN_HEALTH_GATE:-}"

REPO_ROOT=$(find_repo_root)

# ---------- machine-mode resolution (Epic #3835 Phase 3b, #4229) ----------
# LOOM_MACHINE_CHECKOUT (set by the `scripts/loom` dispatcher before it execs
# this script) is authoritative regardless of $PWD: the launchd label this
# script drives (com.rjwalters.loom-daemon) is a machine-wide singleton, so
# `loom start` run from repo A and again from repo B must resolve the SAME
# workdir + pid/flags home -- not two different ones keyed to whichever repo
# happened to be $PWD when it was invoked. Direct invocation of this script
# (no dispatcher -- the existing dev workflow) leaves this var unset and falls
# through to the pre-#4229 $PWD-based contract below, byte-for-byte unchanged.
MACHINE_CHECKOUT="${LOOM_MACHINE_CHECKOUT:-}"
MACHINE_MODE=false
if [[ -n "$MACHINE_CHECKOUT" ]]; then
    MACHINE_MODE=true
    if [[ ! -d "$MACHINE_CHECKOUT" ]]; then
        err "LOOM_MACHINE_CHECKOUT does not exist: $MACHINE_CHECKOUT"
        exit 1
    fi
    REPO_ROOT="$MACHINE_CHECKOUT"
    # Runtime artifacts (pid file, persisted flags, startup log) live under the
    # EXISTING machine-level state home (~/.loom -- socket, token pool,
    # activity.db, and the daemon's own log already live there; see
    # machine-dispatcher.md's "pid/flags relocation" note) rather than under
    # the checkout itself, which may be a symlink to a developer's working
    # clone and is not otherwise treated as writable runtime state.
    DAEMON_STATE_HOME="$HOME/.loom"
elif [[ -n "$REPO_ROOT" ]]; then
    DAEMON_STATE_HOME="$REPO_ROOT/.loom"
else
    err "Not in a Loom workspace (.loom directory not found)"
    exit 1
fi

# ---------- daemon-binary lookup ----------
# Skipped (never fatal) under --heal-watchdog-only (#5405): that mode never
# starts, stops, or even talks to a daemon process, so it must not fail just
# because $DAEMON_BIN happens to be unresolvable on this host (e.g. a binary
# that was later moved/removed after the daemon it belongs to was started --
# the watchdog job/timer should still be re-provisionable independent of that).
DAEMON_BIN=$(loom_locate_daemon_bin "$REPO_ROOT")
if [[ -z "$DAEMON_BIN" && "$HEAL_WATCHDOG_ONLY" != "true" ]]; then
    err "loom-daemon binary not found. Checked:"
    loom_daemon_bin_search_paths "$REPO_ROOT" | sed 's/^/  - /' >&2
    echo "Build it (cargo build --release -p loom-daemon), install it to one of the paths above, or set LOOM_DAEMON_BIN=/path/to/loom-daemon" >&2
    exit 1
fi

# ---------- deterministic plist PATH (#4172) ----------
# Resolved ONCE per invocation so both the daemon plist and the watchdog
# plist (below) render the identical PATH, and so the choice is logged to
# stderr exactly once per run rather than once per plist rendered.
PLIST_PATH_VALUE="$(resolve_plist_path)"

# ---------- pid-file derivation: DERIVED-ONLY BY DESIGN (#6420) ----------
# Unlike loom-daemon-stop.sh / -update.sh / -watchdog.sh / daemon_pidfile.rs --
# which all resolve an inbound LOOM_PID_FILE as TIER 1, ahead of this same
# derivation (#6386, #5118) -- this script deliberately does NOT read
# LOOM_PID_FILE. It WRITES it. The asymmetry is the point, not an oversight:
#
#   * One writer, N readers. `start` is the only end that CHOOSES where the pid
#     file lives; every other end must resolve whatever `start` chose. That is
#     what keeps "all ends mean the same file" true, and it is why the value is
#     exported and baked into the plist/unit below rather than re-derived by
#     each reader.
#   * The blast radius runs the OTHER WAY here. For a reader, honoring an
#     explicit LOOM_PID_FILE NARROWS what it touches -- that is precisely
#     #6386's fix (a stop that was told which pid file to use must not wander
#     onto the live one via $PWD). For `start`, honoring it WIDENS what it
#     touches: this script reads the path for its already-running guard, `rm
#     -f`s it, writes the new pid into it, and hands it to a daemon that claims
#     it. And LOOM_PID_FILE is AMBIENT in any Loom agent session -- this very
#     export lands in the daemon's env and is inherited by every sweep/agent
#     child it spawns (observed on a worker host; see the header of
#     defaults/scripts/tests/lib/live-state-sandbox.sh). Honoring it would mean
#     a `start` run inside a scratch fixture silently claims, rewrites, and
#     `rm -f`s the LIVE daemon's pid file -- incident #5179's exact shape, with
#     the resulting FALSE `degraded` liveness verdict for the operator and a
#     poisoned watchdog input.
#   * Nothing is lost. A caller who needs the pid file somewhere else moves the
#     STATE HOME (LOOM_MACHINE_CHECKOUT, or the repo root $PWD resolves to),
#     which this script does honor -- an unambiguous, deliberate act rather
#     than an inherited env var.
#
# Regression-pinned by test-loom-daemon-start.sh ("LOOM_PID_FILE is an OUTPUT")
# so this contract cannot be "aligned" away silently.
PID_FILE="$DAEMON_STATE_HOME/.daemon.pid"
# Exported (#4774) so the daemon writes the SAME file this script does. Both
# the plist and systemd-unit renderers harvest every exported LOOM_* var, so
# the path chosen here is baked into the supervisor definition and every
# supervisor-triggered relaunch resolves it identically -- which is the whole
# point: those relaunches (launchd KeepAlive, systemd Restart=, the #4054
# restart primitive, the self-update roll, `launchctl kickstart`) never re-run
# this script, so before #4774 the file kept naming a long-dead pid. The daemon
# now claims it itself right after its socket bind succeeds.
export LOOM_PID_FILE="$PID_FILE"
SOCKET_PATH="${LOOM_SOCKET_PATH:-$HOME/.loom/loom-daemon.sock}"
START_LOG="$DAEMON_STATE_HOME/logs/daemon-start.log"
# Skipped for the two pure-inspection modes (#6387): they only ever render
# $START_LOG as a STRING into the plist/unit preview and never open it, so
# creating the directory would be a gratuitous filesystem write on a path that
# advertises "no side effects". Every other mode (including
# --heal-watchdog-only) still gets it, unchanged.
if [[ "$PRINT_PLIST" != "true" && "$PRINT_UNIT" != "true" ]]; then
    mkdir -p "$DAEMON_STATE_HOME/logs"
fi

# ---------- autonomy-desired marker + heartbeat paths (#4011) ----------
# LOOM_DIR is the machine-level dir the daemon uses for its socket/log/heartbeat
# — the parent of SOCKET_PATH, matching the daemon's own resolve_loom_dir()
# (LOOM_SOCKET_PATH parent, else ~/.loom). Pointing SOCKET_PATH at a tempdir (as
# the lifecycle tests do) therefore isolates the marker + heartbeat there too,
# never touching the operator's real ~/.loom.
LOOM_DIR="$(dirname "$SOCKET_PATH")"
INTENT_MARKER="${LOOM_AUTONOMY_MARKER:-$LOOM_DIR/autonomy-desired}"
HEARTBEAT_FILE="$LOOM_DIR/daemon.heartbeat"
# Kept in sync with the daemon-side default (daemon_heartbeat.rs) so the
# watchdog's derived staleness threshold matches the real cadence.
HEARTBEAT_INTERVAL_SECS="${LOOM_DAEMON_HEARTBEAT_INTERVAL_SECS:-60}"

# ---------- --heal-watchdog-only short-circuit (#5405) ----------
# A narrow, side-effect-scoped entry point: perform ONLY the watchdog
# provisioning-gap heal (heal_watchdog_provisioning_gap, #5343 -- reused
# verbatim, never reimplemented) and exit, using the SAME LOOM_DIR /
# INTENT_MARKER / PID_FILE / PLIST_PATH_VALUE / SOCKET_PATH the normal
# already-running-guard heal call below uses (so a launchd/systemd unit it
# renders is byte-identical to one rendered from a real start). Placed BEFORE
# the "already-running guard" so it can never fall through into the
# guard's "stale PID file -> attempt to actually start a NEW daemon" branch --
# the exact "disturb the running daemon" outcome #5405's AC2 forbids. This
# lets a host-resident periodic caller (the daemon's own
# watchdog_provisioning_guard loop, loom-daemon/src/watchdog_provisioning_guard.rs)
# invoke this repeatedly and safely, independent of whether the PID file this
# script itself manages happens to be present, stale, or absent.
if [[ "$HEAL_WATCHDOG_ONLY" == "true" ]]; then
    heal_watchdog_provisioning_gap
    exit 0
fi

# ---------- --print-plist / --print-unit short-circuit (#6387) ----------
# The two pure-inspection modes are decided from ARGV ALONE and return here,
# BEFORE the already-running guard below -- and therefore before any state read
# that can branch into provisioning, marker writes, or a launchctl/systemctl
# call. Placement is the whole fix (#6387): these two exits used to sit ~300
# lines further down, so a live PID file made the already-running guard fire
# first and its heal_watchdog_provisioning_gap call `launchctl bootstrap` a REAL
# watchdog job under whatever $LOOM_LAUNCHD_LABEL was set -- documented as "no
# side effects", observed on 2026-08-16 bootstrapping two test-labelled watchdog
# jobs that then ran for ~11h against the operator's real daemon state. Same
# reasoning (and same position) as the --heal-watchdog-only short-circuit above:
# a narrow mode must never fall through into a wider mode's side effects.
#
# resolve_autonomy_env must run first: render_launchd_plist/render_systemd_unit
# harvest the process env, so the preview would otherwise silently omit the
# autonomy vars a real start would bake in.
if [[ "$PRINT_PLIST" == "true" || "$PRINT_UNIT" == "true" ]]; then
    resolve_autonomy_env
    run_inspection_mode_and_exit
fi

# ---------- already-running guard (PID file) ----------
if [[ -f "$PID_FILE" ]]; then
    existing_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
        warn "loom-daemon already running (pid $existing_pid, per $PID_FILE)."
        # #5409 secondary papercut: a --work-finder/--no-work-finder/
        # --health-gate/--no-health-gate/--from-config passed to THIS
        # invocation is silently ignored on this path -- the daemon is never
        # touched, so none of them take effect. Before this, an operator
        # could believe the flag applied (it was accepted, not rejected) and
        # only discover otherwise by inspecting the live plist/unit. Say so
        # explicitly instead of staying silent about it.
        ignored_flags=()
        [[ "$WANT_WORK_FINDER" == "on" ]] && ignored_flags+=("--work-finder")
        [[ "$WANT_WORK_FINDER" == "off" ]] && ignored_flags+=("--no-work-finder")
        [[ "$WANT_HEALTH_GATE" == "on" ]] && ignored_flags+=("--health-gate")
        [[ "$WANT_HEALTH_GATE" == "off" ]] && ignored_flags+=("--no-health-gate")
        [[ "$FROM_CONFIG" == "true" ]] && ignored_flags+=("--from-config")
        if [[ "${#ignored_flags[@]}" -gt 0 ]]; then
            ignored_joined="$(IFS=', '; echo "${ignored_flags[*]}")"
            warn "Ignoring ${ignored_joined} -- the daemon is already running, and flags only"
            warn "take effect on (re)start. To apply them, stop first:"
        fi
        unset ignored_flags ignored_joined
        # #5343: self-heal a watchdog-provisioning gap even though the daemon
        # itself is already running and this invocation is about to exit
        # without touching it. See heal_watchdog_provisioning_gap's doc
        # comment for why this guard used to be a dead end for that repair.
        heal_watchdog_provisioning_gap
        if [[ "$MACHINE_MODE" == "true" ]]; then
            echo "To restart: loom restart  (or: loom stop && loom start)" >&2
        else
            echo "To restart: ./.loom/scripts/cli/loom-daemon-stop.sh && $0" >&2
        fi
        exit 0
    fi
    # Stale PID file — clean it up and continue.
    rm -f "$PID_FILE"
fi

# ---------- advisory host-sleep check (never blocks — #3350) ----------
SLEEP_CHECK="$REPO_ROOT/.loom/scripts/check-host-sleep.sh"
[[ -x "$SLEEP_CHECK" ]] || SLEEP_CHECK="$REPO_ROOT/defaults/scripts/check-host-sleep.sh"
if [[ -x "$SLEEP_CHECK" ]]; then
    "$SLEEP_CHECK" || true
fi

# ---------- host-sleep prevention wrap, foreground mode only (#6311) ----------
# Repo-level opt-in (`host.preventSleep`, see lib/host-sleep-config.sh — same
# env > config > default-OFF precedence, and same Linux-only / never-`sudo`
# guardrails as spawn-claude.sh's identical mechanism). Computed here,
# consumed at the `--foreground` exec below.
#
# Deliberately NOT wired into the systemd-unit (`ExecStart=`) or nohup-
# fallback launch paths further down: both persist `$daemon_pid` into
# `$PID_FILE` / `systemctl show -p MainPID`, which every other lifecycle
# script (stop, watchdog, `loom-daemon status`) assumes IS the daemon's own
# pid. Prefixing either launch with `systemd-inhibit` would make that pid
# belong to `systemd-inhibit` instead — an untested, high-blast-radius change
# to already-load-bearing process-identity assumptions this issue's scope
# does not justify. `--foreground` has neither a PID file nor a watchdog
# consumer (it is Ctrl-C-driven), so it is a safe, useful increment on its
# own — and every daemon-dispatched sweep/role-runner spawn already self-
# wraps via spawn-claude.sh's identical mechanism, which (since `idle:sleep`
# locks are host-wide, not per-process) keeps a systemd/nohup-launched daemon
# awake too for as long as at least one spawn is in flight.
DAEMON_SLEEP_INHIBIT_WRAP=()
_daemon_sleep_inhibit_config_lib="$_LOOM_LAUNCHD_LIB_DIR/host-sleep-config.sh"
if [[ -f "$_daemon_sleep_inhibit_config_lib" ]]; then
    # shellcheck source=../lib/host-sleep-config.sh
    source "$_daemon_sleep_inhibit_config_lib"
    if declare -F loom_host_prevent_sleep_enabled >/dev/null 2>&1 \
        && [[ "$(loom_host_prevent_sleep_enabled "$REPO_ROOT")" == "1" ]] \
        && command -v systemd-inhibit >/dev/null 2>&1 \
        && systemd-inhibit --what=idle:sleep --who=loom --why=probe -- true >/dev/null 2>&1; then
        DAEMON_SLEEP_INHIBIT_WRAP=(systemd-inhibit --what=idle:sleep --who=loom --why=daemon --)
        echo "Sleep inhibit:  host.preventSleep enabled — foreground mode will wrap in systemd-inhibit (issue #6311)"
    fi
fi

# ---------- autonomous-mode env + guard-hook autonomy defaults ----------
# Body lives in resolve_autonomy_env() (defined with the other helpers above)
# so the pure-inspection short-circuit (--print-plist/--print-unit, #6387) can
# resolve the SAME env from its much earlier position, before the
# already-running guard. Exactly one caller runs per invocation.
resolve_autonomy_env

# ---------- persist invocation flags (Issue #3968) ----------
# `loom-daemon-update.sh` reads this file to restart with EXACTLY the same
# autonomy flags after a rebuild — the FLAGS-OFF/opt-in contract must never
# widen across an update. Script-only flags that don't describe daemon
# autonomy state (--foreground/--fg, --help/-h, --print-plist, --print-unit,
# --force-env, #4522) are filtered out; everything else (--from-config,
# --work-finder, --health-gate, --no-work-finder, --no-health-gate) is
# preserved verbatim, one per line. Written on every start attempt (success or
# failure) so the record always reflects the most recent invocation.
FLAGS_FILE="$DAEMON_STATE_HOME/.daemon.flags"
: > "$FLAGS_FILE"
# Guard the array expansion: a bare invocation (the common case) leaves
# ORIGINAL_ARGS empty, and "${arr[@]}" on a zero-element array is an unbound
# variable error under `set -u` on bash < 4.4 (still the default /bin/bash on
# stock macOS). ${#ORIGINAL_ARGS[@]} is always safe to query.
if [[ "${#ORIGINAL_ARGS[@]}" -gt 0 ]]; then
    for _flag_arg in "${ORIGINAL_ARGS[@]}"; do
        case "$_flag_arg" in
            --foreground|--fg|--help|-h|--no-launchd|--no-systemd|--print-plist|--print-unit|--force-env) continue ;;
            *) echo "$_flag_arg" >> "$FLAGS_FILE" ;;
        esac
    done
    unset _flag_arg
fi

echo "Daemon binary: $DAEMON_BIN"
echo "Socket:        $SOCKET_PATH"
echo "Daemon log:    ${HOME}/.loom/daemon.log"
if [[ "$MACHINE_MODE" == "true" ]]; then
    echo "Mode:          machine (workdir: $REPO_ROOT, state: $DAEMON_STATE_HOME)"
else
    echo "Mode:          dev (repo: $REPO_ROOT)"
fi

# ---------- foreground mode ----------
if [[ "$FOREGROUND" == "true" ]]; then
    echo "Starting loom-daemon in the foreground (Ctrl-C to stop)..."
    exec ${DAEMON_SLEEP_INHIBIT_WRAP[@]+"${DAEMON_SLEEP_INHIBIT_WRAP[@]}"} "$DAEMON_BIN"
fi

# ---------- platform detection (#3972) ----------
IS_DARWIN=false
[[ "$(uname -s)" == "Darwin" ]] && IS_DARWIN=true

USE_LAUNCHD=false
if [[ "$IS_DARWIN" == "true" ]]; then
    USE_LAUNCHD=true
    if [[ "${LOOM_DAEMON_LAUNCHD:-}" =~ ^(0|false|no)$ ]]; then
        USE_LAUNCHD=false
    fi
fi
[[ "$NO_LAUNCHD" == "true" ]] && USE_LAUNCHD=false

# ---------- Linux systemd --user detection (#4268) ----------
# On a systemd Linux host, supervise the daemon as a `systemd --user` service
# instead of a plain nohup background job (the launchd analog, #3972). The
# escape hatch --no-systemd / LOOM_DAEMON_SYSTEMD=0 forces the legacy nohup path,
# symmetric with --no-launchd / LOOM_DAEMON_LAUNCHD=0 on Darwin (#4078 analog).
# is_linux_systemd() (lib/systemd-user.sh) is false on a non-systemd Linux host,
# in a container without a user manager, or on Darwin -- all of which fall
# through to the nohup path byte-compatibly.
IS_LINUX_SYSTEMD=false
if [[ "$USE_LAUNCHD" != "true" ]] \
    && ! [[ "${LOOM_DAEMON_SYSTEMD:-}" =~ ^(0|false|no)$ ]] \
    && [[ "$NO_SYSTEMD" != "true" ]]; then
    if declare -f is_linux_systemd >/dev/null 2>&1 && is_linux_systemd; then
        IS_LINUX_SYSTEMD=true
    elif [[ "$IS_DARWIN" != "true" ]] && command -v systemctl >/dev/null 2>&1 \
        && declare -f systemd_user_manager_reachable >/dev/null 2>&1 \
        && ! systemd_user_manager_reachable; then
        # systemctl is present but the per-user manager is unreachable (a bare
        # SSH login with no lingering / no active user session). Warn clearly and
        # fall back to nohup rather than failing with a cryptic bus error.
        warn "systemd --user manager unreachable (no XDG_RUNTIME_DIR / offline) — falling back to nohup."
        warn "For a supervised, reboot-surviving daemon, run: loginctl enable-linger \"\$USER\" and retry."
    fi
fi

# ---------- prior installed plist/unit (autonomy-downgrade check, #4693) ----------
# Resolved once here, now that platform detection has picked the mechanism
# this invocation would use -- the SAME label/unit-path helpers the real
# install below uses, so "prior" always means "whatever is installed under the
# identifier THIS invocation would overwrite". Left empty on the nohup fallback
# tier (no rendered file exists there) -- the autonomy-desired marker alone is
# the only available signal in that case (see check_autonomy_downgrade_key
# above).
#
# This is the REAL-START path only. --print-plist / --print-unit resolve the
# same three variables from ARGV ALONE (never from platform detection, so the
# downgrade warning is never silently unreachable under --print-plist on a
# Linux host, #4693) and exit long before here -- see
# run_inspection_mode_and_exit and its call site above the already-running
# guard (#6387).
PRIOR_AUTONOMY_MECH=""
if [[ "$USE_LAUNCHD" == "true" ]]; then
    PRIOR_AUTONOMY_MECH="launchd"
elif [[ "$IS_LINUX_SYSTEMD" == "true" ]]; then
    PRIOR_AUTONOMY_MECH="systemd"
fi

PRIOR_AUTONOMY_FILE=""
PRIOR_AUTONOMY_EXTRACTOR=""
if [[ "$PRIOR_AUTONOMY_MECH" == "launchd" ]]; then
    PRIOR_AUTONOMY_FILE="$HOME/Library/LaunchAgents/$(resolve_launchd_label).plist"
    PRIOR_AUTONOMY_EXTRACTOR="extract_plist_env_value"
elif [[ "$PRIOR_AUTONOMY_MECH" == "systemd" ]] && declare -f resolve_systemd_unit_path >/dev/null 2>&1; then
    PRIOR_AUTONOMY_FILE="$(resolve_systemd_unit_path 2>/dev/null || true)"
    PRIOR_AUTONOMY_EXTRACTOR="extract_systemd_env_value"
fi

# Run BEFORE the real install below, so the operator sees the warning before
# the prior file gets overwritten. The inspection modes run their own
# (warn-only) call from run_inspection_mode_and_exit, above.
warn_autonomy_downgrade
# #6568: placed here, alongside warn_autonomy_downgrade, for the same reason --
# this is the last point before the plist/unit is rendered and installed, and
# platform detection (just above) has decided which supervisor identity would
# be written. guard_session_context_start REFUSES on this path (it only warns
# under --print-plist/--print-unit); warn_scratch_workdir_drift is advisory on
# both, and runs FIRST so a refused start still reports both diagnoses.
warn_scratch_workdir_drift
guard_session_context_start

# ---------- background + PID file ----------
: > "$START_LOG"

if [[ "$USE_LAUNCHD" == "true" ]] && ! command -v launchctl >/dev/null 2>&1; then
    warn "launchctl not found despite running on Darwin -- falling back to nohup."
    USE_LAUNCHD=false
fi

if [[ "$USE_LAUNCHD" == "true" ]]; then
    # ---------- macOS: launchd LaunchAgent (#3972) ----------
    # A plain `nohup ... &` stays in the LAUNCHING SESSION's Mach bootstrap
    # namespace; when that session dies, trustd/opendirectoryd XPC lookups
    # start failing for the daemon and every child it spawns (gh TLS errors,
    # "No user exists for uid N" from git) with no crash and no obvious log
    # signal. Loading as a launchd LaunchAgent keeps the daemon in a durable
    # per-user bootstrap domain instead, independent of whichever
    # terminal/session launched it. See daemon-reference.md Operability for
    # the incident writeup. Escape hatch: --no-launchd / LOOM_DAEMON_LAUNCHD=0.
    # The domain is resolve_launchd_domain()'s pick (#4130): gui/<uid> with a
    # live GUI login (unchanged from before), else the SSH-reachable user/<uid>
    # background domain so a headless start no longer fails `error 125`.
    LAUNCHD_LABEL=$(resolve_launchd_label)
    LAUNCHD_DOMAIN="$(resolve_launchd_domain)"
    LAUNCHD_SERVICE="${LAUNCHD_DOMAIN}/${LAUNCHD_LABEL}"
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_FILE="$PLIST_DIR/${LAUNCHD_LABEL}.plist"
    mkdir -p "$PLIST_DIR"

    # Render to a scratch file first -- NOT directly over $PLIST_FILE -- so the
    # dropped-env-key check (#4522) below can compare against whatever
    # $PLIST_FILE already contains before it gets clobbered, and so the
    # carry-forward merge (#5344) can rewrite $_PLIST_NEW_TMP in place BEFORE
    # it is installed.
    _PLIST_NEW_TMP="$(mktemp "$PLIST_DIR/.${LAUNCHD_LABEL}.new.XXXXXX")"
    render_launchd_plist "$LAUNCHD_LABEL" "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG" > "$_PLIST_NEW_TMP"
    warn_dropped_env_keys "$PLIST_FILE" "$_PLIST_NEW_TMP" extract_plist_env_keys extract_plist_env_value inject_one_plist_env_entry
    mv "$_PLIST_NEW_TMP" "$PLIST_FILE"

    # Harden the rendered plist when it carries a forwarded credential
    # (#4005): the token-forwarding loop in render_launchd_plist writes any
    # exported GH_TOKEN/GITEA_TOKEN/FORGE_TOKEN straight into
    # EnvironmentVariables above, and the plain `>` redirect otherwise leaves
    # the file at the process's umask (typically world-readable, 0644) --
    # any local user could read the PAT straight out of
    # ~/Library/LaunchAgents. Match the same env pattern the forwarding loop
    # reads from.
    if env | grep -qE '^(GH_TOKEN|GITEA_TOKEN|FORGE_TOKEN)=' 2>/dev/null; then
        chmod 600 "$PLIST_FILE"
    fi

    echo "Launchd label:  $LAUNCHD_LABEL"
    echo "Launchd plist:  $PLIST_FILE"

    # Reload with the freshly-rendered plist every time -- a job left loaded
    # from a prior invocation (possibly with different flags/env) must not
    # silently keep running its OLD definition.
    #
    # `launchctl bootout` is ASYNCHRONOUS (#5081): it returns before the
    # kernel has actually finished tearing the old job down, so an immediate
    # `bootstrap` can race that teardown and fail with "Bootstrap failed: 5:
    # Input/output error" (EIO) even though the plist is perfectly valid --
    # leaving NO job loaded and the daemon down until a retry. (This is
    # unrelated to whether bootout kills in-flight SWEEPS -- it no longer does,
    # since #3800 gives every sweep its own process group -- this is purely
    # about the bootout/bootstrap race on the job itself.) Settle briefly
    # after bootout (poll `launchctl print` until the job is actually gone,
    # bounded by LOOM_DAEMON_BOOTOUT_SETTLE_SECS), and retry bootstrap
    # specifically on that EIO shape (never on other failures, which are
    # genuine plist/permission problems a retry cannot fix) rather than
    # reporting a half-applied update.
    if launchctl print "$LAUNCHD_SERVICE" >/dev/null 2>&1; then
        launchctl bootout "$LAUNCHD_SERVICE" >/dev/null 2>&1 || true
        BOOTOUT_SETTLE_SECS="${LOOM_DAEMON_BOOTOUT_SETTLE_SECS:-5}"
        _bootout_settle_deadline=$((SECONDS + BOOTOUT_SETTLE_SECS))
        while launchctl print "$LAUNCHD_SERVICE" >/dev/null 2>&1; do
            [[ $SECONDS -ge $_bootout_settle_deadline ]] && break
            sleep 0.2
        done
    fi

    BOOTSTRAP_ERR="$START_LOG.bootstrap-err"
    BOOTSTRAP_MAX_ATTEMPTS="${LOOM_DAEMON_BOOTSTRAP_RETRY_ATTEMPTS:-4}"
    BOOTSTRAP_RETRY_SLEEP_SECS="${LOOM_DAEMON_BOOTSTRAP_RETRY_SECS:-2}"
    _bootstrap_attempt=0
    while :; do
        _bootstrap_attempt=$((_bootstrap_attempt + 1))
        if launchctl bootstrap "$LAUNCHD_DOMAIN" "$PLIST_FILE" 2>"$BOOTSTRAP_ERR"; then
            rm -f "$BOOTSTRAP_ERR"
            break
        fi
        if grep -qE '(^|[^0-9])5: Input/output error' "$BOOTSTRAP_ERR" 2>/dev/null \
            && [[ "$_bootstrap_attempt" -lt "$BOOTSTRAP_MAX_ATTEMPTS" ]]; then
            warn "launchctl bootstrap hit the async-bootout race (EIO) for $LAUNCHD_SERVICE -- attempt ${_bootstrap_attempt}/${BOOTSTRAP_MAX_ATTEMPTS}, settling ${BOOTSTRAP_RETRY_SLEEP_SECS}s and retrying (#5081)."
            sleep "$BOOTSTRAP_RETRY_SLEEP_SECS"
            continue
        fi
        err "launchctl bootstrap failed for $LAUNCHD_SERVICE (attempt ${_bootstrap_attempt}/${BOOTSTRAP_MAX_ATTEMPTS}):"
        cat "$BOOTSTRAP_ERR" >&2 2>/dev/null || true
        rm -f "$BOOTSTRAP_ERR"
        exit 1
    done

    # RunAtLoad=true means bootstrap alone would already start it, but we
    # kickstart -k explicitly anyway so THIS invocation deterministically wins
    # (the -k kill-first semantics guarantee a fresh process picking up the
    # plist we just wrote, rather than racing launchd's own RunAtLoad timing).
    KICKSTART_ERR="$START_LOG.kickstart-err"
    if ! launchctl kickstart -k "$LAUNCHD_SERVICE" 2>"$KICKSTART_ERR"; then
        err "launchctl kickstart failed for $LAUNCHD_SERVICE:"
        cat "$KICKSTART_ERR" >&2 2>/dev/null || true
        rm -f "$KICKSTART_ERR"
        exit 1
    fi
    rm -f "$KICKSTART_ERR"

    # Give it a moment to either bind the socket or trip the singleton guard.
    sleep 2

    daemon_pid=$(launchctl print "$LAUNCHD_SERVICE" 2>/dev/null | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/[^0-9]/, "", $2); print $2; exit}')

    if [[ -z "$daemon_pid" ]] || ! kill -0 "$daemon_pid" 2>/dev/null; then
        err "loom-daemon did not stay running under launchd ($LAUNCHD_SERVICE)."
        if [[ -s "$START_LOG" ]]; then
            echo "----- startup output ($START_LOG) -----" >&2
            tail -n 20 "$START_LOG" >&2
            echo "---------------------------------------" >&2
        fi
        warn "If another daemon is already listening on the socket, stop it first"
        warn "(./.loom/scripts/cli/loom-daemon-stop.sh) and retry."
        exit 1
    fi

    # Post-condition (#5081): a successful bootstrap + a live pid do not, by
    # themselves, prove the freshly-rendered plist's EnvironmentVariables
    # actually took effect -- launchd's own "environment = { ... }" block
    # (from `launchctl print`) is the only authoritative source for what the
    # running process actually received. Verify it before reporting success,
    # rather than silently returning a daemon that is alive but still running
    # under some stale/unexpected env.
    if declare -f verify_launchd_env_applied >/dev/null 2>&1; then
        _env_verify_out=$(verify_launchd_env_applied "$LAUNCHD_SERVICE" "$PLIST_FILE" 2>&1)
        _env_verify_rc=$?
        if [[ "$_env_verify_rc" -eq 1 ]]; then
            err "loom-daemon is running (pid ${daemon_pid}) under launchd, but its reported environment does NOT match the freshly-rendered plist -- refusing to report success (#5081)."
            printf '%s\n' "$_env_verify_out" >&2
            exit 1
        elif [[ "$_env_verify_rc" -ne 0 ]]; then
            warn "Could not verify the running job's env against the plist (plutil/jq unavailable?) -- proceeding, but the env change is unconfirmed:"
            printf '%s\n' "$_env_verify_out" | while IFS= read -r _line; do warn "  $_line"; done
        fi
    fi

    # Redundant since #4774 -- the daemon claims $PID_FILE itself immediately
    # after its socket bind succeeds -- but kept deliberately. It costs nothing,
    # it closes the window between "supervisor reports a pid" and "the daemon
    # reaches its bind", and it is the only writer for a daemon binary older
    # than #4774 (a start script and a daemon roll independently). Harmless if
    # both write: same path, same pid, and the daemon's write is atomic.
    echo "$daemon_pid" > "$PID_FILE"
    # Record operator intent + arm the host-side autonomy-loss watchdog (#4011).
    write_intent_marker "true" "$LAUNCHD_LABEL"
    provision_watchdog_job_launchd
    ok "loom-daemon started under launchd (pid $daemon_pid, label $LAUNCHD_LABEL)."
    echo "PID file: $PID_FILE"
    echo "Intent marker: $INTENT_MARKER"
    print_safehouse_status
    print_calibrate_hint
    if [[ "$MACHINE_MODE" == "true" ]]; then
        echo "Stop with: loom stop"
    else
        echo "Stop with: ./.loom/scripts/cli/loom-daemon-stop.sh"
    fi
    exit 0
fi

# ---------- Linux: systemd --user service (#4268) ----------
# The Linux mirror of the launchd path above: install a `systemd --user` unit and
# `enable --now` it so the daemon survives the launching shell's death and comes
# back on login (and, with `loginctl enable-linger`, after a reboot). Restart=
# on-success (rendered above) relaunches ONLY on a clean exit 0 -- the exact
# analog of KeepAlive:{SuccessfulExit:true} (#4054). Escape hatch: --no-systemd /
# LOOM_DAEMON_SYSTEMD=0 falls through to the nohup path below.
if [[ "$IS_LINUX_SYSTEMD" == "true" ]]; then
    SYSTEMD_UNIT="$(resolve_systemd_unit)"
    SYSTEMD_UNIT_DIR="$(resolve_systemd_unit_dir)"
    SYSTEMD_UNIT_PATH="$(resolve_systemd_unit_path)"
    mkdir -p "$SYSTEMD_UNIT_DIR"

    # Render to a scratch file first -- NOT directly over $SYSTEMD_UNIT_PATH --
    # so the dropped-env-key check (#4522) below can compare against whatever
    # $SYSTEMD_UNIT_PATH already contains before it gets clobbered, and so the
    # carry-forward merge (#5344) can rewrite $_UNIT_NEW_TMP in place BEFORE
    # it is installed.
    _UNIT_NEW_TMP="$(mktemp "$SYSTEMD_UNIT_DIR/.${SYSTEMD_UNIT}.new.XXXXXX")"
    render_systemd_unit "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG" > "$_UNIT_NEW_TMP"
    warn_dropped_env_keys "$SYSTEMD_UNIT_PATH" "$_UNIT_NEW_TMP" extract_systemd_env_keys extract_systemd_env_value inject_one_systemd_env_entry
    mv "$_UNIT_NEW_TMP" "$SYSTEMD_UNIT_PATH"

    # Harden the rendered unit when it carries a forwarded credential (#4005
    # analog): the env-forwarding loop in render_systemd_unit writes any exported
    # GH_TOKEN/GITEA_TOKEN/FORGE_TOKEN straight into Environment= lines, and the
    # plain `>` redirect otherwise leaves the file world-readable (0644).
    if env | grep -qE '^(GH_TOKEN|GITEA_TOKEN|FORGE_TOKEN)=' 2>/dev/null; then
        chmod 600 "$SYSTEMD_UNIT_PATH"
    fi

    echo "Systemd unit:   $SYSTEMD_UNIT"
    echo "Unit file:      $SYSTEMD_UNIT_PATH"

    # Reload so systemd picks up the freshly-rendered unit (a unit left from a
    # prior invocation, possibly with different flags/env, must not keep running
    # its OLD definition), then enable --now to install into default.target AND
    # start it in one step.
    systemctl --user daemon-reload >/dev/null 2>&1 || true

    ENABLE_ERR="$START_LOG.enable-err"
    if ! systemctl --user enable --now "$SYSTEMD_UNIT" 2>"$ENABLE_ERR"; then
        err "systemctl --user enable --now failed for $SYSTEMD_UNIT:"
        cat "$ENABLE_ERR" >&2 2>/dev/null || true
        rm -f "$ENABLE_ERR"
        exit 1
    fi
    rm -f "$ENABLE_ERR"

    # Give it a moment to either bind the socket or trip the singleton guard.
    sleep 2

    daemon_pid="$(systemctl --user show -p MainPID --value "$SYSTEMD_UNIT" 2>/dev/null)"
    if [[ -z "$daemon_pid" || "$daemon_pid" == "0" ]] || ! kill -0 "$daemon_pid" 2>/dev/null; then
        err "loom-daemon did not stay running under systemd ($SYSTEMD_UNIT)."
        if [[ -s "$START_LOG" ]]; then
            echo "----- startup output ($START_LOG) -----" >&2
            tail -n 20 "$START_LOG" >&2
            echo "---------------------------------------" >&2
        fi
        warn "If another daemon is already listening on the socket, stop it first"
        warn "(./.loom/scripts/cli/loom-daemon-stop.sh) and retry."
        exit 1
    fi

    # Redundant since #4774 -- the daemon claims $PID_FILE itself immediately
    # after its socket bind succeeds -- but kept deliberately. It costs nothing,
    # it closes the window between "supervisor reports a pid" and "the daemon
    # reaches its bind", and it is the only writer for a daemon binary older
    # than #4774 (a start script and a daemon roll independently). Harmless if
    # both write: same path, same pid, and the daemon's write is atomic.
    echo "$daemon_pid" > "$PID_FILE"
    # Record operator intent + arm the systemd-timer autonomy-loss watchdog
    # (#4011, #4260 sub-issue D). use_systemd=true + the resolved unit name
    # (#4862) let the watchdog probe `systemctl --user` for its own bounded
    # auto-remediation gate, mirroring the launchd job_loaded/kickstart path.
    write_intent_marker "false" "" "true" "$SYSTEMD_UNIT"
    provision_watchdog_job_systemd
    ok "loom-daemon started under systemd (pid $daemon_pid, unit $SYSTEMD_UNIT)."
    echo "PID file: $PID_FILE"
    echo "Intent marker: $INTENT_MARKER"
    print_safehouse_status
    print_calibrate_hint
    warn "Reboot survival requires lingering: run 'loginctl enable-linger \"\$USER\"' once (SSH-only / headless hosts)."
    if [[ "$MACHINE_MODE" == "true" ]]; then
        echo "Stop with: loom stop"
    else
        echo "Stop with: ./.loom/scripts/cli/loom-daemon-stop.sh"
    fi
    exit 0
fi

# ---------- Linux (non-systemd, or --no-launchd/--no-systemd): plain nohup ----------
nohup "$DAEMON_BIN" >> "$START_LOG" 2>&1 &
daemon_pid=$!

# Give it a moment to either bind the socket or trip the singleton guard.
sleep 2

if ! kill -0 "$daemon_pid" 2>/dev/null; then
    err "loom-daemon exited immediately after start (pid $daemon_pid)."
    if [[ -s "$START_LOG" ]]; then
        echo "----- startup output ($START_LOG) -----" >&2
        tail -n 20 "$START_LOG" >&2
        echo "---------------------------------------" >&2
    fi
    warn "If another daemon is already listening on the socket, stop it first"
    warn "(./.loom/scripts/cli/loom-daemon-stop.sh) and retry."
    exit 1
fi

# Redundant since #4774 -- the daemon claims $PID_FILE itself immediately
# after its socket bind succeeds -- but kept deliberately. It costs nothing,
# it closes the window between "supervisor reports a pid" and "the daemon
# reaches its bind", and it is the only writer for a daemon binary older
# than #4774 (a start script and a daemon roll independently). Harmless if
# both write: same path, same pid, and the daemon's write is atomic.
echo "$daemon_pid" > "$PID_FILE"
# Record operator intent (#4011). This is the nohup fallback tier (non-systemd
# Linux host, or --no-launchd/--no-systemd), so there is no scheduled checker to
# provision here — the marker + heartbeat are still written, and
# `loom-daemon-watchdog.sh` can be run by hand or wired to cron.
write_intent_marker "false" ""
provision_watchdog_job_none
ok "loom-daemon started (pid $daemon_pid). PID file: $PID_FILE"
echo "Intent marker: $INTENT_MARKER"
print_safehouse_status
print_calibrate_hint
if [[ "$MACHINE_MODE" == "true" ]]; then
    echo "Stop with: loom stop"
else
    echo "Stop with: ./.loom/scripts/cli/loom-daemon-stop.sh"
fi
exit 0
