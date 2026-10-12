#!/usr/bin/env bash
# test-loom-daemon-update-otlp-probe.sh -- loom-daemon-update.sh refuses a
# freshly-built binary that lacks the OTLP exporter (#10700).
#
# The post-build probe `telemetry-capabilities --require-otlp` exits 1 for a
# binary without the exporter; `rebuild.rs::probe_otlp` then refuses
# (MissingOtlp, "OTLP probe FAILED") and the installed binary is left untouched.
# Every happy-path scenario in test-loom-daemon-update.sh proves the probe
# passes for a capable daemon (its fakes answer it); this suite proves the
# probe is load-bearing.
#
# WHY THIS IS A SEPARATE SUITE: test-loom-daemon-update.sh is over the
# file-size ratchet's 1000-line threshold and frozen
# (.loom/docs/file-size-policy.md), so the scenario lives in a sibling that
# shares lib/daemon-update-fixtures.sh with it.
#
# Needs a BUILT loom-daemon (the fixture lib pins $LOOM_DAEMON_BIN), so it is
# wired in ci.yml's "Native Port Suites" job. Hermetic: no network, no forge.
#
# Usage:
#   ./.loom/scripts/tests/test-loom-daemon-update-otlp-probe.sh

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
export LOOM_SYSTEMD_UNIT="loom-daemon-otlp-probe-test-$$.service"

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
cleanup() { rm -rf "$BASE_WORKDIR"; }
trap cleanup EXIT

FAKE_BIN_DIR="$BASE_WORKDIR/fakebin"
mkdir -p "$FAKE_BIN_DIR"
write_fake_cargo "$FAKE_BIN_DIR/cargo"
launchd_sandbox_install_stubs "$FAKE_BIN_DIR" "$BASE_WORKDIR/launchd-log"
TEST_PATH="$FAKE_BIN_DIR:$MINIMAL_PATH"

WOT="$BASE_WORKDIR/wotlp"
new_fixture "$WOT"
HEADOT="$(cd "$WOT" && git rev-parse --short HEAD)"
INSTALLEDOT="$WOT/installed/loom-daemon"
mkdir -p "$WOT/installed"
write_fake_daemon "$INSTALLEDOT" "deadbee" "$WOT/old-marker"
NEW_FAKEOT="$WOT/new-fake-daemon"
write_fake_daemon "$NEW_FAKEOT" "$HEADOT" "$WOT/new-marker"
( cd "$WOT" && PATH="$TEST_PATH" LOOM_PID_FILE='' LOOM_DAEMON_BIN="$INSTALLEDOT" NEW_FAKE_BIN_SRC="$NEW_FAKEOT" \
    FAKE_DAEMON_OTLP_PROBE_EXIT=1 \
    env -u LOOM_WORK_FINDER -u LOOM_MAIN_HEALTH_GATE \
    bash "$UPDATE_SCRIPT" >"$WOT/update.log" 2>&1 )
otlp_rc=$?
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$otlp_rc" -ne 0 ]] && grep -q 'OTLP probe FAILED' "$WOT/update.log"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} update refuses when the built binary lacks OTLP (probe exit 1, #10700)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} update refuses when the built binary lacks OTLP (rc=$otlp_rc)"
    sed 's/^/    /' "$WOT/update.log" | tail -15
fi
TESTS_RUN=$((TESTS_RUN + 1))
installed_ver="$("$INSTALLEDOT" --version 2>/dev/null)"
if [[ "$installed_ver" == *deadbee* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} refused OTLP-less build did NOT replace the installed binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} refused OTLP-less build replaced the installed binary"
fi

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
