#!/bin/bash

# Loom Worktree Helper Script
# Safely creates and manages git worktrees for agent development
#
# Usage:
#   pnpm worktree <issue-number>                    # Create worktree for issue
#   pnpm worktree <issue-number> <branch>           # Create worktree with custom branch name
#   pnpm worktree --check                           # Check if currently in a worktree
#   pnpm worktree --json <issue-number>             # Machine-readable output
#   pnpm worktree --return-to <dir> <issue-number>  # Store return directory
#   pnpm worktree --help                            # Show help

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

# Function to pull latest changes from origin/main
# Stashes local changes, pulls with fast-forward only, then restores stash
pull_latest_main() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Pulling latest changes from origin/main..."
    fi

    # Stash any local changes first
    STASH_OUTPUT=$(git stash push -m "worktree-creation-auto-stash" 2>&1)
    STASHED=false
    if [[ "$STASH_OUTPUT" != *"No local changes"* ]] && [[ "$STASH_OUTPUT" != *"nothing to save"* ]]; then
        STASHED=true
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Stashed local changes"
        fi
    fi

    # Pull latest with fast-forward only (prevents accidental merge commits on main)
    if git pull --ff-only origin main 2>/dev/null; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_success "Updated main to latest"
        fi
    else
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_warning "Could not fast-forward main (may need manual intervention)"
        fi
    fi

    # Pop stash if we stashed
    if [[ "$STASHED" == "true" ]]; then
        if git stash pop 2>/dev/null; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_info "Restored stashed changes"
            fi
        else
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Could not restore stash (check 'git stash list')"
            fi
        fi
    fi
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

# Function to get current worktree info
get_worktree_info() {
    if check_if_in_worktree; then
        local worktree_path=$(git rev-parse --show-toplevel)
        local branch=$(git rev-parse --abbrev-ref HEAD)

        echo "Current worktree:"
        echo "  Path: $worktree_path"
        echo "  Branch: $branch"
        return 0
    else
        echo "Not currently in a worktree (you're in the main working directory)"
        return 1
    fi
}

# Function to check for .loom-in-use marker
check_in_use_marker() {
    local worktree_path="$1"
    local marker_file="$worktree_path/.loom-in-use"

    if [[ -f "$marker_file" ]]; then
        return 0  # Marker exists - worktree is in use
    else
        return 1  # No marker - not in use
    fi
}

# Function to check for active processes using worktree as CWD
check_active_processes() {
    local worktree_path="$1"
    local abs_worktree_path
    abs_worktree_path=$(cd "$worktree_path" 2>/dev/null && pwd) || return 1

    # Try to find processes with CWD in the worktree
    if command -v lsof &>/dev/null; then
        # macOS/BSD: use lsof
        local pids
        pids=$(lsof +d "$abs_worktree_path" -F pt 2>/dev/null | grep -A1 '^p' | grep '^tcwd' -B1 | grep '^p' | cut -c2- | grep -v "^$$" || true)
        if [[ -n "$pids" ]]; then
            return 0  # Active processes found
        fi
    elif [[ -d "/proc" ]]; then
        # Linux: check /proc
        for pid_dir in /proc/[0-9]*; do
            local pid="${pid_dir##*/}"
            [[ "$pid" == "$$" ]] && continue  # Skip current process
            local cwd
            cwd=$(readlink -f "$pid_dir/cwd" 2>/dev/null) || continue
            if [[ "$cwd" == "$abs_worktree_path" || "$cwd" == "$abs_worktree_path/"* ]]; then
                return 0  # Active process found
            fi
        done
    fi

    return 1  # No active processes
}

# Function to check worktree creation grace period (5 minutes default)
check_grace_period() {
    local worktree_path="$1"
    local grace_seconds="${2:-300}"  # Default 5 minutes
    local git_file="$worktree_path/.git"

    local creation_time
    if [[ -e "$git_file" ]]; then
        # Get creation/modification time
        if stat --version &>/dev/null 2>&1; then
            # GNU stat
            creation_time=$(stat -c %Y "$git_file" 2>/dev/null) || return 1
        else
            # BSD stat (macOS)
            creation_time=$(stat -f %m "$git_file" 2>/dev/null) || return 1
        fi
    else
        return 1  # No .git file
    fi

    local current_time
    current_time=$(date +%s)
    local age=$((current_time - creation_time))

    if [[ "$age" -lt "$grace_seconds" ]]; then
        return 0  # Within grace period
    else
        return 1  # Past grace period
    fi
}

