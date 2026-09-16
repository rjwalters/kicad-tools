#!/usr/bin/env bash
# test-merge-pr-loom-pr-label-guard.sh - Unit tests for the PRE-merge
# loom:pr review-signal guard in merge-pr.sh (#7419).
#
# Before either the auto-merge or synchronous-merge path attempts the actual
# merge API call, merge-pr.sh now runs a guard that refuses to merge a PR
# whose current head does not carry the `loom:pr` label — the only
# forge-visible signal that Judge reviewed that head. By default the guard
# HARD-BLOCKS the merge (error, exit 1), printing the current label set and
# head SHA. `--allow-unapproved` bypasses the block (records a warning and,
# on a real run, a best-effort PR comment). `--dry-run` reports the would-be
# block but never exits 1. When `loom:pr` IS present, the guard also WARNS
# (never hard-blocks) if a `<!-- champion:hold-state head=<sha> -->` marker
# in the PR's comments names a SHA different from the current head.
#
# Strategy (mirrors test-merge-pr-merge-ordering-guard.sh): the functions
# under test (_check_loom_pr_label, _check_champion_hold_state_staleness)
# depend only on globals (PR_NUMBER, REPO_NWO, PR_LABELS, PR_HEAD_SHA,
# DRY_RUN, ALLOW_UNAPPROVED) plus forge_get_pr_comments() and
# forge_gh_comment_rl_safe() (both stubbed as shell functions here, not real
# forge-helpers.sh — this test does not source that file). We extract the
# function definitions from merge-pr.sh and source them, stub their forge
# calls, then assert on exit code + emitted message. Because the block path
# calls `error` (which `exit 1`s), the guard is invoked inside a
# command-substitution subshell so the exit does not tear down the test.
# Extracting from source (rather than replicating) keeps the test in
# lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-loom-pr-label-guard.sh

# SC2034: several globals (PR_NUMBER, REPO_NWO, PR_LABELS, PR_HEAD_SHA,
# DRY_RUN, ALLOW_UNAPPROVED) are read only by the functions extracted+sourced
# from merge-pr.sh, which shellcheck cannot see — every such assignment looks
# "unused" to the linter.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# Colors
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
    # Here-string, not a pipe, so grep -q exiting early on a match cannot
    # SIGPIPE printf and (under set -o pipefail) flip the pipeline non-zero
    # despite a match — a size-sensitive flake on large haystacks (#3820).
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

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Minimal logging/error shims the extracted functions call ---
# `error` must exit non-zero to faithfully model the real script's hard block;
# the guard is always invoked in a subshell (see run_guard) so this exit only
# tears down that subshell, not the test.
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- Stub the two forge calls the extracted functions depend on ---
# Overridable per-test via the FAKE_* globals below.
#
# run_guard (below) invokes the guard inside a `$( ... )` command-substitution
# subshell (needed so the block path's `error`/exit does not tear down the
# test). A subshell cannot mutate the parent shell's variables, so tracking
# "was the comment posted, and with what body" via a plain variable would
# silently always read as "never called" — this must go through a FILE
# instead, which is visible from both sides.
COMMENT_POST_LOG="$(mktemp)"
FAKE_PR_COMMENTS=""
FAKE_COMMENT_POST_RC=0
forge_get_pr_comments() { printf '%s' "$FAKE_PR_COMMENTS"; }
forge_gh_comment_rl_safe() {
    printf '%s' "$3" >> "$COMMENT_POST_LOG"
    printf '\n---CALL-BOUNDARY---\n' >> "$COMMENT_POST_LOG"
    return "$FAKE_COMMENT_POST_RC"
}
comment_post_call_count() {
    [[ -s "$COMMENT_POST_LOG" ]] || { echo 0; return; }
    grep -c '^---CALL-BOUNDARY---$' "$COMMENT_POST_LOG"
}
reset_comment_post_log() { : > "$COMMENT_POST_LOG"; }
last_posted_comment() {
    # Everything before the LAST call-boundary marker's preceding boundary
    # (or start of file for a single call) — for these tests only ever one
    # call is made per reset, so the whole (trimmed) file is that one body.
    sed '/^---CALL-BOUNDARY---$/d' "$COMMENT_POST_LOG"
}

