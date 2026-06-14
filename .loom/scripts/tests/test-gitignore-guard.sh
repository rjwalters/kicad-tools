#!/usr/bin/env bash
# test-gitignore-guard.sh - Unit tests for the gitignore-guard block in
# scripts/install-loom.sh (issue #3326).
#
# The guard runs after install metadata has been written and checks that
# none of the just-installed .loom/scripts/lib/*.sh files are matched by
# a .gitignore rule. When any are matched, the guard must:
#
#   1. Use `git check-ignore -v` to print the offending file along with
#      the originating `<gitignore-file>:<line>:<pattern>` triple.
#   2. Emit a context-aware "Suggested fix" message:
#        - unanchored single-segment dir (e.g. `lib/`) -> anchor it (`/lib/`)
#        - .loom-shaped pattern (e.g. `.loom/`, `.loom*`) -> remove it
#        - anything else -> generic "remove or narrow" guidance
#   3. Exit non-zero so the installer refuses to proceed.
#
# Test strategy: create a temp git repo, drop the relevant fake files,
# write a .gitignore with the pattern under test, then run *only* the
# guard block (extracted from install-loom.sh via sed) against the temp
# repo. Assert on stderr content and the resulting exit code.
#
# Usage:
#   bash .loom/scripts/tests/test-gitignore-guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/scripts/install-loom.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Looking for: '$needle'"
        echo "    In output:"
        echo "$haystack" | sed 's/^/      /'
    fi
}

assert_nonzero_exit() {
    local actual="$1"
    local msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" -ne 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (exit=$actual)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected non-zero, got $actual)"
    fi
}

if [[ ! -f "$INSTALL_SCRIPT" ]]; then
    echo "ERROR: $INSTALL_SCRIPT not found" >&2
    exit 1
fi

# Build a runnable harness once: helper functions + the gitignore-guard
# block, extracted verbatim from install-loom.sh so the test exercises the
# real production code path (not a re-implementation).
HARNESS_FILE=$(mktemp /tmp/loom-gitignore-guard-harness.XXXXXX.sh)
trap 'rm -f "$HARNESS_FILE"' EXIT

