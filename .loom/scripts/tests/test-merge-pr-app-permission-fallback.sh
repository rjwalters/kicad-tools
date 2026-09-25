#!/usr/bin/env bash
# test-merge-pr-app-permission-fallback.sh - Unit tests for the App-token
# permission-scope 403 escalation on the MERGE path (#6752).
#
# #6074 gave every shell `gh` write site a 3-rung credential ladder
# (ambient -> force-minted installation token -> personal token) that recovers
# from `403 Resource not accessible by integration`. The merge itself bypassed
# it on BOTH of its write paths:
#
#   * `merge-pr.sh` calls the native Rust `loom-daemon forge auto-merge`, which
#     has its own forge-write code entirely outside the bash ladder, and
#   * the synchronous path's `forge_merge_pr` issued a bare `gh api ... -X PUT`.
#
# Observed live 2026-08-22 (`/loom:sweep 6746`, PR #6751): `merge-pr.sh --auto`
# died with the exact integration-403 while the same repo's comment/label
# writes recovered through the ladder; the orchestrator had to `unset
# GH_CONFIG_DIR` by hand — rung 3, performed manually — to complete the merge.
#
# This file tests:
#   1. forge_cmd_perm_safe(): the ladder generalized past `gh` — a clean call
#      runs once and never mints; an integration-403 escalates to a FORCE-minted
#      installation token and then to a personal token; a non-permission failure
#      escalates nothing; and the wrapped command's own exit code is preserved
#      verbatim (loom-daemon's 3 = forge declined, 4 = head-SHA mismatch must
#      never be retried or rewritten).
#   2. forge_merge_pr() (GitHub REST PUT) escalates on the integration-403 and
#      returns the merge payload from the escalated attempt.
#   3. merge-pr.sh routes the native `loom-daemon forge auto-merge` call through
#      forge_cmd_perm_safe (no bare invocation left behind).
#   4. No regression to #6074: forge_gh_perm_safe is still the `gh` spelling of
#      the same ladder and still escalates a `gh` write identically.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-app-permission-fallback.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
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

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
ATTEMPT_LOG="$STUB_DIR/attempts.log"
MINT_LOG="$STUB_DIR/mint.log"
MODE_FILE="$STUB_DIR/mode.txt"
MINT_MODE_FILE="$STUB_DIR/mint-mode.txt"
export ATTEMPT_LOG MINT_LOG MODE_FILE MINT_MODE_FILE

# A `loom-daemon` stub standing in for the native forge subcommand. It logs
# which credential each attempt carried, then answers according to $MODE_FILE:
#   ok            - auto-merge enabled (exit 0).
#   perm403       - every attempt 403s with the integration wording (exit 1).
#   perm403-once  - the FIRST attempt 403s, later attempts succeed.
#   other-error   - an unrelated failure (no escalation allowed).
#   declined      - exit 3, the Gitea "not handled natively" decline.
#   head-mismatch - exit 4, EX_FORGE_HEAD_MISMATCH.
cat > "$STUB_DIR/loom-daemon" <<'STUB'
#!/usr/bin/env bash
mode="$(cat "$MODE_FILE" 2>/dev/null || echo ok)"
cred="ambient"
[[ -n "${GH_TOKEN:-}" ]] && cred="token:${GH_TOKEN}"
[[ -z "${GH_TOKEN:-}" && -z "${GH_CONFIG_DIR:-}" ]] && cred="personal-ambient"
printf '%s | %s\n' "$cred" "$*" >> "$ATTEMPT_LOG"
attempts=$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')

case "$mode" in
  ok)
    echo "Auto-merge enabled for PR #6751"
    exit 0
    ;;
  perm403)
    echo "Failed to enable auto-merge for PR #6751: HTTP 403: Resource not accessible by integration" >&2
    exit 1
    ;;
  perm403-once)
    if [[ "$attempts" == "1" ]]; then
      echo "Failed to enable auto-merge for PR #6751: HTTP 403: Resource not accessible by integration" >&2
      exit 1
    fi
    echo "Auto-merge enabled for PR #6751"
    exit 0
    ;;
  other-error)
    echo "Failed to enable auto-merge for PR #6751: Pull request Pull request is in clean status" >&2
    exit 1
    ;;
  declined)
    echo "loom-daemon forge auto-merge: gitea is not handled natively" >&2
    exit 3
    ;;
  head-mismatch)
    echo "Failed to enable auto-merge for PR #6751: head branch was modified" >&2
    exit 4
    ;;
esac
STUB
chmod +x "$STUB_DIR/loom-daemon"

# A `gh` stub sharing the same mode/attempt protocol, for the forge_merge_pr
# and forge_gh_perm_safe cases.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
mode="$(cat "$MODE_FILE" 2>/dev/null || echo ok)"
cred="ambient"
[[ -n "${GH_TOKEN:-}" ]] && cred="token:${GH_TOKEN}"
[[ -z "${GH_TOKEN:-}" && -z "${GH_CONFIG_DIR:-}" ]] && cred="personal-ambient"
printf '%s | %s\n' "$cred" "$*" >> "$ATTEMPT_LOG"
attempts=$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')

