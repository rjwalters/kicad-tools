#!/usr/bin/env bash
# test-default-branch.sh — Tests for default-branch detection (#3549)
#
# Verifies defaults/scripts/lib/default-branch.sh :: loom_default_branch and its
# integration into worktree.sh, which historically hardcoded `origin/main` and
# broke on repos whose default branch is `master`.
#
# Coverage:
#   1. LOOM_DEFAULT_BRANCH env override wins over everything.
#   2. git symbolic-ref detection on a main-default repo -> "main".
#   3. git symbolic-ref detection on a master-default repo -> "master".
#   4. ls-remote --symref fallback when refs/remotes/origin/HEAD is unset.
#   5. Local probe fallback (origin/HEAD unset AND remote unreachable) -> main/master.
#   6. Hard-fail (non-zero, remediation on stderr) when undetectable — never a
#      silent `main` default.
#   7. Integration: worktree.sh creates a worktree on a master-default repo
#      (branch based on origin/master, .loom-managed sentinel present).
#
# Pattern follows test-worktree-root-override.sh: throwaway bare origin + repo
# in a mktemp dir, copy worktree.sh + lib/, run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"
DEFAULT_BRANCH_LIB="$SCRIPTS_DIR/lib/default-branch.sh"

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
assert_file() {
    if [[ -f "$1" ]]; then pass "$2"; else fail "$2 (expected file: $1)"; fi
}

# Build a throwaway repo with a bare origin whose default branch is $2.
# Echoes the working-tree path.
setup_repo() {
    local name="${1:-myrepo}"
    local branch="${2:-main}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-defbranch.XXXXXX)
    git init -q -b "$branch" "$tmp/origin.git" --bare
    git init -q -b "$branch" "$tmp/$name"
    (
        cd "$tmp/$name"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin "$branch"
        # Populate refs/remotes/origin/HEAD (as a normal clone would have).
        git remote set-head origin -a >/dev/null 2>&1 || true
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# shellcheck source=../lib/default-branch.sh
source "$DEFAULT_BRANCH_LIB"

# --- Test 1: env override wins ---
echo "Test 1: LOOM_DEFAULT_BRANCH env override"
REPO=$(setup_repo envrepo master)
(
    cd "$REPO"
    # Even though this repo is master-default, the env override must win.
    r=$(LOOM_DEFAULT_BRANCH="develop" loom_default_branch)
    [[ "$r" == "develop" ]] && echo OK || echo "GOT:$r"
) | grep -q OK && pass "env override returns 'develop'" || fail "env override ignored"
cleanup_repo "$REPO"

# --- Test 2: symbolic-ref on a main-default repo ---
echo ""
echo "Test 2: symbolic-ref detection (main-default repo)"
REPO=$(setup_repo mainrepo main)
r=$(cd "$REPO" && unset LOOM_DEFAULT_BRANCH; loom_default_branch)
assert_eq "$r" "main" "main-default repo resolves to 'main'"
cleanup_repo "$REPO"

# --- Test 3: symbolic-ref on a master-default repo ---
echo ""
echo "Test 3: symbolic-ref detection (master-default repo)"
REPO=$(setup_repo masterrepo master)
r=$(cd "$REPO" && unset LOOM_DEFAULT_BRANCH; loom_default_branch)
assert_eq "$r" "master" "master-default repo resolves to 'master'"
cleanup_repo "$REPO"

# --- Test 4: ls-remote --symref fallback when origin/HEAD is unset ---
echo ""
echo "Test 4: ls-remote --symref fallback (origin/HEAD unset)"
REPO=$(setup_repo lsremoterepo master)
(
    cd "$REPO"
    # Remove the local symbolic ref so tier 2 misses and tier 3 (ls-remote) fires.
    git symbolic-ref -d refs/remotes/origin/HEAD >/dev/null 2>&1 || true
)
r=$(cd "$REPO" && unset LOOM_DEFAULT_BRANCH; loom_default_branch)
assert_eq "$r" "master" "ls-remote fallback resolves master when origin/HEAD unset"
cleanup_repo "$REPO"

# --- Test 5: local probe fallback (origin/HEAD unset AND remote unreachable) ---
echo ""
echo "Test 5: local probe fallback (no origin/HEAD, remote gone)"
REPO=$(setup_repo proberepo master)
(
    cd "$REPO"
    git symbolic-ref -d refs/remotes/origin/HEAD >/dev/null 2>&1 || true
    # Point origin at a dead path so ls-remote fails; the remote-tracking ref
    # refs/remotes/origin/master still exists locally for the probe tier.
    git remote set-url origin /nonexistent/loom-dead-remote.git
)
r=$(cd "$REPO" && unset LOOM_DEFAULT_BRANCH; loom_default_branch)
assert_eq "$r" "master" "local probe resolves master via refs/remotes/origin/master"
cleanup_repo "$REPO"

# --- Test 6: hard-fail when undetectable (no silent main default) ---
echo ""
echo "Test 6: hard-fail when the default branch cannot be determined"
HARD=$(mktemp -d /tmp/loom-defbranch-hard.XXXXXX)
(
    cd "$HARD"
    git init -q "$HARD/repo"
    cd "$HARD/repo"
    git config user.email t@t
    git config user.name t
    # No remote at all: no origin/HEAD, no ls-remote, no refs/remotes/origin/*.
    git remote add origin /nonexistent/loom-dead.git
)
if (cd "$HARD/repo" && unset LOOM_DEFAULT_BRANCH; loom_default_branch >/dev/null 2>&1); then
    fail "hard-fail case unexpectedly succeeded (silent default?)"
else
    pass "returns non-zero when default branch is undetectable"
fi
# Remediation hint must reach stderr. Capture stderr into a var first so
# pipefail doesn't fold loom_default_branch's intentional exit-1 into a grep
# pipeline (which would mask a matching hint).
hint_stderr=$(cd "$HARD/repo" && unset LOOM_DEFAULT_BRANCH; loom_default_branch 2>&1 >/dev/null || true)
if grep -q "LOOM_DEFAULT_BRANCH" <<<"$hint_stderr"; then
    pass "hard-fail emits a remediation hint on stderr"
else
    fail "hard-fail did not emit remediation hint"
fi
rm -rf "$HARD"

# --- Test 7: integration — worktree.sh on a master-default repo ---
echo ""
echo "Test 7: worktree.sh creates a worktree on a master-default repo"
REPO=$(setup_repo wtmaster master)
(
    cd "$REPO"
    unset LOOM_DEFAULT_BRANCH
    ./.loom/scripts/worktree.sh 700 >/tmp/defbranch-wt.$$ 2>&1 || {
        echo "worktree.sh failed (see below):"; cat /tmp/defbranch-wt.$$
    }
)
if [[ -d "$REPO/.loom/worktrees/issue-700" ]]; then
    pass "worktree created on master-default repo"
else
    fail "worktree NOT created on master-default repo"
fi
assert_file "$REPO/.loom/worktrees/issue-700/.loom-managed" "worktree carries .loom-managed sentinel"
# The feature branch must be based on origin/master's tip.
if [[ -d "$REPO/.loom/worktrees/issue-700" ]]; then
    base=$(git -C "$REPO" rev-parse origin/master 2>/dev/null)
    head=$(git -C "$REPO/.loom/worktrees/issue-700" rev-parse HEAD 2>/dev/null)
    assert_eq "$head" "$base" "feature branch based on origin/master tip"
fi
rm -f "/tmp/defbranch-wt.$$"
cleanup_repo "$REPO"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
