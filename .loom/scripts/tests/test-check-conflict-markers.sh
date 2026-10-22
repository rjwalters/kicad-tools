#!/usr/bin/env bash
# test-check-conflict-markers.sh - Smoke tests for check-conflict-markers.sh (#6499)
#
# Exercises the tracked-file conflict-marker gate added for #6499 (an
# abandoned `git stash pop` conflict left live markers in the tracked
# `.loom/config.json`, was then COMMITTED by a resync pass, and silently
# disabled observability/safehouse/roleRunner on the next daemon boot —
# every working-tree-state detector is blind once the corruption is
# committed, and check-shell-syntax.sh only covers `*.sh`).
#
# This file constructs literal conflict-marker fixtures on the fly (via
# printf, never as source lines of its own), so unlike
# test-check-shell-syntax.sh it does NOT need the in-file opt-out sentinel.
#
# Verified behavior:
#   - exit 0 on a clean tree, exit 2 when a file carries markers
#   - the offender's path AND the offending line numbers are named
#   - the #6499 shape (a stash-pop conflict inside JSON) is detected
#   - detection is extension-agnostic (.json/.md/.rs, not just .sh) — the
#     gap that let #6499 through check-shell-syntax.sh
#   - a bare `=======` line is NOT flagged (markdown setext heading
#     underline / separator comment — the false positive that would make
#     this gate unusable in a docs-heavy repo)
#   - inline backticked marker text mid-line is NOT flagged (how the
#     troubleshooting docs describe this very failure)
#   - the in-file `check-conflict-markers:allow` sentinel exempts a file
#   - default (no --dir) mode scans git-TRACKED files: an untracked file
#     with markers is ignored, a tracked one is caught
#   - --dir is repeatable and scans recursively
#   - --quiet suppresses the success line but still reports failures
#   - --self-test exits 0
#   - --dir with a nonexistent path exits 1 (usage error)
#   - --help exits 0 and prints usage
#   - unknown argument exits 1
#
# Usage:
#   ./.loom/scripts/tests/test-check-conflict-markers.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-conflict-markers.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-conflict-markers.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# Writes the exact #6499 corruption shape into $1: a `git stash pop` conflict
# on `safehouse.room` inside an otherwise-valid config object. Constructed via
# printf so this test file's own source stays marker-free.
write_conflicted_config() {
    {
        printf '{\n  "safehouse": {\n    "enabled": false,\n'
        printf '<<<<<<< Updated upstream\n'
        printf '    "room": "!MQP2aSTA5uDu7czxYZ:safehouse.example.com"\n'
        printf '=======\n'
        printf '    "socket": "/home/ubuntu/.loom/safehoused/state/safehoused.sock",\n'
        printf '    "room": "loom-fleet",\n'
        printf '>>>>>>> Stashed changes\n'
        printf '    "persona": "loom_daemon"\n  }\n}\n'
    } > "$1"
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-conflict-markers.sh is executable"
else
    fail "check-conflict-markers.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: --help exits 0 and prints usage --------
echo "Test 2: --help"
out=$("$SCRIPT" --help 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"check-conflict-markers.sh"* ]]; then
    pass "--help exits 0 and prints usage"
else
    fail "expected exit 0 with usage text, got rc=$RC out=$out"
fi

# -------- Test 3: unknown argument exits 1 --------
echo "Test 3: unknown argument"
"$SCRIPT" --bogus >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 1 ]]; then pass "unknown arg exits 1"; else fail "expected 1, got $RC"; fi

# -------- Test 4: --dir with a nonexistent path exits 1 --------
echo "Test 4: --dir nonexistent path"
"$SCRIPT" --dir "$WORKDIR/does-not-exist" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 1 ]]; then pass "--dir nonexistent path exits 1"; else fail "expected 1, got $RC"; fi

# -------- Test 5: --self-test exits 0 --------
echo "Test 5: --self-test"
out=$("$SCRIPT" --self-test 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"self-test passed"* ]]; then
    pass "--self-test exits 0"
else
    fail "expected exit 0 from --self-test, got rc=$RC out=$out"
fi

