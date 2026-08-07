#!/usr/bin/env bash
# test-loom-daemon-stop.sh — Tests for loom-daemon-stop.sh, including the
# macOS launchd bootout counterpart added by #3972.
#
# Every invocation below pins LOOM_LAUNCHD_LABEL to a random, guaranteed-
# nonexistent label. This is deliberate and load-bearing: it ensures
# `launchd_job_loaded` always resolves false (no loaded job with that label)
# so these tests can NEVER observe or mutate the real machine's
# ~/Library/LaunchAgents/com.rjwalters.loom-daemon.plist, on this dev box or
# any CI runner.
#
# Style matches test-loom-daemon-start.sh — plain bash, hand-rolled
# assertions. Bats is NOT used in this repository.
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-stop.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOP_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-stop.sh"

# Shared launchd sandbox (#4078): scratch-label generator, decoy spawner, and
# stub launchctl/pgrep. Stubs the syscall layer only — never the stop script.
# shellcheck source=lib/launchd-sandbox.sh
source "$SCRIPT_DIR/lib/launchd-sandbox.sh"

# Background-PID bookkeeping (#4773): every `sleep 30 &` decoy this suite
# backgrounds as a stand-in "live daemon" for STOP_SCRIPT to kill is tracked
# here too — each test already falls back to an inline `kill -9` when the
# stop-under-test assertion fails, but that fallback (like the DECOY_PID
# tracking above) never runs if the suite itself is interrupted first.
# shellcheck source=lib/bg-proc-trap.sh
source "$SCRIPT_DIR/lib/bg-proc-trap.sh"

# Shared live-state sandbox (#5179, adopted here per #5191). This suite is the
# HIGHEST-risk of the three lifecycle suites for this class of leak: it both
# `rm -f`s and `kill`s the pid it reads, and several call sites below (tests
# 1/2/3/6/7/8) pin ONLY LOOM_LAUNCHD_LABEL, not LOOM_SOCKET_PATH /
# LOOM_AUTONOMY_MARKER / LOOM_MACHINE_CHECKOUT -- so pre-fix, an ambient
# LOOM_MACHINE_CHECKOUT (as a Loom agent session exports) would have flipped
# loom-daemon-stop.sh's DAEMON_STATE_HOME to the REAL $HOME/.loom, and the
# script would `kill` whatever pid is recorded in the REAL production
# .daemon.pid. The snapshot MUST run here -- before live_state_sandbox_init
# below rewrites the LOOM_* state vars, and before any sub-invocation can
# write/kill anything -- because it discovers WHICH paths are the live ones by
# reading the ambient environment. The matching live_state_sandbox_assert_untouched
# runs as the suite's final guard.
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

# A guaranteed-nonexistent label so `launchctl print` never matches a real
# loaded job on the host machine, regardless of platform.
FAKE_LABEL="$(launchd_sandbox_new_label)"

# Scratch systemd unit (#4268), the systemd analog of FAKE_LABEL: exported so
# EVERY stop invocation below (not just the new systemd-tier cases) resolves a
# guaranteed-nonexistent unit. On a Darwin runner `systemctl` is absent so the
# systemd tier is never taken; on a real systemd Linux host this ensures the
# existing cases probe a scratch unit (is-active/is-enabled false → fall through
# to the pid tier) and can NEVER disable the operator's real loom-daemon.service.
export LOOM_SYSTEMD_UNIT="loom-daemon-test-$$.service"

WORKDIR="$(mktemp -d)"

# ---------- live daemon state sandbox (#5179, adopted here per #5191) ----------
# ONE helper owns every live-state path this suite could otherwise reach (see
# lib/live-state-sandbox.sh for the full per-variable rationale). This is the
# suite-wide FLOOR, not a replacement for every case: tests below that need a
# specific fixture's OWN scratch HOME still pin LOOM_SOCKET_PATH /
# LOOM_AUTONOMY_MARKER inline on their own invocation (a per-command assignment
# always wins over this exported default, same precedence as before). This
# only closes the call sites that do NOT pin them.
live_state_sandbox_init "$WORKDIR/live-state"

