#!/usr/bin/env bash
# test-worktree-base-override.sh — Tests for --base branch override (#3729)
#
# Verifies the stacked-PR v1 base-branch override in defaults/scripts/worktree.sh:
# `worktree.sh N --base feature/issue-<parent>` branches the new worktree off
# the named parent branch instead of origin/<default>, so a child sweep stacks
# on its parent. Used by /loom:sweep --depends-on (which the daemon threads from
# dispatch_sweep's depends_on param).
#
# Coverage:
#   1. Default (no --base): new branch is created from origin/main, unchanged.
#   2. --base override: the child worktree's branch descends from the parent
#      branch (contains the parent's unique commit) and NOT from a diverged main.
#   3. --base with a nonexistent branch: hard-fails (does not silently branch
#      off main).
#   4. --base with no value: usage error.
#
# Pattern follows test-worktree-root-override.sh: throwaway bare origin + repo
# in a mktemp dir, copy worktree.sh + lib/, run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

# Build a throwaway repo with an origin/main ref plus a parent feature branch
# (feature/issue-41) that has one unique commit on top of main. Echoes the
# working-tree path.
setup_repo() {
    local name="${1:-stackrepo}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtbase.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/$name"
    (
        cd "$tmp/$name"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin main
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh
        # Create the parent branch with one unique commit and push it.
        git checkout -q -b feature/issue-41
        echo "parent-artifact" > parent-file.txt
        git add parent-file.txt
        git commit -q -m "parent: add artifact consumed by child"
        git push -q origin feature/issue-41
        # Return main to the checked-out branch so the helper auto-navigates cleanly.
        git checkout -q main
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# --- Test 1: default (no --base) branches off origin/main ---
echo "Test 1: default (no --base) branches off origin/main"
REPO=$(setup_repo defrepo)
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 100 >/tmp/wtbase-def.$$ 2>&1 || { echo "FAILED"; cat /tmp/wtbase-def.$$; }
)
# The default worktree must NOT contain the parent's unique file.
if [[ -f "$REPO/.loom/worktrees/issue-100/parent-file.txt" ]]; then
    fail "default worktree unexpectedly contains parent artifact (should branch off main)"
else
    pass "default worktree branches off main (no parent artifact present)"
fi
cleanup_repo "$REPO"

# --- Test 2: --base feature/issue-41 branches off the parent ---
echo ""
echo "Test 2: --base feature/issue-41 branches the child off the parent"
REPO=$(setup_repo baserepo)
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 42 --base feature/issue-41 >/tmp/wtbase-ovr.$$ 2>&1 || { echo "FAILED"; cat /tmp/wtbase-ovr.$$; }
)
# The child worktree must contain the parent's unique file (it descends from it).
if [[ -f "$REPO/.loom/worktrees/issue-42/parent-file.txt" ]]; then
    pass "child worktree contains the parent artifact (branched off feature/issue-41)"
else
    fail "child worktree missing parent artifact (did not branch off the parent)"
fi
# And its branch tip must have the parent commit in its ancestry.
if git -C "$REPO/.loom/worktrees/issue-42" merge-base --is-ancestor origin/feature/issue-41 HEAD 2>/dev/null; then
    pass "child branch descends from origin/feature/issue-41"
else
    fail "child branch does not descend from the parent branch"
fi
cleanup_repo "$REPO"

# --- Test 3: --base with a nonexistent branch hard-fails ---
echo ""
echo "Test 3: --base with a nonexistent branch hard-fails (no silent main fallback)"
REPO=$(setup_repo missrepo)
rc=0
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 43 --base feature/issue-9999 >/tmp/wtbase-miss.$$ 2>&1
) || rc=$?
assert_eq "$rc" "1" "worktree.sh exits 1 when --base branch cannot be resolved"
if [[ ! -d "$REPO/.loom/worktrees/issue-43" ]]; then
    pass "no worktree created for an unresolvable --base"
else
    fail "worktree created despite unresolvable --base (silent fallback to main?)"
fi
cleanup_repo "$REPO"

# --- Test 4: --base with no value is a usage error ---
echo ""
echo "Test 4: --base with no value is a usage error"
REPO=$(setup_repo novalrepo)
rc=0
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 44 --base >/tmp/wtbase-noval.$$ 2>&1
) || rc=$?
assert_eq "$rc" "1" "worktree.sh exits 1 when --base has no value"
cleanup_repo "$REPO"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
