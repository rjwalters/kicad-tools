#!/usr/bin/env bash
# test-loom-daemon-start.sh — Tests for loom-daemon-start.sh autonomous-mode
# env resolution.
#
# Focus (#3911): a bare `loom-daemon-start.sh` must default FLAGS-OFF — it must
# NOT enable the autonomous work finder. Opt-in (--work-finder / --health-gate /
# --from-config) and explicit-off (--no-work-finder) must still behave.
#
# Style matches test-spawn-claude.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Strategy: drive the script in --foreground mode against a FAKE daemon binary
# that prints the LOOM_WORK_FINDER / LOOM_MAIN_HEALTH_GATE it inherited, then
# assert on that marker line. --foreground `exec`s the binary, so the exported
# env is exactly what a real daemon would see.
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-start.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-start.sh"

# Background-PID bookkeeping (#4773): the `sleep 30 &` decoys below stand in
# for a real daemon MainPID and are tracked here so the EXIT/INT/TERM trap can
# reap them even if this suite is interrupted before its own inline `kill`.
# shellcheck source=lib/bg-proc-trap.sh
source "$SCRIPT_DIR/lib/bg-proc-trap.sh"

# Shared live-state sandbox (#5179, adopted here per #5191 — this suite had
# ZERO suite-wide LOOM_PID_FILE-class pins, only per-invocation ones, so any
# call site below that forgot one inherited whatever LOOM_SOCKET_PATH /
# LOOM_AUTONOMY_MARKER / LOOM_MACHINE_CHECKOUT / LOOM_WORKSPACE / LOOM_DAEMON_BIN
# the calling session happened to export). The snapshot MUST run here — before
# live_state_sandbox_init below rewrites the LOOM_* state vars, and before any
# sub-invocation can write anything — because it discovers WHICH paths are the
# live ones by reading the ambient environment. The matching
# live_state_sandbox_assert_untouched runs as the suite's final guard.
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
        echo -e "  expected: [$expected]"
        echo -e "  actual:   [$actual]"
    fi
}

# ---------- fixture ----------
WORKDIR="$(mktemp -d)"
# bg_proc_reap kills the `sleep 30 &` decoys tracked via bg_proc_track below
# (their argv never references $WORKDIR, so a path-pattern pkill would miss
# them); the pkill backstop catches any real nohup'd fake-daemon process this
# suite starts (BG_FAKE_BIN / SH_BG_FAKE_BIN both live under $WORKDIR, so
# their argv always matches). EXIT/INT/TERM (not just EXIT, #4773) so a hard
# interruption of this suite still reaps every tracked/backstopped process.
# NOTE: a bare `trap CMD EXIT INT TERM` runs CMD on INT/TERM but does NOT stop
# the script (only an EXIT-trap firing auto-exits) -- the explicit `exit`
# below is required, else a SIGTERM'd suite would clean up once and then keep
# running every remaining test case (re-populating $WORKDIR as it goes).
trap 'bg_proc_reap; [ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"' EXIT
trap 'bg_proc_reap; [ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"; exit 1' INT TERM
mkdir -p "$WORKDIR/.loom/logs"

# ---------- live daemon state sandbox (#5179, adopted here per #5191) ----------
# ONE helper owns every live-state path this suite could otherwise reach (see
# lib/live-state-sandbox.sh for the full per-variable rationale). This is the
# suite-wide FLOOR, not a replacement for every case: tests below that need a
# specific fixture's OWN scratch HOME still pin LOOM_SOCKET_PATH /
# LOOM_AUTONOMY_MARKER inline on their own invocation (a per-command assignment
# always wins over this exported default, same precedence as before). This
# only closes the call sites that do NOT pin them — e.g. tests 1-7 below run
# with no override at all, so pre-fix they resolved LOOM_SOCKET_PATH's default
# fallback (`${LOOM_SOCKET_PATH:-$HOME/.loom/loom-daemon.sock}` in
# loom-daemon-start.sh) straight onto the REAL $HOME/.loom.
live_state_sandbox_init "$WORKDIR/live-state"

FAKE_BIN="$WORKDIR/fake-loom-daemon"
cat > "$FAKE_BIN" <<'EOF'
#!/usr/bin/env bash
# Prints the autonomous-mode env it inherited, then exits cleanly.
echo "FAKE_DAEMON WF=[${LOOM_WORK_FINDER:-}] HG=[${LOOM_MAIN_HEALTH_GATE:-}]"
EOF
chmod +x "$FAKE_BIN"

# ---------- tests ----------

