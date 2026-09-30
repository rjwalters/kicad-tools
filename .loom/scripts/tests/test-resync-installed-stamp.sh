#!/usr/bin/env bash
# test-resync-installed-stamp.sh - install-metadata.json re-stamping in
# resync-installed.sh's restamp_metadata() (#9174, #9613)
#
# Split out of test-resync-installed.sh (frozen by the file-size ratchet,
# .loom/docs/file-size-policy.md) rather than grown in place.
#
# Covers the two ways the stamp was wrong:
#   (1) #9174 -- loom_commit was written with `git rev-parse --short HEAD`.
#       Git auto-sizes that abbreviation per repository and per git version
#       when core.abbrev is unset, so two hosts resyncing from the SAME source
#       commit wrote different strings into a TRACKED file (measured:
#       `64a325804` on git 2.43 vs `64a32580` on git 2.54). Every scheduled
#       pass on one host then saw a dirty tree, committed it, and the other
#       host reversed it -- indefinite no-op churn on every fleet repo, and
#       "is this host's surface current?" un-answerable from the file the
#       field exists to answer it with.
#   (2) #9613 -- a source resolved from .loom/loom-source-path that holds a
#       copied defaults/ tree but no package.json and no .git stamped
#       loom_version / loom_commit = the literal string "unknown" into that
#       same tracked file, and the unparseable stamp was committed to a
#       consumer's main. Downstream version checks then could not compare
#       anything, and the local-fix guard's content-lineage rung had no commit
#       to resolve against.
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-stamp.sh

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

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-stamp.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# read_field <metadata-file> <field>
read_field() {
    sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$1" 2>/dev/null | head -1
}

# --- dogfood fixture: the repo owns its own defaults/ (source == destination)
make_dogfood_fixture() {
    local repo="$WORKDIR/$1"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts" \
             "$repo/.loom/hooks" "$repo/.loom/scripts"
    git -C "$repo" init -q
    printf 'A\n' > "$repo/defaults/hooks/guard.sh"
    chmod +x "$repo/defaults/hooks/guard.sh"
    printf 'OLD\n' > "$repo/.loom/hooks/guard.sh"
    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
        > "$repo/.loom/install-metadata.json"
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
    echo "$repo"
}

echo ""
echo "Test group 1: loom_commit is stamped as a full 40-hex SHA (#9174)"
REPO1="$(make_dogfood_fixture repo-full-sha)"
(cd "$REPO1" && bash "$SCRIPT" >/dev/null 2>&1)
STAMPED="$(read_field "$REPO1/.loom/install-metadata.json" loom_commit)"
EXPECTED="$(git -C "$REPO1" rev-parse HEAD)"
if [[ "$STAMPED" =~ ^[0-9a-f]{40}$ ]]; then
    pass "(#9174) loom_commit is 40 hex characters (got ${#STAMPED})"
else
    fail "(#9174) loom_commit is not a full 40-hex SHA (got '$STAMPED')"
fi
if [[ "$STAMPED" == "$EXPECTED" ]]; then
    pass "(#9174) loom_commit equals the source checkout's HEAD"
else
    fail "(#9174) loom_commit '$STAMPED' != source HEAD '$EXPECTED'"
fi

# The convergence property the field exists for: the stamp must not depend on
# how this host's git chooses to abbreviate. core.abbrev is the knob that made
# two hosts disagree, so setting it to an extreme value and resyncing again
# must leave the file byte-identical.
echo ""
echo "Test group 2: the stamp is independent of core.abbrev, so hosts converge (#9174)"
BEFORE_MD5="$(cksum < "$REPO1/.loom/install-metadata.json")"
git -C "$REPO1" config core.abbrev 4
(cd "$REPO1" && bash "$SCRIPT" >/dev/null 2>&1)
AFTER_MD5="$(cksum < "$REPO1/.loom/install-metadata.json")"
if [[ "$BEFORE_MD5" == "$AFTER_MD5" ]]; then
    pass "(#9174) a re-resync under core.abbrev=4 leaves install-metadata.json byte-identical"
else
    fail "(#9174) core.abbrev changed the stamp — hosts would keep reversing each other's commit"
fi
if [[ "$(read_field "$REPO1/.loom/install-metadata.json" loom_commit)" =~ ^[0-9a-f]{40}$ ]]; then
    pass "(#9174) loom_commit is still a full SHA under core.abbrev=4"
else
    fail "(#9174) core.abbrev=4 shortened loom_commit"
fi

