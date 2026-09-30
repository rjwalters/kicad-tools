#!/usr/bin/env bash
# test-merge-pr-rate-limit-fallback.sh - `merge-pr.sh --auto` must still merge
# when the credential's GraphQL quota is exhausted (#4447) — now guaranteed by
# construction rather than by a rate-limit-string fallback.
#
# ## History
#
# GitHub's `enablePullRequestAutoMerge` mutation is GraphQL-only. When the
# shared credential's GraphQL quota was exhausted the mutation attempt failed
# with a rate-limit-shaped error — or, on the native `loom-daemon forge
# auto-merge` path, the *prior* repo-NWO resolution (`gh repo view`, itself
# GraphQL) failed first with "could not resolve repository NWO". Neither string
# matched the CLEAN/UNSTABLE/auto-merge-disabled greps, so `--auto` aborted even
# though a plain synchronous REST merge would have succeeded immediately
# (observed 2026-07-29 on PR #4439: 0/5000 GraphQL remaining, ~4000 REST left).
# #4447 added a fifth rejection handler to degrade to the REST wait-then-merge
# path.
#
# ## What changed (#8410)
#
# `merge-pr.sh` no longer arms the server-side auto-merge queue at all — it is
# gated only by the ruleset's REQUIRED checks and re-reads neither the `loom:pr`
# label nor the non-required suites once armed. With the GraphQL mutation gone
# from the merge path, GraphQL quota exhaustion can no longer block a merge in
# the way #4447 describes: the merge itself (`forge_merge_pr`), the check-runs
# poll and the PR re-reads are all REST.
#
# This file therefore pins the structural property that makes #4447's failure
# mode unreachable, rather than a string matcher for an error that can no longer
# be produced:
#
#   1. No GraphQL auto-merge mutation is issued on any path.
#   2. The merge itself goes through the REST `forge_merge_pr` helper.
#   3. `--auto` reaches that REST merge via the bounded REST check-runs wait.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-rate-limit-fallback.sh

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

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

src_absent() {
    local pattern="$1" msg="$2"
    if grep -q -- "$pattern" "$MERGE_PR_SRC"; then fail "$msg (found: $pattern)"; else pass "$msg"; fi
}
src_present() {
    local pattern="$1" msg="$2"
    if grep -q -- "$pattern" "$MERGE_PR_SRC"; then pass "$msg"; else fail "$msg"; fi
}

# --- 1. No GraphQL auto-merge mutation is issued on any path (#8410) --------
echo "Testing that no GraphQL auto-merge mutation remains on the merge path (#4447/#8410)..."

src_absent 'loom-daemon forge auto-merge' \
  "#8410: the native (GraphQL) auto-merge enable call is gone"
src_absent 'forge_auto_merge "$REPO_NWO"' \
  "#8410: the shell auto-merge enable call is gone"
src_absent 'API rate limit' \
  "#8410: the #4447 rate-limit rejection handler is gone with the mutation it handled"
src_absent 'could not resolve repository NWO' \
  "#8410: the #4447 NWO-resolution rejection handler is gone too"

# --- 2. The merge itself is the REST helper --------------------------------
echo ""
echo "Testing that the merge itself is REST (#4447's whole point)..."

src_present 'forge_merge_pr "$REPO_NWO" "$PR_NUMBER" "$MERGE_PRECONDITION_SHA"' \
  "the merge call is forge_merge_pr (REST PUT /pulls/{n}/merge)"

TESTS_RUN=$((TESTS_RUN + 1))
if awk '/^forge_merge_pr\(\)/{f=1} f; f && /^}/{exit}' "$FORGE_HELPERS_SRC" | grep -q 'api graphql'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: forge_merge_pr now uses GraphQL — the #4447 exhaustion failure mode would return"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: forge_merge_pr does not depend on GraphQL"
fi

# --- 3. --auto reaches that merge via the bounded REST check-runs wait ------
echo ""
echo "Testing that --auto reaches the REST merge via the bounded wait..."

src_present '_wait_for_checks_then_sync_merge() {' \
  "the bounded wait-then-sync-merge path is defined"
src_present 'forge_get_check_runs "$REPO_NWO" "$head_sha"' \
  "the wait polls check-runs (REST) rather than the GraphQL merge-state"

_auto_block="$(awk '/^if \[\[ "\$AUTO_MERGE" == "true" \]\]; then$/{f=1} f; f && /^fi$/{exit}' "$MERGE_PR_SRC")"
if printf '%s\n' "$_auto_block" | grep -q '^  _wait_for_checks_then_sync_merge$'; then
    pass "--auto calls the wait path unconditionally (no rejection string needed to get there)"
else
    fail "--auto must call _wait_for_checks_then_sync_merge unconditionally"
fi
if printf '%s\n' "$_auto_block" | grep -q '^  AUTO_MERGE=false$'; then
    pass "--auto then hands off to the synchronous (REST) merge path"
else
    fail "--auto must flip AUTO_MERGE=false so the synchronous merge path runs"
fi

# The one GraphQL read still reachable from the wait path is the CLASSIC
# branch-protection query inside forge_get_required_status_check_contexts, and
# it is only consulted when a check has actually FAILED. It fails CLOSED (the
# merge is refused, never silently allowed) — assert that contract survives,
# since it is the only remaining way a GraphQL outage can affect a merge.
echo ""
echo "Testing the remaining GraphQL dependency still fails closed..."
_wait_body="$(awk '/^_wait_for_checks_then_sync_merge\(\) \{/{f=1} f; f && /^}/{exit}' "$MERGE_PR_SRC")"
if printf '%s\n' "$_wait_body" | grep -q 'lookup_rc" -ne 0' && \
   printf '%s\n' "$_wait_body" | grep -q 'error "Failed to resolve required status checks'; then
    pass "a required-context lookup failure refuses the merge (fail closed)"
else
    fail "the required-context lookup failure path must refuse the merge"
fi

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
