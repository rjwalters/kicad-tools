#!/usr/bin/env bash
# test-merge-admission-telemetry.sh - Regression test for issue #6978
# (follow-up from #6156)
#
# #6156's efficacy review of merge-pr.sh's stale-cached-mergeable recheck
# (_recheck_mergeable_before_refusal, #6104/#6118) found the one actionable
# gap in that path: its merge/refuse-conflict/refuse-stale decision was only
# ever echoed to stdout/stderr -- nothing durable recorded it anywhere. This
# suite covers the new, deliberately decoupled local telemetry surface that
# closes that gap (mirroring guide-docs-telemetry.sh, issue #6136).
#
# Verifies:
#   1. `record` appends a well-formed JSONL line with the expected fields,
#      including null retries_used/backoff_delay_sec when not supplied
#      (never coerced to 0) and real integers when supplied.
#   2. `record` validates --pr (required, numeric) and --action (required,
#      one of merge|refuse-conflict|refuse-stale).
#   3. `report` on an empty/missing log renders "no activity" without
#      erroring, for both human and --json output.
#   4. `report` correctly filters records to the requested --since window
#      and breaks the count down by action.
#   5. `report --since` rejects a malformed window value.
#   6. argument validation (unknown command, missing --pr, bad --action).
#
# Hermetic: throwaway git repo under mktemp -d, no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

TELEMETRY_SH="$SCRIPTS_DIR/merge-admission-telemetry.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

if [[ ! -x "$TELEMETRY_SH" ]]; then
    fail "$TELEMETRY_SH missing or not executable"
fi
command -v jq >/dev/null 2>&1 || fail "jq is required by merge-admission-telemetry.sh but is not on PATH"

if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo ""
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi

SANDBOX="$(mktemp -d)"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

SANDBOX_REPO="$SANDBOX/repo"
mkdir -p "$SANDBOX_REPO"
git init --quiet "$SANDBOX_REPO"
git -C "$SANDBOX_REPO" config user.email "test@loom.local"
git -C "$SANDBOX_REPO" config user.name "Loom Test"

LOG_FILE="$SANDBOX_REPO/.loom/logs/merge-admission-telemetry.jsonl"
export LOOM_MERGE_ADMISSION_TELEMETRY_LOG="$LOG_FILE"

# --- Test 1: report on a missing log -----------------------------------------
echo "Test 1: report renders 'no activity' when the log does not exist yet"

OUT="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d 2>&1)"
RC=$?
if [[ "$RC" == "0" ]]; then pass "report exits 0 on a missing log"; else fail "report exited $RC on a missing log"; fi
if grep -q "No merge-admission-recheck invocations in this window" <<<"$OUT"; then
    pass "report reports zero activity without erroring"
else
    fail "report did not report zero activity cleanly: $OUT"
fi

OUT_JSON="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d --json 2>&1)"
RC=$?
if [[ "$RC" == "0" ]] && [[ "$(jq -r '.record_count' <<<"$OUT_JSON")" == "0" ]]; then
    pass "report --json reports record_count: 0 on a missing log"
else
    fail "report --json did not cleanly report zero activity: $OUT_JSON (rc=$RC)"
fi

# --- Test 2: record validates --pr and --action ------------------------------
echo ""
echo "Test 2: record requires a numeric --pr and a valid --action"

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --action merge --reason x >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "record with no --pr exits 2"; else fail "expected exit 2, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr notanumber --action merge --reason x >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "record with a non-numeric --pr exits 2"; else fail "expected exit 2, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 1 --action bogus --reason x >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "record with an invalid --action exits 2"; else fail "expected exit 2, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 1 --reason x >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "record with a missing --action exits 2"; else fail "expected exit 2, got $RC"; fi

if [[ -f "$LOG_FILE" ]]; then
    fail "a rejected record must not create/append to the log file"
else
    pass "no log file was created by the rejected record calls"
fi

# --- Test 3: record appends a well-formed line -------------------------------
echo ""
echo "Test 3: record appends a well-formed JSONL line"

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 4242 --repo acme/widgets --action merge \
    --reason "cached mergeable=false was stale; recheck #2 (post-backoff, uncached) now reports mergeable=true" \
    --retries-used 2 --backoff-delay-sec 3 >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "record with all fields exits 0"; else fail "record exited $RC"; fi

