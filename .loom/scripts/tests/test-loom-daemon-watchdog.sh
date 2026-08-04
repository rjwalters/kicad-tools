#!/usr/bin/env bash
# test-loom-daemon-watchdog.sh — Tests for loom-daemon-watchdog.sh, the host-side
# autonomy-loss detector (issue #4011).
#
# The watchdog compares operator INTENT (the autonomy-desired marker) against
# REALITY (daemon liveness + heartbeat freshness + — since #4398 — a bounded
# in-band IPC round-trip) and reports on divergence. This suite drives it
# entirely from files it creates in a tempdir — a real daemon is never started,
# and no invocation ever touches ~/.loom or the operator's real launchd jobs
# (liveness is exercised through the pid-file path with LOOM_DAEMON_LAUNCHD=0,
# plus one stubbed-launchctl case; the #4398 probe always runs against a stub
# binary pinned via LOOM_DAEMON_BIN, never the installed loom-daemon).
#
# Style matches the other daemon lifecycle tests — plain bash, hand-rolled
# assertions. Bats is NOT used in this repository.
#
# Exit-code contract under test:
#   0  no divergence (healthy, or no daemon expected)
#   1  DIVERGENCE reported (expected-but-not-running, wedged/stale heartbeat,
#      #4398: alive + heartbeat-fresh but the bounded IPC round-trip failed, or
#      #5118: the socket answers while the supervisor says its job is down)
#   3  #5118: liveness UNDETERMINED — no out-of-band signal AND no usable
#      in-band probe. Deliberately distinct from 1: "I cannot tell" is not
#      "the daemon is down", and defaulting to the latter is what made this
#      watchdog fire every 5 minutes fleet-wide against healthy daemons.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-watchdog.sh"

# Background-PID bookkeeping (#4773): every `sleeper` (line ~113) and the
# `sleep 60` kickstart decoy (below) is tracked here so the EXIT/INT/TERM trap
# can reap it even if this suite is interrupted before its own inline `kill`.
# shellcheck source=lib/bg-proc-trap.sh
source "$SCRIPT_DIR/lib/bg-proc-trap.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "${GREEN}✓${NC} $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "${RED}✗${NC} $1"; }

assert_rc() { # <expected> <actual> <msg>
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected rc=$1, got rc=$2)"; fi
}

WORKDIR="$(mktemp -d)"
# bg_proc_reap kills every sleeper()/decoy PID tracked via bg_proc_track;
# EXIT/INT/TERM (not just EXIT, #4773) so a hard interruption of this suite
# still reaps them, not only the individual tests' own inline `kill` calls.
# NOTE: a bare `trap CMD EXIT INT TERM` runs CMD on INT/TERM but does NOT stop
# the script (only an EXIT-trap firing auto-exits) -- the explicit `exit`
# below is required, else a SIGTERM'd suite would clean up once and then keep
# running every remaining test case.
trap 'bg_proc_reap; rm -rf "$WORKDIR"' EXIT
trap 'bg_proc_reap; rm -rf "$WORKDIR"; exit 1' INT TERM

MARKER="$WORKDIR/autonomy-desired"
HEARTBEAT="$WORKDIR/daemon.heartbeat"
WDLOG="$WORKDIR/watchdog.log"      # the watchdog's own report log (LOOM_WATCHDOG_LOG)
OUT="$WORKDIR/out.txt"             # captured stdout+stderr of each run (separate file)

# Write a marker describing a non-launchd (pid-file) daemon so liveness is probed
# via the pid file — deterministic and platform-independent.
write_marker() { # <pid_file> <heartbeat_interval_secs>
    cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
repo_root=$WORKDIR
pid_file=$1
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=$2
use_launchd=false
launchd_label=com.example.test
socket_path=$WORKDIR/loom-daemon.sock
EOF
}

# Portable mtime back-dating (macOS `date -v`, GNU `date -d`).
back_date_file() { # <file> <seconds_ago>
    local f="$1" secs="$2" ts
    ts=$(date -v-"${secs}"S '+%Y%m%d%H%M.%S' 2>/dev/null) \
        || ts=$(date -d "@$(( $(date +%s) - secs ))" '+%Y%m%d%H%M.%S' 2>/dev/null)
    [[ -n "$ts" ]] && touch -t "$ts" "$f" 2>/dev/null
}

# Run the watchdog on the pid-file path (LOOM_DAEMON_LAUNCHD=0). Extra env
# assignments may be passed as KEY=VAL positional args. Sets global RC.
#
# LOOM_SOCKET_PATH is pinned into the tempdir so the resolved <loom_dir> is
# $WORKDIR — this matters for the marker-ABSENT reality probe (#4331), whose
# default pid file is `<loom_dir>/.daemon.pid`: without this the probe would look
# at the operator's real ~/.loom/.daemon.pid and a live host daemon could break
# the "nothing alive" cases.
#
# LOOM_WATCHDOG_IPC_PROBE=0 is the harness DEFAULT (#4398): the pre-#4398 cases
# below assert the pid + heartbeat contract, and must not become dependent on
# whether the host happens to have a `loom-daemon` on PATH or a live socket. It
# is listed BEFORE "$@" so a caller can re-enable the probe by passing
# LOOM_WATCHDOG_IPC_PROBE=1 (later `env` assignments win). The dedicated probe
# cases in section 12+ do exactly that.
#
# #5118: LOOM_PID_FILE / LOOM_WORKSPACE / LOOM_MACHINE_CHECKOUT are pinned EMPTY
# (empty ⇒ "unset" in both the watchdog's resolve_pid_file and the daemon's
# daemon_pidfile.rs) so the pid-file path resolves from the MARKER, the tier
# these cases mean to exercise. Without this the suite is non-hermetic on any
# host whose daemon exports those into its agents' environment — a real
# observed failure: an inherited LOOM_PID_FILE pointed every case at the
# operator's live ~/GitHub/loom/.loom/.daemon.pid. Listed BEFORE "$@" so the
# dedicated LOOM_PID_FILE case can still set it.
run_watchdog() {
    : > "$OUT"
    env LOOM_WATCHDOG_IPC_PROBE=0 LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
        "$@" LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock" \
        LOOM_DAEMON_LAUNCHD=0 bash "$WATCHDOG" > "$OUT" 2>&1
    RC=$?
}

# As run_watchdog, but with --verbose so the OK/skip diagnostics (which the
# non-verbose report() deliberately suppresses on stderr but always writes to the
# log) are asserted from the same log the operator would read.
run_watchdog_verbose() {
    : > "$OUT"
    env LOOM_WATCHDOG_IPC_PROBE=0 LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
        "$@" LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock" \
        LOOM_DAEMON_LAUNCHD=0 bash "$WATCHDOG" --verbose > "$OUT" 2>&1
    RC=$?
}

