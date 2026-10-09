#!/usr/bin/env bash
# Test suite for the `loom-daemon workspace` guard in
# defaults/hooks/guard-loom-workflow.sh (issue #4326)
#
# Usage: ./defaults/hooks/tests/test-guard-loom-workspace.sh
#
# Covers the #4326 guard: `loom-daemon workspace add|remove|set-priority`
# mutate the machine-level registry (normally the operator's real
# ~/.loom/workspaces.json), so the guard ASKS for confirmation unless
# LOOM_WORKSPACES_PATH (the sanctioned scratch-registry test seam,
# loom-daemon/src/workspace_registry.rs) is in play — either already set in
# the environment, or assigned inline on the same command line.
#
#   - `loom-daemon workspace add <path>`    -> ask, no LOOM_WORKSPACES_PATH
#   - `loom-daemon workspace remove <path>` -> ask, no LOOM_WORKSPACES_PATH
#   - `loom-daemon workspace set-priority <path> <n>` -> ask, no env
#   - LOOM_WORKSPACES_PATH set in the ambient environment -> allow
#   - LOOM_WORKSPACES_PATH=... assigned inline on the command line -> allow
#   - `loom-daemon workspace list` -> ALWAYS allow (read-only, never matched)
#   - an unrelated command mentioning "workspace" -> allow
#   - a path-qualified invocation (e.g. .loom/bin/loom-daemon) still matches
#   - guards.workspaceRegistry / LOOM_GUARD_WORKSPACE_REGISTRY toggle (env
#     beats config; config beats default-on) — independent of
#     LOOM_WORKSPACES_PATH
#   - contract: exit is always 0; ask output is well-formed JSON with
#     permissionDecision == "ask"
#
# The hook under test is the canonical source at defaults/ (the version-
# controlled source of truth), copied into an isolated temp git tree so the
# hook's REPO_ROOT/HOOK_ERROR_LOG resolve there. Exit 0 = all pass, 1 = fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# Prefer the installed hook/library (a Loom-installed consumer repo has no
# defaults/ directory at all); fall back to defaults/ for Loom's own source
# tree. See issue #6496. DEFAULTS_HOOK stays strictly the defaults/ path --
# it is the source-of-truth side of the "defaults/ vs .loom/ sync" check
# below, which must remain a real cross-copy diff, not a self-diff.
SRC_HOOK="$REPO_ROOT/.loom/hooks/guard-loom-workflow.sh"
[[ -r "$SRC_HOOK" ]] || SRC_HOOK="$REPO_ROOT/defaults/hooks/guard-loom-workflow.sh"
DEFAULTS_HOOK="$REPO_ROOT/defaults/hooks/guard-loom-workflow.sh"
CONFIG_RESOLVER="$REPO_ROOT/.loom/scripts/lib/config-resolver.sh"
[[ -r "$CONFIG_RESOLVER" ]] || CONFIG_RESOLVER="$REPO_ROOT/defaults/scripts/lib/config-resolver.sh"

PASS=0
FAIL=0
TOTAL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
git init -q "$TMPROOT"
mkdir -p "$TMPROOT/.loom/hooks" "$TMPROOT/.loom/scripts/lib"
cp "$SRC_HOOK" "$TMPROOT/.loom/hooks/guard-loom-workflow.sh"
chmod +x "$TMPROOT/.loom/hooks/guard-loom-workflow.sh"
# The hook sources ../scripts/lib/config-resolver.sh — stage the real
# resolver at the equivalent installed-layout path (mirrors
# test-guard-worktree-paths.sh) so decision_log_enabled() exercises the real
# tiered resolution rather than silently no-op'ing.
cp "$CONFIG_RESOLVER" "$TMPROOT/.loom/scripts/lib/config-resolver.sh"
HOOK="$TMPROOT/.loom/hooks/guard-loom-workflow.sh"

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); printf "${GREEN}PASS${NC} %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); printf "${RED}FAIL${NC} %s\n" "$1"; }