case "$mode" in
  ok)
    echo '{"merged":true,"sha":"cafe1234"}'
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
    echo '{"merged":true,"sha":"cafe1234"}'
    exit 0
    ;;
  other-error)
    echo "HTTP 405: Base branch was modified. Review and try the merge again." >&2
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

# A git repo with an origin remote, so _forge_nwo_from_remote resolves the NWO
# for the re-mint without any API call.
FAKE_REPO="$STUB_DIR/repo"
mkdir -p "$FAKE_REPO"
git -C "$FAKE_REPO" init -q
git -C "$FAKE_REPO" remote add origin "https://github.com/owner/repo.git"

# Runs a ladder invocation inside the fake repo with the stubs on PATH.
# Usage: _run <mode> <mint-mode> [env-prefix…] <command…>
_run() {
    local mode="$1" mint_mode="$2"
    shift 2
    echo "$mode" > "$MODE_FILE"
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

# Sources the helpers fresh in a child shell (needed whenever the invocation
# has to carry an `env` prefix, which cannot wrap a shell function).
_ladder_in_child='source "'"$HELPERS_DIR"'/lib/forge-helpers.sh"; '

# --- 1. forge_cmd_perm_safe(): the ladder generalized past `gh` -------------
echo "Testing forge_cmd_perm_safe() on a native (non-gh) forge write..."

NATIVE_ARGS=(loom-daemon forge auto-merge 6751 --method squash --expected-head-sha deadbeef)

# Happy path: one attempt, no mint.
rc=0
out="$(_run ok ok forge_cmd_perm_safe "${NATIVE_ARGS[@]}" 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" "forge_cmd_perm_safe: a successful native call returns 0"
assert_eq "Auto-merge enabled for PR #6751" "$out" \
    "forge_cmd_perm_safe: a successful native call returns the command's stdout unchanged"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_cmd_perm_safe: a successful native call makes exactly one attempt"
assert_eq "0" "$(wc -c < "$MINT_LOG" | tr -d ' ')" \
    "forge_cmd_perm_safe: a successful native call never mints a token"

# Rung 2: the incident itself — an integration-403 from the native merge path
# force-mints a fresh installation token, and the retried merge succeeds.
rc=0
out="$(_run perm403-once ok forge_cmd_perm_safe "${NATIVE_ARGS[@]}" 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" \
    "forge_cmd_perm_safe: an integration-403 from loom-daemon recovers via a fresh installation token"
assert_eq "Auto-merge enabled for PR #6751" "$out" \
    "forge_cmd_perm_safe: the escalated native attempt's stdout is returned"
assert_contains "$(cat "$MINT_LOG")" "get-token --force" \
    "forge_cmd_perm_safe: the re-mint BYPASSES the ~1h cache (--force)"
assert_contains "$(cat "$MINT_LOG")" "owner/repo" \
    "forge_cmd_perm_safe: the re-mint targets the repo parsed from the git remote"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "token:ghs_fresh" \
    "forge_cmd_perm_safe: the native retry runs under the freshly minted token"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "forge auto-merge 6751 --method squash --expected-head-sha deadbeef" \
    "forge_cmd_perm_safe: the retry replays the native argv verbatim (precondition SHA included)"

# Rung 3: a still-403ing fresh token falls back to the personal token — the
# `unset GH_CONFIG_DIR` the orchestrator had to perform by hand on PR #6751.
rc=0
_run perm403 ok env LOOM_PERSONAL_GH_TOKEN=ghp_personal \
    bash -c "${_ladder_in_child}forge_cmd_perm_safe ${NATIVE_ARGS[*]}" >/dev/null 2>&1 || rc=$?
assert_eq "1" "$rc" "forge_cmd_perm_safe: an exhausted ladder still reports the command's failure"
assert_contains "$(sed -n '3p' "$ATTEMPT_LOG")" "token:ghp_personal" \
    "forge_cmd_perm_safe: rung 3 retries the native call with LOOM_PERSONAL_GH_TOKEN"
assert_eq "3" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_cmd_perm_safe: the ladder is bounded at three attempts"

# A non-permission failure escalates nothing — merge-pr.sh's CLEAN/UNSTABLE/
# rate-limit classifiers must keep seeing a single, unmodified failure.
rc=0
out="$(_run other-error ok forge_cmd_perm_safe "${NATIVE_ARGS[@]}" 2>&1)" || rc=$?
assert_eq "1" "$rc" "forge_cmd_perm_safe: a non-permission failure propagates"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_cmd_perm_safe: a non-permission failure makes exactly one attempt"
assert_eq "0" "$(wc -c < "$MINT_LOG" | tr -d ' ')" \
    "forge_cmd_perm_safe: a non-permission failure never mints a token"
assert_contains "$out" "is in clean status" \
    "forge_cmd_perm_safe: the failure text reaches the caller verbatim (classifiers still fire)"

# Exit-code fidelity: loom-daemon's meaningful codes must survive the wrapper
# untouched and unretried (3 = Gitea decline -> shell fallback, 4 = head-SHA
# mismatch -> re-queue, NOT a failure).
rc=0
_run declined ok forge_cmd_perm_safe "${NATIVE_ARGS[@]}" >/dev/null 2>&1 || rc=$?
assert_eq "3" "$rc" "forge_cmd_perm_safe: the Gitea decline exit (3) is preserved verbatim"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_cmd_perm_safe: a decline is never retried"

rc=0
_run head-mismatch ok forge_cmd_perm_safe "${NATIVE_ARGS[@]}" >/dev/null 2>&1 || rc=$?
assert_eq "4" "$rc" "forge_cmd_perm_safe: the head-SHA-mismatch exit (4) is preserved verbatim"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_cmd_perm_safe: a head-SHA mismatch is never retried"

# --- 2. forge_merge_pr(): the synchronous REST merge ------------------------
echo ""
echo "Testing forge_merge_pr() (GitHub REST PUT) escalation..."

rc=0
out="$(_run perm403-once ok env FORGE_TYPE=github \
    bash -c "${_ladder_in_child}FORGE_TYPE=github forge_merge_pr owner/repo 6751 deadbeef" 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" "forge_merge_pr: an integration-403 no longer fails the merge"
assert_contains "$out" '"merged":true' \
    "forge_merge_pr: the escalated attempt's merge payload is returned"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "token:ghs_fresh" \
    "forge_merge_pr: the retry runs under the freshly minted installation token"
assert_contains "$(sed -n '2p' "$ATTEMPT_LOG")" "sha=deadbeef" \
    "forge_merge_pr: the retry preserves the -f sha= optimistic-concurrency precondition (#5579)"

rc=0
out="$(_run other-error ok env FORGE_TYPE=github \
    bash -c "${_ladder_in_child}FORGE_TYPE=github forge_merge_pr owner/repo 6751 deadbeef" 2>&1)" || rc=$?
assert_eq "1" "$rc" "forge_merge_pr: a non-permission merge failure still propagates"
assert_eq "1" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_merge_pr: a non-permission merge failure makes exactly one attempt"
assert_contains "$out" "Base branch was modified" \
    "forge_merge_pr: the stale-base retry string still reaches merge-pr.sh's classifier"

# --- 3. merge-pr.sh source wiring ------------------------------------------
echo ""
echo "Testing merge-pr.sh source wiring (#6752)..."

# #8410 removed the native auto-merge call site along with the rest of the
# server-side arm, so the write this ladder has to protect on the `--auto`
# path is now the synchronous REST merge itself (forge_merge_pr, section 2
# above — it carries its own escalation and is exercised there). What must
# still hold here is that the merge call merge-pr.sh DOES make is the
# ladder-protected one.
if grep -q 'forge_merge_pr "\$REPO_NWO" "\$PR_NUMBER" "\$MERGE_PRECONDITION_SHA"' "$MERGE_PR_SRC"; then
    pass "merge-pr.sh merges through forge_merge_pr, which carries the #6752 escalation ladder"
else
    fail "merge-pr.sh must merge via forge_merge_pr (the ladder-protected write)"
fi

# The forcing function: an unwrapped invocation IS the bug, so assert none
# remains. Since #8410 there is no wrapped one either — any `loom-daemon forge
# auto-merge` line in this script is a reintroduced server-side arm.
# Prose/comment mentions never start an assignment or a command.
bare_native="$(grep -nE '^[[:space:]]*(AUTO_MERGE_OUTPUT=\$\()?loom-daemon forge auto-merge' "$MERGE_PR_SRC" || true)"
if [[ -z "$bare_native" ]]; then
    pass "no bare (unwrapped) 'loom-daemon forge auto-merge' invocation remains in merge-pr.sh"
else
    fail "merge-pr.sh still invokes 'loom-daemon forge auto-merge' outside the ladder (#6752):"
    printf '    %s\n' "$bare_native"
fi

# --- 4. No regression to the #6074 bash-side ladder ------------------------
echo ""
echo "Testing forge_gh_perm_safe() regression (#6074)..."

if grep -q 'forge_cmd_perm_safe gh "\$@"' "$HELPERS_DIR/lib/forge-helpers.sh"; then
    pass "forge_gh_perm_safe is the gh-prefixed spelling of the same ladder (single implementation)"
else
    fail "forge_gh_perm_safe must delegate to forge_cmd_perm_safe so the two cannot drift"
fi

rc=0
out="$(_run perm403-once ok forge_gh_perm_safe api repos/owner/repo/issues/1/comments -f body=hi 2>/dev/null)" || rc=$?
assert_eq "0" "$rc" "forge_gh_perm_safe: a gh integration-403 still recovers via a fresh installation token"
assert_contains "$out" '"merged":true' \
    "forge_gh_perm_safe: the escalated gh attempt's stdout is still returned"
assert_contains "$(sed -n '1p' "$ATTEMPT_LOG")" "api repos/owner/repo/issues/1/comments" \
    "forge_gh_perm_safe: the gh argv is passed through unchanged"
assert_eq "2" "$(wc -l < "$ATTEMPT_LOG" | tr -d ' ')" \
    "forge_gh_perm_safe: the recovered call stops at rung 2 (no needless personal-token attempt)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
