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
#   ./.loom/scripts/cli/loom-daemon-start.sh --force-env     Suppress the dropped-env-key warning (#4522) for an intentional narrower re-render
#   ./.loom/scripts/cli/loom-daemon-start.sh --help
#
# Environment:
#   LOOM_DAEMON_BIN     Path to the loom-daemon binary (else auto-detected)
#   LOOM_SOCKET_PATH    Override the daemon socket (default ~/.loom/loom-daemon.sock)
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
#   1  usage error / binary not found / daemon failed to start

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

# warn_dropped_env_keys <old_file> <new_file> <extractor_function_name> — compare
# the env-var KEY sets (not values) between an already-installed plist/unit and
# a freshly-rendered replacement; warn (listing the keys) when the replacement
# DROPS a key the installed file carried. <extractor_function_name> is
# extract_plist_env_keys or extract_systemd_env_keys.
#
#   - A missing old_file (first-ever install -- nothing installed yet) is not a
#     drop: returns silently, no warning.
#   - --force-env (FORCE_ENV=true) acknowledges an intentional narrowing (e.g.
#     an explicit minimal re-render) and suppresses the warning.
#   - A dropped LOOM_SAFEHOUSE_* key gets a specific migration hint (the
#     "safehouse" block in .loom/config.json + --from-config, #4353) instead of
#     a generic warning.
warn_dropped_env_keys() {
    local old_file="$1" new_file="$2" extractor="$3"
    [[ -f "$old_file" ]] || return 0
    [[ "${FORCE_ENV:-false}" == "true" ]] && return 0

    local old_keys new_keys
    old_keys="$("$extractor" "$old_file" 2>/dev/null || true)"
    [[ -z "$old_keys" ]] && return 0
    new_keys="$("$extractor" "$new_file" 2>/dev/null || true)"

    local dropped=() k nk hit
    while IFS= read -r k; do
        [[ -z "$k" ]] && continue
        hit=false
        if [[ -n "$new_keys" ]]; then
            while IFS= read -r nk; do
                if [[ "$nk" == "$k" ]]; then hit=true; break; fi
            done <<< "$new_keys"
        fi
        [[ "$hit" == "false" ]] && dropped+=("$k")
    done <<< "$old_keys"

    [[ "${#dropped[@]}" -eq 0 ]] && return 0

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
    warn "Pass --force-env to acknowledge an intentional narrowing and suppress this warning."
}

# ---------- silent autonomy-downgrade detection (#4693) ----------
# Incident 2026-07-30: a routine loom-daemon-start.sh run (no flags) silently
# re-rendered the plist with LOOM_WORK_FINDER=0 -- downgrading a previously
# autonomous daemon to FLAGS-OFF with NO warning. ~3h of dispatch outage (23
# ready issues sat queued, "work availability is the limiter") before the
# missing "work_finder: starting" log line was traced back to the plist env.
#
# The FLAGS-OFF default for a PLAIN start (#3911) is correct and stays
# unchanged -- this only closes the SILENT part of a transition FROM
# autonomous TO FLAGS-OFF. Advisory only, exactly like warn_dropped_env_keys
# above: it never blocks the start.
#
# Signals consulted (either alone is sufficient to flag a downgrade):
#   1. the PRIOR installed plist/unit had the key ON (=1) -- direct evidence
#      this daemon was running autonomously a moment ago.
#   2. the autonomy-desired marker (#4011) is present but no prior plist/unit
#      value could be read (e.g. the first Darwin start after a nohup-only
#      history) -- the marker alone is recorded operator intent, and the
#      issue explicitly calls this combination out.
# When the prior value was already "0" (no transition) this stays silent --
# a standing marker-vs-FLAGS-OFF mismatch with no fresh transition is
# `loom-daemon status`'s job to flag (AC3), not this one-shot start-time check.
#
# Deliberately NOT triggered by:
#   - --from-config (control is explicitly handed to .loom/config.json --
#     not a silent default; see the FROM_CONFIG guard in the caller),
#   - an explicit --no-work-finder / --no-health-gate THIS invocation (an
#     explicit ask is not silent),
#   - an operator-exported LOOM_WORK_FINDER=0 / LOOM_MAIN_HEALTH_GATE=0 in the
#     calling shell (also an explicit, non-default signal -- "Respected when
#     already exported", see the Environment section in the help banner).
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

    if [[ "$old_val" == "1" ]]; then
        warn ""
        warn "WARNING: autonomy downgrade -- $key: 1 -> 0"
        warn "  The previously installed daemon had $key=1 (autonomous); this plain start"
        warn "  renders it OFF -- matching the FLAGS-OFF-by-default contract for a start with"
        warn "  no explicit flags (#3911), but SILENTLY from an operator's point of view."
        warn "  Remediation: pass --from-config (drive from .loom/config.json -> autonomous)"
        warn "  or --work-finder / --health-gate to keep autonomy on."
        return 0
    fi

    if [[ -z "$old_val" && "$marker_present" == "true" ]]; then
        warn ""
        warn "WARNING: autonomy downgrade -- $key renders 0 this start, and no prior plist/unit"
        warn "  value could be read -- but the autonomy-desired marker ($INTENT_MARKER) is"
        warn "  present, meaning this host previously ran loom-daemon autonomously."
        warn "  Remediation: pass --from-config (drive from .loom/config.json -> autonomous)"
        warn "  or --work-finder / --health-gate to keep autonomy on."
        return 0
    fi
}

