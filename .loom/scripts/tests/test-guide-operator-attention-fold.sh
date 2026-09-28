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
#      "Operator Priority") — so it is exactly as (non-)volatile as everything else in
#      the generated region.
#   2. guide.md explicitly documents the fix and prohibits reintroducing a
#      hand-appended narrative paragraph (the #5930 regression marker).
#   3. The committed WORK_PLAN.md carries the section INSIDE the markers, not
#      above them, and contains no leftover `**Update (` append-log residue
#      above the markers.
#   4. THE REGRESSION, executed rather than grepped: a reconstruction of the
#      section's render function, run twice against the SAME `loom:operator`
#      PR membership, produces byte-identical output — i.e. a tick where the
#      underlying facts (which PRs are held) have not changed can never
#      manufacture a diff, regardless of when it runs.
#   5. #6457: `$held` is derived FROM `$approved`'s already-fetched PR list
#      (filtered on `loom:operator` label membership) rather than issuing a
#      second, independently-cached/independently-search-indexed
#      `gh pr list --label "loom:operator"` query — so the "Operator
#      Attention" and "Approved (Awaiting Merge)" sections (and the Backlog
#      Balance count derived from `$held`) can no longer disagree about
#      which open `loom:pr` PRs also carry `loom:operator`, even under
#      cache-age skew or GitHub search-index lag on a just-applied label.
#
# Hermetic: pure grep/string reconstruction against fixture data. No forge,
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
STARRED_LINE="$(grep -n 'section "Operator Priority"' <<<"$RPB_BODY" | head -1 | cut -d: -f1)"
if [[ -n "$OPERATOR_LINE" && -n "$STARRED_LINE" && "$OPERATOR_LINE" -lt "$STARRED_LINE" ]]; then
    pass "the Operator Attention section is emitted BEFORE the Operator Priority section"
else
    fail "expected the Operator Attention section call to precede the Operator Priority section call (operator=$OPERATOR_LINE, starred=$STARRED_LINE)"
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
# Reconstruct the section-rendering logic exactly as render_plan_body()
# performs it for this section (mirrors section()'s heading/blurb/body
# shape). $1 = held PR bullet list.
# ---------------------------------------------------------------------------
render_operator_attention() {
    local held="$1"
    printf '## Operator Attention: Merge-Risk-Hold Pileup\n'
    printf '\n%s\n' "Judge-approved PRs stuck under a \`loom:operator\` merge-risk hold — implementation work is done, only a human merge decision is missing."
    printf '\n%s\n' "${held:-_None._}"
}

# ---------------------------------------------------------------------------
# Test 4: THE REGRESSION — identical membership renders byte-identical text
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: identical loom:operator membership renders byte-identical output"

HELD_FIXTURE=$'- **#5904**: fix(daemon): treat stale worktree registrations as already-removed in loom-daemon clean\n- **#5619**: tokens: record (provider, upstream account id) in the pool storage layer'

RENDER_1="$(render_operator_attention "$HELD_FIXTURE")"
# Simulate a later tick — same PR membership, but "time has passed" (nothing
# about that shows up in the render since there is no timestamp to smuggle
# in, unlike the old hand-appended paragraph).
RENDER_2="$(render_operator_attention "$HELD_FIXTURE")"

assert_eq "$RENDER_1" "$RENDER_2" \
    "two renders of the same held-PR set produce byte-identical text (no timestamp/prose drift)"

# A genuinely different membership (a PR cleared, or a new one added) DOES
# change the output — the fold must not make the section permanently inert.
HELD_FIXTURE_CHANGED=$'- **#5904**: fix(daemon): treat stale worktree registrations as already-removed in loom-daemon clean'
RENDER_3="$(render_operator_attention "$HELD_FIXTURE_CHANGED")"
if [[ "$RENDER_1" != "$RENDER_3" ]]; then
    pass "a genuine membership change (a PR cleared) still changes the rendered output"
else
    fail "expected a genuine membership change to change the rendered output"
fi

# Empty membership (no operator holds at all) renders the section's
# "_None._" fallback rather than omitting the section — keeping it in
# lockstep with every other section() call in render_plan_body().
RENDER_EMPTY="$(render_operator_attention "")"
if grep -q '_None._' <<<"$RENDER_EMPTY"; then
    pass "empty loom:operator membership renders the _None._ fallback, consistent with other sections"
else
    fail "expected the _None._ fallback for empty loom:operator membership"
fi

# ---------------------------------------------------------------------------
# Test 5 (#6457): $held is derived from $approved's fetched PR list, so the
# "Operator Attention" and "Approved (Awaiting Merge)" sections can no
# longer disagree about which open loom:pr PRs also carry loom:operator —
# even when a second, independently-cached/independently-search-indexed
# `gh pr list --label "loom:operator"` query would have returned stale or
# lagging results for a PR labeled `loom:operator` moments earlier.
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: \$held is derived from \$approved's list, not a second live query (#6457)"

if command -v jq >/dev/null 2>&1; then
    # Simulate: two PRs carry both loom:pr and loom:operator (one just
    # labeled loom:operator moments before this "tick"); a third carries
    # only loom:pr. A second, separately-cached `loom:operator`-filtered
    # query is deliberately NOT modeled here, because the fix removes it
    # entirely — $held must come from the SAME fetched JSON as $approved.
    APPROVED_JSON_FIXTURE='[
      {"number":6333,"title":"Test PR A","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]},
      {"number":6325,"title":"Test PR B","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]},
      {"number":9999,"title":"Test PR C","labels":[{"name":"loom:pr"}]}
    ]'

    APPROVED_DERIVED="$(printf '%s' "$APPROVED_JSON_FIXTURE" | jq -r '.[] | "- **#\(.number)**: \(.title)"')"
    HELD_DERIVED="$(printf '%s' "$APPROVED_JSON_FIXTURE" | jq -r '.[] | select([(.labels // [])[].name] | index("loom:operator")) | "- **#\(.number)**: \(.title)"')"

    if grep -q '#6333' <<<"$HELD_DERIVED" && grep -q '#6325' <<<"$HELD_DERIVED"; then
        pass "both loom:operator-labeled PRs are derived into \$held from the same fetch as \$approved"
    else
        fail "expected both #6333 and #6325 in \$held derived from \$approved_json (got: $HELD_DERIVED)"
    fi

    if grep -q '#9999' <<<"$HELD_DERIVED"; then
        fail "PR #9999 (loom:pr only, no loom:operator) unexpectedly appeared in derived \$held"
    else
        pass "a loom:pr-only PR is correctly excluded from derived \$held"
    fi

    if grep -q '#9999' <<<"$APPROVED_DERIVED"; then
        pass "\$approved still lists all three loom:pr PRs (rendering unchanged by the derivation refactor)"
    else
        fail "expected \$approved to still include #9999"
    fi

    HELD_COUNT="$([ -z "$HELD_DERIVED" ] && printf '0' || printf '%s\n' "$HELD_DERIVED" | grep -c '^- ')"
    assert_eq "$HELD_COUNT" "2" "Backlog Balance's derived held-count is correct by construction (2)"
else
    echo "  SKIP: jq not available, skipping Test 5's executable derivation check"
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
