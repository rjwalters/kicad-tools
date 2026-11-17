#!/usr/bin/env bash
# bg-proc-trap.sh — shared background-PID bookkeeping for test fixtures that
# spawn long-lived child processes (fake daemons, decoys, mock servers).
#
# Why this exists (#4773): several shell test harnesses under
# defaults/scripts/tests/ background a fixture process (a fake `loom-daemon`
# stub, a `sleep N` decoy standing in for a real PID, a mock AF_UNIX server)
# and only reap it on a clean EXIT — or only via an in-body `wait "$PID"` that
# never runs if the test process is interrupted first. A hard interruption
# (SIGINT/SIGTERM of the test runner, e.g. a cancelled overnight sweep) then
# leaks the fixture process indefinitely; a stub named `loom-daemon` is
# actively hazardous because it poisons name-based `pgrep` liveness checks.
#
# This generalizes the LIVE_PIDS array + looped-kill cleanup() pattern
# pioneered by test-sweep-run-registry.sh: every suite that backgrounds a
# fixture process tracks its PID here, then reaps every tracked PID from an
# EXIT *and* INT/TERM trap. A SIGKILL of the test-runner process cannot be
# caught by any trap — that is a hard bash/POSIX limitation, not a gap in
# this helper — so it does not attempt to close that residual case.
#
# A second, related bash limitation (verified empirically while building this
# fix): a trap for a signal received WHILE the script is blocked on a
# synchronous *foreground* command (a `$(...)` capture, a plain `cmd`, a `(
# ... )` subshell -- anything that is not one of ITS OWN `&`-backgrounded,
# bg_proc_track'd children) is deferred until that foreground command
# completes; it does not interrupt it. If that foreground command itself
# hangs forever, the trap never runs at all. This is orthogonal to (and not
# fixed by) this helper -- it affects trap-based cleanup in general, not just
# background-PID reaping. In this repo it is the documented, already-excluded
# root cause of test-loom-daemon-update.sh's "full update run" hang (see
# defaults/scripts/tests/ci-excluded.txt): a fake test daemon that only
# handles `--version` falls into an infinite loop under `loom-daemon
# calibrate`, which loom-daemon-start.sh's print_calibrate_hint() calls via a
# blocking `$(...)`. Fixing that hang (a production timeout guard around that
# call, or a smarter fake-daemon stub) is out of scope for #4773.
#
# Usage (source it, then track every backgrounded PID immediately after `&`):
#   source "$SCRIPT_DIR/lib/bg-proc-trap.sh"
#   some_fixture_daemon >/dev/null 2>&1 &
#   bg_proc_track "$!"
#   trap 'bg_proc_reap; rm -rf "$WORKDIR"' EXIT
#   trap 'bg_proc_reap; rm -rf "$WORKDIR"; exit 1' INT TERM
#
# IMPORTANT: register EXIT and INT/TERM as TWO separate `trap` calls, not one
# combined `trap CMD EXIT INT TERM`. Bash runs CMD when INT/TERM arrives but
# does NOT exit the script afterward (only a firing EXIT trap auto-exits) --
# a single combined trap would clean up once and then keep executing every
# remaining test case in the suite, re-populating $WORKDIR as it goes. The
# INT/TERM trap must end with an explicit `exit` to actually stop the script.
#
# Two documented contracts (PR #4790 judge nits, #4800):
#   1. bg_proc_track ALWAYS returns 0, even when called with an empty/absent
#      PID (e.g. a failed spawn produced no `$!`). This is a tolerated no-op,
#      not an error, so the call is safe to make under `set -e` — a future
#      `set -e` suite sourcing this helper will not abort on a benign empty
#      capture.
#   2. A few suites add a path-pattern `pkill -f "$WORKDIR"` backstop (on top
#      of bg_proc_reap) to catch fixture processes whose argv references a
#      scratch directory rather than a tracked PID. If `mktemp -d` ever fails,
#      $WORKDIR is empty and `pkill -f ""` matches (and kills) EVERY process
#      visible to the caller — any suite adding such a backstop MUST guard it
#      with `[ -n "$WORKDIR" ] && pkill -f "$WORKDIR" ...` first.

# Array of PIDs to kill on cleanup. Declared at source time so every sourcing
# script (and any function it defines) shares one array without redeclaring
# it — bg_proc_track/bg_proc_reap below close over this exact variable.
BG_PROC_TRACKED_PIDS=()

# Record a backgrounded PID for later reaping. Call immediately after `&`,
# e.g. `some_cmd & bg_proc_track "$!"`. Contract: an empty/absent PID is a
# tolerated no-op that always returns 0 (success) — never 1 — so it is safe
# to call under `set -e` even if the spawn failed to produce a PID.
bg_proc_track() {
    local pid="$1"
    [[ -n "$pid" ]] && BG_PROC_TRACKED_PIDS+=("$pid")
    return 0
}

# Kill every tracked PID that is still alive. Idempotent and safe to call
# more than once (e.g. once from an explicit mid-test cleanup and again from
# the EXIT/INT/TERM trap) — killing an already-dead PID is a harmless no-op.
bg_proc_reap() {
    local pid
    for pid in "${BG_PROC_TRACKED_PIDS[@]:-}"; do
        [[ -n "$pid" ]] && kill "$pid" 2>/dev/null
    done
}
