#!/usr/bin/env bash
# test-blame-issue.sh - Tests for blame-issue.sh (#4338, codecast borrow item 1)
#
# Style matches test-check-main-freshness.sh / test-disk-headroom.sh: a
# throwaway `mktemp -d` synthetic git repo fixture, hand-rolled assertions, no
# bats. blame-issue.sh's PR/issue/role resolution shells out to `gh`, so this
# harness stubs a fake `gh` binary on PATH that answers the exact `gh pr view`
# / `gh api .../pulls` / `gh api .../timeline` invocations the script makes,
# keyed off a synthetic PR number embedded in each fixture commit's subject
# (either the squash "(#NNN)" suffix or the "Merge pull request #NNN from ..."
# merge-commit subject -- #9105 made merge commits Loom's default, so both
# forms must resolve offline) -- no network access required.
#
# Usage:
#   ./defaults/scripts/tests/test-blame-issue.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BLAME_SCRIPT="$SCRIPTS_DIR/blame-issue.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (expected substring '$needle' in: $haystack)"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (unexpected substring '$needle' in: $haystack)"
    fi
}

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

if [[ ! -x "$BLAME_SCRIPT" ]]; then
    echo -e "${RED}FATAL${NC}: $BLAME_SCRIPT not found or not executable" >&2
    exit 1
fi

# Scratch area for fixtures + fake gh -- cleaned on exit.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-blame-issue.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# ---- Fake `gh` stub ---------------------------------------------------------
# Answers exactly the invocations blame-issue.sh makes, keyed off a synthetic
# PR number: PR 101 = builder-only cycle (issue 5001, no changes-requested);
# PR 102 = builder+doctor cycle (issue 5002, changes-requested was applied).
FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/gh" <<'FAKEGH'
#!/usr/bin/env bash
case "$1" in
    auth)
        exit 0
        ;;
    pr)
        if [[ "$2" == "view" ]]; then
            pr="$3"
            case "$pr" in
                101) echo "5001" ;;
                102) echo "5002" ;;
                103) echo "5003" ;;
                *) echo "" ;;
            esac
        fi
        ;;
    api)
        path="$2"
        if [[ "$path" == *"/timeline" ]]; then
            pr="${path#*/issues/}"
            pr="${pr%/timeline}"
            case "$pr" in
                101) printf 'loom:review-requested\nloom:pr\n' ;;
                102) printf 'loom:review-requested\nloom:changes-requested\nloom:pr\n' ;;
                103) printf 'loom:review-requested\nloom:pr\n' ;;
            esac
        elif [[ "$path" == *"/pulls" ]]; then
            # commit -> associated PRs online fallback. Every resolvable fixture
            # commit's subject resolves the PR offline, so when $PULLS_LOG is
            # set we record any call there to assert the fallback stayed unused
            # for those subjects (#9105).
            [[ -n "${PULLS_LOG:-}" ]] && echo "$path" >> "$PULLS_LOG"
            echo ""
        fi
        ;;
esac
exit 0
FAKEGH
chmod +x "$FAKE_BIN/gh"

# ---- Fixture repo -----------------------------------------------------------
REPO="$WORKDIR/repo"
git init --quiet "$REPO"
{
    echo "line one"
} > "$REPO/file.txt"
git -C "$REPO" add file.txt
git -C "$REPO" commit --quiet -m "feat: add line one (#101)"

{
    echo "line one"
    echo "line two"
} > "$REPO/file.txt"
git -C "$REPO" add file.txt
git -C "$REPO" commit --quiet -m "fix: add line two (#102)"

{
    echo "line one"
    echo "line two"
    echo "line three (no PR suffix)"
} > "$REPO/file.txt"
git -C "$REPO" add file.txt
git -C "$REPO" commit --quiet -m "direct commit, no PR reference"

{
    echo "line one"
    echo "line two"
    echo "line three (no PR suffix)"
    echo "line four (merge-commit subject)"
} > "$REPO/file.txt"
git -C "$REPO" add file.txt
git -C "$REPO" commit --quiet -m "Merge pull request #103 from feature/merge-style-9105"

# A subject that merely *quotes* a merge subject must not resolve to the quoted
# PR: the merge pattern is `^`-anchored, so this falls through to the online
# commit->pulls fallback (which the stub answers empty) instead.
{
    echo "line one"
    echo "line two"
    echo "line three (no PR suffix)"
    echo "line four (merge-commit subject)"
    echo "line five (quoted merge subject)"
} > "$REPO/file.txt"
git -C "$REPO" add file.txt
git -C "$REPO" commit --quiet -m 'Revert "Merge pull request #104 from feature/oops"'

