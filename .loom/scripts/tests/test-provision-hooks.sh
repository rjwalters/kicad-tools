#!/usr/bin/env bash
# test-provision-hooks.sh — tests for user-scope Loom guard-hook wiring
# (Epic #3835 Phase 5, #4262).
#
# Covers scripts/install/provision-hooks.sh:
#   - fresh provision merges the Loom hook entries into a missing / empty /
#     populated ~/.claude/settings.json
#   - the verifiable-globals contract (PROVISIONED_HOOKS_SETTINGS /
#     PROVISIONED_HOOKS_BACKUP, mirroring provision-dispatcher.sh #4053)
#   - idempotent re-provision (no duplicates, including a requoted pre-existing
#     entry — the #4200 lesson)
#   - pre-existing non-Loom hooks + permissions are preserved
#   - invalid existing JSON -> soft-fail (return 1) with NO write
#   - a backup file is written before the first mutation
#   - the wired command WRAPPER behaves: no-ops outside a Loom workspace (AC3),
#     execs the machine-checkout hook inside one (AC1), and defers to a present
#     per-repo .loom/hooks/ copy (transition dedup, design decision 3)
#   - deprovision removes ONLY Loom-owned entries, preserving operator hooks
#
# Sandboxed $HOME per case via mktemp -d, matching test-provision-skills.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# defaults/scripts/tests -> defaults/scripts/tests/../../.. -> repo root
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

PROVISION_LIB="$REPO_ROOT/scripts/install/provision-hooks.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}
assert_contains() {
    if [[ "$1" == *"$2"* ]]; then pass "$3"; else fail "$3 (missing substring: '$2')"; fi
}

[[ -f "$PROVISION_LIB" ]] || { echo "provisioning lib not found at $PROVISION_LIB"; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "jq required for these tests"; exit 1; }

# shellcheck source=/dev/null
source "$PROVISION_LIB"

# Count hook commands (across all types/matchers) whose command carries the
# machine-level marker for a given hook script name.
count_marker() {
    local file="$1" name="$2"
    jq --arg m "defaults/hooks/$name" '
        [ (.hooks // {}) | to_entries[] | .value[]? | .hooks[]? | .command // "" | select(contains($m)) ] | length
    ' "$file" 2>/dev/null
}

# ── Test 1: fresh provision into a MISSING settings.json ─────────────────────
echo "Test 1: fresh provision creates settings.json and wires all Loom hooks"
HOME1=$(mktemp -d)
OUT1=$(mktemp)
provision_loom_hooks "$HOME1/.claude" >"$OUT1" 2>&1
rc=$?
assert_eq "$rc" "0" "fresh provision returns 0"
SET1="$HOME1/.claude/settings.json"
[[ -f "$SET1" ]] && pass "settings.json was created" || fail "settings.json was not created"
jq empty "$SET1" 2>/dev/null && pass "settings.json is valid JSON" || fail "settings.json is not valid JSON"
assert_eq "$(count_marker "$SET1" guard-destructive.sh)" "1" "guard-destructive.sh wired once"
assert_eq "$(count_marker "$SET1" guard-worktree-paths.sh)" "1" "guard-worktree-paths.sh (Edit|Write) wired once"
assert_eq "$(count_marker "$SET1" guard-background-subagents.sh)" "1" "Stop hook wired once"
# The Edit|Write matcher exists (design decision 4 — consumers never had it).
assert_eq "$(jq -r '[.hooks.PreToolUse[]?.matcher] | index("Edit|Write") != null' "$SET1")" "true" "Edit|Write matcher is present"

# ── Test 2: verifiable-globals contract (#4053) ──────────────────────────────
echo "Test 2: provisioning exposes verifiable globals (#4053)"
assert_eq "$PROVISIONED_HOOKS_SETTINGS" "$SET1" "PROVISIONED_HOOKS_SETTINGS points at the settings file"
# No backup on a fresh (missing) file — nothing to preserve.
assert_eq "$PROVISIONED_HOOKS_BACKUP" "" "no backup written when there was no pre-existing file"

# ── Test 3: idempotent re-provision (no duplicates) ──────────────────────────
echo "Test 3: re-provision is idempotent (no duplicate entries)"
provision_loom_hooks "$HOME1/.claude" >/dev/null 2>&1
assert_eq "$(count_marker "$SET1" guard-destructive.sh)" "1" "guard-destructive.sh still wired exactly once after re-run"
assert_eq "$(count_marker "$SET1" skill-router.sh)" "1" "skill-router.sh still wired exactly once after re-run"

# ── Test 4: dedup against a REQUOTED pre-existing entry (#4200 lesson) ────────
echo "Test 4: dedup keys on the marker substring, so a requoted entry is not duplicated (#4200)"
HOME4=$(mktemp -d); mkdir -p "$HOME4/.claude"
# A pre-existing Loom entry with DIFFERENT quoting/wrapper but the SAME marker.
cat > "$HOME4/.claude/settings.json" <<'EOF'
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [
        { "type": "command", "command": "sh -c \"exec ${LOOM_HOME}/defaults/hooks/guard-destructive.sh\"" }
      ] }
    ]
  }
}
EOF
provision_loom_hooks "$HOME4/.claude" >/dev/null 2>&1
assert_eq "$(count_marker "$HOME4/.claude/settings.json" guard-destructive.sh)" "1" "requoted guard-destructive.sh entry is not duplicated"

