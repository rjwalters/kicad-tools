#!/usr/bin/env bash
# test-merge-pr-auto-reconcile.sh - Unit tests for the automated stacked-PR
# reconciliation trigger in merge-pr.sh (#3747, stacked-PR v2 item 1).
#
# When a stacked PARENT PR (branch feature/issue-<N>) squash-merges, merge-pr.sh
# now discovers open CHILD PRs based on the parent branch via a LIVE forge query
# (`gh pr list --base <parent>`, never the daemon registry) and, per child:
#   - Safe   (child issue NOT loom:building): invokes reconcile-stack.sh.
#   - Unsafe (child issue still loom:building): skips the rebase and posts a
#     deferred-reconciliation comment on the child PR instead.
# The whole step is best-effort and must never fail the parent merge, and it is
# a no-op for non-feature/issue-N parent branches and non-GitHub forges.
#
# Strategy (mirrors test-merge-pr-partial-increment.sh): the functions under
# test (_auto_reconcile_stacked_children and _reconcile_one_stacked_child) depend
# only on globals (PR_BRANCH, REPO_NWO, FORGE_TYPE, SCRIPT_DIR), the `gh` CLI, and
# an invocation of $SCRIPT_DIR/reconcile-stack.sh. We extract the function
# definitions from merge-pr.sh and source them, stub `gh` on PATH to serve canned
# issue JSON / child-PR lists and record mutating calls, point SCRIPT_DIR at a
# stub reconcile-stack.sh that records its args, then assert on the recorded
# calls. Extracting from source (rather than replicating) keeps the test in
# lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-auto-reconcile.sh

# SC2034: several globals (PR_BRANCH, REPO_NWO, FORGE_TYPE, SCRIPT_DIR) are read
# only by the functions extracted+sourced from merge-pr.sh, which shellcheck
# cannot see — every such assignment looks "unused" to the linter.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
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
    # Here-string, not a pipe, so grep -q exiting early on a match cannot
    # SIGPIPE printf and (under set -o pipefail) flip the pipeline non-zero
    # despite a match — a size-sensitive flake on large haystacks (#3820).
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

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! grep -qF -- "$needle" <<<"$haystack"; then
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

