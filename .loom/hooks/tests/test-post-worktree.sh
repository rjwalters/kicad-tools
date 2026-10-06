#!/usr/bin/env bash
# Regression tests for the REPO-OWNED .loom/hooks/post-worktree.sh (issue #5967).
#
# Usage: ./.loom/hooks/tests/test-post-worktree.sh
#
# A stub `uv` on PATH stands in for the real one, so no environment is synced.
# Covers:
#   (a) success: exit 0, success line, per-step timing written to the log
#   (b) failing sync: exit 0 with a warning
#   (c) hung sync: killed at POST_WORKTREE_SYNC_TIMEOUT, warning, exit 0, fast
#   (d) stdin is /dev/null (a sync that reads stdin cannot block on a prompt)
#   (e) fd hygiene: run the way worktree.sh does it (`exec 3>&1`, piped caller),
#       a sync that leaves a long-lived child holding fd 3 must not keep the
#       caller's pipe open -- the #5967 "worktree.sh never exits" shape
#   (f) uv missing from PATH: exit 0 with a warning
#   (g) invalid POST_WORKTREE_SYNC_TIMEOUT falls back to the default
#   (h) a leading-zero timeout ("02") is read as decimal and still caps (#5973)
#   (i) TERM to the hook mid-sync kills the sync instead of orphaning it, and
#       the hook dies of SIGTERM (exit 143) rather than reporting success (#5973)
#   (j) Ctrl-C (SIGINT to the process group) aborts the calling parent script
#       too, the way worktree.sh calls the hook; it must not carry on (#5973)
#   (k) oversized POST_WORKTREE_SYNC_TIMEOUT is rejected, not wrapped (#5973)
# Exit code 0 = all pass, 1 = failures.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SCRIPT_DIR/../post-worktree.sh"

PASS=0
FAIL=0
TMPROOT="$(mktemp -d)"
trap 'pkill -f "post-worktree-test-sleeper" 2>/dev/null; rm -rf "$TMPROOT"' EXIT

