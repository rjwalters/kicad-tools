#!/usr/bin/env bash
# test-resync-installed-local-fix-guard.sh - local-divergence protection in
# resync-installed.sh's sync_one() (#7864)
#
# Split out of test-resync-installed.sh (which is frozen by the file-size
# ratchet, .loom/docs/file-size-policy.md) rather than grown in place -- see
# that file's own header for the full fixture-based test catalog this one
# does not duplicate, and its make_fixture() for why the fixture commit
# message there deliberately matches the "routine resync" lineage pattern
# this suite is built around.
#
# Reproduces the incident this protection exists for, first hit and fixed
# downstream at `2AMLogic/sky130-modexp` (its #98/#100, fixed in PR #117):
# sync_one() used to overwrite an installed file unconditionally whenever it
# differed from defaults/, with no diff review and no way to tell "upstream
# moved forward" from "upstream's defaults/ has not caught up with a fix that
# landed directly on the INSTALLED copy". A resync in that second state
# silently reverted a merged, tested guard-hook fix this way.
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-local-fix-guard.sh

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-localfix.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder (trimmed copy of test-resync-installed.sh's) -----------
# Just enough of a defaults/ + .loom/ tree for resync-installed.sh to run to
# completion. defaults/hooks/guard.sh stays at "A" throughout this file --
# every test below layers its OWN commit on .loom/hooks/guard.sh on top of
# this baseline to model a specific lineage shape.
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

    # A real commit so loom_commit re-stamps to an actual short sha. #7864:
    # the message deliberately matches RESYNC_COMMIT_SUBJECT_RE ("safe
    # lineage") -- this is the ordinary "just installed, never individually
    # patched" baseline every test below diverges from on purpose.
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1

    echo "$repo"
}

# --- reproduction: a direct fix landed on the installed copy is BLOCKED -----
#
# Simulates the #98/#100 incident shape directly: a fix landed with a commit
# DIRECTLY on the installed copy (not via a resync commit), and defaults/ has
# not caught up yet (still the pre-fix content, "A"). A resync that would
# silently overwrite the fix must instead be blocked, not applied.
echo "Test group 1: local-divergence protection blocks a resync that would revert a direct fix (#7864)"
REPO="$(make_fixture)"
printf 'A\nGUARD-FIX-LINE\n' > "$REPO/.loom/hooks/guard.sh"
git -C "$REPO" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO" commit -qm "fix(guard): resolve_var() now substitutes a mid-token embedded \$VAR reference" >/dev/null 2>&1
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(#7864) a resync that would revert a direct fix exits 1"
else
    fail "(#7864) expected exit 1 when a direct fix would be reverted (got $RC)"
fi
if grep -q "BLOCKED" <<<"$OUT" && grep -q "hooks/guard.sh" <<<"$OUT"; then
    pass "(#7864) the blocked file is named in the summary"
else
    fail "(#7864) the blocked file was not named in the summary; out=$OUT"
fi
if grep -q "looks like a local fix" <<<"$OUT" && grep -q "diff -u" <<<"$OUT"; then
    pass "(#7864) the WARN names the review command"
else
    fail "(#7864) the WARN did not name a diff -u review command; out=$OUT"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == $'A\nGUARD-FIX-LINE' ]]; then
    pass "(#7864) the direct fix was NOT reverted"
else
    fail "(#7864) the direct fix was silently reverted despite the protection"
fi

# --- --dry-run previews the block without writing ---------------------------
echo "Test group 2: --dry-run previews the block without writing (#7864)"
REPO2="$(make_fixture)"
printf 'A\nGUARD-FIX-LINE\n' > "$REPO2/.loom/hooks/guard.sh"
git -C "$REPO2" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO2" commit -qm "fix(guard): direct hotfix" >/dev/null 2>&1
OUT="$(cd "$REPO2" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(#7864) --dry-run with a would-be-blocked file exits 2"
else
    fail "(#7864) --dry-run with a would-be-blocked file exits 2 (got $RC)"
