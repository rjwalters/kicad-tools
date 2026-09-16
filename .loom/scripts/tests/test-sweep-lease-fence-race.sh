#!/usr/bin/env bash
# test-sweep-lease-fence-race.sh - Regression suite: does the Phase 3
# sweep-side lease fencing check (sweep-lease-fence.sh, #6309) actually
# bound a two-host ACQUISITION race to one wasted build (Issue #6310, Epic
# #6165 Phase 3).
#
# ## Why this is a separate suite from test-sweep-lease-fence.sh
#
# test-sweep-lease-fence.sh already unit-tests each fencing outcome
# (fresh/owned -> PASS, expired -> ABORT EXPIRED, superseded -> ABORT
# SUPERSEDED, every fail-open case) against hand-picked single-comment
# fixtures, run through ONE invocation of the script. That is necessary but
# not sufficient evidence for the epic's own acceptance criterion ("no
# sweep opens a PR while its lease is expired or held elsewhere") -- it
# proves the CHECK's exit code is correct, not that the exit code actually
# gates the externally-visible side effect (a pushed branch / opened PR)
# for BOTH sides of a genuine two-writer race at once.
#
# This suite is explicitly NOT re-testing Phase 2 (the daemon's
# reclamation-side lease-freshness guard, `claim_reconciliation.rs`'s
# `lease_is_fresh` / `fetch_freshest_lease_updated_at`) -- that is already
# covered end-to-end by
# `loom-daemon/src/sweep_registry/dispatch.rs`'s
# `combined_dispatch_and_reclaim_race_produces_no_duplicate_build_with_safehouse_down`
# test (Issue #6288). Where #6288 composes Phase 2's dispatch-time
# claim-then-verify-order tie-break (#6287) with Phase 2's reclamation
# lease-freshness guard (#6286), this suite composes Phase 3's fencing
# check with a MOCK push/PR-open step, against a race scenario #6287's
# dispatch-time tie-break does not fully cover: two hosts that both got as
# far as *starting a build* (so #6287 already let both dispatches through,
# or one host's claim was later reclaimed and re-dispatched to a second
# host while the first host's build was still in flight) and are now both
# racing to reach the one irreversible step -- push / PR-open.
#
# ## Harness shape
#
# Mirrors test-sweep-lease-fence.sh's `gh` stub (real `jq` applies the
# script's own `--jq` filter against a canned comments fixture) but adds a
# `simulate_sweep_attempt` helper that runs the REAL sweep-lease-fence.sh
# `check` subprocess for a given host and, ONLY if it exits 0, appends that
# host's identity to a shared `pushed.log` -- modeling "this sweep proceeded
# to push its branch and open its PR". A host whose check aborts (exit 3 or
# 4) must produce ZERO lines in pushed.log; this is the negative-space
# assertion the issue's acceptance criteria call for ("not zero, not two").
#
# Covers:
#   (1) Acquisition race resolved by host identity: two hosts each hold a
#       fresh lease comment for the same issue, one strictly freshest by
#       forge `updated_at` ordering -> the freshest host's push proceeds,
#       the other aborts SUPERSEDED before any push/PR-open.
#   (2) Acquisition race resolved by elapsed time: the losing host's own
#       lease record goes stale (no renewal reached it before its build
#       got to the push step) -> it aborts EXPIRED before any push/PR-open,
#       and only afterward does a second host's later dispatch (re-claiming
#       the abandoned issue, modeled purely as the forge state left behind
#       by such a re-claim -- Phase 2's own reclaim mechanism is NOT
#       exercised here, see #6288 above) write a fresh lease and succeed.
#   (3) Winner-unaffected: in both (1) and (2), the winning host's own
#       fencing check is a genuine PASS (fresh lease, host match) rather
#       than an accidental fail-open "no evidence" PASS -- and it is not
#       disturbed by having run alongside (or after) the losing host's
#       aborted attempt against the SAME shared comment store.
#   (4) Combined negative-space assertion: across the full two-attempt
#       sequence of each race, pushed.log contains EXACTLY one entry --
#       never zero (the winner must still succeed) and never two (the
#       fencing check must actually block the loser).
#   (5) Issue #6485's own race shape: two lease comments within the same
#       second, the LATER one's host posts its own `loom:lease-yield`
#       standdown record and then keeps its lease looking fresh (as if a
#       misdirected renewal loop kept PATCHing it after the standdown) --
#       the EARLIER (tie-break-winning) host's fencing check must still
#       PASS even though its own lease was never renewed at all.
#   (6) Edge case guarding (5): the SAME fixture shape with the yield
#       record removed -- a genuinely fresher peer lease that never
#       yielded must still correctly SUPERSEDE the earlier host. This is
#       the negative check that (5)'s fix has not weakened ordinary
#       fencing for a lease that simply never yielded.
#   (7) Issue #6783's own incident shape (issue #6694 / PR #6773): the
#       ONLY lease record on the issue was written by a host that has since
#       died -- never renewed again, never yielded -- and is now well past
#       the TTL, with zero live competitors. A successor sweep on a
#       DIFFERENT host, dispatched later with no lease of its own yet, must
#       PASS its fencing check (not be permanently fenced out by the
#       abandoned record) and go on to push/PR-open.
#
# Usage:
#   ./.loom/scripts/tests/test-sweep-lease-fence-race.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SCRIPT="$SCRIPTS_DIR/sweep-lease-fence.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $SCRIPT not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH (identical contract to test-sweep-lease-fence.sh) -----
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "api" ]]; then
  shift
  path=""
  filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --jq) filter="$2"; shift 2 ;;
      -R) shift 2 ;;
      --paginate) shift ;;
      *)
        if [[ -z "$path" ]]; then path="$1"; fi
        shift
        ;;
    esac
  done
  if [[ "$path" == repos/*/issues/*/comments ]]; then
    if [[ -f "$D/comments-fail" ]]; then
      echo "stub gh: comments fetch failed" >&2
      exit 1
    fi
    canned="$D/comments.json"
    if [[ ! -f "$canned" ]]; then canned="$D/empty.json"; echo "[]" > "$canned"; fi
    jq -c "$filter" "$canned"
    exit 0
  fi
  echo "stub gh: unhandled api args: path=$path" >&2
  exit 3
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

