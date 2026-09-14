#!/usr/bin/env bash
# test-guide-work-plan-approved-review-label-fidelity.sh - Regression guard for
# issue #7270.
#
# BACKGROUND: #7270 reported that WORK_PLAN.md's "Operator Attention:
# Merge-Risk-Hold Pileup" / "Approved (Awaiting Merge)" sections listed PRs
# that were actually `loom:review-requested`, not `loom:pr`, while "PRs
# Awaiting Review" (which should list exactly those `loom:review-requested`
# PRs) rendered `_None._` across several consecutive docs-maintenance merges.
#
# ROOT-CAUSE FINDING (documented in full on the PR that added this test):
# `render_plan_body()`'s queries were NOT the cause — `approved_json` has
# fetched `pr list --label "loom:pr"` (never `loom:review-requested`) since
# commit 96f15f59d (2026-08-18), and a live audit of every PR named in the
# committed WORK_PLAN.md's "Approved"/"Operator Attention" sections (via
# `gh api repos/.../issues/<n>/events`) showed each one genuinely carried
# `loom:pr`+`loom:operator` continuously for days at the moment each render
# was generated. The apparent staleness was a real, ~4-minute mass
# re-review event (triggered by PR #7269 landing a fix to
# `has_superseding_block()`) that happened to fall in the gap between
# WORK_PLAN.md's last render and the issue being filed — a normal, expected
# artifact of the up-to-`LOOM_WORK_PLAN_DEBOUNCE_SECS` (1h) staleness window
# documented in guide.md's "WORK_PLAN debounce (#5890)" section, not a code
# defect. The very next Guide tick (PR #7273) already produced a fully
# corrected render, exactly as `update_work_plan()`'s debounce design intends.
#
# No code change was warranted for the reported symptom, but the issue's own
# hypothesis #1 — a future regression that DOES source `approved`/`held` from
# the wrong label — is worth permanently guarding against, mirroring the
# fixture-based approach `test-guide-work-plan-ready-open-pr-exclude.sh`
# already uses for the "Ready" section's open-linked-PR exclusion. This suite
# is that guard: it feeds `render_plan_body()` two DISJOINT, clearly-labeled
# `gh pr list` fixtures (one for `loom:pr`, one for `loom:review-requested`)
# and asserts the four sections built from them never cross-contaminate:
#
#   1. STRUCTURE: `review` and `approved_json` are two DISTINCT queries
#      (`--label "loom:review-requested"` vs `--label "loom:pr"`), and
#      `held` is derived from `approved_json` (not a third query) — so the
#      three can never silently start sharing one label filter.
#   2. THE REGRESSION, executed rather than grepped: PRs that only exist in
#      the `loom:pr` fixture appear in "Approved (Awaiting Merge)" (and, if
#      also `loom:operator`, in "Operator Attention") but NEVER in "PRs
#      Awaiting Review".
#   3. THE MIRROR CASE: PRs that only exist in the `loom:review-requested`
#      fixture appear in "PRs Awaiting Review" but NEVER in "Approved
#      (Awaiting Merge)" or "Operator Attention" — even though both fixtures
#      are `--state open` and could otherwise share a naive listing.
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

# Same extraction the #6993/#5930/#6457/#7071/#7083 suites use: the function's
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
# Test 1: STRUCTURE — review and approved_json are two distinct label
# queries; held is derived from approved_json, not a third query.
# ---------------------------------------------------------------------------
echo ""
echo "Test 1: 'review' and 'approved_json' use distinct, non-overlapping label filters"

if grep -q 'review=\$("\$GH_READ" pr list --label "loom:review-requested"' <<<"$RPB_BODY"; then
    pass "review= queries --label \"loom:review-requested\""
else
    fail "expected review= to query --label \"loom:review-requested\""
fi

if grep -q 'approved_json=\$("\$GH_READ" pr list --label "loom:pr"' <<<"$RPB_BODY"; then
    pass "approved_json= queries --label \"loom:pr\""
else
    fail "expected approved_json= to query --label \"loom:pr\""
fi

PR_REVIEW_QUERY_COUNT="$(grep -c 'pr list --label "loom:review-requested"' <<<"$RPB_BODY" || true)"
if [[ "$PR_REVIEW_QUERY_COUNT" -eq 1 ]]; then
    pass "exactly one 'pr list --label \"loom:review-requested\"' query in render_plan_body()"
else
    fail "expected exactly one 'pr list --label \"loom:review-requested\"' query, found $PR_REVIEW_QUERY_COUNT"
fi

PR_LOOM_PR_QUERY_COUNT="$(grep -c 'pr list --label "loom:pr"' <<<"$RPB_BODY" || true)"
if [[ "$PR_LOOM_PR_QUERY_COUNT" -eq 1 ]]; then
    pass "exactly one 'pr list --label \"loom:pr\"' query in render_plan_body() (no second query added)"
else
    fail "expected exactly one 'pr list --label \"loom:pr\"' query, found $PR_LOOM_PR_QUERY_COUNT"
fi

if grep -q '^  held=\$(printf .%s. "\$approved_json"' <<<"$RPB_BODY"; then
    pass "held is derived from \$approved_json (not a fresh query) — #6457 discipline"
else
    fail "expected held= to be derived from \$approved_json via printf | jq"
fi

if ! command -v jq >/dev/null 2>&1; then
    echo ""
    echo "  SKIP: jq not available — skipping the executable cross-contamination tests"
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
# then honours `--jq` exactly like real `gh ... --json ... --jq EXPR` does.
# Mirrors test-guide-work-plan-ready-open-pr-exclude.sh's stub.
# ---------------------------------------------------------------------------
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

