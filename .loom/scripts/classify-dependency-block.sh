#!/usr/bin/env bash
# classify-dependency-block.sh - Separate a TIMING finding from a MERITS finding
# in Champion's proposal-escalation decision (issue #5664).
#
# THE FAILURE MODE THIS EXISTS FOR
#
# Champion escalates a proposal to `loom:operator-only` once it has been
# evaluated N times (default 2) without being revised
# (champion-issue-promo.md -> "Bounding the silent skip"). That rule is right
# for a proposal rejected on its MERITS: the author has been told what is wrong
# and has not acted, so a human has to decide.
#
# It is wrong for a proposal whose only failing finding is "criterion 2: hard
# dependency on #N, which is still open". That is not a merits finding, it is a
# TIMING finding, and it clears itself the moment #N closes. Escalating on it
# converts a transient state into a permanent one, because the asymmetry is
# total:
#
#   * `loom:operator-only` makes Champion skip the issue on every later pass;
#   * so the only actor that could ever notice "the blocker closed, this is
#     promotable now" is the actor that has been told to ignore it.
#
# The observed incident: one architect pass filed five proposals, three of which
# hard-depended on a fourth. Champion escalated all three for the open
# dependency; the dependency merged MINUTES later; the three sat at
# `loom:operator-only` with a stated reason that had become false and no
# mechanism to re-evaluate them. The repo returned to zero dispatchable work
# while holding three ready-to-run proposals.
#
# This script answers the two questions that fix requires, as one testable unit
# instead of as prose judgement inside the role file:
#
#   --check-defer       "Would this escalation be for a self-clearing open
#                        dependency rather than for the proposal's merits?"
#                        -> DEFER (do not escalate; wait for the blocker)
#   --check-unescalate  "Is this ALREADY-escalated proposal one whose recorded
#                        blocker has since closed?"  -> UNESCALATE (remove
#                        `loom:operator-only` and let it be evaluated again)
#
# Both are deliberately CONSERVATIVE in the same direction: a finding is treated
# as dependency-attributable only when it both names a dependency and cites an
# issue/PR reference. Anything else - any merits criterion, an unreadable
# blocker, a genuine dependency CYCLE (which cannot self-clear and is owned by
# `detect-dependency-cycle.sh`) - falls through to the unchanged escalation
# path. Deferring wrongly costs one more waiting proposal; un-escalating wrongly
# costs a human decision being re-automated, so that side is stricter still.
#
# SUB-ISSUE GRANULARITY (#5664, "recurred after closure"). Both questions above
# are about the WHOLE issue. A proposal can declare a `## Startable Subset` (see
# detect-startable-subset.sh) naming part of its work that does not depend on
# the open blocker(s) at all -- an explicit split point the architect stated so
# a Builder could land the unblocked half first. Discarding that split and
# parking the whole issue is a DIFFERENT bug from the timing/merits one above:
# the work was never blocked, only mis-classified at the wrong granularity. Both
# modes below check for it, ahead of the open-blocker outcome:
#
#   --check-defer       an open blocker + a declared startable subset ->
#                        PROMOTE_SUBSET (promote now, scoped to the subset)
#                        instead of DEFER (wait for the whole issue)
#   --check-unescalate   a still-open blocker + a declared startable subset ->
#                        UNESCALATE (with SUBSET_CARVEOUT: yes) instead of
#                        NO_UNESCALATE/blocker-still-open -- this is what heals
#                        an issue that was ALREADY mis-parked before this fix
#                        landed, without waiting for the blocker to close
#
# A THIRD question, added by #7650, generalizes --check-unescalate beyond
# dependency findings to arbitrary FACT-CHECKABLE ones:
#
#   --check-fact-unescalate   "Have ALL the cited findings in this ALREADY-
#                              escalated proposal's escalation comment been
#                              independently re-verified as resolved (by the
#                              CALLER, not this script) against a named
#                              commit?" -> FACT_UNESCALATE (append a `##
#                              Revision` section, remove `loom:operator-only`,
#                              and let it be evaluated again)
#
# --check-unescalate's classification (dependency-word + issue/PR reference,
# resolved by checking the referenced issue/PR's forge state) is entirely
# mechanical -- it never needs judgement about what a finding actually means.
# A finding like "`layout/toolchain.json` still carries the old pin" or
# "`verification/_repo_utils.py` does not exist anywhere in this repo" is
# fact-checkable in exactly the same self-clearing sense, but verifying it
# requires READING THE REPO, which only the caller (Curator, an LLM role) can
# do. --check-fact-unescalate therefore does not re-derive truth itself: it
# takes the caller's per-finding verdicts via --resolutions-file and enforces
# the same safety-guard SHAPE --check-unescalate already established --
# own-marker-only, never a dependency cycle, all-or-nothing, a namespaced
# anti-refight marker -- around writes the caller could not safely make by
# hand (label removal + comment ordering, idempotency). See curator.md's "De-
# escalating Fact-Based Champion Escalations" for the procedure that supplies
# --resolutions-file.
#
# Usage:
#   classify-dependency-block.sh --issue <N> [--repo <owner/repo>] [options]
#   classify-dependency-block.sh --issue <N> --check-unescalate [--apply]
#   classify-dependency-block.sh --issue <N> --check-fact-unescalate \
#       --resolutions-file <path> --commit <sha> [--apply]
#
# Options:
#   --issue <N>            Issue number (required).
#   --repo <nwo>           owner/repo of --issue (default: the current repo's origin).
#   --check-defer          Default mode. Should Champion defer instead of escalating?
#   --check-unescalate     Should an already-escalated proposal be un-escalated
#                          (dependency-timing case)?
#   --check-fact-unescalate  Should an already-escalated proposal be un-
#                          escalated (arbitrary fact-checkable finding case,
#                          #7650)? Requires --resolutions-file and, for
#                          --apply, --commit.
#   --apply                (--check-unescalate / --check-fact-unescalate only)
#                          actually remove `loom:operator-only` and post one
#                          idempotent comment (--check-fact-unescalate also
#                          appends the `## Revision` body section). Without it
#                          the script is strictly read-only.
#   --resolutions-file <p> (--check-fact-unescalate only) one line per finding
#                          cited in the escalation comment, in the same order
#                          `extract_findings` emits them, each prefixed
#                          `RESOLVED: ` or `UNRESOLVED: ` followed by the
#                          evidence for that verdict. The line COUNT must match
#                          the finding count exactly -- a short file cannot
#                          silently approve a finding it never addressed.
#   --commit <sha>         (--check-fact-unescalate only) the commit the
#                          caller verified every finding against. Required
#                          even without --apply -- the anti-refight
#                          fingerprint is keyed on it, so the dry-run
#                          already-unescalated check needs it too. Named in
#                          the `## Revision` section and the confirming
#                          comment on --apply, and folded into the anti-
#                          refight fingerprint so re-verifying against a
#                          LATER commit is a genuine new attempt rather than
#                          a no-op.
#   --findings-file <p>    Read the findings from this file instead of fetching
#                          the relevant Champion comment (used by the role file
#                          when it already holds `$COMMENT_BODY`, and by tests).
#   --skip-cycle-check     Do not run detect-dependency-cycle.sh (only safe when
#                          the caller already ran the cycle gate this pass).
#   --no-cache             Read via plain `gh` instead of the `gh-cached` wrapper.
#   --help,-h              Show this help.
#
# Output (stdout, marker lines -- parseable by the Champion prose):
#   --check-defer:
#     DEFER                        + OPEN_BLOCKERS: o/r#3 ...
#                                  + BLOCKER_FINGERPRINT: <16 hex>
#     PROMOTE_SUBSET                + OPEN_BLOCKERS: o/r#3 ...
#                                  + STARTABLE_SUBSET: <the declared subset text>
#     REEVALUATE                   + REASON: blockers-cleared
#     NO_DEFER                     + REASON: <slug>
#   --check-unescalate:
#     UNESCALATE                   + CLEARED_BLOCKERS: o/r#3 ...      (blocker closed)
#                                  + BLOCKER_FINGERPRINT: <16 hex>
#                                  + UNESCALATED: o/r#5        (--apply only --
#                                    removes loom:operator-only AND, best-
#                                    effort, its loom:operator-blocked sub-kind
#                                    label if present, #5671)
#     UNESCALATE                   + SUBSET_CARVEOUT: yes             (blocker
#                                  + STILL_OPEN_BLOCKERS: o/r#3 ...    still open,
#                                  + BLOCKER_FINGERPRINT: <16 hex>     but a
#                                  + UNESCALATED: o/r#5                startable
#                                    (--apply only, same as above)     subset heals it)
#     NO_UNESCALATE                + REASON: <slug>
#   --check-fact-unescalate:
#     FACT_UNESCALATE              + VERIFIED_COMMIT: <sha>
#                                  + RESOLVED_COUNT: <n>
#                                  + FINGERPRINT: fact-<16 hex>
#                                  + UNESCALATED: o/r#5        (--apply only --
#                                    appends `## Revision`, removes
#                                    loom:operator-only AND, best-effort,
#                                    loom:operator-decision)
#     NO_FACT_UNESCALATE           + REASON: <slug>
#   Either mode (informational, never a verdict on its own):
#     UNREADABLE: o/r#9 ...
#
# Reason slugs: no-findings, merits-finding, no-recorded-blocker,
#   blockers-cleared, dependency-cycle, unreadable-blocker, blocker-still-open,
#   not-operator-only, no-escalation-record, cycle-escalation,
#   already-unescalated, apply-failed, missing-resolutions-file,
#   resolutions-mismatch, partial-resolution, missing-commit.
#
# Exit codes:
#   0 - the special action applies (DEFER / UNESCALATE / FACT_UNESCALATE)
#   1 - it does not apply; the caller proceeds exactly as it did before
#   2 - error (bad arguments, issue unreadable, missing jq)
#   3 - (--check-defer only) REEVALUATE: the findings were dependency-only and
#       every recorded blocker is now CLOSED, so the recorded verdict is stale.
#       Re-run the criteria instead of escalating on a finding that no longer holds.
#   4 - (--check-defer only) PROMOTE_SUBSET: the findings were dependency-only,
#       at least one blocker is still OPEN, but the issue declares a startable
#       subset independent of it (#5664) -- promote scoped to that subset
#       instead of deferring the whole issue.
#
# BOUNDED COST. One cached read of the issue, one cached read per DISTINCT
# referenced blocker (deduplicated), and - only when an open blocker is found -
# one bounded `detect-dependency-cycle.sh` walk that shares the same 30s cache.
# --check-fact-unescalate costs exactly one read (it never fetches blockers --
# resolution is supplied by the caller).
# A proposal with no dependency findings costs exactly one read.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Reuse the repo's ONE dependency-phrase parser (`parse_dependency_refs`, the
# #4508 vocabulary) plus its small set/hash helpers, rather than writing a
# second, divergent one. detect-dependency-cycle.sh guards its `main` on
# `BASH_SOURCE == $0`, so sourcing it only defines functions.
# shellcheck source=detect-dependency-cycle.sh
source "$SCRIPT_DIR/detect-dependency-cycle.sh"
# Reuse the sub-issue-granularity carve-out check (`has_startable_subset`,
# #5664) the same way -- also guards its `main` on `BASH_SOURCE == $0`.
# shellcheck source=detect-startable-subset.sh
source "$SCRIPT_DIR/detect-startable-subset.sh"

