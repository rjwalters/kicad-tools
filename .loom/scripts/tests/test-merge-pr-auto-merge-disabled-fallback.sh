#!/usr/bin/env bash
# test-merge-pr-auto-merge-disabled-fallback.sh - `merge-pr.sh --auto` must
# merge on a repository whose GitHub "Allow auto-merge" setting is OFF (#3763,
# #3820) — now guaranteed by construction rather than by a rejection handler.
#
# ## History
#
# When a repository's "Allow auto-merge" setting is OFF, GitHub rejects the
# enablePullRequestAutoMerge mutation with:
#
#     gh: Auto merge is not allowed for this repository
#
# #3763 added a reactive handler for that rejection (re-check .mergeable, then
# merge synchronously) and #3820 added a proactive probe
# (forge_check_auto_merge_allowed) that skipped the doomed mutation entirely and
# degraded `--auto` to "wait for checks, then merge in-process".
#
# ## What changed (#8410)
#
# `merge-pr.sh` no longer arms the server-side auto-merge queue AT ALL: a merge
# armed there is gated only by the branch ruleset's REQUIRED checks and re-reads
# neither the `loom:pr` label nor the non-required test suites, so a later
# `loom:verdict-stale` revocation and five still-running suites both went
# ignored on PR #8220. Every `--auto` run now takes #3820's wait-then-merge path
# unconditionally.
#
# So the repo-level setting this file is named after can no longer affect
# anything: the mutation it rejects is never issued. That is a STRONGER
# guarantee than the fallbacks were, and it is what this file now pins:
#
#   1. The probe helper and the shell arm it gated are RETIRED (#8427):
#      forge-helpers.sh defines neither, and no installed script calls them.
#   2. merge-pr.sh issues no enable-auto-merge mutation by any route, so the
#      "Auto merge is not allowed for this repository" rejection is unreachable.
#   3. `--auto` routes into the bounded wait-then-synchronous-merge path — the
#      #3820 behaviour, now for every repo rather than only disabled ones.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-auto-merge-disabled-fallback.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"
FORGE_HELPERS_SRC="$HELPERS_DIR/lib/forge-helpers.sh"

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

assert_src_absent() {
    local pattern="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -q -- "$pattern" "$MERGE_PR_SRC"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (found: $pattern)"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

assert_src_present() {
    local pattern="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -q -- "$pattern" "$MERGE_PR_SRC"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
    fi
}

# --- 1. The #3820 probe helper and the shell arm are retired (#8427) -------
echo "Testing that the shell auto-merge arming helpers are retired (#8427)..."

TESTS_RUN=$((TESTS_RUN + 1))
if grep -Eq '^[[:space:]]*(forge_check_auto_merge_allowed|forge_auto_merge)\(\)' "$FORGE_HELPERS_SRC"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge-helpers.sh still defines a retired auto-merge arming helper"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge-helpers.sh defines neither forge_auto_merge nor forge_check_auto_merge_allowed"
fi

# No installed script (tests excluded) may call either retired helper: a
# live call would now be a `command not found` at merge time.
TESTS_RUN=$((TESTS_RUN + 1))
stray_callers="$(grep -rnE '(^|[^_[:alnum:]])(forge_check_auto_merge_allowed|forge_auto_merge)([[:space:]]|$|\))' \
    --include='*.sh' "$HELPERS_DIR" 2>/dev/null \
    | grep -v '/tests/' | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' || true)"
if [[ -z "$stray_callers" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: no installed script calls a retired auto-merge arming helper"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: retired auto-merge helper still called:"
    echo "$stray_callers" | sed 's/^/    /'
fi

# --- 2. The rejection this file is named after is now unreachable (#8410) ---
#
# No enable-auto-merge mutation is issued by any route, so "Auto merge is not
# allowed for this repository" can never be returned to this script — and the
# #3763 handler for it is correctly gone rather than dormant.
echo ""
echo "Testing that merge-pr.sh never issues an enable-auto-merge mutation (#8410)..."

assert_src_absent 'forge_auto_merge "$REPO_NWO"' \
  "#8410: no shell forge_auto_merge call (the Gitea/GitHub arm)"
assert_src_absent 'loom-daemon forge auto-merge' \
  "#8410: no native loom-daemon forge auto-merge call"
assert_src_absent 'Auto merge is not allowed for this repository' \
  "#8410: the #3763 rejection handler is gone with the mutation it handled"
assert_src_absent 'AUTO_MERGE_OK' \
  "#8410: the enable-mutation retry loop and its success flag are gone"
assert_src_absent 'Auto-merge queued' \
  "#8410: no queued early-exit — every --auto merge completes in-process"

# --- 3. #3820's behaviour survives, unconditionally -------------------------
#
# The thing #3820 actually guaranteed — a repo with auto-merge disabled still
# merges, via a bounded wait then a synchronous merge — is what EVERY --auto run
# now does, on every repo.
echo ""
echo "Testing that --auto always waits then merges in-process (#3820 generalised)..."

assert_src_present '_wait_for_checks_then_sync_merge() {' \
  "merge-pr.sh still defines the bounded wait-then-sync-merge path"
assert_src_present 'LOOM_AUTO_MERGE_TIMEOUT' \
  "the wait is bounded by LOOM_AUTO_MERGE_TIMEOUT"

# The --auto block must call the wait, re-validate, and hand off to the
# synchronous path — in that order.
_auto_block="$(awk '/^if \[\[ "\$AUTO_MERGE" == "true" \]\]; then$/{f=1} f; f && /^fi$/{exit}' "$MERGE_PR_SRC")"
_wait_line="$(printf '%s\n' "$_auto_block" | grep -n '^  _wait_for_checks_then_sync_merge$' | head -1 | cut -d: -f1)"
_reval_line="$(printf '%s\n' "$_auto_block" | grep -n '^  _revalidate_merge_guards$' | head -1 | cut -d: -f1)"
_flip_line="$(printf '%s\n' "$_auto_block" | grep -n '^  AUTO_MERGE=false$' | head -1 | cut -d: -f1)"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -n "$_wait_line" && -n "$_reval_line" && -n "$_flip_line" ]] && \
   [[ "$_wait_line" -lt "$_reval_line" && "$_reval_line" -lt "$_flip_line" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: --auto waits, re-validates, then hands off to the synchronous merge (wait=$_wait_line revalidate=$_reval_line flip=$_flip_line)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: --auto must call _wait_for_checks_then_sync_merge, then _revalidate_merge_guards, then flip AUTO_MERGE=false (wait=$_wait_line revalidate=$_reval_line flip=$_flip_line)"
fi

# The degrade is not conditional on any repo setting any more.
assert_src_absent 'REPO_AUTO_MERGE_ALLOWED' \
  "#8410: the wait is unconditional — no repo-setting probe gates it"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
