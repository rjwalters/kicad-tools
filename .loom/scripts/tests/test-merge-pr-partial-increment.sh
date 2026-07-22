#!/usr/bin/env bash
# test-merge-pr-partial-increment.sh - Unit tests for the partial-increment
# label-reset logic in merge-pr.sh (#3667).
#
# A merged PR that references a family/epic issue with a NON-closing keyword
# (`Part of #N` / `Contributes to #N`) does not auto-close the issue, so its
# `loom:building` label is left orphaned. merge-pr.sh's
# _reset_partial_increment_labels swaps that still-open, still-loom:building
# issue back to `loom:issue` at the merge choke point (mirroring
# orphan_recovery.py's recover_issue() semantics). Closing keywords
# (`Closes`/`Fixes`/`Resolves`) are untouched (GitHub auto-closes those).
#
# Strategy: the two functions under test (_reset_one_partial_issue and
# _reset_partial_increment_labels) depend only on globals (PR_JSON, REPO_NWO,
# PR_NUMBER, FORGE_TYPE) and the `gh` CLI. We extract just those two function
# definitions from merge-pr.sh and source them, stub `gh` on PATH to serve
# canned issue JSON and record mutating calls, then assert on the recorded
# calls. Extracting from source (rather than replicating) keeps the test in
# lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-partial-increment.sh

# SC2034: several globals (PR_JSON, REPO_NWO, PR_NUMBER, FORGE_TYPE) are read
# only by the functions extracted+sourced from merge-pr.sh, which shellcheck
# cannot see — every such assignment looks "unused" to the linter.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

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

# --- Minimal logging shims the extracted functions call ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }

# --- Extract the two functions under test from merge-pr.sh and source them ---
# From `_reset_one_partial_issue() {` up to (not including) the
# `# Handle auto-merge mode` line — this span contains exactly the two function
# definitions plus intervening comments (harmless when sourced).
FUNCS_FILE="$(mktemp)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^_reset_one_partial_issue\(\) \{/ { capture=1 }
  /^# Handle auto-merge mode/        { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_reset_partial_increment_labels()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract functions from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
STUB_DIR="$(mktemp -d)"
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# Stub gh for test-merge-pr-partial-increment.sh.
#   gh api repos/OWNER/REPO/issues/N   -> cat $STUB_DIR/issue-N.json (or {})
#   gh issue edit N ...                -> record to $STUB_DIR/gh-calls.log
#   gh issue comment N ...             -> record to $STUB_DIR/gh-calls.log
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "api" ]]; then
  # Last arg is the api path: repos/owner/repo/issues/N
  path="${!#}"
  num="${path##*/}"
  canned="$STUB_DIR_FROM_ENV/issue-$num.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '{}'; fi
  exit 0
fi

if [[ "$1" == "issue" ]]; then
  # Record the full argv (issue edit/comment) for assertions.
  echo "$*" >> "$LOG"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# --- Shared globals the functions read (consumed indirectly by the sourced
# functions; see the file-level SC2034 disable at the top). ---
REPO_NWO="owner/repo"
PR_NUMBER="999"
FORGE_TYPE="github"

# Canned issue fixtures.
cat > "$STUB_DIR/issue-123.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"},{"name":"loom:epic"}]}
EOF
cat > "$STUB_DIR/issue-456.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-777.json" <<'EOF'
{"state":"closed","labels":[{"name":"loom:building"}]}
EOF
cat > "$STUB_DIR/issue-888.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-321.json" <<'EOF'
{"state":"open","pull_request":{"url":"x"},"labels":[{"name":"loom:building"}]}
EOF

reset_log() { : > "$STUB_DIR/gh-calls.log"; }
read_log()  { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }

echo "Testing _reset_partial_increment_labels behavior..."

# T1: Part of #123, open + loom:building -> swap to loom:issue + comment.
reset_log
PR_JSON='{"body":"Implements a slice.\n\nPart of #123"}'
_reset_partial_increment_labels
log="$(read_log)"
assert_contains "$log" "issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Part of #123 (open, building) -> swaps loom:building to loom:issue (repo-scoped)"
assert_contains "$log" "issue comment 123 --repo owner/repo" \
  "Part of #123 -> posts an auditable comment (repo-scoped)"

# T2: Closes #123 -> NOT a partial ref -> no mutation (existing behavior preserved).
reset_log
PR_JSON='{"body":"All done.\n\nCloses #123"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Closes #123 -> no label mutation (auto-close path untouched)"

# T2b: Fixes / Resolves also untouched.
reset_log
PR_JSON='{"body":"Fixes #123 and Resolves #456"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Fixes/Resolves -> no label mutation"

# T3: Part of #777 but issue is CLOSED -> no-op.
reset_log
PR_JSON='{"body":"Part of #777"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Part of #777 (closed) -> no-op, no mutation"

# T4: Part of #888 but issue lacks loom:building -> no-op (idempotent).
reset_log
PR_JSON='{"body":"Part of #888"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Part of #888 (not loom:building) -> no-op, idempotent"

# T5: Contributes to #456 (open, building) -> swap.
reset_log
PR_JSON='{"body":"Contributes to #456"}'
_reset_partial_increment_labels
assert_contains "$(read_log)" "issue edit 456 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Contributes to #456 (open, building) -> swaps to loom:issue (repo-scoped)"

# T6: case-insensitive keyword + multiple refs -> both swapped.
reset_log
PR_JSON='{"body":"part of #123\ncontributes TO #456"}'
_reset_partial_increment_labels
log="$(read_log)"
assert_contains "$log" "issue edit 123 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Case-insensitive 'part of #123' matched"
assert_contains "$log" "issue edit 456 --repo owner/repo --remove-label loom:building --add-label loom:issue" \
  "Multiple refs: #456 also matched"

# T7: reference target is actually a PR (has .pull_request) -> skip.
reset_log
PR_JSON='{"body":"Part of #321"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "Part of #321 (is a PR, has .pull_request) -> skipped"

# T8: FORGE_TYPE != github -> no-op (v1 is GitHub-only).
reset_log
FORGE_TYPE="gitea"
PR_JSON='{"body":"Part of #123"}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "FORGE_TYPE=gitea -> no-op (GitHub-only v1)"
FORGE_TYPE="github"

# T9: empty / null body -> no-op.
reset_log
PR_JSON='{"body":null}'
_reset_partial_increment_labels
assert_eq "" "$(read_log)" "null PR body -> no-op"

# T10: closing keyword AND a partial ref in the same body -> only the partial
# ref's issue is reset; the Closes issue is left to GitHub auto-close.
reset_log
PR_JSON='{"body":"Closes #888\n\nPart of #123"}'
_reset_partial_increment_labels
log="$(read_log)"
assert_contains "$log" "issue edit 123 --repo owner/repo --remove-label loom:building" \
  "Mixed body: Part of #123 is reset"
assert_not_contains "$log" "issue edit 888" \
  "Mixed body: Closes #888 is NOT touched"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_reset_partial_increment_labels" \
  "merge-pr.sh defines and calls _reset_partial_increment_labels"
assert_contains "$src" "(Part of|Contributes to)" \
  "merge-pr.sh matches the non-closing partial-increment keywords"
assert_contains "$src" "--remove-label \"loom:building\"" \
  "merge-pr.sh swaps loom:building"
assert_contains "$src" "--add-label \"loom:issue\"" \
  "merge-pr.sh restores loom:issue"
assert_contains "$src" "--repo \"\$REPO_NWO\"" \
  "merge-pr.sh scopes the partial-increment mutations to REPO_NWO"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
