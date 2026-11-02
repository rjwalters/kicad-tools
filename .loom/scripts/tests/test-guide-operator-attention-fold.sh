#!/usr/bin/env bash
# test-guide-operator-attention-fold.sh - Regression test for issues #5930, #6457
#
# PR #5892 (closing #5890) added a debounce so the *machine-generated* region
# of WORK_PLAN.md (between `<!-- guide:plan-body:start -->` / `:end -->`) is
# only rewritten once per hour even if Ready/Urgent/In-Progress differ. That
# fixed churn from label-bounce regeneration, but WORK_PLAN.md ALSO carried a
# hand-written "Operator Attention: Merge-Risk-Hold Pileup" section ABOVE the
# markers, updated by *appending* a fresh `**Update (... UTC)**:` paragraph
# every tick instead of being rewritten. Each such edit is real content (the
# paragraph text always differs — new timestamp, new prose), so
# `git diff --cached --quiet` in `create_docs_pr()` always found something to
# commit — the #5890 debounce never even ran for this case, since it only
# ever guarded `render_plan_body()`'s output. Net effect: ~30
# "docs: Guide document maintenance update" PRs merged in one day, driven
# almost entirely by this ever-growing narrative section.
#
# The fix folds the section INTO `render_plan_body()` as its first generated
# subsection (deterministically rendered from current `loom:operator` PR
# membership, no timestamps or freeform prose), so it rides the SAME
# byte-for-byte `new_body == old_body` comparison and the SAME
# `LOOM_WORK_PLAN_DEBOUNCE_SECS` gate as every other section — no separate
# mechanism, and nothing left to hand-append to.
#
# Verifies that:
#   1. guide.md's render_plan_body() queries `loom:operator` PRs into a
#      `held` variable and renders an "Operator Attention: Merge-Risk-Hold
#      Pileup" section from it, as the FIRST section emitted (ahead of
#      "Very Important") — so it is exactly as (non-)volatile as everything else in
#      the generated region.
#   2. guide.md explicitly documents the fix and prohibits reintroducing a
#      hand-appended narrative paragraph (the #5930 regression marker).
#   3. The committed WORK_PLAN.md carries the section INSIDE the markers, not
#      above them, and contains no leftover `**Update (` append-log residue
#      above the markers.
#   4. #6457: `$held` is derived FROM `$approved`'s already-fetched PR list
#      (filtered on hold-label membership) rather than issuing a second,
#      independently-cached/independently-search-indexed
#      `gh pr list --label "loom:operator"` query — so the "Operator
#      Attention" and "Approved (Awaiting Merge)" sections (and the Backlog
#      Balance count derived from `$held`) can no longer disagree about
#      which open `loom:pr` PRs are held, even under cache-age skew or
#      GitHub search-index lag on a just-applied label. The jq filter is
#      EXTRACTED from guide.md and executed against a fixture (#13384), so it
#      tests the shipped filter rather than a copy.
#
# Hermetic: grep plus the real extracted jq filter against fixture data. No forge,
# network, or `gh` calls.

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
WORK_PLAN_MD="$REPO_ROOT/WORK_PLAN.md"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then pass "$msg"; else fail "$msg (got '$actual', expected '$expected')"; fi
}

assert_not_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then fail "$msg (unexpected match: $pattern)"; else pass "$msg"; fi
}

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi
if [[ ! -f "$WORK_PLAN_MD" ]]; then
    echo -e "${RED}FATAL${NC}: WORK_PLAN.md not found at $WORK_PLAN_MD"
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 1: render_plan_body() folds the Operator Attention section in, first
# ---------------------------------------------------------------------------
echo "Test 1: guide.md folds Operator Attention into render_plan_body()"

assert_grep 'held=\$\(printf .%s. "\$approved_json" \| jq -r .\.\[\] \| select' "$GUIDE_MD" \
    "render_plan_body() derives \$held from \$approved_json (#6457) rather than a second live query"
assert_not_grep '\$GH_READ" pr list --label "loom:operator"' "$GUIDE_MD" \
    "render_plan_body() no longer issues a separate 'gh pr list --label loom:operator' query (#6457)"
assert_grep '"Operator Attention: Merge-Risk-Hold Pileup"' "$GUIDE_MD" \
    "guide.md renders an Operator Attention: Merge-Risk-Hold Pileup section"

RPB_BODY="$(awk '/^render_plan_body\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"
if [[ -z "$RPB_BODY" ]]; then
    fail "could not extract render_plan_body() body from guide.md"
else
    pass "render_plan_body() body extracted"
fi

OPERATOR_LINE="$(grep -n 'section "Operator Attention: Merge-Risk-Hold Pileup"' <<<"$RPB_BODY" | head -1 | cut -d: -f1)"
STARRED_LINE="$(grep -n 'section "Very Important"' <<<"$RPB_BODY" | head -1 | cut -d: -f1)"
if [[ -n "$OPERATOR_LINE" && -n "$STARRED_LINE" && "$OPERATOR_LINE" -lt "$STARRED_LINE" ]]; then
    pass "the Operator Attention section is emitted BEFORE the Very Important section"
