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
#
# #5391: LOOM_WATCHDOG_AUTO_RECOVER=0 / LOOM_WATCHDOG_ESCALATE=0 are harness
# DEFAULTS for the same reason LOOM_WATCHDOG_IPC_PROBE=0 is — every case above
# section 30 asserts the DETECTION contract and must not, as a side effect,
# exec the real loom-daemon-start.sh (which would try to start an actual daemon
# on the test host) or file a real forge issue. Listed BEFORE "$@" so the
# dedicated recovery cases in section 30+ can re-enable both, pointing
# LOOM_WATCHDOG_RECOVER_CMD at a recorder stub.
run_watchdog() {
    : > "$OUT"
    env LOOM_WATCHDOG_IPC_PROBE=0 LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
        LOOM_WATCHDOG_AUTO_RECOVER=0 LOOM_WATCHDOG_ESCALATE=0 \
        LOOM_WATCHDOG_RECOVERY_STATE="$WORKDIR/.watchdog-recovery-state" \
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
        LOOM_WATCHDOG_AUTO_RECOVER=0 LOOM_WATCHDOG_ESCALATE=0 \
        LOOM_WATCHDOG_RECOVERY_STATE="$WORKDIR/.watchdog-recovery-state" \
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
                     LOOM_MACHINE_CHECKOUT= LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock"
                     LOOM_WATCHDOG_AUTO_RECOVER=0 LOOM_WATCHDOG_ESCALATE=0
                     LOOM_WATCHDOG_RECOVERY_STATE="$WORKDIR/.watchdog-recovery-state")

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
# the outcome. The stub is named `loom-daemon-mock`, NOT `loom-daemon` (#5548):
# it is resolved solely via LOOM_DAEMON_BIN (never a PATH lookup by name), and
# the `hang` mode below backgrounds an infinite loop that is the exact process
# shape (`bash <path ending in loom-daemon>`, orphaned) a leaked #5548-style
# fixture takes — a distinct name means a leak of this fixture, past this
# suite's traps, could never forge a production `pgrep -f loom-daemon`-style
# liveness check. Each mode reproduces one shape the classifier must handle:
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
            printf '#!/usr/bin/env bash\necho "no active quarantines"\nexit 0\n' > "$dir/loom-daemon-mock" ;;
        slow-ok)
            printf '#!/usr/bin/env bash\nsleep %s\necho "no active quarantines"\nexit 0\n' "$secs" > "$dir/loom-daemon-mock" ;;
        hang)
            printf '#!/usr/bin/env bash\nwhile true; do sleep 1; done\n' > "$dir/loom-daemon-mock" ;;
        unreachable)
            printf '#!/usr/bin/env bash\necho "Could not reach loom-daemon at /tmp/x.sock: round-trip timed out after 5s" >&2\nexit 1\n' > "$dir/loom-daemon-mock" ;;
        usage)
            printf '#!/usr/bin/env bash\necho "error: unrecognized subcommand" >&2\nexit 2\n' > "$dir/loom-daemon-mock" ;;
        daemon-err)
            printf '#!/usr/bin/env bash\necho "Daemon error: workspace not registered" >&2\nexit 1\n' > "$dir/loom-daemon-mock" ;;
        *) echo "unknown stub mode $mode" >&2; return 1 ;;
    esac
    chmod +x "$dir/loom-daemon-mock"
    echo "$dir"
}

# A recovery-command stub that appends one line per invocation to <log>.
#   noop    records the call and changes nothing (recovery keeps failing)
#   revive  records the call and writes a LIVE pid into <pidfile> (recovery works)
#   hang    never returns — only the watchdog's own budget can end it
make_recover_stub() { # <mode> <log> [pidfile] [pid]
    local mode="$1" log="$2" pidfile="${3:-}" pid="${4:-}" dir
    dir="$(mktemp -d)"
    case "$mode" in
        noop)
            printf '#!/usr/bin/env bash\necho "recover $*" >> %s\nexit 1\n' "$log" > "$dir/recover.sh" ;;
        revive)
            printf '#!/usr/bin/env bash\necho "recover $*" >> %s\necho %s > %s\nexit 0\n' \
                "$log" "$pid" "$pidfile" > "$dir/recover.sh" ;;
        hang)
            printf '#!/usr/bin/env bash\necho "recover $*" >> %s\nwhile true; do sleep 1; done\n' "$log" > "$dir/recover.sh" ;;
        *) echo "unknown recover stub mode $mode" >&2; return 1 ;;
    esac
    chmod +x "$dir/recover.sh"
    echo "$dir/recover.sh"
}

# ---------------------------------------------------------------------------
# #6222 (Layer 3 of #6157) peer-coordination-alert fixtures.
#
# Unlike make_daemon_stub() above (whose stubs answer identically regardless
# of argv), the peer-coordination check needs the SAME binary to answer TWO
# distinct subcommands differently within one tick: `quarantine list` (the
# ordinary IPC probe, always healthy here so `probe_verdict == healthy` and
# the peer-coordination gate is actually reached) and `peer-claims --json`
# (what the new alert reads). The JSON shape mirrors
# `loom_daemon::types::PeerClaimStatus` exactly (`coordination.degraded`,
# `.degraded_for_secs`, `.consecutive_receives_toward_recovery`,
# `.recovery_threshold`, top-level `.advertised`/`.received`).
#
#   green        coordination.degraded=false (healthy/recovered)
#   degraded     coordination.degraded=true, with the given fields
#   malformed    `peer-claims --json` "succeeds" but the body is not JSON at
#                all (a corrupted/unexpected reply) — must degrade to "could
#                not determine", never a crash or a fabricated verdict
#   unsupported  this build's CLI does not know the `peer-claims` subcommand
#                (clap usage error, exit 2) — an older loom-daemon binary
make_peer_coord_stub() { # <state: green|degraded|malformed|unsupported> [degraded_for] [consecutive] [threshold] [advertised] [received]
    local state="$1" degraded_for="${2:-120}" consecutive="${3:-1}" threshold="${4:-3}" advertised="${5:-10}" received="${6:-10}"
    local dir
    dir="$(mktemp -d)"
    case "$state" in
        green)
            printf '{"self_host":"test-host","ttl_secs":300,"entries":[],"advertised":%s,"received":%s,"expired":0,"dispatch_skipped":0,"coordination":{"degraded":false,"degraded_for_secs":null,"consecutive_receives_toward_recovery":0,"recovery_threshold":%s}}' \
                "$advertised" "$received" "$threshold" > "$dir/peer-claims.json"
            ;;
        degraded)
            printf '{"self_host":"test-host","ttl_secs":300,"entries":[],"advertised":%s,"received":%s,"expired":0,"dispatch_skipped":0,"coordination":{"degraded":true,"degraded_for_secs":%s,"consecutive_receives_toward_recovery":%s,"recovery_threshold":%s}}' \
                "$advertised" "$received" "$degraded_for" "$consecutive" "$threshold" > "$dir/peer-claims.json"
            ;;
        malformed)
            printf 'not json at all' > "$dir/peer-claims.json"
            ;;
        unsupported)
            : ;; # loom-daemon-mock below special-cases this — no json file needed
        *) echo "unknown peer-coord stub state $state" >&2; return 1 ;;
    esac

    if [[ "$state" == "unsupported" ]]; then
        printf '#!/usr/bin/env bash\nif [[ "$1" == "peer-claims" ]]; then\n  echo "error: unrecognized subcommand" >&2\n  exit 2\nfi\necho "no active quarantines"\nexit 0\n' \
            > "$dir/loom-daemon-mock"
    else
        printf '#!/usr/bin/env bash\nif [[ "$1" == "peer-claims" ]]; then\n  cat "%s/peer-claims.json"\n  exit 0\nfi\necho "no active quarantines"\nexit 0\n' \
            "$dir" > "$dir/loom-daemon-mock"
    fi
    chmod +x "$dir/loom-daemon-mock"
    echo "$dir"
}

# ---------------------------------------------------------------------------
# #5790 load-average fixtures.
#
# get_load_average() tries `uptime`, then `sysctl -n vm.loadavg`, then
# /proc/loadavg (LOOM_WATCHDOG_LOAD_AVG_PROC_PATH). Both external commands are
# stubbed via a PATH-prepended directory — the same technique make_ps_stub
# uses for `ps` — rather than relying on (or trying to hide) whatever the real
# host happens to provide, so these cases are deterministic on any CI runner
# regardless of its actual `uptime`/`sysctl` availability or load.
#
#   parseable  `uptime` prints a load-average line get_load_average() can
#              parse (Linux wording: "load average: X, Y, Z"). Prints the
#              stub dir; put it at the FRONT of PATH.
#   empty      `uptime` and `sysctl` both exist but produce nothing
#              get_load_average() can parse (used together with pointing
#              LOOM_WATCHDOG_LOAD_AVG_PROC_PATH at a nonexistent file to
#              exercise the full "unavailable" degrade without depending on
#              this host's real /proc/loadavg).
make_load_stub() { # <mode: parseable|empty> [load_avg_string for parseable]
    local mode="$1" load="${2:-}" dir
    dir="$(mktemp -d)"
    case "$mode" in
        parseable)
            printf '#!/usr/bin/env bash\necho "10:00:00 up 1 day, 2 users, load average: %s"\n' \
                "$load" > "$dir/uptime"
            chmod +x "$dir/uptime"
            ;;
        empty)
            printf '#!/usr/bin/env bash\necho "10:00:00 up 1 day, 2 users"\n' > "$dir/uptime"
            chmod +x "$dir/uptime"
            printf '#!/usr/bin/env bash\nexit 1\n' > "$dir/sysctl"
            chmod +x "$dir/sysctl"
            ;;
        *) echo "unknown load stub mode $mode" >&2; return 1 ;;
    esac
    echo "$dir"
}

