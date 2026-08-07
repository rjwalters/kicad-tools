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
#   LOOM_DAEMON_BIN        -> UNSET  (#4902: the ambient value names the REAL binary)
#   LOOM_WORKSPACE         -> UNSET  (pid-file tier 3; each sub-invocation must
#                             resolve its OWN fixture repo root instead)
#   LOOM_MACHINE_CHECKOUT  -> UNSET  (pid-file tier 2 + the scripts' machine
#                             mode, whose state home is the real $HOME/.loom)
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
# ## Usage (source it)
#
#   source "$SCRIPT_DIR/lib/live-state-sandbox.sh"
#   live_state_sandbox_snapshot                      # BEFORE any sandbox export
#   ...
#   live_state_sandbox_init "$BASE_WORKDIR/live-state"
#   ... run the suite ...
#   live_state_sandbox_assert_untouched              # rc 0 = clean, 1 = leaked

# State files that identify/steer a daemon and are written only at lifecycle
# transitions — safe to compare before/after even while a real daemon runs.
LIVE_STATE_SANDBOX_GUARDED_FILES=".daemon.pid .daemon.flags autonomy-desired"

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
}

# Redirect every live state path into <dir>. Exports are inherited by every
# sub-invocation, so a call site that forgets to sandbox something still cannot
# reach a production path.
live_state_sandbox_init() {
    local dir="$1"
    [[ -n "$dir" ]] || { echo "live_state_sandbox_init: missing <dir>" >&2; return 1; }
    mkdir -p "$dir" "$dir/logs" "$dir/machine-level-bin-sandbox"

    export LOOM_PID_FILE="$dir/.daemon.pid"
    export LOOM_AUTONOMY_MARKER="$dir/autonomy-desired"
    export LOOM_SOCKET_PATH="$dir/loom-daemon.sock"
    export LOOM_DAEMON_BIN_DIR="$dir/machine-level-bin-sandbox"

    # Unset (not re-pointed): each sub-invocation must resolve these from its
    # OWN fixture. An ambient value here silently outranks the fixture — that
    # is the #4902 shape, and for LOOM_MACHINE_CHECKOUT it additionally flips
    # both lifecycle scripts into machine mode, whose state home is the REAL
    # $HOME/.loom. Tests that need them pin them inline per invocation.
    unset LOOM_WORKSPACE
    unset LOOM_MACHINE_CHECKOUT
    unset LOOM_DAEMON_BIN

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
    return "$dirty"
}
