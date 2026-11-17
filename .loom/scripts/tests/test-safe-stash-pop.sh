#!/usr/bin/env bash
# test-safe-stash-pop.sh — tests for safe-stash-pop.sh (#6501).
#
# safe-stash-pop.sh is a data-safety mechanism that runs against the PRIMARY
# checkout, so its state machine is exercised exhaustively here rather than
# smoke-tested. Every case builds a throwaway git repo under a mktemp dir; the
# real repo is never touched.
#
# Coverage:
#   Preconditions      — non-repo, no stash stack, missing entry, mid-merge,
#                        pre-existing unmerged index, --dry-run
#   Clean-pop branch   — entry consumed, content applied, snapshot ref cleaned
#                        up, --index split preserved, -u payload restored,
#                        pre-existing conflict markers NOT treated as a conflict
#   Conflict branch    — the #6499/#6502 incident shape (a tracked
#                        .loom/config.json left with live conflict markers by a
#                        raw `git stash pop`), rollback + verification, stash
#                        preservation, unrelated WIP survival, --no-restore
#   Reporting          — --json line shape per branch
#
# Usage:
#   ./defaults/scripts/tests/test-safe-stash-pop.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$SCRIPTS_DIR/safe-stash-pop.sh"

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
    [[ $# -ge 2 ]] && echo "        $2"
}

assert_eq() { # desc expected actual
    if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1" "expected [$2], got [$3]"; fi
}

assert_contains() { # desc haystack needle
    case "$2" in *"$3"*) pass "$1" ;; *) fail "$1" "missing [$3] in: $2" ;; esac
}

assert_not_contains() { # desc haystack needle
    case "$2" in *"$3"*) fail "$1" "unexpected [$3] in: $2" ;; *) pass "$1" ;; esac
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-safe-stash-pop.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# git needs an identity in a clean CI environment.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"
# Keep a caller's ambient git config from changing merge/stash behaviour.
export GIT_CONFIG_NOSYSTEM=1
export HOME="$WORKDIR/home"
mkdir -p "$HOME"

REPO="$WORKDIR/repo"

# Fresh single-commit repo with f.txt.
mk_repo() {
    rm -rf "$REPO"
    git init --quiet "$REPO"
    git -C "$REPO" checkout -q -b main
    printf 'line1\nline2\nline3\n' > "$REPO/f.txt"
    git -C "$REPO" add f.txt
    git -C "$REPO" commit -q -m c1
}

# Build the guaranteed-conflicting setup on <path>: the stash entry and HEAD
# both rewrite the same line, so a 3-way merge cannot auto-resolve.
# $1 = path relative to repo, $2 = stashed content, $3 = new HEAD content
stage_conflict() {
    local rel="$1" stashed="$2" head_content="$3"
    printf '%s' "$stashed" > "$REPO/$rel"
    git -C "$REPO" stash push -q -m "wip"
    printf '%s' "$head_content" > "$REPO/$rel"
    git -C "$REPO" add "$rel"
    git -C "$REPO" commit -q -m "upstream change"
}

has_markers() { # file
    grep -q '^<<<<<<< ' "$1" 2>/dev/null && grep -q '^>>>>>>> ' "$1" 2>/dev/null
}

run_script() { # args... ; sets RC and OUT (stdout) / ERR (stderr)
    OUT="$("$SCRIPT" "$@" 2>"$WORKDIR/stderr")"
    RC=$?
    ERR="$(cat "$WORKDIR/stderr" 2>/dev/null)"
}

# ---------------------------------------------------------------------------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "safe-stash-pop.sh is executable"
else
    fail "safe-stash-pop.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# ---------------------------------------------------------------------------
echo "Test 2: --help exits 0 and documents the exit codes"
run_script --help
assert_eq "--help exits 0" "0" "$RC"
assert_contains "--help documents the contract" "$OUT" "EXIT CODES"

# ---------------------------------------------------------------------------
echo "Test 3: outside a git repository -> exit 1 (precondition)"
mkdir -p "$WORKDIR/not-a-repo"
run_script --repo "$WORKDIR/not-a-repo"
assert_eq "non-repo exits 1" "1" "$RC"
assert_contains "non-repo names the problem" "$ERR" "Not a git repository"

