#!/bin/bash
# Loom Branch Cleanup Script - Remove feature branches for closed issues, and
# review-time PR branches for merged/closed PRs.
#
# This script automatically cleans up stale local branches:
#   1. `feature/issue-<N>` branches for issues that are confirmed CLOSED.
#   2. `pr-<N>` / `pr<N>-*` / `pr-<N>-*` review-time branches (left behind by
#      Judge/Doctor sessions that checked out a PR outside a managed worktree,
#      e.g. `pr-4372`, `pr-4372-v2`, `pr4396-review`) for PRs that are
#      confirmed MERGED or CLOSED (#4405). Because this repo squash-merges,
#      `git branch --merged` can never classify these — they must be
#      cross-checked against the forge's PR state instead.
#
# Usage:
#   ./scripts/cleanup-branches.sh [--dry-run]
#
# Options:
#   --dry-run    Show what would be deleted without actually deleting
#
# Safety:
#   - Only deletes feature/issue-* branches for confirmed CLOSED issues
#   - Only deletes pr-* review branches for confirmed MERGED/CLOSED PRs, and
#     only through `loom-daemon merge-pr delete-branch` — the shared
#     tip-SHA-verified rule merge-pr.sh and `worktree.sh remove` both use
#     (never a raw `git branch -D`)
#   - Preserves branches for OPEN issues/PRs
#   - Provides summary of actions taken

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Forge-agnostic issue/PR operations via the native `loom-daemon forge`
# subcommand (port of the retired `loom-forge`). GitHub: passthrough to `gh`.
# Gitea: declines (exit 3), degrading to the `gh` fallback. Fall back to `gh`
# when loom-daemon is absent so a bare workspace still works.
if command -v loom-daemon &>/dev/null; then
    FORGE="loom-daemon forge"
else
    FORGE="gh"
fi

# Colors
# shellcheck disable=SC2034  # Color palette - not all colors used in every script
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse arguments
DRY_RUN=false
if [[ "$1" == "--dry-run" ]]; then
  DRY_RUN=true
  echo -e "${YELLOW}🔍 DRY RUN MODE - No branches will be deleted${NC}"
  echo
fi

# Get all feature branches
branches=$(git branch | grep "feature/issue-" | sed 's/^[*+ ]*//' || true)

# Track stats
checked=0
closed=0
open=0
errors=0

if [ -z "$branches" ]; then
  echo "No feature branches found matching pattern 'feature/issue-*'"
else
  echo "Checking branch status..."
  echo

  for branch in $branches; do
      # Extract issue number (handle branches like feature/issue-123 or feature/issue-123-description)
      issue_num=$(echo "$branch" | sed 's/feature\/issue-//' | sed 's/-.*//' | sed 's/[^0-9].*//')

      # Skip if we couldn't extract a valid number
      if [[ ! "$issue_num" =~ ^[0-9]+$ ]]; then
          echo -e "${YELLOW}? Skipping $branch (couldn't extract issue number)${NC}"
          continue
      fi

      checked=$((checked + 1))

      # Check issue status
      status=$($FORGE issue view "$issue_num" --json state --jq .state 2>/dev/null || echo "NOT_FOUND")

      if [[ "$status" == "CLOSED" ]]; then
          echo -e "${GREEN}✓${NC} Issue #$issue_num is CLOSED - deleting $branch"
          if [[ "$DRY_RUN" == false ]]; then
              # `git branch -D` exits non-zero when the branch is checked out in
              # a linked worktree. Under `set -e` a bare, error-swallowed delete
              # would abort the whole script mid-run — before the pr-* review
              # pass below ever executes — while still printing the "deleting"
              # line and counting a success. Make the delete non-fatal and keep
              # the counters honest (#4405). Same reasoning as the `((var++))`
              # fix: a single un-deletable branch must not sink the entire run.
              if git branch -D "$branch" 2>/dev/null; then
                  closed=$((closed + 1))
              else
                  echo -e "${YELLOW}⚠${NC} Could not delete $branch (checked out in a worktree?) - keeping"
                  errors=$((errors + 1))
              fi
          else
              closed=$((closed + 1))
          fi
      elif [[ "$status" == "OPEN" ]]; then
          echo -e "${BLUE}○${NC} Issue #$issue_num is OPEN - keeping $branch"
          open=$((open + 1))
      else
          echo -e "${YELLOW}?${NC} Issue #$issue_num not found - keeping $branch"
          errors=$((errors + 1))
      fi
  done

  echo
  echo "Summary:"
  echo "  Checked: $checked branches"
  if [[ "$DRY_RUN" == true ]]; then
      echo -e "  ${YELLOW}Would delete${NC}: $closed (closed issues)"
  else
      echo -e "  ${GREEN}Deleted${NC}: $closed (closed issues)"
  fi
  echo -e "  ${BLUE}Kept${NC}:    $open (open issues)"
  if [[ $errors -gt 0 ]]; then
      echo -e "  ${YELLOW}Errors${NC}:  $errors (issue not found / branch not deletable)"
  fi
