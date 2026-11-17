#!/usr/bin/env bash
# test-cargo-test-daemon-guard.sh — the live-daemon guard for the plain
# `cargo test --workspace` path used by the root `package.json` `test` script
# (issue #6554).
#
# #6554: `cargo test --workspace --locked --all-features --no-fail-fast --
# --nocapture` — the root `package.json` `test` script, invoked by `pnpm
# test` / `npm run check:all` — is unsafe to run directly on a host with a
# live `loom-daemon` and real tmux agent sessions, the same hazard
# nextest-daemon-guard.sh already closed for `cargo nextest run --workspace`
# (#6528, itself the same class as #6386).
#
# Driven entirely through `cargo-test-daemon-guard.sh --plan`/`--resolve`,
# which only print a decision and never invoke `cargo`/`tmux`/anything else —
# so this suite is hermetic and never touches a real daemon, tmux session, or
# the actual Rust build, regardless of whether the host running it happens to
# have a live daemon.
#
# Every case pins the candidate pid-file list via the guard's shared
# TEST-ONLY seam `LOOM_CI_DAEMON_PIDFILE_CANDIDATES` (`none` = no candidates
# at all — see defaults/scripts/lib/live-daemon-guard.sh), so no assertion
# depends on whether THIS host happens to be running a real daemon.
#
# Usage:
#   ./defaults/scripts/tests/test-cargo-test-daemon-guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$SCRIPT_DIR/cargo-test-daemon-guard.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} $1"
}
fail() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} $1"
    [[ -n "${2:-}" ]] && echo "$2" | sed 's/^/    /'
}
check() {
    local rc="$1" msg="$2" detail="${3:-}"
    if [[ "$rc" -eq 0 ]]; then pass "$msg"; else fail "$msg" "$detail"; fi
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

TEST_CMD="cargo test --workspace --locked --all-features --no-fail-fast -- --nocapture"

# Every invocation below pins the candidate list, so nothing about the host
# running this suite can influence the outcome.
run_plan() { # <pidfile-candidates|none> [stderr-path]
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES="$1" bash "$GUARD" --plan "$TEST_CMD" 2>"${2:-/dev/null}"
}
run_resolve() { # <pidfile-candidates|none> [stderr-path]
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES="$1" bash "$GUARD" --resolve "$TEST_CMD" 2>"${2:-/dev/null}"
}

# ---------- 1. no pid file anywhere -> RUN, unchanged command -----------------
ABSENT_PLAN="$( run_plan "$WORKDIR/absent-a.pid:$WORKDIR/absent-b.pid" )"
absent_rc=$?
check "$absent_rc" "no daemon pid file: --plan exits 0"
check "$([[ "$ABSENT_PLAN" == "RUN $TEST_CMD" ]] && echo 0 || echo 1)" \
    "no daemon pid file: planned to RUN the unmodified command (guard is not always-on)" \
    "$ABSENT_PLAN"

ABSENT_RESOLVED="$( run_resolve "$WORKDIR/absent-a.pid" )"
check "$([[ "$ABSENT_RESOLVED" == "$TEST_CMD" ]] && echo 0 || echo 1)" \
    "no daemon pid file: --resolve returns the command byte-for-byte unchanged" \
    "$ABSENT_RESOLVED"

# ---------- 2. a live-looking pid file -> GUARD, split into 3 invocations -----
# "Live-looking" without ever naming a real daemon: the pid recorded is this
# test process's own, which is unambiguously alive and unambiguously not a
# daemon. Nothing in this suite ever signals it.
LIVE_PID_FILE="$WORKDIR/live/.loom/.daemon.pid"
mkdir -p "$(dirname "$LIVE_PID_FILE")"
echo "$$" > "$LIVE_PID_FILE"

LIVE_PLAN_ERR="$WORKDIR/live-plan.err"
LIVE_PLAN="$( run_plan "$LIVE_PID_FILE" "$LIVE_PLAN_ERR" )"
live_rc=$?
check "$live_rc" "live pid file: --plan still exits 0 (a guard is not a failure)"
check "$([[ "$LIVE_PLAN" == GUARD* ]] && echo 0 || echo 1)" \
    "live pid file: planned as GUARD, not RUN (#6554 hazard is unreachable)" "$LIVE_PLAN"

LIVE_RESOLVED="$( run_resolve "$LIVE_PID_FILE" )"
check "$([[ "$LIVE_RESOLVED" != *$'\n'* ]] && echo 0 || echo 1)" \
    "live pid file: --resolve's stdout is exactly one line (guard evidence stays on stderr)" \
    "$LIVE_RESOLVED"
check "$([[ "$LIVE_RESOLVED" == *"--exclude loom-daemon"* ]] && echo 0 || echo 1)" \
    "live pid file: the resolved command excludes the loom-daemon package from the main invocation" \
    "$LIVE_RESOLVED"
check "$([[ "$LIVE_RESOLVED" == *"-p loom-daemon --lib --bins"* ]] && echo 0 || echo 1)" \
    "live pid file: the resolved command re-adds loom-daemon's own lib/bin tests separately" \
    "$LIVE_RESOLVED"
check "$([[ "$LIVE_RESOLVED" == *"-p loom-daemon --doc"* ]] && echo 0 || echo 1)" \
    "live pid file: the resolved command re-adds loom-daemon's doctests as their own invocation" \
    "$LIVE_RESOLVED"
check "$([[ "$LIVE_RESOLVED" != *"--test integration_security"* && "$LIVE_RESOLVED" != *"--test integration_factory_reset"* ]] && echo 0 || echo 1)" \
    "live pid file: the two host-mutating binaries are NOT in the re-added --test allowlist" \
    "$LIVE_RESOLVED"
check "$([[ "$LIVE_RESOLVED" == *"--test integration_basic"* ]] && echo 0 || echo 1)" \
    "live pid file: integration_basic (TEST_PREFIX-scoped cleanup, not host-wide) stays in the allowlist" \
    "$LIVE_RESOLVED"
check "$([[ "$LIVE_RESOLVED" == *"--nocapture"* ]] && echo 0 || echo 1)" \
    "live pid file: the original '-- --nocapture' suffix is preserved in every split invocation" \
    "$LIVE_RESOLVED"

# The guard must be LOUD on stderr — an invisible guard reads as "the full
# suite ran".
guard_err="$(cat "$LIVE_PLAN_ERR" 2>/dev/null)"
check "$([[ "$guard_err" == *"LIVE DAEMON DETECTED"* ]] && echo 0 || echo 1)" \
    "live pid file: the guard announces itself loudly on stderr" "$guard_err"
check "$([[ "$guard_err" == *"$LIVE_PID_FILE"* ]] && echo 0 || echo 1)" \
    "live pid file: the guard names the exact pid file it found" "$guard_err"
check "$([[ "$guard_err" == *"integration_security"* && "$guard_err" == *"integration_factory_reset"* ]] && echo 0 || echo 1)" \
    "live pid file: the guard names the two excluded binaries" "$guard_err"

# ---------- 3. a STALE pid file also trips the guard --------------------------
# The two guarded binaries kill every loom-* session unconditionally in
# setup(), so even a pid file naming a dead process is host state the guard
# must not silently run past (mirrors run-ci-suites.sh's EXISTENCE-not-
# liveness convention, #6386).
STALE_PID_FILE="$WORKDIR/stale/.loom/.daemon.pid"
mkdir -p "$(dirname "$STALE_PID_FILE")"
echo "2147483646" > "$STALE_PID_FILE"   # far above any live pid on a real host
STALE_PLAN="$( run_plan "$STALE_PID_FILE" )"
check "$([[ "$STALE_PLAN" == GUARD* ]] && echo 0 || echo 1)" \
    "stale pid file: still guarded (existence is the trigger, not liveness)" "$STALE_PLAN"

# ---------- 4. `none` means no candidates at all ------------------------------
NONE_PLAN="$( run_plan none )"
check "$([[ "$NONE_PLAN" == "RUN $TEST_CMD" ]] && echo 0 || echo 1)" \
    "candidates=none: nothing is detected, so the command RUNs unmodified (even on a host with a live daemon)" \
    "$NONE_PLAN"

# ---------- 5. --plan / --resolve run nothing ----------------------------------
# Neither mode ever shells out to `cargo`/`tmux` — proven the same way
# test-nextest-daemon-guard.sh proves it: a `set -x` trace of the guard
# invocation itself shows no such call (the printed-but-not-executed command
# string obviously still contains the literal token `cargo`, so this checks
# for an EXECUTED `+ cargo` trace line, not mere presence of the word).
TRACE="$(bash -x "$GUARD" --plan "$TEST_CMD" 2>&1 >/dev/null)"
check "$([[ "$TRACE" != *"+ cargo"* && "$TRACE" != *"+ tmux"* ]] && echo 0 || echo 1)" \
    "--plan invokes neither cargo nor tmux (hermetic: prints a decision, executes nothing)" \
    "$TRACE"

# ---------- 6. a missing command argument is rejected -------------------------
bad_out="$(bash "$GUARD" --plan 2>&1)"
bad_rc=$?
check "$([[ "$bad_rc" -eq 64 && "$bad_out" == *"Usage:"* ]] && echo 0 || echo 1)" \
    "a missing <cargo test command> argument is rejected before anything runs (rc=$bad_rc)" "$bad_out"

# ---------- 7. no trailing '-- <args>' in the input is handled ----------------
NO_POST_CMD="cargo test --workspace --locked --all-features --no-fail-fast"
NO_POST_RESOLVED="$(LOOM_CI_DAEMON_PIDFILE_CANDIDATES="$LIVE_PID_FILE" bash "$GUARD" --resolve "$NO_POST_CMD" 2>/dev/null)"
check "$([[ "$NO_POST_RESOLVED" == *"--exclude loom-daemon"* && "$NO_POST_RESOLVED" == *"-p loom-daemon --doc"* ]] && echo 0 || echo 1)" \
    "a command with no trailing '-- <args>' still resolves to a valid 3-way split" \
    "$NO_POST_RESOLVED"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
