#!/usr/bin/env bash
# test-loom-daemon-watchdog-dedup.sh — Tests for the #7664 peer-coordination
# same-issue dedup window (lib/watchdog-peer-coord-dedup.sh, sourced by
# cli/loom-daemon-watchdog.sh), layered on top of the #7258 cooldown.
#
# Sibling of test-loom-daemon-watchdog.sh, split out under the File Size
# Ratchet (#7711): that suite is frozen at its baseline size, so the #7664
# cases live here instead of growing it. The fixtures below (run_watchdog,
# make_peer_coord_stub, start_alive_and_fresh, ...) are a deliberately minimal
# copy of the main suite's — same env pins, same stub shapes — kept inline so
# this file runs standalone; unifying both suites behind one shared harness is
# the structural follow-up tracked in #7826, not this file's job.
#
# Every case pins LOOM_WATCHDOG_ESCALATE=1 plus fresh PEER_COORD_SENTINEL /
# PEER_COORD_COOLDOWN_STATE paths inside the tempdir, exactly as the main
# suite's cases 40-53 do, so no case can dedupe against another's leftover
# state and nothing ever touches ~/.loom or a real forge. `gh` is stubbed via a
# PATH-prepended directory; create-issue.sh is a recorder stub resolved through
# the marker's repo_root.
#
# Style matches the other daemon lifecycle tests — plain bash, hand-rolled
# assertions. Bats is NOT used in this repository.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-watchdog.sh"

# Background-PID bookkeeping (#4773): the sleeper() below is tracked so the
# EXIT/INT/TERM trap reaps it even if this suite is interrupted.
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

WORKDIR="$(mktemp -d)"
# Same combined trap shape as the main suite: bg_proc_reap kills the tracked
# sleeper; the explicit `exit 1` on INT/TERM is required because a bare trap
# does not stop the script on those signals.
trap 'bg_proc_reap; rm -rf "$WORKDIR"' EXIT
trap 'bg_proc_reap; rm -rf "$WORKDIR"; exit 1' INT TERM

MARKER="$WORKDIR/autonomy-desired"
HEARTBEAT="$WORKDIR/daemon.heartbeat"
WDLOG="$WORKDIR/watchdog.log"      # the watchdog's own report log (LOOM_WATCHDOG_LOG)
OUT="$WORKDIR/out.txt"             # captured stdout+stderr of each run

# Non-launchd (pid-file) daemon marker so liveness is probed via the pid file.
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

# Run the watchdog on the pid-file path with the main suite's hermetic env pins
# (see test-loom-daemon-watchdog.sh's run_watchdog for the rationale behind
# each: #4331 socket path, #4398 probe default, #5118 pid-file tiers, #5391
# recover/escalate defaults). Extra KEY=VAL args are placed AFTER the defaults
# so a case can re-enable the probe / escalation. These cases assert on the
# watchdog's side effects (stub logs, sentinel, state file, its own log), not
# its exit code, so no RC is captured.
run_watchdog() {
    : > "$OUT"
    env LOOM_WATCHDOG_IPC_PROBE=0 LOOM_PID_FILE= LOOM_WORKSPACE= LOOM_MACHINE_CHECKOUT= \
        LOOM_WATCHDOG_AUTO_RECOVER=0 LOOM_WATCHDOG_ESCALATE=0 \
        LOOM_WATCHDOG_RECOVERY_STATE="$WORKDIR/.watchdog-recovery-state" \
        "$@" LOOM_AUTONOMY_MARKER="$MARKER" LOOM_WATCHDOG_LOG="$WDLOG" \
        LOOM_SOCKET_PATH="$WORKDIR/loom-daemon.sock" \
        LOOM_DAEMON_LAUNCHD=0 bash "$WATCHDOG" > "$OUT" 2>&1
}

log_hasi() { grep -qi "$1" "$WDLOG" 2>/dev/null; }

# A live pid we own for the "daemon alive" state (stdio detached so the
# `$(sleeper)` substitution does not block on the pipe).
sleeper() { sleep 60 >/dev/null 2>&1 & echo $!; }

# A `ps` stub reporting a fixed `-o etime=` so the sleeper's age is pinned
# well past the startup grace window.
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