# ---------------------------------------------------------------------------
echo "Test 4: no stash stack -> exit 2 (nothing to do, not an error)"
mk_repo
run_script --repo "$REPO"
assert_eq "empty stash stack exits 2" "2" "$RC"

# ---------------------------------------------------------------------------
echo "Test 5: missing entry at an explicit ref -> exit 2"
mk_repo
printf 'line1\nline2\nline3\nwip\n' > "$REPO/f.txt"
git -C "$REPO" stash push -q -m wip
run_script --repo "$REPO" 'stash@{7}'
assert_eq "missing stash@{7} exits 2" "2" "$RC"
assert_eq "the real entry is untouched" "1" "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"

# ---------------------------------------------------------------------------
echo "Test 6: clean pop -> exit 0, entry consumed, content applied"
mk_repo
printf 'line1\nline2\nline3\nwip\n' > "$REPO/f.txt"
git -C "$REPO" stash push -q -m wip
run_script --repo "$REPO"
assert_eq "clean pop exits 0" "0" "$RC"
assert_contains "clean pop content restored" "$(cat "$REPO/f.txt")" "wip"
assert_eq "clean pop consumed the entry" "0" "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"
assert_eq "clean pop leaves no snapshot ref behind" "" \
    "$(git -C "$REPO" for-each-ref --format='%(refname)' refs/loom/safe-stash-pop 2>/dev/null)"

# ---------------------------------------------------------------------------
echo "Test 7: mid-merge state -> exit 1, refuses to pop"
mk_repo
printf 'line1\nline2\nline3\nwip\n' > "$REPO/f.txt"
git -C "$REPO" stash push -q -m wip
git_dir="$(git -C "$REPO" rev-parse --absolute-git-dir)"
git -C "$REPO" rev-parse HEAD > "$git_dir/MERGE_HEAD"
run_script --repo "$REPO"
assert_eq "mid-merge exits 1" "1" "$RC"
assert_contains "mid-merge names the state" "$ERR" "mid-operation"
assert_eq "mid-merge did not pop" "1" "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"
rm -f "$git_dir/MERGE_HEAD"

# ---------------------------------------------------------------------------
echo "Test 8: pre-existing unmerged index -> exit 1, refuses to pop on top"
mk_repo
# Manufacture an unmerged index entry for f.txt without an in-progress merge.
blob_a="$(printf 'A\n' | git -C "$REPO" hash-object -w --stdin)"
blob_b="$(printf 'B\n' | git -C "$REPO" hash-object -w --stdin)"
git -C "$REPO" rm -q --cached f.txt >/dev/null
{
    printf '100644 %s 2\tf.txt\n' "$blob_a"
    printf '100644 %s 3\tf.txt\n' "$blob_b"
} | git -C "$REPO" update-index --index-info
printf 'x\n' > "$REPO/g.txt"
git -C "$REPO" stash push -q -u -m wip 2>/dev/null || true
run_script --repo "$REPO"
assert_eq "pre-existing conflict state exits 1" "1" "$RC"
assert_contains "pre-existing conflict state is named" "$ERR" "unmerged index entries"

# ---------------------------------------------------------------------------
echo "Test 9: --dry-run mutates nothing"
mk_repo
printf 'line1\nline2\nline3\nwip\n' > "$REPO/f.txt"
git -C "$REPO" stash push -q -m wip
before="$(git -C "$REPO" rev-parse 'stash@{0}')"
run_script --repo "$REPO" --dry-run
assert_eq "--dry-run exits 0" "0" "$RC"
assert_eq "--dry-run left the entry in place" "$before" "$(git -C "$REPO" rev-parse 'stash@{0}')"
assert_eq "--dry-run left the tree clean" "" "$(git -C "$REPO" status --porcelain)"

# ---------------------------------------------------------------------------
echo "Test 10: THE INCIDENT SHAPE (#6499/#6502) — a conflicting pop must never"
echo "         leave conflict markers in a tracked .loom/config.json"
mk_repo
mkdir -p "$REPO/.loom"
printf '{\n  "safehouse": {"socket": "/committed/loom.sock"}\n}\n' > "$REPO/.loom/config.json"
git -C "$REPO" add .loom/config.json
git -C "$REPO" commit -q -m "add config"

