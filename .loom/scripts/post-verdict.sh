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
# VERDICT GATE AND LABEL TRANSITION (#10581)
#
# Before posting, `loom-daemon forge verdict-gate` reads the PR's trusted
# comments and labels. An approval is REFUSED (exit 7, nothing posted) while
# `loom:ci-failure` is on the PR, when the newest verdict marker at this same
# head is changes-requested (unless --overrules-prior explains why each point
# no longer blocks), or when either read fails. A second verdict identical to
# one posted at the same head in the last 10 minutes (two concurrent Judges) is
# DEDUPED: no second comment, the labels are still applied. After posting,
# `loom-daemon forge verdict-labels` applies the base transition itself (approve:
# +loom:pr, -loom:changes-requested -loom:ci-failure -loom:reviewing
# -loom:review-requested; changes-requested: +loom:changes-requested, -loom:pr
# -loom:reviewing -loom:review-requested), verifies it, and on failure this
# script exits 8 with a repair command — a verdict comment never silently
# lacks its label (#10605). Companion labels a verdict ADDS (loom:ci-failure,
# loom:merge-conflict on a changes-requested) stay with the caller.
#
# What this script deliberately does NOT do: decide FRESH/STALE/
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
# EXACT-HEAD ALL-CI GATE (#10485)
#
# Every `approved` verdict (fast paths included, same single chokepoint) also
# requires every check on the exact reviewed head to be settled green. After the
# review gate, this runs `loom-daemon forge wait-checks <pr> --timeout 20` (bounded
# snapshot; override: LOOM_POST_VERDICT_CI_TIMEOUT; the one status reader, never a second policy here) and branches on
# its first output LINE, never its exit code: GREEN proceeds; NONE proceeds
# (the reader's zero-row settle: no required contexts, ~3 empty polls, so the
# timeout must stay >= the ~10 s settle window; 0 would read as TIMEOUT);
# RED (any failing check, required or not; cancelled, timed_out,
# action_required, stale and startup_failure count as failing; success,
# neutral and skipped count as green) refuses with exit 6; TIMEOUT (pending,
# or empty with required contexts, e.g. approval-required workflows), ERROR,
# HEAD-MOVED, a head other than <sha>, no sentinel (older/missing daemon) or a
# failed head read all refuse with exit 5. Immediately before posting, the PR
# head is re-read and must still be <sha>. Nothing is posted on a refusal, so
# the caller's `&&`-chained `loom:pr` label edit cannot run either.
#
# Usage:
#   post-verdict.sh <pr-number> <approved|changes-requested> <sha> \
#       (--body TEXT | --body-file PATH) [--reviews-reconciled TEXT] \
#       [--overrules-prior TEXT]
#
#   PATH may be "-" to read the body from stdin.
#
#   --reviews-reconciled TEXT   Explicit disposition of the outstanding formal
#       reviews / inline threads that check-review-feedback.sh reported. Must
#       cite every blocking review id it named. Ignored for
#       `changes-requested` (the gate only guards approvals).
#
#   --overrules-prior TEXT   Approvals only: why each point of a changes-requested
#       verdict at this SAME head no longer blocks (at least 40 chars). Without
#       it such an approval is refused (#10581); with no new push, the usual
#       answer is to wait for the Doctor rather than to overrule.
#
# Output: whatever `gh pr comment` prints on success (the comment URL) —
# unchanged, so a caller parsing that output needs no change.
#
# Exit codes:
#   0 - comment posted
#   1 - the `gh pr comment` call failed
#   2 - invalid arguments (bad PR number, verdict token, or SHA; missing body)
#   3 - approval refused by the formal-review reconciliation gate (#7647)
#   5 - approval refused, CI not settled or unverifiable on <sha> (#10485):
#       pending, head moved, reader error/absent. Post nothing approving,
#       leave loom:review-requested, retry on a later pass.
#   6 - approval refused, a check on <sha> is red (#10485), required or not.
#   4 - refused: the PR's repo is not one this installation may write to
#       (loom_write_repo, lib/forge-helpers.sh, #9548); nothing was posted
#   7 - refused by the verdict gate (#10581): loom:ci-failure on an approval,
#       a same-head changes-requested verdict not overruled, an unread PR
#       state, or a gate that gave no answer; nothing was posted
#   8 - the comment is posted (or deduped) but the label transition did not
#       hold; stderr carries the repair command (#10581)
#   9 - the per-PR verdict lock is held by another verdict transaction on
#       this host (loom-daemon forge verdict-lock, #10581); nothing was posted
#
#   A resolved loom-daemon without the #10581 verdict verbs (a capability
#   probe, not a version guess) takes the legacy path for either verdict: the
#   pre-#10581 posting plus a `gh pr edit` label flip, with a loud warning
#   naming the binary and its --version. Its exits are 0 / 1 / 5 / 8 above.
#
# NOTE: GitHub-specific (uses `gh pr comment`), like create-pr.sh /
# merge-pr.sh. On a Gitea forge, post the equivalent comment via that forge's
# own CLI and append the identical marker by hand.

