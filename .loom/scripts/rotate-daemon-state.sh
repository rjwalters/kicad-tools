#!/usr/bin/env bash
# Rotate daemon state files to preserve session history
# Usage: ./scripts/rotate-daemon-state.sh [options]
#
# This script rotates daemon-state.json files at daemon startup to preserve
# session history for debugging, analysis, and crash recovery.

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

# Detect repository root
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || error "Not in a git repository"

# Configuration
LOOM_DIR="$REPO_ROOT/.loom"
STATE_FILE="$LOOM_DIR/daemon-state.json"
MAX_ARCHIVED_SESSIONS="${LOOM_MAX_ARCHIVED_SESSIONS:-10}"
DRY_RUN=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --max-sessions)
      MAX_ARCHIVED_SESSIONS="$2"
      shift 2
      ;;
    --help|-h)
      cat <<EOF
Rotate daemon state files to preserve session history

Usage: ./scripts/rotate-daemon-state.sh [options]

Options:
  --dry-run               Show what would be done without making changes
  --max-sessions <N>      Maximum archived sessions to keep (default: 10)
  -h, --help              Show this help message

Archive Format:
  .loom/
  ├── daemon-state.json           # Current session (always this name)
  ├── 00-daemon-state.json        # First archived session
  ├── 01-daemon-state.json        # Second archived session
  └── ...

The script:
  1. Checks if daemon-state.json exists
  2. If it exists and has meaningful content, rotates it to NN-daemon-state.json
  3. Enforces MAX_ARCHIVED_SESSIONS limit by deleting oldest archives
  4. Creates a fresh daemon-state.json for the new session

Environment Variables:
  LOOM_MAX_ARCHIVED_SESSIONS    Maximum sessions to keep (default: 10)

Examples:
  # Rotate state at daemon startup
  ./scripts/rotate-daemon-state.sh

  # Preview rotation without making changes
  ./scripts/rotate-daemon-state.sh --dry-run

  # Keep more archived sessions
  ./scripts/rotate-daemon-state.sh --max-sessions 20
EOF
      exit 0
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# Ensure .loom directory exists
if [[ ! -d "$LOOM_DIR" ]]; then
  if [[ "$DRY_RUN" == true ]]; then
    info "[DRY-RUN] Would create $LOOM_DIR"
  else
    mkdir -p "$LOOM_DIR"
  fi
fi

# Check if current state file exists
if [[ ! -f "$STATE_FILE" ]]; then
  info "No existing daemon-state.json found, nothing to rotate"
  exit 0
fi

# Check if the state file has meaningful content (not just empty or minimal)
state_size=$(wc -c < "$STATE_FILE" | tr -d ' ')
if [[ "$state_size" -lt 50 ]]; then
  info "State file too small ($state_size bytes), skipping rotation"
  exit 0
fi

# Check if it was an incomplete/crashed session (never got iteration > 0)
iteration=$(jq -r '.iteration // 0' "$STATE_FILE" 2>/dev/null || echo "0")
if [[ "$iteration" -eq 0 ]]; then
  # Check if there's any useful data
  has_shepherds=$(jq -r '[.shepherds // {} | to_entries[] | select(.value.issue != null)] | length' "$STATE_FILE" 2>/dev/null || echo "0")
  has_completed=$(jq -r '.completed_issues // [] | length' "$STATE_FILE" 2>/dev/null || echo "0")

  if [[ "$has_shepherds" -eq 0 && "$has_completed" -eq 0 ]]; then
    info "State file has no useful data (iteration=0, no work done), skipping rotation"
    exit 0
  fi
fi

# Find next available session number
find_next_session_number() {
  local session_num=0
  while [[ -f "$LOOM_DIR/$(printf '%02d' $session_num)-daemon-state.json" ]]; do
    ((session_num++))
    if [[ $session_num -ge 100 ]]; then
      warning "Maximum session number reached (100), will wrap around"
      # Find lowest numbered existing archive and overwrite it
      session_num=0
      break
    fi
  done
  echo "$session_num"
}

