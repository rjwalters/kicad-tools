#!/usr/bin/env bash
# test-resync-installed-gh-config-guard.sh - the printed `--output` staging
# mode "next steps" never suggest a bare `git add -A` that could sweep a live
# GitHub App installation token into a commit (#7818).
#
# Split out of test-resync-installed.sh (which is frozen by the file-size
# ratchet, .loom/docs/file-size-policy.md) rather than grown in place -- see
# that file's own header for the full fixture-based test catalog this one
# does not duplicate.
#
# Background: `.loom/gh-config/` and `.loom/gh-config-by-owner/` are the
# daemon-owned GH_CONFIG_DIR trees holding host-local GitHub App installation
# tokens (#4458/#5401). They are now in loom-daemon's managed `.gitignore`
# block (post_init.rs EPHEMERAL_PATTERNS), but a resync commit on
# rjwalters/anvil (2026-08-23) landed before that fix existed and swept a live
# token into a public repo via a bare `git add -A`. This suite pins the
# belt-and-braces fix in `print_output_mode_next_steps()`: the printed
# staging-worktree "next steps" recipe must exclude both credential trees via
# an explicit pathspec, unconditionally -- so the suggestion is safe to
# copy-paste even on a host whose `.gitignore` is missing/stale.
#
# `land-resync-commit.sh` (the OTHER path that commits "chore: resync
# installed Loom surfaces") has its own dedicated coverage for the same
# belt-and-braces contract in test-land-resync-commit.sh test (n).
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

echo "Test group 1: --output staging mode's printed next-steps exclude the credential class from git add (#7818/#8005)"
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
if grep -q "git add -A" <<< "$OUT"; then
    pass "(#7818) the next-steps recipe still suggests a git add -A (sanity: this test would be vacuous otherwise)"
else
    fail "(#7818) no 'git add -A' suggestion found at all — test fixture/assumptions are stale (out=$OUT)"
fi
# Capture the `git add -A` line plus its continuation ONCE into a variable and
# match against that, rather than piping `grep -A1` into `grep -qF`: a pipe into
# an early-exit consumer under `pipefail` can SIGPIPE the producer and report a
# spurious failure (scripts/check-pipefail-early-exit.sh, #7790).
ADD_LINE_CTX="$(grep -A1 "git add -A" <<< "$OUT")"
if grep -q "git add -A" <<< "$OUT" && \
   grep -qF -- ":!.loom/gh-config" <<< "$ADD_LINE_CTX" && \
   grep -qF -- ":!.loom/gh-config-by-owner" <<< "$ADD_LINE_CTX"; then
    pass "(#7818) the git add -A line's own pathspec excludes both .loom/gh-config and .loom/gh-config-by-owner"
else
    fail "(#7818) the git add -A suggestion does not exclude the credential trees (out=$OUT)"
fi

echo ""
echo "Test group 2: the excluding pathspec works against a real git add, for the whole credential class (#7818/#8005)"
# Belt-and-braces: don't just assert the printed string, prove the emitted
# pathspec really does what it claims against a real `git add -A` invocation,
# the same way an operator/agent following the suggestion would run it.
#
# #8006: run the command the script ACTUALLY emitted -- extracted from
# $ADD_LINE_CTX above -- never a hand-maintained literal copy of it. A
# hardcoded copy would only re-prove git's pathspec semantics (never in doubt)
# and would keep passing after the script's own pathspec regressed: exactly the
# circular-fixture smell judge.md names, where both sides of the comparison come
# from the fixture instead of from the subject under test.
#
# Strip the ANSI bold/reset codes print_output_mode_next_steps() wraps each
# recipe line in, then take everything from `git add` to end of line. Both
# filters read a HERE-STRING, not a pipe: an early-exit consumer (`grep -m1`)
# at the end of a pipeline can SIGPIPE its producer under `pipefail`
# (scripts/check-pipefail-early-exit.sh, #7790).
ADD_LINE_PLAIN="$(sed -e $'s/\033\\[[0-9;]*m//g' <<< "$ADD_LINE_CTX")"
ADD_CMD="$(grep -m1 -o "git add -A.*" <<< "$ADD_LINE_PLAIN")"
if [[ -n "$ADD_CMD" ]]; then
    pass "(#7818) the emitted git add command was extracted verbatim from the script's own output: $ADD_CMD"
else
    fail "(#7818) could not extract the emitted git add command to execute (ctx=$ADD_LINE_CTX)"
fi
# #8005: seed EVERY member of the credential class (post_init.rs
# CREDENTIAL_PATTERNS), not just the two gh-config trees #7818 started with.
CRED_FILES=(
    .loom/gh-config/hosts.yml
    .loom/gh-config-by-owner/some-owner/hosts.yml
    .loom/tokens/acct-1.token
    .loom/accounts.env
    .loom/api-keys/zai/acct.env
    .loom/claude-config/builder-1/.credentials.json
)
for f in "${CRED_FILES[@]}"; do
    mkdir -p "$STAGE/$(dirname "$f")"
    printf 'live-secret-dummy\n' > "$STAGE/$f"
done
# #8006/#8005: the pathspec is belt-and-braces FOR A HOST WHOSE .gitignore IS
# MISSING OR STALE -- and resync-installed.sh refreshes the loom-managed
# .gitignore block in this very staging worktree, which already lists every
# credential path. Leave it in place and a BARE `git add -A` skips them too,
# so this group would pass no matter what pathspec the script emitted. Remove
# the .gitignore ENTIRELY (the #8005 acceptance state) so the emitted pathspec
# is the ONLY thing that can keep a credential out of the index.
rm -f "$STAGE/.gitignore"
STILL_IGNORED=0
for f in "${CRED_FILES[@]}"; do
    git -C "$STAGE" check-ignore -q "$f" && STILL_IGNORED=1
done
if [[ ! -e "$STAGE/.gitignore" && "$STILL_IGNORED" -eq 0 ]]; then
    pass "(#8005) fixture now models a host with NO .gitignore (the pathspec is the only guard left)"
else
    fail "(#8005) a credential path is still gitignored — this group would pass regardless of the emitted pathspec"
fi
# Run it the way an operator pasting the suggestion into a shell would.
(cd "$STAGE" && bash -c "$ADD_CMD")
STAGED="$(git -C "$STAGE" diff --cached --name-only)"
# Sensitivity guard: an empty stage would make the exclusion assertion below
# pass for the wrong reason (nothing staged => no credential path staged), so
# prove the emitted command really added the worktree's non-credential content.
if [[ -n "$STAGED" ]]; then
    pass "(#7818) the emitted command really staged the staging worktree's content (the exclusion check below is not vacuous)"
else
    fail "(#7818) the emitted command staged nothing at all — the exclusion check below would pass vacuously (cmd=$ADD_CMD)"
fi
for f in "${CRED_FILES[@]}"; do
    if ! grep -qxF "$f" <<< "$STAGED"; then
        pass "(#8005) $f was not staged by the printed pathspec"
    else
        fail "(#8005) credential path $f was staged: $STAGED"
    fi
    if [[ -f "$STAGE/$f" ]]; then
        pass "(#8005) $f is left on disk, untouched"
    else
        fail "(#8005) $f was unexpectedly removed"
    fi
done
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
