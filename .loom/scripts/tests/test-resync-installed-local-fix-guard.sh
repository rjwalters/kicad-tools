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
    'chore: install Loom v1 and also revert the guard fix' \
    'chore: install Loom v1 plus my hand fix'; do
    REPO9="$(make_fixture)"
    git -C "$REPO9" commit --amend -qm "$SUBJECT"
    OUT="$(cd "$REPO9" && bash "$SCRIPT" 2>&1)"
    RC=$?
    if [[ "$SUBJECT" == *'with a local fix'* || "$SUBJECT" == *'and also revert'* \
        || "$SUBJECT" == *'plus my hand fix'* ]]; then
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

# --- the removed-line count must not depend on the ambient locale (#8165) ----
#
# removed_line_count() sorts both of comm's inputs with LC_ALL=C, but used to
# run `comm` itself in the ambient locale. GNU comm validates input order
# against the CURRENT collation, so under a locale that collates differently
# from C (en_US.UTF-8 folds case and ignores punctuation; C is byte order) it
# rejected the C-sorted streams as "not in sorted order" and emitted a garbage
# line set -- over-counting (a pure-addition update spuriously BLOCKED, only
# recoverable with --force) or under-counting (a silent fail-open back to the
# pre-#7864 revert-the-local-fix behaviour). The verdict must be identical in
# every locale.
echo "Test group 10: the local-divergence verdict is locale-invariant (#8165)"

# Deterministic half: assert the script hands `comm` LC_ALL=C no matter what
# the ambient LC_ALL says. This runs everywhere -- it does not need any
# particular locale to be installed, only the env var to be set, since the
# shim reads LC_ALL rather than collating with it.
export LOOM_TEST_REAL_COMM
LOOM_TEST_REAL_COMM="$(command -v comm)"
mkdir -p "$WORKDIR/comm-bin"
cat > "$WORKDIR/comm-bin/comm" <<'SH'
#!/usr/bin/env bash
if [[ "${LC_ALL:-unset}" != "C" ]]; then
    echo "TEST_COMM_BAD_LOCALE=${LC_ALL:-unset}" >&2
    exit 90
fi
exec "$LOOM_TEST_REAL_COMM" "$@"
SH
chmod +x "$WORKDIR/comm-bin/comm"
REPO10="$(make_fixture)"
printf 'a\nB\nLOCAL-FIX\n' > "$REPO10/.loom/hooks/guard.sh"
git -C "$REPO10" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO10" commit -qm "fix(guard): a direct local fix" >/dev/null 2>&1
printf 'a\nB\n' > "$REPO10/defaults/hooks/guard.sh"
OUT="$(cd "$REPO10" && PATH="$WORKDIR/comm-bin:$PATH" LC_ALL=en_US.UTF-8 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 1 ]] && grep -q "BLOCKED" <<<"$OUT" && ! grep -q "TEST_COMM_BAD_LOCALE" <<<"$OUT"; then
    pass "(#8165) comm runs under LC_ALL=C even when the caller's LC_ALL is not C"
else
    fail "(#8165) comm did not run under LC_ALL=C with a non-C ambient locale (rc=$RC); out=$OUT"
fi

# Real-locale half: run the guard end-to-end under an installed locale whose
# collation actually differs from C, and require the same verdicts as under C.
# Skipped (not failed) on a host with no such locale installed -- the shim
# half above still covers the mechanism there.
NONC_LOCALE=""
while read -r CAND; do
    [[ -n "$CAND" ]] || continue
    PROBE="$(printf 'B\na\n' | LC_ALL="$CAND" sort 2>/dev/null)"
    if [[ "${PROBE%%$'\n'*}" == "a" ]]; then
        NONC_LOCALE="$CAND"
        break
    fi
done < <(locale -a 2>/dev/null)

if [[ -z "$NONC_LOCALE" ]]; then
    echo "  SKIP: no installed locale collates differently from C on this host"
