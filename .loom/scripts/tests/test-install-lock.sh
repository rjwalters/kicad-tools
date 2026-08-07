#!/usr/bin/env bash
# test-install-lock.sh — per-target install lock regression tests (issue #4928).
#
# Before the fix, nothing in install.sh detected or prevented a SECOND
# installer running against the same target while a first one was mid-flight.
# In the reported incident (2026-08-01) a caller concluded a silent
# `--quick --yes --confirm-reinstall` run had died, restored the target from
# git, and started a second installer — which ran concurrently with the
# still-alive first one. The only thing that serialized them was cargo's
# build-directory lock, by luck; the uninstall-phase deletions of one run and
# the copy phase of the other could just as easily have corrupted the target.
#
# The fix (scripts/install/install-lock.sh) takes a PID-bearing lock file at
# <target>/.loom/.install.lock before any destructive phase of the --quick /
# --clean paths, releases it from an EXIT trap, and reclaims it automatically
# when the recorded PID is no longer alive.
#
# Strategy — two layers:
#   1. Unit tests that source the real helper directly (mirroring
#      test-install-stash-scope.sh) and pin acquire/reclaim/phase/release
#      semantics deterministically, including the cross-host and
#      malformed-lock edge cases.
#   2. End-to-end tests that run the REAL install.sh from a throwaway "fake
#      LOOM_ROOT" (mirroring test-install-full-reinstall-no-target-mutation.sh)
#      and assert that a held lock stops the second installer before it
#      touches the target, that a stale lock does not block, and that the lock
#      is released on both a successful run and an `error()`-triggered exit.
#
# Usage:
#   bash defaults/scripts/tests/test-install-lock.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# In the source checkout this file lives at defaults/scripts/tests/; the repo
# root is three levels up. This suite is a source-checkout artifact and expects
# the real scripts/install/ and install.sh.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
LOCK_LIB="$REPO_ROOT/scripts/install/install-lock.sh"
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

assert_contains() {
  local haystack="$1" needle="$2" msg="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    pass "$msg"
  else
    fail "$msg" "expected to find: [$needle]
in:
$haystack"
  fi
}

assert_not_contains() {
  local haystack="$1" needle="$2" msg="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    pass "$msg"
  else
    fail "$msg" "did NOT expect to find: [$needle]
in:
$haystack"
  fi
}

for f in "$LOCK_LIB" "$INSTALL_SH"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: $f not found" >&2
    exit 1
  fi
done

if ! command -v git &> /dev/null; then
  echo -e "${YELLOW}SKIP${NC}: missing required dependency 'git'"
  exit 0
fi

echo "================================================================"
echo "Per-target install lock (issue #4928)"
echo "================================================================"
echo ""

WORKDIR="$(mktemp -d /tmp/loom-install-lock.XXXXXX)"

# NOTE: deliberately no EXIT trap for cleanup. Background jobs inherit a shell's
# EXIT trap, so the sleeper below would run it when killed — deleting $WORKDIR
# out from under the still-running suite. Cleanup happens at the end instead
# (matching the sibling install suites).

# A definitely-live PID: a sleeper we own for the duration of the suite.
sleep 600 &
LIVE_PID=$!

# A definitely-dead PID: a short-lived child, confirmed reaped. Retry a few
# times in the (vanishingly unlikely) event the PID is immediately recycled.
DEAD_PID=""
for _ in 1 2 3 4 5; do
  _candidate="$(bash -c 'echo $$')"
  if ! kill -0 "$_candidate" 2>/dev/null && ! ps -p "$_candidate" >/dev/null 2>&1; then
    DEAD_PID="$_candidate"
    break
  fi
done
if [[ -z "$DEAD_PID" ]]; then
  echo -e "${YELLOW}SKIP${NC}: could not obtain a reliably-dead PID on this host"
  rm -rf "$WORKDIR"
  kill "$LIVE_PID" 2>/dev/null
  exit 0
fi

THIS_HOST="$(hostname 2>/dev/null || uname -n 2>/dev/null || echo unknown-host)"

