#!/usr/bin/env bash
# test-judge-fallback-cap.sh - Unit tests for judge-fallback-guard.sh (#5455).
#
# judge-fallback-guard.sh moves the Judge's fallback-queue skip/evaluate
# decision out of prompt-embedded bash into a real, testable script. It was
# added after PR #4972 (rjwalters/loom) accumulated 199 fallback-mode Judge
# comments over 37 hours — the existing SHA-based dedup (#5058) is correct in
# spec but expressed only as prose an LLM must faithfully re-derive on every
# pass, and a fleet propagation-lag window let ~21h of comments through even
# after #5058 had merged.
#
# This is a black-box test: judge-fallback-guard.sh is a full CLI script (no
# functions to source), so we stub `gh`/`jq` availability via a real `jq` (jq
# itself is not stubbed — its logic is exactly what's under test) and stub
# `gh` on PATH, then invoke the real script as a subprocess, asserting on
# stdout/exit code. Mirrors the stubbing pattern in test-check-duplicate.sh.
#
# Usage:
#   ./.loom/scripts/tests/test-judge-fallback-cap.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
GUARD="$SCRIPTS_DIR/judge-fallback-guard.sh"

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
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

if [[ ! -x "$GUARD" ]]; then
    echo -e "${RED}FATAL${NC}: $GUARD not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh pr view <N> --json author,headRefOid -> cat $STUB_DIR/pr-<N>.json
#                                               (fails if pr-view-fail-<N> exists)
#   gh api repos/{owner}/{repo}/issues/<N>/comments --paginate
#                                            -> cat $STUB_DIR/comments-<N>.json
#                                               (or "[]"; fails if comments-fail-<N> exists)
# Real `jq` is used unstubbed — it is exactly the logic under test.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
case "$1" in
  pr)
    if [[ "$2" == "view" ]]; then
      pr_num="$3"
      if [[ -f "$STUB_DIR_FROM_ENV/pr-view-fail-$pr_num" ]]; then
        echo "stub gh: pr view failed" >&2
        exit 1
      fi
      # Simulate `gh` emitting incidental content to stderr on a SUCCESSFUL
      # call (update-notifier banner, rate-limit hint, proxy/TLS warning) —
      # the guard must parse only stdout, never merge this into the JSON.
      if [[ -f "$STUB_DIR_FROM_ENV/pr-view-stderr-$pr_num" ]]; then
        echo "gh: A new release of gh is available: 2.0.0 -> 2.1.0" >&2
      fi
      canned="$STUB_DIR_FROM_ENV/pr-$pr_num.json"
      if [[ -f "$canned" ]]; then cat "$canned"; else echo '{"author":{"is_bot":false},"headRefOid":"0000000000000000000000000000000000000000"}'; fi
      exit 0
    fi
    echo "stub gh: unhandled pr args: $*" >&2
    exit 3
    ;;
  api)
    path="$2"
    if [[ "$path" == repos/*/issues/*/comments ]]; then
      num="${path#repos/*/issues/}"
      num="${num%/comments}"
      if [[ -f "$STUB_DIR_FROM_ENV/comments-fail-$num" ]]; then
        echo "stub gh: comments fetch failed" >&2
        exit 1
      fi
      # Same as above: benign stderr chatter on a SUCCESSFUL comments fetch.
      if [[ -f "$STUB_DIR_FROM_ENV/comments-stderr-$num" ]]; then
        echo "gh: request took longer than expected" >&2
      fi
      canned="$STUB_DIR_FROM_ENV/comments-$num.json"
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

# ISO-8601 timestamp N hours before now-ish (macOS `date -v` first, GNU
# `date -d` fallback — mirrors judge-fallback-guard.sh's own dual-path idiom,
# using the REAL `date` binary, which is intentionally not stubbed here).
hours_ago() {
    date -u -v-"$1"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "-$1 hours" +%Y-%m-%dT%H:%M:%SZ
}

