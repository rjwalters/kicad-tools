#!/usr/bin/env bash
# test-claim-staleness.sh - Unit tests for claim-staleness.sh (#6514), the
# shared staleness evaluator behind judge.md's "Stale `loom:reviewing` Claim
# Check", doctor.md's "Stale `loom:treating` Claim Check" and curator.md's
# "Stale `loom:curating` Claim Check".
#
# The regression under test is the PR #6513 livelock: a single routine Builder
# post-push status comment landing after the claim used to pin that claim
# "fresh" forever (`COMMENTS_AFTER > 0`), while duplicate stand-down
# suppression simultaneously starved `STANDDOWN_COUNT` so the bounded fallback
# could never fire either. T3/T7/T8/T9 are the direct regression tests.
#
# Strategy: stub `gh` on PATH so the script never touches the network. The stub
# serves canned issue/timeline/comments fixtures from $LOOM_TEST_STUB_DIR and
# records every mutating call (POST/PATCH) with its payload.
#
# Usage:
#   ./.loom/scripts/tests/test-claim-staleness.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_SCRIPT="$HELPERS_DIR/claim-staleness.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

[[ -x "$TARGET_SCRIPT" ]] || {
    echo -e "${RED}FATAL${NC}: $TARGET_SCRIPT missing or not executable"
    exit 2
}
command -v jq >/dev/null 2>&1 || {
    echo -e "${RED}FATAL${NC}: jq required"
    exit 2
}

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

cat >"$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-claim-staleness.sh.
set -uo pipefail
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$D/gh-calls.log"

[[ "${1:-}" == "api" ]] || { echo "stub gh: unhandled args: $*" >&2; exit 3; }
shift

path="" jq_expr="" method="GET" read_stdin=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jq|-q) jq_expr="${2:-}"; shift 2 ;;
    --method|-X) method="${2:-GET}"; shift 2 ;;
    --input) read_stdin=1; shift 2 ;;
    --paginate|--silent) shift ;;
    -*) shift ;;
    *) [[ -z "$path" ]] && path="$1"; shift ;;
  esac
done

if [[ "$method" == "POST" || "$method" == "PATCH" ]]; then
  payload=""
  [[ $read_stdin -eq 1 ]] && payload="$(cat)"
  if [[ -n "${LOOM_TEST_MUTATION_FAILS:-}" ]]; then
    echo '{"message":"boom"}' >&2
    exit 1
  fi
  {
    echo "$method $path"
    printf '%s\n' "$payload"
  } >>"$LOG"
  echo '{}'
  exit 0
fi

serve() { # <fixture-file>
  local f="$D/$1"
  [[ -f "$f" ]] || { echo "stub gh: missing fixture $1" >&2; exit 1; }
  if [[ -n "$jq_expr" ]]; then jq -r "$jq_expr" "$f"; else cat "$f"; fi
}

case "$path" in
  *"/timeline"*) serve timeline.json ;;
  *"/comments"*) serve comments.json ;;
  */issues/[0-9]*)
    if [[ "${LOOM_TEST_ISSUE_READ_FAILS:-}" == "1" ]]; then
      echo '{"message":"Not Found"}' >&2
      exit 1
    fi
    serve issue.json
    ;;
  *) echo "stub gh: unhandled path: $path" >&2; exit 3 ;;
esac
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# Deterministic defaults regardless of the ambient environment.
export LOOM_STALE_REVIEWING_MINUTES=30
export LOOM_STALE_TREATING_MINUTES=60
export LOOM_STALE_CURATING_MINUTES=30
export LOOM_MAX_STANDDOWN_STREAK=3

ago() { # <minutes> -> RFC3339 UTC timestamp that many minutes in the past
    date -u -d "-$1 minutes" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null ||
        date -u -v-"$1"M +%Y-%m-%dT%H:%M:%SZ
}

set_labels() { # <label>...
    printf '%s\n' "$*" | jq -R 'split(" ") | map(select(length > 0) | {name: .}) | {labels: .}' \
        >"$STUB_DIR/issue.json"
}

set_claim() { # <label> <iso-ts>  (empty ts => no claim event in the timeline)
    if [[ -z "${2:-}" ]]; then
        echo '[]' >"$STUB_DIR/timeline.json"
    else
        jq -n --arg l "$1" --arg t "$2" \
            '[{event:"labeled", label:{name:$l}, created_at:$t}]' >"$STUB_DIR/timeline.json"
    fi
}

