#!/usr/bin/env bash
# test-install-reinstall-safety.sh - Regression tests for issue #4188
# ("Harvest (fork #55): harden installer reinstall safety + target-aware
# setup-mcp.sh"), ported from gpeyton/loom commit f064aeef.
#
# Covers the installer-safety findings that remain in scope on this repo's
# restructured installer (see issue #4188 for the full harvest triage):
#   1. --quick / --yes / --full must not silently bypass the
#      destructive-reinstall acknowledgement gate -- a non-interactive
#      reinstall over an existing .loom/ install now requires
#      --confirm-reinstall (install.sh).
#   2. Quick Install must rebuild the loom-daemon binary when the source
#      tree is newer than the binary on disk, not just when it's absent
#      (install.sh::loom_daemon_binary_stale).
#   3. `scripts/uninstall-loom.sh --local`'s staging step must only stage
#      Loom-managed paths, not unrelated untracked/staged consumer files
#      sitting in the working tree (already landed via the #3545 work --
#      this group is a regression guard, not new behavior).
#
# Two groups present in the original fork commit are intentionally NOT
# ported here:
#   - is_legacy_rjwalters_install() metadata-first detection: the function
#     does not exist on this repo's restructured installer;
#     install-metadata.json is already written and consumed as authoritative
#     (install.sh's TARGET_PATH/.loom detection), so there is nothing left to
#     regression-test under that name.
#   - Hook-dedup quote-normalization (scaffolding.rs): split out to #4200,
#     tracked as a Rust unit test there.
#
# Strategy: install.sh's loom_daemon_binary_stale() is pure and side-effect
# free, so it is extracted via awk (same pattern as
# test-install-source-guard.sh) and exercised in an isolated harness rather
# than running the full installer end-to-end. The --confirm-reinstall gate
# and the uninstall staging scope are tested by actually invoking
# install.sh / uninstall-loom.sh against throwaway temp git repos.
#
# Usage:
#   bash defaults/scripts/tests/test-install-reinstall-safety.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"
UNINSTALL_SH="$REPO_ROOT/scripts/uninstall-loom.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Looking for: '$needle'"
        echo "    In output:"
        echo "$haystack" | sed 's/^/      /'
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Should NOT contain: '$needle'"
    fi
}

assert_zero_exit() {
    local actual="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" -eq 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (exit=$actual)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected 0, got $actual)"
    fi
}

assert_nonzero_exit() {
    local actual="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" -ne 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (exit=$actual)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected non-zero, got $actual)"
    fi
}

if [[ ! -f "$INSTALL_SH" ]]; then
    echo "ERROR: $INSTALL_SH not found" >&2
    exit 1
fi
if [[ ! -f "$UNINSTALL_SH" ]]; then
    echo "ERROR: $UNINSTALL_SH not found" >&2
    exit 1
fi

# Extract a single top-level function body ("name() {" ... "}") from a file.
extract_function() {
    local func_name="$1" file="$2"
    awk -v fn="${func_name}() {" '
        $0 == fn { capture=1 }
        capture { print }
        capture && /^\}$/ { exit }
    ' "$file"
}

echo "================================================================"
echo "Group 1: --confirm-reinstall gate (issue #4188)"
echo "================================================================"
echo ""

# install.sh's dependency-check step (git/node/pnpm/cargo) runs BEFORE the
# reinstall gate this group tests, and prompts interactively if any are
# missing. Rather than risk a false failure (or a hang) on a runner missing
# one of these, skip this group with a clear message when the full
# dependency set isn't available -- Groups 2/3 don't need it.
MISSING_FOR_GROUP1=()
for dep in git node pnpm cargo; do
    command -v "$dep" &> /dev/null || MISSING_FOR_GROUP1+=("$dep")
done

