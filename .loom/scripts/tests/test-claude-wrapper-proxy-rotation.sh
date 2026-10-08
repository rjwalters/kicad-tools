#!/usr/bin/env bash
# test-claude-wrapper-proxy-rotation.sh — claude-wrapper.sh's account rotation
# under the Claude credential egress proxy (issue #8818).
#
# Background. With `runtimes.containment.claudeCredentialProxy` on, the wrapper
# runs inside a container whose token pool is masked and whose
# CLAUDE_CODE_OAUTH_TOKEN is a `loom-placeholder-…`. The three rotation helpers
# must then ask the HOST to rotate (`loom-daemon worker proxy-rotate`) instead
# of calling `tokens mark-bad` / `tokens select` against a pool they cannot see.
# The host half (mark, select, swap, trust boundary) is covered by the Rust
# suite in loom-daemon/src/worker_spawn/egress_proxy/rotation_tests.rs; this
# file pins the wrapper's routing:
#
#   1. proxied + exhausted     -> proxy-rotate usage-limit, no tokens calls, the
#                                 run continues on the rotated account
#   2. proxied + session limit -> proxy-rotate session-window
#   3. proxied + auth-dead     -> proxy-rotate auth-dead
#   4. proxied + concurrent    -> proxy-rotate concurrent-session
#   5. proxied + host refuses  -> ACCOUNT_POOL_EXHAUSTED, still no tokens calls
#   6. UNproxied + exhausted   -> tokens mark-bad + tokens select, no
#                                 proxy-rotate (AC4: unchanged)
#   7-8. proxied + 'OAuth token revoked' (#10294) -> auth-dead; refusal -> 78
#   9-10. DIRECT + 'OAuth token revoked' (#10294) against a stateful pool:
#         one auth-reason mark before re-selection, no backoff, exclusion
#         until unblock; no eligible alternate -> exit 78
#
# Style matches test-token-cache-affinity.sh — plain bash, hand-rolled
# assertions, stub `loom-daemon` and `claude`.
#
# Usage:
#   ./.loom/scripts/tests/test-claude-wrapper-proxy-rotation.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="$SCRIPTS_DIR/claude-wrapper.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected to contain: '$needle'"
        echo "    In: '$haystack'"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected NOT to contain: '$needle'"
        echo "    In: '$haystack'"
    fi
}

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

echo "============================================"
echo "test-claude-wrapper-proxy-rotation.sh (#8818)"
echo "============================================"

if [[ ! -f "$WRAPPER" ]]; then
    echo "SKIP: claude-wrapper.sh not found at $WRAPPER (not shipped into this layout)" >&2
    exit 0
fi

WS="$(mktemp -d)"
STUB="$(mktemp -d)"
CALLS="$(mktemp)"
trap 'rm -rf "$WS" "$STUB"; rm -f "$CALLS"' EXIT
mkdir -p "$WS/.loom/tokens"
PLACEHOLDER="loom-placeholder-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

# Stub daemon. `retry-classify` answers from the transcript (the same questions
# the real classifier is asked); `worker proxy-rotate` records its argv and,
# unless PROXY_REFUSE is set, hands back the rotated account; `tokens …` is
# recorded so the proxied cases can assert it was never reached.
cat > "$STUB/loom-daemon" <<STUB
#!/usr/bin/env bash
case "\$1 \${2:-}" in
    "retry-classify account-exhaustion") grep -qiE "hit your (session )?limit" && exit 0 || exit 1 ;;
    "retry-classify auth-dead") grep -qE "401 Invalid bearer token|OAuth token revoked" && exit 0 || exit 1 ;;
    "retry-classify session-limit") grep -q "concurrent sessions" && exit 0 || exit 1 ;;
    "retry-classify "*) exit 1 ;;
    "worker proxy-rotate")
        printf 'PROXY_ROTATE %s\n' "\${*:3}" >> "$CALLS"
        [[ -n "\${PROXY_REFUSE:-}" ]] && { echo "proxy-rotate: refused by the proxy (503 pool_exhausted)" >&2; exit 1; }
        printf "export LOOM_TOKEN_NAME='beta'\n"
        exit 0 ;;
    "tokens select")
        [[ "\${3:-}" == "--help" ]] && exit 0
        printf 'TOKENS_SELECT\n' >> "$CALLS"
        printf "export CLAUDE_CODE_OAUTH_TOKEN='tok-beta'\nexport LOOM_TOKEN_NAME='beta'\n"
        exit 0 ;;
    "tokens "*) printf 'TOKENS %s\n' "\${*:2}" >> "$CALLS"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$STUB/loom-daemon"

