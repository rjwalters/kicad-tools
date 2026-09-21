#!/usr/bin/env bash
# test-claude-wrapper-retry.sh - characterization of claude-wrapper.sh's retry
# policy (#8032).
#
# WHY THIS EXISTS, AND WHY IT IS NOT A PORT
#
# `claude-wrapper.sh` is 1,675 code lines and had NO dedicated test suite.
# .loom/docs/file-size-policy.md names it as the one port target whose logic
# exists only in shell: "retry/backoff lives ONLY here -- the retry policy
# itself, which is application logic".
#
# Every port epic #7810 has landed used one method: keep the script's black-box
# suite and run its assertions unchanged against the Rust, as the equivalence
# proof. That method caught four real defects that review alone did not. Here
# there was no suite to keep, so a port would have had no equivalence evidence
# at all -- exactly the condition those defects would have shipped under.
#
# So this suite pins what the CURRENT SHELL decides, and must pass against the
# unmodified script. It is the evidence a later port will be checked against;
# it is not itself a step of that port.
#
# THE SURFACE IT COVERS
#
# 79 code lines across six functions decide whether a sweep retries, rotates or
# dies. The other ~1,600 lines are preflight, MCP repair, output monitoring and
# process plumbing, and are deliberately out of scope.
#
# EVERY CLASSIFIER HAS TWO MODES, AND BOTH ARE PINNED
#
# `lib/classify-error.sh` is sourced OPTIONALLY (claude-wrapper.sh:140). When it
# is present the classifiers delegate to `classify_error`; when it is not, each
# falls back to its own hand-rolled regex. Those two paths can drift, and a port
# that reproduced only the library path would silently change what happens on a
# host where the library is missing. Both are covered.
#
# Hermetic: sources the wrapper with CLAUDE_WRAPPER_SOURCE_ONLY=1 (the existing
# #4192 seam -- no modification to the script was needed). No network, no live
# forge, no token pool, no subprocess spawn.
#
# Usage:
#   ./.loom/scripts/tests/test-claude-wrapper-retry.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="$SCRIPTS_DIR/claude-wrapper.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# assert_pred <fn> <expect 0|1> <output> <exit_code> <msg>
assert_pred() {
    local fn="$1" want="$2" out="$3" rc="$4" msg="$5" got=0
    "$fn" "$out" "$rc" >/dev/null 2>&1 || got=$?
    [[ "$got" -ne 0 ]] && got=1
    if [[ "$got" == "$want" ]]; then
        pass "$msg"
    else
        fail "$msg (wanted $want, got $got)"
    fi
}

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

[[ -r "$WRAPPER" ]] || { echo "FATAL: $WRAPPER not readable" >&2; exit 2; }

# The #4192 seam: reach the function definitions without running main.
# shellcheck source=/dev/null
CLAUDE_WRAPPER_SOURCE_ONLY=1 source "$WRAPPER" >/dev/null 2>&1

for fn in is_transient_error is_account_exhaustion is_account_auth_dead \
          is_account_session_limit is_mcp_error calculate_wait_time; do
    if ! declare -F "$fn" >/dev/null 2>&1; then
        echo "FATAL: sourcing the wrapper did not define $fn — the #4192 seam moved." >&2
        exit 2
    fi
done

# Sanity: the library path is the one under test first. If this is absent the
# whole "library mode" half below would silently be testing the fallback.
if ! declare -F classify_error >/dev/null 2>&1; then
    echo "FATAL: classify_error is not defined after sourcing the wrapper;" >&2
    echo "       lib/classify-error.sh did not load, so 'library mode' below would be a lie." >&2
    exit 2
fi

echo "=== library mode (lib/classify-error.sh sourced — the normal case) ==="

echo
echo "--- is_transient_error ---"
# The wrapper's OWN sentinel, emitted by its output/startup monitors — not a CLI
# phrasing. Checked before classification, and deliberately NOT transient: the
# CLI hit a usage limit and showed an interactive prompt, so retrying hits the
# same limit. Rotation consumes it first; reaching here means rotation was
# capped or the pool was empty.
assert_pred is_transient_error 1 "RATE_LIMIT_ABORT" 1 \
    "RATE_LIMIT_ABORT is NOT transient (retrying would hit the same limit)"
assert_pred is_transient_error 0 "connection reset by peer" 1 \
    "a recoverable network error is transient"
assert_pred is_transient_error 1 "" 0 \
    "exit 0 is SUCCESS, which is terminal — there is nothing to retry"

