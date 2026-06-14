#!/usr/bin/env bash
# test-install-active-session.sh — Unit tests for scripts/install/check-active-session.sh
#
# Exercises each indicator individually (positive + negative), the
# multi-indicator output, and the --allow-active-session flag matrix on
# install-loom.sh.
#
# Per indicator (from issue #3331):
#   1. Daemon PID file: .loom/daemon-loop.pid + alive PID
#   2. Recent active state: .loom/daemon-state.json running=true + mtime <5min
#   3. In-flight builder: .loom/worktrees/issue-N with mtime <5min
#
# Usage:
#   ./defaults/scripts/tests/test-install-active-session.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# defaults/scripts/tests → defaults/scripts → defaults → repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CHECK_SCRIPT="$REPO_ROOT/scripts/install/check-active-session.sh"
INSTALL_SCRIPT="$REPO_ROOT/scripts/install-loom.sh"

# Colors
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
  if [[ -n "${2:-}" ]]; then
    echo "    Detail: $2"
  fi
}

assert_eq() {
  local expected="$1"
  local actual="$2"
  local msg="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass "$msg"
  else
    fail "$msg" "expected='$expected' actual='$actual'"
  fi
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  local msg="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    pass "$msg"
  else
    fail "$msg" "needle='$needle' not found in output"
  fi
}

assert_not_contains() {
  local haystack="$1"
  local needle="$2"
  local msg="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    pass "$msg"
  else
    fail "$msg" "unwanted needle='$needle' present in output"
  fi
}

# Each test creates a fresh tmp dir and reuses it via $TARGET.
TARGET=""
new_target() {
  TARGET=$(mktemp -d "/tmp/loom-active-session-test.XXXXXX")
  mkdir -p "$TARGET/.loom"
}

cleanup_target() {
  if [[ -n "$TARGET" && -d "$TARGET" ]]; then
    rm -rf "$TARGET"
  fi
  TARGET=""
}

trap '[[ -n "$TARGET" ]] && rm -rf "$TARGET"' EXIT

# Portable mtime backdating: set a file's mtime to ($EPOCH - $seconds_ago).
# Uses `touch -d @timestamp` on GNU (Linux) and `touch -t YYYYMMDDhhmm.ss` on BSD/macOS.
backdate() {
  local path="$1"
  local seconds_ago="$2"
  local target_epoch
  target_epoch=$(( $(date +%s) - seconds_ago ))

  if touch -d "@$target_epoch" "$path" 2>/dev/null; then
    return 0
  fi
  # BSD touch (macOS): -t [[CC]YY]MMDDhhmm[.SS]
  local stamp
  stamp=$(date -r "$target_epoch" +%Y%m%d%H%M.%S 2>/dev/null || true)
  if [[ -n "$stamp" ]]; then
    touch -t "$stamp" "$path"
  else
    echo "ERROR: cannot backdate $path" >&2
    return 1
  fi
}

# Run the check-active-session helper and capture exit code + stderr.
run_check() {
  local target="$1"
  local stderr_file
  stderr_file=$(mktemp)
  local exit_code=0
  "$CHECK_SCRIPT" "$target" 2>"$stderr_file" || exit_code=$?
  CHECK_STDERR=$(cat "$stderr_file")
  rm -f "$stderr_file"
  CHECK_EXIT=$exit_code
}

# ───────────────────────────────────────────────────────────────────────────
# Preconditions
# ───────────────────────────────────────────────────────────────────────────
if [[ ! -x "$CHECK_SCRIPT" ]]; then
  echo "ERROR: check script not executable: $CHECK_SCRIPT" >&2
  exit 1
fi

if [[ ! -x "$INSTALL_SCRIPT" ]]; then
  echo "ERROR: install script not executable: $INSTALL_SCRIPT" >&2
  exit 1
fi

