#!/usr/bin/env bash
# Loom PR Merge - Worktree-safe merge using forge API (GitHub or Gitea)
# Usage: ./.loom/scripts/merge-pr.sh <pr-number> [options]
#
# Merges a PR via the forge API (not `gh pr merge`) to avoid
# "already used by worktree" errors when merging from inside a worktree.
#
# Supports both GitHub and Gitea forges. Forge detection is automatic
# (see forge-helpers.sh for details).
#
# Options:
#   --no-cleanup-worktree  Skip local worktree cleanup after merge
#   --cleanup-worktree     (no-op, worktree cleanup is now the default)
#   --worktree-path <dir>  Explicit worktree path to clean up (bypasses
#                          .loom-managed sentinel guard — caller asserts
#                          responsibility). Also deletes the matching local
#                          branch via `git branch -d` (refuses on unmerged
#                          commits — Git's own safety check).
#   --dry-run              Show what would happen without merging
#   --auto                 Enable auto-merge instead of immediate merge
#
# By default, the local worktree is cleaned up after a successful merge.
# Pass --no-cleanup-worktree to skip this (e.g., when other terminals may
# have their CWD inside the worktree).
#
# Cleanup is restricted to Loom-managed worktrees (those containing the
# .loom-managed sentinel written by worktree.sh). Worktrees lacking the
# sentinel are treated as user-owned and never removed. Set
# LOOM_PRESERVE_WORKTREE=1 to disable cleanup unconditionally for a session.
#
# Override: pass --worktree-path <dir> to opt into removing a non-Loom
# worktree (the sentinel guard is bypassed only when this flag is supplied).
# Discovery: if neither the default issue-N nor pr-N worktree exists, the
# script walks `git worktree list --porcelain` looking for a worktree whose
# branch matches the merged PR's head branch. It emits a hint (not an
# auto-remove) so the operator can re-run with --worktree-path.
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

# Function to show help
show_help() {
    cat << EOF
Loom PR Merge - Worktree-safe merge using forge API (GitHub or Gitea)

Usage: ./.loom/scripts/merge-pr.sh <pr-number> [options]

Merges a PR via the forge API (not 'gh pr merge') to avoid
"already used by worktree" errors when merging from inside a worktree.

Supports both GitHub and Gitea forges. Forge detection is automatic
(see forge-helpers.sh for details).

Options:
  --no-cleanup-worktree  Skip local worktree cleanup after merge
  --cleanup-worktree     (no-op, worktree cleanup is now the default)
  --worktree-path <dir>  Explicit worktree path to clean up. Bypasses the
                         .loom-managed sentinel guard (caller asserts
                         responsibility — this is the documented opt-in
                         for removing non-Loom worktrees). Also deletes
                         the matching local branch via 'git branch -d'
                         (Git refuses on unmerged commits).
  --dry-run              Show what would happen without merging
  --auto                 Enable auto-merge instead of immediate merge
  -h, --help             Show this help and exit

By default, the local worktree is cleaned up after a successful merge.
Pass --no-cleanup-worktree to skip this (e.g., when other terminals may
have their CWD inside the worktree).

Cleanup is restricted to Loom-managed worktrees (those under
.loom/worktrees/issue-N that contain a .loom-managed sentinel file written
by worktree.sh). User-provisioned worktrees at other paths are never
removed by the default code path. Set LOOM_PRESERVE_WORKTREE=1 to disable
cleanup unconditionally for a session.

When --worktree-path <dir> is passed explicitly, the operator is taking
responsibility for the cleanup decision: the sentinel guard is bypassed
for that one path. The path is validated against 'git worktree list'
and rejected if it is not a worktree of this repository.

Discovery fallback: if neither .loom/worktrees/issue-N/ nor
.loom/worktrees/pr-<PR_NUMBER>/ exists, the script walks
'git worktree list --porcelain' looking for a worktree whose branch
matches the merged PR head branch. It NEVER auto-removes a discovered
user-owned worktree; it only logs the path and suggests re-running with
--worktree-path <found-path>.

Precedence (highest wins):
  1. LOOM_PRESERVE_WORKTREE=1     (always skip cleanup)
  2. --no-cleanup-worktree        (always skip cleanup; warns if combined
                                  with --worktree-path)
  3. --worktree-path <dir>        (explicit path; bypasses sentinel)
  4. default: .loom/worktrees/issue-N or pr-N + sentinel guard

Exit codes:
  0 = merged (or auto-merge enabled, or --help)
  1 = failed

Examples:
  ./.loom/scripts/merge-pr.sh 123
    Merges PR #123 (squash), deletes remote branch, cleans up worktree

  ./.loom/scripts/merge-pr.sh 123 --dry-run
    Shows what would happen without merging

  ./.loom/scripts/merge-pr.sh 123 --auto
    Enables auto-merge instead of merging immediately

  ./.loom/scripts/merge-pr.sh 123 --no-cleanup-worktree
    Merges PR but leaves the local worktree in place

  ./.loom/scripts/merge-pr.sh 123 --worktree-path ../adhoc-wt
    Merges PR #123 and removes the worktree at ../adhoc-wt plus its
    matching local branch (bypasses the .loom-managed sentinel guard).
EOF
}

