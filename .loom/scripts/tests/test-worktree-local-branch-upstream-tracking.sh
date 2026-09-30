#!/usr/bin/env bash
# test-worktree-local-branch-upstream-tracking.sh — Tests for correcting a
# pre-existing local branch's upstream on the worktree.sh reuse path (#6095)
#
# Regression coverage for the incident on #6086/PR #6093: a builder worktree's
# local `feature/issue-N` branch was checked out but its upstream tracking ref
# had somehow been set to `origin/main` instead of `origin/feature/issue-N`.
# A `git pull --ff-only` then silently fast-forwarded the local branch to
# main's tip, diverging it from the PR's actual head.
#
# Root cause: the "Check if branch already exists" reuse path in worktree.sh
# (`if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"`) reused the
# existing local branch by name only — it never inspected or corrected that
# branch's upstream tracking ref before handing it to `git worktree add`.
# This is unlike the sibling "no local branch, but origin/$BRANCH_NAME
# exists" path (#4823, covered by test-worktree-remote-branch-tracking.sh),
# which explicitly creates the local branch tracking the remote one — that
# path only runs when the local branch does NOT already exist, so it never
# fired for a worktree whose local branch pre-existed from an earlier
# Builder pass.
#
# Coverage:
#   1. Local branch exists with its upstream mis-set to origin/main (and
#      origin/feature/issue-N has diverged, carrying a commit the local
#      branch doesn't have — matching the observed incident shape):
#      worktree.sh corrects the upstream to origin/feature/issue-N.
#   2. Local branch exists with NO upstream configured at all, and
#      origin/feature/issue-N exists: worktree.sh sets the upstream to
#      origin/feature/issue-N (does not leave it unset).
#   3. Local branch exists, already correctly tracking
#      origin/feature/issue-N: worktree.sh leaves it unchanged (no-op,
#      regression guard against accidentally breaking the correct case).
#   4. Local branch exists, was NEVER pushed (no origin/feature/issue-N at
#      all): worktree.sh leaves tracking exactly as it was (does not
#      fabricate an upstream that doesn't exist).
#
# Pattern follows test-worktree-remote-branch-tracking.sh: throwaway bare
# origin + repo in a mktemp dir, copy worktree.sh + lib/, run, assert on
# `git branch -vv` / `@{u}` output.
#
# SINCE #8195 SLICE 9 (epic #7810) this correction is `loom-daemon
# worktree-upstream --arm local-branch`, sharing one implementation with the
# registered-worktree fast path that #6257 had to re-implement by hand (see
# test-worktree-existing-dir-drift-check.sh). Every assertion below is
# unchanged from the shell implementation — that is what makes them the
# equivalence evidence for retiring it — so the binary is pinned via
# loom_test_require_daemon_bin and this suite FAILS rather than skips when
# there is none. Without a binary the correction does not happen at all (a
# correct, documented degradation: the pre-#6095 behaviour), which would leave
# Tests 1 and 2 asserting on something nothing ran.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
# #8195 slice 14 folded this arm's correction into `worktree-branch-reuse`,
# which calls `worktree_cli::upstream` IN-PROCESS: the shell's
# `_worktree_upstream_check local-branch` call site is gone, so the subcommand
# this suite reaches through worktree.sh is the reuse arm, not `worktree-upstream`
# directly. Both are named so a binary missing either is diagnosed at once.
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-upstream" "worktree-branch-reuse"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Build a throwaway repo with:
#   - a local feature/issue-<n> branch (simulating one that already existed
#     from an earlier worktree.sh/Builder pass)
#   - that same branch pushed to origin
#   - origin/main advanced afterward (so origin/feature/issue-<n> has
#     diverged from origin/main, matching the observed incident shape)
# Echoes the repo path.
setup_repo() {
    local name="$1"
    local issue="$2"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtupstream.XXXXXX)
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

        git checkout -q -b "feature/issue-$issue"
        echo "builder-work" > work.txt
        git add work.txt
        git commit -q -m "builder work"
        git push -q origin "feature/issue-$issue"

        # Advance origin/main so origin/feature/issue-<n> has diverged from
        # it — the exact shape observed in the incident.
        git checkout -q main
        echo "later-main-work" > later-main.txt
        git add later-main.txt
        git commit -q -m "main: advance past the branch"
        git push -q origin main

        # Baseline: correctly tracking origin/feature/issue-<n> (a plain
        # `git push` without `-u` leaves the local branch with NO upstream
        # at all, so this must be set explicitly). Each test then mutates
        # this baseline as needed (mis-set it, unset it, or leave it).
        git checkout -q "feature/issue-$issue"
        git branch -q --set-upstream-to="origin/feature/issue-$issue" "feature/issue-$issue"
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# --- Test 1: local branch's upstream mis-set to origin/main -> corrected ---
echo "Test 1: local branch upstream mis-set to origin/main -> worktree.sh corrects it to origin/feature/issue-N"
REPO=$(setup_repo mainrepo 201)
(
    cd "$REPO"
    git branch --set-upstream-to=origin/main "feature/issue-201"
    git checkout -q main
)
OUT_LOG="/tmp/wtupstream-mismain.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 201 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
WT_UPSTREAM=$(git -C "$REPO/.loom/worktrees/issue-201" rev-parse --abbrev-ref 'feature/issue-201@{u}' 2>/dev/null || echo "")
if [[ "$WT_UPSTREAM" == "origin/feature/issue-201" ]]; then
    pass "worktree's local branch now tracks origin/feature/issue-201 (was origin/main)"
