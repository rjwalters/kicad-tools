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
#   pnpm worktree --help                               # Show help

set -e

# The --sparse always-included safety set (.claude .loom .githooks scripts, plus
# $LOOM_WORKTREE_ALWAYS_INCLUDE) lives with the rest of sparse mode in
# `loom-daemon worktree-sparse` since #8195 slice 10 - see _worktree_sparse.

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
#
# #8458's per-worktree CARGO_TARGET_DIR needs nothing sourced here either: the
# create path below drives `loom-daemon cargo-target-dir provision` straight
# off the located binary, and every removal path reads the marker through the
# same daemon (`cargo-target-dir is-attributable|marker`).

# Shared "has this branch landed?" primitive (#7812): forge PR state first,
# then `git merge-tree --write-tree` tree equality, answering landed /
# not-landed / unknown. Replaces this script's two private squash heuristics
# (the deleted `_worktree_merged_pr_head_sha` and merge-pr.sh's
# `_worktree_branch_fully_captured`). Sourced defensively with a fail-closed
# `unknown` fallback for the same reason as the ledger/target-dir libs above:
# a partially-resynced .loom/ must degrade to "cannot tell, keep the branch",
# never to a `source` failure that breaks worktree creation and removal.
#
# Since #8195 slice 14 this file has no direct caller of its own: the LOCAL
# branch arm's call moved into `loom-daemon worktree-branch-reuse`, which asks
# the Rust primitive. The source stays because lib/worktree-forge-pr-check.sh
# (the sibling origin/<branch> arm) still runs in THIS process and needs it.
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
#
# Ported to `loom-daemon worktree-reset` (#8195 slice 6): the lib now keeps the
# function name and its 0/1/2 contract and delegates the body to
# `loom-daemon/src/worktree_cli/reset.rs`. No daemon means the wrapper returns 1
# ("refused, nothing changed"), which lands on the "Could not reset stale
# worktree (continuing to use as-is)" arm below — so a host without one loses a
# reset, never data.
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
    local common abs_common
    common=$(git rev-parse --git-common-dir 2>/dev/null || true)
    [[ -n "$common" ]] || { echo ".loom/locks"; return 0; }
    # git-common-dir may be returned as a relative path; resolve it.
    abs_common=$(cd "$common" 2>/dev/null && pwd) || abs_common="$common"
    echo "$(dirname "$abs_common")/.loom/locks"
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
# $_WT_LOCK_DELEGATED records which of the two implementations below minted the
# live token, so release_worktree_lock always returns it to the same one.
WORKTREE_LOCK_HOLDER_PID=""; WORKTREE_LOCK_TOKEN=""; _WT_LOCK_DELEGATED=false

