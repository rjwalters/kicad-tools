#!/usr/bin/env bash
# require-daemon-bin.sh — pin the `loom-daemon` binary a stub-driven suite tests
# against (epic #7810, PR 3 onwards).
#
# Source this file (do not exec). Defines:
#
#   loom_test_require_daemon_bin [--self-only] <scripts-dir> <subcommand> [<subcommand>...]
#       Resolves a loom-daemon, exports LOOM_DAEMON_SELF_BIN (and, by default,
#       LOOM_DAEMON_BIN) so every stub this suite invokes execs THAT binary,
#       and verifies it knows each named subcommand. Never returns on failure —
#       it exits 1 with an actionable message.
#
# WHICH VARIABLE PINS THE STUB (#8134)
#
# LOOM_DAEMON_SELF_BIN is the pin that matters, and it is always exported: it
# means "the binary that IMPLEMENTS this stub", which `lib/script-helper.sh`
# now consults ahead of everything else. LOOM_DAEMON_BIN means something
# different — "the daemon this caller manages or probes" — and a suite whose
# subject invokes a daemon of its own uses it for exactly that.
#
# `--self-only` is for those suites: it pins the implementation and leaves
# LOOM_DAEMON_BIN alone, so the suite's own per-invocation
# `LOOM_DAEMON_BIN=<mock>` keeps its original meaning and its assertions need
# no edits. `loom-daemon-watchdog.sh` is the case this was built for — its
# retained suite pins a HANGING mock through LOOM_DAEMON_BIN to exercise the
# IPC probe, and a harness that exported LOOM_DAEMON_BIN over the top of it
# would both clobber that meaning and hand the stub the mock to exec.
#
# Without the flag BOTH are exported, which is what the five pure-computation
# suites already on this harness want: nothing in them reads LOOM_DAEMON_BIN
# for a second purpose, and keeping it exported preserves their behaviour
# exactly.
#
# WHY A SUITE NEEDS THIS
#
# A suite whose subject is now a thin stub over `loom-daemon <subcommand>` is
# only testing what it thinks it is if the stub execs the binary built from the
# working tree. Left to ambient resolution, `loom_locate_daemon_bin` prefers an
# installed `loom-daemon` on PATH (and, before that, `$LOOM_DAEMON_BIN`), so a
# stale machine-level install silently answers instead of the build under test.
# `LOOM_PREFER_REPO_BUILD=1` hoists the repo build above the install; in an
# installed consumer repo there is no repo build and this falls through to the
# installed binary exactly as before.
#
# WHY IT IS FATAL, NOT A SKIP
#
# Suites in this family carry assertions written against a SHELL implementation
# that has since been deleted. Running them against the port is the evidence
# that the port preserved its behaviour. A suite that quietly SKIPped itself
# when no binary resolved would remove that evidence from CI while still
# reporting green — the exact failure this epic keeps running into. If it
# cannot test the port, it fails and says why.
#
# The subcommand preflight exists for legibility: a binary predating the port
# makes every assertion exit 2 at once, which reads like a logic failure across
# the whole suite rather than one environment problem.

loom_test_require_daemon_bin() {
    local self_only=0
    while [[ "${1:-}" == --* ]]; do
        case "$1" in
            --self-only) self_only=1; shift ;;
            *) echo "FATAL: loom_test_require_daemon_bin: unknown option '$1'" >&2; exit 1 ;;
        esac
    done

    local scripts_dir="$1"
    shift

    # shellcheck source=../../lib/locate-daemon-bin.sh
    source "$scripts_dir/lib/locate-daemon-bin.sh"

    LOOM_LOCATE_DAEMON_BIN_QUIET=1
    LOOM_PREFER_REPO_BUILD=1
    export LOOM_LOCATE_DAEMON_BIN_QUIET LOOM_PREFER_REPO_BUILD

    local repo_root bin
    repo_root="$(cd "$scripts_dir/../.." && pwd)"
    if ! bin="$(loom_daemon_self_bin_override)"; then
        if [[ "$self_only" -eq 1 ]]; then
            # In --self-only mode $LOOM_DAEMON_BIN is the suite's PROBE
            # binary, so it must not answer "which binary implements the
            # stub" either. Unset for this one resolution (a command
            # substitution is already a subshell, so the caller's value is
            # untouched) — otherwise an ambient value would quietly become
            # the implementation, which is the #8134 collision one level up.
            bin="$(unset LOOM_DAEMON_BIN; loom_locate_daemon_bin "$repo_root")"
        else
            bin="$(loom_locate_daemon_bin "$repo_root")"
        fi
    fi
    if [[ -z "$bin" ]]; then
        echo "FATAL: no loom-daemon binary found, so this suite cannot test the port." >&2
        echo "  Build it:  cargo build --package loom-daemon" >&2
        echo "  Or set:    LOOM_DAEMON_SELF_BIN=/path/to/loom-daemon" >&2
        exit 1
    fi
    # Always pin the IMPLEMENTATION (#8134) — this is what every stub this
    # suite invokes now resolves first, whatever LOOM_DAEMON_BIN happens to
    # mean in this suite.
    export LOOM_DAEMON_SELF_BIN="$bin"
    if [[ "$self_only" -eq 0 ]]; then
        export LOOM_DAEMON_BIN="$bin"
    fi

    local sub
    for sub in "$@"; do
        if ! "$bin" "$sub" --help >/dev/null 2>&1; then
            echo "FATAL: $bin does not know the '$sub' subcommand," >&2
            echo "so it predates the port this suite exists to verify." >&2
            echo "Rebuild it: cargo build --package loom-daemon" >&2
            exit 1
        fi
    done
}
