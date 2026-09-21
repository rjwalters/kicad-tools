#!/usr/bin/env bash
# test-merge-pr-auto-queued-stacked-children.sh — unit tests for #8048: the
# `--auto` path must not exit 0 on a merely-QUEUED server-side merge when the
# parent branch still has open stacked child PRs.
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
# ## The fix under test
#
# #8048 adds a SECOND trigger to the pre-existing, already-bounded #3820
# degradation ("repo has Allow auto-merge disabled → wait for checks, then
# merge synchronously"): the merge-ordering guard having pinned this parent's
# tip, i.e. `STACKED_CHILDREN_PIN_WRITTEN == true`. On that trigger `--auto`
# becomes a synchronous merge in THIS process, so the post-merge cleanup block
# — and with it `_auto_reconcile_stacked_children` — is actually reached, with
# the parent branch still present. A failing required check or the
# LOOM_AUTO_MERGE_TIMEOUT ceiling makes `_wait_for_checks_then_sync_merge`
# error() out non-zero instead.
#
# A backstop at the queued-exit site turns that branch into a loud, non-zero
# exit if it is ever reached with children pinned (it should now be
# unreachable — the backstop exists so a future refactor cannot silently
# reopen the gap).
#
# ## Strategy
#
# Both decision sites are inline in merge-pr.sh (the file is over the
# file-size ratchet ceiling, and `contract` is baseline-only in
# scripts/shell-allowlist.txt, so neither can be extracted to a sibling lib —
# see merge-pr.sh's own comment above `_check_no_open_stacked_children`).
# So: EXTRACT each block from the script source by its own anchor lines and
# `eval` it in a subshell with stubbed `info` / `error` /
# `_wait_for_checks_then_sync_merge`, asserting the resulting state and exit
# code. Extracting (rather than replicating) keeps the tests in lockstep with
# the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-auto-queued-stacked-children.sh

# SC2034: the globals the extracted blocks read (REPO_AUTO_MERGE_ALLOWED,
# STACKED_CHILDREN_PIN_WRITTEN, POST_AUTO_MERGED, PR_NUMBER, PR_BRANCH) are
# consumed only from inside an `eval`d block, which shellcheck cannot see —
# every such assignment looks "unused" to the linter.
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

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if grep -qF -- "$needle" <<<"$haystack"; then
        fail "$msg" "Unexpected substring: '$needle'"
    else
        pass "$msg"
    fi
}

# --- Extract the two decision blocks from merge-pr.sh ------------------------
# Each is anchored on its own opening `if` line — matched at column 1 including
# its leading indentation, so the `--auto` degrade anchor cannot accidentally
# select the more-deeply-indented `[dry-run]` branch that tests the same
# variable a few lines above — and closed by the first `fi` at that same
# indentation, so the extraction cannot swallow a neighbouring block.
INDENT_2='  '
INDENT_4='    '
extract_block() {
    local anchor="$1" indent="$2"
    awk -v anchor="${indent}${anchor}" -v closer="${indent}fi" '
        index($0, anchor) == 1 { f = 1 }
        f { print }
        f && $0 == closer { exit }
    ' "$MERGE_PR_SRC"
}

# Anchors are PREFIXES of the opening line (everything up to, but not
# including, the trailing `; then`), so adding a second `||` clause to the
# condition does not break the extraction.
DEGRADE_ANCHOR='if [[ "$REPO_AUTO_MERGE_ALLOWED" == "false" ]]'
QUEUED_ANCHOR='if [[ "$POST_AUTO_MERGED" != "true" ]]'
DEGRADE_BLOCK="$(extract_block "$DEGRADE_ANCHOR" "$INDENT_2")"
QUEUED_BLOCK="$(extract_block "$QUEUED_ANCHOR" "$INDENT_4")"

echo "Extracting the --auto decision blocks from merge-pr.sh..."
if [[ -n "$DEGRADE_BLOCK" ]]; then
    pass "found the #3820 wait-then-sync-merge degrade block"
else
    fail "could not extract the #3820 degrade block from $MERGE_PR_SRC"
fi
if [[ -n "$QUEUED_BLOCK" ]]; then
    pass "found the auto-merge-queued early-exit block"
else
    fail "could not extract the queued early-exit block from $MERGE_PR_SRC"
fi

