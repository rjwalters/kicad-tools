#!/usr/bin/env bash
# body-repo-affinity.sh - Detection backstop for cross-repo issue-body
# corruption (issue #6771, deferred from #6714's filing-lock).
#
# #6714 shipped a fleet-wide lock that PREVENTS concurrent Architects filing
# into different repos from racing on issue-body content. This script is the
# independent backstop that would CATCH it if the lock ever has a gap: on
# 2026-08-08 two Architects filing 5-issue bursts into different repos raced,
# and one repo's issue bodies got silently overwritten with another repo's
# content (titles untouched) -- undetected for 13 days until a human ran
# `git grep -i modexp` and got zero hits. Nothing had looked before that.
#
# Heuristic (deliberately simple, local-only, warn-only):
#   1. Extract candidate repo-local identifiers from the body -- backtick-
#      quoted paths/filenames/module-or-crate names, and bare filenames with
#      a recognizable extension mentioned inline outside backticks.
#   2. Check whether each candidate resolves in the target repo, using only
#      local `git` (tracked-path match, basename glob match anywhere in the
#      tree, or a plain content grep) -- zero network calls.
#   3. If the body cites SEVERAL such identifiers (>= --min-candidates,
#      default 3) and NONE of them resolve, print a warning and exit 1.
#      Otherwise exit 0.
#
# The >= 3-candidates-with-zero-hits threshold exists specifically to
# tolerate the common false positive of a legitimately forward-looking issue
# that proposes files which do not exist yet -- a single unresolved path (or
# a body that also references something that already exists) does not warn.
#
# This script NEVER blocks filing on its own: it is a detector, not a gate.
# Callers (e.g. create-issue.sh) MUST treat any exit code from this script as
# advisory only and must not let it change their own exit status.
#
# Usage:
#   body-repo-affinity.sh --body TEXT --repo-root PATH [--min-candidates N]
#   body-repo-affinity.sh --body-file PATH --repo-root PATH [--min-candidates N]
#   body-repo-affinity.sh --body-file - --repo-root PATH   (read body from stdin)
#   body-repo-affinity.sh --help
#
# Exit codes:
#   0 - Not flagged (fewer than --min-candidates cited identifiers, or at
#       least one of them resolves in --repo-root).
#   1 - Flagged: >= --min-candidates cited identifiers, none resolve.
#   2 - Invalid arguments, or --repo-root is not a git repository.
#
# Output: on flag (exit 1), a one-paragraph warning is printed to stderr
# naming the unresolved candidates and the repo they were checked against.
# Nothing is printed on a clean pass (exit 0) unless --verbose is given.

set -uo pipefail

usage() {
  sed -n '2,42p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
}

BODY=""
BODY_FILE=""
REPO_ROOT=""
MIN_CANDIDATES=3
VERBOSE=false
HAVE_BODY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help | -h)
      usage
      exit 0
      ;;
    --body)
      BODY="${2:-}"
      HAVE_BODY=true
      shift 2
      ;;
    --body-file)
      BODY_FILE="${2:-}"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT="${2:-}"
      shift 2
      ;;
    --min-candidates)
      MIN_CANDIDATES="${2:-}"
      shift 2
      ;;
    --verbose)
      VERBOSE=true
      shift
      ;;
    *)
      echo "body-repo-affinity.sh: unknown argument: $1" >&2
      echo "Run 'body-repo-affinity.sh --help' for usage." >&2
      exit 2
      ;;
  esac
done

if [[ -n "$BODY_FILE" ]] && [[ "$HAVE_BODY" == "true" ]]; then
  echo "body-repo-affinity.sh: --body and --body-file are mutually exclusive" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]]; then
  if [[ "$BODY_FILE" == "-" ]]; then
    BODY="$(cat)"
  elif [[ -r "$BODY_FILE" ]]; then
    BODY="$(cat "$BODY_FILE")"
  else
    echo "body-repo-affinity.sh: cannot read --body-file: $BODY_FILE" >&2
    exit 2
  fi
fi

if [[ -z "$REPO_ROOT" ]]; then
  echo "body-repo-affinity.sh: --repo-root is required" >&2
  exit 2
fi

if ! [[ "$MIN_CANDIDATES" =~ ^[0-9]+$ ]]; then
  echo "body-repo-affinity.sh: --min-candidates must be a non-negative integer" >&2
  exit 2
fi

if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  echo "body-repo-affinity.sh: --repo-root is not a git repository: $REPO_ROOT" >&2
  exit 2
fi

