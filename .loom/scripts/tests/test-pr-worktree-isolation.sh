#!/usr/bin/env bash
# test-pr-worktree-isolation.sh - Regression test for issue #3358
#
# Verifies that:
#   1. pr-worktree.sh creates a worktree at .loom/worktrees/pr-<N>/ with the
#      .loom-managed sentinel (PR-mode entry).
#   2. The orchestrator's main worktree HEAD is unchanged after a simulated
#      doctor pass that targets an external-fork-style PR branch (`fix/foo-bar`).
#   3. merge-pr.sh's regex tightening correctly classifies branch shapes:
#      - `feature/issue-42` -> issue-style (clean up issue-42 worktree)
#      - `release-1`        -> PR-style (clean up pr-<N> worktree)
#      - `fix/foo-bar`      -> PR-style
#      - `feature/issue-42-fix` (extra suffix) -> PR-style (strict regex)
#   4. doctor.md (defaults/) documents the branch-isolation requirement.
#
# This is a static / simulation test — we do not exercise the real `gh pr
# checkout` round-trip because that would require a live forge. The
# correctness of the regex and worktree-path selection is what we care about.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

PR_WORKTREE_SH="$SCRIPTS_DIR/pr-worktree.sh"
MERGE_PR_SH="$SCRIPTS_DIR/merge-pr.sh"
DOCTOR_MD="$REPO_ROOT/defaults/.claude/commands/loom/doctor.md"

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

# --- Test 1: pr-worktree.sh exists and has the sentinel write block ---
echo "Test 1: pr-worktree.sh ships sentinel write logic"
if [[ -x "$PR_WORKTREE_SH" ]]; then
    pass "pr-worktree.sh exists and is executable"
else
    fail "pr-worktree.sh missing or not executable at $PR_WORKTREE_SH"
fi
assert_grep "Loom-managed worktree marker" "$PR_WORKTREE_SH" \
    "pr-worktree.sh writes the sentinel marker"
assert_grep "# PR: " "$PR_WORKTREE_SH" \
    "pr-worktree.sh records PR number in sentinel (mirrors worktree.sh issue convention)"
assert_grep "worktrees/pr-" "$PR_WORKTREE_SH" \
    "pr-worktree.sh uses .loom/worktrees/pr-<N>/ convention"

# --- Test 2: merge-pr.sh uses strict regex and recognizes pr-<N> path ---
echo ""
echo "Test 2: merge-pr.sh uses strict branch regex + pr-<N> cleanup"
assert_grep '\^feature/issue-\(\[0-9\]\+\)\$' "$MERGE_PR_SH" \
    "merge-pr.sh uses strict ^feature/issue-([0-9]+)$ regex (tightened from trailing-digit heuristic)"
assert_grep "worktrees/pr-" "$MERGE_PR_SH" \
    "merge-pr.sh cleans up .loom/worktrees/pr-<N>/ path"
# Confirm the loose trailing-digit heuristic is gone
if grep -q "grep -oE '\[0-9\]+\$'" "$MERGE_PR_SH"; then
    fail "merge-pr.sh still uses the loose trailing-digit regex (should be tightened)"
else
    pass "loose trailing-digit regex no longer present in merge-pr.sh"
fi

# --- Test 3: doctor.md documents PR Branch Isolation ---
echo ""
echo "Test 3: doctor.md documents the worktree-isolation rule"
assert_grep "PR Branch Isolation" "$DOCTOR_MD" \
    "doctor.md has a 'PR Branch Isolation' section"
assert_grep "pr-worktree\.sh" "$DOCTOR_MD" \
    "doctor.md references pr-worktree.sh helper"
assert_grep "\^feature/issue-" "$DOCTOR_MD" \
    "doctor.md documents the feature/issue-<N> regex heuristic"

# --- Test 4: branch-to-worktree classification (in-script regex simulation) ---
echo ""
echo "Test 4: branch classification regex (simulated)"

classify_branch() {
    local branch="$1"
    if [[ "$branch" =~ ^feature/issue-([0-9]+)$ ]]; then
        echo "issue:${BASH_REMATCH[1]}"
    else
        echo "pr"
    fi
}

