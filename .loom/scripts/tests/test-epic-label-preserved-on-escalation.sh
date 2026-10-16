#!/usr/bin/env bash
# test-epic-label-preserved-on-escalation.sh - Regression test for issue #6715
#
# THE FAILURE MODE THIS GUARDS AGAINST
#
# #6715 originally reported that Champion's epic-escalation path
# (champion-epic.md, "Step 4: Reject" -> escalate-to-operator branch) strips
# `loom:epic` when it routes an unrevised epic to
# `loom:operator` -- which would leave the epic
# permanently invisible to every downstream Champion pass (epics are
# discovered BY the `loom:epic` label), even after a human revises it, since
# nothing would ever re-apply the label.
#
# Curator investigation for #6715 found the hypothesis does NOT reproduce
# against this repo's current `defaults/`: the escalation is add-only (since
# #10098 a `loom-daemon operator-decision apply` call that passes no
# `--remove-label` at all). This suite is the anti-regression mechanism that
# keeps it that way, modeled directly on
# defaults/scripts/tests/test-operator-hold-routing.sh's pattern:
#
#   1. LINT -- no `--remove-label` anywhere under defaults/ ever targets
#      `loom:epic` (a static grep, exercised against pass/fail fixtures so it
#      can never pass vacuously).
#   2. WIRING -- champion-epic.md states the preservation requirement in
#      prose near the escalation block (Step 4's `operator-decision apply`), so a
#      future edit to that block cannot silently reintroduce the bug without
#      also having to touch documentation that contradicts the change.
#
# Hermetic: pure file reads plus a mktemp -d fixture dir. No forge/network.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Role prompts are shipped (installed at .claude/commands/loom), so resolve
# the way each layout actually lays it out: the installed path first
# (consumer repos, and Loom's own dogfooded checkout), falling back to the
# defaults/ source-tree path (a bare source checkout with no installed copy
# yet). See issue #6194 / #6241 (same convention as
# test-operator-hold-routing.sh).
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi
if [[ -d "$REPO_ROOT/.loom/scripts" ]]; then
    SCRIPTS_DIR="$REPO_ROOT/.loom/scripts"
else
    SCRIPTS_DIR="$REPO_ROOT/defaults/scripts"
fi
if [[ -d "$REPO_ROOT/.loom/docs" ]]; then
    DOCS_DIR="$REPO_ROOT/.loom/docs"
else
    DOCS_DIR="$REPO_ROOT/defaults/docs"
fi

