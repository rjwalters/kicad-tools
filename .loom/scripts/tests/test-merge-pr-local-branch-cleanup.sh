#!/usr/bin/env bash
# test-merge-pr-local-branch-cleanup.sh - Tests for local-branch deletion (#4100)
#
# merge-pr.sh already cleaned up the remote branch and the worktree on a
# successful merge but left the LOCAL branch behind on every path except the
# --worktree-path explicit override. Every merged issue therefore leaked one
# local `feature/issue-N` branch, all classifying as `[gone]` once
# `git fetch --prune` runs.
#
# Root cause: `_maybe_delete_local_branch` existed but had exactly one call
# site (:1455-era, the --worktree-path override), gated behind
# allow_unmanaged=="true". The default `.loom/worktrees/issue-N` path, the
# discovered-Loom-managed-non-standard-path, and the no-worktree-at-all case
# never called it.
#
# The safety criterion is NOT `git branch --merged` (always false for a
# squash merge) — it's whether the local branch tip equals the merged PR's
# `head.sha` (already parsed by merge-pr.sh into $PR_HEAD_SHA). A matching
# tip is safe to force-delete (`-D`); a non-matching tip (unpushed local
# work) falls back to the original `git branch -d` behaviour, which keeps the
# branch and reports it.
#
# Verifies:
#   1. Source contains the extended helper signature, the wiring outside
#      allow_unmanaged, the disambiguated remote-branch log line, the
#      dry-run preview line, and the --no-cleanup-worktree / preserve-env
#      skip messages.
#   2. Behavioral tests of the REAL `_maybe_delete_local_branch` function
#      body (extracted verbatim from merge-pr.sh, no drift) against real git
#      repos:
#      (a) head-sha tip match -> `-D` (safe force-delete), branch removed.
#      (b) head-sha tip mismatch (unpushed commit) -> kept, warned with the
#          literal `git branch -D <branch>` escape hatch.
#      (c) branch checked out elsewhere (current HEAD or another worktree)
#          -> specific "checked out" message, branch preserved, never
#          force-deleted.
#      (d) branch is the repo's default branch -> refused up front, never
#          even attempts a git branch -d/-D.
#      (e) branch does not exist locally -> quiet no-op info line, not a
#          warning.
#      (f) no expected_head_sha supplied (the pre-#4100 --worktree-path
#          caller shape) -> falls back to plain `-d` behaviour unchanged.
#      (g)-(j) primary-checkout auto-cleanup (#5015): when the branch is
#          checked out in the PRIMARY repo checkout (not a linked worktree)
#          and the tip-match safety check passed, merge-pr.sh no longer just
#          prints manual instructions — it completes the cleanup itself:
#          (g) clean tree -> auto `checkout <default> && branch -D`.
#          (h) dirty tree (control) -> unchanged manual-instructions
#              behavior, HEAD untouched.
#          (i) tree clean but a stash entry exists -> treated as unsafe, not
#              auto-cleaned.
#          (j) CLEANUP_PRIMARY_CHECKOUT=false (--no-cleanup-primary) opts out
#              even when otherwise fully safe.
#   3. The never-closing-programme-issue case (#6694) — an issue that is OPEN,
#      is not a close target of this PR, and never will be (every merge to it
#      is `Part of #N` by design), so the issue-close cleanup gate can never
#      flip true and the pre-#6694 worktree/branch preserve was permanent:
#      (k) the shared `branch_landed` predicate (#7812) the
#          worktree-preserve decision and the `-d` -> `-D` upgrade now BOTH
#          key on, exercised directly: true only for an exact tip match; false
#          for local commits beyond the merged head, a missing branch, and an
#          empty expected SHA.
#      (l) end-to-end ordering: the branch delete is gated on the WORKTREE
#          being gone, never on the issue closing — refused while the
#          fully-captured worktree is present, force-deleted by the identical
#          call once merge-pr.sh removes it, with the issue still open.
#      This is deliberately distinct from the closed-issue reaping path
#      covered by test-merge-pr-closed-issue-cleanup.sh.
#
# This is the companion to test-merge-pr-worktree-path.sh (wiring/discovery)
# and test-merge-pr-primary-worktree-guard.sh (the extract-and-eval pattern
# this test reuses for _maybe_delete_local_branch). See also
# test-merge-pr-primary-checkout-advice.sh (the #4171 companion, also
# extended for #5015 with the tip-match auto-cleanup case).

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

