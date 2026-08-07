#!/usr/bin/env bash
# test-spawn-codex.sh — Tests for the Codex runtime adapter (issue #4468,
# epic #4167 Phase 2).
#
# Style matches test-spawn-worker.sh / test-spawn-claude.sh — plain bash,
# hand-rolled assertions. Bats is NOT used in this repository.
#
# NO LIVE OpenAI CALLS. Two mechanisms keep this hermetic:
#   1. `LOOM_CODEX_NO_EXEC=1` makes spawn-codex.sh print the argv it WOULD run
#      and exit 0 — used for every argv-assembly / precedence assertion, and it
#      works on a host with no `codex` installed at all.
#   2. A fake `codex` shim on a temp PATH that reproduces codex-cli 0.146.0's
#      observed stream split (final message on stdout; banner, `session id:`
#      and `tokens used` on stderr) — used for the dispatch/reporting tests.
#
# Coverage:
#   1. argv assembly + prompt delivery
#   2. sandbox mapping + precedence (SAFETY-CRITICAL)
#   3. model + effort mapping
#   4. git-repo trust check
#   5. args passthrough + usage errors
#   6. CODEX_HOME profile selection (incl. exit 78 + credential hygiene)
#   7. missing binary -> 127 (NOT 78)
#   8. mocked dispatch: stdout/stderr split, stdin, session/transcript/tokens,
#      exit-code passthrough
#   9. LOOM_RUNTIME=codex resolution through spawn-worker.sh
#  10. classify-error.sh `codex` provider vectors (+ `claude` unchanged)
#  11. managed `pre_tool_use` hook readiness/trust preflight (#4495): mutable
#      roles exit 78 before the CLI starts on any not-ready state; read-only
#      roles keep the conservative fallback with an explicit warning; the
#      capability manifest stays evidence-gated at `partial`
#
# Usage:
#   ./.loom/scripts/tests/test-spawn-codex.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SPAWN_CODEX="$SCRIPTS_DIR/spawn-codex.sh"
CLASSIFY_LIB="$SCRIPTS_DIR/lib/classify-error.sh"

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
# Section 0: syntax + help
# ============================================================

echo "Testing spawn-codex.sh syntax and help..."

TESTS_RUN=$((TESTS_RUN + 1))
if bash -n "$SPAWN_CODEX" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: spawn-codex.sh passes bash -n"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: spawn-codex.sh fails bash -n"
fi

set +e
help_out="$(bash "$SPAWN_CODEX" --help 2>&1)"
help_rc=$?
set -e
assert_eq "0" "$help_rc" "--help exits 0"
assert_contains "Runtime adapter #2" "$help_out" "--help renders the header docs"
assert_contains "gpeyton/loom fork" "$help_out" \
    "--help preserves attribution to the gpeyton/loom fork"
assert_not_contains "set -euo" "$help_out" \
    "--help stops before the shebang body (no 'set -euo' leak)"

# ============================================================
# Helper: run spawn-codex.sh in argv-only (LOOM_CODEX_NO_EXEC) mode.
#
# LOOM_SWEEP_NICE=0 disables the #4233 nice re-exec so the test observes one
# clean pass. LOOM_SPAWN_NO_EXPORT=1 skips auth resolution unless a test is
# specifically exercising it. `env -u` clears host-inherited Codex vars so a
# developer's own CODEX_HOME/profile can never perturb an assertion.
# ============================================================

run_argv() {
    # usage: run_argv [VAR=val ...] -- <args...>
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
        -u LOOM_CODEX_SANDBOX -u LOOM_CODEX_NETWORK -u LOOM_MODEL \
        -u LOOM_EFFORT -u LOOM_CODEX_MODEL \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_CODEX" "$@" 2>/dev/null || true
}

