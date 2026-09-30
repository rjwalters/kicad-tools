#!/usr/bin/env bash
# test-spawn-generic-launch.sh — Tests for spawn-generic-launch.sh's
# manifest-driven launch-shape resolution (issue #8671): a tier-3 runtime's
# `defaults/runtimes/<name>.json` "launch" object is the primary way to
# onboard it now, instead of hand-writing a per-CLI spawn-<runtime>.sh.
#
# Style matches test-spawn-generic.sh — plain bash, hand-rolled assertions,
# hermetic (no live CLI calls, no real forge access).
#
# Covers:
#   1. syntax + executable
#   2. manifest-driven defaults reach spawn-generic.sh's argv (cliBin,
#      promptFlag, extraArgs)
#   3. env always wins over the manifest (env > config > default)
#   4. effortFlag/effortValuePrefix and modelEnv, which spawn-generic.sh
#      itself has no reader for — this script performs their mapping
#   5. a launch value containing `}` survives the eval intact
#   6. an unknown `launch` key fails closed (exit 78), never silently ignored
#   7. no manifest at all (and no bundled fallback) degrades to the legacy
#      pure-env-var path rather than failing, when the caller pinned
#      LOOM_GENERIC_CLI_BIN itself
#   8. ...and refuses actionably (exit 78, naming the declared daemon version
#      floor) when it did not
#   9. the manifest is resolved against the SCRIPT-derived repo root, not the
#      invoking CWD (#8700) — a custom `.loom/runtimes/<name>.json` still
#      reaches argv when the script is invoked from outside the repository
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-generic-launch.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_LAUNCH="$SCRIPTS_DIR/spawn-generic-launch.sh"

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

# The port under test is `loom-daemon runtime-launch-env` (#8671); this
# entire suite is testing that binary through the shell stub, same rationale
# as every other suite on this harness (see lib/require-daemon-bin.sh).
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin --self-only "$SCRIPTS_DIR" runtime-launch-env

echo "Testing spawn-generic-launch.sh syntax and permissions..."
TESTS_RUN=$((TESTS_RUN + 1))
if bash -n "$SPAWN_LAUNCH" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: spawn-generic-launch.sh passes bash -n"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: spawn-generic-launch.sh fails bash -n"
fi
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -x "$SPAWN_LAUNCH" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: spawn-generic-launch.sh is executable"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: spawn-generic-launch.sh must be executable"
fi

# A fake repo holding the manifests under test, with the scripts installed
# into it at `.loom/scripts/` — the layout of a real consumer install.
#
# The copy is what makes this fixture honest since #8700: the subject resolves
# its manifest root from its OWN location (`git -C "$SCRIPT_DIR" rev-parse
# --show-toplevel`) and passes it to `runtime-launch-env --repo-root`, so a
# suite that ran the in-repo copy of the script would be asking the daemon to
# resolve `widget.json` out of the loom repository itself, never out of this
# fixture. Running the installed copy means the script-derived root and the
# manifests are the same tree, which is exactly the invariant #8700 restored.
#
# `git init` rather than a bare `mkdir .git`: `--show-toplevel` needs a real
# repository, and the cwd-independence test below depends on that resolution
# succeeding from outside the tree.
FAKE_ROOT="$TMPROOT/fakerepo"
mkdir -p "$FAKE_ROOT/.loom/runtimes" "$FAKE_ROOT/.loom/scripts"
git init -q "$FAKE_ROOT" >/dev/null 2>&1 || {
    echo "FATAL: could not git init the test fixture at $FAKE_ROOT" >&2
    exit 1
}
cp "$SCRIPTS_DIR/spawn-generic-launch.sh" "$SCRIPTS_DIR/spawn-generic.sh" \
    "$FAKE_ROOT/.loom/scripts/"
cp -R "$SCRIPTS_DIR/lib" "$FAKE_ROOT/.loom/scripts/lib"
chmod +x "$FAKE_ROOT/.loom/scripts/spawn-generic-launch.sh" \
    "$FAKE_ROOT/.loom/scripts/spawn-generic.sh"
# Everything below drives the INSTALLED copy; the syntax/permission checks
# above deliberately kept asserting against the tracked source file.
SPAWN_LAUNCH="$FAKE_ROOT/.loom/scripts/spawn-generic-launch.sh"

write_manifest() {
    cat >"$FAKE_ROOT/.loom/runtimes/$1.json"
}

echo ""
echo "Testing manifest-driven defaults reach spawn-generic.sh's argv..."
write_manifest widget <<'JSON'
{
  "runtime": "widget",
  "capabilities": {},
  "launch": {
    "cliBin": "widget-cli",
    "promptFlag": "--message",
    "extraArgs": ["--yes-always"]
  }
}
JSON

