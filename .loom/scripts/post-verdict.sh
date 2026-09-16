#!/usr/bin/env bash
# post-verdict.sh — post a Judge (or Judge-equivalent) verdict comment with
# its mandatory `loom:verdict-sha` marker appended by the script itself,
# rather than typed as prose (#6382).
#
# Why this exists: `verdict-staleness-guard.sh` (#5686) binds a review
# verdict to the tree it was rendered against via a
#
#     <!-- loom:verdict-sha sha=<head-sha> verdict=approved|changes-requested -->
#
# marker on the terminal verdict comment. Before this script, every
# verdict-posting call site in judge.md typed that marker as literal prose in
# a `gh pr comment --body "..."` heredoc — around twenty near-identical call
# sites (#6382's Curator count). judge.md itself records the failure mode
# (#6319): the marker was measured dropped on roughly one verdict in four,
# by the same identity, in the same session — a compliance rate, not a stale
# prompt. Per verdict-staleness-guard.sh's own contract a MISSING marker
# fails safe (UNVERIFIABLE, exit 11, verdict kept) — so a dropped marker does
# not fail loudly, it silently opts that PR out of staleness protection until
# a later anchor pass or a human notices.
#
# This script makes the marker part of the POSTING MECHANISM instead of the
# PROSE: the caller passes the verdict SHA as an argument, and the marker is
# appended unconditionally — there is no code path that posts a verdict
# comment without one.
#
# What this script deliberately does NOT do: it does not touch labels. The
# label transition that follows a verdict comment (loom:review-requested ->
# loom:pr / loom:changes-requested, plus per-PR companions like
# loom:ci-failure / loom:merge-conflict) still varies by call site and stays
# in the caller, chained with `&&` exactly as before — only the
# comment-posting half is centralized. It also does not decide FRESH/STALE/
# UNVERIFIABLE — that reasoning, and the marker FORMAT it depends on, stays
# single-sourced in verdict-staleness-guard.sh; this script's job is only to
# guarantee that whatever marker DOES get posted matches what that guard
# parses (pinned by test-post-verdict.sh, which checks byte-for-byte
# agreement with verdict-staleness-guard.sh's own MARKER_TEST/MARKER_CAPTURE
# regexes — see that script for why this must never become a second, silently
# diverging definition of the marker format).
#
# FORMAL-REVIEW RECONCILIATION GATE (#7647)
#
# Every `approved` verdict — from the full evaluation AND from every fast path
# (Docs-Only, conflict-only fast-track, minor-description-fix, trivial-fix),
# because they all post through this one script — first runs
# `check-review-feedback.sh`, which ingests the PR's formal reviews
# (`pulls/{n}/reviews`, fully paginated) and inline review threads
# (`pulls/{n}/comments` + GraphQL `reviewThreads`). If anything is outstanding
# — or if the read did not complete — the approval comment is REFUSED (exit 3)
# unless the caller supplies `--reviews-reconciled TEXT` explaining which
# findings were fixed, superseded, or remain blocking. That text, and the
# gate's own counts, are appended to the posted comment, so a reconciliation is
# always auditable on the forge rather than asserted in a transcript.
#
# This is the mechanism half of #7647: reading `pulls/reviews` before approving
# stops being something an agent must remember (kicad-tools#5369 approved a PR
# over an unresolved same-head CHANGES_REQUESTED review it had never read) and
# becomes a precondition of the posting mechanism itself — the same move #6382
# made for the verdict-sha marker.
#
# Every READ failure fails closed (an unread review state is never evidence of
# "no blockers"). The single exception is a gate script that is not installed
# at all: that is an install defect that says nothing about this PR, and
# failing closed on it would stall every approval on every host until someone
# resynced — so it degrades to the pre-#7647 behaviour with a loud stderr
# warning and a `state=gate-unavailable` reconciliation marker on the comment.
#
# Usage:
#   post-verdict.sh <pr-number> <approved|changes-requested> <sha> \
#       (--body TEXT | --body-file PATH) [--reviews-reconciled TEXT]
#
#   PATH may be "-" to read the body from stdin.
#
#   --reviews-reconciled TEXT   Explicit disposition of the outstanding formal
#       reviews / inline threads that check-review-feedback.sh reported. Must
#       cite every blocking review id it named. Ignored for
#       `changes-requested` (the gate only guards approvals).
#
# Output: whatever `gh pr comment` prints on success (the comment URL) —
# unchanged, so a caller parsing that output needs no change.
#
# Exit codes:
#   0 - comment posted
#   1 - the `gh pr comment` call failed
#   2 - invalid arguments (bad PR number, verdict token, or SHA; missing body)
#   3 - approval refused by the formal-review reconciliation gate (#7647)
#
# NOTE: GitHub-specific (uses `gh pr comment`), like create-pr.sh /
# merge-pr.sh. On a Gitea forge, post the equivalent comment via that forge's
# own CLI and append the identical marker by hand.

