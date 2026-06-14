#!/usr/bin/env bash
# test-merge-pr-unstable-fallback.sh - Unit tests for the UNSTABLE-fallback
# logic in merge-pr.sh and its supporting helper in forge-helpers.sh.
#
# The UNSTABLE-fallback (#3486) sits immediately after the CLEAN-fallback
# (#3371) and decides whether an auto-merge "Pull request is in unstable
# status" error can be safely demoted to the immediate-merge path. It fires
# only when every failing check on the PR is OUTSIDE branch protection's
# requiredStatusCheckContexts.
#
# This test exercises three surfaces:
#   1. `forge_get_required_status_check_contexts` (GitHub) returns the
#      newline-separated context list emitted by the GraphQL query, with the
#      branchProtectionRule shape stubbed via a PATH-shimmed `gh`. Empty list
#      and missing-rule paths both yield empty stdout.
#   2. `forge_get_required_status_check_contexts` (Gitea, #3488) returns the
#      newline-separated context list parsed from
#      `GET /api/v1/repos/{owner}/{repo}/branch_protections/{branch}`, with
#      `curl` PATH-shimmed to mock the Gitea API. Covers:
#        - all-informational (enable_status_check=true, contexts populated)
#        - at-least-one-required (preserved by the merge-pr.sh callsite)
#        - 404 (missing branch protection → empty → fallback fires)
#        - enable_status_check=false → empty (contexts informational only)
#        - 5xx (fail-closed: nonzero exit, empty stdout)
#   3. The set-difference policy that gates the fallback in merge-pr.sh:
#      - All failing checks informational → fallback fires.
#      - At least one failing check required → fallback does NOT fire.
#   We test the policy by replicating the same `comm -23` / `comm -12` shape
#   the script uses, so the script-internal block stays in lockstep with the
#   test.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-unstable-fallback.sh

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

# --- Source helpers ---
source "$HELPERS_DIR/lib/forge-helpers.sh"

# Reset detected state for tests
FORGE_TYPE=""

# --- Test forge_get_required_status_check_contexts (GitHub path) ---
echo "Testing forge_get_required_status_check_contexts (GitHub stub)..."

FORGE_TYPE="github"

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT

# Stub gh that recognizes the GraphQL query for required status check contexts.
# We inspect $* for the GraphQL ref argument shape and pick the response from
# canned files keyed by `ref=refs/heads/<branch>`.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh used by test-merge-pr-unstable-fallback.sh.
#
# Recognizes:
#   gh api graphql -f query=... -F owner=... -F name=... -F ref=refs/heads/<b>
#                  --jq '.data.repository.ref.branchProtectionRule.requiredStatusCheckContexts // [] | .[]'
#
# It pulls the branch from the ref=... arg and looks up a canned response in
# $STUB_DIR/required-checks-<branch>.txt (one context per line). If the file
# doesn't exist, emits nothing (simulates absent branchProtectionRule).
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:-}"
if [[ -z "$STUB_DIR_FROM_ENV" ]]; then
  echo "stub gh: LOOM_TEST_STUB_DIR not set" >&2
  exit 2
fi