else
    echo "  (non-C collating locale: $NONC_LOCALE)"
    for LOC in C "$NONC_LOCALE"; do
        # (a) pure addition on a diverged file: removed=0 -> must apply. This
        #     is the shape the unpinned comm mis-scored as removed=1.
        REPO10A="$(make_fixture)"
        printf 'a\nB\n' > "$REPO10A/.loom/hooks/guard.sh"
        git -C "$REPO10A" add .loom/hooks/guard.sh >/dev/null 2>&1
        git -C "$REPO10A" commit -qm "fix(guard): a direct local fix" >/dev/null 2>&1
        printf 'a\nB\nNEW\n' > "$REPO10A/defaults/hooks/guard.sh"
        OUT="$(cd "$REPO10A" && LC_ALL="$LOC" bash "$SCRIPT" 2>&1)"
        RC=$?
        if [[ $RC -eq 0 ]] && ! grep -q "BLOCKED" <<<"$OUT" \
            && [[ "$(cat "$REPO10A/.loom/hooks/guard.sh")" == $'a\nB\nNEW' ]]; then
            pass "(#8165) LC_ALL=$LOC: a pure-addition update still applies (no phantom removal)"
        else
            fail "(#8165) LC_ALL=$LOC: a pure-addition update was spuriously blocked (rc=$RC); out=$OUT"
        fi

        # (b) a real removal on the same content: removed>0 -> must block.
        REPO10B="$(make_fixture)"
        printf 'a\nB\nLOCAL-FIX\n' > "$REPO10B/.loom/hooks/guard.sh"
        git -C "$REPO10B" add .loom/hooks/guard.sh >/dev/null 2>&1
        git -C "$REPO10B" commit -qm "fix(guard): a direct local fix" >/dev/null 2>&1
        printf 'a\nB\n' > "$REPO10B/defaults/hooks/guard.sh"
        OUT="$(cd "$REPO10B" && LC_ALL="$LOC" bash "$SCRIPT" 2>&1)"
        RC=$?
        if [[ $RC -eq 1 ]] && grep -q "BLOCKED" <<<"$OUT" \
            && [[ "$(cat "$REPO10B/.loom/hooks/guard.sh")" == $'a\nB\nLOCAL-FIX' ]]; then
            pass "(#8165) LC_ALL=$LOC: a real removal is still blocked"
        else
            fail "(#8165) LC_ALL=$LOC: a real removal was not blocked (rc=$RC); out=$OUT"
        fi
    done
fi

# --- content-based lineage: pure upstream content is never a "local fix" -----
#
# #8676: the guard used to decide lineage purely from the installed file's last
# commit SUBJECT. A file never touched since a repo's ORIGINAL install carries
# that install's subject -- whatever the installing human typed -- and real
# fleet examples match none of RESYNC_COMMIT_SUBJECT_RE's alternatives:
#
#     tooling: install Repo Skills and Loom into the map repo
#     Upgrade Loom to 0.18.0 (resync installed surfaces + role prompts)
#     Install Loom orchestration (quick install from rjwalters/loom@cd4ab46e)
#
# So the moment upstream EDITED such a file, the old upstream text being
# replaced was read as "lines unique to the installed copy" and the update was
# blocked as a phantom local fix -- the incident behind #8676 blocked a
# one-line jq-1.6 compatibility fix to scripts/archive-transcripts.sh across
# the fleet, each repo needing a hand-run --force.
#
# The fix decides by CONTENT first: byte-identical to defaults/<rel> as it
# stood at the installed version (install-metadata.json's loom_commit, else a
# v<loom_version> tag, resolved in SOURCE_ROOT) => pure upstream lineage, no
# local fix to protect, whatever the subject says.

