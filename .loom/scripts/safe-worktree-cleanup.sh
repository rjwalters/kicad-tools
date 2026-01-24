#!/usr/bin/env bash
# Safe worktree cleanup - only removes worktrees for MERGED PRs
# Usage: ./scripts/safe-worktree-cleanup.sh [--dry-run] [--force] [--grace-period N]
#
# Unlike blunt cleanup, this script:
# - Only removes worktrees when the PR is MERGED (not just closed)
# - Checks for uncommitted changes before removal
# - Applies a grace period after merge to avoid race conditions
# - Tracks cleanup state in daemon-state.json

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

error() { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }
info() { echo -e "${BLUE}$*${NC}"; }
success() { echo -e "${GREEN}$*${NC}"; }
warning() { echo -e "${YELLOW}$*${NC}"; }
header() { echo -e "${CYAN}$*${NC}"; }

# Configuration
DRY_RUN=false
FORCE=false
GRACE_PERIOD=600  # 10 minutes in seconds

# Detect repository root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || error "Not in a git repository"

# State file for tracking cleanup
DAEMON_STATE="$REPO_ROOT/.loom/daemon-state.json"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --force|-f)
      FORCE=true
      shift
      ;;
    --grace-period)
      GRACE_PERIOD="$2"
      shift 2
      ;;
    --help|-h)
      cat <<EOF
Safe worktree cleanup - only removes worktrees for MERGED PRs

Usage: ./scripts/safe-worktree-cleanup.sh [options]

Options:
  --dry-run           Show what would be cleaned without making changes
  -f, --force         Skip grace period and uncommitted changes check
  --grace-period N    Seconds to wait after PR merge (default: 600 = 10 min)
  -h, --help          Show this help message

Safety Features:
  - Only cleans worktrees with MERGED PRs (not just closed)
  - Checks for uncommitted changes before removal
  - Grace period after merge to avoid race conditions
  - Tracks cleanup state in daemon-state.json

Cleanup Criteria:
  A worktree is cleaned when ALL of the following are true:
  1. The associated issue is CLOSED
  2. The PR is MERGED (has mergedAt timestamp)
  3. Grace period has passed since merge
  4. No uncommitted changes exist (unless --force)
EOF
      exit 0
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# Functions
check_pr_merged() {
  local issue_num="$1"
  local branch_name="feature/issue-${issue_num}"

  # Find PR by head branch
  local pr_info=$(gh pr list --head "$branch_name" --state all --json number,state,mergedAt --jq '.[0] // empty' 2>/dev/null || echo "")

  if [[ -z "$pr_info" ]]; then
    # Try searching by issue reference
    pr_info=$(gh pr list --search "Closes #${issue_num}" --state all --json number,state,mergedAt --jq '.[0] // empty' 2>/dev/null || echo "")
  fi

  if [[ -z "$pr_info" ]]; then
    echo "NO_PR"
    return
  fi

  local state=$(echo "$pr_info" | jq -r '.state // "UNKNOWN"')
  local merged_at=$(echo "$pr_info" | jq -r '.mergedAt // "null"')

  if [[ "$merged_at" != "null" && "$merged_at" != "" ]]; then
    echo "MERGED:$merged_at"
  elif [[ "$state" == "CLOSED" ]]; then
    echo "CLOSED_NO_MERGE"
  elif [[ "$state" == "OPEN" ]]; then
    echo "OPEN"
  else
    echo "UNKNOWN"
  fi
}

check_uncommitted_changes() {
  local worktree_path="$1"

  if [[ ! -d "$worktree_path" ]]; then
    echo "false"
    return
  fi

  # Check for any uncommitted changes (staged or unstaged)
  if git -C "$worktree_path" diff --quiet 2>/dev/null && \
     git -C "$worktree_path" diff --cached --quiet 2>/dev/null; then
    echo "false"
  else
    echo "true"
  fi
}

check_grace_period() {
  local merged_at="$1"
  local now=$(date +%s)

  # Parse merged_at timestamp
  local merged_ts
  if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    merged_ts=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$merged_at" +%s 2>/dev/null || echo "0")
  else
    # Linux
    merged_ts=$(date -d "$merged_at" +%s 2>/dev/null || echo "0")
  fi

  local elapsed=$((now - merged_ts))

  if [[ $elapsed -gt $GRACE_PERIOD ]]; then
    echo "passed:$elapsed"
  else
    echo "waiting:$((GRACE_PERIOD - elapsed))"
  fi
}

cleanup_worktree() {
  local worktree_path="$1"
  local issue_num="$2"
  local branch_name="feature/issue-${issue_num}"

  if [[ "$DRY_RUN" == true ]]; then
    info "Would remove: $worktree_path"
    info "Would delete branch: $branch_name"
    return 0
  fi

  # Remove worktree
  if git worktree remove "$worktree_path" --force 2>/dev/null; then
    success "Removed worktree: $worktree_path"
  else
    warning "Failed to remove worktree: $worktree_path"
    return 1
  fi

  # Delete local branch (if it exists)
  if git branch -d "$branch_name" 2>/dev/null; then
    success "Deleted branch: $branch_name"
  elif git branch -D "$branch_name" 2>/dev/null; then
    success "Force-deleted branch: $branch_name"
  else
    info "Branch already deleted or doesn't exist: $branch_name"
  fi

  return 0
}

