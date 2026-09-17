#!/usr/bin/env bash
# test-check-evaluating-staleness.sh - Unit tests for
# check-evaluating-staleness.sh (#6828).
#
# check-evaluating-staleness.sh is the shared "is this claim label stale?"
# classifier used by champion-issue-promo.md's Claim section AND the
# self-healing rescan pass that lists `loom:evaluating` issues directly
# (bypassing champion.md's discovery-query exclusion of that label) so a
# claim left behind by a Champion pass that died mid-evaluation is not
# permanently invisible.
#
# This is a black-box test: the script is a full CLI (no functions to
# source), so `gh` is stubbed on PATH and the real script is invoked as a
# subprocess, asserting on stdout / exit code. Real `jq` and `date` are used
# unstubbed. Mirrors the stubbing pattern in test-verdict-staleness-guard.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-check-evaluating-staleness.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SUT="$SCRIPTS_DIR/check-evaluating-staleness.sh"

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

# assert_contains/assert_not_contains use a pure-bash substring match (no
# forked printf|grep pipeline) so a transient fork/exec failure under
# run-ci-suites.sh's parallel suite pool can never masquerade as a genuine
# content mismatch (#7819, #7874).
assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$SUT" ]]; then
    echo -e "${RED}FATAL${NC}: $SUT not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh issue view <N> --json labels    -> cat $STUB_DIR/labels-<N>.json
#                                          (fails if labels-fail-<N> exists)
#   gh api repos/{owner}/{repo}/issues/<N>/timeline --paginate
#                                       -> cat $STUB_DIR/timeline-<N>.json (or "[]")
#                                          (fails if timeline-fail-<N> exists)
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
case "$1" in
  issue)
    case "$2" in
      view)
        num="$3"
        if [[ -f "$STUB_DIR_FROM_ENV/labels-fail-$num" ]]; then
          echo "stub gh: issue view failed" >&2
          exit 1
        fi
        canned="$STUB_DIR_FROM_ENV/labels-$num.json"
        if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"labels":[]}'; fi
        exit 0
        ;;
    esac
    echo "stub gh: unhandled issue args: $*" >&2
    exit 3
    ;;
  api)
    path="$2"
    if [[ "$path" == repos/*/issues/*/timeline ]]; then
      num="${path#repos/*/issues/}"
      num="${num%/timeline}"
      if [[ -f "$STUB_DIR_FROM_ENV/timeline-fail-$num" ]]; then
        echo "stub gh: timeline fetch failed" >&2
        exit 1
      fi
      canned="$STUB_DIR_FROM_ENV/timeline-$num.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
      exit 0
    fi
    echo "stub gh: unhandled api args: $*" >&2
    exit 3
    ;;
  *)
    echo "stub gh: unhandled args: $*" >&2
    exit 3
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

labels_json() {
    # labels_json [label ...] -> the `{"labels":[...]}` doc for gh issue view
    local labels="" l
    for l in "$@"; do
        [[ -n "$labels" ]] && labels="$labels,"
        labels="$labels{\"name\":\"$l\"}"
    done
    printf '{"labels":[%s]}' "$labels"
}

labeled_event() {
    # labeled_event <created_at> <label-name>
    printf '{"event":"labeled","created_at":"%s","label":{"name":"%s"}}' "$1" "$2"
}

reset_state() {
    rm -f "$STUB_DIR"/labels-*.json "$STUB_DIR"/timeline-*.json
    rm -f "$STUB_DIR"/labels-fail-* "$STUB_DIR"/timeline-fail-*
}

run_sut() {
    OUT="$("$SUT" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
}

get_field() {
    printf '%s\n' "$1" | grep "^$2=" | head -n1 | cut -d= -f2-
}

# now_minus_minutes <n> -> an RFC3339 UTC timestamp n minutes in the past
# Portable GNU/BSD idiom (matches check-evaluating-staleness.sh's iso_to_epoch).
now_minus_minutes() {
    date -u -d "-$1 minutes" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
        || date -u -v"-${1}M" +"%Y-%m-%dT%H:%M:%SZ"
}

