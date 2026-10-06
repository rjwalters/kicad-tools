#!/usr/bin/env bash
# test-guide-blocked-unblock-limit.sh - Regression test for issue #10553.
#
# guide.md's unblock sweep listed `loom:blocked` issues with no --limit, so
# gh's default of 30 silently skipped every issue past the first 30. Verifies:
#   1. Both loom:blocked listings pass an explicit --limit well above 30.
#   2. check_and_unblock(), run against a gh stub that honors --limit
#      (default 30) and 45 fixtures, examines all 45 issues.
#   3. A listing that hits the limit logs a truncation warning.
#
# Hermetic: `gh` is stubbed with fixture JSON; only the real `jq` is used.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"; exit 1
fi
command -v jq >/dev/null 2>&1 || { echo "SKIP: jq not available"; exit 0; }

echo "Test 1: every loom:blocked issue listing passes an explicit --limit > 30"
LISTINGS="$(grep -c 'issue list --label "loom:blocked"' "$GUIDE_MD")"
WITH_LIMIT="$(grep 'issue list --label "loom:blocked"' "$GUIDE_MD" | grep -c -- '--limit')"
if [[ "$LISTINGS" -ge 2 && "$LISTINGS" == "$WITH_LIMIT" ]]; then
    pass "all $LISTINGS loom:blocked listings pass --limit"
else
    fail "expected every loom:blocked listing to pass --limit (listings=$LISTINGS, with limit=$WITH_LIMIT)"
fi

echo ""
echo "Test 2: check_and_unblock() examines all 45 fixtures (not just 30)"
FN="$(awk '/^check_and_unblock\(\) \{/{f=1} f{print} /^\}/{if(f){exit}}' "$GUIDE_MD")"
if [[ -z "$FN" ]]; then
    fail "could not extract check_and_unblock() from guide.md"
else
    TMPROOT="$(mktemp -d)"; trap 'rm -rf "$TMPROOT"' EXIT
    GH_STUB="$TMPROOT/gh"
    cat > "$GH_STUB" <<'STUB'
#!/usr/bin/env bash
# `issue list` honors --limit (default 30) over $FIXTURE_JSON, like real gh.
set -uo pipefail
if [[ "${1:-}" == "issue" && "${2:-}" == "list" ]]; then
  shift 2; limit=30
  while [[ $# -gt 0 ]]; do
    case "$1" in --limit) limit="$2"; shift 2 ;; *) shift ;; esac
  done
  printf '%s' "$FIXTURE_JSON" | jq -c ".[0:$limit]"
  exit 0
fi
echo "stub gh: unexpected call: $*" >&2
exit 1
STUB
    chmod +x "$GH_STUB"
    # The parse_dependencies hook logs one line per issue the loop reaches.
    FIXTURE="$(jq -nc '[range(1;46) | {number: ., title: "t\(.)", body: "no deps"}]')"
    run_fn() {
      FIXTURE_JSON="$FIXTURE" GH_READ="$GH_STUB" SEEN="$TMPROOT/seen" FN_SRC="$FN" \
        bash -c '
          : > "$SEEN"
          check_and_unblock_prs() { :; }
          eval "$FN_SRC"
          # Count issues the loop reaches via a parse_dependencies hook.
          parse_dependencies() { echo x >> "$SEEN"; }
          check_and_unblock' 2>"$TMPROOT/stderr" >/dev/null
    }
    run_fn
    SEEN_N="$(wc -l < "$TMPROOT/seen")"
    if [[ "$SEEN_N" -eq 45 ]]; then pass "all 45 blocked issues were examined"
    else fail "expected 45 issues examined, got $SEEN_N (a 30-cap is in effect)"; fi
    if grep -q 'WARNING' "$TMPROOT/stderr"; then fail "unexpected truncation warning below the limit"
    else pass "no truncation warning when the listing is under the limit"; fi

    echo ""
    echo "Test 3: a listing that hits the limit logs a truncation warning"
    FIXTURE="$(jq -nc '[range(1;1001) | {number: ., title: "t", body: "x"}]')"
    run_fn
    if grep -q 'WARNING.*limit' "$TMPROOT/stderr"; then pass "truncation warning logged when count == limit"
    else fail "expected truncation warning on stderr (got: $(cat "$TMPROOT/stderr"))"; fi
fi

echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"; exit 1; fi
echo "All tests passed"
exit 0