# Stub claude: account `alpha` fails with FAILURE_TEXT; any other account
# (i.e. after a rotation exported LOOM_TOKEN_NAME=beta) succeeds. Proxied, the
# token value never changes — only the host swapped what it stands for.
cat > "$STUB/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
[[ -n "${STUB_CALLS:-}" ]] && printf 'CLAUDE %s\n' "${LOOM_TOKEN_NAME:-}" >> "${STUB_CALLS}"
if [[ "${LOOM_TOKEN_NAME:-}" == "alpha" ]]; then
    echo "${FAILURE_TEXT}"
    exit 1
fi
# The direct cases (STUB_CALLS set) assert no credential reaches the output, so
# the stub itself must not print one there.
if [[ -n "${STUB_CALLS:-}" ]]; then echo "stub-claude success as ${LOOM_TOKEN_NAME}"; exit 0; fi
echo "stub-claude success as ${LOOM_TOKEN_NAME} token=${CLAUDE_CODE_OAUTH_TOKEN}"
exit 0
STUB
chmod +x "$STUB/claude"

# run_wrapper <token> <failure text> [extra env assignments...]
# Prints the wrapper's combined output; the call log is left in $CALLS.
run_wrapper() {
    local token="$1" failure="$2"
    shift 2
    : > "$CALLS"
    set +e
    env "$@" \
        LOOM_WORKSPACE="$WS" \
        LOOM_TOKEN_NAME="alpha" \
        CLAUDE_CODE_OAUTH_TOKEN="$token" \
        FAILURE_TEXT="$failure" \
        LOOM_DAEMON_BIN="$STUB/loom-daemon" \
        LOOM_DAEMON_SELF_BIN="$STUB/loom-daemon" \
        LOOM_MAX_RETRIES=1 \
        LOOM_SESSION_LIMIT_BACKOFF=0 \
        LOOM_SHEPHERD_TASK_ID="test-proxy-rotation" \
        LOOM_STARTUP_MONITOR_WINDOW=1 \
        PATH="$STUB:$PATH" \
        bash "$WRAPPER" -p "ping" 2>&1
    echo "$?" > "$WS/.last_rc"
    set -e
}

EXHAUSTED="You've hit your limit · resets 5pm"

echo ""
echo "1. Proxied + exhausted -> host-side rotation, launch continues"
out="$(run_wrapper "$PLACEHOLDER" "$EXHAUSTED")"
calls="$(cat "$CALLS")"
assert_contains "PROXY_ROTATE --reason usage-limit" "$calls" "exhaustion asks the proxy to rotate (usage-limit)"
assert_not_contains "TOKENS" "$calls" "no in-container tokens mark-bad/select on the proxied path"
assert_contains "stub-claude success as beta token=$PLACEHOLDER" "$out" \
    "the retry runs on the rotated account behind the SAME placeholder"
assert_not_contains "ACCOUNT_POOL_EXHAUSTED" "$out" "no pool-exhausted sentinel when the host rotated"

echo ""
echo "2. Proxied + 5h session window -> session-window"
run_wrapper "$PLACEHOLDER" "You've hit your session limit · resets 5pm" >/dev/null
assert_contains "PROXY_ROTATE --reason session-window" "$(cat "$CALLS")" "a session-limit exhaustion asks for the 5h-window mark"

