#!/usr/bin/env bash
# test-cpu-budget.sh — tests for defaults/scripts/lib/cpu-budget.sh (#5111,
# host-wide sharing added by #5979).
#
# The library owns two pieces of arithmetic that spawn-claude.sh depends on:
#
#   loom_cpu_budget_cores <total> <reserved> [in_flight]
#       max(1, floor(max(1, total - reserved) / in_flight)). #5979 added the
#       third argument: without it, three concurrent sweeps on an 8-core host
#       each computed "6 cores are mine" and summed to 18 (observed load
#       133.87). The argument defaults to 1 so every pre-#5979 two-argument
#       call is bit-identical.
#
#   loom_cpu_inflight_sweeps [repo_root]
#       "How many sweeps are live on this host, including mine." Every
#       dependency (jq, a daemon binary, a reachable daemon) is optional, and
#       every failure mode must answer `1` — the fail-safe that reproduces
#       pre-#5979 behavior exactly rather than starving a sweep.
#
# The daemon probe is exercised against a stub `loom-daemon` binary through
# $LOOM_DAEMON_BIN, so this suite is hermetic: it never contacts a real
# daemon, and its result does not depend on what the host happens to be
# running at the time.
#
# Usage:
#   bash defaults/scripts/tests/test-cpu-budget.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SCRIPT_DIR/../lib/cpu-budget.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_PASSED=$((TESTS_PASSED+1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_FAILED=$((TESTS_FAILED+1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_eq() { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1' want '$2')"; fi; }

if [[ ! -r "$LIB" ]]; then
    echo -e "${RED}FATAL${NC}: cannot read $LIB"
    exit 1
fi
# shellcheck source=../lib/cpu-budget.sh
source "$LIB"

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# Never let an ambient value from the caller's environment (a dispatched
# sweep inherits these) leak into the assertions below.
unset LOOM_SWEEP_INFLIGHT_SWEEPS LOOM_SWEEP_CLAIM_OWNED \
      LOOM_SWEEP_INFLIGHT_PROBE_TIMEOUT_SECS 2>/dev/null || true

echo "════════════════════════════════════════════"
echo "  cpu-budget.sh tests (#5111 / #5979)"
echo "════════════════════════════════════════════"

# ------------------------------------------------------------------
echo ""
echo "-- loom_cpu_total_cores --"
# ------------------------------------------------------------------

cores="$(loom_cpu_total_cores)"
if [[ "$cores" =~ ^[0-9]+$ ]] && ((cores >= 1)); then
    pass "loom_cpu_total_cores echoes a positive integer (got $cores)"
else
    fail "loom_cpu_total_cores echoes a positive integer (got '$cores')"
fi

# ------------------------------------------------------------------
echo ""
echo "-- loom_cpu_budget_cores: pre-#5979 two-argument contract --"
# ------------------------------------------------------------------

assert_eq "$(loom_cpu_budget_cores 8 2)" "6" \
    "two-arg call is unchanged: 8 cores - 2 reserved = 6"
assert_eq "$(loom_cpu_budget_cores 8 4)" "4" \
    "two-arg call honors a larger reservation: 8 - 4 = 4"
assert_eq "$(loom_cpu_budget_cores 8 99)" "1" \
    "an over-large reservation still clamps to the 1-core floor"
assert_eq "$(loom_cpu_budget_cores 1 2)" "1" \
    "a 1-core host with reserved=2 clamps to 1, never 0"
assert_eq "$(loom_cpu_budget_cores garbage 2)" "1" \
    "a non-numeric total degrades to 1 core rather than erroring"
assert_eq "$(loom_cpu_budget_cores 8 garbage)" "8" \
    "a non-numeric reservation is treated as 0"

# ------------------------------------------------------------------
echo ""
echo "-- loom_cpu_budget_cores: #5979 concurrent-sweep divisor --"
# ------------------------------------------------------------------

# The regression from the incident: an 8-core host, reserved 2, with three
# concurrent sweeps. Pre-#5979 each got 6 (18 total on 8 cores).
assert_eq "$(loom_cpu_budget_cores 8 2 1)" "6" \
    "1 in-flight sweep gets the full budget (no regression vs. #5111)"
assert_eq "$(loom_cpu_budget_cores 8 2 2)" "3" \
    "2 in-flight sweeps split the 6-core budget into 3 each"
