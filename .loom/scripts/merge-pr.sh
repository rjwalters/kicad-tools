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
#   --auto                 Wait for this head's checks to settle, then merge in
#                          THIS process (immediately if they are already
#                          settled). It never arms the forge's own server-side
#                          auto-merge queue (#8410): that queue is gated only by
#                          the branch ruleset's REQUIRED checks and re-reads
#                          nothing, so a merge armed at queue time ignored both
#                          a later loom:pr revocation and every non-required
#                          test suite. Bounded by LOOM_AUTO_MERGE_TIMEOUT
#                          (default 600s; raise it on a slow-CI repo); exits
#                          non-zero on timeout or a failed required check,
#                          which Champion's next pass retries.
#   --allow-stacked-children
#                          Bypass the pre-merge merge-ordering guard's hard-block
#                          path. That guard's DEFAULT behavior, when the parent
#                          branch (feature/issue-N) still has open stacked child
#                          PRs targeting it, is now to pin the parent's tip to
#                          refs/loom/parent/<branch> and proceed with a WARNING
#                          (see #7982) — it only still hard-blocks when the tip
#                          could not be pinned (a detached/unreadable parent).
#                          This flag skips straight past that remaining block
#                          (operator asserts responsibility, mirroring
#                          --worktree-path). See #3747 item 2.
#   --allow-red-tree       Proceed past a failing repo `merge.treeChecks` gate (warns +
#                          audit comment). See #10026.
#   --allow-red-required <context>
#                          Repeatable. Merge over a red REQUIRED status-check
#                          context, for exactly the named context(s). Without
#                          it a plain (non --auto) merge refuses (exit 1) when
#                          any required context concluded red, and holds (exit
#                          5) when one is missing or pending — even with a
#                          token that could bypass branch protection. Warns and,
#                          on a real merge, posts a PR audit comment, like
#                          --allow-unapproved. See #10688.
#   --allow-unapproved     Bypass the pre-merge loom:pr review-signal guard,
#                          which otherwise hard-blocks merging a PR whose
#                          current head carries no loom:pr label (no
#                          forge-visible signal Judge reviewed it — e.g. a
#                          Doctor rebase cleared it via the staleness guard).
#                          The bypass is recorded as a warning and, on a real
#                          (non-dry-run) merge, best-effort as a PR comment
#                          audit trail. Operator asserts responsibility,
#                          mirroring --allow-stacked-children. See #7419.
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
#   --expected-head <sha>  Fail-closed head pin (#11859): refuse (exit 8) unless
#                          the PR's head is exactly this full SHA, checked on
#                          the first uncached PR read and again on the merge
#                          precondition read, which then gates the forge merge
#                          call on the CALLER's SHA instead of its own read. A
#                          targeted Champion (`/loom:champion --pr N`) passes
#                          the head it evaluated, so a push after its
#                          evaluation can never be merged under its verdict.
#                          Any later head move exits 3: under the pin the #8164
#                          self-sync retry is declined (its check reads only the
#                          merge commit's parents, never its tree), so a fresh
#                          evaluation of the new head always comes first.
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
# Exit codes (caller table: defaults/docs/merge-pr-exit-code-exceptions.md
# §"Exit-code table"). 3-7 are all "nothing merged, nothing failed": re-queue
# for a later pass, NEVER a failure comment; only exit 1 is a failure.
#   0 = merged
#   1 = failed / refused (a real rejection; see the refusal text).
#   3 = PR head moved past the SHA this attempt gated on (#5579): still
#       Judge-approved, its diff just changed underneath it. See "Merge-ancestry
#       detection trap" in the doc above for why ancestry can't verify this.
#   4 = retired (#12063 slice 1): the #8248 freshness guard and its #8508
#       re-date remedy are gone; never emitted.
#   5 = --auto's settle-wait (LOOM_AUTO_MERGE_TIMEOUT, default 600s) expired
#       (#8896); or held: a required context is missing/pending on a plain
#       merge (#10688).
#   7 = held by a sequencing order (#11330): the PR carries loom:pr +
#       loom:sequenced and no other blocker. Prints `LOOM-SEQUENCED-HOLD
#       pr=<N> after=<M>`; the predecessor landing clears it (#9378). Re-queue
#       later; it has no cap of its own (sequence-eval owns release).
#   8 = --expected-head refused (#11859): the PR head is not the SHA the
#       caller evaluated. Prints `LOOM-EXPECTED-HEAD-MISMATCH pr=<N>
#       expected=<sha> actual=<sha|unknown>`. Nothing written; the caller's
#       evaluation is stale, so re-evaluate the new head, never retry blind.

set -euo pipefail

# ANSI color codes (one line).
RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'

error() { echo -e "${RED}Error: $*${NC}" >&2; exit 1; }
# info/success/warning joined onto one line.
info() { echo -e "${BLUE}$*${NC}"; }; success() { echo -e "${GREEN}$*${NC}"; }; warning() { echo -e "${YELLOW}$*${NC}"; }
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

# #11859: --expected-head's fail-closed pin. Distinct exit 8: unlike 3 (a race
# lost mid-merge, re-queue) the caller's evaluation itself is of another tree.
_check_expected_head() { [[ -z "$EXPECTED_HEAD_SHA" || "$1" == "$EXPECTED_HEAD_SHA" ]] && return 0; echo "LOOM-EXPECTED-HEAD-MISMATCH pr=$PR_NUMBER expected=$EXPECTED_HEAD_SHA actual=${1:-unknown}"; echo -e "${RED}Error: Merge refused: PR #$PR_NUMBER head is ${1:-unreadable}, not the --expected-head $EXPECTED_HEAD_SHA the caller evaluated (#11859). Nothing was merged or written; re-evaluate the current head.${NC}" >&2; exit 8; }

# Which route a FAILED merge's forge error TEXT sends the retry loop below down
# (#8191 slice). Prints one of: merge-in-progress (HTTP 405), head-mismatch
# (#5579 — the PR's OWN head moved past the SHA we gated on, so retrying would
# either fail again or silently merge a diff Judge never approved),
# base-modified (the PR's BASE fell behind; rebase-and-retry is correct), other
# (stop). String provenance stays documented on forge_merge_pr in
# lib/forge-helpers.sh — GitHub REST and Gitea verified against each forge's own
# source/spec, the GitHub GraphQL `expectedHeadOid` spelling best-effort from the
# retired server-side arm (#8427).
#
# This replaces three separate `grep` matchers in three separate `if` blocks
# whose relative ORDER was the entire safety property: a head-mismatch reaching
# the base-modified arm answers a moved head with forge_update_branch and another
# merge attempt. Nothing asserted that order except an `awk` scan over THIS
# FILE's source text looking for which `grep` appeared first. It is now one
# ordered `match` in loom-daemon/src/merge_pr/response.rs, pinned by a
# differential against the frozen retired ladder
# (loom-daemon/tests/merge_pr_response_differential.rs) and by the precedence
# unit tests beside the module. Asymmetric case-sensitivity is preserved
# verbatim: the head-mismatch alternation was `grep -Ei`, its two siblings bare
# `grep -q`.
#
# Returns 3 when no route could be OBTAINED — which the caller must never
# collapse into the `other` route. Fails CLOSED there, deliberately: the routes
# are not interchangeable, so an unresolvable binary leaves only "guess a route"
# or "refuse and say which happened", and only the second is distinguishable
# from a merge verdict afterwards. It cannot stop a healthy merge — a merge that
# SUCCEEDS never reaches this function; only one that already failed does.
#
# `printf '%s'`, not the retired `echo "$1"`: bash's `echo` silently swallows an
# argument that is exactly -n/-e/-E, and the response is arbitrary forge bytes.
# The differential's corpus includes those three inputs, so that substitution is
# checked to change no answer rather than assumed equivalent.
_classify_merge_response() { local _k; _k="$(printf '%s' "$1" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr classify-response 2>/dev/null)" || _k=""; [[ "$_k" == "LOOM-MERGE-RESPONSE "* ]] || return 3; printf '%s' "${_k#LOOM-MERGE-RESPONSE }"; }

# #8164: record that THIS script pushed to the head branch, via
# forge_update_branch() ("Base branch was modified" retry). Deliberately does
# NOT re-read or adopt the new head SHA — that adoption used to be blind (no
# parent inspection, no containment check), which meant a session pushing a
# commit on top of ours mid-sync (the exact #5579 scenario) got squashed into
# the merge with no refusal and no trace once the squash discarded ancestry.
# Leaving $MERGE_PRECONDITION_SHA untouched means the next merge attempt gets
# its own 409 "Head branch was modified", which routes through
# _head_moved_or_resync() below exactly like the residual-timing-window case —
# so EVERY head-SHA adoption on this path goes through the same structural
# attribution check, not just the asynchronous-push one. One extra round trip
# buys uniform attribution instead of two different safety levels for the same
# claim ("this new head is our own sync").
# Written as one dense line, like the guards above.
_refresh_precondition_sha() { _HEAD_SELF_SYNCED=true; return 0; }
# _mr_say KEY -- the KEY<TAB>text line of the current `merge-pr merge-route`
# answer ($_MR), or a generic line when the route was synthesized without one.
_mr_say() { local l="${_MR#*$'\n'"$1"$'\t'}"; [[ "$l" != "$_MR" ]] || l="PR #$PR_NUMBER: merge retry, step $1 (no wording: 'merge-pr merge-route' unavailable)"; printf '%s' "${l%%$'\n'*}"; }

# #8164: a head-SHA mismatch is retried ONCE when — and only when — this run's
# own base-sync caused it. Returns 0 when the caller should re-attempt the
# merge against the refreshed $MERGE_PRECONDITION_SHA; otherwise never
# returns, exiting 3 through error_head_moved() exactly as before.
#
# The decision is `loom-daemon merge-pr head-sync-retry` (Rust,
# loom-daemon/src/merge_pr/head_sync.rs — slice 4 of the merge-pr port #8191),
# which authorizes a retry only when the new head is a two-parent merge whose
# FIRST parent is the head we were about to merge and whose SECOND parent is
# already contained in the base branch. Under that shape the new head's
# content is (approved head ∪ base) and nothing else — the same tree the merge
# would have produced. Attribution is structural, never the commit message:
# a message is attacker-supplied text, parent SHAs are not. Any other shape
# (a rebase-style update, a commit pushed on top, a merge of some other
# branch) stays the #5579 hard stop.
#
# Under --expected-head (#11859) the retry is declined: the check reads
# parents, not the tree, and a pinned caller re-evaluates instead. The verb
# still classifies the move, so a retry it would allow is refused here naming
# the pin (#11902), never as a spent budget.
#
# Fails safe by construction: only exit 0 PLUS the sentinel retries, so a
# missing/old/substituted binary, a forge read failure, or any silent failure
# lands on the pre-#8164 behaviour — exit 3, re-queue — which is why this
# guard needs no new helper-missing exit code. It can only add merges that
# would otherwise have been re-queued; it can never remove a refusal.
_head_moved_or_resync() {
  local _CURRENT_HEAD_SHA="" _CHR_JSON _HMR_OUT _HMR_RC=0 _HMR_FLAGS=(); _CHR_JSON="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"; _CURRENT_HEAD_SHA="$(echo "$_CHR_JSON" | jq -r '.head.sha // empty' 2>/dev/null || echo '')"; unset _CHR_JSON
  [[ "${_HEAD_SELF_SYNCED:-}" == "true" ]] && _HMR_FLAGS+=(--self-synced); [[ "${_HEAD_RESYNC_USED:-}" == "true" || "${MERGE_ATTEMPT:-1}" -ge "${MAX_MERGE_RETRIES:-3}" ]] && _HMR_FLAGS+=(--retry-used); [[ "${2:-}" == "exit-code" ]] && _HMR_FLAGS+=(--mismatch-confirmed)
  _HMR_OUT="$(printf '%s' "$1" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr head-sync-retry --pr "$PR_NUMBER" --repo "$REPO_NWO" --precondition-sha "$MERGE_PRECONDITION_SHA" "${_HMR_FLAGS[@]+"${_HMR_FLAGS[@]}"}" 2>/dev/null)" || _HMR_RC=$?
  if [[ $_HMR_RC -eq 0 && "$_HMR_OUT" == "LOOM-HEAD-SELF-SYNC-RETRY "* ]]; then if [[ -n "${EXPECTED_HEAD_SHA:-}" ]]; then _HMR_OUT="PR #$PR_NUMBER: the head moved by this run's own base-sync, but the --expected-head pin forbids the #8164 retry: the new head ${_HMR_OUT##* } was never evaluated, so re-evaluate it (#11902)"; else _HEAD_RESYNC_USED=true; MERGE_PRECONDITION_SHA="${_HMR_OUT##* }"; info "PR #$PR_NUMBER: head-SHA mismatch attributed to this run's own base-sync; retrying once against ${MERGE_PRECONDITION_SHA:0:8} (#8164)"; return 0; fi; fi
  [[ -n "$_HMR_OUT" ]] && warning "$_HMR_OUT"; error_head_moved "PR #$PR_NUMBER: $1" "$MERGE_PRECONDITION_SHA" "$_CURRENT_HEAD_SHA"
}

