#!/usr/bin/env bash
# detect-startable-subset.sh - Reads a proposal's declared "startable subset"
# carve-out (issue #5664, "recurred after closure").
#
# THIN STUB. The implementation is `loom-daemon detect-startable-subset` (Rust,
# `loom-daemon/src/dep_classify/subset.rs`) as of epic #7810 PR 3. This entry
# point survives because Champion's role prompts invoke it BY PATH and parse
# its stdout line-wise. Flags, stdout markers and exit codes are unchanged.
#
# WHAT IT ANSWERS
#
# Champion's dependency handling all answers questions about the WHOLE issue: is
# #N still open, is there a cycle, has the blocker closed. None of them can see
# a dependency that covers only PART of an issue's work. A proposal can state
# the split point explicitly:
#
#   ## Startable Subset
#
#   The comparator and mutation tests need only `warmup/01_netlist.v`, which is
#   already published upstream -- independent of the blocked RTL deliverable.
#
# Nothing read that declaration, so the whole issue was parked as one unit. This
# makes the declaration machine-readable, so the criterion-2 evaluation, the
# Step 4 dependency-timing gate and the Pass 0 re-scan all detect it the same
# way instead of re-reading free text three times.
#
# It does NOT decide whether the declared subset is genuinely independent of the
# blocker - that remains Champion's own judgement.
#
# Usage:
#   detect-startable-subset.sh --issue <N> [--repo <owner/repo>] [options]
#
# Options:
#   --repo <owner/repo>   Defaults to the checkout's origin remote.
#   --body-file <path>    Read the body here instead of from the forge.
#   --no-cache            Bypass the gh read cache.
#
# Exit codes:
#   0  STARTABLE_SUBSET, followed by the subset text
#   1  NO_STARTABLE_SUBSET - a finding, not an error
#   2  invalid arguments, an unreadable issue, or no loom-daemon

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 2, not the default 1: exit 1 here MEANS "no subset declared". A missing binary
# must not be read as a proposal having made no carve-out.
# shellcheck disable=SC2034  # read by loom_exec_script_helper, sourced below
LOOM_SCRIPT_HELPER_MISSING_RC=2

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# Guarded so `source`ing this file is a no-op, exactly as the retired
# implementation's `[[ "${BASH_SOURCE[0]}" == "${0}" ]]` guard on `main` was: a
# stub that exec'd on source would replace the sourcing shell and run the
# subcommand with ITS arguments.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    # requires-daemon: detect-startable-subset >= 0.19.99   #7952/#7953 — the Rust port added loom-daemon/src/cli/dep_classify.rs in 91ebce3ea, merged when VERSION read 0.19.98 (so 0.19.98 is the last version WITHOUT it); the post-merge bump that first shipped it was 0.19.99 (c718d1391). Hard, not `optional`: this stub only execs, it never probes or degrades. The refusal exits LOOM_SCRIPT_HELPER_MISSING_RC=2 set above, never 1 — 1 MEANS "no subset declared" here (#8484).
    loom_exec_script_helper detect-startable-subset "$@"
fi
