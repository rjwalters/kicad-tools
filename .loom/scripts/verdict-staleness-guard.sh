#!/usr/bin/env bash
# verdict-staleness-guard.sh — bind a PR's review verdict to the tree it was
# rendered against, and invalidate it when the head SHA moves (issue #5686).
#
# Problem this closes: a review verdict (`loom:changes-requested`, or the more
# dangerous approving `loom:pr`) is a statement about a SPECIFIC TREE. Before
# this guard, the label outlived the tree — a force-push/rebase could replace
# every commit the verdict was written about and the label would sit there
# unchanged. Observed live on rjwalters/repo#192 (2026-08-08): Judge correctly
# requested changes for a genuinely-failing test at 02:22, the branch was
# rebased and force-pushed at 02:55 making CI green, and the PR then sat with
# `loom:changes-requested` and no Judge re-queue until an operator cleared the
# label by hand. The inverse direction is worse: a `loom:pr` approval that
# survives a force-push lets Champion auto-merge a tree nobody approved.
#
# The fix is a marker, not a new label. Every terminal verdict comment carries
#
#     <!-- loom:verdict-sha sha=<head-sha> verdict=approved|changes-requested -->
#
# (the same HTML-comment marker convention as `<!-- loom:standdown claim=... -->`
# and `<!-- loom:fallback-evaluated sha=... -->`), so the verdict records which
# tree it covers. This script compares that recorded SHA against the PR's
# CURRENT head SHA and reports FRESH / STALE, optionally performing the
# clear+re-queue transition itself.
#
# Decision, in priority order (first match wins):
#   1. NOT_OPEN     — the PR is no longer open (merged, or closed unmerged).
#                     A verdict is a statement about a tree someone might still
#                     act on; once the PR is finished there is nothing left to
#                     invalidate or re-queue. The guard reports and does
#                     NOTHING — no labels, no comment — whatever the marker
#                     says (#6781).
#   2. NO_VERDICT   — the PR carries neither `loom:pr` nor
#                     `loom:changes-requested`. Nothing to invalidate.
#   3. UNVERIFIABLE — a verdict label is present, but no marker comment exists
#                     for THAT verdict kind. Fail safe: the verdict is kept.
#                     This is the pre-migration/rollout case (verdicts written
#                     before this guard shipped carry no marker), the
#                     mixed-fleet case (a host still running the older prompt),
#                     and — most commonly in practice — the case where the
#                     model simply dropped the marker (#6319: observed on
#                     roughly one verdict in four). Never force-clear on
#                     missing evidence; with --anchor, remediate instead (#3b).
#  3b. ANCHORED      — --anchor was passed and the UNVERIFIABLE verdict was
#                     given a marker recording the CURRENT head, so it becomes
#                     invalidatable from here on (#6319).
#   4. FRESH        — the newest matching marker's SHA equals the current head
#                     SHA. The verdict still describes the tree in front of it.
#   5. STALE        — the newest matching marker's SHA differs from the current
#                     head SHA. The verdict describes a tree that no longer
#                     exists; it must not be trusted.
#
# NOT_OPEN is first for a reason (#6781). A merge that lands between a caller's
# read and this check is the ordinary "completed externally (daemon/champion)"
# race (#4884), and a merged PR's head SHA routinely differs from the SHA its
# approval was rendered against (a last commit, a rebase, or the merge itself).
# Without the state check the guard reads that difference as STALE and, with
# --clear, strips `loom:pr` and re-adds `loom:review-requested` on a PR that is
# already merged — observed live on PR #6772 (2026-08-23). Every consumer of
# `loom:review-requested` filters on state==OPEN today, so nothing acted on it,
# but the guard was writing verdict labels onto finished work.
#
# Marker selection deliberately filters on `verdict=` (not just "the newest
# marker of any kind"): a PR that was rejected at SHA A and later approved at
# SHA B has markers for both, and only the one matching the CURRENTLY-HELD
# label says anything about the current verdict. A verdict label with no
# marker of its own kind is UNVERIFIABLE, not STALE — see #2 above.
#
# Any head-SHA change invalidates the verdict — there is deliberately NO
# force-push-vs-fast-forward detector here. For a statement about a tree, an
# appended commit is just as much "not the tree I reviewed" as a rebase is,
# and the extra machinery would not change a single answer (#5686 explicitly
# scopes it out).
#
# Usage:
#   verdict-staleness-guard.sh <pr-number>            # report only
#   verdict-staleness-guard.sh <pr-number> --clear     # report + act on STALE
#   verdict-staleness-guard.sh <pr-number> --anchor    # report + act on UNVERIFIABLE
#
# With --clear, a STALE verdict is cleared in one transition:
#   - remove BOTH terminal verdict labels (`loom:pr` AND `loom:changes-requested`),
#     not just the one detected as stale — a stray copy of the other, left
#     behind by an earlier contradictory state or a manual label edit, must
#     not survive this transition either (#7018). `gh ... --remove-label` on a
#     label that isn't present is a no-op, so requesting removal of both is
#     always safe.
#   - remove its per-tree companions (`loom:ci-failure`, `loom:merge-conflict`)
#     when present — those are findings about the OLD tree too
#   - add `loom:review-requested` so a Judge picks the PR up again
#   - post an auditable comment naming the old and new SHAs
#
# With --anchor, an UNVERIFIABLE verdict is remediated rather than merely
# reported (#6319): the guard posts a comment carrying the marker the verdict
# should have had, recording the head SHA as of NOW.
#
# The marker is prose-compliance, not a mechanism — judge.md ASKS the model to
# append it at every one of ~19 verdict-write sites, and production dropped it
# on roughly one verdict in four. Every dropped marker silently reinstates the
# pre-#5686 hazard for the life of the label: the approval survives any
# force-push undetected and Champion may auto-merge a tree nobody approved.
# Anchoring bounds that exposure to one pass instead of forever.
#
# Anchoring is deliberately NOT a verdict:
#   - It writes NO labels. The verdict label was already there and stays
#     exactly as it was, so anchoring cannot approve, reject, or un-park
#     anything — the only state it changes is "this verdict can now be
#     checked". It is therefore safe in a way --clear is not.
#   - It cannot reconstruct which tree was actually reviewed. If the head
#     already moved before the anchor, the verdict is anchored to a tree that
#     may never have been reviewed. Anchoring bounds FUTURE exposure only, and
#     is a backstop for judge.md's marker, never a substitute for it.
#   - It is idempotent: the marker it posts is exactly what step 3 scans for,
#     so the next run reads FRESH and never anchors twice.
#   - It is suppressed on a hold label, like --clear (see below): a PR a human
#     deliberately parked should not collect automated comments either.
#
# --clear is suppressed (DECISION stays STALE, CLEARED=0) when the PR carries
# an explicit hold label — `loom:blocked`, `loom:operator`, or
# `loom:operator-only`. Those mark a PR a human (or Champion's capped-PR
# recovery pass) deliberately took out of automated flow; silently re-queueing
# it for review would undo that decision. Callers must still treat the verdict
# as untrustworthy: STALE is STALE whether or not it was cleared.
#
# Output (stdout — one KEY=VALUE per line, machine-parseable):
#   DECISION=NOT_OPEN|NO_VERDICT|UNVERIFIABLE|ANCHORED|FRESH|STALE
#   REASON=<short human-readable reason>
#   HEAD_SHA=<current head sha>
#   VERDICT_LABEL=<loom:pr|loom:changes-requested|"">
#   MARKER_SHA=<sha the verdict was recorded against, or "">
#   CLEARED=0|1
#   ANCHORED=0|1
#
# Exit codes:
#   0  = FRESH (verdict is valid for the current head — safe to act on)
#   10 = NO_VERDICT (no terminal verdict label on this PR)
#   11 = UNVERIFIABLE (verdict present, no marker — fail safe, verdict kept)
#   12 = STALE (verdict invalidated by a head-SHA move)
#   13 = ANCHORED (was UNVERIFIABLE; --anchor stamped a marker at the current
#        head, so it is invalidatable from here on. Labels untouched.)
#   14 = NOT_OPEN (PR is merged or closed — nothing was read past the PR's own
#        state and nothing was written. NOT an error: it means "this PR is
#        finished, there is no verdict left to act on". Callers that already
#        route every non-{0,11} code away from merging need no change; a caller
#        that reports unexpected codes should classify 14 as a skip, not a
#        `gh` failure.) (#6781)
#   1  = usage or environment error (bad args, `gh` call failed). Callers must
#        treat this like any other `gh` failure — NOT as "the verdict is fine".
#
# CALLERS MUST NOT SWALLOW THE EXIT CODE with `|| true` (#6319). UNVERIFIABLE
# is the one outcome that looks like success and is not: it means a verdict
# label is standing that nothing can ever invalidate. Count it, report it, or
# pass --anchor to fix it — but do not discard it.
#
# This script decides about ONE given PR number. Finding the candidate set
# (open PRs carrying a verdict label) stays with the caller — judge.md's
# stale-verdict sweep, champion-pr-merge.md's Verdict-State Janitor, and
# loom-daemon's `reconcile_pr_verdicts` backstop each walk their own queue.

