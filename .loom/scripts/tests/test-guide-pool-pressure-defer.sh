#!/usr/bin/env bash
# test-guide-pool-pressure-defer.sh - Regression test for issue #6135
#
# When the sweep queue runs dry, role agents (including Guide) still tick
# every 15-30 minutes, and Guide finds a WORK_LOG/WORK_PLAN delta each time.
# That is precisely when the fleet's Claude account pool tends to be under
# the most pressure -- other roles retrying against a shrinking set of
# available accounts (observed: 12 of 17 pool accounts quota-exhausted)
# while Guide kept filing its own doc-maintenance PRs into the same scarce
# pool. Every Guide-filed PR still has to clear Judge (and possibly Doctor),
# so filing one at exactly the worst time competes with substantive work for
# the resource under the most pressure.
#
# The #6135 fix adds a pool-pressure backoff to Guide's Document Maintenance
# Step 5 (create_docs_pr()), gated by a new Step 4b:
#   - pool_pressure_fraction(): a CHEAP read of the already-refreshed
#     `.loom/tokens/.ranking` file (never a fresh `tokens check --ranking`
#     probe, which would itself burn a real request against the pool this
#     check exists to protect) -- reduced to "fraction of accounts NOT
#     `available`".
#   - last_docs_maintenance_merge_epoch(): epoch of the most recently merged
#     docs-maintenance PR of ANY kind (WORK_LOG, WORK_PLAN, or README) --
#     deliberately NOT filtered to a specific file the way
#     last_work_log_write_epoch()/last_work_plan_write_epoch() are (#5929),
#     because this anchors "how long has Guide gone without shipping
#     ANYTHING", not "since this one file last changed".
#   - should_defer_for_pool_pressure(): compares pool_pressure_fraction()
#     against guide.docsMaintenance.poolPressureThreshold (default 0.70,
#     env > config > default, mirroring buildGate.loadThreshold); at/above
#     threshold, defers UNLESS guide.docsMaintenance.poolPressureMaxDeferSecs
#     (default 14400 = 4h) has elapsed since the last docs-maintenance PR
#     merged -- the "never starves permanently" ceiling (AC3).
#
# Verifies that:
#   1. guide.md defines pool_pressure_fraction(), last_docs_maintenance_merge_
#      epoch(), and should_defer_for_pool_pressure(), wires the last into
#      create_docs_pr() AFTER the "no changes to commit" check but BEFORE the
#      commit (AC1: gates "before opening a WORK_LOG/WORK_PLAN PR"), and
#      reads both config knobs with the documented env-var + default names.
#   2. THE REGRESSION, executed rather than grepped:
#      a. pool_pressure_fraction() itself, sourced verbatim from guide.md and
#         run against real `.ranking` fixtures in a throwaway git repo --
#         mixed statuses, an empty file, and a missing file.
#      b. last_docs_maintenance_merge_epoch()'s jq filter, executed against a
#         fixture that proves it is NOT restricted to a specific file (a
#         README-only docs PR still anchors the clock -- the opposite of
#         last_work_plan_write_epoch()'s #5929 fix, and deliberately so).
#      c. A reconstruction of should_defer_for_pool_pressure()'s decision
#         arithmetic, run against fixture threshold/epoch values -- below
#         threshold, at/above threshold within the max-defer ceiling,
#         at/above threshold past the ceiling, and an empty merge history.
#
# Hermetic: throwaway git repo under mktemp -d for the ranking-file test,
# pure jq/arithmetic against fixture values for the rest. No forge, network,
# or `gh` calls.

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

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 1: guide.md defines and wires the pool-pressure backoff
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines and wires the pool-pressure backoff"

assert_grep 'pool_pressure_fraction\(\) \{' "$GUIDE_MD" \
    "pool_pressure_fraction() is defined"
assert_grep '\.loom/tokens/\.ranking' "$GUIDE_MD" \
    "pool_pressure_fraction() reads .loom/tokens/.ranking"
