#!/usr/bin/env bash
# test-build-gate-runner.sh — regression test for #8326 ("build-gate.sh runs
# `cargo test`, the shared-process runner `.config/nextest.toml` exists to
# avoid").
#
# Covers the three behaviours that issue added to the FULL tier of
# `defaults/scripts/build-gate.sh`:
#
#   AC1 — with `cargo-nextest` on PATH the gate runs
#         `cargo nextest run --workspace --lib --bins --profile ci`
#         (process-per-test, the runner + profile CI uses), NOT `cargo test`.
#   AC2 — with `cargo-nextest` absent the gate DEGRADES to
#         `cargo test --workspace --lib --bins`, but loudly: a `[build-gate]
#         WARNING:` block naming #4385 and `cargo install cargo-nextest`.
#         A silent downgrade is the failure mode this asserts against.
#   AC3 — doctest coverage is not lost by the switch: `cargo test --workspace
#         --doc` runs as its own step on BOTH branches (nextest does not run
#         doctests, #4385).
#
# Hermetic: PATH-stubs `cargo` with a recorder that never invokes a real
# toolchain, disables the build slot + the `nice` re-exec, forces the portable
# timeout path, and never touches a forge, a socket, or the network. The stub
# is told to fail on the `--doc` invocation so the gate aborts (set -e) right
# after both Rust steps have been recorded, rather than going on to run the
# real multi-minute bash suites that follow them.
#
# Layout resolution mirrors test-build-gate-timeout.sh (#6194): build-gate.sh
# is a *shipped* script, so prefer `.loom/scripts/` (installed consumer repos,
# and Loom's own dogfooded checkout where `.loom/scripts` is a symlink to
# `defaults/scripts`) and fall back to `defaults/scripts/`.
#
# Usage:
#   bash defaults/scripts/tests/test-build-gate-runner.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

resolve_shipped_script() {
    local rel="$1"
    if [[ -f "$REPO_ROOT/.loom/scripts/$rel" ]]; then
        printf '%s\n' "$REPO_ROOT/.loom/scripts/$rel"
    else
        printf '%s\n' "$REPO_ROOT/defaults/scripts/$rel"
    fi
}

BUILD_GATE="$(resolve_shipped_script "build-gate.sh")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

passed=0
failed=0
skipped=0
pass() { echo -e "${GREEN}✓${NC} $1"; passed=$((passed + 1)); }
fail() { echo -e "${RED}✗${NC} $1"; failed=$((failed + 1)); }
skip() { echo -e "${YELLOW}—${NC} SKIP: $1"; skipped=$((skipped + 1)); }

if ! command -v git >/dev/null 2>&1; then
    echo "git not found on PATH -- skipping (build-gate.sh requires a git repo)"
    exit 0
fi
if [[ ! -f "$BUILD_GATE" ]]; then
    echo "ERROR: build-gate.sh not found at $REPO_ROOT/.loom/scripts/build-gate.sh or $REPO_ROOT/defaults/scripts/build-gate.sh" >&2
    exit 1
fi

STUB_DIR="$(mktemp -d)"
cleanup() { rm -rf "$STUB_DIR" 2>/dev/null || true; }
trap cleanup EXIT
trap 'cleanup; exit 1' INT TERM

# Stage 0 of build-gate.sh (#9140) runs scripts/check-structural.sh whenever that
# file exists at the repo top level. This suite is about the cargo stages, so run
# the gate from a scratch git repo that has no such file (the same shape a
# consumer repo has) rather than from REPO_ROOT, where stage 0 would run the real
# structural gates against the mocked toolchain and intercept every scenario.
SCRATCH_REPO="$STUB_DIR/scratch-repo"
mkdir -p "$SCRATCH_REPO" && git -C "$SCRATCH_REPO" init -q

CARGO_LOG="$STUB_DIR/cargo-calls.log"

# A SECOND scratch repo that DOES carry a scripts/check-structural.sh -- a
# recording stub, not the real 31-gate aggregate -- so Section 0 below can pin
# stage 0's wiring without re-entering the hermeticity problem the comment above
# describes. It records into $CARGO_LOG so the ORDER of stage 0 against the cargo
# steps is readable from one log, and honours LOOM_TEST_STRUCTURAL_RC so the
# "a red stage 0 stops the gate" case can be exercised too.
STAGE0_REPO="$STUB_DIR/stage0-repo"
mkdir -p "$STAGE0_REPO/scripts" && git -C "$STAGE0_REPO" init -q
cat > "$STAGE0_REPO/scripts/check-structural.sh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "STRUCTURAL-STUB" >> "$CARGO_LOG"
exit "\${LOOM_TEST_STRUCTURAL_RC:-0}"
EOF
chmod +x "$STAGE0_REPO/scripts/check-structural.sh"

