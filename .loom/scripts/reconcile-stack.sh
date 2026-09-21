#!/usr/bin/env bash
# Loom Stacked-PR Reconciliation (issue #3729, stacked-PR v1)
#
# Turns the manual git surgery an operator performs after a stacked parent PR
# squash-merges into one command. This is the v1 manual-but-scripted
# reconciliation step: merge-pr.sh is intentionally NOT modified in v1, and no
# automatic merge-ordering guard is added. The operator runs this by hand AFTER
# confirming the parent branch has squash-merged to the default branch.
#
# Usage:
#   ./.loom/scripts/reconcile-stack.sh <child-pr> <parent-branch> [options]
#
# Example (the live #3725-on-#3726 incident, PR #3727):
#   ./.loom/scripts/reconcile-stack.sh 3727 feature/issue-3726
#
# What it does (equivalent to the three-command manual workaround):
#   git rebase --onto <default-branch> <parent-branch> <child-branch>
#   git push --force-with-lease
#   gh pr edit <child-pr> --base <default-branch>
#
# The repo squash-merges (setup-repository-settings.sh: squash only), so after
# the parent squash-merges to the default branch as ONE commit, the child
# branch still carries the parent's ORIGINAL pre-squash commits. A naive base
# retarget (child base -> default) then re-shows the parent's entire diff. The
# `git rebase --onto` replays ONLY the child's own commits onto the default
# branch, stripping the parent's now-squashed commits, before retargeting.
#
# Safety:
#   - Uses --force-with-lease (NEVER a bare --force) so a concurrent push to the
#     child branch aborts the rebase rather than clobbering it.
#   - --dry-run prints every command without executing.
#   - Refuses to run with a dirty working tree (a rebase would fail confusingly).
#
# Parent-ref fallback (#7982): if <parent-branch> no longer resolves locally
# (delete_branch_on_merge removed it once the parent PR merged), this script
# falls back to refs/loom/parent/<parent-branch> — a ref merge-pr.sh's
# merge-ordering guard pins to the parent's pre-merge tip before merging a
# stacked parent. That ref is shared across every worktree of the same repo.
#
# Options:
#   --dry-run   Print the commands that would run without executing them.
#   --help,-h   Show this help.
#
# Exit codes:
#   0 = reconciled (or dry-run printed)
#   1 = usage / precondition failure
#   2 = a git/gh step failed (rebase conflict, push rejected, retarget failed)

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
    sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

DRY_RUN=false
CHILD_PR=""
PARENT_BRANCH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --help|-h) show_help; exit 0 ;;
        --*) err "Unknown flag: $1"; exit 1 ;;
        *)
            if [[ -z "$CHILD_PR" ]]; then
                CHILD_PR="$1"
            elif [[ -z "$PARENT_BRANCH" ]]; then
                PARENT_BRANCH="$1"
            else
                err "Unexpected argument: $1"; exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$CHILD_PR" || -z "$PARENT_BRANCH" ]]; then
    err "Usage: reconcile-stack.sh <child-pr> <parent-branch> [--dry-run]"
    echo "  Example: reconcile-stack.sh 3727 feature/issue-3726" >&2
    exit 1
fi

if ! [[ "$CHILD_PR" =~ ^[0-9]+$ ]]; then
    err "Child PR must be numeric (got: '$CHILD_PR')"
    exit 1
fi

# Resolve the repo's default branch (the rebase --onto target) offline, honoring
# a LOOM_DEFAULT_BRANCH override. Same resolver worktree.sh uses.
DEFAULT_BRANCH="main"
if [[ -f "$SCRIPT_DIR/lib/default-branch.sh" ]]; then
    # shellcheck source=lib/default-branch.sh
    source "$SCRIPT_DIR/lib/default-branch.sh"
    if resolved="$(loom_default_branch 2>/dev/null)" && [[ -n "$resolved" ]]; then
        DEFAULT_BRANCH="$resolved"
    fi
fi

# Discover the child branch from the PR (GitHub via gh).
if ! command -v gh >/dev/null 2>&1; then
    err "gh CLI not found — required to resolve the child PR's head branch and retarget its base."
    exit 1
