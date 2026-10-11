#!/usr/bin/env bash
# test-operator-only-subkind.sh - Regression test for issues #5819 and #5826
#
# `defaults/docs/label-state-machine.md` (#5671) requires that every
# application of `loom:operator-only` carry exactly one sub-kind label
# (`loom:operator-blocked` / `loom:operator-mechanical` /
# `loom:operator-decision`) applied in the SAME
# command — never the base label alone. When only Champion's escalation paths
# honored that rule, a fleet-wide census found 2 of 78 operator-only issues
# across the five busiest repos carrying a sub-kind: the roles were behaving
# correctly per their own prompts, and the unauditable pile grew as
# policy-generated steady state.
#
# #5819 wired the requirement into Curator, Builder, Doctor, and Judge.
# #5826 added a fourth, objective sub-kind (retired again in #11105: an
# unstated objective is filed as a decision, #10000) and reversed
# the old "loom:operator-decision is the safe default when unsure" rule — a
# second fleet-wide measurement found manual clearing at scale only moved the
# operator-only share from 66% to 64.1%, because that "safe default" refilled
# the pile on every sweep. This suite is the mechanism that keeps both wired,
# checking two things:
#
#   1. LINT — no source under defaults/ instructs an agent (or runs code) that
#      applies `loom:operator-only` via `--add-label` without a sub-kind in the
#      same argument. A shell variable (e.g. `$SUB_KIND`) counts as satisfied;
#      Champion's escalation picks its sub-kind at runtime.
#   2. WIRING — each of the five role prompts that can apply the label carries
#      an "Applying `loom:operator-only`" section that names all three
#      sub-kinds (and not the retired objective one), does NOT claim `loom:operator-decision` is a safe
#      unsure-default, states the axis-naming requirement, and repeats the
#      machine-readable `Blocked by #N` requirement that
#      `detect-dependency-cycle.sh` / `warn-operator-gated.sh` parse.
#
# The lint itself is exercised against pass/fail fixtures so it can never pass
# vacuously.
#
# Hermetic: pure file reads plus a mktemp -d fixture dir. No forge/network.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Role prompts, docs, and scripts are all shipped (installed at
# .claude/commands/loom, .loom/docs, .loom/scripts respectively), so resolve
# each the way each layout actually lays it out: the installed path first
# (consumer repos, and Loom's own dogfooded checkout), falling back to the
# defaults/ source-tree path (a bare source checkout with no installed copy
# yet). See issue #6194 / #6241.
if [[ -d "$REPO_ROOT/.claude/commands/loom" ]]; then
    ROLE_DIR="$REPO_ROOT/.claude/commands/loom"
else
    ROLE_DIR="$REPO_ROOT/defaults/.claude/commands/loom"
fi
if [[ -d "$REPO_ROOT/.loom/docs" ]]; then
    DOCS_DIR="$REPO_ROOT/.loom/docs"
else
    DOCS_DIR="$REPO_ROOT/defaults/docs"
fi
if [[ -d "$REPO_ROOT/.loom/scripts" ]]; then
    SCRIPTS_DIR="$REPO_ROOT/.loom/scripts"
else
    SCRIPTS_DIR="$REPO_ROOT/defaults/scripts"
fi
STATE_MACHINE="$DOCS_DIR/label-state-machine.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

SUB_KIND_RE='loom:operator-(blocked|mechanical|decision|objective)'

