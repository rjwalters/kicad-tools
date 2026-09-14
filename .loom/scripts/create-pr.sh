#!/usr/bin/env bash
# create-pr.sh - Open a PR, surviving a transient GitHub App permission window
# and never leaving a pushed-but-PR-less branch behind (#6074).
#
# A GitHub App installation token carries the permissions it was minted with
# and is then cached for ~1h. In the window after a permission grant
# propagates but before that cache turns over, `git push` can succeed
# (Contents:write present) while the very next `gh pr create` fails with
#
#     HTTP 403: Resource not accessible by integration
#
# Before this script, that killed the sweep with no PR: the issue stayed
# ready, the daemon re-dispatched it, and the next Builder REBUILT the
# identical work -- one duplicate build per pass, each leaving another
# orphaned `feature/issue-N` branch (observed on example-org/tool-repo#205,
# rebuilt 3+ times, and other-canary-repo#6; post-mortem example-org/fleet-repo#304).
#
# This is the single-sourced replacement for a bare `gh pr create` in a role
# prompt. It does three things a bare call cannot:
#
#   1. ADOPT-FIRST. If an open PR already exists for the head branch, its URL
#      is printed and the script exits 0 without creating anything. So a
#      re-dispatched Builder that finds its predecessor's branch already
#      pushed converges on the existing PR instead of failing or duplicating,
#      and a partially-completed earlier attempt is never re-done.
#   2. SUPERSEDED-TARGET-ISSUE CHECK (#6277). If the PR body carries a
#      closing keyword (`Closes`/`Fixes`/`Resolves #N`), re-verify the target
#      issue's freshness immediately before opening the PR: if it is already
#      CLOSED by a different, already-merged PR, refuse to open a duplicate.
#      `Part of #N` / `Contributes to #N` (partial-increment references for
#      a family/epic issue that intentionally stays open) never match the
#      closing-keyword pattern, so those PRs are exempt by construction.
#   3. CREDENTIAL ESCALATION on -- and only on -- the App permission-scope
#      403: force a fresh installation-token mint (bypassing the ~1h cache),
#      then a personal token. See `forge_gh_perm_safe` in lib/forge-helpers.sh
#      for the full ladder and why this is NOT the rate-limit fallback.
#
# Usage:
#   create-pr.sh --title TITLE (--body BODY | --body-file PATH) \
#                [--label LABEL]... [--base BRANCH] [--head BRANCH] \
#                [--draft] [--repo OWNER/REPO]
#   create-pr.sh --help
#
# Flags are a subset of `gh pr create`'s, chosen so a role prompt's existing
# invocation can be switched over by changing the command name:
#   --title, -t TITLE     PR title (required).
#   --body, -b BODY       PR body as literal text.
#   --body-file, -F PATH  Read the body from PATH ("-" = stdin). Mutually
#                         exclusive with --body.
#   --label, -l LABEL     Label to apply at creation. Repeatable. A single
#                         comma-separated value is also accepted, matching
#                         `gh pr create --label "a,b"`.
#   --base, -B BRANCH     Base branch (omit for the repo default).
#   --head, -H BRANCH     Head branch (omit for the current branch).
#   --draft, -d           Create as a draft.
#   --repo, -R OWNER/REPO Target repository. Omit for the current repo.
#
# Output: the PR's URL on stdout -- newly created OR adopted (identical to
# `gh pr create`, so a caller parsing the URL needs no change).
#
# Exit codes:
#   0 - A PR exists for this branch (created by this call, or adopted).
#   1 - Creation failed, or the target issue was already closed by a
#       superseding PR (message on stderr in both cases).
#   2 - Invalid arguments.
#
# NOTE: GitHub-specific, like create-issue.sh. On a Gitea forge it exits 2 --
# Gitea has no GitHub App installation tokens, so it has no equivalent
# failure mode.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./lib/forge-helpers.sh
source "$SCRIPT_DIR/lib/forge-helpers.sh"

usage() {
  sed -n '2,69p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
}

