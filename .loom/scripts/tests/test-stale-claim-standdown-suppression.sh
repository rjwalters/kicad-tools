#!/usr/bin/env bash
# test-stale-claim-standdown-suppression.sh - Regression guard for #5123,
# re-pointed at the shared evaluator by #6514.
#
# #5123 closed two gaps in the loom:reviewing / loom:treating / loom:curating
# stale-claim livelock protection:
#
#   1. judge.md's and doctor.md's "Fresh" stand-down path posted a new marked
#      `<!-- loom:standdown claim=$CLAIMED_AT -->` comment on EVERY pass, even
#      when the immediately preceding comment already carried the identical
#      marker for the same claim — pure noise (observed live on PR #5115: 3
#      near-identical stand-downs in 85 seconds).
#   2. loom:curating (Curator) had no TTL/marker/bounded-fallback mechanism at
#      all — a dead Curator claim was permanently stranded, the exact failure
#      shape already fixed for loom:reviewing/loom:treating.
#
# #6514 then found that #5123's suppression, combined with the old "any comment
# after the claim means the claimant is alive" rule, LIVELOCKED the check: one
# routine Builder status note pinned the claim fresh forever, while the
# suppression starved the bounded fallback's streak so it could never fire
# either. The fix moved the whole computation out of the three prompts and into
# `defaults/scripts/claim-staleness.sh`, whose behaviour — including #5123's
# actual requirement, "never post a duplicate stand-down comment" — is covered
# directly and executably by `test-claim-staleness.sh`.
#
# So this suite is now a DOC-CONFORMANCE guard: it asserts the three role
# prompts really do drive the shared evaluator, still document the #5123 /
# #6514 / #4790 guarantees, and have not re-grown a private copy of the
# defective logic.
#
#   A. Each prompt drives `claim-staleness.sh check` and
#      `claim-staleness.sh standdown` with ITS OWN claim label, and none of them
#      still carries the hand-rolled `COMMENTS_AFTER=` / `LATEST_COMMENT_BODY=`
#      blocks the shared script replaced.
#   B. The decision table each prompt documents covers exactly the CLAIM_STATE
#      values the script can actually emit — the anti-drift join between the
#      prose table and the implementation.
#
# Usage:
#   ./.loom/scripts/tests/test-stale-claim-standdown-suppression.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAIM_STALENESS="$HELPERS_DIR/claim-staleness.sh"

# Role prompts are shipped (installed at .claude/commands/loom/<role>.md), so
# resolve each the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout, where it is a
# symlink back into defaults/), falling back to the defaults/ source-tree path
# (a bare source checkout with no .claude/commands/loom/ copy yet). See issue
# #6194 / #6241.
resolve_role_md() {
    local role="$1"
    if [[ -f "$REPO_ROOT/.claude/commands/loom/$role.md" ]]; then
        echo "$REPO_ROOT/.claude/commands/loom/$role.md"
    else
        echo "$REPO_ROOT/defaults/.claude/commands/loom/$role.md"
    fi
}
JUDGE_MD="$(resolve_role_md judge)"
DOCTOR_MD="$(resolve_role_md doctor)"
CURATOR_MD="$(resolve_role_md curator)"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

for f in "$JUDGE_MD" "$DOCTOR_MD" "$CURATOR_MD"; do
    [[ -f "$f" ]] || {
        echo "FATAL: missing $f"
        exit 1
    }
done
[[ -x "$CLAIM_STALENESS" ]] || {
    echo "FATAL: $CLAIM_STALENESS missing or not executable"
    exit 1
}

assert_file_contains() { # <label> <file> <substring>
    if grep -qF -- "$3" "$2"; then
        pass "$1"
    else
        fail "$1 (expected to find: $3)"
    fi
}

assert_file_lacks() { # <label> <file> <substring>
    if grep -qF -- "$3" "$2"; then
        fail "$1 (must NOT contain: $3)"
    else
        pass "$1"
    fi
}

# ============================================================================
# Part A: each role prompt drives the shared evaluator, not a private copy
# ============================================================================

echo "Part A: role prompts drive claim-staleness.sh (no private re-implementation)"

