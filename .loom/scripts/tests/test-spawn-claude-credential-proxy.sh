#!/usr/bin/env bash
# test-spawn-claude-credential-proxy.sh — spawn-claude.sh's contained dispatch
# routed through the credential egress proxy (issue #8697, follow-up to #8674).
#
# Split out of test-spawn-claude.sh rather than appended to it: that file is
# over scripts/check-file-size-budget.sh's threshold and therefore frozen.
#
# What is real here: spawn-claude.sh itself, host-side token selection against
# a real `loom-daemon tokens select`, and `loom-daemon worker proxy-exec` with
# a real listener. What is stubbed: `docker` (it execs the in-container command
# in this shell, seeding `-e KEY=VALUE` pairs exactly as docker would, and
# otherwise passing on the environment the docker CLIENT was handed — which is
# precisely what a real `-e KEY` reads) and `claude` (it records the
# environment it was started with). So "the container's environment" asserted
# below is exactly the environment a real container would be seeded from.
#
# Style matches test-spawn-claude.sh — plain bash, hand-rolled assertions.
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-claude-credential-proxy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEFAULTS_DIR/.." && pwd)"

# Pin THIS checkout's own binary (same rationale as test-spawn-claude.sh).
DAEMON_BIN=""
for _candidate in \
    "${CARGO_TARGET_DIR:+$CARGO_TARGET_DIR/release/loom-daemon}" \
    "${CARGO_TARGET_DIR:+$CARGO_TARGET_DIR/debug/loom-daemon}" \
    "$REPO_ROOT/target/release/loom-daemon" \
    "$REPO_ROOT/target/debug/loom-daemon"; do
    # First candidate that actually has the subcommand: a stale release build
    # next to a fresh debug one must not decide the suite.
    if [[ -n "$_candidate" && -x "$_candidate" ]] \
        && "$_candidate" worker proxy-exec --help >/dev/null 2>&1; then
        DAEMON_BIN="$_candidate"
        break
    fi
done
if [[ -z "$DAEMON_BIN" ]]; then
    echo "FAIL: no built loom-daemon with 'worker proxy-exec' (cargo build -p loom-daemon first)" >&2
    exit 1
fi

export LOOM_SWEEP_INFLIGHT_SWEEPS=1
export LOOM_SWEEP_CPU_QUOTA=0
# Loopback bind: no `docker network inspect`, and the base URL names
# host.docker.internal, which the stub claude reaches with curl --connect-to.
export LOOM_EGRESS_PROXY_BIND=127.0.0.1

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3"; echo "    Expected: '$1'"; echo "    Actual:   '$2'"; fi
}
assert_contains() {
    if [[ "$2" == *"$1"* ]]; then pass "$3"; else fail "$3"; echo "    Expected to contain: '$1'"; echo "    In: '$2'"; fi
}
assert_not_contains() {
    if [[ "$2" != *"$1"* ]]; then pass "$3"; else fail "$3"; echo "    Expected NOT to contain: '$1'"; fi
}

echo "==========================================="
echo "test-spawn-claude-credential-proxy.sh (#8697)"
echo "==========================================="

REAL_TOKEN="sk-ant-oat01-fake-real-token-8697"

WS="$(mktemp -d)"
mkdir -p "$WS/.loom/tokens" "$WS/.loom/api-keys"
chmod 700 "$WS/.loom/tokens"
echo -n "$REAL_TOKEN" > "$WS/.loom/tokens/acct.token"
chmod 600 "$WS/.loom/tokens/acct.token"
ln -s "$SCRIPTS_DIR" "$WS/.loom/scripts"

STUBS="$(mktemp -d)"
trap 'rm -rf "$WS" "$STUBS"' EXIT

cat > "$STUBS/claude" <<STUB
#!/usr/bin/env bash
env > "$STUBS/claude-env.txt"
printf '%s\n%s\n' "\${CLAUDE_CODE_OAUTH_TOKEN:-}" "\${ANTHROPIC_BASE_URL:-}" > "$STUBS/claude-seen.txt"
: > "$STUBS/claude-curl.txt"
if [[ -n "\${ANTHROPIC_BASE_URL:-}" ]] && command -v curl >/dev/null 2>&1; then
    curl -s --max-time 5 --connect-to "host.docker.internal::127.0.0.1:" \\
        -H "authorization: Bearer loom-placeholder-bogus" -X POST -d '{}' \\
        "\$ANTHROPIC_BASE_URL/v1/messages" > "$STUBS/claude-curl.txt" 2>&1 || true
