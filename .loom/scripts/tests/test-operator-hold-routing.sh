#!/usr/bin/env bash
# test-operator-hold-routing.sh - Regression test for the hold-taxonomy
# collapse (#11795), replacing test-operator-only-subkind.sh (#5819, #5826).
#
# Loom's holds are three labels: `loom:blocked` (waiting on something
# automation resolves), `loom:operator` (a human must act or decide; the BODY
# says which: a `decision` block is a ruling, a capability marker is a
# human-hands step) and `loom:frozen` (not now). #11795 retired five labels
# into them: loom:operator-only, -decision, -mechanical, -blocked, and
# loom:needs-capability. Before the collapse, the rule for applying an
# operator hold was restated in five role prompts (curator, builder, judge,
# doctor, builder-complexity), each with its own sub-kind table; it now lives
# once, in `label-state-machine.md` → "Routing to a human", and every role
# points there in one paragraph.
#
# This suite keeps that true:
#
#   1. LINT — no source under defaults/ (role prompts, docs, scripts) APPLIES
#      a retired hold label: `--add-label` / `--also-label` / a
#      `gh api .../labels -f 'labels[]=...'` write naming one. Mentioning a
#      retired name (the mapping table, history) is fine; applying it fails
#      at runtime once `sync-labels.sh` no longer creates it.
#   2. SHARED RULE — label-state-machine.md's "Routing to a human" names the
#      three routes (operator-decision apply, capability marker, loom:blocked
#      with a park record), the `Blocked by #N` requirement, the
#      falsifiability axis test and the candidate-objectives rule, and does
#      not call a decision a safe default.
#   3. POINTERS — each role prompt that can route to a human points at that
#      section and does NOT restate a sub-kind table.
#
# Hermetic: pure file reads plus a mktemp -d fixture dir. No forge/network.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Installed layout first (consumer repos, Loom's own dogfooded checkout), then
# the defaults/ source tree (#6194 / #6241).
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

# The retired names, built from split literals so a repo-wide `git grep` for
# a retired label does not count this suite as a live use.
OP='loom:operator'
RETIRED_RE="(${OP}-(only|decision|mechanical|blocked)|loom:needs-capa""bility)([^a-z-]|$)"

# Print every "<file>:<line>: <text>" that applies a retired hold label.
scan_file() {
    grep -nE -- "(--add-label|--also-label|labels\\[\\]=)" "$1" 2>/dev/null \
        | grep -E -- "$RETIRED_RE" \
        | sed "s|^|$1:|"
}

echo "================================"
echo "test-operator-hold-routing.sh (#11795)"
echo "================================"

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# --- Test 1: the lint flags a retired application (positive control)
echo ""
echo "Test 1: lint flags an application of a retired hold label"
printf 'gh issue edit <number> --add-label "%s"\n' "${OP}-only" >"$FIXTURE_DIR/bad1.md"
printf 'loom-daemon operator-decision apply 5 --input d.json --also-label %s\n' "${OP}-only" >"$FIXTURE_DIR/bad2.md"
printf "gh api repos/o/r/issues/5/labels -f 'labels[]=%s'\n" "loom:needs-capa""bility" >"$FIXTURE_DIR/bad3.md"
printf 'gh pr edit "$PR" --add-label "loom:blocked,%s"\n' "${OP}-mechanical" >"$FIXTURE_DIR/bad4.md"
for f in bad1 bad2 bad3 bad4; do
    if [[ -n "$(scan_file "$FIXTURE_DIR/$f.md")" ]]; then
        pass "$f.md: retired application reported"
    else
        fail "$f.md: retired application was NOT reported (lint is vacuous)"
    fi
done