# Suite-level safety guard (#4078): a decoy process whose argv ends in
# `/loom-daemon` — exactly what the stop script's label-blind `pgrep -f
# '(^|/)loom-daemon$'` fallback would match. Every stop invocation below pins a
# scratch LOOM_LAUNCHD_LABEL, so the production narrowing must skip the pgrep
# tier and leave this decoy alive. If ANY test in this suite regresses into a
# by-name kill, this decoy dies and the final assertion fails loudly.
DECOY_PID="$(launchd_sandbox_spawn_decoy "$WORKDIR")"
bg_proc_track "$DECOY_PID"
# bg_proc_reap kills every `sleep 30 &` decoy tracked via bg_proc_track below;
# EXIT/INT/TERM (not just EXIT, #4773) so a hard interruption of this suite
# still reaps them, not only the individual tests' own inline `kill` calls.
# NOTE: a bare `trap CMD EXIT INT TERM` runs CMD on INT/TERM but does NOT stop
# the script (bash only auto-exits after an EXIT-trap firing, not an INT/TERM
# one) -- without the explicit `exit` below, a SIGTERM'd suite would clean up
# once and then keep executing every remaining test, re-populating $WORKDIR.
trap 'bg_proc_reap; rm -rf "$WORKDIR"' EXIT
trap 'bg_proc_reap; rm -rf "$WORKDIR"; exit 1' INT TERM
mkdir -p "$WORKDIR/.loom"

# ---------- tests ----------

# 1. No PID file, no running process -> "nothing to stop", exits 0.
out=$( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash "$STOP_SCRIPT" 2>&1 )
rc=$?
assert_eq "0" "$rc" "no daemon running: exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out" | grep -qi "nothing to stop"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no daemon running: reports 'nothing to stop'"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} no daemon running: reports 'nothing to stop'"
fi

# 2. A live PID-file-tracked process is stopped (SIGTERM path).
SLEEP_PID_FILE="$WORKDIR/.loom/.daemon.pid"
( sleep 30 & echo $! > "$SLEEP_PID_FILE" )
sleep_pid=$(cat "$SLEEP_PID_FILE")
bg_proc_track "$sleep_pid"
( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_DAEMON_STOP_GRACE_SECS=2 bash "$STOP_SCRIPT" >/dev/null 2>&1 )
rc2=$?
assert_eq "0" "$rc2" "live pid: stop exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if ! kill -0 "$sleep_pid" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} live pid: process is actually killed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} live pid: process is actually killed"
    kill -9 "$sleep_pid" 2>/dev/null || true
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$SLEEP_PID_FILE" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} live pid: PID file removed after stop"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} live pid: PID file removed after stop"
fi

# 3. --force skips the grace window (SIGKILL immediately).
( sleep 30 & echo $! > "$SLEEP_PID_FILE" )
sleep_pid2=$(cat "$SLEEP_PID_FILE")
bg_proc_track "$sleep_pid2"
start_ts=$(date +%s)
( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash "$STOP_SCRIPT" --force >/dev/null 2>&1 )
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$elapsed" -le 5 ]] && ! kill -0 "$sleep_pid2" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --force kills immediately without waiting the grace window"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --force kills immediately without waiting the grace window (elapsed=${elapsed}s)"
    kill -9 "$sleep_pid2" 2>/dev/null || true
fi

# 4. --help documents the launchd bootout counterpart and LOOM_LAUNCHD_LABEL.
help_out=$(bash "$STOP_SCRIPT" --help 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help_out" | grep -qi 'launchd' && echo "$help_out" | grep -q 'LOOM_LAUNCHD_LABEL'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents the launchd bootout counterpart"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents the launchd bootout counterpart"
fi

# 5. The bootout path is unchanged (#4054 must not remove it — it stays as
#    belt-and-braces alongside the new exit-code-carries-intent contract).
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'launchctl bootout' "$STOP_SCRIPT" && grep -q 'launchd_bootout_if_loaded' "$STOP_SCRIPT"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} bootout path unchanged (launchd_bootout_if_loaded + launchctl bootout still present)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} bootout path unchanged (launchd_bootout_if_loaded + launchctl bootout still present)"
fi

# 6. Stop must NOT report success while a daemon is still alive (#4054): if the
#    launchd job for the label remains loaded with a live pid after the stop (a
#    bootout that did not stick — the inverted-#4011 silent-success hole), the
#    script exits non-zero. Darwin-only: `launchd_job_loaded` short-circuits on
#    non-Darwin, so the relaunch-detection branch cannot run there.
if [[ "$(uname -s)" == "Darwin" ]]; then
    FAKE_BIN_DIR="$WORKDIR/fakebin"
    mkdir -p "$FAKE_BIN_DIR"
    # Fake launchctl: reports the job as loaded and names the "relaunched"
    # daemon's pid, and treats bootout as a no-op (simulating a bootout that
    # failed to stop the relaunched instance).
    cat > "$FAKE_BIN_DIR/launchctl" <<'FAKE'
