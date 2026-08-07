#!/usr/bin/env bash
# test-guide-work-log-issue-out-of-order.sh - Regression test for issue #5539
#
# `update_work_log()` in guide.md used to filter candidate closed issues
# purely by `select(.number > $last_issue)`, assuming issue numbers close in
# the same order they were opened. They don't: a lower-numbered issue can
# stay open (blocked, deferred, reopened) longer than a higher-numbered issue
# that closes first. Once a later tick recorded the higher-numbered issue and
# advanced the watermark past the lower issue's number, `.number >
# $last_issue` could never become true for it again — it was dropped
# SILENTLY and PERMANENTLY. This is the exact #5516 failure shape (already
# fixed on the PR side by PR #5531's `work_log_has_pr()`), just on the issue
# side — observed for real as 30 out-of-order-closed issues silently dropped
# from WORK_LOG.md.
#
# The fix (#5539) replaces the number-watermark filter for issues with a
# presence check: a candidate is new if "Issue #<N>" is NOT already literally
# recorded in WORK_LOG.md, regardless of how its number compares to anything
# already recorded. The closed-issue query window was also widened from a
# fixed `--limit 50` count to a date-bounded `--search "closed:>=<since>"`
# (plus a larger `--limit` as a safety cap), so an out-of-order issue cannot
# be pushed out of the query purely because enough other issues closed after
# it.
#
# Verifies that:
#   1. guide.md defines `work_log_has_issue()` (the presence-check helper)
#      and wires it into `update_work_log()` instead of a
#      `.number > $last_issue` comparison for issue candidates; and that the
#      closed-issue query no longer relies solely on a fixed `--limit 50` (a
#      `closed:>=` date search is present, with a widened `--limit`).
#   2. THE REGRESSION, executed rather than grepped: issue A (lower number,
#      opens first) closes AFTER issue B (higher number). Once B is recorded
#      (so the old watermark sits above A's number), the fixed
#      presence-check logic still records A on the next simulated tick.
#   3. The out-of-order issue is still caught even when it would have fallen
#      outside a naive fixed-count window (the scenario the date-based
#      search widening addresses) — modeled by directly exercising the
#      presence check that no longer depends on `--limit`/count at all.
#   4. Multiple out-of-order issues landing in the same tick are all
#      recorded.
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

# ---------------------------------------------------------------------------
# Test 1: guide.md wires a presence check (not a pure number filter) into the
# closed-issue path, and widens the query window past a fixed --limit 50.
# ---------------------------------------------------------------------------
echo "Test 1: guide.md uses a presence check and a widened closed-issue window"

assert_grep '^work_log_has_issue\(\) \{' "$GUIDE_MD" \
    "work_log_has_issue() is defined"
assert_grep 'if ! work_log_has_issue' "$GUIDE_MD" \
    "update_work_log() gates new issue candidates through work_log_has_issue()"
assert_grep '[-]-search "closed:>=\$since"' "$GUIDE_MD" \
    "the closed-issue query is bounded by date, not just a fixed --limit"

# update_work_log() must no longer filter issue candidates with a number
# watermark comparison.
if grep -qE 'select\(\.number > \$last_issue\)' "$GUIDE_MD"; then
    fail "update_work_log() still filters issues with a .number > \$last_issue watermark"
else
    pass "update_work_log() no longer filters issues with a .number > \$last_issue watermark"
fi

# ---------------------------------------------------------------------------
# Reconstruct the fixed pipeline: candidates are date-bounded (NOT
# number-filtered), then gated one by one through a presence check mirroring
# work_log_has_issue().
# ---------------------------------------------------------------------------

# work_log_has_issue(), verbatim in behavior: exact-line match against the
# extracted "Issue #<N>" numbers already recorded in WORK_LOG.md.
work_log_has_issue() {
    local n="$1" work_log="$2"
    grep -oE 'Issue #[0-9]+' "$work_log" 2>/dev/null | grep -oE '[0-9]+' | grep -qx "$n"
}

# Mirror of update_work_log()'s fixed issue path: $1 = the closed-issue JSON
# gh would have returned for the query window (date-bounded, NOT limited by a
# fixed --number cutoff), $2 = the WORK_LOG.md path to presence-check
# against. Number order plays no role.
new_issues() {
    local candidates work_log="$2" out="[]"
    candidates=$(printf '%s\n' "$1" | jq -c 'sort_by(.closedAt) | reverse')
    while IFS= read -r issue_json; do
        [[ -z "$issue_json" ]] && continue
        local n
        n=$(printf '%s' "$issue_json" | jq -r '.number')
        if ! work_log_has_issue "$n" "$work_log"; then
            out=$(printf '%s\n' "$out" | jq -c --argjson issue "$issue_json" '. + [$issue]')
        fi
    done < <(printf '%s\n' "$candidates" | jq -c '.[]')
    printf '%s\n' "$out"
}

work_log_max_issue() {
    { grep -oE 'Issue #[0-9]+' "$1" 2>/dev/null | grep -oE '[0-9]+'; echo 0; } | sort -rn | head -1
}

# ---------------------------------------------------------------------------
# Test 2: THE REGRESSION — issue A (lower number, closes last) is dropped by
# the old watermark once issue B (higher number, closes first) is recorded.
# The fixed presence-check logic still records A.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: an out-of-order-closed issue is still recorded"