set -uo pipefail

usage() {
  sed -n '2,144p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
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
RECONCILED="" OVERRULE=""

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
    --reviews-reconciled) RECONCILED="${2:-}"; shift 2 ;;
    --overrules-prior) OVERRULE="${2:-}"; shift 2 ;;
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

# --- Exact-head all-CI gate (#10485) ---------------------------------------
# Approvals only. Fails closed on EVERY unread/unsettled state; the daemon's
# wait-checks reader is the sole status policy (non-required red is RED there).
# stderr is merged into stdout: the reader prints the sentinel first, then any
# failing-check detail lines.
# requires-daemon: forge >= 0.19.707   #10330 added `forge wait-checks` (PR #10351, first shipped in 0.19.707). Approvals only: an older or absent binary prints no sentinel, so the gate refuses the approval (exit 5) naming this floor. Changes-requested verdicts never reach this call.
REPO=""
if [[ "$VERDICT" == "approved" ]]; then
  REPO="$(source "$SCRIPT_DIR/lib/forge-helpers.sh" && loom_write_repo "${LOOM_REPO:-}")" || { echo "post-verdict.sh: not posting the verdict on PR #$PR: loom-daemon forge may-write refused the repo (#9548)" >&2; exit 4; }
  CI_OUT="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge wait-checks "$PR" --repo "$REPO" --timeout "${LOOM_POST_VERDICT_CI_TIMEOUT:-20}" 2>&1)" || true
  CI_FIRST="${CI_OUT%%$'\n'*}"
  CI_TOKEN="${CI_FIRST%% *}"
  CI_SHA="$(printf '%s' "$CI_FIRST" | awk '{print $2}')"
  CI_DETAIL=""
  [[ "$CI_OUT" == *$'\n'* ]] && CI_DETAIL="${CI_OUT#*$'\n'}"
  ci_refuse() {
    {
      echo "post-verdict.sh: REFUSING to post an approval — exact-head CI gate: $1 (#10485)"
      [[ -n "$CI_FIRST" ]] && echo "  reader: ${CI_FIRST:0:500}"
      [[ -n "$CI_DETAIL" ]] && printf '%s\n' "${CI_DETAIL:0:4000}" | sed 's/^/  /'
      echo "$2"
    } >&2
    exit "$3"
  }
  PENDING_NEXT="Nothing was posted. Do NOT add loom:pr: release your claim, leave loom:review-requested, and re-evaluate once CI settles on this head."
  case "$CI_TOKEN" in
    LOOM-CHECKS-GREEN|LOOM-CHECKS-NONE) [[ -n "$CI_SHA" && "$CI_SHA" == "$SHA"* ]] || ci_refuse "checks were read for ${CI_SHA:-an unknown head}, not the reviewed head $SHA" "$PENDING_NEXT" 5 ;;
    LOOM-CHECKS-RED) ci_refuse "a check on $SHA is failing (required or not — an all-CI policy refuses both)" "Nothing was posted. Post changes-requested naming the checks above (loom:ci-failure); if the only failure is an external approval-required workflow, point at the operator rather than the Doctor." 6 ;;
    LOOM-CHECKS-TIMEOUT) ci_refuse "checks on $SHA are pending (or empty while contexts are required)" "$PENDING_NEXT" 5 ;;
    LOOM-CHECKS-HEAD-MOVED) ci_refuse "the PR head moved during inspection" "$PENDING_NEXT" 5 ;;
    LOOM-CHECKS-ERROR) ci_refuse "the checks could not be read" "$PENDING_NEXT" 5 ;;
    *) ci_refuse "no checks sentinel from '${LOOM_DAEMON_BIN:-loom-daemon} forge wait-checks' (missing daemon, or older than 0.19.707?) — an unread CI state is never green. Run ./.loom/scripts/resync-installed.sh / roll loom-daemon" "$PENDING_NEXT" 5 ;;
  esac
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
if [[ -n "$OVERRULE" ]]; then FULL_BODY="$FULL_BODY"$'\n\n---\n\n**Overrules prior verdict (#10581)** — '"$OVERRULE"; fi
if [[ -n "$RECONCILIATION_MARKER" ]]; then
  FULL_BODY="$FULL_BODY

