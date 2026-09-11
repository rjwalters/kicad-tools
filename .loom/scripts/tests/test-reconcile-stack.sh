#!/usr/bin/env bash
# test-reconcile-stack.sh — Regression tests for reconcile-stack.sh (#3776).
#
# Covers the two defects hit live 2026-07-22 when collapsing a stacked-PR stack:
#
#   1. Worktree-checked-out child branch (the blocker). A Loom child branch is
#      ALWAYS checked out in its own managed worktree, and git refuses to rebase
#      a branch checked out in another worktree
#      (`fatal: '<branch>' is already used by worktree at ...`). reconcile-stack.sh
#      must detect that worktree (via `git worktree list`) and run the rebase
#      INSIDE it. Scenario A drives the exact failure and asserts a clean rebase.
#
#   2. False "origin/<parent> still exists" warning on a stale local ref. With
#      delete-branch-on-merge the parent branch is deleted on the remote the
#      instant it merges, but the local refs/remotes/origin/<parent> can linger
#      stale until a prune — a `git show-ref` of that stale ref then false-warns
#      on every post-merge reconcile. The fix queries the remote live via
#      `git ls-remote`. Both scenarios assert the spurious warning is absent.
#
# Strategy: build a real, offline git sandbox (bare "remote" + a clone), stack a
# child on a parent, simulate the parent's squash-merge to the default branch,
# delete the parent branch IN THE BARE REMOTE (leaving the clone's local
# remote-tracking ref stale — the exact #3776 condition), then run the ACTUAL
# defaults/scripts/reconcile-stack.sh with a stubbed `gh` on PATH. No network.
#
#   Scenario A: child branch checked out in a worktree  -> rebase runs there.
#   Scenario B: child branch NOT checked out anywhere    -> in-place fallback.
#
# Usage:
#   ./.loom/scripts/tests/test-reconcile-stack.sh

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
RECONCILE="$SCRIPTS_DIR/reconcile-stack.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# Per-scenario sandbox globals.
SANDBOX=""
REMOTE=""
MAIN=""
GH_STUB_DIR=""
GH_EDIT_LOG=""
GIT_STUB_DIR=""

git_q() { git -c advice.detachedHead=false -c protocol.file.allow=always "$@"; }

# Build a fresh sandbox:
#   remote.git  — bare "origin"
#   main/       — a clone with:
#       main branch:  base -> P-squash          (parent already squash-merged)
#       feature/issue-8001 (parent): base -> P-original   (pushed, then DELETED
#                                                          in the bare remote so
#                                                          the clone keeps a
#                                                          STALE origin ref)
#       feature/issue-8002 (child):  base -> P-original -> C   (pushed)
# The child therefore still carries the parent's pre-squash commit; a correct
# `rebase --onto main <parent> <child>` must strip P-original, leaving base ->
# P-squash -> C.
CHILD_BR="feature/issue-8002"
PARENT_BR="feature/issue-8001"
CHILD_PR="9002"

