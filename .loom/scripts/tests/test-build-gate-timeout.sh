#!/usr/bin/env bash
# test-build-gate-timeout.sh — regression test for #6192 ("Sweep build steps
# have no timeout — a wedged build volume accumulated 5 concurrent hung cargo
# builds for one sweep, plus orphans past sweep exit").
#
# Covers the three sweep-side mechanisms that issue added:
#
#   AC1/AC2 — `defaults/scripts/build-gate.sh` runs each toolchain stage under
#             a configurable wall-clock budget. With a stubbed `cargo` on PATH
#             standing in for a disk-wait-wedged build, the gate must fail
#             LOUDLY (naming the hung command + its elapsed time against the
#             budget) and exit a distinct 124 rather than hanging forever. A
#             fast, well-behaved run must be unaffected.
#   AC4     — a timed-out step arms the per-issue dispatch backoff (#4485) via
#             `loom-daemon dispatch-backoff record`, so the next retry is
#             deferred rather than immediate. Driven here with a recording
#             stub binary via $LOOM_DAEMON_BIN, so no daemon is needed.
#   AC3     — `lib/reap-process-group.sh`'s self-reap, in isolation.
#
# Hermetic: PATH-stubs `cargo`, stubs the daemon binary, disables the build
# slot + the `nice` re-exec, and never touches a forge, a socket, or a real
# toolchain.
#
# Every subject this suite exercises — build-gate.sh, lib/reap-process-group.sh,
# claude-wrapper.sh — is a *shipped* script (scripts/* -> .loom/scripts/* per
# scripts/install/manifest.sh), so this suite is meaningful in an installed
# consumer repo too, where no defaults/ directory exists at all. It therefore
# resolves each subject the way each layout actually lays it out:
# `.loom/scripts/<name>` first (installed consumer repos, and Loom's own
# dogfooded checkout, where .loom/scripts is a symlink to defaults/scripts),
# falling back to `defaults/scripts/<name>` (a bare source checkout with no
# .loom/scripts/ symlink/copy yet). See issue #6194.
#
# Usage:
#   bash defaults/scripts/tests/test-build-gate-timeout.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Resolve a shipped script by its path relative to the scripts root, preferring
# the installed location over the source-tree one (see the note above).
resolve_shipped_script() {
    local rel="$1"
    if [[ -f "$REPO_ROOT/.loom/scripts/$rel" ]]; then
        printf '%s\n' "$REPO_ROOT/.loom/scripts/$rel"
    else
        printf '%s\n' "$REPO_ROOT/defaults/scripts/$rel"
    fi
}

BUILD_GATE="$(resolve_shipped_script "build-gate.sh")"
REAP_LIB="$(resolve_shipped_script "lib/reap-process-group.sh")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

passed=0
failed=0
pass() { echo -e "${GREEN}✓${NC} $1"; passed=$((passed + 1)); }
fail() { echo -e "${RED}✗${NC} $1"; failed=$((failed + 1)); }

if ! command -v git >/dev/null 2>&1; then
    echo "git not found on PATH -- skipping (build-gate.sh requires a git repo)"
    exit 0
fi
if [[ ! -f "$BUILD_GATE" ]]; then
    # A genuine error in BOTH layouts: build-gate.sh is a shipped script, so it
    # should be present at one of the two resolved locations. Name both so the
    # message is actionable rather than looking like a source-tree assumption.
    echo "ERROR: build-gate.sh not found at $REPO_ROOT/.loom/scripts/build-gate.sh or $REPO_ROOT/defaults/scripts/build-gate.sh" >&2
    exit 1
fi

STUB_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$STUB_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 1' INT TERM

# Stage 0 of build-gate.sh (#9140) runs scripts/check-structural.sh whenever that
# file exists at the repo top level. This suite is about the cargo stages, so run
# the gate from a scratch git repo that has no such file (the same shape a
# consumer repo has) rather than from REPO_ROOT, where stage 0 would run the real
# structural gates against the mocked toolchain and intercept every scenario.
SCRATCH_REPO="$STUB_DIR/scratch-repo"
mkdir -p "$SCRATCH_REPO" && git -C "$SCRATCH_REPO" init -q

# ---------------------------------------------------------------------------
# Section 1: build-gate.sh per-step timeout (AC1/AC2)
# ---------------------------------------------------------------------------