$RECONCILIATION_MARKER"
fi
# Per-call token (#10581): lets verdict-reconcile find THIS caller's comment among
# identical concurrent ones; cleared below when the gate dedupes (nothing posted).
NONCE="$(date +%s%N)-$$-$RANDOM"
FULL_BODY="$FULL_BODY

<!-- loom:verdict-nonce=$NONCE -->
<!-- loom:verdict-sha sha=$SHA verdict=$VERDICT -->"

# Loom writes only to repos it manages (#9548): vet the target, then name it
# explicitly, so gh's preference for an `upstream` remote cannot redirect the
# verdict onto another project's PR with the same number. forge-helpers.sh is
# sourced inside the command substitution because it turns on `set -e`.
# (An approval already vetted it above, before the CI read.)
[[ -n "$REPO" ]] || REPO="$(source "$SCRIPT_DIR/lib/forge-helpers.sh" && loom_write_repo "${LOOM_REPO:-}")" || { echo "post-verdict.sh: not posting the verdict on PR #$PR: loom-daemon forge may-write refused the repo (#9548)" >&2; exit 4; }
# Serialize the per-PR verdict transaction (#10581): the final head compare, gate,
# post and labels run under one host lock (loom-daemon forge verdict-lock), taken
# before the head compare so that compare sits immediately before the write. A
# rival verdict cannot land between this caller's gate read and its writes, and
# two identical callers cannot both pass the dedupe read. Fail closed (exit 9).
# Host-local: independent hosts share no lock, so the final step below
# (verdict-reconcile) re-reads the forge and arbitrates a cross-host race.
#
# Capability probe first (#10581): the verdict verbs are asked for by name on the
# binary that will run them, so the answer never depends on a version number
# (which cannot name the release a PR lands in). A binary without them (scripts
# rolled ahead of the daemon; in the loom repo .loom/scripts symlinks into
# defaults/scripts, so a `git pull` is a script roll, possibly ahead of the daemon
# release) posts either verdict on the legacy path below: the pre-#10581 posting
# plus a label write (for an approval, exactly the one main's Judge prompt makes),
# so a script roll never stalls the fleet's approvals. It takes no lock, so exit 9
# keeps meaning "the lock is held".
MISSING_VERBS=""
for verb in verdict-lock verdict-gate verdict-labels verdict-reconcile; do
  "${LOOM_DAEMON_BIN:-loom-daemon}" forge "$verb" --help >/dev/null 2>&1 || MISSING_VERBS="$MISSING_VERBS forge $verb"
done
if [[ -n "$MISSING_VERBS" ]]; then
  BIN_VERSION="$("${LOOM_DAEMON_BIN:-loom-daemon}" --version 2>/dev/null)" || BIN_VERSION=""
  WHY="the daemon binary $(command -v "${LOOM_DAEMON_BIN:-loom-daemon}" || echo "${LOOM_DAEMON_BIN:-loom-daemon} (not found)") (version: ${BIN_VERSION%%$'\n'*}) lacks${MISSING_VERBS} (#10581)"
  echo "post-verdict.sh: WARNING — $WHY. Posting the $VERDICT verdict on PR #$PR on the legacy path (no verdict gate, lock, cross-host arbitration or exclusive verdict-label gating); roll loom-daemon to a build that includes #10684 to get them." >&2
else
  "${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-lock acquire "$PR" --repo "$REPO" || { echo "post-verdict.sh: could not take the per-PR verdict lock: another verdict transaction on PR #$PR holds it on this host (or the lock directory is unwritable); nothing was posted (#10581). Retry on a later pass." >&2; exit 9; }
  trap '"${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-lock release "$PR" --repo "$REPO"' EXIT
fi
# Final compare (#10485): the head must still be the reviewed one right before
# the write; an unreadable head is a refusal, never a pass.
if [[ "$VERDICT" == "approved" ]]; then
  FINAL_HEAD="$(gh api "repos/$REPO/pulls/$PR" --jq '.head.sha' 2>/dev/null)" || FINAL_HEAD=""
  if [[ -z "$FINAL_HEAD" || "$FINAL_HEAD" != "$SHA"* ]]; then
    echo "post-verdict.sh: REFUSING to post an approval — PR head is ${FINAL_HEAD:-unreadable}, not the reviewed $SHA (moved or unreadable between the CI read and the post, #10485). Nothing was posted; re-review the new head." >&2
    exit 5
  fi
