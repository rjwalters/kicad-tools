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

# --------------------------------------------------------------------------
# In-worktree detection
# --------------------------------------------------------------------------
#
# Delegated to `loom-daemon worktree-check` (#9425), the verb #8195 slice 11
# (PR #9424) added when it retired this script's predicate from
# `defaults/scripts/worktree.sh`. This file carried a verbatim copy of it:
#
#   git_dir=$(git rev-parse --git-common-dir)
#   work_dir=$(git rev-parse --show-toplevel)
#   [[ "$git_dir" != "$work_dir/.git" ]]        # => "in a worktree"
#
# `--show-toplevel` is always ABSOLUTE; `--git-common-dir` is RELATIVE to the
# current directory whenever it can be (`.git` at the repo root, `../.git` one
# level down). So in the primary clone the comparison read `.git` !=
# `/repo/.git` — true — and it answered "in a worktree" there, in every
# subdirectory of it, and inside a real linked worktree alike. It had no
# reachable false branch from anywhere a caller can stand, so the "Not
# currently in a worktree" arm below was dead code — in the one script that
# tells operators to run `pnpm worktree --check`, which slice 11 had just made
# answer correctly. The two disagreed about what a worktree is.
#
# The verb compares canonicalized `--git-dir` against canonicalized
# `--git-common-dir` — git's own definition of a linked worktree, and correct
# through a symlinked repo path and for a `--separate-git-dir` checkout (where
# `.git` is a FILE, defeating the tempting `[[ -f .git ]]` shortcut) alike.
#
# This is the hard delegation `worktree.sh --check` took, NOT the physical
# fallback its create path kept: that fallback exists because a silent "not in
# a worktree" there would let `git worktree add` nest a worktree inside
# another. Nothing here is irreversible — the worst a wrong answer does is
# refuse to `cd` — so a daemonless host gets the refusal rather than a second
# spelling of the predicate that could drift from the verb again.
#
# LOOM_SCRIPT_HELPER_MISSING_RC=2 — argued, not defaulted: 0 and 1 are the
# verb's two ANSWERS ("inside a linked worktree" / "the main working
# directory") and the call site branches on them, so an unresolvable binary
# must not be readable as either. 2 is the code every epic-#7810 stub reserves
# for "could not run at all".
# requires-daemon: worktree-check >= 0.19.492  #9425 — the in-worktree predicate; without it this script exits 2 rather than guessing
check_if_in_worktree() {
    local helper
    helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/script-helper.sh"
    if [[ ! -f "$helper" ]]; then
        print_error "lib/script-helper.sh is missing — cannot tell whether this is a worktree."
        echo "This install is incomplete; re-run the Loom installer or resync .loom/." >&2
        exit 2
    fi
    # shellcheck source=lib/script-helper.sh
    source "$helper"
    # A SUBSHELL, because `loom_exec_script_helper` execs and never returns: in
    # `( … )` the exec replaces the subshell, so the verb's exit code arrives
    # here as `$?` instead of replacing this script mid-flight. stdout is
    # dropped (the verb's report text is not this script's output, and `--json`
    # callers parse one document); stderr is kept so the missing-daemon
    # message, which names the provisioning path, still reaches the operator.
    (
        LOOM_SCRIPT_HELPER_MISSING_RC=2 \
            loom_exec_script_helper worktree-check >/dev/null
    )
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

# Verify we're in a worktree.
#
# `|| IN_WORKTREE_RC=$?` rather than a bare call: `set -e` is in force at the
# top of this file, and a function whose last command exits non-zero outside a
# condition context would abort the script before the arms below could run. The
# three codes are the verb's contract — 0 "inside a linked worktree", 1 "the
# main working directory", 2 "could not run at all".
IN_WORKTREE_RC=0
check_if_in_worktree || IN_WORKTREE_RC=$?

if [[ "$IN_WORKTREE_RC" == "1" ]]; then
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
elif [[ "$IN_WORKTREE_RC" != "0" ]]; then
    # Deliberately distinct from both answers: this is "I could not look",
    # not "I looked and you are in the main working directory". The resolver
    # has already printed the actionable provisioning message to stderr.
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"error": "Could not determine whether this is a worktree", "inWorktree": null}'
    else
        print_error "Could not determine whether this is a worktree"
        print_info "\`loom-daemon worktree-check\` could not be run — see the message above"
    fi
    exit 2
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
