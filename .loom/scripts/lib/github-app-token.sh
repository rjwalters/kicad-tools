#!/usr/bin/env bash
# github-app-token.sh - GitHub App identity: JWT minting + installation-token
# fetch/cache/refresh, shell-only (openssl + curl) (#4430).
#
# # Why shell, not a new Rust dependency
#
# `loom-daemon` has no HTTP client and no JWT/RSA crate (see
# `loom-daemon/Cargo.toml`); every forge call already shells out to `gh`. This
# lib mints the two things a fleet host needs to authenticate as a GitHub App
# installation with nothing more than tools already assumed present
# (`openssl`, `curl`, `jq`):
#
#   1. A short-lived (<=10 min) RS256 JWT signed with the app's private key
#      (`github_app_jwt`) -- proves "I am app <id>".
#   2. An installation access token minted from that JWT
#      (`github_app_get_token`) -- a normal `ghs_...` token good for ~1h,
#      scoped to exactly the repos the app installation covers, with its own
#      rate-limit bucket separate from any personal account.
#
# `loom-daemon` invokes this file as an executable (`get-token` / `status`
# subcommands below), not sourced -- see `loom-daemon/src/credential_preflight.rs`.
#
# # Config precedence (env > config > default), matching GiteaConfig (#4061)
#
#   LOOM_GITHUB_APP_ID          > forge.githubApp.appId
#   LOOM_GITHUB_APP_KEY_PATH    > forge.githubApp.privateKeyPath
#
# Installation selection is DERIVABLE, not configured: `GET
# /repos/{owner}/{repo}/installation` (JWT-authed) resolves the installation
# for any repo the app can see, cached per-owner so a fleet spanning multiple
# GitHub accounts/orgs (e.g. rjwalters vs 2AMLogic) resolves the right
# installation for whichever repo it is currently operating on.
#
# # Fallback-first (the load-bearing default)
#
# `github_app_configured` returns 1 (with `_GH_APP_LAST_ERROR` set, never a
# secret) whenever the app id or private key path is absent/unreadable --
# every caller (the CLI dispatch below, and `credential_preflight.rs`) treats
# that as "fall back to ambient `gh` auth", never a hard failure. With no app
# credentials configured anywhere (env or config), this file's production
# entry points are never even invoked with real work to do -- byte-identical
# behavior to a host that has never heard of this feature.
#
# # Secret handling
#
# - The private key is read only by `openssl` (as a `-sign` keyfile argument);
#   this script never echoes, greps, or logs its contents.
# - Minted tokens are cached to disk at `0600` (owner read/write only),
#   directory `0700`.
# - No function in this file ever writes a token, JWT, or key material to
#   stdout labelled as anything other than exactly the value the caller asked
#   for (`get-token`'s JSON envelope) -- diagnostics (`status`, errors) carry
#   only non-secret fields: app id, installation id, a token's expiry, and
#   human-readable remedy text.
#
# Usage (executable):
#   ./github-app-token.sh status                 # cheap, no network
#   ./github-app-token.sh get-token OWNER/REPO    # mints/reuses a token
#
# Usage (sourced, for tests / advanced callers):
#   source ".../lib/github-app-token.sh"
#   github_app_configured && github_app_get_token "owner/repo"

set -euo pipefail

# ${BASH_SOURCE[0]:-$0} -- see forge-helpers.sh for the zsh-portable rationale
# (this file is occasionally sourced directly by a zsh-default shell too).
_LOOM_GH_APP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./config-resolver.sh
source "$_LOOM_GH_APP_LIB_DIR/config-resolver.sh"

# --- Config resolution (mirrors forge-helpers.sh::_forge_config_root) ---

