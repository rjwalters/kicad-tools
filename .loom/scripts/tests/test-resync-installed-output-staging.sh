#!/usr/bin/env bash
# test-resync-installed-output-staging.sh - resync-installed.sh's --output
# staging worktree: which HEAD it is based on (#9550) and what its printed
# staging recipe is allowed to `git add` (#9141)
#
# Split out of test-resync-installed.sh (frozen by the file-size ratchet,
# .loom/docs/file-size-policy.md) rather than grown in place.
#
#   (1) #9550 -- --output creates a DETACHED staging worktree, and it was
#       always created at the PRIMARY checkout's HEAD. The documented use case
#       is running --output from a linked feature worktree (the #4563
#       restriction forbids writing to the primary from there), so when the
#       feature branch already carried commits the staged commit's parent was
#       not on that branch: it could not be fast-forwarded in, had to be
#       cherry-picked, and went dangling if the staging worktree was removed
#       first.
#   (2) #9141 -- the printed "turn this into a commit" recipe was
#       `git add -A -- . ':!<six credential paths>'`, an EXCLUSION list, which
#       stages every path nobody thought to list. That is the shape of command
#       behind commit a9da48c2, which swept a whole
#       `.loom/tokens.shadow-disabled-<ts>/` token-pool copy (21 live `.token`
#       files) into a resync commit because the exclusion named `.loom/tokens`
#       and the copy was its sibling.
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-output-staging.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/resync-installed.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-output.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# A dogfood repo (owns its own defaults/) on `main`, plus a linked worktree on
# `feature/x` whose branch is one commit AHEAD of main -- the exact shape #9550
# reports. `.loom/hooks/guard.sh` is stale against defaults/, so every resync
# below has real work to do; the install subject matches the routine-lineage
# pattern so the #7864 local-fix guard never gates it.
REPO="$WORKDIR/repo"
mkdir -p "$REPO/defaults/hooks" "$REPO/defaults/scripts" \
         "$REPO/.loom/hooks" "$REPO/.loom/scripts"
git -C "$REPO" init -q -b main
printf 'A\n' > "$REPO/defaults/hooks/guard.sh"
chmod +x "$REPO/defaults/hooks/guard.sh"
printf 'OLD\n' > "$REPO/.loom/hooks/guard.sh"
printf '{\n  "version": "9.9.9"\n}\n' > "$REPO/package.json"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
    > "$REPO/.loom/install-metadata.json"
git -C "$REPO" add -A >/dev/null 2>&1
git -C "$REPO" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
MAIN_HEAD="$(git -C "$REPO" rev-parse HEAD)"

WT="$WORKDIR/wt"
git -C "$REPO" worktree add -q -b feature/x "$WT" >/dev/null 2>&1
printf 'feature work\n' > "$WT/feature-file.txt"
git -C "$WT" add -A >/dev/null 2>&1
git -C "$WT" commit -qm "feat: an earlier commit on the feature branch" >/dev/null 2>&1
WT_HEAD="$(git -C "$WT" rev-parse HEAD)"

if [[ "$WT_HEAD" != "$MAIN_HEAD" ]]; then
    pass "fixture precondition: the invoking worktree's HEAD is ahead of the primary checkout's"
else
    fail "fixture precondition unmet: the two HEADs are equal, so this suite cannot tell them apart"
fi

# drop_staging <dir> -- unregister a staging worktree the script intentionally
# left behind (KEEP_STAGING_WORKTREE on a successful run).
drop_staging() {
    git -C "$REPO" worktree remove --force "$1" >/dev/null 2>&1 || rm -rf "$1"
}

echo ""
echo "Test group 1: --output from a linked worktree stages at THAT worktree's HEAD (#9550)"
STAGING1="$WORKDIR/staging1"
OUT1="$(cd "$WT" && bash "$SCRIPT" --output "$STAGING1" 2>&1)"
RC1=$?
if [[ $RC1 -eq 0 ]]; then
    pass "(#9550) the run succeeds"
else
    fail "(#9550) the run failed (rc=$RC1); out=$OUT1"
fi
STAGING1_HEAD="$(git -C "$STAGING1" rev-parse HEAD 2>/dev/null || true)"
if [[ "$STAGING1_HEAD" == "$WT_HEAD" ]]; then
    pass "(#9550) the staging worktree is based on the invoking worktree's HEAD"
else
    fail "(#9550) staging HEAD '$STAGING1_HEAD' is not the invoking HEAD '$WT_HEAD' (primary was '$MAIN_HEAD')"
fi
# The point of basing it there: a commit made in the staging worktree
# fast-forwards onto the branch the operator ran it from.
if git -C "$REPO" merge-base --is-ancestor "$STAGING1_HEAD" "$WT_HEAD" 2>/dev/null; then
    pass "(#9550) a commit staged there fast-forwards onto the invoking branch"
else
    fail "(#9550) the staging base is not an ancestor of the invoking branch"
fi
if grep -q "based on $WT_HEAD" <<<"$OUT1" && grep -q "invoking checkout" <<<"$OUT1"; then
    pass "(#9550) the run states which HEAD the staging worktree was based on"
else
    fail "(#9550) the run did not state the staging base; out=$OUT1"
fi
if ! grep -q "cherry-pick" <<<"$OUT1"; then
    pass "(#9550) no cherry-pick hint when the base IS the invoking HEAD"
else
    fail "(#9550) a cherry-pick hint was printed for a fast-forwardable base; out=$OUT1"
