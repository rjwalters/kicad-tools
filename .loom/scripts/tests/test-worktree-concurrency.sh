#!/usr/bin/env bash
# test-worktree-concurrency.sh — Tests for worktree.sh concurrency lock + partial-state cleanup (#3380)
#
# Verifies the fix for "worktree.sh chronic hangs on concurrent invocations":
#   - Parallel invocations against different issues serialize cleanly and finish quickly.
#   - Parallel invocations against the same issue produce no partial state.
#   - Stale `.git/worktrees/issue-N/index.lock` files are recovered automatically.
#   - Half-created `.loom/worktrees/issue-N/` dirs (not registered with git) are removed
#     and recreated, instead of erroring out with the "Directory exists but is not a
#     registered worktree" path.
#   - A stale lock dir from a dead PID is recovered (not stranded).
#   - On lock-acquisition timeout, --json emits the documented error schema.
#
# Pattern follows test-worktree-sentinel.sh: throw-away bare origin + clone in a
# mktemp dir, copy worktree.sh + lib/, exercise behaviors.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Counter file persists across subshells (each test runs in `(...)`).
COUNTER_FILE=$(mktemp /tmp/loom-wt-concur-counts.XXXXXX)
echo "0 0 0" > "$COUNTER_FILE"
trap 'rm -f "$COUNTER_FILE"' EXIT

bump() {
    local kind="$1"
    local run pass fail
    read -r run pass fail < "$COUNTER_FILE"
    run=$((run + 1))
    if [[ "$kind" == "pass" ]]; then pass=$((pass + 1)); else fail=$((fail + 1)); fi
    echo "$run $pass $fail" > "$COUNTER_FILE"
}