PUSHED_LOG="$STUB_DIR/pushed.log"

reset_state() {
    rm -f "$STUB_DIR"/comments.json "$STUB_DIR"/comments-fail "$PUSHED_LOG" "$STUB_DIR"/stderr-*.log
    # Deliberately NOT unsetting LOOM_LEASE_FENCE_NOW here (unlike
    # test-sweep-lease-fence.sh's per-scenario reset): this suite fixes ONE
    # "now" for the whole race timeline across all scenarios/attempts, so
    # every attempt's age math stays comparable against the same clock.
    unset LOOM_HOST_ID LOOM_LEASE_TTL_MINUTES HOSTNAME 2> /dev/null || true
}

# simulate_sweep_attempt <issue> <host> <label-for-stderr-log> [extra check args...]
#
# Runs the REAL sweep-lease-fence.sh `check` subprocess for <host> against
# whatever is currently in $STUB_DIR/comments.json. On exit 0, appends
# <host> to $PUSHED_LOG -- the mocked "pushed branch / opened PR" side
# effect -- and NOTHING otherwise. Sets ATTEMPT_RC and ATTEMPT_ERR (stderr)
# for the caller to assert on.
simulate_sweep_attempt() {
    local issue="$1" host="$2" label="$3"
    shift 3
    ATTEMPT_ERR="$("$SCRIPT" check "$issue" --host "$host" "$@" 2>&1 1>/dev/null)"
    ATTEMPT_RC=$?
    echo "$ATTEMPT_ERR" > "$STUB_DIR/stderr-$label.log"
    if [[ "$ATTEMPT_RC" -eq 0 ]]; then
        echo "$host" >> "$PUSHED_LOG"
    fi
}

