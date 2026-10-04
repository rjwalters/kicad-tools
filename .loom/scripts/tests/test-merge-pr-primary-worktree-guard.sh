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
# Test strategy:
#   1. Static grep assertions that the live source retains the guard (protects
#      against a regression that deletes the guard).
#   2. Behavioral test against a REAL git repo, running the ACTUAL function
#      bodies extracted from merge-pr.sh (no drift): the primary checkout sits
#      at a path that is NOT under a customized worktree.root, carries a
#      .loom-managed sentinel, and has the PR branch checked out. The guard
#      must refuse to remove it and the checkout must survive.
#   3. Control: a genuine SECONDARY Loom-managed worktree IS still removed
#      (the guard is surgical — it only spares the primary).

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
    "merge-pr worktree-primary" "merge-pr worktree-branch-for" \
    "merge-pr worktree-find-by-branch" "merge-pr worktree-contains"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'   # retired() below
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

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

# --- Test 1: source retains the primary-worktree guard ---
echo "Test 1: merge-pr.sh source retains the #3710 primary-worktree guard"

assert_grep '_primary_worktree_path\(\) \{' "$MERGE_PR" \
    "merge-pr.sh defines the _primary_worktree_path helper"
retired "the awk-body grep for _primary_worktree_path's parse" \
    "the primary path is the FIRST worktree entry, parsed space-safely (substr, not \$2 — #3717)" \
    "the awk left merge-pr.sh in the #8191 slice; the parse is Rust in loom-daemon/src/merge_pr/worktrees.rs, so no grep of this file can pass" \
    "Test 4 below (a space-containing primary checkout, behavioural, unchanged), plus loom-daemon/tests/merge_pr_worktrees_differential.rs — which compares the port against a FROZEN copy of that exact awk body, including space-containing-path corpus entries — and primary_is_the_first_worktree_record / paths_containing_spaces_survive_intact in src/merge_pr/worktrees/tests.rs"
assert_grep '_mp_worktree worktree-primary' "$MERGE_PR" \
    "_primary_worktree_path delegates the parse to 'merge-pr worktree-primary'"
retired "the grep for the in-shell 'Refusing to remove the primary/main worktree' text and primary_real comparison" \
    "_remove_loom_worktree refuses when the target resolves to the primary" \
    "the comparison and its message left merge-pr.sh in the #8191 remove-gate slice; they are Rust in loom-daemon/src/merge_pr/remove_gate.rs, so no grep of this file can pass" \
    "Tests 2-3 below (behavioural: the real _remove_loom_worktree, the real binary, a real primary checkout refused), loom-daemon/tests/merge_pr_remove_gate_differential.rs (frozen retired block vs the verb), and the_primary_checkout_is_refused_even_with_sentinel_and_opt_in in src/merge_pr/remove_gate/tests.rs"
assert_grep '_mp_worktree remove-gate' "$MERGE_PR" \
    "_remove_loom_worktree delegates the primary/sentinel gate to 'merge-pr remove-gate'"
# The #8191 slice added a SECOND refusal: when the lookup itself cannot run, the
# removal is refused rather than proceeding on an empty answer (which the
# comparison above would otherwise read as "not the primary").
assert_grep 'the primary-worktree guard \(#3710\) could not run' "$MERGE_PR" \
    "_remove_loom_worktree fails CLOSED when the primary lookup cannot run"

# --- Extract the ACTUAL function bodies from the live source (no drift) ---
# Grabs from `name() {` to the first line beginning with `}` (column 0). All
# four helpers close their brace at column 0 with no intervening column-0 `}`.
extract_fn() {
    local name="$1" file="$2"
    awk -v fn="$name" '
      $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
      grab { print }
      grab && /^}/ { exit }
    ' "$file"
}

# Harness: stub the logging helpers and REPO_ROOT, then eval the real bodies.
info()    { echo "INFO: $*"; }
warning() { echo "WARN: $*"; }
success() { echo "OK: $*"; }
error()   { echo "ERROR: $*" >&2; return 1; }

# #8191 slice: the porcelain lookups below shell out through _mp_worktree, so it
# is extracted with them — without it they die with "_mp_worktree: command not
# found" under `set -e`.
eval "$(extract_fn _mp_worktree           "$MERGE_PR")"
eval "$(extract_fn _primary_worktree_path "$MERGE_PR")"
eval "$(extract_fn _worktree_branch_for   "$MERGE_PR")"
# #7812: _maybe_delete_local_branch's `-d` -> `-D` safety check is now the
# shared `branch_landed` primitive — a real library, so it is SOURCED here
# rather than extracted. Offline: these cases exercise merge-pr.sh's local
# branch logic, not the forge rung, and the suite must stay hermetic.
export LOOM_BRANCH_LANDED_OFFLINE=1
# shellcheck source=../lib/branch-landed.sh
source "$(dirname "$MERGE_PR")/lib/branch-landed.sh"
eval "$(extract_fn _maybe_delete_local_branch "$MERGE_PR")"
eval "$(extract_fn _remove_loom_worktree  "$MERGE_PR")"

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
# Consumed by the eval'd _primary_worktree_path / _remove_loom_worktree bodies.
# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"

