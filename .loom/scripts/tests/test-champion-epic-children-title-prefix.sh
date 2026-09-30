#!/usr/bin/env bash
# test-champion-epic-children-title-prefix.sh - Regression test for #5791.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# champion-common.md's `discover_epic_children` had four containment/prose
# sources: (a) `loom:epic-phase` issues carrying the
# `<!-- loom:epic:$N:phase:M -->` marker, (b) native GitHub sub-issues, (c)
# `- [ ] #N` task-list entries in the EPIC'S OWN BODY, and (d) weak prose
# references matching the literal string "Epic #$N" in another issue's body.
#
# None of these recognize an epic whose phase children were filed directly
# (by the operator or another role) using only the
# `[Epic #N] Phase M: ...` TITLE convention plus a `Part of #N (Phase M)`
# body back-reference -- the exact shape Champion's own Step 3 template
# produces, just filed by someone else. This happened twice on this repo:
#
#   - #5774: children #5775/#5776/#5777 were listed only in an operator
#     COMMENT (source (c) reads the epic's BODY, not comments).
#   - #5784: children #5785/#5786/#5787 have no listing comment at all --
#     their bodies say "Part of #5784 (Phase 1)", not the literal "Epic
#     #5784" phrase source (d) searches for, and the epic body never lists
#     them as a task list. All four sources returned zero for both epics.
#
# The fix adds source (e): search for issues titled "[Epic #$N] Phase "
# (the literal prefix champion-epic.md's own Step 3 template emits)
# regardless of who authored them, verified as a genuine PREFIX match (not
# merely a substring anywhere in the title), and fold matches into the
# STRONG (containment) evidence set alongside (a)/(b)/(c).
#
# `discover_epic_children` is prose an LLM instance reads and executes, not a
# standalone script (same situation as
# test-champion-epic-phase-marker-normalization.sh) -- so this file mirrors
# the documented title-prefix filter and strong/weak union logic in local
# functions, exercises them against fixtures reproducing #5774's and #5784's
# exact shapes, and pins the shipped markdown's exact snippets with
# assert_doc_contains, catching drift between the two.
#
# What this asserts:
#   1. title_prefix_filter() recognizes "[Epic #$N] Phase M: ..." titles for
#      the target epic number and rejects titles that only CONTAIN the
#      phrase mid-string (not a genuine prefix), titles for a different
#      epic, and titles missing the trailing "Phase " space.
#   2. The #5784 shape end-to-end: three candidates titled "[Epic #5784]
#      Phase N: ..." whose bodies say "Part of #5784 (Phase N)" (no phase
#      marker, no sub-issue, no epic-body task-list entry, no literal "Epic
#      #5784" phrase) are found as STRONG evidence via title-prefix ALONE.
#   3. The #5774 shape: the same holds for #5775/#5776/#5777 against epic
#      #5774.
#   4. A title that merely mentions "[Epic #N] Phase" without being a genuine
#      prefix (e.g. a re-titled issue with the phrase relocated) is excluded
#      from the strong set -- the prefix check is not a bare substring test.
#   5. Doc pins: champion-common.md defines source (e), folds it into the
#      STRONG union alongside (a)/(b)/(c), and tracks it in
#      EPIC_CHILD_SOURCES as "title-prefix".
#
# Usage:
#   ./.loom/scripts/tests/test-champion-epic-children-title-prefix.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside a bare source checkout -- probe both
# rather than hard-coding one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_COMMON_MD="$PROMPT_DIR/champion-common.md"

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

assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (missing literal in $file: $needle)"
    fi
}

# =====================================================================
# title_prefix_filter(), mirroring champion-common.md's source (e): given a
# JSON array of candidate issues (each with .number, .state, .title) and a
# target epic number, keep only issues whose title genuinely STARTS WITH
# "[Epic #<num>] Phase " -- mirrors the shipped `select(.title |
# startswith(...))` jq filter, not a bare substring search.
# =====================================================================
title_prefix_filter() {
    local num="$1" candidates_json="$2" prefix
    prefix="[Epic #$num] Phase "
    printf '%s\n' "$candidates_json" \
        | jq -c --arg p "$prefix" '[.[] | select(.title | startswith($p))] | map({number, state})'
}

# Mirrors discover_epic_children's union: STRONG = unique(a ∪ b ∪ c ∪ e),
# excluding the epic's own number.
union_strong() {
    local epic_num="$1" a="$2" b="$3" c="$4" e="$5"
    jq -s --argjson epic "$epic_num" \
        'add | map(select(.number != $epic)) | unique_by(.number)' \
        <(printf '%s' "$a") <(printf '%s' "$b") <(printf '%s' "$c") <(printf '%s' "$e")
}

echo "--- title_prefix_filter(): genuine prefix matches for the target epic ---"

