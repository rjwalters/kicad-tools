#!/usr/bin/env bash
# test-resync-installed.sh - Smoke tests for resync-installed.sh (#3777, #4239)
#
# Constructs throwaway git repos with synthetic defaults/ and installed .loom/
# trees so it can deterministically exercise the load-bearing cases:
#   (a) already in sync     -> exit 0, "Already in sync", no writes
#   (b) drift (differing)   -> file rewritten to match defaults/, exit 0
#   (c) missing installed   -> file created from defaults/, exit 0
#   (d) --dry-run + drift    -> exit 2, installed file UNCHANGED
#   (e) --dry-run + in sync -> exit 0
#   (f) repo-specific file   -> file present only in .loom/ left untouched
#   (g) .loom/resync-ignore  -> pinned file reported "skipped", not overwritten
#   (h) idempotent rerun     -> second run reports all unchanged
# Widened surfaces (#4239):
#   (i) drift in each new surface (roles/docs/bin/commands) -> updated + exit 2 on dry-run
#   (j) local-only custom role -> survives untouched
#   (k) resync-ignore pins a new-surface file -> reported "skipped"
#   (l) symlinked install target -> skipped, not clobbered
#   (l2) symlinked SOURCE file -> resolved to its content (not a copied link),
#        appears in the per-file report (updated/unchanged), destination-side
#        symlink protection (l) is unaffected (#5222)
#   (m) recorded loom_source gone -> clear error, exit 1
#   (n) metadata re-stamp -> loom_version/loom_commit/last_resync present after apply
# Canonical-guard-defer (#4041, #4403, #4566):
#   (o) canonical guard + git-TRACKED vendored guard -> preserved, tree clean, and
#       reported as an informational note (NOT a WARN) that --quiet suppresses
#   (p) canonical guard + UNTRACKED vendored guard   -> removed (unchanged behavior)
# Worktree-isolation refusal (#4563):
#   (q) invoked from a linked worktree -> non-zero exit, NOTHING written to main
#   (r) --allow-worktree / LOOM_RESYNC_ALLOW_WORKTREE=1 -> permitted (warns)
#   (s) main checkout (incl. a subdirectory of it)      -> unaffected, exit 0
# Self-update safety (#4669):
#   (t) the REAL script, installed as a padded "older" copy, resyncs itself from
#       a substantially different newer source -> run completes, no mid-run
#       syntax error, self-copy applied LAST, other surfaces fully refreshed
#   (u) an updated file gets a new inode (staged + renamed, not truncated) with
#       its permissions preserved, and leaves no .resync-stage.* dirt
#   (v) an unsyncable file -> explicit PARTIAL REFRESH report + exit 1
# Plus contract checks:
#   - --help prints usage (documenting --allow-worktree), exit 0
#   - unknown arg exits 1
#   - not-a-git-repo exits 1
#
# Usage:
#   ./.loom/scripts/tests/test-resync-installed.sh

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

# Not counted toward pass/fail: used when a test's precondition (e.g. a
# `update-gitignore`-capable loom-daemon binary) is unavailable in this
# environment, so CI without a built daemon does not spuriously fail.
skip() {
    echo -e "  SKIP: $1"
}

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-resync.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

# --- fixture builder ---------------------------------------------------------
# Creates a git repo at $WORKDIR/repo with:
#   defaults/hooks/guard.sh          (source of truth, "A")
#   defaults/scripts/foo.sh          (source of truth, "S")
#   defaults/scripts/lib/bar.sh      (source of truth, "L")
#   .loom/hooks/guard.sh             (installed, "OLD" -> drift)
#   .loom/scripts/foo.sh             (installed, "S"   -> in sync)
#   (.loom/scripts/lib/bar.sh MISSING -> to be created)
#   .loom/scripts/custom-only.sh     (repo-specific, no defaults/ counterpart)
# Widened surfaces (#4239):
#   defaults/roles/builder.md            -> .loom/roles/builder.md (drift)
#   defaults/roles/symlinked-role.md     -> .loom/roles/symlinked-role.md (SOURCE
#                                            is a symlink to a sibling file, #5222 —
#                                            mirrors defaults/roles/*.md -> ../.claude/
#                                            commands/loom/*.md in the real repo)
#   defaults/docs/troubleshooting.md     -> .loom/docs/troubleshooting.md (drift)
#   defaults/.loom/bin/loom              -> .loom/bin/loom (drift)
#   defaults/.claude/commands/loom/x.md  -> .claude/commands/loom/x.md (drift)
#   .loom/roles/custom-role.md           (repo-specific, no defaults/ counterpart)
#   package.json ("version": "9.9.9")    (loom_version source for re-stamp)
#   .loom/install-metadata.json          (re-stamp target; loom_source -> $repo)
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts/lib" \
             "$repo/defaults/roles" "$repo/defaults/docs" \
             "$repo/defaults/.loom/bin" "$repo/defaults/.claude/commands/loom" \
             "$repo/.loom/hooks" "$repo/.loom/scripts/lib" \
             "$repo/.loom/roles" "$repo/.loom/docs" \
             "$repo/.loom/bin" "$repo/.claude/commands/loom"
    git -C "$repo" init -q

    printf 'A\n' > "$repo/defaults/hooks/guard.sh"
    printf 'S\n' > "$repo/defaults/scripts/foo.sh"
    printf 'L\n' > "$repo/defaults/scripts/lib/bar.sh"
    chmod +x "$repo/defaults/hooks/guard.sh" "$repo/defaults/scripts/foo.sh" \
             "$repo/defaults/scripts/lib/bar.sh"

    printf 'OLD\n' > "$repo/.loom/hooks/guard.sh"
    printf 'S\n'   > "$repo/.loom/scripts/foo.sh"
    printf 'REPO-SPECIFIC\n' > "$repo/.loom/scripts/custom-only.sh"

    # Widened pure-copy surfaces (#4239): each drifts (installed differs from
    # defaults) so a single apply exercises all four new surfaces at once.
    printf 'ROLE-NEW\n' > "$repo/defaults/roles/builder.md"
    printf 'ROLE-OLD\n' > "$repo/.loom/roles/builder.md"
    printf 'CUSTOM-ROLE\n' > "$repo/.loom/roles/custom-role.md"   # local-only

    # #5222: a SOURCE-side symlink, mirroring the real defaults/roles/*.md ->
    # ../.claude/commands/loom/*.md skillification layout. sync_one/resync_tree
    # must resolve this to its target's content, never copy the link itself.
    printf 'SYMLINK-TARGET-CONTENT\n' > "$repo/defaults/roles/_symlink-target.md"
    ln -s "_symlink-target.md" "$repo/defaults/roles/symlinked-role.md"

    printf 'DOC-NEW\n' > "$repo/defaults/docs/troubleshooting.md"
    printf 'DOC-OLD\n' > "$repo/.loom/docs/troubleshooting.md"

    printf 'BIN-NEW\n' > "$repo/defaults/.loom/bin/loom"
    printf 'BIN-OLD\n' > "$repo/.loom/bin/loom"
    chmod +x "$repo/defaults/.loom/bin/loom"

    printf 'CMD-NEW\n' > "$repo/defaults/.claude/commands/loom/builder.md"
    printf 'CMD-OLD\n' > "$repo/.claude/commands/loom/builder.md"

    # Version source + metadata re-stamp target.
    printf '{\n  "version": "9.9.9"\n}\n' > "$repo/package.json"
    printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
        "$repo" > "$repo/.loom/install-metadata.json"

    # A real commit so loom_commit re-stamps to an actual short sha.
    git -C "$repo" add -A >/dev/null 2>&1
    git -C "$repo" commit -qm "fixture" >/dev/null 2>&1

    echo "$repo"
}

