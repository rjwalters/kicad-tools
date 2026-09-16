#!/usr/bin/env bash
# clean-stale-building-labels.sh - Strip a stale `loom:building` claim label
# from CLOSED issues (#6199).
#
# Context: merge-pr.sh's post-merge choke point (_strip_closed_issue_building_labels,
# #6199) now removes `loom:building` from an issue as part of the merge that
# closes it — but only for the merge-driven `Closes #N` / `Fixes #N` /
# `Resolves #N` auto-close path. Two populations are NOT covered by that:
#
#   1. Issues closed OUTSIDE a merge — manually by an operator, closed as a
#      duplicate, or closed by an autonomous role via `--reason "not
#      planned"`. merge-pr.sh has no hook into any of those paths.
#   2. Instances that ALREADY accumulated before #6199 shipped (one consumer
#      repo measured 20 on 2026-08-14; this repo has its own backlog going
#      back to early issue numbers).
#
# Recorded scope decision (#6199): rather than build an automatic reconcile
# pass for population 1 (e.g. a `loom-daemon clean` periodic sweep, which
# would need its own polling/liveness reasoning for a purely cosmetic
# defect — see #2838's original "labels on closed items are harmless"
# finding, which still holds; the only thing #6199 changes is that
# `loom:building` specifically gets read as an in-flight signal by some
# consumers without a state filter), this script is the intentional fix for
# BOTH populations: idempotent, forge-read-driven, safe to run by hand
# whenever the backlog is noticed, and safe to wire into a periodic role
# (Doctor/Auditor/Champion) later if the backlog turns out to regrow faster
# than "run it when noticed" comfortably handles.
#
# Usage:
#   ./clean-stale-building-labels.sh [--repo OWNER/NAME] [--dry-run] [--json]
#
# Options:
#   --repo OWNER/NAME   Target a specific repo (default: the repo of the
#                       current working directory's git remote).
#   --dry-run           List what would be changed without changing anything.
#   --json              Emit a JSON summary on stdout instead of human text.
#
# Safety:
#   - Only ever touches CLOSED issues that currently carry `loom:building`.
#   - Removes ONLY the `loom:building` label — no other label, no state
#     change, no comment.
#   - Idempotent: a second run finds nothing left to do.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/forge-helpers.sh
source "$SCRIPT_DIR/lib/forge-helpers.sh"

DRY_RUN=false
JSON_OUTPUT=false
REPO_NWO_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_NWO_ARG="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --json)
      JSON_OUTPUT=true
      shift
      ;;
    -h|--help)
      # Keep this range in sync with the header comment block (currently lines 2-42).
      sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

forge_detect

if [[ -n "$REPO_NWO_ARG" ]]; then
  REPO_NWO="$REPO_NWO_ARG"
else
  REPO_NWO="$(forge_get_repo_nwo gh)"
fi

if [[ -z "$REPO_NWO" ]]; then
  echo "ERROR: could not resolve a repo (pass --repo OWNER/NAME)" >&2
  exit 1
fi

if [[ "$FORGE_TYPE" != "github" ]]; then
  echo "ERROR: clean-stale-building-labels.sh only supports GitHub today (FORGE_TYPE=$FORGE_TYPE)" >&2
  exit 1
fi

if [[ "$JSON_OUTPUT" != "true" ]]; then
  echo "Scanning $REPO_NWO for closed issues still carrying loom:building..."
fi

# Remove `loom:building` from one issue via a single REST DELETE call — NOT
# forge_gh_remove_label_rl_safe's default `gh issue edit` (GraphQL-backed)
# path. A bulk one-time/periodic cleanup can iterate hundreds of issues (851
# measured on this repo alone at #6199's fix time) and going straight to REST
# avoids burning the shared GraphQL pool (5000/hr, contended by every live
# sweep) on a batch of mutations that have a cheap REST-only equivalent —
# the same "route bulk/independent work to the idle REST pool" reasoning
# CLAUDE.md documents for issue creation (#5047). A 404 (label already
# absent — e.g. a concurrent run, or #6199's own merge-path cleanup got there
# first) is treated as success: idempotent by construction.
_remove_building_label_rest() {
  local issue_num="$1"
  local out
  if out=$(gh api "repos/$REPO_NWO/issues/$issue_num/labels/loom%3Abuilding" -X DELETE 2>&1); then
    return 0
  fi
  if grep -qi 'not found\|404' <<<"$out"; then
    return 0
  fi
  echo "$out" >&2
  return 1
}

# Every closed issue currently labelled loom:building. `gh issue list` caps at
# 30 by default; --limit is set generously high since this is a small,
# infrequent audit, not a hot-path query.
# `mapfile` is bash 4+; macOS ships 3.2 (#7751). Skipping empty lines matters
# here: the `|| true` means a failed gh call yields no output, and an empty
# read must leave stale_numbers empty rather than holding one blank entry --
# `total` below is computed from its length and drives the early-exit path.
stale_numbers=()
while IFS= read -r _num; do
  [[ -n "$_num" ]] || continue
  stale_numbers+=("$_num")
done < <(
  gh issue list --repo "$REPO_NWO" --state closed --label "loom:building" \
    --limit 1000 --json number --jq '.[].number' 2>/dev/null || true
)

total="${#stale_numbers[@]}"
cleaned=0
failed=0

if [[ "$total" -eq 0 ]]; then
  if [[ "$JSON_OUTPUT" == "true" ]]; then
    printf '{"repo":"%s","dry_run":%s,"total":0,"cleaned":0,"failed":0,"issues":[]}\n' \
      "$REPO_NWO" "$DRY_RUN"
  else
    echo "No stale loom:building claims on closed issues — nothing to do."
  fi
  exit 0
fi

for issue_num in "${stale_numbers[@]}"; do
  [[ -n "$issue_num" ]] || continue

  if [[ "$DRY_RUN" == "true" ]]; then
    [[ "$JSON_OUTPUT" == "true" ]] || echo "Would remove loom:building from closed issue #$issue_num"
    cleaned=$((cleaned + 1))
    continue
  fi

  if _remove_building_label_rest "$issue_num" 2>/dev/null; then
    [[ "$JSON_OUTPUT" == "true" ]] || echo "Removed loom:building from closed issue #$issue_num"
    cleaned=$((cleaned + 1))
  else
    [[ "$JSON_OUTPUT" == "true" ]] || echo "FAILED to remove loom:building from closed issue #$issue_num" >&2
    failed=$((failed + 1))
  fi
done

if [[ "$JSON_OUTPUT" == "true" ]]; then
  issues_json="$(printf '%s\n' "${stale_numbers[@]}" | jq -R 'tonumber' | jq -s '.')"
  jq -n --arg repo "$REPO_NWO" --argjson dry_run "$DRY_RUN" \
    --argjson total "$total" --argjson cleaned "$cleaned" --argjson failed "$failed" \
    --argjson issues "$issues_json" \
    '{repo: $repo, dry_run: $dry_run, total: $total, cleaned: $cleaned, failed: $failed, issues: $issues}'
else
  echo
  echo "Summary: $total stale claim(s) found, $cleaned $([[ "$DRY_RUN" == "true" ]] && echo "would be cleaned" || echo "cleaned"), $failed failed"
fi

[[ "$failed" -eq 0 ]]
