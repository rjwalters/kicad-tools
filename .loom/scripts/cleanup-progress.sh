#!/usr/bin/env bash
# Clean up orphaned shepherd progress files in .loom/progress/
#
# Progress files can become orphaned when shepherds crash or daemon sessions
# end abruptly. This script provides manual cleanup capabilities.
#
# Usage:
#   ./cleanup-progress.sh                    # List all progress files (dry run)
#   ./cleanup-progress.sh --stale            # Remove stale working files for closed issues
#   ./cleanup-progress.sh --older 24         # Remove files with heartbeat older than N hours
#   ./cleanup-progress.sh --all              # Remove all progress files
#   ./cleanup-progress.sh --dry-run --stale  # Preview stale cleanup
#   ./cleanup-progress.sh --json             # Output JSON for programmatic use

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

# Detect repository root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || error "Not in a git repository"
PROGRESS_DIR="$REPO_ROOT/.loom/progress"

# Parse arguments
DRY_RUN=false
MODE=""  # list, stale, older, all
OLDER_HOURS=""
JSON_OUTPUT=false

for arg in "$@"; do
  if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
    cat <<EOF
Clean up orphaned shepherd progress files

Usage: ./scripts/cleanup-progress.sh [options]

Modes (mutually exclusive):
  --stale              Remove stale working files for closed issues
  --older <hours>      Remove files with heartbeat older than N hours
  --all                Remove all progress files

Options:
  --dry-run            Show what would be cleaned without deleting
  --json               Output JSON for programmatic use
  -h, --help           Show this help message

Without a mode flag, lists all progress files (read-only).

Examples:
  ./scripts/cleanup-progress.sh                    # List all progress files
  ./scripts/cleanup-progress.sh --stale            # Remove orphaned files
  ./scripts/cleanup-progress.sh --older 12         # Remove files older than 12h
  ./scripts/cleanup-progress.sh --dry-run --stale  # Preview stale cleanup
  ./scripts/cleanup-progress.sh --json             # JSON listing
EOF
    exit 0
  fi
done

while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --stale)
      MODE="stale"
      shift
      ;;
    --older)
      MODE="older"
      OLDER_HOURS="${2:-}"
      if [[ -z "$OLDER_HOURS" ]]; then
        error "--older requires an hours argument"
      fi
      shift 2
      ;;
    --all)
      MODE="all"
      shift
      ;;
    --json)
      JSON_OUTPUT=true
      shift
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# Default to list mode
if [[ -z "$MODE" ]]; then
  MODE="list"
fi

# Check progress directory exists
if [[ ! -d "$PROGRESS_DIR" ]]; then
  if [[ "$JSON_OUTPUT" == true ]]; then
    echo '{"files":[],"message":"No progress directory found"}'
  else
    info "No progress directory found at $PROGRESS_DIR"
  fi
  exit 0
fi

# Get current epoch for age calculations
now_epoch=$(date +%s)

# Helper: convert ISO timestamp to epoch
iso_to_epoch() {
  local ts="$1"
  if [[ -z "$ts" || "$ts" == "null" ]]; then
    echo "0"
    return
  fi
  if [[ "$(uname)" == "Darwin" ]]; then
    date -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" "+%s" 2>/dev/null || echo "0"
  else
    date -d "$ts" "+%s" 2>/dev/null || echo "0"
  fi
}

# Collect file info
declare -a file_entries=()
deleted_count=0
total_count=0

for progress_file in "$PROGRESS_DIR"/shepherd-*.json; do
  if [[ ! -f "$progress_file" ]]; then
    continue
  fi

  total_count=$((total_count + 1))
  local_basename=$(basename "$progress_file")

  # Extract fields
  file_issue=$(jq -r '.issue // 0' "$progress_file" 2>/dev/null || echo "0")
  file_status=$(jq -r '.status // "unknown"' "$progress_file" 2>/dev/null || echo "unknown")
  file_task_id=$(jq -r '.task_id // "unknown"' "$progress_file" 2>/dev/null || echo "unknown")
  last_heartbeat=$(jq -r '.last_heartbeat // ""' "$progress_file" 2>/dev/null || echo "")

  # Calculate age
  hb_epoch=$(iso_to_epoch "$last_heartbeat")
  if [[ "$hb_epoch" -gt 0 ]]; then
    age_hours=$(( (now_epoch - hb_epoch) / 3600 ))
  else
    age_hours=-1  # unknown
  fi

  should_delete=false

  case "$MODE" in
    list)
      # Just display, don't delete
      ;;
    stale)
      # Delete working files for closed issues
      if [[ "$file_status" == "working" && "$file_issue" != "0" ]]; then
        issue_state=$(gh issue view "$file_issue" --json state --jq '.state' 2>/dev/null || echo "unknown")
        if [[ "$issue_state" == "CLOSED" ]]; then
          should_delete=true
        fi
      fi
      # Also delete completed/errored/blocked files unconditionally
      if [[ "$file_status" != "working" && "$file_status" != "unknown" ]]; then
        should_delete=true
      fi
      ;;
    older)
      # Delete files older than threshold
      if [[ "$age_hours" -ge 0 && "$age_hours" -ge "$OLDER_HOURS" ]]; then
        should_delete=true
      fi
      ;;
    all)
      should_delete=true
      ;;
  esac

  if [[ "$JSON_OUTPUT" == true ]]; then
    file_entries+=("{\"file\":\"$local_basename\",\"issue\":$file_issue,\"status\":\"$file_status\",\"task_id\":\"$file_task_id\",\"age_hours\":$age_hours,\"action\":$(if $should_delete; then echo '\"delete\"'; else echo '\"keep\"'; fi)}")
  elif [[ "$MODE" == "list" ]]; then
    age_display="unknown"
    if [[ "$age_hours" -ge 0 ]]; then
      age_display="${age_hours}h"
    fi
    echo -e "  ${CYAN}$local_basename${NC}  issue=#$file_issue  status=$file_status  age=$age_display  task=$file_task_id"
  fi

  if [[ "$should_delete" == true ]]; then
    if [[ "$DRY_RUN" == true ]]; then
      if [[ "$JSON_OUTPUT" != true ]]; then
        info "[DRY-RUN] Would delete: $local_basename (issue #$file_issue, status: $file_status)"
      fi
    else
      rm -f "$progress_file"
      if [[ "$JSON_OUTPUT" != true ]]; then
        success "Deleted: $local_basename (issue #$file_issue, status: $file_status)"
      fi
    fi
    deleted_count=$((deleted_count + 1))
  fi
done

# Output
if [[ "$JSON_OUTPUT" == true ]]; then
  # Join array entries with commas
  joined=""
  for entry in "${file_entries[@]+"${file_entries[@]}"}"; do
    if [[ -n "$joined" ]]; then
      joined="$joined,$entry"
    else
      joined="$entry"
    fi
  done
  echo "{\"total\":$total_count,\"action_count\":$deleted_count,\"dry_run\":$DRY_RUN,\"mode\":\"$MODE\",\"files\":[$joined]}"
else
  echo ""
  if [[ "$MODE" == "list" ]]; then
    info "Found $total_count progress file(s)"
  elif [[ "$DRY_RUN" == true ]]; then
    info "Would delete $deleted_count of $total_count file(s) [dry-run]"
  else
    success "Deleted $deleted_count of $total_count file(s)"
  fi
fi
