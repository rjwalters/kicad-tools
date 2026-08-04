#!/bin/bash

# Loom Worktree Helper Script
# Safely creates and manages git worktrees for agent development
#
# Usage:
#   pnpm worktree <issue-number>                       # Create worktree for issue
#   pnpm worktree <issue-number> <branch>              # Create worktree with custom branch name
#   pnpm worktree <issue-number> --sparse <paths...>   # Cone-mode sparse checkout
#   pnpm worktree <issue-number> --full                # Convert sparse worktree to full
#   pnpm worktree remove <issue-number> [--keep-branch] [--force]  # Remove one managed worktree
#   pnpm worktree snapshot <issue-number> [--include-untracked] [--json]
#     # Write a patch file capturing the worktree's uncommitted diff to
#     # <worktree-root>/.snapshots/issue-<N>-<UTC-timestamp>.patch — WITHOUT
#     # touching `git stash` (which is repo-global and can be clobbered by a
#     # concurrent builder in another worktree). Replay with `git apply`.
#   pnpm worktree stash-push <issue-number> [--include-untracked] [--json]
#   pnpm worktree stash-pop <issue-number> [--json]
#     # Clean-and-restore pair for a "clean baseline vs my diff" comparison
#     # (clippy/shellcheck/test baseline diffing) — WITHOUT touching the
#     # shared `refs/stash` stack. Anchors captured WIP to a PER-ISSUE ref
#     # (refs/loom/stash-baseline/issue-<N>) instead, so no other worktree's
#     # concurrent stash op can ever land "in between" push and pop (#5217).
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
# Operator-facing single-worktree removal (issue #3769)
# --------------------------------------------------------------------------
#
# `worktree.sh remove <N>` (alias `--remove <N>`) is the sanctioned path for an
# operator to remove exactly one managed worktree on demand — e.g. a dead
# builder's stale checkout that pushed nothing and needs to be re-created off an
# updated base. Before this verb existed, the only single-worktree removal was
# `git worktree remove` directly, which CLAUDE.md forbids because running it
# while the shell is inside/near the worktree corrupts shell state.
#
# The guard order deliberately mirrors merge-pr.sh's private
# `_remove_loom_worktree()` (defaults/scripts/merge-pr.sh:1129-1199), scoped to
# the `issue-<N>` path convention only (no --worktree-path override, no
# discovery fallback — those belong to merge-pr.sh's distinct call-sites):
#   1. Idempotent no-op if the worktree dir is absent (still prune).
#   2. Refuse to remove a dir lacking the .loom-managed sentinel (user-owned).
#   3. Refuse to remove a worktree with uncommitted changes unless --force (#4449).
#   4. Discover the attached branch BEFORE removal (the porcelain entry vanishes
#      once the worktree is gone).
#   5. Hop out of the worktree first if our cwd is inside it (CWD-safety).
#   6. `git worktree remove --force`; warn (don't hard-fail) on failure.
#   7. `git branch -d` the attached branch (safe delete, refuses on unmerged
#      commits) unless --keep-branch.
#   8. `git worktree prune`.
#
# Guard 3 exists because step 6 is `git worktree remove --force`, which discards
# the working tree unconditionally — there is no "safe" variant to fall back to
# once it runs. #4449 is the live precedent for why an unconditional destructive
# removal is unacceptable: a tested-but-uncommitted fix was destroyed in the
# window before its `git commit`, with no dirty-check anywhere on the path. The
# create path already preserves a dirty worktree (see the "Worktree has
# uncommitted changes - preserving existing work" branch); this makes the removal
# path consistent with it, and `--force` is the explicit opt-in to the loss.
#
# `loom-clean` remains the bulk/stale-cleanup path across all closed issues;
# this verb targets one specific issue's worktree.