echo
echo "--- is_account_exhaustion: rotate to another account ---"
assert_pred is_account_exhaustion 0 "RATE_LIMIT_ABORT" 1 \
    "RATE_LIMIT_ABORT IS exhaustion (rotate), the mirror of it not being transient"
assert_pred is_account_exhaustion 1 "connection reset by peer" 1 \
    "an ordinary transient error is not exhaustion"

echo
echo "--- is_account_auth_dead: rotate AND mark the account dead ---"
# The distinction that matters: exhaustion recovers with time, auth-death does
# not. Collapsing them either burns healthy accounts or keeps retrying a dead
# one.
assert_pred is_account_auth_dead 1 "connection reset by peer" 1 \
    "a transient error does not mark an account dead"

echo
echo "--- is_mcp_error: attempt an MCP rebuild ---"
# No classify_error path and no exit code: a pure pattern match on output.
assert_pred is_mcp_error 0 "MCP server failed to start" 1 "an MCP server failure is an MCP error"
assert_pred is_mcp_error 0 "plugins failed" 1 "a plugin failure is an MCP error"
assert_pred is_mcp_error 1 "connection reset by peer" 1 "an ordinary error is not an MCP error"
assert_pred is_mcp_error 1 "" 0 "empty output is not an MCP error"
# It ignores the exit code entirely — pinned because a port would naturally add
# one, and that would change behaviour on a zero-exit run whose output mentions
# MCP.
assert_pred is_mcp_error 0 "MCP server failed" 0 \
    "is_mcp_error ignores the exit code (matches on output alone)"

echo
echo "--- calculate_wait_time: the backoff curve ---"
# Pinned exactly, including the ceiling. A port that "improves" the curve
# changes how hard a rate-limited fleet hammers the API.
assert_eq "60"   "$(LOOM_INITIAL_WAIT=60 MAX_WAIT=1800 INITIAL_WAIT=60 MULTIPLIER=2 calculate_wait_time 1)" \
    "attempt 1 waits INITIAL_WAIT (60s)"
assert_eq "120"  "$(INITIAL_WAIT=60 MULTIPLIER=2 MAX_WAIT=1800 calculate_wait_time 2)" "attempt 2 doubles to 120s"
assert_eq "240"  "$(INITIAL_WAIT=60 MULTIPLIER=2 MAX_WAIT=1800 calculate_wait_time 3)" "attempt 3 doubles to 240s"
assert_eq "480"  "$(INITIAL_WAIT=60 MULTIPLIER=2 MAX_WAIT=1800 calculate_wait_time 4)" "attempt 4 doubles to 480s"
assert_eq "960"  "$(INITIAL_WAIT=60 MULTIPLIER=2 MAX_WAIT=1800 calculate_wait_time 5)" "attempt 5 doubles to 960s"
assert_eq "1800" "$(INITIAL_WAIT=60 MULTIPLIER=2 MAX_WAIT=1800 calculate_wait_time 6)" \
    "attempt 6 would be 1920s but is CAPPED at MAX_WAIT (1800s)"
assert_eq "1800" "$(INITIAL_WAIT=60 MULTIPLIER=2 MAX_WAIT=1800 calculate_wait_time 20)" \
    "the cap holds for any later attempt (no overflow past MAX_WAIT)"

echo
echo "=== degraded mode (lib/classify-error.sh NOT sourced) ==="
#
# claude-wrapper.sh:140 sources the library OPTIONALLY — `if [[ -f ... ]]`. On a
# host where it is missing, every classifier falls back to its own hand-rolled
# regex, and those regexes have never been tested. They are the half most likely
# to drift from the library, and a port that reproduced only the library path
# would silently change behaviour exactly where there is least coverage.
#
# Dropping the definitions is what the classifiers actually branch on
# (`declare -F classify_error`), so this reaches the real fallback rather than a
# simulation of it.
unset -f classify_error classification_is_transient

if declare -F classify_error >/dev/null 2>&1; then
    echo "FATAL: classify_error survived the unset; the degraded half would not be testing the fallback." >&2
    exit 2
fi

echo
echo "--- is_transient_error: degraded is RETRY-BY-DEFAULT ---"
# The deliberate fail-safe direction: with no classifier available, any non-zero
# exit retries (bounded by MAX_RETRIES) rather than dying. A port must not
# "improve" this into a deny-list, or a host missing the library stops retrying
# recoverable failures entirely.
assert_pred is_transient_error 0 "something nobody has a pattern for" 1 \
    "an UNRECOGNISED error retries when the classifier is unavailable"
