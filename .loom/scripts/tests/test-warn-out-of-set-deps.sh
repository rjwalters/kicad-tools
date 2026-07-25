#!/usr/bin/env bash
# test-warn-out-of-set-deps.sh - Unit tests for warn-out-of-set-deps.sh
# (#3747, stacked-PR v2 item 4: detect-and-warn for out-of-set dependency refs).
#
# During /loom:sweep candidate-set resolution (Modes A/B), each candidate's body
# is scanned for dependency references (Depends on / Requires / Part of #A). When
# #A is OPEN and NOT in the resolved candidate set and NOT covered by an operator
# --depends-on, an ADVISORY warning is emitted (non-blocking, stderr/log). The
# candidate set is NEVER auto-expanded — detection + advisory only.
#
# Strategy (mirrors test-rebase-stacked-children.sh): warn-out-of-set-deps.sh
# gates its main block on `BASH_SOURCE == $0`, so we `source` it directly (main
# does not run) to get parse_out_of_set_deps / _in_set / _warn_* in scope, then
# stub `gh issue view` on PATH to serve canned body/state and assert on the
# warnings captured from stderr. Sourcing (rather than replicating) keeps the
# test in lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-warn-out-of-set-deps.sh

# SC2034: CANDIDATES / DEPENDS_ON_SET / REPO_NWO / WARN_COUNT are read only by
# the functions sourced from warn-out-of-set-deps.sh, invisible to the linter.
# shellcheck disable=SC2034

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SRC="$SCRIPTS_DIR/warn-out-of-set-deps.sh"

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
    if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
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
    echo -e "${RED}FATAL${NC}: warn-out-of-set-deps.sh not found at $SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$SRC"

if ! declare -f _warn_out_of_set_deps >/dev/null; then
    echo -e "${RED}FATAL${NC}: could not source _warn_out_of_set_deps from $SRC" >&2
    exit 2
fi

# --- Stub gh on PATH ---
#   gh issue view N --json body  -q .body   -> cat $STUB_DIR/body-N.txt   (or "")
#   gh issue view N --json state -q .state  -> cat $STUB_DIR/state-N.txt  (or UNKNOWN)
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
# Expect: gh issue view <N> [--repo X] --json <field> -q .<field>
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
    state)
      f="$STUB_DIR_FROM_ENV/state-$num.txt"
      if [[ -f "$f" ]]; then cat "$f"; else echo "UNKNOWN"; fi
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

# Helper: run _warn_out_of_set_deps with given globals, capture stderr.
run_warn() {
    CANDIDATES="$1"
    DEPENDS_ON_SET="$2"
    REPO_NWO=""
    WARN_COUNT=0
    _warn_out_of_set_deps 2>&1
}

# =====================================================================
echo "--- Parser: reuses guide.md vocabulary, restricted to the 3 phrases ---"

out="$(parse_out_of_set_deps 'Depends on #101
Requires #102
Part of #103')"
assert_eq $'101\n102\n103' "$out" "parses Depends on / Requires / Part of"

out="$(parse_out_of_set_deps 'Blocked by #200')"
assert_eq "" "$out" "Blocked by is NOT parsed as a stacking/dep reference"

out="$(parse_out_of_set_deps 'Depends on #101 and also Depends on #101')"
assert_eq "101" "$out" "duplicate references are deduplicated"

# =====================================================================
echo
echo "--- Out-of-set OPEN reference produces a warning ---"

echo 'Depends on #999' > "$STUB_DIR/body-124.txt"
echo 'OPEN'            > "$STUB_DIR/state-999.txt"
warnings="$(run_warn "124 125" "")"
assert_contains "$warnings" 'warning: issue #124 declares "Depends on #999"' \
    "out-of-set open #999 warns"
assert_contains "$warnings" 'pass --depends-on 999 or include #999' \
    "warning carries the actionable remediation hint"

# =====================================================================
echo
echo "--- In-set reference produces NO warning ---"

echo 'Depends on #125' > "$STUB_DIR/body-124.txt"
echo 'OPEN'            > "$STUB_DIR/state-125.txt"
warnings="$(run_warn "124 125" "")"
assert_not_contains "$warnings" 'warning: issue #124' \
    "in-set #125 does NOT warn (auto-stack's domain)"

# =====================================================================
echo
echo "--- --depends-on-covered reference produces NO warning ---"

echo 'Depends on #300' > "$STUB_DIR/body-124.txt"
echo 'OPEN'            > "$STUB_DIR/state-300.txt"
warnings="$(run_warn "124" "300")"
assert_not_contains "$warnings" 'warning: issue #124' \
    "operator --depends-on 300 suppresses the warning"

# =====================================================================
echo
echo "--- Out-of-set CLOSED reference produces NO warning ---"

echo 'Depends on #400' > "$STUB_DIR/body-124.txt"
echo 'CLOSED'          > "$STUB_DIR/state-400.txt"
warnings="$(run_warn "124" "")"
assert_not_contains "$warnings" 'warning: issue #124' \
    "closed out-of-set #400 does NOT warn (nothing stale to build on)"

# =====================================================================
echo
echo "--- Dedup: one warning per (candidate, dependency) pair ---"

printf 'Depends on #999\nsome text\nRequires #999\n' > "$STUB_DIR/body-124.txt"
echo 'OPEN' > "$STUB_DIR/state-999.txt"
warnings="$(run_warn "124" "")"
count="$(printf '%s\n' "$warnings" | grep -c 'warning: issue #124 declares "Depends on #999"')"
assert_eq "1" "$count" "repeated reference to #999 warns exactly once"

# =====================================================================
echo
echo "--- Self-reference is never a dependency ---"

echo 'Part of #124' > "$STUB_DIR/body-124.txt"
warnings="$(run_warn "124" "")"
assert_not_contains "$warnings" 'warning: issue #124' \
    "a body naming its own number does not warn"

# =====================================================================
echo
echo "--- Multiple candidates, mixed in/out-of-set ---"

echo 'Depends on #126'                         > "$STUB_DIR/body-124.txt"  # in-set -> no warn
printf 'Requires #500\nPart of #501\n'         > "$STUB_DIR/body-126.txt"  # both out-of-set open
echo 'OPEN' > "$STUB_DIR/state-500.txt"
echo 'OPEN' > "$STUB_DIR/state-501.txt"
warnings="$(run_warn "124 126" "")"
assert_not_contains "$warnings" 'warning: issue #124' "in-set edge (#124->#126) stays silent"
assert_contains "$warnings" 'warning: issue #126 declares "Depends on #500"' "#126 out-of-set #500 warns"
assert_contains "$warnings" 'warning: issue #126 declares "Depends on #501"' "#126 out-of-set #501 warns"

# =====================================================================
echo
echo "--- Non-blocking: direct invocation always exits 0 ---"

echo 'Depends on #999' > "$STUB_DIR/body-124.txt"
echo 'OPEN'            > "$STUB_DIR/state-999.txt"
LOOM_TEST_STUB_DIR="$STUB_DIR" PATH="$STUB_DIR:$PATH" \
    bash "$SRC" --candidates "124" >/dev/null 2>&1
assert_eq "0" "$?" "exits 0 even when a warning is emitted (advisory, non-blocking)"

bash "$SRC" >/dev/null 2>&1
assert_eq "1" "$?" "missing --candidates is a usage error (exit 1)"

# =====================================================================
echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
