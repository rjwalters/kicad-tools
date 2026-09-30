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
# Returns on stdout: "merge", "squash", or "rebase" -- never anything else.
#
# Preference order when more than one strategy is allowed: merge > rebase >
# squash (#9105): merge commits preserve the branch's full history -- every
# commit, author, and timestamp -- the record Loom's blame/audit tooling
# joins on; squash flattens it away. This mirrors Loom's own installer
# default (setup-repository-settings.sh) as a tie-break; it is NOT a claim
# that merge commits are universally available.
#
# Fails OPEN to "merge" on: a network/auth error, an unparseable response,
# or a forge reporting every allow_* flag false (a degenerate state neither
# forge's UI actually permits). A transient probe failure therefore never
# blocks a merge outright -- and if the repo genuinely disallows merge
# commits, the merge call fails LOUDLY instead of silently squashing
# history (the pre-#7754 fail-open-to-squash behavior, deliberately
# reversed in #9105).
forge_detect_merge_method() {
  local nwo="$1" gh_cmd="${2:-gh}"
  local repo_json allow_squash allow_merge allow_rebase

  if [[ "$FORGE_TYPE" == "gitea" ]]; then
    forge_split_nwo "$nwo"
    repo_json="$(gitea_api GET "repos/$FORGE_OWNER/$FORGE_REPO" 2>/dev/null)" || { echo "merge"; return 0; }
    allow_squash="$(echo "$repo_json" | jq -r '.allow_squash_merge // empty' 2>/dev/null || true)"
    allow_merge="$(echo "$repo_json" | jq -r '.allow_merge_commits // empty' 2>/dev/null || true)"
    allow_rebase="$(echo "$repo_json" | jq -r '.allow_rebase_merge // empty' 2>/dev/null || true)"
  else
    repo_json="$("$gh_cmd" api "repos/$nwo" 2>/dev/null)" || { echo "merge"; return 0; }
    allow_squash="$(echo "$repo_json" | jq -r '.allow_squash_merge // empty' 2>/dev/null || true)"
    allow_merge="$(echo "$repo_json" | jq -r '.allow_merge_commit // empty' 2>/dev/null || true)"
    allow_rebase="$(echo "$repo_json" | jq -r '.allow_rebase_merge // empty' 2>/dev/null || true)"
  fi

  if [[ "$allow_merge" == "true" ]]; then
    echo "merge"
  elif [[ "$allow_rebase" == "true" ]]; then
    echo "rebase"
  elif [[ "$allow_squash" == "true" ]]; then
    echo "squash"
  else
    echo "merge"  # fail open to the default strategy (see header)
  fi
}
