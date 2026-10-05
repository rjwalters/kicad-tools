#!/usr/bin/env bash
# test-sweep-lease-renew-lifecycle.sh - Lifecycle tests for sweep-lease-renew.sh
# (#10229), split from test-sweep-lease-renew.sh to keep that suite under the
# file-size ratchet.
#
# The ownership and completion DECISIONS live in `loom-daemon lease renewer`
# (unit-tested in loom-daemon/src/cli/lease_renewer/tests.rs). This suite pins
# the shell call-site contract against lib/trust-stub.sh's stand-in:
#
#   (y1) each cycle reads the issue state and hands it to `lease renewer
#        check` with this loop's key and token
#   (y2) check exit 3 (closed / released) ends the loop for good while the
#        watched parent lives; no PATCH afterwards
#   (y3) check exit 4 (state unverified) skips the PATCH, keeps the loop, and
#        renewal resumes once the gate says renew
#   (y4) claim naming a live peer: start reports the peer's pid and its own
#        loop never renews
#   (y5) release delegates to `lease renewer release` with its flags
#   (y6) per-cycle call budget: one state read + one window read + one PATCH
#   (y7) fail-open: a daemon without the verb (exit 2) still renews
#   (z1)-(z4) credential routing: reads on a reader App, the PATCH on the
#        writer, ambient fallback on any App failure, ladder kept, 404 kept
#   (z5) sliding window cursor + own-yield guard retained
#   (z6)/(z7) a loop with no lease stops after two consecutive misses; a hit
#        in between resets the count
#
# With LEASE_RENEWER_DAEMON=<built loom-daemon> the stub hands `lease renewer`
# to the real binary and (r1)-(r4) run end to end: closed issue, concurrent
# identical starts, independent keys, release, dead-owner recovery.
#
# `gh` is stubbed on PATH; no real credentials or live forge calls.

set -uo pipefail
# shellcheck source=lib/write-scope-fixture.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/write-scope-fixture.sh"

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$(cd "$TEST_DIR/.." && pwd)/sweep-lease-renew.sh"
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$1" == "$2" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $3"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $3"
        echo "    Expected: '$1'"
        echo "    Actual:   '$2'"
    fi
}

STUB_DIR="$(mktemp -d)"
trap 'jobs -p | xargs kill 2> /dev/null; rm -rf "$STUB_DIR" 2> /dev/null || true' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
D="${LOOM_TEST_STUB_DIR:?}"
[[ "$1" == "api" ]] || exit 3
shift
method="GET"; path=""; field_kv=""; jq_expr=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --method) method="$2"; shift 2 ;;
    --paginate) shift ;;
    --jq) jq_expr="$2"; shift 2 ;;
    -f|-F|--field|--raw-field) field_kv="$2"; shift 2 ;;
    *) [[ -n "$path" ]] || path="$1"; shift ;;
  esac
done
# #10229: which credential each call ran on, and an App-only failure to inject.
echo "$method ${path%%\?*} tok=${GH_TOKEN-<unset>} cred=${LOOM_LEASE_CREDENTIAL-<unset>}" >> "$D/cred.log"
if [[ "${GH_TOKEN:-}" == ghs_app* && -f "$D/app-fail-$method" ]]; then
  [[ "$(cat "$D/app-fail-$method")" == 403 ]] && echo "HTTP 403: Resource not accessible by integration" >&2 || echo "gh: Not Found (HTTP 404)" >&2
  exit 1
fi
if [[ "$method" == "GET" && "$path" == repos/*/issues/*/comments* ]]; then
  echo "$path" >> "$D/list-calls.log"; cat "$D/comments.json"; exit 0