# Peer-coordination daemon stub (#6222): answers `quarantine list` healthily
# (so the peer-coordination gate is reached) and `peer-claims --json` with the
# requested coordination state. JSON shape mirrors
# loom_daemon::types::PeerClaimStatus. Only the two states these cases need.
make_peer_coord_stub() { # <state: green|degraded>
    local state="$1" dir
    dir="$(mktemp -d)"
    case "$state" in
        green)
            printf '{"self_host":"test-host","ttl_secs":300,"entries":[],"advertised":10,"received":10,"expired":0,"dispatch_skipped":0,"coordination":{"degraded":false,"degraded_for_secs":null,"consecutive_receives_toward_recovery":0,"recovery_threshold":3}}' \
                > "$dir/peer-claims.json"
            ;;
        degraded)
            printf '{"self_host":"test-host","ttl_secs":300,"entries":[],"advertised":10,"received":10,"expired":0,"dispatch_skipped":0,"coordination":{"degraded":true,"degraded_for_secs":120,"consecutive_receives_toward_recovery":1,"recovery_threshold":3}}' \
                > "$dir/peer-claims.json"
            ;;
        *) echo "unknown peer-coord stub state $state" >&2; return 1 ;;
    esac
    # shellcheck disable=SC2016  # the $1 is the STUB's own positional, not ours
    printf '#!/usr/bin/env bash\nif [[ "$1" == "peer-claims" ]]; then\n  cat "%s/peer-claims.json"\n  exit 0\nfi\necho "no active quarantines"\nexit 0\n' \
        "$dir" > "$dir/loom-daemon-mock"
    chmod +x "$dir/loom-daemon-mock"
    echo "$dir"
}

# A recorder create-issue.sh at the marker's repo_root: appends one line per
# call to <log> and prints <issue_url>.
make_create_issue_stub() { # <log> <issue_url>
    mkdir -p "$WORKDIR/.loom/scripts"
    cat > "$WORKDIR/.loom/scripts/create-issue.sh" <<EOF
#!/usr/bin/env bash
echo "create-issue.sh called" >> "$1"
echo "$2"
exit 0
EOF
    chmod +x "$WORKDIR/.loom/scripts/create-issue.sh"
}

# A `gh` stub: records every invocation to <log> (when given) and exits <rc>.
make_gh_stub() { # [log] [rc]
    local log="${1:-}" rc="${2:-0}" dir
    dir="$(mktemp -d)"
    if [[ -n "$log" ]]; then
        printf '#!/usr/bin/env bash\necho "gh $*" >> "%s"\nexit %s\n' "$log" "$rc" > "$dir/gh"
    else
        printf '#!/usr/bin/env bash\nexit %s\n' "$rc" > "$dir/gh"
    fi
    chmod +x "$dir/gh"
    echo "$dir"
}

# Stand up "daemon alive (past the startup grace) + heartbeat FRESH" — the
# state in which only the in-band probe (and, past it, the peer-coordination
# gate) decides the verdict. Sets the globals LIVE_PID and PS_STUB_DIR.
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
    rm -f "$WORKDIR/.watchdog-probe-fail-count" "$WORKDIR/.watchdog-probe-window"
}

PEER_COORD_SENTINEL="$WORKDIR/.watchdog-peer-coordination-escalated"
PEER_COORD_COOLDOWN_STATE="$WORKDIR/.watchdog-peer-coordination-cooldown"
start_alive_and_fresh dedup

# Every case below: cooldown pinned to 100s (so a state line 300s old is
# cooldown-ELAPSED), the dedup window left at its 86400s default unless the
# case is about the window boundary itself.
peer_env=(LOOM_WATCHDOG_IPC_PROBE=1 LOOM_WATCHDOG_ESCALATE=1
    LOOM_WATCHDOG_PEER_COORD_SENTINEL="$PEER_COORD_SENTINEL"
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_STATE="$PEER_COORD_COOLDOWN_STATE")

# ===================================================================
# 1-6. #7664: dedup window layered on top of the #7258 cooldown — a repeat
#      degradation landing AFTER the cooldown elapses but still inside the
#      (longer) LOOM_WATCHDOG_PEER_COORD_DEDUP_WINDOW_SECS comments on (and
#      reopens) the SAME tracking issue instead of filing a fresh one, and
#      carries a running flap count in PEER_COORD_COOLDOWN_STATE. Fixes the
#      chronically-flapping-host shape from anvil#1270, where the #7258
#      cooldown alone still refiled once per cooldown window forever.
# ===================================================================