set -uo pipefail

usage() {
  sed -n '2,86p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 3 ]]; then
  echo "post-verdict.sh: usage: post-verdict.sh <pr-number> <approved|changes-requested> <sha> (--body TEXT | --body-file PATH)" >&2
  exit 2
fi

PR="$1"
VERDICT="$2"
SHA="$3"
shift 3

BODY=""
BODY_FILE=""
HAVE_BODY=false
RECONCILED=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --body)
      BODY="${2:-}"
      HAVE_BODY=true
      shift 2
      ;;
    --body-file)
      BODY_FILE="${2:-}"
      shift 2
      ;;
    --reviews-reconciled)
      RECONCILED="${2:-}"
      shift 2
      ;;
    *)
      echo "post-verdict.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$PR" || ! "$PR" =~ ^[0-9]+$ ]]; then
  echo "post-verdict.sh: a numeric PR number is required, got: '$PR'" >&2
  exit 2
fi

# `gh pr comment --body @path` does NOT expand @path — it posts the literal
# string as the comment (the anti-pattern that destroyed a Judge review on PR
# #4457, and is a hard-denied Bash pattern for a LITERAL `gh pr comment ...
# --body @path` call — see comment-body-literal-path.md). That guard
# pattern-matches the literal command text, so it does not see this call
# (the top-level command is `post-verdict.sh`, not `gh pr comment`) — refuse
# the identical mistake here rather than silently reintroducing the hole one
# layer down.
if [[ "$HAVE_BODY" == "true" && "$BODY" == @* ]]; then
  echo "post-verdict.sh: --body starts with '@' — like 'gh pr comment --body @path', this posts the literal string, it does NOT read the file. Use --body-file <path> instead." >&2
  exit 2
fi

if [[ "$VERDICT" != "approved" && "$VERDICT" != "changes-requested" ]]; then
  echo "post-verdict.sh: verdict must be 'approved' or 'changes-requested', got: '$VERDICT'" >&2
  exit 2
fi

# A short (abbreviated) SHA is accepted defensively, matching
# verdict-staleness-guard.sh's own MARKER_TEST regex — the roles always stamp
# the full headRefOid, but nothing here should silently reject a hand-typed
# abbreviation that the guard would still parse correctly.
if [[ ! "$SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "post-verdict.sh: sha must be a 7-40 char lowercase hex string, got: '$SHA'" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]] && [[ "$HAVE_BODY" == "true" ]]; then
  echo "post-verdict.sh: --body and --body-file are mutually exclusive" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]]; then
  if [[ "$BODY_FILE" == "-" ]]; then
    BODY="$(cat)"
  elif [[ -r "$BODY_FILE" ]]; then
    BODY="$(cat "$BODY_FILE")"
  else
    echo "post-verdict.sh: cannot read --body-file: $BODY_FILE" >&2
    exit 2
  fi
fi

if [[ -z "$BODY" ]]; then
  echo "post-verdict.sh: --body or --body-file is required (and must be non-empty)" >&2
  exit 2
fi

# --- Formal-review reconciliation gate (#7647) -----------------------------
# Runs on APPROVALS ONLY — a changes-requested verdict cannot merge anything,
# so gating it would add forge reads for no safety gain. Because every approval
# path in judge.md posts through this script, gating here covers the fast paths
# too, without each of them having to opt in.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REVIEW_GATE="$SCRIPT_DIR/check-review-feedback.sh"
RECONCILIATION_MARKER=""
RECONCILIATION_NOTE=""

