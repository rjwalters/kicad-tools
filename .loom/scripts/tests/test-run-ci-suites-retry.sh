#!/usr/bin/env bash
# test-run-ci-suites-retry.sh — retry-once-and-record semantics in
# run-ci-suites.sh (issue #7791).
#
# #7287 and its 2026-09-16 recurrence (#7789/#7791) established that a single
# flaky assertion anywhere among 223 suites fails the whole job, which blocks
# every concurrent PR. This suite asserts the fix: a suite that fails once and
# passes on retry does not fail the job, a suite that fails twice still fails
# the job, and — the part the issue calls "the whole design" — every retry is
# RECORDED (both in the printed report and in a durable machine-readable
# file), so a silent retry can never masquerade as a clean first-time pass.
#
# Driven against an isolated fixture repo (a minimal defaults/scripts/{tests,
# lib} skeleton with fake suites that flip pass/fail via a marker file), never
# the real manifest or a real suite — so this is hermetic and fast.
#
# Usage:
#   ./defaults/scripts/tests/test-run-ci-suites-retry.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RUNNER="$SCRIPT_DIR/run-ci-suites.sh"

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

# ==============================================================
# Fixture repo — a throwaway defaults/scripts/{tests,lib} skeleton so this
# suite never touches the real ci-wired.txt, executes no real suite, and
# cannot reach a live daemon.
# ==============================================================
FIXTURE="$WORKDIR/fixture"
FIX_TESTS="$FIXTURE/defaults/scripts/tests"
FIX_LIB="$FIXTURE/defaults/scripts/lib"
mkdir -p "$FIX_TESTS" "$FIX_LIB"
cp "$RUNNER" "$SCRIPT_DIR/check-ci-suite-manifest.sh" "$FIX_TESTS/"
cp "$REPO_ROOT/defaults/scripts/lib/live-daemon-guard.sh" \
   "$REPO_ROOT/defaults/scripts/lib/cpu-budget.sh" \
   "$REPO_ROOT/defaults/scripts/lib/ci-suite-excerpt.sh" "$FIX_LIB/"

MARKER_DIR="$WORKDIR/markers"
mkdir -p "$MARKER_DIR"
export MARKER_DIR

# make_fixture_suite <basename> <mode>
#   pass          — always exits 0.
#   fail-once     — exits 1 the FIRST time it is invoked (per test run, keyed
#                   by its own marker file), 0 every time after — i.e. the
#                   canonical "fails once then passes on retry" flake.
#   fail-always   — exits 1 every time (fails both attempts).
make_fixture_suite() {
    local name="$1" mode="$2"
    case "$mode" in
        pass)
            cat > "$FIX_TESTS/$name" <<'EOF'
#!/usr/bin/env bash
echo "PASS: always passes"
exit 0
EOF
            ;;
        fail-once)
            cat > "$FIX_TESTS/$name" <<EOF
#!/usr/bin/env bash
MARKER="\$MARKER_DIR/$name.seen"
if [[ -f "\$MARKER" ]]; then
    echo "PASS: second invocation (retry)"
    exit 0
else
    : > "\$MARKER"
    echo "FAIL: first invocation (deliberate flake)"
    exit 1
fi
EOF
            ;;
        fail-always)
            cat > "$FIX_TESTS/$name" <<EOF
#!/usr/bin/env bash
echo "FAIL: this suite never passes"
exit 1
EOF
            ;;
    esac
    chmod +x "$FIX_TESTS/$name"
}

PASS_SUITE="test-fixture-retry-pass.sh"
FLAKY_SUITE="test-fixture-retry-flaky.sh"
DEAD_SUITE="test-fixture-retry-dead.sh"
make_fixture_suite "$PASS_SUITE" pass
make_fixture_suite "$FLAKY_SUITE" fail-once
make_fixture_suite "$DEAD_SUITE" fail-always

