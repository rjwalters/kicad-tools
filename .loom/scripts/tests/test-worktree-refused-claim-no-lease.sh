#!/usr/bin/env bash
# test-worktree-refused-claim-no-lease.sh — Tests for #10204.
#
# `worktree.sh N` used to run `loom-daemon lease ensure` at pre-flight, BEFORE
# its refusal gates. Every refusal therefore left a published lease comment and
# a running renewer behind for a claim the caller was told it did not hold. The
# call now runs only once each arm's own operation has succeeded:
# `_worktree_sparse reconfigure`, `_worktree_existing`, `git worktree add`.
#
# Driven by a stub `loom-daemon` (LOOM_DAEMON_SELF_BIN) that logs every
# invocation. Each subcommand answers from a STUB_* env var; anything unset
# exits 2 (unrecognized -> the script's fail-open / no-daemon path).
#   1. forge check-claim exits 0 (open linked PR)        -> refused, no lease
#   2. safe claim                                        -> created, leased once
#   3. reuse arm (worktree already exists)               -> leased
#   4. claim-lock check-issue exits 1                    -> refused, no lease
#   5. reuse arm, dir is not a git worktree              -> refused, no lease
#   6. #8280 worktree-branch-reuse refusal (local branch) -> refused, no lease
#   7. #7765 fork/cross-repo open PR owns the branch      -> refused, no lease
#   8. `git worktree add` itself fails                   -> no worktree, no lease
#   9. --sparse on an existing worktree, reconfigure fails -> no lease
#  10. --sparse on an existing worktree, reconfigure ok    -> leased

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

TMP=$(mktemp -d /tmp/loom-refused-lease.XXXXXX)
trap 'rm -rf "$TMP"; cd "$SCRIPTS_DIR" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main
mkdir -p .loom/scripts/lib .loom/hooks
cp "$WORKTREE_SH" .loom/scripts/worktree.sh
[[ -d "$SCRIPTS_DIR/lib" ]] && cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
chmod +x .loom/scripts/worktree.sh

STUB="$TMP/loom-daemon"
LOG="$TMP/daemon.log"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
# opt_in <rc-var-value>: a subcommand whose STUB_* var is set answers --help
# with 0 (so the script takes the daemon path) and the real call with the value.
opt_in() { [[ -n "$1" ]] || exit 2; [[ "$2" == "--help" ]] && exit 0; }
case "$1" in
    forge)        [[ "$2" == "check-claim" ]] && exit "${STUB_CHECK_CLAIM_RC:-1}" ;;
    worktree-lock) exit "${STUB_CHECK_ISSUE_RC:-2}" ;;
    lease)        [[ "$2" == "ensure" ]] && exit 0 ;;
    worktree-branch-reuse) opt_in "${STUB_BRANCH_REUSE_RC:-}" "$2"; exit "$STUB_BRANCH_REUSE_RC" ;;
    worktree-sparse) opt_in "${STUB_SPARSE_RC:-}" "$2"; exit "$STUB_SPARSE_RC" ;;
    worktree-open-pr)
        opt_in "${STUB_OPEN_PR_CROSS:-}" "$2"
        printf 'STATUS\tfound\nNUMBER\t9\nCROSS_REPO\ttrue\nHEAD_REPO\tfork/loom\nHEAD_REF\t%s\n' "feature/x"
        exit 0 ;;
esac
exit 2
STUBEOF
chmod +x "$STUB"
export LOOM_DAEMON_SELF_BIN="$STUB" STUB_LOG="$LOG"

# A git shim that fails `git worktree add` (only when GIT_SHIM_FAIL_ADD=1) and
# delegates everything else to the real git.
REAL_GIT="$(command -v git)"
mkdir -p "$TMP/shim"
cat > "$TMP/shim/git" <<SHIMEOF
#!/usr/bin/env bash
if [[ "\${GIT_SHIM_FAIL_ADD:-}" == "1" && "\${1:-}" == "worktree" && "\${2:-}" == "add" ]]; then
    echo "fatal: simulated worktree add failure" >&2; exit 1
