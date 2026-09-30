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
# #9091 (the other half of the same branch): "or the bounded wait elapsed" was
# catastrophic on the case that guard meets most often -- a repo with NO CI
# configured for the changed paths returns zero rows on every poll forever, so
# every `--auto` merge there burned the entire LOOM_AUTO_MERGE_TIMEOUT (600s)
# before merging, and the calling agent's own process cap killed it first
# (a fleet repo, 2026-09-26; timeline in #9091: "Proceeding with squash
# merge..." then no merge, no failure, no label change).
#
# WHAT IS TESTED WHERE (#9091's placement, per this repo's shell-language
# policy): the zero-row DECISION -- how many empty polls are enough, the
# required-context discriminator, the LOOM_ZERO_CHECKS_SETTLE_* floors and
# fallbacks -- is `loom-daemon merge-pr zero-checks-settle`
# (loom-daemon/src/merge_pr/zero_checks.rs), and its unit tests live beside it
# in loom-daemon/src/merge_pr/zero_checks/tests.rs. merge-pr.sh's remaining
# share is INVOCATION: consult the subcommand on every zero-row poll, pass it
# the poll count and the cached required-context token, obey the sentinel it
# answers with, and fail closed onto #6169's full wait when it cannot be run at
# all. That wiring is what scenarios (a)-(b) and (e)-(i) below drive, through a
# stub daemon -- so a decision-rule change cannot be "covered" here by a test
# that only ever saw a canned answer.
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
# deterministically and instantly -- no real wall-clock waiting) AND the
# loom-daemon binary the zero-row branch shells out to, then assert on the
# function's return value, how many times it polled forge_get_check_runs, what
# it asked the daemon, and the info/warning narration it emits.
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
SLEPT_FILE="$STATE_DIR/slept-seconds"             # sum of every stubbed sleep's argument

# Never actually sleeps, but records what it was ASKED to sleep. That sum is the
# real-world latency the function would have cost, which is the #9091 assertion
# (a no-CI repo must merge in seconds, not the 600s ceiling) -- and it cannot be
# measured by counting polls alone, since the zero-row path uses a shorter
# spacing than the pending path.
sleep() { echo "$(($(cat "$SLEPT_FILE") + ${1:-0}))" > "$SLEPT_FILE"; }
slept_seconds() { cat "$SLEPT_FILE"; }
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

# --- Stub forge_get_required_status_check_contexts: only the failing-check
# branch reads it, which no scenario below exercises. The zero-row branch's own
# required-context lookup lives in the daemon subcommand as of #9091 ---
forge_get_required_status_check_contexts() { echo ""; }

# --- Stub the loom-daemon binary the zero-row branch shells out to ---
# A real binary is deliberately NOT used here: these scenarios are about the
# WIRING (is the subcommand consulted, with what, and is its verdict obeyed),
# and a real binary would make every case depend on live branch-protection
# reads. The decision rule itself is unit-tested in Rust -- see the header.
#
# The stub records every argv it is handed, then answers either from a canned
# queue ($ZCS_QUEUE, one decision line per call) or, when that is empty, with
# the DEADLINE-ONLY policy -- WAIT until the caller's own --deadline has passed,
# then TIMEOUT. That default is #6169's pre-#9091 behaviour, which keeps
# scenarios (a) and (b) measuring exactly what they measured before.
DAEMON_STUB="$STATE_DIR/loom-daemon"
ZCS_ARGV="$STATE_DIR/zcs-argv"        # one line per invocation
ZCS_QUEUE="$STATE_DIR/zcs-queue"      # canned decision lines, consumed in order
ZCS_REQUIRED_FILE="$STATE_DIR/zcs-required"   # token the default policy echoes
cat > "$DAEMON_STUB" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "$ZCS_ARGV"
# Anything but the expected verb is an old/wrong binary: exit non-zero with no
# sentinel, which is what the caller's fail-closed fallback keys on.
[[ "${1:-}" == "merge-pr" && "${2:-}" == "zero-checks-settle" ]] || exit 2
now=0 deadline=0 poll_interval=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --now) now="$2"; shift 2 ;;
        --deadline) deadline="$2"; shift 2 ;;
        --poll-interval) poll_interval="$2"; shift 2 ;;
        *) shift ;;
    esac
done
if [[ -s "$ZCS_QUEUE" ]]; then
    head -n 1 "$ZCS_QUEUE"
    tail -n +2 "$ZCS_QUEUE" > "$ZCS_QUEUE.rest" && mv "$ZCS_QUEUE.rest" "$ZCS_QUEUE"
    exit 0
fi
required="$(cat "$ZCS_REQUIRED_FILE")"
if [[ "$now" -ge "$deadline" ]]; then
    echo "LOOM-ZERO-CHECKS-TIMEOUT 0 $required PR #42: check-runs rollup remained empty (zero rows) for the entire wait; proceeding on the assumption this repo genuinely has no checks configured for this commit"
