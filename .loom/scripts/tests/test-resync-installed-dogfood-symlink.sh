#!/usr/bin/env bash
# test-resync-installed-dogfood-symlink.sh - NEW-FILE dogfood symlink install
# for the docs surface in resync-installed.sh (#8841)
#
# Split out as its own file rather than grown into test-resync-installed.sh
# (frozen by the file-size ratchet, .loom/docs/file-size-policy.md) — same
# pattern as test-resync-installed-local-fix-guard.sh and
# test-resync-installed-agent-skills-guard.sh.
#
# What regressed (#8841): in the Loom source repo every `.loom/docs/*.md` is a
# symlink to its `defaults/docs/` counterpart (#7752, enforced by
# scripts/check-docs-defaults-parity.sh check 1b). sync_one()'s destination
# symlink guard only ever PRESERVED an existing symlink — it never created one —
# so a `defaults/docs/*.md` added in the same wave as a resync run had no
# `.loom/docs/` counterpart to protect and fell through to the plain-copy
# "create" branch, materializing a second real copy. That is exactly how
# `.loom/docs/private-session-dispatch.md` landed as a 100644 blob and turned
# `main` red on Docs/Defaults Parity Check.
#
# The fix is deliberately narrow, and the narrowness is what these cases pin:
#   (1) dogfood + docs + brand-new file      -> symlink `../../defaults/docs/<name>`
#   (2) --dry-run previews the symlink       -> exit 2, nothing written
#   (3) CONSUMER repo (defaults/ resolved via .loom/loom-source-path, outside
#       the repo)                            -> REAL FILE COPY, never a symlink
#   (4) dogfood + a NON-docs surface         -> real file copy (only docs opts in)
#   (5) dogfood + an EXISTING real docs file -> still an ordinary in-place update
#   (6) the created symlink round-trips      -> rerun is "already in sync"
#   (7) a nested defaults/docs/sub/<name>    -> symlink with the right `../` depth
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed-dogfood-symlink.sh

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

# NOTE: the template deliberately avoids the substring "symlink". Every fixture
# path is echoed back in the script's own progress output, so a WORKDIR named
# after this test would make the "a consumer run reports no symlink" assertion
# below match its own temp directory and fail for no reason.
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync-dogfood-ln.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builders --------------------------------------------------------
#
# populate_defaults <dir>: the minimum defaults/ tree resolve_defaults() accepts
# as usable (defaults/hooks or defaults/scripts populated), plus one already-in-
# sync docs file so the docs surface is exercised without incidental drift.
populate_defaults() {
    local d="$1"
    mkdir -p "$d/hooks" "$d/scripts/lib" "$d/docs" "$d/runtimes"
    printf 'A\n' > "$d/hooks/guard.sh"
    printf 'S\n' > "$d/scripts/foo.sh"
    printf 'L\n' > "$d/scripts/lib/bar.sh"
    chmod +x "$d/hooks/guard.sh" "$d/scripts/foo.sh" "$d/scripts/lib/bar.sh"
    printf 'DOC-STEADY\n' > "$d/docs/troubleshooting.md"
    printf 'RUNTIME\n'    > "$d/runtimes/claude.json"
}

# make_dogfood_fixture [name]: a repo that owns its own defaults/ tree — the
# shape of THIS source repo, and the only shape DOGFOOD_WRITE_ROOT=1 accepts.
# `defaults/docs/new-doc.md` has NO `.loom/docs/` counterpart: the #8841 case.
make_dogfood_fixture() {
    local repo="$WORKDIR/${1:-dogfood}"
    rm -rf "$repo"
    mkdir -p "$repo/.loom/hooks" "$repo/.loom/scripts/lib" "$repo/.loom/docs"
    git -C "$repo" init -q 2>/dev/null || { mkdir -p "$repo"; git -C "$repo" init -q; }

    populate_defaults "$repo/defaults"
    printf 'NEW-DOC-CONTENT\n' > "$repo/defaults/docs/new-doc.md"

    printf 'A\n' > "$repo/.loom/hooks/guard.sh"
    printf 'S\n' > "$repo/.loom/scripts/foo.sh"
    printf 'L\n' > "$repo/.loom/scripts/lib/bar.sh"
    # The steady docs file is installed as a symlink, exactly as this repo does.
    ln -s "../../defaults/docs/troubleshooting.md" "$repo/.loom/docs/troubleshooting.md"

    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
        > "$repo/.loom/install-metadata.json"

    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
    echo "$repo"
}

