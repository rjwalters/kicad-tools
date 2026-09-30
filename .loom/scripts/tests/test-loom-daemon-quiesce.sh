#!/usr/bin/env bash
# test-loom-daemon-quiesce.sh — Tests for loom-daemon-quiesce.sh (issue #6129).
#
# Style matches test-loom-daemon-stop.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# SAFETY (load-bearing, read before touching this file): loom-daemon-quiesce.sh's
# cross-platform fallback step enumerates the WHOLE host's process table (`ps
# -eo pid,ppid,args`) and can SIGTERM/SIGKILL anything matching `claude* -p
# /loom:*` — by design (issue #6129 wants a host-wide drain). On a live
# development/fleet host that table can legitimately include OTHER concurrent
# builders' real sweep/role-agent processes. Every non-dry-run invocation
# below therefore stubs `ps` on PATH (STUB_DIR, prepended) so the script's
# enumeration step sees ONLY the synthetic lines this suite injects — real
# host processes are never candidates, regardless of what is actually running
# alongside this suite. Do not remove the `ps` stub from any real (non
# --dry-run) invocation.
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-quiesce.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUIESCE_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-quiesce.sh"

# shellcheck source=lib/bg-proc-trap.sh
source "$SCRIPT_DIR/lib/bg-proc-trap.sh"
# shellcheck source=lib/live-state-sandbox.sh
source "$SCRIPT_DIR/lib/live-state-sandbox.sh"
live_state_sandbox_snapshot

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
        echo "  expected: [$expected]"
        echo "  actual:   [$actual]"
    fi
}

# A guaranteed-nonexistent unit/label so this suite can never disable/bootout
# the operator's real production daemon, mirroring test-loom-daemon-stop.sh.
export LOOM_SYSTEMD_UNIT="loom-daemon-test-$$.service"
export LOOM_LAUNCHD_LABEL="com.example.loom-quiesce-test-$$"

WORKDIR="$(mktemp -d)"
# The return code is CHECKED, never bare (#6420). init returns non-zero when it
# could not `cd` into the sandbox root (#6386 — the cwd tier is then still aimed
# at wherever this suite was launched from, i.e. potentially a LIVE checkout) or
# when the ambient supervisor label is the real production one (#5501). This
# suite runs under `set -uo pipefail` with NO `-e`, so a bare call would swallow
# both and continue with a HALF-ARMED sandbox — the exact state the helper's own
# failure path exists to prevent — while driving the real lifecycle scripts.
if ! live_state_sandbox_init "$WORKDIR/live-state"; then
    echo "FATAL: live-state sandbox init failed — refusing to run this suite against a half-armed sandbox (#6420)." >&2
    echo "  See the reason above (lib/live-state-sandbox.sh): a writable sandbox root is required, and the ambient LOOM_LAUNCHD_LABEL / LOOM_WATCHDOG_LABEL must not be the real production identities." >&2
    rm -rf "$WORKDIR"
    exit 1
fi
mkdir -p "$WORKDIR/.loom"

trap 'bg_proc_reap; rm -rf "$WORKDIR"' EXIT
trap 'bg_proc_reap; rm -rf "$WORKDIR"; exit 1' INT TERM

# ---------- tests ----------