refute_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then fail "$msg (unexpectedly matched: $pattern)"; else pass "$msg"; fi
}

[[ -x "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not executable" >&2; exit 1; }

# --- Test 1: source contains the extended wiring ---
echo "Test 1: merge-pr.sh source contains the #4100 local-branch cleanup wiring"

assert_grep "PR_HEAD_SHA=\\\$\\(echo \"\\\$PR_JSON\" \\| jq -r '\\.head\\.sha" "$MERGE_PR" \
    "merge-pr.sh parses PR_HEAD_SHA from the PR JSON"
assert_grep 'expected_head_sha="\$\{2:-\}"' "$MERGE_PR" \
    "_maybe_delete_local_branch accepts an optional expected_head_sha second arg"
assert_grep '_maybe_delete_local_branch "\$PR_BRANCH" "\$PR_HEAD_SHA"' "$MERGE_PR" \
    "the unified branch-delete call passes PR_HEAD_SHA (reachable outside allow_unmanaged)"
assert_grep 'delete_flag="-D"' "$MERGE_PR" \
    "helper switches to -D (force) when the tip-match safety check passes"
assert_grep "success \"Remote branch '\\\$PR_BRANCH' deleted\"" "$MERGE_PR" \
    "remote-branch success line is disambiguated as 'Remote branch' (was 'Branch')"
refute_grep "success \"Branch '\\\$PR_BRANCH' deleted\"" "$MERGE_PR" \
    "the old ambiguous 'Branch ... deleted' (without Remote/Local) line is gone"
assert_grep "Would delete local branch '\\\$PR_BRANCH'" "$MERGE_PR" \
    "--dry-run previews the local branch it would delete"
assert_grep "no-cleanup-worktree.*local branch left in place" "$MERGE_PR" \
    "--no-cleanup-worktree emits an explicit skip message covering the local branch"
assert_grep "LOOM_PRESERVE_WORKTREE=1.*local branch left in place" "$MERGE_PR" \
    "LOOM_PRESERVE_WORKTREE=1 emits an explicit skip message covering the local branch"
assert_grep "checked out \\(current HEAD or another worktree\\)" "$MERGE_PR" \
    "checked-out refusal gets a specific message, not the generic unmerged-commits warning"
assert_grep "it is the repository's default branch" "$MERGE_PR" \
    "helper refuses to delete the repo's default branch"

# --- Extract the ACTUAL function body from the live source (no drift) ---
extract_fn() {
    local name="$1" file="$2"
    awk -v fn="$name" '
      $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
      grab { print }
      grab && /^}/ { exit }
    ' "$file"
}

# Harness: stub the logging helpers, then eval the real function body.
info()    { echo "INFO: $*"; }
warning() { echo "WARN: $*"; }
success() { echo "OK: $*"; }
error()   { echo "ERROR: $*" >&2; return 1; }

# _maybe_delete_local_branch's primary-checkout auto-cleanup path (#5015)
# calls _find_worktree_by_branch / _is_primary_worktree_path, which in turn
# calls _primary_worktree_path — extract all three alongside it so the real
# auto-cleanup branch actually runs in this harness (an undefined helper
# silently no-ops instead of erroring, which would otherwise mask the new
# behavior rather than exercise it).
eval "$(extract_fn _primary_worktree_path "$MERGE_PR")"
eval "$(extract_fn _is_primary_worktree_path "$MERGE_PR")"
eval "$(extract_fn _find_worktree_by_branch "$MERGE_PR")"
# #7812: the `-d` -> `-D` safety check is the shared `branch_landed`
# primitive (also reused by the worktree-preserve decision) — a real library,
# SOURCED here rather than extracted, so the real code path drives the
# assertions below. Offline: this suite is about merge-pr.sh's local branch
# logic, not the forge rung, and must stay hermetic.
export LOOM_BRANCH_LANDED_OFFLINE=1
# shellcheck source=../lib/branch-landed.sh
source "$(dirname "$MERGE_PR")/lib/branch-landed.sh"
eval "$(extract_fn _maybe_delete_local_branch "$MERGE_PR")"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-merge-local-branch.XXXXXX")"
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
BASE_SHA="$(git -C "$REPO" rev-parse HEAD)"

