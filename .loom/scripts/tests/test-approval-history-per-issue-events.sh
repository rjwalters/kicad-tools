#!/usr/bin/env bash
# test-approval-history-per-issue-events.sh - pins the per-issue events
# endpoint used by the approval-history helpers (PR #10005, #8742 review).
#
# `guide.md`'s `was_previously_approved()` and `park-record.md`'s
# `previous_review_label()` were switched (in the #8742 permanent-block work)
# to the repo-wide `gh api --paginate repos/{owner}/{repo}/issues/events`
# walk with an `.issue.number == N` jq filter. Champion's merge-risk hold
# named two defects in that shape (PR #10005 routing comment):
#   - Cost: a no-match lookup walks the whole repo's event stream — about
#     300 REST calls at ~100 events/page on this repo's volume.
#   - Correctness: the repo-wide endpoint stops at about 6 days of history,
#     so an issue approved more than ~6 days ago reads as "never approved".
#
# The fix routes both helpers through the per-issue endpoint WITH
# pagination: `gh api --paginate repos/{owner}/{repo}/issues/$N/events`.
# That bounds the walk by the issue's own event count, with no repo-wide
# horizon. Ordering: the per-issue endpoint returns events OLDEST-FIRST
# (chronologically ascending — verified by the Judge against this PR's own
# event history), so `previous_review_label()` takes the LAST match
# (`tail -1`); `was_previously_approved()` is existence-only, so its
# `head -1` has no order dependence.
#
# Verifies that:
#   1. All three shipped copies (guide.md, the loom-guide SKILL.md mirror,
#      park-record.md) call the per-issue endpoint
#      `repos/{owner}/{repo}/issues/${number}/events?per_page=100` with
#      `--paginate`, and no repo-wide `repos/{owner}/{repo}/issues/events`
#      call remains in any of them; no "newest-first" ordering claim
#      remains either (the endpoint is ascending).
#   2. THE REGRESSION, executed rather than grepped: the extracted
#      `was_previously_approved()` finds a `loom:issue` labeled event that
#      sits beyond the first 100 events of the issue (an unpaged per-issue
#      read would truncate it away — the original #8742 defect in mirror
#      image), and returns "false" without matching when the issue was
#      never approved.
#   3. `previous_review_label()` restores the LATEST review label under
#      ascending event order: a `changes-requested` followed by a later
#      `review-requested` restores `loom:review-requested`, and vice versa
#      — both cases fail against the old `head -1` code — defaulting to
#      `loom:review-requested` on empty/ambiguous history, and paginating
#      to a review label that sits beyond the first page.
#
# Hermetic: the `gh` CLI is replaced by a stub function that emulates
# `--paginate` (all events) vs unpaged (first per_page slice), validates the
# requested path is per-issue, and serves fixture JSON from a temp dir — no
# forge, network, or real `gh` calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# All three subjects are SHIPPED (installed into consumer repos), so resolve
# the installed path first (#6194/#6241), falling back to the defaults/
# source-tree path. In Loom's own dogfooded checkout the installed paths are
# symlinks onto defaults/, so both layouts land on the same file.
resolve_shipped() {
    local installed="$1" defaults="$2"
    if [[ -f "$REPO_ROOT/$installed" ]]; then
        echo "$REPO_ROOT/$installed"
    else
        echo "$REPO_ROOT/$defaults"
    fi
}

GUIDE_MD="$(resolve_shipped ".claude/commands/loom/guide.md" "defaults/.claude/commands/loom/guide.md")"
SKILL_MD="$(resolve_shipped ".agents/skills/loom-guide/SKILL.md" "defaults/.agents/skills/loom-guide/SKILL.md")"
PARK_MD="$(resolve_shipped ".loom/docs/park-record.md" "defaults/docs/park-record.md")"

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

assert_not_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then fail "$msg (forbidden pattern present: $pattern)"; else pass "$msg"; fi
}

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
    exit 0
fi

for f in "$GUIDE_MD" "$SKILL_MD" "$PARK_MD"; do
    if [[ ! -f "$f" ]]; then
        echo -e "${RED}FATAL${NC}: required file not found: $f"
        exit 1
    fi
done

SANDBOX="$(mktemp -d)"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Test 1: static pins — the per-issue endpoint, paginated, in all three
# copies; the repo-wide walk is gone from all three.
# ---------------------------------------------------------------------------
echo "Test 1: the per-issue endpoint is pinned in all three shipped copies"

PER_ISSUE_PIN='issues/\$\{number\}/events\?per_page=100'
REPO_WIDE_PATTERN='repos/\{owner\}/\{repo\}/issues/events'

for f in "$GUIDE_MD" "$SKILL_MD"; do
    assert_grep "$PER_ISSUE_PIN" "$f" \
        "$(basename "$(dirname "$(dirname "$(dirname "$f")")")")/$(basename "$f"): was_previously_approved uses the paginated per-issue endpoint"