# DELEGATION (#8195 slice 7, epic #7810): `loom-daemon worktree-lock
# acquire`/`release` (loom-daemon/src/worktree_cli/lock.rs, ported in slice 1 /
# #8226) is the canonical implementation of everything below, and is tried
# FIRST. The mkdir body kept underneath it is the fallback, unchanged.
#
# Why a fallback and not the hard `exec` delegation remove/wip/cleanup/reset
# use: this lock sits on the ALWAYS-TAKEN create path. Slice 1 tried a hard
# delegation here and was reverted (#8226) — every hermetic suite with no built
# Rust binary broke, because under an `exec` contract a missing binary is
# indistinguishable from "no lock taken". Nor is there a safe "refuse" answer
# for a lock the way there is for a destructive verb: skipping serialization on
# a host with no daemon reopens the #3380 race (concurrent `git worktree add`
# hanging on `.git/config.lock`) for exactly the hosts most likely to lack a
# build — CI.
#
# So a daemon answer is trusted only on POSITIVE evidence: exit 0 with a
# non-empty `TOKEN=` (acquired) or exit 1 with `HOLDER_PID=` (refused/timed
# out), which `worktree-lock acquire` always prints on those two paths and
# nothing else does. A missing binary, a daemon too old to know this subcommand
# family, its own rc-2 "locks dir unusable", or a differently-shaped
# `loom-daemon` on PATH that reuses exit 1 for "unrecognized subcommand" all
# read as "not an answer" and fall straight through to the shell body, getting
# exactly today's behaviour. Nothing here can make a no-daemon host worse.
# requires-daemon: worktree-lock optional   #8195 slice 7 — the add-lock delegation; without it the mkdir fallback below runs unchanged
acquire_worktree_lock() {
    local issue="$1" out; _WT_LOCK_DELEGATED=false
    if [[ -n "${_WT_DAEMON_BIN:-}" ]]; then
        out="$("$_WT_DAEMON_BIN" worktree-lock acquire --issue "$issue" --owner-pid "$$" \
            --timeout "$LOOM_WORKTREE_LOCK_TIMEOUT" --poll "$LOOM_WORKTREE_LOCK_POLL_INTERVAL" \
            2>/dev/null)" && out="0 $out" || out="$? $out"
        case "$out" in
            "0 TOKEN="?*)     WORKTREE_LOCK_TOKEN="${out#0 TOKEN=}"; _WT_LOCK_DELEGATED=true; return 0 ;;
            "1 HOLDER_PID="*) WORKTREE_LOCK_HOLDER_PID="${out#1 HOLDER_PID=}"; _WT_LOCK_DELEGATED=true; return 1 ;;
        esac
    fi

    local lock
    lock="$(_worktree_lock_path "$issue")"
    mkdir -p "$(_worktree_locks_dir)" 2>/dev/null || true

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
#
# Clears WORKTREE_LOCK_TOKEN on every path, which is what makes the EXIT trap's
# later call a no-op after an explicit release (it expands the global when it
# fires, not when it is installed).
#
# $_WT_LOCK_DELEGATED is set by acquire_worktree_lock above and read only here,
# so a token minted by the shell body is always released by the shell body even
# if a daemon became resolvable in between — it never does within one process,
# but nothing here depends on that. `loom-daemon worktree-lock release` already
# treats an empty or already-reassigned token as a safe no-op (#6014), matching
# this function's own contract exactly.
release_worktree_lock() {
    local issue="$1"
    local token="$2"
    WORKTREE_LOCK_TOKEN=""
    if [[ "$_WT_LOCK_DELEGATED" == "true" ]]; then
        "$_WT_DAEMON_BIN" worktree-lock release --token "$token" >/dev/null 2>&1 || true; return 0
    fi
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
# successful add, so a half-created dir never has one.
#
# Ported to `loom-daemon worktree-cleanup` (#8195 slice 5, epic #7810). The
# whole body — the lock sweep, the orphan guard's registered/not decision, the
# `rm -rf` it gates and the conditional prune — now lives in
# `loom-daemon/src/worktree_cli/cleanup.rs` with the design rationale it used
# to carry inline.
#
# WHY THIS FAMILY. Step 2 is the single most dangerous predicate in this file:
# a guard whose FALSE answer runs `rm -rf` on a directory that may hold another
# agent's uncommitted work — and it has answered falsely on a live worktree
# twice over (#7858/#7849), once because `awk '{print $2}'` truncates a
# porcelain path at its first space and once because the candidate was resolved
# logically rather than physically. Both halves are structural in Rust: the
# path is `line.strip_prefix("worktree ")` with nothing to word-split (and it
# is now literally `branch_delete::parse_worktree_porcelain`, the reader slice
# 3 already uses, rather than the "mirrors …" copy this comment used to admit
# to), and the candidate goes through `fs::canonicalize`, which has no logical
# variant to forget.
#
# THE CONTRACT THIS STUB PRESERVES, verbatim: the two warning texts and the
# order they print in, silence under --json (fd 1 is already stderr there, so
# `--quiet` suppresses rather than reroutes), and return 0 on every path.
#
# NO DAEMON MEANS NO CLEANUP, deliberately — this sits on the ALWAYS-TAKEN
# create path, where a hard dependency is exactly what got slice 1's lock
# delegation reverted (#8226). That degradation is honest rather than merely
# convenient because of its DIRECTION: a stale lock left in place makes `git
# worktree add` fail with git's own lock error, and an orphan dir left in place
# makes this script exit 1 with "Directory exists but is not a registered
# worktree", naming the `rm -rf` to run. Both are loud, non-destructive
# refusals — the pre-#3416 behaviour this cleanup was added to spare an
# operator. The DANGEROUS direction (deleting a live worktree) is unreachable
# when the code does not run at all, which is why this warns nothing and exits
# 0 rather than refusing the way the `remove`/`wip` verbs do at
# LOOM_SCRIPT_HELPER_MISSING_RC=2: there, a silent skip could be mistaken for a
# completed destructive operation; here there is nothing to mistake.
#
# requires-daemon: worktree-cleanup optional  #8195 slice 5 — a daemon predating the port simply does not clean crash debris; both stale-lock and orphan-dir debris then surface as the loud refusals described above, never as a silent removal
cleanup_partial_worktree_state() {
    local issue="$1"

    # $_WT_DAEMON_BIN is resolved ONCE per process, just above the create
    # path's first call to this function (#8195 slice 7 folded three separate
    # lazy resolutions — this one, the lease/claim-lock pair's, and the
    # add-lock's — into that single assignment, so a run that creates a
    # worktree pays one filesystem probe and a run that does not pays none).
    [[ -n "${_WT_DAEMON_BIN:-}" ]] || return 0

    # Two spellings rather than an array: `"${arr[@]}"` on an EMPTY array is an
    # unbound-variable error under `set -u` in bash 3.2 (macOS), which is a
    # supported host for this script.
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        "$_WT_DAEMON_BIN" worktree-cleanup "$issue" --quiet || true
    else
        "$_WT_DAEMON_BIN" worktree-cleanup "$issue" || true
    fi
    return 0
}

# --------------------------------------------------------------------------
# The LOCAL-branch reuse arm (#6095/#6100, #8280)
# --------------------------------------------------------------------------
#
# Ported WHOLE to `loom-daemon worktree-branch-reuse` (#8195 slice 14, epic
# #7810): the "already exists - reusing it" warning, the #6095/#6100
# upstream-tracking correction, the #8280 already-landed refusal behind its
# degenerate-tip guard, and the base-ref divergence warning. The full design
# rationale lives in `loom-daemon/src/worktree_cli/branch_reuse.rs`.
#
# WHY THIS ARM, AND WHY WHOLE. It is the arm `worktree.sh <N>` takes whenever
# `refs/heads/feature/issue-N` already exists — which on a host that built an
# earlier slice of the same issue is the NORMAL state, so it is the arm the
# partial-increment convention (#3667/#3599) runs through most often. Its four
# steps are one ordered contract and the ORDER is the part no reviewer can
# check from either side alone: steps 1-2 must precede the refusal so an
# operator sees which branch is being judged before it is named, and step 2's
# `git fetch origin -- <branch>` is what makes step 4's history comparison
# meaningful. Moving them one at a time would have left this file interleaving
# a Rust call between two Rust calls purely to preserve print order — the
# "delegation point that is not a decision boundary" shape #8226 reverted.
#
# It also retires the LAST `_worktree_upstream_check` call site (slice 12 took
# the other one, into `worktree-existing`), so the slice-9 wrapper is gone and
# `worktree-upstream` has no bash caller left at all; and it replaces a
# `--json` refusal document that was spliced around `$BRANCH_NAME` by hand.
# `git check-ref-format` permits a `"` in a refname and `$BRANCH_NAME` is
# `feature/$CUSTOM_BRANCH` — operator input — so that document was not valid
# JSON for a name a caller could really pass. Same defect slice 13 fixed for
# `$BASE_BRANCH`, one arm further down the same decision; `serde_json` now
# builds it.
#
# THE CONTRACT PRESERVED: every message and its ORDER, which of them are gated
# on --json (all of them), `print_error`'s stderr routing, the fail-open
# direction on an `unknown` verdict (a forge outage must never block worktree
# creation — test-worktree-stale-merged-branch.sh Test 5), the non-destructive
# refusal (the branch is left alone), and the two exit codes this caller
# branches on.
#
# NO DAEMON: none of the four steps runs and the branch is reused as-is, which
# is the pre-#6095 / pre-#8280 behaviour for that host. Every loss is a
# diagnosis: an upstream left as it was, and a landed branch reused instead of
# refused. Nothing here creates, deletes or resets anything — the refusal
# prevents a low-value PR, it does not prevent data loss — so this degrades
# rather than exiting 2, and `worktree.sh <N>` keeps working on a host with no
# loom-daemon at all. The `--help` probe routes a daemon predating the
# subcommand to the same place, because clap's exit 2 would otherwise be read
# as a refusal and strand an ordinary re-run.
#
# Stdout goes to fd 3 (a no-op in human mode, the caller's real stdout under
# --json) so the refusal document satisfies the #3546 purity contract; the
# human messages are suppressed by the port itself under --json, never
# rerouted into that stream.
# requires-daemon: worktree-branch-reuse optional  #8195 slice 14 — without it the reuse warning, the upstream correction, the already-landed refusal and the divergence warning are all skipped and the local branch is reused as-is (pre-#6095 / pre-#8280 behaviour). A lost diagnosis, not a lost file.
_worktree_branch_reuse() {
    if [[ -z "${_WT_DAEMON_BIN:-}" ]] \
        || ! "$_WT_DAEMON_BIN" worktree-branch-reuse --help >/dev/null 2>&1; then
        return 0
    fi
    "$_WT_DAEMON_BIN" worktree-branch-reuse --repo "$WORKTREE_REPO_ROOT" \
        --branch "$BRANCH_NAME" --issue "$ISSUE_NUMBER" \
        --default-branch "$DEFAULT_BRANCH" --base-ref "$BASE_REF" \
        --base-display "$BASE_DISPLAY" --json-output "$JSON_OUTPUT" >&3 || exit 1
}

# --------------------------------------------------------------------------
# The "worktree directory already exists" arm (#3548/#6257/#6291/#8287/#6334)
# --------------------------------------------------------------------------
#
# Ported WHOLE to `loom-daemon worktree-existing` (#8195 slice 12, epic #7810):
# the registration probe, the upstream/drift check, the preserve-vs-reset
# verdict, the sentinel back-fill and the stale-worktree `git reset --hard`
# behind its #6334 rescue guard. The full design rationale lives in
# `loom-daemon/src/worktree_cli/existing.rs`.
#
# WHY THIS ARM. It opened with the LAST unanchored
# `git worktree list | grep -q "$WORKTREE_PATH"` in this script — slice 10
# retired its twin on the --sparse/--full re-configure arm — and that expression
# is wrong in both directions. `git worktree list` prints symlink-RESOLVED paths
# while $WORKTREE_PATH is concatenated and unresolved, so a repo reached through
# a symlink (every macOS checkout under /tmp) read a LIVE worktree as
# unregistered and told the caller to `rm -rf` it; and the match is an
# unanchored substring, so an unregistered `issue-4` matched a registered
# `issue-44`, ran the drift check and the reset against the MAIN workspace
# (`git -C` on an unregistered dir resolves to the parent repo) and finished by
# writing a `.loom-managed` sentinel into crash debris. The port asks the orphan
# guard's own canonicalizing predicate (slice 5), the same one slice 10 moved to.
#
# THE CONTRACT PRESERVED: every message and its ORDER, which of them are gated
# on --json (the registered arm's are, the unregistered arm's are not), the
# exit codes (1 only for "not a registered worktree", 0 for all three usable
# outcomes), the #3548 sentinel back-fill on both preserve and reset, and the
# reset's target being the same reference the verdict was reached against
# (#8287). The reset still goes through the #6334 rescue guard — the port calls
# the very implementation `lib/worktree-race-rescue.sh` delegates to, with this
# shell's own PIDs passed so its liveness probe (#7463) does not count the
# invoker as a foreign holder.
#
# NO DAEMON: the predicate is answered here instead, in one PHYSICAL comparison
# (is this directory its own git toplevel?), and the answer is always "preserve"
# — the arm never resets and never back-fills the sentinel without the binary.
# Both losses are in the safe direction: a stale worktree left alone, and a
# worktree cleanup tooling refuses to remove, rather than one it removes
# wrongly. That is why this delegation is safe where slice 1's always-taken lock
# delegation was not (#8226): this arm is reached only by a re-invocation
# against a directory that already exists.
# requires-daemon: worktree-existing optional  #8195 slice 12 — without it the registration answer comes from the one-line physical check below and the verdict is always "preserve": no drift report, no sentinel back-fill and no stale-worktree reset, which is the pre-#6257 / pre-#3548 / pre-#3389 behaviour for that host. Nothing here creates or deletes anything.
_worktree_existing() {
    local _q="" _t
    [[ "$JSON_OUTPUT" != "true" ]] || _q="--quiet"
    if [[ -n "${_WT_DAEMON_BIN:-}" ]] && "$_WT_DAEMON_BIN" worktree-existing --help >/dev/null 2>&1; then
        # shellcheck disable=SC2086  # $_q is a fixed literal flag or empty
        "$_WT_DAEMON_BIN" worktree-existing --worktree "$WORKTREE_PATH" \
            --repo "$WORKTREE_REPO_ROOT" --issue "$ISSUE_NUMBER" --branch "$BRANCH_NAME" \
            --default-branch "$DEFAULT_BRANCH" --base-ref "$BASE_REF" \
            --base-display "$BASE_DISPLAY" --base-branch "${BASE_BRANCH:-}" \
            --ignore-pid "$$" --ignore-pid "${BASHPID:-$$}" $_q
        return $?
    fi
    _t="$(cd "$WORKTREE_PATH" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)" || _t=""
    [[ -n "$_t" ]] && [[ "$(cd "$_t" 2>/dev/null && pwd -P)" == "$(cd "$WORKTREE_PATH" 2>/dev/null && pwd -P)" ]] && return 0
    print_error "Worktree directory exists but is not the top level of a git worktree, and no loom-daemon with a 'worktree-existing' subcommand could be resolved to diagnose it: $WORKTREE_PATH"
    return 1
}

# Shared preamble for the `loom_exec_script_helper` verbs below (`remove`, the
# WIP trio, and `--check`): source lib/script-helper.sh, or exit 2 naming $1. The exec
# line itself deliberately stays in each caller with its subcommand spelled
# literally — scripts/check-daemon-subcommand-versions.sh reads a version floor
# off `loom_exec_script_helper <sub>` in command position, and hoisting that
# into a shared `"$@"` here would silently retire both `requires-daemon:`
# markers along with the fleet-floor visibility they exist for.
_worktree_source_script_helper() {
    local helper
    helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/script-helper.sh"
    if [[ ! -f "$helper" ]]; then
        print_error "lib/script-helper.sh is missing — cannot run '$1'."
        echo "This install is incomplete; re-run the Loom installer or resync .loom/." >&2
        exit 2
    fi
    # shellcheck source=lib/script-helper.sh
    source "$helper"
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
    _worktree_source_script_helper remove
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
# requires-daemon: cargo-target-dir optional  #8458 — per-worktree CARGO_TARGET_DIR; a host whose binary predates it (or has none) simply gets no per-worktree dir, which is the pre-#8458 behaviour
# requires-daemon: worktree-wip >= 0.19.224  #8433 (#8195 slice 2) — the WIP-verb port; without it the stub exits 2 and the verbs refuse
_worktree_wip_verb() {
    _worktree_source_script_helper "$1"
    LOOM_SCRIPT_HELPER_MISSING_RC=2 \
        loom_exec_script_helper worktree-wip "$@"
}

# --------------------------------------------------------------------------
# Sparse-checkout: --sparse <paths...> / --full
# --------------------------------------------------------------------------
#
# Ported to `loom-daemon worktree-sparse` (#8195 slice 10, epic #7810). The cone
# setup after `git worktree add --no-checkout`, the "worktree already exists"
# re-configure early exit, the always-included safety set, the size line and
# the cone-to-JSON builder now live in `loom-daemon/src/worktree_cli/sparse.rs`
# with the full design rationale (per-worktree config, idempotent `set`, ...).
#
# WHY THIS FAMILY. It is the one opt-in part of the create path, so it can
# leave the shell outright without putting the ordinary `worktree.sh <N>` on a
# built binary. And it carried three defects, each reproduced before the port:
# a cone git rejects (`src/*`, `/src`) killed the script mid-create with git's
# exit 128 in total silence (both streams /dev/null'd under `set -e`), leaving
# an empty --no-checkout worktree; the `awk` cone builder did not escape, so a
# `"` in a path broke the --json document; and the re-configure arm's "is this
# registered?" was `git worktree list | grep -q` - an unanchored substring
# match against symlink-RESOLVED paths, which refused a live worktree in a repo
# reached through a symlink and accepted an unregistered issue-4 beside a
# registered issue-44 (then ran git against the main workspace and wrote a
# sentinel into the unregistered dir). The port asks the orphan guard's own
# canonicalizing predicate (slice 5) instead.
#
# THE CONTRACT PRESERVED: every message and its order, silence under --json,
# the JSON documents' fields (now validly escaped), the git commands and the
# per-worktree config they write, and the #3548 sentinel back-fill - which the
# re-configure arm writes only once the directory is proven registered.
#
# NO DAEMON: --sparse/--full refuse with exit 2 before anything is touched (the
# gate right after argument parsing) - the code every epic-#7810 stub reserves
# for "could not run", as the `remove`/WIP verbs do. A best-effort skip is not
# available here: without the cone step, --sparse would report success over an
# empty --no-checkout worktree. A plain `worktree.sh <N>` never reaches this.
#
# $1 = arm (create|reconfigure), $2 = worktree path; the mode comes from the
# globals. Under --json the create arm's stdout is the cone array, captured into
# $CONE_JSON for the final document; the re-configure arm's is its own final
# document, so that caller points it at fd 3.
_worktree_sparse() {
    local args=(--arm "$1" --worktree "$2" --issue "$ISSUE_NUMBER" --branch "$BRANCH_NAME")
    [[ "$JSON_OUTPUT" != "true" ]] || args+=(--json)
    if [[ "$FULL_MODE" == "true" ]]; then args+=(--full); else args+=(-- "${SPARSE_PATHS[@]}"); fi
    if [[ "$1" == "create" && "$JSON_OUTPUT" == "true" ]]; then
        CONE_JSON="$("$_WT_DAEMON_BIN" worktree-sparse "${args[@]}")"
    else
        "$_WT_DAEMON_BIN" worktree-sparse "${args[@]}"
    fi
}

# Base-ref preparation: the `origin/$DEFAULT_BRANCH` fetch and the `--base`
# stacked-PR resolution (#3729). Ported WHOLE to `loom-daemon worktree-base`
# (#8195 slice 13, epic #7810) - they were one decision (which ref a new branch
# starts from) with one ordering constraint. The verb prints `TOKEN<TAB>text`
# records replayed through this script's own print_* helpers; BASE_REF /
# BASE_DISPLAY are data records, JSON is a complete --json failure document
# (now built by serde_json; the retired shell spliced $BASE_BRANCH in by hand).
# Without a usable daemon the default-branch fetch degrades to one best-effort
# `git fetch`, but --base REFUSES (exit 2, nothing touched): un-stacking a child
# onto the default branch silently is the failure the block existed to prevent.
# requires-daemon: worktree-base optional  #8195 slice 13 - without it only the default-branch fetch runs (silently); a plain `worktree.sh <N>` is unaffected and --base refuses with exit 2 before touching anything
_worktree_base() {
    local _l _m _out _rc=0 _q=""
    [[ "$JSON_OUTPUT" != "true" ]] || _q="--quiet"
    if [[ -n "${_WT_DAEMON_BIN:-}" ]] && "$_WT_DAEMON_BIN" worktree-base --help >/dev/null 2>&1; then
        # shellcheck disable=SC2086  # $_q is a fixed literal flag or empty
        _out="$("$_WT_DAEMON_BIN" worktree-base --default-branch="$DEFAULT_BRANCH" --base-branch="${BASE_BRANCH:-}" $_q)" || _rc=$?
        while IFS=$'\t' read -r _l _m; do
            case "$_l" in
                INFO) print_info "$_m" ;;  SUCCESS) print_success "$_m" ;;
                WARNING) print_warning "$_m" ;;  ERROR) print_error "$_m" ;;
                PLAIN) echo "$_m" ;;  JSON) echo "$_m" >&3 ;;
                BASE_REF) BASE_REF="$_m" ;;  BASE_DISPLAY) BASE_DISPLAY="$_m" ;;
            esac
        done <<<"$_out"
        return "$_rc"
    fi
    if [[ -n "$BASE_BRANCH" ]]; then
        [[ "$JSON_OUTPUT" != "true" ]] || echo '{"success": false, "error": "base-requires-loom-daemon"}' >&3
        print_error "--base needs 'loom-daemon worktree-base' (#8195 slice 13), and ${_WT_DAEMON_BIN:-no resolvable loom-daemon} cannot run it. Install or update loom-daemon, or re-run without --base."
        return 2
    fi
    git fetch origin -- "$DEFAULT_BRANCH" 2>/dev/null || true
}