# Early help check — runs before any git/forge initialization so --help works
# in any directory and without forge authentication.
if [[ $# -gt 0 ]] && { [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; }; then
    show_help
    exit 0
fi

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

# Source forge helpers for multi-forge support
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/forge-helpers.sh"
# Shared worktree-root resolver (#3530) — cleanup must discover worktrees at an
# overridden root, not just the default .loom/worktrees.
# shellcheck source=lib/worktree-root.sh
source "$SCRIPT_DIR/lib/worktree-root.sh"
forge_detect

# Use gh-cached for read-only queries to reduce API calls (see issue #1609)
# Verify the Python interpreter works too — a broken runtime (e.g. unaccepted
# Xcode license) would make every subsequent gh call fail with a misleading error.
GH_CACHED="$REPO_ROOT/.loom/scripts/gh-cached"
if [[ "$FORGE_TYPE" == "github" ]] && [[ -x "$GH_CACHED" ]] && "$GH_CACHED" --version &>/dev/null; then
    GH="$GH_CACHED"
else
    GH="gh"
fi

REPO_NWO="$(forge_get_repo_nwo "$GH")" || \
  error "Could not determine repository. Is 'gh' authenticated?"

# Parse arguments
PR_NUMBER=""
CLEANUP_WORKTREE=true
DRY_RUN=false
AUTO_MERGE=false
WORKTREE_PATH_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cleanup-worktree) shift ;;  # no-op, cleanup is now the default
    --no-cleanup-worktree) CLEANUP_WORKTREE=false; shift ;;
    --worktree-path)
      [[ $# -lt 2 ]] && error "--worktree-path requires a value"
      WORKTREE_PATH_OVERRIDE="$2"
      shift 2
      ;;
    --worktree-path=*)
      WORKTREE_PATH_OVERRIDE="${1#--worktree-path=}"
      [[ -z "$WORKTREE_PATH_OVERRIDE" ]] && error "--worktree-path= requires a value"
      shift
      ;;
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

[[ -z "$PR_NUMBER" ]] && error "Usage: merge-pr.sh <pr-number> [--no-cleanup-worktree] [--worktree-path <dir>] [--dry-run] [--auto]"
[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || error "PR number must be numeric: $PR_NUMBER"

# Validate --worktree-path early (before any network calls) so bad input
# fails fast. The path must be a real directory and must appear in the
# repository's worktree list. We resolve to an absolute path via cd so
# downstream comparisons against the porcelain output work cleanly.
if [[ -n "$WORKTREE_PATH_OVERRIDE" ]]; then
  if [[ ! -d "$WORKTREE_PATH_OVERRIDE" ]]; then
    error "--worktree-path does not exist or is not a directory: $WORKTREE_PATH_OVERRIDE"
  fi
  _WT_ABS="$(cd "$WORKTREE_PATH_OVERRIDE" 2>/dev/null && pwd -P)" || \
    error "--worktree-path could not be resolved: $WORKTREE_PATH_OVERRIDE"
  # Verify the path is actually a worktree of this repo. `git worktree list`
  # prints absolute paths in column 1; awk on $1 is robust to trailing
  # metadata columns. We compare against the resolved absolute path.
  if ! git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | \
       awk -v p="$_WT_ABS" '/^worktree / { if ($2 == p) { found=1; exit } } END { exit !found }'; then
    error "--worktree-path is not a registered worktree of this repository: $WORKTREE_PATH_OVERRIDE (resolved: $_WT_ABS)"
  fi
  WORKTREE_PATH_OVERRIDE="$_WT_ABS"
  unset _WT_ABS

  # Warn if combined with --no-cleanup-worktree (no-op wins).
  if [[ "$CLEANUP_WORKTREE" == "false" ]]; then
    warning "--worktree-path was supplied but --no-cleanup-worktree wins; no cleanup will occur"
  fi
fi

# Fetch PR state
PR_JSON=$(forge_get_pr "$REPO_NWO" "$PR_NUMBER" "$GH") || \
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
#
# The auto-merge path now mirrors the sync path's resilience patterns:
#   - Retry on "Base branch was modified" with the same backoff loop.
#   - Recheck PR state on failure (concurrent shepherd may have already
#     merged it).
#   - Fall through to the shared cleanup block (lines below) instead of
#     exiting early. Cleanup is gated on `PR.merged == true`; if the
#     server-side merge is still queued, we skip local cleanup and let
#     loom-clean handle it.
#
# See issue #3279.
if [[ "$AUTO_MERGE" == "true" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] Would enable auto-merge for PR #$PR_NUMBER"
    exit 0
  fi

  MAX_MERGE_RETRIES=3
  MERGE_RETRY_DELAY=5
  AUTO_MERGE_OK=false

  for MERGE_ATTEMPT in $(seq 1 $MAX_MERGE_RETRIES); do
    AUTO_MERGE_OUTPUT=""
    # Prefer loom-auto-merge CLI (forge-agnostic, with poll-and-merge for Gitea)
    if command -v loom-auto-merge &>/dev/null; then
      [[ $MERGE_ATTEMPT -eq 1 ]] && info "Using loom-auto-merge (forge-agnostic auto-merge)"
      if AUTO_MERGE_OUTPUT=$(loom-auto-merge "$PR_NUMBER" --method squash 2>&1); then
        AUTO_MERGE_OK=true
        break
      fi
    else
      # Fallback: shell-based forge_auto_merge
      if AUTO_MERGE_OUTPUT=$(forge_auto_merge "$REPO_NWO" "$PR_NUMBER" 2>&1); then
        AUTO_MERGE_OK=true
        break
      fi
    fi

    # Check if PR merged despite error (concurrent merge by another shepherd)
    RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
    RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
    if [[ "$RECHECK" == "true" ]]; then
      warning "Auto-merge reported error but PR is already merged (race condition)"
      AUTO_MERGE_OK=true
      break
    fi

    # Retry on stale-branch race ("Base branch was modified")
    if echo "$AUTO_MERGE_OUTPUT" | grep -q "Base branch was modified"; then
      if [[ $MERGE_ATTEMPT -lt $MAX_MERGE_RETRIES ]]; then
        info "Branch is behind base branch, updating... (attempt $MERGE_ATTEMPT/$MAX_MERGE_RETRIES)"
        forge_update_branch "$REPO_NWO" "$PR_NUMBER" 2>/dev/null || \
          warning "Failed to update branch (continuing anyway)"
        info "Waiting ${MERGE_RETRY_DELAY}s for branch to sync..."
        sleep "$MERGE_RETRY_DELAY"
        MERGE_RETRY_DELAY=$((MERGE_RETRY_DELAY * 2))
        continue
      fi
    fi

    # PR is already CLEAN — GitHub's enablePullRequestAutoMerge mutation rejects
    # this state with "Pull request Pull request is in clean status" (the
    # doubled-word prefix is from GitHub's GraphQL error formatter). Match on
    # the unique substring to stay robust against future normalization. Fall
    # through to the synchronous-merge path below instead of erroring. See #3371.
    if echo "$AUTO_MERGE_OUTPUT" | grep -q "is in clean status"; then
      info "PR #$PR_NUMBER is already CLEAN; falling back to immediate merge"
      AUTO_MERGE=false      # let the synchronous-merge block at ~line 364 run
      AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
      break
    fi

    # PR is UNSTABLE — GitHub's enablePullRequestAutoMerge mutation rejects this
    # state with "Pull request Pull request is in unstable status" when one or
    # more rollup checks are red. The auto-merge call would deadlock indefinitely
    # waiting for those checks to go green, but if every failing check is
    # informational (NOT in branch protection's requiredStatusCheckContexts)
    # the immediate-merge path would succeed. Detect that case and fall through;
    # otherwise preserve the existing UNSTABLE refusal so we never bypass a
    # genuinely required check. Sibling of the CLEAN-fallback above. See #3486.
    if echo "$AUTO_MERGE_OUTPUT" | grep -q "is in unstable status"; then
      # Resolve the failing check names from the rollup against the PR's head
      # SHA, then compute (failing_checks) \ (required_contexts). If the set
      # difference equals failing_checks, every failure is informational.
      _UNSTABLE_HEAD_SHA="$(echo "$PR_JSON" | jq -r '.head.sha // empty')"
      _UNSTABLE_BASE_REF="$(echo "$PR_JSON" | jq -r '.base.ref // empty')"
      if [[ -z "$_UNSTABLE_HEAD_SHA" ]] || [[ -z "$_UNSTABLE_BASE_REF" ]]; then
        # Can't make a safe decision without the head SHA and base ref — fall
        # through to the existing refusal.
        error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
      fi

      _UNSTABLE_FAILING_RAW="$(forge_get_check_runs "$REPO_NWO" "$_UNSTABLE_HEAD_SHA" 2>/dev/null || echo '{"check_runs":[]}')"
      # Names of failing check runs (conclusion=failure OR conclusion=timed_out).
      # Sort + uniq to dedupe re-runs with the same context.
      _UNSTABLE_FAILING="$(echo "$_UNSTABLE_FAILING_RAW" | \
        jq -r '[.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "cancelled" or .conclusion == "action_required") | .name] | unique | .[]' 2>/dev/null || true)"

      if [[ -z "$_UNSTABLE_FAILING" ]]; then
        # No failing checks were enumerated — could be a transient API gap or
        # commit-status (vs check-run) failures. Be safe and keep the existing
        # error path.
        error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
      fi

      # Required contexts on the merge-target branch. Empty output means there
      # is no branch protection rule (or no required checks configured), so
      # every failing check is informational. A nonzero exit from the helper
      # signals a lookup failure (e.g. Gitea 5xx, network error, missing token,
      # or an unknown forge) — fail closed and preserve the UNSTABLE refusal.
      _UNSTABLE_REQUIRED=""
      _UNSTABLE_LOOKUP_RC=0
      _UNSTABLE_REQUIRED="$(forge_get_required_status_check_contexts "$REPO_NWO" "$_UNSTABLE_BASE_REF" "$GH" 2>/dev/null)" || _UNSTABLE_LOOKUP_RC=$?
      if [[ "$_UNSTABLE_LOOKUP_RC" -ne 0 ]]; then
        warning "Failed to resolve required status checks for $_UNSTABLE_BASE_REF (rc=$_UNSTABLE_LOOKUP_RC); preserving UNSTABLE refusal"
        unset _UNSTABLE_LOOKUP_RC
        error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
      fi
      unset _UNSTABLE_LOOKUP_RC

      # Compute set difference: failing_checks \ required_contexts.
      # `comm -23 <failing> <required>` lists lines unique to failing.
      _UNSTABLE_INFORMATIONAL="$(comm -23 \
        <(printf '%s\n' "$_UNSTABLE_FAILING" | sort -u) \
        <(printf '%s\n' "$_UNSTABLE_REQUIRED" | sort -u))"
      _UNSTABLE_OVERLAP="$(comm -12 \
        <(printf '%s\n' "$_UNSTABLE_FAILING" | sort -u) \
        <(printf '%s\n' "$_UNSTABLE_REQUIRED" | sort -u))"

      if [[ -z "$_UNSTABLE_OVERLAP" ]] && [[ -n "$_UNSTABLE_INFORMATIONAL" ]]; then
        # Every failing check is informational. Log a clear INFO message
        # naming them, then fall through to the synchronous-merge path.
        _UNSTABLE_COUNT="$(printf '%s\n' "$_UNSTABLE_INFORMATIONAL" | wc -l | tr -d ' ')"
        info "Falling back to immediate merge: ${_UNSTABLE_COUNT} informational check(s) failing (not in branch protection):"
        printf '%s\n' "$_UNSTABLE_INFORMATIONAL" | while IFS= read -r _ctx; do
          [[ -n "$_ctx" ]] && info "    - $_ctx"
        done
        AUTO_MERGE=false      # let the synchronous-merge block below run
        AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
        unset _UNSTABLE_HEAD_SHA _UNSTABLE_BASE_REF _UNSTABLE_FAILING_RAW _UNSTABLE_FAILING _UNSTABLE_REQUIRED _UNSTABLE_INFORMATIONAL _UNSTABLE_OVERLAP _UNSTABLE_COUNT
        break
      fi

      # At least one failing check IS branch-protection-required — preserve
      # the existing UNSTABLE refusal so we never silently bypass a required
      # gate. Fall through to the error path below.
      unset _UNSTABLE_HEAD_SHA _UNSTABLE_BASE_REF _UNSTABLE_FAILING_RAW _UNSTABLE_FAILING _UNSTABLE_REQUIRED _UNSTABLE_INFORMATIONAL _UNSTABLE_OVERLAP
    fi

    # Other auto-merge errors — fail immediately (no retry would help)
    error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
  done

  if [[ "$AUTO_MERGE_OK" != "true" ]]; then
    error "Failed to enable auto-merge for PR #$PR_NUMBER after $MAX_MERGE_RETRIES attempts"
  fi

  # If the CLEAN-status fall-through fired above, AUTO_MERGE has been flipped
  # to false. Skip the "Auto-merge enabled" success message and the post-auto
  # state poll — let the synchronous-merge block at ~line 376 take over.
  if [[ "$AUTO_MERGE" == "true" ]]; then
    success "Auto-merge enabled for PR #$PR_NUMBER"

    # Check whether the server-side merge has already completed. GitHub
    # auto-merge queues until checks pass, so on most PRs this is still
    # false right after enabling. If merged, fall through to the shared
    # cleanup block below. Otherwise skip cleanup — loom-clean will
    # handle the stale worktree later.
    POST_AUTO_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
    POST_AUTO_MERGED=$(echo "$POST_AUTO_JSON" | jq -r '.merged // false')
    if [[ "$POST_AUTO_MERGED" != "true" ]]; then
      info "Auto-merge queued (server-side merge pending checks); skipping local cleanup"
      info "Run loom-clean later to remove the worktree once GitHub completes the merge"
      exit 0
    fi
    info "PR #$PR_NUMBER already merged server-side; running cleanup"
    # Fall through to the shared cleanup block (branch deletion + worktree).
  fi
fi

# Synchronous-merge path. Skipped when --auto already succeeded server-side
# (in which case we fall through to the shared cleanup block below).
if [[ "$AUTO_MERGE" != "true" ]]; then

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
  MERGE_RESPONSE=$(forge_merge_pr "$REPO_NWO" "$PR_NUMBER" 2>&1) && break  # Success, exit loop

  # Check if it merged despite error (race condition)
  RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
  RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
  if [[ "$RECHECK" == "true" ]]; then
    warning "Merge reported error but PR is merged (race condition)"
    break
  fi

  # Check for "Merge already in progress" (HTTP 405)
  # This happens when auto-merge triggers at the same time as our merge attempt
  if echo "$MERGE_RESPONSE" | grep -q "Merge already in progress"; then
    info "Merge already in progress (HTTP 405), waiting for completion..."
    sleep 5
    RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
    RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
    if [[ "$RECHECK" == "true" ]]; then
      success "PR #$PR_NUMBER merged (concurrent merge completed)"
      break
    fi
    # Still not merged after wait - continue retry loop
    warning "Concurrent merge not yet complete, retrying..."
    continue
  fi

  # Check for stale branch error (base branch was modified)
  if echo "$MERGE_RESPONSE" | grep -q "Base branch was modified"; then
    if [[ $MERGE_ATTEMPT -lt $MAX_MERGE_RETRIES ]]; then
      info "Branch is behind base branch, updating... (attempt $MERGE_ATTEMPT/$MAX_MERGE_RETRIES)"

      # Update branch via forge API
      UPDATE_RESPONSE=$(forge_update_branch "$REPO_NWO" "$PR_NUMBER" 2>&1) || {
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
VERIFY_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
VERIFY_MERGED=$(echo "$VERIFY_JSON" | jq -r '.merged // false')
if [[ "$VERIFY_MERGED" != "true" ]]; then
  error "Merge API call returned but PR #$PR_NUMBER is not merged"
fi

success "PR #$PR_NUMBER merged successfully"

fi  # end synchronous-merge path (AUTO_MERGE != "true")

# NOTE: Label cleanup on linked issues is intentionally skipped.
# Labels on closed/merged items are harmless — all agents filter by open state.
# See: https://github.com/rjwalters/loom/issues/2838
#
# NOTE: This script does NOT close linked issues. Issue auto-close is GitHub's
# responsibility — GitHub's PR parser closes issues referenced via `Closes #N`,
# `Fixes #N`, `Resolves #N` (and the case/tense variants) on merge. Champion's
# "Verify Issue Auto-Close" step is a belt-and-suspenders check that uses
# `forge_pr_close_targets` (which delegates to GitHub's GraphQL
# `closingIssuesReferences` field) to confirm closure. If you are debugging
# why an unintended issue was closed, look at the PR body and Champion logs,
# not at this script. See: https://github.com/rjwalters/loom/issues/3267

# Delete remote branch (skip if forge auto-deletes on merge)
DELETE_BRANCH_ON_MERGE=$(forge_check_auto_delete "$REPO_NWO" "$GH")
if [[ "$DELETE_BRANCH_ON_MERGE" == "true" ]]; then
  info "Skipping branch deletion (auto-delete is enabled)"
else
  info "Deleting remote branch: $PR_BRANCH"
  forge_delete_branch "$REPO_NWO" "$PR_BRANCH" && \
    success "Branch '$PR_BRANCH' deleted" || \
    warning "Could not delete branch '$PR_BRANCH' (may already be deleted)"
fi

# Cleanup worktree if requested.
#
# Ownership model (see issue #3334): Loom owns worktrees it created under
# .loom/worktrees/ (marked with a .loom-managed sentinel file by worktree.sh
# or pr-worktree.sh). Any worktree lacking the sentinel is treated as
# user-owned and is never removed by this script. Operators can also set
# LOOM_PRESERVE_WORKTREE=1 to skip cleanup unconditionally.
#
# Two worktree-path conventions are recognized:
#   - .loom/worktrees/issue-<N>/  (Loom-issue branches: feature/issue-<N>)
#   - .loom/worktrees/pr-<N>/     (external-fork / ad-hoc branches; #3358)
#
# Branch-to-issue regex is the strict `^feature/issue-([0-9]+)$` pattern so
# branches like `release-1` or `fix-bug-42` correctly classify as PR-style
# (not issue-style) and clean up the right worktree.
# Look up the branch attached to a worktree via porcelain. Prints the branch
# short-name (without refs/heads/ prefix) on stdout. Returns 0 with empty
# output for detached / bare worktrees (no branch line in the stanza).
_worktree_branch_for() {
  local target="$1" target_abs
  target_abs="$(cd "$target" 2>/dev/null && pwd -P)" || target_abs="$target"
  git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | \
    awk -v p="$target_abs" '
      /^worktree / { wt=$2; br=""; next }
      /^branch /   { br=$2 }
      /^$/         { if (wt == p && br != "") { sub(/^refs\/heads\//, "", br); print br; exit } }
      END          { if (wt == p && br != "") { sub(/^refs\/heads\//, "", br); print br } }
    '
}

# Walk porcelain output for a worktree whose branch matches the given branch
# short-name. Prints the worktree absolute path or nothing. Skips detached /
# bare entries (they have no `branch refs/heads/...` line).
_find_worktree_by_branch() {
  local want_branch="$1"
  git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | \
    awk -v want="refs/heads/${want_branch}" '
      /^worktree / { wt=$2; br=""; next }
      /^branch /   { br=$2 }
      /^$/         { if (br == want) { print wt; exit } }
      END          { if (br == want) { print wt } }
    '
}

# Delete the matching local branch. Uses `git branch -d` (not -D) so unmerged
# commits abort the delete — that's the right safety net. Never fails the
# cleanup pipeline; warns on errors.
_maybe_delete_local_branch() {
  local branch="$1"
  if [[ -z "$branch" ]]; then
    return 0
  fi
  if ! git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    info "Local branch '$branch' does not exist — skipping branch delete"
    return 0
  fi
  if git -C "$REPO_ROOT" branch -d "$branch" 2>/dev/null; then
    success "Local branch '$branch' deleted"
  else
    warning "Could not delete local branch '$branch' (may have unmerged commits — use 'git branch -D' if intentional)"
  fi
}

# _remove_loom_worktree <path> [allow_unmanaged]
#
# When allow_unmanaged is "true" (only set by the --worktree-path code path),
# the .loom-managed sentinel check is skipped — the caller has taken explicit
# responsibility for the cleanup decision. The default (no second arg, or
# "false") preserves the original sentinel guard.
_remove_loom_worktree() {
  local worktree_path="$1"
  local allow_unmanaged="${2:-false}"
  if [[ ! -d "$worktree_path" ]]; then
    info "No worktree found at $worktree_path"
    return 0
  fi
  if [[ "$allow_unmanaged" != "true" ]] && [[ ! -f "$worktree_path/.loom-managed" ]]; then
    warning "Worktree at $worktree_path lacks .loom-managed sentinel — refusing to remove (user-owned)"
    return 0
  fi
  if [[ "$allow_unmanaged" == "true" ]] && [[ ! -f "$worktree_path/.loom-managed" ]]; then
    info "Bypassing sentinel guard (--worktree-path explicit opt-in for $worktree_path)"
  fi
  # Record the attached branch BEFORE removing the worktree (the porcelain
  # entry vanishes once the worktree is gone). Only relevant when allow_unmanaged
  # — the default issue/pr path already has the branch encoded in PR_BRANCH.
  local attached_branch=""
  if [[ "$allow_unmanaged" == "true" ]]; then
    attached_branch="$(_worktree_branch_for "$worktree_path")"
  fi
  # If our shell is inside the worktree we're removing, hop out first.
  local current_dir worktree_real in_worktree=false
  current_dir="$(pwd -P 2>/dev/null || pwd)"
  worktree_real="$(cd "$worktree_path" 2>/dev/null && pwd -P || echo "$worktree_path")"
  if [[ "$current_dir" == "$worktree_real"* ]]; then
    in_worktree=true
    cd "$REPO_ROOT"
  fi
  info "Removing worktree: $worktree_path"
  if git -C "$REPO_ROOT" worktree remove "$worktree_path" --force 2>/dev/null; then
    success "Worktree removed"
    if [[ "$in_worktree" == "true" ]]; then
      echo ""
      warning "Your shell's working directory was inside the removed worktree."
      warning "Run this command to fix:"
      echo "  cd $REPO_ROOT"
    fi
    # For the explicit-override path, also tidy up the attached local branch.
    # We defer this to AFTER `git worktree remove` succeeds so the worktree's
    # checkout lock is released first.
    if [[ "$allow_unmanaged" == "true" ]] && [[ -n "$attached_branch" ]]; then
      _maybe_delete_local_branch "$attached_branch"
    fi
  else
    warning "Could not remove worktree at $worktree_path"
  fi
}

if [[ "$CLEANUP_WORKTREE" == "true" ]]; then
  if [[ "${LOOM_PRESERVE_WORKTREE:-0}" == "1" ]]; then
    info "Worktree cleanup skipped (LOOM_PRESERVE_WORKTREE=1)"
  elif [[ -n "$WORKTREE_PATH_OVERRIDE" ]]; then
    # Explicit operator opt-in: bypass the sentinel guard for THIS path only.
    # The path was already validated at parse time (exists + is a registered
    # worktree of this repo). _remove_loom_worktree will also delete the
    # matching local branch via `git branch -d` (refuses on unmerged commits).
    info "Cleanup target overridden by --worktree-path: $WORKTREE_PATH_OVERRIDE"
    _remove_loom_worktree "$WORKTREE_PATH_OVERRIDE" "true"
  else
    # Strict pattern: only `feature/issue-<N>` matches. Trailing-number
    # heuristics would misclassify branches like `release-1`.
    # Resolve the worktree base through the shared helper so an overridden
    # root (#3530) is discovered here; defaults to $REPO_ROOT/.loom/worktrees.
    WT_ROOT_DIR="$(loom_worktree_root "$REPO_ROOT")"
    DEFAULT_WT_PATH=""
    if [[ "$PR_BRANCH" =~ ^feature/issue-([0-9]+)$ ]]; then
      ISSUE_NUM="${BASH_REMATCH[1]}"
      DEFAULT_WT_PATH="$WT_ROOT_DIR/issue-$ISSUE_NUM"
    else
      # External-fork / ad-hoc branch — the doctor would have used a
      # `pr-<PR_NUMBER>` worktree if any.
      DEFAULT_WT_PATH="$WT_ROOT_DIR/pr-$PR_NUMBER"
    fi
    if [[ -d "$DEFAULT_WT_PATH" ]]; then
      _remove_loom_worktree "$DEFAULT_WT_PATH"
    else
      # Discovery fallback (warn-only): the Loom-convention path is missing,
      # so walk porcelain looking for any worktree tracking $PR_BRANCH. We
      # never auto-remove a discovered worktree — that would violate the
      # ownership model from #3334. Instead we surface the path so the
      # operator can re-run with --worktree-path.
      DISCOVERED_WT="$(_find_worktree_by_branch "$PR_BRANCH")"
      if [[ -n "$DISCOVERED_WT" ]]; then
        if [[ -f "$DISCOVERED_WT/.loom-managed" ]]; then
          # Rare case: Loom-managed worktree at a non-standard path. The
          # sentinel says it's safe to remove, so do so.
          info "Discovered Loom-managed worktree at non-standard path: $DISCOVERED_WT"
          _remove_loom_worktree "$DISCOVERED_WT"
        else
          warning "Discovered worktree for branch '$PR_BRANCH' at: $DISCOVERED_WT"
          warning "Worktree lacks .loom-managed sentinel — not removing (user-owned)."
          warning "To clean it up, re-run with: --worktree-path '$DISCOVERED_WT'"
          warning "Or manually: git worktree remove '$DISCOVERED_WT'"
        fi
      else
        info "No worktree found at $DEFAULT_WT_PATH (and none tracking '$PR_BRANCH' in 'git worktree list')"
      fi
    fi
  fi
fi

success "Done"
