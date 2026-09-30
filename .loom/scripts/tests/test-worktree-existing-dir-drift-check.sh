#!/usr/bin/env bash
# test-worktree-existing-dir-drift-check.sh — Tests for the drift check on the
# "worktree directory already exists, registered with git" fast path (#6257)
#
# Regression coverage for the incident on #5609: a Judge session reused an
# existing builder worktree (`.loom/worktrees/issue-5609`) that was one commit
# behind the PR's actual pushed tip, had ~230 lines of uncommitted stale WIP
# sitting in the working tree, and whose local branch's upstream tracking ref
# was wrongly set to `origin/main` instead of `origin/feature/issue-5609`.
#
# Root cause: worktree.sh's "worktree directory already exists" fast path
# (`if git worktree list | grep -q "$WORKTREE_PATH"`) only ever compared the
# worktree's HEAD to BASE_REF (the default branch) to decide whether to
# "preserve existing work" or reset a stale worktree — it never fetched or
# compared against the branch's OWN upstream (origin/$BRANCH_NAME), and never
# touched upstream tracking at all. This is a completely different code path
# from the "local branch exists, no worktree dir yet" reuse path (#6095/#6100,
# covered by test-worktree-local-branch-upstream-tracking.sh) — that fix never
# ran here, so a worktree left with stale HEAD and/or wrong upstream tracking
# was silently "preserved" and handed straight to a Judge/Doctor session with
# no signal that it no longer matched the branch's actual pushed tip.
#
# Coverage:
#   1. Worktree one commit behind the pushed branch tip, wrong upstream
#      (origin/main), AND uncommitted changes (the exact incident shape):
#      worktree.sh warns about the drift, corrects the upstream, and does NOT
#      destroy the uncommitted work (still preserved for the caller).
#   2. Worktree with a local commit ahead of the pushed tip (unpushed work)
#      and no uncommitted changes: no false-positive "may be stale" warning.
#   3. Worktree already correctly synced (HEAD matches origin's tip, upstream
#      already correct, no uncommitted changes): no-op, no warning of any
#      kind (regression guard against false positives on the common case).
#   4. (#8287/#8354) Local branch sitting at the base — 0 commits ahead of
#      main, no uncommitted changes, i.e. "stale" by the pre-#8287 criterion —
#      while a LIVE origin/feature/issue-N still carries the branch's real
#      commit: worktree.sh measures and resets at the REMOTE tip, never at
#      main. The #8147/#8190 incident, where resetting to main handed the next
#      session an empty branch to force-push over a real PR.
#   5. (#8287/#8354) Same stale-local shape, but origin/feature/issue-N has
#      already landed (its tip is reachable from origin/main): worktree.sh
#      falls back to resetting at main, so the #5657 reused-branch-name skip
#      keeps working on this code path too.
#
# Cases 4 and 5 are the shell half of `loom-daemon worktree-stale-ref` (#8354,
# the Rust port of PR #8351's shell attempt): the decision itself — including
# the merged-PR rung of the landed ladder, which needs a forge — is pinned in
# loom-daemon/src/worktree_cli/stale_ref/tests.rs, while these two own the
# wiring, i.e. that worktree.sh actually asks and then honours both answers
# end to end. Both run with LOOM_BRANCH_LANDED_OFFLINE=1 so no case here can
# reach a network: case 4's verdict is "not landed as far as anything local can
# tell" and case 5's is settled by ancestry at the ladder's first rung.
#
# Pattern follows test-worktree-local-branch-upstream-tracking.sh: throwaway
# bare origin + repo in a mktemp dir, copy worktree.sh + lib/, but here the
# worktree itself is first materialized via a REAL `./.loom/scripts/worktree.sh
# <N>` call (so the fast path under test — "directory already exists,
# registered with git" — actually fires on the second invocation, exactly as
# it would for a reused builder worktree), then mutated to the drift shape
# under test before invoking worktree.sh a second time.
#
# SINCE #8195 SLICE 9 (epic #7810) this whole block is `loom-daemon
# worktree-upstream --arm registered-worktree`. The duplication this suite's
# own header describes — "that fix never ran here", of #6095/#6100 — is gone:
# both arms are now one implementation, selected by `--arm`, so the next fix
# to either cannot land in only one of them. Every assertion below is
# unchanged from the shell implementation, which is what makes them the
# equivalence evidence, so the binary is pinned via
# loom_test_require_daemon_bin and this suite FAILS rather than skips when
# there is none: without one the block does not run at all (a correct,
# documented degradation to the pre-#6257 behaviour) and Tests 1 and 3 would
# be asserting on something nothing produced.
#
# SINCE #8195 SLICE 12 the arm those five cases drive is ITSELF `loom-daemon
# worktree-existing` — the registration probe, this drift check, the staleness
# reference, the preserve-vs-reset verdict and the reset, one decision in one
# place — so `worktree-existing` joins the pin above. Every assertion below is
# still unchanged, and still runs the real `worktree.sh` end to end, which is
# what makes this suite the wiring evidence for that slice too. The arm's own
# corpus (symlinked roots, space- and metacharacter-bearing paths, a
# lookalike prefix sibling) lives in loom-daemon/tests/worktree_existing_differential.rs;
# none of those shapes appears below, which is exactly why that harness exists.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-upstream" "worktree-stale-ref" "worktree-existing"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Build a throwaway repo with a `feature/issue-<n>` branch pushed to origin,
# then materialize its worktree via a real (first) `worktree.sh <n>` call —
# so the worktree is registered with git exactly as an earlier
# worktree.sh/Builder pass would have left it, with correct tracking. Echoes
# "<repo-path> <worktree-relative-path>".
#
# Resolves the mktemp root to its physical path (pwd -P): on macOS /tmp is a
# symlink to /private/tmp, and worktree.sh's orphan-cleanup compares `git
# worktree list` paths (physical) against a resolved path — a symlinked temp
# root would make the just-registered worktree look unregistered and get
# spuriously deleted, defeating the point of this reuse-path test (mirrors
# test-worktree-sentinel-reinvoke.sh's TMP_ROOT handling).
setup_repo_with_worktree() {
    local name="$1"
    local issue="$2"
    local tmp
    tmp=$(cd "$(mktemp -d /tmp/loom-wtdrift.XXXXXX)" && pwd -P)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/$name"
    (
        cd "$tmp/$name"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin main
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh

        git checkout -q -b "feature/issue-$issue"
        echo "builder-work" > work.txt
        git add work.txt
        git commit -q -m "builder work"
        git push -q -u origin "feature/issue-$issue"
        git checkout -q main

        # First (real) worktree.sh invocation - materializes and registers
        # the worktree exactly as a Builder pass would.
        ./.loom/scripts/worktree.sh "$issue" >/dev/null 2>&1
    )
    echo "$tmp/$name .loom/worktrees/issue-$issue"
}

