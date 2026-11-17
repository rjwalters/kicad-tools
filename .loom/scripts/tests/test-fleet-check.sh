#!/usr/bin/env bash
# test-fleet-check.sh — Tests for the safehouse Bash fleet-comms read/check
# client (issue #4248, follow-on from #4199 / phase 2 of #4196 / #3997).
#
# fleet-check.sh is the read/check counterpart to fleet-send.sh — the Bash
# fallback that lifecycle role subagents (Builder / Judge / Doctor) use to
# read from the safehouse mailbox, since their tool allowlists exclude the
# injected `safehouse_read` MCP tool. Its contract is HARD degradation: never
# block or fail a role. UNLIKE fleet-send.sh, on success it prints each
# message to stdout (one JSON object per line).
#
# Covers:
#   a. No SAFEHOUSED_SOCKET / SAFEHOUSE_PERSONA env ⇒ exit 0, zero output.
#   b. Env set but the socket path is absent (connect fails) ⇒ exit 0.
#   c. Happy path against a mock AF_UNIX server: hello sent before check,
#      messages from the reply are printed to stdout (one per line).
#   d. Empty mailbox (`messages: []`) ⇒ zero output, exit 0.
#   e. --peek and --limit flags appear in the wire request.
#   f. Async push lines (object with `event`, no `id`) interleaved before the
#      genuine reply are skipped.
#   g. Malformed reply / early-closed connection ⇒ exit 0, no output.
#
# Style matches test-fleet-send.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Usage:
#   ./.loom/scripts/tests/test-fleet-check.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FLEET_CHECK="$SCRIPTS_DIR/fleet-check.sh"
PY="${LOOM_PYTHON:-python3}"

# Background-PID bookkeeping (#4773): the mock AF_UNIX server backgrounded by
# start_mock() (below) is tracked here so the EXIT/INT/TERM trap can reap it
# even if this suite is killed before an in-body `wait "$MOCK_PID"` runs.
# shellcheck source=lib/bg-proc-trap.sh
source "$SCRIPT_DIR/lib/bg-proc-trap.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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
    [[ -n "${2:-}" ]] && echo "    $2"
}

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3" "expected '$1', got '$2'"; fi
}
assert_contains() {
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3" "expected substring '$1' in '$2'"; fi
}
assert_not_contains() {
    if [[ "$2" != *"$1"* ]]; then pass "$3"; else fail "$3" "unexpected substring '$1' in '$2'"; fi
}

# Isolate every invocation from the ambient operator environment.
unset SAFEHOUSED_SOCKET LOOM_SAFEHOUSE_SOCKET SAFEHOUSE_PERSONA 2>/dev/null || true

if ! command -v "$PY" >/dev/null 2>&1; then
    echo -e "  ${YELLOW}SKIP${NC}: python3 not available; fleet-check.sh tests need it"
    exit 0
fi

TMPDIR_TEST="$(mktemp -d)"
# bg_proc_reap kills the mock server tracked via bg_proc_track in start_mock()
# below (a backstop for a kill BEFORE the in-body `wait "$MOCK_PID"` runs);
# EXIT/INT/TERM (not just EXIT, #4773) so a hard interruption of this suite
# still reaps it. NOTE: a bare `trap CMD EXIT INT TERM` runs CMD on INT/TERM
# but does NOT stop the script (only an EXIT-trap firing auto-exits) -- the
# explicit `exit` below is required, else a SIGTERM'd suite would clean up
# once and then keep running every remaining test case.
trap 'bg_proc_reap; rm -rf "$TMPDIR_TEST"' EXIT
trap 'bg_proc_reap; rm -rf "$TMPDIR_TEST"; exit 1' INT TERM

# Mock AF_UNIX safehoused: accept one connection, reply ok to hello, then
# reply to `check` per the scripted behavior for this record, and record
# every received line to $2. Times out (writing an empty record) if no
# client ever connects.
#
# Behavior is selected via env vars read by the mock itself:
#   MOCK_MESSAGES        - JSON array string to return as "messages" (default "[]")
#   MOCK_PUSH_LINES      - integer count of async push lines to emit before the
#                          check reply (default 0)
#   MOCK_MALFORMED_CHECK - if "1", write a malformed (non-JSON) line instead of
#                          a check reply, then close.
#   MOCK_CLOSE_EARLY     - if "1", close the connection right after hello,
#                          before any check request is even read.
MOCK_PY="$TMPDIR_TEST/mock-safehoused.py"
cat >"$MOCK_PY" <<'PY'
import json, os, socket, sys

