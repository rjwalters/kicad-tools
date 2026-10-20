#!/usr/bin/env bash
# test-check-runtime-capabilities.sh — Tests for
# defaults/scripts/check-runtime-capabilities.sh (#4170, epic #4167).
#
# Covers:
#   1. Match: a role's runtimeRequirements are all declared "yes" by the
#      runtime manifest -> exit 0.
#   2. Mismatch: a required capability is declared "no" or "partial" (or is
#      simply absent) -> exit 78, message names EACH missing/partial
#      capability and its declared value ("partial" fails closed, same as
#      "no").
#   3. Role without runtimeRequirements -> exit 0 with a no-requirements note
#      (any runtime is trivially compatible).
#   4. Unknown/missing role file and unknown/missing runtime file -> non-zero
#      exit with a message DISTINCT from the mismatch message, so callers can
#      tell "misconfigured" apart from "mismatched".
#
# Style matches test-config-resolver.sh / test-disk-headroom.sh: throwaway
# `mktemp -d` fixture roots (via --dir), hand-rolled assertions, no bats.
#
# Usage:
#   ./defaults/scripts/tests/test-check-runtime-capabilities.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CHECK_SCRIPT="$SCRIPTS_DIR/check-runtime-capabilities.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

assert_contains() {
    local needle="$1" haystack="$2" msg="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$msg"
    else
        fail "$msg (expected substring '$needle' in: $haystack)"
    fi
}

if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found on PATH -- skipping test-check-runtime-capabilities.sh"
    exit 0
fi

if [[ ! -x "$CHECK_SCRIPT" ]]; then
    fail "check-runtime-capabilities.sh is missing or not executable at $CHECK_SCRIPT"
    echo ""
    echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
    exit 1
fi

new_fixture_dir() {
    local dir
    dir=$(mktemp -d)
    mkdir -p "$dir/roles" "$dir/runtimes"
    printf '%s\n' "$dir"
}

# --- Test 1: match -> exit 0 ---
echo "Test 1: role requirements fully satisfied by runtime -> exit 0"
dir=$(new_fixture_dir)
cat > "$dir/roles/builder.json" <<'JSON'
{"name": "Development Worker", "runtimeRequirements": ["worktreeIsolation", "mcp"]}
JSON
cat > "$dir/runtimes/claude.json" <<'JSON'
{"runtime": "claude", "capabilities": {"mcp": "yes", "subagents": "yes", "hooks": "yes", "skills": "yes", "worktreeIsolation": "yes"}}
JSON

set +e
output=$("$CHECK_SCRIPT" --role builder --runtime claude --dir "$dir" 2>&1)
code=$?
set -e
assert_eq "$code" "0" "match case exits 0"
rm -rf "$dir"

# --- Test 2: mismatch -> exit 78, each missing/partial capability named ---
echo "Test 2: mismatch -- missing and partial capabilities both named, exit 78"
dir=$(new_fixture_dir)
cat > "$dir/roles/judge.json" <<'JSON'
{"name": "Code Review Specialist", "runtimeRequirements": ["mcp", "worktreeIsolation", "subagents"]}
JSON
cat > "$dir/runtimes/codex.json" <<'JSON'
{"runtime": "codex", "capabilities": {"mcp": "partial", "subagents": "no", "hooks": "no", "skills": "no", "worktreeIsolation": "yes"}}
JSON

set +e
output=$("$CHECK_SCRIPT" --role judge --runtime codex --dir "$dir" 2>&1)
code=$?
set -e
assert_eq "$code" "78" "mismatch case exits 78 (EX_CONFIG)"
assert_contains "mcp" "$output" "mismatch output names the 'mcp' capability (declared 'partial', fails closed)"
assert_contains "partial" "$output" "mismatch output shows the declared 'partial' value for mcp"
assert_contains "subagents" "$output" "mismatch output names the 'subagents' capability (declared 'no')"
if [[ "$output" == *"worktreeIsolation: runtime"* ]]; then
    fail "satisfied requirement 'worktreeIsolation' should not appear as a mismatch line"