echo ""
echo "3. Proxied + auth-dead -> auth-dead"
run_wrapper "$PLACEHOLDER" "Failed to authenticate. API Error: 401 Invalid bearer token" >/dev/null
calls="$(cat "$CALLS")"
assert_contains "PROXY_ROTATE --reason auth-dead" "$calls" "an auth-dead credential asks the proxy to rotate (auth-dead)"
assert_not_contains "TOKENS" "$calls" "no in-container tokens calls for auth-dead either"

echo ""
echo "4. Proxied + concurrent-session limit -> concurrent-session (no mark)"
run_wrapper "$PLACEHOLDER" "Error: maximum number of concurrent sessions reached for this account" >/dev/null
calls="$(cat "$CALLS")"
assert_contains "PROXY_ROTATE --reason concurrent-session" "$calls" "a capacity fault asks for a swap without a mark"
assert_not_contains "TOKENS" "$calls" "no in-container tokens calls for a capacity fault"

echo ""
echo "5. Proxied + host refuses -> ACCOUNT_POOL_EXHAUSTED, no fallback"
out="$(run_wrapper "$PLACEHOLDER" "$EXHAUSTED" PROXY_REFUSE=1)"
assert_contains "ACCOUNT_POOL_EXHAUSTED" "$out" "a refused host rotation ends the launch as before"
assert_contains "pool_exhausted" "$out" "the proxy's reason token reaches the log"
assert_not_contains "TOKENS" "$(cat "$CALLS")" "a refusal never falls back to in-container pool calls"

echo ""
echo "6. UNproxied + exhausted -> unchanged (tokens mark-bad + select)"
out="$(run_wrapper "tok-alpha" "$EXHAUSTED")"
calls="$(cat "$CALLS")"
assert_contains "TOKENS mark-bad alpha" "$calls" "an unproxied launch still bad-marks in-process"
assert_contains "TOKENS_SELECT" "$calls" "an unproxied launch still re-selects in-process"
assert_not_contains "PROXY_ROTATE" "$calls" "an unproxied launch never calls proxy-rotate"
assert_contains "stub-claude success as beta token=tok-beta" "$out" "the unproxied retry runs on the re-selected token"

echo ""
echo "7. #10294: 'OAuth token revoked' (no auxiliary verb) rotates, no backoff"
REVOKED="Failed to authenticate: OAuth token revoked. Please log in again or contact your administrator."
out="$(run_wrapper "$PLACEHOLDER" "$REVOKED")"
calls="$(cat "$CALLS")"
assert_contains "PROXY_ROTATE --reason auth-dead" "$calls" "the revoked token asks the proxy for an auth-dead rotation"
assert_not_contains "TOKENS" "$calls" "no in-container token mutation"
assert_not_contains "Transient error" "$out" "no transient backoff for a revoked token"
assert_contains "stub-claude success as beta" "$out" "the alternate account is attempted immediately"

echo ""
echo "8. #10294: revoked token and no alternate account -> exit 78"
out="$(run_wrapper "$PLACEHOLDER" "$REVOKED" PROXY_REFUSE=1)"
assert_contains "ACCOUNT_POOL_EXHAUSTED" "$out" "sentinel still emitted"
assert_eq "78" "$(cat "$WS/.last_rc")" "exit 78 when no alternate account"

