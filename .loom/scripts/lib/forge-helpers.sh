#!/usr/bin/env bash
# forge-helpers.sh - Forge-agnostic helpers for shell scripts
#
# Provides forge detection and API dispatch functions that allow
# Loom's shell scripts to work with both GitHub and Gitea.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/forge-helpers.sh"
#   forge_detect   # sets FORGE_TYPE to "github" or "gitea"
#
# Environment Variables:
#   LOOM_FORGE_TYPE              - Override forge detection ("github" or "gitea")
#   GITEA_TOKEN                  - API token / password for Gitea authentication
#   GITEA_URL                    - Base URL for Gitea instance (e.g. "https://gitea.example.com")
#   GITEA_USERNAME               - If set, use HTTP Basic Auth (username + password)
#                                  instead of token auth. Password is taken from
#                                  GITEA_TOKEN. Requires an https:// URL unless
#                                  LOOM_ALLOW_INSECURE_BASIC_AUTH=1.
#   LOOM_ALLOW_INSECURE_BASIC_AUTH - Set to 1 to permit Basic Auth over http://
#                                    (not recommended; for air-gapped LAN only).
#
# Forge detection priority:
#   1. LOOM_FORGE_TYPE env var
#   2. Resolved config (config-resolver tier chain) forge.type (if not "auto")
#   3. Auto-detect from git remote origin URL
#   4. Default to "github"
#
# Config root resolution (#4062, decision recorded in epic #4081): the
# resolved-config root is $REPO_ROOT (env) > $WORKSPACE_ROOT (env) > the
# CANONICAL repo root via `git rev-parse --git-common-dir` — never the
# worktree CWD. This mirrors spawn-claude.sh's #3938 precedent: forge auth is
# exactly the kind of thing that must not silently resolve against the wrong
# (worktree-local) directory.

set -euo pipefail

# ${BASH_SOURCE[0]:-$0} (not bare ${BASH_SOURCE[0]}) -- the bash+zsh-portable
# self-path idiom from #3680. Slash commands (champion-reference.md,
# champion-pr-merge.md) `source` this file DIRECTLY into the invoking shell
# via an absolute path, which on macOS is often zsh (the Bash tool's default
# shell). Under zsh, BASH_SOURCE is unset, so a bare ${BASH_SOURCE[0]}
# resolves to the shell's CWD instead of this lib dir and the source below
# fails (zsh sets $0 to the sourced file's own path, which recovers it).
_LOOM_FORGE_HELPERS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./config-resolver.sh
source "$_LOOM_FORGE_HELPERS_LIB_DIR/config-resolver.sh"

# --- Forge Detection ---

# Global forge state (set by forge_detect)
FORGE_TYPE=""
_GITEA_BASE_URL=""
_GITEA_TOKEN=""
_GITEA_USERNAME=""

# _forge_config_root -> echoes the root to resolve config from.
# Precedence: $REPO_ROOT (env) > $WORKSPACE_ROOT (env) > canonical repo root
# via `git rev-parse --git-common-dir` (parent of the common .git dir — works
# identically from the main checkout or any linked worktree) > "." as a last
# resort when git itself is unavailable (e.g. not inside a git repo).
_forge_config_root() {
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

# Detect forge type from environment, config, or remote URL.
# Sets FORGE_TYPE to "github" or "gitea".
# For Gitea, also sets _GITEA_BASE_URL and _GITEA_TOKEN.
forge_detect() {
  # Already detected
  if [[ -n "$FORGE_TYPE" ]]; then
    return 0
  fi

  # 1. Environment variable override
  local env_val="${LOOM_FORGE_TYPE:-}"
  if [[ -n "$env_val" ]]; then
    local env_lower
    env_lower=$(echo "$env_val" | tr '[:upper:]' '[:lower:]')
    case "$env_lower" in
      github) FORGE_TYPE="github"; return 0 ;;
      gitea)  FORGE_TYPE="gitea"; _load_gitea_config; return 0 ;;
    esac
  fi

  # Resolve the merged effective config ONCE per invocation (config-resolver,
  # #4062) and reuse it below for both the forge.type check and the
  # forge.gitea.url autodetect fallback — never re-merge the tier chain per
  # key within a single call.
  local _forge_root _forge_cfg
  _forge_root=$(_forge_config_root)
  _forge_cfg=$(loom_resolve_config "$_forge_root")

  # 2. Resolved config — forge.type (if not "auto")
  if command -v jq >/dev/null 2>&1; then
    local config_type
    config_type=$(echo "$_forge_cfg" | jq -r '.forge.type // "auto"' 2>/dev/null || echo "auto")
    local config_lower
    config_lower=$(echo "$config_type" | tr '[:upper:]' '[:lower:]')
    case "$config_lower" in
      github) FORGE_TYPE="github"; return 0 ;;
      gitea)  FORGE_TYPE="gitea"; _load_gitea_config; return 0 ;;
    esac
  fi

  # 3. Auto-detect from git remote URL
  local remote_url
  remote_url=$(git remote get-url origin 2>/dev/null || echo "")
  if [[ -n "$remote_url" ]]; then
    local host
    host=$(_extract_host "$remote_url")
    if [[ "$host" == "github.com" ]]; then
      FORGE_TYPE="github"
      return 0
    fi
    # Check if host matches configured Gitea URL
    if command -v jq >/dev/null 2>&1; then
      local gitea_url
      gitea_url=$(echo "$_forge_cfg" | jq -r '.forge.gitea.url // ""' 2>/dev/null || echo "")
      if [[ -n "$gitea_url" ]]; then
        local gitea_host
        gitea_host=$(_extract_host "$gitea_url")
        if [[ "$host" == "$gitea_host" ]]; then
          FORGE_TYPE="gitea"
          _load_gitea_config
          return 0
        fi
      fi
    fi
  fi

  # 4. Default to GitHub
  FORGE_TYPE="github"
}