marker_comment() {
    # marker_comment <created_at> <sha>
    printf '{"created_at":"%s","body":"Looks good.\\n\\n<!-- loom:fallback-evaluated sha=%s -->"}' "$1" "$2"
}

reset_state() {
    rm -f "$STUB_DIR"/pr-*.json "$STUB_DIR"/comments-*.json
    rm -f "$STUB_DIR"/pr-view-fail-* "$STUB_DIR"/comments-fail-*
    rm -f "$STUB_DIR"/pr-view-stderr-* "$STUB_DIR"/comments-stderr-*
}

run_guard() {
    OUT="$("$GUARD" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
}

get_field() {
    # get_field <output> <KEY>
    printf '%s\n' "$1" | grep "^$2=" | head -n1 | cut -d= -f2-
}

echo "Testing judge-fallback-guard.sh..."

# (a) Bot author -> SKIP, exit 10, independent of empty comment history.
reset_state
cat > "$STUB_DIR/pr-100.json" <<'EOF'
{"author":{"is_bot":true,"login":"app/dependabot"},"headRefOid":"aaaa000000000000000000000000000000000a"}
EOF
run_guard 100
assert_eq "10" "$RC" "(a) Bot author -> exit 10"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(a) DECISION=SKIP"
assert_contains "$OUT" "bot-author" "(a) REASON mentions bot-author"

# (b) No comments at all, non-bot author -> EVALUATE, exit 0.
reset_state
cat > "$STUB_DIR/pr-101.json" <<'EOF'
{"author":{"is_bot":false,"login":"someuser"},"headRefOid":"bbbb000000000000000000000000000000000b"}
EOF
run_guard 101
assert_eq "0" "$RC" "(b) No prior evaluations -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(b) DECISION=EVALUATE"
assert_eq "0" "$(get_field "$OUT" MARKER_COUNT)" "(b) MARKER_COUNT=0"
assert_eq "0" "$(get_field "$OUT" VELOCITY_ALERT)" "(b) VELOCITY_ALERT=0"

