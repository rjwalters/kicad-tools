#!/usr/bin/env bash
# test-worktree-orphan-guard-spaces.sh - Regression tests for the orphan
# worktree guard formerly in worktree.sh's cleanup_partial_worktree_state()
# (#7849), ported to `loom-daemon worktree-cleanup` by #8195 slice 5.
#
# The guard decides whether a worktree directory is registered with git. If it
# concludes "no", it `rm -rf`s the directory. Before #7849 it parsed the
# `worktree <path>` porcelain line with awk's `$2`, which truncates at the
# first space, and resolved the directory with logical `pwd`, which keeps the
# symlinked form. Either mismatch makes the comparison miss, so a LIVE,
# registered worktree (with uncommitted work in it) was deleted — inverting the
# function's own sentinel contract (#3334): "a dir that IS registered with git
# is NEVER removed by this helper".
#
# This is the worktree.sh instance of the bug class #3717 fixed in merge-pr.sh;
# #3717 scoped itself to merge-pr.sh and missed this one.
#
# Test strategy:
#   The subject is now `loom-daemon worktree-cleanup`, which this suite invokes
#   DIRECTLY — the same granularity the pre-port suite had when it scraped
#   cleanup_partial_worktree_state() out of the live worktree.sh and eval'd it.
#   Every behavioral assertion below (tests 2/3/4) is unchanged from the shell
#   implementation; running them against the port is the equivalence evidence
#   for deleting it. Only Test 1 could not survive — it read shell idioms out
#   of the shell source — and it is RETIRED in place (per
#   defaults/docs/verification-recipes.md §6) rather than deleted, so a reader
#   can still see what it proved and what proves it now.
#
#   The binary is pinned via loom_test_require_daemon_bin, so this suite FAILS
#   rather than skips when there is none: a suite that skipped would remove the
#   port's equivalence evidence from CI while still reporting green.
#
#   1. RETIRED: static idiom check (the body used substr($0, 10) and `pwd -P`).
#   2. Behavioral: a registered worktree under a path containing a SPACE
#      reports registered and is NOT removed (uncommitted work survives).
#   3. Behavioral: a genuinely unregistered dir IS still removed (the orphan
#      cleanup this function exists for does not regress).
#   4. Behavioral: a registered worktree reached through a SYMLINKED path
#      (macOS /var -> /private/var shape) is NOT removed (the `pwd -P` half).
#   5. Fixture sanity: the pre-fix pipeline ($2 + logical pwd) reports
#      "not registered" on the very same fixture, proving tests 2/4 are not
#      hollow. Deliberately kept as SHELL: it is a frozen reference copy of the
#      pre-#7849 bug, not a copy of the implementation, so it has nothing to
#      drift from.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-worktree-orphan-guard-spaces.sh
#
# Portability: bash 3.2 (macOS) and BSD awk; no mapfile/declare -A, no GNU-only
# flags.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-cleanup"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# An assertion that CANNOT survive the port to Rust, retired under the
# three-part test in defaults/docs/verification-recipes.md §6. Printed, not
# deleted: a reader must be able to see what was removed, why, and what proves
# the property now. Counted as run so the totals stay honest.
retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
}

# run_cleanup <cwd> <issue>  — run the guard with cwd=<cwd>.
# Echoes whatever the guard warns about, on stdout.
#
# The environment mirrors what worktree.sh gives it, minus anything ambient
# that would let the HOST decide the answer:
#   PWD                       — the logical cwd bash would have exported; the
#                               port reads it the way bash's own `pwd` does.
#   LOOM_CONFIG_DEFAULTS_FILE — emptied so a machine-level defaults file cannot
#                               redirect the worktree root out from under the
#                               fixture. The pre-port suite got this hermeticity
#                               by stubbing loom_worktree_root to its documented
#                               DEFAULT tier, `<repo_root>/.loom/worktrees`;
#                               with no config anywhere, that is the tier the
#                               port reaches too.
#   LOOM_WORKTREE_ROOT        — unset for the same reason, one tier up.
run_cleanup() {
    local cdir="$1" issue="$2"
    (
        cd "$cdir" || exit 1
        unset LOOM_WORKTREE_ROOT
        PWD="$cdir" \
        LOOM_CONFIG_DEFAULTS_FILE="" \
            "$LOOM_DAEMON_SELF_BIN" worktree-cleanup "$issue"
    ) 2>&1
}

# --- Fixture: a real repo + a real registered worktree under a SPACE path ---
TMP="$(cd "$(mktemp -d /tmp/loom-orphan-guard.XXXXXX)" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

BASE="$TMP/My Repos"
REPO="$BASE/repo"
mkdir -p "$REPO"
git init -q -b main "$REPO"
git -C "$REPO" config user.email t@t
git -C "$REPO" config user.name t
git -C "$REPO" commit -q --allow-empty -m init

LIVE_WT="$REPO/.loom/worktrees/issue-42"
mkdir -p "$REPO/.loom/worktrees"
git -C "$REPO" worktree add -q -b feature/issue-42 "$LIVE_WT"
echo "precious uncommitted work" > "$LIVE_WT/PRECIOUS.txt"

# --- Test 1: RETIRED (was: the scraped body uses the space-safe idioms) ---
echo "Test 1: worktree-line parse and candidate resolution are space/symlink safe"

