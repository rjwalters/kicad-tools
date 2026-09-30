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
# Also covers the issue #6014 fixes:
#   - A late release from a stale/wedged holder (wrong acquisition token)
#     cannot remove a different, currently-live holder's lock.
#   - A release with no token (never acquired / already released) is a no-op.
#   - The git-worktree-add lock is released as soon as `git worktree add`
#     completes, not held through a slow post-worktree hook -- a second,
#     unrelated invocation is not serialized behind the first one's hook.
#
# Pattern follows test-worktree-sentinel.sh: throw-away bare origin + clone in a
# mktemp dir, copy worktree.sh + lib/, exercise behaviors.
#
# Needs a BUILT `loom-daemon` since #8195 slice 5: the partial-state cleanup
# Tests 2 and 3 assert on (`cleanup_partial_worktree_state`) is now a thin stub
# over `loom-daemon worktree-cleanup`, so those two recoveries do not happen at
# all without the binary. Every assertion below is unchanged from the shell
# implementation — running them against the port is the equivalence evidence —
# so this suite moved to the "Native Port Suites" CI job, which builds the
# binary, and FAILS rather than skips without one.
#
# The lock/concurrency assertions (Tests 1, 4-8) now exercise `loom-daemon
# worktree-lock acquire`/`release` too (#8195 slice 7): worktree.sh's
# acquire_worktree_lock/release_worktree_lock try the daemon FIRST and fall
# straight through to their own mkdir bodies, UNCHANGED, on anything that is not
# a real answer from it (#8226 reverted a hard, exec-style delegation on this
# always-taken path; this is not that). Every assertion is unchanged from the
# shell-only implementation — running them against a real, built binary (which
# this suite already requires) is the equivalence evidence. Test 7 alone stays
# on the shell body deliberately: it sources the extracted primitives with
# $_WT_DAEMON_BIN unset, which is what exercises the ownership-token rules it is
# about. Test 9 covers the fallback discrimination itself. They stay here rather
# than splitting into a second file — they share this suite's fixture, and a
# split would duplicate it.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-worktree-concurrency.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-cleanup"

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

# Echo the path of a temp file holding worktree.sh's lock primitives —
# _worktree_locks_dir / _worktree_lock_path / acquire_worktree_lock /
# release_worktree_lock — cut out of the copy under test, for a caller that
# wants to drive them directly instead of through a full worktree.sh run.
# Tests 7 and 9 both do; sharing this is what keeps the extraction from drifting
# between them. Must be run from inside the fixture repo.
#
# Pipe-free on purpose (`awk … exit` rather than `grep -n | head -1 | cut`):
# under `set -o pipefail` an early-exit consumer can SIGPIPE its producer and
# fail the whole pipeline — scripts/check-pipefail-early-exit.sh's class.
extract_lock_functions() {
    local start end out
    start=$(awk '/^_worktree_locks_dir\(\) \{/{print NR; exit}' .loom/scripts/worktree.sh)
    end=$(awk '/^release_worktree_lock\(\) \{/{f=1} f && /^}/{print NR; exit}' .loom/scripts/worktree.sh)
    out=$(mktemp /tmp/loom-wt-lockfns.XXXXXX)
    sed -n "${start},${end}p" .loom/scripts/worktree.sh > "$out"
    echo "$out"
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
    if [[ $rc -ne 0 ]] && grep -q 'worktree-lock-timeout' <<<"$OUT" && grep -q "\"holderPid\": \"$$\"" <<<"$OUT"; then
        pass "lock-timeout error JSON includes 'worktree-lock-timeout' and holder PID $$"
    else
        fail "lock-timeout JSON output missing expected fields (rc=$rc, output: $OUT)"
    fi
    # Cleanup the synthetic lock we created.
    rm -rf .loom/locks/worktree-add
)
cleanup_repo "$REPO"