#!/usr/bin/env bash
case "$1" in
  print)   printf '\tpid = %s\n' "${RELAUNCH_PID:-0}"; exit 0 ;;
  bootout) exit 0 ;;
  *)       exit 0 ;;
esac
FAKE
    chmod +x "$FAKE_BIN_DIR/launchctl"

    # Original daemon (killed by the stop) + a separate live "relaunched" daemon.
    ( sleep 30 & echo $! > "$SLEEP_PID_FILE" )
    orig_pid=$(cat "$SLEEP_PID_FILE")
    bg_proc_track "$orig_pid"
    sleep 30 &
    relaunch_pid=$!
    bg_proc_track "$relaunch_pid"

    stuck_out=$( cd "$WORKDIR" && PATH="$FAKE_BIN_DIR:$PATH" RELAUNCH_PID="$relaunch_pid" \
        LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_DAEMON_STOP_GRACE_SECS=2 bash "$STOP_SCRIPT" 2>&1 )
    stuck_rc=$?

    assert_eq "1" "$stuck_rc" "relaunched-daemon-still-alive: stop exits non-zero (does not report success)"
    TESTS_RUN=$((TESTS_RUN + 1))
    if echo "$stuck_out" | grep -qi 'still alive'; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} relaunched-daemon-still-alive: reports the live daemon instead of success"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} relaunched-daemon-still-alive: reports the live daemon instead of success"
    fi

    kill -9 "$orig_pid" "$relaunch_pid" 2>/dev/null || true
else
    echo "  (skipping relaunch-detection test — not Darwin)"
fi

# 7. Decoy-process test (#4078): the label-blind `pgrep` fallback must NOT be
#    reachable when the caller scoped the stop to a non-default label. Run the
#    REAL stop script (real pgrep on PATH, no PID file, scratch label) with a
#    live decoy whose argv ends in `/loom-daemon`. Pre-fix, stop would `pgrep
#    -f '(^|/)loom-daemon$'`, match the decoy, and SIGTERM it. Post-fix, the
#    scratch label routes around the pgrep tier entirely, so the decoy survives
#    and stop reports "nothing to stop". This is the test that distinguishes a
#    real fix from an insufficient label-only-on-launchctl one.
decoy7_pid="$(launchd_sandbox_spawn_decoy "$WORKDIR/decoy7")"
sleep 0.2
# Sanity: the decoy really is matchable by the fallback pattern (else the test
# would pass vacuously). Only meaningful where pgrep exists.
if command -v pgrep >/dev/null 2>&1; then
    TESTS_RUN=$((TESTS_RUN + 1))
    if pgrep -f '(^|/)loom-daemon$' 2>/dev/null | grep -qx "$decoy7_pid"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} decoy: is matchable by the pgrep fallback pattern (test is not vacuous)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} decoy: is matchable by the pgrep fallback pattern (test is not vacuous)"
    fi
fi
decoy_out=$( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash "$STOP_SCRIPT" 2>&1 )
decoy_rc=$?
assert_eq "0" "$decoy_rc" "decoy: scratch-label stop with no PID file exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if launchd_sandbox_decoy_alive "$decoy7_pid"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} decoy: survives a scratch-label stop (label-blind pgrep tier not taken)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} decoy: survives a scratch-label stop (label-blind pgrep tier not taken)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$decoy_out" | grep -qi "nothing to stop"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} decoy: reports 'nothing to stop' (does not adopt an unrelated loom-daemon)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} decoy: reports 'nothing to stop' (does not adopt an unrelated loom-daemon)"
fi
kill "$decoy7_pid" 2>/dev/null || true

