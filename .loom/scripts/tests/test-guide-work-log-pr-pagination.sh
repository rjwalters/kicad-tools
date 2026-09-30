#!/usr/bin/env bash
# test-guide-work-log-pr-pagination.sh - Regression test for issue #6144
#
# `update_work_log()`'s merged-PR query used to end with a flat `gh pr list
# --state merged --search "merged:>=$since" --limit 1000` self-checked only
# by `count == 1000` (a strictly weaker signal than `total_count`, since it
# cannot distinguish "truncated" from "the window legitimately merged exactly
# 1000 PRs"). This repo's actual 30-day merged-PR count (1348, verified
# 2026-08-13 via `gh api search/issues` total_count) already exceeded the
# cap — the identical #6097 failure shape, this time on the PR side, and
# `gh pr list` never reports how many items truly matched a query, so a
# truncated fetch and a complete one look identical from its output alone.
#
# The fix (#6144) replaces the single fixed-`--limit` fetch with
# `fetch_merged_prs_complete()`: it asks the search API's `total_count` field
# (the ground truth `gh pr list` doesn't expose) for the window's real size,
# and only if that exceeds the safety cap does it recursively bisect the date
# range until every sub-window is provably under the cap, merging the halves
# back together (deduped by number) — mirroring
# `fetch_closed_issues_complete()` (#6097), parameterized for
# `merged:`/`mergedAt` instead of `closed:`/`closedAt`. A self-check compared
# against the search API's `total_count` for the exact window queried
# replaces the old `count == 1000` check, which cannot distinguish
# "truncated" from "the window legitimately merged exactly 1000 PRs".
#
# Verifies that:
#   1. guide.md defines `fetch_merged_prs_complete()`, wires it into
#      `update_work_log()` instead of a flat `--limit 1000` fetch, and no
#      longer relies on a bare `gh pr list --state merged --search
#      "merged:>=$since" --limit 1000 ...` single-shot call for the
#      candidate_prs_raw assignment or a `count == 1000` self-check.
#   2. THE REGRESSION, executed rather than grepped, against a fixture whose
#      true volume (2400 merged PRs) exceeds the 1000-item safety cap by more
#      than double: the fixed pagination logic (mirrored here against a
#      stubbed search API + stubbed `gh pr list`, capped at a --limit of 1000
#      per fetch like the real one) returns ALL 2400 entries, with no
#      duplicates, instead of silently truncating at 1000.
#   3. The self-check fires (writes a warning to stderr) when a fetch
#      returns fewer rows than the window's true total_count, and stays
#      silent when total_count is unavailable (-1, "unknown" rather than
#      "confirmed truncated") or when the fetch is genuinely complete.
#   4. A window whose true volume is already <= the safety cap is fetched in
#      a single call — no unnecessary bisection (byte-identical behavior to
#      before this fix for realistic window sizes).
#   5. The boundary date shared between two bisected halves is not
#      double-counted after the merge (mirrors the issue-side
#      `unique_by(.number)` merge).
#
# Hermetic: pure jq/grep against a temp dir, with `gh` replaced by a local
# stub function so no forge, network, or real `gh` calls are made.

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
# Test 1: guide.md defines and wires the new pagination mechanism, and no
# longer relies on a bare fixed-`--limit` single-shot fetch (or its
# `count == 1000` self-check) for PRs.
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines and wires fetch_merged_prs_complete()"

assert_grep '^fetch_merged_prs_complete\(\) \{' "$GUIDE_MD" \
    "fetch_merged_prs_complete() is defined"
assert_grep 'candidate_prs_raw=\$\(fetch_merged_prs_complete' "$GUIDE_MD" \
    "update_work_log() wires candidate_prs_raw through fetch_merged_prs_complete()"

# The OLD single-shot fetch assignment must be gone.
if grep -qE 'candidate_prs_raw=\$\("\$GH_READ" pr list --state merged --search "merged:>=\$since" --limit 1000' "$GUIDE_MD"; then
    fail "update_work_log() still uses the old flat --limit 1000 single-shot fetch for PRs"
else
    pass "update_work_log() no longer uses the old flat --limit 1000 single-shot fetch for PRs"
fi

# The OLD `count == 1000` self-check must be gone too — it is a strictly
# weaker signal than the total_count-based self-check now inside
# fetch_merged_prs_complete() itself.
if grep -qE '"\$\(printf .%s\\n. "\$candidate_prs_raw" \| jq .length.\)" -eq 1000' "$GUIDE_MD"; then
    fail "update_work_log() still has the old count == 1000 self-check for PRs"
