#!/usr/bin/env bash
# test-worktree-stale-merged-branch.sh — Tests for #5657.
#
# `worktree.sh N`, when no LOCAL branch exists for feature/issue-N but
# `refs/remotes/origin/feature/issue-N` does, used to ALWAYS reuse the remote
# branch as the worktree base — even when that branch's current tip is
# already the head of an already-MERGED pull request (i.e. it was already
# squash-merged and is semantically obsolete). This happens whenever the
# target repo has forge auto-delete-head-branches disabled (or any other path
# that leaves a merged branch's ref on origin), and matters most with the
# partial-increment (#3667/#3599) slice convention where the SAME branch name
# feature/issue-N is reused across an issue's slices: the next slice's
# worktree would silently be built on top of already-merged, now-conflicting
# history, producing a CONFLICTING PR with zero CI runs.
#
# This suite verifies the reuse path now calls the existing
# `_worktree_merged_pr_head_sha` helper (already wired into the removal path,
# #4889) BEFORE reusing the remote branch:
#   1. origin/feature/issue-N's tip IS the head of an already-merged PR ->
#      the stale remote branch is skipped; worktree.sh creates a fresh branch
#      from the base ref instead, with an actionable message naming the PR.
#   2. origin/feature/issue-N's tip is NOT the head of any merged PR (the
#      #4823 in-flight case) -> continues to reuse it exactly as before.
#   3. The forge lookup is unavailable (no working `gh`) -> fails open to the
#      pre-existing reuse behavior, never blocking worktree creation.
#
# #8280 added Tests 4-5 below: the SIBLING "a LOCAL ref already exists" arm
# never had this check at all — a stale local feature/issue-N left from an
# earlier slice was reused even after its PR had already merged, producing a
# worktree tens of commits behind main on a merged PR's branch, and a PR that
# re-proposed already-merged code with no CI run against it (a surviving
# local ref is the NORMAL state on a host that built the earlier slice, which
# is exactly the partial-increment case #5657 targets). Unlike Test 1, the
# local arm cannot silently fall through to a fresh branch on `landed` — the
# name is already taken locally — so it must refuse outright instead:
#   4. LOCAL feature/issue-N's tip IS the head of an already-merged PR ->
#      worktree.sh refuses (nonzero exit, no worktree created) rather than
#      hand back a worktree on it.
#   5. The forge lookup is unavailable for a LOCAL branch -> fails open to
#      reuse, the same contract as Test 3's remote-arm case.
#
# Companion to test-worktree-remote-branch-tracking.sh (#4823, must keep
# passing unmodified) — follows the same throwaway-bare-origin harness, plus
# the fake-forge-binary pattern from test-worktree-remove-squash-merge.sh
# (#4889).

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

# Build a throwaway repo with an origin/main ref plus a pushed
# feature/issue-77 branch carrying one unique commit — but with NO local copy
# of that branch (deleted after push), simulating a fresh clone / cleaned-up
# worktree that never had it locally. Echoes the working-tree path.
setup_repo() {
    local name="${1:-stalerepo}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtstale.XXXXXX)
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
        # copy (origin keeps it, e.g. auto-delete-head-branches is off).
        git checkout -q -b feature/issue-77
        echo "pr-artifact" > pr-file.txt
        git add pr-file.txt
        git commit -q -m "pr: add artifact from existing PR branch"
        git push -q origin feature/issue-77
        git checkout -q main
        git branch -q -D feature/issue-77
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# Like setup_repo, but KEEPS the local feature/issue-78 branch instead of
# deleting it after push — the #8280 incident shape: a host that built an
# earlier slice still has feature/issue-N checked out locally when the next
# slice's worktree.sh N runs, selecting the LOCAL-ref reuse arm instead of the
# remote-only one setup_repo exercises.
setup_local_repo() {
    local name="${1:-stalelocalrepo}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtstalelocal.XXXXXX)
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

        git checkout -q -b feature/issue-78
        echo "pr-artifact" > pr-file.txt
        git add pr-file.txt
        git commit -q -m "pr: add artifact from existing PR branch"
        git push -q origin feature/issue-78
        git checkout -q main
    )
    echo "$tmp/$name"
}

# A minimal `gh` stand-in on PATH, modeling
# `pr list --head <branch> --state merged --json headRefOid,number --limit 1`.
# FAKE_GH_MODE=merged reports a merged PR whose headRefOid is the CURRENT tip
# of origin/<branch> (the stale case this issue targets). FAKE_GH_MODE=none
# reports no merged PR at all (the #4823 in-flight case). FAKE_GH_MODE=off
# makes every call fail (forge unavailable — fail open).
install_fake_gh() {
    local repo="$1"
    local fake_bin
    fake_bin="$(dirname "$repo")/fakebin"
    mkdir -p "$fake_bin"
    cat > "$fake_bin/gh" << EOF
#!/bin/bash
if [[ "\${FAKE_GH_MODE:-none}" == "off" ]]; then
    echo "gh: fake forge unavailable in this test" >&2
    exit 1
fi
if [[ "\$1" == "pr" && "\$2" == "list" ]]; then
    shift 2
    branch=""
    while [[ \$# -gt 0 ]]; do
        case "\$1" in
            --head) branch="\$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    if [[ "\${FAKE_GH_MODE:-none}" == "merged" && -n "\$branch" ]]; then
        sha="\$(git -C "$repo" rev-parse --verify -q "refs/remotes/origin/\$branch" 2>/dev/null || echo "")"
        if [[ -n "\$sha" ]]; then
            echo "[{\"headRefOid\": \"\$sha\", \"number\": 999}]"
            exit 0
        fi
    fi
    echo "[]"
    exit 0
fi
echo "fake gh: unsupported invocation: \$*" >&2
exit 1
EOF
    chmod +x "$fake_bin/gh"
    echo "$fake_bin"
}

# --- Test 1: origin/feature/issue-77's tip IS an already-merged PR's head ---
echo "Test 1: origin/feature/issue-77 is the head of an already-merged PR -> worktree.sh skips it, branches fresh from base"
REPO=$(setup_repo stalerepo1)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtstale-merged.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=merged ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ ! -f "$REPO/.loom/worktrees/issue-77/pr-file.txt" ]]; then
    pass "worktree does NOT contain the stale merged branch's artifact (created fresh from base, not reused)"
