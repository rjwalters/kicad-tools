#!/usr/bin/env bash
# test-merge-pr-primary-checkout-advice.sh - Regression test for #4171.
#
# merge-pr.sh's discovery-fallback cleanup path (no worktree at the default
# `.loom/worktrees/issue-N` / `pr-N` convention, so it walks `git worktree
# list --porcelain` for a worktree tracking the merged PR's branch) could
# discover the PRIMARY (main) working copy — not a linked worktree at all —
# and, because the primary carries no `.loom-managed` sentinel (correctly:
# it isn't Loom-managed), fell into the generic "lacks sentinel — not
# removing (user-owned)" branch. That branch then suggested `--worktree-path
# '<primary>'` and "manually: git worktree remove '<primary>'" — advice that
# can never work, since git refuses to remove the primary working tree.
# `_maybe_delete_local_branch`'s subsequent branch-delete attempt then failed
# with only the generic "checked out (current HEAD or another worktree)"
# message, without telling the operator how to actually reclaim the branch.
#
# The fix adds `_is_primary_worktree_path` and uses it in two places:
#   1. The discovery-fallback block: when the discovered worktree IS the
#      primary checkout, it never suggests `git worktree remove` /
#      `--worktree-path`; it reports the branch is checked out in the
#      primary checkout instead.
#   2. `_maybe_delete_local_branch`: when a branch-delete fails because the
#      branch is checked out, and that checkout location IS the primary
#      checkout, it prints the exact two-step remediation (`git checkout
#      <default-branch> && git branch -D <branch>`) instead of the generic
#      message.
#
# Verifies:
#   1. Source contains the `_is_primary_worktree_path` helper and its two
#      call sites (static grep checks).
#   2. Behavioral: the discovery-fallback branch, run against a REAL git repo
#      where the PR branch is checked out in the primary checkout (no
#      .loom-managed sentinel there, matching the real-world #4171 repro),
#      never emits `git worktree remove` / `--worktree-path` advice.
#   3. Behavioral: `_maybe_delete_local_branch`, run against the same repo,
#      fails to delete the branch (git refuses — it's the checked-out HEAD)
#      but prints the specific `git checkout <default> && git branch -D
#      <branch>` two-step advice rather than the generic message.
#   4. Control: unchanged behavior for a genuine linked worktree — both the
#      loom-managed (auto-removed) and user-owned (generic "lacks sentinel"
#      advice, `git worktree remove` suggestion intact) cases.
#   5. #5015 extension: the two-step advice above was previously the END of
#      the story even when the tip-match safety check (already computed for
#      the force-delete path) provably held. Now, when the primary
#      checkout's tree is also clean, merge-pr.sh performs the checkout+
#      delete itself rather than just printing instructions; a dirty tree
#      keeps exactly the old print-only behavior. See also
#      test-merge-pr-local-branch-cleanup.sh cases (g)-(j) for the
#      opt-out-flag and active-stash edge cases.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

[[ -f "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not found" >&2; exit 1; }

# --- Test 1: source retains the #4171 primary-checkout distinction ---
echo "Test 1: merge-pr.sh source contains the primary-checkout helper and call sites"

assert_grep '_is_primary_worktree_path\(\) \{' "$MERGE_PR" \
    "merge-pr.sh defines the _is_primary_worktree_path helper"
assert_grep 'if _is_primary_worktree_path "\$DISCOVERED_WT"; then' "$MERGE_PR" \
    "discovery-fallback checks whether the discovered worktree is the primary checkout"
assert_grep 'not a removable worktree' "$MERGE_PR" \
    "discovery-fallback reports the primary checkout as not removable"
assert_grep 'checkout_loc="\$\(_find_worktree_by_branch "\$branch"\)"' "$MERGE_PR" \
    "_maybe_delete_local_branch resolves the branch's checkout location"
assert_grep "git -C '\\\$checkout_loc' checkout \\\$default_label" "$MERGE_PR" \
    "_maybe_delete_local_branch prints the two-step checkout+delete advice"

# --- Extract the ACTUAL function bodies from the live source (no drift) ---
extract_fn() {
    local name="$1" file="$2"
    awk -v fn="$name" '
      $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
      grab { print }
      grab && /^}/ { exit }
    ' "$file"
}

# Harness: stub the logging helpers, then eval the real bodies.
info()    { echo "INFO: $*"; }
warning() { echo "WARN: $*"; }
success() { echo "OK: $*"; }
error()   { echo "ERROR: $*" >&2; return 1; }

eval "$(extract_fn _primary_worktree_path     "$MERGE_PR")"
eval "$(extract_fn _is_primary_worktree_path  "$MERGE_PR")"
eval "$(extract_fn _worktree_branch_for       "$MERGE_PR")"
eval "$(extract_fn _find_worktree_by_branch   "$MERGE_PR")"
eval "$(extract_fn _maybe_delete_local_branch "$MERGE_PR")"
eval "$(extract_fn _remove_loom_worktree      "$MERGE_PR")"

