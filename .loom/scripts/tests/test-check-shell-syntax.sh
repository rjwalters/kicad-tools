#!/usr/bin/env bash
# test-check-shell-syntax.sh - Smoke tests for check-shell-syntax.sh (#6162)
#
# Exercises the `bash -n` parse-check guard added for #6162 (an abandoned
# `git stash pop` conflict left live conflict markers in an installed shell
# script — nothing asserted that installed shell surfaces actually parse).
#
# This file deliberately embeds literal conflict-marker fixtures, so it opts
# itself out of check-conflict-markers.sh (#6499) with that script's in-file
# sentinel: check-conflict-markers:allow
#
# Verified behavior:
#   - exit 0 when every scanned *.sh file parses cleanly
#   - exit 2 when a *.sh file has conflict markers / does not parse, naming
#     the offending file and printing bash -n's own error
#   - --dir is repeatable and scans each directory recursively
#   - non-*.sh files are never scanned (a broken non-shell file is ignored)
#   - --quiet suppresses the "N script(s) parse cleanly" success line but
#     still reports failures
#   - default (no --dir) mode resolves the MAIN worktree's installed
#     .loom/hooks (top-level only) + .loom/scripts (recursive), and works the
#     same from inside a linked worktree (worktree-safe, like
#     check-main-clean.sh)
#   - --dir with a nonexistent path exits 1 (usage error)
#   - --help exits 0 and prints usage
#   - unknown argument exits 1
#
# Usage:
#   ./.loom/scripts/tests/test-check-shell-syntax.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-shell-syntax.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-shell-syntax.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-shell-syntax.sh is executable"
else
    fail "check-shell-syntax.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: --help exits 0 and prints usage --------
echo "Test 2: --help"
out=$("$SCRIPT" --help 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"check-shell-syntax.sh"* ]]; then
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

# -------- Test 5: --dir with only clean *.sh files exits 0 --------
echo "Test 5: clean directory exits 0"
mkdir -p "$WORKDIR/clean"
printf '#!/usr/bin/env bash\necho hello\n' > "$WORKDIR/clean/a.sh"
printf '#!/usr/bin/env bash\nif [[ 1 -eq 1 ]]; then echo yes; fi\n' > "$WORKDIR/clean/b.sh"
out=$("$SCRIPT" --dir "$WORKDIR/clean" 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"2 script(s) parse cleanly"* ]]; then
    pass "clean directory exits 0 and reports the count"
else
    fail "expected exit 0 with a clean count, got rc=$RC out=$out"
fi

# -------- Test 6: a conflict-marker-corrupted *.sh fails, naming the file --------
echo "Test 6: conflict-marker-corrupted script fails and is named"
mkdir -p "$WORKDIR/broken"
printf '#!/usr/bin/env bash\necho hi\n' > "$WORKDIR/broken/ok.sh"
cat > "$WORKDIR/broken/spawn-claude.sh" <<'EOF'
#!/usr/bin/env bash
echo start
<<<<<<< Updated upstream
echo one
=======
echo two
>>>>>>> Stashed changes
EOF
out=$("$SCRIPT" --dir "$WORKDIR/broken" 2>&1); RC=$?
if [[ "$RC" -eq 2 ]] \
   && [[ "$out" == *"spawn-claude.sh does not parse"* ]] \
   && [[ "$out" == *"1 of 2 script(s) FAILED to parse"* ]]; then
    pass "exit 2, names the offending file, and reports the fail count"
else
    fail "expected exit 2 naming spawn-claude.sh, got rc=$RC out=$out"
fi

# -------- Test 7: a broken NON-.sh file is never scanned --------
echo "Test 7: only *.sh files are scanned"
mkdir -p "$WORKDIR/nonshell"
printf 'not bash at all {{{ <<<<<<<\n' > "$WORKDIR/nonshell/notes.txt"
printf '#!/usr/bin/env bash\necho fine\n' > "$WORKDIR/nonshell/fine.sh"
out=$("$SCRIPT" --dir "$WORKDIR/nonshell" 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"1 script(s) parse cleanly"* ]]; then
    pass "non-.sh files are ignored"
else
    fail "expected exit 0 scanning only the .sh file, got rc=$RC out=$out"
fi

# -------- Test 8: --dir is repeatable and scans each directory --------
echo "Test 8: repeated --dir scans every directory"
mkdir -p "$WORKDIR/dirA" "$WORKDIR/dirB"
printf '#!/usr/bin/env bash\necho a\n' > "$WORKDIR/dirA/a.sh"
printf '#!/usr/bin/env bash\necho b\n' > "$WORKDIR/dirB/b.sh"
out=$("$SCRIPT" --dir "$WORKDIR/dirA" --dir "$WORKDIR/dirB" 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"2 script(s) parse cleanly"* ]]; then
    pass "repeated --dir scans both directories"