assert_eq "$(loom_cpu_budget_cores 8 2 3)" "2" \
    "3 in-flight sweeps get 2 each — 6 total, not 18 (the #5979 incident)"
assert_eq "$(loom_cpu_budget_cores 8 2 4)" "1" \
    "4 in-flight sweeps get 1 each (floor(6/4) = 1)"
assert_eq "$(loom_cpu_budget_cores 8 2 6)" "1" \
    "6 in-flight sweeps get exactly 1 each — the budget divides evenly"
assert_eq "$(loom_cpu_budget_cores 8 2 20)" "1" \
    "more sweeps than cores still clamps to the 1-core floor, never 0"

# Division floors, so the shares never sum above the budget (except at the
# 1-core floor, where they structurally cannot).
assert_eq "$(loom_cpu_budget_cores 16 2 3)" "4" \
    "floor division: 14 usable / 3 sweeps = 4, not 5"
assert_eq "$(loom_cpu_budget_cores 64 2 5)" "12" \
    "floor division on a large host: 62 / 5 = 12"

# A garbage / zero divisor must never divide-by-zero or inflate the budget.
assert_eq "$(loom_cpu_budget_cores 8 2 0)" "6" \
    "a zero divisor degrades to 1 (no division by zero)"
assert_eq "$(loom_cpu_budget_cores 8 2 garbage)" "6" \
    "a non-numeric divisor degrades to 1"
assert_eq "$(loom_cpu_budget_cores 8 2 -3)" "6" \
    "a negative divisor degrades to 1"

# ------------------------------------------------------------------
# Shared "this host has no daemon" isolation (#8074)
# ------------------------------------------------------------------

# Every assertion whose expected answer is the no-daemon fail-safe must
# neutralize the WHOLE of loom_locate_daemon_bin's fallback chain, not just
# its first step. Setting $LOOM_DAEMON_BIN to a non-executable path only
# defeats step 1; on any host with a real `loom-daemon` installed (i.e. every
# fleet host — CI has none, which is why this was invisible there) resolution
# then falls through to PATH or $HOME/.local/bin and the probe queries the
# LIVE production daemon, answering its real in-flight count instead of 1.
#
# $NO_DAEMON_PATH is a PATH containing nothing but jq (which the probe needs
# before it ever looks for a daemon), so no `loom-daemon` is discoverable on
# it. $WORKDIR is the repo root every caller passes, and it holds no
# target/{release,debug}/loom-daemon build output, so the repo-local
# candidates cannot resolve either.
NO_DAEMON_PATH="$WORKDIR/empty-path"
mkdir -p "$NO_DAEMON_PATH"
if command -v jq >/dev/null 2>&1; then
    ln -sf "$(command -v jq)" "$NO_DAEMON_PATH/jq"
fi

# inflight_no_daemon <repo_root> — loom_cpu_inflight_sweeps with every
# daemon-resolution route closed off, so the result depends only on the
# library's logic and never on what this host happens to have installed.
inflight_no_daemon() {
    PATH="$NO_DAEMON_PATH" \
    LOOM_DAEMON_BIN=/nonexistent/loom-daemon \
    LOOM_DAEMON_BIN_DIR="$WORKDIR/no-such-bin-dir" \
    CARGO_TARGET_DIR="$WORKDIR/no-such-target-dir" \
    LOOM_PREFER_REPO_BUILD='' \
        loom_cpu_inflight_sweeps "$@" 2>/dev/null
}

# ------------------------------------------------------------------
echo ""
echo "-- loom_cpu_inflight_sweeps: explicit override --"
# ------------------------------------------------------------------

assert_eq "$(LOOM_SWEEP_INFLIGHT_SWEEPS=4 loom_cpu_inflight_sweeps "$WORKDIR")" "4" \
    "LOOM_SWEEP_INFLIGHT_SWEEPS wins outright — no daemon is consulted"
assert_eq "$(LOOM_SWEEP_INFLIGHT_SWEEPS=0 inflight_no_daemon "$WORKDIR")" "1" \
    "an override of 0 is rejected and falls through to the fail-safe"
assert_eq "$(LOOM_SWEEP_INFLIGHT_SWEEPS=nope inflight_no_daemon "$WORKDIR")" "1" \
    "a non-numeric override is rejected and falls through to the fail-safe"

