#!/usr/bin/env bash
# test-guard-hook-schema.sh - Regression tests for the PreToolUse hook schema
# emitted by guard-destructive.sh and guard-readonly-dirs.sh.template (issue #3550).
#
# Claude Code's PreToolUse hook schema REQUIRES a `hookEventName: "PreToolUse"`
# field inside the `hookSpecificOutput` object. Without it, Claude Code silently
# discards the permission decision and the guard becomes inert — every deny/ask
# is a no-op and the guarded command runs anyway.
#
# These tests feed crafted stdin JSON to each hook and assert the emitted JSON
# carries `.hookSpecificOutput.hookEventName == "PreToolUse"` for both the deny
# and ask decisions. They also assert the raw jq-fallback echo strings (used when
# jq -n fails at runtime) carry the same field, so future schema drift is caught
# on either code path.
#
# Usage:
#   bash defaults/scripts/tests/test-guard-hook-schema.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
GUARD_DESTRUCTIVE="$DEFAULTS_DIR/hooks/guard-destructive.sh"
GUARD_READONLY_TEMPLATE="$DEFAULTS_DIR/hooks/guard-readonly-dirs.sh.template"

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
    [[ -n "${2:-}" ]] && echo "    $2"
}

# Assert a JSON blob has the given jq boolean expression evaluate to true.
assert_jq_true() {
    local json="$1" expr="$2" msg="$3"
    local result
    result=$(printf '%s' "$json" | jq -r "$expr" 2>/dev/null) || result="<jq-parse-error>"
    if [[ "$result" == "true" ]]; then
        pass "$msg"
    else
        fail "$msg" "expected '$expr' == true, got '$result'; json: $json"
    fi
}

if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required to run these tests" >&2
    exit 1
fi

if [[ ! -f "$GUARD_DESTRUCTIVE" ]]; then
    echo "ERROR: $GUARD_DESTRUCTIVE not found" >&2
    exit 1
fi
if [[ ! -f "$GUARD_READONLY_TEMPLATE" ]]; then
    echo "ERROR: $GUARD_READONLY_TEMPLATE not found" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# guard-destructive.sh — functional deny path
# ---------------------------------------------------------------------------
echo "guard-destructive.sh: deny decision carries hookEventName"
DENY_INPUT=$(jq -n --arg cmd "rm -rf /" --arg cwd "$DEFAULTS_DIR" \
    '{tool_input: {command: $cmd}, cwd: $cwd}')
DENY_OUT=$(printf '%s' "$DENY_INPUT" | bash "$GUARD_DESTRUCTIVE" 2>/dev/null)

assert_jq_true "$DENY_OUT" '.hookSpecificOutput.hookEventName == "PreToolUse"' \
    "deny: hookEventName == PreToolUse"
assert_jq_true "$DENY_OUT" '.hookSpecificOutput.permissionDecision == "deny"' \
    "deny: permissionDecision == deny"
echo ""

# ---------------------------------------------------------------------------
# guard-destructive.sh — functional ask path
# ---------------------------------------------------------------------------
echo "guard-destructive.sh: ask decision carries hookEventName"
ASK_INPUT=$(jq -n --arg cmd "git reset --hard HEAD~1" --arg cwd "$DEFAULTS_DIR" \
    '{tool_input: {command: $cmd}, cwd: $cwd}')
ASK_OUT=$(printf '%s' "$ASK_INPUT" | bash "$GUARD_DESTRUCTIVE" 2>/dev/null)

assert_jq_true "$ASK_OUT" '.hookSpecificOutput.hookEventName == "PreToolUse"' \
    "ask: hookEventName == PreToolUse"
assert_jq_true "$ASK_OUT" '.hookSpecificOutput.permissionDecision == "ask"' \
    "ask: permissionDecision == ask"
echo ""

# ---------------------------------------------------------------------------
# guard-destructive.sh — raw jq-fallback echo strings carry the field
# ---------------------------------------------------------------------------
echo "guard-destructive.sh: raw jq-fallback echoes carry hookEventName"
FALLBACK_LINES=$(grep -c 'echo "{\\"hookSpecificOutput\\":{\\"hookEventName\\":\\"PreToolUse\\"' "$GUARD_DESTRUCTIVE")
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$FALLBACK_LINES" -eq 2 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: both raw fallback echoes (deny + ask) include hookEventName"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: expected 2 raw fallback echoes with hookEventName, found $FALLBACK_LINES"
fi
echo ""

# ---------------------------------------------------------------------------
# guard-readonly-dirs.sh.template — functional deny path
# ---------------------------------------------------------------------------
echo "guard-readonly-dirs.sh.template: deny decision carries hookEventName"
# Materialize the template into a temp git repo with a configured protected dir.
TMP_REPO=$(mktemp -d /tmp/loom-guard-readonly-test.XXXXXX)
trap 'rm -rf "$TMP_REPO"' EXIT
# Canonicalize to defeat the macOS /tmp -> /private/tmp symlink, so the path we
# feed the hook matches the repo root git resolves via `rev-parse`.
TMP_REPO=$(cd "$TMP_REPO" && pwd -P)
(
    cd "$TMP_REPO" || exit 1
    git init -q .
    git config user.email "test@example.com"
    git config user.name "Test"
    mkdir -p vendor
)
READONLY_HOOK="$TMP_REPO/guard-readonly-dirs.sh"
# Inject a non-empty PROTECTED_DIRS array so the guard is active.
sed 's|^PROTECTED_DIRS=(|PROTECTED_DIRS=(\n    "vendor/"|' \
    "$GUARD_READONLY_TEMPLATE" > "$READONLY_HOOK"
chmod +x "$READONLY_HOOK"

RO_INPUT=$(jq -n --arg fp "$TMP_REPO/vendor/lib.js" --arg cwd "$TMP_REPO" \
    '{tool_input: {file_path: $fp}, cwd: $cwd}')
RO_OUT=$(printf '%s' "$RO_INPUT" | bash "$READONLY_HOOK" 2>/dev/null)

assert_jq_true "$RO_OUT" '.hookSpecificOutput.hookEventName == "PreToolUse"' \
    "readonly deny: hookEventName == PreToolUse"
assert_jq_true "$RO_OUT" '.hookSpecificOutput.permissionDecision == "deny"' \
    "readonly deny: permissionDecision == deny"
echo ""

# ---------------------------------------------------------------------------
# guard-readonly-dirs.sh.template — raw jq-fallback echo carries the field
# ---------------------------------------------------------------------------
echo "guard-readonly-dirs.sh.template: raw jq-fallback echo carries hookEventName"
RO_FALLBACK_LINES=$(grep -c 'echo "{\\"hookSpecificOutput\\":{\\"hookEventName\\":\\"PreToolUse\\"' "$GUARD_READONLY_TEMPLATE")
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$RO_FALLBACK_LINES" -eq 1 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: raw fallback echo (deny) includes hookEventName"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: expected 1 raw fallback echo with hookEventName, found $RO_FALLBACK_LINES"
fi
echo ""

# --- Summary ---
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