# #5118: the supervisor-gate cases (8/9/10/10a/10b) drive a STUBBED
# launchctl/systemctl and assert on the OUT-OF-BAND branch alone, so they pin
# the in-band socket probe OFF and the socket into the tempdir. Without this
# they reach the operator's real ~/.loom/loom-daemon.sock, and a live host
# daemon answering there would (correctly, per #5118) reclassify "the
# supervisor says its job is down" as the unsupervised-daemon WARN — skipping
# the very remediation gate these cases exist to pin down.
SUPERVISOR_CASE_ENV=(LOOM_WATCHDOG_IPC_PROBE=0 LOOM_PID_FILE= LOOM_WORKSPACE=
                     LOOM_MACHINE_CHECKOUT= LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock")

log_has()  { grep -q "$1" "$WDLOG" 2>/dev/null; }
log_hasi() { grep -qi "$1" "$WDLOG" 2>/dev/null; }

# A live pid we own for the "daemon alive" cases. The sleeper's stdio is
# redirected to /dev/null so it does NOT hold open the `$(sleeper)` command
# substitution pipe (which would otherwise block the caller for the full sleep).
sleeper() { sleep 60 >/dev/null 2>&1 & echo $!; }

# A `ps` stub that always reports a fixed `-o etime=` value (used by the
# prior-boot heartbeat tests, #4368) — deterministic regardless of how long
# the real sleeper process has actually been alive by the time the watchdog
# runs. Prints the stub directory; add it to the FRONT of PATH via
# `run_watchdog PATH="$(make_ps_stub ...):$PATH"`.
make_ps_stub() { # <etime-value, e.g. "02:00:00">
    local dir
    dir="$(mktemp -d)"
    cat > "$dir/ps" <<EOF
#!/usr/bin/env bash
echo "$1"
EOF
    chmod +x "$dir/ps"
    echo "$dir"
}

# ---------------------------------------------------------------------------
# #4398 IPC-probe fixtures.
#
# The watchdog shells out to the resolved `loom-daemon` binary for its bounded
# in-band probe, so the probe is driven entirely by a stub binary pinned via
# LOOM_DAEMON_BIN — no real daemon, no real socket, no `timeout` dependency on
# the outcome. Each mode reproduces one shape the classifier must handle:
#
#   ok          successful IPC round-trip (exit 0)
#   slow-ok     eventually-responsive round-trip (the #4279 under-load case):
#               sleeps, then succeeds — must NOT be called a hang
#   hang        never returns at all (#4381's `while true; sleep 1` stub) —
#               only the watchdog's own hard budget can end it
#   unreachable the CLI's own bounded round-trip failed and it said so (exit 1)
#   usage       this build's CLI does not know the probe subcommand (clap, exit 2)
#   daemon-err  the daemon ANSWERED with an application error (exit 1) — proof
#               that IPC works, so it must degrade to "skipped", not "hang"
make_daemon_stub() { # <mode> [sleep_secs]
    local mode="$1" secs="${2:-2}" dir
    dir="$(mktemp -d)"
    case "$mode" in
        ok)
            printf '#!/usr/bin/env bash\necho "no active quarantines"\nexit 0\n' > "$dir/loom-daemon" ;;
        slow-ok)
            printf '#!/usr/bin/env bash\nsleep %s\necho "no active quarantines"\nexit 0\n' "$secs" > "$dir/loom-daemon" ;;
        hang)
            printf '#!/usr/bin/env bash\nwhile true; do sleep 1; done\n' > "$dir/loom-daemon" ;;
        unreachable)
            printf '#!/usr/bin/env bash\necho "Could not reach loom-daemon at /tmp/x.sock: round-trip timed out after 5s" >&2\nexit 1\n' > "$dir/loom-daemon" ;;
        usage)
            printf '#!/usr/bin/env bash\necho "error: unrecognized subcommand" >&2\nexit 2\n' > "$dir/loom-daemon" ;;
        daemon-err)
            printf '#!/usr/bin/env bash\necho "Daemon error: workspace not registered" >&2\nexit 1\n' > "$dir/loom-daemon" ;;
        *) echo "unknown stub mode $mode" >&2; return 1 ;;
    esac
    chmod +x "$dir/loom-daemon"
    echo "$dir"
}

PROBE_STATE="$WORKDIR/.watchdog-probe-fail-count"

# Stand up "daemon alive (past the startup grace) + heartbeat FRESH" — the exact
# state in which pid-alive and heartbeat-fresh both pass and only the in-band
# probe can tell a healthy daemon from a wedged one.
#
# Sets two GLOBALS (and must therefore never be called in a command
# substitution, whose subshell would discard them):
#   LIVE_PID      the live pid the caller must kill afterwards
#   PS_STUB_DIR   a `ps` stub pinning the process age to 7200s — well past any
#                 grace window — which the caller must put at the FRONT of PATH
#                 and `rm -rf` afterwards. Without it the freshly spawned
#                 sleeper's real age is ~0s and every probe would be skipped by
#                 the startup-grace guard.
PS_STUB_DIR=""
LIVE_PID=""
start_alive_and_fresh() { # <pid_file_suffix>
    PS_STUB_DIR="$(make_ps_stub "02:00:00")"
    LIVE_PID=$(sleeper)
    bg_proc_track "$LIVE_PID"
    echo "$LIVE_PID" > "$WORKDIR/pid$1"
    write_marker "$WORKDIR/pid$1" 60
    printf '%s pid=%s ts=now\n' "$(date +%s)" "$LIVE_PID" > "$HEARTBEAT"
    : > "$WDLOG"
    rm -f "$PROBE_STATE"
}

# ===================================================================
# 1. Marker ABSENT ⇒ no daemon expected ⇒ silent OK (exit 0).
# ===================================================================
rm -f "$MARKER" "$HEARTBEAT" "$WDLOG" "$WORKDIR/.daemon.pid"
run_watchdog
assert_rc 0 "$RC" "marker absent + nothing alive: exits 0 (no daemon expected)"
if log_has DIVERGENCE || log_hasi "mismatch"; then fail "marker absent + nothing alive: unexpected report"; else pass "marker absent + nothing alive: no DIVERGENCE/MISMATCH reported"; fi

# ===================================================================
# 1b. Marker ABSENT but a daemon IS running ⇒ STATE MISMATCH (#4331): crash
#     protection is disarmed, so the watchdog WARNs loudly + exits 1 instead of
#     the old silent `[OK] nothing to check`. Reproduces the observed bug — a
#     daemon rolled via the restart primitive / self-update, neither of which
#     re-writes the marker. Liveness is probed via the default pid file
#     (<loom_dir>/.daemon.pid) on the LOOM_DAEMON_LAUNCHD=0 path.
# ===================================================================
rm -f "$MARKER" "$HEARTBEAT" "$WDLOG"
live_pid=$(sleeper)
bg_proc_track "$live_pid"
echo "$live_pid" > "$WORKDIR/.daemon.pid"
run_watchdog
kill "$live_pid" 2>/dev/null || true
assert_rc 1 "$RC" "marker absent + daemon alive: exits 1 (state mismatch, crash protection disarmed)"
if log_hasi "mismatch"; then
    pass "marker absent + daemon alive: WARN reports the state mismatch"
else
    fail "marker absent + daemon alive: missing the state-mismatch WARN"
fi
if log_has DIVERGENCE; then
    fail "marker absent + daemon alive: should be a WARN, not a DIVERGENCE"
else
    pass "marker absent + daemon alive: reported as WARN, not DIVERGENCE"
