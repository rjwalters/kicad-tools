#!/usr/bin/env bash
# test-docs-guide-lock.sh - Regression test for issue #5573
#
# The Guide role's Document Maintenance Step 1 used to be a plain
# check-then-act: query for an open docs PR, and if none, proceed through
# docs-worktree.sh (a single shared worktree slot) to `gh pr create`. Two
# Guide ticks starting within the same short window could both see "no open
# PR" before either had pushed a branch, race each other into
# docs-worktree.sh (the second clobbering the first's in-progress state), and
# both end up creating their own docs PR (observed: #5571 and #5572, opened
# 49 seconds apart with an identical diff shape).
#
# docs-guide-lock.sh closes the gap with a non-blocking mkdir-based lock held
# across the whole Step-1-through-Step-5 window. This suite verifies:
#
#   1. acquire/release round-trip, and `status` reflects held/free correctly.
#   2. serialization — a second concurrent acquire is refused (busy) while
#      the first holds the lock; only one of two truly concurrent,
#      backgrounded acquire attempts wins (the actual race scenario, not
#      just a sequential simulation of it).
#   3. release is idempotent (safe even when the lock was never acquired).
#   4. stale-lock recovery — a lock older than LOOM_DOCS_GUIDE_LOCK_STALE_SECS
#      is reaped by the next acquire attempt, so a crashed tick can never
#      permanently block future ticks.
#   5. argument validation (unknown command, bad stale-secs value).
#   6. guide.md's Document Maintenance phase actually acquires the lock in
#      Step 1 before the open-PR check, and releases it on every exit path
#      out of Step 1 and Step 5 (both the "no changes" and success paths of
#      create_docs_pr()) — and the pre-existing #5454 self-referential-PR
#      exclusion is still intact (this fix must not reintroduce that bug).
#
# Hermetic: throwaway git repo under mktemp -d, no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

LOCK_SH="$SCRIPTS_DIR/docs-guide-lock.sh"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout), falling back
# to the defaults/ source-tree path (a bare source checkout with no
# .claude/commands/loom/ copy yet). See issue #6194 / #6241.
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

if [[ ! -x "$LOCK_SH" ]]; then
    fail "docs-guide-lock.sh missing or not executable at $LOCK_SH"
    echo ""
    echo "================================"
    echo "Tests run:    1"
    echo -e "Tests failed: ${RED}1${NC}"
    exit 1
fi

SANDBOX="$(mktemp -d)"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Sandbox repo: docs-guide-lock.sh resolves its lock dir off `git
# rev-parse --git-common-dir`, so it needs to run inside a real git repo.
# ---------------------------------------------------------------------------
SANDBOX_REPO="$SANDBOX/repo"
mkdir -p "$SANDBOX_REPO"
git init --quiet "$SANDBOX_REPO"
git -C "$SANDBOX_REPO" config user.email "test@loom.local"
git -C "$SANDBOX_REPO" config user.name "Loom Test"

LOCK_DIR_EXPECTED="$SANDBOX_REPO/.loom/locks/docs-guide-lock"

# --- Test 1: acquire / status / release round-trip -------------------------
echo "Test 1: acquire / status / release round-trip"

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" status >/dev/null 2>&1) || RC=$?
if [[ "$RC" != "0" ]]; then pass "status is 'free' before any acquire"; else fail "status reported held before any acquire"; fi

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "first acquire succeeds"; else fail "first acquire failed (rc=$RC)"; fi

if [[ -d "$LOCK_DIR_EXPECTED" ]]; then
    pass "lock directory created at .loom/locks/docs-guide-lock"
else
    fail "lock directory missing at $LOCK_DIR_EXPECTED"
fi

if [[ -f "$LOCK_DIR_EXPECTED/owner.json" ]]; then
    pass "owner.json metadata written"
else
    fail "owner.json missing"
fi

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" status >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "status is 'held' after acquire"; else fail "status did not report held after acquire"; fi

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "release succeeds"; else fail "release failed (rc=$RC)"; fi

