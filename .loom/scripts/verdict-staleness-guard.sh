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
#   3. UNVERIFIABLE — a verdict label is present, but no TRUSTED marker exists
#                     for THAT verdict kind (#9548: an untrusted author's marker is
#                     prose, not state). Fail safe: the verdict is kept.
#                     This is the pre-migration/rollout case (verdicts written
#                     before this guard shipped carry no marker), the
#                     mixed-fleet case (a host still running the older prompt),
#                     and — most commonly in practice — the case where the
#                     model simply dropped the marker (#6319: observed on
#                     roughly one verdict in four). Never force-clear on
#                     missing evidence; with --anchor, remediate instead (#3b).
#  3b. ANCHORED      — --anchor was passed and the UNVERIFIABLE verdict was
#                     given a marker recording the CURRENT head, so it becomes
#                     invalidatable from here on (#6319). `loom:changes-requested`
#                     only since #9258 (see 3c).
#  3c. STALE (unmarked approval, #9258) — `loom:pr` with NO trusted marker, and
#                     the markers could be read. Since #6382 post-verdict.sh
#                     appends a marker to every verdict, so a markerless approval
#                     means the mechanism was bypassed (the #9258 incident: an
#                     approval whose whole body was the literal `@-`). It is NOT
#                     merge-eligible and is NEVER anchored: anchoring would turn
#                     an approval posted at SHA A into a FRESH one at SHA B, a
#                     tree nobody reviewed. It reports DECISION=STALE (exit 12)
#                     with MARKER_SHA empty, and --clear re-queues it exactly like
#                     step 5 (hold labels still suppress the write), commenting
#                     "approval carried no verdict-sha marker; re-review
#                     required" under `<!-- loom:verdict-stale unanchored head=… -->`.
#                     When markers could NOT be authenticated it stays
#                     UNVERIFIABLE (exit 11, --anchor suppressed) — and exit 11
#                     on a `loom:pr` is never merged (champion-pr-merge.md).
#   4. FRESH        — the verdict still describes the tree in front of it,
#                     either because the newest matching marker's SHA equals the
#                     current head SHA, or because the head moved but the two
#                     commits' TREES are byte-identical (#9576 — see below).
#   5. STALE        — the newest matching marker's SHA differs from the current
#                     head SHA AND the trees differ (or the comparison could not
#                     be made). The verdict describes a tree that no longer
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
# ONE exception, and it is evidence rather than a heuristic (#9124/#9576/#9416):
# a head move across which THE CHANGE THIS PR MAKES is unchanged. `loom-daemon
# forge verdict-equivalent <pr> <marker> <head>` re-derives that from the
# repository and answers with the equivalence kind that proved it:
#
#   tree                    the two heads' trees are byte-identical — the #8248
#                           required-check-freshness guard's automated `chore:
#                           re-date required checks …` commit (#8508), whose
#                           whole purpose is to change nothing (#9124/#9576)
#   clean-merge             the head is exactly the clean automatic merge of
#                           this PR's base into the reviewed head (#9416)
#   rebase-patch-identical  the PR's own merge-base-relative patch is
#                           byte-identical before and after the move (#9416)
#
# In all three the verdict still describes the change in front of it, so clearing
# buys a full extra Judge cycle and nothing else. This is NOT a shape inference:
# nothing is read from the commit message, the author, the ref-update shape, or
# any marker — a marker is prose anyone can write (#9548).
#
# CI IS NOT EXEMPTED by any of them. Only the *review* carries over; every
# required check still re-runs against the new head, because the base really did
# move. This guard touches no check and no auto-merge arm on the FRESH path.
#
# THE COMPARISON IS NOT IMPLEMENTED HERE, for the same reason the #8900 disarm
# below is not: it already existed in loom-daemon (#9124 taught the daemon's
# periodic `reconcile_pr_verdicts` pass this exemption), and a second copy in
# shell is what `.loom/docs/shell-language-policy.md` forbids. That divergence
# is exactly the bug #9576 reports — this guard had NO tree comparison at all
# while the daemon did, so PRs #9541 and #9483 lost `loom:pr` here to a re-date
# commit the daemon pass would have kept, on a host already running #9124.
#
# FAIL CLOSED, VISIBLY: only a literal `EQUIVALENCE_KIND=<kind>` line suppresses
# the invalidation; when the verb could not answer, its one-line `Why:` (e.g. an
# unfetchable head commit) is carried into the STALE REASON (#10134). An absent binary, one predating the verb (clap exits non-zero
# with nothing on stdout), a `gh` outage, a non-GitHub forge, a shallow clone, a
# missing git object, a `merge-tree` conflict, an unparsable compare, or either
# kill switch all leave the answer empty and the verdict reads STALE exactly as
# it did before #9576 — the same fail-open-into-invalidation arm as the daemon's
# own `Indeterminate`.
#
# This guard does NOT re-anchor the marker to the new head when it takes that
# exemption (the daemon's carve-out does, in-process). Anchoring is a comment
# write and the marker prose lives in loom-daemon; the cost of not doing it is
# one extra compare call per pass until the daemon's periodic pass re-anchors it
# itself, which is strictly cheaper than a wrongly-cleared verdict.
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
#   - DISARM the forge's server-side auto-merge queue if one is armed (#8900)
#
# The disarm (#8900) is not optional politeness — without it the label flip is
# cosmetic. An armed GitHub auto-merge is gated ONLY by the branch ruleset's
# REQUIRED checks: it never re-reads `loom:pr`, never notices this very
# clearing, never waits for a non-required suite, and never runs merge-pr.sh's
# own merge-time gates (including the #8248 required-check freshness guard,
# which lives inside that script). So a PR whose verdict this guard had just
# invalidated still merged, unreviewed, the moment required checks went green on
# the new head: #8694 merged as 528f2971 on 2026-09-25, three minutes after a
# Doctor rebase force-push, still labeled `loom:review-requested`, with no
# approval at the merged head; #8847 and #8843 merged ~2 minutes after
# `gh pr update-branch` moved their heads.
#
# THE DISARM IS NOT IMPLEMENTED HERE. It is one call to
# `loom-daemon forge disable-auto-merge <pr> --audit-comment --hold <label>`,
# which reads the arm state, sends `disablePullRequestAutoMerge` only when
# something is actually armed, and posts its own audit comment recording what it
# did (silent when nothing was armed). The first draft of #8900 mirrored that
# mutation inline here in `gh api graphql` plus two comment bodies; review
# rejected it (PR #8990) as exactly the new portable shell
# `.loom/docs/shell-language-policy.md` forbids — new executable logic is a
# loom-daemon subcommand — and the shell-budget ratchet refused the PR outright.
# Do NOT reintroduce an inline mutation "as a fallback for hosts without
# loom-daemon": that duplication is the thing that was rejected. A host that
# cannot resolve the binary gets AUTO_MERGE_DISARMED=0 and a named failure in
# REASON, which is loud, plus the daemon's own periodic
# `claim_reconciliation` backstop for the non-held cases.
#
# The disarm runs BEFORE the comment and the label flip. That is the safest of
# the three possible orders: disarming can only PREVENT a merge, never cause
# one, so going first shrinks the window in which the queued merge could still
# fire, and a later comment/label failure leaves the PR disarmed with its
# verdict intact — strictly safer than the pre-#8900 behavior either way.
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
# The #8900 auto-merge disarm is deliberately NOT suppressed by a hold label.
# Every other write this guard makes could undo an operator's decision; the
# disarm is the one that ENFORCES it. A held PR with an armed auto-merge merges
# anyway the moment required checks pass — `loom:operator` means "the engine
# stops acting", and Champion's merge-risk hold applies it specifically to stop
# a merge, so leaving the forge's own queue armed defeats the hold entirely.
# Disarming can only prevent a merge, so it cannot be the write that undoes a
# hold. `--hold "$HOLD_LABEL"` is passed through to the subcommand so its audit
# comment can say why a parked PR was written to at all; a held PR with nothing
# armed still collects no comment and no write, exactly as before.
#
# Output (stdout — one KEY=VALUE per line, machine-parseable):
#   DECISION=NOT_OPEN|NO_VERDICT|UNVERIFIABLE|ANCHORED|FRESH|STALE
#   REASON=<short human-readable reason>
#   HEAD_SHA=<current head sha>
#   VERDICT_LABEL=<loom:pr|loom:changes-requested|"">
#   MARKER_SHA=<sha the verdict was recorded against, or "">
#   CLEARED=0|1
#   ANCHORED=0|1
#   AUTO_MERGE_DISARMED=0|1   (#8900 — 1 only when a queued server-side
#                              auto-merge was actually found armed AND
#                              successfully disabled on this run. 0 covers
#                              "nothing was armed", "the disarm failed", and
#                              "loom-daemon could not be resolved"; the latter
#                              two are named in REASON.)
#
# Exit codes:
#   0  = FRESH (verdict is valid for the current head — safe to act on)
#   10 = NO_VERDICT (no terminal verdict label on this PR)
#   11 = UNVERIFIABLE (verdict present, no marker — fail safe, verdict kept;
#        on a loom:pr only when markers could not be authenticated: not mergeable)
#   12 = STALE (verdict invalidated by a head-SHA move, or an approval with no
#        trusted marker at all — #9258, see 3c)
#   13 = ANCHORED (was UNVERIFIABLE; --anchor stamped a marker at the current
#        head, so it is invalidatable from here on. Labels untouched.
#        loom:changes-requested only, #9258.)
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