# Early help check — runs before any git/forge initialization so --help works
# in any directory and without forge authentication. The text (option list,
# cleanup semantics, exit-code table) is `loom-daemon merge-pr usage`
# (loom-daemon/src/merge_pr/usage.rs + usage.txt, #8191 slice), printed only
# behind its LOOM-MERGE-PR-USAGE sentinel so no other binary's output can pass
# for it. Resolved like _mp_worktree (LOOM_DAEMON_SELF_BIN first, #8134), so
# a suite can pin the implementing binary without touching LOOM_DAEMON_BIN.
# Fails OPEN toward exit 0: a missing/older daemon gets a one-line
# usage plus a pointer on stderr. Help gates nothing, and exit 1 would read as
# a failed merge to a caller probing the script. $_MP_NL, not an inline $'\n'
# pattern: bash 3.2 reads the latter literally (see #8191's partial-comment slice).
if [[ $# -gt 0 ]] && { [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; }; then
  _MP_NL=$'\n'; _MP_USAGE="$("${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}" merge-pr usage 2>/dev/null)" || _MP_USAGE=""
  if [[ "$_MP_USAGE" == "LOOM-MERGE-PR-USAGE$_MP_NL"* ]]; then printf '%s\n' "${_MP_USAGE#*"$_MP_NL"}"; exit 0; fi
  echo "Usage: ./.loom/scripts/merge-pr.sh <pr-number> [--dry-run] [--auto] [--no-cleanup-worktree] [--worktree-path <dir>] [options]"
  echo "Full help is 'loom-daemon merge-pr usage', which '${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}' could not print (missing, or predates it). Roll this host: ${BASH_SOURCE[0]%/*}/cli/loom-daemon-update.sh --fetch" >&2
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
# lib/worktree-root.sh is NOT sourced any more (#8191 slice): the overridden-root
# resolution (#3530) that post-merge cleanup needs is `loom-daemon merge-pr
# cleanup-paths`, which resolves it through loom-daemon's own port of that helper
# (loom-daemon/src/worktree_root.rs, `worktree_root_readable`) rather than a
# second copy reached through a sourced lib. See the cleanup block at the bottom.
# Cargo target-dir reclaim (#7239). Post-merge cleanup is the removal path most
# worktrees actually take, so a redirected CARGO_TARGET_DIR/build.target-dir
# would leak its per-worktree build output here more than anywhere else. The
# resolve/reclaim pair runs in-process inside `merge-pr post-merge-cleanup`
# (#9153, #8191): the same Rust decision `worktree.sh remove`, `loom-daemon
# clean` and the reaper share. See the `requires-daemon:` block below.
# Shared "has this branch landed?" primitive (#7812) — the one implementation
# of the question the branch-delete and worktree-preserve guards below ask.
# Required, like forge-helpers.sh above: every branch-delete decision in this
# script depends on it, and a missing lib must fail loudly at startup rather
# than silently degrade a destructive decision to a guess.
# shellcheck source=lib/branch-landed.sh
source "$SCRIPT_DIR/lib/branch-landed.sh"
# Default-branch resolver (#4100) — the local-branch delete guard must never
# target the repo's default branch. Sourced defensively: a repo where this
# fails to resolve (e.g. no network + no origin/HEAD symref) still falls back
# to the literal "main"/"master" check in the local-branch delete (`merge-pr delete-branch`).
DEFAULT_BRANCH_NAME=""
# Sourced UNCONDITIONALLY (#9106). This lib carries check_branch_name, the
# ref-operand validator every forge-derived branch name below must pass before
# it reaches a git argv, and a fail-closed security guard must not be optional:
# the old `if [[ -f ... ]]` guard let a partially-resynced .loom/ drop the
# validator silently. A missing lib now aborts here instead.
# shellcheck source=lib/default-branch.sh
source "$SCRIPT_DIR/lib/default-branch.sh"
DEFAULT_BRANCH_NAME="$(cd "$REPO_ROOT" && loom_default_branch 2>/dev/null || true)"
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

# #9548: the repo is vetted, not merely resolved. forge_get_repo_nwo asked
# `gh repo view`, which prefers an `upstream` remote over `origin`, and nothing
# checked that the answer was a repo this installation manages or can write.
# loom_write_repo (lib/forge-helpers.sh, sourced above) returns origin only
# when gh resolves there too, the repo is managed here, and the credential has
# WRITE; otherwise the merge is refused before any read or write.
REPO_NWO="$(loom_write_repo "${LOOM_REPO:-}")" || error "Merge refused (#9548): this checkout's repository is not one this installation may write to (reason above)."
# Detect which merge strategy the target repo actually allows (#7754) --
# previously every call site below hardcoded "squash", which fails outright
# ("Squash merges are not allowed on this repository") on any repo that has
# squash-merge disabled. Read once per invocation; fails open to "squash"
# (this script's pre-#7754 behavior) on any probe failure.
REPO_MERGE_METHOD="$(forge_detect_merge_method "$REPO_NWO" "$GH" 2>/dev/null || echo squash)"

# Parse arguments
PR_NUMBER=""; CLEANUP_WORKTREE=true
# CLEANUP_PRIMARY_CHECKOUT (#5015): gates the automatic primary-checkout
# branch cleanup performed by `merge-pr post-merge-cleanup`. Defaults on
# (mirrors CLEANUP_WORKTREE's default); --no-cleanup-primary opts out.
CLEANUP_PRIMARY_CHECKOUT=true; DRY_RUN=false; AUTO_MERGE=false; WORKTREE_PATH_OVERRIDE=""; ALLOW_STACKED_CHILDREN=false
# ALLOW_UNAPPROVED (#7419): bypasses the loom:pr review-signal guard
# (_check_loom_pr_label below). Off by default — a missing loom:pr label
# hard-blocks the merge unless the operator explicitly opts in here.
# ALLOW_RED_TREE (#10026): bypasses _check_tree_checks. ALLOW_RED_REQUIRED (#10688): contexts
# _check_required_checks may merge over red. MERGE_METHOD_REQUESTED (#8845): explicit
# --merge-method override, resolved against REPO_MERGE_METHOD once parsing is done.
ALLOW_UNAPPROVED=false; ALLOW_RED_TREE=false; ALLOW_RED_REQUIRED=(); MERGE_METHOD_REQUESTED=""; EXPECTED_HEAD_SHA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cleanup-worktree|--cleanup-primary) shift ;;  # no-ops, both cleanups are now the default (one arm: #11859 offset)
    --no-cleanup-worktree) CLEANUP_WORKTREE=false; shift ;;
    --expected-head) [[ $# -ge 2 && "$2" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || error "--expected-head requires a full lowercase hex commit SHA (40 or 64 chars)"; EXPECTED_HEAD_SHA="$2"; shift 2 ;;
    --no-cleanup-primary) CLEANUP_PRIMARY_CHECKOUT=false; shift ;;
    # The two --worktree-path arms are joined onto one line each.
    --worktree-path) [[ $# -lt 2 ]] && error "--worktree-path requires a value"; WORKTREE_PATH_OVERRIDE="$2"; shift 2 ;;
    --worktree-path=*) WORKTREE_PATH_OVERRIDE="${1#--worktree-path=}"; [[ -z "$WORKTREE_PATH_OVERRIDE" ]] && error "--worktree-path= requires a value"; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --auto) AUTO_MERGE=true; shift ;;
    --allow-stacked-children) ALLOW_STACKED_CHILDREN=true; shift ;;
    --allow-unapproved) ALLOW_UNAPPROVED=true; shift ;;
    --allow-red-tree) ALLOW_RED_TREE=true; shift ;;
    --allow-red-required) [[ $# -lt 2 || -z "$2" ]] && error "--allow-red-required requires a context name"; ALLOW_RED_REQUIRED+=("$2"); shift 2 ;;
    # #8845's --merge-method arm; the `*)` positional arm below is flattened
    # onto one line, as --worktree-path is above. Validation here is vocabulary-only (mirrors PR_NUMBER's own
    # ^[0-9]+$ check below); which methods the REPO actually allows is decided
    # in loom-daemon (loom-daemon/src/forge_cmd.rs), not here.
    --merge-method) [[ $# -lt 2 ]] && error "--merge-method requires a value"; case "$2" in squash|merge|rebase) ;; *) error "--merge-method must be one of: squash, merge, rebase (got: $2)" ;; esac; MERGE_METHOD_REQUESTED="$2"; shift 2 ;;
    -*)  error "Unknown option: $1" ;;
    *) if [[ -z "$PR_NUMBER" ]]; then PR_NUMBER="$1"; else error "Unexpected argument: $1"; fi; shift ;;
  esac
done

[[ -z "$PR_NUMBER" ]] && error "Usage: merge-pr.sh <pr-number> [--no-cleanup-worktree] [--no-cleanup-primary] [--worktree-path <dir>] [--dry-run] [--auto] [--allow-stacked-children] [--allow-unapproved] [--allow-red-tree] [--allow-red-required <context>]... [--merge-method squash|merge|rebase] [--expected-head <sha>]"
[[ "$PR_NUMBER" =~ ^[0-9]+$ ]] || error "PR number must be numeric: $PR_NUMBER"

# #8845: an explicit --merge-method overrides REPO_MERGE_METHOD's auto-detect
# above, but only after loom-daemon VALIDATES it against the repo's actually
# allowed strategies (loom-daemon/src/forge_cmd.rs, `forge merge-method`) --
# exit 0 = allowed (its stdout is the method, unchanged); exit 1 = requested
# but disallowed, a hard refusal naming the allowed set (never a silent
# fallback to squash, the #8845 bug); any other exit (no daemon, or a Gitea
# decline -- gitea repo-settings probing isn't native yet) degrades to the
# unvalidated request as-is, same as this script's pre-#8845 posture toward
# an unverifiable input, with a warning so the gap is visible rather than silent.
# #8878: the binary probed and invoked is LOOM_DAEMON_BIN when set (as
# every daemon-backed guard here does) -- that override is exactly what
# _mp_daemon_roll_hint tells operators to export when no released daemon carries
# `forge merge-method` yet, so probing PATH's older binary instead threw away the
# validation they had just built and degraded to the unvalidated path anyway.
# The expansion is repeated inline rather than hoisted into a variable on this
# same line: check-daemon-subcommand-versions.sh only registers a binary-holding
# variable from a LINE-LEADING assignment, so a mid-line one would make the
# requires-daemon marker below read as stale (verified -- it fires).
if [[ -n "$MERGE_METHOD_REQUESTED" ]]; then if command -v "${LOOM_DAEMON_BIN:-loom-daemon}" &>/dev/null; then _MPM_RC=0; _MPM_OUT="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge merge-method --repo "$REPO_NWO" --requested "$MERGE_METHOD_REQUESTED" 2>&1)" || _MPM_RC=$?; if [[ $_MPM_RC -eq 0 ]]; then REPO_MERGE_METHOD="$_MPM_OUT"; elif [[ $_MPM_RC -eq 1 ]]; then error "Merge blocked: $_MPM_OUT"; else warning "'${LOOM_DAEMON_BIN:-loom-daemon}' forge merge-method could not validate --merge-method $MERGE_METHOD_REQUESTED (exit $_MPM_RC: $_MPM_OUT); using it unvalidated"; REPO_MERGE_METHOD="$MERGE_METHOD_REQUESTED"; fi; else warning "loom-daemon not found ('${LOOM_DAEMON_BIN:-loom-daemon}'); using --merge-method $MERGE_METHOD_REQUESTED unvalidated"; REPO_MERGE_METHOD="$MERGE_METHOD_REQUESTED"; fi; fi

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
  # Verify the path is actually a worktree of this repo (#8191 slice):
  # `loom-daemon merge-pr worktree-contains` shares the porcelain parser
  # worktree-primary/worktree-branch-for/worktree-find-by-branch already use
  # (loom-daemon/src/merge_pr/worktrees.rs), which fixed this same family's
  # #3717 space-in-path truncation. Exit 0 = registered, 1 = parsed and NOT
  # registered, anything else = the check did not run at all (missing/older
  # daemon) -- WARN and still merge, but DECLINE the override cleanup and keep
  # the worktree: a guard that did not run must refuse the removal rather than
  # wave it through (the #3710 rule; never lean on `git worktree remove` to
  # refuse an unverified path). Skipped cleanup is always recoverable.
  _WTC_RC=0; git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr worktree-contains --path "$_WT_ABS" 2>/dev/null || _WTC_RC=$?
  if [[ $_WTC_RC -eq 1 ]]; then error "--worktree-path is not a registered worktree of this repository: $WORKTREE_PATH_OVERRIDE (resolved: $_WT_ABS)"; elif [[ $_WTC_RC -ne 0 ]]; then warning "--worktree-path's registered-worktree check (#8191 slice) did not run — 'merge-pr worktree-contains' exited $_WTC_RC (a loom-daemon predating this slice has no such verb). The merge still proceeds, but $_WT_ABS is left untouched and worktree cleanup is skipped: an unverified path is never removed. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"; elif [[ "$CLEANUP_WORKTREE" == "false" ]]; then warning "--worktree-path was supplied but --no-cleanup-worktree wins; no cleanup will occur"; fi
  if [[ $_WTC_RC -eq 0 ]]; then WORKTREE_PATH_OVERRIDE="$_WT_ABS"; else WORKTREE_PATH_OVERRIDE=""; CLEANUP_WORKTREE=false; fi
  unset _WTC_RC _WT_ABS
fi

# Fetch PR state — UNCACHED (#8550). $GH may be the `gh-cached` wrapper, whose
# short TTL made this read return the PR's PRE-change label set for anything
# started inside the cache window. That is exactly the window an operator hold
# release lands in (remove `loom:operator`, merge immediately), and the #8112
# verdict-contradiction guard below — whose ONLY input is $PR_LABELS derived
# from this fetch — then correctly refused a merge on labels that no longer
# existed. Labels here are verdict-gating/merge-gating data, i.e. the
# deliberately-uncached class in docs/gh-cached.md, the same class the 15+
# `forge_get_pr_nocache` rechecks further down already belong to.
# A failed fetch names its cause -- status, meaning, forge message (#9192).
_PRF_ERR="$(mktemp)"
PR_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>"$_PRF_ERR") || error "Could not fetch PR #$PR_NUMBER -- $(forge_fetch_error_cause "$(cat "$_PRF_ERR"; rm -f "$_PRF_ERR")" "$PR_JSON")"; rm -f "$_PRF_ERR"; unset _PRF_ERR

# Combined onto two lines (net code-line offset for the #8112 guard added
# below — file-size-policy.md's "remove at least as much as you added"; a
# verbatim, behavior-preserving chain, not a reformat of anything else).
PR_STATE=$(echo "$PR_JSON" | jq -r '.state'); PR_MERGED=$(echo "$PR_JSON" | jq -r '.merged'); PR_BRANCH=$(echo "$PR_JSON" | jq -r '.head.ref')
PR_TITLE=$(echo "$PR_JSON" | jq -r '.title'); PR_MERGEABLE=$(echo "$PR_JSON" | jq -r '.mergeable')
# Head SHA (#4100): the safety criterion for local-branch deletion. A local
# branch whose tip equals this SHA carries no commits absent from the merged
# PR, so it is safe to force-delete even though it will never satisfy
# `git branch --merged` after a squash merge.
PR_HEAD_SHA=$(echo "$PR_JSON" | jq -r '.head.sha // empty')
# Labels (#7419): the loom:pr review-signal guard's only input. Both forges'
# forge_get_pr responses already carry a `labels` array in this shape, so no
# extra API call is needed beyond the fetch above.
PR_LABELS=$(echo "$PR_JSON" | jq -r '.labels[]?.name // empty' 2>/dev/null || true)
# #9106: $PR_BRANCH is `.head.ref` — chosen by whoever opened the PR. Everything
# downstream (the _recheck_mergeable_before_refusal fetch, branch_landed,
# update-ref refs/loom/parent/<branch>, the local `git branch -D`) puts it in a
# git argv, so it is validated ONCE here and the merge is denied outright if it
# is not a safe ref operand. The reason names the offending ref, so the refusal
# is greppable in the run log.
check_branch_name "$PR_BRANCH" "head branch of PR #$PR_NUMBER" || error "Merge blocked: PR #$PR_NUMBER's head branch is not a safe git ref operand (see the check_branch_name refusal above, #9106). Refusing before any git command runs on '$PR_BRANCH'. Rename the branch on the PR and re-run."

# Terminal-state gate: `loom-daemon merge-pr pr-state` (#8191 slice) — merged wins over
# closed. No verdict (missing/older daemon) falls back to the retired PR_MERGED/PR_STATE predicate, like revalidate-head.
case "$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr pr-state --state "$PR_STATE" --merged "$PR_MERGED" 2>/dev/null)" in
  "LOOM-PR-STATE MERGED") warning "PR #$PR_NUMBER is already merged"; exit 0 ;;
  "LOOM-PR-STATE CLOSED") error "PR #$PR_NUMBER is closed (not merged)" ;; "LOOM-PR-STATE OPEN") ;;
  *) [[ "$PR_MERGED" != "true" ]] || { warning "PR #$PR_NUMBER is already merged"; exit 0; }; [[ "$PR_STATE" != "closed" ]] || error "PR #$PR_NUMBER is closed (not merged)" ;; esac
# #11859: pin the caller's evaluated head BEFORE any gate below can write (a
# parent-ref pin, a label) on behalf of a tree it never saw.
_check_expected_head "$PR_HEAD_SHA"

# ---------------------------------------------------------------------------
# Pre-merge merge-ordering guard (#3747, stacked-PR v2 item 2; reshaped by
# #7982 into pin-and-warn).
#
# Runs BEFORE both the auto-merge and synchronous-merge paths (that is why it is
# invoked here, above the "Merging PR" line — not next to item 1's POST-merge
# _auto_reconcile_stacked_children at the bottom of the merge flow).
#
# The decision is `loom-daemon merge-pr stacked-children` (Rust,
# loom-daemon/src/merge_pr/stacked_children.rs — #8191 slice): discover open
# CHILD PRs still targeting this parent branch via a LIVE forge query (never the
# daemon registry), then ESTABLISH the postcondition reconcile-stack.sh needs by
# pinning the parent tip to refs/loom/parent/<branch> rather than merely
# asserting it — GitHub deletes the branch synchronously inside the merge call,
# so item 1's post-merge rebase would otherwise race the repo's own
# delete_branch_on_merge setting and lose. It hard-blocks on exactly one case:
# the tip could not be pinned at all. --allow-stacked-children skips past that
# block; --dry-run reports the would-be outcome and writes no ref.
#
# It emits `CHILDREN`/`PIN-WRITTEN`/`WARNING`/`BLOCK<TAB>line` records, replayed
# below through this script's own warning/error so the operator-visible text is
# unchanged from before the port. `CHILDREN` carries the pre-merge snapshot the
# post-merge reconcile prefers (#8010 item 2); `PIN-WRITTEN` fires only where a
# ref was actually written, which is what the item-3 re-pin must gate on (a
# bypass finds children without pinning). The two globals are plain (non-local)
# assignments so they survive this function returning, and are read as
# `${VAR:-}` everywhere else so a standalone invocation still behaves as before.
#
# Exit 1 WITH a BLOCK record refuses; any other non-zero is a guard FAULT and
# warns-and-proceeds. Unlike every gate below this one, that is fail-OPEN, and
# deliberately: this guard protects a best-effort POST-merge cleanup step, not
# the question of whether this tree may merge, so a skip costs one manual
# reconcile against a SHA the warning prints — while failing closed would stop
# every merge on a host whose daemon lags one release, nearly all of which have
# no stacked children at all. It is also safe by construction: the fail-CLOSED
# `verdict-contradiction` gate runs on this same binary further down, so a
# daemon too old for this verb cannot reach the merge anyway. Full argument:
# the module docs on loom-daemon/src/merge_pr/stacked_children.rs.
#
# _mp_daemon_roll_hint is not yet defined this early in the script (it lands
# with the version-floor block below), hence the `declare -F` probe and the
# inline roll command — a best-effort diagnostic must never be able to abort
# the warning it decorates.
_check_no_open_stacked_children() {
  # Only GitHub can have Loom-style stacked children; the parent-branch shape
  # gate is the Rust side's (it returns before any forge call).
  [[ "$FORGE_TYPE" == "github" ]] || return 0
  local out rc=0 level text children_json="" warn="" blk="" flags=()
  [[ "${DRY_RUN:-false}" != "true" ]] || flags+=(--dry-run)
  [[ "${ALLOW_STACKED_CHILDREN:-false}" != "true" ]] || flags+=(--allow-stacked-children)
  out="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr stacked-children --repo "$REPO_NWO" --repo-root "$REPO_ROOT" --branch "$PR_BRANCH" --head-sha "$PR_HEAD_SHA" --pr "$PR_NUMBER" ${flags[@]+"${flags[@]}"})" || rc=$?
  while IFS=$'\t' read -r level text; do case "$level" in
    CHILDREN) children_json="$text" ;;
    PIN-WRITTEN) STACKED_CHILDREN_PIN_WRITTEN=true ;;
    WARNING) warn+="${warn:+$'\n'}$text" ;;
    BLOCK) blk+="${blk:+$'\n'}$text" ;;
  esac; done <<< "$out"
  [[ -z "$children_json" ]] || STACKED_CHILDREN_JSON="$children_json"
  [[ -z "$warn" ]] || warning "$warn"
  [[ $rc -ne 1 || -z "$blk" ]] || error "$blk"
  [[ $rc -eq 0 ]] || warning "The merge-ordering guard (#3747 item 2) did not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr stacked-children' exited $rc (a loom-daemon predating #8191's slice has no such verb). Proceeding, because this guard establishes a postcondition for the POST-merge stacked reconcile rather than judging this tree: a skip costs at most one manual './.loom/scripts/reconcile-stack.sh <child-pr> $PR_BRANCH' against tip $PR_HEAD_SHA, never a wrong merge. Restore it by rolling this host: ${SCRIPT_DIR:-.loom/scripts}/cli/loom-daemon-update.sh --fetch. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"
  return 0
}

# Invoke the guard before either merge path attempts the actual merge API call.
_check_no_open_stacked_children

# ---------------------------------------------------------------------------
# Pre-merge version policy guard (#7827): feature PRs must not hand-edit a
# version-bearing value — the merge workflow owns bumps (#7743). The guard is
# `loom-daemon merge-pr version-policy` (Rust, loom-daemon/src/merge_pr/
# version_policy.rs — #8191 slice): it runs the canonical
# check-defaults-version-bump.sh --forbid-bump against the merge base, from the
# PR head's copy when the PR's own commits change the version-policy machinery
# (#8284), and classifies pass / skip / block. It prints `WARNING`/`BLOCK<TAB>
# line` records, replayed here through warning/error; exit 1 = confirmed edit.
# Any other exit (a missing or older daemon) is a guard fault, and guard faults
# have always skipped with a warning here, never blocked: CI's
# defaults-version-bump-check job is the policy's primary enforcement (see the
# CLI module docs for why this gate alone does not fail closed).
_check_defaults_version_bump_collision() {
  local out rc=0 level text blk="" dry=(); [[ "${DRY_RUN:-false}" != "true" ]] || dry=(--dry-run)
  out="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr version-policy --repo-root "$REPO_ROOT" --default-branch "${DEFAULT_BRANCH_NAME:-}" --branch "${PR_BRANCH:-}" --head-sha "${PR_HEAD_SHA:-}" --pr "${PR_NUMBER:-}" ${dry[@]+"${dry[@]}"})" || rc=$?
  while IFS=$'\t' read -r level text; do case "$level" in WARNING) warning "$text" ;; BLOCK) blk+="${blk:+$'\n'}$text" ;; esac; done <<< "$out"
  [[ $rc -ne 1 || -z "$blk" ]] || error "$blk"
  [[ $rc -eq 0 ]] || warning "Version policy guard (#7827) did not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr version-policy' exited $rc (a loom-daemon predating #8191's slice has no such verb). Skipping, as for any guard fault: CI's defaults-version-bump-check job still enforces the policy. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"
  return 0
}

# Invoke this guard too, before either merge path attempts the actual merge
# API call — same reasoning as _check_no_open_stacked_children above.
_check_defaults_version_bump_collision

# Pre-merge workflow-scope guard (#10539): `loom-daemon merge-pr workflow-scope`.
# Exit 1 = PR touches .github/workflows/ and the token lacks `workflow`; anything else proceeds (fail open, so an older daemon is a skip).
_check_workflow_scope() { local out rc=0; [[ "${FORGE_TYPE:-github}" == "github" ]] || return 0; out="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr workflow-scope --repo "$REPO_NWO" --pr "$PR_NUMBER" 2>/dev/null)" || rc=$?; [[ $rc -eq 1 ]] || return 0; [[ "${DRY_RUN:-false}" != "true" ]] || { warning "[dry-run] Would BLOCK: $out"; return 0; }; error "$out"; }
_check_workflow_scope

