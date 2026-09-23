#!/usr/bin/env bash
# test-loom-daemon-update-fetch.sh -- artifact-fetch mode (Epic #4990 Phase 3,
# #5020) of loom-daemon-update.sh: tests A-V, split out of
# test-loom-daemon-update.sh by #8028 (epic #7810 PR 6a).
#
# WHY THIS IS A SEPARATE SUITE
#
# As of #8028, fetch_and_verify_artifact() DELEGATES to
# `loom-daemon release-fetch`, so these assertions need a built loom-daemon.
# Its parent suite must not: it drives the real daemon lifecycle scripts,
# which is why run-ci-suites.sh owns it in LIVE_DAEMON_GUARDED_SUITES
# (#6386), and why test-run-ci-suites-daemon-guard.sh pins that membership as
# an explicit literal so a drop is a test failure rather than an invisible
# regression.
#
# Moving the whole parent to a Rust-capable job to satisfy these assertions
# would have reduced that guard's scope. Splitting is the resolution (the
# same one #7977 used for --resolve-json): the parent stays hermetic and
# guarded where it belongs, and only the assertions that need a binary move
# to the job that builds one.
#
# The fixtures both suites use live in lib/daemon-update-fixtures.sh, shared
# rather than copied -- a fake `gh`/`cosign`/`codesign` that drifts in one of
# two copies is a test that proves nothing, which is the class of bug #7977
# hit.
#
# Hermetic otherwise: no network, no live forge, no tokens. Every read goes
# to a stub.
#
# Usage:
#   ./.loom/scripts/tests/test-loom-daemon-update-fetch.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
LOOM_REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/../cli" && pwd)"
UPDATE_SCRIPT="$CLI_DIR/loom-daemon-update.sh"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
START_SCRIPT="$CLI_DIR/loom-daemon-start.sh"

# Every scenario below invokes the real loom-daemon-start.sh/-stop.sh via a
# throwaway fixture (new_fixture), but only ever with --no-restart / --check /
# --dry-run -- no scenario here reaches a real restart. The launchd sandbox is
# still applied for the same reason the resolve-json sibling applies it: a
# scratch label no launchd lookup could resolve to the operator's real job.
# shellcheck source=lib/launchd-sandbox.sh
source "$SCRIPT_DIR/lib/launchd-sandbox.sh"
export LOOM_LAUNCHD_LABEL="$(launchd_sandbox_new_label)"
export LOOM_DAEMON_LAUNCHD=0
export LOOM_SYSTEMD_UNIT="loom-daemon-fetch-test-$$.service"

# The fake-binary / fake-forge fixtures, shared with the parent suite (and
# the resolve-json sibling) so all three build identical ones from a single
# definition (#7977, #8028).
# shellcheck source=lib/daemon-update-fixtures.sh
source "$SCRIPT_DIR/lib/daemon-update-fixtures.sh"

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
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected: '$expected'"
        echo "    Actual:   '$actual'"
    fi
}

MINIMAL_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
BASE_WORKDIR="$(mktemp -d)"
# #8712: every fixture write in this suite must land under this directory. A
# fake loom-daemon that escapes to a real `loom-daemon/target/release/` poisons
# `loom-daemon-update.sh --fetch` on the host until it is removed by hand.
loom_fixture_scratch_root "$BASE_WORKDIR"
cleanup() { rm -rf "$BASE_WORKDIR"; }
trap cleanup EXIT

FAKE_BIN_DIR="$BASE_WORKDIR/fakebin"
mkdir -p "$FAKE_BIN_DIR"
launchd_sandbox_install_stubs "$FAKE_BIN_DIR" "$BASE_WORKDIR/launchd-log"
TEST_PATH="$FAKE_BIN_DIR:$MINIMAL_PATH"

# ============================================================
# Artifact-fetch mode (Epic #4990 Phase 3, #5020): resolve the latest GitHub
# Release, download + verify its artifact for this host's platform, and
# provision it INSTEAD of a local `cargo build --release`. Every test below
# pins LOOM_DAEMON_UPDATE_GH_REPO (bypassing git-remote parsing) and
# LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" (a Linux target, so no
# codesign/cosign tooling is needed to exercise the core resolve/download/
# verify/provision flow — signature verification for that target is a
# soft-skip whenever no `.sig` asset is published, exactly test A's fixture).
# ============================================================

# ------------------------------------------------------------
# A. Successful artifact update: a newer release with a matching-platform
#    artifact is fetched, checksum-verified, and provisioned — WITHOUT ever
#    invoking `cargo build` (no fake cargo is placed on PATH for this test at
#    all, so a fallback to the source-build path would fail loudly rather
#    than silently succeed).
# ------------------------------------------------------------
WA="$BASE_WORKDIR/w-fetch-a"
new_fixture "$WA"
write_fake_daemon "$WA/installed-loom-daemon" "oldc0mm" "$WA/marker"

WA_ASSETS="$WA/gh-assets"
mkdir -p "$WA_ASSETS"
WA_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WA_ASSETS/$WA_BIN_NAME" "0.16.0" "artifac1"
sha256_of "$WA_ASSETS/$WA_BIN_NAME" > "$WA_ASSETS/$WA_BIN_NAME.sha256"

WA_FAKEBIN="$WA/fakebin"
mkdir -p "$WA_FAKEBIN"
write_fake_gh "$WA_FAKEBIN/gh" "v0.16.0" "$WA_ASSETS"

