#!/usr/bin/env bash
# check-verified-corrections-preserved.sh - Diff-before-rewrite guard for the
# Curator's append-only "## Verified corrections" convention (#4135).
#
# Why: Curator's inter-phase handoff is prose that gets rewritten in place. A
# re-curation pass that regenerates an issue body wholesale can silently drop
# a *verified* finding from an earlier pass in favor of a merely *plausible*
# one -- and the loss leaves no trace in the artifact the next agent reads.
# This happened on #4042: a first Curator pass verified three corrections
# against a live host and recorded them; a second pass rewrote the body and
# dropped all three, asserting the opposite of verified fact. The corrections
# survived only in an earlier *comment*, which is not what a Builder reads
# first. See `.loom/roles/curator.md` -> "Verified Corrections Are
# Append-Only" for the full convention this script enforces.
#
# What it checks: every paragraph under the OLD body's `## Verified
# corrections` heading (case-insensitive; up to the next `## ` heading or
# EOF) must still be present, verbatim after whitespace normalization,
# somewhere under the NEW body's `## Verified corrections` section. New
# paragraphs may freely be appended -- this only detects paragraphs that
# would be *lost*, exactly the "append-only" invariant.
#
# Usage:
#   check-verified-corrections-preserved.sh <old-body-file> <new-body-file>
#
# Exit codes:
#   0 - nothing lost (including the common case: OLD has no such section)
#   1 - a verified-corrections paragraph present in OLD is missing from NEW
#   2 - usage error (missing/unreadable arguments)
#
# This is a general-purpose diff tool, not forge-specific -- callers (a
# Curator re-curation pass, or a test suite) supply plain text files
# containing the OLD and proposed NEW issue body.

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $(basename "$0") <old-body-file> <new-body-file>" >&2
  exit 2
fi

OLD_FILE="$1"
NEW_FILE="$2"

for f in "$OLD_FILE" "$NEW_FILE"; do
  if [[ ! -f "$f" ]]; then
    echo "check-verified-corrections-preserved: file not found: $f" >&2
    exit 2
  fi
done

# Extract the body of a "## Verified corrections" section (case-insensitive
# heading match), i.e. everything after the heading line up to the next `## `
# heading or EOF. Prints nothing if the heading is absent.
extract_section() {
  awk '
    BEGIN { insec = 0 }
    {
      line = $0
      lower = tolower(line)
    }
    insec && lower ~ /^##[ \t]/ { insec = 0 }
    lower ~ /^##[ \t]+verified corrections[ \t]*$/ { insec = 1; next }
    insec { print line }
  ' "$1"
}

# Normalize a section's text into one whitespace-collapsed paragraph per
# record, separated by the ASCII Record Separator (0x1e) so downstream
# comparisons never collide with ordinary text content. Blank lines
# (paragraph breaks) delimit records via awk's RS="".
normalize_paragraphs() {
  awk -v RS='' -v ORS=$'\x1e' '
    {
      gsub(/\r/, "");
      gsub(/\n/, " ");
      gsub(/[ \t]+/, " ");
      sub(/^ /, "");
      sub(/ $/, "");
      if (length($0) > 0) print
    }
  '
}

OLD_SECTION="$(extract_section "$OLD_FILE")"

if [[ -z "${OLD_SECTION// /}" ]]; then
  echo "check-verified-corrections-preserved: OK — old body has no '## Verified corrections' section (nothing to preserve)."
  exit 0
fi

NEW_SECTION="$(extract_section "$NEW_FILE")"

if [[ -z "${NEW_SECTION// /}" ]]; then
  echo "check-verified-corrections-preserved: FAIL — old body has a '## Verified corrections' section but the new body has none at all. A re-curation pass may append to this section but must never remove it." >&2
  exit 1
fi

OLD_PARAS="$(printf '%s' "$OLD_SECTION" | normalize_paragraphs)"
NEW_PARAS="$(printf '%s' "$NEW_SECTION" | normalize_paragraphs)"

MISSING=0
while IFS= read -r -d $'\x1e' para; do
  [[ -z "$para" ]] && continue
  # Exact-match a normalized old paragraph against the set of normalized new
  # paragraphs. grep -F/-x avoids both regex-metacharacter surprises in
  # issue text and accidental substring matches across paragraph boundaries.
  # NOTE: the delimiter passed to `tr` must be bash's ANSI-C-quoted $'\x1e'
  # (an actual 0x1E byte), not the single-quoted string '\x1e' -- BSD tr
  # (macOS) does not support \xHH escapes and would instead treat '\x1e' as
  # the three literal characters x/1/e, silently mangling every "e" in the
  # text.
  if ! printf '%s' "$NEW_PARAS" | tr $'\x1e' '\n' | grep -qFx -- "$para"; then
    MISSING=$((MISSING + 1))
    echo "check-verified-corrections-preserved: missing verified-corrections paragraph:" >&2
    echo "  $para" >&2
  fi
done <<< "${OLD_PARAS}"$'\x1e'

if [[ "$MISSING" -gt 0 ]]; then
  echo "check-verified-corrections-preserved: FAIL — $MISSING verified-corrections paragraph(s) from the old body are missing from the new body. Append-only means these may never be deleted or rewritten — restore them verbatim (a disagreement is a new dated paragraph, not a replacement)." >&2
  exit 1
fi

echo "check-verified-corrections-preserved: OK — all verified-corrections paragraphs from the old body are preserved in the new body."
exit 0
