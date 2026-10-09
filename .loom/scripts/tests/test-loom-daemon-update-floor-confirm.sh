#!/usr/bin/env bash
# test-loom-daemon-update-floor-confirm.sh -- loom-daemon-update.sh asks before
# it moves a fleet host off the fleet floor (#11044).
#
# Scenarios (one fixture each, a fake installed daemon at 0.19.950, a source
# tree at VERSION 0.19.953 and a fake `cargo`, so a confirmed run is a real
# --no-fetch --no-restart rebuild + provision into the fixture):
#
#   A  fleet host, no terminal, unmarked      -> refused, exit 1, names --yes
#   B  fleet host, TTY, answer y              -> prompts, proceeds, exit 0
#   C  fleet host, TTY, answer n              -> prompts, refused, exit 1
#   D  fleet host, --yes / LOOM_DAEMON_UPDATE_YES=1 (no terminal) -> proceeds
#   E  fleet host, daemon-started (LOOM_DAEMON_UPDATE_INVOKER=daemon), with a
#      terminal on stdin -> never prompts, proceeds
#   F  non-fleet host, no terminal            -> silent, proceeds
#   G  fleet host, --dry-run, no terminal     -> warns, never prompts, exit 0
#   H  fleet host, target == floor            -> silent, proceeds
#   I  --check on a fleet host                -> unchanged (exit 3, no warning)
#   J  --to-floor: no store -> exit 1; at the floor -> exit 0; below it ->
#      a dry-run plans the floor's own release (needs jq for the fake gh)
#
# The fleet store is declared through the fixture's own .loom/config.json, and
# the floor through a fleet-sync snapshot under LOOM_SOCKET_PATH's directory, so
# nothing here reads the host's real config or ~/.loom.
#
# The TTY scenarios (B, C, E) run the script under a pseudo-terminal with
# python3's `pty`; they are skipped, loudly, without python3.
#
# Hermetic: no network, no live forge, no tokens.
#
# Usage:
#   ./.loom/scripts/tests/test-loom-daemon-update-floor-confirm.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
LOOM_REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/../cli" && pwd)"
UPDATE_SCRIPT="$CLI_DIR/loom-daemon-update.sh"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
START_SCRIPT="$CLI_DIR/loom-daemon-start.sh"

# shellcheck source=lib/launchd-sandbox.sh
source "$SCRIPT_DIR/lib/launchd-sandbox.sh"
export LOOM_LAUNCHD_LABEL="$(launchd_sandbox_new_label)"
export LOOM_DAEMON_LAUNCHD=0
export LOOM_DAEMON_SYSTEMD=0
export LOOM_SYSTEMD_UNIT="loom-daemon-floor-confirm-test-$$.service"

# shellcheck source=lib/daemon-update-fixtures.sh
source "$SCRIPT_DIR/lib/daemon-update-fixtures.sh"
loom_test_require_daemon_bin --self-only "$(cd "$SCRIPT_DIR/.." && pwd)" daemon-update

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# check <description> <output> <command...> -- pass when the command succeeds.
check() {
    local msg="$1" out="$2"
    shift 2
    TESTS_RUN=$((TESTS_RUN + 1))
    if "$@"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    output: $out"
    fi
}

has() { grep -qF -- "$1" <<<"$2"; }
lacks() { ! grep -qF -- "$1" <<<"$2"; }
eq() { [[ "$1" == "$2" ]]; }

MINIMAL_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
BASE_WORKDIR="$(mktemp -d)"
# #8712: every fixture write in this suite must land under this directory.
loom_fixture_scratch_root "$BASE_WORKDIR"
cleanup() { rm -rf "$BASE_WORKDIR"; }
trap cleanup EXIT

FAKE_BIN_DIR="$BASE_WORKDIR/fakebin"
mkdir -p "$FAKE_BIN_DIR"
launchd_sandbox_install_stubs "$FAKE_BIN_DIR" "$BASE_WORKDIR/launchd-log"
TEST_PATH="$FAKE_BIN_DIR:$MINIMAL_PATH"

FLOOR="0.19.950"
WARNING="moves it ahead of the fleet"
PROMPT="Continue? [y/N]"

