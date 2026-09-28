#!/usr/bin/env bash
# test-claude-wrapper-proxy-rotation.sh — claude-wrapper.sh's account rotation
# under the Claude credential egress proxy (issue #8818).
#
# Background. With `runtimes.containment.claudeCredentialProxy` on, the wrapper
# runs inside a container whose token pool is masked and whose
# CLAUDE_CODE_OAUTH_TOKEN is a `loom-placeholder-…`. The three rotation helpers
# must then ask the HOST to rotate (`loom-daemon worker proxy-rotate`) instead
# of calling `tokens mark-bad` / `tokens select` against a pool they cannot see.
# The host half (mark, select, swap, trust boundary) is covered by the Rust
# suite in loom-daemon/src/worker_spawn/egress_proxy/rotation_tests.rs; this
# file pins the wrapper's routing:
#
#   1. proxied + exhausted     -> proxy-rotate usage-limit, no tokens calls, the
#                                 run continues on the rotated account
#   2. proxied + session limit -> proxy-rotate session-window
#   3. proxied + auth-dead     -> proxy-rotate auth-dead
#   4. proxied + concurrent    -> proxy-rotate concurrent-session
#   5. proxied + host refuses  -> ACCOUNT_POOL_EXHAUSTED, still no tokens calls
#   6. UNproxied + exhausted   -> tokens mark-bad + tokens select, no
#                                 proxy-rotate (AC4: unchanged)
#
# Style matches test-token-cache-affinity.sh — plain bash, hand-rolled
# assertions, stub `loom-daemon` and `claude`.
#
# Usage:
#   ./.loom/scripts/tests/test-claude-wrapper-proxy-rotation.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="$SCRIPTS_DIR/claude-wrapper.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected to contain: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected NOT to contain: '$needle'"
        echo "    In: '$haystack'"
    fi
}

echo "============================================"
echo "test-claude-wrapper-proxy-rotation.sh (#8818)"
echo "============================================"

if [[ ! -f "$WRAPPER" ]]; then
    echo "SKIP: claude-wrapper.sh not found at $WRAPPER (not shipped into this layout)" >&2
    exit 0
fi

WS="$(mktemp -d)"
STUB="$(mktemp -d)"
CALLS="$(mktemp)"
trap 'rm -rf "$WS" "$STUB"; rm -f "$CALLS"' EXIT
mkdir -p "$WS/.loom/tokens"
PLACEHOLDER="loom-placeholder-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# Stub daemon. `retry-classify` answers from the transcript (the same questions
# the real classifier is asked); `worker proxy-rotate` records its argv and,
# unless PROXY_REFUSE is set, hands back the rotated account; `tokens …` is
# recorded so the proxied cases can assert it was never reached.
cat > "$STUB/loom-daemon" <<STUB
#!/usr/bin/env bash
case "\$1 \${2:-}" in
    "retry-classify account-exhaustion") grep -qiE "hit your (session )?limit" && exit 0 || exit 1 ;;
    "retry-classify auth-dead") grep -q "401 Invalid bearer token" && exit 0 || exit 1 ;;
    "retry-classify session-limit") grep -q "concurrent sessions" && exit 0 || exit 1 ;;
    "retry-classify "*) exit 1 ;;
    "worker proxy-rotate")
        printf 'PROXY_ROTATE %s\n' "\${*:3}" >> "$CALLS"
        [[ -n "\${PROXY_REFUSE:-}" ]] && { echo "proxy-rotate: refused by the proxy (503 pool_exhausted)" >&2; exit 1; }
        printf "export LOOM_TOKEN_NAME='beta'\n"
        exit 0 ;;
    "tokens select")
        [[ "\${3:-}" == "--help" ]] && exit 0
        printf 'TOKENS_SELECT\n' >> "$CALLS"
        printf "export CLAUDE_CODE_OAUTH_TOKEN='tok-beta'\nexport LOOM_TOKEN_NAME='beta'\n"
        exit 0 ;;
    "tokens "*) printf 'TOKENS %s\n' "\${*:2}" >> "$CALLS"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$STUB/loom-daemon"

# Stub claude: account `alpha` fails with FAILURE_TEXT; any other account
# (i.e. after a rotation exported LOOM_TOKEN_NAME=beta) succeeds. Proxied, the
# token value never changes — only the host swapped what it stands for.
cat > "$STUB/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
if [[ "${LOOM_TOKEN_NAME:-}" == "alpha" ]]; then
    echo "${FAILURE_TEXT}"
    exit 1
fi
echo "stub-claude success as ${LOOM_TOKEN_NAME} token=${CLAUDE_CODE_OAUTH_TOKEN}"
exit 0
STUB
chmod +x "$STUB/claude"

