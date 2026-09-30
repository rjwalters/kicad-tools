#!/usr/bin/env bash
# test-random-file.sh — Regression tests for random-file.sh's gitignore
# handling (#6537).
#
# random-file.sh used to filter .gitignore patterns with a hand-rolled
# glob-to-regex parser (gitignore_to_regex() / filter_by_gitignore()) that
# diverged from real gitignore semantics in two ways:
#   1. A top-level directory pattern (e.g. `build/`) did not reliably exclude
#      files inside it.
#   2. `!`-negation re-inclusion lines were silently dropped.
#
# The fix replaces the hand-rolled parser with git-native handling: `fd`
# keeps its own native gitignore support (no more --no-ignore-vcs), and the
# `find` fallback path uses `git ls-files --cached --others --exclude-standard`
# / `git check-ignore` instead of a regex reimplementation.
#
# Coverage:
#   1. A top-level ignored directory's file is never selected (find fallback).
#   2. A `!`-negated re-inclusion is selected while its blanket exclusion
#      sibling is not (find fallback).
#   3. Same two cases again via the `fd` fast path, when `fd`/`fdfind` is
#      available on the host (skipped otherwise — this is exercising a
#      genuinely different code path, not a duplicate assertion).
#   4. No .gitignore present -> all files are still discoverable (regression
#      guard against the git-native path silently returning nothing).
#   5. Not inside a git repository -> falls back to plain `find` without
#      erroring.
#
# Pattern follows test-default-branch.sh: throwaway repo in a mktemp dir,
# copy the script under test, run it directly (no network, no forge).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RANDOM_FILE_SH="$SCRIPTS_DIR/random-file.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Resolve an `fd` binary (Debian/Ubuntu ships it as `fdfind`), or empty if
# unavailable. Used only to decide whether to also exercise the fd fast path.
resolve_fd() {
    if command -v fd &>/dev/null; then
        command -v fd
    elif command -v fdfind &>/dev/null; then
        command -v fdfind
    fi
}

# A PATH string with every directory that exposes a literal `fd` executable
# stripped out — everything else (git, grep, sed, mktemp, ...) stays
# reachable. This forces random-file.sh's own `command -v fd` check to miss,
# exercising the `find` fallback deterministically regardless of whether this
# host happens to have `fd` installed (as opposed to `fdfind`, which
# random-file.sh does not look for and so never changes this).
NO_FD_PATH=""
IFS=':' read -ra _path_dirs <<< "$PATH"
for _dir in "${_path_dirs[@]}"; do
    [[ -x "$_dir/fd" ]] && continue
    NO_FD_PATH+="$_dir:"
done
NO_FD_PATH="${NO_FD_PATH%:}"

# random-file.sh resolves WORKSPACE_ROOT from its OWN file location
# ($SCRIPT_DIR/../..), not from cwd — it is written to live at
# <repo-root>/defaults/scripts/random-file.sh. So a throwaway repo under
# test must carry its own copy at that same relative depth; invoking the
# real repo's copy against a `cd`'d throwaway directory would silently
# operate on the real repo tree instead (the bug this comment prevents a
# regression of).
copy_script_into() {
    local repo="$1"
    mkdir -p "$repo/defaults/scripts"
    cp "$RANDOM_FILE_SH" "$repo/defaults/scripts/random-file.sh"
    chmod +x "$repo/defaults/scripts/random-file.sh"
    echo "$repo/defaults/scripts/random-file.sh"
}

# Run random-file.sh (the copy under $1) N times and print the sorted,
# de-duplicated union of selected files (relative to $1, the repo root).
# $2.. are extra args to pass through (e.g. --include).
#
# n=100: setup_repo()'s fixture has 4 uniformly-likely candidates under
# pick_random's uniform selection, so any single candidate's per-draw miss
# probability is 3/4. At n=25 the probability that a specific candidate
# (important/keep.dat, the negated re-inclusion) is missed across the whole
# union is (3/4)^25 ~= 0.075% -- rare enough to pass locally but frequent
# enough to flake intermittently in CI (#6749). n=100 drives that down to
# (3/4)^100 ~= 3e-13, low enough to be effectively deterministic without
# weakening what the assertion actually checks (still real
# `random-file.sh` invocations, still a real union of observed output).
sample_selected() {
    local repo="$1"
    shift
    local script="$repo/defaults/scripts/random-file.sh"
    local n=100
    local i out
    local -a results=()
    for ((i = 0; i < n; i++)); do
        out=$(cd "$repo" && "$script" "$@" 2>/dev/null) || continue
        results+=("${out#"$repo"/}")
    done
    printf '%s\n' "${results[@]}" | sort -u
}