run_argv_stderr() {
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    # Capture stderr ONLY: stdout is discarded inside the group, and the
    # group's stderr becomes this function's stdout.
    { env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
        -u LOOM_CODEX_SANDBOX -u LOOM_CODEX_NETWORK -u LOOM_MODEL \
        -u LOOM_EFFORT -u LOOM_CODEX_MODEL \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_CODEX" "$@" >/dev/null; } 2>&1 || true
}

# ============================================================
# Section 1: argv assembly + prompt delivery (contract point 1)
# ============================================================

echo ""
echo "Testing spawn-codex.sh argv assembly + prompt delivery..."

out="$(run_argv -- -p "hello world")"
assert_contains "spawn-codex would-exec: codex exec" "$out" \
    "-p produces a 'codex exec' invocation"
assert_contains "hello world" "$out" "the prompt text reaches the argv"

# The prompt must be the LAST (positional) argument, because on `codex exec`
# `-p` means --profile, not prompt. Assert the tail explicitly.
assert_eq "hello world" "${out##* -s read-only }" \
    "the prompt is delivered POSITIONALLY as the final argv element"
assert_not_contains "codex exec -p" "$out" \
    "-p is CONSUMED, never forwarded to codex (where -p means --profile)"

# --prompt and the =-forms are equivalent.
assert_contains "alt-form" "$(run_argv -- --prompt "alt-form")" \
    "--prompt is accepted as a synonym for -p"
assert_contains "eq-form" "$(run_argv -- --prompt=eq-form)" \
    "--prompt=VALUE is accepted"
assert_contains "short-eq" "$(run_argv -- -p=short-eq)" \
    "-p=VALUE is accepted"

# No prompt -> interactive shape: bare `codex`, no `exec` subcommand.
out="$(run_argv -- --some-flag)"
assert_not_contains "codex exec" "$out" \
    "no -p means no 'exec' subcommand (interactive shape)"
assert_contains "spawn-codex would-exec: codex" "$out" \
    "no -p still assembles a bare 'codex' invocation"

# Observability: exactly one model line and one sandbox line per spawn.
err="$(run_argv_stderr -- -p x)"
assert_eq "1" "$(printf '%s\n' "$err" | grep -c 'spawn-codex: model=')" \
    "exactly ONE structured 'spawn-codex: model=' line per spawn"
assert_eq "1" "$(printf '%s\n' "$err" | grep -c 'spawn-codex: sandbox=')" \
    "exactly ONE structured 'spawn-codex: sandbox=' line per spawn"
assert_contains "spawn-codex: LOOM_SWEEP_CLAIM_OWNED=" "$err" \
    "the daemon self-claim marker is logged on every spawn"

# ============================================================
# Section 2: sandbox mapping + precedence (SAFETY-CRITICAL)
# ============================================================

echo ""
echo "Testing spawn-codex.sh sandbox mapping (SAFETY-CRITICAL)..."

out="$(run_argv -- -p x)"
assert_contains "-s read-only" "$out" \
    "DEFAULT sandbox is read-only (most restrictive; tier-2 posture)"

err="$(run_argv_stderr -- -p x)"
assert_contains "sandbox=read-only source=adapter-default" "$err" \
    "default sandbox logs source=adapter-default"

# Loom's skip-permissions convention maps to workspace-write, NOT full access.
out="$(run_argv -- -p x --dangerously-skip-permissions)"
assert_contains "-s workspace-write" "$out" \
    "--dangerously-skip-permissions maps to workspace-write"
assert_not_contains "danger-full-access" "$out" \
    "--dangerously-skip-permissions does NOT map to danger-full-access (fork divergence)"
assert_not_contains "--dangerously-bypass-approvals-and-sandbox" "$out" \
    "--dangerously-skip-permissions never emits Codex's bypass-everything flag"
assert_not_contains "--dangerously-skip-permissions" "$out" \
    "the Claude-specific flag is CONSUMED, never forwarded to codex"

# LOOM_CODEX_SANDBOX overrides the convention.
out="$(run_argv LOOM_CODEX_SANDBOX=danger-full-access -- -p x --dangerously-skip-permissions)"
assert_contains "-s danger-full-access" "$out" \
    "LOOM_CODEX_SANDBOX overrides the skip-permissions mapping"
assert_not_contains "-s workspace-write" "$out" \
    "LOOM_CODEX_SANDBOX replaces (not appends to) the convention's mode"

# An explicit -s/--sandbox arg beats the env var, and is not duplicated.
out="$(run_argv LOOM_CODEX_SANDBOX=danger-full-access -- -p x -s read-only)"
assert_contains "-s read-only" "$out" \
    "explicit -s wins over LOOM_CODEX_SANDBOX"
assert_not_contains "danger-full-access" "$out" \
    "explicit -s suppresses the env-var mode entirely"
assert_eq "1" "$(printf '%s\n' "$out" | grep -oc -- '-s ' || true)" \
    "explicit -s is never duplicated by an injected -s"

out="$(run_argv -- -p x --sandbox=workspace-write)"
assert_contains "--sandbox=workspace-write" "$out" \
    "--sandbox=VALUE is recognized as explicit and passed through"
assert_not_contains "-s read-only" "$out" \
    "--sandbox=VALUE suppresses the injected default"

# Codex's own bypass flag counts as an explicit sandbox decision (Codex would
# reject a conflicting -s alongside it).
out="$(run_argv -- -p x --dangerously-bypass-approvals-and-sandbox)"
assert_contains "--dangerously-bypass-approvals-and-sandbox" "$out" \
    "Codex's bypass flag passes through when the operator supplies it"
assert_not_contains "-s read-only" "$out" \
    "Codex's bypass flag suppresses the injected -s (no conflicting flags)"

# An invalid LOOM_CODEX_SANDBOX is a config error, not a silent widening.
set +e
bad_out="$(env -u CODEX_HOME LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 \
    LOOM_SPAWN_NO_EXPORT=1 LOOM_CODEX_SANDBOX=wide-open \
    bash "$SPAWN_CODEX" -p x 2>&1)"
bad_rc=$?
set -e
assert_eq "78" "$bad_rc" "an invalid LOOM_CODEX_SANDBOX exits 78 (EX_CONFIG)"
assert_contains "Invalid LOOM_CODEX_SANDBOX" "$bad_out" \
    "the invalid-sandbox message names the offending env var"
assert_contains "read-only workspace-write danger-full-access" "$bad_out" \
    "the invalid-sandbox message lists the valid modes"

# Network coupling: only meaningful under workspace-write.
out="$(run_argv LOOM_CODEX_SANDBOX=workspace-write LOOM_CODEX_NETWORK=1 -- -p x)"
assert_contains "sandbox_workspace_write.network_access=true" "$out" \
    "LOOM_CODEX_NETWORK=1 enables network under workspace-write"

out="$(run_argv LOOM_CODEX_NETWORK=1 -- -p x)"
assert_not_contains "network_access" "$out" \
    "LOOM_CODEX_NETWORK=1 injects nothing under read-only"
err="$(run_argv_stderr LOOM_CODEX_NETWORK=1 -- -p x)"
assert_contains "has no effect under sandbox=read-only" "$err" \
    "LOOM_CODEX_NETWORK=1 warns when the sandbox cannot use it"

# ============================================================
# Section 3: model + effort mapping (contract point 2)
# ============================================================

echo ""
echo "Testing spawn-codex.sh model + effort mapping..."

out="$(run_argv -- -p x)"
assert_not_contains " -m " "$out" \
    "no model configured emits NO -m flag (Codex/profile default preserved)"
err="$(run_argv_stderr -- -p x)"
assert_contains "spawn-codex: model=default" "$err" \
    "no model configured logs model=default"

out="$(run_argv LOOM_MODEL=gpt-5.6-sol -- -p x)"
assert_contains "-m gpt-5.6-sol" "$out" "LOOM_MODEL maps to -m <value>"

out="$(run_argv LOOM_MODEL=from-env -- -p x -m from-arg)"
assert_contains "-m from-arg" "$out" "an explicit -m wins over LOOM_MODEL"
assert_not_contains "from-env" "$out" "LOOM_MODEL is not also appended"
err="$(run_argv_stderr LOOM_MODEL=from-env -- -p x -m from-arg)"
assert_contains "explicit -m/--model in args wins over LOOM_MODEL" "$err" \
    "the model-precedence override is logged"

out="$(run_argv LOOM_CODEX_MODEL=adapter-default -- -p x)"
assert_contains "-m adapter-default" "$out" \
    "LOOM_CODEX_MODEL supplies the adapter's static default model"
out="$(run_argv LOOM_CODEX_MODEL=adapter-default LOOM_MODEL=tier-model -- -p x)"
assert_contains "-m tier-model" "$out" \
    "LOOM_MODEL outranks the adapter's static default"

# codex exec has no --effort flag; the knob is a config override.
out="$(run_argv LOOM_EFFORT=high -- -p x)"
assert_contains "-c model_reasoning_effort=high" "$out" \
    "LOOM_EFFORT maps to -c model_reasoning_effort=<value>"
out="$(run_argv -- -p x)"
assert_not_contains "model_reasoning_effort" "$out" \
    "no LOOM_EFFORT emits no reasoning-effort override"
out="$(run_argv -- -p daemon --effort xhigh --dangerously-skip-permissions --use-wrapper)"
assert_contains "-c model_reasoning_effort=xhigh" "$out" \
    "daemon --effort maps to Codex reasoning config"
assert_contains "-s workspace-write" "$out" \
    "daemon unattended permissions map to workspace-write"
assert_not_contains "--effort" "$out" \
    "Claude-only --effort never reaches codex exec"
assert_not_contains "--use-wrapper" "$out" \
    "generic retry convention never reaches codex exec"
out="$(run_argv LOOM_EFFORT=high -- -p x -c model_reasoning_effort=low)"
assert_not_contains "model_reasoning_effort=high" "$out" \
    "an explicit -c model_reasoning_effort= wins over LOOM_EFFORT"

# ============================================================
# Section 3b: Claude-shaped model refusal (issue #5028)
# ============================================================

echo ""
echo "Testing spawn-codex.sh Claude-shaped model refusal..."

run_refusal() {
    # usage: run_refusal [VAR=val ...] -- <args...>
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    set +e
    out="$(env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
        -u LOOM_CODEX_SANDBOX -u LOOM_CODEX_NETWORK -u LOOM_MODEL \
        -u LOOM_EFFORT -u LOOM_CODEX_MODEL \
        LOOM_SWEEP_NICE=0 LOOM_SPAWN_NO_EXPORT=1 \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_CODEX" "$@" 2>&1)"
    REFUSAL_RC=$?
    set -e
    REFUSAL_OUT="$out"
}

for claude_model in opus opusplan sonnet haiku fable claude-opus-5 OPUS "sonnet@xhigh"; do
    run_refusal -- -p x -m "$claude_model"
    assert_eq "78" "$REFUSAL_RC" "explicit -m $claude_model exits 78 (EX_CONFIG)"
    assert_contains "refusing Claude-shaped model" "$REFUSAL_OUT" \
        "the refusal message names the problem for -m $claude_model"
    assert_contains "autonomous.roleRunner.roleModels" "$REFUSAL_OUT" \
        "the refusal names the config key to fix for -m $claude_model"
done

# The refusal fires identically via LOOM_MODEL and LOOM_CODEX_MODEL, not just -m.
run_refusal LOOM_MODEL=sonnet -- -p x
assert_eq "78" "$REFUSAL_RC" "LOOM_MODEL=sonnet exits 78"
run_refusal LOOM_CODEX_MODEL=opus -- -p x
assert_eq "78" "$REFUSAL_RC" "LOOM_CODEX_MODEL=opus exits 78"

# No argv is ever assembled for a refused launch.
run_refusal -- -p x -m sonnet
assert_not_contains "would-exec" "$REFUSAL_OUT" \
    "a refused launch never reaches argv assembly/dispatch"

# Fail-open: Codex-valid and unrecognized model names are never refused.
for ok_model in gpt-5-codex o3-mini gpt-4.1 mystery-model-9000; do
    out="$(run_argv -- -p x -m "$ok_model")"
    assert_contains "-m $ok_model" "$out" "a Codex/unrecognized model '$ok_model' is never refused"
done

# Escape hatch: with LOOM_CODEX_NO_EXEC=1 (argv-only mode), the refusal check
# is bypassed entirely and the run proceeds to normal argv assembly.
out="$(run_argv LOOM_CODEX_MODEL_CHECK=0 -- -p x -m sonnet)"
assert_contains "-m sonnet" "$out" "LOOM_CODEX_MODEL_CHECK=0 disables the refusal"

# ============================================================
# Section 3c: ChatGPT-plan auth-mode guard for a pinned model (issue #5499)
# ============================================================
#
# A ChatGPT-plan Codex profile only accepts its account's own default model —
# even a Codex-family model (e.g. `gpt-5-codex`), which the Claude-shaped
# refusal above never touches, still 400s on the wire. Verified against a fake
# `codex` on PATH whose `login status` subcommand answers with the CLI's own
# real wording (confirmed live against codex-cli 0.46.0: `codex login status`
# prints exactly "Logged in using ChatGPT" / an API-key profile prints
# "Logged in using an API key").

echo ""
echo "Testing spawn-codex.sh ChatGPT-plan auth-mode guard..."

AUTHMODE_BIN="$TMPROOT/authmode-bin"
mkdir -p "$AUTHMODE_BIN"
AUTHMODE_ARGV_FILE="$TMPROOT/authmode-argv.txt"

# mk_authmode_codex <login-status-line> — a fake codex that answers `login
# status` with the given line, and otherwise behaves like the Section 8 mock
# (records argv, emits a session id, exits 0).
mk_authmode_codex() {
    local status_line="$1"
    cat > "$AUTHMODE_BIN/codex" <<MOCK
#!/usr/bin/env bash
if [[ "\$1" == "login" && "\$2" == "status" ]]; then
    echo "$status_line"
    exit 0
fi
printf '%s\n' "\$*" > "$AUTHMODE_ARGV_FILE"
{
  echo "session id: 00000000-0000-0000-0000-000000000000"
  echo "tokens used"
  echo "1"
} >&2
echo "AUTHMODE-FINAL"
exit 0
MOCK
    chmod +x "$AUTHMODE_BIN/codex"
}

run_authmode() {
    # usage: run_authmode [VAR=val ...] -- <args...>
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    : > "$AUTHMODE_ARGV_FILE"
    env -u LOOM_ROLE -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
        LOOM_SWEEP_NICE=0 LOOM_SPAWN_NO_EXPORT=1 PATH="$AUTHMODE_BIN:$PATH" \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_CODEX" "$@" 2>&1
}

# (1) A ChatGPT-plan profile: the pinned Codex-family model is DROPPED with a
# warning, and the CLI still runs successfully on its own default.
mk_authmode_codex "Logged in using ChatGPT"
out="$(run_authmode -- -p x -m gpt-5-codex)"
assert_contains "authenticated via a ChatGPT plan" "$out" \
    "a ChatGPT-plan profile with a pinned model warns about the auth-mode conflict"
assert_contains "dropping the pinned model 'gpt-5-codex'" "$out" \
    "the warning names the dropped model"
assert_not_contains "-m gpt-5-codex" "$(cat "$AUTHMODE_ARGV_FILE")" \
    "the pinned model is NOT forwarded to the ChatGPT-plan profile's codex invocation"
assert_contains "AUTHMODE-FINAL" "$out" \
    "the invocation still runs (on the account's own default) rather than being refused outright"

# (2) An API-key profile: no conflict, the pinned model passes through
# unchanged.
mk_authmode_codex "Logged in using an API key"
out="$(run_authmode -- -p x -m gpt-5-codex)"
assert_not_contains "authenticated via a ChatGPT plan" "$out" \
    "an API-key profile is never warned about the auth-mode conflict"
assert_contains "-m gpt-5-codex" "$(cat "$AUTHMODE_ARGV_FILE")" \
    "the pinned model IS forwarded to an API-key profile's codex invocation"

# (3) No model pinned at all: the guard has nothing to check and never shells
# out to `codex login status` (the mocked codex still errors if it did, since
# only login status/argv-recording are implemented — passing here already
# proves it wasn't invoked in an unexpected shape).
mk_authmode_codex "Logged in using ChatGPT"
out="$(run_authmode -- -p x)"
assert_not_contains "authenticated via a ChatGPT plan" "$out" \
    "no pinned model means nothing to drop — the guard is a no-op"

# (4) Escape hatch: LOOM_CODEX_AUTH_MODE_CHECK=0 keeps the pin even against a
# ChatGPT-plan profile.
mk_authmode_codex "Logged in using ChatGPT"
out="$(run_authmode LOOM_CODEX_AUTH_MODE_CHECK=0 -- -p x -m gpt-5-codex)"
assert_not_contains "authenticated via a ChatGPT plan" "$out" \
    "LOOM_CODEX_AUTH_MODE_CHECK=0 disables the guard"
assert_contains "-m gpt-5-codex" "$(cat "$AUTHMODE_ARGV_FILE")" \
    "the pinned model survives with the guard disabled"

# (5) LOOM_CODEX_NO_EXEC=1 (argv-preview mode) never shells out to the real
# CLI at all — the guard is skipped and the previewed argv still shows the
# pin, consistent with the mode's "never touch the real CLI" contract.
mk_authmode_codex "Logged in using ChatGPT"
out="$(env -u LOOM_ROLE -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
    LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
    PATH="$AUTHMODE_BIN:$PATH" bash "$SPAWN_CODEX" -p x -m gpt-5-codex 2>&1)"
