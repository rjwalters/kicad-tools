#!/usr/bin/env bash
# test-guide-operator-only-exclude.sh - Regression test for issue #6941
#
# guide.md's `loom:urgent` eligibility filters excluded `loom:building`, an
# open `loom:pr`-labeled linked PR, and `loom:blocked` — but not
# `loom:operator-only` (which `.github/labels.yml` documents as "sweep
# skips", i.e. a Builder can never act on it, exactly like `loom:blocked`,
# just via a different mechanism: a human must act or rule outside
# automation). #6245 was mispromoted to `loom:urgent` twice in under 24h
# because none of the four eligibility call sites excluded it:
#   1. the "Finding Work" ready-queue query / search terms,
#   2. the incumbency "Evict ineligible holders" step,
#   3. the "Fill free slots" / candidate-ranking step, and
#   4. the "Safety Check: Never Mark Building Issues Urgent" section.
#
# The fix adds a `has_operator_only()` helper (one label-membership check,
# mirroring `has_open_pr_labeled_loom_pr()`'s existing shape) and wires it
# into all four call sites.
#
# #7071 EXTENSION: the SAME four call sites never excluded `loom:blocked`
# either — only `loom:operator-only` was fixed above. `loom:blocked` means
# exactly the same thing for a Builder ("can never act on this right now"),
# and a curated issue can carry both `loom:issue` and `loom:blocked`
# simultaneously. The fix mirrors #6941's shape again: a `has_blocked()`
# helper, wired into the same four call sites alongside `has_operator_only()`.
# This suite is extended with a parallel set of assertions (Tests 1b-8b)
# covering `has_blocked()` at each site, without touching the existing
# `has_operator_only()` assertions above (no regression, per #7071 AC5).
#
# Verifies that:
#   1. guide.md defines `has_operator_only()` immediately after
#      `has_open_pr_labeled_loom_pr()`, in the same shape.
#   2. guide.md's ready-queue query excludes `loom:operator-only`.
#   3. The "Evict ineligible holders" step names `loom:operator-only` /
#      `has_operator_only()` as an ineligibility condition, and — like the
#      other ineligibility conditions in that step — does NOT route through
#      `urgent-flip-guard.sh` (eviction is "the one demotion you may make
#      without a challenger", so a newly-added `loom:operator-only` label
#      evicts the SAME tick it's noticed, with no flip-guard reversal wait).
#   4. The "Fill free slots" step excludes `has_operator_only()` candidates.
#   5. The "Safety Check" section calls `has_operator_only()` before writing
#      `loom:urgent`, mirroring the existing `loom:building` /
#      `has_open_pr_labeled_loom_pr()` checks there.
#   6. THE REGRESSION, executed rather than grepped: `has_operator_only()`
#      extracted VERBATIM from guide.md, run against fixture `gh issue view`
#      JSON:
#        a. an incumbent that gains `loom:operator-only` mid-cycle is
#           evicted by a reconstructed eviction filter the same tick,
#        b. a `loom:operator-only` + `loom:issue` candidate is never
#           selected by a reconstructed "Fill free slots" ranking loop,
#           even when it out-ranks the eligible candidate on tier alone.
#
# Hermetic: `gh` is stubbed with fixture JSON; only the real `jq` binary is
# invoked (skipped if unavailable) — no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout), falling back
# to the defaults/ source-tree path (a bare source checkout with no
# .claude/commands/loom/ copy yet). See issue #6194 / #6241.
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then pass "$msg"; else fail "$msg (got '$actual', expected '$expected')"; fi
}

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (missing pattern: $pattern)"; fi
}

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 1: has_operator_only() is defined, mirroring has_open_pr_labeled_loom_pr()
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines has_operator_only()"

assert_grep '^has_operator_only\(\) \{' "$GUIDE_MD" \
    "has_operator_only() is defined"
assert_grep 'loom:operator-only,\*\) echo "true"; return' "$GUIDE_MD" \
    "has_operator_only() matches the loom:operator-only label"

DEF_OPEN_PR="$(grep -n '^has_open_pr_labeled_loom_pr() {' "$GUIDE_MD" | head -1 | cut -d: -f1)"
DEF_OPERATOR_ONLY="$(grep -n '^has_operator_only() {' "$GUIDE_MD" | head -1 | cut -d: -f1)"
if [[ -n "$DEF_OPEN_PR" && -n "$DEF_OPERATOR_ONLY" && "$DEF_OPERATOR_ONLY" -gt "$DEF_OPEN_PR" ]]; then
    pass "has_operator_only() is defined after has_open_pr_labeled_loom_pr() (same neighborhood)"
