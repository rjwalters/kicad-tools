#!/bin/bash

# Loom Worktree Helper Script
# Safely creates and manages git worktrees for agent development
#
# Usage:
#   pnpm worktree <issue-number>                       # Create worktree for issue
#   pnpm worktree <issue-number> <branch>              # Create worktree with custom branch name
#   pnpm worktree <issue-number> --sparse <paths...>   # Cone-mode sparse checkout
#   pnpm worktree <issue-number> --full                # Convert sparse worktree to full
#   pnpm worktree remove <issue-number> [--keep-branch] [--force] [--dry-run]
#     # Remove one managed worktree (--dry-run reports the plan, including any
#     # reclaimable redirected cargo target dir + its size, and changes nothing)
#   pnpm worktree snapshot <issue-number> [--include-untracked] [--json]
#     # Write a patch file capturing the worktree's uncommitted diff to
#     # <worktree-root>/.snapshots/issue-<N>-<UTC-timestamp>.patch — WITHOUT
#     # touching `git stash` (which is repo-global and can be clobbered by a
#     # concurrent builder in another worktree). Replay with `git apply`.
#   pnpm worktree stash-push <issue-number|main> [--include-untracked] [--json]
#   pnpm worktree stash-pop <issue-number|main> [--json]
#     # Clean-and-restore pair for a "clean baseline vs my diff" comparison
#     # (clippy/shellcheck/test baseline diffing) — WITHOUT touching the
#     # shared `refs/stash` stack. Anchors captured WIP to a PER-TARGET ref
#     # (refs/loom/stash-baseline/issue-<N>, or .../main for the primary
#     # clone) instead, so no other worktree's concurrent stash op can ever
#     # land "in between" push and pop (#5217, extended to `main` by #6076).
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

# The worktree-removal ledger (#5950) and the cargo target-dir reclaim (#7239)
# used to be sourced here. Both were only ever consumed by the `remove` verb,
# which is now `loom-daemon worktree-remove` (#8195 slice 3) — and the daemon
# already owned the Rust half of each (`worktree_ops/removal_log.rs`,
# `worktree_ops/cargo_target.rs`), so the port calls those directly rather than
# keeping a second bash implementation alive. The ledger's line format is
# unchanged, so one grep/jq still reads every removal path's entries together.

# Shared "has this branch landed?" primitive (#7812): forge PR state first,
# then `git merge-tree --write-tree` tree equality, answering landed /
# not-landed / unknown. Replaces this script's two private squash heuristics
# (the deleted `_worktree_merged_pr_head_sha` and merge-pr.sh's
# `_worktree_branch_fully_captured`). Sourced defensively with a fail-closed
# `unknown` fallback for the same reason as the ledger/target-dir libs above:
# a partially-resynced .loom/ must degrade to "cannot tell, keep the branch",
# never to a `source` failure that breaks worktree creation and removal.
if [[ -f "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/branch-landed.sh" ]]; then
    # shellcheck source=lib/branch-landed.sh
    source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/branch-landed.sh"
else
    # shellcheck disable=SC2034  # side-channel globals read by callers
    branch_landed() {
        BRANCH_LANDED_VERDICT="unknown"; BRANCH_LANDED_EVIDENCE="inconclusive"
        BRANCH_LANDED_PR_NUMBER=""; BRANCH_LANDED_PR_HEAD_SHA=""
        BRANCH_LANDED_FORGE_STATUS="unavailable"
        printf 'unknown\n'
    }
fi

# Race-safe reset helper (#6334). The "stale worktree" reset path below can
# otherwise discard foreign work that appears in the window between the
# staleness check and the reset itself — see the lib file for the full
# rationale and design decision.
# shellcheck source=lib/worktree-race-rescue.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/worktree-race-rescue.sh"

# Forge-aware guard against creating a fresh branch that shadows an
# already-open PR (#7765) - see the lib file for the full rationale.
# Sourced unconditionally, deliberately WITHOUT the no-op fallback the
# diagnostic libs above use: silently skipping this check is exactly the
# defect it closes, so a missing sibling must fail loudly rather than
# quietly restore the old blind fall-through.
# shellcheck source=lib/worktree-forge-pr-check.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/worktree-forge-pr-check.sh"

