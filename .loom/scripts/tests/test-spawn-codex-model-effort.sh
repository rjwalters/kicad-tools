#!/usr/bin/env bash
# test-spawn-codex-model-effort.sh — spawn-codex.sh's `model@effort` split
# (issue #8387), split out of test-spawn-codex.sh (over the file-size ratchet
# threshold; new assertions go in a sibling, per file-size-policy.md).
#
# A tier model may carry an effort suffix (`gpt-5-codex@high`). Codex has no
# such model id, so spawn-codex.sh must forward only the base as `-m` and route
# the suffix as the LOWEST-priority effort source: an explicit
# `-c model_reasoning_effort=…` and LOOM_EFFORT both outrank it. An empty base
# falls through to LOOM_CODEX_MODEL (or no `-m`); an empty suffix routes no
# effort. The split applies to LOOM_MODEL and LOOM_CODEX_MODEL alike.
#
# Hermetic: LOOM_CODEX_NO_EXEC=1 argv-preview mode — never runs codex.
#
# Usage: ./defaults/scripts/tests/test-spawn-codex-model-effort.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_CODEX="$SCRIPTS_DIR/spawn-codex.sh"

# spawn-codex.sh execs loom-daemon: pin THIS checkout's build up front, before
# the first case, never the installed one (#10662).
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin --self-only --path "$SCRIPTS_DIR" spawn-worker session-exec private-workspace

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
        echo "    Expected substring: '$needle'"
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
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

TMPROOT="$(mktemp -d)"
source "$SCRIPT_DIR/lib/session-lock-sandbox.sh" "$TMPROOT"

# Same helper as test-spawn-codex.sh: argv-only mode, host Codex/model vars
# cleared so a developer's own environment never perturbs an assertion.
run_argv() {
    # usage: run_argv [VAR=val ...] -- <args...>
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
        -u LOOM_CODEX_SANDBOX -u LOOM_CODEX_NETWORK -u LOOM_MODEL \
        -u LOOM_EFFORT -u LOOM_CODEX_MODEL \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_CODEX" "$@" 2>/dev/null || true
}

echo "Testing spawn-codex.sh model@effort split (#8387)..."

# base -> -m, suffix -> lowest-priority effort.
out="$(run_argv LOOM_MODEL=gpt-5-codex@high -- -p x)"
assert_contains "-m gpt-5-codex -c model_reasoning_effort=high" "$out" "model@effort splits into -m base + effort"
out="$(run_argv LOOM_MODEL=gpt-5-codex@high LOOM_EFFORT=low -- -p x)"
assert_contains "model_reasoning_effort=low" "$out" "LOOM_EFFORT outranks the model suffix"
assert_not_contains "model_reasoning_effort=high" "$out" "suffix dropped when LOOM_EFFORT set"
out="$(run_argv LOOM_MODEL=gpt-5-codex@high LOOM_EFFORT=low -- -p x -c model_reasoning_effort=medium)"
assert_contains "model_reasoning_effort=medium" "$out" "explicit -c outranks suffix and LOOM_EFFORT"
assert_not_contains "model_reasoning_effort=high" "$out" "suffix dropped when explicit -c set"
out="$(run_argv LOOM_MODEL=gpt-5-codex@ -- -p x)"
assert_contains "-m gpt-5-codex" "$out" "empty suffix forwards the base only"
assert_not_contains "model_reasoning_effort" "$out" "empty suffix routes no effort"
out="$(run_argv LOOM_MODEL=@high LOOM_CODEX_MODEL=dflt -- -p x)"
assert_contains "-m dflt" "$out" "empty base falls through to the default model"
out="$(run_argv LOOM_CODEX_MODEL=dflt@low -- -p x)"
assert_contains "-m dflt -c model_reasoning_effort=low" "$out" "LOOM_CODEX_MODEL suffix is split too"
out="$(run_argv LOOM_MODEL=@high -- -p x)"
assert_not_contains " -m " "$out" "empty base with no default emits no -m"
assert_not_contains "@" "$out" "no @ forwarded for an empty base"

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