assert_contains "-m gpt-5-codex" "$out" \
    "LOOM_CODEX_NO_EXEC=1 previews the pin unchanged (the guard never shells out under NO_EXEC)"

# (6) The `-m=value` / `--model` / `--model=value` forms are all stripped too,
# and every OTHER passthrough arg survives the filter.
mk_authmode_codex "Logged in using ChatGPT"
out="$(run_authmode -- -p x --model gpt-5-codex --effort high -s workspace-write)"
assert_not_contains "gpt-5-codex" "$(cat "$AUTHMODE_ARGV_FILE")" \
    "--model <value> (long form) is stripped too"
assert_contains "model_reasoning_effort=high" "$(cat "$AUTHMODE_ARGV_FILE")" \
    "a sibling passthrough arg (--effort) survives the model filter"
assert_contains "workspace-write" "$(cat "$AUTHMODE_ARGV_FILE")" \
    "a sibling passthrough arg (-s) survives the model filter"

# ============================================================
# Section 4: git-repo trust check (live-CLI behavior)
# ============================================================

echo ""
echo "Testing spawn-codex.sh git-repo trust check..."

NOTGIT="$TMPROOT/not-a-repo"
mkdir -p "$NOTGIT"

out="$(cd "$NOTGIT" && run_argv -- -p x)"
assert_contains "--skip-git-repo-check" "$out" \
    "outside a git work tree, --skip-git-repo-check is injected"

err="$(cd "$NOTGIT" && run_argv_stderr -- -p x)"
assert_contains "is not inside a git work tree" "$err" \
    "the injection is warned about, not silent"

# Inside a work tree the guardrail is left ENABLED (this repo is one).
out="$(cd "$SCRIPTS_DIR" && run_argv -- -p x)"
assert_not_contains "--skip-git-repo-check" "$out" \
    "inside a git work tree, the trusted-directory check is NOT waived"

# An operator-supplied flag is not duplicated.
out="$(cd "$NOTGIT" && run_argv -- -p x --skip-git-repo-check)"
assert_eq "1" "$(printf '%s\n' "$out" | grep -oc -- '--skip-git-repo-check' || true)" \
    "an explicit --skip-git-repo-check is not duplicated"

# ============================================================
# Section 5: args passthrough + usage errors
# ============================================================

echo ""
echo "Testing spawn-codex.sh args passthrough + usage errors..."

out="$(run_argv -- -p x --json --color never)"
assert_contains "--json" "$out" "unknown flags pass through verbatim"
assert_contains "--color never" "$out" "flag+value pairs pass through verbatim"

out="$(run_argv -- -p x -- --after-ddash)"
assert_contains "--after-ddash" "$out" "-- forwards the remainder verbatim"