TITLE=""
BODY=""
BODY_FILE=""
BASE_BRANCH=""
HEAD_BRANCH=""
REPO_NWO=""
DRAFT=false
LABELS=()
HAVE_BODY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help | -h)
      usage
      exit 0
      ;;
    --title | -t)
      TITLE="${2:-}"
      shift 2
      ;;
    --body | -b)
      BODY="${2:-}"
      HAVE_BODY=true
      shift 2
      ;;
    --body-file | -F)
      BODY_FILE="${2:-}"
      shift 2
      ;;
    --label | -l)
      # `gh pr create --label "a,b"` splits on commas; match that so a
      # prompt's existing invocation transfers unchanged.
      IFS=',' read -r -a _split <<< "${2:-}"
      for _l in "${_split[@]}"; do
        _l="${_l#"${_l%%[![:space:]]*}"}"
        _l="${_l%"${_l##*[![:space:]]}"}"
        [[ -n "$_l" ]] && LABELS+=("$_l")
      done
      shift 2
      ;;
    --base | -B)
      BASE_BRANCH="${2:-}"
      shift 2
      ;;
    --head | -H)
      HEAD_BRANCH="${2:-}"
      shift 2
      ;;
    --draft | -d)
      DRAFT=true
      shift
      ;;
    --repo | -R)
      REPO_NWO="${2:-}"
      shift 2
      ;;
    *)
      echo "create-pr.sh: unknown argument: $1" >&2
      echo "Run 'create-pr.sh --help' for usage." >&2
      exit 2
      ;;
  esac
done

if [[ -z "$TITLE" ]]; then
  echo "create-pr.sh: --title is required" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]] && [[ "$HAVE_BODY" == "true" ]]; then
  echo "create-pr.sh: --body and --body-file are mutually exclusive" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]]; then
  if [[ "$BODY_FILE" == "-" ]]; then
    BODY="$(cat)"
  elif [[ -r "$BODY_FILE" ]]; then
    BODY="$(cat "$BODY_FILE")"
  else
    echo "create-pr.sh: cannot read --body-file: $BODY_FILE" >&2
    exit 2
  fi
fi

forge_detect
if [[ "$FORGE_TYPE" != "github" ]]; then
  echo "create-pr.sh: this GitHub App permission-window fallback is \
GitHub-specific; on $FORGE_TYPE open the PR with your forge's own CLI." >&2
  exit 2
fi

# `gh pr create` infers the head branch from the checkout, but the adopt check
# below needs it explicitly -- and passing it explicitly also stops a failed
# origin auto-detect from orphaning the remote branch (#3244).
if [[ -z "$HEAD_BRANCH" ]]; then
  HEAD_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
fi
if [[ -z "$HEAD_BRANCH" || "$HEAD_BRANCH" == "HEAD" ]]; then
  echo "create-pr.sh: could not determine the head branch (pass --head)" >&2
  exit 2
fi

# --- Version-bearing-file sync check (#6730, #7168) -------------------------
#
# Recurring failure mode: a Builder's version-bump commit hand-edits (or
# mirrors via a partial script) the VERSION_FILES set in scripts/version.sh
# but leaves a separate version-bearing file -- in practice always
# .loom/install-metadata.json -- stale, which only surfaces later as a CI-only
# "Installer Integration Tests" failure that a Judge/Doctor has to patch by
# hand (observed twice in one day on #6497 and #6212). `scripts/version.sh
# check` already catches this correctly; the gap was that nothing forced it
# to run before a PR was opened -- builder-pr.md's "defaults/ VERSION-Bump
# Gate" documented running it by hand, but a checklist step a Builder can
# forget is not enforcement.
#
# The actual gate logic (resolution order, LOOM_VERSION_CHECK_SCRIPT test
# seam, BLOCKER:/Fix: message style) lives in version-check-gate.sh, shared
# with Doctor's merge-conflict rebase recipes (defaults/roles/doctor.md),
# which push directly with `git push --force-with-lease` and never route
# through this script -- see version-check-gate.sh's own header for why that
# second call site exists (#7168).
if [[ -x "$SCRIPT_DIR/version-check-gate.sh" ]]; then
  if ! "$SCRIPT_DIR/version-check-gate.sh" --fix-hint "then re-run create-pr.sh."; then
    exit 1
  fi
fi

# --- Adopt-first ------------------------------------------------------------
#
# An existing open PR for this head branch means the work is already in
# review: print its URL and stop. This is what turns a re-dispatch into a
# no-op instead of a duplicate build, and it makes the whole script idempotent
# (safe to re-run after any partial failure). A failure of the LOOKUP itself
# (rate limit, network) is never fatal -- fall through and let the create call
# be the authority; GitHub rejects a genuine duplicate on its own.
_adopt_args=(pr list --head "$HEAD_BRANCH" --state open --json url --jq '.[0].url')
if [[ -n "$REPO_NWO" ]]; then
  _adopt_args+=(--repo "$REPO_NWO")
fi
EXISTING_URL="$(gh "${_adopt_args[@]}" 2>/dev/null || true)"
if [[ -n "$EXISTING_URL" && "$EXISTING_URL" != "null" ]]; then
  echo "create-pr.sh: an open PR already exists for $HEAD_BRANCH — adopting it" >&2
  printf '%s\n' "$EXISTING_URL"
  exit 0
fi

