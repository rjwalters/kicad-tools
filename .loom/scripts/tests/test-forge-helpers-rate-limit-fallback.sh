#!/usr/bin/env bash
# test-forge-helpers-rate-limit-fallback.sh - Unit tests for the GraphQL-
# exhaustion REST fallback helpers in lib/forge-helpers.sh (#4856).
#
# `gh issue edit`, `gh issue comment`, `gh issue reopen`, and `gh pr comment`
# are GraphQL-backed mutations. During a long sweep, GraphQL quota (5000/hr,
# shared across every agent + tool) can exhaust while REST quota still has
# headroom -- the same independent-quota fact the read-side fallback in
# check-duplicate.sh and merge-pr.sh's #4447 auto-merge-enable fallback
# already rely on. Before this fix, merge-pr.sh's best-effort mutating call
# sites (partial-increment label reset, premature-auto-close reopen,
# stacked-child deferral comment) simply swallowed a rate-limit rejection
# with the same generic warning as any other failure, silently dropping the
# label/comment update instead of retrying over REST.
#
# This file tests:
#   1. is_rate_limit_error() fires on all five documented signatures and
#      stays silent on unrelated failures (auth, network, 404).
#   2. forge_gh_comment_rl_safe / forge_gh_reopen_issue_rl_safe /
#      forge_gh_swap_label_rl_safe: succeed via the primary `gh` mutation
#      when it works, fall back to the correct REST endpoint/method only on
#      a rate-limit rejection, and propagate (not swallow) any OTHER
#      failure without attempting a REST call.
#   3. forge_gh_create_issue_rl_safe (#5047): same succeed/fallback/propagate
#      shape as #2, PLUS an atomicity check -- the REST fallback's payload
#      carries `labels` in the SAME POST as `title`/`body`, never a
#      create-then-label two-step.
#   4. merge-pr.sh source wiring: the four mutating call sites this issue
#      is about actually route through the new wrappers.
#   5. create-issue.sh (#5047/#5077): the CLI wrapper role prompts actually
#      invoke -- flag parsing, --body-file, and both the primary and REST
#      paths end to end. Regression guard: no role prompt under
#      defaults/.claude/commands/loom/ contains a line-anchored bare
#      `gh issue create`, all nine primary + five companion prompts named
#      in #5077 reference create-issue.sh, no prompt teaches a
#      create-issue.sh call immediately followed by a separate `gh issue
#      edit --add-label` (create-then-label), and `loom-daemon forge issue
#      create` documents that it has no REST fallback of its own.
#
# Usage:
#   ./.loom/scripts/tests/test-forge-helpers-rate-limit-fallback.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

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

# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- 1. is_rate_limit_error() signature table ---
echo "Testing is_rate_limit_error() signature table..."

positive_fixtures=(
    "graphql_already_exceeded:GraphQL: API rate limit already exceeded for user ID 12345"
    "rest_exceeded:HTTP 403: API rate limit exceeded for installation ID 1"
    "secondary_rate_limit:You have exceeded a secondary rate limit. Please retry your request again later."
    "abuse_detection:You have triggered an abuse detection mechanism and have been temporarily blocked."
    "submitted_too_quickly:This endpoint has hit a secondary limit and was submitted too quickly"
)

for entry in "${positive_fixtures[@]}"; do
    fixture_name="${entry%%:*}"
    fixture_value="${entry#*:}"
    TESTS_RUN=$((TESTS_RUN + 1))
    if is_rate_limit_error "$fixture_value"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: is_rate_limit_error fires on $fixture_name"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: is_rate_limit_error missed $fixture_name ('$fixture_value')"
    fi
done

negative_fixtures=(
    "auth_failure:gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable. HTTP 401 Bad credentials"
    "not_found:HTTP 404: Not Found"
    "network_error:dial tcp: lookup api.github.com: no such host"
    "bad_label:HTTP 422: Validation Failed (label does not exist)"
)

for entry in "${negative_fixtures[@]}"; do
    fixture_name="${entry%%:*}"
    fixture_value="${entry#*:}"
    TESTS_RUN=$((TESTS_RUN + 1))
    if is_rate_limit_error "$fixture_value"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: is_rate_limit_error fired on $fixture_name (false positive)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: is_rate_limit_error does NOT fire on $fixture_name"
    fi
done

# Case-insensitivity, mirroring the Rust RATE_LIMIT_SIGNATURES / check-duplicate.sh original.
TESTS_RUN=$((TESTS_RUN + 1))
if is_rate_limit_error "GRAPHQL: API RATE LIMIT ALREADY EXCEEDED"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: is_rate_limit_error matches case-insensitively"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: is_rate_limit_error must match case-insensitively"
fi