fi
echo "stub-claude ran"
STUB
chmod +x "$STUBS/claude"

DOCKER_LOG="$STUBS/docker.log"
cat > "$STUBS/docker" <<STUB
#!/usr/bin/env bash
[[ "\${1:-}" == "run" ]] || exit 1
printf '%s\n' "\$*" >> "$DOCKER_LOG"
args=("\$@")
i=1
while true; do
    case "\${args[i]:-}" in
        --rm) i=\$((i + 1)) ;;
        -v | --mount | --add-host | --label | --cpus | --memory | -w) i=\$((i + 2)) ;;
        -e)
            case "\${args[i+1]:-}" in *=*) export "\${args[i+1]}" ;; esac
            i=\$((i + 2))
            ;;
        *) break ;;
    esac
done
i=\$((i + 1)) # image
exec "\${args[@]:i}"
STUB
chmod +x "$STUBS/docker"

# Run spawn-claude.sh with a clean slate for every variable that decides the
# path under test; extra KEY=VALUE pairs come in as arguments.
run_spawn() {
    : > "$DOCKER_LOG"
    rm -f "$STUBS/claude-env.txt" "$STUBS/claude-seen.txt" "$STUBS/claude-curl.txt"
    env -u CLAUDE_CODE_OAUTH_TOKEN -u LOOM_SWEEP_CREDENTIAL_PROXY \
        -u LOOM_SWEEP_CONTAINERIZED -u LOOM_SPAWN_CONTAINERIZED -u LOOM_SPAWN_NO_EXPORT \
        -u ANTHROPIC_BASE_URL -u LOOM_TOKEN_NAME \
        LOOM_WORKSPACE="$WS" LOOM_DAEMON_BIN="$DAEMON_BIN" \
        LOOM_SHARED_TOKENS_DIR="$STUBS/no-shared-pool" \
        PATH="$STUBS:$PATH" "$@" \
        "$SCRIPTS_DIR/spawn-claude.sh" -p "ping" 2>&1
}

# ------------------------------------------------------------------ AC1/AC2
echo ""
echo "Proxy ON: the container sees only a placeholder..."
echo '{"runtimes": {"containment": {"enabled": true, "claudeCredentialProxy": true}}}' > "$WS/.loom/config.json"
out="$(run_spawn || true)"
docker_log="$(cat "$DOCKER_LOG")"
container_env="$(cat "$STUBS/claude-env.txt" 2>/dev/null || true)"
seen_token="$(sed -n 1p "$STUBS/claude-seen.txt" 2>/dev/null || true)"
seen_base="$(sed -n 2p "$STUBS/claude-seen.txt" 2>/dev/null || true)"

assert_contains "stub-claude ran" "$out" "the proxied launch still reaches claude"
assert_contains "# LOOM_ACCOUNT name=acct" "$out" "the account is selected on the HOST"
assert_contains "# LOOM_EGRESS_PROXY launch=" "$out" "proxy-exec writes its secret-free marker to the sweep log"
assert_contains "upstream=https://api.anthropic.com" "$out" "the upstream is pinned to api.anthropic.com"
assert_contains "loom-placeholder-" "$seen_token" "CLAUDE_CODE_OAUTH_TOKEN inside is a Loom placeholder"
assert_not_contains "$REAL_TOKEN" "$container_env" "the real token is NOWHERE in the container environment"
assert_not_contains "$REAL_TOKEN" "$docker_log" "the real token is not in the docker argv"
assert_not_contains "$REAL_TOKEN" "$out" "the real token is not in the sweep log"
assert_contains "http://host.docker.internal:" "$seen_base" "ANTHROPIC_BASE_URL points at the host-side proxy"
assert_contains "-e CLAUDE_CODE_OAUTH_TOKEN " "$docker_log" "the credential variable is forwarded by NAME only"
assert_contains "-e ANTHROPIC_BASE_URL " "$docker_log" "the base URL is forwarded by name"
assert_contains "--add-host host.docker.internal:host-gateway" "$docker_log" "the container can reach the host listener"
assert_contains "--mount type=tmpfs,destination=$WS/.loom/tokens,tmpfs-mode=0555" "$docker_log" \
    "the per-repo token pool is masked out of the container filesystem"