setup_repo() {
    local tmp
    tmp=$(mktemp -d /tmp/loom-randomfile.XXXXXX)
    git init -q "$tmp"
    (
        cd "$tmp" || exit 1
        git config user.email t@t
        git config user.name t
        mkdir -p build important
        echo "ignored" > build/foo.txt
        echo "kept" > keep.txt
        echo "important" > important/keep.dat
        echo "regular" > regular.dat
        cat > .gitignore << 'EOF'
build/
*.dat
!important/**/*.dat
EOF
    )
    copy_script_into "$tmp" >/dev/null
    echo "$tmp"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$repo"
}

# --- Test 1 & 2: find fallback (no fd on PATH) ---
echo "Test 1/2: find fallback — top-level dir exclusion + negation re-inclusion"
REPO=$(setup_repo)
SELECTED=$(PATH="$NO_FD_PATH" sample_selected "$REPO")
if grep -qx "build/foo.txt" <<<"$SELECTED"; then
    fail "top-level ignored directory (build/) file was selected"
else
    pass "top-level ignored directory (build/) file never selected"
fi
if grep -qx "regular.dat" <<<"$SELECTED"; then
    fail "blanket-excluded *.dat file (regular.dat) was selected"
else
    pass "blanket-excluded *.dat file (regular.dat) never selected"
fi
if grep -qx "important/keep.dat" <<<"$SELECTED"; then
    pass "negated re-inclusion (important/keep.dat) was selected"
else
    fail "negated re-inclusion (important/keep.dat) was never selected"
fi
cleanup_repo "$REPO"

# --- Test 3: same coverage via the fd fast path, if available ---
echo ""
echo "Test 3: fd fast path — top-level dir exclusion + negation re-inclusion"
FD_BIN=$(resolve_fd)
if [[ -z "$FD_BIN" ]]; then
    echo "  SKIP: no fd/fdfind binary on PATH"
else
    FD_DIR=$(mktemp -d /tmp/loom-randomfile-fdbin.XXXXXX)
    ln -s "$FD_BIN" "$FD_DIR/fd"
    REPO=$(setup_repo)
    SELECTED=$(PATH="$FD_DIR:$PATH" sample_selected "$REPO")
    if grep -q "build/foo.txt" <<<"$SELECTED"; then
        fail "fd path: top-level ignored directory (build/) file was selected"
    else
        pass "fd path: top-level ignored directory (build/) file never selected"
    fi
    if grep -q "^regular.dat$" <<<"$SELECTED"; then
        fail "fd path: blanket-excluded *.dat file (regular.dat) was selected"
    else
        pass "fd path: blanket-excluded *.dat file (regular.dat) never selected"
    fi
    if grep -q "important/keep.dat" <<<"$SELECTED"; then
        pass "fd path: negated re-inclusion (important/keep.dat) was selected"
    else
        fail "fd path: negated re-inclusion (important/keep.dat) was never selected"
    fi
    cleanup_repo "$REPO"
    rm -rf "$FD_DIR"
fi

# --- Test 4: no .gitignore present ---
echo ""
echo "Test 4: no .gitignore — all files remain discoverable"
REPO=$(mktemp -d /tmp/loom-randomfile.XXXXXX)
git init -q "$REPO"
(cd "$REPO" && git config user.email t@t && git config user.name t && echo a > a.txt && echo b > b.txt)
copy_script_into "$REPO" >/dev/null
SELECTED=$(PATH="$NO_FD_PATH" sample_selected "$REPO" --include "*.txt")
if grep -qx "a.txt" <<<"$SELECTED" && grep -qx "b.txt" <<<"$SELECTED"; then
    pass "both files discoverable with no .gitignore present"
else
    fail "expected both a.txt and b.txt, got: $SELECTED"
fi
cleanup_repo "$REPO"

# --- Test 5: not inside a git repository at all ---
echo ""
echo "Test 5: no git repository — falls back to plain find without erroring"
REPO=$(mktemp -d /tmp/loom-randomfile.XXXXXX)
echo a > "$REPO/a.txt"
SCRIPT_COPY=$(copy_script_into "$REPO")
OUT=$(cd "$REPO" && PATH="$NO_FD_PATH" "$SCRIPT_COPY" --include "*.txt" 2>&1)
RC=$?
if [[ $RC -eq 0 && "$OUT" == "$REPO/a.txt" ]]; then
    pass "non-git directory: a.txt selected without error"
else
    fail "non-git directory: expected '$REPO/a.txt' (rc=0), got rc=$RC out='$OUT'"
fi
cleanup_repo "$REPO"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