# fixture <dir> <fleet|none> [floor] -- a source checkout one rebuild away
# from VERSION 0.19.953, an installed fake daemon at 0.19.950, and (fleet) a
# fleet store plus a snapshot recording <floor> (default $FLOOR).
fixture() {
    local w="$1" kind="$2" floor="${3:-$FLOOR}" head
    new_fixture "$w"
    echo "0.19.953" >"$w/VERSION"
    mkdir -p "$w/installed" "$w/fakebin" "$w/loomdir"
    write_fake_artifact_daemon "$w/installed/loom-daemon" "0.19.950" "deadbee"
    head="$(git -C "$w" rev-parse --short HEAD)"
    write_fake_artifact_daemon "$w/new-loom-daemon" "0.19.953" "$head"
    write_fake_cargo "$w/fakebin/cargo"
    if [[ "$kind" == "fleet" ]]; then
        printf '{"fleet":{"repo":"test-owner/fleet-store"}}\n' >"$w/.loom/config.json"
        printf '{"repo":"test-owner/fleet-store","at":"2026-10-08T00:00:00Z","floor":{"floor":"%s"}}\n' \
            "$floor" >"$w/loomdir/fleet-sync-status.json"
    fi
}

# in_fixture <dir> <command...> -- run <command> in the fixture's pinned
# environment. Every ambient variable that could change the answer is cleared:
# the override and marker under test, a fleet store from the operator's shell,
# a redirected cargo target dir (the fake cargo honours it), and the host's
# private-defaults config tier.
in_fixture() {
    local w="$1"
    shift
    (
        cd "$w" || exit 97
        env -u CARGO_TARGET_DIR -u LOOM_DAEMON_UPDATE_YES -u LOOM_DAEMON_UPDATE_INVOKER \
            -u LOOM_FLEET_REPO -u LOOM_MACHINE_CHECKOUT \
            PATH="$w/fakebin:$TEST_PATH" \
            LOOM_PID_FILE='' \
            LOOM_CONFIG_DEFAULTS_FILE='' \
            LOOM_SOCKET_PATH="$w/loomdir/daemon.sock" \
            LOOM_DAEMON_BIN="$w/installed/loom-daemon" \
            NEW_FAKE_BIN_SRC="$w/new-loom-daemon" \
            "$@"
    )
}

# Was the fixture's installed daemon replaced by the 0.19.953 build?
installed_version() { "$1/installed/loom-daemon" --version 2>/dev/null | awk '{print $2}'; }

# The pty driver: run argv[2:] on a pseudo-terminal, answer argv[1] at the
# first "[y/N]", print everything it wrote, exit with its status. An answer of
# "-" never answers (a prompt then hangs the child, which the 60s cap turns
# into exit 124: the test for "never prompts" is that it never needs one).
PTY_DRIVER="$BASE_WORKDIR/pty-driver.py"
cat >"$PTY_DRIVER" <<'PY'
import os, pty, select, sys, time
answer = sys.argv[1]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(sys.argv[2], sys.argv[2:])
out, sent, deadline = b"", False, time.time() + 60
while True:
    if time.time() > deadline:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        sys.stdout.write(out.decode(errors="replace"))
        sys.exit(124)
    ready, _, _ = select.select([fd], [], [], 0.5)
    if not ready:
        continue
    try:
        data = os.read(fd, 4096)
    except OSError:
        break
    if not data:
        break
    out += data
    if not sent and answer != "-" and b"[y/N]" in out:
        os.write(fd, (answer + "\n").encode())
        sent = True
_, status = os.waitpid(pid, 0)
sys.stdout.write(out.decode(errors="replace"))
sys.exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 125)
PY
HAVE_PTY=0
command -v python3 >/dev/null 2>&1 && HAVE_PTY=1

echo "A. fleet host, no terminal: refused, nothing changed"
WA="$BASE_WORKDIR/a"
fixture "$WA" fleet
outA="$(in_fixture "$WA" bash "$UPDATE_SCRIPT" --no-fetch --no-restart </dev/null 2>&1)"
rcA=$?
check "exits 1" "$outA" eq "1" "$rcA"
check "warns with the fleet, the floor and the target" "$outA" \
    has "This host is in fleet test-owner/fleet-store with loom_min_version $FLOOR" "$outA"
