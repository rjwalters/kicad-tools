#!/usr/bin/env bash
# test-install-reinstall-safety.sh - Regression tests for issue #4188
# ("Harvest (fork #55): harden installer reinstall safety + target-aware
# setup-mcp.sh"), ported from gpeyton/loom commit f064aeef.
#
# Covers the installer-safety findings that remain in scope on this repo's
# restructured installer (see issue #4188 for the full harvest triage):
#   1. --quick / --yes / --full must not silently bypass the
#      destructive-reinstall acknowledgement gate -- a non-interactive
#      reinstall over an existing .loom/ install now requires
#      --confirm-reinstall (install.sh).
#   2. Quick Install must rebuild the loom-daemon binary when the source
#      tree is newer than the binary on disk, not just when it's absent
#      (install.sh::loom_daemon_binary_stale).
#   3. `scripts/uninstall-loom.sh --local`'s staging step must only stage
#      Loom-managed paths, not unrelated untracked/staged consumer files
#      sitting in the working tree (already landed via the #3545 work --
#      this group is a regression guard, not new behavior).
#
# Two groups present in the original fork commit are intentionally NOT
# ported here:
#   - is_legacy_rjwalters_install() metadata-first detection: the function
#     does not exist on this repo's restructured installer;
#     install-metadata.json is already written and consumed as authoritative
#     (install.sh's TARGET_PATH/.loom detection), so there is nothing left to
#     regression-test under that name.
#   - Hook-dedup quote-normalization (scaffolding.rs): split out to #4200,
#     tracked as a Rust unit test there.
#
# Group 7 (issue #6509, follow-up to #6499/#6502): install.sh's reinstall-time
# `git stash pop --index` against $TARGET_PATH is routed through
# defaults/scripts/safe-stash-pop.sh (#6501) via
# scripts/install/reinstall-stash-pop.sh::_reinstall_safe_stash_pop, so a
# conflicting pop can never leave conflict markers / unmerged index entries
# behind. Exercised directly against the pure, side-effect-free helper
# function rather than the full installer end-to-end (same rationale as
# Group 2's extracted loom_daemon_binary_stale()).
#
# Strategy: install.sh's loom_daemon_binary_stale() is pure and side-effect
# free, so it is extracted via awk (same pattern as
# test-install-source-guard.sh) and exercised in an isolated harness rather
# than running the full installer end-to-end. The --confirm-reinstall gate
# and the uninstall staging scope are tested by actually invoking
# install.sh / uninstall-loom.sh against throwaway temp git repos.
#
# Source-tree-only by design (#6194): install.sh and scripts/uninstall-loom.sh
# both live at the repo root, not under defaults/, so neither is shipped into
# an installed consumer repo. This suite SKIPs (exit 0) rather than errors
# when run outside Loom's own checkout.
#
# Usage:
#   bash defaults/scripts/tests/test-install-reinstall-safety.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"
UNINSTALL_SH="$REPO_ROOT/scripts/uninstall-loom.sh"
REINSTALL_STASH_POP="$REPO_ROOT/scripts/install/reinstall-stash-pop.sh"
REAL_SAFE_STASH_POP="$REPO_ROOT/defaults/scripts/safe-stash-pop.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" == *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Looking for: '$needle'"
        echo "    In output:"
        echo "$haystack" | sed 's/^/      /'
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$haystack" != *"$needle"* ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Should NOT contain: '$needle'"
    fi
}

assert_zero_exit() {
    local actual="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" -eq 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (exit=$actual)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected 0, got $actual)"
    fi
}

assert_nonzero_exit() {
    local actual="$1" msg="$2"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$actual" -ne 0 ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg (exit=$actual)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg (expected non-zero, got $actual)"
    fi
}

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    expected: [$expected]"
        echo "    actual:   [$actual]"
    fi
}

# A tracked file carries conflict markers when it has BOTH an opening
# `<<<<<<< ` and a closing `>>>>>>> ` at line start (same narrow definition
# safe-stash-pop.sh and primary_checkout_reaper use).
has_markers() {
    grep -q '^<<<<<<< ' "$1" 2>/dev/null && grep -q '^>>>>>>> ' "$1" 2>/dev/null
}

if [[ ! -f "$INSTALL_SH" ]]; then
    echo "SKIP: source-tree-only test, $INSTALL_SH not found (not shipped into an installed repo)" >&2
    exit 0
fi
if [[ ! -f "$UNINSTALL_SH" ]]; then
    echo "SKIP: source-tree-only test, $UNINSTALL_SH not found (not shipped into an installed repo)" >&2
    exit 0
fi

# Extract a single top-level function body ("name() {" ... "}") from a file.
extract_function() {
    local func_name="$1" file="$2"
    awk -v fn="${func_name}() {" '
        $0 == fn { capture=1 }
        capture { print }
        capture && /^\}$/ { exit }
    ' "$file"
}

echo "================================================================"
echo "Group 1: --confirm-reinstall gate (issue #4188)"
echo "================================================================"
echo ""

# install.sh's dependency-check step (git/node/pnpm/cargo) runs BEFORE the
# reinstall gate this group tests, and prompts interactively if any are
# missing. Rather than risk a false failure (or a hang) on a runner missing
# one of these, skip this group with a clear message when the full
# dependency set isn't available -- Groups 2/3 don't need it.
MISSING_FOR_GROUP1=()
for dep in git node pnpm cargo; do
    command -v "$dep" &> /dev/null || MISSING_FOR_GROUP1+=("$dep")
done

