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
#   --no-cleanup-worktree  Skip local worktree cleanup after merge
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
#
# By default, the local worktree is cleaned up after a successful merge.
# Pass --no-cleanup-worktree to skip this (e.g., when other terminals may
# have their CWD inside the worktree).
#
# Cleanup is restricted to Loom-managed worktrees (those containing the
# .loom-managed sentinel written by worktree.sh). Worktrees lacking the
# sentinel are treated as user-owned and never removed. Set
# LOOM_PRESERVE_WORKTREE=1 to disable cleanup unconditionally for a session.
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
  --no-cleanup-worktree  Skip local worktree cleanup after merge
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
  -h, --help             Show this help and exit

By default, the local worktree is cleaned up after a successful merge.
Pass --no-cleanup-worktree to skip this (e.g., when other terminals may
have their CWD inside the worktree).

Cleanup is restricted to Loom-managed worktrees (those under
.loom/worktrees/issue-N that contain a .loom-managed sentinel file written
by worktree.sh). User-provisioned worktrees at other paths are never
removed by the default code path. Set LOOM_PRESERVE_WORKTREE=1 to disable
cleanup unconditionally for a session.

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
DRY_RUN=false
AUTO_MERGE=false
WORKTREE_PATH_OVERRIDE=""
ALLOW_STACKED_CHILDREN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cleanup-worktree) shift ;;  # no-op, cleanup is now the default
    --no-cleanup-worktree) CLEANUP_WORKTREE=false; shift ;;
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

[[ -z "$PR_NUMBER" ]] && error "Usage: merge-pr.sh <pr-number> [--no-cleanup-worktree] [--worktree-path <dir>] [--dry-run] [--auto] [--allow-stacked-children]"
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
  local issue_json issue_state issue_labels

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
    info "Partial-increment reset: issue #$issue_num is not open (state='${issue_state:-unknown}') — skipping"
    return 0
  fi

  issue_labels="$(echo "$issue_json" | jq -r '.labels[]?.name' 2>/dev/null || true)"
  if ! printf '%s\n' "$issue_labels" | grep -qx 'loom:building'; then
    info "Partial-increment reset: issue #$issue_num is not loom:building — skipping (idempotent)"
    return 0
  fi

  info "Partial-increment reset: PR #$PR_NUMBER merged as a partial slice of #$issue_num; returning it to the ready queue"
  if gh issue edit "$issue_num" \
       --repo "$REPO_NWO" \
       --remove-label "loom:building" \
       --add-label "loom:issue" >/dev/null 2>&1; then
    success "Issue #$issue_num: loom:building -> loom:issue (partial increment; issue remains open)"
    local ts comment
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    comment="## Partial Increment Merged

PR #$PR_NUMBER merged with a non-closing \`Part of\` / \`Contributes to\` reference, so this issue remains **open** for further work.

