#!/usr/bin/env bash
# test-champion-epic-phase-marker-normalization.sh - Regression tests for
# #6967: champion-epic.md's Step 2.75 ("Pre-Creation Existence Check for
# Phase Issues") and "Detecting Phase Completion" both compared phase markers
# via an EXACT literal-string `--search="loom:epic:$EPIC_NUMBER:phase:$PHASE
# in:body"` query. A pre-existing marker in a different form for the same
# logical phase (e.g. a historical/foreign `phase:B` marker) does not match a
# later pass's freshly-derived numeric `phase:2` under that comparison, so
# the existence check silently misses it and creates a duplicate phase-issue
# set -- the same class of missing-idempotency bug #6601 already fixed for
# the "nothing at all existed" case, now recurring for "something existed,
# but in a different phase-token form".
#
# champion-epic.md's Step 2.75 and "Detecting Phase Completion" are prose an
# LLM instance reads and executes, not a standalone script (same situation as
# test-champion-critical-file-check.sh) -- so this file mirrors the
# documented `canonicalize_phase()` + candidate-filter logic in local
# functions and pins the shipped markdown's exact commands with
# `assert_doc_contains` / `assert_doc_lacks`, catching drift between the two.
#
# The fix (#6967):
#   1. `canonicalize_phase()` maps a phase token to a canonical integer --
#      A/B/C/... (case-insensitive) to 1/2/3/..., and a bare integer to
#      itself -- so `phase:B` and `phase:2` compare equal. An unrecognized
#      token (anything else) is returned unchanged, so it can never
#      *falsely* collapse into a match.
#   2. Both call sites widen their `--search` query to the epic-number
#      prefix only (`loom:epic:$EPIC_NUMBER:phase in:body`, dropping the
#      exact `:$PHASE` suffix) and then narrow precisely, client-side, by
#      canonicalized comparison of each candidate's own marker token --
#      strictly more inclusive at the query layer, never less precise at the
#      decision layer.
#   3. Phase-issue creation (Step 3 and "Creating Next Phase Issues") already
#      emits the marker in canonical numeric form only (`phase:1`,
#      `phase:<N+1>`) -- verified unchanged by this fix, pinned below so a
#      future edit can't silently reintroduce a letter-form emission.
#
# What this asserts:
#   1. canonicalize_phase() maps A/B/C (any case) to 1/2/3, passes bare
#      integers through unchanged, and leaves an unrecognized token as-is.
#   2. A pre-existing `phase:B`-marked issue is found as "already exists"
#      when a later pass re-derives `PHASE=2` for the same logical phase --
#      no duplicate would be created.
#   3. Two genuinely different phases (`phase:1` vs `phase:2`) are NEVER
#      treated as equivalent -- an issue for one phase never counts as
#      "already exists" for the other.
#   4. The same canonicalized-filter logic also fixes "Detecting Phase
#      Completion": a closed `phase:B` issue counts toward PHASE=2's
#      closed-count, so phase-completion is still correctly detected even
#      when the existing issue predates the numeric-only convention.
#   5. The shipped markdown defines `canonicalize_phase()` at both call
#      sites, widens both `--search` queries to the epic-number prefix, and
#      still emits only canonical numeric markers at both creation sites.
#
# Usage:
#   ./.loom/scripts/tests/test-champion-epic-phase-marker-normalization.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_EPIC_MD="$PROMPT_DIR/champion-epic.md"

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# Pin a literal snippet as present verbatim in a doc file -- catches drift
# between this test's mirrored function and the shipped markdown.
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

# Pin a literal snippet's ABSENCE from a doc file -- catches a regression
# back to the exact-literal-suffix search this fix removed.
assert_doc_lacks() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found stale literal in $file: $needle)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

# =====================================================================
# canonicalize_phase(), mirrored verbatim from champion-epic.md's Step 2.75
# and "Detecting Phase Completion" (the two call sites define the identical
# function so they can never disagree).
# =====================================================================
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

# Mirrors the candidate-filter loop shared by both call sites: given a JSON
# array of candidate issues (each with .number, .state, .body) and a target
# EPIC_NUMBER/PHASE, keep only the issues whose own marker phase token
# canonicalizes to the same phase.
filter_existing_phase_issues() {
    local epic_number="$1" phase="$2" candidates_json="$3"
    local canonical_phase
    canonical_phase=$(canonicalize_phase "$phase")

    printf '%s\n' "$candidates_json" | jq -c '.[]' | while IFS= read -r issue; do
        local marker_phase
        marker_phase=$(printf '%s' "$issue" | jq -r '.body' | \
            grep -oE "loom:epic:$epic_number:phase:[A-Za-z0-9]+" | head -1 | \
            sed -E "s/^loom:epic:$epic_number:phase://")
        [ -z "$marker_phase" ] && continue
        if [ "$(canonicalize_phase "$marker_phase")" = "$canonical_phase" ]; then
            printf '%s\n' "$issue" | jq 'del(.body)'
        fi
    done | jq -s '.'
}

