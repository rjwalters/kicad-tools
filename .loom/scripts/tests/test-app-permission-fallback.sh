#!/usr/bin/env bash
# test-app-permission-fallback.sh - Unit tests for the GitHub App
# installation-token permission-scope 403 escalation (#6074).
#
# A cached App installation token can carry Contents:write while Issues/
# Pull-requests:write have not yet propagated into it, so a Builder's
# `git push` succeeds and the very next `gh pr create` returns
# `403 Resource not accessible by integration`. Before this fix the sweep died
# with no PR, the issue stayed ready, and the next dispatch REBUILT the same
# work, leaving an orphaned `feature/issue-N` branch behind each time.
#
# This file tests:
#   1. is_app_permission_error() fires on the integration-403 signature and on
#      nothing else -- in particular it must stay disjoint from
#      is_rate_limit_error()'s five signatures, in BOTH directions, so the
#      permission ladder and the REST rate-limit fallback can never be
#      confused for one another.
#   2. forge_gh_perm_safe(): a clean call runs exactly one attempt and never
#      mints; an integration-403 escalates to a FORCE-minted installation
#      token and then to a personal token; a non-permission failure escalates
#      nothing at all.
#   3. github-app-token.sh get-token --force: the flag reaches
#      github_app_get_token as the cache-bypass argument.
#   4. create-pr.sh: adopts an already-open PR for the head branch (the
#      no-rebuild guarantee), creates one otherwise, escalates through the
#      ladder on an integration-403, and rejects bad arguments.
#   5. Role-prompt wiring: the Builder prompts route PR creation through
#      create-pr.sh, with no line-anchored bare `gh pr create` left behind.
#
# Usage:
#   ./.loom/scripts/tests/test-app-permission-fallback.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CREATE_PR_SH="$HELPERS_DIR/create-pr.sh"
APP_TOKEN_SH="$HELPERS_DIR/lib/github-app-token.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

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

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual:              '$haystack'"
    fi
}

# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- 1. is_app_permission_error() signature table ---------------------------
echo "Testing is_app_permission_error() signature table..."

# The verbatim wording GitHub returns for an App installation missing a scope.
INTEGRATION_403='HTTP 403: Resource not accessible by integration (https://api.github.com/repos/o/r/pulls)'

if is_app_permission_error "$INTEGRATION_403"; then
    pass "fires on the observed 'Resource not accessible by integration' 403"
else
    fail "must fire on 'Resource not accessible by integration'"
fi

if is_app_permission_error "gh: RESOURCE NOT ACCESSIBLE BY INTEGRATION"; then
    pass "matches case-insensitively"
else
    fail "must match case-insensitively"
fi

app_perm_negatives=(
    "rate_limit_graphql:GraphQL: API rate limit already exceeded for user ID 12345"
    "rate_limit_rest:HTTP 403: API rate limit exceeded for installation ID 1"
    "secondary_rate_limit:You have exceeded a secondary rate limit. Please retry your request again later."
    "auth_401:HTTP 401: Bad credentials"
    "not_found:HTTP 404: Not Found"
    "plain_403:HTTP 403: Must have admin rights to Repository."
    "validation:HTTP 422: Validation Failed"
)
for entry in "${app_perm_negatives[@]}"; do
    name="${entry%%:*}"
    value="${entry#*:}"
    if is_app_permission_error "$value"; then
        fail "is_app_permission_error false-positived on $name"
    else
        pass "does NOT fire on $name"
    fi
done

# The two tables must be disjoint in BOTH directions: a permission 403 is not
# exhaustion (a REST retry with the same token 403s identically), and an
# exhaustion message must not trigger a credential swap.
if is_rate_limit_error "$INTEGRATION_403"; then
    fail "is_rate_limit_error must NOT claim the integration-403 (it is not exhaustion)"
else
    pass "is_rate_limit_error leaves the integration-403 alone"
fi

# --- 2. forge_gh_perm_safe() escalation ladder ------------------------------
echo ""
echo "Testing forge_gh_perm_safe() escalation ladder..."

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
ATTEMPT_LOG="$STUB_DIR/attempts.log"
MINT_LOG="$STUB_DIR/mint.log"
GH_MODE_FILE="$STUB_DIR/mode.txt"
MINT_MODE_FILE="$STUB_DIR/mint-mode.txt"
export ATTEMPT_LOG MINT_LOG GH_MODE_FILE MINT_MODE_FILE