done
assert_grep "$PER_ISSUE_PIN" "$PARK_MD" \
    "park-record.md: previous_review_label uses the paginated per-issue endpoint"

for f in "$GUIDE_MD" "$SKILL_MD" "$PARK_MD"; do
    assert_not_grep "$REPO_WIDE_PATTERN" "$f" \
        "$(basename "$f"): no repo-wide issues/events walk remains"
done

# `was_previously_approved` keeps `head -1` (existence-only: any match
# satisfies it, no order dependence); `previous_review_label` takes the
# LAST match — the endpoint is ascending, so `tail -1` is the latest.
assert_grep 'head -1' "$GUIDE_MD" "guide.md: was_previously_approved keeps head -1 (existence-only)"
assert_grep 'tail -1' "$PARK_MD" "park-record.md: previous_review_label takes the last (newest) match"
for f in "$GUIDE_MD" "$SKILL_MD" "$PARK_MD"; do
    assert_not_grep 'newest-first' "$f" \
        "$(basename "$f"): no false newest-first ordering claim remains"
done

# ---------------------------------------------------------------------------
# Executable regression harness: extract the real functions from the shipped
# markdown and run them against a stubbed `gh`.
# ---------------------------------------------------------------------------

# Extract a top-level `fn() { ... }` block from a markdown code fence.
extract_fn() {
    local file="$1" fn="$2"
    awk -v fn="$fn" '
        $0 == fn"() {" {inblock=1}
        inblock {print}
        inblock && $0 == "}" {exit}
    ' "$file"
}