out="$(run_argv -- -p x --profile someprofile)"
assert_contains "--profile someprofile" "$out" \
    "--profile (Codex's own long form) passes through untouched"

set +e
noval_out="$(env LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
    bash "$SPAWN_CODEX" -p 2>&1)"
noval_rc=$?
set -e
assert_eq "78" "$noval_rc" "-p with no value exits 78 (EX_CONFIG)"
assert_contains "requires a value" "$noval_out" "-p with no value says so"

set +e
noval_model_out="$(env LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
    bash "$SPAWN_CODEX" -p x -m 2>&1)"
noval_model_rc=$?
set -e
assert_eq "78" "$noval_model_rc" "-m with no value exits 78 (EX_CONFIG)"
assert_contains "requires a value" "$noval_model_out" "-m with no value says so"

set +e
noval_sandbox_out="$(env LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
    bash "$SPAWN_CODEX" -p x -s 2>&1)"
noval_sandbox_rc=$?
set -e
assert_eq "78" "$noval_sandbox_rc" "-s with no value exits 78 (EX_CONFIG)"
assert_contains "requires a value" "$noval_sandbox_out" "-s with no value says so"

# ============================================================
# Section 6: CODEX_HOME profile selection (auth)
# ============================================================

echo ""
echo "Testing spawn-codex.sh CODEX_HOME profile selection..."

GOOD_PROFILE="$TMPROOT/profiles/alice"
mkdir -p "$GOOD_PROFILE"
printf '{"SUPER_SECRET_TOKEN":"tok-do-not-log"}\n' > "$GOOD_PROFILE/auth.json"
EMPTY_PROFILE="$TMPROOT/profiles/bob"
mkdir -p "$EMPTY_PROFILE"
: > "$EMPTY_PROFILE/auth.json"   # zero-byte == unusable
MISSING_PROFILE="$TMPROOT/profiles/carol"
mkdir -p "$MISSING_PROFILE"      # no auth.json at all

run_auth() {
    # Like run_argv but WITHOUT LOOM_SPAWN_NO_EXPORT, so auth resolution runs.
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_CODEX" "$@" 2>&1 || true
}

out="$(run_auth "LOOM_CODEX_HOME=$GOOD_PROFILE" -- -p x)"
assert_contains "using Codex profile 'alice' (source=LOOM_CODEX_HOME)" "$out" \
    "LOOM_CODEX_HOME with a usable auth.json selects the profile"
assert_not_contains "tok-do-not-log" "$out" \
    "auth.json CONTENTS are never logged (credential hygiene)"
assert_not_contains "$GOOD_PROFILE/auth.json" "$out" \
    "the auth.json path is not echoed on the success path"

out="$(run_auth "CODEX_HOME=$GOOD_PROFILE" -- -p x)"
assert_contains "source=CODEX_HOME" "$out" \
    "a pre-set CODEX_HOME is honored verbatim (auth tier 2)"

out="$(run_auth "LOOM_CODEX_PROFILE_ROOT=$TMPROOT/profiles" \
    LOOM_CODEX_PROFILE=alice -- -p x)"
assert_contains "source=LOOM_CODEX_PROFILE" "$out" \
    "LOOM_CODEX_PROFILE resolves a bare name under the profile root"
assert_contains "using Codex profile 'alice'" "$out" \
    "LOOM_CODEX_PROFILE logs the directory NAME only"

# LOOM_CODEX_HOME outranks a pre-set CODEX_HOME.
out="$(run_auth "LOOM_CODEX_HOME=$GOOD_PROFILE" "CODEX_HOME=$MISSING_PROFILE" -- -p x)"
assert_contains "source=LOOM_CODEX_HOME" "$out" \
    "LOOM_CODEX_HOME outranks a pre-set CODEX_HOME"

# An explicitly-requested profile with no usable credential fails LOUDLY.
for bad in "$EMPTY_PROFILE" "$MISSING_PROFILE"; do
    set +e
    env -u CODEX_HOME -u LOOM_CODEX_PROFILE LOOM_SWEEP_NICE=0 \
        LOOM_CODEX_NO_EXEC=1 LOOM_CODEX_HOME="$bad" \
        bash "$SPAWN_CODEX" -p x >/dev/null 2>&1
    bad_auth_rc=$?
    set -e
    assert_eq "78" "$bad_auth_rc" \
        "a requested profile with an unusable auth.json exits 78 ($(basename "$bad"))"
done

out="$(run_auth "LOOM_CODEX_HOME=$MISSING_PROFILE" -- -p x)"
assert_contains "has no usable auth.json" "$out" \
    "the missing-credential message explains the fault"
assert_contains "codex login" "$out" \
    "the missing-credential message gives the provisioning command"

# No profile requested -> ambient auth, never a failure.
out="$(run_auth -- -p x)"
assert_contains "using the Codex CLI's ambient login state" "$out" \
    "no profile requested falls through to ambient auth (not an error)"

out="$(run_argv -- -p x)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -n "$out" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: LOOM_SPAWN_NO_EXPORT skips auth resolution without failing"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: LOOM_SPAWN_NO_EXPORT skips auth resolution without failing"
fi

# ============================================================
# Section 7: missing binary -> 127 (NOT 78)
# ============================================================

echo ""
echo "Testing spawn-codex.sh missing-binary failure..."

# An empty PATH (plus the coreutils dirs the script needs) with no `codex`.
EMPTY_BIN="$TMPROOT/empty-bin"
mkdir -p "$EMPTY_BIN"
set +e
missing_out="$(env -u CODEX_HOME LOOM_SWEEP_NICE=0 LOOM_SPAWN_NO_EXPORT=1 \
    PATH="$EMPTY_BIN:/usr/bin:/bin" \
    bash "$SPAWN_CODEX" -p x 2>&1)"
missing_rc=$?
set -e
assert_eq "127" "$missing_rc" \
    "a missing codex binary exits 127, NOT 78 (78 is reserved for config errors)"
assert_contains "'codex' command not found in PATH" "$missing_out" \
    "the missing-binary message names the binary"
assert_contains "0.146.0" "$missing_out" \
    "the missing-binary message states the minimum supported version"

# ============================================================
# Section 8: mocked dispatch — stream split, stdin, session/tokens, exit code
# ============================================================

echo ""
echo "Testing spawn-codex.sh mocked dispatch (fake codex on PATH)..."

MOCK_BIN="$TMPROOT/mock-bin"
mkdir -p "$MOCK_BIN"
MOCK_SESSION="019fafeb-bdff-7fc1-a1c2-fba0ae4e5f29"
cat > "$MOCK_BIN/codex" <<MOCK
#!/usr/bin/env bash
# Fake codex CLI reproducing codex-cli 0.146.0's OBSERVED stream split:
#   stdout = the agent's final message, and nothing else
#   stderr = banner (incl. 'session id:'), message blocks, 'tokens used'
# Records its argv and whether it saw any stdin, for assertions.
printf '%s\n' "\$@" > "\$MOCK_ARGV_FILE"
_stdin="\$(cat)"
if [[ -n "\$_stdin" ]]; then echo "MOCK-SAW-STDIN:\$_stdin" >&2; fi
{
  echo "OpenAI Codex v0.146.0"
  echo "--------"
  echo "sandbox: read-only"
  echo "session id: $MOCK_SESSION"
  echo "--------"
  echo "tokens used"
  echo "2,502"
  [[ -n "\${MOCK_ERROR:-}" ]] && echo "\$MOCK_ERROR"
} >&2
echo "MOCK-FINAL-MESSAGE"
exit "\${MOCK_RC:-0}"
MOCK
chmod +x "$MOCK_BIN/codex"

MOCK_ARGV_FILE="$TMPROOT/mock-argv.txt"

run_mock() {
    local -a envs=()
    while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
    shift || true
    env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
        LOOM_SWEEP_NICE=0 LOOM_SPAWN_NO_EXPORT=1 \
        MOCK_ARGV_FILE="$MOCK_ARGV_FILE" \
        PATH="$MOCK_BIN:$PATH" \
        ${envs[@]+"${envs[@]}"} \
        bash "$SPAWN_CODEX" "$@"
}

