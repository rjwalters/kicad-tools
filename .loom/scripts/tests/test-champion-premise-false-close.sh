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
# Part 1b (#8593) adds the second half the original suite had no coverage for:
# what the N=2 RE-RUN of a cited check is allowed to conclude. See its own
# header below for the incident (#8494) and the two rules it pins.
#
# Hermetic: local git fixtures under $TMPDIR (a symlink, its target, and a
# regular file) plus string/file processing. No forge, no network, no tokens.

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
# Part 1b: re-run SCORING against a real tree with a symlink (#8593)
# =============================================================================
#
# THE SECOND FAILURE MODE THIS GUARDS AGAINST
#
# Tagging the findings correctly is only half the gate. Step 4 then re-runs
# every cited mechanical check against current `origin/main` and closes if they
# still read false. On 2026-09-21 that re-run closed #8494 on two premises that
# were BOTH TRUE: every check greped `.loom/docs/<f>.md`, which is a SYMLINK on
# `origin/main` (mode 120000) since #7842, so `git show origin/main:<path>`
# returned the link-target string ("../../defaults/docs/<f>.md") rather than
# the document. Every grep of that read comes back empty no matter what the
# file says, and the gate's own escape hatch ("if any re-run now shows the
# premise IS true, go to Step 1") could not fire, because the re-run was wrong
# in exactly the same way the original check was.
#
# The two rules champion-issue-promo.md now states, exercised against a REAL
# git fixture (a symlink, its target, and a regular file), because a prompt
# rule that no fixture exercises is how the first version of this gate shipped:
#   1. Resolve a 120000 entry to its real blob path BEFORE any content check.
#      resolve_main_path() is not re-implemented here -- it is EXTRACTED FROM
#      champion-premise-false-evidence.md and eval'd, so the function under
#      test is literally the code Champion is handed, and the two cannot drift.
#   2. Only POSITIVE evidence is premise-false; an empty result on a path that
#      exists on `main` is "check inconclusive" and must NOT close.
echo ""
echo "Part 1b: re-run scoring against a real tree with a symlink (#8593)"

PREMISE_EVIDENCE_MD="$ROLE_DIR/champion-premise-false-evidence.md"
RESOLVER_SRC="$(awk '/^resolve_main_path\(\) \{/,/^\}/' "$PREMISE_EVIDENCE_MD" 2>/dev/null)"
if [[ -z "$RESOLVER_SRC" || "$RESOLVER_SRC" != *"120000"* ]]; then
    fail "champion-premise-false-evidence.md no longer defines a runnable resolve_main_path() snippet (#8593)"
    RESOLVER_SRC='resolve_main_path() { printf "%s\n" "$1"; }'   # keep going; every case below will fail loudly
else
    pass "champion-premise-false-evidence.md ships a runnable resolve_main_path() snippet"
fi
eval "$RESOLVER_SRC"

FIXTURE_ROOT="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_ROOT" 2>/dev/null || true' EXIT
FIXTURE_REPO="$FIXTURE_ROOT/repo"
mkdir -p "$FIXTURE_REPO/defaults/docs" "$FIXTURE_REPO/.loom/docs"
(
    cd "$FIXTURE_REPO" || exit 1
    git init -q -b main .
    git config user.email "test@example.com"
    git config user.name "Test"
    { seq 1 81; echo '## The 30-day fuse (#8477)'; } > defaults/docs/fuse.md   # 82 lines
    printf 'nothing to see here\n' > defaults/docs/plain.md
    ln -s ../../defaults/docs/fuse.md .loom/docs/fuse.md
    # A 2-link cycle: resolution can never terminate on a real blob, so every
    # content check against it must score INCONCLUSIVE rather than false.
    ln -s loop-b.md .loom/docs/loop-a.md
    ln -s loop-a.md .loom/docs/loop-b.md
    git add .
    git commit -qm "init" >/dev/null
    git update-ref refs/remotes/origin/main refs/heads/main
)