# ── Test 5: pre-existing non-Loom hooks + permissions preserved ──────────────
echo "Test 5: operator's non-Loom hooks and permissions are preserved"
HOME5=$(mktemp -d); mkdir -p "$HOME5/.claude"
cat > "$HOME5/.claude/settings.json" <<'EOF'
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [
        { "type": "command", "command": ".claude/hooks/my-own-guard.sh" }
      ] }
    ]
  },
  "permissions": { "allow": ["Bash(mytool:*)"] }
}
EOF
provision_loom_hooks "$HOME5/.claude" >/dev/null 2>&1
S5="$HOME5/.claude/settings.json"
assert_eq "$(jq -r '[.hooks.PreToolUse[] | .hooks[]? | .command | select(. == ".claude/hooks/my-own-guard.sh")] | length' "$S5")" "1" "operator's own hook preserved"
assert_eq "$(jq -r '.permissions.allow[0]' "$S5")" "Bash(mytool:*)" "operator's permissions preserved"
assert_eq "$(count_marker "$S5" guard-destructive.sh)" "1" "Loom hook added alongside the operator's Bash matcher"

# ── Test 6: invalid existing JSON -> soft-fail, NO write ─────────────────────
echo "Test 6: invalid existing JSON is left untouched (soft-fail)"
HOME6=$(mktemp -d); mkdir -p "$HOME6/.claude"
printf 'this is { not json' > "$HOME6/.claude/settings.json"
before6=$(cat "$HOME6/.claude/settings.json")
OUT6=$(mktemp)
provision_loom_hooks "$HOME6/.claude" >"$OUT6" 2>&1
rc=$?
assert_eq "$rc" "1" "invalid JSON returns 1"
assert_eq "$(cat "$HOME6/.claude/settings.json")" "$before6" "invalid JSON file left byte-identical (no write)"
assert_contains "$(cat "$OUT6")" "not valid JSON" "explains the refusal"

# ── Test 7: a backup is written before the first mutation ─────────────────────
echo "Test 7: a backup file is written before mutating an existing settings.json"
HOME7=$(mktemp -d); mkdir -p "$HOME7/.claude"
echo '{"permissions":{"allow":["Bash(x:*)"]}}' > "$HOME7/.claude/settings.json"
provision_loom_hooks "$HOME7/.claude" >/dev/null 2>&1
backups=$(find "$HOME7/.claude" -maxdepth 1 -name 'settings.json.loom-backup-*' | wc -l | tr -d ' ')
[[ "$backups" -ge 1 ]] && pass "a timestamped backup was written" || fail "no backup file found"
# The backup holds the ORIGINAL content (pre-mutation).
bfile=$(find "$HOME7/.claude" -maxdepth 1 -name 'settings.json.loom-backup-*' | head -1)
assert_eq "$(jq -r '.permissions.allow[0]' "$bfile")" "Bash(x:*)" "backup preserves the pre-mutation content"

