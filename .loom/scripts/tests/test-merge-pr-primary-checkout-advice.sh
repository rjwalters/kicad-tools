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

# #8191: pmc_delete_branch (the harness's stand-in for the retired direct call;
# it runs the whole post-merge cleanup, which ends in `merge-pr delete-branch`) —
# formerly _maybe_delete_local_branch (called directly and via
# _remove_loom_worktree) now delegates to `loom-daemon merge-pr delete-branch`.
# Pin the binary built from this tree so a stale installed daemon cannot answer
# instead — it would warn-and-keep every branch and fail these cases for the
# wrong reason.
#
# #8191 slice: the porcelain lookups this suite extracts (_primary_worktree_path
# / _is_primary_worktree_path / _worktree_branch_for / _find_worktree_by_branch)
# now delegate to `loom-daemon merge-pr worktree-*`, so the LEAF verbs are
# checked too — a binary with only the `merge-pr` group predates this slice and
# would make every lookup fail, which the #3710 guard turns into "refuse to
# clean up anything at all": a whole-suite failure that reads as broken logic
# rather than as one stale binary.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "merge-pr" \
    "merge-pr worktree-primary" "merge-pr worktree-branch-for" \
    "merge-pr worktree-find-by-branch"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# An assertion that CANNOT survive the port to Rust, retired under the
# three-part test in defaults/docs/verification-recipes.md §6. Printed, not
# deleted: a reader must be able to see what was removed, why, and what proves
# the property now. Counted as run so the totals stay honest.
YELLOW='\033[0;33m'
retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
}

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