# --- Superseded-target-issue freshness check (#6277) -------------------------
#
# Two workers racing on the same issue is not caught today until Judge
# review -- the most expensive point in the pipeline to discover it. Mirror
# the adopt-first idempotency check above: re-verify the *target issue*
# immediately before opening a brand-new PR for it, and refuse to proceed if
# it was already closed by a different, already-merged PR.
#
# Only a CLOSING keyword (`Closes`/`Fixes`/`Resolves` immediately followed by
# `#N`, the set builder-pr.md documents under "GitHub Auto-Close
# Requirements") is in scope. `Part of #N` / `Contributes to #N` -- the
# partial-increment references for a family/epic issue that intentionally
# stays open across multiple PRs -- never match this pattern, so those PRs
# are exempt by construction; no separate carve-out is needed.
CLOSES_ISSUE=""
if [[ -n "$BODY" ]]; then
  CLOSES_ISSUE="$(grep -ioE '\b(close[sd]?|closing|fix(e[sd])?|resolve[sd]?)[[:space:]]+#[0-9]+' <<< "$BODY" \
    | head -1 | grep -oE '[0-9]+' || true)"
fi

if [[ -n "$CLOSES_ISSUE" ]]; then
  # shellcheck disable=SC2054  # the comma is inside a single --json value, not an array separator
  _issue_view_args=(issue view "$CLOSES_ISSUE" --json state,closedByPullRequestsReferences)
  if [[ -n "$REPO_NWO" ]]; then
    _issue_view_args+=(--repo "$REPO_NWO")
  fi
  # A lookup failure (rate limit, network, deleted issue) is never fatal here
  # either -- fail open, same posture as the adopt-first lookup above.
  ISSUE_STATE="$(gh "${_issue_view_args[@]}" --jq '.state // empty' 2>/dev/null || true)"
  if [[ "$ISSUE_STATE" == "CLOSED" ]]; then
    SUPERSEDING_PR_NUMBER="$(gh "${_issue_view_args[@]}" \
      --jq '.closedByPullRequestsReferences[0].number // empty' 2>/dev/null || true)"
    SUPERSEDING_PR_URL="$(gh "${_issue_view_args[@]}" \
      --jq '.closedByPullRequestsReferences[0].url // empty' 2>/dev/null || true)"
    SUPERSEDING_HEAD=""
    if [[ -n "$SUPERSEDING_PR_NUMBER" ]]; then
      _pr_view_args=(pr view "$SUPERSEDING_PR_NUMBER" --json headRefName --jq '.headRefName')
      if [[ -n "$REPO_NWO" ]]; then
        _pr_view_args+=(--repo "$REPO_NWO")
      fi
      SUPERSEDING_HEAD="$(gh "${_pr_view_args[@]}" 2>/dev/null || true)"
    fi
    # If the closing PR IS this branch (re-running after our own PR already
    # merged the issue closed), this is not a supersede -- fall through.
    if [[ -z "$SUPERSEDING_PR_NUMBER" || "$SUPERSEDING_HEAD" != "$HEAD_BRANCH" ]]; then
      echo "create-pr.sh: target issue #$CLOSES_ISSUE is already CLOSED\
${SUPERSEDING_PR_NUMBER:+ by #$SUPERSEDING_PR_NUMBER}\
${SUPERSEDING_PR_URL:+ ($SUPERSEDING_PR_URL)} -- refusing to open a \
duplicate PR for $HEAD_BRANCH. Your commits are already pushed; do NOT push \
further and do NOT delete the branch. Rebase onto the superseding PR, \
retarget this PR at a different issue, or discard this branch's work as \
redundant." >&2
      exit 1
    fi
  fi
fi

# --- Create -----------------------------------------------------------------

CREATE_ARGS=(pr create --head "$HEAD_BRANCH" --title "$TITLE" --body "$BODY")
if [[ -n "$BASE_BRANCH" ]]; then
  CREATE_ARGS+=(--base "$BASE_BRANCH")
fi
if [[ -n "$REPO_NWO" ]]; then
  CREATE_ARGS+=(--repo "$REPO_NWO")
fi
if [[ "$DRAFT" == "true" ]]; then
  CREATE_ARGS+=(--draft)
fi
for _label in "${LABELS[@]+"${LABELS[@]}"}"; do
  CREATE_ARGS+=(--label "$_label")
done

if ! forge_gh_perm_safe "${CREATE_ARGS[@]}"; then
  echo "create-pr.sh: could not open a PR for $HEAD_BRANCH. If the commits are \
pushed, do NOT rebuild — re-run this script (it adopts an existing PR) or open \
the PR by hand from that branch." >&2
  exit 1
fi
