#!/usr/bin/env bash
# test-worktree-orphan-guard-spaces.sh - Regression tests for the orphan
# worktree guard in worktree.sh's cleanup_partial_worktree_state() (#7849).
#
# The guard decides whether a worktree directory is registered with git. If it
# concludes "no", it `rm -rf`s the directory. Before #7849 it parsed the
# `worktree <path>` porcelain line with awk's `$2`, which truncates at the
# first space, and resolved the directory with logical `pwd`, which keeps the
# symlinked form. Either mismatch makes `grep -Fxq` miss, so a LIVE, registered
# worktree (with uncommitted work in it) was deleted — inverting the function's
# own sentinel contract (#3334): "a dir that IS registered with git is NEVER
# removed by this helper".
#
# This is the worktree.sh instance of the bug class #3717 fixed in merge-pr.sh;
# #3717 scoped itself to merge-pr.sh and missed this one.
#
# Test strategy:
#   The function body is SCRAPED FROM THE LIVE worktree.sh SOURCE and eval'd,
#   never re-implemented here — the four inline `$2` copies in
#   test-merge-pr-worktree-awk.sh / test-merge-pr-worktree-path.sh show how
#   fast a hand-copied awk program drifts from the implementation it claims to
#   cover. Only two collaborators are stubbed (print_warning, and
#   loom_worktree_root -> its documented default tier, `<repo>/.loom/worktrees`)
#   so the test stays hermetic: no jq, no config tier chain, no private-defaults
#   file on the host.
#
#   1. Static: the scraped body uses substr($0, 10) and `pwd -P`.
#   2. Behavioral: a registered worktree under a path containing a SPACE
#      reports registered and is NOT removed (uncommitted work survives).
#   3. Behavioral: a genuinely unregistered dir IS still removed (the orphan
#      cleanup this function exists for does not regress).
#   4. Behavioral: a registered worktree reached through a SYMLINKED path
#      (macOS /var -> /private/var shape) is NOT removed (the `pwd -P` half).
#   5. Fixture sanity: the pre-fix pipeline ($2 + logical pwd) reports
#      "not registered" on the very same fixture, proving tests 2/4 are not
#      hollow.
#
# Portability: bash 3.2 (macOS) and BSD awk; no mapfile/declare -A, no GNU-only
# flags.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

[[ -f "$WORKTREE_SH" ]] || { echo "ERROR: $WORKTREE_SH not found" >&2; exit 1; }

# --- Scrape cleanup_partial_worktree_state() from the live source ----------
# The function is defined at column 0 and closes with a `}` at column 0.
extract_function() {
    awk -v fn="$1" '
        $0 == fn "() {" { inside = 1 }
        inside          { print }
        inside && $0 == "}" { exit }
    ' "$2"
}

CLEANUP_BODY="$(extract_function cleanup_partial_worktree_state "$WORKTREE_SH")"

if [[ -z "$CLEANUP_BODY" ]] || ! printf '%s\n' "$CLEANUP_BODY" | grep -q 'rm -rf "\$wt_path"'; then
    echo "ERROR: could not scrape cleanup_partial_worktree_state() from $WORKTREE_SH" >&2
    echo "       (the test harness is now out of sync with the source layout)" >&2
    exit 1
fi

# run_cleanup <cwd> <issue>  — run the scraped function with cwd=<cwd>.
# Echoes whatever the function warns about, on stdout.
run_cleanup() {
    local cdir="$1" issue="$2"
    (
        cd "$cdir" || exit 1
        # JSON_OUTPUT / print_warning / loom_worktree_root are consumed by the
        # eval'd body below, which shellcheck cannot see.
        # shellcheck disable=SC2034
        JSON_OUTPUT=false
        print_warning() { echo "WARN: $*"; }
        # Default tier of the real loom_worktree_root() (lib/worktree-root.sh):
        # `<repo_root>/.loom/worktrees`. Stubbed to keep the test hermetic.
        loom_worktree_root() { echo "$1/.loom/worktrees"; }
        eval "$CLEANUP_BODY"
        cleanup_partial_worktree_state "$issue"
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

# --- Test 1: the scraped body uses the space-safe / symlink-safe idioms ---
echo "Test 1: cleanup_partial_worktree_state() source uses substr(\$0, 10) and pwd -P"

if printf '%s\n' "$CLEANUP_BODY" | grep -q 'substr(\$0, 10)'; then
    pass "worktree-line parse uses substr(\$0, 10) (space-safe)"
else
    fail "worktree-line parse does NOT use substr(\$0, 10) — a path with a space truncates at \$2"
fi

if printf '%s\n' "$CLEANUP_BODY" | grep -qE '/\^worktree / \{ *print \$2 *\}'; then
    fail "worktree-line parse still uses awk \$2 (truncates at the first space)"
else
    pass "worktree-line parse no longer uses awk \$2"
fi

if printf '%s\n' "$CLEANUP_BODY" | grep -q 'abs_wt=$(cd "$wt_path" 2>/dev/null && pwd -P)'; then
    pass "abs_wt resolves with pwd -P (matches porcelain's resolved paths)"
else
    fail "abs_wt does NOT resolve with pwd -P — a symlinked path yields a false 'not registered'"
fi

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
# tests 2/4 are hollow.
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
