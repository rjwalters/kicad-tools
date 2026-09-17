#!/usr/bin/env bash
# judge-fallback-guard.sh — deterministic gate for the Judge's fallback queue
# (defaults/.claude/commands/loom/judge.md §"Fallback Queue (When No Labeled
# Work)"), issue #5455.
#
# Problem this closes: PR #4972 (rjwalters/loom) accumulated 199 fallback-mode
# Judge comments over 37 hours before merging — a 2-line Dependabot Cargo.lock
# bump. The existing SHA-based dedup (#5058) mostly works, but it is
# prompt-embedded bash that an LLM must faithfully re-derive on every pass; a
# fleet propagation-lag window (some hosts still running the pre-#5058 prompt)
# let ~21h of comments through even after the fix had merged. This script
# moves the decision itself out of prose into a real, testable subprocess so
# correctness stops depending on the model re-deriving multi-step bash from a
# role prompt each time (deferred, lower-priority AC of #5455).
#
# Decision, in priority order (first match wins):
#   1. BOT AUTHOR — `gh pr view --json author` -> `.author.is_bot == true`
#      (Dependabot, Renovate, github-actions[bot], etc). Such a PR is outside
#      the Loom label workflow by construction: the fallback path deliberately
#      never applies labels to it, so it can never leave the fallback query's
#      result set through any Loom-side action. Skip permanently, independent
#      of the cap or velocity below. (#5455 AC "structurally exclude PRs the
#      fallback queue can never advance")
#   2. LIFETIME CAP — total `loom:fallback-evaluated` marker comments on the
#      PR, counted across ALL head SHAs over the PR's full history (not just
#      the current SHA), has reached --cap. See "Why the cap is per-PR-
#      lifetime, not per-SHA" below. (#5455 AC "hard per-PR cap ... independent
#      of marker/SHA state")
#   3. SHA DEDUP — the existing #5058 mechanism: the most recent marker's SHA
#      already equals the PR's current head SHA, i.e. nothing has changed
#      since the last evaluation.
#   4. Otherwise: OK to evaluate.
#
# Independent of the decision above: VELOCITY ALERT. If the marker-comment
# count within the trailing --velocity-window-hours meets or exceeds
# --velocity-threshold, this script also emits VELOCITY_ALERT=1 — a backstop
# early-warning signal so a future regression in the cap/dedup logic itself is
# loud rather than discovered 199 comments later. It fires independently of
# DECISION (a PR can be both SKIPPED by the cap AND have tripped the velocity
# alert on the same run) — the cap bounds the damage, the alert tells a human
# the bound almost wasn't enough. (#5455 AC "emit a signal when comment
# velocity crosses a threshold")
#
# Why the cap is per-PR-lifetime, not per-SHA:
#   #4972's 199 comments accumulated on a SINGLE fixed head SHA
#   (7e74f3134d51576274e302664f05151f37ce1aca) over 37 hours — so a per-SHA
#   cap alone would have bounded that specific incident. But a per-SHA-only
#   cap does not bound a PR that repeatedly force-pushes trivial commits
#   (each one resetting the head SHA and therefore any per-SHA counter back to
#   zero) — an adversarial or merely noisy PR could force-push its way past a
#   per-SHA cap indefinitely. Counting markers across the PR's ENTIRE comment
#   history (this script does not filter by SHA when computing MARKER_COUNT)
#   closes that gap: the lifetime total only ever goes up, regardless of how
#   many times the head SHA changes. The SHA-based dedup (decision 3 above)
#   still runs on top of the lifetime cap for its original purpose — skipping
#   re-evaluation of an unchanged PR between Judge ticks — but it no longer
#   has to be the ONLY bound.
#
# Usage:
#   judge-fallback-guard.sh <pr-number>
#       [--cap N]                    lifetime marker-comment cap (default 20)
#       [--velocity-threshold N]     alert threshold (default 8)
#       [--velocity-window-hours H]  alert trailing window, hours (default 4)
#
# Output (stdout — one KEY=VALUE per line, machine-parseable):
#   DECISION=EVALUATE|SKIP
#   REASON=<short human-readable reason>
#   HEAD_SHA=<current head sha>
#   MARKER_COUNT=<lifetime loom:fallback-evaluated marker count>
#   VELOCITY_ALERT=0|1
#   VELOCITY_COUNT=<marker count within the trailing velocity window>
#
# Exit codes:
#   0  = DECISION=EVALUATE (proceed with fallback-mode review)
#   10 = SKIP: bot author
#   11 = SKIP: lifetime cap reached
#   12 = SKIP: SHA dedup (already evaluated at the current head SHA)
#   1  = usage or environment error (bad args, `gh` call failed) — the caller
#        should treat this the same as any other fallback-queue `gh` failure,
#        NOT as "no work available".
#
# This script decides whether ONE given PR number should be evaluated. Finding
# the candidate list of unlabeled open PRs itself (the `gh pr list ... | select
# no loom: label` query) stays in judge.md — this is deliberately a single-PR
# gate, called once per candidate in the fallback walk.

