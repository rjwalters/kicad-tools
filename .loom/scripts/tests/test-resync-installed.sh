#!/usr/bin/env bash
# test-resync-installed.sh - Smoke tests for resync-installed.sh (#3777, #4239)
#
# This file deliberately embeds a literal conflict-marker fixture (the #6162
# pre-resync syntax-gate case), so it opts itself out of
# check-conflict-markers.sh (#6499) with that script's in-file sentinel:
# check-conflict-markers:allow
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
#   (g2) .loom/resync-ignore honors the repo-relative pin spelling, e.g.
#        ".loom/hooks/foo.sh" or "./.loom/hooks/foo.sh" pinning the same file
#        as the ".loom/"-relative "hooks/foo.sh" form (#6515)
#   (g3) a .loom/resync-ignore pin matching nothing this run (typo, or a
#        retired file) is reported as "pin had no effect", with a "did you
#        mean" hint when a walked file shares its basename (#6515)
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
#   (#6032) re-stamp also STRIPS a legacy loom_source field, on both the jq
#       and (jq-unavailable) python3 fallback code paths; a fixture with no
#       loom_source field to begin with is a no-op (none is added)
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
# Retired payload files (#5981):
#   (x) a file listed in defaults/.loom-retired.list but with no defaults/
#       counterpart is REMOVED from the installed tree and reported with the
#       "removed" verb; --dry-run previews it (exit 2, "would remove") without
#       deleting; .loom/resync-ignore can pin it against removal exactly like
#       an update; a retired entry with no installed counterpart is a no-op
# Untracked-.loom/-path remedy classification (#5983, #6613):
#   (y) an untracked-and-unignored path under a pure-copy surface
#       (.loom/hooks|scripts|roles|docs|runtimes|bin/) that still exists under
#       defaults/ today is shipped payload -> audit_untracked_loom_paths()
#       recommends committing it directly, not adding it to EPHEMERAL_PATTERNS
#       and not the #6613 retired-file remedy either
#   (z) an untracked-and-unignored path outside any pure-copy surface is
#       genuine runtime state -> the existing EPHEMERAL_PATTERNS remedy is
#       unchanged
#   a path matching the pure-copy-surface PATTERN but with no defaults/
#       counterpart today (removed from defaults/ without a
#       defaults/.loom-retired.list entry) gets a third, distinct remedy
#       naming defaults/.loom-retired.list -- neither "commit them" nor
#       EPHEMERAL_PATTERNS
# Crash-detection marker (#5980):
#   a successful apply leaves no .loom/.resync-in-progress marker behind;
#   --dry-run never writes one; a leftover marker (simulating a crashed prior
#   run) is reported by BOTH --dry-run (untouched, pure detector) and a real
#   apply (which then restarts from scratch, idempotently, and clears the
#   marker on full success); a PARTIAL refresh leaves the marker in place
#   (recording the real target version + a timestamp/pid) until a later
#   successful retry clears it.
# Output-dir staging mode (#6106):
#   --output <dir> resyncs into a disposable, DETACHED `git worktree` instead
#   of the invoking repo's own checkout: the invoking repo (main OR a linked
#   worktree) is left completely unwritten, <dir> is a real independent git
#   checkout that actually received the resync (including files the invoking
#   repo never had), --output is permitted from a linked worktree (unlike a
#   bare run, #4563), an already-existing <dir> is refused, --dry-run +
#   --output leaves no staging worktree or dangling worktree registration
#   behind, LOOM_RESYNC_OUTPUT=<dir> is equivalent to the flag, and --output
#   with no value exits 1.
# Plus contract checks:
#   - --help prints usage (documenting --allow-worktree and --output), exit 0
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
#   defaults/.claude/README.md           -> .claude/README.md (drift, #5264)
#   defaults/.github/CONFIGURATION.md    -> .github/CONFIGURATION.md (drift, #5264)
#   .loom/roles/custom-role.md           (repo-specific, no defaults/ counterpart)
#   package.json ("version": "9.9.9")    (loom_version source for re-stamp)
#   .loom/install-metadata.json          (re-stamp target; loom_source -> $repo)
make_fixture() {
    local repo="$WORKDIR/repo"
    rm -rf "$repo"
    mkdir -p "$repo/defaults/hooks" "$repo/defaults/scripts/lib" \
             "$repo/defaults/roles" "$repo/defaults/docs" \
             "$repo/defaults/.loom/bin" "$repo/defaults/.claude/commands/loom" \
             "$repo/defaults/.github" \
             "$repo/.loom/hooks" "$repo/.loom/scripts/lib" \
             "$repo/.loom/roles" "$repo/.loom/docs" \
             "$repo/.loom/bin" "$repo/.claude/commands/loom" \
             "$repo/.github"
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

    # Single-file consumer-install docs (#5264): .claude/README.md and
    # .github/CONFIGURATION.md are copied verbatim into every consumer repo at
    # install time but, prior to #5264, were never resynced afterward.
    printf 'CLAUDE-README-NEW\n' > "$repo/defaults/.claude/README.md"
    printf 'CLAUDE-README-OLD\n' > "$repo/.claude/README.md"
    printf 'CONFIGURATION-NEW\n' > "$repo/defaults/.github/CONFIGURATION.md"
    printf 'CONFIGURATION-OLD\n' > "$repo/.github/CONFIGURATION.md"

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

# --- (g2) resync-ignore pin written in the natural repo-relative form -------
# (#6515) — a pin spelled ".loom/hooks/guard.sh" (or "./.loom/hooks/guard.sh")
# instead of the ".loom/"-relative "hooks/guard.sh" this script compares
# against must ALSO be honored, not silently ignored.
echo "Test group 5b: .loom/resync-ignore honors the repo-relative pin spelling (#6515)"
REPO="$(make_fixture)"
printf 'PINNED-LOCAL\n' > "$REPO/.loom/hooks/guard.sh"
printf '.loom/hooks/guard.sh  # keep my local tweak (repo-relative form)\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "skipped" <<<"$OUT"; then
    pass "(g2) repo-relative-form pin reported as skipped"
else
    fail "(g2) repo-relative-form pin not reported skipped (rc=$RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "PINNED-LOCAL" ]]; then
    pass "(g2) repo-relative-form pin NOT overwritten"
else
    fail "(g2) repo-relative-form pin was overwritten despite resync-ignore"
fi
if ! grep -q "pin had no effect" <<<"$OUT"; then
    pass "(g2) a pin that matched is not also reported dead"
else
    fail "(g2) a pin that matched was incorrectly reported as having no effect"
fi

# same again with a leading "./" on top of the repo-relative form.
REPO="$(make_fixture)"
printf 'PINNED-LOCAL\n' > "$REPO/.loom/hooks/guard.sh"
printf './.loom/hooks/guard.sh  # keep my local tweak (./ + repo-relative form)\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "skipped" <<<"$OUT" && [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "PINNED-LOCAL" ]]; then
    pass "(g2) './' + repo-relative-form pin also honored"
else
    fail "(g2) './' + repo-relative-form pin was not honored (rc=$RC)"
fi

# --- (g3) dead-pin reporting (#6515) -----------------------------------------
# A pin that matches nothing this run (typo, or a retired file) must be
# reported loudly instead of silently doing nothing forever.
echo "Test group 5c: dead .loom/resync-ignore pins are reported (#6515)"
REPO="$(make_fixture)"
printf 'hooks/gaurd.sh  # typo, does not exist\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if grep -q "pin had no effect: 'hooks/gaurd.sh'" <<<"$OUT"; then
    pass "(g3) a pin matching nothing is reported as having no effect"
else
    fail "(g3) a dead pin was not reported (rc=$RC)"
fi
# unrelated to whether hooks/guard.sh itself drifted -- the dead pin did not
# protect it, so it must still be resynced normally.
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(g3) the (unrelated) file the dead pin was NOT protecting still resynced"
else
    fail "(g3) hooks/guard.sh did not resync despite the pin not matching it"
fi

# a pin whose basename matches a walked file, but in the wrong directory,
# gets a "did you mean" suggestion naming the actual walked path.
REPO="$(make_fixture)"
printf 'scripts/guard.sh  # wrong directory, guard.sh actually lives under hooks/\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if grep -q "pin had no effect: 'scripts/guard.sh' (did you mean 'hooks/guard.sh'?)" <<<"$OUT"; then
    pass "(g3) dead pin gets a 'did you mean' suggestion when a basename match exists"
else
    fail "(g3) dead pin did not get the expected 'did you mean' suggestion (rc=$RC)"
fi

# a pin that DOES match is never reported as dead in the same run as one that
# doesn't -- (g) fixture pin ("hooks/guard.sh") lives alongside a dead one.
REPO="$(make_fixture)"
printf 'PINNED-LOCAL\n' > "$REPO/.loom/hooks/guard.sh"
printf 'hooks/guard.sh   # this one matches\nscripts/nonexistent.sh   # this one does not\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if grep -q "pin had no effect: 'scripts/nonexistent.sh'" <<<"$OUT" && ! grep -q "pin had no effect: 'hooks/guard.sh'" <<<"$OUT"; then
    pass "(g3) only the dead pin is reported; the live pin in the same file is not"
else
    fail "(g3) live/dead pin discrimination within one file failed (rc=$RC)"
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
for surf in "roles/builder.md" "docs/troubleshooting.md" "bin/loom" "commands/loom/builder.md" ".claude/README.md" ".github/CONFIGURATION.md"; do
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
if [[ "$(cat "$REPO/.claude/README.md")" == "CLAUDE-README-NEW" ]]; then
    pass "(i) .claude/README.md resynced from defaults (#5264)"
else
    fail "(i) .claude/README.md not resynced (#5264)"
fi
if [[ "$(cat "$REPO/.github/CONFIGURATION.md")" == "CONFIGURATION-NEW" ]]; then
    pass "(i) .github/CONFIGURATION.md resynced from defaults (#5264)"
else
    fail "(i) .github/CONFIGURATION.md not resynced (#5264)"
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

# --- (k2) .claude/README.md / .github/CONFIGURATION.md are gated on the ------
#          destination already existing (never force-populated, #5264)
echo "Test group 9b: single-file docs are not force-created for a consumer that never had them"
REPO="$(make_fixture)"
# make_fixture git-tracks both files (its `git add -A && git commit`), so a bare
# `rm -f` would NOT simulate "a consumer that never had them" — it leaves a
# *deleted-but-tracked* path that `git status --porcelain` reports as pending
# dirt (` D .claude/README.md`). resync-installed.sh's dirty-tree hint
# (suggest_commit_if_resync_only_dirt) then lists that path in its `git add`
# suggestion, which the "not reported at all" assertion below greps for and
# trips on. Drop the files from the index *and* the worktree and commit the
# removal, so the fixture is genuinely a repo that never received them.
git -C "$REPO" rm -q -- .claude/README.md .github/CONFIGURATION.md >/dev/null 2>&1
git -C "$REPO" commit -qm "consumer install without the single-file docs" >/dev/null 2>&1
if [[ -z "$(git -C "$REPO" status --porcelain -- .claude/README.md .github/CONFIGURATION.md)" ]]; then
    pass "(k2) fixture precondition: both single-file docs are absent AND untracked"
else
    fail "(k2) fixture precondition: single-file docs still show as pending git changes"
fi
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ ! -e "$REPO/.claude/README.md" ]]; then
    pass "(k2) .claude/README.md not force-created when absent"