# make_lineage_fixture <dirname> <install-subject> [metadata-mode]
#   A repo "installed at commit A" with an ARBITRARY install commit subject.
#   .loom/hooks/guard.sh is byte-identical to defaults/hooks/guard.sh at that
#   commit -- the "never individually patched since install" shape.
#
#   metadata-mode (default "commit") selects how the installed version is
#   recorded, so both resolution rungs and the unresolvable fallback are
#   exercised:
#     commit      -- loom_commit = commit A's short sha
#     tag         -- loom_commit unresolvable, but tag v0.0.0 points at A
#     unresolvable-- neither resolves (models a tarball/vendored source tree)
#
#   The metadata is written in a SECOND commit that does not touch
#   .loom/hooks/guard.sh, so that file's last-commit subject stays the
#   arbitrary install subject under test.
make_lineage_fixture() {
    local repo="$WORKDIR/$1" subject="$2" mode="${3:-commit}"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts" \
             "$repo/.loom/hooks" "$repo/.loom/scripts"
    git -C "$repo" init -q

    printf 'line-one\nline-two\nline-three\n' > "$repo/defaults/hooks/guard.sh"
    printf 'line-one\nline-two\nline-three\n' > "$repo/.loom/hooks/guard.sh"
    chmod +x "$repo/defaults/hooks/guard.sh"
    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "unknown",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "$subject" >/dev/null 2>&1

    local recorded="unknown"
    case "$mode" in
        commit) recorded="$(git -C "$repo" rev-parse --short HEAD)" ;;
        tag)    git -C "$repo" tag v0.0.0 >/dev/null 2>&1 ;;
    esac
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "%s",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$recorded" "$repo" > "$repo/.loom/install-metadata.json"
    git -C "$repo" add .loom/install-metadata.json >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: record install metadata" >/dev/null 2>&1

    echo "$repo"
}

# bump_upstream <repo>
#   "Tag B": upstream drops a line from defaults/hooks/guard.sh. Against the
#   untouched installed copy this is a removal (removed_line_count > 0), so it
#   reaches the lineage gate -- exactly the jq-1.6-rename shape.
bump_upstream() {
    local repo="$1"
    printf 'line-one\nline-three\n' > "$repo/defaults/hooks/guard.sh"
    git -C "$repo" add defaults/hooks/guard.sh >/dev/null 2>&1
    git -C "$repo" commit -qm "fix(guard): drop line-two for jq 1.6 compatibility" >/dev/null 2>&1
}

echo "Test group 11: a file identical to upstream-at-installed-version is never a local fix (#8676)"
LINEAGE_N=0
for INSTALL_SUBJECT in \
    'tooling: install Repo Skills and Loom into the map repo' \
    'Upgrade Loom to 0.18.0 (resync installed surfaces + role prompts)' \
    'Install Loom orchestration (quick install from rjwalters/loom@cd4ab46e)'; do
    LINEAGE_N=$((LINEAGE_N + 1))
    REPO11="$(make_lineage_fixture "repo-lineage-$LINEAGE_N" "$INSTALL_SUBJECT")"
    bump_upstream "$REPO11"

    # Precondition: the subject heuristic alone would call this diverged.
    if [[ "$(git -C "$REPO11" log -1 --format='%s' -- .loom/hooks/guard.sh)" == "$INSTALL_SUBJECT" ]]; then
        pass "(#8676) fixture precondition: guard.sh's last subject is the arbitrary install subject"
    else
        fail "(#8676) fixture precondition unmet for: $INSTALL_SUBJECT"
    fi

    # The per-file verdict line, not the tail summary -- the summary always
    # carries the word "blocked" ("... 0 blocked (needs --force ...)").
    OUT="$(cd "$REPO11" && bash "$SCRIPT" --dry-run 2>&1)"
    RC=$?
    if [[ $RC -eq 2 ]] && grep -q "would update hooks/guard.sh" <<<"$OUT" \
        && ! grep -qE 'blocked[[:space:]]+hooks/guard\.sh' <<<"$OUT"; then
        pass "(#8676) --dry-run previews 'would update', not 'blocked': $INSTALL_SUBJECT"
    else
        fail "(#8676) --dry-run reported a block for pure upstream content (rc=$RC): $INSTALL_SUBJECT; out=$OUT"
    fi

    OUT="$(cd "$REPO11" && bash "$SCRIPT" 2>&1)"
    RC=$?
    if [[ $RC -eq 0 ]] && ! grep -q "BLOCKED" <<<"$OUT" \
        && [[ "$(cat "$REPO11/.loom/hooks/guard.sh")" == $'line-one\nline-three' ]]; then
        pass "(#8676) the legitimate upstream removal applies without --force: $INSTALL_SUBJECT"
    else
        fail "(#8676) a legitimate upstream removal was blocked (rc=$RC): $INSTALL_SUBJECT; out=$OUT"
    fi
done

