#!/usr/bin/env bash
# require-daemon-bin.sh — pin the `loom-daemon` binary a stub-driven suite tests
# against (epic #7810, PR 3 onwards).
#
# Source this file (do not exec). Defines:
#
#   loom_test_require_daemon_bin <scripts-dir> <subcommand> [<subcommand>...]
#       Resolves a loom-daemon, exports LOOM_DAEMON_BIN so every stub this
#       suite invokes execs THAT binary, and verifies it knows each named
#       subcommand. Never returns on failure — it exits 1 with an actionable
#       message.
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
    local scripts_dir="$1"
    shift

    # shellcheck source=../../lib/locate-daemon-bin.sh
    source "$scripts_dir/lib/locate-daemon-bin.sh"

    LOOM_LOCATE_DAEMON_BIN_QUIET=1
    LOOM_PREFER_REPO_BUILD=1
    export LOOM_LOCATE_DAEMON_BIN_QUIET LOOM_PREFER_REPO_BUILD

    local repo_root bin
    repo_root="$(cd "$scripts_dir/../.." && pwd)"
    bin="$(loom_locate_daemon_bin "$repo_root")"
    if [[ -z "$bin" ]]; then
        echo "FATAL: no loom-daemon binary found, so this suite cannot test the port." >&2
        echo "  Build it:  cargo build --package loom-daemon" >&2
        echo "  Or set:    LOOM_DAEMON_BIN=/path/to/loom-daemon" >&2
        exit 1
    fi
    export LOOM_DAEMON_BIN="$bin"

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