# run_wrapper <token> <failure text> [extra env assignments...]
# Prints the wrapper's combined output; the call log is left in $CALLS.
run_wrapper() {
    local token="$1" failure="$2"
    shift 2
    : > "$CALLS"
    set +e
    env "$@" \
        LOOM_WORKSPACE="$WS" \
        LOOM_TOKEN_NAME="alpha" \
        CLAUDE_CODE_OAUTH_TOKEN="$token" \
        FAILURE_TEXT="$failure" \
        LOOM_DAEMON_BIN="$STUB/loom-daemon" \
        LOOM_DAEMON_SELF_BIN="$STUB/loom-daemon" \
        LOOM_MAX_RETRIES=1 \
        LOOM_SESSION_LIMIT_BACKOFF=0 \
        LOOM_SHEPHERD_TASK_ID="test-proxy-rotation" \
        LOOM_STARTUP_MONITOR_WINDOW=1 \
        PATH="$STUB:$PATH" \
        bash "$WRAPPER" -p "ping" 2>&1
    set -e
}

EXHAUSTED="You've hit your limit · resets 5pm"

echo ""
echo "1. Proxied + exhausted -> host-side rotation, launch continues"
out="$(run_wrapper "$PLACEHOLDER" "$EXHAUSTED")"
calls="$(cat "$CALLS")"
assert_contains "PROXY_ROTATE --reason usage-limit" "$calls" "exhaustion asks the proxy to rotate (usage-limit)"
assert_not_contains "TOKENS" "$calls" "no in-container tokens mark-bad/select on the proxied path"
assert_contains "stub-claude success as beta token=$PLACEHOLDER" "$out" \
    "the retry runs on the rotated account behind the SAME placeholder"
assert_not_contains "ACCOUNT_POOL_EXHAUSTED" "$out" "no pool-exhausted sentinel when the host rotated"

echo ""
echo "2. Proxied + 5h session window -> session-window"
run_wrapper "$PLACEHOLDER" "You've hit your session limit · resets 5pm" >/dev/null
assert_contains "PROXY_ROTATE --reason session-window" "$(cat "$CALLS")" "a session-limit exhaustion asks for the 5h-window mark"

echo ""
echo "3. Proxied + auth-dead -> auth-dead"
run_wrapper "$PLACEHOLDER" "Failed to authenticate. API Error: 401 Invalid bearer token" >/dev/null
calls="$(cat "$CALLS")"
assert_contains "PROXY_ROTATE --reason auth-dead" "$calls" "an auth-dead credential asks the proxy to rotate (auth-dead)"
assert_not_contains "TOKENS" "$calls" "no in-container tokens calls for auth-dead either"

echo ""
echo "4. Proxied + concurrent-session limit -> concurrent-session (no mark)"
run_wrapper "$PLACEHOLDER" "Error: maximum number of concurrent sessions reached for this account" >/dev/null
calls="$(cat "$CALLS")"
assert_contains "PROXY_ROTATE --reason concurrent-session" "$calls" "a capacity fault asks for a swap without a mark"
assert_not_contains "TOKENS" "$calls" "no in-container tokens calls for a capacity fault"

echo ""
echo "5. Proxied + host refuses -> ACCOUNT_POOL_EXHAUSTED, no fallback"
out="$(run_wrapper "$PLACEHOLDER" "$EXHAUSTED" PROXY_REFUSE=1)"
assert_contains "ACCOUNT_POOL_EXHAUSTED" "$out" "a refused host rotation ends the launch as before"
assert_contains "pool_exhausted" "$out" "the proxy's reason token reaches the log"
assert_not_contains "TOKENS" "$(cat "$CALLS")" "a refusal never falls back to in-container pool calls"

echo ""
echo "6. UNproxied + exhausted -> unchanged (tokens mark-bad + select)"
out="$(run_wrapper "tok-alpha" "$EXHAUSTED")"
calls="$(cat "$CALLS")"
assert_contains "TOKENS mark-bad alpha" "$calls" "an unproxied launch still bad-marks in-process"
assert_contains "TOKENS_SELECT" "$calls" "an unproxied launch still re-selects in-process"
assert_not_contains "PROXY_ROTATE" "$calls" "an unproxied launch never calls proxy-rotate"
assert_contains "stub-claude success as beta token=tok-beta" "$out" "the unproxied retry runs on the re-selected token"

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo "Tests passed: $TESTS_PASSED"
echo "Tests failed: $TESTS_FAILED"
echo "==================================="
[[ "$TESTS_FAILED" -eq 0 ]]
