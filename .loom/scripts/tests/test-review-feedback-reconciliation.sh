#!/usr/bin/env bash
# test-review-feedback-reconciliation.sh - Regression coverage for the formal
# review / inline thread reconciliation gate (#7647).
#
# THE FAILURE SHAPE THIS PINS DOWN
#
#   In kicad-tools#5369 a Judge approved a PR at head 23a0289 while an
#   unresolved formal CHANGES_REQUESTED review sat at that same head. The
#   approving pass had read the PR's ISSUE comments (`/issues/{n}/comments`)
#   and never the formal reviews (`/pulls/{n}/reviews`), so a positive issue
#   comment was the only feedback it saw. Green CI did not exercise the missing
#   requirement either.
#
#   The suite therefore asserts, against a fully mocked forge:
#     T1  a blocking review that only appears on PAGE 2 is still found
#     T2  same-head CHANGES_REQUESTED + a positive issue comment cannot produce
#         an unqualified approval (the #5369 shape)
#     T3  an unresolved inline review THREAD blocks
#     T4  an older-head finding needs reconciliation, and MAY proceed once the
#         caller supplies evidence citing it
#     T5  a read failure / incomplete pagination fails CLOSED (never "clean")
#     T6  a clean, fully-reconciled review state approves normally
#
# Black-box: both subjects are full CLI scripts, so `gh` is stubbed on PATH and
# the real scripts run as subprocesses (same pattern as test-post-verdict.sh).
# The stub emulates `gh api --paginate --jq` faithfully — it applies the
# caller's real jq filter to each fixture page in turn — so the pagination
# assertions exercise the production jq filters, not a paraphrase of them.
#
# Usage:
#   ./.loom/scripts/tests/test-review-feedback-reconciliation.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
GATE="$SCRIPTS_DIR/check-review-feedback.sh"
POST_VERDICT="$SCRIPTS_DIR/post-verdict.sh"

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

assert_contains() {
  local haystack="$1" needle="$2" msg="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $msg"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $msg"
    echo "    Expected substring: '$needle'"
    echo "    In: '$haystack'"
  fi
}

assert_not_contains() {
  local haystack="$1" needle="$2" msg="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $msg"
    echo "    Unexpected substring: '$needle'"
    echo "    In: '$haystack'"
  else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $msg"
  fi
}

if [[ ! -x "$GATE" ]]; then
  echo -e "${RED}FATAL${NC}: $GATE not found or not executable" >&2
  exit 2
fi
if [[ ! -x "$POST_VERDICT" ]]; then
  echo -e "${RED}FATAL${NC}: $POST_VERDICT not found or not executable" >&2
  exit 2
fi
if ! command -v jq > /dev/null 2>&1; then
  echo "SKIP: jq is not installed (required to emulate gh's --jq filtering)" >&2
  exit 0
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR" 2>/dev/null || true' EXIT
STUB_DIR="$WORK_DIR/bin"
FIX_DIR="$WORK_DIR/fixtures"
mkdir -p "$STUB_DIR" "$FIX_DIR"

HEAD_SHA="23a0289f3e23a98369502250c2be4a9f73c60e51"
OLD_SHA="1111111111111111111111111111111111111111"

# --- Stub gh ---------------------------------------------------------------
# Handles exactly what the subjects call:
#   gh api repos/O/R/pulls/N                       --jq .head.sha
#   gh api repos/O/R/pulls/N/reviews?per_page=100  --paginate --jq FILTER
#   gh api repos/O/R/pulls/N/comments?per_page=100 --paginate --jq FILTER
#   gh api graphql -f query=... -F ...
#   gh pr comment N --body B
# Fixture files drive the responses; `fail-<name>` marker files inject failures.
cat > "$STUB_DIR/gh" << 'STUB'
#!/usr/bin/env bash
FIX="${LOOM_TEST_FIXTURE_DIR:?stub gh: LOOM_TEST_FIXTURE_DIR not set}"