if [[ "$VERDICT" == "approved" ]]; then
  if [[ ! -x "$REVIEW_GATE" ]]; then
    # The gate script is not installed next to this one. That is an INSTALL
    # defect, not an unread review — and it is the one case where failing
    # closed would be worse than the disease: it would stall every approval on
    # every host until someone resynced, for a condition that says nothing
    # about this PR's review state. Degrade to the pre-#7647 behaviour, but
    # loudly and with a marker, so the gap is visible on the forge record
    # instead of silent. Every REAL read failure below still fails closed.
    echo "post-verdict.sh: WARNING — $REVIEW_GATE is missing or not executable; posting this approval WITHOUT formal-review reconciliation (#7647). Run ./.loom/scripts/resync-installed.sh from the primary checkout." >&2
    RECONCILIATION_MARKER="<!-- loom:review-reconciliation state=gate-unavailable reviews=0 blocking_current=0 blocking_older=0 inline_unresolved=0 source=none reconciled=no -->"
    GATE_SKIPPED=true
  fi
fi

if [[ "$VERDICT" == "approved" && "${GATE_SKIPPED:-false}" != "true" ]]; then
  GATE_OUT="$("$REVIEW_GATE" --number "$PR" --head-sha "$SHA" --quiet 2>&1)"
  GATE_RC=$?

  gate_field() {
    # Reads one KEY=VALUE line from the gate's output. The gate emits only
    # enum tokens, integers, SHAs, id lists and paths it created itself — but
    # parse rather than `eval` anyway: nothing here needs to execute.
    printf '%s\n' "$GATE_OUT" \
      | grep -m1 "^$1=" \
      | sed -e "s/^$1=//" -e 's/^"//' -e 's/"$//'
  }

  # The EXIT CODE is authoritative; the printed state is a cross-check. If the
  # two disagree (a truncated or mangled read), fail closed on UNKNOWN rather
  # than believing whichever one happens to be friendlier.
  case "$GATE_RC" in
    0) GATE_STATE_FROM_RC="CLEAR" ;;
    10) GATE_STATE_FROM_RC="BLOCKING" ;;
    11) GATE_STATE_FROM_RC="NEEDS_RECONCILIATION" ;;
    *) GATE_STATE_FROM_RC="UNKNOWN" ;;
  esac
  GATE_STATE="$(gate_field REVIEW_FEEDBACK_STATE)"
  if [[ "$GATE_STATE" != "$GATE_STATE_FROM_RC" ]]; then
    GATE_STATE="UNKNOWN"
  fi
  GATE_REVIEWS="$(gate_field REVIEWS_TOTAL)"
  GATE_BLOCK_CURRENT="$(gate_field REVIEWS_BLOCKING_CURRENT_HEAD)"
  GATE_BLOCK_OLDER="$(gate_field REVIEWS_BLOCKING_OLDER_HEAD)"
  GATE_INLINE_UNRESOLVED="$(gate_field INLINE_THREADS_UNRESOLVED)"
  GATE_SOURCE="$(gate_field INLINE_RESOLUTION_SOURCE)"
  GATE_BLOCKING_IDS="$(gate_field REVIEW_BLOCKING_IDS)"
  GATE_INLINE_BLOCKING_IDS="$(gate_field INLINE_BLOCKING_IDS)"
  GATE_FINDINGS_FILE="$(gate_field REVIEW_FEEDBACK_FINDINGS_FILE)"
  # Formal-review ids and unresolved-inline-thread ids are both "things a
  # BLOCKING/NEEDS_RECONCILIATION disposition must cite" — an inline-thread-only
  # block must not be waved through with a citation that only ever names formal
  # review ids (#7647 follow-up: the original cut only wired the formal half).
  # `read -ra` both trims and collapses whitespace, so two empty inputs collapse
  # to a genuinely empty string rather than a single stray space.
  read -ra _gate_all_ids <<< "$GATE_BLOCKING_IDS $GATE_INLINE_BLOCKING_IDS"
  # `${arr[*]+...}` rather than a bare `"${_gate_all_ids[*]}"`: under `set -u`,
  # bash < 4.4 calls an EMPTY array's expansion an unbound variable and aborts.
  # macOS ships 3.2.57 and will not ship newer. Both id strings are empty exactly
  # when nothing is blocking -- the CLEAR path -- so the unguarded form failed on
  # the common case and worked only when the PR had problems (#7783).
  GATE_ALL_BLOCKING_IDS="${_gate_all_ids[*]+${_gate_all_ids[*]}}"

  if [[ "$GATE_STATE" == "CLEAR" ]]; then
    RECONCILIATION_MARKER="<!-- loom:review-reconciliation state=clear reviews=${GATE_REVIEWS:-0} blocking_current=0 blocking_older=0 inline_unresolved=0 source=${GATE_SOURCE:-none} reconciled=n/a -->"
  else
    # Something is outstanding, or the read did not complete. Either way this
    # is NOT evidence of "no blockers" — refuse unless the caller dispositions
    # it explicitly.
    if [[ -z "$RECONCILED" ]]; then
      {
        echo "post-verdict.sh: REFUSING to post an approval — formal-review reconciliation gate: $GATE_STATE (#7647)"
        echo
        if [[ -n "$GATE_FINDINGS_FILE" && -r "$GATE_FINDINGS_FILE" ]]; then
          sed 's/^/  /' "$GATE_FINDINGS_FILE"
        else
          printf '%s\n' "$GATE_OUT" | sed 's/^/  /'
        fi
        echo
        echo "Reconcile each finding above against the current tree, then re-run with:"
        echo "  --reviews-reconciled \"<which findings were fixed / superseded / remain blocking, citing review/thread ids${GATE_ALL_BLOCKING_IDS:+: $GATE_ALL_BLOCKING_IDS}>\""
        echo "An UNKNOWN state means the read itself failed — re-run the gate before asserting anything about it."
        echo "Never dismiss a review to clear this gate, and never assert a resolution you did not verify."
      } >&2
      exit 3
    fi

    if [[ "${#RECONCILED}" -lt 40 ]]; then
      echo "post-verdict.sh: --reviews-reconciled must actually explain the disposition (got ${#RECONCILED} chars). Name each finding and whether it was fixed, superseded, or remains blocking." >&2
      exit 3
    fi

    # A BLOCKING state names concrete review/thread ids; the disposition must
    # cite each one — formal review ids AND unresolved inline-thread ids alike
    # (#7647 follow-up) — so "reconciled" cannot be a blanket sentence that
    # never read the findings. The match is boundary-anchored, not a bare
    # substring: an unanchored match would let a short id spuriously match
    # inside an unrelated longer number/opaque token. Quoting "$id" inside the
    # =~ pattern keeps it literal, so ids containing regex metacharacters
    # cannot corrupt the match.
    MISSING_IDS=""
    for id in $GATE_ALL_BLOCKING_IDS; do
      if [[ "$RECONCILED" =~ (^|[^[:alnum:]])"$id"($|[^[:alnum:]]) ]]; then
        :
      else
        MISSING_IDS="${MISSING_IDS:+$MISSING_IDS }$id"
      fi
    done
    if [[ -n "$MISSING_IDS" ]]; then
      echo "post-verdict.sh: REFUSING to post an approval — --reviews-reconciled does not address review/thread id(s): $MISSING_IDS (#7647)" >&2
      echo "Cite each blocking review/thread id and state what happened to its request." >&2
      exit 3
    fi

    GATE_STATE_TOKEN="$(printf '%s' "$GATE_STATE" | tr 'A-Z' 'a-z')"
    RECONCILIATION_MARKER="<!-- loom:review-reconciliation state=$GATE_STATE_TOKEN reviews=${GATE_REVIEWS:-0} blocking_current=${GATE_BLOCK_CURRENT:-0} blocking_older=${GATE_BLOCK_OLDER:-0} inline_unresolved=${GATE_INLINE_UNRESOLVED:-0} source=${GATE_SOURCE:-none} reconciled=yes -->"
    RECONCILIATION_NOTE="---