assert_grep 'last_docs_maintenance_merge_epoch\(\) \{' "$GUIDE_MD" \
    "last_docs_maintenance_merge_epoch() is defined"
assert_grep 'should_defer_for_pool_pressure\(\) \{' "$GUIDE_MD" \
    "should_defer_for_pool_pressure() is defined"
assert_grep 'LOOM_GUIDE_POOL_PRESSURE_THRESHOLD' "$GUIDE_MD" \
    "should_defer_for_pool_pressure() reads LOOM_GUIDE_POOL_PRESSURE_THRESHOLD"
assert_grep 'LOOM_GUIDE_POOL_PRESSURE_MAX_DEFER_SECS' "$GUIDE_MD" \
    "should_defer_for_pool_pressure() reads LOOM_GUIDE_POOL_PRESSURE_MAX_DEFER_SECS"
assert_grep 'guide\.docsMaintenance\.poolPressureThreshold // 0\.7' "$GUIDE_MD" \
    "the config fallback default for poolPressureThreshold is 0.7 (70%)"
assert_grep 'guide\.docsMaintenance\.poolPressureMaxDeferSecs // 14400' "$GUIDE_MD" \
    "the config fallback default for poolPressureMaxDeferSecs is 14400 (4h)"

# should_defer_for_pool_pressure() must call last_docs_maintenance_merge_epoch()
# reusing GUIDE_DOCS_PR_EXCLUDE, not redefining the docs-PR predicate.
SDFPP_BODY="$(awk '/^should_defer_for_pool_pressure\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"
if [[ "$SDFPP_BODY" == *'last_docs_maintenance_merge_epoch'* ]]; then
    pass "should_defer_for_pool_pressure() calls last_docs_maintenance_merge_epoch()"
else
    fail "should_defer_for_pool_pressure() never calls last_docs_maintenance_merge_epoch()"
fi

LDME_BODY="$(awk '/^last_docs_maintenance_merge_epoch\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"
if [[ "$LDME_BODY" == *'select($GUIDE_DOCS_PR_EXCLUDE)'* ]]; then
    pass "last_docs_maintenance_merge_epoch() reuses GUIDE_DOCS_PR_EXCLUDE rather than redefining the docs-PR predicate"
else
    fail "last_docs_maintenance_merge_epoch() does not reuse GUIDE_DOCS_PR_EXCLUDE"
fi
# #5929-style file filter must be ABSENT here -- this anchor is deliberately
# unfiltered by file, unlike last_work_log_write_epoch()/last_work_plan_write_epoch().
if [[ "$LDME_BODY" == *'index('* ]]; then
    fail "last_docs_maintenance_merge_epoch() must NOT filter by a specific file (index(...)) -- it anchors on ANY docs-maintenance merge"
else
    pass "last_docs_maintenance_merge_epoch() is not filtered to a specific file (anchors on ANY docs-maintenance merge)"
fi

# ---------------------------------------------------------------------------
# Test 2: create_docs_pr() calls the defer check AFTER "no changes to commit"
# but BEFORE the commit -- AC1: gates "before opening a WORK_LOG/WORK_PLAN PR".
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: create_docs_pr() gates the pressure check before committing/filing"

NO_CHANGES_LINE="$(grep -n 'No document changes to commit\.' "$GUIDE_MD" | head -1 | cut -d: -f1)"
DEFER_CALL_LINE="$(grep -n 'if should_defer_for_pool_pressure; then' "$GUIDE_MD" | head -1 | cut -d: -f1)"
COMMIT_LINE="$(grep -n 'git -C "\$DOCS_WT" commit -m "docs: update WORK_LOG' "$GUIDE_MD" | head -1 | cut -d: -f1)"

if [[ -n "$NO_CHANGES_LINE" && -n "$DEFER_CALL_LINE" && "$NO_CHANGES_LINE" -lt "$DEFER_CALL_LINE" ]]; then
    pass "the pressure check runs AFTER the 'no changes to commit' guard (line $NO_CHANGES_LINE < $DEFER_CALL_LINE)"