# --- (a) in-sync / (b) drift / (c) missing: a single apply run --------------
echo "Test group 1: apply resyncs drift + creates missing, leaves the rest"
REPO="$(make_fixture)"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "apply exits 0"; else fail "apply exits 0 (got $RC)"; fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(b) drifted hooks/guard.sh rewritten to match defaults"
else
    fail "(b) drifted hooks/guard.sh not updated"
fi
if [[ -f "$REPO/.loom/scripts/lib/bar.sh" && "$(cat "$REPO/.loom/scripts/lib/bar.sh")" == "L" ]]; then
    pass "(c) missing scripts/lib/bar.sh created from defaults"
else
    fail "(c) missing scripts/lib/bar.sh not created"
fi
if [[ "$(cat "$REPO/.loom/scripts/custom-only.sh")" == "REPO-SPECIFIC" ]]; then
    pass "(f) repo-specific custom-only.sh left untouched"
else
    fail "(f) repo-specific custom-only.sh was modified/removed"
fi
if grep -q "updated" <<<"$OUT" && grep -q "created" <<<"$OUT"; then
    pass "reports both 'updated' and 'created'"
else
    fail "did not report both updated and created"
fi

# --- (h) idempotent rerun ----------------------------------------------------
echo "Test group 2: idempotent rerun is a no-op"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(h) second run reports already in sync, exit 0"
else
    fail "(h) second run not a clean no-op (rc=$RC)"
fi

# --- (d) dry-run + drift: exit 2, no writes ----------------------------------
echo "Test group 3: --dry-run previews without writing"
REPO="$(make_fixture)"
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(d) --dry-run with drift exits 2"
else
    fail "(d) --dry-run with drift exits 2 (got $RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(d) --dry-run left installed file UNCHANGED"
else
    fail "(d) --dry-run modified the installed file"
fi
if [[ ! -f "$REPO/.loom/scripts/lib/bar.sh" ]]; then
    pass "(d) --dry-run did not create the missing file"
else
    fail "(d) --dry-run created a file it should only have previewed"
fi

# --- (e) dry-run + in sync: exit 0 -------------------------------------------
echo "Test group 4: --dry-run when already in sync exits 0"
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)   # apply first
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "already in sync" <<<"$OUT"; then
    pass "(e) --dry-run in sync exits 0"
else
    fail "(e) --dry-run in sync exits 0 (rc=$RC)"
fi

# --- (g) resync-ignore pins a local override ---------------------------------
echo "Test group 5: .loom/resync-ignore preserves a pinned local override"
REPO="$(make_fixture)"
printf 'PINNED-LOCAL\n' > "$REPO/.loom/hooks/guard.sh"
printf 'hooks/guard.sh  # keep my local tweak\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "skipped" <<<"$OUT"; then
    pass "(g) pinned file reported as skipped"
else
    fail "(g) pinned file not reported skipped (rc=$RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "PINNED-LOCAL" ]]; then
    pass "(g) pinned file NOT overwritten"
else
    fail "(g) pinned file was overwritten despite resync-ignore"
fi

# --- (i) widened surfaces: drift detected + fixed ----------------------------
echo "Test group 7: widened surfaces (roles/docs/bin/commands) resync"
REPO="$(make_fixture)"
# dry-run first: drift across the new surfaces must be detected (exit 2)
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(i) --dry-run detects drift across widened surfaces (exit 2)"
else
    fail "(i) --dry-run did not exit 2 across widened surfaces (got $RC)"
fi
for surf in "roles/builder.md" "docs/troubleshooting.md" "bin/loom" "commands/loom/builder.md"; do
    if grep -q "$surf" <<<"$OUT"; then
        pass "(i) --dry-run reports drift for $surf"
    else
        fail "(i) --dry-run did not report drift for $surf"
    fi
done
# apply, then verify each installed surface now matches defaults
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(i) apply exits 0"; else fail "(i) apply exits 0 (got $RC)"; fi
if [[ "$(cat "$REPO/.loom/roles/builder.md")" == "ROLE-NEW" ]]; then
    pass "(i) roles/builder.md resynced from defaults"
else
    fail "(i) roles/builder.md not resynced"
fi
if [[ "$(cat "$REPO/.loom/docs/troubleshooting.md")" == "DOC-NEW" ]]; then
    pass "(i) docs/troubleshooting.md resynced from defaults"
else
    fail "(i) docs/troubleshooting.md not resynced"
fi
if [[ "$(cat "$REPO/.loom/bin/loom")" == "BIN-NEW" && -x "$REPO/.loom/bin/loom" ]]; then
    pass "(i) bin/loom resynced (and executable bit preserved)"
else
    fail "(i) bin/loom not resynced or not executable"
fi
if [[ "$(cat "$REPO/.claude/commands/loom/builder.md")" == "CMD-NEW" ]]; then
    pass "(i) commands/loom/builder.md resynced from defaults"
else
    fail "(i) commands/loom/builder.md not resynced"
fi
# second run is a clean no-op across the widened surfaces too
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(i) widened surfaces idempotent (second run already in sync)"
else
    fail "(i) widened surfaces not idempotent (rc=$RC)"
fi

# --- (j) local-only custom role survives -------------------------------------
echo "Test group 8: local-only custom role is never touched"
if [[ "$(cat "$REPO/.loom/roles/custom-role.md")" == "CUSTOM-ROLE" ]]; then
    pass "(j) local-only custom-role.md left untouched"
else
    fail "(j) local-only custom-role.md was modified/removed"
fi

# --- (k) resync-ignore pins a new-surface file -------------------------------
echo "Test group 9: .loom/resync-ignore pins a widened-surface file"
REPO="$(make_fixture)"
printf 'PINNED-ROLE\n' > "$REPO/.loom/roles/builder.md"
printf 'roles/builder.md  # keep my local role tweak\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "skipped" <<<"$OUT" && grep -q "roles/builder.md" <<<"$OUT"; then
    pass "(k) pinned roles/builder.md reported as skipped"
else
    fail "(k) pinned roles/builder.md not reported skipped (rc=$RC)"
fi
if [[ "$(cat "$REPO/.loom/roles/builder.md")" == "PINNED-ROLE" ]]; then
    pass "(k) pinned roles/builder.md NOT overwritten"
else
    fail "(k) pinned roles/builder.md was overwritten despite resync-ignore"
fi

# --- (l) symlinked install target is skipped, not clobbered ------------------
echo "Test group 10: symlinked install target is skipped (dogfood safety)"
REPO="$(make_fixture)"
# Replace the installed docs file with a symlink pointing back at defaults/
# (mirrors this repo's dogfood .loom/docs/*.md layout).
rm -f "$REPO/.loom/docs/troubleshooting.md"
ln -s "../../defaults/docs/troubleshooting.md" "$REPO/.loom/docs/troubleshooting.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -qi "symlink" <<<"$OUT"; then
    pass "(l) symlinked docs entry reported as skipped"
