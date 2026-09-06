#!/usr/bin/env bash
# check-promotion-landed.sh — reconcile a Champion promotion whose verdict
# comment posted but whose label write silently did not (issue #6862).
#
# Problem this closes: champion-issue-promo.md's Step 3b posts the "Champion
# Review: APPROVED" verdict comment and writes the promotion labels
# (`+loom:issue`, `+tier:...`, `-loom:evaluating`, `-<proposal label>`) as two
# independent `gh` calls. Nothing checked that the label write actually landed
# before this fix — a transient failure or rate limit could drop the label
# edit silently while the comment still posted. Observed live on issue #6464
# (approved 2026-08-18): the comment posted, `loom:issue` never landed, and
# the issue sat invisible to Builder (which filters on `loom:issue`) for 6
# days until a later pass noticed by reading the label timeline by hand — and
# was then re-evaluated from scratch under a possibly different tier.
#
# Step 3b itself now does the label write FIRST and verifies it with its own
# inline read-back before posting the comment (see champion-issue-promo.md),
# which prevents NEW instances of this failure. This script is the backstop
# for issues already stuck in the old (bad) state, and for any partial
# failure Step 3b's own retry does not resolve: it finds a SINGLE issue that
# carries a "Champion Review: APPROVED" verdict comment but is missing
# `loom:issue`, and (with --apply) completes the promotion by recovering the
# tier the original verdict named, or escalates to an operator when the tier
# cannot be safely recovered.
#
# Finding the CANDIDATE SET (which issues to check) is the caller's job — see
# "Pass 0c" in champion-issue-promo.md, which uses `gh issue list --search`
# with GitHub's `in:comments` qualifier to shortlist issues before calling
# this script once per candidate. Mirrors the split every other classify-
# script in this directory uses (detect-dependency-cycle.sh,
# check-evaluating-staleness.sh): one script decides about ONE issue, the
# caller owns the scan.
#
# Usage:
#   check-promotion-landed.sh --issue <n>            # report only
#   check-promotion-landed.sh --issue <n> --apply     # report + reconcile
#
# With --apply, on a MISMATCH this script:
#   1. Looks for a "**Goal Alignment**: Tier <N> ..." line in the newest
#      APPROVED verdict comment (the line Step 3b's own template writes) and
#      maps it to tier:goal-advancing / tier:goal-supporting / tier:maintenance.
#   2. If a tier was recovered: adds `loom:issue` + that tier label, verifies
#      the addition with a read-back, and posts a reconciliation comment.
#      DECISION=COMPLETED.
#   3. If no tier could be recovered, or the completing edit's own read-back
#      still fails: posts an explanatory comment and adds
#      `loom:operator-only,loom:operator-mechanical` so a human finishes it —
#      never guesses. DECISION=ESCALATED. If the issue ALREADY carries
#      `loom:operator-only` (a prior run already escalated it and nothing
#      changed since), this is skipped entirely — no duplicate comment, no
#      redundant label edit. DECISION=ALREADY_ESCALATED instead (#6942).
#
# Output (stdout — one KEY=VALUE per line, machine-parseable):
#   DECISION=OK|NOT_OPEN|MISMATCH|COMPLETED|ESCALATED|ALREADY_ESCALATED
#   REASON=<short human-readable reason>
#   TIER=<tier:goal-advancing|tier:goal-supporting|tier:maintenance|"">
#
# Exit codes:
#   0  = OK (no APPROVED verdict comment; loom:issue already present;
#        loom:issue is currently absent but a later-lifecycle label
#        (loom:building/loom:blocked) is currently present, proving the
#        promotion landed and the issue has since progressed independent of
#        the timeline read (#7299); or loom:issue is currently absent but the
#        label timeline shows it WAS applied after the newest APPROVED
#        comment and the issue has since legitimately progressed further,
#        e.g. loom:issue -> loom:building or -> loom:blocked — nothing to
#        reconcile in any of these cases, #6933).
#        ALSO used for DECISION=ALREADY_ESCALATED (tier unrecoverable, but the
#        issue already carries loom:operator-only from a prior run — a human
#        already owns it, nothing further to do, #6942).
#   1  = usage or environment error (bad args, `gh`/`jq` missing, a required
#        `gh` read failed)
#   10 = NOT_OPEN (issue is closed — nothing left to reconcile)
#   11 = MISMATCH (APPROVED comment present, loom:issue missing, --apply not
#        passed — report only)
#   12 = COMPLETED (--apply recovered a tier, added loom:issue + that tier,
#        and confirmed the addition via read-back)
#   13 = ESCALATED (--apply could not safely complete the promotion — tier
#        unrecoverable from the verdict text, the completing edit failed, or
#        its own read-back still shows loom:issue missing — routed to
#        loom:operator-only,loom:operator-mechanical instead of guessing)
#
# CALLERS MUST NOT SWALLOW THE EXIT CODE. OK (0), NOT_OPEN (10), and
# ALREADY_ESCALATED (0) all mean "nothing to do here";
# MISMATCH/COMPLETED/ESCALATED (11/12/13) are each distinct outcomes a caller
# should account for separately, same as every other classify-then-caller-acts
# script in this directory.

