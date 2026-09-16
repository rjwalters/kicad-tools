#!/usr/bin/env bash
# test-branch-landed.sh — unit tests for the shared `branch_landed` primitive
# (#7812, defaults/scripts/lib/branch-landed.sh).
#
# Every case runs against a disposable local fixture repo under `mktemp -d`;
# nothing here touches the network. The forge rung is exercised by redefining
# `_branch_landed_forge_probe` after sourcing (the seam the library documents),
# and suppressed entirely with LOOM_BRANCH_LANDED_OFFLINE=1 for the cases that
# are about the offline tree-equality path.
#
# Coverage (the acceptance criteria of #7812):
#   1. squash-merged branch                -> landed   (tree equality)
#   2. rebase-merged branch (SHAs rewritten) -> landed (tree equality)
#   3. merge-commit-merged branch          -> landed   (ancestry)
#   4. un-merged branch with real commits  -> not-landed
#   5. no PR at all, offline               -> answered by git alone
#   6. forge reports a merged PR           -> landed, PR number surfaced
#   7. merged PR + extra local commits     -> not-landed (conservative)
#   8. caller-supplied merged head SHA     -> landed with NO forge call
#   9. git < 2.38 simulated                -> ancestry still works; otherwise
#                                             the fail-closed `unknown`
#  10. conflicting branch                  -> not-landed
#  11. unresolvable branch / no default    -> unknown (never a guess)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$(cd "$SCRIPT_DIR/../lib" && pwd)/branch-landed.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" what="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$what (= $actual)"
    else
        fail "$what: expected '$expected', got '$actual'"
    fi
}

# shellcheck source=../lib/branch-landed.sh
source "$LIB"

git_q() { git -C "$1" "${@:2}" >/dev/null 2>&1; }

# A repo with `main` (one committed file) and no remote. Echoes its path.
new_repo() {
    local tmp
    tmp="$(mktemp -d /tmp/loom-branchlanded.XXXXXX)"
    git_q "$tmp" init -q -b main .
    git_q "$tmp" config user.email t@t
    git_q "$tmp" config user.name t
    echo base > "$tmp/base.txt"
    git_q "$tmp" add -A
    git_q "$tmp" commit -q -m base
    echo "$tmp"
}

# Create `feature/issue-1` with one real change, leaving HEAD back on main.
add_feature() {
    local repo="$1" file="${2:-feature.txt}" content="${3:-feature content}"
    git_q "$repo" checkout -q -b feature/issue-1
    echo "$content" > "$repo/$file"
    git_q "$repo" add -A
    git_q "$repo" commit -q -m "issue 1 work"
    git_q "$repo" checkout -q main
}

cleanup() { rm -rf "$1"; }

# All git-only cases run with the forge suppressed — these are the offline
# tree-equality / ancestry rungs, and a stray `gh` on PATH must not make the
# suite non-hermetic.
export LOOM_BRANCH_LANDED_OFFLINE=1

# --- 1. squash-merged -------------------------------------------------------
echo "Test 1: squash-merged branch (new SHA on main, branch not an ancestor)"
REPO="$(new_repo)"; add_feature "$REPO"
git_q "$REPO" merge --squash feature/issue-1
git_q "$REPO" commit -q -m "squash-merge (simulated)"
if git -C "$REPO" merge-base --is-ancestor feature/issue-1 main 2>/dev/null; then
    fail "precondition: squashed branch must NOT be an ancestor of main"
else
    pass "precondition: squashed branch is not an ancestor of main"
fi
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-1 main >/tmp/bl.$$ 2>&1
assert_eq "landed" "$(cat /tmp/bl.$$)" "squash-merged branch is landed"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-1 main >/dev/null
assert_eq "tree-equal" "$BRANCH_LANDED_EVIDENCE" "…proved by tree equality, not ancestry"
cleanup "$REPO"

# --- 2. rebase-merged (SHAs rewritten) --------------------------------------
echo ""
echo "Test 2: rebase-merged branch — same content on main under a different SHA"
REPO="$(new_repo)"; add_feature "$REPO"
TIP="$(git -C "$REPO" rev-parse feature/issue-1)"
echo "meanwhile" > "$REPO/other.txt"
git_q "$REPO" add -A
git_q "$REPO" commit -q -m "unrelated main commit"
git_q "$REPO" cherry-pick "$TIP"
REBASED="$(git -C "$REPO" rev-parse main)"
if [[ "$REBASED" != "$TIP" ]]; then
    pass "precondition: the replayed commit has a different SHA"