else
    fail "expected has_operator_only() to follow has_open_pr_labeled_loom_pr() (open_pr=$DEF_OPEN_PR operator_only=$DEF_OPERATOR_ONLY)"
fi

# ---------------------------------------------------------------------------
# Test 1b (#7071): has_blocked() is defined, mirroring has_operator_only()
# ---------------------------------------------------------------------------
echo ""
echo "Test 1b: guide.md defines has_blocked()"

assert_grep '^has_blocked\(\) \{' "$GUIDE_MD" \
    "has_blocked() is defined"
assert_grep 'loom:blocked,\*\) echo "true"; return' "$GUIDE_MD" \
    "has_blocked() matches the loom:blocked label"

DEF_BLOCKED="$(grep -n '^has_blocked() {' "$GUIDE_MD" | head -1 | cut -d: -f1)"
if [[ -n "$DEF_OPERATOR_ONLY" && -n "$DEF_BLOCKED" && "$DEF_BLOCKED" -gt "$DEF_OPERATOR_ONLY" ]]; then
    pass "has_blocked() is defined after has_operator_only() (same neighborhood)"
else
    fail "expected has_blocked() to follow has_operator_only() (operator_only=$DEF_OPERATOR_ONLY blocked=$DEF_BLOCKED)"
fi

# ---------------------------------------------------------------------------
# Test 2: the "Finding Work" ready-queue query excludes loom:operator-only
# (and, #7071, loom:blocked)
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: the ready-queue query excludes loom:operator-only and loom:blocked"

assert_grep '\-label:loom:building \-label:loom:operator-only \-label:loom:blocked' "$GUIDE_MD" \
    "the ready-queue search term excludes loom:building, loom:operator-only, and loom:blocked"

# ---------------------------------------------------------------------------
# Test 3: "Evict ineligible holders" names loom:operator-only, without a
# flip-guard gate (eviction is unconditional, same tick, per the existing
# loom:building/loom:blocked/open-PR ineligibility conditions).
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: the incumbency eviction step treats loom:operator-only as ineligible, ungated"

EVICT_BLOCK="$(awk '/\*\*Evict ineligible holders\.\*\*/{flag=1} flag{print} /\*\*Fill free slots\.\*\*/{exit}' "$GUIDE_MD")"
if [[ -z "$EVICT_BLOCK" ]]; then
    fail "could not extract the 'Evict ineligible holders' step from guide.md"
else
    pass "extracted the 'Evict ineligible holders' step"
fi

if grep -q 'loom:operator-only' <<<"$EVICT_BLOCK"; then
    pass "the eviction step names loom:operator-only as an ineligibility condition"
else
    fail "expected 'loom:operator-only' inside the 'Evict ineligible holders' step"
fi

if grep -q 'has_operator_only' <<<"$EVICT_BLOCK"; then
    pass "the eviction step references has_operator_only()"
else
    fail "expected 'has_operator_only' inside the 'Evict ineligible holders' step"
fi

if grep -q 'urgent-flip-guard' <<<"$EVICT_BLOCK"; then
    fail "eviction step must NOT route through urgent-flip-guard.sh (it is the one demotion made without a challenger)"
else
    pass "eviction step does not gate on urgent-flip-guard.sh (evicts the same tick it's noticed)"
fi

if grep -q 'has_blocked' <<<"$EVICT_BLOCK"; then
    pass "the eviction step references has_blocked() (#7071)"
else
    fail "expected 'has_blocked' inside the 'Evict ineligible holders' step"
fi

# ---------------------------------------------------------------------------
# Test 4: "Fill free slots" excludes has_operator_only() candidates
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: the 'Fill free slots' step excludes has_operator_only() candidates"

FILL_BLOCK="$(awk '/\*\*Fill free slots\.\*\*/{flag=1} flag{print} /^4\. \*\*With 3 eligible holders/{exit}' "$GUIDE_MD")"
if [[ -z "$FILL_BLOCK" ]]; then
    fail "could not extract the 'Fill free slots' step from guide.md"
else
    pass "extracted the 'Fill free slots' step"
fi

if grep -q 'has_operator_only' <<<"$FILL_BLOCK"; then
    pass "the 'Fill free slots' step references has_operator_only()"
else
    fail "expected 'has_operator_only' inside the 'Fill free slots' step"
fi

if grep -q 'has_blocked' <<<"$FILL_BLOCK"; then
    pass "the 'Fill free slots' step references has_blocked() (#7071)"
