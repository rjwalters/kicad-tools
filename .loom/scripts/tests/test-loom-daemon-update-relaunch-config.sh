#!/usr/bin/env bash
# test-loom-daemon-update-relaunch-config.sh -- `loom-daemon-update.sh
# --relaunch` keeps a config-driven unit config-driven (#11934).
#
# --relaunch re-renders the supervised unit through loom-daemon-start.sh,
# re-exporting the live unit's LOOM_* values first so the re-render cannot
# narrow autonomy (#4011). A unit with NO LOOM_WORK_FINDER line (a
# --from-config render, a `fleet add-worker` unit) left the work finder to
# config. A bare re-render then exported the FLAGS-OFF LOOM_WORK_FINDER=0,
# which the daemon reads before config, switching off a work finder the config
# enables. The relaunch must pass --from-config for such a unit.
#
# Scenario 32 of test-loom-daemon-update.sh covers the opposite shape (a unit
# carrying both autonomy keys keeps them byte-for-byte).
#
# WHY A SEPARATE SUITE: test-loom-daemon-update.sh is over the file-size
# ratchet's 1000-line threshold and frozen (.loom/docs/file-size-policy.md),
# so this lives in a sibling sharing lib/daemon-update-fixtures.sh with it.
# Needs a BUILT loom-daemon (the fixture lib pins it), so it is wired in
# ci.yml's "Native Port Suites" job. Hermetic: stub systemctl via the
# LOOM_SYSTEMD_FORCE seam, no network, no forge.
#
# Usage:
#   ./defaults/scripts/tests/test-loom-daemon-update-relaunch-config.sh

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

# shellcheck source=lib/daemon-update-fixtures.sh
source "$SCRIPT_DIR/lib/daemon-update-fixtures.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

MINIMAL_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
BASE_WORKDIR="$(mktemp -d)"
# #8712: every fixture write must land under this directory.
loom_fixture_scratch_root "$BASE_WORKDIR"
trap '[ -n "$BASE_WORKDIR" ] && pkill -f "$BASE_WORKDIR" >/dev/null 2>&1; rm -rf "$BASE_WORKDIR"' EXIT
trap '[ -n "$BASE_WORKDIR" ] && pkill -f "$BASE_WORKDIR" >/dev/null 2>&1; rm -rf "$BASE_WORKDIR"; exit 1' INT TERM

# The machine config tier is live host state (#11934): pin it to an absent
# file so a host whose own tier enables the work finder runs this hermetically.
export LOOM_CONFIG_DEFAULTS_FILE="$BASE_WORKDIR/machine-config-defaults.json"
unset LOOM_WORK_FINDER LOOM_MAIN_HEALTH_GATE LOOM_PID_FILE LOOM_WORKSPACE \
    LOOM_MACHINE_CHECKOUT LOOM_DAEMON_BIN LOOM_SWEEP_CLAIM_OWNED LOOM_TERMINAL_ID \
    LOOM_ROLE LOOM_RUNTIME

FAKE_BIN_DIR="$BASE_WORKDIR/fakebin"
mkdir -p "$FAKE_BIN_DIR"
write_fake_cargo "$FAKE_BIN_DIR/cargo"
# `crontab -l` runs on every update; answer "no crontab" instead of reading
# the real account's.
printf '%s\n' '#!/usr/bin/env bash' '[[ "${1:-}" == "-l" ]] && exit 1' 'exit 0' \
    > "$FAKE_BIN_DIR/crontab"
chmod +x "$FAKE_BIN_DIR/crontab"
launchd_sandbox_install_stubs "$FAKE_BIN_DIR" "$BASE_WORKDIR/launchd-log"
TEST_PATH="$FAKE_BIN_DIR:$MINIMAL_PATH"

# A fake daemon whose `restart` is REFUSED (a pre-#4267 binary), which is what
# sends an update down the exit-6 / --relaunch path.
write_refusing_daemon() {
    local path="$1" commit="$2"
    cat > "$path" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then
    echo "loom-daemon 0.15.0 (commit ${commit}, built 2026-07-26T00:00:00Z)"
    exit 0
fi
[[ "\${1:-}" == "restart" ]] && exit 1
[[ "\${1:-}" == "calibrate" ]] && exit 1
[[ "\${1:-}" == "telemetry-capabilities" ]] && { echo '{"otlp":true}'; exit 0; }
if [[ -n "\${1:-}" && "\${1:-}" != -* ]]; then exit 1; fi
while true; do sleep 1; done
EOF
    chmod +x "$path"
}

