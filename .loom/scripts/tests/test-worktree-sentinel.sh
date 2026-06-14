#!/usr/bin/env bash
# test-worktree-sentinel.sh - Tests for .loom-managed sentinel marker
#
# Verifies issue #3334 fix: cleanup tooling honors the worktree ownership
# model (Loom-managed worktrees marked with .loom-managed; user-provisioned
# worktrees are never removed).
#
# Coverage:
#   1. worktree.sh writes .loom-managed when creating a Loom-managed worktree
#   2. merge-pr.sh cleanup block contains the sentinel and LOOM_PRESERVE_WORKTREE guards
#   3. agent-destroy.sh cleanup block contains the same guards
#
# Tests 2 and 3 are static (grep-based) because exercising the cleanup path
# end-to-end requires a full forge round-trip. The static check protects
# against accidental regression of the guard lines.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"
MERGE_PR_SH="$SCRIPTS_DIR/merge-pr.sh"
AGENT_DESTROY_SH="$SCRIPTS_DIR/agent-destroy.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_file_exists() {
    if [[ -f "$1" ]]; then
        pass "$2"
    else
        fail "$2 (expected file: $1)"
    fi
}

assert_grep() {
    local pattern="$1"
    local file="$2"
    local msg="$3"
    if grep -qE "$pattern" "$file"; then
        pass "$msg"
    else
        fail "$msg (pattern not found: $pattern in $file)"
    fi
}

# --- Test 1: worktree.sh writes .loom-managed sentinel ---
echo "Test 1: worktree.sh writes .loom-managed sentinel"

TMP=$(mktemp -d /tmp/loom-sentinel-test.XXXXXX)
trap 'rm -rf "$TMP"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

# Build a tiny throwaway repo and copy the script + the helpers it needs.
# worktree.sh expects an origin/main ref, so we set up a bare remote and push.
git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

# Copy worktree.sh (it uses ../scripts/.loom-managed; mirror enough of the
# tree so the helper runs without the real .loom layout).
mkdir -p .loom/scripts/lib .loom/hooks
cp "$WORKTREE_SH" .loom/scripts/worktree.sh
# Copy any lib helpers worktree.sh sources (none required for sentinel test,
# but copy if present to avoid sourcing errors).
if [[ -d "$SCRIPTS_DIR/lib" ]]; then
    cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
fi
chmod +x .loom/scripts/worktree.sh

# Run worktree.sh for a synthetic issue number. The script's own success
# message is noise here — we only care about the sentinel.
ISSUE_NUM=99
if ./.loom/scripts/worktree.sh "$ISSUE_NUM" >/tmp/worktree-out.$$ 2>&1; then
    assert_file_exists ".loom/worktrees/issue-$ISSUE_NUM/.loom-managed" \
        "worktree.sh creates .loom-managed in the new worktree"
    # Verify content gives operators something to grep for
    if [[ -f ".loom/worktrees/issue-$ISSUE_NUM/.loom-managed" ]]; then
        assert_grep "Loom-managed worktree marker" \
            ".loom/worktrees/issue-$ISSUE_NUM/.loom-managed" \
            "sentinel file contains identifying header"
    fi
else
    fail "worktree.sh failed for issue $ISSUE_NUM (see /tmp/worktree-out.$$)"
fi

cd "$REPO_ROOT"

# --- Test 2: merge-pr.sh cleanup block enforces sentinel + env var ---
echo ""
echo "Test 2: merge-pr.sh cleanup honors sentinel + LOOM_PRESERVE_WORKTREE"

assert_grep 'LOOM_PRESERVE_WORKTREE' "$MERGE_PR_SH" \
    "merge-pr.sh references LOOM_PRESERVE_WORKTREE"
assert_grep '\.loom-managed' "$MERGE_PR_SH" \
    "merge-pr.sh references .loom-managed sentinel"
assert_grep 'refusing to remove.*user-owned' "$MERGE_PR_SH" \
    "merge-pr.sh emits a refusal message for unmanaged worktrees"

# --- Test 3: agent-destroy.sh cleanup block enforces the same guards ---
echo ""
echo "Test 3: agent-destroy.sh cleanup honors sentinel + LOOM_PRESERVE_WORKTREE"

assert_grep 'LOOM_PRESERVE_WORKTREE' "$AGENT_DESTROY_SH" \
    "agent-destroy.sh references LOOM_PRESERVE_WORKTREE"
assert_grep '\.loom-managed' "$AGENT_DESTROY_SH" \
    "agent-destroy.sh references .loom-managed sentinel"

# --- Test 4: inline simulation of cleanup decision logic ---
echo ""
echo "Test 4: cleanup decision logic (inline simulation)"

# Replicate the decision shape from merge-pr.sh in a function so we can
# exercise both the "managed" and "user-owned" branches without a real PR.
simulate_cleanup_decision() {
    local worktree_path="$1"
    local preserve="${2:-0}"
    if [[ "$preserve" == "1" ]]; then
        echo "skip:env"
        return 0
    fi
    if [[ ! -d "$worktree_path" ]]; then
        echo "skip:missing"
        return 0
    fi
    if [[ ! -f "$worktree_path/.loom-managed" ]]; then
        echo "skip:user-owned"
        return 0
    fi
    echo "remove"
}

CASE_DIR=$(mktemp -d /tmp/loom-sentinel-cases.XXXXXX)
trap 'rm -rf "$CASE_DIR"' RETURN

mkdir -p "$CASE_DIR/managed" "$CASE_DIR/unmanaged"
touch "$CASE_DIR/managed/.loom-managed"

result=$(simulate_cleanup_decision "$CASE_DIR/managed")
if [[ "$result" == "remove" ]]; then pass "managed worktree → remove"; else fail "managed worktree expected 'remove', got '$result'"; fi

result=$(simulate_cleanup_decision "$CASE_DIR/unmanaged")
if [[ "$result" == "skip:user-owned" ]]; then pass "unmanaged worktree → skip:user-owned"; else fail "unmanaged worktree expected 'skip:user-owned', got '$result'"; fi

result=$(simulate_cleanup_decision "$CASE_DIR/managed" 1)
if [[ "$result" == "skip:env" ]]; then pass "LOOM_PRESERVE_WORKTREE=1 short-circuits even for managed worktree"; else fail "env-var preserve expected 'skip:env', got '$result'"; fi

rm -rf "$CASE_DIR"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
