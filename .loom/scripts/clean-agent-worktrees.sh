#!/bin/bash
# clean-agent-worktrees.sh - Clean up stale .claude/worktrees/agent-* worktrees and branches
#
# The Claude Agent SDK's `isolation: "worktree"` option creates worktrees at
# `.claude/worktrees/agent-<id>` with branches named `worktree-agent-<id>`.
# When agents are stopped via TaskStop, crash, or otherwise exit ungracefully,
# they leave the worktree marked `locked` (so `git worktree remove` fails) and
# the branch behind. `loom-clean` (the upstream Python tool) only handles
# `feature/issue-N` branches and `.loom/worktrees/issue-N` paths, so these
# `agent-*` artifacts accumulate over time.
#
# This script provides a project-local cleanup pass that:
#   - Lists `.claude/worktrees/agent-*` directories
#   - Skips any that look active (PID holding CWD inside OR mtime < age threshold)
#   - For the rest, runs `git worktree remove -f -f <path>` to defeat the lock
#   - Deletes the corresponding `worktree-agent-<id>` branch
#   - Cleans up orphaned `worktree-agent-*` branches whose worktree is already gone
#   - Runs `git worktree prune` and prints a summary
#
# Usage:
#   clean-agent-worktrees.sh [--dry-run] [--age-hours N] [--strict] [--help]
#
# Examples:
#   clean-agent-worktrees.sh --dry-run           # Preview only
#   clean-agent-worktrees.sh                     # Clean unused worktrees
#                                                # (skip only if recent AND active)
#   clean-agent-worktrees.sh --strict            # Skip if recent OR active
#                                                # (conservative)
#   clean-agent-worktrees.sh --age-hours 6       # Treat <6h as "recent"
#
# Safety model:
#   The PID gate (via `lsof +d`) is the primary safety signal: a worktree with
#   any process holding CWD inside it is NEVER removed.
#
#   The age gate is secondary. By default, "recent" worktrees are still
#   removed if they have no active PIDs (an agent that exited an hour ago is
#   not coming back). With --strict, recent worktrees are kept even if idle.
#
#   The default age threshold is 1 hour; this only matters with --strict.
#   Without --strict, age never causes a skip; lsof alone decides.
#
# Future work (out of scope for this script):
#   - Wire into Claude Agent SDK stop hook to clean per-session rather than
#     requiring this bulk pass. That requires SDK changes outside this repo.

set -euo pipefail

# --- Logging helpers (match style of other .loom/scripts/*.sh) ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]${NC} $*" >&2; }
log_success() { echo -e "${GREEN}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ✓${NC} $*" >&2; }
log_warn()    { echo -e "${YELLOW}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ⚠${NC} $*" >&2; }
log_error()   { echo -e "${RED}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ✗${NC} $*" >&2; }

# Find repo root by walking up from CWD
find_repo_root() {
    local dir="${1:-$PWD}"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

show_help() {
    cat <<EOF
${BLUE}clean-agent-worktrees.sh - Clean up stale .claude/worktrees/agent-* artifacts${NC}

${YELLOW}USAGE:${NC}
    clean-agent-worktrees.sh [OPTIONS]

${YELLOW}OPTIONS:${NC}
    --dry-run            Show what would be removed without doing it
    --age-hours N        Threshold (in hours) below which a worktree is
                         considered "recent". Default: 1. Only matters with
                         --strict.
    --strict             Skip a worktree if it is recent OR has active PIDs.
                         Without --strict, only the PID gate causes a skip
                         (recent-but-idle worktrees are removed).
    --help, -h           Show this help

${YELLOW}EXAMPLES:${NC}
    # Preview
    clean-agent-worktrees.sh --dry-run

    # Default: clean any worktree with no active PIDs
    clean-agent-worktrees.sh

    # Conservative: also keep anything modified in the last 1h
    clean-agent-worktrees.sh --strict

    # Even more conservative: keep anything modified in last 24h
    clean-agent-worktrees.sh --strict --age-hours 24

${YELLOW}SAFETY:${NC}
    The PID gate (lsof +d) is the primary safety signal: a worktree with any
    process holding CWD inside it is NEVER removed. The age gate is only
    consulted in --strict mode.
EOF
}

# --- Arg parsing ---
DRY_RUN=false
STRICT=false
AGE_HOURS=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --strict)  STRICT=true; shift ;;
        --age-hours)
            shift
            if [[ $# -lt 1 ]]; then
                log_error "--age-hours requires a value"
                exit 1
            fi
            AGE_HOURS="$1"
            if ! [[ "$AGE_HOURS" =~ ^[0-9]+$ ]]; then
                log_error "--age-hours must be a non-negative integer, got: $AGE_HOURS"
                exit 1
            fi
            shift
            ;;
        --help|-h) show_help; exit 0 ;;
        *) log_error "Unknown argument: $1"; show_help; exit 1 ;;
    esac
done

# --- Locate repo + agent worktrees dir ---
REPO_ROOT=$(find_repo_root) || {
    log_error "Not inside a git repository"
    exit 1
}

AGENT_WT_DIR="$REPO_ROOT/.claude/worktrees"

# Counters
removed_wts=0
removed_branches=0
skipped_active=0
skipped_age=0
orphan_branches_removed=0

if $DRY_RUN; then
    log_info "DRY-RUN MODE: no changes will be made"
fi
log_info "Repo root: $REPO_ROOT"
log_info "Agent worktree dir: $AGENT_WT_DIR"
if $STRICT; then
    log_info "Mode: strict (skip if recent OR active; --age-hours=${AGE_HOURS})"