# shellcheck disable=SC2034
REPO_ROOT="$REPO"
# shellcheck disable=SC2034
DEFAULT_BRANCH_NAME="main"
# shellcheck disable=SC2034
CLEANUP_PRIMARY_CHECKOUT=true

echo ""
echo "Test 2: behavioral tests of the real _maybe_delete_local_branch body"

# --- (a) tip matches expected_head_sha -> safe force-delete (-D) ---
git -C "$REPO" checkout -q -b feature/issue-100
echo "change" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "issue 100 work"
MATCH_SHA="$(git -C "$REPO" rev-parse feature/issue-100)"
git -C "$REPO" checkout -q main

out_a="$(_maybe_delete_local_branch "feature/issue-100" "$MATCH_SHA" 2>&1)"
if [[ "$out_a" == *"OK: Local branch 'feature/issue-100' deleted"* ]] \
   && [[ "$out_a" == *"safe force-delete"* ]] \
   && ! git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-100; then
    pass "(a) tip matching the merged PR head SHA force-deletes the branch"
else
    fail "(a) expected safe force-delete; got: $out_a"
fi

# --- (b) tip does NOT match expected_head_sha (unpushed extra commit) -> kept ---
git -C "$REPO" checkout -q -b feature/issue-200
echo "change" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "merged part"
MERGED_SHA="$(git -C "$REPO" rev-parse feature/issue-200)"
echo "more" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "extra unpushed work"
git -C "$REPO" checkout -q main