# Extract hostname from a URL (SSH or HTTPS)
_extract_host() {
  local url="$1"
  # SSH: git@host:owner/repo.git
  if [[ "$url" =~ ^git@([^:]+): ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  # HTTPS: https://host/...
  if [[ "$url" =~ ^https?://([^/]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  echo ""
}

# Load Gitea configuration (URL, token/password, and optional username for Basic Auth)
_load_gitea_config() {
  # Token: env var first, then config
  _GITEA_TOKEN="${GITEA_TOKEN:-}"

  # URL: env var first, then config
  _GITEA_BASE_URL="${GITEA_URL:-}"

  # Username: env var first, then config. When set, switches to HTTP Basic Auth.
  _GITEA_USERNAME="${GITEA_USERNAME:-}"

  # Resolve the merged effective config ONCE (config-resolver, #4062) — only
  # when at least one of the three env vars above didn't already win, and
  # only once regardless of how many of the three keys are still missing.
  if [[ -z "$_GITEA_TOKEN" || -z "$_GITEA_BASE_URL" || -z "$_GITEA_USERNAME" ]] && command -v jq >/dev/null 2>&1; then
    local _forge_root _forge_cfg
    _forge_root=$(_forge_config_root)
    _forge_cfg=$(loom_resolve_config "$_forge_root")

    if [[ -z "$_GITEA_TOKEN" ]]; then
      _GITEA_TOKEN=$(echo "$_forge_cfg" | jq -r '.forge.gitea.token // ""' 2>/dev/null || echo "")
    fi
    if [[ -z "$_GITEA_BASE_URL" ]]; then
      _GITEA_BASE_URL=$(echo "$_forge_cfg" | jq -r '.forge.gitea.url // ""' 2>/dev/null || echo "")
    fi
    if [[ -z "$_GITEA_USERNAME" ]]; then
      _GITEA_USERNAME=$(echo "$_forge_cfg" | jq -r '.forge.gitea.username // ""' 2>/dev/null || echo "")
    fi
  fi

  _GITEA_BASE_URL="${_GITEA_BASE_URL%/}"  # strip trailing slash
}

# Validate the Gitea Basic Auth configuration. Refuses http:// URLs when a
# username is set (since Basic Auth over plaintext would leak the password)
# unless LOOM_ALLOW_INSECURE_BASIC_AUTH=1 is explicitly exported.
# Returns 0 if the configuration is safe to use, 1 (with stderr message) otherwise.
# Does not log the password or username.
_gitea_validate_basic_auth() {
  if [[ -z "$_GITEA_USERNAME" ]]; then
    return 0
  fi
  # Username with ':' would corrupt the Basic-Auth user:pass split (RFC 7617).
  if [[ "$_GITEA_USERNAME" == *:* ]]; then
    echo "Error: GITEA_USERNAME may not contain ':' (HTTP Basic Auth disallows colons in usernames)." >&2
    return 1
  fi
  if [[ "$_GITEA_BASE_URL" == http://* ]]; then
    if [[ "${LOOM_ALLOW_INSECURE_BASIC_AUTH:-}" != "1" ]]; then
      echo "Error: Gitea Basic Auth requires HTTPS to avoid leaking credentials." >&2
      echo "       Set forge.gitea.url (or GITEA_URL) to an https:// URL, or set" >&2
      echo "       LOOM_ALLOW_INSECURE_BASIC_AUTH=1 to override (not recommended)." >&2
      return 1
    fi
  fi
  return 0
}

# --- Gitea API Helper ---

# Make a Gitea API request using curl.
# Usage: gitea_api METHOD path [curl-args...]
# Returns: response body on stdout, exit code 0 on 2xx, 1 on error
gitea_api() {
  local method="$1"
  local path="$2"
  shift 2

  if [[ -z "$_GITEA_BASE_URL" ]]; then
    echo "Error: Gitea base URL not configured" >&2
    return 1
  fi
  if [[ -z "$_GITEA_TOKEN" ]]; then
    # In Basic Auth mode, the "token" field carries the password.
    if [[ -n "$_GITEA_USERNAME" ]]; then
      echo "Error: Gitea password (GITEA_TOKEN / forge.gitea.token) not configured" >&2
    else
      echo "Error: Gitea token not configured" >&2
    fi
    return 1
  fi

  # Enforce HTTPS guard if Basic Auth is in use.
  if ! _gitea_validate_basic_auth; then
    return 1
  fi

  local url="${_GITEA_BASE_URL}/api/v1/${path#/}"
  local http_code
  local response

  if [[ -n "$_GITEA_USERNAME" ]]; then
    # HTTP Basic Auth (username + password). curl handles base64 encoding
    # of "user:pass" internally; we never echo the password to the log.
    response=$(curl -s -w "\n%{http_code}" \
      -X "$method" \
      -u "${_GITEA_USERNAME}:${_GITEA_TOKEN}" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json" \
      "$@" \
      "$url" 2>/dev/null)
  else
    # Token auth (existing behavior, unchanged).
    response=$(curl -s -w "\n%{http_code}" \
      -X "$method" \
      -H "Authorization: token $_GITEA_TOKEN" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json" \
      "$@" \
      "$url" 2>/dev/null)
  fi

  http_code=$(echo "$response" | tail -1)
  local body
  body=$(echo "$response" | sed '$d')

  if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
    echo "$body"
    return 0
  else
    echo "$body" >&2
    return 1
  fi
}

# --- Owner/Repo Helpers ---

# Extract owner and repo from NWO (name-with-owner) string.
# Usage: forge_split_nwo "owner/repo"
# Outputs: sets FORGE_OWNER and FORGE_REPO
forge_split_nwo() {
  local nwo="$1"
  FORGE_OWNER="${nwo%%/*}"
  FORGE_REPO="${nwo#*/}"
}

# --- Forge-Dispatched Operations ---

# Merge a PR via the forge API.
# Usage: forge_merge_pr NWO PR_NUMBER [EXPECTED_HEAD_SHA]
# GitHub: PUT /repos/{nwo}/pulls/{n}/merge with merge_method=squash
# Gitea: POST /repos/{owner}/{repo}/pulls/{n}/merge with Do=squash
#
# EXPECTED_HEAD_SHA (optional, #5579): an optimistic-concurrency precondition —
# the SHA the PR's head branch must currently match for the merge to proceed.
# Without it, both forges will happily squash-merge whatever the CURRENT head
# is at the moment the request lands, even if it has commits the caller never
# saw approved (silently stranding them — squash-merge makes this invisible to
# an ancestry check afterward, since the new squash commit is not a descendant
# of the stranded commits either way). Pass the freshest possible head-SHA read
# (never a cached one) immediately before calling this.
#
# GitHub: REST's optional `sha` field. Verified (GitHub's public OpenAPI spec,
# 2026-08-07) to fail with HTTP 409 and message "Head branch was modified.
# Review and try the merge again." on a mismatch — a DIFFERENT string from the
# existing "Base branch was modified" retry case handled elsewhere in
# merge-pr.sh; callers must not conflate the two (that one means "rebase onto
# base and retry"; this one means "the approved diff moved out from under us,
# do not retry-and-merge-anyway").
#
# Gitea: the `head_commit_id` field on MergePullRequestOption (confirmed
# present via Gitea/Forgejo's published swagger.v1.json and upstream
# services/pull/merge_prepare.go, 2026-08-07). A mismatch raises
# ErrSHADoesNotMatch, which routers/api/v1/repo/pull.go maps to HTTP 409 with
# message "head out of date".
forge_merge_pr() {
  local nwo="$1"
  local pr_number="$2"
  local expected_head_sha="${3:-}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    if [[ -n "$expected_head_sha" ]]; then
      gitea_api POST "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/merge" \
        -d "$(jq -nc --arg sha "$expected_head_sha" \
          '{"Do":"squash","delete_branch_after_merge":false,"head_commit_id":$sha}')"
    else
      gitea_api POST "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/merge" \
        -d '{"Do":"squash","delete_branch_after_merge":false}'
    fi
  else
    # Routed through the #6074 permission ladder (#6752): a stale/scope-limited
    # App installation token 403s this PUT with "Resource not accessible by
    # integration", which is recoverable with a stronger credential -- the
    # failure that killed the merge of PR #6751 on 2026-08-22. Every other
    # failure (409 head-mismatch, "Base branch was modified", 405 merge in
    # progress) carries no such signature and falls through unretried, so the
    # callers' existing substring classifiers are unaffected.
    if [[ -n "$expected_head_sha" ]]; then
      forge_gh_perm_safe api "repos/$nwo/pulls/$pr_number/merge" \
        -X PUT \
        -f merge_method=squash \
        -f sha="$expected_head_sha" 2>&1
    else
      forge_gh_perm_safe api "repos/$nwo/pulls/$pr_number/merge" \
        -X PUT \
        -f merge_method=squash 2>&1
    fi
  fi
}

# Update a PR branch (rebase on base branch).
# Usage: forge_update_branch NWO PR_NUMBER
# GitHub: PUT /repos/{nwo}/pulls/{n}/update-branch
# Gitea: POST /repos/{owner}/{repo}/pulls/{n}/update
forge_update_branch() {
  local nwo="$1"
  local pr_number="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api POST "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/update"
  else
    gh api "repos/$nwo/pulls/$pr_number/update-branch" -X PUT 2>&1
  fi
}

# Get PR details.
# Usage: forge_get_pr NWO PR_NUMBER
# Returns JSON with .state, .merged, .head.ref, .title, .mergeable
forge_get_pr() {
  local nwo="$1"
  local pr_number="$2"
  local gh_cmd="${3:-gh}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number"
  else
    "$gh_cmd" api "repos/$nwo/pulls/$pr_number" 2>/dev/null
  fi
}

# Get PR details without cache (for race-condition rechecks).
# Usage: forge_get_pr_nocache NWO PR_NUMBER
forge_get_pr_nocache() {
  local nwo="$1"
  local pr_number="$2"
  local gh_cmd="${3:-gh}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    # Gitea has no caching layer like gh-cached
    forge_get_pr "$nwo" "$pr_number"
  elif [[ "$(basename "$gh_cmd")" == "gh" ]]; then
    # Plain `gh` has no --no-cache flag (it's a gh-cached wrapper flag). Plain
    # `gh api` is already uncached, so calling it without --no-cache preserves
    # the no-cache intent. Passing --no-cache to plain gh fails on the unknown
    # flag, and with 2>/dev/null the error is swallowed and callers substitute
    # '{}', silently breaking merge verification and race-condition rechecks
    # whenever gh-cached is absent (issue #3547).
    "$gh_cmd" api "repos/$nwo/pulls/$pr_number" 2>/dev/null
  else
    # gh-cached wrapper: --no-cache bypasses its cache layer as intended.
    "$gh_cmd" --no-cache api "repos/$nwo/pulls/$pr_number" 2>/dev/null
  fi
}

# Get an issue's open/closed state.
# Usage: forge_get_issue_state NWO ISSUE_NUMBER [GH_CMD]
# Returns on stdout: "OPEN" or "CLOSED". On any lookup failure or an
# unrecognized/empty state value, prints nothing and returns exit code 1.
#
# Fail-unsafe-to-preserve contract (#4186): this is used to gate destructive
# cleanup (e.g. merge-pr.sh's worktree removal), so callers MUST treat a
# non-zero return (empty stdout) as "assume the issue might still be open"
# and preserve whatever resource the check gates — never assume CLOSED on a
# lookup failure. This function only ever reports CLOSED when the forge
# unambiguously says so.
forge_get_issue_state() {
  local nwo="$1"
  local issue_number="$2"
  local gh_cmd="${3:-gh}"
  local raw_state=""

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    raw_state=$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/issues/$issue_number" 2>/dev/null \
      | jq -r '.state // empty' 2>/dev/null) || true
  else
    raw_state=$("$gh_cmd" api "repos/$nwo/issues/$issue_number" --jq '.state // empty' 2>/dev/null) || true
  fi

  case "$(echo "$raw_state" | tr '[:lower:]' '[:upper:]')" in
    OPEN)
      echo "OPEN"
      ;;
    CLOSED)
      echo "CLOSED"
      ;;
    *)
      return 1
      ;;
  esac
}

# Check if repo auto-deletes branches on merge.
# Usage: forge_check_auto_delete NWO
# Returns: "true" or "false" on stdout
forge_check_auto_delete() {
  local nwo="$1"
  local gh_cmd="${2:-gh}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    local repo_json
    repo_json=$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO" 2>/dev/null) || {
      echo "false"
      return
    }
    echo "$repo_json" | jq -r '.default_delete_branch_after_merge // false'
  else
    "$gh_cmd" api "repos/$nwo" --jq '.delete_branch_on_merge' 2>/dev/null || echo "false"
  fi
}

# Check whether the repository has GitHub's "Allow auto-merge" setting enabled.
# Usage: forge_check_auto_merge_allowed NWO [GH_CMD]
# Returns on stdout: "true", "false", or "unknown".
#
# GitHub only: reads the repo-level `allow_auto_merge` flag. When it is false,
# GitHub rejects the enablePullRequestAutoMerge mutation outright — no PR-level
# state (CLEAN/UNSTABLE) will ever let it succeed — so callers that want to
# degrade gracefully (wait-for-checks-then-merge) can detect it up front rather
# than reacting to the post-mutation error string (#3820).
#
# Gitea returns "unknown" (there is no equivalent single repo flag consumed
# here; Gitea auto-merge goes through forge_auto_merge's own curl poll-and-merge,
# which this probe must not perturb). A probe failure (network/auth/unexpected value)
# also returns "unknown" so callers preserve their existing behavior fail-safe.
forge_check_auto_merge_allowed() {
  local nwo="$1"
  local gh_cmd="${2:-gh}"

  if [[ "$FORGE_TYPE" != "github" ]]; then
    echo "unknown"
    return 0
  fi

  local val
  val="$("$gh_cmd" api "repos/$nwo" --jq '.allow_auto_merge' 2>/dev/null)" || {
    echo "unknown"
    return 0
  }
  case "$val" in
    true)  echo "true" ;;
    false) echo "false" ;;
    *)     echo "unknown" ;;
  esac
}

# Delete a remote branch.
# Usage: forge_delete_branch NWO BRANCH_NAME
# GitHub: DELETE /repos/{nwo}/git/refs/heads/{branch}
# Gitea: DELETE /repos/{owner}/{repo}/branches/{branch}
forge_delete_branch() {
  local nwo="$1"
  local branch="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api DELETE "repos/$FORGE_OWNER/$FORGE_REPO/branches/$branch" 2>/dev/null
  else
    gh api "repos/$nwo/git/refs/heads/$branch" -X DELETE 2>/dev/null
  fi
}

# Enable auto-merge on a PR.
# Usage: forge_auto_merge NWO PR_NUMBER [EXPECTED_HEAD_SHA]
# GitHub: GraphQL enablePullRequestAutoMerge mutation (pure API, no
#         working-tree dependency — `gh pr merge --auto` does a local
#         checkout that collides with worktrees owning the head branch).
# Gitea: POST /repos/{owner}/{repo}/pulls/{n}/merge with merge_when_checks_succeed
#
# EXPECTED_HEAD_SHA (optional, #5579): same optimistic-concurrency precondition
# as forge_merge_pr's — see that function's comment for the general rationale
# and the Gitea `head_commit_id` citation (identical here; Gitea's `/merge`
# endpoint carries both the auto-merge poll flags and the mismatch guard).
#
# GitHub: the GraphQL mutation's `expectedHeadOid: GitObjectID` input field
# (confirmed present in GitHub's public GraphQL schema, 2026-08-07). The exact
# error string GitHub returns on a mismatch could NOT be verified against a
# live incident or public documentation as of this writing (GraphQL validation
# error text is not part of the published schema) — merge-pr.sh's classifier
# for this path therefore matches a best-effort pattern and should be
# tightened against the first real occurrence, the same way the CLEAN/UNSTABLE
# classifiers elsewhere in this file were derived from live incident text.
forge_auto_merge() {
  local nwo="$1"
  local pr_number="$2"
  local expected_head_sha="${3:-}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    if [[ -n "$expected_head_sha" ]]; then
      gitea_api POST "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/merge" \
        -d "$(jq -nc --arg sha "$expected_head_sha" \
          '{"Do":"squash","merge_when_checks_succeed":true,"delete_branch_after_merge":true,"head_commit_id":$sha}')"
    else
      gitea_api POST "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/merge" \
        -d '{"Do":"squash","merge_when_checks_succeed":true,"delete_branch_after_merge":true}'
    fi
  else
    # Resolve PR node_id (required by GraphQL mutation).
    local node_id
    node_id=$(gh api "repos/$nwo/pulls/$pr_number" --jq '.node_id' 2>/dev/null) || return 1
    [[ -z "$node_id" ]] && return 1

    # The mutation (a WRITE) goes through the #6074 permission ladder (#6752),
    # like the native `loom-daemon forge auto-merge` this shell path stands in
    # for; the node_id lookup above is a read and needs no escalation.
    if [[ -n "$expected_head_sha" ]]; then
      local mutation_with_oid='mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!, $expectedHeadOid: GitObjectID) { enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod, expectedHeadOid: $expectedHeadOid}) { pullRequest { number autoMergeRequest { enabledAt } } } }'

      forge_gh_perm_safe api graphql \
        -f "query=$mutation_with_oid" \
        -F "pullRequestId=$node_id" \
        -F "mergeMethod=SQUASH" \
        -F "expectedHeadOid=$expected_head_sha" 2>/dev/null
    else
      local mutation='mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) { enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod}) { pullRequest { number autoMergeRequest { enabledAt } } } }'

      forge_gh_perm_safe api graphql \
        -f "query=$mutation" \
        -F "pullRequestId=$node_id" \
        -F "mergeMethod=SQUASH" 2>/dev/null
    fi
  fi
}

# --- CI Status Helpers ---

# Distinguished exit code forge_get_check_runs returns when the forge's own
# response makes clear the failure is a genuine HTTP 404 ("no such
# resource"), as opposed to any other failure (network blip, 5xx, auth,
# rate-limit). Callers that poll this function (merge-pr.sh's
# `_wait_for_checks_then_sync_merge()` and its UNSTABLE-fallback sibling) use
# this to tell "this repo has no check-runs to wait for" (e.g. GitHub Actions
# disabled, #6389) apart from a transient fetch failure that should keep
# polling. Any other nonzero return remains the generic "transient failure"
# signal (return 1) so existing bounded-poll behavior is unchanged.
FORGE_CHECK_RUNS_RC_NOT_FOUND=44

# Get CI check runs for a commit.
# Usage: forge_get_check_runs NWO COMMIT_SHA
# GitHub: GET /repos/{nwo}/commits/{sha}/check-runs
# Gitea: GET /repos/{owner}/{repo}/commits/{sha}/statuses (mapped to check-run shape)
#
# Return codes: 0 success (JSON on stdout); $FORGE_CHECK_RUNS_RC_NOT_FOUND
# (44) on a confirmed HTTP 404 (GitHub only — see below); 1 for any other
# failure.
forge_get_check_runs() {
  local nwo="$1"
  local commit="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    local statuses
    statuses=$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/commits/$commit/statuses" 2>/dev/null) || {
      echo '{"total_count":0,"check_runs":[]}'
      return 1
    }

    # Map Gitea commit statuses to GitHub check-run shape.
    # Gitea status field: pending, success, error, failure, warning
    # GitHub check run: status=completed/queued/in_progress, conclusion=success/failure/...
    echo "$statuses" | jq '{
      total_count: (. | length),
      check_runs: [.[] | {
        name: .context,
        status: (if .status == "pending" then "queued"
                 else "completed" end),
        conclusion: (if .status == "success" then "success"
                     elif .status == "failure" then "failure"
                     elif .status == "error" then "failure"
                     elif .status == "warning" then "neutral"
                     elif .status == "pending" then null
                     else null end),
        html_url: .target_url
      }]
    }'
  else
    # Capture stdout and stderr into separate temp files so a non-2xx
    # response's HTTP status (which `gh api` reports only on stderr, as
    # "... (HTTP <code>)") can be inspected without disturbing the JSON
    # payload on success (#6389).
    local out_file err_file rc=0
    out_file=$(mktemp)
    err_file=$(mktemp)
    gh api "repos/$nwo/commits/$commit/check-runs" \
      --header "Accept: application/vnd.github+json" \
      --jq '{
        total_count: .total_count,
        check_runs: [.check_runs[] | {
          name: .name,
          status: .status,
          conclusion: .conclusion,
          html_url: .html_url
        }]
      }' >"$out_file" 2>"$err_file" || rc=$?

    if [[ $rc -ne 0 ]]; then
      if grep -q "HTTP 404" "$err_file" 2>/dev/null; then
        rm -f "$out_file" "$err_file"
        return "$FORGE_CHECK_RUNS_RC_NOT_FOUND"
      fi
      rm -f "$out_file" "$err_file"
      return 1
    fi
    cat "$out_file"
    rm -f "$out_file" "$err_file"
  fi
}

