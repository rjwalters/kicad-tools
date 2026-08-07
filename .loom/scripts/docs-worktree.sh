#!/usr/bin/env bash
# Loom Docs Worktree Helper - Create a managed worktree for the Guide role's
# Document Maintenance phase (issue #5413).
#
# Usage:
#   ./.loom/scripts/docs-worktree.sh [<branch-name>]
#
# Why this exists
# ---------------
# The daemon's role runner starts every scheduled role with its working
# directory set to the WORKSPACE ROOT (loom-daemon/src/role_runner.rs ->
# `cmd.current_dir(workspace_root)`), i.e. the main checkout. Every other role
# (Curator, Champion, Judge, Doctor, Auditor) only reads the forge, so that is
# harmless. The Guide's Document Maintenance phase is the one role phase that
# WRITES repository files (WORK_LOG.md / WORK_PLAN.md / README.md) -- and Loom's
# worktree-isolation guards deny exactly that:
#
#   * guard-worktree-paths.sh   denies any Edit/Write whose path resolves into
#                               the main checkout while at least one managed
#                               worktree exists (the normal state on an active
#                               host).
#   * guard-destructive-generic.sh denies the same target reached through Bash
#                               (`>`, `>>`, `tee`, `sed -i`, `cp`, `mv`), so
#                               retrying via Bash is not a workaround.
#
# So the phase could never land its docs PR under role-runner dispatch, no
# matter how the prompt was worded. The fix is not to weaken a guard: it is to
# give the phase a managed worktree to write in, exactly like every Builder.
# This helper is that worktree. It mirrors pr-worktree.sh, which solves the same
# "I need a managed worktree but there is no issue number" problem for ad-hoc PR
# branches.
#
# What it does:
#   1. Creates (or reuses + resets) a single stable worktree slot at
#      <worktree-root>/docs-guide/ -- one slot, reused every tick, so docs
#      worktrees never accumulate.
#   2. Bases it on a freshly fetched origin/<default-branch> and checks out a
#      new `docs/guide-update-<UTC timestamp>` branch (or the branch name passed
#      as $1).
#   3. Writes a `.loom-managed` sentinel so the worktree-isolation guards allow
#      writes inside it (and so Loom cleanup tooling may remove it).
#   4. Prunes local `docs/guide-update-*` branches from earlier ticks that are
#      already merged into the default branch.
#   5. Prints the absolute worktree path as the ONLY stdout line, so callers can
#      capture it: DOCS_WT="$(./.loom/scripts/docs-worktree.sh | tail -1)"
#
# Note: the reaper (loom-daemon/src/worktree_reaper.rs) only enumerates
# `issue-<N>` directories, so this slot is never reaped out from under a tick.
#
# Exit codes:
#   0 = success (worktree ready at the printed path)
#   1 = failure (error printed to stderr)
#   2 = invalid arguments

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# All human-facing chatter goes to stderr so stdout stays a clean, single-line
# machine-readable contract (the worktree path).
print_error() { echo -e "${RED}ERROR: $1${NC}" >&2; }
print_success() { echo -e "${GREEN}✓ $1${NC}" >&2; }
print_info() { echo -e "${BLUE}ℹ $1${NC}" >&2; }
print_warning() { echo -e "${YELLOW}⚠ $1${NC}" >&2; }

show_help() {
    cat <<'EOF'
Loom Docs Worktree Helper

Usage: ./.loom/scripts/docs-worktree.sh [<branch-name>]

Creates (or resets) the managed worktree the Guide role's Document Maintenance
phase writes WORK_LOG.md / WORK_PLAN.md / README.md in, at
<worktree-root>/docs-guide/, on a fresh branch off origin/<default-branch>.

Writes are impossible in the main checkout: the worktree-isolation guards
(guard-worktree-paths.sh for Edit/Write, guard-destructive-generic.sh for the
Bash write idioms) deny any path that resolves there while a managed worktree
exists. This helper supplies the managed worktree instead of disabling a guard.

Arguments:
  <branch-name>   Branch to check out (default: docs/guide-update-<UTC stamp>)

Output:
  The absolute worktree path on stdout (single line). All progress messages go
  to stderr.

Exit codes:
  0 = worktree ready at the printed path
  1 = failure
  2 = invalid arguments
EOF
}

if [[ "${1:-}" == "--help" ]] || [[ "${1:-}" == "-h" ]]; then
    show_help
    exit 0
fi

