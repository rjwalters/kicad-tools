#!/usr/bin/env bash
# test-champion-out-of-band-ac-gate-switch.sh - tests for the per-repo
# `champion.outOfBandAcGate` switch on Champion's out-of-band acceptance-criteria
# gate (#12703, gate origin #6883).
#
# champion-pr-merge.md Step 4 is prose an LLM executes, so this extracts the
# shipped `ac_gate_enabled` helper from it and drives it against a stub
# `loom-daemon`, then pins the doc surfaces that must mention the key.
#
# Usage:
#   ./defaults/scripts/tests/test-champion-out-of-band-ac-gate-switch.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
    DOCS_DIR="$(cd "$SCRIPTS_DIR/../docs" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
    DOCS_DIR="$(cd "$SCRIPTS_DIR/../docs" && pwd)"
fi
CHAMPION_MD="$PROMPT_DIR/champion-pr-merge.md"
CHAMPION_REF_MD="$PROMPT_DIR/champion-reference.md"
LABEL_MD="$DOCS_DIR/label-state-machine.md"

# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"

HELPER_SRC="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -f "$HELPER_SRC"; rm -rf "$STUB_DIR"' EXIT
awk '/^ac_gate_enabled\(\) \{/{f=1} f{print} f&&/^}/{exit}' "$CHAMPION_MD" >"$HELPER_SRC"

[[ -s "$HELPER_SRC" ]] && pass "ac_gate_enabled helper is shipped in champion-pr-merge.md" \
    || fail "ac_gate_enabled helper is shipped in champion-pr-merge.md"

# $1 = stub stdout, $2 = stub exit code; prints "on" or "off"
gate() {
    printf '#!/usr/bin/env bash\nprintf "%%s" %q\nexit %s\n' "$1" "$2" >"$STUB_DIR/loom-daemon"
    chmod +x "$STUB_DIR/loom-daemon"
    PATH="$STUB_DIR:$PATH" bash -c "$(cat "$HELPER_SRC")"'
if ac_gate_enabled; then echo on; else echo off; fi'
}

K='"key":"champion.outOfBandAcGate"'
assert_eq on  "$(gate "{$K,\"found\":false,\"value\":null}" 1)" "key unset -> gate on (default)"
assert_eq on  "$(gate "{$K,\"found\":true,\"value\":true,\"tier\":\"fleet-repo\"}" 0)" "true -> gate on"
assert_eq off "$(gate "{$K,\"found\":true,\"value\":false,\"tier\":\"fleet-repo\"}" 0)" "false -> gate off"
assert_eq on  "$(gate "{$K,\"found\":true,\"value\":\"false\"}" 0)" "string \"false\" -> gate on (fail closed)"
assert_eq on  "$(gate "{$K,\"found\":true,\"value\":0}" 0)" "non-boolean -> gate on"
assert_eq on  "$(gate 'not json' 0)" "malformed output -> gate on"
assert_eq on  "$(gate '' 2)" "verb failure -> gate on"

# Verb missing entirely: empty PATH dir without loom-daemon.
EMPTY_DIR="$(mktemp -d)"
JQ_BIN="$(dirname "$(command -v jq)")"
out=$(PATH="$EMPTY_DIR:$JQ_BIN" /bin/bash -c "$(cat "$HELPER_SRC")"'
if ac_gate_enabled; then echo on; else echo off; fi')
rm -rf "$EMPTY_DIR"
assert_eq on "$out" "loom-daemon absent -> gate on"

echo "--- Doc pins ---"
grep -q 'AC_RC=10' "$CHAMPION_MD" && pass "Step 4 skips the classifier when the switch is off" \
    || fail "Step 4 skips the classifier when the switch is off"
for f in "$CHAMPION_MD" "$CHAMPION_REF_MD" "$LABEL_MD"; do
    if grep -q 'champion.outOfBandAcGate' "$f" && grep -q 'loom-ui' "$f"; then
        pass "$(basename "$f") documents the key and names loom-ui"
    else
        fail "$(basename "$f") documents the key and names loom-ui"
    fi
done

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