if [[ ! -d "$LOCK_DIR_EXPECTED" ]]; then
    pass "lock directory removed after release"
else
    fail "lock directory survived release"
fi

# --- Test 2: idempotent release ---------------------------------------------
echo ""
echo "Test 2: release is idempotent"
RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "release on an already-free lock still exits 0"; else fail "release on a free lock failed (rc=$RC)"; fi

# --- Test 3: serialization (sequential) -------------------------------------
echo ""
echo "Test 3: a second acquire is refused while the first is held"
(cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1)
RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "1" ]]; then pass "second acquire while held returns busy (exit 1)"; else fail "second acquire returned $RC, expected 1 (busy)"; fi
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1)

# --- Test 4: THE REGRESSION — true concurrent race --------------------------
# This is the actual failure mode from #5571/#5572: two ticks starting within
# the same short window. Launch two acquire attempts as truly-concurrent
# backgrounded processes (not a sequential simulation) and verify exactly one
# wins.
echo ""
echo "Test 4: two concurrent acquire attempts — exactly one wins (THE #5573 RACE)"

RESULTS_DIR="$SANDBOX/race-results"
mkdir -p "$RESULTS_DIR"

race_attempt() {
    local id="$1"
    local rc=0
    (cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1) || rc=$?
    echo "$rc" > "$RESULTS_DIR/attempt-$id.rc"
}

race_attempt 1 &
PID1=$!
race_attempt 2 &
PID2=$!
wait "$PID1"
wait "$PID2"

RC1="$(cat "$RESULTS_DIR/attempt-1.rc" 2>/dev/null || echo "MISSING")"
RC2="$(cat "$RESULTS_DIR/attempt-2.rc" 2>/dev/null || echo "MISSING")"

WINS=0
[[ "$RC1" == "0" ]] && WINS=$((WINS + 1))
[[ "$RC2" == "0" ]] && WINS=$((WINS + 1))

if [[ "$WINS" == "1" ]]; then
    pass "exactly one of two truly-concurrent acquire attempts wins (rc1=$RC1, rc2=$RC2)"
else
    fail "expected exactly one winner, got $WINS winners (rc1=$RC1, rc2=$RC2) — the race is NOT closed"
fi

if [[ -d "$LOCK_DIR_EXPECTED" ]]; then
    pass "lock directory left held by the winner (as expected — release is a separate, explicit step)"
else
    fail "lock directory missing after the race — a winner must hold it until released"
fi
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1)

# --- Test 5: stale-lock recovery (crashed tick never permanently blocks) ---
echo ""
echo "Test 5: a lock older than LOOM_DOCS_GUIDE_LOCK_STALE_SECS is reaped"
(cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1)
sleep 2
RC=0
(cd "$SANDBOX_REPO" && LOOM_DOCS_GUIDE_LOCK_STALE_SECS=1 "$LOCK_SH" acquire >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then
    pass "a stale (crashed-tick) lock is reaped and re-acquired automatically"
else
    fail "expected the stale lock to be reaped and re-acquired, got rc=$RC"
fi
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1)

# A lock that is NOT yet stale (default 30-minute threshold, well above this
# test's runtime) must NOT be reaped by a fresh acquire.
(cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1)
RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "1" ]]; then
    pass "a fresh (non-stale) lock is NOT reaped by a contending acquire"
else
    fail "a fresh lock was incorrectly reaped/re-acquired (rc=$RC)"
fi
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1)

# --- Test 6: argument validation --------------------------------------------
echo ""
echo "Test 6: argument validation"

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "missing command -> exit 2"; else fail "expected exit 2 for missing command, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" bogus >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "unknown command -> exit 2"; else fail "expected exit 2 for an unknown command, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" --help >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "--help -> exit 0"; else fail "expected exit 0 for --help, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && LOOM_DOCS_GUIDE_LOCK_STALE_SECS=notanumber "$LOCK_SH" acquire >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "non-numeric LOOM_DOCS_GUIDE_LOCK_STALE_SECS -> exit 2"; else fail "expected exit 2 for a bad stale-secs value, got $RC"; fi
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1)

