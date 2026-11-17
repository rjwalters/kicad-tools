#!/usr/bin/env bash
# test-guide-split-parent-unblock.sh - Regression test for issue #13635
#
# Guide's check_and_unblock restored `loom:issue` on any issue whose label
# history ever contained it, so a split tracker (an operator's accidental
# +loom:issue/-loom:issue) was promoted to a Builder once its children closed.
# The restore is now gated on `loom-daemon forge parent split-state`.
#
# The label-restore block is extracted VERBATIM from each shipped copy
# (guide.md and the loom-guide SKILL.md) and run against stubs.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1', expected '$2')"; fi
}

# Stubs. SPLIT_RC / SPLIT_OUT drive `loom-daemon`; APPROVED drives
# was_previously_approved; EDITS collects every `gh issue edit` call.
EDITS=""
loom-daemon() { echo "$SPLIT_OUT"; return "$SPLIT_RC"; }
gh() { [[ "$1 $2" == "issue edit" ]] && EDITS="$EDITS|$*"; return 0; }
was_previously_approved() { echo "$APPROVED"; }

POSTED=""

for copy in \
    "defaults/.claude/commands/loom/guide.md" \
    "defaults/.agents/skills/loom-guide/SKILL.md"; do
    f="$REPO_ROOT/$copy"
    echo "Copy: $copy"
    if [[ ! -f "$f" ]]; then fail "$copy exists"; continue; fi

    BLOCK="$(sed -n '/^      split_out=\$(loom-daemon forge parent split-state/,/^      fi$/p' "$f")"
    if [[ -z "$BLOCK" ]]; then fail "extract split-parent block from $copy"; continue; fi
    pass "extracted split-parent block verbatim"

    # post-comment.sh is invoked by path; shadow it via a function-free stub
    # by rewriting the call in the extracted text only.
    BLOCK="${BLOCK//.\/.loom\/scripts\/post-comment.sh/post_comment}"
    post_comment() { POSTED="$POSTED|$*"; }
    eval "run() {
        local number=7 title=T resolved_deps=' #1'
$BLOCK
    }"

    run_case() { # rc out approved
        EDITS=""; POSTED=""; SPLIT_RC="$1"; SPLIT_OUT="$2"; APPROVED="$3"
        run >/dev/null 2>&1
    }

    run_case 0 "SPLIT_ALL_CLOSED #11 #12" true
    [[ "$EDITS" != *"--add-label loom:issue"* ]] && pass "split parent, all closed (previously approved): loom:issue NOT added" \
        || fail "split parent was promoted: $EDITS"
    [[ "$EDITS" == *"--remove-label loom:blocked"* ]] && pass "split parent: loom:blocked removed" || fail "loom:blocked not removed: $EDITS"
    [[ "$POSTED" == *"#11 #12"* ]] && pass "comment names the children" || fail "comment lacks children: $POSTED"

    run_case 1 "NOT_SPLIT" true
    [[ "$EDITS" == *"--add-label loom:issue"* ]] && pass "ordinary previously-approved issue: loom:issue restored" \
        || fail "ordinary issue not restored: $EDITS"

    run_case 1 "NOT_SPLIT" false
    [[ "$EDITS" != *"--add-label loom:issue"* ]] && pass "never-approved issue: loom:issue not added" \
        || fail "never-approved issue promoted: $EDITS"

    run_case 2 "" true
    [[ "$EDITS" != *"--add-label loom:issue"* ]] && pass "unreadable children fail closed: no promotion" \
        || fail "unreadable children promoted: $EDITS"
    [[ "$EDITS" == *"--remove-label loom:blocked"* ]] && pass "unreadable children: loom:blocked still cleared" || fail "loom:blocked kept: $EDITS"
done

echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo "Tests failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