# write_manifest <suite>... — (re)writes ci-wired.txt to exactly the given
# suites, so each scenario below controls precisely which fixture suites
# participate (a fail-always suite in the manifest would fail the WHOLE job,
# which several scenarios below must not do). Any test-*.sh fixture file that
# already exists in $FIX_TESTS but is NOT one of the given suites is written
# to ci-excluded.txt instead — check-ci-suite-manifest.sh (run as step 1 of
# every run-ci-suites.sh invocation) requires every test-*.sh on disk to be
# in exactly one of the two manifests, regardless of which scenario is using
# the fixture directory at the moment.
write_manifest() {
    : > "$FIX_TESTS/ci-wired.txt"
    for s in "$@"; do printf '%s\n' "$s" >> "$FIX_TESTS/ci-wired.txt"; done
    : > "$FIX_TESTS/ci-excluded.txt"
    local f name
    for f in "$FIX_TESTS"/test-*.sh; do
        [[ -e "$f" ]] || continue
        name="$(basename "$f")"
        if ! printf '%s\n' "$@" | grep -qxF "$name"; then
            printf '%s  not used in this scenario (test-run-ci-suites-retry.sh)\n' "$name" \
                >> "$FIX_TESTS/ci-excluded.txt"
        fi
    done
}

run_fixture() { # extra env assignments as args, e.g. run_fixture FOO=bar
    ( cd "$FIXTURE" && \
        env LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none \
        LOOM_CI_SERIAL_SUITES='' \
        LOOM_CI_PARALLELISM=3 \
        "$@" \
        bash "$FIX_TESTS/run-ci-suites.sh" 2>&1 )
}

# ==============================================================
# 1. fail-once-then-pass: the job still passes, and the retry is visible.
# ==============================================================
write_manifest "$PASS_SUITE" "$FLAKY_SUITE"
rm -f "$MARKER_DIR/$FLAKY_SUITE.seen"
RETRY_LOG_1="$WORKDIR/retry-log-1.tsv"
OUT1="$( run_fixture LOOM_CI_RETRY_LOG="$RETRY_LOG_1" )"
RC1=$?

check "$RC1" "a suite that fails once then passes on retry: job still exits 0" "$OUT1"

check "$(grep -qE "PASS  $FLAKY_SUITE .*retried once" <<<"$OUT1" && echo 0 || echo 1)" \
    "the retry is visible in the printed report, distinctly from a plain PASS" "$OUT1"