# A `gh` stub that logs which credential each attempt carried, then answers
# according to $GH_MODE_FILE:
#   ok            - succeeds immediately.
#   perm403       - every attempt 403s with the integration wording.
#   perm403-once  - the FIRST attempt 403s, later attempts succeed.
#   other-error   - fails with an unrelated error (no escalation allowed).
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
mode="$(cat "$GH_MODE_FILE" 2>/dev/null || echo ok)"
cred="ambient"
[[ -n "${GH_TOKEN:-}" ]] && cred="token:${GH_TOKEN}"
[[ -z "${GH_TOKEN:-}" && -z "${GH_CONFIG_DIR:-}" ]] && cred="personal-ambient"
printf '%s | %s\n' "$cred" "$*" >> "$ATTEMPT_LOG"
attempts=$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')

case "$mode" in
  ok)
    echo "https://github.test/o/r/pull/7"
    exit 0
    ;;
  perm403)
    echo "HTTP 403: Resource not accessible by integration" >&2
    exit 1
    ;;
  perm403-once)
    if [[ "$attempts" == "1" ]]; then
      echo "HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    echo "https://github.test/o/r/pull/7"
    exit 0
    ;;
  other-error)
    echo "HTTP 404: Not Found" >&2
    exit 1
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

# A `github-app-token.sh` stub speaking the real JSON envelope.
cat > "$STUB_DIR/github-app-token.sh" <<'MINT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MINT_LOG"
mode="$(cat "$MINT_MODE_FILE" 2>/dev/null || echo ok)"
if [[ "$mode" == "not-configured" ]]; then
  echo '{"status":"not_configured","message":"github app not configured"}'
  exit 0
fi
echo '{"status":"ok","token":"ghs_fresh","installation_id":"1","app_id":"2","expires_at":"2099-01-01T00:00:00Z"}'
MINT
chmod +x "$STUB_DIR/github-app-token.sh"

# A git repo with an origin remote, so _forge_nwo_from_remote resolves without
# any API call.
FAKE_REPO="$STUB_DIR/repo"
mkdir -p "$FAKE_REPO"
git -C "$FAKE_REPO" init -q
git -C "$FAKE_REPO" remote add origin "https://github.com/owner/repo.git"
git -C "$FAKE_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
git -C "$FAKE_REPO" checkout -q -B feature/issue-6074

_run_ladder() {
    local gh_mode="$1" mint_mode="$2"
    shift 2
    echo "$gh_mode" > "$GH_MODE_FILE"
    echo "$mint_mode" > "$MINT_MODE_FILE"
    : > "$ATTEMPT_LOG"
    : > "$MINT_LOG"
    (
        cd "$FAKE_REPO"
        PATH="$STUB_DIR:$PATH" \
        LOOM_GITHUB_APP_SCRIPT="$STUB_DIR/github-app-token.sh" \
            "$@"
    )
}

# Happy path: one attempt, no mint.
out="$(_run_ladder ok ok forge_gh_perm_safe pr create --title T 2>/dev/null)"
assert_eq "https://github.test/o/r/pull/7" "$out" \
    "forge_gh_perm_safe: a successful call returns gh's stdout unchanged"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_perm_safe: a successful call makes exactly one attempt"
assert_eq "0" "$(wc -c < "$MINT_LOG" | tr -d ' ')" \
    "forge_gh_perm_safe: a successful call never mints a token"

# Rung 2: an integration-403 forces a fresh mint, and the retry succeeds.
rc=0
out="$(_run_ladder perm403-once ok forge_gh_perm_safe pr create --title T 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" "forge_gh_perm_safe: an integration-403 recovers via a fresh installation token"
assert_eq "https://github.test/o/r/pull/7" "$out" \
    "forge_gh_perm_safe: the escalated attempt's stdout is returned"
assert_contains "$(cat "$MINT_LOG")" "get-token --force" \
    "forge_gh_perm_safe: the re-mint BYPASSES the ~1h cache (--force)"
assert_contains "$(cat "$MINT_LOG")" "owner/repo" \
    "forge_gh_perm_safe: the re-mint targets the repo parsed from the git remote"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "token:ghs_fresh" \
    "forge_gh_perm_safe: the retry runs under the freshly minted token"

# Rung 3: a still-403ing fresh token falls back to the personal token.
rc=0
out="$(_run_ladder perm403 ok env LOOM_PERSONAL_GH_TOKEN=ghp_personal \
    bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_perm_safe pr create --title T' 2>/dev/null)" || rc=$?