else
    fail "(l) symlinked docs entry not reported skipped (rc=$RC)"
fi
if [[ -L "$REPO/.loom/docs/troubleshooting.md" ]]; then
    pass "(l) symlink left intact (not clobbered into a regular file)"
else
    fail "(l) symlink was clobbered"
fi

# --- (l2) symlinked SOURCE file is resolved to content, not silently skipped -
#
# #5222: defaults/roles/symlinked-role.md (built by make_fixture as a symlink
# to the sibling defaults/roles/_symlink-target.md) mirrors the real repo's
# defaults/roles/*.md -> ../.claude/commands/loom/*.md skillification layout.
# Before the fix, plain `find -type f` lstats each entry, a symlink never
# matches `-type f`, so this file fell out of the walk entirely -- never
# reported, never copied -- while the consumer's install-metadata.json still
# got re-stamped current. The regression check: the resolved destination must
# be a REGULAR FILE with the target's content, not a symlink, and the file
# must show up in the per-file report.
echo "Test group 10b: symlinked SOURCE file is resolved to content (#5222)"
REPO="$(make_fixture)"
DRY_OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]] && grep -q "roles/symlinked-role.md" <<<"$DRY_OUT"; then
    pass "(l2) --dry-run reports the symlinked source file, not silently omitted"
else
    fail "(l2) --dry-run did not report roles/symlinked-role.md (rc=$RC)"
fi
if [[ ! -e "$REPO/.loom/roles/symlinked-role.md" ]]; then
    pass "(l2) --dry-run created nothing for the symlinked source file"
else
    fail "(l2) --dry-run unexpectedly wrote .loom/roles/symlinked-role.md"
fi
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(l2) apply exits 0"; else fail "(l2) apply exits 0 (got $RC)"; fi
if grep -q "roles/symlinked-role.md" <<<"$OUT"; then
    pass "(l2) apply reports the symlinked source file in the per-file output"
else
    fail "(l2) apply did not report roles/symlinked-role.md"
fi
if [[ -f "$REPO/.loom/roles/symlinked-role.md" && ! -L "$REPO/.loom/roles/symlinked-role.md" ]]; then
    pass "(l2) destination is a REGULAR FILE (not a copied symlink)"
else
    fail "(l2) destination is missing or is itself a symlink"
fi
if [[ "$(cat "$REPO/.loom/roles/symlinked-role.md" 2>/dev/null)" == "SYMLINK-TARGET-CONTENT" ]]; then
    pass "(l2) destination content matches the symlink's RESOLVED target"
else
    fail "(l2) destination content does not match the resolved target"
fi
# Idempotent rerun: no drift once the symlinked source has been resolved once.
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(l2) rerun is a clean no-op (symlinked source treated as unchanged)"
else
    fail "(l2) rerun was not a clean no-op (rc=$RC)"
fi
# The destination-side symlink protection case (l) must be completely
# unaffected by resolving SOURCE-side symlinks.
REPO2="$(make_fixture)"
rm -f "$REPO2/.loom/docs/troubleshooting.md"
ln -s "../../defaults/docs/troubleshooting.md" "$REPO2/.loom/docs/troubleshooting.md"
(cd "$REPO2" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ -L "$REPO2/.loom/docs/troubleshooting.md" ]]; then
    pass "(l2) destination-side symlink protection (l) still holds alongside the source-side fix"
else
    fail "(l2) destination-side symlink protection (l) regressed"
fi

# --- (m) recorded loom_source gone -> clear error, exit 1 --------------------
echo "Test group 11: recorded loom_source moved/deleted errors clearly"
REPO="$(make_fixture)"
rm -rf "$REPO/defaults"                       # no dogfood defaults/ tree
rm -f "$REPO/.loom/loom-source-path"           # no source sidecar
printf '{\n  "loom_source": "%s/gone"\n}\n' "$REPO" > "$REPO/.loom/install-metadata.json"
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)" || RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(m) missing source tree exits 1"
else
    fail "(m) missing source tree did not exit 1 (got $RC)"
fi
if grep -qi "could not locate" <<<"$OUT"; then
    pass "(m) missing source tree prints a clear error"
else
    fail "(m) missing source tree error message unclear"
fi

# --- (n) metadata re-stamp ---------------------------------------------------
echo "Test group 12: successful apply re-stamps install-metadata.json"
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)
META="$REPO/.loom/install-metadata.json"
if grep -q '"loom_version": *"9.9.9"' "$META"; then
    pass "(n) loom_version re-stamped from source package.json"
else
    fail "(n) loom_version not re-stamped"
fi
if grep -q "\"last_resync\": *\"$(date +%Y-%m-%d)\"" "$META"; then
    pass "(n) last_resync stamped with today's date"
else
    fail "(n) last_resync not stamped"
fi
if grep -q '"loom_commit"' "$META" && ! grep -q '"loom_commit": *"old"' "$META"; then
    pass "(n) loom_commit re-stamped (no longer the stale value)"
else
    fail "(n) loom_commit not re-stamped"
fi
# out-of-scope metadata fields must be preserved
if grep -q '"install_date": *"2020-01-01"' "$META"; then
    pass "(n) install_date preserved (installer-owned, out of scope)"
else
    fail "(n) install_date was altered"
fi
# --dry-run must NOT re-stamp
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" --dry-run >/dev/null 2>&1)
if grep -q '"loom_version": *"0.0.0"' "$REPO/.loom/install-metadata.json"; then
    pass "(n) --dry-run leaves install-metadata.json unstamped"
else
    fail "(n) --dry-run re-stamped metadata (should be preview-only)"
fi

# --- (#4528) install-metadata.json merge=ours driver wiring -----------------
echo "Test group 12g: resync wires the install-metadata.json merge=ours driver (#4528)"
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)
GA="$REPO/.gitattributes"
if [[ -f "$GA" ]] && grep -qxF ".loom/install-metadata.json merge=ours" "$GA"; then
    pass "(q) .gitattributes gets the install-metadata.json merge=ours rule"
else
    fail "(q) .gitattributes missing the merge=ours rule"
fi
if [[ "$(git -C "$REPO" config --get merge.ours.driver 2>/dev/null)" == "true" ]]; then
    pass "(q) local git config merge.ours.driver=true is set"
else
    fail "(q) local git config merge.ours.driver was not set"
fi
# Idempotent rerun: no duplicate marker block.
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)
OCCURRENCES="$(grep -cxF ".loom/install-metadata.json merge=ours" "$GA")"
if [[ "$OCCURRENCES" -eq 1 ]]; then
    pass "(q) rerun does not duplicate the .gitattributes rule"
else
    fail "(q) rerun duplicated the .gitattributes rule (found $OCCURRENCES occurrences)"
fi
# --dry-run must NOT write .gitattributes or local git config.
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" --dry-run >/dev/null 2>&1)
if [[ ! -f "$REPO/.gitattributes" ]]; then
    pass "(q) --dry-run leaves .gitattributes absent (preview-only)"
