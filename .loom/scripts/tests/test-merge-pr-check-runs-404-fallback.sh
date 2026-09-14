#!/usr/bin/env bash
# test-merge-pr-check-runs-404-fallback.sh - Unit tests for the
# persistent-vs-transient check-runs-404 distinction in merge-pr.sh and its
# supporting helper in forge-helpers.sh (#6389).
#
# Bug: on a repo with NO GitHub Actions workflows (and `allow_auto_merge`
# disabled), `GET /repos/{nwo}/commits/{sha}/check-runs` returns a PERSISTENT
# HTTP 404 for every SHA (the Checks API itself is unavailable). Before this
# fix, `_wait_for_checks_then_sync_merge()` (the degraded `--auto` path) and
# the UNSTABLE fallback's matching fetch loop could not distinguish that
# persistent 404 from a transient fetch blip (network, 5xx) — both were
# treated as "still pending" and polled all the way to LOOM_AUTO_MERGE_TIMEOUT
# (600s) even though the PR was already CLEAN/MERGEABLE.
#
# The fix (#6389):
#   1. `forge_get_check_runs` (GitHub branch) now distinguishes a confirmed
#      HTTP 404 from any other failure by inspecting `gh api`'s stderr for
#      "HTTP 404" and returning the dedicated `$FORGE_CHECK_RUNS_RC_NOT_FOUND`
#      (44) exit code instead of the generic 1.
#   2. Both poll loops track a per-call "confirmed 404 this iteration"
#      signal (BOTH the initial attempt and its retry-once must return the
#      404 rc) across `LOOM_CHECK_RUNS_404_STREAK` (default 2) consecutive
#      iterations, spaced a full LOOM_AUTO_MERGE_POLL_INTERVAL apart. Once
#      that streak is reached, the loop short-circuits to the synchronous
#      merge with an info line instead of continuing to poll. Any other
#      failure shape (a single 404, a 5xx, a network blip, or a 404 mixed
#      with a non-404 failure) resets the streak and preserves today's
#      bounded-poll/retry behavior.
#
# This test exercises three surfaces:
#   1. `forge_get_check_runs` (GitHub) itself, with `gh` PATH-shimmed (the
#      pattern from `test-merge-pr-unstable-fallback.sh`) to return a
#      confirmed 404, a 5xx, a network-style failure, and success — asserting
#      the distinguished exit codes.
#   2. The streak-tracking decision policy, mirrored from
#      `_wait_for_checks_then_sync_merge()` / the UNSTABLE fallback's fetch
#      loop, covering: persistent 404 crossing the threshold; a transient
#      failure (5xx) that never crosses it; a mixed 404-then-non-404 pair
#      that does not count as "confirmed"; and a flaky 404-then-200 sequence
#      that must NOT be misclassified as persistent (the streak resets on the
#      intervening success).
#   3. Source-wiring assertions against merge-pr.sh and forge-helpers.sh so a
#      refactor that drops the distinction fails this test.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-check-runs-404-fallback.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
FORGE_HELPERS_SRC="$HELPERS_DIR/lib/forge-helpers.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
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

# --- Source helpers (defines FORGE_CHECK_RUNS_RC_NOT_FOUND + forge_get_check_runs) ---
source "$FORGE_HELPERS_SRC"

# shellcheck disable=SC2034  # consumed by sourced helpers via FORGE_TYPE global
FORGE_TYPE="github"

# =============================================================================
# Part 1: forge_get_check_runs (GitHub) — distinguished exit codes
# =============================================================================
echo "Testing forge_get_check_runs GitHub-branch exit codes (#6389)..."

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT

# Stub gh that recognizes `gh api repos/<nwo>/commits/<sha>/check-runs ...`
# and returns a canned outcome keyed by the SHA, via
# $STUB_DIR/gh-check-runs-<sha>.mode in {success, 404, 500, network}.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh used by test-merge-pr-check-runs-404-fallback.sh.
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:-}"
if [[ -z "$STUB_DIR_FROM_ENV" ]]; then
  echo "stub gh: LOOM_TEST_STUB_DIR not set" >&2
  exit 2
fi

