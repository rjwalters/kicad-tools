#!/usr/bin/env bash
# Loom Stacked-PR Rebase-on-Parent-Amend (issue #3747, stacked-PR v2 item 3)
#
# Sibling of reconcile-stack.sh. Where reconcile-stack.sh handles the
# post-squash-merge moment (strip the parent's now-squashed commits off a child
# and retarget its base to the default branch), THIS script handles the much
# more common PRE-merge moment: a stacked parent's PR (feature/issue-<parent>)
# is still open and under review, Doctor amends the parent branch (interactive
# rewrite or additive commits + force-with-lease), and any child that branched
# off the parent's *pre-amend* tip is now silently stale. This tool detects that
# and rebases the stale children back onto the parent's current tip — WITHOUT
# retargeting the child PR's base (the child stays stacked on the parent).
#
# Usage:
#   ./.loom/scripts/rebase-stacked-children.sh <parent-branch> [--dry-run]
#
# Example:
#   ./.loom/scripts/rebase-stacked-children.sh feature/issue-3726
#
# What it does, per open child PR based on <parent-branch>:
#   1. Discovery — a LIVE forge query, identical shape to merge-pr.sh's
#      auto-reconcile / merge-ordering guard:
#        gh pr list --repo <nwo> --base <parent-branch> --state open
#   2. Staleness — git fetch, then `git merge-base --is-ancestor
#      origin/<parent-branch> origin/<child-branch>`. If the parent tip IS an
#      ancestor of the child, the child already has everything — skip. If NOT,
#      the child is stale relative to the parent's current tip.
#   3. Safe/unsafe split, keyed on the child ISSUE's loom:building label (fresh,
#      uncached `gh api` read — mirrors merge-pr.sh item 1):
#        - Safe   (child issue NOT loom:building): rebase directly —
#            git rebase origin/<parent-branch> <child-branch>
#            git push --force-with-lease
#          NO PR base retarget: the child's PR base stays <parent-branch>.
#        - Unsafe (child issue still loom:building): a live Builder likely has
#          the child branch checked out — skip the rebase (never force-push over
#          in-progress work) and post a deferred-rebase comment on the child PR.
#
# Safety:
#   - Uses --force-with-lease (NEVER a bare --force) so a concurrent push to the
#     child branch aborts rather than clobbers.
#   - --dry-run reports the per-child outcome (no-op / would-rebase / would-defer)
#     without executing any git/gh mutation.
#   - A rebase conflict on one child does NOT abort the whole run — it is a
#     normal, recoverable failure (final exit 2) with manual-recovery hints; the
#     conflicted rebase is aborted so remaining children still process.
#   - Refuses to run with a dirty working tree (a rebase would fail confusingly).
#   - GitHub-only, and only for a <parent-branch> matching feature/issue-<N>.
#
# Options:
#   --dry-run   Report the per-child outcome without executing git/gh mutations.
#   --help,-h   Show this help.
#
# Exit codes:
#   0 = rebased, deferred, or no-op (or dry-run printed)
#   1 = usage / precondition failure
#   2 = a git/gh step failed (rebase conflict, rejected force-with-lease push)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors (skip when not a TTY).
if [[ -t 2 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi
err()  { echo -e "${RED}ERROR: $1${NC}" >&2; }
ok()   { echo -e "${GREEN}✓ $1${NC}" >&2; }
info() { echo -e "${BLUE}ℹ $1${NC}" >&2; }
warn() { echo -e "${YELLOW}⚠ $1${NC}" >&2; }

show_help() {
    sed -n '2,56p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

DRY_RUN=false
PARENT_BRANCH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --help|-h) show_help; exit 0 ;;
        --*) err "Unknown flag: $1"; exit 1 ;;
        *)
            if [[ -z "$PARENT_BRANCH" ]]; then
                PARENT_BRANCH="$1"
            else
                err "Unexpected argument: $1"; exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$PARENT_BRANCH" ]]; then
    err "Usage: rebase-stacked-children.sh <parent-branch> [--dry-run]"
    echo "  Example: rebase-stacked-children.sh feature/issue-3726" >&2
    exit 1