# ── Test 8: the wired WRAPPER command behaves correctly ──────────────────────
echo "Test 8: the wired command wrapper — workspace gate, machine exec, transition dedup"
# Build a fake machine checkout whose guard-destructive.sh prints a sentinel.
CHK=$(mktemp -d)
mkdir -p "$CHK/defaults/hooks"
printf '#!/usr/bin/env bash\necho "MACHINE-RAN:$LOOM_PROJECT_ROOT"\nexit 0\n' > "$CHK/defaults/hooks/guard-destructive.sh"
chmod +x "$CHK/defaults/hooks/guard-destructive.sh"
# Extract the actual wired command for guard-destructive.sh from a fresh provision.
HOME8=$(mktemp -d)
provision_loom_hooks "$HOME8/.claude" >/dev/null 2>&1
CMD=$(jq -r '.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[] | .command | select(contains("defaults/hooks/guard-destructive.sh"))' "$HOME8/.claude/settings.json" | head -1)
[[ -n "$CMD" ]] && pass "extracted the wired guard-destructive.sh command" || fail "could not extract the wired command"

WOUT=""; WRC=0
run_wrapper() { # $1=cwd -> assigns globals WOUT + WRC in the CURRENT shell
    WOUT=$(cd "$1" && LOOM_HOME="$CHK" bash -c "$CMD" </dev/null 2>/dev/null)
    WRC=$?
}

# 8a: non-Loom repo -> no-op (AC3)
NONLOOM=$(mktemp -d); git -C "$NONLOOM" init -q
run_wrapper "$NONLOOM"
[[ -z "$WOUT" && "$WRC" == "0" ]] && pass "AC3: non-Loom repo -> silent no-op (exit 0)" || fail "AC3: expected silent exit 0, got out='$WOUT' rc=$WRC"

# 8b: Loom workspace (legacy .loom/config.json), no per-repo copy -> machine exec (AC1)
LOOMREPO=$(mktemp -d); git -C "$LOOMREPO" init -q; mkdir -p "$LOOMREPO/.loom"; echo '{}' > "$LOOMREPO/.loom/config.json"
run_wrapper "$LOOMREPO"
assert_contains "$WOUT" "MACHINE-RAN:$LOOMREPO" "AC1: Loom workspace with no copy -> execs the machine hook, LOOM_PROJECT_ROOT set"

# 8c: Loom workspace WITH a per-repo .loom/hooks/ copy but NO project-level
# entry referencing it -> the deferral is now CONDITIONAL (#4806): with
# nothing to defer TO, the wrapper falls through and execs the machine hook
# rather than silently no-op'ing (the zero-guard-hooks bug this issue closes).
mkdir -p "$LOOMREPO/.loom/hooks"
printf '#!/usr/bin/env bash\necho SHOULD-NOT-RUN\n' > "$LOOMREPO/.loom/hooks/guard-destructive.sh"
chmod +x "$LOOMREPO/.loom/hooks/guard-destructive.sh"
run_wrapper "$LOOMREPO"
assert_contains "$WOUT" "MACHINE-RAN:$LOOMREPO" "#4806 AC(a): copies present + no project entry -> machine hook runs (was a zero-guard silent no-op)"