fi
# #9774: through the shared transport (never a bare `gh pr comment`), so the
# verdict posts via the daemon chokepoint when a binary resolves — dashboard
# footer included — and via the gh ladder when it does not.
source "$SCRIPT_DIR/lib/forge-helpers.sh"
# The legacy path (no verdict verbs, see the probe above): post, then one label
# write. An approval's is exactly main's Judge-prompt write (+loom:pr, minus the
# queue/claim labels). It never removes loom:changes-requested: without the gate
# and reconcile, a rival same-head rejection must leave both labels so
# merge-pr.sh's contradiction guard (#8112) refuses the merge (#4560). A
# changes-requested also strips loom:pr (stricter than main, the safe side).
# loom:ci-failure is left as it is; the exact-head CI gate above already refused
# an approval on a red head.
if [[ -n "$MISSING_VERBS" ]]; then
  if [[ "$VERDICT" == "approved" ]]; then
    LEGACY_LABELS=(--add-label loom:pr --remove-label loom:review-requested --remove-label loom:reviewing)
  else
    LEGACY_LABELS=(--add-label loom:changes-requested --remove-label loom:pr --remove-label loom:review-requested --remove-label loom:reviewing)
  fi
  forge_gh_comment_rl_safe "$REPO" "$PR" "$FULL_BODY" 1 || exit 1
  forge_gh_perm_safe pr edit "$PR" --repo "$REPO" "${LEGACY_LABELS[@]}" >/dev/null && exit 0
  echo "post-verdict.sh: the $VERDICT verdict on PR #$PR is posted, but its label transition did not complete. Repair: gh pr edit $PR --repo $REPO ${LEGACY_LABELS[*]}" >&2
  exit 8
fi
# Verdict gate + label transition (#10581): the logic is the daemon's
# (loom_daemon::verdict_gate); only a positive sentinel lets an approval post.
# The verdict verbs (verdict-gate/-labels/-lock/-reconcile, #10581) first ship in
# the build that merges #10684; no version floor is declared for them, because the
# capability probe above checks them on the resolved binary. Roll loom-daemon
# before (or with) these scripts: until then, verdicts take the legacy path.
VG_RC=0; VG_OUT="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-gate "$PR" --repo "$REPO" --verdict "$VERDICT" --sha "$SHA" --overrules-prior "$OVERRULE" 2>&1)" || VG_RC=$?  # set -e is on (forge-helpers.sh)
case "$VG_RC:$VG_OUT" in
  "0:LOOM-VERDICT-GATE PROCEED"*) forge_gh_comment_rl_safe "$REPO" "$PR" "$FULL_BODY" 1 || exit 1 ;;
  "10:LOOM-VERDICT-GATE DEDUPE"*) NONCE=""; echo "post-verdict.sh: not posting a duplicate verdict on PR #$PR (applying its labels only): $VG_OUT" >&2 ;;
  "3:LOOM-VERDICT-GATE REFUSE"*) echo "post-verdict.sh: REFUSING to post the $VERDICT verdict on PR #$PR — nothing was posted: $VG_OUT" >&2; exit 7 ;;
  *) [[ "$VERDICT" == "approved" ]] && { echo "post-verdict.sh: REFUSING to post an approval on PR #$PR: '${LOOM_DAEMON_BIN:-loom-daemon} forge verdict-gate' gave no answer (missing or older daemon?): ${VG_OUT:0:500}. An unrun gate is never a pass; roll loom-daemon / run ./.loom/scripts/resync-installed.sh." >&2; exit 7; }
     echo "post-verdict.sh: WARNING — verdict gate unavailable (${VG_OUT:0:200}); posting the changes-requested verdict anyway" >&2; forge_gh_comment_rl_safe "$REPO" "$PR" "$FULL_BODY" 1 || exit 1 ;;