assert_eq "1" "$rc" "forge_gh_perm_safe: an exhausted ladder still reports failure"
assert_contains "$(sed -n '3p' "$ATTEMPT_LOG")" "token:ghp_personal" \
    "forge_gh_perm_safe: rung 3 retries with LOOM_PERSONAL_GH_TOKEN"
assert_eq "3" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_perm_safe: the ladder is bounded at three attempts"

# Rung 3 without an explicit personal token: drop the daemon-owned
# GH_CONFIG_DIR so the operator's own gh credential is reached.
rc=0
_run_ladder perm403 not-configured env GH_CONFIG_DIR="$STUB_DIR/gh-config" \
    bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_perm_safe pr create --title T' >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_gh_perm_safe: reports failure when even the personal credential 403s"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "personal-ambient" \
    "forge_gh_perm_safe: with no App configured, rung 3 drops GH_CONFIG_DIR for the personal credential"

# With NO App-delivered credential in the environment at all, rung 3 would be
# a verbatim replay of rung 1 — so it must not run.
rc=0
_run_ladder perm403 not-configured \
    env -u GH_TOKEN -u GITHUB_TOKEN -u GH_CONFIG_DIR \
    bash -c 'source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; forge_gh_perm_safe pr create --title T' >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_gh_perm_safe: still reports failure with nothing to escalate to"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_perm_safe: never replays an identical attempt when there is no alternate credential"

# A non-permission failure escalates nothing.
rc=0
_run_ladder other-error ok forge_gh_perm_safe pr create --title T >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_gh_perm_safe: a non-permission failure propagates"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_perm_safe: a non-permission failure makes exactly one attempt"
assert_eq "0" "$(wc -c < "$MINT_LOG" | tr -d ' ')" \
    "forge_gh_perm_safe: a non-permission failure never mints a token"

# --- 3. github-app-token.sh get-token --force -------------------------------
echo ""
echo "Testing github-app-token.sh get-token --force..."

# With no usable app credential the CLI reports not_configured (and never
# errors) — the load-bearing fallback default — for both flag orders. The env
# overrides beat any real host config, so this stays hermetic on a fleet host
# that genuinely has an App installed.
for form in "--force owner/repo" "owner/repo --force"; do
    # shellcheck disable=SC2086
    out="$(env LOOM_GITHUB_APP_ID=1 LOOM_GITHUB_APP_KEY_PATH="$STUB_DIR/absent.pem" \
        REPO_ROOT="$STUB_DIR" bash "$APP_TOKEN_SH" get-token $form 2>/dev/null)"
    assert_eq "not_configured" "$(printf '%s' "$out" | jq -r '.status')" \
        "get-token accepts '$form' and falls back to ambient auth when unconfigured"
done

if grep -q 'github_app_get_token "\$_nwo" "\$_gh_app_force"' "$APP_TOKEN_SH"; then
    pass "the CLI forwards --force into github_app_get_token's cache-bypass argument"
else
    fail "the CLI must forward --force into github_app_get_token"
fi

# --- 4. create-pr.sh --------------------------------------------------------
echo ""
echo "Testing create-pr.sh..."

if [[ -x "$CREATE_PR_SH" ]]; then
    pass "create-pr.sh exists and is executable"
else
    fail "create-pr.sh missing or not executable at $CREATE_PR_SH"
fi

# The adopt-first stub: `gh pr list` answers from $EXISTING_PR_FILE, and
# `gh pr create` behaves per $GH_MODE_FILE.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
mode="$(cat "$GH_MODE_FILE" 2>/dev/null || echo ok)"
cred="ambient"
[[ -n "${GH_TOKEN:-}" ]] && cred="token:${GH_TOKEN}"
printf '%s | %s\n' "$cred" "$*" >> "$ATTEMPT_LOG"

if [[ "$1 $2" == "pr list" ]]; then
  cat "$EXISTING_PR_FILE" 2>/dev/null || true
  exit 0
fi

attempts=$(grep -c "pr create" "$ATTEMPT_LOG" || true)
case "$mode" in
  ok)
    echo "https://github.test/o/r/pull/7"
    exit 0
    ;;
  perm403-once)
    if [[ "$attempts" == "1" ]]; then
      echo "HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    echo "https://github.test/o/r/pull/7"
    exit 0
    ;;
esac
STUB
chmod +x "$STUB_DIR/gh"
EXISTING_PR_FILE="$STUB_DIR/existing-pr.txt"
export EXISTING_PR_FILE
: > "$EXISTING_PR_FILE"