if [[ -f "$LOG_FILE" ]]; then pass "log file created"; else fail "log file not created at $LOG_FILE"; fi

LAST_LINE="$(tail -1 "$LOG_FILE" 2>/dev/null)"
if [[ -n "$LAST_LINE" ]] && jq -e . >/dev/null 2>&1 <<<"$LAST_LINE"; then
    pass "appended line is valid JSON"
else
    fail "appended line is not valid JSON: $LAST_LINE"
fi

if [[ "$(jq -r '.record.kind' <<<"$LAST_LINE")" == "merge.admission_recheck" ]]; then
    pass "record.kind is 'merge.admission_recheck'"
else
    fail "unexpected record.kind: $(jq -r '.record.kind' <<<"$LAST_LINE")"
fi

if [[ "$(jq -r '.record.pr_number' <<<"$LAST_LINE")" == "4242" ]]; then
    pass "record.pr_number matches --pr"
else
    fail "unexpected record.pr_number: $(jq -r '.record.pr_number' <<<"$LAST_LINE")"
fi

if [[ "$(jq -r '.record.action' <<<"$LAST_LINE")" == "merge" ]]; then
    pass "record.action matches --action"
else
    fail "unexpected record.action: $(jq -r '.record.action' <<<"$LAST_LINE")"
fi

if [[ "$(jq -r '.record.reason' <<<"$LAST_LINE")" == *"recheck #2"* ]]; then
    pass "record.reason matches --reason"
else
    fail "unexpected record.reason: $(jq -r '.record.reason' <<<"$LAST_LINE")"
fi

if [[ "$(jq -r '.record.retries_used' <<<"$LAST_LINE")" == "2" ]]; then
    pass "record.retries_used matches --retries-used"
else
    fail "unexpected record.retries_used: $(jq -r '.record.retries_used' <<<"$LAST_LINE")"
fi

if [[ "$(jq -r '.record.backoff_delay_sec' <<<"$LAST_LINE")" == "3" ]]; then
    pass "record.backoff_delay_sec matches --backoff-delay-sec"
else
    fail "unexpected record.backoff_delay_sec: $(jq -r '.record.backoff_delay_sec' <<<"$LAST_LINE")"
fi

if [[ -n "$(jq -r '.emitted_at_epoch' <<<"$LAST_LINE")" ]] && [[ "$(jq -r '.emitted_at_epoch | type' <<<"$LAST_LINE")" == "number" ]]; then
    pass "emitted_at_epoch is present and numeric (used for window filtering)"
else
    fail "emitted_at_epoch missing or non-numeric"
fi

# --- Test 4: retries_used/backoff_delay_sec are null (not 0) when omitted ---
echo ""
echo "Test 4: omitted --retries-used/--backoff-delay-sec record null, never 0"

(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 4243 --repo acme/widgets --action refuse-conflict --reason "genuinely conflicts" >/dev/null 2>&1)
LAST_LINE="$(tail -1 "$LOG_FILE")"
if [[ "$(jq -r '.record.retries_used' <<<"$LAST_LINE")" == "null" ]]; then
    pass "retries_used is null when --retries-used is not supplied"
else
    fail "retries_used should be null, got: $(jq -r '.record.retries_used' <<<"$LAST_LINE")"
fi
if [[ "$(jq -r '.record.backoff_delay_sec' <<<"$LAST_LINE")" == "null" ]]; then
    pass "backoff_delay_sec is null when --backoff-delay-sec is not supplied"
else
    fail "backoff_delay_sec should be null, got: $(jq -r '.record.backoff_delay_sec' <<<"$LAST_LINE")"
fi

# A malformed value must also degrade to null, not crash.
RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 4244 --repo acme/widgets --action refuse-stale --reason "unresolved" --retries-used "" --backoff-delay-sec "" >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then
    pass "empty --retries-used/--backoff-delay-sec values do not crash record"
else
    fail "record crashed (rc=$RC) on empty numeric values"
