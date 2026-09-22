#!/usr/bin/env bash
# live-state-sandbox.sh — ONE shared "sandbox every live daemon state path"
# helper for the loom-daemon lifecycle test suites (issue #5179).
#
# ## Why this exists
#
# The daemon lifecycle suites deliberately execute the REAL
# loom-daemon-start.sh / loom-daemon-stop.sh / loom-daemon-update.sh (not
# mocks), so every state path those scripts resolve is a path the test can
# write. Four separate incidents were each fixed by enumerating ONE more
# surface after it leaked:
#
#   1. #4078/#4087 — a daemon test booted out the operator's PRODUCTION daemon
#      (launchd label + label-blind pgrep; fixed in lib/launchd-sandbox.sh).
#   2. #5131 — the live `autonomy-desired` marker was removed under a running
#      daemon (fixed by a per-suite LOOM_AUTONOMY_MARKER export).
#   3. #5179 — the live `.daemon.pid` was rewritten under a running daemon,
#      producing a FALSE `degraded` liveness verdict for the operator and
#      poisoning the watchdog's (#5118/#5126) primary input.
#   4. #5501 — a harness fully sandboxed on every STATE PATH still stopped the
#      operator's real daemon, because it deliberately pointed LOOM_LAUNCHD_LABEL
#      at the real `com.rjwalters.loom-daemon` label (to prove "default label =
#      the operator's real stop" was unchanged) — sandboxing paths does not
#      sandbox SUPERVISOR IDENTITY, which is a different axis entirely. See
#      "Supervisor IDENTITY" below.
#   5. #8077 — a builder SWEEP's test run leaked into the live host twice over:
#      17 test-spawned daemons wrote full boot blocks into the PRODUCTION
#      `~/.loom/daemon.log` (and one of them adopted the real host's in-flight
#      sweep claim out of the machine sweep journal), and a real-systemd
#      regression block ran 32 × `systemctl --user daemon-reload` against the
#      live user manager supervising that same daemon. Root cause: the daemon's
#      own systemd unit exports `LOOM_SOCKET_PATH=$HOME/.loom/loom-daemon.sock`,
#      every sweep worker the daemon spawns INHERITS it, and `resolve_loom_dir()`
#      takes that variable's PARENT as the loom dir — so a test daemon resolves
#      the production `daemon.log`/`sweeps.json` even when the test sandboxed
#      `$HOME`. See "The #8077 live-host leak guard" below.
#
# The recurring root cause is not "one more variable was forgotten"; it is that
# a Loom agent session EXPORTS the live paths into every child process it
# spawns (observed on a worker host: `LOOM_PID_FILE=$HOME/.loom/.daemon.pid`,
# `LOOM_WORKSPACE=<the real checkout>`, `LOOM_DAEMON_BIN=<the real binary>`).
# A test suite that merely *omits* an override therefore does not get a neutral
# default — it INHERITS the production path, and `LOOM_PID_FILE` is tier 1 of
# the daemon's own resolver (`daemon_pidfile.rs`), ahead of every other tier.
# #4902 fixed exactly this shape for LOOM_DAEMON_BIN; this helper generalizes
# it so a state path added to the daemon later is isolated by construction.
#
# ## What it covers
#
# `live_state_sandbox_init` owns the filesystem STATE PATHS:
#
#   LOOM_PID_FILE          -> <dir>/.daemon.pid          (daemon_pidfile.rs tier 1)
#   LOOM_AUTONOMY_MARKER   -> <dir>/autonomy-desired     (#4011/#5131)
#   LOOM_SOCKET_PATH       -> <dir>/loom-daemon.sock     (its parent is the
#                             daemon's `loom_dir`, so the heartbeat, machine-level
#                             logs and every `resolve_loom_dir()` consumer follow)
#   LOOM_DAEMON_BIN_DIR    -> <dir>/machine-level-bin-sandbox (#4381)
#   LOOM_DAEMON_LOG        -> <dir>/daemon.log           (#8077: the HIGHEST-
#                             precedence log override, ahead of the
#                             LOOM_SOCKET_PATH-derived default, so a daemon
#                             spawned with a per-invocation socket pin of its
#                             own still cannot reach ~/.loom/daemon.log)
#   LOOM_SHARED_TOKENS_DIR -> <dir>/tokens               (#8077: else the pool
#                             defaults to $HOME/.loom/tokens)
#   LOOM_WORKSPACES_PATH   -> <dir>/workspaces.json      (#8077/#4556: the real
#                             registry lists the operator's 48 repos, so an
#                             un-pinned test daemon reconciles against them)
#   LOOM_SWEEPS_JOURNAL_PATH -> <dir>/sweeps.json        (#8077/#4556: the MACHINE
#                             sweep journal — a test daemon that reads it adopts
#                             the real host's in-flight sweep claims)
#   LOOM_WATCHES_PATH      -> <dir>/watches.json         (#4556)
#   LOOM_WATCH_RESULTS_LOG -> <dir>/watch-results.log    (#4556)
#   LOOM_DAEMON_BIN        -> UNSET  (#4902: the ambient value names the REAL binary)
#   LOOM_WORKSPACE         -> UNSET  (pid-file tier 3; each sub-invocation must
#                             resolve its OWN fixture repo root instead)
#   LOOM_MACHINE_CHECKOUT  -> UNSET  (pid-file tier 2 + the scripts' machine
#                             mode, whose state home is the real $HOME/.loom)
#   $PWD                   -> <dir>  (#6386: NOT an env override — the OTHER
#                             resolution tier. `find_repo_root` walks up from
#                             the cwd, so a suite run from the live checkout
#                             hands every un-`cd`-ed sub-invocation the LIVE
#                             `.loom` as its state home. <dir> is given its own
#                             `.loom/` so it is a valid workspace root.)
#
# Supervisor IDENTITY (launchd label, systemd unit) is NOT a filesystem state
# path, so it is not fingerprinted the way the paths above are — the syscall-
# level stubbing lives in lib/launchd-sandbox.sh. But `live_state_sandbox_init`
# / `live_state_sandbox_assert_untouched` DO assert it, below: a sandbox that
# only isolated paths would have missed #5501 (a harness clean on every path
# still stopped the real daemon via a real-looking LOOM_LAUNCHD_LABEL). See
# `live_state_sandbox_assert_supervisor_scoped`.
#
# ## The guard
#
# Isolation alone is unfalsifiable — the previous fixes all *looked*
# complete. `live_state_sandbox_snapshot` fingerprints every live state path
# reachable from the AMBIENT environment BEFORE the sandbox is installed, and
# `live_state_sandbox_assert_untouched` re-fingerprints them at the end of the
# run, so a write to a real `.loom` state path (or the CREATION of one that was
# absent) fails the suite LOUDLY instead of being discovered in production.
# `live_state_sandbox_init` and `live_state_sandbox_assert_untouched` ALSO call
# `live_state_sandbox_assert_supervisor_scoped` — sandboxing paths is
# necessary but not sufficient (#5501): a test whose LOOM_LAUNCHD_LABEL /
# LOOM_WATCHDOG_LABEL resolve to the real production identity is not
# sandboxed, no matter how clean its state paths are.
#
# A test that genuinely needs to prove "default label = the operator's real
# stop, behaviour unchanged" must NOT do it by pointing this sandbox at the
# real label — that IS the #5501 incident shape. Use the
# `LOOM_DAEMON_STOP_DRYRUN=1` seam in loom-daemon-stop.sh instead: it resolves
# the real label/unit for inspection but never issues the real `launchctl
# bootout` / `systemctl --user disable --now` / `kill` against whatever it
# finds, so the assertion below is deliberately bypassed while it is active.
#
# Only files a healthy daemon does NOT continuously rewrite are guarded
# (see LIVE_STATE_SANDBOX_GUARDED_FILES): `daemon.heartbeat`, `daemon.log`,
# `sweeps.json` and `activity.db` are deliberately excluded because a live
# daemon updates them on its own cadence, which would make the guard flap.
#
# ## The #8077 live-host leak guard
#
# `live_host_leak_snapshot` / `live_host_leak_assert_unchanged` are a SECOND,
# independent pair, usable on their own (run-ci-suites.sh drives them around the
# WHOLE shell-suite run, where the state-path pair above would flap on a
# supervised daemon restart). They watch the three surfaces #8077 actually
# damaged, each fingerprinted at a granularity a healthy daemon does not move:
#
#   1. daemon.log BOOT COUNT — the number of `Daemon logging initialized` lines.
#      A daemon writes exactly one per process start, so the count is constant
#      for a running daemon however much it appends. This is why counting boots
#      works where a size/mtime/sha fingerprint of `daemon.log` cannot.
#   2. The supervised unit's restart identity (`NRestarts` +
#      `ExecMainStartTimestampMonotonic`). auto_update legitimately rolls the
#      daemon every ~90 min, which DOES add a boot line — so a boot-count growth
#      is only reported as a FAILURE when the unit's identity is unchanged (no
#      supervised restart could account for it). That is the exact #8077
#      signature: 17 boot blocks, zero unit restarts. When the unit did restart,
#      the growth is reported as ADVISORY instead of failing, because it cannot
#      be attributed either way — a guard that flaps every 90 min gets disabled,
#      which is worse than one that occasionally says "unattributable".
#   3. The `$HOME/.config/systemd/user` unit-file NAME SET and the
#      `$HOME/.loom/tokens` `*.token` NAME SET. Both are deterministic: the
#      daemon rewrites unit-file CONTENT on an auto_update roll and rewrites
#      `.ranking` continuously, but neither adds or removes a name. A test that
#      drops real unit files into the live manager's search path (#4862's MX
#      block) or writes a token into the live pool is caught here.
#
# ## Usage (source it)
#
#   source "$SCRIPT_DIR/lib/live-state-sandbox.sh"
#   live_state_sandbox_snapshot                      # BEFORE any sandbox export
#   ...
#   live_state_sandbox_init "$BASE_WORKDIR/live-state"
#   ... run the suite ...
#   live_state_sandbox_assert_untouched              # rc 0 = clean, 1 = leaked
#
# …or, for a run that must NOT install a sandbox (an outer runner wrapping many
# suites), the #8077 pair on its own:
#
#   live_host_leak_snapshot
#   ... run everything ...
#   live_host_leak_assert_unchanged                  # rc 0 = clean, 1 = leaked

