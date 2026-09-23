#!/usr/bin/env bash
# test-live-state-sandbox.sh — tests for lib/live-state-sandbox.sh (issue #5179).
#
# The helper is the daemon lifecycle suites' single "sandbox every live-state
# path" seam, and its guard half is what converts the recurring
# "a test wrote the live host's daemon state" class (#4087, #5131, #5179) from
# "an operator discovers a degraded host" into "the suite fails loudly". A
# guard that cannot itself be shown to FAIL is worthless, so the cases below
# prove both directions: clean run -> rc 0, and each way a live path can be
# touched -> rc 1 with the offending path named.
#
# Every case runs in a subshell with a FAKE $HOME and a FAKE repo root, and
# with the ambient LOOM_* state vars neutralized, so the suite never depends on
# (or touches) the real host's daemon state.
#
# Usage:
#   ./defaults/scripts/tests/test-live-state-sandbox.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/live-state-sandbox.sh
source "$SCRIPT_DIR/lib/live-state-sandbox.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} $1"
    [[ -n "${2:-}" ]] && echo "  $2"
}

check() { # <condition-rc> <message> [detail]
    if [[ "$1" -eq 0 ]]; then pass "$2"; else fail "$2" "${3:-}"; fi
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# A throwaway "live host": a fake $HOME with a ~/.loom state dir, and a fake
# checkout with its own .loom (the repo-mode state home).
FAKE_HOME="$WORKDIR/home"
FAKE_REPO="$WORKDIR/checkout"
mkdir -p "$FAKE_HOME/.loom" "$FAKE_REPO/.loom" "$FAKE_REPO/.git"
echo "4242" > "$FAKE_HOME/.loom/.daemon.pid"
echo "--work-finder" > "$FAKE_HOME/.loom/.daemon.flags"
echo "desired=true" > "$FAKE_HOME/.loom/autonomy-desired"

# Neutralize the ambient agent-session state vars for every case (the real
# values point at THIS host's live daemon — exactly what must never be read or
# written from a test).
NEUTRAL_ENV='unset LOOM_PID_FILE LOOM_AUTONOMY_MARKER LOOM_SOCKET_PATH LOOM_WORKSPACE LOOM_MACHINE_CHECKOUT LOOM_DAEMON_BIN LOOM_DAEMON_BIN_DIR LOOM_LAUNCHD_LABEL LOOM_WATCHDOG_LABEL'

# ============================================================
# 1. live_state_sandbox_init redirects every state path into the sandbox dir
#    and unsets the ambient vars that would outrank a per-fixture value.
# ============================================================
init_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    export LOOM_WORKSPACE="$FAKE_REPO"
    export LOOM_MACHINE_CHECKOUT="$FAKE_REPO"
    export LOOM_DAEMON_BIN="$FAKE_HOME/.local/bin/loom-daemon"
    export LOOM_PID_FILE="$FAKE_HOME/.loom/.daemon.pid"
    live_state_sandbox_init "$WORKDIR/sandbox1" >/dev/null
    live_state_sandbox_describe
)
check "$([[ "$init_out" == *"LOOM_PID_FILE=$WORKDIR/sandbox1/.daemon.pid"* ]] && echo 0 || echo 1)" \
    "init redirects LOOM_PID_FILE into the sandbox (the #5179 leak)" "$init_out"
check "$([[ "$init_out" == *"LOOM_AUTONOMY_MARKER=$WORKDIR/sandbox1/autonomy-desired"* ]] && echo 0 || echo 1)" \
    "init redirects LOOM_AUTONOMY_MARKER into the sandbox (#4011/#5131)" "$init_out"
check "$([[ "$init_out" == *"LOOM_SOCKET_PATH=$WORKDIR/sandbox1/loom-daemon.sock"* ]] && echo 0 || echo 1)" \
    "init redirects LOOM_SOCKET_PATH (and with it the daemon's loom_dir)" "$init_out"
check "$([[ "$init_out" == *"LOOM_DAEMON_BIN_DIR=$WORKDIR/sandbox1/machine-level-bin-sandbox"* ]] && echo 0 || echo 1)" \
    "init redirects LOOM_DAEMON_BIN_DIR (#4381)" "$init_out"
check "$([[ "$init_out" == *"LOOM_WORKSPACE=<unset>"* && "$init_out" == *"LOOM_MACHINE_CHECKOUT=<unset>"* \
    && "$init_out" == *"LOOM_DAEMON_BIN=<unset>"* ]] && echo 0 || echo 1)" \
    "init unsets the ambient LOOM_WORKSPACE / LOOM_MACHINE_CHECKOUT / LOOM_DAEMON_BIN (#4902 shape)" "$init_out"

