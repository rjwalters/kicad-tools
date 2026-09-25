#!/usr/bin/env bash
# test-merge-pr-auto-blocked-settle.sh — regression tests for #8410: a
# `merge-pr.sh --auto` run whose PR is BLOCKED at queue time must settle the
# checks and re-validate the approval HERE, instead of arming a server-side
# merge that ignores both.
#
# ## The incident (PR #8220, 2026-09-20)
#
#   07:08:53  operator clears loom:operator; `merge-pr.sh 8220 --auto` arms
#             GitHub's server-side auto-merge, pinned to head 490b79f1d
#   07:12:15  head force-pushed to ab58dd87d — auto-merge stays armed (the SHA
#             pin only ever guarded the enable call)
#   07:13:32  loom:verdict-stale posted; loom:pr removed, loom:review-requested
#             added
#   07:18:08  Judge re-claims the PR (loom:reviewing)
#   07:21:03  merged by the server-side queue, with Rust Unit Tests, Shell Test
#             Suites (hermetic), Installer Integration Tests, Native Port Suites
#             and Rust OTLP Feature Build & Test all still pending
#
# The sibling UNSTABLE case never had this problem: GitHub REFUSES to arm
# auto-merge while a required check is mid-flight, so the script fell back to
# polling every check-run and merging synchronously. The BLOCKED case (no
# required check has started yet) is the one where the arm SUCCEEDS — and on
# this repo every required context is a structural gate, so none of the test
# suites above could hold the server back.
#
# ## What is tested here
#
# The four acceptance criteria of #8410, exercised against the REAL functions
# extracted from merge-pr.sh (never re-implementations), with the forge helpers
# stubbed:
#
#   AC1  loom:pr removed after queueing        -> no merge (hard block, exit 1)
#   AC2  any non-skipped check-run pending or  -> no merge (wait, then a bounded
#        a required one failed                    timeout; a failed required
#                                                 check refuses immediately)
#   AC3  head changed after queueing           -> no merge (exit 3, re-queue)
#   AC4  this file, covering the BLOCKED-at-queue-time path alongside the
#        existing UNSTABLE coverage in test-merge-pr-unstable-fallback.sh
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-auto-blocked-settle.sh

# SC2034: PR_JSON/PR_NUMBER/PR_LABELS/... are read only by the extracted+sourced
# functions, which shellcheck cannot see are readers.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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
    # Here-string, never a pipe: `grep -q` exits on first match and would
    # SIGPIPE the producer under `set -o pipefail` (#7771 class).
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

# The verdict-contradiction guard's decision is `loom-daemon merge-pr
# verdict-contradiction`. Pin the binary this suite tests against — FATAL,
# never a skip (a suite that skipped itself would report green while testing
# nothing).
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"

# --- Extract the functions under test from the real merge-pr.sh -------------
FUNCS_FILE="$(mktemp)"
trap 'rm -f "$FUNCS_FILE" "${CHECK_RUNS_COUNTER:-}" 2>/dev/null || true' EXIT

_extract_block() {
    awk -v start="$1" '
        index($0, start) == 1 { f = 1 }
        f { print }
        f && $0 == "}" { exit }
    ' "$MERGE_PR_SRC"
}

{
    _extract_block 'error_head_moved() {'
    _extract_block '_check_champion_hold_state_staleness() {'
    _extract_block '_check_loom_pr_label() {'
    awk '/^_check_verdict_label_contradiction\(\) \{/ { print; exit }' "$MERGE_PR_SRC"
    _extract_block '_wait_for_checks_then_sync_merge() {'
    _extract_block '_revalidate_merge_guards() {'
} > "$FUNCS_FILE"

for fn in error_head_moved _check_loom_pr_label _check_verdict_label_contradiction \
          _wait_for_checks_then_sync_merge _revalidate_merge_guards; do
    if ! grep -q "^${fn}() {" "$FUNCS_FILE"; then
        echo -e "${RED}FATAL${NC}: could not extract ${fn} from $MERGE_PR_SRC" >&2
        exit 2
    fi
done
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Logging shims + forge stubs -------------------------------------------
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*"; }
error()   { echo "ERROR: $*" >&2; exit 1; }

