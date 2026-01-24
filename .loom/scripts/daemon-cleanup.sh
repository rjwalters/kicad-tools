#!/usr/bin/env bash
# Daemon cleanup integration - event-driven cleanup for the Loom daemon
# Usage: ./scripts/daemon-cleanup.sh <event> [options]
#
# Events:
#   shepherd-complete <issue-number> - Cleanup after shepherd finishes an issue
#   daemon-startup                   - Cleanup stale artifacts from previous session
#   daemon-shutdown                  - Archive logs and cleanup before exit
#   periodic                         - Conservative periodic cleanup

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

# Paths
DAEMON_STATE="$REPO_ROOT/.loom/daemon-state.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Configuration (can be overridden via environment or daemon-state.json)
CLEANUP_ENABLED="${LOOM_CLEANUP_ENABLED:-true}"
ARCHIVE_LOGS="${LOOM_ARCHIVE_LOGS:-true}"
RETENTION_DAYS="${LOOM_RETENTION_DAYS:-7}"
PERIODIC_INTERVAL_MINUTES="${LOOM_CLEANUP_INTERVAL:-360}"  # 6 hours
GRACE_PERIOD="${LOOM_GRACE_PERIOD:-600}"  # 10 minutes

# Check for help first
for arg in "$@"; do
  if [[ "$arg" == "--help" || "$arg" == "-h" ]]; then
    cat <<EOF
Daemon cleanup integration - event-driven cleanup for the Loom daemon

Usage: ./scripts/daemon-cleanup.sh <event> [options]

Events:
  shepherd-complete <issue>   Cleanup after shepherd finishes an issue
  daemon-startup              Cleanup stale artifacts from previous session
  daemon-shutdown             Archive logs and cleanup before exit
  periodic                    Conservative periodic cleanup

Options:
  --dry-run                   Show what would be cleaned
  --issue <number>            Issue number (for shepherd-complete)
  -h, --help                  Show this help message

Environment Variables:
  LOOM_CLEANUP_ENABLED        Enable/disable cleanup (default: true)
  LOOM_ARCHIVE_LOGS           Archive logs before deletion (default: true)
  LOOM_RETENTION_DAYS         Days to retain archives (default: 7)
  LOOM_CLEANUP_INTERVAL       Minutes between periodic cleanups (default: 360)
  LOOM_GRACE_PERIOD           Seconds after PR merge before cleanup (default: 600)

Examples:
  # After shepherd completes issue #123
  ./scripts/daemon-cleanup.sh shepherd-complete 123

  # On daemon startup
  ./scripts/daemon-cleanup.sh daemon-startup

  # Preview periodic cleanup
  ./scripts/daemon-cleanup.sh periodic --dry-run
EOF
    exit 0
  fi
done

# Parse arguments
EVENT="${1:-}"
shift || true

DRY_RUN=false
ISSUE_NUMBER=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --issue)
      ISSUE_NUMBER="$2"
      shift 2
      ;;
    [0-9]*)
      ISSUE_NUMBER="$1"
      shift
      ;;
    *)
      error "Unknown option: $1\nUse --help for usage information"
      ;;
  esac
done

# Validate
if [[ -z "$EVENT" ]]; then
  error "Event type required. Use --help for usage information"
fi

if [[ "$CLEANUP_ENABLED" != "true" ]]; then
  info "Cleanup disabled (LOOM_CLEANUP_ENABLED=$CLEANUP_ENABLED)"
  exit 0
fi

# Helper: Check if any shepherds are active
has_active_shepherds() {
  if [[ ! -f "$DAEMON_STATE" ]]; then
    echo "false"
    return
  fi

  local active=$(jq '[.shepherds // {} | to_entries[] | select(.value.issue != null)] | length' "$DAEMON_STATE" 2>/dev/null || echo "0")

  if [[ "$active" -gt 0 ]]; then
    echo "true"
  else
    echo "false"
  fi
}

# Helper: Check if specific issue has active shepherd
is_issue_being_worked() {
  local issue_num="$1"

  if [[ ! -f "$DAEMON_STATE" ]]; then
    echo "false"
    return
  fi

  local working=$(jq --arg issue "$issue_num" '[.shepherds // {} | to_entries[] | select(.value.issue == ($issue | tonumber))] | length' "$DAEMON_STATE" 2>/dev/null || echo "0")

  if [[ "$working" -gt 0 ]]; then
    echo "true"
  else
    echo "false"
  fi
}

