#!/usr/bin/env bash
# test-worktree-stash-baseline.sh - Tests for the `worktree.sh stash-push
# <N>` / `worktree.sh stash-pop <N>` verb pair (#5217)
#
# Verifies the worktree-scoped, per-issue clean-baseline stash:
#   1. stash-push captures tracked-file changes and resets the worktree to a
#      clean baseline WITHOUT touching `git stash` (stash list identical
#      before/after — unlike raw `git stash push`, which IS gated by
#      guard-destructive-generic.sh's stash-scope:worktree-collision ask).
#   2. stash-pop restores exactly what stash-push captured (tracked diff).
#   3. --include-untracked moves untracked files out on push and restores
#      them on pop; the default omits them.
#   4. A clean worktree still succeeds (no-op push, nothing to reset).
#   5. A second stash-push before pop refuses (no data loss / no silent
#      overwrite of a pending baseline).
#   6. stash-pop with nothing pending fails cleanly, without side effects.
#   7. stash-push / stash-pop on a non-existent worktree fail cleanly.
#   8. --json emits a single parseable document for both verbs.
#   9. Loom runtime markers are never swept into the untracked holding dir.
#  10. The per-issue ref (refs/loom/stash-baseline/issue-<N>) is created by
#      push and cleared by pop — never left dangling on a clean round trip.
#  11. Another worktree's `git stash push` landing BETWEEN a stash-push and
#      its stash-pop cannot corrupt the restore (the #5217 race), and does
#      not consume that worktree's shared-stack entry either.
#  12. Contrast fixture: the raw `git stash push`/`pop` pattern DOES lose to
#      the identical interleaving — which is why the guard's ask on raw
#      stash pop/drop/clear must stay exactly as strict as before.
#  13. The full `stash-push && <check> && stash-pop` chain exits 0 even when
#      the worktree was already clean (a pop with nothing captured is a
#      no-op, not an error, so the chain cannot break mid-sweep).
#
# Follows the throwaway-repo harness pattern in test-worktree-snapshot.sh: a
# bare origin remote + a working repo, with worktree.sh + its lib/ helpers
# copied into a temp tree, then the script driven directly.

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

# --- Throwaway repo setup ---------------------------------------------------
TMP=$(mktemp -d /tmp/loom-stash-baseline-test.XXXXXX)
trap 'rm -rf "$TMP"; cd "$REPO_ROOT" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
echo "tracked file" > tracked.txt
git add tracked.txt
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main

mkdir -p .loom/scripts/lib
cp "$WORKTREE_SH" .loom/scripts/worktree.sh
if [[ -d "$SCRIPTS_DIR/lib" ]]; then
    cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
fi
chmod +x .loom/scripts/worktree.sh

REPO="$TMP/repo"

# Helper: create a fresh managed worktree for an issue number.
make_worktree() {
    local n="$1"
    cd "$REPO"
    ./.loom/scripts/worktree.sh "$n" >/dev/null 2>&1
}

# --- Test 1: stash-push resets to a clean baseline without touching git stash
echo "Test 1: stash-push captures tracked changes, resets to clean baseline, never touches git stash"
make_worktree 301
cd "$REPO"
echo "modified content" >> ".loom/worktrees/issue-301/tracked.txt"
stash_before="$(git stash list)"
if ./.loom/scripts/worktree.sh stash-push 301 >/tmp/sbp-out1.$$ 2>&1; then
    pass "stash-push exited 0 for a dirty worktree"
else
    fail "stash-push exited non-zero (see /tmp/sbp-out1.$$)"
fi
stash_after="$(git stash list)"
if [[ "$stash_before" == "$stash_after" ]]; then
    pass "git stash list is unchanged by stash-push"
else
    fail "git stash list changed — stash-push touched the shared stash"
fi
if [[ "$(cat ".loom/worktrees/issue-301/tracked.txt")" == "tracked file" ]]; then
    pass "worktree reset to the clean HEAD baseline after stash-push"
else
    fail "worktree was NOT reset to a clean baseline after stash-push"
fi
if git -C ".loom/worktrees/issue-301" rev-parse --verify --quiet refs/loom/stash-baseline/issue-301 >/dev/null 2>&1; then
    pass "per-issue baseline ref was created (refs/loom/stash-baseline/issue-301)"