# --- Behavioral: the degrade decision ---------------------------------------
# Runs the extracted block with stubs, printing a single state line:
#   waited=<yes|no> auto_merge=<...> auto_merge_ok=<...>
run_degrade() {
    local allowed="$1" pinned="$2"
    (
        set -euo pipefail
        # shellcheck disable=SC2034  # read only by the evaluated block
        REPO_AUTO_MERGE_ALLOWED="$allowed"
        # shellcheck disable=SC2034
        PR_NUMBER=8048
        AUTO_MERGE=true
        AUTO_MERGE_OK=false
        WAITED=no
        if [[ "$pinned" == "<unset>" ]]; then
            unset STACKED_CHILDREN_PIN_WRITTEN
        else
            STACKED_CHILDREN_PIN_WRITTEN="$pinned"
        fi
        info() { echo "INFO: $*" >&2; }
        _wait_for_checks_then_sync_merge() { WAITED=yes; }
        eval "$DEGRADE_BLOCK"
        echo "waited=$WAITED auto_merge=$AUTO_MERGE auto_merge_ok=$AUTO_MERGE_OK"
    ) 2>/dev/null
}

echo ""
echo "Testing the --auto degrade decision (#3820 trigger + #8048 trigger)..."

# The pre-#8048 trigger is untouched.
assert_eq "waited=yes auto_merge=false auto_merge_ok=true" \
    "$(run_degrade false '<unset>')" \
    "#3820 preserved: allow_auto_merge=false still degrades to wait-then-sync-merge"

# The new #8048 trigger: open stacked children pinned by the merge-ordering
# guard, on a repo where auto-merge IS allowed.
assert_eq "waited=yes auto_merge=false auto_merge_ok=true" \
    "$(run_degrade true true)" \
    "#8048: a pinned stacked parent degrades to wait-then-sync-merge even with auto-merge allowed"

# Both triggers at once is still just one degrade.
assert_eq "waited=yes auto_merge=false auto_merge_ok=true" \
    "$(run_degrade false true)" \
    "#8048: both triggers together degrade exactly once"

# THE FAST PATH — the common case must stay a byte-for-byte no-op with no
# added polling. This is the regression the whole design hinges on.
assert_eq "waited=no auto_merge=true auto_merge_ok=false" \
    "$(run_degrade true '<unset>')" \
    "fast path: no stacked children + auto-merge allowed -> NO wait, --auto unchanged"

assert_eq "waited=no auto_merge=true auto_merge_ok=false" \
    "$(run_degrade unknown '<unset>')" \
    "fast path: an 'unknown' auto-merge probe with no children -> NO wait"

# --allow-stacked-children bypass: the guard returns BEFORE pinning, so the
# variable is never set and the operator keeps the fast queued path.
assert_eq "waited=no auto_merge=true auto_merge_ok=false" \
    "$(run_degrade true '')" \
    "--allow-stacked-children bypass (pin never written) keeps the fast queued path"

# A non-"true" value must never trip the new trigger.
assert_eq "waited=no auto_merge=true auto_merge_ok=false" \
    "$(run_degrade true false)" \
    "pin-written=false does not trigger the degrade"

# The degrade's info line must name BOTH facts so an operator can tell which
# trigger fired.
_degrade_msg="$(
    (
        set -euo pipefail
        REPO_AUTO_MERGE_ALLOWED=true
        PR_NUMBER=8048
        AUTO_MERGE=true
        AUTO_MERGE_OK=false
        STACKED_CHILDREN_PIN_WRITTEN=true
        info() { echo "INFO: $*"; }
        _wait_for_checks_then_sync_merge() { :; }
        eval "$DEGRADE_BLOCK"
    ) 2>&1
)"
assert_contains "$_degrade_msg" "8048" \
    "degrade message names the PR number"
assert_contains "$_degrade_msg" "allowed=true" \
    "degrade message reports the repo auto-merge probe result"
assert_contains "$_degrade_msg" "pinned=true" \
    "degrade message reports the stacked-children pin state"

# --- Behavioral: the queued early-exit backstop ------------------------------
# Runs the extracted block with stubs; prints its output and exit code.
run_queued() {
    local pinned="$1"
    local out rc=0
    out="$(
        (
            set -euo pipefail
            # shellcheck disable=SC2034  # read only by the evaluated block
            POST_AUTO_MERGED=false
            # shellcheck disable=SC2034
            PR_NUMBER=8048
            # shellcheck disable=SC2034
            PR_BRANCH="feature/issue-8048"
            if [[ "$pinned" == "<unset>" ]]; then
                unset STACKED_CHILDREN_PIN_WRITTEN
            else
                STACKED_CHILDREN_PIN_WRITTEN="$pinned"
            fi
            info()  { echo "INFO: $*"; }
            error() { echo "ERROR: $*" >&2; exit 1; }
            eval "$QUEUED_BLOCK"
            echo "FELL-THROUGH"
        ) 2>&1
    )" || rc=$?
    echo "rc=$rc"
    echo "$out"
}

echo ""
echo "Testing the queued-exit backstop (#8048)..."