pushed_log_contents() {
    [[ -f "$PUSHED_LOG" ]] && cat "$PUSHED_LOG" || true
}

pushed_log_count() {
    [[ -f "$PUSHED_LOG" ]] && wc -l < "$PUSHED_LOG" | tr -d '[:space:]' || echo 0
}

# A fixed "now" so age math is deterministic without depending on wall clock.
NOW_EPOCH="$(date -u -d "2026-08-15T16:00:00Z" +%s 2>/dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "2026-08-15T16:00:00Z" +%s)"
export LOOM_LEASE_FENCE_NOW="$NOW_EPOCH"

echo "Testing sweep-lease-fence.sh under simulated two-host acquisition races..."

# ============================================================================
# Scenario 1: acquisition race resolved by host identity (SUPERSEDED loser)
# ============================================================================
#
# Two hosts (studio-host, worker-host) both hold a fresh lease comment for
# the SAME issue -- the exact shape of a near-simultaneous acquisition
# window where BOTH dispatches got far enough to write/renew a lease before
# either learned about the other. The forge's own `updated_at` ordering
# (simulated here via the fixture timestamps, matching real REST comment
# semantics) makes studio-host's write the strictly freshest record.
echo ""
echo "--- Scenario 1: SUPERSEDED loser in a genuine two-writer race ---"
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:57:00Z", "body": "<!-- loom:lease host=worker-host sweep=sweep-issue-7001-worker -->\nprose"},
  {"id": 2, "updated_at": "2026-08-15T15:59:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-7001-studio -->\nprose"}
]
JSON

# The loser runs its own push-time check first (it does not know it lost
# yet -- that is the whole point of the fence).
simulate_sweep_attempt 7001 worker-host loser
assert_eq "4" "$ATTEMPT_RC" "(1) losing host's fencing check aborts SUPERSEDED (exit 4) before any push"
assert_contains "$ATTEMPT_ERR" "ABORT: SUPERSEDED" "(1) loser's stderr names the SUPERSEDED abort reason"
assert_contains "$ATTEMPT_ERR" "studio-host" "(1) loser's stderr names the superseding host"

# The winner runs its own push-time check against the SAME shared comment
# store (unmodified by the loser's failed attempt -- fencing never writes).
simulate_sweep_attempt 7001 studio-host winner
assert_eq "0" "$ATTEMPT_RC" "(1) winning host's fencing check PASSes (exit 0) -- proceeds to push"
assert_contains "$ATTEMPT_ERR" "lease fence OK" "(1) winner's stderr confirms a genuine PASS, not a fail-open"
assert_contains "$ATTEMPT_ERR" "host=studio-host" "(1) winner's stderr confirms the match was against its OWN fresh lease"

assert_eq "1" "$(pushed_log_count)" "(1) exactly ONE push/PR-open across the whole race (not zero, not two)"
assert_eq "studio-host" "$(pushed_log_contents)" "(1) the surviving push/PR-open belongs to the winner, not the loser"

# ============================================================================
# Scenario 2: acquisition race resolved by elapsed time (EXPIRED loser)
# ============================================================================
#
# The losing host (worker-host) acquired the issue, but nothing renewed its
# lease before its build reached the push step -- the freshest record on
# the issue is worker-host's OWN comment, now older than the TTL. Only
# AFTER worker-host's fencing check has already aborted does a second host
# (studio-host) write a fresh lease of its own -- modeling the forge state
# left behind by a later dispatch re-claiming the abandoned issue (Phase 2's
# reclaim MECHANISM is out of scope for this suite; only its resulting forge
# state is modeled, per #6288 above).
echo ""
echo "--- Scenario 2: EXPIRED loser, winner re-claims afterward ---"
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:38:00Z", "body": "<!-- loom:lease host=worker-host sweep=sweep-issue-7002-worker -->\nprose"}
]
JSON

