#!/usr/bin/env bash
# test-forge-helpers.sh - Unit tests for forge-helpers.sh dispatch logic
#
# Tests forge detection, host extraction, and verifies that forge dispatch
# functions route to the correct backend based on FORGE_TYPE.
#
# Usage:
#   ./.loom/scripts/tests/test-forge-helpers.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
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

# --- Test _extract_host ---
echo "Testing _extract_host..."

# Need to source the library
source "$HELPERS_DIR/lib/forge-helpers.sh"

# Reset state for testing
FORGE_TYPE=""

result=$(_extract_host "git@github.com:owner/repo.git")
assert_eq "github.com" "$result" "SSH GitHub URL"

result=$(_extract_host "https://github.com/owner/repo.git")
assert_eq "github.com" "$result" "HTTPS GitHub URL"

result=$(_extract_host "git@gitea.example.com:owner/repo.git")
assert_eq "gitea.example.com" "$result" "SSH Gitea URL"

result=$(_extract_host "https://gitea.example.com/owner/repo")
assert_eq "gitea.example.com" "$result" "HTTPS Gitea URL (no .git)"

result=$(_extract_host "not-a-url")
assert_eq "" "$result" "Invalid URL returns empty"

# --- Test forge_detect with env var ---
echo ""
echo "Testing forge_detect with LOOM_FORGE_TYPE env var..."

FORGE_TYPE=""
LOOM_FORGE_TYPE="github" forge_detect
assert_eq "github" "$FORGE_TYPE" "LOOM_FORGE_TYPE=github"

FORGE_TYPE=""
LOOM_FORGE_TYPE="gitea" forge_detect 2>/dev/null || true
# Note: this may fail if no Gitea config, but FORGE_TYPE should still be set
assert_eq "gitea" "$FORGE_TYPE" "LOOM_FORGE_TYPE=gitea"

# --- Test forge_split_nwo ---
echo ""
echo "Testing forge_split_nwo..."

forge_split_nwo "myowner/myrepo"
assert_eq "myowner" "$FORGE_OWNER" "Split NWO owner"
assert_eq "myrepo" "$FORGE_REPO" "Split NWO repo"

forge_split_nwo "org/complex-repo-name"
assert_eq "org" "$FORGE_OWNER" "Split NWO org owner"
assert_eq "complex-repo-name" "$FORGE_REPO" "Split NWO complex repo"

# --- Test forge detection defaults to github ---
echo ""
echo "Testing forge_detect defaults..."

FORGE_TYPE=""
# Unset LOOM_FORGE_TYPE to test auto-detection
unset LOOM_FORGE_TYPE 2>/dev/null || true
export LOOM_FORGE_TYPE=""
forge_detect
# In this repo (github.com remote), should detect as github
assert_eq "github" "$FORGE_TYPE" "Auto-detect defaults to github for github.com remote"

# --- Test forge_get_repo_nwo for github ---
echo ""
echo "Testing forge_get_repo_nwo..."

FORGE_TYPE="github"
result=$(forge_get_repo_nwo "gh" 2>/dev/null || echo "")
# Should return non-empty for this repo
if [[ -n "$result" ]]; then
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_get_repo_nwo returns non-empty for GitHub ($result)"
else
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_get_repo_nwo returned empty"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
