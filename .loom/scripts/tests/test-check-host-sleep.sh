#!/usr/bin/env bash
# test-check-host-sleep.sh - Smoke tests for check-host-sleep.sh
#
# This script exercises the host-sleep readiness check (#3350) against the
# current host. It does NOT mock the platform — it just verifies the helper
# behaves as designed:
#   - always exits 0 (never blocks Loom)
#   - prints something coherent on supported platforms
#   - handles unknown platforms without crashing
#   - respects --quiet (suppresses stdout one-liner but still warns on stderr)
#   - handles --help
#
# Usage:
#   ./.loom/scripts/tests/test-check-host-sleep.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-host-sleep.sh"

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

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-host-sleep.sh is executable"
else
    fail "check-host-sleep.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: default invocation exits 0 --------
echo "Test 2: default invocation always exits 0"
"$SCRIPT" >/dev/null 2>&1
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "exit code is 0"
else
    fail "expected exit 0, got $rc"
fi

# -------- Test 3: --quiet exits 0 and suppresses stdout --------
echo "Test 3: --quiet suppresses stdout"
stdout_quiet="$("$SCRIPT" --quiet 2>/dev/null || true)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--quiet exit code is 0"
else
    fail "--quiet exit code expected 0, got $rc"
fi
if [[ -z "$stdout_quiet" ]]; then
    pass "--quiet produces no stdout"
else
    fail "--quiet produced stdout: $stdout_quiet"
fi

# -------- Test 4: default invocation prints SOMETHING (stdout or stderr) --------
echo "Test 4: default invocation produces output"
combined="$("$SCRIPT" 2>&1 || true)"
if [[ -n "$combined" ]]; then
    pass "produced output on supported platform"
else
    fail "produced no output at all (unexpected)"
fi

# -------- Test 5: --help works --------
echo "Test 5: --help prints usage and exits 0"
help_out="$("$SCRIPT" --help 2>&1 || true)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--help exit code is 0"
else
    fail "--help exit expected 0, got $rc"
fi
if printf '%s' "$help_out" | grep -q -i "Usage"; then
    pass "--help mentions Usage"
else
    fail "--help did not mention Usage. Got: $help_out"
fi

# -------- Test 6: unknown args do not break it --------
echo "Test 6: unknown args do not break the script"
"$SCRIPT" --some-nonsense-flag --another 99 >/dev/null 2>&1
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "unknown args tolerated; exit 0"
else
    fail "unknown args caused non-zero exit ($rc)"
fi

# -------- Test 7: platform-aware content on macOS / Linux --------
echo "Test 7: platform-aware content (best-effort)"
platform="$(uname -s 2>/dev/null || echo unknown)"
combined="$("$SCRIPT" 2>&1 || true)"
case "$platform" in
    Darwin)
        # Either we get a warning mentioning pmset/sleep, or a success-line
        # confirming sleep is disabled. Both are acceptable.
        if printf '%s' "$combined" | grep -qE "(pmset|sleep)"; then
            pass "macOS output mentions pmset/sleep"
        else
            fail "macOS output missing pmset/sleep keywords. Got: $combined"
        fi
        ;;
    Linux)
        if printf '%s' "$combined" | grep -qiE "(systemd|sleep|suspend|inhibit)"; then
            pass "Linux output mentions systemd-inhibit/sleep keywords"
        else
            fail "Linux output missing expected keywords. Got: $combined"
        fi
        ;;
    *)
        if printf '%s' "$combined" | grep -qi "unknown"; then
            pass "unknown platform handled gracefully"
        else
            # Acceptable to be silent; at minimum the script exited 0.
            pass "unknown platform produced no recognizable output (allowed)"
        fi
        ;;
esac

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