check "says the install moves it ahead of the fleet" "$outA" has "$WARNING" "$outA"
check "names the override" "$outA" has "--yes (or LOOM_DAEMON_UPDATE_YES=1)" "$outA"
check "never prompts without a terminal" "$outA" lacks "$PROMPT" "$outA"
check "nothing was built" "$outA" lacks "cargo build" "$outA"
check "the installed daemon is untouched" "$outA" eq "0.19.950" "$(installed_version "$WA")"

if [[ "$HAVE_PTY" == "1" ]]; then
    echo "B. fleet host, terminal, answer y: proceeds"
    WB="$BASE_WORKDIR/b"
    fixture "$WB" fleet
    outB="$(in_fixture "$WB" python3 "$PTY_DRIVER" y bash "$UPDATE_SCRIPT" --no-fetch --no-restart 2>&1)"
    rcB=$?
    check "prompts" "$outB" has "$PROMPT" "$outB"
    check "exits 0" "$outB" eq "0" "$rcB"
    check "installed the 0.19.953 build" "$outB" eq "0.19.953" "$(installed_version "$WB")"

    echo "C. fleet host, terminal, answer n: refused"
    WC="$BASE_WORKDIR/c"
    fixture "$WC" fleet
    outC="$(in_fixture "$WC" python3 "$PTY_DRIVER" n bash "$UPDATE_SCRIPT" --no-fetch --no-restart 2>&1)"
    rcC=$?
    check "prompts" "$outC" has "$PROMPT" "$outC"
    check "exits 1" "$outC" eq "1" "$rcC"
    check "says nothing was changed" "$outC" has "Not confirmed: nothing was changed." "$outC"
    check "the installed daemon is untouched" "$outC" eq "0.19.950" "$(installed_version "$WC")"

    echo "E. daemon-started run with a terminal: never prompts"
    WE="$BASE_WORKDIR/e"
    fixture "$WE" fleet
    outE="$(in_fixture "$WE" python3 "$PTY_DRIVER" - \
        env LOOM_DAEMON_UPDATE_INVOKER=daemon bash "$UPDATE_SCRIPT" --no-fetch --no-restart 2>&1)"
    rcE=$?
    check "exits 0 (no prompt to hang on)" "$outE" eq "0" "$rcE"
    check "never prompts" "$outE" lacks "$PROMPT" "$outE"
    check "still prints the warning to the log" "$outE" has "$WARNING" "$outE"
    check "says who started it" "$outE" has "Started by daemon" "$outE"
    check "installed the 0.19.953 build" "$outE" eq "0.19.953" "$(installed_version "$WE")"
else
    echo -e "  ${YELLOW}SKIP${NC}: B, C, E need python3 for a pseudo-terminal"
fi

echo "E2. daemon-started run without a terminal: proceeds"
WE2="$BASE_WORKDIR/e2"
fixture "$WE2" fleet
outE2="$(in_fixture "$WE2" env LOOM_DAEMON_UPDATE_INVOKER=daemon \
    bash "$UPDATE_SCRIPT" --no-fetch --no-restart </dev/null 2>&1)"
check "exits 0" "$outE2" eq "0" "$?"
check "never prompts" "$outE2" lacks "$PROMPT" "$outE2"

echo "D. --yes and LOOM_DAEMON_UPDATE_YES=1 proceed without a terminal"
WD="$BASE_WORKDIR/d"
fixture "$WD" fleet
outD="$(in_fixture "$WD" bash "$UPDATE_SCRIPT" --no-fetch --no-restart --yes </dev/null 2>&1)"
check "--yes exits 0" "$outD" eq "0" "$?"
check "--yes still prints the warning" "$outD" has "$WARNING" "$outD"
check "--yes never prompts" "$outD" lacks "$PROMPT" "$outD"
check "--yes installed the 0.19.953 build" "$outD" eq "0.19.953" "$(installed_version "$WD")"
WD2="$BASE_WORKDIR/d2"
fixture "$WD2" fleet
outD2="$(in_fixture "$WD2" env LOOM_DAEMON_UPDATE_YES=1 \
    bash "$UPDATE_SCRIPT" --no-fetch --no-restart </dev/null 2>&1)"
check "LOOM_DAEMON_UPDATE_YES=1 exits 0" "$outD2" eq "0" "$?"
check "LOOM_DAEMON_UPDATE_YES=1 installed the 0.19.953 build" "$outD2" eq "0.19.953" "$(installed_version "$WD2")"