else
    fail "(q) --dry-run wrote .gitattributes (should be preview-only)"
fi
if [[ -z "$(git -C "$REPO" config --get merge.ours.driver 2>/dev/null)" ]]; then
    pass "(q) --dry-run leaves merge.ours.driver unset (preview-only)"
else
    fail "(q) --dry-run set merge.ours.driver (should be preview-only)"
fi
# End-to-end: a real merge conflict on install-metadata.json between two
# divergent branches resolves automatically to "ours" once the driver+
# attribute are wired up, instead of stopping for manual resolution.
MERGE_REPO="$(make_fixture)"
(cd "$MERGE_REPO" && bash "$SCRIPT" >/dev/null 2>&1)
git -C "$MERGE_REPO" add -A >/dev/null 2>&1
git -C "$MERGE_REPO" commit -qm "wire merge driver" >/dev/null 2>&1
MERGE_REPO_DEFAULT_BRANCH="$(git -C "$MERGE_REPO" symbolic-ref --short HEAD)"
git -C "$MERGE_REPO" checkout -qb host-a >/dev/null 2>&1
printf '{\n  "loom_version": "1.1.1",\n  "loom_commit": "aaa1111",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$MERGE_REPO" > "$MERGE_REPO/.loom/install-metadata.json"
git -C "$MERGE_REPO" commit -qam "host-a resync stamp" >/dev/null 2>&1
git -C "$MERGE_REPO" checkout -q "$MERGE_REPO_DEFAULT_BRANCH" >/dev/null 2>&1
printf '{\n  "loom_version": "2.2.2",\n  "loom_commit": "bbb2222",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$MERGE_REPO" > "$MERGE_REPO/.loom/install-metadata.json"
git -C "$MERGE_REPO" commit -qam "host-b resync stamp" >/dev/null 2>&1
if git -C "$MERGE_REPO" merge -q host-a >/dev/null 2>&1; then
    if grep -q '"loom_version": *"2.2.2"' "$MERGE_REPO/.loom/install-metadata.json"; then
        pass "(q) two hosts' resync stamps merge cleanly, keeping the local (ours) side"
    else
        fail "(q) merge succeeded but did not keep the local side's stamp"
    fi
else
    fail "(q) merge of two divergent resync stamps still conflicts"
fi

# --- (#4280) .gitignore managed-block refresh + audit ------------------------
echo "Test group 12b: resync refreshes the Loom-managed .gitignore block (#4280)"
# Resolve a loom-daemon binary that supports `update-gitignore` (this feature).
# Prefer $LOOM_DAEMON_BIN, then `loom-daemon` on PATH, then build-output under
# the real Loom checkout the script lives in. Skip the group (do not fail) when
# none is available — CI may run these shell tests without a built daemon.
resolve_capable_daemon_bin() {
    local candidate loom_root
    loom_root="$(cd "$HELPERS_DIR/../.." && pwd)"   # defaults/scripts -> repo root
    for candidate in \
        "${LOOM_DAEMON_BIN:-}" \
        "$(command -v loom-daemon 2>/dev/null || true)" \
        "$loom_root/loom-daemon/target/debug/loom-daemon" \
        "$loom_root/loom-daemon/target/release/loom-daemon" \
        "$loom_root/target/debug/loom-daemon" \
        "$loom_root/target/release/loom-daemon"; do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        if "$candidate" update-gitignore --help >/dev/null 2>&1; then
            echo "$candidate"; return 0
        fi
    done
    echo ""; return 0
}
GI_BIN="$(resolve_capable_daemon_bin)"
if [[ -z "$GI_BIN" ]]; then
    skip "(#4280) no update-gitignore-capable loom-daemon binary resolved — set LOOM_DAEMON_BIN to run"
else
    REPO="$(make_fixture)"
    # Seed a stale pre-#3642 managed block: well-formed markers, but missing the
    # runtime patterns added since (.loom/sweep-checkpoint/, .loom/worktrees-local/).
    cat > "$REPO/.gitignore" <<'GIEOF'
node_modules/
# >>> loom-managed (do not edit) >>>
# Loom runtime state (don't commit these)
.loom-in-use
.loom/state.json
.loom/worktrees/
.loom/logs/
# <<< loom-managed <<<
dist/
GIEOF
    OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$GI_BIN" bash "$SCRIPT" 2>&1)"
    RC=$?
    if [[ $RC -eq 0 ]]; then pass "(#4280) apply with a stale block exits 0"; else fail "(#4280) apply exits 0 (got $RC)"; fi
    # Exactly one managed block, markers preserved.
    if [[ "$(grep -c '>>> loom-managed' "$REPO/.gitignore")" -eq 1 && \
          "$(grep -c '<<< loom-managed' "$REPO/.gitignore")" -eq 1 ]]; then
        pass "(#4280) exactly one managed block, markers preserved"
    else
        fail "(#4280) managed block markers not exactly-one each"
    fi
    # The previously-absent runtime paths are now ignored.
    if (cd "$REPO" && git check-ignore .loom/sweep-checkpoint/issue-1.json >/dev/null 2>&1); then
        pass "(#4280) .loom/sweep-checkpoint/issue-1.json is now ignored"
    else
        fail "(#4280) .loom/sweep-checkpoint/ still not ignored after resync"
    fi
    if (cd "$REPO" && git check-ignore .loom/worktrees-local/x >/dev/null 2>&1); then
        pass "(#4280) .loom/worktrees-local/ is now ignored"
    else
        fail "(#4280) .loom/worktrees-local/ still not ignored after resync"
    fi
    # User content on both sides of the block survived.
    if grep -q '^node_modules/$' "$REPO/.gitignore" && grep -q '^dist/$' "$REPO/.gitignore"; then
        pass "(#4280) user content around the block preserved"
    else
        fail "(#4280) user content around the block was lost"
    fi
    # Idempotent: a second resync leaves the .gitignore byte-identical.
    cp "$REPO/.gitignore" "$WORKDIR/gi-before-2nd"
    (cd "$REPO" && LOOM_DAEMON_BIN="$GI_BIN" bash "$SCRIPT" >/dev/null 2>&1)
    if diff -q "$WORKDIR/gi-before-2nd" "$REPO/.gitignore" >/dev/null 2>&1; then
        pass "(#4280) second resync is byte-identical (idempotent block refresh)"
    else
        fail "(#4280) second resync mutated the .gitignore"
    fi

    # Audit: an untracked-and-unignored path under .loom/ is surfaced as a warning.
    REPO="$(make_fixture)"
    printf 'RUNTIME\n' > "$REPO/.loom/some-new-runtime-dir-marker"   # untracked, unignored
    OUT="$(cd "$REPO" && LOOM_DAEMON_BIN="$GI_BIN" bash "$SCRIPT" 2>&1)"
    if grep -qi "untracked-and-unignored" <<<"$OUT"; then
        pass "(#4280) audit reports an untracked-and-unignored .loom/ path"
    else
        fail "(#4280) audit did not report the untracked-and-unignored path"
    fi
fi