if [[ $# -gt 1 ]]; then
    print_error "Too many arguments (expected at most one branch name, got $#)"
    show_help >&2
    exit 2
fi

BRANCH_NAME="${1:-docs/guide-update-$(date -u +%Y%m%d-%H%M%S)}"

# Reject shapes that would confuse the branch-prune step or git itself. The
# `docs/guide-update-` prefix is not required (a caller may want a different
# docs branch), but whitespace / refspec metacharacters are always wrong.
if [[ -z "$BRANCH_NAME" ]] || [[ "$BRANCH_NAME" =~ [[:space:]~^:?*\[\\] ]] || [[ "$BRANCH_NAME" == -* ]]; then
    print_error "Invalid branch name: '$BRANCH_NAME'"
    exit 2
fi

# Resolve the main repo root even when invoked from a worktree. `pwd -P`
# (not `pwd`) so this matches the canonicalized paths `git worktree list
# --porcelain` reports -- on macOS, `/var` is a symlink to `/private/var`,
# so a plain `pwd` here would never string-match the registered worktree
# path below, making the "reuse an existing slot" branch always miss.
GIT_COMMON_DIR=$(git rev-parse --git-common-dir 2>/dev/null) || {
    print_error "Not in a git repository"
    exit 1
}
REPO_ROOT=$(cd "$(dirname "$GIT_COMMON_DIR")" && pwd -P)

# Shared worktree-root resolver (#3530): honors LOOM_WORKTREE_ROOT /
# worktree.root, otherwise "$REPO_ROOT/.loom/worktrees".
# shellcheck source=lib/worktree-root.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/worktree-root.sh"
WORKTREE_ROOT_DIR="$(loom_worktree_root "$REPO_ROOT")"

# Shared default-branch resolver (#3549): never hardcode origin/main.
# shellcheck source=lib/default-branch.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/default-branch.sh"
if ! DEFAULT_BRANCH="$(cd "$REPO_ROOT" && loom_default_branch)"; then
    print_error "Could not determine the default branch. Set LOOM_DEFAULT_BRANCH or run: git remote set-head origin -a"
    exit 1
fi

WORKTREE_PATH="$WORKTREE_ROOT_DIR/docs-guide"

# Write the sentinel that authorizes writes inside this worktree (and cleanup of
# it). Written on every path, before any content mutation, so a tick that dies
# midway still leaves a worktree the guards recognize.
write_sentinel() {
    cat > "$WORKTREE_PATH/.loom-managed" <<EOF
# Loom-managed worktree marker
# Created by .loom/scripts/docs-worktree.sh
# Purpose: Guide role Document Maintenance phase (WORK_LOG / WORK_PLAN / README)
# Branch: $BRANCH_NAME
# Removing this file makes Loom treat the worktree as user-owned and refuse
# to clean it up automatically -- and makes the worktree-isolation guards deny
# writes inside it, which breaks the Document Maintenance phase.
EOF
}

mkdir -p "$WORKTREE_ROOT_DIR"

# Fetch so the new branch is based on current origin/<default-branch> rather
# than whatever this checkout last saw.
if ! git -C "$REPO_ROOT" fetch origin "$DEFAULT_BRANCH" >/dev/null 2>&1; then
    print_warning "Could not fetch origin/$DEFAULT_BRANCH (continuing with the local ref)"
fi

if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "origin/$DEFAULT_BRANCH" >/dev/null 2>&1; then
    print_error "origin/$DEFAULT_BRANCH does not exist locally and could not be fetched"
    exit 1
fi

if [[ -d "$WORKTREE_PATH" ]]; then
    # `git worktree list` prints "<path> <sha> [branch]"; match the path field
    # exactly so a similarly-prefixed sibling never counts as a hit.
    if git -C "$REPO_ROOT" worktree list --porcelain | grep -qxF "worktree $WORKTREE_PATH"; then
        print_info "Reusing docs worktree at $WORKTREE_PATH"
        write_sentinel
        # Discard whatever a previous tick left behind: this slot is scratch
        # space owned solely by the Guide, never a user's working tree.
        git -C "$WORKTREE_PATH" reset --hard --quiet HEAD 2>/dev/null || true
        git -C "$WORKTREE_PATH" clean -qfd -e .loom-managed 2>/dev/null || true
    else
        print_error "Directory exists but is not a registered worktree: $WORKTREE_PATH"
        print_info "Remove it and retry: rm -rf '$WORKTREE_PATH'"
        exit 1
    fi
else
    print_info "Creating docs worktree at $WORKTREE_PATH"
    # --detach first (never fails on a pre-existing branch ref); the branch is
    # created by the checkout -B below.
    if ! git -C "$REPO_ROOT" worktree add --detach "$WORKTREE_PATH" "origin/$DEFAULT_BRANCH" >/dev/null 2>&1; then
        print_error "Failed to create worktree at $WORKTREE_PATH"
        exit 1
    fi
    write_sentinel
fi

# Point the slot at a fresh branch off origin/<default-branch>. `-B` resets an
# existing branch of the same name, so a retried tick is idempotent.
if ! git -C "$WORKTREE_PATH" checkout -B "$BRANCH_NAME" "origin/$DEFAULT_BRANCH" >/dev/null 2>&1; then
    print_error "Failed to check out '$BRANCH_NAME' from origin/$DEFAULT_BRANCH in $WORKTREE_PATH"
    exit 1
fi

# Symlink .mcp.json so MCP servers work in the docs worktree (same pattern as
# worktree.sh / pr-worktree.sh).
if [[ -f "$REPO_ROOT/.mcp.json" && ! -e "$WORKTREE_PATH/.mcp.json" ]]; then
    ln -s "$REPO_ROOT/.mcp.json" "$WORKTREE_PATH/.mcp.json" 2>/dev/null || true
fi

# Prune local docs branches from earlier ticks that already merged. `-d`
# (not `-D`) refuses anything unmerged, so an in-flight docs PR is never
# deleted, and the currently checked-out branch is skipped by git itself.
while IFS= read -r stale; do
    [[ -n "$stale" ]] || continue
    [[ "$stale" == "$BRANCH_NAME" ]] && continue
    git -C "$REPO_ROOT" branch -d "$stale" >/dev/null 2>&1 || true
done < <(git -C "$REPO_ROOT" for-each-ref --format='%(refname:short)' 'refs/heads/docs/guide-update-*' 2>/dev/null || true)

print_success "Docs worktree ready on branch '$BRANCH_NAME'"
echo "$WORKTREE_PATH"
