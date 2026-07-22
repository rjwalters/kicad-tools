#!/bin/bash

# Loom Worktree Helper Script
# Safely creates and manages git worktrees for agent development
#
# Usage:
#   pnpm worktree <issue-number>                       # Create worktree for issue
#   pnpm worktree <issue-number> <branch>              # Create worktree with custom branch name
#   pnpm worktree <issue-number> --sparse <paths...>   # Cone-mode sparse checkout
#   pnpm worktree <issue-number> --full                # Convert sparse worktree to full
#   pnpm worktree --check                              # Check if currently in a worktree
#   pnpm worktree --json <issue-number>                # Machine-readable output
#   pnpm worktree --return-to <dir> <issue-number>     # Store return directory
#   pnpm worktree --help                               # Show help

set -e

# Always-included safety set for sparse-mode checkouts. Even with --sparse,
# these paths must materialize or the worktree is unusable by an agent:
#   .claude/**         - agent skill graph + methodology hooks
#   .loom/**           - Loom orchestration lifecycle (scripts, roles, hooks)
#   .githooks/**       - repo hook config (core.hooksPath is set post-create)
#   scripts/**         - sibling helpers the agent may invoke
# Top-level tracked files are always included implicitly by cone mode.
#
# Downstream repos can extend this via LOOM_WORKTREE_ALWAYS_INCLUDE (space-
# separated paths).
LOOM_WORKTREE_ALWAYS_INCLUDE_DEFAULT=(.claude .loom .githooks scripts)

# Shared worktree-root resolver (env var / config key / default). Sourced so
# the worktree base can be redirected to an external volume (#3530). With no
# override configured, loom_worktree_root returns the historical
# ${repo_root}/.loom/worktrees path unchanged.
# shellcheck source=lib/worktree-root.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/worktree-root.sh"

# Shared default-branch resolver (env var / symbolic-ref / ls-remote / probe).
# Sourced so worktree base operations work on repos whose default branch is not
# `main` (e.g. `master`) without hardcoding `origin/main` everywhere (#3549).
# shellcheck source=lib/default-branch.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/default-branch.sh"

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
# Loom-managed sentinel (issue #3548)
# --------------------------------------------------------------------------
#
# Write the `.loom-managed` marker that authorizes cleanup tooling
# (merge-pr.sh, agent-destroy.sh, loom-clean) to remove this worktree. A
# worktree lacking this file is treated as user-owned and never touched by
# Loom (see issue #3334).
#
# This MUST be called on every code path that leaves a usable Loom worktree
# behind — not just first-creation. Historically the write lived inline in the
# `_try_worktree_add` success block only, so any re-invocation against an
# existing worktree (preserve-work, stale-reset, --sparse/--full re-config)
# exited before writing the sentinel and stranded the worktree: merge-pr.sh
# then refused to clean it up. See issue #3548.
#
# The write is a plain overwrite (`>`), so it is idempotent and self-heals a
# worktree whose sentinel was deleted. It reads the global $ISSUE_NUMBER and
# $BRANCH_NAME at call time. Do NOT call this for directories that are not
# registered git worktrees (the orphan-debris case) — those must be left
# sentinel-less so cleanup tooling keeps refusing them.
write_loom_sentinel() {
    local wt="$1"
    cat > "$wt/.loom-managed" <<EOF
# Loom-managed worktree marker
# Created by .loom/scripts/worktree.sh
# Issue: $ISSUE_NUMBER
# Branch: $BRANCH_NAME
# Removing this file makes Loom treat the worktree as user-owned and refuse
# to clean it up automatically.
EOF
}

# --------------------------------------------------------------------------
# Concurrency lock (issue #3380)
# --------------------------------------------------------------------------
#
# `git worktree add` is not safe to run concurrently against the same repo —
# parallel invocations contend on the per-worktree administrative dir
# (`.git/worktrees/issue-N/`) and on git's repo-global locks. The observed
# failure mode in busy shepherd sessions is multi-minute hangs (10-20 min)
# while a peer process holds an `index.lock` it will never release.
#
# We use a POSIX-atomic `mkdir`-based lock primitive — `flock` is not
# available on stock macOS, so `mkdir` is the only portable atomic
# file-system operation we can rely on.
#
# Lock scope is **repo-global** (`.loom/locks/worktree-add/`). The original
# per-issue design was tried first but failed under concurrent invocations
# with different issue numbers: `git worktree add` mutates the repo-global
# `.git/config.lock` (writing the new branch's upstream configuration), and
# concurrent processes race with the diagnostic:
#
#   error: could not lock config file .git/config: File exists
#   error: unable to write upstream branch configuration
#
# A repo-global lock serializes the entire `git worktree add` call so this
# race cannot happen. The cost — two builders on different issues no longer
# parallelize through the helper — is acceptable because (a) `git worktree
# add` itself is short relative to the rest of an issue's lifecycle, and
# (b) parallel hangs that hold an `index.lock` for 10-20 minutes are the
# very problem this PR fixes.
#
# The lock path uses the same name (`worktree-<id>/`) the per-issue version
# used so its layout matches `.loom/locks/issue-<N>/`. The "id"
# here is the constant string "add"; per-issue accounting still lives in the
# `owner.json` body for debugging visibility.
#
# Tunables (env vars, documented in show_help):
#   LOOM_WORKTREE_LOCK_TIMEOUT       — seconds to wait (default 600 = 10min,
#                                      sized to cover worst-case cold-clone
#                                      submodule init on heavy repos)
#   LOOM_WORKTREE_LOCK_POLL_INTERVAL — seconds between poll attempts (default 2)

LOOM_WORKTREE_LOCK_TIMEOUT="${LOOM_WORKTREE_LOCK_TIMEOUT:-600}"
LOOM_WORKTREE_LOCK_POLL_INTERVAL="${LOOM_WORKTREE_LOCK_POLL_INTERVAL:-2}"

# Resolve the locks directory to the canonical git common dir so worktrees
# and the main workspace all share the same lock namespace. Falls back to the
# current dir for the rare case where we're not yet inside a repo (tests).
_worktree_locks_dir() {
    local common
    common=$(git rev-parse --git-common-dir 2>/dev/null || true)
    if [[ -n "$common" ]]; then
        # git-common-dir may be returned as a relative path; resolve it.
        local abs_common
        abs_common=$(cd "$common" 2>/dev/null && pwd) || abs_common="$common"
        echo "$(dirname "$abs_common")/.loom/locks"
    else
        echo ".loom/locks"
    fi
}