set_comments() { # reads a JSON array on stdin
    cat >"$STUB_DIR/comments.json"
}

reset() {
    : >"$STUB_DIR/gh-calls.log"
    unset LOOM_TEST_MUTATION_FAILS LOOM_TEST_ISSUE_READ_FAILS
    set_labels "loom:reviewing"
    echo '[]' >"$STUB_DIR/comments.json"
}
read_log() { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }

field() { # <output> <KEY>
    grep -E "^$2=" <<<"$1" | head -n 1 | cut -d= -f2-
}

echo "Testing claim-staleness.sh..."
echo ""

# --- T0: usage errors ------------------------------------------------------
reset
rc=0
"$TARGET_SCRIPT" check --number abc --label loom:reviewing >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0a: non-numeric --number is a usage error"
rc=0
"$TARGET_SCRIPT" check --number 1 --label reviewing >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0b: a non-loom label is a usage error"
rc=0
"$TARGET_SCRIPT" bogus --number 1 --label loom:reviewing >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "T0c: unknown subcommand is a usage error"

# --- T1: no claim label -> unclaimed --------------------------------------
reset
set_labels "loom:review-requested"
set_claim loom:reviewing ""
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "unclaimed" "$(field "$out" CLAIM_STATE)" "T1: an unclaimed PR reports CLAIM_STATE=unclaimed"

# --- T2: fresh claim, nothing else ----------------------------------------
reset
set_claim loom:reviewing "$(ago 5)"
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "fresh" "$(field "$out" CLAIM_STATE)" "T2: a 5-minute-old claim is fresh"

# --- T3: THE #6513 REGRESSION ---------------------------------------------
# Claim 40 minutes old; a single routine Builder post-push status comment
# landed 32 minutes ago. Under the old `COMMENTS_AFTER > 0` rule this pinned
# the claim "fresh" forever. It must now read STALE.
reset
set_claim loom:reviewing "$(ago 40)"
jq -n --arg t "$(ago 32)" '[{id: 101, created_at: $t,
  body: "Post-push status (Builder, in-turn verification): rebased onto main, CI green."}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "stale" "$(field "$out" CLAIM_STATE)" \
    "T3: a non-claimant Builder comment no longer pins a 40-minute-old claim fresh (#6513)"
assert_eq "0" "$(field "$out" ACTIVITY_COUNT)" "T3: the Builder comment is not counted as claimant activity"

# --- T4: multiple non-claimant comments still do not compound --------------
reset
set_claim loom:reviewing "$(ago 40)"
jq -n --arg a "$(ago 35)" --arg b "$(ago 20)" --arg c "$(ago 2)" \
    '[{id:101,created_at:$a,body:"Builder: pushed."},
      {id:102,created_at:$b,body:"Champion: merge-risk hold notice."},
      {id:103,created_at:$c,body:"Someone: unrelated chatter."}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "stale" "$(field "$out" CLAIM_STATE)" \
    "T4: three non-claimant comments (one 2 minutes old) still leave the claim stale"

# --- T5: a genuine claimant heartbeat DOES keep the claim fresh ------------
reset
CLAIM_TS="$(ago 40)"
set_claim loom:reviewing "$CLAIM_TS"
jq -n --arg t "$(ago 3)" --arg m "<!-- loom:claim-activity claim=$CLAIM_TS -->" \
    '[{id:201,created_at:$t,body:("Judge: still running the test suite.\n" + $m)}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "fresh" "$(field "$out" CLAIM_STATE)" \
    "T5: a marked claimant heartbeat 3 minutes ago keeps a 40-minute-old claim fresh"
assert_eq "1" "$(field "$out" ACTIVITY_COUNT)" "T5: the heartbeat is counted as claimant activity"

# --- T6: a heartbeat only extends the claim, it never pins it --------------
reset
CLAIM_TS="$(ago 120)"
set_claim loom:reviewing "$CLAIM_TS"
jq -n --arg t "$(ago 45)" --arg m "<!-- loom:claim-activity claim=$CLAIM_TS -->" \
    '[{id:201,created_at:$t,body:("Judge: mid-review.\n" + $m)}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "stale" "$(field "$out" CLAIM_STATE)" \
    "T6: a heartbeat 45 minutes ago (> 30m idle) leaves the claim stale"