# --------------------------------------------------------------------------
# In-worktree detection: the `--check` verb, and the create path's
# auto-navigation out of a worktree
# --------------------------------------------------------------------------
#
# Ported to `loom-daemon worktree-check` (#8195 slice 11, epic #7810). BOTH
# consumers of `check_if_in_worktree` moved together, because the predicate they
# shared was wrong from every position a caller can stand in:
#
#   [[ "$(git rev-parse --git-common-dir)" != "$(git rev-parse --show-toplevel)/.git" ]]
#
# `--show-toplevel` is always ABSOLUTE; `--git-common-dir` is RELATIVE to the
# current directory whenever it can be (`.git` at the repo root, `../.git` one
# level down). So in the primary clone the comparison read `.git` !=
# `/repo/.git` — true — and the function answered "in a worktree" there, in
# every subdirectory of it, and inside a real linked worktree alike. It had no
# reachable false branch at all (verified against git 2.43 from all four
# positions). Two consequences:
#
#   - `worktree.sh --check` reported the primary clone as a worktree and exited
#     0; its "Not currently in a worktree" arm and its exit 1 were dead code,
#     while the docs told operators to run it.
#   - every `worktree.sh <N>` from the primary clone printed four spurious
#     lines ("Currently in a worktree, auto-navigating…", a `Current worktree:`
#     block naming the primary clone, "Found main workspace: .", "Switched to
#     main workspace") and then `cd`'d to `dirname ".git"` = "." — a no-op BY
#     LUCK: git's relative answer happens to be the relative path to the repo
#     root, so `dirname` of it is too.
#
# Invisible for as long as it existed because `--json` suppresses all four of
# those lines, so nothing a machine reads ever changed, and no retained suite
# asserts either consumer's output. Same class as slices 5, 8 and 10: a path
# compared logically instead of physically. `worktree_cli/check.rs` compares
# `--git-dir` against `--git-common-dir`, both canonicalized — git's own
# definition of a linked worktree, and correct for a symlinked repo path and a
# `--separate-git-dir` checkout (where `.git` is a file) alike.
#
# LOOM_SCRIPT_HELPER_MISSING_RC=2 — argued, not defaulted: 0 and 1 are the
# verb's two ANSWERS, and a caller branches on them, so an unresolvable binary
# must not be readable as either. 2 is the code every other epic-#7810 stub
# reserves for "could not run at all".
# requires-daemon: worktree-check >= 0.19.492  #8195 slice 11 — the `--check` verb execs it and refuses with exit 2 without it. The create path's auto-navigation below probes with `--help` and falls back to its own one-line physical check, so a plain `worktree.sh <N>` still needs no daemon.
_worktree_check_verb() {
    _worktree_source_script_helper --check
    LOOM_SCRIPT_HELPER_MISSING_RC=2 \
        loom_exec_script_helper worktree-check
}