fi

# --- PR review-branch cleanup (#4405) ---
#
# Judge/Doctor review-time branches (e.g. a bare `gh pr checkout` outside a
# managed worktree, or a scratch `-v2` iteration) don't match
# `feature/issue-*`, so the loop above never sees them. Discover branches
# shaped like `pr-<N>`, `pr<N>-*`, or `pr-<N>-*`, resolve each to its
# originating PR, and delete it only when that PR is MERGED or CLOSED.
pr_branches=$(git branch --format='%(refname:short)' | grep -E '^pr-?[0-9]+(-.*)?$' || true)

pr_checked=0
pr_deleted=0
pr_kept_open=0
pr_kept_unsafe=0
pr_errors=0

if [[ -n "$pr_branches" ]]; then
    echo
    echo "Checking PR review-branch status..."
    echo

    # Every delete goes through `loom-daemon merge-pr delete-branch` — the
    # shared, tip-SHA-verified `worktree_cli::branch_delete` rule that
    # merge-pr.sh's own `_maybe_delete_local_branch` and `worktree.sh remove`
    # both call (#8191/#8195 slice 3), never a raw `git branch -D`.
    #
    # Until #8968 this pass `awk`-extracted that shell function out of
    # merge-pr.sh and `eval`ed it, alongside three transitive helpers
    # (`_primary_worktree_path`, `_is_primary_worktree_path`,
    # `_find_worktree_by_branch`), a `lib/branch-landed.sh` source and a
    # fail-closed `branch_landed()` shim. Once #8191 made the extraction
    # target a thin wrapper around this very subcommand, none of that was
    # reachable from the extracted body — so calling the subcommand directly
    # keeps the "one implementation, no drift" guarantee the extraction
    # existed for, with none of the machinery.
    #
    # requires-daemon: merge-pr >= 0.19.399   #8191 — `merge-pr delete-branch`
    # was added in 01982e6be (merge train D, #9020) while VERSION still read
    # 0.19.398, so 0.19.399 (93df55164) is the first version that shipped it.
    # Hard, not `optional`: there is no second implementation to degrade to.
    # Both failure shapes keep every branch, because a guard that cannot run
    # must never delete a branch on its own authority: no binary resolves at
    # all -> the whole pass is skipped with the one warning below; a binary
    # below the floor (or any other non-zero exit) -> one per-branch warning
    # from _delete_landed_branch, naming the version remedy.
    LOOM_DAEMON="${LOOM_DAEMON_BIN:-loom-daemon}"
    if ! command -v "$LOOM_DAEMON" >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠${NC}  Could not resolve '$LOOM_DAEMON' for the branch-delete safety check - skipping PR review-branch cleanup"
    else
        # Logging shims the daemon's decision lines are replayed through.
        info() { echo -e "${BLUE}ℹ${NC} $*"; }
        warning() { echo -e "${YELLOW}⚠${NC} $*"; }
        success() { echo -e "${GREEN}✓${NC} $*"; }

        REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
        DEFAULT_BRANCH_NAME=""
        if [[ -f "$SCRIPT_DIR/lib/default-branch.sh" ]]; then
            # shellcheck source=lib/default-branch.sh
            source "$SCRIPT_DIR/lib/default-branch.sh"
            DEFAULT_BRANCH_NAME="$(cd "$REPO_ROOT" && loom_default_branch 2>/dev/null || true)"
        fi

        # _delete_landed_branch <branch> [expected_head_sha]
        #
        # The daemon emits one `LEVEL<TAB>message` line per decision on
        # stdout; replay each through info/warning/success so the
        # operator-visible text and coloring match merge-pr.sh's. Only stdout
        # is parsed — stderr (clap errors, logs) passes through untouched,
        # never replayed as a bogus INFO line. Whatever it DID print is
        # replayed before a non-zero exit is reported, so a crash after a
        # delete cannot hide the delete. Never fails the pass: this cleanup is
        # advisory, so a guard that could not run is a warning, not a blocker.
        # An unset DEFAULT_BRANCH_NAME omits --default-branch, the
        # conservative direction (it disables the named default-branch arm and
        # the #5015 auto-cleanup), exactly as the empty shell var used to.
        _delete_landed_branch() {
            local branch="$1" expected_head_sha="${2:-}"
            local flags=() out rc=0 level text
            [[ -z "$DEFAULT_BRANCH_NAME" ]] || flags+=(--default-branch "$DEFAULT_BRANCH_NAME")
            out="$("$LOOM_DAEMON" merge-pr delete-branch --repo-root "$REPO_ROOT" --branch "$branch" --expected-head-sha "$expected_head_sha" ${flags[@]+"${flags[@]}"})" || rc=$?
            while IFS=$'\t' read -r level text; do
                [[ -n "$level" ]] || continue
                case "$level" in
                    SUCCESS) success "$text" ;;
                    WARNING) warning "$text" ;;
                    *) info "$text" ;;
                esac
            done <<< "$out"
            [[ $rc -eq 0 ]] || warning "The local-branch cleanup guard for '$branch' did not complete — '$LOOM_DAEMON merge-pr delete-branch' exited $rc (a binary predating #8191 has no such subcommand). Advisory only: any branch action it did take is reported above, otherwise '$branch' is left as-is."
            return 0
        }

        for branch in $pr_branches; do
            pr_num=$(echo "$branch" | sed -E 's/^pr-?([0-9]+).*/\1/')

            if [[ ! "$pr_num" =~ ^[0-9]+$ ]]; then
                echo -e "${YELLOW}?${NC} Skipping $branch (couldn't extract PR number)"
                continue
            fi

            pr_checked=$((pr_checked + 1))

            pr_json=$($FORGE pr view "$pr_num" --json state,headRefOid 2>/dev/null || echo "")
            if [[ -z "$pr_json" ]]; then
                echo -e "${YELLOW}?${NC} PR #$pr_num not found - keeping $branch"
                pr_errors=$((pr_errors + 1))
                continue
            fi

            pr_state=$(echo "$pr_json" | jq -r '.state // "NOT_FOUND"' 2>/dev/null || echo "NOT_FOUND")
            pr_head_sha=$(echo "$pr_json" | jq -r '.headRefOid // empty' 2>/dev/null || echo "")

            if [[ "$pr_state" == "OPEN" ]]; then
                echo -e "${BLUE}○${NC} PR #$pr_num is OPEN - keeping $branch"
                pr_kept_open=$((pr_kept_open + 1))
                continue
            fi

            if [[ "$pr_state" != "MERGED" && "$pr_state" != "CLOSED" ]]; then
                echo -e "${YELLOW}?${NC} PR #$pr_num state unknown ($pr_state) - keeping $branch"
                pr_errors=$((pr_errors + 1))
                continue
            fi

            if [[ "$DRY_RUN" == true ]]; then
                echo -e "${GREEN}✓${NC} PR #$pr_num is $pr_state - would delete $branch (dry-run)"
                pr_deleted=$((pr_deleted + 1))
                continue
            fi

            echo -e "${GREEN}✓${NC} PR #$pr_num is $pr_state - evaluating $branch for deletion"
            _delete_landed_branch "$branch" "$pr_head_sha"
            if git -C "$REPO_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
                pr_kept_unsafe=$((pr_kept_unsafe + 1))
            else
                pr_deleted=$((pr_deleted + 1))
            fi
        done

        echo
        echo "PR review-branch summary:"
        echo "  Checked: $pr_checked branches"
        if [[ "$DRY_RUN" == true ]]; then
            echo -e "  ${YELLOW}Would delete${NC}: $pr_deleted (merged/closed PRs)"
        else
            echo -e "  ${GREEN}Deleted${NC}: $pr_deleted (merged/closed PRs)"
        fi
        echo -e "  ${BLUE}Kept${NC}:    $pr_kept_open (open PRs)"
        if [[ $pr_kept_unsafe -gt 0 ]]; then
            echo -e "  ${YELLOW}Kept (unsafe to force-delete)${NC}: $pr_kept_unsafe"
        fi
        if [[ $pr_errors -gt 0 ]]; then
            echo -e "  ${YELLOW}Errors${NC}:  $pr_errors (PR not found / state unknown)"
        fi
    fi
fi

if [[ "$DRY_RUN" == true ]]; then
    echo
    echo "To actually delete branches, run without --dry-run flag"
fi
