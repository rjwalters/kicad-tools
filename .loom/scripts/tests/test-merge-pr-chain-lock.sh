#!/usr/bin/env bash
# test-merge-pr-chain-lock.sh - Unit tests for merge-pr.sh's chain-head merge
# lock guard (_check_chain_lock, #10167).
#
# The decision is `loom-daemon merge-pr chain-lock` (Rust,
# loom-daemon/src/merge_pr/chain_lock.rs, with its own unit + stub-gh tests
# for lock live / expired / unreadable / outsider / other base). This suite
# pins the merge-pr.sh-side WIRING against a stub binary driven through
# LOOM_DAEMON_BIN: a live lock exits 6 with the holder on stderr and nothing
# after the guard runs; a clear or expired lock proceeds; the override is
# left to the daemon (which skips its reads); the PR's own base is passed;
# plain hand-merges skip the guard; an older daemon proceeds with a warning;
# --dry-run reports without exiting.
#
# Usage: ./.loom/scripts/tests/test-merge-pr-chain-lock.sh

# shellcheck disable=SC2034
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
ok()   { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo "  PASS: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo "  FAIL: $1"; [[ -z "${2:-}" ]] || echo "    $2"; }
assert_eq() { if [[ "$1" == "$2" ]]; then ok "$3"; else fail "$3" "expected '$1' got '$2'"; fi; }
assert_contains() { if grep -qF -- "$2" <<<"$1"; then ok "$3"; else fail "$3" "missing '$2' in: $1"; fi; }
assert_not_contains() { if grep -qF -- "$2" <<<"$1"; then fail "$3" "unexpected '$2' in: $1"; else ok "$3"; fi; }

warning() { echo "WARN: $*"; }
error()   { echo "ERROR: $*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-chain-lock.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

# The guard is one line ending in its own call; extract the definition only.
FUNCS="$WORK/funcs.sh"
awk '/^_check_chain_lock\(\) \{/ { sub(/; _check_chain_lock$/, ""); print; exit }' "$MERGE_PR_SRC" > "$FUNCS"
grep -q '_check_chain_lock()' "$FUNCS" || { echo "FATAL: could not extract _check_chain_lock" >&2; exit 2; }
# shellcheck disable=SC1090
source "$FUNCS"

# The call must sit BEFORE every write the script can make (the stacked-
# children ref pin, the #8508 re-date, comments, the merge itself).
guard_line="$(awk '/; _check_chain_lock$/ { print NR; exit }' "$MERGE_PR_SRC")"
first_write="$(awk '/^_check_no_open_stacked_children$/ { print NR; exit }' "$MERGE_PR_SRC")"
if [[ -n "$guard_line" && -n "$first_write" && "$guard_line" -lt "$first_write" ]]; then
    ok "guard runs before the first writing step"
else
    fail "guard runs before the first writing step" "guard=$guard_line first_write=$first_write"
fi

# --auto waits for checks before merging; a lock taken during that wait must be
# honored, so the post-wait revalidation re-runs the guard too.
if awk '/^_revalidate_merge_guards\(\) \{/ { inf=1 } inf && /^  _check_chain_lock$/ { found=1 } inf && /^\}/ { exit } END { exit !found }' "$MERGE_PR_SRC"; then
    ok "post-wait revalidation re-runs the guard"
else
    fail "post-wait revalidation re-runs the guard" "_check_chain_lock not called inside _revalidate_merge_guards"
fi

# Stub daemon: records argv, then emits the canned outcome.
make_stub() {
    local mode="$1" path; path="$WORK/daemon-$1"
    {
        echo '#!/usr/bin/env bash'
        echo "printf '%s\n' \"\$*\" >> '$WORK/argv-$mode'"
        case "$mode" in
            clear)    echo "echo LOOM-CHAIN-LOCK-CLEAR" ;;
            held)     echo "echo 'LOOM-CHAIN-LOCK-HELD pr=7 holder=#9 head=1111111 expires=2026-10-05T12:20:00Z'; echo 'chain head #9 holds the lock'; exit 6" ;;
            failopen) echo "echo 'LOOM-CHAIN-LOCK-FAIL-OPEN pr=7 unreadable for a whole cap'" ;;
            override) echo "echo 'LOOM-CHAIN-LOCK-OVERRIDDEN pr=7 LOOM_CHAIN_LOCK_OVERRIDE is set'" ;;
            old)      echo "echo \"error: unrecognized subcommand 'chain-lock'\" >&2; exit 2" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}