# Sets $_WT_IN_WORKTREE / $_WT_MAIN_WORKSPACE for the create path's
# auto-navigation below. The port prints a `LEVEL<TAB>text` record stream — the
# shape `merge-pr delete-branch` (#8973) established — replayed here through
# this script's own print_* so every message and its order stay owned by the
# port, while the `cd` stays here because only this process can change its own
# directory. `--quiet` under `--json` mirrors the blanket suppression the
# retired block applied to all four messages.
#
# The fallback is not a second implementation of the DECISION: it is the same
# physical question in one comparison (is this worktree's git dir the common
# one?), spelled here so nested-worktree prevention never depends on a built
# binary — a silent skip would let `git worktree add` create a worktree INSIDE
# another one, whose removal then takes the child with it.
# `loom-daemon/tests/worktree_check_differential.rs` asserts the two agree from
# all four positions. The `--help` probe routes a daemon predating the
# subcommand to the fallback as well: clap's exit 2 prints no records, which
# this loop would read as "not in a worktree" — the one wrong answer that loses
# the guard.
#
# What the fallback deliberately does NOT do is re-print the four banner lines.
# On a host with no resolvable daemon (or one predating this subcommand) the
# navigation is silent apart from the `✓ Switched to main workspace` below: the
# SAFETY property is preserved exactly, the DIAGNOSIS degrades — the same trade
# slices 4, 5 and 9 make with their `optional` markers. Copying the message text
# back into shell would recreate the second implementation this slice exists to
# remove, and it is the one thing the port cannot keep pinned.
_WT_IN_WORKTREE=false
_WT_MAIN_WORKSPACE=""
_worktree_locate() {
    local _l _m _out _q=""
    [[ "$JSON_OUTPUT" != "true" ]] || _q="--quiet"
    if [[ -n "${_WT_DAEMON_BIN:-}" ]] && "$_WT_DAEMON_BIN" worktree-check --help >/dev/null 2>&1; then
        # shellcheck disable=SC2086  # $_q is a fixed literal flag or empty
        _out="$("$_WT_DAEMON_BIN" worktree-check --porcelain $_q 2>/dev/null)" || _out=""
        while IFS=$'\t' read -r _l _m; do
            case "$_l" in
                IN_WORKTREE)    _WT_IN_WORKTREE=true ;;
                MAIN_WORKSPACE) _WT_MAIN_WORKSPACE="$_m" ;;
                WARNING)        print_warning "$_m" ;;
                INFO)           print_info "$_m" ;;
                PLAIN)          echo "$_m" ;;
                BLANK)          echo "" ;;
            esac
        done <<<"$_out"
        return 0
    fi
    local _gd _cd
    _gd="$(cd "$(git rev-parse --git-dir 2>/dev/null || echo .)" 2>/dev/null && pwd -P)" || _gd=""
    _cd="$(cd "$(git rev-parse --git-common-dir 2>/dev/null || echo .)" 2>/dev/null && pwd -P)" || _cd=""
    [[ -n "$_gd" && -n "$_cd" && "$_gd" != "$_cd" ]] || return 0
    _WT_IN_WORKTREE=true
    _WT_MAIN_WORKSPACE="$(dirname "$_cd")"
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