set -uo pipefail

PR=""
CLEAR=0
ANCHOR=0

usage() {
  echo "Usage: $0 <pr-number> [--clear] [--anchor]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clear) CLEAR=1; shift ;;
    --anchor) ANCHOR=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*)
      echo "ERROR: unknown option: $1" >&2
      usage
      exit 1
      ;;
    *)
      if [[ -n "$PR" ]]; then
        echo "ERROR: unexpected extra argument: $1" >&2
        usage
        exit 1
      fi
      PR="$1"
      shift
      ;;
  esac
done

if [[ -z "$PR" || ! "$PR" =~ ^[0-9]+$ ]]; then
  echo "ERROR: a numeric PR number is required" >&2
  usage
  exit 1
fi

for bin in gh jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found on PATH" >&2; exit 1; }
done

# The two terminal verdict labels and the marker `verdict=` token each one is
# recorded under. Kept as parallel lookups rather than one map so this stays
# POSIX-ish bash 3.2 compatible (macOS ships bash 3.2 — no associative arrays).
verdict_token_for_label() { # <label> -> approved|changes-requested
  case "$1" in
    "loom:pr") echo "approved" ;;
    "loom:changes-requested") echo "changes-requested" ;;
    *) echo "" ;;
  esac
}

emit() {
  local decision="$1" reason="$2" head_sha="$3" verdict_label="$4" marker_sha="$5" cleared="$6"
  local anchored="${7:-0}"
  echo "DECISION=$decision"
  echo "REASON=$reason"
  echo "HEAD_SHA=$head_sha"
  echo "VERDICT_LABEL=$verdict_label"
  echo "MARKER_SHA=$marker_sha"
  echo "CLEARED=$cleared"
  echo "ANCHORED=$anchored"
}