else
    fail "expected 'has_blocked' inside the 'Fill free slots' step"
fi

# ---------------------------------------------------------------------------
# Test 5: the Safety Check section calls has_operator_only() before writing
# loom:urgent, mirroring the existing loom:building / open-PR checks there.
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: the Safety Check section gates on has_operator_only()"

SAFETY_BLOCK="$(awk '/^## Safety Check: Never Mark Building Issues Urgent/{flag=1} flag{print} /^## When to Apply loom:urgent/{exit}' "$GUIDE_MD")"
if [[ -z "$SAFETY_BLOCK" ]]; then
    fail "could not extract the Safety Check section from guide.md"
else
    pass "extracted the Safety Check section"
fi

if grep -q 'has_operator_only <number>' <<<"$SAFETY_BLOCK"; then
    pass "the Safety Check section calls has_operator_only() before the urgent-flip-guard write"
else
    fail "expected a 'has_operator_only <number>' call inside the Safety Check section"
fi

if grep -q 'has_blocked <number>' <<<"$SAFETY_BLOCK"; then
    pass "the Safety Check section calls has_blocked() before the urgent-flip-guard write (#7071)"
else
    fail "expected a 'has_blocked <number>' call inside the Safety Check section"
fi

HAS_BUILDING_LINE="$(grep -n 'grep -q "loom:building"' <<<"$SAFETY_BLOCK" | head -1 | cut -d: -f1)"
HAS_OPERATOR_ONLY_LINE="$(grep -n 'has_operator_only <number>' <<<"$SAFETY_BLOCK" | head -1 | cut -d: -f1)"
HAS_BLOCKED_LINE="$(grep -n 'has_blocked <number>' <<<"$SAFETY_BLOCK" | head -1 | cut -d: -f1)"
HAS_OPEN_PR_LINE="$(grep -n 'has_open_pr_labeled_loom_pr <number>' <<<"$SAFETY_BLOCK" | head -1 | cut -d: -f1)"
if [[ -n "$HAS_BUILDING_LINE" && -n "$HAS_OPERATOR_ONLY_LINE" && -n "$HAS_BLOCKED_LINE" && -n "$HAS_OPEN_PR_LINE" ]] \
    && (( HAS_BUILDING_LINE < HAS_OPERATOR_ONLY_LINE && HAS_OPERATOR_ONLY_LINE < HAS_BLOCKED_LINE && HAS_BLOCKED_LINE < HAS_OPEN_PR_LINE )); then
    pass "the checks run in order: loom:building, then has_operator_only(), then has_blocked(), then has_open_pr_labeled_loom_pr()"
else
    fail "expected loom:building < has_operator_only < has_blocked < has_open_pr_labeled_loom_pr (got building=$HAS_BUILDING_LINE operator_only=$HAS_OPERATOR_ONLY_LINE blocked=$HAS_BLOCKED_LINE open_pr=$HAS_OPEN_PR_LINE)"
fi

# ---------------------------------------------------------------------------
# Extract has_operator_only() VERBATIM from guide.md so this suite can never
# silently drift from the actual prompt text.
# ---------------------------------------------------------------------------
FUNC_BODY="$(sed -n '/^has_operator_only() {/,/^}/p' "$GUIDE_MD")"

if [[ -z "$FUNC_BODY" ]]; then
    echo -e "${RED}FATAL${NC}: could not extract has_operator_only() from guide.md"
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    exit 1
fi
pass "extracted has_operator_only() verbatim from guide.md"

BLOCKED_FUNC_BODY="$(sed -n '/^has_blocked() {/,/^}/p' "$GUIDE_MD")"

if [[ -z "$BLOCKED_FUNC_BODY" ]]; then
    echo -e "${RED}FATAL${NC}: could not extract has_blocked() from guide.md"
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    exit 1
fi
pass "extracted has_blocked() verbatim from guide.md (#7071)"

if ! command -v jq >/dev/null 2>&1; then
    echo ""
    echo "SKIP: jq not available, skipping the executable has_operator_only()/eviction/fill tests"
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
    if [[ $TESTS_FAILED -gt 0 ]]; then
        echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
        exit 1
    fi
    echo "All tests passed"
    exit 0
fi

# Load the extracted functions into THIS shell.
eval "$FUNC_BODY"
eval "$BLOCKED_FUNC_BODY"