# Push one more commit to origin/feature/issue-<n> WITHOUT touching the
# existing worktree (which already has that branch checked out) — via a
# throwaway second clone, simulating a later push from a different
# session/worktree.
push_followup_commit() {
    local repo="$1"
    local issue="$2"
    local origin
    origin="$(git -C "$repo" remote get-url origin)"
    local clone_dir
    clone_dir=$(mktemp -d /tmp/loom-wtdrift-clone.XXXXXX)
    git clone -q "$origin" "$clone_dir" >/dev/null 2>&1
    (
        cd "$clone_dir"
        git config user.email t@t
        git config user.name t
        git checkout -q "feature/issue-$issue"
        echo "later-push" > later.txt
        git add later.txt
        git commit -q -m "later push from another session"
        git push -q origin "feature/issue-$issue"
    )
    rm -rf "$clone_dir"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# Rewind the worktree's LOCAL branch tip back to origin/main in place, WITHOUT
# touching origin/feature/issue-<n> — a worktree whose local branch never
# advanced (or lost its commit) while the branch's real content still lives on
# the remote. "0 commits ahead of main, no uncommitted changes" by the
# pre-#8287 staleness criterion, with the PR's actual content reachable only
# through origin/feature/issue-<n>.
#
# Also removes the `.loom-managed` sentinel the first worktree.sh invocation
# left behind: it is untracked and this throwaway fixture has no .gitignore for
# it, so leaving it would make `git status --porcelain` permanently non-empty
# and force every re-invocation down the "preserve existing work" branch
# instead of the staleness check under test (worktree.sh re-creates it on every
# exit path).
rewind_worktree_to_main() {
    local wt="$1"
    git -C "$wt" reset -q --hard origin/main
    rm -f "$wt/.loom-managed"
}

# Land feature/issue-<n> on origin/main by fast-forwarding main onto it and
# pushing — so origin/feature/issue-<n>'s tip becomes reachable from
# origin/main. That is the landed ladder's FIRST rung (ancestry), which needs
# no forge at all, so it is the offline way to drive the #5657 "this remote tip
# is dead history" arm.
land_branch_on_main() {
    local repo="$1" issue="$2"
    git -C "$repo" merge -q --ff-only "feature/issue-$issue"
    git -C "$repo" push -q origin main
}

# --- Test 1: behind pushed tip + wrong upstream + uncommitted changes (the incident) ---
echo "Test 1: worktree one commit behind pushed tip, wrong upstream, uncommitted WIP -> worktree.sh warns and corrects upstream without destroying WIP"
read -r REPO WT_REL <<< "$(setup_repo_with_worktree incident 301)"
WT="$REPO/$WT_REL"

# Simulate: another session pushed a follow-up commit this worktree never saw.
push_followup_commit "$REPO" 301

# Simulate: upstream tracking somehow got mis-set to origin/main (the #6095
# incident shape) after the worktree was created correctly.
git -C "$WT" branch --set-upstream-to=origin/main feature/issue-301

# Simulate: stale uncommitted WIP sitting in the working tree.
echo "stale-wip-line" >> "$WT/work.txt"

OUT_LOG="/tmp/wtdrift-incident.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 301 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)

if grep -qi "may be stale" "$OUT_LOG"; then
    pass "worktree.sh warns that the worktree may be stale"
else
    fail "worktree.sh did not warn about staleness"
    cat "$OUT_LOG"
fi

if grep -qi "uncommitted changes" "$OUT_LOG"; then
    pass "worktree.sh flags the uncommitted changes alongside the drift warning"
else
    fail "worktree.sh did not mention the uncommitted changes in its drift warning"
fi

WT_UPSTREAM=$(git -C "$WT" rev-parse --abbrev-ref 'feature/issue-301@{u}' 2>/dev/null || echo "")
if [[ "$WT_UPSTREAM" == "origin/feature/issue-301" ]]; then
    pass "worktree.sh corrected the upstream to origin/feature/issue-301 (was origin/main)"
else
    fail "worktree's upstream is '$WT_UPSTREAM', expected 'origin/feature/issue-301'"
fi

if grep -q "stale-wip-line" "$WT/work.txt"; then
    pass "uncommitted WIP was NOT destroyed by the drift check"
else
    fail "uncommitted WIP was lost"
fi

WT_HEAD=$(git -C "$WT" rev-parse HEAD)
ORIGIN_TIP=$(git -C "$REPO" rev-parse origin/feature/issue-301)
if [[ "$WT_HEAD" != "$ORIGIN_TIP" ]]; then
    pass "worktree.sh did not silently pull/reset HEAD on its own (still behind, as expected for a warn-only check)"
else
    fail "worktree.sh unexpectedly moved HEAD to the remote tip"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 2: local commit ahead of pushed tip, no uncommitted changes -> no false positive ---
echo ""
echo "Test 2: worktree has an unpushed local commit ahead of origin's tip, no uncommitted changes -> no 'may be stale' false positive"
read -r REPO WT_REL <<< "$(setup_repo_with_worktree ahead 302)"
WT="$REPO/$WT_REL"

# Add a local commit in the worktree that has NOT been pushed.
(
    cd "$WT"
    echo "unpushed-local-commit" > unpushed.txt
    git add unpushed.txt
    git commit -q -m "unpushed local work"
)

OUT_LOG="/tmp/wtdrift-ahead.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 302 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)

if grep -qi "may be stale" "$OUT_LOG"; then
    fail "worktree.sh false-positive warned about staleness for a worktree that is genuinely AHEAD, not behind"
    cat "$OUT_LOG"
else
    pass "worktree.sh did not false-positive warn for a worktree ahead of origin (unpushed local commit)"
fi
if grep -qi "preserving existing work" "$OUT_LOG"; then
    pass "worktree.sh still reports preserving the existing (ahead) work"
else
    fail "worktree.sh did not report preserving the ahead work"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 3: already correctly synced -> pure no-op, no warnings at all ---
echo ""
echo "Test 3: worktree already matches origin's tip, upstream already correct, no uncommitted changes -> no-op"
read -r REPO WT_REL <<< "$(setup_repo_with_worktree synced 303)"
WT="$REPO/$WT_REL"

# Nothing mutated - this worktree is exactly as worktree.sh's first
# invocation left it: HEAD == origin/feature/issue-303, upstream already
# correct, clean tree.

OUT_LOG="/tmp/wtdrift-synced.$$"
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 303 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)