# 1. Plain start = FLAGS-OFF: work finder off, health gate off.
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[0] HG=[0]" "$out" "plain start defaults both loops OFF (#3911)"

# 2. --work-finder opts the finder on (gate stays off).
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --work-finder --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[1] HG=[0]" "$out" "--work-finder enables finder only"

# 3. --health-gate opts the gate on (finder stays off).
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --health-gate --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[0] HG=[1]" "$out" "--health-gate enables gate only"

# 4. Both flags → both on.
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --work-finder --health-gate --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[1] HG=[1]" "$out" "--work-finder --health-gate enables both"

# 5. Already-exported env wins over the flags-off default.
out=$( ( cd "$WORKDIR" && env -u LOOM_MAIN_HEALTH_GATE LOOM_WORK_FINDER=1 LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[1] HG=[0]" "$out" "exported LOOM_WORK_FINDER=1 wins on plain start"

# 6. --from-config forces neither var (leaves both unset for config to drive).
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[] HG=[]" "$out" "--from-config leaves both env vars unset"

# 7. --no-work-finder forces finder off (explicit; matches default).
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --no-work-finder --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[0] HG=[0]" "$out" "--no-work-finder forces finder off"

# ---------- --from-config composition (#4353) ----------
# --from-config used to be a strict either/or: it never looked at
# --work-finder/--health-gate at all, so pairing it with an explicit flag
# silently dropped the flag. It must now COMPOSE: the named loop is forced,
# the other loop is left unset for config to drive.

# 7a. --from-config --work-finder forces the finder ON, leaves the gate unset
#     for config to drive.
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --work-finder --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[1] HG=[]" "$out" "--from-config --work-finder forces finder ON, gate stays config-driven (#4353)"

# 7b. --from-config --no-work-finder forces the finder OFF, gate stays unset.
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --no-work-finder --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[0] HG=[]" "$out" "--from-config --no-work-finder forces finder OFF, gate stays config-driven (#4353)"

# 7c. --from-config --health-gate forces the gate ON, finder stays unset.
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --health-gate --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[] HG=[1]" "$out" "--from-config --health-gate forces gate ON, finder stays config-driven (#4353)"

# 7d. --from-config --no-health-gate forces the gate OFF, finder stays unset.
out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --no-health-gate --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[] HG=[0]" "$out" "--from-config --no-health-gate forces gate OFF, finder stays config-driven (#4353)"

# 7e. A pre-exported env var still wins over the forced flag under
#     --from-config (env > CLI flag, same precedence as the non-config path).
out=$( ( cd "$WORKDIR" && env -u LOOM_MAIN_HEALTH_GATE LOOM_WORK_FINDER=1 LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --no-work-finder --foreground 2>/dev/null ) | grep '^FAKE_DAEMON' )
assert_eq "FAKE_DAEMON WF=[1] HG=[]" "$out" "pre-exported LOOM_WORK_FINDER=1 wins over --from-config --no-work-finder (#4353)"

# 7f. The startup banner names the forced loop and never prints the
#     "env not forced" wording when something WAS forced.
banner_out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --work-finder --foreground 2>&1 ) )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$banner_out" | grep -q 'forced: work_finder=1' && ! echo "$banner_out" | grep -q 'env not forced'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} startup banner names the forced loop, never prints 'env not forced' when something was forced (#4353)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} startup banner names the forced loop, never prints 'env not forced' when something was forced (#4353)"
    echo "  output: $banner_out"
fi

# 8. --help mentions the FLAGS-OFF default and the opt-in flags.
help_out=$(bash "$START_SCRIPT" --help 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help_out" | grep -qi 'FLAGS-OFF' && echo "$help_out" | grep -q -- '--work-finder'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents the FLAGS-OFF default and --work-finder"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents the FLAGS-OFF default and --work-finder"
fi

# 9. Regression (#3968 flag persistence): a BARE invocation with ZERO args in
#    background mode (the common real-world case — --foreground is not used
#    here on purpose) must not crash. Bash < 4.4 (still /bin/bash on stock
#    macOS) raises "unbound variable" on `"${arr[@]}"` for a zero-element
#    array under `set -u`; the persist-flags loop must guard against that.
#    Also asserts the persisted flags file exists and is empty for a bare start.
#    Needs a fake binary that stays alive (the shared $FAKE_BIN above prints
#    one line and exits immediately, which the start script's own liveness
#    check would correctly treat as a startup failure — not what this test
#    is exercising).
#    --no-launchd forces the legacy nohup path even on a Darwin test runner —
#    this test exercises the flags-persistence array guard, not launchd
#    (#3972), and must never mutate the real machine's LaunchAgents.
#    --no-systemd is the Linux-runner analog (#4268): it forces the nohup path so
#    running this suite on a real systemd host never installs/enables a real
#    `systemd --user` unit.
BG_FAKE_BIN="$WORKDIR/fake-loom-daemon-bg"
cat > "$BG_FAKE_BIN" <<'EOF'
#!/usr/bin/env bash
sleep 5
EOF
chmod +x "$BG_FAKE_BIN"
# LOOM_AUTONOMY_MARKER + LOOM_SOCKET_PATH pin the #4011 autonomy-desired marker
# and heartbeat path into WORKDIR so a real start never writes the operator's
# real ~/.loom/autonomy-desired. LOOM_WATCHDOG_LABEL is a scratch label so even
# if the watchdog script were resolvable here, provisioning could not touch the
# real com.rjwalters.loom-daemon-watchdog LaunchAgent.
( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    LOOM_DAEMON_BIN="$BG_FAKE_BIN" \
    LOOM_SOCKET_PATH="$WORKDIR/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$WORKDIR/.loom/autonomy-desired" \
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-$$-watchdog" \
    bash "$START_SCRIPT" --no-launchd --no-systemd >/dev/null 2>&1 )
bg_rc=$?
assert_eq "0" "$bg_rc" "bare (zero-arg) background start exits 0 (no unbound-variable crash, #3968)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$WORKDIR/.loom/.daemon.flags" && ! -s "$WORKDIR/.loom/.daemon.flags" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} bare start persists an EMPTY .loom/.daemon.flags (no autonomy flags to record)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} bare start persists an EMPTY .loom/.daemon.flags (no autonomy flags to record)"
fi
# #4011: a successful start writes the durable autonomy-desired intent marker
# (into the isolated WORKDIR location pinned above — never the real ~/.loom).
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$WORKDIR/.loom/autonomy-desired" ]] \
    && grep -q '^heartbeat_file=' "$WORKDIR/.loom/autonomy-desired" \
    && grep -q '^use_launchd=false' "$WORKDIR/.loom/autonomy-desired"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} bare start writes the autonomy-desired marker with heartbeat + liveness fields (#4011)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} bare start writes the autonomy-desired marker with heartbeat + liveness fields (#4011)"
fi

# Clean up the background daemon this test started.
if [[ -f "$WORKDIR/.loom/.daemon.pid" ]]; then
    kill "$(cat "$WORKDIR/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
fi

# ---------- machine mode (Epic #3835 Phase 3b, #4229) ----------
# LOOM_MACHINE_CHECKOUT (set by the `scripts/loom` dispatcher, never by a
# direct invocation) overrides BOTH the resolved workdir (the checkout, not
# $PWD) and the pid/flags/startup-log home ($HOME/.loom, not $REPO_ROOT/.loom)
# -- and must work from a directory that has NO .loom/ at all, unlike the
# dev-mode fallback below. Every write targets a SCRATCH $HOME, never the real
# operator ~/.loom.
MACHINE_HOME="$(mktemp -d)"
MACHINE_CHECKOUT="$(mktemp -d)"
NON_REPO_DIR="$(mktemp -d)"

out=$( ( cd "$NON_REPO_DIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$MACHINE_HOME" \
    LOOM_MACHINE_CHECKOUT="$MACHINE_CHECKOUT" \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --foreground 2>&1 ) )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out" | grep -q '^FAKE_DAEMON'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: start succeeds from a NON-REPO directory (no dev-mode '.loom directory not found' refusal)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: start succeeds from a NON-REPO directory"
    echo "  output: $out"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out" | grep -q "Mode:.*machine (workdir: $MACHINE_CHECKOUT"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: plist workdir resolves to the checkout, not \$PWD"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: plist workdir resolves to the checkout, not \$PWD"
    echo "  output: $out"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$MACHINE_HOME/.loom/.daemon.flags" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: persisted flags land under \$HOME/.loom (existing machine-level state home)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: persisted flags land under \$HOME/.loom (existing machine-level state home)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -d "$NON_REPO_DIR/.loom" && ! -d "$MACHINE_CHECKOUT/.loom" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: no runtime state leaks into \$PWD or the checkout"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: no runtime state leaks into \$PWD or the checkout"
fi

# Dev-mode fallback (scope guard, filed AC5): direct invocation with NO
# LOOM_MACHINE_CHECKOUT from that SAME non-repo directory still refuses exactly
# as before #4229 -- machine mode is additive, not a replacement.
out_dev=$( cd "$NON_REPO_DIR" && LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --foreground 2>&1 )
rc_dev=$?
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$rc_dev" -ne 0 ]] && echo "$out_dev" | grep -qi "Not in a Loom workspace"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dev-mode fallback unchanged: refuses from a non-repo dir with no dispatcher (#4229 scope guard)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dev-mode fallback unchanged: refuses from a non-repo dir with no dispatcher (#4229 scope guard)"
fi
rm -rf "$MACHINE_HOME" "$MACHINE_CHECKOUT" "$NON_REPO_DIR"

# ---------- systemd --user service path (#4268) ----------
# The Linux mirror of the launchd path. The suite runs on Darwin runners, so
# detection uses the test-only LOOM_SYSTEMD_FORCE=1 seam in lib/systemd-user.sh
# plus a stub `systemctl` on PATH (mirroring the stub `launchctl` below). Every
# invocation also passes --no-launchd so the systemd branch is reachable on a
# Darwin runner (launchd wins over systemd by platform) and never touches real
# launchd, and pins a scratch LOOM_SYSTEMD_UNIT so a stray real user manager is
# never enabled/disabled.
SD_UNIT="loom-daemon-test-$$.service"

# S1. --print-unit renders the unit with NO side effects (no systemctl, no file
#     write). Assert the six load-bearing fields from the issue's test plan:
#     Restart=on-success, KillMode=mixed (#4862), TimeoutStopSec=20 (#4950),
#     WantedBy=default.target, the baked Environment=LOOM_DAEMON_SUPERVISOR=systemd,
#     and WorkingDirectory=<repo>.
unit_out=$( ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-unit 2>/dev/null ) )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$unit_out" | grep -qx 'Restart=on-success' \
    && echo "$unit_out" | grep -qx 'KillMode=mixed' \
    && echo "$unit_out" | grep -qx 'TimeoutStopSec=20' \
    && echo "$unit_out" | grep -qx 'WantedBy=default.target' \
    && echo "$unit_out" | grep -qx 'Environment=LOOM_DAEMON_SUPERVISOR=systemd' \
    && echo "$unit_out" | grep -qx "WorkingDirectory=$WORKDIR"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --print-unit renders Restart=on-success, KillMode=mixed, TimeoutStopSec=20, WantedBy=default.target, LOOM_DAEMON_SUPERVISOR=systemd, WorkingDirectory=<repo>"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --print-unit renders the expected unit fields"
    echo "$unit_out" | sed 's/^/    /'
fi

# Shared stub systemctl: records every call, answers daemon-reload/enable as
# success and `show -p MainPID --value` with a live pid we control. Structurally
# unable to touch a real unit.
SD_BIN="$WORKDIR/sd-bin"; mkdir -p "$SD_BIN"
SD_MAIN_SLEEP_PID=""
make_sd_stub() {
    local log="$1" mainpid="$2"
    cat > "$SD_BIN/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$log"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show) echo "${mainpid}" ;;
  *)    exit 0 ;;
esac
EOF
    chmod +x "$SD_BIN/systemctl"
}

# S2. Forced systemd path installs + enables the unit and writes the PID file
#     from `systemctl --user show -p MainPID`. A real sleeper stands in for the
#     daemon MainPID so the liveness check (kill -0) passes.
sleep 30 & SD_MAIN_SLEEP_PID=$!
bg_proc_track "$SD_MAIN_SLEEP_PID"
SD_LOG="$WORKDIR/sd-enable.log"; : > "$SD_LOG"
make_sd_stub "$SD_LOG" "$SD_MAIN_SLEEP_PID"
SD_HOME="$(mktemp -d)"; mkdir -p "$SD_HOME/.loom/logs"
sd_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$SD_BIN:$PATH" HOME="$SD_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$SD_UNIT" \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$SD_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$SD_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd 2>&1 )
sd_rc=$?
assert_eq "0" "$sd_rc" "systemd path: start exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- "--user enable --now $SD_UNIT" "$SD_LOG" && grep -q -- '--user daemon-reload' "$SD_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd path: runs daemon-reload + enable --now on the unit"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd path: runs daemon-reload + enable --now on the unit"
    echo "  systemctl calls: $(cat "$SD_LOG")"
fi
# The PID file lands under the repo-root state home ($WORKDIR/.loom in dev mode),
# not under the pinned scratch $HOME (which only relocates the socket/marker).
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(cat "$WORKDIR/.loom/.daemon.pid" 2>/dev/null)" == "$SD_MAIN_SLEEP_PID" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd path: PID file written from systemctl show -p MainPID"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd path: PID file written from systemctl show -p MainPID"
    echo "  pid file: [$(cat "$WORKDIR/.loom/.daemon.pid" 2>/dev/null)] expected [$SD_MAIN_SLEEP_PID]"
fi
rm -f "$WORKDIR/.loom/.daemon.pid"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$SD_HOME/.config/systemd/user/$SD_UNIT" ]] \
    && grep -qx 'Restart=on-success' "$SD_HOME/.config/systemd/user/$SD_UNIT" \
    && grep -qx 'KillMode=mixed' "$SD_HOME/.config/systemd/user/$SD_UNIT" \
    && grep -qx 'TimeoutStopSec=20' "$SD_HOME/.config/systemd/user/$SD_UNIT"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd path: renders the unit file under ~/.config/systemd/user with Restart=on-success + KillMode=mixed (#4862) + TimeoutStopSec=20 (#4950)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd path: renders the unit file under ~/.config/systemd/user with Restart=on-success + KillMode=mixed + TimeoutStopSec=20"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$sd_out" | grep -qi 'enable-linger'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd path: prints the loginctl enable-linger reboot-survival reminder"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd path: prints the loginctl enable-linger reboot-survival reminder"
fi
# S2 uses $WORKDIR as REPO_ROOT, which has no loom-daemon-watchdog.sh fixture
# (a scratch tmpdir, not a real checkout) -- so the watchdog provisioning
# tier degrades to its missing-script skip. Confirm that degrade is a WARNING,
# never a failed start (parity with the launchd branch, #4260 sub-issue D AC).
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$sd_out" | grep -qi 'watchdog.*loom-daemon-watchdog.sh not found'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd path: missing watchdog script degrades to a warning (start already asserted exit 0 above)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd path: missing watchdog script degrades to a warning"
    echo "  output: $sd_out"
fi
kill "$SD_MAIN_SLEEP_PID" 2>/dev/null || true
rm -rf "$SD_HOME"

# ---------- systemd --user watchdog timer (#4260 sub-issue D) ----------
# A repo-like scratch checkout with the REAL loom-daemon-watchdog.sh installed
# (S2's $WORKDIR deliberately has none) so provision_watchdog_job_systemd's
# happy path is actually exercised, not just its missing-script skip above.
WD_REPO="$(mktemp -d)"
mkdir -p "$WD_REPO/.loom/scripts/cli" "$WD_REPO/.loom/logs"
cp "$SCRIPT_DIR/../cli/loom-daemon-watchdog.sh" "$WD_REPO/.loom/scripts/cli/loom-daemon-watchdog.sh"

make_sd_stub_wd() {
    local log="$1" mainpid="$2" fail_wd="$3"
    cat > "$SD_BIN/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$log"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show) echo "${mainpid}" ;;
  enable)
    if [[ "$fail_wd" == "1" && "\$*" == *"-watchdog.timer"* ]]; then exit 1; fi
    exit 0 ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$SD_BIN/systemctl"
}

# WD1. Happy path: timer + service rendered with the correct fields, the timer
#      (not the service) is enable --now'd, and LOOM_WATCHDOG_INTERVAL_SECS
#      drives BOTH OnUnitActiveSec and OnBootSec.
sleep 30 & WD_MAIN_SLEEP_PID=$!
bg_proc_track "$WD_MAIN_SLEEP_PID"
WD_LOG="$WORKDIR/sd-watchdog.log"; : > "$WD_LOG"
make_sd_stub_wd "$WD_LOG" "$WD_MAIN_SLEEP_PID" "0"
WD_HOME="$(mktemp -d)"; mkdir -p "$WD_HOME/.loom/logs"
WD_UNIT="loom-daemon-wd-test-$$.service"
wd_out=$( cd "$WD_REPO" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$SD_BIN:$PATH" HOME="$WD_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$WD_UNIT" LOOM_WATCHDOG_INTERVAL_SECS=42 \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$WD_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$WD_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd 2>&1 )
wd_rc=$?
assert_eq "0" "$wd_rc" "systemd watchdog: start exits 0 with a real watchdog script present"
[[ "$wd_rc" == "0" ]] || echo "  output: $wd_out"
WD_TIMER_UNIT="loom-daemon-wd-test-$$-watchdog.timer"
WD_SVC_UNIT="loom-daemon-wd-test-$$-watchdog.service"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- "--user enable --now $WD_TIMER_UNIT" "$WD_LOG" \
    && ! grep -q -- "--user enable --now $WD_SVC_UNIT" "$WD_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd watchdog: enable --now targets the TIMER unit, not the service"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd watchdog: enable --now targets the TIMER unit, not the service"
    echo "  systemctl calls: $(cat "$WD_LOG")"
fi
WD_TIMER_PATH="$WD_HOME/.config/systemd/user/$WD_TIMER_UNIT"
WD_SVC_PATH="$WD_HOME/.config/systemd/user/$WD_SVC_UNIT"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$WD_TIMER_PATH" ]] \
    && grep -qx 'OnUnitActiveSec=42' "$WD_TIMER_PATH" \
    && grep -qx 'OnBootSec=42' "$WD_TIMER_PATH" \
    && grep -qx "Unit=$WD_SVC_UNIT" "$WD_TIMER_PATH" \
    && grep -qx 'Persistent=false' "$WD_TIMER_PATH"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd watchdog: timer renders OnUnitActiveSec/OnBootSec from LOOM_WATCHDOG_INTERVAL_SECS, Unit=<service>, Persistent=false"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd watchdog: timer renders the expected fields"
    cat "$WD_TIMER_PATH" 2>/dev/null | sed 's/^/    /'
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$WD_SVC_PATH" ]] \
    && grep -qx 'Type=oneshot' "$WD_SVC_PATH" \
    && grep -q "^ExecStart=/bin/bash $WD_REPO/.loom/scripts/cli/loom-daemon-watchdog.sh$" "$WD_SVC_PATH"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd watchdog: service renders Type=oneshot and ExecStart=<watchdog script path>"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd watchdog: service renders Type=oneshot and ExecStart=<watchdog script path>"
    cat "$WD_SVC_PATH" 2>/dev/null | sed 's/^/    /'
fi
kill "$WD_MAIN_SLEEP_PID" 2>/dev/null || true
rm -rf "$WD_HOME"

# WD2. Provisioning failure (stub enable --now on the timer exits 1) is a
#      WARNING, never a failed daemon start.
sleep 30 & WD2_MAIN_SLEEP_PID=$!
bg_proc_track "$WD2_MAIN_SLEEP_PID"
WD2_LOG="$WORKDIR/sd-watchdog-fail.log"; : > "$WD2_LOG"
make_sd_stub_wd "$WD2_LOG" "$WD2_MAIN_SLEEP_PID" "1"
WD2_HOME="$(mktemp -d)"; mkdir -p "$WD2_HOME/.loom/logs"
WD2_UNIT="loom-daemon-wd2-test-$$.service"
wd2_out=$( cd "$WD_REPO" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$SD_BIN:$PATH" HOME="$WD2_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$WD2_UNIT" \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$WD2_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$WD2_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd 2>&1 )
wd2_rc=$?
assert_eq "0" "$wd2_rc" "systemd watchdog: a failed 'enable --now' on the timer does not fail the daemon start"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$wd2_out" | grep -qi 'watchdog.*enable --now failed'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd watchdog: install failure is reported as a warning"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd watchdog: install failure is reported as a warning"
    echo "  output: $wd2_out"
fi
kill "$WD2_MAIN_SLEEP_PID" 2>/dev/null || true
rm -rf "$WD2_HOME"
rm -rf "$WD_REPO"

# S3. Escape hatch: --no-systemd falls back to the nohup path byte-compatibly —
#     the stub systemctl is on PATH and detection is FORCED, yet no systemctl
#     call is made (the whole systemd branch is skipped).
SD_LOG2="$WORKDIR/sd-nohatch.log"; : > "$SD_LOG2"
make_sd_stub "$SD_LOG2" "0"
SD_HOME2="$(mktemp -d)"; mkdir -p "$SD_HOME2/.loom/logs"
( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$SD_BIN:$PATH" HOME="$SD_HOME2" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$SD_UNIT" \
    LOOM_DAEMON_BIN="$BG_FAKE_BIN" \
    LOOM_SOCKET_PATH="$SD_HOME2/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$SD_HOME2/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd --no-systemd >/dev/null 2>&1 )
nohatch_rc=$?
assert_eq "0" "$nohatch_rc" "--no-systemd: start exits 0 (nohup fallback)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -s "$SD_LOG2" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --no-systemd: performs no systemctl call at all (byte-compatible nohup path)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --no-systemd: performs no systemctl call at all"
    echo "  systemctl calls: $(cat "$SD_LOG2")"
fi
if [[ -f "$SD_HOME2/.loom/.daemon.pid" ]]; then
    kill "$(cat "$SD_HOME2/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
fi
rm -rf "$SD_HOME2"

# S4. LOOM_DAEMON_SYSTEMD=0 is the env equivalent of --no-systemd (same skip).
SD_LOG3="$WORKDIR/sd-envoff.log"; : > "$SD_LOG3"
make_sd_stub "$SD_LOG3" "0"
SD_HOME3="$(mktemp -d)"; mkdir -p "$SD_HOME3/.loom/logs"
( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$SD_BIN:$PATH" HOME="$SD_HOME3" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$SD_UNIT" LOOM_DAEMON_SYSTEMD=0 \
    LOOM_DAEMON_BIN="$BG_FAKE_BIN" \
    LOOM_SOCKET_PATH="$SD_HOME3/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$SD_HOME3/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -s "$SD_LOG3" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_DAEMON_SYSTEMD=0: performs no systemctl call (env escape hatch, symmetric with --no-systemd)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_DAEMON_SYSTEMD=0: performs no systemctl call"
    echo "  systemctl calls: $(cat "$SD_LOG3")"
fi
if [[ -f "$SD_HOME3/.loom/.daemon.pid" ]]; then
    kill "$(cat "$SD_HOME3/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
fi
rm -rf "$SD_HOME3"

# S5. --help documents the systemd escape hatch + --print-unit.
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help_out" | grep -q -- '--no-systemd' && echo "$help_out" | grep -q -- '--print-unit'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents --no-systemd and --print-unit"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents --no-systemd and --print-unit"
fi

# ---------- launchd domain resolution (#4130) ----------
# resolve_launchd_domain() (lib/launchd-domain.sh) picks gui/<uid> when the GUI
# (Aqua) domain resolves — byte-for-byte the pre-#4130 default — else the
# SSH-reachable user/<uid> background domain, and honors an explicit
# LOOM_LAUNCHD_DOMAIN override verbatim. Driven through a stub launchctl so both
# the GUI-present and headless (SSH-only) cases are deterministic on any host.
LAUNCHD_DOMAIN_LIB="$(cd "$SCRIPT_DIR/../lib" && pwd)/launchd-domain.sh"
UID_NOW="$(id -u)"
DOMAIN_STUB_DIR="$WORKDIR/domain-stub"
mkdir -p "$DOMAIN_STUB_DIR/gui" "$DOMAIN_STUB_DIR/nogui"
# GUI-present: `launchctl print gui/<uid>` succeeds.
cat > "$DOMAIN_STUB_DIR/gui/launchctl" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == "print" && "$2" == gui/* ]] && exit 0
exit 1
EOF
# Headless (no GUI login): every `launchctl print` fails (the gui/<uid> domain
# does not exist — the `error 125` this issue fixes).
cat > "$DOMAIN_STUB_DIR/nogui/launchctl" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == "print" ]] && exit 1
exit 0
EOF
chmod +x "$DOMAIN_STUB_DIR"/*/launchctl

out=$( env -u LOOM_LAUNCHD_DOMAIN PATH="$DOMAIN_STUB_DIR/gui:$PATH" \
    bash -c "source '$LAUNCHD_DOMAIN_LIB'; resolve_launchd_domain" )
assert_eq "gui/${UID_NOW}" "$out" "resolver picks gui/<uid> when the GUI domain resolves (default path unchanged)"

out=$( env -u LOOM_LAUNCHD_DOMAIN PATH="$DOMAIN_STUB_DIR/nogui:$PATH" \
    bash -c "source '$LAUNCHD_DOMAIN_LIB'; resolve_launchd_domain" )
assert_eq "user/${UID_NOW}" "$out" "resolver falls back to user/<uid> when gui/<uid> does not resolve (headless/SSH)"

out=$( LOOM_LAUNCHD_DOMAIN="user/${UID_NOW}" PATH="$DOMAIN_STUB_DIR/gui:$PATH" \
    bash -c "source '$LAUNCHD_DOMAIN_LIB'; resolve_launchd_domain" )
assert_eq "user/${UID_NOW}" "$out" "LOOM_LAUNCHD_DOMAIN override is honored verbatim (wins over the gui probe)"

# A pinned domain that does NOT resolve is still honored verbatim — it must fail
# loudly downstream (at launchctl bootstrap), never silently fall back further.
out=$( LOOM_LAUNCHD_DOMAIN="gui/${UID_NOW}" PATH="$DOMAIN_STUB_DIR/nogui:$PATH" \
    bash -c "source '$LAUNCHD_DOMAIN_LIB'; resolve_launchd_domain" )
assert_eq "gui/${UID_NOW}" "$out" "override to a non-resolving domain is honored verbatim (no silent fallback)"

# ---------- safehouse fleet-comms status surfacing (#4345) ----------
# One-line static visibility check at start time: `loom-daemon-start.sh` can
# only tell "configured" from "not configured" (a live connection needs the
# daemon's own socket -- `loom-daemon status` covers that, see
# .loom/docs/safehouse.md "New-host onboarding"). Uses the nohup fallback path
# (--no-launchd --no-systemd) so this never touches real launchd/systemd, and a
# background daemon fake bin that stays alive long enough for the "started"
# banner (which the safehouse line is printed alongside) to run.
SH_BG_FAKE_BIN="$WORKDIR/fake-loom-daemon-safehouse-bg"
cat > "$SH_BG_FAKE_BIN" <<'EOF'
#!/usr/bin/env bash
sleep 5
EOF
chmod +x "$SH_BG_FAKE_BIN"

# SH1. No `safehouse` block at all -> "not configured".
SH1_HOME="$(mktemp -d)"
mkdir -p "$SH1_HOME/.loom"
sh1_out=$( ( cd "$SH1_HOME" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    -u LOOM_SAFEHOUSE_ENABLED -u LOOM_SAFEHOUSE_SOCKET -u SAFEHOUSED_SOCKET \
    LOOM_DAEMON_BIN="$SH_BG_FAKE_BIN" \
    LOOM_SOCKET_PATH="$SH1_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$SH1_HOME/.loom/autonomy-desired" \
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-$$-sh1-watchdog" \
    bash "$START_SCRIPT" --no-launchd --no-systemd 2>&1 ) )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$sh1_out" | grep -q '^Safehouse:.*not configured'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} safehouse: no config block -> 'not configured' (#4345)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} safehouse: no config block -> 'not configured' (#4345)"
    echo "  output: $sh1_out"
fi
if [[ -f "$SH1_HOME/.loom/.daemon.pid" ]]; then
    kill "$(cat "$SH1_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
fi
rm -rf "$SH1_HOME"

# SH2. safehouse.enabled=true + socket configured but the path does not exist
#      -> "configured, unreachable" (stderr warn -- captured via 2>&1).
SH2_HOME="$(mktemp -d)"
mkdir -p "$SH2_HOME/.loom"
cat > "$SH2_HOME/.loom/config.json" <<EOF
{"safehouse": {"enabled": true, "socket": "$SH2_HOME/.loom/does-not-exist.sock"}}
EOF
sh2_out=$( ( cd "$SH2_HOME" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    -u LOOM_SAFEHOUSE_ENABLED -u LOOM_SAFEHOUSE_SOCKET -u SAFEHOUSED_SOCKET \
    LOOM_DAEMON_BIN="$SH_BG_FAKE_BIN" \
    LOOM_SOCKET_PATH="$SH2_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$SH2_HOME/.loom/autonomy-desired" \
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-$$-sh2-watchdog" \
    bash "$START_SCRIPT" --no-launchd --no-systemd 2>&1 ) )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$sh2_out" | grep -q '^Safehouse:.*configured, unreachable' \
    && echo "$sh2_out" | grep -q 'does-not-exist.sock'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} safehouse: enabled + missing socket -> 'configured, unreachable' with the resolved path (#4345)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} safehouse: enabled + missing socket -> 'configured, unreachable' with the resolved path (#4345)"
    echo "  output: $sh2_out"
fi
if [[ -f "$SH2_HOME/.loom/.daemon.pid" ]]; then
    kill "$(cat "$SH2_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
fi
rm -rf "$SH2_HOME"

# SH3. safehouse.enabled=true + socket path IS a real bound AF_UNIX socket file
#      -> "configured (socket present ...)". Only `bind()`s (no accept loop
#      needed -- the start wrapper only stat()s the path, it never connects).
SH3_HOME="$(mktemp -d)"
mkdir -p "$SH3_HOME/.loom"
SH3_SOCK="$SH3_HOME/.loom/safehoused.sock"
if command -v python3 >/dev/null 2>&1 \
    && python3 -c "
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind('$SH3_SOCK')
" 2>/dev/null; then
    cat > "$SH3_HOME/.loom/config.json" <<EOF
{"safehouse": {"enabled": true, "socket": "$SH3_SOCK"}}
EOF
    sh3_out=$( ( cd "$SH3_HOME" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
        -u LOOM_SAFEHOUSE_ENABLED -u LOOM_SAFEHOUSE_SOCKET -u SAFEHOUSED_SOCKET \
        -u LOOM_SAFEHOUSE_ROOM -u LOOM_SAFEHOUSE_ROOM_SIGNAL \
        LOOM_DAEMON_BIN="$SH_BG_FAKE_BIN" \
        LOOM_SOCKET_PATH="$SH3_HOME/.loom/loom-daemon.sock" \
        LOOM_AUTONOMY_MARKER="$SH3_HOME/.loom/autonomy-desired" \
        LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-$$-sh3-watchdog" \
        bash "$START_SCRIPT" --no-launchd --no-systemd 2>&1 ) )
    TESTS_RUN=$((TESTS_RUN + 1))
    if echo "$sh3_out" | grep -q '^Safehouse:.*configured (socket present'; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} safehouse: enabled + socket file present -> 'configured (socket present ...)' (#4345)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} safehouse: enabled + socket file present -> 'configured (socket present ...)' (#4345)"
        echo "  output: $sh3_out"
    fi
    # #4464: SH3's config has no `safehouse.room`, so the room-unset caveat MUST
    # be surfaced (omitting room is valid only for a single-room safehoused).
    TESTS_RUN=$((TESTS_RUN + 1))
    if echo "$sh3_out" | grep -q 'safehouse.room is unset'; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} safehouse: room unset -> room caveat surfaced (#4464)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} safehouse: room unset -> room caveat surfaced (#4464)"
        echo "  output: $sh3_out"
    fi
    if [[ -f "$SH3_HOME/.loom/.daemon.pid" ]]; then
        kill "$(cat "$SH3_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    fi
else
    echo "  (skipping SH3: python3 AF_UNIX bind unavailable on this host)"
fi
rm -rf "$SH3_HOME"

# SH4. #4464: same as SH3 but WITH an explicit safehouse.room set -> the caveat
#      must NOT appear (the single-room contract is satisfied by an explicit room).
SH4_HOME="$(mktemp -d)"
mkdir -p "$SH4_HOME/.loom"
SH4_SOCK="$SH4_HOME/.loom/safehoused.sock"
if command -v python3 >/dev/null 2>&1 \
    && python3 -c "
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind('$SH4_SOCK')
" 2>/dev/null; then
    cat > "$SH4_HOME/.loom/config.json" <<EOF
{"safehouse": {"enabled": true, "socket": "$SH4_SOCK", "room": "loom-fleet"}}
EOF
    sh4_out=$( ( cd "$SH4_HOME" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
        -u LOOM_SAFEHOUSE_ENABLED -u LOOM_SAFEHOUSE_SOCKET -u SAFEHOUSED_SOCKET \
        -u LOOM_SAFEHOUSE_ROOM -u LOOM_SAFEHOUSE_ROOM_SIGNAL \
        LOOM_DAEMON_BIN="$SH_BG_FAKE_BIN" \
        LOOM_SOCKET_PATH="$SH4_HOME/.loom/loom-daemon.sock" \
        LOOM_AUTONOMY_MARKER="$SH4_HOME/.loom/autonomy-desired" \
        LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-$$-sh4-watchdog" \
        bash "$START_SCRIPT" --no-launchd --no-systemd 2>&1 ) )
    TESTS_RUN=$((TESTS_RUN + 1))
    if echo "$sh4_out" | grep -q 'safehouse.room is unset'; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} safehouse: room set -> no room caveat (#4464)"
        echo "  output: $sh4_out"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} safehouse: room set -> no room caveat (#4464)"
    fi
    if [[ -f "$SH4_HOME/.loom/.daemon.pid" ]]; then
        kill "$(cat "$SH4_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    fi
else
    echo "  (skipping SH4: python3 AF_UNIX bind unavailable on this host)"
fi
rm -rf "$SH4_HOME"

# ---------- dropped-env-key detection on re-render (#4522) ----------
# Root cause under test: render_launchd_plist / render_systemd_unit render the
# EnvironmentVariables dict / Environment= lines strictly from whatever THIS
# invocation has exported -- so a re-render from a context missing the
# operator's exports (a watchdog, a bare re-run, another tool shelling out to
# this script) used to silently replace a richer installed plist/unit with a
# narrower one (every LOOM_SAFEHOUSE_* key + LOOM_WORK_FINDER gone, no trace).
# warn_dropped_env_keys() now diffs the KEY sets (not values) between the
# installed file and the freshly-rendered replacement and warns before the
# overwrite happens.

# ---------- launchd (plist) path -- exercised read-only via --print-plist,
# same technique as the #4172 PATH-drift tests above (the real launchd
# install branch is Darwin-gated with no test-force seam, matching every
# other launchd install test in this suite/repo).
DEK_HOME="$(mktemp -d)"
mkdir -p "$DEK_HOME/Library/LaunchAgents"
DEK_LABEL="com.rjwalters.loom-daemon-dek-test"
DEK_LIVE_PLIST="$DEK_HOME/Library/LaunchAgents/${DEK_LABEL}.plist"

# DEK1. First-ever install (no existing plist) must NOT warn.
dek1_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_SAFEHOUSE_ENABLED \
    HOME="$DEK_HOME" LOOM_MACHINE_CHECKOUT="$DEK_HOME" LOOM_LAUNCHD_LABEL="$DEK_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$dek1_out" | grep -qi 'drops.*env key'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (plist): first-ever install (no existing plist) never warns (#4522)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (plist): first-ever install (no existing plist) never warns"
    echo "  output: $dek1_out"
fi

# Install the "rich" plist (LOOM_SAFEHOUSE_ENABLED + LOOM_WORK_FINDER present).
env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE HOME="$DEK_HOME" LOOM_MACHINE_CHECKOUT="$DEK_HOME" LOOM_LAUNCHD_LABEL="$DEK_LABEL" \
    LOOM_SAFEHOUSE_ENABLED=1 LOOM_WORK_FINDER=1 LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist > "$DEK_LIVE_PLIST" 2>/dev/null

# DEK2. Re-rendering with LOOM_SAFEHOUSE_ENABLED no longer exported warns and
#       names the dropped key, with the LOOM_SAFEHOUSE_* migration hint.
dek2_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_SAFEHOUSE_ENABLED \
    HOME="$DEK_HOME" LOOM_MACHINE_CHECKOUT="$DEK_HOME" LOOM_LAUNCHD_LABEL="$DEK_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dek2_out" | grep -qi 'drops 1 env key' && echo "$dek2_out" | grep -q 'LOOM_SAFEHOUSE_ENABLED'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (plist): re-render missing an exported key warns and names it (#4522)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (plist): re-render missing an exported key warns and names it"
    echo "  output: $dek2_out"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dek2_out" | grep -q 'safehouse.*block.*--from-config'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (plist): LOOM_SAFEHOUSE_* drop prints the config-tier migration hint (#4353)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (plist): LOOM_SAFEHOUSE_* drop prints the config-tier migration hint"
    echo "  output: $dek2_out"
fi

# DEK3. Re-rendering with the SAME (or a superset of) keys does not warn.
dek3_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE HOME="$DEK_HOME" LOOM_MACHINE_CHECKOUT="$DEK_HOME" LOOM_LAUNCHD_LABEL="$DEK_LABEL" \
    LOOM_SAFEHOUSE_ENABLED=1 LOOM_WORK_FINDER=1 LOOM_DAEMON_PATH_EXTRA=/extra/bin LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$dek3_out" | grep -qi 'drops.*env key'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (plist): re-render with the same/superset keys never warns (#4522)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (plist): re-render with the same/superset keys never warns"
    echo "  output: $dek3_out"
fi

# DEK4. --force-env suppresses the warning for an intentional narrowing.
dek4_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_SAFEHOUSE_ENABLED \
    HOME="$DEK_HOME" LOOM_MACHINE_CHECKOUT="$DEK_HOME" LOOM_LAUNCHD_LABEL="$DEK_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist --force-env 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$dek4_out" | grep -qi 'drops.*env key'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (plist): --force-env suppresses the warning (#4522)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (plist): --force-env suppresses the warning"
    echo "  output: $dek4_out"
fi
rm -rf "$DEK_HOME"

# ---------- systemd (unit) path -- exercised through the REAL install site
# (mv over $SYSTEMD_UNIT_PATH), reusing the LOOM_SYSTEMD_FORCE + stub
# systemctl seam already established above (S1-S5) so this covers the actual
# overwrite code path, not just a read-only render.
DEK_SD_BIN="$WORKDIR/dek-sd-bin"; mkdir -p "$DEK_SD_BIN"
DEK_SD_LOG="$WORKDIR/dek-sd.log"; : > "$DEK_SD_LOG"
cat > "$DEK_SD_BIN/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$DEK_SD_LOG"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show) echo "\$\$" ;;
  *)    exit 0 ;;
esac
EOF
chmod +x "$DEK_SD_BIN/systemctl"
DEK_SD_UNIT="loom-daemon-dek-test-$$.service"
DEK_SD_HOME="$(mktemp -d)"; mkdir -p "$DEK_SD_HOME/.loom/logs"
DEK_SD_UNIT_PATH="$DEK_SD_HOME/.config/systemd/user/$DEK_SD_UNIT"

# DEK5. First-ever install (no existing unit) must NOT warn.
dek5_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_SAFEHOUSE_ENABLED \
    PATH="$DEK_SD_BIN:$PATH" HOME="$DEK_SD_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$DEK_SD_UNIT" \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$DEK_SD_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$DEK_SD_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$dek5_out" | grep -qi 'drops.*env key'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (systemd): first-ever install (no existing unit) never warns (#4522)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): first-ever install (no existing unit) never warns"
    echo "  output: $dek5_out"
fi
if [[ -f "$DEK_SD_HOME/.loom/.daemon.pid" ]]; then
    kill "$(cat "$DEK_SD_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    rm -f "$DEK_SD_HOME/.loom/.daemon.pid"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$DEK_SD_UNIT_PATH" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (systemd): first-ever install still writes the unit file"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): first-ever install still writes the unit file"
fi

# DEK6. Re-render with fewer keys (LOOM_SAFEHOUSE_ENABLED dropped from the
#       invoking env) warns and names the dropped key, before the unit is
#       overwritten. Re-run with the rich env first so the installed unit
#       actually carries the key.
: > "$DEK_SD_LOG"
sleep 30 & DEK_SD_PID1=$!
bg_proc_track "$DEK_SD_PID1"
( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$DEK_SD_BIN:$PATH" HOME="$DEK_SD_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$DEK_SD_UNIT" \
    LOOM_SAFEHOUSE_ENABLED=1 LOOM_WORK_FINDER=1 \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$DEK_SD_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$DEK_SD_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd >/dev/null 2>&1 )
kill "$DEK_SD_PID1" 2>/dev/null || true
if [[ -f "$DEK_SD_HOME/.loom/.daemon.pid" ]]; then
    kill "$(cat "$DEK_SD_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    rm -f "$DEK_SD_HOME/.loom/.daemon.pid"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$DEK_SD_UNIT_PATH" ]] && grep -q 'LOOM_SAFEHOUSE_ENABLED' "$DEK_SD_UNIT_PATH"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (systemd): fixture install carries LOOM_SAFEHOUSE_ENABLED (setup for DEK6b)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): fixture install carries LOOM_SAFEHOUSE_ENABLED"
    cat "$DEK_SD_UNIT_PATH" 2>/dev/null | sed 's/^/    /'
fi

sleep 30 & DEK_SD_PID2=$!
bg_proc_track "$DEK_SD_PID2"
dek6_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_SAFEHOUSE_ENABLED \
    PATH="$DEK_SD_BIN:$PATH" HOME="$DEK_SD_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$DEK_SD_UNIT" \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$DEK_SD_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$DEK_SD_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd 2>&1 )
kill "$DEK_SD_PID2" 2>/dev/null || true
if [[ -f "$DEK_SD_HOME/.loom/.daemon.pid" ]]; then
    kill "$(cat "$DEK_SD_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    rm -f "$DEK_SD_HOME/.loom/.daemon.pid"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dek6_out" | grep -qi 'drops 1 env key' && echo "$dek6_out" | grep -q 'LOOM_SAFEHOUSE_ENABLED' \
    && echo "$dek6_out" | grep -q 'safehouse.*block.*--from-config'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (systemd): real re-render missing a key warns, names it, and prints the migration hint (#4522)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): real re-render missing a key warns, names it, and prints the migration hint"
    echo "  output: $dek6_out"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$DEK_SD_UNIT_PATH" ]] && ! grep -q 'LOOM_SAFEHOUSE_ENABLED' "$DEK_SD_UNIT_PATH"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (systemd): the WARNED narrowing still actually installs (warn, don't block)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): the WARNED narrowing still actually installs"
fi

# DEK7. --force-env suppresses the warning on the real install path too.
sleep 30 & DEK_SD_PID3=$!
bg_proc_track "$DEK_SD_PID3"
( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$DEK_SD_BIN:$PATH" HOME="$DEK_SD_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$DEK_SD_UNIT" \
    LOOM_SAFEHOUSE_ENABLED=1 LOOM_WORK_FINDER=1 \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$DEK_SD_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$DEK_SD_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd >/dev/null 2>&1 )
kill "$DEK_SD_PID3" 2>/dev/null || true
if [[ -f "$DEK_SD_HOME/.loom/.daemon.pid" ]]; then
    kill "$(cat "$DEK_SD_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    rm -f "$DEK_SD_HOME/.loom/.daemon.pid"
fi
sleep 30 & DEK_SD_PID4=$!
bg_proc_track "$DEK_SD_PID4"
dek7_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_SAFEHOUSE_ENABLED \
    PATH="$DEK_SD_BIN:$PATH" HOME="$DEK_SD_HOME" \
    LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$DEK_SD_UNIT" \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$DEK_SD_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$DEK_SD_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd --force-env 2>&1 )
kill "$DEK_SD_PID4" 2>/dev/null || true
if [[ -f "$DEK_SD_HOME/.loom/.daemon.pid" ]]; then
    kill "$(cat "$DEK_SD_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    rm -f "$DEK_SD_HOME/.loom/.daemon.pid"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$dek7_out" | grep -qi 'drops.*env key'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (systemd): --force-env suppresses the warning on the real install path (#4522)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): --force-env suppresses the warning on the real install path"
    echo "  output: $dek7_out"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$dek7_out" | grep -q -- '--force-env'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): --force-env is excluded from the persisted .daemon.flags file"
elif [[ -f "$DEK_SD_HOME/.loom/.daemon.flags" ]] && grep -q -- '--force-env' "$DEK_SD_HOME/.loom/.daemon.flags"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dropped-env-key (systemd): --force-env is excluded from the persisted .daemon.flags file"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dropped-env-key (systemd): --force-env is excluded from the persisted .daemon.flags file"
fi
rm -rf "$DEK_SD_HOME"

# SH5. #4225: attention-class routing supplies the room via
#      safehouse.rooms.signal instead of the scalar safehouse.room -> the
#      room-unset caveat must NOT appear (the resolver falls back from
#      rooms.signal to room, so a host with either one is satisfied).
SH5_HOME="$(mktemp -d)"
mkdir -p "$SH5_HOME/.loom"
SH5_SOCK="$SH5_HOME/.loom/safehoused.sock"
if command -v python3 >/dev/null 2>&1 \
    && python3 -c "
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind('$SH5_SOCK')
" 2>/dev/null; then
    cat > "$SH5_HOME/.loom/config.json" <<EOF
{"safehouse": {"enabled": true, "socket": "$SH5_SOCK",
  "rooms": {"signal": "!signal:example.org", "byRepo": {"loom": "!fleet-loom:example.org"}}}}
EOF
    sh5_out=$( ( cd "$SH5_HOME" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
        -u LOOM_SAFEHOUSE_ENABLED -u LOOM_SAFEHOUSE_SOCKET -u SAFEHOUSED_SOCKET \
        -u LOOM_SAFEHOUSE_ROOM -u LOOM_SAFEHOUSE_ROOM_SIGNAL \
        LOOM_DAEMON_BIN="$SH_BG_FAKE_BIN" \
        LOOM_SOCKET_PATH="$SH5_HOME/.loom/loom-daemon.sock" \
        LOOM_AUTONOMY_MARKER="$SH5_HOME/.loom/autonomy-desired" \
        LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-$$-sh5-watchdog" \
        bash "$START_SCRIPT" --no-launchd --no-systemd 2>&1 ) )
    TESTS_RUN=$((TESTS_RUN + 1))
    if echo "$sh5_out" | grep -q 'safehouse.room is unset'; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} safehouse: rooms.signal set -> no room caveat (#4225)"
        echo "  output: $sh5_out"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} safehouse: rooms.signal set -> no room caveat (#4225)"
    fi
    if [[ -f "$SH5_HOME/.loom/.daemon.pid" ]]; then
        kill "$(cat "$SH5_HOME/.loom/.daemon.pid" 2>/dev/null)" 2>/dev/null || true
    fi
else
    echo "  (skipping SH5: python3 AF_UNIX bind unavailable on this host)"
fi
rm -rf "$SH5_HOME"

# ---------- silent autonomy-downgrade detection (#4693) ----------
# Incident 2026-07-30: a plain `loom-daemon-start.sh` run silently re-rendered
# the plist with LOOM_WORK_FINDER=0, downgrading a previously autonomous
# daemon to FLAGS-OFF with no warning. check_autonomy_downgrade_key /
# warn_autonomy_downgrade close the SILENT part of that transition (the
# FLAGS-OFF-by-default policy for a start with NO prior-autonomy signal, #3911,
# stays unchanged -- these tests assert that too).

# ---------- launchd (plist) path -- exercised read-only via --print-plist,
# same technique as the #4522 dropped-env-key tests above.
AD_HOME="$(mktemp -d)"
mkdir -p "$AD_HOME/Library/LaunchAgents"
AD_LABEL="com.rjwalters.loom-daemon-ad-test"
AD_LIVE_PLIST="$AD_HOME/Library/LaunchAgents/${AD_LABEL}.plist"

# AD1. Prior plist had LOOM_WORK_FINDER=1; a plain re-render (no flags) warns
#      and names the exact transition. This is the issue's required test case:
#      "prior-plist-had-work-finder + plain restart -> warning emitted".
env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE HOME="$AD_HOME" LOOM_MACHINE_CHECKOUT="$AD_HOME" LOOM_LAUNCHD_LABEL="$AD_LABEL" \
    LOOM_WORK_FINDER=1 LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist > "$AD_LIVE_PLIST" 2>/dev/null

ad1_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$AD_HOME" LOOM_MACHINE_CHECKOUT="$AD_HOME" LOOM_LAUNCHD_LABEL="$AD_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$ad1_out" | grep -qi 'autonomy downgrade' && echo "$ad1_out" | grep -q 'LOOM_WORK_FINDER: 1 -> 0'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): prior-plist-had-work-finder + plain restart warns and names the transition (#4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): prior-plist-had-work-finder + plain restart warns and names the transition"
    echo "  output: $ad1_out"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$ad1_out" | grep -qi -- '--from-config' && echo "$ad1_out" | grep -qi -- '--work-finder'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): warning names the --from-config / --work-finder remediation (#4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): warning names the --from-config / --work-finder remediation"
    echo "  output: $ad1_out"
fi

# AD1b. Platform-independence regression (#4709 review): --print-plist is a pure
#       INSPECTION mode, so the prior-plist comparison must resolve regardless of
#       whether this host would actually use launchd -- exactly like the
#       pre-existing --print-plist PATH-drift (#4172) / dropped-env-key (#4522)
#       checks, which read $HOME/Library/LaunchAgents/<label>.plist
#       unconditionally. LOOM_DAEMON_LAUNCHD=0 forces USE_LAUNCHD=false, which is
#       the permanent state on every Linux host (incl. the CI runner): when the
#       resolution was gated on USE_LAUNCHD, the whole warning was silently
#       unreachable there -- the exact silence this feature exists to eliminate.
ad1b_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$AD_HOME" LOOM_MACHINE_CHECKOUT="$AD_HOME" LOOM_LAUNCHD_LABEL="$AD_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_DAEMON_LAUNCHD=0 \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$ad1b_out" | grep -qi 'autonomy downgrade' && echo "$ad1b_out" | grep -q 'LOOM_WORK_FINDER: 1 -> 0'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): --print-plist warns even when this host would NOT use launchd (platform-independent, #4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): --print-plist warns even when this host would NOT use launchd"
    echo "  output: $ad1b_out"
fi

# AD2. --from-config is exempt entirely -- control is deliberately handed to
#      .loom/config.json, so re-rendering FLAGS-OFF-equivalent (both vars left
#      unset) after a WORK_FINDER=1 prior install must NOT warn.
ad2_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$AD_HOME" LOOM_MACHINE_CHECKOUT="$AD_HOME" LOOM_LAUNCHD_LABEL="$AD_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist --from-config 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$ad2_out" | grep -qi 'autonomy downgrade'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): --from-config is exempt (control explicitly handed to config, #4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): --from-config is exempt"
    echo "  output: $ad2_out"
fi

# AD3. An explicit --no-work-finder THIS invocation is not silent -- no warning.
ad3_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$AD_HOME" LOOM_MACHINE_CHECKOUT="$AD_HOME" LOOM_LAUNCHD_LABEL="$AD_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist --no-work-finder 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$ad3_out" | grep -qi 'autonomy downgrade'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): explicit --no-work-finder is not silent -- no warning (#4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): explicit --no-work-finder is not silent"
    echo "  output: $ad3_out"
fi

# AD4. An operator-exported LOOM_WORK_FINDER=0 in the calling shell is also an
#      explicit, non-default signal -- no warning.
ad4_out=$( env -u LOOM_MAIN_HEALTH_GATE LOOM_WORK_FINDER=0 \
    HOME="$AD_HOME" LOOM_MACHINE_CHECKOUT="$AD_HOME" LOOM_LAUNCHD_LABEL="$AD_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$ad4_out" | grep -qi 'autonomy downgrade'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): a pre-exported LOOM_WORK_FINDER=0 is not silent -- no warning (#4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): a pre-exported LOOM_WORK_FINDER=0 is not silent"
    echo "  output: $ad4_out"
fi
rm -rf "$AD_HOME"

# AD5. No transition (prior plist already had LOOM_WORK_FINDER=0) -> no
#      warning at start time; a STANDING marker-vs-FLAGS-OFF mismatch with no
#      fresh transition is `loom-daemon status`'s job (AC3), not this check's.
AD5_HOME="$(mktemp -d)"
mkdir -p "$AD5_HOME/Library/LaunchAgents"
AD5_LABEL="com.rjwalters.loom-daemon-ad5-test"
AD5_LIVE_PLIST="$AD5_HOME/Library/LaunchAgents/${AD5_LABEL}.plist"
env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE HOME="$AD5_HOME" LOOM_MACHINE_CHECKOUT="$AD5_HOME" LOOM_LAUNCHD_LABEL="$AD5_LABEL" \
    LOOM_WORK_FINDER=0 LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist > "$AD5_LIVE_PLIST" 2>/dev/null
ad5_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$AD5_HOME" LOOM_MACHINE_CHECKOUT="$AD5_HOME" LOOM_LAUNCHD_LABEL="$AD5_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$ad5_out" | grep -qi 'autonomy downgrade'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): no warning when the prior value was already 0 (no fresh transition, #4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): no warning when the prior value was already 0"
    echo "  output: $ad5_out"
fi
rm -rf "$AD5_HOME"

# AD6. No prior plist AND no marker (clean first-ever install) -> no warning.
AD6_HOME="$(mktemp -d)"
mkdir -p "$AD6_HOME/Library/LaunchAgents"
ad6_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$AD6_HOME" LOOM_MACHINE_CHECKOUT="$AD6_HOME" LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon-ad6-test" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$ad6_out" | grep -qi 'autonomy downgrade'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): clean first-ever install (no prior plist, no marker) never warns (#4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): clean first-ever install never warns"
    echo "  output: $ad6_out"
fi
rm -rf "$AD6_HOME"

# AD7. Marker present but no prior plist/unit value could be read (e.g. the
#      first Darwin start after a nohup-only history) -- the marker ALONE is
#      sufficient to flag the downgrade (the issue's explicit "or the
#      autonomy-desired marker is present" clause).
AD7_HOME="$(mktemp -d)"
mkdir -p "$AD7_HOME/Library/LaunchAgents" "$AD7_HOME/.loom"
: > "$AD7_HOME/.loom/autonomy-desired"
ad7_out=$( env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    HOME="$AD7_HOME" LOOM_MACHINE_CHECKOUT="$AD7_HOME" LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon-ad7-test" LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_AUTONOMY_MARKER="$AD7_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$ad7_out" | grep -qi 'autonomy downgrade' && echo "$ad7_out" | grep -q 'autonomy-desired marker'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (plist): marker present + no readable prior value still warns (#4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (plist): marker present + no readable prior value still warns"
    echo "  output: $ad7_out"
fi
rm -rf "$AD7_HOME"

# ---------- systemd (unit) path -- exercised through the REAL install site,
# reusing the LOOM_SYSTEMD_FORCE + stub systemctl seam (SD_BIN / make_sd_stub,
# defined above at S2) so this covers the actual overwrite code path, not just
# a read-only render.
AD_SD_UNIT="loom-daemon-ad-test-$$.service"
AD8_HOME="$(mktemp -d)"; mkdir -p "$AD8_HOME/.loom/logs"
AD8_LOG="$WORKDIR/ad8-install.log"; : > "$AD8_LOG"

# Install the "prior" unit with LOOM_WORK_FINDER=1.
sleep 30 & AD8_SLEEP_PID1=$!
bg_proc_track "$AD8_SLEEP_PID1"
make_sd_stub "$AD8_LOG" "$AD8_SLEEP_PID1"
( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$SD_BIN:$PATH" HOME="$AD8_HOME" LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$AD_SD_UNIT" \
    LOOM_WORK_FINDER=1 LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$AD8_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$AD8_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd >/dev/null 2>&1 )
kill "$AD8_SLEEP_PID1" 2>/dev/null || true
rm -f "$AD8_HOME/.loom/.daemon.pid"

# AD8. A plain re-install (no flags) both warns AND still actually
#      installs/starts (advisory only, never blocks -- matching the #4522
#      dropped-env-key precedent). This is the issue's required test case:
#      "prior-plist-had-work-finder + plain restart -> warning emitted",
#      systemd sibling.
sleep 30 & AD8_SLEEP_PID2=$!
bg_proc_track "$AD8_SLEEP_PID2"
make_sd_stub "$AD8_LOG" "$AD8_SLEEP_PID2"
ad8_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    PATH="$SD_BIN:$PATH" HOME="$AD8_HOME" LOOM_SYSTEMD_FORCE=1 LOOM_SYSTEMD_UNIT="$AD_SD_UNIT" \
    LOOM_DAEMON_BIN="$FAKE_BIN" \
    LOOM_SOCKET_PATH="$AD8_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$AD8_HOME/.loom/autonomy-desired" \
    bash "$START_SCRIPT" --no-launchd 2>&1 )
ad8_rc=$?
assert_eq "0" "$ad8_rc" "autonomy downgrade (systemd): the WARNED downgrade still actually installs/starts (warn, don't block, #4693)"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$ad8_out" | grep -qi 'autonomy downgrade' && echo "$ad8_out" | grep -q 'LOOM_WORK_FINDER: 1 -> 0'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} autonomy downgrade (systemd): prior-unit-had-work-finder + plain restart warns and names the transition (#4693)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} autonomy downgrade (systemd): prior-unit-had-work-finder + plain restart warns and names the transition"
    echo "  output: $ad8_out"
fi
kill "$AD8_SLEEP_PID2" 2>/dev/null || true
rm -f "$AD8_HOME/.loom/.daemon.pid"
rm -rf "$AD8_HOME"

# ---------- KillMode=mixed real-systemd regression (#4862) ----------
# The stub-based systemd tests above assert the RENDERED TEXT of the unit
# (Restart=on-success, KillMode=mixed present) but never exercise a real
# `systemd --user` manager, so they cannot catch a regression in what those
# fields actually DO. This block drives the ACTUAL production-rendered unit
# (via --print-unit, only ExecStart/WorkingDirectory substituted) against a
# real `systemctl --user` when one is reachable, proving BOTH halves of the
# issue's acceptance criteria in one shot:
#   MX1. a clean exit(0) with a SIGTERM-ignoring lingering child (standing in
#        for an in-flight claude/tee/sleep sweep worker) still causes
#        Restart=on-success to fire — the #4862 fix.
#   MX2. a crash exit(1) does NOT get restarted — the #4054 no-crash-loop
#        property must survive the #4862 change (KillMode=mixed touches only
#        HOW leftover cgroup members are reaped, never the crash/on-success
#        exit-code contract).
# Skips cleanly (not a failure) when no reachable `systemd --user` manager
# exists — e.g. a Darwin CI runner, or a sandboxed host with no user
# session/bus. Real unit files land under the ACTUAL $HOME (systemd --user
# cannot be redirected via an env HOME override the way the stub-based tests
# above redirect it), uniquely named with $$ so a leftover from an
# interrupted run cannot collide with a later one.
MX_HAVE_SYSTEMD=false
if command -v systemctl >/dev/null 2>&1 && [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    mx_state="$(systemctl --user is-system-running 2>/dev/null)"
    [[ "$mx_state" != "offline" && -n "$mx_state" ]] && MX_HAVE_SYSTEMD=true
fi
if [[ "$MX_HAVE_SYSTEMD" == "true" ]]; then
    MX_UNIT_MIXED="loom-daemon-test-mx-mixed-$$.service"
    MX_UNIT_CRASH="loom-daemon-test-mx-crash-$$.service"
    MX_UNIT_DIR="$HOME/.config/systemd/user"
    MX_SCRIPT_DIR="$WORKDIR/mx-scripts"
    mkdir -p "$MX_UNIT_DIR" "$MX_SCRIPT_DIR"

    mx_cleanup() {
        systemctl --user stop "$MX_UNIT_MIXED" "$MX_UNIT_CRASH" >/dev/null 2>&1 || true
        rm -f "$MX_UNIT_DIR/$MX_UNIT_MIXED" "$MX_UNIT_DIR/$MX_UNIT_CRASH"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user reset-failed "$MX_UNIT_MIXED" "$MX_UNIT_CRASH" >/dev/null 2>&1 || true
    }
    # Extend the suite-wide traps (not replace) so an interruption mid-MX-block
    # still tears down these REAL systemd units, not just $WORKDIR.
    trap 'mx_cleanup; bg_proc_reap; [ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"' EXIT
    trap 'mx_cleanup; bg_proc_reap; [ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"; exit 1' INT TERM
    mx_cleanup

    # mx-main.sh: forks a SIGTERM-ignoring child (stand-in for a lingering
    # claude/tee/sleep sweep worker in the SAME cgroup), then exits 0 quickly
    # — mirrors the incident's "clean main-process exit, children still
    # running" shape.
    cat > "$MX_SCRIPT_DIR/mx-main.sh" <<'MXEOF'
#!/bin/bash
trap '' TERM
(trap '' TERM; sleep 30) &
sleep 1
exit 0
MXEOF
    chmod +x "$MX_SCRIPT_DIR/mx-main.sh"
    cat > "$MX_SCRIPT_DIR/mx-crash.sh" <<'MXEOF'
#!/bin/bash
sleep 1
exit 1
MXEOF
    chmod +x "$MX_SCRIPT_DIR/mx-crash.sh"

    # Render via the ACTUAL production code path, then retarget ExecStart/
    # WorkingDirectory at the tiny fixture script above -- proves the SHIPPED
    # unit shape (not a hand-rolled duplicate) reproduces the fix.
    mx_render() {
        local exec_script="$1"
        ( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
            LOOM_DAEMON_BIN="$exec_script" bash "$START_SCRIPT" --print-unit 2>/dev/null ) \
            | sed -e "s|^ExecStart=.*|ExecStart=/bin/bash $exec_script|" \
                  -e "s|^WorkingDirectory=.*|WorkingDirectory=$MX_SCRIPT_DIR|"
    }
    mx_render "$MX_SCRIPT_DIR/mx-main.sh" > "$MX_UNIT_DIR/$MX_UNIT_MIXED"
    mx_render "$MX_SCRIPT_DIR/mx-crash.sh" > "$MX_UNIT_DIR/$MX_UNIT_CRASH"
    systemctl --user daemon-reload >/dev/null 2>&1 || true

    # MX1. Clean exit + lingering child -> Restart=on-success fires (NRestarts
    # climbs above 0 within a few restart cycles). Poll up to ~8s.
    systemctl --user start "$MX_UNIT_MIXED" >/dev/null 2>&1
    mx1_restarts=0
    for _ in 1 2 3 4 5 6 7 8; do
        mx1_restarts="$(systemctl --user show -p NRestarts --value "$MX_UNIT_MIXED" 2>/dev/null)"
        [[ "$mx1_restarts" =~ ^[0-9]+$ ]] && (( mx1_restarts > 0 )) && break
        sleep 1
    done
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$mx1_restarts" =~ ^[0-9]+$ ]] && (( mx1_restarts > 0 )); then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} real systemd (#4862): clean exit + lingering child -> Restart=on-success fires (NRestarts=$mx1_restarts)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} real systemd (#4862): clean exit + lingering child -> Restart=on-success fires"
        echo "  systemctl --user status ${MX_UNIT_MIXED}:"
        systemctl --user status "$MX_UNIT_MIXED" --no-pager -l 2>&1 | sed 's/^/    /'
    fi
    systemctl --user stop "$MX_UNIT_MIXED" >/dev/null 2>&1 || true

    # MX2. Crash exit(1) -> stays down (the #4054 no-crash-loop property).
    # Wait past the fixture's own 1s sleep + a restart-cycle margin, then
    # assert BOTH that it never restarted and that it is in a failed/inactive
    # (not activating/running) state.
    systemctl --user start "$MX_UNIT_CRASH" >/dev/null 2>&1
    sleep 3
    mx2_restarts="$(systemctl --user show -p NRestarts --value "$MX_UNIT_CRASH" 2>/dev/null)"
    mx2_active="$(systemctl --user show -p ActiveState --value "$MX_UNIT_CRASH" 2>/dev/null)"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$mx2_restarts" == "0" ]] && [[ "$mx2_active" != "active" && "$mx2_active" != "activating" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} real systemd (#4862): crash exit(1) does NOT restart (#4054 no-crash-loop preserved; ActiveState=$mx2_active)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} real systemd (#4862): crash exit(1) does NOT restart"
        echo "  NRestarts=$mx2_restarts ActiveState=$mx2_active"
        systemctl --user status "$MX_UNIT_CRASH" --no-pager -l 2>&1 | sed 's/^/    /'
    fi

    mx_cleanup
    # Restore the plain (non-MX) suite-wide traps for the remainder of the run.
    trap 'bg_proc_reap; [ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"' EXIT
    trap 'bg_proc_reap; [ -n "$WORKDIR" ] && pkill -f "$WORKDIR" >/dev/null 2>&1; rm -rf "$WORKDIR"; exit 1' INT TERM
else
    echo "  (skipping real-systemd #4862 regression: no reachable 'systemctl --user' manager on this host)"
fi

# ===================================================================
# Real launchd bootout+bootstrap path (#5081): settle/retry on the async
# EIO race ("Bootstrap failed: 5: Input/output error"), and a post-bootstrap
# check that the running job's REPORTED env actually matches the
# freshly-rendered plist. Only meaningful on Darwin (the launchd path is
# Darwin-gated). `LOOM_LAUNCHD_DOMAIN` is pinned so resolve_launchd_domain()
# never probes `launchctl print gui/<uid>` itself, and HOME is sandboxed so
# the rendered plist never lands in the operator's real
# ~/Library/LaunchAgents -- this suite must never touch real launchd state.
# ===================================================================
if [[ "$(uname -s)" == "Darwin" ]]; then

LD5081_LABEL="com.example.loom-sandbox-5081-$$"
LD5081_DOMAIN="user/$(id -u)"
LD5081_SERVICE="${LD5081_DOMAIN}/${LD5081_LABEL}"

# write_smart_launchd_bin <bin_dir> <bootstrap_log> <state_dir> <eio_failures> <mismatch:0|1>
#
# A launchctl stub that reflects a REAL bootstrap outcome instead of a
# hardcoded fake: `bootstrap` records the plist path it was handed (and, for
# the first <eio_failures> calls, fails with the EXACT text real launchd
# reports for the #5081 async race); `print` — for OUR service only — reports
# a REAL backgrounded process's pid (read from <state_dir>/real.pid, so the
# script's own `kill -0` liveness check succeeds) plus an
# "environment = { KEY => value ... }" block built from the LAST bootstrapped
# plist via plutil+jq, mirroring real `launchctl print` output byte-for-byte
# closely enough for extract_launchd_print_env to parse. <mismatch>=1
# corrupts the reported LOOM_SOCKET_PATH value so a verification-FAILURE path
# can be exercised. Any OTHER service (e.g. the watchdog's own label) reports
# "not loaded" (print exits 1); bootout/kickstart are unconditional no-ops
# for every service.
write_smart_launchd_bin() {
    local bin_dir="$1" bootstrap_log="$2" state_dir="$3" eio_failures="$4" mismatch="${5:-0}"
    mkdir -p "$bin_dir" "$state_dir"
    : > "$bootstrap_log"
    echo 0 > "$state_dir/bootstrap-attempts"
    rm -f "$state_dir/bootstrapped-plist-path"
    cat > "$bin_dir/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${bootstrap_log}"
service_arg="\${2:-}"
case "\${1:-}" in
  print)
    if [[ "\$service_arg" == "${LD5081_SERVICE}" && -f "${state_dir}/bootstrapped-plist-path" ]]; then
      plist_path="\$(cat "${state_dir}/bootstrapped-plist-path")"
      real_pid="\$(cat "${state_dir}/real.pid" 2>/dev/null)"
      echo "	pid = \${real_pid}"
      echo "	environment = {"
      if [[ "${mismatch}" == "1" ]]; then
        plutil -convert json -o - "\$plist_path" 2>/dev/null | jq -r '.EnvironmentVariables // {} | to_entries[] | "\t\t" + .key + " => " + (.value|tostring)' | awk -F' => ' -v OFS=' => ' '{ if (\$1 ~ /LOOM_SOCKET_PATH\$/) { \$2 = "WRONG-VALUE-INJECTED-BY-TEST" }; print }'
      else
        plutil -convert json -o - "\$plist_path" 2>/dev/null | jq -r '.EnvironmentVariables // {} | to_entries[] | "\t\t" + .key + " => " + (.value|tostring)'
      fi
      echo "	}"
      exit 0
    fi
    exit 1
    ;;
  bootout)
    rm -f "${state_dir}/bootstrapped-plist-path"
    exit 0
    ;;
  bootstrap)
    plist_path="\$3"
    attempts="\$(cat "${state_dir}/bootstrap-attempts" 2>/dev/null || echo 0)"
    attempts=\$((attempts + 1))
    echo "\$attempts" > "${state_dir}/bootstrap-attempts"
    if [[ "\$attempts" -le "${eio_failures}" ]]; then
      echo "Bootstrap failed: 5: Input/output error" >&2
      exit 5
    fi
    echo "\$plist_path" > "${state_dir}/bootstrapped-plist-path"
    exit 0
    ;;
  kickstart) exit 0 ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$bin_dir/launchctl"
}

LD5081_FAKE_BIN="$WORKDIR/fake-loom-daemon-5081"
cat > "$LD5081_FAKE_BIN" <<'EOF'
#!/usr/bin/env bash
sleep 60
EOF
chmod +x "$LD5081_FAKE_BIN"

# T1. Clean bootstrap (no EIO race) -> exits 0, env verification passes
#     against the real, freshly-rendered plist.
LD1_HOME="$WORKDIR/ld1-home"
LD1_BIN="$WORKDIR/ld1-launchctl-bin"
LD1_STATE="$WORKDIR/ld1-state"
mkdir -p "$LD1_HOME"
write_smart_launchd_bin "$LD1_BIN" "$WORKDIR/ld1.log" "$LD1_STATE" 0 0
sleep 60 >/dev/null 2>&1 &
LD1_REAL_PID=$!
bg_proc_track "$LD1_REAL_PID"
echo "$LD1_REAL_PID" > "$LD1_STATE/real.pid"
out1=$( PATH="$LD1_BIN:$PATH" HOME="$LD1_HOME" LOOM_MACHINE_CHECKOUT="$LD1_HOME" LOOM_DAEMON_BIN="$LD5081_FAKE_BIN" \
    LOOM_LAUNCHD_LABEL="$LD5081_LABEL" LOOM_LAUNCHD_DOMAIN="$LD5081_DOMAIN" \
    LOOM_SOCKET_PATH="$LD1_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$LD1_HOME/.loom/autonomy-desired" \
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-5081-wd-$$" \
    bash "$START_SCRIPT" 2>&1 )
rc1=$?
kill "$LD1_REAL_PID" 2>/dev/null || true
assert_eq "0" "$rc1" "launchd path (#5081): clean bootstrap + matching env -> exit 0"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$rc1" -eq 0 ]] && ! echo "$out1" | grep -qi 'does NOT match'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} launchd path (#5081): env verification passes against the real rendered plist"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} launchd path (#5081): env verification passes against the real rendered plist"
    echo "  output: $out1"
fi

# T2. Bootstrap fails with the EIO race exactly once, then succeeds on retry
#     -> exit 0, and the output names the retry.
LD2_HOME="$WORKDIR/ld2-home"
LD2_BIN="$WORKDIR/ld2-launchctl-bin"
LD2_STATE="$WORKDIR/ld2-state"
mkdir -p "$LD2_HOME"
write_smart_launchd_bin "$LD2_BIN" "$WORKDIR/ld2.log" "$LD2_STATE" 1 0
sleep 60 >/dev/null 2>&1 &
LD2_REAL_PID=$!
bg_proc_track "$LD2_REAL_PID"
echo "$LD2_REAL_PID" > "$LD2_STATE/real.pid"
out2=$( PATH="$LD2_BIN:$PATH" HOME="$LD2_HOME" LOOM_MACHINE_CHECKOUT="$LD2_HOME" LOOM_DAEMON_BIN="$LD5081_FAKE_BIN" \
    LOOM_LAUNCHD_LABEL="$LD5081_LABEL" LOOM_LAUNCHD_DOMAIN="$LD5081_DOMAIN" \
    LOOM_SOCKET_PATH="$LD2_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$LD2_HOME/.loom/autonomy-desired" \
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-5081-wd-$$" \
    LOOM_DAEMON_BOOTSTRAP_RETRY_SECS=0 \
    bash "$START_SCRIPT" 2>&1 )
rc2=$?
kill "$LD2_REAL_PID" 2>/dev/null || true
assert_eq "0" "$rc2" "launchd path (#5081): one EIO bootstrap failure, retry succeeds -> exit 0"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out2" | grep -qi 'async-bootout race' && echo "$out2" | grep -q 'attempt 1/'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} launchd path (#5081): EIO retry is logged"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} launchd path (#5081): EIO retry is logged"
    echo "  output: $out2"
fi

# T3. Bootstrap keeps failing with the EIO race past the retry ceiling ->
#     exit 1, loudly, and NEVER a false "started" success.
LD3_HOME="$WORKDIR/ld3-home"
LD3_BIN="$WORKDIR/ld3-launchctl-bin"
LD3_STATE="$WORKDIR/ld3-state"
mkdir -p "$LD3_HOME"
write_smart_launchd_bin "$LD3_BIN" "$WORKDIR/ld3.log" "$LD3_STATE" 99 0
out3=$( PATH="$LD3_BIN:$PATH" HOME="$LD3_HOME" LOOM_MACHINE_CHECKOUT="$LD3_HOME" LOOM_DAEMON_BIN="$LD5081_FAKE_BIN" \
    LOOM_LAUNCHD_LABEL="$LD5081_LABEL" LOOM_LAUNCHD_DOMAIN="$LD5081_DOMAIN" \
    LOOM_SOCKET_PATH="$LD3_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$LD3_HOME/.loom/autonomy-desired" \
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-5081-wd-$$" \
    LOOM_DAEMON_BOOTSTRAP_RETRY_ATTEMPTS=2 LOOM_DAEMON_BOOTSTRAP_RETRY_SECS=0 \
    bash "$START_SCRIPT" 2>&1 )
rc3=$?
assert_eq "1" "$rc3" "launchd path (#5081): EIO race persists past the retry ceiling -> exit 1"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out3" | grep -qi 'bootstrap failed' && ! echo "$out3" | grep -qi 'started under launchd'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} launchd path (#5081): exhausted EIO retries never report a false success"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} launchd path (#5081): exhausted EIO retries never report a false success"
    echo "  output: $out3"
fi

# T4. Bootstrap + kickstart succeed, pid is alive, but the running job's
#     REPORTED env does not match the freshly-rendered plist -> exit 1, named
#     as an env mismatch (#5081's "never silently return a wrong env" AC),
#     never a false "started" success either.
LD4_HOME="$WORKDIR/ld4-home"
LD4_BIN="$WORKDIR/ld4-launchctl-bin"
LD4_STATE="$WORKDIR/ld4-state"
mkdir -p "$LD4_HOME"
write_smart_launchd_bin "$LD4_BIN" "$WORKDIR/ld4.log" "$LD4_STATE" 0 1
sleep 60 >/dev/null 2>&1 &
LD4_REAL_PID=$!
bg_proc_track "$LD4_REAL_PID"
echo "$LD4_REAL_PID" > "$LD4_STATE/real.pid"
out4=$( PATH="$LD4_BIN:$PATH" HOME="$LD4_HOME" LOOM_MACHINE_CHECKOUT="$LD4_HOME" LOOM_DAEMON_BIN="$LD5081_FAKE_BIN" \
    LOOM_LAUNCHD_LABEL="$LD5081_LABEL" LOOM_LAUNCHD_DOMAIN="$LD5081_DOMAIN" \
    LOOM_SOCKET_PATH="$LD4_HOME/.loom/loom-daemon.sock" \
    LOOM_AUTONOMY_MARKER="$LD4_HOME/.loom/autonomy-desired" \
    LOOM_WATCHDOG_LABEL="com.example.loom-sandbox-5081-wd-$$" \
    bash "$START_SCRIPT" 2>&1 )
rc4=$?
kill "$LD4_REAL_PID" 2>/dev/null || true
assert_eq "1" "$rc4" "launchd path (#5081): env mismatch after a successful bootstrap -> exit 1"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out4" | grep -qi 'does NOT match' && echo "$out4" | grep -qi 'LOOM_SOCKET_PATH' && ! echo "$out4" | grep -qi 'started under launchd'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} launchd path (#5081): env mismatch is named and never reported as a false success"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} launchd path (#5081): env mismatch is named and never reported as a false success"
    echo "  output: $out4"
fi

else
    echo "  (skipping real launchd bootout/bootstrap #5081 regression: not running on Darwin)"
fi

# ============================================================
# Live daemon state guard (#5179, adopted here per #5191): every live `.loom`
# state path reachable from the ambient environment (the real $HOME/.loom, the
# live checkout's .loom, an ambient LOOM_PID_FILE / LOOM_WORKSPACE /
# LOOM_MACHINE_CHECKOUT) must be byte-and-mtime identical to its pre-suite
# snapshot -- and a path that was ABSENT must still be absent. This converts
# "state file leaked into production" from "discovered by an operator on a
# degraded host" into "caught by the suite".
# ============================================================
TESTS_RUN=$((TESTS_RUN + 1))
if live_state_sandbox_assert_untouched; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no live .loom daemon state path was written during the suite ($(live_state_sandbox_snapshot_size) paths guarded, #5191)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} a LIVE .loom daemon state path was written during this test run (#5191 regression!)"
    echo "  sandbox in effect during the run:"
    live_state_sandbox_describe | sed 's/^/    /'
fi

# ---------- summary ----------
echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