# stdout carries ONLY the agent's final message — the banner/session/token
# lines must not contaminate it (a caller pipes this).
mock_stdout="$(run_mock -- -p "hi" 2>/dev/null)"
assert_eq "MOCK-FINAL-MESSAGE" "$mock_stdout" \
    "stdout carries ONLY the agent's final message (banner stays on stderr)"

mock_stderr="$({ run_mock -- -p "hi" >/dev/null; } 2>&1)"
assert_contains "session id: $MOCK_SESSION" "$mock_stderr" \
    "the child's stderr is still forwarded to the caller's stderr"
assert_contains "spawn-codex: session=$MOCK_SESSION" "$mock_stderr" \
    "the 'session id:' line is parsed and reported as the transcript join key"
assert_contains "spawn-codex: tokens_used=2502" "$mock_stderr" \
    "'tokens used' is parsed (comma-stripped) and reported"
assert_contains \
    "# LOOM_TERMINAL_RESULT v=1 provider=codex account=unknown category=SUCCESS exit_code=0" \
    "$mock_stderr" \
    "a successful child emits one strict structured terminal record"
assert_not_contains "MOCK-SAW-STDIN" "$mock_stderr" \
    "the child's stdin is /dev/null (no <stdin> append, no hang)"

# Even with data waiting on OUR stdin, the child must see none of it.
mock_stderr="$(printf 'unwanted stdin payload\n' | { run_mock -- -p "hi" >/dev/null; } 2>&1)"
assert_not_contains "MOCK-SAW-STDIN" "$mock_stderr" \
    "piped stdin is NOT forwarded to the child (the 0.146.0 stdin-append hang)"

# The mock received the argv the adapter assembled, prompt last.
assert_contains "exec" "$(cat "$MOCK_ARGV_FILE")" "the mock was invoked with the exec subcommand"
assert_eq "hi" "$(tail -1 "$MOCK_ARGV_FILE")" "the prompt is the final positional argv element"

# Exit-code passthrough despite not using `exec`.
set +e
run_mock MOCK_RC=42 -- -p "hi" >/dev/null 2>&1
mock_rc=$?
set -e
assert_eq "42" "$mock_rc" "the codex exit code is passed through (PIPESTATUS)"

set +e
classified_stderr="$({ run_mock MOCK_RC=42 LOOM_ACCOUNT_NAME=profile-a \
    MOCK_ERROR="Not signed in. recognizable-secret" -- -p "hi" >/dev/null; } 2>&1)"
classified_rc=$?
set -e
assert_eq "42" "$classified_rc" \
    "structured classification preserves the original child exit code"
terminal_record="$(printf '%s\n' "$classified_stderr" | grep '^# LOOM_TERMINAL_RESULT ' || true)"
assert_eq \
    "# LOOM_TERMINAL_RESULT v=1 provider=codex account=profile-a category=TOKEN_EXPIRED exit_code=42" \
    "$terminal_record" \
    "the structured record matches the direct Codex classifier and carries account identity"
assert_not_contains "recognizable-secret" "$terminal_record" \
    "the structured record never copies raw child output"

set +e
run_mock MOCK_RC=0 -- -p "hi" >/dev/null 2>&1
mock_rc0=$?
set -e
assert_eq "0" "$mock_rc0" "a clean codex exit stays 0"

# Transcript-path resolution: a real session JSONL under $CODEX_HOME/sessions.
SESSION_PROFILE="$TMPROOT/profiles/dana"
mkdir -p "$SESSION_PROFILE/sessions/2026/07/29"
printf '{"k":"v"}\n' > "$SESSION_PROFILE/auth.json"
TRANSCRIPT="$SESSION_PROFILE/sessions/2026/07/29/rollout-2026-07-29T15-08-10-${MOCK_SESSION}.jsonl"
: > "$TRANSCRIPT"
mock_stderr="$({ env -u LOOM_CODEX_PROFILE LOOM_SWEEP_NICE=0 \
    MOCK_ARGV_FILE="$MOCK_ARGV_FILE" PATH="$MOCK_BIN:$PATH" \
    LOOM_CODEX_HOME="$SESSION_PROFILE" \
    bash "$SPAWN_CODEX" -p "hi" >/dev/null; } 2>&1 || true)"
assert_contains "spawn-codex: transcript=$TRANSCRIPT" "$mock_stderr" \
    "the session id resolves to the concrete transcript JSONL path"

# No session dir -> reported as unresolved, never a failure.
NO_SESSIONS="$TMPROOT/profiles/erin"
mkdir -p "$NO_SESSIONS"
printf '{"k":"v"}\n' > "$NO_SESSIONS/auth.json"
set +e
mock_stderr="$({ env -u LOOM_CODEX_PROFILE LOOM_SWEEP_NICE=0 \
    MOCK_ARGV_FILE="$MOCK_ARGV_FILE" PATH="$MOCK_BIN:$PATH" \
    LOOM_CODEX_HOME="$NO_SESSIONS" \
    bash "$SPAWN_CODEX" -p "hi" >/dev/null; } 2>&1)"
unresolved_rc=$?
set -e
assert_contains "transcript=unresolved" "$mock_stderr" \
    "an unresolvable transcript is reported, not guessed"
assert_eq "0" "$unresolved_rc" \
    "an unresolvable transcript does NOT fail the spawn"

# LOOM_CODEX_NO_CAPTURE takes the plain exec path (no session reporting).
mock_stderr="$({ run_mock LOOM_CODEX_NO_CAPTURE=1 -- -p "hi" >/dev/null; } 2>&1)"
assert_not_contains "spawn-codex: session=" "$mock_stderr" \
    "LOOM_CODEX_NO_CAPTURE=1 skips session reporting (plain exec path)"
assert_contains "session id: $MOCK_SESSION" "$mock_stderr" \
    "LOOM_CODEX_NO_CAPTURE=1 still passes the child's stderr through"

# ============================================================
# Section 9: LOOM_RUNTIME=codex resolves through spawn-worker.sh
# ============================================================

echo ""
echo "Testing LOOM_RUNTIME=codex dispatch through spawn-worker.sh..."

STAGE="$TMPROOT/stage"
WS="$TMPROOT/ws"
mkdir -p "$STAGE/lib" "$WS/.loom"
cp "$SCRIPTS_DIR/spawn-worker.sh" "$STAGE/spawn-worker.sh"
cp "$SCRIPTS_DIR/lib/config-resolver.sh" "$STAGE/lib/config-resolver.sh"
cp "$SPAWN_CODEX" "$STAGE/spawn-codex.sh"
# A stub claude runner so the "available runtimes" list and the default path
# stay realistic without ever reaching the real token-selection code.
cat > "$STAGE/spawn-claude.sh" <<'STUB'
#!/usr/bin/env bash
echo "stub-claude reached"
STUB
chmod +x "$STAGE"/spawn-*.sh

out="$(env -u LOOM_RUNTIME -u CODEX_HOME -u LOOM_CODEX_HOME \
    LOOM_WORKSPACE="$WS" LOOM_CONFIG_DEFAULTS_FILE="" \
    LOOM_RUNTIME=codex LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 \
    LOOM_SPAWN_NO_EXPORT=1 \
    bash "$STAGE/spawn-worker.sh" -p "routed" 2>&1 || true)"
assert_contains "runtime=codex (from env (LOOM_RUNTIME))" "$out" \
    "spawn-worker.sh resolves LOOM_RUNTIME=codex"
assert_contains "spawn-codex would-exec: codex exec" "$out" \
    "spawn-worker.sh dispatches into the real spawn-codex.sh"
assert_contains "routed" "$out" "the prompt survives the dispatcher hop"
assert_not_contains "stub-claude reached" "$out" \
    "codex dispatch does not also reach the claude runner"