PROBE_STATE="$WORKDIR/.watchdog-probe-fail-count"
PROBE_WINDOW_STATE="$WORKDIR/.watchdog-probe-window"   # #5944

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
    rm -f "$PROBE_STATE" "$PROBE_WINDOW_STATE"
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
# 10. #6388: job LOADED + NOT running + last exit status 143 (SIGTERM) with
#     the autonomy-desired marker PRESENT is NOT an operator stop — only
#     marker ABSENCE means that (see 10c below). The narrow #4232 exit-0
#     'launchctl kickstart' gate must still never fire (last exit isn't 0),
#     but the general bounded-recovery path (#5391) now DOES run for this
#     signature, exactly like any other confirmed-down signature. Before this
#     fix, exit 143 here forced `recover_possible=false` and refused to
#     recover for up to 11h in production (#6388).
# ===================================================================
if [[ "$(uname -s)" == "Darwin" ]]; then
    STUB10="$WORKDIR/stub10"
    mkdir -p "$STUB10"
    STATE10="$WORKDIR/state10"
    : > "$STATE10"
    LOG10="$WORKDIR/launchctl10.log"
    REC10LOG="$WORKDIR/recover10.log"
    : > "$LOG10" "$REC10LOG"
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
    # If the NARROW #4232 exit-0 gate were ever wrongly invoked here it WOULD
    # bring the job "up" (proving a false positive would be caught) — the
    # assertion below is that it is simply never called (last exit is 143,
    # not 0), independent of whether general bounded recovery runs.
    echo "999999" > "$STATE10"
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$STUB10/launchctl"
    cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
repo_root=$WORKDIR
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=true
launchd_label=com.example.loom-sandbox-noremediate-$$
socket_path=$WORKDIR/loom-daemon.sock
EOF
    REC10="$(make_recover_stub noop "$REC10LOG")"
    : > "$WDLOG" "$OUT"
    rm -f "$WORKDIR/.watchdog-recovery-state" "$WORKDIR/.watchdog-outage-escalated"
    env PATH="$STUB10:$PATH" "${SUPERVISOR_CASE_ENV[@]}" \
        LOOM_WATCHDOG_AUTO_RECOVER=1 LOOM_WATCHDOG_RECOVER_CMD="$REC10" \
        LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS=1 LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL=0.1 \
        LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        bash "$WATCHDOG" > "$OUT" 2>&1
    rc10=$?
    assert_rc 1 "$rc10" "#6388 exit-143-marker-present: recovery attempted but daemon still down -> exits 1"
    if grep -q 'kickstart' "$LOG10"; then
        fail "#6388 exit-143-marker-present: the narrow #4232 exit-0 gate must still never fire kickstart"
    else
        pass "#6388 exit-143-marker-present: the narrow #4232 exit-0 gate still never fires (last exit isn't 0)"
    fi
    if [[ -s "$REC10LOG" ]]; then
        pass "#6388 exit-143-marker-present: general bounded recovery (#5391) DOES run — marker present overrides the signal"
    else
        fail "#6388 exit-143-marker-present: expected the bounded-recovery command to run ($(cat "$REC10LOG" 2>/dev/null))"
    fi
    if log_hasi 'stray signal' && log_hasi 'AUTO-RECOVERING'; then
        pass "#6388 exit-143-marker-present: the report names the rule that fired (stray signal, recovering)"
    else
        fail "#6388 exit-143-marker-present: expected the report to name the stray-signal rule ($(cat "$WDLOG"))"
    fi
    rm -rf "$(dirname "$REC10")"
else
    pass "#6388 exit-143-marker-present test skipped (non-Darwin host)"
fi

# ===================================================================
# 10c. #6388 sibling of Test 10: exit 143 with the marker ABSENT preserves
#      the ORIGINAL "never revive a deliberate stop" behavior — it is marker
#      ABSENCE, not the exit code, that makes a stop deliberate. The stubbed
#      launchctl still records "last exit status = 143" so a regression that
#      reintroduces exit-code reasoning into the marker-absent branch would
#      be caught too, but the current (correct) code path never even reads
#      it: the marker-absent branch exits quietly before consulting the
#      supervisor at all.
# ===================================================================
if [[ "$(uname -s)" == "Darwin" ]]; then
    STUB10C="$WORKDIR/stub10c"
    mkdir -p "$STUB10C"
    cat > "$STUB10C/launchctl" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  print)
    echo "	state = not running"
    echo "	last exit status = 143"
    exit 0
    ;;
  *) exit 0 ;;
esac
EOF
    chmod +x "$STUB10C/launchctl"
    rm -f "$MARKER"
    : > "$WDLOG" "$OUT"
    env PATH="$STUB10C:$PATH" LOOM_WATCHDOG_IPC_PROBE=0 LOOM_PID_FILE= LOOM_WORKSPACE= \
        LOOM_MACHINE_CHECKOUT= LOOM_SOCKET_PATH="$WORKDIR/loom-daemon-10c.sock" \
        LOOM_WATCHDOG_AUTO_RECOVER=0 LOOM_WATCHDOG_ESCALATE=0 \
        LOOM_WATCHDOG_RECOVERY_STATE="$WORKDIR/.watchdog-recovery-state" \
        LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        bash "$WATCHDOG" > "$OUT" 2>&1
    rc10c=$?
    assert_rc 0 "$rc10c" "#6388 exit-143-marker-absent: no daemon expected -> stays quiet (exit 0)"
    if log_hasi 'deliberate stop, not reviving'; then
        pass "#6388 exit-143-marker-absent: the report names the rule that fired (marker absent -> deliberate stop)"
    else
        fail "#6388 exit-143-marker-absent: expected the marker-absent rule to be named ($(cat "$WDLOG"))"
    fi
else
    pass "#6388 exit-143-marker-absent test skipped (non-Darwin host)"
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
#
# #6341: these checks pipe a large (~32KB) $help_out into `grep -q`, which
# is DELIBERATELY a here-string (`<<<`), never `echo "$help_out" | grep -q`.
# With `set -o pipefail` (line 27) active, `echo "$var" | grep -q PATTERN`
# is racy for a large $var: `grep -q` may find its match and exit(0) before
# `echo` has finished writing the rest of the string to the pipe, which
# sends `echo` a SIGPIPE (exit 141). pipefail then reports the pipeline's
# status as the SIGPIPE'd echo's 141 instead of grep's 0 — a real match is
# misreported as absent, nondeterministically (host-load/scheduling
# dependent), and — since which of the several greps below races depends on
# scheduling — a DIFFERENT check fails each time this reproduces. A
# here-string has no live writer process to receive SIGPIPE (bash feeds it
# via a temp file), so it can't race. This mirrors the pattern already used
# safely at the other two `--help` capture sites in this suite (search
# `<<< "$help_out`).
# ===================================================================
help_out=$(bash "$WATCHDOG" --help 2>/dev/null)
if grep -qi 'autonomy-desired' <<< "$help_out" && grep -qi 'StartInterval' <<< "$help_out"; then
    pass "--help documents the marker + StartInterval rationale"
else
    fail "--help missing marker/StartInterval documentation"
fi
if grep -q 'LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS' <<< "$help_out" \
    && grep -q 'LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD' <<< "$help_out"; then
    pass "--help documents the #4398 IPC-probe knobs"
else
    fail "--help missing the #4398 IPC-probe knob documentation"
fi
if grep -q 'LOOM_WATCHDOG_IPC_PROBE_WINDOW_TICKS' <<< "$help_out" \
    && grep -q 'LOOM_WATCHDOG_IPC_PROBE_WINDOW_FAIL_THRESHOLD' <<< "$help_out"; then
    pass "--help documents the #5944 windowed/rate IPC-probe knobs"
else
    fail "--help missing the #5944 windowed/rate IPC-probe knob documentation"
fi

# ===================================================================
# 12. IPC probe SUCCEEDS ⇒ unchanged healthy behavior (#4398). pid alive +
#     heartbeat fresh + a clean bounded socket round-trip ⇒ exit 0, no
#     DIVERGENCE, and no fail-count state left behind.
# ===================================================================
STUB12="$(make_daemon_stub ok)"
start_alive_and_fresh 12
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB12/loom-daemon-mock"
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
    LOOM_DAEMON_BIN="$STUB13/loom-daemon-mock" \
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
# 13b. #5790: a sub-threshold DIVERGENCE must not be masked by the SAME
#      tick's heartbeat-derived report. Before #5790 this exact scenario (pid
#      alive + heartbeat fresh + probe diverged below threshold) fell through
#      to section 4 and appended an unqualified `[OK] daemon healthy ...`
#      line right after the DIVERGENCE line — so a log reader filtering for
#      `[OK]`, or reading only the tick's last line, would see a clean bill
#      of health in the same tick a divergence was already reported.
# ===================================================================
STUB13B="$(make_daemon_stub hang)"
start_alive_and_fresh 13b
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB13B/loom-daemon-mock" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2 \
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=3
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$RC" "#5790 sub-threshold divergence: still exits 1"
if log_has DIVERGENCE; then
    pass "#5790 sub-threshold divergence: the DIVERGENCE line is still logged"
else
    fail "#5790 sub-threshold divergence: missing the DIVERGENCE line ($(cat "$WDLOG" 2>/dev/null))"
fi
# The historical bug: an unqualified OK line for the heartbeat check,
# unconnected to the divergence reported moments earlier in the SAME tick.
if grep -qE '\[OK\] daemon healthy \(.*heartbeat fresh' "$WDLOG" 2>/dev/null; then
    fail "#5790 sub-threshold divergence: a plain unqualified [OK] heartbeat-healthy line must not appear in a diverged tick ($(cat "$WDLOG" 2>/dev/null))"
else
    pass "#5790 sub-threshold divergence: no plain unqualified [OK] heartbeat-healthy line"
fi
if log_hasi 'IPC probe diverged earlier this tick'; then
    pass "#5790 sub-threshold divergence: the heartbeat-derived line folds in the divergence instead"
else
    fail "#5790 sub-threshold divergence: heartbeat-derived line should note the earlier divergence ($(cat "$WDLOG" 2>/dev/null))"
fi
rm -rf "$PS_STUB_DIR" "$STUB13B"