# The v<loom_version> tag is the second resolution rung: an install whose
# recorded loom_commit has since been GC'd or was never written still proves
# pure lineage when a matching tag exists in the source checkout.
echo "Test group 11b: the v<loom_version> tag is an equivalent lineage anchor (#8676)"
REPO11T="$(make_lineage_fixture repo-lineage-tag 'Upgrade Loom to 0.18.0 (resync installed surfaces + role prompts)' tag)"
bump_upstream "$REPO11T"
OUT="$(cd "$REPO11T" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && ! grep -q "BLOCKED" <<<"$OUT" \
    && [[ "$(cat "$REPO11T/.loom/hooks/guard.sh")" == $'line-one\nline-three' ]]; then
    pass "(#8676) a v<loom_version> tag resolves the installed version when loom_commit does not"
else
    fail "(#8676) the tag rung failed to resolve the installed version (rc=$RC); out=$OUT"
fi

# The narrowing is strictly content-proved: a genuinely hand-edited installed
# file differs from upstream-at-installed-version, so the content rung cannot
# fire and the subject heuristic still protects it exactly as before.
echo "Test group 11c: a genuinely hand-edited file is still blocked (#8676)"
REPO11H="$(make_lineage_fixture repo-lineage-handedit 'tooling: install Repo Skills and Loom into the map repo')"
printf 'line-one\nline-two\nline-three\nLOCAL-HOTFIX\n' > "$REPO11H/.loom/hooks/guard.sh"
git -C "$REPO11H" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO11H" commit -qm "fix(guard): hand-applied hotfix that upstream has not taken yet" >/dev/null 2>&1
bump_upstream "$REPO11H"
OUT="$(cd "$REPO11H" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 1 ]] && grep -q "BLOCKED" <<<"$OUT" \
    && [[ "$(cat "$REPO11H/.loom/hooks/guard.sh")" == $'line-one\nline-two\nline-three\nLOCAL-HOTFIX' ]]; then
    pass "(#8676) a hand-edited installed file is still protected"
else
    fail "(#8676) a hand-edited installed file lost its protection (rc=$RC); out=$OUT"
fi

# Same hand edit, but committed with an install-shaped subject at install time:
# content still differs from upstream-at-installed-version, so the content rung
# must NOT rescue it -- it can only ever prove lineage, never assume it.
echo "Test group 11d: the content rung never fires on content that differs (#8676)"
REPO11D="$(make_lineage_fixture repo-lineage-differs 'tooling: install Repo Skills and Loom into the map repo')"
printf 'line-one\nline-two\nline-three\nDIVERGED\n' > "$REPO11D/.loom/hooks/guard.sh"
git -C "$REPO11D" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO11D" commit -qm "chore: tweak the installed guard" >/dev/null 2>&1
bump_upstream "$REPO11D"
OUT="$(cd "$REPO11D" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]] && grep -qE 'blocked[[:space:]]+hooks/guard\.sh' <<<"$OUT"; then
    pass "(#8676) --dry-run still reports a block when the installed content differs"
else
    fail "(#8676) --dry-run failed to report a block for diverged content (rc=$RC); out=$OUT"
fi

# When the installed version cannot be resolved at all (no usable loom_commit,
# no matching tag -- e.g. a tarball/vendored source tree or a GC'd commit), the
# subject heuristic remains the fallback and behaviour is exactly as before.
echo "Test group 11e: an unresolvable installed version falls back to the subject heuristic (#8676)"
REPO11F="$(make_lineage_fixture repo-lineage-unresolvable 'tooling: install Repo Skills and Loom into the map repo' unresolvable)"
bump_upstream "$REPO11F"
OUT="$(cd "$REPO11F" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 1 ]] && grep -q "BLOCKED" <<<"$OUT" \
    && [[ "$(cat "$REPO11F/.loom/hooks/guard.sh")" == $'line-one\nline-two\nline-three' ]]; then
    pass "(#8676) an unresolvable installed version keeps the pre-#8676 (conservative) verdict"
else
    fail "(#8676) the unresolvable-version fallback did not behave conservatively (rc=$RC); out=$OUT"
fi