# --- Build a real git repo matching the #4171 repro scenario ---
echo ""
echo "Test 2: PR branch checked out in the PRIMARY checkout (the #4171 repro)"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-merge-primary-advice.XXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf "$TMP_ROOT" 2>/dev/null || true; }
trap cleanup EXIT

PRIMARY="$TMP_ROOT/primary-checkout"
mkdir -p "$PRIMARY"
git -C "$PRIMARY" init -q
git -C "$PRIMARY" config user.email "test@example.com"
git -C "$PRIMARY" config user.name "Test"
echo "hello" > "$PRIMARY/README.md"
git -C "$PRIMARY" add -A
git -C "$PRIMARY" commit -q -m "initial"
# Normalize the initial branch name so the test does not depend on the
# runner's init.defaultBranch git config — _maybe_delete_local_branch()'s
# auto-cleanup does `git checkout "$DEFAULT_BRANCH_NAME"` (main), which would
# otherwise fail on runners where `git init` does not name the branch `main`.
git -C "$PRIMARY" branch -M main

# The merged PR's branch, checked out directly in the primary — NO
# .loom-managed sentinel here (this is the real-world case: the primary
# checkout is never Loom-managed).
PR_BRANCH="config/enable-collision-detection"
git -C "$PRIMARY" checkout -q -b "$PR_BRANCH"

# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"
# shellcheck disable=SC2034
DEFAULT_BRANCH_NAME="main"

# (a) Discovery: _find_worktree_by_branch finds the primary itself.
discovered="$(_find_worktree_by_branch "$PR_BRANCH")"
if [[ "$discovered" == "$PRIMARY" ]]; then
    pass "_find_worktree_by_branch discovers the primary checkout for the PR branch"
else
    fail "_find_worktree_by_branch: expected '$PRIMARY', got '$discovered'"
fi

# (b) _is_primary_worktree_path correctly identifies it.
if _is_primary_worktree_path "$discovered"; then
    pass "_is_primary_worktree_path recognizes the discovered path as the primary checkout"
else
    fail "_is_primary_worktree_path failed to recognize the primary checkout"
fi

# (c) Simulate the discovery-fallback decision tree exactly as merge-pr.sh
# does, and assert it NEVER suggests git worktree remove / --worktree-path.
sim_out=""
if _is_primary_worktree_path "$discovered"; then
    sim_out="$(info "PR branch '$PR_BRANCH' is checked out in the primary repository checkout ($discovered) — not a removable worktree." 2>&1)"
elif [[ -f "$discovered/.loom-managed" ]]; then
    sim_out="$(_remove_loom_worktree "$discovered" 2>&1)"
else
    sim_out="$(warning "Discovered worktree for branch '$PR_BRANCH' at: $discovered"; \
               warning "Worktree lacks .loom-managed sentinel — not removing (user-owned)."; \
               warning "To clean it up, re-run with: --worktree-path '$discovered'"; \
               warning "Or manually: git worktree remove '$discovered'")"
fi

if [[ "$sim_out" == *"not a removable worktree"* ]] \
   && [[ "$sim_out" != *"git worktree remove"* ]] \
   && [[ "$sim_out" != *"--worktree-path"* ]]; then
    pass "discovery-fallback never suggests git worktree remove / --worktree-path for the primary checkout"
else
    fail "discovery-fallback should avoid worktree-remove advice for the primary; got: $sim_out"
fi

echo ""
echo "Test 3: branch-delete failure in the primary checkout gives the exact two-step advice"

set +e
branch_out="$(_maybe_delete_local_branch "$PR_BRANCH" 2>&1)"
branch_rc=$?
set -e

if [[ $branch_rc -eq 0 ]] \
   && [[ "$branch_out" == *"checked out in the primary repository checkout"* ]] \
   && [[ "$branch_out" == *"checkout main"* ]] \
   && [[ "$branch_out" == *"branch -D $PR_BRANCH"* ]]; then
    pass "_maybe_delete_local_branch prints the exact two-step 'git checkout main && ... branch -D' advice"
else
    fail "_maybe_delete_local_branch: expected two-step advice; rc=$branch_rc, out: $branch_out"
fi

# Branch must survive (never force-deleted out from under the checked-out HEAD).
if git -C "$PRIMARY" show-ref --verify --quiet "refs/heads/$PR_BRANCH"; then
    pass "branch survives — never force-deleted while checked out in the primary"
else
    fail "branch was deleted despite being checked out in the primary"
fi

# --- Test 4: control — genuine linked worktrees are unaffected ---
echo ""
echo "Test 4: control — genuine linked worktrees keep their existing behavior"

# (a) Loom-managed secondary worktree at a non-standard path: still auto-removed.
SECONDARY="$TMP_ROOT/scratch/issue-999"
mkdir -p "$(dirname "$SECONDARY")"
git -C "$PRIMARY" worktree add -q -b feature/issue-999 "$SECONDARY" >/dev/null 2>&1
touch "$SECONDARY/.loom-managed"

if _is_primary_worktree_path "$SECONDARY"; then
    fail "secondary worktree misidentified as the primary checkout"
else
    pass "secondary worktree correctly NOT identified as the primary checkout"
fi