fi
# The staged resync really happened in the staging worktree, and NOT in the
# primary checkout or the invoking worktree.
if [[ "$(cat "$STAGING1/.loom/hooks/guard.sh")" == "A" ]] \
    && [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]] \
    && [[ "$(cat "$WT/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(#6106) the resync landed in the staging worktree only"
else
    fail "(#6106) the resync escaped the staging worktree"
fi

echo ""
echo "Test group 2: the printed staging recipe is an allowlist, never an exclusion list (#9141)"
if grep -q "git add -- " <<<"$OUT1"; then
    pass "(#9141) the recipe stages an explicit path list (\`git add -- …\`)"
else
    fail "(#9141) the recipe does not use an explicit path list; out=$OUT1"
fi
if ! grep -q "git add -A" <<<"$OUT1"; then
    pass "(#9141) no \`git add -A\` anywhere in the printed next steps"
else
    fail "(#9141) the printed next steps still contain \`git add -A\`; out=$OUT1"
fi
if ! grep -q ":!" <<<"$OUT1"; then
    pass "(#9141) no \`:!\` exclusion pathspec anywhere in the printed next steps"
else
    fail "(#9141) the printed next steps still contain a \`:!\` exclusion list; out=$OUT1"
fi
# Everything on the allowlist must be a resync-managed surface -- in
# particular the feature branch's own file must not appear, even though a
# `git add -A` in that worktree would have picked up anything untracked.
ADD_LINE="$(grep -o 'git add -- .*' <<<"$OUT1" | head -1 | sed 's/\x1b\[[0-9;]*m//g')"
if [[ -n "$ADD_LINE" ]] && grep -q '\.loom/hooks/guard\.sh' <<<"$ADD_LINE"; then
    pass "(#9141) the allowlist names the file this run actually wrote"
else
    fail "(#9141) the allowlist does not name the written file (line='$ADD_LINE')"
fi
if ! grep -q 'feature-file\.txt' <<<"$ADD_LINE"; then
    pass "(#9141) the allowlist carries no path outside the resync-managed surfaces"
else
    fail "(#9141) the allowlist swept in an unrelated path (line='$ADD_LINE')"
fi
drop_staging "$STAGING1"

echo ""
echo "Test group 3: --base overrides the default and gets a cherry-pick hint (#9550)"
STAGING2="$WORKDIR/staging2"
OUT2="$(cd "$WT" && bash "$SCRIPT" --output "$STAGING2" --base main 2>&1)"
RC2=$?
STAGING2_HEAD="$(git -C "$STAGING2" rev-parse HEAD 2>/dev/null || true)"
if [[ $RC2 -eq 0 ]] && [[ "$STAGING2_HEAD" == "$MAIN_HEAD" ]]; then
    pass "(#9550) --base <ref> bases the staging worktree on that ref"
else
    fail "(#9550) --base did not take effect (rc=$RC2, head='$STAGING2_HEAD'); out=$OUT2"
fi
if grep -q "cherry-pick" <<<"$OUT2" && grep -q "$WT_HEAD" <<<"$OUT2"; then
    pass "(#9550) the completion message suggests cherry-pick when the invoking branch is ahead"
else
    fail "(#9550) no cherry-pick hint for an explicitly-based staging worktree; out=$OUT2"
fi
drop_staging "$STAGING2"

echo ""
echo "Test group 4: LOOM_RESYNC_BASE is equivalent to --base (#9550)"
STAGING3="$WORKDIR/staging3"
OUT3="$(cd "$WT" && LOOM_RESYNC_BASE=main bash "$SCRIPT" --output "$STAGING3" 2>&1)"
if [[ "$(git -C "$STAGING3" rev-parse HEAD 2>/dev/null || true)" == "$MAIN_HEAD" ]]; then
    pass "(#9550) LOOM_RESYNC_BASE behaves exactly like --base"
else
    fail "(#9550) LOOM_RESYNC_BASE was ignored; out=$OUT3"
fi
drop_staging "$STAGING3"

echo ""
echo "Test group 5: an unresolvable --base is refused before anything is created (#9550)"
STAGING4="$WORKDIR/staging4"
OUT4="$(cd "$WT" && bash "$SCRIPT" --output "$STAGING4" --base no-such-ref 2>&1)"
RC4=$?
if [[ $RC4 -eq 1 ]] && grep -q "not a commit in this repository" <<<"$OUT4" && [[ ! -e "$STAGING4" ]]; then
    pass "(#9550) a bad --base exits 1, says why, and leaves no staging worktree behind"
else
    fail "(#9550) a bad --base was not refused cleanly (rc=$RC4, dir exists=$([[ -e "$STAGING4" ]] && echo yes || echo no)); out=$OUT4"
fi

echo ""
echo "Test group 6: --base without --output is reported, not silently ignored (#9550)"
OUT5="$(cd "$REPO" && bash "$SCRIPT" --dry-run --base main 2>&1)"
if grep -q "only used by --output" <<<"$OUT5"; then
    pass "(#9550) --base without --output warns"
else
    fail "(#9550) --base without --output was silently ignored; out=$OUT5"
fi

echo ""
echo "Test group 7: run from the PRIMARY checkout, the base is unchanged from pre-#9550 behaviour"
STAGING5="$WORKDIR/staging5"
OUT6="$(cd "$REPO" && bash "$SCRIPT" --output "$STAGING5" 2>&1)"
if [[ "$(git -C "$STAGING5" rev-parse HEAD 2>/dev/null || true)" == "$MAIN_HEAD" ]]; then
    pass "(#9550) invoking from the primary checkout still stages at the primary's HEAD"
else
    fail "(#9550) the primary-checkout case regressed; out=$OUT6"
fi
drop_staging "$STAGING5"

# --- summary -----------------------------------------------------------------
echo ""
echo "========================================"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
echo "========================================"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}$TESTS_FAILED test(s) failed${NC}"
    exit 1
fi
echo -e "${GREEN}All tests passed${NC}"
exit 0
