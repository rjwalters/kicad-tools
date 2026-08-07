#!/usr/bin/env bash
# test-guide-work-log-out-of-order-merge.sh - Regression test for issue #5516
#
# `update_work_log()` in guide.md used to filter candidate PRs purely by
# `select(.number > $last_pr)`, assuming PR numbers increase in merge order.
# They don't: a lower-numbered PR can sit in review/Doctor longer than a
# higher-numbered PR that merges first. Once a later tick recorded the
# higher-numbered PR and advanced the watermark past the lower PR's number,
# `.number > $last_pr` could never become true for it again — it was dropped
# SILENTLY and PERMANENTLY. This happened for real with PR #5507 (opened
# before, merged after, #5509).
#
# The fix (#5516) replaces the number-watermark filter for PRs with a
# presence check: a candidate is new if "PR #<N>" is NOT already literally
# recorded in WORK_LOG.md, regardless of how its number compares to anything
# already recorded. The merged-PR query window was also widened from a fixed
# `--limit 50` count to a date-bounded `--search "merged:>=<since>"` (plus a
# larger `--limit` as a safety cap), so an out-of-order PR cannot be pushed
# out of the query purely because enough other PRs merged after it.
#
# Verifies that:
#   1. guide.md defines `work_log_has_pr()` (the presence-check helper) and
#      wires it into `update_work_log()` instead of a `.number > $last_pr`
#      comparison for PR candidates; and that the merged-PR query no longer
#      relies solely on a fixed `--limit 50` (a `merged:>=` date search is
#      present, with a widened `--limit`).
#   2. THE REGRESSION, executed rather than grepped: PR A (lower number,
#      opens first) merges AFTER PR B (higher number). Once B is recorded (so
#      the old watermark sits above A's number), the fixed presence-check
#      logic still records A on the next simulated tick — reproducing
#      #5507/#5509.
#   3. The out-of-order PR is still caught even when it would have fallen
#      outside a naive fixed-count window (the scenario the date-based search
#      widening addresses) — modeled by directly exercising the presence
#      check that no longer depends on `--limit`/count at all.
#   4. Multiple out-of-order PRs landing in the same tick are all recorded.
#   5. A PR that is both out-of-order AND excluded by GUIDE_DOCS_PR_EXCLUDE
#      (issue #5454) stays excluded — the presence-check fix does not pull
#      docs-maintenance PRs back in.
#
# Hermetic: pure jq/grep against a temp dir. No forge, network, or `gh` calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"

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

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
    exit 0
fi

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

SANDBOX="$(mktemp -d)"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

DOCS_TITLE='docs: Guide document maintenance update'

# ---------------------------------------------------------------------------
# Test 1: guide.md wires a presence check (not a pure number filter) into the
# merged-PR path, and widens the query window past a fixed --limit 50.
# ---------------------------------------------------------------------------
echo "Test 1: guide.md uses a presence check and a widened merged-PR window"

assert_grep '^work_log_has_pr\(\) \{' "$GUIDE_MD" \
    "work_log_has_pr() is defined"
assert_grep 'if ! work_log_has_pr' "$GUIDE_MD" \
    "update_work_log() gates new PR candidates through work_log_has_pr()"
assert_grep 'select\(\(\$GUIDE_DOCS_PR_EXCLUDE\) \| not\)' "$GUIDE_MD" \
    "the docs-PR exclusion (#5454) is still applied to PR candidates"
assert_grep '[-]-search "merged:>=\$since"' "$GUIDE_MD" \
    "the merged-PR query is bounded by date, not just a fixed --limit"
assert_grep '[-]-json number,title,mergedAt,headRefName' "$GUIDE_MD" \
    "headRefName is requested from gh --json (needed by the exclusion filter)"

# ---------------------------------------------------------------------------
# Extract GUIDE_DOCS_PR_EXCLUDE verbatim (same technique as
# test-guide-work-log-self-loop.sh) and reconstruct the fixed pipeline:
# candidates are exclusion-filtered (NOT number-filtered), then gated one by
# one through a presence check mirroring work_log_has_pr().
# ---------------------------------------------------------------------------
EXCLUDE_LINE="$(grep -m1 '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD")"
GUIDE_DOCS_PR_EXCLUDE="${EXCLUDE_LINE#GUIDE_DOCS_PR_EXCLUDE=}"
GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE#\'}"
GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE%\'}"

if [[ -n "$GUIDE_DOCS_PR_EXCLUDE" ]]; then
    pass "extracted exclusion expression from guide.md"
else
    fail "could not extract exclusion expression from guide.md"
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    exit 1
fi

