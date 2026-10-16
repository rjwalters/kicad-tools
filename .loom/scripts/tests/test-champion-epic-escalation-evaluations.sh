#!/usr/bin/env bash
# test-champion-epic-escalation-evaluations.sh - Regression test for issue #12081
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# Champion's "rejected repeatedly without revision" escalation counted a silent
# re-entry (an unrevised epic skipped without a comment) as a second rejection,
# but the escalation comment named only a total. An operator reading the
# timeline saw one posted verdict and could not tell the second counted
# evaluation was a non-verdict. Both escalation templates (champion-epic.md,
# champion-issue-promo.md) must now list each counted evaluation with a link and
# say "N posted + M unrevised", never "rejected N+M times". The two-evaluation
# limit and the epic skip tally (#4967) are unchanged and checked here too.
#
# Hermetic: pure file reads. No forge/network.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi

EPIC="$ROLE_DIR/champion-epic.md"
PROMO="$ROLE_DIR/champion-issue-promo.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }
check() { # check <desc> <file> <fixed-string>
    if grep -qF -- "$3" "$2"; then pass "$1"; else fail "$1 (missing: $3)"; fi
}

echo "Test 1: epic escalation lists counted evaluations with links"
check "epic builds EVALUATIONS_TEXT from posted rejections' html_url" "$EPIC" 'posted rejection: " + .html_url'
check "epic lists unrevised re-entries separately" "$EPIC" 'unrevised re-entry(ies), no comment'
check "epic links the verdict comment carrying the tally" "$EPIC" "jq -r '.html_url"
check "epic template renders the evaluations list" "$EPIC" '$EVALUATIONS_TEXT'
check "epic distinguishes posted from unrevised" "$EPIC" 'posted rejection(s) + $SKIP_STREAK unrevised re-entry(ies)'

echo "Test 2: proposal escalation lists counted evaluations with links"
check "promo builds EVALUATIONS_TEXT from posted rejections' html_url" "$PROMO" 'posted rejection: " + .html_url'
check "promo lists the unrevised re-entry when UNREVISED=yes" "$PROMO" '1 unrevised re-entry, no comment'
check "promo template renders the evaluations list" "$PROMO" '$EVALUATIONS_TEXT'
check "promo distinguishes posted from unrevised" "$PROMO" 'posted rejection(s), plus an unrevised re-entry'

echo "Test 3: never reads as 'rejected N+M times'"
if grep -qF 'rejections plus $SKIP_STREAK silent skips' "$EPIC"; then
    fail "epic still folds silent skips into a bare rejection count"
else pass "epic no longer folds silent skips into a rejection count"; fi

echo "Test 4: limit and skip tally unchanged"
check "epic cap still LOOM_MAX_UNREVISED_EVALUATIONS default 2" "$EPIC" 'LOOM_MAX_UNREVISED_EVALUATIONS:-2'
check "epic skip tally still PATCHed in place" "$EPIC" 'champion:epic-unrevised-skips:'
check "epic UNREVISED_EVALS still sums rejections and skips" "$EPIC" 'UNREVISED_EVALS=$(( PRIOR_REJECTIONS + SKIP_STREAK ))'

echo ""
echo "Ran $TESTS_RUN checks, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