# loom-daemon binary discovery, for the claim-lease step near the bottom of
# this file (#8193). Sourced with the diagnostic libs' defensive shape, not the
# forge-check's loud one: a partially-resynced .loom/ must degrade to "no
# lease", never to a `source` failure that breaks worktree creation outright.
# When the source fails, `loom_resolve_self_daemon_bin` is simply undefined and
# the call site's own `|| true` swallows the resulting 127.
# shellcheck source=lib/locate-daemon-bin.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/locate-daemon-bin.sh" 2>/dev/null || true

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
# parallelize through the helper for the (short) duration of `git worktree
# add` itself — is acceptable because (a) `git worktree add` itself is short
# relative to the rest of an issue's lifecycle, and (b) parallel hangs that
# hold an `index.lock` for 10-20 minutes are the very problem this PR fixes.
#
# The lock path uses the same name (`worktree-<id>/`) the per-issue version
# used so its layout matches `.loom/locks/issue-<N>/`. The "id"
# here is the constant string "add"; per-issue accounting still lives in the
# `owner.json` body for debugging visibility.
#
# **Critical-section scope (issue #6014):** the lock is held across the
# `git worktree add` invocation itself (plus its short recovery retry) and
# the repo-level git preparation that immediately precedes it and must not
# race with a concurrent add — `git worktree prune`, the `git fetch` of
# `origin/$DEFAULT_BRANCH` / the base branch / `origin/feature/issue-N`, and
# base-branch resolution. It is explicitly NOT held across anything that
# follows the add: sentinel writing, sparse-checkout setup, submodule init,
# or the project-specific `post-worktree.sh` hook. The call site releases the
# lock the moment `git worktree add` returns, success or failure, rather than
# waiting for the script's EXIT trap. A repo whose post-worktree hook can run
# for minutes (e.g. a `cargo build --release`) must not serialize every
# *unrelated* worktree creation on the host behind it — the post-add phase
# does not touch `.git/config.lock` at all, so it needs no repo-global
# serialization.
#
# **Ownership verification (issue #6014):** each acquisition writes a random
# one-shot `token` into `owner.json` alongside `owner_pid`, and
# `acquire_worktree_lock` returns it via the `WORKTREE_LOCK_TOKEN` global.
# `release_worktree_lock` requires the caller to pass that same token back
# and refuses to remove the lock directory unless the token it finds on disk
# still matches — so a late release from a stale/wedged holder (e.g. its
# EXIT trap finally firing well after an operator judged it dead, manually
# cleared the lock, and a different process legitimately re-acquired it)
# is a safe no-op instead of deleting a live holder's lock out from under it.
#
# Tunables (env vars, documented in show_help):
#   LOOM_WORKTREE_LOCK_TIMEOUT       — seconds to wait (default 600 = 10min)
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
# on timeout failure so the caller can include it in error output. On success,
# sets WORKTREE_LOCK_TOKEN to the one-shot acquisition token the caller MUST
# pass back to release_worktree_lock (see "Ownership verification" above).
WORKTREE_LOCK_HOLDER_PID=""
WORKTREE_LOCK_TOKEN=""

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
            # Lock acquired; record owner metadata for debugging plus a
            # one-shot token so release can verify it still owns this lock
            # (issue #6014 — see "Ownership verification" above).
            local token
            token="$$-$(date -u +%s%N 2>/dev/null || date -u +%s)-$RANDOM"
            cat > "$lock/owner.json" <<EOF
{
  "issue": $issue,
  "owner_pid": $$,
  "token": "$token",
  "script": "worktree.sh",
  "acquired_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
            WORKTREE_LOCK_TOKEN="$token"
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

# release_worktree_lock <issue> <token>
#
# Removes the repo-global worktree-add lock ONLY if <token> matches the
# token currently recorded in owner.json — i.e. only if the caller is the
# process that most recently acquired it (issue #6014). A caller with a
# stale/empty token (already released, or never actually held the lock)
# leaves the directory untouched: there is nothing it can safely prove it
# owns, so removing anything would risk deleting a different, live holder's
# lock (the exact race described in issue #6014).
release_worktree_lock() {
    local issue="$1"
    local token="$2"
    [[ -z "$issue" ]] && return 0
    # No token means we never held the lock (or already released it) — never
    # remove a lock directory we cannot prove is ours.
    [[ -z "$token" ]] && return 0

    local lock
    lock="$(_worktree_lock_path "$issue")"
    [[ -d "$lock" ]] || return 0

    local current_token=""
    if [[ -f "$lock/owner.json" ]]; then
        current_token=$(awk -F'"' '/"token"[[:space:]]*:/ {print $4; exit}' "$lock/owner.json" 2>/dev/null)
    fi

    if [[ "$current_token" != "$token" ]]; then
        # The lock directory belongs to a different acquisition (ours was
        # already cleared and reassigned) — do NOT touch it.
        return 0
    fi

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
        # `git worktree list --porcelain` emits absolute, symlink-RESOLVED
        # paths on the `worktree ` line, so resolve with `pwd -P` (not logical
        # `pwd`) before comparing — otherwise a symlinked path (macOS
        # /var -> /private/var) never matches. The `worktree ` path itself
        # (prefix = 9 chars) may contain spaces, so parse it with
        # substr($0, 10) rather than $2, which truncates at the first space
        # (#7849 — same class as #3717; both mismatches make grep -Fxq miss
        # and get a LIVE, registered worktree rm -rf'd below). Mirrors
        # `branch_delete::worktree_entries` in loom-daemon, which parses the
        # same porcelain the same way for the `remove` verb (#8195 slice 3).
        local abs_wt
        abs_wt=$(cd "$wt_path" 2>/dev/null && pwd -P) || abs_wt=""
        local registered=0
        if [[ -n "$abs_wt" ]]; then
            if git worktree list --porcelain 2>/dev/null \
                | awk '/^worktree / {print substr($0, 10)}' \
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
# Operator-facing single-worktree removal: `remove <N>` / `--remove <N>`
# --------------------------------------------------------------------------
#
# Ported to `loom-daemon worktree-remove` (#8195 slice 3, epic #7810). The verb
# every irreversible operation in this script was reachable from — the eight
# guards, `git worktree remove --force`, the #5177 direct `rm -rf` fallback,
# the #7239 cargo-target-dir reclaim, the #5950 ledger write and the
# squash-aware `git branch -D` — now lives in
# `loom-daemon/src/worktree_cli/{remove,branch_delete,branch_landed,default_branch}.rs`,
# along with the full design rationale it used to carry inline.
#
# The contract this entry point preserves, verbatim: the verb names
# (`remove`/`--remove`), the flags (`--keep-branch`, `--force|-f`,
# `--dry-run|-n`, `--json`), exit 0 for a removal AND for the idempotent
# "nothing there" no-op AND for every `--dry-run`, exit 1 for a refusal or a
# failed removal, the `--json` document's field set, and the `.loom-managed`
# sentinel contract (only sentinel-bearing worktrees are ever removed).
# `CLAUDE.md`, `builder-worktree.md` and `defaults/docs/troubleshooting.md` all
# name this by path, and operators chain it with `&&`.
#
# Three helpers went with it and are NOT re-implemented here: the dirty-line
# filter (now `worktree_ops::safety::is_loom_own_untracked_path`, shared with
# the daemon's own reclaim path since #8279), the attached-branch porcelain
# parse, and — most importantly — the `awk`-extract-and-`eval` of
# `_maybe_delete_local_branch` out of the live `merge-pr.sh` source. That
# contraption existed only because bash has no import mechanism; Rust does, so
# `merge-pr.sh`'s own port (#8191) can import the rule instead of the script
# re-deriving it at runtime. Until then the two are pinned to each other by a
# test that greps `merge-pr.sh` for every message string.
#
# LOOM_SCRIPT_HELPER_MISSING_RC=2 — argued, not defaulted:
#
#   0 and 1 are both ANSWERS here, and they are the two answers an operator
#   acts on destructively. 0 means "that worktree is gone (or was never
#   there)"; 1 means "I looked and refused, nothing was deleted". An
#   unresolvable binary is neither, and it must never be mistaken for either:
#   read as 0, a caller proceeds as though a worktree with uncommitted work had
#   been safely removed; read as 1, it looks like a considered refusal that an
#   operator may then override with --force. 2 is the code every other
#   epic-#7810 stub reserves for "could not run at all", so an operator reading
#   an exit code gets one consistent answer across all of them.
#
# The missing-library case takes the same code, deliberately NOT left to
# `set -e`: a failed `source` under `set -e` aborts with 1, which is the
# REFUSAL code, so a partially-resynced `.loom/` would present as "I considered
# your worktree and declined" rather than "this install is broken".
# requires-daemon: worktree-remove >= 0.19.340  #8471 (#8195 slice 3) — the removal-verb port; without it the stub exits 2 and the verb refuses
_worktree_remove_verb() {
    local helper
    helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/script-helper.sh"
    if [[ ! -f "$helper" ]]; then
        print_error "lib/script-helper.sh is missing — cannot run 'remove'."
        echo "This install is incomplete; re-run the Loom installer or resync .loom/." >&2
        exit 2
    fi
    # shellcheck source=lib/script-helper.sh
    source "$helper"
    LOOM_SCRIPT_HELPER_MISSING_RC=2 \
        loom_exec_script_helper worktree-remove "$@"
}

# --------------------------------------------------------------------------
# WIP-shelving verbs: snapshot / stash-push / stash-pop
# --------------------------------------------------------------------------
#
# Ported to `loom-daemon worktree-wip` (#8195 slice 2, epic #7810). The three
# verbs that shelve a tree's uncommitted work — and, for `stash-push`, run
# `git reset --hard HEAD` once the capture has succeeded — now live in
# `loom-daemon/src/worktree_cli/{snapshot,baseline,wip}.rs`, where the full
# design rationale they used to carry inline also moved.
#
# The contract this entry point preserves, verbatim: the verb names and their
# flags, `<issue-number>` (plus the literal `main` for the stash pair), exit 0
# for success INCLUDING the legitimate no-ops, exit 1 for a refusal, and the
# `--json` stdout-purity split. Role prompts (`builder.md`,
# `builder-worktree.md`, `doctor.md`), `defaults/docs/guard-hooks.md` and the
# `stash-scope` guard's own deny message all name these by path.
#
# LOOM_SCRIPT_HELPER_MISSING_RC=2 — argued, not defaulted:
#
#   These verbs already use 0 and 1 as ANSWERS. 0 means "your work is captured"
#   (`stash-push`) or "your work is back" (`stash-pop`); 1 means "I refused and
#   changed nothing". Leaving the helper's default of 1 would make an
#   unresolvable binary indistinguishable from a refusal — survivable — but
#   there is no code left that could mean "could not run", and the two failures
#   want opposite handling: a refusal is a fact about your tree, an unresolvable
#   binary is a fact about the host. 2 is the code every other epic-#7810 stub
#   reserves for exactly that, so an operator reading an exit code gets one
#   consistent answer across all of them.
#
#   What must NEVER happen is exit 0. A caller that read "captured" from a
#   binary that never ran would go on to `git reset --hard` nothing, run its
#   baseline check against an uncleaned tree, and then `stash-pop` a capture
#   that does not exist. `loom_exec_script_helper` only ever `exec`s or exits
#   non-zero, so that outcome is unreachable by construction rather than by
#   convention.
#
# The missing-library case is handled the same way, and deliberately NOT left
# to `set -e`: a `source` that fails under `set -e` aborts with 1, which is the
# REFUSAL code, so a partially-resynced `.loom/` would present as "the verb
# considered your tree and declined" rather than "this install is broken". The
# explicit check below reports 2 instead — the one thing the exit codes must
# never do is lie about which of those happened.
# requires-daemon: worktree-wip >= 0.19.224  #8433 (#8195 slice 2) — the WIP-verb port; without it the stub exits 2 and the verbs refuse
_worktree_wip_verb() {
    local verb="$1"
    shift
    local helper
    helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/script-helper.sh"
    if [[ ! -f "$helper" ]]; then
        print_error "lib/script-helper.sh is missing — cannot run '$verb'."
        echo "This install is incomplete; re-run the Loom installer or resync .loom/." >&2
        exit 2
    fi
    # shellcheck source=lib/script-helper.sh
    source "$helper"
    LOOM_SCRIPT_HELPER_MISSING_RC=2 \
        loom_exec_script_helper worktree-wip "$verb" "$@"
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

# (`is_sparse_enabled` lived here and had no caller anywhere in the tree — not
# in this script, not in any sibling, not in any test. Removed in #8193 to pay
# for the lease call site below: `defaults/scripts/` is the shell budget's
# `contract` (portable) pool, whose growth `check_against_rev` refuses with no
# `Shell-Budget-Growth:` override available, so new reach into loom-daemon here
# has to be funded by retiring portable lines. `git log -S is_sparse_enabled`
# has it if it is ever wanted back.)

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
  pnpm worktree remove <N> [--keep-branch] [--force] [--dry-run]
                                                        Remove one managed worktree
                                                        (--dry-run: report the plan only)
  pnpm worktree snapshot <N> [--include-untracked] [--json]
                                                         Save uncommitted WIP as a patch file
  pnpm worktree stash-push <N|main> [--include-untracked] [--json]
                                                         Capture WIP, reset to a clean baseline
  pnpm worktree stash-pop <N|main> [--json]             Restore WIP captured by stash-push
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

  pnpm worktree stash-push main / stash-pop main
    Same clean-and-restore pair, but for the PRIMARY CLONE, anchored to
    refs/loom/stash-baseline/main (#6076). This is what a role that
    legitimately runs in the main checkout (Judge, Champion, Auditor, Guide,
    Hermit) should use instead of raw 'git stash' + 'git stash pop' there:
    the main checkout's refs/stash stack is operator-owned, and a raw pop in
    it is an unanswerable stash-scope:main-checkout ask in a headless run.
    Never touches refs/stash, so it needs no guard bypass.

  pnpm worktree stash-push 42 --json / stash-pop 42 --json
    Output: {"success": true, "issueNumber": 42, "target": "42", "hasTrackedChanges": true, "untrackedCount": 0, "ref": "refs/loom/stash-baseline/issue-42"}
            {"success": true, "issueNumber": 42, "target": "42", "restoredTracked": true, "restoredUntrackedCount": 0}
    For 'main', issueNumber is null and target is "main".

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
                                    (default 600 — covers the pre-add git
                                    prep (prune/fetch) plus 'git worktree
                                    add' itself; the lock is released as soon
                                    as the add returns, before sentinel
                                    writing, submodule init or the
                                    post-worktree hook run)
  LOOM_WORKTREE_LOCK_POLL_INTERVAL  Lock poll interval in seconds (default 2)
  LOOM_PRESERVE_WORKTREE            Disable cleanup-on-merge for all worktrees

Project-Specific Hooks:
  Create .loom/hooks/post-worktree.sh to run custom setup after worktree creation.
  This file is NOT overwritten by Loom upgrades.

  Declaring a repo-owned file under .loom/hooks/: no manifest entry, naming
  convention, or sentinel is required. Every uninstall/reinstall path
  (including a --clean reinstall) computes its removal candidates from Loom's
  own defaults/hooks/ -- per-repo .loom/hooks/ copies are outside that
  ownership boundary entirely (Epic #3835 Phase 5, #4262: hooks execute from
  the machine-level checkout, not the per-repo copy), so nothing under
  .loom/hooks/ is ever swept as "unmanaged" on uninstall, whatever its name.
  This is enforced, not just documented (issue #5971) -- a real consumer
  incident lost a repo-owned .loom/hooks/post-worktree.sh to a --clean
  reinstall before the fix. A fresh --quick install still COPIES the
  current defaults/hooks/*.sh names into .loom/hooks/ (install_hooks_and_cli)
  -- an existing file there is preserved unless the install explicitly forces
  an overwrite (--clean / --force), matching a same-named Loom-shipped hook.

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
# `--remove <N>` is not rejected as "Issue number must be numeric". The
# subcommand parses its own args (issue number + the four flags).
#
# `_worktree_remove_verb` execs `loom-daemon worktree-remove` and never
# returns, so there is no `&& exit 0` pair here any more — the subcommand's own
# exit code reaches the caller directly. See the function for the exit-code
# contract and the LOOM_SCRIPT_HELPER_MISSING_RC choice.
if [[ "$1" == "remove" || "$1" == "--remove" ]]; then
    shift
    _worktree_remove_verb "$@"
fi

# Worktree-scoped WIP-shelving verbs: `snapshot` (#4778) and the
# `stash-push`/`stash-pop` pair (#5217; `main` target added by #6076).
# Dispatched HERE, before the generic numeric-issue-number validation below,
# for the same reason `remove` is: `snapshot <N>` / `stash-push <N|main>` must
# not be rejected as "Issue number must be numeric".
#
# `_worktree_wip_verb` execs `loom-daemon worktree-wip` and never returns, so
# there is no `&& exit 0` pair here any more — the subcommand's own exit code
# reaches the caller directly. See the function for the exit-code contract and
# the LOOM_SCRIPT_HELPER_MISSING_RC choice.
if [[ "$1" == "snapshot" || "$1" == "stash-push" || "$1" == "stash-pop" ]]; then
    _worktree_wip_verb "$@"
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

# Safety-net release on any exit path (success, failure, signal) reached
# BEFORE the explicit release right after `git worktree add` below. Once that
# explicit release runs it clears WORKTREE_LOCK_TOKEN, which makes this trap
# a no-op for the (expected, common) case where we already released
# promptly (issue #6014 — the lock must not be held through submodule init /
# the post-worktree hook). $WORKTREE_LOCK_TOKEN is expanded when the trap
# actually fires, not when it is registered, so it always reflects whichever
# acquisition (or lack thereof) is current at that time.
trap 'release_worktree_lock "$ISSUE_NUMBER" "$WORKTREE_LOCK_TOKEN"' EXIT INT TERM

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
    # #7765: this rewrite used to be silent, which made an explicit branch
    # argument that named an EXISTING branch (e.g. `worktree.sh 7710
    # docs/onboarding-cleanup`, intending to attach to that already-checked-out
    # branch) miss it via the near-miss name and fall through to a fresh
    # branch instead - surprising enough that it caused a real misdiagnosis
    # (see the issue's follow-up comment). Say what it resolved to.
    if [[ "$JSON_OUTPUT" != "true" ]]; then
        print_info "Custom branch '$CUSTOM_BRANCH' resolved to '$BRANCH_NAME' (feature/ prefix applied)"
    fi
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

# --- Lease this claim's liveness (#8193) -------------------------------------
# An in-session Task-tool Builder claims `loom:building` and then publishes no
# liveness record of any kind: `SweepRegistry::dispatch` never ran for it, so
# there is no journal entry and no `write_lease_comment` (#6179), and the
# in-session publish step lives in the SWEEP orchestrator's prompt, not the
# builder's. `claim_reconciliation`'s Phase-2 gate (#6286) then reads
# `lease_evidence=absent` and reclaims a claim that is actively being worked --
# three such reclaims on one six-builder wave, 2026-09-17.
#
# Here, rather than in `builder.md`, for the reason #7672 established: a
# prose-mandated lease step was skipped by exactly one session and cost ~2.5h of
# fleet claim/yield thrash. Every builder already runs this script immediately
# after claiming, so this is the one call site that cannot be forgotten. It sits
# at pre-flight (before the create/reuse branch below) so it covers every way
# this script can conclude, which is also `sweep-lease-publish.sh`'s own
# documented publish-at-pre-flight semantics.
#
# `--watch-pid` is `${CLAUDE_PID:-$PPID}` and NEVER `$$`: `$$` is the one-shot
# tool-call subshell, which exits the instant the call returns, so the renewal
# loop would self-terminate on its first wake-up. The remaining policy -- the
# no-op when the daemon already published (#7672), the refusal outside an agent
# session, the 4h renewal cap -- lives in `loom-daemon lease ensure`, per
# ADR-0018 and because this file's `contract` category admits no growth.
_LEASE_DAEMON_BIN="$(loom_resolve_self_daemon_bin 2>/dev/null || true)"
[[ -z "$_LEASE_DAEMON_BIN" ]] || "$_LEASE_DAEMON_BIN" lease ensure "$ISSUE_NUMBER" --watch-pid "${CLAUDE_PID:-$PPID}" > /dev/null 2>&1 || true

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

        # #6257: this "worktree directory + branch already registered with
        # git" fast path is a completely different code path from the
        # "local branch exists, no worktree dir yet" reuse path below
        # (#6095/#6100) — that fix's upstream-tracking correction never runs
        # here, so a worktree left with stale HEAD and/or wrong upstream
        # tracking was silently "preserved" (below) and handed straight to a
        # Judge/Doctor session with no signal that it no longer matched the
        # branch's actual pushed tip. Correct/report drift against the
        # branch's OWN upstream (not just BASE_REF, computed above) before
        # deciding whether to preserve.
        git -C "$WORKTREE_PATH" fetch origin "$BRANCH_NAME" 2>/dev/null || true
        if git -C "$WORKTREE_PATH" show-ref --verify --quiet "refs/remotes/origin/$BRANCH_NAME"; then
            wt_current_upstream="$(git -C "$WORKTREE_PATH" rev-parse --abbrev-ref "$BRANCH_NAME@{u}" 2>/dev/null || true)"
            if [[ "$wt_current_upstream" != "origin/$BRANCH_NAME" ]]; then
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    if [[ -n "$wt_current_upstream" ]]; then
                        print_warning "Worktree branch '$BRANCH_NAME' was tracking '$wt_current_upstream' - correcting to 'origin/$BRANCH_NAME'"
                    else
                        print_info "Worktree branch '$BRANCH_NAME' has no upstream - setting it to 'origin/$BRANCH_NAME'"
                    fi
                fi
                git -C "$WORKTREE_PATH" branch --set-upstream-to="origin/$BRANCH_NAME" "$BRANCH_NAME" 2>/dev/null || true
            fi

            wt_head_sha="$(git -C "$WORKTREE_PATH" rev-parse HEAD 2>/dev/null || true)"
            wt_origin_tip="$(git -C "$WORKTREE_PATH" rev-parse "origin/$BRANCH_NAME" 2>/dev/null || true)"
            if [[ -n "$wt_head_sha" && -n "$wt_origin_tip" && "$wt_head_sha" != "$wt_origin_tip" ]] && \
               git -C "$WORKTREE_PATH" merge-base --is-ancestor "$wt_head_sha" "$wt_origin_tip" 2>/dev/null; then
                # Local HEAD is a strict ancestor of the branch's pushed tip -
                # i.e. genuinely behind (not just diverged/ahead with unpushed
                # local commits, which is expected and not drift).
                if [[ "$JSON_OUTPUT" != "true" ]]; then
                    print_warning "Worktree HEAD ($wt_head_sha) is behind the pushed tip of branch '$BRANCH_NAME' ($wt_origin_tip) - this worktree may be stale"
                    if [[ -n "$local_uncommitted" ]]; then
                        print_warning "Worktree also has uncommitted changes - resolve before evaluating/building on it:"
                        print_info "  ./.loom/scripts/worktree.sh snapshot $ISSUE_NUMBER --include-untracked   # save WIP"
                        print_info "  git -C $WORKTREE_PATH checkout -- .                                       # clear tracked working-tree drift"
                        print_info "  git -C $WORKTREE_PATH pull --ff-only                                      # resync to origin/$BRANCH_NAME"
                    else
                        print_info "  git -C $WORKTREE_PATH pull --ff-only   # resync to origin/$BRANCH_NAME"
                    fi
                fi
            fi
        fi

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
            # #6334: re-check commits-ahead and tracked-diff state immediately
            # before the destructive reset rather than trusting the
            # point-in-time check above — a second builder (or any other
            # process) can have landed new commits or foreign uncommitted
            # tracked changes into this worktree in the interim. Rescue/refuse
            # instead of silently discarding them (see lib/worktree-race-rescue.sh
            # for the full design decision).
            if git -C "$WORKTREE_PATH" fetch origin "${BASE_BRANCH:-$DEFAULT_BRANCH}" 2>/dev/null && \
               loom_worktree_reset_or_rescue "$WORKTREE_PATH" "$BASE_REF" "issue-$ISSUE_NUMBER-stale-worktree-reset"; then
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
        print_warning "Branch '$BRANCH_NAME' already exists - reusing it (for a fresh branch instead, pass a custom name: ./.loom/scripts/worktree.sh $ISSUE_NUMBER <custom-branch-name>)"
    fi

    # #6095: a pre-existing local branch can be carrying a stale or wrong
    # upstream (e.g. left tracking origin/$DEFAULT_BRANCH from whatever
    # created it, rather than its own PR branch) — and unlike the sibling
    # "no local branch, but origin/$BRANCH_NAME exists" path just below
    # (#4823), this reuse path never touched tracking at all, so the wrong
    # upstream persisted across every subsequent worktree.sh invocation. A
    # later `git pull --ff-only` in the reused worktree then silently
    # fast-forwards the branch onto the WRONG upstream's tip instead of
    # the branch's own remote history (observed on #6086/PR #6093: local
    # feature/issue-6086 tracked origin/main, and a --ff-only pull moved it
    # to main's tip). If origin has a branch of the same name, (re)point the
    # local branch's upstream at it before handing the branch to `git
    # worktree add`. If origin has no branch of this name (never pushed),
    # leave tracking as-is — do not fabricate an upstream that doesn't exist.
    # (The two-deep message dispatch below is one physical line, not two
    # separate `if`s, to keep this reuse arm's net line count in the shell
    # budget ratchet's portable pool flat — see the #8280 comment below for
    # why that budget was worth spending on.)
    git fetch origin "$BRANCH_NAME" 2>/dev/null || true
    if git show-ref --verify --quiet "refs/remotes/origin/$BRANCH_NAME"; then
        current_upstream="$(git rev-parse --abbrev-ref "$BRANCH_NAME@{u}" 2>/dev/null || true)"
        if [[ "$current_upstream" != "origin/$BRANCH_NAME" ]]; then
            if [[ "$JSON_OUTPUT" != "true" ]]; then if [[ -n "$current_upstream" ]]; then print_warning "Branch '$BRANCH_NAME' was tracking '$current_upstream' - correcting to 'origin/$BRANCH_NAME'"; else print_info "Branch '$BRANCH_NAME' has no upstream - setting it to 'origin/$BRANCH_NAME'"; fi; fi
            git branch --set-upstream-to="origin/$BRANCH_NAME" "$BRANCH_NAME" 2>/dev/null || true
        fi
    fi

    # #8280: the sibling arm below refuses an already-LANDED branch via the
    # shared `branch_landed` primitive (#5657/#7812) before reusing it, and
    # warns when the reused branch lacks the base ref's history. This arm did
    # neither, so a stale local feature/issue-N left from an earlier slice was
    # reused in SILENCE — yielding a worktree tens of commits behind the base,
    # on a merged PR's branch, and a PR that re-proposed already-merged code
    # with no CI. A surviving local ref is the normal state on a host that
    # built the previous slice, which is exactly the partial-increment case
    # #5657 was written for.
    #
    # Unlike the sibling arm, this one cannot silently fall through to a fresh
    # branch on `landed` — the name is already taken locally — so it refuses
    # outright and names the fix. `unknown` (forge outage, or the tree check
    # unavailable) keeps today's fail-open-to-reuse behaviour, same as the
    # sibling arm — a forge outage must never block worktree creation.
    #
    # The extra SHA guard below excludes the degenerate case `branch_landed`'s
    # own ancestry rung cannot tell apart from a real landing: a branch tip
    # that is IDENTICAL to origin/$DEFAULT_BRANCH's current tip is trivially
    # its own ancestor, which is exactly the state of a brand-new local branch
    # that has not yet carried any work (worktree.sh's own default creation
    # path, and test-worktree-json-purity.sh's branch-reuse/auto-recovery
    # fixtures). Refusing THAT reuse would break the ordinary re-run case; a
    # branch tip that differs from the current default tip is never this
    # degenerate case, whichever rung answered.
    branch_landed "$BRANCH_NAME" "$DEFAULT_BRANCH"
    if [[ "$BRANCH_LANDED_VERDICT" == "landed" ]] && [[ "$(git rev-parse "$BRANCH_NAME" 2>/dev/null)" != "$(git rev-parse "origin/$DEFAULT_BRANCH" 2>/dev/null)" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then echo '{"success": false, "error": "branch-already-landed", "issueNumber": '"$ISSUE_NUMBER"', "branch": "'"$BRANCH_NAME"'", "prNumber": '"${BRANCH_LANDED_PR_NUMBER:-null}"'}' >&3; else print_error "Local branch '$BRANCH_NAME' has already landed on $BASE_DISPLAY${BRANCH_LANDED_PR_NUMBER:+ (already-merged PR #$BRANCH_LANDED_PR_NUMBER)} - refusing to reuse it. Delete it and re-run: git branch -D $BRANCH_NAME && ./.loom/scripts/worktree.sh $ISSUE_NUMBER"; fi
        exit 1
    fi
    if [[ "$JSON_OUTPUT" != "true" ]] && ! git merge-base --is-ancestor "$BASE_REF" "$BRANCH_NAME" 2>/dev/null; then print_warning "Branch '$BRANCH_NAME' has diverged from $BASE_DISPLAY (does not contain all of its history) - reusing it as-is; rebase or delete it if that is not what you want"; fi

    CREATE_ARGS=("$WORKTREE_PATH" "$BRANCH_NAME")
else
    # No local branch by this name. Before falling back to a fresh branch off
    # BASE_REF, resolve the name against origin AND the forge: an existing
    # pushed PR branch from a prior Builder/Doctor cycle (#4823), a stale
    # already-merged one whose ref origin still carries (#5657), or an open PR
    # whose head never appears as origin/<branch> at all (#7765 — a fork PR's
    # cross-repo head, or a same-repo head the plain-name fetch missed).
    # The whole decision lives in lib/worktree-forge-pr-check.sh: it sets
    # _WT_REUSE_REMOTE_BRANCH, or exits non-zero rather than create a branch
    # that would silently shadow a real PR. Independent of --base, which only
    # chooses the start point when we DO create a fresh branch, below.
    _worktree_resolve_origin_branch_reuse "$BRANCH_NAME" "$ISSUE_NUMBER" "$JSON_OUTPUT" "$BASE_DISPLAY" "$BASE_REF" "$DEFAULT_BRANCH"

    if [[ "$_WT_REUSE_REMOTE_BRANCH" == "true" ]]; then
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
    # Example: "fatal: 'feature/issue-2853' is already used by worktree at '/path/to/loom'"
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
    # Release the git-race-prevention lock now — the operation that required
    # repo-global serialization (git worktree add's contention on
    # .git/config.lock) is complete. Everything below (sentinel writing,
    # submodule init, the project-specific post-worktree hook) does not
    # touch .git/config.lock and must not block unrelated worktree creations
    # for other issues (issue #6014). Clearing WORKTREE_LOCK_TOKEN makes the
    # EXIT trap's later release_worktree_lock call a no-op.
    release_worktree_lock "$ISSUE_NUMBER" "$WORKTREE_LOCK_TOKEN"
    WORKTREE_LOCK_TOKEN=""

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

    # --------------------------------------------------------------------
    # Shared-artifact symlinks + their .git/info/exclude entries
    # --------------------------------------------------------------------
    #
    # Ported to `loom-daemon worktree-link` (#8195 slice 4, epic #7810). The
    # four link families — root node_modules, nested per-package node_modules
    # for pnpm/monorepo layouts (#3528), `worktree.linkPaths` from the config
    # tier chain (#4062) and `.mcp.json` — plus the idempotent info/exclude
    # bookkeeping that keeps `git add -A` from staging any of them (#5474),
    # now live in `loom-daemon/src/worktree_cli/link.rs` with the full design
    # rationale they used to carry inline.
    #
    # This family, and not another arm of the create path, because it is the
    # one that is ALL path interpolation: four `ln -s "$src" "$dst"` pairs, a
    # `find -print0 | read -r -d ''` loop, a `${pkg_dir#"$prefix"/}` strip and
    # a `grep -qxF "$entry" "$file"`. That is #7858's class — an unquoted path
    # that turned a guard into an `rm -rf` on a live worktree — and in Rust a
    # path is an OsString that `symlink()` takes whole, so the class is gone by
    # construction rather than by review.
    #
    # The contract this call site preserves, verbatim: the message text and
    # ORDER (operators read this output), silence under --json (fd 1 is
    # already stderr there, so `--quiet` suppresses rather than reroutes), and
    # best-effort semantics — a failed link warns and worktree creation still
    # succeeds, which is why the exit code is discarded here and `worktree-link`
    # returns 0 unconditionally.
    #
    # No daemon binary means the links are simply not made: the worktree is
    # usable and merely rebuilds what it could have borrowed. That is the one
    # honest answer for a best-effort step, and unlike the `remove`/`wip`
    # slices there is no destructive operation whose silent skip could be
    # mistaken for a completed one — so it WARNS rather than exiting 2, and
    # `worktree.sh <issue>` keeps working on a host with no loom-daemon at all.
    # $_LEASE_DAEMON_BIN is resolved at pre-flight above; the name is
    # historical (#8193) and it is simply "the daemon that implements this
    # script", honouring $LOOM_DAEMON_SELF_BIN.
    # requires-daemon: worktree-link optional   #8195 slice 4 — a daemon predating the port skips the symlinks with a warning; the worktree is created either way
    MAIN_WORKSPACE_DIR=$(git rev-parse --show-toplevel 2>/dev/null)
    WT_LINK_FLAGS=()
    [[ "$JSON_OUTPUT" != "true" ]] || WT_LINK_FLAGS=(--quiet)
    if [[ -n "${_LEASE_DAEMON_BIN:-}" ]]; then
        "$_LEASE_DAEMON_BIN" worktree-link --repo-root "$MAIN_WORKSPACE_DIR" \
            --worktree "$ABS_WORKTREE_PATH" "${WT_LINK_FLAGS[@]}" || true
    elif [[ "$JSON_OUTPUT" != "true" ]]; then
        print_warning "No loom-daemon resolved - skipping node_modules/.mcp.json/linkPaths symlinks (worktree still created)"
    fi

    # Run project-specific post-worktree hook if it exists
    # This allows projects to add custom setup steps (e.g., pnpm install, lake exe cache get)
    # The hook is stored in .loom/hooks/ which is NOT overwritten by Loom upgrades
    # Note: MAIN_WORKSPACE_DIR is already set by the worktree-link section above
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
    # git worktree add failed — the operation the lock guards is over
    # (unsuccessfully); release it immediately rather than holding it
    # through error reporting / exit (issue #6014).
    release_worktree_lock "$ISSUE_NUMBER" "$WORKTREE_LOCK_TOKEN"
    WORKTREE_LOCK_TOKEN=""

    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "Failed to create worktree"}' >&3
    fi
    # Human-readable error already printed by _try_worktree_add / _handle_feature_branch_in_main_worktree
    exit 1
fi
