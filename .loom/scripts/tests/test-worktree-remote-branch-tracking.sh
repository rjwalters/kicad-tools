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
# Since #8280 this file also covers the SIBLING reuse arm's divergence warning.
# `worktree.sh` has two branch-reuse arms — "no local ref, origin has one"
# (above) and "a local ref already exists" — and only the first warned when the
# reused branch did not contain all of the base ref's history. A leftover local
# `feature/issue-N` from an earlier slice was therefore reused in SILENCE: the
# incident on #8280 produced a worktree tens of commits behind the base, sitting
# on a merged PR's branch, and a PR that re-proposed already-merged code with no
# CI run against it. The two arms are supposed to behave symmetrically on this
# point, so both warnings are asserted side by side HERE rather than in separate
# files — the symmetry is the property under test, and it is only checkable when
# the two cases can be read together.
#
# Coverage:
#   1. Remote branch exists, no local branch: worktree.sh tracks origin/<branch>
#      (the worktree HEAD carries the PR branch's unique commit), not a fresh
#      branch off origin/main.
#   2. Remote branch exists AND has diverged from the current base (main
#      advanced past it after the PR branch was cut): worktree.sh still tracks
#      origin/<branch> (prefers the remote, per the issue's acceptance
#      criteria) and logs a line noting the divergence.
#   3. (#8280) A LOCAL branch already exists and the base has advanced past it:
#      the local-reuse arm warns that the branch lacks the base ref's history.
#      This is the case that was silent before #8280.
#   4. (#8280) A LOCAL branch already exists and DOES contain all of the base
#      ref's history: the arm stays silent. A warning that fires on every reuse
#      would be noise, and noise is what let the real divergence hide — so the
#      negative case is as load-bearing as the positive one.
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

# #8280 fixture: build a repo where the LOCAL `feature/issue-<issue>` branch
# still exists (the state a host is left in after building an earlier slice —
# the normal case, not an exotic one) and was pushed to origin.
#
# When behind=true, origin/main is advanced AFTER the branch was cut, so the
# local branch no longer contains all of origin/main's history — the divergence
# the local-reuse arm must announce. When behind=false the branch is cut from
# main's tip only after main has advanced, so it DOES contain all of it and the
# arm must stay silent.
#
# The working tree is left on main, because `git worktree add` refuses a branch
# that is checked out elsewhere. Echoes the working-tree path.
setup_local_branch_repo() {
    local name="$1"
    local issue="$2"
    local behind="$3"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtlocal.XXXXXX)
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

        if [[ "$behind" != "true" ]]; then
            # Advance main FIRST, so the branch cut below contains all of it.
            echo "earlier-main-work" > earlier-main.txt
            git add earlier-main.txt
            git commit -q -m "main: advance before the branch is cut"
            git push -q origin main
        fi

        # The leftover local branch. Unlike setup_repo above, the local copy is
        # deliberately KEPT — that is what selects the local-reuse arm.
        git checkout -q -b "feature/issue-$issue"
        echo "slice-work" > slice.txt
        git add slice.txt
        git commit -q -m "earlier slice's work"
        git push -q origin "feature/issue-$issue"
        git checkout -q main

        if [[ "$behind" == "true" ]]; then
            # Advance main AFTER the branch was cut: the branch is now missing
            # this commit, i.e. diverged from the base ref.
            echo "later-main-work" > later-main.txt
            git add later-main.txt
            git commit -q -m "main: advance past the branch"
            git push -q origin main
        fi
    )
    echo "$tmp/$name"
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

# --- Test 3 (#8280): local branch exists and the base advanced past it -> warn ---
echo ""
echo "Test 3 (#8280): local feature/issue-88 lacks origin/main's history -> local-reuse arm warns about the divergence"
REPO=$(setup_local_branch_repo localdivrepo 88 true)
OUT_LOG="/tmp/wtlocal-div.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 88 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
# Confirm the local-reuse arm is the arm that actually ran — otherwise a passing
# divergence assertion could be the sibling remote arm's warning instead.
if grep -q "already exists - reusing it" "$OUT_LOG"; then
    pass "local-reuse arm ran (reused the pre-existing local branch)"
else
    fail "local-reuse arm did not run — this fixture no longer exercises the arm under test"
    cat "$OUT_LOG"
fi
if grep -q "has diverged from" "$OUT_LOG" && grep -q "reusing it as-is" "$OUT_LOG"; then
    pass "local-reuse arm warns that the branch does not contain all of the base ref's history"
else
    fail "no divergence warning from the local-reuse arm (the #8280 silence has regressed)"
    cat "$OUT_LOG"
fi
# The reuse itself is unchanged — this fix makes it visible, it does not block it.
if [[ -f "$REPO/.loom/worktrees/issue-88/slice.txt" ]]; then
    pass "warning is advisory: the worktree still reuses the branch as-is"
else
    fail "worktree does not carry the reused branch's work — the warning changed behaviour, not just visibility"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 4 (#8280): local branch contains all of the base's history -> silent ---
echo ""
echo "Test 4 (#8280): local feature/issue-89 contains all of origin/main's history -> no divergence warning"
REPO=$(setup_local_branch_repo localokrepo 89 false)
OUT_LOG="/tmp/wtlocal-ok.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 89 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if grep -q "already exists - reusing it" "$OUT_LOG"; then
    pass "up-to-date case: local-reuse arm ran"
else
    fail "up-to-date case: local-reuse arm did not run — fixture no longer exercises the arm under test"
    cat "$OUT_LOG"
fi
if grep -q "has diverged from" "$OUT_LOG"; then
    fail "up-to-date case: divergence warning fired for a branch that is NOT behind the base (false positive)"
    cat "$OUT_LOG"
else
    pass "up-to-date case: no divergence warning (the check discriminates, it does not warn unconditionally)"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
