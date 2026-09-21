#!/usr/bin/env bash
# check-defaults-version-bump.sh - Fail a PR that changes the watched
# surface (defaults/ by default) without either bumping VERSION or
# declaring an explicit no-surface-change marker (#5874, #6480).
#
# Why this exists: every file under defaults/ is copied into every
# consumer's installed .loom/{scripts,hooks,roles,docs,bin}/ +
# .claude/commands/loom/ surfaces at install time -- and is NOT refreshed by
# a `git pull` (that drift is what resync-installed.sh exists to remediate,
# per #3777). The only mechanical signal a consumer has that its installed
# copies are behind is a VERSION comparison: install.sh's own currency check,
# /repo:update-tools, and a fleet's `fleet-resync.sh --dry-run` all key off
# it. If a PR changes defaults/ without bumping VERSION, that signal silently
# lies -- exactly what happened when PR #5846 changed five role prompts plus
# a doc without touching VERSION: 23 fleet repos all reported
# "v0.18.0 -> v0.18.0" (current) while 59-85 installed surfaces per repo were
# actually stale.
#
# This is deliberately NOT trying to force semantic-version inflation on
# every doc typo or test-only edit under defaults/ -- an explicit marker lets
# an author declare "this change does not alter installed behavior" without a
# version bump.
#
# Consumer-repo use case (#6480): any repo shaped like Loom -- one that ships
# its own installable surface via an install.sh that copies files into other
# repos, keying its own currency checks off its own VERSION (e.g.
# rjwalters/repo's Repo Skills, rjwalters/anvil, rjwalters/squad) -- can
# reuse this exact script to gate its own VERSION on its own surface. Point
# --paths (or VERSION_BUMP_WATCH_PATHS) at that repo's installable paths
# instead of defaults/; everything else (the VERSION file name, marker text,
# exit codes) stays the same.
#
# Usage:
#   check-defaults-version-bump.sh --base <ref> [--head <ref>]
#                                   [--paths <p1> [<p2>...]]
#     --base <ref>   Git ref/sha to diff FROM (the PR's base commit, e.g. a
#                     fetched base sha, or origin/main for a local check).
#                     Required.
#     --head <ref>   Git ref/sha to diff TO. Defaults to HEAD.
#     --paths <p1> [<p2>...]
#                     One or more pathspecs to watch (repeatable -- each
#                     --paths flag may take multiple path arguments, and the
#                     flag itself may be passed more than once; all given
#                     paths accumulate). Defaults to `defaults/` so Loom's
#                     own CI job and every existing caller are unaffected.
#                     Also settable via the VERSION_BUMP_WATCH_PATHS
#                     environment variable (a whitespace-separated list),
#                     which --paths overrides when both are given.
#     --forbid-bump   Inverted mode (#7743): FAIL if this PR's own commits
#                     change the extracted version VALUE of any version-bearing file
#                     (package.json, mcp-loom/package.json, Cargo.toml,
#                     CLAUDE.md, VERSION -- the same set scripts/version.sh
#                     manages); PASS otherwise, regardless of --paths /
#                     VERSION_BUMP_WATCH_PATHS (this mode does not gate on a
#                     watched path at all -- it gates on the version value
#                     itself, which is a property of the diff alone). Off by
#                     default so every existing caller -- Loom's own
#                     historical usage and every #6480 consumer-repo reuse --
#                     is byte-for-byte unchanged; only Loom's own
#                     `defaults-version-bump-check` CI job (.github/workflows/
#                     ci.yml) passes it. See "Inverted mode" below for why:
#                     version bumps now happen exactly once, automatically,
#                     in .github/workflows/version-bump-on-merge.yml -- a PR
#                     that hand-edits a version value is either duplicating
#                     that (harmless but pointless) or racing it (actively
#                     wrong, since the PR's value is stale the moment another
#                     defaults/-touching PR merges first).
#   check-defaults-version-bump.sh --help
#
# Inverted mode (--forbid-bump, #7743): the original mode above answers "did
# this PR bump VERSION", which #7743 documents as unsound in both directions
# (a bump anywhere in base..head satisfies it even when unrelated to this
# PR; a downgrade satisfies it too, since it only checks that VERSION
# *changed*, never that it increased) AND expensive even when sound --every
# concurrent defaults/-touching PR had to carry the same mechanical N-file
# diff and re-cut it whenever `main` moved. --forbid-bump asks a different,
# decidable-from-the-diff-alone question instead: "does this diff change a
# version-bearing file's VALUE at all" -- comparing each file's *extracted*
# version value between merge-base(--base, --head) and --head (not "did the
# raw file change", and NOT against --base directly: see the BASE_REF block
# in that code path for why base-branch drift would otherwise false-FAIL),
# so an unrelated CLAUDE.md prose edit that never touches the
# `**Loom Version**:` line still passes even though CLAUDE.md itself is in
# the diff. This mirrors scripts/version.sh's own get_version_from_file()
# extraction rules per file type rather than importing that script, so this
# file stays a single self-contained script (its own #6480 header contract
# is "reuse this exact script" -- not "reuse this script plus a sibling").
#
# No-surface-change marker: a PR whose body OR whose HEAD-reachable commit
# messages (between --base and --head) contain the literal string
#     <!-- loom:no-surface-change -->
# is exempt even when a watched path changed and VERSION did not. Pass the PR
# body via the PR_BODY environment variable (GitHub Actions:
# `env: PR_BODY: ${{ github.event.pull_request.body }}`); the commit-message
# path needs no extra plumbing beyond --base/--head.
#
# CAVEATS for the calling CI job (#6577 -- both bit a real PR, #6576):
#   - Commit-message path: `git log --format=%B "${BASE}..${HEAD}"` can only
#     see commits actually present in the local checkout. A default
#     `actions/checkout@v7` run on a `pull_request` event fetches a SHALLOW,
#     single-commit checkout of the synthetic `refs/pull/<N>/merge` ref --
#     whose own commit message is "Merge <head> into <base>", not any real
#     branch commit -- so a marker placed in a genuine commit message is
#     invisible even though `--base`/`--head` were passed correctly. The
#     caller MUST check out with enough history (`fetch-depth: 0`, or at
#     least deep enough to reach `--base`) AND pass an explicit `--head`
#     (e.g. `ref: ${{ github.event.pull_request.head.sha }}` +
#     `--head "${{ github.event.pull_request.head.sha }}"`) instead of
#     relying on the default merge-ref checkout and the `--head` default of
#     the literal string "HEAD".
#   - PR_BODY path: this env var is a snapshot of the PR body captured in
#     the webhook payload that triggered THIS workflow run. A body edit made
#     after the triggering push (e.g. `gh pr edit --body ...` with no new
#     commit) does not retrigger the workflow, so a marker added to the body
#     post-push is invisible to any already-queued or already-run check --
#     and re-running the same workflow run (`gh run rerun`) replays the
#     ORIGINAL stored event payload, so it does NOT pick up a live body
#     edit either. If you add the marker to the body only, after already
#     pushing, push a new commit (an empty one is fine, e.g.
#     `git commit --allow-empty -m 'trigger marker check'`) to retrigger
#     `synchronize` with a fresh payload -- or just put the marker in a
#     commit message from the start, which the commit-message path (once
#     the caller fetches sufficient history, as above) picks up reliably.
#
# Exit codes (default mode):
#   0 - nothing under the watched paths changed in the diff, OR VERSION was
#       also changed, OR the no-surface-change marker is present.
#   1 - a watched path changed, VERSION was not, and no marker is present.
#   2 - bad usage (missing/invalid --base or --head, or an unknown argument).
#
# Exit codes (--forbid-bump mode):
#   0 - no version-bearing file's extracted value differs between
#       merge-base(--base, --head) and --head.
#   1 - at least one version-bearing file's extracted value differs.
#   2 - bad usage (same as above).