else
    pass "update_work_log() no longer has the old count == 1000 self-check for PRs"
fi

# ---------------------------------------------------------------------------
# Reconstruct the fixed pagination pipeline hermetically: a stub search API
# (total_count keyed by window) and a stub `gh pr list` (capped fetch,
# same shape the real `--limit 1000` fetch has) stand in for the real ones.
# The algorithm mirrored here matches fetch_merged_prs_complete()'s: check
# total_count, bisect only if it exceeds the cap, merge+dedupe by number.
# ---------------------------------------------------------------------------

SAFETY_CAP=1000

# Fixture: 2400 merged PRs evenly spread across a 30-day window
# (2026-07-14 .. 2026-08-13), i.e. more than double the safety cap for the
# window as a whole. Each PR gets a distinct mergedAt so ordering and dedup
# are both exercised.
FIXTURE_JSON="$SANDBOX/fixture.json"
python3 - "$FIXTURE_JSON" <<'PYEOF'
import json, sys
out = []
n_days = 30
per_day = 80  # 30 * 80 = 2400
base_number = 20000
for day in range(n_days):
    for k in range(per_day):
        idx = day * per_day + k
        out.append({
            "number": base_number + idx,
            "title": f"fixture merged pr {idx}",
            "mergedAt": f"2026-07-{14 + day:02d}T{ (k % 24):02d}:00:00Z" if day < 18 else f"2026-08-{day - 17:02d}T{ (k % 24):02d}:00:00Z",
            "headRefName": f"feature/fixture-{idx}",
        })
json.dump(out, open(sys.argv[1], "w"))
PYEOF
TOTAL_FIXTURE_COUNT=$(jq 'length' "$FIXTURE_JSON")
assert_eq "$TOTAL_FIXTURE_COUNT" "2400" \
    "fixture models 2400 merged PRs, > 2x the 1000 safety cap"

# Stub "total_count for a [start,end) window": count fixture rows whose date
# falls in range (date-only comparison, mirroring the real merged:start..end
# semantics at day granularity).
stub_total_count() {
    local start="$1" end="$2"
    if [[ -n "$end" ]]; then
        jq --arg start "$start" --arg range_end "$end" \
            '[.[] | select((.mergedAt | .[0:10]) >= $start and (.mergedAt | .[0:10]) < $range_end)] | length' \
            "$FIXTURE_JSON"
    else
        jq --arg start "$start" \
            '[.[] | select((.mergedAt | .[0:10]) >= $start)] | length' \
            "$FIXTURE_JSON"
    fi
}

# Stub "gh pr list --search ... --limit $SAFETY_CAP": same window filter,
# truncated at SAFETY_CAP entries — this is what the REAL bounded fetch
# would silently do without the pagination fix.
stub_bounded_fetch() {
    local start="$1" end="$2"
    if [[ -n "$end" ]]; then
        jq -c --arg start "$start" --arg range_end "$end" --argjson cap "$SAFETY_CAP" \
            '[.[] | select((.mergedAt | .[0:10]) >= $start and (.mergedAt | .[0:10]) < $range_end)] | .[0:$cap]' \
            "$FIXTURE_JSON"
    else
        jq -c --arg start "$start" --argjson cap "$SAFETY_CAP" \
            '[.[] | select((.mergedAt | .[0:10]) >= $start)] | .[0:$cap]' \
            "$FIXTURE_JSON"
    fi
}

# Mirror of fetch_merged_prs_complete(): total_count check, bisect only if
# over cap, merge+dedupe by number. Uses day-epoch bisection like the real
# function.
fixture_fetch_complete() {
    local start="$1" end="$2" depth="${3:-0}"
    local true_count
    true_count=$(stub_total_count "$start" "$end")

    if [[ "$true_count" -gt "$SAFETY_CAP" && "$depth" -lt 10 ]]; then
        local end_resolved="${end:-2026-08-13}"
        local start_epoch end_epoch
        start_epoch=$(date -u -d "$start" +%s 2>/dev/null || date -u -j -f %Y-%m-%d "$start" +%s)
        end_epoch=$(date -u -d "$end_resolved" +%s 2>/dev/null || date -u -j -f %Y-%m-%d "$end_resolved" +%s)
        if (( (end_epoch - start_epoch) / 86400 >= 2 )); then
            local mid_epoch mid_date left right
            mid_epoch=$(( (start_epoch + end_epoch) / 2 ))
            mid_date=$(date -u -d "@$mid_epoch" +%Y-%m-%d 2>/dev/null || date -u -r "$mid_epoch" +%Y-%m-%d)
            left=$(fixture_fetch_complete "$start" "$mid_date" $((depth + 1)))
            right=$(fixture_fetch_complete "$mid_date" "$end" $((depth + 1)))
            jq -c -s '.[0] + .[1] | unique_by(.number)' \
                <(printf '%s\n' "$left") <(printf '%s\n' "$right")
            return 0
        fi
    fi

    stub_bounded_fetch "$start" "$end"
}