# --- consumer fixture: defaults/ comes from .loom/loom-source-path ------------
#
# make_consumer_fixture <name> <source-has-package-json> <source-is-git-repo>
#   Returns the consumer repo path; the source tree lives beside it.
make_consumer_fixture() {
    local name="$1" with_pkg="$2" with_git="$3"
    local consumer="$WORKDIR/$name" src="$WORKDIR/$name-src"
    rm -rf "$consumer" "$src"

    mkdir -p "$src/defaults/hooks" "$src/defaults/scripts"
    printf 'A\n' > "$src/defaults/hooks/guard.sh"
    chmod +x "$src/defaults/hooks/guard.sh"
    [[ "$with_pkg" == yes ]] && printf '{\n  "version": "1.2.3"\n}\n' > "$src/package.json"
    if [[ "$with_git" == yes ]]; then
        git -C "$src" init -q
        git -C "$src" add -A >/dev/null 2>&1
        git -C "$src" commit -qm "source" >/dev/null 2>&1
    fi

    mkdir -p "$consumer/.loom/hooks" "$consumer/.loom/scripts"
    git -C "$consumer" init -q
    printf 'OLD\n' > "$consumer/.loom/hooks/guard.sh"
    printf '{\n  "loom_version": "0.19.100",\n  "loom_commit": "aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
        > "$consumer/.loom/install-metadata.json"
    printf '%s\n' "$src" > "$consumer/.loom/loom-source-path"
    git -C "$consumer" add -A >/dev/null 2>&1
    git -C "$consumer" commit -qm "chore: install Loom v0.19.100" >/dev/null 2>&1
    echo "$consumer"
}

echo ""
echo "Test group 3: a source with only defaults/ (no package.json, no git) never stamps \"unknown\" (#9613)"
C3="$(make_consumer_fixture consumer-no-meta no no)"
BEFORE_C3="$(cat "$C3/.loom/install-metadata.json")"
OUT="$(cd "$C3" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ "$(cat "$C3/.loom/install-metadata.json")" == "$BEFORE_C3" ]]; then
    pass "(#9613) install-metadata.json is left byte-unchanged"
else
    fail "(#9613) install-metadata.json was rewritten: $(cat "$C3/.loom/install-metadata.json")"
fi
if ! grep -q '"unknown"' "$C3/.loom/install-metadata.json"; then
    pass "(#9613) the literal string \"unknown\" never reaches the tracked file"
else
    fail "(#9613) an \"unknown\" stamp reached the tracked file"
fi
if grep -q "Skipped the install-metadata.json re-stamp" <<<"$OUT" && grep -q "9613" <<<"$OUT"; then
    pass "(#9613) the skip is reported loudly, naming the issue"
else
    fail "(#9613) the skip was silent; out=$OUT"
fi
if grep -q "package.json" <<<"$OUT" && grep -q "git metadata" <<<"$OUT" && grep -qF "$WORKDIR/consumer-no-meta-src" <<<"$OUT"; then
    pass "(#9613) the warning names the source and which piece of metadata is missing"
else
    fail "(#9613) the warning did not diagnose the source; out=$OUT"
fi
# The surface sync itself is still valid -- defaults/ was there and was copied.
# Refusing the whole run would be a bigger outage than the bug.
if [[ $RC -eq 0 ]] && [[ "$(cat "$C3/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(#9613) the surface sync still applied and the run exits 0"
else
    fail "(#9613) the surface sync did not apply (rc=$RC)"
fi

echo ""
echo "Test group 4: a source with package.json but no git metadata also skips the stamp (#9613)"
C4="$(make_consumer_fixture consumer-no-git yes no)"
BEFORE_C4="$(cat "$C4/.loom/install-metadata.json")"
OUT="$(cd "$C4" && bash "$SCRIPT" 2>&1)"
if [[ "$(cat "$C4/.loom/install-metadata.json")" == "$BEFORE_C4" ]]; then
    pass "(#9613) a half-resolvable source still leaves the stamp untouched"
else
    fail "(#9613) a half-resolvable source wrote a partial stamp: $(cat "$C4/.loom/install-metadata.json")"
fi
if grep -q "Skipped the install-metadata.json re-stamp" <<<"$OUT"; then
    pass "(#9613) the half-resolvable case warns too"
else
    fail "(#9613) the half-resolvable case was silent; out=$OUT"
fi

echo ""
echo "Test group 5: a real source checkout still re-stamps normally (#9613 regression control)"
C5="$(make_consumer_fixture consumer-good yes yes)"
(cd "$C5" && bash "$SCRIPT" >/dev/null 2>&1)
GOOD_VERSION="$(read_field "$C5/.loom/install-metadata.json" loom_version)"
GOOD_COMMIT="$(read_field "$C5/.loom/install-metadata.json" loom_commit)"
if [[ "$GOOD_VERSION" == "1.2.3" ]]; then
    pass "(#9613) loom_version is re-stamped from the source package.json"
else
    fail "(#9613) loom_version was not re-stamped (got '$GOOD_VERSION')"
fi
if [[ "$GOOD_COMMIT" == "$(git -C "$WORKDIR/consumer-good-src" rev-parse HEAD)" ]]; then
    pass "(#9174/#9613) loom_commit is the source checkout's full HEAD SHA"
else
    fail "(#9174/#9613) loom_commit is wrong (got '$GOOD_COMMIT')"
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
