#!/usr/bin/env bash
# test-spawn-codex-session-exec.sh — the session-exec invocation's shape
# (issue #8518), split out of test-spawn-codex.sh (over the file-size ratchet
# threshold; new assertions go in a sibling, per file-size-policy.md).
#
# What #8518 fixed: `docker exec` inherits neither the caller's cwd nor its
# environment, so the released invocation started Codex in the image's WORKDIR
# (/home/loom) — never a repository, never a trusted project — and every
# headless dispatch into a session container died with "Not inside a trusted
# directory". The invocation must now carry `--workdir "$PWD"` and
# `--env LOOM_WORKSPACE=…`, forward only Loom's own context variables, and
# never leak the host's HOME/PATH/CODEX_HOME or ambient credentials into the
# account's container (the container owns its CODEX_HOME, ADR-0017 Decision 1).
#
# Hermetic: LOOM_CODEX_NO_EXEC=1 argv-preview mode — never touches docker or
# codex.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPAWN_CODEX="$(cd "$SCRIPT_DIR/.." && pwd)/spawn-codex.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected NOT to contain: '$needle'"
        echo "    Actual: '$haystack'"
    fi
}

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

# A profile adopted by a prior `loom-daemon accounts session start` — marked
# with the exact sentinel session_lifecycle::mark_session_managed writes.
PROFILE="$TMPROOT/profiles/acct"
mkdir -p "$PROFILE"
printf '{"token":"stub"}\n' > "$PROFILE/auth.json"
printf '{"hooks":{}}\n' > "$PROFILE/hooks.json"
printf '' > "$PROFILE/config.toml"
printf '{}\n' > "$PROFILE/loom-codex-hooks.json"
printf '{"schema_version":1,"container_name":"loom-codex-session-acct","adopted_at_unix":0}\n' \
    > "$PROFILE/.session-managed.json"

# The workspace spawn-codex.sh resolves; pinned so the expected string is exact.
WS="$TMPROOT/ws"
mkdir -p "$WS/.loom"

echo "Testing spawn-codex.sh session-exec invocation shape (#8518)..."

out="$(cd "$WS" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE \
    LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_WORKSPACE="$WS" \
    LOOM_CODEX_HOME="$PROFILE" \
    HOME="$TMPROOT/fake-home" \
    bash "$SPAWN_CODEX" -p "hi" 2>&1 || true)"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"

assert_contains "session-exec host --container loom-codex-session-acct --workdir $WS --env LOOM_WORKSPACE=$WS --env CARGO_INCREMENTAL=0" "$line" \
    "the exec carries the caller's cwd as --workdir and LOOM_WORKSPACE, before the container name"
assert_contains " -- codex exec " "$line" \
    "…then the container name, then the codex argv"
assert_contains " hi" "$line" "…and the prompt survives at the end of the argv"
assert_not_contains "HOME=" "$line" \
    "host HOME / CODEX_HOME are never forwarded (the container owns its CODEX_HOME)"
assert_not_contains "PATH=" "$line" \
    "host PATH is never forwarded"

# Loom context variables ride across explicitly; unrelated host env does not.
out="$(cd "$WS" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE \
    LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_WORKSPACE="$WS" \
    LOOM_CODEX_HOME="$PROFILE" \
    LOOM_ROLE=curator LOOM_SWEEP_ID=sweep-1 ANTHROPIC_API_KEY=never-forward-me \
    bash "$SPAWN_CODEX" -p "hi" 2>&1 || true)"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"
assert_contains "--env LOOM_ROLE=curator" "$line" "LOOM_ROLE is forwarded into the container"
assert_contains "--env LOOM_SWEEP_ID=sweep-1" "$line" "LOOM_SWEEP_ID is forwarded into the container"
assert_not_contains "never-forward-me" "$line" \
    "an ambient provider credential in the host env is never forwarded"

# A non-adopted profile is untouched: bare-metal, no docker, no --workdir.
GOOD="$TMPROOT/profiles/bare"
mkdir -p "$GOOD"
printf '{"token":"stub"}\n' > "$GOOD/auth.json"
out="$(cd "$WS" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE \
    LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_WORKSPACE="$WS" \
    LOOM_CODEX_HOME="$GOOD" \
    bash "$SPAWN_CODEX" -p "hi" 2>&1 || true)"
assert_contains "would-exec: codex exec" "$out" "a non-session-managed profile still dispatches bare-metal"
assert_not_contains "--workdir" "$out" "bare-metal dispatch has no docker --workdir"

