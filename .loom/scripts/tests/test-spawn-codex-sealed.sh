#!/usr/bin/env bash
# test-spawn-codex-sealed.sh — spawn-codex.sh passes Codex's hook-trust waiver
# ONLY for a sealed registration in a hardened session container
# (rjwalters/loom#10102), and fails closed on every tamper.
#
# The vetting rule itself is pinned clause by clause in
# loom-daemon/src/tokens_pool/codex_hooks_seal_tests.rs. This suite pins the
# adapter's side: when the REAL `loom-daemon codex-hooks verify
# --allow-sealed` (working-tree build) reports a sealed seat, the argv carries
# `--dangerously-bypass-hook-trust -c features.plugins=false`. Otherwise it
# doesn't: a guarded role exits 78 and a read-only role proceeds unhooked
# with `trust-bypass=never`.
#
# Hermetic: LOOM_CODEX_NO_EXEC=1 prints the argv, and a fake docker answers the
# posture gate's `inspect` (a hardened host-mode container) and both
# `exec … sha256sum` probes from the host profile, like a fresh read-only
# bind would.
#
# Usage: ./defaults/scripts/tests/test-spawn-codex-sealed.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_CODEX="$SCRIPTS_DIR/spawn-codex.sh"
PROVISION_SCRIPT="$SCRIPTS_DIR/provision-codex-hooks.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "codex-hooks"
loom_test_require_daemon_bin --self-only "$SCRIPTS_DIR" session-exec

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
TESTS_RUN=0
TESTS_FAILED=0
check() { # <description> <command...>: passes when the command succeeds
    local desc="$1"
    shift
    TESTS_RUN=$((TESTS_RUN + 1))
    if "$@"; then
        echo -e "  ${GREEN}PASS${NC}: $desc"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $desc"
        printf '%s\n' "$OUT" | sed 's/^/      /' | tail -8
    fi
}

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
WS="$TMPROOT/ws"
mkdir -p "$WS/.loom/hooks"
cp "$SCRIPTS_DIR/../hooks/guard-codex-bridge.sh" "$WS/.loom/hooks/"
git -C "$WS" init -q -b main

