#!/usr/bin/env bash
# test-champion-followon-out-of-scope.sh - Regression tests for #11761:
# champion-pr-merge.md Step 5.5 treated a bare `## Out of Scope` heading as an
# explicit follow-on section, so a deliberate non-goal ("not planned; do not
# file") set SHOULD_CREATE_ISSUE=true and manufactured backlog.
#
# Step 5.5 is prose an LLM executes, so this extracts the section-parser and
# threshold snippets verbatim from the shipped markdown and runs them against
# fixture PR bodies (no forge commands). Asserts:
#   1. A heading-only `## Out of Scope` exclusion creates no issue.
#   2. A PR body with no qualifying signal creates no issue.
#   3. Explicit Follow-on Work / Deferred sections still create an issue.
#   4. Critical (FIXME/HACK/XXX) and 3+ standard TODO thresholds are unchanged.
#   5. The primary-role summary and champion.json no longer list Out of Scope.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-followon-out-of-scope.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Installed (.loom/scripts) vs source (defaults/scripts) layouts differ in depth (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
MERGE_MD="$PROMPT_DIR/champion-pr-merge.md"
ROLE_MD="$PROMPT_DIR/champion.md"
ROLE_JSON="$SCRIPTS_DIR/../roles/champion.json"

# shellcheck source=lib/assert.sh
source "$TEST_DIR/lib/assert.sh"

# Stage 2 section parser: from the FOLLOWON_SECTION init to its HAS_ flag.
PARSER=$(sed -n '/^FOLLOWON_SECTION=""$/,/^HAS_FOLLOWON_SECTION=false$/p' "$MERGE_MD" | sed '$d')
PARSER="$PARSER"$'\n''HAS_FOLLOWON_SECTION=false; [ -n "$FOLLOWON_SECTION" ] && HAS_FOLLOWON_SECTION=true'
# Stage 4 threshold: from SHOULD_CREATE_ISSUE=false through the elif chain's fi.
THRESHOLD=$(sed -n '/^SHOULD_CREATE_ISSUE=false$/,/^fi$/p' "$MERGE_MD")

should_create() {
    local body="$1" critical="${2:-0}" total="${3:-0}"
    PR_BODY="$body" CRITICAL_COUNT="$critical" TOTAL_TODOS="$total" bash -c \
        "$PARSER"$'\n'"$THRESHOLD"$'\n''echo "$SHOULD_CREATE_ISSUE"' 2>/dev/null | tail -1
}

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -n "$PARSER" && -n "$THRESHOLD" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: extracted parser and threshold snippets"
else
    TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: could not extract Step 5.5 snippets from $MERGE_MD"
fi

OOS=$'## Summary\nDid a thing.\n\n## Out of Scope\nNew config keys are deliberately excluded and not planned.\nDo not file follow-up work for this exclusion.\n\n## Test Plan\nok\n'
assert_eq "false" "$(should_create "$OOS")" "heading-only Out of Scope non-goal creates no issue"
assert_eq "false" "$(should_create $'## Summary\nDid a thing.\n')" "no qualifying signal creates no issue"
assert_eq "true" "$(should_create $'## Summary\nx\n\n## Follow-on Work\nHandle the remaining case.\n\n## Test Plan\nok\n')" "explicit Follow-on Work still creates an issue"
assert_eq "true" "$(should_create $'## Summary\nx\n\n## Deferred\nLater.\n\n## Test Plan\nok\n')" "explicit Deferred section still creates an issue"
assert_eq "true" "$(should_create "$OOS" 1 1)" "critical TODO still creates an issue alongside Out of Scope"
assert_eq "true" "$(should_create "$OOS" 0 3)" "3+ standard TODOs still create an issue"
assert_eq "false" "$(should_create "$OOS" 0 2)" "2 standard TODOs stay below threshold"

assert_not_contains "$(grep -n 'for section_name' "$MERGE_MD")" '"Out of Scope"' "Step 5.5 section list omits Out of Scope"
assert_contains "$(grep -A4 '^## Follow-on Issue Creation' "$ROLE_MD")" 'Phase 2"; not "Out of Scope"' "role summary excludes Out of Scope from the creation triggers"
assert_not_contains "$(cat "$ROLE_JSON")" '"Out of Scope"' "champion.json bodySections omits Out of Scope"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