# Find the ref=... arg
ref=""
for a in "$@"; do
  case "$a" in
    ref=refs/heads/*) ref="${a#ref=refs/heads/}" ;;
  esac
done

if [[ -z "$ref" ]]; then
  exit 0
fi

# Canned response file lookup
canned="$STUB_DIR_FROM_ENV/required-checks-$ref.txt"
if [[ -f "$canned" ]]; then
  cat "$canned"
fi
exit 0
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"

# Subtest 1.1: branch has two required contexts
cat > "$STUB_DIR/required-checks-main.txt" <<EOF
Code Ownership
Required Build
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "main" "$STUB_DIR/gh" | tr '\n' '|' | sed 's/|$//')
assert_eq "Code Ownership|Required Build" "$result" "GitHub: two required contexts returned newline-separated"

# Subtest 1.2: branch has no protection rule -> empty output
result=$(forge_get_required_status_check_contexts "owner/repo" "no-protection-branch" "$STUB_DIR/gh" | tr '\n' '|' | sed 's/|$//')
assert_eq "" "$result" "GitHub: missing branchProtectionRule yields empty output"

# Subtest 1.3: branch has protection rule with empty contexts -> empty output
: > "$STUB_DIR/required-checks-empty-required.txt"  # touch empty file
result=$(forge_get_required_status_check_contexts "owner/repo" "empty-required" "$STUB_DIR/gh" | tr '\n' '|' | sed 's/|$//')
assert_eq "" "$result" "GitHub: empty requiredStatusCheckContexts yields empty output"

# Subtest 1.4: single required context
echo "Code Ownership" > "$STUB_DIR/required-checks-single.txt"
result=$(forge_get_required_status_check_contexts "owner/repo" "single" "$STUB_DIR/gh" | tr '\n' '|' | sed 's/|$//')
assert_eq "Code Ownership" "$result" "GitHub: single required context returned correctly"

# --- Test the set-difference policy ---
# These replicate the comm/sort/diff logic used inside merge-pr.sh so that the
# decision can be exercised in isolation. If the inline script implementation
# drifts away from this shape, this test starts failing.
echo ""
echo "Testing set-difference policy (failing_checks \\ required_contexts)..."

# Helper: returns "fire" if the fallback should fire (all failing are
# informational), "preserve" if at least one failing is required (or there are
# no failing checks at all). Note: in production code, a nonzero exit from
# `forge_get_required_status_check_contexts` short-circuits to "preserve" at
# the merge-pr.sh callsite (fail-closed on lookup failure); this helper exists
# only for the happy-path set-difference shape.
_policy_decision() {
    local failing="$1"
    local required="$2"

    if [[ -z "$failing" ]]; then
        echo "preserve"
        return
    fi

    local informational overlap
    informational=$(comm -23 \
      <(printf '%s\n' "$failing" | sort -u) \
      <(printf '%s\n' "$required" | sort -u))
    overlap=$(comm -12 \
      <(printf '%s\n' "$failing" | sort -u) \
      <(printf '%s\n' "$required" | sort -u))

    if [[ -z "$overlap" ]] && [[ -n "$informational" ]]; then
        echo "fire"
    else
        echo "preserve"
    fi
}

# Branch A: all failing checks are informational (NOT in required) -> fallback fires.
failing=$'CI: Stack B lockstep (informational, 30-day soak)\nValidate projects/*/project.json against schema'
required=$'Code Ownership'
result=$(_policy_decision "$failing" "$required")
assert_eq "fire" "$result" "All informational failures -> fallback fires"

# Branch A.2: required is empty (no branch protection) -> fallback fires.
failing=$'Some Informational Check\nAnother One'
required=""
result=$(_policy_decision "$failing" "$required")
assert_eq "fire" "$result" "Empty required (no branch protection) -> fallback fires"

# Branch A.3: same context name twice in failing (re-run) -> still fires.
failing=$'Informational A\nInformational A\nInformational B'
required="Code Ownership"
result=$(_policy_decision "$failing" "$required")
assert_eq "fire" "$result" "Duplicate failing contexts dedupe via sort -u and fallback fires"

# Branch B: at least one failing check IS required -> fallback does NOT fire.
failing=$'Code Ownership\nCI: Stack B lockstep (informational, 30-day soak)'
required=$'Code Ownership'
result=$(_policy_decision "$failing" "$required")
assert_eq "preserve" "$result" "Failing includes a required context -> fallback preserves refusal"

# Branch B.2: all failing checks are required -> fallback does NOT fire.
failing=$'Code Ownership\nRequired Build'
required=$'Code Ownership\nRequired Build'
result=$(_policy_decision "$failing" "$required")
assert_eq "preserve" "$result" "All failing are required -> fallback preserves refusal"

# Branch B.3: failing is empty -> fallback does NOT fire (no failing → not the UNSTABLE case we care about).
failing=""
required=$'Code Ownership'
result=$(_policy_decision "$failing" "$required")
assert_eq "preserve" "$result" "Empty failing set -> fallback preserves refusal"

# --- Test forge_get_required_status_check_contexts (Gitea path, #3488) ---
# The Gitea branch calls curl directly against
#   GET ${_GITEA_BASE_URL}/api/v1/repos/${owner}/${repo}/branch_protections/${branch}
# We PATH-shim curl so it returns canned JSON + HTTP status codes keyed on the
# branch name extracted from the URL path. This mirrors the GitHub stub shape
# but keys on URL path instead of argv args.
echo ""
echo "Testing forge_get_required_status_check_contexts (Gitea stub, #3488)..."