# 8. Symmetry with loom-daemon-start.sh (#4078): LOOM_DAEMON_LAUNCHD=0 must
#    disable ALL launchd interaction on the stop side too, so no `launchctl` is
#    ever invoked. Uses the sandbox stub launchctl (records every call); the
#    recorded log must stay empty. Darwin-only: on non-Darwin launchd is off
#    regardless, so there is nothing to prove.
if [[ "$(uname -s)" == "Darwin" ]]; then
    SYM_BIN="$WORKDIR/sym-bin"
    SYM_LOG="$WORKDIR/sym-log"
    launchd_sandbox_install_stubs "$SYM_BIN" "$SYM_LOG"
    ( cd "$WORKDIR" && PATH="$SYM_BIN:$PATH" LOOM_DAEMON_LAUNCHD=0 \
        LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash "$STOP_SCRIPT" >/dev/null 2>&1 )
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ ! -s "$SYM_LOG/launchctl-invocations.log" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} LOOM_DAEMON_LAUNCHD=0: stop performs no launchctl call at all"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} LOOM_DAEMON_LAUNCHD=0: stop performs no launchctl call at all"
        echo "  launchctl invocations: $(cat "$SYM_LOG/launchctl-invocations.log")"
    fi
    TESTS_RUN=$((TESTS_RUN + 1))
    if launchd_sandbox_assert_no_production_label "$SYM_LOG/launchctl-invocations.log"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} no launchctl invocation named a com.rjwalters.* label"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} no launchctl invocation named a com.rjwalters.* label"
    fi
else
    echo "  (skipping LOOM_DAEMON_LAUNCHD symmetry test — not Darwin)"
fi

# ---------- autonomy-desired marker lifecycle (#4011) ----------
# Every case pins LOOM_AUTONOMY_MARKER into WORKDIR so it can NEVER touch the
# operator's real ~/.loom/autonomy-desired.
MARKER="$WORKDIR/.loom/autonomy-desired"

# 4011-a. Operator stop on the "nothing to stop" path REMOVES the marker.
mkdir -p "$WORKDIR/.loom"
printf 'started_at=x\nlaunchd_label=%s\n' "$FAKE_LABEL" > "$MARKER"
( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_AUTONOMY_MARKER="$MARKER" \
    bash "$STOP_SCRIPT" >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$MARKER" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} operator stop removes the autonomy-desired marker (nothing-to-stop path)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} operator stop removes the autonomy-desired marker (nothing-to-stop path)"
fi

# 4011-b. --restarting PRESERVES the marker (the update.sh self-update path).
printf 'started_at=x\nlaunchd_label=%s\n' "$FAKE_LABEL" > "$MARKER"
( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_AUTONOMY_MARKER="$MARKER" \
    bash "$STOP_SCRIPT" --restarting >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$MARKER" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --restarting preserves the marker (self-update never disarms the detector)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --restarting preserves the marker (self-update never disarms the detector)"
fi

# 4011-c. LOOM_DAEMON_STOP_KEEP_INTENT=1 is the env equivalent of --restarting.
printf 'started_at=x\n' > "$MARKER"
( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_AUTONOMY_MARKER="$MARKER" \
    LOOM_DAEMON_STOP_KEEP_INTENT=1 bash "$STOP_SCRIPT" >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$MARKER" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_DAEMON_STOP_KEEP_INTENT=1 preserves the marker"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_DAEMON_STOP_KEEP_INTENT=1 preserves the marker"
fi

# 4011-d. Operator stop of a LIVE pid also removes the marker (SIGTERM path).
printf 'started_at=x\n' > "$MARKER"
( sleep 30 & echo $! > "$WORKDIR/.loom/.daemon.pid" )
live_pid=$(cat "$WORKDIR/.loom/.daemon.pid")
bg_proc_track "$live_pid"
( cd "$WORKDIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_AUTONOMY_MARKER="$MARKER" \
    LOOM_DAEMON_STOP_GRACE_SECS=2 bash "$STOP_SCRIPT" >/dev/null 2>&1 )
kill -9 "$live_pid" 2>/dev/null || true
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$MARKER" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} operator stop of a live daemon also clears the marker"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} operator stop of a live daemon also clears the marker"
fi

# 4011-e. --help documents the marker + --restarting.
help_out2=$(bash "$STOP_SCRIPT" --help 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help_out2" | grep -q 'restarting' && echo "$help_out2" | grep -qi 'autonomy-desired'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents --restarting and the autonomy-desired marker"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents --restarting and the autonomy-desired marker"
fi

# ---------- systemd --user ownership tier (#4268) ----------
# The Linux mirror of the launchd bootout tier. Detection uses the test-only
# LOOM_SYSTEMD_FORCE=1 seam plus a stub `systemctl` on PATH (mirroring the stub
# launchctl above). The stub models unit state via a `down` marker file so the
# post-disable is-active re-check flips to inactive.
SD_BIN="$WORKDIR/sd-bin"; mkdir -p "$SD_BIN"
SD_STATE="$WORKDIR/sd-state"; mkdir -p "$SD_STATE"
SD_LOG="$WORKDIR/sd-stop.log"
make_sd_stop_stub() {
    : > "$SD_LOG"
    rm -f "$SD_STATE/down"
    cat > "$SD_BIN/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$SD_LOG"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
DOWN="$SD_STATE/down"
case "\${1:-}" in
  is-active)  [[ -f "\$DOWN" ]] && exit 3; exit 0 ;;   # exit 3 = inactive
  is-enabled) [[ -f "\$DOWN" ]] && exit 1; exit 0 ;;
  disable)    touch "\$DOWN"; exit 0 ;;                 # disable --now => now inactive
  *) exit 0 ;;
esac
EOF
    chmod +x "$SD_BIN/systemctl"
}

