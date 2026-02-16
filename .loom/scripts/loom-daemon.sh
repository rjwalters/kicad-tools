#!/usr/bin/env bash
# Wrapper to invoke Python daemon with proper environment
#
# This script provides the entry point for /loom command, routing to
# the Python-based daemon implementation (loom-tools package).
#
# Benefits over LLM-interpreted skill:
#   - Deterministic event loop behavior
#   - No context accumulation (each iteration is independent)
#   - Direct subprocess spawning (no Claude CLI round-trips for iteration)
#   - Handles PYTHONPATH setup for both pip-installed and source installs
#   - Maps CLI flags for user-facing parity (--merge/-m → --force/-f)
#
# Usage:
#   ./.loom/scripts/loom-daemon.sh [options]
#
# Options (user-facing):
#   --force, -f      Enable force mode (auto-promote proposals, auto-merge)
#   --merge, -m      Alias for --force (for CLI parity with /loom --merge)
#   --timeout-min N, -t N  Stop daemon after N minutes (0 = no timeout)
#   --debug, -d      Enable debug logging
#   --status         Check if daemon is running
#   --health         Show daemon health status
#
# See loom.md and CLAUDE.md for full documentation.

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
elif [[ -f "$REPO_ROOT/.loom/install-metadata.json" ]]; then
    # Fallback: loom-source-path missing, try install-metadata.json
    LOOM_SOURCE="$(sed -n 's/.*"loom_source" *: *"\(.*\)".*/\1/p' "$REPO_ROOT/.loom/install-metadata.json")"
    if [[ -n "$LOOM_SOURCE" ]] && [[ -d "$LOOM_SOURCE/loom-tools" ]]; then
        LOOM_TOOLS="$LOOM_SOURCE/loom-tools"
        # Recreate loom-source-path for future runs
        echo "$LOOM_SOURCE" > "$REPO_ROOT/.loom/loom-source-path"
    else
        LOOM_TOOLS=""
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
    echo "[ERROR] Cannot run daemon: repository has unmerged files:" >&2
    echo "$unmerged" | sed 's/^/  /' >&2
    echo "Resolve merge conflicts before running daemon." >&2
    exit 1
fi

# Try Python implementation first
# Priority order:
#   1. Virtual environment in loom-tools (from source or recorded path)
#   2. System-installed loom-daemon (pip install)
#   3. Error with helpful message

if [[ -n "$LOOM_TOOLS" ]] && [[ -x "$LOOM_TOOLS/.venv/bin/loom-daemon" ]]; then
    # Use venv from loom-tools directory
    exec "$LOOM_TOOLS/.venv/bin/loom-daemon" ${args[@]+"${args[@]}"}
elif command -v loom-daemon &>/dev/null; then
    # System-installed - verify loom_tools can be imported
    if ! python3 -c "import loom_tools" 2>/dev/null; then
        echo "[ERROR] loom-daemon is installed but cannot import loom_tools." >&2
        echo "" >&2
        echo "  This usually means the editable install source directory was deleted" >&2
        echo "  (e.g., a worktree was removed while loom-tools was pip install -e'd from it)." >&2
        echo "" >&2
        echo "  To fix:" >&2
        echo "    1. Reinstall loom-tools: pip install loom-tools" >&2
        echo "    2. Or reinstall from source: pip install -e /path/to/loom/loom-tools" >&2
        echo "" >&2
        exit 1
    fi
    exec loom-daemon ${args[@]+"${args[@]}"}
else
    echo "[ERROR] Python daemon not available." >&2
    echo "" >&2
    if [[ -z "$LOOM_TOOLS" ]]; then
        echo "  loom-tools directory not found and no loom-source-path recorded." >&2
        echo "  If this is a target repository, re-run Loom installation." >&2
    elif [[ ! -d "$LOOM_TOOLS/.venv" ]]; then
        echo "  Virtual environment not found in: $LOOM_TOOLS/.venv" >&2
        echo "  Run: $LOOM_TOOLS/../scripts/install/setup-python-tools.sh --loom-root $(dirname "$LOOM_TOOLS")" >&2
    else
        echo "  loom-daemon not found in: $LOOM_TOOLS/.venv/bin/" >&2
        echo "  Run: $LOOM_TOOLS/.venv/bin/pip install -e $LOOM_TOOLS" >&2
    fi
    echo "" >&2
    echo "  Or install system-wide: pip install loom-tools" >&2
    exit 1
fi
