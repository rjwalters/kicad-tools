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
#     "phase": "<curator-done|builder-done|judge-rejected|judge-done|doctor-done|merge-done>",
#     "task_id": "<stable per-sweep-run id>",
#     "timestamp": "<ISO 8601 UTC>",
#     "pr_number": <int or null>,
#     "attempt": <int, optional - omitted when not provided; absent means attempt 1>,
#     "model": "<string, optional - omitted when not provided; absent means default/unknown>",
#     "jev_tier": "<mechanical|routine|complex, optional>",
#     "jev_confidence": <float 0-1, optional - always paired with jev_tier>
#   }
#
# The "jev_tier"/"jev_confidence" pair (#8543) is a shadow-mode Jev (TypeSafe)
# complexity classification, merged onto an ALREADY-EXISTING checkpoint rather
# than written with the rest of the record: the Tier-2.5 dispatch step that
# produces it does not know the sweep's current phase/task-id, which a `write`
# would overwrite. Normally recorded in-process by `loom-daemon resolve-model
# --tier` (see `jev_tier::shadow_sample_from_env`); `jev <issue> <tier>
# <confidence>` below is the same merge exposed for a manual/out-of-band
# sample. Present only when `TYPESAFE_API_KEY` was set for the run; absent
# otherwise (the common case today). Never fed back into model selection.
#
# The "task_id" field (#3768) identifies the sweep RUN that wrote the checkpoint.
# It must be a STABLE per-sweep-run id (generated once at sweep start — see
# sweep-run-registry.sh), NOT the PID of a Bash subshell. The historical
# `sweep-$$` default was the wrong mental model: `$$` is re-evaluated per Bash
# tool call within a single sweep, so it could not distinguish one sweep's
# checkpoints from a concurrent peer sweep's. Callers (defaults/.claude/commands/
# loom/sweep.md) now pass a stable `--task-id "$RUN_ID"`; the field is free-form,
# so legacy checkpoints carrying a `sweep-<pid>` task_id still parse cleanly.
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
# "judge-rejected" records a successful request-changes verdict from the
# Judge that will be followed by another inline Doctor cycle (the initial
# rejection, or a re-rejection still under sweep.max_doctor_cycles). It
# REQUIRES `--pr-number` — the PR number is the durable routing key a
# resumed sweep uses to re-enter the Doctor phase directly, without
# repeating the Judge pass that already ran. Re-rejections should also
# carry `--attempt` matching the value the *next* `doctor-done` write will
# use, so resume re-enters the correct escalation cycle. When the
# doctor-cycle cap is reached and the PR is instead being blocked, do NOT
# write `judge-rejected` for that terminal rejection — leave the prior
# checkpoint as-is for the stale-checkpoint cleanup path.
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
#   sweep-checkpoint.sh jev <issue> <tier> <confidence>  # Patch jev_tier/jev_confidence onto an existing checkpoint (#8543); silent no-op if none exists yet
#   sweep-checkpoint.sh exists <issue>         # Exit 0 if checkpoint exists, 1 otherwise
#   sweep-checkpoint.sh list                   # List all checkpoint issue numbers
#
# Exit codes:
#   0 - success
#   1 - usage / not found
#   2 - invalid phase
#   3 - I/O error

# The native helper owns persistence and lifecycle tracing (#8525).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"
# Missing binary is an operational failure, never a missing checkpoint (1).
export LOOM_SCRIPT_HELPER_MISSING_RC=3
# requires-daemon: sweep-checkpoint >= 0.19.255  #8525 development floor: requires a build containing the Rust checkpoint port; first published release is unassigned. Version alone does not establish capability.
loom_exec_script_helper sweep-checkpoint "$@"