check_lane() { # <lane> <file> <claim-label>
    local lane="$1" file="$2" label="$3"

    assert_file_contains "$lane: runs \`claim-staleness.sh check --label $label\`" \
        "$file" "claim-staleness.sh check --number \"\$N\" --label $label"
    assert_file_contains "$lane: runs \`claim-staleness.sh standdown --label $label\`" \
        "$file" "claim-staleness.sh standdown --number \"\$N\" --label $label"
    assert_file_contains "$lane: documents the claim-activity marker (#6514)" \
        "$file" "<!-- loom:claim-activity claim="
    assert_file_contains "$lane: documents the seq-bumped stand-down marker (#6514)" \
        "$file" "seq="
    assert_file_contains "$lane: keeps the #4790 age floor on the bounded fallback" \
        "$file" "#4790"
    assert_file_contains "$lane: still credits #5123 for comment de-duplication" \
        "$file" "#5123"

    # The defective inline computation must not come back.
    assert_file_lacks "$lane: no hand-rolled COMMENTS_AFTER computation" \
        "$file" 'COMMENTS_AFTER=$(printf'
    assert_file_lacks "$lane: no hand-rolled duplicate-suppression block" \
        "$file" "LATEST_COMMENT_BODY="
    assert_file_lacks "$lane: no 'any comment means fresh' decision row" \
        "$file" 'OR `COMMENTS_AFTER > 0`'
}

check_lane "judge" "$JUDGE_MD" "loom:reviewing"
check_lane "doctor" "$DOCTOR_MD" "loom:treating"
check_lane "curator" "$CURATOR_MD" "loom:curating"

# ============================================================================
# Part B: the documented decision table matches the states the script emits
# ============================================================================

echo ""
echo "Part B: decision-table / implementation anti-drift join"

# Every state the script can assign, in the order the prompts tabulate them.
CLAIM_STATES=(unclaimed fresh stale stale-bounded-fallback unknown)

for state in "${CLAIM_STATES[@]}"; do
    if grep -qF -- "CLAIM_STATE=\"$state\"" "$CLAIM_STALENESS"; then
        pass "claim-staleness.sh can emit CLAIM_STATE=$state"
    else
        fail "claim-staleness.sh no longer emits CLAIM_STATE=$state"
    fi
    for f in "$JUDGE_MD" "$DOCTOR_MD" "$CURATOR_MD"; do
        if grep -qF -- "\`$state\`" "$f"; then
            pass "$(basename "$f" .md): decision table documents \`$state\`"
        else
            fail "$(basename "$f" .md): decision table is missing \`$state\`"
        fi
    done
done

# The script must not grow a state the prompts do not document.
while IFS= read -r found; do
    [[ -n "$found" ]] || continue
    matched=0
    for state in "${CLAIM_STATES[@]}"; do
        [[ "$found" == "$state" ]] && matched=1
    done
    if [[ "$matched" -eq 1 ]]; then
        pass "claim-staleness.sh state '$found' is documented by the prompts"
    else
        fail "claim-staleness.sh emits undocumented state '$found'"
    fi
done < <(grep -oE 'CLAIM_STATE="[a-z-]+"' "$CLAIM_STALENESS" | sed 's/CLAIM_STATE="//; s/"//' | sort -u)

# The three lanes must keep their distinct thresholds (a Doctor fix cycle
# legitimately outruns a Judge review pass).
assert_file_contains "judge: keeps LOOM_STALE_REVIEWING_MINUTES" "$JUDGE_MD" "LOOM_STALE_REVIEWING_MINUTES"
assert_file_contains "doctor: keeps LOOM_STALE_TREATING_MINUTES" "$DOCTOR_MD" "LOOM_STALE_TREATING_MINUTES"
assert_file_contains "curator: keeps LOOM_STALE_CURATING_MINUTES" "$CURATOR_MD" "LOOM_STALE_CURATING_MINUTES"
for f in "$JUDGE_MD" "$DOCTOR_MD" "$CURATOR_MD"; do
    assert_file_contains "$(basename "$f" .md): keeps LOOM_MAX_STANDDOWN_STREAK" \
        "$f" "LOOM_MAX_STANDDOWN_STREAK"
done

# ============================================================================
# Summary
# ============================================================================

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