# Stub systemctl: logs every call and reports an ACTIVE unit with MainPID $3.
write_systemctl_stub() {
    local bin_dir="$1" log="$2" mainpid="$3"
    mkdir -p "$bin_dir"
    : > "$log"
    cat > "$bin_dir/systemctl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${log}"
if [[ "\${1:-}" == "--user" ]]; then shift; fi
case "\${1:-}" in
  show) echo "${mainpid}" ;;
  *)    exit 0 ;;
esac
EOF
    chmod +x "$bin_dir/systemctl"
}

# A config-driven unit: supervised-era layout, but NO LOOM_WORK_FINDER /
# LOOM_MAIN_HEALTH_GATE line, and one unrelated LOOM_* key the harvest must
# still carry through.
write_config_driven_unit() {
    local unit_path="$1" bin="$2"
    mkdir -p "$(dirname "$unit_path")"
    cat > "$unit_path" <<EOF
[Unit]
Description=Loom autonomous daemon (loom-daemon)

[Service]
Type=simple
WorkingDirectory=/tmp
ExecStart=${bin}
Environment=PATH=/usr/bin:/bin
Environment=HOME=/tmp
Environment=LOOM_WORK_FINDER_MAX_CONCURRENT=10

[Install]
WantedBy=default.target
EOF
}

W="$BASE_WORKDIR/w"
new_fixture "$W"
echo '{"autonomous": {"workFinder": {"enabled": true}}}' > "$W/.loom/config.json"
( cd "$W" && git add .loom/config.json && git -c user.email=test@test -c user.name=test commit -q -m config )
INSTALLED="$W/installed/loom-daemon"
mkdir -p "$W/installed"
write_refusing_daemon "$INSTALLED" "deadbee"
NEW_FAKE="$W/new-fake-daemon"
write_refusing_daemon "$NEW_FAKE" "$(cd "$W" && git rev-parse --short HEAD)"
UNIT="loom-daemon-relaunch-config-$$.service"
SD_BIN="$W/systemd-bin"
SD_LOG="$W/systemctl.log"
write_systemctl_stub "$SD_BIN" "$SD_LOG" "4242"
HOME_W="$W/home"
UNIT_PATH="$HOME_W/.config/systemd/user/${UNIT}"
write_config_driven_unit "$UNIT_PATH" "$INSTALLED"

out=$( cd "$W" && PATH="$SD_BIN:$TEST_PATH" HOME="$HOME_W" LOOM_SYSTEMD_FORCE=1 LOOM_DAEMON_SYSTEMD=1 \
    LOOM_SYSTEMD_UNIT="$UNIT" \
    LOOM_SOCKET_PATH="$W/state/loom-daemon.sock" LOOM_AUTONOMY_MARKER="$W/state/autonomy-desired" \
    LOOM_DAEMON_BIN="$INSTALLED" NEW_FAKE_BIN_SRC="$NEW_FAKE" \
    bash "$UPDATE_SCRIPT" --relaunch 2>&1 )

check() {
    local ok="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$ok" == yes ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} $msg"
        echo "  unit:"; sed 's/^/    /' "$UNIT_PATH" 2>/dev/null
        echo "  output:"; printf '%s\n' "$out" | tail -25 | sed 's/^/    /'
    fi
}

check "$(grep -qx 'Environment=LOOM_DAEMON_SUPERVISOR=systemd' "$UNIT_PATH" && echo yes)" \
    "--relaunch re-rendered the config-driven unit (supervised keys present)"
check "$(grep -q -- '--from-config' <<<"$out" && echo yes)" \
    "--relaunch passed --from-config for the unit that carried no autonomy keys (#11934)"
check "$(grep -q '^Environment=LOOM_WORK_FINDER=' "$UNIT_PATH" || echo yes)" \
    "the re-rendered unit carries NO LOOM_WORK_FINDER line, so config keeps the work finder on (#11934)"
check "$(grep -qx 'Environment=LOOM_WORK_FINDER_MAX_CONCURRENT=10' "$UNIT_PATH" && echo yes)" \
    "the harvest still carries the unit's other LOOM_* keys through"

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
