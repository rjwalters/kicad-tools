#!/usr/bin/env bash
# test-spawn-claude.sh — Tests for spawn-claude.sh and classify_error.
#
# Style matches test-forge-helpers.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-claude.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEFAULTS_DIR/.." && pwd)"

# Resolve THIS checkout's own `loom-daemon` binary (issue #4228 — spawn-claude.sh
# / claude-wrapper.sh now select/mark-bad tokens through it instead of Python).
# Pin it explicitly into every spawn-claude.sh / claude-wrapper.sh invocation
# below via LOOM_DAEMON_BIN so the assertions exercise THIS checkout's daemon,
# never any host-level install / canary loom-daemon the dev environment may
# have on PATH (this file used to pin the Python selector's package path for the
# same reason, before #4557 deleted the Python package).
DAEMON_BIN=""
for _candidate in \
    "$REPO_ROOT/target/release/loom-daemon" \
    "$REPO_ROOT/target/debug/loom-daemon"; do
    if [[ -x "$_candidate" ]]; then
        DAEMON_BIN="$_candidate"
        break
    fi
done

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="$3"
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
    local needle="$1"
    local haystack="$2"
    local msg="$3"
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

assert_not_contains() {
    local needle="$1"
    local haystack="$2"
    local msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Forbidden substring present: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# ============================================================
# Section 1: classify_error test vectors (curator's 18 vectors)
# ============================================================

echo "Testing classify_error..."
# Source the library
# shellcheck source=../lib/classify-error.sh
source "$SCRIPTS_DIR/lib/classify-error.sh"

# Vector #1: clean exit with "500" in output → SUCCESS (regression #3233)
result=$(classify_error "successfully merged PR #500 with status 200" 0)
assert_eq "SUCCESS" "$result" "exit=0 output containing '500' is SUCCESS (#3233 regression)"

# Vector #2: clean exit with "rate limit" substring → SUCCESS (regression #3233)
result=$(classify_error "rate limit headers indicate 4500 remaining" 0)
assert_eq "SUCCESS" "$result" "exit=0 with 'rate limit' substring is SUCCESS (#3233 regression)"

# Vector #3: clean exit, "No messages returned" → SUCCESS (regression #3233)
result=$(classify_error "No messages returned in this cycle, exiting" 0)
assert_eq "SUCCESS" "$result" "exit=0 with 'No messages returned' is SUCCESS (#3233 regression)"

# Vector #4: defensive — clean exit even with "500 Internal Server Error"
result=$(classify_error "Internal Server Error 500" 0)
assert_eq "SUCCESS" "$result" "exit=0 with '500 Internal Server Error' is SUCCESS"

# Vector #5: clean exit, empty output
result=$(classify_error "" 0)
assert_eq "SUCCESS" "$result" "exit=0 empty output is SUCCESS"

# Vector #6: timeout exit 124 → TIMEOUT
result=$(classify_error "anything" 124)
assert_eq "TIMEOUT" "$result" "exit=124 is TIMEOUT"

# Vector #7: SIGKILL exit 137 → TIMEOUT
result=$(classify_error "anything" 137)
assert_eq "TIMEOUT" "$result" "exit=137 is TIMEOUT"

# Vector #8: 401 expired → TOKEN_EXPIRED
result=$(classify_error "OAuth token has expired" 1)
assert_eq "TOKEN_EXPIRED" "$result" "OAuth token expired -> TOKEN_EXPIRED"

# Vector #9: 401 authentication_error → TOKEN_EXPIRED
result=$(classify_error "401 authentication_error" 1)
assert_eq "TOKEN_EXPIRED" "$result" "401 authentication_error -> TOKEN_EXPIRED"

# Vector #10: hit your limit → TOKEN_EXHAUSTED
result=$(classify_error "You've hit your limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "hit your limit -> TOKEN_EXHAUSTED"

# Vector #11: weekly limit → TOKEN_EXHAUSTED
result=$(classify_error "hit your weekly limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "weekly limit -> TOKEN_EXHAUSTED"

# Vector #12: 429 → RECOVERABLE
result=$(classify_error "429 Too Many Requests" 1)
assert_eq "RECOVERABLE" "$result" "429 -> RECOVERABLE"

# Vector #13: 500 (with non-zero exit) → RECOVERABLE
result=$(classify_error "500 Internal Server Error" 1)
assert_eq "RECOVERABLE" "$result" "500 + exit=1 -> RECOVERABLE"

# Vector #14: 503 → RECOVERABLE
result=$(classify_error "503 Service Unavailable" 1)
assert_eq "RECOVERABLE" "$result" "503 -> RECOVERABLE"

# Vector #15: ECONNREFUSED → RECOVERABLE
result=$(classify_error "ECONNREFUSED" 1)
assert_eq "RECOVERABLE" "$result" "ECONNREFUSED -> RECOVERABLE"

# Vector #16: No messages returned + exit=1 → RECOVERABLE (only when failed)
result=$(classify_error "No messages returned" 1)
assert_eq "RECOVERABLE" "$result" "'No messages' + exit=1 -> RECOVERABLE"

# Vector #17: cwd deleted → CWD_DELETED
result=$(classify_error "current working directory was deleted" 1)
assert_eq "CWD_DELETED" "$result" "cwd deleted -> CWD_DELETED"

# Vector #18: catch-all unknown failure → RECOVERABLE
result=$(classify_error "" 2)
assert_eq "RECOVERABLE" "$result" "unknown exit=2 -> RECOVERABLE"

# --- MODEL_REFUSAL vectors (issue #3702) ---
# A model safety-classifier refusal (stop_reason "refusal") on a non-zero-exit
# run classifies as MODEL_REFUSAL — a routing error the sweep orchestrator
# handles by dropping one ladder rung without consuming a Doctor cycle.

# Vector #19: JSON-shaped stop_reason refusal + exit=1 → MODEL_REFUSAL
result=$(classify_error '{"type":"message","stop_reason":"refusal"}' 1)
assert_eq "MODEL_REFUSAL" "$result" 'stop_reason:"refusal" + exit=1 -> MODEL_REFUSAL'

# Vector #20: spaced stop_reason = refusal + exit=1 → MODEL_REFUSAL
result=$(classify_error "turn ended: stop_reason: refusal (safety)" 1)
assert_eq "MODEL_REFUSAL" "$result" "spaced stop_reason refusal + exit=1 -> MODEL_REFUSAL"

# Vector #21 (REGRESSION, #3233): clean exit (exit 0) whose output merely
# contains the word "refusal" is STILL SUCCESS — exit-code-first ordering wins
# over any substring, including the new refusal match.
result=$(classify_error '{"stop_reason":"refusal"}' 0)
assert_eq "SUCCESS" "$result" 'exit=0 with stop_reason:"refusal" is SUCCESS (#3233 exit-code-first)'

# Vector #22: plain word "refusal" without a stop_reason on exit=1 is NOT a
# refusal classification — the match is anchored to the stop_reason key, so an
# unrelated failure mentioning the word falls through to the catch-all.
result=$(classify_error "connection reset; will not retry (refusal to reconnect)" 1)
assert_eq "RECOVERABLE" "$result" "bare 'refusal' word (no stop_reason) + exit=1 -> RECOVERABLE"

# --- Widened TOKEN_EXHAUSTED vectors (issue #3738) ---
# The Claude CLI emits several usage/session/weekly/monthly limit phrasings.
# A naive `hit.your.limit`-style pattern misses the multi-word-gap variants
# ("hit your SESSION limit", "monthly usage limit", "out of extra usage") —
# each is verified individually, not just the legacy short form (#10/#11).

# Vector #23: session limit (the exact canary phrase) → TOKEN_EXHAUSTED
result=$(classify_error "You've hit your session limit · resets 4pm" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "hit your session limit -> TOKEN_EXHAUSTED (#3738)"

# Vector #24: full weekly-limit phrasing with reset suffix → TOKEN_EXHAUSTED
result=$(classify_error "You've hit your weekly limit · resets Monday 9am" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "hit your weekly limit (full phrasing) -> TOKEN_EXHAUSTED (#3738)"

# Vector #25: org monthly usage limit → TOKEN_EXHAUSTED
result=$(classify_error "You've hit your org's monthly usage limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "monthly usage limit -> TOKEN_EXHAUSTED (#3738)"

# Vector #26: out of extra usage → TOKEN_EXHAUSTED
result=$(classify_error "You're out of extra usage · resets 4pm" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "out of extra usage -> TOKEN_EXHAUSTED (#3738)"

# Vector #27: plain "You've hit your limit" still classifies (legacy short form)
result=$(classify_error "You've hit your limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "legacy 'hit your limit' still -> TOKEN_EXHAUSTED (#3738)"

# Vector #28 (REGRESSION, #3233): exit-0 output mentioning a limit phrase in
# prose is STILL SUCCESS — exit-code-first ordering wins over the new patterns.
result=$(classify_error "Note: agents pause when they hit your session limit." 0)
assert_eq "SUCCESS" "$result" "exit=0 mentioning 'hit your session limit' is SUCCESS (#3233/#3738)"

# Vector #29 (REGRESSION): exit-0 with "out of extra usage" in prose → SUCCESS
result=$(classify_error "The plan was out of extra usage headroom last week." 0)
assert_eq "SUCCESS" "$result" "exit=0 mentioning 'out of extra usage' is SUCCESS (#3233/#3738)"

# --- SESSION_LIMIT vectors (issue #3947) ---
# A concurrent-session-limit fault is a capacity signal (per-token stacking), NOT
# quota exhaustion, so it classifies distinctly as SESSION_LIMIT and callers must
# NOT mark the token bad. Checked BEFORE TOKEN_EXHAUSTED so the "session limit"
# substring is not swallowed by the exhaustion regex.

# Vector #30: explicit concurrent session limit → SESSION_LIMIT
result=$(classify_error "Error: you have reached your concurrent session limit" 1)
assert_eq "SESSION_LIMIT" "$result" "concurrent session limit -> SESSION_LIMIT (#3947)"

# Vector #31: "maximum number of concurrent sessions" → SESSION_LIMIT
result=$(classify_error "maximum number of concurrent sessions reached" 1)
assert_eq "SESSION_LIMIT" "$result" "maximum number of concurrent sessions -> SESSION_LIMIT (#3947)"

# Vector #32: "too many concurrent" → SESSION_LIMIT
result=$(classify_error "429: too many concurrent requests for this account" 1)
assert_eq "SESSION_LIMIT" "$result" "too many concurrent -> SESSION_LIMIT (#3947)"

# Vector #33: "another session is already running" → SESSION_LIMIT
result=$(classify_error "another session is already active for this token" 1)
assert_eq "SESSION_LIMIT" "$result" "another session already active -> SESSION_LIMIT (#3947)"

# Vector #34 (DISAMBIGUATION): a WEEKLY "session limit" (no concurrency wording)
# stays TOKEN_EXHAUSTED — the capacity classifier must not steal quota faults.
result=$(classify_error "You've hit your session limit · resets 4pm" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "weekly 'session limit' stays TOKEN_EXHAUSTED (#3947 disambiguation)"

# Vector #35 (REGRESSION, #3233): exit-0 output mentioning concurrent sessions in
# prose is STILL SUCCESS — exit-code-first ordering wins over the new pattern.
result=$(classify_error "Note: agents pause on a concurrent session limit." 0)
assert_eq "SUCCESS" "$result" "exit=0 mentioning 'concurrent session' is SUCCESS (#3233/#3947)"

# --- Per-model limit vectors (issue #4501) ---
# The CLI now emits a PER-MODEL ceiling whose model name sits between "your" and
# "limit" — "You've reached your Fable 5 limit. Run /usage-credits to continue or
# switch models with /model." None of the "hit your …" phrasings matched it, so
# `is_account_exhaustion` returned false, no rotation happened, and the child died
# permanently at CLI start. Classified as TOKEN_EXHAUSTED (a safe
# over-approximation — see classify-error.sh) so it reaches the rotation path.

# Vector #36: the exact live message → TOKEN_EXHAUSTED
result=$(classify_error "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model." 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "'reached your Fable 5 limit' -> TOKEN_EXHAUSTED (#4501)"

# Vector #37: no model name in between → still TOKEN_EXHAUSTED
result=$(classify_error "You've reached your limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "'reached your limit' -> TOKEN_EXHAUSTED (#4501)"

# Vector #38: the Codex-style wording reaching the claude table → TOKEN_EXHAUSTED
result=$(classify_error "You have reached your usage limit for this account" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "'reached your usage limit' -> TOKEN_EXHAUSTED (#4501)"

# Vector #39 (ORDERING HAZARD, #3947 must survive): the concurrency wording keeps
# its distinct capacity classification even though it also matches the widened
# "reached your … limit" shape — SESSION_LIMIT is checked FIRST.
result=$(classify_error "Error: you have reached your concurrent session limit" 1)
assert_eq "SESSION_LIMIT" "$result" "'reached your concurrent session limit' still SESSION_LIMIT (#4501 preserves #3947)"

# Vector #40 (REGRESSION, #3233): a clean exit whose output merely mentions a
# model limit is still SUCCESS — exit-code-first ordering is untouched.
result=$(classify_error "Tip: you'll see 'reached your Fable 5 limit' when the tier is capped." 0)
assert_eq "SUCCESS" "$result" "exit=0 mentioning 'reached your Fable 5 limit' is SUCCESS (#3233/#4501)"

# Vector #41 (BOUNDED FILLER): the model-name gap is capped at three words, so an
# unrelated sentence that merely ends in "limit" is not swallowed into exhaustion.
result=$(classify_error "reached your goal well before the team agreed on any spending limit" 1)
assert_eq "RECOVERABLE" "$result" "a long unrelated 'reached your … limit' sentence is not exhaustion (#4501)"

# --- Retry-verdict vectors (issue #4501) ---
# `classification_is_transient` is now THE single source of truth for every retry
# decision, so the printed classification and the retry choice cannot disagree.

for _c in RECOVERABLE TOKEN_EXHAUSTED SESSION_LIMIT; do
    if classification_is_transient "$_c"; then
        assert_eq "retry" "retry" "classification_is_transient $_c -> retry (#4501)"
    else
        assert_eq "retry" "terminal" "classification_is_transient $_c -> retry (#4501)"
    fi
done
for _c in SUCCESS TIMEOUT TOKEN_EXPIRED CWD_DELETED MODEL_REFUSAL FATAL; do
    if classification_is_transient "$_c"; then
        assert_eq "terminal" "retry" "classification_is_transient $_c -> terminal (#4501)"
    else
        assert_eq "terminal" "terminal" "classification_is_transient $_c -> terminal (#4501)"
    fi
done

# The contradiction this issue exists to kill: a RECOVERABLE verdict can never
# coexist with a "not retrying" decision, whatever the output text.
if classification_is_transient "RECOVERABLE"; then
    assert_eq "retry" "retry" "RECOVERABLE is always retryable — the #4501 contradiction cannot recur"
else
    assert_eq "retry" "terminal" "RECOVERABLE is always retryable — the #4501 contradiction cannot recur"
fi

# --- Provider-selector vectors (issue #4190) ---
# classify_error() now accepts an optional 3rd provider arg with precedence
# 3rd arg > $LOOM_RUNTIME > "claude". These vectors prove the split into
# engine + provider pattern tables preserves every prior behavior and adds
# the new selector semantics correctly.

# Vector #36: explicit 3rd arg "claude" behaves identically to the 2-arg
# default (bit-identical proof, explicit form)
result=$(classify_error "OAuth token has expired" 1 "claude")
assert_eq "TOKEN_EXPIRED" "$result" "explicit 3rd arg 'claude' classifies like the 2-arg default (#4190)"

# Vector #37: LOOM_RUNTIME=claude env selects the claude table (no 3rd arg)
result=$(LOOM_RUNTIME=claude classify_error "You've hit your weekly limit" 1)
assert_eq "TOKEN_EXHAUSTED" "$result" "LOOM_RUNTIME=claude selects the claude table (#4190)"

# Vector #38: explicit 3rd arg overrides a conflicting LOOM_RUNTIME
result=$(LOOM_RUNTIME=nosuch classify_error "OAuth token has expired" 1 "claude")
assert_eq "TOKEN_EXPIRED" "$result" "3rd arg 'claude' overrides a conflicting LOOM_RUNTIME=nosuch (#4190)"

# Vector #39: unknown provider (LOOM_RUNTIME=nosuch) hits the generic 429
# fallback rather than erroring or matching a Claude-specific pattern
result=$(LOOM_RUNTIME=nosuch classify_error "429 Too Many Requests" 1)
assert_eq "RECOVERABLE" "$result" "unknown provider -> generic 429 fallback, no error (#4190)"

# Vector #40: unknown provider hits the generic 5xx fallback
result=$(LOOM_RUNTIME=nosuch classify_error "503 Service Unavailable" 1)
assert_eq "RECOVERABLE" "$result" "unknown provider -> generic 5xx fallback, no error (#4190)"

# Vector #41: unknown provider hits the generic network-error fallback
result=$(LOOM_RUNTIME=nosuch classify_error "ECONNREFUSED" 1)
assert_eq "RECOVERABLE" "$result" "unknown provider -> generic network fallback, no error (#4190)"

# Vector #42: unknown provider does NOT match a Claude-specific pattern —
# "OAuth token has expired" falls through to the generic catch-all instead of
# TOKEN_EXPIRED, proving provider tables are genuinely provider-scoped.
result=$(LOOM_RUNTIME=nosuch classify_error "OAuth token has expired" 1)
assert_eq "RECOVERABLE" "$result" "unknown provider does not match the claude-only TOKEN_EXPIRED pattern (#4190)"

# Vector #43: unknown provider + clean exit is still SUCCESS (engine layer is
# provider-independent)
result=$(LOOM_RUNTIME=nosuch classify_error "anything" 0)
assert_eq "SUCCESS" "$result" "unknown provider + exit=0 is SUCCESS (#4190)"

# Vector #44: SESSION_LIMIT still wins over TOKEN_EXHAUSTED under an explicit
# claude provider selector (ordering invariant preserved post-split)
result=$(classify_error "Error: you have reached your concurrent session limit" 1 "claude")
assert_eq "SESSION_LIMIT" "$result" "SESSION_LIMIT beats TOKEN_EXHAUSTED under explicit claude provider (#4190)"

# Vector #45: exit 124/137 -> TIMEOUT regardless of provider (engine layer)
result=$(LOOM_RUNTIME=nosuch classify_error "anything" 124)
assert_eq "TIMEOUT" "$result" "exit=124 is TIMEOUT regardless of provider (#4190)"
result=$(classify_error "anything" 137 "claude")
assert_eq "TIMEOUT" "$result" "exit=137 is TIMEOUT regardless of provider (#4190)"

# Vector #46: a 2-arg call under `set -u` does not trip an unbound-variable
# error — the provider default-expansion chain must be safe with no 3rd arg
# and no LOOM_RUNTIME set.
set -u
result=$(env -u LOOM_RUNTIME bash -c '
  source "'"$SCRIPTS_DIR"'/lib/classify-error.sh"
  set -u
  classify_error "anything" 1
')
assert_eq "RECOVERABLE" "$result" "2-arg call under set -u with no LOOM_RUNTIME does not error (#4190)"

# ============================================================
# Section 2: spawn-claude.sh dispatch (with stub `claude`)
# ============================================================

echo ""
echo "Testing spawn-claude.sh dispatch..."

# spawn-claude.sh's token selection is now a hard dependency on the native
# `loom-daemon` binary (issue #4228, epic #4081 Phase 2 — no Python fallback
# exists to degrade to). Fail fast with a clear message rather than let every
# subsequent test below fail opaquely on "No loom-daemon binary...".
if [[ -z "$DAEMON_BIN" ]]; then
    echo "FATAL: no loom-daemon binary found at $REPO_ROOT/target/{release,debug}/loom-daemon" >&2
    echo "  Build one first: cd loom-daemon && cargo build --release" >&2
    echo "  (spawn-claude.sh's token selection requires it as of issue #4228)" >&2
    exit 1
fi

# Set up a fake workspace
TEST_WS="$(mktemp -d)"
trap 'rm -rf "$TEST_WS"' EXIT

mkdir -p "$TEST_WS/.loom/tokens"
chmod 700 "$TEST_WS/.loom/tokens"
echo -n "fake-token-alpha" > "$TEST_WS/.loom/tokens/alpha.token"
chmod 600 "$TEST_WS/.loom/tokens/alpha.token"

# Stub `claude` binary
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_WS" "$STUB_DIR"' EXIT
cat > "$STUB_DIR/claude" <<'STUB'
#!/usr/bin/env bash
echo "stub-claude got token=${CLAUDE_CODE_OAUTH_TOKEN}"
echo "stub-claude args=$*"
echo "stub-claude ceiling=${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-unset}"
exit 0
STUB
chmod +x "$STUB_DIR/claude"

# Test: spawn-claude selects the only token and exec's the stub
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude got token=fake-token-alpha" "$output" \
    "spawn-claude exports selected token to claude"
assert_contains "stub-claude args=-p ping" "$output" \
    "spawn-claude passes args through to claude"
assert_contains "OAuth account 'alpha'" "$output" \
    "spawn-claude logs the chosen account"

# Test: binary provenance at the selection site (issue #4643). The daemon
# binary that decides token selection is resolved by spawn-claude.sh itself,
# so a stale one is invisible unless it is logged. Assert both the resolved
# path and a version string land in the sweep log.
assert_contains "spawn-claude: token-selection binary: $DAEMON_BIN" "$output" \
    "spawn-claude logs the resolved daemon binary path at token selection (#4643)"
assert_contains "(loom-daemon " "$output" \
    "spawn-claude logs the daemon binary's --version at token selection (#4643)"

# Test: spawn-claude disables the print-mode background-task wait ceiling
# (issue #3943) so a daemon-dispatched sweep child's long-running Builder/Judge
# subagents are not reaped at the 600s ceiling. Default = 0 (no cap).
assert_contains "stub-claude ceiling=0" "$output" \
    "spawn-claude exports CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 by default (#3943)"

# Test: an operator-set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS is PRESERVED
# (the `:=` default-assignment idiom only fills an unset/empty value).
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="120000" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude ceiling=120000" "$output" \
    "spawn-claude preserves an operator-set wait ceiling (#3943)"

# Test: the daemon self-claim marker's presence/value is always logged (Issue
# #3967) — with the var unset, an interactive/manual invocation logs "unset".
# Explicitly unset it first: this test suite may itself be running inside a
# daemon-dispatched `/loom:sweep` session, which would otherwise leak an
# ambient `LOOM_SWEEP_CLAIM_OWNED` into the subshell and false-fail this case.
output=$(env -u LOOM_SWEEP_CLAIM_OWNED LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "spawn-claude: LOOM_SWEEP_CLAIM_OWNED=unset" "$output" \
    "spawn-claude logs LOOM_SWEEP_CLAIM_OWNED=unset when not daemon-dispatched (#3967)"

# Test: with the var set (simulating a daemon-dispatched child, #3823), the
# same log line echoes the exact issue number — the diagnostic this issue's
# acceptance criteria calls for, straight from the per-sweep log.
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_SWEEP_CLAIM_OWNED="3964" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "spawn-claude: LOOM_SWEEP_CLAIM_OWNED=3964" "$output" \
    "spawn-claude logs the daemon self-claim marker's value when set (#3967)"

# Test: the `--claim-owned <N>` self-claim marker (issue #4111 — the belt half
# of the belt-and-suspenders fix) is embedded INSIDE the `-p` prompt string,
# exactly as `SweepRegistry::spawn_child` now emits it
# (`-p "/loom:sweep <N> --claim-owned <N>"`). This is deliberate: `--claim-owned`
# is NOT a real `claude` CLI flag — spawn-claude.sh forwards every non-wrapper
# token verbatim to the real `claude` binary (`exec claude "$@"`), so passing
# `--claim-owned` as its OWN argv token makes `claude` exit 1
# (`error: unknown option '--claim-owned'`) before any session starts (#4120
# review). Only text inside the single `-p "<prompt>"` value reaches the
# `/loom:sweep` skill's own `$ARGUMENTS`. This test asserts the prompt (with the
# embedded flag) is forwarded verbatim as a single `-p` argument, alongside the
# env var (kept for backward compatibility, #3823/#3967).
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_SWEEP_CLAIM_OWNED="4111" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "/loom:sweep 4111 --claim-owned 4111" \
        --dangerously-skip-permissions 2>&1 || true)
assert_contains "stub-claude args=-p /loom:sweep 4111 --claim-owned 4111 --dangerously-skip-permissions" \
    "$output" \
    "spawn-claude forwards the -p prompt (with embedded --claim-owned) verbatim to claude (#4111/#4120)"
assert_contains "spawn-claude: LOOM_SWEEP_CLAIM_OWNED=4111" "$output" \
    "spawn-claude still logs the env var when the --claim-owned flag is also present (#4111 backward compat)"

# Test: explicit CLAUDE_CODE_OAUTH_TOKEN bypasses selection
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    CLAUDE_CODE_OAUTH_TOKEN="caller-supplied" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude got token=caller-supplied" "$output" \
    "explicit CLAUDE_CODE_OAUTH_TOKEN is preserved"

# Test: missing tokens dir → exit 78 with helpful message.
# LOOM_SHARED_TOKENS_DIR="" disables the #3938 shared-pool fallback so this
# case deterministically hard-fails regardless of the host's ~/.loom/tokens.
EMPTY_WS="$(mktemp -d)"
output=$(LOOM_WORKSPACE="$EMPTY_WS" LOOM_SHARED_TOKENS_DIR="" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
exit_code=$?
# The recovery advice names the NATIVE `loom-daemon tokens bootstrap` (epic
# #4081 Phase 4, #4557 — the Python `loom-tokens` console script it used to name
# no longer exists, so advising it would be dead-end advice).
assert_contains "tokens bootstrap" "$output" \
    "empty pool error mentions '<loom-daemon> tokens bootstrap'"
assert_not_contains "loom-tokens bootstrap" "$output" \
    "empty pool error does NOT advise the retired loom-tokens binary (#4557)"
rm -rf "$EMPTY_WS"

# Test: an all-bad pool reports PER-TOKEN exclusion detail plus the deciding
# binary (issue #4643). Diagnosing the 2026-07-30 incident required reading
# Rust source because "All N tokens ... are marked bad or empty" said nothing
# about why each account was out or which binary decided. The whole point is
# that this is answerable from the sweep log, so assert it at the spawn layer.
BAD_WS="$(mktemp -d)"
mkdir -p "$BAD_WS/.loom/tokens"
echo -n "fake-token-exh" > "$BAD_WS/.loom/tokens/exh.token"
echo -n "fake-token-auth" > "$BAD_WS/.loom/tokens/auth.token"
_now_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
{
    echo "$_now_ts exh exhausted: hit your session limit"
    echo "$_now_ts auth 401 unauthorized"
} > "$BAD_WS/.loom/tokens/.bad_tokens"
output=$(LOOM_WORKSPACE="$BAD_WS" LOOM_SHARED_TOKENS_DIR="" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "exh: bad-marked [exhaustion, TTL]" "$output" \
    "all-bad pool error names the exhaustion entry's class and TTL (#4643)"
assert_contains "auth: bad-marked [auth, permanent]" "$output" \
    "all-bad pool error names the auth entry as permanent (#4643)"
assert_contains "clears in " "$output" \
    "all-bad pool error reports the cooldown remaining (#4643)"
assert_contains "deciding binary: loom-daemon " "$output" \
    "all-bad pool error names the deciding binary (#4643)"
rm -rf "$BAD_WS"

# Test that spawn-claude.sh exits 78 on missing tokens (shared fallback off).
set +e
LOOM_WORKSPACE="$(mktemp -d)" LOOM_SHARED_TOKENS_DIR="" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" >/dev/null 2>&1
exit_code=$?
set -e
assert_eq "78" "$exit_code" "missing tokens exits 78 (EX_CONFIG)"

# Test: consumer repo with NO per-repo pool falls back to the shared
# machine-level pool (issue #3938) instead of hard-failing.
CONSUMER_WS="$(mktemp -d)"
SHARED_POOL="$(mktemp -d)"
echo -n "fake-token-shared" > "$SHARED_POOL/shared-acct.token"
chmod 600 "$SHARED_POOL/shared-acct.token"
output=$(LOOM_WORKSPACE="$CONSUMER_WS" LOOM_SHARED_TOKENS_DIR="$SHARED_POOL" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude got token=fake-token-shared" "$output" \
    "spawn-claude falls back to shared pool for a repo with no pool (#3938)"
assert_contains "OAuth account 'shared-acct'" "$output" \
    "spawn-claude logs the shared-pool account (#3938)"
# State must land in the shared pool, never forked into the consumer repo.
# Uses the native `loom-daemon tokens mark-bad` CLI (issue #4228) instead of
# the historical inline `mark_bad` Python call (that package is gone as of #4557).
LOOM_SHARED_TOKENS_DIR="$SHARED_POOL" "$DAEMON_BIN" tokens mark-bad shared-acct \
    --reason test --workspace "$CONSUMER_WS" >/dev/null 2>&1 || true
if [[ -f "$SHARED_POOL/.bad_tokens" && ! -f "$CONSUMER_WS/.loom/tokens/.bad_tokens" ]]; then
    assert_eq "ok" "ok" "shared-pool .bad_tokens written to shared dir, not consumer repo (#3938)"
else
    assert_eq "ok" "fail" "shared-pool .bad_tokens written to shared dir, not consumer repo (#3938)"
fi
rm -rf "$CONSUMER_WS" "$SHARED_POOL"

# ============================================================
# Section 3: model selection (issue #3477, Phase 1)
#
# Precedence chain at the spawn layer, all four observable cases:
#   1. explicit --model arg beats LOOM_MODEL env
#   2. --model=value form also beats LOOM_MODEL env
#   3. LOOM_MODEL alone produces --model in the exec'd args
#   4. no env + no arg produces NO --model at all (session default
#      preserved — the zero-behavior-change acceptance criterion)
# ============================================================

echo ""
echo "Testing spawn-claude.sh model selection (#3477)..."

# Case 3: LOOM_MODEL env produces --model in args
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="claude-sonnet-4-6" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude args=-p ping --model claude-sonnet-4-6" "$output" \
    "LOOM_MODEL env injects --model into claude args"
assert_contains "spawn-claude: model=claude-sonnet-4-6 (from LOOM_MODEL)" "$output" \
    "structured model log line emitted for LOOM_MODEL case (#3482)"

# Case 1: explicit --model arg wins over LOOM_MODEL env
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="claude-sonnet-4-6" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" --model claude-opus-4-8 2>&1 || true)
assert_contains "stub-claude args=-p ping --model claude-opus-4-8" "$output" \
    "explicit --model arg wins over LOOM_MODEL env"
assert_contains "spawn-claude: model=claude-opus-4-8 (from --model arg)" "$output" \
    "structured model log line emitted for explicit --model arg case (#3482)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"claude-sonnet-4-6"* ]] || [[ "$output" == *"wins over LOOM_MODEL"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_MODEL value is not injected when explicit --model present"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_MODEL value is not injected when explicit --model present"
    echo "    In: '$output'"
fi

# Case 2: --model=value form also suppresses LOOM_MODEL injection
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="claude-sonnet-4-6" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" --model=claude-opus-4-8 2>&1 || true)
assert_contains "stub-claude args=-p ping --model=claude-opus-4-8" "$output" \
    "--model=value form wins over LOOM_MODEL env"
assert_contains "spawn-claude: model=claude-opus-4-8 (from --model arg)" "$output" \
    "structured model log line emitted for --model=value form (#3482)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"--model claude-sonnet-4-6"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: --model=value suppresses LOOM_MODEL injection"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: --model=value suppresses LOOM_MODEL injection"
    echo "    In: '$output'"
fi

# Case 4 (zero-behavior-change criterion): no env + no arg => no --model
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" == *"stub-claude args=-p ping"* && "$output" != *"--model"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no LOOM_MODEL + no --model arg emits NO --model (session default preserved)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: no LOOM_MODEL + no --model arg emits NO --model (session default preserved)"
    echo "    In: '$output'"
fi
assert_contains "spawn-claude: model=default" "$output" \
    "structured model=default log line emitted when nothing configured (#3482)"

# Empty LOOM_MODEL is treated as unset — no --model emitted
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_MODEL="" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" == *"stub-claude args=-p ping"* && "$output" != *"--model"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: empty LOOM_MODEL emits NO --model"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: empty LOOM_MODEL emits NO --model"
    echo "    In: '$output'"
fi
assert_contains "spawn-claude: model=default" "$output" \
    "structured model=default log line emitted for empty LOOM_MODEL (#3482)"

# ============================================================
# Section 4: effort selection (issue #3705)
#
# Mirrors the model precedence chain for the `claude --effort <level>`
# session knob, all observable cases:
#   1. LOOM_EFFORT alone produces --effort in the exec'd args
#   2. explicit --effort arg beats LOOM_EFFORT env
#   3. --effort=value form also beats LOOM_EFFORT env
#   4. no env + no arg produces NO --effort at all (session default
#      preserved — the zero-behavior-change acceptance criterion)
#   5. empty LOOM_EFFORT is treated as unset — no --effort emitted
# ============================================================

echo ""
echo "Testing spawn-claude.sh effort selection (#3705)..."

# Case 1: LOOM_EFFORT env produces --effort in args
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_EFFORT="xhigh" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "stub-claude args=-p ping --effort xhigh" "$output" \
    "LOOM_EFFORT env injects --effort into claude args"
assert_contains "spawn-claude: effort=xhigh (from LOOM_EFFORT)" "$output" \
    "structured effort log line emitted for LOOM_EFFORT case (#3705)"

# Case 2: explicit --effort arg wins over LOOM_EFFORT env
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_EFFORT="xhigh" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" --effort high 2>&1 || true)
assert_contains "stub-claude args=-p ping --effort high" "$output" \
    "explicit --effort arg wins over LOOM_EFFORT env"
assert_contains "spawn-claude: effort=high (from --effort arg)" "$output" \
    "structured effort log line emitted for explicit --effort arg case (#3705)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"--effort xhigh"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_EFFORT value is not injected when explicit --effort present"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_EFFORT value is not injected when explicit --effort present"
    echo "    In: '$output'"
fi

# Case 3: --effort=value form also suppresses LOOM_EFFORT injection
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_EFFORT="xhigh" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" --effort=high 2>&1 || true)
assert_contains "stub-claude args=-p ping --effort=high" "$output" \
    "--effort=value form wins over LOOM_EFFORT env"
assert_contains "spawn-claude: effort=high (from --effort arg)" "$output" \
    "structured effort log line emitted for --effort=value form (#3705)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"--effort xhigh"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: --effort=value suppresses LOOM_EFFORT injection"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: --effort=value suppresses LOOM_EFFORT injection"
    echo "    In: '$output'"
fi

# Case 4 (zero-behavior-change criterion): no env + no arg => no --effort
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" == *"stub-claude args=-p ping"* && "$output" != *"--effort"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no LOOM_EFFORT + no --effort arg emits NO --effort (session default preserved)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: no LOOM_EFFORT + no --effort arg emits NO --effort (session default preserved)"
    echo "    In: '$output'"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"spawn-claude: effort="* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no effort log line emitted when nothing configured (#3705)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: no effort log line emitted when nothing configured (#3705)"
    echo "    In: '$output'"
fi

# Case 5: empty LOOM_EFFORT is treated as unset — no --effort emitted
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$STUB_DIR:$PATH" \
    LOOM_EFFORT="" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" == *"stub-claude args=-p ping"* && "$output" != *"--effort"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: empty LOOM_EFFORT emits NO --effort"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: empty LOOM_EFFORT emits NO --effort"
    echo "    In: '$output'"
fi

# ============================================================
# Section 5: claude-wrapper.sh account rotation on exhaustion (#3738)
#
# On a session/usage-limit fault the wrapper must mark the active account bad,
# re-select a fresh account, and retry WITHOUT consuming a MAX_RETRIES attempt.
# When the whole pool is exhausted it must emit a distinct sentinel (not the
# generic "Max retries exceeded" path) and exit non-zero.
# ============================================================

echo ""
echo "Testing claude-wrapper.sh account rotation (#3738)..."

WRAPPER="$SCRIPTS_DIR/claude-wrapper.sh"

if [[ -z "$DAEMON_BIN" ]]; then
    echo "  (skipping rotation tests — loom-daemon binary not found)"
else
  # --- Fixture: 2-account pool; stub exhausts alpha, succeeds on beta ---
  ROT_WS="$(mktemp -d)"
  mkdir -p "$ROT_WS/.loom/tokens"
  chmod 700 "$ROT_WS/.loom/tokens"
  printf '%s' "tok-alpha" > "$ROT_WS/.loom/tokens/alpha.token"
  printf '%s' "tok-beta"  > "$ROT_WS/.loom/tokens/beta.token"
  chmod 600 "$ROT_WS/.loom/tokens/"*.token

  ROT_STUB="$(mktemp -d)"
  cat > "$ROT_STUB/claude" <<'STUB'
#!/usr/bin/env bash
# Only the real prompt run (contains "-p") drives rotation; any preflight
# subcommands (auth/mcp/version) succeed quietly.
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
if [[ "${CLAUDE_CODE_OAUTH_TOKEN}" == "tok-alpha" ]]; then
    echo "You've hit your session limit · resets 4pm"
    exit 1
fi
echo "stub-claude success on token=${CLAUDE_CODE_OAUTH_TOKEN}"
exit 0
STUB
  chmod +x "$ROT_STUB/claude"

  # Force the first account to alpha (as spawn-claude would). MAX_RETRIES=1:
  # if rotation consumed a MAX_RETRIES attempt, beta would never be tried.
  set +e
  rot_out=$(
    LOOM_WORKSPACE="$ROT_WS" \
    LOOM_TOKEN_NAME="alpha" \
    CLAUDE_CODE_OAUTH_TOKEN="tok-alpha" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" \
    LOOM_MAX_RETRIES=1 \
    LOOM_SHEPHERD_TASK_ID="test-rotation" \
    LOOM_STARTUP_MONITOR_WINDOW=1 \
    PATH="$ROT_STUB:$PATH" \
    bash "$WRAPPER" -p "ping" 2>&1
  )
  rot_rc=$?
  set -e

  assert_contains "stub-claude success on token=tok-beta" "$rot_out" \
      "wrapper rotates from exhausted alpha to beta and succeeds"
  assert_eq "0" "$rot_rc" \
      "wrapper exits 0 after rotation (MAX_RETRIES=1 not consumed by rotation)"

  bad_file="$ROT_WS/.loom/tokens/.bad_tokens"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ -f "$bad_file" ]] && grep -qw "alpha" "$bad_file" && ! grep -qw "beta" "$bad_file"; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: .bad_tokens records exhausted alpha but not beta"
  else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: .bad_tokens records exhausted alpha but not beta"
      echo "    .bad_tokens: $(cat "$bad_file" 2>/dev/null || echo '<missing>')"
  fi

  # --- Whole-pool exhaustion: both accounts exhaust → ACCOUNT_POOL_EXHAUSTED ---
  ROT_WS2="$(mktemp -d)"
  mkdir -p "$ROT_WS2/.loom/tokens"
  chmod 700 "$ROT_WS2/.loom/tokens"
  printf '%s' "tok-a" > "$ROT_WS2/.loom/tokens/a.token"
  printf '%s' "tok-b" > "$ROT_WS2/.loom/tokens/b.token"
  chmod 600 "$ROT_WS2/.loom/tokens/"*.token

  ROT_STUB2="$(mktemp -d)"
  cat > "$ROT_STUB2/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
echo "You've hit your session limit · resets 4pm"
exit 1
STUB
  chmod +x "$ROT_STUB2/claude"

  set +e
  rot2_out=$(
    LOOM_WORKSPACE="$ROT_WS2" \
    LOOM_TOKEN_NAME="a" \
    CLAUDE_CODE_OAUTH_TOKEN="tok-a" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" \
    LOOM_MAX_RETRIES=1 \
    LOOM_SHEPHERD_TASK_ID="test-rotation" \
    LOOM_STARTUP_MONITOR_WINDOW=1 \
    PATH="$ROT_STUB2:$PATH" \
    bash "$WRAPPER" -p "ping" 2>&1
  )
  rot2_rc=$?
  set -e

  assert_contains "ACCOUNT_POOL_EXHAUSTED" "$rot2_out" \
      "whole-pool exhaustion emits the ACCOUNT_POOL_EXHAUSTED sentinel"
  assert_contains "Whole account pool exhausted" "$rot2_out" \
      "whole-pool exhaustion logs a distinct (non-'Max retries') message"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$rot2_rc" -ne 0 ]]; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: whole-pool exhaustion exits non-zero"
  else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: whole-pool exhaustion exits non-zero"
  fi
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$rot2_out" != *"Max retries"* ]]; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: whole-pool exhaustion avoids the generic Max-retries path"
  else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: whole-pool exhaustion avoids the generic Max-retries path"
  fi

  rm -rf "$ROT_WS" "$ROT_STUB" "$ROT_WS2" "$ROT_STUB2"
fi

# ============================================================
# Section 6: claude-wrapper.sh script(1) calling convention (#4192)
#
# util-linux script(1) and BSD/macOS script(1) take their arguments in
# incompatible orders. _run_via_script() (extracted from run_with_retry's
# TTY branch specifically so it is unit-testable, #4192) selects the right
# form via `script --version`. These tests exercise the extracted function
# directly by sourcing the wrapper with CLAUDE_WRAPPER_SOURCE_ONLY=1 (which
# suppresses the `main "$@"` CLI entry point), rather than going through
# run_with_retry's `[ -t 0 ]` gate -- a test harness has no real TTY on
# stdin to reach that branch.
# ============================================================

echo ""
echo "Testing claude-wrapper.sh script(1) calling convention (#4192)..."

CC_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_WS" "$STUB_DIR" "$CC_DIR"' EXIT

# Claude stub used by both branches: echoes each arg delimited, so embedded
# spaces/quotes/newlines are unambiguously verifiable in the captured output.
cat > "$CC_DIR/claude" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
    printf 'ARG<%s>\n' "$a"
done
STUB
chmod +x "$CC_DIR/claude"

# --- util-linux `script` stub: supports --version, records its own argv,
#     and (matching real util-linux behavior) runs the `-c` command string
#     via $SHELL, propagating exit status only when `-e` is present. ---
UL_ARGV_FILE="$CC_DIR/ul-argv.txt"
cat > "$CC_DIR/script-util-linux" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "--version" ]]; then
    echo "script from util-linux 2.37.4"
    exit 0
fi
: > "$UL_ARGV_FILE"
for a in "\$@"; do
    printf '%s\n' "\$a" >> "$UL_ARGV_FILE"
done
cmd=""
prev=""
for a in "\$@"; do
    if [[ "\$prev" == "-c" ]]; then
        cmd="\$a"
    fi
    prev="\$a"
done
file="\${*: -1}"
"\${SHELL:-/bin/sh}" -c "\$cmd" > "\$file" 2>&1
rc=\$?
for a in "\$@"; do
    if [[ "\$a" == "-e" ]]; then
        exit \$rc
    fi
done
exit 0
STUB
chmod +x "$CC_DIR/script-util-linux"
mkdir -p "$CC_DIR/ul-bin"
cp "$CC_DIR/script-util-linux" "$CC_DIR/ul-bin/script"
cp "$CC_DIR/claude" "$CC_DIR/ul-bin/claude"

# --- BSD/macOS `script` stub: no --version support (matches real BSD
#     script's "illegal option" behavior on macOS), records argv, and runs
#     the legacy `script -q FILE cmd args...` form. ---
BSD_ARGV_FILE="$CC_DIR/bsd-argv.txt"
cat > "$CC_DIR/script-bsd" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "--version" ]]; then
    echo "script: illegal option -- -" >&2
    exit 1
fi
: > "$BSD_ARGV_FILE"
for a in "\$@"; do
    printf '%s\n' "\$a" >> "$BSD_ARGV_FILE"
done
file="\$2"
cmd_args=("\${@:3}")
"\${cmd_args[@]}" > "\$file" 2>&1
exit \$?
STUB
chmod +x "$CC_DIR/script-bsd"
mkdir -p "$CC_DIR/bsd-bin"
cp "$CC_DIR/script-bsd" "$CC_DIR/bsd-bin/script"
cp "$CC_DIR/claude" "$CC_DIR/bsd-bin/claude"

# Driver scripts (real files, not nested-quoted `bash -c` strings) so
# multi-line/quoted test arguments don't have to survive several layers of
# shell requoting.
cat > "$CC_DIR/ul-driver.sh" <<'DRIVER'
#!/usr/bin/env bash
CLAUDE_WRAPPER_SOURCE_ONLY=1 source "$WRAPPER"
set +e
_run_via_script "$UL_OUT" claude "hello world" 'quote"inside' $'line1\nline2'
echo "RC=$?"
DRIVER

cat > "$CC_DIR/ul-fail-driver.sh" <<'DRIVER'
#!/usr/bin/env bash
CLAUDE_WRAPPER_SOURCE_ONLY=1 source "$WRAPPER"
set +e
_run_via_script "$UL_OUT" bash -c "exit 7"
echo "$?"
DRIVER

cat > "$CC_DIR/bsd-driver.sh" <<'DRIVER'
#!/usr/bin/env bash
CLAUDE_WRAPPER_SOURCE_ONLY=1 source "$WRAPPER"
set +e
_run_via_script "$BSD_OUT" claude "hello world" "--dangerously-skip-permissions"
echo "RC=$?"
DRIVER

# ---- util-linux branch: composes `-q -e -c <quoted cmd>` and pins bash ----
UL_OUT="$CC_DIR/ul-out.txt"
ul_result=$(
    PATH="$CC_DIR/ul-bin:$PATH" WRAPPER="$WRAPPER" UL_OUT="$UL_OUT" SHELL=/bin/zsh \
        bash "$CC_DIR/ul-driver.sh" 2>&1
)
assert_contains "RC=0" "$ul_result" \
    "util-linux branch: _run_via_script exits 0 on success (#4192)"

ul_argv_1=$(sed -n '1p' "$UL_ARGV_FILE")
ul_argv_2=$(sed -n '2p' "$UL_ARGV_FILE")
ul_argv_3=$(sed -n '3p' "$UL_ARGV_FILE")
ul_argv_4=$(sed -n '4p' "$UL_ARGV_FILE")
assert_eq "-q" "$ul_argv_1" "util-linux branch composes -q as the first script(1) flag (#4192)"
assert_eq "-e" "$ul_argv_2" "util-linux branch composes -e for exit-code propagation (#4192)"
assert_eq "-c" "$ul_argv_3" "util-linux branch composes -c to run the quoted command (#4192)"

ul_captured="$(cat "$UL_OUT")"
assert_contains "claude" "$ul_argv_4" \
    "util-linux branch: claude invocation is embedded in the quoted -c command string (#4192)"
assert_contains "ARG<hello world>" "$ul_captured" \
    "util-linux branch: arg with an embedded space survives the round trip (#4192)"
assert_contains 'ARG<quote"inside>' "$ul_captured" \
    "util-linux branch: arg with an embedded double-quote survives the round trip (#4192)"
assert_contains "$(printf 'ARG<line1\nline2>')" "$ul_captured" \
    "util-linux branch: arg with an embedded newline survives the round trip -- proves the \
executing shell is pinned to bash (dash cannot parse %q's \$'...' ANSI-C form) (#4192)"

# Exit-code propagation (#4192 gap 1): util-linux `script` exits 0 by
# default; the real child exit status must still reach the caller via -e.
ul_fail_rc=$(
    PATH="$CC_DIR/ul-bin:$PATH" WRAPPER="$WRAPPER" UL_OUT="$UL_OUT" \
        bash "$CC_DIR/ul-fail-driver.sh"
)
assert_eq "7" "$ul_fail_rc" \
    "util-linux branch: -e propagates the real child exit code through script(1) (#4192)"

# ---- BSD/macOS branch: stays byte-identical to the pre-#4192 form ----
BSD_OUT="$CC_DIR/bsd-out.txt"
bsd_result=$(
    PATH="$CC_DIR/bsd-bin:$PATH" WRAPPER="$WRAPPER" BSD_OUT="$BSD_OUT" \
        bash "$CC_DIR/bsd-driver.sh" 2>&1
)
assert_contains "RC=0" "$bsd_result" \
    "BSD/macOS branch: _run_via_script exits 0 on success (#4192)"

bsd_argv_1=$(sed -n '1p' "$BSD_ARGV_FILE")
bsd_argv_2=$(sed -n '2p' "$BSD_ARGV_FILE")
bsd_argv_3=$(sed -n '3p' "$BSD_ARGV_FILE")
assert_eq "-q" "$bsd_argv_1" "BSD/macOS branch: form unchanged, -q first (#4192)"
assert_eq "$BSD_OUT" "$bsd_argv_2" "BSD/macOS branch: form unchanged, FILE second (#4192)"
assert_eq "claude" "$bsd_argv_3" \
    "BSD/macOS branch: form unchanged, cmd third -- byte-identical to pre-#4192 (#4192)"

bsd_captured="$(cat "$BSD_OUT")"
assert_contains "ARG<hello world>" "$bsd_captured" \
    "BSD/macOS branch: args reach claude unchanged (no %q quoting involved) (#4192)"
assert_contains "ARG<--dangerously-skip-permissions>" "$bsd_captured" \
    "BSD/macOS branch: --dangerously-skip-permissions is not misparsed as script's own flag (#4192)"

rm -rf "$CC_DIR"

# ============================================================
# Section 7: spawn scheduling priority (issue #4233)
#
# Sweep worker processes (and everything they exec/fork downstream) get
# niced up so they no longer starve interactive/system processes under
# fan-out. The niceness value is observable via `ps -o ni=` on the exec'd
# `claude` stub's own pid — the re-exec preserves the pid, so this directly
# proves the value the eventual `claude`/cargo/rustc process tree inherits.
# ============================================================

echo ""
echo "Testing spawn-claude.sh scheduling priority (#4233)..."

PRIO_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_WS" "$STUB_DIR" "$PRIO_DIR"' EXIT

cat > "$PRIO_DIR/claude" <<'STUB'
#!/usr/bin/env bash
echo "nice=$(ps -o ni= -p $$ | tr -d ' ')"
STUB
chmod +x "$PRIO_DIR/claude"

# Fake `taskpolicy` that records its invocation instead of requiring a real
# macOS host — this is a plain PATH-resolved stub, so it exercises the
# `command -v taskpolicy` branch deterministically on any OS.
TASKPOLICY_LOG="$PRIO_DIR/taskpolicy.log"
cat > "$PRIO_DIR/taskpolicy" <<STUB
#!/usr/bin/env bash
echo "\$*" >> "$TASKPOLICY_LOG"
exit 0
STUB
chmod +x "$PRIO_DIR/taskpolicy"

# Test: default niceness is 10 with no env/config set.
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "nice=10" "$output" \
    "spawn-claude defaults to nice 10 (#4233)"
assert_contains "spawn-claude: re-exec at nice -n 10" "$output" \
    "spawn-claude logs the re-exec niceness (#4233)"

# Test: LOOM_SWEEP_NICE=0 disables the entire mechanism (nice AND taskpolicy).
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    LOOM_SWEEP_NICE=0 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "nice=0" "$output" \
    "LOOM_SWEEP_NICE=0 disables the priority mechanism, restoring nice 0 (#4233)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"re-exec at nice"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_SWEEP_NICE=0 suppresses the re-exec entirely (#4233)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_SWEEP_NICE=0 suppresses the re-exec entirely (#4233)"
    echo "    In: '$output'"
fi

# Test: LOOM_SWEEP_NICENESS overrides the default value.
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    LOOM_SWEEP_NICENESS=15 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "nice=15" "$output" \
    "LOOM_SWEEP_NICENESS env overrides the default niceness (#4233)"

# Test: autonomous.spawnNiceness config knob is honored when no env is set
# (env > config > default precedence).
CFG_WS="$(mktemp -d)"
mkdir -p "$CFG_WS/.loom/tokens"
chmod 700 "$CFG_WS/.loom/tokens"
echo -n "fake-token-cfg" > "$CFG_WS/.loom/tokens/alpha.token"
chmod 600 "$CFG_WS/.loom/tokens/alpha.token"
cat > "$CFG_WS/.loom/config.json" <<'EOF'
{ "autonomous": { "spawnNiceness": 7 } }
EOF
output=$(LOOM_WORKSPACE="$CFG_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "nice=7" "$output" \
    "autonomous.spawnNiceness config knob is honored (#4233)"

# Test: an explicit LOOM_SWEEP_NICENESS env still wins over the config knob.
output=$(LOOM_WORKSPACE="$CFG_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    LOOM_SWEEP_NICENESS=3 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "nice=3" "$output" \
    "LOOM_SWEEP_NICENESS env wins over autonomous.spawnNiceness config (#4233)"

# Test: LOOM_SWEEP_TASKPOLICY_CLASS invokes the (stubbed) taskpolicy command
# with the expected class and this process's own pid, additive to nice.
: > "$TASKPOLICY_LOG"
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    LOOM_SWEEP_TASKPOLICY_CLASS="utility" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "-c utility" "$(cat "$TASKPOLICY_LOG")" \
    "LOOM_SWEEP_TASKPOLICY_CLASS invokes taskpolicy -c <class> (#4233)"
assert_contains "spawn-claude: applied taskpolicy -c utility" "$output" \
    "spawn-claude logs the applied taskpolicy class (#4233)"
assert_contains "nice=10" "$output" \
    "taskpolicy and the default nice both apply together (#4233)"

# Test: autonomous.spawnTaskpolicyClass config knob is honored.
: > "$TASKPOLICY_LOG"
cat > "$CFG_WS/.loom/config.json" <<'EOF'
{ "autonomous": { "spawnNiceness": 7, "spawnTaskpolicyClass": "utility" } }
EOF
output=$(LOOM_WORKSPACE="$CFG_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "-c utility" "$(cat "$TASKPOLICY_LOG")" \
    "autonomous.spawnTaskpolicyClass config knob is honored (#4233)"
rm -rf "$CFG_WS"

# Test: a re-exec loop cannot happen — the LOOM_SWEEP_NICED sentinel, once
# set, is never re-applied even if the caller passes a differing niceness.
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$PRIO_DIR:$PATH" \
    LOOM_SWEEP_NICED=1 LOOM_SWEEP_NICENESS=15 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$output" != *"re-exec at nice"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_SWEEP_NICED sentinel prevents a second re-exec (#4233)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_SWEEP_NICED sentinel prevents a second re-exec (#4233)"
    echo "    In: '$output'"
fi

rm -rf "$PRIO_DIR"

# ============================================================
# Section 7b: per-sweep CPU quota (issue #5111)
#
# Nothing bounded a sweep's CPU, so an agent-written driver could run N
# concurrent CPU-bound processes and saturate an entire host. On a host with
# a reachable `systemd --user` manager, spawn-claude.sh wraps the final
# `claude`/`claude-wrapper.sh` exec in `systemd-run --user --scope -p
# CPUQuota=<budget*100>%` (an actual kernel cgroup quota); everywhere else
# (this dev host included) it degrades to advisory-only: the computed budget
# is still exported as LOOM_SWEEP_CPU_BUDGET_CORES, but nothing wraps the
# exec. Both branches are exercised below — `LOOM_SYSTEMD_FORCE=1` (the same
# test-only seam lib/systemd-user.sh already exposes) drives the enforced
# path deterministically on any OS, including this macOS dev host.
# ============================================================

echo ""
echo "Testing spawn-claude.sh CPU quota (#5111)..."

CPU_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_WS" "$STUB_DIR" "$CPU_DIR"' EXIT

cat > "$CPU_DIR/claude" <<'STUB'
#!/usr/bin/env bash
echo "stub-claude budget=${LOOM_SWEEP_CPU_BUDGET_CORES:-unset}"
STUB
chmod +x "$CPU_DIR/claude"

# Deterministic core count for every test below, regardless of host.
cat > "$CPU_DIR/nproc" <<'STUB'
#!/usr/bin/env bash
echo 8
STUB
chmod +x "$CPU_DIR/nproc"

# Fake `systemd-run` that (1) logs its invocation and (2) actually execs the
# wrapped command after stripping the leading flags/properties up to `--`, so
# the stub `claude` behind it still runs and the assertions can see its
# output too — mirrors how a real `systemd-run --scope` hands off to its
# target.
SYSTEMD_RUN_LOG="$CPU_DIR/systemd-run.log"
cat > "$CPU_DIR/systemd-run" <<STUB
#!/usr/bin/env bash
echo "\$*" >> "$SYSTEMD_RUN_LOG"
args=("\$@")
skip=0
for ((i = 0; i < \${#args[@]}; i++)); do
    if [[ "\${args[i]}" == "--" ]]; then
        skip=\$((i + 1))
        break
    fi
done
exec "\${args[@]:skip}"
STUB
chmod +x "$CPU_DIR/systemd-run"

# `is_linux_systemd`'s LOOM_SYSTEMD_FORCE=1 branch still probes `command -v
# systemctl`, so a stub must be on PATH for the forced branch to engage.
cat > "$CPU_DIR/systemctl" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB
chmod +x "$CPU_DIR/systemctl"

# Test: with no systemd --user manager reachable (the default on this dev
# host, and on every macOS worker in this fleet today), the budget is still
# computed and exported, but nothing wraps the exec — advisory-only.
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    env -u LOOM_SYSTEMD_FORCE \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "CPU budget = 6 core(s) of 8 (reserved 2)" "$output" \
    "spawn-claude computes budget = total - reserved(2) with no env/config set (#5111)"
assert_contains "stub-claude budget=6" "$output" \
    "spawn-claude exports LOOM_SWEEP_CPU_BUDGET_CORES to the child (#5111)"
assert_contains "advisory-only" "$output" \
    "spawn-claude logs advisory-only when no systemd --user manager is reachable (#5111)"

# Test: LOOM_SYSTEMD_FORCE=1 drives the enforced path — the final exec is
# wrapped in `systemd-run --user --scope -p CPUQuota=<budget*100>%`.
: > "$SYSTEMD_RUN_LOG"
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "enforcing CPUQuota=600% via systemd --user scope" "$output" \
    "spawn-claude enforces CPUQuota=600% (6 cores * 100) when a systemd --user scope is available (#5111)"
assert_contains "stub-claude budget=6" "$output" \
    "the wrapped stub claude still runs and sees the exported budget (#5111)"
assert_contains "CPUQuota=600%" "$(cat "$SYSTEMD_RUN_LOG")" \
    "systemd-run is actually invoked with the computed CPUQuota (#5111)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(cat "$SYSTEMD_RUN_LOG")" != *"RuntimeMaxSec"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no RuntimeMaxSec property is set when the wall-clock ceiling is left at its disabled default (#5111)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: no RuntimeMaxSec property is set when the wall-clock ceiling is left at its disabled default (#5111)"
fi

# Test: LOOM_SWEEP_RESERVED_CORES overrides the default reservation.
: > "$SYSTEMD_RUN_LOG"
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SWEEP_RESERVED_CORES=4 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "CPU budget = 4 core(s) of 8 (reserved 4)" "$output" \
    "LOOM_SWEEP_RESERVED_CORES overrides the default reservation (#5111)"
assert_contains "CPUQuota=400%" "$(cat "$SYSTEMD_RUN_LOG")" \
    "the overridden reservation is reflected in the enforced CPUQuota (#5111)"

# Test: a reservation >= total cores still yields at least a 1-core budget
# (never a zero/negative quota that would deadlock the sweep).
: > "$SYSTEMD_RUN_LOG"
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SWEEP_RESERVED_CORES=99 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "CPU budget = 1 core(s) of 8 (reserved 99)" "$output" \
    "an over-large reservation clamps the budget to a floor of 1 core, never 0 (#5111)"
assert_contains "CPUQuota=100%" "$(cat "$SYSTEMD_RUN_LOG")" \
    "the floor budget is reflected as CPUQuota=100% (#5111)"

# Test: LOOM_SWEEP_WALLCLOCK_CEILING_SECS adds a RuntimeMaxSec property to
# the same systemd-run invocation — the driver-level wall-clock bound, not
# just each leaf process's own timeout.
: > "$SYSTEMD_RUN_LOG"
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SWEEP_WALLCLOCK_CEILING_SECS=21600 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "RuntimeMaxSec=21600" "$(cat "$SYSTEMD_RUN_LOG")" \
    "LOOM_SWEEP_WALLCLOCK_CEILING_SECS adds a RuntimeMaxSec property when set (#5111)"
assert_contains "RuntimeMaxSec=21600s" "$output" \
    "spawn-claude logs the applied wall-clock ceiling (#5111)"

# Test: autonomous.spawnReservedCores / autonomous.spawnWallClockCeilingSecs
# config knobs are honored when no env override is set (env > config >
# default precedence, mirroring the #4233 niceness knobs).
CPU_CFG_WS="$(mktemp -d)"
mkdir -p "$CPU_CFG_WS/.loom/tokens"
chmod 700 "$CPU_CFG_WS/.loom/tokens"
echo -n "fake-token-cfg" > "$CPU_CFG_WS/.loom/tokens/alpha.token"
chmod 600 "$CPU_CFG_WS/.loom/tokens/alpha.token"
cat > "$CPU_CFG_WS/.loom/config.json" <<'EOF'
{ "autonomous": { "spawnReservedCores": 3, "spawnWallClockCeilingSecs": 7200 } }
EOF
: > "$SYSTEMD_RUN_LOG"
output=$(LOOM_WORKSPACE="$CPU_CFG_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "CPU budget = 5 core(s) of 8 (reserved 3)" "$output" \
    "autonomous.spawnReservedCores config knob is honored (#5111)"
assert_contains "RuntimeMaxSec=7200" "$(cat "$SYSTEMD_RUN_LOG")" \
    "autonomous.spawnWallClockCeilingSecs config knob is honored (#5111)"

# Test: an explicit LOOM_SWEEP_RESERVED_CORES env still wins over the config
# knob.
: > "$SYSTEMD_RUN_LOG"
output=$(LOOM_WORKSPACE="$CPU_CFG_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SWEEP_RESERVED_CORES=1 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "CPU budget = 7 core(s) of 8 (reserved 1)" "$output" \
    "LOOM_SWEEP_RESERVED_CORES env wins over autonomous.spawnReservedCores config (#5111)"
rm -rf "$CPU_CFG_WS"

# Test: LOOM_SWEEP_CPU_QUOTA=0 disables the entire mechanism — no budget
# export, no systemd-run wrap, byte-for-byte pre-#5111 behavior.
: > "$SYSTEMD_RUN_LOG"
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$CPU_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SWEEP_CPU_QUOTA=0 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "CPU quota mechanism disabled" "$output" \
    "LOOM_SWEEP_CPU_QUOTA=0 disables the mechanism (#5111)"
assert_contains "stub-claude budget=unset" "$output" \
    "LOOM_SWEEP_CPU_BUDGET_CORES is not exported when the mechanism is disabled (#5111)"
assert_eq "" "$(cat "$SYSTEMD_RUN_LOG")" \
    "systemd-run is never invoked when LOOM_SWEEP_CPU_QUOTA=0 (#5111)"

# Test: a failing systemd-run probe (e.g. the cpu controller isn't delegated
# to the user manager) degrades to advisory-only rather than replacing the
# process with a failing exec — the spawn must never be blocked by this.
FAIL_DIR="$(mktemp -d)"
cp "$CPU_DIR/claude" "$CPU_DIR/nproc" "$CPU_DIR/systemctl" "$FAIL_DIR/"
cat > "$FAIL_DIR/systemd-run" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$FAIL_DIR/systemd-run"
output=$(LOOM_WORKSPACE="$TEST_WS" LOOM_DAEMON_BIN="$DAEMON_BIN" PATH="$FAIL_DIR:$PATH" \
    LOOM_SYSTEMD_FORCE=1 \
    "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1 || true)
assert_contains "probe failed" "$output" \
    "a failing systemd-run probe is logged and degrades to advisory-only, not a hard failure (#5111)"
assert_contains "stub-claude budget=6" "$output" \
    "the spawn still completes (unwrapped) after a failed probe (#5111)"
rm -rf "$FAIL_DIR"

rm -rf "$CPU_DIR"

# ============================================================
# Section 8: claude-wrapper.sh `Execution error` retry + permanent-death
#            diagnostics (issue #4255)
#
# The Claude CLI's bare `Execution error` fatal was the single most common
# unattended-death signature (21% of daemon sweep logs) yet matched none of
# is_transient_error()'s patterns, so the wrapper died on attempt 1 without a
# retry. It is now recognized as transient (bounded by LOOM_MAX_RETRIES), and a
# permanent death emits a structured diagnostics block (exit code + classification
# + stderr tail) instead of only the bare string. Both are unit-tested by sourcing
# the wrapper with CLAUDE_WRAPPER_SOURCE_ONLY=1 (suppresses `main "$@"`).
# ============================================================

echo ""
echo "Testing claude-wrapper.sh Execution-error handling (#4255)..."

EE_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_WS" "$STUB_DIR" "$EE_DIR"' EXIT

# --- Unit: is_transient_error() now recognizes the bare `Execution error` ---
#
# #4501 note: the verdict is now DERIVED from `classify_error`'s category via the
# shared `classification_is_transient` deny-list, so an *unrecognized* non-zero
# exit (the RECOVERABLE catch-all) is retried instead of dying on attempt 1 —
# bounded by MAX_RETRIES. Genuinely terminal categories are still not retried, so
# this driver now pins those (MODEL_REFUSAL, CWD_DELETED, TOKEN_EXPIRED) as the
# non-transient side of the contract instead of an unrecognized string.
cat > "$EE_DIR/transient-driver.sh" <<'DRIVER'
#!/usr/bin/env bash
CLAUDE_WRAPPER_SOURCE_ONLY=1 source "$WRAPPER"
set +e
if is_transient_error "Execution error" 1; then echo "TRANSIENT=yes"; else echo "TRANSIENT=no"; fi
# An unrecognized non-zero exit classifies RECOVERABLE and is therefore retried
# (#4501) — the retry decision may never contradict the printed classification.
if is_transient_error "error: unknown option '--frobnicate'" 1; then
    echo "UNKNOWN=transient"
else
    echo "UNKNOWN=fatal"
fi
echo "UNKNOWN_CLASS=${_LAST_ERROR_CLASSIFICATION}"
# Terminal categories are still NOT retried.
for _out in 'stop_reason: "refusal"' "current working directory was deleted" "OAuth token has expired"; do
    if is_transient_error "$_out" 1; then
        echo "TERMINAL_${_LAST_ERROR_CLASSIFICATION}=transient"
    else
        echo "TERMINAL_${_LAST_ERROR_CLASSIFICATION}=fatal"
    fi
done
# A model-limit death is exhaustion (rotation), never a permanent death.
is_transient_error "You've reached your Fable 5 limit. Run /usage-credits to continue." 1
echo "MODEL_LIMIT_CLASS=${_LAST_ERROR_CLASSIFICATION}"
DRIVER
ee_transient=$(WRAPPER="$WRAPPER" bash "$EE_DIR/transient-driver.sh" 2>&1)
assert_contains "TRANSIENT=yes" "$ee_transient" \
    "is_transient_error treats bare 'Execution error' as transient (#4255)"
assert_contains "UNKNOWN=transient" "$ee_transient" \
    "is_transient_error retries an unrecognized non-zero exit (classification=RECOVERABLE) (#4501)"
assert_contains "UNKNOWN_CLASS=RECOVERABLE" "$ee_transient" \
    "is_transient_error derives its verdict from classify_error's category (#4501)"
assert_contains "TERMINAL_MODEL_REFUSAL=fatal" "$ee_transient" \
    "is_transient_error still refuses to retry MODEL_REFUSAL (#4501)"
assert_contains "TERMINAL_CWD_DELETED=fatal" "$ee_transient" \
    "is_transient_error still refuses to retry CWD_DELETED (#4501)"
assert_contains "TERMINAL_TOKEN_EXPIRED=fatal" "$ee_transient" \
    "is_transient_error still refuses to retry TOKEN_EXPIRED (#4501)"
assert_contains "MODEL_LIMIT_CLASS=TOKEN_EXHAUSTED" "$ee_transient" \
    "a model-limit death classifies TOKEN_EXHAUSTED, not the RECOVERABLE catch-all (#4501)"

# --- Unit: log_permanent_death() emits exit code + classification + tail ---
cat > "$EE_DIR/death-driver.sh" <<'DRIVER'
#!/usr/bin/env bash
CLAUDE_WRAPPER_SOURCE_ONLY=1 source "$WRAPPER"
set +e
log_permanent_death 42 "line one
line two
Execution error" "max retries (2) exceeded" 2>&1
DRIVER
ee_death=$(WRAPPER="$WRAPPER" bash "$EE_DIR/death-driver.sh" 2>&1)
assert_contains "permanent death (max retries (2) exceeded)" "$ee_death" \
    "log_permanent_death labels the reason (#4255)"
assert_contains "exit_code=42" "$ee_death" \
    "log_permanent_death records the exit code (#4255)"
assert_contains "classification=" "$ee_death" \
    "log_permanent_death records the classify-error verdict (#4255)"
assert_contains "Execution error" "$ee_death" \
    "log_permanent_death includes the stderr/stdout tail (#4255)"

# --- Integration: a stub that always prints `Execution error` retries up to
#     LOOM_MAX_RETRIES, then dies permanently with the diagnostics block. ---
if [[ -z "$DAEMON_BIN" ]]; then
    echo "  (skipping Execution-error retry integration test — loom-daemon binary not found)"
else
  EE_WS="$(mktemp -d)"
  mkdir -p "$EE_WS/.loom/tokens"
  chmod 700 "$EE_WS/.loom/tokens"
  printf '%s' "tok-solo" > "$EE_WS/.loom/tokens/solo.token"
  chmod 600 "$EE_WS/.loom/tokens/solo.token"

  EE_STUB="$(mktemp -d)"
  cat > "$EE_STUB/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
echo "Execution error"
exit 1
STUB
  chmod +x "$EE_STUB/claude"

  set +e
  ee_out=$(
    LOOM_WORKSPACE="$EE_WS" \
    LOOM_TOKEN_NAME="solo" \
    CLAUDE_CODE_OAUTH_TOKEN="tok-solo" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" \
    LOOM_MAX_RETRIES=2 \
    LOOM_INITIAL_WAIT=0 \
    LOOM_SHEPHERD_TASK_ID="test-execution-error" \
    LOOM_STARTUP_MONITOR_WINDOW=1 \
    PATH="$EE_STUB:$PATH" \
    bash "$WRAPPER" -p "ping" 2>&1
  )
  ee_rc=$?
  set -e

  # #4501: the retry log line now names the derived classification instead of a
  # matched pattern from the (deleted) local pattern array.
  assert_contains "Transient error (classification=RECOVERABLE) - retrying" "$ee_out" \
      "wrapper retries on the bare 'Execution error' output (#4255/#4501)"
  assert_contains "Max retries (2) exceeded" "$ee_out" \
      "wrapper stops after LOOM_MAX_RETRIES attempts (#4255)"
  assert_contains "permanent death" "$ee_out" \
      "wrapper emits the permanent-death diagnostics block on final failure (#4255)"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$ee_rc" -ne 0 ]]; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: wrapper exits non-zero after exhausting retries on Execution error"
  else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: wrapper exits non-zero after exhausting retries on Execution error"
  fi

  rm -rf "$EE_WS" "$EE_STUB"
fi

rm -rf "$EE_DIR"

# ============================================================
# Section 9: per-model limit deaths rotate instead of dying (issue #4501)
#
# The CLI's per-model ceiling ("You've reached your Fable 5 limit. Run
# /usage-credits to continue or switch models with /model.") matched none of the
# pre-#4501 exhaustion phrasings, so every daemon-dispatched child died in ~2s
# with the self-contradicting block:
#
#   [ERROR] Non-transient error detected - not retrying
#   [ERROR] exit_code=1 classification=RECOVERABLE
#
# It must now rotate accounts WITHOUT consuming a MAX_RETRIES attempt, stay
# bounded (whole-pool exhaustion still terminates with ACCOUNT_POOL_EXHAUSTED),
# and never emit the "non-transient" + RECOVERABLE contradiction.
# ============================================================

echo ""
echo "Testing claude-wrapper.sh model-limit rotation (#4501)..."

if [[ -z "$DAEMON_BIN" ]]; then
    echo "  (skipping model-limit rotation tests — loom-daemon binary not found)"
else
  # --- Rotation: alpha hits the Fable ceiling, beta succeeds ---
  ML_WS="$(mktemp -d)"
  mkdir -p "$ML_WS/.loom/tokens"
  chmod 700 "$ML_WS/.loom/tokens"
  printf '%s' "tok-alpha" > "$ML_WS/.loom/tokens/alpha.token"
  printf '%s' "tok-beta"  > "$ML_WS/.loom/tokens/beta.token"
  chmod 600 "$ML_WS/.loom/tokens/"*.token

  ML_STUB="$(mktemp -d)"
  cat > "$ML_STUB/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
if [[ "${CLAUDE_CODE_OAUTH_TOKEN}" == "tok-alpha" ]]; then
    echo "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model."
    exit 1
fi
echo "stub-claude success on token=${CLAUDE_CODE_OAUTH_TOKEN}"
exit 0
STUB
  chmod +x "$ML_STUB/claude"

  # MAX_RETRIES=1: if rotation consumed an attempt, beta would never be tried.
  set +e
  ml_out=$(
    LOOM_WORKSPACE="$ML_WS" \
    LOOM_TOKEN_NAME="alpha" \
    CLAUDE_CODE_OAUTH_TOKEN="tok-alpha" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" \
    LOOM_MAX_RETRIES=1 \
    LOOM_SHEPHERD_TASK_ID="test-model-limit" \
    LOOM_STARTUP_MONITOR_WINDOW=1 \
    PATH="$ML_STUB:$PATH" \
    bash "$WRAPPER" -p "ping" 2>&1
  )
  ml_rc=$?
  set -e

  assert_contains "stub-claude success on token=tok-beta" "$ml_out" \
      "wrapper rotates off a per-model limit death and succeeds on the next account (#4501)"
  assert_eq "0" "$ml_rc" \
      "wrapper exits 0 after model-limit rotation (MAX_RETRIES=1 not consumed) (#4501)"
  assert_contains "reached your Fable 5 limit" "$ml_out" \
      "rotation log names the per-model phrase that fired (#4501)"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$ml_out" != *"permanent death"* && "$ml_out" != *"Non-transient error detected"* ]]; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: a per-model limit death never logs permanent death / 'non-transient' (#4501)"
  else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: a per-model limit death never logs permanent death / 'non-transient' (#4501)"
      echo "    In: '$ml_out'"
  fi

  # --- Bounded: a single-account pool still terminates (no infinite rotation) ---
  ML_WS2="$(mktemp -d)"
  mkdir -p "$ML_WS2/.loom/tokens"
  chmod 700 "$ML_WS2/.loom/tokens"
  printf '%s' "tok-solo" > "$ML_WS2/.loom/tokens/solo.token"
  chmod 600 "$ML_WS2/.loom/tokens/solo.token"

  ML_STUB2="$(mktemp -d)"
  cat > "$ML_STUB2/claude" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" -p "*) ;;
  *) exit 0 ;;
esac
echo "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model."
exit 1
STUB
  chmod +x "$ML_STUB2/claude"

  set +e
  ml2_out=$(
    LOOM_WORKSPACE="$ML_WS2" \
    LOOM_TOKEN_NAME="solo" \
    CLAUDE_CODE_OAUTH_TOKEN="tok-solo" \
    LOOM_DAEMON_BIN="$DAEMON_BIN" \
    LOOM_MAX_RETRIES=1 \
    LOOM_SHEPHERD_TASK_ID="test-model-limit" \
    LOOM_STARTUP_MONITOR_WINDOW=1 \
    PATH="$ML_STUB2:$PATH" \
    bash "$WRAPPER" -p "ping" 2>&1
  )
  ml2_rc=$?
  set -e

  assert_contains "ACCOUNT_POOL_EXHAUSTED" "$ml2_out" \
      "model-limit rotation stays bounded: whole-pool exhaustion still terminates (#4501)"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$ml2_rc" -ne 0 ]]; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: model-limit whole-pool exhaustion exits non-zero (#4501)"
  else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: model-limit whole-pool exhaustion exits non-zero (#4501)"
  fi

  # --- The contradiction itself: "Non-transient" and classification=RECOVERABLE
  #     can never appear together in a wrapper run. ---
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ ( "$ml_out" != *"Non-transient error detected"* || "$ml_out" != *"classification=RECOVERABLE"* ) \
     && ( "$ml2_out" != *"Non-transient error detected"* || "$ml2_out" != *"classification=RECOVERABLE"* ) ]]; then
      TESTS_PASSED=$((TESTS_PASSED + 1))
      echo -e "  ${GREEN}PASS${NC}: no run pairs 'Non-transient error detected' with classification=RECOVERABLE (#4501)"
  else
      TESTS_FAILED=$((TESTS_FAILED + 1))
      echo -e "  ${RED}FAIL${NC}: no run pairs 'Non-transient error detected' with classification=RECOVERABLE (#4501)"
  fi

  rm -rf "$ML_WS" "$ML_STUB" "$ML_WS2" "$ML_STUB2"
fi

# --- Unit: the "non-transient" log line always names the classification it
#     acted on, so the two log lines can never disagree (#4501). ---
NT_DIR="$(mktemp -d)"
cat > "$NT_DIR/non-transient-driver.sh" <<'DRIVER'
#!/usr/bin/env bash
CLAUDE_WRAPPER_SOURCE_ONLY=1 source "$WRAPPER"
set +e
# MODEL_REFUSAL is terminal: the wrapper's message and log_permanent_death's
# classification must name the SAME category.
is_transient_error 'stop_reason: "refusal"' 1
echo "MSG=Non-transient error detected (classification=${_LAST_ERROR_CLASSIFICATION}) - not retrying"
log_permanent_death 1 'stop_reason: "refusal"' "non-transient error" 2>&1
# The wrapper's own RATE_LIMIT_ABORT sentinel stays non-transient, and the death
# block reports the SAME verdict the retry decision used (a fresh classify_error
# would report the generic RECOVERABLE for this text).
if is_transient_error "# RATE_LIMIT_ABORT" 1; then echo "ABORT=transient"; else echo "ABORT=fatal"; fi
log_permanent_death 1 "# RATE_LIMIT_ABORT" "non-transient error" 2>&1
DRIVER
nt_out=$(WRAPPER="$WRAPPER" bash "$NT_DIR/non-transient-driver.sh" 2>&1)
assert_contains "MSG=Non-transient error detected (classification=MODEL_REFUSAL) - not retrying" "$nt_out" \
    "the 'non-transient' log line names the derived classification (#4501)"
assert_contains "classification=MODEL_REFUSAL" "$nt_out" \
    "log_permanent_death agrees with the retry decision's classification (#4501)"
assert_contains "ABORT=fatal" "$nt_out" \
    "the RATE_LIMIT_ABORT sentinel path is unchanged: still non-transient (#4501)"
assert_contains "classification=RATE_LIMIT_ABORT" "$nt_out" \
    "log_permanent_death reuses the derived verdict, not a second classification (#4501)"
rm -rf "$NT_DIR"

# ============================================================
# Summary
# ============================================================

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
