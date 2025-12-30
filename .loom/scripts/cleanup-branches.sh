#!/bin/bash
# Loom Branch Cleanup Script - Remove feature branches for closed issues
#
# This script automatically cleans up stale feature branches by checking
# which issues have been closed and deleting their corresponding branches.
#
# Usage:
#   ./scripts/cleanup-branches.sh [--dry-run]
#
# Options:
#   --dry-run    Show what would be deleted without actually deleting
#
# Safety:
#   - Only deletes branches for confirmed CLOSED issues
#   - Preserves branches for OPEN issues
#   - Provides summary of actions taken

set -e  # Exit on error

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse arguments
DRY_RUN=false
if [[ "$1" == "--dry-run" ]]; then
  DRY_RUN=true
  echo -e "${YELLOW}🔍 DRY RUN MODE - No branches will be deleted${NC}"
  echo
fi

# Get all feature branches
branches=$(git branch | grep "feature/issue-" | sed 's/^[*+ ]*//' || true)

if [ -z "$branches" ]; then
  echo "No feature branches found matching pattern 'feature/issue-*'"
  exit 0
fi

# Track stats
checked=0
closed=0
open=0
errors=0

echo "Checking branch status..."
echo

for branch in $branches; do
    # Extract issue number (handle branches like feature/issue-123 or feature/issue-123-description)
    issue_num=$(echo "$branch" | sed 's/feature\/issue-//' | sed 's/-.*//' | sed 's/[^0-9].*//')

    # Skip if we couldn't extract a valid number
    if [[ ! "$issue_num" =~ ^[0-9]+$ ]]; then
        echo -e "${YELLOW}? Skipping $branch (couldn't extract issue number)${NC}"
        continue
    fi

    ((checked++))

    # Check issue status
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
    else
        echo -e "${YELLOW}?${NC} Issue #$issue_num not found - keeping $branch"
        ((errors++))
    fi
done

echo
echo "Summary:"
echo "  Checked: $checked branches"
if [[ "$DRY_RUN" == true ]]; then
    echo -e "  ${YELLOW}Would delete${NC}: $closed (closed issues)"
else
    echo -e "  ${GREEN}Deleted${NC}: $closed (closed issues)"
fi
echo -e "  ${BLUE}Kept${NC}:    $open (open issues)"
if [[ $errors -gt 0 ]]; then
    echo -e "  ${YELLOW}Errors${NC}:  $errors (issue not found)"
fi

if [[ "$DRY_RUN" == true ]]; then
    echo
    echo "To actually delete branches, run without --dry-run flag"
fi
