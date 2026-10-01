#!/usr/bin/env bash
# test-create-pr-superseded-issue.sh - Unit tests for create-pr.sh's
# pre-push superseded-target-issue freshness check (#6277).
#
# Two workers racing on the same issue is not caught today until Judge
# review. create-pr.sh now parses the `Closes #N` / `Fixes #N` / `Resolves
# #N` closing-keyword reference out of the PR body and re-verifies the
# target issue's freshness immediately before opening a brand-new PR: if the
# issue is already CLOSED by a different, already-merged PR, it refuses to
# open a duplicate (clear message, non-zero exit, no push, no branch
# deletion). `Part of #N` / `Contributes to #N` partial-increment references
# never match the closing-keyword pattern, so they are exempt by
# construction.
#
# Strategy: run create-pr.sh directly as a subprocess (like
# test-merge-pr-help.sh) with a stub `gh` on PATH and LOOM_FORGE_TYPE=github
# forced (so forge_detect never touches git/network). --head is always
# passed explicitly so the script never needs a real git checkout.
#
# Usage:
#   ./.loom/scripts/tests/test-create-pr-superseded-issue.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/write-scope-fixture.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/write-scope-fixture.sh"
CREATE_PR="$(cd "$SCRIPT_DIR/.." && pwd)/create-pr.sh"

# Colors
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
    echo "    Looking for: '$needle'"
    echo "    In output:   '$haystack'"
  fi
}

if [[ ! -x "$CREATE_PR" ]]; then
  echo "ERROR: $CREATE_PR is not executable" >&2
  exit 1
fi

# --- Stub gh on PATH ---
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB_DIR"' EXIT

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-create-pr-superseded-issue.sh.
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
echo "$*" >> "$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "pr" && "$2" == "list" ]]; then
  # Adopt-first lookup: no existing open PR by default.
  cat "$STUB_DIR_FROM_ENV/adopt-url.txt" 2>/dev/null || true
  exit 0
fi

if [[ "$1" == "issue" && "$2" == "view" ]]; then
  issue_num="$3"
  jq_expr=""
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[i]}" == "--jq" ]]; then
      jq_expr="${args[i + 1]}"
    fi
  done
  case "$jq_expr" in
    *".state"*)
      cat "$STUB_DIR_FROM_ENV/issue-$issue_num-state.txt" 2>/dev/null || true
      ;;
    *"number"*)
      cat "$STUB_DIR_FROM_ENV/issue-$issue_num-number.txt" 2>/dev/null || true
      ;;
    *"url"*)
      cat "$STUB_DIR_FROM_ENV/issue-$issue_num-url.txt" 2>/dev/null || true
      ;;
  esac
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "view" ]]; then
  pr_num="$3"
  cat "$STUB_DIR_FROM_ENV/pr-$pr_num-head.txt" 2>/dev/null || true
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "create" ]]; then
  echo "CREATED" >> "$STUB_DIR_FROM_ENV/created.log"
  echo "https://github.com/owner/repo/pull/9999"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
export LOOM_FORGE_TYPE=github

# This suite tests the supersede check in isolation -- it must stay hermetic
# regardless of this repo's own ambient version-file sync state (#6730's own
# create-pr.sh version check, tested separately in
# test-create-pr-version-check.sh, would otherwise run for real here since
# these tests execute from this checkout's real worktree root). Point it at
# an always-clean stub so it never fires.
cat > "$STUB_DIR/version-check-ok.sh" <<'STUB'
#!/usr/bin/env bash
echo "OK        (stub): all versions in sync"
exit 0
STUB
chmod +x "$STUB_DIR/version-check-ok.sh"
export LOOM_VERSION_CHECK_SCRIPT="$STUB_DIR/version-check-ok.sh"

# #9548: create-pr.sh vets its write target through the write scope before it
# writes. It runs from a checkout registered as owner/repo (origin, .loom/, push
# reported to the permission probe), so the real decision admits it.
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout"