# Keep `gh`'s stdout (the JSON we parse) and stderr SEPARATE. `gh` writes
# incidental content to stderr even on a successful exit (update-notifier
# banners, rate-limit hints, proxy/TLS warnings); merging that into stdout
# with `2>&1` corrupts the JSON before `jq` sees it. Same lesson as #5455's
# Judge finding on judge-fallback-guard.sh, where merged streams silently
# zeroed a marker count and defeated the guard it was protecting.
GH_STDERR="$(mktemp)"
trap 'rm -f "$GH_STDERR" 2>/dev/null || true' EXIT

# --- Step 1: current head SHA + current labels + open/closed state ----------
# `state` is fetched in the SAME call as the head SHA, not a second round
# trip: the whole point is that the PR may finish between any two reads, so
# the state must come from the same snapshot the SHA came from. `merged` is
# deliberately NOT requested here — real `gh pr view --json` rejects it as an
# unknown field (it only exposes `mergedAt`/`closed`, not a `merged` boolean;
# confirmed against gh 2.97.0/2.98.0). `state` alone already reports MERGED,
# so nothing is lost. Any `.merged` read below is defensive only, for a
# hypothetical forge shim that emits REST-shaped JSON with a literal `merged`
# key; on real `gh` output it is simply absent.
PR_JSON="$(gh pr view "$PR" --json headRefOid,labels,state 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh pr view $PR --json headRefOid,labels,state' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}

HEAD_SHA="$(jq -r '.headRefOid // empty' <<<"$PR_JSON" 2>/dev/null || true)"
if [[ -z "$HEAD_SHA" ]]; then
  echo "ERROR: could not resolve head SHA for PR #$PR from: $PR_JSON" >&2
  exit 1
fi

LABELS="$(jq -r '[.labels[].name] | join("\n")' <<<"$PR_JSON" 2>/dev/null || true)"
has_label() { printf '%s\n' "$LABELS" | grep -qx -- "$1"; }

# The explicit-hold labels: a PR an operator (or Champion's capped-PR recovery
# pass) deliberately took out of automated flow. Echoes the first one present,
# or nothing. Shared by --clear (step 5) and --anchor (step 3b) so the two
# write paths can never disagree about what "parked" means.
hold_label() {
  local held
  for held in "loom:blocked" "loom:operator" "loom:operator-only"; do
    if has_label "$held"; then echo "$held"; return 0; fi
  done
  echo ""
}