else
    echo "LOOM-ZERO-CHECKS-WAIT $poll_interval $required PR #42: check-runs rollup is empty (zero rows) -- ambiguous between 'no checks configured' and a transient forge read; re-polling before trusting it"
fi
STUB
chmod +x "$DAEMON_STUB"
export ZCS_ARGV ZCS_QUEUE ZCS_REQUIRED_FILE

# Appends one canned decision line the stub will answer with, in order.
queue_zcs_decision() { echo "$1" >> "$ZCS_QUEUE"; }
zcs_call_count() { wc -l < "$ZCS_ARGV" | tr -d ' '; }
zcs_argv() { cat "$ZCS_ARGV"; }

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
    echo 0 > "$SLEPT_FILE"
    : > "$FGCR_RESPONSES_FILE"
    PR_JSON='{"head":{"sha":"deadbeef"},"base":{"ref":"main"}}'
    PR_NUMBER=42
    REPO_NWO="owner/repo"
    GH="gh"
    : > "$ZCS_ARGV"
    : > "$ZCS_QUEUE"
    echo "none" > "$ZCS_REQUIRED_FILE"
    LOOM_DAEMON_BIN="$DAEMON_STUB"
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
# rollup, AND the base branch HAS required status-check contexts -- so a
# context that has not registered yet is a gate this merge must not jump
# (#6169's danger, preserved verbatim by #9091). The function must still
# terminate (bounded by LOOM_AUTO_MERGE_TIMEOUT, simulated here via the
# stubbed date counter), but it must NOT settle on the first read -- it must
# poll more than once before giving up, and the fallback narration must say so
# explicitly.
reset_test_state
echo "present" > "$ZCS_REQUIRED_FILE"
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
echo "Testing #9091's zero-row settle DELEGATION to loom-daemon..."

# (e) The subcommand is consulted on the very first zero-row poll, and is handed
# everything the decision needs: the PR, the repo, the base branch whose
# protection is the discriminator, the poll count, the cached required-context
# token, both interval knobs, and the caller's own clock/deadline (passed in, so
# the stubbed `date` above governs the daemon's view of time too).
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=600
LOOM_AUTO_MERGE_POLL_INTERVAL=30
queue_fgcr_response "$EMPTY_ROLLUP"
queue_zcs_decision "LOOM-ZERO-CHECKS-SETTLE 0 none PR #42: settled, no required contexts"
_wait_for_checks_then_sync_merge
rc=$?
first_argv="$(zcs_argv | head -n 1)"
assert_eq "0" "$rc" "(e) Function returns 0 when the subcommand answers SETTLE"
assert_eq "1" "$(zcs_call_count)" "(e) The subcommand was consulted exactly once for one zero-row poll"
assert_eq "1" "$(fgcr_call_count)" "(e) A SETTLE verdict is obeyed immediately -- no further check-runs poll"
assert_contains "$first_argv" "merge-pr zero-checks-settle" "(e) The zero-row decision is delegated to the daemon subcommand"
assert_contains "$first_argv" "--pr 42" "(e) ...with the PR number"
assert_contains "$first_argv" "--repo owner/repo" "(e) ...with the repo"
assert_contains "$first_argv" "--base-ref main" "(e) ...with the base branch (the required-context discriminator)"
assert_contains "$first_argv" "--polls 1" "(e) ...with the zero-row poll count"
assert_contains "$first_argv" "--required-state unknown" "(e) ...with an UNRESOLVED cache token on the first poll"
assert_contains "$first_argv" "--poll-interval 30" "(e) ...with LOOM_AUTO_MERGE_POLL_INTERVAL"
assert_contains "$first_argv" "--timeout 600" "(e) ...with LOOM_AUTO_MERGE_TIMEOUT"
assert_contains "$first_argv" "--deadline" "(e) ...and with the caller's clock and deadline, so the daemon reads no clock of its own"
assert_contains "$INFO_LOG" "settled, no required contexts" \
  "(e) The daemon's narration is replayed through the script's own info()"
assert_eq "" "$WARN_LOG" "(e) A SETTLE verdict narrates as info, never as a warning"

# (f) A WAIT verdict is obeyed literally: the function sleeps the number of
# seconds the DECISION carries (not LOOM_AUTO_MERGE_POLL_INTERVAL, which the
# bounded settle deliberately undercuts) and polls again. This is the assertion
# that #9091's whole point -- seconds, not the 600s ceiling -- survives the
# trip through the shell.
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=600
LOOM_AUTO_MERGE_POLL_INTERVAL=30
queue_fgcr_response "$EMPTY_ROLLUP"
queue_zcs_decision "LOOM-ZERO-CHECKS-WAIT 5 none PR #42: re-polling in 5s"
queue_zcs_decision "LOOM-ZERO-CHECKS-WAIT 5 none PR #42: re-polling in 5s"
queue_zcs_decision "LOOM-ZERO-CHECKS-SETTLE 0 none PR #42: settled after three polls"
_wait_for_checks_then_sync_merge
rc=$?
assert_eq "0" "$rc" "(f) Function returns 0 after the bounded settle completes"
assert_eq "3" "$(fgcr_call_count)" "(f) Each WAIT verdict produced exactly one more check-runs poll"
assert_eq "10" "$(slept_seconds)" \
  "(f) Slept the DECISION's 5s twice (=10s), not LOOM_AUTO_MERGE_POLL_INTERVAL's 30s -- and nowhere near the 600s ceiling"

