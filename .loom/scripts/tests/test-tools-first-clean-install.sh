#!/usr/bin/env bash
# test-tools-first-clean-install.sh — the tools-first locator for the
# loom-tools slice (#12939, epic #12708 Phase 2c; design doc 4.3, 10.2).
#
# Drives `strip-ansi.sh` and `checkpoint.sh` through real copies of the stubs
# in a scratch fixture repo, with fake `loom-tools` / `loom-daemon` binaries
# and a scrubbed environment (`env -i`, no fleet env, no CARGO_TARGET_DIR), so
# nothing on the host can answer for a binary the fixture does not provide.
#
# Pinned:
#   A. Clean install: daemon binary ABSENT, both stubs run via loom-tools, and
#      the child's exit code (a data code) reaches the caller unmodified.
#   B. Order: tools first; the daemon only as an explicit, logged, transitional
#      fallback; LOOM_DAEMON_BIN is an explicit pin that keeps naming the daemon
#      unless LOOM_TOOLS_BIN is also pinned.
#   C. Phase 4 session mode (LOOM_MODE=session) and LOOM_TOOLS_DAEMON_FALLBACK=0
#      never use the daemon fallback; with no loom-tools that is a NONZERO exit.
#   D. Mixed cases for loom_payload_identity_conflict: tools-only, daemon-only,
#      both-agree, both-conflict (refuses), older tools vs newer payload
#      (refuses), rollback (newer tools vs older payload, proceeds).
#
# Usage: bash defaults/scripts/tests/test-tools-first-clean-install.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "${GREEN}✓${NC} $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "${RED}✗${NC} $1"; }
assert_eq() { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$1', got '$2')"; fi; }
assert_contains() { if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3 (expected '$1' in [$2])"; fi; }
assert_not_contains() { if [[ "$2" != *"$1"* ]]; then pass "$3"; else fail "$3 (unexpected '$1' in [$2])"; fi; }

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# A fixture repo: .loom/scripts is a copy of the real scripts the stubs need.
FIX="$WORKDIR/repo"
mkdir -p "$FIX/.git" "$FIX/.loom/scripts/lib" "$WORKDIR/home" "$WORKDIR/bin-tools" "$WORKDIR/bin-daemon"
cp "$SCRIPTS_DIR/strip-ansi.sh" "$SCRIPTS_DIR/checkpoint.sh" "$FIX/.loom/scripts/"
cp "$SCRIPTS_DIR"/lib/script-helper.sh "$SCRIPTS_DIR"/lib/locate-daemon-bin.sh "$FIX/.loom/scripts/lib/"
SYSTEM_PATH="/usr/bin:/bin"

# _fake_tools <path> <requires_daemon>   (checkpoint exits 7: a DATA code)
_fake_tools() {
    cat >"$1" <<FAKE
#!/usr/bin/env bash
case "\${1:-}" in
  --version) echo "loom-tools 0.19.1200"; exit 0 ;;
  capabilities) echo '{"payload_identity":"supports_installed=0.19.0;requires_daemon=$2;invoked_files=x"}'; exit 0 ;;
  strip-ansi) echo "TOOLS strip-ansi"; exit 0 ;;
  checkpoint) echo "TOOLS checkpoint"; exit 7 ;;
esac
exit 2
FAKE
    chmod +x "$1"
}
# _fake_daemon <path> <requires_daemon>
_fake_daemon() {
    cat >"$1" <<FAKE
#!/usr/bin/env bash
case "\${1:-}" in
  --version) echo "loom-daemon 0.19.1200 (commit abc, built 2026-10-21T00:00:00Z)"; exit 0 ;;
  install-compat) echo '{"payload_identity":"supports_installed=0.19.0;requires_daemon=$2;invoked_files=x"}'; exit 0 ;;
  strip-ansi) echo "DAEMON strip-ansi"; exit 0 ;;
  checkpoint) echo "DAEMON checkpoint"; exit 7 ;;
esac
exit 2
FAKE
    chmod +x "$1"
}