# --- Test 7: late release from a stale/wedged holder cannot clobber a live holder's lock (#6014) ---
echo ""
echo "Test 7: late release with a stale token cannot remove a different, live holder's lock"
REPO=$(setup_repo)
(
    cd "$REPO"

    # Exercise the exact lock primitives worktree.sh uses (not a hand-rolled
    # reimplementation): extract _worktree_locks_dir / _worktree_lock_path /
    # acquire_worktree_lock / release_worktree_lock from the copy of
    # worktree.sh under test and source them directly. $_WT_DAEMON_BIN is
    # resolved outside this range and so is unset here, which keeps this test on
    # the shell body (#8195 slice 7) — the ownership-token rules below are what
    # it is about, and they are the fallback's own, not the daemon's.
    FUNCS_FILE=$(extract_lock_functions)

    # These three are consumed only inside the extracted lock functions that
    # get `source`d from "$FUNCS_FILE" below — a dynamic path shellcheck
    # cannot statically follow, so it reports them as unused (SC2034).
    # shellcheck disable=SC2034
    LOOM_WORKTREE_LOCK_TIMEOUT=5
    # shellcheck disable=SC2034
    LOOM_WORKTREE_LOCK_POLL_INTERVAL=1
    # shellcheck disable=SC2034
    JSON_OUTPUT=""
    print_warning() { :; }

    # shellcheck disable=SC1090
    source "$FUNCS_FILE"
    rm -f "$FUNCS_FILE"

    # --- Process A acquires the lock and remembers its own token. ---
    acquire_worktree_lock 200
    TOKEN_A="$WORKTREE_LOCK_TOKEN"

    # --- An operator judges A dead/wedged and manually clears the lock dir
    #     directly -- the exact recovery path acquire_worktree_lock's own
    #     timeout error message suggests (issue #6014, step 2). ---
    rm -rf "$(_worktree_lock_path 200)"

    # --- Process B legitimately re-acquires the now-vacant lock. ---
    acquire_worktree_lock 201
    TOKEN_B="$WORKTREE_LOCK_TOKEN"

    if [[ "$TOKEN_A" == "$TOKEN_B" ]]; then
        fail "test setup invalid: acquisition tokens A and B are identical"
    else
        # --- Process A, unaware it was pre-empted, finally unwinds and its
        #     EXIT trap fires release_worktree_lock with ITS OWN remembered
        #     token (not whatever now happens to be current on disk). ---
        release_worktree_lock 200 "$TOKEN_A"

        LOCK_201="$(_worktree_lock_path 201)"
        if [[ -d "$LOCK_201" ]] && [[ -f "$LOCK_201/owner.json" ]] && grep -q "$TOKEN_B" "$LOCK_201/owner.json"; then
            pass "stale holder A's late release (mismatched token) left live holder B's lock intact"
        else
            fail "stale holder A's late release clobbered live holder B's lock"
        fi

        # --- Process B's own, correctly-tokened release DOES remove it. ---
        release_worktree_lock 201 "$TOKEN_B"
        if [[ ! -d "$LOCK_201" ]]; then
            pass "live holder B's own release (matching token) removes its lock"
        else
            fail "live holder B's own release (matching token) failed to remove its lock"
        fi
    fi

    # --- An empty token (never acquired, or already released) must never
    #     remove a lock -- defends against a caller accidentally releasing
    #     before/without ever successfully acquiring. ---
    LOCK_202="$(_worktree_lock_path 202)"
    mkdir -p "$LOCK_202"
    cat > "$LOCK_202/owner.json" <<EOF
{
  "issue": 202,
  "owner_pid": $$,
  "token": "some-real-token",
  "script": "worktree.sh",
  "acquired_at": "1970-01-01T00:00:00Z"
}
EOF
    release_worktree_lock 202 ""
    if [[ -d "$LOCK_202" ]]; then
        pass "release with an empty token is a no-op (never removes an unowned lock)"
    else
        fail "release with an empty token incorrectly removed a lock"
    fi
    rm -rf "$LOCK_202"
)
cleanup_repo "$REPO"

