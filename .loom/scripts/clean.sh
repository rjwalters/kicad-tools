#!/usr/bin/env bash
# Loom Cleanup - Restore repository to clean state
# Usage: ./.loom/scripts/clean.sh [--deep] [--dry-run]

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

error() {
  echo -e "${RED}✗ Error: $*${NC}" >&2
  exit 1
}

info() {
  echo -e "${BLUE}ℹ $*${NC}"
}

success() {
  echo -e "${GREEN}✓ $*${NC}"
}

warning() {
  echo -e "${YELLOW}⚠ $*${NC}"
}

header() {
  echo -e "${CYAN}$*${NC}"
}

# Find git repository root (works from any subdirectory)
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || \
  error "Not in a git repository"

# Parse arguments
DRY_RUN=false
DEEP_CLEAN=false
FORCE=false

for arg in "$@"; do
  case $arg in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --deep)
      DEEP_CLEAN=true
      shift
      ;;
    --force|-f)
      FORCE=true
      shift
      ;;
    --help|-h)
      echo "Loom Cleanup - Restore repository to clean state"
      echo ""
      echo "Usage: ./.loom/scripts/clean.sh [options]"
      echo ""
      echo "Options:"
      echo "  --dry-run    Show what would be cleaned without making changes"
      echo "  --deep       Deep clean (includes build artifacts)"
      echo "  -f, --force  Non-interactive mode (auto-confirm all prompts)"
      echo "  -h, --help   Show this help message"
      echo ""
      echo "Standard cleanup:"
      echo "  • Stale worktrees (for closed issues)"
      echo "  • Merged local branches"
      echo "  • Loom tmux sessions"
      echo ""
      echo "Deep cleanup (--deep):"
      echo "  • All of the above, plus:"
      echo "  • target/ directory (Rust build artifacts)"
      echo "  • node_modules/ directory"
      echo ""
      exit 0
      ;;
    *)
      error "Unknown option: $arg\nUse --help for usage information"
      ;;
  esac
done

# Show banner
echo ""
header "╔═══════════════════════════════════════════════════════════╗"
if [[ "$DEEP_CLEAN" == true ]]; then
  header "║                  Loom Deep Cleanup                        ║"
else
  header "║                  Loom Cleanup                             ║"
fi
if [[ "$DRY_RUN" == true ]]; then
  header "║                  (DRY RUN MODE)                           ║"
fi
header "╚═══════════════════════════════════════════════════════════╝"
echo ""

cd "$REPO_ROOT"

# Check for active worktrees
echo ""
header "Checking Active Worktrees"
echo ""

ACTIVE_WORKTREES=$(git worktree list | tail -n +2 || true)

if [[ -n "$ACTIVE_WORKTREES" ]]; then
  warning "Active worktrees detected:"
  echo "$ACTIVE_WORKTREES" | while read -r line; do
    echo "  $line"
  done
  echo ""
  info "Active worktrees will be preserved"
  SKIP_ACTIVE=true
else
  success "No active worktrees"
  SKIP_ACTIVE=false
fi

# Show what will be cleaned
echo ""
header "Cleanup Plan"
echo ""

info "Standard cleanup:"
echo "  • Orphaned worktrees (git worktree prune)"
echo "  • Merged local branches for closed issues"
echo "  • Loom tmux sessions (loom-*)"

if [[ "$DEEP_CLEAN" == true ]]; then
  echo ""
  warning "Deep cleanup additions:"
  if [[ -d "target" ]]; then
    SIZE=$(du -sh target 2>/dev/null | cut -f1)
    echo "  • target/ directory ($SIZE)"
  else
    echo "  • target/ directory (not present)"
  fi
  if [[ -d "node_modules" ]]; then
    SIZE=$(du -sh node_modules 2>/dev/null | cut -f1)
    echo "  • node_modules/ directory ($SIZE)"
  else
    echo "  • node_modules/ directory (not present)"
  fi
fi

echo ""

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

# =============================================================================
# CLEANUP: Orphaned Worktrees
# =============================================================================

header "Cleaning Orphaned Worktrees"
echo ""