fi

info "Resolving head branch for child PR #$CHILD_PR..."
CHILD_BRANCH="$(gh pr view "$CHILD_PR" --json headRefName --jq '.headRefName' 2>/dev/null || true)"
if [[ -z "$CHILD_BRANCH" ]]; then
    err "Could not resolve the head branch for PR #$CHILD_PR (is the number correct and the PR open?)."
    exit 1
fi
info "Child branch: $CHILD_BRANCH"
info "Parent branch: $PARENT_BRANCH"
info "Rebase target (default branch): $DEFAULT_BRANCH"

# Best-effort advisory: has the parent branch squash-merged? If origin still has
# the parent branch, the operator likely hasn't merged it yet — warn but do not
# block (the operator asserts they've confirmed the merge).
#
# Query the remote LIVE (git ls-remote) rather than trusting the local
# remote-tracking ref: with delete-branch-on-merge the parent branch is deleted
# on the remote the instant it squash-merges, but the local
# refs/remotes/origin/<parent> can linger stale until a prune — a plain
# `git show-ref` of that stale ref then false-warns on every post-merge
# reconcile (#3776). ls-remote asks the actual remote, so a stale local ref
# never produces a spurious warning. A network/ls-remote failure simply skips
# the advisory (it was only ever advisory).
git fetch origin "$DEFAULT_BRANCH" 2>/dev/null || true
if git ls-remote --exit-code --heads origin "refs/heads/$PARENT_BRANCH" >/dev/null 2>&1; then
    warn "origin/$PARENT_BRANCH still exists — confirm the parent PR has squash-merged before reconciling."
    warn "(delete-branch-on-merge normally removes it once the parent merges.)"
fi

# Locate the worktree (if any) that holds the child branch checked out. A Loom
# child branch is ALWAYS checked out in its own managed worktree
# (.loom/worktrees/issue-<child>), and git refuses to rebase a branch that is
# checked out in another worktree (`fatal: '<branch>' is already used by
# worktree at ...`) — so running the rebase from the main worktree can never
# work for a live Loom stack (#3776). Detect that worktree via
# `git worktree list --porcelain` and run the rebase/push INSIDE it. When no
# worktree holds the branch (e.g. it was already removed), fall back to the
# in-place rebase, which checks the branch out in the current worktree — the
# original v1 behaviour, unchanged.
CHILD_WORKTREE=""
_wt=""
while IFS= read -r _line; do
    case "$_line" in
        "worktree "*) _wt="${_line#worktree }" ;;
        "branch refs/heads/$CHILD_BRANCH") CHILD_WORKTREE="$_wt" ;;
    esac
done < <(git worktree list --porcelain 2>/dev/null || true)

# GIT_C is the git invocation used for the branch-mutating steps (rebase, push).
# Inside the child worktree when one exists, else the current worktree.
if [[ -n "$CHILD_WORKTREE" ]]; then
    info "Child branch $CHILD_BRANCH is checked out in worktree: $CHILD_WORKTREE — running the rebase there."
    GIT_C=(git -C "$CHILD_WORKTREE")
else
    GIT_C=(git)
fi

# Refuse on a dirty working tree — a rebase would fail confusingly. Check the
# tree the rebase will actually run in (the child worktree when one exists).
if [[ -n "$("${GIT_C[@]}" status --porcelain 2>/dev/null)" ]]; then
    err "Working tree is dirty${CHILD_WORKTREE:+ (worktree: $CHILD_WORKTREE)}. Commit, stash, or discard changes before reconciling."
    exit 1
fi

