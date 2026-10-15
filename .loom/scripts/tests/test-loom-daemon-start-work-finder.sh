#!/usr/bin/env bash
# test-loom-daemon-start-work-finder.sh -- a relaunch through
# loom-daemon-start.sh must not switch the work finder off (#11934).
#
# Two fleet dispatchers came back from `loom-daemon restart --pause
# --then-exit` followed by a plain `loom-daemon daemon-start` with
# LOOM_WORK_FINDER=0 in the daemon's environment, although their config set
# autonomous.workFinder.enabled: true. Three things lined up:
#
#   - the pause recorded an operator stop, which MOVES the autonomy-desired
#     marker aside into `<marker>.stopped` (#9588);
#   - the unit was config-driven, so it carried no LOOM_WORK_FINDER line;
#   - so the autonomy-downgrade check (#5409) found no prior signal, and the
#     plain start exported its FLAGS-OFF default `0`, which the daemon reads
#     before config (env > config > default).
#
# This suite drives the real start stub (`loom-daemon daemon-start`) through
# each of those, and proves the refused start leaves the persisted flags the
# watchdog replays as they were.
#
# WHY A SEPARATE SUITE: test-loom-daemon-start.sh is over the file-size
# ratchet's 1000-line threshold and frozen (.loom/docs/file-size-policy.md).
# Needs a BUILT loom-daemon, so it is wired in ci.yml's "Native Port Suites
# (daemon lifecycle, watchdog)" job. Hermetic: no network, no forge, no real
# supervisor (every real start is --no-launchd --no-systemd).
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-start-work-finder.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-start.sh"

# --self-only for the same reason as test-loom-daemon-start.sh: this suite
# pins $LOOM_DAEMON_BIN to FAKE daemons per invocation.
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin --self-only "$(cd "$SCRIPT_DIR/.." && pwd)" daemon-start

# shellcheck source=lib/live-state-sandbox.sh
source "$SCRIPT_DIR/lib/live-state-sandbox.sh"
live_state_sandbox_snapshot

# #6568: a dispatched sweep exports these; inherited, they arm the
# session-isolation guard on real starts.
unset LOOM_SWEEP_CLAIM_OWNED LOOM_SWEEP_CPU_BUDGET_CORES LOOM_SWEEP_NICED \
    LOOM_TERMINAL_ID LOOM_ROLE LOOM_RUNTIME LOOM_ALLOW_SESSION_DAEMON_START

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

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
        echo "  expected to find: [$needle]"
        echo "  in: $haystack"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
        echo "  expected NOT to find: [$needle]"
    fi
}

WORKDIR="$(mktemp -d)"
# Kill the fixture daemon and WAIT for it to go: the next start's
# already-running guard `kill -0`s the recorded pid (#7287's race).
stop_fixture_daemon() {
    local pid_file="$1" pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    rm -f "$pid_file"
    [[ -n "$pid" ]] || return 0
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.1; done
}
trap '[ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"' EXIT
trap '[ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"; exit 1' INT TERM

if ! live_state_sandbox_init "$WORKDIR/live-state"; then
    echo "FATAL: live-state sandbox init failed (#6420)." >&2
    exit 1
fi

# Prints nothing and exits: for the inspection-mode cases.
FAKE_BIN="$WORKDIR/fake-loom-daemon"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$FAKE_BIN"
chmod +x "$FAKE_BIN"

# ---------- WF1. The operator-stop record stands in for the moved marker ----------
WF1_HOME="$WORKDIR/wf1-home"; mkdir -p "$WF1_HOME/.loom"
printf '%s\n' '# [operator-stop #9588] moved aside' 'operator_stop_reason=pause-then-exit' \
    'started_at=2026-10-14T16:27:00Z' 'work_finder=1' 'health_gate=0' \
    > "$WF1_HOME/.loom/autonomy-desired.stopped"