fi
rm -f "$WORKDIR/.daemon.pid"

# ===================================================================
# 2. Intent present, daemon ALIVE, heartbeat FRESH ⇒ silent OK.
# ===================================================================
live_pid=$(sleeper)
bg_proc_track "$live_pid"
echo "$live_pid" > "$WORKDIR/pidA"
write_marker "$WORKDIR/pidA" 60
printf '%s pid=%s ts=now\n' "$(date +%s)" "$live_pid" > "$HEARTBEAT"
: > "$WDLOG"
run_watchdog
kill "$live_pid" 2>/dev/null || true
assert_rc 0 "$RC" "alive + fresh heartbeat: exits 0"

# ===================================================================
# 3. Intent present, daemon ALIVE, heartbeat STALE (but written THIS boot,
#    i.e. younger than the process itself) ⇒ DIVERGENCE (wedged).
#    A `ps` stub pins the process age to 7200s — comfortably older than the
#    3600s-old heartbeat — so this exercises the genuine current-boot-stale
#    verdict deterministically, never the #4368 prior-boot path (which a
#    freshly-spawned real sleeper pid would otherwise always hit, since its
#    real age is far younger than any meaningfully back-dated heartbeat).
# ===================================================================
PS_STUB3="$(make_ps_stub "02:00:00")"
live_pid=$(sleeper)
bg_proc_track "$live_pid"
echo "$live_pid" > "$WORKDIR/pidB"
write_marker "$WORKDIR/pidB" 60
printf 'old\n' > "$HEARTBEAT"
back_date_file "$HEARTBEAT" 3600   # 1h old, well past the 300s default threshold
: > "$WDLOG"
run_watchdog PATH="$PS_STUB3:$PATH"
kill "$live_pid" 2>/dev/null || true
assert_rc 1 "$RC" "alive + STALE heartbeat (current boot): exits 1 (divergence)"
if log_has STALE; then pass "alive + stale heartbeat: reports it as wedged/stale"; else fail "alive + stale heartbeat: no STALE report"; fi
rm -rf "$PS_STUB3"

# ===================================================================
# 3b. Intent present, daemon ALIVE, heartbeat OLDER than the process itself
#     ⇒ liveness-only OK, NEVER a DIVERGENCE/STALE report (#4368). A `ps`
#     stub pins the process age to 5s while the heartbeat is 3600s old —
#     exactly the shape of the reported incident (a migrated/leftover
#     heartbeat file from a previous boot outliving a fresh restart).
# ===================================================================
PS_STUB3B="$(make_ps_stub "00:00:05")"
live_pid=$(sleeper)
bg_proc_track "$live_pid"
echo "$live_pid" > "$WORKDIR/pidB2"
write_marker "$WORKDIR/pidB2" 60
printf 'old\n' > "$HEARTBEAT"
back_date_file "$HEARTBEAT" 3600
: > "$WDLOG"
run_watchdog PATH="$PS_STUB3B:$PATH"
kill "$live_pid" 2>/dev/null || true
assert_rc 0 "$RC" "alive + heartbeat older than process (prior boot): exits 0, not flagged as wedged"
if log_hasi "previous boot"; then
    pass "prior-boot heartbeat: reports it as a previous-boot file, not a wedge"
else
    fail "prior-boot heartbeat: missing the previous-boot report"
fi
if log_has DIVERGENCE; then
    fail "prior-boot heartbeat: must NOT be reported as a DIVERGENCE"
else
    pass "prior-boot heartbeat: not reported as a DIVERGENCE"
fi
rm -rf "$PS_STUB3B"

# ===================================================================
# 4. Intent present, daemon DEAD ⇒ DIVERGENCE (expected but not running).
#    This IS the #4011 outage, reproduced.
# ===================================================================
dead_pid=$(sleeper); bg_proc_track "$dead_pid"; kill "$dead_pid" 2>/dev/null; wait "$dead_pid" 2>/dev/null
echo "$dead_pid" > "$WORKDIR/pidC"
write_marker "$WORKDIR/pidC" 60
printf 'x\n' > "$HEARTBEAT"
: > "$WDLOG"
run_watchdog
assert_rc 1 "$RC" "daemon DEAD but expected: exits 1 (the #4011 outage)"
if log_has EXPECTED && log_hasi "not"; then
    pass "daemon dead: reports 'expected but not running'"
else
    fail "daemon dead: missing the expected-but-not-running report"
fi

# ===================================================================
# 5. Intent present, daemon ALIVE, NO heartbeat file ⇒ liveness-only OK.
#    (heartbeat disabled or not yet written — do NOT false-report.)
# ===================================================================
live_pid=$(sleeper)
bg_proc_track "$live_pid"
echo "$live_pid" > "$WORKDIR/pidD"
write_marker "$WORKDIR/pidD" 60
rm -f "$HEARTBEAT"
: > "$WDLOG"
run_watchdog
kill "$live_pid" 2>/dev/null || true
assert_rc 0 "$RC" "alive + no heartbeat file: exits 0 (liveness-only, no false page)"

# ===================================================================
# 6. Staleness threshold is a comfortable multiple of the cadence: a heartbeat
#    just under the default 300s threshold is NOT reported (no flapping).
# ===================================================================
live_pid=$(sleeper)
bg_proc_track "$live_pid"
echo "$live_pid" > "$WORKDIR/pidE"
write_marker "$WORKDIR/pidE" 60
printf 'x\n' > "$HEARTBEAT"
back_date_file "$HEARTBEAT" 120   # 2min old < 300s threshold ⇒ still fresh
: > "$WDLOG"
run_watchdog
kill "$live_pid" 2>/dev/null || true
assert_rc 0 "$RC" "heartbeat 120s old < 300s threshold: not flagged (no flapping)"

# ===================================================================
# 7. LOOM_DAEMON_HEARTBEAT_STALE_SECS override is honored. A `ps` stub pins
#    the process age well past the 10s heartbeat age so this exercises the
#    current-boot-stale verdict, not the #4368 prior-boot path.
# ===================================================================
PS_STUB7="$(make_ps_stub "00:01:00")"
live_pid=$(sleeper)
bg_proc_track "$live_pid"
echo "$live_pid" > "$WORKDIR/pidF"
write_marker "$WORKDIR/pidF" 60
printf 'x\n' > "$HEARTBEAT"
back_date_file "$HEARTBEAT" 10
: > "$WDLOG"
run_watchdog PATH="$PS_STUB7:$PATH" LOOM_DAEMON_HEARTBEAT_STALE_SECS=5
kill "$live_pid" 2>/dev/null || true
assert_rc 1 "$RC" "STALE_SECS override: 10s-old heartbeat with 5s threshold is flagged"
rm -rf "$PS_STUB7"

# ===================================================================
# 8. launchd path via a stubbed launchctl reporting the job is NOT loaded ⇒
#    DIVERGENCE (mirrors a booted-out daemon).
# ===================================================================
STUB_BIN="$WORKDIR/stubbin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/launchctl" <<'EOF'
#!/usr/bin/env bash
# Stub: `print` always reports the job is not loaded (exit 1); no real job touched.
case "${1:-}" in
  print) exit 1 ;;
  *)     exit 0 ;;
