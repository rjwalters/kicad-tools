#!/usr/bin/env bash
# test-champion-epic-phase-progress-title-prefix.sh - Regression test for #5837.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# #5791 widened champion-common.md's `discover_epic_children` with a
# title-prefix source (e): issues titled "[Epic #N] Phase M: ..." count as
# containment evidence even with no `loom:epic-phase` label and no
# `<!-- loom:epic:N:phase:M -->` body marker. That fix touched ONLY
# `discover_epic_children`, which champion-epic.md Step 0 uses to answer "is
# this epic finished overall".
#
# champion-epic.md's own PER-PHASE counting query -- "Phase Progression" ->
# "Detecting Phase Completion" -- was left on the narrower source (the label
# + the exact body marker). Observed live on 2026-09-30:
#
#   - Epic #5784, Phase 4: child #5798 is titled
#     "[Epic #5784] Phase 4: deconfound kct's match-group length tuning on
#     board 07 (measure first)" with no phase marker and no `loom:epic-phase`
#     label. The query returned OPEN=0 CLOSED=0, so the standard progress
#     comment would have read "Phase 4: 0 closed / 0 total -- not yet
#     complete" while the phase had one open, active child.
#   - Epic #3438, Phase 2: tracked in prose only (via #5410 -- no title
#     prefix, no marker), which NO mechanical query can attribute. That shape
#     is deliberately out of scope for detection; the fail-safe wording below
#     is what covers it.
#
# The fix: union the marker source with a PHASE-SCOPED title-prefix source at
# BOTH call sites (Step 2.75's pre-creation existence check and Detecting
# Phase Completion), and never narrate a zero as "0 total".
#
# Widening only the completion query would be actively unsafe: a
# title-prefix-only phase could then read COMPLETE while Step 2.75 still saw
# no Phase N+1 issues and created a duplicate set -- the exact #6601 failure.
# Hence the both-sites doc pins below.
#
# These queries are prose an LLM instance reads and executes, not standalone
# scripts (same situation as test-champion-epic-children-title-prefix.sh and
# test-champion-epic-phase-marker-normalization.sh) -- so this file mirrors
# the documented filters in local functions, exercises them against fixtures
# reproducing #5784's exact shape, and pins the shipped markdown's snippets
# with assert_doc_contains, catching drift between the two.
#
# What this asserts:
#   1. title_prefix_phase_issues() is PHASE-SCOPED: "[Epic #5784] Phase 4: .."
#      matches PHASE=4 and not PHASE=1, letter-form "Phase B" canonicalizes
#      into PHASE=2, another epic's phase issues never match, and a mid-title
#      mention is not a prefix.
#   2. The #5784 Phase 4 shape end-to-end: with the marker source empty, the
#      union yields OPEN=1 / CLOSED=0 -- so the phase is correctly "in
#      progress", never the misleading "0 closed / 0 total".
#   3. The union dedupes: a child carrying BOTH the marker and the title
#      prefix is counted once.
#   4. Phase completion still requires CLOSED > 0: an all-closed title-prefix
#      phase completes, and a zero-children phase never does.
#   5. The fail-safe progress line: 0/0 produces "count unavailable" wording,
#      never "0 closed / 0 total".
#   6. Doc pins: champion-epic.md defines title_prefix_phase_issues() at
#      BOTH call sites, byte-identically, unions it into both result sets,
#      cites #5837, and interpolates $PROGRESS_LINE (not a bare count) into
#      the progress comment.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-epic-phase-progress-title-prefix.sh

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
CHAMPION_EPIC_MD="$PROMPT_DIR/champion-epic.md"

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

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (unexpectedly found: $needle)"
    fi
}

# =====================================================================
# Mirrors of the shipped prose.
# =====================================================================