# ============================================================
# 2. A run that touches nothing leaves the guard green.
# ============================================================
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_snapshot
    live_state_sandbox_init "$WORKDIR/sandbox2" >/dev/null
    # A well-behaved suite writes ONLY inside the sandbox.
    echo "99999" > "$LOOM_PID_FILE"
    echo "desired=false" > "$LOOM_AUTONOMY_MARKER"
    live_state_sandbox_assert_untouched
) 2>"$WORKDIR/case2.err"
check $? "clean run (writes confined to the sandbox) passes the guard" "$(cat "$WORKDIR/case2.err")"

# ============================================================
# 3. Rewriting an EXISTING live state file fails the guard, loudly.
#    This is the #5179 shape on a machine-mode host: ~/.loom/.daemon.pid gets
#    a test fixture's pid, and the operator sees a false `degraded` verdict.
# ============================================================
case3_err="$WORKDIR/case3.err"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_snapshot
    live_state_sandbox_init "$WORKDIR/sandbox3" >/dev/null
    # The leak: something resolved the LIVE pid file and claimed it.
    echo "86050" > "$FAKE_HOME/.loom/.daemon.pid"
    live_state_sandbox_assert_untouched
) 2>"$case3_err"
rc3=$?
check "$([[ "$rc3" -ne 0 ]] && echo 0 || echo 1)" \
    "rewriting the live ~/.loom/.daemon.pid FAILS the guard (rc=$rc3)"
check "$(grep -q "$FAKE_HOME/.loom/.daemon.pid" "$case3_err" && echo 0 || echo 1)" \
    "the guard names the offending live path in its failure output" "$(cat "$case3_err")"
check "$(grep -q 'before:' "$case3_err" && grep -q 'after:' "$case3_err" && echo 0 || echo 1)" \
    "the guard prints before/after fingerprints for the offender" "$(cat "$case3_err")"
# Restore for the later cases.
echo "4242" > "$FAKE_HOME/.loom/.daemon.pid"

# ============================================================
# 4. CREATING a live state path that was absent fails too — the repo-mode
#    shape, where an un-cd'd sub-invocation resolves REPO_ROOT from the live
#    checkout and writes <checkout>/.loom/.daemon.pid where none existed.
# ============================================================
case4_err="$WORKDIR/case4.err"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_snapshot
    live_state_sandbox_init "$WORKDIR/sandbox4" >/dev/null
    echo "3745" > "$FAKE_REPO/.loom/.daemon.pid"
    live_state_sandbox_assert_untouched
) 2>"$case4_err"
rc4=$?
check "$([[ "$rc4" -ne 0 ]] && echo 0 || echo 1)" \
    "creating a previously-absent <checkout>/.loom/.daemon.pid FAILS the guard (rc=$rc4)"
check "$(grep -q '<absent>' "$case4_err" && echo 0 || echo 1)" \
    "the guard reports the absent->present transition" "$(cat "$case4_err")"
rm -f "$FAKE_REPO/.loom/.daemon.pid"

# ============================================================
# 5. An AMBIENT LOOM_PID_FILE (what a Loom agent session exports — the real
#    root cause of #5179) is both (a) covered by the snapshot and (b)
#    redirected by init, so the suite writes the sandbox copy instead.
# ============================================================
case5_err="$WORKDIR/case5.err"
AMBIENT_PID_FILE="$WORKDIR/ambient-live/.daemon.pid"
mkdir -p "$(dirname "$AMBIENT_PID_FILE")"
echo "850106" > "$AMBIENT_PID_FILE"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    export LOOM_PID_FILE="$AMBIENT_PID_FILE"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_snapshot
    live_state_sandbox_init "$WORKDIR/sandbox5" >/dev/null
    # A daemon spawned by the suite claims whatever LOOM_PID_FILE names.
    echo "$$" > "$LOOM_PID_FILE"
    live_state_sandbox_assert_untouched
) 2>"$case5_err"
rc5=$?
check "$([[ "$rc5" -eq 0 ]] && echo 0 || echo 1)" \
    "an ambient LOOM_PID_FILE is redirected, so the live pid file survives (rc=$rc5)" "$(cat "$case5_err")"
check "$([[ "$(cat "$AMBIENT_PID_FILE")" == "850106" ]] && echo 0 || echo 1)" \
    "the live pid file still records the live daemon's pid, byte-for-byte"

