#!/usr/bin/env bash
# test-fleet-send.sh — Tests for the safehouse Bash fleet-comms client
# (issue #4199, phase 2 of #4196 / #3997).
#
# fleet-send.sh is the Bash fallback that lifecycle role subagents (Builder /
# Judge / Doctor) use to post to the safehouse room, since their tool allowlists
# exclude the injected `safehouse_send` MCP tool. Its contract is HARD
# degradation: never block or fail a role.
#
# Covers:
#   a. No SAFEHOUSED_SOCKET / SAFEHOUSE_PERSONA env ⇒ exit 0, zero output.
#   b. Env set but the socket path is absent (connect fails) ⇒ exit 0.
#   c. Happy path against a mock AF_UNIX server: the server receives `hello`
#      (with the right persona) then `send` (with the right type/task_id/to/body).
#   d. Invalid `--type` ⇒ rejected locally with NO socket write, exit 0.
#
# Style matches test-mcp-config.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Usage:
#   ./.loom/scripts/tests/test-fleet-send.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FLEET_SEND="$SCRIPTS_DIR/fleet-send.sh"
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
    echo -e "  ${YELLOW}SKIP${NC}: python3 not available; fleet-send.sh tests need it"
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

# Mock AF_UNIX safehoused: accept one connection, reply ok to hello + send, and
# record every received line to $2. Times out (writing an empty record) if no
# client ever connects — used to prove the "no socket write" path in test (d).
MOCK_PY="$TMPDIR_TEST/mock-safehoused.py"
cat >"$MOCK_PY" <<'PY'
import json, os, socket, sys

sock_path = sys.argv[1]
record = sys.argv[2]
accept_timeout = float(os.environ.get("MOCK_ACCEPT_TIMEOUT", "3"))

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
        elif op == "send":
            conn.sendall(
                (json.dumps({"id": msg.get("id", 1), "ok": True}) + "\n").encode("utf-8")
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
echo "fleet-send.sh degradation + wire tests"
echo "============================================================"

# ---------------------------------------------------------------------------
# (a) No env at all ⇒ exit 0, zero output.
# ---------------------------------------------------------------------------
echo ""
echo "-- (a) no SAFEHOUSED_SOCKET / SAFEHOUSE_PERSONA --"
out_a="$(env -u SAFEHOUSED_SOCKET -u LOOM_SAFEHOUSE_SOCKET -u SAFEHOUSE_PERSONA \
    bash "$FLEET_SEND" --task-id loom_4199 --type task --body "hi" 2>&1)"
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
    bash "$FLEET_SEND" --task-id loom_4199 --type task --body "hi" 2>&1)"
rc_b=$?
assert_eq "0" "$rc_b" "(b) exits 0 when socket path is absent"
assert_eq "" "$out_b" "(b) produces zero output when socket is absent"

# ---------------------------------------------------------------------------
# (c) Happy path against a mock AF_UNIX server.
# ---------------------------------------------------------------------------
echo ""
echo "-- (c) mock server receives hello then send --"
sock_c="$TMPDIR_TEST/c.sock"
rec_c="$TMPDIR_TEST/c.record"
start_mock "$sock_c" "$rec_c"
if [[ -S "$sock_c" ]]; then
    SAFEHOUSED_SOCKET="$sock_c" SAFEHOUSE_PERSONA="loom_builder_5" \
        bash "$FLEET_SEND" --task-id loom_4199 --type handoff \
        --body "starting issue 4199" --to "*"
    rc_c=$?
    wait "$MOCK_PID" 2>/dev/null || true
    record_c="$(cat "$rec_c" 2>/dev/null || true)"
    assert_eq "0" "$rc_c" "(c) exits 0 on success"
    assert_contains '"op"' "$record_c" "(c) server received at least one request"
    assert_contains 'hello' "$record_c" "(c) server received a hello request"
    assert_contains 'loom_builder_5' "$record_c" "(c) hello carries the resolved persona"
    assert_contains 'send' "$record_c" "(c) server received a send request"
    assert_contains 'handoff' "$record_c" "(c) send carries the requested type"
    assert_contains 'loom_4199' "$record_c" "(c) send carries the task_id"
    assert_contains 'starting issue 4199' "$record_c" "(c) send carries the body"
    assert_contains '"to": "*"' "$record_c" "(c) send carries to=* (broadcast)"
    # Ensure hello precedes send on the wire.
    hello_idx="$(printf '%s\n' "$record_c" | grep -n hello | head -1 | cut -d: -f1)"
    send_idx="$(printf '%s\n' "$record_c" | grep -n '"op": "send"' | head -1 | cut -d: -f1)"
    if [[ -n "$hello_idx" && -n "$send_idx" && "$hello_idx" -lt "$send_idx" ]]; then
        pass "(c) hello precedes send"
    else
        fail "(c) hello must precede send" "hello@$hello_idx send@$send_idx"
    fi
else
    fail "(c) mock server failed to bind its socket"
fi

# ---------------------------------------------------------------------------
# (d) Invalid --type ⇒ rejected locally, NO socket write, exit 0.
# ---------------------------------------------------------------------------
echo ""
echo "-- (d) invalid type rejected without a socket write --"
sock_d="$TMPDIR_TEST/d.sock"
rec_d="$TMPDIR_TEST/d.record"
MOCK_ACCEPT_TIMEOUT=1 start_mock "$sock_d" "$rec_d"
if [[ -S "$sock_d" ]]; then
    out_d="$(SAFEHOUSED_SOCKET="$sock_d" SAFEHOUSE_PERSONA="loom_builder_5" \
        bash "$FLEET_SEND" --task-id loom_4199 --type bogus --body "nope" 2>&1)"
    rc_d=$?
    wait "$MOCK_PID" 2>/dev/null || true
    record_d="$(cat "$rec_d" 2>/dev/null || true)"
    assert_eq "0" "$rc_d" "(d) exits 0 on invalid type"
    assert_eq "" "$out_d" "(d) produces zero output on invalid type"
    assert_not_contains "hello" "$record_d" "(d) no hello reached the socket (no write)"
    assert_eq "" "$record_d" "(d) mock recorded nothing (no connection)"
else
    fail "(d) mock server failed to bind its socket"
fi

# ---------------------------------------------------------------------------
# (d2) Invalid task_id (charset) ⇒ rejected locally, exit 0.
# ---------------------------------------------------------------------------
echo ""
echo "-- (d2) invalid task_id charset rejected without a socket write --"
sock_e="$TMPDIR_TEST/e.sock"
rec_e="$TMPDIR_TEST/e.record"
MOCK_ACCEPT_TIMEOUT=1 start_mock "$sock_e" "$rec_e"
if [[ -S "$sock_e" ]]; then
    out_e="$(SAFEHOUSED_SOCKET="$sock_e" SAFEHOUSE_PERSONA="loom_builder_5" \
        bash "$FLEET_SEND" --task-id "loom/4199 bad" --type task --body "nope" 2>&1)"
    rc_e=$?
    wait "$MOCK_PID" 2>/dev/null || true
    record_e="$(cat "$rec_e" 2>/dev/null || true)"
    assert_eq "0" "$rc_e" "(d2) exits 0 on invalid task_id"
    assert_eq "" "$out_e" "(d2) produces zero output on invalid task_id"
    assert_eq "" "$record_e" "(d2) mock recorded nothing (no connection)"
else
    fail "(d2) mock server failed to bind its socket"
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
