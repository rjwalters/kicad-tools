#!/usr/bin/env bash
# test-resync-installed-stamp.sh - install-metadata.json re-stamping in
# resync-installed.sh's restamp_metadata() (#9174, #9613, #10717)
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

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

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

echo ""
echo "Test group 6: requires_daemon is re-stamped from the source tree (#10717)"
C6="$(make_consumer_fixture consumer-contract yes yes)"
mkdir -p "$WORKDIR/consumer-contract-src/loom-daemon/src"
printf 'pub const REQUIRES_DAEMON: &str = "0.19.772";\n' \
    > "$WORKDIR/consumer-contract-src/loom-daemon/src/install_compat.rs"
git -C "$WORKDIR/consumer-contract-src" add -A >/dev/null 2>&1
git -C "$WORKDIR/consumer-contract-src" commit -qm "contract" >/dev/null 2>&1
(cd "$C6" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ "$(read_field "$C6/.loom/install-metadata.json" requires_daemon)" == "0.19.772" ]]; then
    pass "(#10717) requires_daemon is the source tree's REQUIRES_DAEMON"
else
    fail "(#10717) requires_daemon not stamped: $(cat "$C6/.loom/install-metadata.json")"
fi
# A source that predates the contract makes no claim about the files it just
# installed, so a recorded value is dropped rather than left stale.
rm "$WORKDIR/consumer-contract-src/loom-daemon/src/install_compat.rs"
printf 'B\n' > "$WORKDIR/consumer-contract-src/defaults/hooks/guard.sh"
git -C "$WORKDIR/consumer-contract-src" add -A >/dev/null 2>&1
git -C "$WORKDIR/consumer-contract-src" commit -qm "pre-contract" >/dev/null 2>&1
(cd "$C6" && bash "$SCRIPT" >/dev/null 2>&1)
if ! grep -q requires_daemon "$C6/.loom/install-metadata.json" \
    && [[ "$(read_field "$C6/.loom/install-metadata.json" loom_version)" == "1.2.3" ]]; then
    pass "(#10717) a source without the contract drops requires_daemon"
else
    fail "(#10717) stale requires_daemon survived: $(cat "$C6/.loom/install-metadata.json")"
fi

# --- #11680: a run that changed nothing never touches the stamp ---------------
#
# loom_commit is the source HEAD. On the dogfood repo, committing the restamp
# moved HEAD one past the value just stamped, so every later run on every host
# restamped and committed again, and every consumer inherited a metadata-only
# commit per host per tick. Separately, loom_source_remote flipped between the
# ssh and https spelling depending on which host ran last.

TODAY="$(date -u +%Y-%m-%d)"

# meta_status <repo>: porcelain status of the tracked metadata file ("" = clean)
meta_status() {
    git -C "$1" status --porcelain -- .loom/install-metadata.json 2>/dev/null
}

commit_all() {
    git -C "$1" add -A >/dev/null 2>&1
    git -C "$1" commit -qm "$2" >/dev/null 2>&1
}

echo ""
echo "Test group 7: dogfood -- committing the restamp does not make the next run restamp (#11680)"
REPO7="$(make_dogfood_fixture repo-self-resync)"
(cd "$REPO7" && bash "$SCRIPT" >/dev/null 2>&1)
FIRST7="$(read_field "$REPO7/.loom/install-metadata.json" loom_commit)"
commit_all "$REPO7" "chore: resync installed Loom surfaces"
SUM7="$(cksum < "$REPO7/.loom/install-metadata.json")"
OUT7="$(cd "$REPO7" && bash "$SCRIPT" 2>&1)"
if [[ "$(cksum < "$REPO7/.loom/install-metadata.json")" == "$SUM7" && -z "$(meta_status "$REPO7")" ]]; then
    pass "(#11680) second self-resync leaves install-metadata.json byte-identical (no diff to commit)"
else
    fail "(#11680) second self-resync rewrote the stamp: $(git -C "$REPO7" diff -- .loom/install-metadata.json)"
fi
if [[ "$(read_field "$REPO7/.loom/install-metadata.json" loom_commit)" == "$FIRST7" \
      && "$FIRST7" != "$(git -C "$REPO7" rev-parse HEAD)" ]]; then
    pass "(#11680) loom_commit stays at the last sync that changed something, not the new HEAD"