run_blame() {
    ( cd "$REPO" && PATH="$FAKE_BIN:$PATH" "$BLAME_SCRIPT" "$@" )
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$BLAME_SCRIPT" ]]; then
    pass "blame-issue.sh is executable"
else
    fail "blame-issue.sh is missing or not executable"
fi

# -------- Test 2: --help prints usage and exits 0 --------
echo "Test 2: --help prints usage and exits 0"
help_out="$("$BLAME_SCRIPT" --help 2>&1)"
rc=$?
assert_eq "$rc" "0" "--help exits 0"
assert_contains "Usage" "$help_out" "--help mentions Usage"

# -------- Test 3: blame mode resolves PR/issue/role from commit subject --------
echo "Test 3: blame mode resolves PR 101 (builder-only)"
out="$(run_blame file.txt)"
rc=$?
assert_eq "$rc" "0" "blame mode exits 0"
assert_contains "101" "$out" "resolves PR 101 from commit subject suffix"
assert_contains "5001" "$out" "resolves issue 5001 for PR 101"
assert_contains "builder" "$out" "reports builder role for PR 101 (no changes-requested)"

echo "Test 4: blame mode resolves PR 102 (builder+doctor)"
assert_contains "102" "$out" "resolves PR 102 from commit subject suffix"
assert_contains "5002" "$out" "resolves issue 5002 for PR 102"
assert_contains "builder+doctor" "$out" "reports builder+doctor role for PR 102 (changes-requested applied)"

echo "Test 5: direct commit with no PR suffix falls back gracefully"
assert_contains "direct commit, no PR reference" "$out" "direct commit subject is present in output"

# -------- Test 6: --no-role skips role lookup --------
echo "Test 6: --no-role marks role unknown without querying gh"
out_norole="$(run_blame --no-role file.txt)"
assert_contains "unknown" "$out_norole" "--no-role reports role=unknown"
assert_not_contains "builder+doctor" "$out_norole" "--no-role never resolves builder+doctor"

# -------- Test 7: --pattern mode uses git log -S --------
echo "Test 7: --pattern mode finds commits touching a string"
out_pattern="$(run_blame --pattern "line two" file.txt)"
assert_contains "102" "$out_pattern" "--pattern mode finds the commit that introduced 'line two'"

# -------- Test 8: --format json emits parseable-looking JSON --------
echo "Test 8: --format json emits JSON lines"
out_json="$(run_blame --format json -L 1,1 file.txt)"
assert_contains '"pr":"101"' "$out_json" "--format json includes pr field"
assert_contains '"commit":"' "$out_json" "--format json includes commit field"

# -------- Test 9: missing path -> exit 2 --------
echo "Test 9: missing path exits 2 with an error"
rc=0
err_out="$( (cd "$REPO" && "$BLAME_SCRIPT" does-not-exist.txt) 2>&1 )" || rc=$?
assert_eq "$rc" "2" "missing path exits 2"
assert_contains "not found" "$err_out" "missing path prints a 'not found' error"

# -------- Test 10: outside a git repo -> exit 2 --------
echo "Test 10: outside a git repo exits 2"
NONREPO="$WORKDIR/not-a-repo"
mkdir -p "$NONREPO"
touch "$NONREPO/f.txt"
rc=0
( cd "$NONREPO" && "$BLAME_SCRIPT" f.txt ) >/dev/null 2>&1 || rc=$?
assert_eq "$rc" "2" "non-git dir exits 2"

# -------- Test 11: bad --format value -> exit 2 --------
echo "Test 11: bad --format value exits 2"
rc=0
( cd "$REPO" && "$BLAME_SCRIPT" --format xml file.txt ) >/dev/null 2>&1 || rc=$?
assert_eq "$rc" "2" "bad --format exits 2"

# -------- Test 12: read-only -- no forge writes, no new state on disk --------
echo "Test 12: read-only -- fixture repo has no new untracked files after running"
status_before="$(git -C "$REPO" status --porcelain)"
run_blame file.txt >/dev/null
status_after="$(git -C "$REPO" status --porcelain)"
assert_eq "$status_after" "$status_before" "repo working tree unchanged after blame-issue.sh runs"

# -------- Test 13: merge-commit subjects resolve PR offline (#9105) --------
echo "Test 13: merge-commit subject resolves PR 103 without the online fallback"
rm -f "$WORKDIR/pulls.log"
out_merge="$(PULLS_LOG="$WORKDIR/pulls.log" run_blame -L 4,4 file.txt)"
assert_contains "103" "$out_merge" "resolves PR 103 from merge-commit subject"
assert_contains "5003" "$out_merge" "resolves issue 5003 for PR 103"
assert_contains "builder" "$out_merge" "reports builder role for PR 103"
if [[ -s "${PULLS_LOG:-$WORKDIR/pulls.log}" ]]; then
    fail "online commit->pulls fallback must not be used for a merge-commit subject"
else
    pass "merge-commit subject resolved fully offline (no pulls fallback call)"
fi

# -------- Test 14: a quoted merge subject does NOT resolve offline (#9105) ----
echo "Test 14: 'Revert \"Merge pull request #104 ...\"' does not resolve to PR 104"
rm -f "$WORKDIR/pulls.log"
# JSON form, so the assertion reads the pr field and not the echoed subject.
out_quoted="$(PULLS_LOG="$WORKDIR/pulls.log" run_blame --format json -L 5,5 file.txt)"
assert_not_contains '"pr":"104"' "$out_quoted" "quoted merge subject is not mistaken for PR 104"
if [[ -s "${PULLS_LOG:-$WORKDIR/pulls.log}" ]]; then
    pass "quoted merge subject fell through to the online commit->pulls fallback"
else
    fail "quoted merge subject should have fallen through to the commit->pulls fallback"
fi

# -------- Summary --------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "${RED}FAILED${NC}: $TESTS_FAILED test(s) failed"
    exit 1
fi
echo -e "${GREEN}OK${NC}: all tests passed"
exit 0
