#!/usr/bin/env bash
# test-loom-daemon-launchd-plist.sh — Tests for the launchd LaunchAgent plist
# rendering added by loom-daemon-start.sh / loom-daemon-stop.sh (#3972).
#
# Root cause under test: a plain `nohup "$DAEMON_BIN" &` leaves the daemon
# wired into the LAUNCHING SESSION's Mach bootstrap namespace, so when that
# session dies, gh/git start failing XPC lookups (trustd/opendirectoryd) for
# the daemon and every child it spawns. The fix loads the daemon as a
# `gui/<uid>` LaunchAgent instead.
#
# These tests exercise ONLY the plist-rendering path (`--print-plist`), which
# is pure string generation with NO side effects — it never calls `launchctl`
# and never touches ~/Library/LaunchAgents. This is deliberate: a test suite
# must never mutate the real machine's launchd state, on this dev box or any
# CI runner. Real `launchctl bootstrap`/`kickstart`/`bootout` behavior is not
# covered here (requires an actual GUI login session) and is validated
# manually per the daemon-reference.md Operability writeup.
#
# #6387 hardened both halves of that claim, after it turned out NOT to hold:
#   * the cases that drive the provisioning-capable code paths (tests 15-19)
#     do so through a STUB launchctl/systemctl on PATH, inside a fully
#     self-contained fixture repo — never the real tool, never the real
#     checkout;
#   * every case `cd`s into a fixture before invoking the script, so
#     find_repo_root($PWD) can never resolve the real checkout's real pid file
#     (the 2026-08-16 root cause);
#   * and a post-suite assertion (test 20) checks `launchctl list` /
#     `systemctl --user list-units` directly, because a job bootstrapped into
#     the real domain is invisible to a plist-file count taken in a scratch
#     $HOME and survives the fixture's own `rm -rf`.
#
# Style matches test-loom-daemon-start.sh — plain bash, hand-rolled
# assertions. Bats is NOT used in this repository.
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-launchd-plist.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-start.sh"

# #6387: strip the ambient LOOM_* pointers that a daemon-dispatched agent
# inherits (loom-daemon-start.sh exports LOOM_PID_FILE, and a spawned sweep's
# environment additionally carries LOOM_LAUNCHD_LABEL / LOOM_SOCKET_PATH /
# LOOM_AUTONOMY_MARKER) -- every one of them points at the OPERATOR'S REAL
# daemon state. Inherited, they make this suite resolve real state in exactly
# the way this file's header forbids: the real pid file (the 2026-08-16
# incident), the real autonomy marker, and the real installed plist, whose env
# keys the #5344 dropped-env-key merge then carries into a --from-config
# preview (test 4 fails on any fleet host, passes on a clean CI runner). Every
# case that needs one of these sets it explicitly, so unsetting here is safe.
unset LOOM_LAUNCHD_LABEL LOOM_WATCHDOG_LABEL LOOM_PID_FILE LOOM_SOCKET_PATH \
    LOOM_AUTONOMY_MARKER LOOM_SYSTEMD_UNIT LOOM_SYSTEMD_FORCE

# #6568: the AGENT-SESSION keys are stripped here for the same reason as the
# pointers above -- this suite is routinely RUN BY a daemon-dispatched sweep,
# whose shell exports every one of them. Left inherited, they would make the
# cases below non-deterministic (the session-isolation guard fires on a
# dispatched run and not on a clean CI run, and the env-strip cases would pass
# vacuously on a clean runner where the keys were never present). Every case
# that needs them sets them EXPLICITLY, so unsetting here is what makes the
# suite mean the same thing in both environments.
unset LOOM_SWEEP_CLAIM_OWNED LOOM_SWEEP_CPU_BUDGET_CORES LOOM_SWEEP_NICED \
    LOOM_TERMINAL_ID LOOM_ROLE LOOM_RUNTIME LOOM_ALLOW_SESSION_DAEMON_START

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

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

# ---------- fixture ----------
WORKDIR="$(mktemp -d)"
# No bg_proc_track/bg_proc_reap here (#4773, unlike the other daemon-suite
# traps touched in that issue): $FAKE_BIN is only referenced via
# LOOM_DAEMON_BIN, never actually executed/backgrounded, so no daemon PID can
# leak. (#6387's live-pid-file block below owns exactly one `sleep`, which it
# reaps itself and folds into these traps for the window it is alive.)
# Still widened to INT/TERM (not just EXIT) so $WORKDIR itself is reclaimed on
# a hard interruption, matching the other suites' trap signal set. NOTE: a
# bare `trap CMD EXIT INT TERM` runs CMD on INT/TERM but does NOT stop the
# script (only an EXIT-trap firing auto-exits) -- the explicit `exit` below is
# required, else a SIGTERM'd suite would clean up once and then keep running
# every remaining test case.
trap 'rm -rf "$WORKDIR"' EXIT
trap 'rm -rf "$WORKDIR"; exit 1' INT TERM
mkdir -p "$WORKDIR/.loom/logs"

FAKE_BIN="$WORKDIR/fake-loom-daemon"
cat > "$FAKE_BIN" <<'EOF'
#!/usr/bin/env bash
echo "FAKE_DAEMON"
EOF
chmod +x "$FAKE_BIN"

# ---------- tests ----------

# 1. --print-plist is pure inspection: never writes to ~/Library/LaunchAgents
#    or invokes launchctl, regardless of host platform.
before_count=0
if [[ -d "$HOME/Library/LaunchAgents" ]]; then
    before_count=$(find "$HOME/Library/LaunchAgents" -maxdepth 1 -name 'com.rjwalters.loom-daemon*.plist' 2>/dev/null | wc -l | tr -d ' ')
