#!/usr/bin/env bash
# Test suite for defaults/hooks/guard-worktree-paths.sh (issue #4007)
#
# Usage: ./defaults/hooks/tests/test-guard-worktree-paths.sh
#
# Covers the #4007 rework from an env-only (LOOM_WORKTREE_PATH) worktree
# guard -- structurally inert on the daemon-dispatched sweep path, where
# Task-subagent builders share one process env with no per-subagent env
# injection (#3719) -- to a path-derived guard that works without any
# ambient env var:
#   - target inside a `.loom-managed` worktree -> allow
#   - target at the main repo root while a managed worktree exists -> deny
#   - relative path + cwd at main root, worktree exists elsewhere -> deny
#   - no `.loom-managed` sentinel anywhere -> allow (fail-open)
#   - path outside both the main checkout and any worktree (e.g. /tmp) -> allow
#   - the LOOM_WORKTREE_PATH fast path (tmux/manual sessions) is unchanged
#   - the guards.worktreeIsolation / LOOM_GUARD_WORKTREE_ISOLATION toggle
#     (env beats config; config beats default-on)
#   - fail-open contract: exit is always 0, and any denial emits well-formed
#     hookSpecificOutput JSON
#
# The hook under test is the canonical source at defaults/ (the version-
# controlled source of truth), copied into an isolated temp git tree so the
# hook's MAIN_ROOT/HOOK_ERROR_LOG resolve there (git-common-dir pins
# MAIN_ROOT to the temp root). Exit 0 = all pass, 1 = fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SRC_HOOK="$REPO_ROOT/defaults/hooks/guard-worktree-paths.sh"

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
cp "$SRC_HOOK" "$TMPROOT/.loom/hooks/guard-worktree-paths.sh"
chmod +x "$TMPROOT/.loom/hooks/guard-worktree-paths.sh"
# The hook sources ../scripts/lib/config-resolver.sh (Epic #3835 Phase 5,
# #4262) relative to its own SCRIPT_DIR — stage the real resolver at the
# equivalent installed-layout path so the guards.worktreeIsolation /
# worktree.root reads exercise the actual tiered resolution, not a stub.
cp "$REPO_ROOT/defaults/scripts/lib/config-resolver.sh" "$TMPROOT/.loom/scripts/lib/config-resolver.sh"
# The hook also sources ../scripts/lib/canonical-path.sh (#4495) for
# symlink-aware target canonicalization. Stage it at the installed-layout path
# so the symlink-escape cases below exercise the real resolver rather than the
# lexical fallback.
cp "$REPO_ROOT/defaults/scripts/lib/canonical-path.sh" "$TMPROOT/.loom/scripts/lib/canonical-path.sh"
HOOK="$TMPROOT/.loom/hooks/guard-worktree-paths.sh"

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); printf "${GREEN}PASS${NC} %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); printf "${RED}FAIL${NC} %s\n" "$1"; }

# Build stdin JSON. Optional cwd arg for relative-path resolution.
make_input() {
    local file_path="$1" cwd="${2:-}"
    if [[ -n "$cwd" ]]; then
        jq -n --arg fp "$file_path" --arg cwd "$cwd" '{tool_input: {file_path: $fp}, cwd: $cwd}'
    else
        jq -n --arg fp "$file_path" '{tool_input: {file_path: $fp}}'
    fi
}

