#!/usr/bin/env bash
# test-cleanup-branches-pr-review-branch.sh - Tests for the pr-* review-branch
# cleanup pass added to cleanup-branches.sh (#4405)
#
# Judge/Doctor review-time branches (e.g. a bare `gh pr checkout` outside a
# managed worktree, or a scratch `-v2` iteration) never match
# `feature/issue-*`, so cleanup-branches.sh's original loop never saw them —
# they accumulated forever because a squash-merge repo can never classify
# them via `git branch --merged`. cleanup-branches.sh now additionally
# discovers branches shaped like `pr-<N>` / `pr<N>-*` / `pr-<N>-*`, resolves
# each to its originating PR, and deletes it only when that PR's state is
# MERGED or CLOSED — through `loom-daemon merge-pr delete-branch`, the shared
# tip-SHA-verified rule merge-pr.sh and `worktree.sh remove` also use, rather
# than a raw `git branch -D` (#8968; before that, by awk-extracting and
# `eval`ing merge-pr.sh's own wrapper around the same subcommand).
#
# Verifies:
#   1. Source contains the pr-* discovery regex, the daemon delegation and the
#      PR-state gate.
#   2. Behavioral, end-to-end run of the REAL cleanup-branches.sh against a
#      throwaway git repo with a stubbed `gh`/forge on PATH:
#      (a) a pr-<N>-style branch whose PR is MERGED gets deleted.
#      (b) a pr-<N>-style branch whose PR is still OPEN is left alone.
#      (c) a feature/issue-<N> branch is processed by the pre-existing path
#          only — unaffected by the new pr-* pass (no double-processing).
#      (d) --dry-run reports the merged-PR branch as "would delete" without
#          actually deleting it.
#      (e) a merged-PR branch that is still checked out in a LINKED worktree
#          exercises the delete-refusal path: the run must not abort, the
#          branch must be kept, and the remaining branches must still be
#          processed.
#      (f) a merged-PR branch that is the PRIMARY checkout's own HEAD takes the
#          specialized primary-checkout remediation path (which also needs
#          $DEFAULT_BRANCH_NAME to reach the daemon) without aborting.
#      (g) a CLOSED-issue feature/issue-* branch checked out in a LINKED
#          worktree: the pre-existing feature loop's raw `git branch -D`
#          refuses, and under `set -e` a bare delete would abort the whole
#          script before the new pr-* pass runs. The run must exit 0, still
#          print the pr-* pass header, and still clean a later merged-PR
#          pr-* branch (#4405 third-pass regression).
#
# Companion to test-merge-pr-local-branch-cleanup.sh, which covers merge-pr.sh's
# own `_maybe_delete_local_branch` wrapper over the same subcommand.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLEANUP_SCRIPT="$SCRIPTS_DIR/cleanup-branches.sh"
DEFAULT_BRANCH_LIB="$SCRIPTS_DIR/lib/default-branch.sh"

# #8191/#8968: cleanup-branches.sh's pr-* pass IS a call to `loom-daemon
# merge-pr delete-branch`. Pin the binary built from this tree so a stale
# installed daemon cannot answer instead — it would warn-and-keep every pr-*
# branch and fail these cases for the wrong reason.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "merge-pr"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'  # retired() below
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# An assertion that CANNOT survive #8968's removal of the extract-and-eval
# wiring, retired under the three-part test in
# defaults/docs/verification-recipes.md §6. Printed, not deleted: a reader must
# be able to see what was removed, why, and what proves the property now.
# Counted as run so the totals stay honest.
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

refute_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then fail "$msg (unexpectedly matched: $pattern)"; else pass "$msg"; fi
}

[[ -x "$CLEANUP_SCRIPT" ]] || { echo "ERROR: $CLEANUP_SCRIPT not executable" >&2; exit 1; }

# --- Test 1: source contains the pr-* discovery + reuse wiring ---
echo "Test 1: cleanup-branches.sh source contains the #4405 pr-* review-branch wiring"

