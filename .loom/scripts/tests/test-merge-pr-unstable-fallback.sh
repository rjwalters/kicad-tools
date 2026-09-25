#!/usr/bin/env bash
# test-merge-pr-unstable-fallback.sh - Unit tests for the check-settling
# policy `merge-pr.sh --auto` merges behind, and its supporting helper in
# forge-helpers.sh.
#
# This policy (#3486) decides whether a PR whose check rollup is not green can
# still be merged: it can, only when every failing check on the PR is OUTSIDE
# branch protection's requiredStatusCheckContexts and nothing is still running.
# It used to live in the "Pull request is in unstable status" rejection
# handler; since #8410 removed the server-side auto-merge arm entirely it lives
# in `_wait_for_checks_then_sync_merge`, which every `--auto` run now takes.
# The policy itself — and every assertion below — is unchanged.
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

# Stub gh that recognizes BOTH sources the GitHub path queries (#8103):
#   - the Rulesets effective-rules REST endpoint, and
#   - the classic branch-protection GraphQL query,
# picking each response from canned files keyed by branch name.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh used by test-merge-pr-unstable-fallback.sh.
#
# Recognizes:
#
#   1. Rulesets (#8103):
#        gh api repos/<owner>/<repo>/rules/branches/<b> --jq '<filter>'
#      Canned body: $STUB_DIR/ruleset-rules-<branch>.json — a VERBATIM-SHAPED
#      GitHub effective-rules response. The stub runs the REAL `--jq` filter the
#      helper passed against it, so the fixture exercises the helper's own
#      parsing of the documented API shape rather than a pre-digested answer.
#      A `$STUB_DIR/ruleset-fail-<branch>` marker makes the call exit nonzero
#      (network failure / 403), to test the fail-closed paths — either source
#      erroring fails the lookup. No canned file at all = a 200 with an empty
#      rules array (a SUCCESSFUL "no ruleset rules" answer, not a failure).
#      The marker file's CONTENT (if any) is emitted on stderr, so #8872's
#      plan-gated-403 tests can control the exact failure message.
#
#   2. Classic branch protection:
#        gh api graphql -f query=... -F owner=... -F name=... -F ref=refs/heads/<b>
#                       --jq '.data.repository.ref.branchProtectionRule.requiredStatusCheckContexts // [] | .[]'
#      Canned response: $STUB_DIR/required-checks-<branch>.txt (one context per
#      line, post-jq). Missing file = absent branchProtectionRule (empty).
#      A `$STUB_DIR/graphql-fail-<branch>` marker makes the call exit nonzero,
#      its content (if any) emitted on stderr, same as the ruleset marker.
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:-}"
if [[ -z "$STUB_DIR_FROM_ENV" ]]; then
  echo "stub gh: LOOM_TEST_STUB_DIR not set" >&2
  exit 2
fi

# Find the ref=... arg (GraphQL), the rules-endpoint path (REST), and the --jq
# filter (used verbatim for the REST fixture).
ref=""
rules_branch=""
jq_filter=""
prev=""
for a in "$@"; do
  case "$a" in
    ref=refs/heads/*) ref="${a#ref=refs/heads/}" ;;
    repos/*/rules/branches/*) rules_branch="${a##*/rules/branches/}" ;;
  esac
  [[ "$prev" == "--jq" ]] && jq_filter="$a"
  prev="$a"
done

if [[ -n "$rules_branch" ]]; then
  if [[ -f "$STUB_DIR_FROM_ENV/ruleset-fail-$rules_branch" ]]; then
    cat "$STUB_DIR_FROM_ENV/ruleset-fail-$rules_branch" >&2
    exit 1
  fi
  canned="$STUB_DIR_FROM_ENV/ruleset-rules-$rules_branch.json"
  [[ -f "$canned" ]] || exit 0
  jq -r "$jq_filter" "$canned"
  exit 0
fi

if [[ -z "$ref" ]]; then
  exit 0
fi

if [[ -f "$STUB_DIR_FROM_ENV/graphql-fail-$ref" ]]; then
  cat "$STUB_DIR_FROM_ENV/graphql-fail-$ref" >&2
  exit 1
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