fi
if [[ "$method" == "GET" && "$path" == repos/*/issues/[0-9]* ]]; then
  echo "$path" >> "$D/state-calls.log"
  [[ ! -f "$D/issue-state-fail" ]] || { echo "stub gh: state read failed" >&2; exit 1; }
  state="$(cat "$D/issue-state" 2> /dev/null || echo open)"
  if [[ "$jq_expr" == ".state" ]]; then echo "$state"; else echo "{\"state\":\"$state\"}"; fi
  exit 0
fi
if [[ "$method" == "PATCH" && "$path" == repos/*/issues/comments/* ]]; then
  [[ ! -f "$D/patch-404-once" ]] || { rm -f "$D/patch-404-once"; echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
  echo "${path##*/}" >> "$D/patch-calls.log"; echo '{}'; exit 0
fi
echo "stub gh: unhandled: $method $path" >&2; exit 3
STUB
chmod +x "$STUB_DIR/gh"
cat > "$STUB_DIR/github-app-token.sh" <<'MINT'
#!/usr/bin/env bash
echo '{"status":"not_configured","message":"github app not configured"}'
MINT
chmod +x "$STUB_DIR/github-app-token.sh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export LOOM_LEASE_RENEW_STATE_DIR="$STUB_DIR/renew-state"
export PATH="$STUB_DIR:$PATH"
# shellcheck source=lib/trust-stub.sh
source "$TEST_DIR/lib/trust-stub.sh"
loom_trust_stub "$STUB_DIR"
write_scope_register "$STUB_DIR/checkout" acme/widget
cd "$STUB_DIR/checkout" || exit 1
export LOOM_GITHUB_APP_SCRIPT="$STUB_DIR/github-app-token.sh"
unset GH_TOKEN GITHUB_TOKEN LOOM_PERSONAL_GH_TOKEN LOOM_TERMINAL_ID LOOM_HOST_ID LOOM_LEASE_PUBLISH_HOSTNAME HOSTNAME LOOM_SWEEP_ID LOOM_ROLE LOOM_REPO 2> /dev/null || true

reset_state() {
    rm -f "$STUB_DIR"/comments.json "$STUB_DIR"/issue-state* "$STUB_DIR"/state-calls.log \
        "$STUB_DIR"/list-calls.log "$STUB_DIR"/patch-calls.log "$STUB_DIR"/renewer-* "$STUB_DIR"/r2-out.* \
        "$STUB_DIR"/cred.log "$STUB_DIR"/app-* "$STUB_DIR"/forge-token-args.log "$STUB_DIR"/z-*.log
    rm -rf "$LOOM_LEASE_RENEW_STATE_DIR" 2> /dev/null || true
    echo "[$Y_LEASE]" > "$STUB_DIR/comments.json"
}
patch_n() { cat "$STUB_DIR/patch-calls.log" 2> /dev/null | wc -l | tr -d ' '; }
wait_patches() {
    local waited=0
    while ((waited < 20)) && [[ "$(patch_n)" -lt "$1" ]]; do
        sleep 0.5
        waited=$((waited + 1))
    done
}
start_loop() { "$SCRIPT" start "$1" --interval 1 --watch-pid "$2" --host y-host --sweep-id "${3:-y-sweep}" 2> /dev/null; }
alive() { kill -0 "$1" 2> /dev/null; }
yb() { if "$@"; then echo true; else echo false; fi; }
# The stub gate answers from renewer-check-rc; the real one from the state.
gate() { echo "$1" > "$STUB_DIR/renewer-check-rc"; }

Y_LEASE='{"id": 42, "created_at": "2026-10-01T00:00:00Z", "body": "<!-- loom:lease host=y-host sweep=y-sweep -->\nprose"}'

echo "Testing sweep-lease-renew.sh lifecycle (#10229)${LEASE_RENEWER_DAEMON:+ against $LEASE_RENEWER_DAEMON}..."

if [[ -z "${LEASE_RENEWER_DAEMON:-}" ]]; then
    # (y1) + (y2): the state is forwarded; exit 3 ends the loop, parent lives.
    reset_state
    sleep 30 &
    WATCH=$!
    LOOP="$(start_loop 10229 "$WATCH")"
    wait_patches 1
    CHECK_LINE="$(grep -m1 'lease renewer check' "$STUB_DIR/renewer-args.log" 2> /dev/null)"
    assert_eq "true" "$([[ "$CHECK_LINE" == "lease renewer check 10229 --host y-host --sweep-id y-sweep --token "*" --issue-state open" ]] && echo true || echo false)" "(y1) check gets the key, a token and the state read (got: $CHECK_LINE)"
    CLAIM_LINE="$(grep -m1 'lease renewer claim' "$STUB_DIR/renewer-args.log" 2> /dev/null)"
    TOKEN="${CHECK_LINE#*--token }"
    TOKEN="${TOKEN%% *}"
    assert_eq "lease renewer claim 10229 --host y-host --sweep-id y-sweep --token $TOKEN --pid $LOOP" "$CLAIM_LINE" "(y1) claim records the loop pid under the same key and token"
    gate 3
    sleep 2.5
    assert_eq "false" "$(yb alive "$LOOP")" "(y2) check exit 3 ended the loop, parent still alive"
    N="$(patch_n)"
    sleep 1.5
    assert_eq "$N" "$(patch_n)" "(y2) no PATCH after the stop"
    assert_eq "true" "$(yb alive "$WATCH")" "(y2) the interactive parent was untouched"
    kill "$WATCH" 2> /dev/null

    # (y3) exit 4: skip the PATCH, keep the loop, resume afterwards.
    reset_state
    gate 4
    sleep 30 &
    WATCH=$!
    LOOP="$(start_loop 10229 "$WATCH")"
    sleep 3.5
    assert_eq "0" "$(patch_n)" "(y3) no PATCH while the gate says unverified"
    assert_eq "true" "$(yb alive "$LOOP")" "(y3) the loop survives an unverified cycle"
    gate 0
    wait_patches 1
    assert_eq "true" "$([[ "$(patch_n)" -ge 1 ]] && echo true || echo false)" "(y3) renewal resumes once the gate says renew"
    kill "$LOOP" "$WATCH" 2> /dev/null

    # (y4) a live peer owns the key: its pid is reported, our loop never renews.
    reset_state
    sleep 30 &
    PEER=$!
    echo "$PEER" > "$STUB_DIR/renewer-claim-pid"
    sleep 30 &
    WATCH=$!
    OUT="$(start_loop 10229 "$WATCH")"
    assert_eq "$PEER" "$OUT" "(y4) start reports the live owner's pid"
    sleep 2.5
    assert_eq "0" "$(patch_n)" "(y4) the duplicate loop was dropped before renewing"
    kill "$PEER" "$WATCH" 2> /dev/null

    # (y5) release delegates with its flags.
    reset_state
    "$SCRIPT" release 10229 --host y-host --sweep-id y-sweep 2> /dev/null
    assert_eq "lease renewer release 10229 --host y-host --sweep-id y-sweep" "$(cat "$STUB_DIR/renewer-args.log" 2> /dev/null)" "(y5) release is the daemon verb"

    # (y7) a daemon predating the verb (clap exit 2) renews as before.
    reset_state
    gate 2
    sleep 30 &
    WATCH=$!
    LOOP="$(start_loop 10229 "$WATCH")"
    wait_patches 2
    assert_eq "true" "$([[ "$(patch_n)" -ge 2 ]] && echo true || echo false)" "(y7) an unknown-verb exit still renews (fail-open)"
    kill "$LOOP" "$WATCH" 2> /dev/null
else
    # (r1) open -> closed with a LIVE parent: the real gate stops it for good.
    reset_state
    sleep 30 &
    WATCH=$!
    LOOP="$(start_loop 10229 "$WATCH")"
    wait_patches 1
    echo closed > "$STUB_DIR/issue-state"
    sleep 3
    assert_eq "false" "$(yb alive "$LOOP")" "(r1) the loop exited after the issue closed, parent still alive"
    N="$(patch_n)"
    sleep 2
    assert_eq "$N" "$(patch_n)" "(r1) no PATCH after the close"
    rm -f "$STUB_DIR/issue-state"
    touch "$STUB_DIR/issue-state-fail"
    LOOP="$(start_loop 10230 "$WATCH")"
    sleep 3
    assert_eq "true" "$(yb alive "$LOOP")" "(r1) an unreadable state keeps the loop"
    assert_eq "$N" "$(patch_n)" "(r1) ...and never PATCHes an unverified target"
    kill "$LOOP" 2> /dev/null
    rm -f "$STUB_DIR/issue-state-fail"

    # (r2) concurrent identical starts leave ONE renewer; other keys are their own.
    reset_state
    PIDS=()
    for i in 1 2 3 4; do
        start_loop 10229 "$WATCH" > "$STUB_DIR/r2-out.$i" &
        PIDS+=($!)
    done
    wait "${PIDS[@]}" 2> /dev/null
    OWNER="$(cat "$STUB_DIR"/r2-out.* | sort -u)"
    assert_eq "1" "$(printf '%s\n' "$OWNER" | grep -c .)" "(r2) four concurrent identical starts report ONE loop pid"
    O_ISSUE="$(start_loop 10230 "$WATCH")"
    O_SWEEP="$(start_loop 10229 "$WATCH" y-other)"
    O_REPO="$(LOOM_REPO=acme/other start_loop 10229 "$WATCH")"
    assert_eq "true" "$([[ -n "$O_ISSUE$O_SWEEP$O_REPO" && "$O_ISSUE" != "$OWNER" && "$O_SWEEP" != "$OWNER" && "$O_REPO" != "$OWNER" ]] && echo true || echo false)" "(r2) a different issue, sweep and repo each get their own loop"

    # (r3) release ends exactly that key's owner; a later start is fresh.
    "$SCRIPT" release 10229 --host y-host --sweep-id y-sweep 2> /dev/null
    sleep 0.5
    assert_eq "false" "$(yb alive "$OWNER")" "(r3) release stopped the owner loop"
    assert_eq "true" "$(if alive "$O_ISSUE" && alive "$O_SWEEP" && alive "$O_REPO"; then echo true; else echo false; fi)" "(r3) every other key's loop kept running"
    NEW="$(start_loop 10229 "$WATCH")"
    assert_eq "true" "$([[ -n "$NEW" && "$NEW" != "$OWNER" ]] && yb alive "$NEW" || echo false)" "(r3) a start after release becomes a fresh owner"

    # (r4) a killed owner's record is recovered by the next start.
    kill -9 "$NEW" 2> /dev/null
    sleep 0.3
    REC="$(start_loop 10229 "$WATCH")"
    assert_eq "true" "$([[ -n "$REC" && "$REC" != "$NEW" ]] && yb alive "$REC" || echo false)" "(r4) a killed owner's record is recovered"
    kill "$REC" "$O_ISSUE" "$O_SWEEP" "$O_REPO" "$WATCH" 2> /dev/null
fi

# (y6) per-cycle budget: steady state is one state read + one list/window read
# + one PATCH per interval (3 calls; 36/h at the default 300 s).
reset_state
sleep 30 &
WATCH=$!
LOOP="$(start_loop 10229 "$WATCH")"
wait_patches 3
kill "$LOOP" "$WATCH" 2> /dev/null
P="$(patch_n)"
S="$(cat "$STUB_DIR/state-calls.log" 2> /dev/null | wc -l | tr -d ' ')"
L="$(cat "$STUB_DIR/list-calls.log" 2> /dev/null | wc -l | tr -d ' ')"
assert_eq "true" "$([[ "$P" -ge 3 && "$S" -ge "$P" && "$S" -le $((P + 1)) && "$L" -ge "$P" && "$L" -le $((P + 1)) ]] && echo true || echo false)" "(y6) one state read, one list read, one PATCH per cycle (p=$P s=$S l=$L)"
# (y6) no App configured: every call runs on the caller's own credential.
assert_eq "" "$(grep -v 'tok=<unset> cred=ambient$' "$STUB_DIR/cred.log" 2> /dev/null)" "(y6) without an App every call is ambient, untagged as a fallback"

# --- Credential routing (#10229): reads on a reader App, the PATCH on the writer.
cred_lines() { sed 's/ repos\/[^ ]*\/issues\/comments\/[0-9]*/ PATCH-PATH/; s/ repos\/[^ ]*\/issues\/[0-9]*\/comments/ LIST-PATH/; s/ repos\/[^ ]*\/issues\/[0-9]*/ STATE-PATH/' "$STUB_DIR/cred.log" 2> /dev/null | sort -u; }

# (z1) App configured: the loop's state read and window read use the reader
# token, the PATCH the writer token, and nothing touches the ambient login.
reset_state
echo ghs_app > "$STUB_DIR/app-token"
sleep 30 &
WATCH=$!
LOOP="$(start_loop 10229 "$WATCH")"
wait_patches 2
kill "$LOOP" "$WATCH" 2> /dev/null
assert_eq "GET LIST-PATH tok=ghs_app-read cred=app
GET STATE-PATH tok=ghs_app-read cred=app
PATCH PATCH-PATH tok=ghs_app-write cred=app" "$(cred_lines)" "(z1) reads on the reader App, the PATCH on the writer App, no ambient call"
assert_eq "true" "$(grep -q -- '--repo acme/widget --access read' "$STUB_DIR/forge-token-args.log" && grep -q -- '--repo acme/widget --access write' "$STUB_DIR/forge-token-args.log" && echo true || echo false)" "(z1) the token is asked for this checkout's repo, per access"

# (z2) the App cannot PATCH (404: not installed on this repo) -> the call re-runs
# on the caller's credential, tagged, and the renewal still lands.
reset_state
echo ghs_app > "$STUB_DIR/app-token"
echo 404 > "$STUB_DIR/app-fail-PATCH"
OUT="$("$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep 2>&1)"
RC=$?
assert_eq "0" "$RC" "(z2) renewal succeeds after the App attempt fails"
assert_eq "true" "$([[ "$OUT" == *"lease-credential=ambient-fallback"* ]] && echo true || echo false)" "(z2) the fallback is tagged on stderr"
assert_eq "PATCH tok=<unset> cred=ambient" "$(grep '^PATCH' "$STUB_DIR/cred.log" | tail -n1 | sed 's/ repos[^ ]*//')" "(z2) the retry ran on the ambient credential"
assert_eq "1" "$(patch_n)" "(z2) exactly one PATCH landed"

