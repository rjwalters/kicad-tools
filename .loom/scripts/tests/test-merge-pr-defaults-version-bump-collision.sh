#!/usr/bin/env bash
# test-merge-pr-defaults-version-bump-collision.sh - Unit tests for the
# PRE-merge defaults/ VERSION-bump collision guard in merge-pr.sh (#7302).
#
# check-defaults-version-bump.sh's CI job gates a PR's own HEAD VERSION
# against its PR's `base.sha` -- fixed at PR-open (or last-rebase) time and
# never re-diffed against the CURRENT default branch. When two PRs are open
# concurrently and both bump VERSION from the same stale base to the same
# target, the first to merge advances the default branch to that target; the
# CI gate on the second PR already passed against its own stale base and has
# no visibility into that concurrent merge, so it can land on top with a
# NET-ZERO version increment despite genuinely changing `defaults/` -- see
# PR #7300 vs. concurrently-merged #7298.
#
# _check_defaults_version_bump_collision closes this by re-running the SAME
# check-defaults-version-bump.sh script (unmodified) here, at the merge
# choke point, against the default branch's CURRENT tip instead of the PR's
# stale base.sha.
#
# Strategy: build two REAL local git repos ("origin" and a "local" clone of
# it, playing REPO_ROOT) so the guard's actual `git fetch` / `git rev-parse`
# calls run against genuine refs -- no git stubbing, unlike the gh-stubbing
# tests for other guards, since this guard's whole job is comparing real git
# state. The check-defaults-version-bump.sh script itself is copied
# byte-for-byte from the real source tree (never modified, per #7302's own
# acceptance criteria) into each fixture's defaults/scripts/ so
# `$REPO_ROOT/defaults/scripts/check-defaults-version-bump.sh` resolves.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-defaults-version-bump-collision.sh

# SC2034: several globals (REPO_ROOT, DEFAULT_BRANCH_NAME, PR_BRANCH,
# PR_HEAD_SHA, PR_JSON, PR_NUMBER, DRY_RUN) are read only by the function
# extracted+sourced from merge-pr.sh, which shellcheck cannot see.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
REAL_CHECK_SCRIPT="$HELPERS_DIR/check-defaults-version-bump.sh"

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
    if grep -qF -- "$needle" <<<"$haystack"; then
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
    if ! grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$REAL_CHECK_SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: check-defaults-version-bump.sh missing or not executable: $REAL_CHECK_SCRIPT" >&2
    exit 2
fi

# --- Minimal logging/error shims the extracted function calls ---
# `error` must exit non-zero to faithfully model the real script's hard
# block; the guard is always invoked in a subshell (see run_guard) so this
# exit only tears down that subshell, not the test.
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- Extract the function under test from merge-pr.sh and source it ---
FUNCS_FILE="$(mktemp)"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-vbump-collision.XXXXXX")"
trap 'rm -rf "$FUNCS_FILE" "$WORKDIR" 2>/dev/null || true' EXIT