fi
plist_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>&1 )
plist_rc=$?
after_count=0
if [[ -d "$HOME/Library/LaunchAgents" ]]; then
    after_count=$(find "$HOME/Library/LaunchAgents" -maxdepth 1 -name 'com.rjwalters.loom-daemon*.plist' 2>/dev/null | wc -l | tr -d ' ')
fi
assert_eq "0" "$plist_rc" "--print-plist exits 0"
assert_eq "$before_count" "$after_count" "--print-plist never writes to ~/Library/LaunchAgents"

# 2. Plain start: RunAtLoad true, KeepAlive:{SuccessfulExit:true} (#4054 —
#    relaunch only on a clean exit 0, i.e. the RestartDaemon primitive; a
#    crash/SIGTERM/SIGINT exits non-zero and is NOT respawned, preserving the
#    old no-crash-loop semantics), LOOM_DAEMON_SUPERVISOR=launchd baked in, both
#    autonomy loops OFF present and equal to 0 (FLAGS-OFF default, #3911
#    semantics preserved), PATH covers gh/git/cargo/python3 fallback dirs.
assert_contains "$plist_out" "<key>RunAtLoad</key>" "plist declares RunAtLoad"
assert_contains "$plist_out" $'<key>RunAtLoad</key>\n    <true/>' "RunAtLoad is true (mirrors the validated incident-fix plist; survives reboot/re-login)"
assert_contains "$plist_out" $'<key>KeepAlive</key>\n    <dict>' "KeepAlive is a dict (SuccessfulExit form, #4054)"
assert_contains "$plist_out" $'<key>SuccessfulExit</key>\n        <true/>' "KeepAlive.SuccessfulExit is true (relaunch only on the restart primitive's clean exit 0)"
assert_not_contains "$plist_out" $'<key>KeepAlive</key>\n    <false/>' "KeepAlive is no longer the bare <false/> form"
assert_contains "$plist_out" $'<key>LOOM_DAEMON_SUPERVISOR</key>\n        <string>launchd</string>' "plist bakes in LOOM_DAEMON_SUPERVISOR=launchd (daemon proves supervision before a restart, #4054)"
assert_contains "$plist_out" "<key>LOOM_WORK_FINDER</key>" "plain start forwards LOOM_WORK_FINDER"
assert_contains "$plist_out" $'<key>LOOM_WORK_FINDER</key>\n        <string>0</string>' "plain start: LOOM_WORK_FINDER=0 (FLAGS-OFF default)"
assert_contains "$plist_out" $'<key>LOOM_MAIN_HEALTH_GATE</key>\n        <string>0</string>' "plain start: LOOM_MAIN_HEALTH_GATE=0 (FLAGS-OFF default)"
assert_contains "$plist_out" "/.local/bin" "PATH includes ~/.local/bin fallback (loom-daemon's own provisioning dir, #3922)"
assert_contains "$plist_out" "/.cargo/bin" "PATH includes ~/.cargo/bin fallback (cargo)"
assert_contains "$plist_out" "/usr/bin" "PATH includes /usr/bin fallback (python3, git)"
assert_contains "$plist_out" "/opt/homebrew/bin" "PATH includes Homebrew fallback (gh, git)"
assert_contains "$plist_out" "$FAKE_BIN" "ProgramArguments points at the resolved daemon binary"
assert_contains "$plist_out" "com.rjwalters.loom-daemon" "default Label is com.rjwalters.loom-daemon"

# 3. --work-finder --print-plist -> LOOM_WORK_FINDER=1, health gate stays 0.
wf_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --work-finder --print-plist 2>&1 )
assert_contains "$wf_out" $'<key>LOOM_WORK_FINDER</key>\n        <string>1</string>' "--work-finder: plist forwards LOOM_WORK_FINDER=1"
assert_contains "$wf_out" $'<key>LOOM_MAIN_HEALTH_GATE</key>\n        <string>0</string>' "--work-finder: health gate stays 0"

# 4. --from-config --print-plist -> neither autonomy var is forced into the
#    plist at all (env not forced -- config drives inside the daemon process).
#    Runs under a scratch HOME (#6387): this is the suite's only assert_NOT_contains
#    on an env KEY, and the dropped-env-key merge (#5344) deliberately carries
#    keys forward from the plist installed at $HOME/Library/LaunchAgents/<default
#    label>.plist. On any host where a real com.rjwalters.loom-daemon is
#    installed -- i.e. every fleet host, never a CI runner -- that merge pulled
#    the LIVE daemon's LOOM_WORK_FINDER=1 into this preview and failed the
#    assertion for reasons that have nothing to do with --from-config.
FROMCONFIG_HOME="$WORKDIR/fakehome-fromconfig"
mkdir -p "$FROMCONFIG_HOME/Library/LaunchAgents"
fc_out=$( cd "$WORKDIR" && HOME="$FROMCONFIG_HOME" env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --print-plist 2>&1 )
assert_not_contains "$fc_out" "<key>LOOM_WORK_FINDER</key>" "--from-config: LOOM_WORK_FINDER NOT forced into the plist"
assert_not_contains "$fc_out" "<key>LOOM_MAIN_HEALTH_GATE</key>" "--from-config: LOOM_MAIN_HEALTH_GATE NOT forced into the plist"