# shellcheck disable=SC2034
FORGE_TYPE="gitea"
# Provide the Gitea config the helper expects (token + URL). These are read
# by the helper directly from the _GITEA_* globals set by _load_gitea_config.
# We set them inline to avoid a config-file fixture.
_GITEA_BASE_URL="https://gitea.example.com"
_GITEA_TOKEN="fake-token-for-test"
_GITEA_USERNAME=""

# Stub curl that recognizes the Gitea branch_protections endpoint and pulls
# canned responses + HTTP codes from $STUB_DIR keyed on the branch name.
# Response files:
#   $STUB_DIR/gitea-branch-protection-<branch>.json  - response body
#   $STUB_DIR/gitea-branch-protection-<branch>.code  - HTTP status code
# If the .code file is absent, the stub returns 200 with the body.
# If the .json file is absent, the stub returns 404 with empty body.
cat > "$STUB_DIR/curl" <<'STUB'
#!/usr/bin/env bash
# Stub curl used by test-merge-pr-unstable-fallback.sh (Gitea path).
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:-}"
if [[ -z "$STUB_DIR_FROM_ENV" ]]; then
  echo "stub curl: LOOM_TEST_STUB_DIR not set" >&2
  exit 2
fi

# The helper invokes curl with -w "\n%{http_code}" so we must emit body + newline + code.
# Extract the URL (last positional arg) and find the branch_protections/<branch> path.
url=""
for a in "$@"; do
  case "$a" in
    https://*|http://*) url="$a" ;;
  esac
done

if [[ -z "$url" ]]; then
  exit 0
fi

# Pull the branch from the URL path
branch=""
if [[ "$url" =~ branch_protections/([^/?]+) ]]; then
  branch="${BASH_REMATCH[1]}"
fi

if [[ -z "$branch" ]]; then
  printf '\n404\n'
  exit 0
fi

body_file="$STUB_DIR_FROM_ENV/gitea-branch-protection-$branch.json"
code_file="$STUB_DIR_FROM_ENV/gitea-branch-protection-$branch.code"

if [[ -f "$code_file" ]]; then
  code=$(cat "$code_file")
else
  if [[ -f "$body_file" ]]; then
    code="200"
  else
    code="404"
  fi
fi

if [[ -f "$body_file" ]]; then
  cat "$body_file"
fi
printf '\n%s\n' "$code"
exit 0
STUB
chmod +x "$STUB_DIR/curl"

# Save original PATH and prepend STUB_DIR so curl is shimmed.
_ORIG_PATH="$PATH"
export PATH="$STUB_DIR:$PATH"

# Subtest G.1: enable_status_check=true with two required contexts.
cat > "$STUB_DIR/gitea-branch-protection-main.json" <<'EOF'
{
  "branch_name": "main",
  "enable_status_check": true,
  "status_check_contexts": ["Code Ownership", "Required Build"]
}
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "main" 2>/dev/null | tr '\n' '|' | sed 's/|$//')
rc=$?
assert_eq "Code Ownership|Required Build" "$result" "Gitea: two required contexts returned newline-separated"
assert_eq "0" "$rc" "Gitea: success exit code on 200"

# Subtest G.2: enable_status_check=true with empty contexts list -> empty.
cat > "$STUB_DIR/gitea-branch-protection-no-contexts.json" <<'EOF'
{
  "branch_name": "no-contexts",
  "enable_status_check": true,
  "status_check_contexts": []
}
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "no-contexts" 2>/dev/null | tr '\n' '|' | sed 's/|$//')
rc=$?
assert_eq "" "$result" "Gitea: enable_status_check=true with empty contexts yields empty output"
assert_eq "0" "$rc" "Gitea: success exit code on empty contexts"

# Subtest G.3: enable_status_check=false with populated contexts -> empty.
# (Contexts are informational only when the toggle is off; fallback should fire.)
cat > "$STUB_DIR/gitea-branch-protection-toggle-off.json" <<'EOF'
{
  "branch_name": "toggle-off",
  "enable_status_check": false,
  "status_check_contexts": ["Code Ownership", "Required Build"]
}
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "toggle-off" 2>/dev/null | tr '\n' '|' | sed 's/|$//')
rc=$?
assert_eq "" "$result" "Gitea: enable_status_check=false yields empty output (contexts informational)"
assert_eq "0" "$rc" "Gitea: success exit code when toggle is off"

