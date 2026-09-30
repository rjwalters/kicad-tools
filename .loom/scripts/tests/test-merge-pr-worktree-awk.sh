#!/usr/bin/env bash
# test-merge-pr-worktree-awk.sh - Regression tests for merge-pr.sh's
# porcelain-parsing worktree lookups (#3671; ported to Rust by an #8191 slice).
#
# Verifies that the "which worktree / which branch" lookups emit EXACTLY ONE
# line for a single matching worktree stanza. Prior to #3671 both helpers hit
# the classic awk `exit`-triggers-`END` gotcha: the blank-line rule printed the
# match and called `exit`, but control transferred to the `END` block whose
# condition was still true, printing the same value a second time. The result
# was a doubled `/path\n/path` string in the post-merge worktree-cleanup warning
# (observed on a consumer repo's PRs, #3671).
#
# WHAT CHANGED IN THE #8191 SLICE
#
# The parse moved out of awk into `loom-daemon merge-pr worktree-*`
# (loom-daemon/src/merge_pr/worktrees.rs); merge-pr.sh still runs `git worktree
# list --porcelain` and pipes it in. So:
#
#   - The Test 1 assertions about the awk SOURCE TEXT cannot survive — the awk
#     they grep for is gone from the file. They are retired below under
#     verification-recipes.md §6, naming their successors. In their place, Test 1
#     asserts the delegation the behaviour now depends on.
#   - Tests 2 and 3 got STRONGER rather than being retired. They used to drive
#     hand-copied awk bodies defined in this file — a mirror that could drift
#     from the source it claimed to test. They now drive the real implementation
#     through the same subcommand merge-pr.sh invokes, against the same fixtures
#     and the same assertions.
#   - Test 4 (the pre-fix awk still doubles) is unchanged: it proves the fixtures
#     still exercise the bug, which is what makes Tests 2/3 mean anything.
#
# Test strategy:
#   1. Static grep assertions that the live source delegates the parse (and the
#      four retired awk-body assertions, printed with their successors).
#   2/3. Behavioral assertions: pipe synthetic multi-stanza `git worktree list
#      --porcelain` blocks through the real subcommand and assert
#      exactly-one-line output for:
#        - a match found mid-list (not last stanza) — exercises the
#          `exit`-triggers-`END` path (blank line terminates the stanza).
#        - a match found in the last stanza with NO trailing blank line —
#          exercises the `END`-only path.
#        - no match at all — asserts empty output.
#   4. Sentinel: the un-guarded pre-#3671 awk still doubles on fixture 1.
#   5. "No match" (exit 0) stays distinguishable from "could not run" (non-zero).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"

# #8191 slice: the lookups now delegate to `loom-daemon merge-pr worktree-*`.
# Pin the binary and verify it HAS the leaf subcommands — a binary with only the
# `merge-pr` group predates this slice and would make every lookup below return
# empty, which is the reading that lets cleanup act on the wrong path.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "merge-pr worktree-primary" \
    "merge-pr worktree-branch-for" "merge-pr worktree-find-by-branch"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'   # retired() below
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# An assertion that CANNOT survive the port to Rust, retired under the
# three-part test in defaults/docs/verification-recipes.md §6. Printed, not
# deleted: a reader must be able to see what was removed, why it could not
# survive, and what proves the property now. Counted as run so the totals stay
# honest.
retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
}

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

# assert_single_line <label> <expected> <actual>
# Asserts $actual equals $expected AND contains no embedded newline (i.e. the
# lookup emitted a single line, not a doubled `/path\n/path` string).
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

# --- The real implementation, invoked exactly as merge-pr.sh invokes it ------
#
# Previously this file carried hand-copied awk bodies "mirroring the fixed
# source", kept honest only by the Test 1 greps. Driving the real subcommand
# removes the mirror entirely: there is nothing left to drift.

# find_wt_by_branch <want_branch>  (reads porcelain on stdin)
find_wt_by_branch() {
    "$LOOM_DAEMON_SELF_BIN" merge-pr worktree-find-by-branch --branch "$1"
}

# wt_branch_for <target_abs_path>  (reads porcelain on stdin)
wt_branch_for() {
    "$LOOM_DAEMON_SELF_BIN" merge-pr worktree-branch-for --path "$1"
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

# --- Test 1: the live source delegates the parse -----------------------------
echo "Test 1: merge-pr.sh delegates the porcelain parse to loom-daemon (#8191)"

assert_grep '_mp_worktree worktree-find-by-branch --branch' "$MERGE_PR" \
    "_find_worktree_by_branch delegates to 'merge-pr worktree-find-by-branch'"
assert_grep '_mp_worktree worktree-branch-for --path' "$MERGE_PR" \
    "_worktree_branch_for delegates to 'merge-pr worktree-branch-for'"
assert_grep '_mp_worktree worktree-primary' "$MERGE_PR" \
    "_primary_worktree_path delegates to 'merge-pr worktree-primary'"
assert_grep 'git -C "\$REPO_ROOT" worktree list --porcelain' "$MERGE_PR" \
    "the git invocation itself did NOT move — only the parse did"

retired "the four found-flag guard greps over merge-pr.sh's awk bodies" \
    "a single matching stanza yields exactly ONE line, never a doubled '/path\\n/path' (#3671)" \
    "the awk is no longer in merge-pr.sh; it is Rust in loom-daemon/src/merge_pr/worktrees.rs, so no grep of this file can pass" \
    "Tests 2/3 below now drive the REAL implementation (they previously drove a hand-copied awk mirror), plus loom-daemon/tests/merge_pr_worktrees_differential.rs, which compares it against a frozen copy of those exact awk bodies over a multi-entry corpus, and a_mid_list_match_yields_exactly_one_answer in src/merge_pr/worktrees/tests.rs — the port returns an Option, so the doubled shape is unrepresentable rather than guarded against"

# --- Test 2: the branch->worktree lookup emits exactly one line ---
echo ""
echo "Test 2: worktree-find-by-branch (single-line output)"

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

# --- Test 3: the worktree->branch lookup emits exactly one line ---
echo ""
echo "Test 3: worktree-branch-for (single-line output, refs/heads/ stripped)"

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

# --- Test 5: "no answer" and "could not answer" stay distinguishable ---------
# The #3710 primary-worktree guard reads an empty answer as "the target is not
# the primary checkout" and proceeds to remove it. So the subcommand must exit 0
# on a genuine miss and NON-zero when it could not run at all — which is what
# merge-pr.sh's _primary_worktree_path turns into a refusal.
echo ""
echo "Test 5: exit codes distinguish 'no match' from 'could not run'"

rc=0; out=$(printf '%s' "" | "$LOOM_DAEMON_SELF_BIN" merge-pr worktree-primary) || rc=$?
if [[ "$rc" -eq 0 ]] && [[ -z "$out" ]]; then
    pass "empty porcelain: exit 0 with empty output (an answer, not an error)"
else
    fail "empty porcelain: expected exit 0 and empty output; got rc=$rc out=$(printf '%q' "$out")"
fi

rc=0; printf '%s' "$PORCELAIN_MIDLIST" | "$LOOM_DAEMON_SELF_BIN" merge-pr worktree-no-such-verb >/dev/null 2>&1 || rc=$?
if [[ "$rc" -ne 0 ]]; then
    pass "an unknown verb exits non-zero (a binary predating the port cannot read as 'no match')"
else
    fail "an unknown verb exited 0 — a stale binary would be indistinguishable from a miss"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