# --- (#4280) missing binary degrades to a loud warning, never a silent skip --
echo "Test group 12c: absent daemon binary -> loud warning, apply still exits 0 (#4280)"
REPO="$(make_fixture)"
# Force the resolver to find nothing: no LOOM_DAEMON_BIN, no PATH loom-daemon,
# no build-output under the fixture's SOURCE_ROOT (the fixture repo), and no
# machine-level install fallback (step 4 of loom_locate_daemon_bin) -- on a
# host with a genuine machine-level Loom install, leaving $HOME/
# LOOM_DAEMON_BIN_DIR untouched would let that step resolve the real binary
# and defeat this fixture (#5183).
NO_BIN_HOME="$(mktemp -d)"
OUT="$(cd "$REPO" && env -u LOOM_DAEMON_BIN PATH="/usr/bin:/bin" HOME="$NO_BIN_HOME" \
    LOOM_DAEMON_BIN_DIR="/nonexistent" bash "$SCRIPT" 2>&1)"
RC=$?
rm -rf "$NO_BIN_HOME"
if [[ $RC -eq 0 ]]; then
    pass "(#4280) apply still exits 0 when no daemon binary resolves"
else
    fail "(#4280) apply did not exit 0 with no daemon binary (got $RC)"
fi
if grep -qi "could not refresh the loom-managed .gitignore block\|no loom-daemon binary resolved" <<<"$OUT"; then
    pass "(#4280) missing binary produces a loud warning (not a silent skip)"
else
    fail "(#4280) missing binary did not produce the expected warning"
fi

# --- (#4285) targeted loom-workspace package.json version field edit --------
echo "Test group 12d: loom-workspace package.json decoy version field removal (#4285)"
REPO="$(make_fixture)"
printf '{\n  "name": "loom-workspace",\n  "version": "1.0.0",\n  "scripts": {"test": "my-custom-test"}\n}\n' \
    > "$REPO/package.json"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#4285) apply with a stub version field exits 0"; else fail "(#4285) apply exits 0 (got $RC)"; fi
if ! grep -q '"version"' "$REPO/package.json"; then
    pass "(#4285) loom-workspace package.json version field removed"
else
    fail "(#4285) loom-workspace package.json version field NOT removed"
fi
if grep -q '"name": *"loom-workspace"' "$REPO/package.json"; then
    pass "(#4285) loom-workspace package.json name preserved"
else
    fail "(#4285) loom-workspace package.json name was altered/lost"
fi
if grep -q "my-custom-test" "$REPO/package.json"; then
    pass "(#4285) consumer's customized scripts block preserved"
else
    fail "(#4285) consumer's customized scripts block was lost"
fi
if grep -q "package.json.*removed decoy" <<<"$OUT"; then
    pass "(#4285) apply reports the package.json version removal"
else
    fail "(#4285) apply did not report the package.json version removal"
fi
# Idempotent rerun: second apply is a clean no-op for package.json.
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q "package.json (no decoy version field)" <<<"$OUT"; then
    pass "(#4285) second run reports package.json unchanged (idempotent)"
else
    fail "(#4285) second run did not report package.json as unchanged"
fi

echo "Test group 12e: a consumer's OWN package.json (not loom-workspace) is untouched"
REPO="$(make_fixture)"
printf '{\n  "name": "my-real-project",\n  "version": "2.3.4"\n}\n' > "$REPO/package.json"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q '"version": *"2.3.4"' "$REPO/package.json"; then
    pass "(#4285) consumer's own package.json version left untouched"
else
    fail "(#4285) consumer's own package.json version was modified"
fi

echo "Test group 12f: .loom/resync-ignore pins package.json against the stub edit"
REPO="$(make_fixture)"
printf '{\n  "name": "loom-workspace",\n  "version": "1.0.0"\n}\n' > "$REPO/package.json"
printf 'package.json  # keep my pinned stub version\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q '"version": *"1.0.0"' "$REPO/package.json"; then
    pass "(#4285) pinned package.json version NOT removed"
else
    fail "(#4285) pinned package.json version was removed despite resync-ignore"
fi
if grep -q "skipped.*package.json" <<<"$OUT"; then
    pass "(#4285) pinned package.json reported as skipped"
else
    fail "(#4285) pinned package.json not reported skipped"
fi

# --- (#4403) canonical-guard-defer: git-tracked target must NOT be removed --
echo "Test group 14: canonical guard present + tracked vendored guard is preserved (#4403)"
REPO="$(make_fixture)"
mkdir -p "$REPO/.claude/skills/repo/hooks"
printf '#!/usr/bin/env bash\n# rjwalters/repo#29 canonical guard\n# implements worktree-write-confinement\necho canonical\n' \
    > "$REPO/.claude/skills/repo/hooks/guard-destructive.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/defaults/hooks/guard-destructive-generic.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/.loom/hooks/guard-destructive-generic.sh"
git -C "$REPO" add .loom/hooks/guard-destructive-generic.sh >/dev/null 2>&1
git -C "$REPO" commit -qm "track vendored guard" >/dev/null 2>&1
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#4403) apply exits 0 with a tracked vendored guard present"; else fail "(#4403) apply exits 0 (got $RC)"; fi
if [[ -f "$REPO/.loom/hooks/guard-destructive-generic.sh" ]]; then
    pass "(#4403) git-tracked hooks/guard-destructive-generic.sh preserved (not removed)"
else
    fail "(#4403) git-tracked hooks/guard-destructive-generic.sh was removed"
fi
if grep -q "git-tracked vendored fallback kept" <<<"$OUT"; then
    pass "(#4403) the preserved tracked file is reported explicitly"
else
    fail "(#4403) no report explaining the preserved tracked file"
fi
# #4566: a committed vendored guard is a deliberate, documented posture, so this
# is the expected steady state on EVERY run — it must not be reported at
# alarm level (a WARN here reprinted forever with no way to acknowledge it).
# (Scoped to this message: an unrelated WARN, e.g. the #4280 missing-daemon
# .gitignore notice, can legitimately appear in the same run.)
if grep -qEi "WARN.*(git-tracked|guard-destructive-generic)" <<<"$OUT"; then
    fail "(#4566) tracked vendored guard must not produce a WARN (got: $(grep -Ei "WARN.*(git-tracked|guard-destructive-generic)" <<<"$OUT" | head -1))"
else
    pass "(#4566) no WARN for the deliberately-tracked vendored guard"
fi
if [[ -z "$(cd "$REPO" && git status --porcelain -- .loom/hooks/guard-destructive-generic.sh 2>&1)" ]]; then
    pass "(#4403) tracked guard file stays non-dirty (no local mods/deletions) after the run"
else
    fail "(#4403) tracked guard file is dirty after the run"
fi
# #4566: routed through note(), so --quiet suppresses it entirely while the
# file is still preserved (behavior unchanged, only the reporting is quieter).
OUT_Q="$(cd "$REPO" && bash "$SCRIPT" --quiet 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#4566) --quiet rerun exits 0"; else fail "(#4566) --quiet rerun exits 0 (got $RC)"; fi
if grep -q "git-tracked vendored fallback kept" <<<"$OUT_Q"; then
    fail "(#4566) --quiet still printed the tracked-guard message (not routed through note())"
else
    pass "(#4566) --quiet suppresses the tracked-guard message"