# _trusted_pr_comments <nwo> <pr> -- emit the bodies of `<nwo>`'s PR <pr>`
# comments whose authors Loom trusts as control-signal sources (#9548): a repo
# insider by author_association, one of THIS fleet's Apps, this daemon's own
# identity, or forge.trustedCommenters. The listing is fetched via the REST
# API (which carries user.login + author_association);
# forge_get_pr_comments's bodies-only shape drops authorship -- the exact
# hazard #9548 names -- so every control-phrase reader over PR comments must
# go through here instead (that helper is retired from lib/forge-helpers.sh,
# so no new reader can reach for it). An untrusted or unattributed marker
# counts as ABSENT. Gitea listings carry no author_association and filter to absence
# until a trust mapping exists (said on stderr, never swallowed silently).
# Always exits 0: a filter that cannot run yields nothing, and the CALLER
# decides whether that is fail-open advisory or fail-closed for its signal.
_trusted_pr_comments() {
  local nwo="$1" pr="$2" comments
  if [[ "${FORGE_TYPE:-github}" == "gitea" ]]; then
    echo "trusted-comments: comment authorship cannot be authenticated on Gitea yet (#9548) — comments read as absent" >&2
    return 0
  fi
  comments="$(gh api "repos/$nwo/issues/$pr/comments" --paginate 2>/dev/null || true)"
  [[ -n "$comments" ]] || return 0
  local filtered
  filtered="$(printf '%s\n' "$comments" | "${LOOM_DAEMON_BIN:-loom-daemon}" forge trusted-comments 2>/dev/null)" || {
    echo "trusted-comments: could not authenticate comment authors ('loom-daemon forge trusted-comments' failed); comments read as absent (#9548)" >&2
    return 0
  }
  [[ -n "$filtered" ]] || return 0
  printf '%s\n' "$filtered" | jq -r '.[].body' 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Pre-merge loom:pr review-signal guard (#7419).
#
# `loom:pr` is the only forge-visible statement that the CURRENT head passed
# Judge review. Champion's auto-merge path (champion-pr-merge.md) already
# refuses to merge without it, but this shared script — driven directly by
# humans and in-session agents, not just Champion — previously merged
# whatever PR it was pointed at regardless of label state. That gap let a
# real incident through: a Doctor rebase cleared `loom:pr` via the staleness
# guard, and a human running this script directly moments later squash-merged
# a head no Judge had reviewed, with zero friction at the one point it was
# cheap (#7419).
#
# Default is a hard block (error, exit 1) — the same shape as
# _check_no_open_stacked_children / _check_defaults_version_bump_collision
# above — printing the CURRENT label set and head SHA so the operator can see
# exactly what they are about to merge. --allow-unapproved bypasses the
# block (operator asserts responsibility, mirroring --allow-stacked-children
# and --worktree-path); the bypass is always recorded as a loud warning
# (mirrors --allow-stacked-children's own override warning) and, on a REAL
# run only (never --dry-run — a preview must have zero forge side effects),
# best-effort recorded as a PR comment audit trail too, mirroring the
# _post_premature_close_comment / partial-increment comment pattern already
# used elsewhere in this file. --dry-run reports the would-be block without
# exiting 1 or merging, same dry-run contract as both guards above.
#
# A present `loom:pr` is the overwhelmingly common case and must add zero
# overhead on that path: labels were already extracted from the initial
# $PR_JSON fetch into $PR_LABELS above, so this needs no extra API call to
# pass through.
#
# AC #3: when `loom:pr` IS present, also WARN (never hard-block — presence of
# loom:pr already means Judge approved SOME head, just possibly not the
# current one, which is a softer signal than the missing-label case above) if
# a Champion `<!-- champion:hold-state head=<sha> -->` marker (see
# champion-pr-merge.md's own hold-state tracking) names a SHA that differs
# from the current head. forge_get_pr's response has no `.comments` (unlike
# champion-pr-merge.md's own `gh pr view --json comments,...` fetch), so this
# reads them via _trusted_pr_comments (above), which keeps only
# trusted-authored comments (#9548).
#
# The marker extraction and the staleness comparison are
# `loom-daemon merge-pr hold-state` (Rust, loom-daemon/src/merge_pr/
# hold_state.rs -- #8191 slice). Only the forge READ stays here, so this
# script keeps owning the GitHub/Gitea split _trusted_pr_comments encodes.
# The retired `grep -o '...head=[0-9a-f]*' | tail -1 | sed` pipeline lost this
# warning silently in two ways the port fixes: `[0-9a-f]*` also matched the
# documentation line `head=<sha>` (quoted in champion-pr-merge.md and in this
# file), and `tail -1` then let that empty capture erase a real hold's SHA;
# and a bare substring anywhere -- prose, backticks, an example -- counted as
# recorded state, the hazard Champion's own reader answered with `startswith`
# (#5371). See the module docs for both, and for the fence-stripping
# divergence deliberately NOT taken.
#
# Advisory, so it fails OPEN, unlike every gate around it: a binary that
# cannot run this check has not found a reason to stop the merge, and turning
# "could not warn" into a refusal would make an advisory note more fatal than
# the gates. The fault is still said out loud rather than swallowed.
_check_champion_hold_state_staleness() {
  local comments msg rc=0
  # #9548: the champion:hold-state marker is a control phrase (it records
  # which tree Champion's hold/approval state was recorded against) and counts
  # only from a trusted author. _trusted_pr_comments emits bodies of
  # trusted-authored comments only; an untrusted or unattributed marker counts
  # as ABSENT -- it can neither raise a stale-hold warning nor mask a real
  # one. Gitea listings filter to absence (the helper says so on stderr).
  #
  # Advisory, so it fails OPEN, unchanged from #8191: a filter that cannot run
  # has not found a reason to stop the merge, and turning "could not warn"
  # into a refusal would make an advisory note more fatal than the gates. The
  # fault is still said out loud rather than swallowed.
  comments="$(_trusted_pr_comments "$REPO_NWO" "$PR_NUMBER")"
  [[ -n "$comments" ]] || return 0
  msg="$(printf '%s\n' "$comments" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr hold-state --pr "$PR_NUMBER" --head-sha "$PR_HEAD_SHA" 2>/dev/null)" || rc=$?
  [[ $rc -eq 0 ]] || { warning "The champion:hold-state staleness check (#7419) did not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr hold-state' exited $rc (a loom-daemon predating #8191's slice has no such verb). Advisory only: the merge is NOT blocked by this, but nothing verified that Champion's recorded hold head matches the head being merged. Build or install loom-daemon (cargo build --release -p loom-daemon, or re-run the Loom installer) to restore it."; return 0; }
  [[ "$msg" == "LOOM-HOLD-STATE-CLEAN" || -z "$msg" ]] || warning "$msg"
}

# The decision itself (loom:pr present? overridden? blocked, and the exact
# message) is `loom-daemon merge-pr loom-pr-guard` (Rust,
# loom-daemon/src/merge_pr/loom_pr_guard.rs -- #7419, a slice of #8191). No
# requires-daemon floor of its own: an older binary that does not know this
# verb exits non-zero without the CLEAN sentinel like any other guard fault,
# and the fail-closed branch below refuses the merge exactly as if `loom:pr`
# were genuinely absent with no override -- the safe direction, never a
# silent pass. --allow-unapproved is threaded through as a flag because it
# changes the VERDICT (override vs. block), not just how a verdict is
# displayed -- unlike --dry-run, which stays entirely shell-side, wrapping
# the same "would block" text in a warning instead of an error, same shape as
# every other guard in this file.
_check_loom_pr_label() {
  local msg rc=0 flags=()
  [[ "$ALLOW_UNAPPROVED" == "true" ]] && flags+=(--allow-unapproved)
  msg="$(printf '%s\n' "$PR_LABELS" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr loom-pr-guard --pr "$PR_NUMBER" --head-sha "$PR_HEAD_SHA" ${flags[@]+"${flags[@]}"} 2>/dev/null)" || rc=$?
  if [[ $rc -eq 0 && "$msg" == "LOOM-PR-GUARD-CLEAN" ]]; then
    _check_champion_hold_state_staleness
    return 0
  fi
  if [[ $rc -eq 0 && "$ALLOW_UNAPPROVED" == "true" && "$msg" == "loom:pr guard:"* ]]; then
    warning "$msg"
    # #8896: the audit comment is posted AT MOST ONCE per merge-pr.sh run. On
    # the --auto path this guard runs twice against the same merge — once at
    # queue time, once from _revalidate_merge_guards() after the settle-wait —
    # and an --allow-unapproved run with no loom:pr posted the identical
    # "Merge Proceeded Without loom:pr" comment both times. The warning above
    # is unconditional (it is the log record, and the second evaluation is a
    # real re-check worth logging); only the durable forge comment is deduped.
    # The flag is set only once the comment actually LANDS, so a failed first
    # post still leaves the post-wait re-check free to record the override.
    if [[ "$DRY_RUN" != "true" && "${_LOOM_PR_OVERRIDE_COMMENTED:-false}" != "true" ]]; then
      # The body is `loom-daemon merge-pr loom-pr-override-comment` (Rust,
      # loom-daemon/src/merge_pr/loom_pr_guard.rs, `override_comment` -- #8191
      # slice), byte-frozen from this shell. Same LOOM-MERGE-PR-COMMENT
      # sentinel protocol `_mp_post_partial_comment` uses, inlined here rather
      # than reused because this comment posts on $PR_NUMBER itself, not an
      # issue. Fails OPEN with a warning naming the gap: the merge and the
      # override both already happened, so a missing body costs only the note.
      local _lpoc_out _lpoc_rc=0 nl=$'\n'
      _lpoc_out="$(printf '%s\n' "$PR_LABELS" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr loom-pr-override-comment --pr "$PR_NUMBER" --head-sha "$PR_HEAD_SHA" 2>/dev/null)" || _lpoc_rc=$?
      if [[ $_lpoc_rc -eq 0 && "$_lpoc_out" == "LOOM-MERGE-PR-COMMENT$nl"* ]]; then
        if forge_gh_comment_rl_safe "$REPO_NWO" "$PR_NUMBER" "${_lpoc_out#LOOM-MERGE-PR-COMMENT"$nl"}" 2>/dev/null; then _LOOM_PR_OVERRIDE_COMMENTED=true; else warning "Could not post loom:pr override audit comment on PR #$PR_NUMBER (merge proceeds anyway; the warning above is still the log record)"; fi
      else
        warning "The loom:pr override audit comment for PR #$PR_NUMBER (#7419) was NOT posted — 'merge-pr loom-pr-override-comment' exited $_lpoc_rc without the LOOM-MERGE-PR-COMMENT sentinel (a loom-daemon predating #8191's slice has no such verb). Advisory only: the merge and the override both already happened — only this explanatory note is missing, and an empty comment is never posted in its place. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"
      fi
    fi
    return 0
  fi
  [[ $rc -eq 1 && "$msg" == "Merge blocked:"* ]] || msg="Merge blocked: PR #$PR_NUMBER's loom:pr review-signal guard (#7419) could not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr loom-pr-guard' exited $rc without a recognized verdict. A guard that cannot run refuses the merge rather than passing it: a caller cannot tell 'reviewed' from 'never checked', so only a positive signal is accepted. Build or install loom-daemon (cargo build --release -p loom-daemon, or re-run the Loom installer), then re-run this merge."
  if [[ "$DRY_RUN" == "true" ]]; then
    warning "[dry-run] Would BLOCK merge of PR #$PR_NUMBER: $msg"
    return 0
  fi
  error "$msg"
}

# Invoke the guard before either merge path attempts the actual merge API
# call — same reasoning as the two guards above.
_check_loom_pr_label

# #8191: prove the closing-reference analysis can actually RUN, here, while
# `set -e` is live and before anything has been mutated.
#
# _mp_refs()'s own `error` exit cannot do this. Every consumer calls it inside
# `$(...)` inside _check_partial_increment_close_conflict /
# _reset_partial_increment_labels, and both of those are invoked as `... ||
# true`. Bash suppresses `set -e` in that context, so the exit only kills the
# subshell: the caller receives an EMPTY string and reads it as "no references
# found" — the reading that closes an unfinished issue or reopens a correctly
# closed one. Review reproduced all five skew shapes reaching the merge that
# way, including the default rollout state (a resynced script against an
# installed binary that predates this subcommand and answers exit 2).
#
# Verifying in isolation, as the original fix did, tested the function and not
# the call shape. This is the call shape.

# ---------------------------------------------------------------------------
# Pre-merge verdict-label contradiction guard (#8112).
#
# A PR can carry BOTH `loom:pr` (approved) and a blocking verdict/hold label
# simultaneously — two concurrent Judge passes reaching different verdicts on
# the same head ~45s apart is a real, observed incident (PR #8076), not a
# hypothetical. `_check_loom_pr_label` above only checks loom:pr's ABSENCE; it
# has no way to see a contradicting label standing beside a PRESENT loom:pr.
#
# Judge/Doctor's Verdict-Time CAS Recheck (judge.md / doctor.md) and
# Champion's Verdict-State Janitor Part 1 (champion-pr-merge.md) already exist
# to prevent and auto-resolve exactly this contradiction — but both live in
# markdown-orchestrated agent steps, not in this script, so a human or agent
# invoking merge-pr.sh directly (the documented, canonical way to merge —
# never `gh pr merge`, see CLAUDE.md) bypasses them entirely. This guard is
# the backstop at the one point every merge path actually funnels through.
#
# Deliberately no bypass flag: --allow-unapproved overrides "nobody reviewed
# this head" (an absence of signal); this guard blocks "a reviewer explicitly
# said no" (a present, contradicting signal). Those are different acts, and
# only the first has a documented override. The fix for a real block here is
# a fresh Judge verdict, not a flag.
#
# ---------------------------------------------------------------------------
# DAEMON VERSION FLOOR (#8285)
# ---------------------------------------------------------------------------
# This script hard-requires two loom-daemon subcommands and fails CLOSED when
# the resolved binary predates either. Failing closed is right — an empty
# answer from the closing-reference analysis is indistinguishable from "no
# closing refs", which would silently close an unfinished issue — but the
# version floor has to be SAYABLE, or the refusal cannot tell an operator what
# to roll to. These two markers are that declaration. They are the single
# source of truth: `_mp_daemon_roll_hint` below reads them back out of
# ${BASH_SOURCE[0]} at refusal time, and
# scripts/check-daemon-subcommand-versions.sh enforces that no NEW daemon
# dependency lands here (or in any other shell script) without one.
#
# On 2026-09-18 a host running 0.19.161 against a `main` that carried 0.19.170+
# stopped every merge outright — `.loom/scripts` is a symlink into
# `defaults/scripts` in the primary checkout, so the floor moved the instant
# `main` was pulled, while the auto-update loop deferred ~4.5h behind the
# build-stampede guard (#8252). The refusal named neither the version nor the
# roll command. That is what these markers and the hint below fix.
#
# requires-daemon: merge-pr >= 0.19.465   the NEWEST fail-closed verb in this family, not the oldest (#8967): checks-failure (#8191 slice, merged in #9272); the other fail-closed verbs are partial-conflict >= 0.19.464 (#9246), classify-response >= 0.19.456 (#9228), loom-pr-guard >= 0.19.375 (#7419/#8926) and verdict-contradiction >= 0.19.172 (#8112/#8124). #12063 slice 1 LOWERED it from 0.19.763 by deleting ci-result (#10444) and stale-checks (#8248): removing a verb never makes an older binary refuse. One marker covers the whole `merge-pr` family, so it MUST name the highest of them — a host that satisfied an older floor but not the newest fail-closed verb had every merge refused while the hint quoted a floor it already met. Fail-open verbs (required-checks — a binary without it warns and leaves branch protection as the guard (#10688); tree-checks — opt-in, called only when merge.treeChecks is declared, then refusing on an older binary (#10026); head-sync-retry, hold-state, delete-branch, zero-checks-settle, check-runs-streak, check-runs-rollup, stacked-children, retarget-children — any non-0 exit keeps the merged parent's remote branch rather than deleting it (#9372), revalidate-head — falls back to the retired jq predicate (#8191 slice), pr-state — no verdict falls back to the retired PR_MERGED/PR_STATE predicate (#8191 slice), version-policy, partial-reset, partial-comment, loom-pr-override-comment, closed-building, issue-close-gate, dirty-guard, worktree-preserve, worktree-contains, cleanup-paths — partial-comment and loom-pr-override-comment each render a POST-merge audit comment and skip the note rather than posting an empty one; the last four decline only the post-merge worktree removal, never the merge; worktree-preserve preserves the worktree when the verb is missing, worktree-contains declines --worktree-path's override cleanup and keeps that path when it is missing, and cleanup-paths leaves the cleanup targets unnamed so nothing is removed; post-merge-cleanup (#8191 orchestrator slice 1) folds all of those into one in-process verb and fails the same way: a binary lacking it, or exiting without its DONE terminator, removes no worktree and deletes no local branch and warns, because the merge itself already succeeded) deliberately do NOT raise it; the fail-direction table in tests/test-merge-pr-daemon-version-floor.sh enforces both halves.
# requires-daemon: merge-pr-refs >= 0.19.170   closing-reference analysis (#8191, landed in #8199)
# requires-daemon: forge optional   --merge-method validation (#8845); command -v probes first, and any non-0/1 exit (older daemon lacking the subcommand, or a Gitea decline) falls back to the unvalidated request with a warning
# The `merge-pr >=` floor above covers the whole subcommand group, including
# #8191's post-merge porcelain lookups (`merge-pr worktree-primary` /
# `worktree-branch-for` / `worktree-find-by-branch`, see _mp_worktree, plus the
# --worktree-path parse-time `worktree-contains` check and the `remove-gate`
# primary/sentinel guard, which fails CLOSED to a refused removal), and it is
# deliberately NOT raised to their landing version. Raising it refuses the MERGE
# on every host one release behind — the 2026-09-18 incident above — whereas a
# daemon missing only those leaf verbs declines post-merge CLEANUP: the two
# branch lookups degrade to "delete nothing" and the #3710 primary-worktree guard
# refuses the removal rather than force-removing on no evidence. Skipped cleanup
# is recoverable (`loom-clean`, the daemon's reaper, `worktree.sh remove`);
# removing the primary checkout is not. Leaving the floor where it is also keeps
# _mp_daemon_roll_hint's `${sub} >= ` lookup resolving to the merge-gate version,
# which is the one a refused MERGE should name.
# requires-daemon: notify-cleared-blockers optional   #9102 — the post-merge close-triggered loom:blocked re-check; a daemon lacking the verb exits non-zero, `_notify_cleared_blockers` prints one warning and the merge proceeds. A delayed notice, never a failed merge: the next sweep's `check-stale-blocked` pre-wave pass reports the same stale block.
# requires-daemon: record-rework optional   #9444 — the two in-sweep rework markers this script emits (a `rebase` when it syncs a base that moved, a `merge_conflict` when it refuses a PR that genuinely conflicts). Both calls are `>/dev/null 2>&1 || true`: a daemon lacking the verb records nothing and the merge is byte-for-byte unaffected. Pure telemetry — the cost of a missing marker is one under-reported environmental rework in `sweep.outcome`'s `rework_events`, never a merge that did or did not happen.
#
# Per-verb landing versions for FAIL-OPEN verbs (#9377, via #11167). MESSAGE
# TEXT ONLY, never gating: check-daemon-subcommand-versions.sh does not read
# this prefix, and nothing refuses on it. A fail-open verb deliberately does not
# raise the family `requires-daemon:` floor above, so without these the hint
# for its failure quoted that (unrelated, fail-closed) floor — which a host can
# already exceed while still lacking the verb (rjwalters/repo#494: told to roll
# to 0.19.172 while running 0.19.461, which predated a fail-open verb). Each
# version is `git show <landing commit>:VERSION` + 1. An unannotated verb falls
# back to the family floor, which is exactly the pre-#9377 message.
# daemon-verb-hint: merge-pr-refs loom-issue-trailer-warnings >= 0.19.512   d8fafb19a (merge of #9501, #9465), first released in v0.19.512
#
# _mp_daemon_roll_hint <subcommand> [resolved-bin] [verb] -- the concrete,
# host-local remediation for "your loom-daemon is too old for <subcommand>":
# the declared floor, what the resolved binary actually reports, the
# artifact-first roll command for THIS host, and the two fallbacks when no
# artifact carries the floor yet. [verb] is the sub-subcommand that actually
# failed: when it has a `daemon-verb-hint:` above, the message names
# '<subcommand> <verb>' and that verb's own version; otherwise the family
# floor. Written as a one-liner because merge-pr.sh is frozen by the
# file-size ratchet (scripts/check-file-size-budget.sh) and may not grow.
#
# Both reads carry `|| true` deliberately. merge-pr.sh runs under `set -euo
# pipefail`, and a bare `x="$(cmd)"` assignment adopts cmd's status — so an
# unreadable ${BASH_SOURCE[0]} or a `--version` that exits non-zero would abort
# THIS function partway, and the caller (an `error "… $(…)"` argument) would
# print a refusal with the remediation silently truncated off the end. A
# best-effort diagnostic must never be able to degrade the message it is
# decorating. (The `if` over a `[[ … ]] && { … }` is for the same reason stated
# defensively; bash exempts AND-lists from `set -e`, so that one is style.)
_mp_daemon_roll_hint() { local sub="${1:-}" bin="${2:-}" verb="${3:-}" min="" have=""; [[ -z "$verb" ]] || min="$(sed -n "/^# daemon-verb-hint: ${sub} ${verb} >= /{s|^# daemon-verb-hint: ${sub} ${verb} >= \\([0-9][0-9.]*\\).*|\\1|p;q;}" "${BASH_SOURCE[0]}" 2>/dev/null || true)"; if [[ -n "$min" ]]; then sub="$sub $verb"; else min="$(sed -n "/^# requires-daemon: ${sub} >= /{s|^# requires-daemon: ${sub} >= \\([0-9][0-9.]*\\).*|\\1|p;q;}" "${BASH_SOURCE[0]}" 2>/dev/null || true)"; fi; if [[ -n "$bin" && -x "$bin" ]]; then have="$("$bin" --version 2>/dev/null || true)"; have="${have%%$'\n'*}"; fi; printf "REMEDIATION: this script requires loom-daemon >= %s for '%s'%s. Roll THIS host, artifact-first: %s/cli/loom-daemon-update.sh --fetch — it resolves the newest published release >= the installed version, verifies its checksum (and signature when present), provisions it and restarts the daemon under its supervisor; then re-run this merge. If no release artifact carries %s yet (releases are cut at fleet-rollable boundaries, not on every VERSION bump — see .loom/docs/release-cadence.md), either build it yourself — cargo build --release -p loom-daemon — and export LOOM_DAEMON_BIN=<repo>/target/release/loom-daemon, or pin LOOM_DAEMON_BIN to an existing build that already has '%s'. Confirm before re-running: %s --version && %s %s --help" "${min:-<undeclared>}" "$sub" "${have:+ (the resolved binary reports: $have)}" "${SCRIPT_DIR:-.loom/scripts}" "${min:-that version}" "$sub" "${bin:-loom-daemon}" "${bin:-loom-daemon}" "$sub"; }
_check_verdict_label_contradiction() { local msg rc=0; msg="$(printf '%s\n' "$PR_LABELS" | LOOM_SEQUENCED_HOLD_REPO="${REPO_NWO:-}" "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr verdict-contradiction --pr "$PR_NUMBER" --head-sha "$PR_HEAD_SHA" 2>/dev/null)" || rc=$?; [[ $rc -eq 0 && "$msg" == "LOOM-VERDICT-CLEAN" ]] && return 0; if [[ $rc -eq 7 && "$msg" == "LOOM-SEQUENCED-HOLD "* ]]; then [[ "$DRY_RUN" != "true" ]] || { warning "[dry-run] Would HOLD merge of PR #$PR_NUMBER (exit 7): $msg"; return 0; }; echo "${msg%%$'\n'*}"; warning "${msg#*$'\n'}" >&2; warning "Exiting 7: held by a sequencing order (#11330); nothing was written. Re-queue — never a failure comment." >&2; exit 7; fi; [[ $rc -eq 1 && "$msg" == "Merge blocked:"* ]] || msg="Merge blocked: PR #$PR_NUMBER's verdict-label contradiction guard (#8112) could not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr verdict-contradiction' exited $rc without the LOOM-VERDICT-CLEAN signal. A guard that cannot run refuses the merge rather than passing it: a caller cannot tell 'found nothing' from 'never ran', so only a positive clean signal is accepted. $(_mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"; if [[ "$DRY_RUN" == "true" ]]; then warning "[dry-run] Would BLOCK merge of PR #$PR_NUMBER: $msg"; return 0; fi; error "$msg"; }
_check_verdict_label_contradiction

# The #8248 required-check freshness guard, its #8508 re-date remedy (exit 4),
# the #10388 merge-tree re-verification and the #10444/#10567 CI-run
# conclusion gate were deleted by #12063 slice 1: required checks at the head
# are GitHub's to enforce (the ruleset's required contexts, strict=false), and
# a semantic conflict that lands is main CI's to catch and revert.
# #10026: repo-declared merge.treeChecks run on the merge tree (base + PR head) by `loom-daemon merge-pr tree-checks`. Strict no-op (no daemon call) unless .loom/config.json declares checks; once declared it fails CLOSED, so a missing/older daemon refuses (deliberately no requires-daemon floor: repos that do not opt in are unaffected). --allow-red-tree warns; the daemon records the audit comment. See defaults/docs/merge-tree-checks.md.
_check_tree_checks() { local n msg rc=0 base_ref flags=(); n="$(jq -r '(.merge.treeChecks // []) | length' "${REPO_ROOT:-.}/.loom/config.json" 2>/dev/null)" || true; [[ -f "${REPO_ROOT:-.}/.loom/config.json" && "$n" != "0" ]] || return 0; base_ref="$(echo "$PR_JSON" | jq -r '.base.ref // empty')"; [[ "$DRY_RUN" != "true" ]] || flags+=(--dry-run); [[ "$ALLOW_RED_TREE" != "true" ]] || flags+=(--allow-red-tree); msg="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr tree-checks --pr "$PR_NUMBER" --repo "$REPO_NWO" --head-sha "$PR_HEAD_SHA" --base-ref "${base_ref:-${DEFAULT_BRANCH_NAME:-main}}" --config "${REPO_ROOT:-.}/.loom/config.json" ${flags[@]+"${flags[@]}"})" || rc=$?; [[ $rc -eq 0 && "$msg" == "LOOM-TREE-CHECKS-CLEAN" ]] && return 0; [[ $rc -eq 0 && "$msg" == "LOOM-TREE-CHECKS-BYPASSED"* ]] && { warning "$msg"; return 0; }; [[ $rc -eq 1 ]] || msg="Merge blocked: PR #$PR_NUMBER's merge.treeChecks gate (#10026) could not run (exit $rc): $msg. A gate that cannot run refuses the merge; fix the cause (build/roll loom-daemon, config, fetch); --allow-red-tree overrides only a failing check, not a gate that cannot run."; [[ "$DRY_RUN" != "true" ]] || { warning "[dry-run] Would BLOCK merge of PR #$PR_NUMBER: $msg"; return 0; }; error "$msg"; }
_check_tree_checks
# #10688: no required context may be red, missing or pending on the PLAIN path (called below, after the
# merge-precondition read; --auto's wait loop already refuses red required checks via checks-failure),
# where branch protection is the only other guard and a bypass-capable (admin) token walks through it.
# Logic: `loom-daemon merge-pr required-checks` (merge_pr/checks_failure/required.rs); GitHub-only like
# the forge read. Red -> exit 1 unless every red context is named by --allow-red-required (the daemon
# posts the audit comment); missing/pending -> HOLD exit 5; unreadable forge -> exit 1. FAIL-OPEN on a
# daemon predating the verb (warns; branch protection stays the guard), so no requires-daemon floor raise.
_check_required_checks() { [[ "$FORGE_TYPE" == "github" ]] || return 0; local msg rc=0 a f=() base_ref; base_ref="$(echo "$PR_JSON" | jq -r '.base.ref // empty')"; for a in ${ALLOW_RED_REQUIRED[@]+"${ALLOW_RED_REQUIRED[@]}"}; do f+=(--allow-red-required "$a"); done; [[ "$DRY_RUN" != "true" ]] || f+=(--dry-run); msg="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr required-checks --pr "$PR_NUMBER" --repo "$REPO_NWO" --head-sha "${MERGE_PRECONDITION_SHA:-$PR_HEAD_SHA}" --base-ref "${base_ref:-${DEFAULT_BRANCH_NAME:-main}}" ${f[@]+"${f[@]}"})" || rc=$?; case "$rc:$msg" in "0:LOOM-REQUIRED-CHECKS-CLEAN") return 0 ;; "0:LOOM-REQUIRED-CHECKS-BYPASSED "*) warning "${msg#LOOM-REQUIRED-CHECKS-BYPASSED }"; return 0 ;; "3:LOOM-REQUIRED-CHECKS-HOLD "*) msg="Merge held: PR #$PR_NUMBER — ${msg#LOOM-REQUIRED-CHECKS-HOLD }"; [[ "$DRY_RUN" != "true" ]] || { warning "[dry-run] Would HOLD merge of PR #$PR_NUMBER (exit 5): $msg"; return 0; }; warning "$msg" >&2; warning "Exiting 5: not merged this pass (#10688). Re-attempt on a later pass." >&2; exit 5 ;; "1:Merge blocked:"*) ;; "2:LOOM-REQUIRED-CHECKS-UNREADABLE "*) msg="Merge blocked: PR #$PR_NUMBER's required-context conclusion gate (#10688) could not read the forge: ${msg#LOOM-REQUIRED-CHECKS-UNREADABLE }. An unreadable gate is not permission to merge; re-run once the forge API is healthy." ;; *) warning "Required-context conclusion gate (#10688) did not run ('merge-pr required-checks' exited $rc${msg:+: $msg}); proceeding with branch protection as the only check. Roll loom-daemon to a build carrying 'merge-pr required-checks' to restore it."; return 0 ;; esac; [[ "$DRY_RUN" != "true" ]] || { warning "[dry-run] Would BLOCK merge of PR #$PR_NUMBER: $msg"; return 0; }; error "$msg"; }

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
PARTIAL_OPEN_BEFORE_MERGE=""; PARTIAL_CONFLICT_ISSUES=""

