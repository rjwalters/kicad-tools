#!/usr/bin/env bash
# test-worktree-snapshot.sh - Tests for the `worktree.sh snapshot <N>` verb (#4778)
#
# Verifies the worktree-scoped WIP snapshot verb:
#   1. A snapshot captures tracked-file modifications WITHOUT touching
#      `git stash` (stash list identical before/after).
#   2. The patch file lands at the documented deterministic path
#      (<worktree-root>/.snapshots/issue-<N>-<timestamp>.patch).
#   3. `git apply` of the produced patch against a fresh checkout of the same
#      branch reproduces the original diff.
#   4. --include-untracked folds untracked files into the patch; without the
#      flag, untracked files are NOT captured.
#   5. A worktree with no uncommitted changes still succeeds (empty patch,
#      not an error).
#   6. snapshot on a non-existent worktree fails cleanly.
#   7. LOOM_WORKTREE_ROOT override redirects the snapshot location along
#      with the worktree itself (not the hardcoded default).
#   8. --json emits a single parseable success document.
#   9. Loom runtime markers are excluded even with --include-untracked.
#
# Follows the throwaway-repo harness pattern in test-worktree-remove.sh: a
# bare origin remote + a working repo, with worktree.sh + its lib/ helpers
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
TMP=$(mktemp -d /tmp/loom-snapshot-test.XXXXXX)
trap 'rm -rf "$TMP"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
echo "tracked file" > tracked.txt
git add tracked.txt
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

# --- Test 1: snapshot captures tracked changes without touching git stash --
echo "Test 1: snapshot captures tracked-file changes without touching git stash"
make_worktree 201
cd "$REPO"
echo "modified content" >> ".loom/worktrees/issue-201/tracked.txt"
stash_before="$(git stash list)"
if ./.loom/scripts/worktree.sh snapshot 201 >/tmp/snap-out1.$$ 2>&1; then
    pass "snapshot exited 0 for a dirty worktree"
else
    fail "snapshot exited non-zero (see /tmp/snap-out1.$$)"
fi
stash_after="$(git stash list)"
if [[ "$stash_before" == "$stash_after" ]]; then
    pass "git stash list is unchanged by snapshot"
else
    fail "git stash list changed — snapshot touched the shared stash"
fi

# --- Test 2: patch lands at the documented deterministic path --------------
echo ""
echo "Test 2: patch file lands at <worktree-root>/.snapshots/issue-<N>-<ts>.patch"
patch1="$(find ".loom/worktrees/.snapshots" -name "issue-201-*.patch" 2>/dev/null | head -1)"
if [[ -n "$patch1" && -f "$patch1" ]]; then
    pass "patch file found at documented location: $patch1"
else
    fail "no patch file found under .loom/worktrees/.snapshots/issue-201-*.patch"
fi

# --- Test 3: git apply reproduces the original diff on a fresh checkout ----
echo ""
echo "Test 3: git apply of the snapshot reproduces the diff on a fresh checkout"
FRESH="$TMP/fresh-checkout"
git clone -q "$REPO" "$FRESH" >/dev/null 2>&1
# `git -C <dir> apply <path>` resolves a RELATIVE <path> against <dir>, not
# the shell's cwd — pass an absolute path so this check is robust regardless
# of where $patch1 was discovered relative to.
patch1_abs="$(cd "$(dirname "$patch1")" && pwd)/$(basename "$patch1")"
if [[ -n "$patch1" ]] && git -C "$FRESH" apply --check "$patch1_abs" 2>/tmp/snap-apply-check.$$; then
    pass "git apply --check succeeds against a fresh checkout"
    git -C "$FRESH" apply "$patch1_abs"
    if grep -q "modified content" "$FRESH/tracked.txt"; then
        pass "applied patch reproduces the original change"
    else
        fail "applied patch did not reproduce the original change"
    fi
else
    fail "git apply --check failed (see /tmp/snap-apply-check.$$)"
fi

# --- Test 4: --include-untracked folds in untracked files ------------------
echo ""
echo "Test 4: --include-untracked folds untracked files into the patch; default omits them"
make_worktree 202
cd "$REPO"
echo "brand new file" > ".loom/worktrees/issue-202/untracked.txt"
./.loom/scripts/worktree.sh snapshot 202 >/tmp/snap-out4a.$$ 2>&1
patch_no_untracked="$(find ".loom/worktrees/.snapshots" -name "issue-202-*.patch" 2>/dev/null | sort | tail -1)"
if [[ -n "$patch_no_untracked" ]] && ! grep -q "untracked.txt" "$patch_no_untracked"; then
    pass "default snapshot omits untracked files"
else
    fail "default snapshot unexpectedly included untracked files"
fi
sleep 1.1  # ensure a distinct timestamp for the second snapshot
./.loom/scripts/worktree.sh snapshot 202 --include-untracked >/tmp/snap-out4b.$$ 2>&1
patch_with_untracked="$(find ".loom/worktrees/.snapshots" -name "issue-202-*.patch" 2>/dev/null | sort | tail -1)"
if [[ -n "$patch_with_untracked" ]] && grep -q "untracked.txt" "$patch_with_untracked" && grep -q "brand new file" "$patch_with_untracked"; then
    pass "--include-untracked folds the untracked file's content into the patch"
else
    fail "--include-untracked did not include the untracked file"
