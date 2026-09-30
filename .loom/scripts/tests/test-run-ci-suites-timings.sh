#!/usr/bin/env bash
# test-run-ci-suites-timings.sh — the per-suite timings record run-ci-suites.sh
# writes for `loom-daemon ci-telemetry` to turn into `loom.ci.suite` spans
# (issue #9089).
#
# The printed report has always named each suite's duration, but only a human
# reading one job's log ever saw it: rebalancing the LOOM_CI_SHARD legs (#9065)
# and answering "which suite made this leg slow" both had to be done by eye.
# This suite asserts the machine-readable record that replaces that: it exists,
# it is valid JSON, it carries this shard's identity, and every suite in the
# manifest appears in it with an outcome and an absolute window — including the
# ones that did not run, whose window is zero rather than absent (a suite that
# ran in no time and one that never started must stay distinguishable).
#
# Driven against an isolated fixture repo (a minimal defaults/scripts/{tests,
# lib} skeleton with fake suites), never the real manifest or a real suite — so
# this is hermetic and fast.
#
# Usage:
#   ./defaults/scripts/tests/test-run-ci-suites-timings.sh

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

if ! command -v python3 >/dev/null 2>&1; then
    echo "SKIP: python3 is required to validate the JSON record" >&2
    exit 0
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# ==============================================================
# Fixture repo — a throwaway defaults/scripts/{tests,lib} skeleton so this
# suite never touches the real ci-wired.txt and executes no real suite.
# ==============================================================
FIXTURE="$WORKDIR/fixture"
FIX_TESTS="$FIXTURE/defaults/scripts/tests"
FIX_LIB="$FIXTURE/defaults/scripts/lib"
mkdir -p "$FIX_TESTS" "$FIX_LIB" "$FIX_TESTS/lib"
cp "$RUNNER" "$SCRIPT_DIR/check-ci-suite-manifest.sh" "$FIX_TESTS/"
cp "$REPO_ROOT/defaults/scripts/lib/live-daemon-guard.sh" \
   "$REPO_ROOT/defaults/scripts/lib/cpu-budget.sh" \
   "$REPO_ROOT/defaults/scripts/lib/ci-suite-excerpt.sh" "$FIX_LIB/"
cp "$SCRIPT_DIR/lib/live-state-sandbox.sh" "$FIX_TESTS/lib/"

make_suite() { # <basename> <exit-code> [sleep-seconds]
    local name="$1" rc="$2" nap="${3:-0}"
    cat > "$FIX_TESTS/$name" <<EOF
#!/usr/bin/env bash
sleep $nap
exit $rc
EOF
    chmod +x "$FIX_TESTS/$name"
}

PASS_SUITE="test-fixture-timings-pass.sh"
SLOW_SUITE="test-fixture-timings-slow.sh"
DEAD_SUITE="test-fixture-timings-dead.sh"
make_suite "$PASS_SUITE" 0
make_suite "$SLOW_SUITE" 0 2
make_suite "$DEAD_SUITE" 1

# write_manifest <suite>... — see test-run-ci-suites-retry.sh; every fixture
# suite on disk must be in exactly one of the two manifests.
write_manifest() {
    : > "$FIX_TESTS/ci-wired.txt"
    for s in "$@"; do printf '%s\n' "$s" >> "$FIX_TESTS/ci-wired.txt"; done
    : > "$FIX_TESTS/ci-excluded.txt"
    local f name wired
    # Membership tested with a shell loop rather than `printf … | grep -qxF`:
    # under `set -o pipefail` an early-exit consumer can SIGPIPE the producer
    # and fail the whole pipeline (scripts/check-pipefail-early-exit.sh).
    for f in "$FIX_TESTS"/test-*.sh; do
        [[ -e "$f" ]] || continue
        name="$(basename "$f")"
        local found=0
        for wired in "$@"; do
            [[ "$wired" == "$name" ]] && { found=1; break; }
        done
        if [[ "$found" -eq 0 ]]; then
            printf '%s  not used in this scenario (test-run-ci-suites-timings.sh)\n' "$name" \
                >> "$FIX_TESTS/ci-excluded.txt"
        fi
    done
}