if [[ "${1:-}" == "api" ]]; then
  path="${2:-}"
  if [[ "$path" =~ ^repos/.+/commits/([^/]+)/check-runs$ ]]; then
    sha="${BASH_REMATCH[1]}"
    mode_file="$STUB_DIR_FROM_ENV/gh-check-runs-$sha.mode"
    mode="success"
    [[ -f "$mode_file" ]] && mode="$(cat "$mode_file")"
    case "$mode" in
      success)
        echo '{"total_count":0,"check_runs":[]}'
        exit 0
        ;;
      404)
        # Real gh's error text for a non-2xx REST response: "gh: <message> (HTTP <code>)".
        echo "gh: Not Found (HTTP 404)" >&2
        exit 1
        ;;
      500)
        echo "gh: Internal Server Error (HTTP 500)" >&2
        exit 1
        ;;
      network)
        echo "gh: connect: connection refused" >&2
        exit 1
        ;;
      *)
        echo "stub gh: unknown mode '$mode'" >&2
        exit 2
        ;;
    esac
  fi
fi

echo "stub gh: unrecognized invocation: $*" >&2
exit 2
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
_ORIG_PATH="$PATH"
export PATH="$STUB_DIR:$PATH"

echo "success" > "$STUB_DIR/gh-check-runs-sha-success.mode"
_out=""
_rc=0
_out="$(forge_get_check_runs "owner/repo" "sha-success")" || _rc=$?
assert_eq "0" "$_rc" "success: forge_get_check_runs returns 0"
assert_eq '{"total_count":0,"check_runs":[]}' "$_out" "success: forge_get_check_runs emits the JSON payload on stdout"

echo "404" > "$STUB_DIR/gh-check-runs-sha-404.mode"
_rc=0
forge_get_check_runs "owner/repo" "sha-404" >/dev/null 2>/dev/null || _rc=$?
assert_eq "$FORGE_CHECK_RUNS_RC_NOT_FOUND" "$_rc" "404: forge_get_check_runs returns the dedicated not-found rc (44)"

echo "500" > "$STUB_DIR/gh-check-runs-sha-500.mode"
_rc=0
forge_get_check_runs "owner/repo" "sha-500" >/dev/null 2>/dev/null || _rc=$?
assert_eq "1" "$_rc" "500: forge_get_check_runs returns the generic transient-failure rc (1), NOT the 404 rc"

echo "network" > "$STUB_DIR/gh-check-runs-sha-net.mode"
_rc=0
forge_get_check_runs "owner/repo" "sha-net" >/dev/null 2>/dev/null || _rc=$?
assert_eq "1" "$_rc" "network error: forge_get_check_runs returns the generic transient-failure rc (1)"

# Restore PATH so subsequent (non-stubbed) sections behave normally.
export PATH="$_ORIG_PATH"

# =============================================================================
# Part 2: streak-tracking decision policy
# =============================================================================
# Mirrors the per-iteration classification shared by
# `_wait_for_checks_then_sync_merge()`'s `not_found_streak` and the UNSTABLE
# fallback's `_UNSTABLE_NOT_FOUND_STREAK`. Given this iteration's two attempt
# return codes (mirroring the existing retry-once absorption) and the
# running streak, returns "<new streak> <verdict>" where verdict is one of
# success | still-pending | proceed-to-merge.
echo ""
echo "Testing the persistent-404 streak decision policy (#6389)..."

_iteration_verdict() {
    local attempt1_rc="$1" attempt2_rc="$2" streak_in="$3" threshold="$4"
    local fetch_rc="$attempt1_rc"
    [[ "$attempt1_rc" -ne 0 ]] && fetch_rc="$attempt2_rc"

    if [[ "$fetch_rc" -eq 0 ]]; then
        echo "0 success"
        return
    fi

    local streak_out
    if [[ "$attempt1_rc" -eq "$FORGE_CHECK_RUNS_RC_NOT_FOUND" && "$attempt2_rc" -eq "$FORGE_CHECK_RUNS_RC_NOT_FOUND" ]]; then
        streak_out=$(( streak_in + 1 ))
    else
        streak_out=0
    fi

    if [[ "$streak_out" -ge "$threshold" ]]; then
        echo "$streak_out proceed-to-merge"
    else
        echo "$streak_out still-pending"
    fi
}

NF="$FORGE_CHECK_RUNS_RC_NOT_FOUND"
THRESHOLD=2

# Case A: immediate success (first attempt) -> success, no classification needed.
result=$(_iteration_verdict 0 0 0 "$THRESHOLD")
assert_eq "0 success" "$result" "Immediate success (attempt1=0) -> success"

# Case B: 404 on the first attempt, retry succeeds -> the existing retry-once
# blip absorption swallows it entirely; success, streak untouched.
result=$(_iteration_verdict "$NF" 0 0 "$THRESHOLD")
assert_eq "0 success" "$result" "404 then successful retry -> blip absorbed, success"

