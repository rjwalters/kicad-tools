#!/usr/bin/env bash
# test-detect-dependency-cycle.sh - Tests for detect-dependency-cycle.sh
# (issue #5213), Champion's bounded cross-repo dependency-cycle detector.
#
# Champion's blocking evaluation was single-hop and same-repo: it asked "is the
# issue named in `Blocked by: #N` closed yet?" and re-derived "still blocked"
# forever when the answer could never become yes because the dependency graph
# contained a cycle. The motivating incident was cross-repo (an epic in one repo
# blocking a dependent in another, whose own remaining phase fed that epic's last
# phase) and was found only by an operator walking the graph by hand.
#
# This suite is hybrid, mirroring test-dependency-parse.sh's strategy:
#
#   1. `parse_dependency_refs` / `_cycle_segment` / `_in_set` are real,
#      sourceable functions (the script guards its main on `BASH_SOURCE == $0`),
#      so they are tested directly - no duplicated mirror to drift.
#   2. The walk, the bounds, the reporting and the exit codes are exercised
#      black-box, by stubbing `gh` on PATH (the same approach as
#      test-check-duplicate.sh) and running the real script as a subprocess.
#   3. The two Champion prose call sites are pinned with literal
#      `assert_doc_contains` checks, so wiring the detector out of
#      champion-pr-merge.md / champion-issue-promo.md fails here.
#
# Hermetic: no network, no live forge, no tokens. Every read goes to the stub.
#
# Usage:
#   ./.loom/scripts/tests/test-detect-dependency-cycle.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
DDC="$SCRIPTS_DIR/detect-dependency-cycle.sh"

# Two `..` reaches repo-root/.claude/commands/loom for an INSTALLED copy
# (SCRIPTS_DIR is .loom/scripts there); one `..` reaches defaults/.claude/
# commands/loom when running inside this source repo (SCRIPTS_DIR is
# defaults/scripts) -- the two layouts differ in depth, so probe both rather
# than hard-coding one (#6725).
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
CHAMPION_MERGE_MD="$PROMPT_DIR/champion-pr-merge.md"
CHAMPION_PROMO_MD="$PROMPT_DIR/champion-issue-promo.md"

# Source the script for its pure helpers BEFORE defining our own colors - the
# script's own `[[ -t 2 ]]` block defines RED/YELLOW/BLUE/NC, so sourcing first
# (and setting ours last) keeps this file's output colors consistent.
# Pin the loom-daemon this suite tests against — the subject is a thin stub over
# `loom-daemon detect-dependency-cycle` now (epic #7810 PR 3). FATAL, not SKIP: see the helper.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "detect-dependency-cycle"

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
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected NOT to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

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
# Where the pure-helper unit tests went (epic #7810, PR 3)
# =====================================================================
#
# This suite used to `source "$DDC"` and call parse_dependency_refs and
# _cycle_segment directly. Both are now Rust —
# `loom-daemon/src/dep_classify/refs.rs` and `cycle.rs` — with their own unit
# tests covering the same cases: the #4508 phrase vocabulary (bold/italic/colon
# wrappings, comma lists capturing every ref on the line, a bare #N with no
# phrase not being an edge), the cross-repo forms, and a cycle segment that
# reports the loop rather than the whole approach path.
#
# #7943 landed the refs port beside a differential test that ran the Rust
# function and this shell function over the same fixtures and asserted they
# agreed. That test is deleted with the shell it compared against; the evidence
# is the merged CI run, not a fixture pinning a deleted file.
#
# What remains below is what can still be proven both ways: the black-box
# assertions, written against the shell, run unchanged against the Rust CLI.

# =====================================================================
# Black-box tests: stub `gh` on PATH, run the real script
# =====================================================================

STUB_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ddc-test.XXXXXX")"
cleanup() { rm -rf "$STUB_DIR"; }
trap cleanup EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Minimal `gh` stub: serves issue fixtures from files named issue-<owner>_<repo>_<N>.json
STUB_DIR="$(cd "$(dirname "$0")" && pwd)"
printf '%s\n' "$*" >> "$STUB_DIR/calls.log"

[[ "${1:-}" == "--version" ]] && { echo "gh version 0.0.0 (stub)"; exit 0; }
[[ "${1:-}" != "issue" ]] && { echo "stub gh: unhandled args: $*" >&2; exit 3; }

action="$2"; num="$3"; shift 3
repo=""; jqexpr=""; bodyarg=""; label=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)       repo="$2"; shift 2 ;;
    --json)       shift 2 ;;
    --jq|-q)      jqexpr="$2"; shift 2 ;;
    --body)       bodyarg="$2"; shift 2 ;;
    --add-label)  label="$2"; shift 2 ;;
    *)            shift ;;
  esac
