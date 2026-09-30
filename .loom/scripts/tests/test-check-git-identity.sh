#!/usr/bin/env bash
# test-check-git-identity.sh - Smoke tests for check-git-identity.sh (#4369)
#
# Exercises the local-scope git identity hygiene detector: clean single-value
# identity, stacked/multi-value identity, the glued-shell-command corruption
# pattern (e.g. "...github.comecho"), global-only identity, per-worktree
# config, and the printed remediation command. Each test runs against a
# throwaway temp git repo so the result is deterministic and independent of
# the host's real git identity.
#
# Uses GIT_CONFIG_GLOBAL (git >= 2.32) to sandbox the "global" scope per test
# so results never depend on — and never mutate — the host's real ~/.gitconfig.
#
# Usage:
#   ./.loom/scripts/tests/test-check-git-identity.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-git-identity.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

# Sandboxed empty global config so tests never read/depend on the real host
# ~/.gitconfig. A per-test override can point this at a populated file to
# simulate "global identity present".
EMPTY_GLOBAL="$WORKDIR/empty-global-config"
: > "$EMPTY_GLOBAL"

# make_repo -> path to a fresh temp git repo with NO identity configured at
# any scope (relies on GIT_CONFIG_GLOBAL sandboxing at call sites, not on
# `git config user.*`, so the repo itself starts with zero local values).
make_repo() {
    local dir
    dir=$(mktemp -d)
    GIT_CONFIG_GLOBAL="$EMPTY_GLOBAL" git -C "$dir" init -q
    echo "$dir"
}