sock_path = sys.argv[1]
record = sys.argv[2]
accept_timeout = float(os.environ.get("MOCK_ACCEPT_TIMEOUT", "3"))
messages = json.loads(os.environ.get("MOCK_MESSAGES", "[]"))
push_lines = int(os.environ.get("MOCK_PUSH_LINES", "0"))
malformed_check = os.environ.get("MOCK_MALFORMED_CHECK", "0") == "1"
close_early = os.environ.get("MOCK_CLOSE_EARLY", "0") == "1"

if os.path.exists(sock_path):
    os.unlink(sock_path)

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(1)
srv.settimeout(accept_timeout)

received = []
try:
    conn, _ = srv.accept()
except socket.timeout:
    with open(record, "w") as f:
        pass  # empty record: no client connected
    srv.close()
    sys.exit(0)

conn.settimeout(3.0)
reader = conn.makefile("r", encoding="utf-8")
try:
    for line in reader:
        line = line.strip()
        if not line:
            continue
        received.append(line)
        try:
            msg = json.loads(line)
        except Exception:
            continue
        op = msg.get("op")
        if op == "hello":
            conn.sendall((json.dumps({"id": 0, "ok": True}) + "\n").encode("utf-8"))
            if close_early:
                break
        elif op == "check":
            for i in range(push_lines):
                conn.sendall(
                    (json.dumps({"event": "ping", "seq": i}) + "\n").encode("utf-8")
                )
            if malformed_check:
                conn.sendall(b"{not json\n")
            else:
                conn.sendall(
                    (
                        json.dumps(
                            {
                                "id": msg.get("id", 1),
                                "ok": True,
                                "advanced": not msg.get("peek", False),
                                "messages": messages,
                            }
                        )
                        + "\n"
                    ).encode("utf-8")
                )
            break
finally:
    with open(record, "w") as f:
        f.write("\n".join(received))
    conn.close()
    srv.close()
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
PY

# start_mock <sockpath> <recordfile> — launch the mock, wait for it to bind.
MOCK_PID=""
start_mock() {
    local sock="$1" rec="$2"
    "$PY" "$MOCK_PY" "$sock" "$rec" &
    MOCK_PID=$!
    bg_proc_track "$MOCK_PID"
    local i=0
    while [[ ! -S "$sock" && $i -lt 50 ]]; do
        sleep 0.05
        i=$((i + 1))
    done
}

echo "============================================================"
echo "fleet-check.sh degradation + wire tests"
echo "============================================================"

# ---------------------------------------------------------------------------
# (a) No env at all ⇒ exit 0, zero output.
# ---------------------------------------------------------------------------
echo ""
echo "-- (a) no SAFEHOUSED_SOCKET / SAFEHOUSE_PERSONA --"
out_a="$(env -u SAFEHOUSED_SOCKET -u LOOM_SAFEHOUSE_SOCKET -u SAFEHOUSE_PERSONA \
    bash "$FLEET_CHECK" 2>&1)"
rc_a=$?
assert_eq "0" "$rc_a" "(a) exits 0 with no env"
assert_eq "" "$out_a" "(a) produces zero output with no env"

# ---------------------------------------------------------------------------
# (b) Env set but the socket path does not exist ⇒ exit 0, zero output.
# ---------------------------------------------------------------------------
echo ""
echo "-- (b) env set, socket path absent --"
missing_sock="$TMPDIR_TEST/does-not-exist.sock"
out_b="$(SAFEHOUSED_SOCKET="$missing_sock" SAFEHOUSE_PERSONA="loom_builder_1" \
    bash "$FLEET_CHECK" 2>&1)"
rc_b=$?
assert_eq "0" "$rc_b" "(b) exits 0 when socket path is absent"
assert_eq "" "$out_b" "(b) produces zero output when socket is absent"

