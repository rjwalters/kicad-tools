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
    echo "[ERROR] Cannot run shepherd: repository has unmerged files:" >&2
    echo "$unmerged" | sed 's/^/  /' >&2
    echo "Resolve merge conflicts before running shepherd." >&2
    exit 1
fi

# Disable Python output buffering so stderr reaches callers immediately.
# Without this, output is lost when invoked non-interactively (e.g., Claude Code
# Bash tool, Task agents) because Python buffers stderr and may not flush before exit.
export PYTHONUNBUFFERED=1

# Set up log file for reliable output capture.
# Shepherd output is teed to this file to ensure it's always accessible, even
# when invoked with 2>&1 redirect in contexts where the capture buffer may be
# dropped (e.g., Claude Code Bash tool for long-running processes). The log path
# is printed immediately so it's visible even if subsequent output is invisible.
LOG_DIR="$REPO_ROOT/.loom/logs"
mkdir -p "$LOG_DIR"
SHEPHERD_LOG="$LOG_DIR/loom-shepherd-issue-${1:-unknown}.log"
printf '[INFO] Shepherd log: %s\n' "$SHEPHERD_LOG" >&2

# Try Python implementation first
# Priority order:
#   1. Virtual environment in loom-tools (from source or recorded path)
#   2. System-installed loom-shepherd (pip install)
#   3. Error with helpful message

if [[ -n "$LOOM_TOOLS" ]] && [[ -x "$LOOM_TOOLS/.venv/bin/loom-shepherd" ]]; then
    # Use venv from loom-tools directory
    # Note: intentionally NOT using exec. exec replaces the shell process,
    # which causes output to be invisible in some CLI tool contexts (e.g.,
    # Claude Code Bash tool) because the tool loses its output capture handle
    # on the replaced process. Running as a child preserves output capture
    # while set -e ensures exit code propagation.
    #
    # Output is teed to the log file (2>&1 merges stderr→stdout first so
    # both streams land in the log). We disable set -e temporarily so the
    # pipeline failure doesn't abort the script before we can capture
    # PIPESTATUS[0]. Using || true would overwrite PIPESTATUS, so we use
    # set +e/set -e instead.
    set +e
    "$LOOM_TOOLS/.venv/bin/loom-shepherd" "${args[@]}" 2>&1 | tee -a "$SHEPHERD_LOG"
    _exit="${PIPESTATUS[0]}"
    set -e
    exit "$_exit"
elif command -v loom-shepherd &>/dev/null; then
    # System-installed (same rationale as above — no exec)
    set +e
    loom-shepherd "${args[@]}" 2>&1 | tee -a "$SHEPHERD_LOG"
    _exit="${PIPESTATUS[0]}"
    set -e
    exit "$_exit"
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
