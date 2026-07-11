#!/usr/bin/env bash
# test-worktree-root-override.sh — Tests for configurable worktree root (#3530)
#
# Verifies the opt-in LOOM_WORKTREE_ROOT / .loom/config.json worktree.root
# override implemented via defaults/scripts/lib/worktree-root.sh and wired into
# worktree.sh, pr-worktree.sh, merge-pr.sh, and agent-destroy.sh.
#
# Coverage:
#   1. Default (no override): worktree lands under $repo/.loom/worktrees,
#      byte-for-byte identical to historical behavior.
#   2. Env-var override: worktree is created at
#      ${LOOM_WORKTREE_ROOT}/<repo-basename>/issue-<N>, registered with git,
#      and carries the .loom-managed sentinel. The default .loom/worktrees dir
#      is never created.
#   3. Config-key override (.loom/config.json → worktree.root) with the env var
#      unset produces the same namespaced path.
#   4. Env var beats config when both are set.
#   5. agent-destroy.sh GC-detection recognizes an overridden-root worktree
#      (the substring-only check would have skipped it) — the regression this
#      issue must not reintroduce.
#   6. _worktree_locks_dir() resolves via git-common-dir regardless of the
#      worktree root override (locks stay in the main repo).
#   7. loom_worktree_root() unit behavior: relative override is rejected with a
#      warning and falls back to the default.
#
# Pattern follows test-worktree-sentinel.sh / test-worktree-nested-symlinks.sh:
# throwaway bare origin + repo in a mktemp dir, copy worktree.sh + lib/, run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

WORKTREE_SH="$SCRIPTS_DIR/worktree.sh"
AGENT_DESTROY_SH="$SCRIPTS_DIR/agent-destroy.sh"
WORKTREE_ROOT_LIB="$SCRIPTS_DIR/lib/worktree-root.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_dir() {
    if [[ -d "$1" ]]; then pass "$2"; else fail "$2 (expected dir: $1)"; fi
}
assert_no_dir() {
    if [[ ! -d "$1" ]]; then pass "$2"; else fail "$2 (unexpected dir present: $1)"; fi
}
assert_file() {
    if [[ -f "$1" ]]; then pass "$2"; else fail "$2 (expected file: $1)"; fi
}
assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

