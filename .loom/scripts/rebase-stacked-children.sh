#!/usr/bin/env bash
# Loom Stacked-PR Rebase-on-Parent-Amend (issue #3747, stacked-PR v2 item 3)
#
# Sibling of reconcile-stack.sh. Where reconcile-stack.sh handles the
# post-squash-merge moment (strip the parent's now-squashed commits off a child
# and retarget its base to the default branch), THIS script handles the much
# more common PRE-merge moment: a stacked parent's PR (any branch name)
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
#            git push --force-with-lease=<child-branch>:<live remote head,
#                                         pinned before the rebase> origin …
#            (that argument is `loom-daemon push-lease pin-flag`'s output)
#          NO PR base retarget: the child's PR base stays <parent-branch>.
#        - Unsafe (child issue still loom:building): a live Builder likely has
#          the child branch checked out — skip the rebase (never force-push over
#          in-progress work) and post a deferred-rebase comment on the child PR.
#
# Safety:
#   - Uses --force-with-lease (NEVER a bare --force) so a concurrent push to the
#     child branch aborts rather than clobbers, PINNED (#9487) to the child's
#     live remote head read before the rebase: the bare flag compares against
#     refs/remotes/origin/<child>, a ref shared by every linked worktree of
#     this clone that a sibling agent's fetch can advance out from under this
#     run, which defeats the lease entirely. See
#     defaults/docs/push-lease-pinning.md.
#   - Skips a child whose local branch has not incorporated origin's head —
#     the pin would be accurate and the push would still delete commits.
#   - Skips a child whose pinned lease cannot be built AT ALL (unreadable
#     remote, no loom-daemon): there is no bare-lease fallback.
#   - --dry-run reports the per-child outcome (no-op / would-rebase / would-defer)
#     without executing any git/gh mutation.
#   - A rebase conflict on one child does NOT abort the whole run — it is a
#     normal, recoverable failure (final exit 2) with manual-recovery hints; the
#     conflicted rebase is aborted so remaining children still process.
#   - Refuses to run with a dirty working tree (a rebase would fail confusingly).
#   - GitHub-only. <parent-branch> may have any name; a child that is not a
#     feature/issue-<N> branch has no loom:building claim to race, so it is safe.
#   - Rebases only where `loom-daemon branch-update strategy` says `rebase`
#     (#10771). In a merge-only repo (the default answer, and the answer when
#     anything is unknown) a stale child is NEVER rewritten; it is de-stacked by
#     a merge instead (#9808), one child at a time, in a throwaway worktree:
#       - skipped (logged) when its issue is loom:building or its PR carries
#         loom:treating / loom:reviewing;
#       - `git merge origin/<parent-branch>`; on conflict the merge is aborted,
#         nothing is pushed, and ONE comment (files + the local command) is
#         posted per parent tip;
#       - a clean merge must pass version-check-gate.sh and the fast-tier
#         compile check (buildGate.fastCommand, else LOOM_BUILD_GATE_TIER=fast
#         buildGate.command) before a plain fast-forward push. No force flag of
#         any kind; a rejected push is reported, never retried.
#     --dry-run reports would-merge / would-skip per child.
#
# Options:
#   --dry-run   Report the per-child outcome without executing git/gh mutations.
#   --help,-h   Show this help.
#
# Exit codes:
#   0 = rebased, merged, deferred, skipped, handed off on a conflict, or no-op (or dry-run)
#   1 = usage / precondition failure
#   2 = a git/gh step failed (rebase conflict, rejected push, red verification on a merge)

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
    sed -n '2,81p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

# Verifies the actual post-push ref state when a --force-with-lease push
# reports a rejection (#6695) — see lib/push-lease-verify.sh for why this
# is needed (Git LFS pre-push hook racing the lease re-check).
# shellcheck source=lib/push-lease-verify.sh
source "$SCRIPT_DIR/lib/push-lease-verify.sh"