# ───────────────────────────────────────────────────────────────────────────
# Test 1: No .loom/ at all → silent, exit 0 (first-time install)
# ───────────────────────────────────────────────────────────────────────────
echo "Test 1: missing .loom/ directory exits 0 silently"
TARGET=$(mktemp -d "/tmp/loom-active-session-test.XXXXXX")
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "no .loom/ → exit 0"
assert_eq "" "$CHECK_STDERR" "no .loom/ → silent stderr"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 2: empty .loom/ → silent, exit 0 (happy path)
# ───────────────────────────────────────────────────────────────────────────
echo "Test 2: empty .loom/ directory exits 0 silently"
new_target
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "empty .loom/ → exit 0"
assert_eq "" "$CHECK_STDERR" "empty .loom/ → silent stderr"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 3: Indicator 1 positive — live PID file
# ───────────────────────────────────────────────────────────────────────────
echo "Test 3: live daemon PID file fires indicator 1"
new_target
# $$ is this shell's PID — guaranteed alive
echo "$$" > "$TARGET/.loom/daemon-loop.pid"
run_check "$TARGET"
assert_eq "1" "$CHECK_EXIT" "live PID → exit 1"
assert_contains "$CHECK_STDERR" "Daemon PID file present" "live PID → indicator 1 message"
assert_contains "$CHECK_STDERR" "PID $$ alive" "live PID → exact PID reported"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 4: Indicator 1 negative — stale PID file
# ───────────────────────────────────────────────────────────────────────────
echo "Test 4: stale PID file does NOT fire indicator 1"
new_target
# 2^22 = 4194304: very unlikely to exist; macOS PIDs max out lower than this
echo "4194303" > "$TARGET/.loom/daemon-loop.pid"
# Verify the PID really is dead before asserting
if kill -0 4194303 2>/dev/null || ps -p 4194303 >/dev/null 2>&1; then
  echo "  SKIP: PID 4194303 unexpectedly exists on this host; cannot test stale PID negative"
else
  run_check "$TARGET"
  assert_eq "0" "$CHECK_EXIT" "stale PID → exit 0"
  assert_not_contains "$CHECK_STDERR" "Daemon PID file present" "stale PID → no indicator 1"
fi
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 5: Indicator 1 negative — non-numeric PID content
# ───────────────────────────────────────────────────────────────────────────
echo "Test 5: garbage PID file content does NOT fire indicator 1"
new_target
echo "not-a-pid" > "$TARGET/.loom/daemon-loop.pid"
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "garbage PID → exit 0"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 6: Indicator 2 positive — running=true + fresh mtime
# ───────────────────────────────────────────────────────────────────────────
echo "Test 6: running=true + fresh state fires indicator 2"
new_target
cat > "$TARGET/.loom/daemon-state.json" <<'JSON'
{
  "running": true,
  "iteration": 42
}
JSON
run_check "$TARGET"
assert_eq "1" "$CHECK_EXIT" "fresh running=true state → exit 1"
assert_contains "$CHECK_STDERR" "Active daemon state" "fresh running=true → indicator 2 message"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 7: Indicator 2 negative — running=true but stale mtime
# ───────────────────────────────────────────────────────────────────────────
echo "Test 7: running=true but stale mtime does NOT fire indicator 2"
new_target
cat > "$TARGET/.loom/daemon-state.json" <<'JSON'
{
  "running": true,
  "iteration": 42
}
JSON
# Backdate to 1 hour ago (well beyond 5 minutes)
backdate "$TARGET/.loom/daemon-state.json" 3600
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "stale running=true → exit 0"
assert_not_contains "$CHECK_STDERR" "Active daemon state" "stale running=true → no indicator 2"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 8: Indicator 2 negative — running=false + fresh mtime
# ───────────────────────────────────────────────────────────────────────────
echo "Test 8: running=false + fresh state does NOT fire indicator 2"
new_target
cat > "$TARGET/.loom/daemon-state.json" <<'JSON'
{
  "running": false,
  "iteration": 42
}
JSON
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "running=false fresh → exit 0"
assert_not_contains "$CHECK_STDERR" "Active daemon state" "running=false → no indicator 2"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 9: Indicator 3 positive — fresh issue worktree dir
# ───────────────────────────────────────────────────────────────────────────
echo "Test 9: fresh issue worktree fires indicator 3"
new_target
mkdir -p "$TARGET/.loom/worktrees/issue-42"
run_check "$TARGET"
assert_eq "1" "$CHECK_EXIT" "fresh worktree → exit 1"
assert_contains "$CHECK_STDERR" "In-flight builder worktrees" "fresh worktree → indicator 3 message"
assert_contains "$CHECK_STDERR" "issue-42" "fresh worktree → name reported"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 10: Indicator 3 negative — stale worktree
# ───────────────────────────────────────────────────────────────────────────
echo "Test 10: stale issue worktree does NOT fire indicator 3"
new_target
mkdir -p "$TARGET/.loom/worktrees/issue-99"
backdate "$TARGET/.loom/worktrees/issue-99" 3600
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "stale worktree → exit 0"
assert_not_contains "$CHECK_STDERR" "In-flight builder worktrees" "stale worktree → no indicator 3"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 11: Indicator 3 negative — worktrees/ directory empty
# ───────────────────────────────────────────────────────────────────────────
echo "Test 11: empty worktrees/ directory does NOT fire indicator 3"
new_target
mkdir -p "$TARGET/.loom/worktrees"
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "empty worktrees/ → exit 0"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 12: Indicator 3 negative — non-matching subdirectory (e.g. terminal-1)
# ───────────────────────────────────────────────────────────────────────────
echo "Test 12: non-issue worktree (terminal-1) does NOT fire indicator 3"
new_target
mkdir -p "$TARGET/.loom/worktrees/terminal-1"
run_check "$TARGET"
assert_eq "0" "$CHECK_EXIT" "terminal-1 worktree → exit 0"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 13: Multi-indicator — all three positive, all three reported
# ───────────────────────────────────────────────────────────────────────────
echo "Test 13: all three indicators fire and are all reported"
new_target
echo "$$" > "$TARGET/.loom/daemon-loop.pid"
cat > "$TARGET/.loom/daemon-state.json" <<'JSON'
{
  "running": true,
  "iteration": 7
}
JSON
mkdir -p "$TARGET/.loom/worktrees/issue-42"
mkdir -p "$TARGET/.loom/worktrees/issue-43"
run_check "$TARGET"
assert_eq "1" "$CHECK_EXIT" "all 3 → exit 1"
assert_contains "$CHECK_STDERR" "Daemon PID file present" "multi → indicator 1 reported"
assert_contains "$CHECK_STDERR" "Active daemon state" "multi → indicator 2 reported"
assert_contains "$CHECK_STDERR" "In-flight builder worktrees" "multi → indicator 3 reported"
assert_contains "$CHECK_STDERR" "Reason:" "multi → trailing reason present"
cleanup_target

