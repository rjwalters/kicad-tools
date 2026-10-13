#!/usr/bin/env bash
# Thin wrapper — delegates to the Loom CLI (.loom/bin/loom).
# Kept for backwards compatibility with the legacy ./loom.sh entry point.
#
# The historical daemon.sh was removed in #3432; the working agent-pool
# surface is `.loom/bin/loom start|status|stop` (tmux pool). This wrapper
# maps the legacy flags onto those subcommands.

# Find the repository root by looking for the .loom directory (handles
# worktrees and the symlinked defaults/scripts source layout).
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        if [[ -f "$dir/.git" ]]; then
            local gitdir
            gitdir=$(sed 's/^gitdir: //' "$dir/.git")
            local main_repo
            main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
            if [[ -d "$main_repo/.loom" ]]; then
                echo "$main_repo"
                return 0
            fi
        fi
        dir="$(dirname "$dir")"
    done
    echo ""
}

REPO_ROOT=$(find_repo_root)
LOOM_BIN="$REPO_ROOT/.loom/bin/loom"

if [[ -z "$REPO_ROOT" || ! -x "$LOOM_BIN" ]]; then
    echo "Error: Loom CLI not found (expected at .loom/bin/loom)" >&2
    echo "Is Loom installed in this repository?" >&2
    exit 1
fi

# Map legacy flags to loom subcommands
for arg in "$@"; do
    case "$arg" in
        --status) exec "$LOOM_BIN" status;;
        --stop)   exec "$LOOM_BIN" stop;;
    esac
done

# Headless (#11721): the raw daemon needs no tmux. A leading --headless, or a
# missing tmux, delegates to loom-daemon-start.sh (backgrounds the daemon).
if [[ "${1:-}" == --headless ]] || ! command -v tmux >/dev/null 2>&1; then
    [[ "${1:-}" == --headless ]] && shift || echo "start-daemon.sh: no tmux; starting headless" >&2
    exec "$REPO_ROOT/.loom/scripts/cli/loom-daemon-start.sh" "$@"
fi
exec "$LOOM_BIN" start "$@"
