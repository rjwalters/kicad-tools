#!/usr/bin/env bash
# test-classify-ac-verification.sh - tests for classify-ac-verification.sh,
# Champion's out-of-band acceptance-criteria gate (#6883).
#
# Covers the five exit states (0 CLEAR / 10 NO-AC / 11 SATISFIED / 12 UNVERIFIED
# / 13 STALE-MARKER), the extraction scope (AC headings only -- Test Plan and
# Dependencies checklists are deliberately NOT swept), wrapped-bullet joining,
# "a checked box is not evidence", the `loom:ac-verified` marker's anchoring and
# abbreviation-tolerant SHA comparison, the #4840-style prose-quoting false-
# positive guard, and the bare-word false-positive discipline the vocabulary is
# built around.
#
# Hermetic: drives the script purely through --body-file / --evidence-file, so
# no forge, network, or token is touched.
#
# Usage:
#   ./defaults/scripts/tests/test-classify-ac-verification.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLASSIFY_SCRIPT="$SCRIPTS_DIR/classify-ac-verification.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg (expected '$expected', got '$actual')"
    fi
}

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (expected substring '$needle' in: $haystack)"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (unexpected substring '$needle' in: $haystack)"
    fi
}

if [[ ! -x "$CLASSIFY_SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $CLASSIFY_SCRIPT not found or not executable" >&2
    exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/classify-ac.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

# Capture stdout, stderr, and exit code from ONE invocation. Sets CAP_OUT,
# CAP_ERR, CAP_RC.
CAP_OUT=""
CAP_ERR=""
CAP_RC=0
run_classify() { # <body-text> [<evidence-text> [<head-sha>]]
    local body="$1" evidence="${2:-}" head_sha="${3:-}"
    local body_file="$WORK_DIR/body.md" evidence_file="$WORK_DIR/evidence.md"
    printf '%s\n' "$body" >"$body_file"
    local args=(--body-file "$body_file")
    if [[ -n "$evidence" ]]; then
        printf '%s\n' "$evidence" >"$evidence_file"
        args+=(--evidence-file "$evidence_file")
    fi
    [[ -n "$head_sha" ]] && args+=(--head-sha "$head_sha")
    CAP_OUT="$("$CLASSIFY_SCRIPT" "${args[@]}" 2>"$WORK_DIR/err.txt")"
    CAP_RC=$?
    CAP_ERR="$(cat "$WORK_DIR/err.txt")"
}

HEAD_SHA="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$CLASSIFY_SCRIPT" ]]; then
    pass "classify-ac-verification.sh is executable"
else
    fail "classify-ac-verification.sh is missing or not executable"
fi

# -------- Test 2: no AC checklist at all -> exit 10 (Step 4 unchanged) --------
echo "Test 2: no acceptance-criteria checklist -> exit 10, silent"
run_classify "## Summary

Refactors the token pool selector. No checklist anywhere in this body."
assert_eq "10" "$CAP_RC" "body with no AC heading exits 10 (NO-AC)"
assert_eq "" "$CAP_OUT" "NO-AC body prints nothing"
assert_eq "" "$CAP_ERR" "NO-AC body is silent on stderr too (a no-op, not a warning)"

# -------- Test 3: AC checklist, every item CI-checkable -> exit 0 --------
echo "Test 3: all criteria CI-checkable -> exit 0, silent"
run_classify "## Acceptance Criteria

