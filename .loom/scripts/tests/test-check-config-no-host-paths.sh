#!/usr/bin/env bash
# test-check-config-no-host-paths.sh — Tests for check-config-no-host-paths.sh
# (#6504).
#
# Covers, via the script's default (no-args) file-discovery path against a
# throwaway repo:
#   1. A committed /home/<user>/... path in .loom/config.json -> exit 2,
#      names the offending file/key/value.
#   2. A committed /Users/<user>/... path in .loom-project/project.json ->
#      exit 2 (both default tiers are scanned).
#   3. No home-directory paths anywhere -> exit 0 (PASS).
#   4. The allowlisted daemon.delegatedTo key under a home dir -> exit 0
#      (not flagged), even though the same file also has a genuine violation
#      alongside it (allowlist is per-key, not per-file).
#   5. Neither default file exists -> exit 0 (nothing to scan is not an
#      error).
#   6. Malformed JSON in a scanned file -> exit 1 (usage/parse error, not a
#      silent pass and not conflated with "violations found").
#   7. .loom-local/local.json is never scanned even when it itself carries a
#      home-directory path (it is the documented HOME for these values).
#
# Every test isolates a throwaway `mktemp -d` repo (never the real repo's
# .loom/config.json).
#
# Usage:
#   ./defaults/scripts/tests/test-check-config-no-host-paths.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPTS_DIR/check-config-no-host-paths.sh"

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
    echo "jq not found on PATH -- skipping test-check-config-no-host-paths.sh"
    exit 0
fi

new_repo() {
    local d
    d="$(mktemp -d)"
    (cd "$d" && git init -q)
    printf '%s\n' "$d"
}

echo "0. --self-test: the script's own built-in fixture suite"
if out="$("$CHECK_SCRIPT" --self-test 2>&1)"; then
    assert_contains "Self-test passed" "$out" "built-in --self-test passes"
else
    fail "built-in --self-test passes" "$out"
fi

# --- Test 1: committed /home/ path in .loom/config.json -> exit 2 ----------
echo "Test 1: a committed /home/<user>/... path in .loom/config.json -> exit 2"
repo=$(new_repo)
mkdir -p "$repo/.loom"
cat > "$repo/.loom/config.json" <<'EOF'
{"observability": {"ingestKeyFile": "/home/ubuntu/.loom/observability/ingest.key"}}
EOF
out=$(cd "$repo" && "$CHECK_SCRIPT" 2>&1)
code=$?
assert_eq "$code" "2" "exit 2 when a home-directory path is committed"
assert_contains "observability.ingestKeyFile" "$out" "output names the offending key"
assert_contains "/home/ubuntu" "$out" "output names the offending value"
rm -rf "$repo"

# --- Test 2: committed /Users/ path in .loom-project/project.json ----------
echo "Test 2: a committed /Users/<user>/... path in .loom-project/project.json -> exit 2"
repo=$(new_repo)
mkdir -p "$repo/.loom-project"
cat > "$repo/.loom-project/project.json" <<'EOF'
{"safehouse": {"socket": "/Users/alice/.loom/safehoused/state/safehoused.sock"}}
EOF
out=$(cd "$repo" && "$CHECK_SCRIPT" 2>&1)
code=$?
assert_eq "$code" "2" "the project tier is scanned too, not just the legacy tier"
assert_contains "safehouse.socket" "$out" "output names the offending key"
rm -rf "$repo"

# --- Test 3: no home-directory paths anywhere -> exit 0 --------------------
echo "Test 3: no home-directory paths anywhere -> exit 0 (PASS)"
repo=$(new_repo)
mkdir -p "$repo/.loom"
cat > "$repo/.loom/config.json" <<'EOF'
{"buildGate": {"command": "bash .loom/scripts/build-gate.sh"},
 "observability": {"ingestKeyFile": "/etc/loom/observability-ingest.key"}}
EOF
out=$(cd "$repo" && "$CHECK_SCRIPT" 2>&1)
code=$?
assert_eq "$code" "0" "a clean tracked config passes"
assert_contains "PASS" "$out" "output starts with PASS"
rm -rf "$repo"

# --- Test 4: allowlisted daemon.delegatedTo does not block, but a sibling --
# --- violation in the SAME file still fires (per-key, not per-file) -------
echo "Test 4: allowlisted daemon.delegatedTo is not flagged; a sibling key still is"
repo=$(new_repo)
mkdir -p "$repo/.loom"
cat > "$repo/.loom/config.json" <<'EOF'
{"daemon": {"delegatedTo": "/Users/alice/GitHub/other-repo"},
 "observability": {"ingestKeyFile": "/Users/alice/.loom/observability/ingest.key"}}
EOF
out=$(cd "$repo" && "$CHECK_SCRIPT" 2>&1)
code=$?
assert_eq "$code" "2" "the non-allowlisted sibling key still fires"
assert_contains "observability.ingestKeyFile" "$out" "flags the non-allowlisted key"
if [[ "$out" == *"daemon.delegatedTo ="* ]]; then
    fail "does not list daemon.delegatedTo as a violation" "$out"
else
    pass "does not list daemon.delegatedTo as a violation"
fi
rm -rf "$repo"

# --- Test 5: neither default tier file exists -> exit 0 --------------------
echo "Test 5: neither .loom/config.json nor .loom-project/project.json exists -> exit 0"
repo=$(new_repo)
out=$(cd "$repo" && "$CHECK_SCRIPT" 2>&1)
code=$?
assert_eq "$code" "0" "nothing to scan is not an error"
rm -rf "$repo"

# --- Test 6: malformed JSON -> exit 1 (usage/parse error) ------------------
echo "Test 6: malformed JSON in a scanned file -> exit 1 (not silently clean, not a violation)"
repo=$(new_repo)
mkdir -p "$repo/.loom"
printf '{not valid json' > "$repo/.loom/config.json"
out=$(cd "$repo" && "$CHECK_SCRIPT" 2>&1)
code=$?
assert_eq "$code" "1" "unparseable tracked config is a usage error, distinct from exit 2 (violations found)"
rm -rf "$repo"

# --- Test 7: .loom-local/local.json is never scanned -----------------------
echo "Test 7: .loom-local/local.json itself is never scanned (it IS the documented home)"
repo=$(new_repo)
mkdir -p "$repo/.loom" "$repo/.loom-local"
cat > "$repo/.loom/config.json" <<'EOF'
{"buildGate": {"command": "bash .loom/scripts/build-gate.sh"}}
EOF
cat > "$repo/.loom-local/local.json" <<'EOF'
{"observability": {"ingestKeyFile": "/home/ubuntu/.loom/observability/ingest.key"}}
EOF
out=$(cd "$repo" && "$CHECK_SCRIPT" 2>&1)
code=$?
assert_eq "$code" "0" "a home-directory path in the gitignored local tier is never flagged"
rm -rf "$repo"

# --- Summary -----------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
