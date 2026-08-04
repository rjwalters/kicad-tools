#!/usr/bin/env bash
# test-merge-pr-dirty-worktree-guard.sh - Regression test for #5031.
#
# merge-pr.sh's post-merge worktree cleanup (`_remove_loom_worktree`) keys the
# worktree to remove ONLY by branch name (feature/issue-<N>). The worktree.sh
# naming convention makes that name collide deterministically whenever two
# hosts/sessions independently claim the same issue number. When a *different*,
# still-live builder has that branch checked out with UNCOMMITTED edits, the old
# `git worktree remove --force` silently destroyed them.
#
# Live incident (2026-08-03, #5001): `/loom:sweep 5001` was mid-edit in
# .loom/worktrees/issue-5001 on branch feature/issue-5001 (no commit yet) when a
# *separate* host pushed the same branch name and opened PR #5026. Champion
# merged #5026; merge-pr.sh's cleanup force-removed feature/issue-5001's
# worktree — this session's live, actively-edited one — destroying the
# never-committed edits (unrecoverable).
#
# The fix: before the `git worktree remove --force`, refuse-and-warn if the
# worktree has uncommitted changes (real work, not Loom's own gitignored runtime
# markers). The merge already succeeded, so skipping cleanup is a safe no-op for
# the merge while the sibling's work survives on disk.
#
# NOT to be re-conflated in triage:
#   - #4463 (closed): same-HOST duplicate dispatch, fixed lock *ownership*
#     (a lock is not stolen from a live sweep). About lock records, not
#     worktree/branch-name collision — would not have caught #5031.
#   - #4146 (open, loom:operator-only): *measures* the cross-host collision rate
#     (observation only). This guard is the behavioral mitigation #4146 is meant
#     to eventually quantify.
#
# Test strategy (mirrors test-merge-pr-primary-worktree-guard.sh):
#   1. Static grep assertions that the live source retains the guard.
#   2. Behavioral tests against REAL git repos, running the ACTUAL function
#      bodies extracted from merge-pr.sh (no drift):
#      (a) a Loom-managed worktree with UNCOMMITTED tracked changes -> refused,
#          the worktree AND its uncommitted change survive on disk.
#      (b) a Loom-managed worktree with a NEW untracked source file -> refused,
#          survives (untracked work is real work too).
#      (c) control: a CLEAN Loom-managed worktree -> still removed (the guard is
#          surgical, not a blanket refuse — protects the #4100 cleanup path).
#      (d) a worktree whose ONLY dirt is Loom's own runtime markers
#          (.loom-managed etc.) -> still removed (markers are never user work).

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

# --- Test 1: source retains the #5031 dirty-worktree guard ---
echo "Test 1: merge-pr.sh source retains the #5031 dirty-worktree data-loss guard"

assert_grep 'data-loss guard, #5031' "$MERGE_PR" \
    "_remove_loom_worktree warns with the #5031 data-loss-guard message"
assert_grep 'git -C "\$worktree_path" status --porcelain' "$MERGE_PR" \
    "_remove_loom_worktree inspects the worktree's porcelain status before removal"
assert_grep 'Refusing to remove worktree at \$worktree_path' "$MERGE_PR" \
    "_remove_loom_worktree refuses removal when uncommitted changes are present"
# The refusal must sit BEFORE the actual removal, not after.
guard_line="$(grep -n 'Refusing to remove worktree at \$worktree_path' "$MERGE_PR" | head -1 | cut -d: -f1)"
remove_line="$(grep -n 'git -C "\$REPO_ROOT" worktree remove "\$worktree_path" --force' "$MERGE_PR" | head -1 | cut -d: -f1)"
if [[ -n "$guard_line" && -n "$remove_line" && "$guard_line" -lt "$remove_line" ]]; then
    pass "the dirty-worktree refusal precedes the force-remove call"
else
    fail "guard must precede force-remove (guard@$guard_line remove@$remove_line)"
fi
# Cross-reference note so triage doesn't re-conflate #5031 / #4463 / #4146.
assert_grep '#4463' "$MERGE_PR" "source documents the distinction from #4463 (same-host lock ownership)"
assert_grep '#4146' "$MERGE_PR" "source documents the interaction with #4146 (cross-host collision measurement)"

# --- Extract the ACTUAL function bodies from the live source (no drift) ---
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

eval "$(extract_fn _primary_worktree_path "$MERGE_PR")"
eval "$(extract_fn _worktree_branch_for   "$MERGE_PR")"
eval "$(extract_fn _maybe_delete_local_branch "$MERGE_PR")"
eval "$(extract_fn _remove_loom_worktree  "$MERGE_PR")"

# --- Build a real git repo with a secondary Loom-managed worktree ---
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-merge-dirty-guard.XXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf "$TMP_ROOT" 2>/dev/null || true; }
trap cleanup EXIT