# 8c2: same repo, but NOW the project .claude/settings.json actually
# references the per-repo copy -> the wrapper defers (exactly one fire, no
# double-fire; #4806 AC(b)).
mkdir -p "$LOOMREPO/.claude"
printf '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"${CLAUDE_PROJECT_DIR}/.loom/hooks/guard-destructive.sh"}]}]}}\n' > "$LOOMREPO/.claude/settings.json"
run_wrapper "$LOOMREPO"
[[ -z "$WOUT" && "$WRC" == "0" ]] && pass "#4806 AC(b): copies present + project entry -> defers to it (machine hook does not double-fire)" || fail "#4806 AC(b): expected silent defer, got out='$WOUT' rc=$WRC"

# 8d: Loom workspace, copies ABSENT -> machine exec runs (#4806 AC(c); also
# covered by 8b above, restated explicitly per the issue's AC wording).
rm -rf "$LOOMREPO/.loom/hooks" "$LOOMREPO/.claude/settings.json"
run_wrapper "$LOOMREPO"
assert_contains "$WOUT" "MACHINE-RAN:$LOOMREPO" "#4806 AC(c): copies absent -> machine hook runs"

# 8e: Loom workspace but machine checkout absent -> no-op (fail-open)
out=$(cd "$LOOMREPO" && LOOM_HOME="/nonexistent/checkout" bash -c "$CMD" </dev/null 2>/dev/null); wrc=$?
[[ -z "$out" && "$wrc" == "0" ]] && pass "machine checkout absent -> silent no-op (exit 0)" || fail "expected silent exit 0 when checkout absent, got out='$out' rc=$wrc"

# ── Test 9: deprovision removes only Loom-owned entries ──────────────────────
echo "Test 9: deprovision strips Loom-owned entries, preserves operator hooks"
HOME9=$(mktemp -d); mkdir -p "$HOME9/.claude"
cat > "$HOME9/.claude/settings.json" <<'EOF'
{ "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [
  { "type": "command", "command": ".claude/hooks/my-own-guard.sh" }
] } ] }, "permissions": { "allow": ["Bash(x:*)"] } }
EOF
provision_loom_hooks "$HOME9/.claude" >/dev/null 2>&1
deprovision_loom_hooks "$HOME9/.claude" >/dev/null 2>&1
S9="$HOME9/.claude/settings.json"
assert_eq "$(count_marker "$S9" guard-destructive.sh)" "0" "deprovision removed the Loom hook entries"
assert_eq "$(jq -r '[.hooks.PreToolUse[]? | .hooks[]? | .command | select(. == ".claude/hooks/my-own-guard.sh")] | length' "$S9")" "1" "operator's own hook preserved after deprovision"
assert_eq "$(jq -r '.permissions.allow[0]' "$S9")" "Bash(x:*)" "operator's permissions preserved after deprovision"

# ─────────────────────────────────────────────────────────────────────────────
# ensure_project_hook_wiring — the project-level fallback (#4401)
#
# Guards the exact zero-coverage state reported in #4401: a repo that still
# carries per-repo `.loom/hooks/` copies (so the user-scope wrapper defers) but
# whose project-level `.claude/settings.json` entries were stripped (by the
# 0.16.0 Phase-5 defaults / a --confirm-reinstall's chained uninstall).
# ─────────────────────────────────────────────────────────────────────────────