else
    fail "(#11680) loom_commit chased HEAD"
fi
if grep -q "nothing to re-stamp (#11680)" <<<"$OUT7"; then
    pass "(#11680) the no-op is reported"
else
    fail "(#11680) no-op not reported; out=$OUT7"
fi

echo ""
echo "Test group 8: consumer -- metadata-only source commit + ssh/https remote flip -> no diff (#11680)"
C8="$(make_consumer_fixture consumer-converge yes yes)"
SRC8="$WORKDIR/consumer-converge-src"
git -C "$SRC8" remote add origin "git@github.com:o/r.git"
(cd "$C8" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ "$(read_field "$C8/.loom/install-metadata.json" loom_source_remote)" == "https://github.com/o/r.git" ]]; then
    pass "(#11680) an ssh scp-form origin is stamped in canonical https form"
else
    fail "(#11680) remote not normalized: $(read_field "$C8/.loom/install-metadata.json" loom_source_remote)"
fi
commit_all "$C8" "chore: resync"
SUM8="$(cksum < "$C8/.loom/install-metadata.json")"
# A metadata-only commit in the source (the shape of a self-resync commit on
# Loom itself) moves the source HEAD without touching any installed surface.
mkdir -p "$SRC8/.loom"
printf '{"loom_commit": "x"}\n' > "$SRC8/.loom/install-metadata.json"
commit_all "$SRC8" "chore: resync installed Loom surfaces"
git -C "$SRC8" remote set-url origin "https://github.com/o/r.git"
(cd "$C8" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ "$(cksum < "$C8/.loom/install-metadata.json")" == "$SUM8" && -z "$(meta_status "$C8")" ]]; then
    pass "(#11680) second resync from a moved, https-remote source produces no diff"
else
    fail "(#11680) consumer churned: $(git -C "$C8" diff -- .loom/install-metadata.json)"
fi

echo ""
echo "Test group 9: a real surface change or version bump still restamps everything (#11680 control)"
printf 'C\n' > "$SRC8/defaults/hooks/guard.sh"
commit_all "$SRC8" "feat: change a hook"
git -C "$SRC8" remote set-url origin "ssh://git@github.com:22/o/r.git"
printf '{\n  "loom_version": "1.2.3",\n  "loom_commit": "%s",\n  "last_resync": "2000-01-01",\n  "loom_source_remote": "stale",\n  "installed_files": []\n}\n' \
    "$(read_field "$C8/.loom/install-metadata.json" loom_commit)" > "$C8/.loom/install-metadata.json"
(cd "$C8" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ "$(read_field "$C8/.loom/install-metadata.json" loom_commit)" == "$(git -C "$SRC8" rev-parse HEAD)" \
      && "$(read_field "$C8/.loom/install-metadata.json" last_resync)" == "$TODAY" \
      && "$(read_field "$C8/.loom/install-metadata.json" loom_source_remote)" == "https://github.com/o/r.git" ]]; then
    pass "(#11680) a surface change restamps loom_commit (new HEAD), last_resync and the normalized remote"
else
    fail "(#11680) a surface change did not restamp: $(cat "$C8/.loom/install-metadata.json")"
fi
printf '{\n  "version": "1.2.4"\n}\n' > "$SRC8/package.json"
commit_all "$SRC8" "chore: bump version to 1.2.4"
(cd "$C8" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ "$(read_field "$C8/.loom/install-metadata.json" loom_version)" == "1.2.4" \
      && "$(read_field "$C8/.loom/install-metadata.json" loom_commit)" == "$(git -C "$SRC8" rev-parse HEAD)" ]]; then
    pass "(#11680) a version bump with no surface change still restamps loom_version + loom_commit"
else
    fail "(#11680) a version bump was skipped as a no-op: $(cat "$C8/.loom/install-metadata.json")"
fi

echo ""
echo "Test group 10: an unusable recorded loom_commit is repaired even when nothing changed (#11680)"
for BAD in old 1234567 unknown; do
    sed -i.bak "s/\"loom_commit\": *\"[^\"]*\"/\"loom_commit\": \"$BAD\"/" "$C8/.loom/install-metadata.json"
    (cd "$C8" && bash "$SCRIPT" >/dev/null 2>&1)
    if [[ "$(read_field "$C8/.loom/install-metadata.json" loom_commit)" == "$(git -C "$SRC8" rev-parse HEAD)" ]]; then
        pass "(#11680) loom_commit '$BAD' is repaired to the full source HEAD"
    else
        fail "(#11680) loom_commit '$BAD' was not repaired: $(cat "$C8/.loom/install-metadata.json")"
    fi
