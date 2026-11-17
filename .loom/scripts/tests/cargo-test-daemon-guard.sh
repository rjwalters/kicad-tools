#!/usr/bin/env bash
# cargo-test-daemon-guard.sh — live-daemon guard for the plain `cargo test
# --workspace` path used by the root `package.json` `test` script (issue
# #6554), mirroring nextest-daemon-guard.sh's guard for `cargo nextest run
# --workspace` (#6528) and run-ci-suites.sh's guard for the daemon-lifecycle
# shell suites (#6386).
#
# WHY THIS EXISTS
#
# Root `package.json`'s `test` script is plain `cargo test --workspace
# --locked --all-features --no-fail-fast -- --nocapture` — invoked by `pnpm
# test`, itself the last step of `npm run check:all` (this repo's own CI
# "full-stack check" job, and the command the Auditor role's Repo Discovery
# step recommends for a Node+Rust repo like this one). That directly runs
# `integration_security.rs` and `integration_factory_reset.rs`
# (loom-daemon/tests/), whose `setup()` calls `cleanup_all_loom_sessions()`,
# which kills EVERY `loom-*` tmux session on this host's shared, host-global
# `-L loom` socket — including a live production loom-daemon's own tracked
# sessions, not just other test binaries' (a reviewed, accepted-for-CI
# exception, #4622 — CI has no live daemon so its acceptance is unaffected
# there). `integration_basic.rs` uses the TEST_PREFIX-scoped
# `cleanup_test_sessions()` instead (not the host-wide sweep), so it is NOT
# guarded here — this deliberately matches nextest-daemon-guard.sh's
# GUARDED_BINARIES set exactly, not the wider net named informally in #6554's
# bug report.
#
# nextest-daemon-guard.sh already solves this precisely for `cargo nextest
# run` via `-E 'not binary(...)'`, but plain `cargo test` has no equivalent
# "exclude this named test target" flag (`--test <NAME>` is an ALLOWLIST, and
# `--exclude <SPEC>` only excludes a whole PACKAGE, not one target within it).
# So a live-daemon-guarded run here executes cargo test THREE times:
#
#   1. The original command, unchanged, plus `--exclude loom-daemon` — every
#      other workspace package's tests, untouched.
#   2. A second invocation scoped to `-p loom-daemon --lib --bins`, plus an
#      explicit `--test <name>` for every `loom-daemon/tests/*.rs` target
#      EXCEPT the guarded ones — i.e. loom-daemon's own unit tests and every
#      integration test target that is NOT one of the two host-mutating
#      binaries. The allowlist is derived by listing `loom-daemon/tests/*.rs`
#      at guard time, so a newly added integration test file is automatically
#      included without touching this script.
#   3. A third invocation scoped to `-p loom-daemon --doc` — loom-daemon's
#      doctests, which cargo refuses to run combined with `--lib`/`--bins`/
#      `--test` in a single invocation ("can't mix --doc with other target
#      selecting options").
#
# Reuses the SAME pid-file detection helpers nextest-daemon-guard.sh and
# run-ci-suites.sh use (defaults/scripts/lib/live-daemon-guard.sh), so all
# three guards agree about what "a live daemon on this host" means.
#
# Usage:
#   cargo-test-daemon-guard.sh --resolve "<cargo test command>"
#       Prints, to stdout, the command that should actually be run: byte-for-
#       byte unchanged when no live-daemon evidence is found, or the two-
#       invocation form described above (joined with `&&`) when it is. Guard
#       evidence (if any) is printed to stderr, never stdout, so --resolve's
#       stdout is always exactly one shell command line — safe to capture
#       with `$(...)`.
#
#   cargo-test-daemon-guard.sh --plan "<cargo test command>"
#       Prints "RUN <cmd>" (no live-daemon evidence) or
#       "GUARD <cmd> -> <resolved-cmd>" (evidence found) and exits 0. Runs
#       nothing — the same dry-run seam nextest-daemon-guard.sh and
#       run-ci-suites.sh offer, for this guard's own regression test
#       (test-cargo-test-daemon-guard.sh) and for an operator inspecting the
#       decision before running anything.
#
#   cargo-test-daemon-guard.sh "<cargo test command>"
#       Resolves the command exactly like --resolve, then EXECUTES it
#       (replacing this process via `exec`).
#
# LOOM_CI_DAEMON_PIDFILE_CANDIDATES=<path>[:<path>…]|none
#       TEST-ONLY seam, forwarded to the shared lib — see run-ci-suites.sh's
#       own doc comment for the full explanation. Lets this guard's
#       regression test assert both directions regardless of whether the host
#       running the test happens to have a real daemon.
#
# Exit codes: 0 in every mode except a usage error (missing "<cargo test
# command>" argument), which exits 64. In `exec` mode the exit code is
# whatever the resolved command itself exits with (this process is replaced).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck source=../lib/live-daemon-guard.sh
source "$REPO_ROOT/defaults/scripts/lib/live-daemon-guard.sh"

