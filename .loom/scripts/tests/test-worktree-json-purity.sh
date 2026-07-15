#!/usr/bin/env bash
# test-worktree-json-purity.sh — Regression test for #3546 (JSON stdout purity)
#
# `git worktree add` (and `git submodule update`) write some feedback lines to
# *stdout* rather than stderr — e.g. "branch '...' set up to track '...'.",
# "HEAD is now at <sha> <subject>", "Submodule path '...': checked out '...'".
# In `worktree.sh --json` mode those lines used to prefix the emitted JSON
# document, so a consumer piping into `jq` hit `parse error ... line 1` and —
# because the noise preceded the JSON — closed the pipe on the first bad line,
# SIGPIPE-killing the script mid-creation (orphan branch, no registered
# worktree).
#
# The fix (fd-swap contract in worktree.sh): in --json mode the real stdout is
# saved on fd 3 and fd 1 is redirected to stderr, so ONLY the final JSON
# document reaches the caller's stdout; `trap '' PIPE` keeps an early-closing
# consumer from killing the script.
#
# Coverage:
#   1. New-branch path: `--json N` on a fresh issue → stdout is a single clean
#      JSON object (first byte '{', jq -e .success == true, exactly one object),
#      and the git "branch ... set up to track" / "HEAD is now at" noise is
#      absent from stdout (it lands on stderr instead).
#   2. Branch-reuse path: an existing feature/issue-N branch (worktree removed)
#      → `--json N` still produces pure JSON.
#   3. Auto-recovery retry path: feature branch checked out in the (clean) main
#      worktree → the initial `git worktree add` fails, worktree.sh switches
#      main back and retries; stdout is still pure JSON.
#   4. SIGPIPE safety: piping `--json N` into a consumer that closes the pipe
#      after the first line leaves NO orphan branch + missing worktree — the
#      worktree is fully created (branch exists AND registered with git).
#   5. Human mode unchanged: without --json, git progress ("HEAD is now at")
#      and print_* success messages remain visible on the combined output.
#
# Pattern follows test-worktree-root-override.sh: throwaway bare origin + repo
# in a mktemp dir, copy worktree.sh + lib/, run.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

if ! command -v jq >/dev/null 2>&1; then
    echo -e "${YELLOW}SKIP${NC}: jq not available — JSON purity test requires jq"
    exit 0
fi

# Assert a captured stdout file is a single, clean JSON object with success=true.
# $1 = path to captured stdout, $2 = label prefix
assert_pure_json() {
    local out_file="$1" label="$2"

    # (a) First byte must be '{' — no leading git noise.
    local first_byte
    first_byte=$(head -c1 "$out_file")
    if [[ "$first_byte" == "{" ]]; then
        pass "$label: stdout begins with '{' (no leading noise)"
    else
        fail "$label: stdout does not begin with '{' (got '${first_byte}') — content: $(cat "$out_file")"
    fi

    # (b) Exactly one JSON value on stdout (jq -s slurps the whole stream).
    local count
    if count=$(jq -s 'length' "$out_file" 2>/dev/null) && [[ "$count" == "1" ]]; then
        pass "$label: stdout is exactly one JSON value"
    else
        fail "$label: stdout is not a single JSON value (jq -s length='${count:-parse-error}') — content: $(cat "$out_file")"
    fi

    # (c) .success is true.
    if jq -e '.success == true' "$out_file" >/dev/null 2>&1; then
        pass "$label: .success == true"
    else
        fail "$label: jq -e .success != true — content: $(cat "$out_file")"
    fi

    # (d) None of git's stdout feedback lines leaked into stdout.
    if grep -qE "set up to track|HEAD is now at|Preparing worktree" "$out_file"; then
        fail "$label: git stdout feedback leaked into JSON stream — content: $(cat "$out_file")"
    else
        pass "$label: no git stdout feedback leaked into JSON stream"
    fi
}

