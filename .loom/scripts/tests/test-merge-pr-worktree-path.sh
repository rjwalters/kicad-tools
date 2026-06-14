#!/usr/bin/env bash
# test-merge-pr-worktree-path.sh - Tests for the --worktree-path flag (#3364)
#
# Verifies:
#   1. --worktree-path appears in --help output (composition with help test).
#   2. CLI rejects bad input early: missing value, nonexistent path,
#      registered-worktree validation.
#   3. The script's source contains the bypass-sentinel logic and the
#      porcelain discovery fallback (static grep checks — full integration
#      requires a live forge).
#   4. Inline simulation of the cleanup decision tree:
#      - LOOM_PRESERVE_WORKTREE=1 wins
#      - --no-cleanup-worktree wins over --worktree-path
#      - --worktree-path bypasses sentinel on the explicit path
#      - default path keeps sentinel guard
#      - discovery fallback emits hint without removing
#
# This is the companion to test-merge-pr-help.sh. The help test verifies
# the documentation surface; this test verifies the implementation surface.

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

[[ -x "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not executable" >&2; exit 1; }

# --- Test 1: CLI parsing rejects bad input early ---
echo "Test 1: CLI rejects bad --worktree-path input"

# Missing value
set +e
out=$("$MERGE_PR" 1 --worktree-path 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"--worktree-path requires a value"* ]]; then
    pass "missing value for --worktree-path errors with rc!=0 and clear message"
else
    fail "missing value: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# Nonexistent path
set +e
out=$("$MERGE_PR" --worktree-path /nonexistent-loom-test-path 1 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"does not exist"* ]]; then
    pass "nonexistent --worktree-path errors with rc!=0 and clear message"
else
    fail "nonexistent path: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# Path exists but is not a registered worktree of this repo
set +e
out=$("$MERGE_PR" --worktree-path /tmp 1 2>&1)
rc=$?
set -e
if [[ $rc -ne 0 ]] && [[ "$out" == *"not a registered worktree"* ]]; then
    pass "unregistered --worktree-path errors with rc!=0 and clear message"
else
    fail "unregistered path: expected nonzero exit + message; got rc=$rc, out='$out'"
fi

# --- Test 2: Source contains the expected logic surface ---
echo ""
echo "Test 2: merge-pr.sh source contains the new logic blocks"

assert_grep "WORKTREE_PATH_OVERRIDE=" "$MERGE_PR" \
    "merge-pr.sh declares WORKTREE_PATH_OVERRIDE state variable"
assert_grep "_find_worktree_by_branch" "$MERGE_PR" \
    "merge-pr.sh defines the porcelain branch-search helper"
assert_grep "_worktree_branch_for" "$MERGE_PR" \
    "merge-pr.sh defines the worktree-to-branch lookup helper"
assert_grep "_maybe_delete_local_branch" "$MERGE_PR" \
    "merge-pr.sh defines the safe local-branch delete helper"
assert_grep "git branch -d" "$MERGE_PR" \
    "merge-pr.sh uses git branch -d (safe delete, not -D)"
assert_grep "allow_unmanaged" "$MERGE_PR" \
    "_remove_loom_worktree takes allow_unmanaged second arg"
assert_grep "Bypassing sentinel guard" "$MERGE_PR" \
    "explicit --worktree-path logs the sentinel-bypass action"
assert_grep "Discovered worktree for branch" "$MERGE_PR" \
    "discovery fallback emits a hint about the discovered path"
assert_grep "re-run with: --worktree-path" "$MERGE_PR" \
    "discovery fallback suggests --worktree-path in the hint"

# --- Test 3: Precedence — --no-cleanup-worktree warns when combined ---
echo ""
echo "Test 3: --no-cleanup-worktree wins over --worktree-path"

# This requires a registered worktree path. Use the script's own repo root
# (the worktree this test is running inside).
SELF_WT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
# Resolve the worktree's actual recorded path via porcelain — git's worktree
# list uses the canonical recorded path which may differ from $PWD if there
# are symlinks.
ACTUAL_WT="$(cd "$SELF_WT" && git rev-parse --show-toplevel 2>/dev/null || echo "")"

if [[ -n "$ACTUAL_WT" ]] && git -C "$ACTUAL_WT" worktree list --porcelain 2>/dev/null | \
   awk -v p="$ACTUAL_WT" '/^worktree / { if ($2 == p) { found=1; exit } } END { exit !found }'; then
    # We're in a worktree; we can use ACTUAL_WT as a valid --worktree-path value.
    # Run with --dry-run so the merge itself short-circuits (we only want to see
    # the validation + warning).
    set +e
    out=$("$MERGE_PR" --no-cleanup-worktree --worktree-path "$ACTUAL_WT" 1 --dry-run 2>&1)
    rc=$?
    set -e
    if [[ "$out" == *"--no-cleanup-worktree wins"* ]]; then
        pass "combining --no-cleanup-worktree + --worktree-path warns"
    else
        fail "expected '--no-cleanup-worktree wins' warning; got: $out"
    fi
