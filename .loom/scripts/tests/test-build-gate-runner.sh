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

CARGO_LOG="$STUB_DIR/cargo-calls.log"

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
        PATH="$path_value" \
        LOOM_FORCE_PORTABLE_TIMEOUT=1 \
        LOOM_BUILD_GATE_NICE=0 \
        LOOM_BUILD_SLOTS=0 \
        LOOM_TEST_CARGO_FAIL_ON="--doc" \
        ${@+"$@"} \
        bash "$BUILD_GATE" 2>&1
}

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
present_output="$(cd "$REPO_ROOT" && run_gate_full_tier "$STUB_DIR:$MIN_PATH")"

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
    absent_output="$(cd "$REPO_ROOT" && run_gate_full_tier "$STUB_DIR:$MIN_PATH")"

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

echo
echo "-----------------------------------------------------------"
echo "test-build-gate-runner.sh: $passed passed, $failed failed, $skipped skipped"
if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
exit 0
