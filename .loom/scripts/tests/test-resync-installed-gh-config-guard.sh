#!/usr/bin/env bash
# test-resync-installed-gh-config-guard.sh - the printed `--output` staging
# mode "next steps" recipe can never stage a credential path, whatever that
# path is called (#7818, #8005, #9141).
#
# Split out of test-resync-installed.sh (which is frozen by the file-size
# ratchet, .loom/docs/file-size-policy.md) rather than grown in place -- see
# that file's own header for the full fixture-based test catalog this one
# does not duplicate.
#
# Background: `.loom/gh-config/` and `.loom/gh-config-by-owner/` are the
# daemon-owned GH_CONFIG_DIR trees holding host-local GitHub App installation
# tokens (#4458/#5401). They are in loom-daemon's managed `.gitignore` block
# (post_init.rs EPHEMERAL_PATTERNS), but a resync commit on rjwalters/anvil
# (2026-08-23) landed before that fix existed and swept a live token into a
# public repo via a bare `git add -A`.
#
# HOW THE CONTRACT CHANGED (#9141). #7818's fix, which this suite used to pin,
# was an EXCLUSION list: `git add -A -- . ':!<each known credential path>'`.
# That holds only for the paths somebody predicted, and commit a9da48c2 proved
# it does not hold in general -- it swept an entire token-pool COPY
# (`.loom/tokens.shadow-disabled-<ts>/`: 21 live `.token` files plus the pool's
# `.ranking` / `.rotation_cursor` bookkeeping) into a resync commit, because
# the exclusion named `.loom/tokens` and the copy was its sibling. So the
# recipe is now an ALLOWLIST of the paths the run actually wrote. This suite
# pins the stronger property that buys: not "the six known credential paths
# are excluded" but "NOTHING outside the resync-managed surfaces can be
# staged", which needs no list of credential paths to stay current.
#
# Both groups run the command the script ACTUALLY emitted, against a staging
# worktree with NO `.gitignore` at all, so the recipe itself is the only thing
# standing between a seeded credential and the index.
#
# `land-resync-commit.sh` (the OTHER path that commits "chore: resync
# installed Loom surfaces") has its own dedicated coverage for the same
# contract in test-land-resync-commit.sh tests (n), (p4) and (q2).
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-gh-config-guard.sh

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-ghconfig.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder (trimmed copy of test-resync-installed.sh's) -----------
# Just enough of a defaults/ + .loom/ tree for resync-installed.sh to run
# --output to completion; the printed next-steps text doesn't depend on what
# actually drifted.
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts/lib" \
             "$repo/.loom/hooks" "$repo/.loom/scripts/lib"
    git -C "$repo" init -q

    printf 'A\n' > "$repo/defaults/hooks/guard.sh"
    printf 'S\n' > "$repo/defaults/scripts/foo.sh"
    printf 'L\n' > "$repo/defaults/scripts/lib/bar.sh"
    chmod +x "$repo/defaults/hooks/guard.sh" "$repo/defaults/scripts/foo.sh" \
             "$repo/defaults/scripts/lib/bar.sh"

    printf 'OLD\n' > "$repo/.loom/hooks/guard.sh"
    printf 'S\n'   > "$repo/.loom/scripts/foo.sh"

    # Version source + metadata re-stamp target.
    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"

    git -C "$repo" add -A >/dev/null 2>&1
    # A routine install-subject commit -- this fixture isn't exercising the
    # local-fix guard (#7864), and a non-routine "fixture" subject now trips
    # it, blocking the --output run this test actually cares about.
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1

    echo "$repo"
}

echo "Test group 1: --output staging mode's printed next-steps stage an allowlist, never an exclusion list (#9141)"
REPO="$(make_fixture)"
STAGE="$WORKDIR/output-stage"
rm -rf "$STAGE"
OUT="$(cd "$REPO" && bash "$SCRIPT" --output "$STAGE" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(#7818) --output apply exits 0"
else
    fail "(#7818) --output apply exits 0 (got $RC)"