simulate_sweep_attempt 7002 worker-host loser
assert_eq "3" "$ATTEMPT_RC" "(2) losing host's fencing check aborts EXPIRED (exit 3) before any push"
assert_contains "$ATTEMPT_ERR" "ABORT: EXPIRED" "(2) loser's stderr names the EXPIRED abort reason"

assert_eq "0" "$(pushed_log_count)" "(2) no push/PR-open has happened yet -- the loser produced zero side effects"

# The winner's re-claim lands on the forge only now (after the loser's
# aborted attempt above), exactly matching the issue's own framing: the
# loser fences itself out BEFORE producing a side effect, not after.
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:38:00Z", "body": "<!-- loom:lease host=worker-host sweep=sweep-issue-7002-worker -->\nprose"},
  {"id": 2, "updated_at": "2026-08-15T15:59:30Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-7002-studio -->\nprose"}
]
JSON

simulate_sweep_attempt 7002 studio-host winner
assert_eq "0" "$ATTEMPT_RC" "(2) the re-claiming host's fencing check PASSes (exit 0) -- proceeds to push"
assert_contains "$ATTEMPT_ERR" "lease fence OK" "(2) winner's stderr confirms a genuine PASS"

assert_eq "1" "$(pushed_log_count)" "(2) exactly ONE push/PR-open across the whole race (not zero, not two)"
assert_eq "studio-host" "$(pushed_log_contents)" "(2) the surviving push/PR-open belongs to the re-claiming host, not the abandoned one"

# ============================================================================
# Scenario 3: winner is unaffected by a concurrent/prior losing attempt
# ============================================================================
#
# Re-run the winner's check from Scenario 1 a second time, AFTER the
# loser's own aborted attempt has already run against the identical shared
# store, to rule out any state leakage between the two hosts' attempts
# (fencing is read-only -- it must never mutate the comment store, so
# running the loser first must not change the winner's outcome).
echo ""
echo "--- Scenario 3: winner's own fresh lease is never disturbed by the loser's aborted attempt ---"
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T15:57:00Z", "body": "<!-- loom:lease host=worker-host sweep=sweep-issue-7003-worker -->\nprose"},
  {"id": 2, "updated_at": "2026-08-15T15:59:00Z", "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-7003-studio -->\nprose"}
]
JSON
simulate_sweep_attempt 7003 worker-host loser-first
assert_eq "4" "$ATTEMPT_RC" "(3) loser aborts SUPERSEDED as before"
simulate_sweep_attempt 7003 studio-host winner-after
assert_eq "0" "$ATTEMPT_RC" "(3) winner still PASSes after the loser's attempt ran first against the same store"
assert_contains "$ATTEMPT_ERR" "lease fence OK" "(3) winner's PASS is genuine (fresh + owned), not a fail-open artifact of the loser's run"
assert_eq "1" "$(pushed_log_count)" "(3) still exactly one push/PR-open -- order of attempts does not change the outcome"

# ============================================================================
# Scenario 4: Issue #6485's own race shape -- a yielded-but-still-renewed
# loser must not fence out the tie-break winner
# ============================================================================
#
# Two lease comments posted within the same second: host-winner (id 10,
# EARLIER by forge comment order -- wins Issue #6287's claim-then-verify
# tie-break) and host-loser (id 11, one second later -- loses the tie-break
# and posts its own `loom:lease-yield` standdown record, id 12). Despite
# yielding, host-loser's lease comment (id 11) keeps looking freshly
# renewed -- modeling the exact #6485 incident where a renewal loop (either
# the yielded host's own, or another host's misdirected "newest wins" loop)
# kept PATCHing it well after the standdown. host-winner's OWN lease (id 10)
# is never renewed at all. Per the issue's own acceptance criteria, the
# winner's fencing check must still PASS.
echo ""
echo "--- Scenario 4: yielded-but-still-renewed loser must not fence out the tie-break winner (#6485) ---"
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 10, "updated_at": "2026-08-15T15:58:57Z", "body": "<!-- loom:lease host=host-winner sweep=sweep-issue-9001-winner -->\nprose"},
  {"id": 11, "updated_at": "2026-08-15T15:59:30Z", "body": "<!-- loom:lease host=host-loser sweep=sweep-issue-9001-loser -->\nprose"},
  {"id": 12, "updated_at": "2026-08-15T15:59:00Z", "body": "<!-- loom:lease-yield host=host-loser sweep=sweep-issue-9001-loser earliest_host=host-winner earliest_sweep=sweep-issue-9001-winner -->\nprose"}
]
JSON

