#!/usr/bin/env bash
# test-build-slot.sh — Tests for the machine-wide build-slot lease (#4512)
#
# Covers defaults/scripts/lib/build-slot.sh, the Bash half of the cross-process
# lock that replaced the CPU-headroom admission term:
#
#   1. acquire/release round-trip — the slot lock dir appears and disappears.
#   2. serialization — a second acquire cannot take the only slot.
#   3. BOUNDED WAIT + DEGRADE OPEN — a contended acquire returns 0 with no slot
#      after the bound instead of blocking forever (the "never a deadlock" AC).
#   4. degrade open when the lock store is unusable (a FILE where the slot dir
#      should be).
#   5. LOOM_BUILD_SLOTS=0 disables serialization entirely.
#   6. multi-slot budget — 2 slots admit 2 holders, refuse the 3rd.
#   7. stale reaping — an abandoned slot older than the threshold is reclaimed.
#   8. re-entrancy — LOOM_BUILD_SLOT_HELD makes a nested acquire a no-op.
#   9. path shape parity with `loom_daemon::build_slot::slot_path` ("$dir/slot-N").
#
# Style follows test-disk-headroom.sh: throwaway mktemp dirs, assert harness,
# no network, no real ~/.loom.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_SLOT_LIB="$SCRIPTS_DIR/lib/build-slot.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}
assert_true() { if [[ "$1" == "0" ]]; then pass "$2"; else fail "$2 (expected success, got rc=$1)"; fi; }

# NOTE on the `( … ) && CASE_RC=0 || CASE_RC=$?` idiom below: every case runs in
# a subshell so its env exports (LOOM_BUILD_SLOT_DIR/HELD) cannot leak into the
# next one, and the rc is captured explicitly so a FAILING case reports as a test
# failure instead of aborting the whole suite under `set -e`.

# shellcheck source=../lib/build-slot.sh
source "$BUILD_SLOT_LIB"

TMPROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMPROOT"; }
trap cleanup EXIT

# Every test gets a fresh slot dir and a fast bound so the suite stays quick.
new_slot_dir() {
    local d
    d="$(mktemp -d "$TMPROOT/slots.XXXXXX")"
    printf '%s\n' "$d/build-slot"
}

export LOOM_BUILD_SLOT_WAIT_SECS=1
unset LOOM_BUILD_SLOT_HELD LOOM_BUILD_SLOTS LOOM_BUILD_SLOT_STALE_SECS || true
# #11014: a suite-wide default, so a case that forgets its own LOOM_BUILD_SLOT_DIR
# lands in the temp root, never in the host's ~/.loom/locks/build-slot.
export LOOM_BUILD_SLOT_DIR="$TMPROOT/default/build-slot"

echo ""
echo "=== build-slot.sh: acquire / release round-trip ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    rc=0
    loom_build_slot_acquire "unit" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -n "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    [[ -d "$LOOM_BUILD_SLOT_PATH" ]] || exit 3
    [[ "$LOOM_BUILD_SLOT_HELD" == "1" ]] || exit 4
    loom_build_slot_release >/dev/null 2>&1
    [[ -d "$DIR/slot-0" ]] && exit 5
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "acquire creates slot-0 + exports LOOM_BUILD_SLOT_HELD; release removes it"

echo ""
echo "=== build-slot.sh: serialization + bounded wait (never a deadlock) ==="

DIR="$(new_slot_dir)"
mkdir -p "$DIR"
# Simulate a live holder by taking slot-0 directly (fresh mtime ⇒ not stale).
mkdir "$DIR/slot-0"
START="$(date +%s)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    rc=0
    OUT="$(loom_build_slot_acquire "contended" 2>&1)" || rc=$?
    # Must SUCCEED (degrade open), hold no slot, and say why.
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "waiting up to" <<<"$OUT" || exit 3
    grep -q "degrading open" <<<"$OUT" || exit 4
    # It must NOT have claimed to cover children (that would let a nested
    # acquire skip a slot it never took).
    [[ -z "${LOOM_BUILD_SLOT_HELD:-}" ]] || exit 5
    exit 0
) && RC=0 || RC=$?
ELAPSED=$(( $(date +%s) - START ))
assert_true "$RC" "a contended acquire waits the bound, then degrades open (rc=0, no slot, telemetry)"
if (( ELAPSED >= 1 && ELAPSED < 15 )); then
    pass "the wait is bounded (${ELAPSED}s, bound 1s) — no deadlock"
else
    fail "the wait was not bounded as expected (${ELAPSED}s)"
fi
rmdir "$DIR/slot-0"

echo ""
echo "=== build-slot.sh: degrades open when the lock store is unusable ==="