# _github_app_config_root -> echoes the root to resolve config from.
# Precedence: $REPO_ROOT (env) > $WORKSPACE_ROOT (env) > canonical repo root
# via `git rev-parse --git-common-dir` > "." as a last resort.
_github_app_config_root() {
  if [[ -n "${REPO_ROOT:-}" ]]; then
    echo "$REPO_ROOT"
    return 0
  fi
  if [[ -n "${WORKSPACE_ROOT:-}" ]]; then
    echo "$WORKSPACE_ROOT"
    return 0
  fi

  local git_common_dir
  if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
    if [[ "$git_common_dir" != /* ]]; then
      git_common_dir="$(cd "$git_common_dir" && pwd)"
    fi
    dirname "$git_common_dir"
    return 0
  fi

  echo "."
}

# Global state populated by _github_app_load_config / github_app_configured.
_GH_APP_ID=""
_GH_APP_KEY_PATH=""
_GH_APP_LAST_ERROR=""
# Populated by github_app_get_token on success, for the CLI envelope.
GITHUB_APP_INSTALLATION_ID=""
GITHUB_APP_TOKEN_EXPIRES_AT=""

# _github_app_load_config -> resolves _GH_APP_ID / _GH_APP_KEY_PATH from env
# (highest precedence) or config (forge.githubApp.appId /
# forge.githubApp.privateKeyPath), matching the #4061 GiteaConfig precedence
# pattern (env beats config).
_github_app_load_config() {
  _GH_APP_ID="${LOOM_GITHUB_APP_ID:-}"
  _GH_APP_KEY_PATH="${LOOM_GITHUB_APP_KEY_PATH:-}"

  if [[ ( -z "$_GH_APP_ID" || -z "$_GH_APP_KEY_PATH" ) ]] && command -v jq >/dev/null 2>&1; then
    local root
    root="$(_github_app_config_root)"
    if [[ -z "$_GH_APP_ID" ]]; then
      _GH_APP_ID="$(loom_config_get "$root" "forge.githubApp.appId" "")"
    fi
    if [[ -z "$_GH_APP_KEY_PATH" ]]; then
      _GH_APP_KEY_PATH="$(loom_config_get "$root" "forge.githubApp.privateKeyPath" "")"
    fi
  fi
}

# github_app_configured -> returns 0 when an app id and a readable private
# key are both resolved; 1 otherwise with a human-readable, non-secret
# reason in $_GH_APP_LAST_ERROR. This is the single gate every production
# entry point checks before doing any work -- absent config is NOT an error,
# just "fall back to ambient auth" (the load-bearing default, #4430 AC1).
github_app_configured() {
  _github_app_load_config
  if [[ -z "$_GH_APP_ID" ]]; then
    _GH_APP_LAST_ERROR="github app not configured (set LOOM_GITHUB_APP_ID or forge.githubApp.appId)"
    return 1
  fi
  if [[ -z "$_GH_APP_KEY_PATH" ]]; then
    _GH_APP_LAST_ERROR="github app private key path not configured (set LOOM_GITHUB_APP_KEY_PATH or forge.githubApp.privateKeyPath)"
    return 1
  fi
  if [[ ! -r "$_GH_APP_KEY_PATH" ]]; then
    _GH_APP_LAST_ERROR="github app private key not readable at ${_GH_APP_KEY_PATH} -- check the path and file permissions (revoked/rotated/never-provisioned keys all look like this)"
    return 1
  fi
  return 0
}

# --- base64url helpers (JWT is base64url, RFC 7515) ---
#
# Both directions are pipeline-only (never capture raw binary through a bash
# variable): bash's variables are effectively NUL-terminated C strings, so an
# RSA signature (which can contain an embedded 0x00 byte) silently truncates
# if it passes through `$(...)` before being base64-encoded. Piping openssl's
# raw stdout directly into the base64 step keeps every bash-variable capture
# text-safe.

# _github_app_b64url -> reads stdin, writes base64url (no padding) to stdout.
_github_app_b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

# _github_app_b64url_decode_to_file <b64url-string> <out-file> -> restores
# padding, translates the alphabet back, and writes raw decoded bytes to
# out-file (never through a bash variable -- see note above). Test-only helper
# (signature verification needs the raw bytes on disk for `openssl -verify`).
_github_app_b64url_decode_to_file() {
  local input="$1" out="$2" padded rem
  padded="$input"
  rem=$(( ${#padded} % 4 ))
  case "$rem" in
    2) padded+="==" ;;
    3) padded+="=" ;;
    0) : ;;
    *) : ;; # length%4==1 cannot occur for valid base64url
  esac
  printf '%s' "$padded" | tr -- '-_' '+/' | openssl base64 -d -A > "$out"
}

# --- JWT minting (RS256, GitHub App auth) ---

# github_app_jwt -> mints a fresh RS256 JWT on stdout using the configured
# app id + private key. Returns 1 (with $_GH_APP_LAST_ERROR set) when not
# configured or signing fails. Pure/local -- no network call, so this is
# fully unit-testable with a throwaway keypair (test-github-app-token.sh).
#
# `iat` is backdated 60s to tolerate clock skew between this host and
# GitHub's API (GitHub's own JWT guide: "to protect against clock drift... we
# recommend that you set this 60 seconds in the past"). `exp` is 9 minutes
# out, inside GitHub's 10-minute ceiling with headroom for the request
# round-trip.
github_app_jwt() {
  if ! github_app_configured; then
    return 1
  fi

  local now iat exp header payload header_b64 payload_b64 signing_input signature_b64
  now=$(date +%s)
  iat=$((now - 60))
  exp=$((now + 540))
  header='{"alg":"RS256","typ":"JWT"}'
  payload="{\"iat\":${iat},\"exp\":${exp},\"iss\":\"${_GH_APP_ID}\"}"
  header_b64=$(printf '%s' "$header" | _github_app_b64url)
  payload_b64=$(printf '%s' "$payload" | _github_app_b64url)
  signing_input="${header_b64}.${payload_b64}"

  if ! signature_b64=$(printf '%s' "$signing_input" | openssl dgst -sha256 -sign "$_GH_APP_KEY_PATH" 2>/dev/null | _github_app_b64url); then
    _GH_APP_LAST_ERROR="failed to sign JWT with the configured private key at ${_GH_APP_KEY_PATH} (revoked, rotated, or not a valid RSA private key)"
    return 1
  fi
  if [[ -z "$signature_b64" ]]; then
    _GH_APP_LAST_ERROR="openssl produced no signature -- check that ${_GH_APP_KEY_PATH} is a valid RSA private key"
    return 1
  fi

  printf '%s.%s' "$signing_input" "$signature_b64"
}

# --- Token cache (0600, keyed by installation id) ---

# _github_app_cache_dir -> echoes the cache directory, creating it (0700) if
# absent. Overridable via LOOM_GITHUB_APP_CACHE_DIR (tests use this to avoid
# touching a real $HOME).
_github_app_cache_dir() {
  local dir="${LOOM_GITHUB_APP_CACHE_DIR:-${HOME:-/tmp}/.cache/loom/github-app}"
  mkdir -p "$dir"
  chmod 700 "$dir" 2>/dev/null || true
  echo "$dir"
}

# _github_app_cache_path <installation_id> -> echoes the token-cache file path.
_github_app_cache_path() {
  echo "$(_github_app_cache_dir)/installation-${1}.json"
}

# _github_app_installation_cache_path <owner> -> echoes the owner->installation
# mapping cache file path. This value is not a secret (an installation id is
# just an integer identifying a repo grant) so it does not need 0600, but it
# lives in the same 0700 directory as the token cache for simplicity.
_github_app_installation_cache_path() {
  echo "$(_github_app_cache_dir)/owner-${1}.installation"
}

# _github_app_write_cache <installation_id> <token> <expires_at_iso8601> ->
# atomically writes the token cache file at 0600. Uses a temp file + rename
# (same pattern as every other Loom bookkeeping writer) so a concurrent
# reader never observes a torn file.
_github_app_write_cache() {
  local installation_id="$1" token="$2" expires_at="$3"
  local path tmp
  path="$(_github_app_cache_path "$installation_id")"
  tmp="${path}.tmp.$$"
  jq -cn --arg token "$token" --arg expires_at "$expires_at" --arg installation_id "$installation_id" \
    '{token: $token, expires_at: $expires_at, installation_id: $installation_id}' > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$path"
}

# _github_app_read_cache <installation_id> -> echoes the cached JSON (token +
# expires_at) if the file exists and parses; echoes nothing (exit 1) otherwise.
_github_app_read_cache() {
  local path
  path="$(_github_app_cache_path "$1")"
  if [[ ! -f "$path" ]]; then
    return 1
  fi
  jq -c 'if (.token? and .expires_at?) then . else empty end' "$path" 2>/dev/null || return 1
}

# _github_app_parse_iso8601 <YYYY-MM-DDTHH:MM:SSZ> -> echoes the unix epoch
# seconds. Tries GNU date first, then BSD/macOS date -- the two hosts Loom's
# fleet actually runs on (#4172 precedent for this exact GNU/BSD split).
_github_app_parse_iso8601() {
  local ts="$1"
  date -u -d "$ts" +%s 2>/dev/null && return 0
  date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null && return 0
  return 1
}

# _github_app_needs_remint <expires_epoch> -> returns 0 (true, "remint now")
# when fewer than 600s (10 minutes) of lifetime remain -- the #4430 AC2
# refresh window. A malformed/empty epoch is treated as "needs remint" (fail
# safe toward re-minting, never toward silently reusing an unparseable
# expiry).
_github_app_needs_remint() {
  local expires_epoch="${1:-}"
  if [[ -z "$expires_epoch" || ! "$expires_epoch" =~ ^-?[0-9]+$ ]]; then
    return 0
  fi
  local now remaining
  now=$(date +%s)
  remaining=$((expires_epoch - now))
  [[ "$remaining" -lt 600 ]]
}

# --- Network calls (installation resolution + token minting) ---
#
# Neither of these is required for the unit test suite (#4430 AC: "none
# require a live GitHub App") -- they are exercised only by the executable
# CLI dispatch below, in production, once an app is actually installed.

# _github_app_api_get_installation <owner/repo> -> echoes the installation id
# on stdout. Requires a fresh JWT (github_app_jwt).
_github_app_api_get_installation() {
  local nwo="$1" jwt resp http_code body
  jwt=$(github_app_jwt) || return 1
  resp=$(curl -sS -w '\n%{http_code}' \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${nwo}/installation" 2>/dev/null) || {
    _GH_APP_LAST_ERROR="could not reach the GitHub API to resolve the installation for ${nwo}"
    return 1
  }
  http_code=$(echo "$resp" | tail -1)
  body=$(echo "$resp" | sed '$d')
  if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
    # Validate before echoing: a 2xx body missing `.id` yields the literal
    # string "null", which is non-empty and would poison the per-owner cache
    # persistently (#4456). Installation ids are numeric -- reject anything
    # else at the source so the caller never caches a bad value.
    local id
    id=$(echo "$body" | jq -r '.id')
    if [[ ! "$id" =~ ^[0-9]+$ ]]; then
      _GH_APP_LAST_ERROR="GitHub App installation response for ${nwo} did not include a numeric installation id (HTTP ${http_code})"
      return 1
    fi
    printf '%s' "$id"
    return 0
  fi
  _GH_APP_LAST_ERROR="could not resolve the GitHub App installation for ${nwo} (HTTP ${http_code} -- is the app installed on this repo/org?)"
  return 1
}

# _github_app_api_mint_token <installation_id> -> echoes
# '{"token":...,"expires_at":...}' on stdout. Requires a fresh JWT.
_github_app_api_mint_token() {
  local installation_id="$1" jwt resp http_code body
  jwt=$(github_app_jwt) || return 1
  resp=$(curl -sS -w '\n%{http_code}' -X POST \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/app/installations/${installation_id}/access_tokens" 2>/dev/null) || {
    _GH_APP_LAST_ERROR="could not reach the GitHub API to mint an installation token for installation ${installation_id}"
    return 1
  }
  http_code=$(echo "$resp" | tail -1)
  body=$(echo "$resp" | sed '$d')
  if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
    echo "$body"
    return 0
  fi
  _GH_APP_LAST_ERROR="could not mint an installation token for installation ${installation_id} (HTTP ${http_code})"
  return 1
}

# --- Top-level entry point ---

# github_app_get_token <owner/repo> -> echoes a valid installation token on
# stdout, minting or reusing the cache as needed. On success also sets
# GITHUB_APP_INSTALLATION_ID and GITHUB_APP_TOKEN_EXPIRES_AT (non-secret).
# Returns 1 (with $_GH_APP_LAST_ERROR set) on any failure -- callers MUST
# treat that as "fall back to ambient auth", not a hard error (#4430 AC:
# expired/revoked/unreadable key falls back rather than hard-failing).
github_app_get_token() {
  local nwo="$1"
  if ! github_app_configured; then
    return 1
  fi
  local owner="${nwo%%/*}"
  if [[ -z "$owner" || "$owner" == "$nwo" ]]; then
    _GH_APP_LAST_ERROR="expected owner/repo, got '${nwo}'"
    return 1
  fi

  local installation_id
  installation_id=$(cat "$(_github_app_installation_cache_path "$owner")" 2>/dev/null || echo "")
  # Treat a missing OR non-numeric cache value as a miss (#4456): a
  # pre-poisoned cache file holding the literal "null" (or any non-numeric
  # junk from an older build) is non-empty, so a bare `-z` test would keep
  # reusing it forever. Re-resolving self-heals such hosts.
  if [[ ! "$installation_id" =~ ^[0-9]+$ ]]; then
    installation_id=$(_github_app_api_get_installation "$nwo") || return 1
    printf '%s' "$installation_id" > "$(_github_app_installation_cache_path "$owner")"
  fi

  local cached token expires_at expires_epoch
  cached=$(_github_app_read_cache "$installation_id" || echo "")
  if [[ -n "$cached" ]]; then
    token=$(echo "$cached" | jq -r '.token')
    expires_at=$(echo "$cached" | jq -r '.expires_at')
    expires_epoch=$(_github_app_parse_iso8601 "$expires_at" || echo "")
    if ! _github_app_needs_remint "$expires_epoch"; then
      GITHUB_APP_INSTALLATION_ID="$installation_id"
      GITHUB_APP_TOKEN_EXPIRES_AT="$expires_at"
      echo "$token"
      return 0
    fi
  fi

  local mint_resp
  mint_resp=$(_github_app_api_mint_token "$installation_id") || return 1
  token=$(echo "$mint_resp" | jq -r '.token')
  expires_at=$(echo "$mint_resp" | jq -r '.expires_at')
  if [[ -z "$token" || "$token" == "null" ]]; then
    _GH_APP_LAST_ERROR="installation token mint response for installation ${installation_id} did not include a token"
    return 1
  fi
  _github_app_write_cache "$installation_id" "$token" "$expires_at"
  GITHUB_APP_INSTALLATION_ID="$installation_id"
  GITHUB_APP_TOKEN_EXPIRES_AT="$expires_at"
  echo "$token"
}

# --- Executable CLI dispatch ---
#
# `loom-daemon` invokes this file as a subprocess (never sourced) via the two
# subcommands below. Both ALWAYS exit 0 and communicate outcome through a JSON
# envelope on stdout (`status` field) -- this sidesteps relying on distinct
# process exit codes surviving a `Command` capture, and keeps the daemon-side
# parser to "read one line of JSON, branch on `.status`".
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  _gh_app_cmd="${1:-}"
  case "$_gh_app_cmd" in
    status)
      if github_app_configured; then
        jq -cn --arg app_id "$_GH_APP_ID" --arg key_path "$_GH_APP_KEY_PATH" \
          '{status:"configured", app_id:$app_id, key_path:$key_path}'
      else
        jq -cn --arg message "$_GH_APP_LAST_ERROR" '{status:"not_configured", message:$message}'
      fi
      ;;
    get-token)
      shift || true
      _nwo="${1:-}"
      if [[ -z "$_nwo" ]]; then
        jq -cn '{status:"error", message:"owner/repo argument required"}'
        exit 0
      fi
      if ! github_app_configured; then
        jq -cn --arg message "$_GH_APP_LAST_ERROR" '{status:"not_configured", message:$message}'
        exit 0
      fi
      # `github_app_get_token` sets GITHUB_APP_INSTALLATION_ID /
      # GITHUB_APP_TOKEN_EXPIRES_AT as a side effect, but `$(...)` command
      # substitution runs its command in a SUBSHELL -- those assignments never
      # propagate back to this (the parent) shell, so reading them directly
      # after the substitution always sees the pre-call (empty) values (the
      # root cause of `installation_id` always coming back empty in the CLI
      # envelope). Fix: emit them as two extra output lines from INSIDE the
      # same subshell (`github_app_get_token` itself always emits exactly one
      # trailing-newline-terminated line -- the token), then split the
      # captured 3-line output back apart out here (#5401).
      if _gh_app_out=$(
        github_app_get_token "$_nwo" &&
          printf '%s\n%s' "$GITHUB_APP_INSTALLATION_ID" "$GITHUB_APP_TOKEN_EXPIRES_AT"
      ); then
        _gh_app_token="${_gh_app_out%%$'\n'*}"
        _gh_app_rest="${_gh_app_out#*$'\n'}"
        _gh_app_installation_id="${_gh_app_rest%%$'\n'*}"
        _gh_app_expires_at="${_gh_app_rest#*$'\n'}"
        jq -cn --arg token "$_gh_app_token" --arg installation_id "$_gh_app_installation_id" \
          --arg app_id "$_GH_APP_ID" --arg expires_at "$_gh_app_expires_at" \
          '{status:"ok", token:$token, installation_id:$installation_id, app_id:$app_id, expires_at:$expires_at}'
      else
        jq -cn --arg message "$_GH_APP_LAST_ERROR" '{status:"error", message:$message}'
      fi
      ;;
    *)
      echo "Usage: github-app-token.sh {status|get-token OWNER/REPO}" >&2
      exit 64
      ;;
  esac
fi