set -euo pipefail

MARKER='<!-- loom:no-surface-change -->'

# The version-bearing files --forbid-bump extracts and compares values for.
# Intentionally the same set scripts/version.sh's VERSION_FILES manages
# (#7743) -- kept as a literal duplicate here, not sourced from that script,
# so this file remains a single self-contained script per its own #6480
# consumer-reuse header contract.
FORBID_BUMP_VALUE_FILES=(
  "package.json"
  "mcp-loom/package.json"
  "Cargo.toml"
  "CLAUDE.md"
  "VERSION"
)

BASE=""
HEAD="HEAD"
WATCH_PATHS=()
FORBID_BUMP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      BASE="${2:-}"
      shift 2
      ;;
    --head)
      HEAD="${2:-}"
      shift 2
      ;;
    --paths)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        WATCH_PATHS+=("$1")
        shift
      done
      ;;
    --forbid-bump)
      FORBID_BUMP=true
      shift
      ;;
    --help|-h)
      sed -n '2,135p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "check-defaults-version-bump: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$BASE" ]]; then
  echo "check-defaults-version-bump: --base <ref> is required." >&2
  exit 2
fi

if ! git rev-parse --verify --quiet "${BASE}^{commit}" >/dev/null; then
  echo "check-defaults-version-bump: base ref '$BASE' not found (not fetched?)." >&2
  exit 2