# A stub `cargo` standing in for a disk-wait-wedged toolchain invocation:
# `build` hangs forever; every other subcommand exits 0 immediately (so a
# happy-path run through this stub, if ever reached, would not itself hang).
# `exec sleep` (not a plain foreground `sleep`) is load-bearing: it replaces
# THIS stub script's own process image with `sleep`, so bounded_run's
# TERM/KILL — targeted at the stub's PID — lands on the actual hung process
# rather than on a bash parent whose child would otherwise survive as a real
# leaked background process for the rest of this test run.
write_hanging_cargo_stub() {
    cat > "$STUB_DIR/cargo" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "build" ]]; then
    exec sleep 999999
fi
exit 0
EOF
    chmod +x "$STUB_DIR/cargo"
}

# Run the FAST tier (compile + startup smoke only — two stages, both `cargo`)
# with a 1s per-step budget and the portable timeout path forced, so the same
# code path is exercised identically on macOS and on a GNU-`timeout` runner.
# Extra `NAME=VALUE` env assignments may be passed as arguments. The
# `${@+"$@"}` guard (not a bare `"$@"`) is required under `set -u` on bash 3.2
# — macOS's shipped default — where an empty `"$@"` is an unbound-variable
# error.
#
# The `-u` strips are load-bearing, not defensive noise: a Loom agent running
# this suite is itself a daemon-dispatched child and therefore has
# LOOM_SWEEP_CLAIM_OWNED (and friends) already exported into its ambient
# environment. Without stripping them, the "no claimed issue" case below
# silently inherits a real issue number and the assertion inverts. Every knob
# this suite depends on is stripped first and then set explicitly, so the
# result is identical on a clean CI runner and inside a live sweep.
run_gate_fast_tier() {
    env \
        -u LOOM_SWEEP_CLAIM_OWNED \
        -u LOOM_DAEMON_BIN \
        -u LOOM_DAEMON_BIN_DIR \
        -u LOOM_PREFER_REPO_BUILD \
        -u LOOM_SWEEP_SELF_REAP \
        -u LOOM_BUILD_SLOT_HELD \
        -u LOOM_BUILD_GATE_NICED \
        PATH="$STUB_DIR:$PATH" \
        LOOM_BUILD_GATE_TIER=fast \
        LOOM_BUILD_GATE_STEP_TIMEOUT_SECS=1 \
        LOOM_FORCE_PORTABLE_TIMEOUT=1 \
        LOOM_BUILD_GATE_NICE=0 \
        LOOM_BUILD_SLOTS=0 \
        ${@+"$@"} \
        bash "$BUILD_GATE" 2>&1
}

write_hanging_cargo_stub
output="$(cd "$SCRATCH_REPO" && run_gate_fast_tier)"
rc=$?

if [[ "$rc" -eq 124 ]]; then
    pass "hung 'cargo build' step exits 124 (distinct timeout code), not a hang"
else
    fail "expected exit 124 for a hung step, got $rc"
fi

if printf '%s' "$output" | grep -qi "TIMEOUT"; then
    pass "timeout failure is loud (contains TIMEOUT)"
else
    fail "expected a loud TIMEOUT message in output, got: $output"
fi

if printf '%s' "$output" | grep -q "cargo build"; then
    pass "timeout message names the hung command (cargo build)"
else
    fail "expected the hung command to be named in output, got: $output"
fi

if printf '%s' "$output" | grep -Eq '[0-9]+s \(budget'; then
    pass "timeout message reports elapsed time against the configured budget"
else
    fail "expected an elapsed-time report in output, got: $output"
fi

# Opt-out: LOOM_BUILD_GATE_STEP_TIMEOUT_SECS=0 restores plain unbounded
# execution -- with a hung stub this would hang forever, so instead assert
# the OPPOSITE shape: a stub that succeeds fast is unaffected by the new
# machinery at all (byte-for-byte usable when bounding is disabled).
cat > "$STUB_DIR/cargo" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB_DIR/cargo"

happy_output="$(cd "$SCRATCH_REPO" && run_gate_fast_tier)"
happy_rc=$?

if [[ "$happy_rc" -eq 0 ]]; then
    pass "a fast, well-behaved fast-tier run still passes (0) with bounding enabled"
else
    fail "expected exit 0 for a well-behaved run, got $happy_rc: $happy_output"
fi

if printf '%s' "$happy_output" | grep -qi "TIMEOUT"; then
    fail "well-behaved run should not report a TIMEOUT, got: $happy_output"
else
    pass "well-behaved run reports no spurious TIMEOUT"
fi

# ---------------------------------------------------------------------------
# Section 2: a timed-out step arms the dispatch backoff (AC4)
# ---------------------------------------------------------------------------