fi
LAST_LINE="$(tail -1 "$LOG_FILE")"
if [[ "$(jq -r '.record.retries_used' <<<"$LAST_LINE")" == "null" ]] && [[ "$(jq -r '.record.backoff_delay_sec' <<<"$LAST_LINE")" == "null" ]]; then
    pass "empty numeric values degrade to null"
else
    fail "expected null retries_used/backoff_delay_sec for empty values, got: $(jq -c '.record | {retries_used, backoff_delay_sec}' <<<"$LAST_LINE")"
fi

# --- Test 5: report aggregates across recorded invocations -------------------
echo ""
echo "Test 5: report aggregates record_count / merge_count / refuse_*_count"

SUMMARY="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d --json)"
RECORD_COUNT="$(jq -r '.record_count' <<<"$SUMMARY")"
MERGE_COUNT="$(jq -r '.merge_count' <<<"$SUMMARY")"
REFUSE_CONFLICT_COUNT="$(jq -r '.refuse_conflict_count' <<<"$SUMMARY")"
REFUSE_STALE_COUNT="$(jq -r '.refuse_stale_count' <<<"$SUMMARY")"

if [[ "$RECORD_COUNT" == "3" ]]; then pass "record_count reflects all 3 recorded invocations"; else fail "expected record_count=3, got $RECORD_COUNT"; fi
if [[ "$MERGE_COUNT" == "1" ]]; then pass "merge_count is 1"; else fail "expected merge_count=1, got $MERGE_COUNT"; fi
if [[ "$REFUSE_CONFLICT_COUNT" == "1" ]]; then pass "refuse_conflict_count is 1"; else fail "expected refuse_conflict_count=1, got $REFUSE_CONFLICT_COUNT"; fi
if [[ "$REFUSE_STALE_COUNT" == "1" ]]; then pass "refuse_stale_count is 1"; else fail "expected refuse_stale_count=1, got $REFUSE_STALE_COUNT"; fi

HUMAN="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d)"
if grep -q "Total invocations:     3" <<<"$HUMAN"; then
    pass "human-readable report shows the same total invocation count"
else
    fail "human-readable report did not show 3 total invocations: $HUMAN"
fi
if grep -q "#4242" <<<"$HUMAN" && grep -q "#4243" <<<"$HUMAN" && grep -q "#4244" <<<"$HUMAN"; then
    pass "human-readable report lists all recorded PR numbers"
else
    fail "human-readable report is missing one or more PR numbers: $HUMAN"
fi

# --- Test 6: --since window filtering -----------------------------------------
echo ""
echo "Test 6: --since excludes records older than the window"

sleep 1
SUMMARY_NOW="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 0s --json)"
RECORD_COUNT_NOW="$(jq -r '.record_count' <<<"$SUMMARY_NOW")"
if [[ "$RECORD_COUNT_NOW" == "0" ]]; then
    pass "--since 0s excludes records from before this instant"
else
    fail "expected --since 0s to exclude all 3 prior records, got record_count=$RECORD_COUNT_NOW"
fi

SUMMARY_WIDE="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 999d --json)"
if [[ "$(jq -r '.record_count' <<<"$SUMMARY_WIDE")" == "3" ]]; then
    pass "a wide --since window still includes all 3 records"
else
    fail "expected a wide window to include all 3 records, got record_count=$(jq -r '.record_count' <<<"$SUMMARY_WIDE")"
fi

# --- Test 7: --since parses d/h/m/s suffixes and a bare integer, rejects garbage ---
echo ""
echo "Test 7: --since parses d/h/m/s suffixes and a bare integer, rejects garbage"

for window in "7d" "24h" "30m" "90s" "3600"; do
    RC=0
    (cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since "$window" >/dev/null 2>&1) || RC=$?
    if [[ "$RC" == "0" ]]; then pass "--since $window is accepted"; else fail "--since $window unexpectedly failed (rc=$RC)"; fi
done

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since "bogus" >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "an invalid --since value exits 2"; else fail "expected exit 2 for an invalid --since, got $RC"; fi

# --- Test 8: argument validation ---------------------------------------------
echo ""
echo "Test 8: argument validation"

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "missing command -> exit 2"; else fail "expected exit 2, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" bogus >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "unknown command -> exit 2"; else fail "expected exit 2, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" --help >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "--help -> exit 0"; else fail "expected exit 0, got $RC"; fi

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