# Which terminal verdict (if any) does this PR carry? `loom:pr` is checked
# first: when both are somehow present (the contradictory state
# champion-pr-merge.md's Verdict-State Janitor exists to resolve, #4570), the
# approving label is the dangerous one and is what we must reason about.
current_verdict_label() {
  if has_label "loom:pr"; then
    echo "loom:pr"
  elif has_label "loom:changes-requested"; then
    echo "loom:changes-requested"
  else
    echo ""
  fi
}

# --- Step 1b: is the PR still open? (#6781) ---------------------------------
# Short-circuits BEFORE the comment fetch, the FRESH/STALE comparison, and both
# write paths (--clear, --anchor), so a merged/closed PR can never reach a
# label or comment write no matter what the marker says.
#
# `gh pr view --json state` reports OPEN | CLOSED | MERGED; the REST shape a
# forge shim may return instead is state=closed with merged=true. Both are
# handled: anything that is not OPEN, or that is merged, is not open.
#
# A MISSING state field is deliberately treated as open (pre-#6781 behavior).
# Failing closed on an absent field would mean a forge shim that does not
# report `state` silently loses stale-verdict clearing altogether — which
# reinstates the #5686 hazard (a stale approval standing on a live PR) to guard
# against mislabeling PRs that are already finished. The former is far worse.
PR_STATE="$(jq -r '.state // empty' <<<"$PR_JSON" 2>/dev/null || true)"
PR_STATE_UC="$(printf '%s' "$PR_STATE" | tr '[:lower:]' '[:upper:]')"
# `.merged // false` (not `// empty`): jq's `//` also swallows a literal
# `false`, so the alternative supplies the default for BOTH null and false.
PR_MERGED="$(jq -r 'if (.merged // false) then "true" else "false" end' <<<"$PR_JSON" 2>/dev/null || true)"

if [[ "$PR_MERGED" == "true" || ( -n "$PR_STATE_UC" && "$PR_STATE_UC" != "OPEN" ) ]]; then
  # Real `gh pr view --json state` reports MERGED directly, so prefer it over
  # PR_MERGED for the human-readable distinction — PR_MERGED is permanently
  # "false" on real gh output now that `merged` is no longer a requested
  # field (see the Step 1 comment above), so keying off it alone would
  # mislabel every real merge as "closed without merging".
  NOT_OPEN_WHAT="closed without merging"
  if [[ "$PR_MERGED" == "true" || "$PR_STATE_UC" == "MERGED" ]]; then
    NOT_OPEN_WHAT="merged"
  fi
  emit "NOT_OPEN" "PR is $NOT_OPEN_WHAT (state=${PR_STATE:-unknown}, merged=$PR_MERGED) — verdict labels are never rewritten on a finished PR" \
    "$HEAD_SHA" "$(current_verdict_label)" "" 0 0
  exit 14
fi

# --- Step 2: which terminal verdict (if any) does this PR carry? ------------
VERDICT_LABEL="$(current_verdict_label)"

if [[ -z "$VERDICT_LABEL" ]]; then
  emit "NO_VERDICT" "PR carries no terminal verdict label (loom:pr / loom:changes-requested)" \
    "$HEAD_SHA" "" "" 0
  exit 10
fi

VERDICT_TOKEN="$(verdict_token_for_label "$VERDICT_LABEL")"

# --- Step 3: newest verdict marker for THIS verdict kind --------------------
# --paginate is REQUIRED: without it `gh api` returns only the first page
# (default per_page=30, oldest-first), so on a long-running PR the verdict
# marker — always among the NEWEST comments — would never be seen and every
# verdict would read as UNVERIFIABLE.
COMMENTS_JSON="$(gh api "repos/{owner}/{repo}/issues/$PR/comments" --paginate 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh api .../issues/$PR/comments --paginate' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}