else
    fail "(k2) .claude/README.md was force-created despite being absent"
fi
if [[ ! -e "$REPO/.github/CONFIGURATION.md" ]]; then
    pass "(k2) .github/CONFIGURATION.md not force-created when absent"
else
    fail "(k2) .github/CONFIGURATION.md was force-created despite being absent"
fi
if ! grep -q "\.claude/README\.md" <<<"$OUT" && ! grep -q "\.github/CONFIGURATION\.md" <<<"$OUT"; then
    pass "(k2) absent single-file docs are not reported at all"
else
    fail "(k2) absent single-file docs were unexpectedly reported"
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

# --- (m2) .loom/loom-source-path points at a path that does not exist at all
# -> same clear error, exit 1 (#6780 AC1: "missing directory" case) ----------
echo "Test group 11b: .loom/loom-source-path pointing at a nonexistent directory errors clearly (#6780)"
REPO="$(make_fixture)"
rm -rf "$REPO/defaults"
printf '%s/does-not-exist\n' "$REPO" > "$REPO/.loom/loom-source-path"
rm -f "$REPO/.loom/install-metadata.json"
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)" || RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(m2) nonexistent loom-source-path target exits 1"
else
    fail "(m2) nonexistent loom-source-path target did not exit 1 (got $RC)"
fi
if grep -qi "could not locate" <<<"$OUT"; then
    pass "(m2) nonexistent loom-source-path target prints a clear error"
else
    fail "(m2) nonexistent loom-source-path target error message unclear"
fi
if ! grep -qi "already in sync" <<<"$OUT"; then
    pass "(m2) nonexistent loom-source-path target never reports a false 'already in sync'"
else
    fail "(m2) nonexistent loom-source-path target falsely reported 'already in sync'. Got: $OUT"
fi

# --- (m3) .loom/loom-source-path points at a directory that EXISTS (and even
# has a `defaults/` subdirectory) but is not a real Loom checkout -- e.g. a
# scratch clone whose contents were emptied without removing the directory
# itself. Before #6780 this passed resolve_defaults()'s `-d "$src/defaults"`
# check, so the sync walk below found zero files under the (empty)
# defaults/hooks|scripts and reported a false "already in sync (0 unchanged)"
# -- the exact "reports the install as current" bug from #6780. Must now be
# treated exactly like a fully-missing directory: loud error, never success. -
echo "Test group 11c: .loom/loom-source-path pointing at a stale/empty source tree errors, not a false 'in sync' (#6780)"
REPO="$(make_fixture)"
rm -rf "$REPO/defaults"
STALE_SRC="$WORKDIR/stale-loom-source"
rm -rf "$STALE_SRC"
mkdir -p "$STALE_SRC/defaults"   # exists, but no hooks/ or scripts/ under it
printf '%s\n' "$STALE_SRC" > "$REPO/.loom/loom-source-path"
rm -f "$REPO/.loom/install-metadata.json"
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)" || RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(m3) stale/empty source tree exits 1"
else
    fail "(m3) stale/empty source tree did not exit 1 (got $RC)"
fi
if grep -qi "could not locate" <<<"$OUT"; then
    pass "(m3) stale/empty source tree prints a clear error"
else
    fail "(m3) stale/empty source tree error message unclear. Got: $OUT"
fi
if ! grep -qi "already in sync" <<<"$OUT"; then
    pass "(m3) stale/empty source tree never reports a false 'already in sync'"
else
    fail "(m3) stale/empty source tree falsely reported 'already in sync'. Got: $OUT"
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
# #6780 AC3: re-stamp writes a loom_source_remote key (empty string here,
# since the fixture's SOURCE_ROOT has no `origin` remote configured -- but
# the key itself must always be present, never omitted, so downstream
# tooling can distinguish "no remote" from "field never written").
if grep -q '"loom_source_remote"' "$META"; then
    pass "(#6780) re-stamp writes a loom_source_remote key"
else
    fail "(#6780) re-stamp did not write a loom_source_remote key. Got: $(cat "$META")"
fi
# --dry-run must NOT re-stamp
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" --dry-run >/dev/null 2>&1)
if grep -q '"loom_version": *"0.0.0"' "$REPO/.loom/install-metadata.json"; then
    pass "(n) --dry-run leaves install-metadata.json unstamped"
else
    fail "(n) --dry-run re-stamped metadata (should be preview-only)"
fi

# --- (#6032) legacy loom_source field is stripped on re-stamp (jq path) -----
echo "Test group 12p: re-stamp strips a legacy loom_source field (jq path, #6032)"
REPO="$(make_fixture)"
if grep -q '"loom_source"' "$REPO/.loom/install-metadata.json"; then
    pass "(#6032) fixture starts with a loom_source field to strip"
else
    fail "(#6032) fixture is missing loom_source — test precondition not met"
fi
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)
META="$REPO/.loom/install-metadata.json"
if ! grep -q '"loom_source"' "$META"; then
    pass "(#6032) loom_source stripped from install-metadata.json (jq path)"
else
    fail "(#6032) loom_source still present after re-stamp (jq path)"
fi
if grep -q '"loom_version": *"9.9.9"' "$META"; then
    pass "(#6032) other fields still re-stamped alongside the loom_source strip (jq path)"
else
    fail "(#6032) re-stamp regressed while stripping loom_source (jq path)"
fi

# --- (#6032) no-op: a fixture with no loom_source field stays that way -----
echo "Test group 12q: re-stamp is a no-op re: loom_source when it was never present (#6032)"
REPO="$(make_fixture)"
python3 -c '
import json
p = "'"$REPO"'/.loom/install-metadata.json"
with open(p) as f:
    data = json.load(f)
data.pop("loom_source", None)
with open(p, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
'
if ! grep -q '"loom_source"' "$REPO/.loom/install-metadata.json"; then
    pass "(#6032) fixture precondition: no loom_source field before apply"
else
    fail "(#6032) fixture still has loom_source — precondition not met"
fi
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)
if ! grep -q '"loom_source"' "$REPO/.loom/install-metadata.json"; then
    pass "(#6032) re-stamp does not add a loom_source field when absent"
else
    fail "(#6032) re-stamp introduced a loom_source field that was not there before"
fi