make_input() {
    local cmd="$1"
    jq -n --arg cmd "$cmd" --arg cwd "$TMPROOT" '{tool_input: {command: $cmd}, cwd: $cwd}'
}

# Prints "<exit_code>|<stdout>". Optional extra args are exported env
# assignments applied only for this invocation (e.g. LOOM_WORKSPACES_PATH=...).
run_hook() {
    local cmd="$1"
    shift
    local exit_code=0 output
    output=$(cd "$TMPROOT" && env "$@" bash "$HOOK" < <(make_input "$cmd") 2>/dev/null) || exit_code=$?
    printf '%s|%s' "$exit_code" "$output"
}

assert_allow() {
    local desc="$1" result="$2"
    local code="${result%%|*}" out="${result#*|}"
    if [[ "$code" == "0" && -z "$out" ]]; then
        pass "$desc"
    else
        fail "$desc (expected exit 0 + empty output, got exit=$code output=$out)"
    fi
}

assert_ask() {
    local desc="$1" result="$2"
    local code="${result%%|*}" out="${result#*|}"
    if [[ "$code" != "0" ]]; then
        fail "$desc (expected exit 0 with ask JSON, got NONZERO exit=$code)"
        return
    fi
    local decision
    decision=$(echo "$out" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null || true)
    if [[ "$decision" == "ask" ]]; then
        pass "$desc"
    else
        fail "$desc (expected permissionDecision=ask, got: $out)"
    fi
}

echo "=== guard-loom-workflow.sh: loom-daemon workspace guard tests (#4326) ==="

# --- mutating subcommands without LOOM_WORKSPACES_PATH -> ask --------------
result=$(run_hook "loom-daemon workspace add /tmp/mig-test")
assert_ask "(a) 'workspace add' with no LOOM_WORKSPACES_PATH -> ask" "$result"

result=$(run_hook "loom-daemon workspace remove /tmp/mig-test")
assert_ask "(b) 'workspace remove' with no LOOM_WORKSPACES_PATH -> ask" "$result"

result=$(run_hook "loom-daemon workspace set-priority /tmp/mig-test 3")
assert_ask "(c) 'workspace set-priority' with no LOOM_WORKSPACES_PATH -> ask" "$result"

# --- LOOM_WORKSPACES_PATH already in the environment -> allow --------------
result=$(run_hook "loom-daemon workspace add /tmp/mig-test" "LOOM_WORKSPACES_PATH=$TMPROOT/scratch.json")
assert_allow "(d) ambient LOOM_WORKSPACES_PATH set -> allow" "$result"

# --- LOOM_WORKSPACES_PATH assigned inline on the command line -> allow -----
result=$(run_hook "LOOM_WORKSPACES_PATH=$TMPROOT/scratch.json loom-daemon workspace add /tmp/mig-test")
assert_allow "(e) inline LOOM_WORKSPACES_PATH= on the command line -> allow" "$result"

# --- workspace list is read-only and never guarded --------------------------
result=$(run_hook "loom-daemon workspace list")
assert_allow "(f) 'workspace list' always allowed (read-only)" "$result"

result=$(run_hook "loom-daemon workspace list --json")
assert_allow "(g) 'workspace list --json' always allowed" "$result"

# --- an unrelated command mentioning "workspace" is unaffected --------------
result=$(run_hook "echo 'workspace add is a loom-daemon subcommand'")
assert_allow "(h) unrelated command mentioning the words -> allow" "$result"

# --- a path-qualified loom-daemon invocation still matches ------------------
result=$(run_hook "./.loom/bin/loom-daemon workspace add /tmp/mig-test")
assert_ask "(i) path-qualified loom-daemon still triggers the ask" "$result"

