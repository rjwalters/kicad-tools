#!/usr/bin/env bash
# test-sweep-lease-convergence.sh - Regression suite: does a 3+ host
# simultaneous-claim race on ONE issue converge to a single live claimant in
# a bounded, single-digit number of publish attempts (Issue #5331)?
#
# ## Background
#
# Issue #5331 documents a 36-hour lease-contention thrash on issue #5240
# across 4+ fleet hosts: labels flipped between `loom:issue`/`loom:building`
# every 30-90s, and 137/160 of the issue's comments were
# `<!-- loom:lease ... -->` / `<!-- loom:lease-yield ... -->` markers from
# competing hosts that never converged. The tie-break DECISION itself
# (`SweepRegistry::resolve_lease_order`, Issue #6287) is Rust code in
# `loom-daemon` (a separate repository, NOT vendored into this repo) and is
# out of scope for a change here -- but this repo DOES vendor the bash-side
# half of the same contract that every dispatch path (daemon AND in-session
# `/loom:sweep`) reads and writes: `sweep-lease-publish.sh` (the in-session
# claim writer, #6320), `sweep-lease-fence.sh` (the pre-push reader, #6309),
# and `sweep-lease-renew.sh` (the liveness renewer, #6180).
#
# Root-causing this repo's OWN contribution to that non-convergence found a
# concrete, fixable asymmetry: `sweep-lease-fence.sh` already excludes a
# lease comment from its "freshest" candidate pool once that lease's own
# (host, sweep) has posted a `loom:lease-yield` standdown record (Issue
# #6485) -- but `sweep-lease-publish.sh`'s peer-scan had NO equivalent
# exclusion. A lease that is still nominally "fresh" (within TTL) but whose
# owner has ALREADY stood down after losing a tie-break kept blocking every
# later publisher's own claim attempt (exit 4, SKIP) in deference to a claim
# the tie-break machinery had already resolved away. That phantom block is
# exactly the shape of non-convergence this suite reproduces and asserts
# against: a new host arriving after a tie-break has already produced
# yield records must be able to claim the issue in ONE attempt, not be
# fenced out indefinitely by dead, stood-down peer records.
#
# ## Harness shape
#
# A single shared, mutable stub-`gh` forge state (`$STUB_DIR/comments.json`,
# `$STUB_DIR/post-count`) persists ACROSS every `publish` invocation in this
# file, modeling successive polling rounds of a real multi-host race against
# one shared issue -- unlike test-sweep-lease-publish.sh's per-case
# `reset_state`. `claim_attempt <issue> <host> <sweep-id> <label>` runs the
# REAL `sweep-lease-publish.sh publish` subprocess and records whether it
# claimed (exit 0, `post-count` advanced) or stood down (exit 4/other).
#
# Covers:
#   Scenario A -- literal 3-host simultaneous collision: hosts A, B, C each
#     publish against an independently-empty view of the issue (true
#     near-simultaneous blindness -- none has seen another's write yet),
#     modeling the moment #6287's dispatch-time tie-break has not yet run.
#     All three succeed independently (three lease comments land on the
#     forge). The tie-break's OWN resolution (Rust-side, out of scope here)
#     is then modeled by hand -- exactly as it would appear on the forge a
#     moment later -- to leave B and C's records `loom:lease-yield`-marked
#     in favor of A. A late-arriving 4th host (D) must then correctly stand
#     down against A's still-live claim: convergence to ONE claimant within
#     a single publish attempt each, not repeated re-claims.
#   Scenario B (the #5331 regression) -- the tie-break's winner (A) later
#     goes silent (its own claim ages out past TTL -- renewal loop died,
#     matching Issue #6783's "abandoned lease" shape) while the LOSERS'
#     (B, C) lease comments remain nominally "fresh" (a stray/misdirected
#     renewal loop kept touching them after they yielded -- the exact
#     #6470/#6485 incident shape). A brand-new 5th host (E) must still be
#     able to claim the now-genuinely-unclaimed issue in a SINGLE publish
#     attempt, rather than being phantom-blocked by B's or C's dead,
#     already-yielded records. This is the scenario that would loop
#     indefinitely without this issue's fix.
#   Scenario C -- stability after Scenario B's claim: once E holds a fresh,
#     non-yielded lease, a 6th host (F) arriving in the very next round must
#     correctly stand down (exit 4) against E's legitimate claim -- proving
#     the system settles into a stable single-claimant state instead of
#     continuing to accept competing publishers forever.
#
# Total observable claim attempts across the whole simulated race (3
# simultaneous + 1 late arrival + 1 post-abandonment claimant + 1 stability
# check = 6 `publish` calls) settle into exactly ONE live, non-yielded lease
# -- single-digit attempts, not the 100+ observed in the #5331 incident.
#
# Usage:
#   ./.loom/scripts/tests/test-sweep-lease-convergence.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SCRIPT="$SCRIPTS_DIR/sweep-lease-publish.sh"

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