# Print every offending "<file>:<line>: <text>" — a line that applies
# loom:operator-only via --add-label with no sub-kind (and no shell variable)
# in the same argument. Prints nothing when the file is clean.
scan_file() {
    local file="$1"
    awk -v subkind_re="$SUB_KIND_RE" '
        /--add-label/ && /loom:operator-only/ {
            line = $0
            # Isolate each --add-label argument on the line.
            rest = line
            while (match(rest, /--add-label[ \t]*=?[ \t]*/)) {
                rest = substr(rest, RSTART + RLENGTH)
                arg = rest
                # The argument is the quoted string (or bare token) that follows.
                if (substr(arg, 1, 1) == "\"") {
                    end = index(substr(arg, 2), "\"")
                    arg = (end > 0) ? substr(arg, 2, end - 1) : substr(arg, 2)
                } else if (substr(arg, 1, 1) == "'"'"'") {
                    end = index(substr(arg, 2), "'"'"'")
                    arg = (end > 0) ? substr(arg, 2, end - 1) : substr(arg, 2)
                } else {
                    sub(/[ \t].*$/, "", arg)
                }
                if (arg ~ /loom:operator-only/ && arg !~ subkind_re && arg !~ /\$/) {
                    printf "%s:%d: %s\n", FILENAME, FNR, line
                }
            }
        }
    ' "$file"
}

echo "================================"
echo "test-operator-only-subkind.sh (#5819)"
echo "================================"

# --- Test 1: the lint itself flags a bare application (positive control)
echo ""
echo "Test 1: lint flags a bare loom:operator-only application"
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

cat >"$FIXTURE_DIR/bad.md" <<'EOF'
Route it to a human:

```bash
gh issue edit <number> --add-label "loom:operator-only"
```
EOF
if [[ -n "$(scan_file "$FIXTURE_DIR/bad.md")" ]]; then
    pass "bare --add-label \"loom:operator-only\" is reported"
else
    fail "bare --add-label \"loom:operator-only\" was NOT reported (lint is vacuous)"
fi

cat >"$FIXTURE_DIR/bad2.md" <<'EOF'
gh pr edit "$PR" --add-label loom:blocked --add-label loom:operator-only
EOF
if [[ -n "$(scan_file "$FIXTURE_DIR/bad2.md")" ]]; then
    pass "unquoted bare application in a multi-flag command is reported"
else
    fail "unquoted bare application in a multi-flag command was NOT reported"
fi

# --- Test 2: the lint accepts every legitimate form (negative control)
echo ""
echo "Test 2: lint accepts compliant applications"
cat >"$FIXTURE_DIR/good.md" <<'EOF'
gh issue edit <number> --add-label "loom:operator-only,loom:operator-decision"
gh pr edit "$PR_NUMBER" --add-label "loom:operator-only,loom:operator-mechanical"
gh issue edit "$N" --remove-label "loom:evaluating" --add-label "loom:operator-only,$SUB_KIND"
gh issue list --search "-label:loom:operator-only" --json number
Prose mentioning loom:operator-only without applying it is fine.
EOF
GOOD_HITS="$(scan_file "$FIXTURE_DIR/good.md")"
if [[ -z "$GOOD_HITS" ]]; then
    pass "comma-paired, runtime-variable, and filter-only forms all accepted"
else
    fail "false positive on a compliant form:"$'\n'"$GOOD_HITS"
fi

# --- Test 3: no bare application anywhere under defaults/
echo ""
echo "Test 3: no source under defaults/ applies loom:operator-only bare"
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
    pass "every --add-label application under defaults/ carries a sub-kind"
else
    fail "bare loom:operator-only application(s) found:"$'\n'"$VIOLATIONS"
fi

# --- Test 4-8: each role prompt that can apply the label is wired
echo ""
echo "Test 4: role prompts carry the sub-kind requirement"
for role in curator builder doctor judge; do
    f="$ROLE_DIR/$role.md"
    if [[ ! -f "$f" ]]; then
        fail "$role.md not found at $f"
        continue
    fi
    if grep -qE '^#+ Applying `loom:operator-only`' "$f"; then
        pass "$role.md has an \"Applying \`loom:operator-only\`\" section"
    else
        fail "$role.md is missing the \"Applying \`loom:operator-only\`\" section"
    fi
    missing=""
    for sub in loom:operator-blocked loom:operator-mechanical loom:operator-decision; do
        grep -q "$sub" "$f" || missing+=" $sub"
    done
    if [[ -z "$missing" ]]; then
        pass "$role.md names all three sub-kinds"
    else
        fail "$role.md does not name:$missing"
    fi
    # Split literal: keeps a repo-wide `git grep` for the retired name clean.
    if grep -q 'loom:operator-objec''tive' "$f"; then
        fail "$role.md still names the objective sub-kind retired in #11105"
    else
        pass "$role.md does not name the retired objective sub-kind"
    fi
    # The pre-#5826 rule was carried by these two literal phrases (a table
    # cell and a prose sentence, each self-contained on its own line in every
    # role prompt at the time). Key on their literal absence rather than a
    # cross-line proximity regex, which is fragile to how a given file wraps
    # "loom:operator-decision" relative to "safe default" in the surrounding
    # sentence.
    if grep -qiE 'is always safe to over-apply|safe default whenever the kind is not obvious' "$f"; then
        fail "$role.md still claims loom:operator-decision is a safe unsure-default (#5826 reversed this)"
    else
        pass "$role.md does not claim loom:operator-decision is a safe unsure-default"
    fi
    if grep -qiE 'name the (disagreement )?axis' "$f"; then
        pass "$role.md states the axis-naming requirement for loom:operator-decision"
    else
        fail "$role.md is missing the axis-naming requirement for loom:operator-decision"
    fi
    if grep -qiE 'candidate objectives' "$f"; then
        pass "$role.md routes an unstated objective to a decision over candidate objectives"
    else
        fail "$role.md does not route an unstated objective to a decision over candidate objectives"
    fi
    if grep -qE 'Blocked by #N' "$f" && grep -qE 'Depends on #N|Requires #N' "$f"; then
        pass "$role.md repeats the machine-readable blocker requirement"
    else
        fail "$role.md is missing the machine-readable \`Blocked by #N\` requirement"
    fi
done

echo ""
echo "Test 5: builder-complexity.md routes size findings to loom:blocked, not operator-only"
BC="$ROLE_DIR/builder-complexity.md"
# The paired form is either a literal `--add-label` pair or the #9344
# helper, which adds loom:operator-decision with `--also-label loom:operator-only`
# in the same label write.
if grep -q 'loom:operator-only' "$BC" && grep -qE 'loom:operator-only,loom:operator-(blocked|mechanical|decision)|operator-decision apply .*--also-label loom:operator-only' "$BC"; then
    pass "builder-complexity.md shows the paired-label form"
else
    fail "builder-complexity.md does not show the paired-label form"
fi
if grep -qiE 'Size is not a routing signal|terminal state for a decomposition' "$BC"; then
    pass "builder-complexity.md keeps decomposition on loom:blocked"
else
    fail "builder-complexity.md does not distinguish loom:blocked from loom:operator-only"
fi

echo ""
echo "Test 6: label-state-machine.md credits every wired role"
if [[ -f "$STATE_MACHINE" ]]; then
    WIRED_SECTION="$(awk '/^\*\*Where this is wired today\*\*/,/^## /' "$STATE_MACHINE")"
    for role in Champion Curator Builder Judge Doctor; do
        if grep -q "| $role |" <<<"$WIRED_SECTION"; then
            pass "\"Where this is wired today\" lists $role"
        else
            fail "\"Where this is wired today\" does not list $role"
        fi
    done
else
    fail "label-state-machine.md not found at $STATE_MACHINE"
fi

echo ""
echo "Test 7: sub-kinds stay additive (base label never removed/replaced)"
# The one legitimate removal — Champion's self-healing un-escalation in
# classify-dependency-block.sh (#5664) — removes via "$OPERATOR_ONLY_LABEL",
# not a literal, so it is out of this check by construction.
BAD_REMOVE="$(grep -rnE -- '--remove-label[ \t]*=?[ \t]*"?[^"]*loom:operator-only' \
    "$ROLE_DIR" "$SCRIPTS_DIR"/*.sh 2>/dev/null || true)"
if [[ -z "$BAD_REMOVE" ]]; then
    pass "no role prompt or script literally removes the base label"
else
    fail "unexpected --remove-label of the base label:"$'\n'"$BAD_REMOVE"
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
