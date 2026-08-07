#!/usr/bin/env bash
# test-worktree-remove-squash-merge.sh - Tests for #4889.
#
# `worktree.sh remove <N>` used to delete the attached branch with a bare
# `git branch -d`, which ALWAYS refuses on a squash-merged branch: a squash
# merge rewrites the PR's commits into one new commit on the default branch,
# so the original branch tip is never an ancestor of HEAD and never
# satisfies `git branch --merged` / `-d`. merge-pr.sh already solved this
# (#4100) with a squash-aware safety rule (`_maybe_delete_local_branch`):
# compare the local branch tip against the merged PR's `head.sha`, and only
# upgrade to `git branch -D` when they match.
#
# This suite verifies `worktree.sh remove` now shares that exact rule
# (extracted verbatim from the live merge-pr.sh source, no drift) instead of
# a bare `git branch -d`:
#   1. A branch whose changes were squash-merged onto the default branch (so
#      it is genuinely NOT an ancestor / NOT `--merged`) is still deleted
#      when the forge reports a merged PR whose head SHA matches the local
#      branch tip.
#   2. A branch with genuinely unpushed local work (tip does not match any
#      merged PR head) is still refused — the conservative behaviour from
#      before #4889 is preserved.
#   3. When the forge lookup itself is unavailable (no `gh`, in this test:
#      redirected to a stub that always fails), the branch is left in place
#      and the output distinguishes "could not determine" from "genuinely
#      unmerged".
#
# Follows the throwaway-repo + fake-forge-binary harness pattern already used
# by test-worktree-remove.sh (worktree.sh + its lib/ helpers copied into a
# temp tree) and test-merge-pr-local-branch-cleanup.sh (extract-and-eval the
# real function body — here exercised indirectly through worktree.sh itself,
# not re-extracted a second time).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"
MERGE_PR_SH="$SCRIPTS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# --- Throwaway repo setup ---------------------------------------------------
TMP=$(mktemp -d /tmp/loom-remove-squash-test.XXXXXX)
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
cp "$MERGE_PR_SH" .loom/scripts/merge-pr.sh
if [[ -d "$SCRIPTS_DIR/lib" ]]; then
    cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
fi
chmod +x .loom/scripts/worktree.sh .loom/scripts/merge-pr.sh

REPO="$TMP/repo"

# --- Fake forge binary -------------------------------------------------------
# A minimal `gh` stand-in on PATH. Responds to
# `pr list --head <branch> --state merged --json headRefOid --limit 1` by
# echoing the CURRENT tip of <branch> in $FAKE_GH_REPO — modeling a merged
# PR whose head.sha is exactly the branch's pre-squash tip (the real-world
# invariant: squashing rewrites the DEFAULT branch, never the source
# branch). `FAKE_GH_MODE=off` makes every call fail, modeling an
# unavailable forge.
FAKE_BIN="$TMP/fakebin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/gh" << 'EOF'
#!/bin/bash
if [[ "${FAKE_GH_MODE:-on}" == "off" ]]; then
    echo "gh: fake forge unavailable in this test" >&2
    exit 1
fi
if [[ "$1" == "pr" && "$2" == "list" ]]; then
    shift 2
    branch=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --head) branch="$2"; shift 2 ;;
            *) shift ;;
        esac
    done
    sha=""
    if [[ -n "$branch" && -n "${FAKE_GH_REPO:-}" ]]; then
        sha="$(git -C "$FAKE_GH_REPO" rev-parse --verify -q "refs/heads/$branch" 2>/dev/null || echo "")"
    fi
    # FAKE_GH_MISMATCH_SHA, when set, overrides the reported head SHA with a
    # deliberately WRONG value (models a merged PR whose head does not match
    # this local branch — i.e. genuine unpushed local work on top).
    if [[ -n "${FAKE_GH_MISMATCH_SHA:-}" ]]; then
        sha="$FAKE_GH_MISMATCH_SHA"
    fi
    if [[ -n "$sha" ]]; then
        echo "[{\"headRefOid\": \"$sha\"}]"
    else
        echo "[]"
    fi
    exit 0
fi
echo "fake gh: unsupported invocation: $*" >&2
exit 1
EOF
chmod +x "$FAKE_BIN/gh"

# Prepend the fake bin dir so it shadows any real `gh`/`loom-daemon` on PATH.
export PATH="$FAKE_BIN:$PATH"
export FAKE_GH_REPO="$REPO"

# --- Test 1: squash-merged branch is force-deleted when the forge confirms it -
echo "Test 1: a squash-merged branch is deleted when the forge reports a matching merged-PR head SHA"
cd "$REPO"
./.loom/scripts/worktree.sh 201 >/dev/null 2>&1
echo "feature work" >> ".loom/worktrees/issue-201/README.md" 2>/dev/null || echo "feature work" > ".loom/worktrees/issue-201/feature.txt"
git -C ".loom/worktrees/issue-201" add -A
git -C ".loom/worktrees/issue-201" commit -q -m "issue 201 work"