pass() { bump pass; echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { bump fail; echo -e "  ${RED}FAIL${NC}: $1"; }

# Setup a throw-away repo for one test. Echoes the path of the repo working tree.
setup_repo() {
    local tmp
    tmp=$(mktemp -d /tmp/loom-worktree-concur.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/repo"
    (
        cd "$tmp/repo"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin main
        # Mirror the .loom layout worktree.sh expects to find.
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh
    )
    echo "$tmp/repo"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    # The parent of the repo is the mktemp dir; remove the whole thing.
    rm -rf "$(dirname "$repo")"
}

# --- Test 1: three concurrent invocations on different issues complete fast ---
echo "Test 1: three concurrent invocations (different issues) complete within 60s"
REPO=$(setup_repo)
(
    cd "$REPO"
    # Launch three invocations in parallel; the outer `timeout 60` enforces the
    # acceptance criterion. We swallow stdout but preserve exit codes via wait.
    set +e
    timeout 60 bash -c '
        ./.loom/scripts/worktree.sh 90 >/tmp/wt-90.$$ 2>&1 &
        p90=$!
        ./.loom/scripts/worktree.sh 91 >/tmp/wt-91.$$ 2>&1 &
        p91=$!
        ./.loom/scripts/worktree.sh 92 >/tmp/wt-92.$$ 2>&1 &
        p92=$!
        wait $p90; r90=$?
        wait $p91; r91=$?
        wait $p92; r92=$?
        if [[ $r90 -ne 0 || $r91 -ne 0 || $r92 -ne 0 ]]; then
            echo "--- worktree.sh 90 output (rc=$r90) ---" >&2
            cat /tmp/wt-90.$$ >&2 2>/dev/null || true
            echo "--- worktree.sh 91 output (rc=$r91) ---" >&2
            cat /tmp/wt-91.$$ >&2 2>/dev/null || true
            echo "--- worktree.sh 92 output (rc=$r92) ---" >&2
            cat /tmp/wt-92.$$ >&2 2>/dev/null || true
        fi
        rm -f /tmp/wt-9[0-2].$$
        [[ $r90 -eq 0 && $r91 -eq 0 && $r92 -eq 0 ]]
    '
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        if [[ -f .loom/worktrees/issue-90/.loom-managed \
           && -f .loom/worktrees/issue-91/.loom-managed \
           && -f .loom/worktrees/issue-92/.loom-managed ]]; then
            pass "three concurrent invocations (issues 90/91/92) all succeed within 60s"
        else
            fail "all three exited 0 but at least one .loom-managed sentinel is missing"
        fi
    else
        fail "concurrent invocation timed out or one of three children failed (rc=$rc)"
    fi
)
cleanup_repo "$REPO"

# --- Test 2: stale .git/worktrees/issue-N/index.lock is recovered ---
echo ""
echo "Test 2: stale index.lock under .git/worktrees/issue-N/ is recovered"
REPO=$(setup_repo)
(
    cd "$REPO"
    mkdir -p .git/worktrees/issue-93
    : > .git/worktrees/issue-93/index.lock
    if ./.loom/scripts/worktree.sh 93 >/tmp/wt-93.$$ 2>&1; then
        if [[ ! -e .git/worktrees/issue-93/index.lock ]]; then
            pass "stale index.lock is removed and worktree created normally"
        else
            fail "worktree.sh exited 0 but stale index.lock still present"
        fi
    else
        fail "worktree.sh failed in the presence of a stale index.lock (see /tmp/wt-93.$$)"
    fi
    rm -f /tmp/wt-93.$$
)
cleanup_repo "$REPO"

# --- Test 3: half-created .loom/worktrees/issue-N/ (unregistered) is recovered ---
echo ""
echo "Test 3: half-created .loom/worktrees/issue-N/ dir is recovered"
REPO=$(setup_repo)
(
    cd "$REPO"
    mkdir -p .loom/worktrees/issue-94
    # Deliberately make this dir NOT registered with git — it's a shell.
    : > .loom/worktrees/issue-94/leftover-file
    if ./.loom/scripts/worktree.sh 94 >/tmp/wt-94.$$ 2>&1; then
        if [[ -f .loom/worktrees/issue-94/.loom-managed ]]; then
            pass "half-created dir is removed and worktree recreated with sentinel"
        else
            fail "worktree.sh exited 0 but .loom-managed sentinel missing — likely fell into idempotent branch"
        fi
    else
        fail "worktree.sh failed to recover from half-created dir (see /tmp/wt-94.$$)"
    fi
    rm -f /tmp/wt-94.$$
)
cleanup_repo "$REPO"

# --- Test 4: stale lock dir owned by a dead PID is recovered ---
echo ""
echo "Test 4: stale .loom/locks/worktree-N/ owned by dead PID is recovered"
REPO=$(setup_repo)
(
    cd "$REPO"
    # Find a guaranteed-dead PID by spawning + reaping a short-lived child.
    bash -c 'exit 0' &
    DEAD_PID=$!
    wait $DEAD_PID 2>/dev/null || true
    # Tiny safety: keep retrying until a non-existent PID is found.
    while kill -0 "$DEAD_PID" 2>/dev/null; do
        bash -c 'exit 0' &
        DEAD_PID=$!
        wait $DEAD_PID 2>/dev/null || true
    done

    # Lock is repo-global at .loom/locks/worktree-add/ — stage it there.
    mkdir -p .loom/locks/worktree-add
    cat > .loom/locks/worktree-add/owner.json <<EOF
{
  "issue": 95,
  "owner_pid": $DEAD_PID,
  "script": "worktree.sh",
  "acquired_at": "1970-01-01T00:00:00Z"
}
EOF

    if ./.loom/scripts/worktree.sh 95 >/tmp/wt-95.$$ 2>&1; then
        if [[ -f .loom/worktrees/issue-95/.loom-managed ]]; then
            pass "stale lock from dead PID $DEAD_PID is broken; worktree created"
        else
            fail "worktree.sh exited 0 but .loom-managed sentinel missing"
        fi
    else
        fail "worktree.sh failed to recover from stale lock (see /tmp/wt-95.$$)"
    fi
    rm -f /tmp/wt-95.$$
)
cleanup_repo "$REPO"

# --- Test 5: concurrent invocations on the SAME issue serialize cleanly ---
echo ""
echo "Test 5: two concurrent invocations on the same issue serialize cleanly"
REPO=$(setup_repo)
(
    cd "$REPO"
    set +e
    timeout 60 bash -c '
        ./.loom/scripts/worktree.sh 96 >/tmp/wt-96a.$$ 2>&1 &
        a=$!
        ./.loom/scripts/worktree.sh 96 >/tmp/wt-96b.$$ 2>&1 &
        b=$!
        wait $a; ra=$?
        wait $b; rb=$?
        rm -f /tmp/wt-96a.$$ /tmp/wt-96b.$$
        [[ $ra -eq 0 && $rb -eq 0 ]]
    '
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        if [[ -f .loom/worktrees/issue-96/.loom-managed ]]; then
            # Lock dir should be empty at the end of both runs (lock is
            # repo-global at .loom/locks/worktree-add/, see worktree.sh).
            if [[ ! -d .loom/locks/worktree-add ]]; then
                pass "same-issue concurrent invocations both exit 0; lock dir cleaned up"
            else
                fail "both exited 0 but lock dir still present: .loom/locks/worktree-add"
            fi
        else
            fail "both exited 0 but .loom-managed sentinel missing"
        fi
    else
        fail "concurrent same-issue invocations did not both succeed (rc=$rc)"
    fi
)
cleanup_repo "$REPO"

# --- Test 6: lock-timeout failure emits documented --json error schema ---
echo ""
echo "Test 6: lock-timeout --json error includes 'worktree-lock-timeout' and holderPid"
REPO=$(setup_repo)
(
    cd "$REPO"
    # Stage a lock owned by a definitely-live PID (this shell) so it can't be
    # cleared as stale. Force the timeout to 1s so we don't actually wait.
    # Lock is repo-global; stage it at the global path.
    mkdir -p .loom/locks/worktree-add
    cat > .loom/locks/worktree-add/owner.json <<EOF
{
  "issue": 97,
  "owner_pid": $$,
  "script": "worktree.sh",
  "acquired_at": "1970-01-01T00:00:00Z"
}
EOF
    set +e
    OUT=$(LOOM_WORKTREE_LOCK_TIMEOUT=1 LOOM_WORKTREE_LOCK_POLL_INTERVAL=1 \
        ./.loom/scripts/worktree.sh --json 97 2>&1)
    rc=$?
    set -e
    if [[ $rc -ne 0 ]] && echo "$OUT" | grep -q 'worktree-lock-timeout' && echo "$OUT" | grep -q "\"holderPid\": \"$$\""; then
        pass "lock-timeout error JSON includes 'worktree-lock-timeout' and holder PID $$"
    else
        fail "lock-timeout JSON output missing expected fields (rc=$rc, output: $OUT)"
    fi
    # Cleanup the synthetic lock we created.
    rm -rf .loom/locks/worktree-add
)
cleanup_repo "$REPO"

# --- Summary ---
read -r TESTS_RUN TESTS_PASSED TESTS_FAILED < "$COUNTER_FILE"
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