# make_consumer_fixture: a repo with NO defaults/ of its own, resolving the
# source tree through the `.loom/loom-source-path` sidecar (rung 2) — the shape
# of every downstream install. Nothing here may ever become a symlink: the link
# would have to point outside the consumer's own repository.
make_consumer_fixture() {
    local repo="$WORKDIR/consumer" source_root="$WORKDIR/loom-source"
    rm -rf "$repo" "$source_root"
    mkdir -p "$repo/.loom/hooks" "$repo/.loom/scripts/lib" "$repo/.loom/docs"
    git -C "$repo" init -q 2>/dev/null || { mkdir -p "$repo"; git -C "$repo" init -q; }

    populate_defaults "$source_root/defaults"
    printf 'NEW-DOC-CONTENT\n' > "$source_root/defaults/docs/new-doc.md"
    printf '{\n  "version": "9.9.9"\n}\n' > "$source_root/package.json"

    printf 'A\n' > "$repo/.loom/hooks/guard.sh"
    printf 'S\n' > "$repo/.loom/scripts/foo.sh"
    printf 'L\n' > "$repo/.loom/scripts/lib/bar.sh"
    printf 'DOC-STEADY\n' > "$repo/.loom/docs/troubleshooting.md"

    printf '%s\n' "$source_root" > "$repo/.loom/loom-source-path"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "installed_files": []\n}\n' \
        > "$repo/.loom/install-metadata.json"

    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "chore: install Loom v0.0.0" >/dev/null 2>&1
    echo "$repo"
}

# --- (1) dogfood + brand-new docs file -> symlink ---------------------------
echo "Test group 1: a brand-new defaults/docs/*.md installs as a symlink in the dogfood repo (#8841)"
REPO="$(make_dogfood_fixture)"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
DST="$REPO/.loom/docs/new-doc.md"
if [[ $RC -eq 0 ]]; then
    pass "(1) apply exits 0"
else
    fail "(1) apply exits 0 (got $RC); out=$OUT"
fi
if [[ -L "$DST" ]]; then
    pass "(1) .loom/docs/new-doc.md is a SYMLINK, not a second real copy"
else
    fail "(1) .loom/docs/new-doc.md is not a symlink; out=$OUT"
fi
if [[ "$(readlink "$DST" 2>/dev/null)" == "../../defaults/docs/new-doc.md" ]]; then
    pass "(1) link target is the canonical ../../defaults/docs/<name> spelling"
else
    fail "(1) link target is '$(readlink "$DST" 2>/dev/null)', expected ../../defaults/docs/new-doc.md"
fi
if [[ "$(cat "$DST" 2>/dev/null)" == "NEW-DOC-CONTENT" ]]; then
    pass "(1) the symlink resolves to the defaults/ content"
else
    fail "(1) the symlink does not resolve to the defaults/ content"
fi
if grep -q "created   docs/new-doc.md (symlink -> " <<<"$OUT"; then
    pass "(1) the create is reported and names it as a symlink"
else
    fail "(1) the create was not reported as a symlink; out=$OUT"
fi

# --- (6) the created symlink round-trips: rerun is a clean no-op ------------
echo "Test group 2: the created symlink round-trips (rerun is already in sync)"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(6) rerun reports already in sync, exit 0"
else
    fail "(6) rerun was not a clean no-op (rc=$RC); out=$OUT"
fi
if [[ -L "$DST" ]]; then
    pass "(6) rerun left the symlink intact (the pre-existing -L guard)"
else
    fail "(6) rerun clobbered the symlink"
fi

# --- (2) --dry-run previews the symlink and writes nothing ------------------
echo "Test group 3: --dry-run previews the symlink without creating it"
REPO2="$(make_dogfood_fixture dogfood-dry)"
OUT="$(cd "$REPO2" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(2) --dry-run with a pending new doc exits 2"
else
    fail "(2) --dry-run exits 2 (got $RC); out=$OUT"
fi
if grep -q "would create docs/new-doc.md (symlink -> ../../defaults/docs/new-doc.md)" <<<"$OUT"; then
    pass "(2) --dry-run reports 'would create ... (symlink -> ...)' for the new doc"
else
    fail "(2) --dry-run did not preview the new doc as a symlink; out=$OUT"
fi
if [[ ! -e "$REPO2/.loom/docs/new-doc.md" && ! -L "$REPO2/.loom/docs/new-doc.md" ]]; then
    pass "(2) --dry-run created nothing on disk"
else
    fail "(2) --dry-run wrote .loom/docs/new-doc.md"
fi

