#!/usr/bin/env bash
# test-run-ci-suites-serial-lane.sh — the serial lane in run-ci-suites.sh
# (issue #6622 AC5, evidence gathered on PR #6639).
#
# #6622 made the CI-wired shell suites run concurrently on the premise that
# they are "hermetic by construction". AC5 required an escape hatch for the
# case where that premise turns out to be false for a specific suite: it "gets
# fixed or explicitly pinned to a serial lane". test-loom-daemon-update.sh is
# the first occupant — it failed 2 of the 4 concurrent CI runs of PR #6639
# (different assertions each time, including one with no timing component)
# while passing every sequential run on main.
#
# Since #8087 that suite (and test-loom-daemon-start.sh, the lane's other
# occupant) is no longer wired into THIS runner at all — both drive the
# cli/loom-daemon-start.sh stub over `loom-daemon daemon-start` and moved to
# ci-excluded.txt / ci.yml's sequential "Native Port Suites" job. The pin is
# retained for the day either one returns to the concurrent pool; see Part 1's
# own note for how that changes what is asserted where.
#
# This suite asserts the lane's two guarantees:
#   1. Planning — the pinned suite is still planned to RUN (the lane changes
#      WHEN a suite runs, never WHETHER), is annotated as such, and the
#      live-daemon guard still wins over the lane.
#   2. Execution — a serial-lane suite's execution window does not overlap
#      ANY other suite's window, proven by wall-clock timestamps recorded by
#      fixture suites in an isolated fixture repo (never the real manifest).
#
# The execution half runs against a throwaway fixture tree (a minimal
# defaults/scripts/{tests,lib} skeleton with fake suites), so it never
# executes a real suite, never touches the real ci-wired.txt, and cannot
# reach any daemon.
#
# Usage:
#   ./defaults/scripts/tests/test-run-ci-suites-serial-lane.sh

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
# Part 1 — planning (drives the real manifest via --plan, which
#          executes nothing).
# ==============================================================

plan_verdict() { # <plan output> <suite>  -> field 1 (RUN/SKIP)
    printf '%s\n' "$1" | awk -v s="$2" '$2 == s { print $1; exit }'
}
plan_full_line() { # <plan output> <suite>
    printf '%s\n' "$1" | awk -v s="$2" '$2 == s { print; exit }'
}

# `none` pins the live-daemon guard's candidate list to empty, so these cases
# assert the SERIAL LANE's behavior rather than whatever daemon state the host
# running this test happens to have.
PLAN_NO_DAEMON="$( LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none bash "$RUNNER" --plan 2>/dev/null )"

# The default occupants, as explicit literals: a silent removal from
# SERIAL_LANE_SUITES must fail this test rather than quietly returning either
# suite to the concurrent pool.
#
# Since #8087 BOTH are unwired — cli/loom-daemon-start.sh became a thin stub
# over `loom-daemon daemon-start`, so test-loom-daemon-start.sh and
# test-loom-daemon-update.sh (which reaches that script through
# lib/daemon-update-fixtures.sh) need a built binary and moved to
# ci-excluded.txt, wired in ci.yml's "Native Port Suites" job instead. So lane
# MEMBERSHIP and CI WIRING stopped being the same question here exactly as
# #8086 made them stop being the same question for the live-daemon guard in
# test-run-ci-suites-daemon-guard.sh — and the response is the same one: assert
# them DIFFERENTLY, not less.
#
#   - retention is asserted against the runner's own literal + their absence
#     from --plan (which only ever enumerates ci-wired.txt), so neither a
#     silent removal of the pin NOR a half-applied exclusion can pass;
#   - the lane MECHANISM (defer-not-drop, the annotation, the disable switch,
#     the guard outranking it) is exercised through the documented
#     LOOM_CI_SERIAL_SUITES seam against suites that ARE wired, so it stays
#     covered rather than becoming vacuous.
DEFAULT_LANE_SUITES=(
    test-loom-daemon-update.sh
    test-loom-daemon-start.sh
)

# The seam occupant: wired, hermetic, and NOT a LIVE_DAEMON_GUARDED_SUITES
# member, so the guard never confounds the lane assertions it drives.
PINNED_SUITE="test-locate-daemon-bin.sh"
# A wired suite that IS guarded, for the "guard outranks the lane" case below.
GUARDED_PINNED_SUITE="test-loom-daemon-stop.sh"

lane_plan() { # <lane suites> -> --plan output with the lane pinned to them
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none LOOM_CI_SERIAL_SUITES="$1" \
        bash "$RUNNER" --plan 2>/dev/null
}