else
    fail "precondition: cherry-pick did not rewrite the SHA"
fi
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-1 main >/dev/null
assert_eq "landed" "$BRANCH_LANDED_VERDICT" "rebase-merged branch is landed"
cleanup "$REPO"

# --- 3. merge-commit-merged -------------------------------------------------
echo ""
echo "Test 3: merge-commit-merged branch (ancestry rung)"
REPO="$(new_repo)"; add_feature "$REPO"
git_q "$REPO" merge --no-ff -m "merge feature/issue-1" feature/issue-1
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-1 main >/dev/null
assert_eq "landed" "$BRANCH_LANDED_VERDICT" "merge-commit-merged branch is landed"
assert_eq "ancestor" "$BRANCH_LANDED_EVIDENCE" "…answered by the cheap ancestry rung"
cleanup "$REPO"

# --- 4/5. un-merged branch, no PR at all ------------------------------------
echo ""
echo "Test 4: un-merged branch with real commits and no PR -> not-landed"
REPO="$(new_repo)"; add_feature "$REPO"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-1 main >/dev/null
assert_eq "not-landed" "$BRANCH_LANDED_VERDICT" "un-merged branch is not landed"
assert_eq "tree-differs" "$BRANCH_LANDED_EVIDENCE" "…proved offline, with no forge at all"

# A content-free commit contributes nothing, so it IS landed — the tree is
# what matters, not the commit count.
git_q "$REPO" checkout -q -b feature/issue-2 main
git_q "$REPO" commit -q --allow-empty -m "no content"
git_q "$REPO" checkout -q main
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-2 main >/dev/null
assert_eq "landed" "$BRANCH_LANDED_VERDICT" "a content-free commit is landed (tree equality)"
cleanup "$REPO"

# --- 10. conflicting branch -------------------------------------------------
echo ""
echo "Test 5: a branch that conflicts with main -> not-landed"
REPO="$(new_repo)"
add_feature "$REPO" base.txt "branch version"
echo "main version" > "$REPO/base.txt"
git_q "$REPO" add -A
git_q "$REPO" commit -q -m "main edits the same file"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-1 main >/dev/null
assert_eq "not-landed" "$BRANCH_LANDED_VERDICT" "a conflicting branch is not landed"
cleanup "$REPO"

# --- 9. git < 2.38: the merge-tree rung is unavailable -----------------------
echo ""
echo "Test 6: simulated git < 2.38 (no 'merge-tree --write-tree')"
REPO="$(new_repo)"; add_feature "$REPO"
git_q "$REPO" merge --squash feature/issue-1
git_q "$REPO" commit -q -m "squash-merge (simulated)"
# Forge offline AND no tree comparison: nothing can answer -> `unknown`, NOT a
# guess in either direction. This is the fail-closed contract.
LOOM_BRANCH_LANDED_GIT_VERSION="2.37.9" BRANCH_LANDED_REPO_DIR="$REPO" \
    branch_landed feature/issue-1 main >/dev/null
assert_eq "unknown" "$BRANCH_LANDED_VERDICT" "old git + no forge = unknown (fail closed)"
assert_eq "inconclusive" "$BRANCH_LANDED_EVIDENCE" "…and says so, rather than naming evidence"
# Ancestry needs no merge-tree, so an actually-merged branch is still landed
# on an old git.
git_q "$REPO" checkout -q -b feature/issue-3 main
echo more > "$REPO/more.txt"
git_q "$REPO" add -A
git_q "$REPO" commit -q -m more
git_q "$REPO" checkout -q main
git_q "$REPO" merge --no-ff -m "merge feature/issue-3" feature/issue-3
LOOM_BRANCH_LANDED_GIT_VERSION="2.37.9" BRANCH_LANDED_REPO_DIR="$REPO" \
    branch_landed feature/issue-3 main >/dev/null
assert_eq "landed" "$BRANCH_LANDED_VERDICT" "old git still answers the ancestry rung"
# A version string git itself would never print must not be read as ">= 2.38".
LOOM_BRANCH_LANDED_GIT_VERSION="garbage" BRANCH_LANDED_REPO_DIR="$REPO" \
    branch_landed feature/issue-1 main >/dev/null
assert_eq "unknown" "$BRANCH_LANDED_VERDICT" "an unparseable git version fails closed too"
cleanup "$REPO"