assert_pred is_transient_error 1 "anything at all" 0 \
    "exit 0 is still not transient, even degraded"
assert_pred is_transient_error 1 "RATE_LIMIT_ABORT" 1 \
    "the RATE_LIMIT_ABORT sentinel still wins, before the degraded branch"

echo
echo "--- is_account_exhaustion: the fallback regex ---"
assert_pred is_account_exhaustion 0 "You have hit your weekly limit" 1 "\"hit your <n> limit\" is exhaustion"
assert_pred is_account_exhaustion 0 "monthly usage limit reached" 1 "\"monthly usage limit\" is exhaustion"
assert_pred is_account_exhaustion 0 "You are out of extra usage" 1 "\"out of extra usage\" is exhaustion"
assert_pred is_account_exhaustion 0 "ran out of credits" 1 "\"ran out of credits\" is exhaustion"
assert_pred is_account_exhaustion 0 "no plan credits remaining" 1 "\"no plan credits remaining\" is exhaustion"
assert_pred is_account_exhaustion 0 "insufficient usage credits" 1 "\"insufficient usage credits\" is exhaustion"
# The exit-code conjunction: the regex alone must not fire on a SUCCESSFUL run
# whose output merely quotes a limit phrase (a sweep summarising its own logs).
assert_pred is_account_exhaustion 1 "You have hit your weekly limit" 0 \
    "a limit phrase on a ZERO exit is not exhaustion (the exit code is conjoined)"
assert_pred is_account_exhaustion 1 "connection reset by peer" 1 "an ordinary error is not exhaustion"

echo
echo "--- is_account_auth_dead: the fallback regex ---"
assert_pred is_account_auth_dead 0 '401 authentication_error' 1 "a 401 authentication_error is auth-death"
assert_pred is_account_auth_dead 0 '"type": "authentication_error"' 1 "the JSON authentication_error shape is auth-death"
assert_pred is_account_auth_dead 0 "token has been revoked" 1 "a revoked token is auth-death"
assert_pred is_account_auth_dead 0 "invalid bearer token" 1 "an invalid bearer token is auth-death"
assert_pred is_account_auth_dead 0 "OAuth token has expired" 1 "an expired OAuth token is auth-death"
assert_pred is_account_auth_dead 1 "You have hit your weekly limit" 1 \
    "EXHAUSTION IS NOT AUTH-DEATH — exhaustion recovers with time, auth-death does not"
assert_pred is_account_auth_dead 1 "401 authentication_error" 0 \
    "a 401 phrase on a ZERO exit is not auth-death (the exit code is conjoined)"

echo
echo "--- is_account_session_limit: the fallback regex ---"
assert_pred is_account_session_limit 0 "maximum number of concurrent sessions" 1 "\"maximum number of concurrent\" is a session limit"
assert_pred is_account_session_limit 0 "too many concurrent requests" 1 "\"too many concurrent\" is a session limit"
assert_pred is_account_session_limit 0 "another session is already active" 1 "\"another session is active\" is a session limit"
assert_pred is_account_session_limit 1 "You have hit your weekly limit" 1 \
    "A WEEKLY LIMIT IS NOT A SESSION LIMIT — one clears in minutes, the other in days"
assert_pred is_account_session_limit 1 "connection reset by peer" 1 "an ordinary error is not a session limit"

echo
echo "--- the three account predicates are mutually exclusive on their own phrases ---"
# Each drives a different remedy: wait-and-retry, rotate, or rotate-and-mark-dead.
# Any overlap would send the wrong remedy, so the disjointness is pinned rather
# than assumed.
_excl() { # <phrase> <expected: "e d s" triple> <label>
    local phrase="$1" want="$2" label="$3" e=0 d=0 s=0
    is_account_exhaustion    "$phrase" 1 >/dev/null 2>&1 && e=1
    is_account_auth_dead     "$phrase" 1 >/dev/null 2>&1 && d=1
    is_account_session_limit "$phrase" 1 >/dev/null 2>&1 && s=1
    assert_eq "$want" "$e $d $s" "'$phrase' classifies ONLY as $label"
}
_excl "You have hit your weekly limit"        "1 0 0" "exhaustion"
_excl "OAuth token has expired"               "0 1 0" "auth-dead"
_excl "maximum number of concurrent sessions" "0 0 1" "session-limit"

echo
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