# One "<created_at>\t<sha>" line per matching marker, oldest first (matches
# --paginate's page order). `test(...)` guards `capture(...)` so a non-matching
# body is filtered out via `select` rather than raising a per-item jq error.
# A short (abbreviated) SHA is accepted defensively but never emitted by the
# roles, which always stamp the full `headRefOid`.
MARKER_TEST="<!-- loom:verdict-sha sha=[0-9a-f]{7,40} verdict=$VERDICT_TOKEN -->"
MARKER_CAPTURE="<!-- loom:verdict-sha sha=(?<sha>[0-9a-f]{7,40}) verdict=$VERDICT_TOKEN -->"
MARKER_LINES="$(jq -r --arg t "$MARKER_TEST" --arg c "$MARKER_CAPTURE" '
  .[]
  | select(.body != null and (.body | test($t)))
  | [.created_at, (.body | capture($c).sha)]
  | @tsv
' <<<"$COMMENTS_JSON" 2>/dev/null || true)"

MARKER_SHA=""
if [[ -n "$MARKER_LINES" ]]; then
  MARKER_SHA="$(tail -n 1 <<<"$MARKER_LINES" | cut -f2)"
fi

if [[ -z "$MARKER_SHA" ]]; then
  UNVERIFIABLE_REASON="verdict label $VERDICT_LABEL present but no <!-- loom:verdict-sha ... verdict=$VERDICT_TOKEN --> marker found (marker never written) — failing safe, verdict kept"

  # --- Step 3b: UNVERIFIABLE — optionally anchor to the current head (#6319) -
  # Note the asymmetry with --clear below, and that it is deliberate: this
  # posts a comment but touches NO labels, so it cannot approve, reject, or
  # re-queue anything. It only makes the standing verdict checkable from here
  # on. Without it the verdict stays permanently unverifiable and keeps the
  # full pre-#5686 hazard for as long as the label sits there.
  if [[ "$ANCHOR" -eq 1 ]]; then
    HOLD_LABEL="$(hold_label)"
    if [[ -n "$HOLD_LABEL" ]]; then
      emit "UNVERIFIABLE" "$UNVERIFIABLE_REASON; anchor suppressed — PR is on an explicit $HOLD_LABEL hold" \
        "$HEAD_SHA" "$VERDICT_LABEL" "" 0 0
      exit 11
    fi

    gh pr comment "$PR" --body "<!-- loom:verdict-sha sha=$HEAD_SHA verdict=$VERDICT_TOKEN -->
**Verdict anchored to the current head — no marker had been recorded**

