#!/usr/bin/env bash
# spawn-generic-launch.sh - resolves a tier-3 runtime's launch shape from its
# capability manifest (`defaults/runtimes/<name>.json` / `.loom/runtimes/`)
# and execs the frozen `spawn-generic.sh` template with the resolved
# LOOM_GENERIC_* defaults applied (issue #8671).
#
# This is what a per-CLI `spawn-<runtime>.sh` wrapper (e.g. spawn-aider.sh)
# execs into, pinning only the runtime name -- onboarding another tier-3 CLI
# is now a `defaults/runtimes/<name>.json` "launch" edit plus one thin exec
# stub, not a new script carrying the CLI's own flag knowledge.
#
# `loom-daemon runtime-launch-env --runtime <name>` reads the manifest's
# `launch` object and prints one eval-ready line per declared field:
#
#     [ -n "${LOOM_GENERIC_CLI_BIN:-}" ] || LOOM_GENERIC_CLI_BIN="aider"; export LOOM_GENERIC_CLI_BIN
#
# i.e. "set VAR to this value ONLY WHEN VAR is currently unset or empty, then
# export it". Evaling them therefore gives the manifest DEFAULTS while any
# LOOM_GENERIC_* env var the caller already set always wins (env > config >
# default -- runtime-adapters.md's tier-3 section). The `export` is what
# carries them across the `exec` into spawn-generic.sh, which reads its
# environment, not this shell's plain variables.
# Exit 78 (EX_CONFIG) means the manifest's `launch` object carries
# an unrecognized key and is propagated as a hard failure -- a typo fails
# closed rather than being silently ignored. Any OTHER non-zero exit (no
# manifest, no bundled fallback, or `loom-daemon` predating this subcommand)
# is "no manifest data available", which splits two ways:
#
#   * The caller already pinned LOOM_GENERIC_CLI_BIN -> proceed silently. This
#     is the pre-#8671 pure-env-var path, unchanged, and it is what keeps a
#     from-source checkout usable before the first `cargo build`.
#   * Nothing pinned it -> refuse with exit 78 naming the daemon version
#     floor. WITHOUT this branch the run still fails, but with
#     spawn-generic.sh's bare "LOOM_GENERIC_CLI_BIN is required" -- which
#     names neither the stale binary that actually caused it nor the fix.
#     An unresolvable launch shape is a config error, and a config error
#     should say which config.
#
# `LOOM_GENERIC_EXTRA_ARGS` / `LOOM_GENERIC_MODEL_ENV` /
# `LOOM_GENERIC_EFFORT_FLAG` / `LOOM_GENERIC_EFFORT_VALUE_PREFIX` have no
# reader inside the frozen spawn-generic.sh (`settled` in
# scripts/shell-allowlist.txt -- zero fixes in six months, may shrink, never
# grow), so this script performs their mapping itself: extra args become
# ordinary argv tokens spawn-generic.sh's own passthrough-args loop already
# forwards verbatim, and the effort flag+value do the same. modelFlag needs
# no such translation -- LOOM_GENERIC_MODEL_FLAG is already spawn-generic.sh's
# own hook.
#
# requires-daemon: runtime-launch-env >= 0.19.384   #8671 — the launch-shape
# port. A resolved binary predating it exits non-zero with clap's
# "unrecognized subcommand", so the manifest supplies no defaults at all.
# Declared as a HARD floor rather than `optional` because the degrade is only
# graceful for a caller that pins LOOM_GENERIC_CLI_BIN itself: a manifest-only
# tier-3 runtime (spawn-aider.sh since #8671) has nothing left to fall back
# ON, so a stale binary is a hard failure however it is labelled — and a
# reviewer should see that this diff moves a fleet-wide floor. The number is
# this repo's VERSION as the port landed, which is the highest
# check-daemon-subcommand-versions.sh permits (a PR may never bump VERSION);
# the first RELEASE ARTIFACT carrying the subcommand is the post-merge bump
# immediately above it. The refusal below reads this line rather than
# restating the number, so there is no second copy to drift.
#
# Usage: spawn-generic-launch.sh <runtime-name> [args to spawn-generic.sh...]
set -euo pipefail