# score_recheck <kind> <path> <arg> -> false | inconclusive | true
# The evidence table from champion-issue-promo.md, executable. Content checks
# resolve first and then demand a REGULAR blob (100644/100755): an entry still
# reading 120000 after resolution is unreadable, hence inconclusive.
score_recheck() {
    local kind="$1" p="$2" arg="${3:-}" resolved mode n
    if [[ "$kind" == "path-exists" ]]; then
        n=$(git -C "$FIXTURE_REPO" ls-tree -r origin/main --name-only | grep -c -Fx "$p")
        [[ "$n" -eq 0 ]] && echo "false" || echo "true"
        return
    fi
    resolved="$(cd "$FIXTURE_REPO" && resolve_main_path "$p")"
    mode="$(git -C "$FIXTURE_REPO" ls-tree origin/main -- "$resolved" | awk '{print $1}')"
    case "$mode" in
        100644|100755) ;;
        120000) echo "inconclusive"; return ;;   # still a link: unreadable
        *) echo "false"; return ;;               # absent from the tree
    esac
    case "$kind" in
        string)
            n=$(git -C "$FIXTURE_REPO" show "origin/main:$resolved" | grep -c -- "$arg")
            [[ "$n" -eq 0 ]] && echo "false" || echo "true" ;;
        line-range)
            n=$(git -C "$FIXTURE_REPO" show "origin/main:$resolved" | wc -l | tr -d ' ')
            [[ "$arg" -gt "$n" ]] && echo "false" || echo "true" ;;
        *) echo "inconclusive" ;;
    esac
}

# score_empty_output <path>: the verdict for a CITED check that printed nothing.
# On a path that exists on main this is inconclusive -- an empty result is
# indistinguishable from a check that could not read the file at all.
score_empty_output() {
    local n
    n=$(git -C "$FIXTURE_REPO" ls-tree -r origin/main --name-only | grep -c -Fx "$1")
    [[ "$n" -eq 0 ]] && echo "false" || echo "inconclusive"
}

# premise_false_gate <findings_text> <space-separated re-run scores>
#   -> close | escalate | reevaluate
# Composes classify_recurring_findings() with the re-run scores exactly as the
# prompt's decision table does: any `true` sends the pass back to Step 1; any
# `inconclusive` falls through to the ordinary escalation branch; only an
# all-`false` set closes.
premise_false_gate() {
    local findings="$1" scores="$2" s
    [[ "$(classify_recurring_findings "$findings")" == "yes" ]] || { echo "escalate"; return; }
    for s in $scores; do [[ "$s" == "true" ]] && { echo "reevaluate"; return; }; done
    for s in $scores; do [[ "$s" == "inconclusive" ]] && { echo "escalate"; return; }; done
    [[ -z "${scores// /}" ]] && { echo "escalate"; return; }
    echo "close"
}

echo ""
echo "Test 7: the fixture reproduces the #8494 read (a symlink read through git)"
assert_eq "120000" \
    "$(git -C "$FIXTURE_REPO" ls-tree origin/main -- .loom/docs/fuse.md | awk '{print $1}')" \
    "fixture: .loom/docs/fuse.md is mode 120000 on origin/main"
assert_eq "0" \
    "$(git -C "$FIXTURE_REPO" show origin/main:.loom/docs/fuse.md | grep -c '30-day fuse')" \
    "unresolved, the cited grep finds the string 0 times — the false negative that closed #8494"

echo ""
echo "Test 8 (ACCEPTANCE, Ask item 3): a string present via a .loom/docs symlink is NOT closed"
FINDINGS_SYMLINK='- [premise-false] Criterion 8: `.loom/docs/fuse.md` does not contain "30-day fuse" — verified via: `git show origin/main:.loom/docs/fuse.md | grep -c "30-day fuse"` → 0'
SCORE_SYMLINK="$(score_recheck string ".loom/docs/fuse.md" "30-day fuse")"
assert_eq "true" "$SCORE_SYMLINK" "resolving the symlink first shows the premise is TRUE (string present in the target)"
assert_eq "reevaluate" "$(premise_false_gate "$FINDINGS_SYMLINK" "$SCORE_SYMLINK")" \
    "a proposal citing a string present via a .loom/docs symlink is NOT closed — it goes back to Step 1"

echo ""
echo "Test 9 (ACCEPTANCE, Ask item 2): an empty result on an existing path is inconclusive, not false"
assert_eq "inconclusive" "$(score_empty_output ".loom/docs/fuse.md")" \
    "a cited check that printed nothing, on a path that exists on main, scores inconclusive"