# (z3) an App permission-scope 403 still climbs forge_gh_perm_safe's ladder
# under the App attempt (the personal rung recovers), with no wrapper-level
# fallback -- but the recovering attempt is attributed ambient, not app, and the
# recovery is visible on stderr although the call succeeded.
z3_patches() { grep '^PATCH' "$STUB_DIR/cred.log" | sed 's/ repos[^ ]*//'; }
z3_attempts() { printf '%s\n' "$OUT" | sed -n 's/^lease-credential-attempt: //p'; }
reset_state
echo ghs_app > "$STUB_DIR/app-token"
echo 403 > "$STUB_DIR/app-fail-PATCH"
OUT="$("$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep 2>&1)"
RC=$?
assert_eq "0" "$RC" "(z3) a 403 on the App PATCH recovers through the ladder"
assert_eq "false" "$([[ "$OUT" == *"lease-credential=ambient-fallback"* ]] && echo true || echo false)" "(z3) no wrapper fallback was needed"
assert_eq "1" "$(patch_n)" "(z3) exactly one PATCH landed"
assert_eq "PATCH tok=ghs_app-write cred=app
PATCH tok=<unset> cred=ambient" "$(z3_patches)" "(z3) the App attempt is app; the ambient personal login's recovery is ambient"
assert_eq "attempt=1 credential=app attribution=app
attempt=2 credential=personal-ambient attribution=ambient" "$(z3_attempts)" "(z3) every attempt's credential and attribution is on stderr"
assert_eq "true" "$([[ "$OUT" == *"lease-credential=ambient-recovered: the write call was recovered on the personal credential (personal-ambient)"* ]] && echo true || echo false)" "(z3) the personal recovery is tagged although the call succeeded"
assert_eq "true" "$([[ "$OUT" == *"forge: still 403 after a fresh mint"* ]] && echo true || echo false)" "(z3) the ladder's own diagnostics are no longer discarded"
assert_eq "" "$(grep -v '^PATCH' "$STUB_DIR/cred.log" | grep -v 'cred=app$')" "(z3) the reads stayed on the App"