RUNTIME_NAME="${1:?spawn-generic-launch.sh requires a runtime name}"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/locate-daemon-bin.sh
source "$SCRIPT_DIR/lib/locate-daemon-bin.sh"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || pwd)"
BIN="$(loom_daemon_self_bin_override || loom_locate_daemon_bin "$REPO_ROOT")"

LAUNCH_RC=127
if [[ -n "$BIN" ]]; then
    set +e
    # --repo-root is NOT optional here (#8700). Without it the subcommand
    # falls back to resolving `<name>.json` from its own CWD, while the
    # daemon's runtime ADMISSION for the very same dispatch resolves it from
    # the scripts-derived workspace root -- so one dispatch could read two
    # different manifests. Passing the root this script already computed
    # makes the two agree: inside a linked worktree SCRIPT_DIR is
    # `<worktree>/.loom/scripts`, so `--show-toplevel` yields the worktree
    # root, matching what the CWD default produces in the case it was
    # designed for, while also being right when the daemon's cwd is
    # elsewhere (a custom `.loom/runtimes/<name>.json` was silently losing to
    # the compiled-in fallback, and a custom runtime with no bundled
    # fallback got the version-floor refusal blaming a stale binary).
    LAUNCH_ENV="$("$BIN" runtime-launch-env --runtime "$RUNTIME_NAME" --repo-root "$REPO_ROOT")"  # stderr is NOT eval'd
    LAUNCH_RC=$?
    set -e
    if [[ "$LAUNCH_RC" -eq 78 ]]; then
        echo "spawn-generic-launch($RUNTIME_NAME): manifest launch shape rejected (see above)" >&2
        exit 78
    elif [[ "$LAUNCH_RC" -eq 0 ]]; then
        eval "$LAUNCH_ENV"
    fi
fi

if [[ "$LAUNCH_RC" -ne 0 && -z "${LOOM_GENERIC_CLI_BIN:-}" ]]; then
    MIN_VER="$(sed -n 's|^# requires-daemon: runtime-launch-env >= \([0-9][0-9.]*\).*|\1|p;/^set -euo/q' "${BASH_SOURCE[0]}")"
    printf 'spawn-generic-launch(%s): could not resolve a launch shape, and LOOM_GENERIC_CLI_BIN is not set.\n  Resolved loom-daemon: %s (runtime-launch-env exit %s)\n  REMEDIATION: the launch shape for this runtime lives in its capability manifest (defaults/runtimes/%s.json, key "launch"),\n  which needs loom-daemon >= %s to read. Roll this host (.loom/scripts/cli/loom-daemon-update.sh --fetch), or build it\n  (cargo build --release -p loom-daemon) and export LOOM_DAEMON_BIN. To bypass the manifest entirely, set LOOM_GENERIC_CLI_BIN\n  (and LOOM_GENERIC_PROMPT_FLAG) yourself -- an already-set env var always wins over the manifest.\n' \
        "$RUNTIME_NAME" "${BIN:-<none found>}" "$LAUNCH_RC" "$RUNTIME_NAME" "${MIN_VER:-<undeclared>}" >&2
    exit 78
fi

EXTRA_ARGV=()
if [[ -n "${LOOM_GENERIC_EXTRA_ARGS:-}" ]]; then
    IFS=' ' read -r -a EXTRA_ARGV <<<"$LOOM_GENERIC_EXTRA_ARGS" || true
fi
if [[ -n "${LOOM_GENERIC_MODEL_ENV:-}" && -n "${LOOM_MODEL:-}" ]]; then
    export "${LOOM_GENERIC_MODEL_ENV}=${LOOM_MODEL}"
fi
if [[ -n "${LOOM_GENERIC_EFFORT_FLAG:-}" && -n "${LOOM_EFFORT:-}" ]]; then
    EXTRA_ARGV+=("$LOOM_GENERIC_EFFORT_FLAG" "${LOOM_GENERIC_EFFORT_VALUE_PREFIX:-}${LOOM_EFFORT}")
fi

LOOM_GENERIC_RUNTIME_NAME="$RUNTIME_NAME" \
    exec "$SCRIPT_DIR/spawn-generic.sh" ${EXTRA_ARGV[@]+"${EXTRA_ARGV[@]}"} "$@"