# ===================================================================
# 14. Probe times out N times CONSECUTIVELY ⇒ escalation (#4398): distinct
#     "IPC UNRESPONSIVE (CONFIRMED)" text, an explicit recovery command, exit 1
#     — and still no automatic kill/restart.
# ===================================================================
STUB14="$(make_daemon_stub hang)"
start_alive_and_fresh 14
probe_env=(PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1
    LOOM_DAEMON_BIN="$STUB14/loom-daemon-mock"
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
# 14b. #5790: a host load-average sample is attached to a sub-threshold
#      DIVERGENCE report (Ask item 3 — genuinely net-new, no load-sampling
#      existed anywhere in this script before #5790).
# ===================================================================
STUB14B="$(make_daemon_stub hang)"
LOAD14B="$(make_load_stub parseable "1.23, 0.98, 0.87")"
start_alive_and_fresh 14b
run_watchdog PATH="$LOAD14B:$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB14B/loom-daemon-mock" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2 \
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=3
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$RC" "#5790 load average (sub-threshold): still exits 1"
if log_hasi 'Host load average at probe time: 1.23, 0.98, 0.87'; then
    pass "#5790 load average (sub-threshold): sampled load appears in the DIVERGENCE report"
else
    fail "#5790 load average (sub-threshold): missing the load-average sample ($(cat "$WDLOG" 2>/dev/null))"
fi
rm -rf "$PS_STUB_DIR" "$STUB14B" "$LOAD14B"

# ===================================================================
# 14c. #5790: a host load-average sample is attached to the CONFIRMED
#      DIVERGENCE report too, not just the sub-threshold one.
# ===================================================================
STUB14C="$(make_daemon_stub hang)"
LOAD14C="$(make_load_stub parseable "4.50, 3.10, 2.00")"
start_alive_and_fresh 14c
probe_env_14c=(PATH="$LOAD14C:$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1
    LOOM_DAEMON_BIN="$STUB14C/loom-daemon-mock"
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=2)
run_watchdog "${probe_env_14c[@]}"      # tick 1 -> failure 1 of 2
: > "$WDLOG"
run_watchdog "${probe_env_14c[@]}"      # tick 2 -> CONFIRMED
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$RC" "#5790 load average (CONFIRMED): still exits 1"
if log_hasi 'IPC UNRESPONSIVE (CONFIRMED)' && log_hasi 'Host load average at probe time: 4.50, 3.10, 2.00'; then
    pass "#5790 load average (CONFIRMED): sampled load appears in the CONFIRMED escalation"
else
    fail "#5790 load average (CONFIRMED): missing the load-average sample in the escalation ($(cat "$WDLOG" 2>/dev/null))"
fi
rm -rf "$PS_STUB_DIR" "$STUB14C" "$LOAD14C"

# ===================================================================
# 14d. #5790: graceful degradation when no load source is readable — the
#      tick must still complete and report normally, never fail on its own
#      account. get_load_average() falls back to the literal "unavailable".
# ===================================================================
STUB14D="$(make_daemon_stub hang)"
LOAD14D="$(make_load_stub empty)"
start_alive_and_fresh 14d
run_watchdog PATH="$LOAD14D:$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 \
    LOOM_DAEMON_BIN="$STUB14D/loom-daemon-mock" \
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2 \
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=3 \
    LOOM_WATCHDOG_LOAD_AVG_PROC_PATH="$WORKDIR/no-such-loadavg-14d"
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$RC" "#5790 load average unavailable: the tick still completes and reports (exit 1)"
if log_hasi 'Host load average at probe time: unavailable'; then
    pass "#5790 load average unavailable: degrades to the literal 'unavailable', does not fail the tick"
else
    fail "#5790 load average unavailable: should degrade to 'unavailable' ($(cat "$WDLOG" 2>/dev/null))"
fi
rm -rf "$PS_STUB_DIR" "$STUB14D" "$LOAD14D"

# ===================================================================
# 14e. #5944: an INTERMITTENT probe (fail, succeed, fail, succeed, ... — NEVER
#      3 in a row) never crosses the CONSECUTIVE threshold (#4398) and never
#      re-diverges within the SAME tick (#5790), so before #5944 every
#      succeeding tick reported a bare `[OK] daemon healthy` regardless of how
#      much recent history it followed — the exact hours-long "[OK] while
#      status times out" condition reported live on a loaded host. The
#      windowed/rate signal must surface this WITHOUT over-triggering on the
#      very first isolated failure (#4279's false-positive concern) and
#      WITHOUT ever crossing into the CONFIRMED/exit-1-no-remediation
#      escalation reserved for a proven-sustained (3-consecutive) hang.
#
#      Drives 6 ticks over one pid: F S F S F S, with
#      LOOM_WATCHDOG_IPC_PROBE_WINDOW_TICKS=6 / _WINDOW_FAIL_THRESHOLD=3 (the
#      shipped defaults, pinned explicitly so this test does not silently stop
#      meaning anything if the defaults ever change). By tick 6 the window
#      ("101010") holds exactly 3 failures out of the last 6 ticks — the
#      windowed threshold — while the CONSECUTIVE streak was reset to 1 (or
#      less) by every intervening success and never once reached the
#      CONFIRMED threshold of 3.
# ===================================================================
STUB14E_OK="$(make_daemon_stub ok)"
STUB14E_FAIL="$(make_daemon_stub unreachable)"
start_alive_and_fresh 14e
probe_env_14e=(PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1
    LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS=2
    LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=3
    LOOM_WATCHDOG_IPC_PROBE_WINDOW_TICKS=6
    LOOM_WATCHDOG_IPC_PROBE_WINDOW_FAIL_THRESHOLD=3)

# ---- tick 1: F -> sub-threshold DIVERGENCE (unchanged #5790/#4398 behavior) ----
: > "$WDLOG"
run_watchdog "${probe_env_14e[@]}" LOOM_DAEMON_BIN="$STUB14E_FAIL/loom-daemon-mock"
rc14e_t1=$RC
assert_rc 1 "$rc14e_t1" "#5944 intermittent (tick 1, fail): exits 1"
if log_hasi 'consecutive failure 1 of 3'; then
    pass "#5944 intermittent (tick 1): reported as an ordinary sub-threshold failure"
else
    fail "#5944 intermittent (tick 1): missing the expected sub-threshold DIVERGENCE ($(cat "$WDLOG" 2>/dev/null))"
fi

# ---- tick 2: S -> only 1 of the last 2 ticks failed (below the window
#      threshold of 3) -- must NOT over-trigger DEGRADED off a single isolated
#      failure. This is the #4279 false-positive guard for the new signal. ----
: > "$WDLOG"
run_watchdog "${probe_env_14e[@]}" LOOM_DAEMON_BIN="$STUB14E_OK/loom-daemon-mock"
rc14e_t2=$RC
assert_rc 0 "$rc14e_t2" "#5944 intermittent (tick 2, succeeds after ONE prior failure): exits 0"
if grep -qE '\[OK\] daemon healthy \(.*heartbeat fresh' "$WDLOG" 2>/dev/null; then
    pass "#5944 intermittent (tick 2): a single prior failure alone does not over-trigger DEGRADED"
else
    fail "#5944 intermittent (tick 2): a lone prior failure incorrectly suppressed the plain OK ($(cat "$WDLOG" 2>/dev/null))"
fi

# ---- ticks 3-5: F, S, F -- window keeps accumulating (2 failures of the last
#      4, still below threshold on tick 4's success). ----
run_watchdog "${probe_env_14e[@]}" LOOM_DAEMON_BIN="$STUB14E_FAIL/loom-daemon-mock"   # tick 3: F
run_watchdog "${probe_env_14e[@]}" LOOM_DAEMON_BIN="$STUB14E_OK/loom-daemon-mock"     # tick 4: S
run_watchdog "${probe_env_14e[@]}" LOOM_DAEMON_BIN="$STUB14E_FAIL/loom-daemon-mock"   # tick 5: F

# ---- tick 6: S -- the window is now "101010": 3 of the last 6 ticks failed,
#      crossing the windowed threshold on a tick whose OWN round-trip
#      succeeded. This is the case #5944 exists for: NOT a bare OK. ----
: > "$WDLOG"
run_watchdog "${probe_env_14e[@]}" LOOM_DAEMON_BIN="$STUB14E_OK/loom-daemon-mock"
rc14e_t6=$RC
kill "$LIVE_PID" 2>/dev/null || true
assert_rc 1 "$rc14e_t6" "#5944 intermittent (tick 6, succeeds after 3-of-6 recent failures): exits 1 (not silently healthy)"
if grep -qE '\[OK\] daemon healthy \(.*heartbeat fresh' "$WDLOG" 2>/dev/null; then
    fail "#5944 intermittent (tick 6): must NOT report a plain unqualified [OK] after 3 of the last 6 ticks failed ($(cat "$WDLOG" 2>/dev/null))"
else
    pass "#5944 intermittent (tick 6): no plain unqualified [OK] line despite this tick's own round-trip succeeding"
fi
if log_hasi 'DEGRADED'; then
    pass "#5944 intermittent (tick 6): reported DEGRADED"
else
    fail "#5944 intermittent (tick 6): expected a DEGRADED verdict ($(cat "$WDLOG" 2>/dev/null))"
fi
if log_hasi '3 of the last 6'; then
    pass "#5944 intermittent (tick 6): the report quantifies the windowed failure rate"
else
    fail "#5944 intermittent (tick 6): report should state the windowed tally ($(cat "$WDLOG" 2>/dev/null))"
fi
if log_hasi 'Host load average at probe time'; then
    pass "#5944 intermittent (tick 6): the DEGRADED report records host load at probe time"
else
    fail "#5944 intermittent (tick 6): DEGRADED report is missing the host load-average capture ($(cat "$WDLOG" 2>/dev/null))"
fi
# Matched on the full escalation phrase, same discipline as the #5790 test
# above -- the windowed signal must stay distinct from the hard,
# no-remediation CONFIRMED escalation reserved for a PROVEN 3-consecutive hang.
if log_hasi 'IPC UNRESPONSIVE (CONFIRMED)'; then
    fail "#5944 intermittent (tick 6): an intermittent (never-3-consecutive) pattern must NOT escalate to a CONFIRMED hang"
else
    pass "#5944 intermittent (tick 6): never escalates to the CONFIRMED/no-remediation path"
fi
rm -rf "$PS_STUB_DIR" "$STUB14E_OK" "$STUB14E_FAIL"

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
    LOOM_DAEMON_BIN="$STUB15/loom-daemon-mock" \
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
    LOOM_DAEMON_BIN="$STUB17/loom-daemon-mock" \
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
    LOOM_DAEMON_BIN="$STUB18/loom-daemon-mock" LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=1
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
    LOOM_DAEMON_BIN="$STUB18B/loom-daemon-mock" LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=1
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
    LOOM_DAEMON_BIN="$STUB19/loom-daemon-mock"
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
    LOOM_DAEMON_BIN="$STUB19B/loom-daemon-mock" LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD=2
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
    LOOM_DAEMON_BIN="$STUB20/loom-daemon-mock" \
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
    LOOM_DAEMON_BIN="$STUB21/loom-daemon-mock" \
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
    LOOM_DAEMON_BIN="$STUB22/loom-daemon-mock"
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
# The exact worker-1 / studio-host false alarm.
STUB23="$(make_daemon_stub ok)"
rm -f "$WORKDIR/pid23"
write_marker "$WORKDIR/pid23" 60            # marker names a pid file that does not exist
printf '%s pid=x ts=now\n' "$(date +%s)" > "$HEARTBEAT"
: > "$WDLOG"
run_watchdog_verbose LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB23/loom-daemon-mock"
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
run_watchdog_verbose LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB24/loom-daemon-mock"
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
run_watchdog LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB25/loom-daemon-mock"
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
        LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB28D/loom-daemon-mock" \
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

# ===================================================================
# #5391 — GENERAL-CASE BOUNDED RECOVERY + CIRCUIT BREAKER.
#
# Everything below drives the divergence signature that produced the reported
# incident: a daemon that is EXPECTED, CONFIRMED down, and does NOT match either
# narrow #4232/#4862 auto-remediation gate ("loaded + down + last exit 0"). That
# path was report-only, so a fleet host logged 252 identical `[DIVERGENCE] …
# Recover with: loom-daemon-start.sh` lines in eight days — including a
# continuous 1h40m outage — and never ran the command it kept printing.
#
# The recovery command is ALWAYS a stub pinned via LOOM_WATCHDOG_RECOVER_CMD:
# no real loom-daemon-start.sh is ever executed, no real daemon started, and
# (test 33 aside, which stubs create-issue.sh too) no forge call made. The
# initial down state is the pid-file tier — a pid file naming a dead pid plus an
# `unreachable` socket stub — so these cases are platform-independent: a launchd
# host and a systemd host run the identical code path here.
# ===================================================================

RECOVERY_STATE="$WORKDIR/.watchdog-recovery-state"
OUTAGE_SENTINEL="$WORKDIR/.watchdog-outage-escalated"

# Stand up "a daemon is expected and is CONFIRMED down" on the pid-file tier:
# the marker names a pid file holding a dead pid, and the in-band socket probe
# (stub mode `unreachable`) corroborates. Clears any prior episode state so each
# case starts with a full attempt budget. Sets DOWN_STUB (caller must rm -rf).
DOWN_STUB=""
start_confirmed_down() { # <pid_file_suffix>
    local dead
    DOWN_STUB="$(make_daemon_stub unreachable)"
    dead=$(sleeper); bg_proc_track "$dead"; kill "$dead" 2>/dev/null; wait "$dead" 2>/dev/null
    echo "$dead" > "$WORKDIR/pid$1"
    write_marker "$WORKDIR/pid$1" 60
    : > "$WDLOG"
    rm -f "$RECOVERY_STATE" "$OUTAGE_SENTINEL"
}

# Common env for the recovery cases: probe on (so the socket confirms the
# outage), recovery ON, escalation OFF unless a case opts in, and a fast recheck
# so a failing recovery does not add seconds per tick.
recover_env() { # <recover-cmd> [extra KEY=VAL...]
    local cmd="$1"; shift
    echo LOOM_WATCHDOG_IPC_PROBE=1 \
         LOOM_DAEMON_BIN="$DOWN_STUB/loom-daemon-mock" \
         LOOM_WATCHDOG_AUTO_RECOVER=1 \
         LOOM_WATCHDOG_RECOVER_CMD="$cmd" \
         LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS=1 \
         LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL=0.1 \
         "$@"
}

# ---- 30. Confirmed outage ⇒ the watchdog RUNS the recovery, and succeeding ----
#         ends the episode: exit 0, an OK "auto-recovery SUCCEEDED" report, and
#         no leftover episode state. This is the whole point of #5391 — the
#         252-divergence log line that never acted.
LOG30="$WORKDIR/recover30.log"; : > "$LOG30"
start_confirmed_down 30
live30=$(sleeper); bg_proc_track "$live30"
REC30="$(make_recover_stub revive "$LOG30" "$WORKDIR/pid30" "$live30")"
# shellcheck disable=SC2046  # deliberate word-splitting of the env-assignment list
run_watchdog $(recover_env "$REC30")
kill "$live30" 2>/dev/null || true
assert_rc 0 "$RC" "#5391 confirmed outage + successful recovery: exits 0"
if [[ "$(wc -l < "$LOG30" | tr -d ' ')" == "1" ]]; then
    pass "#5391 confirmed outage: the recovery command is actually INVOKED (exactly once)"
else
    fail "#5391 confirmed outage: expected exactly one recovery invocation, got '$(cat "$LOG30")'"
fi
if log_hasi 'auto-recovery SUCCEEDED'; then
    pass "#5391 successful recovery: reported as a success, not a bare divergence"
else
    fail "#5391 successful recovery: missing the success report ($(cat "$WDLOG"))"
fi
if [[ -f "$RECOVERY_STATE" ]]; then
    fail "#5391 successful recovery: episode state must be cleared"
else
    pass "#5391 successful recovery: episode state cleared (next outage gets a full budget)"
fi
rm -rf "$DOWN_STUB" "$(dirname "$REC30")"

# ---- 31. Repeated failure ⇒ bounded attempts, then the breaker LATCHES ----
#         With the backoff pinned to 0 every tick may attempt, so the ONLY thing
#         that can stop the third tick from restarting again is the circuit
#         breaker. Budget 2 ⇒ at most 2 invocations across 3 ticks, forever.
LOG31="$WORKDIR/recover31.log"; : > "$LOG31"
start_confirmed_down 31
REC31="$(make_recover_stub noop "$LOG31")"
rec31_env=$(recover_env "$REC31" LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS=2 LOOM_WATCHDOG_RECOVER_BACKOFF_SECS=0)
# shellcheck disable=SC2086,SC2046
run_watchdog $rec31_env; rc31a=$RC
# shellcheck disable=SC2086,SC2046
run_watchdog $rec31_env; rc31b=$RC
: > "$WDLOG"
# shellcheck disable=SC2086,SC2046
run_watchdog $rec31_env; rc31c=$RC
assert_rc 1 "$rc31a" "#5391 failing recovery (tick 1): exits 1"
assert_rc 1 "$rc31b" "#5391 failing recovery (tick 2, budget spent): exits 1"
assert_rc 1 "$rc31c" "#5391 failing recovery (tick 3, breaker open): exits 1"
if [[ "$(wc -l < "$LOG31" | tr -d ' ')" == "2" ]]; then
    pass "#5391 circuit breaker: exactly 2 recovery attempts across 3 down ticks (budget honored)"
else
    fail "#5391 circuit breaker: expected 2 invocations for a budget of 2, got $(wc -l < "$LOG31") ($(cat "$LOG31"))"
fi
if log_hasi 'CIRCUIT BREAKER OPEN'; then
    pass "#5391 circuit breaker: the post-budget tick says the breaker is OPEN"
else
    fail "#5391 circuit breaker: expected a 'CIRCUIT BREAKER OPEN' report ($(cat "$WDLOG"))"
fi
if log_hasi 'consecutive watchdog ticks'; then
    pass "#5391 circuit breaker: the report quantifies how long the outage has run"
else
    fail "#5391 circuit breaker: report should quantify the outage duration ($(cat "$WDLOG"))"
fi
rm -rf "$DOWN_STUB" "$(dirname "$REC31")"

# ---- 32. Exponential backoff defers the NEXT attempt ----
#         Same setup with a large backoff: tick 2 must report, but must NOT
#         re-run the recovery command. Without this, a 300s cadence would mean a
#         restart every 300s regardless of how hopeless it is.
LOG32="$WORKDIR/recover32.log"; : > "$LOG32"
start_confirmed_down 32
REC32="$(make_recover_stub noop "$LOG32")"
rec32_env=$(recover_env "$REC32" LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS=5 LOOM_WATCHDOG_RECOVER_BACKOFF_SECS=3600)
# shellcheck disable=SC2086,SC2046
run_watchdog $rec32_env
: > "$WDLOG"
# shellcheck disable=SC2086,SC2046
run_watchdog $rec32_env; rc32=$RC
assert_rc 1 "$rc32" "#5391 backoff: the deferred tick still reports the outage (exit 1)"
if [[ "$(wc -l < "$LOG32" | tr -d ' ')" == "1" ]]; then
    pass "#5391 backoff: the second tick does NOT re-run the recovery command"
else
    fail "#5391 backoff: expected 1 invocation, got $(wc -l < "$LOG32") ($(cat "$LOG32"))"
fi
if log_hasi 'BACKED OFF'; then
    pass "#5391 backoff: the deferred tick explains that the attempt is backed off"
else
    fail "#5391 backoff: expected a 'BACKED OFF' explanation ($(cat "$WDLOG"))"
fi
rm -rf "$DOWN_STUB" "$(dirname "$REC32")"

# ---- 33. Breaker trip ⇒ ONE out-of-band escalation, deduped by a sentinel ----
#         The AC that matters most: an operator must learn about an unrecovered
#         outage WITHOUT tailing daemon-watchdog.log. create-issue.sh is stubbed
#         under the marker's repo_root, so no forge call leaves the tempdir.
LOG33="$WORKDIR/recover33.log"; : > "$LOG33"
ISSUE33="$WORKDIR/create-issue33.log"; : > "$ISSUE33"
mkdir -p "$WORKDIR/.loom/scripts"
# Records ONE line per invocation (title + labels) so the call count is a plain
# line count; the multi-line body is captured separately for content assertions.
BODY33="$WORKDIR/create-issue33-body.txt"; : > "$BODY33"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
title=""; labels=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --title|-t) title="\$2"; shift 2 ;;
    --label|-l) labels="\$labels \$2"; shift 2 ;;
    --body|-b)  printf '%s\n' "\$2" >> "$BODY33"; shift 2 ;;
    *) shift ;;
  esac
