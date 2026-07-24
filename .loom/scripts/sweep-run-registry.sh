#!/usr/bin/env bash

# sweep-run-registry.sh - Stable per-sweep-run identity + lightweight peer registry.
#
# Purpose (#3768): give a single `/loom:sweep` invocation ONE stable run id that
# is fixed for the whole sweep, rather than the historical `sweep-$$` — which is
# the PID of each Bash *subshell* and therefore varies within a single sweep
# across tool calls. That instability meant:
#   - concurrent sweeps could not tell their own checkpoints apart from a peer's,
#   - the main-clean baseline path (a fixed constant) was clobbered when a second
#     sweep re-snapshotted it mid-run of the first.
#
# This helper provides:
#   - `new`     — generate + register a stable run id once, at sweep start.
#   - `peers`   — list OTHER live registered sweeps (dead-PID entries are pruned),
#                 so Stage -1 can print a loud, NON-BLOCKING peer-/sweep warning.
#   - `cleanup` — remove this run's own entry (and prune dead peers) at sweep end.
#   - `list`    — dump all registry entries (debug).
#
# The run id is portable (macOS/Linux, no `uuidgen`): a compact UTC timestamp, a
# PID component, and a random suffix, e.g.
#   sweep-20260722T231500Z-84213-a3f9c1
# It is a free-form string suitable for a checkpoint `task_id` and for embedding
# in a filename (charset restricted to [A-Za-z0-9-]).
#
# Registry entry (atomic write via .tmp + mv):
#   .loom/sweep-run/<RUN_ID>.json
#   {
#     "run_id": "<RUN_ID>",
#     "pid": <liveness PID>,
#     "timestamp": "<ISO 8601 UTC>"
#   }
#
# The "pid" is the LIVENESS handle for peer detection: `peers` treats an entry as
# a live peer only when `kill -0 <pid>` succeeds, and prunes the entry otherwise —
# the same pattern as the legacy `.loom/daemon-loop.pid` check. It defaults to the
# invoking process's parent PID ($PPID, i.e. the long-lived orchestrator/session
# process that outlives each ephemeral Bash tool call), overridable with `--pid`.
#
# Usage:
#   sweep-run-registry.sh new [--pid P]     # print a fresh RUN_ID, register it
#   sweep-run-registry.sh peers <RUN_ID>    # print live peers (one per line), prune dead
#   sweep-run-registry.sh cleanup <RUN_ID>  # remove own entry + prune dead peers
#   sweep-run-registry.sh list              # print all entries (run_id pid timestamp)
#
# `peers` output format (one live peer per line):
#   <run_id> <pid> <timestamp>
# Empty output means "no live peer sweeps" — the single-sweep (no-peer) case.
#
# Exit codes:
#   0 - success (including "no peers found")
#   1 - usage error

set -euo pipefail

usage() {
    sed -n '3,55p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

# Resolve repo root (handles invocation from worktree subdirs, mirroring
# sweep-checkpoint.sh so both helpers agree on where .loom/ lives).
repo_root() {
    git rev-parse --show-toplevel 2>/dev/null || pwd
}

registry_dir() {
    echo "$(repo_root)/.loom/sweep-run"
}

ensure_dir() {
    mkdir -p "$(registry_dir)"
}

iso_now() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

# Extract a string field value from a registry JSON file (no jq dependency).
json_field() {
    local file="$1" field="$2"
    sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$file" | head -n1
}

# Extract a numeric field value from a registry JSON file (no jq dependency).
json_num() {
    local file="$1" field="$2"
    sed -n "s/.*\"${field}\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p" "$file" | head -n1
}

# Generate a stable, portable, filename-safe run id.
gen_run_id() {
    local ts pidpart rand
    ts=$(date -u +"%Y%m%dT%H%M%SZ")
    pidpart="$$"
    # Two 16-bit RANDOM draws → 8 hex chars of entropy (bash builtin, portable).
    rand=$(printf '%04x%04x' "$((RANDOM))" "$((RANDOM))")
    echo "sweep-${ts}-${pidpart}-${rand}"
}

cmd_new() {
    local pid="${PPID:-$$}"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --pid)
                pid="${2:-}"
                shift 2
                ;;
            *)
                echo "ERROR: unknown flag '$1'" >&2
                exit 1
                ;;
        esac
    done
    if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
        echo "ERROR: --pid must be a positive integer (got: '$pid')" >&2
        exit 1
    fi

    local run_id target tmp
    run_id=$(gen_run_id)
    ensure_dir
    target="$(registry_dir)/${run_id}.json"
    tmp="${target}.tmp.$$"

    cat > "$tmp" <<EOF
{
  "run_id": "$run_id",
  "pid": $pid,
  "timestamp": "$(iso_now)"
}
EOF
    mv "$tmp" "$target"

    # The RUN_ID is the load-bearing output: the caller captures it and threads it
    # (as a literal) through every subsequent --task-id / baseline path in the sweep.
    echo "$run_id"
}

# Prune any entry whose recorded PID is no longer alive. Optionally skip a
# specific run id (the caller's own, handled separately).
prune_dead() {
    local skip="${1:-}"
    local dir file rid pid
    dir="$(registry_dir)"
    [[ -d "$dir" ]] || return 0
    for file in "$dir"/*.json; do
        [[ -e "$file" ]] || continue
        rid=$(json_field "$file" run_id)
        [[ -n "$skip" && "$rid" == "$skip" ]] && continue
        pid=$(json_num "$file" pid)
        if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$file"
        fi
    done
}

cmd_peers() {
    local self="${1:-}"
    if [[ -z "$self" ]]; then
        echo "ERROR: peers requires a RUN_ID argument" >&2
        exit 1
    fi
    local dir file rid pid ts
    dir="$(registry_dir)"
    [[ -d "$dir" ]] || return 0
    for file in "$dir"/*.json; do
        [[ -e "$file" ]] || continue
        rid=$(json_field "$file" run_id)
        # Skip our own entry.
        [[ "$rid" == "$self" ]] && continue
        pid=$(json_num "$file" pid)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            ts=$(json_field "$file" timestamp)
            echo "$rid $pid $ts"
        else
            # Dead peer — prune so it never produces a false-positive warning forever.
            rm -f "$file"
        fi
    done
}

cmd_cleanup() {
    local self="${1:-}"
    if [[ -z "$self" ]]; then
        echo "ERROR: cleanup requires a RUN_ID argument" >&2
        exit 1
    fi
    local target
    target="$(registry_dir)/${self}.json"
    rm -f "$target"
    # Opportunistically prune any dead peers too.
    prune_dead "$self"
}

cmd_list() {
    local dir file rid pid ts
    dir="$(registry_dir)"
    [[ -d "$dir" ]] || return 0
    for file in "$dir"/*.json; do
        [[ -e "$file" ]] || continue
        rid=$(json_field "$file" run_id)
        pid=$(json_num "$file" pid)
        ts=$(json_field "$file" timestamp)
        echo "$rid $pid $ts"
    done
}

main() {
    local cmd="${1:-}"
    shift || true
    case "$cmd" in
        new)     cmd_new "$@" ;;
        peers)   cmd_peers "$@" ;;
        cleanup) cmd_cleanup "$@" ;;
        list)    cmd_list "$@" ;;
        -h|--help|"") usage ;;
        *) echo "ERROR: unknown command '$cmd'" >&2; usage ;;
    esac
}

main "$@"