else
    fail "pressure check (line ${DEFER_CALL_LINE:-?}) must run after the 'no changes' guard (line ${NO_CHANGES_LINE:-?})"
fi

if [[ -n "$DEFER_CALL_LINE" && -n "$COMMIT_LINE" && "$DEFER_CALL_LINE" -lt "$COMMIT_LINE" ]]; then
    pass "the pressure check runs BEFORE the commit (line $DEFER_CALL_LINE < $COMMIT_LINE)"
else
    fail "pressure check (line ${DEFER_CALL_LINE:-?}) must run before the commit (line ${COMMIT_LINE:-?})"
fi

# The defer branch must release the lock and return, exactly like the other
# early-exit paths (Step 1's skip, create_docs_pr()'s "no changes").
DEFER_BLOCK="$(sed -n "${DEFER_CALL_LINE},${COMMIT_LINE}p" "$GUIDE_MD" 2>/dev/null || true)"
if [[ -n "$DEFER_BLOCK" ]] && grep -q 'docs-guide-lock\.sh release' <<<"$DEFER_BLOCK" && grep -q 'return' <<<"$DEFER_BLOCK"; then
    pass "the defer branch releases the docs-guide lock and returns before the commit"
else
    fail "expected a release+return between the pressure check (line ${DEFER_CALL_LINE:-?}) and the commit (line ${COMMIT_LINE:-?})"
fi

# ---------------------------------------------------------------------------
# Test 3: THE REGRESSION (a) -- pool_pressure_fraction() sourced verbatim and
# executed against real `.ranking` fixtures in a throwaway git repo.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: pool_pressure_fraction(), executed against real .ranking fixtures"

PPF_SRC="$(awk '/^pool_pressure_fraction\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"

if [[ -z "$PPF_SRC" ]]; then
    fail "could not extract pool_pressure_fraction() from guide.md"
else
    pass "extracted pool_pressure_fraction() from guide.md"

    SANDBOX="$(mktemp -d)"
    cleanup_sandbox() { rm -rf "$SANDBOX"; }
    trap cleanup_sandbox EXIT

    PPF_SCRIPT="$SANDBOX/ppf.sh"
    printf '%s\n' "$PPF_SRC" > "$PPF_SCRIPT"

    (cd "$SANDBOX" && git init -q)
    mkdir -p "$SANDBOX/.loom/tokens"

    # Fixture: 3 of 5 accounts NOT `available` (exhausted x2, rate_limited x1)
    # -- mirrors the #6135 incident shape (a majority-unavailable pool).
    cat > "$SANDBOX/.loom/tokens/.ranking" <<'EOF'
alice|available|0.20|
bob|exhausted|0.99|2026-08-20T00:00:00Z
carol|exhausted|0.99|2026-08-20T00:00:00Z
dave|rate_limited|0.85|2026-08-13T10:00:00Z
eve|available|0.10|
EOF

    RESULT="$(cd "$SANDBOX" && source "$PPF_SCRIPT" && pool_pressure_fraction)"
    assert_eq "$RESULT" "0.6000" "3 of 5 unavailable accounts -> fraction 0.6000"

    # A healthy pool -- 1 of 5 unavailable.
    cat > "$SANDBOX/.loom/tokens/.ranking" <<'EOF'
alice|available|0.20|
bob|available|0.30|
carol|available|0.15|
dave|rate_limited|0.85|2026-08-13T10:00:00Z
eve|available|0.10|
EOF
    RESULT="$(cd "$SANDBOX" && source "$PPF_SCRIPT" && pool_pressure_fraction)"
    assert_eq "$RESULT" "0.2000" "1 of 5 unavailable accounts -> fraction 0.2000"

    # Empty ranking file -- fails open (0 pressure), never a divide-by-zero
    # or a corrupted multi-line capture.
    : > "$SANDBOX/.loom/tokens/.ranking"
    RESULT="$(cd "$SANDBOX" && source "$PPF_SCRIPT" && pool_pressure_fraction)"
    assert_eq "$RESULT" "0" "an empty .ranking file fails open (fraction 0, not an error)"

    # Missing ranking file -- fails open.
    rm -f "$SANDBOX/.loom/tokens/.ranking"
    RESULT="$(cd "$SANDBOX" && source "$PPF_SCRIPT" && pool_pressure_fraction)"
    assert_eq "$RESULT" "0" "a missing .ranking file fails open (fraction 0)"

    trap - EXIT
    cleanup_sandbox
