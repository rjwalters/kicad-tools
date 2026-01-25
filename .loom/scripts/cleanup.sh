#!/usr/bin/env bash
# Loom Cleanup Script - WRAPPER (backwards compatibility)
#
# This script is a thin wrapper around defaults/scripts/clean.sh for backwards compatibility.
# The unified clean.sh now handles all cleanup functionality.
#
# AGENT USAGE INSTRUCTIONS:
#   Non-interactive mode (for Claude Code):
#     ./scripts/cleanup.sh --yes
#     ./scripts/cleanup.sh -y
#
#   Interactive mode (prompts for confirmation):
#     ./scripts/cleanup.sh
#
# DEPRECATED: Use defaults/scripts/clean.sh instead:
#   ./defaults/scripts/clean.sh --deep --force   # Equivalent to cleanup.sh --yes
#
# What this wrapper does:
#   Maps cleanup.sh arguments to clean.sh equivalents
#   Always includes --deep (build artifacts)

set -euo pipefail

# Find script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Map arguments
ARGS=("--deep")  # cleanup.sh always cleans build artifacts

for arg in "$@"; do
  case $arg in
    -y|--yes)
      ARGS+=("--force")
      ;;
    --help|-h)
      cat <<EOF
Loom Cleanup Script - DEPRECATED WRAPPER

This script is a thin wrapper around clean.sh for backwards compatibility.
Please use defaults/scripts/clean.sh directly for more options.

Usage: ./scripts/cleanup.sh [options]

Options:
  -y, --yes    Non-interactive mode (auto-confirm all prompts)
  -h, --help   Show this help message

What it does:
  1. Removes target/ directory (Rust build artifacts)
  2. Removes node_modules/ directory (Node dependencies)
  3. Detects worktrees for closed issues and offers to remove them
  4. Prunes orphaned git worktrees

Equivalent clean.sh commands:
  ./scripts/cleanup.sh           ->  ./defaults/scripts/clean.sh --deep
  ./scripts/cleanup.sh --yes     ->  ./defaults/scripts/clean.sh --deep --force

After running, restore dependencies with: pnpm install
EOF
      exit 0
      ;;
    *)
      ARGS+=("$arg")
      ;;
  esac
done

# Call the unified clean.sh from defaults/scripts/
exec "$PROJECT_ROOT/defaults/scripts/clean.sh" "${ARGS[@]}"
