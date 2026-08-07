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
NEUTRAL_ENV='unset LOOM_PID_FILE LOOM_AUTONOMY_MARKER LOOM_SOCKET_PATH LOOM_WORKSPACE LOOM_MACHINE_CHECKOUT LOOM_DAEMON_BIN LOOM_DAEMON_BIN_DIR'

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

# 9a. A scratch label passes cleanly.
check "$(live_state_sandbox_assert_supervisor_scoped "com.example.scratch-1234" && echo 0 || echo 1)" \
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

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