echo "Testing check-evaluating-staleness.sh..."

# (a) Label not present at all -> NOT_PRESENT, exit 10.
reset_state
labels_json "loom:curated" > "$STUB_DIR/labels-100.json"
run_sut --issue 100
assert_eq "10" "$RC" "(a) label absent -> exit 10"
assert_eq "NOT_PRESENT" "$(get_field "$OUT" DECISION)" "(a) DECISION=NOT_PRESENT"
assert_eq "loom:evaluating" "$(get_field "$OUT" LABEL)" "(a) LABEL defaults to loom:evaluating"

# (b) Label present, labeled 2 minutes ago, default 15m threshold -> FRESH, exit 0.
reset_state
labels_json "loom:curated" "loom:evaluating" > "$STUB_DIR/labels-101.json"
CLAIMED_AT="$(now_minus_minutes 2)"
printf '[%s]' "$(labeled_event "$CLAIMED_AT" "loom:evaluating")" > "$STUB_DIR/timeline-101.json"
run_sut --issue 101
assert_eq "0" "$RC" "(b) fresh claim (2m old) -> exit 0"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(b) DECISION=FRESH"
FRESH_AGE="$(get_field "$OUT" AGE_MIN)"
assert_eq "1" "$([[ "$FRESH_AGE" -ge 1 && "$FRESH_AGE" -le 3 ]] && echo 1 || echo 0)" "(b) AGE_MIN is ~2 (got $FRESH_AGE)"

# (c) Label present, labeled 30 minutes ago, default 15m threshold -> STALE, exit 12.
reset_state
labels_json "loom:architect" "loom:evaluating" > "$STUB_DIR/labels-102.json"
CLAIMED_AT="$(now_minus_minutes 30)"
printf '[%s]' "$(labeled_event "$CLAIMED_AT" "loom:evaluating")" > "$STUB_DIR/timeline-102.json"
run_sut --issue 102
assert_eq "12" "$RC" "(c) stale claim (30m old) -> exit 12"
assert_eq "STALE" "$(get_field "$OUT" DECISION)" "(c) DECISION=STALE"
STALE_AGE="$(get_field "$OUT" AGE_MIN)"
assert_eq "1" "$([[ "$STALE_AGE" -ge 29 && "$STALE_AGE" -le 31 ]] && echo 1 || echo 0)" "(c) AGE_MIN is ~30 (got $STALE_AGE)"

# (d) --threshold-minutes overrides the default: a 10m-old claim is STALE at a 5m threshold.
reset_state
labels_json "loom:evaluating" > "$STUB_DIR/labels-103.json"
CLAIMED_AT="$(now_minus_minutes 10)"
printf '[%s]' "$(labeled_event "$CLAIMED_AT" "loom:evaluating")" > "$STUB_DIR/timeline-103.json"
run_sut --issue 103 --threshold-minutes 5
assert_eq "12" "$RC" "(d) 10m old, 5m threshold -> STALE (exit 12)"
assert_eq "5" "$(get_field "$OUT" THRESHOLD_MIN)" "(d) THRESHOLD_MIN reflects the override"

# (e) LOOM_STALE_EVALUATING_MINUTES env var sets the default threshold.
reset_state
labels_json "loom:evaluating" > "$STUB_DIR/labels-104.json"
CLAIMED_AT="$(now_minus_minutes 10)"
printf '[%s]' "$(labeled_event "$CLAIMED_AT" "loom:evaluating")" > "$STUB_DIR/timeline-104.json"
LOOM_STALE_EVALUATING_MINUTES=5 run_sut --issue 104
assert_eq "12" "$RC" "(e) env-var threshold (5m) makes a 10m claim STALE"

