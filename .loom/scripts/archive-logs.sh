#!/usr/bin/env bash
# Archive task outputs and daemon logs with retention policy
# Usage: ./scripts/archive-logs.sh [--dry-run] [--prune-only] [--retention-days N]
#
# Archives task output files to .loom/logs/{date}/ before deletion.
# Prunes archives older than retention period (default: 7 days).

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
RETENTION_DAYS=7
DRY_RUN=false
PRUNE_ONLY=false

# Detect repository root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || error "Not in a git repository"

# Archive directory
ARCHIVE_DIR="$REPO_ROOT/.loom/logs"

# Task output locations (Claude Code uses these)
TASK_OUTPUT_DIR="/tmp/claude"
ALTERNATIVE_TASK_DIR="/private/tmp/claude"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --prune-only)
      PRUNE_ONLY=true
      shift
      ;;
    --retention-days)
      RETENTION_DAYS="$2"
      shift 2
      ;;
    --help|-h)
      cat <<EOF
Archive task outputs and daemon logs with retention policy

Usage: ./scripts/archive-logs.sh [options]

Options:
  --dry-run           Show what would be archived/pruned without making changes
  --prune-only        Only prune old archives, don't archive new files
  --retention-days N  Days to retain archives (default: 7)
  -h, --help          Show this help message

Archive Structure:
  .loom/logs/
  +-- 2026-01-23/
  |   +-- issue-123-shepherd.log
  |   +-- issue-124-builder.log
  |   +-- daemon-2026-01-23T10-00-00Z.log
  +-- 2026-01-22/
      +-- ...

