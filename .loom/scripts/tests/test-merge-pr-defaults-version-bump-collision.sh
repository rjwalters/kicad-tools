#!/usr/bin/env bash
# Behavioral regression tests for merge-time no-hand-bump policy (#7827).
# Real local origins/clones exercise fetch, ancestry, and the canonical
# checker without mocking Git or version extraction.
# The historical filename/function remain stable for test discovery.
#
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

echo "Testing merge-time version policy (#7827)..."

# Real repositories exercise both the helper fetch and the canonical checker.
case_fixture() {
    make_fixture
    REPO_ROOT="$LOCAL"
    DEFAULT_BRANCH_NAME="main"
    PR_JSON='{"body":""}'
    DRY_RUN=false
}

case_fixture
make_pr_branch "1.0.0"
run_guard
assert_eq "0" "$LAST_RC" "Installed behavior change without manual bump merges"

case_fixture
make_pr_branch "1.0.1"
run_guard
assert_eq "1" "$LAST_RC" "Manual VERSION edit blocks even before main advances"
assert_contains "$LAST_OUT" "hand-edit" "Block identifies the actual policy violation"
assert_not_contains "$LAST_OUT" "version.sh bump patch" "Remedy never recommends a forbidden bump"

case_fixture
make_pr_branch "1.0.1"
PR_JSON='{"body":"<!-- loom:no-surface-change -->"}'
run_guard
assert_eq "1" "$LAST_RC" "No-surface marker cannot waive manual version edits"
DRY_RUN=true
run_guard
assert_eq "0" "$LAST_RC" "Dry-run reports without blocking"
assert_contains "$LAST_OUT" "Would BLOCK" "Dry-run reports forbidden edit"

case_fixture
make_pr_branch "1.0.0"
advance_origin_main "1.0.1"
run_guard
assert_eq "0" "$LAST_RC" "Concurrent automated bump does not invalidate defaults PR"

case_fixture
git -C "$LOCAL" checkout -q -b feature/unrelated
printf 'Rust-only change\n' > "$LOCAL/code.rs"
git -C "$LOCAL" add code.rs
git -C "$LOCAL" commit -q -m 'non-defaults PR'
git -C "$LOCAL" push --quiet origin feature/unrelated
PR_BRANCH=feature/unrelated
PR_HEAD_SHA="$(git -C "$LOCAL" rev-parse HEAD)"
advance_origin_main "1.0.1"
run_guard
assert_eq "0" "$LAST_RC" "Main-only defaults/version drift does not block non-defaults PR"

case_fixture
make_pr_branch "1.0.1"
advance_origin_main "1.0.1"
run_guard
assert_eq "1" "$LAST_RC" "Matching main version cannot hide a manual PR edit"

case_fixture
make_pr_branch "0.9.0"
run_guard
assert_eq "1" "$LAST_RC" "Manual version downgrade also blocks"

case_fixture
make_pr_branch "1.0.0"
git -C "$LOCAL" checkout -q --orphan unrelated-history
git -C "$LOCAL" commit -q -m 'disconnected history'
git -C "$LOCAL" push --quiet origin unrelated-history
PR_BRANCH=unrelated-history
PR_HEAD_SHA="$(git -C "$LOCAL" rev-parse HEAD)"
run_guard
assert_eq "0" "$LAST_RC" "Unresolvable ancestry keeps best-effort skip contract"
assert_contains "$LAST_OUT" "ancestry" "Unknown ancestry is diagnosed, not treated as a version edit"

case_fixture
make_pr_branch "1.0.1"
DEFAULT_BRANCH_NAME=""
run_guard
assert_eq "0" "$LAST_RC" "Unknown default branch keeps skip contract"
DEFAULT_BRANCH_NAME=main
PR_HEAD_SHA=000000000000000000000000000000000000dead
run_guard
assert_eq "0" "$LAST_RC" "Unknown head keeps skip contract"
git -C "$LOCAL" remote set-url origin "$WORKDIR/missing"
run_guard
assert_eq "0" "$LAST_RC" "Failed fetch keeps skip contract"