# SD1. Forced systemd tier: an active unit is stopped + DISABLED (disable --now),
#      the stop exits 0, and the autonomy-desired marker is cleared.
make_sd_stop_stub
mkdir -p "$WORKDIR/.loom"
printf 'started_at=x\n' > "$WORKDIR/.loom/autonomy-desired"
( cd "$WORKDIR" && PATH="$SD_BIN:$PATH" LOOM_SYSTEMD_FORCE=1 \
    LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_AUTONOMY_MARKER="$WORKDIR/.loom/autonomy-desired" \
    bash "$STOP_SCRIPT" >/dev/null 2>&1 )
sd_rc=$?
assert_eq "0" "$sd_rc" "systemd tier: stop of an active unit exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- "--user disable --now $LOOM_SYSTEMD_UNIT" "$SD_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd tier: runs 'systemctl --user disable --now <unit>' (stop + disable so reboot cannot resurrect)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd tier: runs 'systemctl --user disable --now <unit>'"
    echo "  systemctl calls: $(cat "$SD_LOG")"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$WORKDIR/.loom/autonomy-desired" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd tier: operator stop clears the autonomy-desired marker"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd tier: operator stop clears the autonomy-desired marker"
fi
# SD1b (#4260 sub-issue D): the same operator stop also tears down the
# watchdog timer + service pair, symmetric with the launchd bootout tier.
# Naming mirrors loom-daemon-start.sh's resolve_systemd_watchdog_unit():
# <daemon unit>-watchdog(.timer|.service).
SD_WD_UNIT="${LOOM_SYSTEMD_UNIT%.service}-watchdog"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q -- "--user disable --now ${SD_WD_UNIT}.timer" "$SD_LOG" \
    && grep -q -- "--user disable --now ${SD_WD_UNIT}.service" "$SD_LOG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd tier: operator stop disables the watchdog timer + service"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd tier: operator stop disables the watchdog timer + service"
    echo "  systemctl calls: $(cat "$SD_LOG")"
fi

# SD2. Post-disable verification: if the unit is STILL active after disable --now
#      (a disable that did not stick — the inverted-#4011 silent-success hole),
#      the stop exits non-zero instead of reporting success.
: > "$SD_LOG"
cat > "$SD_BIN/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$SD_LOG"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  is-active)  exit 0 ;;   # ALWAYS active — disable did not stick
  is-enabled) exit 0 ;;
  disable)    exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$SD_BIN/systemctl"
stuck_out=$( cd "$WORKDIR" && PATH="$SD_BIN:$PATH" LOOM_SYSTEMD_FORCE=1 \
    LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash "$STOP_SCRIPT" 2>&1 )
stuck_rc=$?
assert_eq "1" "$stuck_rc" "systemd tier: unit still active after disable --now → stop exits non-zero"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$stuck_out" | grep -qi 'still active'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} systemd tier: reports the still-active unit instead of success"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} systemd tier: reports the still-active unit instead of success"
fi

# SD3. Symmetry (#4078 analog): LOOM_DAEMON_SYSTEMD=0 disables ALL systemd
#      interaction, so NO systemctl call is made even with the stub on PATH and
#      detection forced — the stop routes to the pid/nohup tier.
make_sd_stop_stub
( cd "$WORKDIR" && PATH="$SD_BIN:$PATH" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=0 \
    LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash "$STOP_SCRIPT" >/dev/null 2>&1 )
