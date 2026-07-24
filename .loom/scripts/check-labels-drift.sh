#!/usr/bin/env bash
# check-labels-drift.sh - Fail if the two label registries drift apart.
#
# Why (#3896): Loom ships the label registry in TWO places —
#   - <root>/.github/labels.yml          (this repo's live label registry)
#   - <root>/defaults/.github/labels.yml (the installer template copied into
#                                         fresh installs)
# These are supposed to describe the same label set: a fresh install must ship
# exactly the labels Loom itself uses. But they are two hand-edited files with
# no automated tie, so they silently drifted — `loom:auditor`,
# `loom:auditor-capability-request`, `loom:merge-conflict`, `loom:auto-merge-ok`,
# `loom:ci-failure`, `loom:abort`, `loom:operator-only` were all present in the
# root copy but missing from the defaults template, and many descriptions had
# diverged. A fresh install then shipped a label set that differed from the
# source repo's. This check is the tie that prevents recurrence.
#
# Source-of-truth decision (documented in the header of both labels.yml files):
# the two files are kept BYTE-IDENTICAL. There are NO intentional differences —
# so the drift check is a plain `diff`. Edit one file, mirror the change to the
# other. If a future need arises for the template to legitimately differ from the
# repo copy, that is an explicit design change: update this check (and the parity
# note in both file headers) in the same PR that introduces the divergence.
#
# Usage:
#   check-labels-drift.sh [ROOT]
#     ROOT  Repository root containing defaults/ and .github/labels.yml.
#           Defaults to `git rev-parse --show-toplevel`, then the script's own
#           repo root. If <ROOT>/defaults does not exist (e.g. an installed
#           downstream repo with no source tree), the check is a clean no-op.
#
# Exit codes: 0 = files identical (or nothing to check); 1 = drift detected
# (unified diff printed to stderr).

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

ROOT_LABELS="$ROOT/.github/labels.yml"
DEFAULTS_LABELS="$ROOT/defaults/.github/labels.yml"

# --- Skip when there is nothing to compare ----------------------------------
if [[ ! -d "$ROOT/defaults" ]]; then
  # Not a Loom source tree (e.g. an installed repo). Nothing to check.
  echo "check-labels-drift: no defaults/ under $ROOT — nothing to check (ok)."
  exit 0
fi

if [[ ! -f "$ROOT_LABELS" ]]; then
  echo "check-labels-drift: missing $ROOT_LABELS — cannot compare." >&2
  exit 1
fi

if [[ ! -f "$DEFAULTS_LABELS" ]]; then
  echo "check-labels-drift: missing $DEFAULTS_LABELS — cannot compare." >&2
  exit 1
fi

# --- Compare (must be byte-identical) ---------------------------------------
if diff -u "$ROOT_LABELS" "$DEFAULTS_LABELS" >/dev/null 2>&1; then
  echo "check-labels-drift: OK — .github/labels.yml and defaults/.github/labels.yml are identical."
  exit 0
fi

echo "check-labels-drift: FAIL — the two label registries have drifted:" >&2
echo "  A: ${ROOT_LABELS#"$ROOT"/}" >&2
echo "  B: ${DEFAULTS_LABELS#"$ROOT"/}" >&2
echo "" >&2
# --label makes the unified-diff header name each side clearly.
diff -u --label "a/.github/labels.yml" --label "b/defaults/.github/labels.yml" \
  "$ROOT_LABELS" "$DEFAULTS_LABELS" >&2 || true
echo "" >&2
echo "These files must be BYTE-IDENTICAL (see the parity-contract header in each" >&2
echo "labels.yml). Reconcile them — edit one and mirror the change to the other —" >&2
echo "then re-run this check." >&2
exit 1
