#!/usr/bin/env bash
# test-warn-operator-gated.sh - Unit tests for warn-operator-gated.sh (#5137)
#
# The `all` sentinel's aggressive candidate taxonomy hard-skips only the hold
# labels; every other open issue is a build candidate, even one
# whose body declares operator-gating in prose without carrying a label.
# This scans each candidate's already-fetched body for (1) instruction-shaped
# operator-gate phrases and (2) a declared Depends on / Requires #A dependency
# that itself carries an operator-gate label (the registry's `operator_gate`
# property, loom:operator when no daemon answers, #11795) — annotating (never skipping,
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

# shellcheck source=lib/assert.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"


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

# Stub loom-daemon: `labels list --property operator_gate --with-retired`
# prints $LOOM_TEST_GATE_LABELS when set; the same query without the flag
# prints $LOOM_TEST_GATE_LABELS_OLD (a pre-#11795 daemon rejects the flag);
# anything else fails so the script uses its fallback.
cat > "$STUB_DIR/loom-daemon" <<'STUB'
#!/usr/bin/env bash
if [[ "$*" == "labels list --property operator_gate --with-retired" && -n "${LOOM_TEST_GATE_LABELS:-}" ]]; then
  printf '%s\n' "$LOOM_TEST_GATE_LABELS"
  exit 0
fi
if [[ "$*" == "labels list --property operator_gate" && -n "${LOOM_TEST_GATE_LABELS_OLD:-}" ]]; then
  printf '%s\n' "$LOOM_TEST_GATE_LABELS_OLD"
  exit 0
fi
exit 2
STUB
chmod +x "$STUB_DIR/loom-daemon"
# A real daemon from the caller (CI exports LOOM_DAEMON_BIN) checks the registry answer below.
LOOM_TEST_REAL_DAEMON="${LOOM_TEST_REAL_DAEMON:-${LOOM_DAEMON_BIN:-}}"
unset LOOM_DAEMON_BIN LOOM_TEST_GATE_LABELS LOOM_TEST_GATE_LABELS_OLD
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
echo "--- Signal 2: dependency on a loom:operator issue annotates the candidate (fallback set) ---"

echo 'Depends on #4 (the FamilySearch extract in the operator queue)' > "$STUB_DIR/body-88.txt"
echo 'loom:operator' > "$STUB_DIR/labels-4.txt"
out="$(run_warn "88")"
assert_contains "$out" $'88\t⚠ depends on #4, which is loom:operator' \
    "candidate depending on a loom:operator issue is flagged (no daemon answer -> fallback)"

# =====================================================================
echo
echo "--- Signal 2 (#11795): the gate set comes from the registry when the daemon answers ---"

echo 'Depends on #5 (waiting on a capability request)' > "$STUB_DIR/body-92.txt"
echo 'loom:blocked' > "$STUB_DIR/labels-5.txt"
out="$(run_warn "92")"
assert_eq "" "$out" "loom:blocked is not in the fallback gate set"
out="$(LOOM_TEST_GATE_LABELS=$'loom:operator\nloom:blocked' run_warn "92")"
assert_contains "$out" $'92\t⚠ depends on #5, which is loom:blocked' \
    "a label the registry's operator_gate property names is flagged"

# =====================================================================
echo
echo "--- Signal 2 (#11795 transition): an unmigrated loom:operator-only dependency is still flagged ---"

echo 'Depends on #6 (parked for the operator before the fold)' > "$STUB_DIR/body-94.txt"
echo 'loom:operator-only' > "$STUB_DIR/labels-6.txt"
out="$(run_warn "94")"
assert_contains "$out" $'94\t⚠ depends on #6, which is loom:operator-only' \
    "the fallback set still names the retired base label"
out="$(LOOM_TEST_GATE_LABELS=$'loom:operator\nloom:operator-only' run_warn "94")"
assert_contains "$out" $'94\t⚠ depends on #6, which is loom:operator-only' \
    "--with-retired: the registry's retired alias is flagged"
out="$(LOOM_TEST_GATE_LABELS_OLD=$'loom:operator' run_warn "94")"
assert_contains "$out" $'94\t⚠ depends on #6, which is loom:operator-only' \
    "a pre-#11795 daemon (no --with-retired) falls back to the set naming the retired base label"

# The real registry, when a built daemon is available: --with-retired must
# answer loom:operator plus its retired aliases, and only those.
REAL_DAEMON="${LOOM_TEST_REAL_DAEMON:-}"
if [[ -n "$REAL_DAEMON" && -x "$REAL_DAEMON" ]]; then
    real="$("$REAL_DAEMON" labels list --property operator_gate --with-retired 2>/dev/null | sort | tr '\n' ' ')"
    assert_eq "loom:operator loom:operator-blocked loom:operator-decision loom:operator-mechanical loom:operator-only " \
        "$real" "the registry's operator_gate set (with retired aliases) is loom:operator and its folds"
fi

# =====================================================================
echo
echo "--- Dependency on a NON-gated issue does NOT annotate ---"

echo 'Depends on #9' > "$STUB_DIR/body-89.txt"
echo 'loom:issue' > "$STUB_DIR/labels-9.txt"
out="$(run_warn "89")"
assert_eq "" "$out" "dependency without an operator-gate label produces no annotation"

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
echo "--- No matching phrase and no gated dependency -> silent ---"

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
echo 'loom:operator' > "$STUB_DIR/labels-4.txt"
out="$(run_warn "90")"
assert_contains "$out" $'90\t⚠ body declares operator-gating: "operator-gated"' \
    "#90 phrase signal present"
assert_contains "$out" $'90\t⚠ depends on #4, which is loom:operator' \
    "#90 dependency signal also present"

# =====================================================================
echo
echo "--- A dependency carrying two gate labels prints one line per label (#5817) ---"

echo 'Depends on #6' > "$STUB_DIR/body-93.txt"
echo 'loom:operator,loom:blocked' > "$STUB_DIR/labels-6.txt"
out="$(LOOM_TEST_GATE_LABELS=$'loom:operator\nloom:blocked' run_warn "93")"
assert_contains "$out" $'93\t⚠ depends on #6, which is loom:operator' \
    "#93 flagged for the loom:operator label on #6"
assert_contains "$out" $'93\t⚠ depends on #6, which is loom:blocked' \
    "#93 flagged for the loom:blocked label on #6"

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
