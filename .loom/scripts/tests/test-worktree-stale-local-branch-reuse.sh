#!/usr/bin/env bash
# test-worktree-stale-local-branch-reuse.sh — Tests for #13056.
#
# `worktree.sh N` reusing a LOCAL feature/issue-N that is far behind its base
# used to hand it back with only a generic "has diverged" warning. The reuse
# arm (`loom-daemon worktree-branch-reuse`, branch_reuse.rs step 4) now:
#   (a) resets a branch with 0 commits of its own to the base ref;
#   (b) keeps a branch with unique commits, warning with ahead/behind/age;
#   (c) leaves a branch that is not behind untouched.
# Case (a) is driven through `--base dev` with the branch sitting at main's
# tip: a 0-ahead branch behind the DEFAULT branch is refused as already landed
# (#8280) before this step runs, so the degenerate-tip shape is the reachable
# one. Pattern: test-worktree-stale-merged-branch.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-branch-reuse"

source "$SCRIPT_DIR/lib/assert.sh"

# Throwaway repo: origin/main, origin/dev (main + 2 commits), a local
# feature/issue-79 whose placement the caller sets afterwards. Echoes the path.
setup_repo() {
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtstalelocalbranch.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/repo"
    (
        cd "$tmp/repo"
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
        git checkout -q -b dev
        git commit --allow-empty -q -m dev1
        git commit --allow-empty -q -m dev2
        git push -q origin dev
        git checkout -q main
        git branch -q feature/issue-79 main
    )
    echo "$tmp/repo"
}

cleanup_repo() { [[ -z "${1:-}" ]] || rm -rf "$(dirname "$1")"; }

# --- (a) stale, 0 commits of its own -> reset to base ---
echo "Test 1: stale local branch with no work of its own is reset to the base"
REPO=$(setup_repo)
OUT_LOG="/tmp/wtstalelocal-a.$$"
(cd "$REPO" && ./.loom/scripts/worktree.sh 79 --base dev >"$OUT_LOG" 2>&1) || { fail "worktree.sh failed"; cat "$OUT_LOG"; }
assert_eq "$(git -C "$REPO" rev-parse origin/dev)" "$(git -C "$REPO" rev-parse feature/issue-79)" \
    "feature/issue-79 was reset to origin/dev"
assert_contains "$(cat "$OUT_LOG")" "no work of its own" "output explains the reset"
cleanup_repo "$REPO"; rm -f "$OUT_LOG"

# --- (b) stale, with a unique commit -> kept, loud warning ---
echo ""
echo "Test 2: stale local branch with a unique commit is kept and warns with ahead/behind/age"
REPO=$(setup_repo)
OUT_LOG="/tmp/wtstalelocal-b.$$"
(
    cd "$REPO"
    git checkout -q feature/issue-79
    echo mine > mine.txt && git add mine.txt
    GIT_COMMITTER_DATE="2020-01-01T00:00:00" GIT_AUTHOR_DATE="2020-01-01T00:00:00" git commit -q -m "my work"
    git checkout -q main
)
TIP=$(git -C "$REPO" rev-parse feature/issue-79)
(cd "$REPO" && ./.loom/scripts/worktree.sh 79 --base dev >"$OUT_LOG" 2>&1) || { fail "worktree.sh failed"; cat "$OUT_LOG"; }
assert_eq "$TIP" "$(git -C "$REPO" rev-parse feature/issue-79)" "unique work was not discarded"
assert_contains "$(cat "$OUT_LOG")" "1 commit(s) ahead, 2 behind" "warning names ahead/behind"
assert_contains "$(cat "$OUT_LOG")" "day(s) old" "warning names the merge-base age"
cleanup_repo "$REPO"; rm -f "$OUT_LOG"

# --- (c) not behind -> unchanged ---
echo ""
echo "Test 3: a branch that is not behind its base is left unchanged"
REPO=$(setup_repo)
OUT_LOG="/tmp/wtstalelocal-c.$$"
(
    cd "$REPO"
    git checkout -q -b feature/issue-79-tmp origin/dev
    echo mine > mine.txt && git add mine.txt && git commit -q -m "my work"
    git checkout -q main
    git branch -q -f feature/issue-79 feature/issue-79-tmp
    git branch -q -D feature/issue-79-tmp
)
TIP=$(git -C "$REPO" rev-parse feature/issue-79)
(cd "$REPO" && ./.loom/scripts/worktree.sh 79 --base dev >"$OUT_LOG" 2>&1) || { fail "worktree.sh failed"; cat "$OUT_LOG"; }
assert_eq "$TIP" "$(git -C "$REPO" rev-parse feature/issue-79)" "branch tip unchanged"
assert_not_contains "$(cat "$OUT_LOG")" "behind" "no staleness message"
cleanup_repo "$REPO"; rm -f "$OUT_LOG"

echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