# (z3b) LOOM_PERSONAL_GH_TOKEN is the personal rung: also ambient.
reset_state
echo ghs_app > "$STUB_DIR/app-token"
echo 403 > "$STUB_DIR/app-fail-PATCH"
OUT="$(LOOM_PERSONAL_GH_TOKEN=ghp_personal "$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep 2>&1)"
assert_eq "0" "$?" "(z3b) the personal-token rung recovers the App 403"
assert_eq "PATCH tok=ghs_app-write cred=app
PATCH tok=ghp_personal cred=ambient" "$(z3_patches)" "(z3b) the LOOM_PERSONAL_GH_TOKEN attempt is attributed ambient"
assert_eq "attempt=1 credential=app attribution=app
attempt=2 credential=personal-token attribution=ambient" "$(z3_attempts)" "(z3b) every attempt's credential and attribution is on stderr"
assert_eq "true" "$([[ "$OUT" == *"lease-credential=ambient-recovered: the write call was recovered on the personal credential (personal-token)"* ]] && echo true || echo false)" "(z3b) the personal recovery is tagged"

# (z3c) the fresh-mint rung stays app; only the personal rung after it is ambient.
reset_state
echo ghs_app > "$STUB_DIR/app-token"
echo 403 > "$STUB_DIR/app-fail-PATCH"
printf '#!/usr/bin/env bash\necho %s\n' "'{\"status\":\"ok\",\"token\":\"ghs_app-fresh\"}'" > "$STUB_DIR/github-app-token-ok.sh"
OUT="$(LOOM_GITHUB_APP_SCRIPT="$STUB_DIR/github-app-token-ok.sh" "$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep 2>&1)"
assert_eq "0" "$?" "(z3c) the ladder recovers after a fresh mint still 403s"
assert_eq "PATCH tok=ghs_app-write cred=app
PATCH tok=ghs_app-fresh cred=app
PATCH tok=<unset> cred=ambient" "$(z3_patches)" "(z3c) App, fresh-mint App, then the ambient personal login"
assert_eq "attempt=1 credential=app attribution=app
attempt=2 credential=app-fresh-mint attribution=app
attempt=3 credential=personal-ambient attribution=ambient" "$(z3_attempts)" "(z3c) every attempt's credential and attribution is on stderr"
rm -f "$STUB_DIR/github-app-token-ok.sh"