# The PINNED --force-with-lease argument every child push below uses is built
# by `loom-daemon push-lease pin-flag` (loom-daemon/src/cli/push_lease.rs), per
# .loom/docs/shell-language-policy.md: a bare --force-with-lease compares
# against refs/remotes/origin/<child>, a ref SHARED by every linked worktree of
# this clone that a sibling agent's fetch can advance — after which the lease is
# satisfied by construction and the push silently deletes the sibling's commit
# (the live PR #9483 incident, #9487). Finding the binary is part of that
# call-site; an unresolvable one leaves $DAEMON_BIN empty, and the per-child
# call then fails closed (the child is SKIPPED, never pushed with a bare lease).
# requires-daemon: push-lease >= 0.19.1080   #9487 — the pinned-lease builder. Declared at this repo's VERSION because the subcommand lands WITH this marker; the first release actually carrying it is the post-merge bump. A binary predating it is refused here with the floor and the roll command rather than degraded to the bare lease
# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
DAEMON_BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$SCRIPT_DIR/../.." || true)"
loom_daemon_version_preflight push-lease "$DAEMON_BIN"

# Ref-operand validator (#9106). REQUIRED, not defensive: every git call in this
# script takes a branch name as a bare operand, and the child names come
# straight off the forge (`gh pr list --json headRefName`). A missing validator
# must stop the run, never degrade it to the unvalidated behaviour.
# shellcheck source=lib/default-branch.sh
source "$SCRIPT_DIR/lib/default-branch.sh"
# #10771: may PR branches here be rebased at all? A rebase + force-push in a
# repo that bars rebase deletes other agents' merge commits and orphans every
# head a reviewer signed. Anything but an explicit `rebase` answer from
# `loom-daemon branch-update strategy` — including a binary predating the
# subcommand — is merge, which reports every stale child and rewrites none.
# requires-daemon: branch-update optional   #10771 — a binary without it answers nothing, which is read as merge: children are reported, never rewritten
BRANCH_UPDATE_STRATEGY="$("$DAEMON_BIN" branch-update strategy 2>/dev/null)" || BRANCH_UPDATE_STRATEGY=merge

REPO_NWO="$(forge_get_repo_nwo "gh" 2>/dev/null || true)"
# #9548: a real run pushes child branches and comments on child PRs, so it acts
# only on a repo this installation manages and can write. forge_get_repo_nwo is
# `gh repo view`, which prefers an `upstream` remote; the vetted repo replaces it.
[[ "$DRY_RUN" == "true" ]] || REPO_NWO="$(loom_write_repo "${LOOM_REPO:-}")" || { err "Refusing: loom-daemon forge may-write refused this checkout's repo (#9548)."; exit 1; }

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

# Is this child in flight? Prints the reason and returns 0 when the child ISSUE
# is loom:building or the child PR carries loom:treating / loom:reviewing: a live
# agent may be pushing to the branch. Fresh (uncached) `gh api` reads, as above;
# a read failure counts as "not in flight" (the push below is a plain
# fast-forward, so a missed claim costs a rejected push, never lost work).
_stacked_child_in_flight() {
    local child_pr="$1" child_branch="$2" labels=""
    if [[ "$child_branch" =~ ^feature/issue-([0-9]+)$ ]]; then
        labels="$(gh api "repos/$REPO_NWO/issues/${BASH_REMATCH[1]}" 2>/dev/null | jq -r '.labels[]?.name' 2>/dev/null || true)"
        if printf '%s\n' "$labels" | grep -qx 'loom:building'; then
            echo "issue #${BASH_REMATCH[1]} is loom:building"; return 0
        fi
    fi
    labels="$(gh api "repos/$REPO_NWO/issues/$child_pr" 2>/dev/null | jq -r '.labels[]?.name' 2>/dev/null || true)"
    local claim
    for claim in loom:treating loom:reviewing; do
        if printf '%s\n' "$labels" | grep -qx "$claim"; then
            echo "PR #$child_pr carries $claim"; return 0
        fi
    done
    return 1
}