if [[ ! -x "$SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $SCRIPT not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH (same contract as test-sweep-lease-publish.sh, but
# post-count/comments.json are DELIBERATELY never reset between calls in
# this suite -- that persistence across "rounds" is the point). ------------
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "api" ]]; then
  shift
  method="GET"
  path=""
  jq_filter=""
  typed_body=""; have_typed_body=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method) method="$2"; shift 2 ;;
      -R) shift 2 ;;
      --paginate) shift ;;
      --jq) jq_filter="$2"; shift 2 ;;
      -f) shift 2 ;;
      -F)
        if [[ "${2:-}" == body=* ]]; then typed_body="${2#body=}"; have_typed_body=1; fi
        shift 2
        ;;
      *)
        if [[ -z "$path" ]]; then path="$1"; fi
        shift
        ;;
    esac
  done
  resolve_body() {
    if [[ -n "$have_typed_body" ]]; then
      case "$typed_body" in
        "@-") cat ;;
        @*) cat "${typed_body#@}" ;;
        *) printf '%s' "$typed_body" ;;
      esac
    fi
  }
  if [[ "$method" == "GET" && "$path" == repos/*/issues/*/comments ]]; then
    canned="$STUB_ISSUE_COMMENTS_FILE"
    [[ -f "$canned" ]] || echo "[]" > "$canned"
    if [[ -n "$jq_filter" ]]; then
      jq -c "$jq_filter" "$canned"
    else
      cat "$canned"
    fi
    exit 0
  fi
  if [[ "$method" == "POST" && "$path" == repos/*/issues/*/comments ]]; then
    n=$(( $(cat "$D/post-count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$D/post-count"
    body="$(resolve_body)"
    printf '%s' "$body" > "$D/last-post.body"
    # Land the new comment on the SHARED forge state immediately, with a
    # monotonically increasing id/updated_at so later reads see it -- this
    # is what makes "round N+1" see round N's writes.
    id=$((2000 + n))
    ts="$(date -u -d "@$(( $(date -u +%s) ))" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%SZ")"
    canned="$STUB_ISSUE_COMMENTS_FILE"
    [[ -f "$canned" ]] || echo "[]" > "$canned"
    jq --arg id "$id" --arg ts "$ts" --arg body "$body" \
        '. + [{id: ($id | tonumber), updated_at: $ts, body: $body}]' \
        "$canned" > "$canned.tmp" && mv "$canned.tmp" "$canned"
    echo "{\"id\": $id}"
    exit 0
  fi
  echo "stub gh: unhandled api args: method=$method path=$path" >&2
  exit 3
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export STUB_ISSUE_COMMENTS_FILE="$STUB_DIR/comments.json"
echo "[]" > "$STUB_ISSUE_COMMENTS_FILE"

# claim_attempt <issue> <host> <sweep-id> <label>
#
# Runs the REAL sweep-lease-publish.sh `publish` subprocess for <host>
# against WHATEVER is currently on the shared forge state, with a
# STANDALONE view of the forge for hosts that must be blind to each other
# (see `blind_claim_attempt` below) or the live shared view otherwise. Sets
# ATTEMPT_RC/ATTEMPT_ERR for the caller to assert on (stdout is discarded --
# no assertion in this suite depends on it, unlike test-sweep-lease-
# publish.sh's unit-level coverage of the printed "<host> <sweep-id>" line).
claim_attempt() {
    local issue="$1" host="$2" sweep_id="$3" label="$4"
    "$SCRIPT" publish "$issue" --host "$host" --sweep-id "$sweep_id" \
        > /dev/null 2> "$STUB_DIR/stderr-$label.log"
    ATTEMPT_RC=$?
    ATTEMPT_ERR="$(cat "$STUB_DIR/stderr-$label.log" 2>/dev/null || true)"
}

# blind_claim_attempt <issue> <host> <sweep-id> <label>
#
# Models a TRUE near-simultaneous collision: this host's `publish` call
# reads an INDEPENDENT, currently-empty forge view (nobody's write has
# landed yet from this host's perspective) and its own write is captured
# SEPARATELY rather than folded into the shared state immediately -- three
# such calls in a row simulate three hosts racing blind to each other, each
# convinced it is the sole claimant. Returns the posted body via
# BLIND_POSTED_BODY for the caller to merge into the shared timeline once
# all blind attempts are done.
blind_claim_attempt() {
    local issue="$1" host="$2" sweep_id="$3" label="$4"
    local blind_comments="$STUB_DIR/blind-comments.json"
    echo "[]" > "$blind_comments"
    STUB_ISSUE_COMMENTS_FILE="$blind_comments" "$SCRIPT" publish "$issue" --host "$host" --sweep-id "$sweep_id" \
        > /dev/null 2> "$STUB_DIR/stderr-$label.log"
    ATTEMPT_RC=$?
    ATTEMPT_ERR="$(cat "$STUB_DIR/stderr-$label.log" 2>/dev/null || true)"
    BLIND_POSTED_BODY="$(cat "$STUB_DIR/last-post.body" 2>/dev/null || echo "")"
}

# merge_comment <id> <ts> <body-file-or-literal>
#
# Appends one comment directly onto the shared forge state -- used both to
# land a blind attempt's write a moment later (once it becomes visible to
# everyone else) and to inject the tie-break's OWN resolution (the
# `loom:lease-yield` records Issue #6287's Rust-side dispatch logic would
# post -- out of scope to actually execute here, so its OUTPUT is modeled
# directly, exactly as it would appear on the forge).
merge_comment() {
    local id="$1" ts="$2" body="$3"
    jq --arg id "$id" --arg ts "$ts" --arg body "$body" \
        '. + [{id: ($id | tonumber), updated_at: $ts, body: $body}]' \
        "$STUB_ISSUE_COMMENTS_FILE" > "$STUB_ISSUE_COMMENTS_FILE.tmp" \
        && mv "$STUB_ISSUE_COMMENTS_FILE.tmp" "$STUB_ISSUE_COMMENTS_FILE"
}

post_count() {
    cat "$STUB_DIR/post-count" 2>/dev/null || echo 0
}

NOW_EPOCH="$(date -u +%s)"
iso_at() {
    local delta_secs="$1"
    date -u -d "@$((NOW_EPOCH + delta_secs))" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null \
        || date -u -r "$((NOW_EPOCH + delta_secs))" +"%Y-%m-%dT%H:%M:%SZ"
}

echo "Testing multi-host lease-claim convergence (Issue #5331)..."

# ============================================================================
# Scenario A: literal 3-host simultaneous collision, then convergence
# ============================================================================
echo ""
echo "--- Scenario A: 3 hosts race blind, tie-break resolves, a 4th host stands down ---"

ISSUE=8001

blind_claim_attempt "$ISSUE" host-A sweep-A blind-A
assert_eq "0" "$ATTEMPT_RC" "(A) host-A's blind claim attempt succeeds (sees no peer yet)"
BODY_A="$BLIND_POSTED_BODY"

blind_claim_attempt "$ISSUE" host-B sweep-B blind-B
assert_eq "0" "$ATTEMPT_RC" "(A) host-B's blind claim attempt ALSO succeeds (still blind to host-A's write) -- the genuine 3-way collision"
BODY_B="$BLIND_POSTED_BODY"

blind_claim_attempt "$ISSUE" host-C sweep-C blind-C
assert_eq "0" "$ATTEMPT_RC" "(A) host-C's blind claim attempt ALSO succeeds -- all three hosts believe they are the sole claimant"
BODY_C="$BLIND_POSTED_BODY"

# A moment later, all three writes have landed on the real forge, in
# comment-id order host-A < host-B < host-C (host-A's write is EARLIEST).
merge_comment 3001 "$(iso_at 0)" "$BODY_A"
merge_comment 3002 "$(iso_at 1)" "$BODY_B"
merge_comment 3003 "$(iso_at 2)" "$BODY_C"

# The tie-break's own resolution (Issue #6287, Rust-side, out of scope for
# this repo) is modeled directly: host-B and host-C lost to host-A's
# earlier comment id and each post their own standdown record.
merge_comment 3004 "$(iso_at 3)" \
    "<!-- loom:lease-yield host=host-B sweep=sweep-B earliest_host=host-A earliest_sweep=sweep-A -->
prose"
merge_comment 3005 "$(iso_at 4)" \
    "<!-- loom:lease-yield host=host-C sweep=sweep-C earliest_host=host-A earliest_sweep=sweep-A -->
prose"

# A 4th, late-arriving host must stand down against host-A's still-live,
# never-yielded claim -- in exactly ONE attempt.
claim_attempt "$ISSUE" host-D sweep-D late-D
assert_eq "4" "$ATTEMPT_RC" "(A) a late-arriving 4th host correctly stands down (exit 4) against the tie-break winner's live claim"
assert_contains "$ATTEMPT_ERR" "host-A" "(A) stderr names host-A as the live peer"

# ============================================================================
# Scenario B (the #5331 regression): the winner goes silent, but the
# YIELDED losers' records remain nominally fresh -- a brand-new host must
# still converge in ONE attempt, not be phantom-blocked forever.
# ============================================================================
echo ""
echo "--- Scenario B (#5331 fix under test): winner's claim ages out; yielded-but-fresh losers must not phantom-block a new claimant ---"

ISSUE=8002
echo "[]" > "$STUB_ISSUE_COMMENTS_FILE"

# host-A's original claim (the Scenario-A shape, replayed on a fresh issue)
# is now STALE -- past the default 15-minute TTL, modeling a dead renewal
# loop (Issue #6783's own "abandoned lease" shape).
merge_comment 4001 "$(iso_at -1200)" \
    "<!-- loom:lease host=host-A sweep=sweep-A -->
prose"

# host-B and host-C both lost the tie-break to host-A and yielded -- but
# their OWN lease comments were touched again AFTER they yielded (the
# #6470/#6485 incident shape: a stray or misdirected renewal loop kept
# PATCHing a stood-down claim), so they still read as "fresh" by
# `updated_at` alone.
merge_comment 4002 "$(iso_at -60)" \
    "<!-- loom:lease host=host-B sweep=sweep-B -->
prose"
merge_comment 4003 "$(iso_at -30)" \
    "<!-- loom:lease host=host-C sweep=sweep-C -->
prose"
merge_comment 4004 "$(iso_at -900)" \
    "<!-- loom:lease-yield host=host-B sweep=sweep-B earliest_host=host-A earliest_sweep=sweep-A -->
prose"
merge_comment 4005 "$(iso_at -900)" \
    "<!-- loom:lease-yield host=host-C sweep=sweep-C earliest_host=host-A earliest_sweep=sweep-A -->
prose"

BEFORE_POST_COUNT="$(post_count)"
claim_attempt "$ISSUE" host-E sweep-E new-claimant-E
assert_eq "0" "$ATTEMPT_RC" "(B) a brand-new 5th host claims the genuinely-unclaimed issue in ONE attempt -- not phantom-blocked by dead, yielded peer records"
assert_eq "$((BEFORE_POST_COUNT + 1))" "$(post_count)" "(B) exactly one new lease comment is posted for the new claimant"
assert_contains "$ATTEMPT_ERR" "published lease record" "(B) stderr confirms a genuine publish, not a silent fail-open no-op"

# ============================================================================
# Scenario C: stability -- once host-E holds a live, non-yielded lease, a
# 6th host must stand down in the very next round (no further churn).
# ============================================================================
echo ""
echo "--- Scenario C: the system settles -- a 6th host stands down against the new stable claimant ---"

claim_attempt "$ISSUE" host-F sweep-F stability-F
assert_eq "4" "$ATTEMPT_RC" "(C) a 6th host correctly stands down against host-E's now-live claim -- convergence holds, no further churn"
assert_contains "$ATTEMPT_ERR" "host-E" "(C) stderr names host-E as the live peer blocking host-F"

echo ""
echo "Total simulated claim attempts across the whole 6-host race: $(post_count) successful publishes (of 6 total publish() calls) -- single-digit, not the 100+ observed in the #5331 incident."

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if ((TESTS_FAILED > 0)); then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}ALL PASSED${NC}"
exit 0