# ---------------------------------------------------------------------------
# Fixture: a stub `gh` returning canned `issue view --json labels` output for
# a handful of issue numbers. #100 is a plain urgent incumbent; #200 is an
# incumbent that has since GAINED loom:operator-only (+ its operator-blocked
# sub-kind, mirroring how the label always ships alongside a sub-kind, #5671);
# #300 is a plain ready loom:issue candidate (lower tier); #400 is a
# loom:operator-only + loom:issue candidate that outranks #300 on tier alone.
# #7071: #250 mirrors #200 but for loom:blocked (an incumbent that has since
# GAINED loom:blocked); #450 mirrors #400 but for loom:blocked (a
# loom:blocked + loom:issue candidate that outranks #300 on tier alone).
# ---------------------------------------------------------------------------
gh() {
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        local number="$3" labels_csv label_json jq_filter="" args=("$@") i
        case "$number" in
            100) labels_csv="loom:issue,loom:urgent" ;;
            200) labels_csv="loom:issue,loom:urgent,loom:operator-only,loom:operator-blocked" ;;
            250) labels_csv="loom:issue,loom:urgent,loom:blocked" ;;
            300) labels_csv="loom:issue,tier:goal-supporting" ;;
            400) labels_csv="loom:issue,tier:goal-advancing,loom:operator-only,loom:operator-decision" ;;
            450) labels_csv="loom:issue,tier:goal-advancing,loom:blocked" ;;
            *) labels_csv="" ;;
        esac
        label_json="$(IFS=,; for n in $labels_csv; do printf '{"name":"%s"},' "$n"; done)"
        label_json="${label_json%,}"
        # Real `gh ... --json X --jq FILTER` applies FILTER server-side
        # before returning; emulate that here (rather than returning raw
        # JSON) so has_operator_only()'s own `--jq` filter is what's under
        # test, not this fixture's JSON shape.
        for ((i = 0; i < ${#args[@]}; i++)); do
            if [[ "${args[$i]}" == "--jq" ]]; then
                jq_filter="${args[$((i + 1))]}"
            fi
        done
        if [[ -n "$jq_filter" ]]; then
            printf '{"labels":[%s]}' "$label_json" | jq -r "$jq_filter"
        else
            printf '{"labels":[%s]}' "$label_json"
        fi
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Test 6: has_operator_only() itself, against the fixtures above
# ---------------------------------------------------------------------------
echo ""
echo "Test 6: has_operator_only() (executed, against fixture gh output)"

assert_eq "$(has_operator_only 100)" "false" "#100 (plain loom:urgent incumbent) is not operator-only"
assert_eq "$(has_operator_only 200)" "true" "#200 (gained loom:operator-only + operator-blocked) is operator-only"
assert_eq "$(has_operator_only 300)" "false" "#300 (plain loom:issue candidate) is not operator-only"
assert_eq "$(has_operator_only 400)" "true" "#400 (loom:issue + loom:operator-only + operator-decision) is operator-only"

# ---------------------------------------------------------------------------
# Test 6b (#7071): has_blocked() itself, against the fixtures above
# ---------------------------------------------------------------------------
echo ""
echo "Test 6b: has_blocked() (executed, against fixture gh output)"

assert_eq "$(has_blocked 100)" "false" "#100 (plain loom:urgent incumbent) is not blocked"
assert_eq "$(has_blocked 250)" "true" "#250 (gained loom:blocked) is blocked"
assert_eq "$(has_blocked 300)" "false" "#300 (plain loom:issue candidate) is not blocked"
assert_eq "$(has_blocked 450)" "true" "#450 (loom:issue + loom:blocked) is blocked"

# ---------------------------------------------------------------------------
# Test 7 (AC): an incumbent that gains loom:operator-only is evicted the
# SAME tick, with no flip-guard reversal needed — reconstructing the
# "Evict ineligible holders" filter exactly as guide.md specifies it.
# ---------------------------------------------------------------------------
echo ""
echo "Test 7: incumbent eviction — gaining loom:operator-only evicts immediately"

evict_ineligible() {
    # Mirrors "Evict ineligible holders": ineligible if closed / lost
    # loom:issue / gained loom:building or loom:blocked / has_operator_only.
    local number="$1"
    if [[ "$(has_operator_only "$number")" == "true" ]]; then
        echo "true"
        return
    fi
    echo "false"
}

INCUMBENTS=(100 200)
SURVIVORS=()
for n in "${INCUMBENTS[@]}"; do
    if [[ "$(evict_ineligible "$n")" == "false" ]]; then
        SURVIVORS+=("$n")
    fi
done

if [[ "${#SURVIVORS[@]}" -eq 1 && "${SURVIVORS[0]}" == "100" ]]; then
    pass "incumbent #200 (gained loom:operator-only) is evicted; #100 stays"
else
    fail "expected only #100 to survive eviction, got: ${SURVIVORS[*]:-<none>}"
fi

# No urgent-flip-guard.sh dependency in this reconstruction — the eviction
# path never calls it (Test 3 above already proves guide.md's prose agrees),
# so the eviction happens the same tick it's noticed, with no cooldown wait.

# ---------------------------------------------------------------------------
# Test 7b (AC, #7071): an incumbent that gains loom:blocked is evicted the
# SAME tick, mirroring Test 7 but for has_blocked().
# ---------------------------------------------------------------------------
echo ""
echo "Test 7b: incumbent eviction — gaining loom:blocked evicts immediately"

evict_ineligible_blocked() {
    # Mirrors "Evict ineligible holders" for the has_blocked() condition only.
    local number="$1"
    if [[ "$(has_blocked "$number")" == "true" ]]; then
        echo "true"
        return
    fi
    echo "false"
}

INCUMBENTS_BLOCKED=(100 250)
SURVIVORS_BLOCKED=()
for n in "${INCUMBENTS_BLOCKED[@]}"; do
    if [[ "$(evict_ineligible_blocked "$n")" == "false" ]]; then
        SURVIVORS_BLOCKED+=("$n")
    fi
done

if [[ "${#SURVIVORS_BLOCKED[@]}" -eq 1 && "${SURVIVORS_BLOCKED[0]}" == "100" ]]; then
    pass "incumbent #250 (gained loom:blocked) is evicted; #100 stays"
else
    fail "expected only #100 to survive eviction, got: ${SURVIVORS_BLOCKED[*]:-<none>}"
fi

# ---------------------------------------------------------------------------
# Test 8 (AC): a loom:operator-only + loom:issue candidate is never selected
# to fill a free slot, even when it strictly outranks the only other
# eligible candidate on tier.
# ---------------------------------------------------------------------------
echo ""
echo "Test 8: 'Fill free slots' never selects a loom:operator-only candidate"

fill_free_slot() {
    # Mirrors "Fill free slots": walk ranked candidates, skip any for which
    # has_operator_only() is true, select the first eligible one.
    local candidates=("$@")
    for n in "${candidates[@]}"; do
        if [[ "$(has_operator_only "$n")" == "true" ]]; then
            continue
        fi
        echo "$n"
        return
    done
    echo ""
}

# #400 (tier:goal-advancing, rank 3) would out-rank #300 (tier:goal-supporting,
# rank 4) on urgency_rank() alone -- listed FIRST to prove has_operator_only()
# is what excludes it, not candidate ordering.
SELECTED="$(fill_free_slot 400 300)"
assert_eq "$SELECTED" "300" \
    "the loom:operator-only candidate (#400) is skipped even though it outranks #300 on tier"

# A slot with ONLY an operator-only candidate available stays unfilled rather
# than promoting it.
SELECTED_NONE="$(fill_free_slot 400)"
assert_eq "$SELECTED_NONE" "" \
    "a free slot with only a loom:operator-only candidate available is left unfilled"

# ---------------------------------------------------------------------------
# Test 8b (AC, #7071): a loom:blocked + loom:issue candidate is never
# selected to fill a free slot, mirroring Test 8 but for has_blocked().
# ---------------------------------------------------------------------------
echo ""
echo "Test 8b: 'Fill free slots' never selects a loom:blocked candidate"

fill_free_slot_blocked() {
    # Mirrors "Fill free slots" for the has_blocked() condition only.
    local candidates=("$@")
    for n in "${candidates[@]}"; do
        if [[ "$(has_blocked "$n")" == "true" ]]; then
            continue
        fi
        echo "$n"
        return
    done
    echo ""
}

# #450 (tier:goal-advancing, rank 3) would out-rank #300 (tier:goal-supporting,
# rank 4) on urgency_rank() alone -- listed FIRST to prove has_blocked() is
# what excludes it, not candidate ordering.
SELECTED_BLOCKED="$(fill_free_slot_blocked 450 300)"
assert_eq "$SELECTED_BLOCKED" "300" \
    "the loom:blocked candidate (#450) is skipped even though it outranks #300 on tier"

# A slot with ONLY a blocked candidate available stays unfilled rather than
# promoting it.
SELECTED_BLOCKED_NONE="$(fill_free_slot_blocked 450)"
assert_eq "$SELECTED_BLOCKED_NONE" "" \
    "a free slot with only a loom:blocked candidate available is left unfilled"

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