set +e
sec_out="$(_remove_loom_worktree "$SECONDARY" 2>&1)"
sec_rc=$?
set -e
if [[ $sec_rc -eq 0 ]] \
   && [[ "$sec_out" == *"Removing worktree"* ]] \
   && [[ ! -d "$SECONDARY" ]]; then
    pass "genuine Loom-managed secondary worktree is still auto-removed (unchanged)"
else
    fail "secondary should still be removed; rc=$sec_rc, out: $sec_out"
fi

# (b) Genuine user-owned linked worktree (no sentinel, NOT the primary): the
# original "lacks sentinel — not removing" + git-worktree-remove advice is
# still correct there and must be preserved by the simulated decision tree.
USER_WT="$TMP_ROOT/scratch/user-owned"
git -C "$PRIMARY" worktree add -q -b feature/user-owned "$USER_WT" >/dev/null 2>&1

if _is_primary_worktree_path "$USER_WT"; then
    fail "user-owned linked worktree misidentified as the primary checkout"
else
    user_sim_out="$(warning "Discovered worktree for branch 'feature/user-owned' at: $USER_WT"; \
                     warning "Worktree lacks .loom-managed sentinel — not removing (user-owned)."; \
                     warning "To clean it up, re-run with: --worktree-path '$USER_WT'"; \
                     warning "Or manually: git worktree remove '$USER_WT'")"
    if [[ "$user_sim_out" == *"git worktree remove"* ]] && [[ "$user_sim_out" == *"--worktree-path"* ]]; then
        pass "genuine user-owned linked worktree keeps the git-worktree-remove advice (unchanged)"
    else
        fail "user-owned worktree advice regressed: $user_sim_out"
    fi
fi

# --- Test 5: primary-checkout auto-cleanup when it is PROVABLY safe (#5015) ---
#
# Test 3 above only exercises the "print manual instructions" path because it
# calls _maybe_delete_local_branch without an expected_head_sha, so the
# tip-match safety check never engages. This distinguishes that case from the
# genuinely-safe one: a clean tree AND a tip matching the merged PR head SHA
# completes the cleanup automatically instead of just printing advice. The
# companion dirty-tree control confirms the two-step advice remains unchanged
# whenever the tree is NOT provably clean.
echo ""
echo "Test 5: primary-checkout auto-cleanup when provably safe (#5015)"

# shellcheck disable=SC2034
CLEANUP_PRIMARY_CHECKOUT=true

# (a) clean tree + tip matches the merged PR head SHA -> auto checkout+delete.
git -C "$PRIMARY" checkout -q -b feature/issue-501
echo "clean work" >> "$PRIMARY/README.md"
git -C "$PRIMARY" commit -q -am "issue 501 work"
CLEAN_TIP_SHA="$(git -C "$PRIMARY" rev-parse feature/issue-501)"

set +e
clean_out="$(_maybe_delete_local_branch "feature/issue-501" "$CLEAN_TIP_SHA" 2>&1)"
clean_rc=$?
set -e

if [[ $clean_rc -eq 0 ]] \
   && [[ "$clean_out" == *"deleted"* ]] \
   && [[ "$clean_out" == *"safe force-delete"* ]] \
   && [[ "$(git -C "$PRIMARY" branch --show-current)" == "main" ]] \
   && ! git -C "$PRIMARY" show-ref --verify --quiet refs/heads/feature/issue-501; then
    pass "(5a) clean tree + tip-matching head SHA auto-cleans the branch instead of printing advice"
else
    fail "(5a) expected auto-cleanup; rc=$clean_rc, out: $clean_out, branch: $(git -C "$PRIMARY" branch --show-current)"
fi

# (b) control: same setup but a DIRTY tree -> unchanged manual-instructions
# behavior (this is the "leave current behavior unchanged when the tree is
# dirty" acceptance criterion).
git -C "$PRIMARY" checkout -q -b feature/issue-502
echo "clean work" >> "$PRIMARY/README.md"
git -C "$PRIMARY" commit -q -am "issue 502 work"
DIRTY_TIP_SHA="$(git -C "$PRIMARY" rev-parse feature/issue-502)"
echo "uncommitted" >> "$PRIMARY/README.md"

set +e
dirty_out="$(_maybe_delete_local_branch "feature/issue-502" "$DIRTY_TIP_SHA" 2>&1)"
dirty_rc=$?
set -e

if [[ $dirty_rc -eq 0 ]] \
   && [[ "$dirty_out" == *"checked out in the primary repository checkout"* ]] \
   && [[ "$dirty_out" == *"checkout main"* ]] \
   && [[ "$dirty_out" == *"branch -D feature/issue-502"* ]] \
   && [[ "$(git -C "$PRIMARY" branch --show-current)" == "feature/issue-502" ]] \
   && git -C "$PRIMARY" show-ref --verify --quiet refs/heads/feature/issue-502; then
    pass "(5b) a dirty tree keeps the manual-instructions behavior unchanged, HEAD untouched"
else
    fail "(5b) expected unchanged manual-instructions behavior on a dirty tree; rc=$dirty_rc, out: $dirty_out"
fi
git -C "$PRIMARY" checkout -q -- README.md

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
