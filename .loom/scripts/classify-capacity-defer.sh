#!/usr/bin/env bash
# classify-capacity-defer.sh - Idempotency for Champion's tier-capacity
# deferral comment (issue #6729).
#
# THE FAILURE MODE THIS EXISTS FOR
#
# champion-issue-promo.md's tier-based rate limiting ("Rate Limiting by Tier")
# can decide that a proposal passes all 8 promotion criteria but is held back
# THIS PASS anyway -- Tier 3 promotes only one issue per iteration and only if
# fewer than 5 Tier 3 issues are already in the backlog; Tier 2 promotes at
# most 2 per iteration. Every later Champion pass (cron tick, role-runner
# tick, or a fresh dispatch) re-derives the same "criteria pass, capacity
# gate blocks it" conclusion for an issue that has not been revised at all --
# and, without a guard, posts an equivalent "Tier N backlog cap reached"
# comment EVERY time. Observed live: #6628 accumulated 10 near-identical
# capacity-deferral comments over ~22 hours; #6647 and #6649 each accumulated
# 2-3 more, all citing the SAME five occupant tier:maintenance issues.
#
# This is the same class of noise the dependency-timing gate
# (classify-dependency-block.sh --check-defer, #5664) already solved for
# open-dependency deferrals: record the deferral in place (or say nothing new)
# rather than posting a fresh comment every cycle. That mechanism cannot be
# reused directly -- it works by PATCHing the existing REJECTION verdict
# comment, and a capacity deferral is explicitly NOT a rejection (no revision
# is needed, so champion-issue-promo.md's Step 4 never writes a verdict
# marker for it). This script gives the capacity-deferral path its OWN
# idempotency marker instead, keyed to the thing that actually decides whether
# a fresh comment is warranted: the TIER and the OCCUPANT SET of the backlog
# that is currently blocking promotion.
#
# THE RULE
#
#   Same tier, same occupant set as the last capacity-deferral comment on this
#   issue  -> nothing has changed since a human could already see why this is
#             stuck; SKIP (no new comment; --apply bumps a "seen N times"
#             counter on the existing comment for audit purposes only).
#   First deferral, OR a different occupant set (an issue left/joined the
#   backlog, the tier changed, the cap was reached by a different set)
#             -> POST a fresh comment; an operator can still see WHEN and WHY
#                this is stuck, and the composition actually changed.
#
# Unlike the dependency-timing gate this has no escalation counterpart and
# none is added here: a capacity deferral is not a merits problem, it never
# needs a human decision, and the condition ends on its own the moment the
# backlog composition changes (the exact next `--check-capacity-defer` call
# reports POST_COMMENT again). Bounding a state that self-resolves and never
# needs a human would only reintroduce noise for no benefit.
#
# Usage:
#   classify-capacity-defer.sh --issue <N> --tier <label> --occupants <list>
#       [--repo <owner/repo>] [--apply] [--comments-file <path>] [--no-cache]
#
# Options:
#   --issue <N>          Issue number being evaluated (required).
#   --tier <label>        The tier label whose cap is blocking promotion this
#                         pass, e.g. `tier:maintenance` (required). Scopes the
#                         marker so a re-tiered issue never matches a stale
#                         fingerprint recorded under its old tier.
#   --occupants <list>    The current backlog occupant set for that tier's cap
#                         -- the same issue numbers champion-issue-promo.md's
#                         "Backlog Balance Check" / tier-limit logic already
#                         computed (comma- and/or whitespace-separated, `#`
#                         prefix optional, e.g. "#6612,#6076,#6068,#5512,#4136"
#                         or "6612 6076 6068 5512 4136"). Required (may be
#                         empty for a cap that isn't occupant-enumerable).
#   --repo <nwo>          owner/repo of --issue (default: current repo's
#                         origin).
#   --apply               Only meaningful when SKIP_COMMENT applies: PATCH the
#                         existing capacity-deferral comment to bump its
#                         "seen N times" counter. Without it the script is
#                         strictly read-only. Never posts a NEW comment.
#   --comments-file <p>   Read comments from this file (a JSON array of
#                         REST-shaped comments: id, body, user.login,
#                         user.type, author_association) instead of fetching.
#                         Used by tests and by callers that already hold the
#                         REST comment list.
#   --no-cache             Accepted for compatibility; a no-op. The read is
#                         always a fresh REST listing (see BOUNDED COST).
#   --help,-h              Show this help.
#
# Output (stdout, marker lines -- parseable by the Champion prose):
#   POST_COMMENT
#   REASON: first-deferral | composition-changed
#   TIER: <tier>
#   OCCUPANTS: <normalized, sorted, space-separated>
#   FINGERPRINT: <16 hex>
#   PRIOR_OCCUPANTS: <normalized prior set>            (composition-changed only)
#
#   SKIP_COMMENT
#   TIER: <tier>
#   OCCUPANTS: <normalized, sorted, space-separated>
#   FINGERPRINT: <16 hex>
#   PATCHED: <owner/repo>#<issue>                      (--apply only, on success)
#
# Exit codes:
#   0 - SKIP_COMMENT applies (composition unchanged since the last
#       capacity-deferral comment on this issue for this tier)
#   1 - POST_COMMENT applies (first deferral, or the composition changed) --
#       the normal/default action, unaffected by this script existing
#   2 - error (bad arguments, issue unreadable, missing jq)
#
# BOUNDED COST. One uncached REST paginate of the issue's comments (via
# `loom-daemon forge trusted-comments --fetch`), which serves both the
# decision and --apply (REST carries the numeric comment id the PATCH needs).
# --apply additionally costs one PATCH.

