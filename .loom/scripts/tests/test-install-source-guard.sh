#!/usr/bin/env bash
# test-install-source-guard.sh - Unit tests for the source-state guard
# in scripts/install-loom.sh (issue #3327).
#
# The guard (check_source_state) runs after argv parsing and helper
# definitions. It refuses to install from a Loom source checkout that is:
#   - on a non-main branch, OR
#   - in detached HEAD that does NOT resolve to an exact-match tag, OR
#   - behind origin/main
# ...unless --allow-non-main-source is passed (info + continue), or the
# operator confirms an interactive prompt with 'y'. Detached HEAD on an
# exact-match tag is treated as clean (info-only, no warning gate).
#
# Test strategy: extract the check_source_state() function from
# install-loom.sh via awk, source it into a small harness that stubs the
# color helpers and read prompt, then exercise it against temp git repos
# built with `git init`. We do NOT run the full installer — the guard is
# pure (only touches the source repo).
#
# Usage:
#   bash defaults/scripts/tests/test-install-source-guard.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/scripts/install-loom.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local msg="$3"
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

assert_zero_exit() {
    local actual="$1"
    local msg="$2"
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
    local actual="$1"
    local msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" -ne 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (exit=$actual)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected non-zero, got $actual)"
    fi
}

if [[ ! -f "$INSTALL_SCRIPT" ]]; then
    echo "ERROR: $INSTALL_SCRIPT not found" >&2
    exit 1
fi

# Build a runnable harness once: extract the check_source_state function
# (and only that function) so the test exercises the production code path.
HARNESS_FILE=$(mktemp /tmp/loom-source-guard-harness.XXXXXX.sh)
trap 'rm -f "$HARNESS_FILE"' EXIT