# --- Test 7: guide.md wiring -------------------------------------------------
echo ""
echo "Test 7: guide.md acquires/releases the lock around Steps 1-5"

assert_grep 'docs-guide-lock\.sh acquire' "$GUIDE_MD" \
    "Step 1 acquires the docs-guide lock"
assert_grep 'docs-guide-lock\.sh release' "$GUIDE_MD" \
    "at least one exit path releases the docs-guide lock"

# Every exit path must release: Step 1's open-PR skip, create_docs_pr()'s
# "no changes" return, and create_docs_pr()'s success path. That's 3 distinct
# release call sites (plus the wiring-check above already covers >=1).
RELEASE_COUNT="$(grep -c 'docs-guide-lock\.sh release' "$GUIDE_MD")"
if [[ "$RELEASE_COUNT" -ge 3 ]]; then
    pass "the lock is released on every known exit path ($RELEASE_COUNT release call sites)"
else
    fail "expected >=3 release call sites (Step 1 skip, create_docs_pr no-op, create_docs_pr success), found $RELEASE_COUNT"
fi

# The acquire must run BEFORE the open-PR check, not after -- an acquire
# positioned after the check would just reintroduce the original TOCTOU gap.
ACQUIRE_LINE="$(grep -n 'docs-guide-lock\.sh acquire' "$GUIDE_MD" | head -1 | cut -d: -f1)"
OPEN_PR_LINE="$(grep -n 'OPEN_DOCS_PR=' "$GUIDE_MD" | head -1 | cut -d: -f1)"
if [[ -n "$ACQUIRE_LINE" && -n "$OPEN_PR_LINE" && "$ACQUIRE_LINE" -lt "$OPEN_PR_LINE" ]]; then
    pass "the lock is acquired BEFORE the open-docs-PR check (closes the TOCTOU gap)"
else
    fail "lock acquire (line ${ACQUIRE_LINE:-?}) must precede the open-PR check (line ${OPEN_PR_LINE:-?})"
fi

# --- Test 8: #5454 self-referential-PR exclusion is still intact -----------
echo ""
echo "Test 8: the #5454 fix (own-docs-PR exclusion) was not disturbed"
assert_grep 'GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD" \
    "GUIDE_DOCS_PR_EXCLUDE is still defined"
assert_grep 'startswith\("docs/guide-update"\)' "$GUIDE_MD" \
    "GUIDE_DOCS_PR_EXCLUDE still excludes by head-branch prefix"

# --- Test 9: #5615 cross-host guard -----------------------------------------
# docs-guide-lock.sh only ever serializes ticks on the SAME host (it is a
# local `mkdir` lock). This suite can't spin up two real hosts, so it verifies
# the cross-host mitigation the same way Test 7 verifies the same-host lock
# wiring: statically, against the actual guide.md prompt text that an agent
# executes -- an uncached recheck of the open-docs-PR search positioned
# between the local commit and the push/create, mirroring Judge/Champion's
# Verdict-Time CAS Recheck for the analogous PR-label race.
echo ""
echo "Test 9: create_docs_pr() rechecks for an open docs PR (uncached) before push/create"

assert_grep 'OPEN_DOCS_PR_RECHECK=' "$GUIDE_MD" \
    "a second, distinctly-named open-docs-PR check exists (the cross-host recheck)"

# Must NOT go through $GH_READ (which may resolve to gh-cached, whose default
# 30s read TTL is scoped per host and would happily return a stale "no PR"
# answer for a PR a DIFFERENT host just opened) -- plain `gh` only.
RECHECK_LINE_TEXT="$(grep -n 'OPEN_DOCS_PR_RECHECK=' "$GUIDE_MD" | head -1)"
if [[ "$RECHECK_LINE_TEXT" == *'$GH_READ'* ]]; then
    fail "the cross-host recheck must use plain 'gh', not \$GH_READ/gh-cached (found \$GH_READ on: $RECHECK_LINE_TEXT)"
