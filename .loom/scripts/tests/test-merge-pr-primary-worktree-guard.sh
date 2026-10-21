#!/usr/bin/env bash
# test-merge-pr-primary-worktree-guard.sh - Regression test for #3710.
#
# merge-pr.sh's `_remove_loom_worktree` must NEVER attempt to remove the
# primary/main worktree (the FIRST entry of `git worktree list --porcelain`),
# regardless of a .loom-managed sentinel, the checked-out branch, or a
# customized worktree.root.
#
# The bug (Loom 0.12.0, observed in geode-fem): a repo with a customized
# `worktree.root` has its primary checkout at a "non-standard path" relative to
# that root. Merging a PR whose branch was checked out in the primary reached
# the discovery fallback, which found the primary via _find_worktree_by_branch;
# because the primary carried a .loom-managed sentinel, the code called
# `_remove_loom_worktree` on the main working tree. Git fails safe there, but
# the attempt was a logic error and printed a misleading
# "Removing worktree / Could not remove worktree" pair.
#
# Test strategy (#8191 orchestrator slice 1: the cleanup is `loom-daemon
# merge-pr post-merge-cleanup`; these cases drive it through the REAL
# `_post_merge_cleanup` wrapper extracted from merge-pr.sh, against REAL git
# repos — see lib/post-merge-cleanup-harness.sh):
#   1. Where the guard lives now (Rust), and the shell helpers that were retired.
#   2. The #3710 repro itself: the primary checkout sits at a path that is NOT
#      under a customized worktree.root, carries a .loom-managed sentinel, and
#      has the PR branch checked out. The guard must refuse to remove it and the
#      checkout must survive — via the discovery fallback (what actually
#      happened) and via --worktree-path (allow_unmanaged).
#   3. Control: a genuine SECONDARY Loom-managed worktree IS still removed
#      (the guard is surgical — it only spares the primary).

# Globals assigned below (REPO_ROOT, PR_*, CLEANUP_*, ...) are read by the
# _post_merge_cleanup wrapper the harness extracts from merge-pr.sh, which the
# linter cannot see.
# shellcheck disable=SC2034
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"

# #8191: _maybe_delete_local_branch (called directly and via
# _remove_loom_worktree) now delegates to `loom-daemon merge-pr delete-branch`.
# Pin the binary built from this tree so a stale installed daemon cannot answer
# instead — it would warn-and-keep every branch and fail these cases for the
# wrong reason.
#
# The LEAF verbs too since #8191's worktrees slice: `_primary_worktree_path` is
# now a call to `merge-pr worktree-primary`, and a binary carrying only the
# `merge-pr` group (every daemon since #8124 does) would make Test 4's
# space-containing-path cases and Test 5's control read as logic failures rather
# than as one environment problem.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "merge-pr" \
    "merge-pr post-merge-cleanup" "merge-pr worktree-primary" \
    "merge-pr worktree-contains"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"
YELLOW='\033[0;33m'   # retired() below

# An assertion that CANNOT survive the port to Rust, retired under the
# three-part test in defaults/docs/verification-recipes.md §6. Printed, not
# deleted: a reader must be able to see what was removed, why it could not
# survive, and what proves the property now. Counted as run so the totals stay
# honest.
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

# --- Test 1: where the primary-worktree guard lives now ---
echo "Test 1: the #3710 primary-worktree guard now lives in the Rust orchestrator"

