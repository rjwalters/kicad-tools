#!/usr/bin/env bash
# test-check-main-clean-build-trees.sh - check-main-clean.sh --quarantine and
# cargo build trees: the #11149 follow-ups to #11075.
#
# A sibling of test-check-main-clean.sh (which holds the #11075 cases and is
# frozen by the file-size ratchet). Verified behavior:
#   - a staged `git mv old.txt <buildtree>/b.o` quarantines cleanly (exit 4):
#     the old.txt deletion is in the stash and the worktree copy is restored;
#     the build tree is neither stashed nor removed from disk (#11149 item 1)
#   - a `loom-daemon stashes build-trees` that prints a directory and then
#     fails yields the loud include-everything fallback EXACTLY: the warning is
#     printed and the printed directory is NOT excluded (#11149 item 2)
#
# Usage:
#   ./.loom/scripts/tests/test-check-main-clean-build-trees.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# #9548: the subject vets its write target (`forge may-write`) first; that
# decision is not what this suite tests (test-write-scope.sh does).
WS_STUB_DIR="$(mktemp -d)"
# shellcheck source=lib/write-scope-stub.sh
source "$SCRIPT_DIR/lib/write-scope-stub.sh"
write_scope_allow_all "$WS_STUB_DIR"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-main-clean.sh"
# --quarantine asks `loom-daemon stashes build-trees` which cargo build trees to
# exclude, so these cases need THIS checkout's build. FATAL, not a skip,
# without one (wired in the "Native Port Suites" job for that reason).
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin --self-only "$HELPERS_DIR" "stashes"

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

# A throwaway git repo with one commit and a tracked source file.
make_repo_with_source() {
    local dir
    dir=$(mktemp -d)
    git -C "$dir" init -q
    git -C "$dir" config user.email t@t.t
    git -C "$dir" config user.name test
    printf '.loom/worktrees/\n.loom/sweep-checkpoint/\n' > "$dir/.gitignore"
    printf 'original tracked content\n' > "$dir/tracked_source.py"
    git -C "$dir" add .gitignore tracked_source.py
    git -C "$dir" commit -q -m init
    echo "$dir"
}

# stash_files <repo> -> every path in stash@{0}'s worktree, index and untracked trees.
stash_files() {
    local r
    for r in 'stash@{0}' 'stash@{0}^2' 'stash@{0}^3'; do
        git -C "$1" ls-tree -r --name-only "$r" 2>/dev/null || true
    done
}

# -------- Test: a staged rename INTO a build tree still rescues the source deletion (#11149) --------
# The build-tree side is unstaged and excluded, which leaves `D  old.txt`. The
# filter must hand that deletion to the stash, not drop it with the rename line
# (which used to fail closed: exit 3 "STILL PRESENT"), and the stash must
# cope with a pathspec whose only change is that staged deletion.
echo "Test 11149a: --quarantine of 'git mv old.txt <buildtree>/b.o' stashes the deletion"
REPO=$(make_repo_with_source)
printf 'old content\n' > "$REPO/old.txt"
git -C "$REPO" add old.txt && git -C "$REPO" commit -qm "add old.txt"
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-11149a.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
mkdir -p "$REPO/target-x/debug"
printf 'Signature: 8a477f597d28d172789f06886806bc55\n' > "$REPO/target-x/CACHEDIR.TAG"
printf 'bin\n' > "$REPO/target-x/debug/artifact.o"
git -C "$REPO" mv old.txt target-x/b.o
out=$( cd "$REPO" && LOOM_QUARANTINE_COMMENT=0 "$SCRIPT" --baseline "$SNAP" --quarantine --label "run=R issue=11149" 2>&1 ); RC=$?
if [[ "$RC" -eq 4 ]]; then pass "rename-into-build-tree quarantine exits 4"; else fail "expected 4, got $RC; out=$out"; fi
STASH_DIFF=$(git -C "$REPO" diff --name-status 'stash@{0}^1' 'stash@{0}' 2>/dev/null || true)
if grep -qx $'D\told.txt' <<<"$STASH_DIFF" && [[ "$(cat "$REPO/old.txt" 2>/dev/null)" == "old content" ]]; then
    pass "the old.txt deletion is in the stash and the worktree copy is restored"
else
    fail "source deletion not rescued; stash diff: $STASH_DIFF; out=$out"
fi
STASH_FILES=$(stash_files "$REPO")
if grep -q '^target-x/' <<<"$STASH_FILES"; then
    fail "build-tree content leaked into refs/stash: $STASH_FILES"
else
    pass "no build-tree blob in refs/stash"
fi
if [[ -f "$REPO/target-x/b.o" && -f "$REPO/target-x/debug/artifact.o" ]]; then
    pass "build-tree content (incl. the rename destination) left on disk"
else
    fail "build-tree content was removed from disk"
fi
rm -rf "${REPO:?}"

# -------- Test: partial daemon output then failure -> include-everything, exactly (#11149) --------
# A daemon that prints some build-tree dirs and then fails leaves bt_ok=0, so
# the fallback warning says nothing is excluded. Those dirs must not be.
# Modelled on test-check-main-clean.sh's Test 11075e stub.
echo "Test 11149b: daemon prints a build-tree dir then fails -> warning AND no exclusion"
STUB_DIR=$(mktemp -d)
printf '#!/usr/bin/env bash\nprintf "target-x\\0"\nexit 1\n' > "$STUB_DIR/loom-daemon"
chmod +x "$STUB_DIR/loom-daemon"
REPO=$(make_repo_with_source)
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-11149b.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
mkdir -p "$REPO/target-x/debug"
printf 'Signature: 8a477f597d28d172789f06886806bc55\n' > "$REPO/target-x/CACHEDIR.TAG"
printf 'bin\n' > "$REPO/target-x/debug/artifact.o"
printf 'def leaked(): pass\n' > "$REPO/leaked_module.py"
out=$( cd "$REPO" && LOOM_QUARANTINE_COMMENT=0 LOOM_DAEMON_SELF_BIN="$STUB_DIR/loom-daemon" \
    "$SCRIPT" --baseline "$SNAP" --quarantine --label "run=R issue=11149" 2>&1 ); RC=$?
STASH_FILES=$(stash_files "$REPO")
if [[ "$RC" -eq 4 ]] && grep -q '^leaked_module.py$' <<<"$STASH_FILES"; then
    pass "partial-output daemon: real work is still quarantined (exit 4)"
else
    fail "partial-output daemon: rescue skipped real work (rc=$RC); stash holds: $STASH_FILES; out=$out"
fi
if grep -q 'WARNING: .*stashes build-trees' <<<"$out" && grep -q 'NOT be excluded' <<<"$out"; then
    pass "partial-output daemon: the fallback warning is printed"
else
    fail "partial-output daemon: fallback was silent; out=$out"
fi
if grep -q '^target-x/' <<<"$STASH_FILES"; then
    pass "partial-output daemon: the printed dir is NOT excluded (warning is exactly true)"
else
    fail "partial-output daemon: partial output still drove an exclusion; stash holds: $STASH_FILES"
fi
rm -rf "${REPO:?}" "${STUB_DIR:?}" "${WS_STUB_DIR:?}"

# -------- Summary --------
echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED/$TESTS_RUN tests passed${NC}"
    exit 0
else
    echo -e "${RED}FAILED: $TESTS_FAILED/$TESTS_RUN tests failed${NC}"
    exit 1
fi