# Get combined commit status.
# Usage: forge_get_commit_status NWO COMMIT_SHA
# Both forges support GET /repos/{nwo}/commits/{sha}/status
forge_get_commit_status() {
  local nwo="$1"
  local commit="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    local status_json
    status_json=$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/commits/$commit/status" 2>/dev/null) || {
      echo '{"state": "unknown", "statuses": []}'
      return 0
    }
    # Map Gitea's "warning" state to "pending" for compatibility
    echo "$status_json" | jq '{
      state: (if .state == "warning" then "pending" else .state end),
      statuses: [(.statuses // [])[] | {
        context: .context,
        state: .state,
        target_url: .target_url
      }]
    }'
  else
    gh api "repos/$nwo/commits/$commit/status" \
      --header "Accept: application/vnd.github+json" \
      --jq '{
        state: .state,
        statuses: [.statuses[] | {
          context: .context,
          state: .state,
          target_url: .target_url
        }]
      }' 2>/dev/null
  fi
}

# Get GitHub Actions workflow runs (or the Gitea Actions equivalent) for a
# commit, independent of the Checks API.
# Usage: forge_get_workflow_runs NWO COMMIT_SHA
# Returns JSON: {"workflow_runs": [{"name": ..., "status": ..., "conclusion": ...}]}
#
# Why this exists (#5495): the Checks API (forge_get_check_runs, above) only
# ever reports check-runs that already exist -- a workflow_run that is still
# `queued` and has not yet dispatched a single job has ZERO check-runs, so it
# is completely invisible to analyze_status()'s counts. If a handful of
# other, faster/independent workflows for the same commit have already
# completed, `success > 0 && pending == 0` was satisfied and the overall
# status was reported as "success" even though the primary CI workflow
# hadn't run a single job yet. This helper queries workflow-run state
# directly (not check-run state) so a still-queued/in_progress run can be
# folded into the pending count regardless of how many check-runs exist.
#
# GitHub: GET /repos/{nwo}/actions/runs?head_sha={sha} -- authoritative,
#   filtered server-side by head_sha.
# Gitea: GET /repos/{owner}/{repo}/actions/tasks -- Gitea's Actions task-list
#   API has no head_sha filter, so this filters client-side over the
#   (default first page of) returned tasks. This is best-effort: a commit
#   whose task fell off the first page would not be found, degrading back to
#   the pre-#5495 behavior for that commit rather than failing loudly. Any
#   fetch/parse failure returns an empty list the same way, so callers can
#   treat "no signal" identically to "definitely not pending" -- deliberately
#   fail-open here (unlike e.g. forge_get_issue_state's fail-unsafe contract)
#   because this only ever *adds* to the pending count; a false negative just
#   reproduces the exact false-success bug this helper exists to fix, never
#   a new failure mode.
forge_get_workflow_runs() {
  local nwo="$1"
  local commit="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    local tasks_json
    tasks_json=$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/actions/tasks" 2>/dev/null) || {
      echo '{"workflow_runs": []}'
      return 0
    }
    echo "$tasks_json" | jq --arg sha "$commit" '{
      workflow_runs: [(.workflow_runs // [])[] | select(.head_sha == $sha) | {
        name: (.name // .display_title // "workflow"),
        status: .status,
        conclusion: (.conclusion // null)
      }]
    }' 2>/dev/null || echo '{"workflow_runs": []}'
  else
    local runs_json
    runs_json=$(gh api "repos/$nwo/actions/runs?head_sha=$commit&per_page=100" \
      --header "Accept: application/vnd.github+json" 2>/dev/null) || {
      echo '{"workflow_runs": []}'
      return 0
    }
    echo "$runs_json" | jq '{
      workflow_runs: [(.workflow_runs // [])[] | {
        name: .name,
        status: .status,
        conclusion: .conclusion
      }]
    }' 2>/dev/null || echo '{"workflow_runs": []}'
  fi
}