# Loom writes only to repos it manages (#9548). --clear/--anchor are vetted
# once, up front: a refusal turns them into a report-only run (the REASON says
# why) and every write below names the vetted repo explicitly, so gh's
# preference for an `upstream` remote can never redirect one.
# The helper prints only the repo on success and only the reason on failure,
# so one capture serves both. forge-helpers.sh is sourced in the subshell
# because it turns on `set -e`, which this script does not run under.
WRITE_REPO="" WRITE_BLOCK=""
if [[ "$CLEAR" -eq 1 || "$ANCHOR" -eq 1 ]] && ! WRITE_REPO="$(source "$(dirname "${BASH_SOURCE[0]:-$0}")/lib/forge-helpers.sh" && loom_write_repo "${LOOM_REPO:-}" 2>&1)"; then WRITE_BLOCK="loom-daemon forge may-write: $(printf '%s' "$WRITE_REPO" | tr '\n' ' ')"; WRITE_REPO=""; CLEAR=0; ANCHOR=0; fi

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
  echo "REASON=$reason${WRITE_BLOCK:+; --clear/--anchor suppressed, $WRITE_BLOCK}"
  echo "HEAD_SHA=$head_sha"
  echo "VERDICT_LABEL=$verdict_label"
  echo "MARKER_SHA=$marker_sha"
  echo "CLEARED=$cleared"
  echo "ANCHORED=$anchored"
  # Read from the global rather than an 8th positional arg (#8900): every
  # existing emit() call site keeps its signature, and the value is the same for
  # whichever emit() ends up firing. Unset on every path that returns before
  # step 5 (report-only, FRESH, NO_VERDICT, NOT_OPEN, UNVERIFIABLE), which is
  # exactly the paths on which no disarm was attempted, so the `:-0` default
  # states the truth rather than papering over one.
  echo "AUTO_MERGE_DISARMED=${AUTO_MERGE_DISARMED:-0}"
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
# Plain `gh`, never gh-cached (docs/gh-cached.md policy "verdict-time CAS
# rechecks", #9953): the guard exists to observe writes landing within the TTL.
COMMENTS_JSON="$(gh api "repos/{owner}/{repo}/issues/$PR/comments" --paginate 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh api .../issues/$PR/comments --paginate' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}

