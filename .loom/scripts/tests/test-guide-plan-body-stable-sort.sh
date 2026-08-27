#!/usr/bin/env bash
# test-guide-plan-body-stable-sort.sh - Regression test for issue #6993
#
# Guide's Document Maintenance phase renders the machine-generated region of
# WORK_PLAN.md via `render_plan_body()`, and `update_work_plan()` decides
# whether to write by comparing that render byte-for-byte against the
# committed region (correct, and deliberately so — see the #5413 note in
# guide.md).
#
# THE BUG: `render_plan_body()`'s section queries were plain
# `gh issue list` / `gh pr list --json number,title --jq '.[] | ...'` calls
# with no explicit sort. GitHub's search-backed listings are NOT stably
# ordered: two items that tie on whatever the default ordering key is
# (labeled within the same second, or split across a pagination boundary) can
# swap positions between two otherwise-identical queries with ZERO underlying
# state change. A pure-reorder render is then byte-different from the
# committed region, so `update_work_plan()` cannot tell it apart from a
# genuine label-state change — and once the debounce window elapses it
# produces a real docs-maintenance PR whose entire content is a cosmetic
# swap. PR #6988 merged exactly such a 2-line diff (one issue, #6613, moving
# by one position in both the Ready and Proposed sections and nothing else),
# and the same tie flapped again on the next tick.
#
# THE FIX: every one of `render_plan_body()`'s 9 forge listings pipes through
# `sort_by(.number)` — a stable, content-derived key — before formatting
# bullets. `approved_json` is sorted once at the source so both the
# `approved` and `held` derivations inherit it (without reintroducing the
# second query #6457 removed).
#
# Verifies that:
#   1. STRUCTURE: every `--jq` pipeline inside `render_plan_body()` carries
#      `sort_by(.number)` (all 9 of them), and `approved_json` is sorted at
#      the source so the `approved`/`held` split stays consistent.
#   2. THE REGRESSION, executed rather than grepped: the REAL
#      `render_plan_body()` (extracted from guide.md and eval'd against a
#      stubbed `$GH_READ`) fed the SAME issue/PR set in two DIFFERENT
#      orders produces byte-identical output.
#   3. NEGATIVE CONTROL: the pre-fix pipelines (same fixtures, `sort_by`
#      removed) produce DIFFERENT output for the two orderings — i.e. this
#      test would actually have failed before the fix.
#   4. MEMBERSHIP UNCHANGED: the sorted render lists exactly the same
#      issues/PRs in each section as the unsorted one — only the ORDER
#      differs (the fix must not filter anything out).
#   5. EDGE CASES: an empty section still renders `_None._`; a genuine
#      membership change still changes the output (the render must not
#      become inert).
#
# Hermetic: a `gh` stub serving JSON fixtures from a temp dir. No forge or
# network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve the installed path first (consumer repos, and Loom's own dogfooded
# checkout), falling back to the defaults/ source-tree path (a bare source
# checkout with no .claude/commands/loom/ copy yet). See issue #6194 / #6241.
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

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# Same extraction the #5930/#6457 suite uses: the function's closing `}` is
# the first line starting at column 0 after the opening line (its nested
# `section()`/`count()` helpers are indented).
RPB_BODY="$(awk '/^render_plan_body\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"

# ---------------------------------------------------------------------------
# Test 1: STRUCTURE — every forge listing in render_plan_body() sorts
# ---------------------------------------------------------------------------
echo "Test 1: every --jq pipeline in render_plan_body() sorts by issue/PR number"

if [[ -z "$RPB_BODY" ]]; then
    fail "could not extract render_plan_body() body from guide.md"
    echo -e "${RED}FATAL${NC}: cannot continue without the function body"
    exit 1
fi
pass "render_plan_body() body extracted from $GUIDE_MD"

JQ_LINES="$(grep -c -- '--jq' <<<"$RPB_BODY")"
assert_eq "$JQ_LINES" "9" "render_plan_body() has the expected 9 --jq forge pipelines"

UNSORTED="$(grep -n -- '--jq' <<<"$RPB_BODY" | grep -v 'sort_by(\.number)' || true)"
if [[ -z "$UNSORTED" ]]; then
    pass "all --jq pipelines carry sort_by(.number)"
else
    fail "these --jq pipelines are missing sort_by(.number): $UNSORTED"
fi

# `approved_json` must be sorted AT THE SOURCE, so the `approved` and `held`
# derivations (which share it, per #6457) can never disagree on order.
if grep -A1 'approved_json=\$(' <<<"$RPB_BODY" | grep -q "sort_by(\.number)"; then
    pass "approved_json is sorted at the source (covers both \$approved and \$held, #6457)"
else
    fail "expected approved_json's query to pipe through sort_by(.number)"
fi

if grep -q '#6993' <<<"$RPB_BODY"; then
    pass "render_plan_body() documents the #6993 stable-sort requirement inline"
else
    fail "expected an inline #6993 note explaining why the sort must not be dropped"
fi

