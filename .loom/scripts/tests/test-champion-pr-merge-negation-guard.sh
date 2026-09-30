#!/usr/bin/env bash
# test-champion-pr-merge-negation-guard.sh - Regression coverage for issue #1057.
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# example-org/tool-repo#1057: PR #1051's body contained "**does not fix #909** -- ...
# #909 is left open for its owner to close or subsume" -- an explicit intent
# to leave #909 open. GitHub's closingIssuesReferences parser (and the Gitea
# word-boundary regex fallback in forge_pr_close_targets) is not
# negation-aware, so #909 was closed against the author's stated intent, and
# champion-pr-merge.md's Step 4 ("Verify Issue Auto-Close") would `gh issue
# close` every candidate with no re-check of the source text.
#
# THE FIX
#
# `loom-daemon merge-pr-refs has-unnegated-closing-ref --issue N` (text on
# stdin) is a TRI-STATE predicate: exit 0 = an unnegated closing reference
# exists, 1 = every reference found is negated, 3 = no textual reference to
# the issue at all (e.g. it is linked only through the PR's Development
# sidebar, or via `Fixes owner/repo#N` / `Closes: #N` -- forms the regex
# cannot see). Distinguishing 1 from 3 matters: an earlier, two-state version
# of this predicate conflated "never mentioned" with "mentioned and
# disclaimed", so a PR closing an issue through one of those unseen channels
# was wrongly reopened. Step 4 runs it per LINKED_ISSUES candidate over the PR
# body plus the squash merge commit message, BEFORE `gh issue close`, and
# reopens on exit 1 only. The predicate's logic is covered by Rust unit tests
# (loom-daemon/src/merge_pr/refs/tests.rs, including the exact PR #1051 body
# and the no-reference/cross-repo cases); this suite pins the WIRING so a
# future edit cannot silently drop the cross-check or the reopen self-heal.
#
# Hermetic: greps the shipped doc. No forge, no network, no tokens.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Installed path first (consumer repos, and Loom's own dogfooded checkout),
# falling back to the defaults/ source tree. See issue #6194 / #6241.
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi
DOC="$ROLE_DIR/champion-pr-merge.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_doc_contains() {
    local needle="$1" msg="$2"
    if grep -qF -- "$needle" "$DOC"; then
        pass "$msg"
    else
        fail "$msg (missing literal in $DOC: $needle)"
    fi
}

echo "test-champion-pr-merge-negation-guard.sh (#1057): champion-pr-merge.md Step 4 wiring"

assert_doc_contains "loom-daemon merge-pr-refs has-unnegated-closing-ref --issue \"\$issue\"" \
    "Step 4 runs the negation predicate per linked issue"
assert_doc_contains 'if [ $? -eq 1 ] && [ -n "$NEG_SRC" ]; then' \
    "only exit 1 (and only with source text in hand) counts as negated -- an older daemon's exit 2 falls through"
assert_doc_contains "gh issue reopen" \
    "Step 4 reopens an issue GitHub's own parser closed on a negated-only reference"
assert_doc_contains ".commit.message" \
    "Step 4 cross-checks the squash merge commit message, not just the PR body"
assert_doc_contains "#1057" "Step 4's negation cross-check cites issue #1057"

# The cross-check must run BEFORE the unconditional `gh issue close` -- confirm
# by line number rather than mere presence.
NEGATION_LINE=$(grep -n "has-unnegated-closing-ref" "$DOC" | tail -1 | cut -d: -f1)
CLOSE_LINE=$(grep -n 'gh issue close "\$issue"' "$DOC" | tail -1 | cut -d: -f1)
if [[ -n "$NEGATION_LINE" && -n "$CLOSE_LINE" && "$NEGATION_LINE" -lt "$CLOSE_LINE" ]]; then
    pass "the negation cross-check runs BEFORE the unconditional gh issue close call"
else
    fail "the negation cross-check does not precede gh issue close (negation line=$NEGATION_LINE, close line=$CLOSE_LINE)"
fi

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
