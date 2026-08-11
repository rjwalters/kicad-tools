#!/usr/bin/env bash
# test-dependency-parse.sh - Fixture tests for the dependency-phrase parser
# vocabulary widened in #4508.
#
# Before #4508, the documented dependency-phrase matcher
# `(Blocked by|Depends on|Requires) #[0-9]+` (established in guide.md's
# `parse_dependencies` and reused by sweep.md's --auto-stack detection,
# warn-out-of-set-deps.sh, and champion-pr-merge.md's unblock loop) failed to
# match the common markdown-bold/colon form `**Blocked by:** #1 (x), #3 (y)`
# — an empty parse that, under the aggressive `/loom:sweep all` taxonomy,
# would silently strip `loom:blocked` and build out of dependency order (see
# #4508 for the incident). This file asserts the fix's two properties:
#
#   1. The widened separator `[*_:[:space:]]*` between the phrase and the
#      first `#N` tolerates markdown emphasis (`*`/`_`) and an optional
#      colon, on every reuser site.
#   2. The two-stage parse (select matching lines, then extract every `#N`
#      on those lines) captures ALL refs in a comma-separated list, not just
#      the first.
#
# It also guards the load-bearing phrase-set split (#4508's curator
# enhancement): guide.md keeps all four alternatives (incl. the `- [ ]`
# checkbox form); the --auto-stack / warn-out-of-set-deps sites stay
# restricted to `Depends on`/`Requires`(/`Part of`) and must NEVER match
# `Blocked by` — that phrase drives the distinct loom:blocked machinery.
#
# Strategy: warn-out-of-set-deps.sh's `parse_out_of_set_deps` is REAL,
# sourceable code (guarded on `BASH_SOURCE == $0`), so it is tested directly
# — no duplication. guide.md's `parse_dependencies` and the markdown-embedded
# snippets in sweep.md / champion-pr-merge.md are prose (Claude reads and
# executes them, they are not standalone scripts), so this file mirrors each
# one verbatim in a local function and pins the shipped doc text with a
# literal `assert_doc_contains` check — if a future edit changes the doc
# without updating this file (or vice versa), the corresponding check fails.
#
# Usage:
#   ./.loom/scripts/tests/test-dependency-parse.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
GUIDE_MD="$DEFAULTS_DIR/.claude/commands/loom/guide.md"
SWEEP_MD="$DEFAULTS_DIR/.claude/commands/loom/sweep.md"
CHAMPION_MD="$DEFAULTS_DIR/.claude/commands/loom/champion-pr-merge.md"

# Source warn-out-of-set-deps.sh's real parse_out_of_set_deps BEFORE defining
# our own RED/GREEN/NC below — the script's own `[[ -t 2 ]]` color block
# redefines RED/NC (not GREEN), so sourcing first (then setting ours last)
# keeps this file's output colors consistent regardless of source order.
# shellcheck source=/dev/null
source "$SCRIPTS_DIR/warn-out-of-set-deps.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

# Pin a literal snippet as present verbatim in a doc file — catches drift
# between this test's mirrored functions and the shipped markdown.
assert_doc_contains() {
    local file="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" "$file"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (missing literal in $file: $needle)"
    fi
}

# =====================================================================
# guide.md's parse_dependencies — mirrors defaults/.claude/commands/loom/guide.md
# verbatim (four alternatives incl. checkbox form; widened separator).
# =====================================================================
guide_parse_dependencies() {
    local body="$1"
    echo "$body" \
        | grep -E '(Blocked by|Depends on|Requires|\- \[.\])[*_:[:space:]]*#[0-9]+' \
        | grep -oE '#[0-9]+' | tr -d '#' | sort -u
}

# =====================================================================
# sweep.md's --auto-stack detection snippet — mirrors
# defaults/.claude/commands/loom/sweep.md verbatim (restricted to
# Depends on / Requires; deliberately EXCLUDES Blocked by).
# =====================================================================
sweep_auto_stack_detect() {
    local body="$1"
    echo "$body" | grep -E '(Depends on|Requires)[*_:[:space:]]*#[0-9]+' | grep -oE '#[0-9]+' | tr -d '#' | sort -u
}

# =====================================================================
# champion-pr-merge.md's ALL_DEPS extraction — mirrors
# defaults/.claude/commands/loom/champion-pr-merge.md verbatim.
# =====================================================================
champion_all_deps() {
    local blocked_body="$1"
    echo "$blocked_body" | grep -E "(Blocked by|Depends on|Requires)[*_:[:space:]]*#[0-9]+" | grep -Eo "#[0-9]+" | grep -Eo "[0-9]+" | sort -u
}

echo "--- guide.md parse_dependencies: markdown-bold/colon forms ---"

out="$(guide_parse_dependencies '**Blocked by:** #1 (short reason), #3 (short reason)')"
assert_eq $'1\n3' "$out" "captures ALL refs from a bold+colon comma-list, not just #1"

