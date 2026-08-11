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
# Usage: forge_merge_pr NWO PR_NUMBER
# GitHub: PUT /repos/{nwo}/pulls/{n}/merge with merge_method=squash
# Gitea: POST /repos/{owner}/{repo}/pulls/{n}/merge with Do=squash
forge_merge_pr() {
  local nwo="$1"
  local pr_number="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api POST "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/merge" \
      -d '{"Do":"squash","delete_branch_after_merge":false}'
  else
    gh api "repos/$nwo/pulls/$pr_number/merge" \
      -X PUT \
      -f merge_method=squash 2>&1
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
# Usage: forge_auto_merge NWO PR_NUMBER
# GitHub: GraphQL enablePullRequestAutoMerge mutation (pure API, no
#         working-tree dependency — `gh pr merge --auto` does a local
#         checkout that collides with worktrees owning the head branch).
# Gitea: POST /repos/{owner}/{repo}/pulls/{n}/merge with merge_when_checks_succeed
forge_auto_merge() {
  local nwo="$1"
  local pr_number="$2"

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    gitea_api POST "repos/$FORGE_OWNER/$FORGE_REPO/pulls/$pr_number/merge" \
      -d '{"Do":"squash","merge_when_checks_succeed":true,"delete_branch_after_merge":true}'
  else
    # Resolve PR node_id (required by GraphQL mutation).
    local node_id
    node_id=$(gh api "repos/$nwo/pulls/$pr_number" --jq '.node_id' 2>/dev/null) || return 1
    [[ -z "$node_id" ]] && return 1

    local mutation='mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) { enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod}) { pullRequest { number autoMergeRequest { enabledAt } } } }'

    gh api graphql \
      -f "query=$mutation" \
      -F "pullRequestId=$node_id" \
      -F "mergeMethod=SQUASH" 2>/dev/null
  fi
}

# --- CI Status Helpers ---

# Get CI check runs for a commit.
# Usage: forge_get_check_runs NWO COMMIT_SHA
# GitHub: GET /repos/{nwo}/commits/{sha}/check-runs
# Gitea: GET /repos/{owner}/{repo}/commits/{sha}/statuses (mapped to check-run shape)
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
      }' 2>/dev/null
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
  if out=$(gh issue comment "$number" --repo "$nwo" --body "$body" 2>&1); then
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
  if out=$(gh issue edit "$issue_num" --repo "$nwo" \
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
  out=$(gh issue create "${create_args[@]}" 2>"$err_file") || rc=$?
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