assert_grep "grep -E '\\^pr-\\?\\[0-9\\]\\+\\(-\\.\\*\\)\\?\\\$'" "$CLEANUP_SCRIPT" \
    "discovers pr-<N> / pr<N>-* / pr-<N>-* branches via the shared regex"
assert_grep 'merge-pr delete-branch --repo-root "\$REPO_ROOT" --branch "\$branch" --expected-head-sha' "$CLEANUP_SCRIPT" \
    "delegates the delete to 'loom-daemon merge-pr delete-branch' with the PR's head SHA (#8968)"
assert_grep '_delete_landed_branch "\$branch" "\$pr_head_sha"' "$CLEANUP_SCRIPT" \
    "the pr-* loop routes every candidate through that one delegating helper"
assert_grep '^[[:space:]]*# requires-daemon: merge-pr >= [0-9]' "$CLEANUP_SCRIPT" \
    "declares the daemon version floor the delegation needs (#8285)"
refute_grep '_extract_shell_fn|_MAYBE_DELETE' "$CLEANUP_SCRIPT" \
    "no longer awk-extracts any function body out of merge-pr.sh (#8968)"
refute_grep '(^|[^[:alnum:]_])eval ' "$CLEANUP_SCRIPT" \
    "no longer evals shell text extracted from another script (#8968)"

# --- Retired source-text assertions (#8968) ---
#
# Nine assertions below pinned the extract-and-`eval` contraption this pass used
# before #8968: the `awk` extraction of merge-pr.sh's `_maybe_delete_local_branch`
# body, its three transitive helpers (and the matching "still a top-level
# function in merge-pr.sh" extraction-target checks), the `lib/branch-landed.sh`
# source and the fail-closed `branch_landed()` shim. #8191 turned the extraction
# target into a thin wrapper around `loom-daemon merge-pr delete-branch`, which
# left every one of those helpers unreachable from the extracted body; #8968
# deleted the whole contraption in favour of calling the subcommand directly.
# Each is retired with its behavioral successor named — and the successors are
# stronger than a grep: the scratch repo the behavioral cases run in no longer
# gets a copy of merge-pr.sh at all (see "no merge-pr.sh" below), so an
# extraction reintroduced here would fail case (a) outright.
retired \
    "extracts the real _maybe_delete_local_branch() function body from merge-pr.sh (no duplication)" \
    "the cleanup-branches.sh source literally contains '_MAYBE_DELETE_FN=\"\$(_extract_shell_fn _maybe_delete_local_branch'" \
    "#8968: there is no extraction left to assert on — the pass calls 'loom-daemon merge-pr delete-branch' itself, which is the same worktree_cli::branch_delete rule the extracted wrapper called, so 'no duplication' is now structural rather than textual" \
    "the 'merge-pr delete-branch --repo-root' assertion above (the call replacing it) plus behavioral case (a) below, which still proves a merged-PR branch is actually deleted end-to-end — now with no merge-pr.sh in the scratch tree to extract from"
# The refusal path inside the rule distinguishes "checked out in the primary
# checkout" from "checked out in some other worktree" (#4171). Before #8968
# those three helpers had to be extracted from merge-pr.sh alongside the
# wrapper or the script died with "command not found" under `set -e` (#4405);
# they now live in Rust, reached over the subcommand boundary.
for dep_fn in _primary_worktree_path _is_primary_worktree_path _find_worktree_by_branch; do
    retired \
        "extracts _maybe_delete_local_branch's transitive helper $dep_fn" \
        "the cleanup-branches.sh source literally mentions $dep_fn" \
        "#8968: the refusal path is inside loom-daemon (worktree_cli::branch_delete), not in shell text this script evals, so there is no helper for it to carry — and a missing one can no longer produce 'command not found' here at all" \
        "behavioral cases (e) and (f) below, which drive the linked-worktree refusal and the primary-checkout remediation end-to-end and still assert the exact operator-visible text of each"
    retired \
        "$dep_fn is still a top-level function in merge-pr.sh (extraction target intact)" \
        "merge-pr.sh defines ^$dep_fn() { at column 0, where the awk extractor could find it" \
        "#8968: nothing here extracts from merge-pr.sh, so merge-pr.sh's internal formatting is no longer this suite's concern" \
        "test-merge-pr-local-branch-cleanup.sh, whose own eval harness still extracts all three and fails if their shape changes, plus behavioral cases (e)/(f) below"
