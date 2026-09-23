#!/usr/bin/env bash
# create-issue.sh - File a new issue, surviving GraphQL rate-limit exhaustion.
#
# `gh issue create` is GraphQL-backed. GitHub's GraphQL quota (5000/hr, shared
# across every agent + tool) and its REST quota are INDEPENDENT, and in a busy
# fleet the GraphQL pool exhausts while REST sits nearly untouched (observed
# 2026-08-03: core 19/5000 consumed vs graphql 1378/5000). Before #5047, every
# issue-filing role -- Architect, Auditor, Curator (decomposition), Builder
# (decomposition), Doctor, Hermit, Judge -- died outright at that moment, even
# though comments, labels and issue state already had documented REST
# fallbacks (#4856).
#
# This script is the single-sourced replacement for a bare `gh issue create`
# in a role prompt: it tries `gh issue create` first, and on a rate-limit
# rejection (and ONLY on a rate-limit rejection) retries the identical filing
# as one REST POST to `repos/{owner}/{repo}/issues`. Labels ride along in the
# same call on both paths -- never a create-then-label sequence.
#
# Usage:
#   create-issue.sh --title TITLE (--body BODY | --body-file PATH) \
#                   [--label LABEL]... [--repo OWNER/REPO] [--force]
#   create-issue.sh --help
#
# Flags are a subset of `gh issue create`'s, chosen so a role prompt's
# existing invocation can be switched over by changing the command name:
#   --title, -t TITLE     Issue title (required).
#   --body, -b BODY       Issue body as literal text.
#   --body-file, -F PATH  Read the body from PATH ("-" = stdin). Mutually
#                         exclusive with --body.
#   --label, -l LABEL     Label to apply at creation. Repeatable. A single
#                         comma-separated value is also accepted, matching
#                         `gh issue create --label "a,b"`.
#   --repo, -R OWNER/REPO Target repository. Omit for the current repo (the
#                         preferred form -- the REST path then resolves the
#                         repo from the git remote with zero API calls).
#
# Loom-specific flags (no `gh issue create` equivalent):
#   --force, --skip-duplicate-check
#                         File even if the duplicate backstop below finds an
#                         above-threshold open match. Same effect as exporting
#                         LOOM_SKIP_DUPLICATE_CHECK=1.
#   --duplicate-threshold N
#                         Jaccard similarity percentage the backstop blocks at
#                         (default: check-duplicate.sh's own default).
#   --duplicate-warn-threshold N
#                         Jaccard similarity percentage at/above which the
#                         backstop WARNS (stderr) but still files (#8289).
#                         Default 13; must stay below the block threshold or
#                         the warn band is disabled. 0 disables it outright.
#
# Output: the new issue's URL on stdout (identical to `gh issue create`).
#
# Exit codes:
#   0 - Issue created (via either path).
#   1 - Creation failed (message on stderr). A non-rate-limit failure is
#       reported as-is and is NEVER retried over REST.
#   2 - Invalid arguments.
#   3 - NOT FILED: the duplicate backstop matched an open issue at or above
#       the BLOCK threshold (default 18%, see below). Nothing was created; the
#       matches are listed on stderr with the --force re-run.
#  75 - DEFERRED (#6714): the machine-wide issue-filing lock could not be
#       acquired within its bounded wait, so NOTHING WAS FILED. The caller
#       should defer this filing burst to its next tick and try again. This is
#       deliberately fail-SAFE rather than fail-open: filing unserialized is
#       what corrupted five issue bodies on 2026-08-08 (see lib/filing-lock.sh).
#
# Graduated duplicate response (#8289, re-landed by #8360). The backstop used
# to be a cliff: a hard exit 3 at >= 18% similarity and TOTAL SILENCE at 17%,
# with nothing in between -- which is what trains callers to reflexively pass
# --force. Three bands now, all derived from the SAME historical calibration
# data check-duplicate.sh's --threshold default is built on (#4409):
#
#   >= 18%  BLOCK (exit 3, nothing filed). 18 is not an arbitrary low bar: it
#           is the line drawn from this repo's own confirmed duplicate pairs
#           -- #3550/#3551 scored 19% on full bodies (26% on titles alone) --
#           against unrelated richly-worded issues, which scored 4-13%. The
#           19% refusal reported in #8289 was a TRUE positive, so this band
#           stays exactly where it was; the block message now explains the
#           calibration instead of just asserting a percentage. Between 18%
#           and 25% a block additionally requires >= 18% TITLE overlap
#           (#8591): two long issues in the same subsystem clear 18% on body
#           text alone, which is how #8561 was refused as a duplicate of the
#           unrelated #8505 at exactly the floor. An uncorroborated match
#           drops to the WARN band instead of blocking; nothing is hidden.
#           The rule and its constants live in the scan, see
#           loom-daemon/src/cli/duplicate_scan.rs.
#   13-17%  WARN (stderr, then FILE ANYWAY -- what --force used to be needed
#           for). This is the documented ambiguous band: 13 is both the top of
#           the unrelated range AND the score of a second CONFIRMED duplicate
#           pair, so a score here is genuinely undecidable. Blocking on it
#           would be wrong; saying nothing (the old behaviour) hid it.
#   < 13%   silent, as before -- unrelated-issue territory.
#
# Both flags below move the bands; --force/LOOM_SKIP_DUPLICATE_CHECK=1 skip
# them entirely and remain available no matter which band a match lands in.
# The scoring itself is `loom-daemon duplicate-scan` since #8360, reached
# through check-duplicate.sh.
#
# Duplicate backstop (#7971): before filing, this runs the sibling
# `check-duplicate.sh` against OPEN issues and exits 3 without filing when it
# reports an above-threshold match. Prompt-level dedup instructions only reach
# the roles someone remembered to write them into -- on 2026-09-16 three
# Builders filed the SAME bug (#7957/#7960/#7968) inside four minutes because
# Builder/Doctor/Judge, the roles that file issues as a SIDE EFFECT of other
# work, had no dedup step at any layer. A backstop here covers every caller,
# including roles nobody updates.
#
# It is deliberately conservative -- it fails OPEN on everything inconclusive,
# so it can never become a new way for a filing to die:
#   * check-duplicate.sh missing, non-executable, erroring, or rate-limited
#     into a partial answer (exit 2) -> warn on stderr, file anyway. The
#     backstop never introduces a GraphQL dependency the REST fallback path
#     (#5047) did not already have: check-duplicate.sh has its own REST
#     fallback, and a total failure of it does not block the create.
#   * NON_DISCRIMINATIVE (#4409, the scorer self-reporting that it isn't
#     separating anything for this query) -> warn, file anyway.
#   * A match in the WARN band only (#8289) -> warn, file anyway. Never a
#     block: the caller is told what it nearly collided with, not stopped.
#   * A match the filing ALREADY cross-references by number ("Part of #123",
#     "Parent: #123", "split out of #123") -> not a duplicate. An intentional
#     follow-up naturally scores high against the work that spawned it; that
#     is the decomposition path, not a duplicate, and it is never blocked.
#   * --repo given -> skipped entirely (check-duplicate.sh searches the
#     working directory's repo, so it cannot answer for another one).
#
# Serialization (#6714): every create goes through the machine-wide filing
# lock, so two issue-creating agents -- in the SAME repo or in DIFFERENT ones,
# which is the case #3707's documentation-only mitigation missed -- can never
# interleave their `gh issue create` calls. See `lib/filing-lock.sh` for the
# protocol, the tiers (host = hard, fleet = soft advisory), and the knobs.
#
# NOTE: this is GitHub-specific, like the other `*_rl_safe` helpers. On a
# Gitea forge it exits 2 with a pointer to `gh`/`tea` -- Gitea does not have
# GitHub's split GraphQL/REST quota, so it has no equivalent failure mode.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./lib/forge-helpers.sh
source "$SCRIPT_DIR/lib/forge-helpers.sh"
# shellcheck source=./lib/filing-lock.sh
source "$SCRIPT_DIR/lib/filing-lock.sh"