# State files that identify/steer a daemon and are written only at lifecycle
# transitions — safe to compare before/after even while a real daemon runs.
LIVE_STATE_SANDBOX_GUARDED_FILES=".daemon.pid .daemon.flags autonomy-desired"

# The systemd --user unit name the production daemon is supervised by. Used by
# the #8077 leak guard below to tell an EXPECTED supervised restart (auto_update
# rolls the daemon every ~90 min) apart from a test-spawned daemon.
LIVE_STATE_SANDBOX_PRODUCTION_SYSTEMD_UNIT="${LIVE_STATE_SANDBOX_PRODUCTION_SYSTEMD_UNIT:-loom-daemon}"

# The real production supervisor identities (#5501). ANY test that resolves
# either of these while a live-state sandbox is active can reach out and
# stop/disable the operator's REAL supervised job — see
# live_state_sandbox_assert_supervisor_scoped below.
LIVE_STATE_SANDBOX_PRODUCTION_LAUNCHD_LABEL="com.rjwalters.loom-daemon"
LIVE_STATE_SANDBOX_PRODUCTION_WATCHDOG_LABEL="com.rjwalters.loom-daemon-watchdog"

# Newline-separated "<path><TAB><fingerprint>" records captured by
# live_state_sandbox_snapshot. Empty until the snapshot runs.
_LSS_SNAPSHOT=""
_LSS_SNAPSHOT_TAKEN=0