GH_STUB="$TMPROOT/gh-stub"
cat > "$GH_STUB" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
kind="${1:-}"; shift || true
label=""; jqexpr=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)  label="${2:-}"; shift 2 ;;
    --jq)     jqexpr="${2:-}"; shift 2 ;;
    *)        shift ;;
  esac
done
fixture="$LOOM_FIXTURE_DIR/${kind}-${label//:/_}.json"
if [[ -f "$fixture" ]]; then json="$(cat "$fixture")"; else json='[]'; fi
if [[ -n "$jqexpr" ]]; then printf '%s' "$json" | jq -r "$jqexpr"; else printf '%s' "$json"; fi
STUB
chmod +x "$GH_STUB"

mkdir -p "$TMPROOT/fixtures"
FIX="$TMPROOT/fixtures"

# `loom:pr` fixture: #9001 (no operator hold) and #9002 (loom:operator hold).
# Neither number appears in the loom:review-requested fixture below — the
# two sets are deliberately disjoint so any cross-contamination is
# unambiguous.
cat > "$FIX/pr-loom_pr.json" <<'JSON'
[
  {"number":9001,"title":"Approved, no operator hold","labels":[{"name":"loom:pr"}],"closingIssuesReferences":[]},
  {"number":9002,"title":"Approved, under operator hold","labels":[{"name":"loom:pr"},{"name":"loom:operator"}],"closingIssuesReferences":[]}
]
JSON

# `loom:review-requested` fixture: #9003 and #9004 — genuinely mid-review,
# never approved. Same --state open scope as the fixture above; only the
# label differs.
cat > "$FIX/pr-loom_review-requested.json" <<'JSON'
[
  {"number":9003,"title":"Still awaiting Judge review (A)"},
  {"number":9004,"title":"Still awaiting Judge review (B)"}
]
JSON

RENDER="$(LOOM_FIXTURE_DIR="$FIX" GH_READ="$GH_STUB" RPB_SRC="$RPB_BODY" bash -c 'eval "$RPB_SRC"; render_plan_body')"

if [[ -z "$RENDER" ]]; then
    fail "render_plan_body() produced no output against the fixtures (harness broken)"
else
    pass "render_plan_body() executed against the stubbed \$GH_READ"
fi

HELD_BODY="$(awk '/^## Operator Attention/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"
REVIEW_BODY="$(awk '/^## PRs Awaiting Review/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"
APPROVED_BODY="$(awk '/^## Approved/{flag=1; next} /^## /{flag=0} flag' <<<"$RENDER")"

# ---------------------------------------------------------------------------
# Test 2 (AC): loom:pr-fixture PRs land in Approved (and, if loom:operator,
# in Operator Attention) but NEVER in PRs Awaiting Review.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: loom:pr PRs render into Approved/Operator-Attention, never into PRs Awaiting Review"

if grep -q '#9001' <<<"$APPROVED_BODY"; then
    pass "#9001 (loom:pr, no operator hold) appears in Approved (Awaiting Merge)"
else
    fail "#9001 (loom:pr) should appear in Approved (Awaiting Merge)"
fi

if grep -q '#9002' <<<"$APPROVED_BODY"; then
    pass "#9002 (loom:pr + loom:operator) appears in Approved (Awaiting Merge)"
else
    fail "#9002 (loom:pr + loom:operator) should appear in Approved (Awaiting Merge)"
fi

if grep -q '#9002' <<<"$HELD_BODY"; then
    pass "#9002 (loom:operator) appears in Operator Attention: Merge-Risk-Hold Pileup"
else
    fail "#9002 (loom:operator) should appear in Operator Attention: Merge-Risk-Hold Pileup"
fi

if grep -q '#9001' <<<"$HELD_BODY"; then
    fail "#9001 (no loom:operator) must NOT appear in Operator Attention: Merge-Risk-Hold Pileup"
else
    pass "#9001 (no loom:operator) is correctly absent from Operator Attention: Merge-Risk-Hold Pileup"
fi

if grep -qE '#900[12]' <<<"$REVIEW_BODY"; then
    fail "loom:pr PRs (#9001/#9002) must NEVER appear in PRs Awaiting Review — this is the #7270 regression shape"
else
    pass "loom:pr PRs (#9001/#9002) are correctly absent from PRs Awaiting Review"
fi

# ---------------------------------------------------------------------------
# Test 3 (mirror): loom:review-requested-fixture PRs land in PRs Awaiting
# Review but NEVER in Approved / Operator Attention.
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: loom:review-requested PRs render into PRs Awaiting Review, never into Approved/Operator-Attention"

if grep -q '#9003' <<<"$REVIEW_BODY" && grep -q '#9004' <<<"$REVIEW_BODY"; then
    pass "#9003 and #9004 (loom:review-requested) appear in PRs Awaiting Review"
else
    fail "#9003 and #9004 (loom:review-requested) should appear in PRs Awaiting Review"
fi

if grep -qE '#900[34]' <<<"$APPROVED_BODY"; then
    fail "loom:review-requested PRs (#9003/#9004) must NEVER appear in Approved (Awaiting Merge) — the #7270 regression shape"
else
    pass "loom:review-requested PRs (#9003/#9004) are correctly absent from Approved (Awaiting Merge)"
fi

if grep -qE '#900[34]' <<<"$HELD_BODY"; then
    fail "loom:review-requested PRs (#9003/#9004) must NEVER appear in Operator Attention: Merge-Risk-Hold Pileup"
else
    pass "loom:review-requested PRs (#9003/#9004) are correctly absent from Operator Attention: Merge-Risk-Hold Pileup"
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
