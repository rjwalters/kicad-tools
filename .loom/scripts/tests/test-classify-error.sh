#!/usr/bin/env bash
# test-classify-error.sh - Unit tests for lib/classify-error.sh (#5631)
#
# Table-driven coverage of `classify_error`'s TOKEN_EXHAUSTED / SESSION_LIMIT /
# MODEL_CREDITS_EXHAUSTED regexes against known Claude CLI limit strings, per
# the "Suggested test" spec in issue #5631: session, weekly, monthly usage,
# monthly spend, per-model, plus the SESSION_LIMIT precedence edge case that
# motivated the ordering in the first place (#3947) and the credit-exhaustion
# class added in #5687.
#
# Usage:
#   ./.loom/scripts/tests/test-classify-error.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SRC="$SCRIPTS_DIR/lib/classify-error.sh"

# The suite's heredoc tables assert the DEFAULT provider chain (2-arg
# `classify_error` calls, explicit provider only where noted). A host that
# exports LOOM_RUNTIME for its own dispatch (every Loom worker does) would
# otherwise select that provider for every 2-arg call and fail the whole
# table — the env var is the caller's knob, not the suite's.
unset LOOM_RUNTIME

# shellcheck source=../lib/classify-error.sh
source "$SRC"

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

# The pattern tables live only in `loom-daemon classify-error` (#11219), so the
# vector sections below run through the library's native delegation and are
# exercisable only when a capable binary resolved. With none, they are a
# counted skip and the degraded-answer section at the bottom still runs.
if [[ -z "${_CLASSIFY_DAEMON_BIN:-}" ]]; then
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no loom-daemon with classify-error resolved; pattern vectors skipped (degraded answers asserted below)"
    echo "mode: degraded"
else
echo "mode: native delegation via $_CLASSIFY_DAEMON_BIN"

echo "--- classify_error: TOKEN_EXHAUSTED phrase family (#5631) ---"

# Table: description | CLI output | expected category
while IFS='|' read -r desc output expected; do
    [[ -z "$desc" ]] && continue
    actual="$(classify_error "$output" 1)"
    assert_eq "$expected" "$actual" "$desc"