esac
EOF
chmod +x "$STUB_BIN/launchctl"
cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=true
launchd_label=com.example.loom-sandbox-$$
socket_path=$WORKDIR/loom-daemon.sock
EOF
: > "$WDLOG" "$OUT"
if [[ "$(uname -s)" == "Darwin" ]]; then
    env PATH="$STUB_BIN:$PATH" "${SUPERVISOR_CASE_ENV[@]}" \
        LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        bash "$WATCHDOG" > "$OUT" 2>&1
    assert_rc 1 "$?" "launchd path: booted-out job (stub print exit 1) ⇒ divergence"
else
    # On non-Darwin the watchdog forces the pid-file path. With no pid_file in
    # the marker AND the in-band probe pinned off, NOTHING can establish
    # liveness either way — since #5118 that is reported as UNDETERMINED
    # (exit 3), deliberately NOT as the "daemon is down" divergence a missing
    # pid file used to manufacture. Section 23 below asserts the same contract
    # platform-independently.
    env "${SUPERVISOR_CASE_ENV[@]}" LOOM_AUTONOMY_MARKER="$MARKER" \
        LOOM_WATCHDOG_LOG="$WDLOG" bash "$WATCHDOG" > "$OUT" 2>&1
    assert_rc 3 "$?" "non-Darwin: no pid file + no in-band probe ⇒ UNDETERMINED, not a false 'down'"
fi

# ===================================================================
# 9. Bounded auto-remediation (#4232): job LOADED + NOT running + last exit
#    status 0 is EXACTLY the signature a restart-primitive exit that launchd
#    failed to honor can produce — the watchdog auto-`launchctl kickstart`s
#    (PLAIN, never -k) and, once that relaunches the job, exits 0 with an OK
#    "remediation succeeded" report instead of a bare DIVERGENCE.
# ===================================================================
if [[ "$(uname -s)" == "Darwin" ]]; then
    STUB9="$WORKDIR/stub9"
    mkdir -p "$STUB9"
    STATE9="$WORKDIR/state9"
    : > "$STATE9"   # empty -> "not running" at check time
    sleep 60 >/dev/null 2>&1 &
    NEW_PID9=$!
    bg_proc_track "$NEW_PID9"
    LOG9="$WORKDIR/launchctl9.log"
    : > "$LOG9"
    cat > "$STUB9/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$LOG9"
case "\${1:-}" in
  print)
    pid="\$(cat "$STATE9" 2>/dev/null)"
    if [[ -n "\$pid" ]]; then
      echo "	state = running"
      echo "	pid = \$pid"
    else
      echo "	state = not running"
    fi
    echo "	last exit status = 0"
    exit 0
    ;;
  kickstart)
    echo "$NEW_PID9" > "$STATE9"
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$STUB9/launchctl"
    cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=true
launchd_label=com.example.loom-sandbox-remediate-$$
socket_path=$WORKDIR/loom-daemon.sock
EOF
    : > "$WDLOG" "$OUT"
    env PATH="$STUB9:$PATH" "${SUPERVISOR_CASE_ENV[@]}" \
        LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS=5 LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL=0.2 \
        bash "$WATCHDOG" > "$OUT" 2>&1
    rc9=$?
    kill "$NEW_PID9" 2>/dev/null || true
    assert_rc 0 "$rc9" "exit-0-and-down: auto-kickstart relaunches the job -> exits 0"
    if grep -qE '^kickstart ' "$LOG9"; then
        pass "exit-0-and-down: auto-remediation invokes 'launchctl kickstart'"
    else
        fail "exit-0-and-down: auto-remediation invokes 'launchctl kickstart' (log: $(cat "$LOG9" 2>/dev/null))"
    fi
    if grep -q -- '-k' "$LOG9"; then
        fail "exit-0-and-down: kickstart is invoked PLAIN, never with -k"
    else
        pass "exit-0-and-down: kickstart is invoked PLAIN, never with -k"
    fi
    if log_hasi 'remediat'; then
        pass "exit-0-and-down: watchdog log records the remediation"
    else
        fail "exit-0-and-down: watchdog log records the remediation"
    fi
else
    pass "exit-0-and-down auto-remediation test skipped (non-Darwin host)"
fi

# ===================================================================
# 10. Every OTHER divergence stays report-only: job LOADED + NOT running but
#     last exit status 143 (a SIGTERM'd operator stop) must NEVER trigger the
#     auto-kickstart — narrow-gate proof that this is not a crash-loop reviver.
# ===================================================================
if [[ "$(uname -s)" == "Darwin" ]]; then
    STUB10="$WORKDIR/stub10"
    mkdir -p "$STUB10"
    STATE10="$WORKDIR/state10"
    : > "$STATE10"
    LOG10="$WORKDIR/launchctl10.log"
    : > "$LOG10"
    cat > "$STUB10/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$LOG10"
case "\${1:-}" in
  print)
    pid="\$(cat "$STATE10" 2>/dev/null)"
    if [[ -n "\$pid" ]]; then
      echo "	state = running"
      echo "	pid = \$pid"
    else
      echo "	state = not running"
    fi
    echo "	last exit status = 143"
    exit 0
    ;;
  kickstart)
    # If this were ever wrongly invoked it WOULD bring the job "up" (proving a
    # false positive would be caught) — the assertion below is that it is
    # simply never called.
    echo "999999" > "$STATE10"
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$STUB10/launchctl"
    cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=true
launchd_label=com.example.loom-sandbox-noremediate-$$
socket_path=$WORKDIR/loom-daemon.sock
EOF
    : > "$WDLOG" "$OUT"
    env PATH="$STUB10:$PATH" "${SUPERVISOR_CASE_ENV[@]}" \
        LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        bash "$WATCHDOG" > "$OUT" 2>&1
    rc10=$?
    assert_rc 1 "$rc10" "exit-143-and-down: stays report-only (no auto-kickstart) -> exits 1"
    if grep -q 'kickstart' "$LOG10"; then
        fail "exit-143-and-down: kickstart is NEVER invoked (no crash-loop revival of an operator stop)"
    else
        pass "exit-143-and-down: kickstart is NEVER invoked (no crash-loop revival of an operator stop)"
    fi
else
    pass "exit-143-and-down report-only test skipped (non-Darwin host)"
fi

