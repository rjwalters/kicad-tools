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
#   - Only deletes pr-* review branches for confirmed MERGED/CLOSED PRs,
#     reusing merge-pr.sh's tip-SHA-verified `_maybe_delete_local_branch`
#     helper (never a raw `git branch -D`)
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

    # Reuse merge-pr.sh's `_maybe_delete_local_branch` (tip-SHA-verified
    # safety check around lines 1493-1568) instead of duplicating it. Extract
    # the real function bodies from the live source so the two never drift —
    # the same technique defaults/scripts/tests/test-merge-pr-local-branch-cleanup.sh
    # uses to test it.
    MERGE_PR_SCRIPT="$SCRIPT_DIR/merge-pr.sh"

    # The landed check `_maybe_delete_local_branch` delegates its `-d` → `-D`
    # upgrade to (#7812). Sourced, not extracted — it is a real shared library.
    # Fail-closed `unknown` shim when a partially-resynced .loom/ lacks it, so
    # this pass degrades to the conservative `git branch -d`, never crashes.
    if [[ -f "$SCRIPT_DIR/lib/branch-landed.sh" ]]; then
        # shellcheck source=lib/branch-landed.sh
        source "$SCRIPT_DIR/lib/branch-landed.sh"
    else
        # shellcheck disable=SC2034  # side-channel globals read by callers
        branch_landed() {
            BRANCH_LANDED_VERDICT="unknown"; BRANCH_LANDED_EVIDENCE="inconclusive"
            BRANCH_LANDED_PR_NUMBER=""; BRANCH_LANDED_PR_HEAD_SHA=""
            BRANCH_LANDED_FORGE_STATUS="unavailable"
            printf 'unknown\n'
        }
    fi

    # Extract one top-level function definition verbatim from a shell script.
    # merge-pr.sh defines every function at column 0 with its closing brace
    # also at column 0, so "first `^}` after the opening line" is exact.
    _extract_shell_fn() {
        local fn_name="$1" src="$2"
        awk -v fn="$fn_name" '
            $0 ~ "^" fn "\\(\\) \\{" { grab=1 }
            grab { print }
            grab && /^}/ { exit }
        ' "$src"
    }

    _MAYBE_DELETE_FN=""
    # _maybe_delete_local_branch's delete-refusal path calls these three
    # helpers (merge-pr.sh, #4171) to distinguish "checked out in the primary
    # checkout" from "checked out in some other worktree". They must be
    # extracted alongside it — otherwise a merged/closed PR whose `pr-<N>`
    # branch is still checked out in a live review worktree (exactly the
    # population this pass targets) aborts the whole script with
    # `_find_worktree_by_branch: command not found` under `set -e` (#4405).
    # The `-d` → `-D` upgrade's safety predicate is NOT in this list since
    # #7812: it is `branch_landed` from lib/branch-landed.sh, a real shared
    # library sourced below, so there is no function body to extract.
    _MAYBE_DELETE_DEPS=(_primary_worktree_path _is_primary_worktree_path _find_worktree_by_branch)
    _MAYBE_DELETE_DEP_FNS=""
    if [[ -f "$MERGE_PR_SCRIPT" ]]; then
        _MAYBE_DELETE_FN="$(_extract_shell_fn _maybe_delete_local_branch "$MERGE_PR_SCRIPT")"
        for _dep_fn in "${_MAYBE_DELETE_DEPS[@]}"; do
            _dep_src="$(_extract_shell_fn "$_dep_fn" "$MERGE_PR_SCRIPT")"
            if [[ -n "$_dep_src" ]]; then
                _MAYBE_DELETE_DEP_FNS+="$_dep_src"$'\n'
            else
                # Upstream renamed/removed the helper: degrade to the generic
                # "checked out somewhere" warning instead of crashing. All
                # shims are safe under `set -e` — the path shims print nothing
                # and succeed, and `_is_primary_worktree_path` is only ever
                # used as an `if` test.
                case "$_dep_fn" in
                    _is_primary_worktree_path)
                        _MAYBE_DELETE_DEP_FNS+="$_dep_fn() { return 1; }"$'\n' ;;
                    *)  _MAYBE_DELETE_DEP_FNS+="$_dep_fn() { :; }"$'\n' ;;
                esac
            fi
        done
    fi

    if [[ -z "$_MAYBE_DELETE_FN" ]]; then
        echo -e "${YELLOW}⚠${NC}  Could not load the branch-delete safety helper from $MERGE_PR_SCRIPT - skipping PR review-branch cleanup"
    else
        # Minimal logging shims — _maybe_delete_local_branch calls these.
        info() { echo -e "${BLUE}ℹ${NC} $*"; }
        warning() { echo -e "${YELLOW}⚠${NC} $*"; }
        success() { echo -e "${GREEN}✓${NC} $*"; }
        # shellcheck disable=SC2317  # invoked indirectly via the evaluated function
        error() { echo -e "${RED}✗${NC} $*" >&2; return 1; }

        # REPO_ROOT / DEFAULT_BRANCH_NAME are read by the evaluated bodies, so
        # they must be set before the first call (not before the eval itself).
        # shellcheck disable=SC2034  # read inside the evaluated merge-pr.sh function bodies
        REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
        # shellcheck disable=SC2034  # read inside the evaluated _maybe_delete_local_branch body
        DEFAULT_BRANCH_NAME=""
        if [[ -f "$SCRIPT_DIR/lib/default-branch.sh" ]]; then
            # shellcheck source=lib/default-branch.sh
            source "$SCRIPT_DIR/lib/default-branch.sh"
            # shellcheck disable=SC2034  # read inside the evaluated _maybe_delete_local_branch body
            DEFAULT_BRANCH_NAME="$(cd "$REPO_ROOT" && loom_default_branch 2>/dev/null || true)"
        fi

        eval "$_MAYBE_DELETE_DEP_FNS"
        eval "$_MAYBE_DELETE_FN"

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
            _maybe_delete_local_branch "$branch" "$pr_head_sha"
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
