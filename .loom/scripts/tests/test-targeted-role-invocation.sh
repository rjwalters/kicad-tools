#!/usr/bin/env bash
# test-targeted-role-invocation.sh - Prompt-lint for issue #11863.
#
# The daemon invokes item roles with a target (`/loom:<role> <N>`): Judge and
# Doctor through the per-repository lanes today, every item role under step
# dispatch. Each role's targeted path ("## Argument Handling") skips "Finding
# Work", so every gate that discovery applies must be repeated there, before
# the first write:
#
#   Judge   - one REST read refuses an issue number, a closed PR and
#             loom:frozen; a fallback PR follows the Fallback Queue rules;
#             the claim (lease, then label) comes after the gate.
#   Curator - pool gate, then the kind probe, then the loom:curating claim;
#             the probe also refuses closed / frozen / hard-excluded.
#   Builder - `forge check-claim` before the loom:building claim, with the
#             never-overridable tokens refusing and the invoker's own claim
#             allowed.
#
# Each path ends with the LOOM_STEP_OUTCOME line, whose classes live in
# defaults/docs/targeted-role-invocation.md. A later trim could silently drop
# a gate, so this suite asserts each one is present and ordered, in the
# defaults/ source and in the generated agent-skill copies.
#
# Hermetic: reads files only - no forge, network, or loom-daemon binary.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CMDS="$REPO_ROOT/defaults/.claude/commands/loom"
SKILLS="$REPO_ROOT/defaults/.agents/skills"
DOC="$REPO_ROOT/defaults/docs/targeted-role-invocation.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

for f in "$CMDS/judge.md" "$CMDS/curator.md" "$CMDS/builder.md" "$DOC" \
    "$SKILLS/loom-judge/SKILL.md" "$SKILLS/loom-curator/SKILL.md" "$SKILLS/loom-builder/SKILL.md"; do
    if [[ ! -f "$f" ]]; then
        echo -e "${RED}FAIL${NC}: missing $f" >&2
        exit 1
    fi
done