if [[ ${#MISSING_FOR_GROUP1[@]} -gt 0 ]]; then
    echo -e "${YELLOW}SKIP${NC}: missing ${MISSING_FOR_GROUP1[*]} -- install.sh's dependency-check step runs before the reinstall gate"
else
    # Build a throwaway "existing install" target: any dir with a .loom/
    # subdirectory is enough to hit the reinstall gate in install.sh, since
    # the gate itself runs before any uninstall/build work happens.
    T1=$(mktemp -d /tmp/loom-confirm-reinstall-test.XXXXXX)
    mkdir -p "$T1/.loom"
    echo "sentinel" > "$T1/.loom/sentinel-marker"
    git -C "$T1" init --quiet 2>/dev/null || true

    # Case 1: --quick without --confirm-reinstall must refuse (non-zero
    # exit), print guidance mentioning --confirm-reinstall, and leave the
    # existing .loom/ untouched (no uninstall attempted). Stdin redirected
    # from /dev/null so any latent interactive prompt fails fast (EOF)
    # instead of hanging.
    OUTPUT1=$("$INSTALL_SH" --quick "$T1" < /dev/null 2>&1)
    EXIT1=$?
    assert_nonzero_exit "$EXIT1" "Case 1: --quick without --confirm-reinstall refuses"
    assert_contains "$OUTPUT1" "--confirm-reinstall" "Case 1: guidance mentions --confirm-reinstall"
    if [[ -f "$T1/.loom/sentinel-marker" ]]; then
        TESTS_RUN=$((TESTS_RUN + 1))
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 1: existing .loom/ left untouched (no destructive uninstall ran)"
    else
        TESTS_RUN=$((TESTS_RUN + 1))
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 1: existing .loom/ was modified/removed -- gate did not block in time"
    fi

    # Case 2: -y (--yes) without --quick/--confirm-reinstall must ALSO
    # refuse -- -y alone still implies NON_INTERACTIVE=true, so it must hit
    # the same gate (regression guard for "any non-interactive flag
    # combination bypasses the warning", not just --quick specifically).
    OUTPUT2=$("$INSTALL_SH" -y "$T1" < /dev/null 2>&1)
    EXIT2=$?
    assert_nonzero_exit "$EXIT2" "Case 2: -y without --confirm-reinstall refuses"
    assert_contains "$OUTPUT2" "--confirm-reinstall" "Case 2: guidance mentions --confirm-reinstall"

    # Case 3: --full without --confirm-reinstall must ALSO refuse.
    OUTPUT3=$("$INSTALL_SH" --full "$T1" < /dev/null 2>&1)
    EXIT3=$?
    assert_nonzero_exit "$EXIT3" "Case 3: --full without --confirm-reinstall refuses"
    assert_contains "$OUTPUT3" "--confirm-reinstall" "Case 3: guidance mentions --confirm-reinstall"

    rm -rf "$T1"
fi
echo ""

echo "================================================================"
echo "Group 2: loom-daemon binary staleness (issue #4188)"
echo "================================================================"
echo ""

STALE_FN=$(extract_function "loom_daemon_binary_stale" "$INSTALL_SH")
if [[ -z "$STALE_FN" ]]; then
    echo "ERROR: could not extract loom_daemon_binary_stale() from $INSTALL_SH" >&2
    exit 1
fi

HARNESS=$(mktemp /tmp/loom-stale-binary-harness.XXXXXX.sh)
{
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    printf '%s\n' "$STALE_FN"
    echo 'loom_daemon_binary_stale "$1"'
    echo 'exit $?'
} > "$HARNESS"
chmod +x "$HARNESS"

T2=$(mktemp -d /tmp/loom-stale-binary-test.XXXXXX)
mkdir -p "$T2/loom-daemon/src" "$T2/loom-api/src" "$T2/target/release"
echo 'fn main() {}' > "$T2/loom-daemon/src/main.rs"
touch "$T2/Cargo.toml" "$T2/Cargo.lock"

# Case 1: binary absent -> stale.
"$HARNESS" "$T2"
assert_zero_exit "$?" "Case 1: missing binary is stale"

# Case 2: binary present and newer than all source files -> not stale.
touch "$T2/target/release/loom-daemon"
sleep 1.1
touch "$T2/target/release/loom-daemon"
"$HARNESS" "$T2"
assert_nonzero_exit "$?" "Case 2: binary newer than source is NOT stale"

# Case 3: source file touched after the binary (simulating `git pull`
# landing a newer commit) -> stale again.
sleep 1.1
echo '// updated' >> "$T2/loom-daemon/src/main.rs"
"$HARNESS" "$T2"
assert_zero_exit "$?" "Case 3: source newer than binary IS stale (issue #4188 regression)"

rm -rf "$T2" "$HARNESS"
echo ""

echo "================================================================"
echo "Group 3: uninstall-loom.sh --local stages only Loom-managed paths"
echo "================================================================"
echo ""

T3=$(mktemp -d /tmp/loom-uninstall-scope-test.XXXXXX)
git -C "$T3" init --quiet
git -C "$T3" config user.email "test@test.com"
git -C "$T3" config user.name "Test"

# Minimal Loom footprint (heuristic/legacy removal path -- no
# install-metadata.json manifest, so uninstall-loom.sh walks defaults/).
mkdir -p "$T3/.loom/roles" "$T3/.loom/scripts"
echo '{}' > "$T3/.loom/config.json"
cat > "$T3/CLAUDE.md" <<'EOF'
# Loom Orchestration - Repository Guide

Generated by Loom Installation Process
EOF
git -C "$T3" add -A
git -C "$T3" commit -m "Initial commit with Loom installed" --quiet

# Unrelated consumer state that must NOT be touched by the uninstall's
# staging step: an untracked file, a staged-but-uncommitted new file, and a
# modification to an existing tracked (non-Loom) file.
echo "unrelated" > "$T3/unrelated-untracked.txt"
echo "console.log('hi')" > "$T3/app.js"
git -C "$T3" add app.js
echo "# My Project" > "$T3/README.md"
git -C "$T3" add README.md
git -C "$T3" commit -m "Add app.js and README.md" --quiet
echo "# My Project (locally modified, not staged)" >> "$T3/README.md"
echo "another unrelated untracked file" > "$T3/scratch-notes.txt"

"$UNINSTALL_SH" --yes --local "$T3" > /tmp/loom-uninstall-scope-test.log 2>&1
UNINSTALL_EXIT=$?

TESTS_RUN=$((TESTS_RUN + 1))
if [[ $UNINSTALL_EXIT -eq 0 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: uninstall-loom.sh --local exited 0"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: uninstall-loom.sh --local exited $UNINSTALL_EXIT"
    sed 's/^/    /' /tmp/loom-uninstall-scope-test.log
fi

STAGED_AFTER=$(git -C "$T3" diff --cached --name-only)

# The unrelated untracked files must remain untracked (NOT staged).
assert_not_contains "$STAGED_AFTER" "unrelated-untracked.txt" \
    "unrelated untracked file was not staged by uninstall"
assert_not_contains "$STAGED_AFTER" "scratch-notes.txt" \
    "second unrelated untracked file was not staged by uninstall"

# The locally-modified-but-unstaged README.md change must remain unstaged
# (git add -A would have staged it as part of the "everything" sweep).
README_STAGED_DIFF=$(git -C "$T3" diff --cached -- README.md)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -z "$README_STAGED_DIFF" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: unrelated unstaged README.md edit was not swept into the index"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: unrelated unstaged README.md edit was staged by uninstall"
fi

# Loom-managed removals (e.g. .loom/config.json) SHOULD be staged as
# deletions -- the fix must not become "stage nothing".
assert_contains "$STAGED_AFTER" ".loom/config.json" \
    "Loom-managed file removal WAS staged (fix doesn't over-correct to no-op)"

rm -rf "$T3" /tmp/loom-uninstall-scope-test.log
echo ""

echo "================================================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "================================================================"
echo "All tests passed."
