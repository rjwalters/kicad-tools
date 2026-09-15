#!/usr/bin/env bash
# check-review-feedback.sh - Ingest a PR's FORMAL reviews and INLINE review
# threads, and report whether anything is outstanding against the current head
# (#7647).
#
# WHY THIS EXISTS
#
#   A Judge that reads only `/issues/{n}/comments` sees the PR's general
#   conversation and nothing else. Formal reviews live at
#   `/pulls/{n}/reviews`; inline, diff-anchored review comments live at
#   `/pulls/{n}/comments`; thread resolution state lives only in GraphQL's
#   `reviewThreads`. In kicad-tools#5369 a Judge approved a PR at
#   23a0289 while an unresolved formal CHANGES_REQUESTED review sat at that
#   exact same head, because the approving pass had read issue comments and
#   never `pulls/reviews`. Green CI did not exercise the missing requirement
#   either. Champion stopped the merge; nothing about the pipeline would have.
#
#   This script is the mechanical half of the fix: it makes "did anyone
#   formally request changes, and is that request still outstanding?" a
#   command with an exit code, rather than a thing an agent is asked to
#   remember. post-verdict.sh runs it on every `approved` verdict, so every
#   approval path — including the Docs-Only and conflict-only fast paths —
#   passes through it without each call site having to opt in.
#
# FAIL-CLOSED BY CONSTRUCTION
#
#   Incomplete pagination, an API failure, a missing dependency, or a review
#   state this script does not recognize are ALL reported as UNKNOWN — never
#   as "clean". The only way to reach CLEAR is a complete read that found
#   nothing outstanding. An older-head finding is NEVER auto-dismissed just
#   because the head moved: it is reported as NEEDS_RECONCILIATION, which the
#   caller clears with evidence of repair or explicit disposition.
#
# Usage:
#   check-review-feedback.sh --number N [options]
#
# Options:
#   --number N           PR number (required).
#   --head-sha SHA       Head SHA the verdict is about. Default: read live from
#                        the REST pulls endpoint (never GraphQL).
#   --repo OWNER/NAME    Target repo (default: derived from the git remote).
#   --findings-file PATH Where to write the human-readable finding list
#                        (default: a mktemp file; the path is always echoed as
#                        REVIEW_FEEDBACK_FINDINGS_FILE).
#   --json               Emit a JSON object instead of KEY=VALUE lines.
#   --quiet              Suppress the findings echo on stderr.
#
# Output (KEY=VALUE, safe for `eval`): every value is a fixed enum token, an
# integer, a hex SHA, a space-separated id list, or a path this script itself
# created. NO forge-authored text is ever emitted on stdout — review bodies go
# only to the findings file, which is data to read, never to eval.
#
#   REVIEW_FEEDBACK_STATE=CLEAR|BLOCKING|NEEDS_RECONCILIATION|UNKNOWN
#   REVIEW_HEAD_SHA=<sha or "">
#   REVIEWS_TOTAL=<int>
#   REVIEWS_BLOCKING_CURRENT_HEAD=<int>
#   REVIEWS_BLOCKING_OLDER_HEAD=<int>
#   REVIEWS_UNKNOWN_STATE=<int>
#   REVIEW_BLOCKING_IDS="<ids, space separated>"
#   INLINE_COMMENTS_TOTAL=<int>
#   INLINE_THREADS_UNRESOLVED=<int>
#   INLINE_THREADS_UNRESOLVED_OUTDATED=<int>
#   INLINE_BLOCKING_IDS="<thread/comment ids, space separated>"
#   INLINE_RESOLUTION_SOURCE=graphql|rest-unknown|none
#   REVIEW_FEEDBACK_FINDINGS_FILE=<path>
#
# STATE semantics:
#   CLEAR                 Complete read; no outstanding formal request and no
#                         unresolved inline thread. Safe to approve.
#   BLOCKING              An outstanding CHANGES_REQUESTED review, or an
#                         unresolved non-outdated inline thread, at the CURRENT
#                         head. Approval requires explicit reconciliation that
#                         cites the blocking review ids.
#   NEEDS_RECONCILIATION  Findings exist but are anchored to an older head, or
#                         their resolution state could not be established
#                         (GraphQL unavailable). Approval requires explicit
#                         evidence of repair or disposition — NOT an automatic
#                         dismissal because the head moved.
#   UNKNOWN               The read itself did not complete. Never evidence of
#                         "no blockers".
#
# Exit codes:
#   0  CLEAR
#   10 BLOCKING
#   11 NEEDS_RECONCILIATION
#   12 UNKNOWN (read failure / incomplete pagination / missing dependency)
#   2  usage error
#
# All reads are live and plain (`gh`, never gh-cached): this gates a verdict,
# and a 30s-old cache entry is exactly the window a new review lands in.