reset_fixtures() {
  : > "$STUB_DIR/gh-calls.log"
  : > "$STUB_DIR/created.log"
  rm -f "$STUB_DIR"/issue-*.txt "$STUB_DIR"/pr-*.txt "$STUB_DIR/adopt-url.txt"
}

run_create_pr() {
  set +e
  OUTPUT=$("$CREATE_PR" "$@" 2>&1)
  EXIT_CODE=$?
  set -e
}

created_count() {
  if [[ -f "$STUB_DIR/created.log" ]]; then
    grep -c "CREATED" "$STUB_DIR/created.log" 2>/dev/null || true
  else
    echo 0
  fi
}

echo "Testing create-pr.sh superseded-target-issue freshness check (#6277)..."
echo ""

# T1: target issue already CLOSED by a DIFFERENT, already-merged PR -> refuse,
# non-zero exit, clear message naming the superseding PR, no PR created.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-100-state.txt"
echo "555" > "$STUB_DIR/issue-100-number.txt"
echo "https://github.com/owner/repo/pull/555" > "$STUB_DIR/issue-100-url.txt"
echo "feature/issue-999" > "$STUB_DIR/pr-555-head.txt"
run_create_pr --title "fix: something" --body "Fixes the thing.

Closes #100" --head "feature/issue-100"
assert_eq "1" "$EXIT_CODE" "Closes #100 (already CLOSED by #555) -> non-zero exit"
assert_contains "$OUTPUT" "#100" "Error message names the target issue"
assert_contains "$OUTPUT" "#555" "Error message names the superseding PR"
assert_contains "$OUTPUT" "already CLOSED" "Error message states the issue is already closed"
assert_contains "$OUTPUT" "do NOT" "Error message advises against pushing further / deleting the branch"
assert_eq "0" "$(created_count)" "No PR was created"

# T2: target issue is still OPEN -> proceeds to create.
reset_fixtures
echo "OPEN" > "$STUB_DIR/issue-101-state.txt"
run_create_pr --title "fix: something" --body "Fixes the thing.

Closes #101" --head "feature/issue-101"
assert_eq "0" "$EXIT_CODE" "Closes #101 (still OPEN) -> exits 0"
assert_eq "1" "$(created_count)" "PR IS created when the target issue is still open"

# T3: `Part of #N` (partial increment) is EXEMPT even when #N is CLOSED --
# the freshness check must never fire for a non-closing reference.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-102-state.txt"
echo "666" > "$STUB_DIR/issue-102-number.txt"
run_create_pr --title "feat: slice" --body "Implements a slice.

Part of #102" --head "feature/issue-102"
assert_eq "0" "$EXIT_CODE" "Part of #102 (closed) -> exempt, exits 0"
assert_eq "1" "$(created_count)" "Part of #102 -> PR is still created (no supersede check applied)"

# T4: `Contributes to #N` is also exempt.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-103-state.txt"
run_create_pr --title "feat: slice" --body "Contributes to #103" --head "feature/issue-103"
assert_eq "0" "$EXIT_CODE" "Contributes to #103 (closed) -> exempt, exits 0"
assert_eq "1" "$(created_count)" "Contributes to #103 -> PR is still created"

# T5: closing keyword AND a partial-increment reference to a DIFFERENT issue
# in the same body -> only the closing-keyword issue is checked.
reset_fixtures
echo "OPEN" > "$STUB_DIR/issue-104-state.txt"
run_create_pr --title "fix: something" --body "Closes #104

Part of #200" --head "feature/issue-104"
assert_eq "0" "$EXIT_CODE" "Closes #104 (open) + Part of #200 -> exits 0"
assert_eq "1" "$(created_count)" "Mixed body -> PR is created (closing target is open)"