assert_classify() {
    local branch="$1" expected="$2"
    local got
    got=$(classify_branch "$branch")
    if [[ "$got" == "$expected" ]]; then
        pass "branch '$branch' -> $expected"
    else
        fail "branch '$branch' expected '$expected', got '$got'"
    fi
}

assert_classify "feature/issue-42"      "issue:42"
assert_classify "feature/issue-3358"    "issue:3358"
assert_classify "fix/foo-bar"           "pr"
assert_classify "release-1"             "pr"            # tightening fix: no longer matches issue style
assert_classify "fix-bug-42"            "pr"            # tightening fix: no longer matches issue style
assert_classify "feature/issue-42-fix"  "pr"            # extra suffix -> not strict match
assert_classify "jperla/fix-thing"      "pr"
assert_classify "main"                  "pr"

# --- Test 5: simulated doctor pass leaves orchestrator HEAD unchanged ---
echo ""
echo "Test 5: doctor pass on 'fix/foo-bar' does not move orchestrator HEAD"

TMP=$(mktemp -d /tmp/loom-pr-iso-test.XXXXXX)
trap 'rm -rf "$TMP"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

# Build a tiny throwaway repo so we exercise real git commands without
# touching the real workspace. We simulate the doctor's worktree-creation
# step only — we don't call `gh pr checkout` since there is no live forge.
git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

ORCH_HEAD_BEFORE=$(git rev-parse HEAD)
ORCH_BRANCH_BEFORE=$(git rev-parse --abbrev-ref HEAD)

# Simulate what the corrected doctor flow does for an external-fork PR
# whose branch is `fix/foo-bar`: create a `pr-<N>` worktree from origin/main
# and "check out" a synthetic PR branch INSIDE that worktree. We don't have a
# real PR, so we substitute a fake branch ref pointing at origin/main and
# check it out inside the dedicated worktree.
PR_NUM=999
PR_BRANCH="fix/foo-bar"
git update-ref "refs/heads/$PR_BRANCH" HEAD
git worktree add --detach ".loom/worktrees/pr-$PR_NUM" origin/main >/dev/null 2>&1

# Write a sentinel mirroring pr-worktree.sh
mkdir -p ".loom/worktrees/pr-$PR_NUM"
cat > ".loom/worktrees/pr-$PR_NUM/.loom-managed" <<EOF
# Loom-managed worktree marker
# PR: $PR_NUM
EOF

# Now "check out" the PR branch from INSIDE the worktree.
(cd ".loom/worktrees/pr-$PR_NUM" && git checkout "$PR_BRANCH" >/dev/null 2>&1)

ORCH_HEAD_AFTER=$(git rev-parse HEAD)
ORCH_BRANCH_AFTER=$(git rev-parse --abbrev-ref HEAD)

if [[ "$ORCH_HEAD_BEFORE" == "$ORCH_HEAD_AFTER" ]]; then
    pass "orchestrator HEAD SHA unchanged after PR-worktree checkout"
else
    fail "orchestrator HEAD moved: $ORCH_HEAD_BEFORE -> $ORCH_HEAD_AFTER"
fi
if [[ "$ORCH_BRANCH_BEFORE" == "$ORCH_BRANCH_AFTER" ]]; then
    pass "orchestrator branch unchanged ($ORCH_BRANCH_BEFORE)"
else
    fail "orchestrator branch moved: $ORCH_BRANCH_BEFORE -> $ORCH_BRANCH_AFTER"
fi

# Verify the worktree got the fork branch checked out
WT_BRANCH=$(git -C ".loom/worktrees/pr-$PR_NUM" rev-parse --abbrev-ref HEAD)
if [[ "$WT_BRANCH" == "$PR_BRANCH" ]]; then
    pass "PR worktree HEAD is on '$PR_BRANCH'"
else
    fail "PR worktree HEAD expected '$PR_BRANCH', got '$WT_BRANCH'"
fi

# Verify sentinel exists in the pr-<N> worktree
if [[ -f ".loom/worktrees/pr-$PR_NUM/.loom-managed" ]]; then
    pass "pr-$PR_NUM worktree has .loom-managed sentinel"
else
    fail "pr-$PR_NUM worktree missing .loom-managed sentinel"
fi

cd "$REPO_ROOT"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