# Count project-level hook commands referencing a given hook script name.
count_project_entry() {
    local file="$1" name="$2"
    jq --arg m ".loom/hooks/$name" '
        [ (.hooks // {}) | to_entries[] | .value[]? | .hooks[]? | .command // ""
          | select(contains($m)) | select(contains("defaults/hooks/") | not) ] | length
    ' "$file" 2>/dev/null
}

# Build a "pre-Phase-6 transition repo" fixture: .loom/hooks/ copies present.
make_transition_repo() {
    local root="$1"
    mkdir -p "$root/.loom/hooks" "$root/.claude"
    local n
    for n in guard-destructive.sh guard-loom-workflow.sh guard-worktree-paths.sh \
             skill-router.sh methodology-inject.sh guard-background-subagents.sh; do
        printf '#!/usr/bin/env bash\nexit 0\n' > "$root/.loom/hooks/$n"
        chmod +x "$root/.loom/hooks/$n"
    done
}

# ── Test 10: the #4401 repro — copies present, hooks block stripped ──────────
echo "Test 10: #4401 repro — per-repo copies + stripped project hooks -> entries restored"
R10=$(mktemp -d)
make_transition_repo "$R10"
# The post-uninstall / post-init state: 0.16.0 defaults settings.json, i.e.
# permissions only and NO `hooks` key at all.
printf '{"permissions":{"allow":["Bash(gh:*)"]}}\n' > "$R10/.claude/settings.json"
assert_eq "$(jq -r 'has("hooks")' "$R10/.claude/settings.json")" "false" "precondition: zero guard-hook coverage (no hooks key)"
# Invoked in the CURRENT shell (not a command substitution) so the verifiable
# globals it publishes survive for the assertions below.
ensure_project_hook_wiring "$R10" >/dev/null 2>&1; rc=$?
assert_eq "$rc" "0" "ensure_project_hook_wiring returns 0 on a transition repo"
S10="$R10/.claude/settings.json"
assert_eq "$(count_project_entry "$S10" guard-destructive.sh)" "1" "guard-destructive.sh reachable via a project-level entry"
assert_eq "$(count_project_entry "$S10" guard-loom-workflow.sh)" "1" "guard-loom-workflow.sh reachable"
assert_eq "$(count_project_entry "$S10" guard-worktree-paths.sh)" "1" "guard-worktree-paths.sh (Edit|Write) reachable"
assert_eq "$(count_project_entry "$S10" skill-router.sh)" "1" "skill-router.sh reachable"
assert_eq "$(count_project_entry "$S10" methodology-inject.sh)" "1" "methodology-inject.sh reachable"
assert_eq "$(count_project_entry "$S10" guard-background-subagents.sh)" "1" "guard-background-subagents.sh (Stop) reachable"
assert_eq "$PROJECT_HOOKS_WIRED" "6" "PROJECT_HOOKS_WIRED reports all six copies (#4053 verifiable-globals contract)"
assert_eq "$PROJECT_HOOKS_SETTINGS" "$S10" "PROJECT_HOOKS_SETTINGS points at the project settings file"
assert_eq "$(jq -r '.permissions.allow[0]' "$S10")" "Bash(gh:*)" "existing project permissions preserved"
# Every written command must be resolvable by Claude Code from the project root.
assert_eq "$(jq -r '[(.hooks // {}) | to_entries[] | .value[]? | .hooks[]? | .command | select(startswith("${CLAUDE_PROJECT_DIR}/.loom/hooks/") | not)] | length' "$S10")" "0" "all written commands use the \${CLAUDE_PROJECT_DIR} prefix (#3277)"
# The referenced script must exist — a dangling entry is not coverage.
for n in guard-destructive.sh guard-background-subagents.sh; do
    [[ -x "$R10/.loom/hooks/$n" ]] && pass "wired entry for $n points at an executable copy" \
        || fail "wired entry for $n points at a missing/non-executable copy"
done

# ── Test 11: idempotent — a second install must not duplicate entries ────────
echo "Test 11: re-running is idempotent (no duplicate project-level entries)"
ensure_project_hook_wiring "$R10" >/dev/null 2>&1
assert_eq "$(count_project_entry "$S10" guard-destructive.sh)" "1" "guard-destructive.sh still exactly one entry"
assert_eq "$(count_project_entry "$S10" skill-router.sh)" "1" "skill-router.sh still exactly one entry"

# ── Test 12: legacy bare-relative entries are recognized, not duplicated ─────
echo "Test 12: a legacy pre-#3277 bare-relative entry is not duplicated"
R12=$(mktemp -d)
make_transition_repo "$R12"
cat > "$R12/.claude/settings.json" <<'EOF'
{ "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [
  { "type": "command", "command": ".loom/hooks/guard-destructive.sh" }
] } ] } }
EOF
ensure_project_hook_wiring "$R12" >/dev/null 2>&1
assert_eq "$(count_project_entry "$R12/.claude/settings.json" guard-destructive.sh)" "1" "legacy bare-relative entry recognized (no duplicate added)"
assert_eq "$(count_project_entry "$R12/.claude/settings.json" guard-loom-workflow.sh)" "1" "the missing sibling entry was still added"

