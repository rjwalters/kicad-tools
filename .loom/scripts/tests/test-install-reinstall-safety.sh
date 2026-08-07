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
# Strategy: install.sh's loom_daemon_binary_stale() is pure and side-effect
# free, so it is extracted via awk (same pattern as
# test-install-source-guard.sh) and exercised in an isolated harness rather
# than running the full installer end-to-end. The --confirm-reinstall gate
# and the uninstall staging scope are tested by actually invoking
# install.sh / uninstall-loom.sh against throwaway temp git repos.
#
# Usage:
#   bash defaults/scripts/tests/test-install-reinstall-safety.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"
UNINSTALL_SH="$REPO_ROOT/scripts/uninstall-loom.sh"

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

if [[ ! -f "$INSTALL_SH" ]]; then
    echo "ERROR: $INSTALL_SH not found" >&2
    exit 1
fi
if [[ ! -f "$UNINSTALL_SH" ]]; then
    echo "ERROR: $UNINSTALL_SH not found" >&2
    exit 1
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
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    exit 1
fi
echo "================================================================"
echo "All tests passed."