check "$(grep -q "Retried suites (1" <<<"$OUT1" && echo 0 || echo 1)" \
    "the run-level summary names exactly one retried suite" "$OUT1"

# ==============================================================
# 2. a durable, queryable record is produced — and distinguishes a
#    zero-retry run from a retried one.
# ==============================================================
check "$([[ -f "$RETRY_LOG_1" ]] && echo 0 || echo 1)" \
    "the durable retry record file was written" "path: $RETRY_LOG_1"

check "$(grep -q "retried_count=1" "$RETRY_LOG_1" && echo 0 || echo 1)" \
    "the durable record's header states retried_count=1" "$(cat "$RETRY_LOG_1" 2>/dev/null)"

check "$(grep -qE "^${FLAKY_SUITE}"$'\t'"1"$'\t'"0"$'\t'"PASS$" "$RETRY_LOG_1" && echo 0 || echo 1)" \
    "the record's line for the flaky suite names suite, first exit 1, retry exit 0, outcome PASS" \
    "$(cat "$RETRY_LOG_1" 2>/dev/null)"

check "$(grep -q "^${PASS_SUITE}" "$RETRY_LOG_1" && echo 1 || echo 0)" \
    "a suite that passed FIRST TIME is NOT in the retry record (control)" \
    "$(cat "$RETRY_LOG_1" 2>/dev/null)"

# A separate, all-pass run (no flake tripped) must be distinguishable from the
# retried run above: retried_count=0, and no suite lines at all.
rm -f "$MARKER_DIR/$FLAKY_SUITE.seen"
: > "$MARKER_DIR/$FLAKY_SUITE.seen"   # pre-seed so it passes on its FIRST attempt this run
RETRY_LOG_0="$WORKDIR/retry-log-0.tsv"
OUT0="$( run_fixture LOOM_CI_RETRY_LOG="$RETRY_LOG_0" )"
RC0=$?
check "$RC0" "a zero-retry run (no suite flakes) still exits 0" "$OUT0"
check "$(grep -q "retried_count=0" "$RETRY_LOG_0" && echo 0 || echo 1)" \
    "a zero-retry run's durable record states retried_count=0 — distinguishable from the retried run above" \
    "$(cat "$RETRY_LOG_0" 2>/dev/null)"
check "$(grep -qE "^${FLAKY_SUITE}"$'\t' "$RETRY_LOG_0" && echo 1 || echo 0)" \
    "a zero-retry run's record carries no per-suite line at all" \
    "$(cat "$RETRY_LOG_0" 2>/dev/null)"
check "$(grep -q "retried once" <<<"$OUT0" && echo 1 || echo 0)" \
    "a zero-retry run's printed report never claims a retry" "$OUT0"

# ==============================================================
# 3. fail-twice: the job fails, and the record shows BOTH exit codes.
# ==============================================================
write_manifest "$PASS_SUITE" "$DEAD_SUITE"
RETRY_LOG_2="$WORKDIR/retry-log-2.tsv"
OUT2="$( run_fixture LOOM_CI_RETRY_LOG="$RETRY_LOG_2" )"
RC2=$?

check "$([[ "$RC2" -ne 0 ]] && echo 0 || echo 1)" \
    "a suite that fails BOTH attempts still fails the job" "$OUT2"

check "$(grep -q "Failed suites:.*$DEAD_SUITE" <<<"$OUT2" && echo 0 || echo 1)" \
    "the always-failing suite is named in the job's failure summary" "$OUT2"

check "$(grep -qE "FAIL  $DEAD_SUITE .*failed both attempts" <<<"$OUT2" && echo 0 || echo 1)" \
    "the report distinguishes a double-failure from a single-attempt failure" "$OUT2"

check "$(grep -qE "^${DEAD_SUITE}"$'\t'"1"$'\t'"1"$'\t'"FAIL$" "$RETRY_LOG_2" && echo 0 || echo 1)" \
    "the durable record shows both exit codes for the double-failing suite" \
    "$(cat "$RETRY_LOG_2" 2>/dev/null)"

# ==============================================================
# 4. $GITHUB_STEP_SUMMARY integration (best-effort surface for CI, #7791).
# ==============================================================
write_manifest "$PASS_SUITE" "$FLAKY_SUITE"
rm -f "$MARKER_DIR/$FLAKY_SUITE.seen"
SUMMARY_FILE="$WORKDIR/step-summary.md"
: > "$SUMMARY_FILE"
OUT3="$( cd "$FIXTURE" && \
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none LOOM_CI_SERIAL_SUITES='' LOOM_CI_PARALLELISM=3 \
    LOOM_CI_RETRY_LOG="$WORKDIR/retry-log-3.tsv" GITHUB_STEP_SUMMARY="$SUMMARY_FILE" \
    bash "$FIX_TESTS/run-ci-suites.sh" 2>&1 )"
RC3=$?
check "$RC3" "the GITHUB_STEP_SUMMARY scenario run itself still exits 0" "$OUT3"
check "$(grep -q "CI suite retries" "$SUMMARY_FILE" && echo 0 || echo 1)" \
    "GITHUB_STEP_SUMMARY receives a retry section when set" "$(cat "$SUMMARY_FILE" 2>/dev/null)"
check "$(grep -q "$FLAKY_SUITE" "$SUMMARY_FILE" && echo 0 || echo 1)" \
    "GITHUB_STEP_SUMMARY names the retried suite" "$(cat "$SUMMARY_FILE" 2>/dev/null)"

# ==============================================================
# 5. serial-lane suite retried once — still runs alone (#7791 x #6622 AC5).
# ==============================================================
LANE_OUT_DIR="$WORKDIR/lane-windows"
mkdir -p "$LANE_OUT_DIR"
export LANE_OUT_DIR
POOL_A="test-fixture-retry-pool-a.sh"
cat > "$FIX_TESTS/$POOL_A" <<'EOF'
#!/usr/bin/env bash
printf '%s ' "$(date +%s%N)" > "$LANE_OUT_DIR/pool-a"
sleep 1
printf '%s\n' "$(date +%s%N)" >> "$LANE_OUT_DIR/pool-a"
exit 0
EOF
chmod +x "$FIX_TESTS/$POOL_A"

# Overwrite the flaky suite's own script so its (single) successful attempt
# also records a wall-clock window, comparable against the pooled suite's.
LANE_FLAKY="$FIX_TESTS/$FLAKY_SUITE"
cat > "$LANE_FLAKY" <<EOF
#!/usr/bin/env bash
MARKER="\$MARKER_DIR/$FLAKY_SUITE.seen"
if [[ -f "\$MARKER" ]]; then
    printf '%s ' "\$(date +%s%N)" > "\$LANE_OUT_DIR/flaky"
    sleep 1
    printf '%s\n' "\$(date +%s%N)" >> "\$LANE_OUT_DIR/flaky"
    exit 0
else
    : > "\$MARKER"
    exit 1
fi
EOF
chmod +x "$LANE_FLAKY"

write_manifest "$FLAKY_SUITE" "$POOL_A"
rm -f "$MARKER_DIR/$FLAKY_SUITE.seen"

LANE_OUT="$( cd "$FIXTURE" && \
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none \
    LOOM_CI_SERIAL_SUITES="$FLAKY_SUITE" \
    LOOM_CI_PARALLELISM=4 \
    LOOM_CI_RETRY_LOG="$WORKDIR/retry-log-lane.tsv" \
    bash "$FIX_TESTS/run-ci-suites.sh" 2>&1 )"
LANE_RC=$?
check "$LANE_RC" "serial-lane suite retried once: job still exits 0" "$LANE_OUT"
check "$(grep -qE "PASS  $FLAKY_SUITE .*retried once" <<<"$LANE_OUT" && echo 0 || echo 1)" \
    "serial-lane suite's retry is reported exactly like a pooled suite's" "$LANE_OUT"

# Both fixture suites must have actually run before the overlap check means
# anything.
if [[ -s "$LANE_OUT_DIR/flaky" && -s "$LANE_OUT_DIR/pool-a" ]]; then
    read -r flaky_start flaky_end < "$LANE_OUT_DIR/flaky"
    read -r pool_start pool_end < "$LANE_OUT_DIR/pool-a"
    if [[ "$flaky_start" -lt "$pool_end" && "$pool_start" -lt "$flaky_end" ]]; then
        overlap_rc=1
    else
        overlap_rc=0
    fi
    check "$overlap_rc" \
        "the serial-lane suite's (retried) run still never overlaps the pooled suite"
else
    fail "both the serial-lane and pooled fixture suites recorded a window (retry did not skip execution)"
fi

# ==============================================================
# 6. a suite that TIMES OUT (not just exits non-zero) on its first attempt —
#    the retry-once policy still applies, exactly as for an ordinary failure.
# ==============================================================
if command -v timeout >/dev/null 2>&1 || command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_SUITE="test-fixture-retry-timeout.sh"
    cat > "$FIX_TESTS/$TIMEOUT_SUITE" <<EOF
#!/usr/bin/env bash
MARKER="\$MARKER_DIR/$TIMEOUT_SUITE.seen"
if [[ -f "\$MARKER" ]]; then
    echo "PASS: second invocation, well under the timeout"
    exit 0
else
    : > "\$MARKER"
    echo "hanging past the per-suite timeout"
    sleep 5
    exit 0
fi
EOF
    chmod +x "$FIX_TESTS/$TIMEOUT_SUITE"
    write_manifest "$TIMEOUT_SUITE"
    rm -f "$MARKER_DIR/$TIMEOUT_SUITE.seen"

    RETRY_LOG_TIMEOUT="$WORKDIR/retry-log-timeout.tsv"
    TIMEOUT_OUT="$( cd "$FIXTURE" && \
        env LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none LOOM_CI_SERIAL_SUITES='' \
        LOOM_CI_PARALLELISM=1 LOOM_CI_SUITE_TIMEOUT=1 \
        LOOM_CI_RETRY_LOG="$RETRY_LOG_TIMEOUT" \
        bash "$FIX_TESTS/run-ci-suites.sh" 2>&1 )"
    TIMEOUT_RC=$?

    check "$TIMEOUT_RC" \
        "a first attempt that TIMES OUT is still retried once, and the retry passing clears the job" \
        "$TIMEOUT_OUT"
    check "$(grep -qE "PASS  $TIMEOUT_SUITE .*retried once" <<<"$TIMEOUT_OUT" && echo 0 || echo 1)" \
        "a timed-out first attempt is reported as a retry exactly like an ordinary exit-code failure" \
        "$TIMEOUT_OUT"
    check "$(grep -qE "^${TIMEOUT_SUITE}"$'\t'"124"$'\t'"0"$'\t'"PASS$" "$RETRY_LOG_TIMEOUT" && echo 0 || echo 1)" \
        "the durable record captures the timeout's own exit code (124) as the first attempt" \
        "$(cat "$RETRY_LOG_TIMEOUT" 2>/dev/null)"
else
    echo "SKIP: no timeout/gtimeout binary on this host — cannot exercise the timeout-retry edge case"
fi

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]] || exit 1
exit 0