# ---------------------------------------------------------------------------
# 9-10. #10294 DIRECT (unproxied) launch against a STATEFUL pool fixture.
#
# Cases 7-8 prove proxy routing only: the stub above classifies by its own
# regex and its `tokens select` always answers beta. Here, instead:
#   * classification comes from the REAL lib/classify-error.sh the wrapper
#     sources (it hands `--classification <category>` to retry-classify), and
#     retry-classify itself is a freshly built worktree daemon when one exists
#     ($LOOM_TEST_SELF_DAEMON, else <checkout>/target/{release,debug}); with no
#     build it falls back to a mirror of the Rust library arm
#     (`is_account_auth_dead`: classification == TOKEN_EXPIRED);
#   * the pool daemon keeps real state under a temp workspace OUTSIDE the
#     repository: <ws>/.loom/tokens/<name>.token (fake values) and .bad_tokens.
#     `select` returns the first account (name order) with no bad entry and
#     fails when none is eligible; `mark-bad` appends; `unblock` removes. An
#     auth-class reason never expires, as in bad_tokens.rs.
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
REAL_SELF=""
for cand in "${LOOM_TEST_SELF_DAEMON:-}" \
    "${CARGO_TARGET_DIR:-$REPO_ROOT/target}/release/loom-daemon" \
    "${CARGO_TARGET_DIR:-$REPO_ROOT/target}/debug/loom-daemon"; do
    if [[ -n "$cand" && -x "$cand" ]] && "$cand" retry-classify auth-dead --help >/dev/null 2>&1; then
        REAL_SELF="$cand"
        break
    fi
done
FIX="$(mktemp -d)"
DWS="$(mktemp -d)"
trap 'rm -rf "$WS" "$STUB" "$FIX" "$DWS"; rm -f "$CALLS"' EXIT

# Classification delegate: logs what it was asked, then answers via the real
# build or the library-arm mirror.
cat > "$FIX/classify-daemon" <<FIXTURE
#!/usr/bin/env bash
sub="\${2:-}" cls="" transient=0 args=("\$@")
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        --classification) cls="\$2"; shift ;;
        --classification-transient) transient=1 ;;
    esac
    shift
done
printf 'CLASSIFY %s classification=%s\n' "\$sub" "\${cls:-<degraded>}" >> "$CALLS"
if [[ -n "$REAL_SELF" ]]; then exec "$REAL_SELF" "\${args[@]}"; fi
cat >/dev/null
case "\$sub" in
    auth-dead) [[ "\$cls" == "TOKEN_EXPIRED" ]] ;;
    account-exhaustion) [[ "\$cls" == "TOKEN_EXHAUSTED" ]] ;;
    session-limit) [[ "\$cls" == "SESSION_LIMIT" ]] ;;
    transient) echo "\${cls:-UNCLASSIFIED}"; [[ "\$transient" -eq 1 ]] ;;
    *) exit 1 ;;
esac
FIXTURE
chmod +x "$FIX/classify-daemon"

# Stateful pool daemon (the INSTALLED-daemon role: mark-bad / select / unblock).
cat > "$FIX/pool-daemon" <<FIXTURE
#!/usr/bin/env bash
cmd="\${1:-} \${2:-}"; shift 2 || true
ws="" name="" reason="" export=0
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        --workspace) ws="\$2"; shift ;;
        --reason) reason="\$2"; shift ;;
        --export) export=1 ;;
        --help) exit 0 ;;
        -*) ;;
        *) name="\$1" ;;
    esac
    shift