# Guard every "is NOT annotated" assertion below against passing vacuously on
# an empty plan (e.g. a manifest-invariant failure, which exits before
# printing anything).
check "$([[ -n "$(plan_verdict "$PLAN_NO_DAEMON" "test-live-state-sandbox.sh")" ]] && echo 0 || echo 1)" \
    "--plan produced a plan at all (guards the negative assertions below)" \
    "$PLAN_NO_DAEMON"

# ---- the default pin is retained, and its suites really are excluded --------
unnamed="" still_planned=""
for suite in "${DEFAULT_LANE_SUITES[@]}"; do
    grep -q "SERIAL_LANE_SUITES=.*$suite" "$RUNNER" || unnamed="$unnamed $suite"
    [[ -z "$(plan_verdict "$PLAN_NO_DAEMON" "$suite")" ]] || still_planned="$still_planned $suite"
done
check "$([[ -z "$unnamed" ]] && echo 0 || echo 1)" \
    "the default serial-lane suites are still named in SERIAL_LANE_SUITES (no silent unpinning)" \
    "not named in the runner:$unnamed"
check "$([[ -z "$still_planned" ]] && echo 0 || echo 1)" \
    "the default serial-lane suites are absent from --plan (the #8087 ci-excluded move is real)" \
    "still planned:$still_planned"

# ---- the lane mechanism, driven through the seam on a wired suite -----------
PLAN_LANE="$( lane_plan "$PINNED_SUITE" )"

check "$([[ "$(plan_verdict "$PLAN_LANE" "$PINNED_SUITE")" == "RUN" ]] && echo 0 || echo 1)" \
    "serial-lane suite is still planned to RUN (the lane defers a suite, never drops it)" \
    "$(plan_full_line "$PLAN_LANE" "$PINNED_SUITE")"

check "$(plan_full_line "$PLAN_LANE" "$PINNED_SUITE" | grep -q 'serial lane' && echo 0 || echo 1)" \
    "serial-lane suite is annotated as such in --plan (the lane is observable, not silent)" \
    "$(plan_full_line "$PLAN_LANE" "$PINNED_SUITE")"

# A pooled control: an ordinary suite must NOT carry the annotation, otherwise
# the assertion above would pass even if every suite were annotated.
check "$(plan_full_line "$PLAN_LANE" "test-live-state-sandbox.sh" | grep -q 'serial lane' && echo 1 || echo 0)" \
    "an ordinary suite is NOT annotated as serial lane (control)" \
    "$(plan_full_line "$PLAN_LANE" "test-live-state-sandbox.sh")"

# Empty LOOM_CI_SERIAL_SUITES disables the lane entirely.
PLAN_NO_LANE="$( lane_plan '' )"
check "$(plan_full_line "$PLAN_NO_LANE" "$PINNED_SUITE" | grep -q 'serial lane' && echo 1 || echo 0)" \
    "LOOM_CI_SERIAL_SUITES= disables the lane (suite returns to the pool)" \
    "$(plan_full_line "$PLAN_NO_LANE" "$PINNED_SUITE")"
check "$([[ "$(plan_verdict "$PLAN_NO_LANE" "$PINNED_SUITE")" == "RUN" ]] && echo 0 || echo 1)" \
    "LOOM_CI_SERIAL_SUITES= still plans the suite to RUN"

# The live-daemon guard (#6386) outranks the lane: a guarded suite is SKIPped
# outright, never merely deferred. "Live-looking" without naming a real
# daemon — the pid recorded is this test process's own.
LIVE_PID_FILE="$WORKDIR/live.pid"
echo "$$" > "$LIVE_PID_FILE"
PLAN_LIVE="$( LOOM_CI_DAEMON_PIDFILE_CANDIDATES="$LIVE_PID_FILE" \
    LOOM_CI_SERIAL_SUITES="$GUARDED_PINNED_SUITE" bash "$RUNNER" --plan 2>/dev/null )"
check "$([[ "$(plan_verdict "$PLAN_LIVE" "$GUARDED_PINNED_SUITE")" == "SKIP" ]] && echo 0 || echo 1)" \
    "live-daemon guard still outranks the serial lane (guarded suite is SKIP, not deferred)" \
    "$(plan_full_line "$PLAN_LIVE" "$GUARDED_PINNED_SUITE")"

# ==============================================================
# Part 2 — execution, against an isolated fixture repo.
# ==============================================================

FIXTURE="$WORKDIR/fixture"
FIX_TESTS="$FIXTURE/defaults/scripts/tests"
FIX_LIB="$FIXTURE/defaults/scripts/lib"
mkdir -p "$FIX_TESTS" "$FIX_LIB" "$FIX_TESTS/lib"
cp "$RUNNER" "$SCRIPT_DIR/check-ci-suite-manifest.sh" "$FIX_TESTS/"
cp "$REPO_ROOT/defaults/scripts/lib/live-daemon-guard.sh" \
   "$REPO_ROOT/defaults/scripts/lib/cpu-budget.sh" "$FIX_LIB/"
