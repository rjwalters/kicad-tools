#!/usr/bin/env bash
# test-spawn-generic.sh — Tests for the tier-3 "generic passthrough" runtime
# adapter mechanism (issue #4780, epic #4167).
#
# Style matches test-spawn-codex.sh / test-check-runtime-capabilities.sh —
# plain bash, hand-rolled assertions, hermetic (no live CLI calls).
#
# Covers:
#   1. syntax + --help
#   2. argv assembly + prompt delivery (LOOM_GENERIC_NO_EXEC)
#   3. required env vars (LOOM_GENERIC_RUNTIME_NAME / LOOM_GENERIC_CLI_BIN)
#      missing -> exit 78
#   4. missing binary -> exit 127
#   5. mocked dispatch: exit-code passthrough, generic-transient
#      classification fallthrough (no bespoke pattern table for a tier-3
#      runtime name)
#   6. LOOM_RUNTIME=aider resolves through spawn-worker.sh with NO changes to
#      the dispatcher (the seam already generalizes)
#   7. capability manifest gate: check-runtime-capabilities.sh fails closed
#      for builder/doctor/judge + aider, and admits curator/guide/auditor
#      (which declare no runtimeRequirements)
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-generic.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULTS_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
SPAWN_GENERIC="$SCRIPTS_DIR/spawn-generic.sh"
SPAWN_AIDER="$SCRIPTS_DIR/spawn-aider.sh"
CHECK_SCRIPT="$SCRIPTS_DIR/check-runtime-capabilities.sh"
AIDER_MANIFEST="$DEFAULTS_DIR/runtimes/aider.json"

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