# Closing-reference / partial-increment analysis, ported to Rust (#8191).
#
# These predicates decide whether merging would close an issue this PR
# only declared itself a PART OF. Their whole bug history is about what a
# stacked grep/sed/awk pipeline accidentally matched -- #5234 (a backticked
# hypothetical mention read as a declaration, reopening a correctly closed
# issue) and the numbered-list ordinal trap (`3. Part of #789` read as
# referencing both 3 and 789). Both are ordering bugs inside a pipeline.
#
# The body goes over STDIN, never argv: it is untrusted external content and
# routinely tens of kilobytes.
#
# A missing binary is FATAL here rather than degrading. Every one of these
# feeds a merge-or-refuse decision, and an empty answer is not "no references
# found" -- it is "no answer", which would silently close an unfinished issue
# or reopen a correctly closed one. Failing loudly is the safe direction.
_mp_refs() {
  # Resolved inline, not via lib/locate-daemon-bin.sh: the retained suites
  # extract these functions and source them alone, with no libs present.
  # LOOM_DAEMON_SELF_BIN first, per #8134 — it means "the binary that
  # IMPLEMENTS this entry point", which is exactly what this is.
  local bin out rc=0
  bin="${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-}}"
  if [[ -n "$bin" ]]; then
    # A PINNED path that is unusable must refuse, not quietly resolve a
    # different binary off PATH — that substitutes an unknown version for the
    # one an operator deliberately selected.
    [[ -x "$bin" ]] || error "merge-pr.sh: LOOM_DAEMON_SELF_BIN/LOOM_DAEMON_BIN is set to '$bin', which is not executable. Refusing rather than silently falling back to a different loom-daemon."
  else
    bin="$(command -v loom-daemon 2>/dev/null || printf '%s' "$HOME/.local/bin/loom-daemon")"
    [[ -x "$bin" ]] || error "merge-pr.sh needs loom-daemon for its closing-reference analysis (#8191) and could not resolve one. Refusing rather than proceeding with no answer: an empty result is indistinguishable from 'no references', which would close an unfinished issue or reopen a correctly closed one. $(_mp_daemon_roll_hint merge-pr-refs)"
  fi
  # `|| rc=$?`, not `; rc=$?`: under `set -e` a failing command substitution in
  # a bare assignment aborts the script AT THAT LINE, so the check below never
  # ran and the refusal was silent — fail-closed, but with nothing said.
  out="$("$bin" merge-pr-refs "$@" 2>/dev/null)" || rc=$?
  [[ "$rc" -eq 0 ]] || error "merge-pr.sh's closing-reference analysis failed: '$bin merge-pr-refs $*' exited $rc. A loom-daemon predating #8191 has no such subcommand. Refusing rather than treating an empty result as 'no references'. $(_mp_daemon_roll_hint merge-pr-refs "$bin")"
  printf '%s' "$out"
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
  printf '%s\n' "$1" | _mp_refs partial-increment-refs
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

  # bt_warn/bt_rc are declared here, not next to their own assignment below,
  # purely so that assignment can own a line: see the SC2046 note below.
  local partial_refs bt_warn bt_rc=0 lt_warn lt_rc=0
  partial_refs="$(_partial_increment_refs "$pr_body")"

  # Backticked-trailer warning (#5690, ported to Rust #8831 —
  # cli/merge_pr_refs.rs's `backticks-partial-increment-warnings`, which
  # recomputes both declaration sets from $pr_body itself and diffs them, so
  # this call passes nothing but the PR number and dry-run state). Runs BEFORE
  # the early return below because the case it exists for is precisely the one
  # where $partial_refs is EMPTY — a trailer the author backticked, which
  # parses as no declaration at all. Pure text analysis, no forge calls, so
  # the common (non-partial-increment) path still costs zero extra requests.
  # (The statements below share one line deliberately (#8831). The unquoted $(...) is intentional: it
  # expands to a single `--dry-run` token or nothing, never anything word
  # splitting could mis-tokenize — hence the SC2046 disable directly below.
  # That directive covers only the ONE statement that follows it, which is why
  # bt_warn/bt_rc are declared up with `local partial_refs` instead of leading
  # this line: as `local bt_warn bt_rc=0; bt_warn="$(...)"` the disable landed
  # on the declaration and the real finding leaked into CI (#8985). Moving the
  # declaration rather than adding a line keeps the budget at net zero too.)
  #
  # #8897: a BARE assignment (not `local var=$(...)`) with `2>/dev/null` and
  # `|| bt_rc=$?`, mirroring _mp_refs's own `out="$(...)" || rc=$?` pattern
  # above — so a daemon that answers `closing-refs` (checked already) but
  # rejects this newer MODE (unrecognized-subcommand exit) is detected here
  # instead of only printing _mp_refs's hardcoded closing-ref "Refusing..."
  # wording to the terminal (wrong mode, wrong PR, wrong version) while the
  # merge proceeds anyway (the `local var=$(...)` exit-status swallow that made
  # this call fail-open in practice all along). This call stays advisory-only:
  # a mode failure is reported as a skipped check, never as a refusal.
  #
  # #9502 (via #11167): the `Loom-Issue: owner/repo#N` trailer's own detector
  # (`loom-issue-trailer-warnings`, #9465 — backticked or slug-less trailers)
  # rides the SAME line, after the #5690 call, with the identical posture: a
  # bare assignment + `|| lt_rc=$?`, a warning naming a too-old daemon (and
  # that verb's own landing version, via its `daemon-verb-hint:`) on a
  # non-zero exit, never a refusal. Before the early return for the same
  # reason: a PR declaring only a Loom-Issue link has no partial refs at all.
  # The `{ …; }` group exists only so the SC2046 disable covers both calls.
  # shellcheck disable=SC2046
  { bt_warn="$(printf '%s\n' "$pr_body" | _mp_refs backticks-partial-increment-warnings --pr "$PR_NUMBER" $([[ "${DRY_RUN:-false}" == "true" ]] && echo --dry-run) 2>/dev/null)" || bt_rc=$?; if [[ $bt_rc -eq 0 ]]; then [[ -z "$bt_warn" ]] || warning "$bt_warn"; else warning "Skipped backticked-trailer advisory warning check: loom-daemon rejected 'merge-pr-refs backticks-partial-increment-warnings' (exit $bt_rc) -- most likely a daemon predating this mode. Not refusing; this check is advisory-only."; fi; lt_warn="$(printf '%s\n' "$pr_body" | _mp_refs loom-issue-trailer-warnings --pr "$PR_NUMBER" $([[ "${DRY_RUN:-false}" == "true" ]] && echo --dry-run) 2>/dev/null)" || lt_rc=$?; if [[ $lt_rc -eq 0 ]]; then [[ -z "$lt_warn" ]] || warning "$lt_warn"; else warning "Skipped Loom-Issue trailer advisory warning check (#9465): loom-daemon rejected 'merge-pr-refs loom-issue-trailer-warnings' (exit $lt_rc) -- most likely a daemon predating this mode. Not refusing; this check is advisory-only. $(_mp_daemon_roll_hint merge-pr-refs "$(command -v "${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}" 2>/dev/null || true)" loom-issue-trailer-warnings)"; fi; }; [[ -n "$partial_refs" ]] || return 0

  # Which declared issues are open now, and which a closing reference will
  # close anyway (#4569), from three unioned signals: the body's own closing
  # keywords (quota-free regex); this PR's COMMIT MESSAGES (#4595) — quota-free
  # REST, and the source of the squash message this script does not override;
  # and GitHub's closingIssuesReferences (best-effort — empty under GraphQL
  # exhaustion, but it alone surfaces a Development-sidebar link). The commit
  # fetch happens only past the partial_refs early-return above, so the common
  # path costs zero extra API calls.
  #
  # The decision — the union, each issue's PR/open read, the membership test
  # and the source-attributed warning — is `loom-daemon merge-pr
  # partial-conflict` (Rust, loom-daemon/src/merge_pr/partial_conflict.rs —
  # #8191 slice). Only the forge reads stay here: fresh (uncached) plain
  # `gh api`, not $GH, per issue, mirroring _reset_one_partial_issue. They go
  # over stdin NUL-framed (a bash string cannot hold NUL, so the framing is
  # lossless and has no argv size limit). The plan's OPEN/CONFLICT<TAB>n lines
  # fill the two sets the post-merge pass reads; WARNING<TAB>text is replayed.
  # Fail CLOSED without the DONE terminator: an unread plan is not an empty
  # one, and read as empty it would leave a partial increment this merge
  # closes with nothing recorded to revert it.
  local frame=() issue_num plan rc=0 kind val dr=()
  frame=("$pr_body" "$(_pr_commit_messages)" "$(forge_pr_close_targets "$PR_NUMBER" "$GH" 2>/dev/null || true)")
  while IFS= read -r issue_num; do [[ -n "$issue_num" ]] || continue; frame+=("$issue_num" "$(gh api "repos/$REPO_NWO/issues/$issue_num" 2>/dev/null || echo '{}')"); done <<< "$partial_refs"
  [[ "${DRY_RUN:-false}" != "true" ]] || dr=(--dry-run)
  plan="$(printf '%s\0' "${frame[@]}" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr partial-conflict --pr "$PR_NUMBER" ${dr[@]+"${dr[@]}"} 2>/dev/null)" || rc=$?
  if [[ $rc -ne 0 || "$plan" != *"LOOM-PARTIAL-CONFLICT-DONE" ]]; then
    plan="Merge blocked: PR #$PR_NUMBER's partial-increment close-conflict guard (#4569) could not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr partial-conflict' exited $rc without the LOOM-PARTIAL-CONFLICT-DONE terminator (a loom-daemon predating #8191's slice has no such verb). Refusing rather than reading silence as 'no conflict': this plan records which declared partial increments a stray closing reference will close, which is what the post-merge pass reverts. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"
    [[ "${DRY_RUN:-false}" == "true" ]] || error "$plan"; warning "[dry-run] Would BLOCK merge of PR #$PR_NUMBER: $plan"; return 0
  fi
  while IFS=$'\t' read -r kind val; do
    case "$kind" in
      OPEN) PARTIAL_OPEN_BEFORE_MERGE="${PARTIAL_OPEN_BEFORE_MERGE:+$PARTIAL_OPEN_BEFORE_MERGE }$val" ;;
      CONFLICT) PARTIAL_CONFLICT_ISSUES="${PARTIAL_CONFLICT_ISSUES:+$PARTIAL_CONFLICT_ISSUES }$val" ;;
      WARNING) warning "$val" ;;
    esac
  done <<< "$plan"
  return 0
}