awk '
  /^_check_defaults_version_bump_collision\(\) \{/ { capture=1 }
  /^# Invoke this guard too,/                        { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_check_defaults_version_bump_collision()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_defaults_version_bump_collision from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

ORIGIN="$WORKDIR/origin"
LOCAL="$WORKDIR/local"

# Fresh "origin" repo on branch main: defaults/foo.md + VERSION=1.0.0,
# committed as "base". A real copy of the (unmodified) check script is
# vendored into defaults/scripts/ so the guard's
# $REPO_ROOT/defaults/scripts/check-defaults-version-bump.sh resolves inside
# the fixture. A "local" clone plays REPO_ROOT (mirrors merge-pr.sh running
# from the primary checkout).
make_fixture() {
    rm -rf "$ORIGIN" "$LOCAL"
    git init --quiet "$ORIGIN"
    git -C "$ORIGIN" checkout -q -b main
    mkdir -p "$ORIGIN/defaults/scripts"
    cp "$REAL_CHECK_SCRIPT" "$ORIGIN/defaults/scripts/check-defaults-version-bump.sh"
    chmod +x "$ORIGIN/defaults/scripts/check-defaults-version-bump.sh"
    echo "hello" > "$ORIGIN/defaults/scripts/foo.md"
    echo "1.0.0" > "$ORIGIN/VERSION"
    git -C "$ORIGIN" add -A
    git -C "$ORIGIN" commit -q -m "base"

    git clone --quiet "$ORIGIN" "$LOCAL"
}

# Creates a PR branch on both origin and local: based on origin/main's
# CURRENT tip, changes defaults/scripts/foo.md, sets VERSION to $1, commits.
# Sets PR_BRANCH / PR_HEAD_SHA globals. Pushes the branch to origin (a real
# open PR always has its branch on the remote).
make_pr_branch() {
    local target_version="$1" branch="${2:-feature/issue-1}"
    git -C "$LOCAL" fetch --quiet origin
    git -C "$LOCAL" checkout -q -B "$branch" "origin/main"
    echo "changed by PR" >> "$LOCAL/defaults/scripts/foo.md"
    echo "$target_version" > "$LOCAL/VERSION"
    git -C "$LOCAL" commit -q -am "pr change"
    git -C "$LOCAL" push --quiet origin "$branch"
    PR_BRANCH="$branch"
    PR_HEAD_SHA="$(git -C "$LOCAL" rev-parse HEAD)"
}

# Advances origin/main by $1 (a commit description) to $2 (VERSION), touching
# defaults/ too -- simulates a concurrently-merged PR advancing the default
# branch out from under the PR being tested.
advance_origin_main() {
    local target_version="$1"
    git -C "$ORIGIN" checkout -q main
    echo "changed by concurrent merge" >> "$ORIGIN/defaults/scripts/foo.md"
    echo "$target_version" > "$ORIGIN/VERSION"
    git -C "$ORIGIN" commit -q -am "concurrent merge bump"
}

# Shared globals the function reads (see the file-level SC2034 disable).
PR_NUMBER="7302"
DRY_RUN=false

# Run the guard in a subshell (its block path calls `error`, which exit 1's),
# capturing combined stdout+stderr in LAST_OUT and the exit code in LAST_RC.
LAST_OUT=""
LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( _check_defaults_version_bump_collision 2>&1 )"
    LAST_RC=$?
    set -e
}

echo "Testing _check_defaults_version_bump_collision behavior..."

# T1: no collision -- origin/main unchanged since the PR branched off it, PR
# bumps VERSION 1.0.0 -> 1.0.1 alongside a defaults/ change. Guard passes.
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
make_pr_branch "1.0.1"
PR_JSON='{"body":""}'
run_guard
assert_eq "0" "$LAST_RC" "No concurrent merge -> guard passes (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "No concurrent merge -> no block message"

# T2: collision -- a concurrent merge already advanced origin/main to the
# SAME target VERSION (1.0.1) the PR itself bumps to. Guard hard-blocks.
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
make_pr_branch "1.0.1"
advance_origin_main "1.0.1"
PR_JSON='{"body":""}'
run_guard
assert_eq "1" "$LAST_RC" "Concurrent merge to the same target VERSION -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "Merge blocked" "Collision -> block message emitted"
assert_contains "$LAST_OUT" "#7302" "Collision -> block message references #7302"
assert_contains "$LAST_OUT" "version.sh bump patch" "Collision -> block message points at the rebase+rebump remedy"

# T3: --dry-run with the same collision as T2 -> reports the would-be block
# WITHOUT exiting 1 (dry-run contract preserved).
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
make_pr_branch "1.0.1"
advance_origin_main "1.0.1"
PR_JSON='{"body":""}'
DRY_RUN=true
run_guard
assert_eq "0" "$LAST_RC" "--dry-run + collision -> guard does NOT exit 1 (dry-run contract)"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run -> reports the would-be block"
DRY_RUN=false