candidates_5784=$(jq -n '[
  {number: 5785, state: "OPEN", title: "[Epic #5784] Phase 1: KiCad-oracle completion loop for pour nets + one completion verdict"},
  {number: 5786, state: "OPEN", title: "[Epic #5784] Phase 2: pose-based centerline search (Dubins heuristic) for coupled diff pairs"},
  {number: 5787, state: "CLOSED", title: "[Epic #5784] Phase 3: shared-referee runtime parity"},
  {number: 9999, state: "OPEN", title: "[Epic #1234] Phase 1: unrelated epic, must not match"},
  {number: 8888, state: "OPEN", title: "A random issue that mentions [Epic #5784] Phase 1 mid-sentence, not as a prefix"}
]')
result=$(title_prefix_filter 5784 "$candidates_5784")
assert_eq "3" "$(printf '%s\n' "$result" | jq 'length')" \
    "exactly the 3 genuinely-prefixed #5784 phase issues match (unrelated epic and mid-sentence mention excluded)"
assert_eq "true" "$(printf '%s\n' "$result" | jq '[.[].number] | sort == [5785,5786,5787]')" \
    "the matched numbers are exactly 5785, 5786, 5787"

echo
echo "--- title_prefix_filter(): the #5774 shape (a second real recurrence) ---"

candidates_5774=$(jq -n '[
  {number: 5775, state: "CLOSED", title: "[Epic #5774] Phase 1: changelog fragments + per-PR changelog check"},
  {number: 5776, state: "CLOSED", title: "[Epic #5774] Phase 2: changelog-driven release notes"},
  {number: 5777, state: "CLOSED", title: "[Epic #5774] Phase 3: auto-release cutover"}
]')
result_5774=$(title_prefix_filter 5774 "$candidates_5774")
assert_eq "3" "$(printf '%s\n' "$result_5774" | jq 'length')" \
    "all 3 of #5774's title-prefixed phase children are found"
assert_eq "3" "$(printf '%s\n' "$result_5774" | jq '[.[] | select(.state=="CLOSED")] | length')" \
    "all 3 are counted CLOSED (matches the real #5774 outcome)"

echo
echo "--- title_prefix_filter(): a mid-string mention is NOT a genuine prefix match ---"

mid_string_only=$(jq -n '[
  {number: 7001, state: "OPEN", title: "Follow-up to [Epic #5784] Phase 1 -- not itself a phase issue"}
]')
result_mid=$(title_prefix_filter 5784 "$mid_string_only")
assert_eq "0" "$(printf '%s\n' "$result_mid" | jq 'length')" \
    "a title that merely CONTAINS the phrase (not as a prefix) is excluded"

echo
echo "--- title_prefix_filter(): the trailing space after 'Phase' is required (no false match on 'Phaser'/'Phase2') ---"

near_miss=$(jq -n '[
  {number: 7002, state: "OPEN", title: "[Epic #5784] Phase2: no space before the phase number"},
  {number: 7003, state: "OPEN", title: "[Epic #5784] PhaseOut of scope work"}
]')
result_near=$(title_prefix_filter 5784 "$near_miss")
assert_eq "0" "$(printf '%s\n' "$result_near" | jq 'length')" \
    "titles missing the literal 'Phase ' (with trailing space) are excluded"

echo
echo "--- End-to-end #5784 shape: title-prefix is the ONLY source that finds the children (#5791) ---"

# Reproduces #5784's exact shape: no phase marker (a), no native sub-issues
# (b), no epic-body task-list entry (c), and bodies say "Part of #5784
# (Phase N)" rather than the literal "Epic #5784" phrase, so the pre-existing
# weak source (d) also finds nothing. Only source (e) -- title-prefix --
# contributes.
a='[]'   # no loom:epic-phase / phase-marker children
b='[]'   # no native GitHub sub-issues
c='[]'   # epic body has no "- [ ] #N" task list
e=$(title_prefix_filter 5784 "$candidates_5784")

strong=$(union_strong 5784 "$a" "$b" "$c" "$e")
open_count=$(printf '%s\n' "$strong" | jq '[.[] | select(.state=="OPEN")] | length')
closed_count=$(printf '%s\n' "$strong" | jq '[.[] | select(.state=="CLOSED")] | length')

assert_eq "2" "$open_count" \
    "STRONG_OPEN=2 for epic #5784 (issues 5785, 5786) with (a)/(b)/(c) all empty -- title-prefix alone supplies containment evidence"
assert_eq "1" "$closed_count" \
    "STRONG_CLOSED=1 for epic #5784 (issue 5787) -- so the epic is correctly classified as blocked-in-progress, not blocked-not-started"

echo
echo "--- Doc pins: champion-common.md ships source (e) and folds it into the STRONG union ---"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    '--search "\"[Epic #$num] Phase\" in:title"' \
    "discover_epic_children queries gh issue list for the title-prefix phrase"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    'select(.title | startswith(\"[Epic #$num] Phase \"))' \
    "the title-prefix source verifies a genuine prefix via jq startswith, not a bare substring match"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    '<(printf '"'"'%s'"'"' "$c") <(printf '"'"'%s'"'"' "$e")' \
    "the STRONG union includes source (e) alongside (a)/(b)/(c)"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    'srcs="$srcs,title-prefix"' \
    "EPIC_CHILD_SOURCES records \"title-prefix\" when source (e) contributes"

assert_doc_contains "$CHAMPION_COMMON_MD" \
    '#5791' \
    "champion-common.md documents the #5791 title-prefix-source fix"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