# Prune old sessions to enforce MAX_ARCHIVED_SESSIONS
prune_old_sessions() {
  # Get list of archived sessions sorted by number (oldest first)
  local archives
  archives=$(find "$LOOM_DIR" -maxdepth 1 -name '[0-9][0-9]-daemon-state.json' | sort)

  local archive_count
  archive_count=$(echo "$archives" | grep -c . || echo "0")

  # Calculate how many to delete (if we're at the limit, delete 1 to make room)
  local to_delete=$((archive_count - MAX_ARCHIVED_SESSIONS + 1))

  if [[ $to_delete -le 0 ]]; then
    return 0
  fi

  info "Pruning $to_delete old session(s) to maintain limit of $MAX_ARCHIVED_SESSIONS..."

  # Delete oldest archives
  local deleted=0
  for archive in $archives; do
    if [[ $deleted -ge $to_delete ]]; then
      break
    fi

    local basename
    basename=$(basename "$archive")

    if [[ "$DRY_RUN" == true ]]; then
      info "[DRY-RUN] Would delete: $basename"
    else
      rm -f "$archive"
      info "Deleted old archive: $basename"
    fi

    ((deleted++))
  done
}

# Add session summary to state before archiving
add_session_summary() {
  local state_file="$1"
  local session_num="$2"
  local timestamp
  timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  # Calculate session summary
  local completed_count
  completed_count=$(jq -r '.completed_issues // [] | length' "$state_file" 2>/dev/null || echo "0")

  local prs_merged
  prs_merged=$(jq -r '.total_prs_merged // 0' "$state_file" 2>/dev/null || echo "0")

  local final_iteration
  final_iteration=$(jq -r '.iteration // 0' "$state_file" 2>/dev/null || echo "0")

  # Add session summary to the state
  if [[ "$DRY_RUN" != true ]]; then
    jq --arg session "$session_num" \
       --arg archived "$timestamp" \
       --arg completed "$completed_count" \
       --arg merged "$prs_merged" \
       --arg iterations "$final_iteration" \
       '.session_summary = {
          session_id: ($session | tonumber),
          archived_at: $archived,
          issues_completed: ($completed | tonumber),
          prs_merged: ($merged | tonumber),
          total_iterations: ($iterations | tonumber)
        }' "$state_file" > "${state_file}.tmp" && mv "${state_file}.tmp" "$state_file"
  fi
}

# Main rotation logic
info "Rotating daemon state files..."

# Find next session number
session_num=$(find_next_session_number)
archive_name=$(printf '%02d' $session_num)-daemon-state.json
archive_path="$LOOM_DIR/$archive_name"

info "Current state will be archived as: $archive_name"

# Prune old sessions first to make room
prune_old_sessions

# Add session summary before archiving
if [[ "$DRY_RUN" == true ]]; then
  info "[DRY-RUN] Would add session summary to state"
else
  add_session_summary "$STATE_FILE" "$session_num"
fi

# Rotate the state file
if [[ "$DRY_RUN" == true ]]; then
  info "[DRY-RUN] Would rename: daemon-state.json -> $archive_name"
else
  mv "$STATE_FILE" "$archive_path"
  info "Archived: daemon-state.json -> $archive_name"
fi

# Report summary
if [[ "$DRY_RUN" == true ]]; then
  info "[DRY-RUN] Rotation complete (no changes made)"
else
  success "Session rotation complete"
  info "  Archived session: $archive_name"
  info "  Ready for new daemon session"
fi

# Count current archives
archive_count=$(find "$LOOM_DIR" -maxdepth 1 -name '[0-9][0-9]-daemon-state.json' 2>/dev/null | wc -l | tr -d ' ')
info "  Total archived sessions: $archive_count / $MAX_ARCHIVED_SESSIONS max"