done <<'EOF'
session limit|You've hit your session limit|TOKEN_EXHAUSTED
weekly limit|You've hit your weekly limit|TOKEN_EXHAUSTED
monthly usage limit|You've hit your monthly usage limit|TOKEN_EXHAUSTED
monthly spend limit (issue #5631 regression)|You've hit your monthly spend limit - raise it at claude.ai/settings/usage|TOKEN_EXHAUSTED
bare limit, no filler words|You've hit your limit|TOKEN_EXHAUSTED
per-model ceiling (#4501)|You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model.|TOKEN_EXHAUSTED
out of extra usage|You are out of extra usage for this billing period|TOKEN_EXHAUSTED
used 100% of weekly limit|You have used 100% of your weekly limit|RECOVERABLE
EOF

echo
echo "--- classify_error: SESSION_LIMIT precedence is unaffected by the widened TOKEN_EXHAUSTED regex (#3947) ---"

assert_eq "SESSION_LIMIT" "$(classify_error "maximum number of concurrent sessions reached" 1)" \
    "concurrent-session capacity fault stays SESSION_LIMIT, not TOKEN_EXHAUSTED"
assert_eq "SESSION_LIMIT" "$(classify_error "Another session is already active" 1)" \
    "'another session is already active' stays SESSION_LIMIT"

echo
echo "--- classify_error: MODEL_CREDITS_EXHAUSTED, the per-model-tier credit class (#5687) ---"

# The incident wording (2026-08-08, wave-width-6 /loom:sweep all) plus the
# nearby phrasings, and — critically — the NEGATIVE cases that must NOT be
# swallowed by the new branch.
while IFS='|' read -r desc output expected; do
    [[ -z "$desc" ]] && continue
    actual="$(classify_error "$output" 1)"
    assert_eq "$expected" "$actual" "$desc"
done <<'EOF'
the observed incident wording|You're out of usage credits. Run /usage-credits or switch models with /model.|MODEL_CREDITS_EXHAUSTED
bare "out of credits"|You are out of credits|MODEL_CREDITS_EXHAUSTED
"ran out of usage credits"|The session ran out of usage credits|MODEL_CREDITS_EXHAUSTED
"no usage credits remaining"|Your account has no usage credits remaining|MODEL_CREDITS_EXHAUSTED
"insufficient credits"|Request failed: insufficient credits for this model|MODEL_CREDITS_EXHAUSTED
case-insensitive|YOU'RE OUT OF USAGE CREDITS|MODEL_CREDITS_EXHAUSTED
negative: unrelated "credit" mention|Payment failed: your credit card was declined|RECOVERABLE
negative: crediting an author|Please credit the original author in the commit message|RECOVERABLE
negative: "credits" with no exhaustion verb|Remaining usage credits: 4200|RECOVERABLE
EOF

echo
echo "--- classify_error: #5687's branch must NOT steal any pre-existing vector (order: after TOKEN_EXHAUSTED) ---"

# #4501's live per-model ceiling mentions /usage-credits too. It predates
# #5687 and must keep TOKEN_EXHAUSTED, which is only true because the new
# branch is checked AFTER the exhaustion regex.
assert_eq "TOKEN_EXHAUSTED" \
    "$(classify_error "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model." 1)" \
    "#4501 per-model ceiling still TOKEN_EXHAUSTED despite mentioning credits"
assert_eq "TOKEN_EXHAUSTED" \
    "$(classify_error "You've hit your weekly limit. You are out of credits." 1)" \
    "a mixed message keeps the older TOKEN_EXHAUSTED classification (order is load-bearing)"
# Exit-code-first (#3233) applies to the new branch like every other.
assert_eq "SUCCESS" "$(classify_error "You're out of usage credits" 0)" \
    "a CLEAN exit whose output mentions credit exhaustion stays SUCCESS (#3233)"
# The capacity fault still wins over both exhaustion classes (#3947).
assert_eq "SESSION_LIMIT" \
    "$(classify_error "maximum number of concurrent sessions reached; you are out of usage credits" 1)" \
    "SESSION_LIMIT still precedes the credit class"

echo
echo "--- classify_error: TOKEN_EXPIRED covers a REVOKED OAuth token, incl. the JSON envelope (#6614) ---"

# The verbatim death-tail from the incident in #6614: an operator's `/login` on
# the host revoked the pooled credential mid-flight. Before the fix, the JSON
# envelope defeated the `401[^a-z]*authentication_error` pattern (the `[^a-z]*`
# gap cannot cross `{"type":"`), nothing matched "revoked", and the death fell
# through to the RECOVERABLE catch-all — so claude-wrapper.sh retried the same
# revoked credential MAX_RETRIES (5) times instead of marking it bad + rotating.
while IFS='|' read -r desc output expected; do
    [[ -z "$desc" ]] && continue
    actual="$(classify_error "$output" 1)"
    assert_eq "$expected" "$actual" "$desc"
done <<'EOF'
the verbatim incident wording (#6614)|Failed to authenticate. API Error: 401 {"type":"authentication_error","message":"OAuth access token has been revoked."}|TOKEN_EXPIRED
nested error envelope the API also serves|API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"OAuth access token has been revoked."}}|TOKEN_EXPIRED
prose form, no JSON at all|Failed to authenticate. API Error: 401 OAuth access token has been revoked|TOKEN_EXPIRED
exact #10294 message, no auxiliary verb|Failed to authenticate: OAuth token revoked. Please log in again or contact your administrator.|TOKEN_EXPIRED
past-tense variant|Your access token was revoked|TOKEN_EXPIRED
JSON authentication_error with some OTHER message|{"type":"authentication_error","message":"Invalid API key"}|TOKEN_EXPIRED
pre-#6614 plain form still classifies|API Error: 401 authentication_error|TOKEN_EXPIRED
negative: unrelated revoked word without token|Your session was revoked by the admin|RECOVERABLE
negative: a revoked thing that is not a token|The reviewer revoked their approval and the ruleset was revoked|RECOVERABLE
EOF

# The exit-code-first guarantee (#3233) for the new phrasing, checked with the
# real exit code rather than smuggling it through the table above.
assert_eq "SUCCESS" \
    "$(classify_error 'API Error: 401 {"type":"authentication_error","message":"OAuth access token has been revoked."}' 0)" \
    "a CLEAN exit whose output quotes the revoked-token 401 stays SUCCESS (#3233)"

assert_eq "SUCCESS" \
    "$(classify_error 'Failed to authenticate: OAuth token revoked.' 0)" \
    "zero exit quoting the no-verb revoked text stays SUCCESS (#10294)"

# TOKEN_EXPIRED must be FATAL, not transient: that is what makes
# claude-wrapper.sh mark the account bad and rotate instead of retrying the
# same dead credential (the 5x retry loop reported in #6614).
TESTS_RUN=$((TESTS_RUN + 1))
if classification_is_transient TOKEN_EXPIRED; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: TOKEN_EXPIRED is NOT transient (no retry against the same revoked token)"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: TOKEN_EXPIRED is NOT transient (no retry against the same revoked token)"
fi

echo
echo "--- classify_error: Kimi Code CLI (#8561) needs no provider table; the generic transients already cover it ---"

# Issue #8561 was filed against a documented Kimi print-mode contract of
# "75 = transient (rate limit / 5xx / timeout)". Probing the pinned CLI
# (@moonshot-ai/kimi-code 2.0.2) disproved it: 75 is emitted nowhere in the
# shipped bundle. Kimi absorbs transients IN-PROCESS — a 429 is retried up to
# ten times with exponential backoff — and only the exhaustion of that ladder
# reaches the process boundary, as exit 1. Full probe evidence:
# docs/experiments/kimi-harness-probe-2026-09-22.json.
#
# So no `_classify_error_kimi` table was added and no 75 special case exists.
# These assertions pin WHY that is safe rather than an oversight: the strings
# Kimi actually emits already land on RECOVERABLE through the generic,
# provider-independent table, which is precisely the retry-with-backoff verdict
# the phantom 75 mapping was meant to produce. If someone later adds a kimi
# table, these must keep passing.
while IFS='|' read -r desc output expected; do
    [[ -z "$desc" ]] && continue
    actual="$(classify_error "$output" 1 kimi)"
    assert_eq "$expected" "$actual" "$desc"
done <<'EOF'
429 retry-ladder exhaustion, verbatim from 2.0.2|error: failed to run prompt: provider.rate_limit: 429 rate limit exceeded|RECOVERABLE
turn.step.retrying NDJSON meta event, verbatim from 2.0.2|{"role":"meta","type":"turn.step.retrying","failed_attempt":9,"next_attempt":10,"max_attempts":10,"error_name":"APIProviderRateLimitError","error_message":"429 rate limit exceeded","status_code":429}|RECOVERABLE
unrecognised Kimi config fault falls through to the daemon-mode catch-all|error: failed to run prompt: No model configured. Run `kimi` and use /login to sign in.|RECOVERABLE
EOF

# An unknown provider selector must never make classification FAIL — "kimi"
# matches no provider table by design, and the exit-code-first
# rules still apply ahead of any output inspection.
assert_eq "SUCCESS" "$(classify_error 'provider.rate_limit: 429 rate limit exceeded' 0 kimi)" \
    "a CLEAN Kimi exit stays SUCCESS even when its stream quoted a 429 (#3233)"
assert_eq "TIMEOUT" "$(classify_error '' 124 kimi)" \
    "timeout(1) on a Kimi launch is provider-independent TIMEOUT"

TESTS_RUN=$((TESTS_RUN + 1))
if classification_is_transient "$(classify_error 'error: failed to run prompt: provider.rate_limit: 429 rate limit exceeded' 1 kimi)"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Kimi's rate-limit exhaustion is retryable (the verdict the phantom exit-75 mapping wanted)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Kimi's rate-limit exhaustion is retryable (the verdict the phantom exit-75 mapping wanted)"
fi

echo
echo "--- classification_is_transient: TOKEN_EXHAUSTED stays retryable (rotation path consumes it first) ---"

for _category in TOKEN_EXHAUSTED MODEL_CREDITS_EXHAUSTED; do
    if classification_is_transient "$_category"; then
        TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $_category is transient (retry/rotate, not fatal)"
    else
        TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $_category is transient (retry/rotate, not fatal)"
    fi
done

echo
echo "--- classify_error: verbatim Claude Code messages (#11205), from fixtures/classify_error/vectors.tsv ---"

# A TSV file, not a heredoc table: several of these strings contain `|`.
VECTORS="$TEST_DIR/fixtures/classify_error/vectors.tsv"
vectors_run=0
while IFS=$'\t' read -r expected exit_code provider desc output; do
    [[ -z "$expected" || "$expected" == \#* ]] && continue
    vectors_run=$((vectors_run + 1))
    assert_eq "$expected" "$(classify_error "$output" "$exit_code" "$provider")" "$desc"
done < "$VECTORS"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$vectors_run" -gt 0 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: read $vectors_run vectors from $VECTORS"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: no vectors read from $VECTORS"
fi

echo
echo '--- native `loom-daemon classify-error` direct checks (#11208) ---'

# The fixture-driven vectors above already ran through the library's native
# delegation. This section drives the subcommand itself: engine short-circuits,
# the --transient exit-code contract, $LOOM_RUNTIME, --stdout-tail and usage
# errors. The shell tables it used to be diffed against were deleted (#11219).
parity_bin="$_CLASSIFY_DAEMON_BIN"
{
    native_classify() {
        printf '%s' "$1" | "$parity_bin" classify-error --exit-code "$2" --provider "$3" 2>/dev/null
    }

    # Engine short-circuits, straight through the subcommand.
    assert_eq "TIMEOUT" "$(native_classify "hit your weekly limit" 124 claude)" \
        "native: exit 124 is TIMEOUT before any pattern (#3233 engine)"
    assert_eq "TIMEOUT" "$(native_classify "" 137 codex)" "native: exit 137 is TIMEOUT"
    assert_eq "SUCCESS" "$(native_classify "API Error: 429 rate limit exceeded" 0 claude)" \
        "native: a clean exit quoting a 429 stays SUCCESS (#3233)"

    # The --transient deny-list travels in the exit code.
    TESTS_RUN=$((TESTS_RUN + 1))
    if "$parity_bin" classify-error --transient TOKEN_EXPIRED >/dev/null 2>&1; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: native --transient TOKEN_EXPIRED must be terminal (exit 1)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: native --transient TOKEN_EXPIRED is terminal (exit 1)"
    fi
    TESTS_RUN=$((TESTS_RUN + 1))
    if "$parity_bin" classify-error --transient RECOVERABLE >/dev/null 2>&1 \
        && "$parity_bin" classify-error --transient TOKEN_EXHAUSTED >/dev/null 2>&1 \
        && "$parity_bin" classify-error --transient MODEL_CREDITS_EXHAUSTED >/dev/null 2>&1 \
        && "$parity_bin" classify-error --transient SESSION_LIMIT >/dev/null 2>&1 \
        && "$parity_bin" classify-error --transient ANYTHING_ELSE >/dev/null 2>&1; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: native --transient retries RECOVERABLE/exhaustion/session-limit/unknown (exit 0)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: native --transient deny-list is not retry-by-default"
    fi

    # The $LOOM_RUNTIME default chain, native side (explicit --provider was
    # already exercised by every call above).
    rt_ans="$(printf '%s' 'Youve hit your usage limit.' | LOOM_RUNTIME=codex "$parity_bin" classify-error --exit-code 1 2>/dev/null)"
    assert_eq "TOKEN_EXHAUSTED" "$rt_ans" "native: provider defaults to \$LOOM_RUNTIME then claude"

    # File-input mode: the same text as a --stdout-tail file classifies
    # identically to stdin.
    tail_file="$(mktemp "${TMPDIR:-/tmp}/classify-parity.XXXXXX")"
    printf '%s' 'Failed to authenticate. API Error: 401 OAuth access token is invalid.' > "$tail_file"
    tail_ans="$("$parity_bin" classify-error --exit-code 1 --provider claude --stdout-tail "$tail_file" 2>/dev/null)"
    assert_eq "TOKEN_EXPIRED" "$tail_ans" "native: --stdout-tail file input classifies like stdin"
    rm -f "$tail_file"

    # Usage errors keep stdout EMPTY (the shell delegation reads that as
    # "could not ask" and degrades) and exit 2.
    TESTS_RUN=$((TESTS_RUN + 1))
    usage_out="$(printf '%s' 'x' | "$parity_bin" classify-error --provider claude 2>/dev/null)"
    usage_rc=0
    "$parity_bin" classify-error --provider claude </dev/null >/dev/null 2>&1 || usage_rc=$?
    if [[ "$usage_rc" -eq 2 && -z "$usage_out" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: native: missing --exit-code is a usage error (exit 2, empty stdout)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: native: missing --exit-code: rc=$usage_rc stdout='$usage_out' (want rc 2, empty stdout)"
    fi
}
fi

echo
echo "--- delegation safety: a binary that cannot answer is never asked ---"

# A resolved binary that fails the library's one-time capability probe (here
# a stub that exits 1 to everything, the same code `--transient` uses for
# "terminal"; /bin/false is not portable to macOS) must be dropped at
# source time. Were it asked, every retryable
# category would read as terminal. With no binary the documented fail-safe
# degraded answer applies (#11219): exit-code short-circuits stay exact,
# any other non-zero exit is RECOVERABLE, and every category is retryable.
# Runs in a subshell so the re-source does not disturb the mode of the rest.
NOCAP_STUB="$(mktemp)"
printf '#!/bin/sh\nexit 1\n' > "$NOCAP_STUB"
chmod +x "$NOCAP_STUB"
degraded_out="$(
    LOOM_DAEMON_SELF_BIN="$NOCAP_STUB"
    export LOOM_DAEMON_SELF_BIN
    # shellcheck source=../lib/classify-error.sh
    source "$SRC"
    printf 'bin=%s ' "${_CLASSIFY_DAEMON_BIN:-none}"
    classification_is_transient TOKEN_EXHAUSTED && printf 'exhausted=retry ' || printf 'exhausted=terminal '
    classification_is_transient TOKEN_EXPIRED && printf 'expired=retry ' || printf 'expired=terminal '
    printf 'class=%s ' "$(classify_error "You've hit your weekly limit" 1 claude)"
    printf 'ok=%s ' "$(classify_error "You've hit your weekly limit" 0 claude)"
    printf 'timeout=%s' "$(classify_error "" 124 claude)"
)"
assert_eq "bin=none exhausted=retry expired=retry class=RECOVERABLE ok=SUCCESS timeout=TIMEOUT" "$degraded_out" \
    "no capable binary: documented degraded answer (RECOVERABLE / retry; exit-code short-circuits exact)"
rm -f "$NOCAP_STUB"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