CHAMPION_EPIC="$ROLE_DIR/champion-epic.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Print every offending "<file>:<line>: <text>" -- a line where a
# `--remove-label` argument's token list literally contains `loom:epic`.
# Mirrors test-operator-hold-routing.sh's per-argument isolation so a
# multi-flag command (`--remove-label "loom:building" --add-label
# "loom:epic"`, which does NOT remove the epic label) is not a false
# positive.
scan_file() {
    local file="$1"
    awk '
        /--remove-label/ {
            line = $0
            rest = line
            while (match(rest, /--remove-label[ \t]*=?[ \t]*/)) {
                rest = substr(rest, RSTART + RLENGTH)
                arg = rest
                if (substr(arg, 1, 1) == "\"") {
                    end = index(substr(arg, 2), "\"")
                    arg = (end > 0) ? substr(arg, 2, end - 1) : substr(arg, 2)
                } else if (substr(arg, 1, 1) == "'"'"'") {
                    end = index(substr(arg, 2), "'"'"'")
                    arg = (end > 0) ? substr(arg, 2, end - 1) : substr(arg, 2)
                } else {
                    sub(/[ \t].*$/, "", arg)
                }
                # Split the argument on commas (--remove-label supports a
                # comma-separated list) and flag an exact "loom:epic" token.
                n = split(arg, tokens, ",")
                for (i = 1; i <= n; i++) {
                    if (tokens[i] == "loom:epic") {
                        printf "%s:%d: %s\n", FILENAME, FNR, line
                    }
                }
            }
        }
    ' "$file"
}

echo "================================"
echo "test-epic-label-preserved-on-escalation.sh (#6715)"
echo "================================"

# --- Test 1: the lint itself flags a reintroduced removal (positive control)
echo ""
echo "Test 1: lint flags --remove-label targeting loom:epic"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

cat >"$FIXTURE_DIR/bad.md" <<'EOF'
Escalate to the operator:

```bash
gh issue edit <number> --remove-label "loom:epic" --add-label "loom:operator"
```
EOF
if [[ -n "$(scan_file "$FIXTURE_DIR/bad.md")" ]]; then
    pass "--remove-label \"loom:epic\" (standalone) is reported"
else
    fail "--remove-label \"loom:epic\" (standalone) was NOT reported (lint is vacuous)"
fi

cat >"$FIXTURE_DIR/bad2.md" <<'EOF'
gh issue edit "$N" --remove-label "loom:building,loom:epic" --add-label "loom:operator"
EOF
if [[ -n "$(scan_file "$FIXTURE_DIR/bad2.md")" ]]; then
    pass "--remove-label with loom:epic in a comma-separated list is reported"
else
    fail "--remove-label with loom:epic in a comma-separated list was NOT reported"
fi

# --- Test 2: the lint accepts every legitimate form (negative control)
echo ""
echo "Test 2: lint accepts compliant (add-only, or unrelated removal) forms"
cat >"$FIXTURE_DIR/good.md" <<'EOF'
gh issue edit <number> --add-label "loom:operator"
gh issue edit <number> --remove-label "loom:building" --add-label "loom:epic"
gh issue edit "$N" --remove-label "loom:issue" --add-label "loom:building"
Prose mentioning loom:epic without a --remove-label flag is fine.
EOF
GOOD_HITS="$(scan_file "$FIXTURE_DIR/good.md")"
if [[ -z "$GOOD_HITS" ]]; then
    pass "add-only and unrelated-removal forms are accepted"
else
    fail "false positive on a compliant form:"$'\n'"$GOOD_HITS"
fi

# --- Test 3: no source under defaults/ removes loom:epic
echo ""
echo "Test 3: no source under defaults/ ever removes loom:epic"
VIOLATIONS=""
while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    hits="$(scan_file "$f")"
    [[ -n "$hits" ]] && VIOLATIONS+="$hits"$'\n'
done < <(
    find "$ROLE_DIR" -maxdepth 1 -name '*.md' 2>/dev/null
    find "$DOCS_DIR" -maxdepth 1 -name '*.md' 2>/dev/null
    find "$SCRIPTS_DIR" -maxdepth 1 -name '*.sh' 2>/dev/null
)
if [[ -z "${VIOLATIONS//[$'\n' ]/}" ]]; then
    pass "no --remove-label under defaults/ ever targets loom:epic"
else
    fail "--remove-label of loom:epic found:"$'\n'"$VIOLATIONS"
fi

# --- Test 4: fixture-level end-to-end -- FAIL against a reintroduced bug,
#     PASS against the current shipped escalation block (the issue's own
#     test plan requirement).
echo ""
echo "Test 4: escalation block itself is add-only (fails on a reintroduced regression)"
if [[ -f "$CHAMPION_EPIC" ]]; then
    # Isolate the escalation code block: from the ESCALATE_MARKER assignment
    # to the closing \`\`\` fence that follows it.
    ESCALATION_BLOCK="$(awk '/ESCALATE_MARKER=/{flag=1} flag{print} flag && /^```$/ && NR>1 && seen{exit} flag && /^```$/{seen=1}' "$CHAMPION_EPIC")"
    if [[ -z "$ESCALATION_BLOCK" ]]; then
        fail "could not locate the escalation code block (ESCALATE_MARKER=... fence) in $CHAMPION_EPIC"
    elif grep -qE -- '--remove-label[ \t]*=?[ \t]*"?loom:epic\b' <<<"$ESCALATION_BLOCK"; then
        fail "the shipped escalation block removes loom:epic -- regression reintroduced!"
    else
        pass "the shipped escalation block is add-only (does not remove loom:epic)"
    fi

    # Deliberately-reintroduced fixture copy of the escalation block, per the
    # issue's test plan: this MUST fail the same check above.
    BUGGY_BLOCK='ESCALATE_MARKER="<!-- champion:epic-escalated -->"
gh issue comment <number> --body "$ESCALATE_MARKER" \
  && gh issue edit <number> --remove-label "loom:epic" --add-label "loom:operator"'
    if grep -qE -- '--remove-label[ \t]*=?[ \t]*"?loom:epic\b' <<<"$BUGGY_BLOCK"; then
        pass "the same check correctly FAILS a deliberately-reintroduced regression fixture"
    else
        fail "the check did not catch the deliberately-reintroduced regression fixture (test is broken)"
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# --- Test 5: WIRING -- champion-epic.md states the preservation requirement
#     in prose near the escalation block.
echo ""
echo "Test 5: champion-epic.md documents the anti-regression requirement near the escalation block"
if [[ -f "$CHAMPION_EPIC" ]]; then
    MARKER_LINE="$(grep -n 'ESCALATE_MARKER=' "$CHAMPION_EPIC" | head -1 | cut -d: -f1)"
    if [[ -z "$MARKER_LINE" ]]; then
        fail "could not find the ESCALATE_MARKER= line to anchor the wiring check"
    else
        # Look in a window around the escalation block (60 lines before/after)
        # for prose naming both loom:epic and a MUST-NOT / must-not-target
        # framing referencing --remove-label.
        START=$(( MARKER_LINE > 60 ? MARKER_LINE - 60 : 1 ))
        END=$(( MARKER_LINE + 60 ))
        WINDOW="$(sed -n "${START},${END}p" "$CHAMPION_EPIC")"
        if grep -qiE 'must not.*(--remove-label|remove.*loom:epic)|never target `?loom:epic`?' <<<"$WINDOW" \
            && grep -q 'loom:epic' <<<"$WINDOW"; then
            pass "escalation block's surrounding prose states the loom:epic preservation requirement"
        else
            fail "escalation block's surrounding prose does not state the loom:epic preservation requirement"
        fi
    fi
else
    fail "champion-epic.md not found at $CHAMPION_EPIC"
fi

# ---------------------------------------------------------------------------
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
