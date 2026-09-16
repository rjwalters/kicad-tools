#!/usr/bin/env bash
# test-worktree-race-rescue.sh — Tests for #6334.
#
# worktree.sh's "stale worktree" reset path (`git reset --hard <base>`) is
# gated on a point-in-time staleness check (0 commits ahead, no uncommitted
# changes). Between that check and the reset itself, a second builder — or
# any other process re-entering the same worktree — can write foreign work
# into it; the lease record is evidence a claim exists, not a mutex that
# prevents this (#6165/#6320/#6333). An unqualified `git reset --hard` in
# that window silently discards the other process's work (the #6320
# incident).
#
# Covers defaults/scripts/lib/worktree-race-rescue.sh's
# `loom_worktree_reset_or_rescue`:
#
#   1. Clean worktree -> resets normally, no rescue patch written (fast path
#      unchanged).
#   2. Worktree with foreign uncommitted TRACKED changes -> rescued to a
#      patch file under `.snapshots/` (recoverable via `git apply`) instead
#      of being discarded; reset still lands the worktree at the target ref.
#   3. Worktree with foreign untracked files only -> reset proceeds
#      unconditionally (no patch needed): `git reset --hard` never touches
#      untracked files (this repo's worktree.sh never runs `git clean`), so
#      the untracked file is left exactly where it was, not silently lost.
#   4. The target ref content is correct after a rescue-then-reset (the
#      worktree actually reaches the target state, not just "not the old
#      state").
#   5. A worktree that gained new COMMITS (not just uncommitted changes)
#      since the caller's staleness check refuses the reset outright rather
#      than rewinding history out from under whoever committed it.
#   6. A rescue-patch write failure (unwritable rescue directory) refuses
#      the reset instead of discarding the foreign work it could not
#      capture first.
#   7. (#7463) A live process with its cwd inside the worktree root causes
#      the reset to be refused outright, before any git-level check runs —
#      reported via a stderr message, not a silent no-op — and the worktree
#      is left completely unchanged.
#   8. (#7463) Once that live process exits, the reset proceeds normally —
#      the liveness refusal is not a permanent wedge.
#   9. (#7463) A live process whose cwd is a SUBDIRECTORY of the worktree
#      root (not the root itself) is still detected and still refuses the
#      reset.
#  10. (#7463) The same, but NESTED SEVERAL LEVELS DEEP (an ordinary depth
#      for real work, e.g. `loom-daemon/src/worktree_ops/`). This is the
#      case Test 9 cannot cover on its own: Test 9's subdirectory is a
#      direct child of the worktree root, which lsof's NON-recursive `+d`
#      happens to match, so Test 9 passes even against a `+d` scan that
#      misses everything deeper. Only a depth >= 2 process distinguishes
#      `+d` from the recursive `+D` the lib actually needs.
#
# This is a pure lib-function test (no worktree.sh invocation needed) —
# follows the pattern in test-disk-headroom.sh: source the lib directly,
# drive it against a throwaway repo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RACE_RESCUE_LIB="$SCRIPTS_DIR/lib/worktree-race-rescue.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

# shellcheck source=../lib/worktree-race-rescue.sh
source "$RACE_RESCUE_LIB"

