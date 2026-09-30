#!/usr/bin/env bash
# test-loom-daemon-update-resolve-json.sh - the --resolve-json mode of
# loom-daemon-update.sh (#7609), split out of test-loom-daemon-update.sh by
# #7810 PR 5.
#
# WHY THIS IS A SEPARATE SUITE
#
# As of #7977 this mode DELEGATES to `loom-daemon release-resolve`, so these
# assertions need a built loom-daemon. Its parent suite must not: it drives the
# real daemon lifecycle scripts, which is why run-ci-suites.sh owns it in
# LIVE_DAEMON_GUARDED_SUITES (#6386), and why test-run-ci-suites-daemon-guard.sh
# pins that membership as an explicit literal so a drop is a test failure rather
# than an invisible regression.
#
# Moving the whole parent to a Rust-capable job to satisfy 27 assertions would
# have reduced that guard's scope, and the tripwire correctly said so. Splitting
# is the resolution: the parent stays hermetic and guarded where it belongs, and
# only the assertions that need a binary move to the job that builds one.
#
# The fixtures both suites use live in lib/daemon-update-fixtures.sh, shared
# rather than copied — a fake `gh` that drifts in one of two copies is a test
# that proves nothing, which is the class of bug #7977 hit.
#
# Hermetic otherwise: no network, no live forge, no tokens. Every read goes to
# the stub.
#
# Usage:
#   ./.loom/scripts/tests/test-loom-daemon-update-resolve-json.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
LOOM_REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
CLI_DIR="$(cd "$SCRIPT_DIR/../cli" && pwd)"
UPDATE_SCRIPT="$CLI_DIR/loom-daemon-update.sh"
# shellcheck disable=SC2034  # read by lib/daemon-update-fixtures.sh, sourced below
START_SCRIPT="$CLI_DIR/loom-daemon-start.sh"

# This mode is strictly read-only, but the fixtures still copy the real
# lifecycle scripts, so keep the same launchd sandbox the parent uses: a
# scratch label no launchd lookup could resolve to the operator's real job.
# shellcheck source=lib/launchd-sandbox.sh
source "$SCRIPT_DIR/lib/launchd-sandbox.sh"
export LOOM_LAUNCHD_LABEL="$(launchd_sandbox_new_label)"
export LOOM_DAEMON_LAUNCHD=0
export LOOM_SYSTEMD_UNIT="loom-daemon-resolve-json-test-$$.service"

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
# 81. --resolve-json (#7609): READ-ONLY artifact resolution for the daemon's
#     artifact-first auto_update tick. Must print exactly one JSON object on
#     stdout (every other line on stderr), report the release AND the
#     installed binary (version + sha256, which is what lets the daemon
#     detect "same version, different bytes"), and must not fetch the binary,
#     build, provision, restart, or touch the git checkout.
# ============================================================
W81="$BASE_WORKDIR/w81"
new_fixture "$W81"
INSTALLED81="$W81/installed/loom-daemon"
mkdir -p "$W81/installed"
write_fake_artifact_daemon "$INSTALLED81" "0.19.21" "deadbee"
echo "0.19.30" > "$W81/VERSION"

W81_ASSETS="$W81/gh-assets"
mkdir -p "$W81_ASSETS"
write_fake_artifact_daemon "$W81_ASSETS/loom-daemon-x86_64-unknown-linux-gnu" "0.19.24" "cafe123"
sha256_of "$W81_ASSETS/loom-daemon-x86_64-unknown-linux-gnu" \
    > "$W81_ASSETS/loom-daemon-x86_64-unknown-linux-gnu.sha256"
ASSET_SHA81="$(awk 'NR==1{print $1}' "$W81_ASSETS/loom-daemon-x86_64-unknown-linux-gnu.sha256")"

W81_FAKEBIN="$W81/fakebin"
mkdir -p "$W81_FAKEBIN"
write_fake_gh "$W81_FAKEBIN/gh" "v0.19.24" "$W81_ASSETS"
write_fake_cargo "$W81_FAKEBIN/cargo"

W81_STDERR="$W81/resolve.stderr"
out81=$( cd "$W81" && PATH="$W81_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$INSTALLED81" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --resolve-json 2>"$W81_STDERR" )
rc81=$?
assert_eq "0" "$rc81" "--resolve-json: exits 0 when a release artifact resolves"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(printf '%s' "$out81" | wc -l | tr -d ' ')" == "0" ]] && [[ "$out81" == \{*\} ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --resolve-json: stdout is exactly one JSON object (everything else went to stderr)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --resolve-json: stdout is exactly one JSON object (everything else went to stderr)"
    echo "  stdout: $out81"
fi

