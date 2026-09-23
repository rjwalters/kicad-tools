#!/usr/bin/env bash
# test-token-cache-affinity.sh — prompt-cache affinity must NOT leak into
# claude-wrapper.sh's post-failure account rotation (issue #8146).
#
# Background. `loom-daemon tokens select --role` is the second half of the
# prompt-cache affinity key, and it is declared with clap's `env = "LOOM_ROLE"`
# — so it is active for EVERY `tokens select` whose environment carries
# LOOM_ROLE, with no `--role` on the command line at all. The role runner
# exports LOOM_ROLE into every spawn and it is inherited straight through
# spawn-claude.sh into claude-wrapper.sh.
#
# That is correct for the initial selection and WRONG for the rotation paths.
# `reselect_account_no_mark()` exists to spread to a healthy sibling after a
# concurrent-session-limit fault, and it deliberately does NOT bad-mark — so
# the saturated account is still a candidate, and an active affinity key names
# it as the PREFERRED one. The retry would then re-pick the account that just
# failed, re-fail on the same limit, and burn the retry budget. The fix is
# `env -u LOOM_ROLE` on the three `tokens select` invocations in the wrapper;
# this suite is what stops that coupling silently returning.
#
# Hermetic: no network, no live pool, no real `claude`. A stub `loom-daemon`
# records whether LOOM_ROLE was present in the environment of each
# `tokens select` call it received.
#
# Style matches test-token-model-class.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Usage:
#   ./.loom/scripts/tests/test-token-cache-affinity.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Self-relative: the subject ships alongside this suite, so this resolves in
# both the source tree (defaults/scripts/) and an installed repo
# (.loom/scripts/) without a hardcoded REPO_ROOT path.
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="$SCRIPTS_DIR/claude-wrapper.sh"

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

echo "==================================="
echo "test-token-cache-affinity.sh (#8146)"
echo "==================================="

if [[ ! -f "$WRAPPER" ]]; then
    echo "SKIP: claude-wrapper.sh not found at $WRAPPER (not shipped into this layout)" >&2
    exit 0
fi

# ============================================================
# Section 1: source-level invariant
# ============================================================
#
# Cheap, layout-independent, and it is the assertion that names the defect:
# every `tokens select` the wrapper issues must be prefixed with
# `env -u LOOM_ROLE`. Runs even where no loom-daemon binary is available.

echo ""
echo "Testing claude-wrapper.sh 'tokens select' invocations (#8146)..."

# An *invocation* is a line that actually runs the subcommand (it carries
# `--workspace`); a bare `tokens select` mention elsewhere in the file is
# prose in a comment. Every invocation must carry the `env -u LOOM_ROLE`
# prefix, so a future call site added without it fails here.
invocations="$(grep -c 'tokens select --workspace' "$WRAPPER" || true)"
unguarded="$(grep 'tokens select --workspace' "$WRAPPER" | grep -cv 'env -u LOOM_ROLE' || true)"
assert_eq "3" "$invocations" \
    "claude-wrapper.sh still issues exactly the three known 'tokens select' calls (#8146)"
assert_eq "0" "$unguarded" \
    "no 'tokens select' invocation runs without the env -u LOOM_ROLE prefix (#8146)"

# ============================================================
# Section 2: behavioural regression — reselect_account_no_mark
# ============================================================
#
# Drive the real wrapper through a real concurrent-session-limit fault with
# LOOM_ROLE exported, and assert the `tokens select` the rotation issues does
# NOT see it. A stub `loom-daemon` records the presence/absence of LOOM_ROLE
# for every `tokens select` call; a stub `claude` produces the fault.

echo ""
echo "Testing reselect_account_no_mark() is not steered by an inherited LOOM_ROLE (#8146)..."

AFF_WS="$(mktemp -d)"
AFF_STUB="$(mktemp -d)"
AFF_ROLE_LOG="$(mktemp)"
trap 'rm -rf "$AFF_WS" "$AFF_STUB"; rm -f "$AFF_ROLE_LOG"' EXIT

mkdir -p "$AFF_WS/.loom/tokens"
chmod 700 "$AFF_WS/.loom/tokens"
printf '%s' "tok-alpha" > "$AFF_WS/.loom/tokens/alpha.token"
printf '%s' "tok-beta"  > "$AFF_WS/.loom/tokens/beta.token"
chmod 600 "$AFF_WS/.loom/tokens/"*.token

# Stub daemon: log whether LOOM_ROLE reached this `tokens select`, then hand
# back a valid `--export` payload naming the sibling account. Everything else
# the wrapper may ask of `loom-daemon` is a silent no-op.
cat > "$AFF_STUB/loom-daemon" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "tokens" && "\$2" == "select" && "\$3" != "--help" ]]; then
    if [[ -n "\${LOOM_ROLE+set}" ]]; then
        printf 'LOOM_ROLE_PRESENT=%s\n' "\${LOOM_ROLE}" >> "$AFF_ROLE_LOG"
    else
        printf 'LOOM_ROLE_ABSENT\n' >> "$AFF_ROLE_LOG"
    fi
    printf "export CLAUDE_CODE_OAUTH_TOKEN='tok-beta'\n"
    printf "export LOOM_TOKEN_NAME='beta'\n"
    printf "LOOM_TOKEN_MODE='random'\n"
    exit 0
fi
exit 0
STUB
chmod +x "$AFF_STUB/loom-daemon"

# Stub claude: the alpha account hits a concurrent-session limit (a capacity
# fault, NOT exhaustion — the wrapper must re-select without bad-marking);
# beta succeeds, so the run terminates.
cat > "$AFF_STUB/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
if [[ "${CLAUDE_CODE_OAUTH_TOKEN}" == "tok-alpha" ]]; then
    echo "Error: maximum number of concurrent sessions reached for this account"
    exit 1
fi
echo "stub-claude success on token=${CLAUDE_CODE_OAUTH_TOKEN}"
exit 0
STUB
chmod +x "$AFF_STUB/claude"

set +e
LOOM_WORKSPACE="$AFF_WS" \
LOOM_TOKEN_NAME="alpha" \
CLAUDE_CODE_OAUTH_TOKEN="tok-alpha" \
LOOM_DAEMON_BIN="$AFF_STUB/loom-daemon" \
LOOM_ROLE="judge" \
LOOM_MAX_RETRIES=1 \
LOOM_SESSION_LIMIT_BACKOFF=0 \
LOOM_SHEPHERD_TASK_ID="test-cache-affinity" \
LOOM_STARTUP_MONITOR_WINDOW=1 \
PATH="$AFF_STUB:$PATH" \
    bash "$WRAPPER" -p "ping" >/dev/null 2>&1
set -e

role_log="$(cat "$AFF_ROLE_LOG" 2>/dev/null || true)"

assert_contains "LOOM_ROLE_ABSENT" "$role_log" \
    "the rotation's 'tokens select' ran with LOOM_ROLE unset (#8146)"
assert_not_contains "LOOM_ROLE_PRESENT" "$role_log" \
    "an inherited LOOM_ROLE never reaches a rotation 'tokens select' (#8146)"

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
