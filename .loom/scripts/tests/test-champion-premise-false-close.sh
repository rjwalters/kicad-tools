#!/usr/bin/env bash
# test-champion-premise-false-close.sh - Regression coverage for issue #7657.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# champion-issue-promo.md's Step 4 escalates ANY proposal that has failed
# evaluation twice without revision (UNREVISED_EVALS >= N) to
# `loom:operator-only`, regardless of WHY it keeps failing. A proposal whose
# central factual claim is conclusively false on current `main` -- a cited
# file does not exist, a "tracked" file is not tracked, a cited line range
# does not exist -- took the identical path as a proposal that merely needs
# revision, even though re-running the exact same mechanical check
# (`git ls-tree`, `git ls-files`, `wc -l`, ...) a third time can never change
# the outcome. That wastes an operator's attention on a decision agents have
# already conclusively made twice.
#
# Champion Step 4 now defines a `premise-false` finding kind (tagged
# `[premise-false]` on a Recurring-findings bullet, with the exact mechanical
# check + output cited) and a "premise-false close gate" that runs before the
# ordinary escalation branch: when EVERY recurring finding is tagged
# `premise-false` and every cited check re-verifies false, Champion closes the
# issue (`<!-- champion:premise-false-closed:<main-sha> -->`,
# `gh issue close --reason "not planned"`) instead of applying
# `loom:operator-only`. A mixed finding set (one premise-false + one ordinary
# merits finding) still escalates exactly as before this gate existed.
#
# This suite is hybrid, mirroring test-classify-dependency-block.sh and
# test-champion-epic-verdict-marker-scope.sh:
#
#   1. LOGIC -- classify_recurring_findings() is a small, directly-testable
#      shell function implementing EXACTLY the decision table
#      champion-issue-promo.md's "Premise-false close gate" section
#      documents: given a Recurring-findings bullet list, decide close vs.
#      escalate. Exercised against fixture finding-lists for both Acceptance
#      Criteria scenarios plus edge cases (all-premise-false, mixed,
#      no-premise-false, empty).
#   2. WIRING -- the shipped role/reference/doc files are pinned with literal
#      assert_doc_contains checks: the marker name, the close command, the
#      "no loom:operator-only" invariant, the criteria-6/8 finding-kind
#      definition, the Edge Case table row, the label-state-machine row, the
#      CLAUDE.md role-autonomy grant, and hermit.md's corrected description
#      all fail this suite if removed or drifted.
#
# Hermetic: pure string/file processing. No forge/network/live git calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Role prompts are shipped (installed at .claude/commands/loom) -- resolve the
# way each layout actually lays it out: the installed path first (consumer
# repos, and Loom's own dogfooded checkout), falling back to the defaults/
# source-tree path (a bare source checkout with no installed copy yet). See
# issue #6194 / #6241.
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi

CHAMPION_PROMO_MD="$ROLE_DIR/champion-issue-promo.md"
CHAMPION_MD="$ROLE_DIR/champion.md"
CHAMPION_REF_MD="$ROLE_DIR/champion-reference.md"
HERMIT_MD="$ROLE_DIR/hermit.md"

if [[ -f "$REPO_ROOT/.loom/docs/label-state-machine.md" ]]; then
    LABEL_STATE_MD="$REPO_ROOT/.loom/docs/label-state-machine.md"
else
    LABEL_STATE_MD="$REPO_ROOT/defaults/docs/label-state-machine.md"
fi

CLAUDE_MD="$REPO_ROOT/CLAUDE.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    if grep -qF -- "$needle" "$file"; then
        pass "$msg"
    else
        fail "$msg (missing literal in $file: $needle)"
    fi
}

