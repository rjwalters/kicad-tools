#!/usr/bin/env bash
# test-guide-work-log-debounce.sh - Regression test for issue #6133
#
# Between 2026-08-13's PRs #6088-#6091, 4 near-identical `docs: Guide document
# maintenance update` PRs merged in ~3h for ~20 total net lines -- driven by
# `update_work_log()` filing a fresh PR on the very next tick that found ANY
# new merged PR or closed issue not yet recorded (subject only to the "one
# open docs PR at a time" lock, which prevents overlapping PRs, not cadence).
# WORK_PLAN.md already had a time-based debounce for the analogous problem
# (#5890/#5929) -- this issue extends batching to WORK_LOG.md.
#
# Unlike WORK_PLAN's periodic full-regenerate, WORK_LOG is
# append-only/event-driven: entries only need to be recorded before a prior
# tick's presence-check window ages them out, not "correct as of right now".
# So the #6133 fix batches pending entries via TWO knobs evaluated together
# (mirroring `LOOM_WORK_PLAN_DEBOUNCE_SECS`'s shape, plus an entry-count
# escape hatch WORK_PLAN has no equivalent of):
#   - `LOOM_WORK_LOG_MIN_ENTRIES` (default 5): a pending delta at or above
#     this size writes IMMEDIATELY, debounce window or not -- the
#     "no starvation" guarantee for a large accumulated delta.
#   - `LOOM_WORK_LOG_DEBOUNCE_SECS` (default 1800 = 30 min): below the
#     min-entries threshold, a pending delta waits until at least this long
#     has elapsed since WORK_LOG.md was last actually WRITTEN by a merged
#     docs-maintenance PR, anchored via `last_work_log_write_epoch()` (mirrors
#     `last_work_plan_write_epoch()`, #5929's fix, but filtered to merges
#     whose changed files include WORK_LOG.md instead of WORK_PLAN.md).
#
# Verifies that:
#   1. guide.md defines `last_work_log_write_epoch()`, reusing
#      `GUIDE_DOCS_PR_EXCLUDE` rather than redefining the docs-PR predicate,
#      AND filters merged PRs down to ones whose `files` include
#      `WORK_LOG.md` (mirrors #5929's WORK_PLAN.md filter).
#   2. guide.md's `update_work_log()` gates the write behind BOTH
#      `LOOM_WORK_LOG_DEBOUNCE_SECS` (default 1800) and
#      `LOOM_WORK_LOG_MIN_ENTRIES` (default 5).
#   3. THE REGRESSION, executed rather than grepped: a reconstruction of the
#      debounce arithmetic, run against fixture timelines --
#        a. a tick within the debounce window with a small pending delta
#           does not write (batches instead).
#        b. a tick past the debounce window with the same pending delta
#           writes.
#        c. a sufficiently large accumulated delta writes immediately even
#           INSIDE the debounce window (the min-entries escape hatch --
#           the "no starvation" guarantee for large deltas).
#        d. zero pending entries never writes, regardless of window/size.
#        e. the first-ever tick (no prior WORK_LOG-writing docs PR to anchor
#           the debounce clock, last_merged_epoch == 0) still writes
#           immediately for a small delta -- never suppressed by a write
#           that never happened.
#   4. `last_work_log_write_epoch()`'s jq filter, executed against a fixture
#      that mixes a WORK_PLAN-only docs PR (must be skipped) with an older
#      docs PR that actually touched WORK_LOG.md (must be selected).
#
# Hermetic: pure grep/jq/arithmetic against fixture values. No forge,
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
# Test 1: the prompt wires the debounce into update_work_log()
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines and wires the WORK_LOG debounce"

assert_grep 'last_work_log_write_epoch\(\) \{' "$GUIDE_MD" \
    "last_work_log_write_epoch() is defined"
assert_grep 'select\(\$GUIDE_DOCS_PR_EXCLUDE\)' "$GUIDE_MD" \
    "last_work_log_write_epoch() reuses GUIDE_DOCS_PR_EXCLUDE rather than redefining the docs-PR predicate"
assert_grep 'index\(\\"WORK_LOG\.md\\"\)' "$GUIDE_MD" \
    "last_work_log_write_epoch() filters merged PRs to ones whose files include WORK_LOG.md"