# #9548: a marker counts only from a TRUSTED author — a repo insider by
# author_association, one of this fleet's Apps, this daemon's own identity, or
# forge.trustedCommenters. Anyone can post a well-formed marker on a public
# repo, and another Loom fleet's markers are not ours; an untrusted marker is
# prose and reads exactly as if it were absent (so it can neither vouch for a
# verdict nor invalidate one). The predicate lives in the daemon
# (`loom-daemon forge trusted-comments`, loom-daemon/src/comment_trust.rs);
# this script owns none of it. If the filter cannot run, EVERY marker is
# treated as absent: the verdict reads UNVERIFIABLE (never FRESH on
# unauthenticated markers) and --anchor is suppressed, since anchoring an
# unverified verdict to the current head would itself mint a FRESH marker.
# requires-daemon: forge optional   Without the `trusted-comments` verb (an absent binary, or one predating #9548: clap exits 2 with nothing on stdout) every marker counts as absent, so the verdict reads UNVERIFIABLE with the cause named in REASON and --anchor does not post. No version floor on purpose: the degraded answer is the fail-safe one.
RAW_COMMENTS_JSON="$COMMENTS_JSON"
COMMENTS_JSON="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge trusted-comments <<<"$COMMENTS_JSON" 2>/dev/null)" && TRUSTED=1 || { COMMENTS_JSON='[]'; TRUSTED=0; }

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