# --- Issue #9979: the container is the boundary -------------------------------
# Codex's bubblewrap sandbox cannot start inside a session container, so a
# session dispatch runs `-s danger-full-access` — but ONLY into a container the
# REAL `loom-daemon session-exec posture` (built from this tree) has checked:
# posture label AND actual HostConfig. A fake docker answers `docker inspect`
# with JSON; the full property matrix is pinned in session_exec/posture_tests.rs.
echo ""
echo "Testing the session container boundary (#9979)..."
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin --self-only "$(cd "$SCRIPT_DIR/.." && pwd)" session-exec
FAKE_DOCKER="$TMPROOT/fake-docker"
cat > "$FAKE_DOCKER" <<'FAKE'
#!/usr/bin/env bash
# Answers `docker inspect ... <container>` with $FAKE_INSPECT (JSON), and the
# posture gate's `docker exec <c> sha256sum -- <profile controls>` from
# $CODEX_HOME: the container sees the host's copy, except a control named in
# $FAKE_CONTROLS_STALE (a host-side replace the file bind did not follow).
if [[ "$1" == "exec" && "$3" == "sha256sum" ]]; then
    command -v sha256sum >/dev/null && sum=(sha256sum) || sum=(shasum -a 256)
    for p in "${@:5}"; do
        [[ -f "$CODEX_HOME/${p##*/}" && "${p##*/}" != "${FAKE_CONTROLS_STALE:-}" ]] || continue
        printf '%s  %s\n' "$("${sum[@]}" < "$CODEX_HOME/${p##*/}" | cut -d' ' -f1)" "$p"
    done
    exit 0
fi
[[ "$1" == "inspect" ]] || exit 1
[[ -n "${FAKE_INSPECT+x}" ]] || { echo "[]"; echo "Error: No such object" >&2; exit 1; }
printf '%s\n' "$FAKE_INSPECT"
FAKE
chmod +x "$FAKE_DOCKER"

# state <running> <labels-json> [<hostconfig-json>] [<mounts-json>]
HC_OK='{"Privileged":false,"NetworkMode":"bridge","CapDrop":["ALL"],"SecurityOpt":["no-new-privileges"]}'
# control_mount <file> [<RW>]: the read-only bind of one profile control.
control_mount() {
    printf '{"Type":"bind","Source":"%s/%s","Destination":"/home/loom/.codex-profile/%s","RW":%s}' \
        "$PROFILE" "$1" "$1" "${2:-false}"
}
CONTROLS="$(control_mount hooks.json),$(control_mount config.toml),$(control_mount loom-codex-hooks.json)"
state() {
    printf '[{"State":{"Running":%s},"Config":{"Labels":%s},"HostConfig":%s,"Mounts":%s}]' \
        "$1" "$2" "${3:-$HC_OK}" "${4:-[$CONTROLS]}"
}
HOST_LABEL='{"loom.session-posture":"container-boundary-v1"}'
CLONE_LABEL='{"loom.workspace-mode":"private-clone"}'

session_run() {
    # $1 = FAKE_INSPECT value ("__unset__" for a missing container); rest = env/args
    local inspect="$1"; shift
    local -a envs=(LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_WORKSPACE="$WS"
        LOOM_CODEX_HOME="$PROFILE" LOOM_CODEX_SESSION_DOCKER="$FAKE_DOCKER")
    [[ "$inspect" != "__unset__" ]] && envs+=(FAKE_INSPECT="$inspect")
    (cd "$WS" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE -u FAKE_INSPECT -u GH_CONFIG_DIR \
        -u LOOM_CODEX_CONTAINER_SANDBOX -u LOOM_PRIVATE_LEASE_FD "${envs[@]}" "$@" 2>&1; echo "rc=$?")
}

# Hardened host-mode container: the requested workspace-write becomes
# danger-full-access, and the requested mode is still named in the log.
out="$(session_run "$(state true "$HOST_LABEL")" bash "$SPAWN_CODEX" -p "hi" --dangerously-skip-permissions)"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"
assert_contains "-s danger-full-access" "$line" "a hardened container runs Codex with its own sandbox off"
assert_not_contains "-s workspace-write" "$line" "the bwrap-dependent mode is never forwarded into the container"
assert_contains "sandbox=danger-full-access source=session-container-boundary requested=workspace-write" "$out" \
    "the audit line keeps the requested mode and names the container boundary"
assert_contains "posture=host" "$out" "the container posture is logged"

# An explicit `-s read-only` is replaced too (not forwarded alongside).
out="$(session_run "$(state true "$HOST_LABEL")" bash "$SPAWN_CODEX" -p "hi" -s read-only)"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"
assert_contains "-s danger-full-access" "$line" "an explicit -s is replaced inside the container"
assert_not_contains "read-only" "$line" "…and the explicit mode is not forwarded as well"

# Private-clone containers were created hardened (#8787) and qualify.
out="$(session_run "$(state true "$CLONE_LABEL")" bash "$SPAWN_CODEX" -p "hi")"
assert_contains "posture=private-clone" "$out" "a private-clone container qualifies"
assert_contains "-s danger-full-access" "$out" "…and runs Codex with its sandbox off"

# A container created before the hardening (no posture label) is refused: it
# mounted the whole checkout parent and the Claude token pool.
out="$(session_run "$(state true '{}')" bash "$SPAWN_CODEX" -p "hi")"
assert_contains "rc=78" "$out" "an unhardened container exits 78 (EX_CONFIG)"
assert_contains "created before the container-boundary hardening" "$out" "…naming why"
assert_contains "loom-daemon accounts session stop acct" "$out" "…and the recreate step"
assert_not_contains "would-exec:" "$out" "…before anything is dispatched"

# The label is not the posture: a labelled container whose ACTUAL settings
# are not hardened is refused with exit 78 before anything is dispatched.
for bad in \
    '{"Privileged":true,"NetworkMode":"bridge","CapDrop":["ALL"],"SecurityOpt":["no-new-privileges"]}|privileged=true' \
    '{"Privileged":false,"NetworkMode":"host","CapDrop":["ALL"],"SecurityOpt":["no-new-privileges"]}|host-namespace(network)' \
    '{"Privileged":false,"NetworkMode":"bridge","SecurityOpt":["no-new-privileges"]}|cap-drop-ALL-missing' \
    '{"Privileged":false,"NetworkMode":"bridge","CapDrop":["ALL"],"SecurityOpt":["no-new-privileges","seccomp=unconfined"]}|security-opt=seccomp=unconfined'; do
    for label in "$HOST_LABEL" "$CLONE_LABEL"; do
        out="$(session_run "$(state true "$label" "${bad%|*}")" bash "$SPAWN_CODEX" -p "hi" --dangerously-skip-permissions)"
        assert_contains "rc=78" "$out" "labelled $label but ${bad##*|}: exit 78"
        assert_contains "${bad##*|}" "$out" "…naming the violation (${bad##*|})"
        assert_not_contains "would-exec:" "$out" "…before anything is dispatched (${bad##*|})"
    done
done
out="$(session_run "$(state true "$HOST_LABEL" "$HC_OK" '[{"Source":"/var/run/docker.sock","Destination":"/var/run/docker.sock"}]')" bash "$SPAWN_CODEX" -p "hi")"
assert_contains "docker-socket-mounted" "$out" "a docker.sock mount is refused"

# The profile's hook-control files are part of the posture: with the sandbox
# off, a session that could rewrite hooks.json / config.toml /
# loom-codex-hooks.json could leave the NEXT session reading guard-ready while
# Codex skips Loom's hook. Each must be a read-only bind, under both labels.
for name in hooks.json config.toml loom-codex-hooks.json; do
    others="$(for o in hooks.json config.toml loom-codex-hooks.json; do [[ "$o" == "$name" ]] || printf '%s,' "$(control_mount "$o")"; done)"
    for label in "$HOST_LABEL" "$CLONE_LABEL"; do
        for mounts in "[${others}$(control_mount "$name" true)]" "[${others%,}]"; do
            out="$(session_run "$(state true "$label" "$HC_OK" "$mounts")" bash "$SPAWN_CODEX" -p "hi")"
            assert_contains "rc=78" "$out" "$name writable or unbound ($label): exit 78"
            assert_contains "profile-control-writable($name)" "$out" "…naming the control ($name)"
            assert_not_contains "would-exec:" "$out" "…before anything is dispatched ($name)"
        done
    done
done
# In host mode the container's copy must be the host's: a file bind does not
# follow a host-side replace (provisioning, accepting hook trust), so Codex
# would read a stale or missing registration while the host verifies ready.
for name in hooks.json config.toml loom-codex-hooks.json; do
    out="$(session_run "$(state true "$HOST_LABEL")" env FAKE_CONTROLS_STALE="$name" bash "$SPAWN_CODEX" -p "hi")"
    assert_contains "rc=78" "$out" "a host-mode container with a stale $name exits 78"
    assert_contains "does not see the host's copy of its profile control files: $name" "$out" "…naming it"
    assert_contains "loom-daemon accounts session stop acct" "$out" "…and the restart step"
    assert_not_contains "would-exec:" "$out" "…before anything is dispatched (stale $name)"
done

# A missing or stopped container never gets the sandbox dropped: the posture
# is unverifiable, the requested mode stands, and dispatch is refused
# downstream by `session-exec host` (asserted in test-spawn-codex.sh).
out="$(session_run "__unset__" bash "$SPAWN_CODEX" -p "hi" --dangerously-skip-permissions)"
assert_contains "posture not verified (not-running)" "$out" "a missing container is not treated as hardened"
assert_not_contains "danger-full-access" "$out" "…and the sandbox is not dropped for it"
out="$(session_run "$(state false "$HOST_LABEL")" bash "$SPAWN_CODEX" -p "hi" --dangerously-skip-permissions)"
assert_not_contains "danger-full-access" "$out" "a stopped container is not treated as hardened either"

# Escape hatch: keep the requested Codex sandbox (needs a userns-capable profile).
out="$(session_run "$(state true "$HOST_LABEL")" env LOOM_CODEX_CONTAINER_SANDBOX=codex bash "$SPAWN_CODEX" -p "hi" --dangerously-skip-permissions)"
assert_contains "-s workspace-write" "$out" "LOOM_CODEX_CONTAINER_SANDBOX=codex keeps the requested sandbox"
assert_contains "LOOM_CODEX_CONTAINER_SANDBOX=codex keeps sandbox=workspace-write" "$out" "…with a warning"
out="$(session_run "$(state true "$HOST_LABEL")" env LOOM_CODEX_CONTAINER_SANDBOX=bogus bash "$SPAWN_CODEX" -p "hi")"
assert_contains "rc=78" "$out" "an invalid LOOM_CODEX_CONTAINER_SANDBOX exits 78"

# gh inside a host-mode container authenticates through the daemon's App token
# dir: the PATH is passed through by name, never a token value.
GH_MOUNTS="[{\"Source\":\"$WS/.loom/gh-config\",\"Destination\":\"$WS/.loom/gh-config\"},$CONTROLS]"
out="$(session_run "$(state true "$HOST_LABEL" "$HC_OK" "$GH_MOUNTS")" env GH_CONFIG_DIR="$WS/.loom/gh-config" GH_TOKEN=never-forward-token bash "$SPAWN_CODEX" -p "hi")"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"
assert_contains "--env GH_CONFIG_DIR --" "$line" "GH_CONFIG_DIR is passed through (by name) into a host-mode container that mounts it"
assert_not_contains "GH_CONFIG_DIR=" "$line" "…by name only: the inherited value, never a new one"
assert_not_contains "never-forward-token" "$line" "a GH_TOKEN value is never forwarded"
out="$(session_run "$(state true "$HOST_LABEL")" env GH_CONFIG_DIR="$WS/.loom/gh-config" bash "$SPAWN_CODEX" -p "hi")"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"
assert_not_contains "GH_CONFIG_DIR" "$line" "an unmounted GH_CONFIG_DIR is not forwarded"
assert_contains "gh inside the session will be unauthenticated" "$out" "…and the gap is named"
out="$(session_run "$(state true "$CLONE_LABEL" "$HC_OK" "$GH_MOUNTS")" env GH_CONFIG_DIR="$WS/.loom/gh-config" bash "$SPAWN_CODEX" -p "hi")"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"
assert_not_contains "GH_CONFIG_DIR" "$line" "a private-clone container keeps its own GH_CONFIG_DIR"

# A daemon without `session-exec posture` (predating #9979) fails closed.
OLD_DAEMON="$TMPROOT/old-daemon"
printf '#!/usr/bin/env bash\necho "error: unrecognized subcommand '"'"'posture'"'"'" >&2; exit 2\n' > "$OLD_DAEMON"
chmod +x "$OLD_DAEMON"
out="$(session_run "$(state true "$HOST_LABEL")" env LOOM_DAEMON_SELF_BIN="$OLD_DAEMON" bash "$SPAWN_CODEX" -p "hi")"
assert_contains "rc=78" "$out" "a daemon predating session-exec posture exits 78"
assert_not_contains "would-exec:" "$out" "…and never dispatches with the sandbox off"

# Bare-metal dispatch is unchanged: the requested sandbox is forwarded as before.
out="$(cd "$WS" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE \
    LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_WORKSPACE="$WS" \
    LOOM_CODEX_HOME="$GOOD" \
    bash "$SPAWN_CODEX" -p "hi" --dangerously-skip-permissions 2>&1 || true)"
assert_contains "-s workspace-write" "$out" "bare-metal dispatch keeps Codex's own sandbox"
assert_not_contains "danger-full-access" "$out" "bare-metal dispatch never drops the sandbox"

# Hook trust is keyed by the CODEX_HOME Codex runs with, so the readiness
# check must be told where THIS launch runs (container mount vs bare metal),
# never left to re-derive it from the adoption marker (#9390): with
# LOOM_CODEX_SESSION_EXEC=0/1 overriding the marker, the derivation is wrong.
assert_contains '--runtime-codex-home "$_hook_runtime_home"' "$(cat "$SPAWN_CODEX")" \
    "spawn-codex names the runtime CODEX_HOME to the hook readiness check"

echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