# run_stub <stub> <args...> with extra env in $STUB_ENV (array). Echoes
# "<rc>|<stdout+stderr>".
run_stub() {
    local stub="$1"; shift
    local out rc
    out="$(env -i PATH="$SYSTEM_PATH" HOME="$WORKDIR/home" "${STUB_ENV[@]}" \
        "$FIX/.loom/scripts/$stub" "$@" 2>&1 </dev/null)"; rc=$?
    echo "$rc|$out"
}

_fake_tools "$WORKDIR/bin-tools/loom-tools" 0.19.1088
_fake_daemon "$WORKDIR/bin-daemon/loom-daemon" 0.19.1088

echo "A. clean install: no daemon binary, no fleet env"
STUB_ENV=(LOOM_TOOLS_BIN="$WORKDIR/bin-tools/loom-tools")
res="$(run_stub strip-ansi.sh)"
assert_eq "0" "${res%%|*}" "strip-ansi.sh succeeds with only loom-tools"
assert_contains "TOOLS strip-ansi" "$res" "strip-ansi.sh ran loom-tools"
res="$(run_stub checkpoint.sh read)"
assert_eq "7" "${res%%|*}" "checkpoint.sh propagates loom-tools' data exit code unmodified"
assert_contains "TOOLS checkpoint" "$res" "checkpoint.sh ran loom-tools"
# Same through PATH and through the machine-level install dir.
STUB_ENV=(); SYSTEM_PATH="$WORKDIR/bin-tools:/usr/bin:/bin"
res="$(run_stub strip-ansi.sh)"; assert_contains "TOOLS strip-ansi" "$res" "loom-tools found on PATH"
SYSTEM_PATH="/usr/bin:/bin"; mkdir -p "$WORKDIR/home/.local/bin"; cp "$WORKDIR/bin-tools/loom-tools" "$WORKDIR/home/.local/bin/"
res="$(run_stub strip-ansi.sh)"; assert_contains "TOOLS strip-ansi" "$res" "loom-tools found in the machine-level install dir"
rm -f "$WORKDIR/home/.local/bin/loom-tools"

echo "B. order and the transitional fallback"
SYSTEM_PATH="$WORKDIR/bin-daemon:/usr/bin:/bin"
STUB_ENV=(); res="$(run_stub strip-ansi.sh)"
assert_contains "DAEMON strip-ansi" "$res" "no loom-tools: loom-daemon on PATH serves the stub"
assert_contains "transitional" "$res" "the daemon fallback is logged as transitional"
cp "$WORKDIR/bin-tools/loom-tools" "$WORKDIR/bin-daemon/loom-tools"
res="$(run_stub strip-ansi.sh)"
assert_contains "TOOLS strip-ansi" "$res" "loom-tools beats loom-daemon when both are on PATH"
assert_not_contains "transitional" "$res" "…and no transitional line is logged then"
rm -f "$WORKDIR/bin-daemon/loom-tools"
STUB_ENV=(LOOM_DAEMON_BIN="$WORKDIR/bin-daemon/loom-daemon"); SYSTEM_PATH="$WORKDIR/bin-tools:/usr/bin:/bin"
res="$(run_stub strip-ansi.sh)"
assert_contains "DAEMON strip-ansi" "$res" "an explicit LOOM_DAEMON_BIN pin keeps naming the daemon"
STUB_ENV=(LOOM_DAEMON_BIN="$WORKDIR/bin-daemon/loom-daemon" LOOM_TOOLS_BIN="$WORKDIR/bin-tools/loom-tools")
res="$(run_stub strip-ansi.sh)"
assert_contains "TOOLS strip-ansi" "$res" "LOOM_TOOLS_BIN overrides the LOOM_DAEMON_BIN pin"