else
    fail "no per-issue baseline ref was created"
fi

# --- Test 2: stash-pop restores exactly what stash-push captured -----------
echo ""
echo "Test 2: stash-pop restores the captured tracked diff and clears the ref"
if ./.loom/scripts/worktree.sh stash-pop 301 >/tmp/sbp-out2.$$ 2>&1; then
    pass "stash-pop exited 0"
else
    fail "stash-pop exited non-zero (see /tmp/sbp-out2.$$)"
fi
if grep -q "modified content" ".loom/worktrees/issue-301/tracked.txt"; then
    pass "stash-pop restored the captured tracked-file diff"
else
    fail "stash-pop did NOT restore the captured diff"
fi
if ! git -C ".loom/worktrees/issue-301" rev-parse --verify --quiet refs/loom/stash-baseline/issue-301 >/dev/null 2>&1; then
    pass "per-issue baseline ref was cleared by stash-pop"
else
    fail "per-issue baseline ref was left dangling after stash-pop"
fi

# --- Test 3: --include-untracked round-trips untracked files ---------------
echo ""
echo "Test 3: --include-untracked moves untracked files out on push, restores them on pop"
make_worktree 302
cd "$REPO"
echo "brand new file" > ".loom/worktrees/issue-302/untracked.txt"
if ./.loom/scripts/worktree.sh stash-push 302 --include-untracked >/tmp/sbp-out3a.$$ 2>&1; then
    pass "stash-push --include-untracked exited 0"
else
    fail "stash-push --include-untracked exited non-zero (see /tmp/sbp-out3a.$$)"
fi
if [[ ! -e ".loom/worktrees/issue-302/untracked.txt" ]]; then
    pass "untracked file was moved out of the worktree by stash-push --include-untracked"
else
    fail "untracked file was NOT moved out of the worktree"
fi
if ./.loom/scripts/worktree.sh stash-pop 302 >/tmp/sbp-out3b.$$ 2>&1; then
    pass "stash-pop exited 0"
else
    fail "stash-pop exited non-zero (see /tmp/sbp-out3b.$$)"
fi
if [[ -f ".loom/worktrees/issue-302/untracked.txt" ]] && grep -q "brand new file" ".loom/worktrees/issue-302/untracked.txt"; then
    pass "stash-pop restored the untracked file with its original content"
else
    fail "stash-pop did NOT restore the untracked file"
fi
if git -C ".loom/worktrees/issue-302" status --porcelain -- untracked.txt | grep -q '^??'; then
    pass "restored file remains untracked (not staged) after stash-pop"
else
    fail "restored file was left staged/tracked after stash-pop"
fi

echo ""
echo "Test 3b: without --include-untracked, an untracked file is left alone by stash-push"
make_worktree 303
cd "$REPO"
echo "should stay put" > ".loom/worktrees/issue-303/plain-untracked.txt"
./.loom/scripts/worktree.sh stash-push 303 >/tmp/sbp-out3c.$$ 2>&1 || true
if [[ -f ".loom/worktrees/issue-303/plain-untracked.txt" ]]; then
    pass "default stash-push (no --include-untracked) leaves untracked files in place"
else
    fail "default stash-push unexpectedly moved an untracked file"
fi
./.loom/scripts/worktree.sh stash-pop 303 >/tmp/sbp-out3d.$$ 2>&1 || true

# --- Test 4: a clean worktree still succeeds (no-op) ------------------------
echo ""
echo "Test 4: a clean worktree still succeeds as a no-op"
make_worktree 304
cd "$REPO"
if ./.loom/scripts/worktree.sh stash-push 304 >/tmp/sbp-out4.$$ 2>&1; then
    pass "stash-push exited 0 for a clean worktree"
else
    fail "stash-push exited non-zero for a clean worktree (see /tmp/sbp-out4.$$)"
fi
if ! git -C ".loom/worktrees/issue-304" rev-parse --verify --quiet refs/loom/stash-baseline/issue-304 >/dev/null 2>&1; then
    pass "no baseline ref created for a worktree with nothing to push"
else
    fail "a baseline ref was created despite no changes to push"
fi

