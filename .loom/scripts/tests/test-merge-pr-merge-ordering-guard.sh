#!/usr/bin/env bash
# test-merge-pr-merge-ordering-guard.sh - Unit tests for the PRE-merge
# merge-ordering guard in merge-pr.sh (#3747, stacked-PR v2 item 2).
#
# Before either the auto-merge or synchronous-merge path attempts the actual
# merge API call, merge-pr.sh now runs a guard that discovers open CHILD PRs
# still targeting the PARENT branch (feature/issue-<N>) via a LIVE forge query
# (`gh pr list --base <parent> --state open`, never the daemon registry). If any
# open child PR exists the guard, by default, HARD-BLOCKS the merge (error, exit
# 1) — because the repo's delete_branch_on_merge setting would delete the parent
# branch synchronously during the merge and leave the children unable to rebase
# onto it. --allow-stacked-children bypasses the guard; --dry-run still runs it
# and reports the would-be block but never exits 1 (honors the dry-run contract).
# The guard is a no-op for non-feature/issue-N parent branches and non-GitHub
# forges, and keys purely on "does an open child PR target this branch" (NOT on
# the child issue's loom:building label — that split is item 1's concern).
#
# Strategy (mirrors test-merge-pr-auto-reconcile.sh): the function under test
# (_check_no_open_stacked_children) depends only on globals (PR_BRANCH, REPO_NWO,
# FORGE_TYPE, DRY_RUN, ALLOW_STACKED_CHILDREN) and the `gh` CLI. We extract the
# function definition from merge-pr.sh and source it, stub `gh` on PATH to serve
# canned child-PR lists, then assert on the guard's exit code + emitted message.
# Because the block path calls `error` (which `exit 1`s), the guard is invoked
# inside a command-substitution subshell so the exit does not tear down the test.
# Extracting from source (rather than replicating) keeps the test in lockstep
# with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-merge-ordering-guard.sh

# SC2034: several globals (PR_BRANCH, REPO_NWO, FORGE_TYPE, DRY_RUN,
# ALLOW_STACKED_CHILDREN) are read only by the function extracted+sourced from
# merge-pr.sh, which shellcheck cannot see — every such assignment looks
# "unused" to the linter.
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

# --- Minimal logging/error shims the extracted function calls ---
# `error` must exit non-zero to faithfully model the real script's hard block;
# the guard is always invoked in a subshell (see run_guard) so this exit only
# tears down that subshell, not the test.
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- Extract the function under test from merge-pr.sh and source it ---
# From `_check_no_open_stacked_children() {` up to (not including) the
# `# Invoke the guard before` invocation comment. Extracting from source keeps
# the test in lockstep with the script.
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^_check_no_open_stacked_children\(\) \{/ { capture=1 }
  /^# Invoke the guard before/              { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_check_no_open_stacked_children()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_no_open_stacked_children from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
#   gh pr list --base B ...  -> cat $STUB_DIR/prlist-<sanitized B>.json (or [])
# The --base value contains a '/' (feature/issue-N), so the stub sanitizes it to
# '_' before building the fixture filename; the test writes fixtures the same way.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
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
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# --- Shared globals the function reads (see the file-level SC2034 disable). ---
PR_NUMBER="999"
REPO_NWO="owner/repo"
FORGE_TYPE="github"
DRY_RUN=false
ALLOW_STACKED_CHILDREN=false

# Fixture writers (base -> sanitized filename).
write_prlist() { printf '%s\n' "$2" > "$STUB_DIR/prlist-${1//\//_}.json"; }
clear_prlist() { rm -f "$STUB_DIR/prlist-${1//\//_}.json"; }

# Run the guard in a subshell (its block path calls `error`, which exit 1's),
# capturing combined stdout+stderr in LAST_OUT and the exit code in LAST_RC.
LAST_OUT=""
LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( _check_no_open_stacked_children 2>&1 )"
    LAST_RC=$?
    set -e
}

echo "Testing _check_no_open_stacked_children behavior..."

# T1: no open children -> guard passes (rc 0), no block.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
clear_prlist "feature/issue-100"   # stub returns [] with no fixture
run_guard
assert_eq "0" "$LAST_RC" "No open children -> guard passes (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "No open children -> no block message"