# (g) The required-context token the daemon resolves is cached BY THE CALLER and
# replayed on every later poll. That is what makes the two-read branch-protection
# lookup happen once per wait instead of once per poll -- the shell's whole share
# of that property, since each invocation is a fresh process.
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=600
LOOM_AUTO_MERGE_POLL_INTERVAL=1
queue_fgcr_response "$EMPTY_ROLLUP"
queue_zcs_decision "LOOM-ZERO-CHECKS-WAIT 1 present PR #42: required contexts present, waiting"
queue_zcs_decision "LOOM-ZERO-CHECKS-WAIT 1 present PR #42: required contexts present, waiting"
queue_zcs_decision "LOOM-ZERO-CHECKS-TIMEOUT 0 present PR #42: remained empty for the entire wait"
_wait_for_checks_then_sync_merge
rc=$?
assert_eq "0" "$rc" "(g) Function returns 0 once the daemon reports the whole wait spent"
assert_eq "1" "$(zcs_argv | grep -c -- '--required-state unknown')" \
  "(g) EXACTLY ONE poll asked for an unresolved lookup -- the rest replayed the cached token"
assert_eq "2" "$(zcs_argv | grep -c -- '--required-state present')" \
  "(g) Every later poll passed back the token the daemon resolved"
assert_eq "1
2
3" "$(zcs_argv | grep -o -- '--polls [0-9]*' | awk '{print $2}')" \
  "(g) The zero-row poll count is monotonic across iterations, not reset each time"
assert_contains "$WARN_LOG" "remained empty" \
  "(g) A TIMEOUT verdict narrates through warning(), not info() -- nothing was ever confirmed"

# (h) Fail closed when the subcommand cannot be run at all, or answers with
# something that is not a decision: the bounded settle is unavailable, so
# #6169's FULL deadline-bounded wait applies. The one thing this must never
# degrade into is settling on a single empty read -- that IS #6169. Two shapes
# are checked, because they fail differently: a missing/old binary exits
# non-zero (the easy case), while garbage on stdout with a ZERO exit is the
# shape a naive caller would accept as a verdict.
#
# A "garbage, exit 0" stub lives in its own file so $DAEMON_STUB stays intact
# for the scenarios after this one.
JUNK_STUB="$STATE_DIR/loom-daemon-junk"
cat > "$JUNK_STUB" <<'JUNK'
#!/usr/bin/env bash
echo "hello, this is not a decision"
exit 0
JUNK
chmod +x "$JUNK_STUB"

for bad_bin in "$STATE_DIR/does-not-exist" "$JUNK_STUB"; do
    reset_test_state
    LOOM_DAEMON_BIN="$bad_bin"
    LOOM_AUTO_MERGE_TIMEOUT=4
    LOOM_AUTO_MERGE_POLL_INTERVAL=1
    queue_fgcr_response "$EMPTY_ROLLUP"
    _wait_for_checks_then_sync_merge
    rc=$?
    calls="$(fgcr_call_count)"
    label="$(basename "$bad_bin")"
    assert_eq "0" "$rc" "(h) Function still terminates with an unusable loom-daemon ($label)"
    assert_eq "true" "$([[ $calls -gt 1 ]] && echo true || echo false)" \
      "(h/$label) Fail-closed path polled MORE THAN ONCE (call count=$calls) -- never settles on a single empty read"
    assert_contains "$INFO_LOG" "bounded settle is unavailable" \
      "(h/$label) Says out loud, on every degraded poll, that the bounded settle was unavailable"
    assert_contains "$INFO_LOG" "falling back to #6169's full 4s wait" \
      "(h/$label) ...naming the wait it fell back to"
    assert_contains "$WARN_LOG" "bounded settle is unavailable" \
      "(h/$label) The terminal (deadline-reached) poll narrates through warning(), not info()"
done

# (i) The delegation is reachable ONLY from the zero-row branch. A healthy,
# nonempty rollup must not spend a subprocess per poll.
reset_test_state
LOOM_AUTO_MERGE_TIMEOUT=100
LOOM_AUTO_MERGE_POLL_INTERVAL=1
queue_fgcr_response "$ONE_PENDING_ROLLUP"
queue_fgcr_response "$ONE_SUCCESS_ROLLUP"
_wait_for_checks_then_sync_merge
rc=$?
assert_eq "0" "$rc" "(i) Function returns 0 for the ordinary pending-then-settled path"
assert_eq "0" "$(zcs_call_count)" "(i) The subcommand is never consulted when the rollup is non-empty"

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