done
retired \
    "invokes the extracted helper with the PR's head SHA for the tip-match safety check" \
    "the cleanup-branches.sh source literally contains '_maybe_delete_local_branch \"\$branch\" \"\$pr_head_sha\"'" \
    "#8968: merge-pr.sh's private helper name is no longer in this script; the head SHA now reaches the same rule as the subcommand's --expected-head-sha argument" \
    "the two assertions above ('--expected-head-sha' on the delegating call, and the loop's _delete_landed_branch invocation), plus behavioral cases (a)/(e), which prove a tip match deletes and a refusal keeps"
retired \
    "sources the shared branch-landed primitive the delete safety check needs (#7812)" \
    "the cleanup-branches.sh source literally contains 'source \"\$SCRIPT_DIR/lib/branch-landed.sh\"'" \
    "#8968: the extracted body stopped calling branch_landed at #8191 — the landed verdict is computed inside loom-daemon now, so sourcing the shell library here loaded a function nothing called" \
    "loom-daemon's branch_delete::only_a_landed_verdict_may_escalate_to_force_delete and branch_landed::tokens_match_the_shell_twin unit tests, plus behavioral case (a), which proves the landed escalation still reaches the operator's branch"
retired \
    "carries a fail-closed 'unknown' shim for a partially-resynced .loom/ (#7812)" \
    "the cleanup-branches.sh source literally contains 'branch_landed()'" \
    "#8968: the shim existed only so the evaled body could call branch_landed when lib/branch-landed.sh was missing; with no eval and no call there is nothing to shim, and a partially-resynced .loom/ now degrades through the single 'could not resolve loom-daemon' skip instead" \
    "the daemon-resolution skip warning in cleanup-branches.sh (one message, branch kept) and behavioral case (e)'s no-'command not found' assertion, which is what the shim ultimately protected"

assert_grep 'pr view "\$pr_num" --json state,headRefOid' "$CLEANUP_SCRIPT" \
    "resolves PR state + head SHA via \$FORGE/gh pr view"
assert_grep 'pr_state" == "OPEN"' "$CLEANUP_SCRIPT" \
    "leaves the branch alone when its PR is still OPEN"
assert_grep 'pr_state" != "MERGED" && "\$pr_state" != "CLOSED"' "$CLEANUP_SCRIPT" \
    "only proceeds to delete when the PR is MERGED or CLOSED"
assert_grep '"\$1" == "--dry-run"' "$CLEANUP_SCRIPT" \
    "cleanup-branches.sh still supports --dry-run"
assert_grep 'would delete \$branch \(dry-run\)' "$CLEANUP_SCRIPT" \
    "--dry-run previews the pr-* branch it would delete without deleting it"

# --- Behavioral end-to-end tests ---
echo ""
echo "Test 2: end-to-end run of the real cleanup-branches.sh"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-cleanup-branches-pr.XXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf "$TMP_ROOT" 2>/dev/null || true; }
trap cleanup EXIT

REPO="$TMP_ROOT/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q
git -C "$REPO" config user.email "test@example.com"
git -C "$REPO" config user.name "Test"
echo "hello" > "$REPO/README.md"
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "initial"
git -C "$REPO" branch -M main
HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"

# Scratch scripts/ tree the fake `gh` and cleanup-branches.sh both need
# (cleanup-branches.sh resolves lib/default-branch.sh relative to its own
# SCRIPT_DIR).
#
# NO merge-pr.sh is copied here, deliberately (#8968): after dropping the
# extract-and-eval wiring, cleanup-branches.sh must not need merge-pr.sh at
# all. That absence is what makes the behavioral cases below successors to the
# retired "extracts the real function body" source assertion rather than a
# weaker restatement of it — reintroduce any extraction and the pass would fail
# to load its helper, so case (a) would fail instead of quietly passing.
mkdir -p "$REPO/scripts/lib"
cp "$CLEANUP_SCRIPT" "$REPO/scripts/cleanup-branches.sh"
[[ -f "$DEFAULT_BRANCH_LIB" ]] && cp "$DEFAULT_BRANCH_LIB" "$REPO/scripts/lib/default-branch.sh"
chmod +x "$REPO/scripts/cleanup-branches.sh"