sym_rc=$?
assert_eq "0" "$sym_rc" "LOOM_DAEMON_SYSTEMD=0: stop exits 0 (pid/nohup tier)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -s "$SD_LOG" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} LOOM_DAEMON_SYSTEMD=0: stop performs no systemctl call at all (symmetric with --no-systemd)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} LOOM_DAEMON_SYSTEMD=0: stop performs no systemctl call at all"
    echo "  systemctl calls: $(cat "$SD_LOG")"
fi

# SD4. --help documents the systemd disable counterpart + the LOOM_DAEMON_SYSTEMD
#      escape hatch.
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help_out" | grep -qi 'systemd' && echo "$help_out" | grep -q 'LOOM_DAEMON_SYSTEMD'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents the systemd disable counterpart + LOOM_DAEMON_SYSTEMD"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents the systemd disable counterpart + LOOM_DAEMON_SYSTEMD"
fi

# ---------- machine mode (Epic #3835 Phase 3b, #4229) ----------
# LOOM_MACHINE_CHECKOUT makes the pid-file home resolve from $HOME/.loom
# instead of $PWD's repo -- and must work even from a directory with NO
# .loom/ at all (unlike the dev-mode fallback, which requires one). Every
# write below targets a SCRATCH $HOME, never the real operator ~/.loom.
MACHINE_HOME="$(mktemp -d)"
mkdir -p "$MACHINE_HOME/.loom"
MACHINE_CHECKOUT="$(mktemp -d)"
NON_REPO_DIR="$(mktemp -d)"

( sleep 30 & echo $! > "$MACHINE_HOME/.loom/.daemon.pid" )
machine_pid=$(cat "$MACHINE_HOME/.loom/.daemon.pid")
bg_proc_track "$machine_pid"
out_machine=$( cd "$NON_REPO_DIR" && HOME="$MACHINE_HOME" LOOM_MACHINE_CHECKOUT="$MACHINE_CHECKOUT" \
    LOOM_LAUNCHD_LABEL="$FAKE_LABEL" LOOM_DAEMON_STOP_GRACE_SECS=2 bash "$STOP_SCRIPT" 2>&1 )
rc_machine=$?
assert_eq "0" "$rc_machine" "machine mode: stop from a non-repo dir exits 0"
TESTS_RUN=$((TESTS_RUN + 1))
if ! echo "$out_machine" | grep -qi "Not in a Loom workspace"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: never hits the dev-mode 'Not in a Loom workspace' refusal"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: never hits the dev-mode 'Not in a Loom workspace' refusal"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if ! kill -0 "$machine_pid" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: stop resolves the pid file under \$HOME/.loom (not \$PWD) and kills it"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    kill -9 "$machine_pid" 2>/dev/null || true
    echo -e "${RED}✗${NC} machine mode: stop resolves the pid file under \$HOME/.loom (not \$PWD) and kills it"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -d "$NON_REPO_DIR/.loom" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} machine mode: stop does not require (or create) .loom/ at \$PWD"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} machine mode: stop does not require (or create) .loom/ at \$PWD"
fi

# Dev-mode fallback (scope guard): direct invocation with NO LOOM_MACHINE_CHECKOUT
# from a non-repo directory still refuses exactly as before #4229.
out_dev=$( cd "$NON_REPO_DIR" && LOOM_LAUNCHD_LABEL="$FAKE_LABEL" bash "$STOP_SCRIPT" 2>&1 )
rc_dev=$?
assert_eq "1" "$rc_dev" "dev-mode fallback unchanged: stop from a non-repo dir (no dispatcher) still exits 1"
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$out_dev" | grep -qi "Not in a Loom workspace"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} dev-mode fallback unchanged: reports 'Not in a Loom workspace'"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} dev-mode fallback unchanged: reports 'Not in a Loom workspace'"
fi
rm -rf "$MACHINE_HOME" "$MACHINE_CHECKOUT" "$NON_REPO_DIR"

# ---------- summary ----------
# Final suite-level decoy guard (#4078): nothing above should have killed the
# by-name-matchable decoy spawned at suite start.
TESTS_RUN=$((TESTS_RUN + 1))
if launchd_sandbox_decoy_alive "$DECOY_PID"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} suite-level decoy loom-daemon survived the whole stop suite"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} suite-level decoy loom-daemon survived the whole stop suite"
fi