set -uo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_usage() {
    # Keep this range in sync with the header block above ("Usage:" through
    # the exit-code table).
    sed -n '34,89p' "$0" | sed 's/^# \{0,1\}//'
}

_die() {
    echo "$SCRIPT_NAME: $1" >&2
    exit "${2:-2}"
}

NUMBER=""
HEAD_SHA=""
REPO_ARG=""
FINDINGS_FILE=""
JSON_OUTPUT=false
QUIET=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --number)
            NUMBER="${2:-}"
            shift 2
            ;;
        --head-sha)
            HEAD_SHA="${2:-}"
            shift 2
            ;;
        --repo)
            REPO_ARG="${2:-}"
            shift 2
            ;;
        --findings-file)
            FINDINGS_FILE="${2:-}"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --quiet)
            QUIET=true
            shift
            ;;
        -h | --help)
            _usage
            exit 0
            ;;
        *) _die "unknown argument '$1'; see --help" ;;
    esac
done

[[ -n "$NUMBER" && "$NUMBER" =~ ^[0-9]+$ ]] || _die "a numeric --number is required"
if [[ -n "$HEAD_SHA" && ! "$HEAD_SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
    _die "--head-sha must be a 7-40 char lowercase hex string, got: '$HEAD_SHA'"
fi

if [[ -z "$FINDINGS_FILE" ]]; then
    FINDINGS_FILE="$(mktemp "${TMPDIR:-/tmp}/loom-review-feedback-${NUMBER}.XXXXXX")"
fi
: > "$FINDINGS_FILE"

# --- State accumulators ----------------------------------------------------
STATE="UNKNOWN"
REVIEWS_TOTAL=0
BLOCKING_CURRENT=0
BLOCKING_OLDER=0
REVIEWS_UNKNOWN_STATE=0
BLOCKING_IDS=""
INLINE_COMMENTS_TOTAL=0
INLINE_UNRESOLVED=0
INLINE_UNRESOLVED_OUTDATED=0
INLINE_BLOCKING_IDS=""
INLINE_SOURCE="none"
FAILURE_REASON=""

_emit_and_exit() {
    local code
    case "$STATE" in
        CLEAR) code=0 ;;
        BLOCKING) code=10 ;;
        NEEDS_RECONCILIATION) code=11 ;;
        *) code=12 ;;
    esac

    if [[ "$JSON_OUTPUT" == "true" ]]; then
        jq -n \
            --arg state "$STATE" \
            --arg head "$HEAD_SHA" \
            --argjson total "$REVIEWS_TOTAL" \
            --argjson blocking_current "$BLOCKING_CURRENT" \
            --argjson blocking_older "$BLOCKING_OLDER" \
            --argjson unknown_state "$REVIEWS_UNKNOWN_STATE" \
            --arg blocking_ids "$BLOCKING_IDS" \
            --argjson inline_total "$INLINE_COMMENTS_TOTAL" \
            --argjson inline_unresolved "$INLINE_UNRESOLVED" \
            --argjson inline_unresolved_outdated "$INLINE_UNRESOLVED_OUTDATED" \
            --arg inline_blocking_ids "$INLINE_BLOCKING_IDS" \
            --arg inline_source "$INLINE_SOURCE" \
            --arg findings_file "$FINDINGS_FILE" \
            --arg failure_reason "$FAILURE_REASON" \
            '{state:$state, head_sha:$head, reviews_total:$total,
              reviews_blocking_current_head:$blocking_current,
              reviews_blocking_older_head:$blocking_older,
              reviews_unknown_state:$unknown_state,
              blocking_ids:($blocking_ids | split(" ") | map(select(. != ""))),
              inline_comments_total:$inline_total,
              inline_threads_unresolved:$inline_unresolved,
              inline_threads_unresolved_outdated:$inline_unresolved_outdated,
              inline_blocking_ids:($inline_blocking_ids | split(" ") | map(select(. != ""))),
              inline_resolution_source:$inline_source,
              findings_file:$findings_file,
              failure_reason:$failure_reason}' 2>/dev/null \
            || printf '{"state":"UNKNOWN"}\n'
    else
        echo "REVIEW_FEEDBACK_STATE=$STATE"
        echo "REVIEW_HEAD_SHA=$HEAD_SHA"
        echo "REVIEWS_TOTAL=$REVIEWS_TOTAL"
        echo "REVIEWS_BLOCKING_CURRENT_HEAD=$BLOCKING_CURRENT"
        echo "REVIEWS_BLOCKING_OLDER_HEAD=$BLOCKING_OLDER"
        echo "REVIEWS_UNKNOWN_STATE=$REVIEWS_UNKNOWN_STATE"
        echo "REVIEW_BLOCKING_IDS=\"$BLOCKING_IDS\""
        echo "INLINE_COMMENTS_TOTAL=$INLINE_COMMENTS_TOTAL"
        echo "INLINE_THREADS_UNRESOLVED=$INLINE_UNRESOLVED"
        echo "INLINE_THREADS_UNRESOLVED_OUTDATED=$INLINE_UNRESOLVED_OUTDATED"
        echo "INLINE_BLOCKING_IDS=\"$INLINE_BLOCKING_IDS\""
        echo "INLINE_RESOLUTION_SOURCE=$INLINE_SOURCE"
        echo "REVIEW_FEEDBACK_FINDINGS_FILE=$FINDINGS_FILE"
    fi

    if [[ "$QUIET" != "true" && -s "$FINDINGS_FILE" ]]; then
        echo "$SCRIPT_NAME: findings for PR #$NUMBER ($STATE):" >&2
        sed 's/^/  /' "$FINDINGS_FILE" >&2
    fi

    exit "$code"
}

