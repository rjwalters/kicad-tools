#!/usr/bin/env bash
# live-daemon-guard.sh — shared "is there a live loom-daemon on THIS host?"
# detection, extracted from run-ci-suites.sh's #6386 guard so a second caller
# (nextest-daemon-guard.sh, #6528) can reuse the exact same logic instead of
# duplicating it and risking the two definitions drifting apart.
#
# Source this file (do not exec). It defines three functions:
#
#   guard_repo_root_from <dir>
#       Mirror of `find_repo_root()` in loom-daemon-start.sh/-stop.sh
#       (#6420) — see the function's own comment below for the walk it does.
#       Prints the resolved root and returns 0, or returns 1 when the walk
#       finds neither a `.loom` directory nor a worktree `.git` file whose
#       main checkout has one.
#
#   live_daemon_pidfile_candidates
#       Prints every pid-file path a loom-daemon on this host could be
#       recorded in, one per line (duplicates are harmless — consumers
#       `sort -u`). Depends on the CALLER having already set `$SCRIPT_DIR`
#       (that script's own directory, used as one of the `guard_repo_root_from`
#       walk origins) and `$REPO_ROOT` (this repo's root) before sourcing —
#       both run-ci-suites.sh and nextest-daemon-guard.sh set these before the
#       `source` line.
#
#   live_daemon_pidfiles_present
#       Prints each EXISTING candidate as "<path> (pid <n>, ALIVE|not
#       running)". Existence alone is the trigger, not liveness — see the
#       function's own comment below. Empty output = no evidence of a live
#       daemon on this host.
#
# LOOM_CI_DAEMON_PIDFILE_CANDIDATES=<path>[:<path>…]|none
#       TEST-ONLY seam: replace the derived candidate list (`none` = no
#       candidates at all), so a guard's own regression suite can assert BOTH
#       directions regardless of whether its host runs a real daemon. See
#       test-run-ci-suites-daemon-guard.sh and test-nextest-daemon-guard.sh.
#
# Original history / incident context: #6386 (an Auditor's
# run-ci-suites.sh invocation SIGTERM'd the fleet's live dispatcher via this
# exact detection gap, 11h outage) and #6528 (the same detection reused to
# guard the Rust `daemon-integration` nextest group).

# guard_repo_root_from <dir> — walk up from <dir> and stop at the first of:
#
#   * a `.loom` DIRECTORY  -> that dir is the workspace root, or
#   * a `.git` FILE        -> a linked worktree, whose gitdir names the MAIN
#                             checkout; that checkout is the root when IT has a
#                             `.loom/` (the same gate find_repo_root applies).
#
# The second branch matters in a consumer repo whose worktrees have no tracked
# `.loom/`: a suite launched from a worktree resolves the MAIN checkout's state
# home (that is what the lifecycle scripts do, via $PWD), so the candidate list
# must include the main checkout's pid file too, not just the worktree's own.
# Prints the root and returns 0; returns 1 when the walk finds neither.
guard_repo_root_from() {
    local dir="$1" gitdir main_repo
    while [[ -n "$dir" && "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then printf '%s\n' "$dir"; return 0; fi
        if [[ -f "$dir/.git" ]]; then
            gitdir=$(sed 's/^gitdir: //' "$dir/.git" 2>/dev/null)
            if [[ -n "$gitdir" ]]; then
                main_repo=$(dirname "$(dirname "$(dirname "$gitdir")")")
                if [[ -d "$main_repo/.loom" ]]; then printf '%s\n' "$main_repo"; return 0; fi
            fi
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

# Every pid file a loom-daemon on THIS host could be recorded in, mirroring the
# resolution tiers the lifecycle scripts and the daemon use: the explicit
# LOOM_PID_FILE, the machine-level state home, the `<repo>/.loom` of the
# workspace / machine checkout / this checkout, and — via guard_repo_root_from
# above (#6420) — whatever find_repo_root()'s own walk resolves from the
# caller's SCRIPT_DIR and from $PWD, including the `.git`-file (worktree ->
# main checkout) branch.
live_daemon_pidfile_candidates() {
    local root
    if [[ "${LOOM_CI_DAEMON_PIDFILE_CANDIDATES:-}" == "none" ]]; then
        return 0
    fi
    if [[ -n "${LOOM_CI_DAEMON_PIDFILE_CANDIDATES:-}" ]]; then
        printf '%s\n' "${LOOM_CI_DAEMON_PIDFILE_CANDIDATES//:/$'\n'}"
        return 0
    fi
    [[ -n "${LOOM_PID_FILE:-}" ]] && printf '%s\n' "$LOOM_PID_FILE"
    printf '%s\n' "$HOME/.loom/.daemon.pid"
    [[ -n "${LOOM_WORKSPACE:-}" ]] && printf '%s\n' "$LOOM_WORKSPACE/.loom/.daemon.pid"
    [[ -n "${LOOM_MACHINE_CHECKOUT:-}" ]] && printf '%s\n' "$LOOM_MACHINE_CHECKOUT/.loom/.daemon.pid"
    printf '%s\n' "$REPO_ROOT/.loom/.daemon.pid"
    # The find_repo_root() walk itself (#6420) — from the caller's own script
    # location and from the cwd the caller inherits, since that is the tier the
    # lifecycle scripts resolve. Duplicates are harmless (the consumer sorts -u).
    if root="$(guard_repo_root_from "$SCRIPT_DIR")"; then printf '%s\n' "$root/.loom/.daemon.pid"; fi
    if root="$(guard_repo_root_from "$PWD")"; then printf '%s\n' "$root/.loom/.daemon.pid"; fi
    return 0
}

# Print each existing candidate as "<path> (pid <n>, alive|not running)".
# Existence alone is the trigger, not liveness: the lifecycle suites `rm -f`
# the pid file they resolve, so even a stale one is state this host owns and
# these suites must not silently delete.
#
# Deliberately NOT a second `pgrep -f '(^|/)loom-daemon$'` tier. It looks like
# free defence-in-depth ("catch a daemon whose pid file was already deleted"),
# but on macOS — the platform this fleet's dispatcher runs on — `pgrep -f`
# could not see the live daemon at all when this was measured (2026-08-17: the
# daemon was up, `ps` showed `/Users/…/.local/bin/loom-daemon`, and every
# `pgrep -f` pattern matching it returned nothing). A safety tier that silently
# matches nothing is worse than no tier: it invites exactly the "the guard has
# it covered" confidence that made the #6386 incident possible. The pid-file
# tiers above were verified to fire on a real fleet host. (The lifecycle
# scripts' own last-resort `pgrep` tier is subject to the same limitation — it
# is a best-effort recovery path there, never a safety guarantee.)
live_daemon_pidfiles_present() {
    local path pid
    while IFS= read -r path; do
        [[ -n "$path" && -f "$path" ]] || continue
        pid="$(tr -dc '0-9' < "$path" 2>/dev/null | head -c 12)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            printf '%s (pid %s, ALIVE)\n' "$path" "$pid"
        else
            printf '%s (pid %s, not running)\n' "$path" "${pid:-?}"
        fi
    done < <(live_daemon_pidfile_candidates | awk 'NF' | sort -u)
}
