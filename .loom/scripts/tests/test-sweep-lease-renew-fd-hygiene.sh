#!/usr/bin/env bash
# test-sweep-lease-renew-fd-hygiene.sh - the detached renewal loop that
# `sweep-lease-renew.sh start` forks must not hold the caller's fds (#10203).
#
# `worktree.sh N | tail` hung for hours: worktree.sh keeps its caller's stdout
# on fd 3, and the loop's `< /dev/null > /dev/null 2>&1` covered fds 0-2 only,
# so the loop and its `sleep` children kept the pipe open until the 4h cap.
#
# Covers:
#   (1) a pipe handed to `start` on fds 3 and 7 closes as soon as `start`
#       returns -- pre-fix, `cat` waits out the loop's first 8s sleep
#   (2) the loop is still running once the pipe has closed
#   (3) none of fds 3-8 or 10-12 is open in the loop or its `sleep` child once
#       it has parked in that sleep (fd 9 is its own log)
#
# Bounded: a regression fails in ~8s instead of hanging. The watched PID dies
# (4s) before the loop's first wake-up (8s), so the loop exits without ever
# renewing: no `gh` call is made and no stub is needed. Kept apart from
# test-sweep-lease-renew.sh, which sits at the file-size threshold
# (.loom/docs/file-size-policy.md).
#
# Needs a built loom-daemon (CI: the daemon-built job, not the hermetic loop).
#
# Usage:
#   ./.loom/scripts/tests/test-sweep-lease-renew-fd-hygiene.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SCRIPT="$SCRIPTS_DIR/sweep-lease-renew.sh"

# fds 10+ are closed by `loom-daemon lease renewer sanitize-exec`, which `start`
# re-enters through. Pin the built binary (FAILS rather than skips without one).
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "lease renewer sanitize-exec"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_FAILED=0
check() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$1" == "true" ]]; then
        echo -e "  ${GREEN}PASS${NC}: $2"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $2"
    fi
}

LOG="$(mktemp)"
sleep 4 &
WATCH_PID=$!
cleanup() {
    kill "$WATCH_PID" 2> /dev/null || true
    # The loop's `sleep` child outlives a killed loop; reap it too.
    [[ "${LOOP_PID:-}" =~ ^[0-9]+$ ]] && { pkill -P "$LOOP_PID" 2> /dev/null || true; }
    [[ "${LOOP_PID:-}" =~ ^[0-9]+$ ]] && { kill "$LOOP_PID" 2> /dev/null || true; }
    rm -f "$LOG"
}
trap cleanup EXIT

T0="$(date +%s)"
LOOP_PID="$({ LOOM_TERMINAL_ID='' "$SCRIPT" start 10203 --interval 8 --watch-pid "$WATCH_PID" 2> "$LOG" 3>&1 7>&1 10>&1 12>&1; } | cat)"
ELAPSED=$(($(date +%s) - T0))

check "$([[ "$ELAPSED" -lt 4 ]] && echo true || echo false)" \
    "(1) a pipe handed to start on fds 3/7/10/12 closes when start returns (took ${ELAPSED}s)"
LOOP_ALIVE=false
[[ "$LOOP_PID" =~ ^[0-9]+$ ]] && kill -0 "$LOOP_PID" 2> /dev/null && LOOP_ALIVE=true
check "$LOOP_ALIVE" "(2) the renewal loop is still running after the pipe closed (pid '${LOOP_PID}')"

# Sample only once the loop is parked in its first `sleep`. Before that, its own
# `$(ps ... | tr ...)` / `$(date ...)` substitutions briefly open a pipe on the
# lowest free fd -- 3, now that 3-8 are closed -- which a too-early /proc probe
# misread as a leak on Linux. An inherited fd persists through the sleep, so it
# is still caught here, in the loop and in the `sleep` child alike.
SLEEP_PID=""
for _ in $(seq 1 30); do
    SLEEP_PID="$(pgrep -P "$LOOP_PID" -x sleep 2> /dev/null | head -n 1)"
    [[ -n "$SLEEP_PID" ]] && break
    sleep 0.1
done
HELD=""
for pid in $LOOP_PID $SLEEP_PID; do
    [[ "$LOOP_ALIVE" == true ]] || break
    if [[ -d "/proc/$pid/fd" ]]; then
        for fd in 3 4 5 6 7 8 10 11 12; do [[ -e "/proc/$pid/fd/$fd" ]] && HELD+="$pid:$fd "; done
    elif command -v lsof > /dev/null 2>&1; then
        HELD+="$(lsof -a -p "$pid" -d 3-8,10-12 -F f 2> /dev/null | sed -n "s/^f\([0-9][0-9]*\)\$/$pid:\1/p" | tr '\n' ' ')"
    fi
done
check "$([[ -n "$SLEEP_PID" ]] && echo true || echo false)" \
    "(3a) the loop reached its sleep before fds were sampled (sleep pid '${SLEEP_PID}')"
check "$([[ "$LOOP_ALIVE" == true && -z "$HELD" ]] && echo true || echo false)" \
    "(3) neither the loop nor its sleep holds any of fds 3-8 or 10-12 (held: '${HELD}')"

echo ""
echo "Results: $((TESTS_RUN - TESTS_FAILED))/$TESTS_RUN passed"
if ((TESTS_FAILED > 0)); then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}ALL PASSED${NC}"
