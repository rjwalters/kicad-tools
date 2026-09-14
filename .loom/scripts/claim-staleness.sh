#!/usr/bin/env bash
# claim-staleness.sh - Decide whether a role claim label on an issue or PR is
# stale, and post/bump the stand-down comment when it is not (#6514).
#
# Replaces the three hand-rolled, structurally-identical inline shell blocks in
# judge.md ("Stale `loom:reviewing` Claim Check"), doctor.md ("Stale
# `loom:treating` Claim Check") and curator.md ("Stale `loom:curating` Claim
# Check") with one testable implementation, so the three lanes cannot drift.
#
# WHAT CHANGED VS. THE OLD INLINE LOGIC (#6514)
#
#   The old rule treated ANY non-stand-down comment posted after the claim as
#   proof the claimant was still alive (`COMMENTS_AFTER > 0` => "fresh"). A
#   routine Builder post-push status note - authored by someone who is not the
#   claimant and says nothing about the claim - therefore pinned the claim
#   "fresh" for the rest of its life, because `CLAIMED_AT` never moves. The
#   bounded fallback could not rescue it either: duplicate stand-down
#   suppression stopped posting new stand-down comments, so `STANDDOWN_COUNT`
#   never grew past 1 and never reached `LOOM_MAX_STANDDOWN_STREAK` (PR #6513).
#
#   This script fixes both halves:
#
#   1. Liveness is measured from CLAIMANT activity only. A comment counts as
#      claimant activity iff it carries the claim-activity marker for this exact
#      claim timestamp:
#
#          <!-- loom:claim-activity claim=<CLAIMED_AT> -->
#
#      (print it with the `marker` subcommand). Every other comment - Builder
#      status notes, Champion notices, human chatter, bots - is IGNORED: it
#      neither pins nor extends the claim. And claimant activity only RESETS the
#      idle clock rather than pinning the claim forever, so even a genuine
#      heartbeat only buys another `--stale-minutes`.
#
#   2. Stand-down comments carry a sequence counter in their marker:
#
#          <!-- loom:standdown claim=<CLAIMED_AT> seq=<N> -->
#
#      The `standdown` subcommand posts the first one and thereafter EDITS it in
#      place, bumping `seq`. The forge stays free of duplicate comments (the
#      #5123 goal) while the streak still accumulates toward
#      `--max-standdown-streak` (the #4618 AC3 goal), so the bounded fallback is
#      reachable again. A legacy marker with no `seq=` counts as `seq=1`.
#
#   The 30/60-minute age floors are UNCHANGED, so the double-claim race #4618's
#   machinery exists to prevent is still closed by the same threshold it always
#   was; only the accidental "any stranger's comment pins the claim" behaviour
#   is removed.
#
# Usage:
#   claim-staleness.sh check     --number N --label LABEL [options]
#   claim-staleness.sh standdown --number N --label LABEL [options] [--dry-run]
#   claim-staleness.sh marker    --number N --label LABEL [options]
#
# Subcommands:
#   check       Evaluate the claim and print KEY=VALUE lines (or --json).
#   standdown   Post the stand-down comment, or bump the existing one's seq.
#               Prints the same KEY=VALUE evaluation plus STANDDOWN_ACTION.
#   marker      Print the claim-activity marker line for the current claim.
#               Append it to any progress comment the CLAIMANT posts to reset
#               the idle clock. Prints nothing when there is no live claim.
#
# Options:
#   --number N               Issue or PR number (required).
#   --label LABEL            Claim label: loom:reviewing | loom:treating |
#                            loom:curating (or any loom:* claim label).
#   --stale-minutes M        Idle/age threshold. Default per label:
#                            reviewing $LOOM_STALE_REVIEWING_MINUTES (30),
#                            treating  $LOOM_STALE_TREATING_MINUTES  (60),
#                            curating  $LOOM_STALE_CURATING_MINUTES  (30).
#   --max-standdown-streak K Bounded-fallback cap. Default
#                            $LOOM_MAX_STANDDOWN_STREAK (3).
#   --role NAME              Role name used in the stand-down comment text
#                            (default: derived from --label).
#   --repo OWNER/NAME        Target repo (default: the cwd's git remote).
#   --json                   Emit a JSON object instead of KEY=VALUE lines.
#   --dry-run                `standdown` only: print what it would post/patch.
#
# CLAIM_STATE values:
#   unclaimed              The item does not currently carry LABEL -> claim it.
#   fresh                  A claimant is plausibly alive -> stand down.
#   stale                  Idle >= threshold with no claimant activity -> reclaim.
#   stale-bounded-fallback Stand-down streak >= cap AND claim age >= threshold
#                          -> force-reclaim (livelock breaker).
#   unknown                Timeline unavailable/unparseable -> FAIL SAFE, treat
#                          exactly like `fresh`. Never stomp a claim on missing
#                          data.
#
# Exit codes:
#   0  evaluation completed (branch on CLAIM_STATE)
#   2  usage error
#   3  missing dependency (gh or jq)
#
# All reads are live `gh api` calls on purpose: this is claim arbitration, and a
# 30s-stale cache read is exactly the window a competing claim lands in. Never
# route them through gh-cached.

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