# (z3d) a clean App call stays silent: no attempt lines, no recovery tag.
reset_state
echo ghs_app > "$STUB_DIR/app-token"
OUT="$("$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep 2>&1)"
assert_eq "false" "$([[ "$OUT" == *"lease-credential"* ]] && echo true || echo false)" "(z3d) steady state prints no credential diagnostics"

# (z4) a deleted comment 404s on BOTH credentials -> still recognised as a
# PATCH 404, so the cached path re-lists (#10021) instead of failing.
reset_state
echo ghs_app > "$STUB_DIR/app-token"
echo 404 > "$STUB_DIR/app-fail-PATCH"
touch "$STUB_DIR/patch-404-once"
OUT="$("$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep --cached-lease 42@2026-10-01T00:00:00Z 2>&1)"
assert_eq "0" "$?" "(z4) a 404 on both credentials re-lists and renews"
assert_eq "true" "$([[ "$OUT" == *"returned 404; re-listing"* ]] && echo true || echo false)" "(z4) it is still recognised as the patch-404 fallback"
assert_eq "1" "$(patch_n)" "(z4) exactly one PATCH landed"

# (z5) sliding window: the cursor is the lease's updated_at as listed, and a
# yield record posted after it still stops renewal (own-yield guard retained).
reset_state
echo '[{"id": 42, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T05:00:00Z", "body": "<!-- loom:lease host=y-host sweep=y-sweep -->\nprose"}]' > "$STUB_DIR/comments.json"
OUT="$("$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep 2>&1)"
assert_eq "true" "$([[ "$OUT" == *"lease-cache=42@2026-10-01T05:00:00Z"* ]] && echo true || echo false)" "(z5) the next cursor is the listed updated_at, not created_at"
"$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep --cached-lease 42@2026-10-01T05:00:00Z > /dev/null 2>&1
assert_eq "repos/{owner}/{repo}/issues/10229/comments?since=2026-10-01T05:00:00Z&per_page=100" "$(tail -n1 "$STUB_DIR/list-calls.log")" "(z5) the cached window starts at that cursor"
echo '[{"id": 42, "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T05:05:00Z", "body": "<!-- loom:lease host=y-host sweep=y-sweep -->\nprose"},
 {"id": 43, "created_at": "2026-10-01T05:06:00Z", "updated_at": "2026-10-01T05:06:00Z", "body": "<!-- loom:lease-yield host=y-host sweep=y-sweep earliest_host=x earliest_sweep=z -->\nprose"}]' > "$STUB_DIR/comments.json"