DIR="$(new_slot_dir)"
# A FILE where the slot directory should be ⇒ mkdir -p fails.
printf 'x' > "$DIR"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    rc=0
    OUT="$(loom_build_slot_acquire "broken-store" 2>&1)" || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "unusable" <<<"$OUT" || exit 3
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "an unusable slot dir degrades open (never a failure, never a spin)"

echo ""
echo "=== build-slot.sh: LOOM_BUILD_SLOTS=0 disables serialization ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOTS=0
    rc=0
    OUT="$(loom_build_slot_acquire "opted-out" 2>&1)" || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "disabled" <<<"$OUT" || exit 3
    [[ -e "$DIR" ]] && exit 4   # must not even create the dir
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_BUILD_SLOTS=0 opts out cleanly (no dir, no slot, rc=0)"

echo ""
echo "=== build-slot.sh: multi-slot budget ==="

DIR="$(new_slot_dir)"
mkdir -p "$DIR"
mkdir "$DIR/slot-0"    # holder A
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOTS=2
    rc=0
    loom_build_slot_acquire "second-of-two" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ "$LOOM_BUILD_SLOT_PATH" == "$DIR/slot-1" ]] || exit 2
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "with 2 slots, a second holder takes slot-1"

# The subshell above exited WITHOUT releasing (no trap), exactly like a crashed
# holder, so slot-1 is still held here — that is the state the third holder must
# face. Both slots are now occupied.
[[ -d "$DIR/slot-1" ]] || fail "slot-1 should still be held by the previous (unreleased) holder"
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOTS=2
    rc=0
    loom_build_slot_acquire "third-of-two" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "with 2 slots, a third holder gets none (degrades open after the bound)"
rmdir "$DIR/slot-0" "$DIR/slot-1"

echo ""
echo "=== build-slot.sh: stale slot from a crashed holder is reaped ==="

DIR="$(new_slot_dir)"
mkdir -p "$DIR"
mkdir "$DIR/slot-0"
sleep 1   # let its mtime age past the 0-second-ish threshold below
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOT_STALE_SECS=1
    rc=0
    loom_build_slot_acquire "after-crash" >/dev/null 2>&1 || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ "$LOOM_BUILD_SLOT_PATH" == "$DIR/slot-0" ]] || exit 2
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a slot older than LOOM_BUILD_SLOT_STALE_SECS is reclaimed"

echo ""
echo "=== build-slot.sh: re-entrancy ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR" LOOM_BUILD_SLOT_HELD=1
    rc=0
    OUT="$(loom_build_slot_acquire "nested" 2>&1)" || rc=$?
    [[ "$rc" == "0" ]] || exit 1
    [[ -z "$LOOM_BUILD_SLOT_PATH" ]] || exit 2
    grep -q "re-entrant" <<<"$OUT" || exit 3
    [[ -e "$DIR/slot-0" ]] && exit 4
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_BUILD_SLOT_HELD=1 makes a nested acquire a no-op (no second slot taken)"

echo ""
echo "=== build-slot.sh: cross-language path shape parity ==="

DIR="$(new_slot_dir)"
(
    export LOOM_BUILD_SLOT_DIR="$DIR"
    loom_build_slot_acquire "shape" >/dev/null 2>&1
    # `loom_daemon::build_slot::slot_path` composes exactly "$dir/slot-$i"; a
    # divergence here silently stops the daemon gate and the sweep-side gate
    # from serializing against each other.
    [[ "$LOOM_BUILD_SLOT_PATH" == "$DIR/slot-0" ]] || exit 1
    loom_build_slot_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "slot path is \$dir/slot-N, matching build_slot.rs"

assert_eq "$(LOOM_BUILD_SLOT_DIR="/tmp/x" loom_build_slot_dir)" "/tmp/x" \
    "LOOM_BUILD_SLOT_DIR overrides the default location"
assert_eq "$(LOOM_BUILD_SLOT_DIR="   " HOME=/h loom_build_slot_dir)" "/h/.loom/locks/build-slot" \
    "a blank override falls back to the machine-wide ~/.loom/locks/build-slot"

echo ""
echo "=== build-gate.sh sources the helper ==="
if grep -q "lib/build-slot.sh" "$SCRIPTS_DIR/build-gate.sh"; then
    pass "build-gate.sh sources lib/build-slot.sh (the designated high-CPU stage)"
else
    fail "build-gate.sh no longer takes a build slot — the #4512 serialization point is gone"
fi

echo ""
echo "==============================================="
echo "  Tests run:    $TESTS_RUN"
echo -e "  ${GREEN}Passed:${NC}       $TESTS_PASSED"
echo -e "  ${RED}Failed:${NC}       $TESTS_FAILED"
echo "==============================================="

[[ "$TESTS_FAILED" -eq 0 ]]
