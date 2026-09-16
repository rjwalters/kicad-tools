#!/usr/bin/env bash
# test-sweep-lease-renew.sh - Unit tests for sweep-lease-renew.sh (#6180).
#
# Black-box tests: sweep-lease-renew.sh is a full CLI script (no functions to
# source), so `gh` is stubbed on PATH (real `jq` is used unstubbed — its
# logic is exactly what's under test) and the real script is invoked as a
# subprocess, asserting on stdout/stderr/exit code. Mirrors the stubbing
# pattern in test-judge-fallback-cap.sh.
#
# Covers:
#   (a) renew-once finds the newest lease-marker comment and PATCHes it,
#       preserving the first-line marker byte-for-byte
#   (b) renew-once is idempotent: a second call replaces (not accumulates)
#       the `loom:lease-renewed` trailer line
#   (c) renew-once ignores a comment whose body merely CONTAINS the marker
#       text without it being the first line (startswith, not substring)
#   (d) renew-once with no matching lease comment -> exit 2 (best-effort
#       no-op, not a hard failure)
#   (e) renew-once --host/--sweep-id requires an EXACT marker match, both
#       the miss and the hit cases
#   (f) renew-once --host without --sweep-id (and vice versa) -> usage error
#   (g) start spawns a background loop that self-terminates once its
#       watched PID dies (never renews after that point) — verifies the
#       "sweep exits -> renewal naturally stops" contract, entirely via a
#       real background process against the real script (no `gh` needed
#       for the termination half; the PATCH stub records call count too)
#   (h) stop kills a given PID
#   (i) renew-once's own-yield guard (#6485): a candidate lease whose own
#       (host, sweep) has a matching `loom:lease-yield` comment is NOT
#       PATCHed and exits 4 — both the miss (no matching yield) and hit
#       (matching yield) cases, plus a non-matching yield for a DIFFERENT
#       host/sweep that must NOT trip the guard
#   (j) start's default --host/--sweep-id auto-resolution (#6485): with
#       $LOOM_TERMINAL_ID=daemon-<sweep-id> and $LOOM_HOST_ID set, `start`
#       renews ONLY its own exact lease comment even when a PEER's lease
#       comment is the "newest" one on the issue — the exact misdirection
#       this issue reports (a live renewal loop keeping a peer's claim
#       looking fresh while its own claim's `updated_at` never advances)
#   (k) start's loop stops renewing (self-terminates) as soon as a
#       renew-once cycle reports the own-yield guard (exit 4), without
#       waiting for the watched PID to die
#   (l) renew-once's two `gh api` call sites (comments-list read, PATCH)
#       route through forge_gh_perm_safe's escalation ladder (#6541): a
#       GitHub App-installation permission-scope 403 on either call site
#       recovers via a freshly minted installation token and the renewal
#       still succeeds; when the ladder is fully exhausted, renew-once still
#       fails closed with the existing "ERROR: PATCH of lease comment ...
#       failed" message (never silently-allow-through)
#   (m) cmd_start's renewal loop emits a visible log line (Issue #6541) when
#       a renew-once cycle genuinely FAILS, even though the loop's own I/O is
#       unconditionally sent to /dev/null for detachment -- and does NOT log
#       a "failure" line for the normal exit-2 (no lease) or exit-4
#       (#6485 own-yield guard) outcomes, which are not failures
#
# Usage:
#   ./.loom/scripts/tests/test-sweep-lease-renew.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SCRIPT="$SCRIPTS_DIR/sweep-lease-renew.sh"

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
    if printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_true() {
    local cond="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$cond" == "true" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
    fi
}