done
tokdir="\${ws:-$DWS}/.loom/tokens"
bad="\$tokdir/.bad_tokens"
touch "\$bad"
case "\$cmd" in
    "tokens mark-bad")
        printf 'MARK_BAD %s reason=%s\n' "\$name" "\$reason" >> "$CALLS"
        printf '%s\t%s\n' "\$name" "\$reason" >> "\$bad" ;;
    "tokens unblock")
        printf 'UNBLOCK %s\n' "\$name" >> "$CALLS"
        grep -v "^\${name}	" "\$bad" > "\$bad.tmp" || true; mv "\$bad.tmp" "\$bad" ;;
    "tokens select")
        for f in "\$tokdir"/*.token; do
            [[ -e "\$f" ]] || continue
            n="\$(basename "\$f" .token)"
            grep -q "^\${n}	" "\$bad" && continue
            printf 'SELECT %s\n' "\$n" >> "$CALLS"
            [[ "\$export" -eq 1 ]] && printf "export CLAUDE_CODE_OAUTH_TOKEN='%s'\nexport LOOM_TOKEN_NAME='%s'\n" "\$(cat "\$f")" "\$n" || echo "\$n"
            exit 0
        done
        printf 'SELECT <none>\n' >> "$CALLS"
        exit 1 ;;
    "worker proxy-rotate") printf 'PROXY_ROTATE\n' >> "$CALLS"; exit 1 ;;
esac
exit 0
FIXTURE
chmod +x "$FIX/pool-daemon"

# Stubbed sleep: records calls and returns at once, so a backoff regression
# cannot stall the suite.
cat > "$FIX/sleep" <<FIXTURE
#!/usr/bin/env bash
printf 'SLEEP %s\n' "\$*" >> "$CALLS"
FIXTURE
chmod +x "$FIX/sleep"

# Fake credentials, workspace in a temp dir outside every repository/worktree.
FAKE_A="fake-alpha-$(printf '%s' "$DWS" | cksum | cut -d' ' -f1)-not-a-real-credential"
FAKE_B="fake-beta-$(printf '%s' "$DWS" | cksum | cut -d' ' -f1)-not-a-real-credential"

reset_pool() {
    rm -rf "$DWS/.loom"; mkdir -p "$DWS/.loom/tokens"
    printf '%s' "$FAKE_A" > "$DWS/.loom/tokens/alpha.token"
    [[ "$1" == "with-beta" ]] && printf '%s' "$FAKE_B" > "$DWS/.loom/tokens/beta.token"
    : > "$DWS/.loom/tokens/.bad_tokens"
}

run_direct() {
    local start="$SECONDS"
    : > "$CALLS"
    set +e
    env LOOM_WORKSPACE="$DWS" \
        LOOM_TOKEN_NAME="alpha" \
        CLAUDE_CODE_OAUTH_TOKEN="$FAKE_A" \
        FAILURE_TEXT="$REVOKED" \
        STUB_CALLS="$CALLS" \
        LOOM_DAEMON_BIN="$FIX/pool-daemon" \
        LOOM_DAEMON_SELF_BIN="$FIX/classify-daemon" \
        LOOM_MAX_RETRIES=3 \
        LOOM_INITIAL_WAIT=60 \
        LOOM_SESSION_LIMIT_BACKOFF=0 \
        LOOM_SHEPHERD_TASK_ID="test-direct-revoked" \
        LOOM_STARTUP_MONITOR_WINDOW=1 \
        PATH="$FIX:$STUB:$PATH" \
        bash "$WRAPPER" -p "ping" 2>&1
    echo "$?" > "$DWS/.last_rc"
    echo "$((SECONDS - start))" > "$DWS/.elapsed"
    set -e
}

# The transient backoff sleeps in 5s slices, indistinguishable from the
# background monitors' own 5s polls, so detect it at its entry instead:
# `calculate_wait_time` (-> `retry-classify wait-time`) runs only on the
# backoff path. The stubbed `sleep` keeps a regression from stalling the suite;
# the wall-clock assertion next to each use bounds the run independently.
backoff_sleeps() { grep -E '^CLASSIFY wait-time' "$CALLS" || true; }

# line_of <prefix> — 1-based line of the first call-log entry starting with it.
line_of() { { grep -n "^$1" "$CALLS" || true; } | head -1 | cut -d: -f1; }

echo ""
echo "9. #10294 DIRECT: exact incident message on alpha, beta healthy"
if [[ -n "$REAL_SELF" ]]; then
    echo "  (retry-classify: real daemon $REAL_SELF)"
else
    echo "  (retry-classify: no worktree build found; library-arm mirror over the real classify-error.sh)"
fi
reset_pool with-beta
out="$(run_direct)"
calls="$(cat "$CALLS")"
assert_eq "0" "$(cat "$DWS/.last_rc")" "the launch succeeds on the alternate account"
assert_contains "CLASSIFY auth-dead classification=TOKEN_EXPIRED" "$calls" \
    "the real shell classifier reports TOKEN_EXPIRED for the exact message"
assert_eq "1" "$(grep -c '^MARK_BAD ' "$CALLS")" "exactly one bad mark"
assert_contains "MARK_BAD alpha reason=auth-dead: OAuth token revoked" "$calls" \
    "the bad mark names alpha with an auth-dead reason"
reason="$(cut -f2 "$DWS/.loom/tokens/.bad_tokens")"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qiE '\b(401|oauth|auth(entication)?|unauthorized|token[_[:space:]]?expired|expired|blocked)\b' <<<"$reason"; then
    TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: the recorded reason is auth-class (bad_tokens.rs auth_reason_regex)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: reason '$reason' is not auth-class"
fi
mark_line="$(line_of 'MARK_BAD alpha')"; sel_line="$(line_of 'SELECT beta')"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -n "$mark_line" && -n "$sel_line" && "$mark_line" -lt "$sel_line" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: alpha is bad-marked before beta is selected"
else
    TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: mark/select order wrong (mark=$mark_line select=$sel_line)"; echo "$calls"
fi
assert_eq "1" "$(grep -c '^CLAUDE alpha$' "$CALLS")" "alpha is attempted exactly once (no retry on it)"
assert_eq "CLAUDE alpha|CLAUDE beta" "$(grep '^CLAUDE ' "$CALLS" | paste -sd'|' -)" \
    "beta is the very next attempt"
assert_eq "" "$(backoff_sleeps)" "no transient backoff (wait-time never computed) between the two attempts"
assert_eq "1" "$(( $(cat "$DWS/.elapsed") < 60 ))" "the failover completes well under a minute"
assert_not_contains "CLASSIFY transient" "$calls" "the transient predicate is never consulted"
assert_not_contains "Transient error" "$out" "no transient-backoff log line"
assert_not_contains "PROXY_ROTATE" "$calls" "a direct launch never asks the proxy"
assert_contains "Marked account 'alpha' auth-dead in .bad_tokens (OAuth token revoked)" "$out" \
    "rejection/quarantine evidence names the account and failure class"
assert_not_contains "$FAKE_A" "$out" "no credential value of alpha in the wrapper output"
assert_not_contains "$FAKE_B" "$out" "no credential value of beta in the wrapper output"
assert_not_contains "$FAKE_A" "$(cat "$DWS/.loom/tokens/.bad_tokens")" "no credential value in the quarantine record"
# Subsequent selection excludes alpha until an explicit unblock.
assert_eq "beta" "$("$FIX/pool-daemon" tokens select --workspace "$DWS")" "a later selection still excludes alpha"
assert_eq "beta" "$("$FIX/pool-daemon" tokens select --workspace "$DWS")" "...and again (the auth mark does not expire)"
"$FIX/pool-daemon" tokens unblock alpha --workspace "$DWS"
assert_eq "alpha" "$("$FIX/pool-daemon" tokens select --workspace "$DWS")" "after tokens unblock, alpha is eligible again"

echo ""
echo "10. #10294 DIRECT: exact incident message, no eligible alternate -> exit 78"
reset_pool alpha-only
out="$(run_direct)"
calls="$(cat "$CALLS")"
assert_eq "78" "$(cat "$DWS/.last_rc")" "exit 78 (EX_CONFIG) when no eligible account remains"
assert_eq "1" "$(grep -c '^MARK_BAD alpha reason=auth-dead' "$CALLS")" "alpha is still bad-marked once (auth reason)"
assert_contains "SELECT <none>" "$calls" "selection found no eligible account"
assert_eq "1" "$(grep -c '^CLAUDE ' "$CALLS")" "no further launch attempt"
assert_eq "" "$(backoff_sleeps)" "no transient backoff (wait-time never computed) before giving up"
assert_eq "1" "$(( $(cat "$DWS/.elapsed") < 60 ))" "exit 78 arrives well under a minute"
assert_contains "ACCOUNT_POOL_EXHAUSTED" "$out" "the pool-exhausted sentinel is emitted"
assert_not_contains "$FAKE_A" "$out" "no credential value in the failure output"

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo "Tests passed: $TESTS_PASSED"
echo "Tests failed: $TESTS_FAILED"
echo "==================================="
[[ "$TESTS_FAILED" -eq 0 ]]
