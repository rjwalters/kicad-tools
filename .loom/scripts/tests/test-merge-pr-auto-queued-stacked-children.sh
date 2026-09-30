#!/usr/bin/env bash
# test-merge-pr-auto-queued-stacked-children.sh — unit tests for #8048 (a
# merely-QUEUED server-side merge skipped the post-merge stacked-children
# reconcile pass) as it stands after #8410 removed the queued path entirely.
#
# ## The gap this pins
#
# `merge-pr.sh <PR> --auto` (the primary headless invocation — champion-pr-merge
# and sweep-wave-lifecycle both use it) used to return as soon as GitHub's
# server-side auto-merge was *enabled*:
#
#     if [[ "$POST_AUTO_MERGED" != "true" ]]; then
#       info "Auto-merge queued (server-side merge pending checks); ..."
#       exit 0
#     fi
#
# `_auto_reconcile_stacked_children` lives AFTER that exit, so on the queued
# path it never ran. GitHub completed the merge minutes later,
# delete_branch_on_merge dropped the parent branch, and nothing ever reconciled
# the children — the `refs/loom/parent/<branch>` pin (#7982/#7998) sat unread
# until an operator remembered `reconcile-stack.sh` by hand.
#
# ## How it is closed now
#
# #8048 closed it conditionally: a parent with open stacked children degraded
# `--auto` to the bounded wait-then-synchronous-merge path (#3820's), so the
# post-merge block — and with it the reconcile pass — was actually reached, and
# a backstop turned the queued exit into a loud non-zero failure if it was ever
# reached with children pinned.
#
# #8410 then removed the server-side arm for EVERY `--auto` run: an armed merge
# re-reads neither the `loom:pr` label nor the non-required test suites, so it
# merged PR #8220 over a `loom:verdict-stale` revocation with five suites still
# running. With no arm there is no queued exit, so #8048's gap is closed
# structurally rather than conditionally — there is no longer a code path on
# which the reconcile pass can be skipped, for stacked parents or anything else.
#
# ## Strategy
#
# The decision site is inline in merge-pr.sh (the file is over the file-size
# ratchet ceiling, and `contract` is baseline-only in
# scripts/shell-allowlist.txt, so it cannot be extracted to a sibling lib — see
# merge-pr.sh's own comment above `_check_no_open_stacked_children`). So:
# EXTRACT the `--auto` block from the script source by its own anchor lines and
# `eval` it in a subshell with stubbed `info` / wait / re-validate, asserting
# the resulting state. Extracting (rather than replicating) keeps the tests in
# lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-auto-queued-stacked-children.sh

# SC2034: the globals the extracted block reads (STACKED_CHILDREN_PIN_WRITTEN,
# PR_NUMBER, DRY_RUN) are consumed only from inside an `eval`d block that the
# linter cannot see into — every such assignment looks "unused" to it.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}
fail() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
    [[ $# -lt 2 ]] || echo "    $2"
}

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg" "Expected: '$expected' / Actual: '$actual'"
    fi
}

# Here-string, never a pipe: `grep -q` exits on first match and would SIGPIPE
# the producer under `set -o pipefail` (the #7771 class, ledgered in
# scripts/pipefail-early-exit-baseline.txt).
assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if grep -qF -- "$needle" <<<"$haystack"; then
        pass "$msg"
    else
        fail "$msg" "Expected substring: '$needle'"
    fi
}

assert_src_absent() {
    local pattern="$1" msg="$2"
    if grep -q -- "$pattern" "$MERGE_PR_SRC"; then
        fail "$msg" "found: $pattern"
    else
        pass "$msg"
    fi
}

# --- Extract the --auto block from merge-pr.sh -------------------------------
# Anchored on its opening `if` at column 1 and closed by the first `fi` at the
# same indentation, so the extraction cannot swallow a neighbouring block.
AUTO_ANCHOR='if [[ "$AUTO_MERGE" == "true" ]]; then'
AUTO_BLOCK="$(awk -v anchor="$AUTO_ANCHOR" '
    index($0, anchor) == 1 { f = 1 }
    f { print }
    f && $0 == "fi" { exit }
