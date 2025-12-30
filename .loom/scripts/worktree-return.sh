#!/bin/bash

# Loom Worktree Return Helper Script
# Returns from an issue worktree to the terminal worktree that created it
#
# Usage:
#   pnpm worktree:return              # Return from current issue worktree
#   pnpm worktree:return --check      # Check if return path is stored
#   pnpm worktree:return --json       # Machine-readable output
#   pnpm worktree:return --help       # Show help

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_error() {
    echo -e "${RED}ERROR: $1${NC}" >&2
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# Function to check if we're in a worktree
check_if_in_worktree() {
    local git_dir=$(git rev-parse --git-common-dir 2>/dev/null)
    local work_dir=$(git rev-parse --show-toplevel 2>/dev/null)

    if [[ "$git_dir" != "$work_dir/.git" ]]; then
        return 0  # In a worktree
    else
        return 1  # In main working directory
    fi
}

# Function to show help
show_help() {
    cat << EOF
Loom Worktree Return Helper

This script returns from an issue worktree to the terminal worktree that created it.

Usage:
  pnpm worktree:return              Return from current issue worktree
  pnpm worktree:return --check      Check if return path is stored
  pnpm worktree:return --json       Machine-readable JSON output
  pnpm worktree:return --help       Show this help

Examples:
  # After finishing work in issue-42 worktree
  cd .loom/worktrees/issue-42
  # ... do work ...
  pnpm worktree:return
  # → Returns to terminal-N worktree

  # Check if current directory has return path
  pnpm worktree:return --check
  Output: Return path: /path/to/.loom/worktrees/terminal-1

  # Get machine-readable output
  pnpm worktree:return --json
  Output: {"success": true, "returnPath": "/path/to/.loom/worktrees/terminal-1"}

How it Works:
  1. When creating issue worktrees with --return-to flag, the return directory
     is stored in .loom-return-to file within the worktree
  2. This script reads that file and changes to the stored directory
  3. Useful for agents working in terminal-N → issue-N → terminal-N workflow

Notes:
  - Must be run from within an issue worktree
  - Return path must have been set with: pnpm worktree --return-to <dir> <issue>
  - Does NOT remove the issue worktree (cleanup happens separately)
  - After return, you're back in your terminal worktree ready for next task
EOF
}

# Parse arguments
JSON_OUTPUT=false

if [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; then
    show_help
    exit 0
fi

if [[ "$1" == "--json" ]]; then
    JSON_OUTPUT=true
    shift
fi

# Check mode
CHECK_ONLY=false
if [[ "$1" == "--check" ]]; then
    CHECK_ONLY=true
fi

# Verify we're in a worktree
if ! check_if_in_worktree; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "Not in a worktree", "inWorktree": false}'
    else
        print_error "Not currently in a worktree"
        print_info "This command must be run from within an issue worktree"
        echo ""
        echo "To check your current location:"
        echo "  pnpm worktree --check"
    fi
    exit 1
fi

# Get current worktree path
CURRENT_WORKTREE=$(git rev-parse --show-toplevel 2>/dev/null)
if [[ -z "$CURRENT_WORKTREE" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "Failed to determine current worktree path"}'
    else
        print_error "Failed to determine current worktree path"
    fi
    exit 1
fi

# Check for .loom-return-to file
RETURN_TO_FILE="$CURRENT_WORKTREE/.loom-return-to"
if [[ ! -f "$RETURN_TO_FILE" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "No return path stored", "hasReturnPath": false, "currentWorktree": "'"$CURRENT_WORKTREE"'"}'
    else
        print_error "No return path stored in this worktree"
        echo ""
        print_info "This worktree was not created with --return-to flag"
        echo ""
        echo "To set up return path for future worktrees:"
        echo "  pnpm worktree --return-to \$(pwd) <issue-number>"
        echo ""
        echo "You can still manually navigate back to your terminal worktree:"
        echo "  cd /path/to/.loom/worktrees/terminal-N"
    fi
    exit 1
fi

# Read return path
RETURN_PATH=$(cat "$RETURN_TO_FILE")
if [[ -z "$RETURN_PATH" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "Return path file is empty"}'
    else
        print_error "Return path file is empty"
    fi
    exit 1
fi

# Verify return path exists
if [[ ! -d "$RETURN_PATH" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "Return path no longer exists", "returnPath": "'"$RETURN_PATH"'"}'
    else
        print_error "Return path no longer exists: $RETURN_PATH"
        print_info "The terminal worktree may have been removed"
    fi
    exit 1
fi

# If check-only mode, just report and exit
if [[ "$CHECK_ONLY" == "true" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"hasReturnPath": true, "returnPath": "'"$RETURN_PATH"'", "currentWorktree": "'"$CURRENT_WORKTREE"'"}'
    else
        print_success "Return path is stored"
        echo "  Current: $CURRENT_WORKTREE"
        echo "  Return to: $RETURN_PATH"
    fi
    exit 0
fi

# Perform the return
if [[ "$JSON_OUTPUT" != "true" ]]; then
    print_info "Returning to terminal worktree..."
    echo "  From: $CURRENT_WORKTREE"
    echo "  To: $RETURN_PATH"
    echo ""
fi

# Change to return path
if cd "$RETURN_PATH" 2>/dev/null; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": true, "returnPath": "'"$RETURN_PATH"'", "previousWorktree": "'"$CURRENT_WORKTREE"'"}'
    else
        print_success "Returned to terminal worktree"
        echo ""
        print_info "Current directory: $(pwd)"
        echo ""
        echo "Ready for next task!"
        echo ""
        echo "Note: The issue worktree at $CURRENT_WORKTREE remains"
        echo "until cleanup (typically after PR is merged)"
    fi
else
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "Failed to change to return path", "returnPath": "'"$RETURN_PATH"'"}'
    else
        print_error "Failed to change to return path: $RETURN_PATH"
    fi
    exit 1
fi