# --- ask reason references LOOM_WORKSPACES_PATH and the real registry ------
raw=$(run_hook "loom-daemon workspace add /tmp/mig-test")
out="${raw#*|}"
reason=$(echo "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // empty' 2>/dev/null || true)
if [[ "$reason" == *"LOOM_WORKSPACES_PATH"* && "$reason" == *"registry"* ]]; then
    pass "ask reason names LOOM_WORKSPACES_PATH and the registry"
else
    fail "ask reason names LOOM_WORKSPACES_PATH and the registry (got: $reason)"
fi

# --- Guard toggle: guards.workspaceRegistry / LOOM_GUARD_WORKSPACE_REGISTRY -
cat > "$TMPROOT/.loom/config.json" <<'EOF'
{"guards": {"workspaceRegistry": false}}
EOF
result=$(run_hook "loom-daemon workspace add /tmp/mig-test")
assert_allow "toggle: guards.workspaceRegistry=false -> allow (no ask)" "$result"

result=$(run_hook "loom-daemon workspace add /tmp/mig-test" "LOOM_GUARD_WORKSPACE_REGISTRY=1")
assert_ask "toggle: env LOOM_GUARD_WORKSPACE_REGISTRY=1 overrides config false -> ask" "$result"

rm -f "$TMPROOT/.loom/config.json"
result=$(run_hook "loom-daemon workspace add /tmp/mig-test" "LOOM_GUARD_WORKSPACE_REGISTRY=0")
assert_allow "toggle: env LOOM_GUARD_WORKSPACE_REGISTRY=0 -> allow" "$result"

result=$(run_hook "loom-daemon workspace add /tmp/mig-test")
assert_ask "toggle: default (no config, no env) is ON -> ask" "$result"

# The toggle is independent of LOOM_WORKSPACES_PATH: even with the guard
# force-enabled, the sanctioned test seam still allows the command through.
result=$(run_hook "loom-daemon workspace add /tmp/mig-test" "LOOM_GUARD_WORKSPACE_REGISTRY=1" "LOOM_WORKSPACES_PATH=$TMPROOT/scratch.json")
assert_allow "toggle forced ON + LOOM_WORKSPACES_PATH set -> still allow" "$result"

# --- Contract checks: exit is always 0, ask emits well-formed JSON ---------
for r in \
    "$(run_hook "loom-daemon workspace add /tmp/mig-test")" \
    "$(run_hook "loom-daemon workspace list")" \
    "$(run_hook "")" \
    ; do
    code="${r%%|*}"
    if [[ "$code" == "0" ]]; then
        pass "contract: exit 0 (case: ${r:0:40})"
    else
        fail "contract: exit 0 (case: ${r:0:40}, got exit=$code)"
    fi
done

raw=$(run_hook "loom-daemon workspace add /tmp/mig-test")
out="${raw#*|}"
if echo "$out" | jq empty 2>/dev/null; then
    pass "contract: ask output is valid JSON"
else
    fail "contract: ask output is valid JSON (got: $out)"
fi

# --- jq absent -> allow (fail-open) -----------------------------------------
NOJQ_DIR="$(mktemp -d)"
for b in bash cat mkdir echo date sed dirname basename find python3 grep git env; do
    p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$NOJQ_DIR/$b"
done
out=$(cd "$TMPROOT" && make_input "loom-daemon workspace add /tmp/mig-test" | PATH="$NOJQ_DIR" bash "$HOOK" 2>/dev/null)
code=$?
if [[ "$code" == "0" && -z "$out" ]]; then
    pass "jq absent -> allow (fail-open)"
else
    fail "jq absent -> allow (fail-open) (got exit=$code output=$out)"
fi
rm -rf "$NOJQ_DIR"

# --- defaults/ vs .loom/ sync ------------------------------------------------
DEPLOY_HOOK="$REPO_ROOT/.loom/hooks/guard-loom-workflow.sh"
if [[ ! -f "$DEFAULTS_HOOK" ]]; then
    echo "SKIP: defaults/hooks/guard-loom-workflow.sh not present (bare consumer layout) -- sync check not applicable"
elif [[ -f "$DEPLOY_HOOK" ]] && diff -q "$DEFAULTS_HOOK" "$DEPLOY_HOOK" >/dev/null 2>&1; then
    pass ".loom/ hook byte-identical to defaults/"
else
    fail ".loom/ hook byte-identical to defaults/"
fi

echo "=== $PASS/$TOTAL passed ==="
[[ "$FAIL" -eq 0 ]]