# Newline-separated "<kind>:<key><TAB><fingerprint>" records captured by
# live_host_leak_snapshot (#8077). Empty until that snapshot runs.
_LSS_LEAK_SNAPSHOT=""
_LSS_LEAK_SNAPSHOT_TAKEN=0

# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

# Print <path>'s mtime as an epoch second, portably: GNU `stat -c %Y` first,
# then BSD/macOS `stat -f %m`, else `?`. Each result is validated as digits-only
# BEFORE it is accepted — GNU stat's `-f` is "filesystem status" (it SUCCEEDS
# with multi-line output rather than failing), so a naive `-f || -c` chain
# silently injects newlines into the fingerprint and makes every record
# unparseable.
_lss_mtime() {
    local m
    m=$(stat -c %Y "$1" 2>/dev/null)
    case "$m" in ''|*[!0-9]*) m=$(stat -f %m "$1" 2>/dev/null) ;; esac
    case "$m" in ''|*[!0-9]*) m="?" ;; esac
    printf '%s' "$m"
}

# Print <path>'s sha256, using whichever checksum tool exists (mirrors the
# #4381 production-binary guard's tool selection).
_lss_checksum() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" 2>/dev/null | awk '{print $1}'
    else
        echo "<no-checksum-tool>"
    fi
}

# Print a stable fingerprint of <path>: `<absent>` when it does not exist,
# `<non-regular>` for sockets/dirs (content is meaningless there), else
# size + mtime + checksum so ANY rewrite — including one that restores the
# same bytes at a later mtime — is visible.
_lss_fingerprint() {
    local path="$1"
    [[ -e "$path" ]] || { echo "<absent>"; return 0; }
    [[ -f "$path" ]] || { echo "<non-regular>"; return 0; }
    local size
    size=$(wc -c < "$path" 2>/dev/null | tr -d ' ')
    echo "size=${size:-?} mtime=$(_lss_mtime "$path") sha=$(_lss_checksum "$path")"
}

# ---- #8077 live-host leak fingerprints -------------------------------------

# Print the number of daemon BOOT BLOCKS in <log>: `<absent>` when the file does
# not exist, else the count of `Daemon logging initialized` lines. The daemon
# writes exactly one of those per process start (daemon_service.rs
# `setup_logging`), so this number does NOT move while a daemon runs, however
# much it appends — which is precisely why `daemon.log` can be guarded this way
# when a size/mtime/sha fingerprint of it would flap every second.
_lss_daemon_log_boots() {
    local path="$1" n
    [[ -f "$path" ]] || { printf '%s' '<absent>'; return 0; }
    # `grep -c` exits 1 on zero matches, so its rc is deliberately ignored; the
    # digits-only validation below is what rejects a genuinely failed read.
    n=$(grep -c 'Daemon logging initialized' "$path" 2>/dev/null)
    case "$n" in ''|*[!0-9]*) n='?' ;; esac
    printf '%s' "$n"
}