fi
# Capture the recipe line ONCE into a variable and match against that, rather
# than piping `grep -A1` into `grep -qF`: a pipe into an early-exit consumer
# under `pipefail` can SIGPIPE the producer and report a spurious failure
# (scripts/check-pipefail-early-exit.sh, #7790).
ADD_LINE_CTX="$(grep -A1 "git add" <<< "$OUT")"
if grep -q "git add -- " <<< "$ADD_LINE_CTX"; then
    pass "(#9141) the next-steps recipe stages an explicit path list (sanity: this suite would be vacuous otherwise)"
else
    fail "(#9141) no 'git add -- <paths>' suggestion found at all — test fixture/assumptions are stale (out=$OUT)"
fi
# The two shapes #9141 retired. `-A` stages everything not excluded; a `:!`
# pathspec is the exclusion list that made that "safe" only for predicted
# paths. Neither may reappear -- an allowlist that also carries `-A` is not an
# allowlist.
if ! grep -q "git add -A" <<< "$OUT"; then
    pass "(#9141) no 'git add -A' anywhere in the printed next steps"
else
    fail "(#9141) the next-steps recipe still prints 'git add -A' (out=$OUT)"
fi
if ! grep -qF -- ":!" <<< "$OUT"; then
    pass "(#9141) no ':!' exclusion pathspec anywhere in the printed next steps"
else
    fail "(#9141) the next-steps recipe still prints a ':!' exclusion pathspec (out=$OUT)"
fi

echo ""
echo "Test group 2: the emitted recipe, actually run, stages no credential path — listed or not (#7818/#8005/#9141)"
# Belt-and-braces: don't just assert the printed string, prove the emitted
# command really does what it claims when an operator/agent pastes it into a
# shell.
#
# #8006: run the command the script ACTUALLY emitted -- extracted from
# $ADD_LINE_CTX above -- never a hand-maintained literal copy of it. A
# hardcoded copy would only re-prove git's pathspec semantics (never in doubt)
# and would keep passing after the script's own recipe regressed: exactly the
# circular-fixture smell judge.md names, where both sides of the comparison come
# from the fixture instead of from the subject under test.
#
# Strip the ANSI bold/reset codes print_output_mode_next_steps() wraps each
# recipe line in, then take everything from `git add` to end of line. Both
# filters read a HERE-STRING, not a pipe: an early-exit consumer (`grep -m1`)
# at the end of a pipeline can SIGPIPE its producer under `pipefail`
# (scripts/check-pipefail-early-exit.sh, #7790).
#
# In --output mode the script prints the allowlist TWICE -- once in
# suggest_commit_if_resync_only_dirt()'s one-liner (`cd <dir> && git add -- …
# && git commit …`) and once in print_output_mode_next_steps()' step list.
# Both are built from the same RESYNC_DIRT_PATHS array, so either is a valid
# sample; this takes the first. Everything from the first ` && ` on is dropped
# so only the `git add` runs: the chained `git commit` would consume the index
# and leave `git diff --cached` empty, making every check below vacuous for a
# reason that has nothing to do with what was staged.
ADD_LINE_PLAIN="$(sed -e $'s/\033\\[[0-9;]*m//g' <<< "$ADD_LINE_CTX")"
ADD_CMD="$(grep -m1 -o "git add -- .*" <<< "$ADD_LINE_PLAIN")"
ADD_CMD="${ADD_CMD%% && *}"
if [[ -n "$ADD_CMD" ]]; then
    pass "(#9141) the emitted git add command was extracted verbatim from the script's own output: $ADD_CMD"
else
    fail "(#9141) could not extract the emitted git add command to execute (ctx=$ADD_LINE_CTX)"
fi
# #8005: seed EVERY member of the credential class (post_init.rs
# CREDENTIAL_PATTERNS), not just the two gh-config trees #7818 started with.
#
# #9141: plus the two paths that are NOT in that class by name -- the a9da48c2
# token-pool copy and a `-` separated sibling of it. Under the retired
# exclusion recipe these are the ones that leaked; under an allowlist they are
# no different from any other unmanaged path, which is the whole point. A
# credential location nobody has thought of yet behaves exactly like these two.
CRED_FILES=(
    .loom/gh-config/hosts.yml
    .loom/gh-config-by-owner/some-owner/hosts.yml
    .loom/tokens/acct-1.token
    .loom/accounts.env
    .loom/api-keys/zai/acct.env
    .loom/claude-config/builder-1/.credentials.json
    .loom/tokens.shadow-disabled-20260926T021559Z/acct-1.token
    .loom/tokens.shadow-disabled-20260926T021559Z/.rotation_cursor
    .loom/tokens-archive/acct-2.token
)
for f in "${CRED_FILES[@]}"; do
    mkdir -p "$STAGE/$(dirname "$f")"
    printf 'live-secret-dummy\n' > "$STAGE/$f"