# First check for stale worktrees pointing to closed issues
if [[ "$SKIP_ACTIVE" == true ]]; then
  # Get active worktree paths
  ACTIVE_PATHS=$(git worktree list | tail -n +2 | awk '{print $1}')

  # Check each .loom/worktrees/issue-* directory
  if [[ -d ".loom/worktrees" ]]; then
    for worktree_dir in .loom/worktrees/issue-*; do
      if [[ -d "$worktree_dir" ]]; then
        worktree_path="$(cd "$worktree_dir" && pwd)"

        # Check if this is an active worktree
        if echo "$ACTIVE_PATHS" | grep -q "^$worktree_path$"; then
          # Extract issue number
          issue_num=$(basename "$worktree_dir" | sed 's/issue-//')

          # Check if issue is closed
          if command -v gh &> /dev/null; then
            status=$(gh issue view "$issue_num" --json state --jq .state 2>/dev/null || echo "UNKNOWN")

            if [[ "$status" == "CLOSED" ]]; then
              warning "Worktree for closed issue #$issue_num is still active"

              if [[ "$DRY_RUN" == true ]]; then
                info "Would remove: $worktree_dir"
              elif [[ "$FORCE" == true ]]; then
                info "Auto-removing: $worktree_dir"
                git worktree remove "$worktree_path" --force && success "Removed: $worktree_dir" || warning "Failed to remove: $worktree_dir"
              else
                read -r -p "  Force remove this worktree? [y/N] " -n 1 REMOVE_WORKTREE
                echo ""

                if [[ $REMOVE_WORKTREE =~ ^[Yy]$ ]]; then
                  git worktree remove "$worktree_path" --force && success "Removed: $worktree_dir" || warning "Failed to remove: $worktree_dir"
                else
                  info "Skipping: $worktree_dir"
                  echo "  Run manually: git worktree remove $worktree_path --force"
                fi
              fi
            else
              info "Preserving active worktree for issue #$issue_num ($status)"
            fi
          fi
        fi
      fi
    done
  fi
fi

# Prune orphaned references
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

# =============================================================================
# CLEANUP: Merged Branches
# =============================================================================

header "Cleaning Merged Branches"
echo ""

# Check if cleanup-branches.sh exists (only in Loom repo, not target repos)
if [[ -f "scripts/cleanup-branches.sh" ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    ./scripts/cleanup-branches.sh --dry-run
  else
    ./scripts/cleanup-branches.sh
  fi
else
  # Manual branch cleanup for target repositories
  branches=$(git branch | grep "feature/issue-" | sed 's/^[*+ ]*//' || true)

  if [[ -z "$branches" ]]; then
    success "No feature branches found"
  else
    checked=0
    closed=0
    open=0

    for branch in $branches; do
      # Extract issue number
      issue_num=$(echo "$branch" | sed 's/feature\/issue-//' | sed 's/-.*//' | sed 's/[^0-9].*//')

      if [[ ! "$issue_num" =~ ^[0-9]+$ ]]; then
        continue
      fi

      ((checked++))

      # Check issue status
      if command -v gh &> /dev/null; then
        status=$(gh issue view "$issue_num" --json state --jq .state 2>/dev/null || echo "NOT_FOUND")

        if [[ "$status" == "CLOSED" ]]; then
          echo -e "${GREEN}✓${NC} Issue #$issue_num is CLOSED - deleting $branch"
          if [[ "$DRY_RUN" == false ]]; then
            git branch -D "$branch" 2>/dev/null
          fi
          ((closed++))
        elif [[ "$status" == "OPEN" ]]; then
          echo -e "${BLUE}○${NC} Issue #$issue_num is OPEN - keeping $branch"
          ((open++))
        fi
      fi
    done

    echo ""
    echo "Summary:"
    echo "  Checked: $checked branches"
    if [[ "$DRY_RUN" == true ]]; then
      echo -e "  ${YELLOW}Would delete${NC}: $closed (closed issues)"
    else
      echo -e "  ${GREEN}Deleted${NC}: $closed (closed issues)"
    fi
    echo -e "  ${BLUE}Kept${NC}:    $open (open issues)"
  fi
fi

echo ""

# =============================================================================
# CLEANUP: Tmux Sessions
# =============================================================================

header "Cleaning Loom Tmux Sessions"
echo ""

LOOM_SESSIONS=$(tmux list-sessions 2>/dev/null | grep '^loom-' | cut -d: -f1 || true)

if [[ -n "$LOOM_SESSIONS" ]]; then
  echo "Found Loom tmux sessions:"
  echo "$LOOM_SESSIONS" | while read -r session; do
    echo "  • $session"
  done
  echo ""

  if [[ "$DRY_RUN" == true ]]; then
    info "Would kill these sessions"
  else
    echo "$LOOM_SESSIONS" | while read -r session; do
      tmux kill-session -t "$session" 2>/dev/null && success "Killed: $session"
    done
  fi
else
  success "No Loom tmux sessions found"
fi

echo ""

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
header "═══════════════════════════════════════════════════════════"
echo ""

if [[ "$DRY_RUN" == true ]]; then
  success "Dry run complete - no changes made"
  echo ""
  info "To actually clean, run: ./.loom/scripts/clean.sh"
  if [[ "$DEEP_CLEAN" == true ]]; then
    echo "                        ./.loom/scripts/clean.sh --deep"
  fi
else
  success "Cleanup complete!"
  echo ""

  if [[ "$DEEP_CLEAN" == true ]]; then
    info "To restore dependencies, run:"
    echo "  pnpm install"
  fi
fi

echo ""