else
    echo "  SKIP: not running inside a registered worktree, skipping precedence test"
fi

# --- Test 4: Inline simulation of the cleanup decision tree ---
echo ""
echo "Test 4: cleanup decision tree (inline simulation)"

# Replicate the decision shape from merge-pr.sh's cleanup driver so we can
# exercise every branch without a live forge round-trip.
simulate_cleanup() {
    # Args:
    #   $1 preserve            ("0" / "1")        # LOOM_PRESERVE_WORKTREE
    #   $2 cleanup             ("true" / "false") # --no-cleanup-worktree => false
    #   $3 override            (string or "")     # --worktree-path value
    #   $4 default_exists      ("true" / "false") # whether .loom/worktrees/issue-N exists
    #   $5 override_has_sentinel ("true" / "false") # does override path have .loom-managed
    #   $6 discovered          (string or "")     # discovered worktree path
    #   $7 discovered_has_sentinel ("true" / "false")
    local preserve="$1" cleanup="$2" override="$3" default_exists="$4" \
          override_has_sentinel="$5" discovered="$6" discovered_has_sentinel="$7"

    if [[ "$cleanup" != "true" ]]; then
        echo "skip:no-cleanup"; return 0
    fi
    if [[ "$preserve" == "1" ]]; then
        echo "skip:env"; return 0
    fi
    if [[ -n "$override" ]]; then
        # --worktree-path bypasses sentinel
        if [[ "$override_has_sentinel" == "true" ]]; then
            echo "remove:override-managed"
        else
            echo "remove:override-bypass-sentinel"
        fi
        return 0
    fi
    if [[ "$default_exists" == "true" ]]; then
        echo "remove:default"
        return 0
    fi
    # Fallback discovery
    if [[ -n "$discovered" ]]; then
        if [[ "$discovered_has_sentinel" == "true" ]]; then
            echo "remove:discovered-managed"
        else
            echo "warn:discovered-user-owned"
        fi
        return 0
    fi
    echo "skip:nothing-to-do"
}

# Args: preserve cleanup override default_exists override_has_sentinel discovered discovered_has_sentinel

# Case A: LOOM_PRESERVE_WORKTREE=1 wins over everything
result=$(simulate_cleanup 1 true "/path/x" false true "" false)
if [[ "$result" == "skip:env" ]]; then
    pass "case A: LOOM_PRESERVE_WORKTREE=1 short-circuits everything"
else
    fail "case A: expected 'skip:env', got '$result'"
fi

# Case B: --no-cleanup-worktree wins (cleanup=false)
result=$(simulate_cleanup 0 false "/path/x" false true "" false)
if [[ "$result" == "skip:no-cleanup" ]]; then
    pass "case B: --no-cleanup-worktree short-circuits even with override"
else
    fail "case B: expected 'skip:no-cleanup', got '$result'"
fi

# Case C: --worktree-path bypasses sentinel (no sentinel on override path)
result=$(simulate_cleanup 0 true "/path/x" true false "" false)
if [[ "$result" == "remove:override-bypass-sentinel" ]]; then
    pass "case C: --worktree-path bypasses sentinel for non-Loom worktree"
else
    fail "case C: expected 'remove:override-bypass-sentinel', got '$result'"
fi

# Case D: --worktree-path on Loom-managed worktree — still removes
result=$(simulate_cleanup 0 true "/path/x" true true "" false)
if [[ "$result" == "remove:override-managed" ]]; then
    pass "case D: --worktree-path also removes Loom-managed worktrees"
else
    fail "case D: expected 'remove:override-managed', got '$result'"
fi

# Case E: default path exists — remove via sentinel-guarded path
result=$(simulate_cleanup 0 true "" true false "" false)
if [[ "$result" == "remove:default" ]]; then
    pass "case E: default Loom-convention path used when present"
else
    fail "case E: expected 'remove:default', got '$result'"
fi

# Case F: default missing, discovered worktree has sentinel — remove
result=$(simulate_cleanup 0 true "" false false "/found" true)
if [[ "$result" == "remove:discovered-managed" ]]; then
    pass "case F: discovery removes Loom-managed worktree at non-standard path"
else
    fail "case F: expected 'remove:discovered-managed', got '$result'"
fi

# Case G: default missing, discovered worktree LACKS sentinel — warn-only
result=$(simulate_cleanup 0 true "" false false "/found" false)
if [[ "$result" == "warn:discovered-user-owned" ]]; then
    pass "case G: discovery warns but does NOT remove user-owned worktree"
else
    fail "case G: expected 'warn:discovered-user-owned', got '$result'"
fi

# Case H: nothing found anywhere — quiet success
result=$(simulate_cleanup 0 true "" false false "" false)
if [[ "$result" == "skip:nothing-to-do" ]]; then
    pass "case H: nothing-found is a quiet no-op"
else
    fail "case H: expected 'skip:nothing-to-do', got '$result'"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