# Run the hook from inside the temp tree so git-common-dir resolves MAIN_ROOT
# to it. Prints "<exit_code>|<stdout>". Optional extra args are exported env
# assignments applied only for this invocation (e.g. LOOM_WORKTREE_PATH=...).
run_hook() {
    local file_path="$1" cwd="${2:-}"
    shift; [[ $# -gt 0 ]] && shift
    local exit_code=0 output
    output=$(cd "$TMPROOT" && env "$@" bash "$HOOK" < <(make_input "$file_path" "$cwd") 2>/dev/null) || exit_code=$?
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

assert_deny() {
    local desc="$1" result="$2"
    local code="${result%%|*}" out="${result#*|}"
    if [[ "$code" != "0" ]]; then
        fail "$desc (expected exit 0 with deny JSON, got NONZERO exit=$code)"
        return
    fi
    local decision
    decision=$(echo "$out" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null || true)
    if [[ "$decision" == "deny" ]]; then
        pass "$desc"
    else
        fail "$desc (expected permissionDecision=deny, got: $out)"
    fi
}

echo "=== guard-worktree-paths.sh tests (#4007) ==="

# --- Fixture: one managed worktree at $TMPROOT/.loom/worktrees/issue-1 -----
WT="$TMPROOT/.loom/worktrees/issue-1"
mkdir -p "$WT/src"
cat > "$WT/.loom-managed" <<'EOF'
# Loom-managed worktree marker
EOF

# (a) target inside a `.loom-managed` worktree -> allow
result=$(run_hook "$WT/src/foo.rs")
assert_allow "(a) target inside managed worktree -> allow" "$result"

# (b) target at the main repo root while a managed worktree exists -> deny
result=$(run_hook "$TMPROOT/CLAUDE.md")
assert_deny "(b) target at main root, worktree exists -> deny" "$result"

# (c) relative path + cwd at main root -> deny
result=$(run_hook "CLAUDE.md" "$TMPROOT")
assert_deny "(c) relative path + cwd at main root -> deny" "$result"

# --- deny reason references the worktree isolation contract ----------------
raw=$(run_hook "$TMPROOT/CLAUDE.md")
out="${raw#*|}"
reason=$(echo "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // empty' 2>/dev/null || true)
if [[ "$reason" == *"main repository checkout"* && "$reason" == *"worktree"* ]]; then
    pass "deny reason explains main-checkout + worktree contract"
else
    fail "deny reason explains main-checkout + worktree contract (got: $reason)"
fi

# --- deny reason warns against the Bash-redirection bypass (#4178) ---------
# The reason must explicitly steer away from retrying via Bash (>, tee, sed -i,
# cp/mv) -- that bypass is exactly what sweep #4063 used, and the same target
# is independently confined by guard-destructive-generic.sh's write-
# confinement category. Belt-and-suspenders: this asserts the Edit/Write
# guard's message CARRIES the warning; the Bash-side confinement is covered by
# tests/hooks/test-guard-destructive.sh.
if [[ "$reason" == *"Bash"* ]]; then
    pass "deny reason warns against retrying via Bash redirection (#4178)"
else
    fail "deny reason warns against retrying via Bash redirection (#4178) (got: $reason)"
fi

# --- fail-open: no sentinel anywhere -----------------------------------
rm -rf "$TMPROOT/.loom/worktrees"
result=$(run_hook "$TMPROOT/CLAUDE.md")
assert_allow "(d) no sentinel anywhere -> allow (fail-open)" "$result"

# Restore fixture for remaining tests
mkdir -p "$WT/src"
cat > "$WT/.loom-managed" <<'EOF'
# Loom-managed worktree marker
EOF

# --- path outside both the main checkout and any worktree -> allow ---------
OUTSIDE="$(mktemp -d)"
result=$(run_hook "$OUTSIDE/scratch.txt")
assert_allow "path outside main checkout and worktrees (e.g. /tmp) -> allow" "$result"
rm -rf "$OUTSIDE"

# --- edge case: path under .loom/worktrees/ belonging to a DIFFERENT issue -
# The guard cannot distinguish which issue a given subagent owns (no ambient
# signal exists for that, #3719) -- it only prevents writes from landing in
# the MAIN checkout. A write into another issue's worktree is still "inside
# some managed worktree" and therefore allowed by design.
WT2="$TMPROOT/.loom/worktrees/issue-2"
mkdir -p "$WT2"
cat > "$WT2/.loom-managed" <<'EOF'
# Loom-managed worktree marker
EOF
result=$(run_hook "$WT2/other-issue-file.txt")
assert_allow "cross-issue worktree write is allowed (not the guarded failure mode)" "$result"

# --- LOOM_WORKTREE_PATH fast path (tmux/manual) is unchanged ---------------
result=$(run_hook "$WT/src/foo.rs" "" "LOOM_WORKTREE_PATH=$WT")
assert_allow "fast path: LOOM_WORKTREE_PATH set, target inside -> allow" "$result"

result=$(run_hook "$TMPROOT/CLAUDE.md" "" "LOOM_WORKTREE_PATH=$WT")
assert_deny "fast path: LOOM_WORKTREE_PATH set, target outside -> deny" "$result"

# --- Guard toggle: guards.worktreeIsolation / LOOM_GUARD_WORKTREE_ISOLATION -
cat > "$TMPROOT/.loom/config.json" <<'EOF'
{"guards": {"worktreeIsolation": false}}
EOF
result=$(run_hook "$TMPROOT/CLAUDE.md")
assert_allow "toggle: guards.worktreeIsolation=false -> allow at main root" "$result"

result=$(run_hook "$TMPROOT/CLAUDE.md" "" "LOOM_GUARD_WORKTREE_ISOLATION=1")
assert_deny "toggle: env LOOM_GUARD_WORKTREE_ISOLATION=1 overrides config false -> deny" "$result"

rm -f "$TMPROOT/.loom/config.json"
result=$(run_hook "$TMPROOT/CLAUDE.md" "" "LOOM_GUARD_WORKTREE_ISOLATION=0"  )
assert_allow "toggle: env LOOM_GUARD_WORKTREE_ISOLATION=0 -> allow" "$result"

result=$(run_hook "$TMPROOT/CLAUDE.md")
assert_deny "toggle: default (no config, no env) is ON -> deny" "$result"

# --- Tiered config (Epic #3835 Phase 5, #4262): .loom-project/project.json --
mkdir -p "$TMPROOT/.loom-project"
cat > "$TMPROOT/.loom-project/project.json" <<'EOF'
{"guards": {"worktreeIsolation": false}}
EOF
result=$(run_hook "$TMPROOT/CLAUDE.md")
assert_allow "tiered config: .loom-project/project.json guards.worktreeIsolation=false -> allow" "$result"

# .loom-project (higher tier) overrides a conflicting legacy .loom/config.json
cat > "$TMPROOT/.loom/config.json" <<'EOF'
{"guards": {"worktreeIsolation": true}}
EOF
result=$(run_hook "$TMPROOT/CLAUDE.md")
assert_allow "tiered config: .loom-project overrides conflicting legacy .loom/config.json" "$result"
rm -f "$TMPROOT/.loom/config.json"

# worktree.root also resolves from .loom-project/project.json: point it at an
# alternate base holding the ONLY managed worktree, and confirm a target under
# the (now-legacy) worktrees dir at the main root no longer counts as "inside
# a managed worktree" for this guard.
ALT_BASE="$(mktemp -d)"
cat > "$TMPROOT/.loom-project/project.json" <<EOF
{"worktree": {"root": "$ALT_BASE"}}
EOF
mkdir -p "$ALT_BASE/$(basename "$TMPROOT")/issue-9"
cat > "$ALT_BASE/$(basename "$TMPROOT")/issue-9/.loom-managed" <<'EOF'
# Loom-managed worktree marker
EOF
result=$(run_hook "$ALT_BASE/$(basename "$TMPROOT")/issue-9/src/foo.rs")
assert_allow "tiered config: worktree.root from .loom-project resolves an alt worktree base -> allow" "$result"
rm -rf "$ALT_BASE"
rm -f "$TMPROOT/.loom-project/project.json"
rmdir "$TMPROOT/.loom-project" 2>/dev/null || true

# --- Contract checks: exit is always 0, deny emits well-formed JSON --------
for r in \
    "$(run_hook "$WT/src/foo.rs")" \
    "$(run_hook "$TMPROOT/CLAUDE.md")" \
    "$(run_hook "" )" \
    ; do
    code="${r%%|*}"
    if [[ "$code" == "0" ]]; then
        pass "contract: exit 0 (case: ${r:0:40})"
    else
        fail "contract: exit 0 (case: ${r:0:40}, got exit=$code)"
    fi
done

raw=$(run_hook "$TMPROOT/CLAUDE.md")
out="${raw#*|}"
if echo "$out" | jq empty 2>/dev/null; then
    pass "contract: deny output is valid JSON"
else
    fail "contract: deny output is valid JSON (got: $out)"
fi

# --- jq absent -> allow (fail-open) -----------------------------------------
NOJQ_DIR="$(mktemp -d)"
for b in bash cat mkdir echo date sed dirname basename find python3 grep git env; do
    p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$NOJQ_DIR/$b"
done
out=$(cd "$TMPROOT" && make_input "$TMPROOT/CLAUDE.md" | PATH="$NOJQ_DIR" bash "$HOOK" 2>/dev/null)
code=$?
if [[ "$code" == "0" && -z "$out" ]]; then
    pass "jq absent -> allow (fail-open)"
else
    fail "jq absent -> allow (fail-open) (got exit=$code output=$out)"
fi
rm -rf "$NOJQ_DIR"

# --- symlink escapes (#4495) -------------------------------------------------
# The pre-#4495 normalization was purely lexical (os.path.normpath), so a
# symlink INSIDE the worktree that points at the main checkout looked like an
# in-worktree path and was allowed. loom_canonical_path resolves it.
ln -sfn "$TMPROOT" "$WT/escape-to-main"
result=$(run_hook "$WT/escape-to-main/CLAUDE.md")
assert_deny "(e) symlink inside worktree -> main checkout is resolved and denied" "$result"

# A symlink that stays inside the worktree must still be allowed.
mkdir -p "$WT/real"
ln -sfn "$WT/real" "$WT/link-to-real"
result=$(run_hook "$WT/link-to-real/new-file.txt")
assert_allow "(f) symlink inside worktree -> worktree stays allowed" "$result"

# `..` traversal out of the worktree and back into the main checkout.
result=$(run_hook "$WT/src/../../../../CLAUDE.md")
assert_deny "(g) .. traversal out of the worktree into main -> deny" "$result"

# A path with spaces and shell metacharacters must be handled as data, never
# interpolated into a command.
result=$(run_hook "$TMPROOT/a dir with spaces/\$(touch /tmp/loom-pwn); echo.md")
assert_deny "(h) path with spaces + shell metacharacters -> deny, no interpolation" "$result"
if [[ -e /tmp/loom-pwn ]]; then
    fail "(h) shell metacharacters in a path were EXECUTED"
    rm -f /tmp/loom-pwn
else
    pass "(h) shell metacharacters in a path were not executed"
fi

# --- defaults/ vs .loom/ sync ------------------------------------------------
DEPLOY_HOOK="$REPO_ROOT/.loom/hooks/guard-worktree-paths.sh"
if [[ -f "$DEPLOY_HOOK" ]] && diff -q "$SRC_HOOK" "$DEPLOY_HOOK" >/dev/null 2>&1; then
    pass ".loom/ hook byte-identical to defaults/"
else
    fail ".loom/ hook byte-identical to defaults/"
fi

echo "=== $PASS/$TOTAL passed ==="
[[ "$FAIL" -eq 0 ]]