if [[ ! -x "$SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $SCRIPT not found or not executable" >&2
    exit 2
fi

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

# --- Stub gh on PATH ---------------------------------------------------
#   gh api [-R repo] repos/{owner}/{repo}/issues/<N>/comments --paginate
#       -> cat $STUB_DIR/comments.json (or "[]"; fails if comments-fail exists)
#          -- OR a 403 "not accessible by integration" if comments-403-always
#          exists, or on the FIRST attempt only if comments-403-once exists
#          (each attempt is counted in $D/comments-attempt-count)
#   gh api [-R repo] --method PATCH repos/{owner}/{repo}/issues/comments/<id> -F body=@<path>
#       -> reads the file the -F value's "@" prefix references into
#          $STUB_DIR/patch-<id>-N.body, appends "<id>" to
#          $STUB_DIR/patch-calls.log, prints "{}" (fails if patch-fail exists)
#          -- OR a 403 "not accessible by integration" if patch-403-always
#          exists, or on the FIRST attempt only if patch-403-once exists
#          (each attempt for a given <id> is counted in $D/patch-count-<id>,
#          the same counter the body-numbering below already used)
#
#   The 403 files (#6541) let a test drive forge_gh_perm_safe's escalation
#   ladder deterministically: "*-403-once" simulates a transient App-token
#   permission-scope 403 that a fresh mint recovers from; "*-403-always"
#   simulates every rung failing (a fully exhausted ladder). Every attempt
#   -- including escalated retries under a different credential -- reaches
#   this SAME stub (PATH is overridden for the whole test process), so the
#   attempt counters below see every rung, not just the first.
#
#   The stub deliberately distinguishes `-f`/`--raw-field` (real `gh api`
#   semantics: the value is a LITERAL string -- `@-`/`@path` is NOT expanded,
#   stdin is never read) from `-F`/`--field` (real `gh api` semantics: a
#   `@-`/`@path` value IS expanded, reading from stdin/file respectively).
#   This is what catches the `-f body=@-` regression (#6357): with `-f`, the
#   stub records the literal two-character string `@-` as the PATCHed body
#   instead of the piped renewed content -- exactly like the real `gh` CLI --
#   so a script that (incorrectly) uses `-f body=@-` fails test (a) below.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
if [[ "$1" == "api" ]]; then
  shift
  method="GET"
  path=""
  field_flag=""
  field_kv=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method) method="$2"; shift 2 ;;
      -R) shift 2 ;;
      --paginate) shift ;;
      -f|--raw-field) field_flag="-f"; field_kv="$2"; shift 2 ;;
      -F|--field) field_flag="-F"; field_kv="$2"; shift 2 ;;
      *)
        if [[ -z "$path" ]]; then path="$1"; fi
        shift
        ;;
    esac
  done
  if [[ "$method" == "GET" && "$path" == repos/*/issues/*/comments ]]; then
    if [[ -f "$D/comments-fail" ]]; then
      echo "stub gh: comments fetch failed" >&2
      exit 1
    fi
    n=$(( $(cat "$D/comments-attempt-count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$D/comments-attempt-count"
    if [[ -f "$D/comments-403-always" ]] || { [[ -f "$D/comments-403-once" ]] && [[ "$n" -eq 1 ]]; }; then
      echo "HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    canned="$D/comments.json"
    if [[ -f "$canned" ]]; then cat "$canned"; else echo "[]"; fi
    exit 0
  fi
  if [[ "$method" == "PATCH" && "$path" == repos/*/issues/comments/* ]]; then
    id="${path##*/}"
    n=$(( $(cat "$D/patch-count-$id" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$D/patch-count-$id"
    if [[ -f "$D/patch-fail" ]]; then
      echo "stub gh: patch failed" >&2
      exit 1
    fi
    if [[ -f "$D/patch-403-always" ]] || { [[ -f "$D/patch-403-once" ]] && [[ "$n" -eq 1 ]]; }; then
      echo "HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    val="${field_kv#*=}"
    if [[ "$field_flag" == "-F" && "$val" == "@-" ]]; then
      # -F/--field DOES expand "@-": read the real value from stdin.
      cat > "$D/patch-$id-$n.body"
    elif [[ "$field_flag" == "-F" && "$val" == @* ]]; then
      # -F/--field DOES expand "@<path>": read the real value from the file.
      cat "${val#@}" > "$D/patch-$id-$n.body" 2>/dev/null || true
    else
      # -f/--raw-field does NOT expand "@..." -- it's posted as the literal
      # string (this is the real gh CLI behavior the #6357 bug exploited).
      printf '%s' "$val" > "$D/patch-$id-$n.body"
    fi
    echo "$id" >> "$D/patch-calls.log"
    echo '{}'
    exit 0
  fi
  echo "stub gh: unhandled api args: method=$method path=$path" >&2
  exit 3
fi
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

# A `github-app-token.sh` stub speaking the real JSON envelope (mirrors
# test-app-permission-fallback.sh) -- lets tests (l1)/(l2) deterministically
# force forge_gh_perm_safe's rung-2 fresh mint to succeed, and test (l3)
# force it to report not_configured so the ladder has nothing to escalate
# to beyond rung 1 (an exhausted-ladder scenario that does not depend on
# whatever real GitHub App may or may not be configured on the host running
# this test).
cat > "$STUB_DIR/github-app-token.sh" <<'MINT'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?stub github-app-token.sh: LOOM_TEST_STUB_DIR not set}"
mode="$(cat "$D/mint-mode" 2>/dev/null || echo not-configured)"
if [[ "$mode" == "ok" ]]; then
  echo '{"status":"ok","token":"ghs_fresh_lease_renew_test","installation_id":"1","app_id":"2","expires_at":"2099-01-01T00:00:00Z"}'
else
  echo '{"status":"not_configured","message":"github app not configured"}'
fi
MINT
chmod +x "$STUB_DIR/github-app-token.sh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_GITHUB_APP_SCRIPT="$STUB_DIR/github-app-token.sh"

reset_state() {
    rm -f "$STUB_DIR"/comments.json "$STUB_DIR"/comments-fail "$STUB_DIR"/patch-fail
    rm -f "$STUB_DIR"/comments-403-once "$STUB_DIR"/comments-403-always "$STUB_DIR"/comments-attempt-count
    rm -f "$STUB_DIR"/patch-403-once "$STUB_DIR"/patch-403-always
    rm -f "$STUB_DIR"/patch-*.body "$STUB_DIR"/patch-count-* "$STUB_DIR"/patch-calls.log
    echo "not-configured" > "$STUB_DIR/mint-mode"
    # Ensure rung 3 (personal-token / personal-ambient) has nothing of ITS
    # OWN to escalate to beyond whatever the real ambient host credential
    # is -- tests that need a fully exhausted ladder set LOOM_PERSONAL_GH_TOKEN
    # or drop ambient creds explicitly; this default just keeps unrelated
    # tests from accidentally depending on an operator's real credential.
    unset LOOM_PERSONAL_GH_TOKEN 2> /dev/null || true
    # Strip ambient dispatch-time identity env vars (#6485): a Builder session
    # running THIS test suite is itself a daemon-dispatched sweep, so
    # $LOOM_TERMINAL_ID/$LOOM_HOST_ID are routinely already set in the real
    # environment. `start`'s new auto-resolution (see test (i) below) reads
    # exactly these vars, so every test that does NOT intend to exercise that
    # path must not inherit them -- otherwise "newest wins" tests silently
    # become exact-match tests against identity values the fixtures were
    # never written to match.
    unset LOOM_TERMINAL_ID LOOM_HOST_ID LOOM_LEASE_PUBLISH_HOSTNAME HOSTNAME 2> /dev/null || true
}

run_script() {
    OUT="$("$SCRIPT" "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
    ERR="$(cat "$STUB_DIR/stderr.log" 2>/dev/null || true)"
}

echo "Testing sweep-lease-renew.sh..."

# --- (a) finds newest lease-marker comment, PATCHes it, preserves marker ---
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 1, "body": "unrelated comment, no marker here"},
  {"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired for this claim."}
]
JSON
run_script renew-once 6180
assert_eq "0" "$RC" "(a) renew-once exits 0 when a lease comment exists"
assert_eq "" "$OUT" "(a) renew-once prints nothing to stdout (all diagnostics go to stderr)"
BODY_A="$(cat "$STUB_DIR/patch-42-1.body" 2>/dev/null || echo MISSING)"
assert_true "$([[ "$BODY_A" != "@-" ]] && echo true || echo false)" "(a) PATCH body is the real renewed content, not the literal string '@-' (#6357: requires -F, not -f)"
assert_contains "$BODY_A" "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->" "(a) PATCH body preserves the first-line marker byte-for-byte"
assert_contains "$BODY_A" "Lease acquired for this claim." "(a) PATCH body preserves the original free-form prose"
assert_contains "$BODY_A" "<!-- loom:lease-renewed " "(a) PATCH body appends a loom:lease-renewed trailer"
FIRST_LINE_A="$(head -n1 "$STUB_DIR/patch-42-1.body")"
assert_eq "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->" "$FIRST_LINE_A" "(a) the marker is still the LITERAL first line"

# --- (b) idempotent: second renewal REPLACES, not accumulates, the trailer -
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired."}
]
JSON
run_script renew-once 6180
sleep 1.1
# Feed the just-patched body back in as if the forge now holds it (jq --
# already a hard dependency of the script under test -- rather than adding a
# python3 dependency just for this one test-fixture mutation).
jq --rawfile body "$STUB_DIR/patch-42-1.body" \
    '(.[] | select(.id == 42) | .body) = $body' \
    "$STUB_DIR/comments.json" > "$STUB_DIR/comments.json.tmp"
mv "$STUB_DIR/comments.json.tmp" "$STUB_DIR/comments.json"
run_script renew-once 6180
assert_eq "0" "$RC" "(b) second renew-once also exits 0"
TRAILER_COUNT="$(grep -c "loom:lease-renewed" "$STUB_DIR/patch-42-2.body" 2>/dev/null || echo 0)"
assert_eq "1" "$TRAILER_COUNT" "(b) exactly ONE loom:lease-renewed trailer line after two renewals (no accumulation)"
BODY_B1="$(cat "$STUB_DIR/patch-42-1.body")"
BODY_B2="$(cat "$STUB_DIR/patch-42-2.body")"
assert_true "$([[ "$BODY_B1" != "$BODY_B2" ]] && echo true || echo false)" "(b) the two renewal PATCHes carry different content (updated_at will genuinely advance)"

# --- (c) startswith, not substring: a mid-body mention of the marker text
#     must NOT be treated as a lease comment -------------------------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 7, "body": "Discussing the format: `<!-- loom:lease host=x sweep=y -->` is the marker, but this is prose, not a lease record."}
]
JSON
run_script renew-once 6180
assert_eq "2" "$RC" "(c) a comment merely mentioning the marker (not as its first line) is NOT treated as a lease"

# --- (d) no matching lease comment -> exit 2, not a hard failure ----------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "body": "nothing to see here"}]
JSON
run_script renew-once 6180
assert_eq "2" "$RC" "(d) no lease comment -> exit 2 (best-effort no-op)"
assert_contains "$ERR" "nothing to renew" "(d) exit-2 message explains why"

# --- (e) --host/--sweep-id exact-match filter -----------------------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired."}
]
JSON
run_script renew-once 6180 --host wrong-host --sweep-id sweep-issue-6180-1000
assert_eq "2" "$RC" "(e) exact host/sweep-id filter: mismatched host -> exit 2"
run_script renew-once 6180 --host studio-host --sweep-id sweep-issue-6180-1000
assert_eq "0" "$RC" "(e) exact host/sweep-id filter: matching pair -> exit 0"

# --- (f) --host without --sweep-id (and vice versa) -> usage error --------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired."}]
JSON
run_script renew-once 6180 --host studio-host
assert_eq "1" "$RC" "(f) --host without --sweep-id is a usage error"
run_script renew-once 6180 --sweep-id sweep-issue-6180-1000
assert_eq "1" "$RC" "(f) --sweep-id without --host is a usage error"

# --- (g) start's background loop self-terminates when its watched PID dies,
#     and never renews again after that point ------------------------------
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired."}
]
JSON
sleep 6 &
WATCH_PID=$!
LOOP_PID="$("$SCRIPT" start 6180 --interval 1 --watch-pid "$WATCH_PID" 2>"$STUB_DIR/start-stderr.log")"
sleep 2.5
kill "$WATCH_PID" 2>/dev/null || true
wait "$WATCH_PID" 2>/dev/null || true
sleep 1.5
LOOP_ALIVE="false"
kill -0 "$LOOP_PID" 2>/dev/null && LOOP_ALIVE="true"
assert_true "$([[ "$LOOP_ALIVE" == "false" ]] && echo true || echo false)" "(g) renewal loop self-terminates after its watched PID dies"
COUNT_BEFORE_DEATH="$(wc -l < "$STUB_DIR/patch-calls.log" 2>/dev/null | tr -d ' ')"
COUNT_BEFORE_DEATH="${COUNT_BEFORE_DEATH:-0}"
sleep 2
COUNT_AFTER_WAIT="$(wc -l < "$STUB_DIR/patch-calls.log" 2>/dev/null | tr -d ' ')"
COUNT_AFTER_WAIT="${COUNT_AFTER_WAIT:-0}"
assert_eq "$COUNT_BEFORE_DEATH" "$COUNT_AFTER_WAIT" "(g) no further renewals happen once the watched PID is dead"
TESTS_RUN=$((TESTS_RUN + 1))
if ((COUNT_BEFORE_DEATH >= 1)); then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: (g) at least one renewal happened while the watched PID was alive"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: (g) expected at least one renewal before the watched PID died (got $COUNT_BEFORE_DEATH)"
fi
# Defensive cleanup in case the assertion above failed the self-termination.
kill "$LOOP_PID" 2>/dev/null || true

# --- (h) stop kills a given PID -------------------------------------------
sleep 30 &
BG_PID=$!
"$SCRIPT" stop "$BG_PID" > /dev/null 2>&1
sleep 0.3
STILL_ALIVE="false"
kill -0 "$BG_PID" 2>/dev/null && STILL_ALIVE="true"
assert_true "$([[ "$STILL_ALIVE" == "false" ]] && echo true || echo false)" "(h) stop kills the given PID"
kill "$BG_PID" 2>/dev/null || true

# --- (i) own-yield guard (#6485) -------------------------------------------
echo ""
echo "--- (i) own-yield guard ---"

# (i-1) exact-match candidate with NO matching yield comment -> renews normally
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=hostA sweep=sweepA -->\nprose"}
]
JSON
run_script renew-once 6485 --host hostA --sweep-id sweepA
assert_eq "0" "$RC" "(i-1) no matching yield comment -> renews normally (exit 0)"

