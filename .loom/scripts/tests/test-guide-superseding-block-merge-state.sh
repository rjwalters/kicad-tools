#!/usr/bin/env bash
# test-guide-superseding-block-merge-state.sh - Regression test for issue #7267
#
# guide.md's `has_superseding_block()` (the "Superseding Block Check (#4634)"
# section) only checked whether a linked, still-OPEN PR carried
# `loom:changes-requested` or `loom:blocked`. `curator.md`'s equivalent "When
# Dependencies Complete" check is fuller: it also treats `mergeable ==
# "CONFLICTING"` / `mergeStateStatus` in (`DIRTY`, `CONFLICTING`) on that same
# PR as independently sufficient evidence of a superseding block, regardless
# of labels. Because the two checks diverged, Guide's automated "Unblocked"
# pass and Curator's dependency re-check disagreed and flip-flopped
# `loom:blocked` on issue #6925 against its own implementing PR #7246
# (`loom:pr`+`loom:operator`, `mergeable=CONFLICTING`/`mergeStateStatus=DIRTY`)
# — four times in ~4 hours.
#
# THE FIX: `has_superseding_block()` now also returns `true` when a linked
# OPEN PR carries `loom:operator` (a Champion merge-risk hold — a human
# decision is pending, so an autonomous "Unblocked" promotion that re-queues
# the issue for a brand-new Builder is premature regardless of mergeability),
# OR when its `mergeable`/`mergeStateStatus` indicates it cannot currently
# land (`CONFLICTING`/`DIRTY`).
#
# Verifies that:
#   1. STRUCTURE: `has_superseding_block()`'s `gh pr view` call requests
#      `mergeable,mergeStateStatus` (not just `state,labels`), and its label
#      check includes `loom:operator`.
#   2. THE REGRESSION, executed rather than grepped: `has_superseding_block()`
#      extracted VERBATIM from guide.md, run against fixture `gh` output,
#      covering every case in the issue's test plan:
#        a. PR open + CONFLICTING/DIRTY merge state, no blocking label -> true
#        b. PR open + loom:operator only (clean merge state) -> true
#        c. PR merged -> false (unchanged)
#        d. PR closed (not merged) -> false (unchanged)
#        e. PR open + MERGEABLE/CLEAN + no blocking label -> false (unchanged)
#        f. PR open + loom:changes-requested -> true (no regression of the
#           original #4634 case)
#        g. no linked PR at all -> false (unchanged)
#
# Hermetic: `gh` is stubbed with fixture JSON; only the real `jq` binary is
# invoked (skipped if unavailable) — no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout), falling back to
# the defaults/ source-tree path (a bare source checkout with no
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

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 1: has_superseding_block()'s gh pr view call fetches merge-state
# fields, and its label check covers loom:operator.
# ---------------------------------------------------------------------------
echo "Test 1: has_superseding_block() requests merge-state fields and checks loom:operator"

if grep -qE 'gh pr view "\$pr" --json state,labels,mergeable,mergeStateStatus' "$GUIDE_MD"; then
    pass "gh pr view requests state,labels,mergeable,mergeStateStatus"
else
    fail "expected gh pr view to request state,labels,mergeable,mergeStateStatus"
fi

FUNC_BODY_GREP="$(sed -n '/^has_superseding_block() {/,/^}/p' "$GUIDE_MD")"
if grep -q 'loom:operator' <<<"$FUNC_BODY_GREP"; then
    pass "has_superseding_block() label check includes loom:operator"
else
    fail "expected has_superseding_block() to check loom:operator"
fi

if grep -q 'CONFLICTING' <<<"$FUNC_BODY_GREP" && grep -q 'DIRTY' <<<"$FUNC_BODY_GREP"; then
    pass "has_superseding_block() checks mergeable/mergeStateStatus for CONFLICTING/DIRTY"
else
    fail "expected has_superseding_block() to check CONFLICTING/DIRTY merge state"
fi

# ---------------------------------------------------------------------------
# Extract has_superseding_block() VERBATIM from guide.md so this suite can
# never silently drift from the actual prompt text.
# ---------------------------------------------------------------------------
FUNC_BODY="$(sed -n '/^has_superseding_block() {/,/^}/p' "$GUIDE_MD")"

if [[ -z "$FUNC_BODY" ]]; then
    echo -e "${RED}FATAL${NC}: could not extract has_superseding_block() from guide.md"
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    exit 1
fi
pass "extracted has_superseding_block() verbatim from guide.md"

if ! command -v jq >/dev/null 2>&1; then
    echo ""
    echo "SKIP: jq not available, skipping the executable has_superseding_block() tests"
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

# Load the extracted function into THIS shell.
eval "$FUNC_BODY"