# ---------------------------------------------------------------------------
# (c) Happy path against a mock AF_UNIX server: hello before check, messages
#     printed to stdout.
# ---------------------------------------------------------------------------
echo ""
echo "-- (c) mock server: hello before check, messages printed --"
sock_c="$TMPDIR_TEST/c.sock"
rec_c="$TMPDIR_TEST/c.record"
msgs_c='[{"room_id": "r1", "event_id": "e1", "sender": "alice", "envelope": {"v": 1, "from": "alice", "to": "*", "type": "chat", "body": "hello there"}}, {"room_id": "r1", "event_id": "e2", "sender": "bob", "envelope": {"v": 1, "from": "bob", "to": "*", "type": "chat", "body": "second message"}}]'
MOCK_MESSAGES="$msgs_c" start_mock "$sock_c" "$rec_c"
if [[ -S "$sock_c" ]]; then
    out_c="$(SAFEHOUSED_SOCKET="$sock_c" SAFEHOUSE_PERSONA="loom_builder_5" \
        MOCK_MESSAGES="$msgs_c" bash "$FLEET_CHECK")"
    rc_c=$?
    wait "$MOCK_PID" 2>/dev/null || true
    record_c="$(cat "$rec_c" 2>/dev/null || true)"
    assert_eq "0" "$rc_c" "(c) exits 0 on success"
    assert_contains "hello" "$record_c" "(c) server received a hello request"
    assert_contains 'loom_builder_5' "$record_c" "(c) hello carries the resolved persona"
    assert_contains '"op": "check"' "$record_c" "(c) server received a check request"
    # Ensure hello precedes check on the wire.
    hello_idx="$(printf '%s\n' "$record_c" | grep -n hello | head -1 | cut -d: -f1)"
    check_idx="$(printf '%s\n' "$record_c" | grep -n '"op": "check"' | head -1 | cut -d: -f1)"
    if [[ -n "$hello_idx" && -n "$check_idx" && "$hello_idx" -lt "$check_idx" ]]; then
        pass "(c) hello precedes check"
    else
        fail "(c) hello must precede check" "hello@$hello_idx check@$check_idx"
    fi
    line_count_c="$(printf '%s' "$out_c" | grep -c '.' || true)"
    assert_eq "2" "$line_count_c" "(c) prints one line per message (2 messages)"
    assert_contains "hello there" "$out_c" "(c) stdout contains first message body"
    assert_contains "second message" "$out_c" "(c) stdout contains second message body"
else
    fail "(c) mock server failed to bind its socket"
fi

# ---------------------------------------------------------------------------
# (d) Empty mailbox ⇒ zero output, exit 0.
# ---------------------------------------------------------------------------
echo ""
echo "-- (d) empty mailbox produces zero output --"
sock_d="$TMPDIR_TEST/d.sock"
rec_d="$TMPDIR_TEST/d.record"
MOCK_MESSAGES='[]' start_mock "$sock_d" "$rec_d"
if [[ -S "$sock_d" ]]; then
    out_d="$(SAFEHOUSED_SOCKET="$sock_d" SAFEHOUSE_PERSONA="loom_builder_5" \
        MOCK_MESSAGES='[]' bash "$FLEET_CHECK" 2>&1)"
    rc_d=$?
    wait "$MOCK_PID" 2>/dev/null || true
    assert_eq "0" "$rc_d" "(d) exits 0 on empty mailbox"
    assert_eq "" "$out_d" "(d) produces zero output on empty mailbox"
else
    fail "(d) mock server failed to bind its socket"
fi

# ---------------------------------------------------------------------------
# (e) --peek and --limit flags appear in the wire request.
# ---------------------------------------------------------------------------
echo ""
echo "-- (e) --peek and --limit appear in the check request --"
sock_e="$TMPDIR_TEST/e.sock"
rec_e="$TMPDIR_TEST/e.record"
MOCK_MESSAGES='[]' start_mock "$sock_e" "$rec_e"
if [[ -S "$sock_e" ]]; then
    SAFEHOUSED_SOCKET="$sock_e" SAFEHOUSE_PERSONA="loom_builder_5" \
        MOCK_MESSAGES='[]' bash "$FLEET_CHECK" --peek --limit 2 >/dev/null 2>&1
    rc_e=$?
    wait "$MOCK_PID" 2>/dev/null || true
    record_e="$(cat "$rec_e" 2>/dev/null || true)"
    assert_eq "0" "$rc_e" "(e) exits 0"
    assert_contains '"peek": true' "$record_e" "(e) request carries peek: true"
    assert_contains '"limit": 2' "$record_e" "(e) request carries limit: 2"