# ============================================================
# ============================================================
# #5131: a label-scoped stop that finds NO daemon must not tear down the
# HOST-GLOBAL autonomy marker.
#
# The pid resolver's last-resort  tier is skipped for a non-default
# LOOM_LAUNCHD_LABEL (#4078), so such an invocation reaches the "nothing to
# stop" path whenever its own per-workspace PID_FILE is absent. Before this
# fix it still ran teardown_autonomy_intent, deleting
# $LOOM_DIR/autonomy-desired and booting the watchdog for the WHOLE host --
# then exited 0, so it read as a correct no-op.
#
# Scoping rule under test: tear down only when this stop owns that state --
# the default label, or an explicit LOOM_AUTONOMY_MARKER (what
# live_state_sandbox_init sets, which is why the suites were never bitten).
# ============================================================
scope_dir="$(mktemp -d)"
mkdir -p "$scope_dir/loomdir"

# (a) non-default label, marker NOT scoped -> marker must survive
: > "$scope_dir/loomdir/autonomy-desired"
( LOOM_SOCKET_PATH="$scope_dir/loomdir/loom-daemon.sock"   LOOM_PID_FILE="$scope_dir/absent.pid"   LOOM_LAUNCHD_LABEL="com.example.scratch-5131"   bash "$STOP_SCRIPT" ) >/dev/null 2>&1
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -f "$scope_dir/loomdir/autonomy-desired" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} #5131: label-scoped stop with nothing to stop preserves the host-global marker"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} #5131: label-scoped stop with nothing to stop preserves the host-global marker"
fi

# (b) non-default label WITH an explicit marker -> teardown still happens
#     (the sandboxed-suite path must keep working)
: > "$scope_dir/loomdir/autonomy-desired"
( LOOM_SOCKET_PATH="$scope_dir/loomdir/loom-daemon.sock"   LOOM_PID_FILE="$scope_dir/absent.pid"   LOOM_LAUNCHD_LABEL="com.example.scratch-5131"   LOOM_AUTONOMY_MARKER="$scope_dir/loomdir/autonomy-desired"   bash "$STOP_SCRIPT" ) >/dev/null 2>&1
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$scope_dir/loomdir/autonomy-desired" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} #5131: an explicitly-scoped marker is still torn down (sandboxed suites unaffected)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} #5131: an explicitly-scoped marker is still torn down (sandboxed suites unaffected)"
fi
rm -rf "$scope_dir"

# ============================================================
# ============================================================
# #5501: LOOM_DAEMON_STOP_DRYRUN — a supported way to exercise default-label
# semantics without ever touching a real supervised job.
#
# Reproduces the incident shape: LOOM_LAUNCHD_LABEL pointed at the REAL
# production label, no PID file (as if LOOM_PID_FILE were sandboxed/empty —
# the harness's actual mistake), so the target pid is resolved via the
# launchd-label fallback, exactly how a "prove default-label behaviour is
# unchanged" test reached the operator's real daemon. A fake `launchctl`
# stub simulates "the production job is loaded" with a decoy's pid, so this
# reproduction is safe regardless of DRYRUN — nothing here ever calls the
# REAL launchctl. Darwin-only: launchd_job_loaded short-circuits on
# non-Darwin, so the label-fallback path used here cannot be exercised there.
# ============================================================
if [[ "$(uname -s)" == "Darwin" ]]; then
    DR_BIN="$WORKDIR/dryrun-bin"; mkdir -p "$DR_BIN"
    DR_LOG="$WORKDIR/dryrun-launchctl.log"
    make_dr_launchctl() {
        local pid="$1"
        : > "$DR_LOG"
        cat > "$DR_BIN/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$DR_LOG"
case "\$1" in
  print)   printf '\tpid = %s\n' "$pid"; exit 0 ;;
  bootout) exit 0 ;;
  *)       exit 0 ;;