# Control: what a RAW `git stash pop` does in this exact situation.
control="$WORKDIR/control"
rm -rf "$control"
cp -R "$REPO" "$control"
printf '{\n  "safehouse": {"socket": "/host/patched.sock"}\n}\n' > "$control/.loom/config.json"
git -C "$control" stash push -q -m wip
printf '{\n  "safehouse": {"socket": "/upstream/new.sock"}\n}\n' > "$control/.loom/config.json"
git -C "$control" add .loom/config.json
git -C "$control" commit -q -m upstream
git -C "$control" stash pop >/dev/null 2>&1 || true
if has_markers "$control/.loom/config.json"; then
    pass "control: a raw 'git stash pop' DOES leave conflict markers (incident reproduced)"
else
    fail "control: raw pop did not conflict — the fixture no longer reproduces the incident"
fi

# Now the same situation through the wrapper.
stage_conflict ".loom/config.json" \
    '{
  "safehouse": {"socket": "/host/patched.sock"}
}
' \
    '{
  "safehouse": {"socket": "/upstream/new.sock"}
}
'
run_script --repo "$REPO"
assert_eq "incident shape exits 3 (conflict, rolled back)" "3" "$RC"
if has_markers "$REPO/.loom/config.json"; then
    fail "wrapper left conflict markers in .loom/config.json"
else
    pass "wrapper left NO conflict markers in .loom/config.json"
fi
assert_eq "config.json matches its committed HEAD content" \
    "$(git -C "$REPO" show 'HEAD:.loom/config.json')" "$(cat "$REPO/.loom/config.json")"
assert_eq "the stash entry is PRESERVED (nothing discarded)" "1" \
    "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"
assert_eq "no unmerged index entries remain" "" "$(git -C "$REPO" ls-files --unmerged)"
assert_eq "the working tree is clean again" "" "$(git -C "$REPO" status --porcelain)"
if command -v jq >/dev/null 2>&1; then
    if jq -e . "$REPO/.loom/config.json" >/dev/null 2>&1; then
        pass "config.json still parses as JSON after the conflicting pop"
    else
        fail "config.json does not parse as JSON after the conflicting pop"
    fi
fi
assert_contains "conflict report names the affected file" "$ERR" ".loom/config.json"

# ---------------------------------------------------------------------------
echo "Test 11: rollback preserves UNRELATED pre-pop WIP"
mk_repo
printf 'wip-in-progress\n' > "$REPO/other.txt"
git -C "$REPO" add other.txt
git -C "$REPO" commit -q -m "add other"
# stash a conflicting f.txt edit, then move HEAD's f.txt under it
printf 'STASHED\nline2\nline3\n' > "$REPO/f.txt"
git -C "$REPO" stash push -q -m wip
printf 'UPSTREAM\nline2\nline3\n' > "$REPO/f.txt"
git -C "$REPO" add f.txt
git -C "$REPO" commit -q -m upstream
# now create unrelated WIP that the rollback must NOT destroy
printf 'wip-in-progress\nmy-uncommitted-edit\n' > "$REPO/other.txt"
run_script --repo "$REPO"
assert_eq "conflicting pop with pre-existing WIP exits 3" "3" "$RC"
assert_contains "unrelated WIP survived the rollback" "$(cat "$REPO/other.txt")" "my-uncommitted-edit"
assert_eq "f.txt is back to HEAD" "$(git -C "$REPO" show HEAD:f.txt)" "$(cat "$REPO/f.txt")"
assert_eq "stash preserved alongside the surviving WIP" "1" \
    "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"
assert_contains "snapshot ref preserved as insurance" \
    "$(git -C "$REPO" for-each-ref --format='%(refname)' refs/loom/safe-stash-pop)" \
    "refs/loom/safe-stash-pop/"