if ! command -v jq >/dev/null 2>&1; then
    echo ""
    echo "  SKIP: jq not available — skipping the executable determinism tests"
    echo ""
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
    [[ $TESTS_FAILED -gt 0 ]] && { echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"; exit 1; }
    echo "All tests passed"
    exit 0
fi

# ---------------------------------------------------------------------------
# Harness: a `gh` stub that serves per-label JSON fixtures from a directory,
# honouring `--jq` exactly the way `gh ... --json ... --jq EXPR` does.
# ---------------------------------------------------------------------------
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

GH_STUB="$TMPROOT/gh-stub"
cat > "$GH_STUB" <<'STUB'
#!/usr/bin/env bash
# Minimal `gh {issue|pr} list --label L [--json ...] [--jq EXPR]` stub.
# Serves $LOOM_FIXTURE_DIR/<kind>-<label with ':' -> '_'>.json, or [] if the
# fixture is absent (an empty section).
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
# A missing fixture means "no item carries that label" — an EMPTY RESULT SET,
# which real `gh` still runs --jq over (so `sort_by(.number)` must tolerate
# an empty array). Do not short-circuit past the --jq here.
if [[ -f "$fixture" ]]; then json="$(cat "$fixture")"; else json='[]'; fi
if [[ -n "$jqexpr" ]]; then printf '%s' "$json" | jq -r "$jqexpr"; else printf '%s' "$json"; fi
STUB
chmod +x "$GH_STUB"

# The same logical issue/PR set, written in two different orders. These are
# NOT different facts — they are the identical membership as GitHub's
# search-backed listing might hand it back on two different ticks when the
# ordering key ties (the #6993 flap, reproduced).
mk_fixtures() {
    local dir="$1" order="$2"   # order: "a" | "b" (b = reversed)
    mkdir -p "$dir"
    _write() {
        local file="$1"; shift
        local json="[$(IFS=,; echo "$*")]"
        if [[ "$order" == "b" ]]; then
            printf '%s' "$json" | jq 'reverse' > "$dir/$file"
        else
            printf '%s' "$json" | jq '.' > "$dir/$file"
        fi
    }
    _write issue-loom_urgent.json \
        '{"number":6993,"title":"Guide render_plan_body is not stably sorted"}'
    # The exact #6613 tie from PR #6988, plus neighbours.
    _write issue-loom_issue.json \
        '{"number":6612,"title":"Issue six-six-one-two"}' \
        '{"number":6613,"title":"resync orphan warning: distinguish formerly-shipped files"}' \
        '{"number":6614,"title":"Issue six-six-one-four"}'
    _write issue-loom_building.json \
        '{"number":6500,"title":"Building A"}' \
        '{"number":6501,"title":"Building B"}'
    _write pr-loom_review-requested.json \
        '{"number":6980,"title":"PR awaiting review A"}' \
        '{"number":6981,"title":"PR awaiting review B"}'
    _write pr-loom_pr.json \
        '{"number":6732,"title":"Approved PR held","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]}' \
        '{"number":6742,"title":"Approved PR free","labels":[{"name":"loom:pr"}]}' \
        '{"number":6817,"title":"Approved PR held two","labels":[{"name":"loom:pr"},{"name":"loom:operator"}]}'
    _write issue-loom_curated.json \
        '{"number":6612,"title":"Issue six-six-one-two"}' \
        '{"number":6613,"title":"resync orphan warning: distinguish formerly-shipped files"}'
    _write issue-loom_architect.json \
        '{"number":6400,"title":"Architect proposal A"}' \
        '{"number":6401,"title":"Architect proposal B"}'
    # loom:hermit is deliberately absent -> exercises the empty-section path.
    _write issue-loom_epic.json \
        '{"number":6165,"title":"Epic: lease records"}'
}

mk_fixtures "$TMPROOT/order-a" a
mk_fixtures "$TMPROOT/order-b" b

# ---------------------------------------------------------------------------
# Test 2: THE REGRESSION — reordered input, byte-identical output
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: the real render_plan_body() is order-insensitive (#6993)"

RENDER_A="$(LOOM_FIXTURE_DIR="$TMPROOT/order-a" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"
RENDER_A2="$(LOOM_FIXTURE_DIR="$TMPROOT/order-a" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"
RENDER_B="$(LOOM_FIXTURE_DIR="$TMPROOT/order-b" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"

if [[ -z "$RENDER_A" ]]; then
    fail "render_plan_body() produced no output against the fixtures (harness broken)"
else
    pass "render_plan_body() executed against the stubbed \$GH_READ"
fi

assert_eq "$RENDER_A" "$RENDER_A2" \
    "two consecutive renders against unchanged label state are byte-identical"
assert_eq "$RENDER_A" "$RENDER_B" \
    "a render of the SAME issue/PR set in a DIFFERENT arrival order is byte-identical (the #6988 flap)"

# The #6613 tie specifically: it must land in the same place both times.
POS_A="$(grep -n '#6613' <<<"$RENDER_A" | cut -d: -f1 | tr '\n' ' ')"
POS_B="$(grep -n '#6613' <<<"$RENDER_B" | cut -d: -f1 | tr '\n' ' ')"
assert_eq "$POS_A" "$POS_B" "#6613 occupies the same line positions under both arrival orders"

# ---------------------------------------------------------------------------
# Test 3: NEGATIVE CONTROL — the pre-fix pipeline WOULD have differed
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: negative control — the unsorted (pre-fix) pipeline is order-sensitive"

unsorted_ready() {   # $1 = fixture dir — the pre-#6993 pipeline, verbatim
    LOOM_FIXTURE_DIR="$1" "$GH_STUB" issue list --label "loom:issue" --state open --limit 200 \
        --json number,title --jq '.[] | "- **#\(.number)**: \(.title)"'
}
PRE_A="$(unsorted_ready "$TMPROOT/order-a")"
PRE_B="$(unsorted_ready "$TMPROOT/order-b")"
if [[ "$PRE_A" != "$PRE_B" ]]; then
    pass "without sort_by(.number) the two arrival orders DO render differently (bug reproduced)"
else
    fail "expected the unsorted pipeline to be order-sensitive — the fixtures do not exercise the bug"
fi

# ---------------------------------------------------------------------------
# Test 4: MEMBERSHIP UNCHANGED — sorting reorders, it never filters
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: section membership is unchanged by the sort"

# render_plan_body emits `held` (a subset of `approved`) as an extra section,
# so compare the SET of numbers rather than raw counts.
SET_SORTED="$(grep -o '#[0-9]\+' <<<"$RENDER_A" | sort -u | tr '\n' ' ')"
SET_PRE="$(for f in "$TMPROOT"/order-a/*.json; do jq -r '.[] | "#\(.number)"' "$f"; done | sort -u | tr '\n' ' ')"
assert_eq "$SET_SORTED" "$SET_PRE" \
    "every fixture issue/PR still appears in the render (sorting filters nothing)"

# Each section's bullets are in ascending numeric order.
BAD_SECTION=""
while IFS= read -r heading; do
    body="$(awk -v h="$heading" '$0 == h {flag=1; next} /^## /{flag=0} flag' <<<"$RENDER_A" | grep -o '^- \*\*#[0-9]\+' | grep -o '[0-9]\+')"
    [[ -z "$body" ]] && continue
    if [[ "$body" != "$(sort -n <<<"$body")" ]]; then
        BAD_SECTION="$BAD_SECTION $heading"
    fi
done < <(grep '^## ' <<<"$RENDER_A")
if [[ -z "$BAD_SECTION" ]]; then
    pass "every section's bullets are in ascending issue/PR-number order"
else
    fail "these sections are not ascending-sorted:$BAD_SECTION"
fi

# ---------------------------------------------------------------------------
# Test 5: EDGE CASES — empty section, approved/held consistency, live changes
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: edge cases (empty section, approved/held split, genuine changes)"

# A label nobody carries must still render the section's `_None._` fallback
# after sorting (`sort_by` on an empty array is an empty array, not an error).
mkdir -p "$TMPROOT/order-empty"
RENDER_EMPTY="$(LOOM_FIXTURE_DIR="$TMPROOT/order-empty" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"
EMPTY_SECTIONS="$(grep -c '^## ' <<<"$RENDER_EMPTY")"
NONE_COUNT="$(grep -c '^_None\._$' <<<"$RENDER_EMPTY")"
# Every section but "Backlog Balance" (which always renders its table) falls
# back to `_None._` when its label set is empty.
assert_eq "$NONE_COUNT" "$((EMPTY_SECTIONS - 1))" \
    "every empty section still renders the _None._ fallback after sorting"

# `held` is a filtered view of the same sorted `approved_json` — both must be
# ascending and `held` must be a subset of `approved`.
HELD_BODY="$(awk '/^## Operator Attention/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER_A" | grep -o '#[0-9]\+' | tr '\n' ' ')"
APPROVED_BODY="$(awk '/^## Approved \(Awaiting Merge\)/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER_A" | grep -o '#[0-9]\+' | tr '\n' ' ')"
assert_eq "$HELD_BODY" "#6732 #6817 " "held PRs render in ascending order from the sorted approved_json"
assert_eq "$APPROVED_BODY" "#6732 #6742 #6817 " "approved PRs render in ascending order from the same sorted fetch"

# A genuine membership change must STILL change the render — the sort must
# not make the region inert (the failure mode a naive "always skip" fix has).
cp -r "$TMPROOT/order-a" "$TMPROOT/order-c"
jq '. + [{"number":6615,"title":"A genuinely new ready issue"}]' \
    "$TMPROOT/order-a/issue-loom_issue.json" > "$TMPROOT/order-c/issue-loom_issue.json"
RENDER_C="$(LOOM_FIXTURE_DIR="$TMPROOT/order-c" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"
if [[ "$RENDER_A" != "$RENDER_C" ]]; then
    pass "a genuine label-state change (a new loom:issue) still changes the render"
else
    fail "expected a genuine membership change to change the rendered output"
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