# work_log_has_pr(), verbatim in behavior: exact-line match against the
# extracted "PR #<N>" numbers already recorded in WORK_LOG.md.
work_log_has_pr() {
    local n="$1" work_log="$2"
    grep -oE 'PR #[0-9]+' "$work_log" 2>/dev/null | grep -oE '[0-9]+' | grep -qx "$n"
}

# Mirror of update_work_log()'s fixed PR path: $1 = the merged-PR JSON gh
# would have returned for the query window (date-bounded, NOT limited by a
# fixed --number cutoff), $2 = the WORK_LOG.md path to presence-check against.
# Candidates are exclusion-filtered only; number order plays no role.
new_prs() {
    local candidates work_log="$2" out="[]"
    candidates=$(printf '%s\n' "$1" | jq -c "[.[] | select(($GUIDE_DOCS_PR_EXCLUDE) | not)] | sort_by(.mergedAt) | reverse")
    while IFS= read -r pr_json; do
        [[ -z "$pr_json" ]] && continue
        local n
        n=$(printf '%s' "$pr_json" | jq -r '.number')
        if ! work_log_has_pr "$n" "$work_log"; then
            out=$(printf '%s\n' "$out" | jq -c --argjson pr "$pr_json" '. + [$pr]')
        fi
    done < <(printf '%s\n' "$candidates" | jq -c '.[]')
    printf '%s\n' "$out"
}

work_log_max_pr() {
    { grep -oE 'PR #[0-9]+' "$1" 2>/dev/null | grep -oE '[0-9]+'; echo 0; } | sort -rn | head -1
}

# ---------------------------------------------------------------------------
# Test 2: THE REGRESSION — #5507/#5509 reproduced. PR A (#5507) opens before,
# but merges after, PR B (#5509). B is already recorded (so the OLD watermark
# sits at 5509, above A's number 5507). The fixed logic still records A.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: an out-of-order-merged PR is still recorded (the #5507/#5509 case)"

WORK_LOG="$SANDBOX/WORK_LOG.md"
cat > "$WORK_LOG" <<'EOF'
# Work Log

### 2026-08-06

- **PR #5509**: feat(fleet): add `fleet roll`
EOF

OLD_WATERMARK="$(work_log_max_pr "$WORK_LOG")"
assert_eq "$OLD_WATERMARK" "5509" \
    "watermark already sits above PR A's number (5507 < 5509) after B was recorded"

# Under the OLD (buggy) `.number > $last_pr` filter, PR A could NEVER be
# selected again: 5507 > 5509 is false, permanently.
OLD_FILTER_KEEPS_A=$(jq -n --arg n "5507" '($n | tonumber) > 5509')
assert_eq "$OLD_FILTER_KEEPS_A" "false" \
    "sanity check: the old number filter would have permanently excluded PR A"

# The next tick's merged-PR query window (date-bounded, includes both PRs
# regardless of number order):
NEW_CANDIDATES=$(jq -n '[
  {number: 5507, title: "fix: guard test sandbox supervisor identity, not just state paths", mergedAt: "2026-08-06T05:30:06Z", headRefName: "feature/issue-5501"},
  {number: 5509, title: "feat(fleet): add `fleet roll`",                                     mergedAt: "2026-08-06T05:01:50Z", headRefName: "feature/issue-5505"}
]')

RESULT="$(new_prs "$NEW_CANDIDATES" "$WORK_LOG")"
assert_eq "$(printf '%s' "$RESULT" | jq 'length')" "1" \
    "exactly one candidate is new (B is already recorded, A is not)"
assert_eq "$(printf '%s' "$RESULT" | jq -r '.[0].number')" "5507" \
    "the fixed presence-check logic recovers PR A (#5507) despite its lower number"

# Simulate the append, and confirm the presence check now finds A too.
{
    printf '\n### 2026-08-06\n\n'
    printf '%s\n' "$RESULT" | jq -r '.[] | "- **PR #\(.number)**: \(.title)"'
} >> "$WORK_LOG"

if work_log_has_pr "5507" "$WORK_LOG"; then
    pass "PR #5507 is now present in WORK_LOG.md after the simulated tick"
else
    fail "PR #5507 was not written to WORK_LOG.md"
fi

# ---------------------------------------------------------------------------
# Test 3: edge case (a) — the out-of-order PR would have fallen outside a
# naive fixed-count `--limit 50` window entirely. The presence check has no
# concept of "window position": it only asks "is this literally recorded
# yet?", so it still recovers the PR when handed a broader, date-bounded
# candidate set that a count-only query might have excluded it from.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: an out-of-order PR that a fixed-count window would have missed is still caught"

WORK_LOG2="$SANDBOX/WORK_LOG2.md"
cat > "$WORK_LOG2" <<'EOF'
# Work Log

