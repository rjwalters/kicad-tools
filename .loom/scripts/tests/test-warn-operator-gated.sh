#!/usr/bin/env bash
# test-warn-operator-gated.sh - Unit tests for warn-operator-gated.sh (#5137)
#
# The `all` sentinel's aggressive candidate taxonomy hard-skips only
# loom:operator-only; every other open issue is a build candidate, even one
# whose body declares operator-gating in prose without carrying the label.
# This scans each candidate's already-fetched body for (1) instruction-shaped
# operator-gate phrases and (2) a declared Depends on / Requires #A dependency
# that itself carries loom:operator-only — annotating (never skipping,
# never mutating labels) at the confirmation gate.
#
# Strategy (mirrors test-warn-out-of-set-deps.sh): warn-operator-gated.sh
# gates its main block on `BASH_SOURCE == $0`, so we `source` it directly
# (main does not run) to get the functions in scope, then stub `gh issue
# view` on PATH to serve canned body/labels and assert on stdout.
#
# Usage:
#   ./.loom/scripts/tests/test-warn-operator-gated.sh

# SC2034: CANDIDATES / REPO_NWO / MATCH_COUNT are read only by the functions
# sourced from warn-operator-gated.sh, invisible to the linter.
# shellcheck disable=SC2034

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SRC="$SCRIPTS_DIR/warn-operator-gated.sh"

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
    if printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
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
    if ! printf '%s' "$haystack" | grep -F -- "$needle" >/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Source the functions under test (main block is gated on BASH_SOURCE==$0) ---
if [[ ! -f "$SRC" ]]; then
    echo -e "${RED}FATAL${NC}: warn-operator-gated.sh not found at $SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$SRC"

if ! declare -f _warn_operator_gated >/dev/null; then
    echo -e "${RED}FATAL${NC}: could not source _warn_operator_gated from $SRC" >&2
    exit 2
fi

# --- Stub gh on PATH ---
#   gh issue view N --json body    -q .body                       -> cat $STUB_DIR/body-N.txt (or "")
#   gh issue view N --json labels  -q '[.labels[].name] | join(",")' -> cat $STUB_DIR/labels-N.txt (or "")
#   gh issue view N --json title   -q .title                      -> cat $STUB_DIR/title-N.txt (or "")
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
# Expect: gh issue view <N> [--repo X] --json <field> -q <jq>
if [[ "$1" == "issue" && "$2" == "view" ]]; then
  num="$3"
  field=""
  shift 3
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--json" ]]; then field="$2"; shift 2; continue; fi
    shift
  done
  case "$field" in
    body)
      f="$STUB_DIR_FROM_ENV/body-$num.txt"
      if [[ -f "$f" ]]; then cat "$f"; else echo ""; fi
      ;;
    labels)
      f="$STUB_DIR_FROM_ENV/labels-$num.txt"
      if [[ -f "$f" ]]; then cat "$f"; else echo ""; fi
      ;;
    title)
      f="$STUB_DIR_FROM_ENV/title-$num.txt"
      if [[ -f "$f" ]]; then cat "$f"; else echo ""; fi
      ;;
    *) echo "" ;;
  esac
  exit 0
fi
echo "stub gh: unhandled args: $*" >&2
exit 1
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# Helper: run _warn_operator_gated with given globals, capture stdout.
run_warn() {
    CANDIDATES="$1"
    REPO_NWO=""
    MATCH_COUNT=0
    _warn_operator_gated
}

# =====================================================================
echo "--- Phrase matcher: instruction-shaped fragments, not bare substrings ---"

out="$(_operator_gate_phrase_match 'the index is login-walled, so acquisition is operator-gated')"
assert_eq "operator-gated" "$out" "matches the first declared phrase in order (operator-gated)"

out="$(_operator_gate_phrase_match 'Operator decision: send this paired with the paper')"
assert_eq "operator decision:" "$out" "matches 'Operator decision:' case-insensitively"

out="$(_operator_gate_phrase_match 'waiting on operator to review this PR')" || true
assert_eq "" "$out" "bare 'operator' substring does NOT match (avoids false positive)"

out="$(_operator_gate_phrase_match 'store credentials in .env, already configured')" || true
assert_eq "" "$out" "bare 'credentials' substring does NOT match (avoids false positive)"

out="$(_operator_gate_phrase_match 'this step requires credentials from the vault')"
assert_eq "requires credentials" "$out" "'requires credentials' (instruction-shaped) matches"

# --- #6198: the most common operator-task phrasing ---

out="$(_operator_gate_phrase_match '**Operator task — requires human action, not automation.**')"
assert_eq "operator task" "$out" "the observed '#82' body matches ('operator task' wins declared order)"

out="$(_operator_gate_phrase_match '**Operator task — requires human action (Amazon Associates enrollment).**')"
assert_eq "operator task" "$out" "the observed '#83' body matches ('operator task' wins declared order)"

out="$(_operator_gate_phrase_match 'Rotating the key requires human action before the sweep can proceed')"
assert_eq "requires human action" "$out" "'requires human action' matches on its own (without 'operator task')"

