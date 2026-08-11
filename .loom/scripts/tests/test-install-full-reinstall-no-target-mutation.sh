#!/usr/bin/env bash
# test-install-full-reinstall-no-target-mutation.sh — regression test for
# issue #4888 defect 2.
#
# Before the fix, `install.sh`'s `--confirm-reinstall` flow ran a chained
# `uninstall-loom.sh --yes --local "$TARGET_PATH"` directly against the
# TARGET's main checkout for BOTH the --quick and --full reinstall paths,
# unconditionally -- staging deletions and stripping the Loom sections out of
# CLAUDE.md / .gitignore in place -- before delegating the --full path via
# `exec "$LOOM_ROOT/scripts/install-loom.sh" ...`. Because `exec` replaces the
# current process, nothing in install.sh could roll that mutation back if the
# delegated install-loom.sh run failed downstream (e.g. at `loom-daemon
# init`), leaving the target's main checkout half-uninstalled with no
# automatic recovery (the reported symptom: manual `git reset --hard HEAD`
# required).
#
# The fix scopes that chained uninstall to the --quick path only (where the
# fresh install runs synchronously in the same working tree and unstages/
# restores as part of its own completion) -- the --full path never mutates
# the target's main checkout at all before delegating, so a downstream
# failure has nothing to roll back.
#
# Strategy: build a throwaway "fake LOOM_ROOT" whose install.sh is the REAL
# script (symlinked) but whose scripts/install-loom.sh is a stub that always
# fails -- deterministically simulating "the delegated Full Install workflow
# failed" without needing a built loom-daemon binary, gh auth, or network
# access. Run it against a scratch target repo seeded with an existing
# (older) Loom install, and assert the scratch repo's tracked tree is
# byte-for-byte unchanged afterward.
#
# Usage:
#   bash defaults/scripts/tests/test-install-full-reinstall-no-target-mutation.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

if [[ ! -f "$INSTALL_SH" ]]; then
  echo "ERROR: $INSTALL_SH not found" >&2
  exit 1
fi

if ! command -v git &> /dev/null; then
  echo -e "${YELLOW}SKIP${NC}: missing required dependency 'git'"
  exit 0
fi

echo "================================================================"
echo "Full-reinstall no-target-mutation-on-failure (issue #4888)"
echo "================================================================"
echo ""

# --- 1. Build a fake LOOM_ROOT: real install.sh, stubbed install-loom.sh ---
#
# scripts/install/ (provision-daemon.sh, provision-hooks.sh, stash-scope.sh,
# ...) and the real uninstall-loom.sh are symlinked in so the reinstall gate
# behaves identically to a real checkout, regardless of which code path
# (pre- or post-fix) this test happens to be pointed at. Only
# scripts/install-loom.sh -- the Full Install delegate -- is replaced with a
# stub that always fails, deterministically simulating "the delegated
# workflow failed downstream" without needing a built loom-daemon binary, gh
# auth, or network access.
FAKE_ROOT="$(mktemp -d /tmp/loom-full-reinstall-fakeroot.XXXXXX)"
mkdir -p "$FAKE_ROOT/scripts"
ln -s "$REPO_ROOT/scripts/install" "$FAKE_ROOT/scripts/install"
ln -s "$REPO_ROOT/scripts/uninstall-loom.sh" "$FAKE_ROOT/scripts/uninstall-loom.sh"
ln -s "$REPO_ROOT/install.sh" "$FAKE_ROOT/install.sh"

STUB_MARKER="STUB install-loom.sh: simulating a downstream Full Install failure"
cat > "$FAKE_ROOT/scripts/install-loom.sh" <<EOF
#!/usr/bin/env bash
echo "$STUB_MARKER" >&2
exit 1
EOF
chmod +x "$FAKE_ROOT/scripts/install-loom.sh"