### 2026-08-06

- **PR #5600**: some later, unrelated PR
EOF

# Simulate 60 intervening PRs merging after the out-of-order PR A (#5507) but
# before this tick, plus the already-recorded PR #5600. A fixed
# `--limit 50 --state merged` query (no date bound) sorted newest-first would
# truncate before ever reaching #5507. The date-bounded query the fix uses
# has no such count ceiling, so it still surfaces #5507.
WIDE_CANDIDATES=$(jq -n '
  [{number: 5507, title: "fix: guard test sandbox supervisor identity",
    mergedAt: "2026-08-06T05:30:06Z", headRefName: "feature/issue-5501"}]
  + [range(5541; 5601) | {
      number: .,
      title: "unrelated merged PR",
      mergedAt: "2026-08-06T06:00:00Z",
      headRefName: ("feature/issue-" + (. | tostring))
    }]
')
assert_eq "$(printf '%s' "$WIDE_CANDIDATES" | jq 'length')" "61" \
    "fixture models 61 candidates merged since A, i.e. beyond a --limit 50 cutoff"

RESULT2="$(new_prs "$WIDE_CANDIDATES" "$WORK_LOG2")"
assert_eq "$(printf '%s' "$RESULT2" | jq '[.[] | select(.number == 5507)] | length')" "1" \
    "PR #5507 survives even inside a 61-candidate window a --limit 50 count would have truncated"

# ---------------------------------------------------------------------------
# Test 4: multiple out-of-order PRs landing in the same tick are all kept.
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: multiple out-of-order PRs in one tick are all recorded"

WORK_LOG3="$SANDBOX/WORK_LOG3.md"
cat > "$WORK_LOG3" <<'EOF'
# Work Log

### 2026-08-06

- **PR #5490**: some already-recorded, higher-numbered PR
EOF

MULTI_CANDIDATES=$(jq -n '[
  {number: 5470, title: "PR A: opened first, merged last",  mergedAt: "2026-08-06T09:00:00Z", headRefName: "feature/issue-5460"},
  {number: 5480, title: "PR B: opened second, merged mid",  mergedAt: "2026-08-06T08:00:00Z", headRefName: "feature/issue-5465"},
  {number: 5490, title: "some already-recorded, higher-numbered PR", mergedAt: "2026-08-06T07:00:00Z", headRefName: "feature/issue-5488"}
]')

RESULT3="$(new_prs "$MULTI_CANDIDATES" "$WORK_LOG3")"
assert_eq "$(printf '%s' "$RESULT3" | jq 'length')" "2" \
    "both out-of-order PRs (5470, 5480) are kept; the already-recorded one (5490) is dropped"
assert_eq "$(printf '%s' "$RESULT3" | jq -c '[.[].number] | sort')" "[5470,5480]" \
    "the two out-of-order PR numbers are exactly the ones recovered"

# ---------------------------------------------------------------------------
# Test 5: a PR that is BOTH out-of-order AND excluded by GUIDE_DOCS_PR_EXCLUDE
# (#5454) must stay excluded — the presence-check fix must not pull docs
# self-loop PRs back in.
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: an out-of-order docs-maintenance PR stays excluded (#5454 preserved)"

WORK_LOG4="$SANDBOX/WORK_LOG4.md"
cat > "$WORK_LOG4" <<'EOF'
# Work Log

### 2026-08-06

- **PR #5495**: a later, higher-numbered genuine PR
EOF

DOCS_AND_OOO_CANDIDATES=$(jq -n --arg t "$DOCS_TITLE" '[
  {number: 5480, title: $t, mergedAt: "2026-08-06T09:00:00Z", headRefName: "docs/guide-update-20260806-090000"},
  {number: 5485, title: "a genuine out-of-order PR", mergedAt: "2026-08-06T09:30:00Z", headRefName: "feature/issue-5482"},
  {number: 5495, title: "a later, higher-numbered genuine PR", mergedAt: "2026-08-06T08:00:00Z", headRefName: "feature/issue-5490"}
]')

RESULT4="$(new_prs "$DOCS_AND_OOO_CANDIDATES" "$WORK_LOG4")"
assert_eq "$(printf '%s' "$RESULT4" | jq 'length')" "1" \
    "only the genuine out-of-order PR survives"
assert_eq "$(printf '%s' "$RESULT4" | jq -r '.[0].number')" "5485" \
    "the surviving entry is the genuine out-of-order PR (#5485), not the docs PR"
assert_eq "$(printf '%s' "$RESULT4" | jq -r "[.[] | select(.title == \"$DOCS_TITLE\")] | length")" "0" \
    "the out-of-order docs-maintenance PR (#5480) never re-enters via the presence-check fix"

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