# Extract the guard block (the conditional starting at the lib-dir check).
# Markers: `if [[ -d ".loom/scripts/lib" ]]; then` ... matching `fi` (we
# capture until the line that reads `success "lib/*.sh files are not
# gitignored"` followed by `echo ""` and `fi`).
GUARD_BLOCK=$(awk '
    /^if \[\[ -d "\.loom\/scripts\/lib" \]\]; then$/ { capture=1 }
    capture { print }
    capture && /^fi$/ { exit }
' "$INSTALL_SCRIPT")

if [[ -z "$GUARD_BLOCK" ]]; then
    echo "ERROR: could not extract gitignore-guard block from $INSTALL_SCRIPT" >&2
    exit 1
fi

cat >"$HARNESS_FILE" <<'PROLOGUE'
#!/usr/bin/env bash
set -uo pipefail
# Stub helpers so the extracted block runs standalone.
RED=''; GREEN=''; BLUE=''; YELLOW=''; CYAN=''; NC=''
error() { echo "Error: $*" >&2; exit 1; }
info()    { echo "$*"; }
success() { echo "$*"; }
warning() { echo "Warning: $*" >&2; }
PROLOGUE

printf '%s\n' "$GUARD_BLOCK" >>"$HARNESS_FILE"

# Helper: run the harness inside a fresh git repo and capture stderr + exit.
run_guard_in_repo() {
    local gitignore_content="$1"
    local tmp
    tmp=$(mktemp -d /tmp/loom-gitignore-guard-test.XXXXXX)
    (
        cd "$tmp" || exit 1
        git init -q .
        git config user.email "test@example.com"
        git config user.name "Test"
        mkdir -p .loom/scripts/lib
        : > .loom/scripts/lib/classify-error.sh
        : > .loom/scripts/lib/forge-helpers.sh
        printf '%s\n' "$gitignore_content" > .gitignore
        bash "$HARNESS_FILE" 2>&1
        echo "__EXIT__=$?"
    )
    rm -rf "$tmp"
}

# ---------------------------------------------------------------------------
# Case 1: unanchored single-segment dir `lib/` should suggest `/lib/`.
# ---------------------------------------------------------------------------
echo "Case 1: unanchored 'lib/' -> anchor suggestion"
OUTPUT=$(run_guard_in_repo $'# pad line 1\nlib/\n')
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
OUTPUT_CLEAN=$(printf '%s' "$OUTPUT" | grep -v '^__EXIT__=')

assert_contains "$OUTPUT_CLEAN" ".loom/scripts/lib/classify-error.sh" "Case 1: lists offending file"
assert_contains "$OUTPUT_CLEAN" ".gitignore:2 pattern 'lib/'" "Case 1: shows gitignore:line pattern"
assert_contains "$OUTPUT_CLEAN" "Suggested fix: anchor the pattern" "Case 1: suggests anchoring"
assert_contains "$OUTPUT_CLEAN" "Change line 2 of .gitignore from:  lib/" "Case 1: shows before line"
assert_contains "$OUTPUT_CLEAN" "/lib/" "Case 1: shows after pattern with leading slash"
assert_nonzero_exit "$EXIT_CODE" "Case 1: exits non-zero"
echo ""

# ---------------------------------------------------------------------------
# Case 2: `.loom/` pattern -> suggest deletion, not anchoring.
# ---------------------------------------------------------------------------
echo "Case 2: '.loom/' -> remove suggestion"
OUTPUT=$(run_guard_in_repo $'.loom/\n')
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
OUTPUT_CLEAN=$(printf '%s' "$OUTPUT" | grep -v '^__EXIT__=')

assert_contains "$OUTPUT_CLEAN" ".gitignore:1 pattern '.loom/'" "Case 2: shows gitignore:line pattern"
assert_contains "$OUTPUT_CLEAN" "Suggested fix: remove this pattern" "Case 2: suggests removal"
assert_contains "$OUTPUT_CLEAN" "Loom's working directory" "Case 2: explains why removal is needed"
# Must NOT suggest anchoring `.loom/` to `/.loom/` — that would still hide files.
if [[ "$OUTPUT_CLEAN" == *"anchor the pattern"* ]]; then
    echo -e "  ${RED}FAIL${NC}: Case 2: should not suggest anchoring for .loom/"
    TESTS_FAILED=$((TESTS_FAILED + 1))
else
    echo -e "  ${GREEN}PASS${NC}: Case 2: does not suggest anchoring for .loom/"
    TESTS_PASSED=$((TESTS_PASSED + 1))
fi
TESTS_RUN=$((TESTS_RUN + 1))
assert_nonzero_exit "$EXIT_CODE" "Case 2: exits non-zero"
echo ""

# ---------------------------------------------------------------------------
# Case 3: exotic pattern (e.g. `**/lib/`) -> generic remove/narrow guidance.
# ---------------------------------------------------------------------------
echo "Case 3: exotic '**/lib/' -> generic suggestion"
OUTPUT=$(run_guard_in_repo $'**/lib/\n')
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
OUTPUT_CLEAN=$(printf '%s' "$OUTPUT" | grep -v '^__EXIT__=')

assert_contains "$OUTPUT_CLEAN" "pattern '**/lib/'" "Case 3: shows pattern in match line"
assert_contains "$OUTPUT_CLEAN" "Suggested fix: remove or narrow the pattern '**/lib/'" "Case 3: generic narrow guidance"
assert_nonzero_exit "$EXIT_CODE" "Case 3: exits non-zero"
echo ""

# ---------------------------------------------------------------------------
# Case 4: no offending pattern -> success path, exit 0.
# ---------------------------------------------------------------------------
echo "Case 4: benign .gitignore -> success path"
OUTPUT=$(run_guard_in_repo $'# nothing to see here\nbuild/\n')
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
OUTPUT_CLEAN=$(printf '%s' "$OUTPUT" | grep -v '^__EXIT__=')

assert_contains "$OUTPUT_CLEAN" "lib/*.sh files are not gitignored" "Case 4: success message printed"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$EXIT_CODE" == "0" ]]; then
    echo -e "  ${GREEN}PASS${NC}: Case 4: exits 0 when no conflict"
    TESTS_PASSED=$((TESTS_PASSED + 1))
else
    echo -e "  ${RED}FAIL${NC}: Case 4: expected exit 0, got $EXIT_CODE"
    TESTS_FAILED=$((TESTS_FAILED + 1))
fi
echo ""

# ---------------------------------------------------------------------------
# Case 5: multiple lib files matched -> each is listed with its triple.
# ---------------------------------------------------------------------------
echo "Case 5: multiple matched lib files all listed"
OUTPUT=$(run_guard_in_repo $'# pad\nlib/\n')
OUTPUT_CLEAN=$(printf '%s' "$OUTPUT" | grep -v '^__EXIT__=')

assert_contains "$OUTPUT_CLEAN" "classify-error.sh" "Case 5: first lib file listed"
assert_contains "$OUTPUT_CLEAN" "forge-helpers.sh" "Case 5: second lib file listed"
echo ""

# --- Summary ---
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