# Function to check if current shell's CWD is inside a worktree
check_cwd_inside_worktree() {
    local worktree_path="$1"
    local current_cwd
    local abs_worktree_path

    # Get current working directory (resolved, follows symlinks)
    current_cwd=$(pwd -P 2>/dev/null || pwd)

    # Get absolute path of worktree (resolved)
    abs_worktree_path=$(cd "$worktree_path" 2>/dev/null && pwd -P || echo "$worktree_path")

    # Check if current CWD is exactly the worktree or inside it
    if [[ "$current_cwd" == "$abs_worktree_path" || "$current_cwd" == "$abs_worktree_path/"* ]]; then
        return 0  # CWD is inside worktree
    else
        return 1  # CWD is not inside worktree
    fi
}

# Function to check if worktree is safe to remove
is_worktree_safe_to_remove() {
    local worktree_path="$1"
    local reason=""

    # Check 0: Current shell's CWD inside worktree (simplest and most direct check)
    if check_cwd_inside_worktree "$worktree_path"; then
        reason="current shell CWD is inside worktree"
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "  $reason - preserving"
        fi
        return 1
    fi

    # Check 1: In-use marker
    if check_in_use_marker "$worktree_path"; then
        reason="worktree has .loom-in-use marker"
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "  $reason - preserving"
        fi
        return 1
    fi

    # Check 2: Active processes
    if check_active_processes "$worktree_path"; then
        reason="active process(es) using worktree"
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "  $reason - preserving"
        fi
        return 1
    fi

    # Check 3: Grace period
    if check_grace_period "$worktree_path"; then
        reason="worktree within grace period"
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "  $reason - preserving"
        fi
        return 1
    fi

    return 0  # Safe to remove
}

# Function to show help
show_help() {
    cat << EOF
Loom Worktree Helper

This script helps AI agents safely create and manage git worktrees.

Usage:
  pnpm worktree <issue-number>                    Create worktree for issue
  pnpm worktree <issue-number> <branch>           Create worktree with custom branch
  pnpm worktree --check                           Check if in a worktree
  pnpm worktree --json <issue-number>             Machine-readable JSON output
  pnpm worktree --return-to <dir> <issue-number>  Store return directory
  pnpm worktree --help                            Show this help

Examples:
  pnpm worktree 42
    Creates: .loom/worktrees/issue-42
    Branch: feature/issue-42

  pnpm worktree 42 fix-bug
    Creates: .loom/worktrees/issue-42
    Branch: feature/fix-bug

  pnpm worktree --check
    Shows current worktree status

  pnpm worktree --json 42
    Output: {"success": true, "worktreePath": "/path/to/.loom/worktrees/issue-42", ...}

  pnpm worktree --return-to $(pwd) 42
    Creates worktree and stores current directory for later return

Safety Features:
  ✓ Detects if already in a worktree
  ✓ Uses sandbox-safe path (.loom/worktrees/)
  ✓ Pulls latest origin/main before creating worktree
  ✓ Automatically creates branch from main
  ✓ Prevents nested worktrees
  ✓ Non-interactive (safe for AI agents)
  ✓ Reuses existing branches automatically
  ✓ Symlinks node_modules from main (avoids pnpm install)
  ✓ Runs project-specific hooks after creation
  ✓ Stashes/restores local changes during pull

Project-Specific Hooks:
  Create .loom/hooks/post-worktree.sh to run custom setup after worktree creation.
  This file is NOT overwritten by Loom upgrades.

  The hook receives three arguments:
    \$1 - Absolute path to the new worktree
    \$2 - Branch name (e.g., feature/issue-42)
    \$3 - Issue number

  Example hook (.loom/hooks/post-worktree.sh):
    #!/bin/bash
    cd "\$1"
    pnpm install  # or: lake exe cache get, pip install -e ., etc.

Resuming Abandoned Work:
  If an agent abandoned work on issue #42, a new agent can resume:
    ./.loom/scripts/worktree.sh 42
  This will:
    - Reuse the existing feature/issue-42 branch
    - Create a fresh worktree at .loom/worktrees/issue-42
    - Allow continuing from where the previous agent left off

Notes:
  - All worktrees are created in .loom/worktrees/ (gitignored)
  - Branch names automatically prefixed with 'feature/'
  - Existing branches are reused without prompting (non-interactive)
  - After creation, cd into the worktree to start working
  - To return to main: cd /path/to/repo && git checkout main
EOF
}