else
    fail "worktree contains the stale merged branch's artifact — reused a branch that was already squash-merged"
fi
WT_HEAD=$(git -C "$REPO/.loom/worktrees/issue-77" rev-parse HEAD 2>/dev/null || echo "")
MAIN_HEAD=$(git -C "$REPO" rev-parse origin/main 2>/dev/null || echo "")
if [[ -n "$WT_HEAD" && "$WT_HEAD" == "$MAIN_HEAD" ]]; then
    pass "worktree HEAD equals origin/main's head commit (fresh branch from base)"
else
    fail "worktree HEAD ($WT_HEAD) does not equal origin/main ($MAIN_HEAD)"
fi
if grep -qi "already-merged PR #999" "$OUT_LOG"; then
    pass "output names the already-merged PR and explains why the branch was skipped"
else
    fail "output does not explain the skip (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 2: origin/feature/issue-77 exists but is NOT a merged PR head (#4823 in-flight case) ---
echo ""
echo "Test 2: origin/feature/issue-77 is NOT the head of any merged PR -> worktree.sh still reuses it (#4823 unaffected)"
REPO=$(setup_repo stalerepo2)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtstale-inflight.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=none ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -f "$REPO/.loom/worktrees/issue-77/pr-file.txt" ]]; then
    pass "worktree contains the in-flight branch's artifact (still reused, per #4823)"
else
    fail "worktree is missing the in-flight branch's artifact — regressed #4823"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 3: forge lookup unavailable -> fails open to the pre-existing reuse behavior ---
echo ""
echo "Test 3: forge lookup unavailable -> worktree.sh falls open and still reuses the remote branch"
REPO=$(setup_repo stalerepo3)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtstale-unavailable.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=off ./.loom/scripts/worktree.sh 77 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -f "$REPO/.loom/worktrees/issue-77/pr-file.txt" ]]; then
    pass "worktree contains the branch's artifact (forge unavailable -> fail open to reuse)"
else
    fail "worktree is missing the branch's artifact — forge-unavailable case incorrectly blocked reuse"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 4 (#8280): LOCAL feature/issue-78's tip IS an already-merged PR's head ---
echo ""
echo "Test 4 (#8280): LOCAL feature/issue-78 is the head of an already-merged PR -> worktree.sh refuses to reuse it"
REPO=$(setup_local_repo stalelocalrepo1)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtstalelocal-merged.$$"
RC=0
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=merged ./.loom/scripts/worktree.sh 78 >"$OUT_LOG" 2>&1
) || RC=$?
if [[ "$RC" -ne 0 ]]; then
    pass "worktree.sh exits non-zero rather than hand back a worktree on the already-landed local branch"
else
    fail "worktree.sh exited 0 - it must refuse rather than silently reuse the landed branch"
    cat "$OUT_LOG"
fi
if [[ ! -d "$REPO/.loom/worktrees/issue-78" ]]; then
    pass "no worktree was created on the already-merged local branch"
else
    fail "a worktree was created despite the local branch already having landed"
fi
if grep -qi "already-merged PR #999" "$OUT_LOG"; then
    pass "output names the already-merged PR and explains the refusal"
else
    fail "output does not explain the refusal (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
if git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-78; then
    pass "the local branch itself is left alone (refusal, not deletion)"
else
    fail "the local branch was unexpectedly deleted - refusal must not be destructive"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 5 (#8280): forge lookup unavailable for a LOCAL branch -> fails open ---
echo ""
echo "Test 5 (#8280): forge lookup unavailable for a LOCAL branch -> worktree.sh still reuses it"
REPO=$(setup_local_repo stalelocalrepo2)
FAKE_BIN=$(install_fake_gh "$REPO")
OUT_LOG="/tmp/wtstalelocal-unavailable.$$"
(
    cd "$REPO"
    PATH="$FAKE_BIN:$PATH" FAKE_GH_MODE=off ./.loom/scripts/worktree.sh 78 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)
if [[ -f "$REPO/.loom/worktrees/issue-78/pr-file.txt" ]]; then
    pass "worktree contains the local branch's artifact (forge unavailable -> fails open to reuse, same as the remote arm's Test 3)"
else
    fail "worktree is missing the local branch's artifact - forge-unavailable case incorrectly blocked local reuse"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
