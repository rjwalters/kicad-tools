#!/usr/bin/env bash
# test-nextest-daemon-guard.sh — the live-daemon guard for the Rust
# `daemon-integration` nextest group (issue #6528).
#
# #6528: `cargo nextest run --workspace --profile ci` — this repo's own CI
# invocation — is unsafe to run directly on a host with a live `loom-daemon`
# and real tmux agent sessions: `integration_security.rs` and
# `integration_factory_reset.rs` call `cleanup_all_loom_sessions()` in their
# setup(), which kills EVERY `loom-*` tmux session on the shared, host-global
# `-L loom` socket — the same blast-radius class #6386 already fixed for the
# daemon-lifecycle shell suites. nextest-daemon-guard.sh closes the gap for
# this Rust test group the same way run-ci-suites.sh does for the shell ones.
#
# Driven entirely through `nextest-daemon-guard.sh --plan`/`--resolve`, which
# only print a decision and never invoke `cargo`/`tmux`/anything else — so
# this suite is hermetic and never touches a real daemon or tmux session,
# regardless of whether the host running it happens to have either.
#
# Also covers #6607: `integration_basic.rs` shares the same `-L loom` tmux
# socket hazard class but is non-destructive, so the guard must warn about it
# without adding it to the exclusion filter — see the dedicated assertions
# below.
#
# Every case pins the candidate pid-file list via the guard's shared
# TEST-ONLY seam `LOOM_CI_DAEMON_PIDFILE_CANDIDATES` (`none` = no candidates
# at all — see defaults/scripts/lib/live-daemon-guard.sh), so no assertion
# depends on whether THIS host happens to be running a real daemon.
#
# Usage:
#   ./defaults/scripts/tests/test-nextest-daemon-guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$SCRIPT_DIR/nextest-daemon-guard.sh"

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

NEXTEST_CMD="cargo nextest run --workspace --profile ci"
EXCLUDE_FILTER="not binary(integration_security) and not binary(integration_factory_reset)"

# Every invocation below pins the candidate list, so nothing about the host
# running this suite can influence the outcome.
run_plan() { # <pidfile-candidates|none> [stderr-path]
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES="$1" bash "$GUARD" --plan "$NEXTEST_CMD" 2>"${2:-/dev/null}"
}
run_resolve() { # <pidfile-candidates|none> [stderr-path]
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES="$1" bash "$GUARD" --resolve "$NEXTEST_CMD" 2>"${2:-/dev/null}"
}

# ---------- 1. no pid file anywhere -> RUN, unchanged command -----------------
ABSENT_PLAN="$( run_plan "$WORKDIR/absent-a.pid:$WORKDIR/absent-b.pid" )"
absent_rc=$?
check "$absent_rc" "no daemon pid file: --plan exits 0"
check "$([[ "$ABSENT_PLAN" == "RUN $NEXTEST_CMD" ]] && echo 0 || echo 1)" \
    "no daemon pid file: planned to RUN the unmodified command (guard is not always-on)" \
    "$ABSENT_PLAN"

ABSENT_RESOLVED="$( run_resolve "$WORKDIR/absent-a.pid" )"
check "$([[ "$ABSENT_RESOLVED" == "$NEXTEST_CMD" ]] && echo 0 || echo 1)" \
    "no daemon pid file: --resolve returns the command byte-for-byte unchanged" \
    "$ABSENT_RESOLVED"

# ---------- 2. a live-looking pid file -> GUARD, exclusion filter appended ----
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
    "live pid file: planned as GUARD, not RUN (#6528 hazard is unreachable)" "$LIVE_PLAN"
check "$([[ "$LIVE_PLAN" == *"-E '$EXCLUDE_FILTER'"* ]] && echo 0 || echo 1)" \
    "live pid file: the resolved command carries the two-binary exclusion filter" "$LIVE_PLAN"

