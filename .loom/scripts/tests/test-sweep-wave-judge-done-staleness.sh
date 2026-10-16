#!/bin/bash
# test-sweep-wave-judge-done-staleness.sh - Doc-shape tests for the wave
# lifecycle's judge-done resume skip (issue #11384).
#
# A sweep killed between the Judge's approval (judge-done checkpoint) and the
# merge, whose PR then gained a new head, used to resume straight into Merge on
# the strength of the `loom:pr` label alone — merging a tree nobody reviewed.
# The fix: the judge-done skip bullet runs verdict-staleness-guard.sh --clear
# before routing to Merge, and re-runs Judge on exit 12 (STALE, re-queued).
#
# /sweep is a markdown skill, so these are documentation-shape checks scoped to
# the single judge-done bullet (a whole-file grep would pass on Mode C's or the
# rebase pass's own guard call and pin nothing). Also pins the doctor-done
# attempt comment's rebase-pass case and the rebase-pass dispatch log line.
#
# Run from anywhere — paths resolve via the script's own directory.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$SCRIPT_DIR/../../../defaults/.claude/commands/loom"
WAVE_MD="$SKILL_DIR/sweep-wave-lifecycle.md"
EXEC_MD="$SKILL_DIR/sweep-execution-model.md"

for f in "$WAVE_MD" "$EXEC_MD"; do
    if [[ ! -f "$f" ]]; then
        echo "FAIL: skill file not found at $f" >&2
        exit 1
    fi
done

PASS=0
FAIL=0

# assert_in <desc> <haystack> <needle> — fixed-string containment.
# IMPORTANT: pass needles single-quoted so backticks are not expanded.
assert_in() {
    local desc="$1" haystack="$2" needle="$3"
    if grep -qF -- "$needle" <<<"$haystack"; then
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (missing literal: $needle)" >&2
        FAIL=$((FAIL + 1))
    fi
}

assert_not_in() {
    local desc="$1" haystack="$2" needle="$3"
    if grep -qF -- "$needle" <<<"$haystack"; then
        echo "FAIL: $desc (forbidden literal present: $needle)" >&2
        FAIL=$((FAIL + 1))
    else
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    fi
}

echo "--- judge-done checkpoint skip runs the verdict-staleness guard ---"

JUDGE_DONE_BULLET="$(grep -F -- '- If `CHECKPOINT_PHASE == "judge-done"`' "$WAVE_MD" || true)"
if [[ -z "$JUDGE_DONE_BULLET" ]]; then
    echo "FAIL: sweep-wave-lifecycle.md has no judge-done checkpoint-skip bullet" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: judge-done checkpoint-skip bullet present"
    PASS=$((PASS + 1))
    assert_in "judge-done bullet names verdict-staleness-guard.sh" \
        "$JUDGE_DONE_BULLET" 'verdict-staleness-guard.sh'
    assert_in "judge-done bullet runs the guard with --clear" \
        "$JUDGE_DONE_BULLET" 'verdict-staleness-guard.sh <PR> --clear'
    assert_in "judge-done bullet routes exit 12 (STALE) back to Judge" \
        "$JUDGE_DONE_BULLET" '`12` (STALE'
    assert_in "judge-done bullet routes exit 0 (FRESH) to Merge" \
        "$JUDGE_DONE_BULLET" '`0` (FRESH) → Merge'
    assert_in "judge-done bullet keeps the missing-loom:pr divergence fallback" \
        "$JUDGE_DONE_BULLET" 'No `loom:pr`'
    assert_not_in "judge-done bullet no longer routes straight to Merge unguarded" \
        "$JUDGE_DONE_BULLET" 'route the PR straight to Merge'
fi

echo
echo "--- doctor-done attempt comment covers the rebase pass ---"

DOCTOR_DONE_COMMENT="$(grep -F -- '# <attempt>' "$WAVE_MD" || true)"
assert_in "doctor-done comment: attempt unchanged for a rebase pass" \
    "$DOCTOR_DONE_COMMENT" 'unchanged for a rebase pass'
assert_in "doctor-done comment: cycle index + 1 otherwise" \
    "$DOCTOR_DONE_COMMENT" 'cycle index + 1'

echo
echo "--- rebase-pass dispatch has a mandated log line ---"

assert_in "execution model mandates the rebase-pass dispatch log line" \
    "$(cat "$EXEC_MD")" 'PR #P: rebase pass dispatched (no outstanding rejection)'

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
