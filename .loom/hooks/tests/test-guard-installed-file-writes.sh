#!/usr/bin/env bash
# Test suite for the installed-managed-file write guard (issue #7995)
#
# Usage: ./defaults/hooks/tests/test-guard-installed-file-writes.sh
#
# Covers the two-step shape #7995 asked for:
#
#   STEP 1 — the repo-identity discriminator
#   (defaults/scripts/lib/installed-file-guard.sh, loom_repo_identity):
#     - `upstream` for rjwalters/loom's OWN tree (asserted against this very
#       checkout, not a fixture) and for a simulated Loom source tree
#     - `consumer` for a simulated consumer-repo checkout
#     - `unknown`, i.e. PERMISSIVE, whenever it cannot be determined: no
#       checkout root, a nonexistent root, or an installed `.loom/` with no
#       readable install marker
#     - managed-prefix splitting, including the nested worktree-inside-a-repo
#       case and the `.claude/commands/loom/` prefix
#
#   STEP 2 — the guard itself, on BOTH PreToolUse matchers:
#     - Edit/Write (guard-worktree-paths.sh): deny in a consumer repo, allow
#       in Loom's own tree, allow when the discriminator is unreadable
#     - Bash (guard-loom-workflow.sh): the same verdict for every write idiom
#       guard-destructive-generic.sh already handles (`>`, `>>`, tee, sed -i,
#       cp, mv), and no denial for a READ of the same path
#     - `.loom/resync-ignore` pins are honored (the pin IS a sanctioned
#       disposition; denying it would block the remedy the guard recommends)
#     - the deny message names BOTH dispositions (upstream / pin)
#     - guards.installedFileWrites / LOOM_GUARD_INSTALLED_FILE_WRITES toggle
#       (env beats config; config beats default-on)
#     - fail-open contract: exit is always 0 and every denial is well-formed
#       hookSpecificOutput JSON
#
# Fixtures are isolated `mktemp -d` git trees; the hooks under test are copied
# in at their installed layout (.loom/hooks/ + .loom/scripts/lib/) so their
# SCRIPT_DIR-relative library sourcing exercises the real libraries. No
# network, no forge, no `gh`.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Prefer the installed copies (a Loom-installed consumer repo has no defaults/
# directory at all), fall back to defaults/ for Loom's own source tree — the
# same resolution the sibling suites use (#6496).
pick() {
    local installed="$REPO_ROOT/$1" source_path="$REPO_ROOT/$2"
    if [[ -r "$installed" ]]; then printf '%s' "$installed"; else printf '%s' "$source_path"; fi
}
WT_HOOK="$(pick .loom/hooks/guard-worktree-paths.sh defaults/hooks/guard-worktree-paths.sh)"
WF_HOOK="$(pick .loom/hooks/guard-loom-workflow.sh defaults/hooks/guard-loom-workflow.sh)"
LIB_IFW="$(pick .loom/scripts/lib/installed-file-guard.sh defaults/scripts/lib/installed-file-guard.sh)"
LIB_CFG="$(pick .loom/scripts/lib/config-resolver.sh defaults/scripts/lib/config-resolver.sh)"
LIB_CANON="$(pick .loom/scripts/lib/canonical-path.sh defaults/scripts/lib/canonical-path.sh)"

PASS=0
FAIL=0
TOTAL=0
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); printf "${GREEN}PASS${NC} %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); printf "${RED}FAIL${NC} %s\n" "$1"; }

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$desc"
    else
        fail "$desc (expected '$expected', got '$actual')"
    fi
}

