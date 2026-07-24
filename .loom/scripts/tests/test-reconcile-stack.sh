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
}

teardown_sandbox() {
    [[ -n "$SANDBOX" && -d "$SANDBOX" ]] && rm -rf "$SANDBOX"
    SANDBOX=""
}

# Run reconcile-stack.sh from $1 (a cwd), capturing combined output + exit code.
# Result in RUN_OUT / RUN_RC. gh stub + git file-protocol are made available.
RUN_OUT=""
RUN_RC=0
run_reconcile() {
    local cwd="$1"
    RUN_RC=0
    RUN_OUT="$(
        cd "$cwd" &&
        PATH="$GH_STUB_DIR:$PATH" \
        LOOM_DEFAULT_BRANCH="main" \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="protocol.file.allow" GIT_CONFIG_VALUE_0="always" \
        bash "$RECONCILE" "$CHILD_PR" "$PARENT_BR" 2>&1
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

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
