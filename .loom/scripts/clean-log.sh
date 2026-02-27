#!/bin/bash
# clean-log.sh - Strip terminal rendering noise from agent log files
#
# Agent logs captured via tmux pipe-pane contain massive amounts of
# terminal rendering artifacts (spinner characters, animation text,
# partial word fragments from redraws, permission banners, etc.).
# This script produces a cleaned version that preserves only the
# meaningful content: wrapper log lines, tool calls, agent output,
# checkpoint saves, and test results.
#
# Delegates to loom_tools.log_filter --file for the actual filtering,
# which handles ANSI escape sequence stripping, Claude Code TUI noise
# removal, and blank line collapsing.
#
# Usage:
#   ./clean-log.sh <logfile>              # Write cleaned version to <logfile>.clean
#   ./clean-log.sh <logfile> -o <output>  # Write cleaned version to <output>
#   ./clean-log.sh <logfile> --in-place   # Overwrite the original file
#   ./clean-log.sh <logfile> --stdout     # Print to stdout
#
# The original log file is preserved by default (cleaned version gets
# a .clean suffix).  Use --in-place to overwrite the original.
#
# Environment Variables:
#   LOOM_CLEAN_LOG_KEEP_RAW=1  - Skip cleaning (no-op, for debugging)

set -euo pipefail

usage() {
    echo "Usage: $0 <logfile> [--in-place | --stdout | -o <output>]"
    echo ""
    echo "Strip terminal rendering noise from agent log files."
    echo ""
    echo "Options:"
    echo "  --in-place   Overwrite the original file"
    echo "  --stdout     Print cleaned output to stdout"
    echo "  -o <file>    Write cleaned output to <file>"
    echo "  (default)    Write to <logfile>.clean"
    exit 1
}

if [[ $# -lt 1 || "$1" == "--help" || "$1" == "-h" ]]; then
    usage
fi

INPUT_FILE="$1"
shift

if [[ ! -f "$INPUT_FILE" ]]; then
    echo "Error: File not found: $INPUT_FILE" >&2
    exit 1
fi

# Parse output mode
OUTPUT_MODE="suffix"  # default: write to <input>.clean
OUTPUT_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --in-place)
            OUTPUT_MODE="inplace"
            shift
            ;;
        --stdout)
            OUTPUT_MODE="stdout"
            shift
            ;;
        -o)
            OUTPUT_MODE="file"
            OUTPUT_FILE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            ;;
    esac
done

# No-op escape hatch for debugging
if [[ "${LOOM_CLEAN_LOG_KEEP_RAW:-}" == "1" ]]; then
    if [[ "$OUTPUT_MODE" == "stdout" ]]; then
        cat "$INPUT_FILE"
    fi
    exit 0
fi

# Delegate to the Python module for actual filtering
clean_log() {
    python3 -m loom_tools.log_filter --file "$INPUT_FILE"
}

case "$OUTPUT_MODE" in
    stdout)
        clean_log
        ;;
    inplace)
        tmp=$(mktemp "${INPUT_FILE}.tmp.XXXXXX")
        clean_log > "$tmp"
        mv "$tmp" "$INPUT_FILE"
        ;;
    file)
        clean_log > "$OUTPUT_FILE"
        ;;
    suffix)
        clean_log > "${INPUT_FILE}.clean"
        ;;
esac
