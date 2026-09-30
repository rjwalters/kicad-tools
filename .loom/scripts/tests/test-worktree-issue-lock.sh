#!/usr/bin/env bash
# test-worktree-issue-lock.sh - Tests for worktree.sh's issue claim-lock
# cross-check (#8553).
#
# Before #8553, worktree.sh never consulted `.loom/locks/issue-<N>/` -- the
# daemon's per-issue sweep-claim lock -- so a second, independently-driven
# session could create a worktree for an issue a live sweep already owned,
# with no warning. The two sessions then shared one working tree.
#
# Verifies:
#   1. No lock: worktree creation proceeds normally.
#   2. A live lock (owner_pid alive): worktree.sh refuses (nonzero exit), no
#      worktree is created, and the message names the sweep/pid.
#   3. The same live lock with --force: worktree.sh proceeds and warns.
#   4. A dead-pid lock: fails open, worktree creation proceeds.
#   5. Reverse order (#8553 AC2): the worktree already exists, THEN a live
#      lock appears -- a second invocation for the same issue is still
#      refused, identically to the lock-first ordering.
#   6. --json refusal emits the documented error schema.
#   7. #8702: a live lock whose sweep_id matches the caller's own
#      LOOM_SWEEP_ID proceeds -- a sweep is not refused by its own claim.
#   8. #8702: a live lock with a DIFFERENT (or unset) LOOM_SWEEP_ID still
#      refuses -- the #8553 incident this suite exists to pin.
#
# Needs a BUILT loom-daemon: the check is `loom-daemon worktree-lock
# check-issue`, reached through worktree.sh's single delegating call (this
# file is frozen by the file-size ratchet, so the decision logic lives in
# Rust, not shell). Wired in the "Native Port Suites" CI job, which builds
# the binary, and FAILS rather than skips without one.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-worktree-issue-lock.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-lock"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# --- Throwaway repo setup ---------------------------------------------------
TMP=$(mktemp -d /tmp/loom-issue-lock-test.XXXXXX)
FOREIGN_PID=""
trap 'rm -rf "$TMP"; cd "$SCRIPTS_DIR" 2>/dev/null || true; [[ -n "$FOREIGN_PID" ]] && kill "$FOREIGN_PID" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

mkdir -p .loom/scripts/lib
cp "$WORKTREE_SH" .loom/scripts/worktree.sh
if [[ -d "$SCRIPTS_DIR/lib" ]]; then
    cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
fi
chmod +x .loom/scripts/worktree.sh

# write_issue_lock <issue> <owner_pid> <sweep_id> -- stage a live-shaped
# claim lock at the DAEMON's schema (sweep_registry::locks::LockOwner), which
# is what worktree.sh's cross-check reads.
write_issue_lock() {
    local issue="$1" pid="$2" sweep_id="$3"
    mkdir -p ".loom/locks/issue-$issue"
    cat > ".loom/locks/issue-$issue/owner.json" <<EOF
{
  "issue": $issue,
  "owner_pid": $pid,
  "acquired_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "sweep_id": "$sweep_id"
}
EOF
}

# a_dead_pid -- print a pid guaranteed not to belong to any live process.
a_dead_pid() {
    local pid
    bash -c 'exit 0' &
    pid=$!
    wait "$pid" 2>/dev/null || true
    printf '%s\n' "$pid"
}

# a_foreign_pid -- spawn a background process that is alive but, unlike $$,
# is NOT an ancestor of the worktree.sh invocations under test -- a genuinely
# foreign owner. Stays alive until the EXIT trap kills it via $FOREIGN_PID.
# Redirects the background process's stdio away from /dev/null explicitly:
# left inherited, it would hold open the pipe `$(a_foreign_pid)` reads from,
# and the substitution would hang until the 300s sleep exits.
a_foreign_pid() {
    sleep 300 >/dev/null 2>&1 &
    FOREIGN_PID=$!
    printf '%s\n' "$FOREIGN_PID"
}

# --- Test 1: no lock -> worktree creation proceeds normally ----------------
echo "Test 1: no issue lock present -- worktree creation proceeds"
if ./.loom/scripts/worktree.sh 301 >/tmp/wtl-out1.$$ 2>&1; then
    if [[ -f .loom/worktrees/issue-301/.loom-managed ]]; then
        pass "worktree created with no lock present"
    else
        fail "worktree.sh exited 0 but .loom-managed sentinel missing"
    fi
else
    fail "worktree.sh failed with no lock present (see /tmp/wtl-out1.$$)"
fi
rm -f /tmp/wtl-out1.$$

# --- Test 2: a live lock refuses, and creates nothing -----------------------
echo ""
echo "Test 2: a live claim lock refuses worktree creation (#8553's incident)"
FOREIGN_PID="$(a_foreign_pid)"
write_issue_lock 302 "$FOREIGN_PID" "sweep-issue-302-live"
if ./.loom/scripts/worktree.sh 302 >/tmp/wtl-out2.$$ 2>&1; then
    fail "worktree.sh exited 0 despite a live claim lock (see /tmp/wtl-out2.$$)"
else
    if [[ ! -d .loom/worktrees/issue-302 ]] && grep -q "sweep-issue-302-live" /tmp/wtl-out2.$$; then
        pass "worktree.sh refused (nonzero exit), created nothing, and named the live sweep"
    else
        fail "worktree.sh refused but either created a worktree or omitted the sweep id (see /tmp/wtl-out2.$$)"
    fi
fi
rm -f /tmp/wtl-out2.$$