# ---------------------------------------------------------------------------
echo "Test 12: --no-restore leaves the conflict in place deliberately (exit 5)"
mk_repo
stage_conflict "f.txt" 'STASHED\nline2\nline3\n' 'UPSTREAM\nline2\nline3\n'
run_script --repo "$REPO" --no-restore
assert_eq "--no-restore exits 5" "5" "$RC"
if has_markers "$REPO/f.txt"; then
    pass "--no-restore keeps the conflicted tree for manual resolution"
else
    fail "--no-restore unexpectedly rolled back"
fi
assert_eq "--no-restore preserves the stash entry" "1" \
    "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"

# ---------------------------------------------------------------------------
echo "Test 13: --index reinstates the staged/unstaged split on a clean pop"
mk_repo
printf 'line1\nline2\nline3\nstaged\n' > "$REPO/f.txt"
git -C "$REPO" add f.txt
printf 'line1\nline2\nline3\nstaged\nunstaged\n' > "$REPO/f.txt"
git -C "$REPO" stash push -q -m wip
run_script --repo "$REPO" --index
assert_eq "--index clean pop exits 0" "0" "$RC"
assert_contains "--index restored the staged half" \
    "$(git -C "$REPO" diff --cached --name-only)" "f.txt"
assert_contains "--index restored the unstaged half" \
    "$(git -C "$REPO" diff --name-only)" "f.txt"

# ---------------------------------------------------------------------------
echo "Test 14: a stash carrying pre-existing conflict markers is NOT a conflict"
# A tracked file whose CONTENT legitimately contains marker-shaped lines (this
# repo's own guard fixtures do) must pop cleanly — the scan compares against the
# pre-pop snapshot, so only NEWLY introduced markers count.
mk_repo
{
    printf 'intro\n'
    printf '<<<<<<< Updated upstream\n'
    printf 'a\n'
    printf '=======\n'
    printf 'b\n'
    printf '>>>>>>> Stashed changes\n'
} > "$REPO/fixture.txt"
git -C "$REPO" add fixture.txt
git -C "$REPO" stash push -q -u -m "marker fixture"
run_script --repo "$REPO"
assert_eq "marker-bearing stash content pops cleanly (exit 0)" "0" "$RC"
if has_markers "$REPO/fixture.txt"; then
    pass "the fixture's own marker text survived the pop untouched"
else
    fail "the fixture's marker text was lost"
fi
assert_eq "marker fixture entry consumed" "0" "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"

# ---------------------------------------------------------------------------
echo "Test 15: --json emits one parseable line per branch"
mk_repo
printf 'line1\nline2\nline3\nwip\n' > "$REPO/f.txt"
git -C "$REPO" stash push -q -m wip
run_script --repo "$REPO" --json --quiet
assert_eq "clean --json exits 0" "0" "$RC"
assert_contains "clean --json reports result=clean" "$OUT" '"result":"clean"'
assert_contains "clean --json reports exitCode 0" "$OUT" '"exitCode":0'
assert_eq "clean --json is exactly one line" "1" "$(printf '%s\n' "$OUT" | wc -l | tr -d ' ')"

mk_repo
stage_conflict "f.txt" 'STASHED\nline2\nline3\n' 'UPSTREAM\nline2\nline3\n'
run_script --repo "$REPO" --json --quiet
assert_eq "conflict --json exits 3" "3" "$RC"
assert_contains "conflict --json reports result=restored" "$OUT" '"result":"restored"'
assert_contains "conflict --json reports the failing pop status" "$OUT" '"popStatus":1'
assert_contains "conflict --json names the conflicted file" "$OUT" '"conflictFiles":["f.txt"]'
# The pre-pop tree was clean here, so "restore to clean HEAD" needs no snapshot
# commit and none is anchored. Test 11 covers the dirty-tree case where one is.
assert_contains "conflict --json reports an empty snapshotRef for a clean pre-pop tree" \
    "$OUT" '"snapshotRef":""'
if command -v jq >/dev/null 2>&1; then
    if printf '%s' "$OUT" | jq -e . >/dev/null 2>&1; then
        pass "conflict --json line is valid JSON"
    else
        fail "conflict --json line is not valid JSON: $OUT"
    fi
fi