set -euo pipefail

CAP=20
VELOCITY_THRESHOLD=8
VELOCITY_WINDOW_HOURS=4
PR=""

usage() {
  echo "Usage: $0 <pr-number> [--cap N] [--velocity-threshold N] [--velocity-window-hours H]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cap) CAP="${2:?--cap requires a value}"; shift 2 ;;
    --cap=*) CAP="${1#*=}"; shift ;;
    --velocity-threshold) VELOCITY_THRESHOLD="${2:?--velocity-threshold requires a value}"; shift 2 ;;
    --velocity-threshold=*) VELOCITY_THRESHOLD="${1#*=}"; shift ;;
    --velocity-window-hours) VELOCITY_WINDOW_HOURS="${2:?--velocity-window-hours requires a value}"; shift 2 ;;
    --velocity-window-hours=*) VELOCITY_WINDOW_HOURS="${1#*=}"; shift ;;
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
if ! [[ "$CAP" =~ ^[0-9]+$ ]] || ! [[ "$VELOCITY_THRESHOLD" =~ ^[0-9]+$ ]] || ! [[ "$VELOCITY_WINDOW_HOURS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --cap / --velocity-threshold / --velocity-window-hours must all be non-negative integers" >&2
  exit 1
fi

for bin in gh jq; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: '$bin' not found on PATH" >&2; exit 1; }
done

emit() {
  local decision="$1" reason="$2" head_sha="$3" marker_count="$4" velocity_alert="$5" velocity_count="$6"
  echo "DECISION=$decision"
  echo "REASON=$reason"
  echo "HEAD_SHA=$head_sha"
  echo "MARKER_COUNT=$marker_count"
  echo "VELOCITY_ALERT=$velocity_alert"
  echo "VELOCITY_COUNT=$velocity_count"
}

# Scratch file for each `gh` call's stderr. We deliberately keep `gh`'s stdout
# (the JSON we parse) and stderr SEPARATE — `gh` writes incidental content to
# stderr even on a successful exit (update-notifier banners, rate-limit hints,
# proxy/TLS warnings), and merging that into stdout with `2>&1` would corrupt
# the JSON blob before `jq` sees it. On the success path we parse only stdout;
# stderr is surfaced only when the call actually fails (non-zero exit). See
# #5455 (Judge review): merging the streams silently zeroed MARKER_COUNT and
# defeated the lifetime cap whenever `gh` emitted any stderr chatter.
GH_STDERR="$(mktemp)"
trap 'rm -f "$GH_STDERR" 2>/dev/null || true' EXIT

# --- Step 1: bot-author check + current head SHA ---------------------------
PR_JSON="$(gh pr view "$PR" --json author,headRefOid 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh pr view $PR --json author,headRefOid' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}

IS_BOT="$(jq -r '.author.is_bot // false' <<<"$PR_JSON" 2>/dev/null || echo "false")"
AUTHOR_LOGIN="$(jq -r '.author.login // empty' <<<"$PR_JSON" 2>/dev/null || true)"
HEAD_SHA="$(jq -r '.headRefOid // empty' <<<"$PR_JSON" 2>/dev/null || true)"

if [[ -z "$HEAD_SHA" ]]; then
  echo "ERROR: could not resolve head SHA for PR #$PR from: $PR_JSON" >&2
  exit 1
fi

# Loom's own GitHub App dispatch identity is reported by GitHub as
# `is_bot: true` (same as Dependabot/Renovate/github-actions[bot]), but it is
# NOT an external bot outside the Loom label workflow — it's Loom's own PR
# creation path. Allowlist it by exact `.author.login` match so it proceeds
# to the cap/dedup checks below like any other Loom-authored PR, instead of
# being permanently invisible to both the primary and fallback Judge queues
# (#6982). This narrows the bot-author check; it does not weaken it for
# genuinely external bots.
if [[ "$IS_BOT" == "true" && "$AUTHOR_LOGIN" != "app/loom-fleet-dispatch" ]]; then
  emit "SKIP" "bot-author (outside Loom label workflow; fallback queue cannot advance it)" "$HEAD_SHA" 0 0 0
  exit 10
fi

# --- Step 2: fetch every fallback-evaluated marker across the PR's full
#     comment history. --paginate is REQUIRED: without it `gh api` returns
#     only the first page (default per_page=30, oldest-first), so a marker
#     posted late in a long comment history (#4972 already had 129 comments
#     when the #5058 dedup shipped) would never be seen — the same pitfall
#     documented in judge.md's own fallback-queue example workflow.
COMMENTS_JSON="$(gh api "repos/{owner}/{repo}/issues/$PR/comments" --paginate 2>"$GH_STDERR")" || {
  echo "ERROR: 'gh api .../issues/$PR/comments --paginate' failed: $(cat "$GH_STDERR" 2>/dev/null)" >&2
  exit 1
}

# One "<ISO-8601 created_at>\t<sha>" line per marker comment, oldest first
# (matches --paginate's page order). `test(...)` guards the `capture(...)`
# call so a non-matching comment body is filtered out via `select` rather
# than raising a per-item jq error.
MARKER_LINES="$(jq -r '
  .[]
  | select(.body != null and (.body | test("<!-- loom:fallback-evaluated sha=[0-9a-f]+ -->")))
  | [.created_at, (.body | capture("<!-- loom:fallback-evaluated sha=(?<sha>[0-9a-f]+) -->").sha)]
  | @tsv
' <<<"$COMMENTS_JSON" 2>/dev/null || true)"

MARKER_COUNT=0
if [[ -n "$MARKER_LINES" ]]; then
  MARKER_COUNT="$(wc -l <<<"$MARKER_LINES" | tr -d ' ')"
fi

# --- Step 3: velocity check (independent of the decision below) ------------
# Portable ISO-8601 -> epoch-seconds: GNU `date -d` first, BSD/macOS `date -j
# -f` fallback (matches the existing dual-path idiom in
# archive-transcripts.sh's epoch_to_date()/iso_now()).
iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" +%s 2>/dev/null || echo ""
}

NOW_EPOCH="$(date -u +%s)"
WINDOW_SECONDS=$(( VELOCITY_WINDOW_HOURS * 3600 ))
VELOCITY_COUNT=0
if [[ -n "$MARKER_LINES" ]]; then
  while IFS=$'\t' read -r created_at _sha; do
    [[ -z "$created_at" ]] && continue
    ts="$(iso_to_epoch "$created_at")"
    [[ -z "$ts" ]] && continue
    if (( NOW_EPOCH - ts <= WINDOW_SECONDS )); then
      VELOCITY_COUNT=$(( VELOCITY_COUNT + 1 ))
    fi
  done <<<"$MARKER_LINES"
fi

VELOCITY_ALERT=0
if (( VELOCITY_COUNT >= VELOCITY_THRESHOLD )); then
  VELOCITY_ALERT=1
fi

# --- Step 4: lifetime cap ---------------------------------------------------
if (( MARKER_COUNT >= CAP )); then
  emit "SKIP" "lifetime fallback-evaluation cap reached ($MARKER_COUNT >= $CAP markers across all head SHAs)" \
    "$HEAD_SHA" "$MARKER_COUNT" "$VELOCITY_ALERT" "$VELOCITY_COUNT"
  exit 11
fi

# --- Step 5: SHA dedup (#5058) — most recent marker, if any -----------------
LAST_MARKER_SHA=""
if [[ -n "$MARKER_LINES" ]]; then
  LAST_MARKER_SHA="$(tail -n 1 <<<"$MARKER_LINES" | cut -f2)"
fi

if [[ -n "$LAST_MARKER_SHA" && "$LAST_MARKER_SHA" == "$HEAD_SHA" ]]; then
  emit "SKIP" "already evaluated in fallback mode at current head SHA (no new commits)" \
    "$HEAD_SHA" "$MARKER_COUNT" "$VELOCITY_ALERT" "$VELOCITY_COUNT"
  exit 12
fi

# --- Otherwise: proceed ------------------------------------------------------
emit "EVALUATE" "no skip condition matched" "$HEAD_SHA" "$MARKER_COUNT" "$VELOCITY_ALERT" "$VELOCITY_COUNT"
exit 0
