#!/usr/bin/env bash
# test-spawn-claude.sh — Tests for spawn-claude.sh and classify_error.
#
# Style matches test-forge-helpers.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-claude.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
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
    local needle="$1"
    local haystack="$2"
    local msg="$3"
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

# ============================================================
# Section 1: classify_error test vectors (curator's 18 vectors)
# ============================================================

echo "Testing classify_error..."
# Source the library
# shellcheck source=../lib/classify-error.sh
source "$SCRIPTS_DIR/lib/classify-error.sh"

# Vector #1: clean exit with "500" in output → SUCCESS (regression #3233)
result=$(classify_error "successfully merged PR #500 with status 200" 0)
assert_eq "SUCCESS" "$result" "exit=0 output containing '500' is SUCCESS (#3233 regression)"

# Vector #2: clean exit with "rate limit" substring → SUCCESS (regression #3233)
result=$(classify_error "rate limit headers indicate 4500 remaining" 0)
assert_eq "SUCCESS" "$result" "exit=0 with 'rate limit' substring is SUCCESS (#3233 regression)"

# Vector #3: clean exit, "No messages returned" → SUCCESS (regression #3233)
result=$(classify_error "No messages returned in this cycle, exiting" 0)
assert_eq "SUCCESS" "$result" "exit=0 with 'No messages returned' is SUCCESS (#3233 regression)"

# Vector #4: defensive — clean exit even with "500 Internal Server Error"
result=$(classify_error "Internal Server Error 500" 0)
assert_eq "SUCCESS" "$result" "exit=0 with '500 Internal Server Error' is SUCCESS"

# Vector #5: clean exit, empty output
result=$(classify_error "" 0)
assert_eq "SUCCESS" "$result" "exit=0 empty output is SUCCESS"

# Vector #6: timeout exit 124 → TIMEOUT
result=$(classify_error "anything" 124)
assert_eq "TIMEOUT" "$result" "exit=124 is TIMEOUT"

# Vector #7: SIGKILL exit 137 → TIMEOUT
result=$(classify_error "anything" 137)
assert_eq "TIMEOUT" "$result" "exit=137 is TIMEOUT"

# Vector #8: 401 expired → TOKEN_EXPIRED
result=$(classify_error "OAuth token has expired" 1)
assert_eq "TOKEN_EXPIRED" "$result" "OAuth token expired -> TOKEN_EXPIRED"

# Vector #9: 401 authentication_error → TOKEN_EXPIRED
result=$(classify_error "401 authentication_error" 1)
assert_eq "TOKEN_EXPIRED" "$result" "401 authentication_error -> TOKEN_EXPIRED"