# Branches under test:
#   pr-100      -> PR #100, MERGED, tip == HEAD_SHA  -> should be deleted
#   pr-200-old  -> PR #200, OPEN                      -> must be kept
#   feature/issue-300 -> issue #300, OPEN              -> must be kept
#     (also proves the pre-existing feature/issue-* path is unaffected by
#      the new pr-* pass — it is never matched by the pr-* regex)
git -C "$REPO" branch pr-100 main
git -C "$REPO" branch pr-200-old main
git -C "$REPO" branch feature/issue-300 main

FAKE_BIN="$TMP_ROOT/fakebin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/gh" <<EOF
#!/bin/bash
if [[ "\$1" == "issue" && "\$2" == "view" ]]; then
  case "\$3" in
    300) echo "OPEN" ;;
    999) echo "CLOSED" ;;
    *) echo "NOT_FOUND" ;;
  esac
  exit 0
fi
if [[ "\$1" == "pr" && "\$2" == "view" ]]; then
  case "\$3" in
    100) echo '{"state":"MERGED","headRefOid":"$HEAD_SHA"}' ;;
    101) echo '{"state":"MERGED","headRefOid":"$HEAD_SHA"}' ;;
    102) echo '{"state":"MERGED","headRefOid":"$HEAD_SHA"}' ;;
    103) echo '{"state":"CLOSED","headRefOid":"$HEAD_SHA"}' ;;
    105) echo '{"state":"MERGED","headRefOid":"$HEAD_SHA"}' ;;
    200) echo '{"state":"OPEN","headRefOid":"$HEAD_SHA"}' ;;
    *) exit 1 ;;
  esac
  exit 0
fi
exit 1
EOF
chmod +x "$FAKE_BIN/gh"

# --- (a)+(b)+(c): real run deletes the merged pr-* branch, keeps the open
#     pr-* branch and the open feature/issue-* branch untouched ---
run_output="$(cd "$REPO" && PATH="$FAKE_BIN:$PATH" ./scripts/cleanup-branches.sh 2>&1)"

if git -C "$REPO" show-ref --verify --quiet refs/heads/pr-100; then
    fail "(a) pr-100 (PR #100 MERGED) should have been deleted; still present"
else
    pass "(a) pr-<N> branch whose PR is MERGED gets deleted"
fi

if git -C "$REPO" show-ref --verify --quiet refs/heads/pr-200-old; then
    pass "(b) pr-<N>-style branch whose PR is still OPEN is left alone"
else
    fail "(b) pr-200-old (PR #200 OPEN) was unexpectedly deleted"
fi

if git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-300; then
    pass "(c) feature/issue-<N> branch is unaffected by the new pr-* pass (issue still OPEN, kept)"
else
    fail "(c) feature/issue-300 was unexpectedly deleted"
fi

if [[ "$run_output" == *"PR #100 is MERGED"* ]] && [[ "$run_output" == *"deleted"* ]]; then
    pass "output reports PR #100 as MERGED and the branch as deleted"
else
    fail "expected MERGED+deleted reporting for PR #100; got: $run_output"
fi

if [[ "$run_output" == *"PR #200 is OPEN - keeping pr-200-old"* ]]; then
    pass "output reports PR #200 as OPEN and keeps pr-200-old"
else
    fail "expected OPEN+keep reporting for PR #200; got: $run_output"
fi

