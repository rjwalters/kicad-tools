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
# Usage:
#   post-verdict.sh <pr-number> <approved|changes-requested> <sha> \
#       (--body TEXT | --body-file PATH)
#
#   PATH may be "-" to read the body from stdin.
#
# Output: whatever `gh pr comment` prints on success (the comment URL) —
# unchanged, so a caller parsing that output needs no change.
#
# Exit codes:
#   0 - comment posted
#   1 - the `gh pr comment` call failed
#   2 - invalid arguments (bad PR number, verdict token, or SHA; missing body)
#
# NOTE: GitHub-specific (uses `gh pr comment`), like create-pr.sh /
# merge-pr.sh. On a Gitea forge, post the equivalent comment via that forge's
# own CLI and append the identical marker by hand.

set -uo pipefail

usage() {
  sed -n '2,49p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
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

# --- The marker is appended HERE, never accepted as part of $BODY ----------
# This is the entire point of the script: omission becomes structurally
# impossible instead of a matter of remembering to type it. Format must stay
# byte-identical to verdict-staleness-guard.sh's MARKER_TEST/MARKER_CAPTURE.
FULL_BODY="$BODY

<!-- loom:verdict-sha sha=$SHA verdict=$VERDICT -->"

gh pr comment "$PR" --body "$FULL_BODY"
