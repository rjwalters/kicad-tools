#!/usr/bin/env bash
# script-helper.sh — resolve + exec a native `loom-daemon` script-helper
# subcommand (issue #4275, epic #4081 Phase 3 family 5).
#
# Source this file (do not exec). Defines:
#
#   loom_exec_script_helper <subcommand> [args...]
#       execs `loom-daemon <subcommand> "$@"`, resolving the binary through
#       lib/locate-daemon-bin.sh. Never returns on success.
#
# WHICH BINARY A STUB EXECS (#8134)
#
# A stub needs the binary that IMPLEMENTS its subcommand, which is not always
# the binary `$LOOM_DAEMON_BIN` names. That variable means "the daemon this
# caller manages or probes" — the install whose version is compared, the
# endpoint a watchdog round-trips, a deliberately fake binary in a test — and
# a script that BOTH is a stub AND invokes a daemon has two different binaries
# in play at once. `loom-daemon-watchdog.sh` is the first of those: its
# retained suite pins `$LOOM_DAEMON_BIN` to a hanging mock to exercise the IPC
# probe, and before this split the stub exec'd that mock as the watchdog and
# never returned.
#
# So resolution here is:
#
#   1. $LOOM_DAEMON_SELF_BIN (loom_daemon_self_bin_override) — the explicit
#      "this is my implementation" seam, for a test harness or an operator.
#   2. Otherwise loom_locate_daemon_bin, UNCHANGED — $LOOM_DAEMON_BIN, then
#      $PATH, then the machine-level install, then a repo-local build.
#
# Tier 2 is deliberately the whole existing chain and not the rest of
# loom_resolve_self_daemon_bin's: in production the installed daemon IS the
# implementation, `$LOOM_DAEMON_BIN` must keep pinning it (that is what
# `loom update` and an operator debugging a stub both rely on), and hoisting a
# checkout-local build above it would be a silent behaviour change of exactly
# the kind #8134 rejected.
#
# This is the native replacement for `lib/loom-tools.sh`'s `run_loom_tool` on
# the six script-helper entry points (`strip-ansi.sh`, `resolve-model.sh`,
# `check-usage.sh`, `checkpoint.sh`, `sweep-experiment.sh`,
# `validate-phase.sh`). It exists for the same reason `run_loom_tool` did:
# resolution belongs in ONE place, so the stubs stay one line each and a
# consumer workspace with no Python (and no pip) still works.
#
# Exit-code discipline (load-bearing): `exec` replaces this shell, so the
# subcommand's own exit code reaches the caller unmodified. Several of these
# helpers use non-zero codes as *data* rather than errors — `resolve-model
# --tier` and `--task-alias` exit 3 to mean "no mapping, fall through to your
# normal precedence chain", and `loom-claim` uses 1/2/3/4 to distinguish
# already-claimed / bad args / not-found / wrong-agent. Any wrapper that
# swallowed or remapped those codes would silently change dispatch behavior,
# which is why nothing here inspects the child's status.
#
# When no `loom-daemon` can be resolved, prints an actionable error naming the
# provisioning path and exits 1 (there is no Python fallback any more — the
# Python modules these subcommands replaced were deleted in #4275).

