#!/usr/bin/env bash
# test-github-app-token.sh - Unit tests for github-app-token.sh (#4430)
#
# Covers, per the issue's acceptance criteria, all without a live GitHub App:
#   - JWT construction: signature verifies against a generated test keypair,
#     iat backdated ~60s, exp within GitHub's 10-minute ceiling.
#   - Refresh-window logic (cache/re-mint decision).
#   - Cache file permissions (0600) and no-secret-in-output.
#   - Mechanism/fallback selection (config precedence: env beats config;
#     unconfigured/unreadable key -> not configured, never a hard error).
#
# Usage:
#   ./.loom/scripts/tests/test-github-app-token.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/lib"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}
fail() {
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
    [[ -n "${2:-}" ]] && echo "    $2"
}

if ! command -v openssl >/dev/null 2>&1; then
    echo "SKIP: openssl not available -- cannot test JWT signing"
    exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available -- github-app-token.sh requires it"
    exit 0
fi

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

# A throwaway RSA keypair -- generated fresh per test run, never committed,
# never used against a real GitHub App.
KEY_PATH="$WORK_DIR/test-app-key.pem"
PUB_PATH="$WORK_DIR/test-app-key.pub.pem"
openssl genrsa -out "$KEY_PATH" 2048 >/dev/null 2>&1
openssl rsa -in "$KEY_PATH" -pubout -out "$PUB_PATH" >/dev/null 2>&1
chmod 600 "$KEY_PATH"

# Isolate the token cache from the real $HOME for the whole test run.
export LOOM_GITHUB_APP_CACHE_DIR="$WORK_DIR/cache"

# Source the library under test.
# shellcheck source=../lib/github-app-token.sh
source "$LIB_DIR/github-app-token.sh"

# ============================================================================
# Mechanism / fallback selection (config precedence, #4061-style)
# ============================================================================
echo "Testing github_app_configured (mechanism/fallback selection)..."

unset LOOM_GITHUB_APP_ID LOOM_GITHUB_APP_KEY_PATH 2>/dev/null || true
REPO_ROOT="$WORK_DIR/no-config-repo"
mkdir -p "$REPO_ROOT"
# REPO_ROOT alone only isolates config tiers 2-4 (legacy .loom/config.json,
# .loom-project/project.json, .loom-local/local.json) per config-resolver.sh's
# documented precedence. Tier 1 (private/shared defaults --
# $LOOM_CONFIG_DEFAULTS_FILE, else ~/.local/share/loom/config/defaults.json)
# is host-global and must be explicitly disabled too (#5441), matching the
# LOOM_CONFIG_DEFAULTS_FILE="" idiom used elsewhere (e.g.
# test-config-resolver.sh) -- otherwise a host with real forge.githubApp
# defaults provisioned (e.g. for fleet dispatch) spuriously fails this case.
if ! (REPO_ROOT="$REPO_ROOT" LOOM_CONFIG_DEFAULTS_FILE="" github_app_configured); then
    pass "unconfigured (no env, no config) -> github_app_configured fails, i.e. fallback path"
else
    fail "unconfigured should NOT report configured"
fi

LOOM_GITHUB_APP_ID="12345" LOOM_GITHUB_APP_KEY_PATH="$KEY_PATH" \
  REPO_ROOT="$REPO_ROOT" bash -c "
    source '$LIB_DIR/github-app-token.sh'
    github_app_configured
  "
if [[ $? -eq 0 ]]; then
    pass "env LOOM_GITHUB_APP_ID + LOOM_GITHUB_APP_KEY_PATH -> configured"
else
    fail "env-configured app should report configured"
fi

