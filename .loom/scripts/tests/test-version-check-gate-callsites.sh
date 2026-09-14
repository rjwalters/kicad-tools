#!/usr/bin/env bash
# test-version-check-gate-callsites.sh - Structural regression guard over
# EVERY documented/scripted recipe that rebases a branch and pushes directly
# (never through create-pr.sh, so create-pr.sh's own gate at PR-creation
# time never sees the push) -- each must call the shared
# defaults/scripts/version-check-gate.sh before its `git push --force-with-lease`.
#
# Background (#7168, #7171, #7341): `create-pr.sh` gates version-bearing-file
# sync (VERSION/CLAUDE.md/Cargo.lock/.../.loom/install-metadata.json) once,
# at PR-creation time (#6730). That gate never sees a rebase performed AFTER
# a PR already exists, because those recipes push directly. #7171 closed the
# gap for Doctor's two merge-conflict rebase recipes and
# rebase-stacked-children.sh. #7341 found the SAME gap in three more call
# sites that also rebase + `git push --force-with-lease` directly:
# reconcile-stack.sh (`rebase --onto`, the stacked-PR post-merge collapse),
# three separate recipes in judge.md (Judge's own DIRTY/BEHIND/simple-
# conflict auto-rebase-and-push paths), one in builder-worktree.md
# (a Builder rebasing an already-open PR branch outside create-pr.sh), and
# doctor.md's own Step 9 pre-push head-SHA-recheck recovery (the "rebase onto
# the new head, then push --force-with-lease" rows, which #7171 missed because
# it only gated doctor.md's two merge-conflict recipes). Left
# ungated, a rebase can silently absorb origin/main's own version-bearing
# values into every file EXCEPT one the branch's own commits never touched
# (in practice .loom/install-metadata.json, since it never raises a git
# conflict) -- invisible until CI's "Installer Integration Tests" fails, or
# (per #7341's own investigation) an agent "recovers" by hand-patching the
# version-bearing files directly instead of running
# `./scripts/version.sh set <version>`, missing install-metadata.json all
# over again.
#
# This suite does NOT re-test version-check-gate.sh's own pass/fail logic
# (see test-version-check-gate.sh) or reconcile-stack.sh's specific gate
# BEHAVIOR (see test-reconcile-stack.sh Scenarios E/F) or
# rebase-stacked-children.sh's (see test-rebase-stacked-children.sh). It is a
# pure structural guard: grep every known rebase-then-push call site for the
# gate invocation, so a future refactor that silently drops one call site
# fails loudly here instead of resurfacing as a THIRD/FOURTH occurrence of
# this exact incident.
#
# Usage:
#   ./.loom/scripts/tests/test-version-check-gate-callsites.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_ge() {
  local min="$1" actual="$2" msg="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if [[ "$actual" -ge "$min" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $msg (found $actual, need >= $min)"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $msg"
    echo "    Found:    $actual"
    echo "    Required: >= $min"
  fi
}

assert_contains_file() {
  local file="$1" needle="$2" msg="$3"
  TESTS_RUN=$((TESTS_RUN + 1))
  if grep -qF -- "$needle" "$file" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $msg"
  else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $msg"
    echo "    Missing substring: '$needle'"
    echo "    In file: $file"
  fi
}

# call_site_count <file> -- counts lines that both check the gate is
# executable AND invoke it: the `[ -x .../version-check-gate.sh ] && ! ...`
# idiom every doc recipe below uses, or the equivalent script-side
# `[[ -x "$SCRIPT_DIR/version-check-gate.sh" ]]` guard. Either form counts
# once per call site regardless of how many times the literal substring
# "version-check-gate.sh" appears on that line.
call_site_count() {
  local file="$1"
  grep -cE -- '-x .*version-check-gate\.sh' "$file" 2>/dev/null || true
}