# Runs before either merge path so the operator sees the conflict BEFORE the
# close happens, and so --dry-run reports it without merging. Best-effort.
# #8191 preflight. One call, at top level: NOT inside `$(...)`, NOT behind
# `|| true`, and AFTER the definitions (_mp_refs lives inside the span the
# retained suite extracts, so it cannot be defined any earlier). _mp_refs's own
# `error` therefore exits the script here, which it cannot do from inside a
# command substitution in a `|| true` caller — the shape review proved lets all
# five version-skew cases reach the merge with an empty "no references" answer.
_mp_refs closing-refs </dev/null >/dev/null

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
# it is still open and still carries loom:building (reopening it first when
# this very merge auto-closed it through a stray closing keyword, #4569).
# Idempotent: a no-op when the issue is already closed, already lacks
# loom:building (e.g. re-claimed by a second builder), or is actually a PR.
#
# The decision — which of those cases this is, and the log text for each — is
# `loom-daemon merge-pr partial-reset` (Rust, loom-daemon/src/merge_pr/
# partial_reset.rs — #8191 slice), fed the fresh issue body on stdin. It prints
# the steps in order: `INFO`/`WARNING<TAB>text` to replay, `REOPEN`, `SWAP`.
# Only the mutations and their audit comments stay here. The fresh read uses
# plain `gh api` (uncached; not $GH, which may be gh-cached) so a stale cached
# view cannot mask a re-claim. Best-effort like the rest of this pass: a daemon
# that cannot plan (missing, or predating the verb) is a warning naming the
# manual swap, never a guessed mutation. The plan is read on fd 3 so no forge
# call below can consume it from stdin.
_reset_one_partial_issue() {
  local issue_num="$1" issue_json reopened=false out rc=0 level text flags=()
  issue_json="$(gh api "repos/$REPO_NWO/issues/$issue_num" 2>/dev/null || echo '{}')"
  ! _partial_ref_is_conflicted "$issue_num" || flags+=(--conflicted)
  ! _partial_ref_was_open_before_merge "$issue_num" || flags+=(--open-before-merge)
  out="$(printf '%s\n' "$issue_json" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr partial-reset --issue "$issue_num" --pr "$PR_NUMBER" --repo "$REPO_NWO" ${flags[@]+"${flags[@]}"} 2>/dev/null)" || rc=$?
  if [[ $rc -ne 0 ]]; then warning "Partial-increment reset for issue #$issue_num did not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr partial-reset' exited $rc (a loom-daemon predating #8191's slice has no such verb). Advisory only — the merge already happened and #$issue_num was left untouched; if it is still open and loom:building, return it to the ready queue by hand: gh issue edit $issue_num --repo $REPO_NWO --remove-label loom:building --add-label loom:issue $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"; return 0; fi
  while IFS=$'\t' read -r -u 3 level text; do
    case "$level" in
      INFO) info "$text" ;;
      WARNING) warning "$text" ;;
      REOPEN)
        # forge_gh_reopen_issue_rl_safe (#4856): falls back to a REST PATCH
        # (state=open) when `gh issue reopen`'s GraphQL mutation is rate-limited.
        if forge_gh_reopen_issue_rl_safe "$REPO_NWO" "$issue_num" 2>/dev/null; then
          success "Issue #$issue_num reopened (premature auto-close reverted)"
          reopened=true
          _post_premature_close_comment "$issue_num"
        else
          warning "Could not reopen issue #$issue_num after its premature auto-close — reopen manually: gh issue reopen $issue_num --repo $REPO_NWO"
          return 0
        fi ;;
      SWAP)
        # forge_gh_swap_label_rl_safe (#4856): falls back to REST (DELETE the old
        # label, POST the new one) when `gh issue edit`'s GraphQL mutation is
        # rate-limited, rather than silently dropping the label swap.
        if forge_gh_swap_label_rl_safe "$REPO_NWO" "$issue_num" "loom:building" "${text:-loom:issue}" 2>/dev/null; then
          success "Issue #$issue_num: loom:building -> ${text:-loom:issue} (partial increment; issue remains open)"
          local rn=(); [[ "$reopened" != "true" ]] || rn=(--reopened); [[ -z "$text" ]] || rn+=(--held)  # held remainder (#9264)
          # forge_gh_comment_rl_safe (#4856): falls back to the REST comments
          # endpoint on a GraphQL rate-limit rejection.
          _mp_post_partial_comment partial-merged "$issue_num" "Could not post partial-increment comment on issue #$issue_num (label swap still applied)" ${rn[@]+"${rn[@]}"}
        else
          warning "Could not reset labels on issue #$issue_num (partial increment) — may need manual 'gh issue edit'"
        fi
        ;;
    esac
  done 3<<< "$out"
}

# _mp_post_partial_comment <kind> <issue_num> <post-failure warning> [--reopened]
#
# Render one of the post-merge partial-increment audit comments and post it.
# Both bodies are `loom-daemon merge-pr partial-comment` (Rust,
# loom-daemon/src/merge_pr/partial_comment.rs — #8191 slice), byte-frozen from
# the shell that used to build them here; only the POST stays, behind #4856's
# rate-limit-safe wrapper.
#
# The bodies moved for the reason this file exists: they were ~40 lines of pure
# text in a script the size ratchet freezes, and NOTHING asserted either of them
# — the retained suite stubs the comment post, so a mangled interpolation or a
# lost `$reopen_note` would have shipped silently. They now have unit tests and
# a byte-for-byte differential against the frozen retired shell
# (loom-daemon/tests/merge_pr_partial_comment_differential.rs).
#
# Fails OPEN, and deliberately: both comments are posted AFTER the mutation they
# describe (the #4569 reopen, the #3667 label swap), and the merge itself is
# long done — so a body that cannot be rendered costs the note, nothing else.
# But it must never post SILENCE over the audit trail, which is what a bare
# `comment="$(… || true)"` would do against a daemon predating the verb. Only
# output led by the LOOM-MERGE-PR-COMMENT sentinel is posted; anything else is a
# warning naming what is missing.
#
# `$nl` holds the sentinel's terminating newline in a plain variable rather than
# writing `$'\n'` inline in the two patterns below. Both places are PATTERNS —
# a `[[ ]]` right-hand side and a `${var#…}` word — and bash only began
# processing `$'…'` inside those relatively late (it is literal `$'\n'` on
# macOS's stock /bin/bash 3.2, which this repo supports and has been bitten by
# before: #7717/#7721/#4242). Getting it literal would fail in BOTH directions,
# silently and only off-Linux: the `[[ ]]` would never match, so every macOS
# host would skip both comments with a warning, and if only the strip were
# literal the sentinel line would be posted as the body's first line. A plain
# `$nl` expansion is unambiguous on every bash. The ANSI-C quote in the
# *assignment* is fine everywhere — that use has worked since bash 2. Inside the
# `${out#…}` word `"$nl"` is quoted separately (SC2295) so the newline is matched
# literally rather than as a pattern; it holds no glob metacharacters either way,
# but the quoting is what says so.
_mp_post_partial_comment() {
  local kind="$1" issue_num="$2" post_warn="$3"; shift 3
  local out rc=0 nl
  nl=$'\n'
  out="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr partial-comment --kind "$kind" --issue "$issue_num" --pr "$PR_NUMBER" "$@" <<<"${issue_json-}" 2>/dev/null)" || rc=$?
  if [[ $rc -ne 0 || "$out" != "LOOM-MERGE-PR-COMMENT$nl"* ]]; then
    warning "The $kind audit comment for issue #$issue_num (#3667/#4569) was NOT posted — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr partial-comment' exited $rc without the LOOM-MERGE-PR-COMMENT sentinel (a loom-daemon predating #8191's slice has no such verb). Advisory only: the merge, the reopen and the label swap all already happened and are unaffected — only this explanatory note is missing, and an empty comment is never posted in its place. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"
    return 0
  fi
  forge_gh_comment_rl_safe "$REPO_NWO" "$issue_num" "${out#LOOM-MERGE-PR-COMMENT"$nl"}" 2>/dev/null || warning "$post_warn"
}

# Audit trail for a reverted premature auto-close (#4569). Posted right after
# the reopen so the record survives even when the label swap below is skipped
# (e.g. the issue no longer carries loom:building). Best-effort.
_post_premature_close_comment() {
  _mp_post_partial_comment premature-close "$1" "Could not post premature-close comment on issue #$1 (reopen still applied)"
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
#
# The decision — which of those cases this is — is `loom-daemon merge-pr
# closed-building` (Rust, loom-daemon/src/merge_pr/closed_building.rs — #8191
# slice), fed the fresh issue body on stdin. It prints exactly one line:
# `STRIP`, or `SKIP<TAB><reason>` which this pass deliberately discards (the
# retired function's skips were silent and stdout stays byte-identical). Only
# the mutation stays here. The fresh read uses plain `gh api` (uncached; not
# $GH, which may be gh-cached) so a stale cached view cannot mask a fresh
# re-claim — the same freshness discipline _reset_one_partial_issue keeps.
# Best-effort like the rest of this pass: a daemon that cannot decide, or that
# answers with anything but the two known lines, is a warning naming the manual
# removal, never a guessed mutation. Silence is NOT read as `SKIP`.
_strip_one_closed_issue_building_label() {
  local issue_num="$1" issue_json out rc=0

  issue_json="$(gh api "repos/$REPO_NWO/issues/$issue_num" 2>/dev/null || echo '{}')"
  out="$(printf '%s\n' "$issue_json" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr closed-building 2>/dev/null)" || rc=$?
  if [[ $rc -ne 0 || ( "$out" != STRIP && "$out" != STRIP-READY && "$out" != STRIP-BOTH && "$out" != "SKIP"$'\t'* ) ]]; then
    warning "Closed-issue loom:building cleanup for issue #$issue_num did not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr closed-building' exited $rc and printed '${out//$'\n'/ }' rather than STRIP or SKIP (a loom-daemon predating #8191's slice has no such verb). Advisory only — the merge already happened and #$issue_num was left untouched; if it is closed and still loom:building, drop the stale claim by hand: gh issue edit $issue_num --repo $REPO_NWO --remove-label loom:building $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"
    return 0
  fi
  local labels=()
  case "$out" in
    STRIP) labels=(loom:building) ;;
    # #11517 (operator ruling 2026-10-13): a closed issue also loses loom:issue.
    STRIP-READY) labels=(loom:issue) ;;
    STRIP-BOTH) labels=(loom:building loom:issue) ;;
    *) return 0 ;;
  esac

  local label
  for label in "${labels[@]}"; do
    if forge_gh_remove_label_rl_safe "$REPO_NWO" "$issue_num" "$label" 2>/dev/null; then
      success "Issue #$issue_num: removed stale $label label (closed by this merge, #6199/#11517)"
    else
      warning "Could not remove $label from closed issue #$issue_num — may need manual: gh issue edit $issue_num --repo $REPO_NWO --remove-label $label"
    fi
  done
}

# Resolve this PR's closing issue targets and strip loom:building from each
# that is (still) closed. Best-effort; returns 0 unconditionally.
_strip_closed_issue_building_labels() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0

  local close_targets issue_num
  close_targets="$(forge_pr_close_targets "$PR_NUMBER" "$GH" 2>/dev/null || true)"
  [[ -n "$close_targets" ]] || return 0

  # Loop status is 0: each body command (continue / the helper) returns 0.
  while IFS= read -r issue_num; do
    [[ -n "$issue_num" ]] || continue
    _strip_one_closed_issue_building_label "$issue_num"
  done <<< "$close_targets"
}

# ---------------------------------------------------------------------------
# Branch-derived issue close (#12615). A `feature/issue-N` PR can merge with no
# closing keyword (PR #12512 / issue #12509 -- a Doctor body edit dropped it),
# leaving N open and `loom:issue`-eligible so Builders are re-dispatched onto
# finished work. The branch name identifies the issue, so close N here unless
# the PR (a) already closes it (close targets / body / commit keyword), or
# (b) declares it a partial increment (`Part of #N` / `Contributes to #N`;
# slice-branch reuse of feature/issue-N is legitimate, #3667/#3599).
#
# Fail-safe: if the partial-increment analysis cannot run, nothing is closed.
# Runs BEFORE _strip_closed_issue_building_labels so that pass strips
# loom:building / loom:issue from the issue closed here. GitHub-only,
# best-effort, never fails the merge.
_close_branch_derived_issue() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0
  [[ "${PR_BRANCH:-}" =~ ^feature/issue-([0-9]+)$ ]] || return 0
  local issue_num="${BASH_REMATCH[1]}"

  local pr_body refs
  pr_body="$(echo "$PR_JSON" | jq -r '.body // ""')"
  refs="$(_partial_increment_refs "$pr_body")" || return 0
  if grep -qx "$issue_num" <<<"$refs"; then
    return 0
  fi

  local close_targets
  close_targets="$(forge_pr_close_targets "$PR_NUMBER" "$GH" 2>/dev/null || true)"
  if grep -qx "$issue_num" <<<"$close_targets"; then
    return 0
  fi
  # closingIssuesReferences can be empty on API exhaustion; a closing keyword in
  # the body or a commit message means GitHub closes the issue itself.
  if printf '%s\n%s\n' "$pr_body" "$(_pr_commit_messages)" \
      | grep -Eqi "\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b[[:space:]]+#${issue_num}\b"; then
    return 0
  fi

  local state
  state="$(gh api "repos/$REPO_NWO/issues/$issue_num" --jq 'if has("pull_request") then "pr" else .state end' 2>/dev/null || true)"
  [[ "$state" == "open" ]] || return 0

  if gh issue close "$issue_num" --repo "$REPO_NWO" --reason completed \
      --comment "Closing: PR #$PR_NUMBER merged from \`$PR_BRANCH\` without a closing keyword or a \`Part of #$issue_num\` partial-increment reference, so this issue's work is delivered (#12615). If more work remains, reopen it and have the follow-up PR declare \`Part of #$issue_num\`." >/dev/null 2>&1; then
    success "Issue #$issue_num: closed (merged from $PR_BRANCH with no closing keyword, #12615)"
  else
    warning "Could not close issue #$issue_num after merging $PR_BRANCH -- may need manual: gh issue close $issue_num --repo $REPO_NWO"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Close-triggered `loom:blocked` re-check (#9102; item 2 of #8927's deferred
# fix list). `loom-daemon check-stale-blocked` (#8927) finds a stale block at
# the next sweep pre-wave; this finds it the moment the blocker closes: every
# open `loom:blocked` issue/PR citing this merged PR, or an issue it closed, as
# a blocker gets a comment now (never a label edit). Resolving the closed set,
# the decision and the comment all live in `loom-daemon notify-cleared-blockers`
# (reusing the advisory's enumeration and the `dep_recheck` parsers — no second
# parser, .loom/docs/shell-language-policy.md); it always exits 0. Best-effort
# and GitHub-only, like every step in this section.
_notify_cleared_blockers() { [[ "$FORGE_TYPE" == "github" ]] || return 0; "${LOOM_DAEMON_BIN:-loom-daemon}" notify-cleared-blockers --pr "$PR_NUMBER" --repo "$REPO_NWO" --repo-root "${REPO_ROOT:-.}" --quiet || warning "Close-triggered loom:blocked re-check (#9102) did not run; the next sweep's check-stale-blocked pass still covers it."; }

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

# The two decisions here are `loom-daemon merge-pr reconcile-plan` and
# `merge-pr reconcile-child` (Rust, loom-daemon/src/merge_pr/reconcile.rs —
# #8191 slice). The plan owns the parent-branch gate, the children-rollup parse
# and each child's derived issue number; the child verb owns safe/unsafe and the
# deferral comment's byte-frozen text. Everything with an EFFECT stays here: the
# live `gh pr list` discovery (never the daemon registry), the uncached `gh api`
# label read, the reconcile-stack.sh invocation and the #4856 comment post.
#
# Why these: the retired shell wrote the `feature/issue-<N>` predicate out TWICE
# — once as the parent gate, once as the child derivation, 90 lines apart against
# two different variables — and reached the force-push-authorising answer through
# three stacked `|| echo '{}'` / `|| true` layers that each turn a failed lookup
# into the empty string, which `grep -qx` then reports as "no claim". That is the
# one wrong answer in this file that rebases a branch a Builder still has checked
# out. The port keeps the same disposition (force-with-lease is the remaining
# protection) but makes it a decided one, and it is now impossible to match
# `loom:building-paused` by widening a `grep`.
#
# Fail direction: OPEN, like the pre-merge sibling `merge-pr stacked-children`.
# A missing or older daemon prints no sentinel, and the seam then WARNS and skips
# auto-reconciliation for this pass — exactly the disposition the pre-existing
# "reconcile-stack.sh not found" skip already has. This runs after the merge has
# already happened and cannot make a merge wrong, so it must not be able to stop
# one; the cost of a skip is the one manual reconcile-stack.sh invocation every
# message on both routes already prints. That is why it raises no
# `requires-daemon: merge-pr` floor. Silence is never a route: `reconcile` (the
# force-pushing one) is reachable only through a positive sentinel.

# The roll-this-host remediation both skip paths below append. Guarded with
# `declare -F` exactly as the other fail-open seams (#3747 item 2, #7827,
# partial-reset, closed-building) are: `_mp_daemon_roll_hint` is defined ~700
# lines above, OUTSIDE the span test-merge-pr-auto-reconcile.sh extracts and
# sources, so an unguarded call would put `command not found` into the one
# message whose whole job is to tell an operator what to do next.
#
# One line, like `_mp_daemon_roll_hint` itself: this file is over the file-size
# ratchet's threshold, so it may shrink but not grow (.loom/docs/file-size-policy.md).
_mp_reconcile_roll_hint() { declare -F _mp_daemon_roll_hint >/dev/null || return 0; _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}" 2>/dev/null || true)"; }