else
    fail "worktree's local branch tracks '$WT_UPSTREAM', expected 'origin/feature/issue-201'"
fi
# Also confirm the worktree HEAD is the branch's own tip, not main's tip
# (i.e. no pull happened yet — this asserts the tracking fix, not a pull).
WT_HEAD=$(git -C "$REPO/.loom/worktrees/issue-201" rev-parse HEAD 2>/dev/null || echo "")
BRANCH_TIP=$(git -C "$REPO" rev-parse origin/feature/issue-201 2>/dev/null || echo "")
if [[ -n "$WT_HEAD" && "$WT_HEAD" == "$BRANCH_TIP" ]]; then
    pass "worktree HEAD is the branch's own tip (not main's tip)"
else
    fail "worktree HEAD ($WT_HEAD) is not the branch's own tip ($BRANCH_TIP)"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 2: local branch has no upstream at all, remote branch exists -> set ---
echo ""
echo "Test 2: local branch has no upstream configured, origin/feature/issue-N exists -> worktree.sh sets it"
REPO=$(setup_repo norepo 202)
(
    cd "$REPO"
    git branch --unset-upstream "feature/issue-202" 2>/dev/null || true
    git checkout -q main
)
OUT_LOG="/tmp/wtupstream-none.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 202 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
WT_UPSTREAM=$(git -C "$REPO/.loom/worktrees/issue-202" rev-parse --abbrev-ref 'feature/issue-202@{u}' 2>/dev/null || echo "")
if [[ "$WT_UPSTREAM" == "origin/feature/issue-202" ]]; then
    pass "worktree's local branch now tracks origin/feature/issue-202 (was unset)"
else
    fail "worktree's local branch tracks '$WT_UPSTREAM', expected 'origin/feature/issue-202'"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 3: local branch already correctly tracking -> left unchanged ---
echo ""
echo "Test 3: local branch already tracks origin/feature/issue-N -> worktree.sh leaves it unchanged"
REPO=$(setup_repo okrepo 203)
(
    cd "$REPO"
    git checkout -q main
)
OUT_LOG="/tmp/wtupstream-ok.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 203 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
WT_UPSTREAM=$(git -C "$REPO/.loom/worktrees/issue-203" rev-parse --abbrev-ref 'feature/issue-203@{u}' 2>/dev/null || echo "")
if [[ "$WT_UPSTREAM" == "origin/feature/issue-203" ]]; then
    pass "worktree's local branch still tracks origin/feature/issue-203 (already-correct case unaffected)"
else
    fail "worktree's local branch tracks '$WT_UPSTREAM', expected unchanged 'origin/feature/issue-203'"
fi
if grep -qi "correcting to\|has no upstream" "$OUT_LOG"; then
    fail "worktree.sh printed a correction message even though tracking was already correct"
else
    pass "worktree.sh did not print a correction message for the already-correct case"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 4: local branch never pushed -> tracking left as-is (no fabrication) ---
echo ""
echo "Test 4: local branch never pushed to origin -> worktree.sh does not fabricate an upstream"
TMP4=$(mktemp -d /tmp/loom-wtupstream.XXXXXX)
git init -q -b main "$TMP4/origin.git" --bare
git init -q -b main "$TMP4/neverpushed"
(
    cd "$TMP4/neverpushed"
    git config user.email t@t
    git config user.name t
    git commit --allow-empty -q -m init
    git remote add origin "$TMP4/origin.git"
    git push -q origin main
    mkdir -p .loom/scripts/lib .loom/hooks
    cp "$WORKTREE_SH" .loom/scripts/worktree.sh
    if [[ -d "$SCRIPTS_DIR/lib" ]]; then
        cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
    fi
    chmod +x .loom/scripts/worktree.sh
    git checkout -q -b feature/issue-204
    echo local-only > local.txt
    git add local.txt
    git commit -q -m "local only, never pushed"
    git checkout -q main
)
OUT_LOG="/tmp/wtupstream-neverpushed.$$"
(
    cd "$TMP4/neverpushed"
    ./.loom/scripts/worktree.sh 204 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if git -C "$TMP4/neverpushed/.loom/worktrees/issue-204" rev-parse --abbrev-ref 'feature/issue-204@{u}' >/dev/null 2>&1; then
    WT_UPSTREAM=$(git -C "$TMP4/neverpushed/.loom/worktrees/issue-204" rev-parse --abbrev-ref 'feature/issue-204@{u}')
    fail "worktree's local branch unexpectedly has an upstream ('$WT_UPSTREAM') fabricated for a branch that was never pushed"
else
    pass "worktree's local branch correctly has no upstream (never fabricated one for an unpushed branch)"
fi
rm -rf "$TMP4"
rm -f "$OUT_LOG"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
