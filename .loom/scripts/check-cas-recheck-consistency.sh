#!/usr/bin/env bash
# check-cas-recheck-consistency.sh - Regression guard for the verdict-time
# CAS recheck + verdict-label mutual-exclusion janitor (#4570).
#
# Why: #4570 closed a real incident (PR #4560, 2026-07-30) where two
# concurrent Judges produced a contradictory `loom:pr` + `loom:changes-requested`
# label state on the same PR. The fix is prompt/doc-only (Option A — these are
# role prompts, not compiled code), so there is no compiler to catch a
# regression if a future edit strips the recheck sections back out, or if the
# Champion janitor / criterion-1 fix gets silently reverted. This script is
# the executable tie that would fail CI if any of that guidance regresses.
#
# What it checks:
#   1. defaults/.claude/commands/loom/judge.md contains the Verdict-Time CAS
#      Recheck section, and its symlinked defaults/roles/judge.md resolves to
#      the same content (single-source-of-truth invariant, not a copy-drift
#      check — see #1185).
#   2. Same for doctor.md.
#   3. defaults/.claude/commands/loom/champion-pr-merge.md contains the
#      Verdict-State Janitor section AND criterion 1's verification snippet
#      actually checks for loom:changes-requested (not just loom:pr presence).
#   4. .github/labels.yml and defaults/.github/labels.yml both document the
#      verdict-label mutual-exclusion invariant (byte-identity between the
#      two files is separately enforced by check-labels-drift.sh).
#   5. (#5686) The SHA-scoped verdict lifetime survives: the guard script
#      exists, judge.md still carries the Verdict SHA Marker + Stale-Verdict
#      Sweep sections, and doctor.md/champion-pr-merge.md still call the
#      guard. A verdict with no marker is UNVERIFIABLE and fails safe (kept,
#      never cleared), so a regression here would silently restore pre-#5686
#      behavior with nothing else failing.
#   5b. (#6382) judge.md's canonical Label Workflow approve/request-changes
#      commands still route through post-verdict.sh (which exists and is
#      executable) instead of hand-typing the `<!-- loom:verdict-sha ... -->`
#      marker as prose — the marker was measured dropped on roughly one
#      verdict in four before this script existed (#6319), a compliance rate
#      no amount of prose fixed. This does NOT re-check the marker's exact
#      format — that is pinned by test-post-verdict.sh's own regex
#      cross-check against verdict-staleness-guard.sh, so it stays
#      single-sourced there rather than duplicated here.
#
# Usage:
#   check-cas-recheck-consistency.sh [ROOT]
#     ROOT  Repository root. Defaults to `git rev-parse --show-toplevel`, then
#           the script's own repo root. If <ROOT>/defaults does not exist
#           (e.g. an installed downstream repo with no source tree), the
#           check is a clean no-op.
#
# Exit codes: 0 = all checks pass (or nothing to check); 1 = a regression was
# detected (details printed to stderr).

set -euo pipefail

# --- Resolve ROOT -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -ge 1 && -n "${1:-}" ]]; then
  ROOT="$1"
else
  if ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    :
  else
    # defaults/scripts/ -> defaults/ -> repo root
    ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  fi
fi

if [[ ! -d "$ROOT/defaults" ]]; then
  echo "check-cas-recheck-consistency: no defaults/ under $ROOT — nothing to check (ok)."
  exit 0
fi

FAIL=0

fail() {
  echo "check-cas-recheck-consistency: FAIL — $1" >&2
  FAIL=1
}

# --- 1 & 2: Judge / Doctor Verdict-Time CAS Recheck section present ---------
JUDGE_MD="$ROOT/defaults/.claude/commands/loom/judge.md"
JUDGE_SYMLINK="$ROOT/defaults/roles/judge.md"
DOCTOR_MD="$ROOT/defaults/.claude/commands/loom/doctor.md"
DOCTOR_SYMLINK="$ROOT/defaults/roles/doctor.md"
CHAMPION_MERGE_MD="$ROOT/defaults/.claude/commands/loom/champion-pr-merge.md"
LABELS_ROOT="$ROOT/.github/labels.yml"
LABELS_DEFAULTS="$ROOT/defaults/.github/labels.yml"

for f in "$JUDGE_MD" "$DOCTOR_MD" "$CHAMPION_MERGE_MD" "$LABELS_ROOT" "$LABELS_DEFAULTS"; do
  if [[ ! -f "$f" ]]; then
    fail "missing expected file: ${f#"$ROOT"/}"
  fi
done

if [[ "$FAIL" -eq 1 ]]; then
  exit 1
fi

# defaults/roles/<role>.md is a symlink into defaults/.claude/commands/loom/
# (single source of truth since #1185) — assert that invariant still holds
# rather than requiring a second, independently-maintained copy.
if [[ ! -L "$JUDGE_SYMLINK" ]]; then
  fail "$JUDGE_SYMLINK is no longer a symlink — if judge.md was de-consolidated into two independent copies, this script (and CLAUDE.md's role-file guidance) needs updating"
elif [[ "$(readlink -f "$JUDGE_SYMLINK")" != "$(readlink -f "$JUDGE_MD")" ]]; then
  fail "$JUDGE_SYMLINK does not resolve to $JUDGE_MD"
fi

if [[ ! -L "$DOCTOR_SYMLINK" ]]; then
  fail "$DOCTOR_SYMLINK is no longer a symlink — see the judge.md note above"
elif [[ "$(readlink -f "$DOCTOR_SYMLINK")" != "$(readlink -f "$DOCTOR_MD")" ]]; then
  fail "$DOCTOR_SYMLINK does not resolve to $DOCTOR_MD"
fi