# ------------------------------------------------------------------
echo ""
echo "-- loom_cpu_inflight_sweeps: no daemon reachable --"
# ------------------------------------------------------------------

# $LOOM_DAEMON_BIN pointing at a non-executable path, with PATH stripped of
# any real `loom-daemon`, is the "this host has no daemon" shape. It must
# answer 1 (pre-#5979 behavior), not fail the caller.
out="$(inflight_no_daemon "$WORKDIR")"
assert_eq "$out" "1" \
    "no locatable daemon binary answers the fail-safe 1"

# ------------------------------------------------------------------
echo ""
echo "-- loom_cpu_inflight_sweeps: daemon probe --"
# ------------------------------------------------------------------

if ! command -v jq >/dev/null 2>&1; then
    echo "  SKIP: jq unavailable — daemon-probe parsing tests need it"
else
    # Stub `loom-daemon` whose `status --json` prints whatever fixture the
    # test placed at $WORKDIR/status.json (and exits non-zero for anything
    # else, so an unexpected subcommand is loud rather than silently empty).
    STUB_DAEMON="$WORKDIR/loom-daemon"
    cat > "$STUB_DAEMON" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "status" && "\$2" == "--json" ]]; then
    cat "$WORKDIR/status.json"
    exit 0
fi
exit 1
STUB
    chmod +x "$STUB_DAEMON"

    probe() { LOOM_DAEMON_BIN="$STUB_DAEMON" loom_cpu_inflight_sweeps "$WORKDIR" 2>/dev/null; }

    # An idle daemon: no sweeps at all. The caller's own about-to-start sweep
    # is the "+1", so a solo sweep sees exactly 1 and keeps the full budget.
    cat > "$WORKDIR/status.json" <<'JSON'
{"in_flight_count": 0, "in_flight": [], "unregistered_locked_count": 0, "unregistered_locked": []}
JSON
    assert_eq "$(probe)" "1" \
        "an idle daemon + this sweep = 1 (a solo sweep still gets the full budget)"

    # Two siblings already running, and this sweep is NOT yet in the snapshot
    # (the daemon inserts a registry entry only after Command::spawn returns).
    cat > "$WORKDIR/status.json" <<'JSON'
{"in_flight_count": 2,
 "in_flight": [
   {"kind": {"type": "Issue", "value": 101}},
   {"kind": {"type": "Issue", "value": 102}}
 ],
 "unregistered_locked_count": 0, "unregistered_locked": []}
JSON
    assert_eq "$(LOOM_SWEEP_CLAIM_OWNED=103 probe)" "3" \
        "2 siblings + an unlisted self = 3 (the spawn-time insertion race)"

    # Same host state, but the snapshot already lists this sweep. The count
    # must be identical — self is subtracted out, then added back once.
    cat > "$WORKDIR/status.json" <<'JSON'
{"in_flight_count": 3,
 "in_flight": [
   {"kind": {"type": "Issue", "value": 101}},
   {"kind": {"type": "Issue", "value": 102}},
   {"kind": {"type": "Issue", "value": 103}}
 ],
 "unregistered_locked_count": 0, "unregistered_locked": []}
JSON
    assert_eq "$(LOOM_SWEEP_CLAIM_OWNED=103 probe)" "3" \
        "2 siblings + a self already listed = 3 (self is never double-counted)"

    # A sweep with no claimed issue (a `--prs` Mode C sweep, or a hand-run
    # spawn-claude.sh) matches nothing, so it is counted as the "+1".
    assert_eq "$(probe)" "4" \
        "a sweep with no LOOM_SWEEP_CLAIM_OWNED counts itself on top of all 3"

    # PrSet sweeps in the snapshot are real host load and must be counted.
    cat > "$WORKDIR/status.json" <<'JSON'
{"in_flight_count": 2,
 "in_flight": [
   {"kind": {"type": "Issue", "value": 101}},
   {"kind": {"type": "PrSet", "value": [7, 8]}}
 ],
 "unregistered_locked_count": 0, "unregistered_locked": []}