# --- 2. REST-fallback wrapper functions, with a stubbed `gh` on PATH ---
echo ""
echo "Testing forge_gh_*_rl_safe REST-fallback wrappers..."

STUB_DIR=$(mktemp -d)
ARGV_LOG="$STUB_DIR/argv.log"
GH_MODE_FILE="$STUB_DIR/mode.txt"
# Captures the JSON body a `gh api ... --input -` call reads from stdin, so
# the #5047 tests can assert labels ride along in the SAME POST as the title
# and body (atomic create+label, not create-then-label).
API_STDIN_LOG="$STUB_DIR/api-stdin.json"
# The stub script below runs as a separate process, so these must be
# exported for it to see them (a plain `VAR=x cmd` prefix only exports for
# the immediate command, not variables set earlier in this shell).
export ARGV_LOG GH_MODE_FILE API_STDIN_LOG

# A `gh` stub whose behavior is driven by $GH_MODE_FILE:
#   "ok"          - the primary (non-`api`) mutation succeeds immediately.
#   "ratelimited" - the primary mutation is rate-limited; `gh api ...` (the
#                   REST fallback) succeeds.
#   "other-error" - the primary mutation fails with an unrelated error; `gh
#                   api ...` must NEVER be reached (recorded as a FAIL marker
#                   in the argv log if it is).
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
mode="$(cat "$GH_MODE_FILE" 2>/dev/null || echo ok)"
printf '%s\n' "$*" >> "$ARGV_LOG"

if [[ "$1" == "api" ]]; then
  # Drain stdin only when the caller actually piped a body in (`--input -`),
  # never unconditionally -- an unconditional `cat` would block on the
  # inherited terminal for the label/comment fallbacks that use -f/-F.
  if [[ "$*" == *"--input -"* ]]; then
    cat > "$API_STDIN_LOG"
  fi
  if [[ "$mode" == "ratelimited" ]]; then
    # POST .../issues --jq .html_url returns the new issue's URL (#5047).
    [[ "$*" == *"--method POST"*/issues* ]] && echo "https://github.test/o/r/issues/1234"
    exit 0
  fi
  # Reached only when it should not have been.
  echo "UNEXPECTED_REST_CALL" >> "$ARGV_LOG"
  exit 1
fi

case "$mode" in
  ok)
    # `gh issue create` prints the new issue's URL on stdout (#5047).
    [[ "$1 $2" == "issue create" ]] && echo "https://github.test/o/r/issues/99"
    exit 0
    ;;
  ratelimited)
    echo "GraphQL: API rate limit already exceeded for user ID 1" >&2
    exit 1
    ;;
  other-error)
    echo "HTTP 404: Not Found" >&2
    exit 1
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

_run_stubbed() {
    local mode="$1"; shift
    echo "$mode" > "$GH_MODE_FILE"
    : > "$ARGV_LOG"
    : > "$API_STDIN_LOG"
    PATH="$STUB_DIR:$PATH" "$@"
}

# --- forge_gh_comment_rl_safe ---
rc=0
_run_stubbed ok forge_gh_comment_rl_safe "owner/repo" "42" "hello" >/dev/null 2>&1 || rc=$?
assert_eq "0" "$rc" "forge_gh_comment_rl_safe: primary gh issue comment succeeds -> no REST call"
TESTS_RUN=$((TESTS_RUN + 1))
if ! grep -q "^api " "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_comment_rl_safe (ok mode) never calls gh api"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_comment_rl_safe (ok mode) unexpectedly called gh api"
fi

rc=0
_run_stubbed ratelimited forge_gh_comment_rl_safe "owner/repo" "42" "hello" >/dev/null 2>&1 || rc=$?
assert_eq "0" "$rc" "forge_gh_comment_rl_safe: rate-limited primary call recovers via REST fallback"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "^api repos/owner/repo/issues/42/comments" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_comment_rl_safe REST fallback hits the correct comments endpoint"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_comment_rl_safe REST fallback endpoint wrong or missing"
fi

rc=0
_run_stubbed other-error forge_gh_comment_rl_safe "owner/repo" "42" "hello" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_gh_comment_rl_safe: non-rate-limit failure propagates (not swallowed)"
TESTS_RUN=$((TESTS_RUN + 1))
if ! grep -q "UNEXPECTED_REST_CALL" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_comment_rl_safe never retries over REST on a non-rate-limit failure"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_comment_rl_safe incorrectly retried over REST on a non-rate-limit failure"
fi

