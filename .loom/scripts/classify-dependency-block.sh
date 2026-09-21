#!/usr/bin/env bash
# classify-dependency-block.sh - Champion's timing-vs-merits split (issue #5664).
#
# THIN STUB. The implementation is `loom-daemon classify-dependency-block`
# (Rust, `loom-daemon/src/dep_classify/`) as of epic #7810 PR 3. This entry
# point survives because role prompts - champion-issue-promo.md,
# champion-reference.md, curator.md - invoke it BY PATH and parse its stdout
# line-wise. Flags, stdout markers and exit codes are unchanged; see
# `loom-daemon/src/dep_classify/cli.rs` for what is contract and why.
#
# WHAT IT ANSWERS
#
# Champion escalated a proposal to `loom:operator-only` after N=2 unrevised
# evaluations regardless of WHY it kept failing. For a proposal whose only
# finding was "hard dependency on #3, which is still open" that turned a
# self-clearing timing condition into a permanent one: `loom:operator-only`
# makes Champion skip the issue forever, so the only actor that could notice #3
# had closed was the one told to ignore it. This gate splits timing findings
# from merits findings, and reverses the escalation once the timing reason
# stops holding.
#
# Usage:
#   classify-dependency-block.sh --issue <N> [--repo <owner/repo>] [options]
#
# Modes (mutually exclusive; --check-defer is the default):
#   --check-defer              Should this proposal be parked on a blocker?
#   --check-unescalate         May a parked proposal be released because its
#                              blockers closed?
#   --check-fact-unescalate    May a parked proposal be released because a
#                              commit resolved every cited finding? (#7650)
#
# Options:
#   --repo <owner/repo>        Defaults to the checkout's origin remote.
#   --apply                    Perform the release, not just report it. Only
#                              meaningful with the two un-escalate modes.
#   --findings-file <path>     Read findings here instead of from comments.
#   --resolutions-file <path>  Per-finding RESOLVED:/UNRESOLVED: lines
#                              (--check-fact-unescalate only).
#   --commit <sha>             The commit that resolved them (same mode only).
#   --skip-cycle-check         Skip the bounded dependency-cycle walk.
#   --no-cache                 Bypass the gh read cache.
#
# Exit codes:
#   0  DEFER / UNESCALATE / FACT_UNESCALATE
#   1  NO_DEFER / NO_UNESCALATE / NO_FACT_UNESCALATE (a verdict, not an error)
#   2  invalid arguments, an unreadable issue, or no loom-daemon
#   3  REEVALUATE - every recorded blocker has closed (--check-defer)
#   4  PROMOTE_SUBSET - a startable subset is declared (--check-defer)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 2, not the default 1: this script's exit 1 means NO_DEFER / NO_UNESCALATE,
# which a caller acts on. A missing binary must never be mistaken for a verdict.
# shellcheck disable=SC2034  # read by loom_exec_script_helper, sourced below
LOOM_SCRIPT_HELPER_MISSING_RC=2

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# Guarded so `source`ing this file is a no-op, exactly as the retired
# implementation's `[[ "${BASH_SOURCE[0]}" == "${0}" ]]` guard on `main` was: a
# stub that exec'd on source would replace the sourcing shell and run the
# subcommand with ITS arguments.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    loom_exec_script_helper classify-dependency-block "$@"
fi