run_fixture() { # extra env assignments as args
    # The GITHUB_* variables are STRIPPED before the caller's own assignments
    # are applied (`env -u` must precede any VAR=value operand). Without this
    # the fixture inherits the REAL Actions environment when this suite itself
    # runs in CI, so "a run outside Actions records run_id=local" would assert
    # against the live GITHUB_RUN_ID and pass only on a developer's laptop —
    # exactly how it failed on PR #9423. GITHUB_STEP_SUMMARY is stripped for a
    # second reason: left set, each fixture run would append its retry section
    # to the real job summary.
    ( cd "$FIXTURE" && \
        env -u GITHUB_RUN_ID -u GITHUB_RUN_ATTEMPT -u GITHUB_STEP_SUMMARY \
        LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none \
        LOOM_CI_SERIAL_SUITES='' \
        LOOM_CI_PARALLELISM=3 \
        "$@" \
        bash "$FIX_TESTS/run-ci-suites.sh" 2>&1 )
}

# q <file> <python-expression over `d`> — read one value out of the record.
q() {
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print($2)
" "$1" 2>&1
}

# ==============================================================
# 1. The record exists, parses, and describes this shard.
# ==============================================================
write_manifest "$PASS_SUITE" "$SLOW_SUITE"
TIMINGS_1="$WORKDIR/timings-1.json"
OUT1="$( run_fixture LOOM_CI_SUITE_TIMINGS="$TIMINGS_1" \
    LOOM_CI_RETRY_LOG="$WORKDIR/retry-1.tsv" \
    LOOM_CI_SHARD=1/1 GITHUB_RUN_ID=424242 GITHUB_RUN_ATTEMPT=2 )"
RC1=$?

check "$RC1" "an all-passing sharded run exits 0" "$OUT1"

check "$([[ -f "$TIMINGS_1" ]] && echo 0 || echo 1)" \
    "the timings record file was written" "path: $TIMINGS_1"

check "$(python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$TIMINGS_1" \
    >/dev/null 2>&1 && echo 0 || echo 1)" \
    "the timings record is valid JSON" "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "d['schema']")" == "loom.ci.suite-timings/1" ]] && echo 0 || echo 1)" \
    "the record names its schema (the consumer's recognition key)" \
    "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "d['shard']")" == "1/1" ]] && echo 0 || echo 1)" \
    "the record carries this leg's LOOM_CI_SHARD k/N — what pairs it with a job span" \
    "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "d['run_id']")" == "424242" ]] && echo 0 || echo 1)" \
    "the record carries GITHUB_RUN_ID" "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "d['run_attempt']")" == "2" ]] && echo 0 || echo 1)" \
    "the record carries GITHUB_RUN_ATTEMPT (a re-run is a distinct trace)" \
    "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "len(d['suites'])")" == "2" ]] && echo 0 || echo 1)" \
    "every suite the shard selected appears exactly once" "$(cat "$TIMINGS_1" 2>/dev/null)"

# ==============================================================
# 2. Each entry carries an absolute window, not only a duration — a span needs
#    a start and an end, and concurrent suites' windows legitimately overlap.
# ==============================================================
check "$([[ "$(q "$TIMINGS_1" "sorted(s['suite'] for s in d['suites'])==sorted(['$PASS_SUITE','$SLOW_SUITE'])")" == "True" ]] && echo 0 || echo 1)" \
    "entries are keyed by suite name" "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "all(s['outcome']=='pass' for s in d['suites'])")" == "True" ]] && echo 0 || echo 1)" \
    "a passing suite's outcome is 'pass'" "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "all(s['started_at_epoch']>0 and s['ended_at_epoch']>=s['started_at_epoch'] for s in d['suites'])")" == "True" ]] && echo 0 || echo 1)" \
    "every executed suite has a real, non-inverted absolute window" \
    "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "[s['ended_at_epoch']-s['started_at_epoch'] for s in d['suites'] if s['suite']=='$SLOW_SUITE'][0] >= 2")" == "True" ]] && echo 0 || echo 1)" \
    "a suite that slept 2s is recorded as having taken at least 2s" \
    "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_1" "all(s['retried'] is False for s in d['suites'])")" == "True" ]] && echo 0 || echo 1)" \
    "a first-time pass is recorded as retried=false (a JSON boolean, not \"0\")" \
    "$(cat "$TIMINGS_1" 2>/dev/null)"