# A hardened host-mode container whose read-only binds are the host's files.
FAKE_DOCKER="$TMPROOT/docker"
cat > "$FAKE_DOCKER" <<'FAKE'
#!/usr/bin/env bash
if [[ "$1" == "exec" && "$3" == "sha256sum" ]]; then
    command -v sha256sum >/dev/null && sum=(sha256sum) || sum=(shasum -a 256)
    for p in "${@:5}"; do
        [[ -f "$CODEX_HOME/${p##*/}" ]] || continue
        printf '%s  %s\n' "$("${sum[@]}" < "$CODEX_HOME/${p##*/}" | cut -d' ' -f1)" "$p"
    done
    exit 0
fi
[[ "$1" == "inspect" ]] || exit 1
mount() { printf '{"Type":"bind","Source":"x","Destination":"/home/loom/.codex-profile/%s","RW":false}' "$1"; }
printf '[{"State":{"Running":true},"Config":{"Labels":{"loom.session-posture":"container-boundary-v1"}},"HostConfig":{"Privileged":false,"NetworkMode":"bridge","CapDrop":["ALL"],"SecurityOpt":["no-new-privileges"]},"Mounts":[%s,%s,%s]}]\n' \
    "$(mount hooks.json)" "$(mount config.toml)" "$(mount loom-codex-hooks.json)"
FAKE
chmod +x "$FAKE_DOCKER"

# A session-adopted profile carrying Loom's registration and NO recorded trust.
PROFILE="$TMPROOT/profiles/acct"
mkdir -p "$PROFILE"
printf '{"token":"stub"}\n' > "$PROFILE/auth.json"
printf '{"schema_version":1,"container_name":"loom-codex-session-acct","adopted_at_unix":0}\n' \
    > "$PROFILE/.session-managed.json"
bash "$PROVISION_SCRIPT" install --codex-home "$PROFILE" --workspace "$WS" >/dev/null 2>&1
printf 'model = "m"\n' > "$PROFILE/config.toml"
SEALED_HOOKS="$(cat "$PROFILE/hooks.json")"

OUT=""
rc=0
spawn() { # <role> [extra spawn args...]; env overrides via SPAWN_ENV
    local role="$1"
    shift
    rc=0
    OUT="$(cd "$WS" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE -u LOOM_CODEX_SANDBOX -u LOOM_MODEL \
        -u GH_CONFIG_DIR -u LOOM_PRIVATE_LEASE_FD -u LOOM_CODEX_CONTAINER_SANDBOX \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_WORKSPACE="$WS" \
        LOOM_CODEX_HOME="$PROFILE" LOOM_CODEX_SESSION_DOCKER="$FAKE_DOCKER" \
        ${SPAWN_ENV[@]+"${SPAWN_ENV[@]}"} LOOM_ROLE="$role" \
        bash "$SPAWN_CODEX" -p "hi" "$@" 2>&1)" || rc=$?
}
SPAWN_ENV=()

has() { [[ "$OUT" == *"$1"* ]]; }
lacks() { [[ "$OUT" != *"$1"* ]]; }
rc_is() { [[ "$rc" == "$1" ]]; }
argv_has() { [[ "$(printf '%s\n' "$OUT" | grep '^spawn-codex would-exec:' || true)" == *"$1"* ]]; }
WAIVER="--dangerously-bypass-hook-trust -c features.plugins=false"

echo "Testing the sealed-registration trust waiver (#10102)..."
for role in judge champion builder curator; do
    spawn "$role"
    check "$role on a sealed session seat with no recorded trust -> proceeds" rc_is 0
    check "$role: the argv carries the waiver with plugins off" argv_has "codex exec $WAIVER"
    check "$role: the audit line says trust-bypass=sealed" has "trust-bypass=sealed"
done

# Tamper the host profile (the fake container sees the same bytes, as an
# in-place write through a bind would): every one loses the waiver.
tamper() { # <label> <jq filter on hooks.json>
    printf '%s' "$SEALED_HOOKS" | jq "$2" > "$PROFILE/hooks.json"
    spawn judge
    check "$1: judge exits 78" rc_is 78
    check "$1: no codex argv is assembled for judge" lacks "would-exec"
    spawn curator
    check "$1: a read-only role proceeds WITHOUT the waiver" argv_has "codex exec -"
    check "$1: …and never with it" lacks "$WAIVER"
    check "$1: …and says trust-bypass=never" has "trust-bypass=never"
    printf '%s' "$SEALED_HOOKS" > "$PROFILE/hooks.json"
}
tamper "an extra hook" '.hooks.PreToolUse[0].hooks += [{"type":"command","command":"true","timeout":30}]'
tamper "a changed matcher" '.hooks.PreToolUse[0].matcher = "Bash"'
tamper "another event" '.hooks.SessionStart = [{"hooks":[{"type":"command","command":"true","timeout":30}]}]'

printf 'model = "m"\n[features]\nhooks = false\n' > "$PROFILE/config.toml"
spawn judge
check "config.toml switching hooks off: judge exits 78" rc_is 78
printf 'model = "m"\n' > "$PROFILE/config.toml"

mkdir -p "$WS/.codex" && printf '{}\n' > "$WS/.codex/hooks.json"
spawn judge
check "a project .codex/hooks.json in the checkout: judge exits 78" rc_is 78
rm -rf "$WS/.codex"

spawn judge -c 'hooks.PreToolUse=[]'
check "a -c hooks override in the launch argv: judge exits 78" rc_is 78

spawn curator --dangerously-bypass-hook-trust
check "a caller-supplied waiver is refused outright (78)" rc_is 78
spawn curator -- --dangerously-bypass-hook-trust
check "…also after --" rc_is 78

# No container proof (argv preview with no docker to ask): sealed is not enough.
OUT="$(cd "$WS" && env -u CODEX_HOME -u LOOM_CODEX_PROFILE -u LOOM_CODEX_SESSION_DOCKER \
    LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_WORKSPACE="$WS" LOOM_CODEX_HOME="$PROFILE" \
    LOOM_ROLE=judge bash "$SPAWN_CODEX" -p "hi" 2>&1)" && rc=0 || rc=$?
check "sealed but the container's copies unproven: judge exits 78" rc_is 78
check "…and the waiver is never asked for" has "trust-bypass=never"

# Bare metal never gets the waiver, sealed or not.
SPAWN_ENV=(LOOM_CODEX_SESSION_EXEC=0)
spawn judge
check "the same sealed profile on bare metal: judge exits 78" rc_is 78
spawn curator
check "…and a read-only role runs without the waiver" lacks "$WAIVER"
SPAWN_ENV=()

# A daemon predating #10102 rejects --allow-sealed (exit 2): judged on recorded
# trust alone, so the sealed-but-untrusted seat is refused, not waived.
OLD="$TMPROOT/old-daemon"
REAL_BIN="${LOOM_DAEMON_SELF_BIN:-}"
cat > "$OLD" <<OLD_DAEMON
#!/usr/bin/env bash
for a in "\$@"; do [[ "\$a" == --allow-sealed ]] && { echo "error: unexpected argument '--allow-sealed'" >&2; exit 2; }; done
exec "$REAL_BIN" "\$@"
OLD_DAEMON
chmod +x "$OLD"
if [[ -n "$REAL_BIN" ]]; then
    SPAWN_ENV=(LOOM_DAEMON_SELF_BIN="$OLD")
    spawn judge
    check "an older daemon: falls back to recorded trust and refuses the untrusted seat" rc_is 78
    check "…without ever passing the waiver" lacks "$WAIVER"
    SPAWN_ENV=()
fi

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
