#!/usr/bin/env bash
# test-config-resolver.sh — Tests for the config resolution layer (#4039,
# Epic #3835 Phase 2).
#
# Covers defaults/scripts/lib/config-resolver.sh:
#
#   1. loom_resolve_config — tier precedence (local > project > legacy >
#      private defaults), soft-fail on missing/malformed tiers, disjoint-key
#      union, nested-object merge, and behavior preservation when only the
#      legacy tier is present (byte-for-byte identical to that file's
#      content).
#   2. loom_config_get — dotted-path lookup, default fallback on a missing
#      path, and soft-fail (not a crash) when the path indexes through a
#      scalar.
#
# Uses a throwaway `mktemp -d` repo_root per test, matching the pattern in
# test-disk-headroom.sh / test-worktree-root-override.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_RESOLVER_LIB="$SCRIPTS_DIR/lib/config-resolver.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

# shellcheck source=../lib/config-resolver.sh
source "$CONFIG_RESOLVER_LIB"

# Always disable the private-defaults tier for deterministic test output --
# it would otherwise read whatever happens to exist at
# ~/.local/share/loom/config/defaults.json on the CI/dev host.
export LOOM_CONFIG_DEFAULTS_FILE=""

if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found on PATH -- skipping test-config-resolver.sh"
    exit 0
fi

new_repo() {
    mktemp -d
}

# --- Test 1: only legacy tier present -> matches legacy content exactly ---
echo "Test 1: only .loom/config.json present -> byte-for-byte behavior preservation"
repo=$(new_repo)
mkdir -p "$repo/.loom"
echo '{"nextAgentNumber": 3, "autonomous": {"perTokenConcurrency": 2}}' > "$repo/.loom/config.json"

actual=$(loom_resolve_config "$repo")
expected=$(jq -c -n '{"nextAgentNumber": 3, "autonomous": {"perTokenConcurrency": 2}}')
assert_eq "$actual" "$expected" "legacy-only repo resolves to exactly the legacy file's content"
rm -rf "$repo"

# --- Test 2: no files present -> empty object ---
echo "Test 2: no config files present -> {}"
repo=$(new_repo)
assert_eq "$(loom_resolve_config "$repo")" "{}" "no tiers present -> empty object"
rm -rf "$repo"

# --- Test 3: malformed legacy tier soft-fails without aborting ---
echo "Test 3: malformed .loom/config.json soft-fails to {} for that tier"
repo=$(new_repo)
mkdir -p "$repo/.loom" "$repo/.loom-project"
echo '{not json' > "$repo/.loom/config.json"
echo '{"buildGate": {"enabled": true}}' > "$repo/.loom-project/project.json"

actual=$(loom_resolve_config "$repo")
expected=$(jq -c -n '{"buildGate": {"enabled": true}}')
assert_eq "$actual" "$expected" "malformed legacy tier contributes nothing; project tier still resolves"
rm -rf "$repo"

# --- Test 4: precedence -- local overrides project overrides legacy ---
echo "Test 4: tier precedence (local > project > legacy)"
repo=$(new_repo)
mkdir -p "$repo/.loom" "$repo/.loom-project" "$repo/.loom-local"
echo '{"a": "legacy", "shared": 1}' > "$repo/.loom/config.json"
echo '{"a": "project", "shared": 2}' > "$repo/.loom-project/project.json"
echo '{"a": "local"}' > "$repo/.loom-local/local.json"

effective=$(loom_resolve_config "$repo")
assert_eq "$(echo "$effective" | jq -r '.a')" "local" "key set at all 3 tiers -> local (highest) wins"
assert_eq "$(echo "$effective" | jq -r '.shared')" "2" "key set at legacy+project -> project wins over legacy"
rm -rf "$repo"

# --- Test 5: disjoint keys across tiers all present ---
echo "Test 5: disjoint keys across tiers -> union"
repo=$(new_repo)
mkdir -p "$repo/.loom" "$repo/.loom-project" "$repo/.loom-local"
echo '{"legacyOnly": 1}' > "$repo/.loom/config.json"
echo '{"projectOnly": 2}' > "$repo/.loom-project/project.json"
echo '{"localOnly": 3}' > "$repo/.loom-local/local.json"

actual=$(loom_resolve_config "$repo")
expected=$(jq -c -n '{"legacyOnly": 1, "projectOnly": 2, "localOnly": 3}')
assert_eq "$actual" "$expected" "disjoint keys across all 3 tiers all present in result"
rm -rf "$repo"

