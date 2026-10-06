#!/usr/bin/env bash
# test-guide-unblock-merged-blocker.sh - Regression test for issue #10040
#
# Guide's issue-side unblock loop (check_and_unblock) only treated a blocker
# as resolved when `gh issue view <N> --json state` returned CLOSED. For a
# blocker that is a PR, gh reports a merged PR as MERGED, so issues parked on
# a PR never auto-unblocked. The loop now accepts CLOSED or MERGED, matching
# check_and_unblock_prs in defaults/docs/park-record.md.
#
# The `for dep in $deps` loop is extracted VERBATIM from each shipped copy
# (guide.md and the loom-guide SKILL.md) and run against a stubbed `gh`.

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

# Stub gh: blocker number -> state.
gh() {
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        case "$3" in
            1) echo "MERGED" ;;
            2) echo "CLOSED" ;;
            3) echo "OPEN" ;;
            4) echo "UNKNOWN" ;;
            5) return 1 ;;   # gh failure -> UNKNOWN fallback
            *) echo "OPEN" ;;
        esac
        return 0
    fi
    return 1
}

for copy in \
    "defaults/.claude/commands/loom/guide.md" \
    "defaults/.agents/skills/loom-guide/SKILL.md"; do
    f="$REPO_ROOT/$copy"
    echo "Copy: $copy"
    if [[ ! -f "$f" ]]; then fail "$copy exists"; continue; fi

    LOOP="$(sed -n '/^    for dep in \$deps; do$/,/^    done$/p' "$f")"
    if [[ -z "$LOOP" ]]; then fail "extract dep loop from $copy"; continue; fi
    pass "extracted dep loop verbatim"

    if grep -q '"\$state" != "MERGED"' <<<"$LOOP"; then
        pass "loop accepts MERGED"
    else
        fail "loop accepts MERGED"
    fi

    # Wrap the extracted loop; report all_resolved for a given dep list.
    eval "resolve() {
        local deps=\"\$1\" all_resolved=true resolved_deps=\"\"
$LOOP
        echo \"\$all_resolved\"
    }"

    assert_eq "$(resolve 1)" "true"  "merged PR blocker is resolved"
    assert_eq "$(resolve 2)" "true"  "closed blocker is resolved"
    assert_eq "$(resolve 3)" "false" "open blocker is unresolved"
    assert_eq "$(resolve 4)" "false" "UNKNOWN blocker is unresolved"
    assert_eq "$(resolve 5)" "false" "gh failure is unresolved"
    assert_eq "$(resolve '1 2')" "true"  "merged + closed all resolved"
    assert_eq "$(resolve '1 3')" "false" "merged + open not resolved"
done

echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
