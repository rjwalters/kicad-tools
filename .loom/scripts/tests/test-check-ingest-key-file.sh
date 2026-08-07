#!/usr/bin/env bash
# test-check-ingest-key-file.sh — Tests for check-ingest-key-file.sh (#5336).
#
# Covers:
#   1. No config anywhere, a readable key at the $HOME-relative default path
#      -> PASS.
#   2. No config anywhere, nothing at the default path -> FAIL (unreadable).
#   3. observability.ingestKeyFile in .loom/config.json naming a DIFFERENT
#      host's home -> FAIL (foreign-home path — the #5336 defect class).
#   4. observability.ingestKeyFile naming a non-home system path (e.g.
#      /etc/loom/...) that doesn't exist -> FAIL (unreadable), NOT flagged as
#      foreign-home (system paths are never home-relative).
#   5. Same system path, but the file exists and is readable -> PASS.
#   6. $LOOM_OBSERVABILITY_INGEST_KEY_FILE env var overrides the config value.
#   7. .loom-local/local.json's ingestKeyFile overrides .loom/config.json's
#      (tier precedence, matching config-resolver.sh).
#
# Every test isolates $HOME via a throwaway `mktemp -d` fake home (never the
# real ambient $HOME) and disables the private-defaults tier
# (LOOM_CONFIG_DEFAULTS_FILE="") for determinism, matching
# test-config-resolver.sh's isolation pattern.
#
# Usage:
#   ./defaults/scripts/tests/test-check-ingest-key-file.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPTS_DIR/check-ingest-key-file.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; [[ -n "${2:-}" ]] && echo "    $2"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3" "expected '$2', got '$1'"; fi
}
assert_contains() {
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3" "expected substring '$1' in '$2'"; fi
}

if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found on PATH -- skipping test-check-ingest-key-file.sh"
    exit 0
fi

new_repo() { mktemp -d; }
new_home() { mktemp -d; }

write_key() {
    local path="$1"
    mkdir -p "$(dirname "$path")"
    printf 'test-ingest-key-value\n' > "$path"
    chmod 600 "$path"
}

# --- Test 1: no config, readable key at the $HOME-relative default -> PASS -
echo "Test 1: no config anywhere, readable key at \$HOME default -> PASS"
repo=$(new_repo)
home=$(new_home)
write_key "$home/.loom/observability/ingest.key"

out=$(HOME="$home" LOOM_CONFIG_DEFAULTS_FILE="" "$CHECK_SCRIPT" "$repo" 2>&1)
code=$?
assert_eq "$code" "0" "exit 0 when the default path is readable"
assert_contains "PASS" "$out" "output starts with PASS"
assert_contains "$home/.loom/observability/ingest.key" "$out" "output names the resolved default path"
rm -rf "$repo" "$home"

# --- Test 2: no config, nothing at the default path -> FAIL (unreadable) ---
echo "Test 2: no config anywhere, nothing at \$HOME default -> FAIL unreadable"
repo=$(new_repo)
home=$(new_home)

out=$(HOME="$home" LOOM_CONFIG_DEFAULTS_FILE="" "$CHECK_SCRIPT" "$repo" 2>&1)
code=$?
assert_eq "$code" "2" "exit 2 when nothing is readable at the resolved path"
assert_contains "not a readable file" "$out" "message explains the failure"
rm -rf "$repo" "$home"

# --- Test 3: config value under a DIFFERENT host's home -> FAIL foreign ----
echo "Test 3: ingestKeyFile under a different host's home -> FAIL foreign-home"
repo=$(new_repo)
home=$(new_home)
mkdir -p "$repo/.loom"
cat > "$repo/.loom/config.json" <<'EOF'
{"observability": {"ingestKeyFile": "/Users/a-different-mac-user/.loom/observability/ingest.key"}}
EOF

