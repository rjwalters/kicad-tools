#!/usr/bin/env bash
# test-spawn-codex-guarded.sh — spawn-codex.sh's merging-role hook gate
# (rjwalters/loom#9390). Split out of test-spawn-codex.sh, which the file-size
# ratchet freezes.
#
# Champion merges and Judge issues the verdict a merge relies on, so on Codex
# both need Loom's managed pre_tool_use hook proven exactly like a mutable
# role, and fail closed (exit 78) before the CLI starts without it. They keep
# the read-only sandbox. Read-only roles keep the warn-and-proceed fallback.
#
# Hermetic: LOOM_CODEX_NO_EXEC=1 prints the argv instead of running Codex, and
# LOOM_SPAWN_NO_EXPORT=1 skips account selection. Readiness is the real
# `loom-daemon codex-hooks verify` behind provision-codex-hooks.sh, pinned to
# the working-tree build; the suite FAILS, never skips, without one.
#
# Usage: ./defaults/scripts/tests/test-spawn-codex-guarded.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_CODEX="$SCRIPTS_DIR/spawn-codex.sh"
PROVISION_SCRIPT="$SCRIPTS_DIR/provision-codex-hooks.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "codex-hooks"

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
export LOOM_WORKSPACE="$WS"

profile() { mkdir -p "$TMPROOT/$1" && printf '{"tokens":{}}\n' > "$TMPROOT/$1/auth.json" && printf '%s' "$TMPROOT/$1"; }
READY="$(profile ready)"; UNTRUSTED="$(profile untrusted)"; BARE="$(profile bare)"
bash "$PROVISION_SCRIPT" install --codex-home "$READY" --workspace "$WS" >/dev/null 2>&1
bash "$PROVISION_SCRIPT" install --codex-home "$UNTRUSTED" --workspace "$WS" >/dev/null 2>&1
# The operator's trust decision, keyed as Codex keys it for a bare-metal run.
printf '[hooks.state."%s/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "op"\n' "$(cd -P "$READY" && pwd -P)" > "$READY/config.toml"
# Trust recorded somewhere Codex will not look it up for this profile.
printf '[hooks.state."/elsewhere/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "op"\n' > "$UNTRUSTED/config.toml"

# Sets OUT and rc in THIS shell (never call it inside $(...): the
# assignments would be lost in the subshell).
OUT=""
rc=0
spawn() { # <role> <profile>
    rc=0
    OUT="$(env -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE -u LOOM_CODEX_SANDBOX -u LOOM_MODEL \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
        LOOM_ROLE="$1" CODEX_HOME="$2" bash "$SPAWN_CODEX" -p "hi" 2>&1)" || rc=$?
}

has() { [[ "$OUT" == *"$1"* ]]; }
lacks() { [[ "$OUT" != *"$1"* ]]; }
rc_is() { [[ "$rc" == "$1" ]]; }
echo "Testing the merging-role hook gate (#9390)..."
for role in champion judge; do
    spawn "$role" "$BARE"
    check "$role + unprovisioned profile -> exit 78" rc_is 78
    check "$role: no codex argv is assembled" lacks would-exec
    check "$role: the audit line marks the hook as required" has "required=true"
    spawn "$role" "$UNTRUSTED"
    check "$role + trust recorded at the wrong location -> exit 78" rc_is 78
    spawn "$role" "$READY"
    check "$role + ready managed hook -> proceeds" rc_is 0
    check "$role + ready managed hook: audit line says hooks=ready" has "hooks=ready"
    check "$role keeps the read-only sandbox" has "sandbox=read-only"
done
for role in curator hermit; do
    spawn "$role" "$BARE"
    check "$role (read-only) + unprovisioned profile -> proceeds" rc_is 0
    check "$role (read-only): warned that hook parity is unavailable" has "hook parity unavailable"
    check "$role (read-only): the hook is not required" has "required=false"
done
spawn builder "$BARE"
check "builder (mutable) is still refused" rc_is 78

# An adopted (session-managed) profile forced onto bare metal with
# LOOM_CODEX_SESSION_EXEC=0: Codex keys trust by the HOST path there, so trust
# taken only inside the container must not pass for this launch, and host-keyed
# trust must. spawn-codex names the runtime CODEX_HOME to verify (#9390).
ADOPTED="$(profile adopted)"
bash "$PROVISION_SCRIPT" install --codex-home "$ADOPTED" --workspace "$WS" >/dev/null 2>&1
printf '{}\n' > "$ADOPTED/.session-managed.json"
printf '[hooks.state."/home/loom/.codex-profile/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "op"\n' > "$ADOPTED/config.toml"
export LOOM_CODEX_SESSION_EXEC=0
spawn judge "$ADOPTED"
check "judge on bare metal with container-only trust -> exit 78" rc_is 78
printf '[hooks.state."%s/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "op"\n' "$(cd -P "$ADOPTED" && pwd -P)" > "$ADOPTED/config.toml"
spawn judge "$ADOPTED"
check "judge on bare metal with host-keyed trust -> proceeds" rc_is 0
unset LOOM_CODEX_SESSION_EXEC

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