# --- (d): --dry-run never deletes, only previews ---
git -C "$REPO" branch pr-101 main
dry_output="$(cd "$REPO" && PATH="$FAKE_BIN:$PATH" ./scripts/cleanup-branches.sh --dry-run 2>&1)"
if [[ "$dry_output" == *"would delete pr-101 (dry-run)"* ]] || [[ "$dry_output" == *"pr-101"* && "$dry_output" == *"dry-run"* ]]; then
    if git -C "$REPO" show-ref --verify --quiet refs/heads/pr-101; then
        pass "(d) --dry-run previews the merged-PR branch without deleting it"
    else
        fail "(d) --dry-run must not actually delete the branch; pr-101 is gone"
    fi
else
    fail "(d) expected a dry-run preview mentioning pr-101; got: $dry_output"
fi
git -C "$REPO" branch -D pr-101 >/dev/null 2>&1 || true

# --- (e): merged-PR branch checked out in a LINKED worktree exercises the
#     delete-refusal path, which classifies "checked out in the primary
#     checkout" vs "checked out in some other worktree" (#4171). Pre-#8968 that
#     classifier was shell text extracted from merge-pr.sh, and extracting the
#     wrapper without its helpers killed the run with
#     "_find_worktree_by_branch: command not found" (exit 127) under `set -e`;
#     it now lives in loom-daemon, reached over the subcommand boundary. Either
#     way the operator-visible contract asserted here is the same.
git -C "$REPO" branch pr-102 main
git -C "$REPO" worktree add -q "$TMP_ROOT/wt-102" pr-102 >/dev/null 2>&1
# A second, deletable branch AFTER pr-102 alphabetically proves the run did
# not abort partway through the pr-* loop.
git -C "$REPO" branch pr-103 main

set +e
refusal_output="$(cd "$REPO" && PATH="$FAKE_BIN:$PATH" ./scripts/cleanup-branches.sh 2>&1)"
refusal_rc=$?
set -e

if [[ $refusal_rc -eq 0 ]]; then
    pass "(e) run exits 0 when a merged-PR branch is checked out in a linked worktree"
else
    fail "(e) run exited $refusal_rc (expected 0); got: $refusal_output"
fi

if [[ "$refusal_output" != *"command not found"* ]]; then
    pass "(e) no missing-helper 'command not found' in the delete-refusal path"
else
    fail "(e) delete-refusal path hit a missing helper; got: $refusal_output"
fi

if git -C "$REPO" show-ref --verify --quiet refs/heads/pr-102; then
    pass "(e) branch checked out in a linked worktree is kept, not force-deleted"
else
    fail "(e) pr-102 was deleted despite being checked out in a linked worktree"
fi

if [[ "$refusal_output" == *"Could not delete local branch 'pr-102'"* ]]; then
    pass "(e) reports the refusal with the checked-out explanation"
else
    fail "(e) expected a 'Could not delete local branch' warning for pr-102; got: $refusal_output"
fi

if ! git -C "$REPO" show-ref --verify --quiet refs/heads/pr-103; then
    pass "(e) the pr-* loop continues past the refusal (pr-103 still processed)"
else
    fail "(e) pr-103 was not processed — the loop aborted at the refusal"
fi

if [[ "$refusal_output" == *"Kept (unsafe to force-delete)"* ]]; then
    pass "(e) summary counts the refused branch under 'Kept (unsafe to force-delete)'"
else
    fail "(e) expected an unsafe-kept summary line; got: $refusal_output"
fi

git -C "$REPO" worktree remove --force "$TMP_ROOT/wt-102" >/dev/null 2>&1 || true
git -C "$REPO" branch -D pr-102 >/dev/null 2>&1 || true

# --- (f): merged-PR branch that is the PRIMARY checkout's own HEAD takes the
#     specialized primary-checkout remediation path, which also needs the
#     resolved default branch. Same failure class as (e), plus it proves
#     $DEFAULT_BRANCH_NAME still reaches the rule — now as the subcommand's
#     --default-branch argument, which cleanup-branches.sh omits when the
#     resolution came back empty.
git -C "$REPO" checkout -q -b pr-104 main
sed -i.bak 's/^    102)/    104) echo '"'"'{"state":"MERGED","headRefOid":"'"$HEAD_SHA"'"}'"'"' ;;\n    102)/' "$FAKE_BIN/gh"
rm -f "$FAKE_BIN/gh.bak"