fi

# ---------------------------------------------------------------------------
# Test 4: THE REGRESSION (b) -- last_docs_maintenance_merge_epoch()'s jq
# filter, executed against a fixture proving it anchors on ANY
# docs-maintenance merge, not one restricted to a specific file.
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: last_docs_maintenance_merge_epoch()'s jq filter, executed against a fixture"

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
else
    JQ_EXPR="$(awk '/^last_docs_maintenance_merge_epoch\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD" \
        | grep -m1 -- '--jq' | sed -E 's/^.*--jq "(.*)"\)$/\1/')"

    if [[ -n "$JQ_EXPR" ]]; then
        pass "extracted the --jq expression from last_docs_maintenance_merge_epoch()"
    else
        fail "could not extract the --jq expression from last_docs_maintenance_merge_epoch()"
    fi

    EXCLUDE_LINE="$(grep -m1 '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD")"
    GUIDE_DOCS_PR_EXCLUDE="${EXCLUDE_LINE#GUIDE_DOCS_PR_EXCLUDE=}"
    GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE#\'}"
    GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE%\'}"

    # Fixture: the MOST RECENTLY merged docs-maintenance PR touched ONLY
    # README.md (would be REJECTED as the anchor for last_work_plan_write_
    # epoch()/last_work_log_write_epoch(), which filter by file) -- here it
    # MUST still be selected, because this anchor asks "when did Guide last
    # ship anything", not "when did this one file last change". A genuine
    # (non-docs) PR merged even more recently must never be selected either.
    FIXTURE_JSON=$(jq -n '[
      {number: 6060, title: "docs: Guide document maintenance update",
       mergedAt: "2026-08-13T10:00:00Z", headRefName: "docs/guide-update-20260813-100000",
       files: [{path: "WORK_LOG.md"}]},
      {number: 6090, title: "docs: Guide document maintenance update",
       mergedAt: "2026-08-13T20:00:00Z", headRefName: "docs/guide-update-20260813-200000",
       files: [{path: "README.md"}]},
      {number: 6070, title: "feat(cli): add tokens check --ranking",
       mergedAt: "2026-08-13T21:00:00Z", headRefName: "feature/issue-6070",
       files: [{path: "WORK_LOG.md"}]}
    ]')

    RESULT="$(printf '%s\n' "$FIXTURE_JSON" | eval "jq -r \"$JQ_EXPR\"" 2>/dev/null)"
    assert_eq "$RESULT" "2026-08-13T20:00:00Z" \
        "the README-only docs PR (#6090, merged most recently among docs PRs) IS selected -- this anchor is not file-restricted; the later non-docs PR (#6070) is never eligible"
fi

# ---------------------------------------------------------------------------
# Reconstruct should_defer_for_pool_pressure()'s decision arithmetic exactly
# as it is performed: $1 = fraction, $2 = threshold, $3 = last_merged_epoch,
# $4 = now_epoch, $5 = max_defer_secs. Echoes "DEFER" or "FILE".
# ---------------------------------------------------------------------------
pool_pressure_decision() {
    local fraction="$1" threshold="$2" last_merged_epoch="$3" now_epoch="$4" max_defer="$5"
    local elapsed

    if awk -v f="$fraction" -v t="$threshold" 'BEGIN { exit !(f < t) }'; then
        echo "FILE"
        return
    fi

    elapsed=$((now_epoch - last_merged_epoch))
    if [[ "$last_merged_epoch" -gt 0 ]] && [[ "$elapsed" -lt "$max_defer" ]]; then
        echo "DEFER"
        return
    fi

    echo "FILE"
}