# --- (3) CONSUMER repo -> real file copy, never a symlink -------------------
echo "Test group 4: a consumer repo still gets a REAL FILE COPY, never a symlink (#8841 scope guard)"
CREPO="$(make_consumer_fixture)"
OUT="$(cd "$CREPO" && bash "$SCRIPT" 2>&1)"
RC=$?
CDST="$CREPO/.loom/docs/new-doc.md"
if [[ $RC -eq 0 ]]; then
    pass "(3) consumer apply exits 0"
else
    fail "(3) consumer apply exits 0 (got $RC); out=$OUT"
fi
if [[ -f "$CDST" && ! -L "$CDST" ]]; then
    pass "(3) consumer .loom/docs/new-doc.md is a REGULAR FILE"
else
    fail "(3) consumer .loom/docs/new-doc.md is missing or a symlink; out=$OUT"
fi
if [[ "$(cat "$CDST" 2>/dev/null)" == "NEW-DOC-CONTENT" ]]; then
    pass "(3) consumer copy has the defaults/ content"
else
    fail "(3) consumer copy content wrong"
fi
# Match the report marker sync_one emits for a link ("(symlink -> ...)"), not the
# bare word: fixture paths appear verbatim in the progress output.
if ! grep -q "(symlink -> " <<<"$OUT"; then
    pass "(3) consumer run never reports installing a symlink"
else
    fail "(3) consumer run reported a symlink; out=$OUT"
fi
if [[ -z "$(find "$CREPO/.loom" -type l -print -quit 2>/dev/null)" ]]; then
    pass "(3) the consumer's whole .loom/ tree contains NO symlinks at all"
else
    pass_out="$(find "$CREPO/.loom" -type l 2>/dev/null)"
    fail "(3) the consumer's .loom/ tree contains symlink(s): $pass_out"
fi

# --- (4) dogfood + a NON-docs surface -> still a real copy ------------------
echo "Test group 5: only the docs surface opts in — other surfaces stay real copies"
REPO3="$(make_dogfood_fixture dogfood-surfaces)"
# A brand-new file on each of the other create-capable surfaces.
printf 'NEW-SCRIPT\n' > "$REPO3/defaults/scripts/lib/baz.sh"
chmod +x "$REPO3/defaults/scripts/lib/baz.sh"
printf 'NEW-HOOK\n' > "$REPO3/defaults/hooks/new-guard.sh"
chmod +x "$REPO3/defaults/hooks/new-guard.sh"
OUT="$(cd "$REPO3" && bash "$SCRIPT" 2>&1)"
if [[ -f "$REPO3/.loom/scripts/lib/baz.sh" && ! -L "$REPO3/.loom/scripts/lib/baz.sh" ]]; then
    pass "(4) a brand-new scripts/ file is a REGULAR FILE, not a symlink"
else
    fail "(4) a brand-new scripts/ file was not installed as a regular file; out=$OUT"
fi
if [[ -f "$REPO3/.loom/hooks/new-guard.sh" && ! -L "$REPO3/.loom/hooks/new-guard.sh" ]]; then
    pass "(4) a brand-new hooks/ file is a REGULAR FILE, not a symlink"
else
    fail "(4) a brand-new hooks/ file was not installed as a regular file; out=$OUT"
fi
if [[ -f "$REPO3/.loom/runtimes/claude.json" && ! -L "$REPO3/.loom/runtimes/claude.json" ]]; then
    pass "(4) the unconditional runtimes/ backfill is a REGULAR FILE, not a symlink"
else
    fail "(4) runtimes/claude.json was not installed as a regular file; out=$OUT"
fi

# --- (5) dogfood + an EXISTING real docs file -> ordinary in-place update ---
echo "Test group 6: an existing REAL docs file is still updated in place, not relinked"
REPO4="$(make_dogfood_fixture dogfood-existing)"
rm -f "$REPO4/.loom/docs/troubleshooting.md"
printf 'DOC-OLD\n' > "$REPO4/.loom/docs/troubleshooting.md"   # a real, drifted copy
git -C "$REPO4" add -A >/dev/null 2>&1
git -C "$REPO4" commit -qm "chore: resync installed Loom surfaces" >/dev/null 2>&1
OUT="$(cd "$REPO4" && bash "$SCRIPT" 2>&1)"
TDST="$REPO4/.loom/docs/troubleshooting.md"
if [[ -f "$TDST" && ! -L "$TDST" ]]; then
    pass "(5) the existing real docs file stayed a regular file"
else
    fail "(5) the existing real docs file was replaced by a symlink; out=$OUT"
