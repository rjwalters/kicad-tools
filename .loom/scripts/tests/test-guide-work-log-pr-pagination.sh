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
# The shipped functions are sourced from guide.md (not mirrored). Hermetic: pure jq/grep against a temp dir, with `gh` replaced by a local
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

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then pass "$msg"; else fail "$msg (got '$actual', expected '$expected')"; fi
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
# Load the SHIPPED functions out of guide.md (no mirrored copy of the
# algorithm) and drive them against a stubbed `gh`: a stub search API
# (total_count keyed by window) and a stub `gh pr list` (capped fetch, same
# shape the real `--limit 1000` fetch has) stand in for the real ones.
# ---------------------------------------------------------------------------

FUNCS_SRC="$SANDBOX/funcs.sh"
sed -n '/^_work_log_search_total_count() {/,/^update_work_log() {/p' "$GUIDE_MD" \
    | sed '$d' > "$FUNCS_SRC"
if [[ -s "$FUNCS_SRC" ]] && bash -n "$FUNCS_SRC"; then
    pass "pagination functions extracted from guide.md and parse cleanly"
else
    fail "could not extract the pagination functions from guide.md"
fi
# shellcheck disable=SC1090
source "$FUNCS_SRC"

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

CALL_COUNT_FILE="$SANDBOX/calls.count"
: > "$CALL_COUNT_FILE"
# Per-test knob for the self-check test: "" = honest total_count, a number =
# override, "fail" = the search API call fails (total_count unknown).
STUB_TOTAL_OVERRIDE=""

# The pagination functions call `git remote get-url origin`; give them a repo.
STUB_REPO="$SANDBOX/repo"
git init -q "$STUB_REPO"
git -C "$STUB_REPO" remote add origin git@github.com:example/stub.git
cd "$STUB_REPO" || exit 1

# Window filter shared by the stub search API and the stub list: parses the
# `<field>:<start>..<end>` / `<field>:>=<start>` token the real code builds.
stub_window_filter() {
    local range="$1" cap="$2" fields="$3" mode="$4" spec start end
    spec="${range#*:}"
    if [[ "$spec" == '>='* ]]; then start="${spec#>=}"; end=""; else start="${spec%%..*}"; end="${spec##*..}"; fi
    jq -c --arg start "$start" --arg range_end "$end" --argjson cap "$cap" \
        --arg fields "$fields" --arg mode "$mode" '
        [.[] | select((.mergedAt | .[0:10]) >= $start and ($range_end == "" or (.mergedAt | .[0:10]) < $range_end))] as $hits
        | if $mode == "count" then ($hits | length)
          else ($hits | .[0:$cap]
                | map(with_entries(select(.key as $k | ($fields | split(",") | index($k)))))) end' \
        "$FIXTURE_JSON"
}

# Stub `gh`: `api -X GET search/issues -f q=... --jq .total_count` and
# `pr list --state S --search RANGE --limit N --json FIELDS`.
stub_gh() {
    local sub="$1"; shift
    if [[ "$sub" == "api" ]]; then
        [[ "$STUB_TOTAL_OVERRIDE" == "fail" ]] && return 1
        local q="" prev=""
        for a in "$@"; do [[ "$prev" == "-f" ]] && q="${a#q=}"; prev="$a"; done
        if [[ -n "$STUB_TOTAL_OVERRIDE" ]]; then echo "$STUB_TOTAL_OVERRIDE"; return 0; fi
        stub_window_filter "${q##* }" 0 "" count
    else
        echo "x" >> "$CALL_COUNT_FILE"
        local search="" limit="" fields="" prev=""
        for a in "$@"; do
            case "$prev" in
                --search) search="$a" ;;
                --limit) limit="$a" ;;
                --json) fields="$a" ;;
            esac
            prev="$a"
        done
        stub_window_filter "$search" "$limit" "$fields" list
    fi
}
export GH_READ=stub_gh  # read by the sourced pagination functions

# ---------------------------------------------------------------------------
# Test 2: THE REGRESSION — a 2400-pr window is fetched WITHOUT truncation.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: a 2400-pr window (> 2x the safety cap) is fetched completely"

RESULT="$(fetch_merged_prs_complete "2026-07-14" "")"
FETCHED_COUNT=$(printf '%s\n' "$RESULT" | jq 'length')
assert_eq "$FETCHED_COUNT" "2400" \
    "all 2400 merged PRs are recovered, none silently dropped"

DEDUPE_COUNT=$(printf '%s\n' "$RESULT" | jq '[.[].number] | unique | length')
assert_eq "$DEDUPE_COUNT" "2400" \
    "no duplicate items after merging bisected sub-windows (boundary date not double-counted)"

# Sanity: the OLD single-shot `--limit 1000` behavior really would have
# truncated this fixture, which is exactly the bug #6144 fixes.
OLD_BEHAVIOR_COUNT=$(stub_window_filter "merged:>=2026-07-14" "$SAFETY_CAP" "number" list | jq 'length')
assert_eq "$OLD_BEHAVIOR_COUNT" "1000" \
    "sanity check: a single flat --limit 1000 fetch over this fixture WOULD have truncated to 1000"

# update_work_log()'s $GUIDE_DOCS_PR_EXCLUDE filter reads .headRefName.
NULL_HEADS=$(printf '%s\n' "$RESULT" | jq '[.[] | select(.headRefName == null)] | length')
assert_eq "$NULL_HEADS" "0" \
    "headRefName is requested from gh, so the docs-PR exclude filter has its field"

# ---------------------------------------------------------------------------
# Test 3: a window already under the safety cap is fetched without
# bisecting (no unnecessary recursion / extra API calls).
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: a small window (under the cap) is fetched in a single call"

: > "$CALL_COUNT_FILE"
SMALL_RESULT=$(fetch_merged_prs_complete "2026-07-14" "2026-07-15")
SMALL_COUNT=$(printf '%s\n' "$SMALL_RESULT" | jq 'length')
assert_eq "$SMALL_COUNT" "80" \
    "single-day window (80 items, under cap) returns all of them"
assert_eq "$(wc -l < "$CALL_COUNT_FILE" | tr -d ' ')" "1" \
    "a window under the cap makes exactly one bounded fetch call, no bisection"

# ---------------------------------------------------------------------------
# Test 4: the real self-check warns on a mismatch between fetched count and
# total_count, and stays silent when total_count is unknown (-1) or the
# fetch is genuinely complete.
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: self-check warns on mismatch, silent when unknown/complete"

STUB_TOTAL_OVERRIDE=90   # API claims 90; the stub list only has 80
WARN_OUT="$(fetch_merged_prs_complete "2026-07-14" "2026-07-15" 2>&1 1>/dev/null)"
if [[ "$WARN_OUT" == *"merged-PR fetch"*"returned 80 of 90"*"#6144"* ]]; then
    pass "self-check warns when fetched count (80) diverges from total_count (90)"
else
    fail "self-check did not warn on a mismatch (got: '$WARN_OUT')"
fi

STUB_TOTAL_OVERRIDE=fail
SILENT_OUT="$(fetch_merged_prs_complete "2026-07-14" "2026-07-15" 2>&1 1>/dev/null)"
assert_eq "$SILENT_OUT" "" \
    "self-check stays silent when total_count is unavailable (-1, unknown)"

STUB_TOTAL_OVERRIDE=""
COMPLETE_OUT="$(fetch_merged_prs_complete "2026-07-14" "2026-07-15" 2>&1 1>/dev/null)"
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