# (i-2) a yield comment for a DIFFERENT (host, sweep) must NOT trip the guard
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=hostA sweep=sweepA -->\nprose"},
  {"id": 43, "body": "<!-- loom:lease-yield host=hostB sweep=sweepB earliest_host=hostA earliest_sweep=sweepA -->\nprose"}
]
JSON
run_script renew-once 6485 --host hostA --sweep-id sweepA
assert_eq "0" "$RC" "(i-2) a yield comment for a different (host, sweep) does not block renewal"

# (i-3) a yield comment matching the candidate's OWN (host, sweep) -> refuse
# to renew, exit 4, and issue NO PATCH at all.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=hostA sweep=sweepA -->\nprose"},
  {"id": 43, "body": "<!-- loom:lease-yield host=hostA sweep=sweepA earliest_host=hostB earliest_sweep=sweepB -->\nprose"}
]
JSON
run_script renew-once 6485 --host hostA --sweep-id sweepA
assert_eq "4" "$RC" "(i-3) a yield record matching the candidate's own (host, sweep) -> exit 4, refuse to renew"
assert_contains "$ERR" "already posted a loom:lease-yield" "(i-3) stderr explains the own-yield guard"
assert_true "$([[ ! -f "$STUB_DIR/patch-42-1.body" ]] && echo true || echo false)" "(i-3) no PATCH was issued for the yielded owner's lease"

