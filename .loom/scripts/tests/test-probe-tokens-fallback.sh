#!/usr/bin/env bash
# test-probe-tokens-fallback.sh - Regression guard for #4079, re-scoped in
# #4228 (epic #4081 Phase 2) and again in #4557 (Phase 4).
#
# probe-tokens.sh delegates to the native `loom-daemon tokens check`
# subcommand (issue #4080) and has NO fallback tier left at all:
#   - the bare `python3 -m loom_tools.tokens.cli` tier that #4079 originally
#     regression-tested went in #4080;
#   - the `loom-tokens`-console-script-on-PATH tier went in #4557, which
#     deleted the Python package that provided it. A `loom-tokens` binary
#     surviving on PATH after that deletion is by definition a stale
#     editable-install leftover — the #4079 shadowing failure mode — so
#     dispatching to it would be a bug, not a graceful degradation.
#
# This test asserts:
#   1. probe-tokens.sh no longer references the stale module path (#4079).
#   2. probe-tokens.sh contains NO python3/loom_tools reference whatsoever —
#      the bare interpreter fallback tier is gone, not just fixed (#4228).
#   3. probe-tokens.sh's executable code contains NO `loom-tokens` reference
#      whatsoever — the console-script fallback tier is gone (#4557).
#   4. With no capable `loom-daemon` binary resolvable, probe-tokens.sh fails
#      loudly (exit 1, actionable message) rather than silently degrading.
#   5. It fails loudly EVEN WITH a `loom-tokens` on PATH — the presence of a
#      stale console script must not resurrect the removed fallback (#4557).
#
# Usage:
#   ./.loom/scripts/tests/test-probe-tokens-fallback.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROBE_SCRIPT="$SCRIPTS_DIR/probe-tokens.sh"

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
if [[ -x "$PROBE_SCRIPT" ]]; then
    pass "probe-tokens.sh is executable"
else
    fail "probe-tokens.sh is missing or not executable: $PROBE_SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: no reference to the stale never-existent module --------
echo "Test 2: no reference to the stale loom_tools.cli.loom_tokens module"
if grep -q "loom_tools\.cli\.loom_tokens" "$PROBE_SCRIPT"; then
    fail "probe-tokens.sh still references the never-existent loom_tools.cli.loom_tokens"
else
    pass "probe-tokens.sh does not reference loom_tools.cli.loom_tokens"
fi

# -------- Test 3: NO python3/loom_tools reference in CODE (issue #4228) --------
# The bare interpreted-language fallback tier #4079 regression-tested is gone
# entirely as of #4080 — the only remaining fallback is `loom-tokens` on PATH.
# Deliberate comments explaining the history (e.g. "the bare python3 -m
# fallback tier has been removed") are allowed — only non-comment lines count.
echo "Test 3: no python3/loom_tools reference in probe-tokens.sh's executable code"
code_hits="$(grep -vE '^\s*#' "$PROBE_SCRIPT" | grep -nE "python3|loom_tools" || true)"
if [[ -n "$code_hits" ]]; then
    fail "probe-tokens.sh's executable code still references python3/loom_tools: $code_hits"
else
    pass "probe-tokens.sh's executable code contains no python3/loom_tools reference"
fi

# -------- Test 3b: NO `loom-tokens` console-script reference in CODE (#4557) --
# Epic #4081 Phase 4 deleted the Python package that shipped the `loom-tokens`
# console script, so `loom-daemon tokens` is the only implementation left. A
# `loom-tokens` surviving on PATH is a stale editable-install leftover (#4079);
# dispatching to it would run frozen token logic. Comments recording that
# history are allowed — only non-comment lines count.
echo "Test 3b: no loom-tokens console-script fallback in probe-tokens.sh's executable code"
tokens_hits="$(grep -vE '^\s*#' "$PROBE_SCRIPT" | grep -nE "loom-tokens" || true)"
if [[ -n "$tokens_hits" ]]; then
    fail "probe-tokens.sh's executable code still references the retired loom-tokens binary: $tokens_hits"
else
    pass "probe-tokens.sh's executable code contains no loom-tokens reference"
fi

# A hermetic "nothing resolves" workspace: no .git/.loom markers (so
# probe-tokens.sh's own find_repo_root() falls back to $PWD) and no
# target/{release,debug}/loom-daemon build-output-relative candidate
# underneath it — required to truly defeat daemon-binary resolution rather
# than accidentally finding THIS checkout's own freshly-built binary.
NO_DAEMON_WS="$(mktemp -d)"
trap 'rm -rf "$NO_DAEMON_WS"' EXIT
STRIPPED_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

# An empty $HOME so step 4 of loom_locate_daemon_bin (the machine-level
# install fallback under ${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}) can't fall
# through to a real host's install -- on a host with a genuine machine-level
# Loom install, leaving $HOME/LOOM_DAEMON_BIN_DIR untouched would defeat these
# "no resolvable daemon binary" fixtures (#5183).
EMPTY_HOME="$(mktemp -d)"
trap 'rm -rf "$NO_DAEMON_WS" "$EMPTY_HOME"' EXIT

# -------- Test 4: no capable daemon binary fails loudly (exit 1,
#                   actionable message) --------
echo "Test 4: no capable daemon binary -> exit 1"
out="$(cd "$NO_DAEMON_WS" && LOOM_DAEMON_BIN="/nonexistent/loom-daemon" \
    PATH="$STRIPPED_PATH" HOME="$EMPTY_HOME" LOOM_DAEMON_BIN_DIR="/nonexistent" \
    "$PROBE_SCRIPT" --json 2>&1)"
rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "no resolvable daemon binary exits 1"
else
    fail "expected exit 1 with no resolvable daemon binary, got $rc"
fi
if [[ "$out" == *"no loom-daemon binary"* ]]; then
    pass "failure message is actionable (names the missing daemon binary)"
else
    fail "failure message did not mention the missing daemon binary. Got: $out"
fi

# -------- Test 5: a stale `loom-tokens` on PATH does NOT resurrect the removed
#                   fallback — probe-tokens.sh must still fail loudly (#4557) --
echo "Test 5: a stale loom-tokens on PATH does not resurrect the removed fallback"
STALE_BIN_DIR="$(mktemp -d)"
cat > "$STALE_BIN_DIR/loom-tokens" <<'STALE_EOF'
#!/usr/bin/env bash
# Fixture standing in for a stale editable-install console script (#4079).
echo '{"stale":"this must never run"}'
exit 0
STALE_EOF
chmod +x "$STALE_BIN_DIR/loom-tokens"

stale_out="$(cd "$NO_DAEMON_WS" && LOOM_DAEMON_BIN="/nonexistent/loom-daemon" \
    PATH="$STALE_BIN_DIR:$STRIPPED_PATH" HOME="$EMPTY_HOME" LOOM_DAEMON_BIN_DIR="/nonexistent" \
    "$PROBE_SCRIPT" --json 2>&1)"
rc=$?
rm -rf "$STALE_BIN_DIR"
if [[ "$rc" -eq 1 ]]; then
    pass "stale loom-tokens on PATH still exits 1 (fallback tier is gone)"
else
    fail "expected exit 1 with a stale loom-tokens on PATH, got $rc"
fi
if [[ "$stale_out" != *"this must never run"* ]]; then
    pass "stale loom-tokens on PATH was never invoked"
else
    fail "probe-tokens.sh invoked the stale loom-tokens fixture. Got: $stale_out"
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