echo "C. no daemon fallback in session mode / when disabled"
SYSTEM_PATH="$WORKDIR/bin-daemon:/usr/bin:/bin"
STUB_ENV=(LOOM_MODE=session)
res="$(run_stub strip-ansi.sh)"
assert_eq "1" "${res%%|*}" "session mode with no loom-tools exits nonzero"
assert_not_contains "DAEMON strip-ansi" "$res" "session mode never runs the daemon fallback"
STUB_ENV=(LOOM_TOOLS_DAEMON_FALLBACK=0)
res="$(run_stub checkpoint.sh read)"
assert_eq "1" "${res%%|*}" "LOOM_TOOLS_DAEMON_FALLBACK=0 with no loom-tools exits nonzero"
assert_not_contains "DAEMON checkpoint" "$res" "the switch disables the daemon fallback"
SYSTEM_PATH="/usr/bin:/bin"; STUB_ENV=()
res="$(run_stub strip-ansi.sh)"
assert_eq "1" "${res%%|*}" "no binary at all is a nonzero exit, never a success"
assert_contains "neither loom-tools nor loom-daemon" "$res" "…with an actionable message"

echo "D. payload identity across tools and daemon"
LIBSRC="$FIX/.loom/scripts/lib/locate-daemon-bin.sh"
conflict() { # <tools-bin|""> <daemon-bin|""> [source-root]; echoes "<rc>|<stderr>"
    local out rc
    out="$(env -i PATH="/usr/bin:/bin" HOME="$WORKDIR/home" LOOM_TOOLS_BIN="${1:-}" LOOM_DAEMON_BIN="${2:-}" \
        bash -c "source '$LIBSRC'; loom_payload_identity_conflict '$FIX' '${3:-}'" 2>&1 </dev/null)"; rc=$?
    echo "$rc|$out"
}
mkdir -p "$WORKDIR/id"
_fake_tools  "$WORKDIR/id/tools-1088" 0.19.1088
_fake_daemon "$WORKDIR/id/daemon-1088" 0.19.1088
_fake_daemon "$WORKDIR/id/daemon-1100" 0.19.1100
res="$(conflict "$WORKDIR/id/tools-1088" "")"; assert_eq "1" "${res%%|*}" "tools-only proceeds"
res="$(conflict "" "$WORKDIR/id/daemon-1088")"; assert_eq "1" "${res%%|*}" "daemon-only proceeds"
res="$(conflict "$WORKDIR/id/tools-1088" "$WORKDIR/id/daemon-1088")"; assert_eq "1" "${res%%|*}" "both-agree proceeds"
res="$(conflict "$WORKDIR/id/tools-1088" "$WORKDIR/id/daemon-1100")"
assert_eq "0" "${res%%|*}" "both-conflict refuses"
assert_contains "different payload identities" "$res" "…naming the disagreement"
assert_contains "No tie-break" "$res" "…and stating that nothing breaks the tie"
# older tools vs newer payload / rollback: the source's REQUIRES_DAEMON decides.
SRC="$WORKDIR/src"; mkdir -p "$SRC/loom-daemon/src"
echo 'pub const REQUIRES_DAEMON: &str = "0.19.1100";' >"$SRC/loom-daemon/src/install_compat.rs"
res="$(conflict "$WORKDIR/id/tools-1088" "" "$SRC")"
assert_eq "0" "${res%%|*}" "older tools vs newer payload refuses"
assert_contains "older than the payload" "$res" "…saying why"
echo 'pub const REQUIRES_DAEMON: &str = "0.19.1000";' >"$SRC/loom-daemon/src/install_compat.rs"
res="$(conflict "$WORKDIR/id/tools-1088" "" "$SRC")"
assert_eq "1" "${res%%|*}" "rollback (tools newer than the payload) proceeds"
printf '#!/usr/bin/env bash\nexit 2\n' >"$WORKDIR/id/old-daemon"; chmod +x "$WORKDIR/id/old-daemon"
res="$(conflict "$WORKDIR/id/tools-1088" "$WORKDIR/id/old-daemon")"
assert_eq "1" "${res%%|*}" "a daemon with no readable identity fails open"

echo
echo "Ran $TESTS_RUN tests: $TESTS_PASSED passed, $TESTS_FAILED failed"
[[ "$TESTS_FAILED" -eq 0 ]]
