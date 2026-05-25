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
#   2. .loom/config.json forge.type (if not "auto")
#   3. Auto-detect from git remote origin URL
#   4. Default to "github"

set -euo pipefail

# --- Forge Detection ---

# Global forge state (set by forge_detect)
FORGE_TYPE=""
_GITEA_BASE_URL=""
_GITEA_TOKEN=""
_GITEA_USERNAME=""

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

  # 2. Config file
  local config_file
  if [[ -n "${REPO_ROOT:-}" ]]; then
    config_file="$REPO_ROOT/.loom/config.json"
  elif [[ -n "${WORKSPACE_ROOT:-}" ]]; then
    config_file="$WORKSPACE_ROOT/.loom/config.json"
  else
    config_file=".loom/config.json"
  fi

  if [[ -f "$config_file" ]] && command -v jq &>/dev/null; then
    local config_type
    config_type=$(jq -r '.forge.type // "auto"' "$config_file" 2>/dev/null || echo "auto")
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
    if [[ -f "$config_file" ]] && command -v jq &>/dev/null; then
      local gitea_url
      gitea_url=$(jq -r '.forge.gitea.url // ""' "$config_file" 2>/dev/null || echo "")
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

  local config_file
  if [[ -n "${REPO_ROOT:-}" ]]; then
    config_file="$REPO_ROOT/.loom/config.json"
  elif [[ -n "${WORKSPACE_ROOT:-}" ]]; then
    config_file="$WORKSPACE_ROOT/.loom/config.json"
  else
    config_file=".loom/config.json"
  fi

  if [[ -f "$config_file" ]] && command -v jq &>/dev/null; then
    if [[ -z "$_GITEA_TOKEN" ]]; then
      _GITEA_TOKEN=$(jq -r '.forge.gitea.token // ""' "$config_file" 2>/dev/null || echo "")
    fi
    if [[ -z "$_GITEA_BASE_URL" ]]; then
      _GITEA_BASE_URL=$(jq -r '.forge.gitea.url // ""' "$config_file" 2>/dev/null || echo "")
    fi
    if [[ -z "$_GITEA_USERNAME" ]]; then
      _GITEA_USERNAME=$(jq -r '.forge.gitea.username // ""' "$config_file" 2>/dev/null || echo "")
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
  else
    "$gh_cmd" --no-cache api "repos/$nwo/pulls/$pr_number" 2>/dev/null
  fi
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