# A recording `cargo` stub: appends its own argv to $CARGO_LOG and exits 0,
# except when its argv matches $LOOM_TEST_CARGO_FAIL_ON (used to abort the gate
# deliberately once the steps under test have been recorded).
cat > "$STUB_DIR/cargo" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CARGO_LOG"
if [[ -n "\${LOOM_TEST_CARGO_FAIL_ON:-}" && "\$*" == *"\$LOOM_TEST_CARGO_FAIL_ON"* ]]; then
    exit 1
fi
exit 0
EOF
chmod +x "$STUB_DIR/cargo"

# A minimal PATH for the "nextest is not installed" case. Deliberately excludes
# ~/.cargo/bin (where cargo-nextest normally lives) so the gate's `command -v
# cargo-nextest` probe genuinely fails, while still providing git/coreutils.
MIN_PATH="/usr/bin:/bin:/usr/sbin:/sbin"

# Run the FULL tier with the stubbed toolchain. The `-u` strips matter for the
# same reason as in test-build-gate-timeout.sh: a Loom agent running this suite
# is itself a daemon-dispatched child and has these exported already.
run_gate_full_tier() {
    local path_value="$1"; shift
    env \
        -u LOOM_SWEEP_CLAIM_OWNED \
        -u LOOM_DAEMON_BIN \
        -u LOOM_DAEMON_BIN_DIR \
        -u LOOM_PREFER_REPO_BUILD \
        -u LOOM_SWEEP_SELF_REAP \
        -u LOOM_BUILD_SLOT_HELD \
        -u LOOM_BUILD_GATE_NICED \
        -u LOOM_BUILD_GATE_TIER \
        -u LOOM_BUILD_GATE_INSTALLER_SUITE \
        PATH="$path_value" \
        LOOM_FORCE_PORTABLE_TIMEOUT=1 \
        LOOM_BUILD_GATE_NICE=0 \
        LOOM_BUILD_SLOTS=0 \
        LOOM_TEST_CARGO_FAIL_ON="--doc" \
        ${@+"$@"} \
        bash "$BUILD_GATE" 2>&1
}

# ---------------------------------------------------------------------------
# Section 0: stage 0 (the structural phase) is actually wired in (#9494)
# ---------------------------------------------------------------------------
#
# WHY THIS SECTION EXISTS. #9140 added a structural phase to build-gate.sh as two
# lines ABOVE the tier switch, and nothing pinned either fact. `check-structural
# .sh --self-test` covers the derivation in isolation; it says nothing about
# whether build-gate.sh calls it. Deleting both lines -- or relocating them below
# the `LOOM_BUILD_GATE_TIER` switch, which would silently exempt the fast tier --
# left every test and every CI job green. That is the same class of bug #9140 was
# filed for, one level up.
#
# WHY BEHAVIOURAL RATHER THAN A grep/LINE-NUMBER ASSERTION (which #9494 suggested
# as the cheap option). A line-number comparison pins the TEXT's layout, so it
# passes for any refactor that keeps the call textually early while making it
# unreachable -- wrapped in a false conditional, placed after an `exit`, guarded
# by a variable that is never set. Running the gate against a recording STUB
# check-structural.sh pins the property the issue actually cares about (stage 0
# runs, in BOTH tiers, before the expensive phases) at the same cost: the stub
# cargo makes the fast tier a sub-second run, and the stub structural gate means
# no real aggregate ever executes here.
run_gate_fast_tier() {
    local path_value="$1"; shift
    env \
        -u LOOM_SWEEP_CLAIM_OWNED \
        -u LOOM_DAEMON_BIN \
        -u LOOM_DAEMON_BIN_DIR \
        -u LOOM_PREFER_REPO_BUILD \
        -u LOOM_SWEEP_SELF_REAP \
        -u LOOM_BUILD_SLOT_HELD \
        -u LOOM_BUILD_GATE_NICED \
        PATH="$path_value" \
        LOOM_FORCE_PORTABLE_TIMEOUT=1 \
        LOOM_BUILD_GATE_NICE=0 \
        LOOM_BUILD_SLOTS=0 \
        LOOM_BUILD_GATE_TIER=fast \
        ${@+"$@"} \
        bash "$BUILD_GATE" 2>&1
}

# The FAST tier is the load-bearing case: it is the one a relocation below the
# tier switch would silently exempt, because the fast branch ends in `exit 0`.
: > "$CARGO_LOG"
stage0_fast_rc=0
stage0_fast_output="$(cd "$STAGE0_REPO" && run_gate_fast_tier "$STUB_DIR:$MIN_PATH")" \
    || stage0_fast_rc=$?