# T6: re-running create-pr.sh on the SAME branch that already merged and
# closed the issue (idempotent re-run) is NOT treated as a supersede -- the
# superseding PR's head branch matches our own --head.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-105-state.txt"
echo "777" > "$STUB_DIR/issue-105-number.txt"
echo "feature/issue-105" > "$STUB_DIR/pr-777-head.txt"
run_create_pr --title "fix: something" --body "Closes #105" --head "feature/issue-105"
assert_eq "0" "$EXIT_CODE" "Own branch already closed the issue -> not a supersede, exits 0"
assert_eq "1" "$(created_count)" "Own-branch case -> PR path is still taken (not refused)"

# T7: `Fixes #N` and `Resolves #N` are recognized closing keywords too.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-106-state.txt"
echo "888" > "$STUB_DIR/issue-106-number.txt"
echo "feature/issue-999" > "$STUB_DIR/pr-888-head.txt"
run_create_pr --title "fix: something" --body "Fixes #106" --head "feature/issue-106"
assert_eq "1" "$EXIT_CODE" "Fixes #106 (closed) -> refused"

reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-107-state.txt"
echo "888" > "$STUB_DIR/issue-107-number.txt"
echo "feature/issue-999" > "$STUB_DIR/pr-888-head.txt"
run_create_pr --title "fix: something" --body "Resolves #107" --head "feature/issue-107"
assert_eq "1" "$EXIT_CODE" "Resolves #107 (closed) -> refused"

# T8: a body with no closing keyword at all (e.g. docs-only, no issue
# reference) is not checked -- the check only fires when a target is parsed.
reset_fixtures
run_create_pr --title "docs: update readme" --body "Just a docs tweak, no issue reference." --head "feature/misc"
assert_eq "0" "$EXIT_CODE" "No closing keyword in body -> no supersede check, exits 0"
assert_eq "1" "$(created_count)" "No closing keyword -> PR is still created"

# T9: `gh issue view` lookup failure (simulated by leaving the state fixture
# missing, so the stub returns empty) is fail-open, never fatal.
reset_fixtures
run_create_pr --title "fix: something" --body "Closes #108" --head "feature/issue-108"
assert_eq "0" "$EXIT_CODE" "Lookup failure (empty state) -> fails open, exits 0"
assert_eq "1" "$(created_count)" "Lookup failure -> PR is still created (fail-open)"

# T10: 'close issue #N' (a word between the keyword and the reference) is NOT
# a closing reference -- same word-adjacency rule GitHub itself enforces and
# merge-pr.sh's _body_closing_refs already relies on.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-109-state.txt"
echo "888" > "$STUB_DIR/issue-109-number.txt"
run_create_pr --title "fix: something" --body "close issue #109" --head "feature/issue-109"
assert_eq "0" "$EXIT_CODE" "'close issue #109' is not adjacency-matched -> no supersede check, exits 0"
assert_eq "1" "$(created_count)" "'close issue #109' -> PR is still created"

# T11: target issue CLOSED but with NO linked PR (e.g. closed manually / not
# planned, not by a merge) -- still refused, just without a superseding PR
# number/URL to name.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-110-state.txt"
run_create_pr --title "fix: something" --body "Closes #110" --head "feature/issue-110"
assert_eq "1" "$EXIT_CODE" "Closes #110 (closed, no linked PR) -> still refused"
assert_contains "$OUTPUT" "already CLOSED" "No-linked-PR case still explains the issue is closed"
assert_eq "0" "$(created_count)" "No-linked-PR case -> no PR created"

# T12: 'Closing #N' (the progressive form GitHub also honors) is recognized.
reset_fixtures
echo "CLOSED" > "$STUB_DIR/issue-111-state.txt"
echo "888" > "$STUB_DIR/issue-111-number.txt"
echo "feature/issue-999" > "$STUB_DIR/pr-888-head.txt"
run_create_pr --title "fix: something" --body "Closing #111" --head "feature/issue-111"
assert_eq "1" "$EXIT_CODE" "Closing #111 (closed) -> refused"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