# T4: PR bumps PAST current main's version (no collision) -- origin/main
# already advanced to 1.0.1 (e.g. an earlier unrelated concurrent merge), and
# the PR's own diff still bumps further, to 1.0.2. Must NOT false-positive.
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
advance_origin_main "1.0.1"
make_pr_branch "1.0.2"
PR_JSON='{"body":""}'
run_guard
assert_eq "0" "$LAST_RC" "PR bumps past current main's VERSION -> guard passes (exit 0), no false positive"
assert_not_contains "$LAST_OUT" "Merge blocked" "PR bumps past current main -> no block"

# T5: same collision as T2, but the PR body carries the
# <!-- loom:no-surface-change --> marker -- check-defaults-version-bump.sh's
# own marker exemption must still apply when re-run against current main.
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
make_pr_branch "1.0.1"
advance_origin_main "1.0.1"
PR_JSON='{"body":"some text\n<!-- loom:no-surface-change -->\nmore text"}'
run_guard
assert_eq "0" "$LAST_RC" "no-surface-change marker in PR body -> guard passes even on a would-be collision"
assert_not_contains "$LAST_OUT" "Merge blocked" "no-surface-change marker -> no block"

# T6: base already current (no concurrent merge at all, PR base IS current
# main) -- the common, non-colliding case. Must NOT false-positive (mirrors
# T1 but named for the specific "base is already current" edge case).
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
make_pr_branch "1.0.1"
PR_JSON='{"body":""}'
run_guard
assert_eq "0" "$LAST_RC" "PR's base is already current main -> guard passes (exit 0)"

# T7: default branch cannot be resolved (empty DEFAULT_BRANCH_NAME) ->
# best-effort skip, never blocks.
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME=""
make_pr_branch "1.0.1"
advance_origin_main "1.0.1"
PR_JSON='{"body":""}'
run_guard
assert_eq "0" "$LAST_RC" "Empty DEFAULT_BRANCH_NAME -> guard skips (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "Empty DEFAULT_BRANCH_NAME -> no block"

# T8: PR head SHA not reachable locally (bogus/unfetchable SHA) ->
# best-effort skip, never blocks.
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
make_pr_branch "1.0.1"
advance_origin_main "1.0.1"
PR_BRANCH="main"
PR_HEAD_SHA="0000000000000000000000000000000000dead"
PR_JSON='{"body":""}'
run_guard
assert_eq "0" "$LAST_RC" "Unreachable PR_HEAD_SHA -> guard skips (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "Unreachable PR_HEAD_SHA -> no block"

# T9: origin unreachable (fetch fails) -> best-effort skip, never blocks.
make_fixture
REPO_ROOT="$LOCAL"
DEFAULT_BRANCH_NAME="main"
make_pr_branch "1.0.1"
git -C "$LOCAL" remote set-url origin "$WORKDIR/does-not-exist"
PR_JSON='{"body":""}'
run_guard
assert_eq "0" "$LAST_RC" "Unreachable origin remote -> guard skips (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "Unreachable origin -> no block"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_check_defaults_version_bump_collision" \
  "merge-pr.sh defines and invokes _check_defaults_version_bump_collision"
assert_contains "$src" 'check_script="$REPO_ROOT/defaults/scripts/check-defaults-version-bump.sh"' \
  "merge-pr.sh re-runs the UNMODIFIED check-defaults-version-bump.sh (not a reimplementation)"
assert_contains "$src" 'git -C "$REPO_ROOT" fetch --quiet origin "$DEFAULT_BRANCH_NAME" "$PR_BRANCH"' \
  "merge-pr.sh re-checks against a freshly-fetched CURRENT default branch tip"

# Assert the guard is invoked BEFORE the auto-merge path (line ordering).
guard_line="$(grep -n '^_check_defaults_version_bump_collision$' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
automerge_line="$(grep -n '^# Handle auto-merge mode' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
if [[ -n "$guard_line" && -n "$automerge_line" && "$guard_line" -lt "$automerge_line" ]]; then
    ordered="yes"
else
    ordered="no (guard=$guard_line automerge=$automerge_line)"
fi
assert_eq "yes" "$ordered" \
  "guard is invoked before both merge paths (before '# Handle auto-merge mode')"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