update_cleanup_state() {
  local issue_num="$1"
  local status="$2"

  if [[ ! -f "$DAEMON_STATE" ]]; then
    return
  fi

  local timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  # Initialize cleanup section if needed
  if ! jq -e '.cleanup' "$DAEMON_STATE" >/dev/null 2>&1; then
    jq '.cleanup = {"lastRun": null, "lastCleaned": [], "pendingCleanup": [], "errors": []}' \
      "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE"
  fi

  case "$status" in
    cleaned)
      jq ".cleanup.lastCleaned += [\"issue-${issue_num}\"] | .cleanup.lastRun = \"$timestamp\"" \
        "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE"
      ;;
    pending)
      jq ".cleanup.pendingCleanup += [\"issue-${issue_num}\"] | .cleanup.pendingCleanup |= unique" \
        "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE"
      ;;
    error)
      jq ".cleanup.errors += [{\"issue\": $issue_num, \"timestamp\": \"$timestamp\"}]" \
        "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE"
      ;;
  esac
}

# Main
cd "$REPO_ROOT"

header "========================================"
header "  Safe Worktree Cleanup"
if [[ "$DRY_RUN" == true ]]; then
  header "  (DRY RUN MODE)"
fi
header "========================================"
echo ""

# Get worktree directory
WORKTREE_DIR="$REPO_ROOT/.loom/worktrees"

if [[ ! -d "$WORKTREE_DIR" ]]; then
  info "No worktrees directory found"
  exit 0
fi

# Counters
cleaned=0
skipped_open=0
skipped_not_merged=0
skipped_grace=0
skipped_uncommitted=0
errors=0

# Process each worktree
for worktree_path in "$WORKTREE_DIR"/issue-*; do
  if [[ ! -d "$worktree_path" ]]; then
    continue
  fi

  issue_num=$(basename "$worktree_path" | sed 's/issue-//')

  if [[ ! "$issue_num" =~ ^[0-9]+$ ]]; then
    continue
  fi

  echo "Checking worktree: issue-$issue_num"

  # Check issue state
  issue_state=$(gh issue view "$issue_num" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN")

  if [[ "$issue_state" != "CLOSED" ]]; then
    info "  Issue #$issue_num is $issue_state - skipping"
    ((skipped_open++)) || true
    continue
  fi

  # Check PR merge status
  pr_status=$(check_pr_merged "$issue_num")

  case "$pr_status" in
    MERGED:*)
      merged_at="${pr_status#MERGED:}"
      ;;
    CLOSED_NO_MERGE)
      warning "  PR closed without merge - skipping (may need investigation)"
      ((skipped_not_merged++)) || true
      continue
      ;;
    OPEN)
      info "  PR still open - skipping"
      ((skipped_open++)) || true
      continue
      ;;
    NO_PR)
      warning "  No PR found for issue - skipping"
      ((skipped_not_merged++)) || true
      continue
      ;;
    *)
      warning "  Unknown PR status - skipping"
      ((errors++)) || true
      continue
      ;;
  esac

  # Check grace period
  if [[ "$FORCE" != true ]]; then
    grace_status=$(check_grace_period "$merged_at")
    case "$grace_status" in
      waiting:*)
        remaining="${grace_status#waiting:}"
        info "  PR merged but grace period not passed (${remaining}s remaining)"
        update_cleanup_state "$issue_num" "pending"
        ((skipped_grace++)) || true
        continue
        ;;
    esac
  fi

  # Check for uncommitted changes
  if [[ "$FORCE" != true ]]; then
    has_changes=$(check_uncommitted_changes "$worktree_path")
    if [[ "$has_changes" == "true" ]]; then
      warning "  Uncommitted changes detected - skipping"
      warning "    To force: ./scripts/safe-worktree-cleanup.sh --force"
      ((skipped_uncommitted++)) || true
      continue
    fi
  fi

  # All checks passed - cleanup
  if cleanup_worktree "$worktree_path" "$issue_num"; then
    if [[ "$DRY_RUN" != true ]]; then
      update_cleanup_state "$issue_num" "cleaned"
    fi
    ((cleaned++)) || true
  else
    if [[ "$DRY_RUN" != true ]]; then
      update_cleanup_state "$issue_num" "error"
    fi
    ((errors++)) || true
  fi
done

# Prune orphaned git worktree references
echo ""
header "Pruning Orphaned References"
if [[ "$DRY_RUN" == true ]]; then
  git worktree prune --dry-run --verbose 2>&1 || true
else
  git worktree prune --verbose 2>&1 || true
fi

# Summary
echo ""
header "========================================"
header "  Summary"
header "========================================"
echo ""

if [[ "$DRY_RUN" == true ]]; then
  echo "  Would clean: $cleaned worktree(s)"
else
  echo "  Cleaned: $cleaned worktree(s)"
fi
echo "  Skipped (open): $skipped_open"
echo "  Skipped (not merged): $skipped_not_merged"
echo "  Skipped (grace period): $skipped_grace"
echo "  Skipped (uncommitted): $skipped_uncommitted"
echo "  Errors: $errors"
echo ""

if [[ "$DRY_RUN" == true ]]; then
  warning "Dry run complete - no changes made"
  info "Run without --dry-run to perform cleanup"
else
  success "Cleanup complete!"
fi
