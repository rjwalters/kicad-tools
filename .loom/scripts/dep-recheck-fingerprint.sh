#!/usr/bin/env bash
# dep-recheck-fingerprint.sh - Deterministically compute the VERDICT/BLOCKERS/
# CONCLUSION_HASH fingerprint behind curator.md's "Re-check Idempotency"
# (#4986) and "Checking Operator-Only Premises" (#6849) sections (#7281).
#
# WHY THIS EXISTS (#7281)
#
#   Both sections used to define their CONCLUSION_HASH as inline bash embedded
#   in the role prompt TEXT, re-derived independently by every Curator agent
#   invocation from natural-language instructions rather than one canonical
#   implementation — exactly the failure mode `claim-staleness.sh` (#6514) was
#   extracted to prevent for the sibling claim-staleness computation. In
#   production this let the fingerprint churn across dozens of distinct hash
#   values on #6335/#6805 over weeks with an unchanged blocking condition,
#   defeating the "never re-post an unchanged conclusion" guard and spamming
#   near-duplicate comments.
#
#   The specific non-determinism this script closes: a superseding-block PR
#   whose `mergeable`/`mergeStateStatus` is transiently `UNKNOWN` (GitHub has
#   not finished computing it yet) used to be read as "not conflicting", so a
#   PR that was blocking purely on merge-state (no blocking label) could
#   appear to "clear" for one pass and re-block the next, flipping VERDICT
#   back and forth with nothing about the PR actually changing. This script
#   fails safe instead: `UNKNOWN` on either field is treated the same as
#   `CONFLICTING`/`DIRTY` for merge-state purposes (the codebase's existing
#   "never stomp on missing data" convention — see `claim-staleness.sh`'s
#   `unknown` -> `fresh` fail-safe), so a real value flickering through
#   `UNKNOWN` and back does not change VERDICT, and therefore does not change
#   CONCLUSION_HASH, on its own.
#
# WHAT THIS SCRIPT DOES NOT DO
#
#   It does not compare against a prior marker, decide comment/skip/heartbeat,
#   or post anything — that four-way decision (see curator.md "Re-check
#   Idempotency") stays in curator.md, reading the emitted CONCLUSION_HASH
#   against the most recent `<!-- curator:dep-recheck:... -->` /
#   `<!-- curator:operator-premise-recheck:... -->` marker exactly as before.
#   This script only answers "what did THIS pass conclude", deterministically.
#
#   It also does not compute `BLOCK_REASON` (the free-text justification used
#   only when there is no linked PR at all, from the secondary heuristic) or
#   `ORTHOGONAL` (the diagnosed-but-orthogonal-blocker identity, #6516) —
#   both are judgment calls made by reading prose/comments, not mechanical
#   PR-state facts. Pass them through via `--block-reason` / `--orthogonal`
#   so they still fold into CONCLUSION_HASH exactly as the old inline formula
#   did; the script's job is to stop the *mechanical* half (VERDICT + BLOCKERS
#   from current PR/ref state) from being hand-rolled and drifting.
#
# Usage:
#   dep-recheck-fingerprint.sh dep-recheck (--number N [--repo OWNER/NAME] | --stdin)
#       [--verdict blocked|clear] [--block-reason TEXT] [--orthogonal ID] [--json]
#   dep-recheck-fingerprint.sh operator-premise (--refs "N1 N2 ..." [--repo OWNER/NAME] | --stdin)
#       [--json]
#
# Subcommands:
#   dep-recheck        The "Re-check Idempotency" fingerprint: VERDICT
#                       (blocked|clear), BLOCKERS (one "<pr#>:<state>:<sorted
#                       loom: labels>" line per PR in `closedByPullRequestsReferences`,
#                       sorted), and CONCLUSION_HASH.
#   operator-premise    The "Checking Operator-Only Premises" fingerprint:
#                       VERDICT (stale-premise|open), REFS (one "<ref#>:<state>"
#                       line per checked reference, sorted), and
#                       CONCLUSION_HASH — left EMPTY when VERDICT=open, per
#                       "no comment this pass" (nothing to report, nothing to
#                       compare).
#
# Input modes (either one, mutually exclusive):
#   --number N [--repo OWNER/NAME]   Live mode: fetch current PR/ref state via
#                                     `gh issue view` / `gh pr view`. `dep-recheck`
#                                     derives its own PR list from the issue's
#                                     `closedByPullRequestsReferences`;
#                                     `operator-premise` requires the caller's
#                                     already-extracted `--refs "N1 N2 ..."`
#                                     (this script does not parse issue body
#                                     text for `Blocked by #N` etc. — that
#                                     extraction stays in curator.md, tightly
#                                     coupled to the phrasings it recognizes).
#   --stdin                          Offline mode: read a JSON document on
#                                     stdin instead of calling `gh` (used by
#                                     the test suite, and available to any
#                                     caller that already has the PR/ref state
#                                     in hand). Shape:
#                                       dep-recheck:      {"prs": [{"number":N,
#                                         "state":"OPEN","labels":["..."],
#                                         "mergeable":"CONFLICTING",
#                                         "mergeStateStatus":"DIRTY"}, ...]}
#                                       operator-premise: {"refs": [{"number":N,
#                                         "state":"OPEN"}, ...]}
#
# Options:
#   --verdict blocked|clear   `dep-recheck` only: override the mechanically
#                             computed VERDICT. Required when `prs` is empty
#                             and the true verdict comes from the secondary
#                             heuristic (no linked PR at all) rather than PR
#                             state — the script cannot infer that case on its
#                             own. Ignored (a no-op) when omitted and `prs` is
#                             non-empty: the mechanical computation stands.
#   --block-reason TEXT       `dep-recheck` only: folded into CONCLUSION_HASH
#                             verbatim, empty by default. Only meaningful
#                             alongside an empty `prs` list (the secondary
#                             heuristic path) — see curator.md.
#   --orthogonal ID           `dep-recheck` only: the diagnosed-but-orthogonal
#                             condition's stable identity (#6516), folded into
#                             CONCLUSION_HASH verbatim, empty by default (the
#                             ordinary case — every existing fingerprint is
#                             unaffected when this is empty).
#   --repo OWNER/NAME         Target repo for live mode (default: the cwd's
#                             git remote).
#   --json                    Emit a JSON object instead of KEY=VALUE lines.
#
# Exit codes:
#   0  evaluation completed (branch on VERDICT / CONCLUSION_HASH)
#   2  usage error
#   3  missing dependency (gh or jq)
#
# `eval`-safe like `claim-staleness.sh`: KEY=VALUE output is built only from a
# fixed enum, a hex hash and pre-sorted plain-text lines — never raw forge
# text — so no comment/PR body content can reach your shell via `eval`.

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