fi

# Source forge helpers for multi-forge detection (mirrors merge-pr.sh). The
# GitHub-only / feature-branch guards live in _rebase_stacked_children so a
# non-GitHub forge or non-feature parent is a clean no-op (exit 0).
# shellcheck source=lib/forge-helpers.sh
source "$SCRIPT_DIR/lib/forge-helpers.sh"
forge_detect

REPO_NWO="$(forge_get_repo_nwo "gh" 2>/dev/null || true)"

# ---- core reconciliation functions (extracted by tests) ----
# Execute a mutating command, or (under --dry-run) print what would run without
# executing it. Read-only staleness probes (git fetch / merge-base) do NOT go
# through run() — only the child-mutating rebase/push/comment do.
run() {
    if [[ "$DRY_RUN" == "true" ]]; then
        echo -e "${YELLOW}[dry-run] would: $*${NC}" >&2
        return 0
    fi
    "$@"
}

# Rebase (or defer) one discovered child PR relative to the parent's current
# tip. Sets RSC_FAILURE=2 on a git/gh step failure but always returns 0 so the
# caller keeps processing the remaining children.
_process_one_stacked_child() {
    local child_pr="$1" child_branch="$2" parent_branch="$3"

    # Fetch the parent + child tips so the staleness check reflects the current
    # remote state, not a stale local view. Read-only w.r.t. the remote.
    if ! git fetch origin "$parent_branch" "$child_branch" >/dev/null 2>&1; then
        warn "Could not fetch origin refs for '$parent_branch' / '$child_branch' — skipping child PR #$child_pr"
        return 0
    fi

    # Up-to-date: is the parent's current tip already an ancestor of the child?
    # If so the child already contains everything the parent has — no-op.
    if git merge-base --is-ancestor "origin/$parent_branch" "origin/$child_branch" >/dev/null 2>&1; then
        info "Child PR #$child_pr ($child_branch) already contains parent tip — up to date, skipping"
        return 0
    fi

    # Stale relative to the parent's current tip. Derive the child ISSUE number
    # from its head branch (feature/issue-<N>) for the safe/unsafe split. A
    # non-feature/issue-N child has no loom:building claim to race → treated safe.
    local child_issue=""
    if [[ "$child_branch" =~ ^feature/issue-([0-9]+)$ ]]; then
        child_issue="${BASH_REMATCH[1]}"
    fi

    # Fresh (uncached) label read — mirrors merge-pr.sh's _reconcile_one_stacked_child:
    # plain `gh api` (never a gh-cache) so a stale cached view cannot mask a live
    # re-claim. A read failure is treated as "not building" (safe); force-with-lease
    # still protects the branch on the safe path.
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
        # Unsafe: a live Builder likely holds the child branch checked out. Skip
        # the rebase and post a deferred-rebase comment (mirrors item 1's format).
        info "Child PR #$child_pr (issue #$child_issue) is still loom:building — deferring rebase to avoid racing a live Builder"
        local ts comment
        ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        comment="## Stacked parent advanced — rebase deferred

Parent branch \`$parent_branch\` advanced (amended/pushed) after this child branched off it, so this PR is now **stale** relative to the parent's current tip. But issue #$child_issue is still \`loom:building\` — a Builder likely has this branch checked out. The auto-rebase was **skipped** to avoid racing that in-progress work with an out-of-band \`git rebase\` + \`push --force-with-lease\`.

**What happens next**: once issue #$child_issue is no longer \`loom:building\`, re-run this script to rebase the child onto the parent's current tip (the child's PR base stays \`$parent_branch\` — it remains stacked):

\`\`\`
./.loom/scripts/rebase-stacked-children.sh $parent_branch
\`\`\`

---
*Deferred by rebase-stacked-children.sh (#3747) at $ts*"
        if ! run gh pr comment "$child_pr" --repo "$REPO_NWO" --body "$comment"; then
            warn "Could not post deferred-rebase comment on PR #$child_pr"
        fi
        return 0
    fi

    # Safe: no live claim — rebase the child onto the parent's current tip and
    # force-push. NO PR base retarget (unlike reconcile-stack.sh's post-merge
    # case): the parent has not merged, so the child stays stacked on it.
    info "Child PR #$child_pr ($child_branch) is stale relative to '$parent_branch' — rebasing onto origin/$parent_branch"
    if ! run git rebase "origin/$parent_branch" "$child_branch"; then
        err "Rebase of '$child_branch' onto 'origin/$parent_branch' hit a conflict."
        echo "    Resolve it, then finish manually:" >&2
        echo "    git rebase origin/$parent_branch $child_branch   # then, after resolving each conflict:" >&2
        echo "    git rebase --continue" >&2
        echo "    git push --force-with-lease" >&2
        # Abort the conflicted rebase so the remaining children can still process
        # (best-effort; the whole run is not aborted by one child's conflict).
        git rebase --abort >/dev/null 2>&1 || true
        RSC_FAILURE=2
        return 0
    fi
    if ! run git push --force-with-lease; then
        err "force-with-lease push rejected for '$child_branch' (someone else pushed). Fetch, review, and retry."
        RSC_FAILURE=2
        return 0
    fi
    ok "Rebased child PR #$child_pr ($child_branch) onto origin/$parent_branch and force-pushed (base unchanged, still stacked on $parent_branch)"
    return 0
}

# Discover open child PRs stacked on <parent-branch> and rebase (or defer) each
# stale one. GitHub-only, feature/issue-<N> parent only; a no-op otherwise.
_rebase_stacked_children() {
    local parent_branch="$1"

    [[ "$FORGE_TYPE" == "github" ]] || { info "Forge is '${FORGE_TYPE:-unknown}', not github — nothing to do"; return 0; }
    [[ "$parent_branch" =~ ^feature/issue-([0-9]+)$ ]] || { info "Parent branch '$parent_branch' is not a feature/issue-<N> branch — nothing to do"; return 0; }

    # Live forge discovery — same shape as merge-pr.sh's item 1/2 query. Plain
    # `gh` (uncached) so we see child PRs as of right now.
    local children_json
    children_json="$(gh pr list --repo "$REPO_NWO" --base "$parent_branch" --state open \
        --json number,headRefName 2>/dev/null || echo '[]')"
    [[ -n "$children_json" ]] || return 0

    local count
    count="$(echo "$children_json" | jq 'length' 2>/dev/null || echo 0)"
    if [[ "$count" -eq 0 ]]; then
        info "No open child PRs target '$parent_branch' — nothing to rebase"
        return 0
    fi

    info "Found $count open child PR(s) based on '$parent_branch'"

    local rows child_pr child_branch
    rows="$(echo "$children_json" | jq -r '.[] | "\(.number)\t\(.headRefName)"' 2>/dev/null || true)"
    while IFS=$'\t' read -r child_pr child_branch; do
        [[ -n "$child_pr" ]] || continue
        _process_one_stacked_child "$child_pr" "$child_branch" "$parent_branch"
    done <<< "$rows"

    return 0
}
# ---- main ----

# Refuse on a dirty working tree — a rebase switches branches and would fail
# confusingly. Skipped under --dry-run (which never mutates the tree). Only
# relevant when the parent branch actually gates a real rebase below.
if [[ "$DRY_RUN" != "true" ]] \
   && [[ "$FORGE_TYPE" == "github" ]] \
   && [[ "$PARENT_BRANCH" =~ ^feature/issue-([0-9]+)$ ]] \
   && [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    err "Working tree is dirty. Commit, stash, or discard changes before rebasing stacked children."
    exit 1
fi

RSC_FAILURE=0
_rebase_stacked_children "$PARENT_BRANCH"

if [[ "$DRY_RUN" == "true" ]]; then
    ok "Dry run complete — no changes made. Re-run without --dry-run to rebase stale children."
    exit 0
fi

if [[ "$RSC_FAILURE" -ne 0 ]]; then
    err "One or more children failed to rebase (see above). Resolve them and re-run."
    exit "$RSC_FAILURE"
fi

exit 0