assert_contains "--mount type=tmpfs,destination=$WS/.loom/api-keys,tmpfs-mode=0555" "$docker_log" \
    "the per-repo API-key pool is masked out of the container filesystem"
assert_contains "-e LOOM_TOKEN_NAME " "$docker_log" "the host-selected account name is forwarded by name"
assert_not_contains "no-shared-pool" "$docker_log" "the shared token pool is never mounted"
assert_contains "containment=claude-ephemeral" "$out" "the containment marker is unchanged"
if command -v curl >/dev/null 2>&1; then
    assert_contains "unknown_placeholder" "$(cat "$STUBS/claude-curl.txt")" \
        "the proxy is live during the launch and refuses a placeholder it did not issue"
    # AC3: once the launch has ended, its placeholder reaches nothing.
    port="${seen_base##*:}"
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST -d '{}' \
        -H "authorization: Bearer $seen_token" "http://127.0.0.1:${port}/v1/messages" || true)"
    assert_eq "000" "$code" "after the launch exits the placeholder is dead (listener gone)"
fi

# ------------------------------------------------------------------ AC4
echo ""
echo "Proxy OFF / containment OFF: unchanged..."
echo '{"runtimes": {"containment": {"enabled": true}}}' > "$WS/.loom/config.json"
out="$(run_spawn || true)"
docker_log="$(cat "$DOCKER_LOG")"
assert_contains "stub-claude ran" "$out" "flag off: the contained launch runs"
assert_not_contains "# LOOM_EGRESS_PROXY" "$out" "flag off: no proxy"
assert_not_contains "type=tmpfs" "$docker_log" "flag off: no pool masking"
assert_not_contains "host-gateway" "$docker_log" "flag off: no --add-host"
assert_eq "$REAL_TOKEN" "$(sed -n 1p "$STUBS/claude-seen.txt")" \
    "flag off: the in-container selection path is unchanged (real token, pre-#8697 behaviour)"

echo '{"runtimes": {"containment": {"enabled": true, "claudeCredentialProxy": true}}}' > "$WS/.loom/config.json"
out="$(run_spawn LOOM_SWEEP_CREDENTIAL_PROXY=0 || true)"
assert_not_contains "# LOOM_EGRESS_PROXY" "$out" "LOOM_SWEEP_CREDENTIAL_PROXY=0 wins over config true"

echo '{"runtimes": {"containment": {"claudeCredentialProxy": true}}}' > "$WS/.loom/config.json"
out="$(run_spawn || true)"
assert_contains "# LOOM_DISPATCH_MODE mode=bare-metal" "$out" "containment off: the proxy flag alone does nothing"
assert_eq "" "$(cat "$DOCKER_LOG")" "containment off: docker is never invoked"
assert_eq "$REAL_TOKEN" "$(sed -n 1p "$STUBS/claude-seen.txt")" "containment off: uncontained dispatch unchanged"

# ------------------------------------------------------------ fail closed
echo ""
echo "Fail closed..."
echo '{"runtimes": {"containment": {"enabled": true, "claudeCredentialProxy": true}}}' > "$WS/.loom/config.json"

set +e
out="$(run_spawn LOOM_SPAWN_NO_EXPORT=1)"
rc=$?
set -e
assert_eq "78" "$rc" "no host credential: refused with EX_CONFIG"
assert_eq "" "$(cat "$DOCKER_LOG")" "no host credential: docker is never invoked"

OLD="$STUBS/old-daemon"
mkdir -p "$OLD"
cat > "$OLD/loom-daemon" <<STUB
#!/usr/bin/env bash
[[ "\$1" == "worker" ]] && exit 2
exec "$DAEMON_BIN" "\$@"
STUB
chmod +x "$OLD/loom-daemon"
set +e
out="$(run_spawn LOOM_DAEMON_BIN="$OLD/loom-daemon")"
rc=$?
set -e
assert_eq "78" "$rc" "a daemon without 'worker proxy-exec': refused, never an unproxied container launch"
assert_contains "Refusing to forward the real credential" "$out" "the refusal names why"
assert_eq "" "$(cat "$DOCKER_LOG")" "a daemon without 'worker proxy-exec': docker is never invoked"

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