# ───────────────────────────────────────────────────────────────────────────
# Test 14: install-loom.sh --help documents --allow-active-session
# ───────────────────────────────────────────────────────────────────────────
echo "Test 14: install-loom.sh --help documents the new flag"
set +e
HELP_OUTPUT=$("$INSTALL_SCRIPT" --help 2>&1)
HELP_EXIT=$?
set -e
assert_eq "0" "$HELP_EXIT" "install-loom.sh --help → exit 0"
assert_contains "$HELP_OUTPUT" "--allow-active-session" "--help mentions --allow-active-session"
assert_contains "$HELP_OUTPUT" "does NOT bypass active-session" \
  "--help notes that --force does NOT bypass the guard"

# ───────────────────────────────────────────────────────────────────────────
# Test 15: install-loom.sh --allow-active-session is parsed without error
# ───────────────────────────────────────────────────────────────────────────
# We exercise the argument parser without performing a full install. The
# easiest hermetic way to do this is to pass --help alongside --allow-active-session:
# the parser must accept the flag before the help branch runs.
echo "Test 15: install-loom.sh accepts --allow-active-session in argv"
set +e
FLAG_OUTPUT=$("$INSTALL_SCRIPT" --allow-active-session --help 2>&1)
FLAG_EXIT=$?
set -e
assert_eq "0" "$FLAG_EXIT" "install-loom.sh --allow-active-session --help → exit 0"
assert_contains "$FLAG_OUTPUT" "Usage:" "flag combo prints usage (parser accepted flag)"

# ───────────────────────────────────────────────────────────────────────────
# Test 16: usage error — no target path
# ───────────────────────────────────────────────────────────────────────────
echo "Test 16: check-active-session.sh with no args → exit 2"
set +e
USAGE_OUTPUT=$("$CHECK_SCRIPT" 2>&1)
USAGE_EXIT=$?
set -e
assert_eq "2" "$USAGE_EXIT" "no args → exit 2"
assert_contains "$USAGE_OUTPUT" "Usage:" "no args → usage line emitted"

# ───────────────────────────────────────────────────────────────────────────
# Test 17: usage error — nonexistent target path
# ───────────────────────────────────────────────────────────────────────────
echo "Test 17: check-active-session.sh with nonexistent target → exit 2"
set +e
NX_OUTPUT=$("$CHECK_SCRIPT" "/this/path/does/not/exist/xyzzy-$RANDOM" 2>&1)
NX_EXIT=$?
set -e
assert_eq "2" "$NX_EXIT" "nonexistent target → exit 2"
assert_contains "$NX_OUTPUT" "does not exist" "nonexistent target → diagnostic message"

# ───────────────────────────────────────────────────────────────────────────
# Summary
# ───────────────────────────────────────────────────────────────────────────
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"

if (( TESTS_FAILED > 0 )); then
  exit 1
fi
exit 0
