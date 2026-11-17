#!/bin/bash
# post-worktree.sh -- REPO-OWNED kicad-tools project hook (issue #4558).
#
# NOTE for /repo tooling and auditors: unlike the guard-*.sh scripts next to
# it, this file is NOT vendored/installer-managed. It uses the documented
# Loom extension point (worktree.sh runs `.loom/hooks/post-worktree.sh` after
# creating a worktree, and Loom upgrades never overwrite this path). Do not
# flag or delete it as unmanaged Loom content.
#
# Why: worktree creation performs zero Python env setup, so a fresh
# worktree's .venv can drift from uv.lock (observed: mypy 1.20.2 vs the
# locked 1.19.1), producing spurious mypy-baseline noise that looks like a
# real regression. Syncing the lock-pinned dev environment here makes every
# local tool run match CI's `uv sync --frozen --extra dev` environment.
#
# Bounded execution (issue #5967): the sync runs non-interactively
# (stdin </dev/null, UV_NO_PROGRESS=1), with every step timed and logged to
# <worktree>/.loom/logs/post-worktree.log (gitignored), under a hard timeout
# (POST_WORKTREE_SYNC_TIMEOUT seconds, default 600). On timeout the sync is
# killed and the hook warns and exits 0 -- a usable worktree with a
# "run uv sync yourself" warning beats a hung setup. The timeout is a portable
# poll loop (macOS ships no `timeout(1)`), and no helper process outlives the
# hook.
#
# fd hygiene (issue #5967 root cause): worktree.sh runs `exec 3>&1`, so this
# hook inherits the CALLER's stdout as fd 3. Any long-lived descendant that
# keeps fd 3 open stops the caller's pipe from ever reaching EOF, so a
# `worktree.sh ... | tail` or a background agent tool call "never exits" even
# after worktree.sh itself has exited. Children started here therefore run with
# fds 3 and 9 closed.
#
# Args (passed by worktree.sh):
#   $1 - absolute path to the new worktree
#   $2 - branch name (e.g. feature/issue-42)   [unused]
#   $3 - issue number                          [unused]
#
# Env:
#   POST_WORKTREE_SYNC_TIMEOUT  hard cap on `uv sync`, in seconds (default
#                               600; a non-integer or 0 falls back to 600)
#
# This hook must never fail worktree creation: it warns and exits 0 on any
# problem (missing uv, failed sync, timeout).

set -u

WORKTREE_PATH="${1:-$PWD}"
SYNC_CMD=(uv sync --frozen --extra dev)
SYNC_CMD_TEXT="uv sync --frozen --extra dev"

