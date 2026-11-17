#!/usr/bin/env bash
# test-guide-park-record-precedence.sh - Regression test for issue #13414
#
# Guide's unblock sweep lifted operator-ruled parks because unchecked
# acceptance-criterion `- [ ] #N` lines counted as gates. A park record now wins.
# parse_dependencies / has_restore_note are extracted VERBATIM from each shipped
# copy and run against fixtures (and a stubbed loom-daemon).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; }
assert_eq() { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1', expected '$2')"; fi; }

STUB_COMMENTS='[]'
loom-daemon() { printf '%s\n' "$STUB_COMMENTS"; }

for copy in "defaults/.claude/commands/loom/guide.md" "defaults/.agents/skills/loom-guide/SKILL.md"; do
    f="$REPO_ROOT/$copy"; echo "Copy: $copy"
    [[ -f "$f" ]] || { fail "$copy exists"; continue; }
    PD="$(sed -n '/^parse_dependencies() {$/,/^}$/p' "$f")"
    HR="$(sed -n '/^has_restore_note() {/,/^}$/p' "$f")"
    [[ -n "$PD" && -n "$HR" ]] || { fail "extract functions from $copy"; continue; }
    eval "$PD"; eval "$HR"

    AC=$'## Acceptance Criteria\n- [ ] #100: something closed\n'
    PARK=$'<!-- loom:park Blocked by: #200 by=builder at=2026-10-31T00:00:00Z reason="see #300" -->'
    assert_eq "$(parse_dependencies "$AC$PARK")" "200" "park record wins over AC checkbox; reason refs ignored"
    assert_eq "$(parse_dependencies "$AC")" "100" "no park record: checkbox dep still parsed"
    assert_eq "$(parse_dependencies $'- [ ] example-org/tool-repo#55: x')" "example-org/tool-repo#55" "cross-repo ref keeps owner prefix"
    assert_eq "$(parse_dependencies $'<!-- loom:park Blocked by: (unstated) by=daemon at=x -->\n- [ ] #100')" "" "unstated park record gates nothing from AC"

    STUB_COMMENTS='[{"body":"old"},{"body":"Restored hold.\n<!-- loom:hold-restored -->"}]'
    assert_eq "$(has_restore_note 1)" "true" "latest trusted comment restore note => skip"
    STUB_COMMENTS='[{"body":"<!-- loom:hold-restored -->"},{"body":"later comment"}]'
    assert_eq "$(has_restore_note 1)" "false" "restore note superseded by later comment"
done
echo "Tests run: $TESTS_RUN"
[[ $TESTS_FAILED -eq 0 ]] && echo "All tests passed" || { echo "Tests failed: $TESTS_FAILED"; exit 1; }
