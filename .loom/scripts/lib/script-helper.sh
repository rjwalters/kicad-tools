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

loom_exec_script_helper() {
    local subcommand="$1"
    shift

    # ${BASH_SOURCE[1]:-$0} hardens the caller-frame lookup the same way
    # run_loom_tool did (#3680): every call site is a bash-shebang'd script, so
    # BASH_SOURCE is populated, but the bare bashism would break if that changed.
    local script_dir repo_root bin
    script_dir="$(cd "$(dirname "${BASH_SOURCE[1]:-$0}")" && pwd)"
    repo_root="$(_lsh_find_repo_root "$script_dir")" || repo_root=""

    # shellcheck source=/dev/null
    source "$(dirname "${BASH_SOURCE[0]}")/locate-daemon-bin.sh"
    bin="$(loom_locate_daemon_bin "$repo_root")"

    if [[ -n "$bin" ]]; then
        exec "$bin" "$subcommand" "$@"
    fi

    echo "[ERROR] loom-daemon not found (needed for '$subcommand')." >&2
    echo "" >&2
    echo "This script is a thin stub over the native \`loom-daemon $subcommand\`" >&2
    echo "subcommand (issue #4275). Provide a binary by either:" >&2
    echo "  - setting LOOM_DAEMON_BIN=/path/to/loom-daemon, or" >&2
    if [[ -n "$repo_root" && -d "$repo_root/loom-daemon" ]]; then
        echo "  - building it: cargo build --release --manifest-path $repo_root/loom-daemon/Cargo.toml" >&2
    else
        echo "  - re-running the Loom installer, which provisions loom-daemon onto PATH" >&2
    fi
    exit 1
}