# --- Extract the functions under test from merge-pr.sh and source them ---
# From `_reset_one_partial_issue() {` up to (not including) the
# `# Handle auto-merge mode` line — this span contains the partial-increment
# functions AND the stacked-reconcile functions under test (plus intervening
# comments, harmless when sourced). Extracting from source keeps the test in
# lockstep with the script.
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d)"
RECON_DIR="$(mktemp -d)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" "$RECON_DIR" 2>/dev/null || true' EXIT
awk '
  /^_reset_one_partial_issue\(\) \{/ { capture=1 }
  /^# Handle auto-merge mode/        { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_auto_reconcile_stacked_children()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _auto_reconcile_stacked_children from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub reconcile-stack.sh (records its argv) ---
# SCRIPT_DIR (read by the sourced functions) points here so the safe-case
# invocation `$SCRIPT_DIR/reconcile-stack.sh <child-pr> <parent-branch>` hits the
# stub. LOOM_TEST_RECON_EXIT lets a test force a non-zero exit (conflict path).
SCRIPT_DIR="$RECON_DIR"
cat > "$RECON_DIR/reconcile-stack.sh" <<'RECON'
#!/usr/bin/env bash
LOG="${LOOM_TEST_RECON_LOG:?stub reconcile-stack: LOOM_TEST_RECON_LOG not set}"
echo "reconcile-stack.sh $*" >> "$LOG"
exit "${LOOM_TEST_RECON_EXIT:-0}"
RECON
chmod +x "$RECON_DIR/reconcile-stack.sh"
export LOOM_TEST_RECON_LOG="$RECON_DIR/recon-calls.log"

# --- Stub gh on PATH ---
#   gh api repos/OWNER/REPO/issues/N   -> cat $STUB_DIR/issue-N.json (or {})
#   gh pr list --base B ...            -> cat $STUB_DIR/prlist-<sanitized B>.json (or [])
#   gh pr comment N ...                -> record to $STUB_DIR/gh-calls.log
# The --base value contains a '/' (feature/issue-N), so the stub sanitizes it to
# '_' before building the fixture filename; the test writes fixtures the same way.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
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

if [[ "$1" == "pr" && "$2" == "list" ]]; then
  base=""
  shift 2
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--base" ]]; then base="$2"; shift 2; continue; fi
    shift
  done
  safe="${base//\//_}"
  canned="$STUB_DIR_FROM_ENV/prlist-$safe.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '[]'; fi
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  echo "$*" >> "$LOG"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# --- Shared globals the functions read (see the file-level SC2034 disable). ---
REPO_NWO="owner/repo"
FORGE_TYPE="github"

# Canned issue fixtures (child issue label state).
cat > "$STUB_DIR/issue-201.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-202.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# Fixture writers (base -> sanitized filename).
write_prlist() { printf '%s\n' "$2" > "$STUB_DIR/prlist-${1//\//_}.json"; }
clear_prlist() { rm -f "$STUB_DIR/prlist-${1//\//_}.json"; }

reset_logs() {
    : > "$STUB_DIR/gh-calls.log"
    : > "$RECON_DIR/recon-calls.log"
    unset LOOM_TEST_RECON_EXIT
}
read_gh_log() { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }
read_recon()  { cat "$RECON_DIR/recon-calls.log" 2>/dev/null || true; }

echo "Testing _auto_reconcile_stacked_children behavior..."

# T1: no open children -> no-op (no reconcile, no comment).
reset_logs
PR_BRANCH="feature/issue-100"
clear_prlist "feature/issue-100"   # stub returns [] with no fixture
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "No open children -> reconcile-stack.sh NOT invoked"
assert_eq "" "$(read_gh_log)" "No open children -> no comment posted"

# T2: safe child (issue not loom:building) -> reconcile-stack.sh invoked with the
# child PR number and the parent branch; no comment posted.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "Safe child #501 (issue #201 not building) -> reconcile-stack.sh 501 feature/issue-100"
assert_eq "" "$(read_gh_log)" "Safe child -> no deferred-reconciliation comment"

# T3: unsafe child (issue still loom:building) -> comment posted on the child PR;
# reconcile-stack.sh NOT invoked.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":502,"headRefName":"feature/issue-202"}]'
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "Unsafe child #502 (issue #202 building) -> reconcile-stack.sh NOT invoked"
assert_contains "$(read_gh_log)" "pr comment 502 --repo owner/repo" \
  "Unsafe child -> deferred-reconciliation comment posted on PR #502"

# T4: non-feature/issue-N parent branch -> step skipped entirely (no discovery).
reset_logs
PR_BRANCH="release-1"
write_prlist "release-1" '[{"number":503,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "Non-feature/issue-N parent 'release-1' -> reconcile skipped"
assert_eq "" "$(read_gh_log)" "Non-feature/issue-N parent -> no comment, no discovery"

# T5: FORGE_TYPE != github -> no-op (GitHub-only for v2 item 1).
reset_logs
FORGE_TYPE="gitea"
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "FORGE_TYPE=gitea -> reconcile skipped (GitHub-only)"
assert_eq "" "$(read_gh_log)" "FORGE_TYPE=gitea -> no comment"
FORGE_TYPE="github"

# T6: reconcile-stack.sh failure (rebase conflict) is swallowed — the function
# still returns 0 (best-effort, never fails the parent merge).
reset_logs
export LOOM_TEST_RECON_EXIT=2
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
rc=0
_auto_reconcile_stacked_children || rc=$?
assert_eq "0" "$rc" "reconcile-stack.sh failure -> function still returns 0 (best-effort)"
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "Failing reconcile still attempted the safe child"
unset LOOM_TEST_RECON_EXIT

# T7: multiple children, mixed safe/unsafe -> each handled independently.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" \
  '[{"number":501,"headRefName":"feature/issue-201"},{"number":502,"headRefName":"feature/issue-202"}]'
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "Mixed set: safe child #501 reconciled"
assert_not_contains "$(read_recon)" "502" \
  "Mixed set: unsafe child #502 NOT reconciled"
assert_contains "$(read_gh_log)" "pr comment 502 --repo owner/repo" \
  "Mixed set: unsafe child #502 got a deferred comment"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_auto_reconcile_stacked_children" \
  "merge-pr.sh defines and calls _auto_reconcile_stacked_children"
assert_contains "$src" "_auto_reconcile_stacked_children || true" \
  "merge-pr.sh invokes the reconcile step best-effort (|| true) at the merge choke point"
assert_contains "$src" 'gh pr list --repo "$REPO_NWO" --base "$PR_BRANCH" --state open' \
  "merge-pr.sh discovers children via a live forge query, not the daemon registry"
assert_contains "$src" "grep -qx 'loom:building'" \
  "merge-pr.sh gates safe/unsafe on the child issue's loom:building label"
assert_contains "$src" '"$SCRIPT_DIR/reconcile-stack.sh" "$child_pr" "$parent_branch"' \
  "merge-pr.sh reuses reconcile-stack.sh unmodified (no inline rebase logic)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