# ===================================================================
# 10a/10b. systemd mirror of tests 9/10 (#4862): a stubbed `systemctl` (not a
# real systemd --user manager — deterministic and portable to a Darwin
# runner, same technique as the launchctl stubs above) proves the auto-
# remediation gate fires for the EXACT #4862 signature (unit LOADED + NOT
# running + ExecMainCode=exited + ExecMainStatus=0) and stays report-only for
# a genuine crash (ExecMainStatus=1).
# ===================================================================
STUB10A="$WORKDIR/stub10a"
mkdir -p "$STUB10A"
STATE10A="$WORKDIR/state10a"   # empty -> "not running"; a pid -> "running"
LOG10A="$WORKDIR/systemctl10a.log"
: > "$STATE10A" "$LOG10A"
UNIT10A="loom-daemon-test-remediate-$$.service"
sleep 60 >/dev/null 2>&1 &
NEW_PID10A=$!
bg_proc_track "$NEW_PID10A"
cat > "$STUB10A/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$LOG10A"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show)
    case "\$*" in
      *"-p MainPID"*)         pid="\$(cat "$STATE10A" 2>/dev/null)"; echo "\${pid:-0}" ;;
      *"-p LoadState"*)       echo "loaded" ;;
      *"-p ExecMainCode"*)    echo "exited" ;;
      *"-p ExecMainStatus"*)  echo "0" ;;
      *)                      echo "" ;;
    esac
    ;;
  reset-failed) exit 0 ;;
  start)
    echo "$NEW_PID10A" > "$STATE10A"
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB10A/systemctl"
cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=false
use_systemd=true
systemd_unit=$UNIT10A
socket_path=$WORKDIR/loom-daemon.sock
EOF
: > "$WDLOG" "$OUT"
env PATH="$STUB10A:$PATH" "${SUPERVISOR_CASE_ENV[@]}" \
    LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
    LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS=5 LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL=0.2 \
    bash "$WATCHDOG" > "$OUT" 2>&1
rc10a=$?
kill "$NEW_PID10A" 2>/dev/null || true
assert_rc 0 "$rc10a" "systemd exit-0-and-down (#4862): auto-remediation relaunches the unit -> exits 0"
if grep -q -- '--user start ' "$LOG10A"; then
    pass "systemd exit-0-and-down: auto-remediation invokes 'systemctl --user start'"
else
    fail "systemd exit-0-and-down: auto-remediation invokes 'systemctl --user start' (log: $(cat "$LOG10A" 2>/dev/null))"
fi
if grep -q -- '--user reset-failed ' "$LOG10A"; then
    pass "systemd exit-0-and-down: auto-remediation clears the failed state first ('reset-failed')"
else
    fail "systemd exit-0-and-down: auto-remediation clears the failed state first"
fi
if log_hasi 'remediat'; then
    pass "systemd exit-0-and-down: watchdog log records the remediation"
else
    fail "systemd exit-0-and-down: watchdog log records the remediation"
fi

# 10b. ExecMainStatus=1 (crash) — the auto-remediation gate must NEVER fire.
STUB10B="$WORKDIR/stub10b"
mkdir -p "$STUB10B"
LOG10B="$WORKDIR/systemctl10b.log"
: > "$LOG10B"
UNIT10B="loom-daemon-test-noremediate-$$.service"
cat > "$STUB10B/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$LOG10B"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show)
    case "\$*" in
      *"-p MainPID"*)         echo "0" ;;
      *"-p LoadState"*)       echo "loaded" ;;
      *"-p ExecMainCode"*)    echo "exited" ;;
      *"-p ExecMainStatus"*)  echo "1" ;;
      *)                      echo "" ;;
    esac
    ;;
  reset-failed) exit 0 ;;
  start)
    # If this were ever wrongly invoked it would show up in the log below —
    # the assertion is that 'start' never appears at all.
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB10B/systemctl"
cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=false
use_systemd=true
systemd_unit=$UNIT10B
socket_path=$WORKDIR/loom-daemon.sock
EOF
: > "$WDLOG" "$OUT"
env PATH="$STUB10B:$PATH" "${SUPERVISOR_CASE_ENV[@]}" \
    LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
    bash "$WATCHDOG" > "$OUT" 2>&1
rc10b=$?
assert_rc 1 "$rc10b" "systemd exit-1-and-down: stays report-only (no auto-remediation) -> exits 1 (#4054 no-crash-loop preserved)"
if grep -q -- '--user start ' "$LOG10B"; then
    fail "systemd exit-1-and-down: 'systemctl --user start' is NEVER invoked (no crash-loop revival)"
else
    pass "systemd exit-1-and-down: 'systemctl --user start' is NEVER invoked (no crash-loop revival)"
fi

# ===================================================================
# 11. --help works and documents the marker + StartInterval design.
# ===================================================================
help_out=$(bash "$WATCHDOG" --help 2>/dev/null)
if echo "$help_out" | grep -qi 'autonomy-desired' && echo "$help_out" | grep -qi 'StartInterval'; then
    pass "--help documents the marker + StartInterval rationale"
else
    fail "--help missing marker/StartInterval documentation"
fi
if echo "$help_out" | grep -q 'LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS' \
    && echo "$help_out" | grep -q 'LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD'; then
    pass "--help documents the #4398 IPC-probe knobs"
else
    fail "--help missing the #4398 IPC-probe knob documentation"
fi

# ===================================================================
# 12. IPC probe SUCCEEDS ⇒ unchanged healthy behavior (#4398). pid alive +
#     heartbeat fresh + a clean bounded socket round-trip ⇒ exit 0, no
#     DIVERGENCE, and no fail-count state left behind.
# ===================================================================
STUB12="$(make_daemon_stub ok)"
start_alive_and_fresh 12
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB12/loom-daemon"
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "probe succeeds: exits 0 (healthy, unchanged behavior)"
if log_has DIVERGENCE; then
    fail "probe succeeds: must not report a DIVERGENCE"
else
    pass "probe succeeds: no DIVERGENCE reported"
fi
if [[ -f "$PROBE_STATE" ]]; then
    fail "probe succeeds: no fail-count state file should exist"
else
    pass "probe succeeds: no fail-count state file left behind"
fi
rm -rf "$PS_STUB_DIR" "$STUB12"

# ===================================================================
# 13. Probe times out ONCE ⇒ DIVERGENCE reported, but explicitly NOT a
#     confirmed hang and NO remediation (#4398). Uses the #4381 stub shape (a
#     binary that never returns at all), so only the watchdog's own hard budget
#     can end the probe.
# ===================================================================
STUB13="$(make_daemon_stub hang)"
start_alive_and_fresh 13
t0=$(date +%s)
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB13/loom-daemon" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2 \
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=3
elapsed=$(( $(date +%s) - t0 ))
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$RC" "probe times out once: exits 1 (DIVERGENCE reported)"
if log_has DIVERGENCE && log_hasi 'consecutive failure 1 of 3'; then
    pass "probe times out once: reported as failure 1 of 3, not yet confirmed"
else
    fail "probe times out once: missing the 'failure 1 of 3' DIVERGENCE ($(cat "$WDLOG" 2>/dev/null))"
fi
# Matched on the FULL escalation phrase, not a bare "CONFIRMED" — the
# below-threshold message legitimately contains the words "NOT yet a confirmed
# hang", which a looser pattern would false-match.
if log_hasi 'IPC UNRESPONSIVE (CONFIRMED)'; then
    fail "probe times out once: must NOT escalate to a CONFIRMED hang"
else
    pass "probe times out once: does not escalate to a CONFIRMED hang"
fi
if (( elapsed < 20 )); then
    pass "probe times out once: the watchdog itself stayed bounded (${elapsed}s with a never-returning binary)"
else
    fail "probe times out once: watchdog took ${elapsed}s — the probe budget did not bound it"
fi
rm -rf "$PS_STUB_DIR" "$STUB13"