WORK_LOG="$SANDBOX/WORK_LOG.md"
cat > "$WORK_LOG" <<'EOF'
# Work Log

### 2026-08-06

- **Issue #5525** (closed): feat(fleet): add `fleet roll`
EOF

OLD_WATERMARK="$(work_log_max_issue "$WORK_LOG")"
assert_eq "$OLD_WATERMARK" "5525" \
    "watermark already sits above issue A's number (5510 < 5525) after B was recorded"

# Under the OLD (buggy) `.number > $last_issue` filter, issue A could NEVER
# be selected again: 5510 > 5525 is false, permanently.
OLD_FILTER_KEEPS_A=$(jq -n --arg n "5510" '($n | tonumber) > 5525')
assert_eq "$OLD_FILTER_KEEPS_A" "false" \
    "sanity check: the old number filter would have permanently excluded issue A"

# The next tick's closed-issue query window (date-bounded, includes both
# issues regardless of number order):
NEW_CANDIDATES=$(jq -n '[
  {number: 5510, title: "fix: a blocked issue that stayed open a long time", closedAt: "2026-08-06T09:30:06Z"},
  {number: 5525, title: "feat(fleet): add `fleet roll`",                    closedAt: "2026-08-06T05:01:50Z"}
]')

RESULT="$(new_issues "$NEW_CANDIDATES" "$WORK_LOG")"
assert_eq "$(printf '%s' "$RESULT" | jq 'length')" "1" \
    "exactly one candidate is new (B is already recorded, A is not)"
assert_eq "$(printf '%s' "$RESULT" | jq -r '.[0].number')" "5510" \
    "the fixed presence-check logic recovers issue A (#5510) despite its lower number"

# Simulate the append, and confirm the presence check now finds A too.
{
    printf '\n### 2026-08-06\n\n'
    printf '%s\n' "$RESULT" | jq -r '.[] | "- **Issue #\(.number)** (closed): \(.title)"'
} >> "$WORK_LOG"

if work_log_has_issue "5510" "$WORK_LOG"; then
    pass "Issue #5510 is now present in WORK_LOG.md after the simulated tick"
else
    fail "Issue #5510 was not written to WORK_LOG.md"
fi

# ---------------------------------------------------------------------------
# Test 3: edge case — the out-of-order issue would have fallen outside a
# naive fixed-count `--limit 50` window entirely. The presence check has no
# concept of "window position": it only asks "is this literally recorded
# yet?", so it still recovers the issue when handed a broader, date-bounded
# candidate set that a count-only query might have excluded it from.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: an out-of-order issue that a fixed-count window would have missed is still caught"

WORK_LOG2="$SANDBOX/WORK_LOG2.md"
cat > "$WORK_LOG2" <<'EOF'
# Work Log

### 2026-08-06

- **Issue #5600** (closed): some later, unrelated issue
EOF

# Simulate 60 intervening issues closing after the out-of-order issue A
# (#5510) but before this tick, plus the already-recorded issue #5600. A
# fixed `--limit 50 --state closed` query (no date bound) sorted
# newest-first would truncate before ever reaching #5510. The date-bounded
# query the fix uses has no such count ceiling, so it still surfaces #5510.
WIDE_CANDIDATES=$(jq -n '
  [{number: 5510, title: "fix: a blocked issue that stayed open a long time",
    closedAt: "2026-08-06T09:30:06Z"}]
  + [range(5541; 5601) | {
      number: .,
      title: "unrelated closed issue",
      closedAt: "2026-08-06T06:00:00Z"
    }]
')
assert_eq "$(printf '%s' "$WIDE_CANDIDATES" | jq 'length')" "61" \
    "fixture models 61 candidates closed since A, i.e. beyond a --limit 50 cutoff"

RESULT2="$(new_issues "$WIDE_CANDIDATES" "$WORK_LOG2")"
assert_eq "$(printf '%s' "$RESULT2" | jq '[.[] | select(.number == 5510)] | length')" "1" \
    "issue #5510 survives even inside a 61-candidate window a --limit 50 count would have truncated"

# ---------------------------------------------------------------------------
# Test 4: multiple out-of-order issues landing in the same tick are all kept.
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: multiple out-of-order issues in one tick are all recorded"

WORK_LOG3="$SANDBOX/WORK_LOG3.md"
cat > "$WORK_LOG3" <<'EOF'
# Work Log

### 2026-08-06

- **Issue #5490** (closed): some already-recorded, higher-numbered issue
EOF

MULTI_CANDIDATES=$(jq -n '[
  {number: 5470, title: "Issue A: opened first, closed last",  closedAt: "2026-08-06T09:00:00Z"},
  {number: 5480, title: "Issue B: opened second, closed mid",  closedAt: "2026-08-06T08:00:00Z"},
  {number: 5490, title: "some already-recorded, higher-numbered issue", closedAt: "2026-08-06T07:00:00Z"}
]')

RESULT3="$(new_issues "$MULTI_CANDIDATES" "$WORK_LOG3")"
assert_eq "$(printf '%s' "$RESULT3" | jq 'length')" "2" \
    "both out-of-order issues (5470, 5480) are kept; the already-recorded one (5490) is dropped"
assert_eq "$(printf '%s' "$RESULT3" | jq -c '[.[].number] | sort')" "[5470,5480]" \
    "the two out-of-order issue numbers are exactly the ones recovered"

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
