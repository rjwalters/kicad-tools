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
    LOOM_ROLE=judge LOOM_SWEEP_ID=sweep-1 ANTHROPIC_API_KEY=never-forward-me \
    bash "$SPAWN_CODEX" -p "hi" 2>&1 || true)"
line="$(printf '%s\n' "$out" | grep '^spawn-codex would-exec:' || true)"
assert_contains "--env LOOM_ROLE=judge" "$line" "LOOM_ROLE is forwarded into the container"
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

echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