# Newest marker wins (the lines are oldest-first). The `-n "$MARKER_LINES"`
# guard this used to carry was redundant — `tail -n 1` of the empty string is an
# empty line and `cut -f2` of an empty line is empty, so MARKER_SHA comes out ""
# either way, which is the value the UNVERIFIABLE branch below keys on. Dropping
# it pays for the three lines the #8900 disarm delegation adds above, per option
# 2 of the shell-budget gate's own remedies ("remove portable shell elsewhere in
# the same change to pay for it"); cases (g)/(h)/(i) cover the no-marker path.
MARKER_SHA="$(tail -n 1 <<<"$MARKER_LINES" | cut -f2)"

# #9258: an APPROVAL with no trusted marker is STALE, not UNVERIFIABLE — never
# anchored (3b would launder an approval posted at SHA A into a FRESH one at B),
# never merged, and re-queued by --clear (step 5). post-verdict.sh marks every
# verdict, so a markerless loom:pr bypassed it. Only when markers could be read
# (TRUSTED=1); a markerless loom:changes-requested still takes 3/3b below.
[[ -z "$MARKER_SHA" && "$VERDICT_TOKEN" == approved && "$TRUSTED" -eq 1 ]] && UNANCHORED_MARKER="<!-- loom:verdict-stale unanchored head=$HEAD_SHA -->" STALE_TITLE="Approval re-queued — it carried no verdict-sha marker; re-review required"
if [[ -z "$MARKER_SHA" && -z "${UNANCHORED_MARKER:-}" ]]; then
  UNVERIFIABLE_REASON="verdict label $VERDICT_LABEL present but no <!-- loom:verdict-sha ... verdict=$VERDICT_TOKEN --> marker from a trusted author found — failing safe, verdict kept"
  [[ "$TRUSTED" -eq 1 ]] || UNVERIFIABLE_REASON="$UNVERIFIABLE_REASON; markers could not be authenticated (loom-daemon forge trusted-comments unavailable), so every marker was treated as absent and --anchor is suppressed (#9548)"

  # --- Step 3b: UNVERIFIABLE — optionally anchor to the current head (#6319) -
  # Note the asymmetry with --clear below, and that it is deliberate: this
  # posts a comment but touches NO labels, so it cannot approve, reject, or
  # re-queue anything. It only makes the standing verdict checkable from here
  # on. Without it the verdict stays permanently unverifiable and keeps the
  # full pre-#5686 hazard for as long as the label sits there.
  if [[ "$ANCHOR" -eq 1 && "$TRUSTED" -eq 1 ]]; then
    HOLD_LABEL="$(hold_label)"
    if [[ -n "$HOLD_LABEL" ]]; then
      emit "UNVERIFIABLE" "$UNVERIFIABLE_REASON; anchor suppressed — PR is on an explicit $HOLD_LABEL hold" \
        "$HEAD_SHA" "$VERDICT_LABEL" "" 0 0
      exit 11
    fi

    gh pr comment "$PR" --repo "$WRITE_REPO" --body "<!-- loom:verdict-sha sha=$HEAD_SHA verdict=$VERDICT_TOKEN -->
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
# only guards a hand-written or truncated marker). That string compare is the
# first arm on purpose: it is the common case and needs no binary, no network
# and no forge, so a host that cannot resolve loom-daemon still reads FRESH
# normally and only loses the #9576 exemption.
#
# The second arm delegates whole to `loom-daemon forge verdict-equivalent`
# (loom-daemon/src/verdict_equivalence/) — the SAME function the daemon's
# periodic pass calls in-process, so the two can no longer disagree. It prints
# VERDICT_EQUIVALENT=1 plus EQUIVALENCE_KIND=<kind> and exits 0 when the verdict
# carries, VERDICT_EQUIVALENT=0 and exits 0 when it provably does not; anything
# else (exit 1, an absent binary, a daemon predating the verb,
# LOOM_VERDICT_TREE_CARVEOUT or LOOM_VERDICT_EQUIVALENCE switched off — the verb
# evaluates both kill switches itself) leaves EQUIVALENCE_KIND empty, which falls
# through to STALE. The verb supersedes `forge tree-unchanged` here: it asks that
# same tree-identical test FIRST and then the two #9416 kinds, so this arm can
# never be less permissive than it was before #9416. See the header for why every
# non-affirmative answer is fail-closed.
# requires-daemon: forge optional   Without the `verdict-equivalent` verb (an absent binary, or one predating #9416: clap exits non-zero with nothing on stdout) an equivalent head move reads STALE — the pre-#9576 behavior, which only ever costs a redundant Judge cycle. No version floor on purpose: the degraded answer is the fail-safe one.
FRESH_REASON=""
if [[ -n "$MARKER_SHA" && "${HEAD_SHA:0:${#MARKER_SHA}}" == "$MARKER_SHA" ]]; then
  FRESH_REASON="verdict $VERDICT_LABEL was rendered against the current head SHA"