# 1. --help documents the daemon-stop + agent-drain two-step and --dry-run.
help_out=$(bash "$QUIESCE_SCRIPT" --help 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qi 'quiesce' <<<"$help_out" && grep -q -- '--dry-run' <<<"$help_out" \
    && grep -q -- '--force' <<<"$help_out"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents the quiesce action, --dry-run, and --force"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents the quiesce action, --dry-run, and --force"
fi

# ---------- shared fixture: stub `ps` + `systemctl`, scoped to THIS suite ----------
# BOTH stubs exist from the very first invocation below, not just in the
# systemd-specific test. On a Linux host the quiesce script's step 2a runs
# whenever `is_linux_systemd` succeeds, which is a function of the OS plus
# `command -v systemctl` -- so without a stub on PATH from the start, running
# this suite on a real Linux fleet host would enumerate and stop that host's
# REAL `loom-agent-*.scope` units, i.e. its live agents. The stub answers
# list-units from a fixture file (empty unless a test populates it), so the
# real user manager is never consulted on any platform.
STUB_DIR="$WORKDIR/stub-bin"
mkdir -p "$STUB_DIR"
PS_FIXTURE="$WORKDIR/ps-lines.txt"
: > "$PS_FIXTURE"
cat > "$STUB_DIR/ps" <<EOF
#!/usr/bin/env bash
cat "$PS_FIXTURE"
EOF
chmod +x "$STUB_DIR/ps"

SD_STATE_FILE="$WORKDIR/sd-active"
: > "$SD_STATE_FILE"
SD_STOP_LOG="$WORKDIR/sd-stop.log"
: > "$SD_STOP_LOG"
SD_UNITS_FIXTURE="$WORKDIR/sd-units.txt"
: > "$SD_UNITS_FIXTURE"
cat > "$STUB_DIR/systemctl" <<EOF
#!/usr/bin/env bash
args=("\$@")
[[ "\${args[0]:-}" == "--user" ]] && args=("\${args[@]:1}")
case "\${args[0]:-}" in
  is-active)
    [[ -f "$SD_STATE_FILE" ]] && exit 0 || exit 1
    ;;
  is-enabled)
    exit 0
    ;;
  disable)
    rm -f "$SD_STATE_FILE"
    exit 0
    ;;
  list-units)
    cat "$SD_UNITS_FIXTURE"
    ;;
  stop)
    echo "\${args[1]:-}" >> "$SD_STOP_LOG"
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB_DIR/systemctl"

# 2. Cross-platform process-pattern fallback (the ONLY mechanism on launchd,
#    and the belt-and-braces catch on a systemd host with no reachable
#    systemd --user manager, e.g. no LOOM_SYSTEMD_FORCE below). Daemon-stop
#    uses the plain pid-file tier (LOOM_DAEMON_LAUNCHD=0 / LOOM_DAEMON_SYSTEMD=0
#    so no real launchd/systemd interaction happens); the agent-drain step
#    matches on the stubbed `ps` output only.
DAEMON_PID_FILE="$WORKDIR/.loom/.daemon.pid"
( sleep 30 & echo $! > "$DAEMON_PID_FILE" )
daemon_pid=$(cat "$DAEMON_PID_FILE")
bg_proc_track "$daemon_pid"

sleep 30 &
agent_pid=$!
bg_proc_track "$agent_pid"

sleep 30 &
unrelated_pid=$!
bg_proc_track "$unrelated_pid"

{
    echo "$agent_pid 1 claude -p /loom:champion"
    echo "$unrelated_pid 1 vim notes.txt"
} > "$PS_FIXTURE"

# `LOOM_PID_FILE=''` (empty) is deliberate since #6386: loom-daemon-stop.sh --
# which quiesce delegates step 1 to -- now resolves LOOM_PID_FILE ahead of the
# $PWD-derived state home, and live_state_sandbox_init exports it suite-wide.
# A case whose fixture pid file lives at "$WORKDIR/.loom/.daemon.pid" must
# therefore say it means the $PWD tier. Safe: the paired `cd "$WORKDIR"` keeps
# that tier inside this suite's own scratch workspace.
out2=$( cd "$WORKDIR" && PATH="$STUB_DIR:$PATH" LOOM_PID_FILE='' \
    LOOM_DAEMON_LAUNCHD=0 LOOM_DAEMON_SYSTEMD=0 LOOM_DAEMON_QUIESCE_GRACE_SECS=2 \
    bash "$QUIESCE_SCRIPT" 2>&1 )
rc2=$?
assert_eq "0" "$rc2" "cross-platform fallback: quiesce exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if ! kill -0 "$daemon_pid" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} cross-platform fallback: the daemon decoy is stopped (step 1)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} cross-platform fallback: the daemon decoy is stopped (step 1)"
    echo "$out2" | sed 's/^/    /'
    kill -9 "$daemon_pid" 2>/dev/null || true
fi
TESTS_RUN=$((TESTS_RUN + 1))
if ! kill -0 "$agent_pid" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} cross-platform fallback: the matching agent decoy (claude -p /loom:champion) is stopped (step 2)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} cross-platform fallback: the matching agent decoy is stopped (step 2)"
    kill -9 "$agent_pid" 2>/dev/null || true
fi
TESTS_RUN=$((TESTS_RUN + 1))
if kill -0 "$unrelated_pid" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} cross-platform fallback: a non-matching process (vim notes.txt) survives — selective, not host-wide"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} cross-platform fallback: a non-matching process survives (it was incorrectly killed)"
fi
kill -9 "$unrelated_pid" 2>/dev/null || true

