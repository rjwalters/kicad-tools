#!/usr/bin/env bash
# Test suite for defaults/hooks/methodology-inject.sh (issue #3758)
#
# Usage: ./defaults/hooks/tests/test-methodology-inject.sh
#
# Covers the #3758 rework of the UserPromptSubmit methodology-injection hook:
#   - opt-in gate: .loom/context/ absent -> silent exit 0, no output
#   - universal.md is injected ONCE PER SESSION by default (new default),
#     deduped via the session_id present on stdin (own marker namespace)
#   - universal_frequency: "always" restores the legacy per-prompt injection
#   - a missing/empty session_id degrades gracefully (inject every turn)
#   - role and topic injection are UNCHANGED (still fire every matching turn)
#   - the hook never exits non-zero and never emits invalid JSON
#
# The hook under test is the canonical source at defaults/ (the version-
# controlled source of truth), copied into an isolated temp git tree so the
# hook's MAIN_ROOT resolves there (git-common-dir pins MAIN_ROOT to the temp
# root, and .loom/logs/ markers are written there). Exit 0 = all pass, 1 = fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SRC_HOOK="$REPO_ROOT/defaults/hooks/methodology-inject.sh"

PASS=0
FAIL=0
TOTAL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# Isolated tree so the hook reads OUR context/ and writes OUR session markers.
# It must be a git repo: the hook resolves MAIN_ROOT via `git rev-parse
# --git-common-dir`, so an isolated repo root pins MAIN_ROOT to the temp tree.
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
git init -q "$TMPROOT"
mkdir -p "$TMPROOT/defaults/hooks"
cp "$SRC_HOOK" "$TMPROOT/defaults/hooks/methodology-inject.sh"
chmod +x "$TMPROOT/defaults/hooks/methodology-inject.sh"
HOOK="$TMPROOT/defaults/hooks/methodology-inject.sh"

CONTEXT_DIR="$TMPROOT/.loom/context"

# Build stdin JSON. Second arg (session_id) is optional.
make_input() {
    local prompt="$1"
    local session="${2:-}"
    if [[ -n "$session" ]]; then
        jq -n --arg p "$prompt" --arg s "$session" '{prompt: $p, session_id: $s}'
    else
        jq -n --arg p "$prompt" '{prompt: $p}'
    fi
}

# Run the hook from inside the temp tree so git-common-dir resolves MAIN_ROOT to
# it. Echoes stdout; asserts exit 0 (never non-zero) and valid JSON.
# Optional third arg sets LOOM_ROLE for the invocation.
run_hook() {
    local prompt="$1"
    local session="${2:-}"
    local role="${3:-}"
    local output exit_code=0
    if [[ -n "$role" ]]; then
        output=$(cd "$TMPROOT" && make_input "$prompt" "$session" | LOOM_ROLE="$role" "$HOOK" 2>/dev/null) || exit_code=$?
    else
        output=$(cd "$TMPROOT" && make_input "$prompt" "$session" | "$HOOK" 2>/dev/null) || exit_code=$?
    fi
    if [[ "$exit_code" -ne 0 ]]; then
        echo "__NONZERO_EXIT__:$exit_code"
        return 0
    fi
    # Any non-empty output must be valid JSON.
    if [[ -n "$output" ]] && ! echo "$output" | jq empty 2>/dev/null; then
        echo "__INVALID_JSON__"
        return 0
    fi
    echo "$output"
}

# Extract the additionalContext string from hook output ("" if none).
context_of() {
    echo "$1" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null || true
}

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); printf "${GREEN}PASS${NC} %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); printf "${RED}FAIL${NC} %s\n" "$1"; }

assert_no_output() {
    local desc="$1" out="$2"
    if [[ -z "$out" ]]; then
        pass "$desc"
    else
        fail "$desc (expected empty output, got: $out)"
    fi
}

assert_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$desc"
    else
        fail "$desc (expected to contain '$needle', got: $haystack)"
    fi
}

assert_not_contains() {
    local desc="$1" haystack="$2" needle="$3"
    if [[ "$haystack" != *"$needle"* ]]; then
        pass "$desc"
    else
        fail "$desc (expected NOT to contain '$needle', got: $haystack)"
    fi
}

# Reset per-session markers and rebuild the context dir between cases.
reset_markers() { rm -rf "$TMPROOT/.loom/logs/methodology-inject-seen" 2>/dev/null || true; }

setup_context() {
    rm -rf "$CONTEXT_DIR"
    mkdir -p "$CONTEXT_DIR/roles" "$CONTEXT_DIR/topics"
    printf '# Universal Project Rules\nMARKER_UNIVERSAL\n' > "$CONTEXT_DIR/universal.md"
    printf '# Builder Role\nMARKER_ROLE_BUILDER\n' > "$CONTEXT_DIR/roles/builder.md"
    printf '# Security Topic\nMARKER_TOPIC_SECURITY\n' > "$CONTEXT_DIR/topics/security.md"
}