usage() {
  # Line range = the whole leading comment block (keep in sync when the header
  # grows; `--help` silently truncating its own docs is its own small #8289).
  sed -n '2,135p' "${BASH_SOURCE[0]:-$0}" | sed 's/^# \{0,1\}//'
}

TITLE=""
BODY=""
BODY_FILE=""
REPO_NWO=""
LABELS=()
HAVE_BODY=false
# Duplicate backstop (#7971). Env default so a caller that has ALREADY run
# check-duplicate.sh for a whole burst (Architect, Hermit, Auditor, Curator,
# Guide all do) can export LOOM_SKIP_DUPLICATE_CHECK=1 once instead of
# re-paying for the same search on every filing.
SKIP_DUP_CHECK=false
case "${LOOM_SKIP_DUPLICATE_CHECK:-}" in 1 | true | TRUE | yes) SKIP_DUP_CHECK=true ;; esac
DUP_THRESHOLD=""
DUP_WARN_THRESHOLD=""
# Band defaults (#8289). Both come from the #4409 calibration documented in
# check-duplicate.sh's --threshold/--warn-threshold help: 18 = where this
# repo's confirmed duplicate pairs scored; 13 = the top of the observed
# unrelated range AND a second confirmed pair's score, i.e. the floor of the
# band where the two populations overlap and nothing can be decided.
DUP_BLOCK_DEFAULT=18
DUP_WARN_DEFAULT=13
# Width preserved when --duplicate-threshold moves the block line without an
# explicit --duplicate-warn-threshold: 18-13.
DUP_BAND_WIDTH=5

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help | -h)
      usage
      exit 0
      ;;
    --title | -t)
      TITLE="${2:-}"
      shift 2
      ;;
    --body | -b)
      BODY="${2:-}"
      HAVE_BODY=true
      shift 2
      ;;
    --body-file | -F)
      BODY_FILE="${2:-}"
      shift 2
      ;;
    --label | -l)
      # `gh issue create --label "a,b"` splits on commas; match that so a
      # prompt's existing invocation transfers unchanged.
      IFS=',' read -r -a _split <<< "${2:-}"
      for _l in "${_split[@]}"; do
        # Trim surrounding whitespace from "a, b".
        _l="${_l#"${_l%%[![:space:]]*}"}"
        _l="${_l%"${_l##*[![:space:]]}"}"
        [[ -n "$_l" ]] && LABELS+=("$_l")
      done
      shift 2
      ;;
    --repo | -R)
      REPO_NWO="${2:-}"
      shift 2
      ;;
    --force | --skip-duplicate-check)
      SKIP_DUP_CHECK=true
      shift
      ;;
    --duplicate-threshold)
      DUP_THRESHOLD="${2:-}"
      if [[ ! "$DUP_THRESHOLD" =~ ^[0-9]+$ ]]; then
        echo "create-issue.sh: --duplicate-threshold takes a number: $DUP_THRESHOLD" >&2
        exit 2
      fi
      shift 2
      ;;
    --duplicate-warn-threshold)
      DUP_WARN_THRESHOLD="${2:-}"
      if [[ ! "$DUP_WARN_THRESHOLD" =~ ^[0-9]+$ ]]; then
        echo "create-issue.sh: --duplicate-warn-threshold takes a number: $DUP_WARN_THRESHOLD" >&2
        exit 2
      fi
      shift 2
      ;;
    *)
      echo "create-issue.sh: unknown argument: $1" >&2
      echo "Run 'create-issue.sh --help' for usage." >&2
      exit 2
      ;;
  esac
