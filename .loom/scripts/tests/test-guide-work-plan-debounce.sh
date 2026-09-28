#!/usr/bin/env bash
# test-guide-work-plan-debounce.sh - Regression test for issues #5890 and #5929
#
# Between 2026-08-10T03:09Z and 2026-08-10T11:10Z, 9 consecutive merged PRs on
# main were all `docs: Guide document maintenance update`, with no
# substantive work merged in between. Root cause: `update_work_plan()`'s only
# write-worthiness gate was "does render_plan_body()'s output differ from the
# committed WORK_PLAN.md region" — true on every `loom:building`/`loom:issue`
# transition on ANY issue, including issues bouncing through Builder-claim ->
# Judge-approve -> Champion merge-risk-hold -> re-claim cycles (observed on
# #5607/#5629). Every bounce manufactured its own docs PR. This mirrors the
# #5643 label-flip incident (fixed then by a per-label flip guard, since retired),
# but nothing analogous gated the plan-body regeneration itself.
#
# The #5890 fix added a time-based debounce: `update_work_plan()` only writes
# a rewritten body once at least `LOOM_WORK_PLAN_DEBOUNCE_SECS` (default 3600)
# have elapsed since the most recently MERGED docs-maintenance PR, using the
# forge's own merged-PR history as the durable, side-car-free anchor (reusing
# `GUIDE_DOCS_PR_EXCLUDE`, the same "is this a docs-maintenance PR" predicate
# #5454's fix already established).
#
# #5929 BUG, DO NOT REINTRODUCE: that anchor was "time since ANY docs-
# maintenance PR merged", including one whose commit only touched
# WORK_LOG.md and never touched WORK_PLAN.md at all (Step 5 stages
# `WORK_LOG.md WORK_PLAN.md README.md` together, but only files that actually
# changed end up non-empty in the diff). On a repo with sustained merge
# cadence, WORK_LOG-only docs PRs kept landing every 15-40 minutes, which kept
# resetting the debounce clock forever even though WORK_PLAN.md's own content
# had gone stale far longer ago — an overdue rewrite could be suppressed
# indefinitely. The fix anchors the clock to "time since a merged
# docs-maintenance PR's changed files actually included WORK_PLAN.md"
# instead (`last_work_plan_write_epoch()`, renamed from
# `last_docs_pr_merged_epoch()`), which still fully preserves the #5890
# flap-suppression intent.
#
# Verifies that:
#   1. guide.md defines `last_work_plan_write_epoch()`, reusing
#      `GUIDE_DOCS_PR_EXCLUDE` rather than redefining the docs-PR predicate,
#      AND additionally filters merged PRs down to ones whose `files` include
#      `WORK_PLAN.md` (#5929).
#   2. guide.md's `update_work_plan()` gates the write behind
#      `LOOM_WORK_PLAN_DEBOUNCE_SECS` (default 3600) since that write.
#   3. THE REGRESSION, executed rather than grepped: a reconstruction of the
#      debounce arithmetic, run against fixture timelines —
#        a. zero label churn (new_body == old_body) still produces zero
#           writes, unchanged from before #5890.
#        b. a rapid label flip-flop diff, observed inside the debounce
#           window since the last WORK_PLAN.md write, is suppressed (no
#           write).
#        c. the SAME diff, once the debounce window has elapsed, produces
#           exactly one write.
#        d. a diff with no prior WORK_PLAN.md write in history at all
#           (last_merged_epoch == 0) writes immediately — never suppressed
#           by a write that never happened.
#        e. THE #5929 REGRESSION: a diff whose only "recent merge" is a
#           WORK_LOG-only docs PR (no WORK_PLAN.md write) is NOT suppressed,
#           even though a docs-maintenance PR merged within the window —
#           because that merge never touched WORK_PLAN.md, it must not be
#           the anchor at all.
#
# Hermetic: pure grep/arithmetic against fixture epoch values. No forge,
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

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 1: the prompt wires the debounce into update_work_plan()
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines and wires the WORK_PLAN debounce"

assert_grep 'last_work_plan_write_epoch\(\) \{' "$GUIDE_MD" \
    "last_work_plan_write_epoch() is defined"
assert_grep 'select\(\$GUIDE_DOCS_PR_EXCLUDE\)' "$GUIDE_MD" \
    "last_work_plan_write_epoch() reuses GUIDE_DOCS_PR_EXCLUDE rather than redefining the docs-PR predicate"