out="$(cd "$FAKE_ROOT" && env -u LOOM_GENERIC_CLI_BIN -u LOOM_GENERIC_PROMPT_FLAG \
    LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 bash "$SPAWN_LAUNCH" widget -p "hi" 2>/dev/null)"
assert_contains "spawn-generic would-exec: widget-cli" "$out" \
    "the manifest's cliBin reaches spawn-generic.sh"
assert_contains "--yes-always" "$out" "the manifest's extraArgs are prepended"
assert_contains "--message hi" "$out" "the manifest's promptFlag delivers the prompt"

echo ""
echo "Testing env wins over the manifest (env > config > default)..."
out="$(cd "$FAKE_ROOT" && env LOOM_GENERIC_CLI_BIN=overridden-cli LOOM_SWEEP_NICE=0 \
    LOOM_GENERIC_NO_EXEC=1 bash "$SPAWN_LAUNCH" widget -p "hi" 2>/dev/null)"
assert_contains "spawn-generic would-exec: overridden-cli" "$out" \
    "an already-set LOOM_GENERIC_CLI_BIN wins over the manifest's cliBin"
assert_not_contains "widget-cli" "$out" "the manifest's cliBin is NOT applied once the env var is set"

echo ""
echo "Testing effortFlag/effortValuePrefix and modelEnv mapping..."
write_manifest widget-effort <<'JSON'
{
  "runtime": "widget-effort",
  "capabilities": {},
  "launch": {
    "cliBin": "widget-cli",
    "promptFlag": "--message",
    "modelEnv": "WIDGET_MODEL",
    "effortFlag": "-c",
    "effortValuePrefix": "model_reasoning_effort="
  }
}
JSON
MOCK_BIN="$TMPROOT/mock-bin"
mkdir -p "$MOCK_BIN"
cat >"$MOCK_BIN/widget-cli" <<'MOCK'
#!/usr/bin/env bash
echo "ARGS: $*"
echo "WIDGET_MODEL=${WIDGET_MODEL:-unset}"
MOCK
chmod +x "$MOCK_BIN/widget-cli"

out="$(cd "$FAKE_ROOT" && env -u LOOM_GENERIC_CLI_BIN -u LOOM_GENERIC_MODEL_ENV \
    -u LOOM_GENERIC_EFFORT_FLAG -u LOOM_GENERIC_EFFORT_VALUE_PREFIX \
    PATH="$MOCK_BIN:$PATH" LOOM_SWEEP_NICE=0 LOOM_MODEL="sonnet" LOOM_EFFORT="high" \
    bash "$SPAWN_LAUNCH" widget-effort -p "hi" 2>/dev/null)"
assert_contains "-c model_reasoning_effort=high" "$out" \
    "LOOM_EFFORT maps through effortFlag/effortValuePrefix into an extra argv pair"
assert_contains "WIDGET_MODEL=sonnet" "$out" \
    "LOOM_MODEL is exported under the manifest's modelEnv name"

echo ""
echo "Testing a launch value containing '}' survives the eval intact..."
# Regression guard for the rejected `: "${VAR:=<value>}"` emission form, whose
# `}` would have closed the parameter expansion early and mangled the value.
write_manifest widget-brace <<'JSON'
{
  "runtime": "widget-brace",
  "capabilities": {},
  "launch": { "cliBin": "widget-cli", "promptFlag": "--odd}flag" }
}
JSON
out="$(cd "$FAKE_ROOT" && env -u LOOM_GENERIC_CLI_BIN -u LOOM_GENERIC_PROMPT_FLAG \
    LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 bash "$SPAWN_LAUNCH" widget-brace -p "hi" 2>/dev/null)"
assert_contains "--odd}flag hi" "$out" \
    "a '}' inside a launch value is passed through verbatim, not truncated by the eval"

echo ""
echo "Testing an unknown launch key fails closed (exit 78)..."
write_manifest widget-bad <<'JSON'
{
  "runtime": "widget-bad",
  "capabilities": {},
  "launch": { "cliBin": "widget-cli", "bogusKey": "oops" }
}
JSON
set +e
bad_out="$(cd "$FAKE_ROOT" && env -u LOOM_GENERIC_CLI_BIN LOOM_SWEEP_NICE=0 \
    LOOM_GENERIC_NO_EXEC=1 bash "$SPAWN_LAUNCH" widget-bad -p "hi" 2>&1)"
bad_rc=$?
set -e
assert_eq "78" "$bad_rc" "an unrecognized launch key exits 78 (EX_CONFIG), never silently ignored"
assert_contains "bogusKey" "$bad_out" "the refusal names the offending key"

echo ""
echo "Testing no manifest at all degrades to the legacy pure-env-var path..."
set +e
legacy_out="$(cd "$FAKE_ROOT" && env LOOM_GENERIC_CLI_BIN=legacy-cli \
    LOOM_GENERIC_PROMPT_FLAG=--say LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 \
    bash "$SPAWN_LAUNCH" totally-unknown-runtime -p "hi" 2>/dev/null)"
