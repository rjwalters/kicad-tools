#!/usr/bin/env bash
# test-spawn-loop.sh — Smoke tests for spawn-loop.sh (#3374).
#
# These tests exercise the local, deterministic pieces:
#   - State file read/write round-trip
#   - mkdir-based lock acquire/release primitive
#   - Concurrent lock acquisition (race)
#   - Opt-in gate (LOOM_USE_SPAWN_LOOP guard)
#   - Daemon-coexistence warning
#
# What they do NOT test (intentionally):
#   - End-to-end spawning of `claude` (requires real OAuth token + network)
#   - `gh issue list` / label flipping (requires GitHub credential)
#   - Long-running shutdown (the manual test in the PR body covers this)
#
# Style mirrors test-spawn-claude.sh: plain bash, hand-rolled assertions, no
# bats. Run from the repo root or any subdir:
#
#   ./.loom/scripts/tests/test-spawn-loop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_LOOP="$SCRIPTS_DIR/spawn-loop.sh"

if [[ ! -x "$SPAWN_LOOP" ]]; then
    echo "ERROR: spawn-loop.sh not found or not executable at $SPAWN_LOOP" >&2
    exit 1
fi

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

assert_true() {
    local cond_desc="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if eval "$1"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $cond_desc"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $cond_desc"
    fi
}

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Expected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# ─── Setup: hermetic fake repo root ─────────────────────────────────────────
# We point spawn-loop at a temp dir so the tests don't touch the real
# .loom/ state. We re-implement just enough to invoke the loop's helper
# functions; the script uses `git rev-parse --git-common-dir` so we either
# `git init` or shadow with explicit paths.
TMPDIR_BASE="$(mktemp -d -t spawn-loop-test.XXXXXX)"
trap 'rm -rf "$TMPDIR_BASE"' EXIT
cd "$TMPDIR_BASE"
git init -q .
mkdir -p .loom/scripts .loom/logs .loom/locks
# Stage the script into the fake repo's expected path so internal references
# (e.g. spawn-claude.sh) at least exist as no-ops.
cp "$SPAWN_LOOP" .loom/scripts/spawn-loop.sh
chmod +x .loom/scripts/spawn-loop.sh
# Provide a stub spawn-claude.sh so spawn_sweep doesn't refuse to start when
# we exercise the spawn path in isolation later.
cat > .loom/scripts/spawn-claude.sh <<'STUB'
#!/usr/bin/env bash
# Test stub — echoes its args and sleeps, simulating a long-running child.
echo "stub-spawn-claude args: $*"
sleep 60
STUB
chmod +x .loom/scripts/spawn-claude.sh

# Force gh helpers to be quiet by stubbing gh out of PATH for this shell.
# (Sourcing the script will define functions but won't call them unless
# tick() runs, so this is just defensive.)
export PATH="$TMPDIR_BASE/bin:$PATH"
mkdir -p "$TMPDIR_BASE/bin"
cat > "$TMPDIR_BASE/bin/gh" <<'GH'
#!/usr/bin/env bash
# Test gh stub: prints nothing, exits 0 — simulates "no ready issues".
exit 0
GH
chmod +x "$TMPDIR_BASE/bin/gh"

# ─── Source the script so we can call individual functions ──────────────────
# spawn-loop.sh defines functions then dispatches on $1. We source with no
# args, which routes to usage() then exits 0 — but we want the functions
# defined without running anything. Trick: stub `main` before sourcing.
SCRIPT_BODY="$(grep -v '^main "$@"' "$SPAWN_LOOP")"
# shellcheck disable=SC1091
eval "$SCRIPT_BODY"

# ─── Section 1: state file round-trip ───────────────────────────────────────
echo "Testing state file management..."

# state_init should create the file with started_at + empty running[].
rm -f "$STATEFILE"
state_init
assert_true "[[ -f '$STATEFILE' ]]" "state_init creates state file"

initial_count="$(state_count_running)"
assert_eq "0" "$initial_count" "fresh state has zero running children"

# Add a synthetic child with PID=$$ (we know that's alive — it's us).
state_add_child 123 $$ "agent-test"
count_after_add="$(state_count_running)"
assert_eq "1" "$count_after_add" "state_add_child increments running count"