# Write a lock file by hand, standing in for another installer's lock.
write_lock() {
  local file="$1" pid="$2" host="$3" phase="$4" epoch="${5:-$(date +%s)}"
  mkdir -p "$(dirname "$file")"
  {
    echo "# Loom install lock (issue #4928)."
    echo "pid=$pid"
    echo "host=$host"
    echo "started=2026-08-01T00:00:00Z"
    echo "started_epoch=$epoch"
    echo "phase=$phase"
    echo "target=$(dirname "$(dirname "$file")")"
  } > "$file"
}

# Fresh scratch target directory for one scenario.
new_target() {
  local d
  d="$(mktemp -d "$WORKDIR/target.XXXXXX")"
  echo "$d"
}

# ===============================================================
# Part 1 — unit tests against the real helper
# ===============================================================
echo "--- Unit: acquire / reclaim / phase / release ---"

# 1. A fresh acquire creates the lock, records this PID, and creates .loom/.
T="$(new_target)"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" && echo "ACQUIRED"
)"
assert_contains "$OUT" "ACQUIRED" "fresh acquire succeeds"
if [[ -f "$T/.loom/.install.lock" ]]; then
  pass "lock file created at <target>/.loom/.install.lock"
else
  fail "lock file was not created at $T/.loom/.install.lock"
fi
assert_contains "$(cat "$T/.loom/.install.lock")" "phase=preparing" "lock records the phase"
assert_contains "$(cat "$T/.loom/.install.lock")" "host=$THIS_HOST" "lock records the host"

# 2. A lock held by a LIVE pid blocks a second acquire, and is not stolen.
T="$(new_target)"
write_lock "$T/.loom/.install.lock" "$LIVE_PID" "$THIS_HOST" "uninstalling"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" 2>&1 && echo "ACQUIRED"
)"
assert_not_contains "$OUT" "ACQUIRED" "acquire fails while a live installer holds the lock"
assert_contains "$OUT" "Another Loom install is already running" "failure names the conflict"
assert_contains "$OUT" "Held by pid $LIVE_PID" "failure names the live owning pid"
assert_contains "$OUT" "$T/.loom/.install.lock" "failure names the lock path"
if [[ -f "$T/.loom/.install.lock" ]]; then
  pass "the live installer's lock is left intact"
else
  fail "the live installer's lock was stolen/removed"
fi

# 3. A lock whose pid is dead is reclaimed rather than blocking forever.
T="$(new_target)"
write_lock "$T/.loom/.install.lock" "$DEAD_PID" "$THIS_HOST" "preparing"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" 2>&1 && echo "ACQUIRED"
)"
assert_contains "$OUT" "ACQUIRED" "stale (dead pid) lock is reclaimed"
assert_contains "$OUT" "Reclaiming stale Loom install lock" "reclaim is announced"
assert_contains "$OUT" "no longer running" "reclaim names the dead-pid reason"

# 4. Reclaiming a lock interrupted mid-uninstall surfaces recovery guidance
#    (the AC #3 "half-uninstalled window is explicit in state" contract).
T="$(new_target)"
write_lock "$T/.loom/.install.lock" "$DEAD_PID" "$THIS_HOST" "uninstalling"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" 2>&1 && echo "ACQUIRED"
)"
assert_contains "$OUT" "ACQUIRED" "interrupted-run lock does not block the retry"
assert_contains "$OUT" "interrupted during the 'uninstalling' phase" "names the interrupted phase"
assert_contains "$OUT" "PARTIALLY UNINSTALLED" "warns the target may be half-uninstalled"
assert_contains "$OUT" "git -C \"$T\" status --short" "prints a concrete recovery command"

# 5. A lock with no parseable pid is treated as malformed and reclaimed.
T="$(new_target)"
mkdir -p "$T/.loom"
echo "# garbage" > "$T/.loom/.install.lock"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" 2>&1 && echo "ACQUIRED"
)"
assert_contains "$OUT" "ACQUIRED" "malformed lock is reclaimed"
assert_contains "$OUT" "no owning pid" "malformed reclaim names the reason"