fi
if [[ -f "$REPO/.loom/hooks/guard-destructive-generic.sh" ]]; then
    pass "(#4566) --quiet rerun still preserves the git-tracked vendored guard"
else
    fail "(#4566) --quiet rerun removed the git-tracked vendored guard"
fi

# --- (#4403) canonical-guard-defer: untracked target keeps existing behavior -
echo "Test group 15: canonical guard present + UNTRACKED vendored guard is still removed (#4403)"
REPO="$(make_fixture)"
mkdir -p "$REPO/.claude/skills/repo/hooks"
printf '#!/usr/bin/env bash\n# rjwalters/repo#29 canonical guard\n# implements worktree-write-confinement\necho canonical\n' \
    > "$REPO/.claude/skills/repo/hooks/guard-destructive.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/defaults/hooks/guard-destructive-generic.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/.loom/hooks/guard-destructive-generic.sh"
# Note: intentionally NOT git-added, so this is the normal consumer-repo case
# where .loom/ isn't committed.
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#4403) apply exits 0 with an untracked vendored guard present"; else fail "(#4403) apply exits 0 (got $RC)"; fi
if [[ ! -f "$REPO/.loom/hooks/guard-destructive-generic.sh" ]]; then
    pass "(#4403) untracked hooks/guard-destructive-generic.sh removed (existing behavior unchanged)"
else
    fail "(#4403) untracked hooks/guard-destructive-generic.sh was NOT removed"
fi
if grep -q "removed.*hooks/guard-destructive-generic.sh" <<<"$OUT"; then
    pass "(#4403) removal reported as before"
else
    fail "(#4403) removal not reported"
fi

# --- (#4894) capability-gap canonical guard: vendored guard must be KEPT ----
# The canonical guard carries the repo#29 VERSION marker but NOT the
# write-confinement CAPABILITY marker (the Repo Skills 0.7.0 shape that
# motivated #4894). Before #4894, CANONICAL_GUARD_PRESENT was version-only, so
# this untracked vendored copy would have been removed here too — stripping
# the dispatcher's fallback out from under it and leaving zero destructive-
# command coverage, since the dispatcher (correctly, post-#4894) declines to
# exec a canonical guard that fails the capability probe.
echo "Test group 15b: canonical guard has version marker but NOT capability marker -> vendored guard is kept, not removed (#4894)"
REPO="$(make_fixture)"
mkdir -p "$REPO/.claude/skills/repo/hooks"
printf '#!/usr/bin/env bash\n# rjwalters/repo#29 canonical guard ONLY\necho canonical\n' \
    > "$REPO/.claude/skills/repo/hooks/guard-destructive.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/defaults/hooks/guard-destructive-generic.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/.loom/hooks/guard-destructive-generic.sh"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#4894) apply exits 0 with a capability-gap canonical guard present"; else fail "(#4894) apply exits 0 (got $RC)"; fi
if [[ -f "$REPO/.loom/hooks/guard-destructive-generic.sh" ]]; then
    pass "(#4894) vendored guard-destructive-generic.sh is KEPT (dispatcher's fallback preserved)"
else
    fail "(#4894) vendored guard-destructive-generic.sh was removed despite the canonical guard lacking write-confinement"
fi

# --- (#4563) refuse to run from a linked worktree ----------------------------
#
# The installed .loom/ is always resolved against the PRIMARY worktree, so a run
# from a linked (issue/PR) worktree writes to the MAIN checkout — the 2026-07-30
# contamination incident. Assert the refusal, that it wrote NOTHING to main, and
# that the explicit overrides still permit the write.
echo "Test group 16: linked-worktree invocation is refused (#4563)"
REPO="$(make_fixture)"
LINKED_WT="$WORKDIR/linked-wt"
rm -rf "$LINKED_WT"
if ! git -C "$REPO" worktree add -q -b wt-4563 "$LINKED_WT" >/dev/null 2>&1; then
    skip "(#4563) git worktree add unavailable in this environment"
