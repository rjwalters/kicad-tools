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
# Style matches test-loom-daemon-start.sh — plain bash, hand-rolled
# assertions. Bats is NOT used in this repository.
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-launchd-plist.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/loom-daemon-start.sh"

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
# traps touched in that issue): every invocation below passes --print-plist
# (or --help), which per the file header above is pure string generation with
# NO side effects — $FAKE_BIN is only referenced via LOOM_DAEMON_BIN, never
# actually executed/backgrounded, so there is no PID this suite could leak.
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
fc_out=$( cd "$WORKDIR" && env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE LOOM_DAEMON_BIN="$FAKE_BIN" bash "$START_SCRIPT" --from-config --print-plist 2>&1 )
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
NODRIFT_HOME="$WORKDIR/fakehome-nodrift"
NODRIFT_LABEL="com.rjwalters.loom-daemon-nodrift-test"
mkdir -p "$NODRIFT_HOME/Library/LaunchAgents"
HOME="$NODRIFT_HOME" LOOM_LAUNCHD_LABEL="$NODRIFT_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    env -u LOOM_DAEMON_PATH -u LOOM_DAEMON_PATH_EXTRA -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$START_SCRIPT" --print-plist > "$NODRIFT_HOME/Library/LaunchAgents/${NODRIFT_LABEL}.plist" 2>/dev/null
nodrift_out=$( HOME="$NODRIFT_HOME" LOOM_LAUNCHD_LABEL="$NODRIFT_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
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
drift_out=$( HOME="$DRIFT_HOME" LOOM_LAUNCHD_LABEL="$DRIFT_LABEL" LOOM_DAEMON_BIN="$FAKE_BIN" \
    env -u LOOM_DAEMON_PATH -u LOOM_DAEMON_PATH_EXTRA -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$START_SCRIPT" --print-plist 2>&1 >/dev/null )
assert_contains "$drift_out" "PATH DRIFT DETECTED" "a PATH change from the live plist is flagged at --print-plist time"
assert_contains "$drift_out" "/old/stale/shell/path:/usr/bin" "drift warning shows the OLD (live) PATH value"
assert_contains "$drift_out" "- live:" "drift warning is diff-friendly (- live / + new lines)"
assert_contains "$drift_out" "+ new:" "drift warning is diff-friendly (- live / + new lines)"

# ---------- summary ----------
echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