# --- forge_gh_reopen_issue_rl_safe ---
rc=0
_run_stubbed ratelimited forge_gh_reopen_issue_rl_safe "owner/repo" "99" >/dev/null 2>&1 || rc=$?
assert_eq "0" "$rc" "forge_gh_reopen_issue_rl_safe: rate-limited primary call recovers via REST fallback"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "^api repos/owner/repo/issues/99 -X PATCH -f state=open" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_reopen_issue_rl_safe REST fallback PATCHes state=open on the correct issue"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_reopen_issue_rl_safe REST fallback call wrong or missing"
fi

rc=0
_run_stubbed other-error forge_gh_reopen_issue_rl_safe "owner/repo" "99" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_gh_reopen_issue_rl_safe: non-rate-limit failure propagates"

# --- forge_gh_swap_label_rl_safe ---
rc=0
_run_stubbed ratelimited forge_gh_swap_label_rl_safe "owner/repo" "7" "loom:building" "loom:issue" >/dev/null 2>&1 || rc=$?
assert_eq "0" "$rc" "forge_gh_swap_label_rl_safe: rate-limited primary call recovers via REST fallback"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "^api repos/owner/repo/issues/7/labels/loom%3Abuilding -X DELETE" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_swap_label_rl_safe REST fallback DELETEs the percent-encoded old label"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_swap_label_rl_safe REST DELETE call wrong or missing (':' must be %3A-encoded)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "^api repos/owner/repo/issues/7/labels -f labels\[\]=loom:issue" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_swap_label_rl_safe REST fallback POSTs the new label"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_swap_label_rl_safe REST POST call wrong or missing"
fi

rc=0
_run_stubbed other-error forge_gh_swap_label_rl_safe "owner/repo" "7" "loom:building" "loom:issue" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_gh_swap_label_rl_safe: non-rate-limit failure propagates"

# --- forge_gh_create_issue_rl_safe (#5047) ---
echo ""
echo "Testing forge_gh_create_issue_rl_safe (#5047 issue-creation fallback)..."

# Primary path: `gh issue create` succeeds -> URL on stdout, no REST call.
out=$(_run_stubbed ok forge_gh_create_issue_rl_safe "owner/repo" "A title" "A body" "loom:triage" 2>/dev/null)
assert_eq "https://github.test/o/r/issues/99" "$out" \
    "forge_gh_create_issue_rl_safe: primary path returns the created issue URL"
TESTS_RUN=$((TESTS_RUN + 1))
if ! grep -q "^api " "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_create_issue_rl_safe (ok mode) never calls gh api"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_create_issue_rl_safe (ok mode) unexpectedly called gh api"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- "issue create --title A title --body A body --repo owner/repo --label loom:triage" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_create_issue_rl_safe primary path passes --label in the SAME create call"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_create_issue_rl_safe primary path must apply labels atomically via --label"
fi

# Exhausted-GraphQL path: falls back to ONE REST POST carrying labels inline.
out=$(_run_stubbed ratelimited forge_gh_create_issue_rl_safe "owner/repo" "A title" "A body" "loom:triage" "tier:goal-supporting" 2>/dev/null)
assert_eq "https://github.test/o/r/issues/1234" "$out" \
    "forge_gh_create_issue_rl_safe: exhausted GraphQL recovers via REST and returns the URL"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "^api --method POST repos/owner/repo/issues --input - --jq .html_url" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_create_issue_rl_safe REST fallback POSTs to repos/{nwo}/issues"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_create_issue_rl_safe REST fallback endpoint wrong or missing"
fi

# The atomicity guarantee: title + body + BOTH labels in one payload.
assert_eq "A title" "$(jq -r '.title' "$API_STDIN_LOG")" \
    "forge_gh_create_issue_rl_safe REST payload carries the title"
assert_eq "A body" "$(jq -r '.body' "$API_STDIN_LOG")" \
    "forge_gh_create_issue_rl_safe REST payload carries the body"
assert_eq "loom:triage,tier:goal-supporting" "$(jq -r '.labels | join(",")' "$API_STDIN_LOG")" \
    "forge_gh_create_issue_rl_safe REST payload applies ALL labels atomically with creation"

# Exactly one REST call -- never create-then-label (that doubles the request
# count under exactly the scarcity this fallback exists for, and can half-fail).
rest_call_count="$(grep -c "^api " "$ARGV_LOG" || true)"
assert_eq "1" "$rest_call_count" \
    "forge_gh_create_issue_rl_safe REST fallback is a SINGLE call (no create-then-label)"