# Print the short branch name attached to a worktree path, parsed from
# `git worktree list --porcelain`. Robust to custom branch names (worktree.sh
# <N> <custom-branch> allows a non-`feature/issue-<N>` branch). Mirrors
# merge-pr.sh's _worktree_branch_for(). Prints nothing for a detached/bare
# worktree or on error.
_worktree_attached_branch() {
    local repo_root="$1" target="$2" target_abs
    target_abs="$(cd "$target" 2>/dev/null && pwd -P)" || target_abs="$target"
    # The `worktree ` path line (prefix = 9 chars) may contain spaces, so parse
    # it with substr($0, 10) rather than $2. The `branch ` line is safe with $2
    # (git ref names cannot contain spaces).
    git -C "$repo_root" worktree list --porcelain 2>/dev/null | \
        awk -v p="$target_abs" '
            /^worktree / { wt=substr($0, 10); br=""; next }
            /^branch /   { br=$2 }
            /^$/         { if (wt == p && br != "" && !found) { sub(/^refs\/heads\//, "", br); print br; found=1; exit } }
            END          { if (wt == p && br != "" && !found) { sub(/^refs\/heads\//, "", br); print br } }
        '
}

# Print a worktree's uncommitted-change lines in `git status --porcelain`
# format, EXCLUDING Loom runtime marker files (#4449).
#
# `.loom-managed` / `.loom-in-use` / `.loom-checkpoint` / `.no-changes-needed` are
# runtime breadcrumbs every managed worktree legitimately carries. A correctly
# installed repo gitignores them, but a stale / pre-#3838 `.gitignore` does not —
# and if they counted as "uncommitted work", the dirty guard below would refuse
# to remove *every* managed worktree, which is worse than no guard at all. They
# carry no work, so they are filtered out here rather than special-cased at each
# call site.
#
# Empty output ⇒ nothing worth preserving. Never fails (a non-repo path or a
# missing git prints nothing).
_worktree_dirty_lines() {
    local wt="$1"
    git -C "$wt" status --porcelain --untracked-files=all 2>/dev/null | awk '
        {
            # Porcelain v1: 2 status chars + 1 space, then the path. Renames
            # render as "old -> new" and never match a bare marker name.
            path = substr($0, 4)
            gsub(/^"/, "", path); gsub(/"$/, "", path)
            if (path == ".loom-managed"      || path == ".loom-in-use" ||
                path == ".loom-checkpoint"   || path == ".no-changes-needed") next
            print
        }
    ' || true
}

# Whether $branch has a MERGED pull request on the forge (#5177 / #4889).
#
# This repo squash-merges, so once a PR lands, the branch's original commits are
# never reachable from the squash commit on main — `git branch -d`'s "fully
# merged" safety check therefore refuses to delete a genuinely-landed branch.
# When the forge confirms the PR merged, the work IS landed and `git branch -D`
# is safe, mirroring merge-pr.sh's existing squash-aware `-d`→`-D` fallback.
#
# Fail-closed: a missing gh, any gh error, or an empty result all return
# non-zero ("not merged"), so a probe failure never escalates to a force-delete.
_worktree_pr_is_merged() {
    local repo_root="$1" branch="$2" count
    [[ -n "$branch" ]] || return 1
    command -v gh >/dev/null 2>&1 || return 1
    count="$( (cd "$repo_root" 2>/dev/null && \
        gh pr list --head "$branch" --state merged --json number --jq 'length' 2>/dev/null) )" || return 1
    [[ -n "$count" && "$count" != "0" ]]
}

# remove_worktree_command [--keep-branch] [--force] [--json] <issue-number>
#
# Invoked from the early arg dispatch below. Returns 0 on success (including the
# idempotent no-op) and 1 on refusal / usage error / removal failure.
remove_worktree_command() {
    local issue_number="" keep_branch=false json=false force=false
    local usage="Usage: pnpm worktree remove <issue-number> [--keep-branch] [--force] [--json]"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --keep-branch) keep_branch=true; shift ;;
            --json)        json=true; shift ;;
            --force|-f)    force=true; shift ;;
            --*)
                print_error "Unknown flag for remove: $1"
                echo ""
                echo "$usage"
                return 1
                ;;
            *)
                if [[ -z "$issue_number" ]]; then
                    issue_number="$1"; shift
                else
                    print_error "Unexpected argument: $1"
                    return 1
                fi
                ;;
        esac
    done

    if [[ -z "$issue_number" ]]; then
        print_error "remove requires an issue number"
        echo ""
        echo "$usage"
        return 1
    fi
    if ! [[ "$issue_number" =~ ^[0-9]+$ ]]; then
        print_error "Issue number must be numeric (got: '$issue_number')"
        echo ""
        echo "$usage"
        return 1
    fi

    # In --json mode, human-readable status goes to stderr so stdout carries
    # only the final JSON document (stdout-purity, mirrors the main script's
    # fd-3 plumbing). print_error already writes to stderr, safe in both modes.
    _rm_info()    { if [[ "$json" == true ]]; then echo -e "${BLUE}ℹ $*${NC}" >&2; else print_info "$*"; fi; }
    _rm_success() { if [[ "$json" == true ]]; then echo -e "${GREEN}✓ $*${NC}" >&2; else print_success "$*"; fi; }
    _rm_warning() { if [[ "$json" == true ]]; then echo -e "${YELLOW}⚠ $*${NC}" >&2; else print_warning "$*"; fi; }
    _rm_json() {
        # $1=success(bool) $2=removed(bool) $3=branchStatus
        [[ "$json" == true ]] || return 0
        printf '{"success": %s, "issueNumber": %s, "worktreePath": "%s", "removed": %s, "branch": "%s", "branchStatus": "%s"}\n' \
            "$1" "$issue_number" "$worktree_path" "$2" "${attached_branch:-}" "$3"
    }

    # Resolve the repo root even when invoked from inside a worktree: the git
    # common dir's parent is always the main workspace.
    local git_common repo_root
    if ! git_common=$(git rev-parse --git-common-dir 2>/dev/null); then
        print_error "Not inside a git repository"
        return 1
    fi
    repo_root=$(cd "$(dirname "$git_common")" 2>/dev/null && pwd) || repo_root="$(pwd)"

    local worktree_root_dir worktree_path
    worktree_root_dir="$(loom_worktree_root "$repo_root")"
    worktree_path="$worktree_root_dir/issue-$issue_number"
    local attached_branch=""

    # 1. Idempotent no-op if the worktree dir is absent (still prune any stale
    #    registration, matching the "prunes git worktree registration" AC).
    if [[ ! -d "$worktree_path" ]]; then
        git -C "$repo_root" worktree prune 2>/dev/null || true
        _rm_info "No worktree found at $worktree_path — nothing to remove"
        _rm_json true false "absent"
        return 0
    fi

    # 2. Sentinel guard: refuse to remove a user-owned / non-managed worktree.
    if [[ ! -f "$worktree_path/.loom-managed" ]]; then
        print_error "Worktree at $worktree_path lacks .loom-managed sentinel — refusing to remove (user-owned)"
        _rm_json false false "untouched"
        return 1
    fi

    # 3. Dirty guard (#4449): step 5's `git worktree remove --force` discards the
    #    working tree unconditionally, so uncommitted work must be surfaced and
    #    the removal refused unless the caller explicitly opts into the loss.
    #    Defense in depth alongside the create path, which already preserves a
    #    dirty worktree rather than resetting it.
    local dirty_lines dirty_count
    dirty_lines="$(_worktree_dirty_lines "$worktree_path")"
    if [[ -n "$dirty_lines" ]]; then
        dirty_count=$(printf '%s\n' "$dirty_lines" | grep -c . || true)
        if [[ "$force" != true ]]; then
            print_error "Refusing to remove $worktree_path — it has $dirty_count uncommitted change(s):"
            printf '%s\n' "$dirty_lines" | head -20 >&2
            if [[ "$dirty_count" -gt 20 ]]; then
                echo "  ... and $((dirty_count - 20)) more" >&2
            fi
            echo "" >&2
            echo "Removing it would destroy that work irreversibly. To proceed, pick one:" >&2
            echo "  1. Commit it:    git -C $worktree_path add -A && git -C $worktree_path commit -m '...'" >&2
            echo "  2. Save a patch: git -C $worktree_path diff HEAD > /tmp/issue-$issue_number.patch" >&2
            echo "  3. Stash it:     git -C $worktree_path stash push -u -m 'issue-$issue_number'" >&2
            echo "  4. Discard it:   re-run with --force (the uncommitted changes are lost)" >&2
            _rm_json false false "untouched"
            return 1
        fi
        _rm_warning "Worktree has $dirty_count uncommitted change(s) - discarding them (--force)"
        printf '%s\n' "$dirty_lines" | head -20 >&2
    fi

    # 4. Discover the attached branch BEFORE removal (porcelain entry vanishes
    #    once the worktree is gone).
    attached_branch="$(_worktree_attached_branch "$repo_root" "$worktree_path")" || attached_branch=""

    # 5. CWD-safety: if our shell is inside the worktree, hop out first.
    local worktree_real current_dir in_worktree=false
    worktree_real="$(cd "$worktree_path" 2>/dev/null && pwd -P)" || worktree_real="$worktree_path"
    current_dir="$(pwd -P 2>/dev/null || pwd)"
    if [[ "$current_dir" == "$worktree_real"* ]]; then
        in_worktree=true
        cd "$repo_root" 2>/dev/null || true
    fi

    # 6. Remove the worktree.
    _rm_info "Removing worktree: $worktree_path"
    local removed=false remove_err
    if remove_err="$(git -C "$repo_root" worktree remove "$worktree_path" --force 2>&1)"; then
        removed=true
        _rm_success "Worktree removed"
        if [[ "$in_worktree" == true ]]; then
            _rm_warning "Your shell's working directory was inside the removed worktree."
            _rm_warning "Run this command to fix:  cd $repo_root"
        fi
    elif printf '%s' "$remove_err" | grep -qi "is not a working tree" && \
         [[ -f "$worktree_path/.loom-managed" ]]; then
        # #5177: git no longer tracks this path as a worktree (e.g. a stale
        # `git worktree prune` left the directory on disk), so `git worktree
        # remove` can never clean it and it accumulates forever. It is confirmed
        # Loom-managed (the step-2 sentinel guard is re-checked here) and is by
        # construction under the managed worktree root ($worktree_root_dir/issue-N),
        # so remove the directory directly and prune the dangling registration.
        if rm -rf "$worktree_path"; then
            removed=true
            _rm_success "Removed untracked worktree directory (no git worktree entry)"
        else
            _rm_warning "Could not remove untracked worktree directory at $worktree_path"
        fi
    else
        _rm_warning "Could not remove worktree at $worktree_path"
    fi

    # 7. Branch cleanup (unless --keep-branch). Deferred until after removal so
    #    the worktree's checkout lock on the branch is released first.
    local branch_status="none"
    if [[ "$keep_branch" == true ]]; then
        if [[ -n "$attached_branch" ]]; then
            _rm_info "Keeping local branch '$attached_branch' (--keep-branch)"
            branch_status="kept"
        fi
    elif [[ "$removed" == true && -n "$attached_branch" ]]; then
        if ! git -C "$repo_root" show-ref --verify --quiet "refs/heads/$attached_branch"; then
            _rm_info "Local branch '$attached_branch' does not exist — skipping branch delete"
            branch_status="absent"
        elif git -C "$repo_root" branch -d "$attached_branch" >/dev/null 2>&1; then
            _rm_success "Local branch '$attached_branch' deleted"
            branch_status="deleted"
        elif _worktree_pr_is_merged "$repo_root" "$attached_branch"; then
            # #5177 / #4889: `git branch -d` refused because a squash-merged
            # branch is never "fully merged" by reachability — but the forge
            # confirms its PR merged, so the work is landed and -D is safe.
            if git -C "$repo_root" branch -D "$attached_branch" >/dev/null 2>&1; then
                _rm_success "Local branch '$attached_branch' force-deleted (PR merged — squash-safe)"
                branch_status="deleted"
            else
                _rm_warning "Could not delete local branch '$attached_branch' even after confirming its PR merged"
                branch_status="unmerged"
            fi
        else
            _rm_warning "Could not delete local branch '$attached_branch' (may have unmerged commits — use 'git branch -D' if intentional)"
            branch_status="unmerged"
        fi
    fi

    # 8. Prune the git worktree registration.
    git -C "$repo_root" worktree prune 2>/dev/null || true

    if [[ "$removed" == true ]]; then
        _rm_json true true "$branch_status"
        return 0
    else
        _rm_json false false "$branch_status"
        return 1
    fi
}

# --------------------------------------------------------------------------
# Worktree-scoped snapshot (issue #4778)
# --------------------------------------------------------------------------
#
# `worktree.sh snapshot <N>` captures a worktree's uncommitted WIP as a
# standalone patch file WITHOUT touching `git stash` at all. `git stash` is
# repo-global across worktrees — one shared stash list for the whole repo —
# so two builders stashing around the same time in different `issue-<N>`
# worktrees can pop/clobber each other's WIP (documented cross-worktree
# contamination class). A patch file is inherently per-invocation and
# per-path: there is no shared mutable list to collide on.
#
# Deterministic, discoverable location — resolved through the SAME
# loom_worktree_root() the rest of this script uses, so an overridden
# LOOM_WORKTREE_ROOT / worktree.root config redirects snapshots along with
# worktrees rather than falling back to a hardcoded `.loom/worktrees` path:
#
#   $(loom_worktree_root <repo_root>)/.snapshots/issue-<N>-<UTC-timestamp>.patch
#
# This is deliberately the same family as check-main-clean.sh's --quarantine
# rescue path: both produce a "replay this diff inside your worktree"
# artifact, and both are named/labeled by issue for attribution. They differ
# in mechanism on purpose: check-main-clean.sh rescues contamination that
# leaked into the shared MAIN checkout (its whole point is a repo-global
# stash ref, because the dirt itself is repo-global); `snapshot` rescues one
# worktree's own WIP (its whole point is per-path isolation, because the
# thing it's defending against IS the shared stash list). Replay contract for
# both is the same: `git apply <patch>` against a fresh checkout reproduces
# the captured diff.
snapshot_worktree_command() {
    local issue_number="" json=false include_untracked=false
    local usage="Usage: pnpm worktree snapshot <issue-number> [--include-untracked] [--json]"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --include-untracked) include_untracked=true; shift ;;
            --json)               json=true; shift ;;
            --*)
                print_error "Unknown flag for snapshot: $1"
                echo ""
                echo "$usage"
                return 1
                ;;
            *)
                if [[ -z "$issue_number" ]]; then
                    issue_number="$1"; shift
                else
                    print_error "Unexpected argument: $1"
                    return 1
                fi
                ;;
        esac
    done

    if [[ -z "$issue_number" ]]; then
        print_error "snapshot requires an issue number"
        echo ""
        echo "$usage"
        return 1
    fi
    if ! [[ "$issue_number" =~ ^[0-9]+$ ]]; then
        print_error "Issue number must be numeric (got: '$issue_number')"
        echo ""
        echo "$usage"
        return 1
    fi

    # Mirrors remove_worktree_command's stdout-purity split: in --json mode
    # human-readable status goes to stderr so stdout carries only the final
    # JSON document.
    _snap_info()    { if [[ "$json" == true ]]; then echo -e "${BLUE}ℹ $*${NC}" >&2; else print_info "$*"; fi; }
    _snap_success() { if [[ "$json" == true ]]; then echo -e "${GREEN}✓ $*${NC}" >&2; else print_success "$*"; fi; }
    _snap_json() {
        # $1=success(bool) $2=patchPath $3=hasChanges(bool) $4=bytes
        [[ "$json" == true ]] || return 0
        printf '{"success": %s, "issueNumber": %s, "patchPath": "%s", "hasChanges": %s, "bytes": %s}\n' \
            "$1" "$issue_number" "$2" "$3" "$4"
    }

    # Resolve the repo root even when invoked from inside a worktree: the git
    # common dir's parent is always the main workspace.
    local git_common repo_root
    if ! git_common=$(git rev-parse --git-common-dir 2>/dev/null); then
        print_error "Not inside a git repository"
        return 1
    fi
    repo_root=$(cd "$(dirname "$git_common")" 2>/dev/null && pwd) || repo_root="$(pwd)"

    local worktree_root_dir worktree_path
    worktree_root_dir="$(loom_worktree_root "$repo_root")"
    worktree_path="$worktree_root_dir/issue-$issue_number"

    if [[ ! -d "$worktree_path" ]]; then
        print_error "No worktree found at $worktree_path — nothing to snapshot"
        _snap_json false "" false 0
        return 1
    fi
    if ! git -C "$worktree_path" rev-parse --git-dir >/dev/null 2>&1; then
        print_error "$worktree_path is not a git working tree"
        _snap_json false "" false 0
        return 1
    fi

    local snapshot_dir="$worktree_root_dir/.snapshots"
    if ! mkdir -p "$snapshot_dir" 2>/dev/null; then
        print_error "Could not create snapshot directory: $snapshot_dir"
        _snap_json false "" false 0
        return 1
    fi

    local ts
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    local patch_path="$snapshot_dir/issue-$issue_number-$ts.patch"

    # Optionally fold untracked files into the same patch via a temporary
    # intent-to-add (`git add -N`), which makes `git diff HEAD` render them as
    # new-file hunks WITHOUT staging their content. Reverted immediately after
    # the diff is captured so the worktree ends in its exact prior state
    # (still untracked, nothing left staged) — this never touches the index
    # any longer than the single `git diff` call below. Loom runtime markers
    # (.loom-managed et al) are excluded, same filter as _worktree_dirty_lines,
    # so a snapshot never captures noise every managed worktree carries.
    local -a added_for_diff=()
    if [[ "$include_untracked" == true ]]; then
        local untracked
        untracked="$(git -C "$worktree_path" ls-files --others --exclude-standard 2>/dev/null | \
            grep -vE '(^|/)\.loom-managed$|(^|/)\.loom-in-use$|(^|/)\.loom-checkpoint$|(^|/)\.no-changes-needed$' || true)"
        if [[ -n "$untracked" ]]; then
            while IFS= read -r f; do
                [[ -n "$f" ]] || continue
                if git -C "$worktree_path" add -N -- "$f" >/dev/null 2>&1; then
                    added_for_diff+=("$f")
                fi
            done <<< "$untracked"
        fi
    fi

    # A plain `git diff` (no --exit-code) always exits 0 unless a real error
    # occurred (bad HEAD, corrupt worktree, etc.) — it does not use exit code
    # to signal "has changes", so any nonzero here is a genuine failure.
    local diff_status=0
    git -C "$worktree_path" diff HEAD > "$patch_path" 2>/dev/null || diff_status=$?

    if [[ ${#added_for_diff[@]} -gt 0 ]]; then
        git -C "$worktree_path" reset -- "${added_for_diff[@]}" >/dev/null 2>&1 || true
    fi

    if [[ $diff_status -ne 0 ]]; then
        rm -f "$patch_path" 2>/dev/null || true
        print_error "git diff failed for $worktree_path (exit $diff_status)"
        _snap_json false "" false 0
        return 1
    fi

    local bytes has_changes=false
    bytes=$(wc -c < "$patch_path" 2>/dev/null | tr -d ' ')
    bytes="${bytes:-0}"
    [[ "$bytes" -gt 0 ]] && has_changes=true

    if [[ "$has_changes" == true ]]; then
        _snap_success "Snapshot written: $patch_path ($bytes bytes)"
    else
        _snap_info "No uncommitted changes — wrote an empty snapshot: $patch_path"
    fi
    _snap_info "Replay into a fresh worktree with: git apply $patch_path"

    _snap_json true "$patch_path" "$has_changes" "$bytes"
    return 0
}

# --------------------------------------------------------------------------
# Worktree-scoped clean-baseline stash (issue #5217)
# --------------------------------------------------------------------------
#
# `worktree.sh stash-push <N>` / `worktree.sh stash-pop <N>` give headless
# Builder/Doctor sweeps a genuinely safe replacement for the
# `git stash && <baseline check> && git stash pop` pattern used to diff a
# clean baseline against in-progress WIP (clippy/shellcheck/test-output
# comparisons). That raw pattern is correctly gated by
# guard-destructive-generic.sh's `stash-scope:worktree-collision` check
# (#4821) whenever >=2 `.loom-managed` worktrees are active — which in this
# repo is nearly always true — producing an unanswerable `ask` in headless
# mode with no human to answer it (#5217).
#
# `snapshot` (above) already solves the ADJACENT "shelve my WIP as a patch"
# case, but deliberately does not reset the working tree, so it cannot alone
# produce a clean baseline to diff against. stash-push/stash-pop close that
# gap WITHOUT touching `refs/stash` at all:
#
#   - stash-push captures the tracked diff via `git stash create` (which
#     builds a stash-format commit object but — unlike `git stash push` —
#     never writes to refs/stash), anchors it under a PER-ISSUE ref
#     (refs/loom/stash-baseline/issue-<N>) so it survives gc, then resets the
#     worktree's tracked files to HEAD (`git reset --hard HEAD`, scoped to
#     this one worktree's own index/working tree). Untracked files
#     (--include-untracked) are moved into a per-issue holding directory
#     rather than folded into the stash entry.
#   - stash-pop reads back the SAME per-issue ref / holding-directory pair
#     and restores both, then clears them.
#
# Because every issue gets its OWN ref rather than a shared stack, there is
# no window for another worktree's concurrent `git stash push` to land "in
# between" your push and pop — the race that makes a same-chain push/pop
# ALLOW heuristic in the GUARD itself unsafe (considered and rejected during
# #5217's curation: push and pop are two separate guard-approved Bash calls
# with an arbitrary-duration command running between them, so anything that
# lands on the SHARED stack during that window can still be popped by
# mistake by a same-chain heuristic that only checks command shape, not
# actual stack state). Anchoring to a per-issue ref instead of the shared
# stack removes the shared-mutable-state precondition for that race
# entirely, rather than trying to detect it after the fact.
#
# Durability note: both halves of the captured state live OUTSIDE the
# worktree — the ref in the repo's common git dir, the untracked holding
# directory and the pending marker under `<worktree-root>/.stash-baseline/`.
# So even if the worktree is removed while a push is pending, nothing is
# unrecoverable: `git stash apply refs/loom/stash-baseline/issue-<N>` still
# replays the captured diff.
#
# Raw `git stash pop/drop/clear` remains exactly as gated as before by
# guard-destructive-generic.sh — stash-push/stash-pop are the sanctioned,
# guard-transparent replacement path for THIS pattern, not a guard exemption:
# neither literally invokes `git stash pop|drop|clear`, so the guard's
# pattern match never sees them, and it keeps asking on every raw stash
# pop/drop/clear exactly as it did before this issue.
stash_push_worktree_command() {
    local issue_number="" json=false include_untracked=false
    local usage="Usage: pnpm worktree stash-push <issue-number> [--include-untracked] [--json]"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --include-untracked) include_untracked=true; shift ;;
            --json)               json=true; shift ;;
            --*)
                print_error "Unknown flag for stash-push: $1"
                echo ""
                echo "$usage"
                return 1
                ;;
            *)
                if [[ -z "$issue_number" ]]; then
                    issue_number="$1"; shift
                else
                    print_error "Unexpected argument: $1"
                    return 1
                fi
                ;;
        esac
    done

    if [[ -z "$issue_number" ]]; then
        print_error "stash-push requires an issue number"
        echo ""
        echo "$usage"
        return 1
    fi
    if ! [[ "$issue_number" =~ ^[0-9]+$ ]]; then
        print_error "Issue number must be numeric (got: '$issue_number')"
        echo ""
        echo "$usage"
        return 1
    fi

    _sbp_info()    { if [[ "$json" == true ]]; then echo -e "${BLUE}ℹ $*${NC}" >&2; else print_info "$*"; fi; }
    _sbp_success() { if [[ "$json" == true ]]; then echo -e "${GREEN}✓ $*${NC}" >&2; else print_success "$*"; fi; }
    _sbp_json() {
        # $1=success(bool) $2=hasTrackedChanges(bool) $3=untrackedCount $4=ref
        [[ "$json" == true ]] || return 0
        printf '{"success": %s, "issueNumber": %s, "hasTrackedChanges": %s, "untrackedCount": %s, "ref": "%s"}\n' \
            "$1" "$issue_number" "$2" "$3" "$4"
    }

    local git_common repo_root
    if ! git_common=$(git rev-parse --git-common-dir 2>/dev/null); then
        print_error "Not inside a git repository"
        return 1
    fi
    repo_root=$(cd "$(dirname "$git_common")" 2>/dev/null && pwd) || repo_root="$(pwd)"

    local worktree_root_dir worktree_path
    worktree_root_dir="$(loom_worktree_root "$repo_root")"
    worktree_path="$worktree_root_dir/issue-$issue_number"

    if [[ ! -d "$worktree_path" ]]; then
        print_error "No worktree found at $worktree_path — nothing to stash-push"
        _sbp_json false false 0 ""
        return 1
    fi
    if ! git -C "$worktree_path" rev-parse --git-dir >/dev/null 2>&1; then
        print_error "$worktree_path is not a git working tree"
        _sbp_json false false 0 ""
        return 1
    fi

    local ref="refs/loom/stash-baseline/issue-$issue_number"
    local holding_dir="$worktree_root_dir/.stash-baseline/issue-$issue_number"
    local manifest_path="$holding_dir/untracked.manifest"
    # The pending marker is what makes the intended headless chain
    # `stash-push N && <baseline check> && stash-pop N` safe when the worktree
    # happened to be CLEAN: nothing is captured, but the marker still records
    # that a push occurred, so the paired stash-pop can succeed as a no-op
    # instead of exiting 1 and breaking the `&&` chain mid-sweep. Without it,
    # "there was nothing to restore" and "you never pushed" are indistinguishable.
    local pending_marker="$holding_dir/pending"

    if git -C "$worktree_path" rev-parse --verify --quiet "$ref" >/dev/null 2>&1 || [[ -f "$manifest_path" ]] || [[ -f "$pending_marker" ]]; then
        print_error "A pending stash-push already exists for issue $issue_number — run 'stash-pop $issue_number' first (or resolve manually: ref $ref / $holding_dir)"
        _sbp_json false false 0 ""
        return 1
    fi

    local stash_commit=""
    stash_commit="$(git -C "$worktree_path" stash create 2>/dev/null || true)"

    local has_tracked=false
    if [[ -n "$stash_commit" ]]; then
        has_tracked=true
        if ! git -C "$worktree_path" update-ref "$ref" "$stash_commit" 2>/dev/null; then
            print_error "Failed to anchor baseline commit under $ref"
            _sbp_json false false 0 ""
            return 1
        fi
        if ! git -C "$worktree_path" reset --hard HEAD >/dev/null 2>&1; then
            print_error "Failed to reset $worktree_path to a clean baseline after capturing WIP — baseline preserved at $ref, nothing lost"
            _sbp_json false true 0 "$ref"
            return 1
        fi
    fi

    local untracked_count=0
    if [[ "$include_untracked" == true ]]; then
        local untracked
        untracked="$(git -C "$worktree_path" ls-files --others --exclude-standard 2>/dev/null | \
            grep -vE '(^|/)\.loom-managed$|(^|/)\.loom-in-use$|(^|/)\.loom-checkpoint$|(^|/)\.no-changes-needed$' || true)"
        if [[ -n "$untracked" ]]; then
            if ! mkdir -p "$holding_dir/untracked" 2>/dev/null; then
                print_error "Could not create holding directory: $holding_dir/untracked"
                _sbp_json false "$has_tracked" 0 "$ref"
                return 1
            fi
            : > "$manifest_path"
            while IFS= read -r f; do
                [[ -n "$f" ]] || continue
                local dest="$holding_dir/untracked/$f"
                mkdir -p "$(dirname "$dest")" 2>/dev/null || continue
                if mv "$worktree_path/$f" "$dest" 2>/dev/null; then
                    echo "$f" >> "$manifest_path"
                    untracked_count=$((untracked_count + 1))
                fi
            done <<< "$untracked"
            [[ "$untracked_count" -eq 0 ]] && { rm -f "$manifest_path" 2>/dev/null || true; }
        fi
    fi

    # Record the push itself, whether or not anything was captured, so the
    # paired stash-pop is always a legitimate no-op rather than an error.
    if ! mkdir -p "$holding_dir" 2>/dev/null || ! date -u +"%Y-%m-%dT%H:%M:%SZ" > "$pending_marker" 2>/dev/null; then
        print_error "Could not record the pending-push marker at $pending_marker"
        _sbp_json false "$has_tracked" "$untracked_count" "$ref"
        return 1
    fi

    if [[ "$has_tracked" == false && "$untracked_count" -eq 0 ]]; then
        _sbp_info "No uncommitted changes to push for issue $issue_number — worktree was already clean"
    else
        _sbp_success "Baseline captured for issue $issue_number (tracked: $has_tracked, untracked files moved: $untracked_count)"
    fi
    _sbp_info "Restore with: ./.loom/scripts/worktree.sh stash-pop $issue_number"

    _sbp_json true "$has_tracked" "$untracked_count" "$ref"
    return 0
}

