#!/usr/bin/env bash
# test-spawn-claude-ambient-scrub.sh — spawn-claude.sh must not hand a
# pool-selected session a shadowing ambient Anthropic credential (#10413).
#
# Claude Code's credential precedence is ANTHROPIC_API_KEY env >
# CLAUDE_CODE_OAUTH_TOKEN env > keychain. spawn-claude.sh's selection branch
# exists so that the pool token it picks is the credential the spawned
# session actually uses — but the 2026-10-04 incident (#10413) showed an
# ambient ANTHROPIC_API_KEY exported by the spawning shell (a machine-level
# rc file pinning a zero-credit console key) silently winning over every
# account Loom selected, with no error at spawn time: the sessions simply
# ran, and died, on a key nobody chose.
#
# Contract under test (bare-metal dispatch, containment OFF):
#   1. When Loom selects the token itself (no LOOM_SPAWN_NO_EXPORT, no
#      caller-set CLAUDE_CODE_OAUTH_TOKEN), ambient ANTHROPIC_API_KEY /
#      ANTHROPIC_AUTH_TOKEN are UNSET for the child, a loud secret-free
#      warning names them, and the child still carries the pool token.
#   2. Explicit-credential callers (LOOM_SPAWN_NO_EXPORT + a caller-set
#      CLAUDE_CODE_OAUTH_TOKEN) are untouched: their ambient ANTHROPIC_*
#      environment passes through byte-identical and no selection runs.
#   3. A spawn with no ambient Anthropic credentials is a no-op: no
#      warning, token present.
#
# What is real here: spawn-claude.sh itself and host-side token selection
# against a real `loom-daemon tokens select`. What is stubbed: `claude`
# (it records the environment it was started with) — so "the child's
# environment" asserted below is exactly the environment the real CLI
# would have been started with.
#
# Split out from the parent suite for the same reason the #8697 suite was:
# test-spawn-claude.sh is over scripts/check-file-size-budget.sh's threshold
# and therefore frozen.
#
# Style matches test-spawn-claude.sh — plain bash, hand-rolled assertions.
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-claude-ambient-scrub.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEFAULTS_DIR/.." && pwd)"

# Pin THIS checkout's own binary (same rationale as test-spawn-claude.sh).
DAEMON_BIN=""
for _candidate in \
    "${CARGO_TARGET_DIR:+$CARGO_TARGET_DIR/release/loom-daemon}" \
    "${CARGO_TARGET_DIR:+$CARGO_TARGET_DIR/debug/loom-daemon}" \
    "$REPO_ROOT/target/release/loom-daemon" \
    "$REPO_ROOT/target/debug/loom-daemon"; do
    # First candidate that actually has the subcommand: a stale release build
    # next to a fresh debug one must not decide the suite.
    if [[ -n "$_candidate" && -x "$_candidate" ]] \
        && "$_candidate" tokens select --help >/dev/null 2>&1; then
        DAEMON_BIN="$_candidate"
        break
    fi
done
if [[ -z "$DAEMON_BIN" ]]; then
    echo "FAIL: no built loom-daemon with 'tokens select' (cargo build -p loom-daemon first)" >&2
    exit 1
fi

export LOOM_SWEEP_INFLIGHT_SWEEPS=1
export LOOM_SWEEP_CPU_QUOTA=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3"; echo "    Expected: '$1'"; echo "    Actual:   '$2'"; fi
}
assert_contains() {
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3"; echo "    Expected to contain: '$1'"; echo "    In: '$2'"; fi
}
assert_not_contains() {
    if [[ "$2" != *"$1"* ]]; then pass "$3"; else fail "$3"; echo "    Expected NOT to contain: '$1'"; fi
}

echo "==========================================="
echo "test-spawn-claude-ambient-scrub.sh (#10413)"
echo "==========================================="

REAL_TOKEN="sk-ant-oat01-fake-real-token-8697"
FAKE_API_KEY="sk-ant-api03-fake-console-key-10413"
FAKE_AUTH_TOKEN="fake-ambient-auth-token-10413"

WS="$(mktemp -d)"
mkdir -p "$WS/.loom/tokens"
chmod 700 "$WS/.loom/tokens"
echo -n "$REAL_TOKEN" > "$WS/.loom/tokens/acct.token"
chmod 600 "$WS/.loom/tokens/acct.token"
ln -s "$SCRIPTS_DIR" "$WS/.loom/scripts"
# Containment OFF: bare-metal dispatch execs the (stubbed) claude directly,
# so the stub's recorded env IS the child's spawn environment.
echo '{"runtimes": {"containment": {"enabled": false}}}' > "$WS/.loom/config.json"

STUBS="$(mktemp -d)"
trap 'rm -rf "$WS" "$STUBS"' EXIT