assert_grep 'index\(\\"WORK_PLAN\.md\\"\)' "$GUIDE_MD" \
    "last_work_plan_write_epoch() additionally filters merged PRs to ones whose files include WORK_PLAN.md (#5929)"
assert_grep 'LOOM_WORK_PLAN_DEBOUNCE_SECS:-\$\(jq -r' "$GUIDE_MD" \
    "update_work_plan() reads LOOM_WORK_PLAN_DEBOUNCE_SECS, falling back to a config read (#6327)"
assert_grep 'guide\.docsMaintenance\.workPlanDebounceSecs // 3600' "$GUIDE_MD" \
    "update_work_plan() reads guide.docsMaintenance.workPlanDebounceSecs from .loom/config.json with a 3600s (1h) default (#6327)"
assert_grep 'update_work_plan\(\) \{' "$GUIDE_MD" \
    "update_work_plan() is defined"

# #5929: last_docs_pr_merged_epoch() (the pre-fix name) must be gone entirely
# -- a stray reference would mean the old, WORK_LOG-oblivious anchor is still
# wired in somewhere.
if grep -qE 'last_docs_pr_merged_epoch\(\)[[:space:]]*\{' "$GUIDE_MD"; then
    fail "last_docs_pr_merged_epoch() (the pre-#5929 function) must not be defined anymore"
else
    pass "last_docs_pr_merged_epoch() (the pre-#5929 function) is no longer defined"
fi

# update_work_plan() must call last_work_plan_write_epoch() AFTER the
# new_body == old_body check (the debounce must never suppress the
# "nothing changed" fast path, only genuine diffs).
UWP_BODY="$(awk '/^update_work_plan\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"
if [[ "$UWP_BODY" == *'new_body" = "$old_body"'*'last_work_plan_write_epoch'* ]]; then
    pass "the no-change fast path (new_body == old_body) is checked BEFORE the debounce"
else
    fail "expected the no-change check to precede the debounce call inside update_work_plan()"
fi
if [[ "$UWP_BODY" == *'last_work_plan_write_epoch'* ]]; then
    pass "update_work_plan() calls last_work_plan_write_epoch()"
else
    fail "update_work_plan() never calls last_work_plan_write_epoch()"
fi

# ---------------------------------------------------------------------------
# Test 1b: THE #5929 REGRESSION, executed against the real jq filter — the
# `--jq` expression is extracted VERBATIM from `last_work_plan_write_epoch()`
# and run through the real jq pipeline against a fixture merged-PR list that
# mixes a WORK_LOG-only docs PR with a WORK_PLAN.md-touching docs PR and a
# genuine (non-docs) PR. Mirrors the extraction style of
# test-guide-work-log-self-loop.sh.
# ---------------------------------------------------------------------------
echo ""
echo "Test 1b: last_work_plan_write_epoch()'s jq filter, executed against a fixture"

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
else
    JQ_EXPR="$(awk '/^last_work_plan_write_epoch\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD" \
        | grep -m1 -- '--jq' | sed -E 's/^.*--jq "(.*)"\)$/\1/')"

    if [[ -n "$JQ_EXPR" ]]; then
        pass "extracted the --jq expression from last_work_plan_write_epoch()"
    else
        fail "could not extract the --jq expression from last_work_plan_write_epoch()"
    fi

    # $GUIDE_DOCS_PR_EXCLUDE is interpolated into $JQ_EXPR by guide.md at run
    # time via bash double-quote expansion — reproduce that here rather than
    # redefining the predicate, so the two can never drift apart.
    EXCLUDE_LINE="$(grep -m1 '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD")"
    GUIDE_DOCS_PR_EXCLUDE="${EXCLUDE_LINE#GUIDE_DOCS_PR_EXCLUDE=}"
    GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE#\'}"
    GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE%\'}"

    # #5929 fixture: a WORK_LOG-only docs PR merged MOST RECENTLY (would have
    # wrongly anchored the old last_docs_pr_merged_epoch()), an OLDER docs PR
    # that actually touched WORK_PLAN.md, and one genuine (non-docs) PR that
    # must never be selected regardless of its files.
    FIXTURE_JSON=$(jq -n '[
      {number: 5940, title: "docs: Guide document maintenance update",
       mergedAt: "2026-08-10T20:00:00Z", headRefName: "docs/guide-update-20260810-200000",
       files: [{path: "WORK_LOG.md"}]},
      {number: 5900, title: "docs: Guide document maintenance update",
       mergedAt: "2026-08-10T10:00:00Z", headRefName: "docs/guide-update-20260810-100000",
       files: [{path: "WORK_LOG.md"}, {path: "WORK_PLAN.md"}]},
      {number: 5935, title: "feat(cli): add tokens check --ranking",
       mergedAt: "2026-08-10T19:00:00Z", headRefName: "feature/issue-5935",
       files: [{path: "WORK_PLAN.md"}]}
    ]')

    RESULT="$(printf '%s\n' "$FIXTURE_JSON" | eval "jq -r \"$JQ_EXPR\"" 2>/dev/null)"
    assert_eq "$RESULT" "2026-08-10T10:00:00Z" \
        "the WORK_LOG-only docs PR (#5940, merged most recently) is skipped in favor of the older docs PR that actually touched WORK_PLAN.md (#5900) -- a non-docs PR touching WORK_PLAN.md (#5935) is never eligible either"