# The recorded version DRIFTS AHEAD of the installed bytes: restamp_metadata()
# rewrites loom_commit to the source HEAD on every non-dry run, and it runs
# BEFORE the blocked-file exit -- so the run that blocks a file also records a
# version whose content that file no longer matches. One blocked run is enough
# to put a repo here permanently, and it is where #8676's 23 fleet repos
# already are: they saw the block, and any `loom update` since stamped their
# metadata past the upstream commit that caused it. An exact-version-only
# content check would leave every one of them blocked forever.
echo "Test group 11f: a recorded version that drifted PAST the installed bytes still resolves (#8676)"
REPO11G="$(make_lineage_fixture repo-lineage-drift 'Install Loom orchestration (quick install from rjwalters/loom@cd4ab46e)')"
bump_upstream "$REPO11G"
# Re-stamp loom_commit to the bumped HEAD, exactly as a blocked non-dry run
# would have. The stamp lands in its own commit so .loom/hooks/guard.sh keeps
# the arbitrary install subject as its last change.
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "%s",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$(git -C "$REPO11G" rev-parse --short HEAD)" "$REPO11G" > "$REPO11G/.loom/install-metadata.json"
git -C "$REPO11G" add .loom/install-metadata.json >/dev/null 2>&1
git -C "$REPO11G" commit -qm "chore: re-stamp install metadata" >/dev/null 2>&1

# Precondition: the installed bytes no longer match the RECORDED version, so
# only the reachable-revision rung can prove lineage here.
if ! git -C "$REPO11G" show "$(git -C "$REPO11G" rev-parse --short HEAD~1):./defaults/hooks/guard.sh" 2>/dev/null \
    | cmp -s - "$REPO11G/.loom/hooks/guard.sh"; then
    pass "(#8676) drift fixture precondition: installed bytes differ from the recorded version"
else
    fail "(#8676) drift fixture precondition unmet: recorded version still matches the installed bytes"
fi

OUT="$(cd "$REPO11G" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && ! grep -q "BLOCKED" <<<"$OUT" \
    && [[ "$(cat "$REPO11G/.loom/hooks/guard.sh")" == $'line-one\nline-three' ]]; then
    pass "(#8676) a drifted loom_commit still proves lineage from a reachable revision"
else
    fail "(#8676) a drifted loom_commit left pure upstream content blocked (rc=$RC); out=$OUT"
fi

# The reachable-revision rung must not become a blanket amnesty: content that
# was never any revision of this path stays protected even though the recorded
# version has drifted.
echo "Test group 11g: drift does not excuse content upstream never had (#8676)"
REPO11H2="$(make_lineage_fixture repo-lineage-drift-handedit 'Install Loom orchestration (quick install from rjwalters/loom@cd4ab46e)')"
printf 'line-one\nline-two\nline-three\nLOCAL-HOTFIX\n' > "$REPO11H2/.loom/hooks/guard.sh"
git -C "$REPO11H2" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO11H2" commit -qm "fix(guard): hand-applied hotfix upstream never took" >/dev/null 2>&1
bump_upstream "$REPO11H2"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "%s",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$(git -C "$REPO11H2" rev-parse --short HEAD)" "$REPO11H2" > "$REPO11H2/.loom/install-metadata.json"
git -C "$REPO11H2" add .loom/install-metadata.json >/dev/null 2>&1
git -C "$REPO11H2" commit -qm "chore: re-stamp install metadata" >/dev/null 2>&1
OUT="$(cd "$REPO11H2" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 1 ]] && grep -q "BLOCKED" <<<"$OUT" \
    && [[ "$(cat "$REPO11H2/.loom/hooks/guard.sh")" == $'line-one\nline-two\nline-three\nLOCAL-HOTFIX' ]]; then
    pass "(#8676) a hand edit stays protected even when the recorded version drifted"
else
    fail "(#8676) drift handling weakened protection for a real hand edit (rc=$RC); out=$OUT"
fi

# The reachable-revision lookup pipes `git cat-file --batch-check` into grep,
# under this script's `set -o pipefail`. With `grep -q` that is a latent phantom
# block: grep exits the instant it matches, SIGPIPEs cat-file into status 141,
# and pipefail reports the whole pipeline as failed -- so a SUCCESSFUL lineage
# proof reads as "no proof" and the file is blocked anyway. It only bites once
# the object-id list outgrows the 64KiB pipe buffer (~1600 revisions of one
# path), which is why every small fixture above passes either way. This fixture
# is deliberately large enough to cross that line, and puts the matching
# revision NEXT TO HEAD so grep matches on its second line of input -- the
# earliest possible exit, i.e. the worst case for SIGPIPE.
echo "Test group 11h: a long-lived path does not phantom-block via pipefail+SIGPIPE (#8676)"
REPO11P="$WORKDIR/repo-lineage-longhistory"
rm -rf "$REPO11P"
mkdir -p "$REPO11P/defaults/hooks" "$REPO11P/defaults/scripts" \
         "$REPO11P/.loom/hooks" "$REPO11P/.loom/scripts"