# 5. An already-exported LOOM_WORK_FINDER=1 (operator override) is forwarded
#    verbatim even on a plain (non-flag) invocation.
exported_out=$( cd "$WORKDIR" && env -u LOOM_MAIN_HEALTH_GATE LOOM_WORK_FINDER=1 LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>&1 )
assert_contains "$exported_out" $'<key>LOOM_WORK_FINDER</key>\n        <string>1</string>' "already-exported LOOM_WORK_FINDER=1 wins and is forwarded"

# 6. LOOM_LAUNCHD_LABEL overrides the default label.
label_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_LAUNCHD_LABEL="com.example.custom" LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>&1 )
assert_contains "$label_out" "com.example.custom" "LOOM_LAUNCHD_LABEL overrides the default Label"
assert_not_contains "$label_out" "<string>com.rjwalters.loom-daemon</string>" "custom label replaces (not appends to) the default"

# 7. --print-plist never persists to .loom/.daemon.flags (it isn't a daemon
#    autonomy flag; loom-daemon-update.sh must never replay it).
rm -f "$WORKDIR/.loom/.daemon.flags"
( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --work-finder --print-plist >/dev/null 2>&1 )
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -f "$WORKDIR/.loom/.daemon.flags" ]] || ! grep -q -- '--print-plist' "$WORKDIR/.loom/.daemon.flags" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --print-plist is excluded from the persisted .daemon.flags file"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --print-plist is excluded from the persisted .daemon.flags file"
fi

# 8. --help documents --no-launchd and --print-plist.
help_out=$(bash "$START_SCRIPT" --help 2>/dev/null)
TESTS_RUN=$((TESTS_RUN + 1))
if echo "$help_out" | grep -q -- '--no-launchd' && echo "$help_out" | grep -q -- '--print-plist'; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --help documents --no-launchd and --print-plist"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --help documents --no-launchd and --print-plist"
fi

# 9. Domain resolution (#4130) does not leak into the DEFAULT rendered plist:
#    the launchd domain (gui/<uid> vs user/<uid>) is a LOAD-time concern, not a
#    plist field. render_launchd_plist is untouched by #4130, so a plain
#    --print-plist (no LOOM_LAUNCHD_DOMAIN pin) carries neither a gui/<uid> nor a
#    user/<uid> domain token — the GUI-path plist is byte-for-byte what it was.
default_plist=$( cd "$WORKDIR" && env -u LOOM_LAUNCHD_DOMAIN -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>/dev/null )
assert_not_contains "$default_plist" "gui/$(id -u)" "default plist carries no gui/<uid> domain token (#4130 — domain is load-time, GUI path unchanged)"
assert_not_contains "$default_plist" "user/$(id -u)" "default plist carries no user/<uid> domain token (#4130 — domain is load-time)"

# ---------- #4172: deterministic plist PATH ----------
# Root cause under test: the rendered plist's PATH used to be "$PATH:<canonical
# fallback>" -- the INVOKING SHELL's entire interactive PATH prefixed onto the
# fallback set -- so a re-render (e.g. `loom-daemon-update.sh --relaunch`)
# silently replaced whatever PATH the live plist carried with whoever's shell
# happened to run the roll. Tests 10-14 below cover the fix: a deterministic
# canonical-by-default PATH, an explicit full-override / extend-only escape
# hatch, and a diff-friendly drift check against a previously-installed plist.

# 10. Default (no LOOM_DAEMON_PATH / LOOM_DAEMON_PATH_EXTRA): the invoking
#     shell's PATH is NOT prefixed onto the rendered plist -- a marker dir
#     injected into PATH must not leak through, while the canonical fallback
#     set is still present (unchanged from before #4172).
MARKER_DIR="/tmp/loom-test-marker-4172-$$"
det_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_DAEMON_PATH -u LOOM_DAEMON_PATH_EXTRA \
    PATH="${MARKER_DIR}:${PATH}" LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>/dev/null )
assert_not_contains "$det_out" "$MARKER_DIR" "default plist PATH does NOT leak the invoking shell's PATH (#4172 — deterministic, not shell-derived)"
assert_contains "$det_out" "/.local/bin" "default plist PATH still includes the canonical ~/.local/bin fallback"
assert_contains "$det_out" "/opt/homebrew/bin" "default plist PATH still includes the canonical Homebrew fallback"

# 11. LOOM_DAEMON_PATH is a FULL override -- used verbatim, no canonical
#     fallback appended.
override_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_DAEMON_PATH_EXTRA \
    LOOM_DAEMON_PATH="/custom/override/bin:/custom/override/sbin" \
    LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>/dev/null )
assert_contains "$override_out" $'<key>PATH</key>\n        <string>/custom/override/bin:/custom/override/sbin</string>' "LOOM_DAEMON_PATH is used verbatim as the plist PATH"
assert_not_contains "$override_out" "/opt/homebrew/bin" "LOOM_DAEMON_PATH override does NOT append the canonical fallback"

# 12. LOOM_DAEMON_PATH_EXTRA prepends onto the canonical minimal PATH instead
#     of replacing it entirely.
extra_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_DAEMON_PATH \
    LOOM_DAEMON_PATH_EXTRA="/extra/project/bin" \
    LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>/dev/null )
assert_contains "$extra_out" "/extra/project/bin:" "LOOM_DAEMON_PATH_EXTRA is prepended onto the plist PATH"
assert_contains "$extra_out" "/opt/homebrew/bin" "LOOM_DAEMON_PATH_EXTRA still carries the canonical Homebrew fallback"

# 13. The chosen PATH is always logged (stderr) -- visible at every render,
#     not just on inspection.
log_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_DAEMON_PATH -u LOOM_DAEMON_PATH_EXTRA \
    LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_contains "$log_out" "Rendered plist PATH: canonical minimal PATH (deterministic default)" "default render logs its PATH choice to stderr"