# (i-4) the exact race shape from the issue, WITHOUT any --host/--sweep-id
# ("newest wins" mode): the NEWEST lease comment on the issue belongs to a
# host that has since yielded -- the own-yield guard must still catch it
# even though the caller supplied no exact-match filter at all.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 10, "body": "<!-- loom:lease host=host-winner sweep=sweep-winner -->\nprose"},
  {"id": 20, "body": "<!-- loom:lease host=host-loser sweep=sweep-loser -->\nprose"},
  {"id": 21, "body": "<!-- loom:lease-yield host=host-loser sweep=sweep-loser earliest_host=host-winner earliest_sweep=sweep-winner -->\nprose"}
]
JSON
run_script renew-once 6485
assert_eq "4" "$RC" "(i-4) newest-wins candidate is the yielded loser's lease -> exit 4, refuse to renew"
assert_contains "$ERR" "host=host-loser sweep=sweep-loser" "(i-4) stderr names the yielded owner, not the winner"

# --- (j) start's default --host/--sweep-id auto-resolution (#6485) --------
echo ""
echo "--- (j) start auto-resolves its own lease via LOOM_TERMINAL_ID/LOOM_HOST_ID ---"

compute_opaque_host_id() {
    local host="$1" hash
    if command -v shasum > /dev/null 2>&1; then
        hash="$(printf '%s%s' "loom-lease-host-id-v1:" "$host" | shasum -a 256 | awk '{print $1}')"
    else
        hash="$(printf '%s%s' "loom-lease-host-id-v1:" "$host" | sha256sum | awk '{print $1}')"
    fi
    printf 'host-%s' "${hash:0:8}"
}

