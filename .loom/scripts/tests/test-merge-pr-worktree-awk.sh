#!/usr/bin/env bash
# test-merge-pr-worktree-awk.sh - Regression tests for the porcelain-parsing
# awk helpers in merge-pr.sh (#3671).
#
# Verifies that _find_worktree_by_branch() and _worktree_branch_for() emit
# EXACTLY ONE line for a single matching worktree stanza. Prior to #3671 both
# helpers hit the classic awk `exit`-triggers-`END` gotcha: the blank-line rule
# printed the match and called `exit`, but control transferred to the `END`
# block whose condition was still true, printing the same value a second time.
# The result was a doubled `/path\n/path` string in the post-merge
# worktree-cleanup warning (observed on botho PRs #1113 / #1116).
#
# The fix adds a `found`-flag guard so `exit` reliably means "already emitted"
# and `END` becomes a no-op once the main body has printed — mirroring the
# correct pattern already used by the --worktree-path validator.
#
# Test strategy:
#   1. Static grep assertions that the live source retains the `!found` guard
#      in both helpers (guards against a regression to the un-guarded awk).
#   2. Behavioral assertions: pipe synthetic multi-stanza `git worktree list
#      --porcelain` blocks through awk bodies that mirror the fixed source and
#      assert exactly-one-line output for:
#        - a match found mid-list (not last stanza) — exercises the
#          `exit`-triggers-`END` path (blank line terminates the stanza).
#        - a match found in the last stanza with NO trailing blank line —
#          exercises the `END`-only path.
#        - no match at all — asserts empty output.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

# assert_single_line <label> <expected> <actual>
# Asserts $actual equals $expected AND contains no embedded newline (i.e. the
# helper emitted a single line, not a doubled `/path\n/path` string).
assert_single_line() {
    local label="$1" expected="$2" actual="$3"
    local nlines
    nlines=$(printf '%s' "$actual" | grep -c '' || true)
    if [[ "$actual" == "$expected" ]] && [[ "$nlines" -le 1 ]]; then
        pass "$label (single line: '$actual')"
    else
        fail "$label: expected exactly one line '$expected'; got ${nlines} line(s): $(printf '%q' "$actual")"
    fi
}