_usage() {
    # Keep this range in sync with the header comment block above.
    sed -n '/^# Usage:/,/^# text — so no comment.*body content can reach your shell via .eval.\.$/p' "$0" | sed 's/^# \{0,1\}//'
}

_die() {
    echo "$SCRIPT_NAME: $1" >&2
    exit "${2:-2}"
}

SUBCOMMAND="${1:-}"
case "$SUBCOMMAND" in
    dep-recheck | operator-premise) shift ;;
    -h | --help)
        _usage
        exit 0
        ;;
    "") _die "missing subcommand (dep-recheck | operator-premise); see --help" ;;
    *) _die "unknown subcommand '$SUBCOMMAND' (dep-recheck | operator-premise)" ;;
esac

NUMBER=""
REPO_ARG=""
REFS_ARG=""
USE_STDIN=false
VERDICT_OVERRIDE=""
BLOCK_REASON=""
ORTHOGONAL=""
JSON_OUTPUT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --number)
            NUMBER="${2:-}"
            shift 2
            ;;
        --repo)
            REPO_ARG="${2:-}"
            shift 2
            ;;
        --refs)
            REFS_ARG="${2:-}"
            shift 2
            ;;
        --stdin)
            USE_STDIN=true
            shift
            ;;
        --verdict)
            VERDICT_OVERRIDE="${2:-}"
            shift 2
            ;;
        --block-reason)
            BLOCK_REASON="${2:-}"
            shift 2
            ;;
        --orthogonal)
            ORTHOGONAL="${2:-}"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        -h | --help)
            _usage
            exit 0
            ;;
        *) _die "unknown option '$1'" ;;
    esac
done

command -v jq >/dev/null 2>&1 || _die "jq not found on PATH" 3