done

key="$(printf '%s' "$repo#$num" | tr '/#' '__')"
f="$STUB_DIR/issue-$key.json"

case "$action" in
  view)
    [[ -f "$f" ]] || { echo "gh: issue not found" >&2; exit 1; }
    if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" < "$f"; else cat "$f"; fi
    ;;
  comment)
    [[ -f "$f" ]] || { echo "gh: issue not found" >&2; exit 1; }
    printf '%s\n' "$bodyarg" >> "$STUB_DIR/comments-$key.log"
    tmp="$(mktemp)"
    jq --arg b "$bodyarg" '.comments += [{"body":$b}]' "$f" > "$tmp" && mv "$tmp" "$f"
    ;;
  edit)
    [[ -f "$f" ]] || { echo "gh: issue not found" >&2; exit 1; }
    printf '%s\n' "$label" >> "$STUB_DIR/labels-$key.log"
    ;;
  *)
    echo "stub gh: unhandled issue action: $action" >&2; exit 3 ;;
esac
STUB
chmod +x "$STUB_DIR/gh"

export PATH="$STUB_DIR:$PATH"
export GH_CACHE_DISABLE=1

# fixture <owner/repo#N> <STATE> <body>
fixture() {
    local node="$1" state="$2" body="$3"
    local key
    key="$(printf '%s' "$node" | tr '/#' '__')"
    jq -n --arg s "$state" --arg b "$body" \
        '{state:$s, body:$b, comments:[]}' > "$STUB_DIR/issue-$key.json"
}

reset_state() {
    rm -f "$STUB_DIR"/issue-*.json "$STUB_DIR"/comments-*.log \
          "$STUB_DIR"/labels-*.log "$STUB_DIR/calls.log"
}

# run_ddc <args...> -> sets OUT / RC
run_ddc() {
    OUT="$("$DDC" --no-cache "$@" 2>"$STUB_DIR/stderr.log")"
    RC=$?
}

echo
echo "--- direct 2-cycle (same repo) ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #2'
fixture 'o/r#2' OPEN 'Blocked by #1'
run_ddc --issue 1 --repo o/r
assert_eq "1" "$RC" "exit 1 on a detected cycle"
assert_contains "$OUT" "CYCLE_DETECTED" "CYCLE_DETECTED marker present"
assert_contains "$OUT" "CYCLE_PATH: o/r#1 -> o/r#2 -> o/r#1" "cycle path names both sides and closes the loop"
assert_contains "$OUT" "CYCLE_NODES: o/r#1 o/r#2" "cycle node set listed"

echo
echo "--- 3-hop cycle (A -> B -> C -> A) ---"
reset_state
fixture 'o/r#1' OPEN 'Depends on #2'
fixture 'o/r#2' OPEN 'Depends on #3'
fixture 'o/r#3' OPEN 'Depends on #1'
run_ddc --issue 1 --repo o/r
assert_eq "1" "$RC" "exit 1 on a 3-hop cycle"
assert_contains "$OUT" "CYCLE_PATH: o/r#1 -> o/r#2 -> o/r#3 -> o/r#1" "3-hop path reported in order"

echo
echo "--- cycle that does not pass through the root ---"
reset_state
fixture 'o/r#1' OPEN 'Depends on #2'
fixture 'o/r#2' OPEN 'Depends on #3'
fixture 'o/r#3' OPEN 'Depends on #2'
run_ddc --issue 1 --repo o/r
assert_eq "1" "$RC" "exit 1 - a cycle downstream of the root still deadlocks the root"
assert_contains "$OUT" "CYCLE_PATH: o/r#2 -> o/r#3 -> o/r#2" "reports the loop itself, not the approach path"

echo
echo "--- cross-repo cycle (the motivating incident's shape) ---"
reset_state
fixture 'example-org/downstream-repo#101' OPEN 'Blocked by example-org/tool-repo#202 (epic must land first)'
fixture 'example-org/tool-repo#202' OPEN 'Phase 8 Depends on https://github.com/example-org/downstream-repo/issues/101 canary output'
run_ddc --issue 101 --repo example-org/downstream-repo
assert_eq "1" "$RC" "exit 1 on a cycle spanning two repos"
assert_contains "$OUT" "CYCLE_PATH: example-org/downstream-repo#101 -> example-org/tool-repo#202 -> example-org/downstream-repo#101" \
    "cross-repo path names both repos"