# ---------------------------------------------------------------------------
# Fixture: a stub `gh` mapping issue numbers to their linked PR(s), and PR
# numbers to their state/labels/mergeable/mergeStateStatus.
#
#   #6925 -> PR #7246: OPEN, loom:pr+loom:operator, CONFLICTING/DIRTY
#            (the exact live #6925/#7246 case from the issue)
#   #100   -> PR #200: OPEN, no blocking label, MERGEABLE/CLEAN -> false
#   #101   -> PR #201: OPEN, loom:changes-requested, MERGEABLE/CLEAN
#            (the original #4634 case -- must still be true)
#   #102   -> PR #202: OPEN, no blocking label, CONFLICTING mergeable
#   #103   -> PR #203: OPEN, no blocking label, MERGEABLE/DIRTY mergeStateStatus
#   #104   -> PR #204: OPEN, loom:operator only, MERGEABLE/CLEAN
#   #105   -> PR #205: MERGED
#   #106   -> PR #206: CLOSED (not merged)
#   #107   -> (no linked PR)
# ---------------------------------------------------------------------------
gh() {
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        local number="$3"
        case "$number" in
            6925) echo "7246" ;;
            100) echo "200" ;;
            101) echo "201" ;;
            102) echo "202" ;;
            103) echo "203" ;;
            104) echo "204" ;;
            105) echo "205" ;;
            106) echo "206" ;;
            107) : ;;  # no linked PR
            *) : ;;
        esac
        return 0
    fi
    if [[ "$1" == "pr" && "$2" == "view" ]]; then
        local pr="$3" state labels mergeable merge_state
        case "$pr" in
            7246) state="OPEN"; labels='"loom:pr","loom:operator"'; mergeable="CONFLICTING"; merge_state="DIRTY" ;;
            200)  state="OPEN"; labels='"loom:pr"'; mergeable="MERGEABLE"; merge_state="CLEAN" ;;
            201)  state="OPEN"; labels='"loom:changes-requested"'; mergeable="MERGEABLE"; merge_state="CLEAN" ;;
            202)  state="OPEN"; labels='"loom:pr"'; mergeable="CONFLICTING"; merge_state="UNSTABLE" ;;
            203)  state="OPEN"; labels='"loom:pr"'; mergeable="MERGEABLE"; merge_state="DIRTY" ;;
            204)  state="OPEN"; labels='"loom:pr","loom:operator"'; mergeable="MERGEABLE"; merge_state="CLEAN" ;;
            205)  state="MERGED"; labels='"loom:pr"'; mergeable="MERGEABLE"; merge_state="CLEAN" ;;
            206)  state="CLOSED"; labels=""; mergeable="UNKNOWN"; merge_state="UNKNOWN" ;;
            *)    state=""; labels=""; mergeable=""; merge_state="" ;;
        esac
        local label_json=""
        if [[ -n "$labels" ]]; then
            IFS=',' read -ra parts <<<"$labels"
            for p in "${parts[@]}"; do
                label_json+="{\"name\":${p}},"
            done
            label_json="${label_json%,}"
        fi
        printf '{"state":"%s","labels":[%s],"mergeable":"%s","mergeStateStatus":"%s"}\n' \
            "$state" "$label_json" "$mergeable" "$merge_state"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Test 2: the live #6925/#7246 case -- loom:operator + CONFLICTING/DIRTY
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: the live #6925/#7246 case (loom:pr+loom:operator, CONFLICTING/DIRTY)"

assert_eq "$(has_superseding_block 6925)" "true" \
    "#6925 (linked PR #7246 open, loom:operator + CONFLICTING/DIRTY) is a superseding block"

# ---------------------------------------------------------------------------
# Test 3: unchanged cases (no regression)
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: unchanged cases"

assert_eq "$(has_superseding_block 100)" "false" \
    "#100 (linked PR open, no blocking label, MERGEABLE/CLEAN) is NOT a superseding block"
assert_eq "$(has_superseding_block 101)" "true" \
    "#101 (linked PR open, loom:changes-requested) is a superseding block (#4634 regression check)"
assert_eq "$(has_superseding_block 105)" "false" \
    "#105 (linked PR MERGED) is NOT a superseding block"
assert_eq "$(has_superseding_block 106)" "false" \
    "#106 (linked PR CLOSED, not merged) is NOT a superseding block"
assert_eq "$(has_superseding_block 107)" "false" \
    "#107 (no linked PR) is NOT a superseding block"

# ---------------------------------------------------------------------------
# Test 4: new cases -- merge-state check independent of labels
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: merge-state check (independent of labels)"

assert_eq "$(has_superseding_block 102)" "true" \
    "#102 (linked PR open, no blocking label, mergeable=CONFLICTING) is a superseding block"
assert_eq "$(has_superseding_block 103)" "true" \
    "#103 (linked PR open, no blocking label, mergeStateStatus=DIRTY) is a superseding block"

# ---------------------------------------------------------------------------
# Test 5: new case -- loom:operator alone (clean merge state) is sufficient
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: loom:operator label check (independent of merge state)"

assert_eq "$(has_superseding_block 104)" "true" \
    "#104 (linked PR open, loom:operator only, MERGEABLE/CLEAN) is a superseding block"

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
