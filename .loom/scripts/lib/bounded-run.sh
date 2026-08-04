#!/usr/bin/env bash
# bounded-run.sh — Run a command under a HARD wall-clock budget, returning 124
# on timeout exactly like GNU `timeout` does.
#
# Source this file (do not exec). Defines a single function:
#
#   bounded_run <timeout_secs> <cmd> [args...] -> runs <cmd> under the budget,
#                                                 forwarding stdout/stderr and
#                                                 its exit code; 124 on timeout.
#
# Extracted from loom-daemon-watchdog.sh's IPC-probe helper (#4398) so a SECOND
# blocking-`$(...)`-hang site could reuse the same hardened bounded-probe
# pattern instead of inventing a one-off: loom-daemon-start.sh's
# print_calibrate_hint() (#4799) makes a blocking `$(daemon calibrate
# ...)` call that never returns against a daemon binary with no `calibrate`
# handler (e.g. a test fixture, or a future breaking CLI change). Per the
# #4790 judge's repro of that exact hang, a signal arriving while a script is
# blocked inside a command substitution is deferred until the substitution
# returns -- which for a truly-wedged child never happens, so even an
# EXIT/INT/TERM trap on the *outer* script cannot fire in that state. Only a
# bound on the substitution itself closes that gap.
#
# macOS ships no `timeout(1)`, and for a probe an unbounded fallback is not
# acceptable -- "the command never returns" is precisely the failure mode
# this helper exists to bound -- so the no-`timeout` path below is a real
# bounded implementation, not a degrade-to-unbounded.
#
# `-k 2` is load-bearing on the `timeout(1)` path: a non-interactive bash that
# is blocked in a foreground command defers SIGTERM, so without the KILL
# escalation `timeout` itself would wait indefinitely for a child that ignores
# the TERM -- reintroducing the very unbounded wait this helper exists to
# prevent.
#
# LOOM_FORCE_PORTABLE_TIMEOUT=1 forces the portable (no-`timeout(1)`) fallback
# path, so that behavior (the default macOS shape) is testable on any host.
bounded_run() { # <timeout_secs> <cmd> [args...]
    local secs="$1"; shift
    if [[ ! "${LOOM_FORCE_PORTABLE_TIMEOUT:-}" =~ ^(1|true|yes)$ ]]; then
        if command -v timeout >/dev/null 2>&1; then
            timeout -k 2 "$secs" "$@"
            return $?
        fi
        if command -v gtimeout >/dev/null 2>&1; then
            gtimeout -k 2 "$secs" "$@"
            return $?
        fi
    fi
    # Portable fallback: run the command in the background and pair it with a
    # killer subshell that TERM/KILLs it once the budget elapses.
    "$@" &
    local cmd_pid=$!
    (
        sleep "$secs"
        kill -TERM "$cmd_pid" 2>/dev/null
        sleep 2
        kill -KILL "$cmd_pid" 2>/dev/null
    ) >/dev/null 2>&1 &
    local killer_pid=$! rc=0
    wait "$cmd_pid" 2>/dev/null || rc=$?
    kill "$killer_pid" 2>/dev/null
    wait "$killer_pid" 2>/dev/null
    # Normalize a killed-by-the-budget exit (128+TERM / 128+KILL) to `timeout`'s
    # own 124 so the caller has ONE code to branch on regardless of which
    # implementation ran.
    case "$rc" in 143|137) rc=124 ;; esac
    return "$rc"
}