done

echo ""
echo "Test group 11: a no-op run still applies the legacy loom_source removal (#11680)"
SUM11_COMMIT="$(read_field "$C8/.loom/install-metadata.json" loom_commit)"
python3 -I - "$C8/.loom/install-metadata.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["loom_source"] = "/abs/host/path"
json.dump(d, open(p, "w"), indent=2)
open(p, "a").write("\n")
PY
git -C "$SRC8" commit -q --allow-empty -m "chore: unrelated" >/dev/null 2>&1
(cd "$C8" && bash "$SCRIPT" >/dev/null 2>&1)
if ! grep -q '"loom_source"' "$C8/.loom/install-metadata.json" \
    && [[ "$(read_field "$C8/.loom/install-metadata.json" loom_commit)" == "$SUM11_COMMIT" ]]; then
    pass "(#11680) loom_source dropped while loom_commit is kept"
else
    fail "(#11680) legacy edit not applied on a no-op: $(cat "$C8/.loom/install-metadata.json")"
fi

echo ""
echo "Test group 12: the python3 fallback behaves like jq (#11680)"
SHIM="$WORKDIR/nojq-bin"
mkdir -p "$SHIM"
printf '#!/bin/sh\nexit 1\n' > "$SHIM/jq"
chmod +x "$SHIM/jq"
git -C "$SRC8" commit -q --allow-empty -m "chore: unrelated 2" >/dev/null 2>&1
SUM12="$(cksum < "$C8/.loom/install-metadata.json")"
(cd "$C8" && PATH="$SHIM:$PATH" bash "$SCRIPT" >/dev/null 2>&1)
if [[ "$(cksum < "$C8/.loom/install-metadata.json")" == "$SUM12" ]]; then
    pass "(#11680) python3 path: a no-op run leaves the file byte-identical"
else
    fail "(#11680) python3 path rewrote a no-op: $(cat "$C8/.loom/install-metadata.json")"
fi
sed -i.bak "s/\"loom_commit\": *\"[^\"]*\"/\"loom_commit\": \"old\"/" "$C8/.loom/install-metadata.json"
(cd "$C8" && PATH="$SHIM:$PATH" bash "$SCRIPT" >/dev/null 2>&1)
if [[ "$(read_field "$C8/.loom/install-metadata.json" loom_commit)" == "$(git -C "$SRC8" rev-parse HEAD)" ]]; then
    pass "(#11680) python3 path: a bad loom_commit is still repaired"
else
    fail "(#11680) python3 path did not repair: $(cat "$C8/.loom/install-metadata.json")"
fi

echo ""
echo "Test group 13: normalize_source_remote canonical forms (#11680)"
eval "$(sed -n '/^normalize_source_remote() {/,/^}/p' "$SCRIPT")"
while IFS='|' read -r IN WANT; do
    GOT="$(normalize_source_remote "$IN")"
    if [[ "$GOT" == "$WANT" ]]; then
        pass "(#11680) '$IN' -> '$WANT'"
    else
        fail "(#11680) '$IN' -> '$GOT' (want '$WANT')"
    fi
done <<'EOF'
git@github.com:rjwalters/loom.git|https://github.com/rjwalters/loom.git
git@github.com:rjwalters/loom|https://github.com/rjwalters/loom.git
ssh://git@github.com/rjwalters/loom.git|https://github.com/rjwalters/loom.git
ssh://git@gitea.example:2222/o/r|https://gitea.example/o/r.git
https://github.com/rjwalters/loom.git|https://github.com/rjwalters/loom.git
https://github.com/rjwalters/loom|https://github.com/rjwalters/loom.git
https://x-access-token:SECRET@github.com/o/r.git|https://github.com/o/r.git
/Users/someone/GitHub/loom|/Users/someone/GitHub/loom
file:///srv/loom.git|file:///srv/loom.git
EOF
if [[ -z "$(normalize_source_remote "")" ]]; then
    pass "(#11680) an empty remote stays empty"
else
    fail "(#11680) an empty remote was rewritten"
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