# Vector #10: hit your limit → TOKEN_EXHAUSTED
result=$(classify_error "You've hit your limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "hit your limit -> TOKEN_EXHAUSTED"

# Vector #11: weekly limit → TOKEN_EXHAUSTED
result=$(classify_error "hit your weekly limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "weekly limit -> TOKEN_EXHAUSTED"

# Vector #12: 429 → RECOVERABLE
result=$(classify_error "429 Too Many Requests" 1)
assert_eq "RECOVERABLE" "$result" "429 -> RECOVERABLE"

# Vector #13: 500 (with non-zero exit) → RECOVERABLE
result=$(classify_error "500 Internal Server Error" 1)
assert_eq "RECOVERABLE" "$result" "500 + exit=1 -> RECOVERABLE"

# Vector #14: 503 → RECOVERABLE
result=$(classify_error "503 Service Unavailable" 1)
assert_eq "RECOVERABLE" "$result" "503 -> RECOVERABLE"

# Vector #15: ECONNREFUSED → RECOVERABLE
result=$(classify_error "ECONNREFUSED" 1)
assert_eq "RECOVERABLE" "$result" "ECONNREFUSED -> RECOVERABLE"

# Vector #16: No messages returned + exit=1 → RECOVERABLE (only when failed)
result=$(classify_error "No messages returned" 1)
assert_eq "RECOVERABLE" "$result" "'No messages' + exit=1 -> RECOVERABLE"

# Vector #17: cwd deleted → CWD_DELETED
result=$(classify_error "current working directory was deleted" 1)
assert_eq "CWD_DELETED" "$result" "cwd deleted -> CWD_DELETED"

# Vector #18: catch-all unknown failure → RECOVERABLE
result=$(classify_error "" 2)
assert_eq "RECOVERABLE" "$result" "unknown exit=2 -> RECOVERABLE"

# ============================================================
# Section 2: spawn-claude.sh dispatch (with stub `claude`)
# ============================================================

echo ""
echo "Testing spawn-claude.sh dispatch..."

# Set up a fake workspace
TEST_WS="$(mktemp -d)"
trap 'rm -rf "$TEST_WS"' EXIT

mkdir -p "$TEST_WS/.loom/tokens"
chmod 700 "$TEST_WS/.loom/tokens"
echo -n "fake-token-alpha" > "$TEST_WS/.loom/tokens/alpha.token"
chmod 600 "$TEST_WS/.loom/tokens/alpha.token"

# Stub `claude` binary
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_WS" "$STUB_DIR"' EXIT
cat > "$STUB_DIR/claude" <<'STUB'
#!/usr/bin/env bash
echo "stub-claude got token=${CLAUDE_CODE_OAUTH_TOKEN}"
echo "stub-claude args=$*"
exit 0
STUB
chmod +x "$STUB_DIR/claude"

# Test: spawn-claude selects the only token and exec's the stub
output=$(LOOM_WORKSPACE="$TEST_WS" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude got token=fake-token-alpha" "$output" \
    "spawn-claude exports selected token to claude"
assert_contains "stub-claude args=-p ping" "$output" \
    "spawn-claude passes args through to claude"
assert_contains "OAuth account 'alpha'" "$output" \
    "spawn-claude logs the chosen account"

# Test: explicit CLAUDE_CODE_OAUTH_TOKEN bypasses selection
output=$(LOOM_WORKSPACE="$TEST_WS" PATH="$STUB_DIR:$PATH" \
    CLAUDE_CODE_OAUTH_TOKEN="caller-supplied" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude got token=caller-supplied" "$output" \
    "explicit CLAUDE_CODE_OAUTH_TOKEN is preserved"

# Test: missing tokens dir → exit 78 with helpful message
EMPTY_WS="$(mktemp -d)"
output=$(LOOM_WORKSPACE="$EMPTY_WS" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
exit_code=$?
assert_contains "loom-tokens bootstrap" "$output" \
    "empty pool error mentions 'loom-tokens bootstrap'"
rm -rf "$EMPTY_WS"

# Test that spawn-claude.sh exits 78 on missing tokens
set +e
LOOM_WORKSPACE="$(mktemp -d)" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" >/dev/null 2>&1
exit_code=$?
set -e
assert_eq "78" "$exit_code" "missing tokens exits 78 (EX_CONFIG)"

# ============================================================
# Section 3: model selection (issue #3477, Phase 1)
#
# Precedence chain at the spawn layer, all four observable cases:
#   1. explicit --model arg beats LOOM_MODEL env
#   2. --model=value form also beats LOOM_MODEL env
#   3. LOOM_MODEL alone produces --model in the exec'd args
#   4. no env + no arg produces NO --model at all (session default
#      preserved — the zero-behavior-change acceptance criterion)
# ============================================================

echo ""
echo "Testing spawn-claude.sh model selection (#3477)..."

# Case 3: LOOM_MODEL env produces --model in args
output=$(LOOM_WORKSPACE="$TEST_WS" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="claude-sonnet-4-6" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude args=-p ping --model claude-sonnet-4-6" "$output" \
    "LOOM_MODEL env injects --model into claude args"
assert_contains "spawn-claude: model=claude-sonnet-4-6 (from LOOM_MODEL)" "$output" \
    "structured model log line emitted for LOOM_MODEL case (#3482)"

# Case 1: explicit --model arg wins over LOOM_MODEL env
output=$(LOOM_WORKSPACE="$TEST_WS" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="claude-sonnet-4-6" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" --model claude-opus-4-8 2>&1 || true)
assert_contains "stub-claude args=-p ping --model claude-opus-4-8" "$output" \
    "explicit --model arg wins over LOOM_MODEL env"
assert_contains "spawn-claude: model=claude-opus-4-8 (from --model arg)" "$output" \
    "structured model log line emitted for explicit --model arg case (#3482)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"claude-sonnet-4-6"* ]] || [[ "$output" == *"wins over LOOM_MODEL"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_MODEL value is not injected when explicit --model present"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_MODEL value is not injected when explicit --model present"
    echo "    In: '$output'"
fi

# Case 2: --model=value form also suppresses LOOM_MODEL injection
output=$(LOOM_WORKSPACE="$TEST_WS" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="claude-sonnet-4-6" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" --model=claude-opus-4-8 2>&1 || true)
assert_contains "stub-claude args=-p ping --model=claude-opus-4-8" "$output" \
    "--model=value form wins over LOOM_MODEL env"
assert_contains "spawn-claude: model=claude-opus-4-8 (from --model arg)" "$output" \
    "structured model log line emitted for --model=value form (#3482)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"--model claude-sonnet-4-6"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: --model=value suppresses LOOM_MODEL injection"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: --model=value suppresses LOOM_MODEL injection"
    echo "    In: '$output'"
fi

# Case 4 (zero-behavior-change criterion): no env + no arg => no --model
output=$(LOOM_WORKSPACE="$TEST_WS" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" == *"stub-claude args=-p ping"* && "$output" != *"--model"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no LOOM_MODEL + no --model arg emits NO --model (session default preserved)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: no LOOM_MODEL + no --model arg emits NO --model (session default preserved)"
    echo "    In: '$output'"
fi
assert_contains "spawn-claude: model=default" "$output" \
    "structured model=default log line emitted when nothing configured (#3482)"

# Empty LOOM_MODEL is treated as unset — no --model emitted
output=$(LOOM_WORKSPACE="$TEST_WS" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" == *"stub-claude args=-p ping"* && "$output" != *"--model"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: empty LOOM_MODEL emits NO --model"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: empty LOOM_MODEL emits NO --model"
    echo "    In: '$output'"
fi
assert_contains "spawn-claude: model=default" "$output" \
    "structured model=default log line emitted for empty LOOM_MODEL (#3482)"

# ============================================================
# Summary
# ============================================================

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