# 6. A recent lock owned by ANOTHER host is respected (pid liveness is not
#    probeable across hosts), but an old one is reclaimed rather than wedging
#    the target forever.
T="$(new_target)"
write_lock "$T/.loom/.install.lock" "999999" "some-other-host" "installing"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" 2>&1 && echo "ACQUIRED"
)"
assert_not_contains "$OUT" "ACQUIRED" "recent foreign-host lock is respected"
assert_contains "$OUT" "some-other-host" "foreign-host failure names the host"

T="$(new_target)"
write_lock "$T/.loom/.install.lock" "999999" "some-other-host" "installing" "$(( $(date +%s) - 999999 ))"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" 2>&1 && echo "ACQUIRED"
)"
assert_contains "$OUT" "ACQUIRED" "long-abandoned foreign-host lock is reclaimed"

# 7. set_install_lock_phase rewrites the phase in place; release removes the
#    lock; release also drops a .loom/ the lock itself created.
T="$(new_target)"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" >/dev/null 2>&1 || exit 1
  set_install_lock_phase "uninstalling"
  cat "$T/.loom/.install.lock"
  release_install_lock
  [[ -e "$T/.loom/.install.lock" ]] && echo "LOCK-STILL-PRESENT"
  [[ -d "$T/.loom" ]] && echo "LOOM-DIR-STILL-PRESENT"
  echo "DONE"
)"
assert_contains "$OUT" "phase=uninstalling" "set_install_lock_phase updates the recorded phase"
assert_not_contains "$OUT" "LOCK-STILL-PRESENT" "release removes the lock file"
assert_not_contains "$OUT" "LOOM-DIR-STILL-PRESENT" "release drops a .loom/ it created itself"

# 8. release never removes a lock that now belongs to a DIFFERENT process
#    (e.g. this run was stopped long enough to be reclaimed).
T="$(new_target)"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" >/dev/null 2>&1 || exit 1
  # Another installer reclaimed and re-took the lock.
  printf 'pid=%s\nhost=%s\nphase=installing\n' "$LIVE_PID" "$THIS_HOST" > "$T/.loom/.install.lock"
  release_install_lock
  [[ -f "$T/.loom/.install.lock" ]] && echo "FOREIGN-LOCK-PRESERVED"
  echo "DONE"
)"
assert_contains "$OUT" "FOREIGN-LOCK-PRESERVED" "release leaves another process's lock alone"

# 9. A second acquire from the same process is idempotent (phase update only).
T="$(new_target)"
OUT="$(
  source "$LOCK_LIB"
  acquire_install_lock "$T" "preparing" >/dev/null 2>&1 || exit 1
  acquire_install_lock "$T" "installing" >/dev/null 2>&1 && echo "RE-ACQUIRED"
  cat "$T/.loom/.install.lock"
)"
assert_contains "$OUT" "RE-ACQUIRED" "re-acquiring an already-held lock succeeds"
assert_contains "$OUT" "phase=installing" "re-acquire updates the phase"

# ===============================================================
# Part 2 — end-to-end through the real install.sh
# ===============================================================
echo ""
echo "--- End-to-end: install.sh --quick ---"

# Build a fake LOOM_ROOT: the REAL install.sh + real helper scripts, with only
# the pieces that need network/toolchain replaced by stubs. Same construction
# as test-install-full-reinstall-no-target-mutation.sh.
FAKE_ROOT="$WORKDIR/fakeroot"
mkdir -p "$FAKE_ROOT/scripts" "$FAKE_ROOT/target/release"
ln -s "$REPO_ROOT/scripts/install" "$FAKE_ROOT/scripts/install"
ln -s "$REPO_ROOT/scripts/uninstall-loom.sh" "$FAKE_ROOT/scripts/uninstall-loom.sh"
ln -s "$REPO_ROOT/install.sh" "$FAKE_ROOT/install.sh"
ln -s "$REPO_ROOT/defaults" "$FAKE_ROOT/defaults"
cat > "$FAKE_ROOT/scripts/install-loom.sh" <<'EOF'
#!/usr/bin/env bash
echo "STUB install-loom.sh (should not be reached by these tests)" >&2
exit 1
EOF
chmod +x "$FAKE_ROOT/scripts/install-loom.sh"