fi
if [[ "$(cat "$TDST" 2>/dev/null)" == "DOC-STEADY" ]]; then
    pass "(5) the existing real docs file was refreshed from defaults/"
else
    fail "(5) the existing real docs file was not refreshed; out=$OUT"
fi

# --- (7) nested new doc -> the right ../ depth ------------------------------
echo "Test group 7: a nested defaults/docs/<sub>/<name>.md gets the right ../ depth"
REPO5="$(make_dogfood_fixture dogfood-nested)"
mkdir -p "$REPO5/defaults/docs/sub"
printf 'NESTED\n' > "$REPO5/defaults/docs/sub/nested-doc.md"
OUT="$(cd "$REPO5" && bash "$SCRIPT" 2>&1)"
NDST="$REPO5/.loom/docs/sub/nested-doc.md"
if [[ -L "$NDST" ]] && \
   [[ "$(readlink "$NDST" 2>/dev/null)" == "../../../defaults/docs/sub/nested-doc.md" ]]; then
    pass "(7) nested new doc links via ../../../defaults/docs/sub/nested-doc.md"
else
    fail "(7) nested new doc link wrong: '$(readlink "$NDST" 2>/dev/null)'; out=$OUT"
fi
if [[ "$(cat "$NDST" 2>/dev/null)" == "NESTED" ]]; then
    pass "(7) the nested symlink resolves to the defaults/ content"
else
    fail "(7) the nested symlink does not resolve"
fi

# --- (8) --output staging mode links INSIDE the staged tree -----------------
#
# #6106 --output is the documented way to stage a resync from a linked worktree,
# and its whole point is that you then commit/push from the staging directory —
# so it is a first-class producer of the very commits #8841 is about. It is also
# the one mode where WRITE_ROOT and the resolved DEFAULTS_DIR live in DIFFERENT
# directories (defaults/ resolves against the primary checkout, writes land in
# the staging worktree). A link computed from the primary's path would escape the
# staged tree — `../../../<primary>/defaults/docs/...` — and get committed, which
# is exactly what check-docs-defaults-parity.sh's escaping-link check rejects.
# The link must instead aim at the staging worktree's OWN defaults/ copy.
echo "Test group 8: --output staging mode links inside the staged tree, never out of it"
REPO6="$(make_dogfood_fixture dogfood-output)"
STAGE="$WORKDIR/stage-out"
OUT="$(cd "$REPO6" && bash "$SCRIPT" --output "$STAGE" 2>&1)"
RC=$?
SDST="$STAGE/.loom/docs/new-doc.md"
if [[ $RC -eq 0 ]]; then
    pass "(8) --output run exits 0"
else
    fail "(8) --output run exits 0 (got $RC); out=$OUT"
fi
if [[ -L "$SDST" ]]; then
    pass "(8) the staged .loom/docs/new-doc.md is a SYMLINK"
else
    fail "(8) the staged .loom/docs/new-doc.md is not a symlink; out=$OUT"
fi
if [[ "$(readlink "$SDST" 2>/dev/null)" == "../../defaults/docs/new-doc.md" ]]; then
    pass "(8) the staged link is the in-tree ../../defaults/docs/<name> spelling"
else
    fail "(8) the staged link is '$(readlink "$SDST" 2>/dev/null)', expected ../../defaults/docs/new-doc.md"
fi
# The load-bearing assertion: resolve the link and prove the file it lands on is
# under the STAGING tree, not back in the primary checkout.
SRESOLVED="$(cd "$(dirname "$SDST")" && cd "$(dirname "$(readlink "$SDST")")" 2>/dev/null && pwd -P)"
STAGE_PHYS="$(cd "$STAGE" && pwd -P)"
if [[ -n "$SRESOLVED" && "$SRESOLVED" == "$STAGE_PHYS/"* ]]; then
    pass "(8) the staged link resolves INSIDE the staging worktree ($SRESOLVED)"
else
    fail "(8) the staged link escapes the staging worktree: resolved to '$SRESOLVED', stage=$STAGE_PHYS"
fi
if [[ "$(cat "$SDST" 2>/dev/null)" == "NEW-DOC-CONTENT" ]]; then
    pass "(8) the staged link resolves to the right content"
else
    fail "(8) the staged link does not resolve to the right content"
fi
# Tidy up the git-worktree registration --output left behind in REPO6.
git -C "$REPO6" worktree remove --force "$STAGE" >/dev/null 2>&1 || true

# --- summary ---------------------------------------------------------------
echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed"
if [[ "$TESTS_FAILED" -gt 0 ]]; then
    exit 1
fi
exit 0