# ---- 1. A repeat degradation AFTER the cooldown has elapsed, but still ----
#         within the dedup window, COMMENTS ON + REOPENS the prior tracking
#         issue instead of filing a new one, and bumps the flap count.
rm -f "$PEER_COORD_SENTINEL"
ISSUE1="$WORKDIR/create-issue1.log"; : > "$ISSUE1"
make_create_issue_stub "$ISSUE1" "https://example.invalid/repo/issues/5401"
GHLOG1="$WORKDIR/gh1.log"; : > "$GHLOG1"
GHSTUB1="$(make_gh_stub "$GHLOG1")"
# 300s past a 100s cooldown -- comfortably inside the default 86400s dedup
# window -- with a hand-crafted <ts> <issue-ref> <flap-count> state line
# standing in for what a real recovery would have written.
echo "$(( $(date -u +%s) - 300 )) https://example.invalid/repo/issues/6001 1" > "$PEER_COORD_COOLDOWN_STATE"
STUB1="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$GHSTUB1:$PS_STUB_DIR:$PATH" "${peer_env[@]}" LOOM_DAEMON_BIN="$STUB1/loom-daemon-mock" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS=100
if [[ "$(wc -l < "$ISSUE1" | tr -d ' ')" == "0" ]]; then
    pass "#7664 dedup window: a repeat excursion inside the dedup window does NOT file a new issue"
else
    fail "#7664 dedup window: expected create-issue.sh to stay unused ($(cat "$ISSUE1"))"
fi
if grep -q 'issue comment https://example.invalid/repo/issues/6001' "$GHLOG1"; then
    pass "#7664 dedup window: comments on the prior tracking issue instead"
else
    fail "#7664 dedup window: expected a gh issue comment on the prior issue ($(cat "$GHLOG1"))"
fi
if grep -q 'issue reopen https://example.invalid/repo/issues/6001' "$GHLOG1"; then
    pass "#7664 dedup window: reopens the prior tracking issue"
else
    fail "#7664 dedup window: expected a gh issue reopen call ($(cat "$GHLOG1"))"
fi
if [[ -f "$PEER_COORD_SENTINEL" ]] && grep -q 'https://example.invalid/repo/issues/6001' "$PEER_COORD_SENTINEL"; then
    pass "#7664 dedup window: the sentinel is re-armed against the SAME issue"
else
    fail "#7664 dedup window: expected the sentinel to reference the reopened issue ($(cat "$PEER_COORD_SENTINEL" 2>/dev/null))"
fi
if [[ -f "$PEER_COORD_COOLDOWN_STATE" ]] && grep -q ' https://example\.invalid/repo/issues/6001 2$' "$PEER_COORD_COOLDOWN_STATE"; then
    pass "#7664 dedup window: the flap count is bumped to 2 in the cooldown-state file"
else
    fail "#7664 dedup window: expected flap count 2 in cooldown-state ($(cat "$PEER_COORD_COOLDOWN_STATE" 2>/dev/null))"
fi
if grep -q 'flap #2' "$GHLOG1"; then
    pass "#7664 dedup window: the comment body names the flap count"
else
    fail "#7664 dedup window: expected the comment body to name flap #2 ($(cat "$GHLOG1"))"
fi
if log_hasi 'Repeat flap #2' && log_hasi 'dedup window'; then
    pass "#7664 dedup window: the watchdog log records the dedup escalation"
else
    fail "#7664 dedup window: expected a dedup-window log note ($(cat "$WDLOG"))"
fi
if grep -qi 'advertised' "$GHLOG1" && grep -qi 'anvil#1270' "$GHLOG1"; then
    pass "#7664 dedup window: the comment names the anvil#1270 advertised/dispatch-time hypothesis"
else
    fail "#7664 dedup window: expected the anvil#1270 hypothesis in the comment ($(cat "$GHLOG1"))"
fi