show_help() {
    sed -n '2,191p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# Default so the pure helpers above stay sourceable by tests without `main`
# having run (the script is `set -u`).
GH_READ="${GH_READ:-gh}"

# ---- defaults (declared AFTER the source above, which sets its own) ----
ISSUE=""
REPO_NWO=""
MODE="defer"
DO_APPLY=0
FINDINGS_FILE=""
RESOLUTIONS_FILE=""
COMMIT_SHA=""
SKIP_CYCLE_CHECK=0
NO_CACHE=0

ESCALATE_MARKER="<!-- champion:proposal-escalated -->"
CYCLE_MARKER_PREFIX="<!-- champion:dep-cycle:"
UNESCALATE_MARKER_PREFIX="<!-- champion:proposal-unescalated:"
# Namespaced separately from UNESCALATE_MARKER_PREFIX (#7650) -- this
# mechanism reverses a MERITS-shaped escalation (arbitrary fact-checkable
# findings), never a dependency-timing one, so its own idempotency/anti-
# refight marker must never collide with the dependency mechanism's, even for
# the same issue number.
FACT_UNESCALATE_MARKER_PREFIX="<!-- champion:proposal-unescalated-facts:"
REJECT_NEEDLE="Champion Review: NEEDS REVISION"
OPERATOR_ONLY_LABEL="loom:operator-only"
# The operator-only sub-kind (#5671) a dependency-only escalation is expected
# to carry when applied via champion-issue-promo.md's SUB_KIND selection.
# check_unescalate() drops this alongside the base label -- a sub-label must
# never outlive the base label it accompanies -- but never REQUIRES it (see
# "No backfill" in .loom/docs/label-state-machine.md: a pre-#5679 escalation
# never carried a sub-label at all, and this script must still heal those).
OPERATOR_BLOCKED_LABEL="loom:operator-blocked"
# The sub-kind a fact-checkable (non-dependency) escalation carries instead
# (#7650) -- champion-issue-promo.md's SUB_KIND selection uses
# loom:operator-decision as the safe default whenever a recurring finding
# isn't a pure dependency citation, which is exactly this script's fact-based
# case. check_fact_unescalate() drops this alongside the base label, best-
# effort, for the same "sub-label must never outlive the base label" reason.
OPERATOR_DECISION_LABEL="loom:operator-decision"

# =====================================================================
# Pure helpers (sourceable and unit-tested directly by
# tests/test-classify-dependency-block.sh -- no stubs, no forge)
# =====================================================================

# extract_findings <comment-body>
#
# Emit ONE findings item per line: the first contiguous bullet block in a
# Champion verdict/escalation comment, with wrapped continuation lines folded
# back onto their bullet. That block is the failing-criteria list in both
# comment shapes this repo posts:
#
#   NEEDS REVISION: intro prose, then the failing-criteria bullets, then
#                   `**Recommended actions:**` and a SECOND bullet list;
#   escalation:     `**Recurring findings:**`, then the recurring bullets,
#                   then closing prose.
#
# `**Recommended actions:**` terminates the scan outright -- those bullets are
# suggestions, not findings, and reading them as findings would let a
# "Recommended actions: link the blocking issue" line masquerade as a
# dependency finding.
extract_findings() {
    printf '%s\n' "$1" | awk '
        function flush() { if (cur != "") { print cur; cur = "" } }
        /^[[:space:]]*\*\*Recommended actions/ { flush(); exit }
        {
            if ($0 ~ /^[[:space:]]*[-*][[:space:]]/) { flush(); cur = $0; started = 1; next }
            if (started) {
                if ($0 ~ /^[[:space:]]+[^[:space:]]/) { cur = cur " " $0; next }
                flush(); exit
            }
        }
        END { flush() }
    '
}

# _extract_refs <text> <default-repo>
#
# Stage 2 ONLY of the shared parse: normalize every issue/PR reference found in
# already-selected text to `owner/repo#N`. This is deliberately NOT a second
# dependency-PHRASE parser -- `parse_dependency_refs` (sourced above) remains
# the sole owner of "which lines declare a dependency". This runs over findings
# bullets that have already been classified as dependency findings, where every
# reference present IS the blocker being cited.
_extract_refs() {
    local text="$1" default_repo="$2"
    printf '%s\n' "$text" \
        | grep -oE '([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#[0-9]+|https?://[^[:space:]),]+/(issues|pull)/[0-9]+' \
        | while IFS= read -r ref; do
            case "$ref" in
                http*)
                    local num="${ref##*/}"
                    local rest="${ref%/*/*}"        # https://<host>/<owner>/<repo>
                    rest="${rest#*://}"
                    rest="${rest#*/}"
                    [[ "$rest" == */* ]] && printf '%s#%s\n' "$rest" "$num"
                    ;;
                '#'*)
                    printf '%s#%s\n' "$default_repo" "${ref#\#}"
                    ;;
                *)
                    printf '%s\n' "$ref"
                    ;;
            esac
        done \
        | sort -u
}

# is_dependency_finding <one findings bullet>
#
# True (0) when the bullet is attributable to a DEPENDENCY rather than to the
# proposal's merits. Both halves are required:
#
#   1. a dependency word ("blocked by", "blocker", "depends on", "dependent
#      on", "dependency on"/"dependencies on"/"dependency of"/"dependencies
#      of", "requires", "prerequisite", "waiting on", "waits on", "cannot
#      start/proceed/begin ... until", "not startable/beginable until",
#      "must wait for/until"), and
#   2. an actual issue/PR reference.
#
# (2) is what keeps ordinary English out of the timing bucket: "Requires a
# migration plan" and "no dependency injection seam" are merits findings and
# cite nothing, so they still escalate. A finding that cannot name the thing it
# is waiting for is not a finding that can self-clear.
#
# The bare noun "dependency"/"dependencies" is deliberately EXCLUDED from (1) --
# only "dependenc(y|ies) on|of" (a verb-phrase-shaped usage, e.g. "hard
# dependency on #3") counts. A bare noun matches an English section heading
# just as readily as a real blocker (e.g. a quoted "Dependencies / references"
# heading, or "Dependencies section") with no dependency relationship implied
# at all -- that false positive incorrectly un-escalated #4196 (#6112).
# "dependent on" already covers the adjectival phrasing, so nothing is lost by
# requiring the noun form to be followed by its preposition.
#
# "cannot start until #N" / "cannot proceed until #N" and their kin (#7652)
# describe the exact same timing relationship as "blocked by #N" in different
# words -- a sequential-ordering finding, not a merits finding -- so they are
# included as their own phrase family rather than folded into "blocked".
#
# #7756: a phrase word ANYWHERE in the bullet plus a reference ANYWHERE in the
# bullet is not enough -- a bullet can narratively mention a phrase-list word
# (e.g. "prerequisite") while discussing an issue reference elsewhere in the
# same bullet, with no actual "blocked by"/"depends on"/"requires" framing
# tying the two together. Observed on #7431: "#7430 (... a **prerequisite**
# for any meaningful soak) merged only minutes before this evaluation, so no
# soak observation window has started yet" -- "prerequisite" explains why the
# soak hasn't started, it does not cite #7430 as a blocker of THIS proposal.
#
# So in addition to the two bullet-wide checks, require the reference to sit in
# a WINDOW next to a phrase match, not merely somewhere in the bullet. Which
# side of the phrase that window is on depends on the phrase family (#7784):
#
#   (a) Prepositional family ("blocked by", "depends on", "dependent on",
#       "dependenc(y|ies) on|of", "requires", "prerequisite", "waiting on",
#       "waits on", "cannot ... until", "not ...able until", "must wait
#       for|until") -- these are grammatically followed immediately by the
#       thing they name ("blocked by #N", "depends on #N", "requires #N"), so
#       the reference must appear within $_DEP_REF_WINDOW chars AFTER the
#       phrase. This is the #7756 rule, unchanged: it is what keeps the #7431
#       shape ("#7430 (... a prerequisite for any meaningful soak) merged
#       ...") classified on the merits.
#
#   (b) Bare verb/noun family ("blocks", "blocking", "blocker") -- these are
#       the exception the (a) reasoning does not cover. Their idiomatic word
#       order puts the reference BEFORE the phrase ("#N blocks this proposal",
#       "#N is the blocker here", "#N is still blocking this work") just as
#       often as after it ("this blocks on #N", "blocking dependency: #N"), so
#       for these three -- and ONLY these three -- a reference within
#       $_DEP_REF_LEAD_WINDOW chars BEFORE the phrase also counts.
#
# The leading window is deliberately much narrower than the trailing one: a
# reference far upstream of a bare "blocking" is narrative co-occurrence (the
# #7756 failure mode), not a citation. It is NOT applied to family (a), so
# widening it can never reopen the #7431 false positive that motivated #7756.
_DEP_REF_WINDOW=60
_DEP_REF_LEAD_WINDOW=30
is_dependency_finding() {
    local bullet="$1"
    local phrase_re='(blocked by|blocker|blocking|blocks|depends on|dependent on|dependenc(y|ies) (on|of)|requires|prerequisite|waiting on|waits on|cannot (start|proceed|begin)( work)? until|not (start|begin)able until|must wait (for|until))'
    local bare_phrase_re='(blocker|blocking|blocks)'
    local ref_re='([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#[0-9]+|https?://[^[:space:]),]+/(issues|pull)/[0-9]+'
    local windows

    printf '%s' "$bullet" | grep -qiE "$phrase_re" || return 1
    printf '%s' "$bullet" | grep -qE "$ref_re" || return 1

    # (a) reference AFTER any phrase (both families).
    windows="$(printf '%s' "$bullet" | grep -oiE "${phrase_re}.{0,${_DEP_REF_WINDOW}}")" || windows=""
    printf '%s' "$windows" | grep -qE "$ref_re" && return 0

    # (b) reference BEFORE a bare verb/noun phrase only.
    windows="$(printf '%s' "$bullet" | grep -oiE ".{0,${_DEP_REF_LEAD_WINDOW}}${bare_phrase_re}")" || windows=""
    printf '%s' "$windows" | grep -qE "$ref_re"
}

# _strip_premise_false <findings, one per line>
#
# Drops any bullet tagged `[premise-false]` -- champion-issue-promo.md's
# premise-false close gate vocabulary (#7657). That finding kind self-clears
# through its OWN separate gate (re-verifying the cited mechanical check
# against current `main`), never by waiting on a blocker, so it must not count
# as a disqualifying "merits" finding for --check-defer's all-or-nothing
# dependency classification below. #7904: previously a `[premise-false]`
# bullet mixed with a genuine open-dependency bullet made
# findings_are_dependency_only() fail (it is not itself dependency-shaped),
# so the set fell through to ordinary escalation instead of deferring -- the
# issue's own stated edge case ("a mixed premise-false + open-dependency
# finding set should still defer"). Scoped to check_defer() only: an
# ALREADY-escalated issue's --check-unescalate path is a different life-cycle
# question and is untouched.
_strip_premise_false() {
    printf '%s\n' "$1" | grep -v '^[[:space:]]*[-*][[:space:]]*\[premise-false\]'
}

# findings_are_dependency_only <findings, one per line>
# True (0) only when there is at least one finding and EVERY finding is
# dependency-attributable. A single merits finding disqualifies the whole set:
# the escalation would then be about the merits, and merits do not self-clear.
findings_are_dependency_only() {
    local findings="$1" line saw=0
    [[ -n "${findings//[[:space:]]/}" ]] || return 1
    while IFS= read -r line; do
        [[ -n "${line//[[:space:]]/}" ]] || continue
        saw=1
        is_dependency_finding "$line" || return 1
    done <<< "$findings"
    [[ "$saw" -eq 1 ]]
}

# _fingerprint <space-separated node list>
# Identity of a blocker SET (sorted, deduplicated) so the same set collapses to
# one marker no matter what order it was discovered in -- the same shape
# detect-dependency-cycle.sh uses for a cycle's node set.
_fingerprint() {
    local nodes
    # shellcheck disable=SC2086  # intentional word splitting
    nodes="$(printf '%s\n' $1 | sort -u | tr '\n' ' ')"
    printf '%s' "${nodes% }" | _sha256 | awk '{print substr($1, 1, 16)}'
}

# =====================================================================
# Forge reads
# =====================================================================

# _ref_state <owner/repo#N> -> OPEN | CLOSED | MERGED | UNKNOWN
#
# A referenced blocker may be a PR, not an issue (`gh issue view` is
# GraphQL-backed and cannot read a PR number), so a failed issue read falls back
# to `gh pr view` before concluding UNKNOWN. MERGED is reported as-is and
# treated as resolved by callers.
_ref_state() {
    local node="$1"
    local repo="${node%#*}" num="${node##*#}" st=""
    st="$("$GH_READ" issue view "$num" --repo "$repo" --json state --jq '.state' 2>/dev/null)"
    if [[ -z "$st" ]]; then
        st="$("$GH_READ" pr view "$num" --repo "$repo" --json state --jq '.state' 2>/dev/null)"
    fi
    printf '%s\n' "${st:-UNKNOWN}"
}

# _classify_refs <newline-separated nodes>
# Sets OPEN_REFS / RESOLVED_REFS / UNKNOWN_REFS (space-separated).
OPEN_REFS=""
RESOLVED_REFS=""
UNKNOWN_REFS=""
_classify_refs() {
    local node state
    OPEN_REFS=""; RESOLVED_REFS=""; UNKNOWN_REFS=""
    while IFS= read -r node; do
        [[ -n "$node" ]] || continue
        state="$(_ref_state "$node")"
        case "$state" in
            OPEN)            OPEN_REFS="$OPEN_REFS $node" ;;
            CLOSED|MERGED)   RESOLVED_REFS="$RESOLVED_REFS $node" ;;
            *)               UNKNOWN_REFS="$UNKNOWN_REFS $node" ;;
        esac
    done <<< "$1"
    OPEN_REFS="${OPEN_REFS# }"; RESOLVED_REFS="${RESOLVED_REFS# }"; UNKNOWN_REFS="${UNKNOWN_REFS# }"
}

# _has_cycle <issue-number> <repo>
# 0 when detect-dependency-cycle.sh reports a cycle. A cycle is the one
# dependency shape that CANNOT self-clear, so it must keep escalating through
# the existing gate rather than deferring here forever.
_has_cycle() {
    [[ "$SKIP_CYCLE_CHECK" -eq 1 ]] && return 1
    local args=(--issue "$1" --repo "$2")
    [[ "$NO_CACHE" -eq 1 ]] && args+=(--no-cache)
    "$SCRIPT_DIR/detect-dependency-cycle.sh" "${args[@]}" >/dev/null 2>&1
    [[ "$?" -eq 1 ]]
}

_no_defer()           { echo "NO_DEFER"; echo "REASON: $1"; exit 1; }
_no_unescalate()      { echo "NO_UNESCALATE"; echo "REASON: $1"; exit 1; }
_no_fact_unescalate() { echo "NO_FACT_UNESCALATE"; echo "REASON: $1"; exit 1; }

_print_unreadable() {
    [[ -n "$UNKNOWN_REFS" ]] && echo "UNREADABLE: $UNKNOWN_REFS"
    return 0
}

# _resolve_blockers <findings> -- sets BLOCKER_REFS from the findings, falling
# back to the issue body's own declared dependencies (the `## Dependencies`
# checklist / `Blocked by:` lines) when the findings prose cited none.
BLOCKER_REFS=""
# A proposal citing its own number is never a blocker on itself, and dropping
# self-references BEFORE testing emptiness is what makes the body fallback
# reachable: "this depends on #5 landing first" on issue #5 records no blocker
# at all, so the issue's own declared dependencies are the next best source.
_drop_self_ref() { grep -v "^$REPO_NWO#$ISSUE\$" || true; }
_resolve_blockers() {
    local findings="$1" body="$2"
    BLOCKER_REFS="$(_extract_refs "$findings" "$REPO_NWO" | _drop_self_ref)"
    if [[ -z "${BLOCKER_REFS//[[:space:]]/}" ]]; then
        BLOCKER_REFS="$(parse_dependency_refs "$body" "$REPO_NWO" | _drop_self_ref)"
    fi
}

# =====================================================================
# Mode: --check-defer
# =====================================================================
check_defer() {
    local issue_json body findings
    issue_json="$("$GH_READ" issue view "$ISSUE" --repo "$REPO_NWO" --json body,comments 2>/dev/null)"
    if [[ -z "$issue_json" ]]; then
        err "could not read $REPO_NWO#$ISSUE"
        exit 2
    fi
    body="$(printf '%s\n' "$issue_json" | jq -r '.body // ""')"

    local source_body
    if [[ -n "$FINDINGS_FILE" ]]; then
        source_body="$(cat "$FINDINGS_FILE")"
    else
        source_body="$(printf '%s\n' "$issue_json" \
            | jq -r --arg n "$REJECT_NEEDLE" \
                '[.comments[] | select(.body | contains($n))] | last | .body // ""')"
    fi
    [[ -n "${source_body//[[:space:]]/}" ]] || _no_defer "no-findings"

    findings="$(extract_findings "$source_body")"
    [[ -n "${findings//[[:space:]]/}" ]] || _no_defer "no-findings"

    # #7904: classify on the finding set with `[premise-false]`-tagged bullets
    # removed -- see _strip_premise_false() above for why those must not count
    # as a disqualifying merits finding here.
    local dep_check_findings
    dep_check_findings="$(_strip_premise_false "$findings")"
    if [[ -z "${dep_check_findings//[[:space:]]/}" ]]; then
        # Every finding was premise-false: nothing left to classify as a
        # dependency wait, but this is not a merits finding either. Fall
        # through to escalate (DEP_RC=1) so the caller's separate
        # premise-false close gate gets to run against the full, unfiltered
        # finding set.
        _no_defer "premise-false-only"
    fi

    # A single ordinary merits finding means the escalation is about the
    # merits. Unchanged behaviour: escalate.
    findings_are_dependency_only "$dep_check_findings" || _no_defer "merits-finding"

    _resolve_blockers "$dep_check_findings" "$body"
    [[ -n "${BLOCKER_REFS//[[:space:]]/}" ]] || _no_defer "no-recorded-blocker"

    _classify_refs "$BLOCKER_REFS"
    _print_unreadable

    if [[ -z "$OPEN_REFS" ]]; then
        # Dependency-only findings whose blockers have all closed: the recorded
        # verdict is stale, so escalating on it would escalate a finding that no
        # longer holds. Re-evaluate instead.
        echo "REEVALUATE"
        echo "REASON: blockers-cleared"
        [[ -n "$RESOLVED_REFS" ]] && echo "CLEARED_BLOCKERS: $RESOLVED_REFS"
        exit 3
    fi

    if _has_cycle "$ISSUE" "$REPO_NWO"; then
        # A cycle never resolves itself, so it is the one dependency shape that
        # SHOULD reach a human -- checked BEFORE the subset carve-out below.
        # detect-dependency-cycle.sh --report owns that routing; this mode only
        # declines to defer. A declared startable subset does not change this: a
        # cycle means the DEPENDENCY GRAPH itself needs a human decision, which
        # is a property of the blocker, not of how much of this issue's own work
        # is independent of it.
        _no_defer "dependency-cycle"
    fi

    # Sub-issue granularity (#5664): the blocker is open and not a cycle, but the
    # issue itself declares a subset of its work that does not depend on it. That
    # is not a reason to keep waiting -- promote now, scoped to the subset, in
    # place of deferring the whole issue.
    local startable
    startable="$(extract_startable_subset "$body")"
    if [[ -n "${startable//[[:space:]]/}" ]]; then
        echo "PROMOTE_SUBSET"
        echo "OPEN_BLOCKERS: $OPEN_REFS"
        echo "STARTABLE_SUBSET:"
        printf '%s\n' "$startable"
        exit 4
    fi

    echo "DEFER"
    echo "OPEN_BLOCKERS: $OPEN_REFS"
    echo "BLOCKER_FINGERPRINT: $(_fingerprint "$OPEN_REFS")"
    exit 0
}

# =====================================================================
# Mode: --check-unescalate
# =====================================================================
_apply_unescalation() {
    local fingerprint="$1" cleared="$2" mode="${3:-cleared}" subset="${4:-}"
    local marker="$UNESCALATE_MARKER_PREFIX$fingerprint -->"
    local body

    if [[ "$mode" == "subset" ]]; then
        # Assigned via `read`, NOT `"$(cat <<EOF ...)"` (#7508): bash 3.2 -- the
        # stock macOS /bin/bash -- does not skip heredoc bodies when scanning a
        # command substitution for its closing paren, so punctuation in the prose
        # below (here, the `#5664` issue reference inside parentheses) is misread
        # as opening a region it never closes: `bad substitution: no closing )`,
        # an EMPTY body, and a silently failed un-escalation. `read` never enters
        # that scan. It returns non-zero at EOF, hence `|| true`.
        #
        # `_apply_fact_unescalation` below was already converted; these two were
        # missed, which is why every `--apply` assertion in
        # tests/test-classify-dependency-block.sh failed on macOS while passing
        # on CI's bash 5 (#7930).
        IFS= read -r -d '' body <<EOF || true
**Champion: Un-escalating — a startable subset was never actually blocked**

This proposal was routed to \`$OPERATOR_ONLY_LABEL\` for a **timing** finding, not a
merits one: its only recurring finding was an open dependency. That dependency
($cleared) is still open, but this issue declares a **startable subset**
independent of it:

$subset

Parking the whole issue on a blocker that only covers part of its work was the
mistake, not the wait itself — un-escalating so the subset above can be
promoted and built now.

Removed \`$OPERATOR_ONLY_LABEL\` (and its \`$OPERATOR_BLOCKED_LABEL\` sub-kind
label, if present); this proposal returns to normal evaluation, scoped to the
startable subset until $cleared closes. Nothing here overrides a human
decision — if this proposal genuinely needs one, re-add the label (or state
the merits finding) and it will not be un-escalated again.

---
*Automated by Champion role (classify-dependency-block.sh, #5664)*
$marker
EOF
    else
        # Assigned via `read`, NOT `"$(cat <<EOF ...)"` (#7508): bash 3.2 -- the
        # stock macOS /bin/bash -- does not skip heredoc bodies when scanning a
        # command substitution for its closing paren, so punctuation in the prose
        # below (here, the `#5664` issue reference inside parentheses) is misread
        # as opening a region it never closes: `bad substitution: no closing )`,
        # an EMPTY body, and a silently failed un-escalation. `read` never enters
        # that scan. It returns non-zero at EOF, hence `|| true`.
        #
        # `_apply_fact_unescalation` below was already converted; these two were
        # missed, which is why every `--apply` assertion in
        # tests/test-classify-dependency-block.sh failed on macOS while passing
        # on CI's bash 5 (#7930).
        IFS= read -r -d '' body <<EOF || true
**Champion: Un-escalating — the recorded blocker has closed**

This proposal was routed to \`$OPERATOR_ONLY_LABEL\` for a **timing** finding, not a
merits one: its only recurring finding was an open dependency. That dependency has
since closed, so the stated reason for the escalation no longer holds.

**Cleared blockers**: $cleared

Removed \`$OPERATOR_ONLY_LABEL\` (and its \`$OPERATOR_BLOCKED_LABEL\` sub-kind
label, if present); this proposal returns to normal evaluation. Nothing
here overrides a human decision — if this proposal genuinely needs one, re-add
the label (or state the merits finding) and it will not be un-escalated again.

---
*Automated by Champion role (classify-dependency-block.sh, #5664)*
$marker
EOF
    fi

    # WRITE ORDER IS LOAD-BEARING -- label first, comment second.
    #
    # check_unescalate()'s idempotency guard keys on the presence of the marker
    # COMMENT, not on the actual label state. If the comment were posted first
    # and the label removal then failed (two independent `gh` calls: a transient
    # API/network error is enough), every later re-scan would find the marker,
    # short-circuit on "already-unescalated", and never retry the removal -- the
    # proposal would sit at $OPERATOR_ONLY_LABEL forever, carrying a comment
    # claiming it had been un-escalated. That is exactly the permanence failure
    # #5664 exists to eliminate, so it must not be reachable through the
    # self-healing path's own partial-failure case.
    #
    # Removing the label first makes both failure directions safe:
    #   - label removal fails  -> no comment is posted, so the next re-scan still
    #     sees the label, finds no marker, and RETRIES.
    #   - comment post fails   -> the state change that matters already landed;
    #     the next re-scan stops at "not-operator-only". Only the audit trail
    #     (and the anti-refight marker) is missing, which is the soft direction.
    if ! gh issue edit "$ISSUE" --repo "$REPO_NWO" --remove-label "$OPERATOR_ONLY_LABEL" >/dev/null 2>&1; then
        warn "could not remove $OPERATOR_ONLY_LABEL from $REPO_NWO#$ISSUE (no comment posted; a later pass will retry)"
        return 1
    fi
    # Best-effort, not fatal: a sub-label (#5671) must never outlive the base
    # label it accompanies, but a pre-#5679 escalation never carried one at all
    # ("No backfill" -- .loom/docs/label-state-machine.md), so "already absent"
    # is the common case here, not an error.
    gh issue edit "$ISSUE" --repo "$REPO_NWO" --remove-label "$OPERATOR_BLOCKED_LABEL" >/dev/null 2>&1 || true

    # Not fatal: the un-escalation itself already succeeded above. Reporting
    # apply-failed here would misstate the issue's real state (the label IS
    # gone), so warn and still report success.
    if ! gh issue comment "$ISSUE" --repo "$REPO_NWO" --body "$body" >/dev/null 2>&1; then
        warn "removed $OPERATOR_ONLY_LABEL from $REPO_NWO#$ISSUE but could not post the un-escalation comment (audit trail missing)"
    fi
    return 0
}

check_unescalate() {
    local issue_json body labels comments findings escalation

    issue_json="$("$GH_READ" issue view "$ISSUE" --repo "$REPO_NWO" --json body,labels,comments 2>/dev/null)"
    if [[ -z "$issue_json" ]]; then
        err "could not read $REPO_NWO#$ISSUE"
        exit 2
    fi
    body="$(printf '%s\n' "$issue_json" | jq -r '.body // ""')"
    labels="$(printf '%s\n' "$issue_json" | jq -r '[.labels[]?.name] | join(",")')"
    comments="$(printf '%s\n' "$issue_json" | jq -r '[.comments[]?.body] | join("\n")')"

    printf ',%s,' "$labels" | grep -qF ",$OPERATOR_ONLY_LABEL," || _no_unescalate "not-operator-only"

    # A cycle escalation is CORRECTLY permanent: the loop cannot resolve itself,
    # so a human still owns it. Never un-escalate one.
    printf '%s' "$comments" | grep -qF "$CYCLE_MARKER_PREFIX" && _no_unescalate "cycle-escalation"

    if [[ -n "$FINDINGS_FILE" ]]; then
        escalation="$(cat "$FINDINGS_FILE")"
    else
        escalation="$(printf '%s\n' "$issue_json" \
            | jq -r --arg m "$ESCALATE_MARKER" \
                '[.comments[] | select(.body | contains($m))] | last | .body // ""')"
    fi
    # Only Champion's OWN N=2 escalation is reversible here. A `loom:operator-only`
    # applied by a human, by the epic-aware blocker check, or by any other path
    # carries no such record and is left strictly alone.
    [[ -n "${escalation//[[:space:]]/}" ]] || _no_unescalate "no-escalation-record"

    findings="$(extract_findings "$escalation")"
    [[ -n "${findings//[[:space:]]/}" ]] || _no_unescalate "no-findings"
    findings_are_dependency_only "$findings" || _no_unescalate "merits-finding"

    _resolve_blockers "$findings" "$body"
    [[ -n "${BLOCKER_REFS//[[:space:]]/}" ]] || _no_unescalate "no-recorded-blocker"

    _classify_refs "$BLOCKER_REFS"
    _print_unreadable

    # Strict on this side: an unreadable blocker is not evidence that anything
    # cleared, and un-escalating is the higher-consequence direction.
    [[ -z "$UNKNOWN_REFS" ]] || _no_unescalate "unreadable-blocker"
    if [[ -n "$OPEN_REFS" ]]; then
        echo "STILL_OPEN: $OPEN_REFS"

        # Sub-issue granularity (#5664): the blocker is still open, so nothing
        # CLEARED -- but this issue may have been mis-parked at issue
        # granularity when only PART of it depends on that blocker. Heals an
        # issue that was already escalated before this carve-out existed,
        # without waiting for the blocker to close (unlike the "cleared"
        # UNESCALATE path below, which requires it).
        local startable
        startable="$(extract_startable_subset "$body")"
        if [[ -z "${startable//[[:space:]]/}" ]]; then
            _no_unescalate "blocker-still-open"
        fi

        # Namespaced separately from the "cleared" fingerprint below (prefixed
        # "subset:") so the two idempotency markers can never collide even if
        # the open-blocker set happens to match a past resolved-blocker set.
        local subset_fingerprint
        subset_fingerprint="subset-$(_fingerprint "$OPEN_REFS")"

        printf '%s' "$comments" | grep -qF "$UNESCALATE_MARKER_PREFIX$subset_fingerprint -->" \
            && _no_unescalate "already-unescalated"

        echo "UNESCALATE"
        echo "SUBSET_CARVEOUT: yes"
        echo "STILL_OPEN_BLOCKERS: $OPEN_REFS"
        echo "BLOCKER_FINGERPRINT: $subset_fingerprint"

        if [[ "$DO_APPLY" -eq 1 ]]; then
            if _apply_unescalation "$subset_fingerprint" "$OPEN_REFS" "subset" "$startable"; then
                echo "UNESCALATED: $REPO_NWO#$ISSUE"
            else
                echo "REASON: apply-failed"
                exit 1
            fi
        fi
        exit 0
    fi

    local fingerprint
    fingerprint="$(_fingerprint "$RESOLVED_REFS")"

    # Idempotency: this exact blocker set was already un-escalated once. If the
    # label is back, a human (or another mechanism) re-applied it deliberately -
    # do not fight them.
    #
    # This guard is safe ONLY because _apply_unescalation() removes the label
    # BEFORE posting this marker: a marker can therefore never exist for an
    # un-escalation that did not actually happen, so "marker present + label
    # present" unambiguously means someone re-applied the label after a
    # completed un-escalation. Do not reorder those two writes.
    printf '%s' "$comments" | grep -qF "$UNESCALATE_MARKER_PREFIX$fingerprint -->" \
        && _no_unescalate "already-unescalated"

    echo "UNESCALATE"
    echo "CLEARED_BLOCKERS: $RESOLVED_REFS"
    echo "BLOCKER_FINGERPRINT: $fingerprint"

    if [[ "$DO_APPLY" -eq 1 ]]; then
        if _apply_unescalation "$fingerprint" "$RESOLVED_REFS"; then
            echo "UNESCALATED: $REPO_NWO#$ISSUE"
        else
            echo "REASON: apply-failed"
            exit 1
        fi
    fi
    exit 0
}

# =====================================================================
# Mode: --check-fact-unescalate (#7650)
# =====================================================================

# _resolutions_summary <resolutions-file>
# Strips the leading "RESOLVED: " tag (every line is required to be RESOLVED
# by the time this is called -- partial sets never reach the apply path) so
# the evidence text can be folded directly into the body/comment prose.
_resolutions_summary() {
    sed -E 's/^RESOLVED:[[:space:]]*/- /' "$1"
}

# WRITE ORDER IS LOAD-BEARING, same rule as _apply_unescalation() above, with
# one extra write in front of it: body edit, THEN label removal, THEN comment.
#
#   - body edit fails    -> no label change, no comment; a later re-scan finds
#     the body unrevised and retries the whole sequence from the top.
#   - label removal fails -> the body already carries the `## Revision`
#     section (idempotency-guarded below so a retry does not double-append
#     it), but the label is still present; a later re-scan re-reads the
#     ALREADY-revised body, recomputes the SAME fingerprint (the fingerprint
#     is keyed to the escalation comment + commit, not the body), and retries
#     the label/comment steps.
#   - comment post fails  -> the state changes that matter (revision + label
#     removal) already landed; only the audit trail and anti-refight marker
#     are missing, the soft direction, exactly as in _apply_unescalation().
_apply_fact_unescalation() {
    local fingerprint="$1" body="$2"
    local marker="$FACT_UNESCALATE_MARKER_PREFIX$fingerprint -->"
    local revision_marker="<!-- curator:fact-revision:$fingerprint -->"
    local today summary
    today="$(date -u +%Y-%m-%d)"
    summary="$(_resolutions_summary "$RESOLUTIONS_FILE")"

    if ! printf '%s' "$body" | grep -qF "$revision_marker"; then
        local new_body
        # Assigned via `read`, NOT `"$(cat <<EOF ...)"`: bash 3.2 -- the stock
        # macOS /bin/bash, which `#!/usr/bin/env bash` resolves to -- does not
        # skip heredoc bodies when scanning a command substitution for its
        # closing paren. An apostrophe in the prose below ("Champion's") then
        # opens a quote that never closes, and the whole file fails `bash -n`
        # ~150 lines downstream, taking `check-shell-syntax.sh` and every
        # `resync-installed.sh` run on macOS with it (#7721). `read` returns
        # non-zero at EOF, hence `|| true`; the trailing-newline strip keeps
        # this byte-identical to the command substitution it replaces.
        IFS= read -r -d '' new_body <<EOF || true
$body

## Revision ($today)

Curator re-verified every objection Champion's escalation cited against
\`$COMMIT_SHA\` and found all of them resolved:

$summary

$revision_marker
EOF
        new_body="${new_body%$'\n'}"
        if ! gh issue edit "$ISSUE" --repo "$REPO_NWO" --body "$new_body" >/dev/null 2>&1; then
            warn "could not append the ## Revision section to $REPO_NWO#$ISSUE (no label change, no comment; a later pass will retry)"
            return 1
        fi
    fi

    if ! gh issue edit "$ISSUE" --repo "$REPO_NWO" --remove-label "$OPERATOR_ONLY_LABEL" >/dev/null 2>&1; then
        warn "could not remove $OPERATOR_ONLY_LABEL from $REPO_NWO#$ISSUE (no comment posted; a later pass will retry)"
        return 1
    fi
    # Best-effort, not fatal -- same "must never outlive the base label, but
    # never required" rule as _apply_unescalation()'s sub-label removal.
    gh issue edit "$ISSUE" --repo "$REPO_NWO" --remove-label "$OPERATOR_DECISION_LABEL" >/dev/null 2>&1 || true

    local comment_body
    # Assigned via `read`, NOT `"$(cat <<EOF ...)"`: bash 3.2 -- the stock
    # macOS /bin/bash, which `#!/usr/bin/env bash` resolves to -- does not
    # skip heredoc bodies when scanning a command substitution for its
    # closing paren. An apostrophe in the prose below ("Champion's") then
    # opens a quote that never closes, and the whole file fails `bash -n`
    # ~150 lines downstream, taking `check-shell-syntax.sh` and every
    # `resync-installed.sh` run on macOS with it (#7721). `read` returns
    # non-zero at EOF, hence `|| true`; the trailing-newline strip keeps
    # this byte-identical to the command substitution it replaces.
    IFS= read -r -d '' comment_body <<EOF || true
**Curator: De-escalating — every cited objection has resolved on \`main\`**

Champion escalated this proposal for repeated rejection without revision.
Every objection Champion's escalation cited has since been independently
re-verified against \`$COMMIT_SHA\`:

$summary

Appended a \`## Revision\` section naming the verifying commit (this changes
the body hash, the existing contract for "revised — evaluate again") and
removed \`$OPERATOR_ONLY_LABEL\` (and its \`$OPERATOR_DECISION_LABEL\`
sub-kind label, if present). This proposal returns to Champion's normal
evaluation queue. Nothing here overrides a human decision — if this proposal
genuinely needs one, re-add the label and it will not be de-escalated again
for the same finding set.

---
*Automated by Curator role (classify-dependency-block.sh --check-fact-unescalate, #7650)*
$marker
EOF
    comment_body="${comment_body%$'\n'}"
    if ! gh issue comment "$ISSUE" --repo "$REPO_NWO" --body "$comment_body" >/dev/null 2>&1; then
        warn "de-escalated $REPO_NWO#$ISSUE but could not post the confirming comment (audit trail missing)"
    fi
    return 0
}

check_fact_unescalate() {
    local issue_json body labels comments findings escalation

    issue_json="$("$GH_READ" issue view "$ISSUE" --repo "$REPO_NWO" --json body,labels,comments 2>/dev/null)"
    if [[ -z "$issue_json" ]]; then
        err "could not read $REPO_NWO#$ISSUE"
        exit 2
    fi
    body="$(printf '%s\n' "$issue_json" | jq -r '.body // ""')"
    labels="$(printf '%s\n' "$issue_json" | jq -r '[.labels[]?.name] | join(",")')"
    comments="$(printf '%s\n' "$issue_json" | jq -r '[.comments[]?.body] | join("\n")')"

    printf ',%s,' "$labels" | grep -qF ",$OPERATOR_ONLY_LABEL," || _no_fact_unescalate "not-operator-only"

    # A cycle escalation is correctly permanent -- same rule as check_unescalate().
    printf '%s' "$comments" | grep -qF "$CYCLE_MARKER_PREFIX" && _no_fact_unescalate "cycle-escalation"

    # Only Champion's OWN escalation is reversible here. No marker, no touch --
    # a label applied by a human or any other path carries no such record.
    escalation="$(printf '%s\n' "$issue_json" \
        | jq -r --arg m "$ESCALATE_MARKER" \
            '[.comments[] | select(.body | contains($m))] | last | .body // ""')"
    [[ -n "${escalation//[[:space:]]/}" ]] || _no_fact_unescalate "no-escalation-record"

    findings="$(extract_findings "$escalation")"
    [[ -n "${findings//[[:space:]]/}" ]] || _no_fact_unescalate "no-findings"

    [[ -n "$RESOLUTIONS_FILE" && -f "$RESOLUTIONS_FILE" ]] || _no_fact_unescalate "missing-resolutions-file"

    local n_findings n_resolutions n_unresolved
    n_findings=$(printf '%s\n' "$findings" | grep -c '.')
    n_resolutions=$(grep -cE '^(RESOLVED|UNRESOLVED):' "$RESOLUTIONS_FILE")
    # A resolutions file that does not address every cited finding 1:1 can
    # never approve -- a short file must not silently pass a finding it never
    # spoke to.
    [[ "$n_findings" -eq "$n_resolutions" ]] || _no_fact_unescalate "resolutions-mismatch"

    n_unresolved=$(grep -cE '^UNRESOLVED:' "$RESOLUTIONS_FILE")
    # Guard (4) from #7650: a PARTIAL resolution leaves the escalation in
    # place. All-or-nothing, same conservatism direction as check_unescalate().
    [[ "$n_unresolved" -eq 0 ]] || _no_fact_unescalate "partial-resolution"

    [[ -n "$COMMIT_SHA" ]] || _no_fact_unescalate "missing-commit"

    local fingerprint
    fingerprint="fact-$(printf '%s\n%s' "$escalation" "$COMMIT_SHA" | _sha256 | awk '{print substr($1, 1, 16)}')"

    # Idempotency / anti-refight, same shape and same safety argument as
    # check_unescalate()'s: the marker can only exist once the label removal
    # already landed (write order below), so "marker present + label present"
    # unambiguously means a human re-applied the label after a completed
    # de-escalation -- do not fight them.
    printf '%s' "$comments" | grep -qF "$FACT_UNESCALATE_MARKER_PREFIX$fingerprint -->" \
        && _no_fact_unescalate "already-unescalated"

    echo "FACT_UNESCALATE"
    echo "VERIFIED_COMMIT: $COMMIT_SHA"
    echo "RESOLVED_COUNT: $n_resolutions"
    echo "FINGERPRINT: $fingerprint"

    if [[ "$DO_APPLY" -eq 1 ]]; then
        if _apply_fact_unescalation "$fingerprint" "$body"; then
            echo "UNESCALATED: $REPO_NWO#$ISSUE"
        else
            echo "REASON: apply-failed"
            exit 1
        fi
    fi
    exit 0
}

# =====================================================================
# main
# =====================================================================
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --issue)             ISSUE="${2:-}"; shift 2 ;;
            --repo)              REPO_NWO="${2:-}"; shift 2 ;;
            --check-defer)       MODE="defer"; shift ;;
            --check-unescalate)  MODE="unescalate"; shift ;;
            --check-fact-unescalate) MODE="fact-unescalate"; shift ;;
            --apply)             DO_APPLY=1; shift ;;
            --resolutions-file)  RESOLUTIONS_FILE="${2:-}"; shift 2 ;;
            --commit)            COMMIT_SHA="${2:-}"; shift 2 ;;
            --findings-file)     FINDINGS_FILE="${2:-}"; shift 2 ;;
            --skip-cycle-check)  SKIP_CYCLE_CHECK=1; shift ;;
            --no-cache)          NO_CACHE=1; shift ;;
            --help|-h)           show_help; exit 0 ;;
            *)                   err "Unexpected argument: $1"; show_help >&2; exit 2 ;;
        esac
    done

    if [[ -z "$ISSUE" ]]; then
        err "Usage: classify-dependency-block.sh --issue <N> [--check-unescalate] [--apply]"
        exit 2
    fi
    if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
        err "--issue must be a number (got: $ISSUE)"
        exit 2
    fi
    if [[ "$DO_APPLY" -eq 1 && "$MODE" != "unescalate" && "$MODE" != "fact-unescalate" ]]; then
        err "--apply is only meaningful with --check-unescalate or --check-fact-unescalate"
        exit 2
    fi
    if [[ -n "$FINDINGS_FILE" && ! -f "$FINDINGS_FILE" ]]; then
        err "--findings-file not found: $FINDINGS_FILE"
        exit 2
    fi
    if [[ -n "$RESOLUTIONS_FILE" && "$MODE" != "fact-unescalate" ]]; then
        err "--resolutions-file is only meaningful with --check-fact-unescalate"
        exit 2
    fi
    if [[ -n "$COMMIT_SHA" && "$MODE" != "fact-unescalate" ]]; then
        err "--commit is only meaningful with --check-fact-unescalate"
        exit 2
    fi
    if ! command -v jq >/dev/null 2>&1; then
        err "jq is required"
        exit 2
    fi

    GH_READ="gh"
    if [[ "$NO_CACHE" -eq 0 ]]; then
        local ghc="$SCRIPT_DIR/gh-cached"
        if [[ -x "$ghc" ]] && "$ghc" --version >/dev/null 2>&1; then GH_READ="$ghc"; fi
    fi

    if [[ -z "$REPO_NWO" ]]; then
        REPO_NWO="$(_resolve_repo)"
    fi
    if [[ -z "$REPO_NWO" ]]; then
        err "could not determine the repo - pass --repo <owner/repo>"
        exit 2
    fi

    case "$MODE" in
        defer)           check_defer ;;
        unescalate)      check_unescalate ;;
        fact-unescalate) check_fact_unescalate ;;
    esac
}

# Only run main when executed directly (not when sourced by tests).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