fi

if ! git rev-parse --verify --quiet "${HEAD}^{commit}" >/dev/null; then
  echo "check-defaults-version-bump: head ref '$HEAD' not found." >&2
  exit 2
fi

# --- --forbid-bump mode (#7743) ---------------------------------------------
#
# A separate code path, not a modifier on the default logic below: it asks a
# different question ("does this diff change a version-bearing file's VALUE
# at all", independent of --paths/VERSION_BUMP_WATCH_PATHS) rather than the
# default mode's ("did a watched path change without VERSION also changing").
if $FORBID_BUMP; then
  # Compare against merge-base($BASE, $HEAD), not $BASE itself (#7823 review).
  # CI wires --base to `github.event.pull_request.base.sha` -- the live tip of
  # the base branch at trigger time, NOT the point this branch diverged from
  # it. Those differ the moment ANY sibling PR merges after this branch was
  # cut, and under #7743 every defaults/-touching merge bumps the version, so
  # a raw $BASE..$HEAD value comparison reports a version DOWNGRADE (base's
  # new value -> head's older, untouched one) for a PR whose own commits never
  # touched a version-bearing file at all. That is a false FAIL caused purely
  # by base-branch drift -- the same "gate that is unsound" symptom class
  # #7743 set out to eliminate, and it fired on #7743's own PR (#7823).
  #
  # The merge-base is the last commit this branch and the base branch agree
  # on, so BASE_REF..$HEAD contains exactly this PR's own commits: base drift
  # becomes invisible, while a genuine hand-edit (which lives inside that
  # range) still fails. Idempotent when the caller already passes a merge-base
  # (builder-pr.md's local pre-flight does), since merge-base(mb, head) == mb.
  #
  # Falls back to the raw $BASE when no merge-base is resolvable -- a shallow
  # CI checkout whose two histories do not share enough depth (the same
  # ancestry caveat the default mode's direct two-ref diff documents below).
  # Callers that need this narrowing must therefore check out with enough
  # history (`fetch-depth: 0`), as .github/workflows/ci.yml's
  # defaults-version-bump-check job does.
  BASE_REF="$BASE"
  if MERGE_BASE="$(git merge-base "$BASE" "$HEAD" 2>/dev/null)" && [[ -n "$MERGE_BASE" ]]; then
    BASE_REF="$MERGE_BASE"
  fi

  # Extracts file:path's version VALUE at git ref:ref, or prints nothing if
  # the file doesn't exist at that ref (so a file added/removed between base
  # and head is treated as "no value" on the missing side, not an error) or
  # its content doesn't parse (e.g. a malformed/non-JSON package.json --
  # this check only cares about VALUE changes, not validating file shape).
  # Mirrors scripts/version.sh's get_version_from_file() case-by-case, kept
  # as a literal duplicate per the header comment above FORBID_BUMP_VALUE_FILES.
  extract_version_value() {
    local ref="$1" file="$2" content
    content="$(git show "${ref}:${file}" 2>/dev/null || true)"
    [[ -z "$content" ]] && return 0
    case "$file" in
      *.json)
        jq -r '.version // empty' <<<"$content" 2>/dev/null || true
        ;;
      *.toml)
        grep -m1 '^version' <<<"$content" | sed 's/version = "\(.*\)"/\1/' || true
        ;;
      CLAUDE.md)
        grep -o 'Loom Version\*\*: [0-9]*\.[0-9]*\.[0-9]*' <<<"$content" | grep -o '[0-9]*\.[0-9]*\.[0-9]*' || true
        ;;
      VERSION)
        tr -d '[:space:]' <<<"$content"
        ;;
    esac
  }

  CHANGED_VALUES=""
  for vf in "${FORBID_BUMP_VALUE_FILES[@]}"; do
    base_val="$(extract_version_value "$BASE_REF" "$vf")"
    head_val="$(extract_version_value "$HEAD" "$vf")"
    if [[ "$base_val" != "$head_val" ]]; then
      CHANGED_VALUES+="  $vf: '$base_val' -> '$head_val'"$'\n'
    fi
  done

  if [[ -z "$CHANGED_VALUES" ]]; then
    echo "check-defaults-version-bump: OK — no version-bearing file's value changed in this diff (compared against $BASE_REF)."
    exit 0
  fi

  echo "check-defaults-version-bump: FAIL — this diff hand-edits a version-bearing file's value" >&2
  echo "(comparing $BASE_REF..$HEAD, i.e. this PR's own commits):" >&2
  echo "" >&2
  printf '%s' "$CHANGED_VALUES" >&2
  echo "" >&2
  echo "Version bumps now happen exactly once, automatically, in" >&2
  echo ".github/workflows/version-bump-on-merge.yml after this PR merges (#7743)." >&2
  echo "A feature PR must not touch a version-bearing file's value -- revert the" >&2
  echo "change(s) above (the rest of your diff is unaffected)." >&2
  exit 1