# Empty NWO -> gh's literal {owner}/{repo} placeholder, which gh expands from
# the git remote with zero API calls (never `gh repo view`, itself GraphQL).
_run_stubbed ratelimited forge_gh_create_issue_rl_safe "" "T" "B" >/dev/null 2>&1
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qF "api --method POST repos/{owner}/{repo}/issues" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_create_issue_rl_safe uses the zero-API-call {owner}/{repo} placeholder when NWO is empty"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_create_issue_rl_safe must use the {owner}/{repo} placeholder when NWO is empty"
fi
assert_eq "[]" "$(jq -c '.labels' "$API_STDIN_LOG")" \
    "forge_gh_create_issue_rl_safe REST payload uses an empty labels array when no labels are given"

# A non-rate-limit failure must propagate untouched, with no REST retry.
rc=0
_run_stubbed other-error forge_gh_create_issue_rl_safe "owner/repo" "T" "B" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_gh_create_issue_rl_safe: non-rate-limit failure propagates (not swallowed)"
TESTS_RUN=$((TESTS_RUN + 1))
if ! grep -q "UNEXPECTED_REST_CALL" "$ARGV_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_gh_create_issue_rl_safe never retries over REST on a non-rate-limit failure"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_gh_create_issue_rl_safe incorrectly retried over REST on a non-rate-limit failure"
fi

# --- create-issue.sh CLI wrapper (#5047), the surface role prompts invoke ---
echo ""
echo "Testing create-issue.sh CLI wrapper (#5047)..."

CREATE_ISSUE_SH="$HELPERS_DIR/create-issue.sh"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$CREATE_ISSUE_SH" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: create-issue.sh exists and is executable"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: create-issue.sh missing or not executable at $CREATE_ISSUE_SH"
fi

BODY_TMP="$STUB_DIR/body.md"
cat > "$BODY_TMP" <<'BODY'
## Problem

A body read from a file.
BODY

out=$(_run_stubbed ratelimited env LOOM_FORGE_TYPE=github "$CREATE_ISSUE_SH" \
    --title "From CLI" --body-file "$BODY_TMP" --label "loom:triage,tier:goal-supporting" 2>/dev/null)
assert_eq "https://github.test/o/r/issues/1234" "$out" \
    "create-issue.sh: exhausted GraphQL still files the issue (REST) and prints the URL"
assert_eq "loom:triage,tier:goal-supporting" "$(jq -r '.labels | join(",")' "$API_STDIN_LOG")" \
    "create-issue.sh: comma-separated --label splits like gh, applied atomically"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(jq -r '.body' "$API_STDIN_LOG")" == *"A body read from a file."* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: create-issue.sh --body-file sends the file CONTENTS (not the path)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: create-issue.sh --body-file must send file contents, not the literal path"
fi

rc=0
_run_stubbed ok env LOOM_FORGE_TYPE=github "$CREATE_ISSUE_SH" --body "no title" >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "create-issue.sh: missing --title exits 2"

rc=0
_run_stubbed ok env LOOM_FORGE_TYPE=github "$CREATE_ISSUE_SH" --title "T" --body "B" --nope >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "create-issue.sh: unknown argument exits 2"

rc=0
_run_stubbed ok env LOOM_FORGE_TYPE=github "$CREATE_ISSUE_SH" \
    --title "T" --body "B" --body-file "$BODY_TMP" >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "create-issue.sh: --body and --body-file together exit 2"

rc=0
_run_stubbed other-error env LOOM_FORGE_TYPE=github "$CREATE_ISSUE_SH" \
    --title "T" --body "B" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "create-issue.sh: a non-rate-limit gh failure exits 1"

rm -rf "$STUB_DIR"

# --- 3. merge-pr.sh source wiring: the four #4856 call sites use the wrappers ---
echo ""
echo "Testing merge-pr.sh source wiring (#4856)..."

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'forge_gh_reopen_issue_rl_safe "\$REPO_NWO" "\$issue_num"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh's premature-auto-close reopen uses forge_gh_reopen_issue_rl_safe"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh missing forge_gh_reopen_issue_rl_safe at the reopen call site"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'forge_gh_swap_label_rl_safe "\$REPO_NWO" "\$issue_num" "loom:building" "loom:issue"' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: merge-pr.sh's partial-increment label reset uses forge_gh_swap_label_rl_safe"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: merge-pr.sh missing forge_gh_swap_label_rl_safe at the label-swap call site"
fi

comment_call_count="$(grep -c 'forge_gh_comment_rl_safe "\$REPO_NWO"' "$MERGE_PR_SRC" || true)"
assert_eq "3" "$comment_call_count" "merge-pr.sh routes all 3 comment call sites (2x issue, 1x PR) through forge_gh_comment_rl_safe"

