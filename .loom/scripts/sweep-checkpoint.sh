#!/bin/bash

# sweep-checkpoint.sh - Manage per-issue phase checkpoints for /loom:sweep resume.
#
# This script provides read/write/delete operations on sweep checkpoint files:
#   .loom/sweep-checkpoint/issue-<N>.json
#
# The sweep skill calls this helper at each phase boundary to record progress.
# On re-entry (after a kill, OS reboot, or token exhaustion), the sweep skill
# reads the checkpoint for each issue and skips already-completed phases per
# the skip rules documented in defaults/.claude/commands/loom/sweep.md.
#
# Checkpoint file format (atomic write via .tmp + mv):
#   {
#     "phase": "<curator-done|builder-done|judge-done|doctor-done|merge-done>",
#     "task_id": "<task identifier, e.g. sweep PID>",
#     "timestamp": "<ISO 8601 UTC>",
#     "pr_number": <int or null>,
#     "attempt": <int, optional - omitted when not provided; absent means attempt 1>,
#     "model": "<string, optional - omitted when not provided; absent means default/unknown>"
#   }
#
# The "attempt" field (#3481) is forward-compat bookkeeping for model
# escalation: attempt 1 is the first Builder pass, attempt 2 is the Doctor
# dispatched after a Judge rejection. Readers MUST tolerate checkpoints
# without the field (legacy checkpoints predate it) and treat absence as
# attempt 1. The v1 escalation decision derives from the
# loom:changes-requested label/phase, not this counter.
#
# The "model" field (#3482, Phase 3a observability) records the model the
# orchestrator resolved for the phase's subagent (alias like "sonnet"/"opus"
# or a pinned ID like "claude-sonnet-4-6"). Observability only — it never
# feeds back into model selection. Readers MUST tolerate checkpoints without
# the field (legacy checkpoints predate it) and treat absence as
# default/unknown.
#
# Phases are recorded *after* successful completion of the corresponding
# lifecycle phase, so "curator-done" means the curator phase succeeded for
# this issue and the next sweep should skip it.
#
# On merge-done, callers should invoke `delete` to remove the checkpoint —
# stale-checkpoint detection (closed issue + leftover checkpoint) is performed
# inline by the sweep skill (see defaults/.claude/commands/loom/sweep.md), not
# by this helper, and the next sweep entry will clean it up with a warning.
#
# Usage:
#   sweep-checkpoint.sh write <issue> <phase> [--task-id ID] [--pr-number N] [--attempt N] [--model M]
#   sweep-checkpoint.sh read <issue>
#   sweep-checkpoint.sh delete <issue>
#   sweep-checkpoint.sh phase <issue>          # Print phase string only (or empty)
#   sweep-checkpoint.sh attempt <issue>        # Print attempt number (empty if absent = attempt 1)
#   sweep-checkpoint.sh model <issue>          # Print model string (empty if absent = default/unknown)
#   sweep-checkpoint.sh exists <issue>         # Exit 0 if checkpoint exists, 1 otherwise
#   sweep-checkpoint.sh list                   # List all checkpoint issue numbers
#
# Exit codes:
#   0 - success
#   1 - usage / not found
#   2 - invalid phase
#   3 - I/O error

set -euo pipefail

VALID_PHASES=(curator-done builder-done judge-done doctor-done merge-done)