fi

# --paths on the command line wins; otherwise fall back to the
# VERSION_BUMP_WATCH_PATHS env var (whitespace-separated); otherwise the
# original hardcoded default of defaults/, so every existing caller
# (Loom's own CI job included) is byte-for-byte unchanged.
if [[ ${#WATCH_PATHS[@]} -eq 0 ]]; then
  if [[ -n "${VERSION_BUMP_WATCH_PATHS:-}" ]]; then
    # shellcheck disable=SC2206  # intentional word-splitting on a
    # whitespace-separated path list
    WATCH_PATHS=($VERSION_BUMP_WATCH_PATHS)
  else
    WATCH_PATHS=("defaults/")
  fi
fi

WATCHED_DESC="${WATCH_PATHS[*]}"

# Deliberately a direct two-ref diff, not a merge-base-narrowed one -- this
# script is invoked from a shallow single-commit-fetch CI checkout where the
# base and head shallow histories may not share enough depth to resolve a
# merge-base. A direct diff answers the question this check actually cares
# about ("does applying head's changes touch a watched path without touching
# VERSION") without requiring ancestry.
CHANGED_FILES="$(git diff --name-only "$BASE" "$HEAD" -- "${WATCH_PATHS[@]}" 2>/dev/null || true)"

if [[ -z "$CHANGED_FILES" ]]; then
  echo "check-defaults-version-bump: OK — no changes under watched path(s) ($WATCHED_DESC) in this diff."
  exit 0
fi

VERSION_CHANGED="$(git diff --name-only "$BASE" "$HEAD" -- VERSION 2>/dev/null || true)"

if [[ -n "$VERSION_CHANGED" ]]; then
  echo "check-defaults-version-bump: OK — watched path(s) ($WATCHED_DESC) changed and VERSION was bumped."
  exit 0
fi

# --- no-surface-change marker check -----------------------------------------

if [[ -n "${PR_BODY:-}" ]] && grep -qF "$MARKER" <<<"$PR_BODY"; then
  echo "check-defaults-version-bump: OK — no-surface-change marker found in the PR body."
  exit 0
fi

if git log --format=%B "${BASE}..${HEAD}" 2>/dev/null | grep -qF "$MARKER"; then
  echo "check-defaults-version-bump: OK — no-surface-change marker found in a commit message."
  exit 0
fi

echo "check-defaults-version-bump: FAIL — watched path(s) ($WATCHED_DESC) changed without a VERSION bump:" >&2
echo "" >&2
echo "$CHANGED_FILES" | sed 's/^/  /' >&2
echo "" >&2
echo "Every file under defaults/ is copied into consumers' installed" >&2
echo ".loom/{scripts,hooks,roles,docs,bin}/ + .claude/commands/loom/ surfaces at" >&2
echo "install time -- NOT refreshed by a git pull (#3777). VERSION is the only" >&2
echo "mechanical signal consumers have that those copies are stale, so a" >&2
echo "watched-path change must eventually be paired with a bump (at minimum" >&2
echo "the patch component):" >&2
echo "    ./scripts/version.sh bump patch" >&2
echo "" >&2
echo "If you are a Builder on a feature PR: do NOT run that command yourself." >&2
echo ".github/workflows/version-bump-on-merge.yml (#7743) bumps VERSION exactly" >&2
echo "once, automatically, right after this PR merges -- and the separate" >&2
echo "--forbid-bump invocation of this same script (the one CI's PR gate job" >&2
echo "actually runs, #7823) FAILS if your diff hand-edits VERSION or any other" >&2
echo "version-bearing file. This default-mode FAIL is the merge-time /" >&2
echo "repo-maintainer gate (e.g. a #6480 consumer repo bumping its own VERSION" >&2
echo "directly on its own main), not per-PR guidance -- the two modes" >&2
echo "intentionally disagree about who should touch VERSION and when." >&2
echo "" >&2
echo "If this change genuinely does not alter installed behavior (e.g. a" >&2
echo "comment, a test-only edit, a typo fix), declare that explicitly instead" >&2
echo "of bumping VERSION -- add this exact marker to the PR body or to a commit" >&2
echo "message in this PR:" >&2
echo "    $MARKER" >&2
exit 1