else
    fail "expected the Operator Attention section call to precede the Very Important section call (operator=$OPERATOR_LINE, starred=$STARRED_LINE)"
fi

# ---------------------------------------------------------------------------
# Test 2: guide.md documents the fix and forbids the append pattern
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: guide.md documents the fix (won't silently regress)"

assert_grep '#5930' "$GUIDE_MD" \
    "guide.md references #5930"
assert_grep '(hand-appended|hand-written narrative)' "$GUIDE_MD" \
    "guide.md calls out the hand-appended/hand-written narrative anti-pattern by name"

# ---------------------------------------------------------------------------
# Test 3: the committed WORK_PLAN.md carries the section INSIDE the markers
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: WORK_PLAN.md's Operator Attention section lives inside the markers"

ABOVE_MARKERS="$(awk '/<!-- guide:plan-body:start -->/{exit} {print}' "$WORK_PLAN_MD")"
INSIDE_MARKERS="$(sed -n '/<!-- guide:plan-body:start -->/,/<!-- guide:plan-body:end -->/p' "$WORK_PLAN_MD")"

if grep -q '## Operator Attention' <<<"$ABOVE_MARKERS"; then
    fail "found a '## Operator Attention' heading ABOVE the markers (the old hand-written location)"
else
    pass "no '## Operator Attention' heading above the markers"
fi

if grep -q '## Operator Attention' <<<"$INSIDE_MARKERS"; then
    pass "'## Operator Attention' heading found INSIDE the generated markers"
else
    fail "expected an '## Operator Attention' heading inside the generated markers"
fi

if grep -qE '\*\*Update \(' <<<"$ABOVE_MARKERS"; then
    fail "found leftover '**Update (...)**:' append-log residue above the markers"
else
    pass "no leftover append-log residue above the markers"
fi

# ---------------------------------------------------------------------------
# Test 4 (#6457): $held is derived from $approved's fetched PR list, so the
# "Operator Attention" and "Approved (Awaiting Merge)" sections can no
# longer disagree about which open loom:pr PRs are held — even when a second,
# independently-cached/independently-search-indexed
# `gh pr list --label "loom:operator"` query would have returned stale or
# lagging results for a PR labeled `loom:operator` moments earlier.
#
# The jq program under test is EXTRACTED from the shipped render_plan_body()
# in guide.md and executed against a fixture — not re-typed here — so a
# change to the real filter changes this test's result (#13384).
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: guide.md's real \$held filter derives held PRs from \$approved's list (#6457)"

if command -v jq >/dev/null 2>&1; then
    HELD_LINE="$(grep -E '^[[:space:]]*held=\$\(printf .%s. "\$approved_json" \| jq -r ' <<<"$RPB_BODY" | head -1)"
    HELD_JQ=""
    if [[ "$HELD_LINE" =~ jq\ -r\ \'(.*)\'\)[[:space:]]*$ ]]; then
        HELD_JQ="${BASH_REMATCH[1]}"
    fi

    if [[ -z "$HELD_JQ" ]]; then
        fail "could not extract the jq program from guide.md's held= derivation (line: '$HELD_LINE')"
    else
        pass "extracted the real held= jq program from guide.md"

        # Two PRs carry loom:operator (one just labeled moments before this
        # "tick"), one carries loom:blocked, and one carries neither.
        APPROVED_JSON_FIXTURE='[
          {"number":6333,"title":"Test PR A","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]},
          {"number":6325,"title":"Test PR B","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]},
          {"number":7777,"title":"Test PR D","labels":[{"name":"loom:pr"},{"name":"loom:blocked"}]},
          {"number":9999,"title":"Test PR C","labels":[{"name":"loom:pr"}]}
        ]'

        HELD_DERIVED="$(printf '%s' "$APPROVED_JSON_FIXTURE" | jq -r "$HELD_JQ" 2>&1)"

        if grep -q '#6333' <<<"$HELD_DERIVED" && grep -q '#6325' <<<"$HELD_DERIVED"; then
            pass "both loom:operator-labeled PRs are derived into \$held from the same fetch as \$approved"
        else
            fail "expected both #6333 and #6325 in \$held derived from \$approved_json (got: $HELD_DERIVED)"
        fi

        if grep -q '#7777' <<<"$HELD_DERIVED"; then
            pass "a loom:blocked PR is derived into \$held by the real filter"
        else
            fail "expected #7777 (loom:blocked) in \$held derived from \$approved_json (got: $HELD_DERIVED)"
        fi

        if grep -q '#9999' <<<"$HELD_DERIVED"; then
            fail "PR #9999 (loom:pr only, no hold label) unexpectedly appeared in derived \$held"
        else
            pass "a loom:pr-only PR is correctly excluded from derived \$held"
        fi

        HELD_COUNT="$([ -z "$HELD_DERIVED" ] && printf '0' || printf '%s\n' "$HELD_DERIVED" | grep -c '^- ')"
        assert_eq "$HELD_COUNT" "3" "the real filter yields the expected held-count (3)"
    fi
else
    echo "  SKIP: jq not available, skipping Test 4's executable derivation check"
fi

# ---------------------------------------------------------------------------
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
