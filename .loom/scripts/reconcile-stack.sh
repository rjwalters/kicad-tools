#!/usr/bin/env bash
# Loom Stacked-PR Reconciliation (issue #3729, stacked-PR v1)
#
# Turns the manual git surgery an operator performs after a stacked parent PR
# squash-merges into one command. The operator (or merge-pr.sh's post-merge
# pass) runs this AFTER the parent branch has squash-merged to the default
# branch.
#
# Usage:
#   ./.loom/scripts/reconcile-stack.sh <child-pr> <parent-branch> [options]
#
# Example (the live #3725-on-#3726 incident, PR #3727):
#   ./.loom/scripts/reconcile-stack.sh 3727 feature/issue-3726
#
# What it does:
#   git rebase --onto <fetched default-branch COMMIT> <parent-ref> <child-branch>
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
# WHERE THE LOGIC LIVES (#8583)
#
# Everything that decides WHAT to rebase onto, FROM where, and in WHICH
# directory — plus the rebase itself — is `loom-daemon reconcile-stack`
# (Rust: loom-daemon/src/reconcile_stack.rs), per
# .loom/docs/shell-language-policy.md. This file keeps its name (merge-pr.sh,
# role prompts and the sweep lifecycle all invoke it by path) and keeps the
# three forge/publish steps: resolve the child branch, force-with-lease push,
# retarget the PR base. New logic goes into the subcommand, not here.
#
# The destination is the commit this run FETCHED from the remote, never the
# local branch of the same name: a stale local default branch made an
# apparently-clean rebase silently drop the just-merged parent's
# implementation (#8583). A fetch failure refuses instead of degrading.
#
# Safety:
#   - Uses --force-with-lease (NEVER a bare --force) so a concurrent push to the
#     child branch aborts the rebase rather than clobbering it.
#   - --dry-run plans (and fetches, and verifies every prerequisite) but
#     mutates no branch and prints the remaining commands.
#   - Refuses on a dirty working tree, a failed fetch, a missing remote
#     default branch, an unresolvable parent ref, and a pinned parent ref that
#     is not an ancestor of the child — each before anything is mutated.
#
# Options:
#   --dry-run   Plan and report without rebasing, pushing, or retargeting.
#   --help,-h   Show this help.
#
# Exit codes:
#   0 = reconciled (or dry-run printed)
#   1 = usage / precondition failure (nothing was mutated)
#   2 = a git/gh step failed (rebase conflict, push rejected, retarget failed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd)" || REPO_ROOT="$PWD"

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
    sed -n '2,58p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

# Resolve the repo's default branch NAME offline, honoring a
# LOOM_DEFAULT_BRANCH override. Same resolver worktree.sh uses. This is the
# `gh pr edit --base` target; the git mutation target is the fetched COMMIT
# the subcommand resolves below, and the two are deliberately kept apart.
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
info "Forge retarget base (default branch name): $DEFAULT_BRANCH"

# The reconciliation planner/executor. Hard requirement, failing CLOSED: with
# no binary there is no verified fetch and no pinned destination, and the only
# thing left to rebase onto would be the stale local branch this exists to
# stop using.
# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
DAEMON_BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT" || true)"
if [[ -z "$DAEMON_BIN" ]]; then
    err "loom-daemon not found — required for 'reconcile-stack' (#8583). Build it (cargo build --release --package loom-daemon) or set LOOM_DAEMON_SELF_BIN=/path/to/loom-daemon. Refusing rather than rebasing onto an unverified destination."
    exit 1
fi

