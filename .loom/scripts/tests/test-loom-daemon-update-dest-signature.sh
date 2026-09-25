#!/usr/bin/env bash
# test-loom-daemon-update-dest-signature.sh -- loom-daemon-update.sh's
# POST-PROVISION destination signature check (verify_destination_artifact()'s
# signature-preservation assertion): scenarios V (#8008) and V2 (#8770), split
# out of test-loom-daemon-update-fetch.sh by #8770.
#
# WHY THIS IS A SEPARATE SUITE
#
# Subject, then size. These two scenarios are not about `--fetch` at all: they
# assert what happens AFTER provisioning, on the destination binary, on Darwin
# only. They lived in the fetch suite because that is where a Darwin-target
# fixture already existed, not because they test artifact fetching.
#
# The forcing function was the file-size ratchet
# (.loom/docs/file-size-policy.md): test-loom-daemon-update-fetch.sh sat at 999
# code lines -- one short of the 1000-line freeze -- and its own closing
# comment already said so. Adding #8770's scenario crossed it. Extracting a
# coherent subject into a sibling is the remedy that policy names first, and
# the same one #7977 (--resolve-json) and #8028 (--fetch) already applied to
# this suite's parent twice.
#
# Like its two siblings it needs a BUILT loom-daemon: lib/daemon-update-fixtures.sh
# pins $LOOM_DAEMON_BIN through require-daemon-bin.sh, and the flows here reach
# `loom-daemon release-fetch` (#8028). It is therefore wired in ci.yml's
# "Native Port Suites" job, not in run-ci-suites.sh, and it FAILS rather than
# SKIPs without a binary.
#
# The fixtures live in lib/daemon-update-fixtures.sh, shared rather than
# copied -- a fake `gh`/`codesign` that drifts in one of several copies is a
# test that proves nothing, which is the class of bug #7977 hit.
#
# Hermetic otherwise: no network, no live forge, no tokens. Every read goes
# to a stub.
#
# Usage:
#   ./.loom/scripts/tests/test-loom-daemon-update-dest-signature.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
LOOM_REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/../cli" && pwd)"
UPDATE_SCRIPT="$CLI_DIR/loom-daemon-update.sh"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
START_SCRIPT="$CLI_DIR/loom-daemon-start.sh"

# Both scenarios invoke the real loom-daemon-start.sh/-stop.sh via a throwaway
# fixture (new_fixture), but only ever with --no-restart -- neither reaches a
# real restart. The launchd sandbox is still applied for the same reason the
# fetch sibling applies it: a scratch label no launchd lookup could resolve to
# the operator's real job.
# shellcheck source=lib/launchd-sandbox.sh
source "$SCRIPT_DIR/lib/launchd-sandbox.sh"
export LOOM_LAUNCHD_LABEL="$(launchd_sandbox_new_label)"
export LOOM_DAEMON_LAUNCHD=0
export LOOM_SYSTEMD_UNIT="loom-daemon-dest-sig-test-$$.service"

# The fake-binary / fake-forge fixtures, shared with the parent suite and its
# fetch / resolve-json siblings so all of them build identical ones from a
# single definition (#7977, #8028).
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
# Post-provision signature preservation (Darwin only). Both scenarios pin
# LOOM_DAEMON_UPDATE_GH_REPO (bypassing git-remote parsing) and
# LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin", so the fetched artifact is
# a Darwin one and verify_destination_artifact()'s codesign branch is reached
# with $ARTIFACT_SIGNATURE_HAD_AUTHORITY=true.
# ============================================================

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
# V2. (#8770) A post-provision `codesign -dvvv` that never answers (a
#     contended host, the same class #8754 fixed one layer up in
#     verify_darwin) must be BOUNDED and treated as INCONCLUSIVE -- a warn
#     and a skip of the downgrade check -- never the exit-5 "DOWNGRADED"
#     accusation the empty report used to produce. LOOM_DAEMON_UPDATE_DEST_SIG_TIMEOUT_SECS
#     shortens the deadline so this test does not wait out the 30s production
#     ceiling; the fake codesign sleeps well past it on the destination path
#     only, so every OTHER codesign call (the pre-provision verify) still
#     answers immediately and the run reaches post-provision verification at
#     all.
# ------------------------------------------------------------
WV2="$BASE_WORKDIR/w-fetch-v2"
new_fixture "$WV2"
write_fake_daemon "$WV2/installed-loom-daemon" "oldc0mm" "$WV2/marker"

WV2_ASSETS="$WV2/gh-assets"
mkdir -p "$WV2_ASSETS"
WV2_BIN_NAME="loom-daemon-aarch64-apple-darwin"
write_fake_artifact_daemon "$WV2_ASSETS/$WV2_BIN_NAME" "0.16.0" "sigdown0"
sha256_of "$WV2_ASSETS/$WV2_BIN_NAME" > "$WV2_ASSETS/$WV2_BIN_NAME.sha256"

WV2_FAKEBIN="$WV2/fakebin"
mkdir -p "$WV2_FAKEBIN"
write_fake_gh "$WV2_FAKEBIN/gh" "v0.16.0" "$WV2_ASSETS"
# Authority= for every codesign target EXCEPT the provisioning destination,
# which hangs for 10s -- long enough to outlive the 1s deadline below.
write_fake_codesign_signature_hang "$WV2_FAKEBIN/codesign" "$WV2/installed-loom-daemon" 10

outV2=$( cd "$WV2" && PATH="$WV2_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$WV2/installed-loom-daemon" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="aarch64-apple-darwin" \
    LOOM_DAEMON_UPDATE_DEST_SIG_TIMEOUT_SECS=1 \
    timeout 20 bash "$UPDATE_SCRIPT" --no-restart 2>&1; echo "EXIT=$?" )
rcV2=$(echo "$outV2" | grep -o 'EXIT=[0-9]*' | cut -d= -f2)
assert_eq "0" "$rcV2" "macOS signature-preservation: a codesign that never answers is bounded and does NOT hard-fail (exit 0, not exit 5)"

TESTS_RUN=$((TESTS_RUN + 1))
if grep -qi 'timed out' <<< "$outV2" && ! grep -q 'DOWNGRADED the signature' <<< "$outV2"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} macOS signature-preservation: a timed-out codesign is reported as inconclusive, never as a downgrade"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} macOS signature-preservation: a timed-out codesign is reported as inconclusive, never as a downgrade"
    echo "  output: $outV2"
fi

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