TMP=$(mktemp -d /tmp/loom-race-rescue-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
echo "base content" > tracked.txt
git add tracked.txt
git commit -q -m "base"
BASE_SHA="$(git rev-parse HEAD)"

count_patches() {
    { find "$TMP/repo/.snapshots" -name '*.patch' 2>/dev/null || true; } | wc -l | tr -d ' '
}

# run_reset <worktree_path> <target_ref> [<rescue_label>]
#
# Thin wrapper around loom_worktree_reset_or_rescue that calls it with this
# script's own cwd OUTSIDE the repo under test. The function itself only
# ever operates via explicit paths (`git -C "$worktree_path"` etc.), so it
# does not depend on the caller's cwd — but the #7463 liveness check this
# test file exercises does look at every live process's cwd, and this
# script's own shell sits at `cd "$TMP/repo"` for the rest of its body (so
# the surrounding tests can keep using bare relative paths like
# `tracked.txt`). Calling the function directly from that cwd would make
# THIS SCRIPT's own shell (and the transient process it forks to run lsof)
# self-match as a "live process inside the worktree" — a test-harness
# artifact, not a real orphan. Stepping out to $TMP for the duration of the
# call avoids that false self-match without touching the rest of the file's
# relative-path style.
run_reset() {
    local rc
    ( cd "$TMP" && loom_worktree_reset_or_rescue "$@" )
    rc=$?
    return "$rc"
}

# --- Test 1: clean worktree -> plain reset, no rescue patch written ---
echo "Test 1: clean worktree resets without writing a rescue patch"
PATCHES_BEFORE="$(count_patches)"
if run_reset "$TMP/repo" "$BASE_SHA" "test-rescue"; then
    pass "clean worktree: reset succeeded"
else
    fail "clean worktree: reset should have succeeded"
fi
assert_eq "$(count_patches)" "$PATCHES_BEFORE" "clean worktree: no rescue patch was written"

# --- Test 2: foreign uncommitted TRACKED change is rescued, not discarded ---
echo "Test 2: foreign tracked change is rescued to a patch file before reset"
echo "foreign edit" > tracked.txt
if run_reset "$TMP/repo" "$BASE_SHA" "test-rescue"; then
    pass "dirty (tracked) worktree: reset succeeded"
else
    fail "dirty (tracked) worktree: reset should have succeeded (after rescue)"
fi
PATCH_FILE="$(find "$TMP/repo/.snapshots" -name '*.patch' 2>/dev/null | head -1)"
if [[ -n "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
    pass "dirty (tracked) worktree: exactly one non-empty rescue patch exists"
else
    fail "dirty (tracked) worktree: expected a non-empty rescue patch, found none"
fi
RESCUED_CONTENT="$(grep '^+' "$PATCH_FILE" | grep -v '^+++' | sed 's/^+//')"
assert_eq "$RESCUED_CONTENT" "foreign edit" "dirty (tracked) worktree: the discarded content is recoverable from the patch"
CURRENT_CONTENT="$(cat tracked.txt)"
assert_eq "$CURRENT_CONTENT" "base content" "dirty (tracked) worktree: worktree reached the target ref's content after rescue"
rm -rf "$TMP/repo/.snapshots"

# --- Test 3: foreign UNTRACKED file needs no rescue — reset never touches it ---
echo "Test 3: foreign untracked file survives the reset untouched (no patch needed)"
echo "untracked scratch" > scratch.txt
if run_reset "$TMP/repo" "$BASE_SHA" "test-rescue"; then
    pass "dirty (untracked-only) worktree: reset succeeded"
else
    fail "dirty (untracked-only) worktree: reset should have succeeded"
fi
if [[ -f scratch.txt && "$(cat scratch.txt)" == "untracked scratch" ]]; then
    pass "dirty (untracked-only) worktree: untracked file is left in place, content intact"
else
    fail "dirty (untracked-only) worktree: untracked file should survive git reset --hard unmodified"
fi
assert_eq "$(count_patches)" "0" "dirty (untracked-only) worktree: no rescue patch was written (nothing tracked was at risk)"
rm -f scratch.txt

# --- Test 4: reset actually lands on the target ref, not just "changed" ---
echo "Test 4: reset lands the worktree on the correct target ref"
# HEAD is currently at BASE_SHA (clean, left there by test 3's reset). Create
# a forward commit and reset onto it directly: HEAD is an ancestor of the
# target, so "ahead(target..HEAD)" is 0 and the reset must proceed, landing
# exactly on the requested ref rather than merely "somewhere different".
echo "second commit" > tracked.txt
git add tracked.txt
git commit -q -m "second"
SECOND_SHA="$(git rev-parse HEAD)"
git reset -q --hard "$BASE_SHA"
if run_reset "$TMP/repo" "$SECOND_SHA" "test-rescue"; then
    pass "reset onto a forward target ref succeeded"
else
    fail "reset onto a forward target ref should have succeeded"
fi
CURRENT_HEAD="$(git rev-parse HEAD)"
assert_eq "$CURRENT_HEAD" "$SECOND_SHA" "reset landed HEAD on the requested target ref ($SECOND_SHA), not $BASE_SHA"
rm -rf "$TMP/repo/.snapshots"

# --- Test 5: new commits since the staleness check refuse the reset ---
echo "Test 5: a worktree that gained new commits refuses the reset outright"
git reset -q --hard "$BASE_SHA"
echo "a real commit that landed in the race window" > tracked.txt
git add tracked.txt
git commit -q -m "raced commit"
RACED_SHA="$(git rev-parse HEAD)"
set +e
run_reset "$TMP/repo" "$BASE_SHA" "test-rescue" >/tmp/loom-race-rescue-test-stderr.$$ 2>&1
RC=$?
set -e
assert_eq "$RC" "1" "worktree ahead of target: helper refuses to reset (returns 1)"
CURRENT_HEAD="$(git rev-parse HEAD)"
assert_eq "$CURRENT_HEAD" "$RACED_SHA" "worktree ahead of target: the raced commit is still present (reset was never attempted)"
rm -f "/tmp/loom-race-rescue-test-stderr.$$"

# --- Test 6: rescue-patch write failure refuses the reset ---
echo "Test 6: a failed rescue-patch write refuses the reset instead of discarding foreign work"
git reset -q --hard "$RACED_SHA"
echo "precious foreign work" > tracked.txt
# Force the rescue-directory mkdir to fail by pre-creating a same-named
# regular FILE at the .snapshots path (mkdir -p on top of a file fails
# cleanly, unlike corrupting .git/objects — which would also break the
# earlier `git diff HEAD --quiet` read this helper depends on to detect
# "dirty" in the first place, masking the case this test targets).
rm -rf "$TMP/repo/.snapshots"
: > "$TMP/repo/.snapshots"
set +e
run_reset "$TMP/repo" "$RACED_SHA" "test-rescue" >/tmp/loom-race-rescue-test-stderr2.$$ 2>&1
RC=$?
set -e
rm -f "$TMP/repo/.snapshots"
assert_eq "$RC" "1" "unrescuable dirty worktree: helper returns 1 (refuses to reset)"
CURRENT_CONTENT="$(cat tracked.txt)"
assert_eq "$CURRENT_CONTENT" "precious foreign work" "unrescuable dirty worktree: foreign work is still present (reset was never attempted)"
rm -f "/tmp/loom-race-rescue-test-stderr2.$$"
git checkout -q -- tracked.txt 2>/dev/null || echo "base content" > tracked.txt

# --- Test 7: a live process with cwd inside the worktree refuses the reset (#7463) ---
echo "Test 7: a live process with cwd inside the worktree causes the reset to be refused"
git reset -q --hard "$BASE_SHA"
# Same forward-target setup as Test 4 (0 commits ahead, clean tree — the
# reset would normally proceed) but with a live process holding the
# worktree open via its cwd. exec replaces the backgrounded subshell with
# sleep directly, so $! is the PID lsof will actually find as "cwd" here.
(cd "$TMP/repo" && exec sleep 60) &
LIVE_PID=$!
sleep 0.3
set +e
run_reset "$TMP/repo" "$SECOND_SHA" "test-rescue" >/tmp/loom-race-rescue-test-stderr3.$$ 2>&1
RC=$?
set -e
kill "$LIVE_PID" 2>/dev/null || true
wait "$LIVE_PID" 2>/dev/null || true
assert_eq "$RC" "1" "live process holding worktree root: helper refuses to reset (returns 1)"
CURRENT_HEAD="$(git rev-parse HEAD)"
assert_eq "$CURRENT_HEAD" "$BASE_SHA" "live process holding worktree root: HEAD is unchanged (reset was never attempted)"
if grep -qi "live process" "/tmp/loom-race-rescue-test-stderr3.$$"; then
    pass "live process holding worktree root: refusal is reported via a message, not a silent no-op"
else
    fail "live process holding worktree root: expected a refusal message mentioning the live process"
fi
rm -f "/tmp/loom-race-rescue-test-stderr3.$$"

# --- Test 8: once the live process exits, the reset proceeds normally ---
echo "Test 8: once the live process exits, the reset proceeds normally"
if run_reset "$TMP/repo" "$SECOND_SHA" "test-rescue"; then
    pass "live process gone: reset succeeded"
else
    fail "live process gone: reset should have succeeded now that nothing holds the worktree open"
fi
CURRENT_HEAD="$(git rev-parse HEAD)"
assert_eq "$CURRENT_HEAD" "$SECOND_SHA" "live process gone: reset landed HEAD on the requested target ref"

# --- Test 9: a live process in a SUBDIRECTORY of the worktree also refuses the reset ---
echo "Test 9: a live process with cwd in a subdirectory of the worktree still refuses the reset"
git reset -q --hard "$BASE_SHA"
mkdir -p "$TMP/repo/subdir"
(cd "$TMP/repo/subdir" && exec sleep 60) &
LIVE_PID=$!
sleep 0.3
set +e
run_reset "$TMP/repo" "$SECOND_SHA" "test-rescue" >/tmp/loom-race-rescue-test-stderr4.$$ 2>&1
RC=$?
set -e
kill "$LIVE_PID" 2>/dev/null || true
wait "$LIVE_PID" 2>/dev/null || true
assert_eq "$RC" "1" "live process holding a subdirectory: helper refuses to reset (returns 1)"
CURRENT_HEAD="$(git rev-parse HEAD)"
assert_eq "$CURRENT_HEAD" "$BASE_SHA" "live process holding a subdirectory: HEAD is unchanged (reset was never attempted)"
rm -f "/tmp/loom-race-rescue-test-stderr4.$$"
rm -rf "$TMP/repo/subdir"

# --- Test 10: a live process nested SEVERAL LEVELS DEEP also refuses the reset ---
#
# Regression guard for the `+d`-vs-`+D` gap. Test 9's subdirectory is a
# direct child of the worktree root, which lsof's non-recursive `+d` matches
# by accident — so Test 9 alone passes even when everything deeper than one
# level is invisible to the scan. This case puts the live process three
# levels down (the shape of a real agent working in, say,
# `loom-daemon/src/worktree_ops/`), which ONLY the recursive `+D` scan can
# see. Against `+d` this test fails: the reset proceeds and HEAD moves.
echo "Test 10: a live process nested several levels deep still refuses the reset"
git reset -q --hard "$BASE_SHA"
mkdir -p "$TMP/repo/nested/two/three"
(cd "$TMP/repo/nested/two/three" && exec sleep 60) &
LIVE_PID=$!
sleep 0.3
set +e
run_reset "$TMP/repo" "$SECOND_SHA" "test-rescue" >/tmp/loom-race-rescue-test-stderr5.$$ 2>&1
RC=$?
set -e
kill "$LIVE_PID" 2>/dev/null || true
wait "$LIVE_PID" 2>/dev/null || true
assert_eq "$RC" "1" "live process nested 3 levels deep: helper refuses to reset (returns 1)"
CURRENT_HEAD="$(git rev-parse HEAD)"
assert_eq "$CURRENT_HEAD" "$BASE_SHA" "live process nested 3 levels deep: HEAD is unchanged (reset was never attempted)"
if grep -qi "live process" "/tmp/loom-race-rescue-test-stderr5.$$"; then
    pass "live process nested 3 levels deep: refusal is reported via a message, not a silent no-op"
else
    fail "live process nested 3 levels deep: expected a refusal message mentioning the live process"
fi
rm -f "/tmp/loom-race-rescue-test-stderr5.$$"
rm -rf "$TMP/repo/nested"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