STANDDOWN_PREFIX="<!-- loom:standdown claim="
ACTIVITY_PREFIX="<!-- loom:claim-activity claim="

_usage() {
    # Keep this range in sync with the header comment block above
    # (currently lines 50-96: "Usage:" through the gh-cached caveat).
    sed -n '50,96p' "$0" | sed 's/^# \{0,1\}//'
}

_die() {
    echo "$SCRIPT_NAME: $1" >&2
    exit "${2:-2}"
}

SUBCOMMAND="${1:-}"
case "$SUBCOMMAND" in
    check | standdown | marker) shift ;;
    -h | --help)
        _usage
        exit 0
        ;;
    "") _die "missing subcommand (check | standdown | marker); see --help" ;;
    *) _die "unknown subcommand '$SUBCOMMAND' (check | standdown | marker)" ;;
esac

NUMBER=""
LABEL=""
STALE_MINUTES=""
MAX_STREAK="${LOOM_MAX_STANDDOWN_STREAK:-3}"
ROLE=""
REPO_ARG=""
JSON_OUTPUT=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --number)
            NUMBER="${2:-}"
            shift 2
            ;;
        --label)
            LABEL="${2:-}"
            shift 2
            ;;
        --stale-minutes)
            STALE_MINUTES="${2:-}"
            shift 2
            ;;
        --max-standdown-streak)
            MAX_STREAK="${2:-}"
            shift 2
            ;;
        --role)
            ROLE="${2:-}"
            shift 2
            ;;
        --repo)
            REPO_ARG="${2:-}"
            shift 2
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h | --help)
            _usage
            exit 0
            ;;
        *) _die "unknown option '$1'" ;;
    esac
done

[[ "$NUMBER" =~ ^[0-9]+$ ]] || _die "--number must be a positive integer (got '$NUMBER')"
[[ "$LABEL" =~ ^loom:[a-z][a-z0-9-]*$ ]] || _die "--label must look like loom:<name> (got '$LABEL')"
[[ "$MAX_STREAK" =~ ^[0-9]+$ ]] || _die "--max-standdown-streak must be an integer (got '$MAX_STREAK')"

command -v gh >/dev/null 2>&1 || _die "gh CLI not found on PATH" 3
command -v jq >/dev/null 2>&1 || _die "jq not found on PATH" 3

if [[ -z "$STALE_MINUTES" ]]; then
    case "$LABEL" in
        loom:reviewing) STALE_MINUTES="${LOOM_STALE_REVIEWING_MINUTES:-30}" ;;
        loom:treating) STALE_MINUTES="${LOOM_STALE_TREATING_MINUTES:-60}" ;;
        loom:curating) STALE_MINUTES="${LOOM_STALE_CURATING_MINUTES:-30}" ;;
        *) STALE_MINUTES="${LOOM_STALE_CLAIM_MINUTES:-30}" ;;
    esac
fi
[[ "$STALE_MINUTES" =~ ^[0-9]+$ ]] || _die "--stale-minutes must be an integer (got '$STALE_MINUTES')"

