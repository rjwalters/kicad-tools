#!/usr/bin/env bash
# test-guide-work-log-self-loop.sh - Regression test for issue #5454
#
# The Guide role's Document Maintenance phase self-perpetuated: `update_work_log()`
# built its "new merged PRs" set from `gh pr list --state merged` with NO exclusion
# for the phase's own `docs: Guide document maintenance update` PRs. Since every
# docs-maintenance PR is itself a merged PR, merging PR N always produced "new
# content above the watermark" for tick N+1 — the "nothing new, skip" branch could
# never be true two ticks in a row, and the phase emitted a fresh PR forever
# (observed: 23 self-referential PRs, one every ~15-30 min).
#
# The fix is a jq exclusion applied BEFORE `new_prs` is computed, matching either
# the `docs/guide-update` head-branch prefix (the branch docs-worktree.sh creates,
# and the same prefix Step 1's open-docs-PR guard searches on) or the exact title
# `create_docs_pr` passes to `gh pr create`.
#
# Verifies that:
#   1. guide.md still defines the single-line GUIDE_DOCS_PR_EXCLUDE expression
#      and applies it inside update_work_log()'s `new_prs` query, with
#      `headRefName` requested from `gh --json` (jq cannot filter a field gh was
#      not asked to return).
#   2. THE REGRESSION, executed rather than grepped: the expression extracted
#      verbatim FROM guide.md, run through the real jq pipeline, drops docs PRs
#      (by branch and by title) and keeps genuine work PRs.
#   3. Two consecutive ticks whose only merged PR above the watermark is a prior
#      docs PR both yield an empty `new_prs` — i.e. the second tick produces no
#      PR, which is the loop-termination property #5454 is about.
#   4. A tick with BOTH a genuine PR and a prior docs PR still logs the genuine
#      one (the filter excludes docs PRs, not everything).
#   5. The watermark helper `work_log_max_pr` reads back only what was written,
#      so an excluded docs PR never advances it — and is re-filtered next tick.
#
# Hermetic: pure jq/grep against a temp dir. No forge, network, or `gh` calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"

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

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
    exit 0
fi

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

SANDBOX="$(mktemp -d)"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

DOCS_TITLE='docs: Guide document maintenance update'

# ---------------------------------------------------------------------------
# Test 1: the prompt still carries the exclusion, wired into the real query
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines and applies the docs-PR exclusion"

assert_grep '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD" \
    "GUIDE_DOCS_PR_EXCLUDE is defined as a single-line assignment"
assert_grep 'select\(\(\$GUIDE_DOCS_PR_EXCLUDE\) \| not\)' "$GUIDE_MD" \
    "update_work_log()'s new_prs query applies the exclusion"
# Bracket the leading dash so grep does not read the pattern as an option.
assert_grep '[-]-json number,title,mergedAt,headRefName' "$GUIDE_MD" \
    "headRefName is requested from gh --json (jq cannot filter an absent field)"
assert_grep 'docs/guide-update' "$GUIDE_MD" \
    "exclusion matches the docs-worktree branch prefix"

# ---------------------------------------------------------------------------
# Extract the expression VERBATIM from guide.md and reconstruct the exact jq
# pipeline update_work_log() builds. Everything below executes the prompt's own
# filter, so a prompt edit that breaks it fails this suite.
# ---------------------------------------------------------------------------
EXCLUDE_LINE="$(grep -m1 '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD")"
# The assignment is `GUIDE_DOCS_PR_EXCLUDE='<expr>'` — a single-quoted literal.
GUIDE_DOCS_PR_EXCLUDE="${EXCLUDE_LINE#GUIDE_DOCS_PR_EXCLUDE=}"
GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE#\'}"
GUIDE_DOCS_PR_EXCLUDE="${GUIDE_DOCS_PR_EXCLUDE%\'}"

