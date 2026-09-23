#!/usr/bin/env bash
# Strip ANSI escape sequences and clean terminal output from stdin.
#
# Thin stub over the native `loom-daemon strip-ansi` subcommand (issue #4275,
# epic #4081 Phase 3 family 5 — the native port of the retired
# `loom_tools.log_filter`). Handles cursor sequences, spinner animations,
# Claude Code TUI banners and other redraw artifacts.
#
# Usage:
#   cat .loom/logs/loom-builder-issue-42.log | ./.loom/scripts/strip-ansi.sh
#   ./.loom/scripts/strip-ansi.sh < .loom/logs/loom-builder-issue-42.log
#   ./.loom/scripts/strip-ansi.sh --file .loom/logs/loom-builder-issue-42.log
#
# With no arguments this is a real-time stdin -> stdout filter (safe for tmux
# `pipe-pane`); `--file` deep-cleans a captured log, which additionally strips
# short redraw debris — a heuristic that needs whole-file context and would
# eat legitimate short lines from a live stream (issue #2798).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# requires-daemon: strip-ansi >= 0.17.0   #4275/#4552 — the Rust port landed in 3f147e8f0 (2026-07-29), AFTER v0.16.0 was tagged (57cfec3aa) and while the workspace version still read 0.16.0, so 0.16.0 is the last version WITHOUT it; the next bump, 0.17.0 (3e5a9439e), is the first that shipped it. Hard, not `optional`: this stub only execs, it never probes or degrades. Default LOOM_SCRIPT_HELPER_MISSING_RC (1) is left alone deliberately — `loom-daemon strip-ansi` is a filter that exits 0 or fails; it has no data exit code at all (a downstream broken pipe is swallowed as success), so the refusal cannot be mistaken for an answer (#8484).
loom_exec_script_helper strip-ansi "$@"