# --- Test 5: a second stash-push before pop refuses -------------------------
echo ""
echo "Test 5: stash-push refuses when a pending push already exists for the issue"
make_worktree 305
cd "$REPO"
echo "first change" >> ".loom/worktrees/issue-305/tracked.txt"
./.loom/scripts/worktree.sh stash-push 305 >/tmp/sbp-out5a.$$ 2>&1
echo "second change" >> ".loom/worktrees/issue-305/tracked.txt"
if ./.loom/scripts/worktree.sh stash-push 305 >/tmp/sbp-out5b.$$ 2>&1; then
    fail "stash-push succeeded despite a pending push already existing (data-loss risk)"
else
    pass "stash-push refused a second push while one is already pending"
fi
if grep -q "second change" ".loom/worktrees/issue-305/tracked.txt"; then
    pass "the second (uncaptured) change was left in place, not silently discarded"
else
    fail "the second change was lost by the refused stash-push"
fi
# Clean up: restore file to a poppable state before continuing.
git -C ".loom/worktrees/issue-305" checkout -- tracked.txt
./.loom/scripts/worktree.sh stash-pop 305 >/tmp/sbp-out5c.$$ 2>&1 || true

# --- Test 6: stash-pop with nothing pending fails cleanly -------------------
echo ""
echo "Test 6: stash-pop refuses cleanly when nothing is pending"
make_worktree 306
cd "$REPO"
if ./.loom/scripts/worktree.sh stash-pop 306 >/tmp/sbp-out6.$$ 2>&1; then
    fail "stash-pop succeeded despite nothing being pending"
else
    pass "stash-pop exited non-zero when nothing is pending"
fi

# --- Test 7: stash-push / stash-pop on a non-existent worktree fail cleanly -
echo ""
echo "Test 7: stash-push and stash-pop refuse a non-existent issue worktree"
cd "$REPO"
if ./.loom/scripts/worktree.sh stash-push 999999 >/tmp/sbp-out7a.$$ 2>&1; then
    fail "stash-push succeeded for a non-existent worktree (should have failed)"
else
    pass "stash-push exited non-zero for a non-existent worktree"
fi
if ./.loom/scripts/worktree.sh stash-pop 999999 >/tmp/sbp-out7b.$$ 2>&1; then
    fail "stash-pop succeeded for a non-existent worktree (should have failed)"
else
    pass "stash-pop exited non-zero for a non-existent worktree"
fi

# --- Test 8: --json emits a single parseable document -----------------------
echo ""
echo "Test 8: stash-push/stash-pop --json emit parseable JSON documents"
make_worktree 307
cd "$REPO"
echo "json test" >> ".loom/worktrees/issue-307/tracked.txt"
if ./.loom/scripts/worktree.sh stash-push 307 --json >/tmp/sbp-out8a.$$ 2>/tmp/sbp-err8a.$$; then
    if grep -q '"success": true' /tmp/sbp-out8a.$$ && grep -q '"hasTrackedChanges": true' /tmp/sbp-out8a.$$ && grep -q '"ref"' /tmp/sbp-out8a.$$; then
        pass "stash-push --json emits success=true, hasTrackedChanges=true, ref"
    else
        fail "stash-push --json output missing expected fields (see /tmp/sbp-out8a.$$)"
    fi
else
    fail "stash-push --json exited non-zero (see /tmp/sbp-out8a.$$ /tmp/sbp-err8a.$$)"
fi
if ./.loom/scripts/worktree.sh stash-pop 307 --json >/tmp/sbp-out8b.$$ 2>/tmp/sbp-err8b.$$; then
    if grep -q '"success": true' /tmp/sbp-out8b.$$ && grep -q '"restoredTracked": true' /tmp/sbp-out8b.$$; then
        pass "stash-pop --json emits success=true, restoredTracked=true"
    else
        fail "stash-pop --json output missing expected fields (see /tmp/sbp-out8b.$$)"
    fi
else
    fail "stash-pop --json exited non-zero (see /tmp/sbp-out8b.$$ /tmp/sbp-err8b.$$)"
fi