# args <file> - print the "## Argument Handling" section only, so a match
# elsewhere in a 2000-line prompt cannot satisfy an assertion.
args() {
    awk '
        $0 ~ "^## " { if (f) exit; if ($0 ~ /^## Argument Handling/) f = 1 }
        f { print }
    ' "$1"
}

# expect <label> <haystack> <fixed-string>
expect() {
    if grep -qF -- "$3" <<< "$2"; then pass "$1"; else fail "$1 (missing: $3)"; fi
}

# before <label> <haystack> <first> <second> - <first> appears on an earlier
# line than <second>.
before() {
    local a b
    a="$(grep -nF -- "$3" <<< "$2" | head -1 | cut -d: -f1)"
    b="$(grep -nF -- "$4" <<< "$2" | head -1 | cut -d: -f1)"
    if [[ -n "$a" && -n "$b" && "$a" -lt "$b" ]]; then
        pass "$1"
    else
        fail "$1 (first=$a second=$b)"
    fi
}

PROBE="(.pull_request != null), .state, ([.labels[].name]"

check_judge() {
    local J="$1" where="$2"
    expect "$where judge: REST kind/state/label probe" "$J" "$PROBE"
    expect "$where judge: an issue number refuses"     "$J" "\`false\` (an issue number; Judge is a PR role)"
    expect "$where judge: closed and frozen refuse"    "$J" "\`closed\`, \`loom:frozen\`, or a failed read"
    expect "$where judge: fallback PRs stay review-only" "$J" "\"Fallback Queue\" rules (guard, review only, never claim or label)"
    expect "$where judge: a priority label alone is unlabelled" "$J" "Neither those nor a verdict label (e.g. only \`loom:important\`)"
    expect "$where judge: a judged PR is a no-op"      "$J" "A verdict label without \`loom:review-requested\` → no-op"
    expect "$where judge: outcome line"                "$J" "LOOM_STEP_OUTCOME role=judge"
    before "$where judge: probe precedes the claim"    "$J" "$PROBE" "claim exactly as Primary Queue step 2"
}

check_curator() {
    local C="$1" where="$2"
    expect "$where curator: pool gate runs first"      "$C" "FIRST, run the pool gate (above) and the kind probe, then claim"
    expect "$where curator: REST kind/state/label probe" "$C" "$PROBE"
    expect "$where curator: closed/frozen/hard-excluded refuse" "$C" "So do \`closed\`, \`loom:frozen\` and a hard-exclusion label"
    expect "$where curator: outcome line"              "$C" "LOOM_STEP_OUTCOME role=curator"
    before "$where curator: probe precedes the claim"  "$C" "$PROBE" "labels[]=loom:curating"
}

check_builder() {
    local B="$1" where="$2"
    expect "$where builder: check-claim guard"         "$B" "loom-daemon forge check-claim <number>"
    for tok in TARGET_IS_PR CLOSED FROZEN OPEN_PR; do
        expect "$where builder: $tok refuses"          "$B" "\`$tok\`"
    done
    expect "$where builder: invoker's own claim allowed" "$B" "refuse unless your invoker holds this claim (\`LOOM_SWEEP_CLAIM_OWNED\`"
    expect "$where builder: the sweep brief is an ownership signal" "$B" "your brief says \`This /loom:sweep pre-flighted #<number>\`"
    expect "$where builder: operator override is claim-only" "$B" "that overrides these three, as \`--force-claim\` does, never the four above"
    expect "$where builder: other exits are not an all-clear" "$B" "Any other exit is not an all-clear"
    expect "$where builder: outcome line"              "$B" "LOOM_STEP_OUTCOME role=builder"
    before "$where builder: guard precedes the claim"  "$B" "forge check-claim <number>" "--add-label \"loom:building\""
}

echo "Test group 1: role prompts (defaults/.claude/commands/loom)"
check_judge   "$(args "$CMDS/judge.md")"   "prompt"
check_curator "$(args "$CMDS/curator.md")" "prompt"
check_builder "$(args "$CMDS/builder.md")" "prompt"

echo ""
echo "Test group 2: generated agent-skill copies carry the same gates"
check_judge   "$(args "$SKILLS/loom-judge/SKILL.md")"   "skill"
check_curator "$(args "$SKILLS/loom-curator/SKILL.md")" "skill"
check_builder "$(args "$SKILLS/loom-builder/SKILL.md")" "skill"

echo ""
echo "Test group 3: an attended sweep tells its Task Builders the claim is theirs"
# worker run --role builder exports LOOM_SWEEP_CLAIM_OWNED itself (pinned by
# worker_spawn::claim_owned's builder_worker_is_told_its_sweep_owns_the_claim); a Task
# subagent cannot get a per-subagent env (#3719), so the brief must say it.
SWL="$(cat "$CMDS/sweep-wave-lifecycle.md")"
expect "sweep step 4 requires the ownership sentence"  "$SWL" "\`This /loom:sweep pre-flighted #N and holds its claim and lease.\`"
before "the requirement sits in the Builder dispatch step" "$SWL" "### 4. Builder phase" "This /loom:sweep pre-flighted #N"

echo ""
echo "Test group 4: the outcome contract doc"
D="$(cat "$DOC")"
for cls in verdict noop refused failed; do
    expect "doc defines class $cls"                    "$D" "| \`$cls\` |"
done
expect "doc: forge post-state is authoritative"        "$D" "The forge post-state is authoritative."
expect "doc: outcome line format"                      "$D" "LOOM_STEP_OUTCOME role=<judge|curator|builder> target=<N> class=<verdict|noop|refused|failed> reason=<token>"
expect "doc: names where the loom:operator skip goes"  "$D" "Where the \`loom:operator\` skip goes"
for tok in target_is_pr target_is_issue pool_gate frozen closed claimed probe_failed; do
    expect "doc: reason token $tok"                    "$D" "\`$tok\`"
done

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
