#!/usr/bin/env bash
# test-champion-pr-merge-negation-guard.sh - Regression coverage for issue #1057.
#
# #1057 (example-org/tool-repo): PR #1051's body said "**does not fix #909** --
# ... #909 is left open for its owner to close or subsume". GitHub's
# closingIssuesReferences parser (and the Gitea regex fallback in
# forge_pr_close_targets) is not negation-aware, so #909 was closed against the
# author's stated intent, and champion-pr-merge.md's Step 4 ("Verify Issue
# Auto-Close") would `gh issue close` every candidate with no re-check.
# THE FIX: `loom-daemon merge-pr-refs has-unnegated-closing-ref --issue N` (text
# on stdin), a TRI-STATE predicate: exit 0 = an unnegated closing reference
# exists, 1 = every reference found is negated, 3 = no textual reference at all
# (a Development-sidebar link, `Fixes owner/repo#N`, `Closes: #N` -- forms the
# regex cannot see). 1 vs 3 matters: a two-state version conflated "never
# mentioned" with "disclaimed" and wrongly reopened issues closed through an
# unseen channel. Step 4 runs it per LINKED_ISSUES candidate over the PR body
# plus the merge commit message, BEFORE `gh issue close`; only exit 1 may reopen.
# #8942: that reopen fired on `state = CLOSED` alone, so an issue closed BEFORE
# this merge (by an earlier PR, or by hand) was reopened and the reopen blamed
# on a merge that never touched it. It is now gated on `loom-daemon merge-pr
# closed-by-merge`: exit 0 = this merge closed it, 1 = it did not, anything
# else (an older daemon's clap 2 included) = no answer and no reopen.
# Rust unit tests cover both predicates (merge_pr/{refs,closed_by_merge}/tests.rs);
# this suite pins the WIRING, so an edit cannot silently drop a check or the gate.
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
assert_doc_contains 'merge-pr closed-by-merge --issue "$issue" --pr "$PR_NUMBER"' "Step 4 asks the daemon whether THIS merge closed the issue (#8942)"
assert_doc_contains '0) gh issue reopen "$issue"' "only the gate's exit 0 reaches the reopen"
assert_doc_contains '*) echo "Issue #$issue: ' "an unanswered gate (older daemon included) logs a line naming the issue"
if grep -qE '= *"?CLOSED"? *\] *&& *gh issue reopen' "$DOC"; then fail "a 'state = CLOSED && gh issue reopen' one-liner is back (#8942)"
else pass "no reopen fires on state = CLOSED alone"; fi

# Order, by line number: negation predicate -> gate -> reopen -> close.
NEGATION_LINE=$(grep -n "has-unnegated-closing-ref" "$DOC" | tail -1 | cut -d: -f1)
GATE_LINE=$(grep -n "closed-by-merge --issue" "$DOC" | tail -1 | cut -d: -f1)
REOPEN_LINE=$(grep -n 'gh issue reopen "\$issue"' "$DOC" | head -1 | cut -d: -f1)
CLOSE_LINE=$(grep -n 'gh issue close "\$issue"' "$DOC" | tail -1 | cut -d: -f1)
if [[ "${NEGATION_LINE:-0}" -gt 0 && "$NEGATION_LINE" -lt "${GATE_LINE:-0}" && "$GATE_LINE" -lt "${REOPEN_LINE:-0}" && "$REOPEN_LINE" -lt "${CLOSE_LINE:-0}" ]]; then
    pass "negation cross-check, then the closed-by-merge gate, then the reopen, all BEFORE gh issue close"
else
    fail "Step 4 order broken (negation=$NEGATION_LINE gate=$GATE_LINE reopen=$REOPEN_LINE close=$CLOSE_LINE)"
fi

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