if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  pr_num="$3"
  body=""
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    [[ "${args[i]}" == "--body" ]] && body="${args[i + 1]}"
  done
  printf '%s\n' "$pr_num" > "$FIX/last-pr.txt"
  printf '%s' "$body" > "$FIX/last-body.txt"
  echo "https://github.com/owner/repo/pull/$pr_num#issuecomment-1"
  exit 0
fi

if [[ "$1" != "api" ]]; then
  echo "stub gh: unhandled args: $*" >&2
  exit 3
fi
shift

endpoint="$1"
shift
jq_filter=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jq)
      jq_filter="$2"
      shift 2
      ;;
    --paginate)
      shift
      ;;
    -f | -F)
      shift 2
      ;;
    *) shift ;;
  esac
done

emit_pages() {
  # Emulates `gh api --paginate --jq FILTER`: the filter is applied to EACH
  # page, and the pages are concatenated — exactly how gh streams them.
  local glob="$1" page found=0
  for page in "$FIX"/$glob; do
    [[ -f "$page" ]] || continue
    found=1
    if [[ -n "$jq_filter" ]]; then
      jq -r "$jq_filter" "$page" || exit 1
    else
      cat "$page"
    fi
  done
  [[ "$found" -eq 1 ]] || printf ''
  exit 0
}