setup_sandbox() {
    SANDBOX="$(mktemp -d)"
    REMOTE="$SANDBOX/remote.git"
    MAIN="$SANDBOX/main"

    git_q init --quiet --bare "$REMOTE"

    git_q init --quiet "$MAIN"
    git_q -C "$MAIN" config user.email "test@loom.local"
    git_q -C "$MAIN" config user.name "Loom Test"
    git_q -C "$MAIN" config commit.gpgsign false
    git_q -C "$MAIN" checkout -q -b main
    echo "base" > "$MAIN/base.txt"
    git_q -C "$MAIN" add base.txt
    git_q -C "$MAIN" commit -q -m "base"
    git_q -C "$MAIN" remote add origin "$REMOTE"
    git_q -C "$MAIN" push -q -u origin main

    # Parent branch: one original pre-squash commit.
    git_q -C "$MAIN" checkout -q -b "$PARENT_BR"
    echo "parent" > "$MAIN/parent.txt"
    git_q -C "$MAIN" add parent.txt
    git_q -C "$MAIN" commit -q -m "P-original"
    git_q -C "$MAIN" push -q -u origin "$PARENT_BR"

    # Child branch: stacked on the parent, one own commit.
    git_q -C "$MAIN" checkout -q -b "$CHILD_BR"
    echo "child" > "$MAIN/child.txt"
    git_q -C "$MAIN" add child.txt
    git_q -C "$MAIN" commit -q -m "C-own-commit"
    git_q -C "$MAIN" push -q -u origin "$CHILD_BR"

    # Simulate the parent squash-merging to main: one squashed commit on main.
    git_q -C "$MAIN" checkout -q main
    echo "parent" > "$MAIN/parent.txt"
    git_q -C "$MAIN" add parent.txt
    git_q -C "$MAIN" commit -q -m "P-squash (#8001)"
    git_q -C "$MAIN" push -q origin main

    # Delete the parent branch IN THE BARE REMOTE directly (delete-branch-on-merge
    # equivalent), WITHOUT pruning the clone — so refs/remotes/origin/<parent>
    # is now STALE in the clone. This is the exact #3776 false-warn condition.
    git_q -C "$REMOTE" branch -D "$PARENT_BR" >/dev/null 2>&1

    # Sanity: the stale local ref really is still present in the clone.
    if ! git_q -C "$MAIN" show-ref --verify --quiet "refs/remotes/origin/$PARENT_BR"; then
        echo -e "  ${RED}FATAL${NC}: test setup expected a stale origin/$PARENT_BR ref in the clone" >&2
        exit 2
    fi

    # Stub gh on PATH: resolve the child head branch and record `pr edit`.
    GH_STUB_DIR="$SANDBOX/bin"
    mkdir -p "$GH_STUB_DIR"
    GH_EDIT_LOG="$SANDBOX/gh-edit.log"
    : > "$GH_EDIT_LOG"
    cat > "$GH_STUB_DIR/gh" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "pr" && "\$2" == "view" ]]; then
  # gh pr view <n> --json headRefName --jq '.headRefName'
  echo "$CHILD_BR"
  exit 0
fi
if [[ "\$1" == "pr" && "\$2" == "edit" ]]; then
  echo "\$*" >> "$GH_EDIT_LOG"
  exit 0
fi
echo "stub gh: unhandled: \$*" >&2
exit 3
STUB
    chmod +x "$GH_STUB_DIR/gh"

    # Stub `git` that transparently delegates to the REAL git for everything
    # except a --force-with-lease push made while LOOM_TEST_FAKE_REJECT=1: in
    # that one case it runs the real push (so the remote genuinely updates,
    # exactly like the LFS-hook race in #6695), then discards the real exit
    # code and prints a synthetic rejection instead — modeling a push that
    # reports failure even though it landed.
    GIT_STUB_DIR="$SANDBOX/gitbin"
    mkdir -p "$GIT_STUB_DIR"
    REAL_GIT="$(command -v git)"
    cat > "$GIT_STUB_DIR/git" <<STUB
#!/usr/bin/env bash
REAL_GIT="$REAL_GIT"
if [[ "\$1" == "push" && "\${LOOM_TEST_FAKE_REJECT:-0}" == "1" ]]; then
    "\$REAL_GIT" "\$@"
    rc=\$?
    if [[ \$rc -eq 0 ]]; then
        echo "To $REMOTE" >&2
        echo " ! [rejected]        $CHILD_BR -> $CHILD_BR (stale info)" >&2
        echo "error: failed to push some refs to '$REMOTE'" >&2
        echo "remote rejected $CHILD_BR -> $CHILD_BR (is at deadbeefcafe but expected 0000000)" >&2
        exit 1
    fi
    exit "\$rc"
fi
exec "\$REAL_GIT" "\$@"
STUB
    chmod +x "$GIT_STUB_DIR/git"
}