done
echo "create-issue title=\$title labels=\$labels" >> "$ISSUE33"
echo "https://example.invalid/issues/1"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
start_confirmed_down 33
REC33="$(make_recover_stub noop "$LOG33")"
rec33_env=$(recover_env "$REC33" LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS=1 \
    LOOM_WATCHDOG_RECOVER_BACKOFF_SECS=0 LOOM_WATCHDOG_ESCALATE=1 \
    LOOM_WATCHDOG_ESCALATION_SENTINEL="$OUTAGE_SENTINEL")
# shellcheck disable=SC2086,SC2046
run_watchdog $rec33_env; rc33=$RC
assert_rc 1 "$rc33" "#5391 escalation: the breaker-trip tick still exits 1"
if [[ "$(wc -l < "$ISSUE33" | tr -d ' ')" == "1" ]]; then
    pass "#5391 escalation: files exactly ONE forge issue when the breaker trips"
else
    fail "#5391 escalation: expected one create-issue.sh call, got $(wc -l < "$ISSUE33") ($(cat "$ISSUE33"))"
fi
if grep -q 'loom:triage' "$ISSUE33"; then
    pass "#5391 escalation: the filed issue carries a triage label (enters the normal queue)"
else
    fail "#5391 escalation: expected a labelled filing ($(cat "$ISSUE33"))"
