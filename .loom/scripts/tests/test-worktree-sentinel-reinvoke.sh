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
#
# SINCE #8195 SLICE 10 (epic #7810) the --sparse/--full re-configure arm that
# Tests 3 and 4 drive is `loom-daemon worktree-sparse --arm reconfigure`,
# which now owns that arm's registration check and its sentinel back-fill.
# Every assertion below is unchanged from the shell implementation, which is
# what makes Tests 3/4 equivalence evidence for the port, so the binary is
# pinned via loom_test_require_daemon_bin and this suite FAILS rather than
# skips when there is none: without one, --sparse/--full refuse with exit 2
# before touching anything, and Tests 3/4 would be asserting on a refusal.
#
# SINCE #8195 SLICE 12 the "worktree directory already exists" arm that Tests 1
# and 2 drive is `loom-daemon worktree-existing`, which now owns that arm's
# registration probe, its preserve-vs-reset verdict and its sentinel back-fill.
# Tests 1-4 are unchanged and still drive the real `worktree.sh` end to end,
# which is what makes them the equivalence evidence; the binary is pinned for
# both subcommands below. Test 5 could not survive that move and is RETIRED
# in place — see its own block for the property, the structural reason and the
# successor.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-sparse" "worktree-existing"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# --- Test 5: RETIRED (#8195 slice 12) ------------------------------------
#
# What it was: a structural scan of worktree.sh asserting that no
# `write_loom_sentinel` call sits between a "not a registered worktree" marker
# and its `exit 1`, plus a sanity grep that such a refusal branch still exists.
#
# Why it cannot survive: slice 12 moved the last of those refusal branches out
# of worktree.sh into `loom-daemon worktree-existing`. The phrase now appears in
# that file exactly once, inside a COMMENT, so both halves of the scan would
# keep passing — against a comment, for the wrong reason. An assertion that
# passes because of a comment is worse than no assertion.
#
# The three-part test (defaults/docs/verification-recipes.md §6) is spelled out
# in the retired() call below rather than only here, so it travels with the
# suite.
echo ""
echo "Test 5: unregistered-worktree exit-1 paths write no sentinel"

retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
}

retired \
    "static scan: no write_loom_sentinel on an unregistered-worktree exit-1 path" \
    "an orphan-debris directory must never receive a .loom-managed sentinel — that marker is what merge-pr.sh and the reaper read as authorization to rm -rf it (#3334/#3548)" \
    "the refusal branch the scan read is no longer in worktree.sh: #8195 slice 12 moved the whole 'worktree dir already exists' arm into loom-daemon worktree-existing, and the sentinel write there is unreachable from the Unregistered outcome by control flow, not by care" \
    "loom_daemon::worktree_cli::existing::tests::an_unregistered_directory_is_refused_and_gets_no_sentinel and ::a_prefix_of_a_registered_worktree_path_is_not_registered assert the absence behaviourally; tests/worktree_existing_differential.rs::a_prefix_of_a_registered_path_makes_the_shell_accept_debris additionally compares the port against the retired shell on the very input that made the old scan's subject write one"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