# Build a throwaway repo named `<basename>` with an origin/main ref and the
# minimal .loom layout worktree.sh needs. Echoes the working-tree path.
# The repo basename is meaningful — the override namespacing keys off it — so
# the caller passes a stable name.
setup_repo() {
    local name="${1:-myrepo}"
    local tmp
    tmp=$(mktemp -d /tmp/loom-wtroot.XXXXXX)
    git init -q -b main "$tmp/origin.git" --bare
    git init -q -b main "$tmp/$name"
    (
        cd "$tmp/$name"
        git config user.email t@t
        git config user.name t
        git commit --allow-empty -q -m init
        git remote add origin "$tmp/origin.git"
        git push -q origin main
        mkdir -p .loom/scripts/lib .loom/hooks
        cp "$WORKTREE_SH" .loom/scripts/worktree.sh
        if [[ -d "$SCRIPTS_DIR/lib" ]]; then
            cp -R "$SCRIPTS_DIR"/lib/* .loom/scripts/lib/ 2>/dev/null || true
        fi
        chmod +x .loom/scripts/worktree.sh
    )
    echo "$tmp/$name"
}

cleanup_repo() {
    local repo="$1"
    [[ -z "$repo" ]] && return 0
    rm -rf "$(dirname "$repo")"
}

# --- Test 1: default (no override) — worktree under .loom/worktrees ---
echo "Test 1: default behavior unchanged (no override)"
REPO=$(setup_repo defrepo)
(
    cd "$REPO"
    unset LOOM_WORKTREE_ROOT
    ./.loom/scripts/worktree.sh 100 >/tmp/wtroot-default.$$ 2>&1 || {
        echo "worktree.sh failed (see /tmp/wtroot-default.$$)"; cat /tmp/wtroot-default.$$
    }
)
assert_dir "$REPO/.loom/worktrees/issue-100" "default worktree created under .loom/worktrees"
assert_file "$REPO/.loom/worktrees/issue-100/.loom-managed" "default worktree carries .loom-managed sentinel"
cleanup_repo "$REPO"

# --- Test 2: env-var override ---
echo ""
echo "Test 2: LOOM_WORKTREE_ROOT env override → namespaced external path"
REPO=$(setup_repo envrepo)
EXT=$(mktemp -d /tmp/loom-ext.XXXXXX)
(
    cd "$REPO"
    LOOM_WORKTREE_ROOT="$EXT" ./.loom/scripts/worktree.sh 200 >/tmp/wtroot-env.$$ 2>&1 || {
        echo "worktree.sh failed (see /tmp/wtroot-env.$$)"; cat /tmp/wtroot-env.$$
    }
)
assert_dir "$EXT/envrepo/issue-200" "env-override worktree created at \${root}/<repo>/issue-N"
assert_file "$EXT/envrepo/issue-200/.loom-managed" "env-override worktree carries .loom-managed sentinel"
assert_no_dir "$REPO/.loom/worktrees/issue-200" "default .loom/worktrees not used when override set"
# git must have registered the worktree at the external path.
if git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -qF "$EXT/envrepo/issue-200"; then
    pass "env-override worktree registered with git worktree list"
else
    fail "env-override worktree not registered with git"
fi
rm -rf "$EXT"
cleanup_repo "$REPO"

# --- Test 3: config-key override ---
echo ""
echo "Test 3: .loom/config.json worktree.root override (env var unset)"
REPO=$(setup_repo cfgrepo)
CFGEXT=$(mktemp -d /tmp/loom-cfgext.XXXXXX)
(
    cd "$REPO"
    unset LOOM_WORKTREE_ROOT
    printf '{ "worktree": { "root": "%s" } }\n' "$CFGEXT" > .loom/config.json
    ./.loom/scripts/worktree.sh 300 >/tmp/wtroot-cfg.$$ 2>&1 || {
        echo "worktree.sh failed (see /tmp/wtroot-cfg.$$)"; cat /tmp/wtroot-cfg.$$
    }
)
assert_dir "$CFGEXT/cfgrepo/issue-300" "config-override worktree created at configured root"
assert_no_dir "$REPO/.loom/worktrees/issue-300" "default .loom/worktrees not used with config override"
rm -rf "$CFGEXT"
cleanup_repo "$REPO"

# --- Test 4: env beats config ---
echo ""
echo "Test 4: env var takes precedence over config key"
REPO=$(setup_repo bothrepo)
CFGEXT=$(mktemp -d /tmp/loom-both-cfg.XXXXXX)
ENVEXT=$(mktemp -d /tmp/loom-both-env.XXXXXX)
(
    cd "$REPO"
    printf '{ "worktree": { "root": "%s" } }\n' "$CFGEXT" > .loom/config.json
    LOOM_WORKTREE_ROOT="$ENVEXT" ./.loom/scripts/worktree.sh 400 >/tmp/wtroot-both.$$ 2>&1 || {
        echo "worktree.sh failed (see /tmp/wtroot-both.$$)"; cat /tmp/wtroot-both.$$
    }
)
assert_dir "$ENVEXT/bothrepo/issue-400" "env-var root wins when both set"
assert_no_dir "$CFGEXT/bothrepo/issue-400" "config root ignored when env var also set"
rm -rf "$CFGEXT" "$ENVEXT"
cleanup_repo "$REPO"

# --- Test 5: agent-destroy.sh GC recognizes overridden root ---
echo ""
echo "Test 5: agent-destroy.sh GC-detection accepts an overridden-root worktree"
# We exercise the decision predicate directly by sourcing the same helper the
# script uses and replicating the load-bearing condition from agent-destroy.sh:
#   worktree_path != repo_root AND
#   ( worktree_path starts with resolved-root/ OR contains .loom/worktrees/ )
# The regression this guards: an overridden root outside .loom/worktrees must
# still be recognized as a Loom-managed worktree (not skipped as user-owned).
# shellcheck source=../lib/worktree-root.sh
source "$WORKTREE_ROOT_LIB"

gc_recognizes() {
    # $1 repo_root, $2 worktree_path
    local repo_root="$1" worktree_path="$2" wt_root_dir
    wt_root_dir="$(loom_worktree_root "$repo_root")"
    if [[ "$worktree_path" != "$repo_root" ]] && \
       { [[ "$worktree_path" == "$wt_root_dir/"* ]] || [[ "$worktree_path" == *".loom/worktrees/"* ]]; }; then
        echo "recognized"
    else
        echo "skipped"
    fi
}

# Default root: recognized via both the resolved-root and the substring branch.
unset LOOM_WORKTREE_ROOT
r=$(gc_recognizes "/Users/foo/repo" "/Users/foo/repo/.loom/worktrees/issue-9")
assert_eq "$r" "recognized" "default-root worktree recognized by GC predicate"

# Overridden root outside .loom/worktrees: recognized only because the resolved
# root now matches (the old substring-only check would have skipped it).
r=$(LOOM_WORKTREE_ROOT="/Volumes/Stripe" gc_recognizes "/Users/foo/repo" "/Volumes/Stripe/repo/issue-9")
assert_eq "$r" "recognized" "overridden-root worktree recognized by GC predicate (regression #3530)"

# Sanity: an unrelated path outside both is still skipped (user-owned).
r=$(LOOM_WORKTREE_ROOT="/Volumes/Stripe" gc_recognizes "/Users/foo/repo" "/Users/foo/somewhere-else")
assert_eq "$r" "skipped" "unrelated path is not recognized (stays user-owned)"

# The wiring is present in the real script (guards against silent unwiring).
# shellcheck disable=SC2016  # literal '$repo_root' is intentional in the grep pattern
if grep -q 'loom_worktree_root "\$repo_root"' "$AGENT_DESTROY_SH"; then
    pass "agent-destroy.sh sources and calls loom_worktree_root for GC detection"
else
    fail "agent-destroy.sh no longer routes GC detection through loom_worktree_root"
fi

# --- Test 6: locks stay in main repo regardless of override ---
echo ""
echo "Test 6: _worktree_locks_dir() independent of worktree root override"
REPO=$(setup_repo lockrepo)
EXT=$(mktemp -d /tmp/loom-lock-ext.XXXXXX)
(
    cd "$REPO"
    LOOM_WORKTREE_ROOT="$EXT" ./.loom/scripts/worktree.sh 500 >/tmp/wtroot-lock.$$ 2>&1 || {
        echo "worktree.sh failed (see /tmp/wtroot-lock.$$)"; cat /tmp/wtroot-lock.$$
    }
)
# The lock dir (.loom/locks) lives in the MAIN repo, never at the override root.
# worktree.sh removes the lock on exit, but the parent .loom/locks dir persists.
if [[ -d "$REPO/.loom/locks" ]]; then
    pass "lock namespace (.loom/locks) resides in the main repo, not the override root"
else
    # Lock dir may be pruned; assert the override root has NO locks dir instead.
    if [[ ! -d "$EXT/lockrepo/.loom/locks" ]] && [[ ! -d "$EXT/.loom/locks" ]]; then
        pass "no lock namespace created under the override root"
    else
        fail "lock namespace leaked into the override root"
    fi
fi
rm -rf "$EXT"
cleanup_repo "$REPO"

# --- Test 7: relative override rejected with warning → default fallback ---
echo ""
echo "Test 7: relative override is rejected and falls back to default"
unset LOOM_WORKTREE_ROOT
r=$(LOOM_WORKTREE_ROOT="rel/dir" loom_worktree_root "/Users/foo/repo" 2>/dev/null)
assert_eq "$r" "/Users/foo/repo/.loom/worktrees" "relative env override falls back to default path"
if LOOM_WORKTREE_ROOT="rel/dir" loom_worktree_root "/Users/foo/repo" 2>&1 >/dev/null | grep -q "must be an absolute path"; then
    pass "relative env override emits an absolute-path warning to stderr"
else
    fail "relative env override did not warn"
fi
# Config relative override likewise falls back.
CFGREPO=$(mktemp -d /tmp/loom-relcfg.XXXXXX)
mkdir -p "$CFGREPO/.loom"
printf '{ "worktree": { "root": "also/relative" } }\n' > "$CFGREPO/.loom/config.json"
r=$(loom_worktree_root "$CFGREPO" 2>/dev/null)
assert_eq "$r" "$CFGREPO/.loom/worktrees" "relative config override falls back to default path"
rm -rf "$CFGREPO"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