usage() {
    sed -n '3,55p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

# Resolve repo root (handles invocation from worktree subdirs).
repo_root() {
    git rev-parse --show-toplevel 2>/dev/null || pwd
}

checkpoint_dir() {
    echo "$(repo_root)/.loom/sweep-checkpoint"
}

checkpoint_file() {
    local issue="$1"
    echo "$(checkpoint_dir)/issue-${issue}.json"
}

ensure_dir() {
    mkdir -p "$(checkpoint_dir)"
}

validate_issue() {
    local issue="$1"
    if [[ ! "$issue" =~ ^[0-9]+$ ]]; then
        echo "ERROR: issue must be a positive integer (got: '$issue')" >&2
        exit 1
    fi
}

validate_phase() {
    local phase="$1"
    for valid in "${VALID_PHASES[@]}"; do
        [[ "$phase" == "$valid" ]] && return 0
    done
    echo "ERROR: invalid phase '$phase'. Valid: ${VALID_PHASES[*]}" >&2
    exit 2
}

iso_now() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

cmd_write() {
    local issue="${1:-}" phase="${2:-}"
    shift 2 || true
    validate_issue "$issue"
    validate_phase "$phase"

    local task_id="" pr_number="null" attempt="" model=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --task-id) task_id="${2:-}"; shift 2 ;;
            --pr-number) pr_number="${2:-null}"; shift 2 ;;
            --attempt) attempt="${2:-}"; shift 2 ;;
            --model) model="${2:-}"; shift 2 ;;
            *) echo "ERROR: unknown flag '$1'" >&2; exit 1 ;;
        esac
    done

    [[ -z "$task_id" ]] && task_id="sweep-$$"
    if [[ "$pr_number" != "null" && ! "$pr_number" =~ ^[0-9]+$ ]]; then
        echo "ERROR: --pr-number must be a positive integer or 'null'" >&2
        exit 1
    fi
    if [[ -n "$attempt" && ! "$attempt" =~ ^[1-9][0-9]*$ ]]; then
        echo "ERROR: --attempt must be a positive integer >= 1 (got: '$attempt')" >&2
        exit 1
    fi
    # Model values are aliases (sonnet/opus/haiku) or pinned IDs
    # (claude-sonnet-4-6). Restrict the charset so the value embeds safely
    # in the hand-rolled JSON below (no quotes/backslashes/control chars).
    if [[ -n "$model" && ! "$model" =~ ^[A-Za-z0-9._-]+$ ]]; then
        echo "ERROR: --model must match [A-Za-z0-9._-]+ (got: '$model')" >&2
        exit 1
    fi

    # Optional fields: omitted entirely when not provided so legacy readers
    # (and diffs against old checkpoints) stay clean.
    local attempt_json=""
    [[ -n "$attempt" ]] && attempt_json=$',\n  "attempt": '"$attempt"
    local model_json=""
    [[ -n "$model" ]] && model_json=$',\n  "model": "'"$model"'"'

    ensure_dir
    local target tmp
    target="$(checkpoint_file "$issue")"
    tmp="${target}.tmp.$$"

    cat > "$tmp" <<EOF
{
  "phase": "$phase",
  "task_id": "$task_id",
  "timestamp": "$(iso_now)",
  "pr_number": $pr_number$attempt_json$model_json
}
EOF

    mv "$tmp" "$target"
    echo "wrote $target (phase=$phase)"
}

cmd_read() {
    local issue="${1:-}"
    validate_issue "$issue"
    local target
    target="$(checkpoint_file "$issue")"
    if [[ ! -f "$target" ]]; then
        return 1
    fi
    cat "$target"
}

cmd_phase() {
    local issue="${1:-}"
    validate_issue "$issue"
    local target
    target="$(checkpoint_file "$issue")"
    [[ ! -f "$target" ]] && return 0
    # Extract phase via grep+sed to avoid jq dependency.
    sed -n 's/.*"phase"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$target" | head -n1
}

cmd_attempt() {
    local issue="${1:-}"
    validate_issue "$issue"
    local target
    target="$(checkpoint_file "$issue")"
    [[ ! -f "$target" ]] && return 0
    # Empty output means the field is absent (legacy checkpoint) = attempt 1.
    sed -n 's/.*"attempt"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$target" | head -n1
}

cmd_model() {
    local issue="${1:-}"
    validate_issue "$issue"
    local target
    target="$(checkpoint_file "$issue")"
    [[ ! -f "$target" ]] && return 0
    # Empty output means the field is absent (legacy checkpoint) =
    # default/unknown model. Mirrors cmd_attempt semantics (#3482).
    sed -n 's/.*"model"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$target" | head -n1
}

cmd_exists() {
    local issue="${1:-}"
    validate_issue "$issue"
    [[ -f "$(checkpoint_file "$issue")" ]]
}

cmd_delete() {
    local issue="${1:-}"
    validate_issue "$issue"
    local target
    target="$(checkpoint_file "$issue")"
    if [[ -f "$target" ]]; then
        rm -f "$target"
        echo "deleted $target"
    fi
}

cmd_list() {
    local dir
    dir="$(checkpoint_dir)"
    [[ ! -d "$dir" ]] && return 0
    find "$dir" -maxdepth 1 -name 'issue-*.json' -type f 2>/dev/null \
        | sed -E 's|.*/issue-([0-9]+)\.json$|\1|' \
        | sort -n
}

main() {
    local cmd="${1:-}"
    shift || true
    case "$cmd" in
        write)   cmd_write "$@" ;;
        read)    cmd_read "$@" ;;
        phase)   cmd_phase "$@" ;;
        attempt) cmd_attempt "$@" ;;
        model)   cmd_model "$@" ;;
        exists)  cmd_exists "$@" ;;
        delete)  cmd_delete "$@" ;;
        list)    cmd_list "$@" ;;
        -h|--help|"") usage ;;
        *) echo "ERROR: unknown command '$cmd'" >&2; usage ;;
    esac
}

main "$@"