# Build a throwaway repo with an origin/main ref and the minimal .loom layout.
setup_repo() {
    local name="${1:-jsonrepo}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtjson.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/$name"
    (
        cd "$tmp/$name" || exit 1
        git config user.email t@t
        git config user.name t
        # Gitignore .loom/ (as real Loom repos do) so the copied scripts below
        # don't register as uncommitted changes — the auto-recovery path (Test 3)
        # refuses to auto-switch a dirty main worktree.
        printf '.loom/\n' > .gitignore
        git add .gitignore
        git commit -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin main
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh
    )
    # Return the PHYSICAL path (resolve /tmp -> /private/tmp on macOS). The
    # auto-recovery path in worktree.sh compares the conflicting worktree path
    # (which git reports physically) against `pwd` of the main workspace; if the
    # caller cd's in via a symlinked path those diverge and recovery bails. Real
    # repos don't live under a symlinked /tmp, so pin the physical path here.
    (cd "$tmp/$name" && pwd -P)
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# --- Test 1: new-branch path → pure JSON on stdout ---
echo "Test 1: new-branch path produces pure JSON on stdout"
REPO=$(setup_repo newbranch)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
ERR=$(mktemp /tmp/loom-wtjson-err.XXXXXX)
(
    cd "$REPO" || exit 1
    ./.loom/scripts/worktree.sh --json 100 >"$OUT" 2>"$ERR"
)
assert_pure_json "$OUT" "new-branch"
assert_dir_exists() { [[ -d "$1" ]] && pass "$2" || fail "$2"; }
assert_dir_exists "$REPO/.loom/worktrees/issue-100" "new-branch: worktree directory created"
rm -f "$OUT" "$ERR"
cleanup_repo "$REPO"

# --- Test 2: branch-reuse path → pure JSON on stdout ---
echo ""
echo "Test 2: branch-reuse path (existing branch, worktree removed) produces pure JSON"
REPO=$(setup_repo reusebranch)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
(
    cd "$REPO" || exit 1
    # First create the worktree (new branch), then remove the worktree dir while
    # KEEPING the feature/issue-101 branch so the second run hits the reuse path.
    ./.loom/scripts/worktree.sh --json 101 >/dev/null 2>&1
    git worktree remove --force .loom/worktrees/issue-101 >/dev/null 2>&1
    # Sanity: branch still present, dir gone.
    git show-ref --verify --quiet refs/heads/feature/issue-101
    ./.loom/scripts/worktree.sh --json 101 >"$OUT" 2>/dev/null
)
assert_pure_json "$OUT" "branch-reuse"
rm -f "$OUT"
cleanup_repo "$REPO"

# --- Test 3: auto-recovery retry path → pure JSON on stdout ---
echo ""
echo "Test 3: auto-recovery retry (feature branch in clean main worktree) produces pure JSON"
REPO=$(setup_repo recovery)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
(
    cd "$REPO" || exit 1
    # Check the feature branch out in the main worktree (clean tree). The first
    # `git worktree add` will fail with "is already used by worktree at ...";
    # worktree.sh switches the main worktree back to main and retries.
    git checkout -q -b feature/issue-102
    ./.loom/scripts/worktree.sh --json 102 >"$OUT" 2>/dev/null
)
assert_pure_json "$OUT" "auto-recovery"
rm -f "$OUT"
cleanup_repo "$REPO"

# --- Test 4: SIGPIPE safety — early-closing consumer leaves no orphan state ---
echo ""
echo "Test 4: consumer closing the pipe early does not leave an orphan branch"
REPO=$(setup_repo sigpipe)
(
    cd "$REPO" || exit 1
    # Pipe into a consumer that reads a single line and exits, closing the pipe.
    # Pre-fix this SIGPIPE-killed worktree.sh between branch creation and worktree
    # registration. `trap '' PIPE` + fd-swap must let it finish cleanly.
    PARSED=$(./.loom/scripts/worktree.sh --json 103 2>/dev/null | head -n1)
    # The consumer must have received a clean JSON object.
    echo "$PARSED" | jq -e '.success == true' >/dev/null 2>&1 && echo "CONSUMER_OK" >/tmp/loom-sigpipe-$$ || true
)
if [[ -f "/tmp/loom-sigpipe-$$" ]]; then
    pass "SIGPIPE: early-closing consumer still received a clean JSON object"
    rm -f "/tmp/loom-sigpipe-$$"
else
    fail "SIGPIPE: consumer did not receive a clean JSON object"
fi
# The load-bearing assertion: no orphan branch without a registered worktree.
if git -C "$REPO" show-ref --verify --quiet refs/heads/feature/issue-103; then
    if git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -qF "issue-103"; then
        pass "SIGPIPE: branch AND registered worktree both exist (no orphan/partial state)"
    else
        fail "SIGPIPE: branch exists but worktree not registered (orphan branch — the #3546 bug)"
    fi
else
    fail "SIGPIPE: feature/issue-103 branch was not created at all"
fi
cleanup_repo "$REPO"

# --- Test 5: human mode unchanged (git progress + success message visible) ---
echo ""
echo "Test 5: human mode still shows git progress and success message"
REPO=$(setup_repo humanmode)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
(
    cd "$REPO" || exit 1
    # Combine stdout+stderr the way a human at a terminal sees it.
    ./.loom/scripts/worktree.sh 104 >"$OUT" 2>&1
)
if grep -q "Worktree created successfully" "$OUT"; then
    pass "human mode: success message present"
else
    fail "human mode: success message missing — content: $(cat "$OUT")"
fi
if grep -qE "HEAD is now at|set up to track" "$OUT"; then
    pass "human mode: git progress still visible (not suppressed)"
else
    fail "human mode: git progress unexpectedly missing — content: $(cat "$OUT")"
fi
# In human mode there is no JSON document at all.
if grep -q '"success":' "$OUT"; then
    fail "human mode: unexpected JSON document in output"
else
    pass "human mode: no JSON document emitted"
fi
rm -f "$OUT"
cleanup_repo "$REPO"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