# Config file precedence: env absent, config supplies both keys.
CONFIG_REPO="$WORK_DIR/config-repo"
mkdir -p "$CONFIG_REPO/.loom"
cat > "$CONFIG_REPO/.loom/config.json" <<EOF
{"forge":{"githubApp":{"appId":"99999","privateKeyPath":"$KEY_PATH"}}}
EOF
unset LOOM_GITHUB_APP_ID LOOM_GITHUB_APP_KEY_PATH 2>/dev/null || true
if (REPO_ROOT="$CONFIG_REPO" bash -c "
    source '$LIB_DIR/github-app-token.sh'
    github_app_configured && [[ \"\$_GH_APP_ID\" == '99999' ]]
  "); then
    pass "config forge.githubApp.{appId,privateKeyPath} resolves when env is absent"
else
    fail "config-sourced app id/key path should resolve"
fi

# Env beats config: env app id should win over the config file's app id.
if (LOOM_GITHUB_APP_ID="11111" REPO_ROOT="$CONFIG_REPO" bash -c "
    source '$LIB_DIR/github-app-token.sh'
    github_app_configured && [[ \"\$_GH_APP_ID\" == '11111' ]]
  "); then
    pass "env LOOM_GITHUB_APP_ID beats config forge.githubApp.appId"
else
    fail "env app id should take precedence over config"
fi

# Unreadable key path -> not configured, with a remedy-naming message, never
# a hard failure (the caller sees this as 'fall back', not a crash).
UNREADABLE_KEY="$WORK_DIR/does-not-exist.pem"
if ! (LOOM_GITHUB_APP_ID="12345" LOOM_GITHUB_APP_KEY_PATH="$UNREADABLE_KEY" \
      bash -c "source '$LIB_DIR/github-app-token.sh'; github_app_configured"); then
    msg=$(LOOM_GITHUB_APP_ID="12345" LOOM_GITHUB_APP_KEY_PATH="$UNREADABLE_KEY" \
      bash -c "source '$LIB_DIR/github-app-token.sh'; github_app_configured || echo \"\$_GH_APP_LAST_ERROR\"")
    if [[ "$msg" == *"not readable"* ]]; then
        pass "unreadable private key -> not configured, remedy-naming message"
    else
        fail "expected a 'not readable' remedy message" "got: $msg"
    fi
else
    fail "unreadable private key should NOT report configured"
fi

# ============================================================================
# JWT construction: signature verifies against the test public key
# ============================================================================
echo "Testing github_app_jwt..."

export LOOM_GITHUB_APP_ID="424242"
export LOOM_GITHUB_APP_KEY_PATH="$KEY_PATH"

jwt=$(github_app_jwt)
if [[ -z "$jwt" ]]; then
    fail "github_app_jwt produced no output"
else
    signing_input="${jwt%.*}"
    signature_b64="${jwt##*.}"
    header_b64="${signing_input%%.*}"
    payload_b64="${signing_input#*.}"

    sig_file="$WORK_DIR/sig.bin"
    _github_app_b64url_decode_to_file "$signature_b64" "$sig_file"

    if printf '%s' "$signing_input" | openssl dgst -sha256 -verify "$PUB_PATH" -signature "$sig_file" >/dev/null 2>&1; then
        pass "JWT signature verifies against the test public key"
    else
        fail "JWT signature did NOT verify against the test public key"
    fi

    # Decode header/payload (base64url -> file -> read as text; these are
    # plain JSON, safe to read back into a bash variable unlike the raw
    # signature bytes above).
    header_file="$WORK_DIR/header.json"
    payload_file="$WORK_DIR/payload.json"
    _github_app_b64url_decode_to_file "$header_b64" "$header_file"
    _github_app_b64url_decode_to_file "$payload_b64" "$payload_file"

    alg=$(jq -r '.alg' "$header_file")
    typ=$(jq -r '.typ' "$header_file")
    if [[ "$alg" == "RS256" && "$typ" == "JWT" ]]; then
        pass "JWT header declares alg=RS256, typ=JWT"
    else
        fail "unexpected JWT header" "alg=$alg typ=$typ"
    fi

    iss=$(jq -r '.iss' "$payload_file")
    iat=$(jq -r '.iat' "$payload_file")
    exp=$(jq -r '.exp' "$payload_file")
    now=$(date +%s)
    if [[ "$iss" == "424242" ]]; then
        pass "JWT iss matches the configured app id"
    else
        fail "JWT iss mismatch" "expected 424242, got $iss"
    fi

    iat_skew=$((now - iat))
    if [[ "$iat_skew" -ge 55 && "$iat_skew" -le 90 ]]; then
        pass "JWT iat is backdated ~60s for clock-skew tolerance"
    else
        fail "JWT iat not backdated as expected" "now-iat=$iat_skew (want ~60)"
    fi

    lifetime=$((exp - iat))
    if [[ "$lifetime" -le 600 ]]; then
        pass "JWT lifetime is within GitHub's 10-minute ceiling"
    else
        fail "JWT lifetime exceeds the 10-minute ceiling" "lifetime=${lifetime}s"
    fi
fi

# ============================================================================
# Refresh-window logic
# ============================================================================
echo "Testing _github_app_needs_remint..."

future_epoch=$(( $(date +%s) + 3600 ))
if ! _github_app_needs_remint "$future_epoch"; then
    pass "token with 1h remaining -> no remint needed"
else
    fail "token with 1h remaining should NOT need a remint"
fi

soon_epoch=$(( $(date +%s) + 120 ))
if _github_app_needs_remint "$soon_epoch"; then
    pass "token with 2min remaining -> remint needed (<10min window)"
else
    fail "token with 2min remaining SHOULD need a remint"
fi

boundary_epoch=$(( $(date +%s) + 599 ))
if _github_app_needs_remint "$boundary_epoch"; then
    pass "token with 599s remaining -> remint needed (just inside the 10min window)"
else
    fail "token with 599s remaining SHOULD need a remint"
fi

if _github_app_needs_remint ""; then
    pass "malformed/empty expiry fails safe toward remint"
else
    fail "malformed/empty expiry should fail safe toward remint"
fi

# ============================================================================
# Cache: 0600 perms, correct round-trip, no secret in unrelated output
# ============================================================================
echo "Testing token cache..."

_github_app_write_cache "777" "ghs_supersecrettokenvalue1234567890" "2099-01-01T00:00:00Z"
cache_file="$(_github_app_cache_path "777")"
if [[ -f "$cache_file" ]]; then
    perms=$(stat -c '%a' "$cache_file" 2>/dev/null || stat -f '%Lp' "$cache_file" 2>/dev/null)
    if [[ "$perms" == "600" ]]; then
        pass "cache file written at 0600"
    else
        fail "cache file has wrong permissions" "got $perms, want 600"
    fi
else
    fail "cache file was not written"
fi

read_back=$(_github_app_read_cache "777")
cached_token=$(echo "$read_back" | jq -r '.token')
if [[ "$cached_token" == "ghs_supersecrettokenvalue1234567890" ]]; then
    pass "cache round-trips the token value"
else
    fail "cache round-trip mismatch" "got: $cached_token"
fi

# No-secret-in-output: the `status` CLI path (and JWT signing errors) must
# never leak a token/JWT/key value. Simulate a `get-token`-shaped diagnostic
# by asserting the last-error string never contains the private key's PEM
# body or the cached token above.
key_body=$(tail -n +2 "$KEY_PATH" | head -n 1)
status_output=$(LOOM_GITHUB_APP_ID="424242" LOOM_GITHUB_APP_KEY_PATH="$KEY_PATH" \
  bash "$LIB_DIR/github-app-token.sh" status)
if [[ "$status_output" != *"$key_body"* && "$status_output" != *"ghs_supersecrettokenvalue1234567890"* ]]; then
    pass "\`status\` CLI output contains no key/token material"
else
    fail "\`status\` output leaked secret material" "$status_output"
fi
status_status=$(echo "$status_output" | jq -r '.status')
if [[ "$status_status" == "configured" ]]; then
    pass "\`status\` CLI reports configured when app id + readable key are set"
else
    fail "\`status\` CLI should report configured" "$status_output"
fi

unset LOOM_GITHUB_APP_ID LOOM_GITHUB_APP_KEY_PATH
# Same tier-1 leak as the "unconfigured" case above (#5441): these `status`
# and `get-token` CLI invocations resolve config via REPO_ROOT (defaulted to
# the real repo here since it's unset), which only covers tiers 2-4. Disable
# tier 1 explicitly so a host with real forge.githubApp defaults provisioned
# doesn't spuriously fail these "not configured" assertions.
not_configured_output=$(LOOM_CONFIG_DEFAULTS_FILE="" bash "$LIB_DIR/github-app-token.sh" status)
not_configured_status=$(echo "$not_configured_output" | jq -r '.status')
if [[ "$not_configured_status" == "not_configured" ]]; then
    pass "\`status\` CLI reports not_configured with no app credentials"
else
    fail "\`status\` CLI should report not_configured" "$not_configured_output"
fi

get_token_output=$(LOOM_CONFIG_DEFAULTS_FILE="" bash "$LIB_DIR/github-app-token.sh" get-token owner/repo)
get_token_status=$(echo "$get_token_output" | jq -r '.status')
if [[ "$get_token_status" == "not_configured" ]]; then
    pass "\`get-token\` CLI falls back to not_configured (never a hard failure) when unconfigured"
else
    fail "\`get-token\` CLI should report not_configured when unconfigured" "$get_token_output"
fi

# ============================================================================
# Installation-id validation (#4456): a 2xx response missing/`null` `.id` must
# not be echoed (and thus never cached), and a pre-poisoned cache holding a
# non-numeric value must be treated as a miss (self-healing).
# ============================================================================
echo "Testing installation-id validation (#4456)..."

# Stub the network + JWT so these run without a live GitHub App. `curl` is
# dispatched on the request URL (its last argument); `github_app_jwt` is a
# constant so no signing is required.
STUB_INSTALL_BODY='{"id": 55}'
STUB_INSTALL_CODE='200'
curl() {
    local url=""
    for _a in "$@"; do url="$_a"; done
    case "$url" in
        */installation) printf '%s\n%s' "$STUB_INSTALL_BODY" "$STUB_INSTALL_CODE" ;;
        */access_tokens) printf '%s\n%s' '{"token":"ghs_mintedvalue","expires_at":"2099-01-01T00:00:00Z"}' '200' ;;
        *) printf '%s\n%s' '{}' '404' ;;
    esac
}
github_app_jwt() { echo "fake.jwt.signature"; }

export LOOM_GITHUB_APP_ID="424242"
export LOOM_GITHUB_APP_KEY_PATH="$KEY_PATH"

# Valid numeric id -> echoed, success.
STUB_INSTALL_BODY='{"id": 55}'
if resolved_id=$(_github_app_api_get_installation "acme/widgets") && [[ "$resolved_id" == "55" ]]; then
    pass "installation resolve echoes a numeric id on a well-formed 2xx response"
else
    fail "well-formed 2xx installation response should echo the numeric id" "got: ${resolved_id:-<none>}"
fi

# 2xx body with `"id": null` -> must NOT echo, must return 1 with an error set.
# Run in the current shell (stdout redirected to a file, not command
# substitution) so the `_GH_APP_LAST_ERROR` the function sets is observable.
inst_out="$WORK_DIR/get-installation.out"
STUB_INSTALL_BODY='{"id": null}'
_GH_APP_LAST_ERROR=""
if _github_app_api_get_installation "acme/widgets" > "$inst_out" 2>/dev/null; then
    fail "2xx response with null id should fail, not succeed" "echoed: $(cat "$inst_out")"
else
    echoed=$(cat "$inst_out")
    if [[ "$echoed" != *"null"* && -n "$_GH_APP_LAST_ERROR" ]]; then
        pass "2xx response with null id -> returns 1, no 'null' echoed, error set"
    else
        fail "null id should not be echoed and must set _GH_APP_LAST_ERROR" "echoed='${echoed}' err='${_GH_APP_LAST_ERROR}'"
    fi
fi

# 2xx body with no `.id` field at all -> same rejection.
STUB_INSTALL_BODY='{"message":"Not Found"}'
_GH_APP_LAST_ERROR=""
if _github_app_api_get_installation "acme/widgets" > "$inst_out" 2>/dev/null; then
    fail "2xx response missing .id should fail" "echoed: $(cat "$inst_out")"
else
    if [[ -n "$_GH_APP_LAST_ERROR" ]]; then
        pass "2xx response missing .id -> returns 1 with error set"
    else
        fail "missing .id must set _GH_APP_LAST_ERROR"
    fi
fi

# Pre-poisoned cache holding the literal "null" -> treated as a miss and
# re-resolved (self-healing), so github_app_get_token still succeeds and the
# cache file is rewritten with the numeric id.
STUB_INSTALL_BODY='{"id": 55}'
poison_owner="poisoned"
poison_cache="$(_github_app_installation_cache_path "$poison_owner")"
mkdir -p "$(dirname "$poison_cache")"
printf '%s' "null" > "$poison_cache"
if heal_token=$(github_app_get_token "${poison_owner}/repo") && [[ "$heal_token" == "ghs_mintedvalue" ]]; then
    cache_after=$(cat "$poison_cache")
    if [[ "$cache_after" == "55" ]]; then
        pass "pre-poisoned 'null' installation cache is treated as a miss and self-heals to the numeric id"
    else
        fail "poisoned cache should be rewritten with the numeric id" "cache now: '${cache_after}'"
    fi
else
    fail "github_app_get_token should self-heal past a poisoned 'null' cache" "got: ${heal_token:-<none>}"
fi

# ============================================================================
# `get-token` CLI envelope carries a non-empty installation_id (#5401
# regression test: `_gh_app_token=$(github_app_get_token "$_nwo")` runs the
# mint in a SUBSHELL, so GITHUB_APP_INSTALLATION_ID's assignment inside it
# never propagated back out -- `installation_id` came back "" even on a
# successful mint).
# ============================================================================
echo "Testing get-token CLI envelope installation_id (#5401)..."

# The stub `curl`/`github_app_jwt` functions above only exist in THIS shell;
# `get-token` runs as a genuine child process (matching how `loom-daemon`
# invokes it), so both the functions AND the plain variables the `curl` stub
# reads (`STUB_INSTALL_BODY`/`STUB_INSTALL_CODE`) must be exported for the
# child to see them.
export STUB_INSTALL_BODY='{"id": 55}'
export STUB_INSTALL_CODE='200'
export -f curl github_app_jwt
cli_get_token_output=$(bash "$LIB_DIR/github-app-token.sh" get-token cliowner/repo)
cli_get_token_status=$(echo "$cli_get_token_output" | jq -r '.status')
cli_get_token_installation_id=$(echo "$cli_get_token_output" | jq -r '.installation_id')
cli_get_token_token=$(echo "$cli_get_token_output" | jq -r '.token')
if [[ "$cli_get_token_status" == "ok" && "$cli_get_token_installation_id" == "55" && "$cli_get_token_token" == "ghs_mintedvalue" ]]; then
    pass "\`get-token\` CLI envelope reports the real installation_id on a successful mint"
else
    fail "\`get-token\` CLI envelope should carry a non-empty, correct installation_id" "$cli_get_token_output"
fi

unset -f curl github_app_jwt
unset LOOM_GITHUB_APP_ID LOOM_GITHUB_APP_KEY_PATH

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
