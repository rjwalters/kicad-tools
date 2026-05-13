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

# --- Test forge_pr_close_targets ---
# Tests the canonical "what issues does this PR close?" helper, which is the
# single source of truth used by the Champion role's verify-issue-closure
# step. See issue #2849.
#
# We test by stubbing `gh` (GitHub path) and `forge_get_pr_body` (Gitea path).
# The stub strategy mirrors how forge_pr_close_targets calls into them.
echo ""
echo "Testing forge_pr_close_targets..."

# Helper to define a one-shot stub `gh` that emits canned JSON for the
# `closingIssuesReferences` query and a different payload for everything else.
_stub_gh_closing_refs() {
    # $1 = JSON string for .closingIssuesReferences (e.g. '[{"number":100}]')
    local refs_json="$1"
    eval "gh() {
        if [[ \"\$*\" == *closingIssuesReferences* ]]; then
            if [[ \"\$*\" == *--jq* ]]; then
                # Emulate \`gh ... --jq '.closingIssuesReferences[].number'\`
                echo '$refs_json' | jq -r '.[].number'
            else
                echo '{\"closingIssuesReferences\": $refs_json}'
            fi
        fi
    }"
    export -f gh 2>/dev/null || true
}

_unstub_gh() {
    unset -f gh 2>/dev/null || true
}

FORGE_TYPE="github"

# Case 1: PR with Closes #100 -> closingIssuesReferences: [{number:100}]
_stub_gh_closing_refs '[{"number":100}]'
result=$(forge_pr_close_targets 1234 gh)
assert_eq "100" "$result" "Closes #100 -> 100"
_unstub_gh

# Case 2: PR with Updates #100 only -> closingIssuesReferences: []
_stub_gh_closing_refs '[]'
result=$(forge_pr_close_targets 1234 gh)
assert_eq "" "$result" "Updates #100 (no Closes) -> empty (the #2849 bug case)"
_unstub_gh

# Case 3: PR with Closes #100 + Updates #200 -> only 100 closes
_stub_gh_closing_refs '[{"number":100}]'
result=$(forge_pr_close_targets 1234 gh)
assert_eq "100" "$result" "Closes #100 + Updates #200 -> 100 only"
_unstub_gh

# Case 4: PR mentioning "Closure of #100" but no actual close keyword -> empty
_stub_gh_closing_refs '[]'
result=$(forge_pr_close_targets 1234 gh)
assert_eq "" "$result" "Closure of #100 (substring) -> empty"
_unstub_gh

# Case 5: Multiple closing references
_stub_gh_closing_refs '[{"number":100},{"number":200},{"number":300}]'
result=$(forge_pr_close_targets 1234 gh | tr '\n' ' ' | sed 's/ $//')
assert_eq "100 200 300" "$result" "Multiple closes -> all numbers"
_unstub_gh

# Case 6: Empty body / no references
_stub_gh_closing_refs '[]'
result=$(forge_pr_close_targets 1234 gh)
assert_eq "" "$result" "Empty body -> empty"
_unstub_gh

# --- Test Gitea body-regex path for forge_pr_close_targets ---
echo ""
echo "Testing forge_pr_close_targets (Gitea body-regex path)..."

# Stub forge_get_pr_body and forge_get_repo_nwo for the Gitea path
_stub_pr_body() {
    local body="$1"
    eval "forge_get_pr_body() { printf '%s' \"\$(cat <<'BODY_EOF'
$body
BODY_EOF
)\"; }"
    eval "forge_get_repo_nwo() { echo 'owner/repo'; }"
}

_unstub_pr_body() {
    # Re-source to restore originals
    source "$HELPERS_DIR/lib/forge-helpers.sh"
}

FORGE_TYPE="gitea"

_stub_pr_body "Closes #100"
result=$(forge_pr_close_targets 1234)
assert_eq "100" "$result" "Gitea: Closes #100 -> 100"
_unstub_pr_body

FORGE_TYPE="gitea"
_stub_pr_body "Updates #100 only, no closure keyword."
result=$(forge_pr_close_targets 1234)
assert_eq "" "$result" "Gitea: Updates #100 only -> empty"
_unstub_pr_body

FORGE_TYPE="gitea"
_stub_pr_body "Closure of this criterion is gated on #200."
result=$(forge_pr_close_targets 1234)
assert_eq "" "$result" "Gitea: 'Closure of' substring -> empty (word boundary)"
_unstub_pr_body

FORGE_TYPE="gitea"
_stub_pr_body "closes #42 and Fixes #43 and resolves #44"
result=$(forge_pr_close_targets 1234 | tr '\n' ' ' | sed 's/ $//')
assert_eq "42 43 44" "$result" "Gitea: mixed-case canonical keywords -> all"
_unstub_pr_body

# Restore FORGE_TYPE
FORGE_TYPE="github"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