# --- Test 8: the lock is released right after `git worktree add`, not held through the post-worktree hook (#6014) ---
echo ""
echo "Test 8: a slow post-worktree hook for one issue does not serialize an unrelated invocation"
REPO=$(setup_repo)
(
    cd "$REPO"

    MARKER="/tmp/loom-wt-hook-marker-$$"
    rm -f "$MARKER"
    # Hook blocks ONLY for issue 98 (simulating a slow `cargo build --release`
    # in a project's real post-worktree hook), signaling readiness via a
    # marker file so the test doesn't have to guess timing.
    cat > .loom/hooks/post-worktree.sh <<EOF
#!/usr/bin/env bash
if [[ "\$3" == "98" ]]; then
    touch "$MARKER"
    sleep 6
fi
exit 0
EOF
    chmod +x .loom/hooks/post-worktree.sh

    set +e
    ./.loom/scripts/worktree.sh 98 >/tmp/wt-98.$$ 2>&1 &
    PID98=$!

    # Wait (bounded) for the hook to signal it has started sleeping -- i.e.
    # `git worktree add` for issue 98 is done and its lock should already be
    # released.
    WAITED=0
    while [[ ! -f "$MARKER" ]] && [[ $WAITED -lt 30 ]]; do
        sleep 1
        WAITED=$((WAITED + 1))
    done

    if [[ ! -f "$MARKER" ]]; then
        fail "post-worktree hook for issue 98 never signaled start (see /tmp/wt-98.$$)"
        kill "$PID98" 2>/dev/null
        wait "$PID98" 2>/dev/null
    else
        START=$(date +%s)
        timeout 30 ./.loom/scripts/worktree.sh 99 >/tmp/wt-99.$$ 2>&1
        rc99=$?
        ELAPSED=$(( $(date +%s) - START ))

        if [[ $rc99 -eq 0 ]] && [[ -f .loom/worktrees/issue-99/.loom-managed ]] && [[ $ELAPSED -lt 5 ]]; then
            pass "issue 99's worktree.sh completed in ${ELAPSED}s while issue 98's hook was still sleeping (not serialized)"
        else
            echo "--- worktree.sh 99 output (rc=$rc99, elapsed=${ELAPSED}s) ---" >&2
            cat /tmp/wt-99.$$ >&2 2>/dev/null || true
            fail "issue 99's worktree.sh was serialized behind issue 98's slow hook (rc=$rc99, elapsed=${ELAPSED}s, expected <5s)"
        fi

        wait "$PID98" 2>/dev/null
    fi
    set -e
    rm -f "$MARKER" /tmp/wt-98.$$ /tmp/wt-99.$$
)
cleanup_repo "$REPO"