echo "F. non-fleet host: unchanged"
WF="$BASE_WORKDIR/f"
fixture "$WF" none
outF="$(in_fixture "$WF" bash "$UPDATE_SCRIPT" --no-fetch --no-restart </dev/null 2>&1)"
check "exits 0" "$outF" eq "0" "$?"
check "no fleet warning" "$outF" lacks "This host is in fleet" "$outF"
check "installed the 0.19.953 build" "$outF" eq "0.19.953" "$(installed_version "$WF")"

echo "G. --dry-run on a fleet host: warns, never prompts"
WG="$BASE_WORKDIR/g"
fixture "$WG" fleet
outG="$(in_fixture "$WG" bash "$UPDATE_SCRIPT" --no-fetch --dry-run </dev/null 2>&1)"
check "exits 0" "$outG" eq "0" "$?"
check "prints the warning" "$outG" has "$WARNING" "$outG"
check "says a real run would ask" "$outG" has "[dry-run] A real run would ask for confirmation" "$outG"
check "never prompts" "$outG" lacks "$PROMPT" "$outG"
check "still prints the plan" "$outG" has "[dry-run] Would run:" "$outG"

echo "H. target equal to the floor: silent"
WH="$BASE_WORKDIR/h"
fixture "$WH" fleet "0.19.953"
outH="$(in_fixture "$WH" bash "$UPDATE_SCRIPT" --no-fetch --no-restart </dev/null 2>&1)"
check "exits 0" "$outH" eq "0" "$?"
check "no fleet warning" "$outH" lacks "This host is in fleet" "$outH"

echo "I. --check on a fleet host: unchanged"
WI="$BASE_WORKDIR/i"
fixture "$WI" fleet
outI="$(in_fixture "$WI" bash "$UPDATE_SCRIPT" --no-fetch --check </dev/null 2>&1)"
check "exits 3 (update available)" "$outI" eq "3" "$?"
check "no fleet warning" "$outI" lacks "This host is in fleet" "$outI"

echo "J. --to-floor"
WJ1="$BASE_WORKDIR/j1"
fixture "$WJ1" none
outJ1="$(in_fixture "$WJ1" bash "$UPDATE_SCRIPT" --to-floor </dev/null 2>&1)"
check "no fleet store: exits 1" "$outJ1" eq "1" "$?"
check "no fleet store: says so" "$outJ1" has "reads no fleet store" "$outJ1"
WJ2="$BASE_WORKDIR/j2"
fixture "$WJ2" fleet "0.19.950"
outJ2="$(in_fixture "$WJ2" bash "$UPDATE_SCRIPT" --to-floor </dev/null 2>&1)"
check "installed at the floor: exits 0" "$outJ2" eq "0" "$?"
check "installed at the floor: nothing to do" "$outJ2" has "already the fleet floor's release" "$outJ2"
if command -v jq >/dev/null 2>&1; then
    WJ3="$BASE_WORKDIR/j3"
    fixture "$WJ3" fleet "0.19.960"
    mkdir -p "$WJ3/gh-assets"
    write_fake_artifact_daemon "$WJ3/gh-assets/loom-daemon-x86_64-unknown-linux-gnu" "0.19.960" "cafe123"
    sha256_of "$WJ3/gh-assets/loom-daemon-x86_64-unknown-linux-gnu" \
        >"$WJ3/gh-assets/loom-daemon-x86_64-unknown-linux-gnu.sha256"
    write_fake_gh "$WJ3/fakebin/gh" "v0.19.960" "$WJ3/gh-assets"
    ln -s "$(command -v jq)" "$WJ3/fakebin/jq"
    outJ3="$(in_fixture "$WJ3" env LOOM_DAEMON_UPDATE_GH_REPO=test-owner/test-repo \
        LOOM_DAEMON_UPDATE_TARGET=x86_64-unknown-linux-gnu \
        bash "$UPDATE_SCRIPT" --to-floor --dry-run </dev/null 2>&1)"
    check "below the floor: exits 0" "$outJ3" eq "0" "$?"
    check "below the floor: plans the floor's own release" "$outJ3" \
        has "[dry-run] Would fetch + verify release artifact v0.19.960" "$outJ3"
    check "below the floor: the floor's release needs no confirmation" "$outJ3" lacks "This host is in fleet" "$outJ3"
else
    echo -e "  ${YELLOW}SKIP${NC}: J3 needs jq for the fake gh"
fi

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