# ---- 2. The flap count keeps incrementing across a full flap -> recover -> ----
#         flap cycle (Ask #3: surface the flap count across the window, not
#         just a single dedup comment).
GHSTUB2="$(make_gh_stub)"
STUB2G="$(make_peer_coord_stub green)"
run_watchdog PATH="$GHSTUB2:$PS_STUB_DIR:$PATH" "${peer_env[@]}" LOOM_DAEMON_BIN="$STUB2G/loom-daemon-mock"
rm -rf "$STUB2G" "$GHSTUB2"
if [[ -f "$PEER_COORD_COOLDOWN_STATE" ]] && grep -q ' https://example\.invalid/repo/issues/6001 2$' "$PEER_COORD_COOLDOWN_STATE"; then
    pass "#7664 dedup window: a recovery preserves the running flap count (still 2) while refreshing the timestamp"
else
    fail "#7664 dedup window: expected the recovery to carry the flap count forward ($(cat "$PEER_COORD_COOLDOWN_STATE" 2>/dev/null))"
fi

# Backdate the freshly-stamped state past the (test-scoped) cooldown again,
# simulating the SAME host flapping a second time.
echo "$(( $(date -u +%s) - 300 )) https://example.invalid/repo/issues/6001 2" > "$PEER_COORD_COOLDOWN_STATE"
GHLOG2B="$WORKDIR/gh2b.log"; : > "$GHLOG2B"
GHSTUB2B="$(make_gh_stub "$GHLOG2B")"
STUB2B="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$GHSTUB2B:$PS_STUB_DIR:$PATH" "${peer_env[@]}" LOOM_DAEMON_BIN="$STUB2B/loom-daemon-mock" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS=100
if grep -q 'flap #3' "$GHLOG2B" && log_hasi 'Repeat flap #3'; then
    pass "#7664 dedup window: a third excursion in the same window reports flap #3"
else
    fail "#7664 dedup window: expected flap #3 on the second dedup cycle ($(cat "$GHLOG2B"); log: $(cat "$WDLOG"))"
fi
rm -rf "$STUB1" "$STUB2B" "$GHSTUB1" "$GHSTUB2B"

# ---- 3. Once the DEDUP WINDOW itself elapses, a repeat excursion files ----
#         fresh again and the flap count resets — the window is anchored to
#         the last recovery, not open-ended.
rm -f "$PEER_COORD_SENTINEL"
ISSUE3="$WORKDIR/create-issue3.log"; : > "$ISSUE3"
make_create_issue_stub "$ISSUE3" "https://example.invalid/repo/issues/5601"
echo "$(( $(date -u +%s) - 100000 )) https://example.invalid/repo/issues/6001 5" > "$PEER_COORD_COOLDOWN_STATE"   # ~27.8h ago > default 24h dedup window
STUB3="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" "${peer_env[@]}" LOOM_DAEMON_BIN="$STUB3/loom-daemon-mock"
if [[ "$(wc -l < "$ISSUE3" | tr -d ' ')" == "1" ]]; then
    pass "#7664 dedup window: once the dedup window elapses, a repeat excursion files fresh again"
else
    fail "#7664 dedup window: expected a fresh filing once the dedup window elapsed ($(cat "$ISSUE3"))"
fi
if [[ -f "$PEER_COORD_SENTINEL" ]] && grep -q 'https://example.invalid/repo/issues/5601' "$PEER_COORD_SENTINEL"; then
    pass "#7664 dedup window: the fresh filing writes its own sentinel against the NEW issue"
else
    fail "#7664 dedup window: expected a fresh sentinel referencing the new issue ($(cat "$PEER_COORD_SENTINEL" 2>/dev/null))"
fi
rm -rf "$STUB3"

# ---- 4. Boundary: an excursion landing exactly AT the dedup window ----
#         (elapsed == LOOM_WATCHDOG_PEER_COORD_DEDUP_WINDOW_SECS) is treated
#         as window-ELAPSED (strict `<`), mirroring the #7258 cooldown's own
#         boundary convention, and files fresh.
rm -f "$PEER_COORD_SENTINEL"
ISSUE4="$WORKDIR/create-issue4.log"; : > "$ISSUE4"
make_create_issue_stub "$ISSUE4" "https://example.invalid/repo/issues/5701"
echo "$(( $(date -u +%s) - 500 )) https://example.invalid/repo/issues/6001 3" > "$PEER_COORD_COOLDOWN_STATE"   # exactly 500s ago
STUB4="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" "${peer_env[@]}" LOOM_DAEMON_BIN="$STUB4/loom-daemon-mock" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS=100 \
    LOOM_WATCHDOG_PEER_COORD_DEDUP_WINDOW_SECS=500
