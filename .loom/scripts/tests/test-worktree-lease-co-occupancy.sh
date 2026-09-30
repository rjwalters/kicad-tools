#!/usr/bin/env bash
# test-worktree-lease-co-occupancy.sh - Tests for worktree.sh's shared-lease
# guard on the "existing worktree has uncommitted changes -> preserve" fast
# path (rjwalters/kicad-tools#5783).
#
# kicad-tools issue #5781 was claimed by several same-host sweeps at once; two
# Builders edited the same uncommitted file in the shared
# .loom/worktrees/issue-5781 concurrently, because this fast path handed the
# directory back unconditionally. worktree.sh now asks `loom-daemon lease
# co-occupancy` first, and refuses when the issue carries 2+ simultaneously
# fresh sweep leases.
#
# Verifies, through the real worktree.sh + the real subcommand, with the
# forge read served by a fake gh (LOOM_GH_BIN):
#   1. Two fresh leases + uncommitted changes: refuses (nonzero), names the
#      override, and leaves the uncommitted work untouched.
#   2. The same with WORKTREE_ALLOW_SHARED_LEASE=1: proceeds (preserves).
#   3. Exactly one fresh lease: proceeds.
#   4. A failing forge read: fails OPEN, proceeds.
#   5. --json refusal: a parseable success=false document on stdout.
#   6. Two fresh leases but NO uncommitted changes: not consulted, proceeds.
#
# Needs a BUILT loom-daemon (wired in the "Native Port Suites" CI job, which
# downloads the shared build first); FAILS rather than skips without one.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-worktree-lease-co-occupancy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "lease"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

TMP=$(mktemp -d /tmp/loom-lease-cooc-test.XXXXXX)
trap 'rm -rf "$TMP"; cd "$SCRIPTS_DIR" 2>/dev/null || true' EXIT

git init -q -b main "$TMP/origin.git" --bare
git init -q -b main "$TMP/repo"
cd "$TMP/repo"
git config user.email t@t
git config user.name t
git commit --allow-empty -q -m init
git remote add origin "$TMP/origin.git"
git push -q origin main
# A real install gitignores the worktree sentinel (the loom-managed .gitignore
# block, init/post_init.rs); without that, `.loom-managed` alone would read as
# uncommitted work and Test 6 could not tell "clean" from "dirty".
echo ".loom-managed" >> .git/info/exclude

mkdir -p .loom/scripts/lib
cp "$WORKTREE_SH" .loom/scripts/worktree.sh
cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
chmod +x .loom/scripts/worktree.sh

# The fake gh: `api .../comments` prints $TMP/leases.ndjson (the NDJSON the
# real --jq filter would emit), or fails when $TMP/gh-fail exists. Reached
# only via LOOM_GH_BIN, so nothing else in worktree.sh sees it.
cat > "$TMP/gh" <<EOF
#!/usr/bin/env bash
[[ -e "$TMP/gh-fail" ]] && { echo "HTTP 502" >&2; exit 1; }
cat "$TMP/leases.ndjson" 2>/dev/null || true
EOF
chmod +x "$TMP/gh"
export LOOM_GH_BIN="$TMP/gh"
unset WORKTREE_ALLOW_SHARED_LEASE

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# Authored by the fleet's default App, as the real --jq projection now
# attributes every row: the reader drops untrusted/unattributed ones (#9631).
lease_line() {
    printf '{"updated_at":"%s","body":"<!-- loom:lease host=%s sweep=%s -->\\nprose","user":{"login":"loom-fleet-dispatch[bot]","type":"Bot"},"author_association":"NONE"}\n' "$NOW_ISO" "$1" "$2"
}
two_leases() { { lease_line host-x sweep-a; lease_line host-x sweep-b; } > "$TMP/leases.ndjson"; }

# A worktree for issue 401 with uncommitted work in it.
./.loom/scripts/worktree.sh 401 >/dev/null 2>&1
echo "in-progress edit" > .loom/worktrees/issue-401/wip.txt

echo "Test 1: two fresh leases + uncommitted changes -> refuse"
two_leases
if ./.loom/scripts/worktree.sh 401 >"$TMP/out1" 2>&1; then
    fail "worktree.sh handed back a co-occupied worktree (see $(cat "$TMP/out1"))"
elif grep -q "WORKTREE_ALLOW_SHARED_LEASE" "$TMP/out1" && grep -q "sweep=sweep-b" "$TMP/out1" \
    && [[ -f .loom/worktrees/issue-401/wip.txt ]]; then
    pass "refused, named the live leases and the override, left the uncommitted work alone"
else
    fail "refused but output or worktree state unexpected: $(cat "$TMP/out1")"
fi

echo ""
echo "Test 2: WORKTREE_ALLOW_SHARED_LEASE=1 overrides the refusal"
if WORKTREE_ALLOW_SHARED_LEASE=1 ./.loom/scripts/worktree.sh 401 >"$TMP/out2" 2>&1 \
    && grep -q "proceeding despite" "$TMP/out2"; then
    pass "override proceeds and says so"
else
    fail "override did not proceed with a warning: $(cat "$TMP/out2")"
fi

echo ""
echo "Test 3: exactly one fresh lease -> proceed"
lease_line host-x sweep-a > "$TMP/leases.ndjson"
if ./.loom/scripts/worktree.sh 401 >"$TMP/out3" 2>&1; then
    pass "a single live lease (the sweep resuming its own tree) proceeds"
else
    fail "refused with only one live lease: $(cat "$TMP/out3")"
fi

echo ""
echo "Test 4: a failing forge read fails open"
two_leases
touch "$TMP/gh-fail"
if ./.loom/scripts/worktree.sh 401 >"$TMP/out4" 2>&1; then
    pass "read failure is not evidence of a peer"
else
    fail "a gh read failure blocked the worktree: $(cat "$TMP/out4")"
fi
rm -f "$TMP/gh-fail"

echo ""
echo "Test 5: --json refusal is a success=false document on stdout"
if ./.loom/scripts/worktree.sh --json 401 >"$TMP/out5" 2>"$TMP/err5"; then
    fail "--json exited 0 despite two live leases"
elif grep -q '"success":false' "$TMP/out5" && grep -q "WORKTREE_ALLOW_SHARED_LEASE" "$TMP/out5"; then
    pass "--json refusal carries the documented shape"
else
    fail "--json refusal missing expected fields: stdout=$(cat "$TMP/out5") stderr=$(cat "$TMP/err5")"
fi

echo ""
echo "Test 6: no uncommitted changes -> the guard is not consulted"
./.loom/scripts/worktree.sh 402 >/dev/null 2>&1
git -C .loom/worktrees/issue-402 commit -q --allow-empty -m "local work"
if ./.loom/scripts/worktree.sh 402 >"$TMP/out6" 2>&1; then
    pass "a committed-only worktree is preserved without the lease read"
else
    fail "a worktree with no uncommitted changes was refused: $(cat "$TMP/out6")"
fi

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
[[ "$TESTS_FAILED" -eq 0 ]] || exit 1