# The same case WITHOUT the sandbox is what production looked like: prove the
# snapshot half would have caught it.
case5b_err="$WORKDIR/case5b.err"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    export LOOM_PID_FILE="$AMBIENT_PID_FILE"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_snapshot
    # No live_state_sandbox_init: the pre-#5179 suite, inheriting the live path.
    echo "86050" > "$LOOM_PID_FILE"
    live_state_sandbox_assert_untouched
) 2>"$case5b_err"
rc5b=$?
check "$([[ "$rc5b" -ne 0 ]] && echo 0 || echo 1)" \
    "without init, a write through the ambient LOOM_PID_FILE FAILS the guard (rc=$rc5b)" "$(cat "$case5b_err")"
echo "850106" > "$AMBIENT_PID_FILE"

# ============================================================
# 6. Asserting without a snapshot fails loudly rather than silently passing —
#    a suite that forgets the snapshot must not look green.
# ============================================================
case6_err="$WORKDIR/case6.err"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    live_state_sandbox_assert_untouched
) 2>"$case6_err"
rc6=$?
check "$([[ "$rc6" -ne 0 ]] && echo 0 || echo 1)" \
    "assert without a prior snapshot fails (rc=$rc6)"
check "$(grep -qi 'no snapshot' "$case6_err" && echo 0 || echo 1)" \
    "the no-snapshot failure explains what is missing" "$(cat "$case6_err")"

# ============================================================
# 7. Volatile daemon-written files are deliberately NOT guarded (a live
#    daemon rewrites them on its own cadence, which would make the guard flap).
# ============================================================
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    echo "ts=1" > "$FAKE_HOME/.loom/daemon.heartbeat"
    live_state_sandbox_snapshot
    live_state_sandbox_init "$WORKDIR/sandbox7" >/dev/null
    echo "ts=2" > "$FAKE_HOME/.loom/daemon.heartbeat"
    live_state_sandbox_assert_untouched
) 2>"$WORKDIR/case7.err"
check $? "a live daemon's own heartbeat churn does not flap the guard" "$(cat "$WORKDIR/case7.err")"

# ============================================================
# 8. The snapshot actually covers something (a silently-empty path list would
#    make every assertion above vacuously true).
# ============================================================
snap_size=$(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_snapshot
    live_state_sandbox_snapshot_size
)
check "$([[ "${snap_size:-0}" -ge 6 ]] && echo 0 || echo 1)" \
    "snapshot covers both the \$HOME/.loom and the checkout state roots ($snap_size paths)"

# ============================================================
# 9. Supervisor IDENTITY (#5501): sandboxing paths does not sandbox
#    LOOM_LAUNCHD_LABEL / LOOM_WATCHDOG_LABEL — a path-clean run whose label
#    resolves to the REAL production identity must fail the guard too.
# ============================================================

# 9a. A scratch label passes cleanly. Both args are passed explicitly (scratch
#     values) so this case never relies on live_state_sandbox_assert_supervisor_scoped's
#     default-to-ambient behavior -- if the ambient LOOM_WATCHDOG_LABEL happens
#     to be the real production watchdog label (e.g. this suite is run on a
#     host with a live daemon), the default-to-ambient second arg would
#     otherwise make this case spuriously fail.
check "$(live_state_sandbox_assert_supervisor_scoped "com.example.scratch-1234" "com.example.scratch-1234-watchdog" && echo 0 || echo 1)" \
    "a scratch LOOM_LAUNCHD_LABEL passes the supervisor-identity guard"

# 9b. The REAL production launchd label fails loudly.
case9b_err="$WORKDIR/case9b.err"
live_state_sandbox_assert_supervisor_scoped "com.rjwalters.loom-daemon" 2>"$case9b_err"
rc9b=$?
check "$([[ "$rc9b" -ne 0 ]] && echo 0 || echo 1)" \
    "the REAL production launchd label FAILS the supervisor-identity guard (rc=$rc9b)"
check "$(grep -q 'com.rjwalters.loom-daemon' "$case9b_err" && echo 0 || echo 1)" \
    "the guard names the offending production label in its failure output" "$(cat "$case9b_err")"

# 9c. The REAL production watchdog label fails loudly too (second arg).
case9c_err="$WORKDIR/case9c.err"
live_state_sandbox_assert_supervisor_scoped "com.example.scratch-5678" "com.rjwalters.loom-daemon-watchdog" 2>"$case9c_err"
rc9c=$?
check "$([[ "$rc9c" -ne 0 ]] && echo 0 || echo 1)" \
    "the REAL production watchdog label FAILS the supervisor-identity guard (rc=$rc9c)"

# 9d. Reading ambient env (no args) catches a harness that EXPORTED the real
#     label before calling live_state_sandbox_init — the exact #5501 shape.
case9d_err="$WORKDIR/case9d.err"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    export LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_init "$WORKDIR/sandbox9d"
) 2>"$case9d_err"
rc9d=$?
check "$([[ "$rc9d" -ne 0 ]] && echo 0 || echo 1)" \
    "live_state_sandbox_init itself fails when the ambient LOOM_LAUNCHD_LABEL is the real one (rc=$rc9d, #5501 AC1)"