_run_create_pr() {
    local gh_mode="$1"
    shift
    echo "$gh_mode" > "$GH_MODE_FILE"
    echo "ok" > "$MINT_MODE_FILE"
    : > "$ATTEMPT_LOG"
    : > "$MINT_LOG"
    (
        cd "$FAKE_REPO"
        PATH="$STUB_DIR:$PATH" \
        LOOM_FORGE_TYPE=github \
        LOOM_GITHUB_APP_SCRIPT="$STUB_DIR/github-app-token.sh" \
            "$CREATE_PR_SH" "$@"
    )
}

# Adopt: an already-open PR for this branch is returned, and NOTHING is created.
printf 'https://github.test/o/r/pull/42\n' > "$EXISTING_PR_FILE"
out="$(_run_create_pr ok --title "fix: t" --body "b" --label "loom:review-requested" 2>/dev/null)"
assert_eq "https://github.test/o/r/pull/42" "$out" \
    "create-pr.sh: adopts the open PR that already exists for the head branch"
if grep -q "pr create" "$ATTEMPT_LOG"; then
    fail "create-pr.sh must NOT create a second PR when one already exists"
else
    pass "create-pr.sh: adopting never issues a duplicate 'pr create'"
fi

# Create: no existing PR -> one create call carrying --head and the label.
: > "$EXISTING_PR_FILE"
out="$(_run_create_pr ok --title "fix: t" --body "b" --label "loom:review-requested" 2>/dev/null)"
assert_eq "https://github.test/o/r/pull/7" "$out" \
    "create-pr.sh: creates the PR and returns its URL when none exists"
assert_contains "$(cat "$ATTEMPT_LOG")" "pr create --head feature/issue-6074" \
    "create-pr.sh: passes --head explicitly (never relies on origin auto-detect)"
assert_contains "$(cat "$ATTEMPT_LOG")" "--label loom:review-requested" \
    "create-pr.sh: applies labels in the SAME create call"

# The incident itself, end to end: the create 403s, the ladder re-mints, the
# PR is opened — no failure, so no rebuild and no orphaned branch.
rc=0
out="$(_run_create_pr perm403-once --title "fix: t" --body "b" --label "loom:review-requested" 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" "create-pr.sh: an App permission-window 403 no longer fails the PR creation"
assert_eq "https://github.test/o/r/pull/7" "$out" \
    "create-pr.sh: returns the PR URL created by the escalated attempt"
assert_contains "$(cat "$MINT_LOG")" "get-token --force" \
    "create-pr.sh: the escalation force-mints a fresh installation token"

rc=0
_run_create_pr ok --body "b" >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "create-pr.sh: missing --title exits 2"

rc=0
_run_create_pr ok --title "t" --body "b" --nope >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "create-pr.sh: unknown argument exits 2"

rc=0
_run_create_pr ok --title "t" --body "b" --body-file /dev/null >/dev/null 2>&1 || rc=$?
assert_eq "2" "$rc" "create-pr.sh: --body and --body-file together exit 2"

# --- 5. Role-prompt wiring --------------------------------------------------
echo ""
echo "Testing Builder role-prompt wiring (#6074)..."

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (HELPERS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (HELPERS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#447).
if [[ -d "$HELPERS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$HELPERS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$HELPERS_DIR/../.claude/commands/loom" && pwd)"
fi

for prompt in builder.md builder-pr.md builder-worktree.md; do
    if grep -q 'create-pr\.sh' "$PROMPT_DIR/$prompt"; then
        pass "$prompt routes PR creation through create-pr.sh"
    else
        fail "$prompt has no reference to create-pr.sh (#6074)"
    fi
done

# The forcing function: a Builder prompt is what an agent actually follows, so
# an executable bare `gh pr create` line in one IS the bug. Prose mentions
# (backtick-quoted, mid-sentence) never start a line, so anchoring separates
# "instruction to run" from "discussion of".
bare_creates="$(grep -lnE '^[[:space:]]*gh pr create|=\$\(gh pr create' \
    "$PROMPT_DIR/builder.md" "$PROMPT_DIR/builder-pr.md" "$PROMPT_DIR/builder-worktree.md" || true)"
if [[ -z "$bare_creates" ]]; then
    pass "no Builder prompt instructs a bare 'gh pr create'"
else
    fail "these Builder prompts still instruct a bare 'gh pr create' (use create-pr.sh — #6074):"
    printf '    %s\n' $bare_creates
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
