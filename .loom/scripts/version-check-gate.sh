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
# A second, related drop shape (#7351): even when the branch's OWN commits DO
# touch .loom/install-metadata.json (e.g. a dedicated "resync install-metadata
# after rebase" commit) and origin/main independently touched the same file,
# a rebase can still silently discard the branch's edit with no conflict ever
# raised, printing `dropping <sha> ... -- patch contents already upstream`.
# This isn't a git diff/patch-id quirk (a report attributing it to git's
# adjacent-line 3-way-merge hunking was investigated and superseded, see
# #7351) -- it's this repo's own `.loom/install-metadata.json merge=ours`
# .gitattributes driver (#4528), which always keeps "ours" on any merge
# touching that path. During a rebase, "ours" is the upstream side being
# rebased onto, so the driver discards the replayed commit's edit wholesale
# (any line, not just an adjacent one) whenever origin/main also touched the
# file -- git then sees an empty resulting diff and drops the commit outright.
# This gate, run immediately after the rebase and before the push, still
# catches the resulting mismatch the same way it catches the first shape
# above -- see test-version-check-gate.sh's T8/T9 for a real `git rebase`
# repro of both this drop and the false-positive-free ordinary case.
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
# A third drop shape (#7417): `scripts/version.sh bump` (no `--tag`) only
# rewrites the version-bearing files on disk -- the `git add`/`git commit`
# pair lives exclusively inside `do_tag()`, which only runs when a caller
# passes `--tag`. Before #7743/#7954 forbade it, Doctor's rebase recipes
# would run `version.sh bump <part>` with no `--tag` from this gate's own
# printed Fix: hint; a caller that followed that hint and pushed without
# also committing could push a head where the bump exists on disk but never
# landed in the committed tree -- `version.sh check` alone can't catch this
# because it only compares the files against EACH OTHER on disk, never
# against git's index/HEAD. When VERSION_CHECK_SCRIPT was auto-detected
# (case 2 above, meaning file paths are trustworthy relative to a real
# worktree), this gate additionally fails if any version-bearing file is
# modified/untracked-but-present relative to HEAD.
#
# A fourth drop shape (#7705): the #7417 check above only walks
# `$(scripts/version.sh list)`, and `list` deliberately excludes
# `Cargo.lock`/`mcp-loom/package-lock.json` -- they're "derived artifacts" as
# far as `/repo:release`'s use of `list` is concerned, even though
# `version.sh check`/`bump` both DO treat them as version-bearing on disk. A
# caller that runs `bump <part>` (regenerating both lockfiles on disk) and
# then stages/commits only `$(scripts/version.sh list)` passes BOTH the
# content-comparison check above AND the #7417 dirty check above cleanly --
# every version-bearing file the gate walks agrees with itself and with git
# HEAD -- while the lockfiles stay modified-but-uncommitted and never reach
# the pushed branch (the #7673/#7680 symptom). This gate now walks a second,
# fixed (Cargo.lock, mcp-loom/package-lock.json) list for the same
# dirty-relative-to-HEAD check, kept separate from VERSION_FILES_LIST so
# `scripts/version.sh list`'s own output contract never has to change.
#
# Usage:
#   version-check-gate.sh [--fix-hint "<text appended after the bump command>"]
#
# Exit codes:
#   0 = all version-bearing files (including Cargo.lock/mcp-loom/
#       package-lock.json) agree with each other AND with git HEAD, or
#       scripts/version.sh isn't resolvable in this checkout (nothing to
#       check)
#   1 = a mismatch (files disagree with each other) or an uncommitted bump
#       (files agree with each other but not with git HEAD, including a
#       lockfile-only drop) was found; the raw `version.sh check` MISMATCH
#       output (mismatch case) or a list of the dirty file(s)
#       (uncommitted-bump case) is printed to stderr, followed by a
#       BLOCKER:/Fix: message pair

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
VERSION_CHECK_SCRIPT_AUTO_DETECTED=false
if [[ -z "$VERSION_CHECK_SCRIPT" ]]; then
  _worktree_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$_worktree_root" && -f "$_worktree_root/scripts/version.sh" ]]; then
    VERSION_CHECK_SCRIPT="$_worktree_root/scripts/version.sh"
    VERSION_CHECK_SCRIPT_AUTO_DETECTED=true
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
  echo "version-check-gate.sh: Fix: NEVER run './scripts/version.sh bump' here -- #7743 forbids a hand-bump on a PR branch and CI's defaults-version-bump-check will reject it. A clean rebase lands origin/main's own values, so a mismatch means this branch itself carries a version-bearing edit (often a pre-#7743 bump commit): revert the MISMATCH file(s) above to origin/main's values (git checkout origin/main -- <file(s)>, e.g. VERSION CLAUDE.md .loom/install-metadata.json) and commit that, $FIX_HINT" >&2
  exit 1