- [ ] \`cargo test\` passes with the new selector
- [ ] the docs table lists all six risk axes
- [x] \`pnpm check:ci\` is green"
assert_eq "0" "$CAP_RC" "all-CI-checkable AC list exits 0 (CLEAR)"
assert_eq "" "$CAP_OUT" "CLEAR body prints nothing"

# -------- Test 4: out-of-band criterion, no marker -> exit 12 + verbatim text --------
echo "Test 4: out-of-band criterion with no evidence marker -> exit 12"
run_classify "## Suggested acceptance criteria

- [ ] The filter no longer drops items matching the pattern, covered by a unit test
- [ ] Confirm the dropped item is processed (or explicitly rejected on the
      merits) on the next real run — the current failure is invisible"
assert_eq "12" "$CAP_RC" "out-of-band criterion with no marker exits 12 (UNVERIFIED)"
assert_contains "real run" "$CAP_OUT" "the matched phrase is reported"
assert_contains "Confirm the dropped item is processed" "$CAP_OUT" "the criterion text is reported for verbatim quoting"
assert_not_contains "The filter no longer drops items" "$CAP_OUT" "the CI-checkable sibling criterion is NOT reported"

# -------- Test 5: a wrapped bullet is joined into one greppable line --------
echo "Test 5: wrapped continuation lines join into one criterion"
assert_eq "1" "$(printf '%s' "$CAP_OUT" | grep -c 'real run')" "the wrapped criterion is emitted as exactly one line"
assert_contains "merits) on the next real run" "$CAP_OUT" "the continuation line is joined onto the bullet, not dropped"

# -------- Test 6: a CHECKED box is not evidence --------
echo "Test 6: a checked [x] out-of-band criterion is still classified out-of-band"
run_classify "## Acceptance Criteria

- [x] Verified against the live source and the item now appears"
assert_eq "12" "$CAP_RC" "a checked box does not satisfy an out-of-band criterion"
assert_contains "against the live" "$CAP_OUT" "the checked criterion is still reported"

# -------- Test 7: Test Plan / Dependencies checklists are NOT swept --------
echo "Test 7: only AC-headed checklists are scanned"
run_classify "## Acceptance Criteria

- [ ] the parser rejects a malformed marker

## Test Plan

- [ ] Manual verification: watch the live dashboard on the next run

## Dependencies

- [x] #123: requires a live run of the ingest job"
assert_eq "0" "$CAP_RC" "out-of-band phrasing under Test Plan / Dependencies does not trigger the gate"
assert_eq "" "$CAP_OUT" "no criteria are reported from non-AC checklists"

# -------- Test 8: the AC section ends at the next heading --------
echo "Test 8: the AC section terminates at the next heading of any level"
run_classify "### Sharpened Acceptance Criteria

- [ ] the classifier is wired into Step 4

#### Notes

- [ ] someone should watch the live site for a week"
assert_eq "0" "$CAP_RC" "a checklist after the next heading is outside the AC section"

# -------- Test 9: multiple AC headings are all scanned --------
echo "Test 9: several acceptance-criteria headings in one body are all scanned"
run_classify "## Suggested acceptance criteria

- [ ] the fix lands with a unit test

---

## Curator Enhancement

### Sharpened Acceptance Criteria

- [ ] confirm the behaviour holds over the next 24 hours of scheduled ingest"
assert_eq "12" "$CAP_RC" "an out-of-band criterion under a SECOND AC heading is found"
assert_contains "over the next" "$CAP_OUT" "the second heading's criterion reports its matched phrase"

# -------- Test 10: multiple out-of-band criteria -> one line each --------
echo "Test 10: every out-of-band criterion is reported, one line each"
run_classify "## Acceptance Criteria

- [ ] confirm the item is processed on the next real run
- [ ] the docs table is updated
- [ ] a manual verification of the live site is recorded"
assert_eq "12" "$CAP_RC" "multiple out-of-band criteria exit 12"
assert_eq "2" "$(printf '%s\n' "$CAP_OUT" | grep -c .)" "exactly two criteria are reported (the docs one is CI-checkable)"

# -------- Test 11: current-SHA marker satisfies the gate -> exit 11 --------
echo "Test 11: a current-SHA loom:ac-verified marker satisfies the gate"
run_classify "## Acceptance Criteria

- [ ] confirm the item is processed on the next real run" \
"Ran the ingest job at 14:02 UTC against the live source; the previously-dropped
listing was processed. Log attached.

<!-- loom:ac-verified sha=$HEAD_SHA -->" \
"$HEAD_SHA"
assert_eq "11" "$CAP_RC" "a marker whose SHA is the head exits 11 (SATISFIED)"

# -------- Test 12: abbreviated marker SHA still matches the full head --------
echo "Test 12: an abbreviated marker SHA matches the full head SHA"
run_classify "## Acceptance Criteria

- [ ] confirm the item is processed on the next real run" \
"<!-- loom:ac-verified sha=a1b2c3d -->" \
"$HEAD_SHA"
assert_eq "11" "$CAP_RC" "a 7-char abbreviated marker SHA matches the 40-char head"

# -------- Test 13: a marker for a different tree is STALE, not satisfying --------
echo "Test 13: a marker naming a different SHA -> exit 13 (STALE-MARKER)"
run_classify "## Acceptance Criteria

- [ ] confirm the item is processed on the next real run" \
"<!-- loom:ac-verified sha=ffffffffffffffffffffffffffffffffffffffff -->" \
"$HEAD_SHA"
assert_eq "13" "$CAP_RC" "a marker describing another tree exits 13, not 11"
assert_contains "real run" "$CAP_OUT" "the unmet criterion is still reported on the stale-marker path"

# -------- Test 14: marker present but no head SHA known -> fails closed --------
echo "Test 14: a marker with no head SHA to check it against fails closed"
run_classify "## Acceptance Criteria

- [ ] confirm the item is processed on the next real run" \
"<!-- loom:ac-verified sha=a1b2c3d -->"
assert_eq "13" "$CAP_RC" "an uncheckable marker holds (13) rather than satisfying (11)"

# -------- Test 15: prose quoting the marker syntax is not a live marker --------
echo "Test 15: prose quoting the marker syntax (<head> placeholder) is not a match"
run_classify "## Acceptance Criteria

- [ ] confirm the item is processed on the next real run" \
"The convention is \`<!-- loom:ac-verified sha=<head> -->\`, posted once the
step has actually been performed. Nobody has performed it yet." \
"$HEAD_SHA"
assert_eq "12" "$CAP_RC" "a placeholder-quoting mention does not satisfy the gate"

# -------- Test 16: bare-word false-positive discipline --------
echo "Test 16: bare words that appear in ordinary AC prose do not trigger the gate"
run_classify "## Acceptance Criteria

- [ ] the dev server supports live reload
- [ ] the production build stays under the size budget
- [ ] run the test suite in CI and verify the output is deterministic
- [ ] the fixture uses a real-world example payload
- [ ] an end-to-end test covers the happy path"
assert_eq "0" "$CAP_RC" "live reload / production build / run the test suite / real-world / end-to-end are all CI-checkable"
assert_eq "" "$CAP_OUT" "no false-positive criterion is reported"

# -------- Test 17: '* [ ]' bullet form is recognized --------
echo "Test 17: the '* [ ]' task-list form is recognized"
run_classify "## Acceptance Criteria

* [ ] confirm the behaviour on a real run of the nightly job"
assert_eq "12" "$CAP_RC" "'* [ ]' bullets are classified like '- [ ]' bullets"

# -------- Test 18: matching is case-insensitive --------
echo "Test 18: phrase matching is case-insensitive"
run_classify "## Acceptance Criteria

- [ ] Confirm on the NEXT REAL RUN that the listing appears"
assert_eq "12" "$CAP_RC" "an upper-cased phrase still matches"

# -------- Test 19: usage errors fail closed with a nonzero exit --------
echo "Test 19: usage / precondition failures exit 1"
"$CLASSIFY_SCRIPT" >/dev/null 2>&1
assert_eq "1" "$?" "no arguments exits 1 (ERROR, caller must treat as a hold)"
"$CLASSIFY_SCRIPT" --body-file "$WORK_DIR/does-not-exist.md" >/dev/null 2>&1
assert_eq "1" "$?" "a missing --body-file exits 1"
"$CLASSIFY_SCRIPT" --bogus-flag >/dev/null 2>&1
assert_eq "1" "$?" "an unknown flag exits 1"

# -------- Test 20: the documented vocabulary is the one the script uses --------
echo "Test 20: the phrase vocabulary is present and instruction-shaped"
# Sourcing re-runs the subject's own TTY-colour block, which blanks $NC in a
# non-TTY run — save and restore it so the summary below stays readable.
_NC_SAVE="$NC"
# shellcheck source=/dev/null
source "$CLASSIFY_SCRIPT"
NC="$_NC_SAVE"
assert_contains "real run" "${OUT_OF_BAND_PHRASES[*]}" "vocabulary contains 'real run'"
assert_contains "over the next" "${OUT_OF_BAND_PHRASES[*]}" "vocabulary contains 'over the next'"
assert_contains "manual verification" "${OUT_OF_BAND_PHRASES[*]}" "vocabulary contains 'manual verification'"
BARE_WORD_FOUND=0
for phrase in "${OUT_OF_BAND_PHRASES[@]}"; do
    case "$phrase" in
        live|real|run|verify|production|manual|observe|end-to-end) BARE_WORD_FOUND=1 ;;
    esac
done
assert_eq "0" "$BARE_WORD_FOUND" "no bare single-word phrase is in the vocabulary"

# -------- Test 21: forge evidence counts only via the trust filter (#9548) --------
echo "Test 21: --issue evidence is read through forge trusted-comments, never raw"
T21="$WORK_DIR/t21"; mkdir -p "$T21"
SHA21="abcdef1234567890abcdef1234567890abcdef12"
# gh: the issue body carries an out-of-band criterion; any raw comment read
# would see an (outsider's) marker for the head.
cat > "$T21/gh" <<STUB
#!/usr/bin/env bash
case "\$*" in
  *"--json body"*) printf '## Acceptance Criteria\n\n- [ ] Confirm it works on the next real run\n' ;;
  *"--json headRefOid"*) echo "$SHA21" ;;
  *) printf '<!-- loom:ac-verified sha=$SHA21 -->\n' ;;
esac
STUB
# loom-daemon stub: prints the listing in \$T21_LISTING, or fails.
cat > "$T21/loom-daemon" <<'STUB'
#!/usr/bin/env bash
[[ -n "${T21_LISTING:-}" ]] || exit 1
printf '%s\n' "$T21_LISTING"
STUB
chmod +x "$T21/gh" "$T21/loom-daemon"
run21() { PATH="$T21:$PATH" LOOM_DAEMON_BIN="$T21/loom-daemon" T21_LISTING="$1" \
    "$CLASSIFY_SCRIPT" --issue 5 --pr 6 --repo o/r >/dev/null 2>&1; echo $?; }
assert_eq "12" "$(run21 '[]')" "no trusted marker -> 12, whatever raw comments say"
assert_eq "12" "$(run21 '')" "filter unavailable -> 12 (held open, fail safe)"
assert_eq "11" "$(run21 "[{\"body\":\"ran it <!-- loom:ac-verified sha=$SHA21 -->\"}]")" "a trusted marker for the head -> 11"
echo ""

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