# Helper: Update cleanup timestamp
update_cleanup_timestamp() {
  local event="$1"
  local timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  if [[ ! -f "$DAEMON_STATE" ]]; then
    return
  fi

  # Initialize cleanup section if needed
  if ! jq -e '.cleanup' "$DAEMON_STATE" >/dev/null 2>&1; then
    jq '.cleanup = {"lastRun": null, "lastCleaned": [], "pendingCleanup": [], "errors": []}' \
      "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE"
  fi

  jq --arg ts "$timestamp" --arg event "$event" '.cleanup.lastRun = $ts | .cleanup.lastEvent = $event' \
    "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE"
}

# Event: shepherd-complete
# Triggered when a shepherd finishes working on an issue
handle_shepherd_complete() {
  if [[ -z "$ISSUE_NUMBER" ]]; then
    error "Issue number required for shepherd-complete event"
  fi

  header "Shepherd Complete Cleanup: Issue #$ISSUE_NUMBER"
  echo ""

  # Check if PR is merged
  local branch_name="feature/issue-${ISSUE_NUMBER}"
  local pr_state=$(gh pr list --head "$branch_name" --state all --json state,mergedAt --jq '.[0] // empty' 2>/dev/null || echo "")

  if [[ -z "$pr_state" ]]; then
    info "No PR found for issue #$ISSUE_NUMBER, skipping cleanup"
    return
  fi

  local merged_at=$(echo "$pr_state" | jq -r '.mergedAt // "null"')

  if [[ "$merged_at" == "null" || -z "$merged_at" ]]; then
    info "PR not merged yet, scheduling for later cleanup"
    if [[ "$DRY_RUN" != true ]]; then
      jq --arg issue "issue-$ISSUE_NUMBER" '.cleanup.pendingCleanup += [$issue] | .cleanup.pendingCleanup |= unique' \
        "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE" 2>/dev/null || true
    fi
    return
  fi

  # Archive logs for this issue
  if [[ "$ARCHIVE_LOGS" == "true" ]]; then
    info "Archiving logs for issue #$ISSUE_NUMBER..."
    if [[ "$DRY_RUN" == true ]]; then
      "$SCRIPT_DIR/archive-logs.sh" --dry-run 2>/dev/null || true
    else
      "$SCRIPT_DIR/archive-logs.sh" 2>/dev/null || true
    fi
  fi

  # Clean up worktree (with grace period)
  local worktree_path="$REPO_ROOT/.loom/worktrees/issue-$ISSUE_NUMBER"

  if [[ -d "$worktree_path" ]]; then
    info "Cleaning worktree for issue #$ISSUE_NUMBER..."
    if [[ "$DRY_RUN" == true ]]; then
      "$SCRIPT_DIR/safe-worktree-cleanup.sh" --dry-run --grace-period "$GRACE_PERIOD" 2>/dev/null || true
    else
      "$SCRIPT_DIR/safe-worktree-cleanup.sh" --grace-period "$GRACE_PERIOD" 2>/dev/null || true
    fi
  fi

  if [[ "$DRY_RUN" != true ]]; then
    update_cleanup_timestamp "shepherd-complete"
  fi

  success "Shepherd complete cleanup finished for issue #$ISSUE_NUMBER"
}

# Event: daemon-startup
# Cleanup stale artifacts from previous daemon session
handle_daemon_startup() {
  header "Daemon Startup Cleanup"
  echo ""

  # Archive any orphaned task outputs from previous session
  if [[ "$ARCHIVE_LOGS" == "true" ]]; then
    info "Archiving orphaned task outputs..."
    if [[ "$DRY_RUN" == true ]]; then
      "$SCRIPT_DIR/archive-logs.sh" --dry-run 2>/dev/null || warning "archive-logs.sh not found"
    else
      "$SCRIPT_DIR/archive-logs.sh" 2>/dev/null || warning "archive-logs.sh not found"
    fi
  fi

  # Process any pending cleanups from previous session
  if [[ -f "$DAEMON_STATE" ]]; then
    local pending=$(jq -r '.cleanup.pendingCleanup // [] | .[]' "$DAEMON_STATE" 2>/dev/null || echo "")

    if [[ -n "$pending" ]]; then
      info "Processing pending cleanups from previous session..."
      for item in $pending; do
        issue_num=$(echo "$item" | sed 's/issue-//')
        info "  Processing: $item"
        if [[ "$DRY_RUN" != true ]]; then
          # Remove from pending list
          jq --arg item "$item" '.cleanup.pendingCleanup -= [$item]' \
            "$DAEMON_STATE" > "${DAEMON_STATE}.tmp" && mv "${DAEMON_STATE}.tmp" "$DAEMON_STATE"
        fi
      done
    fi
  fi

  # Run safe worktree cleanup
  info "Cleaning stale worktrees..."
  if [[ "$DRY_RUN" == true ]]; then
    "$SCRIPT_DIR/safe-worktree-cleanup.sh" --dry-run 2>/dev/null || warning "safe-worktree-cleanup.sh not found"
  else
    "$SCRIPT_DIR/safe-worktree-cleanup.sh" 2>/dev/null || warning "safe-worktree-cleanup.sh not found"
  fi

  # Prune old archives
  info "Pruning old archives..."
  if [[ "$DRY_RUN" == true ]]; then
    "$SCRIPT_DIR/archive-logs.sh" --prune-only --dry-run --retention-days "$RETENTION_DAYS" 2>/dev/null || true
  else
    "$SCRIPT_DIR/archive-logs.sh" --prune-only --retention-days "$RETENTION_DAYS" 2>/dev/null || true
  fi

  if [[ "$DRY_RUN" != true ]]; then
    update_cleanup_timestamp "daemon-startup"
  fi

  success "Daemon startup cleanup complete"
}