out=$(HOME="$home" LOOM_CONFIG_DEFAULTS_FILE="" "$CHECK_SCRIPT" "$repo" 2>&1)
code=$?
assert_eq "$code" "1" "exit 1 for a foreign-home path (this is the #5336 defect class)"
assert_contains "DIFFERENT host's home" "$out" "message names the defect"
rm -rf "$repo" "$home"

# --- Test 4: non-home system path, unreadable -> FAIL, NOT foreign-home ----
echo "Test 4: non-home system path that doesn't exist -> FAIL unreadable, not foreign-home"
repo=$(new_repo)
home=$(new_home)
mkdir -p "$repo/.loom"
cat > "$repo/.loom/config.json" <<'EOF'
{"observability": {"ingestKeyFile": "/etc/loom/observability-ingest.key"}}
EOF

out=$(HOME="$home" LOOM_CONFIG_DEFAULTS_FILE="" "$CHECK_SCRIPT" "$repo" 2>&1)
code=$?
assert_eq "$code" "2" "system paths are never flagged foreign-home -- unreadable is the only applicable failure"
rm -rf "$repo" "$home"

# --- Test 5: non-home system path, readable -> PASS -------------------------
echo "Test 5: non-home system path that IS readable -> PASS"
repo=$(new_repo)
home=$(new_home)
key_dir=$(mktemp -d)
write_key "$key_dir/observability-ingest.key"
mkdir -p "$repo/.loom"
cat > "$repo/.loom/config.json" <<EOF
{"observability": {"ingestKeyFile": "$key_dir/observability-ingest.key"}}
EOF

out=$(HOME="$home" LOOM_CONFIG_DEFAULTS_FILE="" "$CHECK_SCRIPT" "$repo" 2>&1)
code=$?
assert_eq "$code" "0" "readable non-home path passes"
rm -rf "$repo" "$home" "$key_dir"

# --- Test 6: env var override wins over config -------------------------------
echo "Test 6: \$LOOM_OBSERVABILITY_INGEST_KEY_FILE overrides the config value"
repo=$(new_repo)
home=$(new_home)
env_key_dir=$(mktemp -d)
write_key "$env_key_dir/env-ingest.key"
mkdir -p "$repo/.loom"
cat > "$repo/.loom/config.json" <<'EOF'
{"observability": {"ingestKeyFile": "/Users/a-different-mac-user/.loom/observability/ingest.key"}}
EOF

out=$(HOME="$home" LOOM_CONFIG_DEFAULTS_FILE="" \
    LOOM_OBSERVABILITY_INGEST_KEY_FILE="$env_key_dir/env-ingest.key" \
    "$CHECK_SCRIPT" "$repo" 2>&1)
code=$?
assert_eq "$code" "0" "env override wins, so the foreign-home config value is never consulted"
assert_contains "env-ingest.key" "$out" "output names the env-resolved path"
rm -rf "$repo" "$home" "$env_key_dir"

# --- Test 7: .loom-local/local.json overrides .loom/config.json ------------
echo "Test 7: .loom-local/local.json's ingestKeyFile overrides the legacy tier"
repo=$(new_repo)
home=$(new_home)
local_key_dir=$(mktemp -d)
write_key "$local_key_dir/local-ingest.key"
mkdir -p "$repo/.loom" "$repo/.loom-local"
cat > "$repo/.loom/config.json" <<'EOF'
{"observability": {"ingestKeyFile": "/Users/a-different-mac-user/.loom/observability/ingest.key"}}
EOF
cat > "$repo/.loom-local/local.json" <<EOF
{"observability": {"ingestKeyFile": "$local_key_dir/local-ingest.key"}}
EOF

out=$(HOME="$home" LOOM_CONFIG_DEFAULTS_FILE="" "$CHECK_SCRIPT" "$repo" 2>&1)
code=$?
assert_eq "$code" "0" "the host-local override tier wins over the committed legacy tier"
assert_contains "local-ingest.key" "$out" "output names the local-tier-resolved path"
rm -rf "$repo" "$home" "$local_key_dir"

# --- Summary -----------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