# --- classify_recurring_findings(): the function under test -----------------
#
# Mirrors champion-issue-promo.md's "Premise-false close gate" bash block
# exactly: given the Recurring-findings bullet text (one finding per line,
# each starting with "- "), decide CLOSE_PREMISE_FALSE=yes|no. A finding
# counts as premise-false only if it is tagged "- [premise-false]"; the gate
# requires TOTAL_FINDINGS > 0 (an empty list never closes) and
# TOTAL_FINDINGS == PREMISE_FALSE_FINDINGS (all-or-nothing -- one ordinary
# finding anywhere in the set disqualifies the whole set).
classify_recurring_findings() {
    local findings_text="$1"
    local total premise_false
    total=$(printf '%s\n' "$findings_text" | grep -c '^- ' || true)
    premise_false=$(printf '%s\n' "$findings_text" | grep -c '^- \[premise-false\]' || true)
    if [[ "$total" -gt 0 && "$total" -eq "$premise_false" ]]; then
        echo "yes"
    else
        echo "no"
    fi
}

echo "================================"
echo "test-champion-premise-false-close.sh (#7657)"
echo "================================"

# =============================================================================
# Part 1: classify_recurring_findings() logic
# =============================================================================
echo ""
echo "Part 1: classify_recurring_findings() decision logic"

# --- Acceptance criterion 1: only recurring finding is premise-false --------
echo ""
echo "Test 1: a single premise-false finding closes"
FINDINGS_ALL_FALSE='- [premise-false] Criterion 8: proposal cites `src/does/not/exist.rs:42`, which is not on `origin/main` — verified via: `git ls-tree -r origin/main --name-only | grep -Fx src/does/not/exist.rs` → (no output, exit 1)'
RESULT="$(classify_recurring_findings "$FINDINGS_ALL_FALSE")"
assert_eq "yes" "$RESULT" "a lone premise-false finding classifies as CLOSE"

echo ""
echo "Test 2: two recurring findings, both premise-false, closes"
FINDINGS_TWO_FALSE='- [premise-false] Criterion 6: proposal claims `tests/fixtures/legacy.json` is tracked — verified via: `git ls-files tests/fixtures/legacy.json` → (no output)
- [premise-false] Criterion 8: proposal cites a 40-line test file at `spec/foo_test.rb:1-40` that only has 12 lines — verified via: `wc -l spec/foo_test.rb` → `12 spec/foo_test.rb`'
RESULT="$(classify_recurring_findings "$FINDINGS_TWO_FALSE")"
assert_eq "yes" "$RESULT" "two premise-false findings (no other findings) classify as CLOSE"

# --- Acceptance criterion 2: premise-false + an ordinary merits finding -----
echo ""
echo "Test 3: one premise-false finding plus one ordinary style finding escalates"
FINDINGS_MIXED='- [premise-false] Criterion 8: proposal cites `lib/removed_module.py`, which is not on `origin/main` — verified via: `git ls-tree -r origin/main --name-only | grep -Fx lib/removed_module.py` → (no output, exit 1)
- Criterion 6: proposal body is missing a test strategy section'
RESULT="$(classify_recurring_findings "$FINDINGS_MIXED")"
assert_eq "no" "$RESULT" "a mixed premise-false + ordinary finding set classifies as ESCALATE, not CLOSE"

echo ""
echo "Test 4: two ordinary findings, no premise-false tag, escalates"
FINDINGS_ORDINARY='- Criterion 3: acceptance criteria are not testable
- Criterion 5: scope is too large for one PR'
RESULT="$(classify_recurring_findings "$FINDINGS_ORDINARY")"
assert_eq "no" "$RESULT" "an all-ordinary finding set classifies as ESCALATE"

echo ""
echo "Test 5: empty findings text never closes (all-or-nothing needs at least one finding)"
RESULT="$(classify_recurring_findings "")"
assert_eq "no" "$RESULT" "an empty findings list classifies as ESCALATE (never a vacuous CLOSE)"

echo ""
echo "Test 6: a dependency finding (not premise-false) still escalates via this gate"
FINDINGS_DEP='- Criterion 2: blocked by #1234, which is still open'
RESULT="$(classify_recurring_findings "$FINDINGS_DEP")"
assert_eq "no" "$RESULT" "an ordinary dependency finding (handled by the separate dependency-timing gate) does not trip the premise-false gate"