fi

# ---------------------------------------------------------------------------
# Test 1c (#6327): config-key precedence for the debounce window -- env var
# (if set) always wins; config value used when env var unset; hardcoded 3600
# default when neither is present. Mirrors
# test-guide-work-log-debounce.sh's Test 2b, but for WORK_PLAN. Extracts the
# EXACT assignment + fallback lines from guide.md and evals them in a
# sandbox, so the test and the prompt can never drift apart.
# ---------------------------------------------------------------------------
echo ""
echo "Test 1c: LOOM_WORK_PLAN_DEBOUNCE_SECS config-key precedence (env > config > default, #6327)"

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
else
    DEBOUNCE_LINE="$(awk '/debounce_secs="\$\{LOOM_WORK_PLAN_DEBOUNCE_SECS/{print; getline; print; exit}' "$GUIDE_MD")"
    if [[ -n "$DEBOUNCE_LINE" ]]; then
        pass "extracted the debounce_secs assignment + fallback lines from update_work_plan()"
    else
        fail "could not extract the debounce_secs assignment lines from update_work_plan()"
    fi

    SANDBOX="$(mktemp -d)"
    cleanup_sandbox_wp() { rm -rf "$SANDBOX"; }
    trap cleanup_sandbox_wp EXIT
    mkdir -p "$SANDBOX/.loom"

    run_debounce_line() {
        # $1 = optional LOOM_WORK_PLAN_DEBOUNCE_SECS value ("" = unset)
        local env_val="$1" out
        out=$(cd "$SANDBOX" && env ${env_val:+LOOM_WORK_PLAN_DEBOUNCE_SECS="$env_val"} bash -c "
            $(printf '%s' "$DEBOUNCE_LINE" | sed -E 's/^\s*//')
            echo \"\$debounce_secs\"
        ")
        printf '%s' "$out"
    }

    rm -f "$SANDBOX/.loom/config.json"
    RESULT="$(run_debounce_line "")"
    assert_eq "$RESULT" "3600" \
        "no guide.docsMaintenance block at all falls back to the hardcoded 3600s default"

    cat > "$SANDBOX/.loom/config.json" <<'JSON'
{"guide": {"docsMaintenance": {"workPlanDebounceSecs": 7200}}}
JSON
    RESULT="$(run_debounce_line "")"
    assert_eq "$RESULT" "7200" \
        "guide.docsMaintenance.workPlanDebounceSecs in .loom/config.json overrides the 3600s default"

    RESULT="$(run_debounce_line "1200")"
    assert_eq "$RESULT" "1200" \
        "LOOM_WORK_PLAN_DEBOUNCE_SECS env var wins over the config value when both are set"

    rm -f "$SANDBOX/.loom/config.json"
    RESULT="$(run_debounce_line "600")"
    assert_eq "$RESULT" "600" \
        "LOOM_WORK_PLAN_DEBOUNCE_SECS env var wins over the default when no config block is present"

    trap - EXIT
    cleanup_sandbox_wp
fi

# ---------------------------------------------------------------------------
# Reconstruct the debounce arithmetic exactly as update_work_plan() performs
# it (mirrors the extraction style of test-guide-work-log-self-loop.sh):
# $1 = new_body, $2 = old_body, $3 = last_merged_epoch, $4 = now_epoch,
# $5 = debounce_secs. Echoes "WRITE" or "SUPPRESS".
# ---------------------------------------------------------------------------
work_plan_decision() {
    local new_body="$1" old_body="$2" last_merged_epoch="$3" now_epoch="$4" debounce_secs="$5"
    local elapsed

    if [[ "$new_body" == "$old_body" ]]; then
        echo "SUPPRESS"
        return
    fi

    elapsed=$((now_epoch - last_merged_epoch))
    if [[ "$last_merged_epoch" -gt 0 ]] && [[ "$elapsed" -lt "$debounce_secs" ]]; then
        echo "SUPPRESS"
        return
    fi

    echo "WRITE"
}

DEBOUNCE=3600

# ---------------------------------------------------------------------------
# Test 2: zero label churn still produces zero writes (unchanged behavior)
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: stable label state (no diff) is unaffected by the debounce"

DECISION="$(work_plan_decision "same body" "same body" 0 1000000 "$DEBOUNCE")"
assert_eq "$DECISION" "SUPPRESS" "identical new_body/old_body suppresses regardless of merge history"

DECISION="$(work_plan_decision "same body" "same body" 999995 1000000 "$DEBOUNCE")"
assert_eq "$DECISION" "SUPPRESS" "identical new_body/old_body suppresses even seconds after a merge"

# ---------------------------------------------------------------------------
# Test 3: THE FLAP — a diff inside the debounce window is suppressed
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: a rapid label flip-flop diff inside the debounce window is suppressed"

# Last docs-maintenance PR merged at T=1000000. A #5607/#5629-style bounce
# reshapes render_plan_body()'s output 5 minutes later (300s < 3600s window).
LAST_MERGE=1000000
FLAP_TICK=$((LAST_MERGE + 300))
DECISION="$(work_plan_decision "## Ready\n- #5629" "## Ready\n(none)" "$LAST_MERGE" "$FLAP_TICK" "$DEBOUNCE")"
assert_eq "$DECISION" "SUPPRESS" "a diff 300s after the last merge (inside the 3600s window) is suppressed"

# A second bounce a few minutes later, still inside the window, is also
# suppressed — the flap never accumulates a write no matter how many times
# it re-renders differently within the window.
FLAP_TICK_2=$((LAST_MERGE + 900))
DECISION="$(work_plan_decision "## Ready\n(none)" "## Ready\n(none)" "$LAST_MERGE" "$FLAP_TICK_2" "$DEBOUNCE")"
assert_eq "$DECISION" "SUPPRESS" "a settled-back-to-committed-state re-render inside the window still suppresses"

# ---------------------------------------------------------------------------
# Test 4: a PERSISTING genuine change still produces exactly one write, once
# the debounce window elapses
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: a change that persists past the debounce window produces exactly one write"

PAST_WINDOW=$((LAST_MERGE + DEBOUNCE + 1))
DECISION="$(work_plan_decision "## Ready\n- #5629" "## Ready\n(none)" "$LAST_MERGE" "$PAST_WINDOW" "$DEBOUNCE")"
assert_eq "$DECISION" "WRITE" "the same persisting diff writes once the debounce window has elapsed"

# Exactly at the boundary the window has NOT yet elapsed (strict <), so this
# must still suppress -- guards against an off-by-one flipping the gate open
# one tick early.
AT_BOUNDARY=$((LAST_MERGE + DEBOUNCE))
DECISION="$(work_plan_decision "## Ready\n- #5629" "## Ready\n(none)" "$LAST_MERGE" "$AT_BOUNDARY" "$DEBOUNCE")"
assert_eq "$DECISION" "WRITE" "elapsed == debounce_secs is treated as \"the window has elapsed\" (not suppressed)"

JUST_BEFORE=$((LAST_MERGE + DEBOUNCE - 1))
DECISION="$(work_plan_decision "## Ready\n- #5629" "## Ready\n(none)" "$LAST_MERGE" "$JUST_BEFORE" "$DEBOUNCE")"
assert_eq "$DECISION" "SUPPRESS" "one second before the window elapses, the same diff is still suppressed"

# ---------------------------------------------------------------------------
# Test 5: no docs-maintenance PR has ever merged (last_merged_epoch == 0) —
# a genuine first-ever change must never be suppressed by a merge that never
# happened.
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: an empty docs-maintenance history never suppresses a genuine diff"

DECISION="$(work_plan_decision "## Ready\n- #1" "(none)" 0 1000000 "$DEBOUNCE")"
assert_eq "$DECISION" "WRITE" "last_merged_epoch == 0 (no prior docs PR) writes immediately"

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