JSON
    assert_eq "$(LOOM_SWEEP_CLAIM_OWNED=103 probe)" "3" \
        "a PrSet sweep in the snapshot counts toward the host's load"

    # #4214 unregistered-but-locked sweeps are documented as demonstrably
    # alive, so they burn CPU exactly like a registered sweep does.
    cat > "$WORKDIR/status.json" <<'JSON'
{"in_flight_count": 1,
 "in_flight": [{"kind": {"type": "Issue", "value": 101}}],
 "unregistered_locked_count": 1,
 "unregistered_locked": [{"root": "/repo", "issue": 202, "owner_pid": 4242}]}
JSON
    assert_eq "$(LOOM_SWEEP_CLAIM_OWNED=103 probe)" "3" \
        "an unregistered-but-locked sweep (#4214) counts toward the host's load"
    assert_eq "$(LOOM_SWEEP_CLAIM_OWNED=202 probe)" "2" \
        "a self that appears only in unregistered_locked is not double-counted"

    # The #4069 unreachable-daemon error object has no in_flight key at all.
    cat > "$WORKDIR/status.json" <<'JSON'
{"error": "daemon unreachable", "socket": "/tmp/loom.sock", "install_state": {"state": "ExpectedButDead"}}
JSON
    assert_eq "$(probe)" "1" \
        "the unreachable-daemon error payload answers the fail-safe 1"

    # Garbage on stdout must not propagate a garbage divisor.
    echo 'not json at all' > "$WORKDIR/status.json"
    assert_eq "$(probe)" "1" \
        "unparseable daemon output answers the fail-safe 1"

    : > "$WORKDIR/status.json"
    assert_eq "$(probe)" "1" \
        "empty daemon output answers the fail-safe 1"

    # An explicit override still wins over a live, answering daemon.
    cat > "$WORKDIR/status.json" <<'JSON'
{"in_flight_count": 5,
 "in_flight": [
   {"kind": {"type": "Issue", "value": 1}}, {"kind": {"type": "Issue", "value": 2}},
   {"kind": {"type": "Issue", "value": 3}}, {"kind": {"type": "Issue", "value": 4}},
   {"kind": {"type": "Issue", "value": 5}}
 ],
 "unregistered_locked_count": 0, "unregistered_locked": []}
JSON
    assert_eq "$(LOOM_SWEEP_INFLIGHT_SWEEPS=2 probe)" "2" \
        "LOOM_SWEEP_INFLIGHT_SWEEPS overrides an answering daemon"

    # A wedged daemon must cost the probe budget, not the sweep. The probe is
    # bounded by LOOM_SWEEP_INFLIGHT_PROBE_TIMEOUT_SECS and then fails safe.
    WEDGED="$WORKDIR/loom-daemon-wedged"
    cat > "$WEDGED" <<'STUB'
#!/usr/bin/env bash
while true; do sleep 1; done
STUB
    chmod +x "$WEDGED"
    start=$(date +%s)
    out="$(LOOM_DAEMON_BIN="$WEDGED" LOOM_SWEEP_INFLIGHT_PROBE_TIMEOUT_SECS=2 \
        loom_cpu_inflight_sweeps "$WORKDIR" 2>/dev/null)"
    elapsed=$(( $(date +%s) - start ))
    assert_eq "$out" "1" \
        "a wedged daemon is bounded and answers the fail-safe 1"
    if ((elapsed <= 8)); then
        pass "the wedged probe returned within its budget (${elapsed}s)"
    else
        fail "the wedged probe returned within its budget (took ${elapsed}s)"
    fi

    # End-to-end: the divisor the probe produces, fed to the budget math, is
    # the number the #5979 incident needed.
    cat > "$WORKDIR/status.json" <<'JSON'
{"in_flight_count": 2,
 "in_flight": [
   {"kind": {"type": "Issue", "value": 83}},
   {"kind": {"type": "Issue", "value": 85}}
 ],
 "unregistered_locked_count": 0, "unregistered_locked": []}
JSON
    n="$(LOOM_SWEEP_CLAIM_OWNED=84 probe)"
    assert_eq "$(loom_cpu_budget_cores 8 2 "$n")" "2" \
        "the incident's 3-sweep/8-core shape now yields 2 cores each, not 6"
fi

echo ""
echo "════════════════════════════════════════════"
echo "  Total: $TESTS_RUN  Passed: $TESTS_PASSED  Failed: $TESTS_FAILED"
echo "════════════════════════════════════════════"
[[ "$TESTS_FAILED" -eq 0 ]]
