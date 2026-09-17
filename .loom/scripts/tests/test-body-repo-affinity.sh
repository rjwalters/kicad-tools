#!/usr/bin/env bash
# test-body-repo-affinity.sh - Unit tests for body-repo-affinity.sh (#6771),
# the detection backstop for cross-repo issue-body corruption deferred from
# #6714's fleet-wide issue-filing lock.
#
# This is a black-box test: body-repo-affinity.sh is a full CLI script, so we
# build small throwaway git repos under a temp dir and invoke the real script
# as a subprocess, asserting on stdout/stderr/exit code. Fully hermetic -- no
# network calls, no `gh`.
#
# Fixtures:
#   1. Regression: the actual 2026-08-08 shape -- an SRAM-macro repo body
#      cites `rtl/modexp.v`, `spec/modexp.md`, and `sky130_fd_sc_hd`, none of
#      which exist in the target repo -- MUST be flagged (exit 1).
#   2. Negative: a forward-looking issue proposing new files that also
#      references something that already exists in the target repo -- MUST
#      NOT be flagged (exit 0).
#   3. Below-threshold: fewer than --min-candidates cited identifiers is
#      never evaluated, regardless of resolution -- MUST NOT be flagged.
#   4. A body that genuinely matches the repo it's filed into (all cited
#      paths exist) -- MUST NOT be flagged.
#
# Usage:
#   ./.loom/scripts/tests/test-body-repo-affinity.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
BRA="$SCRIPTS_DIR/lib/body-repo-affinity.sh"

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

# assert_contains/assert_not_contains use a pure-bash substring match (no
# forked printf|grep pipeline) so a transient fork/exec failure under
# run-ci-suites.sh's parallel suite pool can never masquerade as a genuine
# content mismatch (#7819, #7874).
assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Fixture setup: two throwaway git repos under a temp dir -----------------

WORK=""
cleanup() {
    [[ -n "$WORK" && -d "$WORK" ]] && rm -rf "$WORK"
}
trap cleanup EXIT

WORK="$(mktemp -d)"

# "sram-repo": the target repo in the regression fixture. Contains nothing
# related to modular exponentiation -- exactly the 2026-08-08 incident shape.
SRAM_REPO="$WORK/sram-repo"
mkdir -p "$SRAM_REPO/src"
(
    cd "$SRAM_REPO" || exit 1
    git init -q
    git config user.email "test@example.com"
    git config user.name "Test"
    printf 'module sram_macro();\nendmodule\n' > src/sram_macro.v
    git add -A
    git commit -q -m "init"
)

# "app-repo": a second repo, used for the "genuinely matches" fixture and as
# the existing-context anchor for the forward-looking negative fixture.
APP_REPO="$WORK/app-repo"
mkdir -p "$APP_REPO/src" "$APP_REPO/tests"
(
    cd "$APP_REPO" || exit 1
    git init -q
    git config user.email "test@example.com"
    git config user.name "Test"
    printf 'def main():\n    pass\n' > src/main.py
    printf 'def test_main():\n    pass\n' > tests/test_main.py
    git add -A
    git commit -q -m "init"
)

echo "=== body-repo-affinity.sh tests ==="
echo

# --- Test 1: Regression fixture (2026-08-08 shape) --------------------------
echo "--- Regression fixture: cross-repo body mismatch ---"
REGRESSION_BODY='Add support for the modular exponentiation core. See `rtl/modexp.v` for the RTL and `spec/modexp.md` for the spec. This needs OpenROAD digital P&R using `sky130_fd_sc_hd` as the standard cell library.'

set +e
OUT="$("$BRA" --body "$REGRESSION_BODY" --repo-root "$SRAM_REPO" 2>&1)"
RC=$?
set -e 2>/dev/null || true

assert_eq "1" "$RC" "regression fixture: flagged (exit 1)"
assert_contains "$OUT" "rtl/modexp.v" "regression fixture: warning names rtl/modexp.v"
assert_contains "$OUT" "spec/modexp.md" "regression fixture: warning names spec/modexp.md"
assert_contains "$OUT" "sky130_fd_sc_hd" "regression fixture: warning names sky130_fd_sc_hd"
assert_contains "$OUT" "WARNING" "regression fixture: warning text present"

echo

# --- Test 2: Negative fixture (forward-looking, references existing file) ---
echo "--- Negative fixture: forward-looking issue, not flagged ---"
NEGATIVE_BODY='Add a new `src/feature_x.py` module implementing the parser, wired into the existing `src/main.py` entry point, with tests in `tests/test_feature_x.py`.'

set +e
"$BRA" --body "$NEGATIVE_BODY" --repo-root "$APP_REPO" >/dev/null 2>&1
RC2=$?
set -e 2>/dev/null || true

assert_eq "0" "$RC2" "negative fixture: NOT flagged (exit 0)"

echo

# --- Test 3: Below candidate threshold -> never evaluated -------------------
echo "--- Below-threshold: fewer than --min-candidates never flags ---"
SHORT_BODY='Please also update `rtl/modexp.v` while you are in there.'

set +e
"$BRA" --body "$SHORT_BODY" --repo-root "$SRAM_REPO" >/dev/null 2>&1
RC3=$?
set -e 2>/dev/null || true

assert_eq "0" "$RC3" "below-threshold fixture: NOT flagged (exit 0)"

echo

# --- Test 4: Body genuinely matches the repo it's filed into ----------------
echo "--- Genuine match: cited identifiers exist in target repo ---"
MATCH_BODY='`src/main.py` needs a refactor; see `tests/test_main.py` and also touches `src/utils.py` (not yet created).'

set +e
"$BRA" --body "$MATCH_BODY" --repo-root "$APP_REPO" >/dev/null 2>&1
RC4=$?
set -e 2>/dev/null || true

assert_eq "0" "$RC4" "genuine-match fixture: NOT flagged (exit 0)"

echo

# --- Test 5: Invalid --repo-root is a hard usage error, not a silent pass ---
echo "--- Invalid --repo-root: usage error ---"
set +e
"$BRA" --body "$REGRESSION_BODY" --repo-root "$WORK/does-not-exist" >/dev/null 2>&1
RC5=$?
set -e 2>/dev/null || true

assert_eq "2" "$RC5" "invalid repo-root: exit 2 (usage error)"

echo

# --- Summary ------------------------------------------------------------
echo "=== Results: $TESTS_PASSED/$TESTS_RUN passed ==="
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