if grep -Fxq "STRUCTURAL-STUB" "$CARGO_LOG"; then
    pass "stage 0 runs scripts/check-structural.sh in the FAST tier (#9140 wiring, #9494)"
else
    fail "FAST tier did not invoke scripts/check-structural.sh — stage 0 is missing, or it moved below the LOOM_BUILD_GATE_TIER switch. Calls were: $(cat "$CARGO_LOG")"
fi

if [[ "$stage0_fast_rc" -eq 0 ]]; then
    pass "a green stage 0 does not disturb the fast tier's own verdict"
else
    fail "expected the fast tier to exit 0 with a green stage 0, got $stage0_fast_rc: $stage0_fast_output"
fi

# The FULL tier, and the ordering claim build-gate.sh's own comment makes: stage 0
# runs FIRST, so its ~30s of grep/wc returns before the ~700s cargo phases.
: > "$CARGO_LOG"
stage0_full_output="$(cd "$STAGE0_REPO" && run_gate_full_tier "$STUB_DIR:$MIN_PATH")" || true

if [[ "$(head -n 1 "$CARGO_LOG")" == "STRUCTURAL-STUB" ]]; then
    pass "stage 0 runs in the FULL tier too, and runs FIRST (before any cargo step)"
else
    fail "expected STRUCTURAL-STUB as the first recorded call in the full tier, calls were: $(cat "$CARGO_LOG"); gate output: $stage0_full_output"
fi

# And it is a GATE, not a report: a red stage 0 must stop the gate before cargo.
: > "$CARGO_LOG"
stage0_red_rc=0
stage0_red_output="$(cd "$STAGE0_REPO" \
    && run_gate_full_tier "$STUB_DIR:$MIN_PATH" LOOM_TEST_STRUCTURAL_RC=1)" || stage0_red_rc=$?

if [[ "$stage0_red_rc" -ne 0 ]]; then
    pass "a failing stage 0 fails the whole gate (set -e), rather than being reported and ignored"
else
    fail "expected a non-zero gate exit when stage 0 fails, got 0: $stage0_red_output"
fi

if grep -q "^build\|^nextest\|^test " "$CARGO_LOG"; then
    fail "gate ran cargo steps after stage 0 failed, calls were: $(cat "$CARGO_LOG")"
else
    pass "a failing stage 0 aborts before the expensive cargo phases"
fi

# ---------------------------------------------------------------------------
# Section 1: cargo-nextest present -> nextest is preferred (AC1)
# ---------------------------------------------------------------------------

# Presence is detected via `command -v cargo-nextest`, so an executable of that
# name in the stub dir is exactly the condition under test. It is never
# executed: `cargo nextest run …` dispatches through the `cargo` stub above.
cat > "$STUB_DIR/cargo-nextest" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STUB_DIR/cargo-nextest"

: > "$CARGO_LOG"
present_output="$(cd "$SCRATCH_REPO" && run_gate_full_tier "$STUB_DIR:$MIN_PATH")"

if grep -Fxq "nextest run --workspace --lib --bins --profile ci" "$CARGO_LOG"; then
    pass "with cargo-nextest installed, the gate runs 'cargo nextest run --workspace --lib --bins --profile ci'"
else
    fail "expected a nextest invocation, cargo calls were: $(cat "$CARGO_LOG")"
fi

if grep -Fxq "test --workspace --lib --bins" "$CARGO_LOG"; then
    fail "gate ran the shared-process 'cargo test' unit step despite nextest being available: $(cat "$CARGO_LOG")"
else
    pass "the shared-process 'cargo test --workspace --lib --bins' step is not run when nextest is available"
fi

if [[ "$present_output" == *"WARNING"* ]]; then
    fail "no fallback WARNING should be printed when nextest is installed, got: $present_output"
else
    pass "no spurious fallback warning when nextest is installed"
fi

if grep -Fxq "test --workspace --doc" "$CARGO_LOG"; then
    pass "doctests still run as their own step on the nextest branch (AC3, #4385)"
else
    fail "expected a 'cargo test --workspace --doc' step, cargo calls were: $(cat "$CARGO_LOG")"
fi

if [[ "$present_output" == *"bash scripts/test-installer.sh"* ]]; then
    fail "gate should have aborted at the deliberately-failed doctest step, got: $present_output"
else
    pass "a failing Rust step aborts the gate before the later bash suites (set -e)"
fi

# ---------------------------------------------------------------------------
# Section 2: cargo-nextest absent -> loud degradation to cargo test (AC2)
# ---------------------------------------------------------------------------

rm -f "$STUB_DIR/cargo-nextest"

