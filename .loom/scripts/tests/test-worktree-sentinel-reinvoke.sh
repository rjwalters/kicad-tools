#!/usr/bin/env bash
# test-worktree-sentinel-reinvoke.sh - Regression tests for issue #3548
#
# The .loom-managed sentinel used to be written ONLY on the successful
# first-creation path. Every "worktree already exists" early-exit branch
# (preserve-existing-work, stale-reset, --sparse re-config, --full re-config)
# returned before that write, so a re-invocation against an existing worktree
# left it sentinel-less. merge-pr.sh's cleanup gate then refused to remove it
# ("user-owned"), permanently stranding the worktree.
#
# Fix: factor the write into write_loom_sentinel() and call it on all five
# early-exit paths plus first-creation. The write is a plain overwrite so it
# is idempotent and self-heals a worktree whose sentinel was deleted.
#
# Coverage:
#   1. delete-sentinel + re-invoke (stale-reset path)      -> sentinel restored
#   2. delete-sentinel + re-invoke (preserve-work path)    -> sentinel restored
#   3. delete-sentinel + re-invoke with --sparse           -> sentinel restored
#   4. delete-sentinel + re-invoke with --full             -> sentinel restored
#   5. NEGATIVE: unregistered directory                    -> exit 1, no sentinel

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_file_exists() {
    if [[ -f "$1" ]]; then pass "$2"; else fail "$2 (expected file: $1)"; fi
}

# Resolve to the physical path (pwd -P): on macOS /tmp is a symlink to
# /private/tmp, and worktree.sh's orphan-cleanup compares `git worktree list`
# paths (physical) against a resolved path. A symlinked temp root would make
# registered worktrees look unregistered and get spuriously recreated,
# defeating the point of these re-invocation tests.
TMP_ROOT=$(cd "$(mktemp -d /tmp/loom-sentinel-reinvoke.XXXXXX)" && pwd -P)
trap 'rm -rf "$TMP_ROOT"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

# Build a fresh throwaway repo with a bare origin/main and a copy of
# worktree.sh, returning its path via stdout.
make_repo() {
    local name="$1"
    local base="$TMP_ROOT/$name"
    git init -q -b main "$base/origin.git" --bare
    git init -q -b main "$base/repo"
    (
        cd "$base/repo"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$base/origin.git"
        git push -q origin main
        mkdir -p .loom/scripts/lib
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh
    )
    echo "$base/repo"
}

# --- Test 1: stale-reset re-invoke restores a deleted sentinel ---
echo "Test 1: re-invoke on a clean (stale) worktree restores deleted sentinel"
REPO=$(make_repo t1)
(
    cd "$REPO"
    SENT=".loom/worktrees/issue-11/.loom-managed"
    ./.loom/scripts/worktree.sh 11 >/dev/null 2>&1
    [[ -f "$SENT" ]] || { echo "setup: first creation did not write sentinel"; exit 1; }
    rm -f "$SENT"   # simulate a worktree that lost its marker
    ./.loom/scripts/worktree.sh 11 >"$TMP_ROOT/t1.out" 2>&1
)
grep -qi "reset to origin/main\|Stale worktree" "$TMP_ROOT/t1.out" \
    && pass "re-invoke took the stale-reset branch" \
    || fail "re-invoke did NOT take the stale-reset branch (see $TMP_ROOT/t1.out)"
assert_file_exists "$REPO/.loom/worktrees/issue-11/.loom-managed" \
    "stale-reset re-invoke re-creates .loom-managed"

# --- Test 2: preserve-work re-invoke restores a deleted sentinel ---
echo ""
echo "Test 2: re-invoke on a worktree with uncommitted work restores sentinel"
REPO=$(make_repo t2)
(
    cd "$REPO"
    SENT=".loom/worktrees/issue-22/.loom-managed"
    ./.loom/scripts/worktree.sh 22 >/dev/null 2>&1
    # Dirty the worktree so the re-invoke takes the preserve-existing-work path.
    echo "wip" > ".loom/worktrees/issue-22/wip.txt"
    rm -f "$SENT"
    ./.loom/scripts/worktree.sh 22 >"$TMP_ROOT/t2.out" 2>&1
)
grep -qi "preserving existing work" "$TMP_ROOT/t2.out" \
    && pass "re-invoke took the preserve-existing-work branch" \
    || fail "re-invoke did NOT take the preserve-work branch (see $TMP_ROOT/t2.out)"
