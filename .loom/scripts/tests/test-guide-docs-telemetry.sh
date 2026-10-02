#!/usr/bin/env bash
# test-guide-docs-telemetry.sh - Regression test for issue #6136
#
# Fleet observability gap: there was no per-role/per-category breakdown of
# fleet token spend — support-role crons (Judge/Champion/Curator/Guide) never
# emit `sweep.*` telemetry and fall into an undifferentiated "unattributed"
# bucket (loom-ui:docs/token-analytics.md). This suite covers the new,
# deliberately decoupled local telemetry surface that closes one narrow slice
# of that gap: Guide's Document Maintenance phase (doc-maintenance PRs).
#
# Verifies:
#   1. `record` appends a well-formed JSONL line with the expected fields,
#      including a null duration_sec when none is supplied (never coerced
#      to 0) and a real integer when one is.
#   2. `record` validates --pr (required, numeric).
#   3. `report` on an empty/missing log renders "no activity" without
#      erroring (the explicit edge case from the issue's Test Plan) and with
#      the same behavior for both human and --json output.
#   4. `report` correctly filters records to the requested --since window
#      and computes pr_count / total_duration_sec / duration_known_count.
#   5. `report --since` rejects a malformed window value.
#   6. argument validation (unknown command, missing --pr).
#   7. `docs-guide-lock.sh age` — the phase-duration proxy source — reports
#      0 (no error) when unheld, a live in-progress lock's age, and errors
#      (exit 1) when the lock is not held.
#   8. Static wiring: guide.md's create_docs_pr() calls both
#      `docs-guide-lock.sh age` and `guide-docs-telemetry.sh record` on the
#      success path, and does so BEFORE releasing the lock (so `age` still
#      reports this tick's elapsed time).
#
# Hermetic: throwaway git repo under mktemp -d, no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

TELEMETRY_SH="$SCRIPTS_DIR/guide-docs-telemetry.sh"
LOCK_SH="$SCRIPTS_DIR/docs-guide-lock.sh"
GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (missing pattern: $pattern)"; fi
}

for bin in "$TELEMETRY_SH" "$LOCK_SH"; do
    if [[ ! -x "$bin" ]]; then
        fail "$bin missing or not executable"
    fi
done
command -v jq >/dev/null 2>&1 || fail "jq is required by guide-docs-telemetry.sh but is not on PATH"

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

LOG_FILE="$SANDBOX_REPO/.loom/logs/guide-docs-telemetry.jsonl"
export LOOM_GUIDE_DOCS_TELEMETRY_LOG="$LOG_FILE"

# --- Test 1: report on a missing log -----------------------------------------
echo "Test 1: report renders 'no activity' when the log does not exist yet"

OUT="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d 2>&1)"
RC=$?
if [[ "$RC" == "0" ]]; then pass "report exits 0 on a missing log"; else fail "report exited $RC on a missing log"; fi
if grep -q "No doc-maintenance PRs in this window" <<<"$OUT"; then
    pass "report reports zero activity without erroring"
else
    fail "report did not report zero activity cleanly: $OUT"
fi

OUT_JSON="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d --json 2>&1)"
RC=$?
if [[ "$RC" == "0" ]] && [[ "$(jq -r '.pr_count' <<<"$OUT_JSON")" == "0" ]]; then
    pass "report --json reports pr_count: 0 on a missing log"
else
    fail "report --json did not cleanly report zero activity: $OUT_JSON (rc=$RC)"
fi

# --- Test 2: record validates --pr -------------------------------------------
echo ""
echo "Test 2: record requires a numeric --pr"

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "record with no --pr exits 2"; else fail "expected exit 2, got $RC"; fi

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr notanumber >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "2" ]]; then pass "record with a non-numeric --pr exits 2"; else fail "expected exit 2, got $RC"; fi

if [[ -f "$LOG_FILE" ]]; then
    fail "a rejected record must not create/append to the log file"
else
    pass "no log file was created by the rejected record calls"
fi

# --- Test 3: record appends a well-formed line -------------------------------
echo ""
echo "Test 3: record appends a well-formed JSONL line"

RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 4242 --repo acme/widgets --duration-sec 77 --files "WORK_LOG.md,README.md" >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then pass "record with all fields exits 0"; else fail "record exited $RC"; fi

if [[ -f "$LOG_FILE" ]]; then pass "log file created"; else fail "log file not created at $LOG_FILE"; fi

LAST_LINE="$(tail -1 "$LOG_FILE" 2>/dev/null)"
if [[ -n "$LAST_LINE" ]] && jq -e . >/dev/null 2>&1 <<<"$LAST_LINE"; then
    pass "appended line is valid JSON"
