#!/usr/bin/env bash
# version-check-gate.sh - Shared version-bearing-file sync gate (#6730, #7168)
#
# Runs `scripts/version.sh check` and, on a mismatch, prints a BLOCKER:/Fix:
# message pair (mirroring builder-pr.md's own "defaults/ VERSION-Bump Gate"
# style) and exits non-zero. Extracted from create-pr.sh's original inline
# gate (#6730) so more than one caller can share the exact same enforcement
# instead of each hand-rolling (or forgetting) its own copy.
#
# Why a second caller was needed (#7168): create-pr.sh's gate only runs once,
# at PR-creation time. It does NOT cover Doctor's merge-conflict rebase
# recipes (defaults/roles/doctor.md), which push directly with
# `git push --force-with-lease` and never route through create-pr.sh. A
# rebase silently absorbs whatever version-bearing values origin/main already
# had -- .loom/install-metadata.json never appears as a conflicting file (the
# local branch's own commit never touched it), so git raises no conflict, but
# the resulting value can still be stale relative to VERSION/the other files
# that WERE part of the conflict resolution. Doctor's rebase recipes now call
# this same script after conflict resolution and before the force-push.
#
# Resolution order for which `version.sh` to run:
#   1. $LOOM_VERSION_CHECK_SCRIPT if set (test seam -- same convention as
#      LOOM_GITHUB_APP_SCRIPT in lib/forge-helpers.sh, lets a test stub
#      deterministic pass/fail/missing-file behavior without depending on
#      this repo's own ambient version state).
#   2. <current worktree's own top-level>/scripts/version.sh, if present.
#      Resolved from the CALLER's own top-level (not forge-helpers.sh's
#      _forge_config_root, which deliberately points at the main checkout
#      for config sharing) because the version-bearing files being checked
#      are worktree-local content, not shared .git state.
#   3. Not found -> `scripts/version.sh` is a Loom-repo-only dev script
#      (never installed into a consumer's .loom/ surface, same as
#      .loom/install-metadata.json itself) -- exit 0, not a failure.
#
# Usage:
#   version-check-gate.sh [--fix-hint "<text appended after the bump command>"]
#
# Exit codes:
#   0 = all version-bearing files agree, or scripts/version.sh isn't
#       resolvable in this checkout (nothing to check)
#   1 = a mismatch was found; the raw `version.sh check` MISMATCH output is
#       printed to stderr, followed by a BLOCKER:/Fix: message pair

set -euo pipefail

FIX_HINT="then re-run."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --fix-hint)
      FIX_HINT="${2:?--fix-hint requires an argument}"
      shift 2
      ;;
    -h|--help)
      grep '^#' "$0" | sed '1d;s/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "version-check-gate.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

VERSION_CHECK_SCRIPT="${LOOM_VERSION_CHECK_SCRIPT:-}"
if [[ -z "$VERSION_CHECK_SCRIPT" ]]; then
  _worktree_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$_worktree_root" && -f "$_worktree_root/scripts/version.sh" ]]; then
    VERSION_CHECK_SCRIPT="$_worktree_root/scripts/version.sh"
  fi
fi

if [[ -z "$VERSION_CHECK_SCRIPT" ]]; then
  # Not a dogfooded Loom checkout -- nothing to check, not a failure.
  exit 0
fi

VERSION_CHECK_STATUS=0
VERSION_CHECK_OUTPUT="$(bash "$VERSION_CHECK_SCRIPT" check 2>&1)" || VERSION_CHECK_STATUS=$?

if [[ "$VERSION_CHECK_STATUS" -ne 0 ]]; then
  echo "$VERSION_CHECK_OUTPUT" >&2
  echo "version-check-gate.sh: BLOCKER: 'scripts/version.sh check' found a version mismatch -- see MISMATCH line(s) above." >&2
  echo "version-check-gate.sh: Fix: ./scripts/version.sh bump patch   (re-syncs all version-bearing files, including .loom/install-metadata.json if present), $FIX_HINT" >&2
  exit 1
fi

exit 0