What Gets Archived:
  - Task output files from /tmp/claude/.../tasks/*.output
  - Daemon state snapshots (on shutdown)

What Gets Pruned:
  - Archive directories older than retention period
EOF
      exit 0
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# Functions
archive_task_outputs() {
  header "Archiving Task Outputs"
  echo ""

  local today=$(date +%Y-%m-%d)
  local archive_subdir="$ARCHIVE_DIR/$today"
  local archived=0
  local skipped=0

  # Find task output directories
  for base_dir in "$TASK_OUTPUT_DIR" "$ALTERNATIVE_TASK_DIR"; do
    if [[ ! -d "$base_dir" ]]; then
      continue
    fi

    # Look for workspace-specific task directories
    # Pattern: /tmp/claude/-Users-name-GitHub-repo/tasks/*.output
    find "$base_dir" -type d -name "tasks" 2>/dev/null | while read -r task_dir; do
      # Find output files - use nullglob to handle no matches
      shopt -s nullglob
      for output_file in "$task_dir"/*.output; do
        if [[ ! -f "$output_file" ]]; then
          continue
        fi

        local filename=$(basename "$output_file")
        local task_id="${filename%.output}"

        # Try to extract issue number from file content or filename
        local issue_num=""
        if [[ -f "$output_file" ]]; then
          # Look for issue number patterns in the output
          issue_num=$(grep -oE 'issue[- ]?#?([0-9]+)' "$output_file" 2>/dev/null | head -1 | grep -oE '[0-9]+' || true)
        fi

        # Create archive filename
        local archive_name
        if [[ -n "$issue_num" ]]; then
          archive_name="issue-${issue_num}-${task_id}.log"
        else
          archive_name="task-${task_id}.log"
        fi

        if [[ "$DRY_RUN" == true ]]; then
          info "Would archive: $output_file -> $archive_subdir/$archive_name"
          ((archived++)) || true
        else
          # Create archive directory
          mkdir -p "$archive_subdir"

          # Copy with metadata preservation
          cp -p "$output_file" "$archive_subdir/$archive_name"

          # Remove original
          rm "$output_file"

          success "Archived: $archive_name"
          ((archived++)) || true
        fi
      done
    done
  done

  if [[ $archived -eq 0 ]]; then
    info "No task outputs found to archive"
  else
    if [[ "$DRY_RUN" == true ]]; then
      info "Would archive $archived file(s)"
    else
      success "Archived $archived file(s) to $archive_subdir"
    fi
  fi

  echo ""
}

prune_old_archives() {
  header "Pruning Old Archives"
  echo ""

  if [[ ! -d "$ARCHIVE_DIR" ]]; then
    info "No archive directory found"
    echo ""
    return
  fi

  local cutoff_date=$(date -v-${RETENTION_DAYS}d +%Y-%m-%d 2>/dev/null || date -d "-${RETENTION_DAYS} days" +%Y-%m-%d)
  local pruned=0

  # Find directories matching date pattern
  for date_dir in "$ARCHIVE_DIR"/????-??-??; do
    if [[ ! -d "$date_dir" ]]; then
      continue
    fi

    local dir_date=$(basename "$date_dir")

    # Compare dates (lexicographic comparison works for YYYY-MM-DD format)
    if [[ "$dir_date" < "$cutoff_date" ]]; then
      if [[ "$DRY_RUN" == true ]]; then
        info "Would prune: $date_dir"
        ((pruned++)) || true
      else
        rm -rf "$date_dir"
        success "Pruned: $date_dir"
        ((pruned++)) || true
      fi
    fi
  done

  if [[ $pruned -eq 0 ]]; then
    info "No archives older than $RETENTION_DAYS days"
  else
    if [[ "$DRY_RUN" == true ]]; then
      info "Would prune $pruned archive(s)"
    else
      success "Pruned $pruned archive(s)"
    fi
  fi

  echo ""
}

archive_daemon_state() {
  header "Archiving Daemon State"
  echo ""

  local daemon_state="$REPO_ROOT/.loom/daemon-state.json"

  if [[ ! -f "$daemon_state" ]]; then
    info "No daemon state file found"
    echo ""
    return
  fi

  # Check if daemon is stopped (we only archive stopped daemon states)
  local running=$(jq -r '.running // true' "$daemon_state" 2>/dev/null || echo "true")

  if [[ "$running" == "true" ]]; then
    info "Daemon is running, skipping state archive"
    echo ""
    return
  fi

  local today=$(date +%Y-%m-%d)
  local timestamp=$(date +%Y-%m-%dT%H-%M-%SZ)
  local archive_subdir="$ARCHIVE_DIR/$today"
  local archive_name="daemon-state-${timestamp}.json"

  if [[ "$DRY_RUN" == true ]]; then
    info "Would archive: $daemon_state -> $archive_subdir/$archive_name"
  else
    mkdir -p "$archive_subdir"
    cp -p "$daemon_state" "$archive_subdir/$archive_name"
    success "Archived daemon state: $archive_name"
  fi

  echo ""
}

show_archive_stats() {
  header "Archive Statistics"
  echo ""

  if [[ ! -d "$ARCHIVE_DIR" ]]; then
    info "No archives found"
    return
  fi

  local total_size=$(du -sh "$ARCHIVE_DIR" 2>/dev/null | cut -f1 || echo "0")
  local dir_count=$(find "$ARCHIVE_DIR" -maxdepth 1 -type d -name "????-??-??" 2>/dev/null | wc -l | tr -d ' ')
  local file_count=$(find "$ARCHIVE_DIR" -type f 2>/dev/null | wc -l | tr -d ' ')

  echo "  Archive location: $ARCHIVE_DIR"
  echo "  Total size: $total_size"
  echo "  Date directories: $dir_count"
  echo "  Total files: $file_count"
  echo "  Retention policy: $RETENTION_DAYS days"
  echo ""
}

# Main
header "================================="
header "  Loom Log Archiver"
if [[ "$DRY_RUN" == true ]]; then
  header "  (DRY RUN MODE)"
fi
header "================================="
echo ""

if [[ "$PRUNE_ONLY" == true ]]; then
  prune_old_archives
else
  archive_task_outputs
  archive_daemon_state
  prune_old_archives
fi

show_archive_stats

if [[ "$DRY_RUN" == true ]]; then
  warning "Dry run complete - no changes made"
  info "Run without --dry-run to archive and prune"
else
  success "Archive complete!"
fi