# Non-defaults edits of each other canonical value are forbidden too.
# CLAUDE.md is deliberately absent: #8147 removed it from the version-bearing
# set (it is injected into every agent session's prompt prefix, so it carries
# no version stamp at all any more) -- the case below asserts the inverse.
for file in package.json mcp-loom/package.json Cargo.toml; do
    case_fixture
    git -C "$LOCAL" checkout -q -b feature/value
    mkdir -p "$(dirname "$LOCAL/$file")"
    case "$file" in
        *.json) printf '{"version":"2.0.0"}\n' > "$LOCAL/$file" ;;
        *.toml) printf 'version = "2.0.0"\n' > "$LOCAL/$file" ;;
    esac
    git -C "$LOCAL" add "$file"
    git -C "$LOCAL" commit -q -m 'manual version value'
    git -C "$LOCAL" push --quiet origin feature/value
    PR_BRANCH=feature/value
    PR_HEAD_SHA="$(git -C "$LOCAL" rev-parse HEAD)"
    PR_JSON='{"body":"<!-- loom:no-surface-change -->"}'
    run_guard
    assert_eq 1 "$LAST_RC" "$file value edit blocks without defaults change, even with marker"
done

case_fixture
git -C "$LOCAL" checkout -q -b feature/prose
printf 'Documentation only\n' > "$LOCAL/CLAUDE.md"
git -C "$LOCAL" add CLAUDE.md
git -C "$LOCAL" commit -q -m 'prose only'
git -C "$LOCAL" push --quiet origin feature/prose
PR_BRANCH=feature/prose
PR_HEAD_SHA="$(git -C "$LOCAL" rev-parse HEAD)"
run_guard
assert_eq 0 "$LAST_RC" "Prose in version-bearing file passes with unchanged value"

# #8147: a CLAUDE.md edit that DOES rewrite a `**Loom Version**:` line is no
# longer a hand-edit at all -- the file left the version-bearing set with the
# stamp. Without this the PR that removed the stamp could not have merged.
case_fixture
git -C "$LOCAL" checkout -q -b feature/claude-version-line
printf '**Loom Version**: 2.0.0\n' > "$LOCAL/CLAUDE.md"
git -C "$LOCAL" add CLAUDE.md
git -C "$LOCAL" commit -q -m 'CLAUDE.md version-looking line'
git -C "$LOCAL" push --quiet origin feature/claude-version-line
PR_BRANCH=feature/claude-version-line
PR_HEAD_SHA="$(git -C "$LOCAL" rev-parse HEAD)"
run_guard
assert_eq 0 "$LAST_RC" "CLAUDE.md is no longer version-bearing (#8147)"

case_fixture
make_pr_branch "1.0.0"
printf '#!/bin/sh\nexit 2\n' > "$LOCAL/defaults/scripts/check-defaults-version-bump.sh"
run_guard
assert_eq 0 "$LAST_RC" "Checker usage/internal fault retains skip contract"
assert_contains "$LAST_OUT" "exited 2" "Internal checker failure is diagnosed"

# --- #8284: which ref's checker is the oracle -------------------------------
#
# merge-pr.sh runs from a primary checkout sitting on the DEFAULT BRANCH, so
# $REPO_ROOT's on-disk checker is main's copy while the PR's own copy exists
# only as a git object. Every case below depends on that distinction, which
# make_pr_branch deliberately does not model (it leaves the local checkout on
# the PR branch), so these build the branch and then return to main.
start_pr_branch() {
    git -C "$LOCAL" fetch --quiet origin
    git -C "$LOCAL" checkout -q -B "$1" "origin/main"
}
finish_pr_branch() {
    git -C "$LOCAL" add -A
    git -C "$LOCAL" commit -q -m "$2"
    git -C "$LOCAL" push --quiet origin "$1"
    PR_BRANCH="$1"
    PR_HEAD_SHA="$(git -C "$LOCAL" rev-parse HEAD)"
    git -C "$LOCAL" checkout -q main
}

# Drops "VERSION" from the working copy's version-bearing set, mirroring what
# #8190 did to CLAUDE.md. Rewrites via a temp file so the edit works the same
# on BSD and GNU userlands (no `sed -i` portability split).
shrink_version_bearing_set() {
    grep -v '^  "VERSION"$' "$LOCAL/defaults/scripts/check-defaults-version-bump.sh" > "$WORKDIR/checker.new"
    mv "$WORKDIR/checker.new" "$LOCAL/defaults/scripts/check-defaults-version-bump.sh"
    chmod +x "$LOCAL/defaults/scripts/check-defaults-version-bump.sh"
}