# 1. Fetch + pin the remote default-branch tip, route to the worktree holding
#    the child branch, resolve the parent ref (branch name, else the #7982
#    pin, whose ancestry is checked per #8010), then replay ONLY the child's
#    own commits onto the pinned commit. Exit 1 = a prerequisite refused and
#    nothing was mutated; exit 2 = the rebase itself failed.
# requires-daemon: reconcile-stack >= 0.19.271   #8583 — the reconciliation planner/executor. Declared at this repo's VERSION because the subcommand lands WITH this marker; the first release actually carrying it is the post-merge bump. A binary predating it refuses with clap's "unrecognized subcommand", which this script reports as a precondition failure rather than proceeding
RECONCILE_ARGS=(--child-branch "$CHILD_BRANCH" --parent-branch "$PARENT_BRANCH" --default-branch "$DEFAULT_BRANCH" --repo-dir "$PWD")
[[ "$DRY_RUN" == "true" ]] || RECONCILE_ARGS+=(--rebase)
# Refuse a too-old binary with the floor and the roll command (#8385) rather
# than letting clap's bare "unrecognized subcommand" be the whole diagnosis.
loom_daemon_version_preflight reconcile-stack "$DAEMON_BIN"
info "Step 1/3: $DAEMON_BIN reconcile-stack ${RECONCILE_ARGS[*]}"
PLAN_RC=0
PLAN_OUT="$("$DAEMON_BIN" reconcile-stack "${RECONCILE_ARGS[@]}")" || PLAN_RC=$?
if [[ $PLAN_RC -eq 2 ]]; then
    err "Rebase failed (likely a conflict). Resolve it, then re-run this script or finish manually:"
    echo "    git rebase --continue   # after resolving" >&2
    echo "    git push --force-with-lease" >&2
    echo "    gh pr edit $CHILD_PR --base $DEFAULT_BRANCH" >&2
    exit 2
elif [[ $PLAN_RC -ne 0 ]]; then
    err "Reconciliation refused before '$CHILD_BRANCH' was touched (exit $PLAN_RC) — the failing prerequisite is named above. If it reads 'unrecognized subcommand', this loom-daemon predates #8583: rebuild it."
    exit 1
fi
# stdout is `eval`-able assignments, printed only once every prerequisite held:
# LOOM_RS_TARGET_COMMIT / _TARGET_REF / _GIT_DIR / _CHILD_WORKTREE /
# _PARENT_REF / _PARENT_PIN_REF.
eval "$PLAN_OUT"

# Version-bearing-file sync gate (#7168, extended #7341): `rebase --onto`
# replays ONLY the child's own commits, so it silently absorbs whatever
# version-bearing values the destination already had. A file the child's own
# commits never touched (in practice .loom/install-metadata.json) never raises
# a git conflict, so it can end up stale relative to VERSION/the files that
# WERE part of the rebase — invisible until CI's "Installer Integration Tests"
# fails. Run the shared gate in whichever directory the rebase actually ran.
# Skipped under --dry-run: no rebase happened, so there is nothing to check.
if [[ "$DRY_RUN" != "true" ]] && [[ -x "$SCRIPT_DIR/version-check-gate.sh" ]]; then
    if ! (cd "$LOOM_RS_GIT_DIR" && "$SCRIPT_DIR/version-check-gate.sh" --fix-hint "then push."); then
        err "Version-bearing files are out of sync for '$CHILD_BRANCH' after rebase onto $LOOM_RS_TARGET_COMMIT (see BLOCKER:/Fix: above)."
        exit 2
    fi
fi

run() {
    echo -e "${YELLOW}\$ $*${NC}" >&2
    if [[ "$DRY_RUN" == "true" ]]; then
        return 0
    fi
    "$@"
}

# GIT_C runs in the directory the rebase ran in (the child worktree when one
# holds the branch, else here), so the current branch there is the child.
GIT_C=(git -C "$LOOM_RS_GIT_DIR")

# 2. Publish the rewritten child branch. --force-with-lease (never bare
#    --force) so a concurrent push aborts rather than clobbers.
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

# 3. Retarget the child PR's base to the default branch NAME (not the commit —
#    a PR base is a branch).
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
    # unrelated child of the same name. Only when the fallback was actually
    # USED — $LOOM_RS_PARENT_PIN_REF is empty otherwise. Best-effort: a failure
    # here costs a stale ref, never the reconcile that already succeeded.
    if [[ -n "$LOOM_RS_PARENT_PIN_REF" ]]; then
        if "${GIT_C[@]}" update-ref -d "$LOOM_RS_PARENT_PIN_REF" 2>/dev/null; then
            info "Reaped the consumed pin $LOOM_RS_PARENT_PIN_REF."
        else
            warn "Could not delete the consumed pin $LOOM_RS_PARENT_PIN_REF — harmless, but remove it by hand if a later child reuses '$PARENT_BRANCH'."
        fi
    fi
    ok "Reconciled: PR #$CHILD_PR now stacks only its own commits on $DEFAULT_BRANCH ($LOOM_RS_TARGET_COMMIT)."
fi
