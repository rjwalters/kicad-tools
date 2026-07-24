#!/usr/bin/env bash
# check-phantom-labels.sh - Fail if a role prompt applies a label that does not
# exist in the label registry (`.github/labels.yml`).
#
# Why (#3786): role-prompt markdown/JSON files are authored independently over
# time and drift from `.github/labels.yml`. When a prompt instructs an agent to
# run `gh issue edit --add-label "loom:does-not-exist"`, the command fails at
# runtime (gh rejects nonexistent labels) or — worse, given the repo policy of
# never minting new labels — silently defeats the intended coordination. There
# was no automated tie between prompt content and the label registry, so the
# drift accumulated until an agent hit a live failure. This lint is that tie.
#
# What it scans: `<root>/defaults/**/*.md` and `<root>/defaults/**/*.json`
# (regular files only — the `defaults/roles/*.md` symlinks into
# `defaults/.claude/commands/loom/` are skipped so each file is scanned once).
#
# What it flags: a `loom:<token>` that appears in a LABEL-APPLICATION context —
# a line containing `--add-label`, `--remove-label`, or `--label` — and is NOT
# present in the label registry. This deliberately targets the operationally
# dangerous case (a gh label-mutating command that would fail), and structurally
# excludes:
#   - slash-command names like `/loom:sweep` (a `/`-prefixed token, never a label)
#   - prose that merely discusses a label name without an application flag
#   - HTML-comment markers such as `<!-- loom:complexity=complex -->`
# so the false positive noted during #3786 curation (`` `loom:sweep` ``, the
# `/loom:sweep` command) does not trip the check.
#
# Registry: the UNION of label names in `<root>/.github/labels.yml` and
# `<root>/defaults/.github/labels.yml`. A label defined in either file is real,
# and a phantom label is absent from BOTH. As of #3896 the two files are kept
# BYTE-IDENTICAL (enforced by defaults/scripts/check-labels-drift.sh), so the
# union now equals either file on its own; it is retained here defensively so
# this lint stays correct even if the two copies transiently diverge.
#
# Usage:
#   check-phantom-labels.sh [ROOT]
#     ROOT  Repository root containing defaults/ and .github/labels.yml.
#           Defaults to `git rev-parse --show-toplevel`, then the script's
#           own repo root. If <ROOT>/defaults does not exist (e.g. an installed
#           downstream repo with no source tree), the check is a clean no-op.
#
# Exit codes: 0 = no phantom labels (or nothing to scan); 1 = phantom label(s)
# found (details printed to stderr).

set -euo pipefail

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

DEFAULTS_DIR="$ROOT/defaults"
if [[ ! -d "$DEFAULTS_DIR" ]]; then
  # Not a Loom source tree (e.g. an installed repo). Nothing to lint.
  echo "check-phantom-labels: no defaults/ under $ROOT — nothing to scan (ok)."
  exit 0
fi

# --- Build the label registry (union of both labels.yml files) --------------
LABELS_FILES=()
[[ -f "$ROOT/.github/labels.yml" ]] && LABELS_FILES+=("$ROOT/.github/labels.yml")
[[ -f "$DEFAULTS_DIR/.github/labels.yml" ]] && LABELS_FILES+=("$DEFAULTS_DIR/.github/labels.yml")

if [[ ${#LABELS_FILES[@]} -eq 0 ]]; then
  echo "check-phantom-labels: no labels.yml found under $ROOT — cannot validate." >&2
  exit 1
fi

DEFINED="$(grep -hoE '^- name: loom:[A-Za-z0-9_-]+' "${LABELS_FILES[@]}" \
  | sed 's/^- name: //' | sort -u)"

is_defined() {
  # Exact whole-line match against the defined set.
  grep -qxF "$1" <<<"$DEFINED"
}

# --- Scan role prompts for label-application-context loom: tokens -----------
# Marker = a label-mutating flag on the line. `--add-label` contains `add-label`
# and `--remove-label` contains `remove-label`, so the three bare alternatives
# cover the `--`-prefixed forms too.
MARKER='add-label|remove-label|--label'

phantom_found=0

while IFS= read -r -d '' file; do
  # grep marker lines with line numbers; tolerate files with no matches.
  while IFS= read -r line; do
    lineno="${line%%:*}"
    content="${line#*:}"
    # Extract loom:<token> occurrences, capturing one leading char so we can
    # reject slash-command names (`/loom:sweep`). ERE has no lookbehind.
    while IFS= read -r hit; do
      [[ -z "$hit" ]] && continue
      prefix="${hit%loom:*}"     # everything before the final loom:
      token="loom:${hit##*loom:}"
      # Skip slash-command names: /loom:<name>
      [[ "$prefix" == *"/" ]] && continue
      if ! is_defined "$token"; then
        echo "PHANTOM LABEL: $token" >&2
        echo "  at ${file#"$ROOT"/}:$lineno" >&2
        echo "  line: $(echo "$content" | sed 's/^[[:space:]]*//')" >&2
        phantom_found=1
      fi
    done < <(grep -oE '(^|[^/])loom:[A-Za-z0-9_-]+' <<<"$content" || true)
  done < <(grep -nE "$MARKER" "$file" || true)
done < <(find "$DEFAULTS_DIR" -type f \( -name '*.md' -o -name '*.json' \) -print0)

if [[ "$phantom_found" -ne 0 ]]; then
  echo "" >&2
  echo "check-phantom-labels: FAIL — role prompt(s) apply label(s) absent from the" >&2
  echo "label registry (.github/labels.yml). Either fix the prompt to use a real" >&2
  echo "label or add the label to .github/labels.yml (repo policy: never mint" >&2
  echo "labels casually — see .github/labels.yml and CLAUDE.md)." >&2
  exit 1
fi

echo "check-phantom-labels: OK — all applied labels exist in the registry."
exit 0