FORGE_TYPE=github; PR_NUMBER=7; PR_JSON='{"base":{"ref":"release/1.x"}}'; REPO_NWO=o/r
REPO_ROOT="$WORK"; AUTO_MERGE=true; DRY_RUN=false
LAST_OUT=""; LAST_ERR=""; LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( (_check_chain_lock; echo "REACHED-AFTER-GUARD") 2>"$WORK/stderr")"; LAST_RC=$?
    LAST_ERR="$(cat "$WORK/stderr")"
    set -e
}

echo "Testing _check_chain_lock behavior..."

# T1: live lock on another PR -> exit 6, holder named on stderr, nothing after the guard runs
LOOM_DAEMON_BIN="$(make_stub held)" run_guard
assert_eq 6 "$LAST_RC" "live lock -> exit 6"
assert_contains "$LAST_ERR" "holder=#9" "holder named on stderr"
assert_contains "$LAST_ERR" "Exiting 6" "deferral explained on stderr"
assert_not_contains "$LAST_OUT" "REACHED-AFTER-GUARD" "nothing after the guard runs (no write)"
assert_contains "$(cat "$WORK/argv-held")" "merge-pr chain-lock --pr 7 --repo o/r --base-ref release/1.x --repo-root $WORK" "daemon gets the PR's own base"

# T2: no live lock (or expired) -> proceeds silently
LOOM_DAEMON_BIN="$(make_stub clear)" run_guard
assert_eq 0 "$LAST_RC" "clear/expired lock -> proceeds"
assert_contains "$LAST_OUT" "REACHED-AFTER-GUARD" "merge flow continues"
assert_not_contains "$LAST_OUT" "WARN" "clear is silent"

# T3: override / fail-open -> proceeds, the daemon's note surfaced as a warning
for mode in override failopen; do
    LOOM_DAEMON_BIN="$(make_stub "$mode")" run_guard
    assert_eq 0 "$LAST_RC" "$mode -> proceeds"
    assert_contains "$LAST_OUT" "WARN: LOOM-CHAIN-LOCK-" "$mode is logged"
done

# T4: a plain hand-merge (no --auto, no LOOM_CHAIN_LOCK_GUARD) never consults the lock
rm -f "$WORK/argv-held"
AUTO_MERGE=false LOOM_DAEMON_BIN="$(make_stub held)" run_guard
assert_eq 0 "$LAST_RC" "hand-merge -> not held"
[[ ! -e "$WORK/argv-held" ]] && ok "hand-merge -> daemon never invoked" || fail "hand-merge -> daemon never invoked"
AUTO_MERGE=false LOOM_CHAIN_LOCK_GUARD=1 LOOM_DAEMON_BIN="$(make_stub held)" run_guard
assert_eq 6 "$LAST_RC" "LOOM_CHAIN_LOCK_GUARD=1 opts a hand-merge in"

# T5: Gitea -> no-op
rm -f "$WORK/argv-held"
FORGE_TYPE=gitea LOOM_DAEMON_BIN="$(make_stub held)" run_guard
assert_eq 0 "$LAST_RC" "gitea -> no-op"
[[ ! -e "$WORK/argv-held" ]] && ok "gitea -> daemon never invoked" || fail "gitea -> daemon never invoked"

# T6: an older daemon without the verb (or a missing binary) proceeds with a warning
LOOM_DAEMON_BIN="$(make_stub old)" run_guard
assert_eq 0 "$LAST_RC" "old daemon -> proceeds"
assert_contains "$LAST_OUT" "did not run" "old daemon -> warned"
LOOM_DAEMON_BIN="$WORK/does-not-exist" run_guard
assert_eq 0 "$LAST_RC" "missing binary -> proceeds"

# T7: --dry-run reports the would-defer without exiting
DRY_RUN=true LOOM_DAEMON_BIN="$(make_stub held)" run_guard
assert_eq 0 "$LAST_RC" "--dry-run -> does not exit"
assert_contains "$LAST_OUT" "[dry-run] Would DEFER" "--dry-run reports the would-defer"

# T8: no base in the PR JSON falls back to the default branch
rm -f "$WORK/argv-clear"
PR_JSON='{}' DEFAULT_BRANCH_NAME=trunk LOOM_DAEMON_BIN="$(make_stub clear)" run_guard
assert_contains "$(cat "$WORK/argv-clear")" "--base-ref trunk" "missing base -> default branch"

echo
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
