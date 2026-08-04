#!/usr/bin/env bash
# test-worktree-remote-branch-tracking.sh — Tests for remote-branch tracking on
# worktree creation (#4823)
#
# Verifies that when `worktree.sh N` finds no LOCAL `refs/heads/feature/issue-N`
# but origin already has a pushed `refs/remotes/origin/feature/issue-N` (e.g. an
# existing PR branch from a prior Builder/Doctor cycle), it fetches and creates
# the local branch tracking that remote ref — instead of silently branching a
# fresh copy off origin/$DEFAULT_BRANCH, which diverges from the real PR history
# and risks a PR-clobbering force-push or a diff against the wrong base.
#
# Coverage:
#   1. Remote branch exists, no local branch: worktree.sh tracks origin/<branch>
#      (the worktree HEAD carries the PR branch's unique commit), not a fresh
#      branch off origin/main.
#   2. Remote branch exists AND has diverged from the current base (main
#      advanced past it after the PR branch was cut): worktree.sh still tracks
#      origin/<branch> (prefers the remote, per the issue's acceptance
#      criteria) and logs a line noting the divergence.
#
# Pattern follows test-worktree-base-override.sh: throwaway bare origin + repo
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

# Build a throwaway repo with an origin/main ref plus a pushed PR branch
# (feature/issue-77) carrying one unique commit — but with NO local copy of
# that branch (deleted after push), simulating a fresh clone / cleaned-up
# worktree that never had it locally. When advance_main=true, origin/main is
# advanced with an extra commit AFTER the PR branch was cut, so the PR branch
# no longer contains all of main's history (the divergence case). Echoes the
# working-tree path.
setup_repo() {
    local name="${1:-remoterepo}"
    local advance_main="${2:-false}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtremote.XXXXXX)
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

        # Create the "existing PR" branch, push it, then delete the LOCAL
        # copy (origin keeps it) — this is the scenario the fix targets.
        git checkout -q -b feature/issue-77
        echo "pr-artifact" > pr-file.txt
        git add pr-file.txt
        git commit -q -m "pr: add artifact from existing PR branch"
        git push -q origin feature/issue-77
        git checkout -q main
        git branch -q -D feature/issue-77

        if [[ "$advance_main" == "true" ]]; then
            echo "later-main-work" > later-main.txt
            git add later-main.txt
            git commit -q -m "main: advance past the PR branch"
            git push -q origin main
        fi
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# --- Test 1: no local branch, remote PR branch exists -> tracks the remote ---
echo "Test 1: no local branch + origin/feature/issue-77 exists -> worktree tracks the remote branch"
REPO=$(setup_repo trackrepo false)
OUT_LOG="/tmp/wtremote-track.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -f "$REPO/.loom/worktrees/issue-77/pr-file.txt" ]]; then
    pass "worktree contains the PR branch's unique artifact (tracked origin/feature/issue-77, not a fresh branch off main)"
else
    fail "worktree is missing the PR branch's artifact (silently branched off main instead of tracking origin/feature/issue-77)"
fi
WT_HEAD=$(git -C "$REPO/.loom/worktrees/issue-77" rev-parse HEAD 2>/dev/null || echo "")
ORIGIN_HEAD=$(git -C "$REPO" rev-parse origin/feature/issue-77 2>/dev/null || echo "")
if [[ -n "$WT_HEAD" && "$WT_HEAD" == "$ORIGIN_HEAD" ]]; then
    pass "worktree HEAD equals origin/feature/issue-77's head commit"
else
    fail "worktree HEAD ($WT_HEAD) does not equal origin/feature/issue-77 ($ORIGIN_HEAD)"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 2: remote branch has diverged from the current base -> still tracks it, with a log line ---
echo ""
echo "Test 2: origin/feature/issue-77 diverged from origin/main -> still tracks the remote, logs divergence"
REPO=$(setup_repo divrepo true)
OUT_LOG="/tmp/wtremote-div.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -f "$REPO/.loom/worktrees/issue-77/pr-file.txt" ]]; then
    pass "diverged case: worktree still contains the PR branch's artifact (prefers the remote)"
else
    fail "diverged case: worktree is missing the PR branch's artifact"
fi
if [[ -f "$REPO/.loom/worktrees/issue-77/later-main.txt" ]]; then
    fail "diverged case: worktree unexpectedly contains main's post-divergence commit (branched off main instead of the PR branch)"
else
    pass "diverged case: worktree does NOT contain main's post-divergence commit"
fi
if grep -qi "diverged" "$OUT_LOG"; then
    pass "diverged case: worktree.sh logs a line noting the divergence"
else
    fail "diverged case: no log line noting the divergence"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