# Extract the function body. Markers: the `check_source_state()` declaration
# line down through its closing `}` at column 0.
FUNC_BLOCK=$(awk '
    /^check_source_state\(\) \{$/ { capture=1 }
    capture { print }
    capture && /^\}$/ { exit }
' "$INSTALL_SCRIPT")

if [[ -z "$FUNC_BLOCK" ]]; then
    echo "ERROR: could not extract check_source_state() from $INSTALL_SCRIPT" >&2
    exit 1
fi

cat >"$HARNESS_FILE" <<'PROLOGUE'
#!/usr/bin/env bash
set -uo pipefail
# Stub helpers so the extracted function runs standalone.
RED=''; GREEN=''; BLUE=''; YELLOW=''; CYAN=''; NC=''
error()   { echo "Error: $*" >&2; exit 1; }
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
PROLOGUE

printf '%s\n' "$FUNC_BLOCK" >>"$HARNESS_FILE"

# Append the dispatcher: read env vars and call the function.
cat >>"$HARNESS_FILE" <<'EPILOGUE'
check_source_state
echo "__POST__=ok"
EPILOGUE

# Helper: run the harness with a synthesized git repo as LOOM_ROOT.
# Args:
#   $1: setup function name (creates the temp repo, leaves CWD inside it)
#   $2: ALLOW_NON_MAIN_SOURCE (true|false)
#   $3: NON_INTERACTIVE (true|false)
#   $4: stdin input (passed to read -p prompt)
run_guard() {
    local setup_fn="$1"
    local allow="$2"
    local non_interactive="$3"
    local stdin_in="$4"
    local tmp
    tmp=$(mktemp -d /tmp/loom-source-guard-test.XXXXXX)
    (
        cd "$tmp" || exit 1
        # shellcheck disable=SC2086,SC2317
        "$setup_fn" "$tmp"
        # Run harness with LOOM_ROOT pointing at the temp repo. Use printf to
        # feed stdin (for interactive prompt cases).
        ALLOW_NON_MAIN_SOURCE="$allow" \
        NON_INTERACTIVE="$non_interactive" \
        LOOM_ROOT="$tmp" \
            bash "$HARNESS_FILE" <<<"$stdin_in" 2>&1
        echo "__EXIT__=$?"
    )
    rm -rf "$tmp"
}

# Setup helpers — each leaves CWD inside the temp repo with the desired state.

setup_main_clean() {
    local dir="$1"
    git -C "$dir" init -q -b main .
    git -C "$dir" config user.email "test@example.com"
    git -C "$dir" config user.name "Test"
    : > "$dir/README.md"
    git -C "$dir" add README.md
    git -C "$dir" commit -q -m "initial"
}

setup_feature_branch() {
    local dir="$1"
    setup_main_clean "$dir"
    git -C "$dir" checkout -q -b feature/whatever
    : > "$dir/feature.txt"
    git -C "$dir" add feature.txt
    git -C "$dir" commit -q -m "feature work"
}

setup_detached_on_tag() {
    local dir="$1"
    setup_main_clean "$dir"
    git -C "$dir" tag v0.8.0
    # Detach HEAD onto the tag (older git versions don't accept tag refs
    # directly in --detach; resolve to SHA first).
    local sha
    sha=$(git -C "$dir" rev-parse v0.8.0)
    git -C "$dir" checkout -q --detach "$sha"
    # Re-create the tag annotation context by ensuring HEAD points at the
    # tagged SHA. `git describe --exact-match --tags` should now match.
}

setup_detached_no_tag() {
    local dir="$1"
    setup_main_clean "$dir"
    # Make a second commit, then detach at the first (which has no tag).
    local first
    first=$(git -C "$dir" rev-parse HEAD)
    : > "$dir/extra.txt"
    git -C "$dir" add extra.txt
    git -C "$dir" commit -q -m "second"
    git -C "$dir" checkout -q --detach "$first"
}

# ---------------------------------------------------------------------------
# Case 1: source on main, clean (no origin) — should pass silently.
# ---------------------------------------------------------------------------
echo "Case 1: clean main, no origin -> pass without warning"
OUTPUT=$(run_guard setup_main_clean false false "")
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
assert_zero_exit "$EXIT_CODE" "Case 1: exits zero"
# Must NOT contain the warning gate
if [[ "$OUTPUT" == *"is not on a clean main"* ]]; then
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 1: should not warn on clean main"
    echo "    Output: $OUTPUT"
else
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 1: no warning emitted"
fi
echo ""

# ---------------------------------------------------------------------------
# Case 2: source on feature branch, --yes (non-interactive), no allow flag
#         -> refuses with actionable error mentioning the override flag.
# ---------------------------------------------------------------------------
echo "Case 2: feature branch + --yes (no override) -> refuses"
OUTPUT=$(run_guard setup_feature_branch false true "")
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
assert_nonzero_exit "$EXIT_CODE" "Case 2: refuses install"
assert_contains "$OUTPUT" "feature/whatever" "Case 2: shows branch name"
assert_contains "$OUTPUT" "--allow-non-main-source" "Case 2: hints at override flag"
echo ""

# ---------------------------------------------------------------------------
# Case 3: source on feature branch + --allow-non-main-source -> proceeds.
# ---------------------------------------------------------------------------
echo "Case 3: feature branch + --allow-non-main-source -> proceeds"
OUTPUT=$(run_guard setup_feature_branch true true "")
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
assert_zero_exit "$EXIT_CODE" "Case 3: continues with override"
assert_contains "$OUTPUT" "Continuing anyway" "Case 3: announces override"
assert_contains "$OUTPUT" "__POST__=ok" "Case 3: function returned normally"
echo ""

# ---------------------------------------------------------------------------
# Case 4: detached HEAD on a v* tag, no override, --yes -> passes (tagged
#         release exemption: detached on an exact-match tag is treated as a
#         clean snapshot).
# ---------------------------------------------------------------------------
echo "Case 4: detached HEAD on v0.8.0 tag -> passes (tagged release)"
OUTPUT=$(run_guard setup_detached_on_tag false true "")
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
assert_zero_exit "$EXIT_CODE" "Case 4: tagged detached HEAD passes"
assert_contains "$OUTPUT" "v0.8.0" "Case 4: announces tag"
assert_contains "$OUTPUT" "__POST__=ok" "Case 4: function returned normally"
# Must NOT contain the "not on a clean main" warning
if [[ "$OUTPUT" == *"is not on a clean main"* ]]; then
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 4: should not warn for tagged release"
    echo "    Output: $OUTPUT"
else
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 4: no warning gate for tagged release"
fi
echo ""

# ---------------------------------------------------------------------------
# Case 5 (bonus): detached HEAD with NO matching tag, --yes, no override
#         -> refuses (this is the arbitrary detached-HEAD case, which we
#         treat as non-main since the operator may be at an old commit).
# ---------------------------------------------------------------------------
echo "Case 5: detached HEAD with no matching tag + --yes -> refuses"
OUTPUT=$(run_guard setup_detached_no_tag false true "")
EXIT_CODE=$(printf '%s' "$OUTPUT" | grep -oE '__EXIT__=[0-9]+' | tail -1 | cut -d= -f2)
assert_nonzero_exit "$EXIT_CODE" "Case 5: untagged detached HEAD refused"
assert_contains "$OUTPUT" "detached HEAD" "Case 5: mentions detached HEAD"
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "================================================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "================================================================"
echo "All tests passed."