else
    pass "satisfied requirement 'worktreeIsolation' correctly excluded from mismatch list"
fi
rm -rf "$dir"

# --- Test 3: role without runtimeRequirements -> exit 0 with a note ---
echo "Test 3: role without runtimeRequirements -> exit 0, any runtime compatible"
dir=$(new_fixture_dir)
cat > "$dir/roles/driver.json" <<'JSON'
{"name": "Direct Command Execution"}
JSON
cat > "$dir/runtimes/claude.json" <<'JSON'
{"runtime": "claude", "capabilities": {}}
JSON

set +e
output=$("$CHECK_SCRIPT" --role driver --runtime claude --dir "$dir" 2>&1)
code=$?
set -e
assert_eq "$code" "0" "role without runtimeRequirements exits 0 even against an empty capability set"
assert_contains "no runtimeRequirements" "$output" "output notes the role declares no runtimeRequirements"
rm -rf "$dir"

# --- Test 4a: unknown role file -> non-zero, distinct message from mismatch ---
echo "Test 4a: unknown role -> non-zero exit with a message distinct from the mismatch message"
dir=$(new_fixture_dir)
cat > "$dir/runtimes/claude.json" <<'JSON'
{"runtime": "claude", "capabilities": {"mcp": "yes"}}
JSON

set +e
output=$("$CHECK_SCRIPT" --role does-not-exist --runtime claude --dir "$dir" 2>&1)
code=$?
set -e
if [[ "$code" -ne 0 ]]; then pass "unknown role exits non-zero"; else fail "unknown role should not exit 0 (got $code)"; fi
if [[ "$code" -ne 78 ]]; then pass "unknown role exit code ($code) is distinct from the mismatch exit code 78"; else fail "unknown role must not be conflated with the mismatch exit code 78"; fi
assert_contains "Unknown role" "$output" "unknown role message names the failure distinctly (not the mismatch wording)"
rm -rf "$dir"

# --- Test 4b: unknown runtime file -> non-zero, distinct message from mismatch ---
echo "Test 4b: unknown runtime -> non-zero exit with a message distinct from the mismatch message"
dir=$(new_fixture_dir)
cat > "$dir/roles/builder.json" <<'JSON'
{"runtimeRequirements": ["mcp"]}
JSON

set +e
output=$("$CHECK_SCRIPT" --role builder --runtime does-not-exist --dir "$dir" 2>&1)
code=$?
set -e
if [[ "$code" -ne 0 ]]; then pass "unknown runtime exits non-zero"; else fail "unknown runtime should not exit 0 (got $code)"; fi
if [[ "$code" -ne 78 ]]; then pass "unknown runtime exit code ($code) is distinct from the mismatch exit code 78"; else fail "unknown runtime must not be conflated with the mismatch exit code 78"; fi
assert_contains "Unknown runtime" "$output" "unknown runtime message names the failure distinctly (not the mismatch wording)"
rm -rf "$dir"

# --- Test 5: explicit --role-file / --runtime-file overrides ---
echo "Test 5: --role-file and --runtime-file overrides bypass name-based resolution"
dir=$(new_fixture_dir)
role_file="$dir/some-role.json"
runtime_file="$dir/some-runtime.json"
cat > "$role_file" <<'JSON'
{"runtimeRequirements": ["hooks"]}
JSON
cat > "$runtime_file" <<'JSON'
{"runtime": "claude", "capabilities": {"hooks": "yes"}}
JSON

set +e
output=$("$CHECK_SCRIPT" --role-file "$role_file" --runtime-file "$runtime_file" --role ignored --runtime ignored 2>&1)
code=$?
set -e
assert_eq "$code" "0" "explicit file overrides resolve and match correctly"
rm -rf "$dir"