# ── Test 13: post-Phase-6 (migrated, copy-free) repo -> no-op ────────────────
echo "Test 13: a migrated repo with no .loom/hooks/ copies is left alone"
R13=$(mktemp -d)
mkdir -p "$R13/.loom" "$R13/.claude"
printf '{"permissions":{"allow":["Bash(gh:*)"]}}\n' > "$R13/.claude/settings.json"
BEFORE13=$(cat "$R13/.claude/settings.json")
LOG13=$(mktemp)
ensure_project_hook_wiring "$R13" >"$LOG13" 2>&1; rc=$?
OUT13=$(cat "$LOG13")
assert_eq "$rc" "0" "copy-free repo returns 0"
assert_eq "$(cat "$R13/.claude/settings.json")" "$BEFORE13" "copy-free repo settings.json left byte-identical"
assert_eq "$PROJECT_HOOKS_WIRED" "0" "PROJECT_HOOKS_WIRED is 0 on a copy-free repo"
assert_contains "$OUT13" "machine checkout" "explains that guards run from the machine checkout"

# ── Test 14: only hooks whose copy exists get an entry (no dangling entries) ──
echo "Test 14: a hook with no per-repo copy gets no project-level entry"
R14=$(mktemp -d)
make_transition_repo "$R14"
rm -f "$R14/.loom/hooks/skill-router.sh"
printf '{}\n' > "$R14/.claude/settings.json"
ensure_project_hook_wiring "$R14" >/dev/null 2>&1
assert_eq "$(count_project_entry "$R14/.claude/settings.json" skill-router.sh)" "0" "no entry written for the absent skill-router.sh copy"
assert_eq "$(count_project_entry "$R14/.claude/settings.json" guard-destructive.sh)" "1" "present copies still wired"
assert_eq "$PROJECT_HOOKS_WIRED" "5" "PROJECT_HOOKS_WIRED counts only present copies"

# ── Test 15: operator's own project hooks are preserved ──────────────────────
echo "Test 15: an operator's own project-level hooks survive"
R15=$(mktemp -d)
make_transition_repo "$R15"
cat > "$R15/.claude/settings.json" <<'EOF'
{ "hooks": { "PreToolUse": [ { "matcher": "Bash", "hooks": [
  { "type": "command", "command": ".claude/hooks/my-own-guard.sh" }
] } ] }, "permissions": { "allow": ["Bash(x:*)"] } }
EOF
ensure_project_hook_wiring "$R15" >/dev/null 2>&1
assert_eq "$(jq -r '[.hooks.PreToolUse[]? | .hooks[]? | .command | select(. == ".claude/hooks/my-own-guard.sh")] | length' "$R15/.claude/settings.json")" "1" "operator's own project hook preserved"
assert_eq "$(count_project_entry "$R15/.claude/settings.json" guard-destructive.sh)" "1" "Loom entry added alongside it"

# ── Test 16: invalid existing JSON -> soft-fail, no write ───────────────────
echo "Test 16: invalid project settings.json is left untouched (soft-fail)"
R16=$(mktemp -d)
make_transition_repo "$R16"
printf '{ this is not json' > "$R16/.claude/settings.json"
BEFORE16=$(cat "$R16/.claude/settings.json")
LOG16=$(mktemp)
ensure_project_hook_wiring "$R16" >"$LOG16" 2>&1; rc=$?
OUT16=$(cat "$LOG16")
assert_eq "$rc" "1" "invalid JSON returns 1"
assert_eq "$(cat "$R16/.claude/settings.json")" "$BEFORE16" "invalid JSON left byte-identical (no write)"
assert_contains "$OUT16" "not valid JSON" "explains the refusal"

