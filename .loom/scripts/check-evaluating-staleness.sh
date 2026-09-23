#!/usr/bin/env bash
# check-evaluating-staleness.sh — single source of truth for "is this issue's
# claim label stale?" (issue #6828).
#
# Problem this closes: champion-issue-promo.md's "Claim (staleness-aware...)"
# section already reclaims a stale `loom:evaluating` claim — a prior Champion
# pass that died mid-evaluation, leaving the label stuck. But that code only
# runs AFTER an issue has already been selected by champion.md's Priority 2/3
# discovery queries, and those queries unconditionally EXCLUDE every issue
# carrying `loom:evaluating` (correct for a genuinely fresh, in-flight claim,
# #4954). The two mechanisms never compose: a stale claim is never selected,
# so its own staleness reconciliation never runs on it — confirmed live on
# a real downstream repo, stuck with a 9-day-old `loom:evaluating` claim.
#
# This script is the shared staleness classifier both call sites use, so
# there is exactly one implementation of "how old is this labeled event" to
# keep correct:
#   - champion-issue-promo.md's "Claim (staleness-aware...)" section (decides
#     whether to skip a fresh concurrent claim or steal a stale one)
#   - the self-healing rescan pass ("Pass 0b") that lists `loom:evaluating`
#     issues directly (bypassing champion.md's exclusion) and un-claims the
#     stale ones so they re-enter normal discovery
#
# This script only CLASSIFIES — it never writes a label or a comment. Both
# call sites own their own label/comment writes (posting an audit-trail
# comment before a reclaim, or escalating after repeated staleness), exactly
# like every other classify-then-caller-acts script in this directory
# (detect-dependency-cycle.sh, detect-startable-subset.sh).
#
# Usage:
#   check-evaluating-staleness.sh --issue <N> [--label <label>] [--threshold-minutes <n>]
#
# --label defaults to "loom:evaluating"; --threshold-minutes defaults to
# LOOM_STALE_EVALUATING_MINUTES (or 15 if unset) — the same env var name and
# default champion-issue-promo.md's Claim section already documents.
#
# Output (stdout — one KEY=VALUE per line, machine-parseable):
#   DECISION=NOT_PRESENT|FRESH|STALE
#   LABEL=<label>
#   THRESHOLD_MIN=<n>
#   AGE_MIN=<n>          (0 when NOT_PRESENT, or when the labeled event's
#                          timestamp could not be read — fail-safe as FRESH,
#                          matching the pre-existing inline check's behavior)
#   CLAIMED_AT=<timestamp, or empty when NOT_PRESENT / unreadable>
#
# Exit codes:
#   0  = FRESH (label present, age < threshold — a concurrent claim is
#        genuinely in flight; do not touch it)
#   1  = usage or environment error (bad args, `gh`/`jq` missing, `gh` call
#        failed)
#   10 = NOT_PRESENT (the issue does not currently carry the label — nothing
#        to reconcile; may mean a concurrent pass already released it)
#   12 = STALE (label present, age >= threshold — the caller should reclaim it)
#
# CALLERS MUST NOT SWALLOW THE EXIT CODE. NOT_PRESENT (10) and FRESH (0) both
# mean "do nothing here"; only STALE (12) is actionable.

set -uo pipefail

ISSUE=""
LABEL="loom:evaluating"
THRESHOLD_MIN="${LOOM_STALE_EVALUATING_MINUTES:-15}"

usage() {
  echo "Usage: $0 --issue <N> [--label <label>] [--threshold-minutes <n>]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) ISSUE="${2:-}"; shift 2 ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    --threshold-minutes) THRESHOLD_MIN="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$ISSUE" || ! "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --issue <N> is required and must be numeric" >&2
  usage
  exit 1
fi
if [[ -z "$LABEL" ]]; then
  echo "ERROR: --label must not be empty" >&2
  usage
  exit 1
fi
if [[ ! "$THRESHOLD_MIN" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --threshold-minutes must be a non-negative integer, got: '$THRESHOLD_MIN'" >&2
  usage
  exit 1
fi

for bin in gh jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found on PATH" >&2; exit 1; }
done

emit() {
  local decision="$1" age="$2" claimed_at="$3"
  echo "DECISION=$decision"
  echo "LABEL=$LABEL"
  echo "THRESHOLD_MIN=$THRESHOLD_MIN"
  echo "AGE_MIN=$age"
  echo "CLAIMED_AT=$claimed_at"
}

# Keep `gh`'s stdout (the JSON/text we parse) and stderr SEPARATE — `gh`
# writes incidental content to stderr even on success (update-notifier
# banners, rate-limit hints), and merging streams can corrupt the payload
# before jq sees it (#5455).
GH_STDERR="$(mktemp)"
trap 'rm -f "$GH_STDERR" 2>/dev/null || true' EXIT

# --- Step 1: is $LABEL currently present on $ISSUE? --------------------------
# Plain `gh` — claim arbitration, never a cached reader (mirrors champion-
# issue-promo.md's own rule for its Claim section: a stale cache would
# reintroduce the double-claim race #4954 closed). Raw JSON is fetched and
# filtered with a local `jq`, not `gh --json ... --jq ...`, so the same
# invocation shape works whether $LABEL is a literal or (as here) a variable.
LABELS_JSON="$(gh issue view "$ISSUE" --json labels 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh issue view $ISSUE --json labels' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}
CURRENT_LABELS="$(jq -r '[.labels[].name] | join(",")' <<<"$LABELS_JSON" 2>/dev/null || true)"

if ! echo ",$CURRENT_LABELS," | grep -q ",$LABEL,"; then
  emit "NOT_PRESENT" "0" ""
  exit 10
fi

# --- Step 2: age of the most recent `labeled` timeline event for $LABEL -----
TIMELINE_JSON="$(gh api "repos/{owner}/{repo}/issues/$ISSUE/timeline" --paginate 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh api .../issues/$ISSUE/timeline' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}
CLAIMED_AT="$(jq -r --arg claim_label "$LABEL" \
  '[.[] | select(.event=="labeled" and .label.name==$claim_label)] | last | .created_at // empty' \
  <<<"$TIMELINE_JSON" 2>/dev/null || true)"

# Portable ISO-8601 -> epoch-seconds: GNU `date -d` first, BSD/macOS `date -j
# -f` fallback (matches the existing dual-path idiom in judge-fallback-guard.sh,
# sweep-run-registry.sh, sweep-lease-fence.sh, urgent-flip-guard.sh).
iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" +%s 2>/dev/null || echo ""
}

if [[ -n "$CLAIMED_AT" ]]; then
  CLAIMED_EPOCH="$(iso_to_epoch "$CLAIMED_AT")"
  if [[ -n "$CLAIMED_EPOCH" ]]; then
    AGE_MIN=$(( ($(date -u +%s) - CLAIMED_EPOCH) / 60 ))
  else
    AGE_MIN=0   # unparseable timestamp — fail safe, treat as fresh
  fi
else
  AGE_MIN=0   # unknown — fail safe, treat as fresh (matches the pre-existing inline check)
fi

# --- Step 3: fresh or stale? --------------------------------------------------
if [[ "$AGE_MIN" -lt "$THRESHOLD_MIN" ]]; then
  emit "FRESH" "$AGE_MIN" "$CLAIMED_AT"
  exit 0
fi

emit "STALE" "$AGE_MIN" "$CLAIMED_AT"
exit 12