_worktree_lock_path() {
    # The argument is the issue number — accepted for owner-metadata logging
    # only. The lock itself is repo-global; see the design note above.
    echo "$(_worktree_locks_dir)/worktree-add"
}

# Returns 0 if lock acquired, non-zero otherwise. Sets WORKTREE_LOCK_HOLDER_PID
# on timeout failure so the caller can include it in error output.
WORKTREE_LOCK_HOLDER_PID=""

acquire_worktree_lock() {
    local issue="$1"
    local lock
    lock="$(_worktree_lock_path "$issue")"
    local locks_dir
    locks_dir="$(_worktree_locks_dir)"

    mkdir -p "$locks_dir" 2>/dev/null || true

    local deadline=$(( $(date +%s) + LOOM_WORKTREE_LOCK_TIMEOUT ))
    local stale_retry_done=0

    while true; do
        if mkdir "$lock" 2>/dev/null; then
            # Lock acquired; record owner metadata for debugging.
            cat > "$lock/owner.json" <<EOF
{
  "issue": $issue,
  "owner_pid": $$,
  "script": "worktree.sh",
  "acquired_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
            return 0
        fi

        # Lock exists. Check whether the owner is still alive; if not, clear
        # it once and retry (stale-lock recovery).
        local owner_pid=""
        if [[ -f "$lock/owner.json" ]]; then
            owner_pid=$(awk -F'[ ,]+' '/owner_pid/ {gsub(/[^0-9]/,"",$3); print $3; exit}' "$lock/owner.json" 2>/dev/null)
        fi

        if [[ -n "$owner_pid" ]] && [[ "$stale_retry_done" -eq 0 ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Stale worktree lock from dead PID $owner_pid — cleaning up"
            fi
            rm -rf "$lock" 2>/dev/null || true
            stale_retry_done=1
            continue
        fi

        if [[ $(date +%s) -ge $deadline ]]; then
            WORKTREE_LOCK_HOLDER_PID="$owner_pid"
            return 1
        fi

        sleep "$LOOM_WORKTREE_LOCK_POLL_INTERVAL"
    done
}

release_worktree_lock() {
    local issue="$1"
    [[ -z "$issue" ]] && return 0
    local lock
    lock="$(_worktree_lock_path "$issue")"
    [[ -d "$lock" ]] || return 0
    rm -rf "$lock" 2>/dev/null || true
}

# cleanup_partial_worktree_state <issue>
#
# Removes the residue of a crashed `git worktree add`:
#   - `.git/worktrees/issue-<N>/{index,HEAD,gitdir}.lock` — file-level locks
#     that git would normally hold for the duration of an add operation and
#     release on success/failure. A SIGKILL'd or stuck process leaves them
#     behind, where they block every subsequent operation against the same
#     administrative dir.
#   - `.loom/worktrees/issue-<N>/` — a half-created worktree dir that was
#     never registered with git (verified via `git worktree list --porcelain`).
#
# **Sentinel contract** (#3334): a dir that IS registered with git is NEVER
# removed by this helper, regardless of `.loom-managed` presence. The sentinel
# governs cleanup-on-merge; this helper governs cleanup-on-crash-recovery, and
# the dividing line is "registered with git or not". An unregistered dir is by
# definition a shell from a killed add — the sentinel is written *after* a
# successful add (worktree.sh:761), so a half-created dir never has one.
cleanup_partial_worktree_state() {
    local issue="$1"
    local git_common
    git_common=$(git rev-parse --git-common-dir 2>/dev/null) || return 0

    local admin_dir="$git_common/worktrees/issue-$issue"
    local cleaned=0

    # 1. Per-worktree file locks.
    local lf
    for lf in index.lock HEAD.lock gitdir.lock; do
        if [[ -f "$admin_dir/$lf" ]]; then
            rm -f "$admin_dir/$lf" 2>/dev/null && cleaned=1
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Cleaned stale $lf at $admin_dir/$lf"
            fi
        fi
    done

    # 2. Orphan worktree dir (exists but git doesn't know about it).
    #    Resolve the base through loom_worktree_root so an overridden root
    #    (#3530) has its orphan debris cleaned too. The repo root is the parent
    #    of the git common dir (works whether or not cwd is the main workspace).
    local repo_root
    repo_root=$(cd "$(dirname "$git_common")" 2>/dev/null && pwd) || repo_root="$(pwd)"
    local wt_path
    wt_path="$(loom_worktree_root "$repo_root")/issue-$issue"
    if [[ -d "$wt_path" ]]; then
        # `git worktree list --porcelain` emits absolute paths on the
        # `worktree ` line; compare against the resolved absolute path.
        local abs_wt
        abs_wt=$(cd "$wt_path" 2>/dev/null && pwd) || abs_wt=""
        local registered=0
        if [[ -n "$abs_wt" ]]; then
            if git worktree list --porcelain 2>/dev/null \
                | awk '/^worktree / {print $2}' \
                | grep -Fxq "$abs_wt"; then
                registered=1
            fi
        fi
        if [[ $registered -eq 0 ]]; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Removing orphan worktree dir (not registered with git): $wt_path"
            fi
            rm -rf "$wt_path" 2>/dev/null && cleaned=1
        fi
    fi

    # 3. Prune now that the orphan administrative dir is locally consistent.
    if [[ $cleaned -eq 1 ]]; then
        git worktree prune 2>/dev/null || true
    fi
}

# --------------------------------------------------------------------------
# Sparse-checkout helpers
# --------------------------------------------------------------------------
#
# IMPORTANT: `git sparse-checkout init` writes core.sparseCheckout and
# core.sparseCheckoutCone to the per-worktree config
# (.git/worktrees/<name>/config.worktree), NOT to the shared .git/config.
# This avoids the regression where a stale shared core.sparseCheckout=true
# silently breaks later actions/checkout runs.

# Apply the sparse-checkout cone to an existing worktree.
# Args: $1 = worktree path; remaining args = cone paths (already including the
# always-included safety set).
apply_sparse_cone() {
    local wt_path="$1"
    shift
    local paths=("$@")

    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Configuring sparse-checkout cone..."
    fi

    git -C "$wt_path" sparse-checkout init --cone >/dev/null 2>&1
    # `sparse-checkout set` replaces the cone (idempotent: same paths = no-op).
    git -C "$wt_path" sparse-checkout set "${paths[@]}" >/dev/null 2>&1
}

# Materialize files for the configured cone.
materialize_sparse_cone() {
    local wt_path="$1"
    git -C "$wt_path" checkout >/dev/null 2>&1 || true
}

# Convert a sparse worktree back to a full checkout. Safe on already-full
# worktrees (sparse-checkout disable is a no-op).
disable_sparse_checkout() {
    local wt_path="$1"

    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Disabling sparse-checkout (full mode)..."
    fi

    if git -C "$wt_path" sparse-checkout disable >/dev/null 2>&1; then
        :
    else
        # Fallback: manually unset per-worktree config keys.
        git -C "$wt_path" config --unset core.sparseCheckout 2>/dev/null || true
        git -C "$wt_path" config --unset core.sparseCheckoutCone 2>/dev/null || true
    fi
    # Re-materialize the full working tree.
    git -C "$wt_path" checkout >/dev/null 2>&1 || true
}

# Check whether a worktree currently has sparse-checkout enabled (per-worktree
# config). Echoes "true" or "false".
is_sparse_enabled() {
    local wt_path="$1"
    local val
    val=$(git -C "$wt_path" config --get core.sparseCheckout 2>/dev/null || echo "")
    if [[ "$val" == "true" ]]; then
        echo "true"
    else
        echo "false"
    fi
}

# Log the realized disk footprint of a worktree (human-readable only).
log_worktree_size() {
    local wt_path="$1"
    local label="${2:-Worktree size}"
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        return 0
    fi
    local size
    size=$(du -sh "$wt_path" 2>/dev/null | awk '{print $1}')
    if [[ -n "$size" ]]; then
        print_info "$label: $size"
    fi
}

# Function to fetch latest changes from the default branch
# Uses fetch-only approach to avoid conflicts with worktrees that have the
# default branch checked out. Relies on the global DEFAULT_BRANCH (resolved via
# loom_default_branch before this is called).
fetch_latest_main() {
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Fetching latest changes from origin/$DEFAULT_BRANCH..."
    fi

    if git fetch origin "$DEFAULT_BRANCH" 2>/dev/null; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_success "Fetched latest origin/$DEFAULT_BRANCH"
        fi
    else
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_warning "Could not fetch origin/$DEFAULT_BRANCH (continuing with local state)"
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

# Function to show help
show_help() {
    cat << EOF
Loom Worktree Helper

This script helps AI agents safely create and manage git worktrees.

Usage:
  pnpm worktree <issue-number>                          Create worktree for issue
  pnpm worktree <issue-number> <branch>                 Create worktree with custom branch
  pnpm worktree <issue-number> --sparse <paths...>      Cone-mode sparse checkout
  pnpm worktree <issue-number> --full                   Convert sparse worktree to full
  pnpm worktree --check                                 Check if in a worktree
  pnpm worktree --json <issue-number>                   Machine-readable JSON output
  pnpm worktree --return-to <dir> <issue-number>        Store return directory
  pnpm worktree --help                                  Show this help

Examples:
  pnpm worktree 42
    Creates: .loom/worktrees/issue-42
    Branch: feature/issue-42

  pnpm worktree 42 fix-bug
    Creates: .loom/worktrees/issue-42
    Branch: feature/fix-bug

  pnpm worktree 42 --sparse src/lib defaults/scripts
    Creates a sparse worktree containing only the listed paths plus the
    always-included safety set (.claude/, .loom/, .githooks/, scripts/, and
    all tracked top-level files).

  pnpm worktree 42 --full
    Converts an existing sparse worktree back to a full checkout
    (no-op on an already-full worktree).

  pnpm worktree --check
    Shows current worktree status

  pnpm worktree --json 42
    Output: {"success": true, "worktreePath": "/path/to/.loom/worktrees/issue-42", ...}

  pnpm worktree --return-to $(pwd) 42
    Creates worktree and stores current directory for later return

Sparse-Mode Notes:
  - --sparse and --full are mutually exclusive
  - --sparse requires at least one path
  - Re-running --sparse with the same cone is a clean no-op (idempotent)
  - Re-running --sparse with a different cone replaces the cone
  - Set LOOM_WORKTREE_ALWAYS_INCLUDE to add repo-specific safety paths

Safety Features:
  ✓ Detects if already in a worktree
  ✓ Uses sandbox-safe path (.loom/worktrees/)
  ✓ Pulls latest origin/main before creating worktree
  ✓ Automatically creates branch from main
  ✓ Prevents nested worktrees
  ✓ Non-interactive (safe for AI agents)
  ✓ Reuses existing branches automatically
  ✓ Symlinks node_modules from main (avoids pnpm install)
  ✓ Symlinks nested per-package node_modules for pnpm/monorepo workspaces
  ✓ Symlinks extra gitignored paths via .loom/config.json worktree.linkPaths
  ✓ Excludes created symlinks via .git/info/exclude (no accidental git add)
  ✓ Symlinks .mcp.json from main (MCP config visible in worktrees)
  ✓ Runs project-specific hooks after creation
  ✓ Stashes/restores local changes during pull
  ✓ Repo-global lock serializes concurrent invocations (issue #3380)
  ✓ Recovers from stale .git/worktrees/issue-N/index.lock files
  ✓ Recovers from half-created .loom/worktrees/issue-N/ dirs

Environment Variables:
  LOOM_WORKTREE_ALWAYS_INCLUDE      Extra sparse-mode safety paths (space-sep)
  LOOM_SUBMODULE_TIMEOUT            Per-submodule init timeout (default 300s)
  LOOM_WORKTREE_LOCK_TIMEOUT        Lock acquisition timeout in seconds
                                    (default 600 — sized to cover worst-case
                                    cold-clone submodule init)
  LOOM_WORKTREE_LOCK_POLL_INTERVAL  Lock poll interval in seconds (default 2)
  LOOM_PRESERVE_WORKTREE            Disable cleanup-on-merge for all worktrees

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

Monorepo / Generated-Artifact Symlinks:
  In addition to the root node_modules symlink, worktree.sh symlinks:
    - Nested per-package node_modules (e.g. apps/web/node_modules) discovered by
      scanning the main workspace for node_modules dirs that sit next to a
      package.json (pnpm/monorepo layouts). No YAML parser dependency.
    - Extra gitignored paths listed in .loom/config.json under worktree.linkPaths,
      e.g. generated wasm-pack bindings that are expensive to rebuild per worktree:

        { "worktree": { "linkPaths": ["apps/web/src/wasm"] } }

  Each created symlink is added to the worktree's .git/info/exclude so 'git add -A'
  never stages it. All symlinking is best-effort — a failed link warns and
  continues; it never aborts worktree creation. Repos with no nested node_modules
  and no worktree.linkPaths config see no behavior change.

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

# JSON stdout-purity contract (#3546).
#
# `git worktree add` and `git submodule update` write some of their feedback
# lines to *stdout*, not stderr — e.g. "branch '...' set up to track '...'",
# "HEAD is now at <sha> <subject>", "Submodule path '...': checked out '<sha>'".
# In --json mode those lines would prefix the JSON document, so a consumer
# piping into `jq` hits `parse error ... line 1` AND (because the noise precedes
# the JSON) closes the pipe on the first bad line, SIGPIPE-killing this script
# mid-creation and leaving an orphan branch with no registered worktree.
#
# Fix the whole class rather than one call: in --json mode save the real stdout
# on fd 3 and redirect fd 1 to stderr, so *only* the final JSON document (which
# we emit explicitly to >&3) can reach the caller's stdout. Any stray git stdout
# now lands harmlessly on stderr. `trap '' PIPE` makes a consumer that closes
# early survive as a clean write failure instead of a fatal signal. In human
# mode fd 3 is just an alias for stdout, so the `>&3` JSON writes below are a
# no-op there and git progress stays visible on stdout as before.
if [[ "$JSON_OUTPUT" == "true" ]]; then
    exec 3>&1 1>&2
    trap '' PIPE
else
    exec 3>&1
fi

# Check for --return-to flag
if [[ "$1" == "--return-to" ]]; then
    RETURN_TO_DIR="$2"
    shift 2
    # Validate return directory exists
    if [[ ! -d "$RETURN_TO_DIR" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Return directory does not exist", "returnTo": "'"$RETURN_TO_DIR"'"}' >&3
        else
            print_error "Return directory does not exist: $RETURN_TO_DIR"
        fi
        exit 1
    fi
fi

# Main worktree creation logic
ISSUE_NUMBER="$1"
shift || true

# Validate issue number
if ! [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
    print_error "Issue number must be numeric (got: '$ISSUE_NUMBER')"
    echo ""
    echo "Usage: pnpm worktree <issue-number> [branch-name] [--sparse <paths...> | --full]"
    exit 1
fi

# Parse remaining args:
#   <branch> (positional, optional)
#   --sparse <path1> [path2 ...]
#   --full
SPARSE_MODE=false
FULL_MODE=false
SPARSE_PATHS=()
CUSTOM_BRANCH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sparse)
            SPARSE_MODE=true
            shift
            # Collect remaining args as paths until we hit another flag
            while [[ $# -gt 0 ]] && [[ "$1" != --* ]]; do
                SPARSE_PATHS+=("$1")
                shift
            done
            ;;
        --full)
            FULL_MODE=true
            shift
            ;;
        --*)
            print_error "Unknown flag: $1"
            echo ""
            echo "Usage: pnpm worktree <issue-number> [branch-name] [--sparse <paths...> | --full]"
            exit 1
            ;;
        *)
            if [[ -z "$CUSTOM_BRANCH" ]]; then
                CUSTOM_BRANCH="$1"
                shift
            else
                print_error "Unexpected argument: $1"
                exit 1
            fi
            ;;
    esac
done

# Validate flag combinations
if [[ "$SPARSE_MODE" == "true" && "$FULL_MODE" == "true" ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "--sparse and --full are mutually exclusive"}' >&3
    else
        print_error "--sparse and --full are mutually exclusive"
    fi
    exit 1
fi

if [[ "$SPARSE_MODE" == "true" && ${#SPARSE_PATHS[@]} -eq 0 ]]; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "--sparse requires at least one path"}' >&3
    else
        print_error "--sparse requires at least one path"
        echo ""
        echo "Example: pnpm worktree $ISSUE_NUMBER --sparse src/lib defaults/scripts"
    fi
    exit 1
fi

# Build the always-included safety set, allowing repo override via env var.
ALWAYS_INCLUDE=("${LOOM_WORKTREE_ALWAYS_INCLUDE_DEFAULT[@]}")
if [[ -n "${LOOM_WORKTREE_ALWAYS_INCLUDE:-}" ]]; then
    # Split on whitespace
    # shellcheck disable=SC2206
    EXTRA_INCLUDE=(${LOOM_WORKTREE_ALWAYS_INCLUDE})
    ALWAYS_INCLUDE+=("${EXTRA_INCLUDE[@]}")
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
            echo '{"error": "Failed to find git common directory"}' >&3
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
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Failed to change to main workspace", "mainWorkspace": "'"$MAIN_WORKSPACE"'"}' >&3
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

# ─── Concurrency lock (issue #3380) ─────────────────────────────────────────
# Serialize concurrent invocations against the same issue. The lock dir
# lives under the canonical git common dir so worktrees and the main
# workspace agree on the lock namespace.
#
# Pre-cleanup runs *before* the lock so a crashed prior run's debris (which
# would otherwise prevent us from making progress under the lock) is cleared
# regardless of whether we ultimately acquire the lock.
cleanup_partial_worktree_state "$ISSUE_NUMBER" || true

if ! acquire_worktree_lock "$ISSUE_NUMBER"; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "worktree-lock-timeout", "issueNumber": '"$ISSUE_NUMBER"', "holderPid": "'"${WORKTREE_LOCK_HOLDER_PID:-}"'", "timeoutSeconds": '"$LOOM_WORKTREE_LOCK_TIMEOUT"'}' >&3
    else
        print_error "Timed out waiting for worktree lock after ${LOOM_WORKTREE_LOCK_TIMEOUT}s"
        if [[ -n "${WORKTREE_LOCK_HOLDER_PID:-}" ]]; then
            echo "  Lock holder PID: $WORKTREE_LOCK_HOLDER_PID"
        fi
        echo "  Lock dir: $(_worktree_lock_path "$ISSUE_NUMBER")"
        echo ""
        echo "  If the holder is dead, remove the lock dir manually:"
        echo "    rm -rf '$(_worktree_lock_path "$ISSUE_NUMBER")'"
    fi
    exit 1
fi

# Release the lock on any exit path (success, failure, signal).
trap 'release_worktree_lock "$ISSUE_NUMBER"' EXIT INT TERM

# Re-run cleanup under the lock so a crashed concurrent peer (one that died
# between our pre-cleanup and our lock acquisition) is still handled.
cleanup_partial_worktree_state "$ISSUE_NUMBER" || true

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

# Resolve the repo's default branch once (cwd is now the main workspace, so
# git symbolic-ref sees refs/remotes/origin/HEAD). Hard-fail rather than proceed
# with an empty/wrong branch — an empty `origin/` refspec is worse than the
# original bug (#3549).
if ! DEFAULT_BRANCH="$(loom_default_branch)"; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "Could not determine the default branch (see stderr; set LOOM_DEFAULT_BRANCH or run: git remote set-head origin -a)"}' >&3
    else
        print_error "Could not determine the default branch. Set LOOM_DEFAULT_BRANCH or run: git remote set-head origin -a"
    fi
    exit 1
fi

# Fetch latest changes from origin/$DEFAULT_BRANCH before creating the worktree
# Uses fetch-only to avoid conflicts with worktrees that have it checked out
fetch_latest_main

# Determine branch name
if [[ -n "$CUSTOM_BRANCH" ]]; then
    BRANCH_NAME="feature/$CUSTOM_BRANCH"
else
    BRANCH_NAME="feature/issue-$ISSUE_NUMBER"
fi

# Worktree path. At this point cwd is the main workspace root (the script
# auto-navigates out of any worktree above), so REPO_ROOT is the current dir.
# loom_worktree_root returns an absolute base; when no override is configured
# it is "$REPO_ROOT/.loom/worktrees" — identical to the historical relative
# ".loom/worktrees" resolved against this same cwd.
WORKTREE_REPO_ROOT="$(pwd)"
WORKTREE_ROOT_DIR="$(loom_worktree_root "$WORKTREE_REPO_ROOT")"
# Ensure the base dir exists. `git worktree add` creates only the leaf, so an
# external override root (e.g. /Volumes/Stripe/<repo>) needs its parents made.
mkdir -p "$WORKTREE_ROOT_DIR" 2>/dev/null || true
WORKTREE_PATH="$WORKTREE_ROOT_DIR/issue-$ISSUE_NUMBER"

# Check if worktree already exists
if [[ -d "$WORKTREE_PATH" ]]; then
    # If caller passed --sparse / --full, apply the mode to the existing
    # worktree and exit. This is the idempotent path: same cone is a no-op,
    # different cone replaces the cone, --full disables sparse-checkout.
    if [[ "$SPARSE_MODE" == "true" || "$FULL_MODE" == "true" ]]; then
        if ! git worktree list | grep -q "$WORKTREE_PATH"; then
            if [[ "$JSON_OUTPUT" == "true" ]]; then
                echo '{"success": false, "error": "Directory exists but is not a registered worktree"}' >&3
            else
                print_error "Directory exists but is not a registered worktree: $WORKTREE_PATH"
            fi
            exit 1
        fi

        if [[ "$FULL_MODE" == "true" ]]; then
            disable_sparse_checkout "$WORKTREE_PATH"
            log_worktree_size "$WORKTREE_PATH" "Worktree size (full)"
            # Back-fill/refresh the Loom sentinel so re-config of an existing
            # (possibly sentinel-less) worktree stays cleanup-eligible (#3548).
            write_loom_sentinel "$WORKTREE_PATH"
            if [[ "$JSON_OUTPUT" == "true" ]]; then
                ABS_WT=$(cd "$WORKTREE_PATH" && pwd)
                echo '{"success": true, "worktreePath": "'"$ABS_WT"'", "branchName": "'"$BRANCH_NAME"'", "issueNumber": '"$ISSUE_NUMBER"', "sparse": false, "cone": []}' >&3
            else
                print_success "Worktree converted to full checkout"
                print_info "To use this worktree: cd $WORKTREE_PATH"
            fi
            exit 0
        fi

        # SPARSE_MODE
        CONE_PATHS=("${SPARSE_PATHS[@]}" "${ALWAYS_INCLUDE[@]}")
        apply_sparse_cone "$WORKTREE_PATH" "${CONE_PATHS[@]}"
        materialize_sparse_cone "$WORKTREE_PATH"
        log_worktree_size "$WORKTREE_PATH" "Worktree size (sparse)"
        # Back-fill/refresh the Loom sentinel so re-config of an existing
        # (possibly sentinel-less) worktree stays cleanup-eligible (#3548).
        write_loom_sentinel "$WORKTREE_PATH"
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            ABS_WT=$(cd "$WORKTREE_PATH" && pwd)
            CONE_JSON=$(printf '%s\n' "${CONE_PATHS[@]}" | awk 'BEGIN{printf "["} {if(NR>1)printf ","; printf "\"%s\"", $0} END{printf "]"}')
            echo '{"success": true, "worktreePath": "'"$ABS_WT"'", "branchName": "'"$BRANCH_NAME"'", "issueNumber": '"$ISSUE_NUMBER"', "sparse": true, "cone": '"$CONE_JSON"'}' >&3
        else
            print_success "Sparse-checkout cone applied"
            print_info "To use this worktree: cd $WORKTREE_PATH"
        fi
        exit 0
    fi

    print_warning "Worktree already exists at: $WORKTREE_PATH"

    # Check if it's registered with git
    if git worktree list | grep -q "$WORKTREE_PATH"; then
        # Check if worktree is stale: no commits ahead of main and behind main
        local_commits_ahead=$(git -C "$WORKTREE_PATH" rev-list --count "origin/$DEFAULT_BRANCH..HEAD" 2>/dev/null) || local_commits_ahead="0"
        local_commits_behind=$(git -C "$WORKTREE_PATH" rev-list --count "HEAD..origin/$DEFAULT_BRANCH" 2>/dev/null) || local_commits_behind="0"
        local_uncommitted=$(git -C "$WORKTREE_PATH" status --porcelain 2>/dev/null) || local_uncommitted=""

        if [[ "$local_commits_ahead" -gt 0 || -n "$local_uncommitted" ]]; then
            # Worktree has real work - preserve it
            # Back-fill/refresh the Loom sentinel so a resumed worktree that
            # lost its marker stays cleanup-eligible (#3548).
            write_loom_sentinel "$WORKTREE_PATH"
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
        else
            # Stale worktree: no commits ahead, no uncommitted changes
            # Reset in place instead of removing (avoids CWD corruption)
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Stale worktree detected (0 commits ahead, $local_commits_behind behind $DEFAULT_BRANCH, no uncommitted changes)"
                print_info "Resetting worktree in place to origin/$DEFAULT_BRANCH..."
            fi

            # Back-fill/refresh the Loom sentinel on both reset outcomes: the
            # worktree remains usable either way, so keep it cleanup-eligible
            # (#3548).
            write_loom_sentinel "$WORKTREE_PATH"
            if git -C "$WORKTREE_PATH" fetch origin "$DEFAULT_BRANCH" 2>/dev/null && \
               git -C "$WORKTREE_PATH" reset --hard "origin/$DEFAULT_BRANCH" 2>/dev/null; then
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_success "Stale worktree reset to origin/$DEFAULT_BRANCH"
                    echo ""
                    print_info "To use this worktree: cd $WORKTREE_PATH"
                fi
                exit 0
            else
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_warning "Could not reset stale worktree (continuing to use as-is)"
                    echo ""
                    print_info "To use this worktree: cd $WORKTREE_PATH"
                fi
                exit 0
            fi
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
    # Create new branch from the default branch
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Creating new branch from $DEFAULT_BRANCH"
    fi
    CREATE_ARGS=("$WORKTREE_PATH" "-b" "$BRANCH_NAME" "origin/$DEFAULT_BRANCH")
fi

# In sparse mode, defer file materialization until after we configure the cone.
if [[ "$SPARSE_MODE" == "true" ]]; then
    CREATE_ARGS=("--no-checkout" "${CREATE_ARGS[@]}")
fi

# Create the worktree
if [[ "$JSON_OUTPUT" != "true" ]]; then
    print_info "Creating worktree..."
    echo "  Path: $WORKTREE_PATH"
    echo "  Branch: $BRANCH_NAME"
    if [[ "$SPARSE_MODE" == "true" ]]; then
        echo "  Mode: sparse (cone: ${SPARSE_PATHS[*]})"
    fi
    echo ""
fi

# Helper: attempt recovery when feature branch is checked out in the main worktree.
# This happens when a previous builder manually checked out feature/issue-N in the
# main workspace and left it there.  Git refuses to create a new worktree for that
# branch: "fatal: 'feature/issue-N' is already used by worktree at '<main-path>'"
#
# Recovery strategy:
#   1. Detect the "already used by worktree at" pattern in stderr
#   2. Confirm the conflicting worktree is the main workspace (not a feature worktree)
#   3. If main workspace is clean: auto-switch it back to main and retry
#   4. If main workspace has uncommitted changes: emit an actionable error message
_handle_feature_branch_in_main_worktree() {
    local error_output="$1"
    local branch="$2"

    # Only act on the specific "already used by worktree at" error
    if ! echo "$error_output" | grep -q "is already used by worktree at"; then
        return 1  # Not this error — caller should fail normally
    fi

    # Extract the conflicting worktree path from the error message
    # Example: "fatal: 'feature/issue-2853' is already used by worktree at '/Users/rwalters/GitHub/loom'"
    local conflict_path
    conflict_path=$(echo "$error_output" | grep -o "is already used by worktree at '[^']*'" | sed "s/is already used by worktree at '//;s/'$//")

    if [[ -z "$conflict_path" ]]; then
        # Could not parse path — emit a generic actionable message (human-readable only)
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_error "Cannot create worktree: branch '$branch' is already checked out in another worktree."
            echo ""
            echo "  The branch is in use elsewhere. To free it, find the worktree with:"
            echo "    git worktree list"
            echo "  Then switch that worktree to $DEFAULT_BRANCH:"
            echo "    cd <worktree-path> && git checkout $DEFAULT_BRANCH"
        fi
        return 0  # Handled (with human-readable message), no retry possible
    fi

    # Determine the main workspace path
    local main_workspace
    main_workspace=$(git rev-parse --git-common-dir 2>/dev/null)
    main_workspace=$(dirname "$main_workspace" 2>/dev/null)

    # Resolve both paths to absolute for comparison
    local abs_conflict abs_main
    abs_conflict=$(cd "$conflict_path" 2>/dev/null && pwd) || abs_conflict="$conflict_path"
    abs_main=$(cd "$main_workspace" 2>/dev/null && pwd) || abs_main="$main_workspace"

    if [[ "$abs_conflict" != "$abs_main" ]]; then
        # Conflicting worktree is not the main workspace — it's a different issue worktree.
        # This is unusual but can happen. Emit actionable guidance without auto-recovery.
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_error "Cannot create worktree for branch '$branch':"
            echo "  Branch is already checked out at: $conflict_path"
            echo ""
            echo "  To fix:"
            echo "    cd $conflict_path && git checkout $DEFAULT_BRANCH"
        fi
        return 0  # Handled (with error message), no retry
    fi

    # The conflict is in the main workspace. Check for uncommitted changes.
    local uncommitted
    uncommitted=$(git -C "$abs_conflict" status --porcelain 2>/dev/null)

    if [[ -n "$uncommitted" ]]; then
        # Main workspace has uncommitted changes — cannot auto-recover safely
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_error "Cannot create worktree for issue #$ISSUE_NUMBER: branch '$branch'"
            echo "  is already checked out at '$abs_conflict' (main worktree)."
            echo ""
            echo "  The main worktree has uncommitted changes — cannot auto-switch."
            echo "  To fix manually:"
            echo "    cd $abs_conflict"
            echo "    git stash  # or commit your changes"
            echo "    git checkout $DEFAULT_BRANCH"
            echo "  Then rerun: ./.loom/scripts/worktree.sh $ISSUE_NUMBER"
        fi
        return 0  # Handled (with error message), no retry
    fi

    # Main workspace is clean — auto-switch to the default branch and signal
    # caller to retry.
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_warning "Branch '$branch' is checked out in the main worktree."
        print_info "Main worktree is clean — auto-switching to $DEFAULT_BRANCH branch..."
    fi

    if git -C "$abs_conflict" checkout "$DEFAULT_BRANCH" 2>/dev/null; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_success "Main worktree switched to $DEFAULT_BRANCH branch"
        fi
        return 2  # Signal: auto-recovered, caller should retry
    else
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_error "Failed to switch main worktree to $DEFAULT_BRANCH branch."
            echo "  To fix manually:"
            echo "    cd $abs_conflict && git checkout $DEFAULT_BRANCH"
            echo "  Then rerun: ./.loom/scripts/worktree.sh $ISSUE_NUMBER"
        fi
        return 0  # Handled (with error message), no retry
    fi
}

_try_worktree_add() {
    # Capture stderr separately so we can inspect it on failure while still
    # showing stdout (git progress messages like "Preparing worktree...") to user.
    local stderr_file
    stderr_file=$(mktemp /tmp/loom-worktree-stderr-$$-XXXXXX)

    git worktree add "${CREATE_ARGS[@]}" 2>"$stderr_file"
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        rm -f "$stderr_file"
        return 0
    fi

    local worktree_error
    worktree_error=$(cat "$stderr_file")
    rm -f "$stderr_file"

    # Attempt recovery for the "feature branch in main worktree" case.
    # Wrap in a subshell result capture to safely handle non-zero returns
    # without triggering set -e (we use exit code 2 as a retry signal).
    local recovery_code=0
    _handle_feature_branch_in_main_worktree "$worktree_error" "$BRANCH_NAME" && recovery_code=0 || recovery_code=$?

    if [[ $recovery_code -eq 2 ]]; then
        # Auto-recovered: retry worktree creation once
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Retrying worktree creation..."
        fi
        git worktree add "${CREATE_ARGS[@]}"
        return $?
    fi

    if [[ $recovery_code -eq 1 ]]; then
        # _handle_feature_branch_in_main_worktree returned 1 (not this error type)
        # Print the original git error since nothing else has
        echo "$worktree_error" >&2
    fi
    # recovery_code == 0 means error was handled and message already printed
    return 1
}


if _try_worktree_add; then
    # Get absolute path to worktree
    ABS_WORKTREE_PATH=$(cd "$WORKTREE_PATH" && pwd)

    # Write a sentinel marker identifying this worktree as Loom-managed.
    # Cleanup tooling (merge-pr.sh, agent-destroy.sh, loom-clean) refuses to
    # remove worktrees lacking this marker, so user-provisioned worktrees at
    # arbitrary paths are never touched by Loom. See issue #3334. The write is
    # factored into write_loom_sentinel() so every re-invocation path can
    # back-fill it too (#3548).
    write_loom_sentinel "$ABS_WORKTREE_PATH"

    # Sparse-mode: configure cone and materialize tracked files.
    # This must run before submodule init / symlinking so the working tree
    # exists and helpers see the same file layout as full mode.
    SPARSE_CONE_PATHS=()
    if [[ "$SPARSE_MODE" == "true" ]]; then
        SPARSE_CONE_PATHS=("${SPARSE_PATHS[@]}" "${ALWAYS_INCLUDE[@]}")
        apply_sparse_cone "$ABS_WORKTREE_PATH" "${SPARSE_CONE_PATHS[@]}"
        materialize_sparse_cone "$ABS_WORKTREE_PATH"
        log_worktree_size "$ABS_WORKTREE_PATH" "Sparse worktree size"
    fi

    # Set git hooks path so .githooks/ works in worktrees (no npx/husky needed).
    # Only when the repo actually ships a .githooks/ dir — otherwise pointing
    # core.hooksPath at a missing dir silently disables all hooks (git treats a
    # nonexistent hooksPath as "no hooks"). $WORKTREE_REPO_ROOT is the main repo
    # root captured at L824 (cwd is the main workspace here, not the worktree).
    if [[ -d "$WORKTREE_REPO_ROOT/.githooks" ]]; then
        git -C "$ABS_WORKTREE_PATH" config core.hooksPath .githooks
    fi

    # Store return-to directory if provided
    if [[ -n "$RETURN_TO_DIR" ]]; then
        ABS_RETURN_TO=$(cd "$RETURN_TO_DIR" && pwd)
        echo "$ABS_RETURN_TO" > "$ABS_WORKTREE_PATH/.loom-return-to"
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Stored return directory: $ABS_RETURN_TO"
        fi
    fi

    # Initialize submodules with reference to main workspace (for object sharing)
    # This is much faster than downloading from network and saves disk space.
    #
    # In sparse mode, `git submodule status` already lists only submodules
    # whose path lies inside the materialized cone -- so this loop naturally
    # filters out out-of-cone submodules without extra logic.
    #
    # Uses --recursive to handle nested submodules (a top-level submodule may
    # itself declare submodules; without --recursive those remain empty and a
    # builder sees a half-populated reference directory with no error).
    # Timeout is generous (300s) because cold clones of large reference corpora
    # without an object cache can legitimately exceed 30s. Override via
    # LOOM_SUBMODULE_TIMEOUT.
    # Stderr is preserved (not redirected to /dev/null) so the underlying git
    # error is visible to whoever runs worktree.sh -- the previous "Some
    # submodules failed to initialize" warning was a black box.
    MAIN_GIT_DIR=$(git rev-parse --git-common-dir 2>/dev/null)
    UNINIT_SUBMODULES=$(cd "$ABS_WORKTREE_PATH" && git submodule status 2>/dev/null | grep '^-' | wc -l | tr -d ' ')
    SUBMODULE_TIMEOUT="${LOOM_SUBMODULE_TIMEOUT:-300}"

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
                if ! timeout "$SUBMODULE_TIMEOUT" git submodule update --init --recursive --reference "$ref_path" -- "$submod_path"; then
                    echo "SUBMODULE_FAILED" > /tmp/loom-submodule-status-$$
                fi
            else
                # No reference available, initialize normally (may need network)
                if ! timeout "$SUBMODULE_TIMEOUT" git submodule update --init --recursive -- "$submod_path"; then
                    echo "SUBMODULE_FAILED" > /tmp/loom-submodule-status-$$
                fi
            fi
        done

        # Check if any submodule failed
        if [[ -f "/tmp/loom-submodule-status-$$" ]]; then
            rm -f "/tmp/loom-submodule-status-$$"
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Some submodules failed to initialize (worktree still created)"
                print_info "See stderr above for the underlying git error."
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

    # Resolve the info/exclude path that applies to this worktree. Running
    # `git rev-parse --git-path info/exclude` from inside the worktree returns
    # the correct file for whatever git layout is in play (info/exclude is a
    # common-dir path, so worktrees inherit the main repo's .git/info/exclude;
    # asking git rather than hardcoding a path keeps us correct across layouts).
    # Entries appended here keep `git add -A` from staging the created symlinks
    # even when the repo's .gitignore rules don't match a symlink (the classic
    # `node_modules/` dir-rule-vs-symlink hazard from #3528).
    WORKTREE_INFO_EXCLUDE=$(cd "$ABS_WORKTREE_PATH" 2>/dev/null \
        && git rev-parse --git-path info/exclude 2>/dev/null)
    if [[ -n "$WORKTREE_INFO_EXCLUDE" && "$WORKTREE_INFO_EXCLUDE" != /* ]]; then
        # git rev-parse may return a path relative to the worktree cwd; anchor it.
        WORKTREE_INFO_EXCLUDE="$ABS_WORKTREE_PATH/$WORKTREE_INFO_EXCLUDE"
    fi

    # Idempotently append a path to the worktree's info/exclude. Safe to call
    # repeatedly (grep -qxF guards against duplicate lines) and best-effort
    # (a missing exclude file just means git tracked the ignore elsewhere).
    _append_worktree_exclude() {
        local entry="$1"
        if [[ -z "$WORKTREE_INFO_EXCLUDE" ]]; then
            return 0
        fi
        mkdir -p "$(dirname "$WORKTREE_INFO_EXCLUDE")" 2>/dev/null || true
        grep -qxF "$entry" "$WORKTREE_INFO_EXCLUDE" 2>/dev/null \
            || echo "$entry" >> "$WORKTREE_INFO_EXCLUDE" 2>/dev/null || true
    }

    # Symlink nested (per-package) node_modules for pnpm/monorepo workspaces.
    # The root node_modules symlink above does not cover per-package installs
    # (e.g. apps/web/node_modules), so a fresh worktree fails typecheck/build
    # until each is linked. Directory-scan discovery (no YAML parser dependency,
    # see #3528): find node_modules dirs at shallow depth that sit next to a
    # package.json, skipping the root (already handled) and anything nested
    # inside another node_modules (avoids recursing into node_modules/.pnpm/**).
    if [[ -d "$MAIN_NODE_MODULES" ]]; then
        while IFS= read -r -d '' pkg_node_modules; do
            pkg_dir="$(dirname "$pkg_node_modules")"
            rel_path="${pkg_dir#"$MAIN_WORKSPACE_DIR"/}"
            # Skip if the prefix strip did nothing (path not under main workspace).
            if [[ "$rel_path" == "$pkg_dir" ]]; then
                continue
            fi
            # Only mirror package roots (node_modules alongside a package.json).
            if [[ ! -f "$pkg_dir/package.json" ]]; then
                continue
            fi
            worktree_pkg_dir="$ABS_WORKTREE_PATH/$rel_path"
            worktree_pkg_node_modules="$worktree_pkg_dir/node_modules"
            if [[ -d "$worktree_pkg_dir" && ! -e "$worktree_pkg_node_modules" ]]; then
                if ln -s "$pkg_node_modules" "$worktree_pkg_node_modules" 2>/dev/null; then
                    _append_worktree_exclude "$rel_path/node_modules"
                    if [[ "$JSON_OUTPUT" != "true" ]]; then
                        print_success "Symlinked $rel_path/node_modules from main workspace"
                    fi
                else
                    if [[ "$JSON_OUTPUT" != "true" ]]; then
                        print_warning "Could not symlink $rel_path/node_modules"
                    fi
                fi
            fi
        done < <(find "$MAIN_WORKSPACE_DIR" -mindepth 2 -maxdepth 3 -type d \
                    -name node_modules -not -path "*/node_modules/*" -print0 2>/dev/null)
    fi

    # Symlink additional gitignored paths configured in .loom/config.json under
    # worktree.linkPaths (e.g. generated wasm-pack bindings that are expensive
    # to rebuild per worktree). Best-effort: missing config, missing jq, malformed
    # JSON, or an empty/absent key all silently skip this step (#3528). Mirrors
    # the inline-jq-with-guard pattern from validate-roles.sh.
    LOOM_CONFIG_FILE="$MAIN_WORKSPACE_DIR/.loom/config.json"
    if command -v jq >/dev/null 2>&1 && [[ -f "$LOOM_CONFIG_FILE" ]]; then
        while IFS= read -r link_path; do
            if [[ -z "$link_path" ]]; then
                continue
            fi
            link_src="$MAIN_WORKSPACE_DIR/$link_path"
            link_dst="$ABS_WORKTREE_PATH/$link_path"
            if [[ -e "$link_src" && ! -e "$link_dst" ]]; then
                mkdir -p "$(dirname "$link_dst")" 2>/dev/null || true
                if ln -s "$link_src" "$link_dst" 2>/dev/null; then
                    _append_worktree_exclude "$link_path"
                    if [[ "$JSON_OUTPUT" != "true" ]]; then
                        print_success "Symlinked $link_path from main workspace"
                    fi
                else
                    if [[ "$JSON_OUTPUT" != "true" ]]; then
                        print_warning "Could not symlink $link_path"
                    fi
                fi
            fi
        done < <(jq -r '.worktree.linkPaths[]? // empty' "$LOOM_CONFIG_FILE" 2>/dev/null)
    fi

    # Symlink .mcp.json from main workspace if available
    # .mcp.json is gitignored so it's invisible from worktree git roots,
    # which prevents Claude Code from discovering MCP server config
    MAIN_MCP_JSON="$MAIN_WORKSPACE_DIR/.mcp.json"
    WORKTREE_MCP_JSON="$ABS_WORKTREE_PATH/.mcp.json"

    if [[ -f "$MAIN_MCP_JSON" && ! -e "$WORKTREE_MCP_JSON" ]]; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Symlinking .mcp.json from main workspace..."
        fi

        if ln -s "$MAIN_MCP_JSON" "$WORKTREE_MCP_JSON" 2>/dev/null; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_success ".mcp.json symlinked"
            fi
        else
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "Could not symlink .mcp.json"
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
        # Machine-readable JSON output. Sparse mode adds "sparse": true and
        # "cone": [...] fields; full mode keeps "sparse": false with an empty cone.
        if [[ "$SPARSE_MODE" == "true" ]]; then
            CONE_JSON=$(printf '%s\n' "${SPARSE_CONE_PATHS[@]}" | awk 'BEGIN{printf "["} {if(NR>1)printf ","; printf "\"%s\"", $0} END{printf "]"}')
            echo '{"success": true, "worktreePath": "'"$ABS_WORKTREE_PATH"'", "branchName": "'"$BRANCH_NAME"'", "issueNumber": '"$ISSUE_NUMBER"', "returnTo": "'"${ABS_RETURN_TO:-}"'", "sparse": true, "cone": '"$CONE_JSON"'}' >&3
        else
            echo '{"success": true, "worktreePath": "'"$ABS_WORKTREE_PATH"'", "branchName": "'"$BRANCH_NAME"'", "issueNumber": '"$ISSUE_NUMBER"', "returnTo": "'"${ABS_RETURN_TO:-}"'", "sparse": false, "cone": []}' >&3
        fi
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
        echo '{"success": false, "error": "Failed to create worktree"}' >&3
    fi
    # Human-readable error already printed by _try_worktree_add / _handle_feature_branch_in_main_worktree
    exit 1
fi