# Case C: persistent 404 — BOTH attempts 404, for THRESHOLD consecutive
# iterations -> the (threshold-1)th iteration keeps polling; the thresholdth
# iteration crosses over to proceed-to-merge.
streak=0
result=$(_iteration_verdict "$NF" "$NF" "$streak" "$THRESHOLD")
assert_eq "1 still-pending" "$result" "Persistent 404, iteration 1/2 -> still-pending (below threshold)"
streak=1
result=$(_iteration_verdict "$NF" "$NF" "$streak" "$THRESHOLD")
assert_eq "2 proceed-to-merge" "$result" "Persistent 404, iteration 2/2 -> proceed-to-merge (threshold reached)"

# Case D: transient failure — a 5xx on BOTH attempts (rc=1, not the 404 rc) —
# must never cross the persistent-404 threshold, no matter how many
# iterations it repeats. Today's bounded-poll/retry behavior is unchanged
# (it keeps polling until LOOM_AUTO_MERGE_TIMEOUT, outside this policy's
# scope).
streak=0
for i in 1 2 3; do
    result=$(_iteration_verdict 1 1 "$streak" "$THRESHOLD")
    assert_eq "0 still-pending" "$result" "Transient 5xx failure, repeat #$i -> streak stays 0, never proceeds"
    streak="${result%% *}"
done

# Case E: mixed failure shape — attempt1 is a confirmed 404 but the retry is
# a DIFFERENT failure (e.g. a 5xx). Only a 404 on BOTH attempts counts as
# "confirmed" for this iteration, so this must NOT increment the streak.
streak=1
result=$(_iteration_verdict "$NF" 1 "$streak" "$THRESHOLD")
assert_eq "0 still-pending" "$result" "Mixed 404-then-5xx -> NOT confirmed, streak resets to 0"

# Case F: flaky 404-then-200 sequence across iterations must NOT be
# misclassified as persistent. Iteration 1: confirmed 404 (streak -> 1,
# below threshold). Iteration 2: check-runs recovers (success) -> streak
# resets to 0. Iteration 3: confirmed 404 again -> streak restarts at 1, NOT
# 3 — it must take another full THRESHOLD-length run to cross over.
streak=0
result=$(_iteration_verdict "$NF" "$NF" "$streak" "$THRESHOLD"); streak="${result%% *}"
assert_eq "1 still-pending" "$result" "Flaky sequence iter1: confirmed 404 -> streak=1"
result=$(_iteration_verdict 0 0 "$streak" "$THRESHOLD"); streak="${result%% *}"
assert_eq "0 success" "$result" "Flaky sequence iter2: recovers -> streak resets to 0"
result=$(_iteration_verdict "$NF" "$NF" "$streak" "$THRESHOLD"); streak="${result%% *}"
assert_eq "1 still-pending" "$result" "Flaky sequence iter3: confirmed 404 again -> streak restarts at 1 (NOT misclassified as persistent)"

# =============================================================================
# Part 3: LOOM_CHECK_RUNS_404_STREAK default wiring
# =============================================================================
echo ""
echo "Testing LOOM_CHECK_RUNS_404_STREAK default wiring (#6389)..."

unset LOOM_CHECK_RUNS_404_STREAK 2>/dev/null || true
assert_eq "2" "${LOOM_CHECK_RUNS_404_STREAK:-2}" "LOOM_CHECK_RUNS_404_STREAK defaults to 2 when unset"
LOOM_CHECK_RUNS_404_STREAK=5
assert_eq "5" "${LOOM_CHECK_RUNS_404_STREAK:-2}" "LOOM_CHECK_RUNS_404_STREAK honors a caller override"
unset LOOM_CHECK_RUNS_404_STREAK 2>/dev/null || true

if grep -q 'LOOM_CHECK_RUNS_404_STREAK="\${LOOM_CHECK_RUNS_404_STREAK:-2}"' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh wires the LOOM_CHECK_RUNS_404_STREAK default (2)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh missing the LOOM_CHECK_RUNS_404_STREAK default wiring"
fi

# =============================================================================
# Part 4: source-wiring assertions
# =============================================================================
echo ""
echo "Testing merge-pr.sh / forge-helpers.sh source wiring (#6389)..."

if grep -q 'FORGE_CHECK_RUNS_RC_NOT_FOUND=44' "$FORGE_HELPERS_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge-helpers.sh defines FORGE_CHECK_RUNS_RC_NOT_FOUND"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge-helpers.sh missing FORGE_CHECK_RUNS_RC_NOT_FOUND"
fi