elif [[ -n "$MARKER_SHA" ]]; then
  EQUIV_KIND="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-equivalent "$PR" "$MARKER_SHA" "$HEAD_SHA" 2>"$GH_STDERR" | sed -n 's/^EQUIVALENCE_KIND=//p')"
  EQUIV_WHY="$(sed -n 's/.* Why: //p' "$GH_STDERR" | head -n 1)"; if [[ -n "$EQUIV_KIND" ]]; then
    FRESH_REASON="verdict $VERDICT_LABEL was rendered against $MARKER_SHA and head is now $HEAD_SHA, but the change this PR makes is unchanged across the move (equivalence kind: $EQUIV_KIND) — so the verdict still describes it (#9576, #9416). CI still re-runs against $HEAD_SHA; only the review carries over."
  fi
fi

if [[ -n "$FRESH_REASON" ]]; then
  emit "FRESH" "$FRESH_REASON" "$HEAD_SHA" "$VERDICT_LABEL" "$MARKER_SHA" 0
  exit 0
fi

# --- Step 5: STALE — optionally clear + re-queue -----------------------------
CLEARED=0
REASON="verdict $VERDICT_LABEL was rendered against ${MARKER_SHA:-no recorded tree (no trusted verdict-sha marker: post-verdict.sh always writes one, so the mechanism was bypassed; never anchored or merged, #9258)} but head is now $HEAD_SHA${EQUIV_WHY:+; equivalence could not be checked, failing closed (#10134): $EQUIV_WHY}"