# Reconcile (or defer) one child PR from the plan. Best-effort; returns 0.
_reconcile_one_stacked_child() {
  local child_pr="$1" child_branch="$2" parent_branch="$3" child_issue="$4"

  # Fresh (uncached) label read — mirrors _reset_one_partial_issue: use plain
  # `gh api` (not $GH, which may be gh-cached) so a stale cached view cannot mask
  # a live re-claim. Skipped entirely when the plan derived no issue number,
  # which is the "child branch is not feature/issue-<N>, so no claim to race"
  # case; the verb reaches the same answer from an empty --child-issue.
  local issue_labels=""; [[ -z "$child_issue" ]] || issue_labels="$(gh api "repos/$REPO_NWO/issues/$child_issue" 2>/dev/null | jq -r '.labels[]?.name' 2>/dev/null || true)"

  local out rc=0
  out="$(printf '%s\n' "$issue_labels" | "${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}" merge-pr reconcile-child --child-pr "$child_pr" --parent-branch "$parent_branch" --child-issue "$child_issue" 2>/dev/null)" || rc=$?
  case "${out%%$'\n'*}" in
    "LOOM-RECONCILE-CHILD defer")
      # Unsafe: defer, do not rebase.
      info "Stacked reconcile: child PR #$child_pr (issue #$child_issue) is still loom:building — deferring auto-rebase to avoid racing a live Builder"
      # Everything after the marker is the comment body verbatim, so the text
      # cannot be reshaped by a line-oriented read on this side.
      local comment="${out#*$'\nLOOM-RECONCILE-COMMENT\n'}"
      # forge_gh_comment_rl_safe (#4856): the REST comments endpoint is shared
      # by issues and PRs, so the same helper covers this `gh pr comment` call
      # site's GraphQL rate-limit fallback.
      forge_gh_comment_rl_safe "$REPO_NWO" "$child_pr" "$comment" 2>/dev/null || \
        warning "Could not post deferred-reconciliation comment on PR #$child_pr"
      ;;
    "LOOM-RECONCILE-CHILD reconcile")
      # Safe: no live claim — run the existing reconcile script unmodified. Do
      # NOT re-implement the rebase/force-with-lease/retarget logic inline.
      info "Stacked reconcile: parent '$parent_branch' merged; reconciling child PR #$child_pr onto the default branch"
      if "$SCRIPT_DIR/reconcile-stack.sh" "$child_pr" "$parent_branch"; then
        success "Stacked reconcile: child PR #$child_pr reconciled onto the default branch"
      else
        warning "Stacked reconcile: reconcile-stack.sh failed for child PR #$child_pr (rebase conflict, rejected force-with-lease push, or retarget failure). The parent merge is unaffected — reconcile manually: ./.loom/scripts/reconcile-stack.sh $child_pr $parent_branch"
      fi
      ;;
    *)
      warning "Stacked reconcile: SKIPPING child PR #$child_pr — 'loom-daemon merge-pr reconcile-child' exited $rc without a LOOM-RECONCILE-CHILD verdict, so whether a Builder still holds issue #${child_issue:-?} is unknown. Rebasing on a guess could force-push over uncommitted work, so nothing was done. The parent merge is unaffected — reconcile by hand once that is answered: ./.loom/scripts/reconcile-stack.sh $child_pr $parent_branch $(_mp_reconcile_roll_hint)" ;;
  esac
  return 0
}

# Discover open child PRs stacked on the just-merged parent branch and reconcile
# (or defer) each. Best-effort; returns 0 unconditionally.
_auto_reconcile_stacked_children() {
  [[ "$FORGE_TYPE" == "github" ]] || return 0

  # Prefer the pre-merge snapshot the guard above already captured (#8010
  # item 2) over a fresh post-merge query: GitHub retargets an open child PR
  # the instant delete_branch_on_merge removes this parent branch, so a query
  # run AFTER the merge can return zero rows even though children existed
  # seconds earlier — the guard's own pre-merge query already paid for this
  # exact answer. Live forge discovery (uncached `gh`, NEVER the daemon
  # registry) is only a fallback for when the guard never ran (e.g. this
  # function invoked standalone, as the unit tests do).
  local children_json="${STACKED_CHILDREN_JSON:-}"

  # AT MOST TWO plan calls, which is what the `for` bounds structurally.
  #
  # Pass `gate`: the parent-branch gate. NOT-STACKED is decided from
  # --parent-branch alone, so feeding it `[]` when no snapshot exists answers
  # the gate without a forge round trip — which is why the live query below
  # still costs nothing on an ordinary non-stacked merge. Only a stacked parent
  # with no snapshot falls through to pass `live` and queries, precisely when
  # the retired shell queried.
  #
  # The gate is also where the fail-OPEN skip is taken, BEFORE the query rather
  # than after it. An unanswered gate is indistinguishable from NOT-STACKED, so
  # on a host whose daemon predates these verbs this path runs once per merge;
  # it must not also spend a `gh pr list` on a rollup nothing will read.
  local plan rc verdict count _pass bin="${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}"
  for _pass in gate live; do
    rc=0; plan="$(printf '%s' "${children_json:-[]}" | "$bin" merge-pr reconcile-plan --parent-branch "$PR_BRANCH" 2>/dev/null)" || rc=$?
    verdict="${plan%%$'\n'*}"
    [[ "$verdict" == "LOOM-RECONCILE-PLAN NOT-STACKED" ]] && return 0
    [[ "$verdict" == "LOOM-RECONCILE-PLAN COUNT "* ]] || { warning "Stacked reconcile: skipping auto-reconciliation for '$PR_BRANCH' — 'loom-daemon merge-pr reconcile-plan' exited $rc without a plan${verdict:+ (it said: $verdict)}. The parent merge is unaffected; reconcile any stacked child by hand: ./.loom/scripts/reconcile-stack.sh <child-pr> $PR_BRANCH $(_mp_reconcile_roll_hint)"; return 0; }
    [[ -z "$children_json" ]] || break   # the snapshot (or pass `live`'s query) already answered
    children_json="$(gh pr list --repo "$REPO_NWO" --base "$PR_BRANCH" --state open \
      --json number,headRefName 2>/dev/null || echo '[]')"; children_json="${children_json:-[]}"
  done
  count="${verdict#LOOM-RECONCILE-PLAN COUNT }"; [[ "$count" -gt 0 ]] || return 0

  info "Stacked reconcile: found $count open child PR(s) based on '$PR_BRANCH'"

  if [[ ! -x "$SCRIPT_DIR/reconcile-stack.sh" ]]; then
    warning "Stacked reconcile: reconcile-stack.sh not found or not executable at $SCRIPT_DIR — skipping auto-reconciliation"
    return 0
  fi

  local line child_pr child_branch child_issue
  while IFS= read -r line; do
    case "$line" in
      "LOOM-RECONCILE-PLAN CHILD "*)
        IFS=$'\t' read -r child_pr child_branch child_issue <<<"${line#LOOM-RECONCILE-PLAN CHILD }"
        [[ -n "$child_pr" ]] || continue
        _reconcile_one_stacked_child "$child_pr" "$child_branch" "$PR_BRANCH" "$child_issue"
        ;;
      "LOOM-RECONCILE-PLAN MALFORMED "*)
        warning "Stacked reconcile: ignoring an unusable child row from the forge — ${line#LOOM-RECONCILE-PLAN MALFORMED }" ;;
    esac
  done <<< "$plan"

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
#
# The terminal classification — which <action>:<reason> these observations
# add up to — is `loom-daemon merge-pr mergeable-recheck` (Rust,
# loom-daemon/src/merge_pr/mergeable_recheck.rs — #8191 slice): the reason
# strings are byte-frozen there and held by a differential test. The I/O loop
# below (backoff, uncached re-reads, fetch, merge-tree) stays here so the
# retained suite's stubs keep driving the real code path unchanged. A daemon
# that cannot answer is a POSITIVE refuse-stale, never a silent pass: an
# unanswered corroboration must not read as "confirmed clean".
_recheck_mergeable_before_refusal() {
  local nwo="$1" pr_number="$2" gh_cmd="$3" base_ref="$4" head_ref="$5" repo_root="$6" retries="${7:-3}" delay="${8:-3}"
  local attempt recheck_json recheck_mergeable resolved_attempt="" _MPR_BIN _MPR_OUT _MPR_RC=0
  local _MPR_FLAGS=(--retries "$retries" --base-ref "$base_ref" --head-ref "$head_ref")

  for attempt in $(seq 1 "$retries"); do
    sleep "$delay"
    recheck_json="$(forge_get_pr_nocache "$nwo" "$pr_number" "$gh_cmd" 2>/dev/null || echo '{}')"
    recheck_mergeable="$(echo "$recheck_json" | jq -r '.mergeable // empty')"
    if [[ "$recheck_mergeable" == "true" ]]; then resolved_attempt="$attempt"; break; fi
  done

  # Still false/unknown after the backoff retries — corroborate with a local
  # git merge-tree check before conceding this is a genuine conflict.
  [[ -n "$resolved_attempt" ]] && _MPR_FLAGS+=(--resolved-attempt "$resolved_attempt")
  if [[ -z "$resolved_attempt" ]]; then
    # #9106: both refs are forge-controlled ($base_ref from `.base.ref`,
    # $head_ref from `.head.ref`) and the fetch below puts them in a git argv.
    # A name git would parse as a switch is refused HERE, before the fetch —
    # refuse-stale is the fail-closed verdict (it denies the merge without
    # claiming a content conflict). An EMPTY ref keeps its pre-existing
    # --refs-missing classification, which this must not swallow. The `--`
    # after `origin` is defence in depth, not the gate.
    if [[ -z "$base_ref" || -z "$head_ref" ]]; then _MPR_FLAGS+=(--refs-missing)
    elif ! check_branch_name "$base_ref" "base ref of PR #$pr_number" || ! check_branch_name "$head_ref" "head ref of PR #$pr_number"; then
      echo "refuse-stale:PR #$pr_number carries a ref name that is not a safe git operand (base='$base_ref' head='$head_ref') — refused before 'git fetch' could parse it as a switch (#9106). Rename the branch on the PR and re-run."; return 0
    elif git -C "$repo_root" fetch -q origin -- "$base_ref" "$head_ref" 2>/dev/null; then
      if git -C "$repo_root" merge-tree --write-tree "origin/$base_ref" "origin/$head_ref" >/dev/null 2>&1; then _MPR_FLAGS+=(--tree clean); else _MPR_FLAGS+=(--tree conflict); fi
    else _MPR_FLAGS+=(--fetch-failed); fi
  fi

  _MPR_BIN="${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-$(command -v loom-daemon 2>/dev/null || printf '%s' "$HOME/.local/bin/loom-daemon")}}"
  _MPR_OUT="$("$_MPR_BIN" merge-pr mergeable-recheck "${_MPR_FLAGS[@]}" 2>/dev/null)" || _MPR_RC=$?
  if [[ "$_MPR_RC" -eq 0 ]] && [[ "$_MPR_OUT" == merge:* || "$_MPR_OUT" == refuse-stale:* || "$_MPR_OUT" == refuse-conflict:* ]]; then echo "$_MPR_OUT"; return 0; fi
  echo "refuse-stale:mergeability corroboration could not be classified — 'merge-pr mergeable-recheck' exited $_MPR_RC without a recognized action (missing or older binary). Refusing rather than treating an unanswered corroboration as clean; build or install loom-daemon (cargo build --release -p loom-daemon, or re-run the Loom installer), then re-run this merge."
  return 0
}