fi
if grep -qi 'circuit breaker is OPEN' "$BODY33" && grep -q 'loom-daemon-start.sh' "$BODY33"; then
    pass "#5391 escalation: the issue body states why recovery stopped and how to fix it by hand"
else
    fail "#5391 escalation: issue body should explain the breaker state + manual recovery ($(cat "$BODY33"))"
fi
if [[ -f "$OUTAGE_SENTINEL" ]]; then
    pass "#5391 escalation: a dedupe sentinel is written"
else
    fail "#5391 escalation: expected a dedupe sentinel at $OUTAGE_SENTINEL"
fi
if log_hasi 'ESCALATED out-of-band'; then
    pass "#5391 escalation: the log records that the outage left the logfile"
else
    fail "#5391 escalation: expected an 'ESCALATED out-of-band' note ($(cat "$WDLOG"))"
fi
: > "$WDLOG"
# shellcheck disable=SC2086,SC2046
run_watchdog $rec33_env
if [[ "$(wc -l < "$ISSUE33" | tr -d ' ')" == "1" ]]; then
    pass "#5391 escalation: a second down tick does NOT file a duplicate issue"
else
    fail "#5391 escalation: duplicate filing on the next tick ($(cat "$ISSUE33"))"
fi
if log_hasi 'ALREADY been escalated'; then
    pass "#5391 escalation: the repeat tick says the outage is already escalated"
else
    fail "#5391 escalation: expected an already-escalated note ($(cat "$WDLOG"))"
fi
rm -rf "$DOWN_STUB" "$(dirname "$REC33")" "$WORKDIR/.loom"
rm -f "$OUTAGE_SENTINEL"

# ---- 34. #6388: a signal-shaped exit (systemd killed/TERM) with the marker ----
#          PRESENT is NOT an operator stop -- it now gets the SAME general
#          bounded recovery (#5391) as any other confirmed-down signature,
#          exactly like Test 35's ExecMainStatus=1 crash case below. Before
#          this fix this exact signature forced `recover_possible=false` and
#          the recovery command was NEVER invoked -- the systemd mirror of the
#          11h outage #6388 reports on the launchd side (Test 10).
STUB34="$WORKDIR/stub34"; mkdir -p "$STUB34"
LOG34="$WORKDIR/recover34.log"; : > "$LOG34"
UNIT34="loom-daemon-test-signal-exit-$$.service"
cat > "$STUB34/systemctl" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show)
    case "\$*" in
      *"-p MainPID"*)        echo "0" ;;
      *"-p LoadState"*)      echo "loaded" ;;
      *"-p ExecMainCode"*)   echo "killed" ;;
      *"-p ExecMainStatus"*) echo "TERM" ;;
      *)                     echo "" ;;
    esac
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB34/systemctl"
cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
repo_root=$WORKDIR
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=false
use_systemd=true
systemd_unit=$UNIT34
socket_path=$WORKDIR/loom-daemon.sock
EOF
REC34="$(make_recover_stub noop "$LOG34")"
: > "$WDLOG" "$OUT"
rm -f "$RECOVERY_STATE"
env PATH="$STUB34:$PATH" LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
    LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock" LOOM_WATCHDOG_IPC_PROBE=0 \
    LOOM_WATCHDOG_RECOVERY_STATE="$RECOVERY_STATE" LOOM_WATCHDOG_ESCALATE=0 \
    LOOM_WATCHDOG_AUTO_RECOVER=1 LOOM_WATCHDOG_RECOVER_CMD="$REC34" \
    LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
    bash "$WATCHDOG" > "$OUT" 2>&1
rc34=$?
assert_rc 1 "$rc34" "#6388 signal-exit-marker-present: recovery attempted but daemon still down -> exits 1"
if [[ -s "$LOG34" ]]; then
    pass "#6388 signal-exit-marker-present: general bounded recovery (#5391) DOES run — marker present overrides the signal"
else
    fail "#6388 signal-exit-marker-present: expected the bounded-recovery command to run ($(cat "$LOG34"))"
fi
if log_hasi 'stray signal' && log_hasi 'AUTO-RECOVERING'; then
    pass "#6388 signal-exit-marker-present: the report names the rule that fired (stray signal, recovering)"
else
    fail "#6388 signal-exit-marker-present: expected the report to name the stray-signal rule ($(cat "$WDLOG"))"
fi
if log_hasi 'operator-initiated stop'; then
    fail "#6388 signal-exit-marker-present: must NOT be labelled a deliberate operator stop while the marker is present ($(cat "$WDLOG"))"
else
    pass "#6388 signal-exit-marker-present: not mislabelled as a deliberate operator stop"
fi
rm -rf "$(dirname "$REC34")"

# ---- 35. A crash (ExecMainStatus=1) gets BOUNDED recovery, not the narrow ----
#          gate. #4862's guarantee was "a crash never triggers a naive
#          `systemctl --user start` revival"; that stays true — the narrow gate
#          is still silent — while #5391's bounded, budgeted recovery does act.
STUB35="$WORKDIR/stub35"; mkdir -p "$STUB35"
LOG35="$WORKDIR/recover35.log"; : > "$LOG35"
SYSLOG35="$WORKDIR/systemctl35.log"; : > "$SYSLOG35"
UNIT35="loom-daemon-test-crash-$$.service"
cat > "$STUB35/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$SYSLOG35"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show)
    case "\$*" in
      *"-p MainPID"*)        echo "0" ;;
      *"-p LoadState"*)      echo "loaded" ;;
      *"-p ExecMainCode"*)   echo "exited" ;;
      *"-p ExecMainStatus"*) echo "1" ;;
      *)                     echo "" ;;
    esac
    ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$STUB35/systemctl"
cat > "$MARKER" <<EOF
started_at=2026-07-27T00:00:00Z
repo_root=$WORKDIR
heartbeat_file=$HEARTBEAT
heartbeat_interval_secs=60
use_launchd=false
use_systemd=true
systemd_unit=$UNIT35
socket_path=$WORKDIR/loom-daemon.sock
EOF
REC35="$(make_recover_stub noop "$LOG35")"
: > "$WDLOG" "$OUT"
rm -f "$RECOVERY_STATE"
env PATH="$STUB35:$PATH" LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
    LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock" LOOM_WATCHDOG_IPC_PROBE=0 \
    LOOM_WATCHDOG_RECOVERY_STATE="$RECOVERY_STATE" LOOM_WATCHDOG_ESCALATE=0 \
    LOOM_WATCHDOG_AUTO_RECOVER=1 LOOM_WATCHDOG_RECOVER_CMD="$REC35" \
    LOOM_WATCHDOG_KICKSTART_RECHECK_ATTEMPTS=1 LOOM_WATCHDOG_KICKSTART_RECHECK_INTERVAL=0.1 \
    LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
    bash "$WATCHDOG" > "$OUT" 2>&1
rc35=$?
assert_rc 1 "$rc35" "#5391 crash + failed recovery: still exits 1"
if grep -q -- '--user start ' "$SYSLOG35"; then
    fail "#5391 crash: the #4862 narrow gate must still NOT fire 'systemctl --user start'"
else
    pass "#5391 crash: the #4862 narrow gate stays silent (no naive crash-loop revival)"
fi
if [[ "$(wc -l < "$LOG35" | tr -d ' ')" == "1" ]]; then
    pass "#5391 crash: bounded recovery runs exactly once on this tick"
else
    fail "#5391 crash: expected exactly one bounded recovery attempt, got '$(cat "$LOG35")'"
fi
if log_hasi 'attempt 1 of'; then
    pass "#5391 crash: the report numbers the attempt against the breaker budget"
else
    fail "#5391 crash: expected an 'attempt N of M' report ($(cat "$WDLOG"))"
fi
rm -rf "$(dirname "$REC35")"

# ---- 36. Recovery DISABLED ⇒ the watchdog says so in its own words ----
#          So an operator can never read "watchdog unit installed" as
#          "self-healing host". This is the report-only half of the AC.
start_confirmed_down 36
: > "$WDLOG"
run_watchdog LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$DOWN_STUB/loom-daemon-mock" \
    LOOM_WATCHDOG_AUTO_RECOVER=0
