#!/usr/bin/env bash
# test-docs-worktree.sh - Regression test for issue #5413
#
# The Guide role's Document Maintenance phase went silent for ~5.5 months. One
# cause was config (#5392/#5407: `guide` missing from roleRunner.roles). The
# other, found while closing #5413, is structural and is what this suite locks
# down: the role runner starts every role in the MAIN CHECKOUT
# (loom-daemon/src/role_runner.rs -> cmd.current_dir(workspace_root)), and both
# worktree-isolation guards deny writes there — so the one role phase that
# writes repository files could never write them.
#
# Verifies that:
#   1. docs-worktree.sh creates a managed worktree slot at
#      <worktree-root>/docs-guide/ with the .loom-managed sentinel, on a fresh
#      docs/guide-update-* branch off origin/<default-branch>.
#   2. stdout is exactly the worktree path (machine-readable contract) and all
#      chatter goes to stderr.
#   3. Re-running reuses the same slot and discards leftover scratch state, so
#      docs worktrees never accumulate.
#   4. THE REGRESSION: guard-worktree-paths.sh DENIES a write to the main
#      checkout's WORK_LOG.md but ALLOWS the same write inside the docs
#      worktree. This is the exact reason the phase must not write in place.
#   5. Argument validation rejects junk with exit 2.
#   6. guide.md's Document Maintenance phase actually routes through the helper
#      and no longer carries the two defects #5413 fixed (main-checkout
#      `git checkout -b`, and the hash comparison that could never match).
#
# Hermetic: builds a throwaway git repo under mktemp -d, no forge/network calls.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"

DOCS_WORKTREE_SH="$SCRIPTS_DIR/docs-worktree.sh"
GUARD_SH="$REPO_ROOT/defaults/hooks/guard-worktree-paths.sh"
GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
WORK_PLAN_MD="$REPO_ROOT/WORK_PLAN.md"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (missing pattern: $pattern)"; fi
}

assert_no_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then fail "$msg (unexpected pattern: $pattern)"; else pass "$msg"; fi
}

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not available"
    exit 0
fi

# Canonicalize once, up front: on macOS `/var` is a symlink to `/private/var`,
# and `git worktree list --porcelain` (which docs-worktree.sh matches against)
# always reports the resolved path. Without this, every expected-path
# comparison below would compare a `/var/...` string against the script's
# canonical `/private/var/...` output and spuriously fail.
SANDBOX="$(cd "$(mktemp -d)" && pwd -P)"
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Sandbox repo: a bare "origin" plus a clone that stands in for the main
# checkout the role runner would drop the Guide into.
# ---------------------------------------------------------------------------
SANDBOX_ORIGIN="$SANDBOX/origin.git"
SANDBOX_REPO="$SANDBOX/repo"
git init --quiet --bare "$SANDBOX_ORIGIN"
git clone --quiet "$SANDBOX_ORIGIN" "$SANDBOX_REPO" 2>/dev/null
git -C "$SANDBOX_REPO" config user.email "test@loom.local"
git -C "$SANDBOX_REPO" config user.name "Loom Test"
mkdir -p "$SANDBOX_REPO/.loom"
printf '# Work Log\n' > "$SANDBOX_REPO/WORK_LOG.md"
printf '# Work Plan\n' > "$SANDBOX_REPO/WORK_PLAN.md"
git -C "$SANDBOX_REPO" add -A
git -C "$SANDBOX_REPO" commit --quiet -m "init"
git -C "$SANDBOX_REPO" branch -M main
git -C "$SANDBOX_REPO" push --quiet -u origin main
git -C "$SANDBOX_REPO" remote set-head origin main

DOCS_WT_EXPECTED="$SANDBOX_REPO/.loom/worktrees/docs-guide"

# --- Test 1: creation ------------------------------------------------------
echo "Test 1: docs-worktree.sh creates the managed docs worktree"
if [[ -x "$DOCS_WORKTREE_SH" ]]; then
    pass "docs-worktree.sh exists and is executable"
else
    fail "docs-worktree.sh missing or not executable at $DOCS_WORKTREE_SH"
fi

STDOUT_1="$(cd "$SANDBOX_REPO" && "$DOCS_WORKTREE_SH" 2>"$SANDBOX/stderr1")"
if [[ "$STDOUT_1" == "$DOCS_WT_EXPECTED" ]]; then
    pass "stdout is exactly the worktree path (machine-readable contract)"
else
    fail "stdout was '$STDOUT_1', expected '$DOCS_WT_EXPECTED'"
fi

if [[ -f "$DOCS_WT_EXPECTED/.loom-managed" ]]; then
    pass ".loom-managed sentinel written (guards allow writes inside)"
else
    fail ".loom-managed sentinel missing at $DOCS_WT_EXPECTED"
fi

BRANCH_1="$(git -C "$DOCS_WT_EXPECTED" branch --show-current 2>/dev/null)"
if [[ "$BRANCH_1" =~ ^docs/guide-update-[0-9]{8}-[0-9]{6}$ ]]; then
    pass "default branch matches docs/guide-update-<UTC timestamp> ($BRANCH_1)"
else
    fail "unexpected default branch: '$BRANCH_1'"
fi

if [[ -s "$SANDBOX/stderr1" ]]; then
    pass "progress messages go to stderr, not stdout"
else
    fail "expected progress output on stderr"
fi

# The main checkout must never be switched off its branch.
MAIN_BRANCH_AFTER="$(git -C "$SANDBOX_REPO" branch --show-current)"
if [[ "$MAIN_BRANCH_AFTER" == "main" ]]; then
    pass "main checkout stays on its own branch (no git checkout -b in the primary clone)"