assert_not_contains() {
    local needle="$1" haystack="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

# ============================================================
# Section 1: syntax + help
# ============================================================

echo "Testing spawn-generic.sh / spawn-aider.sh syntax and help..."

TESTS_RUN=$((TESTS_RUN + 1))
if bash -n "$SPAWN_GENERIC" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: spawn-generic.sh passes bash -n"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: spawn-generic.sh fails bash -n"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if bash -n "$SPAWN_AIDER" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: spawn-aider.sh passes bash -n"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: spawn-aider.sh fails bash -n"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$SPAWN_GENERIC" && -x "$SPAWN_AIDER" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: both scripts are executable"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: both scripts must be executable"
fi

set +e
help_out="$(bash "$SPAWN_GENERIC" --help 2>&1)"
help_rc=$?
set -e
assert_eq "0" "$help_rc" "--help exits 0"
assert_contains "Tier-3" "$help_out" "--help renders the header docs"
assert_not_contains "set -euo" "$help_out" "--help stops before the shebang body"

# ============================================================
# Section 2: argv assembly + prompt delivery (LOOM_GENERIC_NO_EXEC)
# ============================================================

echo ""
echo "Testing spawn-generic.sh argv assembly + prompt delivery..."

run_aider_argv() {
    env -u LOOM_MODEL LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 \
        bash "$SPAWN_AIDER" "$@" 2>/dev/null || true
}

out="$(run_aider_argv -p "hello world")"
assert_contains "spawn-generic would-exec: aider" "$out" \
    "spawn-aider.sh pins bin=aider and delegates to the shared template"
assert_contains "--yes-always" "$out" \
    "spawn-aider.sh injects --yes-always for unattended dispatch"
assert_contains "--message hello world" "$out" \
    "the prompt is delivered via the configured LOOM_GENERIC_PROMPT_FLAG"

out="$(run_aider_argv --prompt "alt-form")"
assert_contains "--message alt-form" "$out" "--prompt is accepted as a synonym for -p"

out="$(run_aider_argv -p=short-eq)"
assert_contains "--message short-eq" "$out" "-p=VALUE is accepted"

# No -p configured -> the generic template falls back to a positional prompt
# when LOOM_GENERIC_PROMPT_FLAG is unset.
out="$(env -u LOOM_MODEL LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 \
    LOOM_GENERIC_RUNTIME_NAME=positional-cli LOOM_GENERIC_CLI_BIN=somecli \
    bash "$SPAWN_GENERIC" -p "positional-prompt" 2>/dev/null || true)"
assert_contains "somecli positional-prompt" "$out" \
    "with no LOOM_GENERIC_PROMPT_FLAG, the prompt is delivered as the final positional argv element"

# Loom's runner-neutral conventions are CONSUMED, never forwarded (tier-3 has
# no sandbox/effort mapping to translate them into).
out="$(run_aider_argv -p x --dangerously-skip-permissions --use-wrapper --effort high)"
assert_not_contains "--dangerously-skip-permissions" "$out" \
    "--dangerously-skip-permissions is consumed, never forwarded"
assert_not_contains "--use-wrapper" "$out" "--use-wrapper is consumed, never forwarded"
assert_not_contains "--effort" "$out" "--effort (and its value) is consumed, never forwarded"

# Unrecognized args pass through verbatim.
out="$(run_aider_argv -p x --some-flag --color never)"
assert_contains "--some-flag" "$out" "unrecognized flags pass through verbatim"
assert_contains "--color never" "$out" "flag+value pairs pass through verbatim"

# Model mapping: minimalism by default (no LOOM_GENERIC_MODEL_FLAG configured).
out="$(env LOOM_MODEL=some-model LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 \
    bash "$SPAWN_AIDER" -p x 2>/dev/null || true)"
assert_not_contains "some-model" "$out" \
    "LOOM_MODEL is silently NOT forwarded with no LOOM_GENERIC_MODEL_FLAG configured (contract's 'omit, no error' facet)"

out="$(env LOOM_MODEL=some-model LOOM_GENERIC_MODEL_FLAG=--model LOOM_SWEEP_NICE=0 \
    LOOM_GENERIC_NO_EXEC=1 bash "$SPAWN_AIDER" -p x 2>/dev/null || true)"
assert_contains "--model some-model" "$out" \
    "LOOM_GENERIC_MODEL_FLAG + LOOM_MODEL together DO map to a model flag"

# ============================================================
# Section 3: required env vars -> exit 78 (EX_CONFIG)
# ============================================================

echo ""
echo "Testing spawn-generic.sh required-env-var failures..."

set +e
noname_out="$(env -u LOOM_GENERIC_RUNTIME_NAME LOOM_GENERIC_CLI_BIN=foo \
    LOOM_SWEEP_NICE=0 bash "$SPAWN_GENERIC" -p x 2>&1)"
noname_rc=$?
set -e
assert_eq "78" "$noname_rc" "missing LOOM_GENERIC_RUNTIME_NAME exits 78"
assert_contains "LOOM_GENERIC_RUNTIME_NAME is required" "$noname_out" \
    "the missing-runtime-name message names the offending env var"

set +e
nobin_out="$(env -u LOOM_GENERIC_CLI_BIN LOOM_GENERIC_RUNTIME_NAME=foo \
    LOOM_SWEEP_NICE=0 bash "$SPAWN_GENERIC" -p x 2>&1)"
nobin_rc=$?
set -e
assert_eq "78" "$nobin_rc" "missing LOOM_GENERIC_CLI_BIN exits 78"
assert_contains "LOOM_GENERIC_CLI_BIN is required" "$nobin_out" \
    "the missing-cli-bin message names the offending env var"

# -p / -m with no value are usage errors (EX_CONFIG), matching spawn-codex.sh.
set +e
noval_out="$(env LOOM_GENERIC_RUNTIME_NAME=foo LOOM_GENERIC_CLI_BIN=foo \
    LOOM_SWEEP_NICE=0 bash "$SPAWN_GENERIC" -p 2>&1)"
noval_rc=$?
set -e
assert_eq "78" "$noval_rc" "-p with no value exits 78 (EX_CONFIG)"
assert_contains "requires a value" "$noval_out" "-p with no value says so"

# ============================================================
# Section 4: missing binary -> exit 127 (NOT 78)
# ============================================================

echo ""
echo "Testing spawn-generic.sh missing-binary failure..."

EMPTY_BIN="$TMPROOT/empty-bin"
mkdir -p "$EMPTY_BIN"
set +e
missing_out="$(env LOOM_GENERIC_RUNTIME_NAME=aider LOOM_GENERIC_CLI_BIN=aider \
    LOOM_SWEEP_NICE=0 PATH="$EMPTY_BIN:/usr/bin:/bin" \
    bash "$SPAWN_GENERIC" -p x 2>&1)"
missing_rc=$?
set -e
assert_eq "127" "$missing_rc" \
    "a missing underlying binary exits 127, NOT 78 (78 is reserved for config errors)"
assert_contains "'aider' command not found in PATH" "$missing_out" \
    "the missing-binary message names the binary"

# ============================================================
# Section 5: mocked dispatch — exit-code passthrough + generic-transient
# classification fallthrough (contract point 3, tier-3 minimalism)
# ============================================================

echo ""
echo "Testing spawn-generic.sh mocked dispatch (fake aider on PATH)..."

MOCK_BIN="$TMPROOT/mock-bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/aider" <<'MOCK'
#!/usr/bin/env bash
_stdin="$(cat)"
if [[ -n "$_stdin" ]]; then echo "MOCK-SAW-STDIN:$_stdin" >&2; fi
echo "MOCK-OUTPUT: ${MOCK_MESSAGE:-ok}"
exit "${MOCK_RC:-0}"
MOCK
chmod +x "$MOCK_BIN/aider"

run_mock() {
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    env LOOM_SWEEP_NICE=0 PATH="$MOCK_BIN:$PATH" \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_AIDER" "$@"
}

set +e
mock_stdout="$(run_mock -- -p "hi" 2>/dev/null)"
mock_rc=$?
set -e
assert_eq "0" "$mock_rc" "a clean mock exit stays 0"
assert_contains "MOCK-OUTPUT: ok" "$mock_stdout" "the mock's output reaches stdout"

set +e
mock_stderr="$({ run_mock -- -p "hi" >/dev/null; } 2>&1)"
set -e
assert_contains \
    "# LOOM_TERMINAL_RESULT v=1 provider=aider account=unknown category=SUCCESS exit_code=0" \
    "$mock_stderr" \
    "a clean exit classifies as SUCCESS (exit-code-first, #3233) even with no pattern table"
assert_not_contains "MOCK-SAW-STDIN" "$mock_stderr" \
    "the child's stdin is /dev/null (no hang on a dispatched worker's unread pipe)"

# Piped stdin from the caller must never reach the child either.
mock_stderr="$(printf 'unwanted stdin payload\n' | { run_mock -- -p "hi" >/dev/null; } 2>&1)"
assert_not_contains "MOCK-SAW-STDIN" "$mock_stderr" \
    "piped caller stdin is NOT forwarded to the child"

# Exit-code passthrough for a non-zero exit with UNRECOGNIZED output -> the
# generic-transient fallthrough (RECOVERABLE), because a tier-3 runtime name
# is deliberately unregistered in classify-error.sh's provider tables.
set +e
run_mock MOCK_RC=5 MOCK_MESSAGE="some transient network error" \
    -- -p "hi" >/dev/null 2>&1
mock_rc5=$?
set -e
assert_eq "5" "$mock_rc5" "the underlying CLI's exit code is passed through"

mock_stderr="$({ run_mock MOCK_RC=5 MOCK_MESSAGE="some transient network error" \
    -- -p "hi" >/dev/null; } 2>&1 || true)"
assert_contains \
    "# LOOM_TERMINAL_RESULT v=1 provider=aider account=unknown category=RECOVERABLE exit_code=5" \
    "$mock_stderr" \
    "an unmatched non-zero exit falls through to RECOVERABLE (generic-transient fallthrough, no bespoke pattern table)"

# ============================================================
# Section 6: LOOM_RUNTIME=aider resolves through spawn-worker.sh with NO
# changes to the dispatcher itself (issue #4780's central claim)
# ============================================================

echo ""
echo "Testing LOOM_RUNTIME=aider dispatch through spawn-worker.sh..."

STAGE="$TMPROOT/stage"
mkdir -p "$STAGE/lib"
cp "$SCRIPTS_DIR/spawn-worker.sh" "$STAGE/spawn-worker.sh"
cp "$SCRIPTS_DIR/lib/config-resolver.sh" "$STAGE/lib/config-resolver.sh"
cp "$SPAWN_GENERIC" "$STAGE/spawn-generic.sh"
cp "$SPAWN_AIDER" "$STAGE/spawn-aider.sh"
cp "$SCRIPTS_DIR/lib/classify-error.sh" "$STAGE/lib/classify-error.sh"
chmod +x "$STAGE"/spawn-*.sh

out="$(env -u LOOM_RUNTIME LOOM_WORKSPACE="$TMPROOT/ws" \
    LOOM_CONFIG_DEFAULTS_FILE="" LOOM_RUNTIME=aider LOOM_SWEEP_NICE=0 \
    LOOM_GENERIC_NO_EXEC=1 bash "$STAGE/spawn-worker.sh" -p "routed" 2>&1 || true)"
assert_contains "runtime=aider (from env (LOOM_RUNTIME))" "$out" \
    "spawn-worker.sh resolves LOOM_RUNTIME=aider with no dispatcher changes"
assert_contains "spawn-generic would-exec: aider" "$out" \
    "spawn-worker.sh dispatches into spawn-aider.sh -> spawn-generic.sh"
assert_contains "routed" "$out" "the prompt survives the dispatcher hop"

# ============================================================
# Section 7: capability manifest gate (contract point 7)
# ============================================================

echo ""
echo "Testing the tier-3 capability manifest gate (check-runtime-capabilities.sh)..."

TESTS_RUN=$((TESTS_RUN + 1))
if jq -e '.runtime == "aider" and (.capabilities | type == "object")' \
    "$AIDER_MANIFEST" >/dev/null 2>&1; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: aider.json is valid JSON with the expected top-level shape"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: aider.json is valid JSON with the expected top-level shape"
fi

assert_eq "no" "$(jq -r '.capabilities.worktreeIsolation' "$AIDER_MANIFEST")" \
    "aider.json declares worktreeIsolation EXPLICITLY as 'no' (not 'partial')"

bad="$(jq -r '.capabilities | to_entries[]
              | select(.value != "yes" and .value != "no" and .value != "partial")
              | .key' "$AIDER_MANIFEST")"
assert_eq "" "$bad" "every aider.json capability value is one of yes|no|partial"

set +e
bash "$CHECK_SCRIPT" --role builder --runtime aider --dir "$DEFAULTS_DIR" >/dev/null 2>&1
builder_rc=$?
bash "$CHECK_SCRIPT" --role doctor --runtime aider --dir "$DEFAULTS_DIR" >/dev/null 2>&1
doctor_rc=$?
bash "$CHECK_SCRIPT" --role judge --runtime aider --dir "$DEFAULTS_DIR" >/dev/null 2>&1
judge_rc=$?
bash "$CHECK_SCRIPT" --role curator --runtime aider --dir "$DEFAULTS_DIR" >/dev/null 2>&1
curator_rc=$?
bash "$CHECK_SCRIPT" --role guide --runtime aider --dir "$DEFAULTS_DIR" >/dev/null 2>&1
guide_rc=$?
bash "$CHECK_SCRIPT" --role auditor --runtime aider --dir "$DEFAULTS_DIR" >/dev/null 2>&1
auditor_rc=$?
set -e

assert_eq "78" "$builder_rc" \
    "builder + aider is refused (EX_CONFIG) -- worktreeIsolation='no' fails closed, same as Codex's 'partial'"
assert_eq "78" "$doctor_rc" \
    "doctor + aider is refused (EX_CONFIG) -- doctor also requires worktreeIsolation"
assert_eq "78" "$judge_rc" \
    "judge + aider is refused (EX_CONFIG) -- judge requires mcp, and aider declares mcp='no'"
assert_eq "0" "$curator_rc" \
    "curator + aider is admitted -- curator.json declares no runtimeRequirements"
assert_eq "0" "$guide_rc" \
    "guide + aider is admitted -- guide.json declares no runtimeRequirements"
assert_eq "0" "$auditor_rc" \
    "auditor + aider is admitted -- auditor.json declares no runtimeRequirements"

# --json decision surface (#4494) reports the SAME set of unmet capabilities
# for aider's 'no' that a 'partial' manifest would produce.
if command -v jq >/dev/null 2>&1; then
    set +e
    json_out="$(bash "$CHECK_SCRIPT" --role builder --runtime aider --dir "$DEFAULTS_DIR" --json 2>/dev/null)"
    set -e
    assert_eq "reject" "$(jq -r '.decision' <<<"$json_out")" \
        "--json reports decision=reject for builder+aider"
    assert_contains "worktreeIsolation" "$(jq -rc '.unmet' <<<"$json_out")" \
        "--json names worktreeIsolation as unmet for builder+aider"
fi

# ============================================================
# Summary
# ============================================================

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