echo "--- canonicalize_phase(): letter forms map to their alphabetical position ---"

assert_eq "1" "$(canonicalize_phase "A")" "A canonicalizes to 1"
assert_eq "2" "$(canonicalize_phase "B")" "B canonicalizes to 2"
assert_eq "2" "$(canonicalize_phase "b")" "lowercase b canonicalizes to 2 (case-insensitive)"
assert_eq "3" "$(canonicalize_phase "C")" "C canonicalizes to 3"

echo
echo "--- canonicalize_phase(): bare integers pass through unchanged ---"

assert_eq "1" "$(canonicalize_phase "1")" "1 canonicalizes to 1"
assert_eq "2" "$(canonicalize_phase "2")" "2 canonicalizes to 2"
assert_eq "10" "$(canonicalize_phase "10")" "10 canonicalizes to 10 (multi-digit)"

echo
echo "--- canonicalize_phase(): unrecognized tokens are left as-is (never guessed at) ---"

assert_eq "1A" "$(canonicalize_phase "1a")" "a non-integer, non-single-letter token (1a) is left unchanged (uppercased)"
assert_eq "PHASE-X" "$(canonicalize_phase "phase-x")" "an arbitrary non-matching token is left unchanged (uppercased)"

echo
echo "--- Step 2.75 existence check: a pre-existing letter-form marker is found for the equivalent numeric phase (#6967) ---"

# The exact scenario in the issue: an existing issue carries a historical
# `phase:B` marker; a later pass re-derives PHASE=2 for the same logical
# phase (B is the 2nd letter). The exact-literal-string search this fix
# replaces would have missed it and created a duplicate.
candidates=$(jq -n '[
  {number: 501, title: "[Epic #372] Phase B work", state: "CLOSED",
   body: "<!-- loom:epic:372:phase:B -->\nSome phase B body text."}
]')
result=$(filter_existing_phase_issues 372 2 "$candidates")
assert_eq "1" "$(printf '%s\n' "$result" | jq 'length')" \
    "a phase:B-marked issue is recognized as already existing when re-evaluated as PHASE=2"
assert_eq "501" "$(printf '%s\n' "$result" | jq '.[0].number')" \
    "the matched issue is #501, the phase:B-marked one"

echo
echo "--- Step 2.75 existence check: an equivalent numeric-form marker still matches itself (no regression) ---"

candidates_numeric=$(jq -n '[
  {number: 79, title: "[Epic #372] Phase 1 issue", state: "CLOSED",
   body: "<!-- loom:epic:372:phase:1 -->\nPhase 1 body text."}
]')
result_numeric=$(filter_existing_phase_issues 372 1 "$candidates_numeric")
assert_eq "1" "$(printf '%s\n' "$result_numeric" | jq 'length')" \
    "a phase:1-marked issue is still recognized as already existing for PHASE=1 (baseline #6601 behavior unchanged)"

echo
echo "--- Step 2.75 existence check: two genuinely different phases must NEVER be treated as equivalent (#6967 edge case) ---"

# An existing phase:1 issue must never count as "already exists" for PHASE=2
# -- only same-phase differing FORMS collapse, never different phases.
candidates_phase1_only=$(jq -n '[
  {number: 79, title: "[Epic 372] Phase 1 issue", state: "CLOSED",
   body: "<!-- loom:epic:372:phase:1 -->\nPhase 1 body text."}
]')
result_cross_phase=$(filter_existing_phase_issues 372 2 "$candidates_phase1_only")
assert_eq "0" "$(printf '%s\n' "$result_cross_phase" | jq 'length')" \
    "a phase:1-marked issue does NOT count as already-existing for PHASE=2 (different phases never collapse)"

# And the mirror: an existing phase:B (=2) issue must not satisfy a PHASE=1
# (or PHASE=3, i.e. letter-form C) existence check either.
candidates_phaseB_only=$(jq -n '[
  {number: 501, title: "[Epic 372] Phase B issue", state: "CLOSED",
   body: "<!-- loom:epic:372:phase:B -->\nPhase B body text."}
]')
result_cross_phase2=$(filter_existing_phase_issues 372 1 "$candidates_phaseB_only")
assert_eq "0" "$(printf '%s\n' "$result_cross_phase2" | jq 'length')" \
    "a phase:B-marked issue does NOT count as already-existing for PHASE=1"