LIVE_RESOLVED="$( run_resolve "$LIVE_PID_FILE" )"
check "$([[ "$LIVE_RESOLVED" == "$NEXTEST_CMD -E '$EXCLUDE_FILTER'" ]] && echo 0 || echo 1)" \
    "live pid file: --resolve appends the exclusion filter to the original command" \
    "$LIVE_RESOLVED"
check "$([[ "$LIVE_RESOLVED" != *$'\n'* ]] && echo 0 || echo 1)" \
    "live pid file: --resolve's stdout is exactly one line (guard evidence stays on stderr)" \
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

# `integration_basic` (#6607) is a related-but-different hazard: non-
# destructive, so it must stay in the run (not appear in the exclusion
# filter), but the guard should still warn loudly that it can flake under
# tmux contention with a live daemon.
check "$([[ "$LIVE_RESOLVED" != *"integration_basic"* ]] && echo 0 || echo 1)" \
    "live pid file: --resolve's exclusion filter does NOT name integration_basic (#6607, non-destructive)" \
    "$LIVE_RESOLVED"
check "$([[ "$guard_err" == *"integration_basic"* ]] && echo 0 || echo 1)" \
    "live pid file: the guard warns about integration_basic by name (#6607)" "$guard_err"
check "$([[ "$guard_err" == *"NOT excluded"* || "$guard_err" == *"FLAKE"* ]] && echo 0 || echo 1)" \
    "live pid file: the integration_basic warning is clearly non-excluding (#6607)" "$guard_err"

# ---------- 3. a STALE pid file also trips the guard --------------------------
# The daemon-integration binaries kill every loom-* session unconditionally in
# setup(), so even a pid file naming a dead process is host state the guard
# must not silently run past (mirrors run-ci-suites.sh's EXISTENCE-not-liveness
# convention, #6386).
STALE_PID_FILE="$WORKDIR/stale/.loom/.daemon.pid"
mkdir -p "$(dirname "$STALE_PID_FILE")"
echo "2147483646" > "$STALE_PID_FILE"   # far above any live pid on a real host
STALE_PLAN="$( run_plan "$STALE_PID_FILE" )"
check "$([[ "$STALE_PLAN" == GUARD* ]] && echo 0 || echo 1)" \
    "stale pid file: still guarded (existence is the trigger, not liveness)" "$STALE_PLAN"

# ---------- 4. `none` means no candidates at all ------------------------------
# The seam's empty setting has to be distinguishable from "seam not set", or a
# case that means "this host has nothing" would silently fall back to the
# derived tiers and assert against the REAL host — vacuous on CI, wrong on a
# fleet host.
NONE_PLAN="$( run_plan none )"
check "$([[ "$NONE_PLAN" == "RUN $NEXTEST_CMD" ]] && echo 0 || echo 1)" \
    "candidates=none: nothing is detected, so the command RUNs unmodified (even on a host with a live daemon)" \
    "$NONE_PLAN"

# ---------- 5. --plan / --resolve run nothing ----------------------------------
# Neither mode ever shells out to `cargo` or `tmux` — proven by asserting no
# `cargo`/`tmux` process is spawned as a *child* of the guard invocation. The
# guard's own body has no such call, so this is really just documenting the
# hermeticity contract; a `set -x` trace confirms no `cargo`/`tmux` token
# appears anywhere but inside the printed (not executed) command string.
TRACE="$(bash -x "$GUARD" --plan "$NEXTEST_CMD" 2>&1 >/dev/null)"
check "$([[ "$TRACE" != *"+ cargo"* && "$TRACE" != *"+ tmux"* ]] && echo 0 || echo 1)" \
    "--plan invokes neither cargo nor tmux (hermetic: prints a decision, executes nothing)" \
    "$TRACE"

# ---------- 6. a missing command argument is rejected -------------------------
bad_out="$(bash "$GUARD" --plan 2>&1)"
bad_rc=$?
check "$([[ "$bad_rc" -eq 64 && "$bad_out" == *"Usage:"* ]] && echo 0 || echo 1)" \
    "a missing <nextest command> argument is rejected before anything runs (rc=$bad_rc)" "$bad_out"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