# --- 11. nothing resolvable -------------------------------------------------
echo ""
echo "Test 7: an unresolvable branch is 'unknown', never a guess"
REPO="$(new_repo)"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/does-not-exist main >/dev/null
assert_eq "unknown" "$BRANCH_LANDED_VERDICT" "unresolvable branch -> unknown"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed "" main >/dev/null
assert_eq "unknown" "$BRANCH_LANDED_VERDICT" "empty branch argument -> unknown"
cleanup "$REPO"

# --- 6/7/8. the forge rung, with the probe stubbed --------------------------
echo ""
echo "Test 8: the forge rung (probe stubbed — no network, no gh)"
unset LOOM_BRANCH_LANDED_OFFLINE
REPO="$(new_repo)"; add_feature "$REPO"
# Squash the branch into main WITHOUT taking its content, so the tree
# comparison cannot be what answers: only the forge can.
git_q "$REPO" checkout -q -b feature/issue-9 main
echo nine > "$REPO/nine.txt"
git_q "$REPO" add -A
git_q "$REPO" commit -q -m "issue 9 work"
git_q "$REPO" checkout -q main
NINE_TIP="$(git -C "$REPO" rev-parse feature/issue-9)"

PROBE_CALLS=0
_branch_landed_forge_probe() {
    PROBE_CALLS=$((PROBE_CALLS + 1))
    _BRANCH_LANDED_PROBE_STATUS="$STUB_STATUS"
    _BRANCH_LANDED_PROBE_SHA="$STUB_SHA"
    _BRANCH_LANDED_PROBE_NUMBER="$STUB_NUMBER"
}

STUB_STATUS="found" STUB_SHA="$NINE_TIP" STUB_NUMBER="4242"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-9 main >/dev/null
assert_eq "landed" "$BRANCH_LANDED_VERDICT" "a merged PR whose head is the branch tip -> landed"
assert_eq "forge-merged-pr" "$BRANCH_LANDED_EVIDENCE" "…attributed to the forge"
assert_eq "4242" "$BRANCH_LANDED_PR_NUMBER" "…and surfaces the PR number for the caller's message"

# A merged PR plus extra local commits on top: the tip is not the merged head,
# and the extra commit carries content main does not have -> conservative.
git_q "$REPO" checkout -q feature/issue-9
echo extra > "$REPO/extra.txt"
git_q "$REPO" add -A
git_q "$REPO" commit -q -m "unpushed extra work"
git_q "$REPO" checkout -q main
STUB_STATUS="found" STUB_SHA="$NINE_TIP" STUB_NUMBER="4242"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-9 main >/dev/null
assert_eq "not-landed" "$BRANCH_LANDED_VERDICT" "merged PR + unpushed extra work -> not landed"

# Forge unreachable: the tree rung still answers (this is the whole point of
# having two independent rungs).
STUB_STATUS="unavailable" STUB_SHA="" STUB_NUMBER=""
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-9 main >/dev/null
assert_eq "not-landed" "$BRANCH_LANDED_VERDICT" "forge unavailable -> the offline rung still answers"
assert_eq "unavailable" "$BRANCH_LANDED_FORGE_STATUS" "…and the caller can see the forge failed"

# Forge unreachable AND the tree rung unavailable -> unknown. Nothing may
# reap or delete on this.
STUB_STATUS="unavailable" STUB_SHA="" STUB_NUMBER=""
LOOM_BRANCH_LANDED_GIT_VERSION="2.30.0" BRANCH_LANDED_REPO_DIR="$REPO" \
    branch_landed feature/issue-9 main >/dev/null
assert_eq "unknown" "$BRANCH_LANDED_VERDICT" "both rungs unavailable -> unknown"

# Caller-supplied merged head SHA: answered with NO forge round-trip at all.
BEFORE="$PROBE_CALLS"
EXTRA_TIP="$(git -C "$REPO" rev-parse feature/issue-9)"
BRANCH_LANDED_REPO_DIR="$REPO" branch_landed feature/issue-9 main "$EXTRA_TIP" >/dev/null
assert_eq "landed" "$BRANCH_LANDED_VERDICT" "tip matching the caller's merged head SHA -> landed"
assert_eq "merged-head-match" "$BRANCH_LANDED_EVIDENCE" "…attributed to the caller's hint"
assert_eq "$BEFORE" "$PROBE_CALLS" "…with no forge call made"
cleanup "$REPO"

# --- Summary ----------------------------------------------------------------
rm -f /tmp/bl.$$
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