# --- Test 9: Loom runtime markers are never swept into the holding dir -----
echo ""
echo "Test 9: Loom runtime markers are never moved by --include-untracked"
make_worktree 308
cd "$REPO"
touch ".loom/worktrees/issue-308/.loom-in-use" ".loom/worktrees/issue-308/.loom-checkpoint"
echo "real wip" > ".loom/worktrees/issue-308/real-wip.txt"
if ./.loom/scripts/worktree.sh stash-push 308 --include-untracked >/tmp/sbp-out9.$$ 2>&1; then
    if [[ -f ".loom/worktrees/issue-308/.loom-in-use" ]] && [[ -f ".loom/worktrees/issue-308/.loom-checkpoint" ]] && [[ ! -e ".loom/worktrees/issue-308/real-wip.txt" ]]; then
        pass "runtime markers stayed in place; real WIP file was moved out"
    else
        fail "runtime markers were swept, or real WIP was left behind"
    fi
else
    fail "stash-push --include-untracked exited non-zero (see /tmp/sbp-out9.$$)"
fi
./.loom/scripts/worktree.sh stash-pop 308 >/tmp/sbp-out9b.$$ 2>&1 || true

# --- Test 10: the cross-worktree race the shared stash stack exposes --------
#
# This is the acceptance-criteria case from #5217: "simulate another
# worktree's stash entry landing mid-sequence". A same-chain
# `git stash push && <cmd> && git stash pop` heuristic in the GUARD cannot
# make that pattern safe, because push and pop are two separate calls with an
# arbitrary-duration command in between — another worktree's `git stash push`
# can land on the SHARED stack during that window and be popped by mistake.
# stash-push/stash-pop are immune by construction: each issue's WIP is
# anchored to its OWN ref, so there is no shared stack to interleave on.
echo ""
echo "Test 10: another worktree's git stash landing between stash-push and stash-pop cannot corrupt the restore"
make_worktree 310
make_worktree 311
cd "$REPO"
echo "wip-from-310" >> ".loom/worktrees/issue-310/tracked.txt"
./.loom/scripts/worktree.sh stash-push 310 >/tmp/sbp-out10a.$$ 2>&1

# Mid-sequence: a concurrent builder in ANOTHER worktree pushes onto the
# shared refs/stash stack — exactly the interleaving that makes a raw
# push/pop pair unsafe.
echo "wip-from-311" >> ".loom/worktrees/issue-311/tracked.txt"
git -C ".loom/worktrees/issue-311" stash push -q -m "concurrent builder 311" >/dev/null 2>&1
shared_stack_depth_before=$(git stash list | wc -l | tr -d ' ')

if ./.loom/scripts/worktree.sh stash-pop 310 >/tmp/sbp-out10b.$$ 2>&1; then
    pass "stash-pop 310 exited 0 despite another worktree's stash entry landing mid-sequence"
else
    fail "stash-pop 310 failed after a concurrent cross-worktree stash push (see /tmp/sbp-out10b.$$)"
fi
if grep -q "wip-from-310" ".loom/worktrees/issue-310/tracked.txt" && \
   ! grep -q "wip-from-311" ".loom/worktrees/issue-310/tracked.txt"; then
    pass "stash-pop restored issue 310's OWN wip, not the concurrent worktree's"
else
    fail "stash-pop restored the WRONG worktree's wip (cross-worktree collision not prevented)"
fi
shared_stack_depth_after=$(git stash list | wc -l | tr -d ' ')
if [[ "$shared_stack_depth_before" == "$shared_stack_depth_after" ]]; then
    pass "the other worktree's shared-stack entry was left untouched by stash-pop"
else
    fail "stash-pop consumed an entry from the shared refs/stash stack"
fi
git -C ".loom/worktrees/issue-311" stash pop -q >/dev/null 2>&1 || true

# --- Test 11: contrast — the raw pattern DOES lose to the same interleaving -
#
# Documents why the guard must keep asking on raw `git stash pop` from a
# worktree, and why the rejected same-chain-heuristic fix would not have been
# safe: with the identical interleaving, raw push/pop restores the OTHER
# worktree's WIP.
echo ""
echo "Test 11: contrast — raw git stash push/pop loses to the same interleaving (why the guard still asks)"
make_worktree 312
make_worktree 313
cd "$REPO"
echo "raw-wip-312" >> ".loom/worktrees/issue-312/tracked.txt"
git -C ".loom/worktrees/issue-312" stash push -q -m "builder 312" >/dev/null 2>&1
echo "raw-wip-313" >> ".loom/worktrees/issue-313/tracked.txt"
git -C ".loom/worktrees/issue-313" stash push -q -m "builder 313" >/dev/null 2>&1
git -C ".loom/worktrees/issue-312" stash pop -q >/dev/null 2>&1 || true
if grep -q "raw-wip-313" ".loom/worktrees/issue-312/tracked.txt"; then
    pass "raw git stash pop in worktree 312 restored worktree 313's WIP (the #4821 collision, still ask-gated)"