# Print (one per line) every `daemon.log` a daemon spawned from the CURRENT
# environment could resolve — the ambient LOOM_DAEMON_LOG, the $HOME default,
# and the LOOM_SOCKET_PATH-derived one. The last of these is the #8077 path: a
# sweep worker inherits the production daemon's own `LOOM_SOCKET_PATH` from its
# systemd unit, so the "default" a forgetful test gets is the LIVE log.
_lss_enumerate_live_daemon_logs() {
    {
        printf '%s\n' "${LOOM_DAEMON_LOG:-}"
        printf '%s\n' "$HOME/.loom/daemon.log"
        if [[ -n "${LOOM_SOCKET_PATH:-}" ]]; then
            printf '%s\n' "$(dirname "$LOOM_SOCKET_PATH")/daemon.log"
        fi
    } | awk 'NF' | sort -u
}

# Print the live systemd --user unit's restart identity, or a `<...>` reason
# token when no user manager is reachable (Darwin, a CI runner with no user
# session/bus, or no systemctl at all). `NRestarts` alone is not enough — it
# counts only AUTOMATIC restarts, not an explicit `systemctl restart` — so the
# main-process start timestamp is carried alongside it.
# shellcheck disable=SC2120  # <unit> is an override seam for this lib's own test suite; in-repo callers take the default.
_lss_systemd_unit_identity() {
    local unit="${1:-$LIVE_STATE_SANDBOX_PRODUCTION_SYSTEMD_UNIT}" restarts started
    command -v systemctl >/dev/null 2>&1 || { printf '%s' '<no-systemctl>'; return 0; }
    [[ -n "${XDG_RUNTIME_DIR:-}" ]] || { printf '%s' '<no-user-manager>'; return 0; }
    restarts=$(systemctl --user show -p NRestarts --value "$unit" 2>/dev/null)
    started=$(systemctl --user show -p ExecMainStartTimestampMonotonic --value "$unit" 2>/dev/null)
    case "$restarts" in ''|*[!0-9]*) restarts='?' ;; esac
    case "$started" in ''|*[!0-9]*) started='?' ;; esac
    printf 'restarts=%s started=%s' "$restarts" "$started"
}

# Print the sorted NAME SET of a directory's entries (matching <glob>, default
# everything), or `<absent>`. Names only: the daemon rewrites unit-file CONTENT
# on an auto_update roll and rewrites the token pool's `.ranking` continuously,
# so a content fingerprint would flap — but neither operation ADDS or REMOVES a
# name, which is the thing a leaking test does.
#
# An empty match set prints as `<absent>` too, whether or not the directory
# itself exists — a `mkdir -p` that a test's OWN cleanup leaves behind (e.g.
# the #4862 MX block creating `$HOME/.config/systemd/user` before writing,
# then removing, its unit files) is not a leak. A leak is a NAME appearing
# that was not there before; the directory's own existence is not the thing
# being guarded (#8077 CI false positive: before=`<absent>`, after=`names=`).
_lss_dir_name_set() {
    local dir="$1" glob="${2:-*}" names
    [[ -d "$dir" ]] || { printf '%s' '<absent>'; return 0; }
    names=$(
        cd "$dir" 2>/dev/null || exit 0
        # shellcheck disable=SC2086  # deliberate glob expansion of the caller's pattern
        for entry in $glob; do
            [[ -e "$entry" ]] || continue
            printf '%s\n' "$entry"
        done | sort | tr '\n' ','
    )
    [[ -z "$names" ]] && { printf '%s' '<absent>'; return 0; }
    printf 'names=%s' "$names"
}