Sparse-Mode Notes:
  - --sparse and --full are mutually exclusive
  - --sparse requires at least one path
  - Re-running --sparse with the same cone is a clean no-op (idempotent)
  - Re-running --sparse with a different cone replaces the cone
  - Set LOOM_WORKTREE_ALWAYS_INCLUDE to add repo-specific safety paths
  - Needs loom-daemon (worktree-sparse); without it these flags exit 2, changing nothing

Safety Features:
  ✓ Detects if already in a worktree
  ✓ Uses sandbox-safe path (.loom/worktrees/)
  ✓ Pulls latest origin/main before creating worktree
  ✓ Automatically creates branch from main
  ✓ Prevents nested worktrees
  ✓ Non-interactive (safe for AI agents)
  ✓ Reuses existing branches automatically
  ✓ Symlinks node_modules from main (avoids npm install) — NOT on pnpm repos
  ✓ Symlinks nested per-package node_modules for monorepo workspaces
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
    - Nested per-package node_modules (e.g. apps/web/node_modules), found by scanning
      the main workspace for node_modules dirs next to a package.json. No YAML parser.
    - Extra gitignored paths listed in .loom/config.json under worktree.linkPaths,
      e.g. generated wasm-pack bindings that are expensive to rebuild per worktree:

        { "worktree": { "linkPaths": ["apps/web/src/wasm"] } }

  Each created symlink is added to the worktree's .git/info/exclude so 'git add -A'
  never stages it; linking is best-effort and never aborts worktree creation. Repos
  with no nested node_modules and no worktree.linkPaths config see no change.
  NEITHER node_modules family runs on a pnpm workspace (#8944; override: worktree.linkNodeModules
  true|false|"auto"). Pre-#8944 aliases: loom-daemon worktree-link --retire-aliases (#9152).

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

# `_worktree_check_verb` execs `loom-daemon worktree-check` and never returns,
# so the subcommand's own exit code (0 = in a worktree, 1 = the main working
# directory) reaches the caller directly — see the function for the contract and
# the LOOM_SCRIPT_HELPER_MISSING_RC choice.
if [[ "$1" == "--check" ]]; then
    _worktree_check_verb
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
CONE_JSON="[]"
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
        --full) FULL_MODE=true; shift ;;
        --base)
            BASE_BRANCH="$2"
            if [[ -z "$BASE_BRANCH" ]]; then
                print_error "--base requires a branch name"
                exit 1
            fi
            shift 2
            ;;
        # Proceed even though another session's live issue claim-lock is
        # found below (#8553) — see the check itself for what it guards.
        --force) FORCE_CLAIM_LOCK=true; shift ;;
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

# The ONE daemon-binary resolution for this whole run. It sits here, after the
# verb dispatch and argument parsing, so a `--check`/`--help`/`remove`/`snapshot`
# invocation never pays the filesystem probe, while every consumer below shares
# one answer: the --sparse/--full gate immediately after it, then crash-debris
# cleanup, the add lock, the lease + claim-lock pre-flight, submodules and
# worktree-link. A plain assignment, not a getter, so
# scripts/check-daemon-subcommand-versions.sh can textually trace every
# `"$_WT_DAEMON_BIN" <subcommand>` call back to a resolver entry point. Empty
# (never an error) when nothing resolves; each consumer degrades on its own
# terms, documented at each one. Script-relative, so the `cd` below cannot
# change its answer.
_WT_DAEMON_BIN="$(loom_resolve_self_daemon_bin 2>/dev/null || true)"

# --sparse/--full need `loom-daemon worktree-sparse` (#8195 slice 10): refuse
# HERE, before the cleanup, lock, lease or fetch below, so a host without one is
# told why and left exactly as it was. The --help probe also catches a daemon
# that predates the subcommand, whose clap error would otherwise surface
# mid-create. Exit 2 = could not run; see _worktree_sparse for why a skip is not
# an option.
# requires-daemon: worktree-sparse >= 0.19.426  #8195 slice 10 - without it --sparse/--full refuse with exit 2 before touching anything; a plain `worktree.sh <N>` is unaffected
if [[ "$SPARSE_MODE" == "true" || "$FULL_MODE" == "true" ]] \
    && { [[ -z "$_WT_DAEMON_BIN" ]] || ! "$_WT_DAEMON_BIN" worktree-sparse --help >/dev/null 2>&1; }; then
    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "sparse-requires-loom-daemon"}' >&3
    else
        print_error "--sparse/--full need 'loom-daemon worktree-sparse' (#8195 slice 10), and ${_WT_DAEMON_BIN:-no resolvable loom-daemon} cannot run it. Install or update loom-daemon, or re-run without --sparse/--full."
    fi
    exit 2
fi

# Check if already in a worktree and automatically handle it. The detection, the
# main-workspace resolution and the first four messages are
# `loom-daemon worktree-check --porcelain` (#8195 slice 11) — see
# `_worktree_locate` above, which has already replayed them by the time it
# returns. What stays here is the `cd` and the two failure arms, because only
# this process can change its own working directory.
_worktree_locate
if [[ "$_WT_IN_WORKTREE" == "true" ]]; then
    if [[ -z "$_WT_MAIN_WORKSPACE" ]]; then
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Failed to find git common directory"}' >&3
        else
            print_error "Failed to find git common directory"
        fi
        exit 1
    fi

    if cd "$_WT_MAIN_WORKSPACE" 2>/dev/null; then
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_success "Switched to main workspace"
        fi
    else
        if [[ "$JSON_OUTPUT" == "true" ]]; then
            echo '{"error": "Failed to change to main workspace", "mainWorkspace": "'"$_WT_MAIN_WORKSPACE"'"}' >&3
        else
            print_error "Failed to change to main workspace: $_WT_MAIN_WORKSPACE"
            print_info "Please manually run: cd $_WT_MAIN_WORKSPACE"
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
#
# $_WT_DAEMON_BIN was resolved once, just after argument parsing (above).
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
    elif [[ "$GIT_IDENTITY_RC" -eq 4 ]]; then
        # Missing identity (#13088): warn, don't hard-fail. Workers spawned via
        # spawn-claude.sh set user.useConfigOnly, so a commit will refuse loudly
        # and name the fix; a hard-fail here would strand read-only uses too.
        print_warning "No explicit git identity — commits from this worktree will be refused (user.useConfigOnly)."
        echo "$GIT_IDENTITY_OUTPUT"
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