fi
# The untracked file must remain untracked (not staged) after the snapshot.
if git -C ".loom/worktrees/issue-202" status --porcelain -- untracked.txt | grep -q '^??'; then
    pass "untracked file remains untracked after --include-untracked snapshot"
else
    fail "untracked file was left staged/tracked after snapshot"
fi

# --- Test 5: no uncommitted changes still succeeds (empty patch) -----------
echo ""
echo "Test 5: a clean worktree still succeeds, writing an empty snapshot"
make_worktree 203
cd "$REPO"
if ./.loom/scripts/worktree.sh snapshot 203 >/tmp/snap-out5.$$ 2>&1; then
    pass "snapshot exited 0 for a clean worktree"
else
    fail "snapshot exited non-zero for a clean worktree (see /tmp/snap-out5.$$)"
fi
patch5="$(find ".loom/worktrees/.snapshots" -name "issue-203-*.patch" 2>/dev/null | head -1)"
if [[ -n "$patch5" && -f "$patch5" ]]; then
    pass "empty snapshot patch file was still created"
else
    fail "no patch file created for a clean worktree"
fi

# --- Test 6: snapshot on a non-existent worktree fails cleanly -------------
echo ""
echo "Test 6: snapshot refuses a non-existent issue worktree"
cd "$REPO"
if ./.loom/scripts/worktree.sh snapshot 999999 >/tmp/snap-out6.$$ 2>&1; then
    fail "snapshot succeeded for a non-existent worktree (should have failed)"
else
    pass "snapshot exited non-zero for a non-existent worktree"
fi

# --- Test 7: LOOM_WORKTREE_ROOT override redirects the snapshot location ---
echo ""
echo "Test 7: LOOM_WORKTREE_ROOT override redirects .snapshots along with the worktree"
EXT_ROOT="$TMP/external-root"
mkdir -p "$EXT_ROOT"
cd "$REPO"
LOOM_WORKTREE_ROOT="$EXT_ROOT" ./.loom/scripts/worktree.sh 204 >/tmp/snap-out7-create.$$ 2>&1 || true
echo "override content" >> "$EXT_ROOT/$(basename "$REPO")/issue-204/tracked.txt"
if LOOM_WORKTREE_ROOT="$EXT_ROOT" ./.loom/scripts/worktree.sh snapshot 204 >/tmp/snap-out7.$$ 2>&1; then
    if find "$EXT_ROOT/$(basename "$REPO")/.snapshots" -name "issue-204-*.patch" 2>/dev/null | grep -q .; then
        pass "snapshot under LOOM_WORKTREE_ROOT lands under the overridden root, not the default"
    else
        fail "no snapshot found under the overridden LOOM_WORKTREE_ROOT"
    fi
    if [[ ! -d ".loom/worktrees/.snapshots" ]] || ! find ".loom/worktrees/.snapshots" -name "issue-204-*.patch" 2>/dev/null | grep -q .; then
        pass "no stray snapshot written to the default .loom/worktrees/.snapshots path"
    else
        fail "snapshot leaked into the default path despite LOOM_WORKTREE_ROOT override"
    fi
else
    fail "snapshot failed under LOOM_WORKTREE_ROOT override (see /tmp/snap-out7.$$)"
fi

# --- Test 8: --json emits a single parseable success document --------------
echo ""
echo "Test 8: snapshot --json emits a single parseable JSON document"
make_worktree 205
cd "$REPO"
echo "json test" >> ".loom/worktrees/issue-205/tracked.txt"
if ./.loom/scripts/worktree.sh snapshot 205 --json >/tmp/snap-out8.$$ 2>/tmp/snap-err8.$$; then
    if grep -q '"success": true' /tmp/snap-out8.$$ && grep -q '"hasChanges": true' /tmp/snap-out8.$$ && grep -q '"patchPath"' /tmp/snap-out8.$$; then
        pass "--json emits success=true, hasChanges=true, patchPath on stdout"
    else
        fail "--json output missing expected fields (see /tmp/snap-out8.$$)"
    fi
else
    fail "snapshot --json exited non-zero (see /tmp/snap-out8.$$ /tmp/snap-err8.$$)"
fi

# --- Test 9: Loom runtime markers are excluded even with --include-untracked
echo ""
echo "Test 9: Loom runtime markers are never captured, even with --include-untracked"
make_worktree 206
cd "$REPO"
touch ".loom/worktrees/issue-206/.loom-in-use" ".loom/worktrees/issue-206/.loom-checkpoint"
echo "real wip" > ".loom/worktrees/issue-206/real-wip.txt"
if ./.loom/scripts/worktree.sh snapshot 206 --include-untracked >/tmp/snap-out9.$$ 2>&1; then
    patch9="$(find ".loom/worktrees/.snapshots" -name "issue-206-*.patch" 2>/dev/null | head -1)"
    if [[ -n "$patch9" ]] && grep -q "real-wip.txt" "$patch9" && ! grep -q "\.loom-managed" "$patch9" && ! grep -q "\.loom-in-use" "$patch9" && ! grep -q "\.loom-checkpoint" "$patch9"; then
        pass "runtime markers excluded from the patch; real WIP included"
    else
        fail "runtime markers leaked into the patch, or real WIP was missing (see $patch9)"
    fi
else
    fail "snapshot --include-untracked exited non-zero (see /tmp/snap-out9.$$)"
fi

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
