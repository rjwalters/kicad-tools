#!/usr/bin/env bash
# Loom PR Merge - Worktree-safe merge using GitHub API
# Usage: ./.loom/scripts/merge-pr.sh <pr-number> [options]
#
# Merges a PR via the GitHub API (not `gh pr merge`) to avoid
# "already used by worktree" errors when merging from inside a worktree.
#
# Options:
#   --cleanup-worktree   Remove local worktree after successful merge
#   --dry-run            Show what would happen without merging
#   --auto               Enable auto-merge instead of immediate merge
#
# Exit codes:
#   0 = merged (or auto-merge enabled)
#   1 = failed

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

error() { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }
info() { echo -e "${BLUE}$*${NC}"; }
success() { echo -e "${GREEN}$*${NC}"; }
warning() { echo -e "${YELLOW}$*${NC}"; }

# Find the main repository root (works from worktrees too)
# When run from a worktree, git rev-parse --show-toplevel returns the worktree path,
# not the main repository. This function navigates via the gitdir to find the actual root.
find_main_repo_root() {
  local dir
  dir="$(git rev-parse --show-toplevel 2>/dev/null)" || return 1

  # Check if this is a worktree (has .git file, not directory)
  if [[ -f "$dir/.git" ]]; then
    local gitdir
    gitdir=$(cat "$dir/.git" | sed 's/^gitdir: //')
    # gitdir is like /path/to/repo/.git/worktrees/issue-123
    # main repo is 3 levels up from there
    local main_repo
    main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
    if [[ -d "$main_repo/.loom" ]]; then
      echo "$main_repo"
      return 0
    fi
  fi

  # Not a worktree or fallback - return the git root
  echo "$dir"
}

REPO_ROOT="$(find_main_repo_root)" || \
  error "Not in a git repository"

# Use gh-cached for read-only queries to reduce API calls (see issue #1609)
GH_CACHED="$REPO_ROOT/.loom/scripts/gh-cached"
if [[ -x "$GH_CACHED" ]]; then
    GH="$GH_CACHED"
else
    GH="gh"
fi

# Auto-detect owner/repo from git remote
REPO_NWO="$($GH repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null)" || \
  error "Could not determine repository. Is 'gh' authenticated?"

# Parse arguments
PR_NUMBER=""
CLEANUP_WORKTREE=false
DRY_RUN=false
AUTO_MERGE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cleanup-worktree) CLEANUP_WORKTREE=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --auto) AUTO_MERGE=true; shift ;;
    -*)  error "Unknown option: $1" ;;
    *)
      if [[ -z "$PR_NUMBER" ]]; then
        PR_NUMBER="$1"
      else
        error "Unexpected argument: $1"
      fi
      shift
      ;;
  esac
done