# state_list_running_issues should yield "123".
issues="$(state_list_running_issues | tr '\n' ' ' | sed 's/ *$//')"
assert_eq "123" "$issues" "state_list_running_issues returns added issue"

# Add another with a very-likely-dead PID (well above max).
state_add_child 456 999999 "agent-test"
count_after_second="$(state_count_running)"
assert_eq "2" "$count_after_second" "state file holds multiple children"

# state_reap_dead should remove the dead one (issue 456) and emit "456".
dead_output="$(state_reap_dead | tr '\n' ' ' | sed 's/ *$//')"
assert_eq "456" "$dead_output" "state_reap_dead removes pid 999999 and reports issue"
count_after_reap="$(state_count_running)"
assert_eq "1" "$count_after_reap" "state_reap_dead leaves live children alone"

# Clean up the live entry so subsequent tests start fresh.
rm -f "$STATEFILE"
state_init

# ─── Section 2: lock acquire / release ──────────────────────────────────────
echo ""
echo "Testing mkdir-based lock primitive..."

# Fresh acquire should succeed.
acquire_lock 42 && acquired_first=0 || acquired_first=1
assert_eq "0" "$acquired_first" "first acquire_lock 42 succeeds"

# Lock dir + metadata should exist.
LOCK_42="$(lock_path 42)"
assert_true "[[ -d '$LOCK_42' ]]" "lock dir created on disk"
assert_true "[[ -f '$LOCK_42/owner.json' ]]" "owner metadata file written inside lock dir"

# Second acquire (same process) should fail — mkdir on an existing dir is the
# atomic primitive we rely on.
acquire_lock 42 && acquired_second=0 || acquired_second=1
assert_eq "1" "$acquired_second" "second acquire_lock 42 fails (lock held)"

# Release and re-acquire should succeed.
release_lock 42
assert_true "[[ ! -d '$LOCK_42' ]]" "release_lock removes lock dir"
acquire_lock 42 && reacquired=0 || reacquired=1
assert_eq "0" "$reacquired" "re-acquire after release succeeds"
release_lock 42

# ─── Section 3: lock race (two children, one winner) ────────────────────────
echo ""
echo "Testing concurrent lock acquisition..."

# Spawn two background subshells racing for the same lock. Exactly one should
# succeed. Use a barrier file so they start as close together as possible.
RACE_TMP="$(mktemp -d -t spawn-loop-race.XXXXXX)"
BARRIER="$RACE_TMP/start"
WINNERS="$RACE_TMP/winners"
touch "$WINNERS"

race_worker() {
    # shellcheck disable=SC2034
    local wid="$1"
    while [[ ! -f "$BARRIER" ]]; do :; done
    if acquire_lock 7777 2>/dev/null; then
        echo "$wid" >> "$WINNERS"
    fi
}

race_worker A &
PA=$!
race_worker B &
PB=$!
touch "$BARRIER"
wait "$PA" "$PB"

winner_count="$(wc -l < "$WINNERS" | tr -d ' ')"
assert_eq "1" "$winner_count" "exactly one of two racers acquires the lock"
release_lock 7777
rm -rf "$RACE_TMP"

# ─── Section 4: opt-in gate ─────────────────────────────────────────────────
echo ""
echo "Testing LOOM_USE_SPAWN_LOOP opt-in gate..."

# Without the env var, `start` must refuse with exit 78.
set +e
output="$( "$SPAWN_LOOP" start 2>&1 )"
exit_code=$?
set -e
assert_eq "78" "$exit_code" "spawn-loop start exits 78 without LOOM_USE_SPAWN_LOOP=1"
assert_contains "LOOM_USE_SPAWN_LOOP" "$output" "refusal message mentions the env var"
assert_contains "opt-in" "$output" "refusal message explains opt-in nature"

# ─── Section 5: status when not running ─────────────────────────────────────
echo ""
echo "Testing status command (not running)..."