# run_rc <repo> [global-config-file] -- runs the script with a sandboxed
# global config (defaults to the empty one) and records stdout+stderr / rc.
run_check() {
    local repo="$1" global="${2:-$EMPTY_GLOBAL}"
    OUT=$(cd "$repo" && GIT_CONFIG_GLOBAL="$global" "$SCRIPT" 2>&1)
    RC=$?
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-git-identity.sh is executable"
else
    fail "check-git-identity.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: --help exits 0 and prints usage --------
echo "Test 2: --help exits 0 with usage output"
out=$("$SCRIPT" --help 2>&1); rc=$?
if [[ "$rc" -eq 0 && "$out" == *"check-git-identity.sh"* && "$out" == *"Exit codes"* ]]; then
    pass "--help prints usage and exits 0"
else
    fail "--help: expected 0 + usage text, got rc=$rc"
fi

# -------- Test 3: unknown argument exits 2 --------
echo "Test 3: unknown argument exits 2"
"$SCRIPT" --bogus >/dev/null 2>&1; rc=$?
if [[ "$rc" -eq 2 ]]; then pass "exit 2 on unknown argument"; else fail "expected 2, got $rc"; fi

# -------- Test 4: not a git repository exits 2 --------
echo "Test 4: not inside a git repository exits 2"
NOTAREPO=$(mktemp -d)
out=$(cd "$NOTAREPO" && "$SCRIPT" 2>&1); rc=$?
if [[ "$rc" -eq 2 ]] && grep -qi "not inside a git repository" <<<"$out"; then
    pass "exit 2 + message outside a git repo"
else
    fail "expected 2 + message, got rc=$rc; out=$out"
fi
rm -rf "$NOTAREPO"

# -------- Test 5: clean single-value local identity exits 0 --------
echo "Test 5: clean single-value local identity exits 0"
REPO=$(make_repo)
git -C "$REPO" config user.email "dev@example.com"
git -C "$REPO" config user.name "Dev Person"
run_check "$REPO"
if [[ "$RC" -eq 0 ]]; then pass "exit 0 on clean single-value identity"; else fail "expected 0, got $RC; out=$OUT"; fi
rm -rf "$REPO"

# -------- Test 6: no local identity at all (global-only, sandboxed) exits 0 --------
echo "Test 6: no local values (global-only identity) exits 0"
REPO=$(make_repo)
GLOBAL_WITH_ID="$WORKDIR/global-with-identity"
git config --file "$GLOBAL_WITH_ID" user.email "global@example.com"
git config --file "$GLOBAL_WITH_ID" user.name "Global Person"
run_check "$REPO" "$GLOBAL_WITH_ID"
if [[ "$RC" -eq 0 ]]; then
    pass "exit 0 with only a global identity set (no local override)"
else
    fail "expected 0, got $RC; out=$OUT"
fi
rm -rf "$REPO"

# -------- Test 7: stacked (multi-value) local user.email -> warn (exit 1) --------
echo "Test 7: stacked local user.email values warn (exit 1)"
REPO=$(make_repo)
git -C "$REPO" config user.email "first@example.com"
git -C "$REPO" config --add user.email "second@example.com"
run_check "$REPO"
if [[ "$RC" -eq 1 ]] \
   && grep -q "first@example.com" <<<"$OUT" \
   && grep -q "second@example.com" <<<"$OUT" \
   && grep -qi "WARNING" <<<"$OUT"; then
    pass "exit 1 + both stacked values listed"
else
    fail "expected 1 + both values, got rc=$RC; out=$OUT"
fi
rm -rf "$REPO"

# -------- Test 8: corrupted value (glued 'echo') -> hard fail (exit 3) --------
echo "Test 8: corrupted '...github.comecho' value hard-fails (exit 3)"
REPO=$(make_repo)
git -C "$REPO" config user.email "loom-worker@users.noreply.github.com"
git -C "$REPO" config --add user.email "loom-worker@users.noreply.github.comecho"
run_check "$REPO"
if [[ "$RC" -eq 3 ]] \
   && grep -q "loom-worker@users.noreply.github.comecho" <<<"$OUT" \
   && grep -qi "ERROR" <<<"$OUT"; then
    pass "exit 3 + corrupted value reported"
else
    fail "expected 3 + corrupted value, got rc=$RC; out=$OUT"
fi

# -------- Test 9: corruption output includes the documented remediation --------
echo "Test 9: corruption output includes the documented one-liner remediation"
if grep -q "git config --unset-all user.email && git config --unset-all user.name" <<<"$OUT" \
   && grep -q -- "--replace-all user.email" <<<"$OUT"; then
    pass "remediation one-liner + --replace-all fallback both printed"
else
    fail "expected both remediation commands in output; out=$OUT"
fi
rm -rf "$REPO"

# -------- Test 10: single corrupted value (no stacking) still hard-fails --------
echo "Test 10: a single (non-stacked) corrupted value still hard-fails"
REPO=$(make_repo)
git -C "$REPO" config user.email "loom-reviewer@users.noreply.github.comecho"
run_check "$REPO"
if [[ "$RC" -eq 3 ]]; then pass "exit 3 on a single corrupted value"; else fail "expected 3, got $RC; out=$OUT"; fi
rm -rf "$REPO"

# -------- Test 11: legitimate unusual TLD is NOT flagged as corruption --------
echo "Test 11: 'first.last@company.comm' is not flagged as corruption"
REPO=$(make_repo)
git -C "$REPO" config user.email "first.last@company.comm"
run_check "$REPO"
if [[ "$RC" -eq 0 ]]; then
    pass "exit 0 for legitimate unusual-TLD address (no false positive)"
else
    fail "expected 0 (no false positive), got $RC; out=$OUT"
fi
rm -rf "$REPO"

# -------- Test 12: no global identity -> remediation mentions --replace-all fallback --------
echo "Test 12: no global identity present -> message mentions --replace-all fallback"
REPO=$(make_repo)
git -C "$REPO" config user.email "loom-curator@users.noreply.github.comecho"
run_check "$REPO" "$EMPTY_GLOBAL"
if [[ "$RC" -eq 3 ]] && grep -qi -- "--replace-all" <<<"$OUT"; then
    pass "no-global-identity case still surfaces the --replace-all fallback"
else
    fail "expected 3 + --replace-all mention, got rc=$RC; out=$OUT"
fi
rm -rf "$REPO"

# -------- Test 13: worktree-specific config (extensions.worktreeConfig) is read --------
echo "Test 13: a value set only in a per-worktree config is detected"
REPO=$(make_repo)
git -C "$REPO" config user.email "main@example.com"
git -C "$REPO" config extensions.worktreeConfig true
git -C "$REPO" worktree add -q "$WORKDIR/wt13" -b feature-13 >/dev/null 2>&1
git -C "$WORKDIR/wt13" config --worktree user.email "worktree-only@example.com"
run_check "$WORKDIR/wt13"
if [[ "$RC" -eq 1 ]] \
   && grep -q "main@example.com" <<<"$OUT" \
   && grep -q "worktree-only@example.com" <<<"$OUT"; then
    pass "per-worktree config value detected alongside the repo-level value"
else
    fail "expected 1 + both values, got rc=$RC; out=$OUT"
fi
git -C "$REPO" worktree remove --force "$WORKDIR/wt13" >/dev/null 2>&1 || true
rm -rf "$REPO"

# -------- Test 14: corruption takes priority over a co-occurring multi-value warning --------
echo "Test 14: corruption (exit 3) takes priority over plain multi-value (exit 1)"
REPO=$(make_repo)
git -C "$REPO" config user.email "clean@example.com"
git -C "$REPO" config --add user.email "loom-worker@users.noreply.github.comecho"
run_check "$REPO"
if [[ "$RC" -eq 3 ]]; then
    pass "corruption exit code (3) wins over plain multi-value (1)"
else
    fail "expected 3, got $RC; out=$OUT"
fi
rm -rf "$REPO"

# -------- Summary --------
echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED/$TESTS_RUN tests passed${NC}"
    exit 0
else
    echo -e "${RED}FAILED: $TESTS_FAILED/$TESTS_RUN tests failed${NC}"
    exit 1
fi