SYNC_TIMEOUT_RAW="${POST_WORKTREE_SYNC_TIMEOUT:-600}"
# Accept 1-6 decimal digits (up to ~11.5 days). Longer values would wrap bash's
# 64-bit arithmetic. Normalise to base 10 before any arithmetic: bash reads
# "08"/"09" as invalid octal, which would make every timeout comparison error
# and the cap never fire.
if [[ "$SYNC_TIMEOUT_RAW" =~ ^[0-9]{1,6}$ ]] && ((10#$SYNC_TIMEOUT_RAW > 0)); then
    SYNC_TIMEOUT=$((10#$SYNC_TIMEOUT_RAW))
else
    echo "post-worktree hook: ignoring invalid POST_WORKTREE_SYNC_TIMEOUT='$SYNC_TIMEOUT_RAW'; using 600s." >&2
    SYNC_TIMEOUT=600
fi

LOG_DIR="$WORKTREE_PATH/.loom/logs"
LOG_FILE="$LOG_DIR/post-worktree.log"
if ! mkdir -p "$LOG_DIR" 2>/dev/null || ! : >>"$LOG_FILE" 2>/dev/null; then
    LOG_FILE=/dev/null
fi

HOOK_START=$SECONDS

log() {
    printf '%s post-worktree: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$LOG_FILE"
}

say() {
    echo "post-worktree hook: $*"
    log "$*"
}

warn() {
    echo "post-worktree hook: $*" >&2
    log "$*"
}

finish() {
    log "hook finished in $((SECONDS - HOOK_START))s (exit 0)"
    exit 0
}

log "hook start: worktree=$WORKTREE_PATH timeout=${SYNC_TIMEOUT}s pid=$$"

if ! command -v uv >/dev/null 2>&1; then
    warn "'uv' not found on PATH -- skipping env sync."
    warn "run '$SYNC_CMD_TEXT' manually in $WORKTREE_PATH."
    finish
fi
log "uv: $(command -v uv) ($(uv --version 2>/dev/null </dev/null 3>&- 9>&- || echo unknown))"

# Kill a process and its direct children. A `uv sync` stuck building an sdist
# has a build-backend child that must not be orphaned holding the log open.
kill_tree() {
    local pid="$1" sig="$2" child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        kill_tree "$child" "$sig"
    done
    kill "-$sig" "$pid" 2>/dev/null || true
}

say "syncing lock-pinned dev env ($SYNC_CMD_TEXT, timeout ${SYNC_TIMEOUT}s, log: $LOG_FILE)..."
SYNC_START=$SECONDS
(
    cd "$WORKTREE_PATH" || exit 1
    export UV_NO_PROGRESS=1
    exec "${SYNC_CMD[@]}"
) </dev/null >>"$LOG_FILE" 2>&1 3>&- 9>&- &
SYNC_PID=$!

# If the hook itself is killed mid-sync, take the sync down with it rather than
# orphaning uv (and its build-backend children) holding the log open. Then
# re-raise the signal instead of exiting 0: the always-exit-0 contract covers
# failures, not a caller deliberately cancelling, and a parent shell (e.g.
# worktree.sh on Ctrl-C) only aborts if its child really died of the signal.
on_signal() {
    log "hook received SIG$1 during sync; killing pid $SYNC_PID"
    kill_tree "$SYNC_PID" TERM
    sleep 1
    kill_tree "$SYNC_PID" KILL
    wait "$SYNC_PID" 2>/dev/null
    warn "WARNING: hook interrupted (SIG$1); '$SYNC_CMD_TEXT' was killed."
    warn "worktree is usable; run '$SYNC_CMD_TEXT' manually in $WORKTREE_PATH."
    log "hook re-raising SIG$1 after $((SECONDS - HOOK_START))s"
    trap - "$1"
    kill "-$1" $$
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT

# Portable timeout: poll the sync process once per second.
TIMED_OUT=0
while kill -0 "$SYNC_PID" 2>/dev/null; do
    if ((SECONDS - SYNC_START >= SYNC_TIMEOUT)); then
        TIMED_OUT=1
        break
    fi
    sleep 1
done

if [[ "$TIMED_OUT" -eq 1 ]]; then
    log "sync exceeded ${SYNC_TIMEOUT}s; process tree at timeout:"
    ps -o pid,ppid,etime,stat,command -p "$SYNC_PID" >>"$LOG_FILE" 2>&1 || true
    pgrep -P "$SYNC_PID" 2>/dev/null | while read -r c; do
        ps -o pid,ppid,etime,stat,command -p "$c" >>"$LOG_FILE" 2>&1 || true
    done
    kill_tree "$SYNC_PID" TERM
    for _ in 1 2 3 4 5; do
        kill -0 "$SYNC_PID" 2>/dev/null || break
        sleep 1
    done
    kill_tree "$SYNC_PID" KILL
    wait "$SYNC_PID" 2>/dev/null
    warn "WARNING: '$SYNC_CMD_TEXT' did not finish within ${SYNC_TIMEOUT}s and was killed."
    warn "worktree is usable; run '$SYNC_CMD_TEXT' manually in $WORKTREE_PATH"
    warn "before trusting mypy/pytest results. Details: $LOG_FILE"
else
    SYNC_RC=0
    wait "$SYNC_PID" || SYNC_RC=$?
    SYNC_ELAPSED=$((SECONDS - SYNC_START))
    log "sync exited $SYNC_RC after ${SYNC_ELAPSED}s"
    if [[ "$SYNC_RC" -eq 0 ]]; then
        say "worktree env matches uv.lock (sync took ${SYNC_ELAPSED}s)."
    else
        warn "WARNING: '$SYNC_CMD_TEXT' failed (exit $SYNC_RC after ${SYNC_ELAPSED}s)."
        warn "run it manually in $WORKTREE_PATH before trusting mypy/pytest results. Details: $LOG_FILE"
    fi
fi

# The sync is over: a late signal must not be reported as an interrupted sync.
trap - TERM INT

# Standing reminder (CLAUDE.md): uv sync does NOT build the native extension.
echo "post-worktree hook: reminder -- 'uv run kct build-native' is still a separate step" \
    "(the C++ router extension is not built by uv sync)."

finish