log_override_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_DAEMON_PATH_EXTRA \
    LOOM_DAEMON_PATH="/custom/override/bin" LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_contains "$log_override_out" "Rendered plist PATH: full override via LOOM_DAEMON_PATH" "LOOM_DAEMON_PATH override logs its source to stderr"

# 14. --print-plist PATH-drift check: a change from a previously-installed
#     live plist is visible (diff-friendly), not silently swapped out.
#     Isolated via a scratch HOME so this never touches the operator's real
#     ~/Library/LaunchAgents.
#
#     #6387: these two cases MUST `cd "$WORKDIR"` like every other case above.
#     Without it, find_repo_root($PWD) resolved whatever checkout the suite
#     happened to be launched from -- so on 2026-08-16 they read the REAL
#     checkout's live pid file, hit the already-running guard, and (pre-fix)
#     bootstrapped two REAL watchdog launchd jobs named after the test labels
#     below. The scratch $HOME isolates ~/Library/LaunchAgents but never
#     isolated $PWD, and therefore never isolated the pid file / repo state.
NODRIFT_HOME="$WORKDIR/fakehome-nodrift"
NODRIFT_LABEL="com.rjwalters.loom-daemon-nodrift-test"
mkdir -p "$NODRIFT_HOME/Library/LaunchAgents"
( cd "$WORKDIR" && HOME="$NODRIFT_HOME" LOOM_LAUNCHD_LABEL="$NODRIFT_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    env -u LOOM_DAEMON_PATH -u LOOM_DAEMON_PATH_EXTRA -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$START_SCRIPT" --print-plist ) > "$NODRIFT_HOME/Library/LaunchAgents/${NODRIFT_LABEL}.plist" 2>/dev/null
nodrift_out=$( cd "$WORKDIR" && HOME="$NODRIFT_HOME" LOOM_LAUNCHD_LABEL="$NODRIFT_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    env -u LOOM_DAEMON_PATH -u LOOM_DAEMON_PATH_EXTRA -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_not_contains "$nodrift_out" "PATH DRIFT DETECTED" "no drift warning when the live plist's PATH already matches the freshly-rendered one"

DRIFT_HOME="$WORKDIR/fakehome-drift"
DRIFT_LABEL="com.rjwalters.loom-daemon-drift-test"
mkdir -p "$DRIFT_HOME/Library/LaunchAgents"
cat > "$DRIFT_HOME/Library/LaunchAgents/${DRIFT_LABEL}.plist" <<'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>PATH</key>
<string>/old/stale/shell/path:/usr/bin</string>
</dict></plist>
PLISTEOF
drift_out=$( cd "$WORKDIR" && HOME="$DRIFT_HOME" LOOM_LAUNCHD_LABEL="$DRIFT_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    env -u LOOM_DAEMON_PATH -u LOOM_DAEMON_PATH_EXTRA -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_contains "$drift_out" "PATH DRIFT DETECTED" "a PATH change from the live plist is flagged at --print-plist time"
assert_contains "$drift_out" "/old/stale/shell/path:/usr/bin" "drift warning shows the OLD (live) PATH value"
assert_contains "$drift_out" "- live:" "drift warning is diff-friendly (- live / + new lines)"
assert_contains "$drift_out" "+ new:" "drift warning is diff-friendly (- live / + new lines)"

# ---------- #6387: an inspection mode never provisions anything ----------
# Root cause under test: the already-running guard -- which calls
# heal_watchdog_provisioning_gap -> provision_watchdog_job_launchd ->
# `launchctl bootstrap` -- used to run BEFORE the --print-plist/--print-unit
# early exit. So on ANY host where a live pid file resolved (every fleet host,
# never a CI runner), `--print-plist` bootstrapped a REAL watchdog launchd job
# under whatever $LOOM_LAUNCHD_LABEL was set and never printed the plist at
# all. Observed 2026-08-16: two test-labelled watchdog jobs ran every 300s for
# ~11h writing into the operator's real ~/.loom/logs/daemon-watchdog.log.
#
# The fixture below arms EVERY gate the heal path needs (live pid file,
# autonomy-desired marker, a locatable watchdog script) and drives a STUB
# launchctl/systemctl on PATH, so a regression is caught as a RECORDED STUB
# CALL rather than by mutating this machine.
LIVE_HOME="$WORKDIR/fakehome-livepid"
LIVE_REPO="$WORKDIR/livepid-repo"
LIVE_STUB_BIN="$WORKDIR/livepid-bin"
LIVE_LABEL="com.rjwalters.loom-daemon-livepid-test"
LIVE_MARKER="$LIVE_HOME/autonomy-desired"
LIVE_CALL_LOG="$WORKDIR/livepid-supervisor-calls.log"
mkdir -p "$LIVE_HOME/Library/LaunchAgents" "$LIVE_HOME/logs" \
    "$LIVE_REPO/.loom/logs" "$LIVE_REPO/.loom/scripts/cli" "$LIVE_STUB_BIN"
# locate_watchdog_script must SUCCEED, else heal_watchdog_provisioning_gap
# bails early and these cases would pass vacuously.
printf '#!/bin/bash\nexit 0\n' > "$LIVE_REPO/.loom/scripts/cli/loom-daemon-watchdog.sh"
# Marker present == "a daemon is expected on this host" -- the gate that arms
# heal_watchdog_provisioning_gap.
printf 'work_finder=1\n' > "$LIVE_MARKER"
: > "$LIVE_CALL_LOG"
for _stub in launchctl systemctl; do
    cat > "$LIVE_STUB_BIN/$_stub" <<STUBEOF
#!/usr/bin/env bash
echo "$_stub \$*" >> "$LIVE_CALL_LOG"
exit 0
STUBEOF
    chmod +x "$LIVE_STUB_BIN/$_stub"
done
unset _stub
# A REAL live process, so the already-running guard's `kill -0` succeeds.
sleep 30 &
LIVE_PID=$!
echo "$LIVE_PID" > "$LIVE_REPO/.loom/.daemon.pid"
# Reap it (and $WORKDIR) even on a hard interruption -- this is the first
# background process this suite has ever owned, so the traps set at the top
# need widening for it.
trap 'kill '"$LIVE_PID"' 2>/dev/null; rm -rf "$WORKDIR"' EXIT
trap 'kill '"$LIVE_PID"' 2>/dev/null; rm -rf "$WORKDIR"; exit 1' INT TERM

live_run() {
    ( cd "$LIVE_REPO" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
        PATH="$LIVE_STUB_BIN:$PATH" HOME="$LIVE_HOME" \
        LOOM_SOCKET_PATH="$LIVE_HOME/loom-daemon.sock" \
        LOOM_AUTONOMY_MARKER="$LIVE_MARKER" \
        LOOM_LAUNCHD_LABEL="$LIVE_LABEL" \
        LOOM_DAEMON_BIN="$FAKE_BIN" \
        bash "$START_SCRIPT" "$@" 2>/dev/null )
}

# Same fixture, plus the two seams the PROVISIONING cases (18/19) need in order
# to reach a real provisioning branch on a Linux host. Deliberately NOT folded
# into live_run above: cases 15-17 assert that NOTHING provisions, and a seam
# whose only purpose is to arm a provisioning branch has no business being set
# while proving that.
#
#   LOOM_SYSTEMD_FORCE=1 — systemd-user.sh's own documented test seam. Without
#     it, is_linux_systemd() requires a REACHABLE `systemd --user` manager
#     (XDG_RUNTIME_DIR + a non-"offline" `is-system-running`), which a hermetic
#     CI container does not have — so heal_watchdog_provisioning_gap would take
#     its "no mechanism on this tier" escalation branch and make ZERO
#     supervisor calls, and cases 18/19 would assert against nothing. With the
#     seam, is_linux_systemd() reduces to `command -v systemctl`, which
#     resolves to the stub in $LIVE_STUB_BIN. On Darwin the seam is inert for
#     these cases: heal_use_launchd is decided (and wins) before heal_is_systemd
#     is even consulted, so the launchd branch is still what gets exercised.
#   LOOM_SYSTEMD_UNIT — names the systemd side after the SAME test label the
#     launchd side uses, so test 20's live-job scan covers both mechanisms with
#     one substring.
live_run_heal() {
    ( cd "$LIVE_REPO" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
        PATH="$LIVE_STUB_BIN:$PATH" HOME="$LIVE_HOME" \
        LOOM_SOCKET_PATH="$LIVE_HOME/loom-daemon.sock" \
        LOOM_AUTONOMY_MARKER="$LIVE_MARKER" \
        LOOM_LAUNCHD_LABEL="$LIVE_LABEL" \
        LOOM_SYSTEMD_UNIT="${LIVE_LABEL}.service" \
        LOOM_SYSTEMD_FORCE=1 \
        LOOM_DAEMON_BIN="$FAKE_BIN" \
        bash "$START_SCRIPT" "$@" 2>/dev/null )
}

# #6387 portability: heal_watchdog_provisioning_gap selects its mechanism from
# `uname -s` (Darwin -> launchd, else systemd), NOT from which stub happens to
# be on $PATH. Cases 18/19 are the suite's only POSITIVE ("something must have
# been provisioned") assertions, so they have to expect the call THIS host's
# branch actually makes — a hard-coded `launchctl bootstrap` passed on macOS
# dev boxes and failed 2/54 on the hermetic Linux CI runner, which took the
# systemd branch. Both stubs are on PATH either way, so each case also asserts
# the OTHER mechanism was never touched.
if [[ "$(uname -s)" == "Darwin" ]]; then
    HEAL_MECHANISM="launchd"
    HEAL_EXPECTED_CALL="launchctl bootstrap"
    HEAL_FORBIDDEN_CALL="systemctl "
else
    HEAL_MECHANISM="systemd"
    HEAL_EXPECTED_CALL="systemctl --user enable --now"
    HEAL_FORBIDDEN_CALL="launchctl "
fi

assert_no_supervisor_side_effects() {
    local msg_prefix="$1"
    local calls; calls="$(cat "$LIVE_CALL_LOG")"
    assert_eq "" "$calls" "$msg_prefix: no launchctl/systemctl invocation at all"
    local written; written="$(find "$LIVE_HOME/Library/LaunchAgents" -name '*.plist' 2>/dev/null | wc -l | tr -d ' ')"
    assert_eq "0" "$written" "$msg_prefix: no plist written under the scratch HOME"
}

# 15. --print-plist with a LIVE pid file: still prints the plist, still touches
#     nothing (this is the exact 2026-08-16 invocation shape).
: > "$LIVE_CALL_LOG"
live_plist_out="$(live_run --print-plist)"
assert_contains "$live_plist_out" "<plist version=\"1.0\">" "--print-plist still prints the plist when a daemon is already running (#6387)"
assert_contains "$live_plist_out" "$LIVE_LABEL" "--print-plist renders the requested label rather than exiting at the already-running guard"
assert_no_supervisor_side_effects "--print-plist with a live pid file"

# 16. --print-unit gets the IDENTICAL treatment (same guard, same fix).
: > "$LIVE_CALL_LOG"
live_unit_out="$(live_run --print-unit)"
assert_contains "$live_unit_out" "Restart=on-success" "--print-unit still prints the unit when a daemon is already running (#6387)"
assert_contains "$live_unit_out" "Environment=LOOM_DAEMON_SUPERVISOR=systemd" "--print-unit renders the real unit body, not the already-running guard's message"
assert_no_supervisor_side_effects "--print-unit with a live pid file"

# 17. Neither inspection mode is decided from anything but argv: --print-plist
#     must not even truncate the persisted flags file (which the real start
#     path does unconditionally).
TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -e "$LIVE_REPO/.loom/.daemon.flags" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} inspection modes never create/truncate .loom/.daemon.flags (#6387)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} inspection modes never create/truncate .loom/.daemon.flags (#6387)"
fi

# 18. Guard against OVER-correcting: --heal-watchdog-only keeps its own,
#     already-correct early exit and MUST still provision (#5405/#5343).
: > "$LIVE_CALL_LOG"
live_run_heal --heal-watchdog-only >/dev/null
heal_calls="$(cat "$LIVE_CALL_LOG")"
assert_contains "$heal_calls" "$HEAL_EXPECTED_CALL" "--heal-watchdog-only still provisions the watchdog job via $HEAL_MECHANISM (#5405 unchanged by #6387)"
assert_not_contains "$heal_calls" "$HEAL_FORBIDDEN_CALL" "--heal-watchdog-only provisions through this host's supervisor only (no cross-mechanism call)"

# 19. Same guard for the already-running heal (#5343): a PLAIN invocation with
#     a live pid file must still self-heal the watchdog provisioning gap.
#     Test 18 just provisioned the watchdog job, and provision_watchdog_job_launchd
#     deliberately skips the bootout+bootstrap cycle when the job is loaded AND
#     the render is byte-identical (#4862) -- so re-open a genuine gap first,
#     else this would assert the skip path rather than the heal path. (Only the
#     launchd branch needs this: `systemctl --user enable --now` on an already
#     active timer is a verified no-op that still ISSUES the call, #4862, so the
#     systemd branch has nothing to re-open -- the rm is simply a no-op there.)
rm -f "$LIVE_HOME/Library/LaunchAgents/${LIVE_LABEL}-watchdog.plist"
: > "$LIVE_CALL_LOG"
live_run_heal >/dev/null
running_heal_calls="$(cat "$LIVE_CALL_LOG")"
assert_contains "$running_heal_calls" "$HEAL_EXPECTED_CALL" "already-running guard still heals the watchdog provisioning gap via $HEAL_MECHANISM (#5343 unchanged by #6387)"
assert_not_contains "$running_heal_calls" "$HEAL_FORBIDDEN_CALL" "already-running heal provisions through this host's supervisor only (no cross-mechanism call)"

kill "$LIVE_PID" 2>/dev/null
wait "$LIVE_PID" 2>/dev/null
trap 'rm -rf "$WORKDIR"' EXIT
trap 'rm -rf "$WORKDIR"; exit 1' INT TERM

# ---------- #6568: agent-session isolation (render side) ----------
# Root cause under test: a daemon-dispatched sweep invoked loom-daemon-start.sh
# on 2026-08-17 to exercise the start path. Two independent properties combined
# to overwrite the PRODUCTION LaunchAgent on BOTH operator Macs, silently, for
# two days:
#
#   1. render_launchd_plist / render_systemd_unit harvest EVERY exported LOOM_*
#      var into the durable env dict -- so LOOM_SWEEP_CLAIM_OWNED=6388,
#      LOOM_TERMINAL_ID=..., LOOM_ROLE=sweep-lifecycle, LOOM_RUNTIME=claude all
#      became production daemon config.
#   2. resolve_launchd_label() returns the fixed production label whenever
#      LOOM_LAUNCHD_LABEL is unset, so the write landed on the real job.
#
# Cases 21-24 cover the render/preview side (this suite's remit); the
# REAL-START refusal is covered in test-loom-daemon-start.sh's "SI." section,
# because it must drive the actual install path, not a preview.
SESSION_TEST_LABEL="com.rjwalters.loom-daemon-session-test"
SESSION_HOME="$WORKDIR/fakehome-session"
mkdir -p "$SESSION_HOME/Library/LaunchAgents"

# The four session-scoped families the incident laundered, exported exactly as
# a dispatched sweep exports them.
session_env=(
    LOOM_SWEEP_CLAIM_OWNED=6388
    LOOM_TERMINAL_ID=daemon-sweep-issue-6388-abcdef
    LOOM_ROLE=sweep-lifecycle
    LOOM_RUNTIME=claude
)

# 21. --print-plist from a session context with NO LOOM_LAUNCHD_LABEL: the
#     guard WARNS (naming the detected keys and the production label it would
#     have written) but does NOT refuse -- inspection semantics are unchanged
#     (plist on stdout, exit 0, nothing written). This is the same
#     warn-on-preview / refuse-on-real-start split warn_autonomy_downgrade
#     already uses.
sess_preview_out=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE "${session_env[@]}" \
    LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>/dev/null )
sess_preview_rc=$?
sess_preview_err=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE "${session_env[@]}" \
    LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_eq "0" "$sess_preview_rc" "#6568: --print-plist from a session context still exits 0 (read-only preview is never refused)"
assert_contains "$sess_preview_out" '<plist version="1.0">' "#6568: --print-plist from a session context still prints the plist on stdout"
assert_contains "$sess_preview_err" "agent-session context detected" "#6568: --print-plist warns that this shell carries agent-session context"
assert_contains "$sess_preview_err" "LOOM_ROLE" "#6568: the warning NAMES the detected session keys"
assert_contains "$sess_preview_err" "com.rjwalters.loom-daemon" "#6568: the warning names the production label a real start would have written"
assert_contains "$sess_preview_err" "NOT refused" "#6568: the preview says explicitly that it is not refused (and a real start would be)"
assert_not_contains "$sess_preview_err" "refusing to start" "#6568: a read-only preview never emits the refusal (AC5 — inspection semantics unaffected)"

# 22. The session-scoped keys NEVER reach the rendered EnvironmentVariables --
#     even here, where the label IS explicitly scoped (so the guard above is
#     exempt and cannot be what produced the result). The autonomy keys are
#     asserted PRESENT in the same render so this is not a vacuous pass on a
#     render that dropped everything.
sess_render_out=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_MAIN_HEALTH_GATE "${session_env[@]}" LOOM_WORK_FINDER=1 \
    LOOM_LAUNCHD_LABEL="$SESSION_TEST_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>/dev/null )
assert_contains "$sess_render_out" $'<key>LOOM_WORK_FINDER</key>\n        <string>1</string>' "#6568 control: a legitimate exported LOOM_* var is still forwarded (strip is targeted, not a blanket drop)"
assert_not_contains "$sess_render_out" "<key>LOOM_SWEEP_CLAIM_OWNED</key>" "#6568: LOOM_SWEEP_CLAIM_OWNED never reaches the rendered plist"
assert_not_contains "$sess_render_out" "<key>LOOM_TERMINAL_ID</key>" "#6568: LOOM_TERMINAL_ID never reaches the rendered plist"
assert_not_contains "$sess_render_out" "<key>LOOM_ROLE</key>" "#6568: LOOM_ROLE never reaches the rendered plist"
assert_not_contains "$sess_render_out" "<key>LOOM_RUNTIME</key>" "#6568: LOOM_RUNTIME never reaches the rendered plist"
assert_contains "$sess_render_out" "$SESSION_TEST_LABEL" "#6568: an explicit LOOM_LAUNCHD_LABEL still overrides the label from a session context (existing test-authoring pattern preserved)"

# 22b. Identical strip on the systemd tier -- the incident's mechanism is
#      per-platform but the laundering is not.
sess_unit_out=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_MAIN_HEALTH_GATE "${session_env[@]}" LOOM_WORK_FINDER=1 \
    LOOM_SYSTEMD_UNIT="loom-daemon-session-test.service" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-unit 2>/dev/null )