esac
EOF
        chmod +x "$DR_BIN/launchctl"
    }
    rm -f "$SLEEP_PID_FILE"

    # DR1. WITHOUT the seam: the real-labeled job's resolved pid IS killed —
    #      proves this fixture genuinely reproduces the incident (not vacuous).
    sleep 30 &
    dr_decoy_pid="$!"
    bg_proc_track "$dr_decoy_pid"
    make_dr_launchctl "$dr_decoy_pid"
    ( cd "$WORKDIR" && PATH="$DR_BIN:$PATH" LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon" \
        LOOM_DAEMON_STOP_GRACE_SECS=2 bash "$STOP_SCRIPT" >/dev/null 2>&1 )
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! kill -0 "$dr_decoy_pid" 2>/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} #5501 repro: without the dry-run seam, a real-labeled resolved pid IS stopped (fixture is not vacuous)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} #5501 repro: without the dry-run seam, a real-labeled resolved pid IS stopped (fixture is not vacuous)"
        kill -9 "$dr_decoy_pid" 2>/dev/null || true
    fi

    # DR2. WITH LOOM_DAEMON_STOP_DRYRUN=1: the SAME shape never sends a real
    #      signal and never issues a real launchctl bootout -- only logs what
    #      it would have done.
    sleep 30 &
    dr_decoy_pid2="$!"
    bg_proc_track "$dr_decoy_pid2"
    make_dr_launchctl "$dr_decoy_pid2"
    DR_ACTIONS="$WORKDIR/dryrun-actions.log"
    dr_out=$( cd "$WORKDIR" && PATH="$DR_BIN:$PATH" LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon" \
        LOOM_DAEMON_STOP_DRYRUN=1 LOOM_DAEMON_STOP_DRYRUN_LOG="$DR_ACTIONS" \
        LOOM_DAEMON_STOP_GRACE_SECS=2 bash "$STOP_SCRIPT" 2>&1 )
    dr_rc=$?
    assert_eq "0" "$dr_rc" "#5501 dry-run: stop exits 0"
    TESTS_RUN=$((TESTS_RUN + 1))
    if kill -0 "$dr_decoy_pid2" 2>/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} #5501 dry-run: the real-labeled resolved pid SURVIVES (no real signal sent)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} #5501 dry-run: the real-labeled resolved pid SURVIVES (no real signal sent)"
    fi
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -q "would SIGTERM pid $dr_decoy_pid2" "$DR_ACTIONS" 2>/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} #5501 dry-run: the dry-run log records the SIGTERM that would have been sent"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} #5501 dry-run: the dry-run log records the SIGTERM that would have been sent"
        echo "  actions log: $(cat "$DR_ACTIONS" 2>/dev/null)"
        echo "  stop output: $dr_out"
    fi
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -q 'would launchctl bootout' "$DR_ACTIONS" 2>/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} #5501 dry-run: the dry-run log records the launchctl bootout that would have been issued"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} #5501 dry-run: the dry-run log records the launchctl bootout that would have been issued"
    fi
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! grep -q '^bootout' "$DR_LOG" 2>/dev/null; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} #5501 dry-run: no REAL launchctl bootout invocation was recorded"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} #5501 dry-run: no REAL launchctl bootout invocation was recorded"
        echo "  launchctl calls: $(cat "$DR_LOG")"
    fi
    kill -9 "$dr_decoy_pid2" 2>/dev/null || true

    # DR3. --help documents the seam.
    TESTS_RUN=$((TESTS_RUN + 1))
    if echo "$help_out" | grep -q 'LOOM_DAEMON_STOP_DRYRUN'; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} --help documents LOOM_DAEMON_STOP_DRYRUN"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} --help documents LOOM_DAEMON_STOP_DRYRUN"
    fi
else
    echo "  (skipping #5501 dry-run reproduction — not Darwin)"
fi

# DR4 (platform-independent): the supervisor-identity guard in
# lib/live-state-sandbox.sh flags this exact real-label combination outside
# dry-run, and is exempt from flagging it while the seam is active (#5501 AC2
# wiring between the two files).
TESTS_RUN=$((TESTS_RUN + 1))
if LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon" live_state_sandbox_assert_supervisor_scoped 2>/dev/null; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} #5501: the supervisor-identity guard flags the real label outside dry-run"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} #5501: the supervisor-identity guard flags the real label outside dry-run"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if LOOM_LAUNCHD_LABEL="com.rjwalters.loom-daemon" LOOM_DAEMON_STOP_DRYRUN=1 live_state_sandbox_assert_supervisor_scoped 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} #5501: LOOM_DAEMON_STOP_DRYRUN=1 is the supported bypass for the guard"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} #5501: LOOM_DAEMON_STOP_DRYRUN=1 is the supported bypass for the guard"
fi

# Live daemon state guard (#5179, adopted here per #5191): every live `.loom`
# state path reachable from the ambient environment (the real $HOME/.loom, the
# live checkout's .loom, an ambient LOOM_PID_FILE / LOOM_WORKSPACE /
# LOOM_MACHINE_CHECKOUT) must be byte-and-mtime identical to its pre-suite
# snapshot -- and a path that was ABSENT must still be absent. This converts
# "the real .daemon.pid got kill(2)'d" from "discovered by an operator on a
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

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
