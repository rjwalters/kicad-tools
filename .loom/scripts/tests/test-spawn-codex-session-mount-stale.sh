#!/usr/bin/env bash
# test-spawn-codex-session-mount-stale.sh - how a dispatch refused because the
# running session container does not mount the tick's working directory
# reaches the daemon as SESSION_MOUNT_STALE (#10364).
#
# A host-mode session container mounts each registered repo separately, fixed
# at creation, so a repo registered afterwards is not inside it. The mapping
# is daemon-side, not shell: `loom-daemon session-exec host` reads the mounts
# from its one pre-exec `docker inspect`, refuses with exit 78 and announces
# the cause on stderr as `# LOOM_SESSION_REFUSAL v=1
# category=SESSION_MOUNT_STALE`; the daemon's terminal-record parser relabels
# the adapter's generic record from it (`session_exec::refusal`, unit-tested
# in Rust). What spawn-codex.sh owes is pass-through: keep exit 78, carry the
# announcement to its stderr untouched, and report its classifier's own
# category without a per-cause arm.
#
# Hermetic: a fake daemon stands in for loom-daemon for the adapter cases;
# docker is never run. The last cases run THIS checkout's `loom-daemon`
# (lib/require-daemon-bin.sh, #10676) against a fake docker, to pin the
# announcement's exact text and the one-inspect dispatch check behind it.
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-codex-session-mount-stale.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
SPAWN_CODEX="$SCRIPTS_DIR/spawn-codex.sh"
# THIS checkout's build or nothing (#10662/#10676), never whatever is on PATH.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin --self-only "$SCRIPTS_DIR" session-exec
REAL_DAEMON="$LOOM_DAEMON_SELF_BIN"

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
source "$TEST_DIR/lib/session-lock-sandbox.sh" "$WORK"
mkdir -p "$WORK/bin" "$WORK/ws/.loom"

PROFILE="$WORK/profiles/acct"
mkdir -p "$PROFILE"
printf '{"token":"stub"}\n' >"$PROFILE/auth.json"
printf '{"hooks":{}}\n' >"$PROFILE/hooks.json"
printf '' >"$PROFILE/config.toml"
printf '{}\n' >"$PROFILE/loom-codex-hooks.json"
printf '{"schema_version":1,"container_name":"loom-codex-session-acct","adopted_at_unix":0}\n' \
    >"$PROFILE/.session-managed.json"

printf '#!/usr/bin/env bash\nexit 0\n' >"$WORK/bin/docker"
# Safety net: a stray bare-metal codex must never reach the network.
printf '#!/usr/bin/env bash\necho "unexpected bare-metal codex" >&2\nexit 99\n' >"$WORK/bin/codex"
chmod +x "$WORK/bin/docker" "$WORK/bin/codex"

# Fake daemon: posture answers $FAKE_POSTURE; `host` exits $FAKE_HOST_RC.
cat >"$WORK/bin/fake-loom-daemon" <<'FAKE'
#!/usr/bin/env bash
case "${1:-}" in
    --version) echo "loom-daemon 0.0.0-test"; exit 0 ;;
    session-exec)
        case "${2:-}" in
            posture) echo "$FAKE_POSTURE"; exit 0 ;;
            host) [[ -z "${FAKE_REFUSAL:-}" ]] || echo "$FAKE_REFUSAL" >&2
                echo "fake session-exec host refusal" >&2; exit "${FAKE_HOST_RC:-78}" ;;
            *) exit 0 ;;
        esac ;;
esac
exit 0
FAKE
chmod +x "$WORK/bin/fake-loom-daemon"

run_spawn() {
    SPAWN_ERR="$(cd "$WORK/ws" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE -u LOOM_ROLE \
        LOOM_SWEEP_NICE=0 LOOM_WORKSPACE="$WORK/ws" \
        LOOM_CODEX_HOME="$PROFILE" LOOM_ACCOUNT_NAME=acct \
        LOOM_CODEX_SESSION_DOCKER="$WORK/bin/docker" \
        LOOM_DAEMON_SELF_BIN="$WORK/bin/fake-loom-daemon" \
        PATH="$WORK/bin:$PATH" FAKE_POSTURE="$1" FAKE_HOST_RC="$2" FAKE_REFUSAL="${3:-}" \
        bash "$SPAWN_CODEX" -p "hi" 2>&1 >/dev/null)"
    SPAWN_RC=$?
}
record() { printf '%s\n' "$SPAWN_ERR" | grep '^# LOOM_TERMINAL_RESULT ' || true; }
category() { record | sed -n 's/.* category=\([A-Z_]*\) .*/\1/p'; }