# 9e. live_state_sandbox_assert_untouched ALSO re-checks the supervisor
#     identity, in case a case exports the real label sometime AFTER init.
case9e_err="$WORKDIR/case9e.err"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_snapshot
    live_state_sandbox_init "$WORKDIR/sandbox9e" >/dev/null
    export LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon"
    live_state_sandbox_assert_untouched
) 2>"$case9e_err"
rc9e=$?
check "$([[ "$rc9e" -ne 0 ]] && echo 0 || echo 1)" \
    "live_state_sandbox_assert_untouched fails when the real label was set AFTER init (rc=$rc9e)"

# 9f. The LOOM_DAEMON_STOP_DRYRUN bypass (#5501 AC2): the supported way to
#     exercise default-label semantics is exempt from the guard.
check "$(
    eval "$NEUTRAL_ENV"
    export LOOM_DAEMON_STOP_DRYRUN=1
    live_state_sandbox_assert_supervisor_scoped "com.rjwalters.loom-daemon" "com.rjwalters.loom-daemon-watchdog" \
        && echo 0 || echo 1
)" \
    "LOOM_DAEMON_STOP_DRYRUN=1 bypasses the guard for the real labels (the supported dry-run seam)"

# ============================================================
# 10. $PWD is a resolution tier too (#6386): the lifecycle scripts derive
#     DAEMON_STATE_HOME from `find_repo_root`'s walk up from the cwd, so a
#     suite launched from the LIVE checkout hands every sub-invocation that
#     forgets its own `cd` the live `.loom` as its state home. That is the 11h
#     fleet-dispatcher outage. init now `cd`s into the sandbox and gives it a
#     `.loom/`, so the cwd tier lands in scratch by default.
# ============================================================

# 10a. init leaves the caller standing in the sandbox.
cwd10=$(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_init "$WORKDIR/sandbox10" >/dev/null 2>&1
    pwd -P
)
check "$([[ "$cwd10" == "$(cd "$WORKDIR/sandbox10" && pwd -P)" ]] && echo 0 || echo 1)" \
    "init cds the caller into the sandbox root, off the live checkout (#6386)" \
    "cwd after init: $cwd10"

# 10b. …and the sandbox is a VALID workspace root, so find_repo_root's walk
#      stops there instead of continuing up into the live checkout. (A cd into
#      a dir with no `.loom` would keep walking and land right back on it.)
root10=$(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_init "$WORKDIR/sandbox10" >/dev/null 2>&1
    _lss_repo_root_from "$PWD"
)
check "$([[ "$root10" == "$WORKDIR/sandbox10" ]] && echo 0 || echo 1)" \
    "the sandbox root has its own .loom/, so the cwd tier resolves to scratch, not the checkout (#6386)" \
    "repo root from cwd: $root10 (checkout is $FAKE_REPO)"

# 10c. Without the cd, the same walk lands on the live checkout — the tier this
#      hardening closes is real, not hypothetical. (Guards against 10a/10b
#      passing vacuously if the fixture stopped being reachable.)
root10c=$(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    _lss_repo_root_from "$PWD"
)
check "$([[ "$root10c" == "$FAKE_REPO" ]] && echo 0 || echo 1)" \
    "control: without the cd, the cwd tier resolves to the live checkout" \
    "repo root from cwd: $root10c"

# 10d. A cd that CANNOT succeed is fatal and loud, never a silent half-armed
#      sandbox: the paths are exported but the cwd tier is still aimed at
#      wherever the suite was launched from, which is the dangerous half.
mkdir -p "$WORKDIR/blocked" && : > "$WORKDIR/blocked/notadir"
case10d_err="$WORKDIR/case10d.err"
(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME"
    cd "$FAKE_REPO" || exit 1
    live_state_sandbox_init "$WORKDIR/blocked/notadir/sandbox" >/dev/null
) 2>"$case10d_err"
rc10d=$?
check "$([[ "$rc10d" -ne 0 ]] && echo 0 || echo 1)" \
    "init FAILS when it cannot cd into the sandbox root (rc=$rc10d, #6386)"
check "$(grep -q '6386' "$case10d_err" && echo 0 || echo 1)" \
    "the failed-cd message explains that find_repo_root can still escape" "$(cat "$case10d_err")"