# warn_autonomy_downgrade — evaluate both autonomy loops. Called once
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
# "preserves the current flag semantics").
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
#     invocation resolved -- never wider, never narrower. See
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
    return 0
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
# --force-env (#4522): acknowledges an intentional narrower re-render and
# suppresses warn_dropped_env_keys' warning. Script-only (like --print-plist),
# not a daemon autonomy flag -- excluded from the persisted .daemon.flags file
# below.
FORCE_ENV=false
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

DAEMON_BIN=$(loom_locate_daemon_bin "$REPO_ROOT")
if [[ -z "$DAEMON_BIN" ]]; then
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
mkdir -p "$DAEMON_STATE_HOME/logs"

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

# ---------- already-running guard (PID file) ----------
if [[ -f "$PID_FILE" ]]; then
    existing_pid=$(cat "$PID_FILE" 2>/dev/null || true)
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
        warn "loom-daemon already running (pid $existing_pid, per $PID_FILE)."
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
# Children inherit these through the daemon's process environment.
export LOOM_GUARD_DECISION_LOG="${LOOM_GUARD_DECISION_LOG:-1}"
export LOOM_FORCE_SCOPE="${LOOM_FORCE_SCOPE:-protected}"

if [[ "$FROM_CONFIG" == "true" ]]; then
    # Compose (#4353): --from-config alone leaves BOTH vars unset for config to
    # drive. An explicit --work-finder/--no-work-finder (or the health-gate
    # equivalent) additionally FORCES that one var -- using the
    # ${VAR:-default} form so an already-exported env var still wins over the
    # CLI flag, exactly like the non-config branch below. The loop with no
    # explicit flag is left untouched (stays unset, config drives it).
    FORCED_DESC=()
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
        echo -e "${BOLD}Autonomous mode: driven by .loom/config.json -> autonomous (env not forced)${NC}"
    else
        FORCED_JOINED="$(IFS=', '; echo "${FORCED_DESC[*]}")"
        echo -e "${BOLD}Autonomous mode: config-driven; forced: ${FORCED_JOINED}${NC}"
    fi
    unset FORCED_DESC FORCED_JOINED
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
        echo -e "${BOLD}Reliability daemon:${NC} work_finder=off main_health_gate=off (both loops OFF; opt in with --work-finder / --health-gate / --from-config)"
    else
        echo -e "${BOLD}Autonomous mode:${NC} work_finder=${LOOM_WORK_FINDER} main_health_gate=${LOOM_MAIN_HEALTH_GATE}"
    fi