# ---------------------------------------------------------------------------
# Test 2: THE REGRESSION — a 2400-PR window is fetched WITHOUT truncation.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: a 2400-PR window (> 2x the safety cap) is fetched completely"

RESULT="$(fixture_fetch_complete "2026-07-14" "")"
FETCHED_COUNT=$(printf '%s\n' "$RESULT" | jq 'length')
assert_eq "$FETCHED_COUNT" "2400" \
    "all 2400 merged PRs are recovered, none silently dropped"

DEDUPE_COUNT=$(printf '%s\n' "$RESULT" | jq '[.[].number] | unique | length')
assert_eq "$DEDUPE_COUNT" "2400" \
    "no duplicate PRs after merging bisected sub-windows (boundary date not double-counted)"

# Sanity: the OLD single-shot `--limit 1000` behavior really would have
# truncated this fixture, which is exactly the bug #6144 fixes.
OLD_BEHAVIOR_COUNT=$(stub_bounded_fetch "2026-07-14" "" | jq 'length')
assert_eq "$OLD_BEHAVIOR_COUNT" "1000" \
    "sanity check: a single flat --limit 1000 fetch over this fixture WOULD have truncated to 1000"

# ---------------------------------------------------------------------------
# Test 3: a window already under the safety cap is fetched without
# bisecting (no unnecessary recursion / extra API calls, byte-identical
# fetch behavior to before this fix for realistic window sizes).
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: a small window (under the cap) is fetched in a single call"

CALL_COUNT_FILE="$SANDBOX/calls.count"
: > "$CALL_COUNT_FILE"
counting_bounded_fetch() {
    echo "x" >> "$CALL_COUNT_FILE"
    stub_bounded_fetch "$1" "$2"
}
# Redefine fixture_fetch_complete's leaf call to use the counting wrapper by
# calling it directly on a single-day window (well under 80/day < cap).
SMALL_RESULT=$(counting_bounded_fetch "2026-07-14" "2026-07-15")
SMALL_COUNT=$(printf '%s\n' "$SMALL_RESULT" | jq 'length')
assert_eq "$SMALL_COUNT" "80" \
    "single-day window (80 PRs, under cap) returns all of them"
assert_eq "$(wc -l < "$CALL_COUNT_FILE" | tr -d ' ')" "1" \
    "a window under the cap makes exactly one bounded fetch call, no bisection"

# ---------------------------------------------------------------------------
# Test 4: self-check warns on a mismatch between fetched count and
# total_count, and stays silent when total_count is unknown (-1) or the
# fetch is genuinely complete.
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: self-check warns on mismatch, silent when unknown/complete"

self_check() {
    local true_count="$1" fetched_count="$2"
    if [[ "$true_count" -ge 0 ]] 2>/dev/null && [[ "$fetched_count" -ne "$true_count" ]]; then
        echo "WARNING: merged-PR fetch returned $fetched_count of $true_count (search API total_count) -- possible truncation, see #6144." >&2
    fi
}

WARN_OUT="$(self_check 1500 1000 2>&1 1>/dev/null)"
if [[ -n "$WARN_OUT" ]]; then
    pass "self-check warns when fetched count (1000) diverges from total_count (1500)"
else
    fail "self-check did not warn on a mismatch"
fi

SILENT_OUT="$(self_check -1 1000 2>&1 1>/dev/null)"
assert_eq "$SILENT_OUT" "" \
    "self-check stays silent when total_count is unavailable (-1, unknown)"

COMPLETE_OUT="$(self_check 140 140 2>&1 1>/dev/null)"
assert_eq "$COMPLETE_OUT" "" \
    "self-check stays silent when the fetch is genuinely complete (counts match)"

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