assert_contains "$sess_unit_out" "Environment=LOOM_WORK_FINDER=1" "#6568 control: the systemd unit still forwards a legitimate exported LOOM_* var"
assert_not_contains "$sess_unit_out" "Environment=LOOM_SWEEP_CLAIM_OWNED=" "#6568: LOOM_SWEEP_CLAIM_OWNED never reaches the rendered systemd unit"
assert_not_contains "$sess_unit_out" "Environment=LOOM_TERMINAL_ID=" "#6568: LOOM_TERMINAL_ID never reaches the rendered systemd unit"
assert_not_contains "$sess_unit_out" "Environment=LOOM_ROLE=" "#6568: LOOM_ROLE never reaches the rendered systemd unit"
assert_not_contains "$sess_unit_out" "Environment=LOOM_RUNTIME=" "#6568: LOOM_RUNTIME never reaches the rendered systemd unit"

# 23. An explicit LOOM_LAUNCHD_LABEL exempts the guard entirely -- no warning
#     at all, so a test/sandbox invocation from a session context is not
#     merely tolerated but silent (this is what keeps cases 14-19 above, all
#     of which run from whatever shell launched this suite, unchanged).
sess_labeled_err=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE "${session_env[@]}" \
    LOOM_LAUNCHD_LABEL="$SESSION_TEST_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_not_contains "$sess_labeled_err" "agent-session context detected" "#6568: an explicit LOOM_LAUNCHD_LABEL exempts the session-isolation guard (no warning)"