else
    fail "main checkout moved to '$MAIN_BRANCH_AFTER'"
fi

# --- Test 2: idempotent reuse + scratch reset -------------------------------
echo ""
echo "Test 2: re-running reuses the slot and resets leftover scratch state"
printf 'leftover\n' > "$DOCS_WT_EXPECTED/stale-scratch-file"
STDOUT_2="$(cd "$SANDBOX_REPO" && "$DOCS_WORKTREE_SH" "docs/guide-update-custom" 2>/dev/null)"
if [[ "$STDOUT_2" == "$DOCS_WT_EXPECTED" ]]; then
    pass "second run reuses the same slot (no accumulation)"
else
    fail "second run returned '$STDOUT_2'"
fi

if [[ -f "$DOCS_WT_EXPECTED/stale-scratch-file" ]]; then
    fail "leftover scratch file survived the reset"
else
    pass "leftover scratch state discarded on reuse"
fi

BRANCH_2="$(git -C "$DOCS_WT_EXPECTED" branch --show-current 2>/dev/null)"
if [[ "$BRANCH_2" == "docs/guide-update-custom" ]]; then
    pass "explicit branch-name argument honored"
else
    fail "expected branch docs/guide-update-custom, got '$BRANCH_2'"
fi

if [[ -f "$DOCS_WT_EXPECTED/.loom-managed" ]]; then
    pass ".loom-managed sentinel survives the reset"
else
    fail ".loom-managed sentinel lost on reuse"
fi

# --- Test 3: THE REGRESSION — guard verdicts --------------------------------
echo ""
echo "Test 3: worktree-isolation guard denies the main checkout, allows the docs worktree"

# The guard's contract is "exit 0 with NO output = allow", so an empty response
# is an allow, not a parse failure.
guard_decision() {
    local target="$1" out
    out="$(printf '{"tool_input":{"file_path":"%s"},"cwd":"%s"}' "$target" "$SANDBOX_REPO" \
        | (cd "$SANDBOX_REPO" && env -u LOOM_WORKTREE_PATH bash "$GUARD_SH" 2>/dev/null))"
    if [[ -z "$out" ]]; then
        printf 'allow'
    else
        printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null || printf 'allow'
    fi
}

MAIN_VERDICT="$(guard_decision "$SANDBOX_REPO/WORK_LOG.md")"
if [[ "$MAIN_VERDICT" == "deny" ]]; then
    pass "writing WORK_LOG.md in the main checkout is DENIED (why the phase needs a worktree)"
else
    fail "expected deny for the main-checkout path, got '$MAIN_VERDICT'"
fi

DOCS_VERDICT="$(guard_decision "$DOCS_WT_EXPECTED/WORK_LOG.md")"
if [[ "$DOCS_VERDICT" == "allow" ]]; then
    pass "writing WORK_LOG.md inside the docs worktree is ALLOWED"
else
    fail "expected allow for the docs-worktree path, got '$DOCS_VERDICT'"
fi

# --- Test 4: argument validation -------------------------------------------
echo ""
echo "Test 4: argument validation"
run_rc() {
    local rc=0
    (cd "$SANDBOX_REPO" && "$DOCS_WORKTREE_SH" "$@" >/dev/null 2>&1) || rc=$?
    printf '%s' "$rc"
}

RC="$(run_rc one two)"
if [[ "$RC" == "2" ]]; then pass "too many arguments -> exit 2"; else fail "expected exit 2 for extra args, got $RC"; fi

RC="$(run_rc 'bad name')"
if [[ "$RC" == "2" ]]; then pass "whitespace in branch name -> exit 2"; else fail "expected exit 2 for a bad branch name, got $RC"; fi

RC="$(run_rc --help)"
if [[ "$RC" == "0" ]]; then pass "--help -> exit 0"; else fail "expected exit 0 for --help, got $RC"; fi

# --- Test 5: guide.md wiring ------------------------------------------------
echo ""
echo "Test 5: guide.md's Document Maintenance phase routes through the helper"
assert_grep 'docs-worktree\.sh' "$GUIDE_MD" \
    "guide.md invokes docs-worktree.sh"
assert_grep 'DOCS_WT' "$GUIDE_MD" \
    "guide.md writes through \$DOCS_WT"
assert_grep '\$DOCS_WT/WORK_LOG\.md' "$GUIDE_MD" \
    "WORK_LOG.md is addressed inside the docs worktree"
assert_grep '\$DOCS_WT/WORK_PLAN\.md' "$GUIDE_MD" \
    "WORK_PLAN.md is addressed inside the docs worktree"
assert_no_grep 'git checkout -b "\$branch" main' "$GUIDE_MD" \
    "no git checkout -b in the main checkout (Step 5 uses git -C \$DOCS_WT)"

# The dead-code hash comparison (#5413): content_hash was computed over bare
# bullet lines and compared against a hash of the committed marker region
# (marker lines + headings + blurbs), so it could never be equal.
assert_no_grep 'portable_hash' "$GUIDE_MD" \
    "the incommensurable hash comparison is gone from Step 3"
assert_grep 'render_plan_body' "$GUIDE_MD" \
    "Step 3 renders the plan body once and compares it to the committed region"

# --- Test 6: WORK_PLAN.md carries the markers Step 3 diffs against ----------
echo ""
echo "Test 6: WORK_PLAN.md carries the guide:plan-body markers"
assert_grep '<!-- guide:plan-body:start -->' "$WORK_PLAN_MD" \
    "WORK_PLAN.md has the plan-body start marker"
assert_grep '<!-- guide:plan-body:end -->' "$WORK_PLAN_MD" \
    "WORK_PLAN.md has the plan-body end marker"

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