reset_state
OWN_RAW_HOST="test-own-host"
OWN_OPAQUE_HOST="$(compute_opaque_host_id "$OWN_RAW_HOST")"
# id 10 (lower/older) is THIS session's own lease; id 99 (higher/newer) is a
# PEER's lease -- "newest wins" would pick id 99, exactly the misdirection
# #6485 reports.
cat > "$STUB_DIR/comments.json" <<JSON
[
  {"id": 10, "body": "<!-- loom:lease host=${OWN_OPAQUE_HOST} sweep=sweep-mine-1000 -->\nprose"},
  {"id": 99, "body": "<!-- loom:lease host=peer-host sweep=sweep-peer-2000 -->\nprose"}
]
JSON
sleep 4 &
WATCH_PID_J=$!
export LOOM_HOST_ID="$OWN_RAW_HOST"
export LOOM_TERMINAL_ID="daemon-sweep-mine-1000"
LOOP_PID_J="$("$SCRIPT" start 6485 --interval 1 --watch-pid "$WATCH_PID_J" 2> "$STUB_DIR/start-j-stderr.log")"
unset LOOM_HOST_ID LOOM_TERMINAL_ID
sleep 1.8
kill "$WATCH_PID_J" 2> /dev/null || true
wait "$WATCH_PID_J" 2> /dev/null || true
sleep 0.5
kill "$LOOP_PID_J" 2> /dev/null || true
assert_true "$([[ -f "$STUB_DIR/patch-10-1.body" ]] && echo true || echo false)" "(j) start renewed its OWN lease comment (id 10), not the newer peer comment"
assert_true "$([[ ! -f "$STUB_DIR/patch-99-1.body" ]] && echo true || echo false)" "(j) start did NOT renew the peer's newer lease comment (id 99)"