# --- PR Listing Helpers ---

# List merged PRs.
# Usage: forge_list_merged_prs NWO LIMIT [DATE_FILTER]
# GitHub: gh pr list --state merged
# Gitea: GET /repos/{owner}/{repo}/pulls?state=closed + client-side merge filter
forge_list_merged_prs() {
  local nwo="$1"
  local limit="$2"
  local date_filter="${3:-}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    local page=1
    local per_page=50
    local collected=0
    local results="[]"

    while [[ $collected -lt $limit ]]; do
      local batch
      batch=$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/pulls?state=closed&sort=updated&limit=$per_page&page=$page" 2>/dev/null) || break

      local batch_len
      batch_len=$(echo "$batch" | jq 'length')
      [[ "$batch_len" -eq 0 ]] && break

      # Filter to merged PRs and optionally by date
      local filtered
      if [[ -n "$date_filter" ]]; then
        filtered=$(echo "$batch" | jq --arg df "$date_filter" '[.[] | select(.merged == true and .merged_at != null and .merged_at >= $df) | {number: .number, mergedAt: .merged_at}]')
      else
        filtered=$(echo "$batch" | jq '[.[] | select(.merged == true) | {number: .number, mergedAt: .merged_at}]')
      fi

      results=$(echo "$results" "$filtered" | jq -s '.[0] + .[1]')
      collected=$(echo "$results" | jq 'length')

      # If we got a full page, there may be more
      [[ "$batch_len" -lt "$per_page" ]] && break
      page=$((page + 1))

      # Rate limiting protection for Gitea
      sleep 0.2
    done

    # Trim to limit and output just the numbers
    echo "$results" | jq -r ".[:$limit] | .[].number"
  else
    if [[ -n "$date_filter" ]]; then
      gh pr list --state merged --limit "$limit" --json number,mergedAt \
        --jq '[.[] | select(.mergedAt >= "'"$date_filter"'")] | .[].number' 2>/dev/null || echo ""
    else
      gh pr list --state merged --limit "$limit" --json number --jq '.[].number' 2>/dev/null || echo ""
    fi
  fi
}

# Get PR body.
# Usage: forge_get_pr_body NWO PR_NUMBER
forge_get_pr_body() {
  local nwo="$1"
  local pr_number="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number" 2>/dev/null | jq -r '.body // ""'
  else
    gh pr view "$pr_number" --json body --jq '.body // ""' 2>/dev/null || echo ""
  fi
}

# Get issue numbers that a PR will close when merged.
#
# Usage: forge_pr_close_targets PR_NUMBER [GH_CMD]
# Outputs: One issue number per line on stdout, sorted and de-duplicated.
#
# GitHub: Uses GraphQL `closingIssuesReferences` via `gh pr view`. This is
#   GitHub's authoritative parse of the PR body — it correctly handles case
#   sensitivity, word boundaries, fenced code blocks, and the full list of
#   closing keywords (close/closes/closed, fix/fixes/fixed, resolve/resolves/
#   resolved). It also follows GitHub's own rule that "Updates #N", "See #N",
#   and "References #N" do NOT close the issue.
#
# Gitea: The Gitea API does not expose an equivalent of closingIssuesReferences,
#   so this falls back to a word-boundary regex over the PR body. The regex
#   only matches the canonical closing keywords (case-insensitive), so plain
#   `Updates #N` is correctly ignored. The substring trap (e.g. `Discloses #N`)
#   is also avoided thanks to the leading `\b`. Note that this is a syntactic
#   approximation — it does not strip fenced code blocks or quoted text.
#
# This helper replaces the brittle `grep -Eo "(Closes|Fixes|Resolves) #[0-9]+"`
# that previously appeared in Champion's "Verify Issue Auto-Close" step. That
# regex silently misclassified `Updates #N` (and various substring traps) as
# closing references, causing Champion to manually close tracking issues that
# were intentionally left open. See issue #3267 for the full history.
forge_pr_close_targets() {
  local pr_number="$1"
  local gh_cmd="${2:-gh}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    # Gitea fallback: word-boundary regex over the PR body.
    # We need the NWO to fetch the body; assume the caller's working repo.
    local nwo
    nwo=$(forge_get_repo_nwo "$gh_cmd") || return 0
    local body
    body=$(forge_get_pr_body "$nwo" "$pr_number")
    # Word-boundary, case-insensitive match on canonical closing keywords only.
    # `Updates`, `See`, `References` are deliberately excluded.
    # `|| true` neutralizes grep's exit-1 (no match) under `set -e`.
    { echo "$body" \
        | grep -Eoi '\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[[:space:]]+#[0-9]+' \
        | grep -Eo '[0-9]+' \
        | sort -un; } || true
  else
    { "$gh_cmd" pr view "$pr_number" --json closingIssuesReferences \
        --jq '.closingIssuesReferences[].number' 2>/dev/null \
        | sort -un; } || true
  fi
}