if command -v jq >/dev/null 2>&1; then
    printf '{ "runtimes": { "default": "codex" } }\n' > "$WS/.loom/config.json"
    out="$(env -u LOOM_RUNTIME -u CODEX_HOME -u LOOM_CODEX_HOME \
        LOOM_WORKSPACE="$WS" LOOM_CONFIG_DEFAULTS_FILE="" \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
        bash "$STAGE/spawn-worker.sh" -p "cfg" 2>&1 || true)"
    assert_contains "runtime=codex (from config (runtimes.default))" "$out" \
        "runtimes.default=codex resolves through the dispatcher"
    assert_contains "spawn-codex would-exec" "$out" \
        "config-resolved codex dispatch reaches spawn-codex.sh"
    rm -f "$WS/.loom/config.json"
fi

# codex moving from unknown -> known must not weaken the unknown-runtime guard.
set +e
unknown_out="$(env -u LOOM_RUNTIME LOOM_WORKSPACE="$WS" \
    LOOM_CONFIG_DEFAULTS_FILE="" LOOM_RUNTIME=bogus \
    bash "$STAGE/spawn-worker.sh" -p x 2>&1)"
unknown_rc=$?
set -e
assert_eq "78" "$unknown_rc" \
    "an unknown runtime still exits 78 now that spawn-codex.sh exists"
assert_contains "codex" "$unknown_out" \
    "the available-runtimes list now includes codex"

# ============================================================
# Section 10: classify-error.sh `codex` provider vectors
# ============================================================

echo ""
echo "Testing classify-error.sh codex provider table..."

# shellcheck source=../lib/classify-error.sh
source "$CLASSIFY_LIB"

assert_classify() {
    local expected="$1" output="$2" rc="$3" provider="$4" msg="$5"
    assert_eq "$expected" "$(classify_error "$output" "$rc" "$provider")" "$msg"
}

# --- exit-code-first invariants hold for codex too (#3233) ---
assert_classify "SUCCESS" "You've hit your usage limit." 0 codex \
    "exit 0 is SUCCESS even when the output mentions a usage limit (#3233)"
assert_classify "SUCCESS" "Operation not permitted" 0 codex \
    "a sandbox denial on a clean exit stays SUCCESS (denials do not fail the run)"
assert_classify "TIMEOUT" "anything" 124 codex "exit 124 is TIMEOUT for codex"
assert_classify "TIMEOUT" "anything" 137 codex "exit 137 is TIMEOUT for codex"

# --- FATAL: non-retryable configuration faults ---
assert_classify "FATAL" \
    "Not inside a trusted directory and --skip-git-repo-check was not specified." \
    1 codex "the trusted-directory refusal is FATAL (retrying loops forever)"
assert_classify "FATAL" \
    "Error loading config.toml: unknown configuration field \`bogus\` in -c override" \
    1 codex "an unknown -c config field is FATAL"
assert_classify "FATAL" "landlock_sandbox_executable_not_provided" 1 codex \
    "an unconstructable sandbox is FATAL (never silently retried unsandboxed)"

# A ChatGPT-plan seat rejecting a pinned model is FATAL, not RECOVERABLE
# (issue #5499) — retrying the identical invocation can never succeed; the
# fault is the model/auth-mode pairing, not the transport.
assert_classify "FATAL" \
    "{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The 'gpt-5-codex' model is not supported when using Codex with a ChatGPT account.\"}}" \
    1 codex "a ChatGPT-account model-unsupported 400 is FATAL, not RECOVERABLE (#5499)"
assert_classify "FATAL" \
    "The 'sonnet' model is not supported when using Codex with a ChatGPT account." \
    1 codex "the same wording for a different pinned model is also FATAL (#5499)"

# --- TOKEN_EXPIRED: 401 / login / refresh-token failures ---
assert_classify "TOKEN_EXPIRED" \
    "ERROR codex_api: failed to connect to websocket: HTTP error: 401 Unauthorized, url: wss://api.openai.com/v1/responses" \
    1 codex "the observed 401 Unauthorized stream error is TOKEN_EXPIRED"
assert_classify "TOKEN_EXPIRED" \
    "Not signed in. Please run 'codex login' to sign in with ChatGPT" \
    1 codex "a not-signed-in profile is TOKEN_EXPIRED"
assert_classify "TOKEN_EXPIRED" \
    "Your token could not be refreshed because your refresh token has expired. Please log out and sign in again." \
    1 codex "an expired refresh token is TOKEN_EXPIRED"
assert_classify "TOKEN_EXPIRED" "Failed to refresh token: bad grant" 1 codex \
    "a refresh failure is TOKEN_EXPIRED"
assert_classify "TOKEN_EXPIRED" "Your session has expired. Please reauthenticate." 1 codex \
    "an expired session is TOKEN_EXPIRED"
assert_classify "TOKEN_EXPIRED" \
    '{"error":{"code":"invalid_api_key","message":"Incorrect API key provided"}}' \
    1 codex "invalid_api_key is TOKEN_EXPIRED"

# --- TOKEN_EXHAUSTED: plan/quota exhaustion ---
assert_classify "TOKEN_EXHAUSTED" \
    "You've hit your usage limit. Upgrade to Pro to continue using Codex" \
    1 codex "\"hit your usage limit\" is TOKEN_EXHAUSTED"
assert_classify "TOKEN_EXHAUSTED" \
    "You've reached your usage limit. Increase your limits to continue using codex." \
    1 codex "\"reached your usage limit\" is TOKEN_EXHAUSTED"
assert_classify "TOKEN_EXHAUSTED" \
    "Your workspace is out of credits. Ask your workspace admin." 1 codex \
    "an out-of-credits workspace is TOKEN_EXHAUSTED"
assert_classify "TOKEN_EXHAUSTED" \
    '{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}' \
    1 codex "insufficient_quota is TOKEN_EXHAUSTED"
assert_classify "TOKEN_EXHAUSTED" \
    '{"error":{"code":"rate_limit_exceeded"}}' 1 codex \
    "rate_limit_exceeded is TOKEN_EXHAUSTED"

# Quota wording must WIN over a co-occurring 401 (the ordering rationale: a
# TOKEN_EXPIRED verdict marks the account bad with reason `auth`, which persists
# until a manual unblock, whereas `exhausted` TTL-expires on its own).
assert_classify "TOKEN_EXHAUSTED" \
    "HTTP error: 401 Unauthorized — insufficient_quota: You exceeded your current quota" \
    1 codex "quota wording outranks a co-occurring 401 (safer mis-classification direction)"

# --- RECOVERABLE: transients still resolve through the generic table ---
assert_classify "RECOVERABLE" "HTTP error: 429 Too Many Requests" 1 codex \
    "a BARE 429 stays RECOVERABLE (not TOKEN_EXHAUSTED) for codex"
assert_classify "RECOVERABLE" "HTTP error: 503 Service Unavailable" 1 codex \
    "a 5xx is RECOVERABLE for codex"
assert_classify "RECOVERABLE" "ECONNREFUSED connecting to api.openai.com" 1 codex \
    "a network error is RECOVERABLE for codex"
assert_classify "RECOVERABLE" "something nobody has ever seen before" 1 codex \
    "an unrecognized codex failure falls through to RECOVERABLE"

# --- Deliberately unimplemented codex categories ---
assert_classify "RECOVERABLE" "The current working directory was deleted" 1 codex \
    "Claude's CWD_DELETED wording is NOT reused for codex (documented gap)"
assert_classify "RECOVERABLE" 'stop_reason: "refusal"' 1 codex \
    "Claude's MODEL_REFUSAL wording is NOT reused for codex (documented gap)"
assert_classify "RECOVERABLE" "maximum number of concurrent sessions" 1 codex \
    "codex has no SESSION_LIMIT signature; a concurrency fault stays RECOVERABLE"

# --- The claude table is UNCHANGED by this addition (regression guard) ---
echo ""
echo "Testing classify-error.sh claude table is unchanged..."

assert_classify "CWD_DELETED" "The current working directory was deleted" 1 claude \
    "claude CWD_DELETED unchanged"
assert_classify "MODEL_REFUSAL" 'stop_reason: "refusal"' 1 claude \
    "claude MODEL_REFUSAL unchanged"