# ============================================================
# 11. init's failure return must be CHECKED by its callers (#6420).
#
#     10d proves the helper fails loudly. That is only half the contract: the
#     CI suites run under `set -uo pipefail` with NO `-e`, so a BARE
#     `live_state_sandbox_init …` swallows that failure and carries on with a
#     half-armed sandbox — env paths redirected, but the cwd tier still aimed
#     at wherever the suite was launched from (a live checkout, in the #6386
#     incident) — while driving the real lifecycle scripts.
# ============================================================

# 11a. Structural: every call site in the four daemon-lifecycle suites checks
#      the return code (`if ! …` or `… || …`). Enumerated from the files
#      themselves so a NEW bare call site fails this suite rather than being
#      discovered on a fleet host.
GUARDED_CALLERS=(
    test-loom-daemon-start.sh
    test-loom-daemon-stop.sh
    test-loom-daemon-update.sh
    test-loom-daemon-quiesce.sh
)
unchecked=""
no_call=""
for suite in "${GUARDED_CALLERS[@]}"; do
    suite_file="$SCRIPT_DIR/$suite"
    call_lines="$(grep -nE '^[^#]*live_state_sandbox_init[[:space:]]+"' "$suite_file" 2>/dev/null)"
    if [[ -z "$call_lines" ]]; then
        no_call="$no_call $suite"
        continue
    fi
    while IFS= read -r call_line; do
        [[ -n "$call_line" ]] || continue
        if [[ "$call_line" != *"if ! "* && "$call_line" != *"||"* ]]; then
            unchecked="$unchecked
    $suite:$call_line"
        fi
    done <<< "$call_lines"
done
check "$([[ -z "$no_call" ]] && echo 0 || echo 1)" \
    "every daemon-lifecycle suite still calls live_state_sandbox_init (the check below is not vacuous)" \
    "no call site found in:$no_call"
check "$([[ -z "$unchecked" ]] && echo 0 || echo 1)" \
    "no daemon-lifecycle suite calls live_state_sandbox_init bare — every call site checks its rc (#6420)" \
    "unchecked call sites:$unchecked"

# 11b. Behavioural: the guarded shape actually aborts, and the bare shape
#      actually does NOT (the reason 11a is worth enforcing). Both run the same
#      guaranteed-failing init as 10d, in a scratch script that mirrors a
#      suite's own `set -uo pipefail` preamble.
cat > "$WORKDIR/mini-suite-guarded.sh" <<MINI
#!/usr/bin/env bash
set -uo pipefail
source "$SCRIPT_DIR/lib/live-state-sandbox.sh"
if ! live_state_sandbox_init "$WORKDIR/blocked/notadir/sandbox"; then
    echo "ABORTED"
    exit 1
fi
echo "REACHED-THE-CASES"
MINI
guarded_out="$(bash "$WORKDIR/mini-suite-guarded.sh" 2>/dev/null)"
guarded_rc=$?
check "$([[ "$guarded_rc" -ne 0 && "$guarded_out" == *"ABORTED"* && "$guarded_out" != *"REACHED-THE-CASES"* ]] && echo 0 || echo 1)" \
    "a checked call site aborts before any case runs when init fails (rc=$guarded_rc, #6420)" \
    "output: $guarded_out"

cat > "$WORKDIR/mini-suite-bare.sh" <<MINI
#!/usr/bin/env bash
set -uo pipefail
source "$SCRIPT_DIR/lib/live-state-sandbox.sh"
live_state_sandbox_init "$WORKDIR/blocked/notadir/sandbox"
echo "REACHED-THE-CASES"
MINI
bare_out="$(bash "$WORKDIR/mini-suite-bare.sh" 2>/dev/null)"
bare_rc=$?
check "$([[ "$bare_rc" -eq 0 && "$bare_out" == *"REACHED-THE-CASES"* ]] && echo 0 || echo 1)" \
    "control: a BARE call site sails past the same failure under set -uo pipefail (rc=$bare_rc, #6420)" \
    "output: $bare_out"

# ============================================================
# 12. #8077 — the live-HOST leak guard.
#
#     `daemon.log` is deliberately NOT in LIVE_STATE_SANDBOX_GUARDED_FILES: a
#     running daemon appends to it constantly, so a size/mtime/sha fingerprint
#     of it flaps every second. The #8077 pair guards it at the only
#     granularity that does not — the number of `Daemon logging initialized`
#     lines, one per daemon PROCESS START. These cases prove both that an
#     append does not trip it (or the guard would be disabled within a day) and
#     that a new boot block does.
# ============================================================
LEAK_HOME="$WORKDIR/leak-home"
mkdir -p "$LEAK_HOME/.loom/tokens" "$LEAK_HOME/.config/systemd/user"
printf '[t] Daemon logging initialized to x\n[t] Loom daemon starting...\n' > "$LEAK_HOME/.loom/daemon.log"
: > "$LEAK_HOME/.loom/tokens/alpha.token"
: > "$LEAK_HOME/.config/systemd/user/loom-daemon.service"