# Walk up from <dir> to the nearest repo root, mirroring the `find_repo_root()`
# in loom-daemon-{start,stop,update}.sh (a `.git` OR `.loom` directory). Prints
# the root; returns 1 when none is found.
_lss_repo_root_from() {
    local dir="$1"
    while [[ -n "$dir" && "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" || -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

# Print (one per line) every live state path reachable from the CURRENT
# environment. Called only from live_state_sandbox_snapshot, i.e. while the
# environment is still the ambient/production one.
_lss_enumerate_live_paths() {
    local root file

    # Explicit ambient overrides — the highest-precedence tiers, and the exact
    # vars a Loom agent session exports at the REAL paths (#5179).
    if [[ -n "${LOOM_PID_FILE:-}" ]]; then printf '%s\n' "$LOOM_PID_FILE"; fi
    if [[ -n "${LOOM_AUTONOMY_MARKER:-}" ]]; then printf '%s\n' "$LOOM_AUTONOMY_MARKER"; fi

    # State roots the scripts/daemon resolve on their own.
    {
        printf '%s\n' "$HOME/.loom"
        if [[ -n "${LOOM_SOCKET_PATH:-}" ]]; then printf '%s\n' "$(dirname "$LOOM_SOCKET_PATH")"; fi
        if [[ -n "${LOOM_WORKSPACE:-}" ]]; then printf '%s\n' "$LOOM_WORKSPACE/.loom"; fi
        if [[ -n "${LOOM_MACHINE_CHECKOUT:-}" ]]; then printf '%s\n' "$LOOM_MACHINE_CHECKOUT/.loom"; fi
        # $PWD's repo root: the un-sandboxed cwd is how a sub-invocation that
        # forgets to `cd` into its fixture resolves REPO_ROOT — and therefore
        # DAEMON_STATE_HOME="$REPO_ROOT/.loom" — straight onto the live checkout.
        if root="$(_lss_repo_root_from "$PWD")"; then printf '%s\n' "$root/.loom"; fi
        # The checkout this helper itself lives in (the suite may be launched
        # from anywhere, e.g. `bash /path/to/checkout/defaults/scripts/tests/...`).
        if root="$(_lss_repo_root_from "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"; then
            printf '%s\n' "$root/.loom"
        fi
    } | while IFS= read -r root; do
        [[ -n "$root" ]] || continue
        for file in $LIVE_STATE_SANDBOX_GUARDED_FILES; do
            printf '%s\n' "$root/$file"
        done
    done
}

# Fail loudly (rc 1) when LOOM_LAUNCHD_LABEL / LOOM_WATCHDOG_LABEL resolve to
# the REAL production supervisor identity (#5501) — the axis a path-only
# sandbox cannot see: nothing writes to a `.loom` path when the damage is done
# through `launchctl bootout` / `systemctl --user disable --now` against a
# label, not a file.
#
# Args (both optional; default to the CURRENT ambient value of each var, which
# is what a harness that `export`s them ahead of live_state_sandbox_init — the
# exact #5501 incident shape — will trip): [<launchd_label>] [<watchdog_label>].
#
# Bypassed while LOOM_DAEMON_STOP_DRYRUN is truthy: that seam never issues the
# real bootout/disable/kill no matter what label it resolves, so exercising
# default-label semantics under it is the SUPPORTED path, not the incident.
#
# Deliberately does NOT fail merely because a label is unset — most call sites
# in this repo never export LOOM_LAUNCHD_LABEL into their own shell at all,
# scoping it per-invocation instead (`LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash
# "$STOP_SCRIPT"`), which this function cannot observe from the parent
# process. Only a label that is VISIBLY the real one is something this helper
# can catch — see the header comment for the seam that closes the rest.
# shellcheck disable=SC2120  # OK that in-repo callers pass args; test files source this lib and pass their own.
live_state_sandbox_assert_supervisor_scoped() {
    local label="${1-${LOOM_LAUNCHD_LABEL:-}}"
    local wd_label="${2-${LOOM_WATCHDOG_LABEL:-}}"
    if [[ "${LOOM_DAEMON_STOP_DRYRUN:-}" =~ ^(1|true|yes|on)$ ]]; then
        return 0
    fi
    local bad=0
    if [[ "$label" == "$LIVE_STATE_SANDBOX_PRODUCTION_LAUNCHD_LABEL" ]]; then
        echo "live-state-sandbox: LOOM_LAUNCHD_LABEL is set to the REAL production launchd label '$LIVE_STATE_SANDBOX_PRODUCTION_LAUNCHD_LABEL' while a sandbox is active (#5501)." >&2
        echo "  Sandboxing state PATHS does not sandbox supervisor IDENTITY -- this can reach out and" >&2
        echo "  'launchctl bootout'/kill the operator's REAL daemon. Use a scratch label (see" >&2
        echo "  lib/launchd-sandbox.sh's launchd_sandbox_new_label), or, to prove default-label" >&2
        echo "  semantics are unchanged, set LOOM_DAEMON_STOP_DRYRUN=1 on the loom-daemon-stop.sh" >&2
        echo "  invocation under test instead of pointing this at the real label." >&2
        bad=1
    fi
    if [[ -n "$wd_label" && "$wd_label" == "$LIVE_STATE_SANDBOX_PRODUCTION_WATCHDOG_LABEL" ]]; then
        echo "live-state-sandbox: LOOM_WATCHDOG_LABEL is set to the REAL production watchdog label '$LIVE_STATE_SANDBOX_PRODUCTION_WATCHDOG_LABEL' while a sandbox is active (#5501)." >&2
        bad=1
    fi
    return "$bad"
}

# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

# Fingerprint every live state path. MUST be called BEFORE
# live_state_sandbox_init (it reads the ambient LOOM_* vars to discover which
# paths are the live ones) and before the suite runs anything.
live_state_sandbox_snapshot() {
    local path
    _LSS_SNAPSHOT=""
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        _LSS_SNAPSHOT="${_LSS_SNAPSHOT}${path}"$'\t'"$(_lss_fingerprint "$path")"$'\n'
    done < <(_lss_enumerate_live_paths | sort -u)
    _LSS_SNAPSHOT_TAKEN=1
    # The #8077 surfaces are a different fingerprint granularity (boot counts,
    # supervisor identity, directory name sets), so they get their own snapshot
    # — taken here too, so every existing caller of this function is covered
    # without a second call site to forget.
    live_host_leak_snapshot
}

# Fingerprint the live-HOST surfaces #8077 damaged: every reachable
# `daemon.log`'s boot count, the supervised unit's restart identity, the
# systemd --user unit-file name set, and the shared token pool's name set.
# MUST be called before the run under test starts (and, when used together with
# live_state_sandbox_init, before it — init deliberately repoints
# LOOM_DAEMON_LOG, which changes what "reachable" means).
live_host_leak_snapshot() {
    local path
    _LSS_LEAK_SNAPSHOT=""
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        _LSS_LEAK_SNAPSHOT="${_LSS_LEAK_SNAPSHOT}daemon-log:${path}"$'\t'"$(_lss_daemon_log_boots "$path")"$'\n'
    done < <(_lss_enumerate_live_daemon_logs)
    _LSS_LEAK_SNAPSHOT="${_LSS_LEAK_SNAPSHOT}systemd-unit:$LIVE_STATE_SANDBOX_PRODUCTION_SYSTEMD_UNIT"$'\t'"$(_lss_systemd_unit_identity)"$'\n'
    _LSS_LEAK_SNAPSHOT="${_LSS_LEAK_SNAPSHOT}systemd-units-dir:${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"$'\t'"$(_lss_dir_name_set "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user")"$'\n'
    _LSS_LEAK_SNAPSHOT="${_LSS_LEAK_SNAPSHOT}token-pool:$HOME/.loom/tokens"$'\t'"$(_lss_dir_name_set "$HOME/.loom/tokens" '*.token')"$'\n'
    _LSS_LEAK_SNAPSHOT_TAKEN=1
}

# Print how many live-host surfaces the #8077 snapshot covers (0 before one).
live_host_leak_snapshot_size() {
    [[ -n "$_LSS_LEAK_SNAPSHOT" ]] || { echo 0; return 0; }
    printf '%s' "$_LSS_LEAK_SNAPSHOT" | grep -c ''
}

# Re-fingerprint the #8077 surfaces. Returns 0 when nothing a test could have
# done has changed, 1 when it has — naming each offender on stderr.
#
# The one non-obvious rule is the daemon.log boot count. A boot line appearing
# while the supervised unit ALSO restarted is an auto_update roll, not a leak:
# reported as ADVISORY on stderr, rc unaffected. A boot line appearing with the
# unit's restart identity UNCHANGED is a daemon nothing supervised started —
# the #8077 signature (17 boot blocks, zero unit restarts) — and fails. A log
# that was ABSENT and now exists always fails: nothing legitimate creates the
# operator's daemon log during a test run, and that is the only direction a CI
# runner (no daemon, no user manager) can observe.
live_host_leak_assert_unchanged() {
    local line key before after dirty=0 unit_moved=0
    if [[ "$_LSS_LEAK_SNAPSHOT_TAKEN" != "1" ]]; then
        echo "live-state-sandbox: no #8077 leak snapshot taken — call live_host_leak_snapshot first" >&2
        return 1
    fi
    # Resolve the supervised-restart verdict FIRST: it decides how a boot-count
    # growth below is classified, so it cannot be discovered mid-loop.
    while IFS= read -r line; do
        [[ "$line" == systemd-unit:* ]] || continue
        before="${line#*$'\t'}"
        after="$(_lss_systemd_unit_identity)"
        [[ "$before" != "$after" ]] && unit_moved=1
    done <<< "$_LSS_LEAK_SNAPSHOT"

    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        key="${line%%$'\t'*}"
        before="${line#*$'\t'}"
        case "$key" in
            daemon-log:*)
                after="$(_lss_daemon_log_boots "${key#daemon-log:}")"
                [[ "$before" == "$after" ]] && continue
                if [[ "$before" != "<absent>" && "$unit_moved" == "1" ]]; then
                    printf 'live-state-sandbox: ADVISORY — %s gained boot block(s) (%s -> %s) while the supervised unit ALSO restarted; unattributable, not failing (#8077)\n' \
                        "${key#daemon-log:}" "$before" "$after" >&2
                    continue
                fi
                dirty=1
                printf 'live-state-sandbox: a daemon that nothing supervised started wrote into the LIVE daemon log: %s\n' "${key#daemon-log:}" >&2
                printf '    boot blocks before: %s   after: %s   (supervised unit restart: no)\n' "$before" "$after" >&2
                printf '    A test spawned a real loom-daemon without pinning LOOM_DAEMON_LOG / LOOM_SOCKET_PATH.\n' >&2
                printf '    Note the sweep environment INHERITS the production LOOM_SOCKET_PATH, so omitting an\n' >&2
                printf '    override yields the LIVE path, not a neutral default (#8077).\n' >&2
                ;;
            systemd-units-dir:*)
                after="$(_lss_dir_name_set "${key#systemd-units-dir:}")"
                [[ "$before" == "$after" ]] && continue
                dirty=1
                printf 'live-state-sandbox: the LIVE systemd --user unit directory gained/lost unit files during this run: %s\n' "${key#systemd-units-dir:}" >&2
                printf '    before: %s\n    after:  %s\n' "$before" "$after" >&2
                printf '    A test wrote real unit files into the search path of the live manager; gate it behind\n' >&2
                printf '    LOOM_TEST_ALLOW_SYSTEMD=1 instead (#8077).\n' >&2
                ;;
            token-pool:*)
                after="$(_lss_dir_name_set "${key#token-pool:}" '*.token')"
                [[ "$before" == "$after" ]] && continue
                dirty=1
                printf 'live-state-sandbox: the LIVE shared token pool gained/lost token files during this run: %s\n' "${key#token-pool:}" >&2
                printf '    before: %s\n    after:  %s\n' "$before" "$after" >&2
                printf '    Pin LOOM_SHARED_TOKENS_DIR at a scratch directory (#8077).\n' >&2
                ;;
        esac
    done <<< "$_LSS_LEAK_SNAPSHOT"
    return "$dirty"
}

# Redirect every live state path into <dir>. Exports are inherited by every
# sub-invocation, so a call site that forgets to sandbox something still cannot
# reach a production path.
#
# ALSO `cd`s the calling shell into <dir> (#6386). Env exports only cover the
# paths a script reads from the environment; the lifecycle scripts' OTHER
# resolution tier is `find_repo_root`, which walks up from $PWD. A suite
# launched from the live checkout (an Auditor running the CI suites in-place is
# the normal case) therefore leaves every sub-invocation that forgets its own
# `cd` resolving REPO_ROOT — and so `DAEMON_STATE_HOME="$REPO_ROOT/.loom"` —
# straight onto the LIVE checkout. That is how #6386's 11h fleet-dispatcher
# outage happened. <dir> gets its own `.loom/` so it IS a valid workspace root:
# find_repo_root stops there, the scripts resolve a scratch state home instead
# of refusing, and a forgotten `cd` can no longer escape. Cases that
# deliberately exercise a fixture still `cd` into it themselves (a per-case
# `cd` always wins over this default, same as a per-invocation env pin).
live_state_sandbox_init() {
    local dir="$1"
    [[ -n "$dir" ]] || { echo "live_state_sandbox_init: missing <dir>" >&2; return 1; }
    mkdir -p "$dir" "$dir/logs" "$dir/machine-level-bin-sandbox" "$dir/.loom"

    export LOOM_PID_FILE="$dir/.daemon.pid"
    export LOOM_AUTONOMY_MARKER="$dir/autonomy-desired"
    export LOOM_SOCKET_PATH="$dir/loom-daemon.sock"
    export LOOM_DAEMON_BIN_DIR="$dir/machine-level-bin-sandbox"

    # #8077. LOOM_SOCKET_PATH above already redirects the daemon's `loom_dir`,
    # and with it the default log path — but only for a daemon that inherits
    # THIS value. A case that pins its own per-invocation socket (many do) gets
    # its own loom_dir back, and a case that pins neither inherits the
    # production socket path from the sweep environment. LOOM_DAEMON_LOG is the
    # daemon's HIGHEST-precedence log tier (`resolve_log_path`, ahead of the
    # socket-derived default), so setting it here closes both holes at once.
    # The remaining four are the machine-level files the Rust harness's
    # `isolate_daemon_state()` already pins (#4556) and this one did not: the
    # workspace registry (the real one lists the operator's repos, so an
    # un-pinned test daemon reconciles and dispatches against them) and the
    # MACHINE SWEEP JOURNAL (a test daemon that reads it ADOPTS the real host's
    # in-flight sweep claims — observed in #8077's evidence).
    export LOOM_DAEMON_LOG="$dir/daemon.log"
    export LOOM_SHARED_TOKENS_DIR="$dir/tokens"
    export LOOM_WORKSPACES_PATH="$dir/workspaces.json"
    export LOOM_SWEEPS_JOURNAL_PATH="$dir/sweeps.json"
    export LOOM_WATCHES_PATH="$dir/watches.json"
    export LOOM_WATCH_RESULTS_LOG="$dir/watch-results.log"

    # #8077 AC2: a sandboxed suite never gets to drive the LIVE `systemctl
    # --user` manager implicitly. Blocks stay opt-in even here; an operator who
    # really wants them exports the var themselves ahead of the sandbox, which
    # this deliberately preserves rather than overwrites.
    export LOOM_TEST_ALLOW_SYSTEMD="${LOOM_TEST_ALLOW_SYSTEMD:-0}"

    # Unset (not re-pointed): each sub-invocation must resolve these from its
    # OWN fixture. An ambient value here silently outranks the fixture — that
    # is the #4902 shape, and for LOOM_MACHINE_CHECKOUT it additionally flips
    # both lifecycle scripts into machine mode, whose state home is the REAL
    # $HOME/.loom. Tests that need them pin them inline per invocation.
    unset LOOM_WORKSPACE
    unset LOOM_MACHINE_CHECKOUT
    unset LOOM_DAEMON_BIN

    # Leave the caller standing in the sandbox (#6386) — see the header comment
    # above. Done AFTER the exports so a failure here cannot leave a half-armed
    # sandbox, and reported loudly (rather than silently ignored) because a
    # failed `cd` means the cwd tier is still pointed at wherever the suite was
    # launched from.
    if ! cd "$dir"; then
        echo "live-state-sandbox: could not cd into the sandbox root '$dir' — \$PWD still resolves to $PWD, so find_repo_root can escape to the live checkout (#6386)." >&2
        return 1
    fi

    # Supervisor identity (#5501): catches a harness that `export`ed the real
    # LOOM_LAUNCHD_LABEL / LOOM_WATCHDOG_LABEL BEFORE calling init — the exact
    # #5501 incident shape. Loud, but non-fatal here (the caller decides
    # whether to treat a non-zero return as fatal), consistent with every
    # other function in this file.
    live_state_sandbox_assert_supervisor_scoped
}

# Print the sandboxed values (one `NAME=value` per line) — handy in CI logs and
# for asserting the sandbox is actually installed.
live_state_sandbox_describe() {
    printf 'LOOM_PID_FILE=%s\n' "${LOOM_PID_FILE:-}"
    printf 'LOOM_AUTONOMY_MARKER=%s\n' "${LOOM_AUTONOMY_MARKER:-}"
    printf 'LOOM_SOCKET_PATH=%s\n' "${LOOM_SOCKET_PATH:-}"
    printf 'LOOM_DAEMON_BIN_DIR=%s\n' "${LOOM_DAEMON_BIN_DIR:-}"
    printf 'LOOM_DAEMON_LOG=%s\n' "${LOOM_DAEMON_LOG:-}"
    printf 'LOOM_SHARED_TOKENS_DIR=%s\n' "${LOOM_SHARED_TOKENS_DIR:-}"
    printf 'LOOM_WORKSPACES_PATH=%s\n' "${LOOM_WORKSPACES_PATH:-}"
    printf 'LOOM_SWEEPS_JOURNAL_PATH=%s\n' "${LOOM_SWEEPS_JOURNAL_PATH:-}"
    printf 'LOOM_WATCHES_PATH=%s\n' "${LOOM_WATCHES_PATH:-}"
    printf 'LOOM_WATCH_RESULTS_LOG=%s\n' "${LOOM_WATCH_RESULTS_LOG:-}"
    printf 'LOOM_TEST_ALLOW_SYSTEMD=%s\n' "${LOOM_TEST_ALLOW_SYSTEMD:-<unset>}"
    printf 'LOOM_WORKSPACE=%s\n' "${LOOM_WORKSPACE:-<unset>}"
    printf 'LOOM_MACHINE_CHECKOUT=%s\n' "${LOOM_MACHINE_CHECKOUT:-<unset>}"
    printf 'LOOM_DAEMON_BIN=%s\n' "${LOOM_DAEMON_BIN:-<unset>}"
}

# Print how many live paths the snapshot covers (0 before a snapshot).
live_state_sandbox_snapshot_size() {
    [[ -n "$_LSS_SNAPSHOT" ]] || { echo 0; return 0; }
    printf '%s' "$_LSS_SNAPSHOT" | grep -c ''
}

# Re-fingerprint every snapshotted live path. Returns 0 when all are
# byte-and-mtime identical (including "still absent"), 1 when ANY changed —
# printing each offender's before/after to stderr.
live_state_sandbox_assert_untouched() {
    local line path before after dirty=0
    if [[ "$_LSS_SNAPSHOT_TAKEN" != "1" ]]; then
        echo "live-state-sandbox: no snapshot taken — call live_state_sandbox_snapshot first" >&2
        return 1
    fi
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        path="${line%%$'\t'*}"
        before="${line#*$'\t'}"
        after="$(_lss_fingerprint "$path")"
        if [[ "$before" != "$after" ]]; then
            dirty=1
            printf 'live-state-sandbox: LIVE daemon state path was written during this run: %s\n' "$path" >&2
            printf '    before: %s\n' "$before" >&2
            printf '    after:  %s\n' "$after" >&2
        fi
    done <<< "$_LSS_SNAPSHOT"
    # Supervisor identity (#5501): re-checked here too, in case a case set the
    # real label sometime AFTER live_state_sandbox_init (paths staying clean
    # says nothing about whether a label-scoped launchctl/systemctl call
    # reached the real job).
    if ! live_state_sandbox_assert_supervisor_scoped; then
        dirty=1
    fi
    # #8077: the live-HOST surfaces (daemon-log boot counts, the systemd --user
    # unit-file set, the shared token pool). Kept a separate pair because they
    # are a different fingerprint granularity, but asserted here so every
    # existing caller is covered without a second call site to forget.
    if ! live_host_leak_assert_unchanged; then
        dirty=1
    fi
    return "$dirty"
}