git -C "$REPO11P" init -q
LONG_SUBJECT='Install Loom orchestration (quick install from rjwalters/loom@cd4ab46e)'
printf 'line-one\nline-two\nline-three\nrev-0\n' > "$REPO11P/defaults/hooks/guard.sh"
printf 'line-one\nline-two\nline-three\nrev-0\n' > "$REPO11P/.loom/hooks/guard.sh"
chmod +x "$REPO11P/defaults/hooks/guard.sh"
printf '{\n  "version": "9.9.9"\n}\n' > "$REPO11P/package.json"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "unknown",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$REPO11P" > "$REPO11P/.loom/install-metadata.json"
git -C "$REPO11P" add -A >/dev/null 2>&1
git -C "$REPO11P" commit -qm "$LONG_SUBJECT" >/dev/null 2>&1

# ~1700 upstream revisions of the SOURCE file (~7s). --no-verify keeps any
# ambient hook out of the loop.
LONG_REVS=1699
for ((i = 1; i <= LONG_REVS; i++)); do
    printf 'line-one\nline-two\nline-three\nrev-%s\n' "$i" > "$REPO11P/defaults/hooks/guard.sh"
    git -C "$REPO11P" add defaults/hooks/guard.sh
    git -C "$REPO11P" commit -qm "upstream rev $i" --no-verify
done >/dev/null 2>&1

# The installed copy is the LAST of those revisions, committed under the
# arbitrary install subject, so only the content rung can clear it.
printf 'line-one\nline-two\nline-three\nrev-%s\n' "$LONG_REVS" > "$REPO11P/.loom/hooks/guard.sh"
git -C "$REPO11P" add .loom/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO11P" commit -qm "$LONG_SUBJECT" --no-verify >/dev/null 2>&1
# Upstream then drops a line, and the metadata drifts to that new HEAD.
printf 'line-one\nline-three\nrev-%s\n' "$((LONG_REVS + 1))" > "$REPO11P/defaults/hooks/guard.sh"
git -C "$REPO11P" add defaults/hooks/guard.sh >/dev/null 2>&1
git -C "$REPO11P" commit -qm "fix(guard): drop line-two for jq 1.6 compatibility" --no-verify >/dev/null 2>&1
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "%s",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$(git -C "$REPO11P" rev-parse --short HEAD)" "$REPO11P" > "$REPO11P/.loom/install-metadata.json"
git -C "$REPO11P" add .loom/install-metadata.json >/dev/null 2>&1
git -C "$REPO11P" commit -qm "chore: re-stamp install metadata" --no-verify >/dev/null 2>&1

# Precondition: the id list really does outgrow a 64KiB pipe buffer, or this
# fixture cannot exercise the hazard it exists for.
LONG_BYTES="$(git -C "$REPO11P" rev-list HEAD -- defaults/hooks/guard.sh \
    | sed 's|$|:./defaults/hooks/guard.sh|' \
    | git -C "$REPO11P" cat-file --batch-check='%(objectname)' | wc -c | tr -d '[:space:]')"
if [[ "${LONG_BYTES:-0}" -gt 65536 ]]; then
    pass "(#8676) long-history precondition: object-id list is ${LONG_BYTES}B, past the 64KiB pipe buffer"
else
    fail "(#8676) long-history fixture too small to exercise the SIGPIPE hazard (${LONG_BYTES}B)"
fi

OUT="$(cd "$REPO11P" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]] && grep -q "would update hooks/guard.sh" <<<"$OUT" \
    && ! grep -qE 'blocked[[:space:]]+hooks/guard\.sh' <<<"$OUT"; then
    pass "(#8676) a match late in a long id list is not lost to pipefail+SIGPIPE"
else
    fail "(#8676) long-history lineage proof was lost -- phantom block (rc=$RC); out=$OUT"
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