# #9106: $DEFAULT_BRANCH reaches `git fetch origin -- "$DEFAULT_BRANCH"` (and
# `origin/$DEFAULT_BRANCH` refs) as a bare operand. It is normally locally
# derived, but LOOM_DEFAULT_BRANCH is an env escape hatch and `ls-remote
# --symref` reads it off the remote — so loom_default_branch validates its own
# result and returns non-zero on an unsafe name, which lands in the arm above
# (check_branch_name has already printed the precise refusal to stderr).

# Fetch origin/$DEFAULT_BRANCH (fetch-only, so a worktree with it checked out is
# not disturbed) and resolve the base ref: origin/$DEFAULT_BRANCH by default, or
# the `--base <branch>` a stacked child builds on (#3729). Hard-fails rather than
# silently un-stacking. See `_worktree_base` above.
BASE_REF="origin/$DEFAULT_BRANCH"
BASE_DISPLAY="$DEFAULT_BRANCH"
_worktree_base || exit $?

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

# --- Lease this claim's liveness (#8193, #10204) -----------------------------
# An in-session builder otherwise publishes no liveness record, and
# `claim_reconciliation` reclaims an actively worked claim (#6286, #7672). Here,
# not in `builder.md`, so the one call site that cannot be forgotten covers it.
# #10204: it is called only once each arm's own operation has SUCCEEDED --
# `_worktree_sparse reconfigure`, `_worktree_existing`, `git worktree add` --
# never before a gate that can still refuse (claim-lock, co-occupancy,
# `forge check-claim`, #8280 branch reuse, #7765/#9083 origin-branch reuse) or
# an add that can still fail, so no nonzero exit before a real worktree exists
# leaves a lease comment and renewer behind. `--watch-pid` is
# `${LOOM_AGENT_SESSION_PID:-${CLAUDE_PID:-$PPID}}` (#10570: the same chain as the
# CLAUDE.md claim recipe), NEVER `$$` (the one-shot tool-call subshell). stderr is
# left attached: `lease ensure` prints exactly one outcome line there, and a
# declined/failed publish must stay visible, not be swallowed (#10570). The
# remaining policy lives in `loom-daemon lease ensure` (ADR-0018; this file's
# `contract` category admits no growth). $_WT_DAEMON_BIN: resolved above.
# `3>&-` (#10203): fd 3 is the caller's saved stdout (`exec 3>&1`); the up-to-4h
# renewer must not inherit it and hold a `worktree.sh N | tail` pipe open.
_wt_lease_claim() { [[ -z "$_WT_DAEMON_BIN" ]] || "$_WT_DAEMON_BIN" lease ensure "$ISSUE_NUMBER" --watch-pid "${LOOM_AGENT_SESSION_PID:-${CLAUDE_PID:-$PPID}}" > /dev/null 3>&- || true; }

# --- Issue claim-lock cross-check (#8553) ------------------------------------
# Reads (never acquires) the daemon's per-issue sweep-claim lock before every
# create/reuse path below. Only exit 1 refuses; any other nonzero (e.g. a daemon
# predating `check-issue`) fails OPEN, matching every other guard here.
# shellcheck disable=SC2086  # $_ijson is intentionally unquoted: omits the flag when empty
# requires-daemon: worktree-lock optional   #8553 fails open on a daemon predating check-issue (no lock cross-check performed)
[[ -z "$_WT_DAEMON_BIN" ]] || { _ijson=""; _irc=0; [[ "$JSON_OUTPUT" == "true" ]] && _ijson="--json"; "$_WT_DAEMON_BIN" worktree-lock check-issue --issue "$ISSUE_NUMBER" --repo "$WORKTREE_REPO_ROOT" $_ijson ${FORCE_CLAIM_LOCK:+--force} >&3 || _irc=$?; [[ "$_irc" -eq 1 ]] && exit 1; }

# Check if worktree already exists
if [[ -d "$WORKTREE_PATH" ]]; then
    # If caller passed --sparse / --full, apply the mode to the existing
    # worktree and exit. This is the idempotent path: same cone is a no-op,
    # different cone replaces the cone, --full disables sparse-checkout. The
    # registration check, the #3548 sentinel back-fill and the final lines /
    # JSON document are all `loom-daemon worktree-sparse --arm reconfigure`
    # (#8195 slice 10); a refusal or failure exits non-zero, which `set -e`
    # propagates with its code intact.
    if [[ "$SPARSE_MODE" == "true" || "$FULL_MODE" == "true" ]]; then
        _worktree_sparse reconfigure "$WORKTREE_PATH" >&3
        _wt_lease_claim; exit 0
    fi

    print_warning "Worktree already exists at: $WORKTREE_PATH"

    # The whole arm from here down — the registration probe, the upstream/drift
    # check, the preserve-vs-reset verdict, the #3548 sentinel back-fill and the
    # stale-worktree reset behind its #6334 rescue guard — is
    # `loom-daemon worktree-existing` (#8195 slice 12). Exit 0 = the worktree is
    # usable (preserved, reset, or reset-refused-and-left-alone, the three
    # outcomes this arm always reported as 0); non-zero = do not hand it over.
    # See `_worktree_existing` above and loom-daemon/src/worktree_cli/existing.rs.
    # rjwalters/kicad-tools#5783: uncommitted changes are the actual
    # co-occupancy hazard -- two same-host sweeps once edited the same
    # uncommitted file here at once, and one's edit leaked into the other's
    # PR. Before `_worktree_existing` hands such a tree back (it always
    # preserves one with uncommitted changes), `loom-daemon lease co-occupancy`
    # refuses (exit 1) when the issue carries 2+ simultaneously fresh sweep
    # leases; WORKTREE_ALLOW_SHARED_LEASE=1 overrides. Only exit 1 refuses -- a
    # read failure, a timeout, or a daemon predating the subcommand (clap exit
    # 2) all fail OPEN, as #8553's check-issue call above does. The `.git`
    # probe keeps `git status` from walking up into the parent checkout.
    # shellcheck disable=SC2086  # $_ljson is intentionally unquoted: omits the flag when empty
    # requires-daemon: lease optional   kicad-tools#5783 fails open on a daemon predating `lease co-occupancy`
    [[ -z "${_WT_DAEMON_BIN:-}" || ! -e "$WORKTREE_PATH/.git" || -z "$(git -C "$WORKTREE_PATH" status --porcelain 2>/dev/null)" ]] || { _ljson=""; _lrc=0; [[ "$JSON_OUTPUT" == "true" ]] && _ljson="--json"; "$_WT_DAEMON_BIN" lease co-occupancy "$ISSUE_NUMBER" --repo "$WORKTREE_REPO_ROOT" $_ljson >&3 || _lrc=$?; [[ "$_lrc" -eq 1 ]] && exit 1; }
    _worktree_existing || exit 1
    # #9111: under --json the port is --quiet, so the success document for the
    # preserve and both stale-reset outcomes is emitted here — the same key set
    # as the --sparse/--full fast path (sparse.rs JSON_TEMPLATE), which already
    # exited, so $SPARSE_MODE/$CONE_JSON are their not-sparse defaults.
    [[ "$JSON_OUTPUT" != "true" ]] || echo '{"success": true, "worktreePath": "'"$(cd "$WORKTREE_PATH" && pwd)"'", "branchName": "'"$BRANCH_NAME"'", "issueNumber": '"$ISSUE_NUMBER"', "sparse": '"$SPARSE_MODE"', "cone": '"$CONE_JSON"'}' >&3
    _wt_lease_claim; exit 0