N="$(patch_n)"
"$SCRIPT" renew-once 10229 --host y-host --sweep-id y-sweep --cached-lease 42@2026-10-01T05:00:00Z > /dev/null 2>&1
assert_eq "4" "$?" "(z5) a yield inside the sliding window still trips the own-yield guard"
assert_eq "$N" "$(patch_n)" "(z5) ...and nothing is PATCHed"

# (z6) no lease to renew: the loop stops after two consecutive misses instead
# of paying a --paginate listing every interval forever; the parent lives on.
reset_state
echo '[{"id": 1, "body": "no lease here"}]' > "$STUB_DIR/comments.json"
sleep 30 &
WATCH=$!
LOOP="$("$SCRIPT" start 10229 --interval 1 --watch-pid "$WATCH" --host y-host --sweep-id y-sweep 2> "$STUB_DIR/z-6.log")"
waited=0
while ((waited < 40)) && alive "$LOOP"; do sleep 0.2; waited=$((waited + 1)); done
assert_eq "false" "$(yb alive "$LOOP")" "(z6) a loop with no lease comment stops on its own"
assert_eq "true" "$(yb alive "$WATCH")" "(z6) the interactive parent was untouched"
assert_eq "2" "$(wc -l < "$STUB_DIR/list-calls.log" | tr -d ' ')" "(z6) after exactly two listings"
assert_eq "true" "$(grep -q 'no lease comment to renew on two consecutive cycles' "$STUB_DIR/z-6.log" && echo true || echo false)" "(z6) the stop is logged"
assert_eq "0" "$(patch_n)" "(z6) nothing was PATCHed"

# (z7) a miss followed by a hit resets the count: the loop keeps renewing.
reset_state
echo '[{"id": 1, "body": "no lease here"}]' > "$STUB_DIR/comments.json"
LOOP="$("$SCRIPT" start 10229 --interval 2 --watch-pid "$WATCH" --host y-host --sweep-id y-sweep 2> /dev/null)"
waited=0
while ((waited < 50)) && [[ ! -s "$STUB_DIR/list-calls.log" ]]; do sleep 0.1; waited=$((waited + 1)); done
echo "[$Y_LEASE]" > "$STUB_DIR/comments.json"
wait_patches 2
assert_eq "true" "$([[ "$(patch_n)" -ge 2 ]] && yb alive "$LOOP" || echo false)" "(z7) one miss then hits: the loop survives and renews"
kill "$LOOP" "$WATCH" 2> /dev/null
wait "$WATCH" 2> /dev/null

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if ((TESTS_FAILED > 0)); then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}ALL PASSED${NC}"
exit 0