MARKER='# LOOM_SESSION_REFUSAL v=1 category=SESSION_MOUNT_STALE'
markers() { printf '%s\n' "$SPAWN_ERR" | grep -cxF "$MARKER" || true; }
HOST="mode=host sandbox=danger-full-access gh=skip"

echo "--- a stale-mount refusal announced by session-exec passes through, exit 78 kept ---"
run_spawn "$HOST" 78 "$MARKER"
assert_eq "78" "$SPAWN_RC" "the refusal exit code (78) still passes through"
assert_eq "1" "$(markers)" "the daemon's refusal announcement reaches stderr once, untouched"
assert_eq "RECOVERABLE" "$(category)" \
    "the adapter reports its classifier's category; the daemon relabels it, not the shell"
assert_eq "0" "$(grep -c 'SESSION_MOUNT_STALE' "$SPAWN_CODEX" || true)" \
    "spawn-codex.sh carries no SESSION_MOUNT_STALE arm of its own"

echo "--- no announcement, no relabel ---"
run_spawn "$HOST" 78
assert_eq "78" "$SPAWN_RC" "exit code passes through"
assert_eq "0" "$(markers)" "the adapter never invents a refusal announcement"
assert_eq "RECOVERABLE" "$(category)" "the classifier's RECOVERABLE verdict is kept"

# A fake docker that answers `inspect --type container` as a running host-mode
# container whose one workspace bind is $1, counts its inspect calls, and
# fails anything else (so a dispatch that gets past the mount check stops at
# the protocol probe instead of running a worker).
fake_docker() {
    : >"$WORK/docker-calls"
    cat >"$WORK/bin/docker" <<DOCKER
#!/usr/bin/env bash
echo "\$1" >>"$WORK/docker-calls"
[[ "\$1" == inspect ]] || exit 1
echo '[{"State":{"Running":true},"Config":{"Labels":{"loom.workspace":"$WORK"}},"Mounts":[{"Type":"bind","Destination":"$1","RW":true}]}]'
DOCKER
}
real_host() {
    REAL_ERR="$(PATH="$WORK/bin:$PATH" "$REAL_DAEMON" session-exec host \
        --container loom-codex-session-acct --workdir "$WORK/ws" -- true 2>&1 >/dev/null)"
    REAL_RC=$?
}

echo "--- the real daemon announces SESSION_MOUNT_STALE for an unmounted workdir ---"
fake_docker "$WORK/other-repo"
real_host
# It took the dispatch lock before its inspect, in the sandbox, not under the
# real ~/.loom (lib/session-lock-sandbox.sh, #10661).
lss_expect_lock loom-codex-session-acct
# Matched without a pipe: under `pipefail` a `cmd | grep -q` can fail on the
# writer's SIGPIPE (scripts/check-pipefail-early-exit.sh).
case $'\n'"$REAL_ERR"$'\n' in
    *$'\n'"$MARKER"$'\n'*) actual="announced" ;;
    *) actual="missing" ;;
esac
assert_eq "announced" "$actual" "session-exec host announces SESSION_MOUNT_STALE"
assert_eq "78" "$REAL_RC" "…and refuses with 78"
assert_eq "inspect" "$(tr '\n' ' ' <"$WORK/docker-calls" | sed 's/ $//')" \
    "the refusal costs one docker inspect and no exec"
case "$REAL_ERR" in
    *"accounts session stop acct && loom-daemon accounts session start acct --mount-workspace $WORK"*) actual="named" ;;
    *) actual="missing" ;;
esac
assert_eq "named" "$actual" "the refusal names the account's recreate command"

echo "--- the real daemon lets a mounted workdir through to the protocol probe ---"
fake_docker "$WORK/ws"
real_host
assert_eq "0" "$(printf '%s\n' "$REAL_ERR" | grep -c '^# LOOM_SESSION_REFUSAL ' || true)" \
    "a container that mounts the workdir is not refused for its mounts"
assert_eq "inspect exec" "$(tr '\n' ' ' <"$WORK/docker-calls" | sed 's/ $//')" \
    "dispatch makes one inspect, then goes on to the protocol exec"

echo ""
echo "========================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed"