echo "=== methodology-inject.sh tests (#3758) ==="

# --- Opt-in gate: no .loom/context/ -> silent exit 0 ------------------------
rm -rf "$CONTEXT_DIR"
reset_markers
out=$(run_hook "please do something useful here" "sess-gate")
assert_no_output "no .loom/context/ -> no additionalContext (opt-in gate)" "$out"

# --- Default (no config.json): universal.md once per session ----------------
setup_context
reset_markers
out1=$(run_hook "just a normal prompt here" "sess-1")
ctx1=$(context_of "$out1")
assert_contains "default: first prompt in session -> universal injected" "$ctx1" "MARKER_UNIVERSAL"

out2=$(run_hook "another normal prompt here" "sess-1")
ctx2=$(context_of "$out2")
assert_not_contains "default: second prompt same session -> universal deduped" "$ctx2" "MARKER_UNIVERSAL"

# A different session gets its own fresh universal injection.
out3=$(run_hook "a fresh session prompt here" "sess-2")
ctx3=$(context_of "$out3")
assert_contains "default: new session -> universal injected again" "$ctx3" "MARKER_UNIVERSAL"

# --- universal_frequency: "always" restores per-prompt injection ------------
setup_context
printf '{ "universal_frequency": "always" }\n' > "$CONTEXT_DIR/config.json"
reset_markers
outA=$(run_hook "always mode prompt one here" "sess-always")
ctxA=$(context_of "$outA")
assert_contains "always: first prompt -> universal injected" "$ctxA" "MARKER_UNIVERSAL"
outB=$(run_hook "always mode prompt two here" "sess-always")
ctxB=$(context_of "$outB")
assert_contains "always: second prompt same session -> universal STILL injected" "$ctxB" "MARKER_UNIVERSAL"
rm -f "$CONTEXT_DIR/config.json"

# --- Missing/empty session_id degrades gracefully (inject every turn) -------
setup_context
reset_markers
outN1=$(run_hook "no session id prompt one here")
ctxN1=$(context_of "$outN1")
assert_contains "no session_id -> universal injected (turn 1)" "$ctxN1" "MARKER_UNIVERSAL"
outN2=$(run_hook "no session id prompt two here")
ctxN2=$(context_of "$outN2")
assert_contains "no session_id -> universal injected again (graceful degrade)" "$ctxN2" "MARKER_UNIVERSAL"

# --- Role/topic injection UNCHANGED (still fire every matching turn) ---------
setup_context
reset_markers
# Role fires every turn even after universal is deduped for the session.
outR1=$(run_hook "implement the widget please here" "sess-role" "builder")
ctxR1=$(context_of "$outR1")
assert_contains "role: injected on first turn (LOOM_ROLE=builder)" "$ctxR1" "MARKER_ROLE_BUILDER"
outR2=$(run_hook "implement the gadget please here" "sess-role" "builder")
ctxR2=$(context_of "$outR2")
assert_contains "role: STILL injected on second turn (unchanged by #3758)" "$ctxR2" "MARKER_ROLE_BUILDER"
assert_not_contains "role: universal deduped on second turn but role remains" "$ctxR2" "MARKER_UNIVERSAL"

# Topic fires every turn it matches, independent of universal dedup.
setup_context
reset_markers
outT1=$(run_hook "please review the security model here" "sess-topic")
ctxT1=$(context_of "$outT1")
assert_contains "topic: injected on first matching turn" "$ctxT1" "MARKER_TOPIC_SECURITY"
outT2=$(run_hook "more on the security posture here" "sess-topic")
ctxT2=$(context_of "$outT2")
assert_contains "topic: STILL injected on second matching turn (unchanged)" "$ctxT2" "MARKER_TOPIC_SECURITY"
assert_not_contains "topic: universal deduped on second turn but topic remains" "$ctxT2" "MARKER_UNIVERSAL"

# --- inject_universal:false still fully suppresses universal -----------------
setup_context
printf '{ "inject_universal": false }\n' > "$CONTEXT_DIR/config.json"
reset_markers
outF=$(run_hook "universal disabled prompt here" "sess-off")
ctxF=$(context_of "$outF")
assert_not_contains "inject_universal:false -> universal never injected" "$ctxF" "MARKER_UNIVERSAL"
rm -f "$CONTEXT_DIR/config.json"

# --- Malformed config.json falls through to session default, no crash -------
setup_context
printf '{ this is not valid json ' > "$CONTEXT_DIR/config.json"
reset_markers
outM=$(run_hook "malformed config prompt here" "sess-malformed")
if [[ "$outM" == __NONZERO_EXIT__* || "$outM" == "__INVALID_JSON__" ]]; then
    fail "malformed config -> hook stays exit 0 + valid JSON"
else
    pass "malformed config -> hook stays exit 0 + valid JSON"
fi
rm -f "$CONTEXT_DIR/config.json"