if [[ -n "$GUIDE_DOCS_PR_EXCLUDE" ]]; then
    pass "extracted exclusion expression from guide.md"
else
    fail "could not extract exclusion expression from guide.md"
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    exit 1
fi

# Mirror of update_work_log()'s query, minus the `gh pr list` fetch: $1 = the
# merged-PR JSON gh would have returned, $2 = the watermark.
new_prs() {
    printf '%s\n' "$1" | jq -c \
        "[.[] | select(.number > $2) | select(($GUIDE_DOCS_PR_EXCLUDE) | not)] | sort_by(.mergedAt) | reverse"
}

# work_log_max_pr(), verbatim in behavior: highest `PR #N` recorded in the file.
work_log_max_pr() {
    { grep -oE 'PR #[0-9]+' "$1" 2>/dev/null | grep -oE '[0-9]+'; echo 0; } | sort -rn | head -1
}

# ---------------------------------------------------------------------------
# Test 2: the extracted expression drops docs PRs and keeps genuine ones
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: the exclusion drops docs PRs by branch AND by title"

MIXED_JSON=$(jq -n --arg t "$DOCS_TITLE" '[
  {number: 5410, title: "fix(daemon): mint a per-owner App token", mergedAt: "2026-08-05T10:00:00Z", headRefName: "feature/issue-5400"},
  {number: 5420, title: $t,                                        mergedAt: "2026-08-05T10:20:00Z", headRefName: "docs/guide-update-20260805-102000"},
  {number: 5430, title: "docs: update WORK_LOG, WORK_PLAN",        mergedAt: "2026-08-05T10:40:00Z", headRefName: "docs/guide-update-20260805-104000"},
  {number: 5440, title: $t,                                        mergedAt: "2026-08-05T11:00:00Z", headRefName: "chore/hand-renamed-branch"},
  {number: 5450, title: "feat(cli): add tokens check --ranking",   mergedAt: "2026-08-05T11:20:00Z", headRefName: "feature/issue-5445"}
]')

KEPT="$(new_prs "$MIXED_JSON" 0 | jq -c '[.[].number] | sort')"
assert_eq "$KEPT" "[5410,5450]" \
    "only the two genuine PRs survive (branch-matched 5420/5430 and title-matched 5440 dropped)"

