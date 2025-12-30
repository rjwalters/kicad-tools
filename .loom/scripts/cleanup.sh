#!/bin/bash
# Loom Cleanup Script - Remove build artifacts and orphaned worktrees
#
# AGENT USAGE INSTRUCTIONS:
#   This script cleans up Loom build artifacts and orphaned worktrees.
#
#   Non-interactive mode (for Claude Code):
#     ./scripts/cleanup.sh --yes
#     ./scripts/cleanup.sh -y
#
#   Interactive mode (prompts for confirmation):
#     ./scripts/cleanup.sh
#
#   What this script does:
#     1. Removes target/ directory (Rust build artifacts)
#     2. Removes node_modules/ directory (Node dependencies)
#     3. Detects worktrees for closed issues and offers to remove them
#     4. Prunes orphaned git worktrees (with confirmation unless --yes)
#
#   After running, restore dependencies with: pnpm install

set -e  # Exit on error

# Parse command line arguments
NON_INTERACTIVE=false
for arg in "$@"; do
  case $arg in
    -y|--yes)
      NON_INTERACTIVE=true
      shift
      ;;
  esac
done

echo "🧹 Loom Cleanup"
echo ""

# Track if we're in main workspace
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Clean Rust build artifacts
if [ -d "$PROJECT_ROOT/target" ]; then
  SIZE=$(du -sh "$PROJECT_ROOT/target" 2>/dev/null | cut -f1 || echo "unknown")
  echo "Removing target/ ($SIZE)"
  rm -rf "$PROJECT_ROOT/target"
  echo "✓ Removed target/"
else
  echo "ℹ No target/ directory found"
fi

echo ""

# Clean node_modules
if [ -d "$PROJECT_ROOT/node_modules" ]; then
  SIZE=$(du -sh "$PROJECT_ROOT/node_modules" 2>/dev/null | cut -f1 || echo "unknown")
  echo "Removing node_modules/ ($SIZE)"
  rm -rf "$PROJECT_ROOT/node_modules"
  echo "✓ Removed node_modules/"
else
  echo "ℹ No node_modules/ directory found"
fi

echo ""

# Check for worktrees associated with closed issues
echo "Checking for worktrees associated with closed issues..."
cd "$PROJECT_ROOT"

CLOSED_ISSUE_WORKTREES=()

# List all worktrees and check if their issues are closed
while IFS= read -r worktree_line; do
  # Extract worktree path (format: /path/to/worktree COMMIT [branch-name])
  worktree_path=$(echo "$worktree_line" | awk '{print $1}')

  # Skip the main worktree
  if [[ "$worktree_path" == "$PROJECT_ROOT" ]]; then
    continue
  fi

  # Extract issue number from path (e.g., .loom/worktrees/issue-123)
  if [[ "$worktree_path" =~ issue-([0-9]+) ]]; then
    issue_num="${BASH_REMATCH[1]}"

    # Check if issue is closed using gh
    if command -v gh &> /dev/null; then
      issue_state=$(gh issue view "$issue_num" --json state --jq .state 2>/dev/null || echo "")

      if [[ "$issue_state" == "CLOSED" ]]; then
        CLOSED_ISSUE_WORKTREES+=("$worktree_path:$issue_num")
        echo "⚠ Worktree for closed issue #$issue_num is still active"
        echo "ℹ Path: $worktree_path"
      fi
    fi
  fi
done < <(git worktree list --porcelain | grep "worktree " | sed 's/worktree //')

if [[ ${#CLOSED_ISSUE_WORKTREES[@]} -gt 0 ]]; then
  echo ""

  # Auto-remove in non-interactive mode
  if [ "$NON_INTERACTIVE" = true ]; then
    echo "Non-interactive mode: automatically removing closed issue worktrees"
    for entry in "${CLOSED_ISSUE_WORKTREES[@]}"; do
      worktree_path="${entry%%:*}"
      issue_num="${entry##*:}"
      echo "Removing worktree for closed issue #$issue_num..."
      git worktree remove "$worktree_path" --force
      echo "✓ Removed: $worktree_path"
    done
    echo "✓ Removed ${#CLOSED_ISSUE_WORKTREES[@]} closed issue worktree(s)"
  else
    echo "Found ${#CLOSED_ISSUE_WORKTREES[@]} worktree(s) for closed issues."
    read -p "Force remove all closed issue worktrees? (y/N) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
      for entry in "${CLOSED_ISSUE_WORKTREES[@]}"; do
        worktree_path="${entry%%:*}"
        issue_num="${entry##*:}"
        echo "Removing worktree for closed issue #$issue_num..."
        git worktree remove "$worktree_path" --force
        echo "✓ Removed: $worktree_path"
      done
      echo "✓ Removed ${#CLOSED_ISSUE_WORKTREES[@]} closed issue worktree(s)"
    else
      echo "ℹ Skipped closed issue worktree cleanup"
      echo "ℹ To remove individually:"
      for entry in "${CLOSED_ISSUE_WORKTREES[@]}"; do
        worktree_path="${entry%%:*}"
        issue_num="${entry##*:}"
        echo "  git worktree remove $worktree_path --force  # issue #$issue_num"
      done
    fi
  fi
else
  echo "✓ No worktrees found for closed issues"
fi

echo ""

# Clean orphaned worktrees
echo "Checking for orphaned worktrees..."

# Show what would be pruned
PRUNE_OUTPUT=$(git worktree prune --dry-run --verbose 2>&1 || true)

if [ -n "$PRUNE_OUTPUT" ]; then
  echo "$PRUNE_OUTPUT"
  echo ""

  # Auto-confirm in non-interactive mode
  if [ "$NON_INTERACTIVE" = true ]; then
    echo "Non-interactive mode: automatically removing orphaned worktrees"
    git worktree prune --verbose
    echo "✓ Orphaned worktrees removed"
  else
    read -p "Remove orphaned worktrees? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
      git worktree prune --verbose
      echo "✓ Orphaned worktrees removed"
    else
      echo "ℹ Skipped worktree cleanup"
    fi
  fi
else
  echo "✓ No orphaned worktrees found"
fi

echo ""
echo "✅ Cleanup complete!"
echo ""
echo "To restore dependencies, run:"
echo "  pnpm install"