' "$MERGE_PR_SRC")"

echo "Extracting the --auto block from merge-pr.sh..."
if [[ -n "$AUTO_BLOCK" ]]; then
    pass "found the --auto wait-then-merge block"
else
    fail "could not extract the --auto block from $MERGE_PR_SRC"
fi

# --- Behavioral: the --auto block always waits, then merges in-process -------
# Runs the extracted block with stubs, printing a single state line:
#   waited=<yes|no> revalidated=<yes|no> auto_merge=<...>
run_auto() {
    local pinned="$1"
    (
        set -euo pipefail
        PR_NUMBER=8048
        DRY_RUN=false
        AUTO_MERGE=true
        WAITED=no
        REVALIDATED=no
        if [[ "$pinned" == "<unset>" ]]; then
            unset STACKED_CHILDREN_PIN_WRITTEN
        else
            STACKED_CHILDREN_PIN_WRITTEN="$pinned"
        fi
        info() { echo "INFO: $*" >&2; }
        _wait_for_checks_then_sync_merge() { WAITED=yes; }
        _revalidate_merge_guards() { REVALIDATED=yes; }
        eval "$AUTO_BLOCK"
        echo "waited=$WAITED revalidated=$REVALIDATED auto_merge=$AUTO_MERGE"
    ) 2>/dev/null
}

echo ""
echo "Testing the --auto decision (#8410: one path, no conditions)..."

# The #8048 trigger (open stacked children pinned by the merge-ordering guard)
# and its absence must now behave IDENTICALLY — the wait is unconditional.
assert_eq "waited=yes revalidated=yes auto_merge=false" \
    "$(run_auto true)" \
    "#8048: a pinned stacked parent waits, re-validates, then merges in-process"
assert_eq "waited=yes revalidated=yes auto_merge=false" \
    "$(run_auto '<unset>')" \
    "#8410: a parent with NO stacked children takes the same path — no fast queued path exists"
assert_eq "waited=yes revalidated=yes auto_merge=false" \
    "$(run_auto '')" \
    "--allow-stacked-children bypass (pin never written) also waits and merges in-process"

# Ordering inside the block: the wait must precede the re-validation, which must
# precede handing off to the synchronous merge. A re-validation that ran BEFORE
# the wait would re-open exactly the window #8410 closed (labels re-read, then a
# multi-minute wait, then a merge on unre-read state).
_order="$(printf '%s\n' "$AUTO_BLOCK" | grep -E '^  (_wait_for_checks_then_sync_merge|_revalidate_merge_guards|AUTO_MERGE=false)$' | tr -d ' ' | tr '\n' ' ')"
assert_eq "_wait_for_checks_then_sync_merge _revalidate_merge_guards AUTO_MERGE=false " \
    "$_order" \
    "#8410: wait -> re-validate -> synchronous merge, in that order"

# --dry-run must still be a preview with no wait and no merge.
_dry_out="$(
    (
        set -euo pipefail
        PR_NUMBER=8048
        DRY_RUN=true
        AUTO_MERGE=true
        info() { echo "INFO: $*"; }
        _wait_for_checks_then_sync_merge() { echo "WAITED"; }
        _revalidate_merge_guards() { echo "REVALIDATED"; }
        eval "$AUTO_BLOCK"
        echo "FELL-THROUGH"
    ) 2>&1 || true
)"
assert_contains "$_dry_out" "[dry-run]" "--dry-run reports what it would do"
if grep -qF "WAITED" <<<"$_dry_out"; then
    fail "--dry-run must not run the bounded wait"
else
    pass "--dry-run does not run the bounded wait"
fi

# --- Source wiring: the queued exit is gone, ordering still holds ------------
echo ""
echo "Testing merge-pr.sh source wiring (#8048 closed structurally by #8410)..."