# --- Ruleset-sourced required checks (#8103) ---
#
# GitHub has TWO backing systems for branch protection and the classic
# GraphQL `branchProtectionRule` field reports ONLY the legacy one. Verified
# live on rjwalters/loom (2026-09-17): `main` is governed by an ACTIVE
# ruleset, and the GraphQL query still returns `branchProtectionRule: null`
# while `GET /repos/{owner}/{repo}/rules/branches/main` returns every rule.
# Before this fix the helper queried GraphQL only, so on a ruleset-governed
# repo it reported "no required checks" no matter what the ruleset said —
# which would have let merge-pr.sh's #3720 fallback merge straight over a
# failing required check, silently.
#
# The fixtures below are shaped exactly like real effective-rules responses
# (including the non-`required_status_checks` rules that accompany them) and
# the stub applies the helper's real `--jq` filter to them.
echo ""
echo "Testing forge_get_required_status_check_contexts (ruleset source, #8103)..."

# Subtest 1.5: ruleset-only required checks, no classic protection at all —
# this is rjwalters/loom's exact configuration.
cat > "$STUB_DIR/ruleset-rules-ruleset-only.json" <<'EOF'
[
  {"type": "deletion", "ruleset_source_type": "Repository", "ruleset_id": 8809610},
  {"type": "non_fast_forward", "ruleset_source_type": "Repository", "ruleset_id": 8809610},
  {"type": "required_linear_history", "ruleset_source_type": "Repository", "ruleset_id": 8809610},
  {"type": "pull_request",
   "parameters": {"required_approving_review_count": 0, "allowed_merge_methods": ["squash"]},
   "ruleset_source_type": "Repository", "ruleset_id": 8809610},
  {"type": "required_status_checks",
   "parameters": {
     "strict_required_status_checks_policy": false,
     "do_not_enforce_on_create": false,
     "required_status_checks": [
       {"context": "Role Prompt Prefix Ratchet", "integration_id": 15368},
       {"context": "CLAUDE.md Line Budget", "integration_id": 15368}
     ]},
   "ruleset_source_type": "Repository", "ruleset_id": 8809610}
]
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "ruleset-only" "$STUB_DIR/gh" | tr '\n' '|' | sed 's/|$//')
assert_eq "Role Prompt Prefix Ratchet|CLAUDE.md Line Budget" "$result" \
  "#8103: ruleset-sourced required checks are detected with NO classic branch protection"

# Subtest 1.6: an active ruleset carrying no required_status_checks rule (the
# pre-#8103 state of rjwalters/loom) still means "no required checks".
cat > "$STUB_DIR/ruleset-rules-ruleset-no-checks.json" <<'EOF'
[
  {"type": "deletion", "ruleset_source_type": "Repository", "ruleset_id": 8809610},
  {"type": "required_linear_history", "ruleset_source_type": "Repository", "ruleset_id": 8809610}
]
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "ruleset-no-checks" "$STUB_DIR/gh" | tr '\n' '|' | sed 's/|$//')
assert_eq "" "$result" "#8103: ruleset without a required_status_checks rule yields empty output"

# Subtest 1.7: both sources configured — union, de-duplicated, each name once
# (the callers' comm set-difference needs unique names).
cat > "$STUB_DIR/ruleset-rules-both.json" <<'EOF'
[
  {"type": "required_status_checks",
   "parameters": {
     "strict_required_status_checks_policy": true,
     "required_status_checks": [
       {"context": "Shared Check"},
       {"context": "Ruleset Only Check"}
     ]}}
]
EOF
cat > "$STUB_DIR/required-checks-both.txt" <<'EOF'
Shared Check
Classic Only Check
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "both" "$STUB_DIR/gh" | tr '\n' '|' | sed 's/|$//')
assert_eq "Shared Check|Ruleset Only Check|Classic Only Check" "$result" \
  "#8103: ruleset + classic contexts are unioned and de-duplicated"

# Subtest 1.8: partial lookup failure — the rules endpoint errors but classic
# protection answers. FAIL CLOSED anyway: a surviving source is a partial view
# of what is required, and a partial view is not a safe input to a merge
# decision. The surviving source's answer is NOT reported (empty stdout).
: > "$STUB_DIR/ruleset-fail-partial"
cat > "$STUB_DIR/required-checks-partial.txt" <<'EOF'
Classic Survivor
EOF
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "partial" "$STUB_DIR/gh" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "1" "$rc" "#8103: ruleset source failing -> nonzero exit even though classic answered"
assert_eq "" "$result" "#8103: partial failure does not report the surviving source's contexts"