REPO_NWO="rjwalters/loom"
PR_NUMBER="8220"
GH="gh"
DRY_RUN=false
ALLOW_UNAPPROVED=false
PR_HEAD_SHA="490b79f1d"
MERGE_PRECONDITION_SHA="490b79f1d"
PR_LABELS="loom:pr"
PR_JSON='{"number":8220,"head":{"sha":"490b79f1d","ref":"feature/issue-8199"},"base":{"ref":"main"},"merged":false}'

# Speed: no real sleeping, and a deadline the tests set explicitly.
LOOM_AUTO_MERGE_POLL_INTERVAL=0
LOOM_AUTO_MERGE_TIMEOUT=1
LOOM_CHECK_RUNS_404_STREAK=2
FORGE_CHECK_RUNS_RC_NOT_FOUND=44

# Poll script: one entry of check-runs JSON per poll, consumed in order; the
# last entry repeats once exhausted. This is how a BLOCKED-at-queue-time head
# is modelled — the structural gates finish first, the test suites keep
# running. The call counter lives in a FILE, not a variable: the function under
# test calls this stub inside a command substitution, i.e. in a subshell, so a
# shell variable would reset on every poll and the script would never advance.
CHECK_RUNS_SCRIPT=()
CHECK_RUNS_COUNTER="$(mktemp)"
forge_get_check_runs() {
    local idx
    idx="$(cat "$CHECK_RUNS_COUNTER")"
    echo "$(( idx + 1 ))" > "$CHECK_RUNS_COUNTER"
    (( idx >= ${#CHECK_RUNS_SCRIPT[@]} )) && idx=$(( ${#CHECK_RUNS_SCRIPT[@]} - 1 ))
    printf '%s\n' "${CHECK_RUNS_SCRIPT[$idx]}"
}
check_runs_calls() { cat "$CHECK_RUNS_COUNTER"; }

# Required contexts on `main`: on this repo they are all structural gates —
# not one of the five test suites the incident merged over.
REQUIRED_CONTEXTS=$'File Size Ratchet\nCLAUDE.md Line Budget'
REQUIRED_LOOKUP_RC=0
forge_get_required_status_check_contexts() {
    printf '%s\n' "$REQUIRED_CONTEXTS"
    return "$REQUIRED_LOOKUP_RC"
}

# The PR as the forge currently reports it (re-read by both the wait loop and
# the re-validation).
FRESH_PR_JSON="$PR_JSON"
forge_get_pr_nocache() { printf '%s\n' "$FRESH_PR_JSON"; }
forge_get_pr_comments() { printf '%s\n' ""; }
forge_gh_comment_rl_safe() { echo "COMMENT: $3"; }

# Reset per-scenario state.
_reset() {
    echo 0 > "$CHECK_RUNS_COUNTER"
    REQUIRED_CONTEXTS=$'File Size Ratchet\nCLAUDE.md Line Budget'
    REQUIRED_LOOKUP_RC=0
    FRESH_PR_JSON="$PR_JSON"
    PR_LABELS="loom:pr"
    MERGE_PRECONDITION_SHA="490b79f1d"
    ALLOW_UNAPPROVED=false
    LOOM_AUTO_MERGE_TIMEOUT=1
}

# Run one of the extracted functions in a subshell, capturing output + rc.
# `error`/`error_head_moved` exit, so the subshell is mandatory.
LAST_OUT=""
LAST_RC=0
run_fn() {
    local fn="$1"
    LAST_RC=0
    LAST_OUT="$("$fn" 2>&1)" || LAST_RC=$?
}

# Check-run fixtures. "the five suites" are the ones the incident merged over.
_runs() { printf '{"total_count":%s,"check_runs":[%s]}' "$1" "$2"; }
GATES_DONE='{"name":"File Size Ratchet","status":"completed","conclusion":"success"},{"name":"CLAUDE.md Line Budget","status":"completed","conclusion":"success"}'
SUITES_RUNNING='{"name":"Rust Unit Tests","status":"in_progress","conclusion":null},{"name":"Shell Test Suites (hermetic)","status":"queued","conclusion":null},{"name":"Installer Integration Tests","status":"queued","conclusion":null}'
SUITES_GREEN='{"name":"Rust Unit Tests","status":"completed","conclusion":"success"},{"name":"Shell Test Suites (hermetic)","status":"completed","conclusion":"success"},{"name":"Installer Integration Tests","status":"completed","conclusion":"success"}'
SUITES_FAILED='{"name":"Rust Unit Tests","status":"completed","conclusion":"failure"},{"name":"Shell Test Suites (hermetic)","status":"completed","conclusion":"success"},{"name":"Installer Integration Tests","status":"completed","conclusion":"success"}'
SUITES_SKIPPED='{"name":"Rust Unit Tests","status":"completed","conclusion":"skipped"},{"name":"Shell Test Suites (hermetic)","status":"completed","conclusion":"skipped"}'

# ===========================================================================
# AC2 — a BLOCKED-at-queue-time PR does not merge while any non-skipped
#       check-run on its head is pending or has failed.
# ===========================================================================
echo "AC2: the BLOCKED-at-queue-time head must settle before any merge..."

# The incident's exact shape: structural gates green, the five suites still
# running. The old code armed a server-side merge here and exited 0; the wait
# path must keep polling and then time out rather than merge.
_reset
CHECK_RUNS_SCRIPT=("$(_runs 5 "$GATES_DONE,$SUITES_RUNNING")")
LOOM_AUTO_MERGE_TIMEOUT=0
run_fn _wait_for_checks_then_sync_merge
assert_eq "1" "$LAST_RC" \
  "#8410 AC2: suites still pending -> the wait refuses to proceed to a merge"
assert_contains "$LAST_OUT" "pending check(s) on PR #8220" \
  "#8410 AC2: the refusal names the pending checks, not a queued merge"
assert_contains "$LAST_OUT" "for 3 pending check(s)" \
  "#8410 AC2: all three pending NON-REQUIRED suites are counted — the required set is irrelevant to the wait"

# Same head a poll later: the suites finish green, so the wait proceeds.
_reset
CHECK_RUNS_SCRIPT=(
  "$(_runs 5 "$GATES_DONE,$SUITES_RUNNING")"
  "$(_runs 5 "$GATES_DONE,$SUITES_GREEN")"
)
LOOM_AUTO_MERGE_TIMEOUT=60
run_fn _wait_for_checks_then_sync_merge
assert_eq "0" "$LAST_RC" \
  "#8410 AC2: once every suite completes green the wait returns (merge proceeds here, in-process)"
assert_contains "$LAST_OUT" "checks settled" \
  "#8410 AC2: the proceed path says the checks settled"
assert_eq "2" "$(check_runs_calls)" \
  "#8410 AC2: it actually polled again rather than trusting the first read"

# A failing NON-REQUIRED suite must also block: it is a real failure on this
# head even though the branch ruleset would have let the server merge over it.
_reset
CHECK_RUNS_SCRIPT=("$(_runs 5 "$GATES_DONE,$SUITES_FAILED")")
run_fn _wait_for_checks_then_sync_merge
assert_eq "0" "$LAST_RC" \
  "informational-only failure with nothing pending still proceeds (#3486 policy preserved)"
assert_contains "$LAST_OUT" "informational" \
  "the informational-failure proceed path is logged as such"

# A failing REQUIRED check refuses immediately — no waiting out the timeout.
_reset
REQUIRED_CONTEXTS=$'File Size Ratchet\nRust Unit Tests'
CHECK_RUNS_SCRIPT=("$(_runs 5 "$GATES_DONE,$SUITES_FAILED")")
LOOM_AUTO_MERGE_TIMEOUT=60
run_fn _wait_for_checks_then_sync_merge
assert_eq "1" "$LAST_RC" \
  "a failed REQUIRED check refuses the merge"
assert_contains "$LAST_OUT" "required status check has failed" \
  "the refusal names the failed required check"

# Skipped checks are `completed` with a non-failure conclusion, so they neither
# block nor count as pending — "non-skipped" in the AC's wording.
_reset
CHECK_RUNS_SCRIPT=("$(_runs 4 "$GATES_DONE,$SUITES_SKIPPED")")
run_fn _wait_for_checks_then_sync_merge
assert_eq "0" "$LAST_RC" \
  "#8410 AC2: skipped check-runs neither block the merge nor count as pending"

# ===========================================================================
# AC1 — a loom:pr revoked after queue time blocks the merge.
# ===========================================================================
echo ""
echo "AC1: an approval revoked during the wait must block the merge..."

# The incident's 07:13 loom:verdict-stale: loom:pr -> loom:review-requested,
# with the Judge re-claim (loom:reviewing) at 07:18.
_reset
FRESH_PR_JSON='{"number":8220,"head":{"sha":"490b79f1d"},"merged":false,"labels":[{"name":"loom:review-requested"},{"name":"loom:reviewing"}]}'
run_fn _revalidate_merge_guards
assert_eq "1" "$LAST_RC" \
  "#8410 AC1: loom:pr revoked after queueing -> the merge is hard-blocked"
assert_contains "$LAST_OUT" "does not carry the \`loom:pr\` label" \
  "#8410 AC1: the block names the missing review signal"
assert_contains "$LAST_OUT" "loom:review-requested" \
  "#8410 AC1: the block prints the label set as it stands NOW, not at queue time"

# Still approved -> the re-validation passes and the merge continues.
_reset
FRESH_PR_JSON='{"number":8220,"head":{"sha":"490b79f1d"},"merged":false,"labels":[{"name":"loom:pr"}]}'
run_fn _revalidate_merge_guards
assert_eq "0" "$LAST_RC" \
  "#8410 AC1: an intact loom:pr re-validates cleanly (no false block)"

# A verdict CONTRADICTION that appeared during the wait blocks too (#8112):
# loom:pr present but a blocking verdict label beside it.
_reset
FRESH_PR_JSON='{"number":8220,"head":{"sha":"490b79f1d"},"merged":false,"labels":[{"name":"loom:pr"},{"name":"loom:changes-requested"}]}'
run_fn _revalidate_merge_guards
assert_eq "1" "$LAST_RC" \
  "#8410 AC1: a contradicting verdict label added during the wait also blocks (#8112)"
assert_contains "$LAST_OUT" "loom:changes-requested" \
  "#8410 AC1: the contradiction block names the offending label"

# --allow-unapproved still overrides the loom:pr block, exactly as it does at
# queue time — the re-validation must not silently become un-overridable.
_reset
ALLOW_UNAPPROVED=true
FRESH_PR_JSON='{"number":8220,"head":{"sha":"490b79f1d"},"merged":false,"labels":[{"name":"loom:review-requested"}]}'
run_fn _revalidate_merge_guards
assert_eq "0" "$LAST_RC" \
  "--allow-unapproved keeps overriding the loom:pr block on re-validation"
assert_contains "$LAST_OUT" "operator asserts responsibility" \
  "the override is still recorded loudly"

# A PR that merged underneath us while we waited is not a guard failure.
_reset
FRESH_PR_JSON='{"number":8220,"head":{"sha":"490b79f1d"},"merged":true,"labels":[]}'
run_fn _revalidate_merge_guards
assert_eq "0" "$LAST_RC" \
  "a concurrently-merged PR short-circuits the re-validation instead of erroring"

# ===========================================================================
# AC3 — a head that moved after queue time invalidates the merge.
# ===========================================================================
echo ""
echo "AC3: a head moved during the wait must not be merged over..."

# The incident's 07:12 force-push, 490b79f1d -> ab58dd87d.
_reset
FRESH_PR_JSON='{"number":8220,"head":{"sha":"ab58dd87d"},"merged":false,"labels":[{"name":"loom:pr"}]}'
run_fn _revalidate_merge_guards
assert_eq "3" "$LAST_RC" \
  "#8410 AC3: a head that moved during the wait exits 3 (re-queue, not a failure)"
assert_contains "$LAST_OUT" "490b79f1d" \
  "#8410 AC3: the diagnostic names the SHA the merge was gated on"
assert_contains "$LAST_OUT" "ab58dd87d" \
  "#8410 AC3: the diagnostic names the new head too"

# The head check runs BEFORE the label checks: a moved head must re-queue
# (exit 3) rather than be reported as an approval problem (exit 1), even when
# the labels also went stale — which is exactly what happens on a rebase.
_reset
FRESH_PR_JSON='{"number":8220,"head":{"sha":"ab58dd87d"},"merged":false,"labels":[{"name":"loom:review-requested"}]}'
run_fn _revalidate_merge_guards
assert_eq "3" "$LAST_RC" \
  "#8410 AC3: a moved head + a revoked label is still the exit-3 re-queue, not exit 1"

# An unreadable fresh head (forge blip) must not be treated as "moved" — the
# merge API's own precondition is the backstop there.
_reset
FRESH_PR_JSON='{}'
run_fn _revalidate_merge_guards
assert_eq "1" "$LAST_RC" \
  "an empty fresh read falls through to the label guard (no spurious exit 3)"
assert_contains "$LAST_OUT" "does not carry the \`loom:pr\` label" \
  "an empty fresh read is treated as 'no approval visible', which fails closed"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