# --- Extract the functions under test from merge-pr.sh and source them ---
# From `_check_champion_hold_state_staleness() {` up to (not including) the
# `# Invoke the guard before either merge path attempts the actual merge API`
# invocation comment that follows _check_loom_pr_label's definition.
# Extracting from source keeps the test in lockstep with the script.
FUNCS_FILE="$(mktemp)"
trap 'rm -f "$FUNCS_FILE" "$COMMENT_POST_LOG" 2>/dev/null || true' EXIT
awk '
  /^_check_champion_hold_state_staleness\(\) \{/ { capture=1 }
  /^# Invoke the guard before either merge path attempts the actual merge API$/ { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_check_champion_hold_state_staleness()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_champion_hold_state_staleness from $MERGE_PR_SRC" >&2
    exit 2
fi
if ! grep -q '_check_loom_pr_label()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_loom_pr_label from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Shared globals the functions read (see the file-level SC2034 disable). ---
PR_NUMBER="999"
REPO_NWO="owner/repo"
PR_LABELS=""
PR_HEAD_SHA="abc1234"
DRY_RUN=false
ALLOW_UNAPPROVED=false

# Run the guard in a subshell (its block path calls `error`, which exit 1's),
# capturing combined stdout+stderr in LAST_OUT and the exit code in LAST_RC.
LAST_OUT=""
LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( _check_loom_pr_label 2>&1 )"
    LAST_RC=$?
    set -e
}

echo "Testing _check_loom_pr_label behavior..."

# T1: loom:pr absent -> hard block (exit 1) naming the label set and head SHA.
DRY_RUN=false; ALLOW_UNAPPROVED=false
PR_LABELS=$'loom:review-requested\nloom:operator'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "1" "$LAST_RC" "loom:pr absent -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "Merge blocked" "loom:pr absent -> block message emitted"
assert_contains "$LAST_OUT" "loom:review-requested" "Block message prints the current label set"
assert_contains "$LAST_OUT" "deadbeef" "Block message prints the current head SHA"
assert_contains "$LAST_OUT" "--allow-unapproved" "Block message mentions the --allow-unapproved override"

# T2: loom:pr absent + --dry-run -> warning printed, exits 0, no hard block.
DRY_RUN=true; ALLOW_UNAPPROVED=false
PR_LABELS=$'loom:review-requested'
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "0" "$LAST_RC" "--dry-run + loom:pr absent -> guard does NOT exit 1 (dry-run contract)"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run -> reports the would-be block"
DRY_RUN=false

# T3: loom:pr absent + --allow-unapproved -> merge proceeds (rc 0); warning
# emitted and the override is recorded via a PR comment (real run, not dry-run).
DRY_RUN=false; ALLOW_UNAPPROVED=true
PR_LABELS=$'loom:review-requested\nloom:operator'
PR_HEAD_SHA="deadbeef"
reset_comment_post_log
run_guard
assert_eq "0" "$LAST_RC" "--allow-unapproved + loom:pr absent -> guard proceeds (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "--allow-unapproved -> no hard block"
assert_contains "$LAST_OUT" "--allow-unapproved set" "--allow-unapproved -> override warning emitted"
assert_eq "1" "$(comment_post_call_count)" "--allow-unapproved (real run) -> override recorded via a PR comment"
assert_contains "$(last_posted_comment)" "deadbeef" "Override comment records the head SHA"
assert_contains "$(last_posted_comment)" "loom:review-requested" "Override comment records the label set"
ALLOW_UNAPPROVED=false

# T4: loom:pr absent + --allow-unapproved + --dry-run -> proceeds (rc 0), but
# NO PR comment is posted (a dry-run must have zero forge side effects).
DRY_RUN=true; ALLOW_UNAPPROVED=true
PR_LABELS=$'loom:review-requested'
PR_HEAD_SHA="deadbeef"
reset_comment_post_log
run_guard
assert_eq "0" "$LAST_RC" "--allow-unapproved + --dry-run -> guard proceeds (exit 0)"
assert_eq "0" "$(comment_post_call_count)" "--allow-unapproved + --dry-run -> no PR comment posted (no side effects)"
DRY_RUN=false; ALLOW_UNAPPROVED=false

# T5: loom:pr present -> guard is a no-op (matches today's behavior exactly),
# no comment posted either.
DRY_RUN=false; ALLOW_UNAPPROVED=false
PR_LABELS=$'loom:review-requested\nloom:pr'
PR_HEAD_SHA="deadbeef"
FAKE_PR_COMMENTS=""
reset_comment_post_log
run_guard
assert_eq "0" "$LAST_RC" "loom:pr present -> guard passes (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "loom:pr present -> no block message"
assert_not_contains "$LAST_OUT" "--allow-unapproved set" "loom:pr present -> no override warning (not needed)"
assert_eq "0" "$(comment_post_call_count)" "loom:pr present -> no PR comment posted"

