#!/bin/bash
# check-usage.sh - Query Claude API usage via Anthropic OAuth API
#
# Usage:
#   ./.loom/scripts/check-usage.sh           # Returns JSON with usage data
#   ./.loom/scripts/check-usage.sh --status  # Human-readable status
#
# Exit codes:
#   0 - Data returned successfully
#   1 - Token not found or API call failed
#
# Thin wrapper around the loom-usage Python CLI.  Reads the Claude Code
# OAuth token from the macOS Keychain and calls the Anthropic usage API.

if command -v loom-usage &>/dev/null; then
    # No exec — exec makes output invisible in CLI tool contexts
    # (e.g., Claude Code Bash tool). See loom-shepherd.sh for full rationale.
    loom-usage "$@"
    exit $?
fi

python3 -m loom_tools.common.usage "$@"