# --- Never non-zero / never invalid JSON ------------------------------------
setup_context
reset_markers
for probe in "the weather is nice today here" "please implement the feature now" "review the security model here"; do
    out=$(run_hook "$probe" "sess-probe")
    if [[ "$out" == __NONZERO_EXIT__* ]]; then
        fail "hook exits 0 for: $probe"
    elif [[ "$out" == "__INVALID_JSON__" ]]; then
        fail "hook emits valid JSON for: $probe"
    else
        pass "hook exit 0 + valid JSON for: $probe"
    fi
done

# --- Namespaced /loom:<role> detection (#3793) ------------------------------
# Role commands live at .claude/commands/loom/<role>.md and Claude Code 2.1+
# requires the namespaced /loom:<role> form for subdirectory commands (#3345).
# The prompt-preamble fallback must set ROLE for BOTH /builder and /loom:builder.
# Detection only fires when LOOM_ROLE is UNSET, so run these with LOOM_ROLE
# explicitly cleared (env -u) so an inherited LOOM_ROLE never masks the fallback.
setup_context
printf '# Judge Role\nMARKER_ROLE_JUDGE\n' > "$CONTEXT_DIR/roles/judge.md"
printf '# Auditor Role\nMARKER_ROLE_AUDITOR\n' > "$CONTEXT_DIR/roles/auditor.md"

run_hook_norole() {
    local prompt="$1" session="${2:-}"
    local output exit_code=0
    output=$(cd "$TMPROOT" && make_input "$prompt" "$session" | env -u LOOM_ROLE "$HOOK" 2>/dev/null) || exit_code=$?
    if [[ "$exit_code" -ne 0 ]]; then echo "__NONZERO_EXIT__:$exit_code"; return 0; fi
    if [[ -n "$output" ]] && ! echo "$output" | jq empty 2>/dev/null; then echo "__INVALID_JSON__"; return 0; fi
    echo "$output"
}

reset_markers
outNS1=$(run_hook_norole "/loom:builder 123" "sess-ns-1")
assert_contains "namespaced /loom:builder -> builder role injected" "$(context_of "$outNS1")" "MARKER_ROLE_BUILDER"

reset_markers
outNS2=$(run_hook_norole "/loom:judge review this pr" "sess-ns-2")
assert_contains "namespaced /loom:judge -> judge role injected" "$(context_of "$outNS2")" "MARKER_ROLE_JUDGE"

reset_markers
outNS3=$(run_hook_norole "/loom:auditor check main branch" "sess-ns-3")
assert_contains "namespaced /loom:auditor -> auditor role injected" "$(context_of "$outNS3")" "MARKER_ROLE_AUDITOR"

# Regression: the pre-existing unnamespaced forms still resolve unchanged.
reset_markers
outUN1=$(run_hook_norole "/builder 123" "sess-un-1")
assert_contains "unnamespaced /builder -> builder role still injected" "$(context_of "$outUN1")" "MARKER_ROLE_BUILDER"

reset_markers
outUN2=$(run_hook_norole "/judge review this pr" "sess-un-2")
assert_contains "unnamespaced /judge -> judge role still injected" "$(context_of "$outUN2")" "MARKER_ROLE_JUDGE"

# --- Session-marker dir pruning (#3793) -------------------------------------
# Stale markers (>7d) are pruned opportunistically on hook entry; fresh markers
# survive. The prune runs inside the session-dedup branch (universal.md present,
# session_id supplied, universal_frequency != always).
setup_context
reset_markers
SEEN_DIR_M="$TMPROOT/.loom/logs/methodology-inject-seen"
mkdir -p "$SEEN_DIR_M"
touch -t 202001010000 "$SEEN_DIR_M/stale-old-session"   # ~2020 -> older than 7d
touch "$SEEN_DIR_M/fresh-session"                        # now -> within 7d
run_hook "trigger a prune pass here now" "sess-prune-m" >/dev/null
if [[ ! -e "$SEEN_DIR_M/stale-old-session" ]]; then
    pass "methodology: stale (>7d) marker pruned on hook entry"
else
    fail "methodology: stale (>7d) marker pruned on hook entry"
fi
if [[ -e "$SEEN_DIR_M/fresh-session" ]]; then
    pass "methodology: fresh marker survives prune"
else
    fail "methodology: fresh marker survives prune"
fi

# --- defaults/ vs .loom/ sync -----------------------------------------------
DEPLOY_HOOK="$REPO_ROOT/.loom/hooks/methodology-inject.sh"
if [[ -f "$DEPLOY_HOOK" ]] && diff -q "$SRC_HOOK" "$DEPLOY_HOOK" >/dev/null 2>&1; then
    pass ".loom/ hook byte-identical to defaults/"
else
    fail ".loom/ hook byte-identical to defaults/"
fi

echo "=== $PASS/$TOTAL passed ==="
[[ "$FAIL" -eq 0 ]]