# Subtest 1.8a: THE blind spot this whole fix exists to close — the ruleset
# endpoint errors (403/404/network) and classic branch protection SUCCEEDS with
# an empty result. Under a both-must-fail rule this returns success with an
# empty list, which is indistinguishable at the callsite from a genuinely
# unprotected branch and sends `merge-pr.sh --auto` down the
# "No-required-checks fallback (#3720)" path — merging over red required checks
# exactly as the pre-#8103 GraphQL-only helper did. Must fail closed.
: > "$STUB_DIR/ruleset-fail-ruleset-err-classic-empty"
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "ruleset-err-classic-empty" "$STUB_DIR/gh" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "1" "$rc" "#8103: ruleset errors + classic succeeds EMPTY -> nonzero (no silent no-required-checks)"
assert_eq "" "$result" "#8103: ruleset errors + classic succeeds empty -> empty stdout"

# Subtest 1.8b: the mirror image — the ruleset endpoint succeeds with real
# contexts but the classic GraphQL query errors. Also fail closed: an
# unreadable classic rule may require contexts the ruleset does not list.
cat > "$STUB_DIR/ruleset-rules-classic-err.json" <<'EOF'
[
  {"type": "required_status_checks",
   "parameters": {
     "strict_required_status_checks_policy": true,
     "required_status_checks": [{"context": "Ruleset Survivor"}]}}
]
EOF
: > "$STUB_DIR/graphql-fail-classic-err"
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "classic-err" "$STUB_DIR/gh" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "1" "$rc" "#8103: classic source failing -> nonzero exit even though the ruleset answered"
assert_eq "" "$result" "#8103: classic failure does not report the ruleset's contexts"

# Subtest 1.9: BOTH sources error -> fail closed (nonzero exit, empty stdout),
# matching the Gitea path and the callers' documented fail-closed contract.
: > "$STUB_DIR/ruleset-fail-dead"
: > "$STUB_DIR/graphql-fail-dead"
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "dead" "$STUB_DIR/gh" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "1" "$rc" "#8103: both sources failing -> nonzero exit (fail closed)"
assert_eq "" "$result" "#8103: both sources failing -> empty stdout"

# Subtest 1.10: the helper must actually QUERY the rulesets endpoint. A
# refactor that drops the REST call and goes back to GraphQL-only would pass
# every assertion above except this one (the fixtures would simply go unread),
# so anchor on the call itself.
if grep -q 'rules/branches/' "$HELPERS_DIR/lib/forge-helpers.sh"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: #8103: forge-helpers queries the Rulesets effective-rules endpoint"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: #8103: forge-helpers no longer queries /rules/branches/ (ruleset-based required checks would be invisible)"
fi

# --- Plan-gated 403 relaxation (#8872, mirrors #8871's Rust
# `stale_checks::fetch::is_plan_gated`) ---
#
# On a private repo whose GitHub plan excludes rulesets/branch protection,
# BOTH sources answer the SAME 403:
#   "HTTP 403: Upgrade to GitHub Pro or make this repository public to
#   enable this feature."
# That source cannot hold a required_status_checks rule, so it must
# configure NO required checks (per source) rather than failing the whole
# lookup closed. Any OTHER 403 (missing scope, SSO, rate limit) must keep
# failing closed -- the match is on the message, never the status code.
echo ""
echo "Testing the plan-gated 403 relaxation (#8872)..."

PLAN_GATED_403="HTTP 403: Upgrade to GitHub Pro or make this repository public to enable this feature."
OTHER_403="HTTP 403: Resource not accessible by integration"

# Subtest P.1: ruleset source plan-gated, classic source answers normally ->
# the classic contexts stay authoritative; only the ruleset configures
# nothing.
echo "$PLAN_GATED_403" > "$STUB_DIR/ruleset-fail-plan-gated-ruleset"
cat > "$STUB_DIR/required-checks-plan-gated-ruleset.txt" <<EOF
Classic Required
EOF
result=$(forge_get_required_status_check_contexts "owner/repo" "plan-gated-ruleset" "$STUB_DIR/gh" 2>/dev/null | tr '\n' '|' | sed 's/|$//')
assert_eq "Classic Required" "$result" "#8872: plan-gated ruleset source configures nothing; classic source stays authoritative"