else
    log_info "Mode: default (skip only if active; --age-hours ignored)"
fi

# Threshold in seconds
AGE_SECS=$(( AGE_HOURS * 3600 ))
NOW=$(date +%s)

# --- Pass 1: walk .claude/worktrees/agent-* directories ---
if [[ -d "$AGENT_WT_DIR" ]]; then
    # Use a glob; nullglob via shell option
    shopt -s nullglob
    for wt_path in "$AGENT_WT_DIR"/agent-*; do
        # Only act on directories
        [[ -d "$wt_path" ]] || continue

        # Derive agent id and branch name
        agent_id=$(basename "$wt_path")           # e.g. agent-a05ee94e4f37111c2
        branch_name="worktree-${agent_id}"        # e.g. worktree-agent-a05ee94e4f37111c2

        # mtime check (use directory's own mtime). stat differs on macOS vs Linux.
        if mtime=$(stat -f %m "$wt_path" 2>/dev/null); then
            : # macOS
        elif mtime=$(stat -c %Y "$wt_path" 2>/dev/null); then
            : # Linux
        else
            log_warn "Could not stat $wt_path; skipping"
            continue
        fi
        age_secs=$(( NOW - mtime ))

        # PID gate: see if any process holds CWD inside this worktree
        active_pids=""
        if command -v lsof >/dev/null 2>&1; then
            # `lsof +d` lists open files at one directory level; we only need to
            # know that *something* references the directory. Use exit status.
            # We also use -F pt so output is parseable.
            active_pids=$(lsof +d "$wt_path" -F pt 2>/dev/null \
                | awk '/^p/{pid=substr($0,2)} /^tcwd/{print pid}' \
                | sort -u \
                | tr '\n' ' ' \
                | sed 's/ $//' || true)
        fi

        if [[ -n "$active_pids" ]]; then
            log_warn "SKIP (active PIDs: $active_pids): $wt_path"
            skipped_active=$(( skipped_active + 1 ))
            continue
        fi

        # Only honor age gate in --strict mode. The PID gate above is the
        # primary safety check; an idle worktree is safe to remove regardless
        # of when it was last touched.
        if $STRICT && (( AGE_SECS > 0 )) && (( age_secs < AGE_SECS )); then
            human_hours=$(( age_secs / 3600 ))
            log_warn "SKIP (--strict, age ${human_hours}h < ${AGE_HOURS}h): $wt_path"
            skipped_age=$(( skipped_age + 1 ))
            continue
        fi

        # OK to remove
        if $DRY_RUN; then
            log_info "would: git worktree remove -f -f $wt_path"
            log_info "would: git branch -D $branch_name"
            removed_wts=$(( removed_wts + 1 ))
            if git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$branch_name"; then
                removed_branches=$(( removed_branches + 1 ))
            fi
        else
            log_info "Removing worktree: $wt_path"
            if git -C "$REPO_ROOT" worktree remove -f -f "$wt_path" 2>/dev/null; then
                removed_wts=$(( removed_wts + 1 ))
            else
                # Worktree may already be invalid (dir present but git lost track).
                # Try to clean by removing dir directly.
                log_warn "git worktree remove failed; removing directory manually"
                rm -rf "$wt_path" || {
                    log_error "Failed to remove $wt_path"
                    continue
                }
                removed_wts=$(( removed_wts + 1 ))
            fi

            # Delete the branch if it still exists
            if git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$branch_name"; then
                if git -C "$REPO_ROOT" branch -D "$branch_name" >/dev/null 2>&1; then
                    removed_branches=$(( removed_branches + 1 ))
                else
                    log_warn "Failed to delete branch: $branch_name"
                fi
            fi
        fi
    done
    shopt -u nullglob
else
    log_info "No $AGENT_WT_DIR directory; nothing to walk."
fi

# --- Pass 2: orphaned worktree-agent-* branches (worktree already gone) ---
# These come from earlier partial cleanups that pruned the worktree but missed
# the branch.
while IFS= read -r b; do
    # Strip leading whitespace and possible markers
    branch_name=$(echo "$b" | sed 's/^[* +]*//' | awk '{print $1}')
    [[ -n "$branch_name" ]] || continue

    agent_id=${branch_name#worktree-}              # e.g. agent-a05ee...
    wt_path="$AGENT_WT_DIR/$agent_id"

    if [[ -d "$wt_path" ]]; then
        # Worktree still present; handled in pass 1 (or skipped). Don't touch.
        continue
    fi

    if $DRY_RUN; then
        log_info "would: git branch -D $branch_name (orphan, no worktree)"
        orphan_branches_removed=$(( orphan_branches_removed + 1 ))
    else
        if git -C "$REPO_ROOT" branch -D "$branch_name" >/dev/null 2>&1; then
            orphan_branches_removed=$(( orphan_branches_removed + 1 ))
        fi
    fi
done < <(git -C "$REPO_ROOT" branch --list 'worktree-agent-*' 2>/dev/null || true)

# --- Pass 3: prune git's worktree admin records ---
if ! $DRY_RUN; then
    git -C "$REPO_ROOT" worktree prune 2>/dev/null || true
fi

# --- Summary ---
echo
log_success "Summary:"
log_info "  Worktrees removed:        $removed_wts"
log_info "  Matching branches removed: $removed_branches"
log_info "  Orphan branches removed:   $orphan_branches_removed"
log_info "  Skipped (active PIDs):     $skipped_active"
log_info "  Skipped (too recent):      $skipped_age"

if $DRY_RUN; then
    log_info "(dry-run — re-run without --dry-run to actually clean)"
fi
