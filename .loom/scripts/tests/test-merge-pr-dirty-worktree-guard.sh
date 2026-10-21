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

# #8191: _maybe_delete_local_branch (called directly and via
# _remove_loom_worktree) now delegates to `loom-daemon merge-pr delete-branch`.
# Pin the binary built from this tree so a stale installed daemon cannot answer
# instead — it would warn-and-keep every branch and fail these cases for the
# wrong reason.
#
# #8191 slice: the porcelain lookups this suite extracts (_primary_worktree_path
# / _worktree_branch_for) now delegate to `loom-daemon merge-pr worktree-*`, so
# the LEAF verbs are checked too — a binary with only the `merge-pr` group
# predates this slice and would make every lookup fail, which the #3710 guard
# turns into "refuse to clean up anything at all": a whole-suite failure that
# reads as broken logic rather than as one stale binary.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "merge-pr" \
    "merge-pr worktree-primary" "merge-pr worktree-branch-for" \
    "merge-pr worktree-find-by-branch" "merge-pr worktree-teardown"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"
YELLOW='\033[1;33m'

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

# --- Test 1: source retains the #5031 dirty-worktree guard ---
echo "Test 1: merge-pr.sh source retains the #5031 dirty-worktree data-loss guard"

RUST_DIR="$(cd "$SCRIPTS_DIR/../.." && pwd)/loom-daemon/src/merge_pr"
retired "the three greps over _remove_loom_worktree's source (#5031 message, 'git -C \$worktree_path status --porcelain', 'Refusing to remove worktree at \$worktree_path') and the guard-line < remove-line ordering check" \
    "the dirty-worktree refusal exists, reads git's porcelain status, and precedes the force-remove" \
    "#8191 orchestrator slice 1 moved _remove_loom_worktree into loom-daemon (merge_pr/post_merge_cleanup.rs, in-process), so merge-pr.sh no longer contains those lines and no grep of it can pass; ordering is now code order in one Rust function, not text order in a 600-line shell block" \
    "Tests 2-9 below (REAL verb, real git repos: dirty worktrees refused and surviving; the guard's message names the branch and the files), post_merge_cleanup/tests.rs::uncommitted_work_refuses_the_removal_and_names_the_remediation (teardown and ledger never called when dirty; the branch delete still runs) and ::loom_runtime_markers_alone_do_not_block_removal, and the grep below that pins where the git status read lives"
assert_grep 'git_stdout\(&\["-C", path, "status", "--porcelain"\]\)' "$RUST_DIR/post_merge_cleanup.rs" \
    "the orchestrator inspects the worktree's porcelain status before removal"
assert_grep 'dirty_guard::assess' "$RUST_DIR/post_merge_cleanup.rs" \
    "the orchestrator refuses removal through the #5031 dirty_guard::assess decision"
# Cross-reference note so triage doesn't re-conflate #5031 / #4463 / #4146.
assert_grep '#4463' "$RUST_DIR/post_merge_cleanup.rs" "source documents the distinction from #4463 (same-host lock ownership)"
assert_grep '#4146' "$RUST_DIR/post_merge_cleanup.rs" "source documents the interaction with #4146 (cross-host collision measurement)"

# shellcheck source=lib/post-merge-cleanup-harness.sh
source "$SCRIPT_DIR/lib/post-merge-cleanup-harness.sh"
pmc_load "$MERGE_PR"

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

# Helper: create a fresh Loom-managed worktree at the CONVENTION path
# (<repo>/.loom/worktrees/issue-N) on feature/issue-N — where post-merge
# cleanup looks for it (N must be numeric: the strict feature/issue-<N> rule).
make_worktree() { # <name> -> echoes the worktree path
    local name="$1"
    local wt="$PRIMARY/.loom/worktrees/issue-$name"
    mkdir -p "$PRIMARY/.loom/worktrees"
    git -C "$PRIMARY" worktree add -q -b "feature/issue-$name" "$wt" >/dev/null 2>&1
    touch "$wt/.loom-managed"   # the sentinel worktree.sh drops
    echo "$wt"
}

# --- Test 2 (a): uncommitted TRACKED change -> refused, work survives ---
echo ""
echo "Test 2: dirty worktree with an uncommitted tracked change is NOT removed"

WT_A="$(make_worktree 500101)"
# Simulate the live sibling builder mid-edit: modify a tracked file, no commit.
echo "in-flight edit that must not be lost" >> "$WT_A/README.md"