# --------------------------------------------------------------------------
# Fixture builder
# --------------------------------------------------------------------------
# kind=consumer   installed .loom/ + install marker, no defaults/ source tree
# kind=upstream   same, PLUS defaults/.claude/commands/loom/ (Loom's own tree)
# kind=unknown    installed .loom/ tree but NO readable install marker
FIXTURES=()
make_fixture() {
    local kind="$1" root
    root="$(mktemp -d)"
    FIXTURES+=("$root")
    git init -q "$root"
    mkdir -p "$root/.loom/hooks" "$root/.loom/scripts/lib" \
             "$root/.claude/commands/loom" "$root/src"
    cp "$WT_HOOK"   "$root/.loom/hooks/guard-worktree-paths.sh"
    cp "$WF_HOOK"   "$root/.loom/hooks/guard-loom-workflow.sh"
    cp "$LIB_IFW"   "$root/.loom/scripts/lib/installed-file-guard.sh"
    cp "$LIB_CFG"   "$root/.loom/scripts/lib/config-resolver.sh"
    cp "$LIB_CANON" "$root/.loom/scripts/lib/canonical-path.sh"
    chmod +x "$root/.loom/hooks/"*.sh
    case "$kind" in
        consumer)
            printf '{"version":"0.0.0"}\n' > "$root/.loom/install-metadata.json" ;;
        upstream)
            printf '{"version":"0.0.0"}\n' > "$root/.loom/install-metadata.json"
            mkdir -p "$root/defaults/.claude/commands/loom" "$root/defaults/hooks"
            : > "$root/defaults/.claude/commands/loom/builder.md" ;;
        unknown)
            : ;;   # no install-metadata.json, no config.json, no .loom-project
    esac
    printf '%s' "$root"
}
cleanup() { local f; for f in ${FIXTURES[@]+"${FIXTURES[@]}"}; do rm -rf "$f"; done; }
trap cleanup EXIT

# Run the Edit/Write guard. Prints "<exit>|<stdout>".
run_write_hook() {
    local root="$1" file_path="$2"; shift 2
    local out code=0
    out=$(cd "$root" && env LOOM_CONFIG_DEFAULTS_FILE= "$@" \
            bash "$root/.loom/hooks/guard-worktree-paths.sh" \
            < <(jq -n --arg fp "$file_path" --arg cwd "$root" \
                     '{tool_input:{file_path:$fp}, cwd:$cwd}') 2>/dev/null) || code=$?
    printf '%s|%s' "$code" "$out"
}

# Run the Bash guard. Prints "<exit>|<stdout>".
run_bash_hook() {
    local root="$1" command="$2"; shift 2
    local out code=0
    out=$(cd "$root" && env LOOM_CONFIG_DEFAULTS_FILE= "$@" \
            bash "$root/.loom/hooks/guard-loom-workflow.sh" \
            < <(jq -n --arg c "$command" --arg cwd "$root" \
                     '{tool_input:{command:$c}, cwd:$cwd}') 2>/dev/null) || code=$?
    printf '%s|%s' "$code" "$out"
}

decision_of() { echo "${1#*|}" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null || true; }
reason_of()   { echo "${1#*|}" | jq -r '.hookSpecificOutput.permissionDecisionReason // empty' 2>/dev/null || true; }

assert_allow() {
    local desc="$1" result="$2"
    local code="${result%%|*}" out="${result#*|}"
    if [[ "$code" != "0" ]]; then
        fail "$desc (hook exited non-zero: $code)"
        return
    fi
    if [[ -z "$out" ]] || [[ "$(decision_of "$result")" != "deny" ]]; then
        pass "$desc"
    else
        fail "$desc (expected no denial, got: $out)"
    fi
}

assert_deny() {
    local desc="$1" result="$2"
    local code="${result%%|*}"
    if [[ "$code" != "0" ]]; then
        fail "$desc (hook exited non-zero: $code — the fail-open contract is violated)"
        return
    fi
    if [[ "$(decision_of "$result")" == "deny" ]]; then
        pass "$desc"
    else
        fail "$desc (expected permissionDecision=deny, got: ${result#*|})"
    fi
}

echo "=== installed-file write guard (#7995) ==="

CONSUMER="$(make_fixture consumer)"
UPSTREAM="$(make_fixture upstream)"
UNKNOWN="$(make_fixture unknown)"

# ==========================================================================
# STEP 1 — the repo-identity discriminator
# ==========================================================================
echo "--- discriminator (loom_repo_identity) ---"

# shellcheck source=/dev/null
source "$LIB_IFW"

# AC: verified TRUE for rjwalters/loom itself. Asserted against this checkout,
# not a fixture, so the discriminator cannot silently stop recognizing the one
# tree whose Builders must never be denied.
assert_eq "rjwalters/loom's own checkout classifies as 'upstream'" \
    "upstream" "$(loom_repo_identity "$REPO_ROOT")"