assert_rc 1 "$RC" "#5391 recovery disabled: still reports the outage (exit 1)"
if log_hasi 'REPORT-ONLY' && log_hasi 'DETECTION, not self-healing'; then
    pass "#5391 recovery disabled: the report states it is DETECTION, not self-healing"
else
    fail "#5391 recovery disabled: expected an explicit report-only statement ($(cat "$WDLOG"))"
fi
rm -rf "$DOWN_STUB"

# ---- 37. A healthy tick ENDS the episode (clears state + sentinel) ----
#          Health, not elapsed time, is what resets the breaker — so a daemon
#          that comes back gets a full budget for its next outage, and a
#          resolved outage never dedupes the NEXT escalation.
live37=$(sleeper); bg_proc_track "$live37"
echo "$live37" > "$WORKDIR/pid37"
write_marker "$WORKDIR/pid37" 60
printf '%s pid=%s ts=now\n' "$(date +%s)" "$live37" > "$HEARTBEAT"
printf 'down_since=1\nticks=9\nattempts=9\nlast_attempt=1\n' > "$RECOVERY_STATE"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$OUTAGE_SENTINEL"
: > "$WDLOG"
run_watchdog LOOM_WATCHDOG_ESCALATION_SENTINEL="$OUTAGE_SENTINEL"
kill "$live37" 2>/dev/null || true
assert_rc 0 "$RC" "#5391 healthy tick after an outage: exits 0"
if [[ -f "$RECOVERY_STATE" || -f "$OUTAGE_SENTINEL" ]]; then
    fail "#5391 healthy tick: must clear the episode state AND the escalation sentinel"
else
    pass "#5391 healthy tick: episode state and escalation sentinel both cleared"
fi

# ---- 38. The recovery command itself is BOUNDED ----
#          A wedged start must not wedge the tick — a StartInterval/timer job
#          that never returns is exactly the crash-and-stay-dead shape this
#          watchdog's whole design avoids.
LOG38="$WORKDIR/recover38.log"; : > "$LOG38"
start_confirmed_down 38
REC38="$(make_recover_stub hang "$LOG38")"
t38=$(date +%s)
# shellcheck disable=SC2046
run_watchdog $(recover_env "$REC38" LOOM_WATCHDOG_RECOVER_TIMEOUT_SECS=2)
elapsed38=$(( $(date +%s) - t38 ))
assert_rc 1 "$RC" "#5391 wedged recovery command: the tick still completes (exit 1)"
if (( elapsed38 < 30 )); then
    pass "#5391 wedged recovery command: the tick stayed bounded (${elapsed38}s)"
else
    fail "#5391 wedged recovery command: took ${elapsed38}s — the recovery budget did not hold"
fi
rm -rf "$DOWN_STUB" "$(dirname "$REC38")"

# ---- 39. --help documents the recovery policy and its knobs ----
help_out_5391=$(bash "$WATCHDOG" --help 2>/dev/null)
if grep -q 'LOOM_WATCHDOG_AUTO_RECOVER' <<< "$help_out_5391" \
    && grep -q 'LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS' <<< "$help_out_5391" \
    && grep -q 'LOOM_WATCHDOG_RECOVER_BACKOFF_SECS' <<< "$help_out_5391"; then
    pass "--help documents the #5391 bounded-recovery knobs"
else
    fail "--help missing the #5391 bounded-recovery knob documentation"
fi
if grep -qi 'CIRCUIT BREAKER' <<< "$help_out_5391" \
    && grep -qi 'this watchdog RECOVERS' <<< "$help_out_5391"; then
    pass "--help states the recover-vs-report-only decision and the circuit breaker"
else
    fail "--help should state the recover-vs-report-only decision explicitly"
fi

# ===================================================================
# 40-45. Peer-coordination out-of-band alert (#6222, Layer 3 of #6157).
#
#         Distinct from every #5391 outage case above: the daemon here is
#         FULLY ALIVE and answering (a fresh pid + fresh heartbeat, and the
#         ordinary `quarantine list` IPC probe always succeeds) — peer
#         coordination degrading is orthogonal to liveness, which is exactly
#         why #6157 needed its own health section rather than folding into
#         the existing liveness/heartbeat contract. All cases pin
#         LOOM_WATCHDOG_ESCALATE=1 (the harness default is 0, mirroring the
#         #5391 cases' own reason for pinning it) and a fresh
#         PEER_COORD_SENTINEL path so no case can dedupe against another's
#         leftover state. Every case also pins a fresh PEER_COORD_COOLDOWN_STATE
#         path (#7258) for the identical reason: without it, cases 40-49 would
#         share ONE cooldown-state file (defaulting to <loom dir>, i.e.
#         $WORKDIR here) and test 42's recovery would poison every later
#         "expect a fresh filing" case with a cooldown it never asked for.
#         The dedicated cooldown-hysteresis cases live in 50-52 below.
# ===================================================================
PEER_COORD_SENTINEL="$WORKDIR/.watchdog-peer-coordination-escalated"
PEER_COORD_COOLDOWN_STATE="$WORKDIR/.watchdog-peer-coordination-cooldown"
start_alive_and_fresh 40

# ---- 40. First degraded tick ⇒ files EXACTLY ONE forge issue, self- ----
#          describing (embeds degraded_for/consecutive/threshold from
#          PeerCoordinationHealth so the alert needs no second `gh` round-trip
#          to be actionable).
ISSUE40="$WORKDIR/create-issue40.log"; : > "$ISSUE40"
BODY40="$WORKDIR/create-issue40-body.txt"; : > "$BODY40"
mkdir -p "$WORKDIR/.loom/scripts"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
title=""; labels=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --title|-t) title="\$2"; shift 2 ;;
    --label|-l) labels="\$labels \$2"; shift 2 ;;
    --body|-b)  printf '%s\n' "\$2" >> "$BODY40"; shift 2 ;;
    *) shift ;;
  esac
done
echo "create-issue title=\$title labels=\$labels" >> "$ISSUE40"
echo "https://example.invalid/repo/issues/4001"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"

STUB40="$(make_peer_coord_stub degraded 300 1 3 10 7)"
rm -f "$PEER_COORD_SENTINEL" "$PEER_COORD_COOLDOWN_STATE"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB40/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
assert_rc 0 "$RC" "#6222 peer-coordination degraded: liveness itself stays healthy (exit 0)"
if [[ "$(wc -l < "$ISSUE40" | tr -d ' ')" == "1" ]]; then
    pass "#6222 peer-coordination degraded: files exactly ONE forge issue"
else
    fail "#6222 peer-coordination degraded: expected one create-issue.sh call, got $(wc -l < "$ISSUE40") ($(cat "$ISSUE40"))"
fi
if grep -q 'loom:triage' "$ISSUE40"; then
    pass "#6222 peer-coordination degraded: the filed issue carries a triage label"
else
    fail "#6222 peer-coordination degraded: expected a labelled filing ($(cat "$ISSUE40"))"
fi
if grep -qi 'DEGRADED' "$BODY40" && grep -q '1/3' "$BODY40" && grep -q '300s' "$BODY40"; then
    pass "#6222 peer-coordination degraded: the issue body is self-describing (degraded-for + recovery progress)"
else
    fail "#6222 peer-coordination degraded: issue body should embed degraded_for/consecutive/threshold ($(cat "$BODY40"))"
fi
if [[ -f "$PEER_COORD_SENTINEL" ]]; then
    pass "#6222 peer-coordination degraded: a dedupe sentinel is written"
else
    fail "#6222 peer-coordination degraded: expected a dedupe sentinel at $PEER_COORD_SENTINEL"
fi
if log_hasi 'ESCALATED out-of-band' && log_hasi 'peer-claim coordination is DEGRADED'; then
    pass "#6222 peer-coordination degraded: the log records the escalation"
else
    fail "#6222 peer-coordination degraded: expected an ESCALATED out-of-band note ($(cat "$WDLOG"))"
fi

# ---- 41. Repeated degraded ticks do NOT re-file (dedup across ticks) ----
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB40/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
if [[ "$(wc -l < "$ISSUE40" | tr -d ' ')" == "1" ]]; then
    pass "#6222 peer-coordination degraded: a second degraded tick does NOT file a duplicate issue"
else
    fail "#6222 peer-coordination degraded: duplicate filing on the next tick ($(cat "$ISSUE40"))"
fi
if log_hasi 'already escalated out-of-band'; then
    pass "#6222 peer-coordination degraded: the repeat tick notes the degradation is already escalated"
else
    fail "#6222 peer-coordination degraded: expected an already-escalated note ($(cat "$WDLOG"))"
fi
rm -rf "$STUB40"

# ---- 42. Recovery: comments on + closes the EXACT filed issue, clears the ----
#          sentinel — the behavior #5391's own outage escalation never needed
#          (that episode still requires an operator to act; this one heals
#          itself the moment a later tick observes Green again).
GHLOG42="$WORKDIR/gh42.log"; : > "$GHLOG42"
GHSTUB42="$(mktemp -d)"
cat > "$GHSTUB42/gh" <<EOF
#!/usr/bin/env bash
echo "gh \$*" >> "$GHLOG42"
exit 0
EOF
chmod +x "$GHSTUB42/gh"
STUB42="$(make_peer_coord_stub green)"
: > "$WDLOG"
run_watchdog PATH="$GHSTUB42:$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB42/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
assert_rc 0 "$RC" "#6222 peer-coordination recovered: exits 0"
if grep -q 'issue comment https://example.invalid/repo/issues/4001' "$GHLOG42"; then
    pass "#6222 peer-coordination recovered: the tracking issue is commented on"
else
    fail "#6222 peer-coordination recovered: expected a gh issue comment call ($(cat "$GHLOG42"))"
fi
if grep -q 'issue close https://example.invalid/repo/issues/4001' "$GHLOG42"; then
    pass "#6222 peer-coordination recovered: the tracking issue is closed"
else
    fail "#6222 peer-coordination recovered: expected a gh issue close call ($(cat "$GHLOG42"))"
fi
if [[ -f "$PEER_COORD_SENTINEL" ]]; then
    fail "#6222 peer-coordination recovered: the escalation sentinel must be cleared"