[[ -z "$PR_NUMBER" ]] && error "Usage: merge-pr.sh <pr-number> [--cleanup-worktree] [--dry-run] [--auto]"
[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || error "PR number must be numeric: $PR_NUMBER"

# Fetch PR state
PR_JSON=$($GH api "repos/$REPO_NWO/pulls/$PR_NUMBER" 2>/dev/null) || \
  error "Could not fetch PR #$PR_NUMBER"

PR_STATE=$(echo "$PR_JSON" | jq -r '.state')
PR_MERGED=$(echo "$PR_JSON" | jq -r '.merged')
PR_BRANCH=$(echo "$PR_JSON" | jq -r '.head.ref')
PR_TITLE=$(echo "$PR_JSON" | jq -r '.title')
PR_MERGEABLE=$(echo "$PR_JSON" | jq -r '.mergeable')

# Check if already merged
if [[ "$PR_MERGED" == "true" ]]; then
  warning "PR #$PR_NUMBER is already merged"
  exit 0
fi

# Check if closed (not merged)
if [[ "$PR_STATE" == "closed" ]]; then
  error "PR #$PR_NUMBER is closed (not merged)"
fi

info "Merging PR #$PR_NUMBER: $PR_TITLE"
info "Branch: $PR_BRANCH"

# Handle auto-merge mode
if [[ "$AUTO_MERGE" == "true" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] Would enable auto-merge for PR #$PR_NUMBER"
    exit 0
  fi
  # Use gh CLI for auto-merge (API for this requires GraphQL)
  if gh pr merge "$PR_NUMBER" --auto --squash --delete-branch 2>/dev/null; then
    success "Auto-merge enabled for PR #$PR_NUMBER"
    exit 0
  else
    error "Failed to enable auto-merge for PR #$PR_NUMBER"
  fi
fi

# Check mergeability
if [[ "$PR_MERGEABLE" == "false" ]]; then
  error "PR #$PR_NUMBER has merge conflicts — resolve before merging"
fi

if [[ "$DRY_RUN" == "true" ]]; then
  info "[dry-run] Would merge PR #$PR_NUMBER (squash) and delete branch '$PR_BRANCH'"
  [[ "$CLEANUP_WORKTREE" == "true" ]] && info "[dry-run] Would clean up local worktree"
  exit 0
fi

# Merge via API (squash) with retry for stale branch
MAX_MERGE_RETRIES=3
MERGE_RETRY_DELAY=5

for MERGE_ATTEMPT in $(seq 1 $MAX_MERGE_RETRIES); do
  MERGE_RESPONSE=$(gh api "repos/$REPO_NWO/pulls/$PR_NUMBER/merge" \
    -X PUT \
    -f merge_method=squash \
    2>&1) && break  # Success, exit loop

  # Check if it merged despite error (race condition)
  RECHECK=$($GH --no-cache api "repos/$REPO_NWO/pulls/$PR_NUMBER" --jq '.merged' 2>/dev/null || echo "false")
  if [[ "$RECHECK" == "true" ]]; then
    warning "Merge reported error but PR is merged (race condition)"
    break
  fi

  # Check for stale branch error (base branch was modified)
  if echo "$MERGE_RESPONSE" | grep -q "Base branch was modified"; then
    if [[ $MERGE_ATTEMPT -lt $MAX_MERGE_RETRIES ]]; then
      info "Branch is behind base branch, updating... (attempt $MERGE_ATTEMPT/$MAX_MERGE_RETRIES)"

      # Update branch via GitHub API
      UPDATE_RESPONSE=$(gh api "repos/$REPO_NWO/pulls/$PR_NUMBER/update-branch" \
        -X PUT \
        2>&1) || {
        warning "Failed to update branch: $UPDATE_RESPONSE"
        # Continue to retry merge anyway - update may have partially succeeded
      }

      # Wait for branch to sync
      info "Waiting ${MERGE_RETRY_DELAY}s for branch to sync..."
      sleep "$MERGE_RETRY_DELAY"

      # Increase delay for next attempt (exponential backoff)
      MERGE_RETRY_DELAY=$((MERGE_RETRY_DELAY * 2))
      continue
    else
      error "Failed to merge PR #$PR_NUMBER after $MAX_MERGE_RETRIES attempts: Branch remains behind base branch"
    fi
  fi

  # Other merge errors - fail immediately
  error "Failed to merge PR #$PR_NUMBER: $MERGE_RESPONSE"
done

# Verify merge
VERIFY_MERGED=$($GH --no-cache api "repos/$REPO_NWO/pulls/$PR_NUMBER" --jq '.merged' 2>/dev/null || echo "false")
if [[ "$VERIFY_MERGED" != "true" ]]; then
  error "Merge API call returned but PR #$PR_NUMBER is not merged"
fi

success "PR #$PR_NUMBER merged successfully"

# Clean up workflow labels on linked issue
info "Cleaning up workflow labels on linked issue..."
PR_BODY=$(echo "$PR_JSON" | jq -r '.body // ""')
LINKED_ISSUE=$(echo "$PR_BODY" | grep -oE '(Closes|closes|Fixes|fixes|Resolves|resolves) #[0-9]+' | grep -oE '[0-9]+' | head -1)

if [[ -n "$LINKED_ISSUE" ]]; then
  info "Found linked issue: #$LINKED_ISSUE"
  # Remove workflow labels that shouldn't persist on closed issues
  for label in loom:building loom:issue loom:curated loom:curating loom:treating loom:blocked; do
    gh issue edit "$LINKED_ISSUE" --remove-label "$label" 2>/dev/null && \
      info "  Removed label: $label" || true
  done
  success "Workflow labels cleaned up for issue #$LINKED_ISSUE"
else
  info "No linked issue found in PR body (no 'Closes #N' pattern)"
fi

# Delete remote branch
info "Deleting remote branch: $PR_BRANCH"
gh api "repos/$REPO_NWO/git/refs/heads/$PR_BRANCH" -X DELETE 2>/dev/null && \
  success "Branch '$PR_BRANCH' deleted" || \
  warning "Could not delete branch '$PR_BRANCH' (may already be deleted)"

# Cleanup worktree if requested
if [[ "$CLEANUP_WORKTREE" == "true" ]]; then
  # Extract issue number from branch name (feature/issue-N pattern)
  ISSUE_NUM=$(echo "$PR_BRANCH" | grep -oE '[0-9]+$' || true)
  if [[ -n "$ISSUE_NUM" ]]; then
    WORKTREE_PATH="$REPO_ROOT/.loom/worktrees/issue-$ISSUE_NUM"
    if [[ -d "$WORKTREE_PATH" ]]; then
      # Check if we're currently inside the worktree being removed
      CURRENT_DIR="$(pwd -P 2>/dev/null || pwd)"
      WORKTREE_REAL="$(cd "$WORKTREE_PATH" 2>/dev/null && pwd -P || echo "$WORKTREE_PATH")"
      IN_WORKTREE=false
      if [[ "$CURRENT_DIR" == "$WORKTREE_REAL"* ]]; then
        IN_WORKTREE=true
        cd "$REPO_ROOT"
      fi
      info "Removing worktree: $WORKTREE_PATH"
      if git -C "$REPO_ROOT" worktree remove "$WORKTREE_PATH" --force 2>/dev/null; then
        success "Worktree removed"
        if [[ "$IN_WORKTREE" == "true" ]]; then
          echo ""
          warning "Your shell's working directory was inside the removed worktree."
          warning "Run this command to fix:"
          echo "  cd $REPO_ROOT"
        fi
      else
        warning "Could not remove worktree at $WORKTREE_PATH"
      fi
    else
      info "No worktree found at $WORKTREE_PATH"
    fi
  else
    warning "Could not determine issue number from branch '$PR_BRANCH' for worktree cleanup"
  fi
fi

success "Done"