check "$(grep -q "Per-suite timings (2 suite(s)" <<<"$OUT1" && echo 0 || echo 1)" \
    "the printed report names the record it wrote" "$OUT1"

# ==============================================================
# 3. A failing suite is recorded as failing — and does not suppress the record.
#    (A record that only exists for green runs is useless for the one question
#    it is for: what made this leg slow when it broke.)
# ==============================================================
write_manifest "$PASS_SUITE" "$DEAD_SUITE"
TIMINGS_2="$WORKDIR/timings-2.json"
OUT2="$( run_fixture LOOM_CI_SUITE_TIMINGS="$TIMINGS_2" \
    LOOM_CI_RETRY_LOG="$WORKDIR/retry-2.tsv" LOOM_CI_SHARD=2/2 )"
RC2=$?

check "$([[ "$RC2" -ne 0 ]] && echo 0 || echo 1)" \
    "a run with a failing suite still fails the job" "$OUT2"

check "$([[ -f "$TIMINGS_2" ]] && echo 0 || echo 1)" \
    "the timings record is still written when the job fails" "$OUT2"

check "$([[ "$(q "$TIMINGS_2" "[s['outcome'] for s in d['suites'] if s['suite']=='$DEAD_SUITE'][0]")" == "fail" ]] && echo 0 || echo 1)" \
    "a suite that failed both attempts is recorded with outcome 'fail'" \
    "$(cat "$TIMINGS_2" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_2" "[s['retried'] for s in d['suites'] if s['suite']=='$DEAD_SUITE'][0]")" == "True" ]] && echo 0 || echo 1)" \
    "a retried suite is recorded as retried=true, matching the #7791 retry log" \
    "$(cat "$TIMINGS_2" 2>/dev/null)"

check "$([[ "$(q "$TIMINGS_2" "d['shard']")" == "2/2" ]] && echo 0 || echo 1)" \
    "the second leg's record names shard 2/2, not 1/2" "$(cat "$TIMINGS_2" 2>/dev/null)"

# ==============================================================
# 4. An unsharded (local) run still writes a record, with an empty shard — the
#    consumer skips it rather than pairing it with an arbitrary leg.
# ==============================================================
write_manifest "$PASS_SUITE"
TIMINGS_3="$WORKDIR/timings-3.json"
OUT3="$( run_fixture LOOM_CI_SUITE_TIMINGS="$TIMINGS_3" \
    LOOM_CI_RETRY_LOG="$WORKDIR/retry-3.tsv" )"
RC3=$?

check "$RC3" "an unsharded run exits 0" "$OUT3"
check "$([[ "$(q "$TIMINGS_3" "repr(d['shard'])")" == "''" ]] && echo 0 || echo 1)" \
    "an unsharded run records an EMPTY shard rather than inventing 1/1" \
    "$(cat "$TIMINGS_3" 2>/dev/null)"
check "$([[ "$(q "$TIMINGS_3" "d['run_id']")" == "local" ]] && echo 0 || echo 1)" \
    "a run outside Actions records run_id=local" "$(cat "$TIMINGS_3" 2>/dev/null)"

# ==============================================================
# 5. The record can be turned off entirely, and nothing else changes.
# ==============================================================
TIMINGS_4="$WORKDIR/timings-4.json"
OUT4="$( run_fixture LOOM_CI_SUITE_TIMINGS='' \
    LOOM_CI_RETRY_LOG="$WORKDIR/retry-4.tsv" )"
RC4=$?
check "$RC4" "a run with the timings record disabled still exits 0" "$OUT4"
check "$([[ ! -f "$TIMINGS_4" ]] && echo 0 || echo 1)" \
    "LOOM_CI_SUITE_TIMINGS='' writes no record at all" "$OUT4"
check "$(grep -q "Per-suite timings" <<<"$OUT4" && echo 1 || echo 0)" \
    "a disabled record is not announced in the report" "$OUT4"

# ==============================================================
echo
echo "=== $TESTS_PASSED/$TESTS_RUN passed ==="
[[ "$TESTS_FAILED" -eq 0 ]] || exit 1
exit 0