legacy_rc=$?
set -e
assert_eq "0" "$legacy_rc" "no manifest for an unregistered runtime is a soft, non-fatal degrade"
assert_contains "spawn-generic would-exec: legacy-cli" "$legacy_out" \
    "the caller's own LOOM_GENERIC_* env vars still drive dispatch with no manifest at all"

echo ""
echo "Testing an unresolvable launch shape with NO env fallback refuses actionably..."
# The same unresolvable state as above, minus the LOOM_GENERIC_CLI_BIN that
# made it survivable. spawn-generic.sh would exit 78 here on its own, with a
# message naming only the missing env var — this script gets there first and
# names the actual cause (the daemon version floor) instead.
set +e
floor_out="$(cd "$FAKE_ROOT" && env -u LOOM_GENERIC_CLI_BIN -u LOOM_GENERIC_PROMPT_FLAG \
    LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 \
    bash "$SPAWN_LAUNCH" totally-unknown-runtime -p "hi" 2>&1)"
floor_rc=$?
set -e
assert_eq "78" "$floor_rc" "an unresolvable launch shape with nothing to fall back on exits 78 (EX_CONFIG)"
assert_contains "could not resolve a launch shape" "$floor_out" \
    "the refusal says the launch shape is what could not be resolved"
DECLARED_FLOOR="$(sed -n 's|^# requires-daemon: runtime-launch-env >= \([0-9][0-9.]*\).*|\1|p;/^set -euo/q' "$SPAWN_LAUNCH")"
assert_contains "loom-daemon >= $DECLARED_FLOOR" "$floor_out" \
    "the refusal quotes the version floor declared in the script's own requires-daemon marker"
assert_contains "LOOM_GENERIC_CLI_BIN" "$floor_out" \
    "the refusal names the env-var bypass"

echo ""
echo "Testing the manifest resolves from the script's repo root, not the cwd (#8700)..."
# #8700 regression. The subject used to call `runtime-launch-env` with no
# `--repo-root`, so the subcommand fell back to resolving `<name>.json` from
# its own CWD while the daemon's runtime ADMISSION for the same dispatch
# resolved it from the scripts-derived workspace root. A custom
# `.loom/runtimes/<name>.json` therefore lost silently to the compiled-in
# bundled manifest whenever the daemon's cwd was outside the repo — and a
# custom runtime with no bundled fallback got the version-floor refusal,
# blaming a stale binary for what was really a cwd mismatch.
#
# OUTSIDE is deliberately not under FAKE_ROOT and contains no `.git`, so the
# cwd-based fallback resolves nothing: before the fix this case exits 78 with
# "could not resolve a launch shape"; after it, the manifest still wins.
OUTSIDE="$TMPROOT/outside"
mkdir -p "$OUTSIDE"
write_manifest widget-cwd <<'JSON'
{
  "runtime": "widget-cwd",
  "capabilities": {},
  "launch": {
    "cliBin": "cwd-independent-cli",
    "promptFlag": "--message"
  }
}
JSON
set +e
cwd_out="$(cd "$OUTSIDE" && env -u LOOM_GENERIC_CLI_BIN -u LOOM_GENERIC_PROMPT_FLAG \
    LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 bash "$SPAWN_LAUNCH" widget-cwd -p "hi" 2>&1)"
cwd_rc=$?
set -e
assert_eq "0" "$cwd_rc" "resolution from outside the repository is not a refusal"
assert_contains "spawn-generic would-exec: cwd-independent-cli" "$cwd_out" \
    "a manifest-declared cliBin reaches argv when the cwd is outside the repository"
assert_contains "--message hi" "$cwd_out" \
    "the same manifest's promptFlag also survives an out-of-repo cwd"

echo ""
echo "Testing a bundled compiled-in manifest is unaffected by the change..."
# `aider` is the one shipped tier-3 runtime whose manifest the daemon carries
# compiled in, so its resolution never depended on the cwd. Passing
# --repo-root must not disturb that fallback: FAKE_ROOT has no aider.json of
# its own, so this exercises `bundled_runtime_manifest` explicitly.
set +e
aider_out="$(cd "$OUTSIDE" && env -u LOOM_GENERIC_CLI_BIN -u LOOM_GENERIC_PROMPT_FLAG \
    -u LOOM_GENERIC_EXTRA_ARGS LOOM_SWEEP_NICE=0 LOOM_GENERIC_NO_EXEC=1 \
    bash "$SPAWN_LAUNCH" aider -p "hi" 2>&1)"
aider_rc=$?
set -e
assert_eq "0" "$aider_rc" "the bundled aider manifest still resolves"
assert_contains "spawn-generic would-exec: aider" "$aider_out" \
    "aider's compiled-in launch shape is unchanged by the explicit --repo-root"

echo ""
echo "==================================="
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "All tests passed."