out="$(_operator_gate_phrase_match 'This needs human action: someone must accept the ToS in the console')"
assert_eq "needs human action" "$out" "'needs human action' matches (symmetry with needs credentials)"

out="$(_operator_gate_phrase_match 'the human-in-the-loop design keeps a human in the review path')" || true
assert_eq "" "$out" "bare 'human' substring does NOT match (avoids false positive)"

out="$(_operator_gate_phrase_match 'operator precedence in the expression parser is wrong')" || true
assert_eq "" "$out" "'operator precedence' prose does NOT match (bare 'operator' still narrow)"

out="$(_operator_gate_phrase_match 'the task list tracks operator handoffs for each release')" || true
assert_eq "" "$out" "'task' and 'operator' apart do NOT match ('operator task' is a phrase, not two words)"

# =====================================================================
echo
echo "--- Dependency parser: reuses Depends on / Requires vocabulary ---"

out="$(parse_operator_gate_deps 'Depends on #4 (the FamilySearch extract)')"
assert_eq "4" "$out" "parses Depends on #A"

out="$(parse_operator_gate_deps 'Blocked by #200')"
assert_eq "" "$out" "Blocked by is NOT parsed (belongs to loom:blocked machinery)"

# =====================================================================
echo
echo "--- Title-prefix matcher: prefix at the true start, not a bare substring (#6391) ---"

out="$(_operator_gate_title_prefix_match 'Operator: visit the county archive and photograph the 1850 census page')"
assert_eq "Operator:" "$out" "title starting with 'Operator:' matches"

out="$(_operator_gate_title_prefix_match 'operator: renew the physical library card in person')"
assert_eq "Operator:" "$out" "matches case-insensitively (lowercase 'operator:')"

out="$(_operator_gate_title_prefix_match 'Operator — renew the domain registration')"
assert_eq "Operator —" "$out" "title starting with 'Operator —' (em dash) matches"

out="$(_operator_gate_title_prefix_match 'Operator-only: rotate the leaked production credential')"
assert_eq "Operator-only" "$out" "title starting with 'Operator-only' matches"

out="$(_operator_gate_title_prefix_match '  **Operator:** rotate the leaked production credential')"
assert_eq "Operator:" "$out" "leading whitespace/markdown decoration is stripped before matching"

out="$(_operator_gate_title_prefix_match '# Operator: quarterly archive visit')"
assert_eq "Operator:" "$out" "leading markdown heading marker is stripped before matching"

out="$(_operator_gate_title_prefix_match 'Fix operator dashboard rendering')" || true
assert_eq "" "$out" "'operator' mid-sentence (not a prefix) does NOT match"

out="$(_operator_gate_title_prefix_match 'Operatorship changes for the new fiscal year')" || true
assert_eq "" "$out" "'Operatorship' does NOT match (not followed by a declared prefix delimiter)"

# =====================================================================
echo
echo "--- Signal 1: direct phrase match annotates the candidate ---"

echo 'the index is login-walled, so acquisition is operator-gated (related: #4)' > "$STUB_DIR/body-87.txt"
echo '' > "$STUB_DIR/labels-4.txt"
out="$(run_warn "87")"
assert_contains "$out" $'87\t⚠ body declares operator-gating: "operator-gated"' \
    "#87 flagged for direct phrase match"

echo 'Operator decision: send this paired with the paper, once results and the paper are public' > "$STUB_DIR/body-65.txt"
out="$(run_warn "65")"
assert_contains "$out" $'65\t⚠ body declares operator-gating: "operator decision:"' \
    "#65 flagged for 'Operator decision:'"

# #6198: the two bodies observed unflagged in the 2AMLogic/pickwell run.
cat > "$STUB_DIR/body-82.txt" <<'BODY'
**Operator task — requires human action, not automation.**

Two live production credentials were pasted into the repo history and must be rotated.
BODY
out="$(run_warn "82")"
assert_contains "$out" $'82\t⚠ body declares operator-gating: "operator task"' \
    "#82 (credential rotation) flagged — was an unannotated 'would build' before #6198"

cat > "$STUB_DIR/body-83.txt" <<'BODY'
**Operator task — requires human action (Amazon Associates enrollment).**

Enrollment must be completed by a person with the account.
BODY
out="$(run_warn "83")"
assert_contains "$out" $'83\t⚠ body declares operator-gating: "operator task"' \
    "#83 (Associates enrollment) flagged — was an unannotated 'would build' before #6198"

# =====================================================================
echo
echo "--- Signal 2: dependency on a loom:operator-only issue annotates the candidate ---"

echo 'Depends on #4 (the FamilySearch extract in the operator queue)' > "$STUB_DIR/body-88.txt"
echo 'loom:operator-only' > "$STUB_DIR/labels-4.txt"
out="$(run_warn "88")"
assert_contains "$out" $'88\t⚠ depends on #4, which is loom:operator-only' \
    "candidate depending on a loom:operator-only issue is flagged"

# =====================================================================
echo
echo "--- Signal 2 (#5817): dependency on a loom:needs-capability issue annotates the candidate ---"