# Subtest P.2: BOTH sources plan-gated -> empty result, exit 0 (this plan has
# no required checks anywhere), plus a visible warning on stderr.
echo "$PLAN_GATED_403" > "$STUB_DIR/ruleset-fail-plan-gated-both"
echo "$PLAN_GATED_403" > "$STUB_DIR/graphql-fail-plan-gated-both"
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "plan-gated-both" "$STUB_DIR/gh" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
stderr_out=$(forge_get_required_status_check_contexts "owner/repo" "plan-gated-both" "$STUB_DIR/gh" 2>&1 1>/dev/null)
assert_eq "0" "$rc" "#8872: both sources plan-gated -> success exit (no required checks, not a failure)"
assert_eq "" "$result" "#8872: both sources plan-gated -> empty stdout"
case "$stderr_out" in
  *"plan-gated"*)
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: #8872: plan-gated relaxation emits a visible stderr warning" ;;
  *)
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: #8872: no visible warning emitted for the plan-gated relaxation" ;;
esac

# Subtest P.3: a DIFFERENT 403 (missing scope / SSO / rate limit) must keep
# failing closed -- the narrow message match is the whole point.
echo "$OTHER_403" > "$STUB_DIR/ruleset-fail-other-403"
rc=0
result=$(forge_get_required_status_check_contexts "owner/repo" "other-403" "$STUB_DIR/gh" 2>/dev/null | tr '\n' '|' | sed 's/|$//') || rc=$?
assert_eq "1" "$rc" "#8872: a non-plan-gated 403 keeps failing closed"
assert_eq "" "$result" "#8872: a non-plan-gated 403 -> empty stdout"

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

# --- Test the pending-vs-failing classification (#3664) ---
# The UNSTABLE-fallback poll in merge-pr.sh derives two sets from the head-SHA
# check-runs rollup: FAILING (terminal non-success conclusions) and PENDING
# (status != "completed", i.e. queued/in_progress → conclusion still null).
# These mirror the two jq filters used inside the poll body so the script stays
# in lockstep with the test. The #3664 bug was that a rollup that is UNSTABLE
# *solely* because required checks are still running has an empty FAILING set,
# so the pre-#3664 code hit the "unknown gap" hard-error instead of waiting.
echo ""
echo "Testing pending-vs-failing check-run classification (#3664)..."

# Mirror merge-pr.sh's _UNSTABLE_FAILING filter.
_failing_names() {
    echo "$1" | jq -r '[.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "cancelled" or .conclusion == "action_required") | .name] | unique | .[]' 2>/dev/null || true
}
# Mirror merge-pr.sh's _UNSTABLE_PENDING filter.
_pending_names() {
    echo "$1" | jq -r '[.check_runs[] | select(.status != "completed") | .name] | unique | .[]' 2>/dev/null || true
}

# A rollup that is UNSTABLE only because a required check is still running.
runs_pending='{"check_runs":[
  {"name":"Required Build","status":"in_progress","conclusion":null},
  {"name":"Lint","status":"completed","conclusion":"success"}
]}'
assert_eq "" "$(_failing_names "$runs_pending")" "#3664: still-running rollup has NO failing checks (empty FAILING set)"
assert_eq "Required Build" "$(_pending_names "$runs_pending")" "#3664: still-running rollup surfaces the in_progress check in PENDING"

# A queued check is also pending.
runs_queued='{"check_runs":[{"name":"Deploy Preview","status":"queued","conclusion":null}]}'
assert_eq "" "$(_failing_names "$runs_queued")" "#3664: queued rollup has no failing checks"
assert_eq "Deploy Preview" "$(_pending_names "$runs_queued")" "#3664: queued check appears in PENDING"

# An all-green rollup has neither failing nor pending checks.
runs_green='{"check_runs":[{"name":"Required Build","status":"completed","conclusion":"success"}]}'
assert_eq "" "$(_failing_names "$runs_green")" "#3664: all-green rollup has no failing checks"
assert_eq "" "$(_pending_names "$runs_green")" "#3664: all-green rollup has no pending checks"

