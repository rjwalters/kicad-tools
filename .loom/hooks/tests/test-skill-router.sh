#!/usr/bin/env bash
# Test suite for defaults/hooks/skill-router.sh (issue #3609)
#
# Usage: ./defaults/hooks/tests/test-skill-router.sh
#
# Covers the #3609 rework of the UserPromptSubmit routing hook:
#   - non-matching / short / slash prompts emit NO additionalContext
#   - genuine imperative prompts still emit an AGENT_ROUTE suggestion
#   - the agent table is deduped per session (via session_id), appearing at
#     most once; a missing session_id degrades gracefully
#   - the two report example mis-routes ("...builders...", "open an issue
#     with rjwalters/loom") no longer route
#   - the hook never exits non-zero and never emits invalid JSON
#
# The hook + routing config under test are the canonical sources at
# defaults/ (the version-controlled source of truth), copied into an isolated
# temp tree so the hook's MAIN_ROOT resolves there (git-common-dir fails
# outside a repo, so the BASH_SOURCE fallback locates our temp root).
# Exit code 0 = all tests pass, 1 = failures detected.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SRC_HOOK="$REPO_ROOT/defaults/hooks/skill-router.sh"
SRC_CONFIG="$REPO_ROOT/defaults/config/skill-routes.json"

PASS=0
FAIL=0
TOTAL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# Isolated tree so the hook reads OUR config and writes OUR session markers.
# It must be a git repo: the hook resolves MAIN_ROOT via `git rev-parse
# --git-common-dir`, so an isolated repo root pins MAIN_ROOT to the temp tree.
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
git init -q "$TMPROOT"
mkdir -p "$TMPROOT/defaults/hooks" "$TMPROOT/.loom/config"
cp "$SRC_HOOK" "$TMPROOT/defaults/hooks/skill-router.sh"
chmod +x "$TMPROOT/defaults/hooks/skill-router.sh"
cp "$SRC_CONFIG" "$TMPROOT/.loom/config/skill-routes.json"
HOOK="$TMPROOT/defaults/hooks/skill-router.sh"

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

