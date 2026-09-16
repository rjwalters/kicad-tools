#!/usr/bin/env bash
# test-check-defaults-version-bump.sh - Smoke tests for
# check-defaults-version-bump.sh (#5874, #6480).
#
# Constructs a throwaway local git repo with a `defaults/` tree and a
# `VERSION` file, then exercises:
#   (a) no defaults/ change              -> PASS
#   (b) defaults/ change + VERSION bump  -> PASS
#   (c) defaults/ change, no bump        -> FAIL, lists the changed files
#   (d) defaults/ change + PR_BODY marker -> PASS
#   (e) defaults/ change + commit-message marker -> PASS
#   (f) non-defaults/ change only        -> PASS (nothing to check)
# Plus usage/arg-handling checks:
#   - missing --base -> exit 2
#   - unknown base ref -> exit 2
#   - --help prints usage
# Plus custom-watch-path checks (#6480):
#   - --paths: change under a custom watched path, no bump -> FAIL
#   - --paths: change outside the watched paths only        -> PASS
#   - VERSION_BUMP_WATCH_PATHS env var behaves like --paths
#   - no --paths / no env var: default behavior unchanged (covered by the
#     original (a)-(f) cases above, which never set either)
# Plus --forbid-bump mode checks (#7743 -- the inverted predicate used by
# Loom's own defaults-version-bump-check CI job once version bumps move to
# .github/workflows/version-bump-on-merge.yml):
#   - defaults/ change, no version-bearing file value changed -> PASS
#   - a version-bearing file's value hand-edited               -> FAIL
#   - CLAUDE.md prose edit (not the Loom Version line) + a
#     defaults/ change                                         -> PASS
#
# Usage:
#   ./.loom/scripts/tests/test-check-defaults-version-bump.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-defaults-version-bump.sh"

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-defaults-version-bump.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

REPO="$WORKDIR/repo"