# Parse arguments
if [[ $# -eq 0 ]] || [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; then
    show_help
    exit 0
fi

if [[ "$1" == "--check" ]]; then
    get_worktree_info
    exit $?
fi

# Check for --json flag
JSON_OUTPUT=false
RETURN_TO_DIR=""

if [[ "$1" == "--json" ]]; then
    JSON_OUTPUT=true
    shift
fi

# Check for --return-to flag
if [[ "$1" == "--return-to" ]]; then
    RETURN_TO_DIR="$2"
    shift 2
    # Validate return directory exists
    if [[ ! -d "$RETURN_TO_DIR" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Return directory does not exist", "returnTo": "'"$RETURN_TO_DIR"'"}'
        else
            print_error "Return directory does not exist: $RETURN_TO_DIR"
        fi
        exit 1
    fi
fi

# Main worktree creation logic
ISSUE_NUMBER="$1"
CUSTOM_BRANCH="$2"

# Validate issue number
if ! [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
    print_error "Issue number must be numeric (got: '$ISSUE_NUMBER')"
    echo ""
    echo "Usage: pnpm worktree <issue-number> [branch-name]"
    exit 1
fi

# Check if already in a worktree and automatically handle it
if check_if_in_worktree; then
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_warning "Currently in a worktree, auto-navigating to main workspace..."
        echo ""
        get_worktree_info
        echo ""
    fi

    # Find the git root (common directory for all worktrees)
    GIT_COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
    if [[ -z "$GIT_COMMON_DIR" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Failed to find git common directory"}'
        else
            print_error "Failed to find git common directory"
        fi
        exit 1
    fi

    # The main workspace is the parent of .git (or the directory containing .git)
    MAIN_WORKSPACE=$(dirname "$GIT_COMMON_DIR")
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Found main workspace: $MAIN_WORKSPACE"
    fi

    # Change to main workspace
    if cd "$MAIN_WORKSPACE" 2>/dev/null; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_success "Switched to main workspace"
        fi

        # Check if we're on main branch, if not switch to it
        CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
        if [[ "$CURRENT_BRANCH" != "main" ]]; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_info "Switching from $CURRENT_BRANCH to main branch..."
            fi
            if git checkout main 2>/dev/null; then
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_success "Switched to main branch"
                fi
            else
                if [[ "$JSON_OUTPUT" == "true" ]]; then
                    echo '{"error": "Failed to switch to main branch"}'
                else
                    print_error "Failed to switch to main branch"
                    print_info "Please manually run: git checkout main"
                fi
                exit 1
            fi
        fi
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Failed to change to main workspace", "mainWorkspace": "'"$MAIN_WORKSPACE"'"}'
        else
            print_error "Failed to change to main workspace: $MAIN_WORKSPACE"
            print_info "Please manually run: cd $MAIN_WORKSPACE"
        fi
        exit 1
    fi
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        echo ""
    fi
fi

# Prune orphaned worktree references before any worktree operations
# This cleans up stale references when worktree directories were deleted externally (e.g., rm -rf)
# Without this, subsequent worktree operations or `gh pr checkout` can fail
PRUNE_OUTPUT=$(git worktree prune --dry-run --verbose 2>/dev/null || true)
if [[ -n "$PRUNE_OUTPUT" ]]; then
    # There are orphaned references to prune
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Pruning orphaned worktree references..."
    fi
    if git worktree prune 2>/dev/null; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_success "Pruned orphaned worktree references"
        fi
    else
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_warning "Failed to prune worktrees (continuing anyway)"
        fi
    fi
fi

# Ensure we're on main branch and pull latest changes
# This happens whether we came from a worktree (already switched above) or started in main workspace
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Switching from $CURRENT_BRANCH to main branch..."
    fi
    if git checkout main 2>/dev/null; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_success "Switched to main branch"
        fi
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Failed to switch to main branch"}'
        else
            print_error "Failed to switch to main branch"
            print_info "Please manually run: git checkout main"
        fi
        exit 1
    fi
fi

# Pull latest changes from origin/main before creating the worktree
# This ensures new worktrees are based on the most up-to-date code
pull_latest_main

# Determine branch name
if [[ -n "$CUSTOM_BRANCH" ]]; then
    BRANCH_NAME="feature/$CUSTOM_BRANCH"
else
    BRANCH_NAME="feature/issue-$ISSUE_NUMBER"
fi

# Worktree path
WORKTREE_PATH=".loom/worktrees/issue-$ISSUE_NUMBER"

# Check if worktree already exists
if [[ -d "$WORKTREE_PATH" ]]; then
    print_warning "Worktree already exists at: $WORKTREE_PATH"

    # Check if it's registered with git
    if git worktree list | grep -q "$WORKTREE_PATH"; then
        # Check if worktree is stale: no commits ahead of main and behind main
        local_commits_ahead=$(git -C "$WORKTREE_PATH" rev-list --count "origin/main..HEAD" 2>/dev/null) || local_commits_ahead="0"
        local_commits_behind=$(git -C "$WORKTREE_PATH" rev-list --count "HEAD..origin/main" 2>/dev/null) || local_commits_behind="0"
        local_uncommitted=$(git -C "$WORKTREE_PATH" status --porcelain 2>/dev/null) || local_uncommitted=""

        if [[ "$local_commits_ahead" == "0" && "$local_commits_behind" -gt 0 && -z "$local_uncommitted" ]]; then
            # Potentially stale worktree: no work done, behind main, no uncommitted changes
            # But first, check safety constraints before removal
            if ! is_worktree_safe_to_remove "$WORKTREE_PATH"; then
                # Safety check failed - reuse the worktree instead of removing
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_info "Worktree appears stale but cannot be safely removed - reusing"
                    echo ""
                    print_info "To use this worktree: cd $WORKTREE_PATH"
                fi
                exit 0
            fi

            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Stale worktree detected (0 commits ahead, $local_commits_behind behind main, no uncommitted changes)"
                print_info "Removing stale worktree and recreating from current main..."
            fi

            # Remove the stale worktree (safety checks passed)
            # Use absolute path and git -C to avoid CWD-inside-worktree issues
            ABS_WORKTREE="$(cd "$WORKTREE_PATH" 2>/dev/null && pwd -P || echo "$WORKTREE_PATH")"
            REPO_DIR="$(pwd -P)"
            local_branch=$(git -C "$ABS_WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null) || local_branch=""
            git -C "$REPO_DIR" worktree remove "$ABS_WORKTREE" --force 2>/dev/null || {
                print_error "Failed to remove stale worktree"
                exit 1
            }

            # Delete the empty branch if it exists and has no commits ahead
            if [[ -n "$local_branch" && "$local_branch" != "main" ]]; then
                if git branch -d "$local_branch" 2>/dev/null; then
                    if [[ "$JSON_OUTPUT" != "true" ]]; then
                        print_info "Removed empty branch: $local_branch"
                    fi
                else
                    # Branch may have diverged or have other references; force-delete only if truly empty
                    if [[ "$JSON_OUTPUT" != "true" ]]; then
                        print_warning "Could not delete branch $local_branch (may have upstream references)"
                    fi
                fi
            fi

            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_success "Stale worktree cleaned up"
                echo ""
            fi
            # Fall through to create fresh worktree below
        elif [[ "$local_commits_ahead" == "0" && "$local_commits_behind" == "0" && -z "$local_uncommitted" ]]; then
            # Worktree at same commit as main, no commits, no uncommitted changes
            # Check if remote branch exists - if not, this is an abandoned worktree
            if ! git ls-remote --heads origin "$BRANCH_NAME" 2>/dev/null | grep -q .; then
                # No commits + no remote = abandoned worktree, safe to clean
                if ! is_worktree_safe_to_remove "$WORKTREE_PATH"; then
                    # Safety check failed - reuse the worktree instead of removing
                    if [[ "$JSON_OUTPUT" != "true" ]]; then
                        print_info "Worktree appears abandoned but cannot be safely removed - reusing"
                        echo ""
                        print_info "To use this worktree: cd $WORKTREE_PATH"
                    fi
                    exit 0
                fi

                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_warning "Abandoned worktree detected (0 commits, at same commit as main, no remote branch)"
                    print_info "Removing abandoned worktree and recreating from current main..."
                fi

                # Remove the abandoned worktree (safety checks passed)
                ABS_WORKTREE="$(cd "$WORKTREE_PATH" 2>/dev/null && pwd -P || echo "$WORKTREE_PATH")"
                REPO_DIR="$(pwd -P)"
                local_branch=$(git -C "$ABS_WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null) || local_branch=""
                git -C "$REPO_DIR" worktree remove "$ABS_WORKTREE" --force 2>/dev/null || {
                    print_error "Failed to remove abandoned worktree"
                    exit 1
                }

                # Delete the empty branch if it exists
                if [[ -n "$local_branch" && "$local_branch" != "main" ]]; then
                    if git branch -d "$local_branch" 2>/dev/null; then
                        if [[ "$JSON_OUTPUT" != "true" ]]; then
                            print_info "Removed empty branch: $local_branch"
                        fi
                    else
                        if [[ "$JSON_OUTPUT" != "true" ]]; then
                            print_warning "Could not delete branch $local_branch (may have upstream references)"
                        fi
                    fi
                fi

                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_success "Abandoned worktree cleaned up"
                    echo ""
                fi
                # Fall through to create fresh worktree below
            else
                # Remote branch exists - preserve worktree
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_info "Worktree is registered with git"
                    print_info "Remote branch exists - preserving worktree"
                    echo ""
                    print_info "To use this worktree: cd $WORKTREE_PATH"
                fi
                exit 0
            fi
        else
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_info "Worktree is registered with git"
                if [[ "$local_commits_ahead" -gt 0 ]]; then
                    print_info "Worktree has $local_commits_ahead commit(s) ahead of main - preserving existing work"
                elif [[ -n "$local_uncommitted" ]]; then
                    print_info "Worktree has uncommitted changes - preserving existing work"
                fi
                echo ""
                print_info "To use this worktree: cd $WORKTREE_PATH"
            fi
            exit 0
        fi
    else
        print_error "Directory exists but is not a registered worktree"
        echo ""
        print_info "To fix this:"
        echo "  1. Remove the directory: rm -rf $WORKTREE_PATH"
        echo "  2. Run again: pnpm worktree $ISSUE_NUMBER"
        exit 1
    fi
fi

# Check if branch already exists
if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_warning "Branch '$BRANCH_NAME' already exists - reusing it"
        print_info "To create a new branch instead, use a custom branch name:"
        echo "  ./.loom/scripts/worktree.sh $ISSUE_NUMBER <custom-branch-name>"
        echo ""
    fi

    CREATE_ARGS=("$WORKTREE_PATH" "$BRANCH_NAME")
else
    # Create new branch from main
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Creating new branch from main"
    fi
    CREATE_ARGS=("$WORKTREE_PATH" "-b" "$BRANCH_NAME" "main")
fi

# Create the worktree
if [[ "$JSON_OUTPUT" != "true" ]]; then
    print_info "Creating worktree..."
    echo "  Path: $WORKTREE_PATH"
    echo "  Branch: $BRANCH_NAME"
    echo ""
fi

if git worktree add "${CREATE_ARGS[@]}"; then
    # Get absolute path to worktree
    ABS_WORKTREE_PATH=$(cd "$WORKTREE_PATH" && pwd)

    # Set git hooks path so .githooks/ works in worktrees (no npx/husky needed)
    git -C "$ABS_WORKTREE_PATH" config core.hooksPath .githooks

    # Store return-to directory if provided
    if [[ -n "$RETURN_TO_DIR" ]]; then
        ABS_RETURN_TO=$(cd "$RETURN_TO_DIR" && pwd)
        echo "$ABS_RETURN_TO" > "$ABS_WORKTREE_PATH/.loom-return-to"
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Stored return directory: $ABS_RETURN_TO"
        fi
    fi

    # Initialize submodules with reference to main workspace (for object sharing)
    # This is much faster than downloading from network and saves disk space
    MAIN_GIT_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
    UNINIT_SUBMODULES=$(cd "$ABS_WORKTREE_PATH" && git submodule status 2>/dev/null | grep '^-' | wc -l | tr -d ' ')

    if [[ "$UNINIT_SUBMODULES" -gt 0 ]]; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Initializing $UNINIT_SUBMODULES submodule(s) with shared objects..."
        fi

        cd "$ABS_WORKTREE_PATH"

        # Process each uninitialized submodule
        git submodule status | grep '^-' | awk '{print $2}' | while read -r submod_path; do
            ref_path="$MAIN_GIT_DIR/modules/$submod_path"

            if [[ -d "$ref_path" ]]; then
                # Use reference to share objects with main workspace (fast, no network)
                if ! timeout 30 git submodule update --init --reference "$ref_path" -- "$submod_path" 2>/dev/null; then
                    echo "SUBMODULE_FAILED" > /tmp/loom-submodule-status-$$
                fi
            else
                # No reference available, initialize normally (may need network)
                if ! timeout 30 git submodule update --init -- "$submod_path" 2>/dev/null; then
                    echo "SUBMODULE_FAILED" > /tmp/loom-submodule-status-$$
                fi
            fi
        done

        # Check if any submodule failed
        if [[ -f "/tmp/loom-submodule-status-$$" ]]; then
            rm -f "/tmp/loom-submodule-status-$$"
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Some submodules failed to initialize (worktree still created)"
                print_info "You may need to run: git submodule update --init --recursive"
            fi
        else
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_success "Submodules initialized with shared objects"
            fi
        fi

        # Return to original directory
        cd - > /dev/null
    fi

    # Symlink node_modules from main workspace if available
    # This avoids expensive pnpm install on every worktree (30-60s savings)
    MAIN_WORKSPACE_DIR=$(git rev-parse --show-toplevel 2>/dev/null)
    MAIN_NODE_MODULES="$MAIN_WORKSPACE_DIR/node_modules"
    WORKTREE_NODE_MODULES="$ABS_WORKTREE_PATH/node_modules"
    WORKTREE_PACKAGE_JSON="$ABS_WORKTREE_PATH/package.json"

    if [[ -d "$MAIN_NODE_MODULES" && -f "$WORKTREE_PACKAGE_JSON" && ! -e "$WORKTREE_NODE_MODULES" ]]; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Symlinking node_modules from main workspace..."
        fi

        if ln -s "$MAIN_NODE_MODULES" "$WORKTREE_NODE_MODULES" 2>/dev/null; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_success "node_modules symlinked (skipping pnpm install)"
            fi
        else
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Could not symlink node_modules (will install on first build)"
            fi
        fi
    fi

    # Run project-specific post-worktree hook if it exists
    # This allows projects to add custom setup steps (e.g., pnpm install, lake exe cache get)
    # The hook is stored in .loom/hooks/ which is NOT overwritten by Loom upgrades
    # Note: MAIN_WORKSPACE_DIR is already set by node_modules symlink section above
    POST_WORKTREE_HOOK="$MAIN_WORKSPACE_DIR/.loom/hooks/post-worktree.sh"
    if [[ -x "$POST_WORKTREE_HOOK" ]]; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Running project-specific post-worktree hook..."
        fi

        # Run the hook from the new worktree directory
        # Pass: worktree path, branch name, issue number
        if (cd "$ABS_WORKTREE_PATH" && "$POST_WORKTREE_HOOK" "$ABS_WORKTREE_PATH" "$BRANCH_NAME" "$ISSUE_NUMBER"); then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_success "Post-worktree hook completed"
            fi
        else
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Post-worktree hook failed (worktree still created)"
            fi
        fi
    fi

    # Output results
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        # Machine-readable JSON output
        echo '{"success": true, "worktreePath": "'"$ABS_WORKTREE_PATH"'", "branchName": "'"$BRANCH_NAME"'", "issueNumber": '"$ISSUE_NUMBER"', "returnTo": "'"${ABS_RETURN_TO:-}"'"}'
    else
        # Human-readable output
        print_success "Worktree created successfully!"
        echo ""
        print_info "Next steps:"
        echo "  cd $WORKTREE_PATH"
        echo "  # Do your work..."
        echo "  git add -A"
        echo "  git commit -m 'Your message'"
        echo "  git push -u origin $BRANCH_NAME"
        echo "  gh pr create"
    fi
else
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "Failed to create worktree"}'
    else
        print_error "Failed to create worktree"
    fi
    exit 1
fi