# ── Test 17: a missing .claude/settings.json is created ─────────────────────
echo "Test 17: a missing project settings.json is created with the entries"
R17=$(mktemp -d)
make_transition_repo "$R17"
rm -rf "$R17/.claude"
ensure_project_hook_wiring "$R17" >/dev/null 2>&1
[[ -f "$R17/.claude/settings.json" ]] && pass "settings.json created" || fail "settings.json not created"
assert_eq "$(count_project_entry "$R17/.claude/settings.json" guard-destructive.sh)" "1" "entry written into the new file"

# ── Test 18: exactly ONE path fires — no double-fire, no zero-coverage ──────
echo "Test 18: user-scope + project-level compose to exactly one execution path"
# A transition repo wired BOTH ways (what a --quick install now produces).
R18=$(mktemp -d); git -C "$R18" init -q
make_transition_repo "$R18"
mkdir -p "$R18/.loom"; echo '{}' > "$R18/.loom/config.json"
printf '{}\n' > "$R18/.claude/settings.json"
HOME18=$(mktemp -d)
CHK18=$(mktemp -d); mkdir -p "$CHK18/defaults/hooks"
printf '#!/usr/bin/env bash\necho MACHINE-RAN\nexit 0\n' > "$CHK18/defaults/hooks/guard-destructive.sh"
chmod +x "$CHK18/defaults/hooks/guard-destructive.sh"
provision_loom_hooks "$HOME18/.claude" >/dev/null 2>&1
ensure_project_hook_wiring "$R18" >/dev/null 2>&1
# Project-level entry present AND the machine wrapper defers to the copy.
assert_eq "$(count_project_entry "$R18/.claude/settings.json" guard-destructive.sh)" "1" "project-level entry present (the live path)"
CMD18=$(jq -r '.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[] | .command | select(contains("defaults/hooks/guard-destructive.sh"))' "$HOME18/.claude/settings.json" | head -1)
OUT18=$(cd "$R18" && LOOM_HOME="$CHK18" bash -c "$CMD18" </dev/null 2>/dev/null); rc18=$?
[[ -z "$OUT18" && "$rc18" == "0" ]] && pass "user-scope wrapper defers (no double-fire) while the copy exists" \
    || fail "expected the user-scope wrapper to defer, got out='$OUT18' rc=$rc18"
# After a Phase-6 migration removes the copies, the machine path takes over —
# so coverage is never zero in EITHER configuration.
rm -rf "$R18/.loom/hooks"
OUT18b=$(cd "$R18" && LOOM_HOME="$CHK18" bash -c "$CMD18" </dev/null 2>/dev/null)
assert_contains "$OUT18b" "MACHINE-RAN" "once the copies are gone, the machine-checkout hook runs (coverage never zero)"

# ─────────────────────────────────────────────────────────────────────────────
# Stale-Loom-wrapper UPGRADE path (#4806) — re-provisioning an install that
# carries an OLDER Loom-authored wrapper (predating a `_phook_cmd()` edit)
# must REWRITE it in place, since the dedup marker (`defaults/hooks/<name>`)
# matches regardless of wrapper version and would otherwise cause a naive
# re-provision to skip it forever.
# ─────────────────────────────────────────────────────────────────────────────

# An "older" wrapper: same overall shape (the recognizable
# `ROOT=$(cd "$(git rev-parse --git-common-dir` prefix + the
# `defaults/hooks/<name>` marker) but with the UNCONDITIONAL transition-dedup
# step this issue replaces (no `.claude/settings.json` check before deferring).
OLD_WRAPPER_CMD='bash -c '"'"'ROOT=$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd); [ -n "$ROOT" ] || exit 0; { [ -f "$ROOT/.loom-project/project.json" ] || [ -f "$ROOT/.loom/config.json" ]; } || exit 0; [ -x "$ROOT/.loom/hooks/guard-destructive.sh" ] && exit 0; H="${LOOM_HOME:-$HOME/.local/share/loom}/defaults/hooks/guard-destructive.sh"; [ -x "$H" ] && LOOM_PROJECT_ROOT="$ROOT" exec "$H" || exit 0'"'"''