# Fresh repo with a base commit carrying defaults/foo.md, install.sh (a
# stand-in for a consumer repo's own installable surface), VERSION=1.0.0, and
# the rest of --forbid-bump's FORBID_BUMP_VALUE_FILES set (package.json,
# mcp-loom/package.json, Cargo.toml, CLAUDE.md) so --forbid-bump cases can
# exercise the real default file list with no extra flags, tagged "base".
# Callers add more commits on top and diff against "base".
make_fixture() {
    rm -rf "$REPO"
    git init --quiet "$REPO"
    git -C "$REPO" checkout -q -b main
    mkdir -p "$REPO/defaults/scripts" "$REPO/mcp-loom"
    echo "hello" > "$REPO/defaults/scripts/foo.md"
    echo "#!/bin/sh" > "$REPO/install.sh"
    echo "1.0.0" > "$REPO/VERSION"
    echo "unrelated" > "$REPO/README.md"
    echo '{"version": "1.0.0"}' > "$REPO/package.json"
    echo '{"version": "1.0.0"}' > "$REPO/mcp-loom/package.json"
    printf '[workspace.package]\nversion = "1.0.0"\n' > "$REPO/Cargo.toml"
    printf '# Test\n\n**Loom Version**: 1.0.0\n\nSome prose.\n' > "$REPO/CLAUDE.md"
    git -C "$REPO" add -A
    git -C "$REPO" commit -q -m "base"
    git -C "$REPO" tag base
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-defaults-version-bump.sh is executable"
else
    fail "check-defaults-version-bump.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: no defaults/ change -> PASS --------
echo "Test 2: no defaults/ change passes"
make_fixture
echo "more" >> "$REPO/README.md"
git -C "$REPO" commit -q -am "readme only"
out="$(cd "$REPO" && "$SCRIPT" --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "no defaults/ change exits 0"
else
    fail "no defaults/ change expected exit 0, got $rc. Output: $out"
fi
if printf '%s' "$out" | grep -qi "OK"; then
    pass "no defaults/ change prints OK"
else
    fail "no defaults/ change missing OK message. Got: $out"
fi

# -------- Test 3: defaults/ change + VERSION bump -> PASS --------
echo "Test 3: defaults/ change with a VERSION bump passes"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
echo "1.0.1" > "$REPO/VERSION"
git -C "$REPO" commit -q -am "bump"
out="$(cd "$REPO" && "$SCRIPT" --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "defaults/ + VERSION bump exits 0"
else
    fail "defaults/ + VERSION bump expected exit 0, got $rc. Output: $out"
fi

# -------- Test 4: defaults/ change without a VERSION bump -> FAIL --------
echo "Test 4: defaults/ change without a VERSION bump fails"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "prompt change, no bump"
err_out="$(cd "$REPO" && "$SCRIPT" --base base 2>&1 >/dev/null)"
rc=0
( cd "$REPO" && "$SCRIPT" --base base >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "unbumped defaults/ change exits 1"
else
    fail "unbumped defaults/ change expected exit 1, got $rc"
fi
if printf '%s' "$err_out" | grep -q "defaults/scripts/foo.md"; then
    pass "failure output lists the changed file"
else
    fail "failure output missing changed file. Got: $err_out"
fi
if printf '%s' "$err_out" | grep -q "version.sh bump patch"; then
    pass "failure output suggests the version bump remediation"
else
    fail "failure output missing version bump remediation. Got: $err_out"
fi
if printf '%s' "$err_out" | grep -q "loom:no-surface-change"; then
    pass "failure output mentions the no-surface-change marker escape hatch"
else
    fail "failure output missing marker escape hatch. Got: $err_out"
fi

# -------- Test 5: defaults/ change + PR_BODY marker -> PASS --------
echo "Test 5: defaults/ change with a PR_BODY marker passes"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "doc typo fix"
out="$(cd "$REPO" && PR_BODY='fixes a typo

<!-- loom:no-surface-change -->' "$SCRIPT" --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "PR_BODY marker exits 0"
else
    fail "PR_BODY marker expected exit 0, got $rc. Output: $out"
fi

# -------- Test 6: defaults/ change + commit-message marker -> PASS --------
echo "Test 6: defaults/ change with a commit-message marker passes"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "doc typo fix

<!-- loom:no-surface-change -->"
out="$(cd "$REPO" && "$SCRIPT" --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "commit-message marker exits 0"
else
    fail "commit-message marker expected exit 0, got $rc. Output: $out"
fi

# -------- Test 7: missing --base -> exit 2 --------
echo "Test 7: missing --base exits 2"
make_fixture
rc=0
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 2 ]]; then
    pass "missing --base exits 2"
else
    fail "missing --base expected exit 2, got $rc"
fi

# -------- Test 8: unknown base ref -> exit 2 --------
echo "Test 8: unknown --base ref exits 2"
make_fixture
rc=0
( cd "$REPO" && "$SCRIPT" --base does-not-exist >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 2 ]]; then
    pass "unknown --base ref exits 2"
else
    fail "unknown --base ref expected exit 2, got $rc"
fi

# -------- Test 9: --help prints usage and exits 0 --------
echo "Test 9: --help prints usage and exits 0"
help_out="$("$SCRIPT" --help 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--help exit code is 0"
else
    fail "--help expected exit 0, got $rc"
fi
if printf '%s' "$help_out" | grep -qi "Usage"; then
    pass "--help mentions Usage"
else
    fail "--help did not mention Usage. Got: $help_out"
fi