else
    pass "#6222 peer-coordination recovered: the escalation sentinel is cleared"
fi
if log_hasi 'RECOVERED' && log_hasi 'cleared the escalation sentinel'; then
    pass "#6222 peer-coordination recovered: the log records the recovery"
else
    fail "#6222 peer-coordination recovered: expected a RECOVERED note ($(cat "$WDLOG"))"
fi
rm -rf "$STUB42" "$GHSTUB42"

# ---- 43. Best-effort: create-issue.sh fails (no forge auth / offline host) ----
#          degrades to a log line, never a failed tick (mirrors
#          escalate_daemon_outage()'s own best-effort contract, #5391).
#
#          Deliberately a FAILING stub, never a REMOVED file: both
#          escalate_daemon_outage() and escalate_peer_coordination_degraded()
#          fall back to "$_LOOM_WATCHDOG_CLI_DIR/../create-issue.sh" when
#          $repo_root has none — which resolves relative to wherever the
#          watchdog SCRIPT ITSELF lives on disk, not this test's sandboxed
#          $WORKDIR. Since this suite runs the watchdog from its real in-repo
#          path (see $WATCHDOG above), removing $WORKDIR/.loom/scripts/
#          create-issue.sh here would silently fall through to THIS REPO'S
#          OWN real defaults/scripts/create-issue.sh and file a genuine forge
#          issue — exactly what happened once during this test's own
#          development. Keeping the stub present (branch 1 of the resolution
#          order) but failing exercises the identical best-effort contract
#          without ever reaching that fallback.
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<'STUBEOF'
#!/usr/bin/env bash
echo "create-issue.sh: no forge auth available (simulated)" >&2
exit 1
STUBEOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
rm -f "$PEER_COORD_SENTINEL" "$PEER_COORD_COOLDOWN_STATE"
STUB43="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB43/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
assert_rc 0 "$RC" "#6222 peer-coordination degraded, create-issue.sh fails: liveness tick still exits 0 (best-effort, never fatal)"
if [[ -f "$PEER_COORD_SENTINEL" ]]; then
    fail "#6222 peer-coordination degraded, create-issue.sh fails: no sentinel should be written (nothing was filed)"
else
    pass "#6222 peer-coordination degraded, create-issue.sh fails: no sentinel written"
fi
if log_hasi 'NOT possible' && log_hasi 'THIS LOGFILE IS THE ONLY SIGNAL'; then
    pass "#6222 peer-coordination degraded, create-issue.sh fails: degrades to a log line, exactly like escalate_daemon_outage()"
else
    fail "#6222 peer-coordination degraded, create-issue.sh fails: expected a best-effort degrade note ($(cat "$WDLOG"))"
fi
rm -rf "$STUB43"

# ---- 44. An unparseable/unsupported `peer-claims` answer is "could not ----
#          determine", never a fabricated verdict and never a crash — the
#          same discipline every other best-effort probe in this file follows.
rm -f "$PEER_COORD_SENTINEL" "$PEER_COORD_COOLDOWN_STATE"
STUB44="$(make_peer_coord_stub unsupported)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB44/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
assert_rc 0 "$RC" "#6222 peer-claims unsupported by this binary: still exits 0 (not a hang, not a divergence)"
if [[ -f "$PEER_COORD_SENTINEL" ]]; then
    fail "#6222 peer-claims unsupported: no sentinel should be written (nothing observed)"
else
    pass "#6222 peer-claims unsupported: no sentinel written"
fi
if log_hasi 'peer-claim coordination is DEGRADED'; then
    fail "#6222 peer-claims unsupported: must not fabricate a DEGRADED report from an unparseable answer"
else
    pass "#6222 peer-claims unsupported: no fabricated DEGRADED report"
fi
rm -rf "$STUB44"

kill "$LIVE_PID" 2>/dev/null || true

# ---- 45. --help documents the #6222 peer-coordination alert knobs, plus ----
#          the #7258 cooldown knobs layered on top.
help_out_6222=$(bash "$WATCHDOG" --help 2>/dev/null)
if grep -q 'LOOM_WATCHDOG_PEER_COORD_CHECK' <<< "$help_out_6222" \
    && grep -q 'LOOM_WATCHDOG_PEER_COORD_SENTINEL' <<< "$help_out_6222"; then
    pass "--help documents the #6222 peer-coordination alert knobs"
else
    fail "--help missing the #6222 peer-coordination alert knob documentation"
fi
if grep -q 'LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS' <<< "$help_out_6222" \
    && grep -q 'LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE' <<< "$help_out_6222"; then
    pass "--help documents the #7258 peer-coordination cooldown knobs"
else
    fail "--help missing the #7258 peer-coordination cooldown knob documentation"
fi

# ---------------------------------------------------------------------------
# #6272 regression suite: both escalation functions' create-issue.sh
# resolution has a THIRD branch — "$_LOOM_WATCHDOG_CLI_DIR/../create-issue.sh"
# — that is NOT sandboxable via $repo_root: it resolves relative to wherever
# the watchdog SCRIPT ITSELF lives on disk. Because this suite invokes the
# watchdog from its real in-repo path, that branch, unguarded, resolves to
# THIS repo's own real, gh-authenticated defaults/scripts/create-issue.sh —
# which already filed one spurious live issue during this suite's own
# development (#6271, closed). LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR (added
# by #6272) is the seam that lets these cases redirect branch 3 to a sandbox
# location instead, so none of the tests below can ever reach the real path.
# ---------------------------------------------------------------------------

# ---- 46. #6272: branch 3 is never reached — proven by EXECUTION, not just ----
#          if/elif ordering — when a repo-scoped create-issue.sh exists, for
#          escalate_daemon_outage(). LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR
#          points at a "poison" script that records itself being called; if
#          branch 1's repo-scoped stub were not strictly preferred, the
#          poison script would fire and this test would catch it.
mkdir -p "$WORKDIR/.loom/scripts"
LOG46="$WORKDIR/recover46.log"; : > "$LOG46"
ISSUE46="$WORKDIR/create-issue46.log"; : > "$ISSUE46"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "repo-scoped create-issue.sh called" >> "$ISSUE46"
echo "https://example.invalid/issues/46"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
POISON46="$(mktemp -d)"
POISON_LOG46="$WORKDIR/poison46.log"; : > "$POISON_LOG46"
cat > "$POISON46/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "POISON: branch 3 fallback dir was reached despite a repo-scoped create-issue.sh existing" >> "$POISON_LOG46"
exit 1
EOF
chmod +x "$POISON46/create-issue.sh"
start_confirmed_down 46
REC46="$(make_recover_stub noop "$LOG46")"
rec46_env=$(recover_env "$REC46" LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS=1 \
    LOOM_WATCHDOG_RECOVER_BACKOFF_SECS=0 LOOM_WATCHDOG_ESCALATE=1 \
    LOOM_WATCHDOG_ESCALATION_SENTINEL="$OUTAGE_SENTINEL" \
    LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR="$POISON46")
# shellcheck disable=SC2086,SC2046
run_watchdog $rec46_env; rc46=$RC
assert_rc 1 "$rc46" "#6272 branch-3 skipped when repo-scoped exists: the breaker-trip tick still exits 1"
if [[ -s "$POISON_LOG46" ]]; then
    fail "#6272 branch-3 skipped when repo-scoped exists: the fallback-dir script was invoked ($(cat "$POISON_LOG46")) even though a repo-scoped create-issue.sh existed"
else
    pass "#6272 branch-3 skipped when repo-scoped exists: the fallback-dir script was never invoked"
fi
if [[ "$(wc -l < "$ISSUE46" | tr -d ' ')" == "1" ]]; then
    pass "#6272 branch-3 skipped when repo-scoped exists: the repo-scoped create-issue.sh was used instead"
else
    fail "#6272 branch-3 skipped when repo-scoped exists: expected the repo-scoped stub to be called exactly once ($(cat "$ISSUE46"))"
fi
rm -rf "$DOWN_STUB" "$POISON46" "$(dirname "$REC46")"

# ---- 47. #6272: identical branch-3-skipped proof for ----
#          escalate_peer_coordination_degraded() (#6222) — same landmine,
#          same fix, shared verbatim by both escalation functions.
rm -f "$PEER_COORD_SENTINEL" "$PEER_COORD_COOLDOWN_STATE"
ISSUE47="$WORKDIR/create-issue47.log"; : > "$ISSUE47"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "repo-scoped create-issue.sh called" >> "$ISSUE47"
echo "https://example.invalid/issues/47"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
POISON47="$(mktemp -d)"
POISON_LOG47="$WORKDIR/poison47.log"; : > "$POISON_LOG47"
cat > "$POISON47/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "POISON: branch 3 fallback dir was reached despite a repo-scoped create-issue.sh existing" >> "$POISON_LOG47"
exit 1
EOF
chmod +x "$POISON47/create-issue.sh"
STUB47="$(make_peer_coord_stub degraded)"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB47/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE" \
    LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR="$POISON47"
assert_rc 0 "$RC" "#6272 branch-3 skipped (#6222 path): liveness itself stays healthy (exit 0)"
if [[ -s "$POISON_LOG47" ]]; then
    fail "#6272 branch-3 skipped (#6222 path): the fallback-dir script was invoked ($(cat "$POISON_LOG47")) even though a repo-scoped create-issue.sh existed"
else
    pass "#6272 branch-3 skipped (#6222 path): the fallback-dir script was never invoked"
fi
if [[ "$(wc -l < "$ISSUE47" | tr -d ' ')" == "1" ]]; then
    pass "#6272 branch-3 skipped (#6222 path): the repo-scoped create-issue.sh was used instead"
else
    fail "#6272 branch-3 skipped (#6222 path): expected the repo-scoped stub to be called exactly once ($(cat "$ISSUE47"))"
fi
rm -rf "$STUB47" "$POISON47"

