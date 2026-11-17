#!/usr/bin/env bash
# test-ci-wait-prompt-consistency.sh - Prompt-lint for issue #10574.
#
# Judge and Doctor must agree about CI waits:
#   - Judge's pending-CI skip check uses the --timeout 20 snapshot, never
#     --timeout 0 (which reads a no-checks repo as TIMEOUT).
#   - Doctor hands back to Judge on pending CI after a push; the bounded
#     --timeout 1200 foreground wait is only for an invocation that
#     explicitly requires the settled result.
#   - An unreadable required-context lookup is unknown, never green.
#
# Hermetic: reads files only - no forge, network, or loom-daemon binary.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/lib/assert.sh"

check() {
    local J="$1" D="$2" where="$3"
    for f in "$J" "$D"; do
        [[ -f "$f" ]] || { echo -e "${RED}FAIL${NC}: missing $f" >&2; exit 1; }
    done

    # No-checks repo: no executable --timeout 0 wait-checks call remains.
    if grep -E 'wait-checks .*--timeout 0( |$)' "$J" | grep -vq '^\*\*Step 1'; then
        fail "$where judge: no wait-checks call uses --timeout 0"
    else
        pass "$where judge: no wait-checks call uses --timeout 0"
    fi
    if grep -qF 'wait-checks <PR_NUMBER> --timeout 20 2>/dev/null | head -n1' "$J"; then
        pass "$where judge: pending-CI skip check uses the --timeout 20 snapshot"
    else
        fail "$where judge: pending-CI skip check uses the --timeout 20 snapshot"
    fi
    for k in 'LOOM-CHECKS-TIMEOUT*)' 'LOOM-CHECKS-RED*)' 'LOOM-CHECKS-GREEN*|LOOM-CHECKS-NONE*)'; do
        grep -qF -- "$k" "$J" && pass "$where judge: skip-check branch kept: $k" || fail "$where judge: skip-check branch kept: $k"
    done

    # Unknown required contexts.
    grep -qF 'Unknown is never green.' "$J" && grep -qF 'required-contexts-unknown' "$J" \
        && pass "$where judge: unreadable required contexts are unknown, never green" \
        || fail "$where judge: unreadable required contexts are unknown, never green"
    grep -qF 'leaves the result *unknown*, never green' "$D" \
        && pass "$where doctor: unreadable required contexts are unknown" \
        || fail "$where doctor: unreadable required contexts are unknown"

    # Pending CI after a Doctor push: Step 10 hands back, does not point at the 1200s wait.
    local s10
    s10="$(grep -F '10. **Verify CI remotely**' "$D")"
    if grep -qF 'else CI Assessment Step 5' <<< "$s10"; then
        fail "$where doctor: step 10 no longer routes not-green to the 1200s wait"
    else
        pass "$where doctor: step 10 no longer routes not-green to the 1200s wait"
    fi
    grep -qF 'hand back to Judge in step 11' <<< "$s10" \
        && pass "$where doctor: step 10 hands pending CI back to Judge" \
        || fail "$where doctor: step 10 hands pending CI back to Judge"

    # CI Assessment Step 5: 1200 is not the unconditional call; attended path retained.
    local step5
    step5="$(awk '/^### Step 5: Verify Remote CI After Push/{f=1;next} /^### /{f=0} f' "$D")"
    if grep -E '^loom-daemon forge wait-checks .*--timeout 1200' <<< "$step5" | grep -q .; then
        fail "$where doctor: step 5 has no unconditional --timeout 1200"
    else
        pass "$where doctor: step 5 has no unconditional --timeout 1200"
    fi
    grep -qF 'explicitly requires the settled result' <<< "$step5" \
        && pass "$where doctor: step 5 keeps the explicit attended-wait path" \
        || fail "$where doctor: step 5 keeps the explicit attended-wait path"
    grep -qF -- '--timeout 1200 2>"$err"' "$D" \
        && pass "$where doctor: bounded foreground wait recipe retained" \
        || fail "$where doctor: bounded foreground wait recipe retained"
}

C="$REPO_ROOT/defaults/.claude/commands/loom"
S="$REPO_ROOT/defaults/.agents/skills"
check "$C/judge.md" "$C/doctor.md" "commands"
check "$S/loom-judge/SKILL.md" "$S/loom-doctor/SKILL.md" "skills"

echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
