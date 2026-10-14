#!/usr/bin/env bash
# check-dependabot-labels.sh - Fail if a package manifest is not covered by a
# labeled `.github/dependabot.yml` entry (#7577).
#
# `labels:` in dependabot.yml is scoped to the ONE `updates:` entry it appears
# in, so a manifest with no matching entry gets Dependabot's DEFAULT labels,
# never `loom:review-requested`, and its PRs sit in the Judge's unlabeled
# fallback queue, which cannot clear them (the #5455 livelock).
#
# A thin wrapper: the checks live in `loom-daemon check-dependabot-labels`
# (loom-daemon/src/dependabot_labels.rs, which documents them). This script is
# `contract` shell, which the shell-budget ratchet only lets shrink.
#
# Usage:
#   check-dependabot-labels.sh [ROOT]
#     ROOT  Repository root containing .github/dependabot.yml. Defaults to
#           `git rev-parse --show-toplevel`, then the script's own repo root.
#           No .github/dependabot.yml is a clean no-op.
#
# With no loom-daemon resolvable (or one predating the subcommand) the check
# FAILS under GitHub Actions and is skipped with a note elsewhere.
#
# Exit codes: 0 = ok (or nothing to check); 1 = a coverage/label problem.
#
# requires-daemon: check-dependabot-labels optional   a daemon without the subcommand (or none) is skipped with a note outside CI; under GitHub Actions it FAILS instead (#11828)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -ge 1 && -n "${1:-}" ]]; then
  ROOT="$1"
elif ! ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
  ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi

if [[ ! -f "$ROOT/.github/dependabot.yml" ]]; then
  echo "check-dependabot-labels: no .github/dependabot.yml under $ROOT — nothing to check (ok)."
  exit 0
fi

# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
BIN="$(loom_resolve_self_daemon_bin 2>/dev/null || true)"
if [[ -n "$BIN" ]] && "$BIN" check-dependabot-labels --help >/dev/null 2>&1; then
  exec "$BIN" check-dependabot-labels --repo-root "$ROOT"
fi

if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
  echo "check-dependabot-labels: FAIL — no loom-daemon with \`check-dependabot-labels\` resolved; the check cannot be skipped in CI." >&2
  exit 1
fi
echo "check-dependabot-labels: note — no loom-daemon with \`check-dependabot-labels\` found; skipped." >&2
exit 0
