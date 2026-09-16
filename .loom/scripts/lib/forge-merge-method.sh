#!/usr/bin/env bash
# forge-merge-method.sh - Detect a target repo's actually-allowed merge
# strategy, for forge-helpers.sh's merge call sites (#7754).
#
# Split out of forge-helpers.sh as a sibling module per the file-size ratchet
# (scripts/file-size-baseline.txt / .loom/docs/file-size-policy.md) --
# forge-helpers.sh is already over the tracked-line threshold and frozen at
# its current size, so new functionality lands in a new file instead of
# growing it further.
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/forge-merge-method.sh"
#   forge_detect_merge_method "owner/repo" "$GH"
#
# Depends on FORGE_TYPE, forge_split_nwo(), and gitea_api() from
# forge-helpers.sh -- always sourced from there, never standalone (relies on
# the parent's `set -euo pipefail`).

# Detect which merge strategy the target repo actually allows, for callers
# that must send an explicit merge_method/Do value (#7754). GitHub and Gitea
# both let repo admins disable squash-merge entirely (Gitea's "Squash merges
# are not allowed on this repository" is the original #1258 failure), so a
# hardcoded `squash` breaks every such repo outright.
#
# Usage: forge_detect_merge_method NWO [GH_CMD]
# Returns on stdout: "squash", "merge", or "rebase" -- never anything else.
#
# Preference order when more than one strategy is allowed: squash > merge >
# rebase. This mirrors Loom's own installer default
# (setup-repository-settings.sh) purely as a tie-break -- it is NOT a claim
# that squash is universally available, just the least-surprising choice when
# a repo permits it alongside other strategies.
#
# Fails OPEN to "squash" (the historical hardcoded behavior) on: a network/
# auth error, an unparseable response, or a forge reporting every allow_*
# flag false (a degenerate state neither forge's UI actually permits). This
# keeps a transient probe failure from blocking a merge outright -- worst
# case it reproduces the pre-#7754 behavior for that one call.
forge_detect_merge_method() {
  local nwo="$1" gh_cmd="${2:-gh}"
  local repo_json allow_squash allow_merge allow_rebase

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    repo_json="$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO" 2>/dev/null)" || { echo "squash"; return 0; }
    allow_squash="$(echo "$repo_json" | jq -r '.allow_squash_merge // empty' 2>/dev/null || true)"
    allow_merge="$(echo "$repo_json" | jq -r '.allow_merge_commits // empty' 2>/dev/null || true)"
    allow_rebase="$(echo "$repo_json" | jq -r '.allow_rebase_merge // empty' 2>/dev/null || true)"
  else
    repo_json="$("$gh_cmd" api "repos/$nwo" 2>/dev/null)" || { echo "squash"; return 0; }
    allow_squash="$(echo "$repo_json" | jq -r '.allow_squash_merge // empty' 2>/dev/null || true)"
    allow_merge="$(echo "$repo_json" | jq -r '.allow_merge_commit // empty' 2>/dev/null || true)"
    allow_rebase="$(echo "$repo_json" | jq -r '.allow_rebase_merge // empty' 2>/dev/null || true)"
  fi

  if [[ "$allow_squash" == "true" ]]; then
    echo "squash"
  elif [[ "$allow_merge" == "true" ]]; then
    echo "merge"
  elif [[ "$allow_rebase" == "true" ]]; then
    echo "rebase"
  else
    echo "squash"
  fi
}
