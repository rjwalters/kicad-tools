#!/usr/bin/env bash
# test-check-main-clean.sh - Smoke tests for check-main-clean.sh
#
# Exercises the main-worktree contamination backstop (#3513). Each test runs
# against a throwaway temp git repo so the result is deterministic and
# independent of the host repo's pre-existing untracked files.
#
# Verified behavior:
#   - exit 0 when the main worktree is clean
#   - exit 0 when only a gitignored issue worktree exists under .loom/worktrees/
#   - exit 3 when the main worktree has a stray untracked file
#   - exit 3 when the main worktree has a staged change
#   - exit 3 even when invoked from INSIDE a worktree (resolves main correctly)
#   - exit 0 from inside a worktree when main is clean
#   - exit 0 / coherent output for --help
#   - exit 2 for an unknown argument
#
# Usage:
#   ./.loom/scripts/tests/test-check-main-clean.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-main-clean.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

# Run the script, capture its exit code into a global.
run_rc() {
    ( "$@" ) >/dev/null 2>&1
    RC=$?
}

# Create a throwaway git repo with one commit and a gitignored worktree dir.
make_repo() {
    local dir
    dir=$(mktemp -d)
    git -C "$dir" init -q
    git -C "$dir" config user.email t@t.t
    git -C "$dir" config user.name test
    printf '.loom/worktrees/\n' > "$dir/.gitignore"
    git -C "$dir" add .gitignore
    git -C "$dir" commit -q -m init
    echo "$dir"
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-main-clean.sh is executable"
else
    fail "check-main-clean.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: clean main exits 0 --------
echo "Test 2: clean main worktree exits 0"
REPO=$(make_repo)
( cd "$REPO" && run_rc "$SCRIPT" ) && true
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 on clean main"; else fail "expected 0, got $RC"; fi

# -------- Test 3: gitignored issue worktree present is still clean --------
echo "Test 3: gitignored .loom/worktrees/ does not count as dirty"
mkdir -p "$REPO/.loom/worktrees/issue-1"
echo "scratch" > "$REPO/.loom/worktrees/issue-1/foo.txt"
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 with gitignored worktree files"; else fail "expected 0, got $RC"; fi

# -------- Test 4: stray untracked file makes main dirty (exit 3) --------
echo "Test 4: stray untracked file in main exits 3"
echo "stray" > "$REPO/stray.txt"
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "exit 3 on untracked stray file"; else fail "expected 3, got $RC"; fi
rm -f "$REPO/stray.txt"

# -------- Test 5: staged change makes main dirty (exit 3) --------
echo "Test 5: staged change in main exits 3"
echo "content" > "$REPO/tracked.txt"
git -C "$REPO" add tracked.txt
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "exit 3 on staged change"; else fail "expected 3, got $RC"; fi
git -C "$REPO" reset -q HEAD tracked.txt
rm -f "$REPO/tracked.txt"

# -------- Test 6: invoked from inside a worktree, main dirty -> exit 3 --------
echo "Test 6: detects dirty main from inside a worktree"
git -C "$REPO" worktree add -q .loom/worktrees/issue-99 -b feature/issue-99 2>/dev/null
echo "stray2" > "$REPO/stray2.txt"
( cd "$REPO/.loom/worktrees/issue-99" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "exit 3 from worktree when main dirty"; else fail "expected 3, got $RC"; fi

# -------- Test 7: clean main from inside a worktree -> exit 0 --------
echo "Test 7: clean main from inside a worktree exits 0"
rm -f "$REPO/stray2.txt"
( cd "$REPO/.loom/worktrees/issue-99" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 from worktree when main clean"; else fail "expected 0, got $RC"; fi

# -------- Test 8: --help exits 0 and prints usage --------
echo "Test 8: --help exits 0 with usage output"
out=$("$SCRIPT" --help 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"check-main-clean.sh"* ]]; then
    pass "--help prints usage and exits 0"
else
    fail "--help: expected 0 + usage text, got rc=$RC"
fi

# -------- Test 9: unknown argument exits 2 --------
echo "Test 9: unknown argument exits 2"
"$SCRIPT" --bogus >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 2 ]]; then pass "exit 2 on unknown argument"; else fail "expected 2, got $RC"; fi

# Cleanup
git -C "$REPO" worktree remove --force .loom/worktrees/issue-99 2>/dev/null || true
rm -rf "$REPO"

# -------- Summary --------
echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED/$TESTS_RUN tests passed${NC}"
    exit 0
else
    echo -e "${RED}FAILED: $TESTS_FAILED/$TESTS_RUN tests failed${NC}"
    exit 1
fi