else
    fail "appended line is not valid JSON: $LAST_LINE"
fi

if [[ "$(jq -r '.record.kind' <<<"$LAST_LINE")" == "guide.docs_maintenance" ]]; then
    pass "record.kind is 'guide.docs_maintenance'"
else
    fail "unexpected record.kind: $(jq -r '.record.kind' <<<"$LAST_LINE")"
fi

if [[ "$(jq -r '.record.pr_number' <<<"$LAST_LINE")" == "4242" ]]; then
    pass "record.pr_number matches --pr"
else
    fail "unexpected record.pr_number: $(jq -r '.record.pr_number' <<<"$LAST_LINE")"
fi

if [[ "$(jq -r '.record.duration_sec' <<<"$LAST_LINE")" == "77" ]]; then
    pass "record.duration_sec matches --duration-sec"
else
    fail "unexpected record.duration_sec: $(jq -r '.record.duration_sec' <<<"$LAST_LINE")"
fi

if [[ "$(jq -c '.record.files_changed' <<<"$LAST_LINE")" == '["WORK_LOG.md","README.md"]' ]]; then
    pass "record.files_changed parses the --files CSV into an array"
else
    fail "unexpected record.files_changed: $(jq -c '.record.files_changed' <<<"$LAST_LINE")"
fi

if [[ -n "$(jq -r '.emitted_at_epoch' <<<"$LAST_LINE")" ]] && [[ "$(jq -r '.emitted_at_epoch | type' <<<"$LAST_LINE")" == "number" ]]; then
    pass "emitted_at_epoch is present and numeric (used for window filtering)"
else
    fail "emitted_at_epoch missing or non-numeric"
fi

# --- Test 4: duration_sec is null (not 0) when omitted -----------------------
echo ""
echo "Test 4: an omitted --duration-sec records null, never 0"

(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 4243 --repo acme/widgets >/dev/null 2>&1)
LAST_LINE="$(tail -1 "$LOG_FILE")"
if [[ "$(jq -r '.record.duration_sec' <<<"$LAST_LINE")" == "null" ]]; then
    pass "duration_sec is null when --duration-sec is not supplied"
else
    fail "duration_sec should be null, got: $(jq -r '.record.duration_sec' <<<"$LAST_LINE")"
fi

