#!/usr/bin/env bash
# detect-dependency-cycle.sh - Finds closed loops in declared dependencies
# (issue #5671).
#
# THIN STUB. The implementation is `loom-daemon detect-dependency-cycle` (Rust,
# `loom-daemon/src/dep_classify/cycle.rs`) as of epic #7810 PR 3. This entry
# point survives because Champion's role prompts invoke it BY PATH and parse
# its stdout line-wise. Flags, stdout markers and exit codes are unchanged.
#
# WHAT IT ANSWERS
#
# Two issues can each declare the other as a blocker. Every pass re-derives
# "still blocked" on both, forever: waiting cannot resolve a cycle, so it is the
# one dependency shape that SHOULD reach a human. This walks the declared
# `Blocked by` / `Depends on` / `Requires` graph from one issue, bounded in
# depth, nodes and steps, and reports the first loop it closes.
#
# A truncated or partially-unreadable walk reports NO_CYCLE with the reason - it
# never invents one, and never claims completeness it did not have.
#
# Usage:
#   detect-dependency-cycle.sh --issue <N> [--repo <owner/repo>] [options]
#
# Options:
#   --repo <owner/repo>   Defaults to the checkout's origin remote.
#   --max-depth <N>       Default 4.
#   --max-nodes <N>       Default 25.
#   --max-steps <N>       Default 500.
#   --report              Post the cycle report and park the issue for an
#                         operator (idempotent on the cycle's fingerprint).
#   --no-cache            Bypass the gh read cache.
#
# Exit codes:
#   0  NO_CYCLE
#   1  CYCLE_DETECTED - a finding, not an error
#   2  invalid arguments, an unreadable root issue, or no loom-daemon

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 2, not the default 1: exit 1 here MEANS "cycle detected". A caller branching
# on the code alone would read a missing binary as a detected cycle and park a
# perfectly startable issue for a human.
# shellcheck disable=SC2034  # read by loom_exec_script_helper, sourced below
LOOM_SCRIPT_HELPER_MISSING_RC=2

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# Guarded so `source`ing this file is a no-op, exactly as the retired
# implementation's `[[ "${BASH_SOURCE[0]}" == "${0}" ]]` guard on `main` was: a
# stub that exec'd on source would replace the sourcing shell and run the
# subcommand with ITS arguments.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    # requires-daemon: detect-dependency-cycle >= 0.19.99   #7952/#7953 — the Rust port added loom-daemon/src/cli/dep_classify.rs in 91ebce3ea, merged when VERSION read 0.19.98 (so 0.19.98 is the last version WITHOUT it); the post-merge bump that first shipped it was 0.19.99 (c718d1391). Hard, not `optional`: this stub only execs, it never probes or degrades. The refusal exits LOOM_SCRIPT_HELPER_MISSING_RC=2 set above, never 1 — 1 MEANS "cycle detected" here (#8484).
    loom_exec_script_helper detect-dependency-cycle "$@"
fi