if [[ "$CLEAR" -eq 1 ]]; then
  HOLD_LABEL="$(hold_label)"

  # #8900: stand down the forge's queued server-side merge BEFORE the comment
  # and the label flip, and before the hold branch below — the disarm is the one
  # write a hold does NOT suppress (see the header). Delegated whole to
  # `loom-daemon forge disable-auto-merge`, which reads the arm state, mutates
  # only when something is armed, and posts its own audit comment; this guard
  # deliberately owns none of that logic (header: "THE DISARM IS NOT IMPLEMENTED
  # HERE"). `--hold` is passed unconditionally — the subcommand reads an empty
  # value as "not held" — so no conditional argument assembly is needed.
  #
  # The binary is probed through ${LOOM_DAEMON_BIN:-loom-daemon}, the first two
  # tiers of lib/locate-daemon-bin.sh's own precedence (explicit override, then
  # $PATH). The full resolver is NOT sourced here on purpose: it would add more
  # portable shell to this `contract`-category script than the whole delegation
  # does, which the shell-budget ratchet refuses.
  #
  # stderr is deliberately NOT redirected — a failed disarm must reach the
  # caller's stderr, not a temp file nobody reads. An unresolvable binary or a
  # failed mutation leaves this empty, which emit() reports as
  # AUTO_MERGE_DISARMED=0 and the next line names in REASON.
  #
  # requires-daemon: forge optional   The guard probes with `command -v` first and degrades to AUTO_MERGE_DISARMED=0 with the failure named in REASON — an absent binary, and equally a resolved binary predating `disable-auto-merge` or its `--audit-comment`/`--hold` flags (clap exits non-zero with nothing on stdout), both land in that same branch. No version floor is declared on purpose: every other write this guard makes is unaffected, so refusing the whole stale-verdict clear over a missing disarm would trade the #5686 hazard back for the #8900 one (#8900/#8990).
  AUTO_MERGE_DISARMED="$(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" >/dev/null 2>&1 && "${LOOM_DAEMON_BIN:-loom-daemon}" forge disable-auto-merge "$PR" --audit-comment --hold "$HOLD_LABEL" | sed -n 's/^DISARMED=//p')"
  REASON="$REASON; auto-merge DISARMED=${AUTO_MERGE_DISARMED:-FAILED (could not resolve loom-daemon, or the disarm itself failed — a queued merge may still be armed; disarm it by hand)}"

  if [[ -n "$HOLD_LABEL" ]]; then
    REASON="$REASON; clear suppressed — PR is on an explicit $HOLD_LABEL hold"
  else
    # Idempotency: if this exact old->new transition was already announced,
    # don't post a second comment (a Judge pass and the daemon backstop can
    # both notice the same move). The label writes below are idempotent on
    # their own, so this only guards comment spam on a partial-failure retry.
    STALE_MARKER="${UNANCHORED_MARKER:-<!-- loom:verdict-stale from=$MARKER_SHA to=$HEAD_SHA -->}"
    ALREADY_ANNOUNCED="$(jq -r --arg m "$STALE_MARKER" \
      '[.[] | select(.body != null and (.body | contains($m)))] | length' \
      <<<"$COMMENTS_JSON" 2>/dev/null || echo 0)"

    # Labels first, then the comment (#10601) — the announcement claims a
    # state change, so it is only posted after the flip is verified (below).
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
    EDIT_ARGS=(--add-label "loom:review-requested" --remove-label "loom:pr" --remove-label "loom:changes-requested")
    for companion in "loom:ci-failure" "loom:merge-conflict"; do
      if has_label "$companion"; then
        EDIT_ARGS+=(--remove-label "$companion")
      fi
    done
    if gh pr edit "$PR" --repo "$WRITE_REPO" "${EDIT_ARGS[@]}" >/dev/null 2>"$GH_STDERR"; then
      CLEARED=1
      REASON="$REASON; cleared and re-queued as loom:review-requested"
    if [[ "${ALREADY_ANNOUNCED:-0}" -eq 0 ]]; then
      # #9709: the notice is rendered by the daemon's own template (`forge
      # verdict-stale-notice`), so the two stale-clear paths cannot drift. It is
      # fed the RAW listing: when a newer marker was dropped as untrusted, the
      # notice names its login + author_association and forge.trustedCommenters
      # instead of asserting a head move that may not have happened.
      # requires-daemon: forge optional   Without the `verdict-stale-notice` verb (a binary predating #9709: clap exits non-zero with nothing on stdout) the one-line fallback below is posted — same stale marker, so dedup holds; only the #9709 attribution is lost. No version floor: the clear itself is unaffected.
      STALE_BODY="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-stale-notice --label "$VERDICT_LABEL" --marker-sha "$MARKER_SHA" --head-sha "$HEAD_SHA" <<<"$RAW_COMMENTS_JSON" 2>/dev/null)"
      [[ "$STALE_BODY" == "$STALE_MARKER"* ]] || STALE_BODY="$STALE_MARKER
**${STALE_TITLE:-Stale review verdict cleared — head SHA moved}**: \`$VERDICT_LABEL\` was rendered against \`${MARKER_SHA:-no recorded tree}\`, head is now \`$HEAD_SHA\`; returned to \`loom:review-requested\`. *Automated by verdict-staleness-guard.sh (#5686)*"
      if ! gh pr comment "$PR" --repo "$WRITE_REPO" --body "$STALE_BODY" >/dev/null 2>"$GH_STDERR"; then
        # The flip already happened and the label state is the source of truth:
        # report it, do not revert (#10601).
        echo "ERROR: failed to post stale-verdict comment on PR #$PR (labels already flipped): $(cat "$GH_STDERR" 2>/dev/null)" >&2
        REASON="$REASON; announcement comment failed"
      fi
    fi
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
