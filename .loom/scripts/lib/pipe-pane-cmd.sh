#!/bin/bash
# pipe-pane-cmd.sh - Shared pipe-pane command with ANSI escape sequence stripping
#
# Provides a single canonical definition of the pipe-pane filter command
# used across all shell-based tmux pipe-pane call sites.
#
# The sed command removes:
#   - Standard ANSI escape sequences: ESC[...letter (colors, cursor, modes)
#   - Terminal mode queries: ESC[?...h/l (like ?2026h/l)
#   - OSC sequences: ESC]...BEL (title setting, etc.)
#   - Carriage returns (\r) from TUI line rewriting
#   - Backspaces (\x08) from cursor corrections
#   - Bare escape sequences (ESC not followed by [ or ]) from raw cursor movement
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/pipe-pane-cmd.sh"
#   tmux pipe-pane -t "$session" "$(pipe_pane_cmd "$log_file")"

# Build the pipe-pane filter command for a given log file path.
# Arguments:
#   $1 - Path to the log file (will be single-quoted in output)
# Output:
#   The sed command string suitable for passing to tmux pipe-pane
pipe_pane_cmd() {
    local log_file="$1"
    if [[ -z "$log_file" ]]; then
        echo "pipe_pane_cmd: log_file argument required" >&2
        return 1
    fi
    echo "sed -E 's/\\x1b\\[[?0-9;]*[a-zA-Z]//g; s/\\x1b\\][^\\x07]*\\x07//g; s/\\r//g; s/\\x08//g; s/\\x1b[^][]//g' >> '${log_file}'"
}
