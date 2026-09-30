#!/usr/bin/env bash
# test-guide-operator-only-exclude.sh - Regression test for issues #6941,
# #7071 and #9244.
#
# #6941 / #7071: guide.md's "Finding Work" ready-queue search must exclude
# `loom:building`, `loom:operator-only` (labels.yml: "sweep skips") and
# `loom:blocked` — a Builder can never act on any of them, so none may ever
# surface as ready work. (Those issues were first found through Guide's old
# top-3 urgent-label duty, whose per-candidate `has_operator_only()` /
# `has_blocked()` / `has_open_pr_labeled_loom_pr()` helpers and flip guard
# existed only to feed that duty.)
#
# #9244 retired that duty with the urgent label itself. The one priority
# signal is now `loom:operator-priority`, the operator's human-only star.
# Guide READS it (WORK_PLAN's "Operator Priority" section) and never writes
# it.
#
# Verifies that:
#   1. The "Finding Work" ready-queue search still excludes loom:building,
#      loom:operator-only and loom:blocked.
#   2. The retired urgent-label machinery is gone from guide.md: no
#      urgency_rank(), no flip-guard call, no "Maximum Urgent" section, no
#      has_operator_only()/has_blocked() candidate helpers, and no mention
#      of the retired urgent label anywhere.
#   3. guide.md never adds or removes loom:operator-priority (no
#      `gh issue edit ... loom:operator-priority` write), and states so.
#   4. render_plan_body() reads loom:operator-priority into an
#      "Operator Priority" section, executed against a stubbed $GH_READ.
#
# Hermetic: `gh` is stubbed with fixture JSON; only the real `jq` binary is
# invoked — no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first, falling back to the defaults/ source-tree path (#6194 / #6241).
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then pass "$msg"; else fail "$msg (got '$actual', expected '$expected')"; fi
}

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (missing pattern: $pattern)"; fi
}

assert_no_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then fail "$msg (found pattern: $pattern)"; else pass "$msg"; fi
}

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# ---------------------------------------------------------------------------
echo "Test 1: the ready-queue search excludes building, operator-only and blocked"
assert_grep '\-label:loom:building \-label:loom:operator-only \-label:loom:blocked' "$GUIDE_MD" \
    "the ready-queue search term excludes loom:building, loom:operator-only, and loom:blocked"

# ---------------------------------------------------------------------------
echo ""
echo "Test 2: the retired urgent-label machinery is gone (#9244)"
# `urgen[t]` keeps this file itself out of the repo-wide retired-label grep.
assert_no_grep 'loom:urgen[t]' "$GUIDE_MD" "guide.md never names the retired urgent label"
assert_no_grep '^urgency_rank\(\) \{' "$GUIDE_MD" "urgency_rank() is removed"
assert_no_grep 'urgent-flip-guard' "$GUIDE_MD" "no urgent-flip-guard.sh call remains"
assert_no_grep '^## Maximum Urgent' "$GUIDE_MD" "the 'Maximum Urgent' section is removed"
assert_no_grep '^has_operator_only\(\) \{' "$GUIDE_MD" "the urgent-candidate has_operator_only() helper is removed"
assert_no_grep '^has_blocked\(\) \{' "$GUIDE_MD" "the urgent-candidate has_blocked() helper is removed"
if [[ -e "$SCRIPT_DIR/../urgent-flip-guard.sh" ]]; then
    fail "defaults/scripts/urgent-flip-guard.sh still exists"
else
    pass "defaults/scripts/urgent-flip-guard.sh is deleted"
fi

# ---------------------------------------------------------------------------
echo ""
echo "Test 3: Guide never writes loom:operator-priority"
assert_no_grep 'gh issue edit .*loom:operator-priority' "$GUIDE_MD" \
    "no 'gh issue edit ... loom:operator-priority' write in guide.md"
assert_grep 'NEVER add or remove `loom:operator-priority`' "$GUIDE_MD" \
    "guide.md states the star is never added or removed by Guide"

# ---------------------------------------------------------------------------
echo ""
echo "Test 4: render_plan_body() renders a read-only Operator Priority section"

RPB_BODY="$(awk '/^render_plan_body\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"
if [[ -z "$RPB_BODY" ]]; then
    fail "could not extract render_plan_body() from guide.md"
elif ! command -v jq >/dev/null 2>&1; then
    echo "  SKIP: jq not available"
else
    TMPROOT="$(mktemp -d)"
    trap 'rm -rf "$TMPROOT"' EXIT
    GH_STUB="$TMPROOT/gh"
    cat > "$GH_STUB" <<'STUB'
#!/usr/bin/env bash
# Serves $LOOM_FIXTURE_DIR/<kind>-<label with ':' -> '_'>.json, or [].
set -uo pipefail
kind="${1:-}"; shift || true
label=""; jqexpr=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) label="${2:-}"; shift 2 ;;
    --jq)    jqexpr="${2:-}"; shift 2 ;;
    *)       shift ;;
  esac
done
fixture="$LOOM_FIXTURE_DIR/${kind}-${label//:/_}.json"
if [[ -f "$fixture" ]]; then json="$(cat "$fixture")"; else json='[]'; fi
if [[ -n "$jqexpr" ]]; then printf '%s' "$json" | jq -r "$jqexpr"; else printf '%s' "$json"; fi
STUB
    chmod +x "$GH_STUB"
    FIX="$TMPROOT/fixtures"
    mkdir -p "$FIX"
    printf '%s' '[{"number":8191,"title":"Starred issue B"},{"number":4765,"title":"Starred issue A"}]' \
        > "$FIX/issue-loom_operator-priority.json"

    RENDER="$(LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"
    SECTION="$(awk '/^## Operator Priority$/{f=1;next} f && /^## /{exit} f' <<<"$RENDER")"
    if grep -q '^- \*\*#4765\*\*: Starred issue A$' <<<"$SECTION" && grep -q '^- \*\*#8191\*\*: Starred issue B$' <<<"$SECTION"; then
        pass "the Operator Priority section lists every starred issue"
    else
        fail "expected both starred issues in the Operator Priority section (got: $SECTION)"
    fi
    FIRST="$(grep -m1 '^- ' <<<"$SECTION")"
    assert_eq "$FIRST" "- **#4765**: Starred issue A" "starred issues render in stable number order"
    if grep -q '^| Operator priority | 2 |$' <<<"$RENDER"; then
        pass "the Backlog Balance table counts starred issues"
    else
        fail "expected '| Operator priority | 2 |' in the Backlog Balance table"
    fi
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