if [[ "$USE_STDIN" == true ]]; then
    [[ -z "$NUMBER" ]] || _die "--stdin and --number are mutually exclusive"
    [[ -z "$REFS_ARG" ]] || _die "--stdin and --refs are mutually exclusive"
elif [[ "$SUBCOMMAND" == "dep-recheck" ]]; then
    [[ -n "$NUMBER" ]] || _die "one of --number or --stdin is required"
    [[ "$NUMBER" =~ ^[0-9]+$ ]] || _die "--number must be a positive integer (got '$NUMBER')"
    command -v gh >/dev/null 2>&1 || _die "gh CLI not found on PATH" 3
else
    # operator-premise's live mode fetches each --refs number independently;
    # it never needs the parent issue's own number.
    [[ -n "$REFS_ARG" ]] || _die "one of --refs or --stdin is required"
    command -v gh >/dev/null 2>&1 || _die "gh CLI not found on PATH" 3
fi

if [[ -n "$VERDICT_OVERRIDE" && "$VERDICT_OVERRIDE" != "blocked" && "$VERDICT_OVERRIDE" != "clear" ]]; then
    _die "--verdict must be 'blocked' or 'clear' (got '$VERDICT_OVERRIDE')"
fi

REPO_FLAG=()
[[ -n "$REPO_ARG" ]] && REPO_FLAG=(--repo "$REPO_ARG")

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256
    else
        cksum
    fi
}

# --- dep-recheck -------------------------------------------------------------

_fetch_dep_recheck_json() {
    local issue_json pr_nums pr_json pr
    issue_json="$(gh issue view "$NUMBER" "${REPO_FLAG[@]}" --json closedByPullRequestsReferences)" ||
        _die "gh issue view $NUMBER failed — cannot compute a fingerprint from a failed read (fail safe: never guess 'clear' on missing data)" 1
    pr_nums="$(printf '%s\n' "$issue_json" | jq -r '.closedByPullRequestsReferences[].number')" ||
        _die "unexpected response shape from gh issue view $NUMBER (missing closedByPullRequestsReferences)" 1
    pr_json="[]"
    if [[ -n "$pr_nums" ]]; then
        pr_json="["
        local first=true
        for pr in $pr_nums; do
            local one
            one="$(gh pr view "$pr" "${REPO_FLAG[@]}" --json number,state,labels,mergeable,mergeStateStatus)" ||
                _die "gh pr view $pr failed — cannot compute a fingerprint from a failed read" 1
            [[ "$first" == true ]] && first=false || pr_json+=","
            pr_json+="$one"
        done
        pr_json+="]"
    fi
    jq -n --argjson prs "$pr_json" '{prs: $prs}'
}

# One "<pr#>:<state>:<sorted loom: labels>" line per PR, sorted — matches the
# original inline formula exactly (ordering churn from the API never looks
# like a changed conclusion).
_dep_recheck_blockers() {
    jq -r '.prs | sort_by(.number) | .[]
        | "\(.number):\(.state):\([.labels[] | select(startswith("loom:"))] | sort | join(","))"' <<<"$1" | sort
}

# A PR blocks iff it is OPEN and either carries a block-bearing label, or its
# merge state is CONFLICTING/DIRTY, or (#7281 fix) its merge state is
# transiently UNKNOWN — fail-safe: treat "we don't know yet" the same as
# "still conflicting" rather than as "clear", so a value flickering through
# UNKNOWN and back does not flip VERDICT on its own.
_dep_recheck_verdict() {
    jq -r '
      [.prs[] | select(.state == "OPEN") | select(
          ([.labels[] | select(. == "loom:changes-requested" or . == "loom:blocked")] | length) > 0
          or (.mergeable == "CONFLICTING")
          or (.mergeStateStatus == "DIRTY" or .mergeStateStatus == "CONFLICTING")
          or (.mergeable == "UNKNOWN")
          or (.mergeStateStatus == "UNKNOWN")
      )] | length > 0
    ' <<<"$1" | grep -qx true && echo "blocked" || echo "clear"
}