else
    fail "expected exit 0 with 2 scripts, got rc=$RC out=$out"
fi

# -------- Test 9: --dir recurses into subdirectories --------
echo "Test 9: --dir recurses into subdirectories"
mkdir -p "$WORKDIR/nested/sub/deeper"
printf '#!/usr/bin/env bash\necho top\n' > "$WORKDIR/nested/top.sh"
printf '#!/usr/bin/env bash\necho deep\n' > "$WORKDIR/nested/sub/deeper/deep.sh"
out=$("$SCRIPT" --dir "$WORKDIR/nested" 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"2 script(s) parse cleanly"* ]]; then
    pass "recurses into nested subdirectories"
else
    fail "expected exit 0 with 2 scripts, got rc=$RC out=$out"
fi

# -------- Test 10: --quiet suppresses the success line, but not failures --------
echo "Test 10: --quiet"
out=$("$SCRIPT" --dir "$WORKDIR/clean" --quiet 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" != *"parse cleanly"* ]]; then
    pass "--quiet suppresses the success summary line on a clean scan"
else
    fail "expected no success line under --quiet, got rc=$RC out=$out"
fi

out=$("$SCRIPT" --dir "$WORKDIR/broken" --quiet 2>&1); RC=$?
if [[ "$RC" -eq 2 ]] && [[ "$out" == *"spawn-claude.sh does not parse"* ]]; then
    pass "--quiet still reports failures"
else
    fail "expected failure reported under --quiet, got rc=$RC out=$out"
fi

# -------- Test 11: an empty directory (no *.sh files) exits 0 --------
echo "Test 11: empty directory exits 0"
mkdir -p "$WORKDIR/empty"
out=$("$SCRIPT" --dir "$WORKDIR/empty" 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"0 script(s) parse cleanly"* ]]; then
    pass "empty directory exits 0 with a zero count"
else
    fail "expected exit 0 with zero count, got rc=$RC out=$out"
fi

# -------- Test 12: default (no --dir) mode scans a fake installed tree --------
echo "Test 12: default mode resolves the main worktree's installed .loom/{hooks,scripts}"
REPO="$WORKDIR/repo"
git init -q "$REPO"
git -C "$REPO" config user.email t@t.t
git -C "$REPO" config user.name test
mkdir -p "$REPO/.loom/hooks" "$REPO/.loom/scripts/lib"
printf '#!/usr/bin/env bash\necho hook\n' > "$REPO/.loom/hooks/guard.sh"
printf '#!/usr/bin/env bash\necho script\n' > "$REPO/.loom/scripts/foo.sh"
printf '#!/usr/bin/env bash\necho libscript\n' > "$REPO/.loom/scripts/lib/bar.sh"
git -C "$REPO" add -A
git -C "$REPO" commit -q -m init
out=$( cd "$REPO" && "$SCRIPT" 2>&1 ); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"3 script(s) parse cleanly"* ]]; then
    pass "default mode scans installed hooks (top-level) + scripts (recursive)"
else
    fail "expected exit 0 with 3 scripts, got rc=$RC out=$out"
fi

# -------- Test 13: default mode catches a corrupted installed script --------
echo "Test 13: default mode catches a corrupted installed script"
cat > "$REPO/.loom/scripts/broken.sh" <<'EOF'
#!/usr/bin/env bash
echo start
<<<<<<< Updated upstream
echo one
=======
echo two
>>>>>>> Stashed changes
EOF
out=$( cd "$REPO" && "$SCRIPT" 2>&1 ); RC=$?
rm -f "$REPO/.loom/scripts/broken.sh"
if [[ "$RC" -eq 2 ]] && [[ "$out" == *"broken.sh does not parse"* ]]; then
    pass "default mode catches a corrupted installed script"
else
    fail "expected exit 2 naming broken.sh, got rc=$RC out=$out"
fi

# -------- Test 14: default mode ignores a *.sh under a hooks subdirectory --------
echo "Test 14: default mode's hooks scan is top-level only (matches the resync walk)"
mkdir -p "$REPO/.loom/hooks/tests"
cat > "$REPO/.loom/hooks/tests/broken-subdir.sh" <<'EOF'
#!/usr/bin/env bash
<<<<<<< nope
EOF
out=$( cd "$REPO" && "$SCRIPT" 2>&1 ); RC=$?
rm -rf "$REPO/.loom/hooks/tests"
if [[ "$RC" -eq 0 && "$out" == *"3 script(s) parse cleanly"* ]]; then
    pass "a broken script under .loom/hooks/<subdir>/ is not scanned (top-level only)"
else
    fail "expected the subdirectory script to be ignored, got rc=$RC out=$out"
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
