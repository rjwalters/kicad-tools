#!/usr/bin/env bash
# Loom Unified Cleanup - Consolidates clean.sh, cleanup.sh, and safe-worktree-cleanup.sh
# Usage: ./.loom/scripts/clean.sh [options]
#
# This is the unified cleanup script for Loom. It consolidates the functionality of:
#   - clean.sh (general cleanup with UI polish)
#   - cleanup.sh (build artifacts and worktree cleanup)
#   - safe-worktree-cleanup.sh (safe cleanup with grace period and merge checks)
#
# AGENT USAGE INSTRUCTIONS:
#   Non-interactive mode (for Claude Code):
#     ./scripts/clean.sh --force
#     ./scripts/clean.sh -f
#
#   Interactive mode (prompts for confirmation):
#     ./scripts/clean.sh

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

# Find git repository root (works from any subdirectory)
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || \
  error "Not in a git repository"

# Configuration
DRY_RUN=false
DEEP_CLEAN=false
FORCE=false
SAFE_MODE=false
GRACE_PERIOD=600  # 10 minutes in seconds (for --safe mode)
WORKTREES_ONLY=false
BRANCHES_ONLY=false
TMUX_ONLY=false

# State file for tracking cleanup (used in --safe mode)
DAEMON_STATE="$REPO_ROOT/.loom/daemon-state.json"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --deep)
      DEEP_CLEAN=true
      shift
      ;;
    --force|-f|--yes|-y)
      FORCE=true
      shift
      ;;
    --safe)
      SAFE_MODE=true
      shift
      ;;
    --grace-period)
      GRACE_PERIOD="$2"
      shift 2
      ;;
    --worktrees-only|--worktrees)
      WORKTREES_ONLY=true
      shift
      ;;
    --branches-only|--branches)
      BRANCHES_ONLY=true
      shift
      ;;
    --tmux-only|--tmux)
      TMUX_ONLY=true
      shift
      ;;
    --help|-h)
      cat <<EOF
Loom Unified Cleanup - Restore repository to clean state

Usage: ./.loom/scripts/clean.sh [options]

Options:
  --dry-run              Show what would be cleaned without making changes
  --deep                 Deep clean (includes build artifacts)
  -f, --force, -y, --yes Non-interactive mode (auto-confirm all prompts)
  --safe                 Safe mode: only remove worktrees with MERGED PRs,
                         check for uncommitted changes, apply grace period
  --grace-period N       Seconds to wait after PR merge (default: 600, requires --safe)
  --worktrees-only       Only clean worktrees (skip branches and tmux)
  --branches-only        Only clean branches (skip worktrees and tmux)
  --tmux-only            Only clean tmux sessions (skip worktrees and branches)
  -h, --help             Show this help message

Standard cleanup:
  - Stale worktrees (for closed issues)
  - Merged local branches for closed issues
  - Loom tmux sessions (loom-*)

Deep cleanup (--deep):
  - All of the above, plus:
  - target/ directory (Rust build artifacts)
  - node_modules/ directory

Safe mode (--safe):
  - Only removes worktrees when PR is MERGED (not just closed)
  - Checks for uncommitted changes before removal
  - Applies grace period after merge to avoid race conditions
  - Tracks cleanup state in daemon-state.json

Examples:
  ./.loom/scripts/clean.sh                     # Interactive standard cleanup
  ./.loom/scripts/clean.sh --force             # Non-interactive cleanup (CI/automation)
  ./.loom/scripts/clean.sh --deep              # Include build artifacts
  ./.loom/scripts/clean.sh --safe              # Safe mode (MERGED PRs only)
  ./.loom/scripts/clean.sh --safe --force      # Safe mode, non-interactive
  ./.loom/scripts/clean.sh --worktrees-only    # Just worktrees
  ./.loom/scripts/clean.sh --branches-only     # Just branches

Backwards Compatibility:
  This script is called by cleanup.sh and safe-worktree-cleanup.sh
  for backwards compatibility. Those scripts are now thin wrappers.
EOF
      exit 0
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