# -------- Test 10: --paths, change under a watched path, no bump -> FAIL --------
echo "Test 10: --paths custom watch, change under it without a bump fails"
make_fixture
echo "changed" >> "$REPO/install.sh"
git -C "$REPO" commit -q -am "install.sh change, no bump"
rc=0
err_out="$(cd "$REPO" && "$SCRIPT" --base base --paths install.sh 2>&1 >/dev/null)" || true
( cd "$REPO" && "$SCRIPT" --base base --paths install.sh >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "--paths install.sh: unbumped change under watched path exits 1"
else
    fail "--paths install.sh: expected exit 1, got $rc"
fi
if printf '%s' "$err_out" | grep -q "install.sh"; then
    pass "--paths install.sh: failure output lists the changed file"
else
    fail "--paths install.sh: failure output missing changed file. Got: $err_out"
fi
if printf '%s' "$err_out" | grep -q "install.sh"; then
    pass "--paths install.sh: failure output names the actual watched path, not 'defaults/'"
else
    fail "--paths install.sh: failure output missing watched-path name. Got: $err_out"
fi

# -------- Test 11: --paths, change outside the watched paths -> PASS --------
echo "Test 11: --paths custom watch, change outside it passes"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "defaults/ change, not watched"
out="$(cd "$REPO" && "$SCRIPT" --base base --paths install.sh 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--paths install.sh: change outside watched paths exits 0"
else
    fail "--paths install.sh: expected exit 0, got $rc. Output: $out"
fi

# -------- Test 12: --paths with a VERSION bump -> PASS --------
echo "Test 12: --paths custom watch, change under it with a bump passes"
make_fixture
echo "changed" >> "$REPO/install.sh"
echo "1.0.1" > "$REPO/VERSION"
git -C "$REPO" commit -q -am "install.sh change, bumped"
out="$(cd "$REPO" && "$SCRIPT" --base base --paths install.sh 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--paths install.sh: bumped change under watched path exits 0"
else
    fail "--paths install.sh: expected exit 0, got $rc. Output: $out"
fi

# -------- Test 13: VERSION_BUMP_WATCH_PATHS env var, change under it, no bump -> FAIL --------
echo "Test 13: VERSION_BUMP_WATCH_PATHS env var, unbumped change under it fails"
make_fixture
echo "changed" >> "$REPO/install.sh"
git -C "$REPO" commit -q -am "install.sh change via env var, no bump"
rc=0
( cd "$REPO" && VERSION_BUMP_WATCH_PATHS="install.sh" "$SCRIPT" --base base >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "VERSION_BUMP_WATCH_PATHS=install.sh: unbumped change exits 1"
else
    fail "VERSION_BUMP_WATCH_PATHS=install.sh: expected exit 1, got $rc"
fi

# -------- Test 14: VERSION_BUMP_WATCH_PATHS env var, change outside it -> PASS --------
echo "Test 14: VERSION_BUMP_WATCH_PATHS env var, change outside it passes"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "defaults/ change, not watched via env var"
out="$(cd "$REPO" && VERSION_BUMP_WATCH_PATHS="install.sh" "$SCRIPT" --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "VERSION_BUMP_WATCH_PATHS=install.sh: change outside watched paths exits 0"
else
    fail "VERSION_BUMP_WATCH_PATHS=install.sh: expected exit 0, got $rc. Output: $out"
fi

# -------- Test 15: --paths overrides VERSION_BUMP_WATCH_PATHS -------
echo "Test 15: --paths takes precedence over VERSION_BUMP_WATCH_PATHS"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "defaults/ change, --paths should still watch it"
out="$(cd "$REPO" && VERSION_BUMP_WATCH_PATHS="install.sh" "$SCRIPT" --base base --paths defaults/ 2>&1 >/dev/null)"
rc=0
( cd "$REPO" && VERSION_BUMP_WATCH_PATHS="install.sh" "$SCRIPT" --base base --paths defaults/ >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "--paths defaults/ overrides VERSION_BUMP_WATCH_PATHS=install.sh"
else
    fail "--paths defaults/ expected to override env var and exit 1, got $rc"
fi

# -------- Test 16: default behavior with no --paths / no env var unchanged -------
echo "Test 16: default behavior (no --paths, no env var) matches original defaults/-only behavior"
make_fixture
echo "changed" >> "$REPO/install.sh"
git -C "$REPO" commit -q -am "install.sh change only, default watch is defaults/"
out="$(cd "$REPO" && "$SCRIPT" --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "default watch (defaults/): install.sh-only change exits 0 (not watched by default)"
else
    fail "default watch (defaults/): expected exit 0, got $rc. Output: $out"
fi

# -------- Test 17: --forbid-bump, defaults/ change with no version value change -> PASS --------
echo "Test 17: --forbid-bump, defaults/ change with no version-bearing value change passes"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "defaults/ change only, no version edit"
out="$(cd "$REPO" && "$SCRIPT" --forbid-bump --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--forbid-bump: defaults/ change, no version value change exits 0"
else
    fail "--forbid-bump: defaults/ change, no version value change expected exit 0, got $rc. Output: $out"
fi

# -------- Test 18: --forbid-bump, a version-bearing file is hand-edited -> FAIL --------
echo "Test 18: --forbid-bump, a hand-edited version-bearing file fails"
make_fixture
echo "1.0.1" > "$REPO/VERSION"
git -C "$REPO" commit -q -am "hand-bump VERSION"
rc=0
err_out="$(cd "$REPO" && "$SCRIPT" --forbid-bump --base base 2>&1 >/dev/null)"
( cd "$REPO" && "$SCRIPT" --forbid-bump --base base >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "--forbid-bump: hand-edited VERSION exits 1"
else
    fail "--forbid-bump: hand-edited VERSION expected exit 1, got $rc"
fi
if printf '%s' "$err_out" | grep -q "VERSION:"; then
    pass "--forbid-bump: failure output names the changed version-bearing file"
else
    fail "--forbid-bump: failure output missing changed file. Got: $err_out"
fi
if printf '%s' "$err_out" | grep -q "version-bump-on-merge.yml"; then
    pass "--forbid-bump: failure output points at the automated bump workflow"
else
    fail "--forbid-bump: failure output missing bump-workflow pointer. Got: $err_out"
fi

# -------- Test 19: --forbid-bump, CLAUDE.md prose edit + defaults/ change -> PASS --------
echo "Test 19: --forbid-bump, CLAUDE.md prose edit (not the Loom Version line) passes"
make_fixture
echo "More prose, not the version line." >> "$REPO/CLAUDE.md"
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "CLAUDE.md prose + defaults/ change, no version line touched"
out="$(cd "$REPO" && "$SCRIPT" --forbid-bump --base base 2>&1)"
rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--forbid-bump: CLAUDE.md prose-only edit exits 0"
else
    fail "--forbid-bump: CLAUDE.md prose-only edit expected exit 0, got $rc. Output: $out"
fi

# -------- Test 20: --forbid-bump does not require --base to touch defaults/ at all --------
echo "Test 20: --forbid-bump, a version-bearing file edited with NO defaults/ change still fails"
make_fixture
echo '{"version": "1.0.1"}' > "$REPO/package.json"
git -C "$REPO" commit -q -am "hand-bump package.json, no defaults/ touch"
rc=0
( cd "$REPO" && "$SCRIPT" --forbid-bump --base base >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "--forbid-bump: package.json value change with no defaults/ touch still exits 1"
else
    fail "--forbid-bump: expected exit 1 regardless of defaults/, got $rc"
fi

# -------- Test 21: default mode (no --forbid-bump) is unaffected by the new flag's fixture files --------
echo "Test 21: default mode still requires a VERSION bump for a defaults/ change"
make_fixture
echo "changed" >> "$REPO/defaults/scripts/foo.md"
git -C "$REPO" commit -q -am "defaults/ change, default mode"
rc=0
( cd "$REPO" && "$SCRIPT" --base base >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "default mode (no --forbid-bump): unbumped defaults/ change still exits 1"
else
    fail "default mode: expected exit 1, got $rc"
fi

# -------- Tests 22-24: --forbid-bump is immune to base-branch drift (#7823) --------
#
# Regression for the false FAIL that #7743's own PR hit: CI wires --base to
# `github.event.pull_request.base.sha` (the live base-branch tip at trigger
# time), so any PR whose branch predates a sibling merge that bumped the
# version saw a raw base-vs-head value MISMATCH -- reported as a hand-edit
# (in fact a downgrade: base's newer value -> head's older, untouched one)
# even though the PR's own commits never touch a version-bearing file.
# --forbid-bump must compare against merge-base(--base, --head) instead.
#
# Builds: base -> PR branch (defaults/ only, no version touch), while the
# base branch independently advances with the automated post-merge bump.
make_drift_fixture() {
    make_fixture
    git -C "$REPO" checkout -q -b pr base
    echo "changed" >> "$REPO/defaults/scripts/foo.md"
    git -C "$REPO" commit -q -am "pr work: defaults/ only, no version touch"
    PR_HEAD="$(git -C "$REPO" rev-parse HEAD)"
    git -C "$REPO" checkout -q main
    echo "1.0.1" > "$REPO/VERSION"
    echo '{"version": "1.0.1"}' > "$REPO/package.json"
    echo '{"version": "1.0.1"}' > "$REPO/mcp-loom/package.json"
    printf '[workspace.package]\nversion = "1.0.1"\n' > "$REPO/Cargo.toml"
    printf '# Test\n\n**Loom Version**: 1.0.1\n\nSome prose.\n' > "$REPO/CLAUDE.md"
    git -C "$REPO" commit -q -am "automated post-merge version bump (sibling merge)"
    MAIN_TIP="$(git -C "$REPO" rev-parse HEAD)"
}

echo "Test 22: --forbid-bump, base branch bumped after the branch diverged, PR touches no version file -> PASS"
make_drift_fixture
rc=0
out="$(cd "$REPO" && "$SCRIPT" --forbid-bump --base "$MAIN_TIP" --head "$PR_HEAD" 2>&1)" || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--forbid-bump: base-branch drift alone does not fail the PR"
else
    fail "--forbid-bump: base-branch drift expected exit 0, got $rc. Output: $out"
fi

echo "Test 23: --forbid-bump, a genuine hand-edit still fails despite base-branch drift"
make_drift_fixture
git -C "$REPO" checkout -q pr
echo "9.9.9" > "$REPO/VERSION"
git -C "$REPO" commit -q -am "hand-edit VERSION on the PR branch"
PR_HEAD="$(git -C "$REPO" rev-parse HEAD)"
rc=0
err_out="$(cd "$REPO" && "$SCRIPT" --forbid-bump --base "$MAIN_TIP" --head "$PR_HEAD" 2>&1 >/dev/null)" || true
( cd "$REPO" && "$SCRIPT" --forbid-bump --base "$MAIN_TIP" --head "$PR_HEAD" >/dev/null 2>&1 ) || rc=$?
if [[ "$rc" -eq 1 ]]; then
    pass "--forbid-bump: hand-edit inside the PR's own range still exits 1 under drift"
else
    fail "--forbid-bump: hand-edit under drift expected exit 1, got $rc. Output: $err_out"
fi
if printf '%s' "$err_out" | grep -q "VERSION: '1.0.0' -> '9.9.9'"; then
    pass "--forbid-bump: drift-narrowed failure reports the merge-base value, not the base tip's"
else
    fail "--forbid-bump: expected a 1.0.0 -> 9.9.9 VERSION report. Got: $err_out"
fi

echo "Test 24: --forbid-bump is idempotent when the caller already passes a merge-base"
make_drift_fixture
MB="$(git -C "$REPO" merge-base "$MAIN_TIP" "$PR_HEAD")"
rc=0
out="$(cd "$REPO" && "$SCRIPT" --forbid-bump --base "$MB" --head "$PR_HEAD" 2>&1)" || rc=$?
if [[ "$rc" -eq 0 ]]; then
    pass "--forbid-bump: a pre-computed merge-base --base (builder-pr.md's pre-flight) still exits 0"
else
    fail "--forbid-bump: pre-computed merge-base expected exit 0, got $rc. Output: $out"
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