out="$(guide_parse_dependencies 'Blocked by: #2')"
assert_eq "2" "$out" "plain phrase + colon (no bold)"

out="$(guide_parse_dependencies '_Depends on_ #5')"
assert_eq "5" "$out" "italic-wrapped phrase"

echo
echo "--- guide.md parse_dependencies: regression (plain unformatted forms) ---"

out="$(guide_parse_dependencies 'Blocked by #7')"
assert_eq "7" "$out" "plain Blocked by #N (no regression)"

out="$(guide_parse_dependencies 'Depends on #8')"
assert_eq "8" "$out" "plain Depends on #N (no regression)"

out="$(guide_parse_dependencies 'Requires #9')"
assert_eq "9" "$out" "plain Requires #N (no regression)"

out="$(guide_parse_dependencies '- [ ] #12: some description')"
assert_eq "12" "$out" "checkbox form still works (- [ ] #N)"

echo
echo "--- guide.md parse_dependencies: edge case (unrelated same-line ref) ---"

out="$(guide_parse_dependencies 'Depends on #5 (see #99)')"
assert_eq $'5\n99' "$out" "captures the unrelated same-line #99 too (documented conservative trade-off)"

echo
echo "--- sweep.md --auto-stack detection: widened separator, phrase-set unchanged ---"

out="$(sweep_auto_stack_detect '**Depends on:** #101 (x), #102 (y)')"
assert_eq $'101\n102' "$out" "captures all refs from a bold+colon comma-list"

out="$(sweep_auto_stack_detect 'Requires #9')"
assert_eq "9" "$out" "plain Requires #N (no regression)"

out="$(sweep_auto_stack_detect '**Blocked by:** #1')"
assert_eq "" "$out" "Blocked by NEVER matches --auto-stack detection (phrase-set split preserved)"

echo
echo "--- champion-pr-merge.md ALL_DEPS: previously-empty parse now resolves ---"

out="$(champion_all_deps '**Blocked by:** #1 (reason), #3 (reason)')"
assert_eq $'1\n3' "$out" "bold+colon comma-list yields 1 3 (previously empty -> silent unblock)"

out="$(champion_all_deps 'Blocked by #7
Depends on #8
Requires #9')"
assert_eq $'7\n8\n9' "$out" "plain unformatted forms (no regression)"

echo
echo "--- champion-pr-merge.md BLOCKED_ISSUES jq filter: widened separator ---"

if command -v jq >/dev/null 2>&1; then
    matched="$(echo '{"body":"**Blocked by:** #1 (reason), #3 (reason)"}' \
        | jq -r '.body | test("(Blocked by|Depends on|Requires)[*_:[:space:]]*#1"; "i")')"
    assert_eq "true" "$matched" "jq test() matches bold+colon form for the closed-issue number"

    matched="$(echo '{"body":"Depends on #5"}' \
        | jq -r '.body | test("(Blocked by|Depends on|Requires)[*_:[:space:]]*#7"; "i")')"
    assert_eq "false" "$matched" "jq test() does not match an unrelated issue number"
else
    echo "  SKIP: jq not available"
fi

echo
echo "--- warn-out-of-set-deps.sh parse_out_of_set_deps: real sourced code ---"

out="$(parse_out_of_set_deps '**Depends on:** #201 (x), #202 (y)')"
assert_eq $'201\n202' "$out" "bold+colon comma-list captures all refs"

out="$(parse_out_of_set_deps 'Part of: #303')"
assert_eq "303" "$out" "colon-only form (Part of)"

out="$(parse_out_of_set_deps '**Blocked by:** #400')"
assert_eq "" "$out" "Blocked by NEVER matches warn-out-of-set-deps (phrase-set split preserved)"

echo
echo "--- Doc pins: shipped markdown matches the mirrored functions above ---"

assert_doc_contains "$GUIDE_MD" \
    "grep -E '(Blocked by|Depends on|Requires|\- \[.\])[*_:[:space:]]*#[0-9]+'" \
    "guide.md parse_dependencies ships the widened four-alternative pattern"

assert_doc_contains "$SWEEP_MD" \
    "grep -E '(Depends on|Requires)[*_:[:space:]]*#[0-9]+'" \
    "sweep.md --auto-stack detection ships the widened two-phrase pattern"

assert_doc_contains "$CHAMPION_MD" \
    'grep -E "(Blocked by|Depends on|Requires)[*_:[:space:]]*#[0-9]+"' \
    "champion-pr-merge.md ALL_DEPS extraction ships the widened pattern"

assert_doc_contains "$CHAMPION_MD" \
    'test(\"(Blocked by|Depends on|Requires)[*_:[:space:]]*#$CLOSED_ISSUE\"; \"i\")' \
    "champion-pr-merge.md BLOCKED_ISSUES jq filter ships the widened pattern"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