echo
echo "--- cycle fingerprint is identical from either side of the cycle ---"
reset_state
fixture 'o/a#1' OPEN 'Blocked by o/b#2'
fixture 'o/b#2' OPEN 'Blocked by o/a#1'
run_ddc --issue 1 --repo o/a
FP_A="$(printf '%s\n' "$OUT" | sed -n 's/^CYCLE_FINGERPRINT: //p')"
run_ddc --issue 2 --repo o/b
FP_B="$(printf '%s\n' "$OUT" | sed -n 's/^CYCLE_FINGERPRINT: //p')"
assert_eq "$FP_A" "$FP_B" "same cycle discovered from either member yields one identity"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -n "$FP_A" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: fingerprint is non-empty"
else
    TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: fingerprint is non-empty"
fi

echo
echo "--- non-cyclic chain within the depth bound is NOT reported ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #2'
fixture 'o/r#2' OPEN 'Blocked by #3'
fixture 'o/r#3' OPEN 'Blocked by #4'
fixture 'o/r#4' OPEN 'No dependencies here.'
run_ddc --issue 1 --repo o/r
assert_eq "0" "$RC" "exit 0 on an ordinary deep chain (no false positive)"
assert_contains "$OUT" "NO_CYCLE" "NO_CYCLE marker present"
assert_contains "$OUT" "SCANNED: 4" "all four chain nodes were fetched exactly once"
assert_not_contains "$OUT" "SEARCH_TRUNCATED" "a chain that fits the bound is not marked truncated"

echo
echo "--- depth bound stops the walk and says so ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #2'
fixture 'o/r#2' OPEN 'Blocked by #3'
fixture 'o/r#3' OPEN 'Blocked by #4'
fixture 'o/r#4' OPEN 'Blocked by #5'
fixture 'o/r#5' OPEN 'Blocked by #6'
fixture 'o/r#6' OPEN 'Blocked by #1'
run_ddc --issue 1 --repo o/r --max-depth 2
assert_eq "0" "$RC" "exit 0 when the (real) cycle lies beyond the depth bound"
assert_contains "$OUT" "SEARCH_TRUNCATED: depth" "truncation is surfaced, so NO_CYCLE is not read as proof"
assert_contains "$OUT" "SCANNED: 2" "depth bound caps forge reads at root + (max-depth - 1) expandable nodes"
assert_not_contains "$(cat "$STUB_DIR/calls.log")" "issue view 3 " \
    "the frontier node beyond the bound is never fetched (a body we would never parse)"

echo
echo "--- a cycle closing exactly AT the depth bound is still detected ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #2'
fixture 'o/r#2' OPEN 'Blocked by #1'
run_ddc --issue 1 --repo o/r --max-depth 2
assert_eq "1" "$RC" "skipping the frontier FETCH does not skip the frontier back-edge CHECK"
assert_contains "$OUT" "CYCLE_PATH: o/r#1 -> o/r#2 -> o/r#1" "max-depth N still detects an N-node cycle"

echo
echo "--- node bound caps forge reads ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #2'
fixture 'o/r#2' OPEN 'Blocked by #3'
fixture 'o/r#3' OPEN 'Blocked by #4'
fixture 'o/r#4' OPEN 'Blocked by #5'
fixture 'o/r#5' OPEN 'No dependencies here.'
run_ddc --issue 1 --repo o/r --max-nodes 3
assert_eq "0" "$RC" "exit 0 when the node budget is spent without finding a cycle"
assert_contains "$OUT" "SEARCH_TRUNCATED: nodes" "node-budget truncation is surfaced"
assert_contains "$OUT" "SCANNED: 3" "never fetches more than --max-nodes issues"

echo
echo "--- a CLOSED dependency is a resolved edge, not a live blocker ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #2'
fixture 'o/r#2' CLOSED 'Blocked by #1'
run_ddc --issue 1 --repo o/r
assert_eq "0" "$RC" "a loop through a CLOSED issue is not a live deadlock"
assert_contains "$OUT" "NO_CYCLE" "closed edge terminates the walk"

echo
echo "--- self-reference is not a cycle ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #1 (typo, refers to itself)'
run_ddc --issue 1 --repo o/r
assert_eq "0" "$RC" "a body naming its own number is not a dependency on itself"

echo
echo "--- an unreadable cross-repo node degrades, it does not crash ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by private/repo#9'
run_ddc --issue 1 --repo o/r
assert_eq "0" "$RC" "exit 0 when a referenced issue is not visible to this token"
assert_contains "$OUT" "UNREADABLE: private/repo#9" "unreadable node is surfaced as partial coverage"

