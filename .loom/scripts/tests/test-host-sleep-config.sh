#!/usr/bin/env bash
# test-host-sleep-config.sh — Tests for lib/host-sleep-config.sh (#6311).
#
# Covers:
#   loom_host_prevent_sleep_enabled       — env > config > default-OFF
#                                            precedence, malformed-value
#                                            fail-safe, absent-config default.
#   loom_host_sleep_mitigation_acknowledged — env > config > "" precedence.
#
# Uses a throwaway `mktemp -d` repo_root per test, matching the pattern in
# test-config-resolver.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-host-sleep-config.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

HOST_SLEEP_CONFIG_LIB="$SCRIPTS_DIR/lib/host-sleep-config.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

assert_contains() {
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3 (expected to find '$1' in '$2')"; fi
}

if [[ ! -f "$HOST_SLEEP_CONFIG_LIB" ]]; then
    echo "host-sleep-config.sh not found at $HOST_SLEEP_CONFIG_LIB" >&2
    exit 1
fi
# shellcheck source=../lib/host-sleep-config.sh
source "$HOST_SLEEP_CONFIG_LIB"

# Always disable the private-defaults tier for deterministic test output.
export LOOM_CONFIG_DEFAULTS_FILE=""

if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found on PATH -- skipping test-host-sleep-config.sh"
    exit 0
fi

new_repo() {
    local dir
    dir="$(mktemp -d)"
    mkdir -p "$dir/.loom"
    echo "$dir"
}

# --- Test 1: absent config -> disabled ("0"), matching today's behavior ---
echo "Test 1: absent host.preventSleep -> disabled by default"
repo=$(new_repo)
result=$( (unset LOOM_HOST_PREVENT_SLEEP; loom_host_prevent_sleep_enabled "$repo") )
assert_eq "$result" "0" "no .loom/config.json at all resolves to disabled"

# --- Test 2: config true -> enabled ---
echo "Test 2: host.preventSleep=true in config -> enabled"
echo '{"host": {"preventSleep": true}}' > "$repo/.loom/config.json"
result=$( (unset LOOM_HOST_PREVENT_SLEEP; loom_host_prevent_sleep_enabled "$repo") )
assert_eq "$result" "1" "host.preventSleep=true resolves to enabled"

# --- Test 3: config false -> disabled ---
echo "Test 3: host.preventSleep=false in config -> disabled"
echo '{"host": {"preventSleep": false}}' > "$repo/.loom/config.json"
result=$( (unset LOOM_HOST_PREVENT_SLEEP; loom_host_prevent_sleep_enabled "$repo") )
assert_eq "$result" "0" "host.preventSleep=false resolves to disabled"

# --- Test 4: env override wins over config, both directions ---
echo "Test 4: LOOM_HOST_PREVENT_SLEEP env overrides config in both directions"
echo '{"host": {"preventSleep": false}}' > "$repo/.loom/config.json"
result=$(LOOM_HOST_PREVENT_SLEEP=1 loom_host_prevent_sleep_enabled "$repo")
assert_eq "$result" "1" "LOOM_HOST_PREVENT_SLEEP=1 wins over host.preventSleep=false"
echo '{"host": {"preventSleep": true}}' > "$repo/.loom/config.json"
result=$(LOOM_HOST_PREVENT_SLEEP=0 loom_host_prevent_sleep_enabled "$repo")
assert_eq "$result" "0" "LOOM_HOST_PREVENT_SLEEP=0 wins over host.preventSleep=true"

# --- Test 5: alternate true/false spellings accepted ---
echo "Test 5: recognizable true/false spellings"
for spelling in yes on TRUE True; do
    result=$(LOOM_HOST_PREVENT_SLEEP="$spelling" loom_host_prevent_sleep_enabled "$repo")
    assert_eq "$result" "1" "LOOM_HOST_PREVENT_SLEEP=$spelling resolves to enabled"
done
for spelling in no off FALSE False; do
    result=$(LOOM_HOST_PREVENT_SLEEP="$spelling" loom_host_prevent_sleep_enabled "$repo")
    assert_eq "$result" "0" "LOOM_HOST_PREVENT_SLEEP=$spelling resolves to disabled"
done

# --- Test 6: malformed value warns to stderr and falls back to disabled ---
echo "Test 6: malformed value never blocks -- warns and falls back to disabled"
result=$(LOOM_HOST_PREVENT_SLEEP="banana" loom_host_prevent_sleep_enabled "$repo" 2>/dev/null)
assert_eq "$result" "0" "a malformed env value falls back to disabled"
stderr_out=$(LOOM_HOST_PREVENT_SLEEP="banana" loom_host_prevent_sleep_enabled "$repo" 2>&1 1>/dev/null)
assert_contains "WARNING" "$stderr_out" "a malformed env value warns to stderr"
assert_contains "malformed" "$stderr_out" "the warning names the value as malformed"

echo '{"host": {"preventSleep": "banana"}}' > "$repo/.loom/config.json"
result=$( (unset LOOM_HOST_PREVENT_SLEEP; loom_host_prevent_sleep_enabled "$repo") 2>/dev/null )
assert_eq "$result" "0" "a malformed config value falls back to disabled"

# --- Test 7: sleepMitigationAcknowledged resolution ---
echo "Test 7: loom_host_sleep_mitigation_acknowledged precedence"
echo '{}' > "$repo/.loom/config.json"
result=$( (unset LOOM_HOST_SLEEP_MITIGATION_ACKNOWLEDGED; loom_host_sleep_mitigation_acknowledged "$repo") )
assert_eq "$result" "" "absent config -> empty acknowledgement"

echo '{"host": {"sleepMitigationAcknowledged": "pmset sleep=0 set at image build"}}' > "$repo/.loom/config.json"
result=$( (unset LOOM_HOST_SLEEP_MITIGATION_ACKNOWLEDGED; loom_host_sleep_mitigation_acknowledged "$repo") )
assert_eq "$result" "pmset sleep=0 set at image build" "config value is returned verbatim"

result=$(LOOM_HOST_SLEEP_MITIGATION_ACKNOWLEDGED="env override text" loom_host_sleep_mitigation_acknowledged "$repo")
assert_eq "$result" "env override text" "env override wins over config"

rm -rf "$repo"

# --- Summary ---
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