# install.sh gates on node/pnpm/cargo before any of the logic under test, so
# stub them (version printers) to keep the suite host-independent. `git` is
# used for real and keeps its hard skip-guard above.
STUB_BIN="$FAKE_ROOT/stub-bin"
mkdir -p "$STUB_BIN"
for _tool in node pnpm cargo; do
  cat > "$STUB_BIN/$_tool" <<EOF
#!/usr/bin/env bash
echo "$_tool (loom test stub) 0.0.0"
EOF
  chmod +x "$STUB_BIN/$_tool"
done

# Stub loom-daemon. \`init\` writes just enough for install.sh's post-init
# steps (finalize_quick_install / verify_install) to run; LOOM_DAEMON_FAIL=1
# makes it fail instead, standing in for a downstream install failure.
cat > "$FAKE_ROOT/target/release/loom-daemon" <<'EOF'
#!/usr/bin/env bash
if [[ "${LOOM_DAEMON_FAIL:-0}" == "1" ]]; then
  echo "STUB loom-daemon: simulated init failure" >&2
  exit 1
fi
[[ "${1:-}" == "init" ]] || exit 0
target=""
for arg in "$@"; do target="$arg"; done
mkdir -p "$target/.loom/scripts/lib" "$target/.loom/roles"
echo '{"terminals": []}' > "$target/.loom/config.json"
echo '#!/usr/bin/env bash' > "$target/.loom/scripts/worktree.sh"
echo '#!/usr/bin/env bash' > "$target/.loom/scripts/lib/loom-tools.sh"
printf '# Loom\n\n**Loom Version**: %s\n' "${LOOM_VERSION:-unknown}" > "$target/.loom/CLAUDE.md"
echo "STUB loom-daemon: init wrote $target/.loom"
EOF
chmod +x "$FAKE_ROOT/target/release/loom-daemon"

# Seed a scratch target repo carrying an existing ("older") Loom install.
seed_target() {
  local t="$1"
  git -C "$t" init --quiet
  git -C "$t" config user.email "test@test.com"
  git -C "$t" config user.name "Test"
  mkdir -p "$t/.loom/roles" "$t/.claude"
  echo '{"loom_version": "0.1.0"}' > "$t/.loom/install-metadata.json"
  echo '{}' > "$t/.loom/config.json"
  echo '{}' > "$t/.claude/settings.json"
  cat > "$t/CLAUDE.md" <<'CMD'
<!-- BEGIN LOOM ORCHESTRATION -->
This repository uses Loom.
<!-- END LOOM ORCHESTRATION -->

# My Project
CMD
  printf 'node_modules/\n' > "$t/.gitignore"
  git -C "$t" add -A
  git -C "$t" commit -m "Initial commit with an older Loom install" --quiet
}

# Run install.sh from the fake root with a scratch HOME (so user-scope hook
# provisioning and machine-daemon provisioning cannot touch the real one).
run_installer() {
  local target="$1"; shift
  local fake_home="$WORKDIR/home"
  mkdir -p "$fake_home"
  env PATH="$STUB_BIN:$PATH" HOME="$fake_home" \
    LOOM_DAEMON_BIN_DIR="$fake_home/.local/bin" \
    LOOM_DAEMON_FAIL="${LOOM_DAEMON_FAIL:-0}" \
    "$FAKE_ROOT/install.sh" "$@" "$target" < /dev/null 2>&1
}

# --- E2E 1: a held lock stops the second installer before it mutates anything.
T="$(new_target)"
seed_target "$T"
PRE_SHA="$(git -C "$T" rev-parse HEAD)"
write_lock "$T/.loom/.install.lock" "$LIVE_PID" "$THIS_HOST" "uninstalling"