# ---------------------------------------------------------------------------
# GraphQL-exhaustion REST fallback for label/comment/state mutations (#4856).
#
# `gh issue edit`, `gh issue comment`, `gh issue reopen`, and `gh pr comment`
# are GraphQL-backed mutations. During a long sweep, GraphQL quota (5000/hr,
# shared across every agent + tool) can exhaust while REST quota still has
# headroom -- the same independent-quota fact the read-side fallback in
# `check-duplicate.sh` and merge-pr.sh's #4447 auto-merge-enable fallback
# already rely on. Before this fix, the best-effort mutating call sites in
# merge-pr.sh (partial-increment label reset, premature-auto-close reopen,
# stacked-child deferral comment) simply swallowed a rate-limit rejection
# with the same generic warning as any other failure, silently dropping the
# label/comment update instead of retrying over REST -- the exact incident
# reported in #4856 (an orchestrator working around it by hand with raw
# `gh api` DELETE/POST/PATCH calls).
#
# is_rate_limit_error() reuses the exact five-signature table from
# check-duplicate.sh's is_rate_limit_error() (itself mirrored from
# loom-daemon/src/rate_limit_breaker.rs's RATE_LIMIT_SIGNATURES) rather than
# deriving a new one. The GraphQL and REST phrasings are NOT substrings of
# each other -- "already" breaks the contiguous "api rate limit exceeded"
# match -- so both are listed. GitHub-only helpers: every call site below is
# already gated on `[[ "$FORGE_TYPE" == "github" ]]` by its caller, mirroring
# the existing `_reset_partial_increment_labels` / `_auto_reconcile_stacked_children`
# gating in merge-pr.sh, so no Gitea branch is needed here.
#
# #5047 extended this same table + fallback shape to issue *creation*
# (`forge_gh_create_issue_rl_safe`, below) -- the one filing mutation #4856
# left uncovered, since #4856 was scoped to labels/comments/reopen on
# already-existing issues.
is_rate_limit_error() {
  local text
  text=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$text" in
    *"api rate limit exceeded"*) return 0 ;;
    *"api rate limit already exceeded"*) return 0 ;;
    *"secondary rate limit"*) return 0 ;;
    *"abuse detection mechanism"*) return 0 ;;
    *"was submitted too quickly"*) return 0 ;;
  esac
  return 1
}

# ---------------------------------------------------------------------------
# GitHub App installation-token permission-scope 403 escalation (#6074).
#
# A GitHub App installation token is minted with the permissions the
# installation held AT MINT TIME and then reused from an on-disk cache for up
# to ~1h. So there is a window -- after a permission grant has already
# propagated on GitHub's side, before the cached token ages out -- where one
# write scope is present and another is not. Observed live 2026-08-12
# (example-org/fleet-repo#304): a Builder's `git push` SUCCEEDED (Contents:write was in
# the cached token) and the very next `gh pr create` returned
#
#     HTTP 403: Resource not accessible by integration
#
# because Pull-requests:write was not. The sweep died with no PR, the issue
# stayed ready, the daemon re-dispatched it, and the next Builder rebuilt the
# identical work -- one full duplicate build per pass, plus an orphaned
# pushed-but-PR-less `feature/issue-*` branch (tool-repo#205 rebuilt 3+
# times before a human opened the PR by hand).
#
# This is a DIFFERENT failure from both neighbours it is easy to conflate with:
#
#   * NOT rate-limit exhaustion. `is_rate_limit_error` (above) and the sweep's
#     "anything else is NOT exhaustion" rule both stay exactly as they are --
#     a REST retry with the same token 403s identically. The remedy here is a
#     different CREDENTIAL, not a different transport.
#   * NOT a mint failure. `run_with_github_app` (credential_preflight.rs)
#     already falls back to ambient `gh` auth when the token cannot be minted
#     AT ALL. Here the mint succeeded; the token is valid and simply carries a
#     stale permission set, so nothing upstream notices.
#
# The ladder below is therefore deliberately narrow -- it fires ONLY on this
# one signature, and each rung is a strictly stronger credential:
#
#   1. the ambient credential (whatever `gh` already resolves)
#   2. a FORCE-MINTED installation token (bypasses the ~1h cache, so an
#      already-propagated grant is picked up immediately instead of waited out)
#   3. a personal token -- `LOOM_PERSONAL_GH_TOKEN` if set, else the operator's
#      own `gh auth login` credential, reached by dropping the daemon-owned
#      `GH_CONFIG_DIR`/`GH_TOKEN` that shadow it (#4458)
#
# Every other failure -- including a 403 that is a genuine permission
# misconfiguration on a personal token, or a 404, or a rate limit -- falls
# straight through unretried, exactly as before.
#
# #6752 widened the ladder from "`gh` invocations" to "any command whose
# credential comes from the environment". The merge itself was the hole: on
# 2026-08-22 `merge-pr.sh --auto` on PR #6751 died with the exact
# integration-403 above, because BOTH of its merge writes bypassed this ladder
# -- the native `loom-daemon forge auto-merge` (its own Rust code path) and the
# shell `forge_merge_pr` REST PUT. The orchestrator recovered by hand with
# `unset GH_CONFIG_DIR`, i.e. rung 3, done manually. `forge_cmd_perm_safe`
# below is the same ladder with the `gh` literal lifted out, so the native
# subcommand escalates through the identical rungs (`loom-daemon forge` shells
# out to `gh`, so it reads the same GH_TOKEN/GH_CONFIG_DIR the rungs swap);
# `forge_gh_perm_safe` is now just its `gh`-prefixed spelling.

# is_app_permission_error <text> -> 0 when the text carries GitHub's
# App-installation permission-scope rejection. Matched on the distinctive
# "not accessible by integration" phrase (GitHub's wording for "this
# credential is an App installation that lacks the required permission"),
# which no rate-limit or generic-auth message contains.
is_app_permission_error() {
  local text
  text=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$text" in
    *"not accessible by integration"*) return 0 ;;
  esac
  return 1
}

# ---------------------------------------------------------------------------
# Forge-transient (outage) vs. credential/permission fault discrimination
# (issue #6425).
#
# Incident, 2026-08-17: during a confirmed GitHub partial outage (Issues API
# and Git ops degraded per githubstatus; the fleet's own claim_reconciliation
# logged `HTTP 503: No server is currently available`), two sweeps hit forge
# WRITE failures and wrote a confident CREDENTIAL diagnosis into their
# operator-facing summaries -- "this needs operator attention, not a retry ...
# the GitHub App installation token lacking write permission" -- with an
# explicit "Action needed from you" line. Both were wrong: the first PR merged
# normally 17 minutes later with no permission change, and the second repo's
# writes resumed once GitHub recovered. One of the two summaries even recorded
# that `gh api /user` ALSO 403'd on the same token (a signal that should have
# pointed at an outage, since a permission-SCOPE gap does not usually take
# down an unrelated read) and still concluded "permissions".
#
# The fix is two functions, used together by every write call site / summary
# writer that would otherwise assert a credential diagnosis:
#
#   is_forge_transient_error <text>       -> 0 when the text is an outage
#       signature (5xx, "No server is currently available", a network reset)
#       that no retry-with-a-different-credential can fix; the correct
#       response is "retry later", never an operator action item.
#
#   forge_write_permission_confirmed <write_error_text>
#                                          -> 0 ONLY when there is POSITIVE
#       evidence of a genuine, scoped permission fault: the write's own error
#       is not itself a forge-transient signature, AND a cheap read
#       (`gh api /rate_limit`) on the SAME credential context succeeds. A
#       failing read is evidence of a broader outage/token problem, not a
#       narrow scope gap, so it does NOT confirm a permission fault -- return
#       1, the same as when the read is never run.
#
# Every caller (sweep.md's merge/write-failure narration, forge_gh_perm_safe's
# ladder callers) must treat "not confirmed" as "forge writes failing
# (possible GitHub incident) -- will retry", and must NEVER emit a "needs
# operator attention" / permission diagnosis without citing that the
# confirmation check ran and returned positive evidence. See sweep.md, "Forge
# write failure diagnosis (#6425)".