# A malformed --duration-sec (e.g. an empty string forwarded from a failed
# `docs-guide-lock.sh age` call) must also degrade to null, not crash.
RC=0
(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" record --pr 4244 --repo acme/widgets --duration-sec "" >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "0" ]]; then
    pass "an empty --duration-sec value does not crash record"
else
    fail "record crashed (rc=$RC) on an empty --duration-sec value"
fi
LAST_LINE="$(tail -1 "$LOG_FILE")"
if [[ "$(jq -r '.record.duration_sec' <<<"$LAST_LINE")" == "null" ]]; then
    pass "an empty --duration-sec value degrades to null"
else
    fail "expected null duration_sec for an empty value, got: $(jq -r '.record.duration_sec' <<<"$LAST_LINE")"
fi

# --- Test 5: report aggregates across recorded PRs ----------------------------
echo ""
echo "Test 5: report aggregates pr_count / total_duration_sec / duration_known_count"

SUMMARY="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d --json)"
PR_COUNT="$(jq -r '.pr_count' <<<"$SUMMARY")"
TOTAL_DURATION="$(jq -r '.total_duration_sec' <<<"$SUMMARY")"
DURATION_KNOWN="$(jq -r '.duration_known_count' <<<"$SUMMARY")"

if [[ "$PR_COUNT" == "3" ]]; then pass "pr_count reflects all 3 recorded PRs"; else fail "expected pr_count=3, got $PR_COUNT"; fi
if [[ "$TOTAL_DURATION" == "77" ]]; then pass "total_duration_sec sums only the known durations (77)"; else fail "expected total_duration_sec=77, got $TOTAL_DURATION"; fi
if [[ "$DURATION_KNOWN" == "1" ]]; then pass "duration_known_count counts only non-null durations (1 of 3)"; else fail "expected duration_known_count=1, got $DURATION_KNOWN"; fi

HUMAN="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 7d)"
if grep -q "PRs opened:            3" <<<"$HUMAN"; then
    pass "human-readable report shows the same PR count"
else
    fail "human-readable report did not show 3 PRs opened: $HUMAN"
fi
if grep -q "#4242" <<<"$HUMAN" && grep -q "#4243" <<<"$HUMAN" && grep -q "#4244" <<<"$HUMAN"; then
    pass "human-readable report lists all recorded PR numbers"
else
    fail "human-readable report is missing one or more PR numbers: $HUMAN"
fi

# --- Test 6: --since window filtering -----------------------------------------
echo ""
echo "Test 6: --since excludes records older than the window"

# A window of 0 seconds must exclude everything recorded a moment ago (the
# cutoff is computed at query time, strictly after every prior `record` call
# in this suite completed).
sleep 1
SUMMARY_NOW="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 0s --json)"
PR_COUNT_NOW="$(jq -r '.pr_count' <<<"$SUMMARY_NOW")"
if [[ "$PR_COUNT_NOW" == "0" ]]; then
    pass "--since 0s excludes records from before this instant"
else
    fail "expected --since 0s to exclude all 3 prior records, got pr_count=$PR_COUNT_NOW"
fi

# A large window must still include everything.
SUMMARY_WIDE="$(cd "$SANDBOX_REPO" && "$TELEMETRY_SH" report --since 999d --json)"
if [[ "$(jq -r '.pr_count' <<<"$SUMMARY_WIDE")" == "3" ]]; then
    pass "a wide --since window still includes all 3 records"
else
    fail "expected a wide window to include all 3 records, got pr_count=$(jq -r '.pr_count' <<<"$SUMMARY_WIDE")"
fi

# --- Test 7: --since accepts multiple unit suffixes and rejects garbage ------
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

# --- Test 9: docs-guide-lock.sh age -------------------------------------------
echo ""
echo "Test 9: docs-guide-lock.sh age reports elapsed lock-hold time"

RC=0
(cd "$SANDBOX_REPO" && "$LOCK_SH" age >/dev/null 2>&1) || RC=$?
if [[ "$RC" == "1" ]]; then pass "age exits 1 when the lock is not held"; else fail "expected exit 1 when unheld, got $RC"; fi

(cd "$SANDBOX_REPO" && "$LOCK_SH" acquire >/dev/null 2>&1)
sleep 2
AGE="$(cd "$SANDBOX_REPO" && "$LOCK_SH" age 2>/dev/null)"
if [[ "$AGE" =~ ^[0-9]+$ ]] && [[ "$AGE" -ge 1 ]]; then
    pass "age reports a positive integer while the lock is held (got ${AGE}s)"
else
    fail "expected a positive integer age, got: '$AGE'"
fi
(cd "$SANDBOX_REPO" && "$LOCK_SH" release >/dev/null 2>&1)

# --- Test 10: guide.md wiring -------------------------------------------------
echo ""
echo "Test 10: guide.md's create_docs_pr() emits telemetry before releasing the lock"

assert_grep 'guide-docs-telemetry\.sh record' "$GUIDE_MD" \
    "create_docs_pr() calls guide-docs-telemetry.sh record"
assert_grep 'docs-guide-lock\.sh age' "$GUIDE_MD" \
    "create_docs_pr() reads docs-guide-lock.sh age for the duration proxy"

RECORD_LINE="$(grep -n 'guide-docs-telemetry\.sh record' "$GUIDE_MD" | head -1 | cut -d: -f1)"
# The success-path release (the one following `gh pr create`, not Step 1's or
# the "no changes"/cross-host-recheck early releases) — find the LAST release
# call site in the file, which is create_docs_pr()'s final one.
FINAL_RELEASE_LINE="$(grep -n 'docs-guide-lock\.sh release' "$GUIDE_MD" | tail -1 | cut -d: -f1)"
if [[ -n "$RECORD_LINE" && -n "$FINAL_RELEASE_LINE" && "$RECORD_LINE" -lt "$FINAL_RELEASE_LINE" ]]; then
    pass "telemetry is recorded BEFORE the lock is released (line $RECORD_LINE < $FINAL_RELEASE_LINE)"
else
    fail "expected the telemetry record call (line ${RECORD_LINE:-?}) before the final lock release (line ${FINAL_RELEASE_LINE:-?})"
fi

CREATE_LINE="$(grep -n '&& gh pr create' "$GUIDE_MD" | head -1 | cut -d: -f1)"
if [[ -n "$CREATE_LINE" && -n "$RECORD_LINE" && "$CREATE_LINE" -lt "$RECORD_LINE" ]]; then
    pass "telemetry is recorded AFTER gh pr create (line $CREATE_LINE < $RECORD_LINE), so it can carry the PR number"
else
    fail "expected the telemetry record call (line ${RECORD_LINE:-?}) after gh pr create (line ${CREATE_LINE:-?})"
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
