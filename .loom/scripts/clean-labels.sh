#!/usr/bin/env bash
# Loom Label Cleanup - Remove workflow labels from closed issues
# Usage: ./.loom/scripts/clean-labels.sh [options]
#
# Removes workflow labels (loom:building, loom:curated, etc.) from closed issues.
# These labels are normally cleaned up by merge-pr.sh, but this script handles
# cases where issues were closed manually or the cleanup was missed.
#
# Note: Stale labels on closed issues are mostly cosmetic - agents should always
# verify issue state is OPEN before working on it.

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

# Configuration
DRY_RUN=false
FORCE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --force|-f|--yes|-y)
      FORCE=true
      shift
      ;;
    --help|-h)
      cat <<EOF
Loom Label Cleanup - Remove workflow labels from closed issues

Usage: ./.loom/scripts/clean-labels.sh [options]

Options:
  --dry-run              Show what would be cleaned without making changes
  -f, --force, -y, --yes Non-interactive mode (auto-confirm)
  -h, --help             Show this help message

Labels cleaned:
  - loom:building   (work-in-progress indicator)
  - loom:issue      (ready-for-work indicator)
  - loom:curated    (pre-approval indicator)
  - loom:curating   (work-in-progress indicator)
  - loom:treating   (work-in-progress indicator)
  - loom:blocked    (status indicator)

Examples:
  ./.loom/scripts/clean-labels.sh              # Interactive cleanup
  ./.loom/scripts/clean-labels.sh --dry-run    # Preview what would be cleaned
  ./.loom/scripts/clean-labels.sh --force      # Non-interactive cleanup

Note: Labels are normally cleaned by merge-pr.sh when PRs are merged.
This script is for manual cleanup of edge cases.
EOF
      exit 0
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# Check for gh CLI
if ! command -v gh &> /dev/null; then
  error "GitHub CLI (gh) is required but not installed"
fi

echo ""
info "Loom Label Cleanup"
info "=================="
echo ""

if [[ "$DRY_RUN" == true ]]; then
  warning "DRY RUN - No changes will be made"
  echo ""
fi

# Confirmation
if [[ "$DRY_RUN" != true && "$FORCE" != true ]]; then
  read -r -p "Clean workflow labels from closed issues? [y/N] " -n 1 CONFIRM
  echo ""
  if [[ ! $CONFIRM =~ ^[Yy]$ ]]; then
    info "Cleanup cancelled"
    exit 0
  fi
  echo ""
fi

# Define workflow labels to remove from closed issues
WORKFLOW_LABELS=("loom:building" "loom:issue" "loom:curated" "loom:curating" "loom:treating" "loom:blocked")

cleaned_count=0
error_count=0

# Check each workflow label for closed issues
for label in "${WORKFLOW_LABELS[@]}"; do
  # Find closed issues with this label
  closed_with_label=$(gh issue list --state closed --label "$label" --json number --jq '.[].number' 2>/dev/null || true)

  if [[ -n "$closed_with_label" ]]; then
    for issue_num in $closed_with_label; do
      if [[ "$DRY_RUN" == true ]]; then
        info "Would remove '$label' from closed issue #$issue_num"
        ((cleaned_count++)) || true
      else
        if gh issue edit "$issue_num" --remove-label "$label" 2>/dev/null; then
          success "Removed '$label' from closed issue #$issue_num"
          ((cleaned_count++)) || true
        else
          warning "Failed to remove '$label' from issue #$issue_num"
          ((error_count++)) || true
        fi
      fi
    done
  fi
done

echo ""
info "=================="

if [[ "$cleaned_count" -eq 0 ]]; then
  success "No workflow labels to clean on closed issues"
else
  if [[ "$DRY_RUN" == true ]]; then
    echo "Would clean: $cleaned_count label(s)"
  else
    echo "Cleaned: $cleaned_count label(s)"
  fi
fi

if [[ "$error_count" -gt 0 ]]; then
  echo "Errors: $error_count"
fi

echo ""
