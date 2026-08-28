#!/usr/bin/env bash
# test-guide-work-plan-operator-only-exclude.sh - Regression test for issue #7008
#
# guide.md's `render_plan_body()` (WORK_PLAN.md's machine-generated region)
# has SIX forge queries: `urgent`/`ready`/`building`/`review`/`approved`/
# `curated`. The `ready` query (feeding the "Ready" section, documented as
# "Human-approved issues ready for implementation") filtered only on
# `--label "loom:issue"` — unlike the "Finding Work" search earlier in the
# same file, which already excludes `loom:building` and `loom:operator-only`
# via `--search "-label:loom:building -label:loom:operator-only"` (#6941).
#
# `loom:operator-only` (per `.github/labels.yml`: "sweep skips") means a
# Builder can never act on the issue — exactly like `loom:blocked` — so it
# must never render as "ready for implementation". Issue #6245
# (`loom:issue,loom:operator-only,loom:operator-blocked`, stable since
# 2026-08-25) rendered into the Ready section across three consecutive Guide
# document-maintenance ticks (PRs #7005/#7006/#7007), producing a trickle of
# near-content-free "docs: Guide document maintenance update" PRs.
#
# The `building` query had the same structural gap: `loom:operator-only` is
# not documented as mutually exclusive with `loom:building` in
# `.github/labels.yml`, so an issue could render into "In Progress" while
# actually stuck on a human-only decision.
#
# THE FIX: both queries gain the same `--search` exclusion clause the
# "Finding Work" query already uses:
#   ready:    --search "-label:loom:building -label:loom:operator-only"
#   building: --search "-label:loom:operator-only"
#
# Verifies that:
#   1. STRUCTURE: guide.md's `ready=` and `building=` queries inside
#      `render_plan_body()` carry the expected `--search` exclusion clauses.
#   2. THE REGRESSION, executed rather than grepped: the REAL
#      `render_plan_body()` (extracted from guide.md and eval'd against a
#      stubbed `$GH_READ` that honours `--search "-label:X"` the way real
#      `gh` combines `--label` (AND) with `--search` negation) excludes an
#      issue carrying `loom:issue` + `loom:operator-only` from the Ready
#      section, and an issue carrying `loom:building` + `loom:operator-only`
#      from the In Progress section.
#   3. NO OVER-EXCLUSION: a plain `loom:issue` issue (no operator-only/
#      building) still appears in Ready; a plain `loom:building` issue still
#      appears in In Progress.
#   4. NEGATIVE CONTROL: the pre-fix (unfiltered) `ready`/`building`
#      pipelines DO include the operator-only issues — i.e. this test would
#      have failed before the fix (reproduces #6245's flap).
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

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then pass "$msg"; else fail "$msg (got '$actual', expected '$expected')"; fi
}

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# Same extraction the #6993/#5930/#6457 suites use: the function's closing
# `}` is the first line starting at column 0 after the opening line (its
# nested `section()`/`count()` helpers are indented).
RPB_BODY="$(awk '/^render_plan_body\(\) \{/{flag=1} flag{print} /^\}/{if(flag){exit}}' "$GUIDE_MD")"

if [[ -z "$RPB_BODY" ]]; then
    fail "could not extract render_plan_body() body from guide.md"
    echo -e "${RED}FATAL${NC}: cannot continue without the function body"
    exit 1
fi
pass "render_plan_body() body extracted from $GUIDE_MD"

# ---------------------------------------------------------------------------
# Test 1: STRUCTURE — ready/building queries carry the exclusion clauses
# ---------------------------------------------------------------------------
echo ""
echo "Test 1: the ready/building queries exclude loom:operator-only"

READY_LINE="$(grep -n '^  ready=' <<<"$RPB_BODY" || true)"
BUILDING_LINE="$(grep -n '^  building=' <<<"$RPB_BODY" || true)"

if grep -q '^  ready=\$("\$GH_READ" issue list --label "loom:issue" --search "-label:loom:building -label:loom:operator-only"' <<<"$RPB_BODY"; then
    pass "the ready= query excludes loom:building and loom:operator-only via --search"
else
    fail "expected ready= to carry --search \"-label:loom:building -label:loom:operator-only\" (got: $READY_LINE)"
fi

if grep -q '^  building=\$("\$GH_READ" issue list --label "loom:building" --search "-label:loom:operator-only"' <<<"$RPB_BODY"; then
    pass "the building= query excludes loom:operator-only via --search"