**Formal review reconciliation (#7647)** — \`check-review-feedback.sh\` reported **$GATE_STATE**: ${GATE_REVIEWS:-0} formal review(s), ${GATE_BLOCK_CURRENT:-0} outstanding at this head, ${GATE_BLOCK_OLDER:-0} at an older head, ${GATE_INLINE_UNRESOLVED:-0} unresolved inline thread(s) (resolution source: ${GATE_SOURCE:-none}).

$RECONCILED"
  fi
fi

# --- The marker is appended HERE, never accepted as part of $BODY ----------
# This is the entire point of the script: omission becomes structurally
# impossible instead of a matter of remembering to type it. Format must stay
# byte-identical to verdict-staleness-guard.sh's MARKER_TEST/MARKER_CAPTURE.
FULL_BODY="$BODY"
if [[ -n "$RECONCILIATION_NOTE" ]]; then
  FULL_BODY="$FULL_BODY

$RECONCILIATION_NOTE"
fi
if [[ -n "$RECONCILIATION_MARKER" ]]; then
  FULL_BODY="$FULL_BODY

$RECONCILIATION_MARKER"
fi
FULL_BODY="$FULL_BODY

<!-- loom:verdict-sha sha=$SHA verdict=$VERDICT -->"

gh pr comment "$PR" --body "$FULL_BODY"