if grep -qi "may be stale\|correcting to\|has no upstream" "$OUT_LOG"; then
    fail "worktree.sh printed a drift/correction warning for an already-synced worktree"
    cat "$OUT_LOG"
else
    pass "worktree.sh made no drift/correction noise for the already-synced case"
fi
WT_UPSTREAM=$(git -C "$WT" rev-parse --abbrev-ref 'feature/issue-303@{u}' 2>/dev/null || echo "")
if [[ "$WT_UPSTREAM" == "origin/feature/issue-303" ]]; then
    pass "worktree's upstream remains origin/feature/issue-303 (unaffected)"
else
    fail "worktree's upstream is '$WT_UPSTREAM', expected unchanged 'origin/feature/issue-303'"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 4 (#8287/#8354): stale local branch + LIVE remote branch -> reference and reset target are the remote tip ---
echo ""
echo "Test 4 (#8287/#8354): local branch rewound to main (0 ahead, no uncommitted changes) while origin/feature/issue-N is live and unlanded -> worktree.sh measures and resets at the remote tip, not main"
read -r REPO WT_REL <<< "$(setup_repo_with_worktree staleref 401)"
WT="$REPO/$WT_REL"
ORIGIN_TIP=$(git -C "$REPO" rev-parse origin/feature/issue-401)

rewind_worktree_to_main "$WT"

OUT_LOG="/tmp/wtdrift-staleref.$$"
(
    cd "$REPO"
    LOOM_BRANCH_LANDED_OFFLINE=1 ./.loom/scripts/worktree.sh 401 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)

if [[ -f "$WT/work.txt" ]]; then
    pass "worktree recovered the branch's real content from origin (not reset to bare main)"
else
    fail "worktree lost the branch's content — it was reset to main instead of the live remote tip"
    cat "$OUT_LOG"
fi
WT_HEAD=$(git -C "$WT" rev-parse HEAD 2>/dev/null || echo "")
if [[ "$WT_HEAD" == "$ORIGIN_TIP" ]]; then
    pass "worktree HEAD equals origin/feature/issue-401's tip (the reset target was the remote branch)"
else
    fail "worktree HEAD ($WT_HEAD) does not equal origin/feature/issue-401's tip ($ORIGIN_TIP)"
    cat "$OUT_LOG"
fi
# Deliberately matched against the RESET-TARGET sentence rather than a bare
# "origin/feature/issue-401" anywhere in the output: the drift check above
# names that ref too (its "pushed tip of branch" hint), so a bare grep passes
# even with the staleness reference left at main — it is not evidence of
# anything this test is about.
if grep -q "Resetting worktree in place to origin/feature/issue-401" "$OUT_LOG"; then
    pass "output names origin/feature/issue-401 as the reset target, not main"
else
    fail "output does not name origin/feature/issue-401 as the reset target (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Test 5 (#8287/#8354): stale local branch + ALREADY-LANDED remote tip -> falls back to the base ---
echo ""
echo "Test 5 (#8287/#8354): same stale-local shape, but origin/feature/issue-N has already landed on origin/main -> worktree.sh still falls back to main (#5657 skip unaffected)"
read -r REPO WT_REL <<< "$(setup_repo_with_worktree stalereflanded 402)"
WT="$REPO/$WT_REL"
land_branch_on_main "$REPO" 402
MAIN_TIP=$(git -C "$REPO" rev-parse origin/main)

rewind_worktree_to_main "$WT"

OUT_LOG="/tmp/wtdrift-stalereflanded.$$"
(
    cd "$REPO"
    LOOM_BRANCH_LANDED_OFFLINE=1 ./.loom/scripts/worktree.sh 402 >"$OUT_LOG" 2>&1 || { echo "FAILED"; cat "$OUT_LOG"; }
)

if grep -q "origin/feature/issue-402" "$OUT_LOG"; then
    fail "output names the already-landed origin/feature/issue-402 as the reference — the #5657 skip regressed on this code path"
    cat "$OUT_LOG"
else
    pass "output does not treat the already-landed origin/feature/issue-402 as the reference"
fi
if grep -q "Resetting worktree in place to main" "$OUT_LOG"; then
    pass "worktree.sh reports main as the reset target (the base-ref fallback)"
else
    fail "worktree.sh did not report main as the reset target (see $OUT_LOG)"
    cat "$OUT_LOG"
fi
WT_HEAD=$(git -C "$WT" rev-parse HEAD 2>/dev/null || echo "")
if [[ "$WT_HEAD" == "$MAIN_TIP" ]]; then
    pass "worktree HEAD equals origin/main's tip (fell back to the base ref, as #5657 requires)"
else
    fail "worktree HEAD ($WT_HEAD) does not equal origin/main's tip ($MAIN_TIP)"
    cat "$OUT_LOG"
fi
cleanup_repo "$REPO"
rm -f "$OUT_LOG"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
