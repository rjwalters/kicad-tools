#!/usr/bin/env bash
# Loom PR Merge - Worktree-safe merge using forge API (GitHub or Gitea)
# Usage: ./.loom/scripts/merge-pr.sh <pr-number> [options]
#
# Merges a PR via the forge API (not `gh pr merge`) to avoid
# "already used by worktree" errors when merging from inside a worktree.
#
# Supports both GitHub and Gitea forges. Forge detection is automatic
# (see forge-helpers.sh for details).
#
# Options:
#   --no-cleanup-worktree  Skip local worktree AND local branch cleanup after
#                          merge
#   --cleanup-worktree     (no-op, worktree cleanup is now the default)
#   --worktree-path <dir>  Explicit worktree path to clean up (bypasses
#                          .loom-managed sentinel guard — caller asserts
#                          responsibility). Also deletes the matching local
#                          branch via `git branch -d` (refuses on unmerged
#                          commits — Git's own safety check).
#   --dry-run              Show what would happen without merging
#   --auto                 Enable auto-merge instead of immediate merge. On a
#                          repo with GitHub auto-merge disabled
#                          (allow_auto_merge:false) this degrades gracefully to
#                          wait-for-checks-then-merge (immediate if CLEAN)
#                          instead of failing (#3820).
#   --allow-stacked-children
#                          Bypass the pre-merge merge-ordering guard when the
#                          parent branch (feature/issue-N) still has open
#                          stacked child PRs targeting it (operator asserts the
#                          children are already reconciled). See #3747 item 2.
#   --no-cleanup-primary   Skip the automatic primary-checkout branch cleanup
#                          (#5015): when the merged branch is checked out in
#                          the PRIMARY repo checkout (not a worktree) and it
#                          is provably safe (clean tree, no stash entries,
#                          tip matches the merged PR head SHA), the script
#                          normally checks out the default branch there and
#                          deletes the merged branch automatically instead of
#                          just printing manual instructions. Pass this to
#                          always print the manual instructions instead.
#   --cleanup-primary      (no-op, primary-checkout cleanup is the default)
#
# By default, the local worktree AND the local branch it held are cleaned up
# after a successful merge (#4100). Pass --no-cleanup-worktree to skip both
# (e.g., when other terminals may have their CWD inside the worktree, or the
# branch has unpushed commits you want to keep).
#
# Cleanup is restricted to Loom-managed worktrees (those containing the
# .loom-managed sentinel written by worktree.sh). Worktrees lacking the
# sentinel are treated as user-owned and never removed. Set
# LOOM_PRESERVE_WORKTREE=1 to disable cleanup unconditionally for a session.
#
# Local branch deletion (#4100): every cleanup path — the default
# .loom/worktrees/issue-N convention, a discovered Loom-managed worktree at a
# non-standard path, and even the case where no worktree exists at all —
# attempts to delete the merged PR's local branch. Safety is determined by
# whether the local branch tip equals the merged PR's head SHA (not by `git
# branch --merged`, which is always false for a squash merge): a matching tip
# uses `git branch -D` (every commit on the branch was part of the merged PR);
# a non-matching tip (unpushed local work) falls back to `git branch -d`,
# which keeps the branch and reports it instead of force-deleting. A branch
# checked out as the current HEAD (or in another worktree) is never deleted —
# git itself refuses, and the refusal is reported with a specific message
# rather than the generic "unmerged commits" warning. The repo's default
# branch is never a delete target.
#
# Primary-checkout auto-cleanup (#5015): the one case above that is "checked
# out as the current HEAD" AND is specifically the repo's PRIMARY checkout
# (not a linked worktree) gets one extra step: if the tip-matches-head safety
# check already passed AND the primary checkout's working tree is clean with
# no stash entries (re-checked immediately before the mutating step, not
# cached), the script checks out the default branch there and force-deletes
# the branch automatically. A dirty tree, a stash entry, a tip mismatch, or
# --no-cleanup-primary all fall back to printing the manual two-step
# instructions unchanged.
#
# Override: pass --worktree-path <dir> to opt into removing a non-Loom
# worktree (the sentinel guard is bypassed only when this flag is supplied).
# Discovery: if neither the default issue-N nor pr-N worktree exists, the
# script walks `git worktree list --porcelain` looking for a worktree whose
# branch matches the merged PR's head branch. It emits a hint (not an
# auto-remove) so the operator can re-run with --worktree-path.
#
# Exit codes:
#   0 = merged (or auto-merge enabled)
#   1 = failed
#   3 = PR head moved past the SHA this merge attempt gated on (#5579) — a
#       session pushed new commits to the branch after the approving review
#       (or after this run's own head-SHA read). NOT a merge failure: the PR
#       is still Judge-approved, its diff just changed underneath it. Callers
#       (notably champion-pr-merge.md Step 3) must treat this distinctly from
#       exit 1 — re-queue the PR for a fresh pass rather than posting a
#       failure comment. See "Squash-merge detection trap" in that file's
#       Error Handling section for why ancestry checks can't verify this
#       state after the fact.

set -euo pipefail

# ANSI color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

error() { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }
info() { echo -e "${BLUE}$*${NC}"; }
success() { echo -e "${GREEN}$*${NC}"; }
warning() { echo -e "${YELLOW}$*${NC}"; }
# #5579: distinct from error() (exit 1) — see "Exit codes" above. Emits to
# stderr like error() so it is visible in logs, but exits 3 so the caller can
# tell "re-queue" from "genuinely failed" without parsing message text.
# Parameters: $1 = error message (may include forge API text), $2 = stale SHA (optional),
# $3 = current head SHA (optional). If both SHAs provided, includes them in output.
error_head_moved() {
  local msg="$1" stale_sha="${2:-}" current_sha="${3:-}"
  if [[ -n "$stale_sha" && -n "$current_sha" ]]; then
    echo -e "${YELLOW}PR head moved during merge attempt (stale approval, not a failure):${NC}" >&2
    echo -e "${YELLOW}  Merge gated on (stale):    $stale_sha${NC}" >&2
    echo -e "${YELLOW}  Current head SHA:         $current_sha${NC}" >&2
    echo -e "${YELLOW}  Details: $msg${NC}" >&2
  else
    echo -e "${YELLOW}PR head moved during merge attempt (stale approval, not a failure): $msg${NC}" >&2
  fi
  exit 3
}

# #5579: detect a head-SHA-mismatch response from either forge's merge API.
# Distinct from the existing "Base branch was modified" matcher below (that
# one means the PR's BASE fell behind and a rebase-and-retry is correct;
# this one means the PR's OWN head moved, so retrying would either fail again
# or silently merge a different diff than the one that was approved). String
# provenance is documented on forge_merge_pr / forge_auto_merge in
# lib/forge-helpers.sh — GitHub REST and Gitea are verified against each
# forge's own source/spec; the GitHub GraphQL (auto-merge) string is
# best-effort pending a live-incident confirmation.
_is_head_mismatch_response() {
  echo "$1" | grep -Eiq 'Head branch was modified\.|head out of date|expectedHeadOid'
}

# Function to show help
show_help() {
    cat << EOF
Loom PR Merge - Worktree-safe merge using forge API (GitHub or Gitea)

Usage: ./.loom/scripts/merge-pr.sh <pr-number> [options]

Merges a PR via the forge API (not 'gh pr merge') to avoid
"already used by worktree" errors when merging from inside a worktree.

Supports both GitHub and Gitea forges. Forge detection is automatic
(see forge-helpers.sh for details).

Options:
  --no-cleanup-worktree  Skip local worktree AND local branch cleanup
                         after merge
  --cleanup-worktree     (no-op, worktree cleanup is now the default)
  --worktree-path <dir>  Explicit worktree path to clean up. Bypasses the
                         .loom-managed sentinel guard (caller asserts
                         responsibility — this is the documented opt-in
                         for removing non-Loom worktrees). Also deletes
                         the matching local branch via 'git branch -d'
                         (Git refuses on unmerged commits).
  --dry-run              Show what would happen without merging
  --auto                 Enable auto-merge instead of immediate merge. When the
                         repository has GitHub auto-merge disabled
                         (allow_auto_merge:false), this is detected up front and
                         degrades gracefully to wait-for-checks-then-merge
                         (immediate if already CLEAN) rather than failing (#3820).
  --allow-stacked-children
                         Bypass the pre-merge merge-ordering guard. That guard
                         hard-blocks merging a stacked PARENT PR (branch
                         feature/issue-N) while it still has open stacked CHILD
                         PRs targeting its branch, because the repo's
                         delete_branch_on_merge setting would delete the parent
                         branch synchronously during the merge and leave the
                         children unable to rebase onto it (see #3747 item 2).
                         Pass this flag only after you have manually reconciled
                         (or verified) the children — the operator asserts
                         responsibility, mirroring --worktree-path.
  --no-cleanup-primary   Skip automatic primary-checkout branch cleanup (#5015).
                         When the merged branch is checked out in the PRIMARY
                         repo checkout (not a worktree), the script normally
                         auto-checks-out the default branch and force-deletes
                         it there ONLY when provably safe (clean tree, no
                         stash entries, tip matches the merged PR head SHA).
                         Pass this to always print manual instructions instead.
  --cleanup-primary      (no-op, primary-checkout cleanup is the default)
  -h, --help             Show this help and exit

By default, the local worktree AND the local branch it held are cleaned up
after a successful merge (#4100). Pass --no-cleanup-worktree to skip both
(e.g., when other terminals may have their CWD inside the worktree, or you
want to keep a branch with unpushed commits).

Cleanup is restricted to Loom-managed worktrees (those under
.loom/worktrees/issue-N that contain a .loom-managed sentinel file written
by worktree.sh). User-provisioned worktrees at other paths are never
removed by the default code path. Set LOOM_PRESERVE_WORKTREE=1 to disable
cleanup unconditionally for a session.

Local branch deletion (#4100): every cleanup path — including the case
where no worktree exists at all — attempts to delete the merged PR's local
branch. Safety is determined by comparing the local branch tip to the
merged PR's head SHA (not 'git branch --merged', which is always false for
a squash merge): a matching tip uses 'git branch -D'; a non-matching tip
(unpushed local work) falls back to 'git branch -d', which keeps the
branch and reports it instead of force-deleting. The branch currently
checked out (main worktree or any other) is never deleted, and the repo's
default branch is never a delete target.

Primary-checkout auto-cleanup (#5015): the one exception to "checked out
branches are never deleted" is when the branch is checked out in the repo's
PRIMARY checkout specifically (not a linked worktree) AND it is provably
safe — the tip-matches-head safety check above passed, the primary
checkout's working tree is clean, and it has no stash entries. In that case
the script checks out the default branch there and force-deletes the merged
branch automatically instead of just printing instructions. Pass
--no-cleanup-primary to always print the manual instructions instead.

When --worktree-path <dir> is passed explicitly, the operator is taking
responsibility for the cleanup decision: the sentinel guard is bypassed
for that one path. The path is validated against 'git worktree list'
and rejected if it is not a worktree of this repository.

Discovery fallback: if neither .loom/worktrees/issue-N/ nor
.loom/worktrees/pr-<PR_NUMBER>/ exists, the script walks
'git worktree list --porcelain' looking for a worktree whose branch
matches the merged PR head branch. It NEVER auto-removes a discovered
user-owned worktree; it only logs the path and suggests re-running with
--worktree-path <found-path>.

Precedence (highest wins):
  1. LOOM_PRESERVE_WORKTREE=1     (always skip cleanup)
  2. --no-cleanup-worktree        (always skip cleanup; warns if combined
                                  with --worktree-path)
  3. --worktree-path <dir>        (explicit path; bypasses sentinel)
  4. default: .loom/worktrees/issue-N or pr-N + sentinel guard

Exit codes:
  0 = merged (or auto-merge enabled, or --help)
  1 = failed

Examples:
  ./.loom/scripts/merge-pr.sh 123
    Merges PR #123 (squash), deletes remote branch, cleans up worktree

  ./.loom/scripts/merge-pr.sh 123 --dry-run
    Shows what would happen without merging

  ./.loom/scripts/merge-pr.sh 123 --auto
    Enables auto-merge instead of merging immediately (on a repo with
    auto-merge disabled, waits for checks then merges synchronously)

  ./.loom/scripts/merge-pr.sh 123 --no-cleanup-worktree
    Merges PR but leaves the local worktree in place

  ./.loom/scripts/merge-pr.sh 123 --worktree-path ../adhoc-wt
    Merges PR #123 and removes the worktree at ../adhoc-wt plus its
    matching local branch (bypasses the .loom-managed sentinel guard).

  ./.loom/scripts/merge-pr.sh 123 --no-cleanup-primary
    Merges PR but always prints manual instructions instead of
    auto-cleaning a branch checked out in the primary repo checkout.
EOF
}

# Early help check — runs before any git/forge initialization so --help works
# in any directory and without forge authentication.
if [[ $# -gt 0 ]] && { [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; }; then
    show_help
    exit 0
fi

# Find the main repository root (works from worktrees too)
# When run from a worktree, git rev-parse --show-toplevel returns the worktree path,
# not the main repository. This function navigates via the gitdir to find the actual root.
find_main_repo_root() {
  local dir
  dir="$(git rev-parse --show-toplevel 2>/dev/null)" || return 1

  # Check if this is a worktree (has .git file, not directory)
  if [[ -f "$dir/.git" ]]; then
    local gitdir
    gitdir=$(cat "$dir/.git" | sed 's/^gitdir: //')
    # gitdir is like /path/to/repo/.git/worktrees/issue-123
    # main repo is 3 levels up from there
    local main_repo
    main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
    if [[ -d "$main_repo/.loom" ]]; then
      echo "$main_repo"
      return 0
    fi
  fi

  # Not a worktree or fallback - return the git root
  echo "$dir"
}

REPO_ROOT="$(find_main_repo_root)" || \
  error "Not in a git repository"

# Source forge helpers for multi-forge support
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/forge-helpers.sh"
# Shared worktree-root resolver (#3530) — cleanup must discover worktrees at an
# overridden root, not just the default .loom/worktrees.
# shellcheck source=lib/worktree-root.sh
source "$SCRIPT_DIR/lib/worktree-root.sh"
# Worktree-removal ledger (#5950) — post-merge cleanup is one of several
# independent removers; every one of them records to the same file so
# "what removed this worktree?" has a single answer. Sourced defensively with a
# no-op fallback: the ledger is diagnostic only and must never be able to break
# a merge on a partially-resynced .loom/.
if [[ -f "$SCRIPT_DIR/lib/worktree-removal-log.sh" ]]; then
  # shellcheck source=lib/worktree-removal-log.sh
  source "$SCRIPT_DIR/lib/worktree-removal-log.sh"
else
  loom_record_worktree_removal() { :; }
fi
# Default-branch resolver (#4100) — the local-branch delete guard must never
# target the repo's default branch. Sourced defensively: a repo where this
# fails to resolve (e.g. no network + no origin/HEAD symref) still falls back
# to the literal "main"/"master" check in _maybe_delete_local_branch below.
DEFAULT_BRANCH_NAME=""
if [[ -f "$SCRIPT_DIR/lib/default-branch.sh" ]]; then
  # shellcheck source=lib/default-branch.sh
  source "$SCRIPT_DIR/lib/default-branch.sh"
  DEFAULT_BRANCH_NAME="$(cd "$REPO_ROOT" && loom_default_branch 2>/dev/null || true)"
fi
forge_detect

# Use gh-cached for read-only queries to reduce API calls (see issue #1609)
# Verify the Python interpreter works too — a broken runtime (e.g. unaccepted
# Xcode license) would make every subsequent gh call fail with a misleading error.
GH_CACHED="$REPO_ROOT/.loom/scripts/gh-cached"
if [[ "$FORGE_TYPE" == "github" ]] && [[ -x "$GH_CACHED" ]] && "$GH_CACHED" --version &>/dev/null; then
    GH="$GH_CACHED"
else
    GH="gh"
fi

REPO_NWO="$(forge_get_repo_nwo "$GH")" || \
  error "Could not determine repository. Is 'gh' authenticated?"

# Parse arguments
PR_NUMBER=""
CLEANUP_WORKTREE=true
# CLEANUP_PRIMARY_CHECKOUT (#5015): gates the automatic primary-checkout
# branch cleanup performed by _maybe_delete_local_branch. Defaults on
# (mirrors CLEANUP_WORKTREE's default); --no-cleanup-primary opts out.
CLEANUP_PRIMARY_CHECKOUT=true
DRY_RUN=false
AUTO_MERGE=false
WORKTREE_PATH_OVERRIDE=""
ALLOW_STACKED_CHILDREN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cleanup-worktree) shift ;;  # no-op, cleanup is now the default
    --no-cleanup-worktree) CLEANUP_WORKTREE=false; shift ;;
    --cleanup-primary) shift ;;  # no-op, primary-checkout cleanup is now the default
    --no-cleanup-primary) CLEANUP_PRIMARY_CHECKOUT=false; shift ;;
    --worktree-path)
      [[ $# -lt 2 ]] && error "--worktree-path requires a value"
      WORKTREE_PATH_OVERRIDE="$2"
      shift 2
      ;;
    --worktree-path=*)
      WORKTREE_PATH_OVERRIDE="${1#--worktree-path=}"
      [[ -z "$WORKTREE_PATH_OVERRIDE" ]] && error "--worktree-path= requires a value"
      shift
      ;;
    --dry-run) DRY_RUN=true; shift ;;
    --auto) AUTO_MERGE=true; shift ;;
    --allow-stacked-children) ALLOW_STACKED_CHILDREN=true; shift ;;
    -*)  error "Unknown option: $1" ;;
    *)
      if [[ -z "$PR_NUMBER" ]]; then
        PR_NUMBER="$1"
      else
        error "Unexpected argument: $1"
      fi
      shift
      ;;
  esac
done