# is_forge_transient_error <text> -> 0 when the text is an outage-shaped
# signature: an HTTP 5xx status, GitHub's own "No server is currently
# available to service your request" 503 wording, a Bad
# Gateway/Service-Unavailable/Gateway-Timeout phrase, or a connection-level
# reset/refusal. These are NEVER a permission fault (a scope gap 403s
# instantly and consistently; it does not surface as a 5xx or a dropped
# connection), and retrying with a different credential cannot fix a 5xx
# either -- the only correct remedy is "wait and retry the same call".
#
# Anchored on "http 5xx" (not a bare "500"/"502"/... substring) so an
# unrelated numeral in the text -- an issue/PR number, a byte count -- cannot
# false-positive; `gh` itself always renders forge HTTP failures as
# "HTTP <code>: <message>".
is_forge_transient_error() {
  local text
  text=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$text" in
    *"http 500"*|*"http 502"*|*"http 503"*|*"http 504"*) return 0 ;;
    *"internal server error"*) return 0 ;;
    *"bad gateway"*) return 0 ;;
    *"service unavailable"*) return 0 ;;
    *"gateway timeout"*) return 0 ;;
    *"no server is currently available"*) return 0 ;;
    *"connection reset"*) return 0 ;;
    *"econnreset"*) return 0 ;;
    *"econnrefused"*) return 0 ;;
    *"connection refused"*) return 0 ;;
  esac
  return 1
}

# forge_write_permission_confirmed <write_error_text> -> 0 only with positive
# evidence of a genuine credential/permission fault; 1 (not confirmed)
# otherwise -- including when the read probe itself fails, which is evidence
# of an outage rather than a scoped permission gap. Callers must NOT assert a
# permission diagnosis unless this returns 0.
#
# The probe is `gh api /rate_limit`: cheap, side-effect-free, and answerable
# by any authenticated token regardless of its installation scopes (issue
# guidance's own suggested check, alongside the equivalent `gh api /user`).
forge_write_permission_confirmed() {
  local write_error="$1"

  # A forge-transient signature is never a permission fault, regardless of
  # what the read probe does -- short-circuit without spending the API call.
  if is_forge_transient_error "$write_error"; then
    return 1
  fi

  local read_rc=0
  gh api /rate_limit >/dev/null 2>&1 || read_rc=$?
  if [[ $read_rc -ne 0 ]]; then
    # The read ALSO failed on the same credential -- broader outage/token
    # problem, not a scoped write-only gap. Do not confirm.
    return 1
  fi

  # The read succeeded while the write failed on a non-transient error --
  # positive evidence of a genuine, scoped permission fault.
  return 0
}

# _forge_nwo_from_remote -> echoes owner/repo parsed from `git remote get-url
# origin`, with ZERO API calls. Deliberately NOT forge_get_repo_nwo(), whose
# GitHub branch tries `gh repo view` first -- that is GraphQL-backed, so it can
# fail for unrelated reasons in the middle of the very recovery path that
# exists because a `gh` call just failed (#4659's lesson, applied here).
_forge_nwo_from_remote() {
  local remote_url nwo
  remote_url=$(git remote get-url origin 2>/dev/null || echo "")
  [[ -n "$remote_url" ]] || return 1
  nwo=$(printf '%s' "$remote_url" | sed -E 's|\.git$||; s|/$||; s|.*[:/]([^/]+/[^/]+)$|\1|')
  [[ -n "$nwo" ]] || return 1
  printf '%s' "$nwo"
}

# _forge_gh_app_fresh_token <owner/repo> -> echoes a FRESHLY minted
# installation token (cache bypassed), or returns 1 when no GitHub App is
# configured on this host / the mint failed. Returning 1 is the common,
# expected case (most hosts have no App), and simply skips rung 2.
#
# LOOM_GITHUB_APP_SCRIPT overrides the minter's path (same test-seam
# convention as LOOM_GITHUB_APP_CACHE_DIR in github-app-token.sh itself).
_forge_gh_app_fresh_token() {
  local nwo="$1" script resp
  script="${LOOM_GITHUB_APP_SCRIPT:-$_LOOM_FORGE_HELPERS_LIB_DIR/github-app-token.sh}"
  [[ -n "$nwo" && -r "$script" ]] || return 1
  command -v jq >/dev/null 2>&1 || return 1
  resp=$(bash "$script" get-token --force "$nwo" 2>/dev/null) || return 1
  [[ "$(printf '%s' "$resp" | jq -r '.status // empty' 2>/dev/null)" == "ok" ]] || return 1
  local token
  token=$(printf '%s' "$resp" | jq -r '.token // empty' 2>/dev/null)
  [[ -n "$token" ]] || return 1
  printf '%s' "$token"
}

# _forge_cmd_attempt <mode> <token> <stdout_file> <stderr_file> <cmd> [args...]
# Runs one rung of the ladder for an ARBITRARY command. `env` (not a bash
# var-assignment prefix) does the credential swap so the override is
# unambiguously in the child's environment and nowhere else -- this shell's own
# env is never mutated.
#
# The command need not be `gh` itself (#6752): `loom-daemon forge …` shells out
# to `gh` internally and therefore resolves its credential from exactly the same
# `GH_TOKEN` / `GH_CONFIG_DIR` / `GITHUB_TOKEN` environment, so swapping those
# for the child escalates the native path identically to the shell path.
_forge_cmd_attempt() {
  local mode="$1" token="$2" out_file="$3" err_file="$4"
  shift 4
  local rc=0
  case "$mode" in
    ambient)
      "$@" >"$out_file" 2>"$err_file" || rc=$?
      ;;
    app-token)
      env GH_TOKEN="$token" "$@" >"$out_file" 2>"$err_file" || rc=$?
      ;;
    personal-token)
      env -u GITHUB_TOKEN -u GH_CONFIG_DIR GH_TOKEN="$token" "$@" >"$out_file" 2>"$err_file" || rc=$?
      ;;
    personal-ambient)
      env -u GH_TOKEN -u GITHUB_TOKEN -u GH_CONFIG_DIR "$@" >"$out_file" 2>"$err_file" || rc=$?
      ;;
  esac
  return "$rc"
}