assert_file_exists "$REPO/.loom/worktrees/issue-22/.loom-managed" \
    "preserve-work re-invoke re-creates .loom-managed"
# The preserve path must NOT discard the user's uncommitted work.
assert_file_exists "$REPO/.loom/worktrees/issue-22/wip.txt" \
    "preserve-work re-invoke leaves uncommitted work intact"

# --- Test 3: --sparse re-config restores a deleted sentinel ---
echo ""
echo "Test 3: --sparse re-config of an existing worktree writes the sentinel"
REPO=$(make_repo t3)
(
    cd "$REPO"
    SENT=".loom/worktrees/issue-33/.loom-managed"
    ./.loom/scripts/worktree.sh 33 >/dev/null 2>&1
    rm -f "$SENT"
    ./.loom/scripts/worktree.sh 33 --sparse defaults/scripts >"$TMP_ROOT/t3.out" 2>&1
)
grep -qi "Sparse-checkout cone applied" "$TMP_ROOT/t3.out" \
    && pass "re-invoke took the --sparse re-config branch" \
    || fail "re-invoke did NOT take the --sparse branch (see $TMP_ROOT/t3.out)"
assert_file_exists "$REPO/.loom/worktrees/issue-33/.loom-managed" \
    "--sparse re-config re-creates .loom-managed"

# --- Test 4: --full re-config restores a deleted sentinel ---
echo ""
echo "Test 4: --full re-config of an existing worktree writes the sentinel"
REPO=$(make_repo t4)
(
    cd "$REPO"
    SENT=".loom/worktrees/issue-44/.loom-managed"
    ./.loom/scripts/worktree.sh 44 >/dev/null 2>&1
    rm -f "$SENT"
    ./.loom/scripts/worktree.sh 44 --full >"$TMP_ROOT/t4.out" 2>&1
)
grep -qi "converted to full checkout" "$TMP_ROOT/t4.out" \
    && pass "re-invoke took the --full re-config branch" \
    || fail "re-invoke did NOT take the --full branch (see $TMP_ROOT/t4.out)"
assert_file_exists "$REPO/.loom/worktrees/issue-44/.loom-managed" \
    "--full re-config re-creates .loom-managed"

# --- Test 5: NEGATIVE - the "not a registered worktree" exit-1 paths write no
#     sentinel. This is asserted structurally: an empty orphan directory is
#     auto-healed by cleanup_partial_worktree_state() into a fresh managed
#     worktree (legitimate), so the exit-1 branches are not reachable via a
#     plain empty dir. What must hold is that those exit-1 branches never call
#     write_loom_sentinel — an orphan-debris path must stay sentinel-less so
#     merge-pr.sh keeps refusing it.
echo ""
echo "Test 5: unregistered-worktree exit-1 paths write no sentinel (structural)"

# awk: while inside a block bounded by a "not a registered worktree" marker and
# the next "exit 1", flag any write_loom_sentinel call as a violation.
violations=$(awk '
    /not a registered worktree/ { inblock = 1 }
    inblock && /write_loom_sentinel/ { count++ }
    inblock && /exit 1/ { inblock = 0 }
    END { print count + 0 }
' "$WORKTREE_SH")

if [[ "$violations" -eq 0 ]]; then
    pass "no write_loom_sentinel between 'not a registered worktree' and its exit 1"
else
    fail "found $violations write_loom_sentinel call(s) on an unregistered-worktree exit-1 path"
fi

# Sanity: the exit-1 refusal branches still exist (guards the awk above against
# silently passing if the messages were renamed/removed).
if grep -qc 'not a registered worktree' "$WORKTREE_SH"; then
    pass "worktree.sh still contains 'not a registered worktree' refusal branch(es)"
else
    fail "worktree.sh no longer contains the 'not a registered worktree' refusal branch"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