set -uo pipefail

# ---- output helpers ----
if [[ -t 2 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
else
    RED=''; YELLOW=''; NC=''
fi
err()  { echo -e "${RED}ERROR: $1${NC}" >&2; }
warn() { echo -e "${YELLOW}WARNING: $1${NC}" >&2; }

show_help() {
    sed -n '2,105p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---- defaults ----
ISSUE=""
REPO_NWO=""
TIER=""
OCCUPANTS_RAW=""
DO_APPLY=0
COMMENTS_FILE=""

MARKER_TAG="champion:capacity-defer"
SEEN_TAG="champion:capacity-defer-seen"

# =====================================================================
# Pure helpers (sourceable and unit-tested directly by
# tests/test-classify-capacity-defer.sh -- no stubs, no forge)
# =====================================================================

# _sha256 - portable sha256 (sha256sum on Linux, shasum on macOS, cksum as a
# last resort) -- same fallback chain the rest of the repo's scripts use.
_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum
    elif command -v shasum >/dev/null 2>&1; then shasum -a 256
    else cksum; fi
}

# normalize_occupants <raw list>
# Splits on commas/whitespace, strips a leading '#', drops empties, and
# emits the sorted-unique set space-separated. Order- and duplicate-
# insensitive by construction, so the SAME occupant set always normalizes
# identically no matter how the caller formatted it.
normalize_occupants() {
    local raw="$1"
    printf '%s' "$raw" \
        | tr ',' ' ' \
        | tr -s '[:space:]' '\n' \
        | sed -E 's/^#//' \
        | awk 'NF' \
        | sort -u \
        | tr '\n' ' ' \
        | sed -E 's/ $//'
}

# capacity_fingerprint <tier> <normalized occupants>
# Identity of a (tier, occupant-set) PAIR -- the two facts that jointly
# explain why THIS deferral fired. Sorted-unique occupants (from
# normalize_occupants) means the fingerprint is order-independent.
capacity_fingerprint() {
    local tier="$1" occupants="$2"
    printf '%s\n%s' "$tier" "$occupants" | _sha256 | awk '{print substr($1, 1, 16)}'
}

# capacity_marker <tier> <fingerprint>
capacity_marker() {
    printf '<!-- %s:%s:%s -->' "$MARKER_TAG" "$1" "$2"
}

# capacity_marker_prefix <tier>
# The prefix shared by every capacity-deferral marker for this TIER,
# regardless of fingerprint -- used to find the most recent capacity-deferral
# comment on the issue, whatever occupant set it recorded.
capacity_marker_prefix() {
    printf '<!-- %s:%s:' "$MARKER_TAG" "$1"
}

# extract_fingerprint_from_marker <comment-body> <tier>
# Pulls the fingerprint out of the LAST capacity-deferral marker line in a
# comment body for the given tier, or emits nothing if absent.
extract_fingerprint_from_marker() {
    local body="$1" tier="$2" prefix escaped_prefix
    prefix="$(capacity_marker_prefix "$tier")"
    escaped_prefix="$(printf '%s' "$prefix" | sed 's/[][\.*^$/]/\\&/g')"
    printf '%s\n' "$body" \
        | { grep -oE "${escaped_prefix}[0-9a-f]{16} -->" || true; } \
        | tail -n 1 \
        | sed -E 's/.*:([0-9a-f]{16}) -->$/\1/'
}

# =====================================================================
# repo resolution (same shape as detect-startable-subset.sh's _resolve_repo)
# =====================================================================
_resolve_repo() {
    local url
    url="$(git remote get-url origin 2>/dev/null || true)"
    if [[ -n "$url" ]]; then
        url="${url%.git}"
        url="$(printf '%s' "$url" | sed -E 's#^.*[:/]([^/]+/[^/]+)$#\1#')"
        if [[ "$url" == */* ]]; then
            printf '%s\n' "$url"
            return 0
        fi
    fi
    gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null
}

# =====================================================================
# Forge reads / writes
# =====================================================================

# _get_comments -- emits a JSON array of TRUSTED-authored comment objects
# (#9548), REST-shaped. A capacity-deferral marker only suppresses a
# DUPLICATE deferral comment -- it does not defer or promote anything -- but
# it is still a control phrase: a forged one would silence Champion's comment.
# So it counts only from a trusted author, and an untrusted marker reads as
# ABSENT. Reading a genuine marker as absent is NOT free: it re-posts the
# deferral comment on every Champion pass, the #6729 spam this script exists
# to stop. That is why the live read is the REST listing (`forge
# trusted-comments --fetch`): only REST spells a fleet App author as
# `<slug>[bot]`; `gh issue view --json comments` reduces it to a bare login
# the filter must treat as a user, dropping every fleet-authored marker
# (#9657 review: 1 of 7 kept on #9126). --comments-file is filtered the same.
# requires-daemon: forge optional   Without the `trusted-comments` verb (absent binary, or one predating #9548) no author can be verified, so every capacity-defer marker reads as ABSENT (stderr says so) -- a duplicate deferral comment per pass until the binary is current, never an attacker-silenced one.
_get_comments() {
    local filtered
    if [[ -n "$COMMENTS_FILE" ]]; then
        filtered="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge trusted-comments <"$COMMENTS_FILE" 2>/dev/null)"
    else
        filtered="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge trusted-comments --fetch "$ISSUE" --repo "$REPO_NWO" 2>/dev/null)"
    fi || {
        echo "capacity-defer: could not read or authenticate $REPO_NWO#$ISSUE's comments ('loom-daemon forge trusted-comments' failed); treating markers as absent (#9548)" >&2
        return 0
    }
    printf '%s\n' "$filtered"
}

# _patch_streak <fingerprint> <trusted-comments-json> -- finds, among the
# TRUSTED comments _get_comments already read (never an outsider's copy of the
# marker), the latest one carrying this exact fingerprint's marker and PATCHes
# its "seen N times" counter upward. Never posts a new comment. Best-effort: a
# failure here does not change the SKIP decision already made.
_patch_streak() {
    local fingerprint="$1" comments="$2" marker comment_json comment_id comment_body seen next_seen new_body
    marker="$(capacity_marker "$TIER" "$fingerprint")"
    comment_json="$(printf '%s\n' "$comments" | jq -c --arg m "$marker" '[.[] | select(.body | contains($m))] | last // empty')"

    comment_id="$(printf '%s' "$comment_json" | jq -r '.id // empty' 2>/dev/null)"
    comment_body="$(printf '%s' "$comment_json" | jq -r '.body // empty' 2>/dev/null)"
    if [[ -z "$comment_id" ]]; then
        warn "no matching capacity-deferral comment found to patch on $REPO_NWO#$ISSUE (fingerprint $fingerprint) -- skipping the streak bump, not fatal"
        return 1
    fi

    seen="$(printf '%s' "$comment_body" \
        | sed -n "s|.*<!-- $SEEN_TAG:$fingerprint:\([0-9]\{1,\}\) -->.*|\1|p" | tail -n 1)"
    seen="${seen:-1}"
    next_seen=$(( seen + 1 ))

    if printf '%s' "$comment_body" | grep -q "<!-- $SEEN_TAG:$fingerprint:"; then
        new_body="$(printf '%s' "$comment_body" \
            | sed "s|<!-- $SEEN_TAG:$fingerprint:[0-9]\{1,\} -->|<!-- $SEEN_TAG:$fingerprint:$next_seen -->|")"
    else
        new_body="$(printf '%s\n%s' "$comment_body" "<!-- $SEEN_TAG:$fingerprint:$next_seen -->")"
    fi

    if [[ -n "$COMMENTS_FILE" ]]; then
        # Test/dry-run mode: nothing to PATCH against a real forge. Report
        # success so callers exercising --comments-file can assert the
        # decision path without a live gh.
        echo "PATCHED: $REPO_NWO#$ISSUE"
        return 0
    fi

    # #9548: PATCH only a repo this installation manages and can write, by
    # name (never `{owner}/{repo}`, which gh expands from `upstream` first);
    # forge-helpers.sh in a subshell because it sets -e.
    local write_repo
    write_repo="$(source "$(dirname "${BASH_SOURCE[0]}")/lib/forge-helpers.sh" && loom_write_repo "${REPO_NWO:-}")" || { warn "not PATCHing the capacity-deferral comment on $REPO_NWO#$ISSUE: loom-daemon forge may-write refused the repo (#9548)"; return 1; }
    if gh api --method PATCH "repos/$write_repo/issues/comments/$comment_id" -f body="$new_body" >/dev/null 2>&1; then
        echo "PATCHED: $REPO_NWO#$ISSUE"
        return 0
    fi
    warn "could not PATCH the capacity-deferral comment on $REPO_NWO#$ISSUE (a later pass will retry)"
    return 1
}

# =====================================================================
# main
# =====================================================================
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --issue)          ISSUE="${2:-}"; shift 2 ;;
            --repo)           REPO_NWO="${2:-}"; shift 2 ;;
            --tier)           TIER="${2:-}"; shift 2 ;;
            --occupants)      OCCUPANTS_RAW="${2:-}"; shift 2 ;;
            --apply)          DO_APPLY=1; shift ;;
            --comments-file)  COMMENTS_FILE="${2:-}"; shift 2 ;;
            --no-cache)       shift ;;  # no-op, kept for existing callers
            --help|-h)        show_help; exit 0 ;;
            *)                err "Unexpected argument: $1"; show_help >&2; exit 2 ;;
        esac
    done

    if [[ -z "$ISSUE" ]]; then
        err "Usage: classify-capacity-defer.sh --issue <N> --tier <label> --occupants <list> [--repo <owner/repo>] [--apply]"
        exit 2
    fi
    if ! [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
        err "--issue must be a number (got: $ISSUE)"
        exit 2
    fi
    if [[ -z "$TIER" ]]; then
        err "--tier is required"
        exit 2
    fi
    if ! command -v jq >/dev/null 2>&1; then
        err "jq is required"
        exit 2
    fi
    if [[ -n "$COMMENTS_FILE" && ! -f "$COMMENTS_FILE" ]]; then
        err "--comments-file not found: $COMMENTS_FILE"
        exit 2
    fi

    if [[ -z "$REPO_NWO" ]]; then
        REPO_NWO="$(_resolve_repo)"
    fi
    if [[ -z "$REPO_NWO" ]]; then
        err "could not determine the repo - pass --repo <owner/repo>"
        exit 2
    fi

    local occupants fingerprint comments_json prefix latest_body prior_fp
    occupants="$(normalize_occupants "$OCCUPANTS_RAW")"
    fingerprint="$(capacity_fingerprint "$TIER" "$occupants")"

    comments_json="$(_get_comments)"
    if [[ -z "$comments_json" || "$comments_json" == "null" ]]; then
        if [[ -z "$COMMENTS_FILE" ]]; then
            err "could not read $REPO_NWO#$ISSUE"
            exit 2
        fi
        comments_json="[]"
    fi

    prefix="$(capacity_marker_prefix "$TIER")"
    latest_body="$(printf '%s\n' "$comments_json" \
        | jq -r --arg p "$prefix" '[.[] | select(.body | contains($p))] | last | .body // ""' 2>/dev/null)"

    if [[ -z "${latest_body//[[:space:]]/}" ]]; then
        echo "POST_COMMENT"
        echo "REASON: first-deferral"
        echo "TIER: $TIER"
        echo "OCCUPANTS: $occupants"
        echo "FINGERPRINT: $fingerprint"
        exit 1
    fi

    prior_fp="$(extract_fingerprint_from_marker "$latest_body" "$TIER")"

    if [[ "$prior_fp" == "$fingerprint" ]]; then
        echo "SKIP_COMMENT"
        echo "TIER: $TIER"
        echo "OCCUPANTS: $occupants"
        echo "FINGERPRINT: $fingerprint"
        if [[ "$DO_APPLY" -eq 1 ]]; then
            _patch_streak "$fingerprint" "$comments_json" || true
        fi
        exit 0
    fi

    echo "POST_COMMENT"
    echo "REASON: composition-changed"
    echo "TIER: $TIER"
    echo "OCCUPANTS: $occupants"
    echo "FINGERPRINT: $fingerprint"
    [[ -n "$prior_fp" ]] && echo "PRIOR_FINGERPRINT: $prior_fp"
    exit 1
}

# Only run main when executed directly (not when sourced by tests).
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
