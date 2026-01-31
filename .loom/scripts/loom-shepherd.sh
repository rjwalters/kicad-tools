#!/usr/bin/env bash
# Wrapper to invoke Python shepherd with proper environment
#
# This script provides the entry point for /shepherd command, routing to
# the Python-based shepherd implementation (loom-tools package).
#
# Benefits over direct shell script:
#   - Handles PYTHONPATH setup for both pip-installed and source installs
#   - Maps CLI flags for user-facing parity (--merge/-m → --force/-f)
#   - Graceful fallback to shell script if Python unavailable
#   - Single change point for shepherd command routing
#
# Usage:
#   ./.loom/scripts/loom-shepherd.sh <issue-number> [options]
#
# Options (user-facing):
#   --merge, -m     Auto-approve, auto-merge after approval (maps to --force)
#   --to <phase>    Stop after specified phase (curated, pr, approved)
#   --from <phase>  Start from specified phase (curator, builder, judge, merge)
#   --task-id <id>  Use specific task ID
#
# See shepherd.md for full documentation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOOM_TOOLS="$REPO_ROOT/loom-tools"

# Map --merge/-m to --force/-f for CLI parity
# The Python CLI uses --force internally, but users expect --merge per documentation
args=()
for arg in "$@"; do
    case "$arg" in
        --merge|-m)
            args+=("--force")
            ;;
        *)
            args+=("$arg")
            ;;
    esac
done

# Pre-flight: check for unmerged files (merge conflicts)
# Without this check, conflict markers in Python files cause confusing SyntaxError messages
unmerged=$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null | grep '^UU' | cut -c4- || true)
if [[ -n "$unmerged" ]]; then
    echo "[ERROR] Cannot run shepherd: repository has unmerged files:" >&2
    echo "$unmerged" | sed 's/^/  /' >&2
    echo "Resolve merge conflicts before running shepherd." >&2
    exit 1
fi

# Try Python implementation first
# Priority order:
#   1. Virtual environment in loom-tools (development setup)
#   2. System-installed loom-shepherd (pip install)
#   3. Fallback to shell script (transition period)

if [[ -x "$LOOM_TOOLS/.venv/bin/loom-shepherd" ]]; then
    # Development setup: use venv directly
    exec "$LOOM_TOOLS/.venv/bin/loom-shepherd" "${args[@]}"
elif command -v loom-shepherd &>/dev/null; then
    # System-installed
    exec loom-shepherd "${args[@]}"
else
    echo "[ERROR] Python shepherd not available. Install loom-tools:" >&2
    echo "  cd loom-tools && pip install -e ." >&2
    exit 1
fi
