#!/usr/bin/env bash
# test-guide-work-plan-ready-open-pr-exclude.sh - Regression test for issue #7083
#
# guide.md's `render_plan_body()` (WORK_PLAN.md's machine-generated region)
# excludes `loom:building`, `loom:operator-only`, and `loom:blocked` issues
# from the "Ready" section (#7008/#7071) — but never excluded an issue that
# already has an OPEN `loom:pr`-labeled PR linked to it via
# `Closes/Fixes/Resolves #N`. Such an issue is fully implemented and merely
# awaiting a Champion merge decision — it belongs in "Approved (Awaiting
# Merge)", not "Ready" (which is documented as "Human-approved issues ready
# for implementation"). A live audit at filing time found 13/13 issues then
# listed in "Ready" already had exactly this shape.
#
# THE FIX: `render_plan_body()` derives the exclusion set from data it
# already fetches for the "Approved"/"Operator Attention" sections —
# `approved_json` (the `gh pr list --label "loom:pr"` query, #6457) — by
# adding `closingIssuesReferences` to that query's `--json` fields and
# collecting every closed issue number across all open `loom:pr` PRs. The
# `ready` query itself is unchanged (still `--search
# "-label:loom:building -label:loom:operator-only -label:loom:blocked"`);
# only the final bullet-formatting step filters `ready_json` against the
# derived set. No additional forge call is added — same "reuse
# already-fetched data" discipline `held`'s derivation established for #6457.
#
# Verifies that:
#   1. STRUCTURE: `approved_json`'s query carries `closingIssuesReferences`
#      in its `--json` fields, and the function contains exactly ONE
#      `pr list --label "loom:pr"` call (i.e. no second query was added to
#      answer this check).
#   2. THE REGRESSION, executed rather than grepped: the REAL
#      `render_plan_body()` (extracted from guide.md and eval'd against a
#      stubbed `$GH_READ`) excludes an issue carrying `loom:issue` whose
#      closing PR is open and carries `loom:pr` from the "Ready" section.
#   3. NO OVER-EXCLUSION: a plain `loom:issue` issue with no linked PR at
#      all, and one whose linked PR is still mid-review (`loom:pr` not yet
#      applied), both still appear in Ready.
#   4. NEGATIVE CONTROL: the pre-fix (unfiltered) `ready` pipeline DOES
#      include the open-linked-PR issue — i.e. this test would have failed
#      before the fix.
#
# Hermetic: `gh` is stubbed with fixture JSON; only the real `jq` binary is
# invoked (skipped if unavailable) — no forge/network calls.

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

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# Same extraction the #6993/#5930/#6457/#7071 suites use: the function's
# closing `}` is the first line starting at column 0 after the opening line
# (its nested `section()`/`count()` helpers are indented).
RPB_BODY="$(awk '/^render_plan_body\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"

if [[ -z "$RPB_BODY" ]]; then
    fail "could not extract render_plan_body() body from guide.md"
    echo -e "${RED}FATAL${NC}: cannot continue without the function body"
    exit 1
fi
pass "render_plan_body() body extracted from $GUIDE_MD"

# ---------------------------------------------------------------------------
# Test 1: STRUCTURE — approved_json carries closingIssuesReferences, and no
# second `pr list --label "loom:pr"` query was added.
# ---------------------------------------------------------------------------
echo ""
echo "Test 1: approved_json fetches closingIssuesReferences; no extra forge call"

if grep -q 'approved_json=\$("\$GH_READ" pr list --label "loom:pr" --state open --limit 200 --json number,title,labels,closingIssuesReferences' <<<"$RPB_BODY"; then
    pass "approved_json= query carries closingIssuesReferences in --json fields"
else
    fail "expected approved_json= to fetch closingIssuesReferences alongside number,title,labels"
fi

PR_LOOM_PR_QUERY_COUNT="$(grep -c 'pr list --label "loom:pr"' <<<"$RPB_BODY" || true)"
if [[ "$PR_LOOM_PR_QUERY_COUNT" -eq 1 ]]; then
    pass "exactly one 'pr list --label \"loom:pr\"' query in render_plan_body() (no second query added)"
else
    fail "expected exactly one 'pr list --label \"loom:pr\"' query, found $PR_LOOM_PR_QUERY_COUNT"
fi

if grep -q '^  pr_linked_issues=\$(printf .%s. "\$approved_json"' <<<"$RPB_BODY"; then
    pass "pr_linked_issues is derived from \$approved_json (not a fresh query)"
else
    fail "expected pr_linked_issues= to be derived from \$approved_json via printf | jq"
fi

if ! command -v jq >/dev/null 2>&1; then
    echo ""
    echo "  SKIP: jq not available — skipping the executable exclusion tests"
    echo ""
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
    [[ $TESTS_FAILED -gt 0 ]] && { echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"; exit 1; }
    echo "All tests passed"
    exit 0
fi

# ---------------------------------------------------------------------------
# Harness: a `gh` stub that serves per-label JSON fixtures (including a
# `labels` array and a `closingIssuesReferences` array on each PR item) from
# a directory, then applies `--search "-label:X -label:Y"` negation the way
# real `gh` combines it (ANDed) with `--label`, before finally honouring
# `--jq` exactly like real `gh ... --json ... --jq EXPR` does.
# ---------------------------------------------------------------------------
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

GH_STUB="$TMPROOT/gh-stub"
cat > "$GH_STUB" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
kind="${1:-}"; shift || true
label=""; search=""; jqexpr=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)  label="${2:-}"; shift 2 ;;
    --search) search="${2:-}"; shift 2 ;;
    --jq)     jqexpr="${2:-}"; shift 2 ;;
    *)        shift ;;
  esac