# The #8190 shape: the PR removes a file from the version-bearing set AND
# changes that file's value in the same commit. main's checker still encodes the
# old set and reports a hand-edit; the PR head's checker (what CI runs) does
# not. Without the head-ref oracle such a PR can never pass this guard.
case_fixture
start_pr_branch feature/shrink-version-set
shrink_version_bearing_set
echo "9.9.9" > "$LOCAL/VERSION"
finish_pr_branch feature/shrink-version-set 'drop VERSION from the version-bearing set'
run_guard
assert_eq 0 "$LAST_RC" "PR that shrinks the version-bearing set is judged by its own checker (#8284)"
assert_contains "$LAST_OUT" "the PR head ($PR_HEAD_SHA)" "Guard names the PR head as the oracle it used"

# The head oracle is not a bypass: touching the machinery does not excuse a
# hand-bump the head's OWN checker still forbids (VERSION stays in its set).
case_fixture
start_pr_branch feature/touch-version-sh
mkdir -p "$LOCAL/scripts"
printf '#!/usr/bin/env bash\n# unrelated edit to the version helper\n' > "$LOCAL/scripts/version.sh"
echo "1.0.1" > "$LOCAL/VERSION"
finish_pr_branch feature/touch-version-sh 'touch scripts/version.sh and hand-bump VERSION'
run_guard
assert_eq 1 "$LAST_RC" "PR head's own checker still blocks a hand-bump it forbids (#8284)"
assert_contains "$LAST_OUT" "hand-edit" "Head-oracle block still identifies the policy violation"

# Fail closed on a lookup error: a PR that deletes the checker at its head
# touches the machinery, so the head oracle is attempted, but `git show` finds
# nothing. That must fall back to main's checker (which blocks the hand-bump),
# never silently pass.
case_fixture
start_pr_branch feature/delete-checker
git -C "$LOCAL" rm -q defaults/scripts/check-defaults-version-bump.sh
echo "1.0.1" > "$LOCAL/VERSION"
finish_pr_branch feature/delete-checker 'delete the checker and hand-bump VERSION'
run_guard
assert_eq 1 "$LAST_RC" "Unreadable PR-head checker falls back to main's, not a free pass (#8284)"
assert_contains "$LAST_OUT" "'main' (" "Fallback names the default branch as the oracle actually used"

# Unchanged for every other PR: main's checker, no mention of the head oracle.
case_fixture
make_pr_branch "1.0.1"
run_guard
assert_eq 1 "$LAST_RC" "Ordinary hand-bump still blocks under main's checker"
assert_not_contains "$LAST_OUT" "version-policy machinery" "Ordinary PR keeps main's checker as the oracle"

# Base-branch drift must not flip the oracle: the machinery-touch test is scoped
# to merge-base..head, so a concurrent merge that touches scripts/version.sh on
# main leaves this PR (which touches nothing of the sort) on main's checker.
case_fixture
start_pr_branch feature/untouched-machinery
echo "changed by PR" >> "$LOCAL/defaults/scripts/foo.md"
finish_pr_branch feature/untouched-machinery 'ordinary defaults change'
mkdir -p "$ORIGIN/scripts"
printf '#!/usr/bin/env bash\n# concurrent edit on main\n' > "$ORIGIN/scripts/version.sh"
git -C "$ORIGIN" add -A
git -C "$ORIGIN" commit -q -m 'concurrent machinery change on main'
run_guard
assert_eq 0 "$LAST_RC" "Concurrent machinery change on main does not block the PR"
assert_not_contains "$LAST_OUT" "version-policy machinery" "Main-side machinery drift does not flip the oracle"

# The guard must remain before either merge path.
guard_line="$(grep -n '^_check_defaults_version_bump_collision$' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
automerge_line="$(grep -n '^# Handle auto-merge mode' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
assert_eq yes "$( [[ "$guard_line" -lt "$automerge_line" ]] && echo yes || echo no )" "Guard precedes merge paths"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
