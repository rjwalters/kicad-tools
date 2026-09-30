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
#   6. A worktree with uncommitted changes is REFUSED (#4449): non-zero exit,
#      worktree + work intact, message lists what it found and how to proceed.
#   7. The same worktree removes cleanly with an explicit --force.
#   8. Loom runtime markers (.loom-managed et al) do NOT count as uncommitted
#      work — otherwise the guard would refuse every managed worktree in a repo
#      whose .gitignore predates #3838.
#   9. A staged-only (index) change also triggers the refusal.
#
# Follows the throwaway-repo harness pattern in test-worktree-sentinel.sh:
# a bare origin remote + a working repo, with worktree.sh + its lib/ helpers
# copied into a temp tree, then the script driven directly.
#
# Needs a BUILT `loom-daemon` since #8195 slice 3: `worktree.sh remove` is now
# a thin stub over `loom-daemon worktree-remove`. Every assertion below is
# unchanged from the shell implementation — running them against the port is
# the equivalence evidence — so this suite moved to the "Native Port Suites" CI
# job, which builds the binary, and FAILS rather than skips without one.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-worktree-remove.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-remove"

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

# --- Test 6: a dirty worktree is refused (#4449) -----------------------------
# The data-loss precedent: `git worktree remove --force` discards the working
# tree unconditionally, so `remove` must refuse rather than destroy work.
echo ""
echo "Test 6: remove refuses a worktree with uncommitted changes (#4449)"
make_worktree 105
cd "$REPO"
echo "an uncommitted fix that must not be destroyed" > ".loom/worktrees/issue-105/wip.txt"
if ./.loom/scripts/worktree.sh remove 105 >/tmp/rm-out6.$$ 2>&1; then
    fail "remove succeeded on a dirty worktree (should have refused)"
else
    pass "remove exited non-zero for a dirty worktree"
fi
if [[ -d ".loom/worktrees/issue-105" ]]; then
    pass "dirty worktree directory left untouched"
else
    fail "dirty worktree directory was removed"
fi
if [[ -f ".loom/worktrees/issue-105/wip.txt" ]]; then
    pass "uncommitted work survived the refused removal"
else
    fail "uncommitted work was destroyed"
fi
if grep -q "Refusing to remove" /tmp/rm-out6.$$ 2>/dev/null; then
    pass "refusal message mentions 'Refusing to remove'"
else
    fail "no clear refusal message emitted"
fi
if grep -q "wip.txt" /tmp/rm-out6.$$ 2>/dev/null; then
    pass "refusal lists the uncommitted path it found"
else
    fail "refusal did not list what it found"
fi
# The refusal must tell the caller how to proceed (AC: commit / patch / stash /
# force), not just say no.
remedies=0
for needle in "commit" "diff HEAD" "stash" "--force"; do
    grep -qi -- "$needle" /tmp/rm-out6.$$ 2>/dev/null && remedies=$((remedies + 1))
done
if [[ "$remedies" -eq 4 ]]; then
    pass "refusal explains all four ways to proceed (commit/patch/stash/--force)"
else
    fail "refusal is missing remediation guidance ($remedies/4 found)"
fi
# --json mode must still emit a single parseable refusal document on stdout.
if ./.loom/scripts/worktree.sh remove 105 --json >/tmp/rm-out6b.$$ 2>/dev/null; then
    fail "remove --json succeeded on a dirty worktree (should have refused)"
else
    if grep -q '"success": false' /tmp/rm-out6b.$$ && grep -q '"removed": false' /tmp/rm-out6b.$$; then
        pass "--json refusal reports success=false, removed=false on stdout"
    else
        fail "--json refusal did not emit the expected JSON (see /tmp/rm-out6b.$$)"
    fi
fi

# --- Test 7: --force removes the dirty worktree ------------------------------
echo ""
echo "Test 7: remove --force discards uncommitted changes and removes the worktree"
cd "$REPO"
if ./.loom/scripts/worktree.sh remove 105 --force >/tmp/rm-out7.$$ 2>&1; then
    if [[ ! -d ".loom/worktrees/issue-105" ]]; then
        pass "dirty worktree removed with --force"
    else
        fail "dirty worktree still present after remove --force"
    fi
    if grep -qi "discarding them (--force)" /tmp/rm-out7.$$ 2>/dev/null; then
        pass "--force announces that it discarded the uncommitted changes"
    else
        fail "--force did not announce the discard"
    fi
else
    fail "remove --force exited non-zero on a dirty worktree (see /tmp/rm-out7.$$)"
fi

# --- Test 8: Loom runtime markers are not "uncommitted work" -----------------
# worktree.sh writes .loom-managed into every worktree it creates. A repo whose
# .gitignore predates #3838 does not ignore it, so a naive porcelain check would
# see every managed worktree as dirty and refuse ALL removals.
echo ""
echo "Test 8: Loom runtime markers alone do not trigger the dirty refusal"
make_worktree 106
cd "$REPO"
# Prove the marker really is visible to git in this harness (no .gitignore).
if git -C ".loom/worktrees/issue-106" status --porcelain --untracked-files=all | grep -q ".loom-managed"; then
    pass "precondition: .loom-managed is untracked+visible to git in this repo"
else
    echo "  (note: .loom-managed already ignored here — guard still exercised below)"
fi
touch ".loom/worktrees/issue-106/.loom-in-use" ".loom/worktrees/issue-106/.loom-checkpoint"
if ./.loom/scripts/worktree.sh remove 106 >/tmp/rm-out8.$$ 2>&1; then
    pass "remove succeeded with only Loom runtime markers present"
else
    fail "remove refused a worktree whose only 'changes' are Loom markers (see /tmp/rm-out8.$$)"
fi
if [[ ! -d ".loom/worktrees/issue-106" ]]; then
    pass "marker-only worktree was removed"
else
    fail "marker-only worktree still present"
fi

# --- Test 9: a staged-only change also triggers the refusal ------------------
echo ""
echo "Test 9: remove refuses a worktree with staged-but-uncommitted changes"
make_worktree 107
cd "$REPO"
echo "staged content" > ".loom/worktrees/issue-107/staged.txt"
git -C ".loom/worktrees/issue-107" add staged.txt
if ./.loom/scripts/worktree.sh remove 107 >/tmp/rm-out9.$$ 2>&1; then
    fail "remove succeeded on a worktree with staged changes (should have refused)"
else
    pass "remove exited non-zero for staged-but-uncommitted changes"
fi
if [[ -f ".loom/worktrees/issue-107/staged.txt" ]]; then
    pass "staged work survived the refused removal"
else
    fail "staged work was destroyed"
fi

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