if [[ ${#MISSING_FOR_GROUP1[@]} -gt 0 ]]; then
    echo -e "${YELLOW}SKIP${NC}: missing ${MISSING_FOR_GROUP1[*]} -- install.sh's dependency-check step runs before the reinstall gate"
else
    # Build a throwaway "existing install" target: any dir with a .loom/
    # subdirectory is enough to hit the reinstall gate in install.sh, since
    # the gate itself runs before any uninstall/build work happens.
    T1=$(mktemp -d /tmp/loom-confirm-reinstall-test.XXXXXX)
    mkdir -p "$T1/.loom"
    echo "sentinel" > "$T1/.loom/sentinel-marker"
    git -C "$T1" init --quiet 2>/dev/null || true

    # Case 1: --quick without --confirm-reinstall must refuse (non-zero
    # exit), print guidance mentioning --confirm-reinstall, and leave the
    # existing .loom/ untouched (no uninstall attempted). Stdin redirected
    # from /dev/null so any latent interactive prompt fails fast (EOF)
    # instead of hanging.
    OUTPUT1=$("$INSTALL_SH" --quick "$T1" < /dev/null 2>&1)
    EXIT1=$?
    assert_nonzero_exit "$EXIT1" "Case 1: --quick without --confirm-reinstall refuses"
    assert_contains "$OUTPUT1" "--confirm-reinstall" "Case 1: guidance mentions --confirm-reinstall"
    if [[ -f "$T1/.loom/sentinel-marker" ]]; then
        TESTS_RUN=$((TESTS_RUN + 1))
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 1: existing .loom/ left untouched (no destructive uninstall ran)"
    else
        TESTS_RUN=$((TESTS_RUN + 1))
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 1: existing .loom/ was modified/removed -- gate did not block in time"
    fi

    # Case 2: -y (--yes) without --quick/--confirm-reinstall must ALSO
    # refuse -- -y alone still implies NON_INTERACTIVE=true, so it must hit
    # the same gate (regression guard for "any non-interactive flag
    # combination bypasses the warning", not just --quick specifically).
    OUTPUT2=$("$INSTALL_SH" -y "$T1" < /dev/null 2>&1)
    EXIT2=$?
    assert_nonzero_exit "$EXIT2" "Case 2: -y without --confirm-reinstall refuses"
    assert_contains "$OUTPUT2" "--confirm-reinstall" "Case 2: guidance mentions --confirm-reinstall"

    # Case 3: --full without --confirm-reinstall must ALSO refuse.
    OUTPUT3=$("$INSTALL_SH" --full "$T1" < /dev/null 2>&1)
    EXIT3=$?
    assert_nonzero_exit "$EXIT3" "Case 3: --full without --confirm-reinstall refuses"
    assert_contains "$OUTPUT3" "--confirm-reinstall" "Case 3: guidance mentions --confirm-reinstall"

    rm -rf "$T1"
fi
echo ""

echo "================================================================"
echo "Group 2: loom-daemon binary staleness (issue #4188)"
echo "================================================================"
echo ""

STALE_FN=$(extract_function "loom_daemon_binary_stale" "$INSTALL_SH")
if [[ -z "$STALE_FN" ]]; then
    echo "ERROR: could not extract loom_daemon_binary_stale() from $INSTALL_SH" >&2
    exit 1
fi

HARNESS=$(mktemp /tmp/loom-stale-binary-harness.XXXXXX.sh)
{
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    printf '%s\n' "$STALE_FN"
    echo 'loom_daemon_binary_stale "$1"'
    echo 'exit $?'
} > "$HARNESS"
chmod +x "$HARNESS"

T2=$(mktemp -d /tmp/loom-stale-binary-test.XXXXXX)
mkdir -p "$T2/loom-daemon/src" "$T2/loom-api/src" "$T2/target/release"
echo 'fn main() {}' > "$T2/loom-daemon/src/main.rs"
touch "$T2/Cargo.toml" "$T2/Cargo.lock"

# Case 1: binary absent -> stale.
"$HARNESS" "$T2"
assert_zero_exit "$?" "Case 1: missing binary is stale"

# Case 2: binary present and newer than all source files -> not stale.
touch "$T2/target/release/loom-daemon"
sleep 1.1
touch "$T2/target/release/loom-daemon"
"$HARNESS" "$T2"
assert_nonzero_exit "$?" "Case 2: binary newer than source is NOT stale"

# Case 3: source file touched after the binary (simulating `git pull`
# landing a newer commit) -> stale again.
sleep 1.1
echo '// updated' >> "$T2/loom-daemon/src/main.rs"
"$HARNESS" "$T2"
assert_zero_exit "$?" "Case 3: source newer than binary IS stale (issue #4188 regression)"

rm -rf "$T2" "$HARNESS"
echo ""

echo "================================================================"
echo "Group 3: uninstall-loom.sh --local stages only Loom-managed paths"
echo "================================================================"
echo ""

T3=$(mktemp -d /tmp/loom-uninstall-scope-test.XXXXXX)
git -C "$T3" init --quiet
git -C "$T3" config user.email "test@test.com"
git -C "$T3" config user.name "Test"

# Minimal Loom footprint (heuristic/legacy removal path -- no
# install-metadata.json manifest, so uninstall-loom.sh walks defaults/).
mkdir -p "$T3/.loom/roles" "$T3/.loom/scripts"
echo '{}' > "$T3/.loom/config.json"
cat > "$T3/CLAUDE.md" <<'EOF'
# Loom Orchestration - Repository Guide

Generated by Loom Installation Process
EOF
git -C "$T3" add -A
git -C "$T3" commit -m "Initial commit with Loom installed" --quiet

# Unrelated consumer state that must NOT be touched by the uninstall's
# staging step: an untracked file, a staged-but-uncommitted new file, and a
# modification to an existing tracked (non-Loom) file.
echo "unrelated" > "$T3/unrelated-untracked.txt"
echo "console.log('hi')" > "$T3/app.js"
git -C "$T3" add app.js
echo "# My Project" > "$T3/README.md"
git -C "$T3" add README.md
git -C "$T3" commit -m "Add app.js and README.md" --quiet
echo "# My Project (locally modified, not staged)" >> "$T3/README.md"
echo "another unrelated untracked file" > "$T3/scratch-notes.txt"

"$UNINSTALL_SH" --yes --local "$T3" > /tmp/loom-uninstall-scope-test.log 2>&1
UNINSTALL_EXIT=$?

TESTS_RUN=$((TESTS_RUN + 1))
if [[ $UNINSTALL_EXIT -eq 0 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: uninstall-loom.sh --local exited 0"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: uninstall-loom.sh --local exited $UNINSTALL_EXIT"
    sed 's/^/    /' /tmp/loom-uninstall-scope-test.log
fi

STAGED_AFTER=$(git -C "$T3" diff --cached --name-only)

# The unrelated untracked files must remain untracked (NOT staged).
assert_not_contains "$STAGED_AFTER" "unrelated-untracked.txt" \
    "unrelated untracked file was not staged by uninstall"
assert_not_contains "$STAGED_AFTER" "scratch-notes.txt" \
    "second unrelated untracked file was not staged by uninstall"

# The locally-modified-but-unstaged README.md change must remain unstaged
# (git add -A would have staged it as part of the "everything" sweep).
README_STAGED_DIFF=$(git -C "$T3" diff --cached -- README.md)
TESTS_RUN=$((TESTS_RUN + 1))
if [[ -z "$README_STAGED_DIFF" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: unrelated unstaged README.md edit was not swept into the index"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: unrelated unstaged README.md edit was staged by uninstall"
fi

# Loom-managed removals (e.g. .loom/config.json) SHOULD be staged as
# deletions -- the fix must not become "stage nothing".
assert_contains "$STAGED_AFTER" ".loom/config.json" \
    "Loom-managed file removal WAS staged (fix doesn't over-correct to no-op)"

rm -rf "$T3" /tmp/loom-uninstall-scope-test.log
echo ""

echo "================================================================"
echo "Group 4: --local mode does not redirect a linked-worktree target (issue #5240)"
echo "================================================================"
echo ""

# Regression guard for issue #5240: scripts/uninstall-loom.sh:151-157
# unconditionally redirected a linked-worktree $TARGET_PATH to the main
# repository root -- fine for the default (non-local, worktree+PR) mode,
# but fatal for install.sh's --quick-reinstall / --clean call sites, which
# chain "uninstall-loom.sh --yes --local $TARGET_PATH" directly into a
# synchronous reinstall against that SAME $TARGET_PATH. When $TARGET_PATH
# was a linked worktree, the redirect silently split the two operations
# across different directories: uninstall deleted/mutated files in the
# PRIMARY checkout while the reinstall proceeded against the original
# worktree -- corrupting a live sibling checkout (the reported symptom: 158
# files deleted from a primary checkout mid-use by other sessions).
T4=$(mktemp -d /tmp/loom-worktree-redirect-test.XXXXXX)
git -C "$T4" init --quiet
git -C "$T4" config user.email "test@test.com"
git -C "$T4" config user.name "Test"

mkdir -p "$T4/.loom/roles" "$T4/.loom/scripts"
echo '{}' > "$T4/.loom/config.json"
cat > "$T4/CLAUDE.md" <<'EOF'
# Loom Orchestration - Repository Guide

Generated by Loom Installation Process
EOF
git -C "$T4" add -A
git -C "$T4" commit -m "Initial commit with Loom installed" --quiet

# A linked worktree of $T4, sharing the same .git.
T4_WT="${T4}-wt"
git -C "$T4" worktree add "$T4_WT" -b "loom-5240-wt-branch" --quiet

PRIMARY_STATUS_BEFORE="$(git -C "$T4" status --porcelain)"

# Case 1 (the fix): --local against the LINKED WORKTREE must operate on the
# worktree itself, not redirect to $T4 (the primary checkout).
OUTPUT4="$("$UNINSTALL_SH" --yes --local "$T4_WT" 2>&1 < /dev/null)"
EXIT4=$?

assert_zero_exit "$EXIT4" "Case 1: uninstall-loom.sh --local against a linked worktree exits 0"
assert_not_contains "$OUTPUT4" "Resolving to main repository root" \
    "Case 1: --local does NOT redirect a linked-worktree target to the main repository root"
assert_contains "$OUTPUT4" "Target: $T4_WT" \
    "Case 1: --local operates on the worktree path it was given"

PRIMARY_STATUS_AFTER="$(git -C "$T4" status --porcelain)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$PRIMARY_STATUS_BEFORE" == "$PRIMARY_STATUS_AFTER" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 1: primary checkout's git status is byte-identical before/after"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 1: primary checkout's git status changed"
    echo "    Before: '$PRIMARY_STATUS_BEFORE'"
    echo "    After:  '$PRIMARY_STATUS_AFTER'"
fi

# The uninstall must still have actually done its job -- in the WORKTREE,
# not silently become a no-op (the fix must not over-correct).
WORKTREE_STAGED="$(git -C "$T4_WT" diff --cached --name-only)"
assert_contains "$WORKTREE_STAGED" ".loom/config.json" \
    "Case 1: Loom-managed file removal WAS staged in the worktree (fix doesn't become a no-op)"

git -C "$T4" worktree remove "$T4_WT" --force 2>/dev/null || rm -rf "$T4_WT"

# Case 2 (existing behavior preserved): the DEFAULT (non-local, worktree+PR)
# mode must still redirect a linked-worktree target to the main repository
# root -- this is scripts/install-loom.sh's own safe use of the identical
# pattern (:590-595), and other non-local callers rely on it for repo
# identification before branching their own isolated worktree. Only the
# early redirect-decision output is asserted (before any git-push/`gh`
# network step), with a timeout so an unexpected hang can't wedge the suite.
T4_WT2="${T4}-wt2"
git -C "$T4" worktree add "$T4_WT2" -b "loom-5240-wt2-branch" --quiet
OUTPUT5="$(timeout 20 "$UNINSTALL_SH" --yes "$T4_WT2" 2>&1 < /dev/null | head -5)"
assert_contains "$OUTPUT5" "Resolving to main repository root" \
    "Case 2: default (non-local) mode still redirects a linked-worktree target"

git -C "$T4" worktree remove "$T4_WT2" --force 2>/dev/null || rm -rf "$T4_WT2"
rm -rf "$T4"
echo ""

echo "================================================================"
echo "Group 5: --dry-run (issue #5517) -- plan-only, zero mutation, exit 0"
echo "================================================================"
echo ""

# Case 1: fresh (not-yet-installed) target -- install.sh --dry-run prints a
# plan, exits 0, and leaves the target byte-for-byte empty.
T5=$(mktemp -d /tmp/loom-dry-run-fresh-test.XXXXXX)
git -C "$T5" init --quiet
git -C "$T5" config user.email "test@test.com"
git -C "$T5" config user.name "Test"
git -C "$T5" commit --allow-empty -m "init" --quiet
PRE_T5_SHA="$(git -C "$T5" rev-parse HEAD)"

OUTPUT5="$("$INSTALL_SH" --dry-run "$T5" < /dev/null 2>&1)"
EXIT5=$?
assert_zero_exit "$EXIT5" "Case 1: install.sh --dry-run on a fresh target exits 0"
assert_contains "$OUTPUT5" "DRY RUN" "Case 1: output announces a dry run"
assert_contains "$OUTPUT5" "What Will Be Installed" "Case 1: output prints the planned-writes summary"

POST_T5_SHA="$(git -C "$T5" rev-parse HEAD)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$PRE_T5_SHA" == "$POST_T5_SHA" ]] && [[ -z "$(git -C "$T5" status --porcelain)" ]] && [[ ! -d "$T5/.loom" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 1: target is untouched (no .loom/, clean git status, HEAD unchanged)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 1: target was mutated by --dry-run"
fi

rm -rf "$T5"
echo ""

# Case 2: existing install + --dry-run --confirm-reinstall WITHOUT -y/--quick.
# Mirrors the real reinstall gate exactly: NON_INTERACTIVE is false here (no
# -y/--quick/--full), so a real run would hit the interactive "Proceed with
# reinstall?" prompt -- the preview must say so, not fabricate a removal plan
# for a branch the real invocation would never reach.
T6=$(mktemp -d /tmp/loom-dry-run-noninteractive-test.XXXXXX)
mkdir -p "$T6/.loom"
echo '{}' > "$T6/.loom/config.json"
git -C "$T6" init --quiet
git -C "$T6" config user.email "test@test.com"
git -C "$T6" config user.name "Test"
git -C "$T6" add -A
git -C "$T6" commit -m "existing install" --quiet
PRE_T6_SHA="$(git -C "$T6" rev-parse HEAD)"

OUTPUT6="$("$INSTALL_SH" --dry-run --confirm-reinstall "$T6" < /dev/null 2>&1)"
EXIT6=$?
assert_zero_exit "$EXIT6" "Case 2: install.sh --dry-run --confirm-reinstall (no -y) exits 0"
assert_contains "$OUTPUT6" "would be prompted" "Case 2: preview says a real run would hit the interactive prompt"

POST_T6_SHA="$(git -C "$T6" rev-parse HEAD)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$PRE_T6_SHA" == "$POST_T6_SHA" ]] && [[ -z "$(git -C "$T6" status --porcelain)" ]] && [[ -f "$T6/.loom/config.json" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 2: existing install left completely untouched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 2: existing install was mutated by --dry-run"
fi

rm -rf "$T6"
echo ""

# Case 3: existing install + --dry-run --quick --confirm-reinstall -- this IS
# the destructive-reinstall branch (matches install.sh's own INSTALL_TYPE=="1"
# gate). The preview must show the concrete removal plan (via
# scripts/uninstall-loom.sh --dry-run) and leave the target byte-identical.
T7=$(mktemp -d /tmp/loom-dry-run-destructive-test.XXXXXX)
mkdir -p "$T7/.loom"
echo '{}' > "$T7/.loom/config.json"
git -C "$T7" init --quiet
git -C "$T7" config user.email "test@test.com"
git -C "$T7" config user.name "Test"
git -C "$T7" add -A
git -C "$T7" commit -m "existing install" --quiet
PRE_T7_SHA="$(git -C "$T7" rev-parse HEAD)"
PRE_T7_FILES="$(find "$T7" -type f -not -path '*/.git/*' | sort | xargs md5sum 2>/dev/null)"

OUTPUT7="$("$INSTALL_SH" --dry-run --quick --confirm-reinstall "$T7" < /dev/null 2>&1)"
EXIT7=$?
assert_zero_exit "$EXIT7" "Case 3: install.sh --dry-run --quick --confirm-reinstall exits 0"
assert_contains "$OUTPUT7" "DESTRUCTIVE reinstall" "Case 3: preview names the destructive-reinstall plan"
assert_contains "$OUTPUT7" ".loom/config.json" "Case 3: preview lists the concrete file(s) that would be removed"

POST_T7_SHA="$(git -C "$T7" rev-parse HEAD)"
POST_T7_FILES="$(find "$T7" -type f -not -path '*/.git/*' | sort | xargs md5sum 2>/dev/null)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$PRE_T7_SHA" == "$POST_T7_SHA" ]] && [[ "$PRE_T7_FILES" == "$POST_T7_FILES" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 3: target files are byte-identical before/after (nothing removed)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 3: target files changed despite --dry-run"
fi

rm -rf "$T7"
echo ""

# Case 4: scripts/uninstall-loom.sh --dry-run directly -- Step 1-3 build/report
# the removal manifest, exit 0 before Step 4 (worktree)/5 (removal)/6
# (smart-remove writes) ever run.
T8=$(mktemp -d /tmp/loom-dry-run-uninstall-direct-test.XXXXXX)
mkdir -p "$T8/.loom/roles" "$T8/.loom/scripts"
echo '{}' > "$T8/.loom/config.json"
git -C "$T8" init --quiet
git -C "$T8" config user.email "test@test.com"
git -C "$T8" config user.name "Test"
git -C "$T8" add -A
git -C "$T8" commit -m "existing install" --quiet
PRE_T8_SHA="$(git -C "$T8" rev-parse HEAD)"

OUTPUT8="$("$UNINSTALL_SH" --local --dry-run "$T8" < /dev/null 2>&1)"
EXIT8=$?
assert_zero_exit "$EXIT8" "Case 4: uninstall-loom.sh --local --dry-run exits 0 without -y"
assert_contains "$OUTPUT8" "DRY RUN" "Case 4: output announces a dry run"
assert_contains "$OUTPUT8" ".loom/config.json" "Case 4: output lists the concrete file(s) that would be removed"

POST_T8_SHA="$(git -C "$T8" rev-parse HEAD)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$PRE_T8_SHA" == "$POST_T8_SHA" ]] && [[ -z "$(git -C "$T8" status --porcelain)" ]] && [[ -f "$T8/.loom/config.json" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 4: target left completely untouched"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 4: target was mutated by --dry-run"
fi

rm -rf "$T8"
echo ""

echo "================================================================"
echo "Group 6: --local mode preserves live .loom/worktrees/* by default (issue #5973)"
echo "================================================================"
echo ""

# Regression guard for issue #5973: uninstall-loom.sh's RUNTIME_DIRS Step 5
# loop used to `rm -rf .loom/worktrees` unconditionally -- the whole
# directory, including every live `issue-N` worktree inside it -- with no
# dirty-check and no per-worktree naming in the output. install.sh's
# --confirm-reinstall / --clean reinstall flows chain
# "uninstall-loom.sh --yes --local" directly against the live target, so a
# plain reinstall silently destroyed any worktree an operator had
# deliberately kept checked out between sessions (branches survived; the
# working directories did not).
#
# Case 1: a CLEAN, git-registered worktree under .loom/worktrees/ must
# survive a plain `--local` uninstall (no --remove-worktrees) -- the new
# default.
T9=$(mktemp -d /tmp/loom-worktree-preserve-test.XXXXXX)
git -C "$T9" init --quiet
git -C "$T9" config user.email "test@test.com"
git -C "$T9" config user.name "Test"
mkdir -p "$T9/.loom/roles" "$T9/.loom/scripts"
echo '{}' > "$T9/.loom/config.json"
git -C "$T9" add -A
git -C "$T9" commit -m "existing install" --quiet
git -C "$T9" worktree add "$T9/.loom/worktrees/issue-100" -b "feature/issue-100" --quiet

OUTPUT9="$("$UNINSTALL_SH" --yes --local "$T9" 2>&1 < /dev/null)"
EXIT9=$?
assert_zero_exit "$EXIT9" "Case 1: uninstall-loom.sh --local (no --remove-worktrees) exits 0"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -d "$T9/.loom/worktrees/issue-100" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 1: .loom/worktrees/issue-100 directory survives on disk"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 1: .loom/worktrees/issue-100 directory was removed"
fi

WT_LIST_9="$(git -C "$T9" worktree list 2>/dev/null)"
assert_contains "$WT_LIST_9" "issue-100" \
    "Case 1: git still lists .loom/worktrees/issue-100 as a registered worktree"
assert_contains "$OUTPUT9" "Preserving" \
    "Case 1: output announces that worktrees were preserved"
assert_contains "$OUTPUT9" ".loom/worktrees/issue-100" \
    "Case 1: output names the specific preserved worktree (not just a parent-dir summary)"

rm -rf "$T9"
echo ""

# Case 2: a DIRTY (uncommitted changes) worktree must be REFUSED even with
# --remove-worktrees passed -- git's own dirty-check, not a blind rm -rf.
T10=$(mktemp -d /tmp/loom-worktree-dirty-refuse-test.XXXXXX)
git -C "$T10" init --quiet
git -C "$T10" config user.email "test@test.com"
git -C "$T10" config user.name "Test"
mkdir -p "$T10/.loom/roles" "$T10/.loom/scripts"
echo '{}' > "$T10/.loom/config.json"
git -C "$T10" add -A
git -C "$T10" commit -m "existing install" --quiet
git -C "$T10" worktree add "$T10/.loom/worktrees/issue-200" -b "feature/issue-200" --quiet
echo "uncommitted work" > "$T10/.loom/worktrees/issue-200/wip.txt"

OUTPUT10="$("$UNINSTALL_SH" --yes --local --remove-worktrees "$T10" 2>&1 < /dev/null)"
EXIT10=$?
assert_zero_exit "$EXIT10" "Case 2: uninstall-loom.sh --local --remove-worktrees exits 0 overall (one skip doesn't abort the run)"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -d "$T10/.loom/worktrees/issue-200" ]] && [[ -f "$T10/.loom/worktrees/issue-200/wip.txt" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 2: dirty worktree (and its uncommitted file) survives --remove-worktrees"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 2: dirty worktree was destroyed despite uncommitted changes"
fi

assert_contains "$OUTPUT10" "skipped" \
    "Case 2: output reports the dirty worktree as skipped, not silently dropped"
assert_contains "$OUTPUT10" ".loom/worktrees/issue-200" \
    "Case 2: output names the specific skipped worktree"

rm -rf "$T10"
echo ""

# Case 3: a CLEAN worktree IS removed when --remove-worktrees is explicitly
# passed -- the fix must not over-correct into "never remove". Uses
# `git worktree remove` (not `rm -rf`), so the branch itself survives, same
# as the pre-#5973 behavior the original report observed.
T11=$(mktemp -d /tmp/loom-worktree-optin-remove-test.XXXXXX)
git -C "$T11" init --quiet
git -C "$T11" config user.email "test@test.com"
git -C "$T11" config user.name "Test"
mkdir -p "$T11/.loom/roles" "$T11/.loom/scripts"
echo '{}' > "$T11/.loom/config.json"
git -C "$T11" add -A
git -C "$T11" commit -m "existing install" --quiet
git -C "$T11" worktree add "$T11/.loom/worktrees/issue-300" -b "feature/issue-300" --quiet

OUTPUT11="$("$UNINSTALL_SH" --yes --local --remove-worktrees "$T11" 2>&1 < /dev/null)"
EXIT11=$?
assert_zero_exit "$EXIT11" "Case 3: uninstall-loom.sh --local --remove-worktrees exits 0"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -d "$T11/.loom/worktrees/issue-300" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 3: clean worktree IS removed when --remove-worktrees is passed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 3: clean worktree survived despite --remove-worktrees"
fi

assert_contains "$OUTPUT11" "removed: .loom/worktrees/issue-300" \
    "Case 3: output names the specific removed worktree"
BRANCH_LIST_11="$(git -C "$T11" branch --list 'feature/issue-300')"
assert_contains "$BRANCH_LIST_11" "feature/issue-300" \
    "Case 3: the branch itself survives (git worktree remove, not a branch delete)"

rm -rf "$T11"
echo ""

# --------------------------------------------------------------------------
# Cases 4-8: issue #6159 -- check-in on #5999's `rm -rf` fallback.
#
# #5999 gated the fallback's `rm -rf` on a substring match against `git
# worktree remove`'s human-readable error text ("is not a working tree").
# Champion's merge hold named exactly that: misclassifying a live worktree as
# an orphan reproduces the destruction #5973 fixed. Two things follow, and
# both are asserted below:
#
#   Case 4  The string is still a live dependency ELSEWHERE -- both
#           defaults/scripts/worktree.sh's #5177 orphan branch and
#           loom-daemon/src/worktree_ops/clean.rs::is_untracked_worktree_error
#           still parse it. git makes no compatibility promise about that
#           wording, so pin it against the REAL git binary: a future git that
#           rewords it breaks CI here instead of silently changing behavior in
#           the field.
#   Case 5  uninstall-loom.sh's own fallback no longer parses that string --
#           it asks `git worktree list --porcelain` whether the path is
#           registered. A genuine orphan must still be removed.
#   Case 6  ...and a REGISTERED worktree whose removal fails for a reason
#           other than dirtiness (here: locked) must survive. This is the case
#           the error-string predicate could not distinguish on its own.
#   Case 7  The discriminating case: a `git` whose "not a worktree" error has
#           been REWORDED (a PATH shim standing in for a future git release).
#           The pre-#6159 substring predicate skips the orphan there and lets
#           .loom/worktrees/ grow forever; the membership predicate is
#           unaffected because it never reads the error text.
#   Case 8  The membership query's OWN failure mode (PR #6701 review). If
#           `git worktree list --porcelain` cannot be answered, its output is
#           empty -- indistinguishable from "no match" unless the exit status
#           is checked. A membership predicate that ignores that status reads
#           a broken query as "orphan" and `rm -rf`s a live worktree, which is
#           exactly the destruction #5973/#5999/#6159 exist to prevent. The
#           predicate must fail CLOSED: query error => treat as registered.
# --------------------------------------------------------------------------

echo "  -- Cases 4-8 (issue #6159): orphan-fallback predicate --"

# Case 4: pin git's actual error wording (acceptance criterion 4).
T12=$(mktemp -d /tmp/loom-git-wording-test.XXXXXX)
git -C "$T12" init --quiet
git -C "$T12" config user.email "test@test.com"
git -C "$T12" config user.name "Test"
git -C "$T12" commit --allow-empty -m "root" --quiet
mkdir -p "$T12/plain-directory"
GIT_WT_ERR="$(git -C "$T12" worktree remove "$T12/plain-directory" 2>&1)"
GIT_WT_RC=$?

TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$GIT_WT_RC" -ne 0 ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 4: 'git worktree remove <plain dir>' still fails (non-zero exit)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 4: 'git worktree remove <plain dir>' unexpectedly succeeded"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$(printf '%s' "$GIT_WT_ERR" | tr '[:upper:]' '[:lower:]')" == *"is not a working tree"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 4: git $(git --version | awk '{print $3}') still emits 'is not a working tree' (the string worktree.sh + clean.rs parse)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 4: git's 'not a worktree' error has been REWORDED -- update defaults/scripts/worktree.sh (#5177 branch) and loom-daemon/src/worktree_ops/clean.rs::is_untracked_worktree_error"
    echo "    git version: $(git --version)"
    echo "    actual error: $GIT_WT_ERR"
fi

rm -rf "$T12"

# Case 5: a genuine orphan (a directory under .loom/worktrees/ that git does
# NOT list as a worktree) is still removed when --remove-worktrees is passed.
T13=$(mktemp -d /tmp/loom-worktree-orphan-test.XXXXXX)
git -C "$T13" init --quiet
git -C "$T13" config user.email "test@test.com"
git -C "$T13" config user.name "Test"
mkdir -p "$T13/.loom/roles" "$T13/.loom/scripts"
echo '{}' > "$T13/.loom/config.json"
git -C "$T13" add -A
git -C "$T13" commit -m "existing install" --quiet
# Never registered with git -- the #5177 orphan shape.
mkdir -p "$T13/.loom/worktrees/issue-400"
echo "leftover build artifact" > "$T13/.loom/worktrees/issue-400/junk.txt"

OUTPUT13="$("$UNINSTALL_SH" --yes --local --remove-worktrees "$T13" 2>&1 < /dev/null)"
EXIT13=$?
assert_zero_exit "$EXIT13" "Case 5: uninstall-loom.sh --local --remove-worktrees exits 0 with an orphan present"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -d "$T13/.loom/worktrees/issue-400" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 5: orphaned (never git-registered) directory IS removed"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 5: orphaned directory survived --remove-worktrees"
fi

assert_contains "$OUTPUT13" "removed (orphaned, not git-registered): .loom/worktrees/issue-400" \
    "Case 5: output names the orphan and why it was removed"

rm -rf "$T13"

# Case 6: a REGISTERED worktree whose `git worktree remove` fails for a
# non-dirty reason (locked) must NOT take the orphan `rm -rf` path. The old
# error-string predicate happened to be safe here only because git's locked
# message differs; the membership check makes it safe by construction, for
# every removal failure git can ever report.
T14=$(mktemp -d /tmp/loom-worktree-locked-test.XXXXXX)
git -C "$T14" init --quiet
git -C "$T14" config user.email "test@test.com"
git -C "$T14" config user.name "Test"
mkdir -p "$T14/.loom/roles" "$T14/.loom/scripts"
echo '{}' > "$T14/.loom/config.json"
git -C "$T14" add -A
git -C "$T14" commit -m "existing install" --quiet
git -C "$T14" worktree add "$T14/.loom/worktrees/issue-500" -b "feature/issue-500" --quiet
echo "precious work" > "$T14/.loom/worktrees/issue-500/precious.txt"
git -C "$T14" worktree lock "$T14/.loom/worktrees/issue-500"

OUTPUT14="$("$UNINSTALL_SH" --yes --local --remove-worktrees "$T14" 2>&1 < /dev/null)"
EXIT14=$?
assert_zero_exit "$EXIT14" "Case 6: uninstall-loom.sh --local --remove-worktrees exits 0 with a locked worktree present"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -d "$T14/.loom/worktrees/issue-500" ]] && [[ -f "$T14/.loom/worktrees/issue-500/precious.txt" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 6: locked (git-registered) worktree survives -- the orphan rm -rf never fires for a registered path"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 6: locked worktree was destroyed -- the orphan fallback misclassified a registered worktree"
fi

assert_contains "$OUTPUT14" "skipped" \
    "Case 6: output reports the locked worktree as skipped"

git -C "$T14" worktree unlock "$T14/.loom/worktrees/issue-500" 2>/dev/null || true
rm -rf "$T14"

# Case 7: the discriminating case. Stand a shim `git` in front of the real one
# that rewords ONLY the "is not a working tree" failure of `git worktree
# remove` -- exactly what a future git release is free to do -- and delegates
# everything else untouched. With the pre-#6159 substring predicate the orphan
# below is skipped and accumulates forever; with the porcelain-membership
# predicate it is removed, because the predicate never reads the error text.
REAL_GIT="$(command -v git)"
T15=$(mktemp -d /tmp/loom-worktree-reworded-git-test.XXXXXX)
SHIM_DIR=$(mktemp -d /tmp/loom-git-shim.XXXXXX)
cat > "$SHIM_DIR/git" <<SHIM
#!/usr/bin/env bash
# Test shim: a hypothetical future git that reworded its "not a worktree"
# error. Only 'git worktree remove' is intercepted; everything else execs the
# real binary.
REAL="$REAL_GIT"
prev=""
is_wt_remove=false
for a in "\$@"; do
  [[ "\$prev" == "worktree" && "\$a" == "remove" ]] && is_wt_remove=true
  prev="\$a"
done
if [[ "\$is_wt_remove" == true ]]; then
  out="\$("\$REAL" "\$@" 2>&1)"; rc=\$?
  if (( rc != 0 )); then
    printf '%s\n' "\${out//is not a working tree/does not appear to be a linked checkout}" >&2
  else
    printf '%s\n' "\$out"
  fi
  exit \$rc
fi
exec "\$REAL" "\$@"
SHIM
chmod +x "$SHIM_DIR/git"

"$REAL_GIT" -C "$T15" init --quiet
"$REAL_GIT" -C "$T15" config user.email "test@test.com"
"$REAL_GIT" -C "$T15" config user.name "Test"
mkdir -p "$T15/.loom/roles" "$T15/.loom/scripts"
echo '{}' > "$T15/.loom/config.json"
"$REAL_GIT" -C "$T15" add -A
"$REAL_GIT" -C "$T15" commit -m "existing install" --quiet
mkdir -p "$T15/.loom/worktrees/issue-600"
echo "leftover" > "$T15/.loom/worktrees/issue-600/junk.txt"

# Sanity: the shim really does reword the error the old predicate matched.
SHIM_ERR="$(PATH="$SHIM_DIR:$PATH" git -C "$T15" worktree remove "$T15/.loom/worktrees/issue-600" 2>&1)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$SHIM_ERR" != *"is not a working tree"* ]] && [[ "$SHIM_ERR" == *"linked checkout"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 7: shim git reproduces a reworded 'not a worktree' error"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 7: shim git did not reword the error (fixture broken): $SHIM_ERR"
fi

OUTPUT15="$(PATH="$SHIM_DIR:$PATH" "$UNINSTALL_SH" --yes --local --remove-worktrees "$T15" 2>&1 < /dev/null)"
EXIT15=$?
assert_zero_exit "$EXIT15" "Case 7: uninstall-loom.sh --local --remove-worktrees exits 0 under a reworded-error git"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ ! -d "$T15/.loom/worktrees/issue-600" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 7: orphan is still removed when git's error wording changes (predicate is not error-text-coupled)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 7: orphan survived a reworded git error -- the fallback predicate is still parsing error text"
fi

assert_contains "$OUTPUT15" "removed (orphaned, not git-registered): .loom/worktrees/issue-600" \
    "Case 7: output still classifies it as an orphan under a reworded-error git"

rm -rf "$T15" "$SHIM_DIR"

# Case 8: the membership query itself fails (PR #6701 review finding). Shim a
# `git` whose `worktree list --porcelain` errors out -- a transient I/O error,
# lock contention on .git/worktrees/ metadata, a permissions problem, repo
# corruption -- while every other subcommand (including `worktree add`,
# `worktree lock`, `worktree remove`) delegates untouched. The worktree below
# is genuinely registered AND locked, so `git worktree remove` legitimately
# refuses it and the fallback predicate runs. A predicate that only looks at
# the listing's *output* sees nothing and concludes "orphan"; the `rm -rf` then
# destroys a live worktree. The predicate must check the query's exit status
# and fail CLOSED (treat an unanswerable query as "registered").
REAL_GIT="$(command -v git)"
T16=$(mktemp -d /tmp/loom-worktree-listfail-test.XXXXXX)
SHIM_DIR2=$(mktemp -d /tmp/loom-git-shim-listfail.XXXXXX)
cat > "$SHIM_DIR2/git" <<SHIM2
#!/usr/bin/env bash
# Test shim: a git whose 'worktree list' query cannot be answered. Everything
# else execs the real binary, so the worktree really is registered and locked.
REAL="$REAL_GIT"
prev=""
is_wt_list=false
for a in "\$@"; do
  [[ "\$prev" == "worktree" && "\$a" == "list" ]] && is_wt_list=true
  prev="\$a"
done
if [[ "\$is_wt_list" == true ]]; then
  printf 'fatal: simulated transient git error\n' >&2
  exit 128
fi
exec "\$REAL" "\$@"
SHIM2
chmod +x "$SHIM_DIR2/git"

"$REAL_GIT" -C "$T16" init --quiet
"$REAL_GIT" -C "$T16" config user.email "test@test.com"
"$REAL_GIT" -C "$T16" config user.name "Test"
mkdir -p "$T16/.loom/roles" "$T16/.loom/scripts"
echo '{}' > "$T16/.loom/config.json"
"$REAL_GIT" -C "$T16" add -A
"$REAL_GIT" -C "$T16" commit -m "existing install" --quiet
"$REAL_GIT" -C "$T16" worktree add "$T16/.loom/worktrees/issue-700" -b "feature/issue-700" --quiet
echo "precious work" > "$T16/.loom/worktrees/issue-700/precious.txt"
"$REAL_GIT" -C "$T16" worktree lock "$T16/.loom/worktrees/issue-700"

# Sanity 1: the shim really does break the membership query...
TESTS_RUN=$((TESTS_RUN + 1))
if PATH="$SHIM_DIR2:$PATH" git -C "$T16" worktree list --porcelain >/dev/null 2>&1; then
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 8: shim git did not fail 'worktree list --porcelain' (fixture broken)"
else
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 8: shim git fails 'worktree list --porcelain' (query unanswerable)"
fi

# Sanity 2: ...while `worktree remove` still fails for its own, unrelated
# reason (locked) -- so the fallback predicate is genuinely reached.
SHIM2_ERR="$(PATH="$SHIM_DIR2:$PATH" git -C "$T16" worktree remove "$T16/.loom/worktrees/issue-700" 2>&1)"
TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$SHIM2_ERR" == *"locked"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 8: 'worktree remove' still refuses the locked worktree through the shim"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 8: 'worktree remove' did not report a locked worktree (fixture broken): $SHIM2_ERR"
fi

OUTPUT16="$(PATH="$SHIM_DIR2:$PATH" "$UNINSTALL_SH" --yes --local --remove-worktrees "$T16" 2>&1 < /dev/null)"
EXIT16=$?
assert_zero_exit "$EXIT16" "Case 8: uninstall-loom.sh --local --remove-worktrees exits 0 when the membership query fails"

TESTS_RUN=$((TESTS_RUN + 1))
if [[ -d "$T16/.loom/worktrees/issue-700" ]] && [[ -f "$T16/.loom/worktrees/issue-700/precious.txt" ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 8: registered worktree SURVIVES an unanswerable membership query (predicate fails closed)"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 8: live worktree was rm -rf'd because 'git worktree list' failed -- the predicate fails OPEN"
fi

TESTS_RUN=$((TESTS_RUN + 1))
if [[ "$OUTPUT16" != *"removed (orphaned, not git-registered): .loom/worktrees/issue-700"* ]]; then
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: Case 8: a failed query is never reported as 'orphaned, not git-registered'"
else
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: Case 8: a failed query was misclassified as an orphan"
fi

assert_contains "$OUTPUT16" "skipped" \
    "Case 8: output reports the preserved worktree as skipped"

"$REAL_GIT" -C "$T16" worktree unlock "$T16/.loom/worktrees/issue-700" 2>/dev/null || true
rm -rf "$T16" "$SHIM_DIR2"
echo ""

echo "================================================================"
echo "Group 7: reinstall stash-pop safety (issue #6509, follow-up to #6499/#6502)"
echo "================================================================"
echo ""

if [[ ! -f "$REINSTALL_STASH_POP" ]]; then
    echo -e "${YELLOW}SKIP${NC}: source-tree-only helper, $REINSTALL_STASH_POP not found"
elif [[ ! -f "$REAL_SAFE_STASH_POP" ]]; then
    echo -e "${YELLOW}SKIP${NC}: $REAL_SAFE_STASH_POP not found (safe-stash-pop.sh, #6501)"
else
    # shellcheck source=/dev/null
    source "$REINSTALL_STASH_POP"

    export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
    export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"
    export GIT_CONFIG_NOSYSTEM=1

    G7_WORKDIR=$(mktemp -d /tmp/loom-reinstall-stash-pop-test.XXXXXX)
    # A loom_root with NEITHER a source-tree nor a target-local
    # safe-stash-pop.sh available -- exercises the raw-pop fallback (#6509
    # Availability note: older install / partial tree / curl-piped standalone
    # install predating #6501).
    G7_NO_WRAPPER_ROOT="$G7_WORKDIR/no-wrapper-root"
    mkdir -p "$G7_NO_WRAPPER_ROOT/defaults/scripts"

    echo "-- Case 1: wrapper resolution prefers <target>/.loom/scripts/ over the source tree --"
    G7_T1="$G7_WORKDIR/t1"
    git init --quiet "$G7_T1"
    git -C "$G7_T1" checkout -q -b main
    printf 'a\n' > "$G7_T1/f.txt"
    git -C "$G7_T1" add f.txt
    git -C "$G7_T1" commit -q -m c1
    mkdir -p "$G7_T1/.loom/scripts"
    cp "$REAL_SAFE_STASH_POP" "$G7_T1/.loom/scripts/safe-stash-pop.sh"
    chmod +x "$G7_T1/.loom/scripts/safe-stash-pop.sh"
    printf 'a\nwip\n' > "$G7_T1/f.txt"
    git -C "$G7_T1" stash push -q -m wip

    _reinstall_safe_stash_pop "$REPO_ROOT" "$G7_T1" "stash@{0}"
    assert_eq "wrapper" "$REINSTALL_POP_MODE" "Case 1: target-local wrapper: mode=wrapper"
    assert_eq "$G7_T1/.loom/scripts/safe-stash-pop.sh" "$REINSTALL_POP_WRAPPER" \
        "Case 1: target-local copy is preferred over the source-tree copy"
    assert_eq "clean" "$REINSTALL_POP_RESULT" "Case 1: target-local wrapper: clean pop result"
    assert_eq "0" "$REINSTALL_POP_STATUS" "Case 1: target-local wrapper: clean pop status 0"

    echo "-- Case 2: falls back to <loom_root>/defaults/scripts/ with no target-local copy --"
    G7_T2="$G7_WORKDIR/t2"
    git init --quiet "$G7_T2"
    git -C "$G7_T2" checkout -q -b main
    printf 'a\n' > "$G7_T2/f.txt"
    git -C "$G7_T2" add f.txt
    git -C "$G7_T2" commit -q -m c1
    printf 'a\nwip\n' > "$G7_T2/f.txt"
    git -C "$G7_T2" stash push -q -m wip

    _reinstall_safe_stash_pop "$REPO_ROOT" "$G7_T2" "stash@{0}"
    assert_eq "wrapper" "$REINSTALL_POP_MODE" "Case 2: source-tree fallback: mode=wrapper"
    assert_eq "$REPO_ROOT/defaults/scripts/safe-stash-pop.sh" "$REINSTALL_POP_WRAPPER" \
        "Case 2: source-tree copy used when no target-local copy exists"
    assert_eq "clean" "$REINSTALL_POP_RESULT" "Case 2: source-tree fallback: clean pop result"
    assert_contains "$(cat "$G7_T2/f.txt")" "wip" "Case 2: source-tree fallback: content restored"
    assert_eq "0" "$(git -C "$G7_T2" stash list | wc -l | tr -d ' ')" \
        "Case 2: source-tree fallback: stash entry consumed"

    echo "-- Case 3: --index staged/unstaged split preserved on the clean path (#3611) --"
    G7_T3="$G7_WORKDIR/t3"
    git init --quiet "$G7_T3"
    git -C "$G7_T3" checkout -q -b main
    printf 'line1\nline2\n' > "$G7_T3/f.txt"
    printf 'orig\n' > "$G7_T3/g.txt"
    git -C "$G7_T3" add f.txt g.txt
    git -C "$G7_T3" commit -q -m c1
    # Stage one hunk, leave the other unstaged, then stash both -- --index is
    # what reproduces the split on pop.
    printf 'line1\nline2\nSTAGED\n' > "$G7_T3/f.txt"
    git -C "$G7_T3" add f.txt
    printf 'orig\nUNSTAGED\n' > "$G7_T3/g.txt"
    git -C "$G7_T3" stash push -q -m wip

    _reinstall_safe_stash_pop "$REPO_ROOT" "$G7_T3" "stash@{0}"
    assert_eq "clean" "$REINSTALL_POP_RESULT" "Case 3: --index split: clean pop result"
    assert_contains "$(git -C "$G7_T3" diff --staged --name-only)" "f.txt" \
        "Case 3: --index split: f.txt's hunk came back STAGED"
    assert_contains "$(git -C "$G7_T3" diff --name-only)" "g.txt" \
        "Case 3: --index split: g.txt's hunk came back UNSTAGED"

    echo "-- Case 4: THE INCIDENT SHAPE (#6499/#6502) -- a conflicting reinstall pop --"
    echo "   must never leave conflict markers or unmerged entries in \$TARGET_PATH"
    G7_T4="$G7_WORKDIR/t4"
    git init --quiet "$G7_T4"
    git -C "$G7_T4" checkout -q -b main
    mkdir -p "$G7_T4/.loom"
    printf '{\n  "safehouse": {"socket": "/committed/loom.sock"}\n}\n' > "$G7_T4/.loom/config.json"
    git -C "$G7_T4" add .loom/config.json
    git -C "$G7_T4" commit -q -m "add config"
    # Stash a host-specific edit, then have the "upstream reinstall" rewrite
    # the same lines -- the same shape as the real incident (a
    # `loom-daemon init` rewrite landing on top of a host-specific stashed
    # edit).
    printf '{\n  "safehouse": {"socket": "/host/patched.sock"}\n}\n' > "$G7_T4/.loom/config.json"
    git -C "$G7_T4" stash push -q -m wip
    printf '{\n  "safehouse": {"socket": "/upstream/new.sock"}\n}\n' > "$G7_T4/.loom/config.json"
    git -C "$G7_T4" add .loom/config.json
    git -C "$G7_T4" commit -q -m upstream

    _reinstall_safe_stash_pop "$REPO_ROOT" "$G7_T4" "stash@{0}"
    assert_eq "wrapper" "$REINSTALL_POP_MODE" "Case 4: incident shape: mode=wrapper"
    assert_eq "restored" "$REINSTALL_POP_RESULT" \
        "Case 4: incident shape: result=restored (conflict, rolled back)"
    assert_nonzero_exit "$REINSTALL_POP_STATUS" "Case 4: incident shape: non-zero status signals a conflict"
    TESTS_RUN=$((TESTS_RUN + 1))
    if has_markers "$G7_T4/.loom/config.json"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 4: incident shape: NO conflict markers left in .loom/config.json"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 4: incident shape: NO conflict markers left in .loom/config.json"
    fi
    assert_eq "" "$(git -C "$G7_T4" ls-files --unmerged)" "Case 4: incident shape: no unmerged index entries remain"
    assert_eq "" "$(git -C "$G7_T4" status --porcelain)" "Case 4: incident shape: working tree is clean again"
    assert_eq "1" "$(git -C "$G7_T4" stash list | wc -l | tr -d ' ')" \
        "Case 4: incident shape: the stash entry is PRESERVED (nothing discarded)"
    assert_contains "$REINSTALL_POP_OUTPUT" ".loom/config.json" \
        "Case 4: incident shape: REINSTALL_POP_OUTPUT names the conflicting file"

    echo "-- Case 5: fallback -- clean pop still works when neither wrapper copy exists --"
    G7_T5="$G7_WORKDIR/t5"
    git init --quiet "$G7_T5"
    git -C "$G7_T5" checkout -q -b main
    printf 'a\n' > "$G7_T5/f.txt"
    git -C "$G7_T5" add f.txt
    git -C "$G7_T5" commit -q -m c1
    printf 'a\nwip\n' > "$G7_T5/f.txt"
    git -C "$G7_T5" stash push -q -m wip

    _reinstall_safe_stash_pop "$G7_NO_WRAPPER_ROOT" "$G7_T5" "stash@{0}"
    assert_eq "raw" "$REINSTALL_POP_MODE" "Case 5: no-wrapper fallback: mode=raw"
    assert_eq "clean" "$REINSTALL_POP_RESULT" "Case 5: no-wrapper fallback: clean pop result"
    assert_eq "0" "$REINSTALL_POP_STATUS" "Case 5: no-wrapper fallback: clean pop status 0"
    assert_contains "$(cat "$G7_T5/f.txt")" "wip" "Case 5: no-wrapper fallback: content restored"

    echo "-- Case 6: fallback -- a conflicting pop reproduces today's pre-#6509 raw-pop --"
    echo "   behavior exactly (the explicitly permitted fallback shape)"
    G7_T6="$G7_WORKDIR/t6"
    git init --quiet "$G7_T6"
    git -C "$G7_T6" checkout -q -b main
    printf 'line1\nline2\nline3\n' > "$G7_T6/f.txt"
    git -C "$G7_T6" add f.txt
    git -C "$G7_T6" commit -q -m c1
    printf 'STASHED\nline2\nline3\n' > "$G7_T6/f.txt"
    git -C "$G7_T6" stash push -q -m wip
    printf 'UPSTREAM\nline2\nline3\n' > "$G7_T6/f.txt"
    git -C "$G7_T6" add f.txt
    git -C "$G7_T6" commit -q -m upstream

    _reinstall_safe_stash_pop "$G7_NO_WRAPPER_ROOT" "$G7_T6" "stash@{0}"
    assert_eq "raw" "$REINSTALL_POP_MODE" "Case 6: no-wrapper conflict fallback: mode=raw"
    assert_eq "raw_conflict" "$REINSTALL_POP_RESULT" "Case 6: no-wrapper conflict fallback: result=raw_conflict"
    assert_nonzero_exit "$REINSTALL_POP_STATUS" "Case 6: no-wrapper conflict fallback: non-zero status"
    assert_eq "1" "$(git -C "$G7_T6" stash list | wc -l | tr -d ' ')" \
        "Case 6: no-wrapper conflict fallback: the stash entry is PRESERVED (nothing discarded)"
    # This is the one branch where the pre-#6509 limitation is EXPECTED to
    # reproduce: a raw `git stash pop --index` conflict leaves markers behind.
    # Confirming that here proves the fallback genuinely IS the documented
    # raw-pop-with-warning behavior, not a silently-different new behavior.
    TESTS_RUN=$((TESTS_RUN + 1))
    if has_markers "$G7_T6/f.txt"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 6: markers ARE left (today's documented, permitted fallback shape)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 6: markers ARE left (today's documented, permitted fallback shape)"
        echo "    no markers found -- fixture no longer reproduces a raw-pop conflict"
    fi

    echo "-- Case 7: install.sh wiring -- sources the helper and calls the function --"
    echo "   (not an inline raw pop), with reapply sequenced after the pop"
    if grep -q 'source "\$LOOM_ROOT/scripts/install/reinstall-stash-pop.sh"' "$INSTALL_SH"; then
        TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 7: install.sh sources scripts/install/reinstall-stash-pop.sh"
    else
        TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 7: install.sh sources scripts/install/reinstall-stash-pop.sh"
    fi

    G7_CALL_LINE=$(grep -n '_reinstall_safe_stash_pop "\$LOOM_ROOT" "\$TARGET_PATH"' "$INSTALL_SH" | head -1 | cut -d: -f1)
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ -n "$G7_CALL_LINE" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 7: install.sh's reinstall block calls _reinstall_safe_stash_pop"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 7: install.sh's reinstall block calls _reinstall_safe_stash_pop"
    fi

    # The old inline raw pop this issue replaces must be gone from the
    # REINSTALL_STASHED_USER_CHANGES block (it still legitimately exists
    # INSIDE reinstall-stash-pop.sh's own fallback branch, which install.sh
    # does not duplicate).
    if grep -q 'REINSTALL_POP_OUTPUT="\$(git -C "\$TARGET_PATH" stash pop --index 2>&1)"' "$INSTALL_SH"; then
        TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 7: install.sh's reinstall block no longer inlines a raw 'git stash pop --index'"
        echo "    the pre-#6509 inline raw pop is still present verbatim"
    else
        TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 7: install.sh's reinstall block no longer inlines a raw 'git stash pop --index'"
    fi

    # Sequencing (#6509 issue body): on the wrapper's "restored" conflict
    # path, the REINSTALL_RESET_PATHS reapply-from-snapshot loop must run
    # AFTER the _reinstall_safe_stash_pop call, not before -- the wrapper's
    # rollback lands each reset path back at its pre-pop (HEAD-reset) state,
    # and only the reapply loop puts the fresh post-init Loom content back.
    G7_RESTORED_LINE=$(grep -n '"\$REINSTALL_POP_RESULT" == "restored"' "$INSTALL_SH" | head -1 | cut -d: -f1)
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ -n "$G7_CALL_LINE" && -n "$G7_RESTORED_LINE" && "$G7_RESTORED_LINE" -gt "$G7_CALL_LINE" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: Case 7: the 'restored' branch's reset-path reapply is sequenced after the pop call"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: Case 7: the 'restored' branch's reset-path reapply is sequenced after the pop call"
        echo "    call line=$G7_CALL_LINE restored-branch line=$G7_RESTORED_LINE"
    fi

    rm -rf "$G7_WORKDIR"
fi
echo ""

echo "================================================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "================================================================"
echo "All tests passed."