# ...and from inside one of its managed worktrees, where a Builder actually
# works (a worktree is a full checkout, so it carries defaults/ too).
if [[ -d "$REPO_ROOT/.loom/worktrees" ]]; then
    for wt in "$REPO_ROOT"/.loom/worktrees/*/; do
        [[ -d "$wt" ]] || continue
        assert_eq "a managed worktree of Loom's own tree also classifies 'upstream'" \
            "upstream" "$(loom_repo_identity "${wt%/}")"
        break
    done
fi

assert_eq "simulated consumer checkout classifies as 'consumer'" \
    "consumer" "$(loom_repo_identity "$CONSUMER")"
assert_eq "simulated Loom source tree classifies as 'upstream'" \
    "upstream" "$(loom_repo_identity "$UPSTREAM")"
assert_eq "installed .loom/ with no install marker classifies as 'unknown'" \
    "unknown" "$(loom_repo_identity "$UNKNOWN")"
assert_eq "empty root classifies as 'unknown'" \
    "unknown" "$(loom_repo_identity "")"
assert_eq "nonexistent root classifies as 'unknown'" \
    "unknown" "$(loom_repo_identity "/nonexistent/loom/root/$$")"

# A repo carrying .loom/config.json (but no install-metadata.json) is still an
# affirmative Loom install.
printf '{}\n' > "$UNKNOWN/.loom/config.json"
assert_eq ".loom/config.json alone is enough to classify 'consumer'" \
    "consumer" "$(loom_repo_identity "$UNKNOWN")"
rm -f "$UNKNOWN/.loom/config.json"
assert_eq "removing the marker returns it to 'unknown' (permissive)" \
    "unknown" "$(loom_repo_identity "$UNKNOWN")"

# --- managed-prefix splitting ---------------------------------------------
if loom_installed_managed_split "/repo/.loom/hooks/guard-x.sh"; then
    assert_eq "split: root" "/repo" "$LOOM_IFW_ROOT"
    assert_eq "split: report-relative path" "hooks/guard-x.sh" "$LOOM_IFW_REL"
    assert_eq "split: repo-relative path" ".loom/hooks/guard-x.sh" "$LOOM_IFW_REPO_REL"
else
    fail "split: /repo/.loom/hooks/guard-x.sh should match a managed prefix"
fi

# Greedy match: the LAST managed prefix wins, so a write inside a worktree
# nested under `<repo>/.loom/worktrees/` is attributed to the WORKTREE root.
if loom_installed_managed_split "/repo/.loom/worktrees/issue-7/.loom/scripts/x.sh"; then
    assert_eq "split: nested worktree root wins" "/repo/.loom/worktrees/issue-7" "$LOOM_IFW_ROOT"
else
    fail "split: nested worktree path should match a managed prefix"
fi

if loom_installed_managed_split "/repo/.claude/commands/loom/builder.md"; then
    assert_eq "split: .claude/commands/loom maps to the resync-ignore spelling" \
        "commands/loom/builder.md" "$LOOM_IFW_REL"
else
    fail "split: .claude/commands/loom path should match a managed prefix"
fi

for nonmatch in "/repo/defaults/hooks/x.sh" "/repo/.loom/worktrees/issue-1/src/a.rs" \
                "/repo/.loom/docsomething/x" "/repo/.claude/commands/other/x.md" \
                "relative/.loom/hooks/x.sh"; do
    if loom_installed_managed_split "$nonmatch"; then
        fail "split: '$nonmatch' must NOT match a managed prefix (got $LOOM_IFW_ROOT)"
    else
        pass "split: '$nonmatch' correctly does not match a managed prefix"
    fi
done

# --- Bash write-idiom extraction ------------------------------------------
targets_of() { loom_bash_write_targets "$1" | tr '\n' ' '; }
assert_eq "extract: > redirection"      ".loom/hooks/x.sh "  "$(targets_of 'echo hi > .loom/hooks/x.sh')"
assert_eq "extract: >> redirection"     ".loom/bin/loom "    "$(targets_of 'printf x >>.loom/bin/loom')"
assert_eq "extract: tee"                ".loom/docs/a.md "   "$(targets_of 'cat f | tee -a .loom/docs/a.md')"
assert_eq "extract: cp destination"     ".loom/roles/b.md "  "$(targets_of 'cp /tmp/b.md .loom/roles/b.md')"
assert_eq "extract: mv destination"     ".loom/roles/b.md "  "$(targets_of 'mv a.md .loom/roles/b.md')"
assert_eq "extract: cp SOURCE is not a target" "/tmp/out " "$(targets_of 'cp .loom/hooks/x.sh /tmp/out')"
assert_eq "extract: a read is not a write"     ""          "$(targets_of 'grep -rn foo .loom/hooks/')"
assert_eq "extract: running an installed script is not a write" "" \
    "$(targets_of './.loom/scripts/worktree.sh 7995')"

# ==========================================================================
# STEP 2a — Edit/Write matcher (guard-worktree-paths.sh)
# ==========================================================================
echo "--- Edit/Write matcher ---"

assert_deny "consumer repo: Edit/Write to .loom/hooks/ -> deny" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh")"
assert_deny "consumer repo: Edit/Write to .loom/scripts/ -> deny" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/scripts/merge-pr.sh")"
assert_deny "consumer repo: Edit/Write to .loom/roles/ -> deny" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/roles/builder.md")"
assert_deny "consumer repo: Edit/Write to .loom/docs/ -> deny" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/docs/guard-hooks.md")"
assert_deny "consumer repo: Edit/Write to .loom/bin/ -> deny" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/bin/loom")"
assert_deny "consumer repo: Edit/Write to .claude/commands/loom/ -> deny" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.claude/commands/loom/builder.md")"
assert_deny "consumer repo: a relative target resolved against cwd -> deny" \
    "$(run_write_hook "$CONSUMER" ".loom/hooks/new-guard.sh")"

assert_allow "consumer repo: ordinary source file -> allow" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/src/main.rs")"
assert_allow "consumer repo: .loom/ path outside the managed prefixes -> allow" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/resync-ignore")"

# AC: no denial in Loom's own tree.
assert_allow "Loom's own tree: Edit/Write to .loom/hooks/ -> allow" \
    "$(run_write_hook "$UPSTREAM" "$UPSTREAM/.loom/hooks/guard-destructive.sh")"
assert_allow "Loom's own tree: Edit/Write to .claude/commands/loom/ -> allow" \
    "$(run_write_hook "$UPSTREAM" "$UPSTREAM/.claude/commands/loom/builder.md")"
assert_allow "Loom's own tree: Edit/Write to defaults/ -> allow" \
    "$(run_write_hook "$UPSTREAM" "$UPSTREAM/defaults/hooks/guard-destructive.sh")"

# AC: no denial when the discriminator is unreadable.
assert_allow "indeterminate repo (no install marker): Edit/Write to .loom/hooks/ -> allow" \
    "$(run_write_hook "$UNKNOWN" "$UNKNOWN/.loom/hooks/guard-destructive.sh")"

# --- deny message names BOTH dispositions ---------------------------------
raw="$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh")"
reason="$(reason_of "$raw")"
if [[ "$reason" == *"defaults/hooks/guard-destructive.sh"* ]]; then
    pass "deny reason names the exact upstream defaults/ path to PR against"
else
    fail "deny reason names the exact upstream defaults/ path (got: $reason)"
fi
if [[ "$reason" == *"resync-ignore"* ]]; then
    pass "deny reason names the .loom/resync-ignore pin disposition"
else
    fail "deny reason names the .loom/resync-ignore pin disposition (got: $reason)"
fi
if [[ "$reason" == *"guards.installedFileWrites:false in .loom/config.json"* \
   && "$reason" == *"LOOM_GUARD_INSTALLED_FILE_WRITES=0"* && "$reason" == *"does NOT work"* ]]; then
    pass "deny reason names the toggle escape hatch and the inline-env-prefix trap (#6110)"
else
    fail "deny reason names the toggle escape hatch and the inline-env-prefix trap (got: $reason)"
fi

# --- .loom/resync-ignore pin ----------------------------------------------
cat > "$CONSUMER/.loom/resync-ignore" <<'EOF'
# this repo deliberately owns its own copy
hooks/guard-destructive.sh
.loom/docs/guard-hooks.md
EOF
assert_allow "pinned path (.loom/-relative spelling) -> allow" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh")"
assert_allow "pinned path (repo-relative spelling) -> allow" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/docs/guard-hooks.md")"
assert_deny "an UNpinned sibling still denies" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-loom-workflow.sh")"
rm -f "$CONSUMER/.loom/resync-ignore"

# --- toggle ---------------------------------------------------------------
printf '{"guards": {"installedFileWrites": false}}\n' > "$CONSUMER/.loom/config.json"
assert_allow "toggle: guards.installedFileWrites=false -> allow" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh")"
assert_deny "toggle: env LOOM_GUARD_INSTALLED_FILE_WRITES=1 overrides config false" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh" LOOM_GUARD_INSTALLED_FILE_WRITES=1)"
rm -f "$CONSUMER/.loom/config.json"
assert_allow "toggle: env LOOM_GUARD_INSTALLED_FILE_WRITES=0 -> allow" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh" LOOM_GUARD_INSTALLED_FILE_WRITES=0)"
assert_deny "toggle: default (no config, no env) is ON -> deny" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh")"

# The two categories in this hook are independent: turning worktree isolation
# off must NOT disable the installed-file category (and vice versa).
assert_deny "categories are independent: worktreeIsolation=0 still denies an installed-file write" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh" LOOM_GUARD_WORKTREE_ISOLATION=0)"

# ==========================================================================
# STEP 2b — Bash matcher (guard-loom-workflow.sh)
# ==========================================================================
echo "--- Bash matcher ---"

assert_deny "consumer repo: '>' redirection into .loom/hooks/ -> deny" \
    "$(run_bash_hook "$CONSUMER" "echo x > $CONSUMER/.loom/hooks/guard-destructive.sh")"
assert_deny "consumer repo: '>>' redirection into .loom/bin/ -> deny" \
    "$(run_bash_hook "$CONSUMER" "printf x >> .loom/bin/loom")"
assert_deny "consumer repo: tee into .loom/docs/ -> deny" \
    "$(run_bash_hook "$CONSUMER" "echo x | tee .loom/docs/notes.md")"
assert_deny "consumer repo: sed -i on .loom/scripts/ -> deny" \
    "$(run_bash_hook "$CONSUMER" "sed -i 's/a/b/' .loom/scripts/merge-pr.sh")"
assert_deny "consumer repo: cp INTO .loom/roles/ -> deny" \
    "$(run_bash_hook "$CONSUMER" "cp /tmp/b.md .loom/roles/builder.md")"
assert_deny "consumer repo: mv INTO .claude/commands/loom/ -> deny" \
    "$(run_bash_hook "$CONSUMER" "mv /tmp/x.md .claude/commands/loom/x.md")"

assert_allow "consumer repo: READING an installed file -> allow" \
    "$(run_bash_hook "$CONSUMER" "cat .loom/hooks/guard-destructive.sh")"
assert_allow "consumer repo: grepping the installed tree -> allow" \
    "$(run_bash_hook "$CONSUMER" "grep -rn deny .loom/hooks/")"
assert_allow "consumer repo: RUNNING an installed script -> allow" \
    "$(run_bash_hook "$CONSUMER" "./.loom/scripts/worktree.sh 42")"
assert_allow "consumer repo: cp FROM the installed tree to /tmp -> allow" \
    "$(run_bash_hook "$CONSUMER" "cp .loom/hooks/guard-destructive.sh /tmp/backup.sh")"
assert_allow "consumer repo: writing an ordinary source file -> allow" \
    "$(run_bash_hook "$CONSUMER" "echo x > src/main.rs")"

assert_allow "Loom's own tree: '>' redirection into .loom/hooks/ -> allow" \
    "$(run_bash_hook "$UPSTREAM" "echo x > .loom/hooks/guard-destructive.sh")"
assert_allow "indeterminate repo: '>' redirection into .loom/hooks/ -> allow" \
    "$(run_bash_hook "$UNKNOWN" "echo x > .loom/hooks/guard-destructive.sh")"

assert_allow "Bash toggle: LOOM_GUARD_INSTALLED_FILE_WRITES=0 -> allow" \
    "$(run_bash_hook "$CONSUMER" "echo x > .loom/hooks/guard-destructive.sh" LOOM_GUARD_INSTALLED_FILE_WRITES=0)"

# A quoted mention of a write idiom in a text-carrying flag value must not
# false-deny — the tokenizer is quote-aware, so the `>` never becomes an
# operator.
assert_allow "quoted mention of the idiom in a commit message -> allow" \
    "$(run_bash_hook "$CONSUMER" "git commit -m 'never do: echo x > .loom/hooks/y.sh'")"

# --- Bash deny message + contract -----------------------------------------
raw="$(run_bash_hook "$CONSUMER" "echo x > .loom/hooks/guard-destructive.sh")"
reason="$(reason_of "$raw")"
if [[ "$reason" == *"defaults/hooks/guard-destructive.sh"* && "$reason" == *"resync-ignore"* ]]; then
    pass "Bash deny reason names both dispositions (upstream / pin)"
else
    fail "Bash deny reason names both dispositions (got: $reason)"
fi
if echo "${raw#*|}" | jq empty 2>/dev/null; then
    pass "contract: Bash deny output is valid JSON"
else
    fail "contract: Bash deny output is valid JSON (got: ${raw#*|})"
fi
if [[ "$(echo "${raw#*|}" | jq -r '.hookSpecificOutput.hookEventName // empty')" == "PreToolUse" ]]; then
    pass "contract: Bash deny output carries hookEventName=PreToolUse (#3550)"
else
    fail "contract: Bash deny output carries hookEventName=PreToolUse (#3550)"
fi

# --- fail-open contract: exit is always 0 ---------------------------------
for r in \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/x.sh")" \
    "$(run_write_hook "$UPSTREAM" "$UPSTREAM/.loom/hooks/x.sh")" \
    "$(run_write_hook "$UNKNOWN" "")" \
    "$(run_bash_hook "$CONSUMER" "echo x > .loom/hooks/x.sh")" \
    "$(run_bash_hook "$CONSUMER" "")" \
    ; do
    if [[ "${r%%|*}" == "0" ]]; then
        pass "contract: exit 0 (case: ${r:0:32})"
    else
        fail "contract: exit 0 (case: ${r:0:32}, got exit=${r%%|*})"
    fi
done

# --- the library is absent -> the category is skipped entirely (fail open) --
mv "$CONSUMER/.loom/scripts/lib/installed-file-guard.sh" "$CONSUMER/.loom/scripts/lib/installed-file-guard.sh.off"
assert_allow "library missing: Edit/Write falls open" \
    "$(run_write_hook "$CONSUMER" "$CONSUMER/.loom/hooks/guard-destructive.sh")"
assert_allow "library missing: Bash write falls open" \
    "$(run_bash_hook "$CONSUMER" "echo x > .loom/hooks/guard-destructive.sh")"
mv "$CONSUMER/.loom/scripts/lib/installed-file-guard.sh.off" "$CONSUMER/.loom/scripts/lib/installed-file-guard.sh"

# --- defaults/ vs .loom/ sync ---------------------------------------------
for pair in \
    "defaults/scripts/lib/installed-file-guard.sh:.loom/scripts/lib/installed-file-guard.sh" \
    "defaults/hooks/guard-worktree-paths.sh:.loom/hooks/guard-worktree-paths.sh" \
    "defaults/hooks/guard-loom-workflow.sh:.loom/hooks/guard-loom-workflow.sh" \
    ; do
    src="$REPO_ROOT/${pair%%:*}"
    dst="$REPO_ROOT/${pair#*:}"
    if [[ ! -f "$src" ]]; then
        echo "SKIP: ${pair%%:*} not present (bare consumer layout) -- sync check not applicable"
    elif [[ -f "$dst" ]] && diff -q "$src" "$dst" >/dev/null 2>&1; then
        pass "installed copy byte-identical to defaults/ (${pair#*:})"
    else
        fail "installed copy byte-identical to defaults/ (${pair#*:})"
    fi
done

echo "=== $PASS/$TOTAL passed ==="
[[ "$FAIL" -eq 0 ]]