# Common abbreviations that match the bare "word.letters" extension shape but
# are never repo-local identifiers -- kept short and lower-cased; matched
# case-insensitively against each extracted candidate.
_BRA_BLOCKLIST=" e.g i.e etc vs mr mrs ms dr jr sr inc ltd co no fig approx resp cf viz jan feb mar apr jun jul aug sep oct nov dec "

# Extract candidate repo-local identifiers from the body text (arg 1),
# one per line, deduplicated. Two shapes are extracted:
#   (a) backtick-quoted tokens with no whitespace, e.g. `rtl/modexp.v`,
#       `spec/modexp.md`, `sky130_fd_sc_hd` -- covers quoted paths, bare
#       filenames, and module/crate-style identifiers alike.
#   (b) bare (non-backtick) filename-shaped tokens with a recognizable
#       extension mentioned inline, e.g. src/main.py -- the extension must
#       start with a letter, which both requires *something* file-extension-
#       shaped and incidentally excludes dotted version numbers (0.18.137).
_bra_extract_candidates() {
  local body="$1"
  {
    # POSIX ERE has no \K or lookahead, so the backtick-quoted shape is
    # matched (and the delimiters kept) with `-oE`, then stripped below --
    # the interior charset excludes backtick, so each match is naturally
    # bounded by its own pair of delimiters. `-P` is a GNU-only extension
    # unsupported by the BSD grep shipped on macOS (#6784).
    printf '%s' "$body" | grep -oE '`[A-Za-z0-9][A-Za-z0-9_./-]{2,}`' || true
    printf '%s' "$body" | grep -oE '\b[A-Za-z0-9_][A-Za-z0-9_/-]*\.[A-Za-z][A-Za-z0-9]{0,7}\b' || true
  } | while IFS= read -r tok; do
    tok="${tok#\`}"
    tok="${tok%\`}"
    # `${tok,,}` (bash 4+) is unavailable under macOS's stock /bin/bash 3.2;
    # `tr` lowercases portably on both GNU and BSD userlands.
    local lower=" $(printf '%s' "$tok" | tr '[:upper:]' '[:lower:]') "
    if [[ "$_BRA_BLOCKLIST" == *"$lower"* ]]; then
      continue
    fi
    printf '%s\n' "$tok"
  done | sort -u
}

# Return 0 if $2 (a candidate identifier) resolves against $1 (a repo root)
# via local git only; 1 otherwise.
_bra_resolves_in_repo() {
  local repo_root="$1" token="$2" base

  # 1. Exact tracked-path match.
  if git -C "$repo_root" ls-files --error-unmatch -- "$token" >/dev/null 2>&1; then
    return 0
  fi

  # 2. Basename glob match anywhere in the tree (handles a path cited with a
  #    different leading directory, and bare filenames).
  base="${token##*/}"
  if [[ -n "$base" ]] && git -C "$repo_root" ls-files -- ":(glob)**/$base" 2>/dev/null | grep -q .; then
    return 0
  fi

  # 3. Plain content grep (covers module/crate names and identifiers that
  #    appear in source without being a path themselves).
  if git -C "$repo_root" grep -I -F -q -- "$token" -- . 2>/dev/null; then
    return 0
  fi

  return 1
}

main() {
  local -a candidates=()
  local tok
  while IFS= read -r tok; do
    [[ -n "$tok" ]] && candidates+=("$tok")
  done < <(_bra_extract_candidates "$BODY")

  if [[ "${#candidates[@]}" -lt "$MIN_CANDIDATES" ]]; then
    "$VERBOSE" && echo "body-repo-affinity: ${#candidates[@]} candidate identifier(s) (< $MIN_CANDIDATES) -- not evaluated." >&2
    exit 0
  fi

  local -a unresolved=()
  local resolved_count=0
  for tok in "${candidates[@]}"; do
    if _bra_resolves_in_repo "$REPO_ROOT" "$tok"; then
      resolved_count=$((resolved_count + 1))
    else
      unresolved+=("$tok")
    fi
  done

  if [[ "$resolved_count" -gt 0 ]]; then
    "$VERBOSE" && echo "body-repo-affinity: ${resolved_count}/${#candidates[@]} candidate identifier(s) resolved in $REPO_ROOT -- OK." >&2
    exit 0
  fi

  local joined
  joined="$(printf '%s, ' "${candidates[@]}")"
  joined="${joined%, }"
  echo "body-repo-affinity: WARNING: this issue body cites ${#candidates[@]} repo-local identifier(s) ($joined) but NONE of them exist in $REPO_ROOT -- possible cross-repo body mismatch (issue filed with another repo's content, see #6771). This is advisory only; filing proceeds unaffected." >&2
  exit 1
}

main