if [[ "$(wc -l < "$ISSUE4" | tr -d ' ')" == "1" ]]; then
    pass "#7664 dedup window: elapsed == dedup window is treated as elapsed (strict <), files fresh"
else
    fail "#7664 dedup window: expected the exact-boundary tick to escalate fresh ($(cat "$ISSUE4"))"
fi
rm -rf "$STUB4"

# ---- 5. A missing/corrupt issue reference inside the dedup window FAILS ----
#         OPEN: no gh call is possible against a reference that is not
#         there, so a fresh issue is filed instead of silently dropping the
#         escalation. Distinct from the main suite's #52 (corrupt TIMESTAMP):
#         this drives an otherwise-valid, in-window timestamp with the
#         issue-ref field simply absent (an old bare-epoch #7258 state file,
#         pre-#7664).
rm -f "$PEER_COORD_SENTINEL"
ISSUE5="$WORKDIR/create-issue5.log"; : > "$ISSUE5"
make_create_issue_stub "$ISSUE5" "https://example.invalid/repo/issues/5801"
echo "$(( $(date -u +%s) - 300 ))" > "$PEER_COORD_COOLDOWN_STATE"   # bare epoch, no issue-ref field
STUB5="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$PS_STUB_DIR:$PATH" "${peer_env[@]}" LOOM_DAEMON_BIN="$STUB5/loom-daemon-mock" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS=100
if [[ "$(wc -l < "$ISSUE5" | tr -d ' ')" == "1" ]]; then
    pass "#7664 dedup window: a missing issue-ref field fails open (files fresh, not an error)"
else
    fail "#7664 dedup window: a missing issue-ref should fail open, not suppress ($(cat "$ISSUE5"))"
fi
rm -rf "$STUB5"

# ---- 6. A failed gh comment call (offline host / no forge auth) inside ----
#         the dedup window ALSO fails open to a fresh filing, rather than
#         silently dropping the escalation.
rm -f "$PEER_COORD_SENTINEL"
ISSUE6="$WORKDIR/create-issue6.log"; : > "$ISSUE6"
make_create_issue_stub "$ISSUE6" "https://example.invalid/repo/issues/5901"
GHSTUB6="$(make_gh_stub "" 1)"
echo "$(( $(date -u +%s) - 300 )) https://example.invalid/repo/issues/6001 1" > "$PEER_COORD_COOLDOWN_STATE"
STUB6="$(make_peer_coord_stub degraded)"
: > "$WDLOG"
run_watchdog PATH="$GHSTUB6:$PS_STUB_DIR:$PATH" "${peer_env[@]}" LOOM_DAEMON_BIN="$STUB6/loom-daemon-mock" \
    LOOM_WATCHDOG_PEER_COORD_COOLDOWN_SECS=100
if [[ "$(wc -l < "$ISSUE6" | tr -d ' ')" == "1" ]]; then
    pass "#7664 dedup window: a failed gh issue comment call fails open (files fresh)"
else
    fail "#7664 dedup window: a failed gh comment should fail open, not suppress ($(cat "$ISSUE6"))"
fi
if [[ -f "$PEER_COORD_SENTINEL" ]] && grep -q 'https://example.invalid/repo/issues/5901' "$PEER_COORD_SENTINEL"; then
    pass "#7664 dedup window: the fresh filing's sentinel references the NEW issue, not the unreachable one"
else
    fail "#7664 dedup window: expected the sentinel to reference the fresh issue ($(cat "$PEER_COORD_SENTINEL" 2>/dev/null))"
fi
rm -rf "$STUB6" "$GHSTUB6"

# ---- --help documents the #7664 dedup-window knob. ----
help_out_7664=$(bash "$WATCHDOG" --help 2>/dev/null)
if grep -q 'LOOM_WATCHDOG_PEER_COORD_DEDUP_WINDOW_SECS' <<< "$help_out_7664"; then
    pass "--help documents the #7664 peer-coordination dedup-window knob"
else
    fail "--help missing the #7664 peer-coordination dedup-window knob documentation"
fi

rm -rf "$PS_STUB_DIR"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