else
    pass "the cross-host recheck bypasses \$GH_READ/gh-cached (uses plain 'gh' for a fresh read)"
fi

# Ordering: recheck must run AFTER the local commit (so nothing is pushed
# before it) but BEFORE both the push and the `gh pr create` call -- a recheck
# positioned after either would defeat its own purpose.
COMMIT_LINE="$(grep -n 'git -C "\$DOCS_WT" commit -m "docs: update WORK_LOG' "$GUIDE_MD" | head -1 | cut -d: -f1)"
RECHECK_LINE="$(grep -n 'OPEN_DOCS_PR_RECHECK=' "$GUIDE_MD" | head -1 | cut -d: -f1)"
PUSH_LINE="$(grep -n 'git -C "\$DOCS_WT" push -u origin' "$GUIDE_MD" | head -1 | cut -d: -f1)"
# Target the actual `gh pr create` invocation site inside create_docs_pr()
# (the `(cd "$DOCS_WT" && gh pr create \` line), not the many prose mentions
# of "gh pr create" earlier in the file's Step 1 commentary.
CREATE_LINE="$(grep -n '&& gh pr create' "$GUIDE_MD" | head -1 | cut -d: -f1)"

if [[ -n "$COMMIT_LINE" && -n "$RECHECK_LINE" && "$COMMIT_LINE" -lt "$RECHECK_LINE" ]]; then
    pass "the recheck runs AFTER the local commit (line $COMMIT_LINE < $RECHECK_LINE)"
else
    fail "recheck (line ${RECHECK_LINE:-?}) must run after the local commit (line ${COMMIT_LINE:-?})"
fi

if [[ -n "$RECHECK_LINE" && -n "$PUSH_LINE" && "$RECHECK_LINE" -lt "$PUSH_LINE" ]]; then
    pass "the recheck runs BEFORE the push (line $RECHECK_LINE < $PUSH_LINE)"
else
    fail "recheck (line ${RECHECK_LINE:-?}) must run before the push (line ${PUSH_LINE:-?})"
fi

if [[ -n "$RECHECK_LINE" && -n "$CREATE_LINE" && "$RECHECK_LINE" -lt "$CREATE_LINE" ]]; then
    pass "the recheck runs BEFORE gh pr create (line $RECHECK_LINE < $CREATE_LINE)"
else
    fail "recheck (line ${RECHECK_LINE:-?}) must run before gh pr create (line ${CREATE_LINE:-?})"
fi

# When the recheck finds a PR, this tick must bail out (release the lock and
# return) without pushing -- i.e. there is a release+return between the
# recheck and the push, not just at the very end of the function.
RECHECK_BLOCK="$(sed -n "${RECHECK_LINE},${PUSH_LINE}p" "$GUIDE_MD" 2>/dev/null || true)"
if [[ -n "$RECHECK_BLOCK" ]] && grep -q 'docs-guide-lock\.sh release' <<<"$RECHECK_BLOCK" && grep -q 'return' <<<"$RECHECK_BLOCK"; then
    pass "a PR found by the recheck releases the lock and returns before the push"
else
    fail "expected a release+return between the recheck (line ${RECHECK_LINE:-?}) and the push (line ${PUSH_LINE:-?})"
fi

# --- Test 10: #5615 docs-guide-lock.sh header documents same-host-only scope
echo ""
echo "Test 10: docs-guide-lock.sh's header states it only guarantees same-host exclusion"
assert_grep '[Ss][Aa][Mm][Ee]-[Hh][Oo][Ss][Tt]' "$LOCK_SH" \
    "docs-guide-lock.sh's header mentions same-host scope"
assert_grep 'ZERO mutual exclusion across the fleet' "$LOCK_SH" \
    "docs-guide-lock.sh's header states it gives no cross-fleet protection"

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