# 3. --dry-run: resolves every target but kills / systemctl-mutates nothing.
( sleep 30 & echo $! > "$DAEMON_PID_FILE" )
daemon_pid3=$(cat "$DAEMON_PID_FILE")
bg_proc_track "$daemon_pid3"
sleep 30 &
agent_pid3=$!
bg_proc_track "$agent_pid3"
{
    echo "$agent_pid3 1 claude -p /loom:doctor"
} > "$PS_FIXTURE"

out3=$( cd "$WORKDIR" && PATH="$STUB_DIR:$PATH" LOOM_PID_FILE='' \
    LOOM_DAEMON_LAUNCHD=0 LOOM_DAEMON_SYSTEMD=0 \
    bash "$QUIESCE_SCRIPT" --dry-run 2>&1 )
rc3=$?
assert_eq "0" "$rc3" "--dry-run: exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qi 'DRY-RUN' <<<"$out3"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run: output is labeled DRY-RUN"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run: output is labeled DRY-RUN"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if kill -0 "$daemon_pid3" 2>/dev/null && kill -0 "$agent_pid3" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run: neither the daemon nor the agent decoy is actually touched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run: neither the daemon nor the agent decoy is actually touched"
fi
kill -9 "$daemon_pid3" "$agent_pid3" 2>/dev/null || true
rm -f "$DAEMON_PID_FILE"

# 4. Linux systemd --user path (#6129 naming): with LOOM_SYSTEMD_FORCE=1 driving
#    the shared stub `systemctl`, the daemon-stop step takes the systemd tier
#    (disable --now) and the agent-drain step enumerates + stops every active
#    `loom-agent-*.scope` unit. The ps fixture is left EMPTY so this test
#    isolates the scope-based path from the process-pattern fallback; the unit
#    fixture is populated here (it is empty for every other test).
: > "$PS_FIXTURE"
: > "$SD_STATE_FILE"
: > "$SD_STOP_LOG"
{
    echo "loom-agent-1111-22.scope loaded active running"
    echo "loom-agent-3333-44.scope loaded active running"
} > "$SD_UNITS_FIXTURE"

out4=$( cd "$WORKDIR" && PATH="$STUB_DIR:$PATH" LOOM_SYSTEMD_FORCE=1 \
    bash "$QUIESCE_SCRIPT" 2>&1 )
rc4=$?
assert_eq "0" "$rc4" "systemd scope path: quiesce exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$SD_STATE_FILE" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd scope path: the daemon unit is disabled --now (step 1)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd scope path: the daemon unit is disabled --now (step 1)"
    echo "$out4" | sed 's/^/    /'
fi
TESTS_RUN=$((TESTS_RUN + 1))
if grep -qx 'loom-agent-1111-22.scope' "$SD_STOP_LOG" && grep -qx 'loom-agent-3333-44.scope' "$SD_STOP_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd scope path: every active loom-agent-*.scope unit is stopped (step 2, #6129 naming)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd scope path: every active loom-agent-*.scope unit is stopped (step 2, #6129 naming)"
    echo "  stop log: $(cat "$SD_STOP_LOG")"
fi

# 5. A failed daemon-stop step aborts BEFORE touching any agent process (never
#    escalate to a host-wide process scan on top of a stop that didn't work).
: > "$SD_UNITS_FIXTURE"   # back to "no scopes" for every test after #4
FAIL_STOP_DIR="$WORKDIR/fail-stop-cli"
mkdir -p "$FAIL_STOP_DIR"
cp "$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-quiesce.sh" "$FAIL_STOP_DIR/loom-daemon-quiesce.sh"
cat > "$FAIL_STOP_DIR/loom-daemon-stop.sh" <<'EOF'
#!/usr/bin/env bash
echo "FAKE STOP FAILURE" >&2
exit 1
EOF
chmod +x "$FAIL_STOP_DIR/loom-daemon-quiesce.sh" "$FAIL_STOP_DIR/loom-daemon-stop.sh"
sleep 30 &
agent_pid5=$!
bg_proc_track "$agent_pid5"
{
    echo "$agent_pid5 1 claude -p /loom:judge"
} > "$PS_FIXTURE"

out5=$( cd "$WORKDIR" && PATH="$STUB_DIR:$PATH" env -u LOOM_SYSTEMD_FORCE \
    bash "$FAIL_STOP_DIR/loom-daemon-quiesce.sh" 2>&1 )
