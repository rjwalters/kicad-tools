#!/usr/bin/env bash
# nextest-daemon-guard.sh — live-daemon guard for the Rust `daemon-integration`
# nextest test group (issue #6528), mirroring run-ci-suites.sh's shell-suite
# guard (#6386).
#
# WHY THIS EXISTS
#
# `integration_security.rs` and `integration_factory_reset.rs`
# (loom-daemon/tests/) call `cleanup_all_loom_sessions()` in their test
# `setup()`, which kills EVERY `loom-*` tmux session on the shared,
# host-global `-L loom` socket — including a live production loom-daemon's
# own tracked sessions, not just other test binaries' sessions (a reviewed,
# accepted-for-CI exception, #4622 — see `.config/nextest.toml`'s
# `[test-groups.daemon-integration]` comment and
# `loom-daemon/tests/common/mod.rs`'s `cleanup_all_loom_sessions()` doc
# comment). CI has no live daemon, so #4622's acceptance is unaffected there —
# this guard only changes behavior on a host where daemon pid-file evidence
# is present, the same class of hazard #6386 already fixed for the
# daemon-lifecycle shell suites.
#
# A THIRD binary, `integration_basic.rs`, is a related but DIFFERENT hazard
# (#6607) and is deliberately NOT added to the exclusion above: its `setup()`
# uses the TEST_PREFIX-scoped `cleanup_test_sessions()`, not the host-wide
# `cleanup_all_loom_sessions()`, so it never destroys the live daemon's own
# sessions. It still drives the SAME shared `-L loom` tmux socket the live
# daemon uses (`new-session`, `list-sessions`, `capture-pane`, …), so it can
# CONTEND with the daemon's own tmux activity and flake with `Timeout reading
# response` / `deadline has elapsed` under that contention — a host-artifact,
# not a real regression (confirmed by CI, which has no live daemon and passes
# cleanly on the same commit). Because it is non-destructive this guard warns
# rather than excludes it — see the banner below.
#
# Reuses the SAME pid-file detection helpers run-ci-suites.sh uses
# (defaults/scripts/lib/live-daemon-guard.sh), so both guards agree about
# what "a live daemon on this host" means.
#
# Usage:
#   nextest-daemon-guard.sh --resolve "<nextest command>"
#       Prints, to stdout, the command that should actually be run: byte-for-
#       byte unchanged when no live-daemon evidence is found, or with an `-E`
#       exclusion filter removing the two host-mutating binaries appended
#       when it is. Guard evidence (if any) is printed to stderr, never
#       stdout, so --resolve's stdout is always exactly one shell command —
#       safe to capture with `$(...)`.
#
#   nextest-daemon-guard.sh --plan "<nextest command>"
#       Prints "RUN <cmd>" (no live-daemon evidence) or
#       "GUARD <cmd> -> <resolved-cmd>" (evidence found) and exits 0. Runs
#       nothing — the same kind of dry-run seam run-ci-suites.sh --plan
#       offers, for this guard's own regression test
#       (test-nextest-daemon-guard.sh) and for an operator inspecting the
#       decision before running anything.
#
#   nextest-daemon-guard.sh "<nextest command>"
#       Resolves the command exactly like --resolve, then EXECUTES it
#       (replacing this process via `exec`).
#
# LOOM_CI_DAEMON_PIDFILE_CANDIDATES=<path>[:<path>…]|none
#       TEST-ONLY seam, forwarded to the shared lib — see run-ci-suites.sh's
#       own doc comment for the full explanation. Lets this guard's
#       regression test assert both directions regardless of whether the host
#       running the test happens to have a real daemon.
#
# Exit codes: 0 in every mode except a usage error (missing "<nextest
# command>" argument), which exits 64. In `exec` mode the exit code is
# whatever the resolved command itself exits with (this process is replaced).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../lib/live-daemon-guard.sh
source "$REPO_ROOT/defaults/scripts/lib/live-daemon-guard.sh"

# The two host-mutating binaries in the `daemon-integration` test group
# (#6528) — kept as an explicit literal (not scraped from .config/nextest.toml)
# so a silent narrowing here is a test failure, not an invisible regression.
GUARDED_BINARIES="integration_security integration_factory_reset"
EXCLUDE_FILTER="not binary(integration_security) and not binary(integration_factory_reset)"

# A third `daemon-integration` binary that is CONTENTION-flaky rather than
# destructive on a live-daemon host (#6607) — see this file's header comment.
# Deliberately NOT part of GUARDED_BINARIES/EXCLUDE_FILTER: it is non-
# destructive (uses cleanup_test_sessions(), not cleanup_all_loom_sessions()),
# so it keeps running; the guard only warns about it.
CONTENTION_BINARY="integration_basic"

usage() {
    echo "Usage: $(basename "$0") [--resolve|--plan] \"<nextest command>\"" >&2
    exit 64
}

MODE="exec"
case "${1:-}" in
    --resolve) MODE="resolve"; shift ;;
    --plan) MODE="plan"; shift ;;
esac
[[ $# -ge 1 && -n "$1" ]] || usage
CMD="$1"

LIVE_DAEMON_EVIDENCE="$(live_daemon_pidfiles_present)"
GUARDED=false
RESOLVED_CMD="$CMD"
if [[ -n "$LIVE_DAEMON_EVIDENCE" ]]; then
    GUARDED=true
    RESOLVED_CMD="$CMD -E '$EXCLUDE_FILTER'"
    {
        echo
        echo "############################################################"
        echo "!!! LIVE DAEMON DETECTED ON THIS HOST — daemon-integration binaries EXCLUDED (#6528, mirrors #6386)"
        echo "$LIVE_DAEMON_EVIDENCE" | sed 's/^/      /'
        echo "    Excluding: $GUARDED_BINARIES"
        echo "    These binaries' setup() calls cleanup_all_loom_sessions(), which kills EVERY"
        echo "    loom-* tmux session on this host's shared -L loom socket — including a live"
        echo "    daemon's own tracked sessions and real agent sessions, not just other test"
        echo "    binaries' (#6528, the same blast-radius class as #6386's 11h outage)."
        echo "    Run them on a host with no daemon (e.g. CI, where this guard is a no-op), or"
        echo "    run the excluded binaries explicitly as a deliberate, informed choice."
        echo "############################################################"
        echo
        echo "############################################################"
        echo "!!! LIVE DAEMON DETECTED ON THIS HOST — $CONTENTION_BINARY NOT excluded but may FLAKE (#6607)"
        echo "    $CONTENTION_BINARY is non-destructive (cleanup_test_sessions(), not"
        echo "    cleanup_all_loom_sessions()) so it is still included in this run. It drives"
        echo "    the SAME shared -L loom tmux socket the live daemon uses, though, so it can"
        echo "    CONTEND with the daemon's own tmux activity and fail with 'Timeout reading"
        echo "    response' / 'deadline has elapsed' under that contention — a host artifact,"
        echo "    not a real regression (CI has no live daemon and is unaffected)."
        echo "    A $CONTENTION_BINARY failure on THIS host is not necessarily a real bug —"
        echo "    re-run on a host with no daemon (e.g. CI) before treating it as one."
        echo "############################################################"
        echo
    } >&2
fi

case "$MODE" in
    resolve)
        printf '%s\n' "$RESOLVED_CMD"
        ;;
    plan)
        if [[ "$GUARDED" == "true" ]]; then
            printf 'GUARD %s -> %s\n' "$CMD" "$RESOLVED_CMD"
        else
            printf 'RUN %s\n' "$CMD"
        fi
        ;;
    exec)
        exec bash -c "$RESOLVED_CMD"
        ;;
esac