done
# #8006/#8005: the recipe is belt-and-braces FOR A HOST WHOSE MANAGED
# .gitignore BLOCK IS MISSING OR STALE -- and resync-installed.sh refreshes
# that block in this very staging worktree, which then lists every credential
# path. Leave it in place and even a BARE `git add -A` skips them, so this
# group would pass no matter what the script emitted. So STRIP the managed
# block, leaving a .gitignore that ignores nothing: the emitted command is
# then the only thing that can keep a credential out of the index.
#
# Stripped rather than deleted, deliberately. `.gitignore` is itself a resync
# surface (#9345), so the run's allowlist NAMES it -- and `git add -- <paths>`
# fails the whole invocation if any named path is absent, which would stage
# nothing and make every check below vacuous. A present-but-stale block is
# also the more faithful model of the hosts #7818 documents: the file exists,
# its Loom block is out of date.
printf '# stale: no loom-managed block on this host\n' > "$STAGE/.gitignore"
STILL_IGNORED=0
for f in "${CRED_FILES[@]}"; do
    git -C "$STAGE" check-ignore -q "$f" && STILL_IGNORED=1
done
if [[ "$STILL_IGNORED" -eq 0 ]]; then
    pass "(#8005) fixture now models a host whose .gitignore ignores no credential path (the emitted recipe is the only guard left)"
else
    fail "(#8005) a credential path is still gitignored — this group would pass regardless of what was emitted"
fi
# Run it the way an operator pasting the suggestion into a shell would.
(cd "$STAGE" && bash -c "$ADD_CMD")
STAGED="$(git -C "$STAGE" diff --cached --name-only)"
# Sensitivity guard: an empty stage would make the assertions below pass for
# the wrong reason (nothing staged => no credential path staged), so prove the
# emitted command really added this run's own resync output.
if [[ -n "$STAGED" ]]; then
    pass "(#7818) the emitted command really staged this run's resync output (the checks below are not vacuous)"
else
    fail "(#7818) the emitted command staged nothing at all — the checks below would pass vacuously (cmd=$ADD_CMD)"
fi
for f in "${CRED_FILES[@]}"; do
    if ! grep -qxF "$f" <<< "$STAGED"; then
        pass "(#8005/#9141) $f was not staged by the printed recipe"
    else
        fail "(#8005/#9141) credential path $f was staged: $STAGED"
    fi
    if [[ -f "$STAGE/$f" ]]; then
        pass "(#8005) $f is left on disk, untouched"
    else
        fail "(#8005) $f was unexpectedly removed"
    fi
done
# The property an exclusion list could never have: EVERYTHING staged is a
# resync-managed surface. A path is in the index only because the recipe named
# it, so there is no "everything else" bucket for a future credential location
# to hide in. Asserted positively rather than as one more exclusion, so a new
# unmanaged path fails here by default instead of needing to be predicted.
UNMANAGED=""
while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    case "$p" in
        .loom/hooks/*|.loom/scripts/*|.loom/roles/*|.loom/docs/*|.loom/bin/*|\
        .loom/runtimes/*|.agents/skills/*|.claude/commands/loom/*|\
        .claude/README.md|.github/CONFIGURATION.md|.loom/biome.jsonc|\
        .claude/biome.jsonc|.loom/install-metadata.json|.loom/CLAUDE.md|\
        .gitattributes|.gitignore) ;;
        *) UNMANAGED+="$p"$'\n' ;;
    esac
done <<< "$STAGED"
if [[ -z "$UNMANAGED" ]]; then
    pass "(#9141) every staged path is a resync-managed surface — the recipe has no 'everything else'"
else
    fail "(#9141) the recipe staged path(s) outside the resync-managed surfaces: $UNMANAGED"
fi
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
