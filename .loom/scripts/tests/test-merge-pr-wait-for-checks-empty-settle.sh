#!/usr/bin/env bash
# test-merge-pr-wait-for-checks-empty-settle.sh - Unit tests for the
# empty-output false-settle guard in merge-pr.sh's
# _wait_for_checks_then_sync_merge() (#6169).
#
# Bug (#6169): `gh pr checks` (and the check-runs REST endpoint this function
# actually polls via forge_get_check_runs) can return a completely empty
# rollup (zero rows / total_count:0) during a transient forge failure (e.g.
# an intermittent TLS handshake error) -- indistinguishable, on its own, from
# "this repo genuinely has no CI checks configured for this commit". Before
# this fix, _wait_for_checks_then_sync_merge trusted a zero-row read on its
# VERY FIRST poll as "nothing failing, nothing pending -> CLEAN" and returned
# 0 immediately, letting merge-pr.sh proceed straight to a synchronous merge
# without ever having observed real check-run data. Reported live: a Judge
# poller on kicad-tools PR #4792 (2026-08-13) declared CI "settled" 6 minutes
# into a ~40-minute board-test run this exact way.
#
# Fix: track whether a nonzero total_count has EVER been observed
# (observed_checks). A zero-row read is only trusted once observed_checks is
# true (real data has been seen at least once) OR the bounded
# LOOM_AUTO_MERGE_TIMEOUT wait has fully elapsed (at which point continuing
# to wait cannot help either) -- matching the "at least one confirming read"
# discipline the sibling UNSTABLE branch (_UNSTABLE_OBSERVED_PENDING) already
# used.
#
# Strategy (mirrors test-merge-pr-merge-ordering-guard.sh): extract
# _wait_for_checks_then_sync_merge from merge-pr.sh and source it, stub every
# forge_* helper it calls plus `sleep`/`date` (both stubbed so the test runs
# deterministically and instantly -- no real wall-clock waiting), then assert
# on the function's return value, how many times it polled forge_get_check_runs,
# and the info/warning narration it emits.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-wait-for-checks-empty-settle.sh