# See stash_push_worktree_command's comment block above for the full design
# rationale. stash-pop is the restore half: reads back the per-issue ref
# (tracked changes) and holding directory (untracked files) written by
# stash-push for the SAME issue number, applies both, and clears them.
stash_pop_worktree_command() {
    local issue_number="" json=false
    local usage="Usage: pnpm worktree stash-pop <issue-number> [--json]"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json) json=true; shift ;;
            --*)
                print_error "Unknown flag for stash-pop: $1"
                echo ""
                echo "$usage"
                return 1
                ;;
            *)
                if [[ -z "$issue_number" ]]; then
                    issue_number="$1"; shift
                else
                    print_error "Unexpected argument: $1"
                    return 1
                fi
                ;;
        esac
    done

    if [[ -z "$issue_number" ]]; then
        print_error "stash-pop requires an issue number"
        echo ""
        echo "$usage"
        return 1
    fi
    if ! [[ "$issue_number" =~ ^[0-9]+$ ]]; then
        print_error "Issue number must be numeric (got: '$issue_number')"
        echo ""
        echo "$usage"
        return 1
    fi

    _sbo_info()    { if [[ "$json" == true ]]; then echo -e "${BLUE}ℹ $*${NC}" >&2; else print_info "$*"; fi; }
    _sbo_success() { if [[ "$json" == true ]]; then echo -e "${GREEN}✓ $*${NC}" >&2; else print_success "$*"; fi; }
    _sbo_json() {
        # $1=success(bool) $2=restoredTracked(bool) $3=restoredUntrackedCount
        [[ "$json" == true ]] || return 0
        printf '{"success": %s, "issueNumber": %s, "restoredTracked": %s, "restoredUntrackedCount": %s}\n' \
            "$1" "$issue_number" "$2" "$3"
    }

    local git_common repo_root
    if ! git_common=$(git rev-parse --git-common-dir 2>/dev/null); then
        print_error "Not inside a git repository"
        return 1
    fi
    repo_root=$(cd "$(dirname "$git_common")" 2>/dev/null && pwd) || repo_root="$(pwd)"

    local worktree_root_dir worktree_path
    worktree_root_dir="$(loom_worktree_root "$repo_root")"
    worktree_path="$worktree_root_dir/issue-$issue_number"

    if [[ ! -d "$worktree_path" ]]; then
        print_error "No worktree found at $worktree_path"
        _sbo_json false false 0
        return 1
    fi
    if ! git -C "$worktree_path" rev-parse --git-dir >/dev/null 2>&1; then
        print_error "$worktree_path is not a git working tree"
        _sbo_json false false 0
        return 1
    fi

    local ref="refs/loom/stash-baseline/issue-$issue_number"
    local holding_dir="$worktree_root_dir/.stash-baseline/issue-$issue_number"
    local manifest_path="$holding_dir/untracked.manifest"
    local pending_marker="$holding_dir/pending"

    local has_tracked=false stash_commit=""
    if git -C "$worktree_path" rev-parse --verify --quiet "$ref" >/dev/null 2>&1; then
        has_tracked=true
        stash_commit="$(git -C "$worktree_path" rev-parse "$ref" 2>/dev/null || true)"
    fi
    local has_manifest=false
    [[ -f "$manifest_path" ]] && has_manifest=true
    local has_pending=false
    [[ -f "$pending_marker" ]] && has_pending=true

    # Nothing captured AND no record of a push => the caller never pushed.
    # That is a real error. Nothing captured but a pending marker present
    # means stash-push ran against an already-clean worktree — a legitimate
    # no-op restore, so the `push && check && pop` chain must not break.
    if [[ "$has_tracked" == false && "$has_manifest" == false && "$has_pending" == false ]]; then
        print_error "Nothing to restore for issue $issue_number — run 'stash-push $issue_number' first"
        _sbo_json false false 0
        return 1
    fi

    if [[ "$has_tracked" == true ]]; then
        if ! git -C "$worktree_path" stash apply "$stash_commit" >/dev/null 2>&1; then
            print_error "Failed to apply baseline commit $stash_commit for issue $issue_number (likely conflicts with the current tree). The captured baseline is PRESERVED at $ref — resolve manually with 'git -C $worktree_path stash apply $stash_commit', then delete the ref with 'git -C $worktree_path update-ref -d $ref'."
            _sbo_json false false 0
            return 1
        fi
        git -C "$worktree_path" update-ref -d "$ref" >/dev/null 2>&1 || true
    fi

    local restored_untracked=0
    if [[ "$has_manifest" == true ]]; then
        local restore_failed=false
        while IFS= read -r f; do
            [[ -n "$f" ]] || continue
            local src="$holding_dir/untracked/$f"
            [[ -f "$src" ]] || continue
            if ! mkdir -p "$(dirname "$worktree_path/$f")" 2>/dev/null; then
                restore_failed=true
                continue
            fi
            if mv "$src" "$worktree_path/$f" 2>/dev/null; then
                restored_untracked=$((restored_untracked + 1))
            else
                restore_failed=true
            fi
        done < "$manifest_path"

        if [[ "$restore_failed" == true ]]; then
            print_error "Some untracked files for issue $issue_number could not be restored — remaining files are still under $holding_dir/untracked (manifest kept at $manifest_path for manual recovery)"
            _sbo_json false "$has_tracked" "$restored_untracked"
            return 1
        fi

        rm -f "$manifest_path" 2>/dev/null || true
        rmdir "$holding_dir/untracked" 2>/dev/null || true
    fi

    # Clear the pending marker last: everything above either restored cleanly
    # or returned early with the captured state preserved, so reaching here
    # means the push/pop pair is complete.
    rm -f "$pending_marker" 2>/dev/null || true
    rmdir "$holding_dir" 2>/dev/null || true

    if [[ "$has_tracked" == false && "$restored_untracked" -eq 0 ]]; then
        _sbo_info "Nothing was captured for issue $issue_number — the worktree was already clean at stash-push time"
        _sbo_json true false 0
        return 0
    fi

    _sbo_success "Baseline restored for issue $issue_number (tracked: $has_tracked, untracked files restored: $restored_untracked)"
    _sbo_json true "$has_tracked" "$restored_untracked"
    return 0
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
  pnpm worktree <issue-number> --base <branch>          Branch off <branch> (stacked PR, #3729)
  pnpm worktree <issue-number> --sparse <paths...>      Cone-mode sparse checkout
  pnpm worktree <issue-number> --full                   Convert sparse worktree to full
  pnpm worktree remove <N> [--keep-branch] [--force]    Remove one managed worktree
  pnpm worktree snapshot <N> [--include-untracked] [--json]
                                                         Save uncommitted WIP as a patch file
  pnpm worktree stash-push <N> [--include-untracked] [--json]
                                                         Capture WIP, reset to a clean baseline
  pnpm worktree stash-pop <N> [--json]                  Restore WIP captured by stash-push
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

  pnpm worktree 42 --base feature/issue-41
    Creates: .loom/worktrees/issue-42
    Branch: feature/issue-42, branched off feature/issue-41 instead of the
    default branch (stacked-PR mode, #3729). Used by /loom:sweep --depends-on.

  pnpm worktree 42 --sparse src/lib defaults/scripts
    Creates a sparse worktree containing only the listed paths plus the
    always-included safety set (.claude/, .loom/, .githooks/, scripts/, and
    all tracked top-level files).

  pnpm worktree 42 --full
    Converts an existing sparse worktree back to a full checkout
    (no-op on an already-full worktree).

  pnpm worktree remove 42
    Removes the managed worktree .loom/worktrees/issue-42 and deletes its local
    branch (safe delete — refuses on unmerged commits). This is the sanctioned
    single-worktree removal path so you never need 'git worktree remove'
    directly. It honors the .loom-managed sentinel (refuses to remove a
    user-provisioned worktree), REFUSES when the worktree has uncommitted
    changes (#4449 — see --force below), is idempotent (clear no-op if absent),
    and prunes the git worktree registration. Use 'loom-clean' for bulk/stale
    cleanup across all closed issues.

  pnpm worktree remove 42 --keep-branch
    Same as above but leaves the local feature branch intact.

  pnpm worktree remove 42 --force
    Removes the worktree even when it has uncommitted changes, DISCARDING them.
    Without --force, a dirty worktree makes 'remove' exit non-zero, list what it
    found, and print how to preserve the work (commit / save a patch / stash).
    Loom runtime markers (.loom-managed, .loom-in-use, .loom-checkpoint,
    .no-changes-needed) never count as uncommitted work.

  pnpm worktree snapshot 42
    Writes the worktree's uncommitted diff (tracked-file changes: staged +
    unstaged, via 'git diff HEAD') to a patch file at:
      <worktree-root>/.snapshots/issue-42-<UTC-timestamp>.patch
    Does NOT touch 'git stash' — unlike stash, which is repo-global across
    every worktree in the repo, this patch file is scoped to this one
    invocation and this one path, so concurrent snapshots from other
    'issue-<N>' worktrees can never collide or clobber each other. Replay
    into a fresh worktree for the same issue with:
      git -C .loom/worktrees/issue-42 apply <patch-path>
    A worktree with no uncommitted changes still succeeds, writing an empty
    patch file rather than erroring.

  pnpm worktree snapshot 42 --include-untracked
    Same as above, but also folds untracked files into the patch (via a
    temporary 'git add -N' intent-to-add that is reverted immediately after
    the diff is captured — the worktree's index ends unchanged). Loom runtime
    markers are excluded even with this flag.

  pnpm worktree snapshot 42 --json
    Output: {"success": true, "issueNumber": 42, "patchPath": "/path/to/.snapshots/issue-42-...patch", "hasChanges": true, "bytes": 1234}

  pnpm worktree stash-push 42
    For a "clean baseline vs my diff" comparison (clippy/shellcheck/test
    baseline diffing, issue #5217): captures the worktree's uncommitted
    tracked-file diff via 'git stash create' (never touches refs/stash),
    anchors it under the PER-ISSUE ref refs/loom/stash-baseline/issue-42, and
    resets the worktree to a clean 'git reset --hard HEAD' baseline. Unlike
    raw 'git stash push', two builders in different worktrees can never
    collide — each issue gets its own ref, not a shared stack — so this does
    NOT trigger guard-destructive-generic.sh's stash-scope:worktree-collision
    ask even with several other '.loom-managed' worktrees active.

  pnpm worktree stash-push 42 --include-untracked
    Same as above, but also moves untracked files (respecting .gitignore,
    excluding Loom runtime markers) into a per-issue holding directory
    instead of leaving them in the worktree.

  pnpm worktree stash-pop 42
    Restores whatever 'stash-push 42' captured (tracked diff + any moved
    untracked files) and clears the ref / holding directory. Succeeds as a
    no-op when the matching stash-push found an already-clean worktree, so
    'stash-push 42 && <baseline check> && stash-pop 42' never breaks its own
    chain. Errors loudly, WITHOUT discarding the captured baseline, if no
    stash-push is pending at all or if re-applying conflicts with the tree.

  pnpm worktree stash-push 42 --json / stash-pop 42 --json
    Output: {"success": true, "issueNumber": 42, "hasTrackedChanges": true, "untrackedCount": 0, "ref": "refs/loom/stash-baseline/issue-42"}
            {"success": true, "issueNumber": 42, "restoredTracked": true, "restoredUntrackedCount": 0}

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

# Operator-facing single-worktree removal verb (issue #3769). Dispatched HERE,
# before the generic numeric-issue-number validation below, so `remove <N>` /
# `--remove <N>` is not rejected as "Issue number must be numeric". The handler
# parses its own args (issue number + optional --keep-branch / --json).
if [[ "$1" == "remove" || "$1" == "--remove" ]]; then
    shift
    # Left of && so set -e does not abort on a non-zero return from the handler.
    remove_worktree_command "$@" && exit 0
    exit 1
fi

# Worktree-scoped WIP snapshot verb (issue #4778). Dispatched HERE, before the
# generic numeric-issue-number validation below, for the same reason `remove`
# is: `snapshot <N>` must not be rejected as "Issue number must be numeric".
if [[ "$1" == "snapshot" ]]; then
    shift
    # Left of && so set -e does not abort on a non-zero return from the handler.
    snapshot_worktree_command "$@" && exit 0
    exit 1
fi

# Worktree-scoped clean-baseline stash verbs (issue #5217). Dispatched HERE
# for the same reason `snapshot`/`remove` are: `stash-push <N>` / `stash-pop
# <N>` must not be rejected as "Issue number must be numeric".
if [[ "$1" == "stash-push" ]]; then
    shift
    stash_push_worktree_command "$@" && exit 0
    exit 1
fi

if [[ "$1" == "stash-pop" ]]; then
    shift
    stash_pop_worktree_command "$@" && exit 0
    exit 1
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
# Base-branch override (#3729, stacked-PR v1). When set via `--base <branch>`,
# the new feature branch is created from (and stale worktrees reset to) that
# branch instead of origin/$DEFAULT_BRANCH. `/loom:sweep --depends-on <parent>`
# passes `--base feature/issue-<parent>` so the child stacks on the parent.
BASE_BRANCH=""

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
        --base)
            BASE_BRANCH="$2"
            if [[ -z "$BASE_BRANCH" ]]; then
                print_error "--base requires a branch name"
                exit 1
            fi
            shift 2
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

# ─── Git identity hygiene check (#4369) ─────────────────────────────────────
# Worktrees share the parent repo's local git config, so a corrupted local
# user.email/user.name (stacked values, or a value with a glued-on shell
# command like "...github.comecho" — Tauri-era residue, see
# check-git-identity.sh's header) poisons every worktree created from this
# repo, including this one. Hard-fail on the corruption pattern (it would
# otherwise ship a garbled commit author silently — see PR #4303); warn (but
# proceed) on a plain multi-value that doesn't match the corruption pattern,
# since a pre-existing-but-unambiguous local config shouldn't strand a sweep.
GIT_IDENTITY_CHECK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check-git-identity.sh"
if [[ -x "$GIT_IDENTITY_CHECK" ]]; then
    # Note: in --json mode fd 1 is already redirected to stderr (see the
    # stdout-purity block above), so this plain `echo`/print output lands on
    # stderr in both modes — only the explicit `>&3` JSON document below
    # reaches the caller's stdout.
    # `if VAR=$(cmd); then` (rather than a bare assignment) so a non-zero exit
    # from the check does not trip `set -e` before we can inspect $? below.
    if GIT_IDENTITY_OUTPUT=$("$GIT_IDENTITY_CHECK" 2>&1); then
        GIT_IDENTITY_RC=0
    else
        GIT_IDENTITY_RC=$?
    fi
    if [[ "$GIT_IDENTITY_RC" -eq 3 ]]; then
        print_error "Corrupted local git identity detected — refusing to create a worktree."
        echo "$GIT_IDENTITY_OUTPUT"
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"success": false, "error": "corrupted-git-identity", "issueNumber": '"$ISSUE_NUMBER"'}' >&3
        fi
        exit 1
    elif [[ "$GIT_IDENTITY_RC" -eq 1 ]]; then
        print_warning "Stacked local git identity values detected (non-fatal — see details below)."
        echo "$GIT_IDENTITY_OUTPUT"
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

# ─── Base-branch resolution (#3729, stacked-PR v1) ──────────────────────────
# By default a new feature branch is created from origin/$DEFAULT_BRANCH. When
# --base <branch> is passed (e.g. `--base feature/issue-<parent>` from
# /loom:sweep --depends-on), resolve a ref for that base and use it instead so
# the child branch stacks on top of the parent's branch. Prefer the pushed
# origin/<base>, fall back to a local <base>. Hard-fail if neither resolves —
# an explicit base that can't be found is worse than silently branching off
# main (which would un-stack the child).
BASE_REF="origin/$DEFAULT_BRANCH"
BASE_DISPLAY="$DEFAULT_BRANCH"
if [[ -n "$BASE_BRANCH" ]]; then
    git fetch origin "$BASE_BRANCH" 2>/dev/null || true
    if git show-ref --verify --quiet "refs/remotes/origin/$BASE_BRANCH"; then
        BASE_REF="origin/$BASE_BRANCH"
        BASE_DISPLAY="origin/$BASE_BRANCH"
    elif git show-ref --verify --quiet "refs/heads/$BASE_BRANCH"; then
        BASE_REF="$BASE_BRANCH"
        BASE_DISPLAY="$BASE_BRANCH"
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"success": false, "error": "base-branch-not-found", "baseBranch": "'"$BASE_BRANCH"'"}' >&3
        else
            print_error "Requested --base '$BASE_BRANCH' not found as origin/$BASE_BRANCH or a local branch."
            echo "  Ensure the parent sweep has created/pushed feature/issue-<parent> before stacking a child on it."
        fi
        exit 1
    fi
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Stacked worktree base: $BASE_DISPLAY (from --base $BASE_BRANCH)"
    fi
fi

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
        # Check if worktree is stale: no commits ahead of the base and behind it.
        # For a stacked child (--base), staleness is measured against the parent
        # branch (BASE_REF), not the default branch (#3729).
        local_commits_ahead=$(git -C "$WORKTREE_PATH" rev-list --count "$BASE_REF..HEAD" 2>/dev/null) || local_commits_ahead="0"
        local_commits_behind=$(git -C "$WORKTREE_PATH" rev-list --count "HEAD..$BASE_REF" 2>/dev/null) || local_commits_behind="0"
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
                print_warning "Stale worktree detected (0 commits ahead, $local_commits_behind behind $BASE_DISPLAY, no uncommitted changes)"
                print_info "Resetting worktree in place to $BASE_DISPLAY..."
            fi

            # Back-fill/refresh the Loom sentinel on both reset outcomes: the
            # worktree remains usable either way, so keep it cleanup-eligible
            # (#3548).
            write_loom_sentinel "$WORKTREE_PATH"
            if git -C "$WORKTREE_PATH" fetch origin "${BASE_BRANCH:-$DEFAULT_BRANCH}" 2>/dev/null && \
               git -C "$WORKTREE_PATH" reset --hard "$BASE_REF" 2>/dev/null; then
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_success "Stale worktree reset to $BASE_DISPLAY"
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
    # No local branch by this name. Before falling back to a fresh branch off
    # BASE_REF, check whether origin already has a pushed branch of the exact
    # same name — e.g. an existing PR branch from a prior Builder/Doctor cycle
    # (#4823). Without this check, a Doctor fixing review feedback on an
    # already-pushed PR would silently get a NEW branch created from
    # origin/$DEFAULT_BRANCH instead of the real PR history, risking a
    # PR-clobbering force-push or a diff against the wrong base. This is
    # independent of --base (which only chooses the start point when we DO
    # need to create a fresh branch, below).
    git fetch origin "$BRANCH_NAME" 2>/dev/null || true
    if git show-ref --verify --quiet "refs/remotes/origin/$BRANCH_NAME"; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Remote branch 'origin/$BRANCH_NAME' already exists - creating a local branch tracking it (not branching from $BASE_DISPLAY)"
        fi
        # Informational only: the remote branch always wins here (it IS the
        # PR history to continue), but note when it doesn't contain all of
        # BASE_DISPLAY's history (e.g. pushed before recent main commits
        # landed) so a caller reading the log understands why the worktree
        # isn't rebased on top of the latest base.
        if ! git merge-base --is-ancestor "$BASE_REF" "refs/remotes/origin/$BRANCH_NAME" 2>/dev/null; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then
                print_warning "origin/$BRANCH_NAME has diverged from $BASE_DISPLAY (does not contain all of its history) - tracking origin/$BRANCH_NAME as-is"
            fi
        fi
        CREATE_ARGS=("$WORKTREE_PATH" "-b" "$BRANCH_NAME" "origin/$BRANCH_NAME")
    else
        # Create new branch from the base ref (origin/$DEFAULT_BRANCH by default, or
        # the --base override for a stacked child — #3729).
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Creating new branch from $BASE_DISPLAY"
        fi
        CREATE_ARGS=("$WORKTREE_PATH" "-b" "$BRANCH_NAME" "$BASE_REF")
    fi
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

    # Symlink additional gitignored paths configured for worktree.linkPaths
    # (e.g. generated wasm-pack bindings that are expensive to rebuild per
    # worktree). Best-effort: missing config, missing jq, malformed JSON, or
    # an empty/absent key all silently skip this step (#3528).
    #
    # Resolved through the config-resolver tier chain (#4062, lib/worktree-root.sh
    # already sources lib/config-resolver.sh) ONCE, then queried locally via jq
    # — worktree.linkPaths is an array, so loom_config_get's pretty-printed
    # multi-line-JSON return for non-scalars must not be used here (see
    # config-resolver.sh's docstring); resolve the merged JSON and pipe it
    # through the existing jq expression instead.
    if command -v jq >/dev/null 2>&1; then
        LOOM_WORKTREE_LINKPATHS_CFG="$(loom_resolve_config "$MAIN_WORKSPACE_DIR")"
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
        done < <(echo "$LOOM_WORKTREE_LINKPATHS_CFG" | jq -r '.worktree.linkPaths[]? // empty' 2>/dev/null)
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