# Resolve the parent ref to rebase --onto FROM (#7982). By the time this
# script runs, the parent branch's remote (and, once merge-pr.sh's own
# worktree/branch cleanup has run, local) copy is often already gone —
# delete_branch_on_merge removes it the instant the parent PR merges — so the
# literal branch name may no longer resolve as a ref at all. merge-pr.sh's
# merge-ordering guard pins the parent's pre-merge tip to
# refs/loom/parent/<branch> before merging a stacked parent; that ref is a
# plain (non-per-worktree) ref, so it is visible from every worktree of the
# SAME repository regardless of which worktree wrote it. Prefer the literal
# branch name when it still resolves (keeps direct/legacy invocations
# unchanged); fall back to the pinned ref only when it doesn't.
PARENT_REF="$PARENT_BRANCH"
if ! "${GIT_C[@]}" rev-parse --verify --quiet "${PARENT_BRANCH}^{commit}" >/dev/null 2>&1; then
    PINNED_PARENT_REF="refs/loom/parent/$PARENT_BRANCH"
    if "${GIT_C[@]}" rev-parse --verify --quiet "${PINNED_PARENT_REF}^{commit}" >/dev/null 2>&1; then
        # ANCESTRY IS CHECKED, NOT ASSUMED (#8010 item 4).
        #
        # `git rebase --onto <default> <upstream> <child>` replays
        # `(upstream..child]`. If <upstream> is not an ancestor of the child,
        # that command does NOT error — it silently replays the wrong commit
        # range, and the child PR gains commits it should never have had.
        #
        # A stale pin is reachable here, not hypothetical: nothing reaped these
        # refs before this change, and `feature/issue-N` branch names ARE reused
        # in this repo (see CLAUDE.md's #5657/#3667 note on a partial-increment
        # slice's branch name being reused by the next slice). A pin left by an
        # earlier merge of the SAME name would otherwise be preferred without
        # complaint.
        if "${GIT_C[@]}" merge-base --is-ancestor "$PINNED_PARENT_REF" "$CHILD_BRANCH" 2>/dev/null; then
            warn "Branch '$PARENT_BRANCH' no longer resolves locally (likely deleted by delete_branch_on_merge) — falling back to the pinned ref $PINNED_PARENT_REF."
            PARENT_REF="$PINNED_PARENT_REF"
        else
            err "Branch '$PARENT_BRANCH' no longer resolves locally, and the pinned ref $PINNED_PARENT_REF ($("${GIT_C[@]}" rev-parse --short "$PINNED_PARENT_REF" 2>/dev/null || echo '?')) is NOT an ancestor of '$CHILD_BRANCH'."
            err ""
            err "Rebasing onto a non-ancestor would not fail — it would replay the wrong commit range and silently add commits to PR #$CHILD_PR. Refusing."
            err ""
            err "This usually means the pin is stale: the branch name was reused by a later issue slice, and an older merge left the ref behind. Verify which parent this child was actually built on, then either:"
            err "  git update-ref refs/loom/parent/$PARENT_BRANCH <the-correct-tip>"
            err "  git update-ref -d refs/loom/parent/$PARENT_BRANCH   # then rebase by hand"
            exit 1
        fi
    fi
fi

run() {
    echo -e "${YELLOW}\$ $*${NC}" >&2
    if [[ "$DRY_RUN" == "true" ]]; then
        return 0
    fi
    "$@"
}

# 1. Replay ONLY the child's own commits onto the default branch, stripping the
#    parent's now-squashed pre-merge commits. Runs inside the child worktree when
#    one holds the branch (so git does not reject the checked-out branch).
info "Step 1/3: rebase --onto $DEFAULT_BRANCH $PARENT_REF $CHILD_BRANCH"
if ! run "${GIT_C[@]}" rebase --onto "$DEFAULT_BRANCH" "$PARENT_REF" "$CHILD_BRANCH"; then
    err "Rebase failed (likely a conflict). Resolve it, then re-run this script or finish manually:"
    echo "    git rebase --continue   # after resolving" >&2
    echo "    git push --force-with-lease" >&2
    echo "    gh pr edit $CHILD_PR --base $DEFAULT_BRANCH" >&2
    exit 2
fi

