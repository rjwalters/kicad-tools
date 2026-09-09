#!/usr/bin/env bash
# test-repo-mismatch-fallback.sh - Unit tests for the wrong-repo `GH_CONFIG_DIR`
# credential escalation (2am#446).
#
# Incident, 2026-08-21 (`/loom:sweep 438` on `loom-worker-2`): the dispatch
# environment's `GH_CONFIG_DIR` pointed at a DIFFERENT owner's flat
# `<workspace>/.loom/gh-config/` (a daemon workspace's own credential, minted
# for a different org entirely), so every `gh` call against this repo failed
# outright with one of:
#
#   GraphQL: Could not resolve to a Repository with the name '<owner>/<repo>'. (repository)
#   gh: Not Found (HTTP 404)
#
# `forge_gh_perm_safe`'s existing #6074 ladder never fires for this signature
# (it only escalates on the DIFFERENT "not accessible by integration"
# permission-scope 403), so rung 1 just failed outright, breaking
# `sweep-lease-renew.sh`'s renewal loop on every cycle.
#
# This file tests:
#   1. is_repo_mismatch_error() fires on both confirmed signatures and on
#      nothing else -- in particular it must stay disjoint from
#      is_app_permission_error()'s and is_rate_limit_error()'s signatures, so
#      none of the three escalation ladders can be confused for one another.
#   2. forge_gh_repo_safe(): a clean call runs exactly one attempt; a
#      wrong-repo signature escalates to the owner-partitioned
#      `gh-config-by-owner/<owner>` directory when it exists and recovers;
#      falls back further to `env -u GH_CONFIG_DIR` when that directory does
#      not exist (or also fails); is skipped entirely when `GH_CONFIG_DIR` is
#      unset to begin with (nothing to escalate away from); and still
#      recovers via #6074's own ladder first when the failure is a permission
#      403 instead (the two ladders compose, they don't compete).
#
# Usage:
#   ./.loom/scripts/tests/test-repo-mismatch-fallback.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual:              '$haystack'"
    fi
}

# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- 1. is_repo_mismatch_error() signature table ----------------------------
echo "Testing is_repo_mismatch_error() signature table..."

GRAPHQL_MISMATCH="GraphQL: Could not resolve to a Repository with the name '2AMLogic/2am'. (repository)"
REST_MISMATCH="gh: Not Found (HTTP 404)"

if is_repo_mismatch_error "$GRAPHQL_MISMATCH"; then
    pass "fires on the observed GraphQL 'Could not resolve to a Repository' rejection"
else
    fail "must fire on the GraphQL 'Could not resolve to a Repository' rejection"
fi

if is_repo_mismatch_error "$REST_MISMATCH"; then
    pass "fires on the observed REST 'gh: Not Found (HTTP 404)' rendering"
else
    fail "must fire on the REST 'gh: Not Found (HTTP 404)' rendering"
fi

if is_repo_mismatch_error "GRAPHQL: COULD NOT RESOLVE TO A REPOSITORY WITH THE NAME 'X/Y'."; then
    pass "matches case-insensitively"
else
    fail "must match case-insensitively"
fi

repo_mismatch_negatives=(
    "app_permission:HTTP 403: Resource not accessible by integration"
    "rate_limit_graphql:GraphQL: API rate limit already exceeded for user ID 12345"
    "rate_limit_rest:HTTP 403: API rate limit exceeded for installation ID 1"
    "secondary_rate_limit:You have exceeded a secondary rate limit. Please retry your request again later."
    "auth_401:HTTP 401: Bad credentials"
    "plain_403:HTTP 403: Must have admin rights to Repository."
    "validation:HTTP 422: Validation Failed"
    "empty:"
)
for entry in "${repo_mismatch_negatives[@]}"; do
    name="${entry%%:*}"
    value="${entry#*:}"
    if is_repo_mismatch_error "$value"; then
        fail "is_repo_mismatch_error false-positived on $name"
    else
        pass "does NOT fire on $name"
    fi
done

# The three ladders' classifiers must stay disjoint: a repo-mismatch signature
# is not a permission-scope 403 (a different credential problem entirely) and
# not exhaustion (a REST retry with the same token 404s/GraphQL-fails
# identically).
if is_app_permission_error "$GRAPHQL_MISMATCH" || is_app_permission_error "$REST_MISMATCH"; then
    fail "is_app_permission_error must NOT claim either repo-mismatch signature"