RUST_DIR="$(cd "$SCRIPTS_DIR/../.." && pwd)/loom-daemon/src/merge_pr"
retired "the greps for merge-pr.sh's _primary_worktree_path helper, its '_mp_worktree worktree-primary' / '_mp_worktree remove-gate' delegation, the in-shell 'Refusing to remove the primary/main worktree' text and the 'primary-worktree guard (#3710) could not run' fail-closed warning" \
    "removal of the primary checkout is refused, and refused (not waved through) when the primary lookup cannot run" \
    "#8191 orchestrator slice 1 moved _remove_loom_worktree, _primary_worktree_path and the discovery fallback into loom-daemon (merge_pr/post_merge_cleanup.rs, in-process); the lookup is a function call now, not a subprocess that can fail separately, so the 'lookup could not run' branch no longer exists and none of those names are in merge-pr.sh" \
    "Tests 2-5 below (the REAL wrapper and verb against a real primary checkout: refused via discovery and via --worktree-path; space-containing paths; an unusable daemon removes nothing), loom-daemon/tests/merge_pr_remove_gate_differential.rs (frozen retired block vs the gate), the_primary_checkout_is_never_removed_even_with_a_sentinel in merge_pr/post_merge_cleanup/tests.rs, and the_primary_checkout_is_refused_even_with_sentinel_and_opt_in in merge_pr/remove_gate/tests.rs"
assert_grep 'remove_gate::decide' "$RUST_DIR/post_merge_cleanup.rs" \
    "the orchestrator runs the primary/sentinel gate (remove_gate::decide) before any removal"
assert_grep 'worktrees::primary_path' "$RUST_DIR/remove_gate.rs" \
    "the gate compares against the FIRST worktree entry (worktrees::primary_path)"

# shellcheck source=lib/post-merge-cleanup-harness.sh
source "$SCRIPT_DIR/lib/post-merge-cleanup-harness.sh"
pmc_load "$MERGE_PR"

# --- Build a real git repo matching the #3710 repro scenario ---
echo ""
echo "Test 2: guard refuses to remove the primary/main worktree (real git repo)"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-merge-primary-guard.XXXXXX")"
# Resolve symlinks up front so our comparisons match git's canonical paths.
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf "$TMP_ROOT" 2>/dev/null || true; }
trap cleanup EXIT

# Primary checkout at a NON-standard path (just a plain dir — NOT under the
# customized worktree.root below).
PRIMARY="$TMP_ROOT/primary-checkout"
mkdir -p "$PRIMARY"
git -C "$PRIMARY" init -q
git -C "$PRIMARY" config user.email "test@example.com"
git -C "$PRIMARY" config user.name "Test"
echo "hello" > "$PRIMARY/README.md"
git -C "$PRIMARY" add -A
git -C "$PRIMARY" commit -q -m "initial"
git -C "$PRIMARY" branch -M main

# Customized worktree.root pointing somewhere OTHER than the primary's parent,
# matching the repro (config present but irrelevant to the guard — the guard is
# config-independent by construction).
mkdir -p "$PRIMARY/.loom"
CUSTOM_WT_ROOT="$TMP_ROOT/scratch-wt"
mkdir -p "$CUSTOM_WT_ROOT"
cat > "$PRIMARY/.loom/config.json" <<EOF
{ "worktree": { "root": "$CUSTOM_WT_ROOT" } }
EOF

# (a) Primary carries a .loom-managed sentinel (the trap that made the old code
# think it was safe to remove).
touch "$PRIMARY/.loom-managed"

# (b) The PR branch is checked out IN the primary at merge time.
PR_BRANCH="paper/conformal-v4-audited"
git -C "$PRIMARY" checkout -q -b "$PR_BRANCH"

# REPO_ROOT is the primary (as merge-pr.sh resolves it via find_main_repo_root).
# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"
PR_NUMBER=7

# The reported #3710 path: no worktree at the convention path, so cleanup
# DISCOVERS the PR branch's worktree by `git worktree list` — and finds the
# primary. It must not be removed.
set +e
guard_out="$(_post_merge_cleanup 2>&1)"
guard_rc=$?
set -e

if [[ $guard_rc -eq 0 ]] \
   && [[ "$guard_out" == *"checked out in the primary repository checkout"* ]] \
   && [[ "$guard_out" != *"Removing worktree"* ]]; then
    pass "discovery finds the primary, names it as not removable, and never logs 'Removing worktree'"
else
    fail "discovery should leave the primary alone; rc=$guard_rc, out: $guard_out"
fi