fi
if grep -q "blocked" <<<"$OUT" && [[ "$(cat "$REPO2/.loom/hooks/guard.sh")" == $'A\nGUARD-FIX-LINE' ]]; then
    pass "(#7864) --dry-run reports the block and writes nothing"
else
    fail "(#7864) --dry-run either did not report the block or modified the file; out=$OUT"
fi

# --- --force applies the update anyway ---------------------------------------
echo "Test group 3: --force applies the update anyway (#7864)"
OUT="$(cd "$REPO" && bash "$SCRIPT" --force 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(#7864) --force exits 0"
else
    fail "(#7864) --force exits 0 (got $RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(#7864) --force applies the update despite the local divergence"
else
    fail "(#7864) --force did not apply the update"
fi
if grep -qi "forcing" <<<"$OUT"; then
    pass "(#7864) --force logs that it overrode the protection"
else
    fail "(#7864) --force gave no indication it overrode the protection; out=$OUT"
fi

# --- LOOM_RESYNC_FORCE=1 is equivalent to --force ----------------------------
echo "Test group 3b: LOOM_RESYNC_FORCE=1 is equivalent to --force (#7864)"
REPO3B="$(make_fixture)"
printf 'A\nGUARD-FIX-LINE\n' > "$REPO3B/.loom/hooks/guard.sh"
git -C "$REPO3B" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO3B" commit -qm "fix(guard): direct hotfix" >/dev/null 2>&1
OUT="$(cd "$REPO3B" && LOOM_RESYNC_FORCE=1 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && [[ "$(cat "$REPO3B/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(#7864) LOOM_RESYNC_FORCE=1 applies the update exactly like --force"
else
    fail "(#7864) LOOM_RESYNC_FORCE=1 did not behave like --force (rc=$RC)"
fi

# --- regression: no local divergence still resyncs silently -----------------
#
# The overwhelmingly common case -- an installed file whose only history is
# ordinary install/resync lineage -- must keep applying automatically even
# when the update removes/replaces existing lines.
echo "Test group 4: no local divergence -- an ordinary line-replacing update still applies automatically (#7864)"
REPO4="$(make_fixture)"
if [[ "$(cat "$REPO4/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(#7864) fixture precondition: hooks/guard.sh has no local-fix commit (lineage is install-only)"
else
    fail "(#7864) fixture precondition unmet for the no-divergence regression test"
fi
OUT="$(cd "$REPO4" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && [[ "$(cat "$REPO4/.loom/hooks/guard.sh")" == "A" ]] && ! grep -q "BLOCKED" <<<"$OUT"; then
    pass "(#7864) an ordinary update with no local divergence applies without --force"
else
    fail "(#7864) an ordinary update with no local divergence was unexpectedly blocked (rc=$RC); out=$OUT"
fi

# --- a pure-addition update never gates, even when diverged -----------------
echo "Test group 5: a pure-addition update never gates, even on a diverged file (#7864)"
REPO5="$(make_fixture)"
printf 'A\nLOCAL-FIX-LINE\n' > "$REPO5/.loom/hooks/guard.sh"
git -C "$REPO5" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO5" commit -qm "fix(guard): a direct local fix" >/dev/null 2>&1
# defaults/ now adds a NEW line on top of "A" without removing anything the
# installed copy already has -- everything dst has is a subset of the new src.
printf 'A\nLOCAL-FIX-LINE\nNEW-UPSTREAM-LINE\n' > "$REPO5/defaults/hooks/guard.sh"
OUT="$(cd "$REPO5" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && ! grep -q "BLOCKED" <<<"$OUT"; then
    pass "(#7864) a pure-addition update applies automatically even on a diverged file"
else
    fail "(#7864) a pure-addition update was unexpectedly blocked on a diverged file (rc=$RC); out=$OUT"
fi
if [[ "$(cat "$REPO5/.loom/hooks/guard.sh")" == $'A\nLOCAL-FIX-LINE\nNEW-UPSTREAM-LINE' ]]; then
    pass "(#7864) the pure-addition update was applied"
else
    fail "(#7864) the pure-addition update was not applied"
fi

# --- .loom/resync-ignore is unaffected by the new gate -----------------------
echo "Test group 6: .loom/resync-ignore still short-circuits before the local-divergence gate (#7864)"
REPO6="$(make_fixture)"
printf 'A\nLOCAL-FIX-LINE\n' > "$REPO6/.loom/hooks/guard.sh"
git -C "$REPO6" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO6" commit -qm "fix(guard): a direct local fix" >/dev/null 2>&1
printf 'hooks/guard.sh  # keep my local fix\n' > "$REPO6/.loom/resync-ignore"
OUT="$(cd "$REPO6" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "skipped" <<<"$OUT" && ! grep -q "BLOCKED" <<<"$OUT"; then
    pass "(#7864) a resync-ignore pin reports skipped, not blocked, and exits 0"
else
    fail "(#7864) a resync-ignore-pinned diverged file was reported incorrectly (rc=$RC); out=$OUT"
fi
if [[ "$(cat "$REPO6/.loom/hooks/guard.sh")" == $'A\nLOCAL-FIX-LINE' ]]; then
    pass "(#7864) the resync-ignore-pinned local fix was preserved"
else
    fail "(#7864) the resync-ignore-pinned local fix was overwritten"
fi

# --- a file with no git history is never gated -------------------------------
#
# A file that was NEVER committed in this repo (genuinely no `git log`
# entries at all for that path -- the ordinary consumer-repo case where
# .loom/ isn't tracked, just written directly on disk) has nothing to
# protect: dst_diverged_from_resync_lineage() must treat "no history" as "not
# diverged", not crash or false-positive on the empty `git log -1` output.
# Built by hand (not make_fixture(), whose single commit already touches
# hooks/guard.sh) so this path's history is provably empty.
echo "Test group 7: a file with no git history is never gated (#7864)"
REPO7="$WORKDIR/repo-nohistory"
rm -rf "$REPO7"
mkdir -p "$REPO7/defaults/hooks" "$REPO7/.loom/hooks"
git -C "$REPO7" init -q
printf 'A\n' > "$REPO7/defaults/hooks/guard.sh"
chmod +x "$REPO7/defaults/hooks/guard.sh"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$REPO7" > "$REPO7/.loom/install-metadata.json"
git -C "$REPO7" add -A >/dev/null 2>&1
git -C "$REPO7" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
# Written directly to disk AFTER the commit above -- never added, never
# committed, so `git log -1 -- .loom/hooks/guard.sh` returns nothing for it.
printf 'OLD\nUNCOMMITTED-LOCAL-EDIT\n' > "$REPO7/.loom/hooks/guard.sh"
if [[ -z "$(git -C "$REPO7" log -1 --format='%s' -- .loom/hooks/guard.sh 2>/dev/null)" ]]; then
    pass "(#7864) fixture precondition: hooks/guard.sh has no git history at all"
else
    fail "(#7864) fixture precondition unmet: hooks/guard.sh unexpectedly has git history"
fi
OUT="$(cd "$REPO7" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && ! grep -q "BLOCKED" <<<"$OUT" && [[ "$(cat "$REPO7/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(#7864) a no-history installed file is never gated -- update applies automatically"
else
    fail "(#7864) a no-history installed file was incorrectly blocked (rc=$RC); out=$OUT"
fi

# Exercise the real installer through its commit, stopping at the first push.
# The wrapper prevents network access; all local Git operations remain real.
echo "Test group 8: real installer commit provenance"
INSTALLER="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)/scripts/install/create-pr.sh"
export LOOM_TEST_REAL_GIT
LOOM_TEST_REAL_GIT="$(command -v git)"
mkdir -p "$WORKDIR/git-bin"
cat > "$WORKDIR/git-bin/git" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "push" ]]; then
    echo "TEST_STOP_BEFORE_PUSH" >&2
    exit 97
fi
exec "$LOOM_TEST_REAL_GIT" "$@"
SH
chmod +x "$WORKDIR/git-bin/git"
for INSTALL_CASE in normal skip-ci custom-fix; do
    REPO8="$(make_fixture)"
    git -C "$REPO8" remote add origin https://github.com/example/fixture.git
    printf 'OLD\nINSTALLER-CONTENT\n' > "$REPO8/.loom/hooks/guard.sh"
    SKIP_CASE=false
    CUSTOM_MESSAGE=""
    [[ "$INSTALL_CASE" == skip-ci ]] && SKIP_CASE=true
    [[ "$INSTALL_CASE" == custom-fix ]] && CUSTOM_MESSAGE="fix(guard): preserve a custom installer hotfix"
    BEFORE_INSTALL="$(git -C "$REPO8" rev-parse HEAD)"
    INSTALL_OUT="$(PATH="$WORKDIR/git-bin:$PATH" LOOM_VERSION=0.19.60 \
        LOOM_COMMIT=fixture SKIP_TARGET_CI="$SKIP_CASE" COMMIT_MSG="$CUSTOM_MESSAGE" \
        bash "$INSTALLER" "$REPO8" main 2>&1)"
    INSTALL_RC=$?
    if [[ $INSTALL_RC -ne 0 ]] && [[ "$BEFORE_INSTALL" != "$(git -C "$REPO8" rev-parse HEAD)" ]] \
        && [[ "$INSTALL_OUT" == *TEST_STOP_BEFORE_PUSH* ]]; then
        pass "real installer committed $INSTALL_CASE and stopped before network access"
    else
        fail "installer fixture failed for $INSTALL_CASE: $INSTALL_OUT"
        continue
    fi
    OUT="$(cd "$REPO8" && bash "$SCRIPT" 2>&1)"
    RC=$?
    if [[ "$INSTALL_CASE" == custom-fix ]]; then
        if [[ $RC -eq 1 ]] && [[ "$(cat "$REPO8/.loom/hooks/guard.sh")" == $'OLD\nINSTALLER-CONTENT' ]]; then
            pass "custom installer fix message remains protected"
        else
            fail "custom installer fix was not protected: $OUT"
        fi
    elif [[ $RC -eq 0 ]] && [[ "$(cat "$REPO8/.loom/hooks/guard.sh")" == A ]]; then
        pass "real $INSTALL_CASE install permits routine upstream resync"
    else
        fail "real $INSTALL_CASE install falsely blocks routine resync: $OUT"
    fi
done

# GitHub squash merges append a PR number to the routine commit subject.
echo "Test group 9: squash suffix preserves routine provenance"
for SUBJECT in \
    'chore: resync installed Loom surfaces (#123)' \
    'chore(loom): Install Loom 0.19.60 orchestration framework (#123)' \
    '[skip ci] chore(loom): Install Loom 0.19.60 orchestration framework (#123)' \
    'chore: resync installed Loom surfaces with a local fix (#123)' \
    'chore: install Loom v0.19.60 (#123)' \
    'chore: install Loom v1 and also revert the guard fix'; do
    REPO9="$(make_fixture)"
    git -C "$REPO9" commit --amend -qm "$SUBJECT"
    OUT="$(cd "$REPO9" && bash "$SCRIPT" 2>&1)"
    RC=$?
    if [[ "$SUBJECT" == *'with a local fix'* || "$SUBJECT" == *'and also revert'* ]]; then
        if [[ $RC -eq 1 ]] && [[ "$(cat "$REPO9/.loom/hooks/guard.sh")" == OLD ]]; then
            pass "non-routine suffixed subject retains its local content: $SUBJECT"
        else
            fail "non-routine suffixed subject lost protection: $SUBJECT: $OUT"
        fi
    elif [[ $RC -eq 0 ]] && [[ "$(cat "$REPO9/.loom/hooks/guard.sh")" == A ]]; then
        pass "routine squash subject permits update: $SUBJECT"
    else
        fail "routine squash subject falsely blocks update: $SUBJECT: $OUT"
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
