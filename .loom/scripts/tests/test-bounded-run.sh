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
# ## Which exit codes the wedge case may legitimately see (#7788)
#
# The wedge case (contract 2/4 above) is the only one whose verdict depends on a
# child process having actually STARTED, so it is the only one with a fixture
# precondition worth stating:
#
#   124      the only passing verdict. The budget elapsed and the child was
#            signalled; the portable path normalizes 143 (TERM) / 137 (KILL) to
#            `timeout(1)`'s own 124 so callers branch on one code.
#   126/127  NOT a verdict about bounding — the wedged child never ran, so the
#            budget was never in play. Both codes are what the backgrounded
#            `"$@" &` child exits with when bash forks it but the exec fails:
#            127 for "not found"/exec failure, 126 for "found but not
#            executable". Bash-as-the-child prints the errno message to stderr
#            before exiting, which is why this fixture now CAPTURES that stderr
#            instead of discarding it. A third, narrower route exists on the
#            portable path: `wait "$cmd_pid"` returns 127 by definition for a
#            pid bash does not consider its own child — in which case the real
#            child may still be alive and about to write its pid, which is what
#            the bounded pid poll below is for.
#
# This is not hypothetical. CI run 35034960799 recorded exactly it (`[portable]`
# rc 127, pid file never written, bound reported as honored) on a branch whose
# diff touched neither bounded-run.sh nor anything it calls, while the
# `[native]` case moments earlier exec'd the same wedge file fine — and this
# suite runs concurrently with every other wired shell suite (#6622). Which
# errno produced it is still unestablished, precisely because the old fixture
# sent that message to /dev/null; note that plain fork exhaustion is NOT the
# explanation, since a background fork bash cannot complete kills the whole
# non-interactive shell (exit 254) rather than yielding 127 from `wait`.
#
# An attempt that never started the child never exercised the bound, so
# asserting 124 on it reports a verdict the run did not measure. The fixture
# therefore RETRIES that attempt — bounded, loudly, with the child's stderr
# echoed — and asserts only on an attempt whose precondition held. The same
# treatment covers the narrower race the pid handshake has always had: a 124
# whose child was signalled before it could record its pid (bounding worked,
# but the orphan check has no pid to check). Everything that would prove a real
# bounding regression still fails: rc 0, an unnormalized 143/137, a return past
# the budget, or a precondition that never holds across every attempt.
#
# Usage:
#   bash defaults/scripts/tests/test-bounded-run.sh
#   LOOM_TEST_WEDGE_MAX_ATTEMPTS=1 bash …   # one-shot (no retry) wedge case

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
cleanup() {
    # Sweep any wedged child that outlived the attempt that spawned it. The
    # in-band reap check below can only kill a child whose pid was recorded, so
    # an attempt that got no pid handshake (#7788) could otherwise leave a
    # forever-blocking process behind — including one this run retried past. The
    # pattern is this run's own mktemp path, so it cannot match anything else.
    if command -v pkill >/dev/null 2>&1; then
        pkill -KILL -f "$WORKDIR/wedge.sh" 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
}
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

# How many times the wedge case may re-run an attempt that produced no pid
# handshake (see the header). Three, not "until it passes": the cap is what keeps
# a genuine "bounded_run can no longer launch anything" regression a hard
# failure — it only stops ONE unscoreable attempt from being reported as a
# verdict about bounding.
WEDGE_MAX_ATTEMPTS="${LOOM_TEST_WEDGE_MAX_ATTEMPTS:-3}"
(( WEDGE_MAX_ATTEMPTS >= 1 )) || WEDGE_MAX_ATTEMPTS=1   # 0/garbage still runs once

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
    #
    # An attempt only measures that if the wedged child actually started — its
    # pid file is the handshake that says so. An attempt that produced no pid
    # handshake is retried rather than asserted on; see the header's "Which exit
    # codes the wedge case may legitimately see" (#7788).
    PIDFILE="$WORKDIR/wedge-$MODE.pid"
    WEDGE_ERR="$WORKDIR/wedge-$MODE.err"
    attempt=0; rc=""; ELAPSED=0; wedge_pid=""; wedge_err=""
    while (( attempt < WEDGE_MAX_ATTEMPTS )); do
        attempt=$((attempt + 1))
        rm -f "$PIDFILE"
        : > "$WEDGE_ERR"
        START=$SECONDS
        # stderr is captured rather than discarded so a precondition miss can
        # SAY why ("fork: Resource temporarily unavailable", "No such file or
        # directory", …) instead of being a bare exit code in a CI log.
        bounded_run 1 "$WEDGE" "$PIDFILE" >/dev/null 2>"$WEDGE_ERR"; rc=$?
        ELAPSED=$((SECONDS - START))

        # Bounded poll, not a bare `cat`. On the normal path bounded_run has
        # already `wait`ed the child, so the pid file is there and this exits
        # on the first iteration at the cost of one `[[ -s ]]`. The poll is for
        # the paths where bounded_run returns WITHOUT having reaped the real
        # child — e.g. a `wait` that returned 127 for a pid the shell does not
        # own — where a child that is a moment from writing its pid would
        # otherwise be misread as one that never started.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            [[ -s "$PIDFILE" ]] && break
            sleep 0.1
        done
        wedge_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
        [[ -n "$wedge_pid" ]] && break

        # No pid recorded. Retry ONLY for the codes that are consistent with
        # "this attempt never exercised the bound" — 126/127 (the child never
        # ran at all) and 124 (bounded, but the child was signalled before it
        # could record itself, so the orphan check below has nothing to check).
        # Any OTHER code with no pid file is a real signal about bounded_run
        # (0, an unnormalized 143/137, a forwarded code), so stop immediately
        # and let the assertions below report it.
        wedge_err="$(tr '\n' ' ' < "$WEDGE_ERR" 2>/dev/null || true)"
        case "$rc" in
            124|126|127) ;;
            *) break ;;
        esac
        if (( attempt < WEDGE_MAX_ATTEMPTS )); then
            echo "  NOTE: [$MODE] attempt $attempt got no pid handshake from the wedged child" \
                 "(rc=$rc after ${ELAPSED}s, stderr: ${wedge_err:-<empty>})" \
                 "— nothing to score a bounding verdict against, retrying (#7788)"
            sleep 1
        fi
    done

    if [[ -z "$wedge_pid" ]]; then
        # The precondition never held. Fail once, loudly and honestly, rather
        # than reporting a 124/bound verdict no attempt actually measured.
        fail "[$MODE] the wedged child recorded its pid (fixture sanity) — no pid after ${attempt} attempt(s); last rc=${rc} after ${ELAPSED}s, stderr: ${wedge_err:-<empty>}"
    else
        assert_eq "124" "$rc" "[$MODE] a never-returning command is bounded and reports 124"
        if (( ELAPSED <= 10 )); then
            pass "[$MODE] the bound is honored (returned in ${ELAPSED}s, budget 1s)"
        else
            fail "[$MODE] the bound is honored (took ${ELAPSED}s, budget 1s)"
        fi

        # No orphan left behind: the wedged child must be dead once we return.
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