case "$endpoint" in
  graphql)
    [[ -f "$FIX/fail-graphql" ]] && exit 1
    [[ -f "$FIX/graphql-threads.json" ]] || exit 1
    cat "$FIX/graphql-threads.json"
    exit 0
    ;;
  */pulls/*/reviews*)
    [[ -f "$FIX/fail-reviews" ]] && {
      echo "stub gh: simulated reviews read failure" >&2
      exit 1
    }
    emit_pages 'reviews-page-*.json'
    ;;
  */pulls/*/comments*)
    [[ -f "$FIX/fail-inline" ]] && {
      echo "stub gh: simulated inline-comment read failure" >&2
      exit 1
    }
    emit_pages 'inline-page-*.json'
    ;;
  */pulls/*)
    [[ -f "$FIX/fail-pr" ]] && exit 1
    if [[ -n "$jq_filter" ]]; then
      jq -r "$jq_filter" "$FIX/pr.json"
    else
      cat "$FIX/pr.json"
    fi
    exit 0
    ;;
esac

echo "stub gh: unhandled api endpoint: $endpoint" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_FIXTURE_DIR="$FIX_DIR"
export LOOM_FORGE_TYPE="github"
export PATH="$STUB_DIR:$PATH"

printf '{"head":{"sha":"%s"}}\n' "$HEAD_SHA" > "$FIX_DIR/pr.json"

reset_fixtures() {
  rm -f "$FIX_DIR"/reviews-page-*.json "$FIX_DIR"/inline-page-*.json \
    "$FIX_DIR/graphql-threads.json" "$FIX_DIR"/fail-* \
    "$FIX_DIR/last-pr.txt" "$FIX_DIR/last-body.txt"
  # Default: no inline threads at all (GraphQL answers with an empty node list).
  cat > "$FIX_DIR/graphql-threads.json" << 'JSON'
{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}
JSON
  printf '[]\n' > "$FIX_DIR/inline-page-1.json"
  printf '[]\n' > "$FIX_DIR/reviews-page-1.json"
}

run_gate() {
  set +e
  GATE_OUT=$("$GATE" --repo owner/repo --quiet "$@" 2>&1)
  GATE_RC=$?
  set -e
}

run_pv() {
  set +e
  PV_OUT=$("$POST_VERDICT" "$@" 2>&1)
  PV_RC=$?
  set -e
  PV_BODY="$(cat "$FIX_DIR/last-body.txt" 2>/dev/null || true)"
  PV_PR="$(cat "$FIX_DIR/last-pr.txt" 2>/dev/null || true)"
}

echo "Testing formal-review / inline-thread reconciliation gate (#7647)..."
echo ""

# --- T1: a blocking review that only appears on PAGE 2 is still found -------
# The pre-#7647 helper made ONE unpaginated request; a CHANGES_REQUESTED review
# on page 2 was simply invisible.
echo "T1: blocking review on page 2 of pagination"
reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[
  {"id":1001,"state":"COMMENTED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T20:00:00Z","user":{"login":"carol"},"body":"drive-by note"},
  {"id":1002,"state":"APPROVED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T20:30:00Z","user":{"login":"dave"},"body":"looks fine to me"}
]
JSON
cat > "$FIX_DIR/reviews-page-2.json" << JSON
[
  {"id":5202931719,"state":"CHANGES_REQUESTED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T21:10:12Z","user":{"login":"alice"},"body":"DFM-analysis coverage is explicitly required before this is ready."}
]
JSON
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "10" "$GATE_RC" "page-2 CHANGES_REQUESTED -> exit 10 (BLOCKING)"
assert_contains "$GATE_OUT" "REVIEW_FEEDBACK_STATE=BLOCKING" "state is BLOCKING"
assert_contains "$GATE_OUT" "REVIEWS_TOTAL=3" "all 3 reviews across both pages were ingested"
assert_contains "$GATE_OUT" "REVIEWS_BLOCKING_CURRENT_HEAD=1" "the page-2 review is counted at the current head"
assert_contains "$GATE_OUT" "REVIEW_BLOCKING_IDS=\"5202931719\"" "the blocking review id is reported for citation"

# --- T2: the #5369 shape — same-head CHANGES_REQUESTED + positive issue -----
#     comment must NOT yield an unqualified approval.
echo ""
echo "T2: same-head formal CHANGES_REQUESTED + a positive issue comment (#5369 shape)"
# (Fixtures from T1 still describe the incident: an unresolved same-head
# CHANGES_REQUESTED review. The 'positive issue comment' is the approval body
# below — exactly the input the failing Judge acted on.)
run_pv 5369 approved "$HEAD_SHA" --body "Approved — the linked issue comment says coverage is a downstream consumer responsibility, and CI is green."
assert_eq "3" "$PV_RC" "approval is REFUSED by the reconciliation gate (exit 3)"
assert_eq "" "$PV_PR" "no approval comment was posted"
assert_contains "$PV_OUT" "5202931719" "refusal names the formal review the Judge never read"
assert_contains "$PV_OUT" "--reviews-reconciled" "refusal tells the caller how to disposition it"

# A blanket 'reconciled' sentence that never engages with the finding is also
# refused — the disposition must cite the blocking review id.
run_pv 5369 approved "$HEAD_SHA" --body "Approved." \
  --reviews-reconciled "I looked at the review feedback and consider everything to be adequately addressed already."
assert_eq "3" "$PV_RC" "blanket reconciliation text that cites no review id -> still refused"
assert_eq "" "$PV_PR" "still no approval comment posted"

# With the id cited and a real disposition, the approval posts AND carries the
# reconciliation on the forge record.
run_pv 5369 approved "$HEAD_SHA" --body "Approved." \
  --reviews-reconciled "Review 5202931719 (DFM-analysis coverage) remains in scope and was addressed in commit 23a0289: tests/test_dfm_coverage.py now exercises both modules; nothing is deferred to the consumer."
assert_eq "0" "$PV_RC" "explicit, id-citing reconciliation -> approval posts"
assert_contains "$PV_BODY" "<!-- loom:review-reconciliation state=blocking" "posted comment carries the reconciliation marker"
assert_contains "$PV_BODY" "5202931719" "posted comment records which finding was dispositioned"
assert_contains "$PV_BODY" "<!-- loom:verdict-sha sha=$HEAD_SHA verdict=approved -->" "verdict-sha marker is still appended (#6382 unbroken)"

# --- T3: unresolved inline review thread blocks -----------------------------
echo ""
echo "T3: unresolved inline review thread"
reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[{"id":2001,"state":"APPROVED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T20:30:00Z","user":{"login":"dave"},"body":"ok"}]
JSON
cat > "$FIX_DIR/graphql-threads.json" << 'JSON'
{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
  {"id":"PRRT_res","isResolved":true,"isOutdated":false,"comments":{"nodes":[{"path":"src/a.py","author":{"login":"erin"},"body":"nit: rename"}]}},
  {"id":"PRRT_open","isResolved":false,"isOutdated":false,"comments":{"nodes":[{"path":"src/dfm.py","author":{"login":"alice"},"body":"this branch is never exercised by a test"}]}}
]}}}}}
JSON
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "10" "$GATE_RC" "unresolved non-outdated inline thread -> exit 10 (BLOCKING)"
assert_contains "$GATE_OUT" "INLINE_THREADS_UNRESOLVED=1" "only the unresolved thread is counted"
assert_contains "$GATE_OUT" "INLINE_RESOLUTION_SOURCE=graphql" "resolution state came from the supported interface"
assert_contains "$GATE_OUT" "INLINE_BLOCKING_IDS=\"PRRT_open\"" "the unresolved thread's id is reported for citation, the resolved one is not"

run_pv 5369 approved "$HEAD_SHA" --body "Approved, CI green."
assert_eq "3" "$PV_RC" "approval refused while an inline thread is unresolved"
assert_eq "" "$PV_PR" "no approval comment posted"
assert_contains "$PV_OUT" "PRRT_open" "refusal names the unresolved inline thread id"

# A blanket disposition that never cites the thread id is refused, exactly
# like an uncited formal-review id in T2 — an inline-thread-only block must
# not be waved through on a citation that only ever names formal review ids.
run_pv 5369 approved "$HEAD_SHA" --body "Approved." \
  --reviews-reconciled "I looked at the inline feedback and consider everything adequately addressed already."
assert_eq "3" "$PV_RC" "blanket reconciliation text that cites no thread id -> still refused"
assert_eq "" "$PV_PR" "still no approval comment posted"

# Citing the thread id lets the approval through.
run_pv 5369 approved "$HEAD_SHA" --body "Approved." \
  --reviews-reconciled "Thread PRRT_open (missing test coverage for the branch) was addressed in commit 23a0289: the branch is now exercised by tests/test_dfm_branch.py."
assert_eq "0" "$PV_RC" "explicit, thread-id-citing reconciliation -> approval posts"
assert_contains "$PV_BODY" "PRRT_open" "posted comment records which inline thread was dispositioned"

# --- T4: older-head finding, repaired, with evidence ------------------------
echo ""
echo "T4: older-head finding repaired with evidence"
reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[{"id":3003,"state":"CHANGES_REQUESTED","commit_id":"$OLD_SHA","submitted_at":"2026-09-13T10:00:00Z","user":{"login":"alice"},"body":"missing test for the failure path"}]
JSON
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "11" "$GATE_RC" "older-head CHANGES_REQUESTED -> exit 11 (NEEDS_RECONCILIATION), NOT auto-dismissed"
assert_contains "$GATE_OUT" "REVIEWS_BLOCKING_OLDER_HEAD=1" "the finding is reported as older-head"
assert_contains "$GATE_OUT" "REVIEWS_BLOCKING_CURRENT_HEAD=0" "it is not misreported as a current-head blocker"

run_pv 5369 approved "$HEAD_SHA" --body "Approved."
assert_eq "3" "$PV_RC" "a moved head alone does NOT clear the older-head finding"

run_pv 5369 approved "$HEAD_SHA" --body "Approved." \
  --reviews-reconciled "Review 3003 (missing failure-path test) was raised at 1111111 and repaired in 23a0289 — tests/test_failure_path.py::test_raises covers it; verified by running the suite."
assert_eq "0" "$PV_RC" "older-head finding + evidence of repair -> approval proceeds"
assert_contains "$PV_BODY" "state=needs_reconciliation" "marker records that this approval cleared an older-head finding"
assert_contains "$PV_BODY" "repaired in 23a0289" "the repair evidence itself is on the forge record"

# --- T5: read failure / incomplete pagination fails CLOSED ------------------
echo ""
echo "T5: read failure / incomplete pagination"
reset_fixtures
touch "$FIX_DIR/fail-reviews"
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "12" "$GATE_RC" "failed reviews read -> exit 12 (UNKNOWN)"
assert_contains "$GATE_OUT" "REVIEW_FEEDBACK_STATE=UNKNOWN" "a failed read is UNKNOWN, never CLEAR"
assert_not_contains "$GATE_OUT" "REVIEW_FEEDBACK_STATE=CLEAR" "a failed read never reports CLEAR"

run_pv 5369 approved "$HEAD_SHA" --body "Approved, CI green."
assert_eq "3" "$PV_RC" "approval refused when the review read did not complete (fail closed)"
assert_eq "" "$PV_PR" "no approval comment posted on an unknown review state"

# An inline read that fails with GraphQL also unavailable is UNKNOWN too.
reset_fixtures
touch "$FIX_DIR/fail-graphql" "$FIX_DIR/fail-inline"
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "12" "$GATE_RC" "both thread interfaces failing -> exit 12 (UNKNOWN)"

# GraphQL unavailable but REST inline comments readable: resolution state is
# unknown, so any inline comment needs reconciliation — it is never assumed
# resolved.
reset_fixtures
touch "$FIX_DIR/fail-graphql"
cat > "$FIX_DIR/inline-page-1.json" << JSON
[{"id":4004,"pull_request_review_id":2001,"in_reply_to_id":null,"commit_id":"$HEAD_SHA","original_commit_id":"$HEAD_SHA","path":"src/dfm.py","position":7,"user":{"login":"alice"},"body":"still not covered"}]
JSON
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "11" "$GATE_RC" "GraphQL down + an inline comment -> NEEDS_RECONCILIATION, not CLEAR"
assert_contains "$GATE_OUT" "INLINE_RESOLUTION_SOURCE=rest-unknown" "the degraded resolution source is reported"

# A GraphQL outage with NO inline comments at all is genuinely clean.
reset_fixtures
touch "$FIX_DIR/fail-graphql"
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "0" "$GATE_RC" "GraphQL down with zero inline comments -> CLEAR (complete REST read)"

# --- T6: clean, fully reconciled review state approves ----------------------
echo ""
echo "T6: clean, fully reconciled review state"
reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[
  {"id":6001,"state":"CHANGES_REQUESTED","commit_id":"$OLD_SHA","submitted_at":"2026-09-13T10:00:00Z","user":{"login":"alice"},"body":"missing test"},
  {"id":6002,"state":"APPROVED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T22:00:00Z","user":{"login":"alice"},"body":"fixed, thanks"}
]
JSON
cat > "$FIX_DIR/graphql-threads.json" << 'JSON'
{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
  {"id":"PRRT_done","isResolved":true,"isOutdated":false,"comments":{"nodes":[{"path":"src/a.py","author":{"login":"alice"},"body":"nit"}]}}
]}}}}}
JSON
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "0" "$GATE_RC" "reviewer's later APPROVED supersedes their own earlier request -> CLEAR"
assert_contains "$GATE_OUT" "REVIEW_FEEDBACK_STATE=CLEAR" "state is CLEAR"
assert_contains "$GATE_OUT" "REVIEWS_BLOCKING_CURRENT_HEAD=0" "nothing outstanding at the current head"

run_pv 5369 approved "$HEAD_SHA" --body "Approved — all checks pass."
assert_eq "0" "$PV_RC" "clean review state -> approval posts with no extra ceremony"
assert_contains "$PV_BODY" "<!-- loom:review-reconciliation state=clear" "a CLEAR approval still records that the gate ran"
assert_contains "$PV_BODY" "<!-- loom:verdict-sha sha=$HEAD_SHA verdict=approved -->" "verdict-sha marker preserved"

# --- T7: DISMISSED / COMMENTED states are respected, not invented -----------
echo ""
echo "T7: forge-authoritative states"
reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[
  {"id":7001,"state":"CHANGES_REQUESTED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T09:00:00Z","user":{"login":"alice"},"body":"nope"},
  {"id":7002,"state":"DISMISSED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T09:30:00Z","user":{"login":"alice"},"body":""},
  {"id":7003,"state":"COMMENTED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T09:40:00Z","user":{"login":"bob"},"body":"just a thought"}
]
JSON
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "0" "$GATE_RC" "a review dismissed THROUGH THE FORGE is not re-raised; COMMENTED does not block"

reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[{"id":7101,"state":"WEIRD_NEW_STATE","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T09:00:00Z","user":{"login":"alice"},"body":"?"}]
JSON
run_gate --number 5369 --head-sha "$HEAD_SHA"
assert_eq "11" "$GATE_RC" "an unrecognized review state is NOT treated as benign (fail closed)"
assert_contains "$GATE_OUT" "REVIEWS_UNKNOWN_STATE=1" "the unrecognized state is counted"

# An ABBREVIATED head sha must not make a current-head finding look older —
# post-verdict.sh accepts 7-40 char SHAs, so the gate has to tolerate one.
reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[{"id":7201,"state":"CHANGES_REQUESTED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T09:00:00Z","user":{"login":"alice"},"body":"still open"}]
JSON
run_gate --number 5369 --head-sha "${HEAD_SHA:0:7}"
assert_eq "10" "$GATE_RC" "an abbreviated head sha still matches the review's commit (BLOCKING, not older-head)"
assert_contains "$GATE_OUT" "REVIEWS_BLOCKING_CURRENT_HEAD=1" "abbreviated-sha head association is resolved correctly"

# --- T8: changes-requested verdicts are not gated ---------------------------
echo ""
echo "T8: the gate guards approvals only"
reset_fixtures
touch "$FIX_DIR/fail-reviews"
run_pv 5369 changes-requested "$HEAD_SHA" --body "Please add the missing test."
assert_eq "0" "$PV_RC" "a changes-requested verdict posts even when the review read fails"
assert_contains "$PV_BODY" "<!-- loom:verdict-sha sha=$HEAD_SHA verdict=changes-requested -->" "changes-requested marker unchanged"
assert_not_contains "$PV_BODY" "loom:review-reconciliation" "no reconciliation marker on a non-approval"

# --- T9: a MISSING gate script degrades loudly, it does not stall the fleet --
# A gate that is not installed says nothing about this PR's review state, and
# failing closed on it would stall every approval on every host until someone
# resynced. It degrades to the pre-#7647 behaviour — but warns, and stamps a
# `gate-unavailable` marker so the gap is visible on the forge record.
echo ""
echo "T9: gate script missing (install defect)"
reset_fixtures
ISOLATED="$WORK_DIR/isolated"
mkdir -p "$ISOLATED"
cp "$POST_VERDICT" "$ISOLATED/post-verdict.sh"
set +e
PV_OUT=$("$ISOLATED/post-verdict.sh" 5369 approved "$HEAD_SHA" --body "Approved." 2>&1)
PV_RC=$?
set -e
PV_BODY="$(cat "$FIX_DIR/last-body.txt" 2>/dev/null || true)"
assert_eq "0" "$PV_RC" "a missing gate script does not block the approval (availability over a self-inflicted stall)"
assert_contains "$PV_OUT" "WARNING" "the missing gate is reported loudly on stderr"
assert_contains "$PV_BODY" "state=gate-unavailable" "the forge record shows the approval was NOT review-reconciled"

# --- T10: id citation is boundary-anchored, not a bare substring match ------
# A naive `*"$id"*` match lets a short id spuriously match inside an unrelated
# longer number (e.g. blocking id "42" inside "4242"). The disposition must
# name the id as its own token.
echo ""
echo "T10: word-boundary-safe id citation"
reset_fixtures
cat > "$FIX_DIR/reviews-page-1.json" << JSON
[{"id":42,"state":"CHANGES_REQUESTED","commit_id":"$HEAD_SHA","submitted_at":"2026-09-14T09:00:00Z","user":{"login":"alice"},"body":"needs a fix"}]
JSON
run_pv 5369 approved "$HEAD_SHA" --body "Approved." \
  --reviews-reconciled "See PR #4242 and issue 4200 for background; this is fully resolved now, I promise."
assert_eq "3" "$PV_RC" "id '42' embedded only inside longer numbers (4242, 4200) does not satisfy citation"

run_pv 5369 approved "$HEAD_SHA" --body "Approved." \
  --reviews-reconciled "Review 42 (needs a fix) was addressed in commit 23a0289: the fix landed and is covered by a new test."
assert_eq "0" "$PV_RC" "id '42' cited as its own token satisfies citation"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