**Action taken**:
- Removed \`loom:building\` label
- Added \`loom:issue\` label to return to the ready queue

This issue is now available for the next increment (a subsequent \`/loom:sweep\` will treat it as ready rather than in-flight).

---
*Reset by merge-pr.sh (#3667) at $ts*"
    gh issue comment "$issue_num" --repo "$REPO_NWO" --body "$comment" >/dev/null 2>&1 || \
      warning "Could not post partial-increment comment on issue #$issue_num (label swap still applied)"
  else
    warning "Could not reset labels on issue #$issue_num (partial increment) — may need manual 'gh issue edit'"
  fi
}

# Parse the merged PR body for non-closing partial-increment references and
# reset each referenced issue. Best-effort; returns 0 unconditionally.
_reset_partial_increment_labels() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0

  local pr_body
  pr_body="$(echo "$PR_JSON" | jq -r '.body // ""')"
  [[ -n "$pr_body" ]] || return 0

  # Extract issue numbers referenced with a NON-closing partial-increment
  # keyword: "Part of #N" / "Contributes to #N" (case-insensitive), deduped.
  # grep -oiE emits e.g. "Part of #123"; a second grep strips to the number.
  local refs
  refs="$(printf '%s\n' "$pr_body" \
    | grep -oiE '(Part of|Contributes to)[[:space:]]+#[0-9]+' \
    | grep -oE '[0-9]+' \
    | sort -u || true)"
  [[ -n "$refs" ]] || return 0

  local issue_num
  while IFS= read -r issue_num; do
    [[ -n "$issue_num" ]] || continue
    _reset_one_partial_issue "$issue_num"
  done <<< "$refs"

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
    gh pr comment "$child_pr" --repo "$REPO_NWO" --body "$comment" >/dev/null 2>&1 || \
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
if [[ "$AUTO_MERGE" == "true" ]]; then
  # Bounded poll window for the UNSTABLE-because-checks-are-still-running case
  # (#3664). Reuses the same env-var names/semantics as the Gitea auto-merge
  # poller (loom-tools/src/loom_tools/auto_merge.py) so both forges share
  # configuration. Defaults match that CLI: 30s interval, 600s ceiling.
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
    # Prefer loom-auto-merge CLI (forge-agnostic, with poll-and-merge for Gitea)
    if command -v loom-auto-merge &>/dev/null; then
      [[ $MERGE_ATTEMPT -eq 1 ]] && info "Using loom-auto-merge (forge-agnostic auto-merge)"
      if AUTO_MERGE_OUTPUT=$(loom-auto-merge "$PR_NUMBER" --method squash 2>&1); then
        AUTO_MERGE_OK=true
        break
      fi
    else
      # Fallback: shell-based forge_auto_merge
      if AUTO_MERGE_OUTPUT=$(forge_auto_merge "$REPO_NWO" "$PR_NUMBER" 2>&1); then
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

# Check mergeability
if [[ "$PR_MERGEABLE" == "false" ]]; then
  error "PR #$PR_NUMBER has merge conflicts — resolve before merging"
fi

if [[ "$DRY_RUN" == "true" ]]; then
  info "[dry-run] Would merge PR #$PR_NUMBER (squash) and delete branch '$PR_BRANCH'"
  [[ "$CLEANUP_WORKTREE" == "true" ]] && info "[dry-run] Would clean up local worktree"
  exit 0
fi

# Merge via API (squash) with retry for stale branch
MAX_MERGE_RETRIES=3
MERGE_RETRY_DELAY=5

for MERGE_ATTEMPT in $(seq 1 $MAX_MERGE_RETRIES); do
  MERGE_RESPONSE=$(forge_merge_pr "$REPO_NWO" "$PR_NUMBER" 2>&1) && break  # Success, exit loop

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

# Automated stacked-PR reconciliation (#3747, stacked-PR v2 item 1). Runs at the
# same confirmed-merge choke point, and BEFORE branch deletion below so the
# parent branch ref still resolves as reconcile-stack.sh's rebase <upstream>
# argument. Best-effort — never fails the merge. See the function above.
_auto_reconcile_stacked_children || true

# NOTE: Label cleanup on linked issues is intentionally skipped for the
# `Closes #N` / `Fixes #N` / `Resolves #N` auto-close case.
# Labels on closed/merged items are harmless — all agents filter by open state.
# See: https://github.com/rjwalters/loom/issues/2838
#
# EXCEPTION (#3667): non-closing `Part of #N` / `Contributes to #N` partial-
# increment references leave the referenced issue OPEN after merge, so its
# `loom:building` label would otherwise be orphaned. The
# _reset_partial_increment_labels call above handles exactly that case by
# swapping loom:building -> loom:issue on the still-open referenced issue. The
# `Closes`-keyword path below is unchanged.
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
  info "Skipping branch deletion (auto-delete is enabled)"