if [[ -z "$ROLE" ]]; then
    case "$LABEL" in
        loom:reviewing) ROLE="Judge" ;;
        loom:treating) ROLE="Doctor" ;;
        loom:curating) ROLE="Curator" ;;
        *) ROLE="Loom" ;;
    esac
fi

API_BASE="repos/{owner}/{repo}"
[[ -n "$REPO_ARG" ]] && API_BASE="repos/$REPO_ARG"

# --- portable ISO-8601 -> epoch seconds (GNU date, then BSD/macOS date) ------
_iso_to_epoch() {
    local out
    out="$(date -u -d "$1" +%s 2>/dev/null)" && [[ "$out" =~ ^[0-9]+$ ]] && {
        printf '%s' "$out"
        return 0
    }
    out="$(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null)" && [[ "$out" =~ ^[0-9]+$ ]] && {
        printf '%s' "$out"
        return 0
    }
    return 1
}

_minutes_since() { # <iso8601> -> whole minutes, clamped at 0
    local epoch now
    epoch="$(_iso_to_epoch "$1")" || return 1
    now="$(date -u +%s)"
    if ((now <= epoch)); then
        printf '0'
    else
        printf '%s' $(((now - epoch) / 60))
    fi
}

# --- forge reads (live, never cached) ---------------------------------------
_has_claim_label() {
    local names
    names="$(gh api "$API_BASE/issues/$NUMBER" --jq '.labels[].name' 2>/dev/null)" || return 2
    grep -qxF -- "$LABEL" <<<"$names"
}

_claimed_at() {
    # --paginate re-invokes --jq once per page and concatenates the per-page
    # results rather than filtering across the combined timeline (#4637), so a
    # >100-event timeline would otherwise yield a multi-line value. `// empty`
    # drops the no-match-on-this-page line entirely; `sort | tail -n 1`
    # collapses the surviving per-page timestamps to the latest one (RFC3339
    # UTC timestamps sort correctly as plain strings).
    gh api "$API_BASE/issues/$NUMBER/timeline?per_page=100" --paginate \
        --jq "[.[] | select(.event==\"labeled\" and .label.name==\"$LABEL\")] | last | .created_at // empty" 2>/dev/null |
        sort | tail -n 1
}

_comments_json() {
    gh api "$API_BASE/issues/$NUMBER/comments?per_page=100" --paginate 2>/dev/null |
        jq -s 'if length == 0 then [] else (map(if type == "array" then . else [.] end) | add) end' 2>/dev/null
}

# --- evaluate ---------------------------------------------------------------
CLAIM_STATE=""
CLAIMED_AT=""
LAST_ACTIVITY_AT=""
CLAIM_AGE_MINUTES=0
IDLE_MINUTES=0
ACTIVITY_COUNT=0
STANDDOWN_COUNT=0
STANDDOWN_COMMENT_ID=""

if _has_claim_label; then
    :
else
    rc=$?
    if ((rc == 2)); then
        # Could not read labels at all - fail safe, never stomp on a read error.
        CLAIM_STATE="unknown"
    else
        CLAIM_STATE="unclaimed"
    fi
fi