# Recording stub for the daemon binary: appends its own argv to a file so the
# test can assert the exact `dispatch-backoff record <N>` call shape (a
# positional issue argument, NOT an `--issue` flag) without a running daemon
# or a socket.
BACKOFF_LOG="$STUB_DIR/backoff-calls.log"
cat > "$STUB_DIR/loom-daemon" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$BACKOFF_LOG"
exit 0
EOF
chmod +x "$STUB_DIR/loom-daemon"

write_hanging_cargo_stub
: > "$BACKOFF_LOG"
timeout_output="$(cd "$SCRATCH_REPO" && run_gate_fast_tier \
    LOOM_SWEEP_CLAIM_OWNED=6192 \
    LOOM_DAEMON_BIN="$STUB_DIR/loom-daemon")"
timeout_rc=$?

if [[ "$timeout_rc" -eq 124 ]]; then
    pass "timeout still exits 124 with the backoff arm wired in"
else
    fail "expected exit 124, got $timeout_rc: $timeout_output"
fi

if grep -q "dispatch-backoff record" "$BACKOFF_LOG" 2>/dev/null; then
    pass "a timed-out step invokes 'loom-daemon dispatch-backoff record' (#4485)"
else
    fail "expected a dispatch-backoff record call, log was: $(cat "$BACKOFF_LOG" 2>/dev/null)"
fi

if grep -Eq '(^|[[:space:]])6192([[:space:]]|$)' "$BACKOFF_LOG" 2>/dev/null; then
    pass "the backoff is armed for the sweep's own claimed issue (positional issue arg)"
else
    fail "expected a bare positional '6192' in the recorded call, log was: $(cat "$BACKOFF_LOG" 2>/dev/null)"
fi

# The issue number must be POSITIONAL — the real `dispatch-backoff record` CLI
# (DispatchBackoffAction::Record in loom-daemon/src/main.rs) takes ISSUE as a
# bare positional arg, not a `--issue` flag. A `--issue` flag is a clap
# "unexpected argument" parse error against the real binary (#6815).
if grep -q -- "--issue" "$BACKOFF_LOG" 2>/dev/null; then
    fail "recorded call used a non-existent '--issue' flag: $(cat "$BACKOFF_LOG" 2>/dev/null)"
else
    pass "recorded call does not use the non-existent '--issue' flag"
fi

if grep -q "build-gate timeout" "$BACKOFF_LOG" 2>/dev/null; then
    pass "the recorded reason attributes the backoff to a build-gate timeout"
else
    fail "expected a 'build-gate timeout' reason, log was: $(cat "$BACKOFF_LOG" 2>/dev/null)"
fi

# No claimed issue (a manual gate run, or the daemon's own main-health gate)
# => nothing to back off, and the gate must not invoke the daemon at all.
: > "$BACKOFF_LOG"
noclaim_output="$(cd "$SCRATCH_REPO" && run_gate_fast_tier \
    LOOM_DAEMON_BIN="$STUB_DIR/loom-daemon")"
noclaim_rc=$?

if [[ "$noclaim_rc" -eq 124 ]]; then
    pass "a claim-less run still fails loudly with 124 on a hung step"
else
    fail "expected exit 124 for a claim-less hung run, got $noclaim_rc: $noclaim_output"
fi

if [[ -s "$BACKOFF_LOG" ]]; then
    fail "a claim-less run must not arm a backoff, but recorded: $(cat "$BACKOFF_LOG")"
else
    pass "a claim-less run arms no backoff (nothing to defer)"
fi

# An unreachable/erroring daemon must never change the gate's own verdict.
cat > "$STUB_DIR/loom-daemon" <<'EOF'
#!/usr/bin/env bash
echo "Could not reach loom-daemon" >&2
exit 1
EOF
chmod +x "$STUB_DIR/loom-daemon"

deadd_output="$(cd "$SCRATCH_REPO" && run_gate_fast_tier \
    LOOM_SWEEP_CLAIM_OWNED=6192 \
    LOOM_DAEMON_BIN="$STUB_DIR/loom-daemon")"
deadd_rc=$?

if [[ "$deadd_rc" -eq 124 ]]; then
    pass "a failing/unreachable daemon leaves the timeout verdict at 124 (best-effort arm)"
else
    fail "expected exit 124 despite a failing daemon, got $deadd_rc: $deadd_output"
fi

# ---------------------------------------------------------------------------
# Section 3: lib/reap-process-group.sh self-reap (AC3)
# ---------------------------------------------------------------------------