else
    fail "raw stash interleaving did not reproduce as expected — revisit this fixture"
fi
git -C ".loom/worktrees/issue-312" checkout -q -- tracked.txt 2>/dev/null || true
git -C ".loom/worktrees/issue-313" checkout -q -- tracked.txt 2>/dev/null || true
git stash clear >/dev/null 2>&1 || true

# --- Test 12: the full headless chain survives an already-clean worktree ----
#
# The intended headless usage is a single `&&` chain. If stash-pop errored
# whenever stash-push found nothing to capture, that chain would break
# mid-sweep on any clean worktree — reintroducing the very stall #5217 set
# out to remove.
echo ""
echo "Test 12: stash-push && <baseline check> && stash-pop exits 0 on an already-clean worktree"
make_worktree 314
cd "$REPO"
if ./.loom/scripts/worktree.sh stash-push 314 >/tmp/sbp-out12.$$ 2>&1 \
    && cat ".loom/worktrees/issue-314/tracked.txt" >/dev/null \
    && ./.loom/scripts/worktree.sh stash-pop 314 >>/tmp/sbp-out12.$$ 2>&1; then
    pass "the full push/check/pop chain exits 0 for a clean worktree"
else
    fail "the push/check/pop chain broke on a clean worktree (see /tmp/sbp-out12.$$)"
fi
if [[ ! -e "$REPO/.loom/worktrees/.stash-baseline/issue-314" ]]; then
    pass "no pending state left behind after a clean-worktree round trip"
else
    fail "stash-pop left pending state behind at .stash-baseline/issue-314"
fi

# ===========================================================================
# The `main` target (#6076)
# ===========================================================================
#
# Roles that legitimately run in the primary clone (Judge, Champion, Auditor,
# Guide, Hermit) had no sanctioned clean-and-restore pair — the verbs were
# issue-keyed only — so they reached for raw `git stash` + `git stash pop`
# there, and the pop half is an unanswerable `stash-scope:main-checkout` ask
# in a headless run. `stash-push main` / `stash-pop main` close that gap with
# the same never-touch-refs/stash guarantees as the issue-keyed path.

# --- Test 13: stash-push/pop main round-trips the PRIMARY clone -------------
echo ""
echo "Test 13: stash-push main captures the primary clone and never touches refs/stash"
cd "$REPO"
git stash clear >/dev/null 2>&1 || true
echo "main-checkout wip" >> "$REPO/tracked.txt"
stash_before="$(git stash list)"
if ./.loom/scripts/worktree.sh stash-push main >/tmp/sbp-main1.$$ 2>&1; then
    pass "stash-push main exited 0 for a dirty primary clone"
else
    fail "stash-push main exited non-zero (see /tmp/sbp-main1.$$)"
fi
if [[ "$stash_before" == "$(git stash list)" ]]; then
    pass "git stash list is unchanged by stash-push main"
else
    fail "stash-push main touched the shared refs/stash stack"
fi
if [[ "$(cat "$REPO/tracked.txt")" == "tracked file" ]]; then
    pass "primary clone reset to the clean HEAD baseline"
else
    fail "primary clone was NOT reset to a clean baseline"
fi
if git rev-parse --verify --quiet refs/loom/stash-baseline/main >/dev/null 2>&1; then
    pass "main baseline ref was created (refs/loom/stash-baseline/main)"
else
    fail "no main baseline ref was created"
fi
if ./.loom/scripts/worktree.sh stash-pop main >/tmp/sbp-main2.$$ 2>&1; then
    pass "stash-pop main exited 0"
else
    fail "stash-pop main exited non-zero (see /tmp/sbp-main2.$$)"
fi
if grep -q "main-checkout wip" "$REPO/tracked.txt"; then
    pass "stash-pop main restored the captured tracked-file diff"
else
    fail "stash-pop main did NOT restore the captured diff"