# The raw, un-wrapped mutating calls this issue is about must no longer
# appear standalone (they are now routed through the wrapper functions
# above). This deliberately does NOT check `gh issue edit --add-label
# loom:reviewing`-style claim calls elsewhere in the file -- only the four
# specific #4856 call sites.
TESTS_RUN=$((TESTS_RUN + 1))
if ! grep -q 'gh issue reopen "\$issue_num" --repo "\$REPO_NWO" >/dev/null 2>&1' "$MERGE_PR_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: the old unwrapped 'gh issue reopen' call site is gone"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: the old unwrapped 'gh issue reopen' call site is still present"
fi

# --- 4. Role-prompt wiring (#5047): no prompt teaches a bare `gh issue create` ---
#
# This is the forcing function that keeps the fix from regressing back into
# nine prompts. A role prompt is what an agent actually follows, so an
# executable `gh issue create` line in one of them IS the bug, however well
# documented the fallback is elsewhere. Prose mentions of `gh issue create`
# (the #3707 serialization warnings, and the pointer blocks this issue added)
# are fine and expected -- they never start a line, so anchoring on
# line-start cleanly separates "instruction to run" from "discussion of".
echo ""
echo "Testing role-prompt wiring (#5047)..."

PROMPT_DIR="$(cd "$HELPERS_DIR/../.claude/commands/loom" && pwd)"

TESTS_RUN=$((TESTS_RUN + 1))
# Matches both a line-start invocation (`gh issue create ...`) and the
# `VAR=$(gh issue create ...` assignment form (#5077's epic.md /
# champion-pr-merge.md finding) -- both are executable, not prose. Prose
# mentions (backtick-quoted, mid-sentence) never match either shape.
bare_creates="$(grep -rlnE '^[[:space:]]*gh issue create|=\$\(gh issue create' "$PROMPT_DIR" || true)"
if [[ -z "$bare_creates" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no role prompt instructs a bare 'gh issue create'"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: these role prompts still instruct a bare 'gh issue create'"
    echo "    (use ./.loom/scripts/create-issue.sh instead — see #5047):"
    printf '    %s\n' $bare_creates
fi

# The nine prompts named in #5047, PLUS the five companion prompt/template
# files #5077 found still teaching a bare `gh issue create` (architect-
# patterns.md, architect-reference.md, champion-epic.md, champion-
# reference.md, imagine.md), must each reach the helper -- either by
# invoking it or by pointing at it.
for prompt in architect.md auditor.md builder-complexity.md builder-pr.md \
              curator.md doctor.md hermit.md hermit-patterns.md judge.md \
              architect-patterns.md architect-reference.md champion-epic.md \
              champion-reference.md imagine.md; do
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -q 'create-issue\.sh' "$PROMPT_DIR/$prompt"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $prompt routes issue filing through create-issue.sh"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $prompt has no reference to create-issue.sh (#5047)"
    fi
done

# create-then-label regression guard (#5077): a role prompt must never teach
# applying a label via a follow-up `gh issue edit --add-label` on an issue
# number/placeholder produced by the create call directly above it. This
# doesn't try to parse "is this label call related to that create call" --
# far simpler and just as effective: assert no `gh issue edit ... --add-label`
# line ever appears within 2 lines of a `create-issue.sh` invocation, which is
# exactly the shape a create-then-label two-step takes in these prompts.
TESTS_RUN=$((TESTS_RUN + 1))
create_then_label="$(grep -rn -A2 'create-issue\.sh' "$PROMPT_DIR" \
    | grep -E '[-:]gh issue edit .*--add-label' || true)"
if [[ -z "$create_then_label" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no role prompt follows a create-issue.sh call with a separate --add-label edit"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: these lines teach create-then-label right after create-issue.sh (#5077):"
    echo "$create_then_label"
fi

# `loom-daemon forge issue` must not silently look like an escape hatch: the
# passthrough either gains the fallback or says out loud that it has none.
TESTS_RUN=$((TESTS_RUN + 1))
FORGE_CMD_SRC="$(cd "$HELPERS_DIR/../../loom-daemon/src" 2>/dev/null && pwd || true)/forge_cmd.rs"
if [[ -f "$FORGE_CMD_SRC" ]] && grep -q 'create-issue.sh' "$FORGE_CMD_SRC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: loom-daemon forge issue create documents its lack of a REST fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_cmd.rs must state that 'forge issue create' has no REST fallback (#5047)"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
