#!/bin/bash
# probe-tokens.sh - Probe each bootstrapped OAuth account and write .ranking.
#
# Usage:
#   ./.loom/scripts/probe-tokens.sh              # Probe + print human table
#   ./.loom/scripts/probe-tokens.sh --ranking    # Probe + write .loom/tokens/.ranking
#   ./.loom/scripts/probe-tokens.sh --json       # Probe + emit JSON to stdout
#
# Exit codes:
#   0 - At least one account was probed (results may be mixed)
#   1 - Every probe failed, or the tokens directory is missing
#
# Cron example (every 10 minutes):
#   */10 * * * * cd /path/to/repo && ./.loom/scripts/probe-tokens.sh --ranking >> .loom/logs/probe-tokens.log 2>&1
#
# Thin wrapper around the loom-tokens Python CLI.  Modeled on
# .loom/scripts/check-usage.sh — keeps the bash surface tiny so the real
# logic lives in loom-tools.

if command -v loom-tokens &>/dev/null; then
    loom-tokens check "$@"
    exit $?
fi

python3 -m loom_tools.cli.loom_tokens check "$@"