retired "cleanup_partial_worktree_state() source uses substr(\$0, 10) and \`pwd -P\` (3 assertions)" \
    "a porcelain path is read WHOLE (a space must not truncate it) and the candidate is resolved PHYSICALLY before comparison — either mismatch makes a LIVE worktree read as an orphan and get rm -rf'd (#7849)" \
    "both idioms were shell spellings of the property, and the shell is gone (#8195 slice 5). In Rust the path is line.strip_prefix(\"worktree \") — one value, nothing to word-split — and the candidate goes through fs::canonicalize, which has no logical variant to forget. There is no source text left in which a grep could find the wrong spelling" \
    "STRICTLY STRONGER, three ways: (a) worktree_cli::cleanup::registered_worktrees now CALLS branch_delete::parse_worktree_porcelain rather than mirroring it, and cleanup/tests.rs::a_porcelain_path_containing_spaces_is_read_whole asserts the value, not the spelling; (b) tests 2 and 4 below drive the real binary end-to-end on exactly the fixtures these idioms existed for; (c) tests/worktree_cleanup_differential.rs replays a generated corpus of space-bearing and symlinked path shapes through BOTH the frozen retired shell and the port and compares stdout, the surviving filesystem and \`git worktree list --porcelain\` byte-for-byte"

# --- Test 2: a registered worktree under a space path is NOT removed ---
echo ""
echo "Test 2: registered worktree under a path containing a space survives"

OUT="$(run_cleanup "$REPO" 42)"

if [[ -d "$LIVE_WT" ]]; then
    pass "live worktree dir still exists after cleanup"
else
    fail "live worktree dir was DELETED by the orphan guard (the #7849 data-loss bug)"
fi

if [[ -f "$LIVE_WT/PRECIOUS.txt" ]]; then
    pass "uncommitted work in the live worktree survived"
else
    fail "uncommitted work in the live worktree was destroyed"
fi

if printf '%s\n' "$OUT" | grep -q "Removing orphan worktree dir"; then
    fail "guard reported the registered worktree as an orphan: $OUT"
else
    pass "guard did not classify the registered worktree as an orphan"
fi

if git -C "$REPO" worktree list --porcelain | grep -Fq "worktree $LIVE_WT"; then
    pass "worktree is still registered with git after cleanup"
else
    fail "worktree registration was lost after cleanup"
fi

# --- Test 3: a genuinely unregistered dir IS still removed ---
echo ""
echo "Test 3: unregistered orphan dir is still removed (no regression)"

ORPHAN_WT="$REPO/.loom/worktrees/issue-77"
mkdir -p "$ORPHAN_WT"
echo "debris" > "$ORPHAN_WT/leftover.txt"

OUT="$(run_cleanup "$REPO" 77)"

if [[ -d "$ORPHAN_WT" ]]; then
    fail "orphan dir was NOT removed — orphan cleanup regressed"
else
    pass "orphan dir was removed"
fi

if printf '%s\n' "$OUT" | grep -q "Removing orphan worktree dir"; then
    pass "guard warned about removing the orphan dir"
else
    fail "guard removed the orphan dir without the expected warning: $OUT"
fi

# --- Test 4: a registered worktree reached through a SYMLINK survives ---
echo ""
echo "Test 4: registered worktree reached through a symlinked path survives"

ln -s "$BASE" "$TMP/link"
LINK_REPO="$TMP/link/repo"

OUT="$(run_cleanup "$LINK_REPO" 42)"

if [[ -d "$LIVE_WT" ]] && [[ -f "$LIVE_WT/PRECIOUS.txt" ]]; then
    pass "live worktree survived a cleanup run from the symlinked path"
else
    fail "live worktree was DELETED when reached through a symlinked path (logical pwd bug)"
fi

if printf '%s\n' "$OUT" | grep -q "Removing orphan worktree dir"; then
    fail "guard reported the registered worktree as an orphan via the symlink: $OUT"
else
    pass "guard did not classify the symlink-reached worktree as an orphan"
fi

# --- Test 5: fixture sanity — the pre-fix pipeline DOES misfire here ---
# Deliberately buggy reference copy of the pre-#7849 pipeline. If this ever
# starts reporting "registered", the fixture no longer exercises the bug and
# tests 2/4 are hollow. Still shell after the port, and correctly so: it is a
# frozen copy of the BUG, not of the implementation, so there is nothing for it
# to drift away from.
echo ""
echo "Test 5: pre-fix pipeline misclassifies the fixture (fixture sanity)"

buggy_registered() {
    local wt_path="$1" abs_wt
    abs_wt=$(cd "$wt_path" 2>/dev/null && pwd) || abs_wt=""
    if [[ -n "$abs_wt" ]] && git -C "$REPO" worktree list --porcelain 2>/dev/null \
        | awk '/^worktree / {print $2}' \
        | grep -Fxq "$abs_wt"; then
        echo 1
    else
        echo 0
    fi
}

if [[ "$(buggy_registered "$LIVE_WT")" == "0" ]]; then
    pass "pre-fix awk \$2 misses the space-containing path (would have rm -rf'd it)"
else
    fail "pre-fix awk \$2 matched — the space fixture no longer exercises the bug"
fi

if [[ "$(cd "$TMP/link" && pwd)" == "$(cd "$TMP/link" && pwd -P)" ]]; then
    fail "symlink fixture is degenerate — logical and physical pwd are identical"
else
    pass "symlink fixture yields a logical path differing from the physical one"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