# --- T7: first stand-down POSTs a seq=1 comment ---------------------------
reset
set_claim loom:reviewing "$(ago 5)"
out="$("$TARGET_SCRIPT" standdown --repo owner/repo --number 6513 --label loom:reviewing)"
log="$(read_log)"
assert_eq "posted:1" "$(field "$out" STANDDOWN_ACTION)" "T7: first stand-down posts seq=1"
assert_contains "$log" "POST repos/owner/repo/issues/6513/comments" "T7: it POSTs a new comment"
assert_contains "$log" "loom:standdown claim=" "T7: the comment carries the stand-down marker"
assert_contains "$log" "seq=1" "T7: the marker records seq=1"

# --- T8: THE STREAK-STARVATION REGRESSION ---------------------------------
# A stand-down comment already exists for this claim. The old logic skipped
# silently, so STANDDOWN_COUNT froze at 1 forever. It must now bump seq in
# place: one visible comment (the #5123 goal), a growing streak (the #4618
# AC3 goal).
reset
CLAIM_TS="$(ago 5)"
set_claim loom:reviewing "$CLAIM_TS"
jq -n --arg t "$(ago 2)" --arg m "<!-- loom:standdown claim=$CLAIM_TS seq=1 -->" \
    '[{id:301,created_at:$t,body:("Judge pass: standing down.\n" + $m)}]' | set_comments
out="$("$TARGET_SCRIPT" standdown --repo owner/repo --number 6513 --label loom:reviewing)"
log="$(read_log)"
assert_eq "bumped:301:2" "$(field "$out" STANDDOWN_ACTION)" \
    "T8: a duplicate stand-down bumps the existing comment to seq=2 instead of freezing the streak"
assert_contains "$log" "PATCH repos/owner/repo/issues/comments/301" "T8: it PATCHes the existing comment"
assert_not_contains "$log" "POST repos/owner/repo/issues/6513/comments" \
    "T8: it does NOT post a duplicate comment (#5123 still holds)"
assert_contains "$log" "seq=2" "T8: the bumped marker records seq=2"

# --- T9: a legacy seq-less marker counts as seq=1 and bumps to 2 -----------
reset
CLAIM_TS="$(ago 5)"
set_claim loom:reviewing "$CLAIM_TS"
jq -n --arg t "$(ago 2)" --arg m "<!-- loom:standdown claim=$CLAIM_TS -->" \
    '[{id:302,created_at:$t,body:("Judge pass: standing down.\n" + $m)}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "1" "$(field "$out" STANDDOWN_COUNT)" "T9: a legacy seq-less stand-down marker counts as 1"
out="$("$TARGET_SCRIPT" standdown --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "bumped:302:2" "$(field "$out" STANDDOWN_ACTION)" "T9: it is bumped to seq=2 in place"

# --- T10: bounded fallback fires once the streak AND the age floor are met --
reset
CLAIM_TS="$(ago 40)"
set_claim loom:reviewing "$CLAIM_TS"
jq -n --arg t "$(ago 2)" --arg m "<!-- loom:standdown claim=$CLAIM_TS seq=3 -->" \
    --arg b "$(ago 30)" \
    '[{id:401,created_at:$b,body:"Builder: pushed a fixup."},
      {id:402,created_at:$t,body:("Judge pass: standing down.\n" + $m)}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "stale-bounded-fallback" "$(field "$out" CLAIM_STATE)" \
    "T10: seq=3 plus a 40-minute-old claim force-reclaims despite a non-claimant comment"
assert_eq "3" "$(field "$out" STANDDOWN_COUNT)" "T10: the streak reads 3 from the bumped marker alone"

# --- T11: the fallback still respects the #4790 age floor ------------------
reset
CLAIM_TS="$(ago 10)"
set_claim loom:reviewing "$CLAIM_TS"
jq -n --arg t "$(ago 1)" --arg m "<!-- loom:standdown claim=$CLAIM_TS seq=5 -->" \
    '[{id:501,created_at:$t,body:("Judge pass: standing down.\n" + $m)}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "fresh" "$(field "$out" CLAIM_STATE)" \
    "T11: a high peer-arrival streak on a 10-minute-old claim does NOT force-reclaim (#4790)"

# --- T12: the fallback beats an activity-marker-spamming zombie ------------
reset
CLAIM_TS="$(ago 40)"
set_claim loom:reviewing "$CLAIM_TS"
jq -n --arg t "$(ago 1)" --arg am "<!-- loom:claim-activity claim=$CLAIM_TS -->" \
    --arg sm "<!-- loom:standdown claim=$CLAIM_TS seq=3 -->" --arg s "$(ago 2)" \
    '[{id:601,created_at:$t,body:("Judge: still working.\n" + $am)},
      {id:602,created_at:$s,body:("Judge pass: standing down.\n" + $sm)}]' | set_comments
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "stale-bounded-fallback" "$(field "$out" CLAIM_STATE)" \
    "T12: the fallback keys on claim age, so a heartbeat loop cannot hold the claim forever"