# --- (#6032) legacy loom_source field is stripped on re-stamp (python3 fallback) --
echo "Test group 12r: re-stamp strips a legacy loom_source field (python3 fallback, jq unavailable, #6032)"
# Build a PATH that resolves every currently-available command EXCEPT jq, so
# the script's own "command -v jq" probe genuinely fails and it falls through
# to the python3 code path in restamp_metadata() -- rather than merely
# narrowing PATH to "/usr/bin:/bin" (which still contains jq on most hosts).
NOJQ_BIN="$WORKDIR/nojq-bin"
mkdir -p "$NOJQ_BIN"
IFS=':' read -r -a _path_dirs <<< "$PATH"
for _d in "${_path_dirs[@]}"; do
    [[ -d "$_d" ]] || continue
    for _f in "$_d"/*; do
        [[ -x "$_f" ]] || continue
        _name="$(basename "$_f")"
        [[ "$_name" == "jq" ]] && continue
        [[ -e "$NOJQ_BIN/$_name" ]] && continue
        ln -s "$_f" "$NOJQ_BIN/$_name" 2>/dev/null
    done
done
if [[ ! -e "$NOJQ_BIN/jq" ]] && [[ -e "$NOJQ_BIN/python3" ]]; then
    pass "(#6032) constructed a PATH with python3 but no jq"
else
    fail "(#6032) could not construct a jq-less PATH with python3 (precondition not met)"
fi
REPO="$(make_fixture)"
(cd "$REPO" && PATH="$NOJQ_BIN" bash "$SCRIPT" >/dev/null 2>&1)
META="$REPO/.loom/install-metadata.json"
if ! grep -q '"loom_source"' "$META"; then
    pass "(#6032) loom_source stripped from install-metadata.json (python3 fallback)"
else
    fail "(#6032) loom_source still present after re-stamp (python3 fallback)"
fi
if grep -q '"loom_version": *"9.9.9"' "$META"; then
    pass "(#6032) other fields still re-stamped alongside the loom_source strip (python3 fallback)"
else
    fail "(#6032) re-stamp regressed while stripping loom_source (python3 fallback)"
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

# --- (#5983, #6613) audit classifies untracked .loom/ paths before choosing remedy text --
echo "Test group 12n: audit classifies untracked .loom/ paths before choosing remedy text (#5983, #6613)"

# (a) An untracked path under a pure-copy surface (.loom/scripts/) that STILL
# exists under defaults/scripts/ today is shipped payload -- the remedy should
# say to commit it, not point at EPHEMERAL_PATTERNS and not the #6613 retired
# remedy either. The new file is placed directly inside the already-tracked
# .loom/scripts/ directory (a sibling of the fixture's tracked foo.sh) rather
# than a brand-new subdirectory, so `git status --porcelain` reports it as its
# own path rather than folding it into a single untracked-directory line. Its
# defaults/scripts/ counterpart is created with matching content so #6613's
# "does this still exist under defaults/?" check finds it.
REPO="$(make_fixture)"
printf 'NEW-TEST\n' > "$REPO/.loom/scripts/check-defaults-version-bump.sh"   # untracked, unignored, pure-copy surface
printf 'NEW-TEST\n' > "$REPO/defaults/scripts/check-defaults-version-bump.sh"   # still shipped today (#6613)
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -qi "commit them" <<<"$OUT" && grep -q '.loom/scripts/check-defaults-version-bump.sh' <<<"$OUT"; then
    pass "(#5983) untracked payload path under .loom/scripts/ gets 'commit it' guidance"
else
    fail "(#5983) untracked payload path did not get 'commit it' guidance"
fi
if grep -qi "add them to EPHEMERAL_PATTERNS" <<<"$OUT"; then
    fail "(#5983) untracked payload-only path incorrectly suggested the EPHEMERAL_PATTERNS remedy"
else
    pass "(#5983) untracked payload-only path does not suggest the EPHEMERAL_PATTERNS remedy"
fi
if grep -qi "likely retired" <<<"$OUT"; then
    fail "(#6613) still-shipped payload path incorrectly suggested the retired-file remedy"
else
    pass "(#6613) still-shipped payload path does not suggest the retired-file remedy"
fi

# (b) An untracked path OUTSIDE any pure-copy surface (genuine runtime state)
# keeps today's EPHEMERAL_PATTERNS remedy, unchanged. The full fixture already
# exercises several widened-surface creations (roles/, docs/, bin/, commands/)
# that legitimately land in the payload bucket on their own -- so this case
# checks the runtime-state marker is scoped to the EPHEMERAL_PATTERNS block
# specifically, rather than asserting the payload block is empty.
REPO="$(make_fixture)"
printf 'RUNTIME\n' > "$REPO/.loom/some-new-runtime-dir-marker"   # untracked, unignored, not a pure-copy surface
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
runtime_block="$(sed -n '/not covered by the managed \.gitignore block/,/If these are Loom runtime state/p' <<<"$OUT")"
payload_block="$(sed -n '/commit them):/,/not covered by the managed \.gitignore block/p' <<<"$OUT")"
if grep -q '.loom/some-new-runtime-dir-marker' <<<"$runtime_block"; then
    pass "(#5983) untracked runtime-state path keeps the EPHEMERAL_PATTERNS remedy"
else
    fail "(#5983) untracked runtime-state path did not get the EPHEMERAL_PATTERNS remedy"
fi
if grep -q '.loom/some-new-runtime-dir-marker' <<<"$payload_block"; then
    fail "(#5983) untracked runtime-only path incorrectly suggested the shipped-payload remedy"
else
    pass "(#5983) untracked runtime-only path does not suggest the shipped-payload remedy"
fi

# (c) An untracked path matching a pure-copy-surface PATTERN (.loom/scripts/)
# but with NO defaults/scripts/ counterpart and NO defaults/.loom-retired.list
# entry is neither "commit them" (it's dead code, not current payload) nor the
# EPHEMERAL_PATTERNS remedy (it's not runtime state) -- it gets the #6613
# "likely retired" remedy that points at defaults/.loom-retired.list.
REPO="$(make_fixture)"
printf 'ORPHAN\n' > "$REPO/.loom/scripts/some-retired-tool.sh"   # untracked, unignored, no defaults/ counterpart
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
retired_block="$(sed -n '/likely retired, not committed payload/,/Do NOT commit them/p' <<<"$OUT")"
if grep -q '.loom/scripts/some-retired-tool.sh' <<<"$retired_block"; then
    pass "(#6613) untracked retired-but-unlisted path gets the retired-file remedy"
else
    fail "(#6613) untracked retired-but-unlisted path did not get the retired-file remedy"
fi
if grep -q 'defaults/\.loom-retired\.list' <<<"$OUT"; then
    pass "(#6613) retired-file remedy points at defaults/.loom-retired.list"
else
    fail "(#6613) retired-file remedy did not mention defaults/.loom-retired.list"
fi
payload_block="$(sed -n '/commit them):/,/likely retired, not committed payload/p' <<<"$OUT")"
if grep -q '.loom/scripts/some-retired-tool.sh' <<<"$payload_block"; then
    fail "(#6613) retired-but-unlisted path incorrectly got the 'commit them' payload remedy"
else
    pass "(#6613) retired-but-unlisted path does not get the 'commit them' payload remedy"
fi
if grep -qi "add them to EPHEMERAL_PATTERNS" <<<"$OUT" && grep -q '.loom/scripts/some-retired-tool.sh' \
    <<<"$(sed -n '/not covered by the managed \.gitignore block/,/If these are Loom runtime state/p' <<<"$OUT")"; then
    fail "(#6613) retired-but-unlisted path incorrectly got the EPHEMERAL_PATTERNS remedy"
else
    pass "(#6613) retired-but-unlisted path does not get the EPHEMERAL_PATTERNS remedy"
fi

# --- (#7336) suggest_commit_if_resync_only_dirt() applies the same #6613 ----
#     retired-vs-shipped classification as audit_untracked_loom_paths(),
#     instead of folding every pure-copy-surface-matching path straight into
#     its "git add ... && git commit" suggestion.
#
# Both an untracked retired-but-unlisted path (no defaults/ counterpart) and
# an untracked still-shipped path (has one) are placed directly in the
# fixture's already-tracked .loom/scripts/ directory, mirroring test group
# 12n's fixture layout above -- this exercises suggest_commit_if_resync_only_dirt
# (which scans the WHOLE tree, not just .loom/) at the same time as
# audit_untracked_loom_paths() (which only scans .loom/), since a real apply
# run below always executes both.
#
# make_fixture()'s initial commit never includes a .gitignore (it is only
# generated by refresh_gitignore_block() on the first apply), and an untracked
# .gitignore is not one of this function's hardcoded single-file cases -- so a
# first-ever apply on a fresh fixture always trips its catch-all "non-resync
# dirt present" bail before this test's own assertions get a chance to run.
# Prime the fixture with one full apply + commit first (a realistic
# already-installed, already-committed steady state) so the ONLY dirt left
# for the second, real apply below is the deliberately introduced fixture
# below.
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1) || true
git -C "$REPO" add -A >/dev/null 2>&1
git -C "$REPO" commit -qm "prime: first resync + generated .gitignore/.gitattributes" >/dev/null 2>&1
echo "Test group 12y: suggest_commit_if_resync_only_dirt() excludes retired-but-unlisted pure-copy-surface paths (#7336)"
printf 'ORPHAN\n' > "$REPO/.loom/scripts/some-retired-tool.sh"   # untracked, unignored, no defaults/ counterpart
printf 'NEW-TEST\n' > "$REPO/.loom/scripts/check-defaults-version-bump.sh"   # untracked, unignored, pure-copy surface
printf 'NEW-TEST\n' > "$REPO/defaults/scripts/check-defaults-version-bump.sh"   # still shipped today (#6613)
git -C "$REPO" add defaults/scripts/check-defaults-version-bump.sh >/dev/null 2>&1
git -C "$REPO" commit -qm "add new shipped defaults/ file" >/dev/null 2>&1
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
commit_line="$(grep -m1 '^ *git add ' <<<"$OUT")"
if [[ -n "$commit_line" ]]; then
    pass "(#7336) fixture precondition: the dirty-tree commit hint fired"
else
    fail "(#7336) fixture precondition: the dirty-tree commit hint did not fire, cannot test its content"
fi
if grep -q 'some-retired-tool\.sh' <<<"$commit_line"; then
    fail "(#7336) retired-but-unlisted path was incorrectly included in the 'git add' commit suggestion"
else
    pass "(#7336) retired-but-unlisted path is excluded from the 'git add' commit suggestion"
fi
if grep -q 'check-defaults-version-bump\.sh' <<<"$commit_line"; then
    pass "(#7336) still-shipped pure-copy-surface path remains in the 'git add' commit suggestion"
else
    fail "(#7336) still-shipped pure-copy-surface path was unexpectedly dropped from the 'git add' commit suggestion"
fi
# Every real apply re-stamps .loom/install-metadata.json (one of the
# function's OTHER hardcoded single-file case-block paths, unrelated to the
# pure-copy-surface pattern this fix touches) -- confirming it is still
# present guards against the fix accidentally narrowing that unrelated case.
if grep -q 'install-metadata\.json' <<<"$commit_line"; then
    pass "(#7336) other hardcoded single-file case (install-metadata.json) remains in the commit suggestion"
else
    fail "(#7336) other hardcoded single-file case (install-metadata.json) unexpectedly dropped from the commit suggestion"
fi
if grep -qi "excluded from the commit suggestion" <<<"$OUT" && grep -q 'defaults/\.loom-retired\.list' <<<"$OUT"; then
    pass "(#7336) retired-but-unlisted path gets its own remedy pointing at defaults/.loom-retired.list"
else
    fail "(#7336) retired-but-unlisted path did not get a remedy pointing at defaults/.loom-retired.list"
fi

# --- (#5294) stale-binary regression: a loom-daemon binary compiled before a
# given EPHEMERAL_PATTERNS entry existed, resolved ahead of a fresher
# repo-local build under default (no LOOM_PREFER_REPO_BUILD) resolver
# precedence, must not silently drop that pattern from the regenerated
# .gitignore -- this is the exact mechanism that reintroduced #5267's gitlink
# hazard 34 minutes after #5280 fixed it (05cf67e8). These fixtures use fake
# `loom-daemon` shell-script stand-ins (each emitting a canned managed block
# from a fixed pattern list) rather than the real Rust binary, so the
# regression is reproducible without compiling two different daemon versions.
#
# make_fake_daemon_bin <dest> <pattern>... -- writes an executable at <dest>
# that supports exactly `update-gitignore --help` (prints usage, exit 0) and
# `update-gitignore <repo>` (rewrites <repo>/.gitignore's loom-managed block
# to contain exactly the given patterns, preserving surrounding content).
make_fake_daemon_bin() {
    local dest="$1"; shift
    mkdir -p "$(dirname "$dest")"
    printf '%s\n' "$@" > "$dest.patterns"
    cat > "$dest" <<'FAKE_DAEMON_EOF'
#!/usr/bin/env bash
set -u
PATFILE="$0.patterns"
if [[ "${1:-}" == "update-gitignore" ]]; then
    if [[ "${2:-}" == "--help" ]]; then
        echo "usage: loom-daemon update-gitignore <repo>"
        exit 0
    fi
    repo="$2"
    gi="$repo/.gitignore"
    [[ -f "$gi" ]] || : > "$gi"
    tmp="$(mktemp)"
    awk 'BEGIN{skip=0} /# >>> loom-managed/{skip=1;next} /# <<< loom-managed/{skip=0;next} skip==0{print}' "$gi" > "$tmp"
    {
        cat "$tmp"
        echo "# >>> loom-managed (do not edit) >>>"
        echo "# Loom runtime state (don't commit these)"
        cat "$PATFILE"
        echo "# <<< loom-managed <<<"
    } > "$gi"
    rm -f "$tmp"
    exit 0
fi
exit 1
FAKE_DAEMON_EOF
    chmod +x "$dest"
}

# A synthetic post_init.rs declaring an "old" pattern (present in every fake
# binary below) plus a "just-added" pattern (present ONLY in the fresh
# repo-local build) -- mirrors #5280 adding `.claude/worktrees/` to source
# while a stale resolved binary still lacked it.
write_fake_post_init() {
    local repo="$1"
    mkdir -p "$repo/loom-daemon/src/init"
    cat > "$repo/loom-daemon/src/init/post_init.rs" <<'RUST_EOF'
pub const EPHEMERAL_PATTERNS: &[&str] = &[
    ".loom-in-use",
    ".fake-newly-added-pattern/",
];
RUST_EOF
}

echo "Test group 12h: gitignore refresh prefers a repo-local build over a stale PATH binary when no \$LOOM_DAEMON_BIN override is set (#5294)"
REPO="$(make_fixture)"
write_fake_post_init "$REPO"
make_fake_daemon_bin "$REPO/loom-daemon/target/release/loom-daemon" ".loom-in-use" ".fake-newly-added-pattern/"
STALE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fake-path.XXXXXX")"
make_fake_daemon_bin "$STALE_DIR/loom-daemon" ".loom-in-use"
NO_BIN_HOME="$(mktemp -d)"
OUT="$(cd "$REPO" && env -u LOOM_DAEMON_BIN PATH="$STALE_DIR:/usr/bin:/bin" HOME="$NO_BIN_HOME" \
    LOOM_DAEMON_BIN_DIR="/nonexistent" bash "$SCRIPT" 2>&1)"
RC=$?
rm -rf "$NO_BIN_HOME" "$STALE_DIR"
if [[ $RC -eq 0 ]]; then
    pass "(#5294) apply exits 0 with a stale PATH binary and a fresh repo-local build present"
else
    fail "(#5294) apply exits 0 with stale PATH + fresh repo build (got $RC)"
fi
GI_BLOCK="$(sed -n '/# >>> loom-managed/,/# <<< loom-managed/p' "$REPO/.gitignore")"
if grep -qxF ".fake-newly-added-pattern/" <<<"$GI_BLOCK"; then
    pass "(#5294) the newly-added source pattern landed in .gitignore (repo-local build was preferred over stale PATH)"
else
    fail "(#5294) newly-added pattern missing from .gitignore -- the stale PATH binary was used instead of the repo build"
fi
if grep -qi "regenerated .gitignore WITHOUT" <<<"$OUT"; then
    fail "(#5294) unexpected stale-binary warning even though the repo build covered every source pattern"
else
    pass "(#5294) no stale-binary warning printed when the resolved binary satisfies all source patterns"
fi

echo "Test group 12i: gitignore refresh warns loudly -- never silently -- when only a stale binary resolves (#5294)"
REPO="$(make_fixture)"
write_fake_post_init "$REPO"
# No repo-local build this time: resolution falls through to the stale PATH
# binary (the pre-#5294-fix behavior this whole issue is about).
STALE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fake-path.XXXXXX")"
make_fake_daemon_bin "$STALE_DIR/loom-daemon" ".loom-in-use"
NO_BIN_HOME="$(mktemp -d)"
OUT="$(cd "$REPO" && env -u LOOM_DAEMON_BIN PATH="$STALE_DIR:/usr/bin:/bin" HOME="$NO_BIN_HOME" \
    LOOM_DAEMON_BIN_DIR="/nonexistent" bash "$SCRIPT" 2>&1)"
RC=$?
rm -rf "$NO_BIN_HOME" "$STALE_DIR"
if [[ $RC -eq 0 ]]; then
    pass "(#5294) apply still exits 0 when only a stale binary resolves"
else
    fail "(#5294) apply exits 0 with only a stale binary resolvable (got $RC)"
fi
if grep -qi "regenerated .gitignore WITHOUT" <<<"$OUT" && grep -qF ".fake-newly-added-pattern/" <<<"$OUT"; then
    pass "(#5294) the dropped pattern is named in a loud warning instead of being silently lost"
else
    fail "(#5294) stale-binary warning did not name the missing pattern"
fi

# #5991: the guard above (#5294) only ever WARNED about a dropped pattern; it
# never fixed it, so the regressed .gitignore still landed whenever the
# warning scrolled past unread -- which is exactly what happened a third time
# in 94fa30f2 (#5985). Assert the enforcement half directly: a deliberately
# stale pattern list must not be able to produce a committed .gitignore
# missing a source-declared pattern -- the guard must restore it in place.
echo "Test group 12o: gitignore refresh RESTORES a pattern dropped by a stale binary, not just warns about it (#5991)"
REPO="$(make_fixture)"
write_fake_post_init "$REPO"
STALE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fake-path.XXXXXX")"
make_fake_daemon_bin "$STALE_DIR/loom-daemon" ".loom-in-use"
NO_BIN_HOME="$(mktemp -d)"
OUT="$(cd "$REPO" && env -u LOOM_DAEMON_BIN PATH="$STALE_DIR:/usr/bin:/bin" HOME="$NO_BIN_HOME" \
    LOOM_DAEMON_BIN_DIR="/nonexistent" bash "$SCRIPT" 2>&1)"
RC=$?
rm -rf "$NO_BIN_HOME" "$STALE_DIR"
if [[ $RC -eq 0 ]]; then
    pass "(#5991) apply still exits 0 when the stale-binary guard has to restore a pattern"
else
    fail "(#5991) apply exits 0 when the guard restores a pattern (got $RC)"
fi
GI_BLOCK="$(sed -n '/# >>> loom-managed/,/# <<< loom-managed/p' "$REPO/.gitignore")"
if grep -qxF ".fake-newly-added-pattern/" <<<"$GI_BLOCK"; then
    pass "(#5991) the pattern dropped by the stale binary was restored into .gitignore, not just named in a warning"
else
    fail "(#5991) .gitignore is still missing the source-declared pattern after the guard ran"
fi
if [[ "$(grep -c '>>> loom-managed' "$REPO/.gitignore")" -eq 1 && \
      "$(grep -c '<<< loom-managed' "$REPO/.gitignore")" -eq 1 ]]; then
    pass "(#5991) restore left exactly one well-formed managed block (markers not duplicated/corrupted)"
else
    fail "(#5991) restore left a malformed/duplicated managed block"
fi
if grep -qi "restored the missing pattern" <<<"$OUT"; then
    pass "(#5991) the restore is itself reported, not silent"
else
    fail "(#5991) restore happened without a corresponding report line"
fi
# Idempotent OUTPUT: the same stale binary drops the pattern and gets
# corrected again on every run, so a second resync still ends up with a
# byte-identical, fully-restored .gitignore (the stale binary itself never
# self-heals -- only rebuilding it does; see the warning's own guidance).
cp "$REPO/.gitignore" "$WORKDIR/gi-before-2nd-5991"
STALE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fake-path.XXXXXX")"
make_fake_daemon_bin "$STALE_DIR/loom-daemon" ".loom-in-use"
NO_BIN_HOME="$(mktemp -d)"
(cd "$REPO" && env -u LOOM_DAEMON_BIN PATH="$STALE_DIR:/usr/bin:/bin" HOME="$NO_BIN_HOME" \
    LOOM_DAEMON_BIN_DIR="/nonexistent" bash "$SCRIPT") >/dev/null 2>&1
RC2=$?
rm -rf "$NO_BIN_HOME" "$STALE_DIR"
if [[ $RC2 -eq 0 ]] && diff -q "$WORKDIR/gi-before-2nd-5991" "$REPO/.gitignore" >/dev/null 2>&1; then
    pass "(#5991) a second run with the same stale binary leaves .gitignore byte-identical (still fully restored)"
else
    fail "(#5991) second run with the same stale binary left .gitignore in a different (still-regressed?) state (rc=$RC2)"
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

# --- (#6532 review) a ".loom/"-prefixed pin must NOT collapse onto an
# unrelated bare top-level rel via the #6515 normalization -------------------
# ".loom/package.json" is a nonsensical pin (there is no file at that path --
# the root package.json's is_ignored() rel is the bare "package.json"), but
# the #6515 repo-relative normalization would otherwise strip its ".loom/"
# prefix down to "package.json" and silently match the unrelated root file
# anyway. That is exactly the class of silent-pin-misfire bug this PR exists
# to eliminate, just reintroduced by the fix's own normalization (flagged in
# Judge review: a sibling PR adds an analogous bare "CLAUDE.md" rel for the
# root guide, which would collide with a ".loom/CLAUDE.md" pin the same way).
echo "Test group 12s: a '.loom/'-prefixed pin does not collapse onto an unrelated bare top-level rel"
REPO="$(make_fixture)"
printf '{\n  "name": "loom-workspace",\n  "version": "1.0.0"\n}\n' > "$REPO/package.json"
printf '.loom/package.json  # meant to protect something under .loom/, NOT the root package.json\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if ! grep -q '"version"' "$REPO/package.json"; then
    pass "(#6532) '.loom/package.json' pin does NOT suppress the root package.json stub edit"
else
    fail "(#6532) '.loom/package.json' pin incorrectly collided with and suppressed the root package.json stub edit"
fi
if grep -q "pin had no effect: '\.loom/package\.json'" <<<"$OUT"; then
    pass "(#6532) the non-colliding '.loom/package.json' pin is correctly reported dead"
else
    fail "(#6532) the non-colliding '.loom/package.json' pin was not reported dead (rc check: $?)"
fi

# --- (#5559) targeted field edit: .loom/CLAUDE.md version-header restamp ----
echo "Test group 12j: .loom/CLAUDE.md version header restamp (#5559)"
REPO="$(make_fixture)"
printf '# Loom Orchestration - Repository Guide\n\n**Loom Version**: 0.16.0\n**Installation Date**: 2020-01-01\n\nBody text unaffected.\n\n**Generated by Loom Installation Process**\nLast updated: 2026-07-29\n' \
    > "$REPO/.loom/CLAUDE.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#5559) apply with a stale .loom/CLAUDE.md header exits 0"; else fail "(#5559) apply exits 0 (got $RC)"; fi
if grep -q '\*\*Loom Version\*\*: 9.9.9' "$REPO/.loom/CLAUDE.md"; then
    pass "(#5559) .loom/CLAUDE.md Loom Version header restamped to source version"
else
    fail "(#5559) .loom/CLAUDE.md Loom Version header NOT restamped"
fi
if grep -q "^Last updated: $(date +%Y-%m-%d)\$" "$REPO/.loom/CLAUDE.md"; then
    pass "(#5559) .loom/CLAUDE.md Last updated footer restamped to today"
else
    fail "(#5559) .loom/CLAUDE.md Last updated footer NOT restamped"
fi
if grep -q '\*\*Installation Date\*\*: 2020-01-01' "$REPO/.loom/CLAUDE.md"; then
    pass "(#5559) .loom/CLAUDE.md Installation Date header left untouched (original install date, not a resync stamp)"
else
    fail "(#5559) .loom/CLAUDE.md Installation Date header was altered"
fi
if grep -q "Body text unaffected." "$REPO/.loom/CLAUDE.md"; then
    pass "(#5559) .loom/CLAUDE.md body content untouched (targeted field edit, not a regenerate)"
else
    fail "(#5559) .loom/CLAUDE.md body content was altered"
fi
if grep -q "CLAUDE.md.*restamped version header" <<<"$OUT"; then
    pass "(#5559) apply reports the .loom/CLAUDE.md version-header restamp"
else
    fail "(#5559) apply did not report the .loom/CLAUDE.md restamp"
fi
# Idempotent rerun: second apply is a clean no-op for the header.
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q "CLAUDE.md (version header already" <<<"$OUT"; then
    pass "(#5559) second run reports .loom/CLAUDE.md version header unchanged (idempotent)"
else
    fail "(#5559) second run did not report .loom/CLAUDE.md as unchanged"
fi

echo "Test group 12k: .loom/CLAUDE.md missing (pre-#4239 layout) is not created by resync"
REPO="$(make_fixture)"
rm -f "$REPO/.loom/CLAUDE.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && [[ ! -f "$REPO/.loom/CLAUDE.md" ]]; then
    pass "(#5559) apply with no installed .loom/CLAUDE.md exits 0 and does not create it"
else
    fail "(#5559) apply with no installed .loom/CLAUDE.md misbehaved (rc=$RC)"
fi

echo "Test group 12l: --dry-run previews the .loom/CLAUDE.md restamp without writing"
REPO="$(make_fixture)"
printf '**Loom Version**: 0.16.0\nLast updated: 2026-07-29\n' > "$REPO/.loom/CLAUDE.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
if grep -q '\*\*Loom Version\*\*: 0.16.0' "$REPO/.loom/CLAUDE.md"; then
    pass "(#5559) --dry-run leaves .loom/CLAUDE.md unstamped"
else
    fail "(#5559) --dry-run wrote to .loom/CLAUDE.md"
fi
if grep -q "would update.*CLAUDE.md" <<<"$OUT"; then
    pass "(#5559) --dry-run previews the .loom/CLAUDE.md restamp"
else
    fail "(#5559) --dry-run did not preview the .loom/CLAUDE.md restamp"
fi

echo "Test group 12m: .loom/resync-ignore pins .loom/CLAUDE.md against the version-header restamp"
REPO="$(make_fixture)"
printf '**Loom Version**: 0.16.0\nLast updated: 2026-07-29\n' > "$REPO/.loom/CLAUDE.md"
printf '.loom/CLAUDE.md  # keep my pinned header\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q '\*\*Loom Version\*\*: 0.16.0' "$REPO/.loom/CLAUDE.md"; then
    pass "(#5559) pinned .loom/CLAUDE.md header NOT restamped"
else
    fail "(#5559) pinned .loom/CLAUDE.md header was restamped despite resync-ignore"
fi
if grep -q "skipped.*CLAUDE.md" <<<"$OUT"; then
    pass "(#5559) pinned .loom/CLAUDE.md reported as skipped"
else
    fail "(#5559) pinned .loom/CLAUDE.md not reported skipped"
fi

# --- (#6612) targeted field edit: root CLAUDE.md version-header restamp ----
echo "Test group 12s: root CLAUDE.md version header restamp (#6612)"
REPO="$(make_fixture)"
printf '# Loom Orchestration - Repository Guide\n\n**Loom Version**: 0.16.0\n**Installation Date**: 2020-01-01\n\nBody text unaffected.\n\n**Generated by Loom Installation Process**\nLast updated: 2026-07-29\n' \
    > "$REPO/CLAUDE.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#6612) apply with a stale root CLAUDE.md header exits 0"; else fail "(#6612) apply exits 0 (got $RC)"; fi
if grep -q '\*\*Loom Version\*\*: 9.9.9' "$REPO/CLAUDE.md"; then
    pass "(#6612) root CLAUDE.md Loom Version header restamped to source version"
else
    fail "(#6612) root CLAUDE.md Loom Version header NOT restamped"
fi
if grep -q "^Last updated: $(date +%Y-%m-%d)\$" "$REPO/CLAUDE.md"; then
    pass "(#6612) root CLAUDE.md Last updated footer restamped to today"
else
    fail "(#6612) root CLAUDE.md Last updated footer NOT restamped"
fi
if grep -q '\*\*Installation Date\*\*: 2020-01-01' "$REPO/CLAUDE.md"; then
    pass "(#6612) root CLAUDE.md Installation Date header left untouched (original install date, not a resync stamp)"
else
    fail "(#6612) root CLAUDE.md Installation Date header was altered"
fi
if grep -q "Body text unaffected." "$REPO/CLAUDE.md"; then
    pass "(#6612) root CLAUDE.md body content untouched (targeted field edit, not a regenerate)"
else
    fail "(#6612) root CLAUDE.md body content was altered"
fi
if grep -q "CLAUDE.md.*restamped version header.*#6612" <<<"$OUT"; then
    pass "(#6612) apply reports the root CLAUDE.md version-header restamp"
else
    fail "(#6612) apply did not report the root CLAUDE.md restamp"
fi
# Idempotent rerun: second apply is a clean no-op for the header.
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q "CLAUDE.md (version header already" <<<"$OUT"; then
    pass "(#6612) second run reports root CLAUDE.md version header unchanged (idempotent)"
else
    fail "(#6612) second run did not report root CLAUDE.md as unchanged"
fi

echo "Test group 12t: root CLAUDE.md missing is not created by resync"
REPO="$(make_fixture)"
rm -f "$REPO/CLAUDE.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && [[ ! -f "$REPO/CLAUDE.md" ]]; then
    pass "(#6612) apply with no installed root CLAUDE.md exits 0 and does not create it"
else
    fail "(#6612) apply with no installed root CLAUDE.md misbehaved (rc=$RC)"
fi

echo "Test group 12u: --dry-run previews the root CLAUDE.md restamp without writing"
REPO="$(make_fixture)"
printf '**Loom Version**: 0.16.0\nLast updated: 2026-07-29\n' > "$REPO/CLAUDE.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
if grep -q '\*\*Loom Version\*\*: 0.16.0' "$REPO/CLAUDE.md"; then
    pass "(#6612) --dry-run leaves root CLAUDE.md unstamped"
else
    fail "(#6612) --dry-run wrote to root CLAUDE.md"
fi
if grep -q "would update.*CLAUDE.md" <<<"$OUT"; then
    pass "(#6612) --dry-run previews the root CLAUDE.md restamp"
else
    fail "(#6612) --dry-run did not preview the root CLAUDE.md restamp"
fi

echo "Test group 12v: .loom/resync-ignore pins root CLAUDE.md against the version-header restamp"
REPO="$(make_fixture)"
printf '**Loom Version**: 0.16.0\nLast updated: 2026-07-29\n' > "$REPO/CLAUDE.md"
printf 'CLAUDE.md  # keep my pinned header\n' > "$REPO/.loom/resync-ignore"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q '\*\*Loom Version\*\*: 0.16.0' "$REPO/CLAUDE.md"; then
    pass "(#6612) pinned root CLAUDE.md header NOT restamped"
else
    fail "(#6612) pinned root CLAUDE.md header was restamped despite resync-ignore"
fi
if grep -q "skipped.*CLAUDE.md" <<<"$OUT"; then
    pass "(#6612) pinned root CLAUDE.md reported as skipped"
else
    fail "(#6612) pinned root CLAUDE.md not reported skipped"
fi

echo "Test group 12w: root CLAUDE.md with no Loom Version header is left byte-unchanged (#6621)"
REPO="$(make_fixture)"
BEFORE_CONTENT='# Some Repo Guide

No Loom version header anywhere in this file.
'
printf '%s' "$BEFORE_CONTENT" > "$REPO/CLAUDE.md"
BEFORE_SUM="$(shasum "$REPO/CLAUDE.md")"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
AFTER_SUM="$(shasum "$REPO/CLAUDE.md")"
if [[ $RC -eq 0 ]]; then pass "(#6621) apply on a headerless root CLAUDE.md exits 0"; else fail "(#6621) apply exits 0 (got $RC)"; fi
if [[ "$BEFORE_SUM" == "$AFTER_SUM" ]]; then
    pass "(#6621) headerless root CLAUDE.md left byte-unchanged"
else
    fail "(#6621) headerless root CLAUDE.md was rewritten despite having no Loom Version header"
fi
if grep -q "CLAUDE.md.*restamped version header" <<<"$OUT"; then
    fail "(#6621) apply falsely reported a restamp for a headerless root CLAUDE.md"
else
    pass "(#6621) apply does NOT report a phantom restamp for a headerless root CLAUDE.md"
fi

echo "Test group 12x: root CLAUDE.md with an unrelated 'Last updated:' line but no Loom Version header is untouched (#6621)"
REPO="$(make_fixture)"
printf 'Last updated: 2020-01-01 by a human, unrelated to Loom.\n' > "$REPO/CLAUDE.md"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
if grep -q '^Last updated: 2020-01-01 by a human, unrelated to Loom\.$' "$REPO/CLAUDE.md"; then
    pass "(#6621) unrelated 'Last updated:' line survives when no Loom Version header is present"
else
    fail "(#6621) unrelated 'Last updated:' line was clobbered despite no Loom Version header"
fi

# --- (#4403) canonical-guard-defer: git-tracked target must NOT be removed --
echo "Test group 14: canonical guard present + tracked vendored guard is preserved (#4403)"
REPO="$(make_fixture)"
mkdir -p "$REPO/.claude/skills/repo/hooks"
printf '#!/usr/bin/env bash\n# rjwalters/repo#29 canonical guard\n# implements worktree-write-confinement\n# masks --comment|--search and --arg|--argjson\n# denies gh-comment-body-literal-at\necho canonical\n' \
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
printf '#!/usr/bin/env bash\n# rjwalters/repo#29 canonical guard\n# implements worktree-write-confinement\n# masks --comment|--search and --arg|--argjson\n# denies gh-comment-body-literal-at\necho canonical\n' \
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

# --- (#5916) search/jq-mask-gap canonical guard: vendored guard must be KEPT -
# The canonical guard carries the repo#29 VERSION marker AND the
# write-confinement CAPABILITY marker, but NOT the --comment|--search /
# --arg|--argjson search/jq-mask CAPABILITY markers (today's real-world Repo
# Skills shape that motivated #5916, since rjwalters/repo has not yet ported
# an equivalent search/jq masking fix upstream). This untracked vendored copy
# must stay, mirroring Test group 15b's #4894 coverage for the new probe (c) —
# stripping it here would leave zero destructive-command coverage once the
# dispatcher (correctly, post-#5916) declines to exec a canonical guard that
# fails the search/jq-mask capability probe.
echo "Test group 15c: canonical guard has version + write-confinement markers but NOT search/jq-mask markers -> vendored guard is kept, not removed (#5916)"
REPO="$(make_fixture)"
mkdir -p "$REPO/.claude/skills/repo/hooks"
printf '#!/usr/bin/env bash\n# rjwalters/repo#29 canonical guard\n# implements worktree-write-confinement\necho canonical\n' \
    > "$REPO/.claude/skills/repo/hooks/guard-destructive.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/defaults/hooks/guard-destructive-generic.sh"
printf '#!/usr/bin/env bash\necho vendored\n' \
    > "$REPO/.loom/hooks/guard-destructive-generic.sh"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then pass "(#5916) apply exits 0 with a search/jq-mask-gap canonical guard present"; else fail "(#5916) apply exits 0 (got $RC)"; fi
if [[ -f "$REPO/.loom/hooks/guard-destructive-generic.sh" ]]; then
    pass "(#5916) vendored guard-destructive-generic.sh is KEPT (dispatcher's fallback preserved)"
else
    fail "(#5916) vendored guard-destructive-generic.sh was removed despite the canonical guard lacking search/jq-mask markers"
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
# #6032: defaults/hooks/ is included (empty) purely so resolve_defaults()
# resolves this fixture via priority 1 (co-located defaults/ tree) on every
# run, not via the install-metadata.json "loom_source" compatibility fallback
# (priority 3) -- otherwise the second (idempotency) run below would lose its
# only source-tree resolution path the moment restamp_metadata() strips the
# legacy loom_source field on the first run, which is an accurate reflection
# of a real dogfood/consumer install (always has EITHER a co-located
# defaults/ tree OR the .loom/loom-source-path sidecar) rather than a
# regression in the fix itself.
mkdir -p "$RUNTIMES_REPO/defaults/hooks" "$RUNTIMES_REPO/defaults/roles" \
         "$RUNTIMES_REPO/defaults/runtimes" "$RUNTIMES_REPO/.loom/roles"
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

# --- (x) retired payload files are removed on resync (#5981) ----------------
echo "Test group 22: retired payload files are removed (#5981)"
# Builds its own throwaway repo so it can install a file at
# .loom/scripts/retired-tool.sh with NO defaults/scripts/retired-tool.sh
# counterpart at all — the exact live-incident shape (defaults/scripts/status.sh
# deleted upstream in #5710, but the installed copy survives every resync
# forever because the walk never visits a file that no longer has a source).
RETIRED_REPO="$WORKDIR/retired-repo"
rm -rf "$RETIRED_REPO"
mkdir -p "$RETIRED_REPO/defaults/scripts" "$RETIRED_REPO/.loom/scripts"
git -C "$RETIRED_REPO" init -q
printf 'scripts/retired-tool.sh   # #5981 test fixture\n' > "$RETIRED_REPO/defaults/.loom-retired.list"
printf '#!/usr/bin/env bash\necho retired\n' > "$RETIRED_REPO/.loom/scripts/retired-tool.sh"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$RETIRED_REPO" > "$RETIRED_REPO/.loom/install-metadata.json"
git -C "$RETIRED_REPO" add -A >/dev/null 2>&1
git -C "$RETIRED_REPO" commit -qm "fixture" >/dev/null 2>&1

# --dry-run must report the removal (as drift, exit 2) without deleting.
OUT="$(cd "$RETIRED_REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(x) --dry-run with a retired file present exits 2 (drift)"
else
    fail "(x) --dry-run with a retired file present exits 2 (got $RC)"
fi
if grep -q "would remove.*scripts/retired-tool.sh" <<<"$OUT"; then
    pass "(x) --dry-run reports 'would remove' for the retired file"
else
    fail "(x) --dry-run did not report the retired file as 'would remove'"
fi
if [[ -f "$RETIRED_REPO/.loom/scripts/retired-tool.sh" ]]; then
    pass "(x) --dry-run left the retired file in place"
else
    fail "(x) --dry-run deleted the retired file (should only preview)"
fi

# apply: the retired file must actually be removed and reported.
OUT="$(cd "$RETIRED_REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(x) apply exits 0"
else
    fail "(x) apply did not exit 0 (got $RC)"
fi
if [[ ! -e "$RETIRED_REPO/.loom/scripts/retired-tool.sh" ]]; then
    pass "(x) apply removed the retired file"
else
    fail "(x) apply did not remove the retired file"
fi
if grep -q "removed.*scripts/retired-tool.sh" <<<"$OUT"; then
    pass "(x) apply reports 'removed' for the retired file"
else
    fail "(x) apply did not report the retired file as 'removed'"
fi

# idempotent rerun: nothing left to remove, clean no-op.
OUT="$(cd "$RETIRED_REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(x) retired-file removal is idempotent (second run already in sync)"
else
    fail "(x) retired-file removal is not idempotent (rc=$RC)"
fi

# .loom/resync-ignore pins a retired file against removal.
RETIRED_REPO2="$WORKDIR/retired-repo-pinned"
rm -rf "$RETIRED_REPO2"
mkdir -p "$RETIRED_REPO2/defaults/scripts" "$RETIRED_REPO2/.loom/scripts"
git -C "$RETIRED_REPO2" init -q
printf 'scripts/retired-tool.sh\n' > "$RETIRED_REPO2/defaults/.loom-retired.list"
printf 'KEEP-MY-FORK\n' > "$RETIRED_REPO2/.loom/scripts/retired-tool.sh"
printf 'scripts/retired-tool.sh  # I still use this locally\n' > "$RETIRED_REPO2/.loom/resync-ignore"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$RETIRED_REPO2" > "$RETIRED_REPO2/.loom/install-metadata.json"
git -C "$RETIRED_REPO2" add -A >/dev/null 2>&1
git -C "$RETIRED_REPO2" commit -qm "fixture" >/dev/null 2>&1

OUT="$(cd "$RETIRED_REPO2" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "skipped.*scripts/retired-tool.sh" <<<"$OUT"; then
    pass "(x) .loom/resync-ignore pin reports the retired file as skipped"
else
    fail "(x) .loom/resync-ignore pin did not report the retired file as skipped (rc=$RC)"
fi
if [[ "$(cat "$RETIRED_REPO2/.loom/scripts/retired-tool.sh" 2>/dev/null)" == "KEEP-MY-FORK" ]]; then
    pass "(x) .loom/resync-ignore pin preserved the retired file's local fork"
else
    fail "(x) .loom/resync-ignore pin did not preserve the retired file's local fork"
fi

# a retired-list entry naming a file that was never installed is a no-op.
RETIRED_REPO3="$WORKDIR/retired-repo-absent"
rm -rf "$RETIRED_REPO3"
mkdir -p "$RETIRED_REPO3/defaults/scripts" "$RETIRED_REPO3/.loom/scripts"
git -C "$RETIRED_REPO3" init -q
printf 'scripts/never-installed.sh\n' > "$RETIRED_REPO3/defaults/.loom-retired.list"
printf '{\n  "loom_version": "0.0.0",\n  "loom_commit": "old",\n  "install_date": "2020-01-01",\n  "loom_source": "%s",\n  "installed_files": []\n}\n' \
    "$RETIRED_REPO3" > "$RETIRED_REPO3/.loom/install-metadata.json"
git -C "$RETIRED_REPO3" add -A >/dev/null 2>&1
git -C "$RETIRED_REPO3" commit -qm "fixture" >/dev/null 2>&1

OUT="$(cd "$RETIRED_REPO3" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -q "Already in sync" <<<"$OUT"; then
    pass "(x) a retired entry with no installed counterpart is a silent no-op"
else
    fail "(x) a retired entry with no installed counterpart was not a clean no-op (rc=$RC)"
fi

# --- (#5980) crash-detection marker -------------------------------------------
echo "Test group 23: crash-detection marker (#5980)"
MARKER_REL=".loom/.resync-in-progress"

# (a) a clean, fully-successful apply never leaves the marker behind.
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" >/dev/null 2>&1)
if [[ ! -e "$REPO/$MARKER_REL" ]]; then
    pass "(#5980) a successful apply leaves no marker behind"
else
    fail "(#5980) a successful apply left the marker file behind"
fi

# (b) --dry-run never writes the marker, even with drift present.
REPO="$(make_fixture)"
(cd "$REPO" && bash "$SCRIPT" --dry-run >/dev/null 2>&1)
if [[ ! -e "$REPO/$MARKER_REL" ]]; then
    pass "(#5980) --dry-run never writes the marker"
else
    fail "(#5980) --dry-run wrote the marker (should be preview-only)"
fi

# (c) a leftover marker (simulating a crashed prior run) is detected and
# reported by --dry-run WITHOUT being touched (pure, side-effect-free detector).
REPO="$(make_fixture)"
printf 'target_version=0.1.2\nstarted_at=2020-01-01T00:00:00Z\npid=99999\n' > "$REPO/$MARKER_REL"
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
if grep -qi "previous resync did not complete" <<<"$OUT" && grep -q "0.1.2" <<<"$OUT"; then
    pass "(#5980) --dry-run reports a leftover marker naming the stale target version"
else
    fail "(#5980) --dry-run did not report the leftover marker with its target version"
fi
if [[ "$(cat "$REPO/$MARKER_REL")" == "target_version=0.1.2
started_at=2020-01-01T00:00:00Z
pid=99999" ]]; then
    pass "(#5980) --dry-run leaves the leftover marker byte-identical (preview-only)"
else
    fail "(#5980) --dry-run modified the leftover marker"
fi

# (d) the SAME leftover marker is also detected (and reported) by a real,
# non-dry-run apply — and since the run completes successfully this time, the
# marker is overwritten and then cleared, leaving the install fully synced.
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if grep -qi "previous resync did not complete" <<<"$OUT" && grep -q "0.1.2" <<<"$OUT"; then
    pass "(#5980) a real apply also detects and reports the leftover marker"
else
    fail "(#5980) a real apply did not report the leftover marker"
fi
if [[ $RC -eq 0 ]]; then
    pass "(#5980) the run restarts from scratch and completes successfully despite the leftover marker"
else
    fail "(#5980) the run did not complete successfully (rc=$RC)"
fi
if [[ ! -e "$REPO/$MARKER_REL" ]]; then
    pass "(#5980) the marker is cleared once this run reaches a full success"
else
    fail "(#5980) the marker was not cleared after a full success"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "A" ]]; then
    pass "(#5980) the restart-from-scratch run still fully resynced (idempotent recovery)"
else
    fail "(#5980) the restart-from-scratch run did not actually resync"
fi

# (e) the marker records the CURRENT run's target version (from source
# package.json, "9.9.9" in the fixture), not a placeholder.
REPO="$(make_fixture)"
# Make the .loom/scripts/lib directory unwritable so the run partially fails
# (mirrors Test group 20's unsyncable-file fixture) -- this lets us inspect
# the marker WHILE it is still present, right after a real (non-dry-run) run
# started but before it reached the success path.
if [[ "$(id -u)" -eq 0 ]]; then
    skip "(#5980) running as root — an unwritable destination cannot be simulated"
else
    chmod 500 "$REPO/.loom/scripts/lib"
    RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)" || RC=$?
    chmod 700 "$REPO/.loom/scripts/lib"
    if [[ $RC -eq 1 ]]; then
        pass "(#5980) a partial refresh still exits 1 (unchanged from #4669)"
    else
        fail "(#5980) a partial refresh did not exit 1 (got $RC)"
    fi
    if [[ -f "$REPO/$MARKER_REL" ]]; then
        pass "(#5980) a PARTIAL refresh leaves the marker in place (never cleared on partial success)"
    else
        fail "(#5980) a PARTIAL refresh incorrectly cleared the marker"
    fi
    if grep -q '^target_version=9\.9\.9$' "$REPO/$MARKER_REL"; then
        pass "(#5980) the marker records the run's actual target version from source package.json"
    else
        fail "(#5980) the marker does not record the expected target version"
    fi
    if grep -q '^started_at=[0-9]' "$REPO/$MARKER_REL" && grep -q '^pid=[0-9]' "$REPO/$MARKER_REL"; then
        pass "(#5980) the marker records a started_at timestamp and a pid"
    else
        fail "(#5980) the marker is missing started_at/pid fields"
    fi
    # Re-running after fixing the cause (same recovery path #4669 documents)
    # completes the refresh AND finally clears the marker.
    OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
    RC=$?
    if grep -qi "previous resync did not complete" <<<"$OUT"; then
        pass "(#5980) the retry detects and reports the marker the partial run left behind"
    else
        fail "(#5980) the retry did not report the marker from the prior partial run"
    fi
    if [[ $RC -eq 0 && ! -e "$REPO/$MARKER_REL" ]]; then
        pass "(#5980) fixing the cause and re-running completes the refresh and clears the marker"
    else
        fail "(#5980) re-running after fixing the cause did not clear the marker (rc=$RC)"
    fi
fi

# --- (#6106) --output stages a complete resync without touching REPO_ROOT ---
#
# --output <dir> must: (1) never write anything under the invoking repo's own
# checkout, (2) still be permitted from a LINKED worktree (the #4563 refusal
# is the whole reason this mode exists), (3) produce a real, independent git
# worktree at <dir> that actually received the resync, (4) refuse a <dir>
# that already exists, and (5) leave no residue when combined with --dry-run.
echo "Test group 24: --output stages a complete resync in an isolated worktree (#6106)"
REPO="$(make_fixture)"
STAGE="$WORKDIR/output-stage"
rm -rf "$STAGE"
OUT="$(cd "$REPO" && bash "$SCRIPT" --output "$STAGE" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(#6106) --output apply exits 0"
else
    fail "(#6106) --output apply exits 0 (got $RC)"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(#6106) --output leaves the invoking repo's drifted hooks/guard.sh UNCHANGED"
else
    fail "(#6106) --output wrote into the invoking repo's own checkout"
fi
if [[ ! -f "$REPO/.loom/scripts/lib/bar.sh" ]]; then
    pass "(#6106) --output did not create the missing file in the invoking repo"
else
    fail "(#6106) --output created a file in the invoking repo's own checkout"
fi
if [[ -e "$STAGE/.git" ]]; then
    pass "(#6106) --output <dir> is a real, independent git checkout"
else
    fail "(#6106) --output <dir> is not a git checkout"
fi
if [[ "$(cat "$STAGE/.loom/hooks/guard.sh" 2>/dev/null)" == "A" ]]; then
    pass "(#6106) --output <dir> received the resynced (drift-fixed) file"
else
    fail "(#6106) --output <dir> did not receive the resync"
fi
if [[ -f "$STAGE/.loom/scripts/lib/bar.sh" && "$(cat "$STAGE/.loom/scripts/lib/bar.sh")" == "L" ]]; then
    pass "(#6106) --output <dir> received a file missing from the invoking repo"
else
    fail "(#6106) --output <dir> did not receive the missing file"
fi
if grep -q '"loom_version": *"9.9.9"' "$STAGE/.loom/install-metadata.json" 2>/dev/null; then
    pass "(#6106) --output <dir>'s install-metadata.json was re-stamped (not the invoking repo's)"
else
    fail "(#6106) --output <dir>'s install-metadata.json was not re-stamped"
fi
if grep -q '"loom_version": *"0.0.0"' "$REPO/.loom/install-metadata.json"; then
    pass "(#6106) the invoking repo's own install-metadata.json was NOT re-stamped"
else
    fail "(#6106) the invoking repo's own install-metadata.json was unexpectedly re-stamped"
fi
if grep -qi "primary checkout" <<<"$OUT" || grep -qi "never touched" <<<"$OUT"; then
    pass "(#6106) apply prints a 'primary checkout untouched' confirmation"
else
    fail "(#6106) apply did not confirm the primary checkout was untouched"
fi
if grep -q "git add -A" <<<"$OUT" && grep -q "git commit" <<<"$OUT" && grep -q "worktree remove" <<<"$OUT"; then
    pass "(#6106) apply prints the commit + cleanup next-steps"
else
    fail "(#6106) apply did not print the expected next-steps"
fi
git -C "$REPO" worktree remove --force "$STAGE" >/dev/null 2>&1 || true

# --output from a LINKED worktree is the whole point of this mode: allowed,
# even though a bare (no --output) run from the same worktree is refused.
LINKED_OUT_WT="$WORKDIR/linked-output-wt"
rm -rf "$LINKED_OUT_WT"
if git -C "$REPO" worktree add -q -b wt-6106 "$LINKED_OUT_WT" >/dev/null 2>&1; then
    STAGE2="$WORKDIR/output-stage-from-linked"
    rm -rf "$STAGE2"
    RC=0; OUT="$(cd "$LINKED_OUT_WT" && bash "$SCRIPT" --output "$STAGE2" 2>&1)" || RC=$?
    if [[ $RC -eq 0 ]]; then
        pass "(#6106) --output is permitted from a linked worktree (exit 0)"
    else
        fail "(#6106) --output was refused from a linked worktree (got $RC)"
    fi
    if [[ "$(cat "$STAGE2/.loom/hooks/guard.sh" 2>/dev/null)" == "A" ]]; then
        pass "(#6106) --output from a linked worktree still produces a correct resync"
    else
        fail "(#6106) --output from a linked worktree did not resync correctly"
    fi
    if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
        pass "(#6106) --output from a linked worktree still leaves the MAIN checkout untouched"
    else
        fail "(#6106) --output from a linked worktree wrote into the main checkout"
    fi
    git -C "$REPO" worktree remove --force "$STAGE2" >/dev/null 2>&1 || true
    git -C "$REPO" worktree remove --force "$LINKED_OUT_WT" >/dev/null 2>&1 || true
else
    skip "(#6106) git worktree add unavailable in this environment"
fi

# --output refuses a directory that already exists.
STAGE3="$WORKDIR/output-stage-exists"
mkdir -p "$STAGE3"
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" --output "$STAGE3" 2>&1)" || RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(#6106) --output refuses an already-existing directory (exit 1)"
else
    fail "(#6106) --output did not refuse an already-existing directory (got $RC)"
fi
if grep -qi "already exists" <<<"$OUT"; then
    pass "(#6106) the already-exists refusal explains why"
else
    fail "(#6106) the already-exists refusal does not explain why"
fi
rmdir "$STAGE3" 2>/dev/null || true

# --dry-run + --output creates the staging worktree only as the preview's
# target, then removes it before exiting -- a preview must leave no residue.
STAGE4="$WORKDIR/output-stage-dryrun"
rm -rf "$STAGE4"
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run --output "$STAGE4" 2>&1)" || RC=$?
if [[ $RC -eq 2 ]]; then
    pass "(#6106) --dry-run --output exits 2 (drift detected)"
else
    fail "(#6106) --dry-run --output did not exit 2 (got $RC)"
fi
if [[ ! -e "$STAGE4" ]]; then
    pass "(#6106) --dry-run --output leaves no staging worktree behind"
else
    fail "(#6106) --dry-run --output left the staging directory behind"
    git -C "$REPO" worktree remove --force "$STAGE4" >/dev/null 2>&1 || rm -rf "$STAGE4"
fi
if ! git -C "$REPO" worktree list | grep -q "$STAGE4"; then
    pass "(#6106) --dry-run --output leaves no dangling worktree registration"
else
    fail "(#6106) --dry-run --output left a dangling worktree registration"
fi

# LOOM_RESYNC_OUTPUT env var is equivalent to --output.
STAGE5="$WORKDIR/output-stage-env"
rm -rf "$STAGE5"
RC=0; (cd "$REPO" && LOOM_RESYNC_OUTPUT="$STAGE5" bash "$SCRIPT" >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 0 && "$(cat "$STAGE5/.loom/hooks/guard.sh" 2>/dev/null)" == "A" ]]; then
    pass "(#6106) LOOM_RESYNC_OUTPUT=<dir> is equivalent to --output <dir>"
else
    fail "(#6106) LOOM_RESYNC_OUTPUT=<dir> did not behave like --output <dir> (rc=$RC)"
fi
git -C "$REPO" worktree remove --force "$STAGE5" >/dev/null 2>&1 || true

# --output requires a value.
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" --output 2>&1)" || RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(#6106) --output with no value exits 1"
else
    fail "(#6106) --output with no value did not exit 1 (got $RC)"
fi

# --- (#6138) resolve_defaults() failure with --output must not leak the
# staging git worktree it already created -------------------------------------
echo "Test group 25: --output cleans up the staging worktree when defaults/ source resolution fails (#6138)"
REPO="$(make_fixture)"
rm -rf "$REPO/defaults"                             # no dogfood defaults/ tree
rm -f "$REPO/.loom/loom-source-path"                # no source sidecar
printf '{}\n' > "$REPO/.loom/install-metadata.json" # no usable loom_source
STAGE6="$WORKDIR/output-stage-no-source"
rm -rf "$STAGE6"
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" --output "$STAGE6" 2>&1)" || RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(#6138) --output exits 1 when resolve_defaults() fails"
else
    fail "(#6138) --output did not exit 1 when resolve_defaults() fails (got $RC)"
fi
if [[ ! -e "$STAGE6" ]]; then
    pass "(#6138) --output leaves no staging directory behind after a resolve_defaults() failure"
else
    fail "(#6138) --output left the staging directory behind after a resolve_defaults() failure"
    git -C "$REPO" worktree remove --force "$STAGE6" >/dev/null 2>&1 || rm -rf "$STAGE6"
fi
if ! git -C "$REPO" worktree list | grep -q "$STAGE6"; then
    pass "(#6138) --output leaves no dangling worktree registration after a resolve_defaults() failure"
else
    fail "(#6138) --output left a dangling worktree registration after a resolve_defaults() failure"
fi

# A retry with the SAME --output <dir> after the failure must succeed WITHOUT
# requiring manual `git worktree remove` / `rm -rf` + `git worktree prune` --
# confirms the leaked registration doesn't wedge the retry path. Uses a fresh,
# fully-resolvable fixture repo (rather than patching the broken $REPO) so
# only the --output <dir> reuse itself is under test.
REPO_RETRY="$(make_fixture)"
RC=0; OUT="$(cd "$REPO_RETRY" && bash "$SCRIPT" --output "$STAGE6" 2>&1)" || RC=$?
if [[ $RC -eq 0 ]]; then
    pass "(#6138) a retry with the same --output <dir> succeeds without manual cleanup"
else
    fail "(#6138) a retry with the same --output <dir> did not succeed (got $RC)"
fi
git -C "$REPO_RETRY" worktree remove --force "$STAGE6" >/dev/null 2>&1 || true

# --dry-run --output must also leave no residue on a resolve_defaults() failure.
REPO="$(make_fixture)"
rm -rf "$REPO/defaults"
rm -f "$REPO/.loom/loom-source-path"
printf '{}\n' > "$REPO/.loom/install-metadata.json"
STAGE7="$WORKDIR/output-stage-no-source-dryrun"
rm -rf "$STAGE7"
RC=0; OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run --output "$STAGE7" 2>&1)" || RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(#6138) --dry-run --output exits 1 when resolve_defaults() fails"
else
    fail "(#6138) --dry-run --output did not exit 1 when resolve_defaults() fails (got $RC)"
fi
if [[ ! -e "$STAGE7" ]]; then
    pass "(#6138) --dry-run --output leaves no staging directory behind after a resolve_defaults() failure"
else
    fail "(#6138) --dry-run --output left the staging directory behind after a resolve_defaults() failure"
    git -C "$REPO" worktree remove --force "$STAGE7" >/dev/null 2>&1 || rm -rf "$STAGE7"
fi
if ! git -C "$REPO" worktree list | grep -q "$STAGE7"; then
    pass "(#6138) --dry-run --output leaves no dangling worktree registration after a resolve_defaults() failure"
else
    fail "(#6138) --dry-run --output left a dangling worktree registration after a resolve_defaults() failure"
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
if grep -q -- "--output" <<<"$HELP_OUT" && grep -qi "staging" <<<"$HELP_OUT"; then
    pass "(#6106) --help documents --output staging mode"
else
    fail "(#6106) --help does not document --output staging mode"
fi

REPO="$(make_fixture)"
RC=0; (cd "$REPO" && bash "$SCRIPT" --bogus >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 1 ]]; then pass "unknown arg exits 1"; else fail "unknown arg did not exit 1 (got $RC)"; fi

NON_REPO="$WORKDIR/not-a-repo"
mkdir -p "$NON_REPO"
RC=0; (cd "$NON_REPO" && bash "$SCRIPT" >/dev/null 2>&1) || RC=$?
if [[ $RC -eq 1 ]]; then pass "outside a git repo exits 1"; else fail "outside a git repo did not exit 1 (got $RC)"; fi

# --- pre-resync shell-syntax gate (#6162 AC1/AC2) ----------------------------
#
# make_fixture()'s synthetic defaults/scripts/ tree does not include a real
# copy of check-shell-syntax.sh (its files are one-liner placeholders, not
# copies of the real repo), so the gate would silently no-op (degrading to a
# WARN, by design — see resync-installed.sh's own comment on a missing check
# script) unless a real, working copy is placed at the exact path the gate
# looks for. Copy the ACTUAL script under test alongside the fixture's other
# scripts so these tests exercise the real preflight, not a stubbed-out one.
echo "Test group 26: pre-resync shell-syntax gate refuses to copy a non-parsing source script"
REPO="$(make_fixture)"
cp "$HELPERS_DIR/check-shell-syntax.sh" "$REPO/defaults/scripts/check-shell-syntax.sh"
chmod +x "$REPO/defaults/scripts/check-shell-syntax.sh"
cat > "$REPO/defaults/scripts/broken.sh" <<'EOF'
#!/usr/bin/env bash
echo start
<<<<<<< Updated upstream
echo one
=======
echo two
>>>>>>> Stashed changes
EOF

OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(#6162) resync hard-fails (exit 1) when a source script does not parse"
else
    fail "(#6162) expected exit 1 when a source script does not parse (got $RC)"
fi
if grep -q "broken.sh" <<<"$OUT"; then
    pass "(#6162) the offending file is named in the output"
else
    fail "(#6162) the offending file was not named in the output"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(#6162) refuses to resync: drifted hooks/guard.sh left UNCHANGED (nothing copied)"
else
    fail "(#6162) hooks/guard.sh was copied despite the syntax failure elsewhere"
fi
if [[ ! -f "$REPO/.loom/scripts/lib/bar.sh" ]]; then
    pass "(#6162) refuses to resync: missing scripts/lib/bar.sh was NOT created"
else
    fail "(#6162) scripts/lib/bar.sh was created despite the syntax failure elsewhere"
fi

# --dry-run also refuses (a preview must not paper over a real syntax error).
OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 1 ]] && grep -q "broken.sh" <<<"$OUT"; then
    pass "(#6162) --dry-run also refuses on a non-parsing source script (exit 1)"
else
    fail "(#6162) --dry-run did not refuse on a non-parsing source script (got $RC)"
fi

echo "Test group 26b: pre-resync shell-syntax gate adds no friction on a clean/valid tree"
REPO="$(make_fixture)"
cp "$HELPERS_DIR/check-shell-syntax.sh" "$REPO/defaults/scripts/check-shell-syntax.sh"
chmod +x "$REPO/defaults/scripts/check-shell-syntax.sh"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] \
   && ! grep -qi "does not parse" <<<"$OUT" \
   && ! grep -qi "Refusing to resync" <<<"$OUT"; then
    pass "(#6162) a normal apply on a fixture with only valid scripts is unaffected by the new gate"
else
    fail "(#6162) the syntax gate introduced friction on a clean/valid tree (rc=$RC); out=$OUT"
fi

# --- pre-resync conflict-marker gate (#6499) ---------------------------------
#
# #6162's gate is `bash -n` on `*.sh` only, so the identical corruption in a
# doc / role prompt / runtime descriptor sails straight through it and gets
# replicated into every consumer's .loom/. Same fixture discipline as group
# 26: place a REAL copy of the checker at the exact path the gate looks for,
# otherwise it degrades to a WARN and these tests would pass vacuously.
echo "Test group 27: pre-resync conflict-marker gate refuses to copy a marker-corrupted NON-shell source file"
REPO="$(make_fixture)"
cp "$HELPERS_DIR/check-conflict-markers.sh" "$REPO/defaults/scripts/check-conflict-markers.sh"
chmod +x "$REPO/defaults/scripts/check-conflict-markers.sh"
mkdir -p "$REPO/defaults/docs"
# Built with printf so this test file's own source carries no line-start
# markers beyond the group-26 fixture it already opts out for.
{
    printf '# Troubleshooting\n'
    printf '<<<<<<< Updated upstream\n'
    printf 'mac guidance\n'
    printf '=======\n'
    printf 'linux guidance\n'
    printf '>>>>>>> Stashed changes\n'
} > "$REPO/defaults/docs/corrupted-doc.md"

OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 1 ]]; then
    pass "(#6499) resync hard-fails (exit 1) on a marker-corrupted non-shell source file"
else
    fail "(#6499) expected exit 1 on a marker-corrupted .md source (got $RC)"
fi
if grep -q "corrupted-doc.md" <<<"$OUT"; then
    pass "(#6499) the offending non-shell file is named in the output"
else
    fail "(#6499) the offending non-shell file was not named in the output; out=$OUT"
fi
if [[ "$(cat "$REPO/.loom/hooks/guard.sh")" == "OLD" ]]; then
    pass "(#6499) refuses to resync: drifted hooks/guard.sh left UNCHANGED (nothing copied)"
else
    fail "(#6499) hooks/guard.sh was copied despite the marker corruption elsewhere"
fi

OUT="$(cd "$REPO" && bash "$SCRIPT" --dry-run 2>&1)"
RC=$?
if [[ $RC -eq 1 ]] && grep -q "corrupted-doc.md" <<<"$OUT"; then
    pass "(#6499) --dry-run also refuses on a marker-corrupted source file (exit 1)"
else
    fail "(#6499) --dry-run did not refuse on a marker-corrupted source file (got $RC)"
fi

echo "Test group 27b: pre-resync conflict-marker gate adds no friction on a clean tree"
REPO="$(make_fixture)"
cp "$HELPERS_DIR/check-conflict-markers.sh" "$REPO/defaults/scripts/check-conflict-markers.sh"
chmod +x "$REPO/defaults/scripts/check-conflict-markers.sh"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && ! grep -qi "conflict markers" <<<"$OUT"; then
    pass "(#6499) a normal apply on a marker-free fixture is unaffected by the new gate"
else
    fail "(#6499) the conflict-marker gate introduced friction on a clean tree (rc=$RC); out=$OUT"
fi

# --- forge label drift check + safe auto-create (#6716) ---------------------
#
# resync-installed.sh's new labels step is a thin dispatcher onto whatever
# sync-labels.sh ends up installed at .loom/scripts/sync-labels.sh (via the
# widened "walk scripts" resync exercised by earlier groups) -- it does not
# re-derive drift/report semantics itself. So rather than re-proving
# sync-labels.sh's own --check behavior (already covered end-to-end by
# test-sync-labels-check.sh), these groups install a scriptable STUB as
# defaults/scripts/sync-labels.sh and assert only on the WIRING: when the
# stub is called, with/without --check, and how its exit code changes the
# resync's own reporting and control flow.
add_labels_stub() {
    local repo="$1"
    mkdir -p "$repo/.github"
    cat > "$repo/.github/labels.yml" <<'EOF'
- name: loom:issue
  description: "Approved and ready for a Builder"
  color: "3B82F6"
EOF
    cat > "$repo/defaults/scripts/sync-labels.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_LOG:?STUB_LOG not set}"
if printf '%s\n' "$*" | grep -q -- '--check'; then
    exit "${LOOM_TEST_LABELS_CHECK_RC:-0}"
fi
exit "${LOOM_TEST_LABELS_SYNC_RC:-0}"
STUB
    chmod +x "$repo/defaults/scripts/sync-labels.sh"
}

echo "Test group 28: forge label check -- already in sync (#6716)"
REPO="$(make_fixture)"
add_labels_stub "$REPO"
STUB_LOG="$WORKDIR/stub28.log"; : > "$STUB_LOG"
OUT="$(cd "$REPO" && STUB_LOG="$STUB_LOG" LOOM_TEST_LABELS_CHECK_RC=0 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -qi "unchanged.*forge labels" <<<"$OUT"; then
    pass "(#6716) an in-sync live label set is reported unchanged"
else
    fail "(#6716) in-sync case not reported as unchanged (rc=$RC); out=$OUT"
fi
if [[ "$(grep -c -- '--check' "$STUB_LOG" || true)" -eq 1 && "$(wc -l < "$STUB_LOG")" -eq 1 ]]; then
    pass "(#6716) in-sync case calls sync-labels.sh exactly once, with --check"
else
    fail "(#6716) unexpected sync-labels.sh invocation count/shape: $(cat "$STUB_LOG")"
fi

echo "Test group 28b: forge label check -- drift found, real run fixes it (#6716)"
REPO="$(make_fixture)"
add_labels_stub "$REPO"
STUB_LOG="$WORKDIR/stub28b.log"; : > "$STUB_LOG"
OUT="$(cd "$REPO" && STUB_LOG="$STUB_LOG" LOOM_TEST_LABELS_CHECK_RC=3 LOOM_TEST_LABELS_SYNC_RC=0 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -qi "drift detected" <<<"$OUT" && grep -qi "updated.*forge labels" <<<"$OUT"; then
    pass "(#6716) drift is reported and then fixed on a real run"
else
    fail "(#6716) drift-then-fix not reported as expected (rc=$RC); out=$OUT"
fi
if [[ "$(wc -l < "$STUB_LOG")" -eq 2 ]] && grep -q -- '--check' "$STUB_LOG" && grep -qv -- '--check' "$STUB_LOG"; then
    pass "(#6716) a real run with drift calls sync-labels.sh twice: once with --check, once without"
else
    fail "(#6716) unexpected sync-labels.sh invocation shape for drift-then-fix: $(cat "$STUB_LOG")"
fi

echo "Test group 28c: forge label check -- drift found, --dry-run previews without fixing (#6716)"
REPO="$(make_fixture)"
add_labels_stub "$REPO"
STUB_LOG="$WORKDIR/stub28c.log"; : > "$STUB_LOG"
OUT="$(cd "$REPO" && STUB_LOG="$STUB_LOG" LOOM_TEST_LABELS_CHECK_RC=3 bash "$SCRIPT" --dry-run 2>&1)"
if grep -qi "drift detected" <<<"$OUT" && grep -qi "would run.*sync-labels.sh" <<<"$OUT"; then
    pass "(#6716) --dry-run previews the label fix instead of applying it"
else
    fail "(#6716) --dry-run did not preview the label fix as expected; out=$OUT"
fi
if [[ "$(wc -l < "$STUB_LOG")" -eq 1 ]] && grep -q -- '--check' "$STUB_LOG"; then
    pass "(#6716) --dry-run calls sync-labels.sh only once (--check), never the mutating path"
else
    fail "(#6716) --dry-run made an unexpected sync-labels.sh call: $(cat "$STUB_LOG")"
fi

echo "Test group 28d: forge label check -- the fix itself fails -> loud warning, not a resync failure (#6716)"
REPO="$(make_fixture)"
add_labels_stub "$REPO"
STUB_LOG="$WORKDIR/stub28d.log"; : > "$STUB_LOG"
OUT="$(cd "$REPO" && STUB_LOG="$STUB_LOG" LOOM_TEST_LABELS_CHECK_RC=3 LOOM_TEST_LABELS_SYNC_RC=1 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -qi "could not fully apply" <<<"$OUT"; then
    pass "(#6716) a failed label fix is a loud warning, not a resync-wide failure"
else
    fail "(#6716) a failed label fix was not reported as a soft warning (rc=$RC); out=$OUT"
fi

echo "Test group 28e: forge label check -- the check itself errors -> loud warning, not a resync failure (#6716)"
REPO="$(make_fixture)"
add_labels_stub "$REPO"
STUB_LOG="$WORKDIR/stub28e.log"; : > "$STUB_LOG"
OUT="$(cd "$REPO" && STUB_LOG="$STUB_LOG" LOOM_TEST_LABELS_CHECK_RC=1 bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && grep -qi "skipped forge label" <<<"$OUT"; then
    pass "(#6716) a --check lookup error is a loud warning, not a resync-wide failure"
else
    fail "(#6716) a --check lookup error was not reported as a soft warning (rc=$RC); out=$OUT"
fi

echo "Test group 28f: forge label check -- no .github/labels.yml -> silently skipped (#6716)"
REPO="$(make_fixture)"
OUT="$(cd "$REPO" && bash "$SCRIPT" 2>&1)"
RC=$?
if [[ $RC -eq 0 ]] && ! grep -qi "forge label" <<<"$OUT"; then
    pass "(#6716) a repo with no .github/labels.yml is unaffected by the new step"
else
    fail "(#6716) a repo with no labels.yml unexpectedly mentioned forge labels (rc=$RC); out=$OUT"
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