fi

# Check if branch already exists
if git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
    # The whole arm — the reuse warning, the #6095/#6100 upstream-tracking
    # correction, the #8280 already-landed refusal and the base-ref divergence
    # warning, in that order — is `loom-daemon worktree-branch-reuse` (#8195
    # slice 14). Exit 0 = reuse this branch; a refusal has already printed its
    # message (or written its --json document to fd 3) and exits 1 from inside
    # the wrapper. cwd is the main workspace here, which is what
    # $WORKTREE_REPO_ROOT holds. See `_worktree_branch_reuse` above and
    # loom-daemon/src/worktree_cli/branch_reuse.rs.
    _worktree_branch_reuse

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
        # --- Pre-creation claim probe (#9453 Phase 1; fresh-branch only, #9828) ---
        # Guards fresh-branch creation ONLY: a caller reusing a local or origin
        # feature/issue-<N> branch (above) is continuing that open PR -- a Doctor
        # (#9828) -- so OPEN_PR there is expected, and the reuse resolver already
        # refuses to shadow a real PR. Here we would branch fresh off BASE_REF, so
        # an open linked PR (e.g. on another branch name) means a duplicate build.
        # --force-claim: the caller has already claimed (builder.md), so its own
        # `loom:building`/lease are expected; OPEN_PR, TARGET_IS_PR (#9929, the
        # number is a PR), CLOSED (#11863, nothing left to build) and FROZEN
        # (#11743, `loom:frozen`) are the legs it never overrides. Only exit 0
        # (blocked; reason token to >&3, explanation to stderr) refuses; a read failure, an old daemon, or any other code fails
        # OPEN, like the #8553/#5783 guards. A refused caller adopts the existing
        # PR (create-pr.sh) or stands down -- never a suffix branch.
        # requires-daemon: forge optional   #9453 — `forge check-claim` at worktree creation; without it (absent or pre-#9453 binary) no pre-create OPEN_PR gate is performed and creation proceeds as before
        # shellcheck disable=SC2015  # the trailing `|| true` IS the fail-open path: only exit 0 (blocked) refuses
        [[ -n "$_WT_DAEMON_BIN" ]] && "$_WT_DAEMON_BIN" forge check-claim "$ISSUE_NUMBER" --force-claim >&3 && exit 1 || true
        # Create new branch from the base ref (origin/$DEFAULT_BRANCH by default, or
        # the --base override for a stacked child — #3729).
        [[ "$JSON_OUTPUT" == "true" ]] || print_info "Creating new branch from $BASE_DISPLAY"
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