# 12a. Append-only churn (a healthy daemon logging) is NOT a leak.
leak_clean_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$LEAK_HOME" XDG_CONFIG_HOME="$LEAK_HOME/.config"
    live_host_leak_snapshot
    echo "surfaces=$(live_host_leak_snapshot_size)"
    printf '[t] a live daemon keeps logging\n[t] and logging\n' >> "$HOME/.loom/daemon.log"
    live_host_leak_assert_unchanged 2>&1 && echo "RC=0" || echo "RC=1"
)
check "$([[ "$leak_clean_out" == *"RC=0"* ]] && echo 0 || echo 1)" \
    "a live daemon's own daemon.log appends do NOT trip the #8077 guard" "$leak_clean_out"
check "$([[ "$leak_clean_out" == *"surfaces=4"* ]] && echo 0 || echo 1)" \
    "the #8077 snapshot covers 4 live-host surfaces (log, unit identity, unit dir, token pool)" "$leak_clean_out"

# 12b. A NEW boot block with no supervised restart = the #8077 signature.
leak_boot_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$LEAK_HOME" XDG_CONFIG_HOME="$LEAK_HOME/.config"
    live_host_leak_snapshot
    printf '[t] Daemon logging initialized to x\n' >> "$HOME/.loom/daemon.log"
    live_host_leak_assert_unchanged 2>&1 && echo "RC=0" || echo "RC=1"
)
check "$([[ "$leak_boot_out" == *"RC=1"* ]] && echo 0 || echo 1)" \
    "a NEW daemon boot block in the live daemon.log FAILS the #8077 guard (rc=1)" "$leak_boot_out"
check "$([[ "$leak_boot_out" == *"boot blocks before: 1"*"after: 2"* ]] && echo 0 || echo 1)" \
    "the #8077 guard reports the before/after boot counts" "$leak_boot_out"
check "$([[ "$leak_boot_out" == *"LOOM_DAEMON_LOG"* && "$leak_boot_out" == *"LOOM_SOCKET_PATH"* ]] && echo 0 || echo 1)" \
    "the #8077 failure names the two overrides that would have prevented it" "$leak_boot_out"

# 12c. A daemon.log CREATED where none existed is always a leak — the only
#      direction a CI runner (no daemon, no user manager) can observe.
leak_create_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$WORKDIR/leak-bare" XDG_CONFIG_HOME="$WORKDIR/leak-bare/.config"
    mkdir -p "$HOME/.loom"
    live_host_leak_snapshot
    printf '[t] Daemon logging initialized to x\n' > "$HOME/.loom/daemon.log"
    live_host_leak_assert_unchanged 2>&1 && echo "RC=0" || echo "RC=1"
)
check "$([[ "$leak_create_out" == *"RC=1"* ]] && echo 0 || echo 1)" \
    "CREATING a previously-absent live daemon.log FAILS the #8077 guard (rc=1)" "$leak_create_out"

# 12d. Real unit files dropped into the live systemd --user search path (the
#      #4862 MX block's shape) are caught.
leak_unit_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$LEAK_HOME" XDG_CONFIG_HOME="$LEAK_HOME/.config"
    live_host_leak_snapshot
    : > "$XDG_CONFIG_HOME/systemd/user/loom-daemon-test-mx-mixed-999.service"
    live_host_leak_assert_unchanged 2>&1 && echo "RC=0" || echo "RC=1"
)
check "$([[ "$leak_unit_out" == *"RC=1"* ]] && echo 0 || echo 1)" \
    "a test unit file left in the LIVE systemd --user dir FAILS the #8077 guard (rc=1)" "$leak_unit_out"
check "$([[ "$leak_unit_out" == *"LOOM_TEST_ALLOW_SYSTEMD=1"* ]] && echo 0 || echo 1)" \
    "the unit-dir failure names the LOOM_TEST_ALLOW_SYSTEMD opt-in gate" "$leak_unit_out"