else
    RC=0; OUT="$(cd "$LINKED_WT" && bash "$SCRIPT" 2>&1)" || RC=$?
    if [[ $RC -ne 0 ]]; then
        pass "(#4563) run from a linked worktree exits non-zero (got $RC)"
    else
        fail "(#4563) run from a linked worktree did NOT refuse (exit 0)"
    fi
    if grep -qi "worktree" <<<"$OUT"; then
        pass "(#4563) refusal explains the worktree context"
    else
        fail "(#4563) refusal message does not mention the worktree"
    fi
    if grep -q -- "--allow-worktree" <<<"$OUT"; then
        pass "(#4563) refusal names the --allow-worktree override"
    else
        fail "(#4563) refusal does not name the override flag"
    fi
    # (b) NOTHING written under the main checkout's .loom/
    if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
        pass "(#4563) main checkout's drifted hooks/guard.sh left UNCHANGED"
    else
        fail "(#4563) main checkout's hooks/guard.sh was written from the worktree"
    fi
    if [[ ! -f "$REPO/.loom/scripts/lib/bar.sh" ]]; then
        pass "(#4563) main checkout's missing scripts/lib/bar.sh NOT created"
    else
        fail "(#4563) a file was created under the main checkout's .loom/"
    fi
    if grep -q '"loom_version": "0.0.0"' "$REPO/.loom/install-metadata.json"; then
        pass "(#4563) main checkout's install-metadata.json NOT re-stamped"
    else
        fail "(#4563) main checkout's install-metadata.json was re-stamped"
    fi
    # --dry-run is refused too: it reports on the MAIN checkout, not this worktree.
    RC=0; (cd "$LINKED_WT" && bash "$SCRIPT" --dry-run >/dev/null 2>&1) || RC=$?
    if [[ $RC -eq 1 ]]; then
        pass "(#4563) --dry-run from a linked worktree is also refused (exit 1)"
    else
        fail "(#4563) --dry-run from a linked worktree was not refused (got $RC)"
    fi

    # (c) the override permits the write (still targeting the MAIN checkout).
    RC=0; OUT="$(cd "$LINKED_WT" && bash "$SCRIPT" --allow-worktree 2>&1)" || RC=$?
    if [[ $RC -eq 0 ]]; then
        pass "(#4563) --allow-worktree permits the run (exit 0)"
    else
        fail "(#4563) --allow-worktree did not permit the run (got $RC)"
    fi
    if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
        pass "(#4563) --allow-worktree wrote the main checkout's installed copy"
    else
        fail "(#4563) --allow-worktree did not apply the resync"
    fi
    if grep -qi "WARN.*linked worktree" <<<"$OUT"; then
        pass "(#4563) --allow-worktree still WARNs that writes target the main checkout"
    else
        fail "(#4563) --allow-worktree did not warn about the main-checkout target"
    fi

    # The env override is the non-interactive equivalent of the flag.
    REPO="$(make_fixture)"
    rm -rf "$LINKED_WT"
    git -C "$REPO" worktree add -q -b wt-4563-env "$LINKED_WT" >/dev/null 2>&1
    RC=0; (cd "$LINKED_WT" && LOOM_RESYNC_ALLOW_WORKTREE=1 bash "$SCRIPT" >/dev/null 2>&1) || RC=$?
    if [[ $RC -eq 0 && "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
        pass "(#4563) LOOM_RESYNC_ALLOW_WORKTREE=1 is equivalent to --allow-worktree"
    else
        fail "(#4563) LOOM_RESYNC_ALLOW_WORKTREE=1 did not permit the run (rc=$RC)"
    fi
fi

# --- (#4563) the MAIN checkout is completely unaffected ----------------------
#
# Including from a SUBDIRECTORY of it, where `git rev-parse --git-common-dir`
# returns a RELATIVE path ("../../.git") — a naive string compare against the
# absolute `--show-toplevel` would refuse this legitimate run.
echo "Test group 17: main-checkout invocation (incl. subdirectories) still works (#4563)"
REPO="$(make_fixture)"
RC=0; (cd "$REPO/defaults/scripts" && bash "$SCRIPT" >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(#4563) run from a main-checkout subdirectory exits 0"
else
    fail "(#4563) run from a main-checkout subdirectory was refused (got $RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(#4563) run from a main-checkout subdirectory still applies the resync"
else
    fail "(#4563) run from a main-checkout subdirectory did not apply the resync"
fi

# --- (#4669) the resync must survive updating the script it is running -------
#
# resync-installed.sh is itself a file under defaults/scripts/, so every run
# copies a newer version over the very path the running bash process is still
# reading from. The old in-place `cp` truncated and rewrote that file, letting
# bash resume reading the (shorter) new file at a stale byte offset -> `syntax
# error near unexpected token`, aborting the run with dozens of surfaces
# already partially refreshed.
#
# This drives the REAL script as the installed/running copy, padded so it
# differs substantially in both content and byte offsets from the newer source
# that replaces it mid-run — the exact reported scenario.
echo "Test group 18: self-update is atomic + deferred; the run completes (#4669)"

# Builds $1/.loom/scripts/resync-installed.sh as a padded "older" variant of the
# real script, with the real script as the defaults/ source it will sync from.
make_self_update_fixture() {
    local repo="$1"
    mkdir -p "$repo/defaults/scripts" "$repo/.loom/scripts"
    cp "$SCRIPT" "$repo/defaults/scripts/resync-installed.sh"
    chmod +x "$repo/defaults/scripts/resync-installed.sh"
    {
        head -n 1 "$SCRIPT"
        awk 'BEGIN { for (i = 0; i < 6000; i++) print "# old-installed-version padding line " i }'
        tail -n +2 "$SCRIPT"
    } > "$repo/.loom/scripts/resync-installed.sh"
    chmod +x "$repo/.loom/scripts/resync-installed.sh"
}

REPO="$(make_fixture)"
make_self_update_fixture "$REPO"
INSTALLED_RESYNC="$REPO/.loom/scripts/resync-installed.sh"
RC=0; OUT="$(cd "$REPO" && bash "$INSTALLED_RESYNC" 2>&1)" || RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(#4669) a self-updating run completes cleanly (exit 0)"
else
    fail "(#4669) a self-updating run did not exit 0 (got $RC)"
fi
if grep -qiE "syntax error|unexpected token|unexpected end of file" <<<"$OUT"; then
    fail "(#4669) the running script observed a half-written copy of itself: $(grep -iE "syntax error|unexpected token|unexpected end of file" <<<"$OUT" | head -1)"
else
    pass "(#4669) no mid-run syntax error from the rewritten script"
fi
if cmp -s "$REPO/defaults/scripts/resync-installed.sh" "$INSTALLED_RESYNC"; then
    pass "(#4669) the installed resync script was updated to match defaults/"
else
    fail "(#4669) the installed resync script was not updated"
fi
if [[ -x "$INSTALLED_RESYNC" ]] && bash -n "$INSTALLED_RESYNC" 2>/dev/null; then
    pass "(#4669) the updated installed script is executable and syntactically whole"
else
    fail "(#4669) the updated installed script is not executable/parseable"
fi
# The self-update must not cost the rest of the refresh (the reported failure
# left unrelated surfaces half-refreshed).
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" && -f "$REPO/.loom/scripts/lib/bar.sh" && \
      "$(cat "$REPO/.loom/roles/builder.md")" == "ROLE-NEW" && \
      "$(cat "$REPO/.claude/commands/loom/builder.md")" == "CMD-NEW" ]]; then
    pass "(#4669) every other surface was still fully refreshed in the same run"
else
    fail "(#4669) the self-updating run left other surfaces unrefreshed"
fi
# Deferral: the self-copy is applied only after every other surface settled.
SELF_LINE="$(grep -n "scripts/resync-installed.sh" <<<"$OUT" | tail -1 | cut -d: -f1)"
OTHER_LINE="$(grep -n "commands/loom/builder.md" <<<"$OUT" | tail -1 | cut -d: -f1)"
if [[ -n "$SELF_LINE" && -n "$OTHER_LINE" && "$SELF_LINE" -gt "$OTHER_LINE" ]]; then
    pass "(#4669) the self-copy is applied last, after the other surfaces"
else
    fail "(#4669) the self-copy was not deferred to the end (self=$SELF_LINE other=$OTHER_LINE)"
fi
# Rerunning the freshly-updated installed copy is a clean no-op.
RC=0; OUT="$(cd "$REPO" && bash "$INSTALLED_RESYNC" 2>&1)" || RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(#4669) rerunning the updated installed copy is idempotent (already in sync)"
else
    fail "(#4669) rerunning the updated installed copy was not a clean no-op (rc=$RC)"
fi
# --dry-run must still preview the self-update without writing it.
REPO="$(make_fixture)"
make_self_update_fixture "$REPO"
INSTALLED_RESYNC="$REPO/.loom/scripts/resync-installed.sh"
cp "$INSTALLED_RESYNC" "$WORKDIR/self-before-dry-run"
RC=0; OUT="$(cd "$REPO" && bash "$INSTALLED_RESYNC" --dry-run 2>&1)" || RC=$?
if [[ $RC -eq 2 ]] && grep -q "scripts/resync-installed.sh" <<<"$OUT"; then
    pass "(#4669) --dry-run previews the self-update (exit 2)"
else
    fail "(#4669) --dry-run did not preview the self-update (rc=$RC)"
fi
if cmp -s "$WORKDIR/self-before-dry-run" "$INSTALLED_RESYNC"; then
    pass "(#4669) --dry-run left the running script byte-identical"
else
    fail "(#4669) --dry-run modified the running script"
fi

# --- (#4669) writes are staged + renamed, never done in place ----------------
echo "Test group 19: installed files are replaced by atomic rename (#4669)"
REPO="$(make_fixture)"
INODE_BEFORE="$(ls -i "$REPO/.loom/hooks/guard.sh" | awk '{print $1}')"
MODE_BEFORE="$(ls -l "$REPO/.loom/docs/troubleshooting.md" | awk '{print $1}')"
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)
INODE_AFTER="$(ls -i "$REPO/.loom/hooks/guard.sh" | awk '{print $1}')"
MODE_AFTER="$(ls -l "$REPO/.loom/docs/troubleshooting.md" | awk '{print $1}')"
if [[ -n "$INODE_BEFORE" && "$INODE_BEFORE" != "$INODE_AFTER" ]]; then
    pass "(#4669) an updated file gets a NEW inode (renamed into place, not truncated)"
else
    fail "(#4669) the updated file kept its inode (still rewritten in place)"
fi
if [[ "$MODE_BEFORE" == "$MODE_AFTER" ]]; then
    pass "(#4669) the rename preserves the destination's permissions (not mktemp's 0600)"
else
    fail "(#4669) permissions changed across the rename ($MODE_BEFORE -> $MODE_AFTER)"
fi
STRAY_STAGE="$(find "$REPO" -name '.resync-stage.*' 2>/dev/null | wc -l | tr -d '[:space:]')"
if [[ "$STRAY_STAGE" == "0" ]]; then
    pass "(#4669) no staging temp files are left behind"
else
    fail "(#4669) $STRAY_STAGE staging temp file(s) left behind"
fi

# --- (#4669) a failed file is reported as a PARTIAL refresh, never swallowed --
echo "Test group 20: an unsyncable file reports a PARTIAL refresh and exits 1 (#4669)"
if [[ "$(id -u)" -eq 0 ]]; then
    skip "(#4669) running as root — an unwritable destination cannot be simulated"
else
    REPO="$(make_fixture)"
    chmod 500 "$REPO/.loom/scripts/lib"      # scripts/lib/bar.sh cannot be staged here
    RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)" || RC=$?
    chmod 700 "$REPO/.loom/scripts/lib"
    if [[ $RC -eq 1 ]]; then
        pass "(#4669) an unsyncable file exits 1"
    else
        fail "(#4669) an unsyncable file did not exit 1 (got $RC)"
    fi
    if grep -q "PARTIAL REFRESH" <<<"$OUT" && grep -q "scripts/lib/bar.sh" <<<"$OUT"; then
        pass "(#4669) the summary names the partial state and the failed path"
    else
        fail "(#4669) the summary did not report the partial state / failed path"
    fi
    if grep -qi "re-running completes the refresh\|fixing the cause" <<<"$OUT"; then
        pass "(#4669) the summary states the recovery action"
    else
        fail "(#4669) the summary does not state a recovery action"
    fi
    if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
        pass "(#4669) unrelated surfaces are still refreshed despite the failure"
    else
        fail "(#4669) the failure aborted the rest of the refresh"
    fi
    if grep -q "Already in sync\|file(s) updated," <<<"$OUT"; then
        fail "(#4669) a partial refresh still printed a success summary"
    else
        pass "(#4669) no success summary is printed for a partial refresh"
    fi
fi

# --- (w) .loom/runtimes/ backfill for a workspace that never had it (#4688) --
echo "Test group 21: .loom/runtimes/ is backfilled when absent (#4688)"
# Builds its own throwaway repo rather than reusing make_fixture(), so it can
# deliberately NOT create .loom/runtimes/ at all — the exact live-incident
# layout: .loom/roles/ (and the rest of a normal install) present, but
# .loom/runtimes/ was never provisioned by any prior install/resync.
RUNTIMES_REPO="$WORKDIR/runtimes-repo"
rm -rf "$RUNTIMES_REPO"
mkdir -p "$RUNTIMES_REPO/defaults/roles" "$RUNTIMES_REPO/defaults/runtimes" \
         "$RUNTIMES_REPO/.loom/roles"
git -C "$RUNTIMES_REPO" init -q
printf 'ROLE\n' > "$RUNTIMES_REPO/defaults/roles/builder.md"
printf 'ROLE\n' > "$RUNTIMES_REPO/.loom/roles/builder.md"
printf '{"runtime":"claude","capabilities":{"mcp":"yes"}}\n' > "$RUNTIMES_REPO/defaults/runtimes/claude.json"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$RUNTIMES_REPO" > "$RUNTIMES_REPO/.loom/install-metadata.json"
git -C "$RUNTIMES_REPO" add -A >/dev/null 2>&1
git -C "$RUNTIMES_REPO" commit -qm "fixture" >/dev/null 2>&1

if [[ ! -d "$RUNTIMES_REPO/.loom/runtimes" ]]; then
    pass "(w) fixture precondition: .loom/runtimes/ absent before resync"
else
    fail "(w) fixture precondition: .loom/runtimes/ unexpectedly present before resync"
fi

# --dry-run must report the directory would be populated, without writing.
OUT="$(cd "$RUNTIMES_REPO" && bash "$SCRIPT" --dry-run 2>&1)"
if grep -q "runtimes/claude.json" <<<"$OUT"; then
    pass "(w) --dry-run reports runtimes/claude.json would be created"
else
    fail "(w) --dry-run did not mention runtimes/claude.json"
fi
if [[ ! -d "$RUNTIMES_REPO/.loom/runtimes" ]]; then
    pass "(w) --dry-run does not create .loom/runtimes/"
else
    fail "(w) --dry-run created .loom/runtimes/ (should be preview-only)"
fi

# apply: the directory must now exist and be populated from defaults/.
OUT="$(cd "$RUNTIMES_REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(w) apply exits 0"
else
    fail "(w) apply did not exit 0 (got $RC)"
fi
if [[ -d "$RUNTIMES_REPO/.loom/runtimes" ]]; then
    pass "(w) .loom/runtimes/ created by apply"
else
    fail "(w) .loom/runtimes/ was not created by apply"
fi
if [[ "$(cat "$RUNTIMES_REPO/.loom/runtimes/claude.json" 2>/dev/null)" == '{"runtime":"claude","capabilities":{"mcp":"yes"}}' ]]; then
    pass "(w) .loom/runtimes/claude.json populated from defaults/runtimes/"
else
    fail "(w) .loom/runtimes/claude.json missing or content mismatch"
fi
# second run is a clean no-op.
OUT="$(cd "$RUNTIMES_REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(w) runtimes backfill is idempotent (second run already in sync)"
else
    fail "(w) runtimes backfill is not idempotent (rc=$RC)"
fi

# --- contract checks ---------------------------------------------------------
echo "Test group 13: flag/contract checks"
if bash "$SCRIPT" --help 2>&1 | grep -q "resync-installed.sh"; then
    pass "--help prints usage"
else
    fail "--help did not print usage"
fi
if bash "$SCRIPT" --help >/dev/null 2>&1; then pass "--help exits 0"; else fail "--help did not exit 0"; fi
HELP_OUT="$(bash "$SCRIPT" --help 2>&1)"
if grep -q -- "--allow-worktree" <<<"$HELP_OUT" && grep -qi "linked worktree" <<<"$HELP_OUT"; then
    pass "(#4563) --help documents --allow-worktree and the refusal behavior"
else
    fail "(#4563) --help does not document --allow-worktree / the refusal behavior"
fi

REPO="$(make_fixture)"
RC=0; (cd "$REPO" && bash "$SCRIPT" --bogus >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 1 ]]; then pass "unknown arg exits 1"; else fail "unknown arg did not exit 1 (got $RC)"; fi

NON_REPO="$WORKDIR/not-a-repo"
mkdir -p "$NON_REPO"
RC=0; (cd "$NON_REPO" && bash "$SCRIPT" >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 1 ]]; then pass "outside a git repo exits 1"; else fail "outside a git repo did not exit 1 (got $RC)"; fi

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