assert_src_absent 'Auto-merge queued' \
    "#8410: the queued early-exit that skipped the reconcile pass is gone"
assert_src_absent 'POST_AUTO_MERGED' \
    "#8410: the post-arm 'did the server merge it already?' poll is gone with it"
assert_src_absent 'forge_auto_merge "$REPO_NWO"' \
    "#8410: nothing arms a server-side merge, so no queued state can exist"

# The merge-ordering guard must still run before the --auto block (it is what
# pins refs/loom/parent/<branch> for reconcile-stack.sh's fallback).
_guard_line="$(awk '$0 == "_check_no_open_stacked_children" { print NR; exit }' "$MERGE_PR_SRC")"
_auto_line="$(awk -v a="$AUTO_ANCHOR" 'index($0, a) == 1 { print NR; exit }' "$MERGE_PR_SRC")"
if [[ -n "$_guard_line" && -n "$_auto_line" && "$_guard_line" -lt "$_auto_line" ]]; then
    pass "the merge-ordering guard runs before the --auto block (guard=$_guard_line auto=$_auto_line)"
else
    fail "the merge-ordering guard must precede the --auto block" \
        "guard=$_guard_line auto=$_auto_line"
fi

# The pin flag is only ever set on the GitHub path (the guard's own gate).
_guard_body="$(awk '
    /^_check_no_open_stacked_children\(\) \{/ { f = 1 }
    f { print }
    f && $0 == "}" { exit }
' "$MERGE_PR_SRC")"
assert_contains "$_guard_body" 'FORGE_TYPE" == "github"' \
    "STACKED_CHILDREN_PIN_WRITTEN is GitHub-gated"
assert_contains "$_guard_body" "STACKED_CHILDREN_PIN_WRITTEN=true" \
    "the guard is the only writer of STACKED_CHILDREN_PIN_WRITTEN"

# The reconcile pass must sit after the synchronous-merge block — which every
# merge now reaches, including --auto's.
_reconcile_line="$(awk '$0 == "_auto_reconcile_stacked_children || true" { print NR; exit }' "$MERGE_PR_SRC")"
_sync_end_line="$(awk '/^fi  # end synchronous-merge path/ { print NR; exit }' "$MERGE_PR_SRC")"
if [[ -n "$_reconcile_line" && -n "$_sync_end_line" && "$_sync_end_line" -lt "$_reconcile_line" ]]; then
    pass "_auto_reconcile_stacked_children runs after the synchronous merge every --auto run now performs (sync_end=$_sync_end_line reconcile=$_reconcile_line)"
else
    fail "expected _auto_reconcile_stacked_children to sit after the synchronous-merge block" \
        "sync_end=$_sync_end_line reconcile=$_reconcile_line"
fi

# The bounded wait the block delegates to must still be terminal on a failed
# required check / timeout — that is #8048's "loud, non-zero-exit" half, and it
# fires BEFORE the merge, so a parent whose checks never go green can no longer
# strand children behind an already-merged parent.
_wait_body="$(awk '
    /^_wait_for_checks_then_sync_merge\(\) \{/ { f = 1 }
    f { print }
    f && $0 == "}" { exit }
' "$MERGE_PR_SRC")"
assert_contains "$_wait_body" "Timed out after" \
    "the delegated wait still stops short of the merge on the LOOM_AUTO_MERGE_TIMEOUT ceiling"
# #8896: that stop is exit 5 (re-queue), not error()'s exit 1 — still terminal
# for this run, so #8048's "no child stranded behind a merged parent" half holds.
assert_contains "$_wait_body" "exit 5" \
    "the timeout ceiling exits 5 (distinguished re-queue), not the generic failure exit 1 (#8896)"
assert_contains "$_wait_body" "LOOM_AUTO_MERGE_POLL_INTERVAL" \
    "the delegated wait is a bounded poll, not an unbounded block"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
