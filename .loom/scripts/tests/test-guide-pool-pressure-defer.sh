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
#     ranking (never a fresh `tokens check --ranking` probe, which would
#     itself burn a real request against the pool this check exists to
#     protect) -- reduced to "fraction of accounts NOT `available`".
#     Since #13839 it goes through `loom-daemon tokens pressure` rather than
#     opening the pool directory itself: `guide` declares no
#     `credential-store` capability, so the role-tool-policy hook (#8256)
#     DENIES any Bash call naming a path under `.loom/tokens`, and the
#     hardcoded repo-local path was wrong anyway on a shared-pool host.
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
#         run against a STUB `loom-daemon` on PATH -- a real fraction, "0",
#         no output at all, non-numeric output, and a stub that exits
#         non-zero (an older daemon with no `pressure` verb). Every
#         unexpected shape must fail open to "0".
#      a'. #13839: the function body names no path under `.loom/tokens`, and
#         `loom-daemon role-tool-policy check guide` ALLOWS the command it
#         actually runs (the regression that made the whole check dead code).
#      b. last_docs_maintenance_merge_epoch()'s jq filter, executed against a
#         fixture that proves it is NOT restricted to a specific file (a
#         README-only docs PR still anchors the clock -- the opposite of
#         last_work_plan_write_epoch()'s #5929 fix, and deliberately so).
#      c. A reconstruction of should_defer_for_pool_pressure()'s decision
#         arithmetic, run against fixture threshold/epoch values -- below
#         threshold, at/above threshold within the max-defer ceiling,
#         at/above threshold past the ceiling, and an empty merge history.
#
# Hermetic: a throwaway git repo under mktemp -d with a stub `loom-daemon`
# first on PATH for the fraction test, pure jq/arithmetic against fixture
# values for the rest. No forge, network, `gh`, or real-daemon calls.

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

# Local assert_eq kept: takes (actual, expected, msg); shared assert_eq is (expected, actual, msg).
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
assert_grep 'loom-daemon tokens pressure' "$GUIDE_MD" \
    "pool_pressure_fraction() goes through the loom-daemon tokens pressure verb (#13839)"
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
# executed against a STUB `loom-daemon tokens pressure` on PATH.
#
# Before #13839 this function opened `<repo>/.loom/tokens/.ranking` itself,
# which the role-tool-policy hook denies for `guide` (no `credential-store`
# capability) and which points at the wrong directory on a shared-pool host.
# It now shells out to the verb, so what has to be executed here is the
# function's handling of what the verb prints -- including every way an older
# or broken daemon can answer.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: pool_pressure_fraction(), executed against a stub loom-daemon"

PPF_SRC="$(awk '/^pool_pressure_fraction\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"

if [[ -z "$PPF_SRC" ]]; then
    fail "could not extract pool_pressure_fraction() from guide.md"
else
    pass "extracted pool_pressure_fraction() from guide.md"

    # #13839: the body must not name the credential-store path at all. A
    # single occurrence anywhere in the function is enough for the hook to
    # deny the entire Bash call it appears in.
    if [[ "$PPF_SRC" == *'.loom/tokens'* ]]; then
        fail "pool_pressure_fraction() still names a path under .loom/tokens -- the role-tool-policy hook denies that for 'guide' (#13839)"
    else
        pass "pool_pressure_fraction() names no path under .loom/tokens (#13839)"
    fi

    if [[ "$PPF_SRC" == *'loom-daemon tokens pressure'* ]]; then
        pass "pool_pressure_fraction() obtains the fraction from 'loom-daemon tokens pressure'"
    else
        fail "pool_pressure_fraction() does not call 'loom-daemon tokens pressure' (#13839)"
    fi

    SANDBOX="$(mktemp -d)"
    cleanup_sandbox() { rm -rf "$SANDBOX"; }
    trap cleanup_sandbox EXIT

    PPF_SCRIPT="$SANDBOX/ppf.sh"
    printf '%s\n' "$PPF_SRC" > "$PPF_SCRIPT"

    (cd "$SANDBOX" && git init -q)

    # A stub `loom-daemon` first on PATH. It echoes whatever
    # $STUB_OUT holds and exits with $STUB_RC, so one stub covers every
    # answer shape the real verb (or an older daemon) can produce.
    mkdir -p "$SANDBOX/bin"
    cat > "$SANDBOX/bin/loom-daemon" <<'STUB'