done

if [[ -z "$TITLE" ]]; then
  echo "create-issue.sh: --title is required" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]] && [[ "$HAVE_BODY" == "true" ]]; then
  echo "create-issue.sh: --body and --body-file are mutually exclusive" >&2
  exit 2
fi

if [[ -n "$BODY_FILE" ]]; then
  if [[ "$BODY_FILE" == "-" ]]; then
    BODY="$(cat)"
  elif [[ -r "$BODY_FILE" ]]; then
    BODY="$(cat "$BODY_FILE")"
  else
    echo "create-issue.sh: cannot read --body-file: $BODY_FILE" >&2
    exit 2
  fi
fi

forge_detect
if [[ "$FORGE_TYPE" != "github" ]]; then
  echo "create-issue.sh: this GraphQL-exhaustion fallback is GitHub-specific; \
on $FORGE_TYPE file the issue with your forge's own CLI (Gitea has no split \
GraphQL/REST quota, so it has no equivalent failure mode)." >&2
  exit 2
fi

# Detection backstop (#6771, deferred from #6714's filing-lock): warn -- never
# block -- when the body cites several repo-local identifiers and NONE of them
# resolve in the repo being filed into. This is the independent check that
# would catch a cross-repo body mismatch if the lock (#6714) ever has a gap;
# it must never change this script's exit status. Only runs when filing into
# the current working directory's repo (no --repo override) -- with an
# explicit --repo, the target repo's checkout is not necessarily local, so
# there is nothing to check against. Runs before the filing lock below so the
# lock's own held duration stays as short as possible (see its comment).
if [[ -z "$REPO_NWO" ]]; then
  _bra_script="$SCRIPT_DIR/lib/body-repo-affinity.sh"
  if [[ -x "$_bra_script" ]]; then
    "$_bra_script" --body "$BODY" --repo-root "$(pwd)" || true
  fi