if [[ -d "$PRIMARY" ]] && git -C "$PRIMARY" rev-parse --git-dir >/dev/null 2>&1; then
    pass "primary checkout survives (still a valid git repo)"
else
    fail "primary checkout was removed or corrupted"
fi

# Guard must hold even via the --worktree-path explicit-opt-in path
# (allow_unmanaged=true) — the primary is never removable.
set +e
guard_out2="$(WORKTREE_PATH_OVERRIDE="$PRIMARY" _post_merge_cleanup 2>&1)"
guard_rc2=$?
set -e
if [[ $guard_rc2 -eq 0 ]] \
   && [[ "$guard_out2" == *"Refusing to remove the primary/main worktree"* ]] \
   && [[ "$guard_out2" != *"Removing worktree"* ]] \
   && [[ -d "$PRIMARY" ]]; then
    pass "guard also refuses the primary under --worktree-path (allow_unmanaged=true)"
else
    fail "guard should refuse the primary even with --worktree-path; rc=$guard_rc2, out: $guard_out2"
fi

# --- Test 3: control — a genuine SECONDARY managed worktree IS removed ---
echo ""
echo "Test 3: control — a secondary Loom-managed worktree is still removed"

# Where cleanup looks for issue-999 under the CUSTOM root (the #3530 namespacing
# is the verb's business, so ask the verb rather than re-deriving it here).
SECONDARY="$("$LOOM_DAEMON_SELF_BIN" merge-pr cleanup-paths --repo-root "$PRIMARY" --branch feature/issue-999 --pr 7 | cut -f2)"
mkdir -p "${SECONDARY%/*}"
git -C "$PRIMARY" worktree add -q -b feature/issue-999 "$SECONDARY" >/dev/null 2>&1
touch "$SECONDARY/.loom-managed"

set +e
sec_out="$(pmc_run_issue 999 7 2>&1)"
sec_rc=$?
set -e

if [[ $sec_rc -eq 0 ]] \
   && [[ "$sec_out" == *"Removing worktree"* ]] \
   && [[ "$sec_out" != *"Refusing to remove the primary"* ]] \
   && [[ ! -d "$SECONDARY" ]]; then
    pass "secondary managed worktree is removed (guard is surgical, not blanket)"
else
    fail "secondary should be removed; rc=$sec_rc, dir_exists=$([[ -d "$SECONDARY" ]] && echo yes || echo no), out: $sec_out"
fi

# --- Test 4: space-in-path regression (#3717) ---
# The porcelain `worktree <path>` line is unquoted/unescaped and may contain
# spaces. Parsing with $2 (whitespace-split) truncated at the first space.
echo ""
echo "Test 4: porcelain parsing preserves worktree paths containing spaces (#3717)"

SP_ROOT="$TMP_ROOT/My Repos"     # <-- the space lives here
SP_PRIMARY="$SP_ROOT/repo"
mkdir -p "$SP_PRIMARY"
git -C "$SP_PRIMARY" init -q
git -C "$SP_PRIMARY" config user.email "test@example.com"
git -C "$SP_PRIMARY" config user.name "Test"
echo "hello" > "$SP_PRIMARY/README.md"
git -C "$SP_PRIMARY" add -A
git -C "$SP_PRIMARY" commit -q -m "initial"
git -C "$SP_PRIMARY" branch -M main

# A secondary worktree, also under a space-containing path, NOT at the
# convention path — so cleanup finds it only by walking `git worktree list`.
SP_SECONDARY="$SP_ROOT/wt/issue-3717"
SP_BRANCH="feature/issue-3717"
git -C "$SP_PRIMARY" worktree add -q -b "$SP_BRANCH" "$SP_SECONDARY" >/dev/null 2>&1
SP_PRIMARY_REAL="$(cd "$SP_PRIMARY" && pwd -P)"
SP_SECONDARY_REAL="$(cd "$SP_SECONDARY" && pwd -P)"

# shellcheck disable=SC2034
REPO_ROOT="$SP_PRIMARY"
sp_out="$(pmc_run_issue 3717 8 2>&1)" || true