ok() { PASS=$((PASS + 1)); echo "PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; }

# make_stub <name> <body>: a directory holding an executable `uv` stub.
make_stub() {
    local dir="$TMPROOT/bin-$1"
    mkdir -p "$dir"
    printf '#!/bin/bash\nif [[ "${1:-}" == "--version" ]]; then echo "uv 0.0.0-stub"; exit 0; fi\n%s\n' "$2" >"$dir/uv"
    chmod +x "$dir/uv"
    echo "$dir"
}

# Minimal PATH that still has coreutils/pgrep but no real uv.
BASE_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

new_wt() {
    local wt="$TMPROOT/wt-$1"
    mkdir -p "$wt"
    echo "$wt"
}

# (a) success
STUB="$(make_stub ok 'echo "Installed 1 package"; exit 0')"
WT="$(new_wt a)"
OUT="$(PATH="$STUB:$BASE_PATH" "$HOOK" "$WT" b 1 2>&1)"
RC=$?
if [[ $RC -eq 0 && "$OUT" == *"matches uv.lock"* ]]; then ok "(a) success exits 0"; else bad "(a) success: rc=$RC out=$OUT"; fi
LOG="$WT/.loom/logs/post-worktree.log"
if grep -q "sync exited 0 after" "$LOG" && grep -q "hook finished in" "$LOG" && grep -q "Installed 1 package" "$LOG"; then
    ok "(a) log has timing and uv output"
else
    bad "(a) log missing timing/output: $(cat "$LOG" 2>/dev/null)"
fi

# (b) failing sync
STUB="$(make_stub fail 'echo boom >&2; exit 3')"
OUT="$(PATH="$STUB:$BASE_PATH" "$HOOK" "$(new_wt b)" b 1 2>&1)"
RC=$?
if [[ $RC -eq 0 && "$OUT" == *"failed (exit 3"* ]]; then ok "(b) failed sync warns, exits 0"; else bad "(b) rc=$RC out=$OUT"; fi

# (c) hung sync is bounded
STUB="$(make_stub hang 'exec -a post-worktree-test-sleeper sleep 300')"
START=$SECONDS
OUT="$(POST_WORKTREE_SYNC_TIMEOUT=2 PATH="$STUB:$BASE_PATH" "$HOOK" "$(new_wt c)" b 1 2>&1)"
RC=$?
ELAPSED=$((SECONDS - START))
if [[ $RC -eq 0 && "$OUT" == *"did not finish within 2s"* && "$OUT" == *"run 'uv sync --frozen --extra dev' manually"* && $ELAPSED -lt 15 ]]; then
    ok "(c) hung sync killed after timeout (${ELAPSED}s)"
else
    bad "(c) rc=$RC elapsed=${ELAPSED}s out=$OUT"
fi
if pgrep -f "post-worktree-test-sleeper" >/dev/null; then bad "(c) hung sync process survived"; else ok "(c) hung sync process reaped"; fi

# (d) stdin is /dev/null: a stub that reads stdin must see EOF, not the
# caller's input (a prompt in a background job would otherwise block forever).
STUB="$(make_stub stdin 'read -r line; echo "read-rc=$? line=${line:-}"; exit 0')"
WT="$(new_wt d)"
OUT="$(PATH="$STUB:$BASE_PATH" "$HOOK" "$WT" b 1 2>&1 <<<"typed-input")"
if grep -q "read-rc=1 line=$" "$WT/.loom/logs/post-worktree.log"; then ok "(d) stdin is /dev/null"; else bad "(d) $(cat "$WT/.loom/logs/post-worktree.log")"; fi

# (e) fd 3 hygiene: worktree.sh does `exec 3>&1`; a lingering grandchild that
# kept fd 3 would hold the caller's pipe open until it exited.
STUB="$(make_stub fd3 '(exec -a post-worktree-test-sleeper sleep 30) </dev/null >/dev/null 2>&1 & exit 0')"
START=$SECONDS
PATH="$STUB:$BASE_PATH" bash -c 'exec 3>&1 1>&2; "$0" "$1" b 1' "$HOOK" "$(new_wt e)" 2>/dev/null | cat >/dev/null
ELAPSED=$((SECONDS - START))
if [[ $ELAPSED -lt 10 ]]; then ok "(e) caller pipe reaches EOF (${ELAPSED}s)"; else bad "(e) caller pipe held open for ${ELAPSED}s"; fi
pkill -f "post-worktree-test-sleeper" 2>/dev/null

# (f) uv missing
EMPTY="$TMPROOT/empty-bin"
mkdir -p "$EMPTY"
OUT="$(PATH="$EMPTY:$BASE_PATH" "$HOOK" "$(new_wt f)" b 1 2>&1)"
RC=$?
if PATH="$BASE_PATH" command -v uv >/dev/null 2>&1; then
    ok "(f) skipped: a system uv lives in $BASE_PATH"
elif [[ $RC -eq 0 && "$OUT" == *"'uv' not found"* ]]; then ok "(f) missing uv warns, exits 0"; else bad "(f) rc=$RC out=$OUT"; fi

# (g) invalid timeout value
STUB="$(make_stub ok2 'exit 0')"
OUT="$(POST_WORKTREE_SYNC_TIMEOUT=abc PATH="$STUB:$BASE_PATH" "$HOOK" "$(new_wt g)" b 1 2>&1)"
RC=$?
if [[ $RC -eq 0 && "$OUT" == *"invalid POST_WORKTREE_SYNC_TIMEOUT"* && "$OUT" == *"matches uv.lock"* ]]; then ok "(g) invalid timeout falls back"; else bad "(g) rc=$RC out=$OUT"; fi

# (h) leading-zero timeout still caps (bash would read "08" as invalid octal)
STUB="$(make_stub hang-h 'exec -a post-worktree-test-sleeper-h sleep 300')"
START=$SECONDS
OUT="$(POST_WORKTREE_SYNC_TIMEOUT=02 PATH="$STUB:$BASE_PATH" "$HOOK" "$(new_wt h)" b 1 2>&1)"
RC=$?
ELAPSED=$((SECONDS - START))
if [[ $RC -eq 0 && "$OUT" == *"did not finish within 2s"* && "$OUT" != *"value too great"* && $ELAPSED -lt 15 ]]; then
    ok "(h) leading-zero timeout caps the sync (${ELAPSED}s)"
else
    bad "(h) rc=$RC elapsed=${ELAPSED}s out=$OUT"
fi
if pgrep -f "post-worktree-test-sleeper-h" >/dev/null; then bad "(h) hung sync process survived"; else ok "(h) hung sync process reaped"; fi

# (i) TERM to the hook mid-sync does not orphan uv
STUB="$(make_stub hang-i 'exec -a post-worktree-test-sleeper-i sleep 300')"
POST_WORKTREE_SYNC_TIMEOUT=120 PATH="$STUB:$BASE_PATH" "$HOOK" "$(new_wt i)" b 1 >"$TMPROOT/out-i" 2>&1 &
HOOK_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "post-worktree-test-sleeper-i" >/dev/null && break
    sleep 0.5
done
START=$SECONDS
kill -TERM "$HOOK_PID"
wait "$HOOK_PID"
RC=$?
ELAPSED=$((SECONDS - START))
OUT="$(cat "$TMPROOT/out-i")"
if [[ $RC -eq 143 && "$OUT" == *"hook interrupted (SIGTERM)"* && $ELAPSED -lt 10 ]]; then
    ok "(i) TERM to the hook re-raises (exit 143) promptly (${ELAPSED}s)"
else
    bad "(i) rc=$RC elapsed=${ELAPSED}s out=$OUT"
fi
sleep 0.5
if pgrep -f "post-worktree-test-sleeper-i" >/dev/null; then bad "(i) uv orphaned after TERM to the hook"; else ok "(i) sync killed with the hook"; fi

# (j) Ctrl-C reaches the whole foreground process group. Model that: a parent
# shell (in its own process group, with default SIGINT -- a backgrounded job in
# a non-interactive shell would otherwise start with SIGINT ignored) calls the
# hook as worktree.sh does, then would print PARENT_CONTINUED.
STUB="$(make_stub hang-j 'exec -a post-worktree-test-sleeper-j sleep 300')"
WTJ="$(new_wt j)"
POST_WORKTREE_SYNC_TIMEOUT=120 PATH="$STUB:$BASE_PATH" \
    perl -e '$SIG{INT}="DEFAULT"; $SIG{TERM}="DEFAULT"; setpgrp(0,0); exec @ARGV' \
    /bin/bash -c '"$1" "$2" b 1; echo PARENT_CONTINUED' _ "$HOOK" "$WTJ" >"$TMPROOT/out-j" 2>&1 &
PARENT_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -f "post-worktree-test-sleeper-j" >/dev/null && break
    sleep 0.5
done
kill -INT -- "-$PARENT_PID"
wait "$PARENT_PID"
RC=$?
OUT="$(cat "$TMPROOT/out-j")"
if [[ $RC -eq 130 && "$OUT" != *"PARENT_CONTINUED"* && "$OUT" == *"hook interrupted (SIGINT)"* ]]; then
    ok "(j) Ctrl-C aborts the calling parent (exit 130)"
else
    bad "(j) rc=$RC out=$OUT"
fi
sleep 0.5
if pgrep -f "post-worktree-test-sleeper-j" >/dev/null; then bad "(j) uv orphaned after Ctrl-C"; else ok "(j) sync killed on Ctrl-C"; fi

# (k) oversized timeout values are rejected rather than wrapped by 64-bit math
STUB="$(make_stub ok3 'exit 0')"
OUT="$(POST_WORKTREE_SYNC_TIMEOUT=18446744073709551617 PATH="$STUB:$BASE_PATH" "$HOOK" "$(new_wt k)" b 1 2>&1)"
RC=$?
if [[ $RC -eq 0 && "$OUT" == *"invalid POST_WORKTREE_SYNC_TIMEOUT='18446744073709551617'"* && "$OUT" == *"timeout 600s"* ]]; then
    ok "(k) oversized timeout rejected, shown as typed"
else
    bad "(k) rc=$RC out=$OUT"
fi

echo
echo "post-worktree hook tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