wf1_out=$( cd "$WORKDIR/live-state" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$WF1_HOME" LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$WF1_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$WF1_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --print-unit 2>&1 >/dev/null )
assert_contains "$wf1_out" "LOOM_WORK_FINDER: 1 -> 0" \
    "WF1: the record's moved-aside work_finder=1 is a prior autonomous run (plain start after --pause --then-exit warns)"

# WF1b. A record with no moved-aside marker (an unsupervised daemon's stop)
#       says nothing about prior autonomy: no warning.
WF1B_HOME="$WORKDIR/wf1b-home"; mkdir -p "$WF1B_HOME/.loom"
printf '%s\n' '# [operator-stop #9588] no marker' 'operator_stop_reason=then-exit' \
    > "$WF1B_HOME/.loom/autonomy-desired.stopped"
wf1b_out=$( cd "$WORKDIR/live-state" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$WF1B_HOME" LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$WF1B_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$WF1B_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --print-unit 2>&1 >/dev/null )
assert_not_contains "$wf1b_out" "autonomy downgrade" \
    "WF1b: a stop record holding no moved-aside marker is not a prior-autonomy signal"

# ---------- a config-enabled repo root: the shape of a fleet dispatcher ----------
WF_REPO="$WORKDIR/dispatcher"; mkdir -p "$WF_REPO/.loom/logs" "$WF_REPO/state"
echo '{"autonomous": {"workFinder": {"enabled": true}}}' > "$WF_REPO/.loom/config.json"
wf_env=(env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE
    HOME="$WF_REPO/state"
    LOOM_SOCKET_PATH="$WF_REPO/state/loom-daemon.sock"
    LOOM_AUTONOMY_MARKER="$WF_REPO/state/autonomy-desired"
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-$$-wf-watchdog")

# WF2. Inspection: a plain start over the config warns where the start's own
#      LOOM_WORK_FINDER=0 is rendered (the plist); --from-config neither warns
#      nor forces LOOM_WORK_FINDER.
wf2_out=$( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_contains "$wf2_out" "autonomous.workFinder.enabled: true" \
    "WF2: a plain start over a config-enabled work finder is a detected downgrade"
# WF2u. A systemd unit never carries the start's 0 (#11951), so the config
#       keeps the work finder on and a plain start is not a downgrade there.
wf2u_out=$( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-unit 2>&1 )
assert_not_contains "$wf2u_out" "autonomy downgrade" \
    "WF2u: a plain start over a config-enabled work finder is not a downgrade on systemd (#11951)"
assert_not_contains "$wf2u_out" "Environment=LOOM_WORK_FINDER=" \
    "WF2u: the systemd unit does not carry the start's LOOM_WORK_FINDER=0 (#11951)"
wf2c_out=$( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --from-config --print-unit 2>&1 )
assert_not_contains "$wf2c_out" "autonomy downgrade" \
    "WF2: --from-config on a config-enabled host is not a downgrade"
assert_not_contains "$wf2c_out" "Environment=LOOM_WORK_FINDER=" \
    "WF2: --from-config leaves LOOM_WORK_FINDER to the config"
wf2n_out=$( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --no-work-finder --print-unit 2>&1 )
assert_not_contains "$wf2n_out" "autonomy downgrade" \
    "WF2: an explicit --no-work-finder is not silent, so not a downgrade"

# WF3. A REAL plain start refuses, writes no pid file, and leaves the
#      persisted flags as they were. The watchdog replays .daemon.flags on
#      recovery: a refused bare start that left it empty would make the next
#      recovery the downgrade this start refused.
printf '%s\n' '--work-finder' > "$WF_REPO/.loom/.daemon.flags"
BG_FAKE_BIN="$WORKDIR/fake-loom-daemon-bg"
printf '%s\n' '#!/usr/bin/env bash' 'sleep 5' > "$BG_FAKE_BIN"
chmod +x "$BG_FAKE_BIN"
wf3_out=$( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$BG_FAKE_BIN" \
    bash "$START_SCRIPT" --no-launchd --no-systemd 2>&1 )
wf3_rc=$?
assert_eq "1" "$wf3_rc" "WF3: a real plain start on a config-enabled host REFUSES rather than force LOOM_WORK_FINDER=0"
assert_contains "$wf3_out" "refusing to start" "WF3: the refusal is explicit"
assert_contains "$wf3_out" "--from-config" "WF3: the refusal names --from-config"
assert_eq "--work-finder" "$(cat "$WF_REPO/.loom/.daemon.flags" 2>/dev/null)" \
    "WF3: a refused start leaves the persisted .daemon.flags intact"
assert_eq "absent" "$([[ -f "$WF_REPO/.loom/.daemon.pid" ]] && echo present || echo absent)" \
    "WF3: a refused start writes no pid file"

# WF3b. With NO prior flags file, the refused start leaves none behind.
rm -f "$WF_REPO/.loom/.daemon.flags"
( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$BG_FAKE_BIN" \
    bash "$START_SCRIPT" --no-launchd --no-systemd >/dev/null 2>&1 )
assert_eq "absent" "$([[ -e "$WF_REPO/.loom/.daemon.flags" ]] && echo present || echo absent)" \
    "WF3b: a refused start with no prior flags file leaves none behind"

# WF4. The start the refusal asks for: --from-config starts, and the daemon
#      inherits NO LOOM_WORK_FINDER, so its config keeps the work finder on.
WF_ENV_BIN="$WORKDIR/fake-loom-daemon-wf-env"
printf '%s\n' '#!/usr/bin/env bash' \
    "echo \"WF=[\${LOOM_WORK_FINDER-unset}]\" > \"$WF_REPO/state/daemon-env\"" \
    'sleep 5' > "$WF_ENV_BIN"
chmod +x "$WF_ENV_BIN"
( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$WF_ENV_BIN" \
    bash "$START_SCRIPT" --no-launchd --no-systemd --from-config >/dev/null 2>&1 )
wf4_rc=$?
for _ in $(seq 1 30); do [[ -s "$WF_REPO/state/daemon-env" ]] && break; sleep 0.2; done
assert_eq "0" "$wf4_rc" "WF4: --from-config on the config-enabled host starts"
assert_eq "WF=[unset]" "$(cat "$WF_REPO/state/daemon-env" 2>/dev/null)" \
    "WF4: the relaunched daemon inherits no LOOM_WORK_FINDER, so its config keeps the work finder on"
assert_eq "--from-config" "$(cat "$WF_REPO/.loom/.daemon.flags" 2>/dev/null)" \
    "WF4: the successful start persists --from-config for the watchdog to replay"
stop_fixture_daemon "$WF_REPO/.loom/.daemon.pid"

# WF5. An explicit --work-finder keeps it on too. (--no-health-gate because
#      the marker WF4 wrote records health_gate= empty, which the existing
#      #5437 check reads conservatively as "unknown, maybe on".)
rm -f "$WF_REPO/state/daemon-env"
wf5_out=$( cd "$WF_REPO" && "${wf_env[@]}" LOOM_DAEMON_BIN="$WF_ENV_BIN" \
    bash "$START_SCRIPT" --no-launchd --no-systemd --work-finder --no-health-gate 2>&1 )
for _ in $(seq 1 30); do [[ -s "$WF_REPO/state/daemon-env" ]] && break; sleep 0.2; done
assert_eq "WF=[1]" "$(cat "$WF_REPO/state/daemon-env" 2>/dev/null)" \
    "WF5: --work-finder on the config-enabled host starts with LOOM_WORK_FINDER=1"
[[ -s "$WF_REPO/state/daemon-env" ]] || echo "  start output: $wf5_out"
stop_fixture_daemon "$WF_REPO/.loom/.daemon.pid"

# WF6. #11951: a systemd unit no longer carries autonomy env, so --work-finder
#      on a host where nothing else turns the work finder on cannot be
#      honoured. The inspection mode names it; a real start refuses on it.
WF6_HOME="$WORKDIR/wf6-home"; mkdir -p "$WF6_HOME/.loom"
wf6_out=$( cd "$WORKDIR/live-state" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$WF6_HOME" LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$WF6_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$WF6_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --work-finder --print-unit 2>&1 )
assert_contains "$wf6_out" "WARNING (#11951): --work-finder asks for LOOM_WORK_FINDER=1" \
    "WF6: an unhonourable --work-finder on systemd is named (#11951)"
assert_eq "" "$(grep '^Environment=LOOM_WORK_FINDER=' <<<"$wf6_out")" \
    "WF6: the unit does not carry --work-finder's value (#11951)"
# WF6b. The same flag with a drop-in that turns the work finder on is met.
mkdir -p "$WF6_HOME/.config/systemd/user/loom-daemon.service.d"
printf '%s\n' '[Service]' 'Environment=LOOM_WORK_FINDER=1' \
    > "$WF6_HOME/.config/systemd/user/loom-daemon.service.d/15-autonomy.conf"
wf6b_out=$( cd "$WORKDIR/live-state" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    -u LOOM_SYSTEMD_UNIT HOME="$WF6_HOME" LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$WF6_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$WF6_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --work-finder --print-unit 2>&1 )
assert_not_contains "$wf6b_out" "(#11951)" \
    "WF6b: --work-finder is met by a drop-in that sets the work finder on"

TESTS_RUN=$((TESTS_RUN + 1))
if live_state_sandbox_assert_untouched; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no live .loom daemon state path was written during the suite (#5191)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} a LIVE .loom daemon state path was written during this test run"
fi

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
