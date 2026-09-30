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
#
# Moved out of shell-suite-tests by #8195 slice 7: Test 3 ("auto-recovery
# retry path") exercises `_handle_feature_branch_in_main_worktree`, now
# `loom-daemon worktree-branch-conflict`. Success there requires the recovery
# to actually run — unlike slice 4/5's best-effort links/cleanup — so this
# suite pins the binary via loom_test_require_daemon_bin and FAILS rather than
# skips without one; the no-binary fallback (recovery_code=1, reporting git's
# raw error) is a correct degradation but not what this suite measures.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "worktree-branch-conflict"

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

# --- Test 6: an un-ported daemon degrades to "not this error", never to a
# ---         claimed recovery (#8195 slice 7)
#
# `_worktree_handle_branch_conflict` probes `worktree-branch-conflict --help`
# before trusting the subcommand's exit code, because clap answers an UNKNOWN
# subcommand with exit 2 — and 2 is an ANSWER in this contract ("I switched
# your main worktree back to the default branch, retry the add"). Without the
# probe, a daemon predating the port claims a recovery it never performed, on
# every failing `git worktree add`, and worktree.sh prints "Retrying worktree
# creation..." over git's own accurate error.
#
# This test drives that exact shape with a stub standing in for an un-ported
# daemon: a binary that exits 2 for everything, which is precisely what an old
# `loom-daemon` does here. Deleting the probe from worktree.sh makes it fail.
echo ""
echo "Test 6: a daemon without the worktree-branch-conflict subcommand does not fake a recovery"
REPO=$(setup_repo unported)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
OLD_DAEMON=$(mktemp /tmp/loom-wtjson-olddaemon.XXXXXX)
# Every other daemon call site worktree.sh reaches is already best-effort
# (`|| true`) or refuses only on exit 1, so one stub can stand in for the whole
# binary without perturbing them.
cat > "$OLD_DAEMON" <<'STUB'
#!/usr/bin/env bash
# Stands in for a loom-daemon predating #8195 slice 7: clap's unknown-subcommand
# exit code, for any subcommand.
echo "error: unrecognized subcommand '$1'" >&2
exit 2
STUB
chmod +x "$OLD_DAEMON"
(
    cd "$REPO" || exit 1
    git checkout -q -b feature/issue-105
    LOOM_DAEMON_SELF_BIN="$OLD_DAEMON" LOOM_DAEMON_BIN="$OLD_DAEMON" \
        ./.loom/scripts/worktree.sh 105 >"$OUT" 2>&1
) || true
if grep -q "Retrying worktree creation" "$OUT"; then
    fail "un-ported daemon: worktree.sh claimed a recovery that never happened (the probe is missing) — content: $(cat "$OUT")"
else
    pass "un-ported daemon: no phantom 'Retrying worktree creation...' claim"
fi
if grep -q "is already used by worktree at" "$OUT"; then
    pass "un-ported daemon: git's own error text is reported instead"
else
    fail "un-ported daemon: git's raw error was swallowed — content: $(cat "$OUT")"
fi
# The main worktree must still be on the feature branch: nothing switched it.
if [[ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" == "feature/issue-105" ]]; then
    pass "un-ported daemon: main worktree left untouched"
else
    fail "un-ported daemon: main worktree was switched despite no guard being able to run"
fi
rm -f "$OUT" "$OLD_DAEMON"
cleanup_repo "$REPO"

# --- Test 7/8: "worktree already exists" exit-0 paths emit JSON too (#9111) -
#
# Before #9111, `--json N` against an EXISTING worktree that carries real work
# (uncommitted changes, or commits ahead of base) exited 0 with COMPLETELY
# EMPTY stdout — the "preserve existing work" branch never wrote to fd 3, so a
# caller piping into `jq` got a parse error on nothing rather than a document.
# These reuse the newbranch/reusebranch setup: create the worktree once, leave
# real work in it, then run `--json N` again to hit the preserve branch.
#
# assert_worktree_json_shape checks the emitted document's key set against the
# --sparse/--full reconfigure fast path's own shape (worktree.sh's sibling
# "worktree already exists" answer, loom-daemon/src/worktree_cli/sparse.rs's
# `{"success": true, "worktreePath": ..., "branchName": ..., "issueNumber":
# ..., "sparse": ..., "cone": ...}`) — not just "parses", which trivially
# passes on empty input from jq -e's perspective is a parse failure, not a
# pass, so assert_pure_json alone already catches the pre-fix regression; this
# adds the shape check the issue specifically asked for.
assert_worktree_json_shape() {
    local out_file="$1" label="$2"
    local want='["branchName","cone","issueNumber","sparse","success","worktreePath"]'
    local keys
    keys=$(jq -S -c 'keys' "$out_file" 2>/dev/null)
    if [[ "$keys" == "$want" ]]; then
        pass "$label: JSON key set matches the sparse-fast-path shape"
    else
        fail "$label: JSON key set mismatch (got '${keys:-parse-error}', want '$want') — content: $(cat "$out_file")"
    fi
}

echo ""
echo "Test 7: preserve-existing-work path (uncommitted change) produces pure JSON (#9111)"
REPO=$(setup_repo preserveuncommitted)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
(
    cd "$REPO" || exit 1
    ./.loom/scripts/worktree.sh --json 106 >/dev/null 2>&1
    # A tracked-file edit left uncommitted, WITHOUT removing the worktree dir
    # (unlike Test 2's branch-reuse setup) — this is what routes the second
    # run into the registered-worktree "preserve" branch rather than reuse.
    echo "dirty" >> .loom/worktrees/issue-106/.gitignore
    ./.loom/scripts/worktree.sh --json 106 >"$OUT" 2>/dev/null
)
assert_pure_json "$OUT" "preserve-uncommitted"
assert_worktree_json_shape "$OUT" "preserve-uncommitted"
rm -f "$OUT"
cleanup_repo "$REPO"

echo ""
echo "Test 8: preserve-existing-work path (commit ahead of base) produces pure JSON (#9111)"
REPO=$(setup_repo preserveahead)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
(
    cd "$REPO" || exit 1
    ./.loom/scripts/worktree.sh --json 107 >/dev/null 2>&1
    (
        cd .loom/worktrees/issue-107 || exit 1
        git config user.email t@t
        git config user.name t
        echo "work" > work.txt
        git add work.txt
        git commit -q -m "wip"
    )
    ./.loom/scripts/worktree.sh --json 107 >"$OUT" 2>/dev/null
)
assert_pure_json "$OUT" "preserve-ahead"
assert_worktree_json_shape "$OUT" "preserve-ahead"
rm -f "$OUT"
cleanup_repo "$REPO"

echo ""
echo "Test 9: stale-worktree-reset path (no work) produces pure JSON (#9111)"
# A clean worktree with nothing ahead routes the second run into the "stale
# worktree reset" arm, which since #9111 shares the preserve arm's exit.
REPO=$(setup_repo stalereset)
OUT=$(mktemp /tmp/loom-wtjson-out.XXXXXX)
(
    cd "$REPO" || exit 1
    ./.loom/scripts/worktree.sh --json 108 >/dev/null 2>&1
    ./.loom/scripts/worktree.sh --json 108 >"$OUT" 2>/dev/null
)
assert_pure_json "$OUT" "stale-reset"
assert_worktree_json_shape "$OUT" "stale-reset"
rm -f "$OUT"
cleanup_repo "$REPO"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
