#!/usr/bin/env bash
# test-resync-installed-guard-check.sh - guard-hook install check wiring in
# resync-installed.sh (#7761)
#
# Split out of test-resync-installed.sh (which is frozen by the file-size
# ratchet, .loom/docs/file-size-policy.md) rather than grown in place -- see
# that file's own header for the full fixture-based test catalog this one
# does not duplicate.
#
# The real check-guards-installed.sh has its own suite
# (test-check-guards-installed.sh); this file installs a scriptable STUB as
# defaults/scripts/check-guards-installed.sh and asserts only on the WIRING --
# that resync invokes it at all, retries with --fix on a real run, and
# carries an unrepairable BROKEN GUARD INSTALL into the summary and exit code.
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-guard-check.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/resync-installed.sh"

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-guard.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder (trimmed copy of test-resync-installed.sh's) -----------
# Just enough of a defaults/ + .loom/ tree for resync-installed.sh to run to
# completion; the guard-hook install check itself doesn't touch any of it.
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts/lib" \
             "$repo/.loom/hooks" "$repo/.loom/scripts/lib"
    git -C "$repo" init -q

    printf 'A\n' > "$repo/defaults/hooks/guard.sh"
    printf 'S\n' > "$repo/defaults/scripts/foo.sh"
    printf 'L\n' > "$repo/defaults/scripts/lib/bar.sh"
    chmod +x "$repo/defaults/hooks/guard.sh" "$repo/defaults/scripts/foo.sh" \
             "$repo/defaults/scripts/lib/bar.sh"

    printf 'OLD\n' > "$repo/.loom/hooks/guard.sh"
    printf 'S\n'   > "$repo/.loom/scripts/foo.sh"

    # Version source + metadata re-stamp target.
    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"

    # A real commit so loom_commit re-stamps to an actual short sha. #7864:
    # the message deliberately matches resync-installed.sh's local-divergence
    # protection "safe lineage" pattern (RESYNC_COMMIT_SUBJECT_RE) -- this
    # fixture's hooks/guard.sh drift (OLD -> A below) must resync cleanly for
    # this file's guard-hook-install-check assertions to hold; a non-matching
    # message would make that drift look like a local fix and BLOCK it.
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1

    echo "$repo"
}

add_guardcheck_stub() {
    local repo="$1"
    cat > "$repo/defaults/scripts/check-guards-installed.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${GUARD_STUB_LOG:?GUARD_STUB_LOG not set}"
# LOOM_TEST_GUARD_RCS is a space-separated list consumed one entry per call,
# so a group can script "fails, then --fix repairs it".
if [[ -n "${LOOM_TEST_GUARD_RCS:-}" ]]; then
    n="$(wc -l < "$GUARD_STUB_LOG" | tr -d ' ')"
    set -- $LOOM_TEST_GUARD_RCS
    eval "rc=\${$n:-0}"
    exit "$rc"
fi
exit "${LOOM_TEST_GUARD_RC:-0}"
STUB
    chmod +x "$repo/defaults/scripts/check-guards-installed.sh"
}

echo "Test group 1: guard-hook install check -- healthy install (#7761)"
REPO="$(make_fixture)"
add_guardcheck_stub "$REPO"
GUARD_STUB_LOG="$WORKDIR/guard1.log"; : > "$GUARD_STUB_LOG"
OUT="$(cd "$REPO" && GUARD_STUB_LOG="$GUARD_STUB_LOG" LOOM_TEST_GUARD_RC=0 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -qi "guard-hook install" <<<"$OUT"; then
    pass "(#7761) a healthy guard install is reported and does not fail the resync"
else
    fail "(#7761) healthy guard install not reported (rc=$RC); out=$OUT"
fi
if [[ "$(wc -l < "$GUARD_STUB_LOG" | tr -d ' ')" -eq 1 ]] && ! grep -q -- "--fix" "$GUARD_STUB_LOG"; then
    pass "(#7761) a healthy install calls the checker exactly once, never with --fix"
else
    fail "(#7761) unexpected checker invocations: $(cat "$GUARD_STUB_LOG")"
fi

echo "Test group 2: guard-hook install check -- broken, --fix repairs it (#7761)"
REPO="$(make_fixture)"
add_guardcheck_stub "$REPO"
GUARD_STUB_LOG="$WORKDIR/guard2.log"; : > "$GUARD_STUB_LOG"
# First call fails (a hook is not executable), the --fix retry succeeds. This
# also pins the guard_rc reset: without it the successful retry would inherit
# the first call's non-zero status and report a repair as a failure.
OUT="$(cd "$REPO" && GUARD_STUB_LOG="$GUARD_STUB_LOG" LOOM_TEST_GUARD_RCS="2 0" bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && ! grep -qi "BROKEN GUARD INSTALL" <<<"$OUT"; then
    pass "(#7761) a repaired guard install exits 0 and is not reported as broken"
else
    fail "(#7761) a repaired guard install was still reported broken (rc=$RC); out=$OUT"
fi
if grep -q -- "--fix" "$GUARD_STUB_LOG"; then
    pass "(#7761) a real run retries the checker with --fix"
else
    fail "(#7761) the --fix retry never happened: $(cat "$GUARD_STUB_LOG")"
fi

echo "Test group 3: guard-hook install check -- unrepairable -> exit 75 (#7761)"
REPO="$(make_fixture)"
add_guardcheck_stub "$REPO"
GUARD_STUB_LOG="$WORKDIR/guard3.log"; : > "$GUARD_STUB_LOG"
OUT="$(cd "$REPO" && GUARD_STUB_LOG="$GUARD_STUB_LOG" LOOM_TEST_GUARD_RC=2 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 75 ]] && grep -qi "BROKEN GUARD INSTALL" <<<"$OUT"; then
    pass "(#7761) an unrepairable broken guard install exits 75 and says so"
else
    fail "(#7761) a broken guard install did not surface (rc=$RC, want 75); out=$OUT"
fi

echo "Test group 4: guard-hook install check -- --dry-run never tries to fix (#7761)"
REPO="$(make_fixture)"
add_guardcheck_stub "$REPO"
GUARD_STUB_LOG="$WORKDIR/guard4.log"; : > "$GUARD_STUB_LOG"
OUT="$(cd "$REPO" && GUARD_STUB_LOG="$GUARD_STUB_LOG" LOOM_TEST_GUARD_RC=2 bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if ! grep -q -- "--fix" "$GUARD_STUB_LOG"; then
    pass "(#7761) --dry-run reports the broken install without mutating anything"
else
    fail "(#7761) --dry-run called the checker with --fix: $(cat "$GUARD_STUB_LOG")"
fi

# --- summary -----------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
echo "========================================"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