fi
if ! git rev-parse --verify --quiet refs/loom/stash-baseline/main >/dev/null 2>&1; then
    pass "main baseline ref was cleared by stash-pop main"
else
    fail "main baseline ref was left dangling after stash-pop main"
fi
git checkout -q -- tracked.txt 2>/dev/null || true

# --- Test 14: a pending `main` capture refuses a second push ----------------
echo ""
echo "Test 14: stash-push main refuses a second push while one is pending"
cd "$REPO"
echo "first main change" >> "$REPO/tracked.txt"
./.loom/scripts/worktree.sh stash-push main >/tmp/sbp-main3a.$$ 2>&1
echo "second main change" >> "$REPO/tracked.txt"
if ./.loom/scripts/worktree.sh stash-push main >/tmp/sbp-main3b.$$ 2>&1; then
    fail "a second stash-push main succeeded — the first capture could be clobbered"
else
    pass "a second stash-push main is refused while a capture is pending"
fi
if grep -q "stash-pop main" /tmp/sbp-main3b.$$; then
    pass "the refusal names the restore command ('stash-pop main')"
else
    fail "the refusal did not name 'stash-pop main'. Got: $(cat /tmp/sbp-main3b.$$)"
fi
if grep -q "second main change" "$REPO/tracked.txt"; then
    pass "the refused push left the uncaptured change in place, not silently discarded"
else
    fail "the refused push discarded the uncaptured working-tree change"
fi
# Drop the uncaptured edit first: restoring ON TOP of it would conflict, and a
# conflicted restore deliberately PRESERVES the pending state (nothing lost).
git checkout -q -- tracked.txt 2>/dev/null || true
./.loom/scripts/worktree.sh stash-pop main >/tmp/sbp-main3c.$$ 2>&1 || true
git checkout -q -- tracked.txt 2>/dev/null || true
git update-ref -d refs/loom/stash-baseline/main 2>/dev/null || true
rm -rf "$REPO/.loom/worktrees/.stash-baseline/main" 2>/dev/null || true

# --- Test 15: `main` and an issue target are independent --------------------
echo ""
echo "Test 15: a pending main capture does not block (or leak into) an issue capture"
make_worktree 320
cd "$REPO"
echo "main wip" >> "$REPO/tracked.txt"
echo "issue wip" >> "$REPO/.loom/worktrees/issue-320/tracked.txt"
./.loom/scripts/worktree.sh stash-push main >/tmp/sbp-main4a.$$ 2>&1
if ./.loom/scripts/worktree.sh stash-push 320 >/tmp/sbp-main4b.$$ 2>&1; then
    pass "an issue stash-push succeeds while a main capture is pending"
else
    fail "an issue stash-push was blocked by a pending main capture (see /tmp/sbp-main4b.$$)"
fi
./.loom/scripts/worktree.sh stash-pop 320 >/tmp/sbp-main4c.$$ 2>&1 || true
./.loom/scripts/worktree.sh stash-pop main >/tmp/sbp-main4d.$$ 2>&1 || true
if grep -q "main wip" "$REPO/tracked.txt" && grep -q "issue wip" "$REPO/.loom/worktrees/issue-320/tracked.txt"; then
    pass "each target restored its OWN capture (no cross-target leakage)"
else
    fail "captures leaked between the main and issue targets"
fi
git checkout -q -- tracked.txt 2>/dev/null || true
git -C ".loom/worktrees/issue-320" checkout -q -- tracked.txt 2>/dev/null || true

# --- Test 16: main target works when invoked FROM a worktree ----------------
#
# The roles this exists for run in the primary clone, but a sweep can invoke
# the verb from anywhere; the target must be resolved from the git common dir,
# never from cwd.
echo ""
echo "Test 16: stash-push/pop main resolve the primary clone even when run from a worktree"
make_worktree 321
echo "resolved-from-worktree" >> "$REPO/tracked.txt"
if (cd "$REPO/.loom/worktrees/issue-321" && "$REPO/.loom/scripts/worktree.sh" stash-push main >/tmp/sbp-main5a.$$ 2>&1); then
    pass "stash-push main exited 0 when invoked from a worktree cwd"
else
    fail "stash-push main failed when invoked from a worktree cwd (see /tmp/sbp-main5a.$$)"