# 24. An ALREADY-POISONED installed plist must not re-inject its session keys
#     through the #5344 carry-forward merge. Without this, the fix would be
#     self-defeating on exactly the hosts that suffered the incident: the
#     renderer would strip the keys and the merge would put them straight back
#     on every subsequent re-render. A legitimate operator key in the same
#     installed plist IS still carried forward, so the purge is targeted.
POISON_LABEL="com.rjwalters.loom-daemon-poisoned-test"
cat > "$SESSION_HOME/Library/LaunchAgents/${POISON_LABEL}.plist" <<'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>EnvironmentVariables</key>
<dict>
        <key>LOOM_ROLE</key>
        <string>sweep-lifecycle</string>
        <key>LOOM_SWEEP_CLAIM_OWNED</key>
        <string>6388</string>
        <key>LOOM_TERMINAL_ID</key>
        <string>daemon-sweep-issue-6388-abcdef</string>
        <key>LOOM_RUNTIME</key>
        <string>claude</string>
        <key>LOOM_OPERATOR_ONLY_KEY</key>
        <string>keepme</string>
</dict>
</dict></plist>
PLISTEOF
poison_out=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_ROLE -u LOOM_RUNTIME \
    -u LOOM_TERMINAL_ID -u LOOM_SWEEP_CLAIM_OWNED \
    LOOM_LAUNCHD_LABEL="$POISON_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>/dev/null )