check_pr_merged() {
  local issue_num="$1"
  local branch_name="feature/issue-${issue_num}"

  # Find PR by head branch
  local pr_info
  pr_info=$(gh pr list --head "$branch_name" --state all --json number,state,mergedAt --jq '.[0] // empty' 2>/dev/null || echo "")

  if [[ -z "$pr_info" ]]; then
    # Try searching by issue reference
    pr_info=$(gh pr list --search "Closes #${issue_num}" --state all --json number,state,mergedAt --jq '.[0] // empty' 2>/dev/null || echo "")
  fi

  if [[ -z "$pr_info" ]]; then
    echo "NO_PR"
    return
  fi

  local state merged_at
  state=$(echo "$pr_info" | jq -r '.state // "UNKNOWN"')
  merged_at=$(echo "$pr_info" | jq -r '.mergedAt // "null"')

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
  local now
  now=$(date +%s)

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

update_cleanup_state() {
  local issue_num="$1"
  local status="$2"

  if [[ ! -f "$DAEMON_STATE" ]]; then
    return
  fi

  local timestamp
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

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

# =============================================================================
# MAIN
# =============================================================================

cd "$REPO_ROOT"

# Show banner
echo ""
header "========================================"
if [[ "$DEEP_CLEAN" == true ]]; then
  header "  Loom Deep Cleanup"
elif [[ "$SAFE_MODE" == true ]]; then
  header "  Loom Safe Cleanup"
else
  header "  Loom Cleanup"
fi
if [[ "$DRY_RUN" == true ]]; then
  header "  (DRY RUN MODE)"
fi
header "========================================"
echo ""

# Show what will be cleaned
if [[ "$WORKTREES_ONLY" == false && "$BRANCHES_ONLY" == false && "$TMUX_ONLY" == false ]]; then
  info "Cleanup targets:"
  echo "  - Orphaned worktrees (git worktree prune)"
  echo "  - Local branches for closed issues"
  echo "  - Loom tmux sessions (loom-*)"

  if [[ "$SAFE_MODE" == true ]]; then
    echo ""
    warning "Safe mode enabled:"
    echo "  - Only removes worktrees with MERGED PRs"
    echo "  - Checks for uncommitted changes"
    echo "  - Grace period: ${GRACE_PERIOD}s after merge"
  fi

  if [[ "$DEEP_CLEAN" == true ]]; then
    echo ""
    warning "Deep cleanup additions:"
    if [[ -d "target" ]]; then
      SIZE=$(du -sh target 2>/dev/null | cut -f1)
      echo "  - target/ directory ($SIZE)"
    else
      echo "  - target/ directory (not present)"
    fi
    if [[ -d "node_modules" ]]; then
      SIZE=$(du -sh node_modules 2>/dev/null | cut -f1)
      echo "  - node_modules/ directory ($SIZE)"
    else
      echo "  - node_modules/ directory (not present)"
    fi
  fi
fi

echo ""

# Confirmation
if [[ "$DRY_RUN" == true ]]; then
  warning "DRY RUN - No changes will be made"
  CONFIRM=y
elif [[ "$FORCE" == true ]]; then
  info "FORCE MODE - Auto-confirming all prompts"
  CONFIRM=y
else
  read -r -p "Proceed with cleanup? [y/N] " -n 1 CONFIRM
  echo ""
fi

if [[ ! $CONFIRM =~ ^[Yy]$ ]]; then
  info "Cleanup cancelled"
  exit 0
fi

echo ""

# Counters for summary
cleaned_worktrees=0
skipped_open=0
skipped_in_use=0
skipped_not_merged=0
skipped_grace=0
skipped_uncommitted=0
cleaned_branches=0
kept_branches=0
killed_tmux=0
errors=0

# =============================================================================
# CLEANUP: Worktrees
# =============================================================================

if [[ "$BRANCHES_ONLY" == false && "$TMUX_ONLY" == false ]]; then
  header "Cleaning Worktrees"
  echo ""

  # Check each .loom/worktrees/issue-* directory
  if [[ -d ".loom/worktrees" ]]; then
    for worktree_dir in .loom/worktrees/issue-*; do
      if [[ ! -d "$worktree_dir" ]]; then
        continue
      fi

      worktree_path="$(cd "$worktree_dir" && pwd)"
      issue_num=$(basename "$worktree_dir" | sed 's/issue-//')

      if [[ ! "$issue_num" =~ ^[0-9]+$ ]]; then
        continue
      fi

      echo "Checking worktree: issue-$issue_num"

      # Check for in-use marker (shepherd orchestration in progress)
      # See issue #1485: prevents premature cleanup during orchestration
      marker_file="${worktree_path}/.loom-in-use"
      if [[ -f "$marker_file" ]]; then
        marker_task_id="" marker_pid=""
        marker_task_id=$(jq -r '.shepherd_task_id // "unknown"' "$marker_file" 2>/dev/null || echo "unknown")
        marker_pid=$(jq -r '.pid // "unknown"' "$marker_file" 2>/dev/null || echo "unknown")
        info "  Worktree in use by shepherd (task: $marker_task_id, pid: $marker_pid) - preserving"
        ((skipped_in_use++)) || true
        continue
      fi

      # Check issue state
      if ! command -v gh &> /dev/null; then
        warning "  gh CLI not found, skipping GitHub checks"
        continue
      fi

      issue_state=$(gh issue view "$issue_num" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN")

      if [[ "$issue_state" != "CLOSED" ]]; then
        info "  Issue #$issue_num is $issue_state - preserving"
        ((skipped_open++)) || true
        continue
      fi

      # Safe mode: additional checks
      if [[ "$SAFE_MODE" == true ]]; then
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
            warning "  No PR found for closed issue - skipping"
            ((skipped_not_merged++)) || true
            continue
            ;;
          *)
            warning "  Unknown PR status - skipping"
            ((errors++)) || true
            continue
            ;;
        esac

        # Check grace period (unless --force)
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

        # Check for uncommitted changes (unless --force)
        if [[ "$FORCE" != true ]]; then
          has_changes=$(check_uncommitted_changes "$worktree_path")
          if [[ "$has_changes" == "true" ]]; then
            warning "  Uncommitted changes detected - skipping"
            ((skipped_uncommitted++)) || true
            continue
          fi
        fi

        # All checks passed - cleanup with state tracking
        if cleanup_worktree "$worktree_path" "$issue_num"; then
          if [[ "$DRY_RUN" != true ]]; then
            update_cleanup_state "$issue_num" "cleaned"
          fi
          ((cleaned_worktrees++)) || true
        else
          if [[ "$DRY_RUN" != true ]]; then
            update_cleanup_state "$issue_num" "error"
          fi
          ((errors++)) || true
        fi
      else
        # Standard mode: just check if issue is closed
        warning "  Issue #$issue_num is CLOSED"

        if [[ "$DRY_RUN" == true ]]; then
          info "  Would remove: $worktree_dir"
          ((cleaned_worktrees++)) || true
        elif [[ "$FORCE" == true ]]; then
          info "  Auto-removing: $worktree_dir"
          if cleanup_worktree "$worktree_path" "$issue_num"; then
            ((cleaned_worktrees++)) || true
          else
            ((errors++)) || true
          fi
        else
          read -r -p "  Force remove this worktree? [y/N] " -n 1 REMOVE_WORKTREE
          echo ""

          if [[ $REMOVE_WORKTREE =~ ^[Yy]$ ]]; then
            if cleanup_worktree "$worktree_path" "$issue_num"; then
              ((cleaned_worktrees++)) || true
            else
              ((errors++)) || true
            fi
          else
            info "  Skipping: $worktree_dir"
            ((skipped_open++)) || true
          fi
        fi
      fi
    done
  else
    info "No worktrees directory found"
  fi

  # Prune orphaned references
  echo ""
  header "Pruning Orphaned References"
  if [[ "$DRY_RUN" == true ]]; then
    PRUNE_OUTPUT=$(git worktree prune --dry-run --verbose 2>&1 || true)
    if [[ -n "$PRUNE_OUTPUT" ]]; then
      echo "$PRUNE_OUTPUT"
    else
      success "No orphaned worktrees to prune"
    fi
  else
    git worktree prune --verbose 2>&1 || success "No orphaned worktrees to prune"
  fi

  echo ""
fi

# =============================================================================
# CLEANUP: Branches
# =============================================================================

if [[ "$WORKTREES_ONLY" == false && "$TMUX_ONLY" == false ]]; then
  header "Cleaning Merged Branches"
  echo ""

  # Check if cleanup-branches.sh exists (only in Loom repo, not target repos)
  if [[ -f "scripts/cleanup-branches.sh" ]]; then
    if [[ "$DRY_RUN" == true ]]; then
      ./scripts/cleanup-branches.sh --dry-run
    elif [[ "$FORCE" == true ]]; then
      ./scripts/cleanup-branches.sh --force
    else
      ./scripts/cleanup-branches.sh
    fi
  else
    # Manual branch cleanup for target repositories
    branches=$(git branch | grep "feature/issue-" | sed 's/^[*+ ]*//' || true)

    if [[ -z "$branches" ]]; then
      success "No feature branches found"
    else
      for branch in $branches; do
        # Extract issue number
        issue_num=$(echo "$branch" | sed 's/feature\/issue-//' | sed 's/-.*//' | sed 's/[^0-9].*//')

        if [[ ! "$issue_num" =~ ^[0-9]+$ ]]; then
          continue
        fi

        # Check issue status
        if command -v gh &> /dev/null; then
          status=$(gh issue view "$issue_num" --json state --jq .state 2>/dev/null || echo "NOT_FOUND")

          if [[ "$status" == "CLOSED" ]]; then
            echo -e "${GREEN}  Issue #$issue_num CLOSED${NC} - deleting $branch"
            if [[ "$DRY_RUN" == false ]]; then
              git branch -D "$branch" 2>/dev/null && ((cleaned_branches++)) || ((errors++))
            else
              ((cleaned_branches++)) || true
            fi
          elif [[ "$status" == "OPEN" ]]; then
            echo -e "${BLUE}  Issue #$issue_num OPEN${NC} - keeping $branch"
            ((kept_branches++)) || true
          fi
        fi
      done
    fi
  fi

  echo ""
fi

# =============================================================================
# CLEANUP: Tmux Sessions
# =============================================================================

if [[ "$WORKTREES_ONLY" == false && "$BRANCHES_ONLY" == false ]]; then
  header "Cleaning Loom Tmux Sessions"
  echo ""

  LOOM_SESSIONS=$(tmux list-sessions 2>/dev/null | grep '^loom-' | cut -d: -f1 || true)

  if [[ -n "$LOOM_SESSIONS" ]]; then
    echo "Found Loom tmux sessions:"
    echo "$LOOM_SESSIONS" | while read -r session; do
      echo "  - $session"
    done
    echo ""

    if [[ "$DRY_RUN" == true ]]; then
      info "Would kill these sessions"
      killed_tmux=$(echo "$LOOM_SESSIONS" | wc -l | tr -d ' ')
    else
      echo "$LOOM_SESSIONS" | while read -r session; do
        if tmux kill-session -t "$session" 2>/dev/null; then
          success "Killed: $session"
          ((killed_tmux++)) || true
        fi
      done
    fi
  else
    success "No Loom tmux sessions found"
  fi

  echo ""
fi

# =============================================================================
# DEEP CLEANUP: Build Artifacts
# =============================================================================

if [[ "$DEEP_CLEAN" == true ]]; then
  header "Deep Cleaning Build Artifacts"
  echo ""

  # Remove target/
  if [[ -d "target" ]]; then
    SIZE=$(du -sh target 2>/dev/null | cut -f1)
    if [[ "$DRY_RUN" == true ]]; then
      info "Would remove target/ ($SIZE)"
    else
      rm -rf target
      success "Removed target/ ($SIZE)"
    fi
  else
    info "No target/ directory found"
  fi

  echo ""

  # Remove node_modules/
  if [[ -d "node_modules" ]]; then
    SIZE=$(du -sh node_modules 2>/dev/null | cut -f1)
    if [[ "$DRY_RUN" == true ]]; then
      info "Would remove node_modules/ ($SIZE)"
    else
      rm -rf node_modules
      success "Removed node_modules/ ($SIZE)"
    fi
  else
    info "No node_modules/ directory found"
  fi

  echo ""
fi

# =============================================================================
# SUMMARY
# =============================================================================

echo ""
header "========================================"
header "  Summary"
header "========================================"
echo ""

if [[ "$DRY_RUN" == true ]]; then
  echo "  Would clean: $cleaned_worktrees worktree(s)"
else
  echo "  Cleaned: $cleaned_worktrees worktree(s)"
fi

if [[ "$skipped_in_use" -gt 0 ]]; then
  echo "  Skipped (in use by shepherd): $skipped_in_use"
fi

if [[ "$SAFE_MODE" == true ]]; then
  echo "  Skipped (open/not merged): $((skipped_open + skipped_not_merged))"
  echo "  Skipped (grace period): $skipped_grace"
  echo "  Skipped (uncommitted): $skipped_uncommitted"
fi

if [[ "$cleaned_branches" -gt 0 || "$kept_branches" -gt 0 ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    echo "  Would delete: $cleaned_branches branch(es)"
  else
    echo "  Deleted: $cleaned_branches branch(es)"
  fi
  echo "  Kept: $kept_branches branch(es)"
fi

if [[ "$killed_tmux" -gt 0 ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    echo "  Would kill: $killed_tmux tmux session(s)"
  else
    echo "  Killed: $killed_tmux tmux session(s)"
  fi
fi

if [[ "$errors" -gt 0 ]]; then
  echo "  Errors: $errors"
fi

echo ""

if [[ "$DRY_RUN" == true ]]; then
  warning "Dry run complete - no changes made"
  info "Run without --dry-run to perform cleanup"
else
  success "Cleanup complete!"

  if [[ "$DEEP_CLEAN" == true ]]; then
    echo ""
    info "To restore dependencies, run:"
    echo "  pnpm install"
  fi
fi

echo ""