echo
echo "--- --report: surfaces the cycle and routes to loom:operator-only ---"
reset_state
fixture 'example-org/downstream-repo#101' OPEN 'Blocked by example-org/tool-repo#202'
fixture 'example-org/tool-repo#202' OPEN 'Depends on example-org/downstream-repo#101'
run_ddc --issue 101 --repo example-org/downstream-repo --report
assert_eq "1" "$RC" "--report still exits 1 on a cycle"
assert_contains "$OUT" "REPORTED: example-org/downstream-repo#101" "REPORTED marker emitted"
COMMENT="$(cat "$STUB_DIR/comments-example-org_downstream-repo_101.log" 2>/dev/null || true)"
assert_contains "$COMMENT" "Dependency cycle detected" "comment states the cycle explicitly"
assert_contains "$COMMENT" "example-org/downstream-repo#101" "comment names this side of the cycle"
assert_contains "$COMMENT" "example-org/tool-repo#202" "comment names the OTHER side of the cycle"
assert_contains "$COMMENT" "champion:dep-cycle:" "comment carries the idempotency marker"
LABELS="$(cat "$STUB_DIR/labels-example-org_downstream-repo_101.log" 2>/dev/null || true)"
assert_contains "$LABELS" "loom:operator-only" "routes to loom:operator-only"
assert_contains "$LABELS" "loom:operator-decision" "routes to the loom:operator-decision sub-kind (#5671) — breaking a cycle is a judgement call"
# The blocker list is `sort -u`-ordered, so it reads downstream-repo before
# tool-repo for these fixture names.
assert_contains "$COMMENT" "Blocked by example-org/downstream-repo#101 example-org/tool-repo#202" "comment names the blocker(s) in machine-readable form (#5671)"

echo
echo "--- --report is idempotent: the same cycle is surfaced once ---"
run_ddc --issue 101 --repo example-org/downstream-repo --report
assert_eq "1" "$RC" "second pass still reports the cycle to its caller"
assert_contains "$OUT" "ALREADY_REPORTED: example-org/downstream-repo#101" "second pass skips the comment"
COMMENT_COUNT="$(jq '.comments | length' "$STUB_DIR/issue-example-org_downstream-repo_101.json")"
assert_eq "1" "$COMMENT_COUNT" "exactly one comment posted across two passes"

echo
echo "--- default (no --report) is strictly read-only ---"
reset_state
fixture 'o/r#1' OPEN 'Blocked by #2'
fixture 'o/r#2' OPEN 'Blocked by #1'
run_ddc --issue 1 --repo o/r
TESTS_RUN=$((TESTS_RUN + 1))
if ! ls "$STUB_DIR"/comments-*.log "$STUB_DIR"/labels-*.log >/dev/null 2>&1; then
    TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: no comment and no label without --report"
else
    TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: no comment and no label without --report"
fi

echo
echo "--- argument validation ---"
reset_state
run_ddc --repo o/r
assert_eq "2" "$RC" "missing --issue exits 2"
run_ddc --issue notanumber --repo o/r
assert_eq "2" "$RC" "non-numeric --issue exits 2"
run_ddc --issue 404 --repo o/r
assert_eq "2" "$RC" "unreadable ROOT issue exits 2 (no answer at all)"

echo
echo "--- Doc pins: the Champion prose actually calls the detector ---"

# The needles below are LITERAL prose fragments, quoted exactly as they appear in
# the .md files - the single quotes are load-bearing, not an expansion mistake.
# shellcheck disable=SC2016
{
    assert_doc_contains "$CHAMPION_MERGE_MD" "detect-dependency-cycle.sh" \
        "champion-pr-merge.md's unblock evaluation invokes the detector"
    assert_doc_contains "$CHAMPION_MERGE_MD" "--report" \
        "champion-pr-merge.md uses --report so a detected cycle is surfaced, not just logged"
    assert_doc_contains "$CHAMPION_MERGE_MD" 'if [ "$ALL_RESOLVED" = false ]; then' \
        "the walk is gated on the 'still blocked' branch, not run on every pass"
    assert_doc_contains "$CHAMPION_MERGE_MD" '"$GH_READ" issue view "$blocked" --json labels' \
        "an already-surfaced cycle (loom:operator-only) short-circuits the walk with one cached read"
    assert_doc_contains "$CHAMPION_PROMO_MD" "detect-dependency-cycle.sh" \
        "champion-issue-promo.md's Technical Feasibility check invokes the detector"
    assert_doc_contains "$CHAMPION_PROMO_MD" 'if [ "$DECLARES_DEP" -gt 0 ]; then' \
        "promotion runs the walk only when the proposal declares a dependency at all"
}

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
