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

# shellcheck source=../lib/classify-error.sh
source "$SRC"

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
past-tense variant|Your access token was revoked|TOKEN_EXPIRED
JSON authentication_error with some OTHER message|{"type":"authentication_error","message":"Invalid API key"}|TOKEN_EXPIRED
pre-#6614 plain form still classifies|API Error: 401 authentication_error|TOKEN_EXPIRED
negative: a revoked thing that is not a token|The reviewer revoked their approval and the ruleset was revoked|RECOVERABLE
EOF

# The exit-code-first guarantee (#3233) for the new phrasing, checked with the
# real exit code rather than smuggling it through the table above.
assert_eq "SUCCESS" \
    "$(classify_error 'API Error: 401 {"type":"authentication_error","message":"OAuth access token has been revoked."}' 0)" \
    "a CLEAN exit whose output quotes the revoked-token 401 stays SUCCESS (#3233)"

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
# matches no table in _classify_error_provider by design, and the exit-code-first
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
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
