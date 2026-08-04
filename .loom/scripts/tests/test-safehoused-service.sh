#!/usr/bin/env bash
# test-safehoused-service.sh — Tests for safehoused-service.sh (issue #4346).
#
# Focus: the PURE, side-effect-free surface — the --print-plist / --print-unit
# preview modes and parameter resolution (bin / exec / socket / config / log /
# label / unit + precedence). The install / uninstall paths drive real
# launchctl / systemctl and are exercised manually per the issue's test plan;
# here we prove the rendered definitions are valid and that preview never
# touches the filesystem.
#
# Style matches test-loom-daemon-start.sh — plain bash, hand-rolled assertions.
# Bats is NOT used in this repository.
#
# Usage:
#   ./defaults/scripts/tests/test-safehoused-service.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_SCRIPT="$(cd "$SCRIPT_DIR/../cli" && pwd)/safehoused-service.sh"

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
        echo -e "  expected to contain: [$needle]"
        echo -e "  actual: [$haystack]"
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
        echo -e "  expected NOT to contain: [$needle]"
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
        echo -e "  expected: [$expected]"
        echo -e "  actual:   [$actual]"
    fi
}

# ---------- fixture ----------
# Isolated HOME so nothing touches the real ~/Library/LaunchAgents or config,
# and no repo config-resolver interference with the socket precedence tests.
FAKE_HOME="$(mktemp -d)"
trap 'rm -rf "$FAKE_HOME"' EXIT
FAKE_BIN="$FAKE_HOME/safehoused"
: > "$FAKE_BIN"
chmod +x "$FAKE_BIN"

# Run the script from a directory with no .loom so config resolution is a no-op
# (isolates env/flag precedence from any ambient repo config). --socket is the
# highest-precedence source, so socket tests pass it explicitly.
run() {
    ( cd "$FAKE_HOME" && HOME="$FAKE_HOME" bash "$SVC_SCRIPT" "$@" )
}

# ============================================================================
# --print-plist
# ============================================================================
plist="$(run --print-plist --bin "$FAKE_BIN" --socket /run/safehoused.sock 2>/dev/null)"
assert_contains "$plist" "<string>$FAKE_BIN</string>" "plist ProgramArguments carries the resolved binary"
assert_contains "$plist" "com.rjwalters.safehoused" "plist uses the default LaunchAgent label"
assert_contains "$plist" "<key>RunAtLoad</key>" "plist sets RunAtLoad (starts on login / reboot)"
# KeepAlive is a bare <true/> (persistent daemon), NOT the SuccessfulExit dict.
assert_contains "$plist" "<key>KeepAlive</key>"$'\n'"    <true/>" "plist KeepAlive=true (persistent, no clean-exit primitive)"
assert_not_contains "$plist" "SuccessfulExit" "plist does NOT use loom-daemon's SuccessfulExit policy"
assert_contains "$plist" "<key>SAFEHOUSED_SOCKET</key>"$'\n'"        <string>/run/safehoused.sock</string>" "plist bakes SAFEHOUSED_SOCKET from --socket"

# plutil validation (macOS only).
if command -v plutil >/dev/null 2>&1; then
    echo "$plist" > "$FAKE_HOME/rendered.plist"
    if plutil -lint "$FAKE_HOME/rendered.plist" >/dev/null 2>&1; then
        assert_eq "ok" "ok" "rendered plist passes plutil -lint"
    else
        assert_eq "ok" "fail" "rendered plist passes plutil -lint"
    fi
fi

# ============================================================================
# --print-unit
# ============================================================================
unit="$(run --print-unit --bin "$FAKE_BIN" --socket /run/safehoused.sock --config /etc/safehoused.toml 2>/dev/null)"
assert_contains "$unit" "ExecStart=$FAKE_BIN" "unit ExecStart carries the resolved binary"
assert_contains "$unit" "Restart=always" "unit Restart=always (persistent-daemon supervision)"
assert_contains "$unit" "RestartSec=5" "unit bounds relaunch rate with RestartSec"
assert_contains "$unit" "WantedBy=default.target" "unit WantedBy=default.target (enabled starts on login)"
assert_contains "$unit" "Environment=SAFEHOUSED_SOCKET=/run/safehoused.sock" "unit bakes SAFEHOUSED_SOCKET"
assert_contains "$unit" "Environment=SAFEHOUSED_CONFIG=/etc/safehoused.toml" "unit bakes SAFEHOUSED_CONFIG"
assert_not_contains "$unit" "on-success" "unit does NOT use loom-daemon's Restart=on-success policy"

# ============================================================================
# --exec override drives ExecStart / ProgramArguments
# ============================================================================
unit_exec="$(run --print-unit --exec "$FAKE_BIN --config /x.toml --verbose" 2>/dev/null)"
assert_contains "$unit_exec" "ExecStart=$FAKE_BIN --config /x.toml --verbose" "--exec overrides the ExecStart line verbatim"
plist_exec="$(run --print-plist --exec "$FAKE_BIN --verbose" 2>/dev/null)"
assert_contains "$plist_exec" "<string>$FAKE_BIN</string>"$'\n'"        <string>--verbose</string>" "--exec splits into ProgramArguments array entries"

# ============================================================================
# custom label / unit / log
# ============================================================================
plist_lbl="$(run --print-plist --bin "$FAKE_BIN" --label com.example.sh --log /tmp/x.log 2>/dev/null)"
assert_contains "$plist_lbl" "<string>com.example.sh</string>" "--label overrides the LaunchAgent label"
assert_contains "$plist_lbl" "<string>/tmp/x.log</string>" "--log overrides the plist log path"
unit_u="$(run --print-unit --bin "$FAKE_BIN" --unit shd.service 2>/dev/null)"
# The unit name is not embedded in the rendered file, but the script must accept
# the flag and still render a valid unit.
assert_contains "$unit_u" "ExecStart=$FAKE_BIN" "--unit is accepted and the unit still renders"

# ============================================================================
# placeholder fallback when no binary is found (still exits 0, valid preview)
# ============================================================================
ph_out="$(env -u SAFEHOUSED_BIN HOME="$(mktemp -d)" PATH="$(mktemp -d)" "$BASH" "$SVC_SCRIPT" --print-unit 2>/dev/null; echo "rc=$?")"
assert_contains "$ph_out" "ExecStart=/path/to/safehoused" "missing-binary preview falls back to a placeholder ExecStart"
assert_contains "$ph_out" "rc=0" "missing-binary preview still exits 0 (no side effects)"

# ============================================================================
# no side effects: preview never writes a plist / unit to disk
# ============================================================================
run --print-plist --bin "$FAKE_BIN" >/dev/null 2>&1
run --print-unit --bin "$FAKE_BIN" >/dev/null 2>&1
if [[ ! -e "$FAKE_HOME/Library/LaunchAgents" && ! -e "$FAKE_HOME/.config/systemd/user" ]]; then
    assert_eq "clean" "clean" "preview modes create no LaunchAgents / systemd unit files"
else
    assert_eq "clean" "dirty" "preview modes create no LaunchAgents / systemd unit files"
fi

# ============================================================================
# help + error handling
# ============================================================================
help_out="$(bash "$SVC_SCRIPT" --help 2>/dev/null)"
assert_contains "$help_out" "OWNERSHIP DECISION" "--help documents the service-wrapper ownership decision"
assert_contains "$help_out" "--print-plist" "--help documents the preview modes"

badflag_rc=0
bash "$SVC_SCRIPT" --nonsense >/dev/null 2>&1 || badflag_rc=$?
assert_eq "1" "$badflag_rc" "unknown flag exits 1"

# ---------- summary ----------
echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