fi
if [[ "$(cat "$REPO/tracked.txt")" == "tracked file" ]]; then
    pass "the PRIMARY clone (not the worktree) was reset"
else
    fail "stash-push main did not reset the primary clone"
fi
(cd "$REPO/.loom/worktrees/issue-321" && "$REPO/.loom/scripts/worktree.sh" stash-pop main >/tmp/sbp-main5b.$$ 2>&1) || true
cd "$REPO"
git checkout -q -- tracked.txt 2>/dev/null || true

# --- Test 17: clean primary clone is a no-op chain; bad targets rejected ----
echo ""
echo "Test 17: the full main chain is a no-op on a clean primary clone, and bad targets are rejected"
cd "$REPO"
if ./.loom/scripts/worktree.sh stash-push main >/tmp/sbp-main6.$$ 2>&1 \
    && cat "$REPO/tracked.txt" >/dev/null \
    && ./.loom/scripts/worktree.sh stash-pop main >>/tmp/sbp-main6.$$ 2>&1; then
    pass "push/check/pop chain exits 0 for a clean primary clone"
else
    fail "the main push/check/pop chain broke on a clean primary clone (see /tmp/sbp-main6.$$)"
fi
if [[ ! -e "$REPO/.loom/worktrees/.stash-baseline/main" ]]; then
    pass "no pending state left behind after a clean main round trip"
else
    fail "stash-pop main left pending state behind at .stash-baseline/main"
fi
if ./.loom/scripts/worktree.sh stash-push not-a-target >/tmp/sbp-main7.$$ 2>&1; then
    fail "stash-push accepted a non-numeric, non-'main' target"
else
    pass "stash-push rejects a target that is neither an issue number nor 'main'"
fi
if ./.loom/scripts/worktree.sh stash-pop MAIN >/tmp/sbp-main8.$$ 2>&1; then
    fail "stash-pop accepted 'MAIN' — the target must be exact, not case-folded"
else
    pass "stash-pop rejects 'MAIN' (exact 'main' only)"
fi

# --- Test 18: --json for the main target ------------------------------------
echo ""
echo "Test 18: --json reports target=main with a null issueNumber"
cd "$REPO"
echo "json main wip" >> "$REPO/tracked.txt"
json_out="$(./.loom/scripts/worktree.sh stash-push main --json 2>/dev/null)"
if [[ "$(printf '%s\n' "$json_out" | grep -c .)" -eq 1 ]]; then
    pass "stash-push main --json emits exactly one line on stdout"
else
    fail "stash-push main --json stdout was not a single line: $json_out"
fi
if printf '%s' "$json_out" | grep -q '"target": "main"' && printf '%s' "$json_out" | grep -q '"issueNumber": null'; then
    pass "stash-push main --json reports target=main and issueNumber=null"
else
    fail "stash-push main --json payload wrong: $json_out"
fi
if printf '%s' "$json_out" | grep -q 'refs/loom/stash-baseline/main'; then
    pass "stash-push main --json names the main baseline ref"
else
    fail "stash-push main --json did not name the main baseline ref: $json_out"
fi
json_out="$(./.loom/scripts/worktree.sh stash-pop main --json 2>/dev/null)"
if printf '%s' "$json_out" | grep -q '"target": "main"' && printf '%s' "$json_out" | grep -q '"restoredTracked": true'; then
    pass "stash-pop main --json reports the restore"
else
    fail "stash-pop main --json payload wrong: $json_out"
fi
git checkout -q -- tracked.txt 2>/dev/null || true

# --- Test 19: the issue-target JSON keeps its historical fields -------------
echo ""
echo "Test 19: issue-target --json still reports a numeric issueNumber"
make_worktree 322
cd "$REPO"
echo "json issue wip" >> "$REPO/.loom/worktrees/issue-322/tracked.txt"
json_out="$(./.loom/scripts/worktree.sh stash-push 322 --json 2>/dev/null)"
if printf '%s' "$json_out" | grep -q '"issueNumber": 322'; then
    pass "issue-target --json still emits an unquoted numeric issueNumber"
else
    fail "issue-target --json regressed: $json_out"
fi
./.loom/scripts/worktree.sh stash-pop 322 >/dev/null 2>&1 || true

# --- Summary ----------------------------------------------------------------
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