else
    fail "expected building= to carry --search \"-label:loom:operator-only\" (got: $BUILDING_LINE)"
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
# `labels` array on each item) from a directory, then applies `--search
# "-label:X -label:Y"` negation the way real `gh` combines it (ANDed) with
# `--label`, before finally honouring `--jq` exactly like real `gh ... --json
# ... --jq EXPR` does.
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

# `ready` fixture (loom:issue query): #6068 plain-eligible, #6245 mirrors the
# real-world repro (loom:issue + loom:operator-only + loom:operator-blocked),
# #6100 a loom:issue + loom:building issue (also excluded by `ready`'s own
# --search clause, independent of operator-only).
cat > "$FIX/issue-loom_issue.json" <<'JSON'
[
  {"number":6068,"title":"Plain ready issue","labels":[{"name":"loom:issue"}]},
  {"number":6245,"title":"Guard ask-pattern false positive","labels":[{"name":"loom:issue"},{"name":"loom:operator-only"},{"name":"loom:operator-blocked"}]},
  {"number":6100,"title":"Already-building issue","labels":[{"name":"loom:issue"},{"name":"loom:building"}]}
]
JSON

# `building` fixture: #6500 plain-eligible, #6501 gained loom:operator-only.
cat > "$FIX/issue-loom_building.json" <<'JSON'
[
  {"number":6500,"title":"Plain building issue","labels":[{"name":"loom:building"}]},
  {"number":6501,"title":"Building issue that gained operator-only","labels":[{"name":"loom:building"},{"name":"loom:operator-only"}]}
]
JSON

RENDER="$(LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"

if [[ -z "$RENDER" ]]; then
    fail "render_plan_body() produced no output against the fixtures (harness broken)"
else
    pass "render_plan_body() executed against the stubbed \$GH_READ"
fi

READY_BODY="$(awk '/^## Ready/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"
BUILDING_BODY="$(awk '/^## In Progress/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"

# ---------------------------------------------------------------------------
# Test 2 (AC): the real render_plan_body() excludes operator-only issues
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: render_plan_body() excludes loom:operator-only from Ready/In Progress"

if grep -q '#6245' <<<"$READY_BODY"; then
    fail "#6245 (loom:issue + loom:operator-only) must NOT appear in the Ready section"
else
    pass "#6245 (loom:issue + loom:operator-only) is excluded from the Ready section"
fi

if grep -q '#6100' <<<"$READY_BODY"; then
    fail "#6100 (loom:issue + loom:building) must NOT appear in the Ready section"
else
    pass "#6100 (loom:issue + loom:building) is excluded from the Ready section"
fi

if grep -q '#6501' <<<"$BUILDING_BODY"; then
    fail "#6501 (loom:building + loom:operator-only) must NOT appear in the In Progress section"
else
    pass "#6501 (loom:building + loom:operator-only) is excluded from the In Progress section"
fi

# ---------------------------------------------------------------------------
# Test 3: NO OVER-EXCLUSION — plain issues still render
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: plain (non-operator-only) issues still render"

if grep -q '#6068' <<<"$READY_BODY"; then
    pass "#6068 (plain loom:issue) still appears in the Ready section"
else
    fail "#6068 (plain loom:issue) should still appear in the Ready section"
fi

if grep -q '#6500' <<<"$BUILDING_BODY"; then
    pass "#6500 (plain loom:building) still appears in the In Progress section"
else
    fail "#6500 (plain loom:building) should still appear in the In Progress section"
fi

# ---------------------------------------------------------------------------
# Test 4: NEGATIVE CONTROL — the pre-fix (unfiltered) queries DO include them
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: negative control — the pre-fix queries would have included them (bug reproduced)"

PRE_READY="$(LOOM_FIXTURE_DIR="$FIX" "$GH_STUB" issue list --label "loom:issue" --state open --limit 200 \
    --json number,title --jq '.[] | "- **#\(.number)**: \(.title)"')"
PRE_BUILDING="$(LOOM_FIXTURE_DIR="$FIX" "$GH_STUB" issue list --label "loom:building" --state open --limit 200 \
    --json number,title --jq '.[] | "- **#\(.number)**: \(.title)"')"

if grep -q '#6245' <<<"$PRE_READY"; then
    pass "without the --search clause, #6245 WOULD have rendered into Ready (bug reproduced)"
else
    fail "expected the negative-control (unfiltered) query to include #6245"
fi

if grep -q '#6501' <<<"$PRE_BUILDING"; then
    pass "without the --search clause, #6501 WOULD have rendered into In Progress (bug reproduced)"
else
    fail "expected the negative-control (unfiltered) query to include #6501"
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