# --- (k) start's loop stops as soon as its own-yield guard fires ----------
echo ""
echo "--- (k) start stops renewing once its own lease target has yielded ---"

reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 55, "body": "<!-- loom:lease host=k-host sweep=k-sweep -->\nprose"},
  {"id": 56, "body": "<!-- loom:lease-yield host=k-host sweep=k-sweep earliest_host=other-host earliest_sweep=other-sweep -->\nprose"}
]
JSON
sleep 8 &
WATCH_PID_K=$!
LOOP_PID_K="$("$SCRIPT" start 6485 --interval 1 --watch-pid "$WATCH_PID_K" --host k-host --sweep-id k-sweep 2> "$STUB_DIR/start-k-stderr.log")"
sleep 1.8
LOOP_ALIVE_AFTER_YIELD="false"
kill -0 "$LOOP_PID_K" 2> /dev/null && LOOP_ALIVE_AFTER_YIELD="true"
assert_true "$([[ "$LOOP_ALIVE_AFTER_YIELD" == "false" ]] && echo true || echo false)" "(k) loop has already self-terminated shortly after its own-yield guard fires, without waiting for the watched PID to die"
kill "$WATCH_PID_K" 2> /dev/null || true
wait "$WATCH_PID_K" 2> /dev/null || true
kill "$LOOP_PID_K" 2> /dev/null || true
assert_true "$([[ ! -f "$STUB_DIR/patch-55-1.body" ]] && echo true || echo false)" "(k) the yielded lease was never PATCHed"

# --- (l) forge_gh_perm_safe escalation-ladder routing (#6541) -------------
echo ""
echo "--- (l) forge_gh_perm_safe escalation-ladder routing on both gh api call sites ---"