cat > "$STUBS/claude" <<STUB
#!/usr/bin/env bash
env > "$STUBS/claude-env.txt"
echo "stub-claude ran"
STUB
chmod +x "$STUBS/claude"

# Run spawn-claude.sh with a clean slate for every credential variable that
# decides the path under test; extra KEY=VALUE pairs come in as arguments
# (env processes them in order, so a trailing assignment wins over the
# leading -u and is how a test injects an "ambient" credential).
run_spawn() {
    rm -f "$STUBS/claude-env.txt"
    env -u CLAUDE_CODE_OAUTH_TOKEN -u LOOM_SPAWN_NO_EXPORT \
        -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u LOOM_TOKEN_NAME \
        -u LOOM_SWEEP_CREDENTIAL_PROXY -u LOOM_SWEEP_CONTAINERIZED -u LOOM_SPAWN_CONTAINERIZED \
        LOOM_WORKSPACE="$WS" LOOM_DAEMON_BIN="$DAEMON_BIN" \
        LOOM_SHARED_TOKENS_DIR="$STUBS/no-shared-pool" \
        PATH="$STUBS:$PATH" "$@" \
        "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1
}

child_env() {
    cat "$STUBS/claude-env.txt" 2>/dev/null || true
}

# The env dump is NAME=VALUE lines. Whole-line exact match, so a sibling
# variable carrying the target name as a SUFFIX (e.g. GAUNTLET_ANTHROPIC_API_KEY
# next to ANTHROPIC_API_KEY) cannot satisfy or break an assertion.
child_env_has() { grep -qxF -- "$1" <<<"$(child_env)"; }
child_env_lacks() { ! grep -qE "^${1}=" <<<"$(child_env)"; }

# ------------------------------------------------------------------ TC1
echo ""
echo "Ambient ANTHROPIC_* is unset when Loom selects the pool token..."
out="$(run_spawn ANTHROPIC_API_KEY="$FAKE_API_KEY" ANTHROPIC_AUTH_TOKEN="$FAKE_AUTH_TOKEN" || true)"

assert_contains "stub-claude ran" "$out" "the spawn still reaches claude"
assert_contains "# LOOM_ACCOUNT name=acct" "$out" "the pool account is selected as usual"
assert_contains "unsetting ambient ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN" "$out" \
    "the scrub logs a warning naming both variables"
assert_not_contains "$FAKE_API_KEY" "$out" "the API key value never reaches the log"
assert_not_contains "$FAKE_AUTH_TOKEN" "$out" "the auth token value never reaches the log"
if child_env_has "CLAUDE_CODE_OAUTH_TOKEN=$REAL_TOKEN"; then pass "the child carries the pool token it was selected"; else fail "the child carries the pool token it was selected"; fi
if child_env_lacks "ANTHROPIC_API_KEY"; then pass "the ambient API key is gone from the child env"; else fail "the ambient API key is gone from the child env"; fi
if child_env_lacks "ANTHROPIC_AUTH_TOKEN"; then pass "the ambient auth token is gone from the child env"; else fail "the ambient auth token is gone from the child env"; fi

# ------------------------------------------------------------------ TC2
echo ""
echo "Explicit-credential callers are untouched..."
out="$(run_spawn LOOM_SPAWN_NO_EXPORT=1 \
    CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-fake-caller-token-10413" \
    ANTHROPIC_API_KEY="$FAKE_API_KEY" || true)"

assert_contains "stub-claude ran" "$out" "the explicit-credential spawn still reaches claude"
if child_env_has "ANTHROPIC_API_KEY=$FAKE_API_KEY"; then pass "a caller-supplied ambient API key passes through byte-identical"; else fail "a caller-supplied ambient API key passes through byte-identical"; fi
if child_env_has "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fake-caller-token-10413"; then pass "the caller's own token is the one the child carries"; else fail "the caller's own token is the one the child carries"; fi
assert_not_contains "unsetting ambient" "$out" "no scrub warning for explicit-credential spawns"
assert_not_contains "# LOOM_ACCOUNT" "$out" "no pool selection runs for explicit-credential spawns"

# ------------------------------------------------------------------ TC3
echo ""
echo "No ambient credentials: byte-identical behaviour..."
out="$(run_spawn || true)"

assert_contains "stub-claude ran" "$out" "the clean spawn still reaches claude"
assert_not_contains "unsetting ambient" "$out" "no scrub warning when nothing is ambient"
if child_env_has "CLAUDE_CODE_OAUTH_TOKEN=$REAL_TOKEN"; then pass "the pool token is present"; else fail "the pool token is present"; fi
if child_env_lacks "ANTHROPIC_API_KEY"; then pass "no API key appears out of nowhere"; else fail "no API key appears out of nowhere"; fi

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."