else
    pass "is_app_permission_error leaves both repo-mismatch signatures alone"
fi
if is_rate_limit_error "$GRAPHQL_MISMATCH" || is_rate_limit_error "$REST_MISMATCH"; then
    fail "is_rate_limit_error must NOT claim either repo-mismatch signature"
else
    pass "is_rate_limit_error leaves both repo-mismatch signatures alone"
fi

# --- 2. forge_gh_repo_safe() escalation ladder -------------------------------
echo ""
echo "Testing forge_gh_repo_safe() escalation ladder..."

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
ATTEMPT_LOG="$STUB_DIR/attempts.log"
GH_MODE_FILE="$STUB_DIR/mode.txt"
GOOD_CFG="$STUB_DIR/workspace/.loom/gh-config-by-owner/owner"
WRONG_CFG="$STUB_DIR/workspace/.loom/gh-config"
mkdir -p "$GOOD_CFG"
mkdir -p "$WRONG_CFG"
export ATTEMPT_LOG GH_MODE_FILE GOOD_CFG

# A `gh` stub that logs which GH_CONFIG_DIR each attempt carried, then answers
# according to $GH_MODE_FILE:
#   ok             - succeeds immediately (any credential).
#   mismatch       - every attempt fails with the REST 404 signature UNLESS
#                     GH_CONFIG_DIR equals $GOOD_CFG, which always succeeds --
#                     models the owner-partitioned directory holding a
#                     correctly-scoped credential.
#   mismatch-never - every attempt fails with the REST 404 signature,
#                     regardless of credential -- models a host with no
#                     recoverable credential anywhere (the directory doesn't
#                     exist, or the whole repo really is inaccessible).
#   perm403        - every attempt 403s with the #6074 integration wording
#                     (a DIFFERENT failure -- exercises composition with the
#                     existing ladder, not this one).
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
mode="$(cat "$GH_MODE_FILE" 2>/dev/null || echo ok)"
cfg="${GH_CONFIG_DIR:-unset}"
printf '%s | %s\n' "$cfg" "$*" >> "$ATTEMPT_LOG"

case "$mode" in
  ok)
    echo "https://github.test/o/r/issues/1"
    exit 0
    ;;
  mismatch)
    if [[ "$cfg" == "$GOOD_CFG" ]]; then
      echo "https://github.test/o/r/issues/1"
      exit 0
    fi
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
    ;;
  mismatch-never)
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
    ;;
  perm403)
    echo "HTTP 403: Resource not accessible by integration" >&2
    exit 1
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

# A git repo with an origin remote, so `_forge_nwo_from_remote` resolves the
# owner ("owner") without any API call.
FAKE_REPO="$STUB_DIR/repo"
mkdir -p "$FAKE_REPO"
git -C "$FAKE_REPO" init -q
git -C "$FAKE_REPO" remote add origin "https://github.com/owner/repo.git"

_run() {
    local gh_mode="$1" gh_config_dir="$2"
    shift 2
    echo "$gh_mode" > "$GH_MODE_FILE"
    : > "$ATTEMPT_LOG"
    (
        cd "$FAKE_REPO"
        if [[ -n "$gh_config_dir" ]]; then
            PATH="$STUB_DIR:$PATH" GH_CONFIG_DIR="$gh_config_dir" "$@"
        else
            PATH="$STUB_DIR:$PATH" env -u GH_CONFIG_DIR "$@"
        fi
    )
}

# Happy path: one attempt, no escalation.
out="$(_run ok "$WRONG_CFG" bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_repo_safe issue view 1' 2>/dev/null)"
assert_eq "https://github.test/o/r/issues/1" "$out" \
    "forge_gh_repo_safe: a successful call returns gh's stdout unchanged"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_repo_safe: a successful call makes exactly one attempt"

# Wrong-repo signature, owner-partitioned directory EXISTS and carries a
# working credential: escalates and recovers.
rc=0
out="$(_run mismatch "$WRONG_CFG" bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_repo_safe issue view 1' 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" "forge_gh_repo_safe: a wrong-repo signature recovers via the owner-partitioned directory"
assert_eq "https://github.test/o/r/issues/1" "$out" \
    "forge_gh_repo_safe: the escalated attempt's stdout is returned"