# --- Test 6: verify against the real repo fixtures (builder + judge x claude) ---
echo "Test 6: real defaults/ fixtures -- builder and judge are both compatible with claude"
DEFAULTS_DIR="$(cd "$SCRIPTS_DIR/.." && pwd)"
if [[ -f "$DEFAULTS_DIR/runtimes/claude.json" ]]; then
    set +e
    "$CHECK_SCRIPT" --role builder --runtime claude --dir "$DEFAULTS_DIR" >/dev/null 2>&1
    b_code=$?
    "$CHECK_SCRIPT" --role judge --runtime claude --dir "$DEFAULTS_DIR" >/dev/null 2>&1
    j_code=$?
    set -e
    assert_eq "$b_code" "0" "real builder.json requirements are met by real claude.json"
    assert_eq "$j_code" "0" "real judge.json requirements are met by real claude.json"
else
    fail "defaults/runtimes/claude.json not found -- cannot verify real fixtures"
fi

# --- Test 7: --json machine-readable decision output (#4494) ---
# The daemon's native admission path compares its unmet-capability SET against
# this checker's, so the JSON shape (and specifically the `unmet` array) is a
# contract, not a convenience. Exit codes and the human-readable stderr lines
# must stay unchanged when --json is passed.
echo "Test 7: --json emits a stable decision object without changing exit codes (#4494)"
dir=$(new_fixture_dir)
cat > "$dir/roles/jsonrole.json" <<'JSON'
{"name": "JSON Role", "runtimeRequirements": ["mcp", "worktreeIsolation"]}
JSON
cat > "$dir/roles/freerole.json" <<'JSON'
{"name": "Free Role"}
JSON
cat > "$dir/runtimes/jsonrt.json" <<'JSON'
{"runtime": "jsonrt", "capabilities": {"mcp": "yes", "worktreeIsolation": "partial"}}
JSON

set +e
json_out="$("$CHECK_SCRIPT" --role jsonrole --runtime jsonrt --dir "$dir" --json 2>/dev/null)"
json_code=$?
set -e
assert_eq "$json_code" "78" "--json preserves the EX_CONFIG(78) mismatch exit code"
assert_eq "$(jq -r '.decision' <<<"$json_out")" "reject" "--json reports decision=reject on a mismatch"
assert_eq "$(jq -rc '.unmet' <<<"$json_out")" '["worktreeIsolation"]' \
    "--json names the exact unmet-capability set (not just pass/fail)"
assert_eq "$(jq -r '.role + "/" + .runtime' <<<"$json_out")" "jsonrole/jsonrt" \
    "--json echoes the role and runtime under test"

set +e
json_out="$("$CHECK_SCRIPT" --role freerole --runtime jsonrt --dir "$dir" --json 2>/dev/null)"
json_code=$?
set -e
assert_eq "$json_code" "0" "--json preserves the exit-0 admit path"
assert_eq "$(jq -r '.decision' <<<"$json_out")" "admit" "--json reports decision=admit with no requirements"
assert_eq "$(jq -rc '.unmet' <<<"$json_out")" '[]' "--json reports an empty unmet set on admit"

set +e
json_out="$("$CHECK_SCRIPT" --role nosuchrole --runtime jsonrt --dir "$dir" --json 2>/dev/null)"
json_code=$?
set -e
assert_eq "$json_code" "1" "--json preserves the exit-1 configuration-error path"
assert_eq "$(jq -r '.decision' <<<"$json_out")" "error" \
    "--json distinguishes a configuration error from a capability mismatch"

# Without --json, stdout stays empty (the default contract is unchanged).
set +e
plain_out="$("$CHECK_SCRIPT" --role jsonrole --runtime jsonrt --dir "$dir" 2>/dev/null)"
set -e
assert_eq "$plain_out" "" "omitting --json leaves stdout byte-for-byte unchanged (empty)"
rm -rf "$dir"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