# Remove a throwaway checkout made by _merge_one_stacked_child.
_destack_cleanup_checkout() {
    rm -rf "$1"
    git worktree prune >/dev/null 2>&1 || true
}

# Run the repo's fast-tier compile check (#11690) inside checkout $1: buildGate.
# fastCommand, else buildGate.command with LOOM_BUILD_GATE_TIER=fast. No gate
# configured (or enabled:false) is a warned pass, not a failure.
_destack_compile_check() {
    local checkout="$1" cfg="$1/.loom/config.json" cmd secs
    [[ -e "$cfg" ]] || { warn "No .loom/config.json in the merge result — no compile check configured"; return 0; }
    [[ "$(jq -r '.buildGate.enabled // true' "$cfg" 2>/dev/null)" != "false" ]] || { warn "buildGate is disabled — no compile check runs before the push"; return 0; }
    cmd="$(jq -r '.buildGate.fastCommand // empty' "$cfg" 2>/dev/null)"
    if [[ -z "$cmd" ]]; then
        cmd="$(jq -r '.buildGate.command // empty' "$cfg" 2>/dev/null)"
        [[ -n "$cmd" ]] && cmd="LOOM_BUILD_GATE_TIER=fast $cmd"
    fi
    [[ -n "$cmd" ]] || { warn "buildGate names no command — no compile check runs before the push"; return 0; }
    secs="$(jq -r '.buildGate.timeoutSeconds // 600' "$cfg" 2>/dev/null)"
    (cd "$checkout" && timeout "$secs" sh -c "$cmd") >&2
}