esac
# Cross-host arbitration (#10581), BEFORE any label moves: the lock above orders
# callers on THIS host only, so two hosts can both pass the gate read before either
# writes. Re-read the forge now that the writes are visible: changes-requested
# beats an approval, and of two identical verdicts the lowest comment id stands
# (the other withdraws its comment). An approval stays non-actionable (no loom:pr)
# until this succeeds, so an unreadable arbitration never leaves one live.
# An approval that lost a cross-host race: post the superseding marker, flip the
# labels to changes-requested, exit 7. Used before AND after the label write.
superseded_approval() {
  forge_gh_comment_rl_safe "$REPO" "$PR" "**Approval superseded (#10581)** — $1. A changes-requested verdict at this same head landed concurrently from another Judge; changes-requested wins. Read it, and re-review after the next push.

<!-- loom:verdict-sha sha=$SHA verdict=changes-requested -->" 1 || true
  "${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-labels "$PR" --repo "$REPO" --verdict changes-requested >/dev/null 2>&1 || echo "post-verdict.sh: could not flip the labels; run: loom-daemon forge verdict-labels $PR --repo $REPO --verdict changes-requested" >&2
  echo "post-verdict.sh: the approval on PR #$PR was SUPERSEDED by a concurrent changes-requested verdict at the same head; it does not stand: $1" >&2
  exit 7
}
SEEN_OPP=0 SEEN_SAME=0; [[ "$VG_OUT" =~ seen-opposite=([0-9]+) ]] && SEEN_OPP="${BASH_REMATCH[1]}"; [[ "$VG_OUT" =~ seen-same-max-id=([0-9]+) ]] && SEEN_SAME="${BASH_REMATCH[1]}"
RC_RC=0; RC_OUT="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-reconcile "$PR" --repo "$REPO" --verdict "$VERDICT" --sha "$SHA" --seen-opposite "$SEEN_OPP" --seen-same-max-id "$SEEN_SAME" --nonce "$NONCE" 2>&1)" || RC_RC=$?
case "$RC_RC:$RC_OUT" in
  "0:LOOM-VERDICT-RECONCILE STABLE"*|"0:LOOM-VERDICT-RECONCILE PREVAILS"*) ;; # PREVAILS: our labels below win over a rival approval's
  "12:LOOM-VERDICT-RECONCILE DUPLICATE"*) echo "post-verdict.sh: an identical $VERDICT verdict landed first on PR #$PR at $SHA; this duplicate comment was withdrawn and no labels were touched (#10581): $RC_OUT" >&2; exit 0 ;;
  "11:LOOM-VERDICT-RECONCILE SUPERSEDED"*) superseded_approval "$RC_OUT" ;;
  *) printf 'post-verdict.sh: %s verdict on PR #%s posted, but the cross-host reconcile was unconfirmed (#10581): %s\n' "$VERDICT" "$PR" "${RC_OUT:0:300}" >&2
     [[ "$VERDICT" == "approved" ]] && { echo "The approval is NOT live (no loom:pr applied). Re-run this command; the gate dedupes the comment and retries the arbitration." >&2; exit 8; } ;; # changes-requested: labels below, the safe side
esac
VL_OUT="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-labels "$PR" --repo "$REPO" --verdict "$VERDICT" 2>&1)" || { printf 'post-verdict.sh: the %s verdict on PR #%s is posted, but its label transition did not complete (#10581):\n%s\nRe-run: loom-daemon forge verdict-labels %s --repo %s --verdict %s\n' "$VERDICT" "$PR" "$VL_OUT" "$PR" "$REPO" "$VERDICT" >&2; exit 8; }
# Post-label re-arbitration (#10581): the reconcile above ran BEFORE the label
# write, and another host can post a changes-requested verdict in between. Re-read
# once the approval's labels are on: a rival that landed since then supersedes it
# (labels flipped back); an unreadable re-read withdraws loom:pr rather than leave
# a possibly-stale approval actionable. Any rival posting after this read applies
# its own labels after ours, so the newest verdict's labels always end up last.
if [[ "$VERDICT" == "approved" ]]; then
  RC2_RC=0; RC2_OUT="$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-reconcile "$PR" --repo "$REPO" --verdict "$VERDICT" --sha "$SHA" --seen-opposite "$SEEN_OPP" --seen-same-max-id "$SEEN_SAME" --nonce "$NONCE" 2>&1)" || RC2_RC=$?
  case "$RC2_RC:$RC2_OUT" in
    "0:LOOM-VERDICT-RECONCILE STABLE"*|"12:LOOM-VERDICT-RECONCILE DUPLICATE"*) ;;
    "11:LOOM-VERDICT-RECONCILE SUPERSEDED"*) superseded_approval "$RC2_OUT" ;;
    *) WITHDRAWN="loom:pr was withdrawn; the approval is NOT live"
       gh api -X DELETE "repos/$REPO/issues/$PR/labels/loom%3Apr" >/dev/null 2>&1 || WITHDRAWN="loom:pr could NOT be withdrawn (the DELETE failed), so the unconfirmed approval may still be actionable. Remove it now: gh pr edit $PR --repo $REPO --remove-label loom:pr"
       echo "post-verdict.sh: the approval on PR #$PR was posted and labelled, but the post-label re-arbitration was unconfirmed (#10581): ${RC2_OUT:0:300}. $WITHDRAWN. Re-run this command." >&2; exit 8 ;;
  esac
fi