[[ -z "$PR_NUMBER" ]] && error "Usage: merge-pr.sh <pr-number> [--no-cleanup-worktree] [--no-cleanup-primary] [--worktree-path <dir>] [--dry-run] [--auto] [--allow-stacked-children]"
[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || error "PR number must be numeric: $PR_NUMBER"

# Validate --worktree-path early (before any network calls) so bad input
# fails fast. The path must be a real directory and must appear in the
# repository's worktree list. We resolve to an absolute path via cd so
# downstream comparisons against the porcelain output work cleanly.
if [[ -n "$WORKTREE_PATH_OVERRIDE" ]]; then
  if [[ ! -d "$WORKTREE_PATH_OVERRIDE" ]]; then
    error "--worktree-path does not exist or is not a directory: $WORKTREE_PATH_OVERRIDE"
  fi
  _WT_ABS="$(cd "$WORKTREE_PATH_OVERRIDE" 2>/dev/null && pwd -P)" || \
    error "--worktree-path could not be resolved: $WORKTREE_PATH_OVERRIDE"
  # Verify the path is actually a worktree of this repo. Each porcelain stanza
  # begins with a literal `worktree ` prefix (9 chars) followed by the
  # unquoted, unescaped absolute path — which may contain spaces. Parse the
  # path with substr($0, 10), NOT $2/whitespace-split (which truncates at the
  # first space). Caveat: a path containing a literal newline would still break
  # this line-oriented parse; `--porcelain -z` (NUL-delimited) would be needed
  # for full robustness, but spaces are the realistic failure mode (#3717).
  if ! git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | \
       awk -v p="$_WT_ABS" '/^worktree / { if (substr($0, 10) == p) { found=1; exit } } END { exit !found }'; then
    error "--worktree-path is not a registered worktree of this repository: $WORKTREE_PATH_OVERRIDE (resolved: $_WT_ABS)"
  fi
  WORKTREE_PATH_OVERRIDE="$_WT_ABS"
  unset _WT_ABS

  # Warn if combined with --no-cleanup-worktree (no-op wins).
  if [[ "$CLEANUP_WORKTREE" == "false" ]]; then
    warning "--worktree-path was supplied but --no-cleanup-worktree wins; no cleanup will occur"
  fi
fi

# Fetch PR state
PR_JSON=$(forge_get_pr "$REPO_NWO" "$PR_NUMBER" "$GH") || \
  error "Could not fetch PR #$PR_NUMBER"

PR_STATE=$(echo "$PR_JSON" | jq -r '.state')
PR_MERGED=$(echo "$PR_JSON" | jq -r '.merged')
PR_BRANCH=$(echo "$PR_JSON" | jq -r '.head.ref')
PR_TITLE=$(echo "$PR_JSON" | jq -r '.title')
PR_MERGEABLE=$(echo "$PR_JSON" | jq -r '.mergeable')
# Head SHA (#4100): the safety criterion for local-branch deletion. A local
# branch whose tip equals this SHA carries no commits absent from the merged
# PR, so it is safe to force-delete even though it will never satisfy
# `git branch --merged` after a squash merge.
PR_HEAD_SHA=$(echo "$PR_JSON" | jq -r '.head.sha // empty')

# Check if already merged
if [[ "$PR_MERGED" == "true" ]]; then
  warning "PR #$PR_NUMBER is already merged"
  exit 0
fi

# Check if closed (not merged)
if [[ "$PR_STATE" == "closed" ]]; then
  error "PR #$PR_NUMBER is closed (not merged)"
fi

# ---------------------------------------------------------------------------
# Pre-merge merge-ordering guard (#3747, stacked-PR v2 item 2).
#
# Runs BEFORE both the auto-merge and synchronous-merge paths (that is why it is
# defined and invoked here, above the "Merging PR" line — not next to item 1's
# POST-merge _auto_reconcile_stacked_children at the bottom of the merge flow).
#
# The race it closes: when a stacked PARENT PR (branch feature/issue-<N>)
# squash-merges, item 1's post-merge _auto_reconcile_stacked_children rebases any
# open CHILD PRs off the now-squashed parent branch onto the default branch. That
# rebase (reconcile-stack.sh's `git rebase --onto <default> <parent-branch>
# <child-branch>`) needs <parent-branch> to still resolve as a ref. But Loom's own
# recommended repo setting — delete_branch_on_merge:true, applied by
# setup-repository-settings.sh — makes GitHub delete feature/issue-<parent>
# SYNCHRONOUSLY as part of the merge API call itself, before merge-pr.sh even
# reaches the "merged successfully" log line, let alone the post-merge reconcile
# step. Once the ref is gone a fresh fetch won't see it and the rebase's <upstream>
# fails to resolve. Item 1's post-merge pass can therefore race and LOSE against
# the repo's own settings. This guard refuses to let the parent merge happen at
# all while that race exists.
#
# This is orthogonal to item 1's loom:building safe/unsafe split: a "safe" child
# is just as exposed to branch deletion as an "unsafe" one, so the guard keys
# PURELY on "does an open child PR still target this branch", never on the child's
# label. Discovery reuses item 1's live-forge-query shape (`gh pr list --base
# <parent> --state open`), NOT the ephemeral daemon registry.
#
# Default is a hard block (error, exit 1) — a normal, recoverable failure that
# Champion's cron retries next tick, exactly like every other merge-blocking
# condition in this file. --allow-stacked-children bypasses it (operator asserts
# the children are reconciled). --dry-run still runs the guard and REPORTS the
# would-be block, but honors the dry-run contract (never exits 1).
_check_no_open_stacked_children() {
  # Only GitHub, and only a parent PR on a feature/issue-<N> branch, can have
  # stacked children — identical guard conditions to
  # _auto_reconcile_stacked_children. No-op (byte-for-byte unchanged behavior)
  # otherwise.
  [[ "$FORGE_TYPE" == "github" ]] || return 0
  [[ "$PR_BRANCH" =~ ^feature/issue-([0-9]+)$ ]] || return 0

  # Live forge discovery — NEVER the daemon registry. Same call/shape item 1
  # already makes; plain `gh` (uncached) so we see child PRs as of right now.
  local children_json count child_list
  children_json="$(gh pr list --repo "$REPO_NWO" --base "$PR_BRANCH" --state open \
    --json number,headRefName 2>/dev/null || echo '[]')"
  [[ -n "$children_json" ]] || return 0
  count="$(echo "$children_json" | jq 'length' 2>/dev/null || echo 0)"
  [[ "$count" -gt 0 ]] || return 0

  # Comma-separated "#N" list for the operator-facing message.
  child_list="$(echo "$children_json" \
    | jq -r '[.[].number | "#" + tostring] | join(", ")' 2>/dev/null || echo '')"

  # Operator opt-in bypass (mirrors the --worktree-path sentinel-bypass precedent):
  # the operator asserts responsibility for having reconciled/verified the children.
  if [[ "$ALLOW_STACKED_CHILDREN" == "true" ]]; then
    warning "Merge-ordering guard: --allow-stacked-children set; proceeding despite $count open stacked child PR(s) ($child_list) targeting '$PR_BRANCH' (operator asserts they are reconciled)"
    return 0
  fi

  local msg
  msg="Merge blocked: PR #$PR_NUMBER's branch '$PR_BRANCH' still has $count open stacked child PR(s) ($child_list) targeting it.

Merging now would race the repo's delete_branch_on_merge setting: GitHub deletes '$PR_BRANCH' synchronously during the merge, before the child PR(s) can be rebased/retargeted onto the default branch — leaving reconcile-stack.sh's rebase unable to resolve the parent branch ref (#3747 item 2).

Reconcile each child first (from a clean checkout), then re-run this merge:
  ./.loom/scripts/reconcile-stack.sh <child-pr> $PR_BRANCH

Or, if you have already verified/reconciled them, re-run with --allow-stacked-children to bypass this guard."

  # --dry-run still runs the guard and reports the would-be outcome, but honors
  # the dry-run contract (dry-run always exits 0). A real run hard-blocks.
  if [[ "$DRY_RUN" == "true" ]]; then
    warning "[dry-run] Would BLOCK merge of PR #$PR_NUMBER: $count open stacked child PR(s) ($child_list) still target '$PR_BRANCH'. Re-run with --allow-stacked-children to override, or reconcile the children first."
    return 0
  fi

  error "$msg"
}

# Invoke the guard before either merge path attempts the actual merge API call.
_check_no_open_stacked_children

# ---------------------------------------------------------------------------
# Partial-increment closing-keyword conflict detection (#4569, extended by
# #4595 to cover commit messages).
#
# ROOT CAUSE (established from the rjwalters/censusapi#5 -> censusapi#2 incident,
# NOT from the branch-name theory the report floated):
#
#   GitHub's closing-reference parser scans the ENTIRE PR body and honors ANY
#   `close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved` that is
#   IMMEDIATELY followed by `#N` — wherever it appears, including buried in
#   prose, inside a list item, or mid-sentence. It is NOT limited to a
#   line-leading trailer.
#
#   censusapi PR #5 ended with the deliberate non-closing trailer
#   `Contributes to #2`, but an earlier "Operator follow-up (after merge)" step
#   read "...then close #2". GitHub honored that `close #2` as a real closing
#   reference and closed issue #2 on squash-merge, silently defeating the #3599
#   partial-increment convention. The evidence:
#     - issue #2's timeline has NO `connected` event, so there was no
#       Development-sidebar / branch-name link -> the `feature/issue-N`
#       branch-name auto-link hypothesis is RULED OUT;
#     - the squash commit message contained only `Contributes to #2` (no closing
#       keyword), so the close did not come from the commit message either;
#     - the `closed` event has `commit_id: null` with the merger as actor at the
#       merge instant — the signature of a PR-body closing-reference close.
#
# SECOND SOURCE (#4595): the same parser also runs over the SQUASH COMMIT
#   MESSAGE. forge_merge_pr() squash-merges with no commit_title/commit_message
#   override, so GitHub composes that message from the PR's own commit messages —
#   a stray `close #N` in any commit message closes #N on merge even when the PR
#   body only ever says `Part of #N`. That close is fully attributable (the
#   `closed` event carries the merge `commit_id`), it is just invisible to both
#   the body regex and `closingIssuesReferences`, so the commit messages are
#   consulted as a third signal below.
#
# Fix shape: DETECT (here, pre-merge, with a loud actionable warning) plus
# SELF-HEAL (post-merge reopen in _reset_one_partial_issue below). Prevention by
# rewriting the PR body at merge time was rejected — mutating a body the Judge
# already reviewed is a worse failure mode than a seconds-long close/reopen.
#
# Two globals are published for the post-merge pass, both space-separated:
#   PARTIAL_OPEN_BEFORE_MERGE - partial-increment refs that were OPEN right
#       before the merge (so "closed afterwards" is attributable to this merge).
#   PARTIAL_CONFLICT_ISSUES   - the subset that ALSO carries a closing reference
#       from this PR, i.e. the ones GitHub is about to close against the
#       declared intent. Only these are auto-reopened; that keeps a deliberate
#       human close inside the merge window (which carries no closing reference)
#       from being reverted.
PARTIAL_OPEN_BEFORE_MERGE=""
PARTIAL_CONFLICT_ISSUES=""

# Strips Markdown fenced code blocks (``` ... ```) from stdin, one line at a
# time: a fence-open/-close toggle drops everything between the fences
# (inclusive of the fence markers themselves). Used so a `Part of #N` shown as
# example/quoted text inside a fenced block reads as documentation, never a
# live declaration (#5234).
_strip_fenced_code_blocks() {
  awk '
    /^[[:space:]]*```/ { infence = !infence; next }
    !infence { print }
  '
}

# Issue numbers referenced with a NON-closing partial-increment keyword
# (`Part of #N` / `Contributes to #N`, case-insensitive) as a DECLARATION, one
# per line, deduped.
#
# "Declaration" is deliberately narrower than "appears anywhere in the body"
# (#5234): a bare `grep` over the whole text also matched a mid-sentence,
# backticked, conditional mention like "...say so and I will switch the
# reference to `Part of #4574`" — prose describing a hypothetical, not a
# declared intent — and treated it as authoritative, reopening a correctly
# closed issue. To count as a declaration here the keyword must be
# line-leading, optionally preceded by a list marker (`-`/`*`/`+`/numbered
# `1.`/blockquote `>`) and/or whitespace — matching the actual shape this
# convention produces (a one-line `Part of #123` / `Contributes to #456`
# trailer, per builder-pr.md), not prose that merely references the pattern.
#
# Fenced code blocks are stripped first, then inline code spans (`` `...` ``)
# are blanked so a backticked mention cannot itself satisfy the line-leading
# anchor (the #5234 repro used backticks specifically to mark the reference as
# hypothetical, not live).
#
# The second stage extracts `#N` tokens FIRST and only then strips the `#`.
# Scanning the whole matched span for any digit run would also pick up the
# numbered-list marker's own ordinal (`3. Part of #789` -> `3` and `789`),
# which is exactly the false-positive-reopen class this guard exists to
# prevent: a body carrying both `3. Part of #789` and a genuine `Closes #3`
# would see #3 registered as a declared partial increment AND a closing
# reference, and get reopened right after a correct close.
_partial_increment_refs() {
  { printf '%s\n' "$1" \
      | _strip_fenced_code_blocks \
      | sed -E 's/`[^`]*`//g' \
      | grep -oiE '^[[:space:]]*([-*+>]|[0-9]+\.)?[[:space:]]*(Part of|Contributes to)[[:space:]]+#[0-9]+' \
      | grep -oE '#[0-9]+' \
      | tr -d '#' \
      | sort -un; } || true
}

# Issue numbers referenced with a GitHub CLOSING keyword anywhere in the text
# read from stdin, one per line, deduped. The regex is deliberately the same one
# forge_pr_close_targets() uses on its Gitea branch (the canonical keyword set,
# with `\b` guarding against substring traps like `Discloses #N`).
#
# Why a text regex and not only the authoritative GraphQL
# `closingIssuesReferences`: that field is GraphQL-only, and the incident this
# guard exists for happened while GraphQL quota was exhausted (the PR was even
# created via raw REST for that reason). A quota-free text signal is the one that
# still works in exactly the conditions where this bug bites.
_closing_refs_stdin() {
  { grep -oiE '\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[[:space:]]+#[0-9]+' \
      | grep -oE '[0-9]+' \
      | sort -un; } || true
}

# Same, for text passed as $1 (the PR body, historically the only source).
_body_closing_refs() {
  { printf '%s\n' "$1" | _closing_refs_stdin; } || true
}

# The literal offending snippets ("close #N", "Fixes #N", …) that reference
# issue $2 with a closing keyword inside the text $1, rendered for a warning as
# `snippet", "snippet`. Empty when the text carries no such reference — which is
# how the caller tells WHICH source (body vs. commit messages) is at fault.
_closing_ref_snippets() {
  { printf '%s\n' "$1" \
      | grep -oiE "\\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\\b[[:space:]]+#$2\\b" \
      | sort -u | tr '\n' '|' | sed 's/|$//; s/|/", "/g'; } || true
}

# The literal declaration text (`Part of #N` / `Contributes to #N`) that
# _partial_increment_refs read as authoritative for issue $2 inside text $1,
# rendered the same way _closing_ref_snippets renders the closing-keyword side
# (`snippet", "snippet`) — so the pre-merge warning can show an operator BOTH
# sides of the conflict and judge for themselves whether the declaration was
# real (AC #4, #5234). Runs the identical fenced-block/inline-code-span
# stripping as _partial_increment_refs so the quoted snippet always matches
# what was actually matched, never a code-block artifact.
_partial_increment_ref_snippets() {
  { printf '%s\n' "$1" \
      | _strip_fenced_code_blocks \
      | sed -E 's/`[^`]*`//g' \
      | grep -oiE "^[[:space:]]*([-*+>]|[0-9]+\\.)?[[:space:]]*(Part of|Contributes to)[[:space:]]+#$2\\b" \
      | sed -E 's/^[[:space:]]+//' \
      | sort -u | tr '\n' '|' | sed 's/|$//; s/|/", "/g'; } || true
}

# Every commit message of this PR, concatenated (#4595). merge-pr.sh squash-
# merges without overriding the commit message (forge_merge_pr passes no
# commit_title/commit_message), so GitHub composes the squash message from these
# commits — a `close #N` in any of them is a real closing reference that neither
# the PR body regex nor `closingIssuesReferences` reveals.
#
# REST (not GraphQL), so it survives the same quota exhaustion the body regex
# exists for, and `--paginate` so a >30-commit PR is not silently truncated.
# Plain `gh api` (not $GH) for freshness, `--jq` deliberately avoided in favor of
# a jq pipe (jq is already a hard dependency). Best-effort: any failure yields an
# empty string, which degrades to the pre-#4595 behavior (no attribution).
_pr_commit_messages() {
  { gh api "repos/$REPO_NWO/pulls/$PR_NUMBER/commits" --paginate 2>/dev/null \
      | jq -r '.[].commit.message' 2>/dev/null; } || true
}

# Membership tests over the space-separated globals above.
_partial_ref_is_conflicted() {
  [[ -n "${PARTIAL_CONFLICT_ISSUES:-}" ]] || return 1
  [[ " $PARTIAL_CONFLICT_ISSUES " == *" $1 "* ]]
}
_partial_ref_was_open_before_merge() {
  [[ -n "${PARTIAL_OPEN_BEFORE_MERGE:-}" ]] || return 1
  [[ " $PARTIAL_OPEN_BEFORE_MERGE " == *" $1 "* ]]
}

# Populate the two globals and warn about each detected conflict. Best-effort:
# never fails the merge, and a lookup failure simply yields a smaller set.
_check_partial_increment_close_conflict() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0

  local pr_body
  pr_body="$(echo "$PR_JSON" | jq -r '.body // ""')"
  [[ -n "$pr_body" ]] || return 0

  local partial_refs
  partial_refs="$(_partial_increment_refs "$pr_body")"
  [[ -n "$partial_refs" ]] || return 0

  # Closing references GitHub will honor on merge, from three unioned signals:
  #   1. the body's own closing keywords (quota-free regex);
  #   2. this PR's COMMIT MESSAGES (#4595) — quota-free REST, and the source of
  #      the squash commit message this script does not override;
  #   3. GitHub's authoritative closingIssuesReferences (best-effort — empty
  #      under GraphQL quota exhaustion, but when it does answer it also
  #      surfaces a Development-sidebar link that no text reveals).
  # The commit fetch happens only past the partial_refs early-return above, so
  # the common (non-partial-increment) path costs zero extra API calls.
  local body_close_refs commit_messages commit_close_refs graphql_close_refs close_refs
  body_close_refs="$(_body_closing_refs "$pr_body")"
  commit_messages="$(_pr_commit_messages)"
  commit_close_refs="$(printf '%s\n' "$commit_messages" | _closing_refs_stdin)"
  graphql_close_refs="$(forge_pr_close_targets "$PR_NUMBER" "$GH" 2>/dev/null || true)"
  close_refs="$(printf '%s\n%s\n%s\n' "$body_close_refs" "$commit_close_refs" "$graphql_close_refs" \
    | grep -E '^[0-9]+$' | sort -un || true)"

  local issue_num issue_json
  while IFS= read -r issue_num; do
    [[ -n "$issue_num" ]] || continue

    # Fresh (uncached) read — plain `gh api`, not $GH, mirroring
    # _reset_one_partial_issue's freshness discipline. Skip PRs that slipped
    # through the regex (the issues endpoint also returns PRs).
    issue_json="$(gh api "repos/$REPO_NWO/issues/$issue_num" 2>/dev/null || echo '{}')"
    if [[ "$(echo "$issue_json" | jq -r 'has("pull_request")')" == "true" ]]; then
      continue
    fi
    # Only an issue that is OPEN right now can be closed BY this merge; one that
    # is already closed was closed by something else and is not ours to revert.
    if [[ "$(echo "$issue_json" | jq -r '.state // ""')" != "open" ]]; then
      continue
    fi
    PARTIAL_OPEN_BEFORE_MERGE="${PARTIAL_OPEN_BEFORE_MERGE:+$PARTIAL_OPEN_BEFORE_MERGE }$issue_num"

    if ! grep -qx "$issue_num" <<<"$close_refs"; then
      continue
    fi
    PARTIAL_CONFLICT_ISSUES="${PARTIAL_CONFLICT_ISSUES:+$PARTIAL_CONFLICT_ISSUES }$issue_num"

    local body_offending commit_offending partial_offending dr=""
    # Match _check_no_open_stacked_children's dry-run contract: report the
    # would-be outcome without claiming a merge is happening.
    [[ "${DRY_RUN:-false}" == "true" ]] && dr="[dry-run] "
    body_offending="$(_closing_ref_snippets "$pr_body" "$issue_num")"
    commit_offending="$(_closing_ref_snippets "$commit_messages" "$issue_num")"
    # The declaration text itself (AC #4, #5234) — quoted alongside the closing
    # keyword below so an operator can see both sides and judge for themselves
    # whether the declaration was a real trailer or, e.g., prose that happened
    # to survive the structural anchor.
    partial_offending="$(_partial_increment_ref_snippets "$pr_body" "$issue_num")"

    # Name the source, because the operator remedy differs per source: edit the
    # PR body, reword/amend a commit, or unlink a Development-sidebar reference.
    if [[ -n "$body_offending" ]]; then
      warning "${dr}Partial-increment conflict (#4569): PR #$PR_NUMBER declares a NON-closing \`Part of\`/\`Contributes to\` reference to #$issue_num (\"$partial_offending\"), but its body ALSO carries a closing reference to #$issue_num (\"$body_offending\") — GitHub honors a closing keyword ANYWHERE in the body, so merging this PR WILL close #$issue_num against the declared intent."
      warning "  ${dr}merge-pr.sh would reopen #$issue_num immediately after the merge. To avoid the close/reopen flicker entirely, edit the PR body so no closing keyword is immediately followed by \`#$issue_num\` (e.g. write \`close the issue\` or \`close issue #$issue_num\` instead of \`close #$issue_num\`), then re-run this merge."
    elif [[ -n "$commit_offending" ]]; then
      warning "${dr}Partial-increment conflict (#4595): PR #$PR_NUMBER declares a NON-closing \`Part of\`/\`Contributes to\` reference to #$issue_num (\"$partial_offending\"), but a closing keyword in a commit message of this PR references #$issue_num (\"$commit_offending\") — this merge squashes without overriding the commit message, so GitHub composes the squash message from these commits and merging WILL close #$issue_num against the declared intent."
      warning "  ${dr}merge-pr.sh would reopen #$issue_num immediately after the merge. To avoid the close/reopen flicker entirely, reword the offending commit message (\`git commit --amend\` / \`git rebase -i\` + force-push) so no closing keyword is immediately followed by \`#$issue_num\`, then re-run this merge."
    else
      warning "${dr}Partial-increment conflict (#4569): PR #$PR_NUMBER declares a NON-closing \`Part of\`/\`Contributes to\` reference to #$issue_num (\"$partial_offending\"), but GitHub reports #$issue_num as a closing target of this PR (no closing keyword found in the body or commit messages — most likely a Development-sidebar link), so merging this PR WILL close #$issue_num against the declared intent."
      warning "  ${dr}merge-pr.sh would reopen #$issue_num immediately after the merge. To avoid the close/reopen flicker entirely, unlink #$issue_num from this PR's Development sidebar, then re-run this merge."
    fi
  done <<< "$partial_refs"

  return 0
}

# Runs before either merge path so the operator sees the conflict BEFORE the
# close happens, and so --dry-run reports it without merging. Best-effort.
_check_partial_increment_close_conflict || true

info "Merging PR #$PR_NUMBER: $PR_TITLE"
info "Branch: $PR_BRANCH"

# ---------------------------------------------------------------------------
# Partial-increment label reset (#3667).
#
# A PR that implements only a slice of a family/epic issue references it with a
# NON-closing keyword — `Part of #N` / `Contributes to #N` (convention in
# builder-pr.md) — deliberately so the issue survives the merge for further
# work. GitHub never auto-closes such an issue, and the merge path otherwise
# leaves `loom:building` orphaned on it: the #2838 "skip label cleanup on close"
# decision only reasoned about the `Closes #N` auto-close case, where GitHub
# closes the issue and stale labels on closed items are harmless. Nothing else
# reclaims the label until a time-gated `/sweep all` stale-claim pass (>=2h),
# and non-aggressive sweeps hard-skip the still-`loom:building` issue
# indefinitely (issue #3667).
#
# Here — at the deterministic merge choke point — we swap each such still-open,
# still-`loom:building` referenced issue back to `loom:issue`, mirroring
# orphan_recovery.py's recover_issue() label-reset semantics (loom:building ->
# loom:issue, i.e. return to the ready queue). No liveness check is needed: a
# merge just happened on the PR that necessarily came from whoever held the
# claim, so the current increment's work is provably done — a deterministic,
# not heuristic, signal. Closing keywords (`Closes`/`Fixes`/`Resolves`) are NOT
# matched — GitHub auto-closes those and the #2838 no-cleanup path stays
# untouched.
#
# GitHub-only for v1 (guarded on FORGE_TYPE); merge-pr.sh already branches on
# forge type elsewhere. Every step is best-effort and must never fail the merge.

# Reset a single referenced issue's labels if — verified fresh at merge time —
# it is still open and still carries loom:building. Idempotent: a no-op when the
# issue is already closed, already lacks loom:building (e.g. re-claimed by a
# second builder), or is actually a PR.
_reset_one_partial_issue() {
  local issue_num="$1"
  local issue_json issue_state issue_labels reopened=false

  # Fresh (uncached) read so we see the label state AS OF the merge, not as of
  # PR creation. Plain `gh api` is uncached; use it directly (not $GH, which may
  # be gh-cached) to avoid a stale cached view masking a fresh re-claim.
  issue_json="$(gh api "repos/$REPO_NWO/issues/$issue_num" 2>/dev/null || echo '{}')"

  # The GitHub issues endpoint also returns PRs (a PR is an issue with a
  # .pull_request member). Never mutate a PR that slipped through the regex.
  if [[ "$(echo "$issue_json" | jq -r 'has("pull_request")')" == "true" ]]; then
    return 0
  fi

  issue_state="$(echo "$issue_json" | jq -r '.state // ""')"
  if [[ "$issue_state" != "open" ]]; then
    # #4569: a partial-increment issue that was OPEN pre-merge and is closed now
    # was closed BY this merge. If the pre-merge guard recorded a closing
    # reference to it from this very PR (a stray `close #N` in prose, or a
    # Development-sidebar link), that close contradicts the PR's own declared
    # `Part of` / `Contributes to` intent — revert it, then fall through to the
    # normal label swap so the issue re-enters the ready queue.
    if _partial_ref_is_conflicted "$issue_num"; then
      warning "Partial-increment reset: issue #$issue_num was auto-closed by PR #$PR_NUMBER's merge despite its non-closing \`Part of\`/\`Contributes to\` reference (a closing reference to #$issue_num was detected pre-merge) — reopening (#4569)"
      # forge_gh_reopen_issue_rl_safe (#4856): falls back to a REST PATCH
      # (state=open) when `gh issue reopen`'s GraphQL mutation is rate-limited.
      if forge_gh_reopen_issue_rl_safe "$REPO_NWO" "$issue_num" 2>/dev/null; then
        success "Issue #$issue_num reopened (premature auto-close reverted)"
        reopened=true
        _post_premature_close_comment "$issue_num"
      else
        warning "Could not reopen issue #$issue_num after its premature auto-close — reopen manually: gh issue reopen $issue_num --repo $REPO_NWO"
        return 0
      fi
    elif _partial_ref_was_open_before_merge "$issue_num"; then
      # Open before the merge, closed after it, but this PR carries no closing
      # reference we can attribute it to. Could be a deliberate close by a human
      # or another agent in the same window, so do NOT revert it — just make the
      # coincidence loud enough to investigate.
      warning "Partial-increment reset: issue #$issue_num was open before PR #$PR_NUMBER merged and is now closed (state='${issue_state:-unknown}'), but no closing reference to it was detected on this PR — NOT reopening automatically (it may be a deliberate close). If this was a premature auto-close, reopen it with: gh issue reopen $issue_num --repo $REPO_NWO"
      return 0
    else
      info "Partial-increment reset: issue #$issue_num is not open (state='${issue_state:-unknown}') — skipping"
      return 0
    fi
  fi

  issue_labels="$(echo "$issue_json" | jq -r '.labels[]?.name' 2>/dev/null || true)"
  if ! printf '%s\n' "$issue_labels" | grep -qx 'loom:building'; then
    info "Partial-increment reset: issue #$issue_num is not loom:building — skipping (idempotent)"
    return 0
  fi

  info "Partial-increment reset: PR #$PR_NUMBER merged as a partial slice of #$issue_num; returning it to the ready queue"
  # forge_gh_swap_label_rl_safe (#4856): falls back to REST (DELETE the old
  # label, POST the new one) when `gh issue edit`'s GraphQL mutation is
  # rate-limited, rather than silently dropping the label swap.
  if forge_gh_swap_label_rl_safe "$REPO_NWO" "$issue_num" "loom:building" "loom:issue" 2>/dev/null; then
    success "Issue #$issue_num: loom:building -> loom:issue (partial increment; issue remains open)"
    local ts comment reopen_note=""
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    [[ "$reopened" == "true" ]] && reopen_note="
- **Reopened** this issue (GitHub had auto-closed it from a stray closing keyword in PR #$PR_NUMBER's body or one of its commit messages — see #4569)"
    comment="## Partial Increment Merged

PR #$PR_NUMBER merged with a non-closing \`Part of\` / \`Contributes to\` reference, so this issue remains **open** for further work.

**Action taken**:$reopen_note
- Removed \`loom:building\` label
- Added \`loom:issue\` label to return to the ready queue

This issue is now available for the next increment (a subsequent \`/loom:sweep\` will treat it as ready rather than in-flight).

---
*Reset by merge-pr.sh (#3667) at $ts*"
    # forge_gh_comment_rl_safe (#4856): falls back to the REST comments
    # endpoint on a GraphQL rate-limit rejection.
    forge_gh_comment_rl_safe "$REPO_NWO" "$issue_num" "$comment" 2>/dev/null || \
      warning "Could not post partial-increment comment on issue #$issue_num (label swap still applied)"
  else
    warning "Could not reset labels on issue #$issue_num (partial increment) — may need manual 'gh issue edit'"
  fi
}

# Audit trail for a reverted premature auto-close (#4569). Posted right after
# the reopen so the record survives even when the label swap below is skipped
# (e.g. the issue no longer carries loom:building). Best-effort.
_post_premature_close_comment() {
  local issue_num="$1" ts comment
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  comment="## Premature Auto-Close Reverted

PR #$PR_NUMBER referenced this issue with a **non-closing** \`Part of\` / \`Contributes to\` keyword — a declared partial increment, so this issue was meant to stay **open** after the merge. GitHub closed it anyway, because a **closing keyword** (\`close\`/\`fix\`/\`resolve\` and their tense variants) immediately followed by \`#$issue_num\` appeared elsewhere in the PR — in the body, or in one of the PR's commit messages (this merge squashes without overriding the commit message, so GitHub composes the squash message from those commits).

GitHub honors a closing keyword **anywhere** in a PR body or squash commit message — not only in a line-leading trailer — so prose like \"…then close #$issue_num\" in a follow-up checklist, or a stray \`close #$issue_num\` in a commit message, creates a real closing link that overrides the intended \`Contributes to #$issue_num\`.

**Action taken**: reopened this issue.

**To avoid this**: never put a closing keyword immediately before \`#$issue_num\` anywhere in a partial-increment PR's body **or commit messages**. Write \`close the issue\` or \`close issue #$issue_num\` instead of \`close #$issue_num\`.

---
*Reopened by merge-pr.sh (#4569) at $ts*"
  # forge_gh_comment_rl_safe (#4856): REST fallback on GraphQL rate limit.
  forge_gh_comment_rl_safe "$REPO_NWO" "$issue_num" "$comment" 2>/dev/null || \
    warning "Could not post premature-close comment on issue #$issue_num (reopen still applied)"
}

# Parse the merged PR body for non-closing partial-increment references and
# reset each referenced issue. Best-effort; returns 0 unconditionally.
_reset_partial_increment_labels() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0

  local pr_body
  pr_body="$(echo "$PR_JSON" | jq -r '.body // ""')"
  [[ -n "$pr_body" ]] || return 0

  # Issue numbers referenced with a NON-closing partial-increment keyword.
  # Shares _partial_increment_refs with the pre-merge #4569 conflict guard so the
  # two passes can never disagree about which issues are partial increments.
  local refs
  refs="$(_partial_increment_refs "$pr_body")"
  [[ -n "$refs" ]] || return 0

  local issue_num
  while IFS= read -r issue_num; do
    [[ -n "$issue_num" ]] || continue
    _reset_one_partial_issue "$issue_num"
  done <<< "$refs"

  return 0
}

# ---------------------------------------------------------------------------
# Closed-issue `loom:building` cleanup (#6199).
#
# The #2838 "no label cleanup on close" decision reasoned that a stale label
# on a closed issue is harmless — every queue query filters on open state, so
# it can never cause a duplicate build or a blocked candidate. That is still
# true. What #6199 found is that the decision also has a real, if narrow,
# cost: any consumer that reasonably reads `loom:building` as "in flight"
# WITHOUT also filtering on state — a dashboard, a capacity check, an
# operator `gh issue list --label loom:building` spot-check, or a future tool
# — gets pure noise once the population of closed-but-still-labelled issues
# grows (observed: 20 stale claims on one consumer repo, 0 real ones). The
# label has stopped meaning what its name says for anyone who doesn't already
# know to filter it out.
#
# Scope decision (recorded here per #6199's own "worth deciding deliberately"
# note): this pass covers ONLY the merge-driven auto-close path (`Closes #N`
# / `Fixes #N` / `Resolves #N`, resolved the same way Champion's "Verify
# Issue Auto-Close" step does — via GitHub's GraphQL
# `closingIssuesReferences`, see forge_pr_close_targets above) — the
# deterministic case merge-pr.sh already owns and can act on right at the
# confirmed-merge choke point, with zero extra liveness/state ambiguity: a
# merge just happened on the PR that closed the issue, so the label is
# unconditionally stale. An issue closed OUTSIDE a merge (closed manually, as
# a duplicate, or `--reason "not planned"` by an autonomous role) is
# deliberately OUT OF SCOPE here — merge-pr.sh has no hook into that path at
# all, and inventing one (e.g. polling every issue close event) is
# disproportionate to a cosmetic-but-annoying defect. That population is
# instead handled by the standalone, idempotent
# `clean-stale-building-labels.sh` (same directory) — run once against this
# repo as part of #6199 to clear the accumulated backlog, and safe to re-run
# on demand (by an operator, or wired into a periodic role) for any future
# manual-close stragglers. See that script's header for the full rationale.
#
# Runs AFTER _reset_partial_increment_labels (and therefore after any #4569
# premature-auto-close revert) so an issue that pass just reopened is no
# longer `closed` by the time this pass reads it — reopened partial-increment
# issues must keep `loom:building` (they return to `loom:issue` instead, via
# that pass), never lose the label outright.
#
# GitHub-only for v1 (guarded on FORGE_TYPE), mirroring
# _reset_partial_increment_labels's gating — forge_gh_remove_label_rl_safe is
# a `gh`-specific helper. Every step is best-effort and must never fail the
# merge.

# Strip `loom:building` from one issue this merge closed, if it is still
# present. Idempotent: a no-op when the issue isn't actually closed (a
# transient PR-close-target false positive, or #4569 reopened it above),
# already lacks the label, or is actually a PR.
_strip_one_closed_issue_building_label() {
  local issue_num="$1"
  local issue_json issue_state issue_labels

  # Fresh (uncached) read, mirroring _reset_one_partial_issue's freshness
  # discipline: we need the label/state AS OF right now, not as of PR
  # creation or the GraphQL closingIssuesReferences snapshot.
  issue_json="$(gh api "repos/$REPO_NWO/issues/$issue_num" 2>/dev/null || echo '{}')"

  # A PR is also an "issue" on this endpoint (has a .pull_request member).
  if [[ "$(echo "$issue_json" | jq -r 'has("pull_request")')" == "true" ]]; then
    return 0
  fi

  issue_state="$(echo "$issue_json" | jq -r '.state // ""')"
  if [[ "$issue_state" != "closed" ]]; then
    # Not (or no longer) closed — either a #4569 revert just reopened it, the
    # forge's close hadn't landed yet when we read it, or it was never
    # actually closed. Leave the label; a later merge or the standalone
    # cleanup script will catch it once it genuinely closes.
    return 0
  fi

  issue_labels="$(echo "$issue_json" | jq -r '.labels[]?.name' 2>/dev/null || true)"
  if ! printf '%s\n' "$issue_labels" | grep -qx 'loom:building'; then
    return 0
  fi

  if forge_gh_remove_label_rl_safe "$REPO_NWO" "$issue_num" "loom:building" 2>/dev/null; then
    success "Issue #$issue_num: removed stale loom:building label (closed by this merge, #6199)"
  else
    warning "Could not remove loom:building from closed issue #$issue_num — may need manual: gh issue edit $issue_num --repo $REPO_NWO --remove-label loom:building"
  fi
}

# Resolve this PR's closing issue targets and strip loom:building from each
# that is (still) closed. Best-effort; returns 0 unconditionally.
_strip_closed_issue_building_labels() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0

  local close_targets
  close_targets="$(forge_pr_close_targets "$PR_NUMBER" "$GH" 2>/dev/null || true)"
  [[ -n "$close_targets" ]] || return 0

  local issue_num
  while IFS= read -r issue_num; do
    [[ -n "$issue_num" ]] || continue
    _strip_one_closed_issue_building_label "$issue_num"
  done <<< "$close_targets"

  return 0
}

# ---------------------------------------------------------------------------
# Automated stacked-PR reconciliation on parent merge (#3747, stacked-PR v2,
# item 1 of the v2 epic — the remaining five items stay deferred).
#
# When a stacked PARENT PR (branch feature/issue-<N>) squash-merges, any CHILD
# PRs based on the parent branch still carry the parent's now-squashed pre-merge
# commits. reconcile-stack.sh performs the git surgery — `git rebase --onto
# <default> <parent-branch> <child-branch>`, `push --force-with-lease`, retarget
# the child PR base to the default branch — that strips them. v1 (#3729) shipped
# reconcile-stack.sh as a STANDALONE, operator-invoked script and deliberately
# left merge-pr.sh untouched. This v2 slice fires it AUTOMATICALLY here — a
# best-effort, GitHub-only step gated so it never races a live Builder that still
# holds the child branch checked out.
#
# Discovery is via a LIVE forge query (`gh pr list --base <parent>`), NOT the
# ephemeral loom-daemon SweepRegistry: terminal registry entries are
# garbage-collected ~1h after transition and the registry only exists at all when
# loom-daemon is running, but this function may run from Champion's cron or an
# interactive /loom:sweep merge with no daemon present (see
# .loom/docs/daemon-reference.md → "Stacked-PR dependency").
#
# Safe/unsafe split per child, gated on the child ISSUE's loom:building label
# (fresh, uncached `gh api` read, mirroring _reset_one_partial_issue's freshness
# discipline):
#   - Safe   (child issue NOT loom:building): no live claim on the child, so
#            invoke reconcile-stack.sh directly.
#   - Unsafe (child issue still loom:building): a live Builder likely has the
#            child branch checked out in its own worktree; an out-of-band rebase
#            + force-with-lease would corrupt its in-progress work. Skip the
#            auto-rebase and post a comment noting reconciliation is deferred
#            until the Builder finishes (a later parent-merge-triggered pass, or
#            a manual reconcile-stack.sh run, picks it up).
#
# Idempotent by construction: once a child's base is retargeted away from the
# parent branch, `gh pr list --base <parent>` returns zero rows, so re-runs are
# no-ops and nothing double-fires.
#
# Every step is best-effort and must NEVER change merge-pr.sh's exit code — the
# parent merge already happened. Runs BEFORE branch deletion so the parent
# branch ref still resolves as reconcile-stack.sh's rebase <upstream> argument.

# Reconcile (or defer) one discovered child PR. Best-effort; returns 0.
_reconcile_one_stacked_child() {
  local child_pr="$1" child_branch="$2" parent_branch="$3"

  # Derive the child ISSUE number from its head branch (feature/issue-<N>) so we
  # can check its live claim label. A child branch that is not a feature/issue-N
  # branch has no loom:building claim to race, so it is treated as safe.
  local child_issue=""
  if [[ "$child_branch" =~ ^feature/issue-([0-9]+)$ ]]; then
    child_issue="${BASH_REMATCH[1]}"
  fi

  # Fresh (uncached) label read — mirrors _reset_one_partial_issue: use plain
  # `gh api` (not $GH, which may be gh-cached) so a stale cached view cannot mask
  # a live re-claim. A read failure is treated as "not building" (safe) since the
  # reconcile itself is best-effort and force-with-lease still protects the branch.
  local building="false"
  if [[ -n "$child_issue" ]]; then
    local issue_json issue_labels
    issue_json="$(gh api "repos/$REPO_NWO/issues/$child_issue" 2>/dev/null || echo '{}')"
    issue_labels="$(echo "$issue_json" | jq -r '.labels[]?.name' 2>/dev/null || true)"
    if printf '%s\n' "$issue_labels" | grep -qx 'loom:building'; then
      building="true"
    fi
  fi

  if [[ "$building" == "true" ]]; then
    # Unsafe: defer, do not rebase.
    info "Stacked reconcile: child PR #$child_pr (issue #$child_issue) is still loom:building — deferring auto-rebase to avoid racing a live Builder"
    local ts comment
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    comment="## Stacked parent merged — reconciliation deferred

Parent branch \`$parent_branch\` squash-merged, but this child's issue #$child_issue is still \`loom:building\` — a Builder likely has this branch checked out. Auto-reconciliation was **skipped** to avoid racing that in-progress work with an out-of-band \`git rebase --onto\` + \`push --force-with-lease\`.

**What happens next**: once issue #$child_issue is no longer \`loom:building\`, a subsequent parent-merge-triggered pass will reconcile this PR automatically. You can also reconcile it by hand now (from a clean checkout, only once the Builder has finished):

\`\`\`
./.loom/scripts/reconcile-stack.sh $child_pr $parent_branch
\`\`\`

---
*Deferred by merge-pr.sh (#3747) at $ts*"
    # forge_gh_comment_rl_safe (#4856): the REST comments endpoint is shared
    # by issues and PRs, so the same helper covers this `gh pr comment` call
    # site's GraphQL rate-limit fallback.
    forge_gh_comment_rl_safe "$REPO_NWO" "$child_pr" "$comment" 2>/dev/null || \
      warning "Could not post deferred-reconciliation comment on PR #$child_pr"
    return 0
  fi

  # Safe: no live claim — run the existing reconcile script unmodified. Do NOT
  # re-implement the rebase/force-with-lease/retarget logic inline.
  info "Stacked reconcile: parent '$parent_branch' merged; reconciling child PR #$child_pr onto the default branch"
  if "$SCRIPT_DIR/reconcile-stack.sh" "$child_pr" "$parent_branch"; then
    success "Stacked reconcile: child PR #$child_pr reconciled onto the default branch"
  else
    warning "Stacked reconcile: reconcile-stack.sh failed for child PR #$child_pr (rebase conflict, rejected force-with-lease push, or retarget failure). The parent merge is unaffected — reconcile manually: ./.loom/scripts/reconcile-stack.sh $child_pr $parent_branch"
  fi
  return 0
}

# Discover open child PRs stacked on the just-merged parent branch and reconcile
# (or defer) each. Best-effort; returns 0 unconditionally.
_auto_reconcile_stacked_children() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0

  # Only a parent PR on a feature/issue-<N> branch can have stacked children.
  [[ "$PR_BRANCH" =~ ^feature/issue-([0-9]+)$ ]] || return 0

  # Live forge discovery — NEVER the daemon registry. Plain `gh` (uncached) so we
  # see child PRs as of the merge, not a cached list snapshot.
  local children_json
  children_json="$(gh pr list --repo "$REPO_NWO" --base "$PR_BRANCH" --state open \
    --json number,headRefName 2>/dev/null || echo '[]')"
  [[ -n "$children_json" ]] || return 0

  local count
  count="$(echo "$children_json" | jq 'length' 2>/dev/null || echo 0)"
  [[ "$count" -gt 0 ]] || return 0

  info "Stacked reconcile: found $count open child PR(s) based on '$PR_BRANCH'"

  if [[ ! -x "$SCRIPT_DIR/reconcile-stack.sh" ]]; then
    warning "Stacked reconcile: reconcile-stack.sh not found or not executable at $SCRIPT_DIR — skipping auto-reconciliation"
    return 0
  fi

  local rows child_pr child_branch
  rows="$(echo "$children_json" | jq -r '.[] | "\(.number)\t\(.headRefName)"' 2>/dev/null || true)"
  while IFS=$'\t' read -r child_pr child_branch; do
    [[ -n "$child_pr" ]] || continue
    _reconcile_one_stacked_child "$child_pr" "$child_branch" "$PR_BRANCH"
  done <<< "$rows"

  return 0
}

# ---------------------------------------------------------------------------
# Stale-cached-mergeable recheck before refusal (#6104).
#
# GitHub's REST `.mergeable` field is computed asynchronously and invalidated
# on every push to the base branch. On a repo with continuous automated
# merges it can read a stale `false` shortly after a base-branch push even
# though the branch would merge cleanly against current main. The gate at
# the synchronous-merge callsite previously trusted the first `.mergeable`
# read and refused immediately, asserting a conflict that did not actually
# exist.
#
# This function, called only once `.mergeable` has already read `false`:
#   1. Re-queries PR state via the UNCACHED recheck path
#      (forge_get_pr_nocache) after a short backoff, up to `retries` times.
#      Uses the uncached path deliberately — $GH may be wrapped by
#      `gh-cached` (see merge-pr.sh's $GH setup), and re-reading through that
#      cache would keep returning the same stale value, defeating the
#      backoff entirely (mirrors the existing _NRC_RECHECK_JSON pattern).
#   2. If still `false` after all retries, corroborates with a local
#      `git merge-tree` check against the freshly fetched base ref — this is
#      what lets the caller distinguish "the forge's cached state is
#      stale/unknown" from "this branch genuinely conflicts" (a real
#      conflict will also fail `git merge-tree`).
#
# Usage:
#   _recheck_mergeable_before_refusal NWO PR_NUMBER GH_CMD BASE_REF HEAD_REF REPO_ROOT [RETRIES] [DELAY]
#
# Echoes exactly one "<action>:<reason>" line on stdout, always returns 0 (the
# decision is conveyed via stdout, not exit status, so callers under
# `set -e` can safely capture it with `$(...)`):
#   merge:<reason>            - proceed with the merge (recheck succeeded, or
#                                merge-tree independently confirms clean).
#   refuse-conflict:<reason>  - refuse; local git merge-tree independently
#                                confirms a real conflict.
#   refuse-stale:<reason>     - refuse; the forge's cached state never
#                                resolved to true, and local corroboration was
#                                unavailable (missing refs, fetch failure) —
#                                NOT a confirmed conflict, just unresolved.
_recheck_mergeable_before_refusal() {
  local nwo="$1" pr_number="$2" gh_cmd="$3" base_ref="$4" head_ref="$5" repo_root="$6"
  local retries="${7:-3}" delay="${8:-3}"
  local attempt recheck_json recheck_mergeable

  for attempt in $(seq 1 "$retries"); do
    sleep "$delay"
    recheck_json="$(forge_get_pr_nocache "$nwo" "$pr_number" "$gh_cmd" 2>/dev/null || echo '{}')"
    recheck_mergeable="$(echo "$recheck_json" | jq -r '.mergeable // empty')"
    if [[ "$recheck_mergeable" == "true" ]]; then
      echo "merge:cached mergeable=false was stale; recheck #$attempt (post-backoff, uncached) now reports mergeable=true"
      return 0
    fi
  done

  # Still false/unknown after the backoff retries — corroborate with a local
  # git merge-tree check before conceding this is a genuine conflict.
  if [[ -z "$base_ref" ]] || [[ -z "$head_ref" ]]; then
    echo "refuse-stale:forge reports mergeable=false after $retries recheck(s); base/head ref unavailable for local corroboration"
    return 0
  fi

  if ! git -C "$repo_root" fetch -q origin "$base_ref" "$head_ref" 2>/dev/null; then
    echo "refuse-stale:forge reports mergeable=false after $retries recheck(s); could not fetch origin/$base_ref and origin/$head_ref for local corroboration"
    return 0
  fi

  if git -C "$repo_root" merge-tree --write-tree "origin/$base_ref" "origin/$head_ref" >/dev/null 2>&1; then
    echo "merge:forge reports mergeable=false after $retries recheck(s), but local 'git merge-tree' against origin/$base_ref is clean — proceeding (stale/false-negative cached state)"
    return 0
  fi

  echo "refuse-conflict:forge reports mergeable=false after $retries recheck(s), confirmed by local 'git merge-tree' against origin/$base_ref — this branch genuinely conflicts"
  return 0
}

# ---------------------------------------------------------------------------
# Repo-level "Allow auto-merge" disabled — proactive wait-then-merge (#3820).
#
# When the repository's GitHub "Allow auto-merge" setting is OFF
# (`gh api repos/{nwo} --jq .allow_auto_merge` == false), the server-side
# auto-merge queue can NEVER be enabled — enablePullRequestAutoMerge is rejected
# regardless of PR state. The reactive #3763 fallback catches that post-mutation
# rejection, but only degrades gracefully when the PR is ALREADY immediately
# mergeable; when auto-merge is disabled AND the PR is not yet CLEAN (checks
# still running / .mergeable not yet computed), #3763's mergeability recheck sees
# `.mergeable != true` and preserves the terminal error, so the PR never merges
# (the reported failure on a repo with allow_auto_merge:false).
#
# This function is entered PROACTIVELY from the repo-setting probe below (so we
# never attempt the doomed mutation at all) and degrades `--auto` to
# "wait-for-checks-then-merge, or immediate merge if already CLEAN": it polls the
# head-SHA check-runs (bounded by LOOM_AUTO_MERGE_TIMEOUT, same knobs as the
# UNSTABLE fallback) until they settle, then returns 0 so the caller flips to the
# synchronous-merge path. Unlike the UNSTABLE branch it also handles the
# already-CLEAN case (nothing failing, nothing pending) by returning 0 for an
# immediate merge rather than hitting that branch's defensive "unknown gap" error.
#
# Contract:
#   - returns 0  → safe to proceed to the synchronous-merge path (caller flips
#                  AUTO_MERGE=false). Also returned if the PR merged concurrently
#                  while waiting (the synchronous path's own race-detection then
#                  no-ops cleanly).
#   - calls error() (exit 1) → a required status check failed, or the wait timed
#                  out. A normal recoverable failure Champion's cron retries.
# GitHub-only by construction (only invoked when the GitHub-only probe returns
# "false"). Requires LOOM_AUTO_MERGE_POLL_INTERVAL / LOOM_AUTO_MERGE_TIMEOUT set.
_wait_for_checks_then_sync_merge() {
  local head_sha base_ref
  head_sha="$(echo "$PR_JSON" | jq -r '.head.sha // empty')"
  base_ref="$(echo "$PR_JSON" | jq -r '.base.ref // empty')"

  # Without the head SHA we cannot reason about checks — proceed to the
  # synchronous merge, which will itself reject if a required check blocks it.
  if [[ -z "$head_sha" ]]; then
    info "PR #$PR_NUMBER: head SHA unavailable; proceeding directly to synchronous merge"
    return 0
  fi

  local deadline
  deadline=$(( $(date +%s) + LOOM_AUTO_MERGE_TIMEOUT ))

  while true; do
    # A concurrent merger may have completed the PR while we waited.
    local recheck_json
    recheck_json="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
    if [[ "$(echo "$recheck_json" | jq -r '.merged // false')" == "true" ]]; then
      warning "PR #$PR_NUMBER merged by another process while waiting for checks"
      return 0
    fi

    # Fetch the check-runs rollup for the head SHA. Retry once to absorb a blip,
    # then treat a persistent fetch failure as still-pending (bounded wait),
    # mirroring the UNSTABLE fallback's #3678 discipline.
    local fetch_rc=0 runs_raw
    runs_raw="$(forge_get_check_runs "$REPO_NWO" "$head_sha" 2>/dev/null)" || fetch_rc=$?
    if [[ "$fetch_rc" -ne 0 ]]; then
      fetch_rc=0
      runs_raw="$(forge_get_check_runs "$REPO_NWO" "$head_sha" 2>/dev/null)" || fetch_rc=$?
    fi
    if [[ "$fetch_rc" -ne 0 ]]; then
      if [[ "$(date +%s)" -ge "$deadline" ]]; then
        error "Timed out after ${LOOM_AUTO_MERGE_TIMEOUT}s waiting for check-runs to become fetchable for PR #$PR_NUMBER (repo has auto-merge disabled). Re-run once the forge API is healthy, or raise LOOM_AUTO_MERGE_TIMEOUT."
      fi
      warning "Failed to fetch check-runs for PR #$PR_NUMBER (rc=$fetch_rc); treating as still-pending and continuing to poll"
      sleep "$LOOM_AUTO_MERGE_POLL_INTERVAL"
      continue
    fi

    # Failing (terminal non-success) and pending (not yet completed) check names.
    local failing pending
    failing="$(echo "$runs_raw" | \
      jq -r '[.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "cancelled" or .conclusion == "action_required") | .name] | unique | .[]' 2>/dev/null || true)"
    pending="$(echo "$runs_raw" | \
      jq -r '[.check_runs[] | select(.status != "completed") | .name] | unique | .[]' 2>/dev/null || true)"

    if [[ -n "$failing" ]]; then
      # A check failed — classify against branch protection. A required failing
      # check can never merge on this SHA; refuse now. A lookup failure fails
      # closed (refuse), mirroring the UNSTABLE fallback.
      local required lookup_rc=0
      required="$(forge_get_required_status_check_contexts "$REPO_NWO" "$base_ref" "$GH" 2>/dev/null)" || lookup_rc=$?
      if [[ "$lookup_rc" -ne 0 ]]; then
        error "Failed to resolve required status checks for $base_ref (rc=$lookup_rc); refusing to merge PR #$PR_NUMBER with failing check(s) while auto-merge is disabled"
      fi
      local overlap
      overlap="$(comm -12 \
        <(printf '%s\n' "$failing" | sort -u) \
        <(printf '%s\n' "$required" | sort -u))"
      if [[ -n "$overlap" ]]; then
        error "Cannot merge PR #$PR_NUMBER: a required status check has failed ($(printf '%s' "$overlap" | tr '\n' ' ')). Fix the check and re-run the merge."
      fi
      if [[ -z "$pending" ]]; then
        # Only informational (non-required) checks failing and nothing pending →
        # a synchronous merge is safe (matches the UNSTABLE #3486 fallback).
        info "PR #$PR_NUMBER: only informational (non-required) check(s) failing; proceeding to synchronous merge"
        return 0
      fi
      # Informational failures but other checks still running — fall through to
      # the pending wait below.
    fi

    if [[ -n "$pending" ]]; then
      if [[ "$(date +%s)" -ge "$deadline" ]]; then
        local n; n="$(printf '%s\n' "$pending" | wc -l | tr -d ' ')"
        error "Timed out after ${LOOM_AUTO_MERGE_TIMEOUT}s waiting for ${n} pending check(s) on PR #$PR_NUMBER to complete (repo has auto-merge disabled). Re-run once CI settles, or raise LOOM_AUTO_MERGE_TIMEOUT."
      fi
      local n; n="$(printf '%s\n' "$pending" | wc -l | tr -d ' ')"
      info "PR #$PR_NUMBER: ${n} check(s) still running (repo auto-merge disabled); waiting ${LOOM_AUTO_MERGE_POLL_INTERVAL}s for CI (timeout ${LOOM_AUTO_MERGE_TIMEOUT}s)..."
      sleep "$LOOM_AUTO_MERGE_POLL_INTERVAL"
      continue
    fi

    # Nothing failing (or only informational), nothing pending → effectively
    # CLEAN. Proceed to the synchronous merge.
    info "PR #$PR_NUMBER: checks settled (repo auto-merge disabled); proceeding to synchronous merge"
    return 0
  done
}

# Handle auto-merge mode
#
# The auto-merge path now mirrors the sync path's resilience patterns:
#   - Retry on "Base branch was modified" with the same backoff loop.
#   - Recheck PR state on failure (concurrent shepherd may have already
#     merged it).
#   - Fall through to the shared cleanup block (lines below) instead of
#     exiting early. Cleanup is gated on `PR.merged == true`; if the
#     server-side merge is still queued, we skip local cleanup and let
#     loom-clean handle it.
#
# See issue #3279.

# Freshest possible head-SHA read for the merge's optimistic-concurrency
# precondition (#5579). $PR_HEAD_SHA (set above from the initial $PR_JSON
# fetch) may have gone through the gh-cached wrapper via $GH — fine for the
# branch-cleanup safety check it also feeds, but a merge-gating precondition
# must observe current state as closely as possible: a stale value here only
# ever produces a spurious "head moved" re-queue (fail-safe — it can never
# cause a stale-but-accepted merge, since the forge itself does the real
# comparison against its own current head), but staleness still costs an
# unneeded round trip, so read it live via the uncached helper immediately
# before either merge path runs. A lookup failure falls back to the
# already-known $PR_HEAD_SHA rather than merging with no precondition at all.
MERGE_PRECONDITION_SHA="$PR_HEAD_SHA"
_MPS_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
_MPS_FRESH_SHA="$(echo "$_MPS_JSON" | jq -r '.head.sha // empty' 2>/dev/null || echo '')"
[[ -n "$_MPS_FRESH_SHA" ]] && MERGE_PRECONDITION_SHA="$_MPS_FRESH_SHA"
unset _MPS_JSON _MPS_FRESH_SHA

if [[ "$AUTO_MERGE" == "true" ]]; then
  # Bounded poll window for the UNSTABLE-because-checks-are-still-running case
  # (#3664). Reuses the same env-var names/semantics as the shell Gitea
  # auto-merge poller (forge_auto_merge in lib/forge-helpers.sh) so both forges
  # share configuration. Defaults: 30s interval, 600s ceiling.
  # (Also consumed by the #3820 auto-merge-disabled wait path below.)
  LOOM_AUTO_MERGE_POLL_INTERVAL="${LOOM_AUTO_MERGE_POLL_INTERVAL:-30}"
  LOOM_AUTO_MERGE_TIMEOUT="${LOOM_AUTO_MERGE_TIMEOUT:-600}"

  # Proactive repo-level "Allow auto-merge" probe (#3820). Read the setting once
  # (GitHub only; Gitea and any probe failure return "unknown", preserving
  # existing behavior fail-safe). When it is explicitly disabled, the server-side
  # auto-merge queue can never be enabled, so skip the doomed mutation entirely
  # and degrade `--auto` to wait-for-checks-then-merge (immediate if CLEAN).
  REPO_AUTO_MERGE_ALLOWED="$(forge_check_auto_merge_allowed "$REPO_NWO" "$GH" 2>/dev/null || echo unknown)"

  if [[ "$DRY_RUN" == "true" ]]; then
    if [[ "$REPO_AUTO_MERGE_ALLOWED" == "false" ]]; then
      info "[dry-run] Repository 'Allow auto-merge' is disabled; would wait for checks then merge PR #$PR_NUMBER synchronously (immediate if already CLEAN)"
    else
      info "[dry-run] Would enable auto-merge for PR #$PR_NUMBER"
    fi
    exit 0
  fi

  MAX_MERGE_RETRIES=3
  MERGE_RETRY_DELAY=5
  AUTO_MERGE_OK=false

  # #3820: repo has auto-merge disabled → wait for checks, then fall through to
  # the synchronous-merge path instead of attempting the enable mutation. The
  # wait function either returns 0 (proceed) or error()s out terminally. Setting
  # AUTO_MERGE_OK=true lets the post-loop "after N attempts" guard pass; the loop
  # itself is short-circuited by the AUTO_MERGE guard on its first iteration.
  if [[ "$REPO_AUTO_MERGE_ALLOWED" == "false" ]]; then
    info "PR #$PR_NUMBER: repository 'Allow auto-merge' is disabled; --auto will wait for checks then merge synchronously (immediate if already CLEAN)"
    _wait_for_checks_then_sync_merge
    AUTO_MERGE=false
    AUTO_MERGE_OK=true
  fi

  for MERGE_ATTEMPT in $(seq 1 $MAX_MERGE_RETRIES); do
    # #3820: when the repo-level probe already converted --auto to a synchronous
    # merge (AUTO_MERGE flipped false above), do NOT attempt the enable mutation.
    [[ "$AUTO_MERGE" == "true" ]] || break
    AUTO_MERGE_OUTPUT=""
    # Prefer the native `loom-daemon forge auto-merge` (forge-agnostic; GitHub
    # via the enablePullRequestAutoMerge GraphQL mutation — a pure API call with
    # no working-tree checkout). It exits 3 to *decline* Gitea, in which case we
    # fall through to the shell forge_auto_merge below (which carries the Gitea
    # curl poll-and-merge). A native GitHub *failure* (exit 1) is NOT a decline:
    # its gh error is left in AUTO_MERGE_OUTPUT so the disabled/clean/unstable
    # detection further down fires exactly as it did for loom-auto-merge.
    #
    # #5589 (closes the #5579 gap noted here previously): the native path now
    # carries the same `expectedHeadOid` optimistic-concurrency precondition
    # as the shell forge_auto_merge/forge_merge_pr below, via
    # `--expected-head-sha`. A head-SHA mismatch on this path exits 4
    # (EX_FORGE_HEAD_MISMATCH in loom-daemon/src/forge_cmd.rs) — distinct
    # from both the Gitea-decline exit (3) and the generic failure exit (1) —
    # and is routed straight to `error_head_moved()` below, the same
    # "re-queue, not a failure" signal `_is_head_mismatch_response()` gives
    # the shell path.
    #
    # #6752: the native call is wrapped in `forge_cmd_perm_safe` so a stale or
    # scope-limited GitHub App installation token — `403 Resource not
    # accessible by integration`, which killed this exact step on PR #6751 —
    # escalates through the same force-mint-then-personal-token ladder the
    # shell `gh` write sites have had since #6074, instead of hard-failing the
    # merge. `loom-daemon forge` shells out to `gh`, so the ladder's
    # GH_TOKEN/GH_CONFIG_DIR swap reaches it. The wrapper preserves the exit
    # code verbatim (0/3/4/other) and escalates ONLY on that one signature.
    _AM_DECLINED=true
    if command -v loom-daemon &>/dev/null; then
      [[ $MERGE_ATTEMPT -eq 1 ]] && info "Using loom-daemon forge auto-merge (native forge-agnostic auto-merge)"
      # `|| _AM_RC=$?` keeps the failing substitution from tripping `set -e`
      # and captures the native exit code (0=merged, 3=Gitea decline,
      # 4=head-SHA mismatch, else fail).
      _AM_RC=0
      AUTO_MERGE_OUTPUT=$(forge_cmd_perm_safe loom-daemon forge auto-merge "$PR_NUMBER" --method squash --expected-head-sha "$MERGE_PRECONDITION_SHA" 2>&1) || _AM_RC=$?
      if [[ $_AM_RC -eq 0 ]]; then
        AUTO_MERGE_OK=true
        break
      elif [[ $_AM_RC -eq 4 ]]; then
        # Native path detected a head-SHA mismatch (#5589) — same "re-queue,
        # stale approval" signal as the shell path's
        # _is_head_mismatch_response() check further down; do not fall
        # through to the generic failure/retry branch.
        # Fetch current head SHA for diagnostic output (degrade gracefully on fetch failure)
        _CURRENT_HEAD_SHA=""
        _CHR_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
        _CURRENT_HEAD_SHA="$(echo "$_CHR_JSON" | jq -r '.head.sha // empty' 2>/dev/null || echo '')"
        unset _CHR_JSON
        error_head_moved "PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT" "$MERGE_PRECONDITION_SHA" "$_CURRENT_HEAD_SHA"
      elif [[ $_AM_RC -ne 3 ]]; then
        # Native attempted and failed (not a Gitea decline) — keep the gh error
        # in AUTO_MERGE_OUTPUT and fall through to the recheck/retry logic.
        _AM_DECLINED=false
      fi
    fi
    if [[ "$_AM_DECLINED" == true ]]; then
      # loom-daemon absent, or it declined (e.g. Gitea) — shell-based
      # forge_auto_merge carries the poll-and-merge for both forges.
      if AUTO_MERGE_OUTPUT=$(forge_auto_merge "$REPO_NWO" "$PR_NUMBER" "$MERGE_PRECONDITION_SHA" 2>&1); then
        AUTO_MERGE_OK=true
        break
      fi
    fi

    # Check if PR merged despite error (concurrent merge by another shepherd)
    RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
    RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
    if [[ "$RECHECK" == "true" ]]; then
      warning "Auto-merge reported error but PR is already merged (race condition)"
      AUTO_MERGE_OK=true
      break
    fi

    # Head-SHA-mismatch (#5579): the PR's OWN head branch moved past
    # $MERGE_PRECONDITION_SHA — distinct from "Base branch was modified"
    # below (that means the BASE fell behind; this means the branch we're
    # trying to merge changed). Do NOT retry-and-merge: exit 3 so the caller
    # (Champion) re-queues this PR for a fresh pass instead of treating it as
    # a failure. See error_head_moved()/_is_head_mismatch_response() above.
    if _is_head_mismatch_response "$AUTO_MERGE_OUTPUT"; then
      # Fetch current head SHA for diagnostic output (degrade gracefully on fetch failure)
      _CURRENT_HEAD_SHA=""
      _CHR_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
      _CURRENT_HEAD_SHA="$(echo "$_CHR_JSON" | jq -r '.head.sha // empty' 2>/dev/null || echo '')"
      unset _CHR_JSON
      error_head_moved "PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT" "$MERGE_PRECONDITION_SHA" "$_CURRENT_HEAD_SHA"
    fi

    # Retry on stale-branch race ("Base branch was modified")
    if echo "$AUTO_MERGE_OUTPUT" | grep -q "Base branch was modified"; then
      if [[ $MERGE_ATTEMPT -lt $MAX_MERGE_RETRIES ]]; then
        info "Branch is behind base branch, updating... (attempt $MERGE_ATTEMPT/$MAX_MERGE_RETRIES)"
        forge_update_branch "$REPO_NWO" "$PR_NUMBER" 2>/dev/null || \
          warning "Failed to update branch (continuing anyway)"
        info "Waiting ${MERGE_RETRY_DELAY}s for branch to sync..."
        sleep "$MERGE_RETRY_DELAY"
        MERGE_RETRY_DELAY=$((MERGE_RETRY_DELAY * 2))
        continue
      fi
    fi

    # No-required-checks fallback (#3720). When the repo defines ZERO required
    # status checks, GitHub's enablePullRequestAutoMerge mutation is rejected
    # outright — there is nothing to queue the merge behind. The rejection
    # string for that case matches NEITHER the "is in clean status" NOR the
    # "is in unstable status" grep below, so it previously fell through to the
    # generic terminal error at the bottom of this loop (issue #3720: docs-only
    # PRs #4400/#4399 were UNSTABLE from non-required pending jobs and could not
    # enable auto-merge, yet a plain synchronous merge succeeded because they
    # were MERGEABLE).
    #
    # This fallback is deliberately STRING-INDEPENDENT (it never inspects
    # AUTO_MERGE_OUTPUT) and self-gating: it fires only when
    #   (1) the base branch has NO required status check contexts, AND
    #   (2) the PR is mergeable (.mergeable == true).
    # In that case an immediate synchronous merge is exactly equivalent to a
    # server-side auto-merge — there is no required check to wait for. It
    # preserves the #3664/#3486/#3678 required-check gating BY CONSTRUCTION:
    # with ANY required check present, the contexts list is non-empty and this
    # branch is skipped, leaving the UNSTABLE classifier below in charge. A
    # lookup failure (nonzero exit) fails closed (skip → preserve existing
    # behavior). We re-fetch PR state fresh because REST `.mergeable` is null
    # until GitHub computes it — the initial fetch may predate that.
    _NRC_RECHECK_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
    _NRC_BASE_REF="$(echo "$_NRC_RECHECK_JSON" | jq -r '.base.ref // empty')"
    _NRC_MERGEABLE="$(echo "$_NRC_RECHECK_JSON" | jq -r '.mergeable // empty')"
    if [[ -n "$_NRC_BASE_REF" ]] && [[ "$_NRC_MERGEABLE" == "true" ]]; then
      _NRC_REQUIRED=""
      _NRC_LOOKUP_RC=0
      _NRC_REQUIRED="$(forge_get_required_status_check_contexts "$REPO_NWO" "$_NRC_BASE_REF" "$GH" 2>/dev/null)" || _NRC_LOOKUP_RC=$?
      if [[ "$_NRC_LOOKUP_RC" -eq 0 ]] && [[ -z "$_NRC_REQUIRED" ]]; then
        info "PR #$PR_NUMBER: repo has no required status checks and PR is mergeable; falling back to immediate merge"
        unset _NRC_RECHECK_JSON _NRC_BASE_REF _NRC_MERGEABLE _NRC_REQUIRED _NRC_LOOKUP_RC 2>/dev/null || true
        AUTO_MERGE=false      # let the synchronous-merge block below run
        AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
        break
      fi
    fi
    unset _NRC_RECHECK_JSON _NRC_BASE_REF _NRC_MERGEABLE _NRC_REQUIRED _NRC_LOOKUP_RC 2>/dev/null || true

    # Repo-level "Allow auto-merge" disabled fallback (#3763). When the
    # repository's "Allow auto-merge" setting is OFF, GitHub rejects the
    # enablePullRequestAutoMerge mutation outright with
    # "Auto merge is not allowed for this repository". Unlike the CLEAN/UNSTABLE
    # rejections below (which describe the PR's own mergeStateStatus), this is a
    # STATIC, repo-level condition — no amount of polling or branch-updating will
    # change it. It also matches NEITHER the "is in clean status" NOR the
    # "is in unstable status" grep below, so before #3763 it fell through to the
    # generic terminal error at the bottom of this loop even when the PR was
    # immediately mergeable (the observed failure: a CLEAN, Judge-approved PR
    # aborting instead of merging).
    #
    # A single re-check of the PR's mergeability decides the outcome: if the PR
    # is already immediately mergeable (.mergeable == true), a synchronous merge
    # is exactly equivalent to the server-side auto-merge the caller requested,
    # so flip to the immediate-merge path. If it is NOT mergeable, preserve the
    # terminal error rather than silently bypassing a genuine merge blocker. We
    # re-fetch PR state fresh (uncached) because REST `.mergeable` is null until
    # GitHub computes it — the initial fetch may predate that. No poll loop is
    # needed here (unlike the UNSTABLE fallback): the condition is repo-static.
    if echo "$AUTO_MERGE_OUTPUT" | grep -q "Auto merge is not allowed for this repository"; then
      _AMD_RECHECK_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
      _AMD_MERGEABLE="$(echo "$_AMD_RECHECK_JSON" | jq -r '.mergeable // empty')"
      if [[ "$_AMD_MERGEABLE" == "true" ]]; then
        info "PR #$PR_NUMBER: repo-level auto-merge is disabled but PR is mergeable; falling back to immediate merge"
        unset _AMD_RECHECK_JSON _AMD_MERGEABLE 2>/dev/null || true
        AUTO_MERGE=false      # let the synchronous-merge block below run
        AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
        break
      fi
      unset _AMD_RECHECK_JSON _AMD_MERGEABLE 2>/dev/null || true
      # Not immediately mergeable — preserve the terminal error (do NOT bypass a
      # genuine merge blocker just because auto-merge happens to be disabled).
      error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
    fi

    # GraphQL-layer unavailability fallback (#4447). GitHub's
    # enablePullRequestAutoMerge mutation is GraphQL-only; when the shared
    # credential's GraphQL quota is exhausted (or the native `loom-daemon`
    # path cannot resolve the repo NWO via `gh repo view`, which is itself a
    # GraphQL call), the mutation never has a chance to run. Unlike #3763
    # (a permanent repo-level setting), this is a TRANSIENT environmental
    # condition — REST (used by `forge_get_pr_nocache`, `forge_get_check_runs`,
    # `forge_get_required_status_check_contexts`, and the synchronous merge
    # itself) has a separate quota and typically still has headroom. It
    # matches NEITHER the "is in clean status" NOR the "is in unstable status"
    # grep below, so before this fix it fell through to the generic terminal
    # error even when a plain synchronous REST merge would have succeeded
    # immediately (the observed failure: `could not resolve repository NWO`
    # under GraphQL quota exhaustion, 0/5000 remaining while REST had ~4000
    # left).
    #
    # As with #3763, recheck mergeability first (immediate merge if already
    # mergeable). If not yet mergeable (checks still running / `.mergeable`
    # not yet computed), degrade to the same #3820 wait-for-checks-then-merge
    # path used for repo-level auto-merge-disabled — its helpers are REST-only
    # and do not depend on the exhausted GraphQL quota. Do NOT re-attempt the
    # native/shell auto-merge mutation itself; it is the same GraphQL call and
    # will fail identically.
    if echo "$AUTO_MERGE_OUTPUT" | grep -Eq "API rate limit|rate limit exceeded|RATE_LIMITED|was submitted too quickly|could not resolve repository NWO"; then
      info "PR #$PR_NUMBER: auto-merge enablement unavailable (GraphQL rate limit) — degrading to immediate/wait merge"
      _RLF_RECHECK_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
      _RLF_MERGEABLE="$(echo "$_RLF_RECHECK_JSON" | jq -r '.mergeable // empty')"
      unset _RLF_RECHECK_JSON 2>/dev/null || true
      if [[ "$_RLF_MERGEABLE" == "true" ]]; then
        unset _RLF_MERGEABLE 2>/dev/null || true
        AUTO_MERGE=false      # let the synchronous-merge block below run
        AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
        break
      fi
      unset _RLF_MERGEABLE 2>/dev/null || true
      # Not yet mergeable — reuse the #3820 REST-only wait path. It either
      # returns 0 (safe to proceed to the synchronous merge) or error()s out
      # terminally (a required check genuinely failed, or the wait timed out).
      _wait_for_checks_then_sync_merge
      AUTO_MERGE=false      # let the synchronous-merge block below run
      AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
      break
    fi

    # PR is already CLEAN — GitHub's enablePullRequestAutoMerge mutation rejects
    # this state with "Pull request Pull request is in clean status" (the
    # doubled-word prefix is from GitHub's GraphQL error formatter). Match on
    # the unique substring to stay robust against future normalization. Fall
    # through to the synchronous-merge path below instead of erroring. See #3371.
    if echo "$AUTO_MERGE_OUTPUT" | grep -q "is in clean status"; then
      info "PR #$PR_NUMBER is already CLEAN; falling back to immediate merge"
      AUTO_MERGE=false      # let the synchronous-merge block at ~line 364 run
      AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
      break
    fi

    # PR is UNSTABLE — GitHub's enablePullRequestAutoMerge mutation rejects this
    # state with "Pull request Pull request is in unstable status". GitHub emits
    # the SAME string whether the rollup is red (a check FAILED) or merely yellow
    # (checks still QUEUED/IN_PROGRESS). We resolve the PR's head-SHA check-runs
    # and distinguish, in precedence order:
    #
    #   (a) A required check has genuinely FAILED  -> refuse (terminal error),
    #       without waiting out the pending timeout.
    #   (b) A check is still QUEUED/IN_PROGRESS    -> the merge state will settle
    #       (conclusion == null, so it never shows    on its own; poll until it
    #       up as "failing"). This is the #3664       resolves to (a)/(c)/CLEAN,
    #       "checks still running" case.               bounded by
    #                                                   LOOM_AUTO_MERGE_TIMEOUT.
    #   (c) Every FAILED check is informational    -> immediate-merge fallback
    #       (NOT in branch protection) and nothing     (#3486, unchanged).
    #       is pending.
    #   (d) Nothing failed, nothing pending, and   -> genuine "unknown gap"
    #       we never observed a pending check          (e.g. commit-status, not
    #       (e.g. commit-status failures the           check-run, failures) ->
    #       check-runs API omits).                     refuse (terminal),
    #                                                   preserving the #3486
    #                                                   defensive hard-error.
    #
    # Once the checks we waited on all pass, the PR is effectively CLEAN and we
    # fall through to immediate merge (mirroring the CLEAN-fallback above).
    # Sibling of the CLEAN-fallback above. See #3371, #3486, #3664.
    if echo "$AUTO_MERGE_OUTPUT" | grep -q "is in unstable status"; then
      _UNSTABLE_HEAD_SHA="$(echo "$PR_JSON" | jq -r '.head.sha // empty')"
      _UNSTABLE_BASE_REF="$(echo "$PR_JSON" | jq -r '.base.ref // empty')"
      if [[ -z "$_UNSTABLE_HEAD_SHA" ]] || [[ -z "$_UNSTABLE_BASE_REF" ]]; then
        # Can't make a safe decision without the head SHA and base ref — fall
        # through to the existing refusal.
        error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
      fi

      _UNSTABLE_FALLBACK_TO_MERGE=false
      _UNSTABLE_OBSERVED_PENDING=false
      _UNSTABLE_DEADLINE=$(( $(date +%s) + LOOM_AUTO_MERGE_TIMEOUT ))

      while true; do
        # Fetch the check-runs rollup, capturing the helper's own exit status
        # separately from the JSON payload. A transient fetch failure (network
        # blip, 5xx, Gitea `return 1`) must NOT be collapsed into the same
        # `{"check_runs":[]}` shape a legitimately empty rollup produces —
        # doing so lets a fetch error masquerade as "no failing, no pending"
        # and, once a pending check has been observed, take the resolved-green
        # immediate-merge branch on a commit whose real check state is unknown
        # (#3678). Retry once to absorb a single blip, then route a persistent
        # failure into the SAME bounded pending-wait path used by branch (b)
        # below so the LOOM_AUTO_MERGE_TIMEOUT bound still applies.
        _UNSTABLE_FETCH_RC=0
        _UNSTABLE_FAILING_RAW="$(forge_get_check_runs "$REPO_NWO" "$_UNSTABLE_HEAD_SHA" 2>/dev/null)" || _UNSTABLE_FETCH_RC=$?
        if [[ "$_UNSTABLE_FETCH_RC" -ne 0 ]]; then
          _UNSTABLE_FETCH_RC=0
          _UNSTABLE_FAILING_RAW="$(forge_get_check_runs "$REPO_NWO" "$_UNSTABLE_HEAD_SHA" 2>/dev/null)" || _UNSTABLE_FETCH_RC=$?
        fi
        if [[ "$_UNSTABLE_FETCH_RC" -ne 0 ]]; then
          # Fetch is failing (twice). Treat as still-pending and keep polling,
          # reusing the (b) branch's merged-concurrently recheck + deadline
          # guard so this never bypasses the bounded-wait/timeout semantics.
          warning "Failed to fetch check-runs for PR #$PR_NUMBER (rc=$_UNSTABLE_FETCH_RC); treating as still-pending and continuing to poll"
          _UNSTABLE_RECHECK_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
          if [[ "$(echo "$_UNSTABLE_RECHECK_JSON" | jq -r '.merged // false')" == "true" ]]; then
            warning "PR #$PR_NUMBER merged by another process while waiting for checks"
            AUTO_MERGE_OK=true
            break
          fi
          if [[ "$(date +%s)" -ge "$_UNSTABLE_DEADLINE" ]]; then
            error "Timed out after ${LOOM_AUTO_MERGE_TIMEOUT}s waiting for check-runs to become fetchable for PR #$PR_NUMBER (last fetch rc=$_UNSTABLE_FETCH_RC). Re-run the merge once the forge API is healthy, or raise LOOM_AUTO_MERGE_TIMEOUT."
          fi
          info "PR #$PR_NUMBER is UNSTABLE: check-runs fetch failing (rc=$_UNSTABLE_FETCH_RC); waiting ${LOOM_AUTO_MERGE_POLL_INTERVAL}s for the forge API (timeout ${LOOM_AUTO_MERGE_TIMEOUT}s)..."
          sleep "$LOOM_AUTO_MERGE_POLL_INTERVAL"
          continue
        fi
        # Names of failing check runs (terminal non-success conclusions).
        # Sort + uniq to dedupe re-runs with the same context.
        _UNSTABLE_FAILING="$(echo "$_UNSTABLE_FAILING_RAW" | \
          jq -r '[.check_runs[] | select(.conclusion == "failure" or .conclusion == "timed_out" or .conclusion == "cancelled" or .conclusion == "action_required") | .name] | unique | .[]' 2>/dev/null || true)"
        # Names of checks that are still running (queued or in_progress → not
        # yet completed, conclusion == null). These never appear in
        # _UNSTABLE_FAILING; they are the #3664 "still running" case.
        _UNSTABLE_PENDING="$(echo "$_UNSTABLE_FAILING_RAW" | \
          jq -r '[.check_runs[] | select(.status != "completed") | .name] | unique | .[]' 2>/dev/null || true)"

        if [[ -n "$_UNSTABLE_FAILING" ]]; then
          # Some check FAILED — classify against branch protection. A nonzero
          # exit from the helper signals a lookup failure (Gitea 5xx, network
          # error, missing token, unknown forge) — fail closed and refuse.
          _UNSTABLE_REQUIRED=""
          _UNSTABLE_LOOKUP_RC=0
          _UNSTABLE_REQUIRED="$(forge_get_required_status_check_contexts "$REPO_NWO" "$_UNSTABLE_BASE_REF" "$GH" 2>/dev/null)" || _UNSTABLE_LOOKUP_RC=$?
          if [[ "$_UNSTABLE_LOOKUP_RC" -ne 0 ]]; then
            warning "Failed to resolve required status checks for $_UNSTABLE_BASE_REF (rc=$_UNSTABLE_LOOKUP_RC); preserving UNSTABLE refusal"
            error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
          fi

          # Set difference: failing_checks \ required_contexts (informational)
          # and failing_checks ∩ required_contexts (overlap).
          _UNSTABLE_INFORMATIONAL="$(comm -23 \
            <(printf '%s\n' "$_UNSTABLE_FAILING" | sort -u) \
            <(printf '%s\n' "$_UNSTABLE_REQUIRED" | sort -u))"
          _UNSTABLE_OVERLAP="$(comm -12 \
            <(printf '%s\n' "$_UNSTABLE_FAILING" | sort -u) \
            <(printf '%s\n' "$_UNSTABLE_REQUIRED" | sort -u))"

          if [[ -n "$_UNSTABLE_OVERLAP" ]]; then
            # (a) A branch-protection-required check has failed. The PR can
            # never merge on this SHA — refuse now, without waiting on any
            # still-pending checks.
            error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
          fi

          if [[ -z "$_UNSTABLE_PENDING" ]]; then
            # (c) Every failing check is informational and nothing is pending.
            # Log the names, then fall through to the synchronous-merge path.
            _UNSTABLE_COUNT="$(printf '%s\n' "$_UNSTABLE_INFORMATIONAL" | wc -l | tr -d ' ')"
            info "Falling back to immediate merge: ${_UNSTABLE_COUNT} informational check(s) failing (not in branch protection):"
            printf '%s\n' "$_UNSTABLE_INFORMATIONAL" | while IFS= read -r _ctx; do
              [[ -n "$_ctx" ]] && info "    - $_ctx"
            done
            _UNSTABLE_FALLBACK_TO_MERGE=true
            break
          fi
          # Informational failures but other checks are still running — don't
          # merge until everything settles. Fall through to the pending wait.
        fi

        if [[ -n "$_UNSTABLE_PENDING" ]]; then
          # (b) Checks still running. Wait, bounded by LOOM_AUTO_MERGE_TIMEOUT.
          _UNSTABLE_OBSERVED_PENDING=true

          # A concurrent merger may have completed the PR while we waited.
          _UNSTABLE_RECHECK_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
          if [[ "$(echo "$_UNSTABLE_RECHECK_JSON" | jq -r '.merged // false')" == "true" ]]; then
            warning "PR #$PR_NUMBER merged by another process while waiting for checks"
            AUTO_MERGE_OK=true
            break
          fi

          if [[ "$(date +%s)" -ge "$_UNSTABLE_DEADLINE" ]]; then
            _UNSTABLE_PENDING_COUNT="$(printf '%s\n' "$_UNSTABLE_PENDING" | wc -l | tr -d ' ')"
            error "Timed out after ${LOOM_AUTO_MERGE_TIMEOUT}s waiting for ${_UNSTABLE_PENDING_COUNT} pending check(s) on PR #$PR_NUMBER to complete (still queued/in_progress). Re-run the merge once CI settles, or raise LOOM_AUTO_MERGE_TIMEOUT."
          fi

          _UNSTABLE_PENDING_COUNT="$(printf '%s\n' "$_UNSTABLE_PENDING" | wc -l | tr -d ' ')"
          info "PR #$PR_NUMBER is UNSTABLE: ${_UNSTABLE_PENDING_COUNT} check(s) still running; waiting ${LOOM_AUTO_MERGE_POLL_INTERVAL}s for CI (timeout ${LOOM_AUTO_MERGE_TIMEOUT}s)..."
          sleep "$LOOM_AUTO_MERGE_POLL_INTERVAL"
          continue
        fi

        # Nothing failing, nothing pending.
        if [[ "$_UNSTABLE_OBSERVED_PENDING" == "true" ]]; then
          # The checks we waited on all resolved green — the PR is now
          # effectively CLEAN. Fall through to immediate merge.
          info "PR #$PR_NUMBER checks resolved green; falling back to immediate merge"
          _UNSTABLE_FALLBACK_TO_MERGE=true
          break
        fi
        # (d) Never observed a pending check and none failed — a transient API
        # gap or commit-status (vs check-run) failure the check-runs API omits.
        # Be safe and keep the existing #3486 defensive error path.
        error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
      done

      unset _UNSTABLE_HEAD_SHA _UNSTABLE_BASE_REF _UNSTABLE_FAILING_RAW \
        _UNSTABLE_FAILING _UNSTABLE_PENDING _UNSTABLE_REQUIRED \
        _UNSTABLE_INFORMATIONAL _UNSTABLE_OVERLAP _UNSTABLE_COUNT \
        _UNSTABLE_PENDING_COUNT _UNSTABLE_DEADLINE _UNSTABLE_RECHECK_JSON \
        _UNSTABLE_LOOKUP_RC _UNSTABLE_FETCH_RC _UNSTABLE_OBSERVED_PENDING 2>/dev/null || true

      if [[ "$_UNSTABLE_FALLBACK_TO_MERGE" == "true" ]]; then
        unset _UNSTABLE_FALLBACK_TO_MERGE
        AUTO_MERGE=false      # let the synchronous-merge block below run
        AUTO_MERGE_OK=true    # bypass the post-loop "after N attempts" guard
        break                 # exit the outer MERGE_ATTEMPT for-loop
      fi
      unset _UNSTABLE_FALLBACK_TO_MERGE

      # The wait loop set AUTO_MERGE_OK=true only if the PR merged concurrently;
      # break the outer loop to reach the shared cleanup block.
      if [[ "$AUTO_MERGE_OK" == "true" ]]; then
        break
      fi
    fi

    # Other auto-merge errors — fail immediately (no retry would help)
    error "Failed to enable auto-merge for PR #$PR_NUMBER: $AUTO_MERGE_OUTPUT"
  done

  if [[ "$AUTO_MERGE_OK" != "true" ]]; then
    error "Failed to enable auto-merge for PR #$PR_NUMBER after $MAX_MERGE_RETRIES attempts"
  fi

  # If the CLEAN-status fall-through fired above, AUTO_MERGE has been flipped
  # to false. Skip the "Auto-merge enabled" success message and the post-auto
  # state poll — let the synchronous-merge block at ~line 376 take over.
  if [[ "$AUTO_MERGE" == "true" ]]; then
    success "Auto-merge enabled for PR #$PR_NUMBER"

    # Check whether the server-side merge has already completed. GitHub
    # auto-merge queues until checks pass, so on most PRs this is still
    # false right after enabling. If merged, fall through to the shared
    # cleanup block below. Otherwise skip cleanup — loom-clean will
    # handle the stale worktree later.
    POST_AUTO_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
    POST_AUTO_MERGED=$(echo "$POST_AUTO_JSON" | jq -r '.merged // false')
    if [[ "$POST_AUTO_MERGED" != "true" ]]; then
      info "Auto-merge queued (server-side merge pending checks); skipping local cleanup"
      info "Run loom-clean later to remove the worktree once GitHub completes the merge"
      exit 0
    fi
    info "PR #$PR_NUMBER already merged server-side; running cleanup"
    # Fall through to the shared cleanup block (branch deletion + worktree).
  fi
fi

# Synchronous-merge path. Skipped when --auto already succeeded server-side
# (in which case we fall through to the shared cleanup block below).
if [[ "$AUTO_MERGE" != "true" ]]; then

# Check mergeability (#6104). REST `.mergeable` is computed asynchronously and
# invalidated on every push to the base branch — on a fast-moving repo it can
# read a stale `false` for a PR that would actually merge cleanly. Before
# refusing outright, re-query (uncached) after a short backoff, and if it's
# still `false`, corroborate with a local `git merge-tree` check so the
# refusal message can distinguish "the forge's cached state is stale/unknown"
# from "this branch genuinely conflicts" — see _recheck_mergeable_before_refusal().
if [[ "$PR_MERGEABLE" == "false" ]]; then
  _MSM_BASE_REF="$(echo "$PR_JSON" | jq -r '.base.ref // empty')"
  _MSM_RETRIES="${LOOM_MERGEABLE_RECHECK_RETRIES:-3}"
  _MSM_DELAY="${LOOM_MERGEABLE_RECHECK_DELAY:-3}"
  _MSM_DECISION="$(_recheck_mergeable_before_refusal "$REPO_NWO" "$PR_NUMBER" "$GH" \
    "$_MSM_BASE_REF" "$PR_BRANCH" "$REPO_ROOT" \
    "$_MSM_RETRIES" "$_MSM_DELAY")"
  _MSM_ACTION="${_MSM_DECISION%%:*}"
  _MSM_REASON="${_MSM_DECISION#*:}"

  # Every non-early-return path through _recheck_mergeable_before_refusal
  # consumes exactly $_MSM_RETRIES attempts, EXCEPT the one where the recheck
  # resolves mergeable=true mid-loop (at attempt N < retries) -- that path's
  # reason string embeds "recheck #N" (see the function's own echo above), so
  # parse it out for a durable "how many backoff attempts did this actually
  # cost" telemetry field rather than always reporting the configured max.
  # This reads the already-existing decision text; it does not change the
  # recheck's decision logic in any way (#6978, AC4).
  _MSM_RETRIES_USED="$_MSM_RETRIES"
  if [[ "$_MSM_REASON" =~ recheck\ \#([0-9]+) ]]; then
    _MSM_RETRIES_USED="${BASH_REMATCH[1]}"
  fi

  # Durable telemetry (#6978, follow-up from #6156): emit one
  # merge.admission_recheck record per invocation, in addition to the
  # existing info/error stdout messages below. Best-effort only — a failure
  # here (unwritable log dir, missing jq, etc.) must never abort the merge
  # path itself, so it is fully isolated with `|| true` and its own output
  # is discarded. This does not change the decision computed above at all.
  "$SCRIPT_DIR/merge-admission-telemetry.sh" record \
    --repo "$REPO_NWO" --pr "$PR_NUMBER" --action "$_MSM_ACTION" --reason "$_MSM_REASON" \
    --retries-used "$_MSM_RETRIES_USED" --backoff-delay-sec "$_MSM_DELAY" \
    >/dev/null 2>&1 || true

  case "$_MSM_ACTION" in
    merge)
      info "PR #$PR_NUMBER: $_MSM_REASON"
      ;;
    refuse-conflict)
      error "PR #$PR_NUMBER has merge conflicts — resolve before merging ($_MSM_REASON)"
      ;;
    *)
      error "PR #$PR_NUMBER has merge conflicts — resolve before merging (forge's cached mergeable state is stale/unknown and could not be corroborated locally: $_MSM_REASON)"
      ;;
  esac
  unset _MSM_BASE_REF _MSM_RETRIES _MSM_DELAY _MSM_DECISION _MSM_ACTION _MSM_REASON _MSM_RETRIES_USED 2>/dev/null || true
fi

if [[ "$DRY_RUN" == "true" ]]; then
  info "[dry-run] Would merge PR #$PR_NUMBER (squash) and delete remote branch '$PR_BRANCH'"
  if [[ "$CLEANUP_WORKTREE" == "true" ]]; then
    info "[dry-run] Would clean up local worktree"
    if git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$PR_BRANCH"; then
      info "[dry-run] Would delete local branch '$PR_BRANCH'"
    fi
  else
    info "[dry-run] --no-cleanup-worktree: would leave local worktree and local branch '$PR_BRANCH' in place"
  fi
  exit 0
fi

# Merge via API (squash) with retry for stale branch
MAX_MERGE_RETRIES=3
MERGE_RETRY_DELAY=5

for MERGE_ATTEMPT in $(seq 1 $MAX_MERGE_RETRIES); do
  MERGE_RESPONSE=$(forge_merge_pr "$REPO_NWO" "$PR_NUMBER" "$MERGE_PRECONDITION_SHA" 2>&1) && break  # Success, exit loop

  # Check if it merged despite error (race condition)
  RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
  RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
  if [[ "$RECHECK" == "true" ]]; then
    warning "Merge reported error but PR is merged (race condition)"
    break
  fi

  # Check for "Merge already in progress" (HTTP 405)
  # This happens when auto-merge triggers at the same time as our merge attempt
  if echo "$MERGE_RESPONSE" | grep -q "Merge already in progress"; then
    info "Merge already in progress (HTTP 405), waiting for completion..."
    sleep 5
    RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
    RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
    if [[ "$RECHECK" == "true" ]]; then
      success "PR #$PR_NUMBER merged (concurrent merge completed)"
      break
    fi
    # Still not merged after wait - continue retry loop
    warning "Concurrent merge not yet complete, retrying..."
    continue
  fi

  # Head-SHA-mismatch (#5579): the PR's OWN head branch moved past
  # $MERGE_PRECONDITION_SHA — distinct from "Base branch was modified" below
  # (that means the BASE fell behind; this means the branch we're trying to
  # merge changed, most commonly a session pushing new commits mid-merge). Do
  # NOT retry-and-merge: retrying would either fail again (session still
  # pushing) or silently squash a different diff than the one Judge approved.
  # Exit 3 so the caller (Champion) re-queues instead of treating this as a
  # failure. See error_head_moved()/_is_head_mismatch_response() above.
  if _is_head_mismatch_response "$MERGE_RESPONSE"; then
    # Fetch current head SHA for diagnostic output (degrade gracefully on fetch failure)
    _CURRENT_HEAD_SHA=""
    _CHR_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
    _CURRENT_HEAD_SHA="$(echo "$_CHR_JSON" | jq -r '.head.sha // empty' 2>/dev/null || echo '')"
    unset _CHR_JSON
    error_head_moved "PR #$PR_NUMBER: $MERGE_RESPONSE" "$MERGE_PRECONDITION_SHA" "$_CURRENT_HEAD_SHA"
  fi

  # Check for stale branch error (base branch was modified)
  if echo "$MERGE_RESPONSE" | grep -q "Base branch was modified"; then
    if [[ $MERGE_ATTEMPT -lt $MAX_MERGE_RETRIES ]]; then
      info "Branch is behind base branch, updating... (attempt $MERGE_ATTEMPT/$MAX_MERGE_RETRIES)"

      # Update branch via forge API
      UPDATE_RESPONSE=$(forge_update_branch "$REPO_NWO" "$PR_NUMBER" 2>&1) || {
        warning "Failed to update branch: $UPDATE_RESPONSE"
        # Continue to retry merge anyway - update may have partially succeeded
      }

      # Wait for branch to sync
      info "Waiting ${MERGE_RETRY_DELAY}s for branch to sync..."
      sleep "$MERGE_RETRY_DELAY"

      # Increase delay for next attempt (exponential backoff)
      MERGE_RETRY_DELAY=$((MERGE_RETRY_DELAY * 2))
      continue
    else
      error "Failed to merge PR #$PR_NUMBER after $MAX_MERGE_RETRIES attempts: Branch remains behind base branch"
    fi
  fi

  # Other merge errors - fail immediately
  error "Failed to merge PR #$PR_NUMBER: $MERGE_RESPONSE"
done

# Verify merge
VERIFY_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
VERIFY_MERGED=$(echo "$VERIFY_JSON" | jq -r '.merged // false')
if [[ "$VERIFY_MERGED" != "true" ]]; then
  # Defense-in-depth: a transient API error (empty/{} response) must not turn a
  # successful merge into a hard failure. Retry the verify once before failing
  # (issue #3547).
  sleep 2
  VERIFY_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
  VERIFY_MERGED=$(echo "$VERIFY_JSON" | jq -r '.merged // false')
  if [[ "$VERIFY_MERGED" != "true" ]]; then
    error "Merge API call returned but PR #$PR_NUMBER is not merged"
  fi
fi

success "PR #$PR_NUMBER merged successfully"

fi  # end synchronous-merge path (AUTO_MERGE != "true")

# Partial-increment label reset (#3667). Runs only after a confirmed merge (both
# the synchronous path above and the auto-merge server-side-completed fall-
# through reach here; the auto-merge-queued and dry-run paths exit earlier).
# Best-effort — never fails the merge. See the function definitions above.
_reset_partial_increment_labels || true

# Closed-issue `loom:building` cleanup (#6199). Runs right after the partial-
# increment pass (so a #4569 revert of a premature auto-close has already
# happened, and that issue is correctly skipped here — see the function's own
# header comment above) and at the same confirmed-merge choke point.
# Best-effort — never fails the merge.
_strip_closed_issue_building_labels || true

# Automated stacked-PR reconciliation (#3747, stacked-PR v2 item 1). Runs at the
# same confirmed-merge choke point, and BEFORE branch deletion below so the
# parent branch ref still resolves as reconcile-stack.sh's rebase <upstream>
# argument. Best-effort — never fails the merge. See the function above.
_auto_reconcile_stacked_children || true

# NOTE: Full label cleanup on linked issues remains intentionally skipped for
# the `Closes #N` / `Fixes #N` / `Resolves #N` auto-close case — MOST labels
# on a closed issue are harmless, since every queue-driving agent filters on
# open state. See: https://github.com/rjwalters/loom/issues/2838
#
# EXCEPTION 1 (#3667): non-closing `Part of #N` / `Contributes to #N` partial-
# increment references leave the referenced issue OPEN after merge, so its
# `loom:building` label would otherwise be orphaned. The
# _reset_partial_increment_labels call above handles exactly that case by
# swapping loom:building -> loom:issue on the still-open referenced issue.
#
# EXCEPTION 2 (#6199): `loom:building` specifically — unlike other labels — is
# read by some consumers (dashboards, capacity checks, manual spot-checks) as
# meaning "in flight" WITHOUT also filtering on issue state, so a stale claim
# left on a just-closed issue silently becomes noise rather than staying truly
# harmless. The _strip_closed_issue_building_labels call above removes it from
# each issue THIS merge closed (via `Closes`/`Fixes`/`Resolves`). #2838's core
# reasoning — "don't bother cleaning up labels on close" — still holds for
# every other label, and for issues closed by means other than a merge (see
# that function's header for the recorded scope decision).
#
# NOTE: This script does NOT close linked issues. Issue auto-close is GitHub's
# responsibility — GitHub's PR parser closes issues referenced via `Closes #N`,
# `Fixes #N`, `Resolves #N` (and the case/tense variants) on merge. Champion's
# "Verify Issue Auto-Close" step is a belt-and-suspenders check that uses
# `forge_pr_close_targets` (which delegates to GitHub's GraphQL
# `closingIssuesReferences` field) to confirm closure. If you are debugging
# why an unintended issue was closed, look at the PR body and Champion logs,
# not at this script. See: https://github.com/rjwalters/loom/issues/3267

# Delete remote branch (skip if forge auto-deletes on merge)
DELETE_BRANCH_ON_MERGE=$(forge_check_auto_delete "$REPO_NWO" "$GH")
if [[ "$DELETE_BRANCH_ON_MERGE" == "true" ]]; then
  info "Skipping remote branch deletion (auto-delete is enabled)"
else
  info "Deleting remote branch: $PR_BRANCH"
  forge_delete_branch "$REPO_NWO" "$PR_BRANCH" && \
    success "Remote branch '$PR_BRANCH' deleted" || \
    warning "Could not delete remote branch '$PR_BRANCH' (may already be deleted)"
fi

# Cleanup worktree if requested.
#
# Ownership model (see issue #3334): Loom owns worktrees it created under
# .loom/worktrees/ (marked with a .loom-managed sentinel file by worktree.sh
# or pr-worktree.sh). Any worktree lacking the sentinel is treated as
# user-owned and is never removed by this script. Operators can also set
# LOOM_PRESERVE_WORKTREE=1 to skip cleanup unconditionally.
#
# Two worktree-path conventions are recognized:
#   - .loom/worktrees/issue-<N>/  (Loom-issue branches: feature/issue-<N>;
#     the Builder's worktree)
#   - .loom/worktrees/pr-<N>/     (external-fork / ad-hoc branches, #3358;
#     OR a Judge/Doctor review worktree for an ordinary feature/issue-<N>
#     branch created via pr-worktree.sh when no builder issue-<N> worktree
#     was present at review time, #6264)
#
# A pr-<N> worktree of the LATTER shape can exist ALONGSIDE an issue-<N>
# worktree for the same PR (the builder worktree is created/reused after the
# Judge's pr-<N> review worktree, or vice versa) — the merge-time cleanup
# below checks for both when PR_BRANCH matches feature/issue-<N>, not just
# the issue-<N> path (#6264: previously only the external-fork branch ever
# considered a pr-<N> path, so a co-existing Judge review worktree on an
# ordinary issue branch was never cleaned up and survived the merge).
#
# Branch-to-issue regex is the strict `^feature/issue-([0-9]+)$` pattern so
# branches like `release-1` or `fix-bug-42` correctly classify as PR-style
# (not issue-style) and clean up the right worktree.
# Look up the branch attached to a worktree via porcelain. Prints the branch
# short-name (without refs/heads/ prefix) on stdout. Returns 0 with empty
# output for detached / bare worktrees (no branch line in the stanza).
_worktree_branch_for() {
  local target="$1" target_abs
  target_abs="$(cd "$target" 2>/dev/null && pwd -P)" || target_abs="$target"
  # The `worktree ` path line (prefix = 9 chars) may contain spaces, so parse
  # it with substr($0, 10) rather than $2 (which truncates at the first space).
  # The `branch ` line is safe with $2 — git ref names cannot contain spaces.
  # Caveat: a path with a literal newline would still break this line-oriented
  # parse; `--porcelain -z` would be needed for full robustness (#3717).
  git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | \
    awk -v p="$target_abs" '
      /^worktree / { wt=substr($0, 10); br=""; next }
      /^branch /   { br=$2 }
      /^$/         { if (wt == p && br != "" && !found) { sub(/^refs\/heads\//, "", br); print br; found=1; exit } }
      END          { if (wt == p && br != "" && !found) { sub(/^refs\/heads\//, "", br); print br } }
    '
}

# Print the absolute path of the PRIMARY (main) worktree — the FIRST `worktree`
# entry of `git worktree list --porcelain`. Git always lists the main working
# tree first, so `exit` after the first match is correct. Prints nothing on
# error (e.g. not a git repo). Used by _remove_loom_worktree to hard-refuse
# removing the primary checkout (#3710).
_primary_worktree_path() {
  # Parse the path via substr($0, 10) (strip the literal `worktree ` prefix, 9
  # chars) so a primary checkout under a space-containing path is not truncated
  # at the first space. Newline-in-path caveat: see _worktree_branch_for (#3717).
  git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | \
    awk '/^worktree / { print substr($0, 10); exit }'
}

# _is_primary_worktree_path <path>
#
# True (rc 0) when <path> resolves to the same real path as the PRIMARY (main)
# working copy — the FIRST entry of `git worktree list --porcelain` — rather
# than a linked worktree. Used to distinguish "this is the main checkout, not
# a removable worktree at all" from "this is a genuine linked worktree" so
# callers never suggest `git worktree remove` / `--worktree-path` against the
# primary checkout (#4171). Returns 1 (false) if either path fails to resolve.
_is_primary_worktree_path() {
  local check_path="$1" check_real primary_real
  check_real="$(cd "$check_path" 2>/dev/null && pwd -P)" || check_real="$check_path"
  primary_real="$(_primary_worktree_path)"
  [[ -n "$primary_real" ]] && [[ "$check_real" == "$primary_real" ]]
}

# Walk porcelain output for a worktree whose branch matches the given branch
# short-name. Prints the worktree absolute path or nothing. Skips detached /
# bare entries (they have no `branch refs/heads/...` line).
_find_worktree_by_branch() {
  local want_branch="$1"
  # `worktree ` path parsed via substr($0, 10) (space-safe); `branch ` via $2
  # (ref names cannot contain spaces). Newline-in-path caveat: see
  # _worktree_branch_for (#3717).
  git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | \
    awk -v want="refs/heads/${want_branch}" '
      /^worktree / { wt=substr($0, 10); br=""; next }
      /^branch /   { br=$2 }
      /^$/         { if (br == want && !found) { print wt; found=1; exit } }
      END          { if (br == want && !found) { print wt } }
    '
}

# _worktree_branch_fully_captured <branch> <expected_head_sha>
#
# True (rc 0) when the LOCAL branch's tip commit equals expected_head_sha —
# every commit on the branch was part of the just-merged PR, so nothing on
# disk under that branch is unmerged. This is the exact criterion
# `_maybe_delete_local_branch` below already uses to safely upgrade `git
# branch -d` to `-D` after a squash merge (where `git branch --merged` never
# returns true, #4100); it is factored out here (#6694) so the
# worktree-preserve decision can reuse it too, not just the branch-delete
# safety check. `git merge-base --is-ancestor branch default` is NOT an
# equivalent substitute — it is false for a squash-merged branch even though
# every one of its commits made it into the merge — so this stays keyed on
# the merged PR's head SHA rather than default-branch ancestry.
_worktree_branch_fully_captured() {
  local branch="$1" expected_head_sha="${2:-}"
  [[ -z "$branch" || -z "$expected_head_sha" ]] && return 1
  local local_tip
  local_tip="$(git -C "$REPO_ROOT" rev-parse --verify -q "refs/heads/$branch" 2>/dev/null || echo "")"
  [[ -n "$local_tip" ]] && [[ "$local_tip" == "$expected_head_sha" ]]
}

# Delete the matching local branch (#4100).
#
# _maybe_delete_local_branch <branch> [expected_head_sha]
#
# `expected_head_sha` is optional — the merged PR's `head.sha` (already parsed
# into $PR_HEAD_SHA at the top of this script). When it is supplied AND the
# local branch tip equals it, every commit on the branch was part of the
# merged PR, so `git branch -D` (force) is safe even though the branch will
# never satisfy `git branch --merged` after a squash merge. When it is absent
# (the pre-#4100 :1455-equivalent caller below) or the tip does not match
# (unpushed local work), this falls back to the original `git branch -d`
# behaviour: Git's own "not fully merged" safety net, which keeps the branch
# and reports it rather than force-deleting.
#
# Primary-checkout auto-cleanup (#5015): when the branch turns out to be
# checked out in the repo's PRIMARY working copy rather than a removable
# worktree, and the tip-matches-head safety check above already held, and
# the primary checkout's working tree is clean with no stash entries (see
# the auto-cleanup block below for the exact gate), this checks out the
# default branch there and force-deletes the now-unreferenced branch
# instead of just printing manual instructions. Opt out with
# --no-cleanup-primary (CLEANUP_PRIMARY_CHECKOUT=false) if silently moving
# HEAD in the operator's primary checkout is unwanted.
#
# Never fails the cleanup pipeline — always returns 0, warns on errors.
_maybe_delete_local_branch() {
  local branch="$1"
  local expected_head_sha="${2:-}"
  if [[ -z "$branch" ]]; then
    return 0
  fi
  if ! git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    info "Local branch '$branch' does not exist — skipping branch delete"
    return 0
  fi
  # Never delete the repo's default branch (cheap belt-and-suspenders; the
  # merged PR's head branch should never legitimately BE the default branch,
  # but a misdetected $PR_BRANCH must not take this out).
  if [[ -n "$DEFAULT_BRANCH_NAME" && "$branch" == "$DEFAULT_BRANCH_NAME" ]] || \
     [[ "$branch" == "main" ]] || [[ "$branch" == "master" ]]; then
    warning "Refusing to delete local branch '$branch' — it is the repository's default branch"
    return 0
  fi

  local delete_flag="-d"
  local safety_note=""
  if _worktree_branch_fully_captured "$branch" "$expected_head_sha"; then
    delete_flag="-D"
    safety_note=" (tip matches merged PR head SHA — safe force-delete)"
  fi

  local delete_output
  if delete_output="$(git -C "$REPO_ROOT" branch "$delete_flag" "$branch" 2>&1)"; then
    success "Local branch '$branch' deleted$safety_note"
    return 0
  fi

  # Distinguish "checked out somewhere" (current HEAD or another worktree)
  # from a genuine "not fully merged" refusal — the former gets a specific
  # message instead of the generic unmerged-commits warning (#4100 AC #4).
  if echo "$delete_output" | grep -qiE "checked out at|is currently checked out|used by worktree"; then
    # Further distinguish WHERE it's checked out (#4171): if it's the PRIMARY
    # (main) working copy, `git worktree remove`/`--worktree-path` can never
    # apply — there is no worktree to remove, only a branch to switch away
    # from. Give the exact two-step remediation instead of the generic
    # message, which otherwise routes the operator toward worktree cleanup
    # advice that doesn't exist for the primary checkout. A genuine OTHER
    # linked worktree keeps the original generic message unchanged.
    local checkout_loc=""
    checkout_loc="$(_find_worktree_by_branch "$branch")"
    if [[ -n "$checkout_loc" ]] && _is_primary_worktree_path "$checkout_loc"; then
      local default_label="${DEFAULT_BRANCH_NAME:-<default-branch>}"

      # Auto-cleanup (#5015): the two-step remediation below (checkout the
      # default branch, then force-delete) is exactly what this script
      # already knows is safe to do itself whenever ALL of the following
      # hold — do it instead of just printing instructions:
      #   1. Not opted out via --no-cleanup-primary / CLEANUP_PRIMARY_CHECKOUT.
      #   2. The default branch actually resolved (never silently guess one).
      #   3. delete_flag == "-D" — the tip-matches-merged-head-SHA safety
      #      check above already passed, so every commit on $branch is part
      #      of the merged PR; nothing is lost by deleting it.
      #   4. The primary checkout's working tree is clean (no uncommitted
      #      changes, no staged changes) AND has no stash entries — checked
      #      HERE, immediately before the mutating `checkout`, not cached
      #      earlier, to avoid a TOCTOU gap against a concurrent process
      #      working in the same checkout.
      # A dirty tree, a present stash, an opt-out, or a tip mismatch all fall
      # straight through to the manual two-step instructions unchanged.
      if [[ "${CLEANUP_PRIMARY_CHECKOUT:-true}" == "true" ]] && \
         [[ -n "$DEFAULT_BRANCH_NAME" ]] && \
         [[ "$delete_flag" == "-D" ]] && \
         [[ -z "$(git -C "$checkout_loc" status --porcelain 2>/dev/null)" ]] && \
         [[ -z "$(git -C "$checkout_loc" stash list 2>/dev/null)" ]]; then
        if git -C "$checkout_loc" checkout -q "$DEFAULT_BRANCH_NAME" 2>/dev/null && \
           git -C "$checkout_loc" branch -D "$branch" >/dev/null 2>&1; then
          success "Local branch '$branch' deleted$safety_note"
          info "Primary checkout ($checkout_loc) was on '$branch' — automatically switched to '$DEFAULT_BRANCH_NAME' to free it up for deletion"
          return 0
        fi
        warning "Attempted to auto-clean up '$branch' in the primary checkout ($checkout_loc) but the checkout or delete failed — falling back to manual instructions"
      fi

      warning "Could not delete local branch '$branch' — it is checked out in the primary repository checkout ($checkout_loc)."
      warning "To clean it up: git -C '$checkout_loc' checkout $default_label && git -C '$checkout_loc' branch -D $branch"
    else
      warning "Could not delete local branch '$branch' — it is checked out (current HEAD or another worktree)"
    fi
  elif [[ "$delete_flag" == "-d" ]]; then
    warning "Could not delete local branch '$branch' (may have unpushed commits — use 'git branch -D $branch' if intentional)"
  else
    warning "Could not delete local branch '$branch': $delete_output"
  fi
  return 0
}

# _remove_loom_worktree <path> [allow_unmanaged]
#
# When allow_unmanaged is "true" (only set by the --worktree-path code path),
# the .loom-managed sentinel check is skipped — the caller has taken explicit
# responsibility for the cleanup decision. The default (no second arg, or
# "false") preserves the original sentinel guard.
_remove_loom_worktree() {
  local worktree_path="$1"
  local allow_unmanaged="${2:-false}"
  if [[ ! -d "$worktree_path" ]]; then
    info "No worktree found at $worktree_path"
    return 0
  fi
  # Resolve to a canonical absolute path once; reused for both the primary-
  # worktree guard immediately below and the "is our CWD inside it?" check
  # further down.
  local worktree_real
  worktree_real="$(cd "$worktree_path" 2>/dev/null && pwd -P || echo "$worktree_path")"
  # Hard guard (#3710): NEVER attempt to remove the primary/main worktree — the
  # FIRST entry of `git worktree list --porcelain` — regardless of a
  # .loom-managed sentinel, the checked-out branch, --worktree-path, or a
  # customized worktree.root. This is the single choke point for all three
  # removal call-sites (default issue/pr path, --worktree-path override, and the
  # non-standard-path discovery fallback). Without it, a repo whose primary
  # checkout (a) sits at a non-standard path relative to a customized
  # worktree.root, (b) carries a .loom-managed sentinel, and (c) has the PR
  # branch checked out will reach `git worktree remove` on the main working
  # tree: git fails safe ("Could not remove worktree"), but the attempt is a
  # logic error and emits a misleading Removing/Could-not-remove pair. Refuse
  # here, before any sentinel or CWD handling.
  local primary_real
  primary_real="$(_primary_worktree_path)"
  if [[ -n "$primary_real" ]] && [[ "$worktree_real" == "$primary_real" ]]; then
    warning "Refusing to remove the primary/main worktree at $worktree_real (never removable regardless of .loom-managed sentinel, branch, or worktree.root)"
    return 0
  fi
  if [[ "$allow_unmanaged" != "true" ]] && [[ ! -f "$worktree_path/.loom-managed" ]]; then
    warning "Worktree at $worktree_path lacks .loom-managed sentinel — refusing to remove (user-owned)"
    return 0
  fi
  if [[ "$allow_unmanaged" == "true" ]] && [[ ! -f "$worktree_path/.loom-managed" ]]; then
    info "Bypassing sentinel guard (--worktree-path explicit opt-in for $worktree_path)"
  fi
  # Record the attached branch BEFORE removing the worktree (the porcelain
  # entry vanishes once the worktree is gone). Only relevant when allow_unmanaged
  # — the default issue/pr path already has the branch encoded in PR_BRANCH.
  local attached_branch=""
  if [[ "$allow_unmanaged" == "true" ]]; then
    attached_branch="$(_worktree_branch_for "$worktree_path")"
  fi
  # If our shell is inside the worktree we're removing, hop out first.
  # ($worktree_real was already resolved above for the primary-worktree guard.)
  local current_dir in_worktree=false
  current_dir="$(pwd -P 2>/dev/null || pwd)"
  if [[ "$current_dir" == "$worktree_real"* ]]; then
    in_worktree=true
    cd "$REPO_ROOT"
  fi
  # Data-loss guard (#5031): NEVER force-remove a worktree that still holds
  # uncommitted work. Post-merge cleanup keys the worktree only by branch name
  # (feature/issue-<N>) — the worktree.sh naming convention makes that name
  # collide deterministically whenever two hosts/sessions independently claim
  # the same issue number. When a *different*, still-live builder has that
  # branch checked out with unsaved edits, the blanket `git worktree remove
  # --force` below would silently destroy them (observed 2026-08-03 on #5001:
  # a live sibling session lost its in-flight, never-committed edits). The
  # neighbouring guards check only *identity/ownership* (primary-worktree #3710,
  # .loom-managed sentinel), never *live content*, so none of them catch this.
  # Refuse-and-warn instead: the merge itself already succeeded, so skipping
  # cleanup is a safe no-op for the merge while the sibling's work survives on
  # disk to be committed/pushed. The clean common case is unaffected — a
  # worktree that already committed+pushed the merged PR reports no changes, and
  # Loom's own gitignored runtime markers (.loom-managed / .loom-in-use /
  # .loom-checkpoint / .no-changes-needed / .snapshots/) are filtered out so a
  # bare/stale checkout that surfaces them as untracked is still removed.
  #
  # Not to be re-conflated in triage (distinct root causes):
  #   - #4463 (closed): same-HOST duplicate dispatch — fixed lock *ownership* so
  #     a live sweep's lock is not stolen by a peer's reaper. That is about lock
  #     records, not worktree/branch-name collision, and would not have caught
  #     #5031 (the colliding worktree came from a separate host that never
  #     touched this daemon's lock).
  #   - #4146 (open, loom:operator-only): *measures* the cross-host collision
  #     rate (observation only, behavior unchanged on collision by design). This
  #     guard is the behavioral mitigation for the data-loss instance #4146 is
  #     meant to eventually quantify.
  local dirty
  dirty="$(git -C "$worktree_path" status --porcelain 2>/dev/null \
    | grep -vE '[ /]\.loom-managed$|[ /]\.loom-in-use$|[ /]\.loom-checkpoint$|[ /]\.no-changes-needed$|[ /]\.snapshots/' \
    | grep -vE '^[[:space:]]*$' || true)"
  if [[ -n "$dirty" ]]; then
    local live_branch
    live_branch="$(_worktree_branch_for "$worktree_path" 2>/dev/null || true)"
    # Classify the dirty set so the "cross-host duplicate dispatch" hypothesis
    # is only offered when the dirt plausibly represents real, in-flight work.
    # Trivial/generated-artifact churn (e.g. a lockfile regenerated by a
    # routine install) is common and does NOT imply a live sibling builder —
    # asserting the hypothesis there sends the operator hunting for a phantom
    # concurrent session (#5658). Any tracked source change or untracked
    # non-artifact file present ⇒ still offer the hypothesis, even alongside
    # trivial dirt (mixed case: real work always wins).
    local dirty_has_real_work=false dirty_line dirty_path
    while IFS= read -r dirty_line; do
      [[ -z "$dirty_line" ]] && continue
      dirty_path="${dirty_line:3}"
      # Rename entries look like "old -> new" — classify by the new path.
      [[ "$dirty_path" == *" -> "* ]] && dirty_path="${dirty_path##* -> }"
      case "$dirty_path" in
        *.lock | *-lock.json) ;; # trivial/generated-artifact pattern
        *) dirty_has_real_work=true ;;
      esac
    done <<<"$dirty"
    warning "Refusing to remove worktree at $worktree_path — it has uncommitted changes${live_branch:+ on branch '$live_branch'} (data-loss guard, #5031):"
    warning "$dirty"
    if [[ "$dirty_has_real_work" == "true" ]]; then
      warning "A different, still-live builder session likely shares this branch name (cross-host duplicate dispatch). Leaving it in place so that work is not lost."
    else
      warning "The dirt above looks like environment/artifact churn (e.g. a regenerated lockfile), not concurrent work — verify and remove manually."
    fi
    warning "Remove it manually once those changes are saved/committed:"
    echo "  git -C \"$REPO_ROOT\" worktree remove \"$worktree_path\" --force"
    return 0
  fi
  info "Removing worktree: $worktree_path"
  # #6372: capture the actual git error (was silently discarded via 2>/dev/null)
  # and, on first failure, try one `git worktree prune` + retry cycle before
  # giving up — a stale worktree registration (administrative metadata out of
  # sync with the actual directory) can make the first removal attempt fail
  # even though nothing is genuinely holding the worktree open, and `prune`
  # clears exactly that kind of staleness. Confirmed via reproduction: the
  # original report recovered manually with `git worktree prune && rm -rf
  # <path>`, and `git worktree prune` alone (no `rm -rf`) is sufficient when
  # the directory itself is intact — only the registration was stale.
  local remove_err="" removed=false pruned=false
  if remove_err="$(git -C "$REPO_ROOT" worktree remove "$worktree_path" --force 2>&1)"; then
    removed=true
  elif git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1; then
    pruned=true
    if remove_err="$(git -C "$REPO_ROOT" worktree remove "$worktree_path" --force 2>&1)"; then
      removed=true
    fi
  fi

  if [[ "$removed" == "true" ]]; then
    if [[ "$pruned" == "true" ]]; then
      success "Worktree removed (after pruning a stale worktree registration)"
    else
      success "Worktree removed"
    fi
    # #5950: attribute the removal in the shared ledger. `attached_branch` is
    # only resolved on the unmanaged/explicit-override path; on the default
    # issue/PR path the branch is already `PR_BRANCH`, so fall back to that
    # rather than recording a null branch for the common case.
    loom_record_worktree_removal "$REPO_ROOT" "merge-pr.sh" "$worktree_path" \
      "${attached_branch:-${PR_BRANCH:-}}" "post_merge_cleanup"
    if [[ "$in_worktree" == "true" ]]; then
      echo ""
      warning "Your shell's working directory was inside the removed worktree."
      warning "Run this command to fix:"
      echo "  cd $REPO_ROOT"
    fi
    # For the explicit-override path, also tidy up the attached local branch.
    # We defer this to AFTER `git worktree remove` succeeds so the worktree's
    # checkout lock is released first.
    if [[ "$allow_unmanaged" == "true" ]] && [[ -n "$attached_branch" ]]; then
      _maybe_delete_local_branch "$attached_branch"
    fi
  else
    # Best-effort by design (#6372): the merge itself already succeeded and is
    # unaffected by cleanup failing, so this stays a warning rather than an
    # error() (which would exit 1 and misreport the merge as failed). But
    # unlike a bare "could not remove" with no context, name the actual git
    # failure and give an explicit remediation — matching the quality of the
    # existing partial-increment message elsewhere in this function.
    warning "Could not remove worktree at $worktree_path (best-effort cleanup — the merge itself already succeeded and is unaffected):"
    warning "$remove_err"
    warning "Remediation: git worktree prune && git -C \"$REPO_ROOT\" worktree remove \"$worktree_path\" --force"
    warning "If that still fails: rm -rf \"$worktree_path\" && git -C \"$REPO_ROOT\" worktree prune"
  fi
}

# _issue_is_closed_for_cleanup <issue_number>
#
# Async-close-race adaptation (#4186, adapted from fork PR #77's open-issue
# worktree guard).
#
# Removing a worktree unconditionally after merge breaks the partial-
# increment lifecycle (#3667): a `Part of #N` / `Contributes to #N` PR merges
# while issue N stays open, and the next Builder increment (or an agent still
# inside it) needs that worktree. But naively querying the issue's LIVE state
# right after merge has a race: GitHub closes `Closes #N` issues
# ASYNCHRONOUSLY, after the merge webhook fires — so a lookup taken here
# would see "open" for essentially every normal merge and silently defeat
# cleanup entirely. Gate the live lookup on whether this PR is actually a
# close target of the issue:
#
#   - $issue_number IS a close target of $PR_NUMBER -> the merge itself
#     closes it; clean up exactly as before this change (no lookup, no
#     race).
#   - $issue_number is NOT a close target (partial increment, or no closing
#     keyword at all) -> query live state via forge_get_issue_state and
#     preserve the worktree unless that state is CLOSED.
#
# Fail-unsafe-to-preserve: any lookup failure (forge_pr_close_targets
# returning nothing, forge_get_issue_state failing / returning an unknown
# state) is treated as "preserve" — cleanup must never destroy a worktree it
# isn't certain is safe to remove. A skipped cleanup here is always
# recoverable later (loom-clean, or a future merge that actually closes the
# issue).
#
# Returns 0 (true — safe to clean up) or 1 (false — preserve the worktree).
_issue_is_closed_for_cleanup() {
  local issue_number="$1"

  local close_targets
  close_targets="$(forge_pr_close_targets "$PR_NUMBER" "$GH" 2>/dev/null || true)"
  if echo "$close_targets" | grep -qx "$issue_number"; then
    return 0
  fi

  local state
  state="$(forge_get_issue_state "$REPO_NWO" "$issue_number" "$GH" 2>/dev/null || true)"
  [[ "$state" == "CLOSED" ]]
}

if [[ "$CLEANUP_WORKTREE" == "true" ]]; then
  if [[ "${LOOM_PRESERVE_WORKTREE:-0}" == "1" ]]; then
    info "Worktree cleanup skipped (LOOM_PRESERVE_WORKTREE=1) — local branch left in place"
  elif [[ -n "$WORKTREE_PATH_OVERRIDE" ]]; then
    # Explicit operator opt-in: bypass the sentinel guard for THIS path only.
    # The path was already validated at parse time (exists + is a registered
    # worktree of this repo). _remove_loom_worktree will also delete the
    # matching local branch via `git branch -d` (refuses on unmerged commits)
    # — this is the pre-#4100 caller, so no head-SHA safety check is passed;
    # behaviour is unchanged from before #4100.
    info "Cleanup target overridden by --worktree-path: $WORKTREE_PATH_OVERRIDE"
    _remove_loom_worktree "$WORKTREE_PATH_OVERRIDE" "true"
  else
    # Strict pattern: only `feature/issue-<N>` matches. Trailing-number
    # heuristics would misclassify branches like `release-1`.
    # Resolve the worktree base through the shared helper so an overridden
    # root (#3530) is discovered here; defaults to $REPO_ROOT/.loom/worktrees.
    WT_ROOT_DIR="$(loom_worktree_root "$REPO_ROOT")"
    DEFAULT_WT_PATH=""
    JUDGE_PR_WT_PATH=""
    if [[ "$PR_BRANCH" =~ ^feature/issue-([0-9]+)$ ]]; then
      ISSUE_NUM="${BASH_REMATCH[1]}"
      DEFAULT_WT_PATH="$WT_ROOT_DIR/issue-$ISSUE_NUM"
      # #6264: a Judge (or Doctor) review of this same ordinary Loom-issue PR
      # may ALSO have created a co-existing pr-$PR_NUMBER worktree via
      # pr-worktree.sh — checked and removed independently below, alongside
      # (not instead of) the issue-$ISSUE_NUM path above.
      JUDGE_PR_WT_PATH="$WT_ROOT_DIR/pr-$PR_NUMBER"
    else
      # External-fork / ad-hoc branch — the doctor would have used a
      # `pr-<PR_NUMBER>` worktree if any.
      DEFAULT_WT_PATH="$WT_ROOT_DIR/pr-$PR_NUMBER"
    fi
    if [[ -d "$DEFAULT_WT_PATH" ]]; then
      # Close-target-aware gate (#4186): ISSUE_NUM is only set when
      # PR_BRANCH matched the feature/issue-<N> convention above. When it's
      # unset (the pr-<N> path) this check is skipped entirely — unchanged
      # behavior.
      if [[ -n "${ISSUE_NUM:-}" ]] && ! _issue_is_closed_for_cleanup "$ISSUE_NUM"; then
        # #6694: the issue-close gate above says "preserve", but that is only
        # correct while the worktree/branch might still be needed. When the
        # branch's tip is already the merged PR's head SHA, every commit on
        # it made it into the merge — the worktree holds nothing unmerged
        # regardless of whether the ISSUE itself ever closes. Without this
        # check, a programme issue intentionally designed to accumulate
        # `Part of #N` increments forever (every merge non-closing by
        # design) would preserve this worktree/branch indefinitely, since
        # _issue_is_closed_for_cleanup never flips true for it.
        if _worktree_branch_fully_captured "$PR_BRANCH" "$PR_HEAD_SHA"; then
          info "Issue #$ISSUE_NUM is not a close target of PR #$PR_NUMBER (partial-increment case, #3667), but branch '$PR_BRANCH' tip matches PR #$PR_NUMBER's merged head — its content is already on the default branch, so the worktree holds nothing unmerged; removing it (#6694)"
          _remove_loom_worktree "$DEFAULT_WT_PATH"
        else
          warning "Preserving worktree at $DEFAULT_WT_PATH — issue #$ISSUE_NUM is not a close target of PR #$PR_NUMBER, its live state is not CLOSED, and branch '$PR_BRANCH' carries local commits beyond merged PR #$PR_NUMBER's head"
          info "This may be the partial-increment case (#3667) awaiting a future closing merge, or an issue-state lookup failure — cleanup retries automatically on a merge that closes #$ISSUE_NUM. If #$ISSUE_NUM is a programme issue designed never to close (#6694), that retry never fires: remove manually with 'git -C \"$REPO_ROOT\" worktree remove \"$DEFAULT_WT_PATH\" --force && git -C \"$REPO_ROOT\" branch -D $PR_BRANCH'"
        fi
      else
        _remove_loom_worktree "$DEFAULT_WT_PATH"
      fi
    else
      # Discovery fallback (warn-only): the Loom-convention path is missing,
      # so walk porcelain looking for any worktree tracking $PR_BRANCH. We
      # never auto-remove a discovered worktree — that would violate the
      # ownership model from #3334. Instead we surface the path so the
      # operator can re-run with --worktree-path.
      DISCOVERED_WT="$(_find_worktree_by_branch "$PR_BRANCH")"
      if [[ -n "$DISCOVERED_WT" ]]; then
        if _is_primary_worktree_path "$DISCOVERED_WT"; then
          # The PR branch is checked out in the PRIMARY (main) working copy,
          # not a linked worktree at all (#4171). `git worktree remove` /
          # `--worktree-path` can never apply here — git itself refuses to
          # remove the main working tree — so never suggest either. The
          # subsequent _maybe_delete_local_branch call below prints the
          # correct two-step remediation (switch to the default branch, then
          # delete) once the branch-delete attempt fails as "checked out".
          info "PR branch '$PR_BRANCH' is checked out in the primary repository checkout ($DISCOVERED_WT) — not a removable worktree."
        elif [[ -f "$DISCOVERED_WT/.loom-managed" ]]; then
          # Rare case: Loom-managed worktree at a non-standard path. The
          # sentinel says it's safe to remove — unless the close-target-aware
          # gate (#4186) says preserve.
          if [[ -n "${ISSUE_NUM:-}" ]] && ! _issue_is_closed_for_cleanup "$ISSUE_NUM"; then
            # #6694: see the matching comment at the default-path call site
            # above — reuse the tip-matches-merged-head check so a
            # never-closing programme issue does not preserve this
            # non-standard-path worktree forever either.
            if _worktree_branch_fully_captured "$PR_BRANCH" "$PR_HEAD_SHA"; then
              info "Issue #$ISSUE_NUM is not a close target of PR #$PR_NUMBER (partial-increment case, #3667), but branch '$PR_BRANCH' tip matches PR #$PR_NUMBER's merged head — its content is already on the default branch, so the discovered worktree holds nothing unmerged; removing it (#6694)"
              _remove_loom_worktree "$DISCOVERED_WT"
            else
              warning "Preserving discovered worktree at $DISCOVERED_WT — issue #$ISSUE_NUM is not a close target of PR #$PR_NUMBER, its live state is not CLOSED, and branch '$PR_BRANCH' carries local commits beyond merged PR #$PR_NUMBER's head"
              info "This may be the partial-increment case (#3667) awaiting a future closing merge, or an issue-state lookup failure — cleanup retries automatically on a merge that closes #$ISSUE_NUM. If #$ISSUE_NUM is a programme issue designed never to close (#6694), that retry never fires: remove manually with 'git -C \"$REPO_ROOT\" worktree remove \"$DISCOVERED_WT\" --force && git -C \"$REPO_ROOT\" branch -D $PR_BRANCH'"
            fi
          else
            info "Discovered Loom-managed worktree at non-standard path: $DISCOVERED_WT"
            _remove_loom_worktree "$DISCOVERED_WT"
          fi
        else
          warning "Discovered worktree for branch '$PR_BRANCH' at: $DISCOVERED_WT"
          warning "Worktree lacks .loom-managed sentinel — not removing (user-owned)."
          warning "To clean it up, re-run with: --worktree-path '$DISCOVERED_WT'"
          warning "Or manually: git worktree remove '$DISCOVERED_WT'"
        fi
      else
        info "No worktree found at $DEFAULT_WT_PATH (and none tracking '$PR_BRANCH' in 'git worktree list')"
      fi
    fi

    # #6264: independently check for a co-existing Judge/Doctor review
    # worktree at pr-$PR_NUMBER, alongside whatever the issue-$ISSUE_NUM
    # handling above did. Only set when PR_BRANCH matched feature/issue-<N>
    # (the external-fork branch above already used pr-$PR_NUMBER as
    # DEFAULT_WT_PATH and handled it there — this block would be a pure
    # duplicate for that branch, so JUDGE_PR_WT_PATH stays empty there).
    #
    # Checked by PATH existence, not by the branch checked out inside it —
    # pr-worktree.sh creates this worktree via `git worktree add --detach`
    # then `gh pr checkout --force`; the latter fails (and leaves the
    # worktree on a detached HEAD) when the branch collides with one already
    # checked out elsewhere (e.g. this same issue's issue-$ISSUE_NUM
    # worktree) — see pr-worktree.sh's collision handling. A path-based check
    # here removes the worktree either way, matching reap_pr_worktrees'
    # (loom-daemon's #5939 periodic backstop) own PR-number+path keyed
    # eligibility, which is likewise branch-state-independent.
    if [[ -n "$JUDGE_PR_WT_PATH" ]] && [[ -d "$JUDGE_PR_WT_PATH" ]]; then
      if [[ -n "${ISSUE_NUM:-}" ]] && ! _issue_is_closed_for_cleanup "$ISSUE_NUM"; then
        # #6694: see the matching comment at the default-path call site
        # above — reuse the tip-matches-merged-head check so a never-closing
        # programme issue does not preserve this Judge/Doctor review
        # worktree forever either.
        if _worktree_branch_fully_captured "$PR_BRANCH" "$PR_HEAD_SHA"; then
          info "Issue #$ISSUE_NUM is not a close target of PR #$PR_NUMBER (partial-increment case, #3667), but branch '$PR_BRANCH' tip matches PR #$PR_NUMBER's merged head — its content is already on the default branch, so the Judge/Doctor review worktree holds nothing unmerged; removing it (#6694)"
          _remove_loom_worktree "$JUDGE_PR_WT_PATH"
        else
          warning "Preserving Judge/Doctor review worktree at $JUDGE_PR_WT_PATH — issue #$ISSUE_NUM is not a close target of PR #$PR_NUMBER, its live state is not CLOSED, and branch '$PR_BRANCH' carries local commits beyond merged PR #$PR_NUMBER's head"
          info "This may be the partial-increment case (#3667) awaiting a future closing merge, or an issue-state lookup failure — cleanup retries automatically on a merge that closes #$ISSUE_NUM. If #$ISSUE_NUM is a programme issue designed never to close (#6694), that retry never fires: remove manually with 'git -C \"$REPO_ROOT\" worktree remove \"$JUDGE_PR_WT_PATH\" --force && git -C \"$REPO_ROOT\" branch -D $PR_BRANCH'"
        fi
      else
        info "Found co-existing Judge/Doctor review worktree at $JUDGE_PR_WT_PATH (PR #$PR_NUMBER, alongside issue-$ISSUE_NUM handling above) — removing (#6264)"
        _remove_loom_worktree "$JUDGE_PR_WT_PATH"
      fi
    fi
    # Local-branch delete (#4100): the default-convention path, the
    # discovered-Loom-managed-non-standard-path, and the no-worktree-at-all
    # case (rows 2-4 of the issue's path table) all funnel through here —
    # none of them call _maybe_delete_local_branch internally the way the
    # --worktree-path override does above. Passing $PR_HEAD_SHA lets the
    # helper safely `-D` a branch whose tip matches the merged PR (the only
    # criterion that is correct after a squash merge — `git branch --merged`
    # is not). If the discovered worktree above was user-owned and left in
    # place, its branch is still checked out there, so this call is a
    # harmless no-op that reports the specific "checked out" refusal instead
    # of attempting a real delete.
    _maybe_delete_local_branch "$PR_BRANCH" "$PR_HEAD_SHA"
  fi
else
  info "Worktree cleanup skipped (--no-cleanup-worktree) — local branch left in place"
fi

success "Done"