fi

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
    exec "$DAEMON_BIN"
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
# install below (and --print-plist/--print-unit) use, so "prior" always means
# "whatever is installed under the identifier THIS invocation would overwrite".
# Left empty on the nohup fallback tier (no rendered file exists there) -- the
# autonomy-desired marker alone is the only available signal in that case
# (see check_autonomy_downgrade_key above).
#
# The mechanism this comparison targets is chosen by the INVOCATION, not by the
# host OS: --print-plist / --print-unit are pure inspection modes that render
# (and inspect) their mechanism's file regardless of the platform running them,
# exactly like the pre-existing --print-plist PATH-drift (#4172) and
# dropped-env-key (#4522) checks below, which read
# $HOME/Library/LaunchAgents/<label>.plist unconditionally. Gating this
# resolution on USE_LAUNCHD (Darwin-only) instead made the whole downgrade
# warning silently unreachable under --print-plist on any Linux host -- the
# exact silence this check exists to eliminate. Only when NEITHER inspection
# flag is set does platform detection pick the mechanism, which keeps the real
# install path (and its nohup-tier "leave empty" contract) byte-identical.
PRIOR_AUTONOMY_MECH=""
if [[ "$PRINT_PLIST" == "true" ]]; then
    PRIOR_AUTONOMY_MECH="launchd"
elif [[ "$PRINT_UNIT" == "true" ]]; then
    PRIOR_AUTONOMY_MECH="systemd"
elif [[ "$USE_LAUNCHD" == "true" ]]; then
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

# Run BEFORE any of --print-plist / --print-unit / the real install below, so
# an operator sees the warning whether they are just inspecting or actually
# starting -- and before the prior file gets overwritten either way.
warn_autonomy_downgrade

# ---------- --print-plist: pure inspection, no side effects ----------
if [[ "$PRINT_PLIST" == "true" ]]; then
    _plist_rendered="$(render_launchd_plist "$(resolve_launchd_label)" "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG")"
    printf '%s\n' "$_plist_rendered"
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
        # Dropped-env-key check (#4522): read-only inspection counterpart of
        # the same check the real install path below runs before overwriting.
        _plist_new_tmp="$(mktemp "${TMPDIR:-/tmp}/loom-print-plist.XXXXXX")"
        printf '%s\n' "$_plist_rendered" > "$_plist_new_tmp"
        warn_dropped_env_keys "$_live_plist" "$_plist_new_tmp" extract_plist_env_keys
        rm -f "$_plist_new_tmp"
    fi
    exit 0
fi

# ---------- --print-unit: pure inspection, no side effects (#4268) ----------
if [[ "$PRINT_UNIT" == "true" ]]; then
    _unit_rendered="$(render_systemd_unit "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG")"
    printf '%s\n' "$_unit_rendered"
    # Dropped-env-key check (#4522): read-only inspection counterpart of the
    # same check the real install path below runs before overwriting.
    _live_unit=""
    if declare -f resolve_systemd_unit_path >/dev/null 2>&1; then
        _live_unit="$(resolve_systemd_unit_path 2>/dev/null || true)"
    fi
    if [[ -n "$_live_unit" && -f "$_live_unit" ]]; then
        _unit_new_tmp="$(mktemp "${TMPDIR:-/tmp}/loom-print-unit.XXXXXX")"
        printf '%s\n' "$_unit_rendered" > "$_unit_new_tmp"
        warn_dropped_env_keys "$_live_unit" "$_unit_new_tmp" extract_systemd_env_keys
        rm -f "$_unit_new_tmp"
    fi
    exit 0
fi

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
    # $PLIST_FILE already contains before it gets clobbered.
    _PLIST_NEW_TMP="$(mktemp "$PLIST_DIR/.${LAUNCHD_LABEL}.new.XXXXXX")"
    render_launchd_plist "$LAUNCHD_LABEL" "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG" > "$_PLIST_NEW_TMP"
    warn_dropped_env_keys "$PLIST_FILE" "$_PLIST_NEW_TMP" extract_plist_env_keys
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
    # $SYSTEMD_UNIT_PATH already contains before it gets clobbered.
    _UNIT_NEW_TMP="$(mktemp "$SYSTEMD_UNIT_DIR/.${SYSTEMD_UNIT}.new.XXXXXX")"
    render_systemd_unit "$DAEMON_BIN" "$REPO_ROOT" "$START_LOG" > "$_UNIT_NEW_TMP"
    warn_dropped_env_keys "$SYSTEMD_UNIT_PATH" "$_UNIT_NEW_TMP" extract_systemd_env_keys
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