# T6: loom:pr present + a champion:hold-state marker whose SHA differs from
# PR_HEAD_SHA -> WARNING printed, guard still passes (exit 0) — a stale hold
# head does not itself hard-block when loom:pr is present.
DRY_RUN=false; ALLOW_UNAPPROVED=false
PR_LABELS=$'loom:pr'
PR_HEAD_SHA="deadbeef"
FAKE_PR_COMMENTS='<!-- champion:hold-state head=abc1234 -->
Some other hold-state prose.'
run_guard
assert_eq "0" "$LAST_RC" "loom:pr present + stale hold-state -> guard still passes (exit 0)"
assert_contains "$LAST_OUT" "champion:hold-state marker recorded head=abc1234" "Stale hold-state -> warning names the recorded (stale) head"
assert_contains "$LAST_OUT" "deadbeef" "Stale hold-state warning names the current head too"

# T7: loom:pr present + a champion:hold-state marker whose SHA MATCHES the
# current head -> no warning (the common case for a held-then-released PR).
DRY_RUN=false; ALLOW_UNAPPROVED=false
PR_LABELS=$'loom:pr'
PR_HEAD_SHA="deadbeef"
FAKE_PR_COMMENTS='<!-- champion:hold-state head=deadbeef -->
Held pending merge-risk review.'
run_guard
assert_eq "0" "$LAST_RC" "loom:pr present + matching hold-state -> guard passes (exit 0)"
assert_not_contains "$LAST_OUT" "champion:hold-state marker recorded" "Matching hold-state head -> no staleness warning"
FAKE_PR_COMMENTS=""

# T8: loom:pr present + no champion:hold-state marker at all (never held) ->
# no warning, no crash on empty/absent comments.
DRY_RUN=false; ALLOW_UNAPPROVED=false
PR_LABELS=$'loom:pr'
PR_HEAD_SHA="deadbeef"
FAKE_PR_COMMENTS='Just a regular Judge approval comment, no marker here.'
run_guard
assert_eq "0" "$LAST_RC" "loom:pr present + no hold-state marker -> guard passes (exit 0)"
assert_not_contains "$LAST_OUT" "champion:hold-state marker recorded" "No hold-state marker -> no staleness warning"
FAKE_PR_COMMENTS=""

# T9: loom:pr absent + empty label array (edge case: a PR with NO labels at
# all) -> still hard-blocks, and the "<none>" placeholder is used instead of
# an empty string in the message.
DRY_RUN=false; ALLOW_UNAPPROVED=false
PR_LABELS=""
PR_HEAD_SHA="deadbeef"
run_guard
assert_eq "1" "$LAST_RC" "Empty label array -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "<none>" "Empty label array -> message uses <none> placeholder, not a blank line"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_check_loom_pr_label" \
  "merge-pr.sh defines and invokes _check_loom_pr_label"
assert_contains "$src" "ALLOW_UNAPPROVED" \
  "merge-pr.sh threads the --allow-unapproved override into the guard"
assert_contains "$src" "--allow-unapproved) ALLOW_UNAPPROVED=true" \
  "merge-pr.sh parses the --allow-unapproved flag alongside the other options"
assert_contains "$src" "_check_champion_hold_state_staleness" \
  "merge-pr.sh defines the champion:hold-state staleness check"
assert_contains "$src" 'PR_LABELS=$(echo "$PR_JSON" | jq -r' \
  "merge-pr.sh extracts PR_LABELS from the already-fetched PR_JSON (no extra API call)"

# Assert the guard is invoked BEFORE the auto-merge path (line ordering): the
# _check_loom_pr_label invocation must precede `# Handle auto-merge mode`.
guard_line="$(grep -n '^_check_loom_pr_label$' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
automerge_line="$(grep -n '^# Handle auto-merge mode' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
if [[ -n "$guard_line" && -n "$automerge_line" && "$guard_line" -lt "$automerge_line" ]]; then
    ordered="yes"
else
    ordered="no (guard=$guard_line automerge=$automerge_line)"
fi
assert_eq "yes" "$ordered" \
  "guard is invoked before both merge paths (before '# Handle auto-merge mode')"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
