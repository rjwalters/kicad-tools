#!/usr/bin/env bash
# test-clean-stale-building-labels.sh - Unit tests for
# clean-stale-building-labels.sh (#6199), the standalone/idempotent one-time
# (and periodic-on-demand) cleanup for `loom:building` claims left on issues
# that were closed OUTSIDE the merge path merge-pr.sh's own
# _strip_closed_issue_building_labels covers (see that script's header for
# the recorded scope decision).
#
# Strategy: stub `gh` on PATH so the script never touches the network. The
# stub serves a canned `gh issue list --state closed --label loom:building`
# result and records every `gh api ... -X DELETE` call.
#
# Usage:
#   ./.loom/scripts/tests/test-clean-stale-building-labels.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/write-scope-fixture.sh
source "$SCRIPT_DIR/lib/write-scope-fixture.sh"
TESTS_DIR="$SCRIPT_DIR"
HELPERS_DIR="$(cd "$TESTS_DIR/.." && pwd)"
TARGET_SCRIPT="$HELPERS_DIR/clean-stale-building-labels.sh"

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
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

[[ -x "$TARGET_SCRIPT" ]] || { echo -e "${RED}FATAL${NC}: $TARGET_SCRIPT missing or not executable"; exit 2; }

STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR" 2>/dev/null || true' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-clean-stale-building-labels.sh.
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "issue" && "$2" == "list" ]]; then
  shift 2
  jq_expr=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --jq) jq_expr="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ -n "$jq_expr" ]]; then
    jq -r "$jq_expr" "$STUB_DIR_FROM_ENV/issue-list.json"
  else
    cat "$STUB_DIR_FROM_ENV/issue-list.json"
  fi
  exit 0
fi

if [[ "$1" == "api" ]]; then
  shift
  path="" is_delete=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -X) shift; [[ "${1:-}" == "DELETE" ]] && is_delete=1; shift; continue ;;
      -*) shift; continue ;;
      *) [[ -z "$path" ]] && path="$1"; shift; continue ;;
    esac
  done
  if [[ $is_delete -eq 1 ]]; then
    echo "DELETE $path" >> "$LOG"
    if [[ "${LOOM_TEST_DELETE_FAIL_ISSUE:-}" != "" && "$path" == *"/issues/${LOOM_TEST_DELETE_FAIL_ISSUE}/"* ]]; then
      echo '{"message":"Not Found"}' >&2
      exit 1
    fi
    exit 0
  fi
  echo '{}'
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
# forge_detect() short-circuits when FORGE_TYPE is already non-empty.
export FORGE_TYPE="github"

# #9548: the script vets --repo through the write scope before it writes. It
# runs from a checkout registered as owner/repo (origin, .loom/, push reported
# to the permission probe), so the real decision admits it.
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout"

reset() {
  : > "$STUB_DIR/gh-calls.log"
  unset LOOM_TEST_DELETE_FAIL_ISSUE
}
read_log() { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }

echo "Testing clean-stale-building-labels.sh..."

# T1: --dry-run makes no mutating calls, reports the would-be count.
reset
printf '[{"number":501},{"number":502}]' > "$STUB_DIR/issue-list.json"
out="$("$TARGET_SCRIPT" --repo owner/repo --dry-run 2>&1)"
assert_eq "" "$(read_log)" "--dry-run makes zero DELETE calls"
assert_contains "$out" "Would remove loom:building from closed issue #501" \
  "--dry-run reports issue #501"
assert_contains "$out" "2 would be cleaned" \
  "--dry-run summary reports the correct count"

# T2: real run issues one DELETE per closed issue, using REST (not `gh issue
# edit`) — the bulk path deliberately avoids the GraphQL-backed mutation to
# stay off the shared quota (see the script's own header rationale).
reset
printf '[{"number":501},{"number":502}]' > "$STUB_DIR/issue-list.json"
out="$("$TARGET_SCRIPT" --repo owner/repo 2>&1)"
log="$(read_log)"
assert_contains "$log" "DELETE repos/owner/repo/issues/501/labels/loom%3Abuilding" \
  "Real run DELETEs #501's loom:building label via REST"
assert_contains "$log" "DELETE repos/owner/repo/issues/502/labels/loom%3Abuilding" \
  "Real run DELETEs #502's loom:building label via REST"
assert_contains "$out" "2 cleaned, 0 failed" \
  "Real run summary reports 2 cleaned, 0 failed"

# T3: --json emits a machine-readable summary.
reset
printf '[{"number":501}]' > "$STUB_DIR/issue-list.json"
out="$("$TARGET_SCRIPT" --repo owner/repo --dry-run --json 2>&1)"
assert_contains "$out" '"total": 1' "--json summary reports total=1"
assert_contains "$out" '"dry_run": true' "--json summary reports dry_run=true"

# T4: empty result set -> no-op, exits 0.
reset
printf '[]' > "$STUB_DIR/issue-list.json"
if out="$("$TARGET_SCRIPT" --repo owner/repo 2>&1)"; then
  rc=0
else
  rc=$?
fi
assert_eq "0" "$rc" "Empty result set exits 0"
assert_contains "$out" "nothing to do" "Empty result set reports nothing to do"

# T5: a DELETE that comes back 404 (label already gone — e.g. a concurrent
# run, or merge-pr.sh's own per-merge cleanup already got there) is treated
# as success, not a failure — idempotency (#6199 AC #3).
reset
printf '[{"number":501},{"number":502}]' > "$STUB_DIR/issue-list.json"
export LOOM_TEST_DELETE_FAIL_ISSUE="502"
out="$("$TARGET_SCRIPT" --repo owner/repo 2>&1)"
unset LOOM_TEST_DELETE_FAIL_ISSUE
assert_contains "$out" "2 cleaned, 0 failed" \
  "A 404 on an already-clean issue is treated as success (idempotent)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
