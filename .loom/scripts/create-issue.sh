#!/usr/bin/env bash
# create-issue.sh - File a new issue, surviving GraphQL rate-limit exhaustion.
#
# `gh issue create` is GraphQL-backed. GitHub's GraphQL quota (5000/hr, shared
# across every agent + tool) and its REST quota are INDEPENDENT, and in a busy
# fleet the GraphQL pool exhausts while REST sits nearly untouched (observed
# 2026-08-03: core 19/5000 consumed vs graphql 1378/5000). Before #5047, every
# issue-filing role -- Architect, Auditor, Curator (decomposition), Builder
# (decomposition), Doctor, Hermit, Judge -- died outright at that moment, even
# though comments, labels and issue state already had documented REST
# fallbacks (#4856).
#
# This script is the single-sourced replacement for a bare `gh issue create`
# in a role prompt: it tries `gh issue create` first, and on a rate-limit
# rejection (and ONLY on a rate-limit rejection) retries the identical filing
# as one REST POST to `repos/{owner}/{repo}/issues`. Labels ride along in the
# same call on both paths -- never a create-then-label sequence.
#
# Usage:
#   create-issue.sh --title TITLE (--body BODY | --body-file PATH) \
#                   [--label LABEL]... [--repo OWNER/REPO]
#   create-issue.sh --help
#
# Flags are a subset of `gh issue create`'s, chosen so a role prompt's
# existing invocation can be switched over by changing the command name:
#   --title, -t TITLE     Issue title (required).
#   --body, -b BODY       Issue body as literal text.
#   --body-file, -F PATH  Read the body from PATH ("-" = stdin). Mutually
#                         exclusive with --body.
#   --label, -l LABEL     Label to apply at creation. Repeatable. A single
#                         comma-separated value is also accepted, matching
#                         `gh issue create --label "a,b"`.
#   --repo, -R OWNER/REPO Target repository. Omit for the current repo (the
#                         preferred form -- the REST path then resolves the
#                         repo from the git remote with zero API calls).
#
# Output: the new issue's URL on stdout (identical to `gh issue create`).
#
# Exit codes:
#   0 - Issue created (via either path).
#   1 - Creation failed (message on stderr). A non-rate-limit failure is
#       reported as-is and is NEVER retried over REST.
#   2 - Invalid arguments.
#
# NOTE: this is GitHub-specific, like the other `*_rl_safe` helpers. On a
# Gitea forge it exits 2 with a pointer to `gh`/`tea` -- Gitea does not have
# GitHub's split GraphQL/REST quota, so it has no equivalent failure mode.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./lib/forge-helpers.sh
source "$SCRIPT_DIR/lib/forge-helpers.sh"

usage() {
  sed -n '2,47p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
}

TITLE=""
BODY=""
BODY_FILE=""
REPO_NWO=""
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
      # `gh issue create --label "a,b"` splits on commas; match that so a
      # prompt's existing invocation transfers unchanged.
      IFS=',' read -r -a _split <<< "${2:-}"
      for _l in "${_split[@]}"; do
        # Trim surrounding whitespace from "a, b".
        _l="${_l#"${_l%%[![:space:]]*}"}"
        _l="${_l%"${_l##*[![:space:]]}"}"
        [[ -n "$_l" ]] && LABELS+=("$_l")
      done
      shift 2
      ;;
    --repo | -R)
      REPO_NWO="${2:-}"
      shift 2
      ;;
    *)
      echo "create-issue.sh: unknown argument: $1" >&2
      echo "Run 'create-issue.sh --help' for usage." >&2
      exit 2
      ;;
  esac
done

if [[ -z "$TITLE" ]]; then
  echo "create-issue.sh: --title is required" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]] && [[ "$HAVE_BODY" == "true" ]]; then
  echo "create-issue.sh: --body and --body-file are mutually exclusive" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]]; then
  if [[ "$BODY_FILE" == "-" ]]; then
    BODY="$(cat)"
  elif [[ -r "$BODY_FILE" ]]; then
    BODY="$(cat "$BODY_FILE")"
  else
    echo "create-issue.sh: cannot read --body-file: $BODY_FILE" >&2
    exit 2
  fi
fi

forge_detect
if [[ "$FORGE_TYPE" != "github" ]]; then
  echo "create-issue.sh: this GraphQL-exhaustion fallback is GitHub-specific; \
on $FORGE_TYPE file the issue with your forge's own CLI (Gitea has no split \
GraphQL/REST quota, so it has no equivalent failure mode)." >&2
  exit 2
fi

if ! forge_gh_create_issue_rl_safe "$REPO_NWO" "$TITLE" "$BODY" "${LABELS[@]+"${LABELS[@]}"}"; then
  exit 1
fi