TITLES="$(new_prs "$MIXED_JSON" 0 | jq -r "[.[] | select(.title == \"$DOCS_TITLE\")] | length")"
assert_eq "$TITLES" "0" "no surviving entry carries the docs-maintenance title"

BRANCHES="$(new_prs "$MIXED_JSON" 0 | jq -r '[.[] | select(.headRefName | startswith("docs/guide-update"))] | length')"
assert_eq "$BRANCHES" "0" "no surviving entry sits on a docs/guide-update-* branch"

ORDER="$(new_prs "$MIXED_JSON" 0 | jq -c '[.[].number]')"
assert_eq "$ORDER" "[5450,5410]" "surviving entries stay sorted newest-merged-first"

# ---------------------------------------------------------------------------
# Test 3: THE LOOP — two consecutive ticks whose only new PR is a docs PR
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: two consecutive ticks with only a prior docs PR produce nothing"

WORK_LOG="$SANDBOX/WORK_LOG.md"
cat > "$WORK_LOG" <<'EOF'
# Work Log

### 2026-08-05

- **PR #5410**: fix(daemon): mint a per-owner App token
EOF

# --- Tick 1: PR #5420 (a docs PR) merged since the watermark.
WM1="$(work_log_max_pr "$WORK_LOG")"
assert_eq "$WM1" "5410" "tick 1 watermark read back from the committed WORK_LOG.md"

TICK1_JSON=$(jq -n --arg t "$DOCS_TITLE" '[
  {number: 5410, title: "fix(daemon): mint a per-owner App token", mergedAt: "2026-08-05T10:00:00Z", headRefName: "feature/issue-5400"},
  {number: 5420, title: $t,                                        mergedAt: "2026-08-05T10:20:00Z", headRefName: "docs/guide-update-20260805-102000"}
]')
TICK1_LEN="$(new_prs "$TICK1_JSON" "$WM1" | jq 'length')"
assert_eq "$TICK1_LEN" "0" "tick 1: new_prs is empty -> update_work_log returns 1, no WORK_LOG entry"

# Nothing was appended, so the file is byte-identical and the watermark is unmoved.
# (Under the pre-#5454 behavior tick 1 would have appended `- **PR #5420**: docs:
# Guide document maintenance update` here, which is precisely what fed tick 2.)
WM2="$(work_log_max_pr "$WORK_LOG")"
assert_eq "$WM2" "5410" "tick 1 wrote nothing: watermark unchanged (docs PR never becomes a WORK_LOG line)"

# --- Tick 2: the docs PR from tick 1 would have merged by now. Under the bug
# this is where PR N+1 was born. It must still be empty.
TICK2_JSON=$(jq -n --arg t "$DOCS_TITLE" '[
  {number: 5410, title: "fix(daemon): mint a per-owner App token", mergedAt: "2026-08-05T10:00:00Z", headRefName: "feature/issue-5400"},
  {number: 5420, title: $t,                                        mergedAt: "2026-08-05T10:20:00Z", headRefName: "docs/guide-update-20260805-102000"},
  {number: 5432, title: $t,                                        mergedAt: "2026-08-05T10:50:00Z", headRefName: "docs/guide-update-20260805-105000"}
]')
TICK2_LEN="$(new_prs "$TICK2_JSON" "$WM2" | jq 'length')"
assert_eq "$TICK2_LEN" "0" "tick 2: still empty -> the self-perpetuating cycle terminates (no PR)"

# ...and it stays terminated however many docs PRs pile up above the watermark.
TICK_N_JSON=$(jq -n --arg t "$DOCS_TITLE" '[range(5420; 5460; 4) | {
  number: .,
  title: $t,
  mergedAt: "2026-08-05T12:00:00Z",
  headRefName: "docs/guide-update-20260805-120000"
}]')
TICKN_LEN="$(new_prs "$TICK_N_JSON" "$WM2" | jq 'length')"
assert_eq "$TICKN_LEN" "0" "N accumulated docs PRs above the watermark still yield no new content"

# ---------------------------------------------------------------------------
# Test 4: a genuine PR alongside a docs PR is still logged
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: a genuine merged PR still counts when a docs PR is also pending"

TICK3_JSON=$(jq -n --arg t "$DOCS_TITLE" '[
  {number: 5420, title: $t,                                      mergedAt: "2026-08-05T10:20:00Z", headRefName: "docs/guide-update-20260805-102000"},
  {number: 5435, title: "fix(daemon): state confirmed FLAGS-OFF", mergedAt: "2026-08-05T11:30:00Z", headRefName: "feature/issue-5433"}
]')
TICK3="$(new_prs "$TICK3_JSON" 5410)"
assert_eq "$(printf '%s' "$TICK3" | jq 'length')" "1" "exactly one entry survives"
assert_eq "$(printf '%s' "$TICK3" | jq -r '.[0].number')" "5435" "the surviving entry is the genuine PR"

# Simulate the append the Guide would perform, then confirm the watermark
# advances to the genuine PR only — the docs PR is never recorded.
{
    printf '\n### 2026-08-05\n\n'
    printf '%s\n' "$TICK3" | jq -r '.[] | "- **PR #\(.number)**: \(.title)"'
} >> "$WORK_LOG"

assert_eq "$(work_log_max_pr "$WORK_LOG")" "5435" \
    "watermark advances to the genuine PR after the append"
if grep -q "PR #5420" "$WORK_LOG"; then
    fail "the docs PR leaked into WORK_LOG.md"
else
    pass "no docs PR line was written to WORK_LOG.md"
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