# Run the hook from OUTSIDE any git repo so git-common-dir fails and MAIN_ROOT
# falls back to the temp tree. Echoes stdout; asserts exit 0 (never non-zero).
run_hook() {
    local prompt="$1"
    local session="${2:-}"
    local output exit_code=0
    output=$(cd "$TMPROOT" && make_input "$prompt" "$session" | "$HOOK" 2>/dev/null) || exit_code=$?
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

# Reset any per-session markers between independent cases.
reset_markers() { rm -rf "$TMPROOT/.loom/logs/skill-router-seen" 2>/dev/null || true; }

echo "=== skill-router.sh tests (#3609) ==="

# --- No-match / short / slash prompts emit nothing --------------------------
reset_markers
out=$(run_hook "the weather is quite nice today")
assert_no_output "non-matching prompt -> no additionalContext" "$out"

out=$(run_hook "/builder go implement the feature now")
assert_no_output "slash-command prompt -> no additionalContext" "$out"

out=$(run_hook "hi there")
assert_no_output "short prompt (<3 words) -> no additionalContext" "$out"

out=$(run_hook "help")
assert_no_output "single-word prompt -> no additionalContext" "$out"

# --- Genuine imperative matches still route ---------------------------------
reset_markers
out=$(run_hook "please implement the new feature")
ctx=$(context_of "$out")
assert_contains "builder imperative -> AGENT_ROUTE present" "$ctx" "AGENT_ROUTE:"
assert_contains "builder imperative -> routes to /loom:builder" "$ctx" "/loom:builder"

reset_markers
out=$(run_hook "review this PR")
ctx=$(context_of "$out")
assert_contains "'review this PR' -> routes to /loom:judge" "$ctx" "/loom:judge"

reset_markers
out=$(run_hook "file an issue for the flaky test")
ctx=$(context_of "$out")
assert_contains "'file an issue for X' -> routes to /loom:curator" "$ctx" "/loom:curator"

# --- Report example mis-routes no longer route ------------------------------
reset_markers
out=$(run_hook "confirm our builders are still active in the pool")
ctx=$(context_of "$out")
assert_no_output "report example 'builders...' -> no route" "$out"

reset_markers
out=$(run_hook "please open an issue with rjwalters/loom about this")
assert_no_output "report example 'open an issue with rjwalters/loom' -> no route" "$out"

# --- Per-session dedup of the agent table -----------------------------------
reset_markers
out1=$(run_hook "please implement the new feature" "session-abc")
ctx1=$(context_of "$out1")
assert_contains "first match in session -> includes agent table" "$ctx1" "Available Loom agents:"

out2=$(run_hook "please refactor this dead code" "session-abc")
ctx2=$(context_of "$out2")
assert_contains "second match in session -> AGENT_ROUTE still present" "$ctx2" "AGENT_ROUTE:"
assert_not_contains "second match in session -> table deduped away" "$ctx2" "Available Loom agents:"

# A different session gets its own fresh table.
out3=$(run_hook "please implement the new feature" "session-xyz")
ctx3=$(context_of "$out3")
assert_contains "new session -> table included again" "$ctx3" "Available Loom agents:"

# --- Missing session_id degrades gracefully (table each match, no crash) ----
reset_markers
outA=$(run_hook "please implement the new feature")
ctxA=$(context_of "$outA")
assert_contains "no session_id -> AGENT_ROUTE present" "$ctxA" "AGENT_ROUTE:"
assert_contains "no session_id -> table included (no dedup possible)" "$ctxA" "Available Loom agents:"
outB=$(run_hook "please implement the new feature")
ctxB=$(context_of "$outB")
assert_contains "no session_id -> table included again (graceful)" "$ctxB" "Available Loom agents:"

# --- Session-marker dir pruning (#3793) -------------------------------------
# Stale markers (>7d) are pruned opportunistically on hook entry; fresh markers
# survive. The prune runs inside the session-dedup branch (a route matched and
# session_id is supplied).
reset_markers
SEEN_DIR_S="$TMPROOT/.loom/logs/skill-router-seen"
mkdir -p "$SEEN_DIR_S"
touch -t 202001010000 "$SEEN_DIR_S/stale-old-session"   # ~2020 -> older than 7d
touch "$SEEN_DIR_S/fresh-session"                        # now -> within 7d
run_hook "please implement the new feature" "sess-prune-s" >/dev/null
if [[ ! -e "$SEEN_DIR_S/stale-old-session" ]]; then
    pass "skill-router: stale (>7d) marker pruned on hook entry"
else
    fail "skill-router: stale (>7d) marker pruned on hook entry"
fi
if [[ -e "$SEEN_DIR_S/fresh-session" ]]; then
    pass "skill-router: fresh marker survives prune"
else
    fail "skill-router: fresh marker survives prune"
fi

# --- Never non-zero / never invalid JSON ------------------------------------
for probe in "the weather is quite nice today" "please implement the new feature" "review this PR" "/builder x y z"; do
    out=$(run_hook "$probe" "sess-probe")
    if [[ "$out" == __NONZERO_EXIT__* ]]; then
        fail "hook exits 0 for: $probe"
    elif [[ "$out" == "__INVALID_JSON__" ]]; then
        fail "hook emits valid JSON for: $probe"
    else
        pass "hook exit 0 + valid JSON for: $probe"
    fi
done

# --- Config hygiene ---------------------------------------------------------
if jq empty "$SRC_CONFIG" 2>/dev/null; then
    pass "defaults config is valid JSON"
else
    fail "defaults config is valid JSON"
fi

if grep -q "shepherd" "$SRC_CONFIG"; then
    fail "dead shepherd route removed from defaults config"
else
    pass "dead shepherd route removed from defaults config"
fi

if grep -Eq '"/(shepherd|architect|judge|doctor|hermit|builder|curator|guide|auditor|loom)"' "$SRC_CONFIG"; then
    fail "no un-namespaced /<role> agents in defaults config"
else
    pass "all agents are namespaced /loom:<role> in defaults config"
fi

# --- defaults/ vs .loom/ sync (both hook and config) ------------------------
DEPLOY_HOOK="$REPO_ROOT/.loom/hooks/skill-router.sh"
DEPLOY_CONFIG="$REPO_ROOT/.loom/config/skill-routes.json"
if [[ -f "$DEPLOY_HOOK" ]] && diff -q "$SRC_HOOK" "$DEPLOY_HOOK" >/dev/null 2>&1; then
    pass ".loom/ hook byte-identical to defaults/"
else
    fail ".loom/ hook byte-identical to defaults/"
fi
if [[ -f "$DEPLOY_CONFIG" ]] && diff -q "$SRC_CONFIG" "$DEPLOY_CONFIG" >/dev/null 2>&1; then
    pass ".loom/ config byte-identical to defaults/"
else
    fail ".loom/ config byte-identical to defaults/"
fi

echo "=== $PASS/$TOTAL passed ==="
[[ "$FAIL" -eq 0 ]]