# ---------------------------------------------------------------------------
echo "Test 16: an untracked payload (-u) survives a clean pop"
mk_repo
printf 'brand new\n' > "$REPO/new.txt"
git -C "$REPO" stash push -q -u -m "with untracked"
run_script --repo "$REPO"
assert_eq "-u clean pop exits 0" "0" "$RC"
if [[ -f "$REPO/new.txt" ]]; then
    pass "-u payload restored by the clean pop"
else
    fail "-u payload missing after the clean pop"
fi

# ---------------------------------------------------------------------------
echo "Test 17: a CONFLICTING pop's untracked payload is rolled back, and only it"
# git restores a `-u` stash's untracked payload even when the tracked merge goes
# on to conflict (verified against git 2.55), and `reset --hard` does not remove
# untracked files — so the rollback has to clean the payload up itself. It must
# remove ONLY the payload: an operator's own untracked file, present before the
# pop, has to survive.
mk_repo
printf 'STASHED\nline2\nline3\n' > "$REPO/f.txt"
printf 'payload\n' > "$REPO/from-stash.txt"
git -C "$REPO" stash push -q -u -m wip
printf 'UPSTREAM\nline2\nline3\n' > "$REPO/f.txt"
git -C "$REPO" add f.txt
git -C "$REPO" commit -q -m upstream
printf 'operator scratch\n' > "$REPO/keep-me.txt"   # untracked, pre-dates the pop
run_script --repo "$REPO"
assert_eq "conflicting pop with an untracked payload exits 3" "3" "$RC"
if [[ -f "$REPO/from-stash.txt" ]]; then
    fail "the stash's untracked payload was left behind by the rollback"
else
    pass "the stash's untracked payload was rolled back"
fi
if [[ -f "$REPO/keep-me.txt" ]]; then
    pass "an untracked file that pre-dated the pop was NOT removed"
else
    fail "the rollback removed an untracked file it did not put there"
fi
assert_eq "tree is back to pre-pop state (only the operator's own untracked file)" \
    "?? keep-me.txt" "$(git -C "$REPO" status --porcelain)"
assert_eq "the untracked payload is still recoverable from the preserved entry" "1" \
    "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"

# ---------------------------------------------------------------------------
echo "Test 18: rollback never deletes a path that is TRACKED in HEAD"
# A path can be untracked at stash time and committed by pop time. `reset --hard`
# restores the COMMITTED copy at that path; deleting it as if it were the stash's
# payload would manufacture a deletion the caller never asked for.
mk_repo
printf 'STASHED\nline2\nline3\n' > "$REPO/f.txt"
printf 'payload\n' > "$REPO/later-committed.txt"
git -C "$REPO" stash push -q -u -m wip
printf 'UPSTREAM\nline2\nline3\n' > "$REPO/f.txt"
printf 'committed content\n' > "$REPO/later-committed.txt"
git -C "$REPO" add f.txt later-committed.txt
git -C "$REPO" commit -q -m "upstream commits both"
run_script --repo "$REPO"
assert_eq "conflicting pop over a now-tracked payload path exits 3 (rolled back)" "3" "$RC"
assert_eq "the tracked file survives at its committed content" \
    "committed content" "$(cat "$REPO/later-committed.txt" 2>/dev/null)"
assert_eq "no spurious deletion is left in the tree" "" "$(git -C "$REPO" status --porcelain)"
assert_eq "the stash entry is still preserved" "1" \
    "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"

# ---------------------------------------------------------------------------
echo "Test 19: a conflict on a non-ASCII path is reported (#6517)"
# core.quotepath defaults to true, so `git diff --name-only` etc. would emit
# "café.txt" (C-quoted, octal-escaped) rather than café.txt. Before the fix,
# scan_new_marker_files()'s `[[ -f "$REPO/$path" ]]` test on the quoted form
# fails and the path silently drops out of both conflictFiles and the report.
mk_repo
printf 'line1\n' > "$REPO/café.txt"
git -C "$REPO" add café.txt
git -C "$REPO" commit -q -m "add café.txt"
stage_conflict "café.txt" 'STASHED\nline2\nline3\n' 'UPSTREAM\nline2\nline3\n'
run_script --repo "$REPO"
assert_eq "non-ASCII conflict path exits 3 (conflict, rolled back)" "3" "$RC"
assert_contains "non-ASCII conflict path appears in the stderr report" "$ERR" "café.txt"
assert_eq "the stash entry is preserved" "1" \
    "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"

