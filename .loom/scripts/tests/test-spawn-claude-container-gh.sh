#!/usr/bin/env bash
# Tests spawn-claude.sh's container gh credentials and ledger-sink mount
# (rjwalters/loom#11790). Hermetic: stub docker and a
# stub daemon under mktemp -d; no network, forge or real daemon binary.
#
# Pins (a) the forge auth block: GH_TOKEN/GITHUB_TOKEN forwarded by NAME, and
# a read-only ~/.config/gh mount only when neither token is set; and (b) the
# #10607 sink mount read from `loom-daemon gh-shim container-mount`, which
# degrades to no mount when the verb prints nothing or fails.
# shellcheck disable=SC2016,SC2034  # check() takes single-quoted expressions to eval; OUT is for debugging
set -uo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "  ok: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
WS="$T/ws"
mkdir -p "$WS/.loom/tokens" "$T/home/.config/gh" "$T/bin"
chmod 700 "$WS/.loom/tokens"
printf 'fake-token' >"$WS/.loom/tokens/t.token"
chmod 600 "$WS/.loom/tokens/t.token"
ln -s "$SCRIPTS_DIR" "$WS/.loom/scripts"
echo '{"runtimes": {"containment": {"enabled": true}}}' >"$WS/.loom/config.json"

DOCKER_LOG="$T/docker.log"
cat >"$T/bin/docker" <<STUB
#!/usr/bin/env bash
echo "\$*" >>"$DOCKER_LOG"
exit 0
STUB
chmod +x "$T/bin/docker"

# Stub daemon: `gh-shim container-mount` prints FAKE_MOUNT (one arg per line),
# nothing when FAKE_MOUNT is empty, or fails with FAKE_MOUNT_RC. Every other
# verb succeeds silently.
cat >"$T/bin/fake-daemon" <<'STUB'
#!/usr/bin/env bash
if [[ "$*" == "gh-shim container-mount" ]]; then
    echo "$*" >>"$FAKE_CALL_LOG"
    if [[ "${FAKE_MOUNT_RC:-0}" != "0" ]]; then
        echo "usage: loom-daemon gh-shim path|session-env|status" >&2
        exit "$FAKE_MOUNT_RC"
    fi
    [[ -n "${FAKE_MOUNT:-}" ]] && printf '%s\n' -v "$FAKE_MOUNT"
fi
exit 0
STUB
chmod +x "$T/bin/fake-daemon"

run() { # run [ENV=VAL ...]; stdout+stderr, exit code in RC
    : >"$DOCKER_LOG"
    : >"$T/calls.log"
    OUT="$(env -u GITHUB_TOKEN HOME="$T/home" LOOM_WORKSPACE="$WS" LOOM_DAEMON_BIN="$T/bin/fake-daemon" \
        PATH="$T/bin:$PATH" FAKE_CALL_LOG="$T/calls.log" LOOM_SWEEP_CPU_QUOTA=0 "$@" \
        "$SCRIPTS_DIR/spawn-claude.sh" -p ping 2>&1)"
    RC=$?
}

echo "container gh credentials"

run GH_TOKEN=ghp_notreal
check "token set: docker reached" '[[ $RC -eq 0 && -s "$DOCKER_LOG" ]]'
check "token set: GH_TOKEN forwarded by name" 'grep -q -- "-e GH_TOKEN" "$DOCKER_LOG"'
check "token set: the value never reaches the docker argv" '! grep -q "ghp_notreal" "$DOCKER_LOG"'
check "token set: ~/.config/gh is not mounted" '! grep -q "\.config/gh" "$DOCKER_LOG"'

run env -u GH_TOKEN
check "no token: ~/.config/gh mounted read-only at its host path" \
    'grep -q -- "-v $T/home/.config/gh:$T/home/.config/gh:ro" "$DOCKER_LOG"'
check "no token: nothing forwarded by name" '! grep -q -- "-e GH_TOKEN" "$DOCKER_LOG"'

echo "ledger sink mount (gh-shim container-mount)"

run GH_TOKEN=x FAKE_MOUNT="$T/sink/contained:$T/sink"
check "the verb is called" 'grep -q "^gh-shim container-mount$" "$T/calls.log"'
check "its output is passed to docker" 'grep -q -- "-v $T/sink/contained:$T/sink" "$DOCKER_LOG"'

run GH_TOKEN=x FAKE_MOUNT=""
check "empty output: docker still reached" '[[ $RC -eq 0 && -s "$DOCKER_LOG" ]]'
check "empty output: no sink mount" '! grep -q "/sink" "$DOCKER_LOG"'

run GH_TOKEN=x FAKE_MOUNT_RC=2
check "older daemon (verb fails): docker still reached" '[[ $RC -eq 0 && -s "$DOCKER_LOG" ]]'
check "older daemon (verb fails): no sink mount, usage text not passed" \
    '! grep -q "/sink\|usage:" "$DOCKER_LOG"'

check "no mapfile in spawn-claude.sh (Bash 3.2)" '! grep -qE "^[^#]*\bmapfile\b" "$SCRIPTS_DIR/spawn-claude.sh"'

echo "passed=$PASS failed=$FAIL"
[[ $FAIL -eq 0 ]]