# The two host-mutating binaries in loom-daemon/tests/ (#6528) — kept as an
# explicit literal (not scraped from .config/nextest.toml) so a silent
# narrowing here is a test failure, not an invisible regression. Intentionally
# identical to nextest-daemon-guard.sh's GUARDED_BINARIES.
GUARDED_BINARIES="integration_security integration_factory_reset"
LOOM_DAEMON_TESTS_DIR="$REPO_ROOT/loom-daemon/tests"

usage() {
    echo "Usage: $(basename "$0") [--resolve|--plan] \"<cargo test command>\"" >&2
    exit 64
}

MODE="exec"
case "${1:-}" in
    --resolve) MODE="resolve"; shift ;;
    --plan) MODE="plan"; shift ;;
esac
[[ $# -ge 1 && -n "$1" ]] || usage
CMD="$1"

# Every loom-daemon integration test target NOT in GUARDED_BINARIES, as
# `--test <name>` flags — derived at guard time so a newly added test file is
# picked up automatically. Deliberately only *.rs files directly under
# tests/ (not tests/common/, which is `mod common;`, not its own target).
allowed_test_flags() {
    local f name guarded is_guarded
    [[ -d "$LOOM_DAEMON_TESTS_DIR" ]] || return 0
    for f in "$LOOM_DAEMON_TESTS_DIR"/*.rs; do
        [[ -f "$f" ]] || continue
        name="$(basename "$f" .rs)"
        is_guarded=false
        for guarded in $GUARDED_BINARIES; do
            [[ "$name" == "$guarded" ]] && is_guarded=true && break
        done
        "$is_guarded" || printf -- '--test %s ' "$name"
    done
}

LIVE_DAEMON_EVIDENCE="$(live_daemon_pidfiles_present)"
GUARDED=false
RESOLVED_CMD="$CMD"

if [[ -n "$LIVE_DAEMON_EVIDENCE" ]]; then
    GUARDED=true

    # Split the incoming command on the FIRST " -- " into cargo-args (PRE) and
    # whatever gets forwarded to the test binaries (POST) — package.json's
    # `test` script's `-- --nocapture` suffix must land after `--` in BOTH
    # invocations, not after our own inserted flags.
    if [[ "$CMD" == *" -- "* ]]; then
        PRE="${CMD%% -- *}"
        POST="${CMD#* -- }"
    else
        PRE="$CMD"
        POST=""
    fi

    OTHER_PACKAGES_CMD="$PRE --exclude loom-daemon"
    # `--doc` cannot be combined with `--lib`/`--bins`/`--test` ("can't mix
    # --doc with other target selecting options"), so loom-daemon's doctests
    # need their own invocation — matching how CI itself splits nextest (which
    # never runs doctests) from a separate `cargo test --workspace --doc` step.
    LOOM_DAEMON_TESTS_CMD="${PRE/--workspace/-p loom-daemon --lib --bins}"
    LOOM_DAEMON_TESTS_CMD="$LOOM_DAEMON_TESTS_CMD $(allowed_test_flags)"
    LOOM_DAEMON_DOC_CMD="${PRE/--workspace/-p loom-daemon --doc}"
    # Collapse any doubled whitespace left by the substitutions above so the
    # resolved command reads cleanly (cosmetic only — extra spaces are
    # harmless to the shell).
    LOOM_DAEMON_TESTS_CMD="$(printf '%s' "$LOOM_DAEMON_TESTS_CMD" | tr -s ' ')"

    if [[ -n "$POST" ]]; then
        RESOLVED_CMD="$OTHER_PACKAGES_CMD -- $POST && $LOOM_DAEMON_TESTS_CMD -- $POST && $LOOM_DAEMON_DOC_CMD -- $POST"
    else
        RESOLVED_CMD="$OTHER_PACKAGES_CMD && $LOOM_DAEMON_TESTS_CMD && $LOOM_DAEMON_DOC_CMD"
    fi

    {
        echo
        echo "############################################################"
        echo "!!! LIVE DAEMON DETECTED ON THIS HOST — integration_security / integration_factory_reset EXCLUDED (#6554, mirrors #6528/#6386)"
        echo "$LIVE_DAEMON_EVIDENCE" | sed 's/^/      /'
        echo "    Excluding: $GUARDED_BINARIES"
        echo "    These binaries' setup() calls cleanup_all_loom_sessions(), which kills EVERY"
        echo "    loom-* tmux session on this host's shared -L loom socket — including a live"
        echo "    daemon's own tracked sessions and real agent sessions, not just other test"
        echo "    binaries' (the same blast-radius class as #6386's 11h outage)."
        echo "    Run them on a host with no daemon (e.g. CI, where this guard is a no-op), or"
        echo "    run 'cargo test -p loom-daemon --test integration_security --test integration_factory_reset'"
        echo "    explicitly as a deliberate, informed choice."
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