done
fixture="$LOOM_FIXTURE_DIR/${kind}-${label//:/_}.json"
if [[ -f "$fixture" ]]; then json="$(cat "$fixture")"; else json='[]'; fi
# Apply every "-label:X" negation term in --search, ANDed with --label,
# exactly like real gh's combined --label + --search filtering.
for tok in $search; do
  case "$tok" in
    -label:*)
      excl="${tok#-label:}"
      json="$(printf '%s' "$json" | jq --arg l "$excl" '[.[] | select(((.labels // []) | map(.name) | index($l)) | not)]')"
      ;;
  esac
done
if [[ -n "$jqexpr" ]]; then printf '%s' "$json" | jq -r "$jqexpr"; else printf '%s' "$json"; fi
STUB
chmod +x "$GH_STUB"

mkdir -p "$TMPROOT/fixtures"
FIX="$TMPROOT/fixtures"

# `ready` fixture (loom:issue query, already past the loom:building/
# operator-only/blocked --search exclusion — this suite only exercises the
# NEW open-linked-PR exclusion):
#   #7100 plain ready issue, no linked PR at all.
#   #7101 has a linked PR (#8001) that is OPEN and carries loom:pr — must be
#         excluded from Ready by this fix.
#   #7102 has a linked PR (#8002) that is still mid-review (no loom:pr yet)
#         — must NOT be excluded; the work isn't Judge-approved yet.
cat > "$FIX/issue-loom_issue.json" <<'JSON'
[
  {"number":7100,"title":"Plain ready issue, no linked PR","labels":[{"name":"loom:issue"}]},
  {"number":7101,"title":"Issue with an open loom:pr-labeled linked PR","labels":[{"name":"loom:issue"}]},
  {"number":7102,"title":"Issue with a linked PR still under review","labels":[{"name":"loom:issue"}]}
]
JSON

# `approved` fixture (loom:pr query): PR #8001 closes #7101 and carries
# loom:pr (open, approved) — its presence here is what drives the exclusion.
# PR #8002 (closes #7102) deliberately does NOT appear in this fixture: it
# has not been labeled loom:pr yet (mid-review), so it is absent from
# approved_json exactly the way the real `gh pr list --label "loom:pr"`
# query would omit it.
cat > "$FIX/pr-loom_pr.json" <<'JSON'
[
  {"number":8001,"title":"Fix for #7101","labels":[{"name":"loom:pr"}],"closingIssuesReferences":[{"number":7101}]}
]
JSON

RENDER="$(LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"

if [[ -z "$RENDER" ]]; then
    fail "render_plan_body() produced no output against the fixtures (harness broken)"
else
    pass "render_plan_body() executed against the stubbed \$GH_READ"
fi

READY_BODY="$(awk '/^## Ready/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"
APPROVED_BODY="$(awk '/^## Approved/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"

# ---------------------------------------------------------------------------
# Test 2 (AC): the real render_plan_body() excludes the open-linked-PR issue
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: render_plan_body() excludes an issue with an open loom:pr-linked PR from Ready"

if grep -q '#7101' <<<"$READY_BODY"; then
    fail "#7101 (loom:issue + open loom:pr-linked PR #8001) must NOT appear in the Ready section"
else
    pass "#7101 (loom:issue + open loom:pr-linked PR #8001) is excluded from the Ready section"
fi

if grep -q '#8001' <<<"$APPROVED_BODY"; then
    pass "#8001 (the linking PR) still appears in Approved (Awaiting Merge)"
else
    fail "#8001 should still appear in Approved (Awaiting Merge)"
fi

# ---------------------------------------------------------------------------
# Test 3: NO OVER-EXCLUSION
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: issues without an open loom:pr-linked PR still render in Ready"

if grep -q '#7100' <<<"$READY_BODY"; then
    pass "#7100 (plain loom:issue, no linked PR) still appears in the Ready section"
else
    fail "#7100 (plain loom:issue, no linked PR) should still appear in the Ready section"
fi

if grep -q '#7102' <<<"$READY_BODY"; then
    pass "#7102 (linked PR still mid-review, not yet loom:pr) still appears in the Ready section"
else
    fail "#7102 (linked PR still mid-review) should still appear in the Ready section — not yet Judge-approved"
fi

# ---------------------------------------------------------------------------
# Test 4: NEGATIVE CONTROL — the pre-fix (unfiltered) ready query DOES
# include the open-linked-PR issue (bug reproduced).
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: negative control — the pre-fix query would have included #7101 (bug reproduced)"

PRE_READY="$(LOOM_FIXTURE_DIR="$FIX" "$GH_STUB" issue list --label "loom:issue" --state open --limit 200 \
    --json number,title --jq '.[] | "- **#\(.number)**: \(.title)"')"

if grep -q '#7101' <<<"$PRE_READY"; then
    pass "without the exclusion, #7101 WOULD have rendered into Ready (#7083 bug reproduced)"
else
    fail "expected the negative-control (unfiltered) query to include #7101"
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
