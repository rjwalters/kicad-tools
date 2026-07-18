#!/usr/bin/env bash
# Sync workflow labels from .github/labels.yml onto the forge.
#
# Creates (or updates) the Loom coordination labels defined in
# .github/labels.yml and removes GitHub's noisy default labels. Run this
# after a Quick / files-only install to create the labels the label-based
# workflow depends on — a Quick install ships .github/labels.yml but does
# NOT create the labels on the forge (see issue #3582).
#
# Supports both GitHub (via the gh CLI) and Gitea (via the forge API).
#
# Usage:
#   .loom/scripts/sync-labels.sh [WORKTREE_PATH]
#
#   WORKTREE_PATH  Directory containing .github/labels.yml and a git remote.
#                  Defaults to the current directory.
#
# This is the installed-tree counterpart of the source-only
# scripts/install/sync-labels.sh. It is self-contained apart from the
# shipped .loom/scripts/lib/forge-helpers.sh helper library.

set -euo pipefail

# --- Argument parsing -------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: sync-labels.sh [WORKTREE_PATH]

Sync Loom workflow labels from .github/labels.yml onto the forge (GitHub or
Gitea). Creates missing labels, updates existing ones to match labels.yml,
and removes GitHub's default labels.

Arguments:
  WORKTREE_PATH   Directory containing .github/labels.yml (default: .)

Options:
  -h, --help      Show this help and exit.
EOF
}

WORKTREE_PATH="."
case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  -*)
    echo "Unknown option: $1" >&2
    usage >&2
    exit 2
    ;;
  "")
    ;;
  *)
    WORKTREE_PATH="$1"
    ;;
esac

# Source the shipped forge-agnostic helper library. Provides forge_detect,
# forge_get_repo_nwo, forge_split_nwo, and gitea_api.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/forge-helpers.sh
source "${SCRIPT_DIR}/lib/forge-helpers.sh"

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

error() {
  echo -e "${RED}✗ Error: $*${NC}" >&2
  exit 1
}

info() {
  echo -e "${BLUE}ℹ $*${NC}" >&2
}

success() {
  echo -e "${GREEN}✓ $*${NC}" >&2
}

warning() {
  echo -e "${YELLOW}⚠ Warning: $*${NC}" >&2
}

cd "$WORKTREE_PATH"

# Detect forge type (github/gitea) from env, config, or git remote.
forge_detect

# Resolve the target repository NWO (owner/repo).
REPO="$(forge_get_repo_nwo || true)"
if [[ -z "$REPO" ]]; then
  error "Could not determine repository from git remote"
fi
# Populate FORGE_OWNER / FORGE_REPO for the Gitea API paths.
forge_split_nwo "$REPO"

info "Target repository: $REPO (${FORGE_TYPE})"

LABELS_FILE=".github/labels.yml"

if [[ ! -f "$LABELS_FILE" ]]; then
  warning "Labels file not found: $LABELS_FILE"
  warning "Skipping label sync"
  exit 0
fi

info "Syncing workflow labels from $LABELS_FILE..."

# ============================================================================
# GitHub label operations
# ============================================================================

github_delete_label() {
  local label="$1"
  if output=$(gh label delete "$label" -R "$REPO" --yes 2>&1); then
    info "Deleted default label: $label"
  elif ! echo "$output" | grep -qi "not found\|404"; then
    warning "Could not delete label '$label': $output"
  fi
}

github_sync_label() {
  local name="$1" description="$2" color="$3"

  if gh label list -R "$REPO" --json name --jq '.[].name' 2>&1 | grep -q "^${name}$" 2>/dev/null; then
    if output=$(gh label edit "$name" -R "$REPO" --description "$description" --color "$color" 2>&1); then
      info "Updated label: $name"
    else
      warning "Failed to update label: $name"
      echo "$output" >&2
    fi
  else
    if output=$(gh label create "$name" -R "$REPO" --description "$description" --color "$color" 2>&1); then
      info "Created label: $name"
    else
      if echo "$output" | grep -q "already exists"; then
        if update_output=$(gh label edit "$name" -R "$REPO" --description "$description" --color "$color" 2>&1); then
          info "Updated label: $name"
        else
          warning "Failed to update label: $name"
          echo "$update_output" >&2
        fi
      else
        warning "Failed to create label: $name"
        echo "$output" >&2
      fi
    fi
  fi
}