assert_classify "TOKEN_EXPIRED" "401 authentication_error" 1 claude \
    "claude TOKEN_EXPIRED unchanged"
assert_classify "SESSION_LIMIT" "maximum number of concurrent sessions" 1 claude \
    "claude SESSION_LIMIT unchanged"
assert_classify "TOKEN_EXHAUSTED" "You've hit your weekly limit" 1 claude \
    "claude TOKEN_EXHAUSTED unchanged"
assert_classify "RECOVERABLE" "No messages returned" 1 claude \
    "claude 'No messages returned' unchanged"
assert_classify "SUCCESS" "500 rate limit" 0 claude \
    "claude exit-code-first (#3233) unchanged"

# claude never returns FATAL — the codex table is the only FATAL producer.
assert_classify "RECOVERABLE" \
    "Not inside a trusted directory and --skip-git-repo-check was not specified." \
    1 claude "the codex FATAL patterns do NOT leak into the claude table"
assert_classify "RECOVERABLE" \
    "The 'gpt-5-codex' model is not supported when using Codex with a ChatGPT account." \
    1 claude "the #5499 ChatGPT-account FATAL pattern does NOT leak into the claude table"

# Two-arg calls (claude-wrapper.sh) and unknown providers are unaffected.
assert_eq "TOKEN_EXHAUSTED" "$(classify_error "You've hit your weekly limit" 1)" \
    "a two-arg call still defaults to the claude table"
assert_classify "RECOVERABLE" "Not inside a trusted directory" 1 amp \
    "an unknown provider matches no provider table and falls through"

# The LOOM_RUNTIME selector reaches the codex table.
assert_eq "FATAL" \
    "$(LOOM_RUNTIME=codex classify_error "Not inside a trusted directory" 1)" \
    "LOOM_RUNTIME=codex selects the codex table for a two-arg call"

# ============================================================
# Managed Codex hook readiness / trust preflight (issue #4495)
#
# Hermetic: only temporary CODEX_HOME profiles and the repo's own bridge file.
# The real Codex CLI is never invoked (LOOM_CODEX_NO_EXEC=1 short-circuits
# before any exec, and the preflight runs BEFORE that hook anyway).
# ============================================================
echo ""
echo "Testing managed Codex hook readiness preflight (#4495)..."

PROVISION_SCRIPT="$SCRIPTS_DIR/provision-codex-hooks.sh"
BRIDGE_SCRIPT="$(cd "$SCRIPTS_DIR/../hooks" 2>/dev/null && pwd)/guard-codex-bridge.sh"

# Pin the workspace this section provisions/verifies against to LOOM_WORKSPACE
# instead of leaving it to spawn-codex.sh's own git-common-dir resolution
# (issue #5350). provision-codex-hooks.sh install below bakes in whatever
# --workspace value we pass it (here, $(pwd)); spawn-codex.sh's internal verify
# call resolves its OWN --workspace via _resolve_workspace(), which always
# returns the MAIN checkout path via `git rev-parse --git-common-dir`/dirname
# — by design (mirrors guard-worktree-paths.sh's confinement anchor) and NOT
# something this harness change alters. When this suite runs with its CWD
# inside a linked worktree, that main-checkout resolution diverges from the
# $(pwd) this section provisioned against, so the managed-hook verify sees a
# bogus "different bridge than this workspace's" mismatch. Exporting
# LOOM_WORKSPACE here makes both sides agree by construction, regardless of
# which directory the suite is invoked from — LOOM_WORKSPACE is
# _resolve_workspace()'s highest-precedence input, unchanged.
#
# Scope: this script is always run as its own `bash <file>` process (see
# .github/workflows/ci.yml and test-spawn-generic.sh, which reference this
# file by path, never `source` it), so this export cannot leak into another
# suite's shell.
export LOOM_WORKSPACE="$(pwd)"

TESTS_RUN=$((TESTS_RUN + 1))
if bash -n "$PROVISION_SCRIPT" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: provision-codex-hooks.sh passes bash -n"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: provision-codex-hooks.sh fails bash -n"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if bash -n "$BRIDGE_SCRIPT" 2>/dev/null; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: guard-codex-bridge.sh passes bash -n"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: guard-codex-bridge.sh fails bash -n"
fi

HOOKTMP="$(mktemp -d)"
mk_hook_profile() {
    local name="$1"
    local dir="$HOOKTMP/$name"
    mkdir -p "$dir"
    printf '{"tokens":{"refresh_token":"sk-loom-FAKE-4495"}}\n' > "$dir/auth.json"
    printf '%s' "$dir"
}

# run_preflight <expect-exit> <description> [VAR=val ...]
#
# Publishes the combined stdout+stderr in the global PREFLIGHT_OUT rather than
# printing it: the assertions below must run in THIS shell, not inside a
# command substitution, or their TESTS_RUN/TESTS_PASSED increments are lost in
# the subshell and the suite silently under-counts.
PREFLIGHT_OUT=""
run_preflight() {
    local expected="$1" desc="$2"
    shift 2
    local rc=0
    PREFLIGHT_OUT="$(env -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE -u LOOM_CODEX_SANDBOX \
        -u LOOM_CODEX_NETWORK -u LOOM_MODEL -u LOOM_EFFORT -u LOOM_CODEX_MODEL -u LOOM_ROLE \
        LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
        "$@" bash "$SPAWN_CODEX" -p "hi" 2>&1)" || rc=$?
    assert_eq "$expected" "$rc" "$desc"
}

READY_PROFILE="$(mk_hook_profile ready)"
# No explicit --bridge here: production `install` never passes one either, so
# this must resolve the bridge the exact same way spawn-codex.sh's `verify`
# call does (via resolve_bridge()'s --workspace-relative candidate). Pinning
# to $BRIDGE_SCRIPT (the defaults/ copy) would diverge from verify's
# resolution whenever a workspace-local .loom/hooks/guard-codex-bridge.sh
# copy also exists (see #4787), spuriously reporting hooks=not-ready.
bash "$PROVISION_SCRIPT" install --codex-home "$READY_PROFILE" \
    --workspace "$LOOM_WORKSPACE" >/dev/null 2>&1
printf 'hooks.state."loom".trusted_hash = "deadbeef"\n' > "$READY_PROFILE/config.toml"

BARE_PROFILE="$(mk_hook_profile bare)"

# (1) Builder with a ready profile starts.
run_preflight 0 "builder + ready managed hook -> proceeds" \
    LOOM_ROLE=builder CODEX_HOME="$READY_PROFILE"
out="$PREFLIGHT_OUT"
assert_contains "hooks=ready" "$out" "audit line reports hooks=ready"
assert_contains "trust-bypass=never" "$out" "audit line records that trust is never bypassed"
assert_not_contains "sk-loom-FAKE-4495" "$out" "the audit line leaks no credential material"
assert_not_contains "auth.json" "$out" "the audit line names no credential file for a ready profile"

# (2) Builder with an unprovisioned profile fails closed BEFORE the CLI starts.
run_preflight 78 "builder + unprovisioned profile -> exit 78" \
    LOOM_ROLE=builder CODEX_HOME="$BARE_PROFILE"
out="$PREFLIGHT_OUT"
assert_not_contains "would-exec" "$out" "no codex argv is assembled when the preflight fails"
assert_contains "not ready" "$out" "the failure explains that hook parity is not ready"

# (3) Builder with a provisioned-but-untrusted profile fails closed.
UNTRUSTED_PROFILE="$(mk_hook_profile untrusted)"
bash "$PROVISION_SCRIPT" install --codex-home "$UNTRUSTED_PROFILE" \
    --workspace "$LOOM_WORKSPACE" --bridge "$BRIDGE_SCRIPT" >/dev/null 2>&1
run_preflight 78 "builder + installed-but-untrusted profile -> exit 78" \
    LOOM_ROLE=builder CODEX_HOME="$UNTRUSTED_PROFILE"