echo "Testing version-check-gate.sh call-site wiring across every direct rebase+push recipe (#7168, #7171, #7341)..."
echo ""

DOCTOR_MD="$DEFAULTS_DIR/.claude/commands/loom/doctor.md"
JUDGE_MD="$DEFAULTS_DIR/.claude/commands/loom/judge.md"
BUILDER_WORKTREE_MD="$DEFAULTS_DIR/.claude/commands/loom/builder-worktree.md"
CREATE_PR_SH="$DEFAULTS_DIR/scripts/create-pr.sh"
REBASE_STACKED_CHILDREN_SH="$DEFAULTS_DIR/scripts/rebase-stacked-children.sh"
RECONCILE_STACK_SH="$DEFAULTS_DIR/scripts/reconcile-stack.sh"

for f in "$DOCTOR_MD" "$JUDGE_MD" "$BUILDER_WORKTREE_MD" "$CREATE_PR_SH" \
         "$REBASE_STACKED_CHILDREN_SH" "$RECONCILE_STACK_SH"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: expected file not found: $f" >&2
    exit 1
  fi
done

# --- Pre-existing call sites (#7171) -- regression floor, must not shrink ---
echo "Pre-existing call sites (#7171):"
assert_ge 2 "$(call_site_count "$DOCTOR_MD")" \
  "doctor.md still gates both merge-conflict rebase recipes"
assert_ge 1 "$(call_site_count "$CREATE_PR_SH")" \
  "create-pr.sh still gates its PR-creation-time check (#6730)"
assert_ge 1 "$(call_site_count "$REBASE_STACKED_CHILDREN_SH")" \
  "rebase-stacked-children.sh still gates its safe-child rebase path"

echo ""
echo "New call sites (#7341):"
# --- New call sites this issue adds ---
assert_ge 3 "$(call_site_count "$JUDGE_MD")" \
  "judge.md gates all three of its own direct rebase+push recipes (DIRTY, BEHIND, simple-conflict)"
assert_ge 1 "$(call_site_count "$BUILDER_WORKTREE_MD")" \
  "builder-worktree.md gates its conflict-resolution rebase+push recipe"
assert_ge 1 "$(call_site_count "$RECONCILE_STACK_SH")" \
  "reconcile-stack.sh gates its rebase --onto + push recipe"
assert_ge 3 "$(call_site_count "$DOCTOR_MD")" \
  "doctor.md ALSO gates its Step 9 pre-push head-moved rebase-recovery path (the two 'rebase, then push --force-with-lease' rows), on top of #7171's two merge-conflict recipes"

echo ""
echo "Fix guidance points at the real script, not a hand-patch:"
assert_contains_file "$DEFAULTS_DIR/scripts/version-check-gate.sh" "version.sh bump patch" \
  "version-check-gate.sh's own Fix: message tells the caller to run version.sh, never to hand-edit files"
assert_contains_file "$JUDGE_MD" "never hand-patch the version-bearing files yourself" \
  "judge.md's DIRTY-rebase recipe explicitly steers away from a hand-patch recovery"
assert_contains_file "$BUILDER_WORKTREE_MD" "Never hand-patch VERSION/CLAUDE.md/etc" \
  "builder-worktree.md's rebase recipe explicitly steers away from a hand-patch recovery"
assert_contains_file "$DOCTOR_MD" "bef3e07a" \
  "doctor.md's Step 9 recovery names the concrete hand-patched-bump incident it exists to prevent (#7341)"

echo ""
echo "reconcile-stack.sh's gate is skipped under --dry-run (nothing was actually rebased):"
assert_contains_file "$RECONCILE_STACK_SH" '[[ "$DRY_RUN" != "true" ]] && [[ -x "$SCRIPT_DIR/version-check-gate.sh" ]]' \
  "reconcile-stack.sh's gate call is conditioned on DRY_RUN, mirroring rebase-stacked-children.sh"

echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
  exit 1
fi
exit 0