set +e
out_a="$(pmc_run_issue "${WT_A##*issue-}" 2>&1)"
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
if [[ "$out_a" == *"feature/issue-500101"* ]]; then
    pass "(a) refusal warning names the colliding branch"
else
    fail "(a) refusal warning should name the branch; out: $out_a"
fi

# --- Test 2 (b): NEW untracked source file -> refused, survives ---
echo ""
echo "Test 3: dirty worktree with a new untracked source file is NOT removed"

WT_B="$(make_worktree 500102)"
echo "brand new uncommitted module" > "$WT_B/new_module.py"

set +e
out_b="$(pmc_run_issue "${WT_B##*issue-}" 2>&1)"
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

WT_C="$(make_worktree 500103)"   # only the untracked .loom-managed marker

set +e
out_c="$(pmc_run_issue "${WT_C##*issue-}" 2>&1)"
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

WT_D="$(make_worktree 500104)"
# Drop the full spread of Loom runtime markers — none represent user work.
touch "$WT_D/.loom-in-use" "$WT_D/.loom-checkpoint" "$WT_D/.no-changes-needed"
mkdir -p "$WT_D/.snapshots"
echo "patch" > "$WT_D/.snapshots/issue-500104-123.patch"

set +e
out_d="$(pmc_run_issue "${WT_D##*issue-}" 2>&1)"
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

# --- Test 6 (e): lockfile-only dirt -> still refused, message names the file,
#     but the "cross-host duplicate dispatch" hypothesis is NOT asserted (#5658)
echo ""
echo "Test 6: dirty worktree whose ONLY dirt is a lockfile-style artifact is refused, but without the cross-host-dispatch hypothesis"

WT_E="$(make_worktree 565801)"
echo "regenerated-by-routine-install" >"$WT_E/some-lib-lock.json"
git -C "$WT_E" add some-lib-lock.json >/dev/null 2>&1 || true

set +e
out_e="$(pmc_run_issue "${WT_E##*issue-}" 2>&1)"
rc_e=$?
set -e

if [[ $rc_e -eq 0 ]] \
    && [[ "$out_e" == *"Refusing to remove worktree"* ]] \
    && [[ "$out_e" == *"some-lib-lock.json"* ]] \
    && [[ "$out_e" != *"cross-host duplicate dispatch"* ]] \
    && [[ -d "$WT_E" ]] \
    && [[ -f "$WT_E/some-lib-lock.json" ]]; then
    pass "(e) lockfile-only dirt still refused, names the file, suppresses the cross-host-dispatch hypothesis"
else
    fail "(e) expected refuse+name-file+no-hypothesis for lockfile-only dirt; rc=$rc_e, out: $out_e"
fi

# --- Test 7 (f): tracked source file dirty -> refused, message names the file,
#     hypothesis IS still offered (existing behavior preserved)
echo ""
echo "Test 7: dirty worktree with a tracked source-file change is refused, names the file, and still offers the cross-host-dispatch hypothesis"

WT_F="$(make_worktree 565802)"
echo "real in-flight edit" >>"$WT_F/README.md"

set +e
out_f="$(pmc_run_issue "${WT_F##*issue-}" 2>&1)"
rc_f=$?
set -e

if [[ $rc_f -eq 0 ]] \
    && [[ "$out_f" == *"Refusing to remove worktree"* ]] \
    && [[ "$out_f" == *"README.md"* ]] \
    && [[ "$out_f" == *"cross-host duplicate dispatch"* ]] \
    && [[ -d "$WT_F" ]]; then
    pass "(f) tracked source-file dirt still refused, names the file, still offers the cross-host-dispatch hypothesis"
else
    fail "(f) expected refuse+name-file+hypothesis for tracked source dirt; rc=$rc_f, out: $out_f"
fi

# --- Test 8 (g): untracked non-artifact file dirty -> refused, names the file,
#     hypothesis IS still offered
echo ""
echo "Test 8: dirty worktree with an untracked non-artifact file is refused, names the file, and still offers the cross-host-dispatch hypothesis"

WT_G="$(make_worktree 565803)"
echo "brand new uncommitted module" >"$WT_G/new_module.py"

set +e
out_g="$(pmc_run_issue "${WT_G##*issue-}" 2>&1)"
rc_g=$?
set -e

if [[ $rc_g -eq 0 ]] \
    && [[ "$out_g" == *"Refusing to remove worktree"* ]] \
    && [[ "$out_g" == *"new_module.py"* ]] \
    && [[ "$out_g" == *"cross-host duplicate dispatch"* ]] \
    && [[ -d "$WT_G" ]]; then
    pass "(g) untracked non-artifact dirt still refused, names the file, still offers the cross-host-dispatch hypothesis"
else
    fail "(g) expected refuse+name-file+hypothesis for untracked non-artifact dirt; rc=$rc_g, out: $out_g"
fi

# --- Test 9 (h): edge case — BOTH a trivial artifact change AND a real source
#     change dirty at once -> hypothesis is still offered (real work wins)
echo ""
echo "Test 9: mixed dirt (lockfile artifact + real source edit) still offers the cross-host-dispatch hypothesis"

WT_H="$(make_worktree 565804)"
echo "regenerated-by-routine-install" >"$WT_H/package-lock.json"
echo "real in-flight edit" >>"$WT_H/README.md"

set +e
out_h="$(pmc_run_issue "${WT_H##*issue-}" 2>&1)"
rc_h=$?
set -e

if [[ $rc_h -eq 0 ]] \
    && [[ "$out_h" == *"Refusing to remove worktree"* ]] \
    && [[ "$out_h" == *"package-lock.json"* ]] \
    && [[ "$out_h" == *"README.md"* ]] \
    && [[ "$out_h" == *"cross-host duplicate dispatch"* ]] \
    && [[ -d "$WT_H" ]]; then
    pass "(h) mixed trivial+real dirt still refused, names both files, still offers the cross-host-dispatch hypothesis"
else
    fail "(h) expected refuse+name-files+hypothesis for mixed dirt; rc=$rc_h, out: $out_h"
fi

# --- Test 10 (i/j): cleanup FAILS CLOSED when the verb cannot run at all (#8191) ---
#
# The three decisions above are now `loom-daemon merge-pr dirty-guard`, so this
# path has a new failure mode the shell implementation could not have: the verb
# does not answer. Every other step of post-merge cleanup is best-effort (the
# merge already happened), but this one gates `git worktree remove --force`, and
# a caller cannot tell "looked, nothing to save" from "never looked". A CLEAN
# worktree is used deliberately: under the guard's own logic there is nothing to
# protect here, so only the fail-closed rule can produce a refusal — which is
# exactly what makes it evidence rather than a restatement of Test 4.
echo ""
echo "Test 10: a dirty-guard that cannot answer refuses the force-remove (fail-closed), even on a CLEAN worktree"

# (i) the binary does not exist at all — an un-rolled host, the #8285 case.
WT_I="$(make_worktree 819101)"   # clean: only the untracked .loom-managed marker

set +e
out_i="$(LOOM_DAEMON_SELF_BIN="$TMP_ROOT/no-such-loom-daemon" pmc_run_issue "${WT_I##*issue-}" 2>&1)"
rc_i=$?
set -e

if [[ $rc_i -eq 0 ]] \
    && [[ "$out_i" == *"did not run"* ]] \
    && [[ "$out_i" != *"Removing worktree"* ]] \
    && [[ -d "$WT_I" ]]; then
    pass "(i) an unresolvable loom-daemon refuses the removal instead of force-removing on no evidence"
else
    fail "(i) expected fail-closed refusal for an unresolvable daemon; rc=$rc_i, dir=$([[ -d "$WT_I" ]] && echo yes || echo no), out: $out_i"
fi

# (j) a binary that EXITS 0 but never prints the clean sentinel. This is the
# reason the sentinel is positive rather than "silence means clean": a daemon
# that predates the verb, or one whose stdout was swallowed, would otherwise
# hand the caller an exit 0 it would read as consent.
cat >"$TMP_ROOT/mute-daemon" <<'MUTE'
#!/usr/bin/env bash
exit 0
MUTE
chmod +x "$TMP_ROOT/mute-daemon"

WT_J="$(make_worktree 819102)"   # clean, same as (i)

set +e
out_j="$(LOOM_DAEMON_SELF_BIN="$TMP_ROOT/mute-daemon" pmc_run_issue "${WT_J##*issue-}" 2>&1)"
rc_j=$?
set -e

if [[ $rc_j -eq 0 ]] \
    && [[ "$out_j" == *"did not run"* ]] \
    && [[ "$out_j" != *"Removing worktree"* ]] \
    && [[ -d "$WT_J" ]]; then
    pass "(j) exit 0 without the clean sentinel is not consent — the removal is still refused"
else
    fail "(j) expected fail-closed refusal for a silent exit-0 daemon; rc=$rc_j, dir=$([[ -d "$WT_J" ]] && echo yes || echo no), out: $out_j"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