# Find the repository root from a starting directory.
_lsh_find_repo_root() {
    local dir="${1:-$(pwd)}"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

# LOOM_SCRIPT_HELPER_MISSING_RC — the exit code used when no loom-daemon can be
# resolved, and (since #8385) when a resolved binary is below a declared
# `# requires-daemon:` floor. Defaults to 1.
#
# A stub whose subcommand uses non-zero codes as DATA must override this, or a
# missing binary is indistinguishable from an answer. `detect-dependency-cycle`
# exits 1 to mean "cycle found" and `detect-startable-subset` exits 1 to mean
# "no subset declared"; a caller branching on the code alone would read a
# missing binary as a detected cycle. Those stubs set it to 2, which every one
# of these entry points already reserves for "could not run".
loom_exec_script_helper() {
    local subcommand="$1"
    shift

    # ${BASH_SOURCE[1]:-$0} hardens the caller-frame lookup the same way
    # run_loom_tool did (#3680): every call site is a bash-shebang'd script, so
    # BASH_SOURCE is populated, but the bare bashism would break if that changed.
    local script_dir repo_root bin
    script_dir="$(cd "$(dirname "${BASH_SOURCE[1]:-$0}")" && pwd)"
    repo_root="$(_lsh_find_repo_root "$script_dir")" || repo_root=""

    # LOOM_DAEMON_SELF_BIN: which binary IMPLEMENTS this stub, as distinct from
    # LOOM_DAEMON_BIN, which means "the loom-daemon binary a script should
    # INVOKE". Those are the same thing for every port so far, and they are NOT
    # the same for a script that invokes loom-daemon itself: the watchdog's
    # retained suite sets LOOM_DAEMON_BIN to a MOCK so it can drive the IPC
    # probe, and a stub resolving through it would exec the mock as its own
    # implementation. Test 13's mock is `while true; do sleep 1; done`, so that
    # presents as a hang rather than a failure (#8134).
    #
    # Unset in production, where the two ARE the same and the normal resolution
    # below applies unchanged. Checked first, so a harness can pin the real
    # binary without disturbing what LOOM_DAEMON_BIN means to everything else.
    #
    # The library is sourced ABOVE this branch rather than below it (#8385)
    # because both exec paths now go through loom_daemon_exec_checked, and the
    # $LOOM_DAEMON_SELF_BIN seam is "pin the binary that implements me", not
    # "skip my checks": a harness pinning a stale build should get the same
    # actionable refusal an operator would. Sourcing is inert — it defines
    # functions and resolves nothing — so hoisting it changes no behaviour.
    # shellcheck source=/dev/null
    source "$(dirname "${BASH_SOURCE[0]}")/locate-daemon-bin.sh"
    if [[ -n "${LOOM_DAEMON_SELF_BIN:-}" && -x "${LOOM_DAEMON_SELF_BIN}" ]]; then
        loom_daemon_exec_checked "${BASH_SOURCE[1]:-$0}" "${LOOM_DAEMON_SELF_BIN}" "$subcommand" "$@"
    fi

    # $LOOM_DAEMON_SELF_BIN first (the implementation), then the normal
    # resolution completely unchanged — see "WHICH BINARY A STUB EXECS" above.
    # Both tiers are defined in the resolver library, not here: this file stays
    # glue, and every "which loom-daemon?" question is answered in one place.
    bin="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$repo_root")"

    # loom_daemon_exec_checked, not a bare `exec`: it runs the marker-driven
    # daemon-version preflight (#8385) against the CALLING STUB's own
    # `# requires-daemon:` marker and then execs. Inert for a stub that
    # declares nothing, so every one of the eleven stubs behaves exactly as it
    # did until it opts in. ${BASH_SOURCE[1]:-$0} is passed explicitly because
    # the stub — not this library — is the file that owns the declaration.
    if [[ -n "$bin" ]]; then
        loom_daemon_exec_checked "${BASH_SOURCE[1]:-$0}" "$bin" "$subcommand" "$@"
    fi

    printf '%s\n\n' "[ERROR] loom-daemon not found (needed for '$subcommand')." >&2
    echo "This script is a thin stub over the native \`loom-daemon $subcommand\`" >&2
    echo "subcommand (issue #4275). Provide a binary by either:" >&2
    echo "  - setting LOOM_DAEMON_SELF_BIN=/path/to/loom-daemon (the binary that IMPLEMENTS this subcommand, checked first) or LOOM_DAEMON_BIN=/path/to/loom-daemon, or" >&2
    if [[ -n "$repo_root" && -d "$repo_root/loom-daemon" ]]; then
        echo "  - building it: cargo build --release --manifest-path $repo_root/loom-daemon/Cargo.toml" >&2
    else
        echo "  - re-running the Loom installer, which provisions loom-daemon onto PATH" >&2
    fi
    exit "${LOOM_SCRIPT_HELPER_MISSING_RC:-1}"
}
