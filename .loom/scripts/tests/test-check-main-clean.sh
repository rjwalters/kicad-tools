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
#   - --snapshot FILE records main's porcelain state (#3648)
#   - --baseline FILE ignores pre-existing dirt but flags genuinely-new changes
#   - missing baseline file falls back to whole-status hard-fail (fail-safe)
#   - no-arg invocation remains a byte-for-byte whole-status hard-fail (back-compat)
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
    printf '.loom/worktrees/\n.loom/sweep-checkpoint/\n' > "$dir/.gitignore"
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

# ========================================================================
# Baseline / snapshot mode (#3648)
# ========================================================================

# -------- Test 10: --snapshot writes main's porcelain content, exits 0 --------
echo "Test 10: --snapshot records porcelain state and exits 0"
REPO=$(make_repo)
echo "preexisting" > "$REPO/preexisting.txt"    # pre-existing untracked dirt
# Snapshot lives in a gitignored per-sweep transient dir so it does not itself
# register as new dirt (mirrors the /loom:sweep wiring, #3648).
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 && -f "$SNAP" ]] && grep -q "preexisting.txt" "$SNAP"; then
    pass "--snapshot writes porcelain content and exits 0"
else
    fail "--snapshot: expected 0 + file containing preexisting.txt, got rc=$RC"
fi

# -------- Test 11: baseline + only pre-existing dirt -> exit 0 --------
echo "Test 11: baseline ignores pre-existing dirt (exit 0)"
( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 when only pre-existing dirt remains"; else fail "expected 0, got $RC"; fi

# -------- Test 12: baseline + one genuinely-new file -> exit 3, reports only new --------
echo "Test 12: baseline flags a genuinely-new file (exit 3)"
echo "contamination" > "$REPO/new-contamination.txt"
out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]] \
   && echo "$out" | grep -q "new-contamination.txt" \
   && ! echo "$out" | grep -q "preexisting.txt"; then
    pass "exit 3 flagging only the new path, not pre-existing dirt"
else
    fail "expected 3 reporting only new-contamination.txt, got rc=$RC; out=$out"
fi

# -------- Test 13: baseline + pre-existing persists AND new file appears --------
echo "Test 13: baseline offending list excludes pre-existing dirt"
# preexisting.txt and new-contamination.txt both present; only the new one should be flagged.
out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" 2>&1 ); RC=$?
offending=$(echo "$out" | sed -n '/Offending changes:/,$p')
if [[ "$RC" -eq 3 ]] \
   && echo "$offending" | grep -q "new-contamination.txt" \
   && ! echo "$offending" | grep -q "preexisting.txt"; then
    pass "offending list contains only the new path"
else
    fail "expected offending list with only new path, got rc=$RC; offending=$offending"
fi
rm -f "$REPO/new-contamination.txt"

# -------- Test 14: missing baseline file -> fail-safe whole-status behavior --------
echo "Test 14: missing baseline file falls back to whole-status (fail-safe)"
# preexisting.txt is still dirty; with a missing baseline the check must hard-fail.
out=$( cd "$REPO" && "$SCRIPT" --baseline "$REPO/.loom/does-not-exist.txt" 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]] && echo "$out" | grep -qi "missing or unreadable"; then
    pass "missing baseline warns and hard-fails on pre-existing dirt"
else
    fail "expected 3 + fallback warning, got rc=$RC; out=$out"
fi

# -------- Test 15: back-compat -- no-arg check still hard-fails on any dirt --------
echo "Test 15: no-arg invocation is byte-for-byte hard-fail (back-compat)"
# preexisting.txt still present; the legacy no-arg path must exit 3 regardless of any snapshot.
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "no-arg exit 3 on pre-existing dirt (unchanged contract)"; else fail "expected 3, got $RC"; fi

# -------- Test 16: --snapshot / --baseline require a file argument (exit 2) --------
echo "Test 16: --snapshot and --baseline require a file argument"
"$SCRIPT" --snapshot >/dev/null 2>&1; RC1=$?
"$SCRIPT" --baseline >/dev/null 2>&1; RC2=$?
if [[ "$RC1" -eq 2 && "$RC2" -eq 2 ]]; then
    pass "exit 2 when --snapshot/--baseline missing file arg"
else
    fail "expected 2/2, got snapshot=$RC1 baseline=$RC2"
fi

rm -rf "$REPO"

# ========================================================================
# cwd-reset contamination stand-in: a NEW TRACKED-FILE change on main (#3719)
# ========================================================================
# Tests 12/13 exercise an *untracked* stray file. This case covers the shape
# builders actually hit: after a cwd reset a repo-relative Write lands a new
# SOURCE MODULE in the main worktree, and the builder `git add`s it — a staged
# (tracked) change, not just an untracked one. The baseline backstop must still
# flag it (exit 3, naming the path) while ignoring a change that was already
# recorded in the pre-sweep snapshot. This is the detection defense the issue's
# "test simulating a cwd-reset mid-build" AC retargets at (the prevention guard
# `guard-worktree-paths.sh` cannot fire on the Task-subagent path — see PR body).

# -------- Test 17: baseline flags a NEW staged source module (exit 3) --------
echo "Test 17: baseline flags a new tracked-file change, ignores a baselined one"
REPO=$(make_repo)
# A change that predates the sweep and IS captured in the snapshot: a staged file.
printf 'baseline = 1\n' > "$REPO/baseline_mod.py"
git -C "$REPO" add baseline_mod.py
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -ne 0 ]]; then fail "Test 17 setup: --snapshot expected 0, got $RC"; fi

# Simulate the cwd-reset trap: a NEW source module written to main root, staged.
printf 'def widget():\n    return 42\n' > "$REPO/stray_module.py"
git -C "$REPO" add stray_module.py

out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" 2>&1 ); RC=$?
offending=$(echo "$out" | sed -n '/Offending changes:/,$p')
if [[ "$RC" -eq 3 ]] \
   && echo "$offending" | grep -q "stray_module.py" \
   && ! echo "$offending" | grep -q "baseline_mod.py"; then
    pass "exit 3 naming the new staged module, ignoring the baselined change"
else
    fail "expected 3 reporting only stray_module.py, got rc=$RC; offending=$offending"
fi
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