# SC2034: several globals (PR_JSON, PR_NUMBER, REPO_NWO, GH,
# LOOM_AUTO_MERGE_TIMEOUT, LOOM_AUTO_MERGE_POLL_INTERVAL) are read only by the
# function extracted+sourced from merge-pr.sh, which shellcheck cannot see.
# shellcheck disable=SC2034

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

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
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Minimal logging shims the extracted function calls ---
# `error` must exit non-zero to faithfully model the real script's hard
# block; every scenario below runs the function inside a subshell (via
# run_wait) so this exit only tears down that subshell, not the test.
INFO_LOG=""
WARN_LOG=""
info()    { INFO_LOG+="$*"$'\n'; }
warning() { WARN_LOG+="$*"$'\n'; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- Extract the function under test from merge-pr.sh and source it ---
FUNCS_FILE="$(mktemp)"
STATE_DIR="$(mktemp -d)"
trap 'rm -f "$FUNCS_FILE" 2>/dev/null || true; rm -rf "$STATE_DIR" 2>/dev/null || true' EXIT
awk '
  /^_wait_for_checks_then_sync_merge\(\) \{/ { capture=1 }
  /^# Handle auto-merge mode/                { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_wait_for_checks_then_sync_merge()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _wait_for_checks_then_sync_merge from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub `sleep`, `date`, and `forge_get_check_runs` so every scenario runs
# instantly and deterministically -- no real wall-clock waiting, no flakiness
# from system load affecting how many loop iterations fit in N real seconds.
#
# The function under test invokes both `date +%s` and `forge_get_check_runs`
# through `$(...)` command substitutions, which fork a SUBSHELL each time --
# a plain shell-variable counter (tried first) resets to 0 in every subshell
# and never persists across calls, producing an infinite loop (the deadline
# compared against "1" forever). State files survive subshell forks, so both
# counters and the canned-response queue live on disk instead.
DATE_COUNTER_FILE="$STATE_DIR/date-counter"
FGCR_CALLS_FILE="$STATE_DIR/fgcr-calls"
FGCR_RESPONSES_FILE="$STATE_DIR/fgcr-responses"   # one canned JSON response per line

sleep() { :; }
date() {
    if [[ "${1:-}" == "+%s" ]]; then
        local n
        n=$(($(cat "$DATE_COUNTER_FILE") + 1))
        echo "$n" > "$DATE_COUNTER_FILE"
        echo "$n"
        return 0
    fi
    command date "$@"
}

# --- Stub forge_get_pr_nocache: never "merged concurrently" in these tests ---
forge_get_pr_nocache() { echo '{"merged": false}'; }

# --- Stub forge_get_required_status_check_contexts: unused by the
# scenarios below (none exercise the failing-check branch) ---
forge_get_required_status_check_contexts() { echo ""; }

forge_get_check_runs() {
    local calls
    calls=$(($(cat "$FGCR_CALLS_FILE") + 1))
    echo "$calls" > "$FGCR_CALLS_FILE"
    local total_lines
    total_lines=$(wc -l < "$FGCR_RESPONSES_FILE" | tr -d ' ')
    local idx=$calls
    [[ "$idx" -gt "$total_lines" ]] && idx="$total_lines"   # repeat the last canned response once exhausted
    sed -n "${idx}p" "$FGCR_RESPONSES_FILE"
}

fgcr_call_count() { cat "$FGCR_CALLS_FILE"; }

reset_test_state() {
    INFO_LOG=""
    WARN_LOG=""
    echo 0 > "$DATE_COUNTER_FILE"
    echo 0 > "$FGCR_CALLS_FILE"
    : > "$FGCR_RESPONSES_FILE"
    PR_JSON='{"head":{"sha":"deadbeef"},"base":{"ref":"main"}}'
    PR_NUMBER=42
    REPO_NWO="owner/repo"
    GH="gh"
}

# Appends one canned JSON response line to the forge_get_check_runs queue.
queue_fgcr_response() { echo "$1" >> "$FGCR_RESPONSES_FILE"; }

EMPTY_ROLLUP='{"total_count":0,"check_runs":[]}'
ONE_SUCCESS_ROLLUP='{"total_count":1,"check_runs":[{"name":"build","status":"completed","conclusion":"success"}]}'
ONE_PENDING_ROLLUP='{"total_count":1,"check_runs":[{"name":"build","status":"in_progress","conclusion":null}]}'

echo "Testing _wait_for_checks_then_sync_merge empty-output false-settle guard (#6169)..."

# (a) THE bug, reproduced: the very first poll returns a zero-row rollup
# (the exact shape a transient forge failure produces). Before the fix this
# returned 0 (settled) on that single empty read. After the fix it must NOT
# trust the empty read alone -- it must poll again, and only settle once a
# real (nonzero) rollup confirms nothing is pending.
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=100
LOOM_AUTO_MERGE_POLL_INTERVAL=1
queue_fgcr_response "$EMPTY_ROLLUP"
queue_fgcr_response "$ONE_SUCCESS_ROLLUP"
_wait_for_checks_then_sync_merge
rc=$?
calls="$(fgcr_call_count)"
assert_eq "0" "$rc" "(a) Function still returns 0 once real data confirms settlement"
assert_eq "true" "$([[ $calls -ge 2 ]] && echo true || echo false)" \
  "(a) forge_get_check_runs was polled MORE THAN ONCE (call count=$calls) -- did not trust the first empty read"

# (b) The most literal false-settle case: EVERY poll returns a zero-row
# rollup (this repo genuinely has no CI configured for this commit, OR a
# persistent glitch -- from this function's perspective these are
# indistinguishable). The function must still terminate (bounded by
# LOOM_AUTO_MERGE_TIMEOUT, simulated here via the stubbed date counter), but
# it must NOT settle on the first read -- it must poll more than once before
# giving up, and the fallback narration must say so explicitly.
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=3
LOOM_AUTO_MERGE_POLL_INTERVAL=1
queue_fgcr_response "$EMPTY_ROLLUP"
_wait_for_checks_then_sync_merge
rc=$?
calls="$(fgcr_call_count)"
assert_eq "0" "$rc" "(b) Function eventually returns 0 (bounded wait exhausted, not an infinite loop)"
assert_eq "true" "$([[ $calls -ge 2 ]] && echo true || echo false)" \
  "(b) forge_get_check_runs was polled MORE THAN ONCE before giving up (call count=$calls)"
assert_contains "$WARN_LOG" "remained empty" \
  "(b) Warns explicitly that the rollup remained empty for the whole bounded wait, rather than silently declaring settled"

# (c) Regression guard: the common healthy case is unaffected. A rollup that
# is non-empty (real data) on the very first poll, with nothing pending and
# nothing failing, settles immediately -- no unnecessary extra polling for
# the normal case.
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=100
LOOM_AUTO_MERGE_POLL_INTERVAL=1
queue_fgcr_response "$ONE_SUCCESS_ROLLUP"
_wait_for_checks_then_sync_merge
rc=$?
calls="$(fgcr_call_count)"
assert_eq "0" "$rc" "(c) Function returns 0 for a genuinely-settled, nonempty rollup"
assert_eq "1" "$calls" "(c) Only ONE poll needed for the common healthy case (no unnecessary retries)"

# (d) A still-pending check on the first poll is unaffected by the guard --
# it takes the existing pending-wait path, then settles once the check
# resolves.
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=100
LOOM_AUTO_MERGE_POLL_INTERVAL=1
queue_fgcr_response "$ONE_PENDING_ROLLUP"
queue_fgcr_response "$ONE_SUCCESS_ROLLUP"
_wait_for_checks_then_sync_merge
rc=$?
calls="$(fgcr_call_count)"
assert_eq "0" "$rc" "(d) Function returns 0 once the pending check resolves"
assert_eq "2" "$calls" "(d) Exactly two polls: one pending, one resolved"

echo ""
echo "=== Test Summary ==="
echo "Total:  $TESTS_RUN"
echo -e "Passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
else
    echo -e "Failed: $TESTS_FAILED"
    exit 0
fi