# -------- Test 6: clean directory exits 0 --------
echo "Test 6: clean directory"
mkdir -p "$WORKDIR/clean"
printf '{\n  "safehouse": { "room": "loom-fleet" }\n}\n' > "$WORKDIR/clean/config.json"
printf '#!/usr/bin/env bash\necho hello\n' > "$WORKDIR/clean/a.sh"
out=$("$SCRIPT" --dir "$WORKDIR/clean" 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"no conflict markers found"* ]]; then
    pass "clean directory exits 0 and reports the scanned count"
else
    fail "expected exit 0 with a clean summary, got rc=$RC out=$out"
fi

# -------- Test 7: the #6499 shape is detected, with path and line numbers --------
echo "Test 7: conflicted .loom/config.json shape"
mkdir -p "$WORKDIR/incident"
write_conflicted_config "$WORKDIR/incident/config.json"
out=$("$SCRIPT" --dir "$WORKDIR/incident" 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"config.json"* && "$out" == *"4:"* && "$out" == *"9:"* ]]; then
    pass "conflicted JSON exits 2, naming the path and both marker line numbers"
else
    fail "expected exit 2 naming config.json lines 4 and 9, got rc=$RC out=$out"
fi

# -------- Test 8: the fixture really is what broke the daemon (invalid JSON) --------
echo "Test 8: fixture is genuinely unparseable JSON"
if command -v jq >/dev/null 2>&1; then
    if jq -e . "$WORKDIR/incident/config.json" >/dev/null 2>&1; then
        fail "fixture parsed as valid JSON — it does not reproduce the #6499 incident"
    else
        pass "fixture is unparseable JSON (the config-loss precondition)"
    fi
else
    pass "jq unavailable — skipping the parseability cross-check"
fi

# -------- Test 9: detection is extension-agnostic --------
echo "Test 9: extension-agnostic detection (the check-shell-syntax.sh gap)"
mkdir -p "$WORKDIR/exts"
for ext in json md rs toml yml; do
    printf 'prefix\n<<<<<<< Updated upstream\na\n=======\nb\n>>>>>>> Stashed changes\n' \
        > "$WORKDIR/exts/file.$ext"
done
out=$("$SCRIPT" --dir "$WORKDIR/exts" 2>&1); RC=$?
missing=""
for ext in json md rs toml yml; do
    [[ "$out" == *"file.$ext"* ]] || missing="$missing .$ext"
done
if [[ "$RC" -eq 2 && -z "$missing" ]]; then
    pass "markers are caught in .json/.md/.rs/.toml/.yml alike"
else
    fail "expected all five extensions flagged, missing:$missing (rc=$RC)"
fi

# -------- Test 10: a bare `=======` line is NOT flagged --------
echo "Test 10: markdown setext heading / separator is not a false positive"
mkdir -p "$WORKDIR/setext"
{
    printf 'Recovery Procedure\n'
    printf '==================\n'
    printf '\n'
    printf '=======\n'
    printf 'done\n'
} > "$WORKDIR/setext/doc.md"
"$SCRIPT" --dir "$WORKDIR/setext" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "a bare ======= line is not flagged"
else
    fail "expected exit 0 (no false positive on =======), got $RC"
fi

# -------- Test 11: inline backticked marker text is NOT flagged --------
echo "Test 11: inline marker text mid-line is not a false positive"
mkdir -p "$WORKDIR/inline"
printf 'Look for literal `<<<<<<<` / `=======` / `>>>>>>>` markers in the file.\n' \
    > "$WORKDIR/inline/troubleshooting.md"
"$SCRIPT" --dir "$WORKDIR/inline" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "inline (non-line-start) marker text is not flagged"
else
    fail "expected exit 0 (docs may describe markers inline), got $RC"
fi

# -------- Test 12: the in-file opt-out sentinel exempts a file --------
echo "Test 12: in-file opt-out sentinel"
mkdir -p "$WORKDIR/optout"
sentinel="check-conflict-markers$(printf ':')allow"
{
    printf '#!/usr/bin/env bash\n'
    printf '# deliberate fixture: %s\n' "$sentinel"
    printf '<<<<<<< Updated upstream\na\n=======\nb\n>>>>>>> Stashed changes\n'
} > "$WORKDIR/optout/fixture.sh"
"$SCRIPT" --dir "$WORKDIR/optout" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "a file embedding the sentinel is skipped"
else
    fail "expected exit 0 for an opted-out fixture, got $RC"
fi