# (f) Default label (loom:evaluating) checked, but only a DIFFERENT label
# (loom:reviewing) is present -> NOT_PRESENT (the default label is genuinely
# absent, regardless of what else is on the issue).
reset_state
labels_json "loom:reviewing" > "$STUB_DIR/labels-105.json"
run_sut --issue 105
assert_eq "10" "$RC" "(f) only loom:reviewing present, default --label checked -> exit 10"
assert_eq "NOT_PRESENT" "$(get_field "$OUT" DECISION)" "(f) DECISION=NOT_PRESENT"

# (f2) --label lets a caller check a differently-named claim label, and finds
# it present and fresh.
reset_state
labels_json "loom:reviewing" > "$STUB_DIR/labels-106.json"
CLAIMED_AT="$(now_minus_minutes 1)"
printf '[%s]' "$(labeled_event "$CLAIMED_AT" "loom:reviewing")" > "$STUB_DIR/timeline-106.json"
run_sut --issue 106 --label loom:reviewing
assert_eq "0" "$RC" "(f2) --label loom:reviewing, present and fresh -> exit 0"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(f2) DECISION=FRESH for the custom label"

# (g) No `labeled` timeline event found for the label (unusual, but must fail
# safe as FRESH rather than crash or treat as infinitely stale).
reset_state
labels_json "loom:evaluating" > "$STUB_DIR/labels-107.json"
echo "[]" > "$STUB_DIR/timeline-107.json"
run_sut --issue 107
assert_eq "0" "$RC" "(g) label present but no timeline event -> fail-safe FRESH (exit 0)"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(g) DECISION=FRESH"
assert_eq "0" "$(get_field "$OUT" AGE_MIN)" "(g) AGE_MIN=0 when unreadable"

# (h) Multiple `labeled` events for the same label (re-added after a prior
# release) -> uses the LAST (most recent) one, not the first.
reset_state
labels_json "loom:evaluating" > "$STUB_DIR/labels-108.json"
OLD_CLAIM="$(now_minus_minutes 60)"
NEW_CLAIM="$(now_minus_minutes 2)"
printf '[%s,%s]' "$(labeled_event "$OLD_CLAIM" "loom:evaluating")" "$(labeled_event "$NEW_CLAIM" "loom:evaluating")" \
  > "$STUB_DIR/timeline-108.json"
run_sut --issue 108
assert_eq "0" "$RC" "(h) most recent labeled event (2m) used, not the oldest (60m) -> FRESH"
assert_eq "FRESH" "$(get_field "$OUT" DECISION)" "(h) DECISION=FRESH"

# (i) `gh issue view` failure -> usage/environment error, exit 1.
reset_state
touch "$STUB_DIR/labels-fail-109"
run_sut --issue 109
assert_eq "1" "$RC" "(i) gh issue view failure -> exit 1"
assert_contains "$ERR" "gh issue view" "(i) stderr names the failing gh call"

# (j) `gh api .../timeline` failure -> exit 1.
reset_state
labels_json "loom:evaluating" > "$STUB_DIR/labels-110.json"
touch "$STUB_DIR/timeline-fail-110"
run_sut --issue 110
assert_eq "1" "$RC" "(j) gh api timeline failure -> exit 1"
assert_contains "$ERR" "timeline" "(j) stderr names the failing gh call"

# (k) Missing --issue -> usage error, exit 1 (no gh calls made).
reset_state
run_sut
assert_eq "1" "$RC" "(k) missing --issue -> exit 1"
assert_contains "$ERR" "--issue" "(k) stderr explains the usage error"

# (l) Non-numeric --issue -> usage error, exit 1.
reset_state
run_sut --issue abc
assert_eq "1" "$RC" "(l) non-numeric --issue -> exit 1"

# (m) A claim exactly AT the threshold is STALE (>=, not >).
reset_state
labels_json "loom:evaluating" > "$STUB_DIR/labels-111.json"
CLAIMED_AT="$(now_minus_minutes 15)"
printf '[%s]' "$(labeled_event "$CLAIMED_AT" "loom:evaluating")" > "$STUB_DIR/timeline-111.json"
run_sut --issue 111
assert_eq "12" "$RC" "(m) exactly 15m old at default 15m threshold -> STALE (>=)"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