if [[ ! -f "$REAP_LIB" ]]; then
    fail "lib/reap-process-group.sh not found at $REAP_LIB"
else
    # shellcheck source=/dev/null
    source "$REAP_LIB"
    # Same hermeticity concern as the `env -u` list above: this section runs
    # the library IN THIS SHELL, so an ambient opt-out would silently pass the
    # default-behavior assertions below.
    unset LOOM_SWEEP_SELF_REAP

    # Opt-out first: LOOM_SWEEP_SELF_REAP=0 must be a true no-op.
    sleep 60 &
    child_pid=$!
    LOOM_SWEEP_SELF_REAP=0 loom_reap_own_process_group "test" >/dev/null 2>&1
    if kill -0 "$child_pid" 2>/dev/null; then
        pass "LOOM_SWEEP_SELF_REAP=0 leaves a residual child alive (opt-out honored)"
    else
        fail "LOOM_SWEEP_SELF_REAP=0 should not reap anything, but the child is gone"
    fi
    kill -9 "$child_pid" 2>/dev/null
    wait "$child_pid" 2>/dev/null

    # Default (opt-in by default): a residual background child is TERM'd (and
    # KILL'd if it survives) by the time the function returns.
    sleep 60 &
    child_pid=$!
    loom_reap_own_process_group "test" >/dev/null 2>&1
    sleep 0.5
    if kill -0 "$child_pid" 2>/dev/null; then
        fail "loom_reap_own_process_group left a residual child alive"
        kill -9 "$child_pid" 2>/dev/null
    else
        pass "loom_reap_own_process_group reaps a residual child by default"
    fi
    wait "$child_pid" 2>/dev/null

    # Grandchildren count too: the incident's orphans were a build's children
    # and a pipe-holding `tail`, not just direct children of the wrapper.
    bash -c 'sleep 60 & echo $! > "$1"; sleep 60' _ "$STUB_DIR/grandchild.pid" &
    nest_pid=$!
    sleep 1
    grandchild_pid="$(cat "$STUB_DIR/grandchild.pid" 2>/dev/null || true)"
    loom_reap_own_process_group "test" >/dev/null 2>&1
    sleep 0.5
    if [[ -z "$grandchild_pid" ]]; then
        fail "grandchild fixture never recorded its pid — test setup problem"
    elif kill -0 "$grandchild_pid" 2>/dev/null; then
        fail "loom_reap_own_process_group left a grandchild alive (pid $grandchild_pid)"
        kill -9 "$grandchild_pid" 2>/dev/null
    else
        pass "loom_reap_own_process_group reaps grandchildren, not just direct children"
    fi
    kill -9 "$nest_pid" 2>/dev/null
    wait "$nest_pid" 2>/dev/null

    # Never touches the caller's own PID.
    loom_reap_own_process_group "test" >/dev/null 2>&1
    if kill -0 "$$" 2>/dev/null; then
        pass "loom_reap_own_process_group never signals its own caller's PID"
    else
        fail "loom_reap_own_process_group must not have killed the calling shell"
    fi
fi

# ---------------------------------------------------------------------------
# Section 4: the self-reap actually fires at exit, and is not clobbered (AC3)
# ---------------------------------------------------------------------------

# End-to-end proof of the integration pattern: a script that sources the
# library, installs it on EXIT, and leaves a background child behind must have
# no surviving child once it returns. (Sections above test the function when
# called directly; this tests it as an EXIT trap, which is how every caller
# actually uses it.)
cat > "$STUB_DIR/exiting-script.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
source "$REAP_LIB"
trap 'loom_reap_own_process_group "fixture"' EXIT
sleep 120 &
echo \$! > "$STUB_DIR/fixture-child.pid"
exit 0
EOF
chmod +x "$STUB_DIR/exiting-script.sh"

env -u LOOM_SWEEP_SELF_REAP bash "$STUB_DIR/exiting-script.sh" >/dev/null 2>&1
sleep 0.5
fixture_child="$(cat "$STUB_DIR/fixture-child.pid" 2>/dev/null || true)"
if [[ -z "$fixture_child" ]]; then
    fail "fixture never recorded its child pid — test setup problem"
elif kill -0 "$fixture_child" 2>/dev/null; then
    fail "an EXIT-trapped self-reap left an orphan behind (pid $fixture_child)"
    kill -9 "$fixture_child" 2>/dev/null
else
    pass "an EXIT-trapped self-reap leaves no orphan when its script exits"
fi

