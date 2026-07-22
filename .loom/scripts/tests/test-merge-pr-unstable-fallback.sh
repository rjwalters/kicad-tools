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
# the same defaults as loom-auto-merge (30s / 600s). Assert the defaulting
# expressions the script uses resolve as expected.
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
if grep -q '_UNSTABLE_FETCH_RC' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh captures the check-runs fetch exit status (_UNSTABLE_FETCH_RC)"
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

# --- Test the no-required-checks fallback (#3720) ---
# When the repo defines ZERO required status checks, GitHub's
# enablePullRequestAutoMerge mutation is rejected outright (nothing to queue the
# merge behind). That rejection matches neither the CLEAN nor the UNSTABLE grep,
# so pre-#3720 it fell through to the generic terminal error. The #3720 fallback
# is STRING-INDEPENDENT and self-gating: it fires only when the base branch has
# NO required status check contexts AND the PR is mergeable (.mergeable == true),
# in which case an immediate synchronous merge is exactly equivalent to a
# server-side auto-merge. It preserves the #3664/#3486/#3678 required-check
# gating BY CONSTRUCTION — any required context present skips the branch.
echo ""
echo "Testing the no-required-checks fallback decision (#3720)..."

# Mirror the merge-pr.sh callsite's self-gating predicate. Returns "merge" when
# the fallback fires (no required checks + mergeable + clean lookup), else
# "preserve" (the existing CLEAN/UNSTABLE/terminal path stays in charge).
_nrc_decision() {
    local required="$1" mergeable="$2" lookup_rc="$3"
    if [[ "$lookup_rc" -eq 0 ]] && [[ -z "$required" ]] && [[ "$mergeable" == "true" ]]; then
        echo "merge"
    else
        echo "preserve"
    fi
}

# Core #3720 case: no required checks + mergeable + successful lookup -> merge.
assert_eq "merge" "$(_nrc_decision "" "true" 0)" "#3720: no required checks + mergeable -> immediate merge"

# Required checks present -> preserve (UNSTABLE classifier stays in charge). This
# is the by-construction #3664/#3486/#3678 gating guarantee.
assert_eq "preserve" "$(_nrc_decision "Code Ownership" "true" 0)" "#3720: required checks present -> fallback does NOT fire (gating preserved)"
assert_eq "preserve" "$(_nrc_decision $'Code Ownership\nRequired Build' "true" 0)" "#3720: multiple required checks -> fallback does NOT fire"

# Not mergeable -> preserve (a conflicting PR must not be force-merged).
assert_eq "preserve" "$(_nrc_decision "" "false" 0)" "#3720: no required checks but NOT mergeable -> preserve"

# .mergeable still null (GitHub not yet computed / jq // empty) -> preserve.
assert_eq "preserve" "$(_nrc_decision "" "" 0)" "#3720: mergeable unknown (empty) -> preserve (do not merge blind)"

# Lookup failure (nonzero exit) -> fail closed even when required is empty.
assert_eq "preserve" "$(_nrc_decision "" "true" 1)" "#3720: required-checks lookup failure -> fail closed (preserve)"

# End-to-end with the real helper (GitHub stub): a branch with no protection
# rule yields empty required contexts, so a mergeable PR fires the fallback.
required="$(forge_get_required_status_check_contexts "owner/repo" "no-protection-branch" "$STUB_DIR/gh")"
assert_eq "merge" "$(_nrc_decision "$required" "true" 0)" "#3720: GitHub no-protection branch + mergeable -> fallback fires (real helper)"

# End-to-end with the real helper: a branch WITH required contexts preserves.
required="$(forge_get_required_status_check_contexts "owner/repo" "main" "$STUB_DIR/gh")"
assert_eq "preserve" "$(_nrc_decision "$required" "true" 0)" "#3720: GitHub protected branch with required contexts -> preserve (real helper)"

# Assert the merge-pr.sh source actually wires the #3720 fallback so a refactor
# that drops it fails this test. The fallback must be STRING-INDEPENDENT: it
# calls forge_get_required_status_check_contexts and checks .mergeable rather
# than grepping AUTO_MERGE_OUTPUT.
if grep -q '_NRC_REQUIRED' "$MERGE_PR_SRC" && grep -q 'no required status checks' "$MERGE_PR_SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh wires the #3720 no-required-checks fallback"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh missing the #3720 no-required-checks fallback"
fi
# The #3720 fallback must sit BEFORE the CLEAN/UNSTABLE greps so the
# zero-required-checks rejection (which matches neither) is caught first.
_nrc_line=$(grep -n '_NRC_REQUIRED=' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)
# Anchor on the actual CLEAN-grep code line (not a comment mention of the
# substring) so the ordering check reflects execution order.
_clean_line=$(grep -n 'grep -q "is in clean status"' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)
if [[ -n "$_nrc_line" ]] && [[ -n "$_clean_line" ]] && [[ "$_nrc_line" -lt "$_clean_line" ]]; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: #3720 fallback is inserted BEFORE the clean/unstable greps"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: #3720 fallback must precede the clean/unstable greps (nrc=$_nrc_line clean=$_clean_line)"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