fi

# --- #7971: duplicate backstop ----------------------------------------------
# Full rationale and the fail-open contract are in the header. Runs BEFORE the
# filing lock below for the same reason body-repo-affinity does: the lock's
# held duration must cover the create and nothing else. The residual race this
# leaves (two agents checking concurrently, both seeing nothing) is bounded by
# the lock hold -- about a second -- while the duplication window this actually
# closes is the minutes-to-hours one an open issue is visible for.
dup_check_skipped_reason() {
  if [[ "$SKIP_DUP_CHECK" == "true" ]]; then
    echo "forced"
  elif [[ -n "$REPO_NWO" ]]; then
    echo "cross-repo filing (--repo $REPO_NWO): check-duplicate.sh searches the working directory's repo"
  elif [[ ! -x "$SCRIPT_DIR/check-duplicate.sh" ]]; then
    echo "check-duplicate.sh not executable at $SCRIPT_DIR"
  fi
}

# Issue numbers the filing itself cross-references ("Part of #123"). A match
# against one of these is an INTENTIONAL follow-up, not a duplicate.
referenced_issue_numbers() {
  printf '%s\n%s\n' "$TITLE" "$BODY" | grep -oE '#[0-9]+' | tr -d '#' | sort -u || true
}

if [[ -z "$(dup_check_skipped_reason)" ]]; then
  # Resolve the two band edges (#8289). --duplicate-warn-threshold wins; else
  # an explicit --duplicate-threshold carries the band down with it, keeping
  # its width; else the calibrated defaults. A warn floor at/above the block
  # line, or 0, means "no warn band" -- check-duplicate.sh is then invoked
  # exactly as it was before this feature existed.
  _dup_block="${DUP_THRESHOLD:-$DUP_BLOCK_DEFAULT}"
  if [[ -n "$DUP_WARN_THRESHOLD" ]]; then
    _dup_warn="$DUP_WARN_THRESHOLD"
  elif [[ -n "$DUP_THRESHOLD" ]]; then
    _dup_warn=$((_dup_block - DUP_BAND_WIDTH))
    [[ "$_dup_warn" -lt 1 ]] && _dup_warn=0
  else
    _dup_warn="$DUP_WARN_DEFAULT"
  fi
  [[ "$_dup_warn" -ge "$_dup_block" ]] && _dup_warn=0

  _dup_args=(--title "$TITLE")
  [[ -n "$BODY" ]] && _dup_args+=(--body "$BODY")
  [[ -n "$DUP_THRESHOLD" ]] && _dup_args+=(--threshold "$DUP_THRESHOLD")
  [[ "$_dup_warn" -gt 0 ]] && _dup_args+=(--warn-threshold "$_dup_warn")

  # check-duplicate.sh's stderr is CAPTURED, not discarded: on the fail-open
  # path below it is the only thing that says WHY the check could not answer
  # (e.g. "GraphQL rate-limited and REST fallback also failed"). Discarding it
  # was how a degraded filing looked identical to a clean one (#8289).
  _dup_err_file="$(mktemp)"
  _dup_rc=0
  _dup_out="$("$SCRIPT_DIR/check-duplicate.sh" "${_dup_args[@]}" 2>"$_dup_err_file")" || _dup_rc=$?
  _dup_err="$(cat "$_dup_err_file" 2>/dev/null || true)"
  rm -f "$_dup_err_file"

  # rc 0 is parsed too, not just rc 1: a WARN-band match does not (and must
  # not) move check-duplicate.sh's exit code, so the rows are the only signal.
  if [[ "$_dup_rc" -eq 0 || "$_dup_rc" -eq 1 ]]; then
    _dup_refs=" $(referenced_issue_numbers | tr '\n' ' ') "
    _dup_hits=""
    _dup_near=""
    _dup_rows=0
    while IFS= read -r _dup_line; do
      # Only "#N: <title> (similarity: X%)" rows are blocking matches, and
      # "NEAR #N: ..." rows are warn-band context; DUPLICATE_FOUND,
      # NEAR_DUPLICATE, NON_DISCRIMINATIVE, SEARCH_INCOMPLETE and
      # REST_FALLBACK are markers. An intentional follow-up is exempt in BOTH
      # bands -- the same cross-reference logic, applied twice.
      if [[ "$_dup_line" =~ ^#([0-9]+): ]]; then
        _dup_rows=$((_dup_rows + 1))
        case "$_dup_refs" in
          *" ${BASH_REMATCH[1]} "*) continue ;;
        esac
        _dup_hits+="  $_dup_line"$'\n'
      elif [[ "$_dup_line" =~ ^NEAR\ #([0-9]+): ]]; then
        case "$_dup_refs" in
          *" ${BASH_REMATCH[1]} "*) continue ;;
        esac
        _dup_near+="  ${_dup_line#NEAR }"$'\n'
      fi
    done <<< "$_dup_out"

    if [[ -n "$_dup_hits" ]]; then
      {
        echo "create-issue.sh: NOT FILED -- this looks like a duplicate of open work:"
        printf '%s' "$_dup_hits"
        echo "Those scored at or above the ${_dup_block}% BLOCK threshold -- and, below 25%,"
        echo "their TITLES agree too (#8591). #3550/#3551, this repo's confirmed duplicate"
        echo "pair, scores 19% on bodies and 26% on titles; unrelated pairs miss one or both."
        if [[ -n "$_dup_near" ]]; then
          echo "Also seen, as low-confidence context (NOT a reason for this block):"
          printf '%s' "$_dup_near"
        fi
        echo "Nothing was created. Either:"
        echo "  * comment on the issue above instead of filing a new one, or"
        echo "  * re-run with --force if this is genuinely distinct work."
        echo "(Export LOOM_SKIP_DUPLICATE_CHECK=1 to skip this check for a whole filing burst.)"
      } >&2
      exit 3
    fi

    # WARN band: announce and file. Deliberately NOT a block -- see the header.
    if [[ -n "$_dup_near" ]]; then
      {
        echo "create-issue.sh: WARNING -- similar open work that did not meet the block bar (FILING ANYWAY):"
        printf '%s' "$_dup_near"
        echo "  Either it scored in the undecidable ${_dup_warn}-$((_dup_block - 1))% band (unrelated issues reach 13%,"
        echo "  as did one confirmed duplicate pair), or it cleared ${_dup_block}% on body text alone"
        echo "  without title agreement (#8591) -- each row says which. Close it as a dup if it is one."
      } >&2
    fi

    if [[ "$_dup_rc" -eq 1 ]]; then
      if [[ "$_dup_rows" -gt 0 ]]; then
        echo "create-issue.sh: every similar issue is already cross-referenced by this filing (intentional follow-up, not a duplicate) -- filing." >&2
      else
        echo "create-issue.sh: duplicate check returned no discriminating match -- filing." >&2
      fi
    fi
  else
    # Fail OPEN: a broken/rate-limited duplicate check must never be the reason
    # an issue does not get filed -- but say what broke, don't just shrug.
    echo "create-issue.sh: duplicate check unavailable (check-duplicate.sh exit $_dup_rc) -- filing." >&2
    if [[ -n "$_dup_err" ]]; then
      echo "create-issue.sh:   check-duplicate.sh said: ${_dup_err//$'\n'/ }" >&2
    fi
  fi
fi

# --- #6714: serialize the actual filing ------------------------------------
# The lock is taken as late as possible (after every argument/forge validation
# above) and held for exactly the one create below, so a burst's serialization
# window is as short as it can be while still being airtight. A role that wants
# to hold it across an ENTIRE burst can source lib/filing-lock.sh itself and
# acquire once — the lock is re-entrant, so the per-call acquire here becomes a
# no-op inside that hold.
_filing_lock_rc=0
loom_filing_lock_acquire "${LOOM_FILING_LOCK_LABEL:-create-issue}" || _filing_lock_rc=$?
if [[ "$_filing_lock_rc" -eq "$LOOM_FILING_LOCK_DEFER_RC" ]]; then
  # Fail-SAFE: nothing was filed. The caller retries on its next tick.
  exit "$LOOM_FILING_LOCK_DEFER_RC"
fi
# Release on every exit path, including a failed create or an interrupt.
trap 'loom_filing_lock_release' EXIT INT TERM

if ! forge_gh_create_issue_rl_safe "$REPO_NWO" "$TITLE" "$BODY" "${LABELS[@]+"${LABELS[@]}"}"; then
  exit 1
fi
