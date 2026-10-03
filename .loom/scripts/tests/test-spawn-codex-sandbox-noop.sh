#!/usr/bin/env bash
# test-spawn-codex-sandbox-noop.sh - spawn-codex.sh's SANDBOX_UNAVAILABLE
# terminal classification (#10003).
#
# The detection rule itself is `loom-daemon codex-sandbox-noop`'s and is
# unit-tested in Rust (`loom-daemon/src/codex_sandbox_noop.rs`). This suite
# covers the adapter's half of the contract, against a fake `codex` and a fake
# daemon:
#
#   - an exit-0 session the daemon calls a no-op gets
#     `category=SANDBOX_UNAVAILABLE` and a `# LOOM_RUNTIME_NOOP` line, with the
#     exit code (0) still passed through;
#   - "not a no-op" (exit 1), an older daemon without the subcommand (exit 2),
#     and an exit-0 with empty stdout all leave `SUCCESS`;
#   - a non-zero codex exit never asks the daemon at all.
#
# Split out of test-spawn-codex.sh, which the file-size ratchet freezes.
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-codex-sandbox-noop.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SPAWN_CODEX="$SCRIPTS_DIR/spawn-codex.sh"

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

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"

# Fake codex: transcript on stderr, final message on stdout, exit $MOCK_RC.
cat >"$WORK/bin/codex" <<'MOCK'
#!/usr/bin/env bash
cat >/dev/null
printf '%s\n' "${MOCK_STDERR:-}" >&2
echo "final message"
exit "${MOCK_RC:-0}"
MOCK
chmod +x "$WORK/bin/codex"

# Fake daemon: answers `--version` like a loom-daemon build, records every
# `codex-sandbox-noop` call, then replies with $FAKE_NOOP_RC and
# $FAKE_NOOP_OUT. Every other verb exits 0 silently.
cat >"$WORK/bin/fake-loom-daemon" <<'FAKE'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "loom-daemon 0.0.0-test"; exit 0; fi
if [[ "${1:-}" == "codex-sandbox-noop" ]]; then
    echo "called $2" >>"$FAKE_CALLS"
    [[ -n "${FAKE_NOOP_OUT:-}" ]] && printf '%s\n' "$FAKE_NOOP_OUT"
    exit "${FAKE_NOOP_RC:-1}"
fi
exit 0
FAKE
chmod +x "$WORK/bin/fake-loom-daemon"

# run_spawn <codex-rc> <daemon-rc> <daemon-stdout> -> SPAWN_RC, SPAWN_ERR
run_spawn() {
    : >"$WORK/calls"
    SPAWN_ERR="$(env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE -u LOOM_ROLE \
        LOOM_SWEEP_NICE=0 LOOM_SPAWN_NO_EXPORT=1 LOOM_ACCOUNT_NAME=profile-a \
        LOOM_DAEMON_SELF_BIN="$WORK/bin/fake-loom-daemon" FAKE_CALLS="$WORK/calls" \
        PATH="$WORK/bin:$PATH" MOCK_RC="$1" MOCK_STDERR="exec" \
        FAKE_NOOP_RC="$2" FAKE_NOOP_OUT="$3" \
        bash "$SPAWN_CODEX" -p "hi" 2>&1 >/dev/null)"
    SPAWN_RC=$?
}
record() { printf '%s\n' "$SPAWN_ERR" | grep '^# LOOM_TERMINAL_RESULT ' || true; }
sentinel() { printf '%s\n' "$SPAWN_ERR" | grep '^# LOOM_RUNTIME_NOOP ' || true; }
tr_line() {
    printf '# LOOM_TERMINAL_RESULT v=2 provider=codex account=profile-a category=%s exit_code=%s model=none' "$1" "$2"
}
DETAIL="shape=exec-denied execs=1 denied=1 succeeded=0"

echo "--- a no-op verdict relabels an exit-0 session ---"
run_spawn 0 0 "$DETAIL"
assert_eq "0" "$SPAWN_RC" "the codex exit code (0) still passes through"
assert_eq "$(tr_line SANDBOX_UNAVAILABLE 0)" "$(record)" \
    "the terminal record is SANDBOX_UNAVAILABLE, not SUCCESS"
assert_eq "# LOOM_RUNTIME_NOOP runtime=codex reason=sandbox-unavailable $DETAIL" "$(sentinel)" \
    "the no-op line carries the daemon's shape and counts"
calls="$(cat "$WORK/calls")"
assert_eq "called " "${calls%%/*}" "the daemon is handed the capture file"

echo "--- everything else keeps SUCCESS ---"
run_spawn 0 1 ""
assert_eq "$(tr_line SUCCESS 0)" "$(record)" "a session that ran something stays SUCCESS"
assert_eq "" "$(sentinel)" "no no-op line when the session ran something"

run_spawn 0 2 "error: unrecognized subcommand 'codex-sandbox-noop'"
assert_eq "$(tr_line SUCCESS 0)" "$(record)" \
    "an older daemon without the subcommand (exit 2) leaves SUCCESS"

run_spawn 0 0 ""
assert_eq "$(tr_line SUCCESS 0)" "$(record)" "exit 0 with an empty verdict leaves SUCCESS"

echo "--- a non-zero codex exit never asks ---"
run_spawn 1 0 "$DETAIL"
assert_eq "1" "$SPAWN_RC" "a non-zero exit passes through"
assert_eq "" "$(cat "$WORK/calls")" "the daemon is not consulted for a non-zero exit"
case "$(record)" in
    *category=SANDBOX_UNAVAILABLE*) actual="relabelled" ;;
    *) actual="classifier verdict kept" ;;
esac
assert_eq "classifier verdict kept" "$actual" "a non-zero exit keeps the shared classifier's verdict"

echo ""
echo "========================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed"