# Simulate GitHub's squash merge directly on main: `git merge --squash` folds
# the branch's changes into ONE new commit with no parent link back to the
# branch (exactly what a squash merge does), so the branch is provably NOT
# an ancestor of main afterward.
git -C "$REPO" checkout -q main
git -C "$REPO" merge -q --squash feature/issue-201 >/dev/null 2>&1
git -C "$REPO" commit -q -m "squash-merge feature/issue-201 (simulated)"

# Precondition: prove the bug this issue describes would otherwise reproduce
# — the branch is genuinely not an ancestor of main post-squash.
if ! git -C "$REPO" merge-base --is-ancestor feature/issue-201 main 2>/dev/null; then
    pass "precondition: squashed branch is NOT an ancestor of main (plain 'git branch -d' would refuse)"
else
    fail "precondition failed: branch unexpectedly IS an ancestor of main (test setup bug)"
fi

FAKE_GH_MODE=on ./.loom/scripts/worktree.sh remove 201 >/tmp/rm-sq-out1.$$ 2>&1
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "remove exits 0 for a squash-merged worktree"
else
    fail "remove exited non-zero (see /tmp/rm-sq-out1.$$)"
fi
if [[ ! -d ".loom/worktrees/issue-201" ]]; then
    pass "worktree directory removed"
else
    fail "worktree directory still present"
fi
if ! git -C "$REPO" show-ref --verify --quiet "refs/heads/feature/issue-201"; then
    pass "squash-merged local branch feature/issue-201 was deleted"
else
    fail "squash-merged local branch feature/issue-201 still exists (bare 'git branch -d' regression)"
fi
if grep -qi "safe force-delete" /tmp/rm-sq-out1.$$ 2>/dev/null; then
    pass "output attributes the delete to the tip-match safety check"
else
    fail "output does not explain the force-delete (see /tmp/rm-sq-out1.$$)"
fi

# --- Test 2: genuinely unpushed local work is still refused ------------------
echo ""
echo "Test 2: a branch with unpushed local work beyond the merged PR head is still refused"
cd "$REPO"
./.loom/scripts/worktree.sh 202 >/dev/null 2>&1
echo "merged part" > ".loom/worktrees/issue-202/merged.txt"
git -C ".loom/worktrees/issue-202" add -A
git -C ".loom/worktrees/issue-202" commit -q -m "merged part"
echo "extra unpushed work" > ".loom/worktrees/issue-202/extra.txt"
git -C ".loom/worktrees/issue-202" add -A
git -C ".loom/worktrees/issue-202" commit -q -m "extra unpushed work — must survive"

# The fake forge reports a DIFFERENT (mismatched) head SHA, modeling a merged
# PR whose head does not include this extra local commit.
FAKE_GH_MODE=on FAKE_GH_MISMATCH_SHA="0000000000000000000000000000000000dead" \
    ./.loom/scripts/worktree.sh remove 202 >/tmp/rm-sq-out2.$$ 2>&1
if [[ ! -d ".loom/worktrees/issue-202" ]]; then
    pass "worktree directory removed even though branch delete was refused"
else
    fail "worktree directory still present after remove"
fi
if git -C "$REPO" show-ref --verify --quiet "refs/heads/feature/issue-202"; then
    pass "branch with unpushed local work was NOT deleted (conservative refusal preserved)"
else
    fail "branch with unpushed local work was deleted — data-loss regression"
fi
if grep -qi "may have unpushed commits" /tmp/rm-sq-out2.$$ 2>/dev/null; then
    pass "refusal message names 'unpushed commits', not a generic failure"
else
    fail "no clear unpushed-commits refusal message (see /tmp/rm-sq-out2.$$)"
fi

# --- Test 3: forge lookup unavailable -> left in place with a distinct note --
echo ""
echo "Test 3: an unreachable forge is distinguished from 'genuinely unmerged'"
cd "$REPO"
./.loom/scripts/worktree.sh 203 >/dev/null 2>&1
echo "unpublished work" > ".loom/worktrees/issue-203/unpublished.txt"
git -C ".loom/worktrees/issue-203" add -A
git -C ".loom/worktrees/issue-203" commit -q -m "issue 203 work, never merged"

FAKE_GH_MODE=off ./.loom/scripts/worktree.sh remove 203 >/tmp/rm-sq-out3.$$ 2>&1
if git -C "$REPO" show-ref --verify --quiet "refs/heads/feature/issue-203"; then
    pass "branch is left in place when the forge lookup is unavailable"
else
    fail "branch was deleted despite an unavailable forge lookup"
fi
if grep -qi "Could not query the forge" /tmp/rm-sq-out3.$$ 2>/dev/null; then
    pass "output distinguishes 'could not determine' (forge unavailable) from a generic refusal"
else
    fail "no 'could not query the forge' note emitted (see /tmp/rm-sq-out3.$$)"
fi

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
