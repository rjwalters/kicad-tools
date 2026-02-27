#!/usr/bin/env bash
# Strip ANSI escape sequences and clean terminal output from stdin.
# Delegates to Python loom_tools.log_filter for robust handling of
# cursor sequences, spinner animations, and other TUI artifacts.
#
# Usage:
#   cat .loom/logs/loom-builder-issue-42.log | ./.loom/scripts/strip-ansi.sh
#   ./.loom/scripts/strip-ansi.sh < .loom/logs/loom-builder-issue-42.log
exec python3 -m loom_tools.log_filter