# Merge-based de-stacking of ONE stale child (#9808), for a repo whose PR
# branches are never rewritten. The child gets origin/<parent> merged in and a
# plain fast-forward push: no history rewrite, no lease, nothing to clobber.
# Every outcome leaves the child as found or strictly ahead of it. Sets
# RSC_FAILURE=2 when a verification or the push fails; a conflict is a handoff
# (one comment), not a failure. Always returns 0.
_merge_one_stacked_child() {
    local child_pr="$1" child_branch="$2" parent_branch="$3" why
    if why="$(_stacked_child_in_flight "$child_pr" "$child_branch")"; then
        if [[ "$DRY_RUN" == "true" ]]; then
            info "[dry-run] would-skip child PR #$child_pr ($child_branch): $why"
        else
            info "Skipping child PR #$child_pr ($child_branch): $why — a live agent may be pushing to it"
        fi
        return 0
    fi
    if [[ "$DRY_RUN" == "true" ]]; then
        info "[dry-run] would-merge origin/$parent_branch into child PR #$child_pr ($child_branch) and push it"
        return 0
    fi

    local checkout parent_tip
    checkout="$(mktemp -d)" || { warn "Could not create a scratch checkout — skipping child PR #$child_pr"; return 0; }
    if ! git worktree add --detach "$checkout" "origin/$child_branch" >/dev/null 2>&1; then
        warn "Could not check out '$child_branch' in a scratch worktree — skipping child PR #$child_pr"
        _destack_cleanup_checkout "$checkout"; return 0
    fi

    info "Child PR #$child_pr ($child_branch) is stale relative to '$parent_branch' — merging origin/$parent_branch"
    if ! git -C "$checkout" merge --no-edit -m "Merge origin/$parent_branch into $child_branch (de-stack, #9808)" "origin/$parent_branch" >/dev/null 2>&1; then
        local files marker comment
        files="$(git -C "$checkout" diff --name-only --diff-filter=U 2>/dev/null || true)"
        git -C "$checkout" merge --abort >/dev/null 2>&1 || true
        _destack_cleanup_checkout "$checkout"
        parent_tip="$(git rev-parse "origin/$parent_branch" 2>/dev/null || echo unknown)"
        marker="<!-- loom:destack-conflict parent=$parent_tip -->"
        warn "Merging origin/$parent_branch into '$child_branch' conflicts — nothing pushed, handing off on PR #$child_pr"
        if gh pr view "$child_pr" --repo "$REPO_NWO" --json comments --jq '.comments[].body' 2>/dev/null | grep -qF -- "$marker"; then
            info "PR #$child_pr already carries the conflict comment for parent tip $parent_tip — not re-posting"
            return 0
        fi
        comment="## Stacked parent advanced — automatic merge conflicts

\`$parent_branch\` moved ahead of this PR's base, and merging it in conflicts. Nothing was pushed to \`$child_branch\`.

**Conflicting files:**

$(printf '%s\n' "${files:-(none reported)}" | sed 's/^/- /')

**To finish it by hand**, in this branch's worktree:

\`\`\`
git fetch origin
git merge origin/$parent_branch
# resolve the conflicts, commit, then a plain push (never a rewrite):
git push origin HEAD:$child_branch
\`\`\`

---
*Posted once per parent tip by rebase-stacked-children.sh (#9808)*
$marker"
        gh pr comment "$child_pr" --repo "$REPO_NWO" --body "$comment" >/dev/null 2>&1 || warn "Could not post the conflict comment on PR #$child_pr"
        return 0
    fi

    # Verify the MERGE RESULT before anything leaves this host: version-bearing
    # files in sync (#7168), then the fast-tier compile check (#11690).
    if [[ -x "$SCRIPT_DIR/version-check-gate.sh" ]] && ! (cd "$checkout" && "$SCRIPT_DIR/version-check-gate.sh" --fix-hint "then push."); then
        err "Version-bearing files are out of sync for '$child_branch' after merging '$parent_branch' — not pushing (see BLOCKER:/Fix: above)."
        _destack_cleanup_checkout "$checkout"; RSC_FAILURE=2; return 0
    fi
    if ! _destack_compile_check "$checkout"; then
        err "The compile check failed on the merge of '$parent_branch' into '$child_branch' — not pushing; the child is left as found."
        _destack_cleanup_checkout "$checkout"; RSC_FAILURE=2; return 0
    fi

    # A plain fast-forward push. A rejection means someone pushed meanwhile:
    # report it and move on.
    if ! git -C "$checkout" push origin "HEAD:refs/heads/$child_branch" >/dev/null 2>&1; then
        err "Push of the merge into '$child_branch' was rejected (a concurrent update?). Fetch and re-run; nothing was rewritten."
        _destack_cleanup_checkout "$checkout"; RSC_FAILURE=2; return 0
    fi
    _destack_cleanup_checkout "$checkout"
    ok "Merged origin/$parent_branch into child PR #$child_pr ($child_branch) and pushed (fast-forward, base unchanged, still stacked on $parent_branch)"
    return 0
}

# Rebase (or defer) one discovered child PR relative to the parent's current
# tip. Sets RSC_FAILURE=2 on a git/gh step failure but always returns 0 so the
# caller keeps processing the remaining children.
_process_one_stacked_child() {
    local child_pr="$1" child_branch="$2" parent_branch="$3"

    # #9106: $child_branch is the forge's `headRefName` for this PR — attacker-
    # controlled. Validate BEFORE the fetch below (and therefore before the
    # rebase/push further down), and skip this child on refusal rather than
    # letting git parse the name as a switch. $parent_branch was already
    # validated once at the top of the script.
    if ! check_branch_name "$child_branch" "head branch of child PR #$child_pr"; then
        warn "Skipping child PR #$child_pr (#9106) — no git command ran on its head branch."
        RSC_FAILURE=2; return 0
    fi

    # Fetch the parent + child tips so the staleness check reflects the current
    # remote state, not a stale local view. Read-only w.r.t. the remote.
    # `--` ends option parsing: defence in depth behind check_branch_name.
    if ! git fetch origin -- "$parent_branch" "$child_branch" >/dev/null 2>&1; then
        warn "Could not fetch origin refs for '$parent_branch' / '$child_branch' — skipping child PR #$child_pr"
        return 0
    fi

    # Up-to-date: is the parent's current tip already an ancestor of the child?
    # If so the child already contains everything the parent has — no-op.
    if git merge-base --is-ancestor "origin/$parent_branch" "origin/$child_branch" >/dev/null 2>&1; then
        info "Child PR #$child_pr ($child_branch) already contains parent tip — up to date, skipping"
        return 0
    fi

    # #10771: in a merge-only repo, never rewrite the child. It is brought up to
    # date by a merge and a plain push instead (#9808).
    if [[ "${BRANCH_UPDATE_STRATEGY:-}" != "rebase" ]]; then
        _merge_one_stacked_child "$child_pr" "$child_branch" "$parent_branch"
        return 0
    fi

    # Stale relative to the parent's current tip. Derive the child ISSUE number
    # from its head branch (feature/issue-<N>) for the safe/unsafe split. A
    # non-feature/issue-N child has no loom:building claim to race → treated safe.
    local child_issue=""
    if [[ "$child_branch" =~ ^feature/issue-([0-9]+)$ ]]; then
        child_issue="${BASH_REMATCH[1]}"
    fi
    # #9487: build this child's PINNED --force-with-lease argument HERE, before
    # the rebase rewrites the branch, by reading origin's live head. Left
    # implicit at push time the bare flag compares against
    # refs/remotes/origin/<child> — a ref SHARED by every linked worktree of
    # this clone, which a sibling agent's fetch can fast-forward to a commit
    # this run never saw; the bare lease is then satisfied by construction and
    # the push silently deletes that commit (the live PR #9483 incident).
    # `loom-daemon push-lease pin-flag` (cli/push_lease.rs) owns the query, the
    # rendering, and the second refusal for a commit published BEFORE the pin
    # was taken that this clone never incorporated. Any non-zero exit — exit 3
    # unreadable remote, 4 unincorporated head, 127 no binary at all — SKIPS
    # this child; there is no bare-lease fallback, because that is the bug.
    local child_lease_arg="" pin_rc=0
    child_lease_arg="$("$DAEMON_BIN" push-lease pin-flag --remote origin --branch "$child_branch" --local-ref "refs/heads/$child_branch")" || pin_rc=$?
    if [[ $pin_rc -ne 0 || -z "$child_lease_arg" ]]; then
        warn "No pinned force-with-lease value for '$child_branch' (loom-daemon push-lease pin-flag exit $pin_rc — see above, #9487; 'unrecognized subcommand' = this loom-daemon predates #9487: roll it) — skipping child PR #$child_pr rather than pushing with the bare lease."
        RSC_FAILURE=2; return 0
    fi

    # (docs/gh-cached.md policy: claim arbitration stays on plain `gh`, #9953.)
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

Parent branch \`$parent_branch\` advanced (amended/pushed) after this child branched off it, so this PR is now **stale** relative to the parent's current tip. But issue #$child_issue is still \`loom:building\` — a Builder likely has this branch checked out. The auto-rebase was **skipped** to avoid racing that in-progress work with an out-of-band \`git rebase\` and force-push.

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
    if ! run git rebase -- "origin/$parent_branch" "$child_branch"; then
        err "Rebase of '$child_branch' onto 'origin/$parent_branch' hit a conflict."
        printf '    Resolve it, then finish manually:\n    git rebase origin/%s %s   # then, after resolving each conflict:\n    git rebase --continue\n    git push %s origin %s   # pinned, never bare (#9487)\n' "$parent_branch" "$child_branch" "$child_lease_arg" "$child_branch" >&2
        # Abort the conflicted rebase so the remaining children can still process
        # (best-effort; the whole run is not aborted by one child's conflict).
        git rebase --abort >/dev/null 2>&1 || true
        RSC_FAILURE=2
        return 0
    fi

    # Version-bearing-file sync gate (#7168; why: version-check-gate.sh) on the
    # rebased child the rebase checked out. Not under --dry-run: nothing ran.
    if [[ "$DRY_RUN" != "true" ]] && [[ -x "$SCRIPT_DIR/version-check-gate.sh" ]]; then
        if ! "$SCRIPT_DIR/version-check-gate.sh" --fix-hint "then push."; then
            err "Version-bearing files are out of sync for '$child_branch' after rebase onto '$parent_branch' (see BLOCKER:/Fix: above)."
            RSC_FAILURE=2
            return 0
        fi
    fi
    # loom-daemon discard guard (#9282): a clean rebase can still empty the child or drop a
    # hand-made merge resolution. rc 0 = blocked, no push; rc >1 = no answer, warn.
    # requires-daemon: forge optional   rebase-guard (#9282): with no binary, or one predating the verb, the push proceeds unchecked behind a warning
    local rg=1
    [[ "$DRY_RUN" == "true" ]] || { rg=0; "$DAEMON_BIN" forge rebase-guard --base "origin/$parent_branch" --old "origin/$child_branch" --new "$child_branch" || rg=$?; }
    [[ $rg -ne 0 ]] || { err "Not pushing '$child_branch': its rebase onto '$parent_branch' would discard work (see BLOCKER:/Fix: above)."; RSC_FAILURE=2; return 0; }
    [[ $rg -eq 1 ]] || warn "rebase-guard gave no answer (rc=$rg) for '$child_branch' — pushing UNCHECKED for a dropped merge resolution or an emptied branch (#9282)."

    # Pinned, never bare (#9487) — see the capture above.
    if ! run git push "$child_lease_arg" origin "$child_branch"; then
        # A reported rejection is not always a real one (#6695): Git LFS's
        # pre-push hook can race the lease re-check on a branch with pending
        # LFS objects, so the ref update lands while the printed rejection
        # reflects a stale read. Verify the LIVE remote ref before trusting
        # the reported failure.
        local push_race_sha
        push_race_sha="$(git rev-parse "$child_branch" 2>/dev/null || true)"
        if [[ "$DRY_RUN" != "true" ]] && push_landed_despite_rejection origin "$child_branch" "$push_race_sha"; then
            warn "PUSH-LEASE-RACE-DETECTED: push --force-with-lease reported a rejection for '$child_branch', but origin already reflects the update ($push_race_sha) — likely the Git LFS pre-push hook racing the lease re-check (#6695). Treating as landed and continuing."
        else
            err "force-with-lease push rejected for '$child_branch' (someone else pushed). Fetch, review, and retry."
            RSC_FAILURE=2
            return 0
        fi
    fi
    ok "Rebased child PR #$child_pr ($child_branch) onto origin/$parent_branch and force-pushed (base unchanged, still stacked on $parent_branch)"
    return 0
}

# Discover open child PRs stacked on <parent-branch> and rebase (or defer) each
# stale one. GitHub-only; the parent branch may have any name.
_rebase_stacked_children() {
    local parent_branch="$1"

    [[ "$FORGE_TYPE" == "github" ]] || { info "Forge is '${FORGE_TYPE:-unknown}', not github — nothing to do"; return 0; }

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
   && [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    err "Working tree is dirty. Commit, stash, or discard changes before rebasing stacked children."
    exit 1
fi

# #9106: validate the parent branch ONCE, before any git/gh call can consume it
# as a bare operand. It reaches `git fetch origin -- <parent> <child>` and
# `git rebase -- origin/<parent> <child>` below; a name git would parse as a
# switch is an injection attempt (or a broken caller), not a data error, so the
# whole run refuses with a named reason rather than skipping quietly.
check_branch_name "$PARENT_BRANCH" "parent branch argument" || exit 1

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