# ---- 48. #6272: the original landmine, closed — with NO repo-scoped ----
#          create-issue.sh anywhere and LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR
#          redirected to an empty sandbox dir, escalate_daemon_outage()
#          degrades to a best-effort log line instead of falling through to
#          $_LOOM_WATCHDOG_CLI_DIR/../create-issue.sh — this repo's own real,
#          gh-authenticated script (the exact fallthrough that filed a
#          spurious live issue, #6271). This is the scenario a test could not
#          previously simulate safely.
rm -f "$WORKDIR/.loom/scripts/create-issue.sh"
EMPTY48="$(mktemp -d)"
LOG48="$WORKDIR/recover48.log"; : > "$LOG48"
start_confirmed_down 48
REC48="$(make_recover_stub noop "$LOG48")"
rec48_env=$(recover_env "$REC48" LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS=1 \
    LOOM_WATCHDOG_RECOVER_BACKOFF_SECS=0 LOOM_WATCHDOG_ESCALATE=1 \
    LOOM_WATCHDOG_ESCALATION_SENTINEL="$OUTAGE_SENTINEL" \
    LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR="$EMPTY48")
# shellcheck disable=SC2086,SC2046
run_watchdog $rec48_env; rc48=$RC
assert_rc 1 "$rc48" "#6272 landmine closed: the breaker-trip tick still exits 1"
if [[ -f "$OUTAGE_SENTINEL" ]]; then
    fail "#6272 landmine closed: no sentinel should exist — nothing could have been filed with no create-issue.sh reachable anywhere"
else
    pass "#6272 landmine closed: no sentinel written — no create-issue.sh reachable anywhere"
fi
if log_hasi 'THIS LOGFILE IS THE ONLY SIGNAL'; then
    pass "#6272 landmine closed: degrades to the standard best-effort log line"
else
    fail "#6272 landmine closed: expected a best-effort degrade note ($(cat "$WDLOG"))"
fi
rm -rf "$DOWN_STUB" "$EMPTY48" "$(dirname "$REC48")"

# ---- 49. #6272: identical landmine-closed proof for ----
#          escalate_peer_coordination_degraded() (#6222).
rm -f "$WORKDIR/.loom/scripts/create-issue.sh" "$PEER_COORD_SENTINEL" "$PEER_COORD_COOLDOWN_STATE"
EMPTY49="$(mktemp -d)"
STUB49="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB49/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE" \
    LOOM_WATCHDOG_CREATE_ISSUE_FALLBACK_DIR="$EMPTY49"
assert_rc 0 "$RC" "#6272 landmine closed (#6222 path): liveness itself stays healthy (exit 0)"
if [[ -f "$PEER_COORD_SENTINEL" ]]; then
    fail "#6272 landmine closed (#6222 path): no sentinel should exist — nothing could have been filed"
else
    pass "#6272 landmine closed (#6222 path): no sentinel written"
fi
if log_hasi 'NOT possible' && log_hasi 'THIS LOGFILE IS THE ONLY SIGNAL'; then
    pass "#6272 landmine closed (#6222 path): degrades to the standard best-effort log line"
else
    fail "#6272 landmine closed (#6222 path): expected a best-effort degrade note ($(cat "$WDLOG"))"
fi
rm -rf "$STUB49" "$EMPTY49"

# ===================================================================
# 50-53. #7258: post-recovery cooldown/hysteresis for the #6222 peer-
#         coordination alert — a host flapping degraded/healthy every 1-2
#         hours must not file a brand-new tracking issue on every flap. All
#         cases reuse make_peer_coord_stub/PS_STUB_DIR exactly like 46-49
#         above: the #4398 in-band IPC probe alone establishes liveness, so
#         these don't need a live LIVE_PID (already reaped after case 44).
# ===================================================================

# ---- 50. A repeat degradation shortly after a clean recovery is SUPPRESSED ----
#          (no new tracking issue) — the cooldown window's whole reason to
#          exist. Drives a full degrade -> recover -> degrade cycle so the
#          suppression is exercised via the real clear_peer_coordination_
#          escalation() cooldown-stamp write, not a hand-crafted fixture.
rm -f "$PEER_COORD_SENTINEL" "$PEER_COORD_COOLDOWN_STATE"
ISSUE50="$WORKDIR/create-issue50.log"; : > "$ISSUE50"
mkdir -p "$WORKDIR/.loom/scripts"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "create-issue.sh called" >> "$ISSUE50"
echo "https://example.invalid/repo/issues/5001"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"

# 50a. First degraded tick: files the initial tracking issue as usual.
STUB50="$(make_peer_coord_stub degraded)"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB50/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
if [[ "$(wc -l < "$ISSUE50" | tr -d ' ')" == "1" ]]; then
    pass "#7258 cooldown: the initial degraded tick still files a tracking issue"
else
    fail "#7258 cooldown: expected exactly one initial filing ($(cat "$ISSUE50"))"
fi

# 50b. Recovery: comments on + closes the issue, clears the sentinel, and
#      (the behavior under test) stamps the cooldown-state file.
GHSTUB50="$(mktemp -d)"
cat > "$GHSTUB50/gh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$GHSTUB50/gh"
STUB50G="$(make_peer_coord_stub green)"
run_watchdog PATH="$GHSTUB50:$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB50G/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
if [[ -f "$PEER_COORD_COOLDOWN_STATE" ]]; then
    pass "#7258 cooldown: a clean recovery stamps the cooldown-state file"
else
    fail "#7258 cooldown: expected recovery to write $PEER_COORD_COOLDOWN_STATE"
fi
rm -rf "$STUB50G" "$GHSTUB50"

# 50c. A repeat degraded tick immediately afterward (well within the default
#      6h cooldown) must NOT file a second issue.
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB50/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
assert_rc 0 "$RC" "#7258 cooldown: a suppressed repeat degradation still exits 0 (liveness itself is healthy)"
if [[ "$(wc -l < "$ISSUE50" | tr -d ' ')" == "1" ]]; then
    pass "#7258 cooldown: a repeat degradation within the cooldown window does NOT file a new issue"
else
    fail "#7258 cooldown: expected the cooldown to suppress a second filing ($(cat "$ISSUE50"))"
fi
if [[ -f "$PEER_COORD_SENTINEL" ]]; then
    fail "#7258 cooldown: a suppressed escalation must not write a fresh sentinel"
else
    pass "#7258 cooldown: no sentinel is written for a suppressed escalation"
fi
if log_hasi 'cooldown window' && log_hasi 'DEGRADED'; then
    pass "#7258 cooldown: the log records the cooldown suppression"
else
    fail "#7258 cooldown: expected a cooldown-suppression note ($(cat "$WDLOG"))"
fi
rm -rf "$STUB50"

# ---- 51. Once the cooldown window has elapsed, a repeat degradation files ----
#          fresh again, exactly like today. Simulated by backdating the
#          cooldown-state timestamp well past the default window rather than
#          actually sleeping for hours.
rm -f "$PEER_COORD_SENTINEL"
ISSUE51="$WORKDIR/create-issue51.log"; : > "$ISSUE51"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "create-issue.sh called" >> "$ISSUE51"
echo "https://example.invalid/repo/issues/5101"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
echo "$(( $(date -u +%s) - 100000 ))" > "$PEER_COORD_COOLDOWN_STATE"   # ~27.8h ago > default 6h
STUB51="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB51/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
if [[ "$(wc -l < "$ISSUE51" | tr -d ' ')" == "1" ]]; then
    pass "#7258 cooldown: a repeat degradation AFTER the cooldown elapses files fresh again"
else
    fail "#7258 cooldown: expected a fresh filing once the cooldown elapsed ($(cat "$ISSUE51"))"
fi
if [[ -f "$PEER_COORD_SENTINEL" ]]; then
    pass "#7258 cooldown: the fresh filing writes its own dedupe sentinel"
else
    fail "#7258 cooldown: expected a fresh sentinel to be written"
fi
if log_hasi 'ESCALATED out-of-band'; then
    pass "#7258 cooldown: the log records the fresh escalation, not a suppression"
else
    fail "#7258 cooldown: expected an ESCALATED note, not a suppression ($(cat "$WDLOG"))"
fi
rm -rf "$STUB51"

# ---- 52. A corrupt (non-numeric) cooldown-state file FAILS OPEN: treated as ----
#          no cooldown in effect, not an error and not a reason to suppress a
#          genuine degradation.
rm -f "$PEER_COORD_SENTINEL"
ISSUE52="$WORKDIR/create-issue52.log"; : > "$ISSUE52"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "create-issue.sh called" >> "$ISSUE52"
echo "https://example.invalid/repo/issues/5201"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
printf 'not-a-timestamp\n' > "$PEER_COORD_COOLDOWN_STATE"
STUB52="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB52/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE"
if [[ "$(wc -l < "$ISSUE52" | tr -d ' ')" == "1" ]]; then
    pass "#7258 cooldown: a corrupt cooldown-state file fails open (files fresh, not an error)"
else
    fail "#7258 cooldown: a corrupt cooldown-state file should fail open, not suppress ($(cat "$ISSUE52"))"
fi
rm -rf "$STUB52"

# ---- 53. Boundary: an excursion landing exactly AT the cooldown window ----
#          (elapsed == LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS) is treated as
#          cooldown-ELAPSED (strict `<` comparison) and files fresh — proves
#          the documented boundary choice by execution, not just comment.
rm -f "$PEER_COORD_SENTINEL"
ISSUE53="$WORKDIR/create-issue53.log"; : > "$ISSUE53"
cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "create-issue.sh called" >> "$ISSUE53"
echo "https://example.invalid/repo/issues/5301"
exit 0
EOF
chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
echo "$(( $(date -u +%s) - 100 ))" > "$PEER_COORD_COOLDOWN_STATE"   # exactly 100s ago
STUB53="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" LOOM_WATCHDOG_IPC_PROBE=1 LOOM_DAEMON_BIN="$STUB53/loom-daemon-mock" \
    LOOM_WATCHDOG_ESCALATE=1 LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS=100
if [[ "$(wc -l < "$ISSUE53" | tr -d ' ')" == "1" ]]; then
    pass "#7258 cooldown: elapsed == cooldown window is treated as elapsed (strict <), files fresh"
else
    fail "#7258 cooldown: expected the exact-boundary tick to escalate fresh ($(cat "$ISSUE53"))"
fi
rm -rf "$STUB53"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]