fi
exec "$REAL_GIT" "\$@"
SHIMEOF
chmod +x "$TMP/shim/git"

run_wt() { # <args...> -> sets RC, resets log
    : > "$LOG"; RC=0
    ./.loom/scripts/worktree.sh "$@" >"$TMP/out.log" 2>&1 || RC=$?
}
lease_calls() { grep -c '^lease ensure' "$LOG" || true; }
expect_refused_no_lease() { # <issue> <label>
    if [[ "$RC" -ne 0 && "$(lease_calls)" -eq 0 && ! -e ".loom/worktrees/issue-$1/.git" ]]; then
        pass "$2: refused, no worktree, no lease"
    else
        fail "$2: rc=$RC leases=$(lease_calls)"; cat "$TMP/out.log"
    fi
}

echo "Test 1: open linked PR (check-claim exit 0) -> refused, no lease"
STUB_CHECK_CLAIM_RC=0 run_wt 501
expect_refused_no_lease 501 "check-claim"

echo "Test 2: safe claim (check-claim exit 1) -> worktree created, lease ensured once"
run_wt 502
if [[ "$RC" -eq 0 && -d .loom/worktrees/issue-502 ]]; then pass "worktree created"; else fail "expected success (rc=$RC)"; cat "$TMP/out.log"; fi
if grep -q '^lease ensure 502 ' "$LOG" && [[ "$(lease_calls)" -eq 1 ]]; then pass "lease ensure called once"; else fail "lease ensure calls: $(lease_calls)"; fi

echo "Test 3: reuse arm (worktree exists) -> lease still ensured"
run_wt 502
if [[ "$RC" -eq 0 ]] && grep -q '^lease ensure 502 ' "$LOG"; then pass "reuse arm leases"; else fail "reuse arm did not lease (rc=$RC)"; cat "$TMP/out.log"; fi

echo "Test 4: claim-lock conflict (check-issue exit 1) -> refused, no lease"
STUB_CHECK_ISSUE_RC=1 run_wt 503
expect_refused_no_lease 503 "claim-lock"

echo "Test 5: reuse arm on a directory that is not a git worktree -> refused, no lease"
mkdir -p .loom/worktrees/issue-504
run_wt 504
expect_refused_no_lease 504 "worktree-existing"

echo "Test 6: #8280 branch-reuse refusal on an existing local branch -> refused, no lease"
git branch feature/issue-506
STUB_BRANCH_REUSE_RC=1 run_wt 506
expect_refused_no_lease 506 "branch-reuse"

echo "Test 7: #7765 fork PR owns the branch name -> refused, no lease"
STUB_OPEN_PR_CROSS=1 run_wt 507
expect_refused_no_lease 507 "cross-repo open PR"
if grep -q "FORK" "$TMP/out.log"; then pass "refused by the cross-repo arm"; else fail "not refused by the cross-repo arm"; cat "$TMP/out.log"; fi

echo "Test 8: git worktree add fails -> no worktree, no lease"
GIT_SHIM_FAIL_ADD=1 PATH="$TMP/shim:$PATH" run_wt 508
expect_refused_no_lease 508 "worktree add failure"

echo "Test 9: --sparse on existing worktree, reconfigure fails -> no lease"
STUB_SPARSE_RC=1 run_wt 502 --sparse docs
if [[ "$RC" -ne 0 && "$(lease_calls)" -eq 0 ]]; then pass "failed reconfigure, no lease"; else fail "rc=$RC leases=$(lease_calls)"; cat "$TMP/out.log"; fi

echo "Test 10: --sparse on existing worktree, reconfigure succeeds -> leased"
STUB_SPARSE_RC=0 run_wt 502 --sparse docs
if [[ "$RC" -eq 0 ]] && grep -q '^lease ensure 502 ' "$LOG"; then pass "reconfigure leases"; else fail "rc=$RC leases=$(lease_calls)"; cat "$TMP/out.log"; fi

echo
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