# ===================================================================
# 14. Probe times out N times CONSECUTIVELY ⇒ escalation (#4398): distinct
#     "IPC UNRESPONSIVE (CONFIRMED)" text, an explicit recovery command, exit 1
#     — and still no automatic kill/restart.
# ===================================================================
STUB14="$(make_daemon_stub hang)"
start_alive_and_fresh 14
probe_env=(PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1
    LOOM_DAEMON_BIN="$STUB14/loom-daemon"
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=2)
run_watchdog "${probe_env[@]}"            # tick 1 -> failure 1 of 2
rc14a=$RC
: > "$WDLOG"
run_watchdog "${probe_env[@]}"            # tick 2 -> CONFIRMED
rc14b=$RC
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$rc14a" "consecutive timeouts (tick 1): exits 1"
assert_rc 1 "$rc14b" "consecutive timeouts (tick 2, at threshold): exits 1"
if log_hasi 'IPC UNRESPONSIVE (CONFIRMED)'; then
    pass "consecutive timeouts: escalates with distinct 'IPC UNRESPONSIVE (CONFIRMED)' text"
else
    fail "consecutive timeouts: missing the escalation text ($(cat "$WDLOG" 2>/dev/null))"
fi
if log_hasi 'loom-daemon-stop.sh' && log_hasi 'loom-daemon restart'; then
    pass "consecutive timeouts: escalation names explicit recovery commands"
else
    fail "consecutive timeouts: escalation missing explicit recovery commands"
fi
if log_hasi 'no automatic kill/restart'; then
    pass "consecutive timeouts: escalation states that no auto-remediation was attempted"
else
    fail "consecutive timeouts: escalation should state that no auto-remediation was attempted"
fi
rm -rf "$PS_STUB_DIR" "$STUB14"

# ===================================================================
# 15. Post-relaunch socket-bind window (#4331/#4213): a probe against a process
#     YOUNGER than the grace window is skipped outright — no DIVERGENCE, and it
#     may never count toward the confirmed-hang tally.
# ===================================================================
STUB15="$(make_daemon_stub hang)"
start_alive_and_fresh 15
rm -rf "$PS_STUB_DIR"
PS_STUB_DIR="$(make_ps_stub "00:00:10")"   # 10s old ⇒ inside the 90s default grace
run_watchdog_verbose PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB15/loom-daemon" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2 \
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=2
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "young process (inside startup grace): probe skipped, exits 0"
if log_has DIVERGENCE; then
    fail "young process: must NOT report a DIVERGENCE during the socket-bind window"
else
    pass "young process: no DIVERGENCE during the socket-bind window"
fi
if [[ -f "$PROBE_STATE" ]]; then
    fail "young process: must NOT count toward the confirmed-hang tally"
else
    pass "young process: does not count toward the confirmed-hang tally"
fi
if log_hasi 'startup grace'; then
    pass "young process: verbose log explains the grace-window skip"
else
    fail "young process: verbose log should explain the grace-window skip"
fi
rm -rf "$PS_STUB_DIR" "$STUB15"

# ===================================================================
# 16. No resolvable `loom-daemon` binary ⇒ graceful degrade (#4398): the probe
#     is skipped, NOT reported. The watchdog must never page because its own
#     optional helper is missing. PATH is narrowed to the base system dirs so
#     the host's real installed loom-daemon is not picked up, and the marker's
#     repo_root ($WORKDIR) holds no cargo target either. HOME is also
#     sandboxed to a directory with no .local/bin (#4875's machine-level
#     install fallback in lib/locate-daemon-bin.sh checks
#     $LOOM_DAEMON_BIN_DIR/loom-daemon, default $HOME/.local/bin/loom-daemon —
#     without this override the suite would pick up this host's own real
#     ~/.local/bin/loom-daemon instead of exercising the "nothing resolvable"
#     path this test targets).
# ===================================================================
STUB16_PS="$(make_ps_stub "02:00:00")"
start_alive_and_fresh 16
rm -rf "$PS_STUB_DIR"
NO_HOME_16="$WORKDIR/no-home-16"
mkdir -p "$NO_HOME_16"
run_watchdog_verbose PATH="$STUB16_PS:/usr/bin:/bin" HOME="$NO_HOME_16" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$WORKDIR/definitely-not-a-binary"
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "no loom-daemon binary resolvable: exits 0 (graceful degrade)"
if log_has DIVERGENCE; then
    fail "no loom-daemon binary: must NOT report a DIVERGENCE"
else
    pass "no loom-daemon binary: no false DIVERGENCE"
fi
if log_hasi 'no loom-daemon binary resolvable'; then
    pass "no loom-daemon binary: verbose log explains the skip"
else
    fail "no loom-daemon binary: verbose log should explain the skip ($(cat "$WDLOG" 2>/dev/null))"
fi
rm -rf "$STUB16_PS"

# ===================================================================
# 17. No false positive under load (#4279): a slow-but-eventually-responsive
#     round-trip that completes INSIDE the probe budget is healthy, not a hang.
# ===================================================================
STUB17="$(make_daemon_stub slow-ok 2)"
start_alive_and_fresh 17
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB17/loom-daemon" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=15
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "slow-but-responsive probe (2s < 15s budget): exits 0, not flagged as hung"
if log_has DIVERGENCE; then
    fail "slow-but-responsive probe: must NOT be reported as a hang"
else
    pass "slow-but-responsive probe: not reported as a hang"
fi
rm -rf "$PS_STUB_DIR" "$STUB17"

# ===================================================================
# 18. A daemon-side APPLICATION error proves IPC works ⇒ degrade to skipped,
#     never a hang. Same for a CLI that does not know the probe subcommand
#     (an older installed build): exit 2 must not be read as a wedge.
# ===================================================================
STUB18="$(make_daemon_stub daemon-err)"
start_alive_and_fresh 18
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB18/loom-daemon" LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=1
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "daemon answered with an application error: exits 0 (IPC demonstrably works)"
if log_has DIVERGENCE; then
    fail "daemon application error: must NOT be reported as an IPC hang"
else
    pass "daemon application error: not reported as an IPC hang"
fi
rm -rf "$PS_STUB_DIR" "$STUB18"

STUB18B="$(make_daemon_stub usage)"
start_alive_and_fresh 18b
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB18B/loom-daemon" LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=1
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "probe subcommand unsupported by this build: exits 0 (graceful degrade)"
if log_has DIVERGENCE; then
    fail "unsupported probe subcommand: must NOT be reported as an IPC hang"
else
    pass "unsupported probe subcommand: not reported as an IPC hang"
fi
rm -rf "$PS_STUB_DIR" "$STUB18B"

# ===================================================================
# 19. The tally counts CONSECUTIVE failures only, and is keyed to the live pid.
#     19a: a successful probe clears a pre-existing streak.
#     19b: a streak recorded against a DIFFERENT pid is not inherited by a
#          freshly relaunched daemon (which would otherwise escalate on its
#          very first failed tick).
# ===================================================================
STUB19="$(make_daemon_stub ok)"
start_alive_and_fresh 19
printf '%s 5\n' "$LIVE_PID" > "$PROBE_STATE"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB19/loom-daemon"
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "successful probe after a streak: exits 0"
if [[ -f "$PROBE_STATE" ]]; then
    fail "successful probe: must clear the consecutive-failure streak"