_fail_unknown() {
    FAILURE_REASON="$1"
    STATE="UNKNOWN"
    echo "UNKNOWN: $1" >> "$FINDINGS_FILE"
    _emit_and_exit
}

# --- Dependencies ----------------------------------------------------------
command -v jq > /dev/null 2>&1 || _fail_unknown "jq is not installed — review state could not be established"
command -v gh > /dev/null 2>&1 || _fail_unknown "gh is not installed — review state could not be established"

# shellcheck source=lib/forge-helpers.sh
if ! source "$SCRIPT_DIR/lib/forge-helpers.sh" 2>/dev/null; then
    _fail_unknown "could not source lib/forge-helpers.sh — review state could not be established"
fi
# forge-helpers.sh sets `-e` for its own callers. This script must NOT inherit
# it: every forge read here is deliberately checked and converted into an
# explicit UNKNOWN. Under `-e` a failed read would kill the process with a bare
# non-zero status and print nothing at all — a caller could not distinguish
# "read failed" from "nothing outstanding", which is the exact ambiguity this
# script exists to remove.
set +e
forge_detect > /dev/null 2>&1 || true

# --- Resolve the repo ------------------------------------------------------
NWO="$REPO_ARG"
if [[ -z "$NWO" ]]; then
    # Prefer the git remote: `gh repo view --json` is GraphQL-backed and dies
    # first under the exhaustion this read most needs to survive (#4659).
    NWO=$(git remote get-url origin 2>/dev/null \
        | sed -E 's|\.git$||; s|.*[:/]([^/]+/[^/]+)$|\1|') || NWO=""
    if [[ -z "$NWO" || "$NWO" != */* ]]; then
        NWO=$(forge_get_repo_nwo 2>/dev/null) || NWO=""
    fi
fi
[[ -n "$NWO" && "$NWO" == */* ]] || _fail_unknown "could not determine the repository (pass --repo OWNER/NAME)"