# (l1) comments-list read: a transient App-token 403 on the FIRST attempt
# recovers via a freshly minted installation token, and the renewal still
# succeeds end to end.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired."}
]
JSON
touch "$STUB_DIR/comments-403-once"
echo "ok" > "$STUB_DIR/mint-mode"
run_script renew-once 6180
assert_eq "0" "$RC" "(l1) a transient 403 on the comments-list read recovers via the escalation ladder"
assert_contains "$ERR" "forge:" "(l1) forge_gh_perm_safe's escalation diagnostic is visible on stderr"
assert_true "$([[ -f "$STUB_DIR/patch-42-1.body" ]] && echo true || echo false)" "(l1) the renewal still PATCHes the lease comment after the escalated read"

# (l2) PATCH call: a transient App-token 403 on the FIRST attempt recovers
# via a freshly minted installation token, and the PATCH still lands with
# the correct renewed body (proving the switch from a stdin pipe to a temp
# file survives a retried attempt -- a stdin pipe would be empty by then).
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired."}
]
JSON
touch "$STUB_DIR/patch-403-once"
echo "ok" > "$STUB_DIR/mint-mode"
run_script renew-once 6180
assert_eq "0" "$RC" "(l2) a transient 403 on the PATCH call recovers via the escalation ladder"
assert_contains "$ERR" "forge:" "(l2) forge_gh_perm_safe's escalation diagnostic is visible on stderr for the PATCH call too"
BODY_L2="$(cat "$STUB_DIR/patch-42-2.body" 2>/dev/null || echo MISSING)"
assert_contains "$BODY_L2" "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->" "(l2) the escalated retry's PATCH body still preserves the marker byte-for-byte"
assert_contains "$BODY_L2" "<!-- loom:lease-renewed " "(l2) the escalated retry's PATCH body still carries the renewed trailer"

# (l3) PATCH call: a FULLY EXHAUSTED escalation ladder (every rung 403s)
# still fails closed -- non-zero exit, the existing "ERROR: PATCH of lease
# comment ... failed" message -- never silently-allow-through.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=studio-host sweep=sweep-issue-6180-1000 -->\nLease acquired."}
]
JSON
touch "$STUB_DIR/patch-403-always"
run_script renew-once 6180
assert_eq "1" "$RC" "(l3) an exhausted escalation ladder on the PATCH call still fails closed (non-zero exit)"
assert_contains "$ERR" "ERROR: PATCH of lease comment 42 on issue #6180 failed" "(l3) the existing fail-closed error message is preserved verbatim"
assert_true "$(! ls "$STUB_DIR"/patch-42-*.body > /dev/null 2>&1 && echo true || echo false)" "(l3) no PATCH body was ever successfully written when the ladder is exhausted"

# (l4) comments-list read: a FULLY EXHAUSTED escalation ladder also fails
# closed -- renew-once never silently proceeds as if there were no comments.
reset_state
touch "$STUB_DIR/comments-403-always"
run_script renew-once 6180
assert_eq "1" "$RC" "(l4) an exhausted escalation ladder on the comments-list read still fails closed"
assert_contains "$ERR" "escalation ladder exhausted" "(l4) the failure message explains the read failed after escalation"

# --- (m) cmd_start's loop logs a visible failure line (#6541) -------------
echo ""
echo "--- (m) cmd_start's renewal loop logs a visible line on a genuine failure ---"

# (m1) a genuine renewal failure (escalation ladder exhausted on the PATCH
# call) is logged to the loop's saved stderr, even though the loop's own
# I/O is otherwise unconditionally sent to /dev/null for detachment -- the
# exact silent-403-for-a-whole-lease-lifetime failure mode this issue
# reports.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 42, "body": "<!-- loom:lease host=m-host sweep=m-sweep -->\nprose"}
]
JSON
touch "$STUB_DIR/patch-403-always"
sleep 5 &
WATCH_PID_M=$!
LOOP_PID_M="$("$SCRIPT" start 6180 --interval 1 --watch-pid "$WATCH_PID_M" --host m-host --sweep-id m-sweep 2> "$STUB_DIR/start-m-stderr.log")"
sleep 2.5
kill "$WATCH_PID_M" 2> /dev/null || true
wait "$WATCH_PID_M" 2> /dev/null || true
sleep 0.5
kill "$LOOP_PID_M" 2> /dev/null || true
M_ERR="$(cat "$STUB_DIR/start-m-stderr.log" 2> /dev/null || true)"
assert_contains "$M_ERR" "FAILED" "(m1) a genuinely failing renewal cycle logs a visible FAILED line"
assert_contains "$M_ERR" "issue #6180" "(m1) the failure log line identifies which issue's renewal failed"

