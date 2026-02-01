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

# Find loom-tools directory
# Priority:
#   1. Local loom-tools in repo (for Loom source repository development)
#   2. Loom source path recorded during installation (for target repositories)
if [[ -d "$REPO_ROOT/loom-tools" ]]; then
    # Running in Loom source repository
    LOOM_TOOLS="$REPO_ROOT/loom-tools"
elif [[ -f "$REPO_ROOT/.loom/loom-source-path" ]]; then
    # Running in target repository with Loom installed
    LOOM_SOURCE="$(cat "$REPO_ROOT/.loom/loom-source-path")"
    if [[ -d "$LOOM_SOURCE/loom-tools" ]]; then
        LOOM_TOOLS="$LOOM_SOURCE/loom-tools"
    else
        echo "[ERROR] Loom source directory not found: $LOOM_SOURCE" >&2
        echo "  The recorded Loom source path may be invalid." >&2
        echo "  Re-run Loom installation or fix .loom/loom-source-path" >&2
        exit 1
    fi
else
    # Neither found - will fall back to system-installed or error
    LOOM_TOOLS=""
fi

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
#   1. Virtual environment in loom-tools (from source or recorded path)
#   2. System-installed loom-shepherd (pip install)
#   3. Error with helpful message

if [[ -n "$LOOM_TOOLS" ]] && [[ -x "$LOOM_TOOLS/.venv/bin/loom-shepherd" ]]; then
    # Use venv from loom-tools directory
    exec "$LOOM_TOOLS/.venv/bin/loom-shepherd" "${args[@]}"
elif command -v loom-shepherd &>/dev/null; then
    # System-installed
    exec loom-shepherd "${args[@]}"
else
    echo "[ERROR] Python shepherd not available." >&2
    echo "" >&2
    if [[ -z "$LOOM_TOOLS" ]]; then
        echo "  loom-tools directory not found and no loom-source-path recorded." >&2
        echo "  If this is a target repository, re-run Loom installation." >&2
    elif [[ ! -d "$LOOM_TOOLS/.venv" ]]; then
        echo "  Virtual environment not found in: $LOOM_TOOLS/.venv" >&2
        echo "  Run: $LOOM_TOOLS/../scripts/install/setup-python-tools.sh --loom-root $(dirname "$LOOM_TOOLS")" >&2
    else
        echo "  loom-shepherd not found in: $LOOM_TOOLS/.venv/bin/" >&2
        echo "  Run: $LOOM_TOOLS/.venv/bin/pip install -e $LOOM_TOOLS" >&2
    fi
    echo "" >&2
    echo "  Or install system-wide: pip install loom-tools" >&2
    exit 1
fi