# --- Test 2: the lint accepts the new forms and prose (negative control)
echo ""
echo "Test 2: lint accepts the three holds and prose mentions"
cat >"$FIXTURE_DIR/good.md" <<'EOF'
gh issue edit <number> --add-label "loom:operator"
gh pr edit "$PR" --add-label "loom:blocked"
loom-daemon operator-decision apply 5 --input d.json --remove-label loom:building
gh api repos/o/r/issues/5/labels -f 'labels[]=loom:operator'
EOF
printf '| `%s` | `loom:operator` |\n' "${OP}-only" >>"$FIXTURE_DIR/good.md"
GOOD_HITS="$(scan_file "$FIXTURE_DIR/good.md")"
if [[ -z "$GOOD_HITS" ]]; then
    pass "new-label applications and a prose mapping row are accepted"
else
    fail "false positive on a compliant form:"$'\n'"$GOOD_HITS"
fi

# --- Test 3: nothing shipped applies a retired hold label
echo ""
echo "Test 3: no role prompt, doc or script applies a retired hold label"
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
    pass "no shipped surface applies a retired hold label"
else
    fail "retired hold label application(s) found:"$'\n'"$VIOLATIONS"
fi

# --- Test 4: the shared rule exists once, in label-state-machine.md
echo ""
echo "Test 4: label-state-machine.md carries the shared \"Routing to a human\" rule"
if [[ ! -f "$STATE_MACHINE" ]]; then
    fail "label-state-machine.md not found at $STATE_MACHINE"
else
    RULE="$(awk '/^## Routing to a human/ { on = 1; print; next } on && /^## / { exit } on { print }' "$STATE_MACHINE")"
    if [[ -n "$RULE" ]]; then
        pass "the \"Routing to a human\" section exists"
    else
        fail "the \"Routing to a human\" section is missing"
    fi
    for needle in 'loom-daemon operator-decision apply' 'loom:capability=' 'loom:blocked' \
        'park record' 'Blocked by #N' 'Depends on #N' 'candidate' 'objectives' 'falsifiability'; do
        if [[ "$RULE" == *"$needle"* ]]; then
            pass "the rule names \"$needle\""
        else
            fail "the rule does not name \"$needle\""
        fi
    done
    if grep -qiE 'name the axis|axis two well-informed' <<<"$RULE"; then
        pass "the rule states the axis-naming requirement"
    else
        fail "the rule is missing the axis-naming requirement"
    fi
    if grep -qiE 'A decision is never a safe default' <<<"$RULE"; then
        pass "the rule says a decision is never a safe default (#5826)"
    else
        fail "the rule does not reject a decision as the safe unsure-default (#5826)"
    fi
    if grep -qE '^## The three holds' "$STATE_MACHINE" && grep -qE '^### How the old labels map' "$STATE_MACHINE"; then
        pass "the doc has the three-holds table and the old-label mapping"
    else
        fail "the doc lacks \"The three holds\" or \"How the old labels map\""
    fi
fi

# --- Test 5: each routing role points at the shared rule, once, without a table
echo ""
echo "Test 5: role prompts point at the shared rule instead of restating it"
for role in curator builder doctor judge builder-complexity; do
    f="$ROLE_DIR/$role.md"
    if [[ ! -f "$f" ]]; then
        fail "$role.md not found at $f"
        continue
    fi
    # Join wrapped lines (and blockquote markers) so a pointer split across
    # lines still matches; no pipe into an early-exit consumer.
    joined="$(sed 's/^> *//' "$f" | tr '\n' ' ' | tr -s ' ')"
    if [[ "$joined" == *'label-state-machine.md` → "Routing to a human"'* ]]; then
        pass "$role.md points at label-state-machine.md → \"Routing to a human\""
    else
        fail "$role.md does not point at label-state-machine.md → \"Routing to a human\""
    fi
    if grep -qE '^\| *Sub-kind *\|' "$f"; then
        fail "$role.md still restates a sub-kind table"
    else
        pass "$role.md does not restate a sub-kind table"
    fi
    if grep -qE "$RETIRED_RE" "$f"; then
        fail "$role.md still names a retired hold label"
    else
        pass "$role.md names no retired hold label"
    fi
done

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