# Sanity: the primary is the first porcelain entry, and our helper agrees.
primary_reported="$(_primary_worktree_path)"
if [[ "$primary_reported" == "$PRIMARY" ]]; then
    pass "_primary_worktree_path reports the primary checkout ($PRIMARY)"
else
    fail "_primary_worktree_path: expected '$PRIMARY', got '$primary_reported'"
fi

# The actual guard: attempt to remove the primary. Must refuse, and the
# checkout must survive as a valid git repo.
set +e
guard_out="$(_remove_loom_worktree "$PRIMARY" 2>&1)"
guard_rc=$?
set -e

if [[ $guard_rc -eq 0 ]] \
   && [[ "$guard_out" == *"Refusing to remove the primary/main worktree"* ]] \
   && [[ "$guard_out" != *"Removing worktree"* ]]; then
    pass "guard refuses the primary and never logs 'Removing worktree'"
else
    fail "guard should refuse the primary; rc=$guard_rc, out: $guard_out"
fi

if [[ -d "$PRIMARY" ]] && git -C "$PRIMARY" rev-parse --git-dir >/dev/null 2>&1; then
    pass "primary checkout survives (still a valid git repo)"
else
    fail "primary checkout was removed or corrupted"
fi

# Guard must hold even via the --worktree-path explicit-opt-in path
# (allow_unmanaged=true) — the primary is never removable.
set +e
guard_out2="$(_remove_loom_worktree "$PRIMARY" "true" 2>&1)"
guard_rc2=$?
set -e
if [[ $guard_rc2 -eq 0 ]] \
   && [[ "$guard_out2" == *"Refusing to remove the primary/main worktree"* ]] \
   && [[ -d "$PRIMARY" ]]; then
    pass "guard also refuses the primary under allow_unmanaged=true (--worktree-path)"
else
    fail "guard should refuse the primary even with allow_unmanaged=true; rc=$guard_rc2, out: $guard_out2"
fi

# --- Test 3: control — a genuine SECONDARY managed worktree IS removed ---
echo ""
echo "Test 3: control — a secondary Loom-managed worktree is still removed"

SECONDARY="$CUSTOM_WT_ROOT/issue-999"
git -C "$PRIMARY" worktree add -q -b feature/issue-999 "$SECONDARY" >/dev/null 2>&1
touch "$SECONDARY/.loom-managed"

set +e
sec_out="$(_remove_loom_worktree "$SECONDARY" 2>&1)"
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
# spaces. Parsing with $2 (whitespace-split) truncates at the first space; the
# fix parses via substr($0, 10) (strip the literal `worktree ` prefix). Assert
# every porcelain-parsing helper resolves the FULL space-containing path, plus
# the --worktree-path registered-worktree validation snippet.
echo ""
echo "Test 4: porcelain parsing preserves worktree paths containing spaces (#3717)"

# Extract _find_worktree_by_branch too (used below); other bodies already eval'd.
eval "$(extract_fn _find_worktree_by_branch "$MERGE_PR")"

SP_ROOT="$TMP_ROOT/My Repos"     # <-- the space lives here
SP_PRIMARY="$SP_ROOT/repo"
mkdir -p "$SP_PRIMARY"
git -C "$SP_PRIMARY" init -q
git -C "$SP_PRIMARY" config user.email "test@example.com"
git -C "$SP_PRIMARY" config user.name "Test"
echo "hello" > "$SP_PRIMARY/README.md"
git -C "$SP_PRIMARY" add -A
git -C "$SP_PRIMARY" commit -q -m "initial"

# A secondary worktree, also under a space-containing path.
SP_SECONDARY="$SP_ROOT/wt/issue-3717"
SP_BRANCH="feature/issue-3717"
git -C "$SP_PRIMARY" worktree add -q -b "$SP_BRANCH" "$SP_SECONDARY" >/dev/null 2>&1

# Resolve canonical (symlink-free) forms to compare against git's output.
SP_PRIMARY_REAL="$(cd "$SP_PRIMARY" && pwd -P)"
SP_SECONDARY_REAL="$(cd "$SP_SECONDARY" && pwd -P)"

# Point the eval'd helpers at the space-containing primary.
# shellcheck disable=SC2034
REPO_ROOT="$SP_PRIMARY"

# (a) _primary_worktree_path returns the FULL primary path (not truncated at
#     "My").
sp_primary="$(_primary_worktree_path)"
if [[ "$sp_primary" == "$SP_PRIMARY_REAL" ]]; then
    pass "_primary_worktree_path returns the full space-containing path"