# Recovery when a feature branch is checked out in the main worktree. This
# happens when a previous builder manually checked out feature/issue-N in the
# main workspace and left it there: git refuses to create a new worktree for
# that branch ("fatal: 'feature/issue-N' is already used by worktree at
# '<main-path>'"), and this decides whether to auto-switch the main workspace
# back to $DEFAULT_BRANCH (clean) or report why it can't (dirty, or the
# conflict is some other worktree entirely).
#
# Ported to `loom-daemon worktree-branch-conflict` (#8195 slice 7): the string
# parsing of git's error text and the path-comparison decision now live in
# `loom-daemon/src/worktree_cli/branch_conflict.rs`, along with the full
# design rationale.
#
# The contract this wrapper preserves, verbatim: every message (including
# `print_error`'s, which — unlike most call sites in this script — are
# themselves gated on `$JSON_OUTPUT`, not just routed), and the three return
# codes `_try_worktree_add` branches on below.
#
# requires-daemon: worktree-branch-conflict optional  #8195 slice 7 — a daemon predating the port never attempts recovery; the caller falls through to recovery_code=1 and reports the raw git error, same as before this guard existed
_worktree_handle_branch_conflict() {
    local error_output="$1"
    local branch="$2"

    # A binary that predates the port is probed for HERE rather than left to
    # fail on the real invocation, for the reason lib/worktree-race-rescue.sh
    # documents at the same guard (#8195 slice 6): clap answers an unknown
    # subcommand with exit 2, and 2 is an ANSWER in this contract — "I switched
    # your main workspace back to $DEFAULT_BRANCH, retry the add". An un-ported
    # daemon would therefore claim a recovery that never happened, on EVERY
    # failing `git worktree add` and not just the branch-conflict one, replacing
    # git's accurate error with a "Retrying worktree creation..." line and a
    # second identical failure. 1 — "not this error, here is git's own text" —
    # is the only truthful answer without a binary that can run the guard. The
    # extra ~0.2s spawn is paid only after `git worktree add` has already
    # failed, never on the success path.
    if [[ -z "${_WT_DAEMON_BIN:-}" ]] \
        || ! "$_WT_DAEMON_BIN" worktree-branch-conflict --help >/dev/null 2>&1; then
        return 1
    fi

    local flags=()
    [[ "$JSON_OUTPUT" == "true" ]] && flags+=(--quiet)
    printf '%s' "$error_output" | "$_WT_DAEMON_BIN" worktree-branch-conflict \
        --branch "$branch" --default-branch "$DEFAULT_BRANCH" \
        --issue "$ISSUE_NUMBER" --repo-root "$WORKTREE_REPO_ROOT" "${flags[@]}"
    return $?
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
    _worktree_handle_branch_conflict "$worktree_error" "$BRANCH_NAME" && recovery_code=0 || recovery_code=$?

    if [[ $recovery_code -eq 2 ]]; then
        # Auto-recovered: retry worktree creation once
        if [[ "$JSON_OUTPUT" != "true" ]]; then
            print_info "Retrying worktree creation..."
        fi
        git worktree add "${CREATE_ARGS[@]}"
        return $?
    fi

    if [[ $recovery_code -eq 1 ]]; then
        # _worktree_handle_branch_conflict returned 1 (not this error type)
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
    # for other issues (issue #6014). release_worktree_lock clears
    # WORKTREE_LOCK_TOKEN itself, which makes the EXIT trap's later call a no-op.
    release_worktree_lock "$ISSUE_NUMBER" "$WORKTREE_LOCK_TOKEN"
    # The worktree now exists, so every refusal gate is behind us (#10204).
    _wt_lease_claim

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
    # exists and helpers see the same file layout as full mode. Under --json
    # it also fills $CONE_JSON for the final document (#8195 slice 10).
    if [[ "$SPARSE_MODE" == "true" ]]; then
        _worktree_sparse create "$ABS_WORKTREE_PATH"
    fi

    # Set git hooks path so .githooks/ works in worktrees (no npx/husky needed).
    # Only when the repo actually ships a .githooks/ dir — otherwise pointing
    # core.hooksPath at a missing dir silently disables all hooks (git treats a
    # nonexistent hooksPath as "no hooks"). $WORKTREE_REPO_ROOT is the main repo
    # root captured at L824 (cwd is the main workspace here, not the worktree).
    if [[ -d "$WORKTREE_REPO_ROOT/.githooks" ]]; then
        git -C "$ABS_WORKTREE_PATH" config core.hooksPath .githooks
    fi

    # --------------------------------------------------------------------
    # Submodule initialization
    # --------------------------------------------------------------------
    #
    # Ported to `loom-daemon worktree-submodules` (#8195 slice 8, epic #7810).
    # The `git submodule status | grep '^-' | awk '{print $2}'` work list, the
    # `--reference` object-sharing decision, the per-submodule deadline and
    # the failure summary now live in
    # `loom-daemon/src/worktree_cli/submodules.rs` with the full design
    # rationale they used to carry inline.
    #
    # This family, and not another arm of the create path, because the
    # retired fifty lines held four defects that never changed a printed
    # line and so survived every fix commit around them: `awk '{print $2}'`
    # truncated a submodule path at its first space and then used the
    # truncation as BOTH a `--reference` directory and a git pathspec
    # (#7858's class); `timeout(1)` is GNU coreutils and a stock macOS does
    # not have it, so on those hosts every iteration failed with `command not
    # found`; `MAIN_GIT_DIR` held git's RELATIVE `.git` answer and was tested
    # from inside the worktree, where `.git` is a FILE, so the object-sharing
    # fast path this block exists for never once fired; and the failure flag
    # was `echo`d into the world-writable, PID-keyed
    # `/tmp/loom-submodule-status-$$`.
    #
    # The contract this call site preserves, verbatim: the message text and
    # ORDER, silence under --json, the child git's stderr left visible so the
    # underlying error is not a black box, and best-effort semantics -- a
    # failed submodule warns and worktree creation still succeeds, which is
    # why the exit code is discarded here and `worktree-submodules` returns 0
    # unconditionally.
    #
    # ONE DELIBERATE BEHAVIOUR CHANGE, argued in the module doc: `--reference`
    # is now actually passed. Nothing observable moves (same lines, same
    # order, same always-0 exit) but the submodules are populated from the
    # main workspace's object store instead of over the network -- which is
    # what this block was written to do and what its success line has always
    # claimed it did.
    #
    # No daemon binary means the submodules are simply not initialized: the
    # worktree is usable and merely missing content a `git submodule update
    # --init --recursive` inside it restores. That is the same honest answer
    # `worktree-link` gives for the same class of best-effort step, and it is
    # not a new failure mode -- the retired block already degraded to exactly
    # this (same warning, same exit 0) on every host lacking `timeout`. So it
    # WARNS rather than exiting 2, and `worktree.sh <issue>` keeps working on
    # a host with no loom-daemon at all.
    #
    # In sparse mode `git submodule status` already lists only submodules
    # whose path lies inside the materialized cone, so out-of-cone submodules
    # are filtered without extra logic -- true of the port as well, which runs
    # the same command in the same directory.
    # requires-daemon: worktree-submodules optional   #8195 slice 8 -- a daemon predating the port skips submodule init with a warning; the worktree is created either way
    MAIN_WORKSPACE_DIR=$(git rev-parse --show-toplevel 2>/dev/null)
    WT_QUIET_FLAGS=()
    [[ "$JSON_OUTPUT" != "true" ]] || WT_QUIET_FLAGS=(--quiet)
    if [[ -n "${_WT_DAEMON_BIN:-}" ]]; then
        "$_WT_DAEMON_BIN" worktree-submodules --repo-root "$MAIN_WORKSPACE_DIR" \
            --worktree "$ABS_WORKTREE_PATH" --timeout "${LOOM_SUBMODULE_TIMEOUT:-300}" \
            "${WT_QUIET_FLAGS[@]}" || true
    elif [[ "$JSON_OUTPUT" != "true" ]]; then
        print_warning "No loom-daemon resolved - skipping submodule initialization (worktree still created)"
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
    # $_WT_DAEMON_BIN is the one resolution made at the top of the create path;
    # it is simply "the daemon that implements this script", honouring
    # $LOOM_DAEMON_SELF_BIN (#8193).
    # requires-daemon: worktree-link optional   #8195 slice 4 — a daemon predating the port skips the symlinks with a warning; the worktree is created either way
    # $MAIN_WORKSPACE_DIR and $WT_QUIET_FLAGS are resolved by the submodule
    # section above, which runs unconditionally and immediately before this.
    if [[ -n "${_WT_DAEMON_BIN:-}" ]]; then
        "$_WT_DAEMON_BIN" worktree-link --repo-root "$MAIN_WORKSPACE_DIR" \
            --worktree "$ABS_WORKTREE_PATH" "${WT_QUIET_FLAGS[@]}" || true
    elif [[ "$JSON_OUTPUT" != "true" ]]; then
        print_warning "No loom-daemon resolved - skipping node_modules/.mcp.json/linkPaths symlinks (worktree still created)"
    fi

    # #8458: give this worktree its own Cargo target dir under the otherwise
    # shared root and record it in the `.loom-cargo-target-dir` marker, so the
    # removal paths (`loom-daemon worktree-remove`, merge-pr.sh, `loom-daemon
    # clean`, the reaper) can attribute and reclaim it. Off unless the repo opts
    # in; a pure no-op on
    # a host whose Cargo output is not redirected outside the worktree.
    #
    # Sets LOOM_WORKTREE_CARGO_TARGET_DIR for the post-worktree hook below —
    # NOT CARGO_TARGET_DIR, which would make the hook's main-workspace binary
    # lookup miss and reintroduce #6013/#6014's rebuild storm.
    #
    # Always `|| true`: the daemon binary may not be built yet (this runs at
    # worktree creation, before the hook that seeds one), and a build-cache
    # optimisation must never fail a worktree creation. Empty stdout means
    # "no directory" — the subcommand exits 0 for every not-applicable case.
    # `--report`'s stderr is deliberately NOT swallowed (stdout is the directory,
    # which `--json` mode needs clean): it is the one operator-visible sign the
    # scheme is on. Exporting an empty value is harmless — every consumer tests
    # `-n` — so no second statement is needed to unset it.
    _pwt_bin="$(loom_locate_daemon_bin "$MAIN_WORKSPACE_DIR" 2>/dev/null || true)"
    [[ -z "${_pwt_bin:-}" ]] || export LOOM_WORKTREE_CARGO_TARGET_DIR="$("$_pwt_bin" cargo-target-dir \
        provision --repo-root "$MAIN_WORKSPACE_DIR" --report "$ABS_WORKTREE_PATH" || true)"

    # Run project-specific post-worktree hook if it exists
    # This allows projects to add custom setup steps (e.g., pnpm install, lake exe cache get)
    # The hook is stored in .loom/hooks/ which is NOT overwritten by Loom upgrades
    # Note: MAIN_WORKSPACE_DIR is already set by the submodule section above
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
        # Machine-readable JSON output. Sparse mode sets "sparse": true and the
        # "cone": [...] worktree-sparse returned; full mode keeps "sparse":
        # false with the empty cone $CONE_JSON is initialized to.
        echo '{"success": true, "worktreePath": "'"$ABS_WORKTREE_PATH"'", "branchName": "'"$BRANCH_NAME"'", "issueNumber": '"$ISSUE_NUMBER"', "sparse": '"$SPARSE_MODE"', "cone": '"$CONE_JSON"'}' >&3
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

    if [[ "$JSON_OUTPUT" == "true" ]]; then
        echo '{"success": false, "error": "Failed to create worktree"}' >&3
    fi
    # Human-readable error already printed by _try_worktree_add / _handle_feature_branch_in_main_worktree
    exit 1
fi