# (a) the discovery fallback returns the FULL secondary path (user-owned here:
#     no sentinel, so it is advice only and must be left in place).
if [[ "$sp_out" == *"Discovered worktree for branch '$SP_BRANCH' at: $SP_SECONDARY_REAL"* ]] \
   && [[ -d "$SP_SECONDARY" ]]; then
    pass "discovery returns the full space-containing secondary path (and leaves a user-owned worktree alone)"
else
    fail "discovery truncated or removed the space-containing path; out: $sp_out"
fi

# (b) the primary, itself under a space path, is still recognised and spared.
sp_out2="$(WORKTREE_PATH_OVERRIDE="$SP_PRIMARY" _post_merge_cleanup 2>&1)" || true
if [[ "$sp_out2" == *"Refusing to remove the primary/main worktree at $SP_PRIMARY_REAL"* ]] \
   && [[ -d "$SP_PRIMARY" ]]; then
    pass "a space-containing PRIMARY checkout is recognised as the primary and spared"
else
    fail "space-containing primary was not spared; out: $sp_out2"
fi

# (c) --worktree-path's registered-worktree check (the verb merge-pr.sh calls
#     BEFORE cleanup) must accept the full space-containing path.
if git -C "$SP_PRIMARY" worktree list --porcelain 2>/dev/null | \
     "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr worktree-contains --path "$SP_SECONDARY_REAL"; then
    pass "merge-pr worktree-contains accepts a registered space-containing worktree"
else
    fail "merge-pr worktree-contains rejected a registered space-containing worktree"
fi

# (d) Negative control: an unregistered space path must still be rejected.
if git -C "$SP_PRIMARY" worktree list --porcelain 2>/dev/null | \
     "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr worktree-contains --path "$SP_ROOT/not a worktree"; then
    fail "merge-pr worktree-contains wrongly accepted an unregistered path"
else
    pass "merge-pr worktree-contains still rejects an unregistered space-containing path"
fi

# --- Test 5: an unusable daemon removes nothing (fail-closed, #8191) ---
# The cleanup is one verb now, so "the lookup could not run" is "the verb could
# not run": the wrapper must remove NOTHING and say so, and the same worktree
# must be removed by a working binary (the control), so the refusal cannot pass
# by refusing everything for an unrelated reason.
echo ""
echo "Test 5: an unusable loom-daemon removes nothing instead of proceeding"

# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"
CTRL_WT="$("$LOOM_DAEMON_SELF_BIN" merge-pr cleanup-paths --repo-root "$PRIMARY" --branch feature/issue-8191 --pr 9 | cut -f2)"
git -C "$PRIMARY" worktree add -q -b feature/issue-8191 "$CTRL_WT" >/dev/null 2>&1
touch "$CTRL_WT/.loom-managed"

set +e
fc_out="$(LOOM_DAEMON_SELF_BIN="$TMP_ROOT/no-such-loom-daemon" pmc_run_issue 8191 9 2>&1)"
fc_rc=$?
set -e

if [[ $fc_rc -eq 0 ]] \
   && [[ "$fc_out" == *"did not run"* ]] \
   && [[ -d "$CTRL_WT" ]]; then
    pass "an unresolvable loom-daemon removes nothing (worktree left in place, rc 0 — the merge already succeeded)"
else
    fail "expected a refusal that leaves the worktree; rc=$fc_rc, dir_exists=$([[ -d "$CTRL_WT" ]] && echo yes || echo no), out: $fc_out"
fi

set +e
ctrl_out="$(pmc_run_issue 8191 9 2>&1)"
ctrl_rc=$?
set -e
if [[ $ctrl_rc -eq 0 ]] && [[ ! -d "$CTRL_WT" ]] \
   && [[ "$ctrl_out" != *"did not run"* ]]; then
    pass "control: with a working loom-daemon the same worktree IS removed"
else
    fail "control: expected removal; rc=$ctrl_rc, dir_exists=$([[ -d "$CTRL_WT" ]] && echo yes || echo no), out: $ctrl_out"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