fi

# Uncommitted-bump check (#7417) -- see the header comment above for why this
# is needed and why it's gated on auto-detection. Only meaningful when
# VERSION_CHECK_SCRIPT was auto-detected as THIS worktree's own
# scripts/version.sh: a caller-supplied LOOM_VERSION_CHECK_SCRIPT stub (the
# test seam) has no fixed relationship to real tracked file paths in
# $_worktree_root, so this check does not apply to it.
if $VERSION_CHECK_SCRIPT_AUTO_DETECTED; then
  VERSION_FILES_LIST="$(bash "$VERSION_CHECK_SCRIPT" list 2>/dev/null || true)"
  DIRTY_FILES=""
  if [[ -n "$VERSION_FILES_LIST" ]]; then
    while IFS= read -r _vf; do
      [[ -z "$_vf" ]] && continue
      if [[ -n "$(cd "$_worktree_root" && git status --porcelain -- "$_vf" 2>/dev/null)" ]]; then
        DIRTY_FILES+="$_vf"$'\n'
      fi
    done <<< "$VERSION_FILES_LIST"
  fi
  if [[ -n "$DIRTY_FILES" ]]; then
    echo "version-check-gate.sh: version-bearing file(s) are modified on disk but not committed:" >&2
    while IFS= read -r _df; do
      [[ -z "$_df" ]] && continue
      echo "  $_df" >&2
    done <<< "$DIRTY_FILES"
    echo "version-check-gate.sh: BLOCKER: a version bump exists in the worktree but was never committed -- pushing now would push code changes without the bump." >&2
    echo "version-check-gate.sh: Fix: git add -- <file(s) above> && git commit ('scripts/version.sh bump' only rewrites files; it does not commit -- only 'bump --tag'/'set --tag' does), $FIX_HINT" >&2
    exit 1
  fi

  # Lockfile-specific uncommitted-bump check (#7705). `Cargo.lock` and
  # `mcp-loom/package-lock.json` are DELIBERATELY excluded from
  # `scripts/version.sh list`'s output above (list's contract is consumed by
  # `/repo:release` and must stay unchanged), even though `version.sh
  # check`/`bump` both treat them as version-bearing on disk. That means the
  # DIRTY_FILES loop above never looks at them, so a `bump <part>` whose
  # lockfile changes were regenerated on disk but never staged/committed
  # (e.g. a caller that stages only $(./scripts/version.sh list) + commits)
  # passed this gate cleanly -- both files agreeing with each other AND with
  # every OTHER version-bearing file already at the new version -- and the
  # lockfile drift only surfaced later, as CI's "Installer Integration
  # Tests" job failing 'version.sh check' on the pushed branch. This is a
  # distinct, fixed list (not folded into VERSION_FILES_LIST) precisely so
  # scripts/version.sh's own `list` output never has to change.
  LOCK_FILES="Cargo.lock"$'\n'"mcp-loom/package-lock.json"
  DIRTY_LOCK_FILES=""
  while IFS= read -r _lf; do
    [[ -z "$_lf" ]] && continue
    [[ -f "$_worktree_root/$_lf" ]] || continue
    if [[ -n "$(cd "$_worktree_root" && git status --porcelain -- "$_lf" 2>/dev/null)" ]]; then
      DIRTY_LOCK_FILES+="$_lf"$'\n'
    fi
  done <<< "$LOCK_FILES"
  if [[ -n "$DIRTY_LOCK_FILES" ]]; then
    echo "version-check-gate.sh: lockfile(s) are modified on disk but not committed:" >&2
    while IFS= read -r _dlf; do
      [[ -z "$_dlf" ]] && continue
      echo "  $_dlf" >&2
    done <<< "$DIRTY_LOCK_FILES"
    echo "version-check-gate.sh: BLOCKER: a version bump regenerated Cargo.lock/mcp-loom/package-lock.json on disk but the lockfile change(s) were never committed -- pushing now would land the version-bearing files at the new version while their lockfiles stay stale." >&2
    echo "version-check-gate.sh: Fix: git add -- <lockfile(s) above> && git commit (these are not part of 'scripts/version.sh list' and must be staged explicitly -- 'git add \$(./scripts/version.sh list)' alone will skip them), $FIX_HINT" >&2
    exit 1
  fi
fi

exit 0