# Event: daemon-shutdown
# Archive logs and cleanup before daemon exits
handle_daemon_shutdown() {
  header "Daemon Shutdown Cleanup"
  echo ""

  # Archive all current task outputs
  if [[ "$ARCHIVE_LOGS" == "true" ]]; then
    info "Archiving task outputs..."
    if [[ "$DRY_RUN" == true ]]; then
      "$SCRIPT_DIR/archive-logs.sh" --dry-run 2>/dev/null || warning "archive-logs.sh not found"
    else
      "$SCRIPT_DIR/archive-logs.sh" 2>/dev/null || warning "archive-logs.sh not found"
    fi
  fi

  # Note: Don't clean worktrees on shutdown - shepherds might be in progress
  # Let daemon-startup handle that on next run

  if [[ "$DRY_RUN" != true ]]; then
    update_cleanup_timestamp "daemon-shutdown"
  fi

  success "Daemon shutdown cleanup complete"
}

# Event: periodic
# Conservative periodic cleanup (respects active shepherds)
handle_periodic() {
  header "Periodic Cleanup"
  echo ""

  # Check if any shepherds are active
  if [[ "$(has_active_shepherds)" == "true" ]]; then
    info "Active shepherds detected - running conservative cleanup only"
  fi

  # Archive task outputs (safe even with active shepherds)
  if [[ "$ARCHIVE_LOGS" == "true" ]]; then
    info "Archiving task outputs..."
    if [[ "$DRY_RUN" == true ]]; then
      "$SCRIPT_DIR/archive-logs.sh" --dry-run 2>/dev/null || warning "archive-logs.sh not found"
    else
      "$SCRIPT_DIR/archive-logs.sh" 2>/dev/null || warning "archive-logs.sh not found"
    fi
  fi

  # Only clean worktrees if no active shepherds or in force mode
  if [[ "$(has_active_shepherds)" == "false" ]]; then
    info "No active shepherds - running full worktree cleanup..."
    if [[ "$DRY_RUN" == true ]]; then
      "$SCRIPT_DIR/safe-worktree-cleanup.sh" --dry-run 2>/dev/null || warning "safe-worktree-cleanup.sh not found"
    else
      "$SCRIPT_DIR/safe-worktree-cleanup.sh" 2>/dev/null || warning "safe-worktree-cleanup.sh not found"
    fi
  else
    info "Skipping worktree cleanup (active shepherds)"
  fi

  # Prune old archives
  info "Pruning old archives..."
  if [[ "$DRY_RUN" == true ]]; then
    "$SCRIPT_DIR/archive-logs.sh" --prune-only --dry-run --retention-days "$RETENTION_DAYS" 2>/dev/null || true
  else
    "$SCRIPT_DIR/archive-logs.sh" --prune-only --retention-days "$RETENTION_DAYS" 2>/dev/null || true
  fi

  if [[ "$DRY_RUN" != true ]]; then
    update_cleanup_timestamp "periodic"
  fi

  success "Periodic cleanup complete"
}

# Main dispatch
case "$EVENT" in
  shepherd-complete)
    handle_shepherd_complete
    ;;
  daemon-startup)
    handle_daemon_startup
    ;;
  daemon-shutdown)
    handle_daemon_shutdown
    ;;
  periodic)
    handle_periodic
    ;;
  *)
    error "Unknown event: $EVENT\nValid events: shepherd-complete, daemon-startup, daemon-shutdown, periodic"
    ;;
esac
