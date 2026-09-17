#!/usr/bin/env bash
# dep-recheck-fingerprint.sh - Deterministically compute the VERDICT/BLOCKERS/
# CONCLUSION_HASH fingerprints behind curator.md's "Re-check Idempotency"
# (#4986) and "Checking Operator-Only Premises" (#6849) sections (#7281).
#
# THIN STUB. The implementation is `loom-daemon dep-recheck-fingerprint` (Rust,
# `loom-daemon/src/dep_recheck/`) as of epic #7810 PR 4. This entry point
# survives because curator.md invokes it BY PATH and `eval`s its KEY=VALUE
# output. Subcommands, flags, output keys, quoting and exit codes are
# unchanged; see `loom-daemon/src/dep_recheck/cli.rs` for what is contract.
#
# WHY IT EXISTS
#
# Both curator.md sections used to define their CONCLUSION_HASH as inline bash
# embedded in the role prompt TEXT, re-derived independently by every Curator
# invocation from natural-language instructions rather than one canonical
# implementation. In production the fingerprint churned across dozens of
# distinct values on #6335/#6805 over weeks with an unchanged blocking
# condition, defeating the "never re-post an unchanged conclusion" guard and
# spamming near-duplicate comments.
#
# WHAT IT DOES NOT DO
#
# It does not compare against a prior marker or post anything. It answers "what
# did THIS pass conclude", deterministically; curator.md reads the emitted
# CONCLUSION_HASH against the most recent marker comment. The one exception is
# `decide`, which IS that comparison - extracted because it is pure arithmetic
# and because it determines whether a `loom:curating` claim is needed at all
# (#7617), which must be answerable BEFORE claiming.
#
# BLOCK_REASON and ORTHOGONAL (#6516) stay caller-supplied pass-throughs: both
# are judgement calls made by reading prose, not mechanical PR-state facts.
#
# Usage:
#   dep-recheck-fingerprint.sh dep-recheck (--number N [--repo OWNER/NAME] | --stdin)
#       [--verdict blocked|clear] [--block-reason TEXT] [--orthogonal ID] [--json]
#   dep-recheck-fingerprint.sh operator-premise (--refs "N1 N2 ..." [--repo OWNER/NAME] | --stdin)
#       [--json]
#   dep-recheck-fingerprint.sh named-dependency (--number N [--repo OWNER/NAME] | --stdin)
#       [--json]
#   dep-recheck-fingerprint.sh extract-refs (--number N [--repo OWNER/NAME] | --stdin)
#       [--bot-login LOGIN] [--json]
#   dep-recheck-fingerprint.sh decide --hash HASH [--prior-hash HASH]
#       [--prior-age-hours N] [--heartbeat-hours N] [--json]
#
# Subcommands:
#   dep-recheck        VERDICT (blocked|clear) + BLOCKERS, one
#                      "<pr#>:<state>:<block-label|no-block-label>:<conflicting|mergeable>"
#                      line per PR in `closedByPullRequestsReferences`. The
#                      label component is deliberately narrow (#7362) and an
#                      UNKNOWN merge state fails safe to conflicting (#7281),
#                      so ordinary review-cycle churn never moves the hash.
#   operator-premise   VERDICT (stale-premise|open) + REFS. CONCLUSION_HASH is
#                      left EMPTY when VERDICT=open: no comment this pass, so
#                      nothing to compare.
#   named-dependency   The `## Dependencies` checklist fingerprint (#7314) -
#                      the shape dep-recheck cannot see, a checklist item
#                      naming a different, non-closing prerequisite.
#   extract-refs       Reference extraction (#4963): the issue body always,
#                      plus any comment NOT authored by the automation
#                      identity and NOT carrying its own marker. That
#                      exclusion is what stops the self-perpetuating loop from
#                      #4507.
#   decide             The four-way decision (#7617): ACTION
#                      (none|skip|comment|heartbeat) and CLAIM (true|false).
#
# Exit codes:
#   0  evaluation completed (branch on VERDICT / CONCLUSION_HASH)
#   2  usage error, an unreadable issue, or no loom-daemon
#   3  missing dependency
#
# `eval`-safe like `claim-staleness.sh`: KEY=VALUE output is built only from a
# fixed enum, a hex hash and pre-sorted plain-text lines - never raw forge
# text - so no comment/PR body content can reach your shell via `eval`.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 2, matching this script's own "could not run" code. NOT the default 1: exit 1
# is not used here for errors at all, and a caller that branched on it would be
# reading a missing binary as a completed evaluation.
# shellcheck disable=SC2034  # read by loom_exec_script_helper, sourced below
LOOM_SCRIPT_HELPER_MISSING_RC=2

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib/script-helper.sh"

# Guarded so `source`ing this file is a no-op: a stub that exec'd on source
# would replace the sourcing shell and run the subcommand with ITS arguments.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    loom_exec_script_helper dep-recheck-fingerprint "$@"
fi
