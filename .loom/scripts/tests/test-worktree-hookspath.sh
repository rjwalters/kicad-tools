#!/usr/bin/env bash
# test-worktree-hookspath.sh — Tests for the core.hooksPath guard (#3638)
#
# worktree.sh sets `git config core.hooksPath .githooks` on every new worktree
# so a repo's tracked .githooks/ works without npx/husky. Prior to #3638 this
# was unconditional: repos WITHOUT a .githooks/ dir got core.hooksPath pointed
# at a nonexistent directory, which git treats as "no hooks" — silently
# disabling any hooks configured elsewhere (e.g. a repo-level core.hooksPath).
#
# The fix guards the config on `[[ -d "$WORKTREE_REPO_ROOT/.githooks" ]]`.
#
# Coverage:
#   1. Repo WITH .githooks/ → worktree's core.hooksPath == .githooks (unchanged).
#   2. Repo WITHOUT .githooks/ → worktree's core.hooksPath is unset (no dangling
#      pointer).
#
# Pattern follows test-worktree-root-override.sh: throwaway bare origin + repo
# in a mktemp dir, copy worktree.sh + lib/, run.

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

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

# Build a throwaway repo with an origin/main ref and the minimal .loom layout
# worktree.sh needs. If $2 is "with-githooks", a tracked .githooks/ dir is
# committed. Echoes the working-tree path.
setup_repo() {
    local name="${1:-myrepo}"
    local githooks="${2:-}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wthooks.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/$name"
    (
        cd "$tmp/$name"
        git config user.email t@t
        git config user.name t
        if [[ "$githooks" == "with-githooks" ]]; then
            mkdir -p .githooks
            printf '#!/bin/sh\nexit 0\n' > .githooks/pre-commit
            chmod +x .githooks/pre-commit
            git add .githooks
        fi
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin main
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# --- Test 1: repo WITH .githooks/ → hooksPath set ---
echo "Test 1: repo WITH .githooks/ still gets core.hooksPath == .githooks"
REPO=$(setup_repo hookrepo with-githooks)
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 100 >/tmp/wthooks-with.$$ 2>&1 || {
        echo "worktree.sh failed (see /tmp/wthooks-with.$$)"; cat /tmp/wthooks-with.$$
    }
)
HOOKS_WITH=$(git -C "$REPO/.loom/worktrees/issue-100" config --get core.hooksPath || true)
assert_eq "$HOOKS_WITH" ".githooks" "core.hooksPath set to .githooks when repo ships .githooks/"
cleanup_repo "$REPO"

# --- Test 2: repo WITHOUT .githooks/ → hooksPath unset ---
echo ""
echo "Test 2: repo WITHOUT .githooks/ leaves core.hooksPath unset (no dangling pointer)"
REPO=$(setup_repo nohookrepo)
(
    cd "$REPO"
    ./.loom/scripts/worktree.sh 200 >/tmp/wthooks-without.$$ 2>&1 || {
        echo "worktree.sh failed (see /tmp/wthooks-without.$$)"; cat /tmp/wthooks-without.$$
    }
)
HOOKS_WITHOUT=$(git -C "$REPO/.loom/worktrees/issue-200" config --get core.hooksPath || true)
assert_eq "$HOOKS_WITHOUT" "" "core.hooksPath unset when repo has no .githooks/"
cleanup_repo "$REPO"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