# ---------------------------------------------------------------------------
# `--auto`'s whole implementation: settle this head's checks, then merge HERE
# (#3820, generalised to every `--auto` invocation by #8410).
#
# It polls the head-SHA check-runs (bounded by LOOM_AUTO_MERGE_TIMEOUT) until
# they settle, then returns 0 so the caller merges synchronously. "Settled"
# means: nothing queued/in_progress, and nothing failing except checks that are
# NOT in the base branch's required-context set. An already-settled head costs
# exactly one check-runs read and returns immediately, so the CLEAN case is not
# slowed down by the wait existing.
#
# #8410 — why this is now the ONLY `--auto` path, and the forge's own
# server-side auto-merge queue is never armed:
#
#   Arming that queue (enablePullRequestAutoMerge) hands the merge decision to
#   the forge, which re-reads NOTHING afterwards except the branch ruleset's
#   REQUIRED status checks. Two things this script enforces stop being enforced
#   the moment it is armed:
#     1. Label state. A later `loom:verdict-stale` revocation (loom:pr ->
#        loom:review-requested) or a Judge `loom:reviewing` claim cannot disarm
#        a queued merge.
#     2. The non-required suites. On this repo every required context is a
#        structural gate; the test suites (Rust Unit Tests, Shell Test Suites,
#        Installer Integration Tests, Native Port Suites, Rust OTLP) are NOT
#        required, so the server was free to merge while they were still
#        running.
#   Both fired live on PR #8220 (2026-09-20): armed at 07:08 on a head that was
#   force-pushed at 07:12, `loom:pr` revoked at 07:13, Judge re-claimed it at
#   07:18, merged by the queue at 07:21 with five suites still pending.
#
#   The UNSTABLE case never had this problem — the forge REFUSES to arm when a
#   required check is mid-flight, so the script already fell back to this
#   function (#3486/#3664). The BLOCKED case (no required check has started
#   yet) is the one where arming succeeds, and it is exactly the case where
#   waiting matters most. Rather than keep two paths whose safety differs by
#   which checks happen to be running at the instant of the call, `--auto` now
#   always takes this one (ci-principles rule 4, one mechanism per behaviour).
#   The cost, accepted deliberately: the invoking process must stay alive for
#   up to LOOM_AUTO_MERGE_TIMEOUT, and a slower CI than that budget makes the
#   run exit non-zero instead of queueing — Champion's next pass retries and
#   merges immediately once the checks are done.
#
# Contract:
#   - returns 0  → safe to proceed to the synchronous-merge path (caller flips
#                  AUTO_MERGE=false). Also returned if the PR merged concurrently
#                  while waiting (the synchronous path's own race-detection then
#                  no-ops cleanly).
#   - calls error() (exit 1) → a required status check failed, or the wait timed
#                  out. A normal recoverable failure Champion's cron retries.
# Requires LOOM_AUTO_MERGE_POLL_INTERVAL / LOOM_AUTO_MERGE_TIMEOUT set.
# LOOM_ZERO_CHECKS_SETTLE_POLLS / LOOM_ZERO_CHECKS_SETTLE_INTERVAL (#9091) are
# read and validated by `loom-daemon merge-pr zero-checks-settle`, not here.
_wait_for_checks_then_sync_merge() {
  # The unfetchable/pending arms' deadline-or-wait decision: `loom-daemon merge-pr poll-wait` (#8191 slice).
  # Returns 5 on timeout, 0 to wait; callers sleep OUTSIDE the `|| exit 5` (errexit is off in that context, so a failed sleep would exit 5, not 1). A verb that cannot run keeps the deadline compare here (only the wording degrades). SELF_BIN-first (#8134), like the per-poll check-runs-rollup.
  _mp_poll_wait() { local now o a l m; now="$(date +%s)"; o="$(printf '%s\n' "${2:-}" | "${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}" merge-pr poll-wait --kind "$1" --pr "$PR_NUMBER" --now "$now" --deadline "$deadline" --timeout "$LOOM_AUTO_MERGE_TIMEOUT" --interval "$LOOM_AUTO_MERGE_POLL_INTERVAL" --rc "${3:-0}" 2>/dev/null)" || o=""; [[ "$o" == "LOOM-POLL-WAIT "* ]] || o="LOOM-POLL-WAIT $([[ "$now" -ge "$deadline" ]] && echo TIMEOUT || echo WAIT) warning PR #$PR_NUMBER: 'merge-pr poll-wait' unavailable (missing/older loom-daemon), polling on the deadline alone (check-runs ${1}, rc=${3:-0})"; read -r _ a l m <<<"$o"; if [[ "$a" == TIMEOUT ]]; then warning "$m"; return 5; fi; if [[ "$l" == info ]]; then info "$m"; else warning "$m"; fi; }
  local head_sha base_ref
  # Poll the SHA this run will actually MERGE, not the one the initial
  # (gh-cached) $PR_JSON fetch reported: $MERGE_PRECONDITION_SHA is the live,
  # uncached read taken immediately above, and the merge API call is gated on
  # it. Waiting on a different SHA than the one being merged would settle the
  # wrong tree's checks — the whole point of #8410. Falls back to $PR_JSON's
  # head when the live read failed (the two are equal in the common case).
  head_sha="${MERGE_PRECONDITION_SHA:-}"
  [[ -n "$head_sha" ]] || head_sha="$(echo "$PR_JSON" | jq -r '.head.sha // empty')"
  base_ref="$(echo "$PR_JSON" | jq -r '.base.ref // empty')"

  # Without the head SHA we cannot reason about checks — proceed to the
  # synchronous merge, which will itself reject if a required check blocks it.
  if [[ -z "$head_sha" ]]; then
    info "PR #$PR_NUMBER: head SHA unavailable; proceeding directly to synchronous merge"
    return 0
  fi

  # zero_row_* / _zcs_* are #9091's zero-row settle state; see that branch at
  # the bottom of the loop. Declared here (rather than beside it) so the
  # zero-row poll count and the cached required-context token survive across
  # loop iterations for the lifetime of this call.
  local deadline observed_checks zero_row_polls=0 zero_row_required=unknown _zcs _zcs_action _zcs_sleep _zcs_msg
  deadline=$(( $(date +%s) + LOOM_AUTO_MERGE_TIMEOUT ))
  # #6169: whether we have ever seen a nonzero check-runs total_count for this
  # head SHA. A check-runs rollup with zero rows is ambiguous on its own — it
  # can mean "this repo genuinely has no CI configured" (safe to declare
  # settled) OR "the forge API returned an empty/degraded response for this
  # poll" (e.g. an intermittent TLS failure -- NOT safe to trust). Requiring
  # at least one observed nonzero total_count (or the full bounded wait
  # elapsing) before trusting a zero-row read as genuine settlement closes
  # the false-settle trap without changing behavior for the common case.
  observed_checks=false

  # Consecutive-iteration counter (#6389) for the persistent-404 detection
  # below. Declared outside the loop so it survives across iterations for
  # the lifetime of this function call; reset to 0 whenever an iteration's
  # fetch result is anything other than a confirmed 404 (success, or a
  # non-404 failure).
  local not_found_streak=0

  while true; do
    # A concurrent merger may have completed the PR while we waited.
    local recheck_json
    recheck_json="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
    if [[ "$(echo "$recheck_json" | jq -r '.merged // false')" == "true" ]]; then
      warning "PR #$PR_NUMBER merged by another process while waiting for checks"
      return 0
    fi

    # Fetch the check-runs rollup for the head SHA. Retry once to absorb a
    # blip. A persistent fetch failure is then classified in two ways
    # (#6389): if BOTH attempts this iteration came back as a confirmed HTTP
    # 404 ($FORGE_CHECK_RUNS_RC_NOT_FOUND — see forge_get_check_runs), and
    # that keeps happening for LOOM_CHECK_RUNS_404_STREAK consecutive
    # iterations (spaced a full poll interval apart), the check-runs API is
    # treated as persistently unavailable for this repo (e.g. GitHub Actions
    # disabled) and we short-circuit straight to the synchronous merge
    # instead of polling to LOOM_AUTO_MERGE_TIMEOUT. Any other failure shape
    # (a single 404, a 5xx, a network blip) resets the streak and keeps
    # today's treat-as-still-pending bounded-poll behavior (#3678 discipline).
    local attempt1_rc=0 attempt2_rc=0 fetch_rc runs_raw
    runs_raw="$(forge_get_check_runs "$REPO_NWO" "$head_sha" 2>/dev/null)" || attempt1_rc=$?
    if [[ "$attempt1_rc" -ne 0 ]]; then
      runs_raw="$(forge_get_check_runs "$REPO_NWO" "$head_sha" 2>/dev/null)" || attempt2_rc=$?
    fi
    fetch_rc="$attempt1_rc"
    [[ "$attempt1_rc" -ne 0 ]] && fetch_rc="$attempt2_rc"

    if [[ "$fetch_rc" -ne 0 ]]; then
      # The confirmed-404-streak classification, ported to Rust (#6389, #8191
      # slice): `loom-daemon merge-pr check-runs-streak` is a pure function of
      # both attempts' return codes and the running streak — see
      # loom-daemon/src/merge_pr/check_runs_streak.rs. Always exits 0 with one
      # `LOOM-CHECK-RUNS-STREAK <PROCEED|PENDING> <streak>` line; anything
      # else (missing/older binary) degrades to PENDING with the streak reset
      # to 0 — the pre-#6389 behaviour, so a guard fault can only cost time
      # via the ordinary LOOM_AUTO_MERGE_TIMEOUT ceiling below, never
      # misclassify a transient blip as the persistent condition that skips
      # waiting altogether.
      local _crs_out _crs_sentinel _crs_verdict _crs_streak
      _crs_out="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr check-runs-streak --attempt1-rc "$attempt1_rc" --attempt2-rc "$attempt2_rc" --streak "$not_found_streak" --threshold "$LOOM_CHECK_RUNS_404_STREAK" --not-found-rc "$FORGE_CHECK_RUNS_RC_NOT_FOUND" 2>/dev/null)" || _crs_out=""
      read -r _crs_sentinel _crs_verdict _crs_streak <<< "$_crs_out"
      if [[ "$_crs_sentinel" != "LOOM-CHECK-RUNS-STREAK" ]]; then
        warning "The persistent-404 streak classification (#6389, #8191 slice) did not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr check-runs-streak' printed no recognized decision (a loom-daemon predating this slice has no such verb). Treating this iteration as still-pending with the streak reset — a guard fault here can only cost time, never skip the wait. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"
        _crs_verdict="PENDING"; _crs_streak=0
      fi
      not_found_streak="${_crs_streak:-0}"
      if [[ "$_crs_verdict" == "PROCEED" ]]; then
        info "PR #$PR_NUMBER: check-runs API unavailable for this repo (no checks configured); proceeding to synchronous merge"
        return 0
      fi
      # A SHORT read (#8895) is not a fetch failure: the rollup arrived, it was
      # just a subset of the commit's check-runs (the forge's own total_count
      # said so). forge_get_check_runs withholds it rather than let a subset
      # look like settlement, and this loop's existing nonzero handling —
      # re-poll, then hard-fail at the deadline — is exactly the fail-closed
      # outcome wanted. Name it explicitly so the narration is not the
      # misleading "could not fetch" (the helper's own stderr detail is
      # suppressed at the callsite above). Guarded one-liner rather than an
      # `if` block: `set -e` exempts AND-lists (see the note above
      # _wait_for_checks_then_sync_merge's reads), and this is the same idiom
      # the two `fetch_rc`/`observed_checks` assignments in this loop use.
      [[ "$fetch_rc" -eq "${FORGE_CHECK_RUNS_RC_TRUNCATED:-45}" ]] && warning "PR #$PR_NUMBER: check-runs read was TRUNCATED (fewer rows than the forge's own total_count); refusing to classify a partial set, continuing to poll"
      # Wait-or-timeout (exit 5, #8896) is `_mp_poll_wait` (#8191 slice).
      _mp_poll_wait unfetchable "" "$fetch_rc" || exit 5
      sleep "$LOOM_AUTO_MERGE_POLL_INTERVAL"
      continue
    fi
    not_found_streak=0

    # Failing (terminal non-success) and pending (not yet completed) check
    # names, and total_count: `loom-daemon merge-pr check-runs-rollup` (Rust,
    # loom-daemon/src/merge_pr/check_runs_rollup.rs — #8191 slice), four
    # NUL-terminated fields, sentinel LAST. It replaced three `jq … || true`
    # filters that read an UNREADABLE payload as "nothing failing, nothing
    # pending" — the shape this loop settles on. Fail direction: no sentinel
    # (older/missing binary, or a payload outside forge_get_check_runs'
    # contract) is read as STILL PENDING, so a fault costs time up to the
    # deadline (exit 5, re-queue), never an unsettled merge. SELF_BIN first
    # (#8134), like _mp_refs/_mp_worktree/mergeable-recheck.
    local failing="" pending="" total_count=0 _crr="" _crr_bin="${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}"
    { IFS= read -r -d '' failing; IFS= read -r -d '' pending; IFS= read -r -d '' total_count; IFS= read -r -d '' _crr; } < <(printf '%s' "$runs_raw" | "$_crr_bin" merge-pr check-runs-rollup 2>/dev/null) || true
    [[ "$_crr" == "LOOM-CHECK-RUNS-ROLLUP" ]] || { warning "PR #$PR_NUMBER: 'merge-pr check-runs-rollup' did not classify this poll's check-runs (missing/older loom-daemon, or a payload outside the forge contract); treating it as still pending. $(! declare -F _mp_daemon_roll_hint >/dev/null || _mp_daemon_roll_hint merge-pr "$(command -v "$_crr_bin" 2>/dev/null || true)")"; failing=""; pending="(unclassified check-runs rollup)"; total_count=0; }
    [[ "$total_count" -gt 0 ]] && observed_checks=true

    if [[ -n "$failing" ]]; then
      # A check failed — classify against branch protection. A required failing
      # check can never merge on this SHA; refuse now. A lookup failure fails
      # closed (refuse), mirroring the UNSTABLE fallback.
      # Stderr is NOT redirected here: #8872's plan-gate relaxation warns
      # there, and a silent fail-open is exactly what that warning exists to
      # prevent.
      local required lookup_rc=0
      required="$(forge_get_required_status_check_contexts "$REPO_NWO" "$base_ref" "$GH")" || lookup_rc=$?
      if [[ "$lookup_rc" -ne 0 ]]; then
        error "Failed to resolve required status checks for $base_ref (rc=$lookup_rc); refusing to merge PR #$PR_NUMBER with failing check(s) that cannot be classified as required or informational (fails closed)"
      fi
      # The overlap/proceed-or-continue decision itself is `loom-daemon
      # merge-pr checks-failure` (Rust, loom-daemon/src/merge_pr/
      # checks_failure.rs — #8191 slice): given the failing/required/pending
      # check-name sets already fetched above (the forge reads stay here —
      # $required's lookup covers Gitea too), whether a required check is among the failing ones (refuse), only
      # informational ones are and nothing is pending (proceed), or
      # informational failures coexist with a still-pending check (fall
      # through to the pending wait below, unchanged). A guard fault (missing
      # binary, older install, malformed output) refuses the merge, same as
      # every other guard in this file: a caller cannot tell "only
      # informational checks failing" from "never classified".
      local _cf_out _cf_rc=0
      _cf_out="$(printf '%s\0%s\0%s\0' "$failing" "$required" "$pending" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr checks-failure --pr "$PR_NUMBER" 2>/dev/null)" || _cf_rc=$?
      # Only informational (non-required) checks failing and nothing pending →
      # a synchronous merge is safe (matches the UNSTABLE #3486 fallback).
      # Informational failures with other checks still running (PENDING) fall
      # through to the pending wait below, exactly as before.
      case "$_cf_rc:$_cf_out" in
        1:LOOM-CHECK-FAILURE-REQUIRED$'\t'*) error "Cannot merge PR #$PR_NUMBER: a required status check has failed (${_cf_out#*$'\t'}). Fix the check and re-run the merge." ;;
        0:LOOM-CHECK-FAILURE-PROCEED) info "PR #$PR_NUMBER: only informational (non-required) check(s) failing ($(printf '%s' "$failing" | sort -u | tr '\n' ' ')— not in the ruleset's required contexts, so they do not block); proceeding to synchronous merge"; return 0 ;;
        0:LOOM-CHECK-FAILURE-PENDING) ;;
        *) error "Merge blocked: PR #$PR_NUMBER's failing-check classification (#8191 slice) could not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr checks-failure' exited $_cf_rc without a recognized LOOM-CHECK-FAILURE-* sentinel. A guard that cannot run refuses the merge rather than passing it. $(_mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")" ;;
      esac
    fi

    if [[ -n "$pending" ]]; then
      _mp_poll_wait pending "$pending" || exit 5
      sleep "$LOOM_AUTO_MERGE_POLL_INTERVAL"
      continue
    fi

    # Nothing failing, nothing pending -- but a zero-row rollup we have never
    # seen non-empty is ambiguous (#6169: could be a transient forge read, not
    # genuine settlement), so it is never trusted on a single read.
    #
    # The decision — settle now, keep waiting (and for how long), or report the
    # whole wait spent — is `loom-daemon merge-pr zero-checks-settle` (Rust,
    # loom-daemon/src/merge_pr/zero_checks.rs, #9091). It holds #6169's rule,
    # #9091's narrowing of it (bounded only when the base branch requires NO
    # status-check contexts, so nothing that can gate this merge may still be
    # registering), the LOOM_ZERO_CHECKS_SETTLE_* knobs and their floors, and
    # the two-source required-context lookup it shares with `required-checks`.
    # $zero_row_required is that lookup's answer, echoed back on field 3
    # of every decision line and replayed on the next poll, which is what makes
    # it happen ONCE per wait rather than once per poll.
    #
    # No requires-daemon floor -- same choice `loom-pr-guard` makes above. Output that does not begin with a LOOM-ZERO-CHECKS-* sentinel
    # (missing binary, older binary, clap usage error, silence) falls back to
    # #6169's full deadline-bounded wait: the status quo ante this narrows, so
    # a fault can only cost time, never skip a gate. It can NOT degrade into
    # settling on one empty read, which is #6169 itself.
    if [[ "$total_count" -eq 0 ]] && [[ "$observed_checks" != "true" ]]; then
      zero_row_polls=$(( zero_row_polls + 1 ))
      _zcs="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr zero-checks-settle --pr "$PR_NUMBER" --repo "$REPO_NWO" --base-ref "$base_ref" --polls "$zero_row_polls" --required-state "$zero_row_required" --poll-interval "$LOOM_AUTO_MERGE_POLL_INTERVAL" --timeout "$LOOM_AUTO_MERGE_TIMEOUT" --now "$(date +%s)" --deadline "$deadline" 2>/dev/null | head -1)" || true
      [[ "$_zcs" == LOOM-ZERO-CHECKS-* ]] || _zcs="LOOM-ZERO-CHECKS-$([[ "$(date +%s)" -ge "$deadline" ]] && echo TIMEOUT || echo WAIT) $LOOM_AUTO_MERGE_POLL_INTERVAL lookup-failed PR #$PR_NUMBER: check-runs rollup is empty (zero rows) and 'loom-daemon merge-pr zero-checks-settle' returned no verdict (missing or older binary), so #9091's bounded settle is unavailable; falling back to #6169's full ${LOOM_AUTO_MERGE_TIMEOUT}s wait before trusting it"
      read -r _zcs_action _zcs_sleep zero_row_required _zcs_msg <<<"$_zcs"
      if [[ "$_zcs_action" == "LOOM-ZERO-CHECKS-WAIT" ]]; then info "$_zcs_msg"; sleep "$_zcs_sleep"; continue; fi
      [[ "$_zcs_action" == "LOOM-ZERO-CHECKS-TIMEOUT" ]] && warning "$_zcs_msg" || info "$_zcs_msg"
    fi

    # Nothing failing (or only informational), nothing pending → effectively
    # CLEAN. Proceed to the synchronous merge.
    info "PR #$PR_NUMBER: checks settled; proceeding to synchronous merge"
    return 0
  done
}

# Handle auto-merge mode
#
# `--auto` no longer means "hand the merge to the forge's queue" (#8410) — it
# means "wait for this head's checks to settle, re-validate, then merge here".
# The two steps live below the head-SHA precondition read, in
# `_revalidate_merge_guards` and the `if [[ "$AUTO_MERGE" == "true" ]]` block.
# This line is also the landmark several merge-pr test suites anchor their
# source-span extractions on; keep it.

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
# #11859: under --expected-head the fresh read must equal the caller's SHA, and the caller's SHA is what the forge merge call is gated on.
[[ -n "$_MPS_FRESH_SHA" ]] && MERGE_PRECONDITION_SHA="$_MPS_FRESH_SHA"; _check_expected_head "$MERGE_PRECONDITION_SHA"; MERGE_PRECONDITION_SHA="${EXPECTED_HEAD_SHA:-$MERGE_PRECONDITION_SHA}"
unset _MPS_JSON _MPS_FRESH_SHA; [[ "$AUTO_MERGE" == "true" ]] || _check_required_checks

# Re-pin refs/loom/parent/<branch> to the SHA actually being merged (#8010
# item 3), when the merge-ordering guard above pinned one. That guard pinned
# $PR_HEAD_SHA — read once, at the top of the script, from the initial
# (possibly gh-cached) $PR_JSON — but the merge itself uses the
# $MERGE_PRECONDITION_SHA just refreshed above, live, ~1100 lines and one
# CI-wait later. If the parent branch was pushed in between, the two SHAs
# differ and the pin would otherwise still point at a tip that is NOT what
# gets merged. Re-pointing it here keeps reconcile-stack.sh's fallback
# rebasing from the tip that was ACTUALLY merged. Best-effort and silent: a
# failure here leaves the existing (already-correct-when-written) pin in
# place, and reconcile-stack.sh's #8010-item-4 ancestry check refuses any pin
# that is not an ancestor of the child rather than silently replaying the
# wrong commit range. Gated on `$DRY_RUN != true` — this runs unconditionally
# ahead of both merge paths' own dry-run checks below, and the guard's pin
# path honors the "dry-run never mutates local refs" contract, so this must
# too. Gated on STACKED_CHILDREN_PIN_WRITTEN, NOT STACKED_CHILDREN_JSON: the
# latter is set as soon as the guard FINDS an open child, even on the
# --allow-stacked-children bypass path that returns without ever writing a
# pin — gating on it would silently create a pin the guard itself declined
# to create.
[[ "$DRY_RUN" != "true" && "${STACKED_CHILDREN_PIN_WRITTEN:-}" == "true" && "$MERGE_PRECONDITION_SHA" != "$PR_HEAD_SHA" ]] && { git -C "$REPO_ROOT" update-ref "refs/loom/parent/$PR_BRANCH" "$MERGE_PRECONDITION_SHA" 2>/dev/null || true; }

# `--auto` (#8410): settle the checks, re-validate, then merge in this process.
#
# `_wait_for_checks_then_sync_merge` above either returns 0 (this head's checks
# are settled — nothing queued/in_progress, nothing REQUIRED failing) or
# error()s out terminally. Whatever it waited for took real time, so the guards
# that ran at the top of this script — loom:pr present, no contradicting
# verdict label, and the head they were evaluated against — are re-checked
# against freshly-read state immediately before the merge fires. That re-check
# is precisely what an armed server-side merge structurally cannot do, and it
# is the half of this fix that closes the label-revocation gap (the wait itself
# closes the pending-test-suite gap).
_revalidate_merge_guards() {
  local fresh v; fresh="$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')"
  # The merged / no-head / head-moved decision, and the label set to re-check, are `loom-daemon merge-pr revalidate-head` (Rust, loom-daemon/src/merge_pr/revalidate_head.rs — #8191 slice): MERGED = merged underneath us while we waited (nothing left to guard); MOVED = the head moved during the wait (PR #8220's 07:12 force-push), the #5579 "re-queue, not a failure" signal (exit 3); NO-HEAD = the re-read was unusable (#8896: a forge read failure, NOT a missing loom:pr). No verdict line at all (a daemon predating the verb: rc 2/127) falls back to the retired jq predicate below, emitting the same protocol — the pre-verb behaviour, so an older host still merges (floor-test row `revalidate-head open -`, no floor raise). jq still READS the payload, so "head unchanged" and "never looked" still cannot read alike.
  v="$(printf '%s' "$fresh" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr revalidate-head --precondition-sha "${MERGE_PRECONDITION_SHA:-}" 2>/dev/null)" || v=""
  # Legacy fallback (two joined lines): the retired #8410/#8896 jq filters, byte-for-byte, mapped onto the verb's protocol.
  [[ "$v" == "LOOM-REVALIDATE "* ]] || v="$(_fs="$(jq -r '.head.sha // empty' <<<"$fresh" 2>/dev/null || true)"; if [[ "$(jq -r '.merged // false' <<<"$fresh" 2>/dev/null || true)" == "true" ]]; then echo "LOOM-REVALIDATE MERGED"; elif [[ -z "$_fs" ]]; then echo "LOOM-REVALIDATE NO-HEAD"
    elif [[ -n "${MERGE_PRECONDITION_SHA:-}" && "$_fs" != "$MERGE_PRECONDITION_SHA" ]]; then echo "LOOM-REVALIDATE MOVED $_fs"; else echo "LOOM-REVALIDATE CLEAR"; jq -r '.labels[]?.name // empty' <<<"$fresh" 2>/dev/null || true; fi)"
  # CLEAR: re-point the guards' input at the state just read, then re-run them. A loom:verdict-stale revocation (#5686), a Judge re-claim, or a contradicting verdict label (#8112) now blocks the merge exactly as it would have at queue time. --allow-unapproved still overrides the loom:pr block, as on the queue-time evaluation.
  case "${v%%$'\n'*}" in "LOOM-REVALIDATE MERGED") return 0 ;; "LOOM-REVALIDATE CLEAR") ;;
    "LOOM-REVALIDATE MOVED "*) error_head_moved "PR #$PR_NUMBER: head moved while --auto waited for this head's checks to settle (#8410)" "$MERGE_PRECONDITION_SHA" "${v#LOOM-REVALIDATE MOVED }" ;;
    *) error "Merge blocked: could not re-read PR #$PR_NUMBER after --auto's settle-wait — the uncached re-read returned no usable payload (no head SHA), so neither the head nor the label set could be re-validated against current state. This is a forge read failure, NOT a missing \`loom:pr\` label: refusing to merge rather than treating an unreadable response as a verdict. Re-run once the forge API is healthy." ;; esac
  PR_LABELS=""; [[ "$v" != *$'\n'* ]] || PR_LABELS="${v#*$'\n'}"
  _check_loom_pr_label
  _check_verdict_label_contradiction
}