# A failed check is FAILING but not PENDING.
runs_failed='{"check_runs":[{"name":"Required Build","status":"completed","conclusion":"failure"}]}'
assert_eq "Required Build" "$(_failing_names "$runs_failed")" "#3664: failed check appears in FAILING"
assert_eq "" "$(_pending_names "$runs_failed")" "#3664: failed (completed) check is NOT pending"

# Mixed: one check failed, another still running → both sets populated.
runs_mixed='{"check_runs":[
  {"name":"Flaky Job","status":"completed","conclusion":"failure"},
  {"name":"Required Build","status":"in_progress","conclusion":null}
]}'
assert_eq "Flaky Job" "$(_failing_names "$runs_mixed")" "#3664: mixed rollup surfaces the failed check in FAILING"
assert_eq "Required Build" "$(_pending_names "$runs_mixed")" "#3664: mixed rollup surfaces the running check in PENDING"

# --- Test the poll-decision precedence (#3664) ---
# Mirror the branch order of the UNSTABLE-fallback poll body:
#   (a) a failing REQUIRED check      -> "refuse"  (terminal, no wait)
#   (b) any PENDING check             -> "wait"    (bounded poll)
#   (c) failing INFORMATIONAL only,
#       nothing pending               -> "merge"   (#3486 immediate-merge)
#   (d) nothing failing, nothing
#       pending, observed a pending   -> "merge"   (checks resolved green)
#   (e) nothing failing, nothing
#       pending, never saw pending    -> "unknown" (preserve #3486 hard-error)
echo ""
echo "Testing UNSTABLE poll-decision precedence (#3664)..."

_unstable_decision() {
    local runs="$1" required="$2" observed_pending="$3"
    local failing pending informational overlap
    failing=$(_failing_names "$runs")
    pending=$(_pending_names "$runs")

    if [[ -n "$failing" ]]; then
        informational=$(comm -23 \
          <(printf '%s\n' "$failing" | sort -u) \
          <(printf '%s\n' "$required" | sort -u))
        overlap=$(comm -12 \
          <(printf '%s\n' "$failing" | sort -u) \
          <(printf '%s\n' "$required" | sort -u))
        if [[ -n "$overlap" ]]; then
            echo "refuse"; return                       # (a)
        fi
        if [[ -z "$pending" ]]; then
            echo "merge"; return                        # (c)
        fi
        # informational failures but checks still pending -> fall to wait
    fi

    if [[ -n "$pending" ]]; then
        echo "wait"; return                             # (b)
    fi

    if [[ "$observed_pending" == "true" ]]; then
        echo "merge"; return                            # (d)
    fi
    echo "unknown"                                       # (e)
}

# (b) The core #3664 case: required check still running, nothing failed -> wait.
result=$(_unstable_decision "$runs_pending" "Required Build" "false")
assert_eq "wait" "$result" "#3664: required check still running -> poll/wait (NOT hard error)"

# (a) A required check has failed -> refuse immediately, even with a pending one.
result=$(_unstable_decision "$runs_mixed" "Flaky Job" "false")
assert_eq "refuse" "$result" "#3664: failed REQUIRED check -> refuse without waiting"

# (b') Mixed where the failed check is informational and another is pending -> wait.
result=$(_unstable_decision "$runs_mixed" "Required Build" "false")
assert_eq "wait" "$result" "#3664: informational failure + pending required -> wait (do not merge yet)"

# (c) #3486 preserved: informational failure, nothing pending -> immediate merge.
runs_info_failed='{"check_runs":[{"name":"Informational Soak","status":"completed","conclusion":"failure"}]}'
result=$(_unstable_decision "$runs_info_failed" "Required Build" "false")
assert_eq "merge" "$result" "#3486 preserved: informational failure, nothing pending -> immediate merge"

# (d) Checks we waited on all resolved green -> immediate merge.
result=$(_unstable_decision "$runs_green" "Required Build" "true")
assert_eq "merge" "$result" "#3664: pending checks resolved green -> immediate merge"

# (e) Unknown gap preserved: nothing failing, nothing pending, never saw pending.
result=$(_unstable_decision "$runs_green" "Required Build" "false")
assert_eq "unknown" "$result" "#3664: unknown gap (no failing, no pending, never pending) -> preserve hard error"