else
    pass "successful probe: clears the consecutive-failure streak"
fi
rm -rf "$PS_STUB_DIR" "$STUB19"

STUB19B="$(make_daemon_stub unreachable)"
start_alive_and_fresh 19b
printf '999999 9\n' > "$PROBE_STATE"    # a streak owned by a DIFFERENT (dead) pid
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB19B/loom-daemon" LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=2
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$RC" "failed probe after a relaunch: exits 1 (reported)"
if log_hasi 'consecutive failure 1 of 2'; then
    pass "fail tally is pid-keyed: a relaunched daemon starts its own streak"
else
    fail "fail tally is pid-keyed: streak from a previous pid must not be inherited ($(cat "$WDLOG" 2>/dev/null))"
fi
if log_hasi 'IPC UNRESPONSIVE (CONFIRMED)'; then
    fail "fail tally is pid-keyed: must not escalate on the new pid's first failure"
else
    pass "fail tally is pid-keyed: no escalation on the new pid's first failure"
fi
rm -rf "$PS_STUB_DIR" "$STUB19B"

# ===================================================================
# 20. The probe is opt-out-able (LOOM_WATCHDOG_IPC_PROBE=0): a never-returning
#     binary is not even invoked, so behavior is byte-identical to pre-#4398.
# ===================================================================
STUB20="$(make_daemon_stub hang)"
start_alive_and_fresh 20
t0=$(date +%s)
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=0 \
    LOOM_DAEMON_BIN="$STUB20/loom-daemon" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2
elapsed=$(( $(date +%s) - t0 ))
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 0 "$RC" "LOOM_WATCHDOG_IPC_PROBE=0: probe disabled, exits 0"
if (( elapsed < 2 )); then
    pass "LOOM_WATCHDOG_IPC_PROBE=0: the probe binary is never invoked (${elapsed}s)"
else
    fail "LOOM_WATCHDOG_IPC_PROBE=0: took ${elapsed}s — the probe appears to have run"
fi
rm -rf "$PS_STUB_DIR" "$STUB20"

# ===================================================================
# 21. The bound holds with NO `timeout(1)` on the host (the default macOS
#     shape): LOOM_FORCE_PORTABLE_TIMEOUT=1 (the shared lib/bounded-run.sh
#     seam, #4832 -- previously the watchdog-local
#     LOOM_WATCHDOG_FORCE_PORTABLE_TIMEOUT) exercises the built-in bounded
#     runner against a never-returning binary.
# ===================================================================
STUB21="$(make_daemon_stub hang)"
start_alive_and_fresh 21
t0=$(date +%s)
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_FORCE_PORTABLE_TIMEOUT=1 \
    LOOM_DAEMON_BIN="$STUB21/loom-daemon" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2 \
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=3
elapsed=$(( $(date +%s) - t0 ))
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$RC" "no timeout(1) available: portable bounded runner still detects the hang (exit 1)"
if (( elapsed < 20 )); then
    pass "no timeout(1) available: the watchdog stayed bounded (${elapsed}s)"
else
    fail "no timeout(1) available: watchdog took ${elapsed}s — the portable bound did not hold"
fi
rm -rf "$PS_STUB_DIR" "$STUB21"

# ===================================================================
# 22. #4832: a missing/unreadable lib/bounded-run.sh must degrade to a
#     clearly-diagnosed skip, never a raw "command not found" (rc 127) on a
#     scheduled tick. Simulate by temporarily renaming the shared lib file
#     the watchdog sources bounded_run() from.
# ===================================================================
# NOTE: deliberately does NOT register its own EXIT trap for the restore —
# the suite already owns a single combined EXIT trap (line ~56, `bg_proc_reap;
# rm -rf "$WORKDIR"`), and `trap ... EXIT` REPLACES rather than stacks, so
# adding a second one here would silently disable that cleanup for every test
# after this one. Restore inline instead, immediately after the probing run.
BOUNDED_RUN_LIB="$(cd "$SCRIPT_DIR/../lib" && pwd)/bounded-run.sh"
BOUNDED_RUN_LIB_BAK="${BOUNDED_RUN_LIB}.test-disabled-4832"
mv "$BOUNDED_RUN_LIB" "$BOUNDED_RUN_LIB_BAK"

STUB22="$(make_daemon_stub unreachable)"
start_alive_and_fresh 22
run_watchdog_verbose PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB22/loom-daemon"
kill "$LIVE_PID" 2>/dev/null || true
mv "$BOUNDED_RUN_LIB_BAK" "$BOUNDED_RUN_LIB"
assert_rc 0 "$RC" "missing lib/bounded-run.sh: degrades to a skip, not a hard failure (exit 0)"
if log_hasi 'bounded_run is undefined'; then
    pass "missing lib/bounded-run.sh: clearly-diagnosed skip in the log"
else
    fail "missing lib/bounded-run.sh: expected a clear diagnostic ($(cat "$WDLOG" 2>/dev/null))"
fi
if grep -qi 'command not found' "$OUT" "$WDLOG" 2>/dev/null; then
    fail "missing lib/bounded-run.sh: raw 'command not found' leaked instead of the diagnosed skip"
else
    pass "missing lib/bounded-run.sh: no raw 'command not found' surfaced"
fi
rm -rf "$PS_STUB_DIR" "$STUB22"

# ===================================================================
# #5118 — SOCKET-FIRST LIVENESS. Everything below drives the state the fleet
# was ACTUALLY in on 2026-08-03: a healthy daemon serving its socket while the
# pid file the watchdog consulted was absent or named a long-dead pid. Before
# #5118 every one of these ticks reported `[DIVERGENCE] ... no live pid file`.
#
# The daemon stub answers `quarantine list` (mode `ok`) with no socket
# involved, so these cases are platform-independent: a launchd host and a
# systemd host run the identical code path here, which is how the "verified on
# both" acceptance criterion is met without two live hosts.
# ===================================================================

# ---- 23. pid file ABSENT + socket ANSWERS ⇒ healthy, NOT a divergence ----
# The exact worker-1 / robb-studio false alarm.
STUB23="$(make_daemon_stub ok)"
rm -f "$WORKDIR/pid23"
write_marker "$WORKDIR/pid23" 60            # marker names a pid file that does not exist
printf '%s pid=x ts=now\n' "$(date +%s)" > "$HEARTBEAT"
: > "$WDLOG"
run_watchdog_verbose LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB23/loom-daemon"
assert_rc 0 "$RC" "#5118 pid file ABSENT but socket answers: exits 0 (healthy via the IPC round-trip)"
if log_has DIVERGENCE; then
    fail "#5118 pid file absent + socket answers: reported a DIVERGENCE ($(cat "$WDLOG"))"
else
    pass "#5118 pid file absent + socket answers: no false DIVERGENCE"
fi
if log_hasi 'ANSWERS on'; then
    pass "#5118 pid file absent + socket answers: the report names the authoritative in-band signal"
else
    fail "#5118 pid file absent + socket answers: report should cite the socket round-trip ($(cat "$WDLOG"))"
fi
rm -rf "$STUB23"

