#!/usr/bin/env bash
# test-check-main-freshness.sh - Smoke tests for check-main-freshness.sh (#3770)
#
# Unlike test-check-host-sleep.sh (which probes the live host), this harness
# constructs throwaway local git repos with a synthetic `origin` remote so it can
# deterministically exercise the three load-bearing cases:
#   (a) up-to-date  -> exit 0, no stderr warning
#   (b) behind      -> exit 0, prints the "behind" warning to stderr
#   (c) fetch fails -> exit 0 (never blocks) even when origin is unreachable
# Plus the flag/contract checks mirrored from test-check-host-sleep.sh:
#   - always exits 0
#   - --quiet suppresses the stdout one-liner
#   - --help prints usage
#   - unknown args don't break it
#
# Usage:
#   ./.loom/scripts/tests/test-check-main-freshness.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-main-freshness.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

# Scratch area for fixtures — cleaned on exit.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-freshness.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# git needs an identity in a clean CI environment.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"
# Force the default-branch helper to a known value so detection is deterministic
# regardless of the host's git init.defaultBranch config.
export LOOM_DEFAULT_BRANCH="main"

# --- fixture builder ---------------------------------------------------------
# Creates:
#   $WORKDIR/origin.git  — a bare "remote"
#   $WORKDIR/clone       — a working clone with local `main` tracking origin/main
# The clone's `main` starts at parity with origin/main. Callers then advance
# origin and/or rewind the clone to create the "behind" case.
make_fixture() {
    local origin="$WORKDIR/origin.git"
    local clone="$WORKDIR/clone"
    rm -rf "$origin" "$clone"

    git init --quiet --bare "$origin"
    # Point the bare repo's HEAD at main so `git clone` doesn't warn about a
    # nonexistent default ref (bare init defaults HEAD to refs/heads/master).
    git -C "$origin" symbolic-ref HEAD refs/heads/main >/dev/null 2>&1 || true

    # Seed the remote via a throwaway seed clone.
    local seed="$WORKDIR/seed"
    rm -rf "$seed"
    git init --quiet "$seed"
    git -C "$seed" checkout -q -b main
    echo "v1" > "$seed/file.txt"
    git -C "$seed" add file.txt
    git -C "$seed" commit -q -m "c1"
    git -C "$seed" remote add origin "$origin"
    git -C "$seed" push -q origin main

    # The clone under test.
    git clone -q "$origin" "$clone"
    git -C "$clone" checkout -q main
    # Populate refs/remotes/origin/HEAD so loom_default_branch resolves offline
    # too (belt-and-suspenders; LOOM_DEFAULT_BRANCH already forces it).
    git -C "$clone" remote set-head origin main >/dev/null 2>&1 || true
}

# Advance origin/main by one commit (simulating another PR merging mid-sweep).
advance_origin() {
    local seed="$WORKDIR/seed"
    echo "v2-$RANDOM" >> "$seed/file.txt"
    git -C "$seed" add file.txt
    git -C "$seed" commit -q -m "c2"
    git -C "$seed" push -q origin main
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-main-freshness.sh is executable"
else
    fail "check-main-freshness.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: up-to-date -> exit 0, no stderr warning --------
echo "Test 2: up-to-date case exits 0 with no warning"
make_fixture
stderr_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "up-to-date exit code is 0"
else
    fail "up-to-date expected exit 0, got $rc"
fi
if ! printf '%s' "$stderr_out" | grep -qi "behind"; then
    pass "up-to-date prints no 'behind' warning"
else
    fail "up-to-date unexpectedly warned: $stderr_out"
fi
stdout_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>/dev/null)"
if printf '%s' "$stdout_out" | grep -qi "up to date"; then
    pass "up-to-date prints an up-to-date one-liner"
else
    fail "up-to-date missing one-liner. Got: $stdout_out"
fi

# -------- Test 3: behind -> exit 0 and warns --------
echo "Test 3: behind case exits 0 and prints the warning"
make_fixture
advance_origin   # origin/main now ahead; clone hasn't fetched yet
stderr_out="$(cd "$WORKDIR/clone" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "behind exit code is 0"
else
    fail "behind expected exit 0, got $rc"
fi
if printf '%s' "$stderr_out" | grep -qi "behind"; then
    pass "behind prints the 'behind' warning to stderr"
else
    fail "behind did not warn. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q "3770"; then
    pass "behind warning references issue #3770"
else
    fail "behind warning missing #3770 reference"
fi
if printf '%s' "$stderr_out" | grep -q -- "--ff-only"; then
    pass "behind warning suggests git merge --ff-only remediation"
else
    fail "behind warning missing --ff-only remediation"
fi

# -------- Test 4: fetch failure -> still exit 0 (never blocks) --------
echo "Test 4: fetch failure still exits 0 (never blocks)"
make_fixture
advance_origin
# Point origin at an unreachable path so `git fetch` fails; the local
# refs/remotes/origin/main from clone time is still present (stale) as fallback.
git -C "$WORKDIR/clone" remote set-url origin "/nonexistent/path/repo.git"
# Run it in the clone dir and capture exit.
rc=0
( cd "$WORKDIR/clone" && "$SCRIPT" >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "fetch-failure exit code is 0"
else
    fail "fetch-failure expected exit 0, got $rc"
fi

# -------- Test 5: --quiet suppresses stdout, still exits 0 --------
echo "Test 5: --quiet suppresses stdout"
make_fixture
stdout_quiet="$(cd "$WORKDIR/clone" && "$SCRIPT" --quiet 2>/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--quiet exit code is 0"
else
    fail "--quiet exit expected 0, got $rc"
fi
if [[ -z "$stdout_quiet" ]]; then
    pass "--quiet produces no stdout"
else
    fail "--quiet produced stdout: $stdout_quiet"
fi

# -------- Test 6: --help prints usage and exits 0 --------
echo "Test 6: --help prints usage and exits 0"
help_out="$("$SCRIPT" --help 2>&1 || true)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--help exit code is 0"
else
    fail "--help exit expected 0, got $rc"
fi
if printf '%s' "$help_out" | grep -qi "Usage"; then
    pass "--help mentions Usage"
else
    fail "--help did not mention Usage. Got: $help_out"
fi

# -------- Test 7: unknown args do not break it --------
echo "Test 7: unknown args do not break the script"
make_fixture
rc=0
( cd "$WORKDIR/clone" && "$SCRIPT" --some-nonsense-flag --another 99 >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "unknown args tolerated; exit 0"
else
    fail "unknown args caused non-zero exit ($rc)"
fi

# -------- Test 8: outside a git repo -> exit 0, skip gracefully --------
echo "Test 8: outside a git repo exits 0"
non_git="$WORKDIR/not-a-repo"
mkdir -p "$non_git"
rc=0
( cd "$non_git" && "$SCRIPT" >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "non-git dir exit code is 0"
else
    fail "non-git dir expected exit 0, got $rc"
fi

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
