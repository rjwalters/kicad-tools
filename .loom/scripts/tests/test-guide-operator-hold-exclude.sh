#!/usr/bin/env bash
# test-guide-operator-hold-exclude.sh - Regression test for issues #6941,
# #7071, #9244 and #11103.
#
# #6941 / #7071: guide.md's "Finding Work" ready-queue search must exclude
# `loom:building`, `loom:operator-only` (labels.yml: "sweep skips") and
# `loom:blocked` — a Builder can never act on any of them, so none may ever
# surface as ready work. (Those issues were first found through Guide's old
# top-3 urgent-label duty, whose per-candidate `has_operator_only()` /
# `has_blocked()` / `has_open_pr_labeled_loom_pr()` helpers and flip guard
# existed only to feed that duty.)
#
# #9244 retired that duty with the urgent label itself. Since #11103 the
# priority signal is two labels, `loom:important` and `loom:very-important`,
# which Guide itself applies and removes (and renders in WORK_PLAN).
#
# Verifies that:
#   1. The "Finding Work" ready-queue search still excludes loom:building,
#      loom:operator-only and loom:blocked.
#   2. The retired urgent-label machinery is gone from guide.md: no
#      urgency_rank(), no flip-guard call, no "Maximum Urgent" section, no
#      has_operator_only()/has_blocked() candidate helpers, and no mention
#      of the retired urgent label anywhere.
#   3. guide.md names no retired priority/tier label, states that Guide
#      applies and removes both priority labels, and labels blockers itself.
#   4. render_plan_body() renders "Very Important" and "Important"
#      sections, executed against a stubbed $GH_READ.
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

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# ---------------------------------------------------------------------------
echo "Test 1: the ready-queue search excludes building, operator and blocked"
# Since #11795 the hold set is the registry's (retired names included until
# `labels migrate` has moved every item); the literal list is the fallback.
assert_grep "^HOLD_Q=\\\$\\(loom-daemon labels list --property hold --with-retired 2>/dev/null \\|\\| printf 'loom:blocked\\\\nloom:operator\\\\nloom:frozen'\\)" "$GUIDE_MD" \
    "the ready-queue hold set is the registry's (--with-retired), falling back to loom:blocked, loom:operator, loom:frozen"
assert_grep 'issue list --label "loom:issue" --search "-label:loom:building \$\(sed .s/\^/-label:/. <<<"\$HOLD_Q"' "$GUIDE_MD" \
    "the ready-queue search term excludes loom:building and every hold label"

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
echo "Test 3: Guide owns the two priority labels and never touches the retired star (#11103)"
assert_no_grep 'operator-priority|operator-high-priority|high-priority-inherited|tier:(goal|maint)' "$GUIDE_MD" \
    "guide.md names no retired priority or tier label"
assert_grep 'You add and remove `loom:important` / `loom:very-important`' "$GUIDE_MD" \
    "guide.md states Guide applies and removes both priority labels"
assert_grep 'label the blocker yourself' "$GUIDE_MD" \
    "guide.md labels a priority issue's blocker itself (no daemon inheritance)"

# ---------------------------------------------------------------------------
echo ""
echo "Test 4: render_plan_body() renders one section per priority level"

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
    printf '%s' '[{"number":8191,"title":"Important issue B"},{"number":4765,"title":"Important issue A"}]' \
        > "$FIX/issue-loom_important.json"
    printf '%s' '[{"number":9001,"title":"Very important issue"}]' \
        > "$FIX/issue-loom_very-important.json"

    RENDER="$(LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"
    SECTION="$(awk '/^## Important$/{f=1;next} f && /^## /{exit} f' <<<"$RENDER")"
    if grep -q '^- \*\*#4765\*\*: Important issue A$' <<<"$SECTION" && grep -q '^- \*\*#8191\*\*: Important issue B$' <<<"$SECTION"; then
        pass "the Important section lists every loom:important issue"
    else
        fail "expected both important issues in the Important section (got: $SECTION)"
    fi
    FIRST="$(grep -m1 '^- ' <<<"$SECTION")"
    assert_eq "- **#4765**: Important issue A" "$FIRST" "important issues render in stable number order"
    VSECTION="$(awk '/^## Very Important$/{f=1;next} f && /^## /{exit} f' <<<"$RENDER")"
    assert_eq "- **#9001**: Very important issue" "$(grep -m1 '^- ' <<<"$VSECTION")" \
        "the Very Important section lists the loom:very-important issue"
    if grep -q '^| Important | 2 |$' <<<"$RENDER" && grep -q '^| Very important | 1 |$' <<<"$RENDER"; then
        pass "the Backlog Balance table counts both levels"
    else
        fail "expected '| Very important | 1 |' and '| Important | 2 |' in the Backlog Balance table"
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