# ---- 24. pid file STALE (names a dead pid) + socket ANSWERS ⇒ healthy ----
# #4774's leftover-pid shape: worse than absent, because a pid file naming a
# dead process used to read as CONFIRMED death.
STUB24="$(make_daemon_stub ok)"
dead24=$(sleeper); bg_proc_track "$dead24"; kill "$dead24" 2>/dev/null; wait "$dead24" 2>/dev/null
echo "$dead24" > "$WORKDIR/pid24"
write_marker "$WORKDIR/pid24" 60
printf '%s pid=x ts=now\n' "$(date +%s)" > "$HEARTBEAT"
: > "$WDLOG"
run_watchdog_verbose LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB24/loom-daemon"
assert_rc 0 "$RC" "#5118 STALE pid file (dead pid) but socket answers: exits 0 (healthy)"
if log_has DIVERGENCE; then
    fail "#5118 stale pid file + socket answers: reported a DIVERGENCE ($(cat "$WDLOG"))"
else
    pass "#5118 stale pid file + socket answers: no false DIVERGENCE"
fi
rm -rf "$STUB24"

# ---- 25. socket UNREACHABLE ⇒ the outage is still detected in ONE tick ----
# The load-bearing inverse: socket-first liveness must not blunt real detection.
STUB25="$(make_daemon_stub unreachable)"
rm -f "$WORKDIR/pid25"
write_marker "$WORKDIR/pid25" 60
: > "$WDLOG"
run_watchdog LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB25/loom-daemon"
assert_rc 1 "$RC" "#5118 daemon genuinely down (socket unreachable): exits 1 on the FIRST tick"
if log_has DIVERGENCE && log_hasi 'in-band probe confirms it'; then
    pass "#5118 daemon down: DIVERGENCE cites BOTH the out-of-band and in-band signals"
else
    fail "#5118 daemon down: expected a DIVERGENCE citing the in-band confirmation ($(cat "$WDLOG"))"
fi
rm -rf "$STUB25"

# ---- 26. NEITHER signal available ⇒ UNDETERMINED (exit 3), never "down" ----
# No usable pid file AND no way to ask the socket (no resolvable binary). The
# old code called this an outage; that default is what made the alarm useless.
rm -f "$WORKDIR/pid26"
write_marker "$WORKDIR/pid26" 60
: > "$WDLOG"
NO_HOME_26="$WORKDIR/no-home-26"
mkdir -p "$NO_HOME_26"
run_watchdog PATH="/usr/bin:/bin" HOME="$NO_HOME_26" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$WORKDIR/definitely-not-a-binary"
assert_rc 3 "$RC" "#5118 no pid file + no probe available: exits 3 (liveness UNDETERMINED)"
if log_hasi 'LIVENESS UNDETERMINED'; then
    pass "#5118 undetermined liveness: reported in its own words"
else
    fail "#5118 undetermined liveness: expected an UNDETERMINED report ($(cat "$WDLOG"))"
fi
if log_has DIVERGENCE; then
    fail "#5118 undetermined liveness: must NOT be reported as a DIVERGENCE/outage"
else
    pass "#5118 undetermined liveness: distinct from 'the daemon is down'"
fi

# ---- 27. LOOM_PID_FILE is honored END-TO-END (AC4) ----
# The daemon's own resolver (daemon_pidfile.rs) puts LOOM_PID_FILE first; the
# watchdog now agrees, so an explicit override outranks the marker's recorded
# path. Proven by pointing the marker at a bogus file and LOOM_PID_FILE at a
# live one — no in-band probe involved, so ONLY the env tier can explain a pass.
live27=$(sleeper); bg_proc_track "$live27"
echo "$live27" > "$WORKDIR/pid27-env"
write_marker "$WORKDIR/pid27-marker-bogus" 60    # marker path deliberately absent
printf '%s pid=%s ts=now\n' "$(date +%s)" "$live27" > "$HEARTBEAT"
: > "$WDLOG"
run_watchdog LOOM_PID_FILE="$WORKDIR/pid27-env"
kill "$live27" 2>/dev/null || true
assert_rc 0 "$RC" "#5118 LOOM_PID_FILE overrides the marker's pid_file: exits 0 (daemon found)"
if log_hasi "pid $live27 (from $WORKDIR/pid27-env) alive"; then
    pass "#5118 LOOM_PID_FILE: liveness was read from the env-named file"
else
    fail "#5118 LOOM_PID_FILE: expected liveness from the env-named file ($(cat "$WDLOG"))"
fi

# ---- 28. supervisor says DOWN while the socket ANSWERS ⇒ WARN, no kickstart ----
# An unsupervised-but-serving daemon. Auto-remediation must NOT fire: launching
# a second daemon at a socket a live one already owns can only be refused.
if [[ "$(uname -s)" == "Darwin" ]]; then
    STUB28="$WORKDIR/stub28"
    mkdir -p "$STUB28"
    LOG28="$WORKDIR/launchctl28.log"
    : > "$LOG28"
    cat > "$STUB28/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$LOG28"
case "\${1:-}" in
  print)
    echo "	state = not running"
    echo "	last exit status = 0"     # the ONE signature that would auto-kickstart
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$STUB28/launchctl"
    STUB28D="$(make_daemon_stub ok)"
    cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=true
launchd_label=com.example.loom-sandbox-unsupervised-$$
socket_path=$WORKDIR/loom-daemon.sock
EOF
    : > "$WDLOG" "$OUT"
    env PATH="$STUB28:$PATH" LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
        LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock" \
        LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB28D/loom-daemon" \
        bash "$WATCHDOG" > "$OUT" 2>&1
    rc28=$?
    assert_rc 1 "$rc28" "#5118 supervisor down + socket answers: exits 1 (state mismatch)"
    if log_hasi 'UNSUPERVISED'; then
        pass "#5118 unsupervised daemon: WARNs that dispatch still runs but nothing supervises it"
    else
        fail "#5118 unsupervised daemon: expected the UNSUPERVISED warning ($(cat "$WDLOG"))"
    fi
    if grep -qE '^kickstart ' "$LOG28"; then
        fail "#5118 unsupervised daemon: must NOT kickstart into a socket a live daemon owns"
    else
        pass "#5118 unsupervised daemon: no auto-kickstart attempted"
    fi
    if log_hasi 'Autonomous dispatch has stopped'; then
        fail "#5118 unsupervised daemon: must not claim dispatch has stopped — it demonstrably has not"
    else
        pass "#5118 unsupervised daemon: does not falsely claim dispatch has stopped"
    fi
    rm -rf "$STUB28D"
else
    pass "#5118 unsupervised-daemon (launchd) case skipped (non-Darwin host)"
fi

# ---- 29. --help documents the new contract ----
help_out_5118=$(bash "$WATCHDOG" --help 2>/dev/null)
if grep -q 'LOOM_PID_FILE' <<< "$help_out_5118" && grep -qi 'UNDETERMINED' <<< "$help_out_5118"; then
    pass "--help documents LOOM_PID_FILE and the UNDETERMINED (exit 3) contract"
else
    fail "--help should document LOOM_PID_FILE and the exit-3 UNDETERMINED contract"
fi

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]