# --- Resolve the head SHA --------------------------------------------------
if [[ -z "$HEAD_SHA" ]]; then
    HEAD_SHA=$(gh api "repos/$NWO/pulls/$NUMBER" --jq '.head.sha' 2>/dev/null) || HEAD_SHA=""
    if [[ ! "$HEAD_SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
        HEAD_SHA=""
        _fail_unknown "could not read the PR head SHA — cannot tell current-head findings from older-head ones"
    fi
fi

# --- 1. Formal reviews (fully paginated, complete records) -----------------
if ! REVIEWS_NDJSON=$(forge_get_pr_reviews "$NWO" "$NUMBER" 2> /dev/null); then
    _fail_unknown "the formal-review read (pulls/$NUMBER/reviews) failed or paginated incompletely"
fi

REVIEW_SUMMARY=$(printf '%s\n' "$REVIEWS_NDJSON" | jq -s --arg head "$HEAD_SHA" '
    # Head association tolerates an abbreviated SHA on either side: a verdict
    # stamped with a 7-char sha must not make every finding look "older head".
    def at_head($h): .commit_id as $c
        | ($c != "") and ($c == $h or ($c | startswith($h)) or ($h | startswith($c)));
    map(select(. != null))
    | . as $all
    # The CURRENT position of a reviewer is their latest DECISIVE review.
    # COMMENTED never decides; PENDING was never submitted; DISMISSED is a real
    # forge state we respect (it was dismissed through the API, not by us).
    | ($all | sort_by(.id)
        | group_by(.author)
        | map([.[] | select(.state as $s | ["APPROVED","CHANGES_REQUESTED","DISMISSED"] | index($s) != null)] | last)
        | map(select(. != null))
        | map(select(.state == "CHANGES_REQUESTED"))) as $outstanding
    | {
        total: ($all | length),
        unknown_state: ([$all[] | select(.state as $s | ["APPROVED","CHANGES_REQUESTED","COMMENTED","DISMISSED","PENDING"] | index($s) == null)] | length),
        unknown_findings: [$all[]
            | select(.state as $s | ["APPROVED","CHANGES_REQUESTED","COMMENTED","DISMISSED","PENDING"] | index($s) == null)
            | "review \(.id) has UNRECOGNIZED state \(.state) by \(.author) — treated as unresolved"],
        # An empty commit_id means the finding cannot be tied to a head at all;
        # the conservative reading is "still applies", not "safely old".
        blocking_current: [$outstanding[] | select(.commit_id == "" or at_head($head))],
        blocking_older: [$outstanding[] | select(.commit_id != "" and (at_head($head) | not))]
      }
    | . + {
        current_findings: [.blocking_current[]
            | "review \(.id) CHANGES_REQUESTED by \(.author) at \(.submitted_at) commit \(if .commit_id == "" then "<unknown>" else .commit_id end) [CURRENT HEAD] :: \((.body | split("\n")[0] // "")[0:200])"],
        older_findings: [.blocking_older[]
            | "review \(.id) CHANGES_REQUESTED by \(.author) at \(.submitted_at) commit \(.commit_id) [OLDER HEAD — needs evidence of repair or explicit disposition] :: \((.body | split("\n")[0] // "")[0:200])"],
        current_ids: [.blocking_current[] | .id | tostring],
        older_ids: [.blocking_older[] | .id | tostring]
      }
' 2>/dev/null)
[[ -n "$REVIEW_SUMMARY" ]] || _fail_unknown "the formal-review payload could not be parsed — review state is unknown"

REVIEWS_TOTAL=$(printf '%s' "$REVIEW_SUMMARY" | jq -r '.total')
REVIEWS_UNKNOWN_STATE=$(printf '%s' "$REVIEW_SUMMARY" | jq -r '.unknown_state')
BLOCKING_CURRENT=$(printf '%s' "$REVIEW_SUMMARY" | jq -r '.blocking_current | length')
BLOCKING_OLDER=$(printf '%s' "$REVIEW_SUMMARY" | jq -r '.blocking_older | length')
BLOCKING_IDS=$(printf '%s' "$REVIEW_SUMMARY" | jq -r '(.current_ids + .older_ids) | join(" ")')
printf '%s' "$REVIEW_SUMMARY" | jq -r '(.current_findings + .older_findings + .unknown_findings)[]' >> "$FINDINGS_FILE"

# --- 2. Inline review threads and their ACTUAL resolution state ------------
THREADS_NDJSON=$(forge_get_pr_review_threads "$NWO" "$NUMBER" 2>/dev/null)
THREADS_RC=$?

if [[ $THREADS_RC -eq 0 ]]; then
    INLINE_SOURCE="graphql"
    THREAD_SUMMARY=$(printf '%s\n' "$THREADS_NDJSON" | jq -s '
        map(select(. != null))
        | {
            total: length,
            unresolved: [.[] | select(.is_resolved == false and .is_outdated == false)],
            unresolved_outdated: [.[] | select(.is_resolved == false and .is_outdated == true)]
          }
        | . + {
            findings: ([.unresolved[] | "inline thread \(.id) UNRESOLVED on \(.path) by \(.author) [CURRENT DIFF] :: \((.body | split("\n")[0] // "")[0:200])"]
                     + [.unresolved_outdated[] | "inline thread \(.id) UNRESOLVED+OUTDATED on \(.path) by \(.author) [older head — needs evidence of repair or explicit disposition] :: \((.body | split("\n")[0] // "")[0:200])"]),
            blocking_ids: ([.unresolved[] | .id | tostring] + [.unresolved_outdated[] | .id | tostring])
          }
    ' 2>/dev/null)
    [[ -n "$THREAD_SUMMARY" ]] || _fail_unknown "the review-thread payload could not be parsed — resolution state is unknown"
    INLINE_COMMENTS_TOTAL=$(printf '%s' "$THREAD_SUMMARY" | jq -r '.total')
    INLINE_UNRESOLVED=$(printf '%s' "$THREAD_SUMMARY" | jq -r '.unresolved | length')
    INLINE_UNRESOLVED_OUTDATED=$(printf '%s' "$THREAD_SUMMARY" | jq -r '.unresolved_outdated | length')
    INLINE_BLOCKING_IDS=$(printf '%s' "$THREAD_SUMMARY" | jq -r '.blocking_ids | join(" ")')
    printf '%s' "$THREAD_SUMMARY" | jq -r '.findings[]' >> "$FINDINGS_FILE"
else
    # GraphQL is unavailable (exhausted, unsupported forge, or an error). Fall
    # back to the REST inline-comment list — which carries NO resolution state.
    # A complete REST read with zero inline comments is genuinely clean; any
    # inline comment whose resolution we cannot establish is reported as
    # unknown-resolution, never as resolved.
    if ! INLINE_NDJSON=$(forge_get_pr_review_comments "$NWO" "$NUMBER" 2> /dev/null); then
        _fail_unknown "both the review-thread (GraphQL) and inline-comment (pulls/$NUMBER/comments) reads failed — inline feedback state is unknown"
    fi
    INLINE_SOURCE="rest-unknown"
    INLINE_SUMMARY=$(printf '%s\n' "$INLINE_NDJSON" | jq -s --arg head "$HEAD_SHA" '
        map(select(. != null))
        | {
            total: length,
            current: [.[] | select(.outdated == false)],
            outdated: [.[] | select(.outdated == true)]
          }
        | . + {
            findings: ([.current[] | "inline comment \(.id) on \(.path) by \(.author) [resolution state UNKNOWN — GraphQL reviewThreads unavailable] :: \((.body | split("\n")[0] // "")[0:200])"]
                     + [.outdated[] | "inline comment \(.id) on \(.path) by \(.author) [OUTDATED, resolution state UNKNOWN] :: \((.body | split("\n")[0] // "")[0:200])"]),
            blocking_ids: ([.current[] | .id | tostring] + [.outdated[] | .id | tostring])
          }
    ' 2>/dev/null)
    [[ -n "$INLINE_SUMMARY" ]] || _fail_unknown "the inline-comment payload could not be parsed — inline feedback state is unknown"
    INLINE_COMMENTS_TOTAL=$(printf '%s' "$INLINE_SUMMARY" | jq -r '.total')
    INLINE_UNRESOLVED=$(printf '%s' "$INLINE_SUMMARY" | jq -r '.current | length')
    INLINE_UNRESOLVED_OUTDATED=$(printf '%s' "$INLINE_SUMMARY" | jq -r '.outdated | length')
    INLINE_BLOCKING_IDS=$(printf '%s' "$INLINE_SUMMARY" | jq -r '.blocking_ids | join(" ")')
    printf '%s' "$INLINE_SUMMARY" | jq -r '.findings[]' >> "$FINDINGS_FILE"
fi

# --- 3. Reconcile ----------------------------------------------------------
# Precedence: anything at the current head BLOCKS; anything older, or any
# finding whose resolution could not be established, NEEDS_RECONCILIATION;
# only a complete read with nothing outstanding is CLEAR.
if [[ "$BLOCKING_CURRENT" -gt 0 ]] \
    || { [[ "$INLINE_SOURCE" == "graphql" ]] && [[ "$INLINE_UNRESOLVED" -gt 0 ]]; }; then
    STATE="BLOCKING"
elif [[ "$BLOCKING_OLDER" -gt 0 ]] \
    || [[ "$REVIEWS_UNKNOWN_STATE" -gt 0 ]] \
    || [[ "$INLINE_UNRESOLVED_OUTDATED" -gt 0 ]] \
    || { [[ "$INLINE_SOURCE" == "rest-unknown" ]] && [[ "$INLINE_UNRESOLVED" -gt 0 ]]; }; then
    STATE="NEEDS_RECONCILIATION"
else
    STATE="CLEAR"
fi

_emit_and_exit
