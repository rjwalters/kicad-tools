#!/usr/bin/env bash
# check-dependabot-labels.sh - Fail if a package manifest is not covered by a
# labeled `.github/dependabot.yml` entry (#7577).
#
# Why: `labels:` in dependabot.yml is scoped to the ONE `updates:` entry it
# appears in. Dependabot matches every PR it opens — version updates *and*
# security updates, which are driven by the repo's Dependabot alerts rather than
# by dependabot.yml — against the entry whose `package-ecosystem` + `directory`
# contain the manifest being patched. A manifest with no matching entry gets
# Dependabot's DEFAULT labels (`dependencies` + the language label) instead, so
# it never receives `loom:review-requested` and lands straight in the Judge's
# unlabeled-PR fallback queue — which applies no labels and therefore cannot
# clear it (the #5455 livelock).
#
# That is not hypothetical: #7474, #7473, #7396, #7395, #7138, #7137 and #6890
# (bumps inside `/mcp-loom` and `/dashboard/web`) all sat open and unreviewed
# for weeks because the root `directory: "/"` entry does not cover nested
# manifests. This check is the structural tie that stops it recurring the next
# time a package is added somewhere in the tree.
#
# Checks performed:
#   1. Every `updates:` entry lists `loom:review-requested` in its `labels:`.
#   2. Every tracked npm manifest that has a committed lockfile beside it is
#      covered by an npm entry for its directory.
#   3. Every npm entry points at a directory that actually contains a tracked
#      `package.json` (catches an entry left behind by a moved/deleted package).
#   4. Advisory only (never fails): a tracked manifest that declares
#      dependencies, has no committed lockfile, and has no entry. These are
#      scaffolds/templates (e.g. `quickstarts/`) that are deliberately not on
#      the weekly update cadence; the warning exists so the decision stays
#      visible rather than silent.
#
# Usage:
#   check-dependabot-labels.sh [ROOT]
#     ROOT  Repository root containing .github/dependabot.yml. Defaults to
#           `git rev-parse --show-toplevel`, then the script's own repo root.
#           If <ROOT>/.github/dependabot.yml does not exist, the check is a
#           clean no-op (an installed downstream repo need not use Dependabot).
#
# Exit codes: 0 = ok (or nothing to check); 1 = a coverage/label problem.

set -euo pipefail

# --- Resolve ROOT -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -ge 1 && -n "${1:-}" ]]; then
  ROOT="$1"
else
  if ! ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    # defaults/scripts/ -> defaults/ -> repo root
    ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
  fi
fi

CONFIG="$ROOT/.github/dependabot.yml"

if [[ ! -f "$CONFIG" ]]; then
  echo "check-dependabot-labels: no .github/dependabot.yml under $ROOT — nothing to check (ok)."
  exit 0
fi

REQUIRED_LABEL="loom:review-requested"
FAILURES=0

# --- Parse dependabot.yml into "<ecosystem>\t<directory>\t<label,label>" -----
# The file is a flat, conventional block-style list; this parser deliberately
# only understands that shape and fails loudly (via the field assertions below)
# rather than silently mis-reading an exotic reformat.
parse_entries() {
  awk '
    function strip(s) {
      gsub(/^[ \t]+|[ \t]+$/, "", s)
      gsub(/^"|"$/, "", s)
      return s
    }
    /^[ \t]*#/ { next }
    /^  - package-ecosystem:/ {
      if (seen) print eco "\t" dir "\t" labels
      seen = 1
      idx = index($0, ":")
      eco = strip(substr($0, idx + 1))
      dir = ""; labels = ""; inlabels = 0
      next
    }
    /^    directory:/ {
      idx = index($0, ":")
      dir = strip(substr($0, idx + 1))
      inlabels = 0
      next
    }
    /^    labels:[ \t]*$/ { inlabels = 1; next }
    /^      - / {
      if (inlabels) {
        item = $0
        sub(/^      - /, "", item)
        labels = labels (labels == "" ? "" : ",") strip(item)
      }
      next
    }
    /^    [A-Za-z]/ { inlabels = 0; next }
    END { if (seen) print eco "\t" dir "\t" labels }
  ' "$CONFIG"
}

ENTRIES="$(parse_entries)"