# Same file WITHOUT the sentinel must still be caught (proves test 12 is not
# passing for some unrelated reason).
printf '<<<<<<< Updated upstream\na\n=======\nb\n>>>>>>> Stashed changes\n' \
    > "$WORKDIR/optout/fixture.sh"
"$SCRIPT" --dir "$WORKDIR/optout" >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 2 ]]; then
    pass "the same fixture without the sentinel IS caught"
else
    fail "expected exit 2 without the sentinel, got $RC"
fi

# -------- Test 13: --dir is repeatable and recursive --------
echo "Test 13: --dir repeatable + recursive"
mkdir -p "$WORKDIR/multi/a/deep" "$WORKDIR/multi/b"
printf 'ok\n' > "$WORKDIR/multi/b/fine.txt"
printf 'x\n<<<<<<< Updated upstream\na\n=======\nb\n>>>>>>> Stashed changes\n' \
    > "$WORKDIR/multi/a/deep/bad.json"
out=$("$SCRIPT" --dir "$WORKDIR/multi/a" --dir "$WORKDIR/multi/b" 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"deep/bad.json"* ]]; then
    pass "--dir is repeatable and descends into subdirectories"
else
    fail "expected exit 2 naming the nested file, got rc=$RC out=$out"
fi

# -------- Test 14: --quiet suppresses success, still reports failures --------
echo "Test 14: --quiet"
out=$("$SCRIPT" --dir "$WORKDIR/clean" --quiet 2>&1); RC=$?
if [[ "$RC" -eq 0 && -z "$out" ]]; then
    pass "--quiet prints nothing on success"
else
    fail "expected silent exit 0, got rc=$RC out=$out"
fi
out=$("$SCRIPT" --dir "$WORKDIR/incident" --quiet 2>&1); RC=$?
if [[ "$RC" -eq 2 && "$out" == *"config.json"* ]]; then
    pass "--quiet still reports failures"
else
    fail "expected exit 2 naming the offender even with --quiet, got rc=$RC out=$out"
fi

# -------- Test 15: default mode scans TRACKED files only --------
echo "Test 15: default mode scans git-tracked files"
REPO="$WORKDIR/repo"
mkdir -p "$REPO/.loom"
git init -q "$REPO" 2>/dev/null
git -C "$REPO" config user.email "test@example.com"
git -C "$REPO" config user.name "Test"
printf 'hello\n' > "$REPO/README.md"
git -C "$REPO" add README.md >/dev/null 2>&1
git -C "$REPO" commit -qm "init" >/dev/null 2>&1

# Untracked file with markers: must be ignored (it is not in history and
# cannot be shipped; the working-tree detectors own that case).
write_conflicted_config "$REPO/.loom/config.json"
out=$( cd "$REPO" && "$SCRIPT" 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "an UNTRACKED file with markers is ignored in default mode"
else
    fail "expected exit 0 for an untracked offender, got rc=$RC out=$out"
fi

# Now track and commit it — the exact #6499 escape (markers swept into a
# commit by a resync pass). This is what the gate exists to catch.
git -C "$REPO" add -f .loom/config.json >/dev/null 2>&1
git -C "$REPO" commit -qm "chore: resync installed Loom surfaces" >/dev/null 2>&1
out=$( cd "$REPO" && "$SCRIPT" 2>&1 ); RC=$?
if [[ "$RC" -eq 2 && "$out" == *".loom/config.json"* ]]; then
    pass "a COMMITTED tracked file with markers exits 2 (the #6499 escape)"
else
    fail "expected exit 2 for the committed offender, got rc=$RC out=$out"
fi

# -------- Test 16: default mode outside a git repo exits 1 --------
echo "Test 16: default mode outside a git repository"
NONREPO="$WORKDIR/not-a-repo"
mkdir -p "$NONREPO"
out=$( cd "$NONREPO" && GIT_CEILING_DIRECTORIES="$WORKDIR" "$SCRIPT" 2>&1 ); RC=$?
if [[ "$RC" -eq 1 && "$out" == *"not inside a git repository"* ]]; then
    pass "default mode outside a repo exits 1 with a clear message"
else
    fail "expected exit 1 outside a git repo, got rc=$RC out=$out"
fi

# -------- Summary --------
echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED/$TESTS_RUN tests passed${NC}"
    exit 0
else
    echo -e "${RED}FAILED: $TESTS_FAILED/$TESTS_RUN tests failed${NC}"
    exit 1
fi
