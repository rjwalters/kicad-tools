#!/usr/bin/env bash
# test-detect-unlabeled-epics.sh - Unit tests for detect-unlabeled-epics.sh (#6715)
#
# detect-unlabeled-epics.sh is a report-only backstop that scans open issues
# for an epic shape (title prefix "Epic:", a "## Phases" body heading, or a
# "[Epic #N]" reference from an open loom:epic-phase child issue's title)
# that carries no `loom:epic` label. It never mutates labels.
#
# Strategy (mirrors test-warn-operator-gated.sh): detect-unlabeled-epics.sh
# gates its main block on `BASH_SOURCE == $0` and defers sourcing
# lib/forge-helpers.sh to that same guarded block, so `source`-ing it here
# only loads the pure functions under test -- no live `gh`/forge dependency,
# no `set -e` pollution from the sourced lib.
#
# Usage:
#   ./.loom/scripts/tests/test-detect-unlabeled-epics.sh

# SC2034: DISMISS_LIST / MATCH_COUNT are read only by functions sourced from
# detect-unlabeled-epics.sh, invisible to the linter.
# shellcheck disable=SC2034

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SRC="$SCRIPTS_DIR/detect-unlabeled-epics.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Source the functions under test (main block is gated on BASH_SOURCE==$0) ---
if [[ ! -f "$SRC" ]]; then
    echo -e "${RED}FATAL${NC}: detect-unlabeled-epics.sh not found at $SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$SRC"

if ! declare -f _scan_unlabeled_epics >/dev/null; then
    echo -e "${RED}FATAL${NC}: could not source _scan_unlabeled_epics from $SRC" >&2
    exit 2
fi

echo "================================"
echo "test-detect-unlabeled-epics.sh (#6715)"
echo "================================"

# --- Title-prefix signal ---
echo ""
echo "Test: title-prefix detection"
if match="$(_epic_title_prefix_match 'Epic: Consolidate the release pipeline')"; then
    assert_eq "Epic:" "$match" "bare 'Epic:' title prefix matches"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: 'Epic:' title prefix did not match"
fi
if match="$(_epic_title_prefix_match '# epic: lowercase, markdown-decorated')"; then
    assert_eq "Epic:" "$match" "markdown-decorated, lowercase 'epic:' prefix matches"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: decorated lowercase 'epic:' prefix did not match"
fi
if _epic_title_prefix_match 'Fix the epic supervisor timer' >/dev/null; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: mid-sentence 'epic' incorrectly matched as a prefix"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: mid-sentence 'epic' does not false-positive as a title prefix"
fi

# --- Phases-heading signal ---
echo ""
echo "Test: '## Phases' body heading detection"
if _epic_body_has_phases_heading $'# Epic: Foo\n\n## Overview\n\n## Phases\n\n### Phase 1'; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: '## Phases' heading is detected"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: '## Phases' heading was NOT detected"
fi
if _epic_body_has_phases_heading $'This body just discusses phases of the moon informally.'; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: prose mentioning 'phases' incorrectly matched a heading"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: prose mentioning 'phases' (not a heading) does not false-positive"
fi

# --- [Epic #N] child-reference extraction ---
echo ""
echo "Test: '[Epic #N]' child-reference extraction"
refs="$(_extract_epic_child_refs '[Epic #42] Implement phase 1 of the thing')"
assert_eq "42" "$refs" "single [Epic #N] reference extracted"
refs="$(_extract_epic_child_refs 'no epic reference here, just #42 as a bare issue mention')"
assert_eq "" "$refs" "a bare #N mention (no [Epic #N] shape) is not extracted"

# --- Dismissal ---
echo ""
echo "Test: dismissal filtering"
if _is_dismissed "42" "17 42 99"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: issue number present in dismiss list is dismissed"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: issue number present in dismiss list was NOT dismissed"
fi
if _is_dismissed "421" "17 42 99"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: '421' incorrectly matched dismiss-list entry '42' (substring, not whole-token)"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: dismiss-list matching is whole-token, not substring"
fi

DISMISS_FIXTURE="$(mktemp)"
trap 'rm -f "$DISMISS_FIXTURE"' EXIT
cat > "$DISMISS_FIXTURE" <<'EOF'
# comment line, ignored
17

42
EOF
file_list="$(_read_dismiss_file "$DISMISS_FIXTURE")"
assert_contains "$file_list" "17" "dismiss file entry '17' is read"
assert_contains "$file_list" "42" "dismiss file entry '42' is read"
assert_eq "" "$(_read_dismiss_file "")" "empty dismiss-file path reads as empty (no-op)"
assert_eq "" "$(_read_dismiss_file "/nonexistent/path/does-not-exist")" "missing dismiss file reads as empty (no-op)"

# --- End-to-end scan: title, body, child-reference, and label/dismiss filters ---
echo ""
echo "Test: end-to-end scan over a fixture issue set"
ISSUES_JSON='[
  {"number": 1, "title": "Epic: Unlabeled title-shaped epic", "body": "Just a body.", "labels": []},
  {"number": 2, "title": "Ordinary issue", "body": "# Epic: Foo\n\n## Phases\n\n### Phase 1", "labels": []},
  {"number": 3, "title": "Already labeled epic", "body": "# Epic: Foo\n\n## Phases", "labels": [{"name": "loom:epic"}]},
  {"number": 4, "title": "Not an epic at all, only known via a child reference", "body": "Nothing else special here.", "labels": []},
  {"number": 5, "title": "[Epic #4] Phase 1 work", "body": "Child of epic #4.", "labels": [{"name": "loom:epic-phase"}]},
  {"number": 6, "title": "Dismissed epic (reclassified)", "body": "## Phases", "labels": []},
  {"number": 7, "title": "Genuinely ordinary issue", "body": "Nothing epic-shaped about this at all.", "labels": []}
]'

DISMISS_LIST="6"
MATCH_COUNT=0
OUTPUT="$(_scan_unlabeled_epics "$ISSUES_JSON")"

assert_contains "$OUTPUT" $'1\t' "issue #1 (title-shaped, unlabeled) is reported"
assert_contains "$OUTPUT" "title declares an epic" "issue #1's reason names the title signal"
assert_contains "$OUTPUT" $'2\t' "issue #2 (## Phases body, unlabeled) is reported"
assert_contains "$OUTPUT" '## Phases' "issue #2's reason names the Phases-heading signal"
assert_not_contains "$OUTPUT" $'3\t' "issue #3 (already loom:epic) is NOT reported"
assert_contains "$OUTPUT" "referenced as [Epic #4] by open phase issue #5" "issue #4 (unlabeled, known only via a child's [Epic #4] reference) is reported"
assert_not_contains "$OUTPUT" $'6\t' "issue #6 (epic-shaped but explicitly dismissed) is NOT reported"
assert_not_contains "$OUTPUT" $'7\t' "issue #7 (genuinely ordinary issue, no signal) is NOT reported"

echo ""
echo "Test: scan never mutates -- purely a stdout report (no gh mutation calls in source)"
if grep -qE 'gh (issue|pr) edit' "$SRC"; then
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: source contains a label-mutating gh call -- this must stay report-only"
else
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: source contains no label-mutating gh call"
fi

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