assert_eq "escalate" "$(premise_false_gate "$FINDINGS_SYMLINK" "inconclusive")" \
    "an inconclusive re-run escalates to the operator instead of closing"
assert_eq "escalate" "$(premise_false_gate "$FINDINGS_TWO_FALSE" "false inconclusive")" \
    "one inconclusive re-run in an otherwise-false set still blocks the close"

echo ""
echo "Test 10: positive evidence still closes (the gate must not become a no-op)"
assert_eq "false" "$(score_recheck path-exists "src/does/not/exist.rs")" \
    "a path absent from the tree scores false (absence IS positive evidence)"
assert_eq "false" "$(score_recheck string "defaults/docs/plain.md" "30-day fuse")" \
    "grep -c = 0 on a regular blob scores false"
assert_eq "false" "$(score_recheck line-range ".loom/docs/fuse.md" "9999")" \
    "a cited line past the end of the RESOLVED document scores false"
assert_eq "true" "$(score_recheck line-range ".loom/docs/fuse.md" "82")" \
    "a cited line inside the resolved document scores true (the link's own 1 line would have said false)"
assert_eq "false" "$(score_recheck string ".loom/docs/absent.md" "anything")" \
    "a .loom/docs path that does not exist at all still scores false (resolution is not an excuse)"
assert_eq "inconclusive" "$(score_recheck string ".loom/docs/loop-a.md" "anything")" \
    "a symlink cycle exhausts the hop bound and scores inconclusive, never false"
assert_eq "escalate" "$(premise_false_gate "$FINDINGS_SYMLINK" "$(score_recheck string ".loom/docs/loop-a.md" "anything")")" \
    "an unresolvable cycle escalates instead of closing"
assert_eq "close" "$(premise_false_gate "$FINDINGS_ALL_FALSE" "false")" \
    "an all-false, all-positive-evidence re-run still CLOSES"
assert_eq "close" "$(premise_false_gate "$FINDINGS_TWO_FALSE" "false false")" \
    "two findings, both re-verified false, still CLOSE"
assert_eq "escalate" "$(premise_false_gate "$FINDINGS_MIXED" "false")" \
    "a mixed finding set escalates regardless of re-run scores"

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
    # --- #8593: symlink resolution, positive evidence, count mismatch
    assert_doc_contains "$CHAMPION_PROMO_MD" 'P=$(resolve_main_path "$p")' \
        "the gate routes every cited path through resolve_main_path before re-running it (#8593)"
    assert_doc_contains "$CHAMPION_PROMO_MD" "120000" \
        "champion-issue-promo.md names the symlink tree mode the resolver exists for"
    assert_doc_contains "$CHAMPION_PROMO_MD" 'An empty result is "check inconclusive", never "premise false"' \
        "champion-issue-promo.md states the empty-result rule as a rule, not an aside"
    assert_doc_contains "$CHAMPION_PROMO_MD" "only if EVERY re-run scored" \
        "the gate's CLOSE_PREMISE_FALSE=yes is conditioned on every re-run scoring false"
    assert_doc_contains "$CHAMPION_PROMO_MD" "premise-false only when the correct count is zero" \
        "champion-issue-promo.md rules an undercount ordinary imprecision, not premise-false (#8313)"
    # The snippet the prompt hands Champion must actually resolve a real
    # symlink — a recipe that does not run is the same class of unrunnable
    # check this whole change is about.
    assert_eq "defaults/docs/fuse.md" \
        "$(cd "$FIXTURE_REPO" && resolve_main_path .loom/docs/fuse.md)" \
        "the prompt's own resolve_main_path() snippet resolves a .loom/docs symlink"
fi

if [[ ! -f "$PREMISE_EVIDENCE_MD" ]]; then
    fail "champion-premise-false-evidence.md not found at $PREMISE_EVIDENCE_MD"
else
    assert_doc_contains "$CHAMPION_PROMO_MD" "champion-premise-false-evidence.md" \
        "champion-issue-promo.md links the evidence rules from the finding-kind section"
    assert_doc_contains "$PREMISE_EVIDENCE_MD" "Positive evidence — premise IS false" \
        "the evidence file carries the positive-evidence table"
    assert_doc_contains "$PREMISE_EVIDENCE_MD" "premise-false only when the correct count is zero" \
        "the evidence file repeats the count-mismatch rule (#8313) beside the table"
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