# Version-bearing-file sync gate (#7168, extended #7341): `rebase --onto`
# replays ONLY the child's own commits, so it silently absorbs whatever
# version-bearing values $DEFAULT_BRANCH already had. A file the child's own
# commits never touched (in practice .loom/install-metadata.json) never
# raises a git conflict, so it can end up stale relative to VERSION/the files
# that WERE part of the rebase -- invisible until CI's "Installer Integration
# Tests" fails. This script had the identical gap rebase-stacked-children.sh
# was fixed for in #7168/#7171 (both rebase + force-push directly, never
# through create-pr.sh) -- run the same shared gate here too, in whichever
# directory the rebase actually ran (the child worktree when one holds the
# branch, else the current directory). Skipped under --dry-run: no rebase
# actually happened, so there is nothing new to check.
if [[ "$DRY_RUN" != "true" ]] && [[ -x "$SCRIPT_DIR/version-check-gate.sh" ]]; then
    GATE_CWD="${CHILD_WORKTREE:-.}"
    if ! (cd "$GATE_CWD" && "$SCRIPT_DIR/version-check-gate.sh" --fix-hint "then push."); then
        err "Version-bearing files are out of sync for '$CHILD_BRANCH' after rebase onto '$DEFAULT_BRANCH' (see BLOCKER:/Fix: above)."
        exit 2
    fi
fi

# 2. Publish the rewritten child branch. --force-with-lease (never bare --force)
#    so a concurrent push aborts rather than clobbers. Pushed from the same
#    worktree the rebase ran in, so the current branch there is the child branch.
info "Step 2/3: push --force-with-lease"
if ! run "${GIT_C[@]}" push --force-with-lease; then
    # A reported rejection is not always a real one (#6695): Git LFS's
    # pre-push hook can race the lease re-check on a branch with pending LFS
    # objects, so the ref update lands while the printed rejection reflects a
    # stale read. Verify the LIVE remote ref before trusting the reported
    # failure — never a local remote-tracking ref, which is not re-fetched here.
    PUSH_RACE_SHA="$("${GIT_C[@]}" rev-parse "$CHILD_BRANCH" 2>/dev/null || true)"
    # shellcheck source=lib/push-lease-verify.sh
    source "$SCRIPT_DIR/lib/push-lease-verify.sh"
    if [[ "$DRY_RUN" != "true" ]] && push_landed_despite_rejection origin "$CHILD_BRANCH" "$PUSH_RACE_SHA" "${GIT_C[@]}"; then
        warn "PUSH-LEASE-RACE-DETECTED: push --force-with-lease reported a rejection for '$CHILD_BRANCH', but origin already reflects the update ($PUSH_RACE_SHA) — likely the Git LFS pre-push hook racing the lease re-check (#6695). Treating as landed and continuing."
    else
        err "Force-with-lease push was rejected (someone else pushed to $CHILD_BRANCH). Fetch, review, and retry."
        exit 2
    fi
fi

# 3. Retarget the child PR's base to the default branch.
info "Step 3/3: gh pr edit $CHILD_PR --base $DEFAULT_BRANCH"
if ! run gh pr edit "$CHILD_PR" --base "$DEFAULT_BRANCH"; then
    err "Failed to retarget PR #$CHILD_PR base to $DEFAULT_BRANCH. Retarget it manually."
    exit 2
fi

if [[ "$DRY_RUN" == "true" ]]; then
    ok "Dry run complete — no changes made. Re-run without --dry-run to reconcile."
else
    # Reap the pin once it has been consumed (#8010 item 4). Leaving it is what
    # makes a stale pin reachable at all: `feature/issue-N` names are reused in
    # this repo, so a ref left behind by one merge can be picked up by a later,
    # unrelated child of the same name. The ancestry check above refuses that
    # case loudly, but not leaving the landmine is better than detecting it.
    #
    # Only when the fallback was actually USED — a direct invocation that
    # resolved the live branch must not delete a pin some other reconcile still
    # needs. Best-effort: a failure here costs a stale ref, never the reconcile
    # that already succeeded.
    if [[ -n "${PINNED_PARENT_REF:-}" && "$PARENT_REF" == "$PINNED_PARENT_REF" ]]; then
        if "${GIT_C[@]}" update-ref -d "$PINNED_PARENT_REF" 2>/dev/null; then
            info "Reaped the consumed pin $PINNED_PARENT_REF."
        else
            warn "Could not delete the consumed pin $PINNED_PARENT_REF — harmless, but remove it by hand if a later child reuses '$PARENT_BRANCH'."
        fi
    fi
    ok "Reconciled: PR #$CHILD_PR now stacks only its own commits on $DEFAULT_BRANCH."
fi