# Stub `gh api`: emulates `--paginate` (emit ALL of the issue's events) vs
# unpaged (emit only the first per_page slice — the truncation being guarded
# against). Only per-issue paths are served; a repo-wide walk is refused
# loudly and logged. Every call is logged for assertions.
GH_CALL_LOG="$SANDBOX/gh-calls.log"
: > "$GH_CALL_LOG"
gh() {
    local paginate=false path="" jq_expr="" per_page=30
    local -a args=("$@")
    local i
    for ((i = 0; i < ${#args[@]}; i++)); do
        case "${args[$i]}" in
            --paginate) paginate=true ;;
            --jq)
                jq_expr="${args[$((i + 1))]}"
                ;;
            repos/*)
                if [[ -z "$path" ]]; then path="${args[$i]}"; fi
                ;;
        esac
    done
    echo "paginate=$paginate path=$path" >> "$GH_CALL_LOG"

    if [[ "$path" =~ ^repos/[^/]+/[^/]+/issues/events ]]; then
        echo "STUB-ERROR: repo-wide issues/events walk requested: $path" >&2
        echo "[]"
        return 0
    fi
    local number=""
    if [[ "$path" =~ issues/([0-9]+)/events ]]; then number="${BASH_REMATCH[1]}"; fi
    if [[ "$path" =~ per_page=([0-9]+) ]]; then per_page="${BASH_REMATCH[1]}"; fi

    local fixture="$SANDBOX/events-${number:-none}.json"
    local out=""
    if [[ -f "$fixture" ]]; then
        if [[ "$paginate" == "true" ]]; then
            out="$(cat "$fixture")"
        else
            out="$(jq -c ".[0:$per_page]" "$fixture")"
        fi
    else
        out="[]"
    fi

    # Real `gh api` applies --jq itself, printing final strings raw (jq -r
    # semantics); the stub must match, or label comparisons in the helpers
    # see a JSON-quoted string and never equal their targets.
    if [[ -n "$jq_expr" ]]; then
        printf '%s' "$out" | jq -r "$jq_expr"
    else
        printf '%s\n' "$out"
    fi
}

# Fixture writers. Events are ordered OLDEST-FIRST (GitHub's per-issue
# events endpoint returns them chronologically ascending), so index 0 is
# the EARLIEST event; a "beyond the first page" event is the NEWEST one,
# appended last.
filler_event() { # $1 = kind
    printf '{"event":"%s"}' "$1"
}
labeled_event() { # $1 = label name
    printf '{"event":"labeled","label":{"name":"%s"}}' "$1"
}
write_fixture() { # $1 = issue number, $2 = filler-event count, $3.. = event JSON fragments (appended after the fillers)
    local number="$1" nfill="$2"
    shift 2
    local -a evs=()
    local i arg
    for ((i = 0; i < nfill; i++)); do evs+=("$(filler_event closed)"); done
    for arg in "$@"; do evs+=("$arg"); done
    { printf '['
      for ((i = 0; i < ${#evs[@]}; i++)); do
          [[ $i -gt 0 ]] && printf ','
          printf '%s' "${evs[$i]}"
      done; printf ']'; } > "$SANDBOX/events-$number.json"
}

eval "$(extract_fn "$GUIDE_MD" was_previously_approved)"
eval "$(extract_fn "$PARK_MD" previous_review_label)"

if ! declare -F was_previously_approved >/dev/null; then
    fail "was_previously_approved() could not be extracted from guide.md"
    echo "================================"
    echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
    exit 1
fi
if ! declare -F previous_review_label >/dev/null; then
    fail "previous_review_label() could not be extracted from park-record.md"
    echo "================================"
    echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 2: was_previously_approved — THE REGRESSION. The approval event sits
# beyond the first 100 events; only a paginated read can find it.
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: was_previously_approved paginates past the first 100 events"

: > "$GH_CALL_LOG"
write_fixture 4242 100 "$(labeled_event loom:issue)"
assert_eq "$(was_previously_approved 4242)" "true" \
    "a loom:issue event beyond the first 100 events is found (pagination works)"

if grep -q '^paginate=true ' "$GH_CALL_LOG"; then
    pass "the helper requests --paginate"
else
    fail "the helper did not pass --paginate"
fi
if grep -qE '^paginate=[a-z]+ path=[^ ]*issues/4242/events' "$GH_CALL_LOG"; then
    pass "the helper queries issue 4242's own per-issue endpoint"
else
    fail "the helper did not query the per-issue endpoint for 4242"
fi
if grep -q 'STUB-ERROR' /dev/null 2>&1 || ! grep -q 'path=repos/{owner}/{repo}/issues/events' "$GH_CALL_LOG"; then
    pass "no repo-wide walk was requested"
else
    fail "a repo-wide issues/events path was requested"
fi

echo ""
echo "Test 3: was_previously_approved negatives"

: > "$GH_CALL_LOG"
write_fixture 4243 100   # approved event nowhere
assert_eq "$(was_previously_approved 4243)" "false" \
    "an issue never approved reads as false (no false promotion)"
write_fixture 4244 0 "$(labeled_event loom:blocked)" "$(labeled_event loom:triage)"
assert_eq "$(was_previously_approved 4244)" "false" \
    "other labels never satisfy the approval check"
assert_eq "$(was_previously_approved 999999)" "false" \
    "an issue with no events at all reads as false"

echo ""
echo "Test 4: sanity — the unpaged per-issue read WOULD have truncated the fixture"
# The mirror-image of the original defect: without --paginate the stub emits
# only the first 100 events, so the same fixture must read as unapproved.
SANITYFixture="$SANDBOX/events-4242.json"
if [[ "$(jq -c '.[0:100]' "$SANITYFixture" | jq '[.[] | select(.event == "labeled" and .label.name == "loom:issue")] | length')" == "0" ]]; then
    pass "fixture sanity: the approval event is NOT in the first 100 events"
else
    fail "fixture sanity: approval event unexpectedly within the first page"
fi

# ---------------------------------------------------------------------------
# Test 5: previous_review_label — ascending order, last match is the latest.
# Both orderings below FAIL against the old `head -1` code (which returned
# the first — i.e. oldest — match) and pass with `tail -1`.
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: previous_review_label restores the LATEST review label (ascending order)"

write_fixture 4301 0 "$(labeled_event loom:changes-requested)" "$(labeled_event loom:review-requested)"
assert_eq "$(previous_review_label 4301)" "loom:review-requested" \
    "changes-requested followed by a later review-requested restores loom:review-requested"

write_fixture 4302 0 "$(labeled_event loom:review-requested)" "$(labeled_event loom:changes-requested)"
assert_eq "$(previous_review_label 4302)" "loom:changes-requested" \
    "review-requested followed by a later changes-requested restores loom:changes-requested"

write_fixture 4303 0 "$(filler_event closed)"
assert_eq "$(previous_review_label 4303)" "loom:review-requested" \
    "no review-label history defaults to loom:review-requested (the safe default)"

assert_eq "$(previous_review_label 999999)" "loom:review-requested" \
    "empty history defaults to loom:review-requested"

echo ""
echo "Test 6: previous_review_label paginates to a review label beyond page 1"

write_fixture 4401 100 "$(labeled_event loom:changes-requested)"
assert_eq "$(previous_review_label 4401)" "loom:changes-requested" \
    "a changes-requested event beyond the first 100 events is found"
write_fixture 4402 100 "$(labeled_event loom:review-requested)"
assert_eq "$(previous_review_label 4402)" "loom:review-requested" \
    "a review-requested event beyond the first 100 events is found"

if grep -q 'path=repos/{owner}/{repo}/issues/events' "$GH_CALL_LOG"; then
    fail "a repo-wide issues/events path was requested at some point"
else
    pass "no repo-wide walk was ever requested across the executable tests"
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