OUT="$(run_installer "$T" --quick --confirm-reinstall -y)"
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
  pass "second installer exits non-zero while the lock is held"
else
  fail "second installer exited 0 (expected a fast failure)" "$OUT"
fi
assert_contains "$OUT" "Another Loom install is already running" "e2e: names the concurrent run"
assert_contains "$OUT" "Held by pid $LIVE_PID" "e2e: names the live pid"
assert_contains "$OUT" "$T/.loom/.install.lock" "e2e: names the lock path"

if [[ -f "$T/.loom/.install.lock" ]]; then
  pass "e2e: the held lock is left intact"
else
  fail "e2e: the held lock was removed by the blocked installer"
fi
# The lock file this test planted is itself untracked in the scratch repo (a
# real install ships a .gitignore entry for it), so filter it out.
STATUS_AFTER="$(git -C "$T" status --porcelain | grep -v '\.loom/\.install\.lock$')"
if [[ "$(git -C "$T" rev-parse HEAD)" == "$PRE_SHA" ]] && [[ -z "$STATUS_AFTER" ]]; then
  pass "e2e: target tree untouched — the uninstall phase never started"
else
  fail "e2e: target was mutated despite the held lock" "$STATUS_AFTER"
fi
assert_not_contains "$OUT" "Uninstalling existing Loom installation" "e2e: uninstall never ran"

# --- E2E 2: a stale lock does not block, and an error() exit releases the lock.
T="$(new_target)"
seed_target "$T"
write_lock "$T/.loom/.install.lock" "$DEAD_PID" "$THIS_HOST" "uninstalling"

export LOOM_DAEMON_FAIL=1
OUT="$(run_installer "$T" --quick --confirm-reinstall -y)"
EXIT_CODE=$?
unset LOOM_DAEMON_FAIL

assert_contains "$OUT" "Reclaiming stale Loom install lock" "e2e: stale lock reclaimed, not blocking"
assert_contains "$OUT" "Entering the destructive uninstall" "e2e: the destructive window is announced"
if [[ $EXIT_CODE -ne 0 ]]; then
  pass "e2e: the simulated init failure propagates (non-zero exit)"
else
  fail "e2e: expected the simulated init failure to propagate" "$OUT"
fi
assert_contains "$OUT" "PARTIALLY UNINSTALLED" "e2e: failure inside the destructive window prints recovery guidance"
if [[ ! -e "$T/.loom/.install.lock" ]]; then
  pass "e2e: lock released on an error()-triggered early exit"
else
  fail "e2e: lock survived an error() exit (would wedge the next install)"
fi

# --- E2E 3: a successful run releases the lock and leaves no lock artifacts.
T="$(new_target)"
git -C "$T" init --quiet
git -C "$T" config user.email "test@test.com"
git -C "$T" config user.name "Test"
printf 'node_modules/\n' > "$T/.gitignore"
git -C "$T" add -A
git -C "$T" commit -m "Empty project, no Loom install" --quiet

OUT="$(run_installer "$T" --quick -y)"
EXIT_CODE=$?

if [[ $EXIT_CODE -eq 0 ]]; then
  pass "e2e: fresh quick install succeeds"
else
  fail "e2e: fresh quick install failed" "$OUT"
fi
if [[ ! -e "$T/.loom/.install.lock" ]]; then
  pass "e2e: lock released on normal successful completion"
else
  fail "e2e: lock left behind after a successful install"
fi
if [[ -z "$(find "$T/.loom" -name '.install.lock*' 2>/dev/null)" ]]; then
  pass "e2e: no lock tmp sidecar left behind"
else
  fail "e2e: a .install.lock* sidecar was left behind" "$(find "$T/.loom" -name '.install.lock*')"
fi

kill "$LIVE_PID" 2>/dev/null
wait "$LIVE_PID" 2>/dev/null
rm -rf "$WORKDIR"

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