assert_grep 'LOOM_WORK_LOG_DEBOUNCE_SECS:-\$\(jq -r' "$GUIDE_MD" \
    "update_work_log() reads LOOM_WORK_LOG_DEBOUNCE_SECS, falling back to a config read (#6327)"
assert_grep 'guide\.docsMaintenance\.workLogDebounceSecs // 1800' "$GUIDE_MD" \
    "update_work_log() reads guide.docsMaintenance.workLogDebounceSecs from .loom/config.json with a 1800s (30 min) default (#6327)"
assert_grep 'LOOM_WORK_LOG_MIN_ENTRIES:-5' "$GUIDE_MD" \
    "update_work_log() reads LOOM_WORK_LOG_MIN_ENTRIES with a 5-entry default"
assert_grep 'update_work_log\(\) \{' "$GUIDE_MD" \
    "update_work_log() is defined"

# update_work_log() must call last_work_log_write_epoch() AFTER the
# "nothing new" (total_new == 0) fast path -- the debounce must never
# suppress the zero-pending-entries case, only genuine deltas.
UWL_BODY="$(awk '/^update_work_log\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"
if [[ "$UWL_BODY" == *'total_new" -eq 0'*'last_work_log_write_epoch'* ]]; then
    pass "the zero-pending-entries fast path is checked BEFORE the debounce"
else
    fail "expected the zero-pending-entries check to precede the debounce call inside update_work_log()"
fi
if [[ "$UWL_BODY" == *'last_work_log_write_epoch'* ]]; then
    pass "update_work_log() calls last_work_log_write_epoch()"
else
    fail "update_work_log() never calls last_work_log_write_epoch()"
fi
if [[ "$UWL_BODY" == *'total_new" -lt "$min_entries"'* ]]; then
    pass "update_work_log() only applies the time debounce below the min-entries threshold (escape hatch present)"
else
    fail "expected update_work_log() to gate the debounce check on total_new < min_entries"
fi

# ---------------------------------------------------------------------------
# Test 2: last_work_log_write_epoch()'s jq filter, executed against a
# fixture -- mirrors test-guide-work-plan-debounce.sh's Test 1b.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: last_work_log_write_epoch()'s jq filter, executed against a fixture"

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
else
    JQ_EXPR="$(awk '/^last_work_log_write_epoch\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD" \
        | grep -m1 -- '--jq' | sed -E 's/^.*--jq "(.*)"\)$/\1/')"

    if [[ -n "$JQ_EXPR" ]]; then
        pass "extracted the --jq expression from last_work_log_write_epoch()"
    else
        fail "could not extract the --jq expression from last_work_log_write_epoch()"
    fi

    # $GUIDE_DOCS_PR_EXCLUDE is interpolated into $JQ_EXPR by guide.md at run
    # time via bash double-quote expansion -- reproduce that here rather than
    # redefining the predicate, so the two can never drift apart.
    EXCLUDE_LINE="$(grep -m1 '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD")"
    GUIDE_DOCS_PR_EXCLUDE="${EXCLUDE_LINE#GUIDE_DOCS_PR_EXCLUDE=}"
    GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE#\'}"
    GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE%\'}"

    # Fixture: a WORK_PLAN-only docs PR merged MOST RECENTLY (must NOT anchor
    # the clock), an OLDER docs PR that actually touched WORK_LOG.md (must be
    # selected), and one genuine (non-docs) PR that must never be selected
    # regardless of its files.
    FIXTURE_JSON=$(jq -n '[
      {number: 6090, title: "docs: Guide document maintenance update",
       mergedAt: "2026-08-13T20:00:00Z", headRefName: "docs/guide-update-20260813-200000",
       files: [{path: "WORK_PLAN.md"}]},
      {number: 6060, title: "docs: Guide document maintenance update",
       mergedAt: "2026-08-13T10:00:00Z", headRefName: "docs/guide-update-20260813-100000",
       files: [{path: "WORK_LOG.md"}, {path: "WORK_PLAN.md"}]},
      {number: 6070, title: "feat(cli): add tokens check --ranking",
       mergedAt: "2026-08-13T19:00:00Z", headRefName: "feature/issue-6070",
       files: [{path: "WORK_LOG.md"}]}
    ]')

    RESULT="$(printf '%s\n' "$FIXTURE_JSON" | eval "jq -r \"$JQ_EXPR\"" 2>/dev/null)"
    assert_eq "$RESULT" "2026-08-13T10:00:00Z" \
        "the WORK_PLAN-only docs PR (#6090, merged most recently) is skipped in favor of the older docs PR that actually touched WORK_LOG.md (#6060) -- a non-docs PR touching WORK_LOG.md (#6070) is never eligible either"