rc5=$?
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$rc5" -ne 0 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} failed daemon-stop: quiesce exits non-zero, not a false success"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} failed daemon-stop: quiesce exits non-zero, not a false success"
    echo "$out5" | sed 's/^/    /'
fi
TESTS_RUN=$((TESTS_RUN + 1))
if kill -0 "$agent_pid5" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} failed daemon-stop: the agent-drain step never ran (agent decoy survives)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} failed daemon-stop: the agent-drain step never ran (agent decoy survives)"
fi
kill -9 "$agent_pid5" 2>/dev/null || true

# 6. bash 3.2 compatibility (macOS stock /bin/bash). Under `set -u`, bash < 4.4
#    treats an EMPTY array as unset, so an unguarded `"${STOP_ARGS[@]}"` /
#    `_survivors=("${_still[@]}")` aborts the whole quiesce with "unbound
#    variable" -- on exactly the most common shapes: no --force, and the last
#    agent exiting during the grace window. macOS/launchd is a first-class
#    platform for this script by construction ("the same command on launchd
#    and systemd"), and macOS still ships bash 3.2 as /bin/bash, so this is a
#    real target, not a hypothetical. Skipped (not failed) where no 3.x bash
#    exists, e.g. a Linux CI runner.
LEGACY_BASH=""
if [[ -x /bin/bash ]] && /bin/bash --version 2>/dev/null | head -n1 | grep -q 'version 3\.'; then
    LEGACY_BASH=/bin/bash
fi
if [[ -n "$LEGACY_BASH" ]]; then
    # 6a. Nothing to drain (empty STOP_ARGS + zero process matches).
    : > "$PS_FIXTURE"
    out6a=$( cd "$WORKDIR" && PATH="$STUB_DIR:$PATH" env -u LOOM_SYSTEMD_FORCE \
        LOOM_DAEMON_LAUNCHD=0 LOOM_DAEMON_SYSTEMD=0 \
        "$LEGACY_BASH" "$QUIESCE_SCRIPT" --dry-run 2>&1 )
    rc6a=$?
    assert_eq "0" "$rc6a" "bash 3.2: the nothing-to-drain path exits 0 (no empty-array abort)"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! grep -q 'unbound variable' <<<"$out6a"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} bash 3.2: no 'unbound variable' in the nothing-to-drain path"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} bash 3.2: no 'unbound variable' in the nothing-to-drain path"
        echo "$out6a" | sed 's/^/    /'
    fi

    # 6b. The grace-window survivor loop, whose `_survivors`/`_still` arrays go
    #     EMPTY the moment the last SIGTERMed agent exits -- the second
    #     empty-array hazard, reachable only on a real (non --dry-run) run.
    ( sleep 30 & echo $! > "$DAEMON_PID_FILE" )
    daemon_pid6=$(cat "$DAEMON_PID_FILE")
    bg_proc_track "$daemon_pid6"
    sleep 30 &
    agent_pid6=$!
    bg_proc_track "$agent_pid6"
    { echo "$agent_pid6 1 claude -p /loom:curator"; } > "$PS_FIXTURE"

    out6b=$( cd "$WORKDIR" && PATH="$STUB_DIR:$PATH" env -u LOOM_SYSTEMD_FORCE \
        LOOM_PID_FILE='' LOOM_DAEMON_LAUNCHD=0 LOOM_DAEMON_SYSTEMD=0 LOOM_DAEMON_QUIESCE_GRACE_SECS=3 \
        "$LEGACY_BASH" "$QUIESCE_SCRIPT" 2>&1 )
    rc6b=$?
    assert_eq "0" "$rc6b" "bash 3.2: a real drain through the grace-window survivor loop exits 0"
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! kill -0 "$agent_pid6" 2>/dev/null && ! grep -q 'unbound variable' <<<"$out6b"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} bash 3.2: the agent decoy is stopped and the survivor loop never aborts"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} bash 3.2: the agent decoy is stopped and the survivor loop never aborts"
        echo "$out6b" | sed 's/^/    /'
    fi
    kill -9 "$daemon_pid6" "$agent_pid6" 2>/dev/null || true
    rm -f "$DAEMON_PID_FILE"
else
    echo "· skipped: bash 3.2 compatibility (no 3.x /bin/bash on this host)"
fi

# ---------- summary ----------
echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"

live_state_sandbox_assert_untouched

if [[ "$TESTS_FAILED" -gt 0 ]]; then
    exit 1
fi
exit 0