# (c) Lifetime cap reached via markers spread across DIFFERENT head SHAs
#     (simulating repeated trivial force-pushes) — the cap must fire even
#     though the most recent marker's SHA does NOT match the current head
#     SHA (i.e. this is NOT the SHA-dedup path; it is specifically the
#     per-PR-lifetime cap, the edge case the Curator flagged: a per-SHA-only
#     cap would not bound this PR because every force-push resets it).
reset_state
cat > "$STUB_DIR/pr-102.json" <<EOF
{"author":{"is_bot":false},"headRefOid":"cccc000000000000000000000000000000000c"}
EOF
{
  echo "["
  marker_comment "$(hours_ago 40)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 30)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 20)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-102.json"
run_guard 102 --cap 3
assert_eq "11" "$RC" "(c) Lifetime cap reached across changing SHAs -> exit 11"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(c) DECISION=SKIP"
assert_contains "$OUT" "lifetime fallback-evaluation cap reached" "(c) REASON names the lifetime cap"
assert_eq "3" "$(get_field "$OUT" MARKER_COUNT)" "(c) MARKER_COUNT=3"

# (c2) Same marker history, but a HIGHER cap that hasn't been reached yet ->
#      falls through to SHA dedup / evaluate instead of the cap.
reset_state
cat > "$STUB_DIR/pr-103.json" <<EOF
{"author":{"is_bot":false},"headRefOid":"dddd000000000000000000000000000000000d"}
EOF
{
  echo "["
  marker_comment "$(hours_ago 10)" "4444444444444444444444444444444444444d"
  echo "]"
} > "$STUB_DIR/comments-103.json"
run_guard 103 --cap 20
assert_eq "0" "$RC" "(c2) One prior marker, cap not reached, SHA changed -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(c2) DECISION=EVALUATE"
assert_eq "1" "$(get_field "$OUT" MARKER_COUNT)" "(c2) MARKER_COUNT=1"

# (d) SHA dedup (#5058): most recent marker's SHA matches the CURRENT head
#     SHA, cap not reached -> SKIP via dedup, exit 12.
reset_state
HEAD_SHA_D="eeee000000000000000000000000000000000e"
cat > "$STUB_DIR/pr-104.json" <<EOF
{"author":{"is_bot":false},"headRefOid":"$HEAD_SHA_D"}
EOF
{
  echo "["
  marker_comment "$(hours_ago 5)" "$HEAD_SHA_D"
  echo "]"
} > "$STUB_DIR/comments-104.json"
run_guard 104 --cap 20
assert_eq "12" "$RC" "(d) Marker SHA matches current head SHA -> exit 12"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(d) DECISION=SKIP"
assert_contains "$OUT" "already evaluated in fallback mode at current head SHA" "(d) REASON names SHA dedup"

# (e) Velocity alert: several markers within the trailing window, cap not
#     reached, SHA changed (so decision is still EVALUATE) -> VELOCITY_ALERT=1
#     fires independently of the cap/dedup decision.
reset_state
cat > "$STUB_DIR/pr-105.json" <<EOF
{"author":{"is_bot":false},"headRefOid":"ffff000000000000000000000000000000000f"}
EOF
{
  echo "["
  marker_comment "$(hours_ago 3)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 2)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 1)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-105.json"
run_guard 105 --cap 20 --velocity-threshold 3 --velocity-window-hours 4
assert_eq "0" "$RC" "(e) Velocity alert does not itself block evaluation -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(e) DECISION=EVALUATE despite velocity alert"
assert_eq "1" "$(get_field "$OUT" VELOCITY_ALERT)" "(e) VELOCITY_ALERT=1"
assert_eq "3" "$(get_field "$OUT" VELOCITY_COUNT)" "(e) VELOCITY_COUNT=3"

# (e2) Same markers, but a narrower window that excludes the oldest one, and
#      a threshold the remaining 2 don't meet -> VELOCITY_ALERT=0.
run_guard 105 --cap 20 --velocity-threshold 3 --velocity-window-hours 1
assert_eq "0" "$(get_field "$OUT" VELOCITY_ALERT)" "(e2) Narrower window drops below threshold -> VELOCITY_ALERT=0"

# (f) A Dependabot-style PR that reaches the cap must fall out of the
#     fallback queue via the SAME cap path as any other PR (structural
#     exclusion is via is_bot, independent of and prior to the cap) — confirm
#     it does not error or loop, i.e. still a clean SKIP with a stable exit
#     code, not e.g. exit 1.
reset_state
cat > "$STUB_DIR/pr-106.json" <<'EOF'
{"author":{"is_bot":true,"login":"app/dependabot"},"headRefOid":"1010101010101010101010101010101010101a"}
EOF
{
  echo "["
  marker_comment "$(hours_ago 1)" "1010101010101010101010101010101010101a"
  echo "]"
} > "$STUB_DIR/comments-106.json"
run_guard 106 --cap 1
assert_eq "10" "$RC" "(f) Dependabot PR skipped via bot-author path (checked before cap) -> exit 10"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(f) DECISION=SKIP for Dependabot PR"

# (g) Bad args: non-numeric PR number -> usage error, exit 1.
reset_state
run_guard not-a-number
assert_eq "1" "$RC" "(g) Non-numeric PR number -> exit 1"
assert_contains "$ERR" "numeric PR number is required" "(g) stderr explains the usage error"

# (h) gh pr view failure -> exit 1 (environment error, not silently EVALUATE
#     or SKIP — the caller must treat this like any other gh failure).
reset_state
touch "$STUB_DIR/pr-view-fail-107"
run_guard 107
assert_eq "1" "$RC" "(h) gh pr view failure -> exit 1"
assert_contains "$ERR" "gh pr view" "(h) stderr names the failing gh call"

# (i) `gh pr view` writes a benign line to STDERR alongside a valid JSON
#     response on STDOUT (exit 0) — e.g. an update-notifier banner. The guard
#     must parse only stdout: the bot-check must resolve correctly and the
#     script must proceed past step 1, NOT spuriously exit 1 on corrupted JSON.
#     Regression guard for the #5455 Judge finding (merged 2>&1 broke parsing).
reset_state
cat > "$STUB_DIR/pr-108.json" <<'EOF'
{"author":{"is_bot":false,"login":"someuser"},"headRefOid":"8080808080808080808080808080808080808a"}
EOF
touch "$STUB_DIR/pr-view-stderr-108"
run_guard 108
assert_eq "0" "$RC" "(i) benign stderr on gh pr view success -> still exit 0 (not spurious exit 1)"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(i) DECISION=EVALUATE despite pr-view stderr chatter"
assert_eq "8080808080808080808080808080808080808a" "$(get_field "$OUT" HEAD_SHA)" "(i) HEAD_SHA parsed cleanly from stdout only"

# (i2) Same stderr-chatter scenario, but the author IS a bot — the bot-check
#      must still fire (SKIP exit 10), proving stdout parsed cleanly for the
#      is_bot field too, not just headRefOid.
reset_state
cat > "$STUB_DIR/pr-109.json" <<'EOF'
{"author":{"is_bot":true,"login":"app/dependabot"},"headRefOid":"9090909090909090909090909090909090909a"}
EOF
touch "$STUB_DIR/pr-view-stderr-109"
run_guard 109
assert_eq "10" "$RC" "(i2) benign stderr + bot author -> still exit 10 (bot-check unaffected)"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(i2) DECISION=SKIP for bot author despite stderr chatter"

# (j) `gh api .../comments` writes a benign line to STDERR alongside a valid
#     JSON array on STDOUT (exit 0). The guard must count markers from stdout
#     only — merging stderr in silently zeroed MARKER_COUNT and defeated the
#     lifetime cap (the exact #4972 199-comment livelock this PR prevents).
#     Here 3 real markers with --cap 3 MUST still fire the cap (exit 11),
#     not silently return MARKER_COUNT=0 / EVALUATE.
reset_state
cat > "$STUB_DIR/pr-110.json" <<'EOF'
{"author":{"is_bot":false},"headRefOid":"a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a"}
EOF
{
  echo "["
  marker_comment "$(hours_ago 40)" "1111111111111111111111111111111111111a"
  echo ","
  marker_comment "$(hours_ago 30)" "2222222222222222222222222222222222222b"
  echo ","
  marker_comment "$(hours_ago 20)" "3333333333333333333333333333333333333c"
  echo "]"
} > "$STUB_DIR/comments-110.json"
touch "$STUB_DIR/comments-stderr-110"
run_guard 110 --cap 3
assert_eq "11" "$RC" "(j) benign stderr on gh api comments success -> cap still fires (exit 11)"
assert_eq "SKIP" "$(get_field "$OUT" DECISION)" "(j) DECISION=SKIP despite comments stderr chatter"
assert_eq "3" "$(get_field "$OUT" MARKER_COUNT)" "(j) MARKER_COUNT=3 parsed from stdout only (not silently 0)"

# (j2) Both call sites emit stderr chatter simultaneously, markers present but
#      cap not yet reached -> still counts correctly and proceeds to EVALUATE
#      with the true MARKER_COUNT, proving neither stream corrupts the other.
reset_state
cat > "$STUB_DIR/pr-111.json" <<'EOF'
{"author":{"is_bot":false},"headRefOid":"b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b"}
EOF
{
  echo "["
  marker_comment "$(hours_ago 10)" "4444444444444444444444444444444444444d"
  echo "]"
} > "$STUB_DIR/comments-111.json"
touch "$STUB_DIR/pr-view-stderr-111"
touch "$STUB_DIR/comments-stderr-111"
run_guard 111 --cap 20
assert_eq "0" "$RC" "(j2) stderr on BOTH calls, cap not reached -> exit 0"
assert_eq "EVALUATE" "$(get_field "$OUT" DECISION)" "(j2) DECISION=EVALUATE with stderr on both calls"
assert_eq "1" "$(get_field "$OUT" MARKER_COUNT)" "(j2) MARKER_COUNT=1 counted correctly despite dual stderr"

# --- Summary -------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