# Run `<cmd> <args...>`, escalating the credential on -- and ONLY on -- an
# App-installation permission-scope 403 (#6074, generalized beyond `gh` by
# #6752). The command's own exit code is preserved verbatim on every rung, so a
# caller that assigns meaning to specific codes (`loom-daemon forge auto-merge`
# exits 3 = forge declined, 4 = head-SHA mismatch) still sees them: neither
# carries the integration-403 signature, so neither is ever retried.
#
# Usage: forge_cmd_perm_safe loom-daemon forge auto-merge 42 --method squash
# Stdout: the wrapped call's stdout.
# Returns the last attempt's exit code; stderr carries the last attempt's error
# text so an outer rate-limit/head-mismatch check still sees what it said.
forge_cmd_perm_safe() {
  local out_file err_file rc=0
  out_file=$(mktemp)
  err_file=$(mktemp)

  _forge_cmd_attempt ambient "" "$out_file" "$err_file" "$@" || rc=$?

  if [[ $rc -ne 0 ]] && is_app_permission_error "$(cat "$err_file" "$out_file" 2>/dev/null)"; then
    local nwo token
    nwo=$(_forge_nwo_from_remote || echo "")

    # Rung 2: force a fresh installation-token mint, bypassing the ~1h cache.
    if token=$(_forge_gh_app_fresh_token "$nwo"); then
      echo "forge: 403 'not accessible by integration' — retrying with a freshly minted installation token (#6074)" >&2
      rc=0
      _forge_cmd_attempt app-token "$token" "$out_file" "$err_file" "$@" || rc=$?
    fi

    # Rung 3: a personal token. Only worth trying when it is actually a
    # DIFFERENT credential from rung 1 -- with no App-delivered token in the
    # environment, `personal-ambient` would re-run rung 1 verbatim.
    if [[ $rc -ne 0 ]] && is_app_permission_error "$(cat "$err_file" "$out_file" 2>/dev/null)"; then
      if [[ -n "${LOOM_PERSONAL_GH_TOKEN:-}" ]]; then
        echo "forge: still 403 after a fresh mint — falling back to LOOM_PERSONAL_GH_TOKEN (#6074)" >&2
        rc=0
        _forge_cmd_attempt personal-token "$LOOM_PERSONAL_GH_TOKEN" "$out_file" "$err_file" "$@" || rc=$?
      elif [[ -n "${GH_CONFIG_DIR:-}${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]]; then
        echo "forge: still 403 after a fresh mint — falling back to the ambient personal gh credential (#6074)" >&2
        rc=0
        _forge_cmd_attempt personal-ambient "" "$out_file" "$err_file" "$@" || rc=$?
      fi
    fi
  fi

  local out err
  out=$(cat "$out_file" 2>/dev/null || true)
  err=$(cat "$err_file" 2>/dev/null || true)
  rm -f "$out_file" "$err_file"

  if [[ -n "$out" ]]; then
    printf '%s\n' "$out"
  fi
  if [[ $rc -ne 0 && -n "$err" ]]; then
    printf '%s\n' "$err" >&2
  fi
  return "$rc"
}

# Run `gh <args...>` through the same ladder (#6074). Every write call site that
# a Builder depends on (PR create, issue comment, label edit) routes through
# this; it is a thin `gh`-prefixed spelling of forge_cmd_perm_safe, so the two
# stay behaviourally identical by construction.
#
# Usage: forge_gh_perm_safe pr create --title T --body B ...
# Stdout: the wrapped call's stdout (e.g. the new PR's URL).
# Returns the last attempt's exit code; stderr carries the last attempt's
# error text so an outer rate-limit check still sees what `gh` actually said.
forge_gh_perm_safe() {
  forge_cmd_perm_safe gh "$@"
}

# Post a comment on an issue OR a pull request via `gh issue comment`, falling
# back to the REST comments endpoint on a GraphQL rate-limit rejection. The
# REST endpoint (`repos/{nwo}/issues/{n}/comments`) is shared by issues and
# PRs on GitHub (a PR IS an issue for labels/comments/state), so one function
# safely serves both `gh issue comment` and `gh pr comment` call sites.
# Usage: forge_gh_comment_rl_safe NWO NUMBER BODY
# Returns 0 on success (either path), 1 on failure (message on stderr).
forge_gh_comment_rl_safe() {
  local nwo="$1" number="$2" body="$3"
  local out
  if out=$(forge_gh_perm_safe issue comment "$number" --repo "$nwo" --body "$body" 2>&1); then
    return 0
  fi
  if is_rate_limit_error "$out"; then
    if gh api "repos/$nwo/issues/$number/comments" -f "body=$body" >/dev/null 2>&1; then
      return 0
    fi
    echo "gh issue comment rate-limited on #$number, and the REST fallback also failed: $out" >&2
    return 1
  fi
  echo "$out" >&2
  return 1
}

# Reopen a closed issue via `gh issue reopen`, falling back to a REST PATCH
# (state=open) on a GraphQL rate-limit rejection.
# Usage: forge_gh_reopen_issue_rl_safe NWO ISSUE_NUMBER
forge_gh_reopen_issue_rl_safe() {
  local nwo="$1" issue_num="$2"
  local out
  if out=$(gh issue reopen "$issue_num" --repo "$nwo" 2>&1); then
    return 0
  fi
  if is_rate_limit_error "$out"; then
    if gh api "repos/$nwo/issues/$issue_num" -X PATCH -f state=open >/dev/null 2>&1; then
      return 0
    fi
    echo "gh issue reopen rate-limited on #$issue_num, and the REST fallback also failed: $out" >&2
    return 1
  fi
  echo "$out" >&2
  return 1
}

# Swap one label for another on an issue via `gh issue edit --remove-label
# --add-label`, falling back to two REST calls (DELETE the old label, POST
# the new one) on a GraphQL rate-limit rejection. The label name is
# percent-encoded for the DELETE path segment (GitHub labels commonly contain
# `:`, e.g. `loom:building`, which must be encoded as `%3A`).
# Usage: forge_gh_swap_label_rl_safe NWO ISSUE_NUMBER REMOVE_LABEL ADD_LABEL
forge_gh_swap_label_rl_safe() {
  local nwo="$1" issue_num="$2" remove_label="$3" add_label="$4"
  local out
  if out=$(forge_gh_perm_safe issue edit "$issue_num" --repo "$nwo" \
      --remove-label "$remove_label" --add-label "$add_label" 2>&1); then
    return 0
  fi
  if is_rate_limit_error "$out"; then
    local encoded_remove ok=true
    encoded_remove="${remove_label//:/%3A}"
    gh api "repos/$nwo/issues/$issue_num/labels/$encoded_remove" -X DELETE >/dev/null 2>&1 || ok=false
    gh api "repos/$nwo/issues/$issue_num/labels" -f "labels[]=$add_label" >/dev/null 2>&1 || ok=false
    if [[ "$ok" == "true" ]]; then
      return 0
    fi
    echo "gh issue edit (label swap) rate-limited on #$issue_num, and the REST fallback also failed: $out" >&2
    return 1
  fi
  echo "$out" >&2
  return 1
}

# Remove a single label from an issue via `gh issue edit --remove-label`,
# falling back to a REST DELETE on a GraphQL rate-limit rejection. Mirrors
# forge_gh_swap_label_rl_safe's REST-fallback shape (#4856) minus the
# add-label half — used where the target issue is closed and should NOT be
# returned to any queue (#6199: stripping an orphaned `loom:building` claim
# from an issue a merge just closed, as opposed to the swap-to-`loom:issue`
# case for a still-open partial-increment issue).
# Idempotent: `gh issue edit --remove-label` on a label the issue does not
# carry, and the REST DELETE fallback on the same, both succeed as no-ops.
# Usage: forge_gh_remove_label_rl_safe NWO ISSUE_NUMBER LABEL
forge_gh_remove_label_rl_safe() {
  local nwo="$1" issue_num="$2" label="$3"
  local out
  if out=$(forge_gh_perm_safe issue edit "$issue_num" --repo "$nwo" \
      --remove-label "$label" 2>&1); then
    return 0
  fi
  if is_rate_limit_error "$out"; then
    local encoded_label
    encoded_label="${label//:/%3A}"
    if gh api "repos/$nwo/issues/$issue_num/labels/$encoded_label" -X DELETE >/dev/null 2>&1; then
      return 0
    fi
    echo "gh issue edit (label remove) rate-limited on #$issue_num, and the REST fallback also failed: $out" >&2
    return 1
  fi
  echo "$out" >&2
  return 1
}

# File a NEW issue via `gh issue create`, falling back to a single REST POST
# to `repos/{nwo}/issues` on a GraphQL rate-limit rejection (#5047).
#
# `gh issue create` is GraphQL-backed, so every issue-filing role (Architect,
# Auditor, Curator decomposition, Builder decomposition, Doctor, Hermit,
# Judge) died outright once the GraphQL pool exhausted -- even though the
# independent REST pool routinely sits ~99% unused at that moment (observed
# 2026-08-03: core 19/5000 consumed vs graphql 1378/5000). Comments, labels
# and state already had REST fallbacks (#4856, above); creation did not.
#
# **Labels are applied atomically with creation on BOTH paths** -- `--label`
# on the primary path, a `labels` array in the same POST body on the REST
# path. Never degrade this to create-then-label: that doubles the request
# count under exactly the conditions where requests are scarce, and can
# half-fail, leaving an unlabelled issue that no role's queue query finds.
#
# NWO may be the empty string, meaning "the repo of the current working
# directory". That is the preferred form: the REST path then uses `gh api`'s
# literal `{owner}/{repo}` placeholder, which gh expands from the git remote
# with ZERO API calls -- unlike `gh repo view --json nameWithOwner`, which is
# itself GraphQL-backed and so fails first under the very exhaustion this
# fallback exists for (#4659).
#
# This is the single-sourced recipe referenced by the role prompts that file
# issues (architect.md, auditor.md, builder-complexity.md, builder-pr.md,
# curator.md, doctor.md, hermit.md, hermit-patterns.md, judge.md) -- via the
# executable wrapper `create-issue.sh`. Update this function, not each
# prompt, if the recipe needs to change.
#
# Usage: forge_gh_create_issue_rl_safe NWO TITLE BODY [LABEL...]
# Stdout: the new issue's URL (both paths).
# Returns 0 on success (either path), 1 on failure (message on stderr).
forge_gh_create_issue_rl_safe() {
  local nwo="$1" title="$2" body="$3"
  shift 3
  local labels=("$@")

  local -a create_args=(--title "$title" --body "$body")
  if [[ -n "$nwo" ]]; then
    create_args+=(--repo "$nwo")
  fi
  local label
  for label in "${labels[@]+"${labels[@]}"}"; do
    create_args+=(--label "$label")
  done

  # Capture stdout (the issue URL) separately from stderr (the error text the
  # rate-limit signature table is matched against), so a successful create
  # never returns gh's progress chatter as the URL.
  local err_file out err rc=0
  err_file=$(mktemp)
  out=$(forge_gh_perm_safe issue create "${create_args[@]}" 2>"$err_file") || rc=$?
  err=$(cat "$err_file" 2>/dev/null || true)
  rm -f "$err_file"

  if [[ $rc -eq 0 ]]; then
    printf '%s\n' "$out"
    return 0
  fi

  if is_rate_limit_error "$err"; then
    # One POST carries title + body + labels together. `--input -` takes the
    # JSON body on stdin, which also sidesteps the guard false positive where
    # a heredoc body containing `>=` is classified as a Bash redirect.
    local payload labels_json
    labels_json=$(jq -nc '$ARGS.positional' --args "${labels[@]+"${labels[@]}"}")
    payload=$(jq -n --arg t "$title" --arg b "$body" --argjson l "$labels_json" \
      '{title: $t, body: $b, labels: $l}')
    local rest_path
    if [[ -n "$nwo" ]]; then
      rest_path="repos/$nwo/issues"
    else
      # Literal placeholder — gh expands it from the git remote, no API call.
      rest_path='repos/{owner}/{repo}/issues'
    fi
    if out=$(printf '%s' "$payload" \
        | gh api --method POST "$rest_path" --input - --jq '.html_url' 2>/dev/null); then
      printf '%s\n' "$out"
      return 0
    fi
    echo "gh issue create rate-limited, and the REST fallback also failed: $err" >&2
    return 1
  fi

  echo "$err" >&2
  return 1
}

# Get PR comments.
# Usage: forge_get_pr_comments NWO PR_NUMBER
# GitHub: gh pr view --comments
# Gitea: GET /repos/{owner}/{repo}/issues/{n}/comments (PRs use issue comment API)
forge_get_pr_comments() {
  local nwo="$1"
  local pr_number="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/issues/$pr_number/comments" 2>/dev/null | \
      jq -r '.[].body // empty'
  else
    gh pr view "$pr_number" --comments --json comments --jq '.comments[].body' 2>/dev/null || echo ""
  fi
}

# Get PR reviews.
# Usage: forge_get_pr_reviews NWO PR_NUMBER
# Both forges: GET /repos/{nwo}/pulls/{n}/reviews
forge_get_pr_reviews() {
  local nwo="$1"
  local pr_number="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/reviews" 2>/dev/null | \
      jq -r '.[].body // empty'
  else
    gh api "repos/$nwo/pulls/$pr_number/reviews" --jq '.[].body // empty' 2>/dev/null || echo ""
  fi
}

# Get branch-protection required status check contexts for a branch.
#
# Usage: forge_get_required_status_check_contexts NWO BRANCH [GH_CMD]
# Outputs: One context name per line on stdout. Empty output means the branch
#   has no required status checks configured (every failing check is
#   informational from a branch-protection standpoint).
# Exit code: 0 on success (including empty result), nonzero on lookup failure.
#
# This is used by merge-pr.sh's UNSTABLE-fallback (sibling of #3371's CLEAN
# fallback) to decide whether an auto-merge "Pull request is in unstable status"
# error can be safely bypassed. If every failing check on the PR is outside this
# set, the immediate-merge path is taken; otherwise the existing UNSTABLE
# refusal is preserved. See issue #3486.
#
# GitHub: GraphQL query against
#   `repository(owner, name).ref(qualifiedName: "refs/heads/<branch>")
#    .branchProtectionRule.requiredStatusCheckContexts`.
#   Branches with no protection rule, or whose rule has no required contexts,
#   yield empty output (exit 0). This is the desired behavior — "no required
#   checks" means every failing check is informational, which is the case the
#   UNSTABLE-fallback wants to unblock.
#
# Gitea: GET /api/v1/repos/{owner}/{repo}/branch_protections/{name}. Gitea's
#   branch-protection rule carries both `enable_status_check` (boolean toggle)
#   and `status_check_contexts` (array of context patterns). The contexts are
#   only enforced when `enable_status_check` is true — when it's false, the
#   contexts list is informational and we emit empty output (every failing
#   check is then treated as informational, same as the GitHub "no rule" path).
#
#   Distinguishing 404 (no protection rule, emit empty → fallback fires) from
#   5xx / network error (emit empty + nonzero exit → caller fails closed) is
#   important: the issue explicitly requires fail-closed semantics on lookup
#   failure. `gitea_api` collapses both 4xx and 5xx into exit 1, so this
#   function uses a direct curl invocation that captures the HTTP code and
#   branches on it explicitly.
#
#   Unknown forge types fall through to a fail-closed nonzero exit, leaving
#   the caller's existing UNSTABLE refusal intact.
forge_get_required_status_check_contexts() {
  local nwo="$1"
  local branch="$2"
  local gh_cmd="${3:-gh}"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"

    # Sanity-check Gitea config before issuing the request. Missing URL or
    # token is treated as fail-closed (nonzero exit, empty stdout) so the
    # caller preserves the UNSTABLE refusal.
    if [[ -z "$_GITEA_BASE_URL" ]] || [[ -z "$_GITEA_TOKEN" ]]; then
      return 1
    fi
    if ! _gitea_validate_basic_auth; then
      return 1
    fi

    local url="${_GITEA_BASE_URL}/api/v1/repos/${FORGE_OWNER}/${FORGE_REPO}/branch_protections/${branch}"
    local response
    if [[ -n "$_GITEA_USERNAME" ]]; then
      response=$(curl -s -w "\n%{http_code}" \
        -X GET \
        -u "${_GITEA_USERNAME}:${_GITEA_TOKEN}" \
        -H "Accept: application/json" \
        "$url" 2>/dev/null) || return 1
    else
      response=$(curl -s -w "\n%{http_code}" \
        -X GET \
        -H "Authorization: token $_GITEA_TOKEN" \
        -H "Accept: application/json" \
        "$url" 2>/dev/null) || return 1
    fi

    local http_code body
    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | sed '$d')

    # 404: no branch protection rule exists. Mirror GitHub's "no rule means no
    # required" behavior — emit empty, exit 0 so the fallback fires.
    if [[ "$http_code" == "404" ]]; then
      return 0
    fi

    # 5xx / network failure / auth error / anything else non-2xx: fail closed.
    # Empty stdout, nonzero exit; the caller will preserve the UNSTABLE refusal.
    if [[ "$http_code" -lt 200 ]] || [[ "$http_code" -ge 300 ]]; then
      return 1
    fi

    # 2xx: parse `enable_status_check` and `status_check_contexts`. When the
    # toggle is off, contexts are not enforced — emit empty. Otherwise emit
    # each context on its own line. A missing/null array also yields empty.
    echo "$body" | jq -r '
      if (.enable_status_check // false) then
        (.status_check_contexts // []) | .[]
      else
        empty
      end
    ' 2>/dev/null || return 1
    return 0
  fi

  if [[ "$FORGE_TYPE" != "github" ]]; then
    # Unknown forge — fail closed so the caller preserves the UNSTABLE refusal.
    return 1
  fi

  forge_split_nwo "$nwo"

  local query='query($owner: String!, $name: String!, $ref: String!) {
    repository(owner: $owner, name: $name) {
      ref(qualifiedName: $ref) {
        branchProtectionRule {
          requiredStatusCheckContexts
        }
      }
    }
  }'

  # `gh api graphql --jq` with a missing path field yields `null`; pipe through
  # jq to flatten the optional contexts array into a newline-separated list.
  # Each step is allowed to yield empty output without failing the helper —
  # absent protection rule or empty contexts list both mean "no required checks".
  "$gh_cmd" api graphql \
    -f "query=$query" \
    -F "owner=$FORGE_OWNER" \
    -F "name=$FORGE_REPO" \
    -F "ref=refs/heads/$branch" \
    --jq '.data.repository.ref.branchProtectionRule.requiredStatusCheckContexts // [] | .[]' \
    2>/dev/null || return 0
}

# Get repo NWO (name with owner).
# Usage: forge_get_repo_nwo [GH_CMD]
# Returns "owner/repo" on stdout.
forge_get_repo_nwo() {
  local gh_cmd="${1:-gh}"
  local nwo

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    # Parse from git remote URL
    local remote_url
    remote_url=$(git remote get-url origin 2>/dev/null || echo "")
    if [[ -n "$remote_url" ]]; then
      nwo=$(echo "$remote_url" | sed -E 's|\.git$||; s|.*[:/]([^/]+/[^/]+)$|\1|')
      echo "$nwo"
      return 0
    fi
    return 1
  else
    # GitHub: try gh repo view, fallback to git remote
    nwo=$("$gh_cmd" repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null) && [[ -n "$nwo" ]] && echo "$nwo" && return 0
    nwo=$(git remote get-url origin 2>/dev/null | sed -E 's|\.git$||; s|.*[:/]([^/]+/[^/]+)$|\1|') && [[ -n "$nwo" ]] && echo "$nwo" && return 0
    return 1
  fi
}