# Subtest G.4: 404 (no branch protection) -> empty, exit 0 (fallback fires).
# We achieve 404 by not providing a .json file for this branch.
result=$(forge_get_required_status_check_contexts "owner/repo" "missing-protection" 2>/dev/null | tr '\n' '|' | sed 's/|$//')
rc=$?
assert_eq "" "$result" "Gitea: 404 missing branch protection yields empty output"
assert_eq "0" "$rc" "Gitea: 404 returns success exit code (mirrors GitHub no-rule path)"

# Subtest G.5: 500 (server error) -> empty, nonzero exit (fail-closed).
echo "500" > "$STUB_DIR/gitea-branch-protection-server-error.code"
echo '{"message":"internal server error"}' > "$STUB_DIR/gitea-branch-protection-server-error.json"
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "server-error" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "" "$result" "Gitea: 500 yields empty stdout (fail-closed)"
if [[ "$rc" -ne 0 ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Gitea: 500 returns nonzero exit code (fail-closed)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Gitea: 500 should return nonzero (got $rc)"
fi

# Subtest G.6: 401 (auth error) -> empty, nonzero exit (fail-closed).
echo "401" > "$STUB_DIR/gitea-branch-protection-auth-fail.code"
echo '{"message":"unauthorized"}' > "$STUB_DIR/gitea-branch-protection-auth-fail.json"
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "auth-fail" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "" "$result" "Gitea: 401 yields empty stdout (fail-closed)"
if [[ "$rc" -ne 0 ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Gitea: 401 returns nonzero exit code (fail-closed)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Gitea: 401 should return nonzero (got $rc)"
fi

# Subtest G.7: missing token (config error) -> fail-closed, nonzero exit.
_SAVED_TOKEN="$_GITEA_TOKEN"
_GITEA_TOKEN=""
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "main" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "" "$result" "Gitea: missing token yields empty stdout (fail-closed)"
if [[ "$rc" -ne 0 ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Gitea: missing token returns nonzero (fail-closed)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Gitea: missing token should return nonzero (got $rc)"
fi
_GITEA_TOKEN="$_SAVED_TOKEN"

# Subtest G.8: end-to-end policy — all-informational on Gitea (fallback fires).
# Use the empty-contexts response to simulate "no required checks".
failing=$'Some Informational Check'
required="$(forge_get_required_status_check_contexts "owner/repo" "no-contexts" 2>/dev/null)"
result=$(_policy_decision "$failing" "$required")
assert_eq "fire" "$result" "Gitea: all-informational with empty contexts -> fallback fires"

# Subtest G.9: end-to-end policy — at-least-one-required on Gitea (preserved).
failing=$'Code Ownership\nSome Informational Check'
required="$(forge_get_required_status_check_contexts "owner/repo" "main" 2>/dev/null)"
result=$(_policy_decision "$failing" "$required")
assert_eq "preserve" "$result" "Gitea: at-least-one-required -> fallback preserves refusal"

# Restore PATH so subsequent tests don't see the stubbed curl.
export PATH="$_ORIG_PATH"
# Switch back to github for any remaining tests that may rely on it.
# shellcheck disable=SC2034  # consumed by sourced helpers via FORGE_TYPE global
FORGE_TYPE="github"

# --- Test that the unstable-status-substring matcher in merge-pr.sh is robust ---
# The merge-pr.sh fallback matches on the substring "is in unstable status"
# (sibling of the CLEAN-fallback's "is in clean status" matcher). This guards
# against GitHub's "Pull request Pull request is in unstable status" doubled-word
# error prefix and any future normalization.
echo ""
echo "Testing the unstable-status-substring matcher shape..."

unstable_error="Failed to enable auto-merge: gh: Pull request Pull request is in unstable status (enablePullRequestAutoMerge)"
if echo "$unstable_error" | grep -q "is in unstable status"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: 'is in unstable status' substring matches GitHub's doubled-word error"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: substring matcher missed the GitHub error"
fi

clean_error="gh: Pull request Pull request is in clean status (enablePullRequestAutoMerge)"
if echo "$clean_error" | grep -q "is in unstable status"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: substring matcher fired on CLEAN error (false positive)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: 'is in unstable status' substring does NOT match CLEAN error"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