outA=$( cd "$WA" && PATH="$WA_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WA/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcA=$(echo "$outA" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcA" "artifact-fetch: successful update exits 0"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outA"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: successful update never invokes 'cargo build' (AC1)"
    echo "  output: $outA"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: successful update never invokes 'cargo build' (AC1)"
fi

installedA_version="$("$WA/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'commit artifac1' <<<"$installedA_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the fetched+verified artifact was provisioned to the destination"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the fetched+verified artifact was provisioned to the destination"
    echo "  --version: $installedA_version"
fi

# ------------------------------------------------------------
# B. Checksum-mismatch abort: a tampered/corrupted checksum aborts the WHOLE
#    update (exit 1) and leaves the running (destination) daemon untouched —
#    never a soft fallback to a source build (AC2).
# ------------------------------------------------------------
WB="$BASE_WORKDIR/w-fetch-b"
new_fixture "$WB"
write_fake_daemon "$WB/installed-loom-daemon" "oldc0mm" "$WB/marker"
installedB_before="$("$WB/installed-loom-daemon" --version 2>/dev/null)"

WB_ASSETS="$WB/gh-assets"
mkdir -p "$WB_ASSETS"
WB_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WB_ASSETS/$WB_BIN_NAME" "0.16.0" "artifac2"
# Deliberately WRONG checksum (does not match the binary written above).
echo "0000000000000000000000000000000000000000000000000000000000000000  ${WB_BIN_NAME}" > "$WB_ASSETS/$WB_BIN_NAME.sha256"

WB_FAKEBIN="$WB/fakebin"
mkdir -p "$WB_FAKEBIN"
write_fake_gh "$WB_FAKEBIN/gh" "v0.16.0" "$WB_ASSETS"

outB=$( cd "$WB" && PATH="$WB_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WB/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcB=$(echo "$outB" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcB" "artifact-fetch: checksum mismatch aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Checksum verification FAILED' <<<"$outB"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: checksum-mismatch failure is reported explicitly"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: checksum-mismatch failure is reported explicitly"
    echo "  output: $outB"
fi

installedB_after="$("$WB/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedB_before" "$installedB_after" "artifact-fetch: checksum mismatch leaves the destination binary untouched"

# ------------------------------------------------------------
# C. Missing-artifact fallback: the resolved release has no artifact
#    published for this host's platform (a real, but incomplete, release) —
#    softly falls back to the existing local source-build path and still
#    completes the update end-to-end (AC4).
# ------------------------------------------------------------
WC="$BASE_WORKDIR/w-fetch-c"
new_fixture "$WC"
HEADC="$(cd "$WC" && git rev-parse --short HEAD)"
write_fake_daemon "$WC/installed-loom-daemon" "deadbee" "$WC/marker"
NEW_FAKE_C="$WC/new-fake-daemon"
write_fake_daemon "$NEW_FAKE_C" "$HEADC" "$WC/new-marker"

# A release exists (newer than installed) but publishes NO assets at all for
# this target — fetch_resolve_latest must reject it (no matching bin/sha256)
# and the caller must fall back, not hard-fail.
WC_ASSETS="$WC/gh-assets"
mkdir -p "$WC_ASSETS"
echo "unrelated-file" > "$WC_ASSETS/README.txt"

WC_FAKEBIN="$WC/fakebin"
mkdir -p "$WC_FAKEBIN"
write_fake_gh "$WC_FAKEBIN/gh" "v0.20.0" "$WC_ASSETS"
write_fake_cargo "$WC_FAKEBIN/cargo"

outC=$( cd "$WC" && PATH="$WC_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WC/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    NEW_FAKE_BIN_SRC="$NEW_FAKE_C" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcC=$(echo "$outC" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcC" "artifact-fetch: missing-artifact fallback still completes the update (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Artifact-fetch:.*falling back to the local source-build path' <<<"$outC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: a resolution failure is reported as a soft fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: a resolution failure is reported as a soft fallback"
    echo "  output: $outC"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outC"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: missing-artifact fallback actually rebuilds from source"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: missing-artifact fallback actually rebuilds from source"
    echo "  output: $outC"
fi

installedC_version="$("$WC/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "commit ${HEADC}" <<<"$installedC_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the source-build fallback provisioned the freshly-built binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the source-build fallback provisioned the freshly-built binary"
    echo "  --version: $installedC_version"
fi

# ------------------------------------------------------------
# D. --no-fetch disables artifact-fetch mode entirely, even when a matching
#    release artifact IS available — restores the pre-#5020 always-build
#    behavior.
# ------------------------------------------------------------
WD="$BASE_WORKDIR/w-fetch-d"
new_fixture "$WD"
HEADD="$(cd "$WD" && git rev-parse --short HEAD)"
write_fake_daemon "$WD/installed-loom-daemon" "deadbee" "$WD/marker"
NEW_FAKE_D="$WD/new-fake-daemon"
write_fake_daemon "$NEW_FAKE_D" "$HEADD" "$WD/new-marker"

WD_ASSETS="$WD/gh-assets"
mkdir -p "$WD_ASSETS"
WD_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WD_ASSETS/$WD_BIN_NAME" "0.16.0" "artifac4"
sha256_of "$WD_ASSETS/$WD_BIN_NAME" > "$WD_ASSETS/$WD_BIN_NAME.sha256"

WD_FAKEBIN="$WD/fakebin"
mkdir -p "$WD_FAKEBIN"
write_fake_gh "$WD_FAKEBIN/gh" "v0.16.0" "$WD_ASSETS"
write_fake_cargo "$WD_FAKEBIN/cargo"

outD=$( cd "$WD" && PATH="$WD_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WD/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    NEW_FAKE_BIN_SRC="$NEW_FAKE_D" \
    bash "$UPDATE_SCRIPT" --no-restart --no-fetch 2>&1; echo "EXIT=$?" )
rcD=$(echo "$outD" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcD" "--no-fetch: update still completes (exit 0)"

installedD_version="$("$WD/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "commit ${HEADD}" <<<"$installedD_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --no-fetch: rebuilds from source even though a release artifact was available"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --no-fetch: rebuilds from source even though a release artifact was available"
    echo "  --version: $installedD_version"
fi

# ------------------------------------------------------------
# E. --fetch (forced) hard-fails rather than silently falling back to a
#    source build when no matching artifact resolves.
# ------------------------------------------------------------
WE="$BASE_WORKDIR/w-fetch-e"
new_fixture "$WE"
write_fake_daemon "$WE/installed-loom-daemon" "deadbee" "$WE/marker"

WE_ASSETS="$WE/gh-assets"
mkdir -p "$WE_ASSETS"
echo "unrelated-file" > "$WE_ASSETS/README.txt"

WE_FAKEBIN="$WE/fakebin"
mkdir -p "$WE_FAKEBIN"
write_fake_gh "$WE_FAKEBIN/gh" "v0.20.0" "$WE_ASSETS"
write_fake_cargo "$WE_FAKEBIN/cargo"

outE=$( cd "$WE" && PATH="$WE_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WE/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart --fetch 2>&1; echo "EXIT=$?" )
rcE=$(echo "$outE" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcE" "--fetch: refuses to silently fall back to a source build (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outE"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --fetch: never falls back to 'cargo build' on a forced-fetch failure"
    echo "  output: $outE"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --fetch: never falls back to 'cargo build' on a forced-fetch failure"
fi

# ------------------------------------------------------------
# F. GitHub API unreachable/rate-limited during release resolution: every
#    `gh release view` fails. This must be a SOFT fallback to the local
#    source build (AC4) — never a hard failure of the whole update.
# ------------------------------------------------------------
WF="$BASE_WORKDIR/w-fetch-f"
new_fixture "$WF"
HEADF="$(cd "$WF" && git rev-parse --short HEAD)"
write_fake_daemon "$WF/installed-loom-daemon" "deadbee" "$WF/marker"
NEW_FAKE_F="$WF/new-fake-daemon"
write_fake_daemon "$NEW_FAKE_F" "$HEADF" "$WF/new-marker"

WF_FAKEBIN="$WF/fakebin"
mkdir -p "$WF_FAKEBIN"
write_fake_gh_unreachable "$WF_FAKEBIN/gh"
write_fake_cargo "$WF_FAKEBIN/cargo"

outF=$( cd "$WF" && PATH="$WF_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WF/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    NEW_FAKE_BIN_SRC="$NEW_FAKE_F" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcF=$(echo "$outF" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcF" "artifact-fetch: an unreachable GitHub API falls back to the source build (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Artifact-fetch:.*falling back to the local source-build path' <<<"$outF"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: an unreachable GitHub API is reported as a soft fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: an unreachable GitHub API is reported as a soft fallback"
    echo "  output: $outF"
fi

installedF_version="$("$WF/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "commit ${HEADF}" <<<"$installedF_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the API-failure fallback still provisioned a freshly-built binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the API-failure fallback still provisioned a freshly-built binary"
    echo "  --version: $installedF_version"
fi

# ------------------------------------------------------------
# G. Already at the latest release: the resolved release's version equals the
#    installed daemon's AND the local source commit matches too — the
#    existing up-to-date no-op contract must hold (exit 0, no download, no
#    build, destination untouched).
# ------------------------------------------------------------
WG="$BASE_WORKDIR/w-fetch-g"
new_fixture "$WG"
HEADG="$(cd "$WG" && git rev-parse --short HEAD)"
write_fake_daemon "$WG/installed-loom-daemon" "$HEADG" "$WG/marker"

WG_ASSETS="$WG/gh-assets"
mkdir -p "$WG_ASSETS"
WG_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WG_ASSETS/$WG_BIN_NAME" "0.15.0" "shouldnt"
sha256_of "$WG_ASSETS/$WG_BIN_NAME" > "$WG_ASSETS/$WG_BIN_NAME.sha256"

WG_FAKEBIN="$WG/fakebin"
mkdir -p "$WG_FAKEBIN"
# write_fake_daemon reports version 0.15.0, so tag v0.15.0 is NOT newer.
write_fake_gh "$WG_FAKEBIN/gh" "v0.15.0" "$WG_ASSETS"

outG=$( cd "$WG" && PATH="$WG_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WG/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcG=$(echo "$outG" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcG" "artifact-fetch: already at the latest release is a no-op (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'is not newer than the installed version' <<<"$outG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: an equal-version release is reported as nothing to fetch"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: an equal-version release is reported as nothing to fetch"
    echo "  output: $outG"
fi

installedG_version="$("$WG/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "commit ${HEADG}" <<<"$installedG_version" && ! grep -q 'Downloading ' <<<"$outG"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact-fetch: the up-to-date no-op downloads nothing and leaves the binary untouched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact-fetch: the up-to-date no-op downloads nothing and leaves the binary untouched"
    echo "  output: $outG"
fi

# ------------------------------------------------------------
# H. --check regression: reports the available RELEASE artifact (exit 3) and
#    writes nothing.
# ------------------------------------------------------------
WH="$BASE_WORKDIR/w-fetch-h"
new_fixture "$WH"
write_fake_daemon "$WH/installed-loom-daemon" "oldc0mm" "$WH/marker"
installedH_before="$("$WH/installed-loom-daemon" --version 2>/dev/null)"

WH_ASSETS="$WH/gh-assets"
mkdir -p "$WH_ASSETS"
WH_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WH_ASSETS/$WH_BIN_NAME" "0.16.0" "artifac8"
sha256_of "$WH_ASSETS/$WH_BIN_NAME" > "$WH_ASSETS/$WH_BIN_NAME.sha256"

WH_FAKEBIN="$WH/fakebin"
mkdir -p "$WH_FAKEBIN"
write_fake_gh "$WH_FAKEBIN/gh" "v0.16.0" "$WH_ASSETS"

outH=$( cd "$WH" && PATH="$WH_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WH/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --check 2>&1; echo "EXIT=$?" )
rcH=$(echo "$outH" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "3" "$rcH" "--check: an available release artifact still reports 'update available' (exit 3)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Update available via release artifact v0.16.0' <<<"$outH"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --check: names the release artifact it would fetch"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --check: names the release artifact it would fetch"
    echo "  output: $outH"
fi

installedH_after="$("$WH/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedH_before" "$installedH_after" "--check: writes nothing even when an artifact is available"

# ------------------------------------------------------------
# I. --dry-run regression: describes the fetch it WOULD perform, never runs
#    cargo, and leaves the destination untouched.
# ------------------------------------------------------------
WI="$BASE_WORKDIR/w-fetch-i"
new_fixture "$WI"
write_fake_daemon "$WI/installed-loom-daemon" "oldc0mm" "$WI/marker"
installedI_before="$("$WI/installed-loom-daemon" --version 2>/dev/null)"

WI_ASSETS="$WI/gh-assets"
mkdir -p "$WI_ASSETS"
WI_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WI_ASSETS/$WI_BIN_NAME" "0.16.0" "artifac9"
sha256_of "$WI_ASSETS/$WI_BIN_NAME" > "$WI_ASSETS/$WI_BIN_NAME.sha256"

WI_FAKEBIN="$WI/fakebin"
mkdir -p "$WI_FAKEBIN"
write_fake_gh "$WI_FAKEBIN/gh" "v0.16.0" "$WI_ASSETS"

outI=$( cd "$WI" && PATH="$WI_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WI/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --dry-run 2>&1; echo "EXIT=$?" )
rcI=$(echo "$outI" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcI" "--dry-run: exits 0 with an artifact available"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '\[dry-run\] Would fetch + verify release artifact v0.16.0' <<<"$outI" \
    && ! grep -q '\[dry-run\] Would run: (cd .* cargo build' <<<"$outI"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --dry-run: describes the artifact fetch instead of a cargo build"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --dry-run: describes the artifact fetch instead of a cargo build"
    echo "  output: $outI"
fi

installedI_after="$("$WI/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedI_before" "$installedI_after" "--dry-run: leaves the destination binary untouched in artifact mode"

# ------------------------------------------------------------
# J. Linux detached signature PRESENT, cosign + a public key both resolvable,
#    verification SUCCEEDS -> the update proceeds and says so (AC3).
# ------------------------------------------------------------
WJ="$BASE_WORKDIR/w-fetch-j"
new_fixture "$WJ"
write_fake_daemon "$WJ/installed-loom-daemon" "oldc0mm" "$WJ/marker"

WJ_ASSETS="$WJ/gh-assets"
mkdir -p "$WJ_ASSETS"
WJ_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WJ_ASSETS/$WJ_BIN_NAME" "0.16.0" "sigokc0"
sha256_of "$WJ_ASSETS/$WJ_BIN_NAME" > "$WJ_ASSETS/$WJ_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WJ_ASSETS/$WJ_BIN_NAME.sig"
echo "-----BEGIN PUBLIC KEY-----fake-----END PUBLIC KEY-----" > "$WJ/cosign.pub"

WJ_FAKEBIN="$WJ/fakebin"
mkdir -p "$WJ_FAKEBIN"
write_fake_gh "$WJ_FAKEBIN/gh" "v0.16.0" "$WJ_ASSETS"
write_fake_cosign "$WJ_FAKEBIN/cosign" 0

outJ=$( cd "$WJ" && PATH="$WJ_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WJ/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY="$WJ/cosign.pub" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcJ=$(echo "$outJ" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcJ" "signature present + cosign verifies: update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'cosign signature verification passed' <<<"$outJ"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present: cosign verification actually ran and passed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present: cosign verification actually ran and passed"
    echo "  output: $outJ"
fi

installedJ_version="$("$WJ/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'commit sigokc0' <<<"$installedJ_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present + verified: the artifact was provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present + verified: the artifact was provisioned"
    echo "  --version: $installedJ_version"
fi

# ------------------------------------------------------------
# K. Linux detached signature PRESENT but cosign verification FAILS -> abort
#    (exit 1), destination untouched. A present-but-invalid signature is
#    tamper evidence, never a soft skip.
# ------------------------------------------------------------
WK="$BASE_WORKDIR/w-fetch-k"
new_fixture "$WK"
write_fake_daemon "$WK/installed-loom-daemon" "oldc0mm" "$WK/marker"
installedK_before="$("$WK/installed-loom-daemon" --version 2>/dev/null)"

WK_ASSETS="$WK/gh-assets"
mkdir -p "$WK_ASSETS"
WK_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WK_ASSETS/$WK_BIN_NAME" "0.16.0" "sigbadc"
sha256_of "$WK_ASSETS/$WK_BIN_NAME" > "$WK_ASSETS/$WK_BIN_NAME.sha256"
echo "tampered-detached-signature" > "$WK_ASSETS/$WK_BIN_NAME.sig"
echo "-----BEGIN PUBLIC KEY-----fake-----END PUBLIC KEY-----" > "$WK/cosign.pub"

WK_FAKEBIN="$WK/fakebin"
mkdir -p "$WK_FAKEBIN"
write_fake_gh "$WK_FAKEBIN/gh" "v0.16.0" "$WK_ASSETS"
write_fake_cosign "$WK_FAKEBIN/cosign" 1
write_fake_cargo "$WK_FAKEBIN/cargo"

outK=$( cd "$WK" && PATH="$WK_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WK/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY="$WK/cosign.pub" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcK=$(echo "$outK" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcK" "signature present but INVALID: aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'cosign signature verification FAILED' <<<"$outK" \
    && ! grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outK"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present but INVALID: never degrades to a source-build fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present but INVALID: never degrades to a source-build fallback"
    echo "  output: $outK"
fi

installedK_after="$("$WK/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedK_before" "$installedK_after" "signature-verification failure leaves the destination binary untouched"

# ------------------------------------------------------------
# L. Linux detached signature PRESENT but no cosign public key is resolvable
#    -> LOUD SKIP, update still proceeds (AC3: an unverifiable-but-optional
#    signature never blocks; the checksum already passed).
# ------------------------------------------------------------
WL="$BASE_WORKDIR/w-fetch-l"
new_fixture "$WL"
write_fake_daemon "$WL/installed-loom-daemon" "oldc0mm" "$WL/marker"

WL_ASSETS="$WL/gh-assets"
mkdir -p "$WL_ASSETS"
WL_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WL_ASSETS/$WL_BIN_NAME" "0.16.0" "nokeyc0"
sha256_of "$WL_ASSETS/$WL_BIN_NAME" > "$WL_ASSETS/$WL_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WL_ASSETS/$WL_BIN_NAME.sig"

WL_FAKEBIN="$WL/fakebin"
mkdir -p "$WL_FAKEBIN"
write_fake_gh "$WL_FAKEBIN/gh" "v0.16.0" "$WL_ASSETS"
write_fake_cosign "$WL_FAKEBIN/cosign" 0

outL=$( cd "$WL" && PATH="$WL_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WL/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcL=$(echo "$outL" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcL" "signature present, no public key: loud skip, update still proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'no cosign public key is resolvable.*SKIPPING verification' <<<"$outL"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present, no public key: the skip is LOUD, not silent"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present, no public key: the skip is LOUD, not silent"
    echo "  output: $outL"
fi

installedL_version="$("$WL/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'commit nokeyc0' <<<"$installedL_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} signature present, no public key: the artifact was still provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} signature present, no public key: the artifact was still provisioned"
    echo "  --version: $installedL_version"
fi

# ------------------------------------------------------------
# M. macOS target, artifact UNSIGNED (no Developer ID secrets were configured
#    for that release) -> soft-skip, update proceeds. This is the case that
#    must NOT be confused with tamper evidence.
# ------------------------------------------------------------
WM="$BASE_WORKDIR/w-fetch-m"
new_fixture "$WM"
write_fake_daemon "$WM/installed-loom-daemon" "oldc0mm" "$WM/marker"

WM_ASSETS="$WM/gh-assets"
mkdir -p "$WM_ASSETS"
WM_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WM_ASSETS/$WM_BIN_NAME" "0.16.0" "unsignd"
sha256_of "$WM_ASSETS/$WM_BIN_NAME" > "$WM_ASSETS/$WM_BIN_NAME.sha256"

WM_FAKEBIN="$WM/fakebin"
mkdir -p "$WM_FAKEBIN"
write_fake_gh "$WM_FAKEBIN/gh" "v0.16.0" "$WM_ASSETS"
write_fake_codesign "$WM_FAKEBIN/codesign" unsigned

outM=$( cd "$WM" && PATH="$WM_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WM/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcM=$(echo "$outM" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcM" "macOS artifact unsigned: soft-skip, update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Downloaded artifact is unsigned' <<<"$outM"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS artifact unsigned: reported as 'unsigned', not as a verification failure"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS artifact unsigned: reported as 'unsigned', not as a verification failure"
    echo "  output: $outM"
fi

installedM_version="$("$WM/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'commit unsignd' <<<"$installedM_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS artifact unsigned: still provisioned (absence never blocks)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS artifact unsigned: still provisioned (absence never blocks)"
    echo "  --version: $installedM_version"
fi

# ------------------------------------------------------------
# N. macOS target, artifact SIGNED and codesign verification succeeds ->
#    proceeds, reporting the verification.
# ------------------------------------------------------------
WN="$BASE_WORKDIR/w-fetch-n"
new_fixture "$WN"
write_fake_daemon "$WN/installed-loom-daemon" "oldc0mm" "$WN/marker"

WN_ASSETS="$WN/gh-assets"
mkdir -p "$WN_ASSETS"
WN_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WN_ASSETS/$WN_BIN_NAME" "0.16.0" "csignok"
sha256_of "$WN_ASSETS/$WN_BIN_NAME" > "$WN_ASSETS/$WN_BIN_NAME.sha256"

WN_FAKEBIN="$WN/fakebin"
mkdir -p "$WN_FAKEBIN"
write_fake_gh "$WN_FAKEBIN/gh" "v0.16.0" "$WN_ASSETS"
write_fake_codesign "$WN_FAKEBIN/codesign" signed-ok

outN=$( cd "$WN" && PATH="$WN_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WN/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcN=$(echo "$outN" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcN" "macOS artifact signed + verified: update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'macOS codesign verification passed' <<<"$outN"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS artifact signed: codesign verification actually ran and passed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS artifact signed: codesign verification actually ran and passed"
    echo "  output: $outN"
fi

# #8008: the post-provision signature-preservation check must NOT fire when
# the destination's signature matches the verified download's (both carry an
# Authority=) -- write_fake_codesign's "signed-ok" mode reports the same
# Authority= line for every target, so this is the "signed-to-signed" no-op
# case the new check must stay silent-success on.
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'retains its Developer ID Authority signature' <<< "$outN"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS artifact signed: post-provision signature-preservation check ran and confirmed no downgrade"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS artifact signed: post-provision signature-preservation check ran and confirmed no downgrade"
    echo "  output: $outN"
fi

# ------------------------------------------------------------
# O. macOS target, artifact SIGNED but codesign verification FAILS -> abort
#    (exit 1), destination untouched. Distinct from the "unsigned" case in M.
# ------------------------------------------------------------
WO="$BASE_WORKDIR/w-fetch-o"
new_fixture "$WO"
write_fake_daemon "$WO/installed-loom-daemon" "oldc0mm" "$WO/marker"
installedO_before="$("$WO/installed-loom-daemon" --version 2>/dev/null)"

WO_ASSETS="$WO/gh-assets"
mkdir -p "$WO_ASSETS"
WO_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WO_ASSETS/$WO_BIN_NAME" "0.16.0" "csignbad"
sha256_of "$WO_ASSETS/$WO_BIN_NAME" > "$WO_ASSETS/$WO_BIN_NAME.sha256"

WO_FAKEBIN="$WO/fakebin"
mkdir -p "$WO_FAKEBIN"
write_fake_gh "$WO_FAKEBIN/gh" "v0.16.0" "$WO_ASSETS"
write_fake_codesign "$WO_FAKEBIN/codesign" signed-bad
write_fake_cargo "$WO_FAKEBIN/cargo"

outO=$( cd "$WO" && PATH="$WO_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WO/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcO=$(echo "$outO" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcO" "macOS artifact signed but INVALID: aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'codesign verification FAILED' <<<"$outO" \
    && ! grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outO"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS signed-but-invalid: treated as tamper evidence, no source-build fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS signed-but-invalid: treated as tamper evidence, no source-build fallback"
    echo "  output: $outO"
fi

installedO_after="$("$WO/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedO_before" "$installedO_after" "macOS signed-but-invalid leaves the destination binary untouched"

# ------------------------------------------------------------
# P. KEYLESS verification is the DEFAULT with NO operator configuration
#    (#5054, the core regression this issue exists for). The release publishes
#    `.sig` + its `.pem` signing certificate; NO LOOM_DAEMON_UPDATE_COSIGN_*
#    env var is set and NO cosign.pub is checked in. Cases J/K/L only ever
#    exercised the LOOM_DAEMON_UPDATE_COSIGN_PUBKEY override, so this is the
#    first coverage of the path a stock install actually takes.
#
#    Asserts the recorded cosign argv, not just the log line: the expected
#    signer identity must be DERIVED from the release slug + tag, and the
#    issuer must be GitHub Actions' OIDC provider. Without that, a verification
#    that "ran" could still be trusting anything.
# ------------------------------------------------------------
WP="$BASE_WORKDIR/w-fetch-p"
new_fixture "$WP"
write_fake_daemon "$WP/installed-loom-daemon" "oldc0mm" "$WP/marker"

WP_ASSETS="$WP/gh-assets"
mkdir -p "$WP_ASSETS"
WP_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WP_ASSETS/$WP_BIN_NAME" "0.16.0" "keyles0"
sha256_of "$WP_ASSETS/$WP_BIN_NAME" > "$WP_ASSETS/$WP_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WP_ASSETS/$WP_BIN_NAME.sig"
echo "-----BEGIN CERTIFICATE-----fake-----END CERTIFICATE-----" > "$WP_ASSETS/$WP_BIN_NAME.pem"

WP_FAKEBIN="$WP/fakebin"
mkdir -p "$WP_FAKEBIN"
WP_COSIGN_ARGS="$WP/cosign-args.log"
write_fake_gh "$WP_FAKEBIN/gh" "v0.16.0" "$WP_ASSETS"
write_fake_cosign_recording "$WP_FAKEBIN/cosign" 0 "$WP_COSIGN_ARGS"

outP=$( cd "$WP" && PATH="$WP_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WP/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcP=$(echo "$outP" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcP" "keyless (sig + cert, no env override): update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'cosign keyless signature verification passed' <<<"$outP" \
    && ! grep -q 'SKIPPING verification' <<<"$outP"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless: real verification runs on a STOCK install (no loud skip, no env override)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless: real verification runs on a STOCK install (no loud skip, no env override)"
    echo "  output: $outP"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -qF -- '--certificate ' "$WP_COSIGN_ARGS" 2>/dev/null \
    && grep -qF -- '--certificate-identity-regexp ^https://github\.com/test-owner/test-repo/\.github/workflows/[^@]+@refs/tags/v0\.16\.0$' "$WP_COSIGN_ARGS" \
    && grep -qF -- '--certificate-oidc-issuer https://token.actions.githubusercontent.com' "$WP_COSIGN_ARGS" \
    && ! grep -qF -- '--key ' "$WP_COSIGN_ARGS"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless: cosign was invoked with the DERIVED signer identity (repo slug + release tag) and the GitHub Actions issuer"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless: cosign was invoked with the DERIVED signer identity (repo slug + release tag) and the GitHub Actions issuer"
    echo "  cosign argv: $(cat "$WP_COSIGN_ARGS" 2>/dev/null)"
fi

installedP_version="$("$WP/installed-loom-daemon" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'commit keyles0' <<<"$installedP_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless verified: the artifact was provisioned"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless verified: the artifact was provisioned"
    echo "  --version: $installedP_version"
fi

# ------------------------------------------------------------
# Q. Keyless verification FAILS (wrong signer / tampered blob) -> abort
#    (exit 1), destination untouched, no source-build fallback. The keyless
#    twin of case K: enforcement must be real in BOTH directions, or "default
#    verification" is theatre.
# ------------------------------------------------------------
WQ="$BASE_WORKDIR/w-fetch-q"
new_fixture "$WQ"
write_fake_daemon "$WQ/installed-loom-daemon" "oldc0mm" "$WQ/marker"
installedQ_before="$("$WQ/installed-loom-daemon" --version 2>/dev/null)"

WQ_ASSETS="$WQ/gh-assets"
mkdir -p "$WQ_ASSETS"
WQ_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WQ_ASSETS/$WQ_BIN_NAME" "0.16.0" "keybad0"
sha256_of "$WQ_ASSETS/$WQ_BIN_NAME" > "$WQ_ASSETS/$WQ_BIN_NAME.sha256"
echo "tampered-detached-signature" > "$WQ_ASSETS/$WQ_BIN_NAME.sig"
echo "-----BEGIN CERTIFICATE-----wrong-signer-----END CERTIFICATE-----" > "$WQ_ASSETS/$WQ_BIN_NAME.pem"

WQ_FAKEBIN="$WQ/fakebin"
mkdir -p "$WQ_FAKEBIN"
write_fake_gh "$WQ_FAKEBIN/gh" "v0.16.0" "$WQ_ASSETS"
write_fake_cosign "$WQ_FAKEBIN/cosign" 1
write_fake_cargo "$WQ_FAKEBIN/cargo"

outQ=$( cd "$WQ" && PATH="$WQ_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WQ/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcQ=$(echo "$outQ" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcQ" "keyless verification failure: aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'cosign keyless signature verification FAILED' <<<"$outQ" \
    && ! grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outQ"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} keyless failure: treated as tamper evidence, never a source-build fallback"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} keyless failure: treated as tamper evidence, never a source-build fallback"
    echo "  output: $outQ"
fi

installedQ_after="$("$WQ/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedQ_before" "$installedQ_after" "keyless verification failure leaves the destination binary untouched"

# ------------------------------------------------------------
# R. KEY-mode default resolution with NO env override (#5054): a key-signed
#    release (bare `.sig`, no `.pem`) plus a checked-in `.loom/cosign.pub`
#    verifies for real -- proving resolve_cosign_pubkey()'s conventional-path
#    branch works, which no prior test covered (J/K set the env override, L
#    resolved nothing at all).
# ------------------------------------------------------------
WR="$BASE_WORKDIR/w-fetch-r"
new_fixture "$WR"
write_fake_daemon "$WR/installed-loom-daemon" "oldc0mm" "$WR/marker"

WR_ASSETS="$WR/gh-assets"
mkdir -p "$WR_ASSETS"
WR_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WR_ASSETS/$WR_BIN_NAME" "0.16.0" "convkey"
sha256_of "$WR_ASSETS/$WR_BIN_NAME" > "$WR_ASSETS/$WR_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WR_ASSETS/$WR_BIN_NAME.sig"
echo "-----BEGIN PUBLIC KEY-----fake-----END PUBLIC KEY-----" > "$WR/.loom/cosign.pub"

WR_FAKEBIN="$WR/fakebin"
mkdir -p "$WR_FAKEBIN"
WR_COSIGN_ARGS="$WR/cosign-args.log"
write_fake_gh "$WR_FAKEBIN/gh" "v0.16.0" "$WR_ASSETS"
write_fake_cosign_recording "$WR_FAKEBIN/cosign" 0 "$WR_COSIGN_ARGS"

outR=$( cd "$WR" && PATH="$WR_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WR/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcR=$(echo "$outR" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcR" "key mode via checked-in .loom/cosign.pub (no env override): update proceeds (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'cosign signature verification passed' <<<"$outR" \
    && grep -qF -- "--key $WR/.loom/cosign.pub" "$WR_COSIGN_ARGS" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} key mode: the checked-in .loom/cosign.pub resolves with no env override"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} key mode: the checked-in .loom/cosign.pub resolves with no env override"
    echo "  output: $outR"
    echo "  cosign argv: $(cat "$WR_COSIGN_ARGS" 2>/dev/null)"
fi

# ------------------------------------------------------------
# S. The ARTIFACT's shape selects the mode, never local config (#5054): a
#    keyless release (`.sig` + `.pem`) is verified keylessly EVEN WHEN a stale
#    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY is set. The inverse (letting a leftover
#    key win) would turn every operator's stale env var into a fleet-wide false
#    "tamper" abort.
# ------------------------------------------------------------
WS="$BASE_WORKDIR/w-fetch-s"
new_fixture "$WS"
write_fake_daemon "$WS/installed-loom-daemon" "oldc0mm" "$WS/marker"

WS_ASSETS="$WS/gh-assets"
mkdir -p "$WS_ASSETS"
WS_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WS_ASSETS/$WS_BIN_NAME" "0.16.0" "shapes0"
sha256_of "$WS_ASSETS/$WS_BIN_NAME" > "$WS_ASSETS/$WS_BIN_NAME.sha256"
echo "fake-detached-signature" > "$WS_ASSETS/$WS_BIN_NAME.sig"
echo "-----BEGIN CERTIFICATE-----fake-----END CERTIFICATE-----" > "$WS_ASSETS/$WS_BIN_NAME.pem"
echo "-----BEGIN PUBLIC KEY-----stale-----END PUBLIC KEY-----" > "$WS/stale-cosign.pub"

WS_FAKEBIN="$WS/fakebin"
mkdir -p "$WS_FAKEBIN"
WS_COSIGN_ARGS="$WS/cosign-args.log"
write_fake_gh "$WS_FAKEBIN/gh" "v0.16.0" "$WS_ASSETS"
write_fake_cosign_recording "$WS_FAKEBIN/cosign" 0 "$WS_COSIGN_ARGS"

outS=$( cd "$WS" && PATH="$WS_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WS/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    LOOM_DAEMON_UPDATE_COSIGN_PUBKEY="$WS/stale-cosign.pub" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcS=$(echo "$outS" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcS" "keyless release + stale pubkey env: still verifies keylessly (exit 0)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'cosign keyless signature verification passed' <<<"$outS" \
    && ! grep -qF -- '--key ' "$WS_COSIGN_ARGS" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} artifact shape (not local config) selects the verification mode"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} artifact shape (not local config) selects the verification mode"
    echo "  output: $outS"
    echo "  cosign argv: $(cat "$WS_COSIGN_ARGS" 2>/dev/null)"
fi

# ------------------------------------------------------------
# T. Release-cadence gap visibility (#6010): the newest resolved release is
#    NEWER than the installed binary (so ARTIFACT_MODE still wins and the
#    update proceeds normally) but OLDER than the source tree's own VERSION
#    file. Both the plain-run advisory and the --check summary must name the
#    gap so an operator can tell, before running a forced --fetch elsewhere,
#    that the artifact path cannot reach current source yet.
# ------------------------------------------------------------
WT="$BASE_WORKDIR/w-fetch-t"
new_fixture "$WT"
write_fake_daemon "$WT/installed-loom-daemon" "oldc0mm" "$WT/marker"
echo "0.20.0" > "$WT/VERSION"

WT_ASSETS="$WT/gh-assets"
mkdir -p "$WT_ASSETS"
WT_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WT_ASSETS/$WT_BIN_NAME" "0.16.0" "artifact-t"
sha256_of "$WT_ASSETS/$WT_BIN_NAME" > "$WT_ASSETS/$WT_BIN_NAME.sha256"

WT_FAKEBIN="$WT/fakebin"
mkdir -p "$WT_FAKEBIN"
# write_fake_daemon reports version 0.15.0, so tag v0.16.0 IS newer than
# installed -- but still behind the fixture's VERSION file (0.20.0) above.
write_fake_gh "$WT_FAKEBIN/gh" "v0.16.0" "$WT_ASSETS"

outT=$( cd "$WT" && PATH="$WT_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WT/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcT=$(echo "$outT" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcT" "release-gap: a still-usable (newer-than-installed) artifact update still exits 0"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Artifact path cannot reach current source' <<<"$outT" \
    && grep -q '0.20.0' <<<"$outT" && grep -q 'v0.16.0' <<<"$outT"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} release-gap: plain run warns when the resolved release is behind source VERSION"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} release-gap: plain run warns when the resolved release is behind source VERSION"
    echo "  output: $outT"
fi

WT2="$BASE_WORKDIR/w-fetch-t2"
new_fixture "$WT2"
write_fake_daemon "$WT2/installed-loom-daemon" "oldc0mm" "$WT2/marker"
echo "0.20.0" > "$WT2/VERSION"
WT2_ASSETS="$WT2/gh-assets"
mkdir -p "$WT2_ASSETS"
write_fake_artifact_daemon "$WT2_ASSETS/$WT_BIN_NAME" "0.16.0" "artifact-t2"
sha256_of "$WT2_ASSETS/$WT_BIN_NAME" > "$WT2_ASSETS/$WT_BIN_NAME.sha256"
WT2_FAKEBIN="$WT2/fakebin"
mkdir -p "$WT2_FAKEBIN"
write_fake_gh "$WT2_FAKEBIN/gh" "v0.16.0" "$WT2_ASSETS"

outT2=$( cd "$WT2" && PATH="$WT2_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WT2/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --check 2>&1; echo "EXIT=$?" )
rcT2=$(echo "$outT2" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "3" "$rcT2" "release-gap: --check still reports the available artifact update (exit 3)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Release gap: installed 0.15.0, newest release 0.16.0, source 0.20.0' <<<"$outT2"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} release-gap: --check summarizes installed/release/source together"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} release-gap: --check summarizes installed/release/source together"
    echo "  output: $outT2"
fi

# ------------------------------------------------------------
# U. Release-cadence gap visibility (#6010), forced --fetch: the newest
#    resolved release is OLDER than the installed binary (the real #6010
#    incident shape -- a host already built past the last cut release) AND
#    older than source VERSION. The existing hard-fail (exit 1, never falls
#    back to a source build) must additionally NAME the source-gap cause.
# ------------------------------------------------------------
WU="$BASE_WORKDIR/w-fetch-u"
new_fixture "$WU"
write_fake_artifact_daemon "$WU/installed-loom-daemon" "0.18.12" "instcommit"
echo "0.18.13" > "$WU/VERSION"

WU_ASSETS="$WU/gh-assets"
mkdir -p "$WU_ASSETS"
WU_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WU_ASSETS/$WU_BIN_NAME" "0.18.0" "artifact-u"
sha256_of "$WU_ASSETS/$WU_BIN_NAME" > "$WU_ASSETS/$WU_BIN_NAME.sha256"

WU_FAKEBIN="$WU/fakebin"
mkdir -p "$WU_FAKEBIN"
write_fake_gh "$WU_FAKEBIN/gh" "v0.18.0" "$WU_ASSETS"
write_fake_cargo "$WU_FAKEBIN/cargo"

outU=$( cd "$WU" && PATH="$WU_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WU/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart --fetch 2>&1; echo "EXIT=$?" )
rcU=$(echo "$outU" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcU" "release-gap: forced --fetch behind BOTH installed and source still hard-fails (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'no usable release artifact was resolved' <<<"$outU" \
    && grep -q 'Cause: the newest release (0.18.0) is behind this source tree'"'"'s VERSION (0.18.13)' <<<"$outU"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} release-gap: forced --fetch hard-fail names the source-gap cause"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} release-gap: forced --fetch hard-fail names the source-gap cause"
    echo "  output: $outU"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outU"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} release-gap: forced --fetch hard-fail never falls back to 'cargo build'"
    echo "  output: $outU"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} release-gap: forced --fetch hard-fail never falls back to 'cargo build'"
fi

# ------------------------------------------------------------
# V. Post-provision signature-preservation assertion (#8008, the follow-up to
#    #7932): the VERIFIED download carries a Developer ID Authority=
#    signature, but the PROVISIONED DESTINATION reports ad-hoc/no Authority=
#    afterward -- exactly the shape of the #7932 regression (sign_daemon_binary's
#    `codesign ... | grep -q` pipe form silently reported 141 under
#    `set -o pipefail`, so it force-resigned every Developer ID-signed release
#    artifact ad-hoc on every provision, unnoticed because nothing compared
#    the destination's signature against the verified download's). Must
#    hard-fail (exit 5, same class as the existing version-mismatch check),
#    not silently succeed.
# ------------------------------------------------------------
WV="$BASE_WORKDIR/w-fetch-v"
new_fixture "$WV"
write_fake_daemon "$WV/installed-loom-daemon" "oldc0mm" "$WV/marker"

WV_ASSETS="$WV/gh-assets"
mkdir -p "$WV_ASSETS"
WV_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WV_ASSETS/$WV_BIN_NAME" "0.16.0" "sigdown0"
sha256_of "$WV_ASSETS/$WV_BIN_NAME" > "$WV_ASSETS/$WV_BIN_NAME.sha256"

WV_FAKEBIN="$WV/fakebin"
mkdir -p "$WV_FAKEBIN"
write_fake_gh "$WV_FAKEBIN/gh" "v0.16.0" "$WV_ASSETS"
# Authority= for every codesign target EXCEPT the provisioning destination
# itself, which reports ad-hoc -- simulating a post-provision downgrade.
write_fake_codesign_signature_downgrade "$WV_FAKEBIN/codesign" "$WV/installed-loom-daemon"

outV=$( cd "$WV" && PATH="$WV_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WV/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcV=$(echo "$outV" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "5" "$rcV" "macOS signature-preservation: Authority pre-provision, ad-hoc post-provision hard-fails (exit 5)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Post-provision verification FAILED.*Developer ID (Authority=) signature' <<< "$outV" \
    && grep -q 'DOWNGRADED the signature' <<< "$outV"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS signature-preservation: downgrade reported explicitly, names the #7932 regression class"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS signature-preservation: downgrade reported explicitly, names the #7932 regression class"
    echo "  output: $outV"
fi


# ------------------------------------------------------------
# W. (#7609) A forced --fetch is NOT blocked by local-checkout state. The
#    parent suite's test 38 proves the default (source-build) path hard-aborts
#    exit 1 on a diverged local commit. --fetch installs a published artifact
#    and never compiles anything, so the same checkout must NOT block it: this
#    is the mode the daemon's artifact-first auto_update tick rolls with, and
#    letting a dirty/diverged/behind checkout veto it would reintroduce
#    exactly the fleet-wide stall #7609 exists to end, one level down.
#
#    Moved here from test-loom-daemon-update.sh by #8028, the same move
#    #7977 made for --resolve-json above: fetch_and_verify_artifact() now
#    delegates to `loom-daemon release-fetch`, so this scenario needs the
#    binary this job builds, which the parent hermetic suite must not require.
# ------------------------------------------------------------
WW="$BASE_WORKDIR/w-fetch-w"
BAREW="$BASE_WORKDIR/w-fetch-w-origin.git"
new_fixture_with_origin "$WW" "$BAREW"
# Advance origin/main with real content...
TMPCLONEW="$(mktemp -d)"
git clone -q "$BAREW" "$TMPCLONEW"
echo "origin-value" > "$TMPCLONEW/origin-only-file.txt"
( cd "$TMPCLONEW" && git add origin-only-file.txt \
    && git -c user.email=test@test -c user.name=test commit -q -m "origin real change" \
    && git push -q origin HEAD:refs/heads/main )
rm -rf "$TMPCLONEW"
# ...and diverge locally with DIFFERENT real content, so `git merge --ff-only`
# genuinely refuses (the test-38 hard-abort shape), plus an untracked stray of
# the kind that shut the gate on two fleet hosts.
echo "local-value" > "$WW/local-only-file.txt"
( cd "$WW" && git add local-only-file.txt \
    && git -c user.email=test@test -c user.name=test commit -q -m "local diverged commit (real content)" )
echo "" > "$WW/pnpm-lock.yaml"
HEAD_BEFOREW="$(cd "$WW" && git rev-parse --short HEAD)"

INSTALLEDW="$WW/installed-loom-daemon"
write_fake_artifact_daemon "$INSTALLEDW" "0.19.21" "deadbee"
WW_ASSETS="$WW/gh-assets"
mkdir -p "$WW_ASSETS"
WW_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WW_ASSETS/$WW_BIN_NAME" "0.19.24" "cafe123"
sha256_of "$WW_ASSETS/$WW_BIN_NAME" > "$WW_ASSETS/$WW_BIN_NAME.sha256"

WW_FAKEBIN="$WW/fakebin"
mkdir -p "$WW_FAKEBIN"
write_fake_gh "$WW_FAKEBIN/gh" "v0.19.24" "$WW_ASSETS"
write_fake_cargo "$WW_FAKEBIN/cargo"

outW=$( cd "$WW" && PATH="$WW_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$INSTALLEDW" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart --fetch 2>&1; echo "EXIT=$?" )
rcW=$(echo "$outW" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcW" "--fetch (#7609): a diverged/dirty/behind checkout does NOT block an artifact roll (exit 0)"

installedW_version="$("$INSTALLEDW" --version 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q "0.19.24" <<<"$installedW_version"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --fetch (#7609): the release artifact was actually provisioned over the stale binary"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --fetch (#7609): the release artifact was actually provisioned over the stale binary"
    echo "  --version: $installedW_version"
    echo "  output: $outW"
fi

HEAD_AFTERW="$(cd "$WW" && git rev-parse --short HEAD)"
assert_eq "$HEAD_BEFOREW" "$HEAD_AFTERW" "--fetch (#7609): leaves the local checkout's HEAD completely untouched (no ff-sync side effect)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'never builds from this checkout' <<<"$outW"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --fetch (#7609): says why the behind-origin checkout was not fast-forwarded"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --fetch (#7609): says why the behind-origin checkout was not fast-forwarded"
    echo "  output: $outW"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outW"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --fetch (#7609): never compiles on the artifact path"
    echo "  output: $outW"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --fetch (#7609): never compiles on the artifact path"
fi


# ------------------------------------------------------------
# X. (#8197) A release that PUBLISHES a `.sig` it cannot serve must refuse the
#    artifact (exit 1) rather than silently degrade to checksum-only
#    verification, and must leave the running daemon untouched. The twin of
#    test A above: there the `.sig` is genuinely absent and the update
#    proceeds; here it is listed-but-unfetchable and the update stops. Both
#    directions are asserted because a fix in one alone would either break
#    every unsigned release or leave the downgrade open.
# ------------------------------------------------------------
WX="$BASE_WORKDIR/w-fetch-x"
new_fixture "$WX"
write_fake_daemon "$WX/installed-loom-daemon" "oldc0mm" "$WX/marker"
installedX_before="$("$WX/installed-loom-daemon" --version 2>/dev/null)"

WX_ASSETS="$WX/gh-assets"
mkdir -p "$WX_ASSETS"
WX_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WX_ASSETS/$WX_BIN_NAME" "0.16.0" "artifacx"
sha256_of "$WX_ASSETS/$WX_BIN_NAME" > "$WX_ASSETS/$WX_BIN_NAME.sha256"

WX_FAKEBIN="$WX/fakebin"
mkdir -p "$WX_FAKEBIN"
# The `.sig` is LISTED by the release but never written to the assets dir, so
# every download of it fails -- a 500, a timeout, a truncated transfer.
write_fake_gh "$WX_FAKEBIN/gh" "v0.16.0" "$WX_ASSETS" "$WX_BIN_NAME.sig"

outX=$( cd "$WX" && PATH="$WX_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WX/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcX=$(echo "$outX" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "1" "$rcX" "published-but-unfetchable .sig: aborts the update (exit 1)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'UNAVAILABLE, not absent' <<<"$outX"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} published-but-unfetchable .sig: says the signature is unavailable, not absent"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} published-but-unfetchable .sig: says the signature is unavailable, not absent"
    echo "  output: $outX"
fi

installedX_after="$("$WX/installed-loom-daemon" --version 2>/dev/null)"
assert_eq "$installedX_before" "$installedX_after" "published-but-unfetchable .sig leaves the destination binary untouched"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q 'Rebuilding loom-daemon (cargo build' <<<"$outX"; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} published-but-unfetchable .sig: refusal is never a source-build fallback"
    echo "  output: $outX"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} published-but-unfetchable .sig: refusal is never a source-build fallback"
fi

# ------------------------------------------------------------
# Y. (#8197) The `KEY=value` stdout contract itself, asserted against
#    `release-fetch` directly rather than through the wrapper: the new
#    SIGNATURE= key is present AND every pre-existing key a consumer already
#    parses is still there, in a release that publishes no signature at all.
#    Additive-only is the contract; this is what proves it.
# ------------------------------------------------------------
# shellcheck source=../lib/locate-daemon-bin.sh
source "$LOOM_REPO_ROOT/defaults/scripts/lib/locate-daemon-bin.sh"
RF_BIN="$(loom_resolve_self_daemon_bin)"

WY="$BASE_WORKDIR/w-fetch-y"
new_fixture "$WY"
WY_ASSETS="$WY/gh-assets"
mkdir -p "$WY_ASSETS"
WY_BIN_NAME="loom-daemon-x86_64-unknown-linux-gnu"
write_fake_artifact_daemon "$WY_ASSETS/$WY_BIN_NAME" "0.16.0" "artifacy"
sha256_of "$WY_ASSETS/$WY_BIN_NAME" > "$WY_ASSETS/$WY_BIN_NAME.sha256"
WY_FAKEBIN="$WY/fakebin"
mkdir -p "$WY_FAKEBIN"
write_fake_gh "$WY_FAKEBIN/gh" "v0.16.0" "$WY_ASSETS"

outY=$( cd "$WY" && PATH="$WY_FAKEBIN:$TEST_PATH" "$RF_BIN" release-fetch \
    --repo-root "$WY" --target "x86_64-unknown-linux-gnu" \
    --repo "test-owner/test-repo" --tag "v0.16.0" 2>/dev/null )
rcY=$?
assert_eq "0" "$rcY" "release-fetch stdout contract: an unsigned release still exits 0"

for key in BIN_PATH TMP_DIR VERSION_OUTPUT COMMIT HAD_AUTHORITY; do
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -q "^${key}=" <<<"$outY"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "${GREEN}✓${NC} release-fetch stdout contract: pre-existing key ${key}= is unaffected"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "${RED}✗${NC} release-fetch stdout contract: pre-existing key ${key}= is unaffected"
        echo "  stdout: $outY"
    fi
done

TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '^SIGNATURE=skipped$' <<<"$outY"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} release-fetch stdout contract: reports SIGNATURE=skipped for an unsigned release"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} release-fetch stdout contract: reports SIGNATURE=skipped for an unsigned release"
    echo "  stdout: $outY"
fi

# release-fetch deliberately hands its scratch dir to the caller (the wrapper's
# EXIT trap owns it in production); this suite is that caller here.
rm -rf "$(grep '^TMP_DIR=' <<<"$outY" | cut -d= -f2-)"

# The containment guard armed above (loom_fixture_scratch_root) is exercised by
# its own sibling suite, test-daemon-update-fixture-containment.sh — it belongs
# to lib/daemon-update-fixtures.sh, not to `--fetch`, and this suite is already
# at the file-size ratchet's threshold.

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