# --- T13: fail-safe paths --------------------------------------------------
reset
set_claim loom:reviewing ""
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "unknown" "$(field "$out" CLAIM_STATE)" \
    "T13a: a labelled item with no claim event in the timeline fails safe to unknown"
out="$("$TARGET_SCRIPT" standdown --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "skipped-unknown-claim" "$(field "$out" STANDDOWN_ACTION)" \
    "T13b: standdown posts nothing when the claim timestamp is unknown"
assert_eq "" "$(read_log)" "T13c: no mutating call is made on the unknown path"

reset
set_claim loom:reviewing "$(ago 5)"
export LOOM_TEST_ISSUE_READ_FAILS=1
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing)"
unset LOOM_TEST_ISSUE_READ_FAILS
assert_eq "unknown" "$(field "$out" CLAIM_STATE)" "T13d: a failed label read fails safe to unknown"

# --- T14: standdown is a no-op once the claim is stale ---------------------
reset
set_claim loom:reviewing "$(ago 40)"
out="$("$TARGET_SCRIPT" standdown --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "skipped-not-fresh" "$(field "$out" STANDDOWN_ACTION)" \
    "T14: a stale claim is not stood down (the caller reclaims instead)"
assert_eq "" "$(read_log)" "T14: no comment is posted for a stale claim"

# --- T15: --dry-run mutates nothing ---------------------------------------
reset
set_claim loom:reviewing "$(ago 5)"
out="$("$TARGET_SCRIPT" standdown --repo owner/repo --number 6513 --label loom:reviewing --dry-run)"
assert_eq "would-post:1" "$(field "$out" STANDDOWN_ACTION)" "T15: --dry-run reports would-post:1"
assert_eq "" "$(read_log)" "T15: --dry-run makes zero mutating calls"

# --- T16: the marker subcommand prints the activity marker ----------------
reset
CLAIM_TS="$(ago 5)"
set_claim loom:reviewing "$CLAIM_TS"
out="$("$TARGET_SCRIPT" marker --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "<!-- loom:claim-activity claim=$CLAIM_TS -->" "$out" \
    "T16: marker prints the activity marker for the live claim"
set_labels "loom:review-requested"
out="$("$TARGET_SCRIPT" marker --repo owner/repo --number 6513 --label loom:reviewing)"
assert_eq "" "$out" "T16: marker prints nothing when there is no live claim"

# --- T17: per-label thresholds -------------------------------------------
reset
set_labels "loom:treating"
set_claim loom:treating "$(ago 45)"
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:treating)"
assert_eq "fresh" "$(field "$out" CLAIM_STATE)" \
    "T17a: loom:treating uses the 60-minute Doctor threshold (45m is still fresh)"
assert_eq "60" "$(field "$out" STALE_MINUTES)" "T17b: STALE_MINUTES reports the Doctor default"
reset
set_labels "loom:curating"
set_claim loom:curating "$(ago 45)"
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:curating)"
assert_eq "stale" "$(field "$out" CLAIM_STATE)" \
    "T17c: loom:curating uses the 30-minute Curator threshold (45m is stale)"

# --- T18: --json output ---------------------------------------------------
reset
set_claim loom:reviewing "$(ago 40)"
out="$("$TARGET_SCRIPT" check --repo owner/repo --number 6513 --label loom:reviewing --json)"
assert_eq "stale" "$(jq -r '.claim_state' <<<"$out")" "T18a: --json reports claim_state"
assert_eq "loom:reviewing" "$(jq -r '.label' <<<"$out")" "T18b: --json reports the label"
assert_eq "30" "$(jq -r '.stale_minutes' <<<"$out")" "T18c: --json reports stale_minutes"

# --- T19: a mutation failure is reported, not swallowed as success --------
reset
set_claim loom:reviewing "$(ago 5)"
export LOOM_TEST_MUTATION_FAILS=1
out="$("$TARGET_SCRIPT" standdown --repo owner/repo --number 6513 --label loom:reviewing)"
unset LOOM_TEST_MUTATION_FAILS
assert_eq "failed-post" "$(field "$out" STANDDOWN_ACTION)" "T19: a failed POST reports failed-post"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