set +e
primary_output="$(cd "$REPO" && PATH="$FAKE_BIN:$PATH" ./scripts/cleanup-branches.sh 2>&1)"
primary_rc=$?
set -e

if [[ $primary_rc -eq 0 ]]; then
    pass "(f) run exits 0 when the merged-PR branch is the primary checkout's HEAD"
else
    fail "(f) run exited $primary_rc (expected 0); got: $primary_output"
fi

if git -C "$REPO" show-ref --verify --quiet refs/heads/pr-104; then
    pass "(f) the primary checkout's own HEAD branch is kept, not force-deleted"
else
    fail "(f) pr-104 was deleted while checked out in the primary checkout"
fi

if [[ "$primary_output" == *"primary repository checkout"* ]] && [[ "$primary_output" == *"branch -D pr-104"* ]]; then
    pass "(f) emits the primary-checkout two-step remediation (uses \$DEFAULT_BRANCH_NAME)"
else
    fail "(f) expected the primary-checkout remediation for pr-104; got: $primary_output"
fi

git -C "$REPO" checkout -q main
git -C "$REPO" branch -D pr-104 >/dev/null 2>&1 || true

# --- (g): a CLOSED-issue feature/issue-* branch checked out in a LINKED
#     worktree must not abort the run. The pre-existing feature loop deletes
#     CLOSED-issue branches with a raw `git branch -D`; when the branch is
#     checked out elsewhere that delete exits 1, and under `set -e` a bare,
#     error-swallowed delete aborts the whole script — BEFORE the new pr-*
#     review pass ever runs, silently leaking every orphaned pr-* branch
#     (#4405, third-pass regression). Assert: (a) exit 0, (b) the pr-* pass
#     header still prints, and (c) a later merged-PR pr-* branch is still
#     cleaned (i.e. the run reached the second pass at all).
git -C "$REPO" branch feature/issue-999 main
git -C "$REPO" worktree add -q "$TMP_ROOT/wt-999" feature/issue-999 >/dev/null 2>&1
git -C "$REPO" branch pr-105 main

set +e
feat_output="$(cd "$REPO" && PATH="$FAKE_BIN:$PATH" ./scripts/cleanup-branches.sh 2>&1)"
feat_rc=$?
set -e

if [[ $feat_rc -eq 0 ]]; then
    pass "(g) run exits 0 when a CLOSED-issue feature branch is checked out in a linked worktree"
else
    fail "(g) run exited $feat_rc (expected 0); got: $feat_output"
fi

if [[ "$feat_output" != *"command not found"* ]]; then
    pass "(g) no crash before the pr-* pass when the feature delete refuses"
else
    fail "(g) hit a crash in the feature loop; got: $feat_output"
fi

if git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-999; then
    pass "(g) the checked-out CLOSED-issue feature branch is kept, not force-deleted"
else
    fail "(g) feature/issue-999 was deleted despite being checked out in a linked worktree"
fi

if [[ "$feat_output" == *"Could not delete feature/issue-999"* ]]; then
    pass "(g) reports the feature-branch refusal instead of silently mis-counting it"
else
    fail "(g) expected a 'Could not delete feature/issue-999' warning; got: $feat_output"
fi

if [[ "$feat_output" == *"Checking PR review-branch status"* ]]; then
    pass "(g) the pr-* review pass still runs after the feature-loop refusal"
else
    fail "(g) the pr-* pass never ran (feature loop aborted the script); got: $feat_output"
fi

if ! git -C "$REPO" show-ref --verify --quiet refs/heads/pr-105; then
    pass "(g) a later merged-PR pr-* branch is still cleaned (run reached the second pass)"
else
    fail "(g) pr-105 was not cleaned — the run aborted before the pr-* pass; got: $feat_output"
fi

git -C "$REPO" worktree remove --force "$TMP_ROOT/wt-999" >/dev/null 2>&1 || true
git -C "$REPO" branch -D feature/issue-999 >/dev/null 2>&1 || true

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