# ── Test 19: re-provisioning REPLACES a stale Loom-authored wrapper ──────────
echo "Test 19: re-provisioning an install with an OLDER Loom wrapper replaces it in place (#4806)"
HOME19=$(mktemp -d); mkdir -p "$HOME19/.claude"
jq -n --arg cmd "$OLD_WRAPPER_CMD" \
    '{hooks:{PreToolUse:[{matcher:"Bash",hooks:[{type:"command",command:$cmd}]}]}}' \
    > "$HOME19/.claude/settings.json"
BEFORE_CMD19=$(jq -r '.hooks.PreToolUse[0].hooks[0].command' "$HOME19/.claude/settings.json")
assert_eq "$BEFORE_CMD19" "$OLD_WRAPPER_CMD" "precondition: the stale wrapper is seeded verbatim"
provision_loom_hooks "$HOME19/.claude" >/dev/null 2>&1
assert_eq "$(count_marker "$HOME19/.claude/settings.json" guard-destructive.sh)" "1" "still exactly one guard-destructive.sh entry after upgrade (no duplicate)"
AFTER_CMD19=$(jq -r '.hooks.PreToolUse[0].hooks[0].command' "$HOME19/.claude/settings.json")
CURRENT_CMD19="$(_phook_cmd guard-destructive.sh)"
assert_eq "$AFTER_CMD19" "$CURRENT_CMD19" "the stale entry was rewritten to the CURRENT _phook_cmd() output"
[[ "$AFTER_CMD19" != "$OLD_WRAPPER_CMD" ]] && pass "the old unconditional-defer text is gone" || fail "the old wrapper text is still present — upgrade did not fire"
# Idempotent: a second re-provision must not touch it again (it now matches).
provision_loom_hooks "$HOME19/.claude" >/dev/null 2>&1
assert_eq "$(count_marker "$HOME19/.claude/settings.json" guard-destructive.sh)" "1" "still exactly one entry after a second re-provision"
assert_eq "$(jq -r '.hooks.PreToolUse[0].hooks[0].command' "$HOME19/.claude/settings.json")" "$CURRENT_CMD19" "already-current entry left unchanged by a second re-provision"

# ── Test 20: a hand-written entry sharing the marker is NEVER rewritten ──────
echo "Test 20: a non-Loom / hand-written entry is never rewritten or removed by the upgrade path (#4806)"
HOME20=$(mktemp -d); mkdir -p "$HOME20/.claude"
HANDWRITTEN_CMD='.claude/hooks/my-custom-wrapper.sh --marker defaults/hooks/guard-destructive.sh'
jq -n --arg cmd "$HANDWRITTEN_CMD" \
    '{hooks:{PreToolUse:[{matcher:"Bash",hooks:[{type:"command",command:$cmd}]}]}}' \
    > "$HOME20/.claude/settings.json"
provision_loom_hooks "$HOME20/.claude" >/dev/null 2>&1
assert_eq "$(jq -r '[.hooks.PreToolUse[0].hooks[] | select(.command == $h)] | length' --arg h "$HANDWRITTEN_CMD" "$HOME20/.claude/settings.json")" "1" "hand-written entry is byte-identical after provisioning (never rewritten)"
# Its shape does not match the known Loom wrapper prefix, so the dedup test
# treats the marker match as satisfied and does NOT append a second (Loom)
# entry either — same no-duplicate contract as before this issue.
assert_eq "$(count_marker "$HOME20/.claude/settings.json" guard-destructive.sh)" "1" "no second (Loom) entry appended alongside the hand-written one"

echo ""
echo "======================================"
echo "test-provision-hooks.sh: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
echo "======================================"
[[ "$TESTS_FAILED" -eq 0 ]]