else
    fail "_primary_worktree_path truncated: expected '$SP_PRIMARY_REAL', got '$sp_primary'"
fi

# (b) _find_worktree_by_branch returns the FULL secondary path.
sp_found="$(_find_worktree_by_branch "$SP_BRANCH")"
if [[ "$sp_found" == "$SP_SECONDARY_REAL" ]]; then
    pass "_find_worktree_by_branch returns the full space-containing secondary path"
else
    fail "_find_worktree_by_branch truncated: expected '$SP_SECONDARY_REAL', got '$sp_found'"
fi

# (c) _worktree_branch_for resolves the correct branch short-name from the
#     full space-containing path.
sp_branch="$(_worktree_branch_for "$SP_SECONDARY_REAL")"
if [[ "$sp_branch" == "$SP_BRANCH" ]]; then
    pass "_worktree_branch_for resolves the branch from a space-containing path"
else
    fail "_worktree_branch_for failed on space path: expected '$SP_BRANCH', got '$sp_branch'"
fi

# (d) --worktree-path's registered-worktree check (#8191 slice: `loom-daemon
#     merge-pr worktree-contains`, the ACTUAL ported verb merge-pr.sh now
#     calls — not a hand-copied re-implementation of the retired awk) must
#     accept the full space-containing path.
if git -C "$SP_PRIMARY" worktree list --porcelain 2>/dev/null | \
     "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr worktree-contains --path "$SP_SECONDARY_REAL"; then
    pass "merge-pr worktree-contains accepts a registered space-containing worktree"
else
    fail "merge-pr worktree-contains rejected a registered space-containing worktree"
fi

# (e) Negative control: an unregistered space path must still be rejected.
if git -C "$SP_PRIMARY" worktree list --porcelain 2>/dev/null | \
     "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr worktree-contains --path "$SP_ROOT/not a worktree"; then
    fail "merge-pr worktree-contains wrongly accepted an unregistered path"
else
    pass "merge-pr worktree-contains still rejects an unregistered space-containing path"
fi

# --- Test 5: the guard fails CLOSED when its lookup cannot run (#8191) ---
# Porting the parse out of awk introduced a failure mode awk did not have: the
# binary can be missing, unreadable, or predate the subcommand. The empty string
# that produces is indistinguishable from "git reported no worktrees", and the
# guard reads an empty primary path as "the target is not the primary checkout"
# — i.e. as permission to remove it. So an unusable binary must reach
# _remove_loom_worktree as a non-zero return and become a REFUSAL, not an empty
# answer. Verified on a genuine secondary worktree with the real binary as the
# control, so this cannot pass by refusing everything for an unrelated reason.
echo ""
echo "Test 5: an unusable loom-daemon refuses the removal instead of proceeding"

# Back to the plain (space-free) primary for this case.
# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"
CTRL_WT="$CUSTOM_WT_ROOT/issue-8191"
git -C "$PRIMARY" worktree add -q -b feature/issue-8191 "$CTRL_WT" >/dev/null 2>&1
touch "$CTRL_WT/.loom-managed"

set +e
( export LOOM_DAEMON_SELF_BIN="$TMP_ROOT/no-such-loom-daemon" LOOM_DAEMON_BIN="$TMP_ROOT/no-such-loom-daemon"
  _remove_loom_worktree "$CTRL_WT" ) >"$TMP_ROOT/failclosed.out" 2>&1
fc_rc=$?
set -e
fc_out="$(cat "$TMP_ROOT/failclosed.out")"

if [[ $fc_rc -eq 0 ]] \
   && [[ "$fc_out" == *"primary-worktree guard (#3710) could not run"* ]] \
   && [[ -d "$CTRL_WT" ]]; then
    pass "an unresolvable loom-daemon refuses the removal (worktree left in place, rc 0 — the merge already succeeded)"
else
    fail "expected a refusal that leaves the worktree; rc=$fc_rc, dir_exists=$([[ -d "$CTRL_WT" ]] && echo yes || echo no), out: $fc_out"
fi

# Control: with the real binary back, the SAME worktree IS removed — so the
# refusal above is about the unusable binary and nothing else.
set +e
ctrl_out="$(_remove_loom_worktree "$CTRL_WT" 2>&1)"
ctrl_rc=$?
set -e
if [[ $ctrl_rc -eq 0 ]] && [[ ! -d "$CTRL_WT" ]] \
   && [[ "$ctrl_out" != *"could not run"* ]]; then
    pass "control: with a working loom-daemon the same worktree IS removed"
else
    fail "control: expected removal; rc=$ctrl_rc, dir_exists=$([[ -d "$CTRL_WT" ]] && echo yes || echo no), out: $ctrl_out"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