else
    fail "(e) mock server failed to bind its socket"
fi

# ---------------------------------------------------------------------------
# (f) Async push lines (event, no id) interleaved before the reply are
#     skipped; the genuine reply's messages still surface.
# ---------------------------------------------------------------------------
echo ""
echo "-- (f) interleaved async push lines are skipped --"
sock_f="$TMPDIR_TEST/f.sock"
rec_f="$TMPDIR_TEST/f.record"
msgs_f='[{"room_id": "r1", "event_id": "e9", "sender": "carol", "envelope": {"v": 1, "from": "carol", "to": "*", "type": "chat", "body": "after the pushes"}}]'
MOCK_MESSAGES="$msgs_f" MOCK_PUSH_LINES=3 start_mock "$sock_f" "$rec_f"
if [[ -S "$sock_f" ]]; then
    out_f="$(SAFEHOUSED_SOCKET="$sock_f" SAFEHOUSE_PERSONA="loom_builder_5" \
        MOCK_MESSAGES="$msgs_f" MOCK_PUSH_LINES=3 bash "$FLEET_CHECK")"
    rc_f=$?
    wait "$MOCK_PID" 2>/dev/null || true
    assert_eq "0" "$rc_f" "(f) exits 0 despite interleaved push lines"
    assert_contains "after the pushes" "$out_f" "(f) genuine reply message still surfaces"
    assert_not_contains '"event": "ping"' "$out_f" "(f) push lines are not echoed to stdout"
else
    fail "(f) mock server failed to bind its socket"
fi

# ---------------------------------------------------------------------------
# (g) Malformed reply ⇒ exit 0, no output.
# ---------------------------------------------------------------------------
echo ""
echo "-- (g) malformed check reply produces no output --"
sock_g="$TMPDIR_TEST/g.sock"
rec_g="$TMPDIR_TEST/g.record"
MOCK_MALFORMED_CHECK=1 start_mock "$sock_g" "$rec_g"
if [[ -S "$sock_g" ]]; then
    out_g="$(SAFEHOUSED_SOCKET="$sock_g" SAFEHOUSE_PERSONA="loom_builder_5" \
        MOCK_MALFORMED_CHECK=1 bash "$FLEET_CHECK" 2>&1)"
    rc_g=$?
    wait "$MOCK_PID" 2>/dev/null || true
    assert_eq "0" "$rc_g" "(g) exits 0 on malformed reply"
    assert_eq "" "$out_g" "(g) produces zero output on malformed reply"
else
    fail "(g) mock server failed to bind its socket"
fi

# ---------------------------------------------------------------------------
# (h) Connection closes right after hello, before any check reply ⇒ exit 0,
#     no output (early-close / EOF path).
# ---------------------------------------------------------------------------
echo ""
echo "-- (h) server closes early (right after hello) ⇒ no output --"
sock_h="$TMPDIR_TEST/h.sock"
rec_h="$TMPDIR_TEST/h.record"
MOCK_CLOSE_EARLY=1 start_mock "$sock_h" "$rec_h"
if [[ -S "$sock_h" ]]; then
    out_h="$(SAFEHOUSED_SOCKET="$sock_h" SAFEHOUSE_PERSONA="loom_builder_5" \
        MOCK_CLOSE_EARLY=1 bash "$FLEET_CHECK" 2>&1)"
    rc_h=$?
    wait "$MOCK_PID" 2>/dev/null || true
    assert_eq "0" "$rc_h" "(h) exits 0 when server closes early"
    assert_eq "" "$out_h" "(h) produces zero output when server closes early"
else
    fail "(h) mock server failed to bind its socket"
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "========================================"
echo "Test Results:"
echo "  Total:  $TESTS_RUN"
echo -e "  ${GREEN}Passed: $TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "  ${RED}Failed: $TESTS_FAILED${NC}"
    exit 1
fi
echo -e "  ${GREEN}All tests passed!${NC}"