echo 'Depends on #5 (waiting on a capability request)' > "$STUB_DIR/body-92.txt"
echo 'loom:needs-capability' > "$STUB_DIR/labels-5.txt"
out="$(run_warn "92")"
assert_contains "$out" $'92\t⚠ depends on #5, which is loom:needs-capability' \
    "candidate depending on a loom:needs-capability issue is flagged"

# =====================================================================
echo
echo "--- Dependency on a NON-operator-only issue does NOT annotate ---"

echo 'Depends on #9' > "$STUB_DIR/body-89.txt"
echo 'loom:issue' > "$STUB_DIR/labels-9.txt"
out="$(run_warn "89")"
assert_eq "" "$out" "dependency without loom:operator-only produces no annotation"

# =====================================================================
echo
echo "--- Signal 3: title-prefix match annotates the candidate (#6391) ---"

# The exact shape from the incident: an unlabeled issue whose title opens
# with 'Operator:' and whose body contains none of the PHRASES vocabulary —
# previously invisible to this scan.
echo 'Operator: visit the county records office and photograph the 1850 census page' > "$STUB_DIR/title-94.txt"
echo 'The county archive is not digitized. Photograph page 12 of volume 3 in person.' > "$STUB_DIR/body-94.txt"
out="$(run_warn "94")"
assert_contains "$out" $'94\t⚠ title declares operator-gating: "Operator:"' \
    "#94 (title-prefixed 'Operator:', ordinary body) flagged by the title signal"

# Negative: the title merely contains the word "operator" mid-sentence — must
# NOT match (same false-positive discipline as the body-phrase vocabulary).
echo 'Fix operator dashboard rendering glitch' > "$STUB_DIR/title-95.txt"
echo 'The dashboard mis-renders the operator queue count on narrow viewports.' > "$STUB_DIR/body-95.txt"
out="$(run_warn "95")"
assert_eq "" "$out" "#95 (title mentions 'operator' mid-sentence, ordinary body) produces no annotation"

# Title signal fires even when the body is empty (checked independently of
# the body's own early-return).
echo 'Operator-only: rotate the leaked production credential' > "$STUB_DIR/title-96.txt"
out="$(run_warn "96")"
assert_contains "$out" $'96\t⚠ title declares operator-gating: "Operator-only"' \
    "#96 (title-prefixed, empty body) still flagged by the title signal"

# =====================================================================
echo
echo "--- No matching phrase and no operator-only dependency -> silent ---"

echo 'This issue requires 100-200 ground-truth labels on handwriting crops.' > "$STUB_DIR/body-64.txt"
out="$(run_warn "64")"
assert_eq "" "$out" "#64 (no phrase, no dependency) produces no annotation"

# =====================================================================
echo
echo "--- Zero matches across the whole candidate set -> zero lines of output ---"

echo 'Nothing operator-related here at all.' > "$STUB_DIR/body-1.txt"
echo 'Fix the flaky retry loop in the worker'  > "$STUB_DIR/title-1.txt"
echo 'Just an ordinary bug fix.'             > "$STUB_DIR/body-2.txt"
echo 'Improve error message for missing config' > "$STUB_DIR/title-2.txt"
out="$(run_warn "1 2")"
assert_eq "" "$out" "clean candidate set (ordinary bodies + titles) produces byte-for-byte empty output"

# =====================================================================
echo
echo "--- A candidate matching BOTH signals prints two lines ---"

echo 'This is operator-gated. Also Depends on #4.' > "$STUB_DIR/body-90.txt"
echo 'loom:operator-only' > "$STUB_DIR/labels-4.txt"
out="$(run_warn "90")"
assert_contains "$out" $'90\t⚠ body declares operator-gating: "operator-gated"' \
    "#90 phrase signal present"
assert_contains "$out" $'90\t⚠ depends on #4, which is loom:operator-only' \
    "#90 dependency signal also present"

# =====================================================================
echo
echo "--- A dependency carrying BOTH labels prints one line per label (#5817) ---"

echo 'Depends on #6' > "$STUB_DIR/body-93.txt"
echo 'loom:operator-only,loom:needs-capability' > "$STUB_DIR/labels-6.txt"
out="$(run_warn "93")"
assert_contains "$out" $'93\t⚠ depends on #6, which is loom:operator-only' \
    "#93 flagged for the loom:operator-only label on #6"
assert_contains "$out" $'93\t⚠ depends on #6, which is loom:needs-capability' \
    "#93 flagged for the loom:needs-capability label on #6"

# =====================================================================
echo
echo "--- Non-blocking: direct invocation always exits 0 ---"

echo 'operator-gated' > "$STUB_DIR/body-91.txt"
LOOM_TEST_STUB_DIR="$STUB_DIR" PATH="$STUB_DIR:$PATH" \
    bash "$SRC" --candidates "91" >/dev/null 2>&1
assert_eq "0" "$?" "exits 0 even when a match is emitted (advisory, non-blocking)"

bash "$SRC" >/dev/null 2>&1
assert_eq "1" "$?" "missing --candidates is a usage error (exit 1)"

# =====================================================================
echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