if [[ -z "$CLAIM_STATE" ]]; then
    CLAIMED_AT="$(_claimed_at || true)"
    if [[ ! "$CLAIMED_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
        # Missing/garbled timeline data -> fail safe.
        CLAIMED_AT=""
        CLAIM_STATE="unknown"
    fi
fi

if [[ -z "$CLAIM_STATE" ]]; then
    COMMENTS_JSON="$(_comments_json || true)"
    [[ -n "$COMMENTS_JSON" ]] || COMMENTS_JSON="[]"

    AFTER_JSON="$(jq --arg t "$CLAIMED_AT" '[.[] | select(.created_at > $t)]' <<<"$COMMENTS_JSON" 2>/dev/null || echo '[]')"

    # Claimant activity: ONLY comments carrying this claim's activity marker.
    ACTIVITY_COUNT="$(jq --arg m "${ACTIVITY_PREFIX}${CLAIMED_AT} -->" \
        '[.[] | select(.body | contains($m))] | length' <<<"$AFTER_JSON")"
    LAST_ACTIVITY_AT="$(jq -r --arg m "${ACTIVITY_PREFIX}${CLAIMED_AT} -->" --arg c "$CLAIMED_AT" \
        '([.[] | select(.body | contains($m)) | .created_at] + [$c]) | max' <<<"$AFTER_JSON")"

    # Stand-down comments for THIS claim (prefix match covers both the legacy
    # `claim=<ts> -->` form and the `claim=<ts> seq=N -->` form).
    STANDDOWN_JSON="$(jq --arg p "${STANDDOWN_PREFIX}${CLAIMED_AT}" \
        '[.[] | select(.body | contains($p))] | sort_by(.created_at)' <<<"$AFTER_JSON")"
    STANDDOWN_N="$(jq 'length' <<<"$STANDDOWN_JSON")"
    STANDDOWN_MAX_SEQ="$(jq -r --arg c "$CLAIMED_AT" '
        [ .[] | (.body | [ scan("<!-- loom:standdown claim=" + $c + "(?: seq=([0-9]+))?") ][0] // [null] | .[0]) ]
        | map(if . == null then 1 else (. | tonumber) end) | max // 0' <<<"$STANDDOWN_JSON")"
    # The streak is the larger of "how many stand-down comments exist" and "the
    # highest seq any of them records" - bumping in place keeps the comment
    # count at 1 while seq grows, and concurrent passes can post more than one
    # comment while each records a low seq.
    if ((STANDDOWN_MAX_SEQ > STANDDOWN_N)); then
        STANDDOWN_COUNT="$STANDDOWN_MAX_SEQ"
    else
        STANDDOWN_COUNT="$STANDDOWN_N"
    fi
    STANDDOWN_COMMENT_ID="$(jq -r 'last | .id // empty' <<<"$STANDDOWN_JSON")"

    CLAIM_AGE_MINUTES="$(_minutes_since "$CLAIMED_AT" || echo "")"
    IDLE_MINUTES="$(_minutes_since "$LAST_ACTIVITY_AT" || echo "")"
    if [[ -z "$CLAIM_AGE_MINUTES" || -z "$IDLE_MINUTES" ]]; then
        CLAIM_STATE="unknown"
        CLAIM_AGE_MINUTES=0
        IDLE_MINUTES=0
    elif ((STANDDOWN_COUNT >= MAX_STREAK)) && ((CLAIM_AGE_MINUTES >= STALE_MINUTES)); then
        # Bounded fallback: the livelock breaker. Deliberately keyed on the
        # CLAIM's own age, not the idle clock, so a claimant stuck in a loop
        # emitting activity markers cannot hold the claim forever (#4790 kept
        # the age floor; #6514 kept the streak reachable).
        CLAIM_STATE="stale-bounded-fallback"
    elif ((IDLE_MINUTES >= STALE_MINUTES)); then
        CLAIM_STATE="stale"
    else
        CLAIM_STATE="fresh"
    fi
fi

_emit_evaluation() {
    local extra_key="${1:-}" extra_val="${2:-}"
    if [[ "$JSON_OUTPUT" == true ]]; then
        jq -n \
            --arg state "$CLAIM_STATE" \
            --arg claimed_at "$CLAIMED_AT" \
            --arg last_activity_at "$LAST_ACTIVITY_AT" \
            --argjson claim_age "${CLAIM_AGE_MINUTES:-0}" \
            --argjson idle "${IDLE_MINUTES:-0}" \
            --argjson activity "${ACTIVITY_COUNT:-0}" \
            --argjson standdown "${STANDDOWN_COUNT:-0}" \
            --argjson stale_minutes "$STALE_MINUTES" \
            --argjson max_streak "$MAX_STREAK" \
            --arg label "$LABEL" \
            --arg extra_key "$extra_key" \
            --arg extra_val "$extra_val" \
            '{
               claim_state: $state,
               label: $label,
               claimed_at: $claimed_at,
               last_activity_at: $last_activity_at,
               claim_age_minutes: $claim_age,
               idle_minutes: $idle,
               activity_count: $activity,
               standdown_count: $standdown,
               stale_minutes: $stale_minutes,
               max_standdown_streak: $max_streak
             }
             + (if $extra_key == "" then {} else {($extra_key | ascii_downcase): $extra_val} end)'
    else
        echo "CLAIM_STATE=$CLAIM_STATE"
        echo "LABEL=$LABEL"
        echo "CLAIMED_AT=$CLAIMED_AT"
        echo "LAST_ACTIVITY_AT=$LAST_ACTIVITY_AT"
        echo "CLAIM_AGE_MINUTES=${CLAIM_AGE_MINUTES:-0}"
        echo "IDLE_MINUTES=${IDLE_MINUTES:-0}"
        echo "ACTIVITY_COUNT=${ACTIVITY_COUNT:-0}"
        echo "STANDDOWN_COUNT=${STANDDOWN_COUNT:-0}"
        echo "STALE_MINUTES=$STALE_MINUTES"
        echo "MAX_STANDDOWN_STREAK=$MAX_STREAK"
        [[ -n "$extra_key" ]] && echo "$extra_key=$extra_val"
    fi
    return 0
}

case "$SUBCOMMAND" in
    check)
        _emit_evaluation
        exit 0
        ;;
    marker)
        [[ "$CLAIM_STATE" == "unclaimed" || -z "$CLAIMED_AT" ]] && exit 0
        echo "${ACTIVITY_PREFIX}${CLAIMED_AT} -->"
        exit 0
        ;;
esac

# --- standdown --------------------------------------------------------------
if [[ "$CLAIM_STATE" != "fresh" && "$CLAIM_STATE" != "unknown" ]]; then
    # Only the "stand down, do not stomp" verdicts post a stand-down comment.
    _emit_evaluation "STANDDOWN_ACTION" "skipped-not-fresh"
    exit 0
fi

if [[ -z "$CLAIMED_AT" ]]; then
    _emit_evaluation "STANDDOWN_ACTION" "skipped-unknown-claim"
    exit 0
fi

NEXT_SEQ=$((STANDDOWN_COUNT + 1))
BODY="$ROLE pass: still carries a fresh \`$LABEL\` claim (claimed $CLAIMED_AT, idle ${IDLE_MINUTES}m) — standing down without reclaiming. Not stomping.

Stand-down passes against this claim: $NEXT_SEQ of $MAX_STREAK before the bounded fallback force-reclaims it. This comment is edited in place on each pass rather than reposted (#5123, #6514).
${STANDDOWN_PREFIX}${CLAIMED_AT} seq=${NEXT_SEQ} -->"

if [[ "$DRY_RUN" == true ]]; then
    if [[ -n "$STANDDOWN_COMMENT_ID" ]]; then
        _emit_evaluation "STANDDOWN_ACTION" "would-bump:$STANDDOWN_COMMENT_ID:$NEXT_SEQ"
    else
        _emit_evaluation "STANDDOWN_ACTION" "would-post:$NEXT_SEQ"
    fi
    exit 0
fi

# `--input -` with a jq-built payload, never `-f body=@...`: `@` prefixes are
# read as file references by gh (see comment-body-literal-path.md).
if [[ -n "$STANDDOWN_COMMENT_ID" ]]; then
    if jq -n --arg b "$BODY" '{body: $b}' |
        gh api --method PATCH "$API_BASE/issues/comments/$STANDDOWN_COMMENT_ID" --input - >/dev/null 2>&1; then
        _emit_evaluation "STANDDOWN_ACTION" "bumped:$STANDDOWN_COMMENT_ID:$NEXT_SEQ"
    else
        _emit_evaluation "STANDDOWN_ACTION" "failed-bump:$STANDDOWN_COMMENT_ID"
    fi
else
    if jq -n --arg b "$BODY" '{body: $b}' |
        gh api --method POST "$API_BASE/issues/$NUMBER/comments" --input - >/dev/null 2>&1; then
        _emit_evaluation "STANDDOWN_ACTION" "posted:$NEXT_SEQ"
    else
        _emit_evaluation "STANDDOWN_ACTION" "failed-post"
    fi
fi
exit 0
