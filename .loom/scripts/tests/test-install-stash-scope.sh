#!/usr/bin/env bash
# test-install-stash-scope.sh — regression tests for the reinstall stash guard
# scoping (issue #3597).
#
# The `--quick` reinstall (install.sh) and `--clean` install (install-loom.sh)
# guards used to run an unscoped `git stash push`, sweeping sibling installers'
# uncommitted tracked changes into the stash and leaving a half-old/half-new
# hybrid tree. The fix scopes the stash to the intersection of the dirty set
# with Loom's ownership set (manifest paths + .gitignore) via
# scripts/install/stash-scope.sh::_emit_loom_owned_dirty_paths.
#
# Strategy: source the real helper against a temp git repo seeded with both a
# Loom-owned file and a sibling (non-Loom) file, dirty both, and assert:
#   1. the helper lists ONLY the Loom-owned dirty path,
#   2. a pathspec-scoped `git stash push` leaves the sibling change untouched
#      in the working tree and absent from the stash,
#   3. a tree dirty with ONLY sibling changes yields no owned-dirty paths
#      (callers skip the stash entirely).
#
# Usage:
#   bash defaults/scripts/tests/test-install-stash-scope.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# In the source checkout this file lives at defaults/scripts/tests/; the repo
# root is three levels up. When shipped into a consumer at
# .loom/scripts/tests/, the same climb lands on the consumer root — but this
# test is a source-checkout artifact and expects the real scripts/install/.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STASH_SCOPE="$REPO_ROOT/scripts/install/stash-scope.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_PASSED=$((TESTS_PASSED + 1)); TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() {
  TESTS_FAILED=$((TESTS_FAILED + 1)); TESTS_RUN=$((TESTS_RUN + 1))
  echo -e "  ${RED}FAIL${NC}: $1"
  [[ -n "${2:-}" ]] && echo "$2" | sed 's/^/      /'
}

assert_eq() {
  local expected="$1" actual="$2" msg="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass "$msg"
  else
    fail "$msg" "expected: [$expected]
  actual: [$actual]"
  fi
}

if [[ ! -f "$STASH_SCOPE" ]]; then
  echo "ERROR: $STASH_SCOPE not found" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$STASH_SCOPE"

# A real Loom-owned path (present in the defaults/ manifest) and a sibling path
# Loom never ships. .loom/roles/builder.md is a stable manifest entry.
OWNED_PATH=".loom/roles/builder.md"
SIBLING_PATH=".anvil/install-metadata.json"

# Sanity: confirm the chosen owned path is actually in the ownership set, so
# the test fails loudly if the manifest layout changes rather than silently
# passing on an empty set.
OWNERSHIP="$(_emit_loom_ownership_paths "$REPO_ROOT" "$REPO_ROOT")"
if ! printf '%s\n' "$OWNERSHIP" | grep -qxF "$OWNED_PATH"; then
  echo "ERROR: expected $OWNED_PATH in the Loom ownership set (manifest drift?)" >&2
  exit 1
fi
if printf '%s\n' "$OWNERSHIP" | grep -qxF "$SIBLING_PATH"; then
  echo "ERROR: sibling path $SIBLING_PATH unexpectedly in ownership set" >&2
  exit 1
fi

# Build a throwaway git repo that mirrors a consumer tree: a committed
# Loom-owned file plus a committed sibling-installer file.
TMP_REPO="$(mktemp -d "${TMPDIR:-/tmp}/loom-stash-scope.XXXXXX")"
trap 'rm -rf "$TMP_REPO"' EXIT

git -C "$TMP_REPO" init -q
git -C "$TMP_REPO" config user.email test@example.com
git -C "$TMP_REPO" config user.name "Test"

mkdir -p "$TMP_REPO/$(dirname "$OWNED_PATH")" "$TMP_REPO/$(dirname "$SIBLING_PATH")"
printf 'loom original\n' > "$TMP_REPO/$OWNED_PATH"
printf '{"version":"old"}\n' > "$TMP_REPO/$SIBLING_PATH"
git -C "$TMP_REPO" add -A
git -C "$TMP_REPO" commit -qm "seed"

echo "== Test 1: mixed-dirty tree — only the Loom-owned path is selected =="
printf 'loom modified\n' > "$TMP_REPO/$OWNED_PATH"
printf '{"version":"new"}\n' > "$TMP_REPO/$SIBLING_PATH"

SELECTED="$(_emit_loom_owned_dirty_paths "$REPO_ROOT" "$TMP_REPO")"
assert_eq "$OWNED_PATH" "$SELECTED" "helper selects only the Loom-owned dirty path"

echo "== Test 2: scoped stash leaves the sibling change in the working tree =="
# Reproduce the caller's pathspec array + stash push.
OWNED_DIRTY=()
while IFS= read -r p; do [[ -n "$p" ]] && OWNED_DIRTY+=("$p"); done \
  < <(_emit_loom_owned_dirty_paths "$REPO_ROOT" "$TMP_REPO")

git -C "$TMP_REPO" stash push -m "loom-install: test" -- "${OWNED_DIRTY[@]}" >/dev/null 2>&1

# Sibling file must still carry its uncommitted modification.
SIBLING_CONTENT="$(cat "$TMP_REPO/$SIBLING_PATH")"
assert_eq '{"version":"new"}' "$SIBLING_CONTENT" "sibling change survives in working tree after stash"

# Loom-owned file must have been reverted to HEAD by the stash.
OWNED_CONTENT="$(cat "$TMP_REPO/$OWNED_PATH")"
assert_eq "loom original" "$OWNED_CONTENT" "Loom-owned change was stashed (reverted to HEAD)"

# The stash must not carry the sibling path.
STASH_FILES="$(git -C "$TMP_REPO" stash show --name-only 'stash@{0}' 2>/dev/null)"
if printf '%s\n' "$STASH_FILES" | grep -qxF "$SIBLING_PATH"; then
  fail "sibling path absent from stash" "stash contained: $STASH_FILES"
else
  pass "sibling path absent from stash"
fi
if printf '%s\n' "$STASH_FILES" | grep -qxF "$OWNED_PATH"; then
  pass "Loom-owned path present in stash"
else
  fail "Loom-owned path present in stash" "stash contained: $STASH_FILES"
fi

# Pop restores the Loom-owned change cleanly.
git -C "$TMP_REPO" stash pop >/dev/null 2>&1
assert_eq "loom modified" "$(cat "$TMP_REPO/$OWNED_PATH")" "stash pop restores the Loom-owned change"

echo "== Test 3: sibling-only dirty tree yields no owned-dirty paths (no stash) =="
git -C "$TMP_REPO" checkout -- . 2>/dev/null || true
git -C "$TMP_REPO" stash clear 2>/dev/null || true
printf '{"version":"newer"}\n' > "$TMP_REPO/$SIBLING_PATH"

SELECTED_SIBLING_ONLY="$(_emit_loom_owned_dirty_paths "$REPO_ROOT" "$TMP_REPO")"
assert_eq "" "$SELECTED_SIBLING_ONLY" "sibling-only dirty tree produces no owned-dirty paths"

echo ""
echo "Ran $TESTS_RUN test(s): $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
