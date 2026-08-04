#!/usr/bin/env bash
# test-bounded-run.sh — tests for defaults/scripts/lib/bounded-run.sh (#4799).
#
# bounded_run() is the shared bounded-probe helper extracted from
# loom-daemon-watchdog.sh's IPC probe so a SECOND blocking-`$(...)`-hang site
# (loom-daemon-start.sh's print_calibrate_hint()) could reuse it. Because it is
# now shared, its contract needs its own coverage rather than being verified
# only indirectly through its callers:
#
#   1. A fast command's stdout and exit code are forwarded verbatim (0 and
#      non-zero), on BOTH implementations.
#   2. A command that never returns is bounded and reported as 124 — GNU
#      `timeout`'s code — on BOTH implementations, so callers branch on one
#      code regardless of which path ran.
#   3. LOOM_FORCE_PORTABLE_TIMEOUT=1 really does take the portable fallback
#      even where `timeout(1)` exists (this is what makes the macOS-shaped path
#      testable on a Linux CI runner — otherwise it is untested dead code).
#   4. The portable fallback leaves no orphan: the wedged child is dead once
#      bounded_run returns.
#   5. Arguments containing spaces survive (no word-splitting through the
#      `"$@"` forwarding).
#
# Usage:
#   bash defaults/scripts/tests/test-bounded-run.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SCRIPT_DIR/../lib/bounded-run.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_PASSED=$((TESTS_PASSED+1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN+1)); TESTS_FAILED=$((TESTS_FAILED+1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_eq() { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1' want '$2')"; fi; }

if [[ ! -r "$LIB" ]]; then
    echo -e "${RED}FATAL${NC}: cannot read $LIB"
    exit 1
fi
# shellcheck source=../lib/bounded-run.sh
source "$LIB"

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# A "wedged" command: writes its own pid, then blocks forever — the exact shape
# print_calibrate_hint() hit against a daemon binary with no calibrate handler.
WEDGE="$WORKDIR/wedge.sh"
cat > "$WEDGE" <<'EOF'
#!/usr/bin/env bash
echo "$$" > "$1"
while true; do sleep 1; done
EOF
chmod +x "$WEDGE"

echo "════════════════════════════════════════════"
echo "  bounded-run.sh tests (#4799)"
echo "════════════════════════════════════════════"

# Both implementations are exercised by running every case twice: once with the
# env unset (the host's native path — `timeout(1)` on Linux, the portable
# fallback on stock macOS) and once with LOOM_FORCE_PORTABLE_TIMEOUT=1 pinned.
for MODE in native portable; do
    if [[ "$MODE" == "portable" ]]; then
        export LOOM_FORCE_PORTABLE_TIMEOUT=1
    else
        unset LOOM_FORCE_PORTABLE_TIMEOUT
    fi
    echo ""
    echo "Mode: $MODE"

    out="$(bounded_run 5 echo hello)"; rc=$?
    assert_eq "hello" "$out" "[$MODE] stdout of a fast command is forwarded"
    assert_eq "0" "$rc" "[$MODE] exit 0 of a fast command is forwarded"

    bounded_run 5 bash -c 'exit 7' >/dev/null 2>&1; rc=$?
    assert_eq "7" "$rc" "[$MODE] non-zero exit code is forwarded verbatim (not normalized)"

    out="$(bounded_run 5 printf '%s\n' 'two words')"
    assert_eq "two words" "$out" "[$MODE] arguments containing spaces are not word-split"

    # The load-bearing case: a command that never returns must be bounded, and
    # reported as timeout(1)'s 124 on either implementation.
    PIDFILE="$WORKDIR/wedge-$MODE.pid"
    rm -f "$PIDFILE"
    START=$SECONDS
    bounded_run 1 "$WEDGE" "$PIDFILE" >/dev/null 2>&1; rc=$?
    ELAPSED=$((SECONDS - START))
    assert_eq "124" "$rc" "[$MODE] a never-returning command is bounded and reports 124"
    if (( ELAPSED <= 10 )); then
        pass "[$MODE] the bound is honored (returned in ${ELAPSED}s, budget 1s)"
    else
        fail "[$MODE] the bound is honored (took ${ELAPSED}s, budget 1s)"
    fi

    # No orphan left behind: the wedged child must be dead once we return.
    wedge_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -z "$wedge_pid" ]]; then
        fail "[$MODE] the wedged child recorded its pid (fixture sanity)"
    else
        pass "[$MODE] the wedged child recorded its pid (fixture sanity)"
        # Allow a beat for the KILL escalation to be reaped.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$wedge_pid" 2>/dev/null || break
            sleep 0.3
        done
        if kill -0 "$wedge_pid" 2>/dev/null; then
            kill -KILL "$wedge_pid" 2>/dev/null || true
            fail "[$MODE] the wedged child is reaped, not orphaned"
        else
            pass "[$MODE] the wedged child is reaped, not orphaned"
        fi
    fi
done
unset LOOM_FORCE_PORTABLE_TIMEOUT

# LOOM_FORCE_PORTABLE_TIMEOUT must actually bypass timeout(1) where it exists —
# otherwise the "portable" mode above silently re-tested the native path and the
# env var is dead code. Prove the bypass by putting a `timeout` stub on PATH
# that records its invocations, then asserting the forced run never called it.
echo ""
echo "Mode: seam verification"
STUB_BIN="$WORKDIR/stub-bin"
mkdir -p "$STUB_BIN"
STUB_LOG="$WORKDIR/timeout-calls.log"
: > "$STUB_LOG"
for t in timeout gtimeout; do
    cat > "$STUB_BIN/$t" <<EOF
#!/usr/bin/env bash
echo "$t \$*" >> "$STUB_LOG"
exit 0
EOF
    chmod +x "$STUB_BIN/$t"
done

( PATH="$STUB_BIN:$PATH"; LOOM_FORCE_PORTABLE_TIMEOUT=1 bounded_run 5 echo forced >/dev/null 2>&1 )
if [[ -s "$STUB_LOG" ]]; then
    fail "LOOM_FORCE_PORTABLE_TIMEOUT=1 bypasses timeout(1) (stub was called: $(cat "$STUB_LOG"))"
else
    pass "LOOM_FORCE_PORTABLE_TIMEOUT=1 bypasses timeout(1) entirely"
fi

# Control: with the seam OFF and a `timeout` on PATH, the timeout(1) path IS
# taken — so the assertion above is measuring the seam, not an absent stub.
: > "$STUB_LOG"
( PATH="$STUB_BIN:$PATH"; unset LOOM_FORCE_PORTABLE_TIMEOUT; bounded_run 5 echo native >/dev/null 2>&1 )
if [[ -s "$STUB_LOG" ]]; then
    pass "control: with the seam OFF, timeout(1) on PATH IS used"
else
    fail "control: with the seam OFF, timeout(1) on PATH IS used (stub never called)"
fi

echo ""
echo "════════════════════════════════════════════"
echo "  Total: $TESTS_RUN  Passed: $TESTS_PASSED  Failed: $TESTS_FAILED"
echo "════════════════════════════════════════════"
[[ "$TESTS_FAILED" -eq 0 ]]