# champion-epic.md's canonicalize_phase(), verbatim in behavior (#6967).
canonicalize_phase() {
    local token
    token=$(printf '%s' "$1" | tr -d '[:space:]' | tr '[:lower:]' '[:upper:]')
    if [[ "$token" =~ ^[0-9]+$ ]]; then
        printf '%s' "$token"
    elif [[ "$token" =~ ^[A-Z]$ ]]; then
        printf '%s' "$(( $(printf '%d' "'$token") - 64 ))"
    else
        printf '%s' "$token"
    fi
}

# champion-epic.md's title_prefix_phase_issues(), with the `gh issue list`
# fetch replaced by a fixture argument: keep only issues whose title
# genuinely STARTS WITH "[Epic #<epic>] Phase " AND whose own title phase
# token canonicalizes to the target phase.
title_prefix_phase_issues() {
    local epic="$1" canonical="$2" candidates="$3"
    printf '%s\n' "$candidates" \
        | jq -c --arg p "[Epic #$epic] Phase " '.[] | select(.title | startswith($p))' \
        | while IFS= read -r issue; do
            local title_phase
            title_phase=$(printf '%s' "$issue" | jq -r '.title' \
                | sed -E "s/^\[Epic #$epic\] Phase[[:space:]]+([A-Za-z0-9]+).*/\1/")
            if [[ "$(canonicalize_phase "$title_phase")" == "$canonical" ]]; then
                printf '%s\n' "$issue"
            fi
        done | jq -s 'unique_by(.number) | map({number, title, state})'
}

# The documented union of the marker source and the title-prefix source.
union_phase_issues() {
    local marker="$1" title="$2"
    jq -s 'add | unique_by(.number) | map({number, state})' \
        <(printf '%s' "$marker") <(printf '%s' "$title")
}

# The documented progress-line selection (#5837's fail-safe).
progress_line() {
    local phase="$1" epic="$2" open="$3" closed="$4"
    if [[ "$open" -eq 0 && "$closed" -eq 0 ]]; then
        printf 'Phase %s: **no machine-detectable children found** — neither a `<!-- loom:epic:%s:phase:%s -->` marker nor an `[Epic #%s] Phase %s` title prefix matched any issue. Read this as *count unavailable*, not as zero work.' \
            "$phase" "$epic" "$phase" "$epic" "$phase"
    else
        printf 'Phase %s: %s closed / %s total — not yet complete.' \
            "$phase" "$closed" "$((open + closed))"
    fi
}

# =====================================================================

echo "--- title_prefix_phase_issues(): phase-scoped, not merely epic-scoped ---"