simulate_sweep_attempt 9001 host-winner yielded-race-winner
assert_eq "0" "$ATTEMPT_RC" "(5) tie-break winner's fencing check PASSes even though its own lease (id 10) was never renewed"
assert_contains "$ATTEMPT_ERR" "lease fence OK" "(5) winner's stderr confirms a genuine PASS"
assert_contains "$ATTEMPT_ERR" "host=host-winner" "(5) winner's stderr confirms the match was against its OWN lease, not the yielded loser's fresher one"
assert_eq "1" "$(pushed_log_count)" "(5) the winner's push/PR-open proceeds"

# ============================================================================
# Scenario 5 (edge case guarding Scenario 4): the SAME shape MINUS the yield
# record -- a genuinely fresher, never-yielded peer lease must still
# correctly SUPERSEDE the earlier host. Proves the #6485 fix has not
# weakened ordinary fencing for a lease that simply never yielded.
# ============================================================================
echo ""
echo "--- Scenario 5: without a yield record, the fresher peer lease still correctly SUPERSEDES (edge case guard) ---"
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 10, "updated_at": "2026-08-15T15:58:57Z", "body": "<!-- loom:lease host=host-winner sweep=sweep-issue-9002-winner -->\nprose"},
  {"id": 11, "updated_at": "2026-08-15T15:59:30Z", "body": "<!-- loom:lease host=host-loser sweep=sweep-issue-9002-loser -->\nprose"}
]
JSON

simulate_sweep_attempt 9002 host-winner unyielded-race-loser
assert_eq "4" "$ATTEMPT_RC" "(6) without a yield record, the earlier host correctly loses to the genuinely fresher peer lease (ABORT SUPERSEDED)"
assert_contains "$ATTEMPT_ERR" "ABORT: SUPERSEDED" "(6) stderr names the SUPERSEDED reason"
assert_contains "$ATTEMPT_ERR" "host-loser" "(6) stderr names the superseding (never-yielded) host"
assert_eq "0" "$(pushed_log_count)" "(6) no push/PR-open happens for the correctly-superseded host"

# ============================================================================
# Scenario 7 (Issue #6783): a dead sweep's abandoned lease from a DIFFERENT
# host must not permanently fence out a successor sweep with zero live
# competitors -- the exact shape observed on issue #6694 / PR #6773.
# ============================================================================
echo ""
echo "--- Scenario 7: abandoned lease from a dead host does not permanently fence out a successor sweep (#6783) ---"
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "updated_at": "2026-08-15T04:13:27Z", "body": "<!-- loom:lease host=host-e1d4c843 sweep=sweep-issue-6694-1787458405 -->\nprose"}
]
JSON

simulate_sweep_attempt 6694 host-successor successor
assert_eq "0" "$ATTEMPT_RC" "(7) successor host's fencing check PASSes despite the sole lease record being expired and owned by a different, dead host"
assert_contains "$ATTEMPT_ERR" "PASS" "(7) stderr reports a PASS, not an ABORT"
assert_contains "$ATTEMPT_ERR" "host-e1d4c843" "(7) stderr names the abandoned lease's owning (dead) host"
assert_eq "1" "$(pushed_log_count)" "(7) the successor's push/PR-open proceeds -- no longer permanently fenced out"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if ((TESTS_FAILED > 0)); then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}ALL PASSED${NC}"
exit 0