# --- Test 9: only a MARKED answer from `loom-daemon worktree-lock acquire` is trusted (#8195 slice 7) ---
echo ""
echo "Test 9: a daemon answer is trusted only on its stdout marker, never on the exit code alone"
REPO=$(setup_repo)
(
    cd "$REPO"

    # Same extraction as Test 7, but this time $_WT_DAEMON_BIN is SET, to each
    # of the shapes a resolved `loom-daemon` can actually turn out to be. The
    # property under test is the one #6017 makes expensive to get wrong: the
    # wrapper must never report a lock it does not hold, and must never hand a
    # release to an implementation that did not mint the token.
    #
    # Exit 1 with nothing on stdout is the shape that motivated this: a
    # DIFFERENT binary named loom-daemon (found for real in another suite's
    # fixture — test-worktree-forge-pr-check.sh's Test 8) reuses exit 1 for "I
    # do not understand this subcommand", and clap does the same on a daemon
    # predating the worktree-lock family. Read as a refusal, worktree.sh would
    # abort a perfectly valid worktree creation; read as an acquisition, two
    # `git worktree add` calls would run concurrently. It must be neither: it
    # must fall through to the shell body, which is what the real refusal's
    # mandatory `HOLDER_PID=` marker makes distinguishable.
    FUNCS_FILE=$(extract_lock_functions)
    # shellcheck disable=SC2034
    LOOM_WORKTREE_LOCK_TIMEOUT=5
    # shellcheck disable=SC2034
    LOOM_WORKTREE_LOCK_POLL_INTERVAL=1
    # shellcheck disable=SC2034
    JSON_OUTPUT=""
    print_warning() { :; }
    # shellcheck disable=SC1090
    source "$FUNCS_FILE"
    rm -f "$FUNCS_FILE"

    FAKES=$(mktemp -d /tmp/loom-wt-fakebin.XXXXXX)
    printf '#!/bin/sh\nexit 1\n'                              > "$FAKES/silent-1"
    printf '#!/bin/sh\necho "unrecognized" >&2\nexit 1\n'     > "$FAKES/noisy-1"
    printf '#!/bin/sh\necho "lock dir boom" >&2\nexit 2\n'    > "$FAKES/unusable-2"
    printf '#!/bin/sh\nexit 0\n'                              > "$FAKES/silent-0"
    chmod +x "$FAKES"/*

    for shape in silent-1 noisy-1 unusable-2 silent-0; do
        _WT_DAEMON_BIN="$FAKES/$shape"
        if acquire_worktree_lock 300 && [[ "$_WT_LOCK_DELEGATED" != "true" ]] \
            && [[ -d "$(_worktree_lock_path 300)" ]]; then
            pass "'$shape' is not an answer: fell through to the shell lock and took it for real"
        else
            fail "'$shape' was mistaken for a daemon answer (delegated=$_WT_LOCK_DELEGATED, dir=$([[ -d "$(_worktree_lock_path 300)" ]] && echo yes || echo no))"
        fi
        # Release must go back to the shell body that minted the token — a
        # delegated release here would hand the token to a binary that has
        # never seen it and leave the real lock dir behind forever.
        release_worktree_lock 300 "$WORKTREE_LOCK_TOKEN"
        if [[ ! -d "$(_worktree_lock_path 300)" ]]; then
            pass "'$shape': the shell body that minted the token also released it"
        else
            fail "'$shape': the lock survived its own release"
        fi
    done

    # And the real binary IS trusted, on both answers: a marked acquisition,
    # then a marked refusal while it is still held.
    _WT_DAEMON_BIN="${LOOM_DAEMON_SELF_BIN:-}"
    if [[ -z "$_WT_DAEMON_BIN" ]]; then
        fail "the require-daemon-bin harness pinned no binary — this suite requires a built one"
    else
        if acquire_worktree_lock 301 && [[ "$_WT_LOCK_DELEGATED" == "true" ]] \
            && [[ -n "$WORKTREE_LOCK_TOKEN" ]] && [[ -d "$(_worktree_lock_path 301)" ]]; then
            pass "a real 'TOKEN=' answer is trusted and the daemon holds the same lock dir the shell would"
        else
            fail "a real acquisition was not trusted (delegated=$_WT_LOCK_DELEGATED)"
        fi
        REAL_TOKEN="$WORKTREE_LOCK_TOKEN"
        if acquire_worktree_lock 302; then
            fail "a second acquisition against a held lock did not refuse"
        else
            if [[ "$_WT_LOCK_DELEGATED" == "true" ]]; then
                pass "a real 'HOLDER_PID=' answer is trusted as a refusal, not retried in shell"
            else
                fail "a real refusal fell through to the shell body instead of being trusted"
            fi
        fi
        release_worktree_lock 301 "$REAL_TOKEN"
        if [[ ! -d "$(_worktree_lock_path 301)" ]]; then
            pass "the delegated release returned the token to the daemon that minted it"
        else
            fail "the delegated release left the lock dir behind"
        fi
    fi
    rm -rf "$FAKES"
)
cleanup_repo "$REPO"

# --- Summary ---
read -r TESTS_RUN TESTS_PASSED TESTS_FAILED < "$COUNTER_FILE"
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
