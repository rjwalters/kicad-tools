#!/usr/bin/env bash
# test-check-quarantine-stashes.sh - Smoke tests for check-quarantine-stashes.sh (#5185)
#
# Constructs a throwaway local git repo so it can deterministically exercise:
#   (a) no stashes at all         -> exit 0, no warning, "no outstanding" one-liner
#   (b) a stash with an unrelated message -> exit 0, no warning (not a quarantine)
#   (c) one or more loom-quarantine: stashes -> exit 0, warns, lists each with a
#       copy-pasteable `stash@{N}` selector (not a date-corrupted one)
#   (d) outside a git repo        -> exit 0 (never blocks)
# Plus the flag/contract checks mirrored from test-check-main-freshness.sh:
#   - always exits 0
#   - --quiet suppresses the stdout one-liner
#   - --help prints usage
#   - unknown args don't break it
#
# Usage:
#   ./.loom/scripts/tests/test-check-quarantine-stashes.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-quarantine-stashes.sh"

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
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-quarantine-stashes.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# git needs an identity in a clean CI environment.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder ---------------------------------------------------------
# Creates $WORKDIR/repo — a plain local repo with one commit on main, ready
# for stashes to be layered on top by each test.
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    git init --quiet "$repo"
    git -C "$repo" checkout -q -b main
    echo "v1" > "$repo/file.txt"
    git -C "$repo" add file.txt
    git -C "$repo" commit -q -m "c1"
}

# Push a stash entry onto refs/stash with an explicit message, via a dirty
# working-tree edit (git stash push requires something to stash).
push_stash() {
    local repo="$WORKDIR/repo" msg="$1" content="$2"
    echo "$content" >> "$repo/file.txt"
    git -C "$repo" stash push -q -m "$msg"
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-quarantine-stashes.sh is executable"
else
    fail "check-quarantine-stashes.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: no stashes at all -> exit 0, no warning --------
echo "Test 2: no stashes at all exits 0 with no warning"
make_fixture
stderr_out="$(cd "$WORKDIR/repo" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "no-stashes exit code is 0"
else
    fail "no-stashes expected exit 0, got $rc"
fi
if ! printf '%s' "$stderr_out" | grep -qi "WARNING"; then
    pass "no-stashes prints no warning"
else
    fail "no-stashes unexpectedly warned: $stderr_out"
fi
stdout_out="$(cd "$WORKDIR/repo" && "$SCRIPT" 2>/dev/null)"
if printf '%s' "$stdout_out" | grep -qi "no outstanding"; then
    pass "no-stashes prints the 'no outstanding' one-liner"
else
    fail "no-stashes missing one-liner. Got: $stdout_out"
fi

# -------- Test 3: an unrelated stash -> exit 0, no warning --------
echo "Test 3: unrelated stash message does not trigger a warning"
make_fixture
push_stash "WIP on main: unrelated scratch edit" "unrelated-$RANDOM"
stderr_out="$(cd "$WORKDIR/repo" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "unrelated-stash exit code is 0"
else
    fail "unrelated-stash expected exit 0, got $rc"
fi
if ! printf '%s' "$stderr_out" | grep -qi "WARNING"; then
    pass "unrelated-stash prints no warning"
else
    fail "unrelated-stash unexpectedly warned: $stderr_out"
fi

# -------- Test 4: a loom-quarantine: stash -> exit 0, warns --------
echo "Test 4: a loom-quarantine: stash triggers a warning"
make_fixture
push_stash "loom-quarantine: run=sweep-test-1 issue=999" "contamination-$RANDOM"
stderr_out="$(cd "$WORKDIR/repo" && "$SCRIPT" 2>&1 >/dev/null)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "quarantine-stash exit code is 0"
else
    fail "quarantine-stash expected exit 0, got $rc"
fi
if printf '%s' "$stderr_out" | grep -qi "WARNING"; then
    pass "quarantine-stash prints a warning"
else
    fail "quarantine-stash did not warn. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q "5185"; then
    pass "quarantine-stash warning references issue #5185"
else
    fail "quarantine-stash warning missing #5185 reference"
fi
if printf '%s' "$stderr_out" | grep -q "issue=999"; then
    pass "quarantine-stash warning names the quarantined issue label"
else
    fail "quarantine-stash warning missing the issue label. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -qE 'stash@\{[0-9]+\}'; then
    pass "quarantine-stash warning lists a copy-pasteable stash@{N} selector"
else
    fail "quarantine-stash warning missing a valid stash@{N} selector (date-corrupted #gd?). Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q -- "git stash show -p"; then
    pass "quarantine-stash warning suggests git stash show -p remediation"
else
    fail "quarantine-stash warning missing git stash show -p remediation"
fi
stdout_out="$(cd "$WORKDIR/repo" && "$SCRIPT" 2>/dev/null)"
if printf '%s' "$stdout_out" | grep -qi "1 outstanding"; then
    pass "quarantine-stash one-liner reports the count"
else
    fail "quarantine-stash one-liner missing count. Got: $stdout_out"
fi

# -------- Test 5: multiple loom-quarantine: stashes -> all listed --------
echo "Test 5: multiple quarantine stashes are all listed"
make_fixture
push_stash "loom-quarantine: issue=100" "c1-$RANDOM"
push_stash "loom-quarantine: run=sweep-test-2 issue=200" "c2-$RANDOM"
push_stash "WIP on main: unrelated" "c3-$RANDOM"
stderr_out="$(cd "$WORKDIR/repo" && "$SCRIPT" 2>&1 >/dev/null)"
if printf '%s' "$stderr_out" | grep -q "issue=100" && printf '%s' "$stderr_out" | grep -q "issue=200"; then
    pass "both quarantine stashes are listed"
else
    fail "not all quarantine stashes listed. Got: $stderr_out"
fi
if printf '%s' "$stderr_out" | grep -q "2 outstanding"; then
    pass "count reflects only the quarantine stashes, not the unrelated one"
else
    fail "count did not correctly exclude the unrelated stash. Got: $stderr_out"
fi

# -------- Test 6: --quiet suppresses the stdout one-liner --------
echo "Test 6: --quiet suppresses stdout"
make_fixture
push_stash "loom-quarantine: issue=1" "c-$RANDOM"
stdout_out="$(cd "$WORKDIR/repo" && "$SCRIPT" --quiet 2>/dev/null)"
if [[ -z "$stdout_out" ]]; then
    pass "--quiet produces no stdout"
else
    fail "--quiet unexpectedly produced stdout: $stdout_out"
fi
stderr_out="$(cd "$WORKDIR/repo" && "$SCRIPT" --quiet 2>&1 >/dev/null)"
if printf '%s' "$stderr_out" | grep -qi "WARNING"; then
    pass "--quiet still warns on stderr"
else
    fail "--quiet suppressed the stderr warning too (should not)"
fi

# -------- Test 7: --help prints usage --------
echo "Test 7: --help prints usage and exits 0"
help_out="$("$SCRIPT" --help 2>&1)"
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

# -------- Test 8: unknown args do not break it --------
echo "Test 8: unknown args do not break the script"
make_fixture
rc=0
( cd "$WORKDIR/repo" && "$SCRIPT" --some-nonsense-flag --another 99 >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "unknown args tolerated; exit 0"
else
    fail "unknown args caused non-zero exit ($rc)"
fi

# -------- Test 9: outside a git repo -> exit 0, skip gracefully --------
echo "Test 9: outside a git repo exits 0"
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