if [[ "$AUTO_MERGE" == "true" ]]; then
  # Bounded poll window (#3664). The env-var names/semantics date from the
  # retired shell Gitea auto-merge poller (removed by #8427) and are kept for
  # config compatibility. Defaults: 30s interval, 600s ceiling —
  # raise LOOM_AUTO_MERGE_TIMEOUT on a repo whose CI runs longer than that.
  LOOM_AUTO_MERGE_POLL_INTERVAL="${LOOM_AUTO_MERGE_POLL_INTERVAL:-30}"
  LOOM_AUTO_MERGE_TIMEOUT="${LOOM_AUTO_MERGE_TIMEOUT:-600}"
  # Consecutive-iteration threshold (#6389) for treating the check-runs
  # endpoint's HTTP 404 as a persistent "no checks configured for this repo"
  # condition rather than a transient blip — see
  # `_wait_for_checks_then_sync_merge()`.
  LOOM_CHECK_RUNS_404_STREAK="${LOOM_CHECK_RUNS_404_STREAK:-2}"

  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] Would wait up to ${LOOM_AUTO_MERGE_TIMEOUT}s for PR #$PR_NUMBER's checks to settle, then merge it in-process (immediately if already settled)"
    exit 0
  fi

  info "PR #$PR_NUMBER: --auto waits for this head's checks to settle, then merges in-process — the forge's server-side auto-merge queue is never armed (#8410)"
  _wait_for_checks_then_sync_merge
  _revalidate_merge_guards
  # The synchronous-merge path below is now the ONLY way a --auto run merges,
  # so the post-merge cleanup block — #3667's partial-increment reset, #6199's
  # loom:building strip, and #8048's stacked-children reconcile pass — is
  # always reached. There is no queued early-exit left to bypass it.
  AUTO_MERGE=false
fi

# Synchronous-merge path — the only merge path (#8410). `--auto` reaches it
# with AUTO_MERGE flipped to false above, after its bounded check-settle wait
# and guard re-validation; a plain invocation reaches it directly. The `if`
# is kept (rather than unwrapped) so the block's structure, and every anchor
# the merge-pr test suites match against it, stay stable.
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
  # Parsed by `loom-daemon merge-pr retries-used` (#8191 slice); telemetry only, so any fault keeps the configured budget.
  _MSM_RETRIES_USED="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr retries-used --reason "$_MSM_REASON" --configured "$_MSM_RETRIES" 2>/dev/null)" || _MSM_RETRIES_USED="$_MSM_RETRIES"

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
    # #9444: a CORROBORATED conflict — the forge said `mergeable=false` and the
    # local `git merge-tree` check agreed — is the `merge_conflict` rework
    # event, and the one acceptance criterion this writer exists to satisfy.
    # The `*` arm below is deliberately NOT marked: it refuses because the
    # cached state could not be corroborated, which is "nobody could tell",
    # not "this branch conflicts", and marking it would inflate the
    # environmental bucket with unanswered checks. Emitted on this line
    # because `error` exits, and inline because the file is ratcheted.
    refuse-conflict) "${LOOM_DAEMON_BIN:-loom-daemon}" record-rework --kind merge_conflict --branch "$PR_BRANCH" --repo-root "$REPO_ROOT" --reason "$_MSM_REASON" >/dev/null 2>&1 || true
      error "PR #$PR_NUMBER has merge conflicts — resolve before merging ($_MSM_REASON)"
      ;;
    *)
      error "PR #$PR_NUMBER has merge conflicts — resolve before merging (forge's cached mergeable state is stale/unknown and could not be corroborated locally: $_MSM_REASON)"
      ;;
  esac
  unset _MSM_BASE_REF _MSM_RETRIES _MSM_DELAY _MSM_DECISION _MSM_ACTION _MSM_REASON _MSM_RETRIES_USED 2>/dev/null || true
fi

if [[ "$DRY_RUN" == "true" ]]; then
  info "[dry-run] Would merge PR #$PR_NUMBER ($REPO_MERGE_METHOD) and delete remote branch '$PR_BRANCH'"
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

# Merge via API (using the repo's detected/allowed merge method, #7754) with
# retry for stale branch
MAX_MERGE_RETRIES=3; MERGE_RETRY_DELAY=5

for MERGE_ATTEMPT in $(seq 1 $MAX_MERGE_RETRIES); do
  MERGE_RESPONSE=$(forge_merge_pr "$REPO_NWO" "$PR_NUMBER" "$MERGE_PRECONDITION_SHA" "$REPO_MERGE_METHOD" 2>&1) && break  # Success, exit loop

  # Check if it merged despite error (race condition)
  RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
  RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
  if [[ "$RECHECK" == "true" ]]; then
    warning "Merge reported error but PR is merged (race condition)"
    break
  fi

  # Classify the response ONCE, before any of the three routes below (#8191
  # slice). Placed after the merged-despite-error recheck above deliberately: that
  # one is a forge round-trip rather than a string test, and a PR that merged
  # underneath us has no route to choose. A helper failure here is reported as a
  # HELPER failure — never folded into the terminal "other" route, which would
  # make "could not classify" indistinguishable from "no marker matched".
  MERGE_RESPONSE_KIND="$(_classify_merge_response "$MERGE_RESPONSE")" || error "Merge blocked: PR #$PR_NUMBER's merge-response classifier could not run — '${LOOM_DAEMON_BIN:-loom-daemon} merge-pr classify-response' returned no LOOM-MERGE-RESPONSE verdict (missing binary, or one predating the subcommand). The routes it chooses between are not interchangeable: one retries after syncing the base, and one must NEVER retry a head that moved past the approved SHA (#5579). An unobtainable classification therefore refuses rather than guesses. This is a helper failure, NOT a merge verdict — nothing about this PR was rejected. The forge reported: $MERGE_RESPONSE. $(_mp_daemon_roll_hint merge-pr "$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" 2>/dev/null || true)")"

  # Head-SHA-mismatch (#5579): the PR's OWN head branch moved past
  # $MERGE_PRECONDITION_SHA — distinct from "Base branch was modified" below
  # (that means the BASE fell behind; this means the branch we're trying to
  # merge changed, most commonly a session pushing new commits mid-merge). Do
  # NOT retry-and-merge: retrying would either fail again (session still
  # pushing) or silently squash a different diff than the one Judge approved.
  # Exit 3 so the caller (Champion) re-queues instead of treating this as a
  # failure. See error_head_moved()/_classify_merge_response() above.
  # Since #8164, via _head_moved_or_resync(): a mismatch caused by this run's
  # own base-sync earns exactly one re-read-and-retry; anything else is the
  # same exit-3 re-queue as before.
  # Head-vs-base precedence lives in the classifier's own ordered match (#8191),
  # not in statement order; and `merge-route` below refuses a head-mismatch
  # rather than syncing it, so even a reorder here could never retry a moved head.
  if [[ "$MERGE_RESPONSE_KIND" == "head-mismatch" ]]; then
    _head_moved_or_resync "$MERGE_RESPONSE" && continue
  fi

  # Every other route — 405 merge-in-progress, a stale base within the retry
  # budget, and the terminal refusals — is `loom-daemon merge-pr merge-route`
  # (#8191 slice, loom-daemon/src/merge_pr/merge_route.rs): the attempt budget,
  # the doubling backoff and every line narrated on the way. This loop keeps
  # only the I/O between those lines. Fail-OPEN onto the retired behaviour: a
  # binary that cannot answer is routed here on the classified kind and the
  # same `attempt < max` budget, so a moved head is still never retried and
  # everything else still refuses; only the wording degrades (_mr_say).
  _MR="$(printf '%s' "$MERGE_RESPONSE" | "${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr merge-route --kind "$MERGE_RESPONSE_KIND" --attempt "$MERGE_ATTEMPT" --max "$MAX_MERGE_RETRIES" --delay "$MERGE_RETRY_DELAY" --pr "$PR_NUMBER" 2>/dev/null)" || _MR=""
  [[ "$_MR" == "LOOM-MERGE-ROUTE "* ]] || { warning "PR #$PR_NUMBER: 'merge-pr merge-route' gave no route (missing/older loom-daemon); routing on the classified kind '$MERGE_RESPONSE_KIND' alone. Roll this host: $SCRIPT_DIR/cli/loom-daemon-update.sh --fetch"; _MR="FAIL"; [[ "$MERGE_RESPONSE_KIND" != merge-in-progress ]] || _MR="AWAIT 5"; [[ "$MERGE_RESPONSE_KIND" != base-modified || $MERGE_ATTEMPT -ge $MAX_MERGE_RETRIES ]] || _MR="SYNC $MERGE_RETRY_DELAY $((MERGE_RETRY_DELAY * 2))"; [[ "$_MR" != FAIL ]] || _MR+=$'\n'"Failed to merge PR #$PR_NUMBER: $MERGE_RESPONSE"; _MR="LOOM-MERGE-ROUTE $_MR"; }
  read -r _ _MR_ROUTE _MR_SLEEP _MR_NEXT_DELAY <<<"${_MR%%$'\n'*}"
  case "$_MR_ROUTE" in
    AWAIT)
      info "$(_mr_say BEFORE)"; sleep "$_MR_SLEEP"
      RECHECK_JSON=$(forge_get_pr_nocache "$REPO_NWO" "$PR_NUMBER" "$GH" 2>/dev/null || echo '{}')
      RECHECK=$(echo "$RECHECK_JSON" | jq -r '.merged // false')
      if [[ "$RECHECK" == "true" ]]; then success "$(_mr_say MERGED)"; break; fi
      warning "$(_mr_say PENDING)"; continue ;;
    SYNC)
      info "$(_mr_say BEFORE)"
      # Update failure is not fatal: the update may have partially succeeded.
      UPDATE_RESPONSE=$(forge_update_branch "$REPO_NWO" "$PR_NUMBER" 2>&1) || warning "Failed to update branch: $UPDATE_RESPONSE"
      info "$(_mr_say WAIT)"; sleep "$_MR_SLEEP"
      # The sync just pushed to the head branch: re-read it, or the retry
      # re-gates on a SHA the forge has already superseded (#8164) — and mark
      # the rework (#9444; the canonical "main moved under the work" event,
      # `--duration-sec` the settle wait just slept; `|| true`: a marker may
      # never fail a merge).
      _refresh_precondition_sha; "${LOOM_DAEMON_BIN:-loom-daemon}" record-rework --kind rebase --branch "$PR_BRANCH" --repo-root "$REPO_ROOT" --reason "$(_mr_say REWORK)" --duration-sec "$_MR_SLEEP" >/dev/null 2>&1 || true
      MERGE_RETRY_DELAY="$_MR_NEXT_DELAY"; continue ;;
  esac
  # FAIL (or a route word this script does not know): refuse, quoting the reason.
  error "${_MR#*$'\n'}"
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

# Partial-increment label reset (#3667). Runs only after a confirmed merge —
# every merge now lands on the synchronous path above (#8410), so this is
# reached for `--auto` too; only the dry-run path exits earlier.
# Best-effort — never fails the merge. See the function definitions above.
_reset_partial_increment_labels || true

# Branch-derived issue close (#12615): close feature/issue-N when the PR carried
# no closing keyword and no partial-increment declaration. Must precede the
# strip pass below so it strips the label from the issue closed here.
# Best-effort -- never fails the merge.
_close_branch_derived_issue || true

# Closed-issue `loom:building` cleanup (#6199). Runs right after the partial-
# increment pass (so a #4569 revert of a premature auto-close has already
# happened, and that issue is correctly skipped here — see the function's own
# header comment above) and at the same confirmed-merge choke point.
# Best-effort — never fails the merge.
_strip_closed_issue_building_labels || true

# Close-triggered loom:blocked re-check (#9102). Runs right after the
# loom:building cleanup above, at the same confirmed-merge choke point.
# Best-effort — never fails the merge. See the function's own header above.
_notify_cleared_blockers || true

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
  # #9372: a bare ref delete makes GitHub CLOSE (unrecoverably) every open PR
  # based on this branch. Retarget open stacked children onto this PR's base
  # first; keep the branch on ANY uncertainty. Only exit 0 authorizes the delete
  # (a binary predating the verb exits 2 => keep). Independent of the #9259
  # reconcile defer and of --allow-stacked-children. GitHub-only: the verb
  # drives `gh pr list/edit`, so on FORGE_TYPE=gitea (the same test
  # forge_delete_branch dispatches on) it is skipped and the delete below runs
  # exactly as before #9372; otherwise a Gitea merge would consult a same-named
  # GitHub repo, or keep every branch when none exists.
  _RC_RC=0; _RC_OUT=""; [[ "${FORGE_TYPE:-}" == "gitea" ]] || _RC_OUT="$("${LOOM_DAEMON_BIN:-loom-daemon}" merge-pr retarget-children --repo "$REPO_NWO" --parent-branch "$PR_BRANCH" --base "$(echo "$PR_JSON" | jq -r '.base.ref // empty' 2>/dev/null)" 2>/dev/null)" || _RC_RC=$?
  while IFS=$'\t' read -r _RC_LVL _RC_MSG; do
    case "$_RC_LVL" in INFO) info "$_RC_MSG" ;; WARNING) warning "$_RC_MSG" ;; esac
  done <<< "$_RC_OUT"
  if [[ $_RC_RC -ne 0 ]]; then
    warning "Skipping remote branch deletion of '$PR_BRANCH' (stacked-child safety, #9372; retarget-children exit $_RC_RC)"
  else
    info "Deleting remote branch: $PR_BRANCH"
    forge_delete_branch "$REPO_NWO" "$PR_BRANCH" && \
      success "Remote branch '$PR_BRANCH' deleted" || \
      warning "Could not delete remote branch '$PR_BRANCH' (may already be deleted)"
  fi
  unset _RC_OUT _RC_RC _RC_LVL _RC_MSG
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
# Post-merge worktree/branch cleanup, ported to Rust (#8191 orchestrator slice
# 1; docs/migration/merge-pr-orchestrator-port.md): `loom-daemon merge-pr
# post-merge-cleanup` names the targets (cleanup-paths), makes the
# remove-vs-preserve decision (worktree-preserve, discovered-worktree), runs
# the primary/sentinel/uncommitted-work guards (remove-gate, dirty-guard), does
# the removal (worktree-teardown) and its ledger entry, deletes the local branch
# (delete-branch) and reclaims a redirected cargo target dir — in that order, in
# one process, instead of this script sequencing nine verbs through `$(...)`
# captures. What stays HERE is what is shared with Gitea or other callers: the
# forge reads and `branch_has_landed`. The verb is LAZY about them — it prints
# `LOOM-NEED close-targets|issue-state <N>|landed` and exits 3 having done
# nothing, and is called again with the fact (`--close-targets`,
# `--issue-state`, `--landed-result`). Every fact is demanded before the first
# mutation, so the retry is replay-safe.
#
# Fails CLOSED toward removing nothing: a verb that did not run (missing or
# older binary, any exit but 0/3) removes no worktree and deletes no branch. The
# merge already succeeded, and a skipped cleanup is recoverable (`loom-clean`,
# the daemon's reaper, `worktree.sh remove`). Output is `LEVEL<TAB>message`
# records replayed through this script's own logging; `PLAIN` is a bare `echo`
# (a remediation command an operator pastes) and `CD` leaves a removed cwd.
_post_merge_cleanup() {
  local bin="${LOOM_DAEMON_SELF_BIN:-${LOOM_DAEMON_BIN:-loom-daemon}}"
  local base=(merge-pr post-merge-cleanup --repo-root "$REPO_ROOT" --pr "$PR_NUMBER" --branch "$PR_BRANCH" --head-sha "${PR_HEAD_SHA:-}")
  [[ -z "${DEFAULT_BRANCH_NAME:-}" ]] || base+=(--default-branch "$DEFAULT_BRANCH_NAME")
  [[ "$CLEANUP_WORKTREE" == "true" ]] || base+=(--no-cleanup-worktree)
  [[ "${CLEANUP_PRIMARY_CHECKOUT:-true}" == "true" ]] || base+=(--no-cleanup-primary)
  [[ "${LOOM_PRESERVE_WORKTREE:-0}" != "1" ]] || base+=(--preserve-env)
  [[ -z "$WORKTREE_PATH_OVERRIDE" ]] || base+=(--worktree-path "$WORKTREE_PATH_OVERRIDE")
  local facts=() out="" rc=0 attempt level text need
  for attempt in 1 2 3 4; do
    rc=0; out="$("$bin" "${base[@]}" ${facts[@]+"${facts[@]}"})" || rc=$?
    [[ $rc -eq 3 && "$out" == "LOOM-NEED "* ]] || break
    need="${out#LOOM-NEED }"
    case "$need" in
      close-targets) facts+=(--close-targets "$(forge_pr_close_targets "$PR_NUMBER" "$GH" 2>/dev/null || true)") ;;
      "issue-state "*) facts+=(--issue-state "$(forge_get_issue_state "$REPO_NWO" "${need#issue-state }" "$GH" 2>/dev/null || true)") ;;
      landed)
        if branch_has_landed "$PR_BRANCH" "${DEFAULT_BRANCH_NAME:-}" "${PR_HEAD_SHA:-}"; then facts+=(--landed-result landed); else facts+=(--landed-result not-landed); fi
        facts+=(--landed-verdict "${BRANCH_LANDED_VERDICT:-}" --landed-evidence "${BRANCH_LANDED_EVIDENCE:-}") ;;
      *) rc=2; break ;;
    esac
  done
  # Exit 0 without the terminator is a stale or muted binary, not a clean run.
  [[ $rc -ne 0 || "${out##*$'\n'}" == "LOOM-POST-MERGE-CLEANUP DONE" ]] || rc=4
  if [[ $rc -ne 0 ]]; then
    warning "Post-merge worktree cleanup for PR #$PR_NUMBER did not run — '$bin merge-pr post-merge-cleanup' exited $rc without finishing (no LOOM-POST-MERGE-CLEANUP DONE terminator; a loom-daemon predating #8191's orchestrator slice has no such verb). Nothing is removed and no local branch is deleted rather than guessed — the merge itself already succeeded and is unaffected. Clean up by hand: ${SCRIPT_DIR:-.loom/scripts}/worktree.sh remove <issue>, or loom-clean; then roll this host's loom-daemon to a build that has 'merge-pr post-merge-cleanup' (see .loom/docs/release-cadence.md) or pin LOOM_DAEMON_BIN to one."
    return 0
  fi
  while IFS=$'\t' read -r level text; do
    [[ -n "$level" ]] || continue
    case "$level" in
      SUCCESS) success "$text" ;;
      WARNING) warning "$text" ;;
      PLAIN) echo "$text" ;;
      CD) cd "$text" 2>/dev/null || true ;;
      LOOM-POST-MERGE-CLEANUP*) ;;
      *) info "$text" ;;
    esac
  done <<<"$out"
}
_post_merge_cleanup

success "Done"