This PR carries \`$VERDICT_LABEL\`, but no verdict-SHA marker was ever written for that verdict, so it was **unverifiable**: nothing could tell whether it still described the tree in front of it, and it would have survived a force-push undetected — the exact pre-#5686 hazard.

This comment records the head SHA as of now, \`$HEAD_SHA\`. It is **not** a review and implies no judgment about this tree: the \`$VERDICT_LABEL\` label is unchanged. From here on the verdict is invalidatable — if the head moves off \`$HEAD_SHA\`, the stale-verdict pass clears \`$VERDICT_LABEL\` and returns the PR to \`loom:review-requested\`.

Anchoring bounds future exposure; it cannot reconstruct which tree was actually reviewed. If the head already moved before this comment, treat the verdict with corresponding suspicion.

---
*Automated by verdict-staleness-guard.sh (#6319)*" >/dev/null 2>"$GH_STDERR" || {
      echo "ERROR: failed to post verdict-anchor comment on PR #$PR: $(cat "$GH_STDERR" 2>/dev/null)" >&2
      emit "UNVERIFIABLE" "$UNVERIFIABLE_REASON; anchor failed" \
        "$HEAD_SHA" "$VERDICT_LABEL" "" 0 0
      exit 1
    }

    emit "ANCHORED" "verdict $VERDICT_LABEL had no marker and was anchored to the current head $HEAD_SHA — invalidatable from here on; labels untouched" \
      "$HEAD_SHA" "$VERDICT_LABEL" "$HEAD_SHA" 0 1
    exit 13
  fi

  emit "UNVERIFIABLE" "$UNVERIFIABLE_REASON" \
    "$HEAD_SHA" "$VERDICT_LABEL" "" 0 0
  exit 11
fi

# --- Step 4: fresh or stale? ------------------------------------------------
# Compare on the marker's own length so a legitimately abbreviated marker SHA
# still matches the full head SHA it prefixes (the roles stamp full SHAs; this
# only guards a hand-written or truncated marker).
if [[ "${HEAD_SHA:0:${#MARKER_SHA}}" == "$MARKER_SHA" ]]; then
  emit "FRESH" "verdict $VERDICT_LABEL was rendered against the current head SHA" \
    "$HEAD_SHA" "$VERDICT_LABEL" "$MARKER_SHA" 0
  exit 0
fi

# --- Step 5: STALE — optionally clear + re-queue -----------------------------
CLEARED=0
REASON="verdict $VERDICT_LABEL was rendered against $MARKER_SHA but head is now $HEAD_SHA"

if [[ "$CLEAR" -eq 1 ]]; then
  HOLD_LABEL="$(hold_label)"

  if [[ -n "$HOLD_LABEL" ]]; then
    REASON="$REASON; clear suppressed — PR is on an explicit $HOLD_LABEL hold"
  else
    # Idempotency: if this exact old->new transition was already announced,
    # don't post a second comment (a Judge pass and the daemon backstop can
    # both notice the same move). The label writes below are idempotent on
    # their own, so this only guards comment spam on a partial-failure retry.
    STALE_MARKER="<!-- loom:verdict-stale from=$MARKER_SHA to=$HEAD_SHA -->"
    ALREADY_ANNOUNCED="$(jq -r --arg m "$STALE_MARKER" \
      '[.[] | select(.body != null and (.body | contains($m)))] | length' \
      <<<"$COMMENTS_JSON" 2>/dev/null || echo 0)"

    if [[ "${ALREADY_ANNOUNCED:-0}" -eq 0 ]]; then
      gh pr comment "$PR" --body "$STALE_MARKER
**Stale review verdict cleared — head SHA moved**

This PR's \`$VERDICT_LABEL\` verdict was rendered against \`$MARKER_SHA\`, but the current head is \`$HEAD_SHA\`. A review verdict is a statement about a specific tree, so it does not survive a rebase, a force-push, or new commits.

- Verdict cleared: \`$VERDICT_LABEL\` (recorded for \`$MARKER_SHA\`)
- Returned to the review queue: \`loom:review-requested\` (current head \`$HEAD_SHA\`)

Judge will re-evaluate the tree that is actually here now. No judgment about the new tree is implied either way — the old verdict simply no longer describes it.

---
*Automated by verdict-staleness-guard.sh (#5686)*" >/dev/null 2>"$GH_STDERR" || {
        echo "ERROR: failed to post stale-verdict comment on PR #$PR: $(cat "$GH_STDERR" 2>/dev/null)" >&2
        emit "STALE" "$REASON; comment failed, labels left untouched" \
          "$HEAD_SHA" "$VERDICT_LABEL" "$MARKER_SHA" 0
        exit 1
      }
    fi

    # Comment first, then labels — the same ordering the roles use for their
    # own verdict writes, so the audit trail can never show a label flip with
    # no explanation attached.
    #
    # Strip BOTH terminal verdict labels here, not just the one this pass
    # detected as stale ($VERDICT_LABEL) — issue #7018. current_verdict_label()
    # only ever picks ONE label to reason about (loom:pr first, since it is
    # the dangerous direction), so if the OTHER verdict label is also present
    # — leftover debris from an earlier contradictory state, a manual label
    # edit, or a bug elsewhere — a single-label removal here leaves it behind
    # and re-adds loom:review-requested on top of it, producing exactly the
    # mutual-exclusion violation this guard exists to prevent. Removing a
    # label that isn't present is a documented no-op for `gh ... --remove-label`
    # (see champion-issue-promo.md's Pass 0b race-safety note), so it is safe
    # to always request removal of both regardless of which one is actually
    # on the PR.
    EDIT_ARGS=(--add-label "loom:review-requested")
    for verdict in "loom:pr" "loom:changes-requested"; do
      EDIT_ARGS+=(--remove-label "$verdict")
    done
    for companion in "loom:ci-failure" "loom:merge-conflict"; do
      if has_label "$companion"; then
        EDIT_ARGS+=(--remove-label "$companion")
      fi
    done
    if gh pr edit "$PR" "${EDIT_ARGS[@]}" >/dev/null 2>"$GH_STDERR"; then
      CLEARED=1
      REASON="$REASON; cleared and re-queued as loom:review-requested"
    else
      echo "ERROR: failed to clear $VERDICT_LABEL on PR #$PR: $(cat "$GH_STDERR" 2>/dev/null)" >&2
      emit "STALE" "$REASON; label clear failed" \
        "$HEAD_SHA" "$VERDICT_LABEL" "$MARKER_SHA" 0
      exit 1
    fi
  fi
fi

emit "STALE" "$REASON" "$HEAD_SHA" "$VERDICT_LABEL" "$MARKER_SHA" "$CLEARED"
exit 12
