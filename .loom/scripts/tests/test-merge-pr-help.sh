#!/usr/bin/env bash
# test-merge-pr-help.sh - Unit tests for the --help / -h flag on merge-pr.sh
#
# Verifies that merge-pr.sh prints usage and exits 0 when --help or -h is
# passed, without any network or git side effects (no forge_detect, no
# find_main_repo_root). Runs from /tmp to confirm early-exit before git
# initialization.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-help.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGE_PR="$(cd "$SCRIPT_DIR/.." && pwd)/merge-pr.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Looking for: '$needle'"
        echo "    In output:   '$haystack'"
    fi
}

if [[ ! -x "$MERGE_PR" ]]; then
    echo "ERROR: $MERGE_PR is not executable" >&2
    exit 1
fi

# --- Test: --help exits 0 with usage ---
echo "Testing merge-pr.sh --help..."

set +e
HELP_OUTPUT=$("$MERGE_PR" --help 2>&1)
HELP_EXIT=$?
set -e

assert_eq "0" "$HELP_EXIT" "--help exits 0"
assert_contains "$HELP_OUTPUT" "Usage:" "--help output contains 'Usage:'"
assert_contains "$HELP_OUTPUT" "--auto" "--help output mentions --auto"
assert_contains "$HELP_OUTPUT" "--dry-run" "--help output mentions --dry-run"
assert_contains "$HELP_OUTPUT" "--no-cleanup-worktree" "--help output mentions --no-cleanup-worktree"
assert_contains "$HELP_OUTPUT" "-h, --help" "--help output documents -h/--help"
# Issue #3364: --worktree-path override flag for non-Loom worktrees
assert_contains "$HELP_OUTPUT" "--worktree-path" "--help output mentions --worktree-path (#3364)"
assert_contains "$HELP_OUTPUT" "sentinel guard" "--help explains the sentinel-guard bypass semantics"
assert_contains "$HELP_OUTPUT" "--worktree-path ../adhoc-wt" "--help shows a --worktree-path example"

# --- Test: -h exits 0 with same usage ---
echo ""
echo "Testing merge-pr.sh -h..."

set +e
SHORT_OUTPUT=$("$MERGE_PR" -h 2>&1)
SHORT_EXIT=$?
set -e

assert_eq "0" "$SHORT_EXIT" "-h exits 0"
assert_eq "$HELP_OUTPUT" "$SHORT_OUTPUT" "-h produces identical output to --help"

# --- Test: --help works outside a git repo (early exit before find_main_repo_root) ---
echo ""
echo "Testing merge-pr.sh --help from outside a git repo..."

TMP_DIR=$(mktemp -d /tmp/loom-merge-pr-help-test.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT

set +e
NONGIT_OUTPUT=$(cd "$TMP_DIR" && "$MERGE_PR" --help 2>&1)
NONGIT_EXIT=$?
set -e

assert_eq "0" "$NONGIT_EXIT" "--help exits 0 outside a git repo"
assert_contains "$NONGIT_OUTPUT" "Usage:" "--help works without git context"

# --- Test: --help with extra args still exits 0 ---
echo ""
echo "Testing merge-pr.sh --help with extra args..."

set +e
EXTRA_OUTPUT=$("$MERGE_PR" --help 999 2>&1)
EXTRA_EXIT=$?
set -e

assert_eq "0" "$EXTRA_EXIT" "--help with extra args exits 0"
assert_contains "$EXTRA_OUTPUT" "Usage:" "--help with extra args prints usage"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