fi

# ---------------------------------------------------------------------------
# Test 2b (#6327): config-key precedence for the debounce window --
# env var (if set) always wins; config value used when env var unset;
# hardcoded 1800 default when neither is present. Extracts the EXACT
# assignment line from guide.md and evals it in a sandbox, rather than
# reimplementing the precedence logic, so the test and the prompt can never
# drift apart.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2b: LOOM_WORK_LOG_DEBOUNCE_SECS config-key precedence (env > config > default, #6327)"

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
else
    # Grab BOTH the assignment line and its empty-string fallback guard (the
    # jq call yields "" rather than the intended 1800 default when
    # .loom/config.json is absent or unparsable -- the guard line is what
    # turns that into a real fallback).
    DEBOUNCE_LINE="$(awk '/debounce_secs="\$\{LOOM_WORK_LOG_DEBOUNCE_SECS/{print; getline; print; exit}' "$GUIDE_MD")"
    if [[ -n "$DEBOUNCE_LINE" ]]; then
        pass "extracted the debounce_secs assignment + fallback lines from update_work_log()"
    else
        fail "could not extract the debounce_secs assignment lines from update_work_log()"
    fi

    SANDBOX="$(mktemp -d)"
    cleanup_sandbox_wl() { rm -rf "$SANDBOX"; }
    trap cleanup_sandbox_wl EXIT
    mkdir -p "$SANDBOX/.loom"

    run_debounce_line() {
        # $1 = optional LOOM_WORK_LOG_DEBOUNCE_SECS value ("" = unset)
        local env_val="$1" out
        out=$(cd "$SANDBOX" && env ${env_val:+LOOM_WORK_LOG_DEBOUNCE_SECS="$env_val"} bash -c "
            $(printf '%s' "$DEBOUNCE_LINE" | sed -E 's/^\s*//')
            echo \"\$debounce_secs\"
        ")
        printf '%s' "$out"
    }

    # No .loom/config.json at all -- falls back to the hardcoded 1800 default
    # (the common case for most Loom-managed repos today).
    rm -f "$SANDBOX/.loom/config.json"
    RESULT="$(run_debounce_line "")"
    assert_eq "$RESULT" "1800" \
        "no guide.docsMaintenance block at all falls back to the hardcoded 1800s default"

    # Config value present, no env var -- config wins over the default.
    cat > "$SANDBOX/.loom/config.json" <<'JSON'
{"guide": {"docsMaintenance": {"workLogDebounceSecs": 5400}}}
JSON
    RESULT="$(run_debounce_line "")"
    assert_eq "$RESULT" "5400" \
        "guide.docsMaintenance.workLogDebounceSecs in .loom/config.json overrides the 1800s default"

    # Both config AND env var present -- env var always wins.
    RESULT="$(run_debounce_line "900")"
    assert_eq "$RESULT" "900" \
        "LOOM_WORK_LOG_DEBOUNCE_SECS env var wins over the config value when both are set"

    # Env var present, no config file -- env var wins over the default too.
    rm -f "$SANDBOX/.loom/config.json"
    RESULT="$(run_debounce_line "600")"
    assert_eq "$RESULT" "600" \
        "LOOM_WORK_LOG_DEBOUNCE_SECS env var wins over the default when no config block is present"

    trap - EXIT
    cleanup_sandbox_wl
fi

# ---------------------------------------------------------------------------
# Reconstruct the debounce arithmetic exactly as update_work_log() performs
# it: $1 = total_new, $2 = last_merged_epoch, $3 = now_epoch,
# $4 = debounce_secs, $5 = min_entries. Echoes "WRITE" or "SUPPRESS".
# ---------------------------------------------------------------------------
work_log_decision() {
    local total_new="$1" last_merged_epoch="$2" now_epoch="$3" debounce_secs="$4" min_entries="$5"
    local elapsed

    if [[ "$total_new" -eq 0 ]]; then
        echo "SUPPRESS"
        return
    fi

    if [[ "$total_new" -lt "$min_entries" ]]; then
        elapsed=$((now_epoch - last_merged_epoch))
        if [[ "$last_merged_epoch" -gt 0 ]] && [[ "$elapsed" -lt "$debounce_secs" ]]; then
            echo "SUPPRESS"
            return
        fi
    fi

    echo "WRITE"
}

DEBOUNCE=1800
MIN_ENTRIES=5

# ---------------------------------------------------------------------------
# Test 3: (a) a tick within the debounce window with a small pending delta
# does not write (batches instead).
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: a small pending delta inside the debounce window is batched, not written"

LAST_MERGE=1000000
INSIDE_WINDOW=$((LAST_MERGE + 300))   # 5 min after the last write, < 30 min window
DECISION="$(work_log_decision 1 "$LAST_MERGE" "$INSIDE_WINDOW" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "SUPPRESS" "1 pending entry, 300s since last write (< 1800s debounce), is suppressed"

DECISION="$(work_log_decision 3 "$LAST_MERGE" "$INSIDE_WINDOW" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "SUPPRESS" "3 pending entries (< 5-entry threshold), still inside the window, is suppressed"

# A second tick a bit later, still inside the window with the delta grown
# slightly (but still under the min-entries threshold), is also suppressed --
# the batching accumulates across ticks rather than writing partial deltas.
LATER_STILL_INSIDE=$((LAST_MERGE + 900))
DECISION="$(work_log_decision 4 "$LAST_MERGE" "$LATER_STILL_INSIDE" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "SUPPRESS" "4 pending entries, 900s since last write (< 1800s debounce), is still suppressed"

# ---------------------------------------------------------------------------
# Test 4: (b) a tick past the debounce window with the same pending delta
# writes.
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: a pending delta that survives past the debounce window is written"

PAST_WINDOW=$((LAST_MERGE + DEBOUNCE + 1))
DECISION="$(work_log_decision 3 "$LAST_MERGE" "$PAST_WINDOW" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "WRITE" "3 pending entries, past the 1800s debounce window, is written"

# Boundary: elapsed == debounce_secs is treated as "the window has elapsed"
# (matches update_work_plan()'s existing off-by-one convention, `-lt`).
AT_BOUNDARY=$((LAST_MERGE + DEBOUNCE))
DECISION="$(work_log_decision 3 "$LAST_MERGE" "$AT_BOUNDARY" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "WRITE" "elapsed == debounce_secs is treated as the window having elapsed (not suppressed)"

JUST_BEFORE=$((LAST_MERGE + DEBOUNCE - 1))
DECISION="$(work_log_decision 3 "$LAST_MERGE" "$JUST_BEFORE" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "SUPPRESS" "one second before the window elapses, the same delta is still suppressed"

# ---------------------------------------------------------------------------
# Test 5: (c) a sufficiently large accumulated delta writes immediately even
# INSIDE the debounce window -- the "no starvation" guarantee.
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: a large accumulated delta writes immediately, even inside the debounce window"

DECISION="$(work_log_decision 5 "$LAST_MERGE" "$INSIDE_WINDOW" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "WRITE" "5 pending entries (== min-entries threshold), only 300s since last write, still writes immediately"

DECISION="$(work_log_decision 12 "$LAST_MERGE" "$INSIDE_WINDOW" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "WRITE" "12 pending entries (well above threshold), only 300s since last write, still writes immediately"

# ---------------------------------------------------------------------------
# Test 6: (d) zero pending entries never writes, regardless of window/size.
# ---------------------------------------------------------------------------
echo ""
echo "Test 6: zero pending entries never writes, regardless of debounce state"

DECISION="$(work_log_decision 0 "$LAST_MERGE" "$PAST_WINDOW" "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "SUPPRESS" "0 pending entries suppresses even long past the debounce window"

DECISION="$(work_log_decision 0 0 1000000 "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "SUPPRESS" "0 pending entries suppresses even with no prior write history"

# ---------------------------------------------------------------------------
# Test 7: (e) the first-ever tick (no prior WORK_LOG-writing docs PR,
# last_merged_epoch == 0) still writes immediately for a small delta.
# ---------------------------------------------------------------------------
echo ""
echo "Test 7: an empty WORK_LOG-write history never suppresses a genuine small delta"

DECISION="$(work_log_decision 1 0 1000000 "$DEBOUNCE" "$MIN_ENTRIES")"
assert_eq "$DECISION" "WRITE" "last_merged_epoch == 0 (no prior WORK_LOG-writing docs PR) writes immediately, even for a single pending entry"

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