[[ -f "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not found" >&2; exit 1; }

# --- awk bodies mirroring the FIXED source (kept in sync via Test 1 grep) ---

# find_wt_by_branch <want_branch>  (reads porcelain on stdin)
find_wt_by_branch() {
    awk -v want="refs/heads/$1" '
      /^worktree / { wt=$2; br=""; next }
      /^branch /   { br=$2 }
      /^$/         { if (br == want && !found) { print wt; found=1; exit } }
      END          { if (br == want && !found) { print wt } }
    '
}

# wt_branch_for <target_abs_path>  (reads porcelain on stdin)
wt_branch_for() {
    awk -v p="$1" '
      /^worktree / { wt=$2; br=""; next }
      /^branch /   { br=$2 }
      /^$/         { if (wt == p && br != "" && !found) { sub(/^refs\/heads\//, "", br); print br; found=1; exit } }
      END          { if (wt == p && br != "" && !found) { sub(/^refs\/heads\//, "", br); print br } }
    '
}

# Synthetic porcelain with a trailing blank line after every stanza (the normal
# `git worktree list --porcelain` shape). The target match is MID-list.
PORCELAIN_MIDLIST="worktree /repo/main
HEAD 1111111111111111111111111111111111111111
branch refs/heads/main

worktree /repo/wt-a
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feature/issue-42

worktree /repo/wt-b
HEAD 3333333333333333333333333333333333333333
branch refs/heads/other
"

# Synthetic porcelain whose matching stanza is LAST with NO trailing blank line.
# This is the case that only the END block can catch.
PORCELAIN_LAST_NOBLANK="worktree /repo/main
HEAD 1111111111111111111111111111111111111111
branch refs/heads/main

worktree /repo/wt-a
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feature/issue-42"

# --- Test 1: source retains the found-flag guard in both helpers ---
echo "Test 1: merge-pr.sh source retains the found-flag guard (no double-print)"

assert_grep 'if \(br == want && !found\) \{ print wt; found=1; exit \}' "$MERGE_PR" \
    "_find_worktree_by_branch blank-line rule guards print with !found + sets found=1"
assert_grep 'END *\{ if \(br == want && !found\) \{ print wt \} \}' "$MERGE_PR" \
    "_find_worktree_by_branch END block guards re-print with !found"
assert_grep 'if \(wt == p && br != "" && !found\) \{.*print br; found=1; exit \}' "$MERGE_PR" \
    "_worktree_branch_for blank-line rule guards print with !found + sets found=1"
assert_grep 'END *\{ if \(wt == p && br != "" && !found\) \{.*print br \} \}' "$MERGE_PR" \
    "_worktree_branch_for END block guards re-print with !found"

# --- Test 2: _find_worktree_by_branch emits exactly one line ---
echo ""
echo "Test 2: _find_worktree_by_branch (single-line output)"

out=$(printf '%s' "$PORCELAIN_MIDLIST" | find_wt_by_branch "feature/issue-42")
assert_single_line "mid-list match (exit-triggers-END path)" "/repo/wt-a" "$out"

out=$(printf '%s' "$PORCELAIN_LAST_NOBLANK" | find_wt_by_branch "feature/issue-42")
assert_single_line "last-stanza match, no trailing blank (END-only path)" "/repo/wt-a" "$out"

out=$(printf '%s' "$PORCELAIN_MIDLIST" | find_wt_by_branch "no/such/branch")
if [[ -z "$out" ]]; then
    pass "no match yields empty output"
else
    fail "no match: expected empty output; got $(printf '%q' "$out")"
fi

# --- Test 3: _worktree_branch_for emits exactly one line ---
echo ""
echo "Test 3: _worktree_branch_for (single-line output, refs/heads/ stripped)"

out=$(printf '%s' "$PORCELAIN_MIDLIST" | wt_branch_for "/repo/wt-a")
assert_single_line "mid-list worktree (exit-triggers-END path)" "feature/issue-42" "$out"

out=$(printf '%s' "$PORCELAIN_LAST_NOBLANK" | wt_branch_for "/repo/wt-a")
assert_single_line "last-stanza worktree, no trailing blank (END-only path)" "feature/issue-42" "$out"

out=$(printf '%s' "$PORCELAIN_MIDLIST" | wt_branch_for "/repo/does-not-exist")
if [[ -z "$out" ]]; then
    pass "no matching worktree path yields empty output"
else
    fail "no match: expected empty output; got $(printf '%q' "$out")"
fi

# --- Test 4: un-guarded (pre-#3671) awk DOES double-print — sentinel check ---
# Confirms the test fixtures actually exercise the bug: running the OLD awk body
# against the same fixture reproduces the doubled output. If this ever stops
# doubling, the fixtures no longer cover the regression and Test 2/3 are hollow.
echo ""
echo "Test 4: pre-fix awk reproduces the double-print (fixture sanity)"

find_wt_by_branch_BUGGY() {
    awk -v want="refs/heads/$1" '
      /^worktree / { wt=$2; br=""; next }
      /^branch /   { br=$2 }
      /^$/         { if (br == want) { print wt; exit } }
      END          { if (br == want) { print wt } }
    '
}

out=$(printf '%s' "$PORCELAIN_MIDLIST" | find_wt_by_branch_BUGGY "feature/issue-42")
nlines=$(printf '%s' "$out" | grep -c '' || true)
if [[ "$nlines" -eq 2 ]] && [[ "$out" == $'/repo/wt-a\n/repo/wt-a' ]]; then
    pass "pre-fix awk doubles the path (fixture exercises the bug)"
else
    fail "pre-fix awk expected 2 doubled lines; got ${nlines}: $(printf '%q' "$out")"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
