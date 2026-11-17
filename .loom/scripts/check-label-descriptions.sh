#!/usr/bin/env bash
# check-label-descriptions.sh - Fail if any label description in the registry
# exceeds GitHub's 100-character limit.
#
# Why (#4335): sync-labels.sh pushes each label's `description` from
# `labels.yml` verbatim to the GitHub API, which rejects descriptions over 100
# characters with HTTP 422 on BOTH create and edit. Five descriptions had
# silently drifted over the limit (`loom:issue`, `loom:reviewing`,
# `loom:curated`, `external`, `loom:triage`), so every consumer sync left those
# labels stale — the create path 422'd on fresh repos, and the edit path 422'd
# everywhere else. There was no automated tie between the registry and the API
# limit, so the drift accumulated until a consumer install surfaced it. This
# lint is that tie: the registry is the enforced source of truth, and truncation
# is deliberately NOT added to sync-labels.sh (silent truncation would hide
# exactly the drift this check exists to catch).
#
# Registry format: the descriptions are one-line quoted YAML values, e.g.
#   description: "Approved for work. Applied by: humans ..."
# so a full YAML parser is unnecessary — a line regex over both registry copies
# (kept byte-identical by check-labels-drift.sh) is sufficient, matching the
# dependency-free bash approach of the sibling checks.
#
# GitHub counts CHARACTERS, not bytes (a multi-byte em-dash counts as one), so
# the length is measured with `${#var}` on a UTF-8 locale rather than byte size.
#
# Usage:
#   check-label-descriptions.sh [ROOT]
#     ROOT  Repository root containing defaults/ and .github/labels.yml.
#           Defaults to `git rev-parse --show-toplevel`, then the script's own
#           repo root. If <ROOT>/defaults does not exist (e.g. an installed
#           downstream repo with no source tree), it falls back to whatever
#           labels.yml copies are present; if none exist it is a clean no-op.
#
# Exit codes: 0 = all descriptions within the limit (or nothing to check);
# 1 = one or more descriptions exceed 100 characters (offenders named on stderr).

set -euo pipefail

readonly MAX_LEN=100

# --- Resolve ROOT -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -ge 1 && -n "${1:-}" ]]; then
  ROOT="$1"
else
  if ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    :
  else
    # defaults/scripts/ -> defaults/ -> repo root
    ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
  fi
fi

# --- Collect the registry copies to check -----------------------------------
LABELS_FILES=()
[[ -f "$ROOT/.github/labels.yml" ]] && LABELS_FILES+=("$ROOT/.github/labels.yml")
[[ -f "$ROOT/defaults/.github/labels.yml" ]] && LABELS_FILES+=("$ROOT/defaults/.github/labels.yml")

if [[ ${#LABELS_FILES[@]} -eq 0 ]]; then
  echo "check-label-descriptions: no labels.yml found under $ROOT — nothing to check (ok)."
  exit 0
fi

# --- Scan every label block for an over-length description ------------------
# labels.yml is a flat list of `- name: <label>` blocks, each followed by a
# `description:` line. Track the most recent `name:` so an offending
# description can be attributed to its label.
too_long=0

for file in "${LABELS_FILES[@]}"; do
  current_label=""
  lineno=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))

    # Capture the label name for the block we're in.
    if [[ "$line" =~ ^-[[:space:]]+name:[[:space:]]*(.*)$ ]]; then
      current_label="${BASH_REMATCH[1]}"
      # Strip surrounding quotes and trailing inline comment/whitespace.
      current_label="${current_label%%#*}"
      current_label="${current_label#\"}"; current_label="${current_label%\"}"
      current_label="${current_label#\'}"; current_label="${current_label%\'}"
      # trim trailing whitespace
      current_label="${current_label%"${current_label##*[![:space:]]}"}"
      continue
    fi

    # Only description lines matter.
    [[ "$line" =~ ^[[:space:]]*description:[[:space:]] ]] || continue

    # Extract the description value after `description:`.
    desc="${line#*description:}"
    # trim leading whitespace
    desc="${desc#"${desc%%[![:space:]]*}"}"
    # Strip one layer of surrounding quotes if present.
    if [[ "$desc" == \"*\" ]]; then
      desc="${desc#\"}"; desc="${desc%\"}"
    elif [[ "$desc" == \'*\' ]]; then
      desc="${desc#\'}"; desc="${desc%\'}"
    fi

    if (( ${#desc} > MAX_LEN )); then
      echo "OVER LIMIT (${#desc} > ${MAX_LEN}): ${current_label:-<unknown>}" >&2
      echo "  at ${file#"$ROOT"/}:$lineno" >&2
      echo "  description: $desc" >&2
      too_long=1
    fi
  done < "$file"
done

if [[ "$too_long" -ne 0 ]]; then
  echo "" >&2
  echo "check-label-descriptions: FAIL — one or more label descriptions exceed" >&2
  echo "GitHub's ${MAX_LEN}-character limit. sync-labels.sh will 422 on both create" >&2
  echo "and edit for these labels, silently leaving them stale on every repo." >&2
  echo "Shorten the description(s) above in BOTH .github/labels.yml and" >&2
  echo "defaults/.github/labels.yml (they are kept byte-identical), preserving the" >&2
  echo "'Applied by:' clause, then re-run this check." >&2
  exit 1
fi

echo "check-label-descriptions: OK — all label descriptions are within ${MAX_LEN} characters."
exit 0