[[ -f "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not found" >&2; exit 1; }

# --- Test 1: source retains the #4171 primary-checkout distinction ---
echo "Test 1: merge-pr.sh source contains the primary-checkout helper and call sites"

retired \
    "the greps for merge-pr.sh's _is_primary_worktree_path helper and its '_is_primary_worktree_path \"\$DISCOVERED_WT\"' call site" \
    "the discovery fallback distinguishes the primary checkout from a linked worktree (#4171)" \
    "#8191 orchestrator slice 1 moved the discovery fallback into loom-daemon (merge_pr/post_merge_cleanup.rs::discovery_fallback, in-process); merge-pr.sh no longer has the helper or the call site" \
    "Test 2 below, which drives the REAL wrapper and verb against a real primary checkout and requires the primary-specific message, plus post_merge_cleanup/tests.rs::a_discovered_user_owned_worktree_is_advice_only"
retired \
    "the grep for the in-shell 'not a removable worktree' text" \
    "the discovery fallback reports the primary checkout as not removable and never suggests worktree removal (#4171)" \
    "#8191: the classification and its message text left merge-pr.sh for 'loom-daemon merge-pr discovered-worktree' (loom-daemon/src/merge_pr/discovered_worktree.rs), so no grep of this file can pass" \
    "loom-daemon/tests/merge_pr_discovered_worktree_differential.rs (frozen retired block vs the verb, primary case byte for byte) and the_primary_checkout_is_noted_and_never_offers_removal_advice in src/merge_pr/discovered_worktree/tests.rs"
retired \
    "_maybe_delete_local_branch resolves the branch's checkout location" \
    "a checked-out refusal is attributed to WHERE the branch is checked out, so the primary checkout gets primary-specific advice (#4171)" \
    "#8191: the refusal handling left the shell. _maybe_delete_local_branch calls 'loom-daemon merge-pr delete-branch', and the location lookup is worktree_cli::branch_delete::handle_checked_out's find_worktree_by_branch — there is no checkout_loc assignment in merge-pr.sh to grep for" \
    "Test 3 below, which runs the real function against a real primary checkout and requires the primary-specific message, plus Test 5a/5b"
retired \
    "_maybe_delete_local_branch prints the two-step checkout+delete advice" \
    "the primary-checkout refusal prints the exact 'git -C <dir> checkout <default> && git -C <dir> branch -D <branch>' remediation" \
    "#8191: that message is now worded by worktree_cli::branch_delete (Rust) and replayed by the shell from a WARNING<TAB>message line; merge-pr.sh never spells '\$checkout_loc' or '\$default_label' again" \
    "Test 3 below, which asserts the replayed output contains 'checkout main' and 'branch -D <branch>' behaviourally — stronger than a grep, since it proves the text reaches the operator"

# shellcheck source=lib/post-merge-cleanup-harness.sh
source "$SCRIPT_DIR/lib/post-merge-cleanup-harness.sh"
pmc_load "$MERGE_PR"

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
# Post-merge cleanup appends to <repo>/.loom/logs/worktree-removals.log (the
# removal ledger, #5950), which every Loom repo gitignores. Do the same here so
# that bookkeeping does not read as "dirty primary tree" to the #5015 check.
printf '.loom/\n' >> "$PRIMARY/.git/info/exclude"

# The merged PR's branch, checked out directly in the primary — NO
# .loom-managed sentinel here (this is the real-world case: the primary
# checkout is never Loom-managed).
PR_BRANCH="config/enable-collision-detection"
git -C "$PRIMARY" checkout -q -b "$PR_BRANCH"
# One commit that never reaches main, so the branch is genuinely UNLANDED
# (#7812): the `-d` -> `-D` upgrade is keyed on whether the default branch
# already contains the work, not on whether the caller passed a head SHA, so a
# branch parked exactly at main's tip would now be force-deletable and this
# test would exercise the auto-cleanup path instead of the advice path.
echo "collision detection" >> "$PRIMARY/README.md"
git -C "$PRIMARY" commit -q -am "unlanded work on the PR branch"

# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"
# shellcheck disable=SC2034
DEFAULT_BRANCH_NAME="main"

# The discovery fallback, run for real: no worktree at the convention path, so
# cleanup walks `git worktree list`, finds the PRIMARY itself, and must NEVER
# suggest `git worktree remove` / `--worktree-path` for it (git refuses to
# remove a primary working tree, so that advice can never work).
disc_out="$(pmc_delete_branch "$PR_BRANCH" 2>&1)" || true
if [[ "$disc_out" == *"checked out in the primary repository checkout ($PRIMARY) — not a removable worktree."* ]] \
   && [[ "$disc_out" != *"git worktree remove"* ]] \
   && [[ "$disc_out" != *"--worktree-path"* ]]; then
    pass "discovery finds the primary checkout and never suggests git worktree remove / --worktree-path for it"
else
    fail "discovery-fallback should name the primary and avoid worktree-remove advice; got: $disc_out"
fi

echo ""
echo "Test 3: branch-delete failure in the primary checkout gives the exact two-step advice"

set +e
branch_out="$(pmc_delete_branch "$PR_BRANCH" 2>&1)"
branch_rc=$?
set -e

if [[ $branch_rc -eq 0 ]] \
   && [[ "$branch_out" == *"checked out in the primary repository checkout"* ]] \
   && [[ "$branch_out" == *"checkout main"* ]] \
   && [[ "$branch_out" == *"branch -D $PR_BRANCH"* ]]; then
    pass "post-merge cleanup prints the exact two-step 'git checkout main && ... branch -D' advice"
else
    fail "post-merge cleanup: expected two-step advice; rc=$branch_rc, out: $branch_out"
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

set +e
sec_out="$(pmc_run_issue 999 2>&1)"
sec_rc=$?
set -e
if [[ $sec_rc -eq 0 ]] \
   && [[ "$sec_out" == *"Removing worktree"* ]] \
   && [[ "$sec_out" != *"not a removable worktree"* ]] \
   && [[ ! -d "$SECONDARY" ]]; then
    pass "genuine Loom-managed secondary worktree (found by discovery) is still auto-removed, and is not mistaken for the primary"
else
    fail "secondary should still be removed; rc=$sec_rc, out: $sec_out"
fi

# (b) Genuine user-owned linked worktree (no sentinel, NOT the primary): the
# original "lacks sentinel — not removing" + git-worktree-remove advice is
# still correct there and must be preserved by the simulated decision tree.
USER_WT="$TMP_ROOT/scratch/user-owned"
git -C "$PRIMARY" worktree add -q -b feature/user-owned "$USER_WT" >/dev/null 2>&1

user_out="$(pmc_delete_branch feature/user-owned 2>&1)" || true
if [[ "$user_out" != *"not a removable worktree"* ]] \
   && [[ "$user_out" == *"git worktree remove '$USER_WT'"* ]] \
   && [[ "$user_out" == *"--worktree-path '$USER_WT'"* ]] \
   && [[ -d "$USER_WT" ]]; then
    pass "genuine user-owned linked worktree keeps the git-worktree-remove advice (unchanged), and is left in place"
else
    fail "user-owned worktree advice regressed: $user_out"
fi

# --- Test 5: primary-checkout auto-cleanup when it is PROVABLY safe (#5015) ---
#
# Test 3 above only exercises the "print manual instructions" path because its
# branch is genuinely unlanded, so the `-d` -> `-D` upgrade never engages
# (#7812). This distinguishes that case from the
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
clean_out="$(pmc_delete_branch "feature/issue-501" "$CLEAN_TIP_SHA" 2>&1)"
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
dirty_out="$(pmc_delete_branch "feature/issue-502" "$DIRTY_TIP_SHA" 2>&1)"
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