# The #8077 live-host leak guard (tests/lib/, not scripts/lib/). The runner
# sources it fail-CLOSED, so a fixture without it exits 1 before running a
# single suite.
cp "$SCRIPT_DIR/lib/live-state-sandbox.sh" "$FIX_TESTS/lib/"

# Fixture suites record their own start/end wall-clock, so overlap is measured
# rather than inferred. $LANE_OUT is exported below and inherited by every
# suite the runner launches.
LANE_OUT="$WORKDIR/windows"
mkdir -p "$LANE_OUT"
export LANE_OUT

make_fixture_suite() { # <basename> <sleep-seconds>
    cat > "$FIX_TESTS/$1" <<EOF
#!/usr/bin/env bash
# fixture suite (test-run-ci-suites-serial-lane.sh) — records its own window
printf '%s ' "\$(date +%s%N)" > "\$LANE_OUT/$1"
sleep $2
printf '%s\n' "\$(date +%s%N)" >> "\$LANE_OUT/$1"
exit 0
EOF
    chmod +x "$FIX_TESTS/$1"
}

SERIAL_FIXTURE="test-fixture-lane-serial.sh"
make_fixture_suite "$SERIAL_FIXTURE" 1
POOLED_FIXTURES=(
    test-fixture-lane-pool-a.sh
    test-fixture-lane-pool-b.sh
    test-fixture-lane-pool-c.sh
    test-fixture-lane-pool-d.sh
)
for s in "${POOLED_FIXTURES[@]}"; do make_fixture_suite "$s" 1; done

# Manifest order deliberately puts the serial suite FIRST: if the lane were
# only "run it last in manifest order" rather than a real post-drain pass,
# this ordering would expose it.
{
    echo "$SERIAL_FIXTURE"
    printf '%s\n' "${POOLED_FIXTURES[@]}"
} > "$FIX_TESTS/ci-wired.txt"
: > "$FIX_TESTS/ci-excluded.txt"

LANE_RUN_OUT="$( cd "$FIXTURE" && \
    LOOM_CI_DAEMON_PIDFILE_CANDIDATES=none \
    LOOM_CI_SERIAL_SUITES="$SERIAL_FIXTURE" \
    LOOM_CI_PARALLELISM=4 \
    bash "$FIX_TESTS/run-ci-suites.sh" 2>&1 )"
lane_rc=$?

check "$lane_rc" "fixture run exits 0 (all five fixture suites pass)" "$LANE_RUN_OUT"

check "$(printf '%s\n' "$LANE_RUN_OUT" | grep -q "Serial lane (run alone after the pool drains" && echo 0 || echo 1)" \
    "the run announces its serial lane" \
    "$LANE_RUN_OUT"

# Every fixture suite must actually have run — otherwise the overlap assertion
# below would pass vacuously.
missing=""
for s in "$SERIAL_FIXTURE" "${POOLED_FIXTURES[@]}"; do
    [[ -s "$LANE_OUT/$s" ]] || missing="$missing $s"
done
check "$([[ -z "$missing" ]] && echo 0 || echo 1)" \
    "every fixture suite recorded a window (the overlap check is not vacuous)" \
    "no window recorded for:$missing"

read -r serial_start serial_end < "$LANE_OUT/$SERIAL_FIXTURE"

overlaps=""
for s in "${POOLED_FIXTURES[@]}"; do
    read -r p_start p_end < "$LANE_OUT/$s"
    # Half-open overlap test: [a,b) intersects [c,d) iff a < d && c < b.
    if [[ "$serial_start" -lt "$p_end" && "$p_start" -lt "$serial_end" ]]; then
        overlaps="$overlaps $s"
    fi
done
check "$([[ -z "$overlaps" ]] && echo 0 || echo 1)" \
    "serial-lane suite never overlaps a pooled suite (it ran alone)" \
    "overlapping windows:$overlaps"

# The pool itself must still be concurrent — a lane that accidentally
# serialized EVERYTHING would pass the assertion above while destroying the
# whole point of #6622.
pool_overlap_found=1
for i in "${!POOLED_FIXTURES[@]}"; do
    for j in "${!POOLED_FIXTURES[@]}"; do
        [[ "$i" -lt "$j" ]] || continue
        read -r a_start a_end < "$LANE_OUT/${POOLED_FIXTURES[$i]}"
        read -r b_start b_end < "$LANE_OUT/${POOLED_FIXTURES[$j]}"
        if [[ "$a_start" -lt "$b_end" && "$b_start" -lt "$a_end" ]]; then
            pool_overlap_found=0
        fi
    done
done
check "$pool_overlap_found" \
    "pooled suites still overlap each other (the lane did not serialize the pool)"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]] || exit 1
exit 0