# The real #5784 candidate set as of 2026-09-30: three Phase 1-3 children and
# the Phase 4 child (#5798) that the marker-only query missed entirely.
candidates_5784=$(jq -n '[
  {number: 5785, state: "CLOSED", title: "[Epic #5784] Phase 1: KiCad-oracle completion loop for pour nets"},
  {number: 5786, state: "CLOSED", title: "[Epic #5784] Phase 2: pose-based centerline search for coupled diff pairs"},
  {number: 5787, state: "CLOSED", title: "[Epic #5784] Phase 3: shared-referee runtime parity"},
  {number: 5798, state: "OPEN",   title: "[Epic #5784] Phase 4: deconfound kct'"'"'s match-group length tuning on board 07 (measure first)"},
  {number: 9999, state: "OPEN",   title: "[Epic #1234] Phase 4: a different epic, must never match"},
  {number: 8888, state: "OPEN",   title: "Follow-up to [Epic #5784] Phase 4 — mentioned mid-title, not a prefix"}
]')

phase4=$(title_prefix_phase_issues 5784 4 "$candidates_5784")
assert_eq "1" "$(printf '%s\n' "$phase4" | jq 'length')" \
    "exactly one issue matches epic #5784 Phase 4 (the other epic and the mid-title mention are excluded)"
assert_eq "5798" "$(printf '%s\n' "$phase4" | jq -r '.[0].number')" \
    "the Phase 4 match is #5798 — the child the marker-only query missed (#5837)"

phase1=$(title_prefix_phase_issues 5784 1 "$candidates_5784")
assert_eq "true" "$(printf '%s\n' "$phase1" | jq '[.[].number] == [5785]')" \
    "PHASE=1 matches only #5785 — the title-prefix source is scoped to ONE phase, not the whole epic"

echo
echo "--- title_prefix_phase_issues(): letter-form phase tokens canonicalize like markers (#6967) ---"

letter_form=$(jq -n '[
  {number: 6001, state: "OPEN",   title: "[Epic #372] Phase B: letter-form phase token"},
  {number: 6002, state: "CLOSED", title: "[Epic #372] Phase 1: numeric sibling, different phase"}
]')
assert_eq "true" "$(title_prefix_phase_issues 372 2 "$letter_form" | jq '[.[].number] == [6001]')" \
    "\"Phase B\" counts toward PHASE=2 (both canonicalize to 2) while \"Phase 1\" does not"
assert_eq "0" "$(title_prefix_phase_issues 372 3 "$letter_form" | jq 'length')" \
    "neither issue counts toward PHASE=3 — canonicalization never falsely collapses"

echo
echo "--- title_prefix_phase_issues(): near-miss titles are excluded ---"

near_miss=$(jq -n '[
  {number: 7002, state: "OPEN", title: "[Epic #5784] Phase4: no space before the phase number"},
  {number: 7003, state: "OPEN", title: "[Epic #5784] PhaseOut of scope work"}
]')
assert_eq "0" "$(title_prefix_phase_issues 5784 4 "$near_miss" | jq 'length')" \
    "titles missing the literal 'Phase ' (with trailing space) are excluded"

echo
echo "--- End-to-end #5784 Phase 4: the union rescues the phase from a false '0 total' (#5837) ---"

marker_none='[]'   # #5798 carries no loom:epic-phase label and no phase marker
title_p4=$(title_prefix_phase_issues 5784 4 "$candidates_5784")
phase_issues=$(union_phase_issues "$marker_none" "$title_p4")
open_count=$(printf '%s\n' "$phase_issues" | jq '[.[] | select(.state == "OPEN")] | length')
closed_count=$(printf '%s\n' "$phase_issues" | jq '[.[] | select(.state == "CLOSED")] | length')

assert_eq "1" "$open_count" \
    "OPEN_COUNT=1 for epic #5784 Phase 4 with the marker source empty — title-prefix alone supplies the child"
assert_eq "0" "$closed_count" "CLOSED_COUNT=0 — the phase is in progress, not complete"

line=$(progress_line 4 5784 "$open_count" "$closed_count")
assert_not_contains "$line" "0 closed / 0 total" \
    "the progress comment no longer reads '0 closed / 0 total' for this phase"
assert_eq "Phase 4: 0 closed / 1 total — not yet complete." "$line" \
    "it reports the real 1-child total instead"

echo
echo "--- The union dedupes a child carrying BOTH sources ---"

marker_dup=$(jq -n '[{number: 5798, state: "OPEN"}]')
deduped=$(union_phase_issues "$marker_dup" "$title_p4")
assert_eq "1" "$(printf '%s\n' "$deduped" | jq 'length')" \
    "a child found by BOTH the marker and the title prefix is counted exactly once"

echo
echo "--- Completion still requires CLOSED > 0 (widening must not invent completions) ---"

all_closed=$(union_phase_issues '[]' "$(title_prefix_phase_issues 5784 3 "$candidates_5784")")
ac_open=$(printf '%s\n' "$all_closed" | jq '[.[] | select(.state == "OPEN")] | length')
ac_closed=$(printf '%s\n' "$all_closed" | jq '[.[] | select(.state == "CLOSED")] | length')
if [[ "$ac_open" -eq 0 && "$ac_closed" -gt 0 ]]; then complete=yes; else complete=no; fi
assert_eq "yes" "$complete" \
    "an all-closed title-prefix-only phase (#5784 Phase 3) now completes and can progress to Phase 4"

empty=$(union_phase_issues '[]' '[]')
e_open=$(printf '%s\n' "$empty" | jq '[.[] | select(.state == "OPEN")] | length')
e_closed=$(printf '%s\n' "$empty" | jq '[.[] | select(.state == "CLOSED")] | length')
if [[ "$e_open" -eq 0 && "$e_closed" -gt 0 ]]; then complete=yes; else complete=no; fi
assert_eq "no" "$complete" \
    "a phase with zero machine-detectable children is never complete (CLOSED_COUNT -gt 0 still gates it)"

echo
echo "--- Fail-safe wording for zero machine-detectable children (#5837, epic #3438 Phase 2's shape) ---"

zero_line=$(progress_line 2 3438 "$e_open" "$e_closed")
assert_not_contains "$zero_line" "0 closed / 0 total" \
    "a zero-children phase is NOT narrated as a measured '0 closed / 0 total'"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$zero_line" == *"no machine-detectable children found"* && "$zero_line" == *"count unavailable"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: it says the count is unavailable, not zero"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: it says the count is unavailable, not zero"
    echo "    Actual: '$zero_line'"
fi

echo
echo "--- Doc pins: champion-epic.md ships the title-prefix source at BOTH call sites ---"

fn_count=$(grep -cF 'title_prefix_phase_issues() {' "$CHAMPION_EPIC_MD" || true)
assert_eq "2" "$fn_count" \
    "title_prefix_phase_issues() is defined at exactly 2 call sites (Step 2.75 and Detecting Phase Completion)"

search_count=$(grep -cF -- '--search="\"[Epic #$epic] Phase\" in:title"' "$CHAMPION_EPIC_MD" || true)
assert_eq "2" "$search_count" \
    "both call sites issue the same title-prefix forge search"

call_count=$(grep -cF 'title_prefix_phase_issues "$EPIC_NUMBER" "$CANONICAL_PHASE"' "$CHAMPION_EPIC_MD" || true)
assert_eq "2" "$call_count" \
    "both call sites actually invoke it with the canonicalized phase"

union_count=$(grep -cF 'add | unique_by(.number)' "$CHAMPION_EPIC_MD" || true)
assert_eq "2" "$union_count" \
    "both call sites union the marker source with the title-prefix source, deduped by issue number"

# The two definitions must stay byte-identical — the whole point of #5837 is
# that a second, drifting copy of the matching logic is what broke.
blocks=$(awk '/title_prefix_phase_issues\(\) \{/{capture=1} capture{print} /^}$/{if (capture) {capture=0; print "---BLOCK---"}}' "$CHAMPION_EPIC_MD")
block_one=$(printf '%s\n' "$blocks" | awk '/---BLOCK---/{n++; next} n==0')
block_two=$(printf '%s\n' "$blocks" | awk '/---BLOCK---/{n++; next} n==1')
assert_eq "$block_one" "$block_two" \
    "the two title_prefix_phase_issues() definitions are byte-identical (no drift between call sites)"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    'select(.title | startswith(\"[Epic #$epic] Phase \"))' \
    "the title-prefix source verifies a genuine prefix via jq startswith, not a bare substring match"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    '#5837' \
    "champion-epic.md documents the #5837 phase-scoped title-prefix fix"

echo
echo "--- Doc pins: the progress comment interpolates \$PROGRESS_LINE, never a bare count ---"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    'PROGRESS_LINE="Phase $PHASE: **no machine-detectable children found**' \
    "the zero-children branch sets the count-unavailable wording"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    'PROGRESS_LINE="Phase $PHASE: $CLOSED_COUNT closed / $((OPEN_COUNT + CLOSED_COUNT)) total — not yet complete."' \
    "the normal branch keeps the existing count wording"

body_count=$(grep -cF 'Phase $PHASE: $CLOSED_COUNT closed / $((OPEN_COUNT + CLOSED_COUNT)) total' "$CHAMPION_EPIC_MD" || true)
assert_eq "1" "$body_count" \
    "the bare count string appears only in the PROGRESS_LINE assignment — the comment body itself uses \$PROGRESS_LINE"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