# --- 1b. Stub the toolchain deps install.sh gates on, so this suite is
# host-independent ---
#
# install.sh runs a "Checking System Dependencies" gate (node / pnpm / cargo /
# git) BEFORE it reaches any of the reinstall logic under test. A host missing
# any one of them aborts the run early, and the "stub marker seen" assertion
# below fails for a reason that has nothing to do with the behavior this suite
# exists to pin. That is exactly what happened on the hermetic CI runner, which
# has node/cargo/git but no pnpm -- the suite only passed on developer machines
# that happened to have pnpm installed.
#
# On the --full path under test, install.sh only ever runs `<tool> --version`
# on these before `exec`ing the (stubbed) Full Install delegate, so trivially
# satisfying the gate with version-printing stubs is sound and keeps the suite
# genuinely exercising the reinstall control flow on every host -- strictly
# better than skipping the whole suite wherever pnpm is absent, which would
# have left the #4888 regression uncovered in CI.
#
# `git` is deliberately NOT stubbed: it is used for real (fixture repo, HEAD
# and `git status --porcelain` assertions), which is why it keeps the hard
# `command -v git` skip-guard above instead.
STUB_BIN="$FAKE_ROOT/stub-bin"
mkdir -p "$STUB_BIN"
for _tool in node pnpm cargo; do
  cat > "$STUB_BIN/$_tool" <<EOF
#!/usr/bin/env bash
# Test stub: install.sh's dependency gate only calls \`$_tool --version\`.
echo "$_tool (loom test stub) 0.0.0"
EOF
  chmod +x "$STUB_BIN/$_tool"
done

# --- 2. Seed a scratch target repo with an existing ("older") Loom install ---
TARGET="$(mktemp -d /tmp/loom-full-reinstall-target.XXXXXX)"
git -C "$TARGET" init --quiet
git -C "$TARGET" config user.email "test@test.com"
git -C "$TARGET" config user.name "Test"

mkdir -p "$TARGET/.loom/roles" "$TARGET/.claude"
echo '{"loom_version": "0.1.0"}' > "$TARGET/.loom/install-metadata.json"
echo '{}' > "$TARGET/.loom/config.json"
echo '{}' > "$TARGET/.claude/settings.json"
cat > "$TARGET/CLAUDE.md" <<'EOF'
<!-- BEGIN LOOM ORCHESTRATION -->
This repository uses Loom.
<!-- END LOOM ORCHESTRATION -->

# My Project

Hand-written project docs.
EOF
cat > "$TARGET/.gitignore" <<'EOF'
node_modules/

# BEGIN LOOM
.loom/tokens/
.loom/*.log
# END LOOM
EOF
git -C "$TARGET" add -A
git -C "$TARGET" commit -m "Initial commit with an older Loom install" --quiet

PRE_INSTALL_SHA="$(git -C "$TARGET" rev-parse HEAD)"

# --- 3. Run the (fake-rooted) installer's --full --confirm-reinstall path ---
OUTPUT="$(PATH="$STUB_BIN:$PATH" "$FAKE_ROOT/install.sh" --full --confirm-reinstall -y "$TARGET" < /dev/null 2>&1)"
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
  pass "install run exited non-zero (downstream failure propagated)"
else
  fail "install run exited 0 (expected the stub failure to propagate)" "$OUTPUT"
fi

if [[ "$OUTPUT" == *"$STUB_MARKER"* ]]; then
  pass "run actually reached the Full Install delegation (stub marker seen)"
else
  fail "run never reached the Full Install delegation -- test setup is broken" "$OUTPUT"
fi

# --- 4. Assert the target's tracked tree is completely untouched ---
POST_SHA="$(git -C "$TARGET" rev-parse HEAD)"
if [[ "$POST_SHA" == "$PRE_INSTALL_SHA" ]]; then
  pass "HEAD is unchanged"
else
  fail "HEAD moved (expected $PRE_INSTALL_SHA, got $POST_SHA)"
fi

STATUS_AFTER="$(git -C "$TARGET" status --porcelain)"
if [[ -z "$STATUS_AFTER" ]]; then
  pass "git status is clean -- no staged/unstaged diff from the pre-install HEAD"
else
  fail "git status is NOT clean after the failed full reinstall" "$STATUS_AFTER"
fi

# Belt-and-suspenders: the specific files the reported bug showed as
# half-uninstalled must still carry their original committed content.
for f in .claude/settings.json .loom/install-metadata.json CLAUDE.md .gitignore; do
  if [[ -f "$TARGET/$f" ]] && git -C "$TARGET" diff --quiet -- "$f"; then
    pass "$f present and unmodified"
  else
    fail "$f missing or modified" "$(git -C "$TARGET" status --short -- "$f")"
  fi
done

rm -rf "$FAKE_ROOT" "$TARGET"

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