poison_err=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE -u LOOM_ROLE -u LOOM_RUNTIME \
    -u LOOM_TERMINAL_ID -u LOOM_SWEEP_CLAIM_OWNED \
    LOOM_LAUNCHD_LABEL="$POISON_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_not_contains "$poison_out" "<key>LOOM_ROLE</key>" "#6568: a session key in the INSTALLED plist is not carried forward by the #5344 merge"
assert_not_contains "$poison_out" "<key>LOOM_SWEEP_CLAIM_OWNED</key>" "#6568: LOOM_SWEEP_CLAIM_OWNED is purged from the carry-forward, not preserved"
assert_not_contains "$poison_out" "<key>LOOM_TERMINAL_ID</key>" "#6568: LOOM_TERMINAL_ID is purged from the carry-forward, not preserved"
assert_not_contains "$poison_out" "<key>LOOM_RUNTIME</key>" "#6568: LOOM_RUNTIME is purged from the carry-forward, not preserved"
assert_contains "$poison_out" $'<key>LOOM_OPERATOR_ONLY_KEY</key>\n        <string>keepme</string>' "#6568 control: a NON-session key in the installed plist is still carried forward (#5344 intact)"
assert_contains "$poison_err" "purging 4 AGENT-SESSION env key(s)" "#6568: the purge is reported, not silent"

