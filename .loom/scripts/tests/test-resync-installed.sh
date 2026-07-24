#!/usr/bin/env bash
# test-resync-installed.sh - Smoke tests for resync-installed.sh (#3777)
#
# Constructs throwaway git repos with synthetic defaults/ and installed .loom/
# trees so it can deterministically exercise the load-bearing cases:
#   (a) already in sync     -> exit 0, "Already in sync", no writes
#   (b) drift (differing)   -> file rewritten to match defaults/, exit 0
#   (c) missing installed   -> file created from defaults/, exit 0
#   (d) --dry-run + drift    -> exit 2, installed file UNCHANGED
#   (e) --dry-run + in sync -> exit 0
#   (f) repo-specific file   -> file present only in .loom/ left untouched
#   (g) .loom/resync-ignore  -> pinned file reported "skipped", not overwritten
#   (h) idempotent rerun     -> second run reports all unchanged
# Plus contract checks:
#   - --help prints usage, exit 0
#   - unknown arg exits 1
#   - not-a-git-repo exits 1
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed.sh

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder ---------------------------------------------------------
# Creates a git repo at $WORKDIR/repo with:
#   defaults/hooks/guard.sh          (source of truth, "A")
#   defaults/scripts/foo.sh          (source of truth, "S")
#   defaults/scripts/lib/bar.sh      (source of truth, "L")
#   .loom/hooks/guard.sh             (installed, "OLD" -> drift)
#   .loom/scripts/foo.sh             (installed, "S"   -> in sync)
#   (.loom/scripts/lib/bar.sh MISSING -> to be created)
#   .loom/scripts/custom-only.sh     (repo-specific, no defaults/ counterpart)
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
    printf 'REPO-SPECIFIC\n' > "$repo/.loom/scripts/custom-only.sh"

    echo "$repo"
}

# --- (a) in-sync / (b) drift / (c) missing: a single apply run --------------
echo "Test group 1: apply resyncs drift + creates missing, leaves the rest"
REPO="$(make_fixture)"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "apply exits 0"; else fail "apply exits 0 (got $RC)"; fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(b) drifted hooks/guard.sh rewritten to match defaults"
else
    fail "(b) drifted hooks/guard.sh not updated"
fi
if [[ -f "$REPO/.loom/scripts/lib/bar.sh" && "$(cat "$REPO/.loom/scripts/lib/bar.sh")" == "L" ]]; then
    pass "(c) missing scripts/lib/bar.sh created from defaults"
else
    fail "(c) missing scripts/lib/bar.sh not created"
fi
if [[ "$(cat "$REPO/.loom/scripts/custom-only.sh")" == "REPO-SPECIFIC" ]]; then
    pass "(f) repo-specific custom-only.sh left untouched"
else
    fail "(f) repo-specific custom-only.sh was modified/removed"
fi
if grep -q "updated" <<<"$OUT" && grep -q "created" <<<"$OUT"; then
    pass "reports both 'updated' and 'created'"
else
    fail "did not report both updated and created"
fi

# --- (h) idempotent rerun ----------------------------------------------------
echo "Test group 2: idempotent rerun is a no-op"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(h) second run reports already in sync, exit 0"
else
    fail "(h) second run not a clean no-op (rc=$RC)"
fi

# --- (d) dry-run + drift: exit 2, no writes ----------------------------------
echo "Test group 3: --dry-run previews without writing"
REPO="$(make_fixture)"
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(d) --dry-run with drift exits 2"
else
    fail "(d) --dry-run with drift exits 2 (got $RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(d) --dry-run left installed file UNCHANGED"
else
    fail "(d) --dry-run modified the installed file"
fi
if [[ ! -f "$REPO/.loom/scripts/lib/bar.sh" ]]; then
    pass "(d) --dry-run did not create the missing file"
else
    fail "(d) --dry-run created a file it should only have previewed"
fi

# --- (e) dry-run + in sync: exit 0 -------------------------------------------
echo "Test group 4: --dry-run when already in sync exits 0"
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)   # apply first
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "already in sync" <<<"$OUT"; then
    pass "(e) --dry-run in sync exits 0"
else
    fail "(e) --dry-run in sync exits 0 (rc=$RC)"
fi

# --- (g) resync-ignore pins a local override ---------------------------------
echo "Test group 5: .loom/resync-ignore preserves a pinned local override"
REPO="$(make_fixture)"
printf 'PINNED-LOCAL\n' > "$REPO/.loom/hooks/guard.sh"
printf 'hooks/guard.sh  # keep my local tweak\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "skipped" <<<"$OUT"; then
    pass "(g) pinned file reported as skipped"
else
    fail "(g) pinned file not reported skipped (rc=$RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "PINNED-LOCAL" ]]; then
    pass "(g) pinned file NOT overwritten"
else
    fail "(g) pinned file was overwritten despite resync-ignore"
fi

# --- contract checks ---------------------------------------------------------
echo "Test group 6: flag/contract checks"
if bash "$SCRIPT" --help 2>&1 | grep -q "resync-installed.sh"; then
    pass "--help prints usage"
else
    fail "--help did not print usage"
fi
if bash "$SCRIPT" --help >/dev/null 2>&1; then pass "--help exits 0"; else fail "--help did not exit 0"; fi

REPO="$(make_fixture)"
RC=0; (cd "$REPO" && bash "$SCRIPT" --bogus >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 1 ]]; then pass "unknown arg exits 1"; else fail "unknown arg did not exit 1 (got $RC)"; fi

NON_REPO="$WORKDIR/not-a-repo"
mkdir -p "$NON_REPO"
RC=0; (cd "$NON_REPO" && bash "$SCRIPT" >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 1 ]]; then pass "outside a git repo exits 1"; else fail "outside a git repo did not exit 1 (got $RC)"; fi

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