set -uo pipefail

ISSUE=""
APPLY=0

usage() {
  echo "Usage: $0 --issue <n> [--apply]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue) ISSUE="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$ISSUE" || ! "$ISSUE" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --issue <n> is required and must be numeric" >&2
  usage
  exit 1
fi

for bin in gh jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found on PATH" >&2; exit 1; }
done

emit() {
  local decision="$1" reason="$2" tier="${3:-}"
  echo "DECISION=$decision"
  echo "REASON=$reason"
  echo "TIER=$tier"
}

# Keep `gh`'s stdout (the JSON we parse) and stderr SEPARATE — `gh` writes
# incidental content to stderr even on success (update-notifier banners,
# rate-limit hints), and merging streams can corrupt the payload before jq
# sees it (#5455).
GH_STDERR="$(mktemp)"
trap 'rm -f "$GH_STDERR" 2>/dev/null || true' EXIT

# --- Step 1: current state, labels, and comments in ONE read -----------------
ISSUE_JSON="$(gh issue view "$ISSUE" --json state,labels,comments 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh issue view $ISSUE --json state,labels,comments' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}

STATE_UC="$(jq -r '.state // empty' <<<"$ISSUE_JSON" 2>/dev/null | tr '[:lower:]' '[:upper:]')"
if [[ -n "$STATE_UC" && "$STATE_UC" != "OPEN" ]]; then
  emit "NOT_OPEN" "issue state is $STATE_UC — nothing to reconcile"
  exit 10
fi

HAS_ISSUE_LABEL="$(jq -e '.labels[] | select(.name=="loom:issue")' <<<"$ISSUE_JSON" >/dev/null 2>&1 && echo yes || echo no)"

# Newest comment (by createdAt) whose body contains the APPROVED verdict
# marker text Step 3b's template writes — compact JSON (`-c`, not `-r`) since
# this is an object, not a scalar.
#
# EXCLUDE this script's own follow-up comments (the `<!-- champion:promotion-
# landed-completed -->` / `<!-- champion:promotion-landed-mismatch -->`
# markers written below): both of those templates quote the literal phrase
# "Champion Review: APPROVED" in their own narrative text ("This issue
# carried a `Champion Review: APPROVED` verdict comment, but ..."), so the
# naive `contains()` match here would otherwise pick up a PRIOR RUN's own
# comment as if it were a fresh Champion verdict once one exists. That
# self-match moves APPROVED_AT forward to whenever this script itself last
# ran — sometimes to mere seconds AFTER the very `labeled loom:issue`
# timeline event Step 2 below is trying to confirm predates it — which is
# exactly how issue #7287 got incorrectly re-escalated on a second run
# despite the promotion having genuinely landed (#7299).
APPROVED_COMMENT="$(jq -c '
  [.comments[] | select(
      .body != null
      and (.body | contains("Champion Review: APPROVED"))
      and (.body | contains("<!-- champion:promotion-landed-") | not)
    )]
  | sort_by(.createdAt)
  | last // empty
' <<<"$ISSUE_JSON" 2>/dev/null || true)"

if [[ -z "$APPROVED_COMMENT" || "$APPROVED_COMMENT" == "null" ]]; then
  emit "OK" "no 'Champion Review: APPROVED' verdict comment on this issue — nothing to reconcile"
  exit 0
fi

if [[ "$HAS_ISSUE_LABEL" == "yes" ]]; then
  emit "OK" "APPROVED verdict comment present and loom:issue is present — promotion landed"
  exit 0
fi

# --- Step 1b: loom:issue is currently absent, but a LATER-lifecycle label is
# present (loom:building, loom:blocked) — this is only reachable if loom:issue
# was applied at some point and the issue has since legitimately progressed
# past it (Builder's claim atomically swaps loom:issue -> loom:building, e.g.).
# This is a strictly stronger, cheaper signal than the Step 2 timeline lookup
# below: it needs no extra `gh api` call and does not depend on the timeline
# read (or the APPROVED-comment match above) being reliable at all. Checked
# BEFORE Step 2 so a timeline hiccup or a self-match on an old comment (the
# exact #7287 failure this fix addresses) can never override it (#7299).
if jq -e '.labels[] | select(.name=="loom:building" or .name=="loom:blocked")' <<<"$ISSUE_JSON" >/dev/null 2>&1; then
  emit "OK" "loom:issue is currently absent but a later-lifecycle label (loom:building/loom:blocked) is present — promotion landed and the issue has since progressed"
  exit 0
fi

# --- Step 2: loom:issue is currently absent — but it may have landed and the
# issue has SINCE legitimately progressed further (loom:issue -> loom:building,
# or -> loom:blocked), which looks identical to a lost write from labels alone
# when judged from the current label set only. Before concluding MISMATCH,
# check the label timeline for a `labeled loom:issue` event that happened
# AFTER the newest APPROVED comment: if one exists, the promotion landed and
# this is not #6862's failure mode at all (#6933).
APPROVED_AT="$(jq -r '.createdAt // empty' <<<"$APPROVED_COMMENT")"

TIMELINE_JSON="$(gh api "repos/{owner}/{repo}/issues/$ISSUE/timeline" --paginate 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh api .../issues/$ISSUE/timeline' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}

# Newest `labeled loom:issue` event (by created_at) in the timeline — mirrors
# check-evaluating-staleness.sh's own `sort_by`-free `last` selection pattern.
# RFC3339 UTC timestamps compare correctly as plain strings, so no epoch
# conversion is needed just to find the max.
LATEST_LABELED_AT="$(jq -r '
  [.[] | select(.event=="labeled" and .label.name=="loom:issue") | .created_at]
  | sort
  | last // empty
' <<<"$TIMELINE_JSON" 2>/dev/null || true)"

if [[ -n "$LATEST_LABELED_AT" && -n "$APPROVED_AT" && "$LATEST_LABELED_AT" > "$APPROVED_AT" ]]; then
  emit "OK" "loom:issue was applied after the APPROVED comment and the issue has since progressed — nothing to reconcile"
  exit 0
fi

# --- MISMATCH: an APPROVED verdict exists but loom:issue never landed --------
REASON="APPROVED verdict comment present but loom:issue is missing — the promotion's label write did not land (#6862)"

if [[ "$APPLY" -eq 0 ]]; then
  emit "MISMATCH" "$REASON"
  exit 11
fi

# Recover the tier from the "**Goal Alignment**: [Tier N] ..." line Step 3b's
# template writes into the verdict comment. Heuristic on purpose — this is
# reading prose an earlier Champion pass wrote, not a machine-parseable
# field — and every branch below fails toward ESCALATED (never guesses a
# wrong tier) when the text does not clearly name exactly one.
COMMENT_BODY="$(jq -r '.body // ""' <<<"$APPROVED_COMMENT")"
TIER_LINE="$(printf '%s\n' "$COMMENT_BODY" | grep -i 'Goal Alignment' | head -n1)"
TIER=""
if printf '%s' "$TIER_LINE" | grep -qi 'Tier 1'; then
  TIER="tier:goal-advancing"
elif printf '%s' "$TIER_LINE" | grep -qi 'Tier 2'; then
  TIER="tier:goal-supporting"
elif printf '%s' "$TIER_LINE" | grep -qi 'Tier 3'; then
  TIER="tier:maintenance"
fi

if [[ -z "$TIER" ]]; then
  # Idempotency guard (#6942): if a PRIOR run of this exact branch already
  # routed the issue to loom:operator-only, nothing about the issue's state
  # has changed since (the label edit is the only durable side effect this
  # branch has, and it's already present) — re-posting the escalation
  # comment on every subsequent --apply invocation just spams the issue
  # (#6076 accumulated 18 near-identical comments this way). Mirrors the
  # ALREADY_ROUTED short-circuit in champion-issue-promo.md's Idempotency
  # check and classify-dependency-block.sh's own-marker check.
  if jq -e '.labels[] | select(.name=="loom:operator-only")' <<<"$ISSUE_JSON" >/dev/null 2>&1; then
    emit "ALREADY_ESCALATED" "$REASON; already routed to loom:operator-only by a prior run — nothing further to do"
    exit 0
  fi

  gh issue comment "$ISSUE" --body "<!-- champion:promotion-landed-mismatch -->
**Champion: Promotion write did not land — escalating**

This issue carries a \`Champion Review: APPROVED\` verdict comment, but \`loom:issue\` was never applied — the label write that was supposed to accompany that verdict silently did not land (#6862). This reconciliation pass could not recover which tier (\`tier:goal-advancing\` / \`tier:goal-supporting\` / \`tier:maintenance\`) the original verdict assigned from its own comment text, so it is routing to an operator to complete the promotion manually rather than guessing.

---
*Automated by check-promotion-landed.sh (#6862)*" >/dev/null 2>"$GH_STDERR" || {
    echo "ERROR: failed to post escalation comment on #$ISSUE: $(cat "$GH_STDERR" 2>/dev/null)" >&2
    emit "ESCALATED" "$REASON; tier unrecoverable and the escalation comment FAILED to post"
    exit 1
  }
  gh issue edit "$ISSUE" --add-label "loom:operator-only,loom:operator-mechanical" >/dev/null 2>"$GH_STDERR" || {
    echo "ERROR: failed to add loom:operator-only to #$ISSUE: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  }
  emit "ESCALATED" "$REASON; tier unrecoverable from verdict comment text — routed to loom:operator-only,loom:operator-mechanical"
  exit 13
fi

# Complete the promotion: add loom:issue + the recovered tier.
if ! gh issue edit "$ISSUE" --add-label "loom:issue" --add-label "$TIER" >/dev/null 2>"$GH_STDERR"; then
  echo "ERROR: failed to add loom:issue/$TIER to #$ISSUE: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  emit "ESCALATED" "$REASON; the completing label edit FAILED" "$TIER"
  exit 13
fi

# Verify the write actually landed — the whole point of this script. Never
# trust the edit's own exit code alone (#6862's root cause).
VERIFY_JSON="$(gh issue view "$ISSUE" --json labels 2>"$GH_STDERR")" || {
  echo "ERROR: read-back after completing promotion on #$ISSUE failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  emit "ESCALATED" "$REASON; completed the edit but the read-back verification itself failed" "$TIER"
  exit 13
}

if ! jq -e '.labels[] | select(.name=="loom:issue")' <<<"$VERIFY_JSON" >/dev/null 2>&1; then
  emit "ESCALATED" "$REASON; edit ran but the read-back STILL shows loom:issue missing" "$TIER"
  exit 13
fi

gh issue comment "$ISSUE" --body "<!-- champion:promotion-landed-completed -->
**Champion: Promotion completed — reconciled a missing label write**

This issue carried a \`Champion Review: APPROVED\` verdict comment, but \`loom:issue\` had never been applied — the label write that was supposed to accompany that verdict silently did not land (#6862). This reconciliation pass recovered \`$TIER\` from the original verdict's \"Goal Alignment\" line, applied \`loom:issue\` + \`$TIER\`, and confirmed both are present via a read-back.

**Ready for Builder to claim.**

---
*Automated by check-promotion-landed.sh (#6862)*" >/dev/null 2>"$GH_STDERR" || \
  echo "WARNING: promotion completed but the reconciliation comment failed to post on #$ISSUE: $(cat "$GH_STDERR" 2>/dev/null)" >&2

emit "COMPLETED" "$REASON; completed with $TIER and verified via read-back" "$TIER"
exit 12