if [[ -z "$ENTRIES" ]]; then
  echo "ERROR: parsed zero entries from $CONFIG — the file shape changed, update this check." >&2
  exit 1
fi

# --- Check 1: every entry carries the review label --------------------------
while IFS=$'\t' read -r eco dir labels; do
  [[ -z "$eco" ]] && continue
  if [[ -z "$dir" ]]; then
    echo "ERROR: $CONFIG entry '$eco' has no 'directory:' (or it could not be parsed)." >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi
  if [[ ",$labels," != *",$REQUIRED_LABEL,"* ]]; then
    echo "ERROR: $CONFIG entry '$eco' (directory '$dir') does not list '$REQUIRED_LABEL' in labels:." >&2
    echo "       Without it, Dependabot PRs for that directory land in the Judge's unlabeled fallback queue (#5455/#7577)." >&2
    FAILURES=$((FAILURES + 1))
  fi
done <<< "$ENTRIES"

# --- Collect the npm entry directories --------------------------------------
NPM_DIRS="$(awk -F'\t' '$1 == "npm" { print $2 }' <<< "$ENTRIES")"

has_npm_entry() {
  local want="$1" d
  while read -r d; do
    [[ "$d" == "$want" ]] && return 0
  done <<< "$NPM_DIRS"
  return 1
}

# Directory of a manifest, expressed the way dependabot.yml spells it
# ("/" for the repo root, "/mcp-loom" for mcp-loom/package.json).
manifest_dir() {
  local path="$1" dir
  dir="$(dirname "$path")"
  [[ "$dir" == "." ]] && { echo "/"; return; }
  echo "/$dir"
}

# --- Checks 2 and 4: every manifest is covered (or knowingly not) -----------
MANIFESTS="$(git -C "$ROOT" ls-files -- 'package.json' '*/package.json' 2>/dev/null || true)"

while read -r manifest; do
  [[ -z "$manifest" ]] && continue
  dir="$(dirname "$manifest")"
  want="$(manifest_dir "$manifest")"

  has_lockfile=0
  for lock in package-lock.json pnpm-lock.yaml yarn.lock npm-shrinkwrap.json; do
    if [[ -f "$ROOT/$dir/$lock" ]]; then
      has_lockfile=1
      break
    fi
  done

  if has_npm_entry "$want"; then
    continue
  fi

  if [[ "$has_lockfile" -eq 1 ]]; then
    echo "ERROR: $manifest has a committed lockfile but no npm entry for directory \"$want\" in $CONFIG." >&2
    echo "       Add one (with the same labels: block) or Dependabot will open unlabeled, inert PRs for it (#7577)." >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi

  # Advisory: dependency-declaring manifest that is deliberately off the
  # update cadence (scaffolds/templates). Never fails the build.
  if command -v jq >/dev/null 2>&1; then
    dep_count="$(jq '[(.dependencies // {}), (.devDependencies // {})] | map(length) | add' "$ROOT/$manifest" 2>/dev/null || echo 0)"
    if [[ "${dep_count:-0}" -gt 0 ]]; then
      echo "note: $manifest declares dependencies and has no dependabot entry (no lockfile — treated as a scaffold, not an error)."
    fi
  fi
done <<< "$MANIFESTS"

# --- Check 3: no npm entry points at a directory with no manifest -----------
while read -r dir; do
  [[ -z "$dir" ]] && continue
  if [[ "$dir" == "/" ]]; then
    candidate="$ROOT/package.json"
  else
    candidate="$ROOT${dir}/package.json"
  fi
  if [[ ! -f "$candidate" ]]; then
    echo "ERROR: $CONFIG has an npm entry for directory \"$dir\" but no package.json exists there." >&2
    FAILURES=$((FAILURES + 1))
  fi
done <<< "$NPM_DIRS"

# --- Verdict ----------------------------------------------------------------
if [[ "$FAILURES" -gt 0 ]]; then
  echo "" >&2
  echo "check-dependabot-labels: $FAILURES problem(s) found in $CONFIG." >&2
  exit 1
fi

echo "check-dependabot-labels: all dependabot.yml entries carry '$REQUIRED_LABEL' and every lockfile-bearing manifest is covered (ok)."
exit 0