# …and on an external kill, not just a clean exit (AC3 says "success, failure,
# OR kill"). SIGTERM is what the daemon's own group-kill and a `loom-daemon
# cancel` deliver; bash runs the EXIT trap for it. SIGKILL is untrappable by
# construction and stays the daemon-side reaper's job.
cat > "$STUB_DIR/killed-script.sh" <<EOF
#!/usr/bin/env bash
set -uo pipefail
source "$REAP_LIB"
trap 'loom_reap_own_process_group "fixture"' EXIT
sleep 120 &
echo \$! > "$STUB_DIR/killed-child.pid"
sleep 120
EOF
chmod +x "$STUB_DIR/killed-script.sh"

rm -f "$STUB_DIR/killed-child.pid"
env -u LOOM_SWEEP_SELF_REAP bash "$STUB_DIR/killed-script.sh" >/dev/null 2>&1 &
killed_pid=$!
sleep 1
killed_child="$(cat "$STUB_DIR/killed-child.pid" 2>/dev/null || true)"
kill -TERM "$killed_pid" 2>/dev/null
wait "$killed_pid" 2>/dev/null
sleep 3
if [[ -z "$killed_child" ]]; then
    fail "kill fixture never recorded its child pid — test setup problem"
elif kill -0 "$killed_child" 2>/dev/null; then
    fail "SIGTERM-killed script left an orphan behind (pid $killed_child)"
    kill -9 "$killed_child" 2>/dev/null
else
    pass "a SIGTERM-killed script still reaps its children (no launchd orphan)"
fi

# Bash keeps exactly ONE EXIT trap: a later `trap ... EXIT` silently REPLACES
# an earlier one rather than composing with it. Both scripts wired for #6192
# already had their own EXIT handler (build-gate.sh releases the machine-wide
# build slot; claude-wrapper.sh clears the retry-state file), so the self-reap
# must be folded INTO those handlers. Adding it as a second `trap ... EXIT`
# compiles, lints, and runs fine while silently doing nothing (or, worse,
# leaking a machine-wide build slot on every run) — which is exactly why this
# is asserted structurally rather than left to review.
assert_single_exit_trap() { # <script-path> <expected-handler> <label>
    local script="$1" handler="$2" label="$3"
    local traps
    traps="$(grep -Eo '^[[:space:]]*trap[[:space:]]+[^#]*EXIT' "$script" || true)"
    if [[ -z "$traps" ]]; then
        fail "$label installs no EXIT trap at all"
        return
    fi
    local bad
    bad="$(printf '%s\n' "$traps" | grep -v "$handler" || true)"
    if [[ -n "$bad" ]]; then
        fail "$label has an EXIT trap that bypasses $handler: $bad"
    else
        pass "$label routes every EXIT trap through $handler (no silent clobber)"
    fi
}

assert_handler_does_both() { # <script-path> <handler> <needle-a> <needle-b> <label>
    local script="$1" handler="$2" a="$3" b="$4" label="$5"
    local body
    body="$(awk -v fn="^${handler}\\\\(\\\\) \\\\{" '
        $0 ~ fn { inside = 1; next }
        inside && /^}/ { inside = 0 }
        inside { print }
    ' "$script")"
    if [[ -z "$body" ]]; then
        fail "$label: could not locate ${handler}()"
        return
    fi
    if printf '%s' "$body" | grep -q "$a" && printf '%s' "$body" | grep -q "$b"; then
        pass "$label: ${handler}() performs both its pre-existing cleanup and the #6192 reap"
    else
        fail "$label: ${handler}() is missing '$a' and/or '$b'"
    fi
}

assert_single_exit_trap "$BUILD_GATE" "_build_gate_exit_cleanup" "build-gate.sh"
assert_handler_does_both "$BUILD_GATE" "_build_gate_exit_cleanup" \
    "loom_build_slot_release" "loom_reap_own_process_group" "build-gate.sh"

WRAPPER="$(resolve_shipped_script "claude-wrapper.sh")"
if [[ -f "$WRAPPER" ]]; then
    assert_single_exit_trap "$WRAPPER" "_wrapper_exit_cleanup" "claude-wrapper.sh"
    assert_handler_does_both "$WRAPPER" "_wrapper_exit_cleanup" \
        "clear_retry_state" "loom_reap_own_process_group" "claude-wrapper.sh"
else
    fail "claude-wrapper.sh not found at $WRAPPER"
fi

echo ""
echo "=== Results: $passed passed, $failed failed ==="
if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
exit 0