# (m2) the normal exit-2 "no matching lease comment" outcome is NOT logged
# as a failure -- it is a documented, expected no-op for any sweep with no
# daemon-written lease at all (e.g. manual /loom:sweep, GH Actions cron).
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[{"id": 1, "body": "nothing to see here"}]
JSON
sleep 5 &
WATCH_PID_M2=$!
LOOP_PID_M2="$("$SCRIPT" start 6180 --interval 1 --watch-pid "$WATCH_PID_M2" 2> "$STUB_DIR/start-m2-stderr.log")"
sleep 2.5
kill "$WATCH_PID_M2" 2> /dev/null || true
wait "$WATCH_PID_M2" 2> /dev/null || true
sleep 0.5
kill "$LOOP_PID_M2" 2> /dev/null || true
M2_ERR="$(cat "$STUB_DIR/start-m2-stderr.log" 2> /dev/null || true)"
assert_true "$([[ "$M2_ERR" != *FAILED* ]] && echo true || echo false)" "(m2) exit-2 'no lease comment' does not log a FAILED line"

# (m3) the #6485 exit-4 own-yield-guard path is UNCHANGED: the loop still
# self-terminates immediately, and it does NOT log a FAILED line either --
# a controlled stand-down is not a failure.
reset_state
cat > "$STUB_DIR/comments.json" <<'JSON'
[
  {"id": 55, "body": "<!-- loom:lease host=m3-host sweep=m3-sweep -->\nprose"},
  {"id": 56, "body": "<!-- loom:lease-yield host=m3-host sweep=m3-sweep earliest_host=other-host earliest_sweep=other-sweep -->\nprose"}
]
JSON
sleep 8 &
WATCH_PID_M3=$!
LOOP_PID_M3="$("$SCRIPT" start 6485 --interval 1 --watch-pid "$WATCH_PID_M3" --host m3-host --sweep-id m3-sweep 2> "$STUB_DIR/start-m3-stderr.log")"
sleep 1.8
LOOP_ALIVE_M3="false"
kill -0 "$LOOP_PID_M3" 2> /dev/null && LOOP_ALIVE_M3="true"
assert_true "$([[ "$LOOP_ALIVE_M3" == "false" ]] && echo true || echo false)" "(m3) the #6485 own-yield-guard exit-4 path still self-terminates the loop immediately (unchanged)"
kill "$WATCH_PID_M3" 2> /dev/null || true
wait "$WATCH_PID_M3" 2> /dev/null || true
kill "$LOOP_PID_M3" 2> /dev/null || true
M3_ERR="$(cat "$STUB_DIR/start-m3-stderr.log" 2> /dev/null || true)"
assert_true "$([[ "$M3_ERR" != *FAILED* ]] && echo true || echo false)" "(m3) the own-yield-guard outcome (exit 4) does not log a FAILED line"

# --- Contract checks (mirrors test-check-quarantine-stashes.sh's style) ---
"$SCRIPT" --help > "$STUB_DIR/help.out" 2>&1
HELP_RC=$?
assert_true "$([[ -s "$STUB_DIR/help.out" ]] && echo true || echo false)" "--help prints usage text"
assert_eq "1" "$HELP_RC" "--help exits 1 (usage-exit convention, matches sweep-run-registry.sh)"
"$SCRIPT" bogus-command > /dev/null 2>&1
BOGUS_RC=$?
assert_true "$([[ "$BOGUS_RC" -ne 0 ]] && echo true || echo false)" "an unknown command exits non-zero"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if ((TESTS_FAILED > 0)); then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}ALL PASSED${NC}"
exit 0