if ! grep -q "### Verdict-Time CAS Recheck" "$JUDGE_MD"; then
  fail "judge.md is missing the 'Verdict-Time CAS Recheck' section (#4570)"
fi

if ! grep -q "### Verdict-Time CAS Recheck" "$DOCTOR_MD"; then
  fail "doctor.md is missing the 'Verdict-Time CAS Recheck' section (#4570)"
fi

# --- 3: Champion Verdict-State Janitor + criterion-1 fix --------------------
if ! grep -q "## Verdict-State Janitor" "$CHAMPION_MERGE_MD"; then
  fail "champion-pr-merge.md is missing the 'Verdict-State Janitor' section (#4570)"
fi

# Criterion 1's verification snippet must actually check for
# loom:changes-requested, not just loom:pr presence (the doc-vs-check
# contradiction #4570 fixed). Look for the grep check within the file.
if ! grep -q 'grep -q "loom:changes-requested"' "$CHAMPION_MERGE_MD"; then
  fail "champion-pr-merge.md criterion 1 no longer checks for loom:changes-requested alongside loom:pr — the doc-vs-check contradiction (#4570) may have regressed"
fi

# --- 4: labels.yml mutual-exclusion invariant documented --------------------
if ! grep -q "verdict-label mutual exclusion" "$LABELS_ROOT"; then
  fail ".github/labels.yml is missing the verdict-label mutual-exclusion invariant comment (#4570)"
fi

if ! grep -q "verdict-label mutual exclusion" "$LABELS_DEFAULTS"; then
  fail "defaults/.github/labels.yml is missing the verdict-label mutual-exclusion invariant comment (#4570)"
fi

# --- 5: SHA-scoped verdict lifetime (#5686) ---------------------------------
# Same rationale as 1-4: the fix is expressed partly as role-prompt guidance,
# so a future edit that strips it back out has no compiler to catch it. The
# executable half (verdict-staleness-guard.sh + loom-daemon's
# reconcile_pr_verdicts) is useless if Judge stops STAMPING the marker, since a
# verdict with no marker is UNVERIFIABLE and deliberately fails safe (kept, not
# cleared) — i.e. a silent regression restores the exact pre-#5686 behavior
# with every check still green. These greps are the tie that makes it loud.
VERDICT_GUARD="$ROOT/defaults/scripts/verdict-staleness-guard.sh"
POST_VERDICT="$ROOT/defaults/scripts/post-verdict.sh"

if [[ ! -x "$VERDICT_GUARD" ]]; then
  fail "defaults/scripts/verdict-staleness-guard.sh is missing or not executable (#5686)"
fi

if ! grep -q "### Verdict SHA Marker" "$JUDGE_MD"; then
  fail "judge.md is missing the 'Verdict SHA Marker' section (#5686) — verdicts would stop recording which tree they describe"
fi

if ! grep -q "### Stale-Verdict Sweep" "$JUDGE_MD"; then
  fail "judge.md is missing the 'Stale-Verdict Sweep' section (#5686) — nothing would re-queue a PR whose verdict went stale"
fi

# --- 5b: verdicts are posted through post-verdict.sh, not typed as prose
# (#6382). Before #6382, the canonical commands stamped the marker as literal
# text in a `gh pr comment` heredoc — compliance-by-memory, dropped on roughly
# one verdict in four in production (#6319). post-verdict.sh takes the SHA as
# an argument and appends the marker itself, so it structurally cannot be
# omitted by whichever call site posts through it. This check makes sure that
# script keeps existing AND that judge.md's canonical Label Workflow commands
# keep routing through it, rather than silently reverting to a hand-typed
# marker (which the pre-#6382 version of this very check used to look for).
if [[ ! -x "$POST_VERDICT" ]]; then
  fail "defaults/scripts/post-verdict.sh is missing or not executable (#6382) — verdict comments would go back to hand-typing the loom:verdict-sha marker"
fi

# The canonical approve / request-changes commands in judge.md's Label Workflow
# must actually route through post-verdict.sh with the right verdict token —
# the section existing while the commands agents copy do not is the
# regression that matters most.
if ! grep -qF 'post-verdict.sh <number> approved' "$JUDGE_MD"; then
  fail "judge.md's approval command in Label Workflow no longer posts through post-verdict.sh (#6382) — the loom:verdict-sha marker could be typed as prose again, and dropped"
fi

if ! grep -qF 'post-verdict.sh <number> changes-requested' "$JUDGE_MD"; then
  fail "judge.md's changes-requested command in Label Workflow no longer posts through post-verdict.sh (#6382) — the loom:verdict-sha marker could be typed as prose again, and dropped"
fi

if ! grep -q "verdict-staleness-guard.sh" "$CHAMPION_MERGE_MD"; then
  fail "champion-pr-merge.md no longer runs verdict-staleness-guard.sh before merging — a stale loom:pr approval could auto-merge an unreviewed tree (#5686)"
fi

if ! grep -q "verdict-staleness-guard.sh" "$DOCTOR_MD"; then
  fail "doctor.md no longer runs verdict-staleness-guard.sh before claiming a verdict-labeled PR (#5686)"
fi

if ! grep -q "SHA-scoped" "$LABELS_ROOT"; then
  fail ".github/labels.yml is missing the SHA-scoped verdict-label invariant comment (#5686)"
fi

if [[ "$FAIL" -eq 1 ]]; then
  exit 1
fi

echo "check-cas-recheck-consistency: OK — Verdict-Time CAS Recheck (Judge + Doctor), Champion's Verdict-State Janitor, criterion 1's fix, the labels.yml mutual-exclusion invariant, and the SHA-scoped verdict lifetime (#5686) are all present."