_q_clean="$(run_queued '<unset>')"
assert_contains "$_q_clean" "rc=0" \
    "no stacked children: the queued path still exits 0 (unchanged behavior)"
assert_not_contains "$_q_clean" "ERROR:" \
    "no stacked children: the queued path emits no error"
assert_contains "$_q_clean" "Auto-merge queued" \
    "no stacked children: the queued path still reports the queued merge"
assert_contains "$_q_clean" "loom-clean" \
    "no stacked children: the queued path still points at loom-clean"

# queued-then-cancelled / never-completed with children pinned: LOUD, non-zero.
_q_pinned="$(run_queued true)"
assert_contains "$_q_pinned" "rc=1" \
    "#8048 AC: a queued-but-unmerged parent with stacked children exits NON-ZERO"
assert_contains "$_q_pinned" "ERROR:" \
    "#8048 AC: the failure is loud (error, not a bare info)"
assert_contains "$_q_pinned" "reconcile-stack.sh" \
    "#8048 AC: the error names the manual remedy"
assert_contains "$_q_pinned" "feature/issue-8048" \
    "#8048 AC: the error names the parent branch"

# --- Source wiring: ordering and gating -------------------------------------
echo ""
echo "Testing merge-pr.sh source wiring (#8048)..."

# The merge-ordering guard must be INVOKED before the degrade decision reads
# STACKED_CHILDREN_PIN_WRITTEN, or the new trigger is always false.
_guard_line="$(awk '$0 == "_check_no_open_stacked_children" { print NR; exit }' "$MERGE_PR_SRC")"
_degrade_line="$(awk -v a="${INDENT_2}${DEGRADE_ANCHOR}" 'index($0, a) == 1 { print NR; exit }' "$MERGE_PR_SRC")"
_queued_line="$(awk -v a="${INDENT_4}${QUEUED_ANCHOR}" 'index($0, a) == 1 { print NR; exit }' "$MERGE_PR_SRC")"

if [[ -n "$_guard_line" && -n "$_degrade_line" && "$_guard_line" -lt "$_degrade_line" ]]; then
    pass "the merge-ordering guard runs before the --auto degrade decision (guard=$_guard_line degrade=$_degrade_line)"
else
    fail "the merge-ordering guard must precede the --auto degrade decision" \
        "guard=$_guard_line degrade=$_degrade_line"
fi

if [[ -n "$_degrade_line" && -n "$_queued_line" && "$_degrade_line" -lt "$_queued_line" ]]; then
    pass "the degrade decision precedes the queued early-exit it makes unreachable (degrade=$_degrade_line queued=$_queued_line)"
else
    fail "the degrade decision must precede the queued early-exit" \
        "degrade=$_degrade_line queued=$_queued_line"
fi

# The pin flag is only ever set on the GitHub path (the guard's own gate), so
# the new trigger cannot fire on Gitea.
_guard_body="$(awk '
    /^_check_no_open_stacked_children\(\) \{/ { f = 1 }
    f { print }
    f && $0 == "}" { exit }
' "$MERGE_PR_SRC")"
assert_contains "$_guard_body" 'FORGE_TYPE" == "github"' \
    "STACKED_CHILDREN_PIN_WRITTEN is GitHub-gated (Gitea keeps the fast path)"
assert_contains "$_guard_body" "STACKED_CHILDREN_PIN_WRITTEN=true" \
    "the guard is the only writer of STACKED_CHILDREN_PIN_WRITTEN"

# The degraded path must reach the post-merge reconcile pass: the reconcile
# call has to live after the synchronous-merge block, not inside the auto
# branch.
_reconcile_line="$(awk '$0 == "_auto_reconcile_stacked_children || true" { print NR; exit }' "$MERGE_PR_SRC")"
if [[ -n "$_reconcile_line" && -n "$_queued_line" && "$_queued_line" -lt "$_reconcile_line" ]]; then
    pass "_auto_reconcile_stacked_children is reached only past the queued exit (queued=$_queued_line reconcile=$_reconcile_line) — which is why the degrade is the fix"
else
    fail "expected _auto_reconcile_stacked_children to sit after the queued early-exit" \
        "queued=$_queued_line reconcile=$_reconcile_line"
fi

# The bounded wait the degrade delegates to must still be terminal on a failed
# required check / timeout — that is #8048's "loud, non-zero-exit" half.
_wait_body="$(awk '
    /^_wait_for_checks_then_sync_merge\(\) \{/ { f = 1 }
    f { print }
    f && $0 == "}" { exit }
' "$MERGE_PR_SRC")"
assert_contains "$_wait_body" "Timed out after" \
    "the delegated wait still error()s out on the LOOM_AUTO_MERGE_TIMEOUT ceiling"
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