# 25. Scratch-workdir drift warning (#6568 ask 3): the incident's production
#     daemons ran with WorkingDirectory/LOOM_WORKSPACE under /tmp for two days
#     with nothing complaining. Advisory only -- it must never block a start.
drift_scratch_err=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    LOOM_WORKSPACE="/tmp/pr6416-checkout" \
    LOOM_LAUNCHD_LABEL="$SESSION_TEST_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_contains "$drift_scratch_err" "SCRATCH / temporary directory" "#6568: a scratch-rooted daemon config is flagged loudly"
assert_contains "$drift_scratch_err" "LOOM_WORKSPACE=/tmp/pr6416-checkout" "#6568: the drift warning names the offending LOOM_WORKSPACE (the incident's actual value)"

drift_ok_err=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    LOOM_WORKSPACE="/opt/loom/machine" \
    LOOM_LAUNCHD_LABEL="$SESSION_TEST_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_not_contains "$drift_ok_err" "LOOM_WORKSPACE=/opt/loom/machine" "#6568 control: a durable LOOM_WORKSPACE is NOT reported as drift (the check discriminates)"

drift_out=$( cd "$WORKDIR" && HOME="$SESSION_HOME" \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    LOOM_WORKSPACE="/tmp/pr6416-checkout" \
    LOOM_LAUNCHD_LABEL="$SESSION_TEST_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    bash "$START_SCRIPT" --print-plist 2>/dev/null )
drift_rc=$?
assert_eq "0" "$drift_rc" "#6568: the drift warning is advisory — it never blocks (exit 0)"
assert_contains "$drift_out" '<plist version="1.0">' "#6568: the drift warning goes to stderr only, leaving stdout a clean plist"

# ---------- 20. post-suite: NO real supervisor job under any test label ----------
# The stronger property test 1's before/after plist count structurally cannot
# see: in the 2026-08-16 incident the plist landed in a scratch HOME while the
# JOB landed in the operator's REAL launchd domain -- and a bootstrapped job
# survives `rm -rf "$WORKDIR"` deleting the plist it was loaded from. So assert
# against the live job table itself, for every label this suite ever hands to
# loom-daemon-start.sh (substring match, which also covers the derived
# `<label>-watchdog` job that resolve_watchdog_label produces -- and, since
# live_run_heal names LOOM_SYSTEMD_UNIT after the same label, the
# `<label>-watchdog.timer`/`.service` units of the systemd branch too).
TEST_LABELS=(
    "com.rjwalters.loom-daemon-nodrift-test"
    "com.rjwalters.loom-daemon-drift-test"
    "com.rjwalters.loom-daemon-livepid-test"
    "com.example.custom"
    "com.rjwalters.loom-daemon-session-test"
    "com.rjwalters.loom-daemon-poisoned-test"
)
supervisor_tool=""
supervisor_listing=""
if command -v launchctl >/dev/null 2>&1; then
    supervisor_tool="launchctl list"
    supervisor_listing="$(launchctl list 2>/dev/null || true)"
elif command -v systemctl >/dev/null 2>&1; then
    supervisor_tool="systemctl --user list-units"
    supervisor_listing="$(systemctl --user list-units --all --no-legend --no-pager 2>/dev/null || true)"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -z "$supervisor_tool" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} no launchctl/systemctl on this host — no supervisor job could exist under a test label (#6387)"
else
    leaked_labels=()
    for _lbl in "${TEST_LABELS[@]}"; do
        if grep -qF -- "$_lbl" <<<"$supervisor_listing"; then
            leaked_labels+=("$_lbl")
        fi
    done
    unset _lbl
    if [[ "${#leaked_labels[@]}" -eq 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} \`$supervisor_tool\` shows no job under any test label (#6387)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} \`$supervisor_tool\` shows a job under a test label (#6387) — this suite leaked into real supervisor state"
        printf '    %s\n' "${leaked_labels[@]}"
        echo "    Remove each with: launchctl bootout gui/\$(id -u)/<label>   (macOS)"
        echo "                  or: systemctl --user disable --now <unit>     (Linux)"
    fi
fi

# ---------- summary ----------
echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