# ============================================================================
# Gitea label operations
#
# forge-helpers.sh's gitea_api emits the response BODY on stdout and signals
# success via its exit code (0 on 2xx, 1 otherwise) — unlike the source
# install script's forge-detect.sh helper, which appended the HTTP status to
# the body. These functions are wired to that exit-code contract.
# ============================================================================

# Look up a Gitea label ID by name. Echoes the ID (empty if not found).
gitea_label_id() {
  local name="$1"
  local body
  body=$(gitea_api GET "repos/${FORGE_OWNER}/${FORGE_REPO}/labels" 2>/dev/null) || return 0
  echo "$body" | python3 -c "
import json, sys
target = $(python3 -c "import json,sys; print(json.dumps('$name'))")
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for l in data:
    if l.get('name') == target:
        print(l['id'])
        break
" 2>/dev/null || true
}

gitea_delete_label() {
  local label="$1"
  local label_id
  label_id=$(gitea_label_id "$label")

  if [[ -n "$label_id" ]]; then
    if gitea_api DELETE "repos/${FORGE_OWNER}/${FORGE_REPO}/labels/${label_id}" >/dev/null 2>&1; then
      info "Deleted default label: $label"
    else
      warning "Could not delete label '$label'"
    fi
  fi
}

gitea_sync_label() {
  local name="$1" description="$2" color="$3"
  local label_id
  label_id=$(gitea_label_id "$name")

  local payload
  payload="{\"name\":$(printf '%s' "$name" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'),\"description\":$(printf '%s' "$description" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'),\"color\":\"#${color}\"}"

  if [[ -n "$label_id" ]]; then
    # Update existing label
    if gitea_api PATCH "repos/${FORGE_OWNER}/${FORGE_REPO}/labels/${label_id}" -d "$payload" >/dev/null 2>&1; then
      info "Updated label: $name"
    else
      warning "Failed to update label: $name"
    fi
  else
    # Create new label
    if gitea_api POST "repos/${FORGE_OWNER}/${FORGE_REPO}/labels" -d "$payload" >/dev/null 2>&1; then
      info "Created label: $name"
    else
      warning "Failed to create label: $name"
    fi
  fi
}

# ============================================================================
# Main sync logic
# ============================================================================

# Remove default labels that clutter issue tracking
DEFAULT_LABELS=(
  "bug"
  "documentation"
  "duplicate"
  "enhancement"
  "good first issue"
  "help wanted"
  "invalid"
  "question"
  "wontfix"
)

info "Removing default labels..."
for label in "${DEFAULT_LABELS[@]}"; do
  if [[ "$FORGE_TYPE" == "github" ]]; then
    github_delete_label "$label"
  elif [[ "$FORGE_TYPE" == "gitea" ]]; then
    gitea_delete_label "$label"
  fi
done

# Sync Loom workflow labels
info "Syncing Loom workflow labels..."

label_count=0
while IFS= read -u 3 -r line; do
  if [[ "$line" =~ ^-\ name:\ (.+)$ ]]; then
    name="${BASH_REMATCH[1]}"
    read -u 3 -r desc_line
    read -u 3 -r color_line

    description=""
    color=""

    if [[ "$desc_line" =~ description:\ (.+)$ ]]; then
      description="${BASH_REMATCH[1]}"
      description="${description//\"/}"
    fi

    if [[ "$color_line" =~ color:\ \"?([0-9A-Fa-f]{6})\"?.*$ ]]; then
      color="${BASH_REMATCH[1]}"
    fi

    if [[ "$FORGE_TYPE" == "github" ]]; then
      github_sync_label "$name" "$description" "$color"
    elif [[ "$FORGE_TYPE" == "gitea" ]]; then
      gitea_sync_label "$name" "$description" "$color"
    fi

    ((label_count++)) || true
  fi
done 3< "$LABELS_FILE"

if [ "$label_count" -gt 0 ]; then
  success "Synced $label_count labels"
else
  warning "No labels found in $LABELS_FILE"
fi