# 12d-2. A test that `mkdir -p`s the LIVE systemd --user dir (absent beforehand)
#        and then cleans up its own unit file — the #4862 MX block's mx_cleanup
#        shape — must NOT trip the guard. The directory's own existence is not
#        what is guarded, only the unit-file NAME SET; a CI runner with no
#        pre-existing $HOME/.config/systemd/user hit this as a false positive
#        (before=`<absent>`, after=`names=` — empty, but no longer `<absent>`).
LEAK_MKDIR_HOME="$WORKDIR/leak-mkdir-home"
mkdir -p "$LEAK_MKDIR_HOME/.loom"
leak_unit_clean_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$LEAK_MKDIR_HOME" XDG_CONFIG_HOME="$LEAK_MKDIR_HOME/.config"
    live_host_leak_snapshot
    mkdir -p "$XDG_CONFIG_HOME/systemd/user"
    : > "$XDG_CONFIG_HOME/systemd/user/loom-daemon-test-mx-mixed-999.service"
    rm -f "$XDG_CONFIG_HOME/systemd/user/loom-daemon-test-mx-mixed-999.service"
    live_host_leak_assert_unchanged 2>&1 && echo "RC=0" || echo "RC=1"
)
check "$([[ "$leak_unit_clean_out" == *"RC=0"* ]] && echo 0 || echo 1)" \
    "mkdir -p'ing an absent LIVE systemd --user dir and cleaning up its own unit file does NOT trip the #8077 guard" "$leak_unit_clean_out"

# 12e. A token written into the live shared pool is caught.
leak_token_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$LEAK_HOME" XDG_CONFIG_HOME="$LEAK_HOME/.config"
    live_host_leak_snapshot
    : > "$HOME/.loom/tokens/beta.token"
    live_host_leak_assert_unchanged 2>&1 && echo "RC=0" || echo "RC=1"
)
check "$([[ "$leak_token_out" == *"RC=1"* ]] && echo 0 || echo 1)" \
    "a token written into the LIVE shared pool FAILS the #8077 guard (rc=1)" "$leak_token_out"

# 12f. Asserting without a snapshot is an error, not a silent pass — the same
#      fail-closed shape the state-path pair already has.
leak_nosnap_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$LEAK_HOME"
    _LSS_LEAK_SNAPSHOT_TAKEN=0
    live_host_leak_assert_unchanged 2>&1 && echo "RC=0" || echo "RC=1"
)
check "$([[ "$leak_nosnap_out" == *"RC=1"* && "$leak_nosnap_out" == *"live_host_leak_snapshot first"* ]] && echo 0 || echo 1)" \
    "asserting the #8077 guard without a snapshot fails and says so" "$leak_nosnap_out"

# 12g. The LOOM_SOCKET_PATH-derived log is enumerated too — that is the #8077
#      resolution tier (the sweep environment inherits the production socket
#      path, so the "default" a forgetful test gets IS the live log).
leak_socket_out=$(
    eval "$NEUTRAL_ENV"
    export HOME="$WORKDIR/leak-sock-home"
    mkdir -p "$HOME/.loom" "$WORKDIR/leak-sock-dir"
    export LOOM_SOCKET_PATH="$WORKDIR/leak-sock-dir/loom-daemon.sock"
    printf '[t] Daemon logging initialized to x\n' > "$WORKDIR/leak-sock-dir/daemon.log"
    live_host_leak_snapshot
    printf '[t] Daemon logging initialized to x\n' >> "$WORKDIR/leak-sock-dir/daemon.log"
    live_host_leak_assert_unchanged 2>&1 && echo "RC=1-with:" || echo "RC=1-with:"
)
check "$([[ "$leak_socket_out" == *"leak-sock-dir/daemon.log"* ]] && echo 0 || echo 1)" \
    "the #8077 guard also watches the LOOM_SOCKET_PATH-derived daemon.log (the sweep-inherited tier)" "$leak_socket_out"

# ============================================================
# 13. #8077 — live_state_sandbox_init redirects the machine-level state the
#     Rust harness's isolate_daemon_state() already pinned and this one did
#     not. The sweep journal is the load-bearing one: a test daemon that reads
#     the real one ADOPTS the live host's in-flight sweep claims.
# ============================================================
init8077_out=$(
    eval "$NEUTRAL_ENV"
    unset LOOM_DAEMON_LOG LOOM_SHARED_TOKENS_DIR LOOM_WORKSPACES_PATH LOOM_SWEEPS_JOURNAL_PATH LOOM_TEST_ALLOW_SYSTEMD
    export HOME="$FAKE_HOME"
    live_state_sandbox_init "$WORKDIR/sandbox8077" >/dev/null
    live_state_sandbox_describe
)
for _var in LOOM_DAEMON_LOG:daemon.log LOOM_SHARED_TOKENS_DIR:tokens \
            LOOM_WORKSPACES_PATH:workspaces.json LOOM_SWEEPS_JOURNAL_PATH:sweeps.json \
            LOOM_WATCHES_PATH:watches.json LOOM_WATCH_RESULTS_LOG:watch-results.log; do
    _name="${_var%%:*}"; _leaf="${_var#*:}"
    check "$([[ "$init8077_out" == *"$_name=$WORKDIR/sandbox8077/$_leaf"* ]] && echo 0 || echo 1)" \
        "init redirects $_name into the sandbox (#8077)" "$init8077_out"