else
  info "Deleting remote branch: $PR_BRANCH"
  forge_delete_branch "$REPO_NWO" "$PR_BRANCH" && \
    success "Branch '$PR_BRANCH' deleted" || \
    warning "Could not delete branch '$PR_BRANCH' (may already be deleted)"
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
#   - .loom/worktrees/issue-<N>/  (Loom-issue branches: feature/issue-<N>)
#   - .loom/worktrees/pr-<N>/     (external-fork / ad-hoc branches; #3358)
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

# Delete the matching local branch. Uses `git branch -d` (not -D) so unmerged
# commits abort the delete — that's the right safety net. Never fails the
# cleanup pipeline; warns on errors.
_maybe_delete_local_branch() {
  local branch="$1"
  if [[ -z "$branch" ]]; then
    return 0
  fi
  if ! git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    info "Local branch '$branch' does not exist — skipping branch delete"
    return 0
  fi
  if git -C "$REPO_ROOT" branch -d "$branch" 2>/dev/null; then
    success "Local branch '$branch' deleted"
  else
    warning "Could not delete local branch '$branch' (may have unmerged commits — use 'git branch -D' if intentional)"
  fi
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
  info "Removing worktree: $worktree_path"
  if git -C "$REPO_ROOT" worktree remove "$worktree_path" --force 2>/dev/null; then
    success "Worktree removed"
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
    warning "Could not remove worktree at $worktree_path"
  fi
}

if [[ "$CLEANUP_WORKTREE" == "true" ]]; then
  if [[ "${LOOM_PRESERVE_WORKTREE:-0}" == "1" ]]; then
    info "Worktree cleanup skipped (LOOM_PRESERVE_WORKTREE=1)"
  elif [[ -n "$WORKTREE_PATH_OVERRIDE" ]]; then
    # Explicit operator opt-in: bypass the sentinel guard for THIS path only.
    # The path was already validated at parse time (exists + is a registered
    # worktree of this repo). _remove_loom_worktree will also delete the
    # matching local branch via `git branch -d` (refuses on unmerged commits).
    info "Cleanup target overridden by --worktree-path: $WORKTREE_PATH_OVERRIDE"
    _remove_loom_worktree "$WORKTREE_PATH_OVERRIDE" "true"
  else
    # Strict pattern: only `feature/issue-<N>` matches. Trailing-number
    # heuristics would misclassify branches like `release-1`.
    # Resolve the worktree base through the shared helper so an overridden
    # root (#3530) is discovered here; defaults to $REPO_ROOT/.loom/worktrees.
    WT_ROOT_DIR="$(loom_worktree_root "$REPO_ROOT")"
    DEFAULT_WT_PATH=""
    if [[ "$PR_BRANCH" =~ ^feature/issue-([0-9]+)$ ]]; then
      ISSUE_NUM="${BASH_REMATCH[1]}"
      DEFAULT_WT_PATH="$WT_ROOT_DIR/issue-$ISSUE_NUM"
    else
      # External-fork / ad-hoc branch — the doctor would have used a
      # `pr-<PR_NUMBER>` worktree if any.
      DEFAULT_WT_PATH="$WT_ROOT_DIR/pr-$PR_NUMBER"
    fi
    if [[ -d "$DEFAULT_WT_PATH" ]]; then
      _remove_loom_worktree "$DEFAULT_WT_PATH"
    else
      # Discovery fallback (warn-only): the Loom-convention path is missing,
      # so walk porcelain looking for any worktree tracking $PR_BRANCH. We
      # never auto-remove a discovered worktree — that would violate the
      # ownership model from #3334. Instead we surface the path so the
      # operator can re-run with --worktree-path.
      DISCOVERED_WT="$(_find_worktree_by_branch "$PR_BRANCH")"
      if [[ -n "$DISCOVERED_WT" ]]; then
        if [[ -f "$DISCOVERED_WT/.loom-managed" ]]; then
          # Rare case: Loom-managed worktree at a non-standard path. The
          # sentinel says it's safe to remove, so do so.
          info "Discovered Loom-managed worktree at non-standard path: $DISCOVERED_WT"
          _remove_loom_worktree "$DISCOVERED_WT"
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
  fi
fi

success "Done"