teardown_sandbox() {
    [[ -n "$SANDBOX" && -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
    SANDBOX=""
}

# Run reconcile-stack.sh from $1 (a cwd), capturing combined output + exit code.
# Result in RUN_OUT / RUN_RC. gh stub + git file-protocol are made available.
# Extra positional args (e.g. --dry-run) are forwarded to reconcile-stack.sh.
# LOOM_VERSION_CHECK_SCRIPT, when set in the caller's environment, is
# forwarded too (the #7341 version-check-gate.sh test seam).
RUN_OUT=""
RUN_RC=0
run_reconcile() {
    local cwd="$1"
    shift
    RUN_RC=0
    RUN_OUT="$(
        cd "$cwd" &&
        PATH="$GIT_STUB_DIR:$GH_STUB_DIR:$PATH" \
        LOOM_DEFAULT_BRANCH="main" \
        LOOM_TEST_FAKE_REJECT="${LOOM_TEST_FAKE_REJECT:-0}" \
        LOOM_VERSION_CHECK_SCRIPT="${LOOM_VERSION_CHECK_SCRIPT:-}" \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="protocol.file.allow" GIT_CONFIG_VALUE_0="always" \
        bash "$RECONCILE" "$CHILD_PR" "$PARENT_BR" "$@" 2>&1
    )" || RUN_RC=$?
}

# ─────────────────────────────────────────────────────────────────────────────
echo "Scenario A: child branch checked out in a worktree (the #3776 blocker)"
setup_sandbox

# Create a linked worktree holding the child branch checked out — exactly the
# Loom managed-worktree situation that made the un-fixed script fail.
CHILD_WT="$SANDBOX/child-wt"
git_q -C "$MAIN" worktree add -q "$CHILD_WT" "$CHILD_BR"

# Run from the MAIN worktree (NOT the child worktree) — the reported failure mode.
run_reconcile "$MAIN"

assert_eq "0" "$RUN_RC" "A: reconcile exits 0 even though the child branch is checked out in a worktree"
assert_not_contains "$RUN_OUT" "is already used by worktree" \
  "A: no 'already used by worktree' fatal (rebase ran inside the child worktree)"
assert_contains "$RUN_OUT" "checked out in worktree" \
  "A: script reports it detected the child worktree"

# The child branch must now be base -> P-squash -> C (parent's original commit stripped).
CHILD_LOG="$(git_q -C "$CHILD_WT" log --format=%s main.."$CHILD_BR")"
assert_eq "C-own-commit" "$CHILD_LOG" "A: child branch carries ONLY its own commit above main"
FULL_LOG="$(git_q -C "$CHILD_WT" log --format=%s)"
assert_not_contains "$FULL_LOG" "P-original" "A: parent's pre-squash commit was stripped by the rebase"
assert_contains "$FULL_LOG" "P-squash" "A: child now sits on main's squashed parent commit"

# The false stale-origin warning must NOT appear (parent gone on remote).
assert_not_contains "$RUN_OUT" "still exists" \
  "A: no false 'origin/<parent> still exists' warning on a stale local ref"

# The base retarget was attempted via gh.
assert_contains "$(cat "$GH_EDIT_LOG")" "pr edit $CHILD_PR --base main" \
  "A: child PR base retargeted to the default branch"

teardown_sandbox

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Scenario B: child branch NOT checked out in any worktree (in-place fallback)"
setup_sandbox

# No worktree created — the branch is checked out nowhere, so the historical
# in-place rebase path applies and must still work.
run_reconcile "$MAIN"

assert_eq "0" "$RUN_RC" "B: reconcile exits 0 in the no-worktree fallback path"
assert_not_contains "$RUN_OUT" "checked out in worktree" \
  "B: script does NOT claim a worktree when none holds the branch"
CHILD_LOG_B="$(git_q -C "$MAIN" log --format=%s main.."$CHILD_BR")"
assert_eq "C-own-commit" "$CHILD_LOG_B" "B: child branch carries ONLY its own commit above main"
assert_not_contains "$RUN_OUT" "still exists" \
  "B: no false 'origin/<parent> still exists' warning on a stale local ref"

teardown_sandbox

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Scenario C: push --force-with-lease reports a rejection but the ref landed (#6695)"
setup_sandbox

# LOOM_TEST_FAKE_REJECT=1 makes the git stub run the REAL push (so origin
# genuinely advances, exactly like the LFS pre-push hook race) and then
# discard the real (successful) exit code in favor of a synthetic rejection —
# modeling the observed failure mode.
LOOM_TEST_FAKE_REJECT=1 run_reconcile "$MAIN"

assert_eq "0" "$RUN_RC" \
  "C: reconcile still exits 0 — the push-lease-verify check treats the landed ref as success"
assert_contains "$RUN_OUT" "PUSH-LEASE-RACE-DETECTED" \
  "C: the race is logged with the greppable PUSH-LEASE-RACE-DETECTED marker"
assert_not_contains "$RUN_OUT" "Force-with-lease push was rejected (someone else pushed" \
  "C: the reported rejection is NOT treated as an ordinary failure"

# The remote must actually carry the rebased child tip — confirms the push
# really did land, not just that the script decided to proceed anyway.
LOCAL_CHILD_SHA="$(git_q -C "$MAIN" rev-parse "$CHILD_BR")"
REMOTE_CHILD_SHA="$(git_q -C "$REMOTE" rev-parse "$CHILD_BR")"
assert_eq "$LOCAL_CHILD_SHA" "$REMOTE_CHILD_SHA" \
  "C: origin/$CHILD_BR actually equals the local child tip after the 'rejected' push"

# The base retarget still ran (script proceeded past the false rejection).
assert_contains "$(cat "$GH_EDIT_LOG")" "pr edit $CHILD_PR --base main" \
  "C: child PR base still retargeted after the race was detected"

teardown_sandbox

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Scenario D: push --force-with-lease is genuinely rejected (real conflicting push)"
setup_sandbox

# Simulate a concurrent pusher landing a DIFFERENT commit on the child branch,
# directly against the bare remote, behind $MAIN's back (no fetch in $MAIN).
CONFLICT_CLONE="$SANDBOX/conflict-clone"
git_q clone --quiet "$REMOTE" "$CONFLICT_CLONE"
git_q -C "$CONFLICT_CLONE" checkout -q "$CHILD_BR"
echo "concurrent" > "$CONFLICT_CLONE/conflict.txt"
git_q -C "$CONFLICT_CLONE" add conflict.txt
git_q -C "$CONFLICT_CLONE" commit -q -m "concurrent push (not from \$MAIN)"
git_q -C "$CONFLICT_CLONE" push -q origin "$CHILD_BR"

# $MAIN's rebase in Step 1 still runs locally against its (stale) view, so the
# subsequent --force-with-lease push genuinely conflicts with the real,
# already-landed concurrent commit above — a TRUE rejection, not a race.
run_reconcile "$MAIN"

assert_eq "2" "$RUN_RC" "D: a genuine rejection still exits 2 (never treated as success)"
assert_contains "$RUN_OUT" "Force-with-lease push was rejected (someone else pushed to $CHILD_BR)" \
  "D: genuine rejection is reported as an ordinary failure"
assert_not_contains "$RUN_OUT" "PUSH-LEASE-RACE-DETECTED" \
  "D: a real rejection is never mislabeled as the race condition"
# The concurrent pusher's commit remains on origin — $MAIN's push did not land.
REMOTE_CHILD_SHA_D="$(git_q -C "$REMOTE" rev-parse "$CHILD_BR")"
CONFLICT_SHA="$(git_q -C "$CONFLICT_CLONE" rev-parse "$CHILD_BR")"
assert_eq "$CONFLICT_SHA" "$REMOTE_CHILD_SHA_D" \
  "D: origin/$CHILD_BR still holds the concurrent commit, unaffected by the rejected push"

teardown_sandbox

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Scenario E: version-check-gate.sh (#7168, extended #7341) reports a mismatch after rebase --onto"
setup_sandbox

# `git rebase --onto` replays ONLY the child's own commits, silently
# absorbing whatever version-bearing values the default branch already had
# -- the exact drift that shipped as bef3e07a on feature/issue-6612 (#7341).
# Stub LOOM_VERSION_CHECK_SCRIPT (the shared gate's own test seam) to report
# a MISMATCH unconditionally, simulating that drift, and confirm
# reconcile-stack.sh aborts BEFORE pushing or retargeting the child PR.
STUB_MISMATCH="$SANDBOX/version-mismatch.sh"
cat > "$STUB_MISMATCH" <<'STUB'
#!/usr/bin/env bash
echo "MISMATCH  .loom/install-metadata.json: 0.1.0 (expected 0.2.0)"
exit 1
STUB
chmod +x "$STUB_MISMATCH"

REMOTE_CHILD_SHA_BEFORE_E="$(git_q -C "$REMOTE" rev-parse "$CHILD_BR")"
LOOM_VERSION_CHECK_SCRIPT="$STUB_MISMATCH" run_reconcile "$MAIN"

assert_eq "2" "$RUN_RC" "E: a version-bearing-file mismatch after rebase aborts with exit 2 (never pushed)"
assert_contains "$RUN_OUT" "BLOCKER" "E: the shared gate's BLOCKER: message is surfaced"
assert_contains "$RUN_OUT" "out of sync" "E: reconcile-stack.sh's own abort message names the mismatch"
assert_not_contains "$RUN_OUT" "Step 2/3" "E: the push step never started"
REMOTE_CHILD_SHA_AFTER_E="$(git_q -C "$REMOTE" rev-parse "$CHILD_BR")"
assert_eq "$REMOTE_CHILD_SHA_BEFORE_E" "$REMOTE_CHILD_SHA_AFTER_E" \
  "E: origin/$CHILD_BR is unchanged -- the rebased-but-ungated commit was never pushed"
assert_eq "" "$(cat "$GH_EDIT_LOG")" "E: the child PR base was never retargeted"

teardown_sandbox

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "Scenario F: --dry-run skips the version-check-gate (nothing was actually rebased)"
setup_sandbox

LOOM_VERSION_CHECK_SCRIPT="$STUB_MISMATCH" run_reconcile "$MAIN" --dry-run

assert_eq "0" "$RUN_RC" "F: --dry-run exits 0 even with a mismatching gate stub (no real rebase happened)"
assert_not_contains "$RUN_OUT" "BLOCKER" "F: the gate never runs under --dry-run"
assert_contains "$RUN_OUT" "Dry run complete" "F: dry-run still reports its own completion message"

teardown_sandbox

# ─────────────────────────────────────────────────────────────────────────────
# Source guards: fail loudly if a refactor drops either fix.
echo ""
echo "Source guards on reconcile-stack.sh"
src="$(cat "$RECONCILE")"
assert_contains "$src" "git worktree list --porcelain" \
  "reconcile-stack.sh detects the child worktree via git worktree list --porcelain"
assert_contains "$src" "git -C" \
  "reconcile-stack.sh runs the rebase/push in the worktree via git -C"
assert_contains "$src" "git ls-remote" \
  "reconcile-stack.sh uses a live ls-remote check for the stale-origin advisory"
assert_contains "$src" "push_landed_despite_rejection" \
  "reconcile-stack.sh verifies the actual remote ref state after a rejected --force-with-lease push (#6695)"
assert_contains "$src" "PUSH-LEASE-RACE-DETECTED" \
  "reconcile-stack.sh logs a greppable marker when a reported rejection is actually landed"
assert_contains "$src" '"$SCRIPT_DIR/version-check-gate.sh"' \
  "reconcile-stack.sh runs the shared version-check-gate.sh after rebase, before push (#7168, #7341)"
assert_contains "$src" 'DRY_RUN' \
  "reconcile-stack.sh's version-check-gate call is itself skipped under --dry-run"

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
