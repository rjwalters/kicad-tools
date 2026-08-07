#!/usr/bin/env bash
# check-ingest-key-file.sh — validate that observability.ingestKeyFile
# resolves to a readable, non-foreign-home path on THIS host (issue #5336).
#
# Root cause this guards against: on 2026-08-04, a `loom-worker-2` (Linux,
# home `/home/ubuntu`) daemon reported observability as `disabled` because
# its resolved `ingestKeyFile` was `/Users/rwalters/.loom/observability/ingest.key`
# — a macOS path committed verbatim into the shared `.loom/config.json`,
# copied unmodified onto every host that pulled `main`. The real key file was
# sitting at `/home/ubuntu/.loom/observability/ingest.key` the whole time.
#
# This script re-derives the SAME resolution the daemon itself performs
# (env > config > $HOME-relative default — see
# `loom-daemon/src/observability/mod.rs::resolve_ingest_key_file`), then
# checks the result for exactly that defect class plus basic readability.
#
# Usage:
#   ./defaults/scripts/check-ingest-key-file.sh [repo_root]
#
#   repo_root defaults to the nearest ancestor of $PWD containing a `.loom/`
#   directory (falls back to $PWD when none is found — matching every other
#   config-resolver.sh caller's soft-fail-to-`{}` contract for a repo with no
#   config at all).
#
# Resolution precedence (highest first), mirroring the daemon exactly:
#   1. $LOOM_OBSERVABILITY_INGEST_KEY_FILE
#   2. observability.ingestKeyFile from the tiered config
#      (defaults/scripts/lib/config-resolver.sh: .loom-local/local.json >
#      .loom-project/project.json > .loom/config.json > private defaults)
#   3. $HOME/.loom/observability/ingest.key (the daemon's built-in default)
#
# Checks, in order:
#   a. Foreign-home path — the resolved path is an absolute path under
#      /Users/<x>, /home/<x>, or /root that names a DIFFERENT home directory
#      than this host's own $HOME. This is the exact #5336 defect class: a
#      value copied verbatim from a different host.
#   b. Readable — the resolved path names a file this process can read (the
#      minimum bar for the daemon, which runs as the same account in every
#      deployment this repo documents, to read it too).
#
# Exit codes:
#   0 - resolved path is readable and is not a foreign-home path (PASS)
#   1 - foreign-home path detected (FAIL)
#   2 - resolved path is not a readable file — missing, a directory, or
#       permission-denied (FAIL)
#
# This single script serves two roles in dashboard/docs/deploy-runbook.md:
#   - step 9c's provisioning-time readability gate (run right after writing a
#     host's key, before declaring that step done), and
#   - the fleet-wide regression check (run unmodified on every fleet host to
#     confirm none of them carries a foreign-home path — issue #5336's own
#     "fleet-wide check" acceptance criterion).
#
# Never a network call, never touches the key's contents (only whether the
# path is readable) — safe to run on a live host at any time.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/config-resolver.sh
source "$SCRIPT_DIR/lib/config-resolver.sh"

find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.loom" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "$PWD"
}

REPO_ROOT="${1:-$(find_repo_root)}"

# --- 1. Resolve ingestKeyFile: env > config > $HOME-relative default -------
resolved=""
source_desc=""

if [[ -n "${LOOM_OBSERVABILITY_INGEST_KEY_FILE:-}" ]]; then
    resolved="$LOOM_OBSERVABILITY_INGEST_KEY_FILE"
    source_desc="\$LOOM_OBSERVABILITY_INGEST_KEY_FILE"
else
    config_value="$(loom_config_get "$REPO_ROOT" "observability.ingestKeyFile" "")"
    if [[ -n "$config_value" ]]; then
        resolved="$config_value"
        source_desc="observability.ingestKeyFile (resolved config)"
    elif [[ -n "${HOME:-}" ]]; then
        resolved="$HOME/.loom/observability/ingest.key"
        source_desc="\$HOME-relative default"
    fi
fi

if [[ -z "$resolved" ]]; then
    echo "FAIL: could not resolve observability.ingestKeyFile — no env override, no config value, and \$HOME is unset" >&2
    exit 2
fi

# --- 2. Foreign-home check ---------------------------------------------------
home_prefix_regex='^(/Users/[^/]+|/home/[^/]+|/root)(/|$)'
if [[ -n "${HOME:-}" && "$resolved" =~ $home_prefix_regex ]]; then
    matched_home="${BASH_REMATCH[1]}"
    if [[ "$matched_home" != "$HOME" ]]; then
        echo "FAIL: $resolved (via $source_desc) names a DIFFERENT host's home ($matched_home), not this host's \$HOME ($HOME) — the #5336 defect class" >&2
        exit 1
    fi
fi

# --- 3. Readability check ----------------------------------------------------
if [[ ! -f "$resolved" || ! -r "$resolved" ]]; then
    echo "FAIL: $resolved (via $source_desc) is not a readable file" >&2
    exit 2
fi

echo "PASS: $resolved (via $source_desc) is readable and not a foreign-home path"
exit 0