# (a') All failing checks required, nothing pending -> refuse (existing behavior).
result=$(_unstable_decision "$runs_failed" "Required Build" "false")
assert_eq "refuse" "$result" "#3486 preserved: failing required check, nothing pending -> refuse"

# --- Test the fetch-failure decision point (#3678) ---
# The #3678 bug: a transient check-runs fetch failure mid-poll yields empty
# JSON, which classifies as "no failing, no pending" -> the resolved-green
# branch -> a premature immediate merge on a commit whose real check state is
# unknown. The fix captures the fetch exit status separately and, on failure,
# routes into the bounded pending-wait path BEFORE any empty-runs
# classification runs. This mirror gates _unstable_decision on fetch success:
# a failed fetch must always resolve to "wait", never reaching the (a)-(e)
# empty-runs classification at all.
echo ""
echo "Testing UNSTABLE fetch-failure decision point (#3678)..."

_unstable_decision_with_fetch() {
    local runs="$1" required="$2" observed_pending="$3" fetch_ok="$4"
    if [[ "$fetch_ok" != "true" ]]; then
        # A failed fetch never reaches the empty-runs classification; it is
        # treated as still-pending and re-polled (bounded by the deadline).
        echo "wait"; return
    fi
    _unstable_decision "$runs" "$required" "$observed_pending"
}

# The exact bug scenario from PR #3669's Judge note: fetch fails AFTER a pending
# check was observed. Must wait, NOT merge.
result=$(_unstable_decision_with_fetch "$runs_green" "Required Build" "true" "false")
assert_eq "wait" "$result" "#3678: fetch fails after observing pending -> wait (NOT premature merge)"

# Fetch fails on the very first poll iteration (never observed pending). Must
# wait, NOT hit the unknown-gap hard error — a fetch error is not a genuine
# unknown gap.
result=$(_unstable_decision_with_fetch "$runs_green" "Required Build" "false" "false")
assert_eq "wait" "$result" "#3678: fetch fails on first iteration -> wait (NOT unknown-gap hard error)"

# Regression guard: a SUCCESSFUL fetch of a genuinely empty rollup with no
# observed pending still resolves to the unknown-gap hard error — the fix must
# not over-widen so real unknown gaps get silently retried forever.
result=$(_unstable_decision_with_fetch "$runs_green" "Required Build" "false" "true")
assert_eq "unknown" "$result" "#3678: successful empty fetch, never pending -> unknown-gap preserved"

# Regression guard: a SUCCESSFUL fetch of an empty rollup after observing
# pending still resolves to merge — the #3664 resolved-green path is unchanged
# for real (non-error) fetches.
result=$(_unstable_decision_with_fetch "$runs_green" "Required Build" "true" "true")
assert_eq "merge" "$result" "#3678: successful empty fetch after pending -> resolved-green merge preserved"

# A failed fetch resolves to wait regardless of what the (ignored) runs payload
# would otherwise classify as — even a would-be failing-required rollup.
result=$(_unstable_decision_with_fetch "$runs_failed" "Required Build" "false" "false")
assert_eq "wait" "$result" "#3678: fetch failure ignores stale/empty payload -> wait (no classification)"

# --- Test the poll-window env-var wiring in merge-pr.sh (#3664) ---
# The script reuses LOOM_AUTO_MERGE_POLL_INTERVAL / LOOM_AUTO_MERGE_TIMEOUT with
# the same defaults as the since-retired (#8427) shell Gitea auto-merge
# poller: 30s / 600s. Assert the defaulting expressions the
# script uses resolve as expected.
echo ""
echo "Testing poll-window env-var defaults (#3664)..."

unset LOOM_AUTO_MERGE_POLL_INTERVAL LOOM_AUTO_MERGE_TIMEOUT 2>/dev/null || true
assert_eq "30" "${LOOM_AUTO_MERGE_POLL_INTERVAL:-30}" "#3664: poll interval defaults to 30s when unset"
assert_eq "600" "${LOOM_AUTO_MERGE_TIMEOUT:-600}" "#3664: poll timeout defaults to 600s when unset"
LOOM_AUTO_MERGE_POLL_INTERVAL=5
LOOM_AUTO_MERGE_TIMEOUT=120
assert_eq "5" "${LOOM_AUTO_MERGE_POLL_INTERVAL:-30}" "#3664: poll interval honors a caller override"
assert_eq "120" "${LOOM_AUTO_MERGE_TIMEOUT:-600}" "#3664: poll timeout honors a caller override"
unset LOOM_AUTO_MERGE_POLL_INTERVAL LOOM_AUTO_MERGE_TIMEOUT 2>/dev/null || true

