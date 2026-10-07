#!/usr/bin/env bash
# check-labels-drift.sh - Fail if the two label registries drift apart.
# requires-daemon: labels optional        probes `labels --help`; degrades to the plain diff check when absent (fatal under GITHUB_ACTIONS)
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
# #10013: both copies are GENERATED from defaults/labels.json. When a
# loom-daemon binary is available, also fail if the Loom block differs from the
# registry (a label added only to labels.yml, or an edit that skipped
# `loom-daemon labels generate --write`). Resolution goes through
# loom_resolve_self_daemon_bin ($LOOM_DAEMON_SELF_BIN, then this checkout's
# target/{release,debug} build, then PATH), so CI's downloaded
# target/debug/loom-daemon is found without being on PATH. The Rust
# label_registry tests do NOT cover this at PR time (backend-tests is not
# path-triggered by labels.yml/labels.json), so under GitHub Actions a missing
# or `labels`-less binary is a hard failure, never a silent skip (PR #10053).
if [[ -f "$ROOT/defaults/labels.json" ]]; then
  # shellcheck source=lib/locate-daemon-bin.sh
  source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
  LABELS_BIN="$(loom_resolve_self_daemon_bin 2>/dev/null || true)"
  if [[ -n "$LABELS_BIN" ]] && "$LABELS_BIN" labels --help >/dev/null 2>&1; then
    echo "check-labels-drift: registry check via $LABELS_BIN"
    if ! "$LABELS_BIN" labels check --root "$ROOT" >&2; then
      echo "check-labels-drift: FAIL — labels.yml differs from defaults/labels.json." >&2
      exit 1
    fi
  elif [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "check-labels-drift: FAIL — no loom-daemon with \`labels\` resolved; the registry check cannot be skipped in CI." >&2
    exit 1
  else
    echo "check-labels-drift: note — no loom-daemon with \`labels\` found; skipped the defaults/labels.json comparison." >&2
  fi
fi

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