# =============================================================================
# Part 2: wiring -- the shipped prompt/reference/doc files
# =============================================================================
echo ""
echo "Part 2: wiring in the shipped role prompts and docs"

if [[ ! -f "$CHAMPION_PROMO_MD" ]]; then
    fail "champion-issue-promo.md not found at $CHAMPION_PROMO_MD"
else
    assert_doc_contains "$CHAMPION_PROMO_MD" "premise-false" \
        "champion-issue-promo.md defines the premise-false finding kind"
    assert_doc_contains "$CHAMPION_PROMO_MD" "Premise-false close gate" \
        "champion-issue-promo.md has a named premise-false close gate section"
    assert_doc_contains "$CHAMPION_PROMO_MD" "champion:premise-false-closed:" \
        "champion-issue-promo.md defines the premise-false-closed marker"
    assert_doc_contains "$CHAMPION_PROMO_MD" 'gh issue close <number> --reason "not planned"' \
        "champion-issue-promo.md's close template uses --reason \"not planned\""
    assert_doc_contains "$CHAMPION_PROMO_MD" "is deliberately **not** applied here" \
        "champion-issue-promo.md states loom:operator-only is NOT applied on a premise-false close"
    assert_doc_contains "$CHAMPION_PROMO_MD" "CLOSE_PREMISE_FALSE" \
        "champion-issue-promo.md's gate computes a CLOSE_PREMISE_FALSE decision variable"
    assert_doc_contains "$CHAMPION_PROMO_MD" "Re-run EACH cited mechanical" \
        "champion-issue-promo.md requires re-running every cited mechanical check before closing"
    assert_doc_contains "$CHAMPION_PROMO_MD" "mixed premise-false + ordinary finding" \
        "champion-issue-promo.md documents that a mixed finding set still escalates"
    assert_doc_contains "$CHAMPION_PROMO_MD" "verified via:" \
        "champion-issue-promo.md's finding-kind definition requires the literal verification command"
fi

if [[ ! -f "$CHAMPION_REF_MD" ]]; then
    fail "champion-reference.md not found at $CHAMPION_REF_MD"
else
    assert_doc_contains "$CHAMPION_REF_MD" "premise-false" \
        "champion-reference.md's Edge Case decision table mentions premise-false"
    assert_doc_contains "$CHAMPION_REF_MD" "champion:premise-false-closed" \
        "champion-reference.md's decision table names the close marker"
fi

if [[ ! -f "$CHAMPION_MD" ]]; then
    fail "champion.md not found at $CHAMPION_MD"
else
    assert_doc_contains "$CHAMPION_MD" "premise-false close gate" \
        "champion.md's Capped-PR Recovery Pass sentence names the premise-false exception"
fi

if [[ ! -f "$HERMIT_MD" ]]; then
    fail "hermit.md not found at $HERMIT_MD"
else
    assert_doc_contains "$HERMIT_MD" "Champion itself almost never closes here" \
        "hermit.md's workflow table corrects the old 'closes issue to reject' claim"
    assert_doc_contains "$HERMIT_MD" "premise-false close gate" \
        "hermit.md's corrected description references the premise-false close gate"
fi

if [[ ! -f "$LABEL_STATE_MD" ]]; then
    fail "label-state-machine.md not found at $LABEL_STATE_MD"
else
    assert_doc_contains "$LABEL_STATE_MD" "champion:premise-false-closed" \
        "label-state-machine.md's Champion escalation row records the close marker"
    assert_doc_contains "$LABEL_STATE_MD" "no \`loom:operator-only\` and no sub-kind" \
        "label-state-machine.md states the close path applies no operator-only sub-kind"
fi

if [[ ! -f "$CLAUDE_MD" ]]; then
    fail "CLAUDE.md not found at $CLAUDE_MD"
else
    assert_doc_contains "$CLAUDE_MD" "premise-false" \
        "root CLAUDE.md's Issues Are Suggestions section names the premise-false grant"
    assert_doc_contains "$CLAUDE_MD" "Champion" \
        "root CLAUDE.md's Issues Are Suggestions section credits Champion"
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