mk_repo
printf 'line1\n' > "$REPO/café.txt"
git -C "$REPO" add café.txt
git -C "$REPO" commit -q -m "add café.txt"
stage_conflict "café.txt" 'STASHED\nline2\nline3\n' 'UPSTREAM\nline2\nline3\n'
run_script --repo "$REPO" --json --quiet
assert_eq "non-ASCII conflict --json exits 3" "3" "$RC"
assert_contains "non-ASCII conflict path appears in conflictFiles" "$OUT" 'café.txt'
if command -v jq >/dev/null 2>&1; then
    if printf '%s' "$OUT" | jq -e . >/dev/null 2>&1; then
        pass "non-ASCII conflict --json line is valid JSON"
    else
        fail "non-ASCII conflict --json line is not valid JSON: $OUT"
    fi
    assert_eq "jq extracts the exact non-ASCII path" "café.txt" \
        "$(printf '%s' "$OUT" | jq -r '.conflictFiles[0]')"
fi

# ---------------------------------------------------------------------------
echo "Test 20: a -u payload with a non-ASCII name is removed by the exit-3 rollback (#6517)"
# The untracked-payload cleanup loop scans `git ls-tree -r --name-only` on the
# stash's untracked tree; before the fix the quoted name fails the `-f` test
# and `rm -f` on it is a silent no-op, leaving the payload as a stray file.
mk_repo
printf 'STASHED\nline2\nline3\n' > "$REPO/f.txt"
printf 'payload\n' > "$REPO/café-payload.txt"
git -C "$REPO" stash push -q -u -m wip
printf 'UPSTREAM\nline2\nline3\n' > "$REPO/f.txt"
git -C "$REPO" add f.txt
git -C "$REPO" commit -q -m upstream
run_script --repo "$REPO"
assert_eq "conflicting pop with a non-ASCII untracked payload exits 3" "3" "$RC"
if [[ -f "$REPO/café-payload.txt" ]]; then
    fail "the non-ASCII untracked payload was left behind by the rollback"
else
    pass "the non-ASCII untracked payload was removed by the rollback"
fi
assert_eq "the untracked payload is still recoverable from the preserved entry" "1" \
    "$(git -C "$REPO" stash list | wc -l | tr -d ' ')"

# ---------------------------------------------------------------------------
echo "Test 21: --json stays jq -e .-parseable for a path containing a literal backslash (#6517)"
# emit_json() must escape a literal backslash BEFORE the quote-escape, or a
# path with a `\` (which is exactly what core.quotepath's C-quoting of a
# non-ASCII path also produces, \NNN-octal) yields an invalid JSON line.
mk_repo
BACKSLASH_NAME='back\slash.txt'
printf 'line1\n' > "$REPO/$BACKSLASH_NAME"
git -C "$REPO" add "$BACKSLASH_NAME"
git -C "$REPO" commit -q -m "add backslash-named file"
stage_conflict "$BACKSLASH_NAME" 'STASHED\nline2\nline3\n' 'UPSTREAM\nline2\nline3\n'
run_script --repo "$REPO" --json --quiet
assert_eq "backslash-path conflict --json exits 3" "3" "$RC"
if command -v jq >/dev/null 2>&1; then
    if printf '%s' "$OUT" | jq -e . >/dev/null 2>&1; then
        pass "backslash-path --json line is valid JSON"
    else
        fail "backslash-path --json line is not valid JSON: $OUT"
    fi
    assert_eq "jq extracts the exact backslash-containing path" "$BACKSLASH_NAME" \
        "$(printf '%s' "$OUT" | jq -r '.conflictFiles[0]')"
else
    fail "jq is required to verify Test 21 (not found on PATH)"
fi

# ---------------------------------------------------------------------------
echo ""
echo "========================================"
if [[ $TESTS_FAILED -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED tests passed${NC}"
    exit 0
fi
echo -e "${RED}$TESTS_FAILED of $TESTS_RUN tests failed${NC}"
exit 1