# (4) Builder with a STALE (tampered) entry fails closed.
STALE_PROFILE="$(mk_hook_profile stale)"
bash "$PROVISION_SCRIPT" install --codex-home "$STALE_PROFILE" \
    --workspace "$LOOM_WORKSPACE" --bridge "$BRIDGE_SCRIPT" >/dev/null 2>&1
printf 'hooks.state."loom".trusted_hash = "deadbeef"\n' > "$STALE_PROFILE/config.toml"
jq '(.hooks.PreToolUse[].hooks[] | select(.command | contains("guard-codex-bridge.sh")) | .command) |= (. + " --tampered")' \
    "$STALE_PROFILE/hooks.json" > "$STALE_PROFILE/hooks.tmp" \
    && mv "$STALE_PROFILE/hooks.tmp" "$STALE_PROFILE/hooks.json"
run_preflight 78 "builder + stale managed-hook entry -> exit 78" \
    LOOM_ROLE=builder CODEX_HOME="$STALE_PROFILE"

# (5) Doctor is a mutable role too.
run_preflight 78 "doctor + unprovisioned profile -> exit 78" \
    LOOM_ROLE=doctor CODEX_HOME="$BARE_PROFILE"
run_preflight 78 "pr-fixer (doctor alias) -> exit 78" \
    LOOM_ROLE=pr-fixer CODEX_HOME="$BARE_PROFILE"
run_preflight 78 "development-worker (builder alias) -> exit 78" \
    LOOM_ROLE=development-worker CODEX_HOME="$BARE_PROFILE"
run_preflight 78 "BUILDER (case-insensitive) -> exit 78" \
    LOOM_ROLE=BUILDER CODEX_HOME="$BARE_PROFILE"
# Issue #4768: a daemon-dispatched sweep child is admitted against Builder's
# requirements and gets LOOM_ROLE=sweep-lifecycle (never a bare "builder") —
# it must get the SAME mutable-role fail-closed treatment, not the read-only
# fallback an unrecognized role would silently take.
run_preflight 78 "sweep-lifecycle (daemon sweep-child alias) -> exit 78" \
    LOOM_ROLE=sweep-lifecycle CODEX_HOME="$BARE_PROFILE"

# (6) Read-only roles keep the conservative fallback, with an explicit warning.
run_preflight 0 "judge + unprovisioned profile -> proceeds (read-only role)" \
    LOOM_ROLE=judge CODEX_HOME="$BARE_PROFILE"
out="$PREFLIGHT_OUT"
assert_contains "hook parity unavailable" "$out" \
    "a read-only role is told, explicitly, that hook parity is unavailable"
assert_contains "NOT Builder-capable" "$out" \
    "a read-only fallback session is never reported as Builder-capable"
assert_contains "would-exec" "$out" "a read-only role still assembles the codex argv"

run_preflight 0 "no LOOM_ROLE + unprovisioned profile -> proceeds" \
    CODEX_HOME="$BARE_PROFILE"
out="$PREFLIGHT_OUT"
assert_contains "role=unset" "$out" "the audit line reports an unset role"

# (7) Ambient auth (no CODEX_HOME) is reported as unavailable, not ready.
out="$(env -u CODEX_HOME -u LOOM_CODEX_HOME -u LOOM_CODEX_PROFILE \
    LOOM_SWEEP_NICE=0 LOOM_CODEX_NO_EXEC=1 LOOM_SPAWN_NO_EXPORT=1 \
    LOOM_ROLE=judge bash "$SPAWN_CODEX" -p "hi" 2>&1)" || true
assert_contains "hooks=unavailable" "$out" \
    "ambient Codex login state reports hooks=unavailable"

# (8) The adapter must never pass Codex's hook-trust bypass flag.
TESTS_RUN=$((TESTS_RUN + 1))
if grep -nE '^[^#]*--dangerously-bypass-hook-trust' "$SPAWN_CODEX" \
    | grep -vqE 'log_(error|warn|info)'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: spawn-codex.sh must never pass --dangerously-bypass-hook-trust"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: spawn-codex.sh never passes --dangerously-bypass-hook-trust"
fi

# ...and the config-key spelling of the same waiver (`-c bypass_hook_trust=true`),
# which the 0.146.0 binary honors identically.
TESTS_RUN=$((TESTS_RUN + 1))
if grep -nE 'bypass_hook_trust' "$SPAWN_CODEX" | grep -vqE '#|log_(error|warn|info)'; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: spawn-codex.sh sets the bypass_hook_trust config key"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: spawn-codex.sh never sets the bypass_hook_trust config key"
fi

# (9) Capability truth stays gated: the manifest must remain `partial` until the
#     #4495 evidence gate is satisfied, so #4494's admission keeps rejecting
#     Builder/Doctor + Codex.
CODEX_MANIFEST="$SCRIPTS_DIR/../runtimes/codex.json"
assert_eq "partial" "$(jq -r '.capabilities.worktreeIsolation' "$CODEX_MANIFEST")" \
    "codex.json worktreeIsolation is still 'partial' (evidence-gated, #4478/#4495)"
assert_eq "partial" "$(jq -r '.capabilities.hooks' "$CODEX_MANIFEST")" \
    "codex.json hooks is still 'partial' (evidence-gated, #4478/#4495)"
assert_eq "no" "$(jq -r '.capabilities.subagents' "$CODEX_MANIFEST")" \
    "codex.json subagents is unchanged by this phase"
assert_eq "yes" "$(jq -r '.capabilities.mcp' "$CODEX_MANIFEST")" \
    "codex.json mcp is unchanged by this phase"
TESTS_RUN=$((TESTS_RUN + 1))
if jq -e '.capabilityGate.pending | type == "array" and length > 0' "$CODEX_MANIFEST" >/dev/null 2>&1; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: codex.json records the outstanding promotion evidence"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: codex.json records the outstanding promotion evidence"
fi

# (10) Doctor declares the isolation/mutation requirements its real work needs,
#      so capability admission rejects Doctor + Codex while the manifest is
#      partial and admits it only once the capability is proven.
DOCTOR_ROLE="$SCRIPTS_DIR/../roles/doctor.json"
TESTS_RUN=$((TESTS_RUN + 1))
if jq -e '.runtimeRequirements | index("worktreeIsolation") != null' "$DOCTOR_ROLE" >/dev/null 2>&1; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: doctor.json declares worktreeIsolation"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: doctor.json declares worktreeIsolation"
fi

set +e
bash "$SCRIPTS_DIR/check-runtime-capabilities.sh" --role doctor --runtime codex \
    --dir "$SCRIPTS_DIR/.." >/dev/null 2>&1
doctor_codex_rc=$?
set -e
assert_eq "78" "$doctor_codex_rc" "doctor + codex is rejected while the manifest is partial (#4494)"

# The same checker admits doctor + claude, so the new requirement is not a
# blanket rejection.
set +e
bash "$SCRIPTS_DIR/check-runtime-capabilities.sh" --role doctor --runtime claude \
    --dir "$SCRIPTS_DIR/.." >/dev/null 2>&1
doctor_claude_rc=$?
set -e
assert_eq "0" "$doctor_claude_rc" "doctor + claude remains admitted"

# ...and with a hypothetical PROMOTED codex manifest, doctor is admitted — the
# inverse assertion that proves the gate is the manifest value, not a hard-coded
# runtime denylist.
PROMOTED_DIR="$HOOKTMP/promoted"
mkdir -p "$PROMOTED_DIR/runtimes" "$PROMOTED_DIR/roles"
jq '.capabilities.worktreeIsolation = "yes" | .capabilities.hooks = "yes"' \
    "$CODEX_MANIFEST" > "$PROMOTED_DIR/runtimes/codex.json"
cp "$DOCTOR_ROLE" "$PROMOTED_DIR/roles/doctor.json"
set +e
bash "$SCRIPTS_DIR/check-runtime-capabilities.sh" --role doctor --runtime codex \
    --dir "$PROMOTED_DIR" >/dev/null 2>&1
promoted_rc=$?
set -e
assert_eq "0" "$promoted_rc" "doctor + codex is admitted once the manifest declares the capability"

rm -rf "$HOOKTMP"

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