# --- Test 6: nested object merges across tiers ---
echo "Test 6: nested object (autonomous block) merges recursively across tiers"
repo=$(new_repo)
mkdir -p "$repo/.loom" "$repo/.loom-local"
echo '{"autonomous": {"workFinder": {"enabled": true}}}' > "$repo/.loom/config.json"
echo '{"autonomous": {"perTokenConcurrency": 4}}' > "$repo/.loom-local/local.json"

actual=$(loom_resolve_config "$repo")
expected=$(jq -c -n '{"autonomous": {"workFinder": {"enabled": true}, "perTokenConcurrency": 4}}')
assert_eq "$actual" "$expected" "nested autonomous block merges recursively, not overwritten wholesale"
rm -rf "$repo"

# --- Test 7: private defaults tier contributes when present ---
echo "Test 7: private-defaults tier contributes when pointed at a real file"
repo=$(new_repo)
mkdir -p "$repo/.loom"
defaults_file=$(mktemp)
echo '{"fromDefaults": true, "shared": "default"}' > "$defaults_file"
echo '{"shared": "legacy"}' > "$repo/.loom/config.json"

actual=$(LOOM_CONFIG_DEFAULTS_FILE="$defaults_file" loom_resolve_config "$repo")
assert_eq "$(echo "$actual" | jq -r '.fromDefaults')" "true" "private-defaults key present when no higher tier overrides it"
assert_eq "$(echo "$actual" | jq -r '.shared')" "legacy" "legacy tier overrides private defaults"
rm -rf "$repo" "$defaults_file"

# --- Test 8: loom_config_get dotted-path lookup ---
echo "Test 8: loom_config_get resolves a nested dotted path"
repo=$(new_repo)
mkdir -p "$repo/.loom"
echo '{"autonomous": {"workFinder": {"enabled": true}}}' > "$repo/.loom/config.json"

assert_eq "$(loom_config_get "$repo" "autonomous.workFinder.enabled")" "true" "nested dotted path resolves"
rm -rf "$repo"

# --- Test 9: loom_config_get default fallback on missing path ---
echo "Test 9: loom_config_get falls back to default on a missing path"
repo=$(new_repo)
assert_eq "$(loom_config_get "$repo" "missing.path" "fallback")" "fallback" "missing path -> default"
assert_eq "$(loom_config_get "$repo" "missing.path")" "" "missing path, no default -> empty string"
rm -rf "$repo"

# --- Test 10: loom_config_get soft-fails (not crash) indexing through a scalar ---
echo "Test 10: loom_config_get soft-fails when a path segment indexes through a scalar"
repo=$(new_repo)
mkdir -p "$repo/.loom"
echo '{"a": 1}' > "$repo/.loom/config.json"

# Must not abort under set -e: run in a subshell that itself has set -e to
# prove the caller's errexit survives the (invalid) getpath() traversal.
result=$(
    set -euo pipefail
    source "$CONFIG_RESOLVER_LIB"
    export LOOM_CONFIG_DEFAULTS_FILE=""
    loom_config_get "$repo" "a.b" "safe-default"
)
assert_eq "$result" "safe-default" "indexing through a scalar soft-fails to default under set -e (does not abort)"
rm -rf "$repo"

# --- Test 11: cross-language conformance fixture (#4039 AC) ---
# The same fixture tree must resolve identically from Rust and Bash -- see
# defaults/scripts/tests/fixtures/config_resolver/README.md. Rust:
# loom-daemon/src/config_resolver.rs
# (test_conformance_fixture_matches_expected_json). (A Python resolver was a
# third consumer until #4970 retired loom-tools/, which is also when this
# fixture relocated from loom-tools/tests/fixtures/config_resolver/ to its
# current path below.)
echo "Test 11: cross-language conformance fixture resolves to the shared expected.json"
FIXTURE_DIR="$SCRIPTS_DIR/tests/fixtures/config_resolver"

if [[ -d "$FIXTURE_DIR" ]]; then
    actual=$(loom_resolve_config "$FIXTURE_DIR" | jq -S -c .)
    expected=$(jq -S -c . "$FIXTURE_DIR/expected.json")
    assert_eq "$actual" "$expected" "Bash resolver matches the cross-language conformance fixture's expected.json"
else
    fail "conformance fixture directory not found at $FIXTURE_DIR"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
