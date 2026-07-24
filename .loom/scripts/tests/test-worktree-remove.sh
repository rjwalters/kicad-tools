#!/usr/bin/env bash
# test-worktree-remove.sh - Tests for the `worktree.sh remove <N>` verb (#3769)
#
# Verifies the operator-facing single-worktree removal verb:
#   1. Removing a .loom-managed worktree succeeds (dir gone, unregistered,
#      local branch deleted).
#   2. Removing a worktree lacking the .loom-managed sentinel is refused
#      (dir untouched, non-zero exit, clear stderr message).
#   3. Removing a non-existent issue worktree is an idempotent no-op success.
#   4. --keep-branch leaves the local branch intact after removal.
#   5. Running `remove` from a shell whose cwd is inside the target worktree
#      completes successfully (script cd's out first — no shell corruption).
#
# Follows the throwaway-repo harness pattern in test-worktree-sentinel.sh:
# a bare origin remote + a working repo, with worktree.sh + its lib/ helpers
# copied into a temp tree, then the script driven directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# --- Throwaway repo setup ---------------------------------------------------
TMP=$(mktemp -d /tmp/loom-remove-test.XXXXXX)
trap 'rm -rf "$TMP"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

mkdir -p .loom/scripts/lib
cp "$WORKTREE_SH" .loom/scripts/worktree.sh
if [[ -d "$SCRIPTS_DIR/lib" ]]; then
    cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
fi
chmod +x .loom/scripts/worktree.sh

REPO="$TMP/repo"

# Helper: create a fresh managed worktree for an issue number.
make_worktree() {
    local n="$1"
    cd "$REPO"
    ./.loom/scripts/worktree.sh "$n" >/dev/null 2>&1
}

# --- Test 1: remove a managed worktree succeeds -----------------------------
echo "Test 1: remove a .loom-managed worktree (dir gone, unregistered, branch deleted)"
make_worktree 101
cd "$REPO"
if ./.loom/scripts/worktree.sh remove 101 >/tmp/rm-out.$$ 2>&1; then
    if [[ ! -d ".loom/worktrees/issue-101" ]]; then
        pass "worktree directory removed"
    else
        fail "worktree directory still present after remove"
    fi
    if ! git worktree list --porcelain | grep -q "issue-101"; then
        pass "worktree no longer registered with git"
    else
        fail "worktree still registered with git"
    fi
    if ! git show-ref --verify --quiet "refs/heads/feature/issue-101"; then
        pass "local branch feature/issue-101 deleted"
    else
        fail "local branch feature/issue-101 still exists"
    fi
else
    fail "remove exited non-zero for a managed worktree (see /tmp/rm-out.$$)"
fi

# --- Test 2: remove a worktree lacking the sentinel is refused --------------
echo ""
echo "Test 2: remove refuses a worktree lacking the .loom-managed sentinel"
make_worktree 102
cd "$REPO"
rm -f ".loom/worktrees/issue-102/.loom-managed"
if ./.loom/scripts/worktree.sh remove 102 >/tmp/rm-out2.$$ 2>&1; then
    fail "remove succeeded on an unmanaged worktree (should have refused)"
else
    pass "remove exited non-zero for an unmanaged worktree"
fi
if [[ -d ".loom/worktrees/issue-102" ]]; then
    pass "unmanaged worktree directory left untouched"
else
    fail "unmanaged worktree directory was removed despite missing sentinel"
fi
if grep -q "refusing to remove" /tmp/rm-out2.$$ 2>/dev/null; then
    pass "refusal message mentions 'refusing to remove'"
else
    fail "no clear refusal message emitted"
fi

# --- Test 3: remove a non-existent worktree is an idempotent no-op ----------
echo ""
echo "Test 3: remove a non-existent issue worktree is an idempotent no-op success"
cd "$REPO"
if ./.loom/scripts/worktree.sh remove 999999 >/tmp/rm-out3.$$ 2>&1; then
    pass "remove exited 0 for a non-existent worktree"
else
    fail "remove exited non-zero for a non-existent worktree (should be no-op success)"
fi
if grep -qi "nothing to remove" /tmp/rm-out3.$$ 2>/dev/null; then
    pass "no-op message emitted ('nothing to remove')"
else
    fail "no clear no-op message emitted"
fi

# --- Test 4: --keep-branch leaves the branch intact -------------------------
echo ""
echo "Test 4: --keep-branch removes the worktree but keeps the local branch"
make_worktree 103
cd "$REPO"
if ./.loom/scripts/worktree.sh remove 103 --keep-branch >/tmp/rm-out4.$$ 2>&1; then
    if [[ ! -d ".loom/worktrees/issue-103" ]]; then
        pass "worktree directory removed with --keep-branch"
    else
        fail "worktree directory still present after remove --keep-branch"
    fi
    if git show-ref --verify --quiet "refs/heads/feature/issue-103"; then
        pass "local branch feature/issue-103 preserved by --keep-branch"
    else
        fail "local branch feature/issue-103 was deleted despite --keep-branch"
    fi
else
    fail "remove --keep-branch exited non-zero (see /tmp/rm-out4.$$)"
fi

# --- Test 5: remove from inside the worktree does not corrupt the shell ------
echo ""
echo "Test 5: remove works when cwd is inside the target worktree"
make_worktree 104
# Drive the script from inside the worktree in a subshell so this harness's own
# cwd is not affected.
if ( cd "$REPO/.loom/worktrees/issue-104" && \
     "$REPO/.loom/scripts/worktree.sh" remove 104 ) >/tmp/rm-out5.$$ 2>&1; then
    cd "$REPO"
    if [[ ! -d ".loom/worktrees/issue-104" ]]; then
        pass "worktree removed even though cwd was inside it"
    else
        fail "worktree still present after in-worktree remove"
    fi
    if grep -qi "working directory was inside" /tmp/rm-out5.$$ 2>/dev/null; then
        pass "emits the CWD-hop advisory when removing from inside"
    else
        fail "no CWD-hop advisory emitted for in-worktree removal"
    fi
else
    cd "$REPO"
    fail "in-worktree remove exited non-zero (see /tmp/rm-out5.$$)"
fi

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