set +e
status_output="$( "$SPAWN_LOOP" status 2>&1 )"
status_exit=$?
set -e
assert_eq "1" "$status_exit" "status exits 1 when not running"
assert_contains "not running" "$status_output" "status output says not running"

# ─── Section 6: stop when not running ───────────────────────────────────────
echo ""
echo "Testing stop command (not running)..."

set +e
stop_output="$( "$SPAWN_LOOP" stop 2>&1 )"
stop_exit=$?
set -e
assert_eq "0" "$stop_exit" "stop exits 0 when nothing to stop (idempotent)"
assert_contains "not running" "$stop_output" "stop output says not running"

# ─── Section 7: stale PID file recovery ─────────────────────────────────────
echo ""
echo "Testing stale PID file handling..."

# Write a PID that's definitely not alive. status should detect it and exit 1
# with a "stale PID file" message.
echo 999999 > "$PIDFILE"
set +e
stale_output="$( "$SPAWN_LOOP" status 2>&1 )"
stale_exit=$?
set -e
assert_eq "1" "$stale_exit" "status with stale PID file exits 1"
assert_contains "stale PID file" "$stale_output" "status flags stale PID file"
rm -f "$PIDFILE"

# ─── Section 8: usage / help ────────────────────────────────────────────────
echo ""
echo "Testing usage output..."

set +e
help_output="$( "$SPAWN_LOOP" --help 2>&1 )"
set -e
assert_contains "spawn-loop" "$help_output" "usage mentions spawn-loop"
assert_contains "LOOM_USE_SPAWN_LOOP" "$help_output" "usage documents opt-in env var"
assert_contains "MAX_PARALLEL" "$help_output" "usage documents MAX_PARALLEL override"
assert_contains "POLL_INTERVAL" "$help_output" "usage documents POLL_INTERVAL override"

# ─── Section 9: deprecation warning (Phase E of #3449) ──────────────────────
echo ""
echo "Testing deprecation warning fires on subcommand invocations..."

# Stop the spawn-loop running in our hermetic temp dir before checking warnings
# (status uses cmd_status which calls _deprecation_warn before the not-running
# check). The script exits with 1 from cmd_status when not running, so we
# tolerate that and just inspect stderr.
set +e
status_stderr="$( "$SPAWN_LOOP" status 2>&1 1>/dev/null )"
set -e
assert_contains "DEPRECATION WARNING" "$status_stderr" "status emits deprecation banner on stderr"
assert_contains "#3449" "$status_stderr" "deprecation warning references epic #3449"
assert_contains "v0.11.0" "$status_stderr" "deprecation warning names v0.11.0 removal target"
assert_contains "mcp__loom__dispatch_sweep" "$status_stderr" "deprecation warning includes Phase A migration hint"

# Stop also fires the warning.
set +e
stop_stderr="$( "$SPAWN_LOOP" stop 2>&1 1>/dev/null )"
set -e
assert_contains "DEPRECATION WARNING" "$stop_stderr" "stop emits deprecation banner on stderr"

# Start fires the warning too (even when blocked by the opt-in gate).
set +e
start_stderr="$( "$SPAWN_LOOP" start 2>&1 1>/dev/null )"
set -e
assert_contains "DEPRECATION WARNING" "$start_stderr" "start emits deprecation banner on stderr"

# LOOM_SUPPRESS_DEPRECATION=1 silences it.
set +e
suppressed_stderr="$( LOOM_SUPPRESS_DEPRECATION=1 "$SPAWN_LOOP" status 2>&1 1>/dev/null )"
set -e
if [[ "$suppressed_stderr" == *"DEPRECATION WARNING"* ]]; then
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_SUPPRESS_DEPRECATION=1 silences the warning"
    echo "    Got stderr: '$suppressed_stderr'"
else
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_SUPPRESS_DEPRECATION=1 silences the warning"
fi

# ─── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "─────────────────────────────────────────"
echo "Tests run: $TESTS_RUN"
echo -e "Passed:    ${GREEN}$TESTS_PASSED${NC}"
if (( TESTS_FAILED > 0 )); then
    echo -e "Failed:    ${RED}$TESTS_FAILED${NC}"
    exit 1
else
    echo "Failed:    0"
fi