# Field-by-field, parsed the same way the daemon parses it (a real JSON
# parser when python3 is available; a grep fallback otherwise so the suite
# still runs on a host without it).
json81_field() {
    local key="$1"
    if command -v python3 >/dev/null 2>&1; then
        printf '%s' "$out81" | python3 -c "import json,sys; v=json.load(sys.stdin).get('$key'); print('' if v is None else v)" 2>/dev/null
    else
        printf '%s' "$out81" | grep -oE "\"$key\":\"[^\"]*\"" | head -n1 | sed -E "s/\"$key\":\"(.*)\"/\1/"
    fi
}
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '"ok":true' <<<"$out81"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --resolve-json: ok is true when a release resolves"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --resolve-json: ok is true when a release resolves"
    echo "  stdout: $out81"
fi
assert_eq "0.19.24" "$(json81_field version)" "--resolve-json: reports the resolved release version"
assert_eq "v0.19.24" "$(json81_field tag)" "--resolve-json: reports the resolved release tag"
assert_eq "0.19.21" "$(json81_field installed_version)" "--resolve-json: reports the INSTALLED version"
assert_eq "$ASSET_SHA81" "$(json81_field asset_sha256)" "--resolve-json: reports the release's published sha256 (from the .sha256 asset)"
assert_eq "$(sha256_of "$INSTALLED81" | awk '{print $1}')" "$(json81_field installed_sha256)" "--resolve-json: reports the installed binary's own sha256"
assert_eq "0.19.30" "$(json81_field source_version)" "--resolve-json: reports the source tree's VERSION"
assert_eq "x86_64-unknown-linux-gnu" "$(json81_field target)" "--resolve-json: reports the resolved target triple"

TESTS_RUN=$((TESTS_RUN + 1))
installed81_after="$("$INSTALLED81" --version 2>/dev/null)"
if grep -q "0.19.21" <<<"$installed81_after" \
    && ! grep -q 'Rebuilding loom-daemon (cargo build' "$W81_STDERR" \
    && ! grep -q 'Downloading loom-daemon-' "$W81_STDERR"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --resolve-json: nothing was built, downloaded, or provisioned (read-only)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --resolve-json: nothing was built, downloaded, or provisioned (read-only)"
    echo "  installed --version after: $installed81_after"
    echo "  stderr: $(cat "$W81_STDERR")"
fi

# ============================================================
# 82. --resolve-json with NO resolvable release (unreachable/rate-limited
#     GitHub API): still prints the JSON object, with ok=false and a reason,
#     and exits 1 — the daemon reads `.ok`/`.reason` and falls back to its
#     source-staleness path rather than treating this as a fault.
# ============================================================
W82="$BASE_WORKDIR/w82"
new_fixture "$W82"
INSTALLED82="$W82/installed/loom-daemon"
mkdir -p "$W82/installed"
write_fake_artifact_daemon "$INSTALLED82" "0.19.21" "deadbee"

W82_FAKEBIN="$W82/fakebin"
mkdir -p "$W82_FAKEBIN"
write_fake_gh_unreachable "$W82_FAKEBIN/gh"
write_fake_cargo "$W82_FAKEBIN/cargo"

out82=$( cd "$W82" && PATH="$W82_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$INSTALLED82" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    bash "$UPDATE_SCRIPT" --resolve-json 2>/dev/null )
rc82=$?
assert_eq "1" "$rc82" "--resolve-json: exits 1 when no release artifact resolves"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$out82" == \{*\} ]] && grep -q '"ok":false' <<<"$out82" && grep -q '"reason":"[^"]' <<<"$out82"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --resolve-json: an unresolvable release still yields JSON with ok=false + a reason"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --resolve-json: an unresolvable release still yields JSON with ok=false + a reason"
    echo "  stdout: $out82"
fi

# ============================================================
# 83. --resolve-json honors --no-fetch / LOOM_DAEMON_UPDATE_FETCH=0: a host
#     that has opted out of the artifact path reports ok=false with that as
#     the reason, which is what keeps the daemon's artifact-first tick on its
#     source path there.
# ============================================================
out83=$( cd "$W81" && PATH="$W81_FAKEBIN:$TEST_PATH" \
    LOOM_DAEMON_BIN="$INSTALLED81" \
    LOOM_DAEMON_UPDATE_GH_REPO="test-owner/test-repo" \
    LOOM_DAEMON_UPDATE_TARGET="x86_64-unknown-linux-gnu" \
    LOOM_DAEMON_UPDATE_FETCH=0 \
    bash "$UPDATE_SCRIPT" --resolve-json 2>/dev/null )
rc83=$?
assert_eq "1" "$rc83" "--resolve-json: exits 1 when artifact-fetch is disabled on the host"
TESTS_RUN=$((TESTS_RUN + 1))
if grep -q '"ok":false' <<<"$out83" && grep -qi 'disabled' <<<"$out83"; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "${GREEN}✓${NC} --resolve-json: --no-fetch/LOOM_DAEMON_UPDATE_FETCH=0 is reported as the reason"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "${RED}✗${NC} --resolve-json: --no-fetch/LOOM_DAEMON_UPDATE_FETCH=0 is reported as the reason"
    echo "  stdout: $out83"
fi

echo
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