if PATH="$STUB_DIR:$MIN_PATH" command -v cargo-nextest >/dev/null 2>&1; then
    skip "cargo-nextest is reachable even on the minimal PATH ($MIN_PATH) — cannot exercise the fallback branch on this host"
else
    : > "$CARGO_LOG"
    absent_output="$(cd "$SCRATCH_REPO" && run_gate_full_tier "$STUB_DIR:$MIN_PATH")"

    if grep -Fxq "test --workspace --lib --bins" "$CARGO_LOG"; then
        pass "without cargo-nextest, the gate falls back to 'cargo test --workspace --lib --bins'"
    else
        fail "expected a cargo test fallback invocation, cargo calls were: $(cat "$CARGO_LOG")"
    fi

    if grep -q "nextest" "$CARGO_LOG"; then
        fail "gate invoked nextest despite it being absent from PATH: $(cat "$CARGO_LOG")"
    else
        pass "no nextest invocation is attempted when cargo-nextest is absent"
    fi

    if [[ "$absent_output" == *"WARNING: cargo-nextest is NOT installed"* ]]; then
        pass "the degradation is loud (a [build-gate] WARNING block), not silent"
    else
        fail "expected a loud missing-nextest warning, got: $absent_output"
    fi

    if [[ "$absent_output" == *"#4385"* ]]; then
        pass "the warning names #4385 (the shared-process env/spawn race it exposes)"
    else
        fail "expected the warning to name #4385, got: $absent_output"
    fi

    if [[ "$absent_output" == *"cargo install cargo-nextest"* ]]; then
        pass "the warning tells the reader how to fix it (cargo install cargo-nextest)"
    else
        fail "expected the warning to suggest 'cargo install cargo-nextest', got: $absent_output"
    fi

    if grep -Fxq "test --workspace --doc" "$CARGO_LOG"; then
        pass "doctests still run as their own step on the fallback branch (AC3)"
    else
        fail "expected a 'cargo test --workspace --doc' step on the fallback branch, cargo calls were: $(cat "$CARGO_LOG")"
    fi
fi

# ---------------------------------------------------------------------------
# Section 3: the installer suite is droppable by pre-flight path scoping (#10860)
# ---------------------------------------------------------------------------
#
# `loom-daemon preflight` exports LOOM_BUILD_GATE_INSTALLER_SUITE empty when the
# diff touches no installer input (`buildGate.preflightPathScopes`). Exported
# empty, the gate skips scripts/test-installer.sh and still runs the other four
# suites; unset (every other caller), all five run, in order, exactly as before.
SUITES_REPO="$STUB_DIR/suites-repo"
mkdir -p "$SUITES_REPO/scripts" && git -C "$SUITES_REPO" init -q
all_suites="test-installer test-changelog test-daemon-liveness test-install-local-mode test-migrate-consumer"
for s in $all_suites; do
    printf '#!/usr/bin/env bash\nprintf "SUITE %%s\\n" %s >> "%s"\n' "$s" "$CARGO_LOG" > "$SUITES_REPO/scripts/$s.sh"
done

suite_calls() { grep '^SUITE ' "$CARGO_LOG" | sed 's/^SUITE //' | tr '\n' ' ' | sed 's/ $//'; }

: > "$CARGO_LOG"
unset_rc=0
unset_output="$(cd "$SUITES_REPO" && run_gate_full_tier "$STUB_DIR:$MIN_PATH" LOOM_TEST_CARGO_FAIL_ON=)" || unset_rc=$?
if [[ "$unset_rc" -eq 0 && "$(suite_calls)" == "$all_suites" ]]; then
    pass "with LOOM_BUILD_GATE_INSTALLER_SUITE unset, all five bash suites run in order"
else
    fail "expected all five suites ($all_suites), rc=$unset_rc, got: $(suite_calls); gate output: $unset_output"
fi

: > "$CARGO_LOG"
skip_rc=0
skip_output="$(cd "$SUITES_REPO" && run_gate_full_tier "$STUB_DIR:$MIN_PATH" LOOM_TEST_CARGO_FAIL_ON= LOOM_BUILD_GATE_INSTALLER_SUITE=)" || skip_rc=$?
if [[ "$skip_rc" -eq 0 && "$(suite_calls)" == "${all_suites#test-installer }" ]]; then
    pass "with LOOM_BUILD_GATE_INSTALLER_SUITE exported empty, test-installer.sh is skipped and the other four run"
else
    fail "expected only '${all_suites#test-installer }', rc=$skip_rc, got: $(suite_calls); gate output: $skip_output"
fi

# ---------------------------------------------------------------------------

echo
echo "-----------------------------------------------------------"
echo "test-build-gate-runner.sh: $passed passed, $failed failed, $skipped skipped"
if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
exit 0