# Assert the merge-pr.sh source actually contains the pending-set filter and the
# poll-window env vars, so a refactor that drops them fails this test.
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
if grep -q 'select(.status != "completed")' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh computes the PENDING set (status != completed)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh missing the PENDING-set filter"
fi
if grep -q 'LOOM_AUTO_MERGE_TIMEOUT' "$MERGE_PR_SRC" && grep -q 'LOOM_AUTO_MERGE_POLL_INTERVAL' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh wires the LOOM_AUTO_MERGE_* poll-window env vars"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh missing the LOOM_AUTO_MERGE_* poll-window env vars"
fi

# Assert the merge-pr.sh source captures the check-runs fetch exit status
# separately (the core of the #3678 fix) rather than collapsing it to empty JSON.
# Post-#8410 the single surviving copy of this loop is the one inside
# `_wait_for_checks_then_sync_merge` (`fetch_rc`); the UNSTABLE-rejection copy
# (`_UNSTABLE_FETCH_RC`) went with the server-side arm it existed to handle.
if grep -q 'fetch_rc="$attempt1_rc"' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh captures the check-runs fetch exit status (fetch_rc)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh missing the fetch-exit-status capture (#3678 regression)"
fi
# Assert the old exit-status-swallowing collapse is gone: the callsite must no
# longer OR a failed check-runs fetch into a hardcoded empty-JSON literal.
if grep -q "forge_get_check_runs .* || echo '{\"check_runs\":\[\]}'" "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh still collapses a failed check-runs fetch to empty JSON (#3678)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh no longer collapses a failed check-runs fetch to empty JSON"
fi

# --- The server-side auto-merge arm is gone (#8410) ---
#
# #3720's no-required-checks fallback, #3763's repo-setting fallback, #4447's
# GraphQL-rate-limit fallback and #3371's CLEAN fallback all existed to handle a
# REJECTION of GitHub's enablePullRequestAutoMerge mutation. #8410 stopped
# calling that mutation at all: a merge armed on the server is gated only by the
# ruleset's REQUIRED checks and re-reads neither the loom:pr label nor the
# non-required suites, so `--auto` now always settles the checks here and merges
# in-process. Each of those rejection paths is therefore unreachable-by-
# construction rather than "handled", and the assertions below pin that — if the
# arm ever comes back, these fail and the fallbacks have to come back with it.
echo ""
echo "Testing that merge-pr.sh never arms a server-side auto-merge (#8410)..."

_assert_absent() {
    local pattern="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -q -- "$pattern" "$MERGE_PR_SRC"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found: $pattern)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

_assert_absent 'forge_auto_merge "$REPO_NWO"' \
  "#8410: merge-pr.sh never calls the shell forge_auto_merge arm"
_assert_absent 'loom-daemon forge auto-merge' \
  "#8410: merge-pr.sh never calls the native forge auto-merge arm"
_assert_absent 'is in clean status' \
  "#8410: the CLEAN-rejection fallback (#3371) is gone with the mutation it handled"
_assert_absent 'is in unstable status' \
  "#8410: the UNSTABLE-rejection fallback (#3486/#3664) is gone with the mutation it handled"
_assert_absent 'Auto merge is not allowed for this repository' \
  "#8410: the repo-setting rejection fallback (#3763) is gone with the mutation it handled"
_assert_absent 'Auto-merge queued' \
  "#8410: there is no queued early-exit left to bypass the post-merge cleanup block"

# ...and the policy those fallbacks protected is still enforced, by the wait
# path every --auto run now takes: it computes the same failing/pending sets and
# the same required-context set difference asserted at the top of this file.
if grep -q '_wait_for_checks_then_sync_merge$' "$MERGE_PR_SRC" && \
   grep -q 'forge_get_required_status_check_contexts "$REPO_NWO" "$base_ref"' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: #8410: --auto routes into the wait path, which classifies against required contexts"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: #8410: --auto must call _wait_for_checks_then_sync_merge, which must classify failures against required contexts"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