assert_eq "2" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_repo_safe: recovery takes exactly two attempts (ambient, then owner-config)"
assert_contains "$(sed -n '1p' "$ATTEMPT_LOG")" "$WRONG_CFG" \
    "forge_gh_repo_safe: rung 1 runs under the original (wrong) GH_CONFIG_DIR"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "$GOOD_CFG" \
    "forge_gh_repo_safe: the recovery rung runs under the owner-partitioned GH_CONFIG_DIR"

# Wrong-repo signature, no owner-partitioned directory anywhere (derived path
# doesn't exist): falls back to env -u GH_CONFIG_DIR, and still reports the
# ultimate failure honestly when that also fails.
rc=0
NO_OWNER_DIR_WRONG_CFG="$STUB_DIR/workspace-solo/.loom/gh-config"
mkdir -p "$NO_OWNER_DIR_WRONG_CFG"
out="$(_run mismatch-never "$NO_OWNER_DIR_WRONG_CFG" bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_repo_safe issue view 1' 2>/dev/null)" || rc=$?
assert_eq "1" "$rc" "forge_gh_repo_safe: reports failure honestly when no credential recovers"
assert_eq "2" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_repo_safe: falls back to exactly one more attempt (env -u GH_CONFIG_DIR) when the owner directory is absent"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "unset" \
    "forge_gh_repo_safe: the fallback rung drops GH_CONFIG_DIR entirely"

# Edge case: GH_CONFIG_DIR unset entirely -> the rung is skipped cleanly, even
# though the (unrelated) failure text still matches the classifier -- there is
# nothing to escalate away from.
rc=0
out="$(_run mismatch-never "" bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_repo_safe issue view 1' 2>/dev/null)" || rc=$?
assert_eq "1" "$rc" "forge_gh_repo_safe: still reports failure when GH_CONFIG_DIR was never set"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_repo_safe: makes exactly one attempt when GH_CONFIG_DIR is unset (rung skipped, no wasted replay)"

# Composition: a #6074 permission-scope 403 recovers via forge_gh_perm_safe's
# OWN ladder (tried first, inside forge_gh_repo_safe) -- this rung never even
# has to inspect the failure, because rung 1 already succeeded via the nested
# ladder. Uses a wrapper stub that force-mints successfully so #6074's rung 2
# recovers.
cat > "$STUB_DIR/github-app-token.sh" <<'MINT'
#!/usr/bin/env bash
echo '{"status":"ok","token":"ghs_fresh","installation_id":"1","app_id":"2","expires_at":"2099-01-01T00:00:00Z"}'
MINT
chmod +x "$STUB_DIR/github-app-token.sh"

mkdir -p "$STUB_DIR/gh-perm-composed"
cat > "$STUB_DIR/gh-perm-composed/gh" <<'STUB'
#!/usr/bin/env bash
cfg="${GH_CONFIG_DIR:-unset}"
printf '%s | %s\n' "$cfg" "$*" >> "$ATTEMPT_LOG"
if [[ -n "${GH_TOKEN:-}" ]]; then
  echo "https://github.test/o/r/issues/1"
  exit 0
fi
echo "HTTP 403: Resource not accessible by integration" >&2
exit 1
STUB
chmod +x "$STUB_DIR/gh-perm-composed/gh"

rc=0
: > "$ATTEMPT_LOG"
out="$(
    cd "$FAKE_REPO"
    PATH="$STUB_DIR/gh-perm-composed:$PATH" GH_CONFIG_DIR="$WRONG_CFG" \
    LOOM_GITHUB_APP_SCRIPT="$STUB_DIR/github-app-token.sh" \
        bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_repo_safe issue view 1' 2>/dev/null
)" || rc=$?
assert_eq "0" "$rc" "forge_gh_repo_safe: a #6074 permission 403 recovers via the nested forge_gh_perm_safe ladder"
assert_eq "https://github.test/o/r/issues/1" "$out" \
    "forge_gh_repo_safe: returns the nested ladder's recovered stdout"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