PRIMARY="$TMP_ROOT/repo"
mkdir -p "$PRIMARY"
git -C "$PRIMARY" init -q
git -C "$PRIMARY" config user.email "test@example.com"
git -C "$PRIMARY" config user.name "Test"
echo "hello" > "$PRIMARY/README.md"
git -C "$PRIMARY" add -A
git -C "$PRIMARY" commit -q -m "initial"
git -C "$PRIMARY" branch -M main

# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"

# Helper: create a fresh secondary Loom-managed worktree on feature/issue-N.
make_worktree() { # <name> -> echoes the worktree path
    local name="$1"
    local wt="$TMP_ROOT/wt-$name"
    git -C "$PRIMARY" worktree add -q -b "feature/issue-$name" "$wt" >/dev/null 2>&1
    touch "$wt/.loom-managed"   # the sentinel worktree.sh drops
    echo "$wt"
}

# --- Test 2 (a): uncommitted TRACKED change -> refused, work survives ---
echo ""
echo "Test 2: dirty worktree with an uncommitted tracked change is NOT removed"

WT_A="$(make_worktree 5001a)"
# Simulate the live sibling builder mid-edit: modify a tracked file, no commit.
echo "in-flight edit that must not be lost" >> "$WT_A/README.md"

set +e
out_a="$(_remove_loom_worktree "$WT_A" 2>&1)"
rc_a=$?
set -e

if [[ $rc_a -eq 0 ]] \
   && [[ "$out_a" == *"Refusing to remove worktree"* ]] \
   && [[ "$out_a" == *"data-loss guard, #5031"* ]] \
   && [[ "$out_a" != *"Removing worktree"* ]] \
   && [[ -d "$WT_A" ]] \
   && grep -q "in-flight edit that must not be lost" "$WT_A/README.md"; then
    pass "(a) dirty worktree refused; directory and uncommitted edit survive on disk"
else
    fail "(a) expected refuse-and-survive; rc=$rc_a, dir=$([[ -d "$WT_A" ]] && echo yes || echo no), out: $out_a"
fi

# The warning should name the colliding branch so the message is actionable.
if [[ "$out_a" == *"feature/issue-5001a"* ]]; then
    pass "(a) refusal warning names the colliding branch"
else
    fail "(a) refusal warning should name the branch; out: $out_a"
fi

# --- Test 2 (b): NEW untracked source file -> refused, survives ---
echo ""
echo "Test 3: dirty worktree with a new untracked source file is NOT removed"

WT_B="$(make_worktree 5001b)"
echo "brand new uncommitted module" > "$WT_B/new_module.py"

set +e
out_b="$(_remove_loom_worktree "$WT_B" 2>&1)"
rc_b=$?
set -e

if [[ $rc_b -eq 0 ]] \
   && [[ "$out_b" == *"Refusing to remove worktree"* ]] \
   && [[ -d "$WT_B" ]] \
   && [[ -f "$WT_B/new_module.py" ]]; then
    pass "(b) untracked new-file work is treated as dirty; worktree survives"
else
    fail "(b) expected refuse-and-survive for untracked work; rc=$rc_b, out: $out_b"
fi

# --- Test 4 (c): CLEAN worktree -> still removed (guard is surgical) ---
echo ""
echo "Test 4: control — a clean Loom-managed worktree is still removed"

WT_C="$(make_worktree 5001c)"   # only the untracked .loom-managed marker

set +e
out_c="$(_remove_loom_worktree "$WT_C" 2>&1)"
rc_c=$?
set -e

if [[ $rc_c -eq 0 ]] \
   && [[ "$out_c" == *"Removing worktree"* ]] \
   && [[ "$out_c" != *"Refusing to remove worktree"* ]] \
   && [[ ! -d "$WT_C" ]]; then
    pass "(c) clean worktree is removed (guard does not regress the #4100 cleanup path)"
else
    fail "(c) clean worktree should be removed; rc=$rc_c, dir=$([[ -d "$WT_C" ]] && echo yes || echo no), out: $out_c"
fi

# --- Test 5 (d): only Loom runtime markers are dirty -> still removed ---
echo ""
echo "Test 5: worktree dirtied ONLY by Loom's own runtime markers is still removed"

WT_D="$(make_worktree 5001d)"
# Drop the full spread of Loom runtime markers — none represent user work.
touch "$WT_D/.loom-in-use" "$WT_D/.loom-checkpoint" "$WT_D/.no-changes-needed"
mkdir -p "$WT_D/.snapshots"
echo "patch" > "$WT_D/.snapshots/issue-5001d-123.patch"

set +e
out_d="$(_remove_loom_worktree "$WT_D" 2>&1)"
rc_d=$?
set -e

if [[ $rc_d -eq 0 ]] \
   && [[ "$out_d" == *"Removing worktree"* ]] \
   && [[ "$out_d" != *"Refusing to remove worktree"* ]] \
   && [[ ! -d "$WT_D" ]]; then
    pass "(d) Loom runtime markers alone do not block cleanup"
else
    fail "(d) marker-only dirt should still be removed; rc=$rc_d, dir=$([[ -d "$WT_D" ]] && echo yes || echo no), out: $out_d"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