done
check "$([[ "$init8077_out" == *"LOOM_TEST_ALLOW_SYSTEMD=0"* ]] && echo 0 || echo 1)" \
    "init defaults LOOM_TEST_ALLOW_SYSTEMD to 0 — a sandboxed suite never drives the LIVE user manager (#8077)" \
    "$init8077_out"

# An operator who deliberately opted in keeps that opt-in through init.
init8077_optin=$(
    eval "$NEUTRAL_ENV"
    export HOME="$FAKE_HOME" LOOM_TEST_ALLOW_SYSTEMD=1
    live_state_sandbox_init "$WORKDIR/sandbox8077b" >/dev/null
    live_state_sandbox_describe
)
check "$([[ "$init8077_optin" == *"LOOM_TEST_ALLOW_SYSTEMD=1"* ]] && echo 0 || echo 1)" \
    "init preserves an explicit LOOM_TEST_ALLOW_SYSTEMD=1 rather than overwriting it (#8077)" \
    "$init8077_optin"

# ============================================================
# 14. #8077 AC2 — the #4862 real-systemd block in test-loom-daemon-start.sh is
#     opt-in. Asserted against the SOURCE rather than by running the suite:
#     executing it is a ~2-minute daemon-lifecycle run, and the property under
#     test is exactly "this code is not reachable by default", which a source
#     assertion states directly. The suite's own run proves the other half.
# ============================================================
MX_SUITE="$SCRIPT_DIR/test-loom-daemon-start.sh"
mx_gate_line="$(grep -n 'MX_HAVE_SYSTEMD=true' -B4 "$MX_SUITE" | grep 'LOOM_TEST_ALLOW_SYSTEMD' || true)"
check "$([[ -n "$mx_gate_line" ]] && echo 0 || echo 1)" \
    "the #4862 real-systemd block is gated on LOOM_TEST_ALLOW_SYSTEMD (#8077 AC2)" \
    "no LOOM_TEST_ALLOW_SYSTEMD in the MX_HAVE_SYSTEMD gate"
check "$([[ "$mx_gate_line" == *'LOOM_TEST_ALLOW_SYSTEMD:-0'* ]] && echo 0 || echo 1)" \
    "…and the gate DEFAULTS to off, so a host with a reachable user manager does not opt itself in" \
    "$mx_gate_line"
# The whole point is that the only live `systemctl --user` MUTATIONS in the
# tests directory live behind that gate. A new unguarded one would reopen
# #8077, so this counts them rather than trusting review.
#
# Matched in COMMAND POSITION only (line starts with `systemctl`), which is the
# form every real invocation in this directory takes and which no assertion
# message, `grep` pattern or stub-expectation string does. A deliberately
# obfuscated call (`eval`, `$(systemctl …)` mid-expression) would slip past —
# this is a tripwire against the next honest addition, not a sandbox. The
# `stop`/`reset-failed` verbs count as mutations too: MX's own cleanup uses
# them, so excluding them would let half the block back in.
mx_unguarded="$(grep -rnE '^[[:space:]]*systemctl --user (daemon-reload|start|stop|restart|enable|disable|reset-failed|kill)' \
    "$SCRIPT_DIR"/*.sh 2>/dev/null | grep -v 'test-loom-daemon-start.sh' || true)"
check "$([[ -z "$mx_unguarded" ]] && echo 0 || echo 1)" \
    "no other suite in defaults/scripts/tests/ mutates the LIVE systemd --user manager (#8077 AC2)" \
    "unguarded call sites:
$mx_unguarded"
# …and the gated file's own mutations are all INSIDE the gate: every one of
# them sits after the `if [[ "$MX_HAVE_SYSTEMD" == "true" ]]` line, so the gate
# above is not merely present but actually covers them.
mx_gate_open="$(grep -n 'if \[\[ "\$MX_HAVE_SYSTEMD" == "true" \]\]' "$MX_SUITE" | head -1 | cut -d: -f1)"
mx_before_gate="$(grep -nE '^[[:space:]]*systemctl --user (daemon-reload|start|stop|restart|enable|disable|reset-failed|kill)' \
    "$MX_SUITE" | awk -F: -v g="${mx_gate_open:-0}" '$1 < g')"
check "$([[ -n "$mx_gate_open" && -z "$mx_before_gate" ]] && echo 0 || echo 1)" \
    "every live 'systemctl --user' mutation in test-loom-daemon-start.sh sits INSIDE the gate (#8077 AC2)" \
    "gate opens at line ${mx_gate_open:-<not found>}; mutations before it:
$mx_before_gate"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