THRESHOLD=0.7
MAX_DEFER=14400

# ---------------------------------------------------------------------------
# Test 5: below threshold -- files exactly as today, regardless of history.
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: pressure below threshold files immediately (unchanged behavior)"

DECISION="$(pool_pressure_decision 0.3 "$THRESHOLD" 1000000 1000300 "$MAX_DEFER")"
assert_eq "$DECISION" "FILE" "fraction 0.3 (< 0.7 threshold) files immediately"

DECISION="$(pool_pressure_decision 0.69 "$THRESHOLD" 0 1000000 "$MAX_DEFER")"
assert_eq "$DECISION" "FILE" "fraction 0.69 (just under threshold) files immediately, even with no merge history"

# ---------------------------------------------------------------------------
# Test 6: at/above threshold, within the max-defer ceiling -- deferred (AC2).
# ---------------------------------------------------------------------------
echo ""
echo "Test 6: pressure at/above threshold defers, within the max-defer ceiling"

LAST_MERGE=1000000
INSIDE_CEILING=$((LAST_MERGE + 3600))   # 1h after last merge, < 14400s ceiling
DECISION="$(pool_pressure_decision 0.7 "$THRESHOLD" "$LAST_MERGE" "$INSIDE_CEILING" "$MAX_DEFER")"
assert_eq "$DECISION" "DEFER" "fraction == threshold (0.7) defers (boundary counts as pressure)"

DECISION="$(pool_pressure_decision 0.85 "$THRESHOLD" "$LAST_MERGE" "$INSIDE_CEILING" "$MAX_DEFER")"
assert_eq "$DECISION" "DEFER" "fraction 0.85 (observed #6135 incident shape) defers within the ceiling"

# ---------------------------------------------------------------------------
# Test 7: at/above threshold, PAST the max-defer ceiling -- files anyway
# (AC3: doc maintenance never starves permanently).
# ---------------------------------------------------------------------------
echo ""
echo "Test 7: the max-defer ceiling always wins -- doc maintenance never starves"

PAST_CEILING=$((LAST_MERGE + MAX_DEFER + 1))
DECISION="$(pool_pressure_decision 0.85 "$THRESHOLD" "$LAST_MERGE" "$PAST_CEILING" "$MAX_DEFER")"
assert_eq "$DECISION" "FILE" "fraction 0.85, past the 14400s ceiling, files anyway despite sustained pressure"

AT_BOUNDARY=$((LAST_MERGE + MAX_DEFER))
DECISION="$(pool_pressure_decision 0.85 "$THRESHOLD" "$LAST_MERGE" "$AT_BOUNDARY" "$MAX_DEFER")"
assert_eq "$DECISION" "FILE" "elapsed == max_defer is treated as the ceiling having elapsed (not deferred)"

JUST_BEFORE=$((LAST_MERGE + MAX_DEFER - 1))
DECISION="$(pool_pressure_decision 0.85 "$THRESHOLD" "$LAST_MERGE" "$JUST_BEFORE" "$MAX_DEFER")"
assert_eq "$DECISION" "DEFER" "one second before the ceiling elapses, sustained pressure still defers"

# ---------------------------------------------------------------------------
# Test 8: no prior docs-maintenance PR ever merged (last_merged_epoch == 0)
# -- must never be treated as "infinitely deferred"; files immediately.
# ---------------------------------------------------------------------------
echo ""
echo "Test 8: an empty docs-maintenance history never defers indefinitely"

DECISION="$(pool_pressure_decision 0.9 "$THRESHOLD" 0 1000000 "$MAX_DEFER")"
assert_eq "$DECISION" "FILE" "last_merged_epoch == 0 (no prior docs-maintenance PR) files immediately, even under high pressure"

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