# --- Test 3: --force downgrades a live conflict to a warning ---------------
echo ""
echo "Test 3: --force proceeds past a live claim lock and warns"
if ./.loom/scripts/worktree.sh 302 --force >/tmp/wtl-out3.$$ 2>&1; then
    if [[ -f .loom/worktrees/issue-302/.loom-managed ]] && grep -qi "force" /tmp/wtl-out3.$$; then
        pass "--force created the worktree and printed a warning"
    else
        fail "--force exited 0 but sentinel or warning text missing (see /tmp/wtl-out3.$$)"
    fi
else
    fail "--force still refused despite the override (see /tmp/wtl-out3.$$)"
fi
rm -f /tmp/wtl-out3.$$

# --- Test 4: a dead-pid lock fails open -------------------------------------
echo ""
echo "Test 4: a lock owned by a dead pid fails open (worktree still created)"
DEAD_PID="$(a_dead_pid)"
write_issue_lock 303 "$DEAD_PID" "sweep-issue-303-dead"
if ./.loom/scripts/worktree.sh 303 >/tmp/wtl-out4.$$ 2>&1; then
    if [[ -f .loom/worktrees/issue-303/.loom-managed ]]; then
        pass "a dead-pid lock did not block worktree creation"
    else
        fail "worktree.sh exited 0 but .loom-managed sentinel missing (see /tmp/wtl-out4.$$)"
    fi
else
    fail "worktree.sh refused despite the lock's owner pid $DEAD_PID being dead (see /tmp/wtl-out4.$$)"
fi
rm -f /tmp/wtl-out4.$$

# --- Test 5 (#8553 AC2): reverse order -- worktree exists, THEN lock appears
echo ""
echo "Test 5: reverse order -- a worktree already exists, then a live lock appears"
./.loom/scripts/worktree.sh 304 >/dev/null 2>&1
write_issue_lock 304 $$ "sweep-issue-304-live"
if ./.loom/scripts/worktree.sh 304 >/tmp/wtl-out5.$$ 2>&1; then
    fail "a second invocation against an existing worktree ignored a live lock created afterward (see /tmp/wtl-out5.$$)"
else
    pass "the same live-lock refusal applies regardless of which was created first"
fi
rm -f /tmp/wtl-out5.$$

# --- Test 6: --json refusal emits the documented error schema --------------
echo ""
echo "Test 6: --json refusal emits success=false with the live lock's fields"
write_issue_lock 305 $$ "sweep-issue-305-json"
if ./.loom/scripts/worktree.sh --json 305 >/tmp/wtl-out6.$$ 2>/tmp/wtl-err6.$$; then
    fail "worktree.sh --json exited 0 despite a live claim lock"
else
    if grep -q '"success": false' /tmp/wtl-out6.$$ && grep -q "sweep-issue-305-json" /tmp/wtl-out6.$$; then
        pass "--json refusal is a parseable success=false document naming the sweep"
    else
        fail "--json refusal missing expected fields (see /tmp/wtl-out6.$$ /tmp/wtl-err6.$$)"
    fi
fi
rm -f /tmp/wtl-out6.$$ /tmp/wtl-err6.$$

# --- Test 7 (#8702): a matching LOOM_SWEEP_ID is exempt ---------------------
echo ""
echo "Test 7: a live lock whose sweep_id matches caller LOOM_SWEEP_ID proceeds (#8702)"
FOREIGN_PID="$(a_foreign_pid)"
write_issue_lock 306 "$FOREIGN_PID" "sweep-issue-306-self"
if LOOM_SWEEP_ID="sweep-issue-306-self" ./.loom/scripts/worktree.sh 306 >/tmp/wtl-out7.$$ 2>&1; then
    if [[ -f .loom/worktrees/issue-306/.loom-managed ]]; then
        pass "the lock's own sweep (matching LOOM_SWEEP_ID) was not refused by its own claim"
    else
        fail "worktree.sh exited 0 but .loom-managed sentinel missing (see /tmp/wtl-out7.$$)"
    fi
else
    fail "worktree.sh refused its own sweep's live lock (see /tmp/wtl-out7.$$)"
fi
rm -f /tmp/wtl-out7.$$

# --- Test 8 (#8702): a different/unset LOOM_SWEEP_ID still refuses ---------
echo ""
echo "Test 8: a live lock with a DIFFERENT or unset LOOM_SWEEP_ID still refuses"
FOREIGN_PID="$(a_foreign_pid)"
write_issue_lock 307 "$FOREIGN_PID" "sweep-issue-307-live"
if LOOM_SWEEP_ID="sweep-issue-307-someone-else" ./.loom/scripts/worktree.sh 307 >/tmp/wtl-out8a.$$ 2>&1; then
    fail "a DIFFERENT LOOM_SWEEP_ID was wrongly treated as the lock's owner (see /tmp/wtl-out8a.$$)"
else
    pass "a different LOOM_SWEEP_ID does not exempt a foreign live lock"
fi
rm -f /tmp/wtl-out8a.$$
unset LOOM_SWEEP_ID
if ./.loom/scripts/worktree.sh 307 >/tmp/wtl-out8b.$$ 2>&1; then
    fail "an unset LOOM_SWEEP_ID was wrongly treated as the lock's owner (see /tmp/wtl-out8b.$$)"
else
    pass "an unset LOOM_SWEEP_ID does not exempt a foreign live lock (#8553's original case)"
fi
rm -f /tmp/wtl-out8b.$$

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