#!/usr/bin/env bash
[[ -n "${STUB_OUT:-}" ]] && printf '%s\n' "$STUB_OUT"
exit "${STUB_RC:-0}"
STUB
    chmod +x "$SANDBOX/bin/loom-daemon"

    # Run the function with the stub shadowing any real loom-daemon.
    run_ppf() {
        (cd "$SANDBOX" && PATH="$SANDBOX/bin:$PATH" \
            STUB_OUT="${1-}" STUB_RC="${2:-0}" \
            bash -c "source '$PPF_SCRIPT'; pool_pressure_fraction")
    }

    # The verb reports real pressure -- passed through verbatim.
    RESULT="$(run_ppf "0.7500" 0)"
    assert_eq "$RESULT" "0.7500" "a 0.7500 reading from the verb is passed through verbatim"

    RESULT="$(run_ppf "0.0000" 0)"
    assert_eq "$RESULT" "0.0000" "a fully healthy pool (0.0000) is passed through verbatim"

    # The verb's own fail-open answer for absent/unparseable ranking data.
    RESULT="$(run_ppf "0" 0)"
    assert_eq "$RESULT" "0" "the verb's no-data answer ('0') is passed through"

    # An older daemon that does not know the `pressure` verb: clap exits
    # non-zero. Must fail open, never propagate an error or empty string.
    RESULT="$(run_ppf "error: unrecognized subcommand 'pressure'" 2)"
    assert_eq "$RESULT" "0" "a daemon too old for the verb (non-zero exit) fails open to 0"

    # Exit 0 but nothing on stdout -- also fails open rather than producing an
    # empty fraction that would corrupt the awk comparison downstream.
    RESULT="$(run_ppf "" 0)"
    assert_eq "$RESULT" "0" "empty output with exit 0 fails open to 0"

    # Non-numeric output (a warning line leaking onto stdout, say) must never
    # reach the awk comparison in should_defer_for_pool_pressure().
    RESULT="$(run_ppf "warning: no pool found" 0)"
    assert_eq "$RESULT" "0" "non-numeric output fails open to 0"

    trap - EXIT
    cleanup_sandbox
fi

# ---------------------------------------------------------------------------
# Test 3b: #13839 -- the command pool_pressure_fraction() runs is ALLOWED by
# the role-tool-policy hook for role `guide`. This is the actual reported
# regression: the prescribed read was refused at the harness, so the whole
# #6135 backoff was dead code for any Guide that followed its prompt.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3b: role-tool-policy allows the command for role 'guide' (#13839)"

if ! command -v loom-daemon >/dev/null 2>&1; then
    echo "SKIP: loom-daemon not on PATH"
elif ! loom-daemon role-tool-policy check --help >/dev/null 2>&1; then
    echo "SKIP: this loom-daemon has no 'role-tool-policy check' subcommand"
else
    # The exact command line the function runs, with the command
    # substitution left intact -- the hook judges raw command text.
    PPF_CMD='loom-daemon tokens pressure --workspace "$(git rev-parse --show-toplevel 2>/dev/null || echo .)"'
    if loom-daemon role-tool-policy check guide --workspace "$REPO_ROOT" --command "$PPF_CMD" >/dev/null 2>&1; then
        pass "role-tool-policy ALLOWS the pool-pressure command for role 'guide'"
    else
        fail "role-tool-policy DENIES the pool-pressure command for role 'guide' (#13839 regression)"
    fi

    # Control: the pre-#13839 direct read is still denied, which is what makes
    # the assertion above meaningful rather than vacuously true.
    if loom-daemon role-tool-policy check guide --workspace "$REPO_ROOT" \
        --command 'awk -F"|" "{print}" .loom/tokens/.ranking' >/dev/null 2>&1; then
        fail "control: a direct read of .loom/tokens/.ranking should be DENIED for 'guide' -- the matcher is not exercising the capability"
    else
        pass "control: a direct read of .loom/tokens/.ranking is still denied for 'guide'"
    fi
fi

# ---------------------------------------------------------------------------
# Test 3c: #13839 -- guide.json keeps an EMPTY capability allowlist. The fix
# must not have widened the role's capabilities to make the read legal.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3c: guide.json still declares no capabilities (#13839)"

GUIDE_JSON=""
for candidate in "$REPO_ROOT/.loom/roles/guide.json" "$REPO_ROOT/defaults/roles/guide.json"; do
    [[ -f "$candidate" ]] && { GUIDE_JSON="$candidate"; break; }
done

if [[ -z "$GUIDE_JSON" ]]; then
    fail "could not locate guide.json"
elif ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
else
    CAPS="$(jq -c '.toolPolicy.allowedCapabilities // []' "$GUIDE_JSON")"
    assert_eq "$CAPS" "[]" "guide.json's allowedCapabilities is still empty (the fix did not grant credential-store)"
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