result_cross_phase3=$(filter_existing_phase_issues 372 3 "$candidates_phaseB_only")
assert_eq "0" "$(printf '%s\n' "$result_cross_phase3" | jq 'length')" \
    "a phase:B-marked issue does NOT count as already-existing for PHASE=3 (letter-form C)"

echo
echo "--- Step 2.75 existence check: a mixed candidate set (multiple epics/phases in the same label) is filtered correctly ---"

mixed_candidates=$(jq -n '[
  {number: 79, title: "[Epic 372] Phase 1 issue", state: "CLOSED",
   body: "<!-- loom:epic:372:phase:1 -->\nPhase 1 body."},
  {number: 501, title: "[Epic 372] Phase B issue", state: "CLOSED",
   body: "<!-- loom:epic:372:phase:B -->\nPhase B body."},
  {number: 900, title: "[Epic 999] Phase 2 issue (different epic)", state: "OPEN",
   body: "<!-- loom:epic:999:phase:2 -->\nA different epic entirely."}
]')
result_mixed=$(filter_existing_phase_issues 372 2 "$mixed_candidates")
assert_eq "1" "$(printf '%s\n' "$result_mixed" | jq 'length')" \
    "only the phase:B issue (#501) matches PHASE=2 for epic 372 -- the phase:1 sibling and the different-epic issue are excluded"
assert_eq "501" "$(printf '%s\n' "$result_mixed" | jq '.[0].number')" \
    "the matched issue is #501"

echo
echo "--- Detecting Phase Completion: a closed letter-form marker still counts toward the equivalent numeric phase's closed count (#6967) ---"

# Mirrors "Detecting Phase Completion"'s OPEN_COUNT/CLOSED_COUNT logic using
# the same canonicalized filter -- a phase:B issue, closed, must be counted
# when the phase is re-evaluated as PHASE=2 so phase-completion detection
# (and therefore Phase N+1 creation) is not silently blocked or, worse,
# blind to an issue that was never re-counted at all.
completion_candidates=$(jq -n '[
  {number: 501, title: "[Epic 372] Phase B issue", state: "CLOSED",
   body: "<!-- loom:epic:372:phase:B -->\nPhase B body."}
]')
phase_issues=$(filter_existing_phase_issues 372 2 "$completion_candidates")
open_count=$(printf '%s\n' "$phase_issues" | jq '[.[] | select(.state == "OPEN")] | length')
closed_count=$(printf '%s\n' "$phase_issues" | jq '[.[] | select(.state == "CLOSED")] | length')
assert_eq "0" "$open_count" "no open issues for the re-derived PHASE=2 (the only match, #501, is closed)"
assert_eq "1" "$closed_count" "the phase:B-marked issue counts as closed under the re-derived PHASE=2"

echo
echo "--- Doc pins: shipped markdown ships canonicalize_phase() at both call sites (#6967) ---"

canonicalize_phase_count=$(grep -cF 'canonicalize_phase() {' "$CHAMPION_EPIC_MD" || true)
assert_eq "2" "$canonicalize_phase_count" \
    "canonicalize_phase() is defined at exactly 2 call sites (Step 2.75 and Detecting Phase Completion)"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    'elif [[ "$token" =~ ^[A-Z]$ ]]; then' \
    "canonicalize_phase() recognizes single-letter phase tokens"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    '#6967' \
    "champion-epic.md documents the #6967 phase-marker-normalization fix"

echo
echo "--- Doc pins: both call sites widen the --search query to the epic-number prefix only ---"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    '--search="loom:epic:$EPIC_NUMBER:phase in:body"' \
    "at least one call site's --search query drops the exact :\$PHASE suffix"

search_prefix_count=$(grep -cF -- '--search="loom:epic:$EPIC_NUMBER:phase in:body"' "$CHAMPION_EPIC_MD" || true)
assert_eq "2" "$search_prefix_count" \
    "both call sites (Step 2.75 and Detecting Phase Completion) widen their --search query the same way"

assert_doc_lacks "$CHAMPION_EPIC_MD" \
    '--search="loom:epic:$EPIC_NUMBER:phase:$PHASE in:body"' \
    "neither call site still uses the old exact-literal-phase-suffix search that missed differently-formed markers"

echo
echo "--- Doc pins: phase-issue creation still emits only canonical numeric markers (verified unchanged, #6967 AC2) ---"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    '<!-- loom:epic:<epic-number>:phase:1 -->' \
    "Step 3 (Phase 1 creation) emits the canonical numeric marker"

assert_doc_contains "$CHAMPION_EPIC_MD" \
    '<!-- loom:epic:<epic-number>:phase:<N+1> -->' \
    "Creating Next Phase Issues emits the canonical numeric marker (N+1, never a letter form)"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
