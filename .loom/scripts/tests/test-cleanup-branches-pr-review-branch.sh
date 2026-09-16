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
# MERGED or CLOSED — reusing merge-pr.sh's tip-SHA-verified
# `_maybe_delete_local_branch` helper rather than a raw `git branch -D`.
#
# Verifies:
#   1. Source contains the pr-* discovery regex and the PR-state gate.
#   2. Behavioral, end-to-end run of the REAL cleanup-branches.sh against a
#      throwaway git repo with a stubbed `gh`/forge on PATH:
#      (a) a pr-<N>-style branch whose PR is MERGED gets deleted.
#      (b) a pr-<N>-style branch whose PR is still OPEN is left alone.
#      (c) a feature/issue-<N> branch is processed by the pre-existing path
#          only — unaffected by the new pr-* pass (no double-processing).
#      (d) --dry-run reports the merged-PR branch as "would delete" without
#          actually deleting it.
#      (e) a merged-PR branch that is still checked out in a LINKED worktree
#          exercises _maybe_delete_local_branch's delete-refusal path (which
#          calls _find_worktree_by_branch / _is_primary_worktree_path): the
#          run must not abort, the branch must be kept, and the remaining
#          branches must still be processed.
#      (f) a merged-PR branch that is the PRIMARY checkout's own HEAD takes
#          _is_primary_worktree_path's specialized remediation path (which
#          also reads $DEFAULT_BRANCH_NAME) without aborting.
#      (g) a CLOSED-issue feature/issue-* branch checked out in a LINKED
#          worktree: the pre-existing feature loop's raw `git branch -D`
#          refuses, and under `set -e` a bare delete would abort the whole
#          script before the new pr-* pass runs. The run must exit 0, still
#          print the pr-* pass header, and still clean a later merged-PR
#          pr-* branch (#4405 third-pass regression).
#
# Companion to test-merge-pr-local-branch-cleanup.sh, which tests
# `_maybe_delete_local_branch` itself (the safety check this script reuses).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLEANUP_SCRIPT="$SCRIPTS_DIR/cleanup-branches.sh"
MERGE_PR_SCRIPT="$SCRIPTS_DIR/merge-pr.sh"
DEFAULT_BRANCH_LIB="$SCRIPTS_DIR/lib/default-branch.sh"

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

[[ -x "$CLEANUP_SCRIPT" ]] || { echo "ERROR: $CLEANUP_SCRIPT not executable" >&2; exit 1; }
[[ -x "$MERGE_PR_SCRIPT" ]] || { echo "ERROR: $MERGE_PR_SCRIPT not executable" >&2; exit 1; }

# --- Test 1: source contains the pr-* discovery + reuse wiring ---
echo "Test 1: cleanup-branches.sh source contains the #4405 pr-* review-branch wiring"

assert_grep "grep -E '\\^pr-\\?\\[0-9\\]\\+\\(-\\.\\*\\)\\?\\\$'" "$CLEANUP_SCRIPT" \
    "discovers pr-<N> / pr<N>-* / pr-<N>-* branches via the shared regex"
if grep -qF '_MAYBE_DELETE_FN="$(_extract_shell_fn _maybe_delete_local_branch' "$CLEANUP_SCRIPT"; then
    pass "extracts the real _maybe_delete_local_branch() function body from merge-pr.sh (no duplication)"
else
    fail "extracts the real _maybe_delete_local_branch() function body from merge-pr.sh (no duplication)"
fi
# The refusal path inside _maybe_delete_local_branch calls these (#4171); if
# they are not extracted too the script dies with "command not found" (#4405).
# The `-d` → `-D` upgrade's safety predicate is NOT on that list since #7812:
# it is `branch_landed` from lib/branch-landed.sh, a real shared library the
# script sources (asserted separately below) rather than a function body to
# extract.
for dep_fn in _primary_worktree_path _is_primary_worktree_path _find_worktree_by_branch; do
    if grep -qF "$dep_fn" "$CLEANUP_SCRIPT"; then
        pass "extracts _maybe_delete_local_branch's transitive helper $dep_fn"
    else
        fail "extracts _maybe_delete_local_branch's transitive helper $dep_fn"
    fi
    if grep -qE "^${dep_fn}\(\) \{" "$MERGE_PR_SCRIPT"; then
        pass "$dep_fn is still a top-level function in merge-pr.sh (extraction target intact)"
    else
        fail "$dep_fn is no longer a top-level function in merge-pr.sh — update the extraction list"
    fi
done
assert_grep '_maybe_delete_local_branch "\$branch" "\$pr_head_sha"' "$CLEANUP_SCRIPT" \
    "invokes the extracted helper with the PR's head SHA for the tip-match safety check"
assert_grep 'source "\$SCRIPT_DIR/lib/branch-landed.sh"' "$CLEANUP_SCRIPT" \
    "sources the shared branch-landed primitive the delete safety check needs (#7812)"
if grep -qF 'branch_landed()' "$CLEANUP_SCRIPT"; then
    pass "carries a fail-closed 'unknown' shim for a partially-resynced .loom/ (#7812)"
else
    fail "carries a fail-closed 'unknown' shim for a partially-resynced .loom/ (#7812)"
fi
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
# (cleanup-branches.sh resolves merge-pr.sh and lib/default-branch.sh
# relative to its own SCRIPT_DIR).
mkdir -p "$REPO/scripts/lib"
cp "$CLEANUP_SCRIPT" "$REPO/scripts/cleanup-branches.sh"
cp "$MERGE_PR_SCRIPT" "$REPO/scripts/merge-pr.sh"
[[ -f "$DEFAULT_BRANCH_LIB" ]] && cp "$DEFAULT_BRANCH_LIB" "$REPO/scripts/lib/default-branch.sh"
chmod +x "$REPO/scripts/cleanup-branches.sh" "$REPO/scripts/merge-pr.sh"

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

# --- (e): merged-PR branch checked out in a LINKED worktree exercises
#     _maybe_delete_local_branch's delete-refusal path. That path calls
#     _find_worktree_by_branch / _is_primary_worktree_path (merge-pr.sh,
#     #4171); if cleanup-branches.sh only extracts _maybe_delete_local_branch
#     itself, the run dies with "_find_worktree_by_branch: command not found"
#     (exit 127) under `set -e`, aborting the whole cleanup mid-pass.
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

# --- (f): merged-PR branch that is the PRIMARY checkout's own HEAD takes
#     _is_primary_worktree_path's specialized remediation path, which also
#     reads $DEFAULT_BRANCH_NAME. Same crash class as (e) if the helpers
#     aren't extracted, plus it proves DEFAULT_BRANCH_NAME reaches the
#     evaluated body (it is only referenced there, hence the SC2034 waiver).
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