out_b="$(_maybe_delete_local_branch "feature/issue-200" "$MERGED_SHA" 2>&1)"
if [[ "$out_b" == *"WARN:"* ]] \
   && [[ "$out_b" == *"git branch -D feature/issue-200"* ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-200; then
    pass "(b) tip mismatch keeps the branch and reports the -D escape hatch"
else
    fail "(b) expected kept-and-warned with -D escape hatch; got: $out_b"
fi
git -C "$REPO" branch -D feature/issue-200 >/dev/null 2>&1 || true

# --- (c) branch checked out in another worktree -> specific message, kept ---
git -C "$REPO" branch feature/issue-300
WT2="$TMP_ROOT/wt2"
git -C "$REPO" worktree add -q "$WT2" feature/issue-300 >/dev/null 2>&1

out_c="$(_maybe_delete_local_branch "feature/issue-300" "" 2>&1)"
if [[ "$out_c" == *"WARN:"* ]] \
   && [[ "$out_c" == *"checked out (current HEAD or another worktree)"* ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-300; then
    pass "(c) branch checked out in another worktree gets the specific checked-out message"
else
    fail "(c) expected checked-out-specific warning; got: $out_c"
fi
git -C "$REPO" worktree remove "$WT2" --force >/dev/null 2>&1 || true
git -C "$REPO" branch -D feature/issue-300 >/dev/null 2>&1 || true

# --- (c2) branch IS the current HEAD (primary checkout) -> also refused.
# REPO here IS the primary checkout, so with _is_primary_worktree_path /
# _find_worktree_by_branch now extracted (needed by the #5015 auto-cleanup
# tests below), this correctly gets the primary-checkout-specific message
# rather than the generic one. The branch carries an unlanded commit (#7812):
# the delete_flag is keyed on whether the work has landed, not on whether a
# head SHA was passed, so a branch parked at main's tip would be landed and
# the #5015 auto-cleanup gate would legitimately fire — the case under test
# here is the one where real work would be lost. ---
git -C "$REPO" checkout -q -b feature/issue-301
echo "unlanded work" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "issue 301 work that never reached main"
out_c2="$(_maybe_delete_local_branch "feature/issue-301" "" 2>&1)"
if [[ "$out_c2" == *"checked out in the primary repository checkout"* ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-301; then
    pass "(c2) branch that is the current HEAD is never deleted"
else
    fail "(c2) expected current-HEAD refusal; got: $out_c2"
fi
git -C "$REPO" checkout -q main
git -C "$REPO" branch -D feature/issue-301 >/dev/null 2>&1 || true

# --- (d) default branch is never a delete target ---
out_d="$(_maybe_delete_local_branch "main" "$BASE_SHA" 2>&1)"
if [[ "$out_d" == *"WARN:"* ]] \
   && [[ "$out_d" == *"it is the repository's default branch"* ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/main; then
    pass "(d) refuses to delete the repo's default branch, even with a matching tip"
else
    fail "(d) expected default-branch refusal; got: $out_d"
fi

# --- (e) branch does not exist locally -> quiet info no-op ---
out_e="$(_maybe_delete_local_branch "feature/issue-999-does-not-exist" "deadbeef" 2>&1)"
if [[ "$out_e" == *"INFO:"* ]] \
   && [[ "$out_e" == *"does not exist"* ]] \
   && [[ "$out_e" != *"WARN:"* ]]; then
    pass "(e) nonexistent local branch is a quiet info no-op, not a warning"
else
    fail "(e) expected quiet info no-op; got: $out_e"
fi

# --- (f) no expected_head_sha (pre-#4100 caller shape). The branch was merged
# with a real merge commit, so since #7812 the shared primitive proves it
# landed by ancestry alone and the delete is attributed accordingly — the
# caller no longer has to hand over a head SHA for a merged branch to be
# cleaned up. The outcome (deleted, exit 0) is what the pre-#7812 `-d` path
# produced here too. ---
git -C "$REPO" checkout -q -b feature/issue-400
echo "clean work" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "clean, mergeable"
git -C "$REPO" checkout -q main
git -C "$REPO" merge -q --no-ff feature/issue-400 -m "merge for -d test"

out_f="$(_maybe_delete_local_branch "feature/issue-400" 2>&1)"
if [[ "$out_f" == *"OK: Local branch 'feature/issue-400' deleted"* ]] \
   && [[ "$out_f" == *"ancestor"* ]] \
   && ! git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-400; then
    pass "(f) omitting expected_head_sha still deletes a genuinely merged branch, by ancestry"
else
    fail "(f) expected an ancestry-attributed delete; got: $out_f"
fi

# --- (f2) the conservative refusal is what an UNLANDED branch still gets,
# with or without a head SHA (#7812: `not-landed` and `unknown` both keep the
# plain `-d`, and git's own safety net then refuses). ---
git -C "$REPO" checkout -q -b feature/issue-401
echo "never merged" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "unlanded work"
git -C "$REPO" checkout -q main

out_f2="$(_maybe_delete_local_branch "feature/issue-401" 2>&1)"
if [[ "$out_f2" == *"WARN:"* ]] \
   && [[ "$out_f2" != *"safe force-delete"* ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-401; then
    pass "(f2) an unlanded branch is still refused, never force-deleted"
else
    fail "(f2) expected an unlanded branch to survive; got: $out_f2"
fi
git -C "$REPO" branch -D feature/issue-401 >/dev/null 2>&1 || true

# --- (g) checked out in the PRIMARY checkout, clean tree, tip matches head SHA
#     -> auto-cleanup: checkout the default branch, then force-delete (#5015) ---
echo ""
echo "Test 3: primary-checkout auto-cleanup (#5015)"

git -C "$REPO" checkout -q -b feature/issue-500
echo "change" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "issue 500 work"
MATCH_SHA_G="$(git -C "$REPO" rev-parse feature/issue-500)"
# feature/issue-500 remains checked out (current HEAD) — REPO IS the primary
# checkout (the sole worktree in this repo), and the tree is clean.

out_g="$(_maybe_delete_local_branch "feature/issue-500" "$MATCH_SHA_G" 2>&1)"
if [[ "$out_g" == *"OK: Local branch 'feature/issue-500' deleted"* ]] \
   && [[ "$out_g" == *"safe force-delete"* ]] \
   && [[ "$out_g" == *"automatically switched to 'main'"* ]] \
   && [[ "$(git -C "$REPO" branch --show-current)" == "main" ]] \
   && ! git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-500; then
    pass "(g) clean tree + tip match in the primary checkout auto-checks-out default and force-deletes"
else
    fail "(g) expected auto-checkout + force-delete; got: $out_g (branch now: $(git -C "$REPO" branch --show-current))"
fi

# --- (h) control: same setup but a DIRTY tree -> unchanged manual-instructions
#     behavior, HEAD untouched, branch preserved ---
git -C "$REPO" checkout -q -b feature/issue-600
echo "change" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "issue 600 work"
MATCH_SHA_H="$(git -C "$REPO" rev-parse feature/issue-600)"
echo "uncommitted work" >> "$REPO/README.md"   # dirty the tree, do NOT commit

out_h="$(_maybe_delete_local_branch "feature/issue-600" "$MATCH_SHA_H" 2>&1)"
if [[ "$out_h" == *"checked out in the primary repository checkout"* ]] \
   && [[ "$out_h" == *"checkout main"* ]] \
   && [[ "$out_h" == *"branch -D feature/issue-600"* ]] \
   && [[ "$(git -C "$REPO" branch --show-current)" == "feature/issue-600" ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-600; then
    pass "(h) dirty tree in the primary checkout keeps current behavior — manual instructions, HEAD untouched"
else
    fail "(h) expected unchanged manual-instructions behavior on a dirty tree; got: $out_h"
fi
git -C "$REPO" checkout -q -- README.md
git -C "$REPO" checkout -q main
git -C "$REPO" branch -D feature/issue-600 >/dev/null 2>&1 || true

# --- (i) edge case: tree is clean but a stash entry exists -> treated as
#     unsafe, not auto-cleaned (AC: "no stash entries created by the script") ---
git -C "$REPO" checkout -q -b feature/issue-700
echo "change" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "issue 700 work"
MATCH_SHA_I="$(git -C "$REPO" rev-parse feature/issue-700)"
echo "stash me" >> "$REPO/README.md"
git -C "$REPO" stash push -q -m "unrelated wip"   # tree is clean again, but a stash entry now exists

out_i="$(_maybe_delete_local_branch "feature/issue-700" "$MATCH_SHA_I" 2>&1)"
if [[ "$out_i" == *"checked out in the primary repository checkout"* ]] \
   && [[ "$(git -C "$REPO" branch --show-current)" == "feature/issue-700" ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-700; then
    pass "(i) a present stash entry blocks auto-cleanup even with an otherwise-clean tree"
else
    fail "(i) expected stash presence to block auto-cleanup; got: $out_i"
fi
git -C "$REPO" stash drop -q >/dev/null 2>&1 || true
git -C "$REPO" checkout -q main
git -C "$REPO" branch -D feature/issue-700 >/dev/null 2>&1 || true

# --- (j) opt-out: CLEANUP_PRIMARY_CHECKOUT=false disables auto-cleanup even
#     when otherwise fully safe (--no-cleanup-primary sets this at the CLI) ---
git -C "$REPO" checkout -q -b feature/issue-800
echo "change" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "issue 800 work"
MATCH_SHA_J="$(git -C "$REPO" rev-parse feature/issue-800)"

CLEANUP_PRIMARY_CHECKOUT=false
out_j="$(_maybe_delete_local_branch "feature/issue-800" "$MATCH_SHA_J" 2>&1)"
# shellcheck disable=SC2034
CLEANUP_PRIMARY_CHECKOUT=true

if [[ "$out_j" == *"checked out in the primary repository checkout"* ]] \
   && [[ "$(git -C "$REPO" branch --show-current)" == "feature/issue-800" ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-800; then
    pass "(j) CLEANUP_PRIMARY_CHECKOUT=false (--no-cleanup-primary) opts out even when otherwise safe"
else
    fail "(j) expected opt-out to fall back to manual instructions; got: $out_j"
fi
git -C "$REPO" checkout -q main
git -C "$REPO" branch -D feature/issue-800 >/dev/null 2>&1 || true

# --- Test 4: never-closing programme issue (#6694) ---
#
# Distinct from the closed-issue reaping path (covered by
# test-merge-pr-closed-issue-cleanup.sh): here the issue is OPEN, is not a
# close target of this PR, and never will close — every merge to it is
# `Part of #N` by design. Pre-#6694 that meant the worktree was preserved
# forever, which in turn made the unconditional _maybe_delete_local_branch
# call a guaranteed no-op ("checked out (current HEAD or another worktree)"),
# so the local branch survived forever too. These cases pin the two halves of
# the fix: the shared predicate itself, and the fact that the branch delete
# succeeds once the fully-captured worktree is removed — with no dependence
# on the issue's state.
echo ""
echo "Test 4: never-closing programme issue — branch/worktree cleanup (#6694)"

# --- (k) the shared landed predicate, exercised directly (#7812).
# The worktree-preserve decision keys on this and NOTHING about the issue, so
# it must be true exactly when the default branch already contains everything
# the branch has — a tip matching the merged PR head SHA being the cheapest
# such proof. ---
git -C "$REPO" checkout -q -b feature/issue-900
echo "captured work" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "issue 900 work (merged as this PR's head)"
CAPTURED_SHA="$(git -C "$REPO" rev-parse feature/issue-900)"
git -C "$REPO" checkout -q main

if branch_has_landed "feature/issue-900" "main" "$CAPTURED_SHA"; then
    pass "(k1) tip == merged PR head SHA is reported landed"
else
    fail "(k1) expected landed for a tip matching the merged PR head SHA"
fi

git -C "$REPO" checkout -q feature/issue-900
echo "unpushed follow-up" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "local work beyond the merged head"
git -C "$REPO" checkout -q main

if branch_has_landed "feature/issue-900" "main" "$CAPTURED_SHA"; then
    fail "(k2) a branch carrying local commits beyond the merged head must NOT be landed"
else
    pass "(k2) local commits beyond the merged head are not landed (preserve stays correct)"
fi

if branch_has_landed "feature/issue-901-nonexistent" "main" "$CAPTURED_SHA"; then
    fail "(k3) a nonexistent local branch must not be reported landed"
else
    pass "(k3) nonexistent local branch is not landed"
fi

# With no head-SHA hint the primitive falls through to its own rungs, which
# (offline, with unlanded content on the branch) still answer "not landed" —
# an absent hint must never read as a blanket yes.
if branch_has_landed "feature/issue-900" "main" ""; then
    fail "(k4) an absent expected head SHA must not be reported landed"
else
    pass "(k4) absent expected head SHA is not landed (no accidental blanket removal)"
fi
git -C "$REPO" branch -D feature/issue-900 >/dev/null 2>&1 || true

# --- (l) end-to-end ordering: the branch delete is gated on the WORKTREE being
# gone, never on the issue closing. While the fully-captured worktree is still
# present the delete is refused (the pre-#6694 permanent state for a
# never-closing issue); once merge-pr.sh removes it — which #6694 now does on
# the fully-captured criterion alone, with the issue still OPEN and still not a
# close target — the very same call force-deletes the branch. ---
git -C "$REPO" branch feature/issue-902
WT_NEVER_CLOSING="$TMP_ROOT/wt-never-closing"
git -C "$REPO" worktree add -q "$WT_NEVER_CLOSING" feature/issue-902 >/dev/null 2>&1
echo "increment" >> "$WT_NEVER_CLOSING/README.md"
git -C "$WT_NEVER_CLOSING" commit -q -am "Part of #902 — one increment of a programme issue"
NEVER_CLOSING_SHA="$(git -C "$REPO" rev-parse feature/issue-902)"

out_l1="$(_maybe_delete_local_branch "feature/issue-902" "$NEVER_CLOSING_SHA" 2>&1)"
if [[ "$out_l1" == *"WARN:"* ]] \
   && [[ "$out_l1" == *"checked out (current HEAD or another worktree)"* ]] \
   && git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-902; then
    pass "(l1) while the worktree is preserved the branch delete is refused (the pre-#6694 permanent state)"
else
    fail "(l1) expected checked-out refusal while the worktree exists; got: $out_l1"
fi

# The branch really has landed — this is exactly the predicate #6694 has
# merge-pr.sh consult to remove the worktree despite the open issue.
if branch_has_landed "feature/issue-902" "main" "$NEVER_CLOSING_SHA"; then
    pass "(l2) the never-closing issue's branch has landed, so the worktree is removable"
else
    fail "(l2) expected the never-closing issue's branch to have landed"
fi

git -C "$REPO" worktree remove "$WT_NEVER_CLOSING" --force >/dev/null 2>&1 || true

out_l3="$(_maybe_delete_local_branch "feature/issue-902" "$NEVER_CLOSING_SHA" 2>&1)"
if [[ "$out_l3" == *"OK: Local branch 'feature/issue-902' deleted"* ]] \
   && [[ "$out_l3" == *"safe force-delete"* ]] \
   && ! git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-902; then
    pass "(l3) once the fully-captured worktree is removed the branch is deleted — no dependence on the issue closing"
else
    fail "(l3) expected force-delete after worktree removal; got: $out_l3"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