_run_dep_recheck() {
    local input_json blockers verdict hash
    if [[ "$USE_STDIN" == true ]]; then
        input_json="$(cat)"
    else
        input_json="$(_fetch_dep_recheck_json)"
    fi
    jq -e '.prs' >/dev/null 2>&1 <<<"$input_json" || _die "input JSON must have a top-level 'prs' array"

    blockers="$(_dep_recheck_blockers "$input_json")"
    if [[ -n "$VERDICT_OVERRIDE" ]]; then
        verdict="$VERDICT_OVERRIDE"
    else
        verdict="$(_dep_recheck_verdict "$input_json")"
    fi
    hash="$(printf '%s\n%s\n%s\n%s' "$verdict" "$blockers" "$BLOCK_REASON" "$ORTHOGONAL" | _sha256 | awk '{print substr($1, 1, 16)}')"

    if [[ "$JSON_OUTPUT" == true ]]; then
        jq -n --arg verdict "$verdict" --arg blockers "$blockers" --arg reason "$BLOCK_REASON" \
            --arg orthogonal "$ORTHOGONAL" --arg hash "$hash" \
            '{verdict: $verdict, blockers: $blockers, block_reason: $reason, orthogonal: $orthogonal, conclusion_hash: $hash}'
    else
        echo "VERDICT=$verdict"
        echo "BLOCKERS=$blockers"
        echo "BLOCK_REASON=$BLOCK_REASON"
        echo "ORTHOGONAL=$ORTHOGONAL"
        echo "CONCLUSION_HASH=$hash"
    fi
}

# --- operator-premise ---------------------------------------------------------

_fetch_operator_premise_json() {
    local refs_json ref state
    refs_json="[]"
    for ref in $REFS_ARG; do
        # A reference is either an issue or a PR — try issue first, and only
        # fall back to `pr view` when that lookup itself fails (the expected
        # shape of "this reference turns out to be a PR, not an issue"). If
        # BOTH fail, that is a real read failure (bad number, API outage,
        # permissions) — die rather than silently defaulting to a status that
        # would misreport as "closed" (fail safe: never guess "stale-premise"
        # on missing data).
        state="$(gh issue view "$ref" "${REPO_FLAG[@]}" --json state --jq '.state' 2>/dev/null || true)"
        if [[ -z "$state" ]]; then
            state="$(gh pr view "$ref" "${REPO_FLAG[@]}" --json state --jq '.state' 2>/dev/null || true)"
        fi
        [[ -n "$state" ]] || _die "could not read state for reference #$ref (neither gh issue view nor gh pr view succeeded)" 1
        refs_json="$(jq --argjson n "$ref" --arg s "$state" '. + [{number: $n, state: $s}]' <<<"$refs_json")"
    done
    jq -n --argjson refs "$refs_json" '{refs: $refs}'
}

_operator_premise_refs() {
    jq -r '.refs | sort_by(.number) | .[] | "\(.number):\(.state)"' <<<"$1" | sort
}

_run_operator_premise() {
    local input_json refs verdict hash
    if [[ "$USE_STDIN" == true ]]; then
        input_json="$(cat)"
    else
        [[ -n "$REFS_ARG" ]] || _die "operator-premise live mode requires --refs \"N1 N2 ...\""
        input_json="$(_fetch_operator_premise_json)"
    fi
    jq -e '.refs' >/dev/null 2>&1 <<<"$input_json" || _die "input JSON must have a top-level 'refs' array"

    refs="$(_operator_premise_refs "$input_json")"
    if jq -e '[.refs[] | select(.state != "OPEN")] | length > 0' >/dev/null 2>&1 <<<"$input_json"; then
        verdict="stale-premise"
        hash="$(printf '%s\n%s' "$verdict" "$refs" | _sha256 | awk '{print substr($1, 1, 16)}')"
    else
        # Nothing to report: no comment this pass, so no hash is computed or
        # compared either (mirrors the dep-recheck "all clear" non-event).
        verdict="open"
        hash=""
    fi

    if [[ "$JSON_OUTPUT" == true ]]; then
        jq -n --arg verdict "$verdict" --arg refs "$refs" --arg hash "$hash" \
            '{verdict: $verdict, refs: $refs, conclusion_hash: $hash}'
    else
        echo "VERDICT=$verdict"
        echo "REFS=$refs"
        echo "CONCLUSION_HASH=$hash"
    fi
}

case "$SUBCOMMAND" in
    dep-recheck) _run_dep_recheck ;;
    operator-premise) _run_operator_premise ;;
esac