if grep -q 'grep -q "HTTP 404" "\$err_file"' "$FORGE_HELPERS_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_get_check_runs inspects gh's stderr for 'HTTP 404'"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_get_check_runs missing the HTTP-404 stderr check"
fi

# _wait_for_checks_then_sync_merge() must track not_found_streak and short-circuit
# to the synchronous merge with the documented info line.
_wfctsm_block="$(awk '/^_wait_for_checks_then_sync_merge\(\)/{f=1} f; /^\}/{if (f) exit}' "$MERGE_PR_SRC")"
if echo "$_wfctsm_block" | grep -q 'not_found_streak'; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: _wait_for_checks_then_sync_merge tracks not_found_streak"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: _wait_for_checks_then_sync_merge missing not_found_streak tracking"
fi

if echo "$_wfctsm_block" | grep -q 'check-runs API unavailable for this repo (no checks configured); proceeding to synchronous merge'; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: _wait_for_checks_then_sync_merge logs the documented persistent-404 info line"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: _wait_for_checks_then_sync_merge missing the persistent-404 info line"
fi

if echo "$_wfctsm_block" | grep -q '"\$attempt1_rc" -eq "\$FORGE_CHECK_RUNS_RC_NOT_FOUND" && "\$attempt2_rc" -eq "\$FORGE_CHECK_RUNS_RC_NOT_FOUND"'; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: _wait_for_checks_then_sync_merge requires BOTH attempts to confirm a 404"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: _wait_for_checks_then_sync_merge missing the both-attempts-404 confirmation"
fi

# The UNSTABLE fallback's fetch loop must carry the identical discipline.
if grep -q '_UNSTABLE_NOT_FOUND_STREAK' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: UNSTABLE fallback tracks _UNSTABLE_NOT_FOUND_STREAK"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: UNSTABLE fallback missing _UNSTABLE_NOT_FOUND_STREAK tracking"
fi

if grep -q '_UNSTABLE_ATTEMPT1_RC" -eq "\$FORGE_CHECK_RUNS_RC_NOT_FOUND" && "\$_UNSTABLE_ATTEMPT2_RC" -eq "\$FORGE_CHECK_RUNS_RC_NOT_FOUND"' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: UNSTABLE fallback requires BOTH attempts to confirm a 404"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: UNSTABLE fallback missing the both-attempts-404 confirmation"
fi

if grep -q '_UNSTABLE_FALLBACK_TO_MERGE=true' "$MERGE_PR_SRC" && \
   grep -c '_UNSTABLE_FALLBACK_TO_MERGE=true' "$MERGE_PR_SRC" | grep -Eq '^[2-9]$|^[0-9][0-9]$'; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: UNSTABLE fallback's persistent-404 branch reuses _UNSTABLE_FALLBACK_TO_MERGE (>=2 sites: informational-only + persistent-404)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: expected _UNSTABLE_FALLBACK_TO_MERGE=true at >=2 sites (informational-only + persistent-404)"
fi

# Both loops must reset their attempt-rc trackers to 0 at the TOP of every
# iteration — otherwise a stale confirmed-404 from a prior, unrelated
# iteration could leak into the next classification. This is the concrete
# form of the "edge case must not crash/misclassify the distinction logic"
# requirement for this design (no external base-branch probe is used here;
# the per-iteration reset is what keeps a one-off glitch from persisting).
if grep -q 'local attempt1_rc=0 attempt2_rc=0 fetch_rc runs_raw' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: _wait_for_checks_then_sync_merge resets attempt1_rc/attempt2_rc every iteration"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: _wait_for_checks_then_sync_merge missing the per-iteration attempt-rc reset"
fi

if grep -q '_UNSTABLE_ATTEMPT1_RC=0' "$MERGE_PR_SRC" && grep -q '_UNSTABLE_ATTEMPT2_RC=0' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: UNSTABLE fallback resets _UNSTABLE_ATTEMPT1_RC/_UNSTABLE_ATTEMPT2_RC every iteration"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: UNSTABLE fallback missing the per-iteration attempt-rc reset"
fi

# The streak vars must be unset at the end of the UNSTABLE block alongside the
# other per-attempt scratch vars, so a subsequent MERGE_ATTEMPT retry starts
# clean.
if grep -q '_UNSTABLE_ATTEMPT1_RC _UNSTABLE_ATTEMPT2_RC _UNSTABLE_NOT_FOUND_STREAK' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: UNSTABLE fallback unsets the new streak-tracking vars at block exit"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: UNSTABLE fallback missing cleanup of the new streak-tracking vars"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