# T2: one open child targeting the parent branch -> hard block (rc 1) with an
# informative message naming the child PR and the reconcile-stack.sh unblock cmd.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
run_guard
assert_eq "1" "$LAST_RC" "Open child #501 -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "Merge blocked" "Open child -> block message emitted"
assert_contains "$LAST_OUT" "#501" "Block message names the blocking child PR #501"
assert_contains "$LAST_OUT" "reconcile-stack.sh" "Block message points at the reconcile-stack.sh unblock path"
assert_contains "$LAST_OUT" "--allow-stacked-children" "Block message mentions the --allow-stacked-children override"

# T3: --allow-stacked-children with an open child present -> merge proceeds
# (rc 0); a warning is emitted but no hard block.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=true
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
run_guard
assert_eq "0" "$LAST_RC" "--allow-stacked-children + open child -> guard proceeds (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "--allow-stacked-children -> no hard block"
assert_contains "$LAST_OUT" "--allow-stacked-children set" "--allow-stacked-children -> override warning emitted"
ALLOW_STACKED_CHILDREN=false

# T4: non-feature/issue-N parent branch -> guard skipped entirely (rc 0), even
# though the stub would return an open child for that base.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="release-1"
write_prlist "release-1" '[{"number":503,"headRefName":"feature/issue-201"}]'
run_guard
assert_eq "0" "$LAST_RC" "Non-feature/issue-N parent 'release-1' -> guard skipped (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "Non-feature/issue-N parent -> no block"

# T5: --dry-run with an open child present -> reports the would-be block WITHOUT
# exiting 1 (dry-run contract preserved), and the reported message still surfaces
# the would-be block.
DRY_RUN=true; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
run_guard
assert_eq "0" "$LAST_RC" "--dry-run + open child -> guard does NOT exit 1 (dry-run contract)"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run -> reports the would-be block"
assert_contains "$LAST_OUT" "#501" "--dry-run block report names the blocking child PR #501"
DRY_RUN=false

# T6: FORGE_TYPE != github -> no-op (GitHub-only for v2 item 2).
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
FORGE_TYPE="gitea"
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
run_guard
assert_eq "0" "$LAST_RC" "FORGE_TYPE=gitea -> guard skipped (GitHub-only)"
assert_not_contains "$LAST_OUT" "Merge blocked" "FORGE_TYPE=gitea -> no block"
FORGE_TYPE="github"

# T7: multiple open children -> hard block naming each blocking child PR.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" \
  '[{"number":501,"headRefName":"feature/issue-201"},{"number":502,"headRefName":"feature/issue-202"}]'
run_guard
assert_eq "1" "$LAST_RC" "Multiple open children -> merge hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "#501" "Multi-child block names child #501"
assert_contains "$LAST_OUT" "#502" "Multi-child block names child #502"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_check_no_open_stacked_children" \
  "merge-pr.sh defines and invokes _check_no_open_stacked_children"
assert_contains "$src" 'gh pr list --repo "$REPO_NWO" --base "$PR_BRANCH" --state open' \
  "merge-pr.sh discovers children via a live forge query, not the daemon registry"
assert_contains "$src" "ALLOW_STACKED_CHILDREN" \
  "merge-pr.sh threads the --allow-stacked-children override into the guard"
assert_contains "$src" "--allow-stacked-children) ALLOW_STACKED_CHILDREN=true" \
  "merge-pr.sh parses the --allow-stacked-children flag alongside the other options"

# Assert the guard is invoked BEFORE the auto-merge path (line ordering): the
# _check_no_open_stacked_children invocation must precede `# Handle auto-merge mode`.
guard_line="$(grep -n '^_check_no_open_stacked_children$' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
automerge_line="$(grep -n '^# Handle auto-merge mode' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
if [[ -n "$guard_line" && -n "$automerge_line" && "$guard_line" -lt "$automerge_line" ]]; then
    ordered="yes"
else
    ordered="no (guard=$guard_line automerge=$automerge_line)"
fi
assert_eq "yes" "$ordered" \
  "guard is invoked before both merge paths (before '# Handle auto-merge mode')"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
