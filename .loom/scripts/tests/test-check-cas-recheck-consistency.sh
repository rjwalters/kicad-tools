#!/usr/bin/env bash
# test-check-cas-recheck-consistency.sh — regression suite for the #12136
# additions to check-cas-recheck-consistency.sh (section 5c).
#
# check-cas-recheck-consistency.sh is a prompt/doc regression guard: it greps
# judge.md (and siblings) for the invariants that have no compiler to catch a
# silent revert. #12136 added section 5c, which pins two prompt invariants:
#   - judge.md names post-verdict.sh the SOLE merge-gating verdict writer.
#   - judge.md forbids the bare `gh pr edit --add-label loom:pr` approval
#     bypass (which drops the verdict-sha marker ~1 in 4, #6319; post-#9258 a
#     markerless loom:pr HARD-STALLS instead of merging).
# If either mandate is edited back out, the fleet silently reopens the stall
# this closed. This suite is the executable tie that makes such a regression
# fail CI loudly.
#
# This is a black-box test: it runs the real check against the real repo (which
# must pass and name #12136), then against temp ROOTs whose judge.md has each
# mandate surgically removed (which must fail, naming #12136). The mutation —
# not the copy — is what trips the guard, so a passing control copy is asserted
# too. No loom-daemon, forge, or network: the guard is pure grep.
#
# Subject (check-cas-recheck-consistency.sh) is SHIPPED, but it NO-OPs without
# a defaults/ tree, which only exists in the loom SOURCE repo — so this suite
# is source-tree-only and SKIPs cleanly in an installed consumer repo.
#
# Usage:
#   ./.loom/scripts/tests/test-check-cas-recheck-consistency.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO="$(cd "$SCRIPTS_DIR/../.." && pwd)"
CHECK="$SCRIPTS_DIR/check-cas-recheck-consistency.sh"

if [[ ! -d "$REPO/defaults" ]]; then
  echo "SKIP: source-tree-only test, $REPO/defaults not found (check-cas-recheck-consistency.sh no-ops without it)" >&2
  exit 0
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

# Build a temp ROOT mirroring the real source tree (symlinks preserved, so
# defaults/roles/judge.md still resolves to its target within the copy).
FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT
cp -a "$REPO/defaults" "$FIX/defaults"
cp -a "$REPO/.github" "$FIX/.github"
FIX_JUDGE="$FIX/defaults/.claude/commands/loom/judge.md"

# --- Test (a): the real in-sync tree passes and names #12136 ----------------
echo "Test (a): the real source tree passes and 5c reports #12136"
OUT_A="$(bash "$CHECK" "$REPO" 2>&1)"; RC_A=$?
if [[ "$RC_A" -eq 0 ]]; then
  pass "real tree: exit 0"
else
  fail "real tree: expected exit 0, got $RC_A — $OUT_A"
fi
if [[ "$OUT_A" == *"#12136"* ]]; then
  pass "real tree: OK message names #12136"
else
  fail "real tree: OK message does not name #12136 — $OUT_A"
fi

# --- Test (b): control — the untouched copy also passes ---------------------
echo "Test (b): the untouched temp copy passes (proves the copy itself is clean)"
OUT_B="$(bash "$CHECK" "$FIX" 2>&1)"; RC_B=$?
if [[ "$RC_B" -eq 0 ]]; then
  pass "temp copy: exit 0"
else
  fail "temp copy: expected exit 0, got $RC_B — $OUT_B"
fi

# --- Test (c): removing the SOLE-writer mandate fails 5c --------------------
echo "Test (c): dropping the 'SOLE merge-gating verdict writer' mandate fails"
perl -i -pe 's/\QSOLE merge-gating verdict writer\E/post-verdict.sh/g' "$FIX_JUDGE"
OUT_C="$(bash "$CHECK" "$FIX" 2>&1)"; RC_C=$?
if [[ "$RC_C" -eq 1 ]]; then
  pass "mutated tree: exit 1"
else
  fail "mutated tree: expected exit 1, got $RC_C — $OUT_C"
fi
if [[ "$OUT_C" == *"SOLE merge-gating verdict writer"* && "$OUT_C" == *"#12136"* ]]; then
  pass "failure names the SOLE-writer mandate and #12136"
else
  fail "failure did not name the SOLE-writer mandate / #12136 — $OUT_C"
fi

# Restore and prove the control passes again (isolates the mutation).
cp -a "$REPO/defaults/.claude/commands/loom/judge.md" "$FIX_JUDGE"
OUT_C2="$(bash "$CHECK" "$FIX" 2>&1)"; RC_C2=$?
if [[ "$RC_C2" -eq 0 ]]; then
  pass "restored tree: exit 0 (the mutation, not the copy, caused the failure)"
else
  fail "restored tree: expected exit 0, got $RC_C2 — $OUT_C2"
fi

# --- Test (d): removing the bare-bypass forbiddance fails 5c ----------------
echo "Test (d): dropping the bare 'gh pr edit --add-label loom:pr' forbiddance fails"
perl -i -pe 's/\Qbare `gh pr edit --add-label "loom:pr"`\E/bare gh command/g' "$FIX_JUDGE"
OUT_D="$(bash "$CHECK" "$FIX" 2>&1)"; RC_D=$?
if [[ "$RC_D" -eq 1 ]]; then
  pass "mutated tree: exit 1"
else
  fail "mutated tree: expected exit 1, got $RC_D — $OUT_D"
fi
if [[ "$OUT_D" == *"bare 'gh pr edit --add-label loom:pr' approval bypass"* && "$OUT_D" == *"#12136"* ]]; then
  pass "failure names the bare-bypass forbiddance and #12136"
else
  fail "failure did not name the bare-bypass forbiddance / #12136 — $OUT_D"
fi

echo
if [[ "$TESTS_FAILED" -eq 0 ]]; then
  echo -e "${GREEN}All $TESTS_RUN checks passed.${NC}"
  exit 0
else
  echo -e "${RED}$TESTS_FAILED of $TESTS_RUN checks failed.${NC}"
  exit 1
fi
